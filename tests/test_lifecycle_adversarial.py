"""Adversarial tests for the guild lifecycle (plan task 11): invited, kicked, confused.

The happy path lives in `test_lifecycle.py`. This is the file for the nights
the bot is offline while a raid kicks it out of forty servers, or while
Discord reports zero guilds because Discord is having a moment. The
questions, roughly in order of how much they'd hurt:

1. Can a bad guild list eat real settings? (The valve, at every boundary
   that matters, and whether anything ever clears a tripped one.)
2. Can an event that lands mid-reconcile leave a row that shouldn't exist,
   or delete one that should? Everything in here is single-threaded asyncio,
   so "race" means "a coroutine that yields at the wrong moment".
3. Does one server's exit, or one server's hostile channel layout, reach
   another server, or take the startup pass down with it?
4. Does anything write a guild's name to a log? (It shouldn't. A server's
   name is its own business.)

Fakes are hand-written and the database is a real temp file with the real
migrations. No network, no real sleeping beyond a few milliseconds. Tests
marked xfail(strict) pin real holes; each reason says what's wrong so the
implement agent doesn't have to rediscover it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from v3_fakes import load_v3_config

from newsbot.bot.client import GuildLifecycle
from newsbot.config import load_config
from newsbot.guilds import lifecycle
from newsbot.guilds.importer import ensure_imported
from newsbot.guilds.lifecycle import FIRST_CONTACT_TEXT, reconcile_plan
from newsbot.store import repo
from newsbot.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
BASE = 500_000_000_000_000_000
G1, G2, G3 = BASE + 1, BASE + 2, BASE + 3
SEND = discord.Permissions(view_channel=True, send_messages=True)
NO_SEND = discord.Permissions(view_channel=True)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CODE = "-".join(c * 5 for c in "ABCDE")


def _http(status=500, exc=discord.HTTPException, text="boom"):
    return exc(SimpleNamespace(status=status, reason="x"), text)


class Chan:
    def __init__(self, channel_id, position=0, perms=SEND, guild=None, error=None, hook=None):
        self.id = channel_id
        self.position = position
        self._perms = perms
        self.guild = guild
        self.sent: list[tuple[str, dict]] = []
        self._error = error
        self._hook = hook

    def permissions_for(self, member):
        return self._perms

    async def send(self, content=None, **kwargs):
        if self._hook is not None:
            await self._hook()
        if self._error is not None:
            raise self._error
        self.sent.append((content, kwargs))


def make_guild(guild_id=G1, system=None, channels=(), me="bot", name="Secret Club Name"):
    return SimpleNamespace(
        id=guild_id, me=me, system_channel=system, text_channels=list(channels), name=name
    )


class Client:
    def __init__(self, guilds=(), ready=True, fetch=None):
        self.guilds = [g if hasattr(g, "id") else make_guild(g) for g in guilds]
        self._ready = ready
        self._fetch = fetch
        self.fetched: list[int] = []

    def is_ready(self):
        return self._ready

    async def fetch_guild(self, guild_id):
        self.fetched.append(guild_id)
        if self._fetch is not None:
            return await self._fetch(guild_id)
        return SimpleNamespace(id=guild_id)


@pytest.fixture
def cfg(monkeypatch):
    return load_v3_config(monkeypatch)


@pytest.fixture
def alerts():
    return []


def _alert_into(alerts):
    async def alert(text):
        alerts.append(text)

    return alert


@pytest.fixture
def life(v3_db, cfg, alerts):
    return GuildLifecycle(v3_db, cfg, _alert_into(alerts))


def _ids(db):
    with closing(connect(db)) as conn:
        return [g.guild_id for g in repo.list_guilds(conn)]


def _tier(db, guild_id):
    with closing(connect(db)) as conn:
        return repo.get_guild(conn, guild_id).tier


def _seed(db, *ids, **kw):
    with closing(connect(db)) as conn:
        for gid in ids:
            repo.create_guild(conn, gid, **kw)


def _count(db, table, where="1=1", args=()):
    with closing(connect(db)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]  # noqa: S608


# --- 1. the valve under realistic numbers ---


def _expected_trip(n_db: int, n_stale: int) -> bool:
    """The valve rule, restated independently in integers: stale > max(3, 10% of all rows)."""
    return n_stale > 3 and n_stale * 10 > n_db


@pytest.mark.parametrize("n_db", [1, 2, 3, 4, 10, 30, 31, 100, 1000])
def test_valve_boundary_at_every_realistic_size(n_db):
    db = list(range(1, n_db + 1))
    seen_trip = False
    for n_stale in sorted({0, 1, 2, 3, 4, 5, 10, 11, 30, 31, 100, 101, n_db - 1, n_db}):
        if n_stale > n_db:
            continue
        # one survivor guarantees a non-empty live list for the percentage rule
        live = [*db[n_stale:], 10**9] if n_stale < n_db else [10**9]
        plan = reconcile_plan(db, live)
        if n_stale == 0:
            assert plan.to_delete == () and not plan.valve_tripped
            continue
        assert plan.valve_tripped is _expected_trip(n_db, n_stale), (n_db, n_stale)
        if plan.valve_tripped:
            assert plan.to_delete == () and plan.to_confirm == ()
            seen_trip = True
        else:
            assert list(plan.to_delete) == db[:n_stale]
    assert seen_trip or n_db >= 1000 or n_db <= 30


@pytest.mark.parametrize("n_db", [1, 2, 3, 4, 10, 30, 1000])
def test_valve_an_empty_live_list_always_trips_for_any_nonempty_db(n_db):
    plan = reconcile_plan(range(n_db), [])
    assert plan.valve_tripped and plan.to_delete == () and plan.to_confirm == ()


def test_valve_three_stale_of_three_with_one_live_elsewhere_trips():
    # All three rows gone but the bot sees some other server: 3 is not > 3, so this deletes
    # everything. Pinned: a total wipe of a 3-row database is allowed when the list is non-empty.
    plan = reconcile_plan([1, 2, 3], [99])
    assert not plan.valve_tripped and plan.to_delete == (1, 2, 3) and plan.to_create == (99,)


def test_valve_small_database_loses_every_row_to_a_one_guild_partial_list():
    # The documented gap in the percentage rule: with 3 or fewer rows, "the cache came back with
    # one wrong guild in it" looks the same as "we were all kicked". Nothing to fix cheaply.
    plan = reconcile_plan([1, 2, 3], [4])
    assert plan.to_delete == (1, 2, 3)


def test_valve_imported_guild_counts_toward_the_limit_and_is_never_asked_about_when_tripped():
    plan = reconcile_plan(range(1, 11), [9, 10], protected_ids=[1])
    assert plan.valve_tripped and plan.to_confirm == ()


def test_valve_imported_guild_is_confirmed_not_deleted_under_the_limit():
    plan = reconcile_plan(range(1, 11), [3, 4, 5, 6, 7, 8, 9, 10], protected_ids=[1])
    assert plan.to_delete == (2,) and plan.to_confirm == (1,)


def test_valve_reason_carries_counts_and_no_ids():
    ids = [BASE + i for i in range(1000)]
    plan = reconcile_plan(ids, ids[500:])
    assert plan.valve_tripped
    assert len(plan.valve_reason) < 200
    assert not any(str(i) in plan.valve_reason for i in ids[:50])
    assert "500 of 1000" in plan.valve_reason


async def test_owner_alert_has_no_ids_no_names_and_is_capped(v3_db, cfg, alerts):
    ids = [BASE + i for i in range(1000)]
    with closing(connect(v3_db)) as conn:
        for gid in ids:
            repo.create_guild(conn, gid)
    life = GuildLifecycle(v3_db, cfg, _alert_into(alerts))
    client = Client([make_guild(g, name="Hidden Name") for g in ids[:400]])
    plan = await life.reconcile(client)
    assert plan.valve_tripped and len(_ids(v3_db)) == 1000
    assert len(alerts) == 1
    text = alerts[0]
    assert len(text) < 400
    assert "Hidden Name" not in text
    assert not any(str(g) in text for g in ids[:100])
    assert "<@" not in text and "@everyone" not in text


async def test_a_tripped_valve_never_clears_on_its_own_and_alerts_every_start(v3_db, cfg, alerts):
    """A raid kicked the bot from 5 of 10 servers while it slept. Nothing ever cleans that up."""
    ids = list(range(BASE, BASE + 10))
    _seed(v3_db, *ids)
    life = GuildLifecycle(v3_db, cfg, _alert_into(alerts))
    survivors = ids[5:]
    for _ in range(3):
        plan = await life.reconcile(Client(survivors))
        assert plan.valve_tripped
    assert _ids(v3_db) == ids
    assert len(alerts) == 3  # nagging, at least; but the rows stay forever


async def test_a_tripped_valve_clears_once_enough_new_servers_join(v3_db, cfg, alerts):
    """The only automatic way out: the database grows until the stale share drops under 10%."""
    ids = list(range(BASE, BASE + 10))
    _seed(v3_db, *ids)
    life = GuildLifecycle(v3_db, cfg, _alert_into(alerts))
    survivors = ids[5:]
    assert (await life.reconcile(Client(survivors))).valve_tripped
    # 5 stale rows; need 5 <= max(3, 0.1 * N), so N >= 50 rows. Join 40 new servers.
    fresh = list(range(BASE + 100, BASE + 140))
    live = Client([*survivors, *fresh])
    # The first pass still sees a 10-row database (the new rows are created after the plan),
    # so it trips again; the clearing happens one start later.
    assert (await life.reconcile(live)).valve_tripped
    plan = await life.reconcile(live)
    assert not plan.valve_tripped
    assert _ids(v3_db) == sorted(survivors + fresh)


async def test_a_tripped_valve_clears_with_per_guild_remove_events_but_none_arrive(v3_db, life):
    """There's no owner override: the stale rows can only go via on_guild_remove, which Discord
    never re-sends for a kick that happened while the bot was down. (Owner may want a command.)"""
    ids = list(range(BASE, BASE + 10))
    _seed(v3_db, *ids)
    await life.reconcile(Client(ids[5:]))
    assert len(_ids(v3_db)) == 10
    for gid in ids[:5]:  # manual cleanup, one event at a time, is the only lever
        await life.on_guild_remove(make_guild(gid))
    assert _ids(v3_db) == ids[5:]


async def test_valve_trip_still_creates_rows_and_comps(v3_db, cfg, alerts):
    cfg = cfg.model_copy(update={"comped_guild_ids": [G1]})
    life = GuildLifecycle(v3_db, cfg, _alert_into(alerts))
    _seed(v3_db, *range(BASE + 10, BASE + 20))
    _seed(v3_db, G1)
    plan = await life.reconcile(Client([G1, G2]))
    assert plan.valve_tripped
    assert _tier(v3_db, G1) == "comped"
    assert G2 in _ids(v3_db)
    assert len(_ids(v3_db)) == 12


# --- 2. event ordering and races ---


async def test_remove_twice_and_remove_before_join_are_harmless(life, v3_db):
    await life.on_guild_remove(make_guild(G1))
    await life.on_guild_join(make_guild(G1))
    await life.on_guild_remove(make_guild(G1))
    await life.on_guild_remove(make_guild(G1))
    assert _ids(v3_db) == []


async def test_remove_then_join_in_the_wrong_order_leaves_a_row_for_a_guild_we_left(life, v3_db):
    """Events arriving out of order: the join is processed after the remove that followed it.
    The row is created (we can't know better); reconcile is the only thing that can fix it."""
    await life.on_guild_remove(make_guild(G1))
    await life.on_guild_join(make_guild(G1))
    assert _ids(v3_db) == [G1]
    await life.reconcile(Client([G2]))  # G1 is stale, G2 is new; 1 stale <= 3, so it's deleted
    assert _ids(v3_db) == [G2]


async def test_two_joins_at_once_say_hello_once(life, v3_db):
    async def yield_once():
        await asyncio.sleep(0.005)

    ch = Chan(1, hook=yield_once)
    guild = make_guild(channels=[ch])
    results = await asyncio.gather(life.on_guild_join(guild), life.on_guild_join(guild))
    assert sorted(results) == [False, True]
    assert len(ch.sent) == 1


async def test_a_join_event_during_reconcile_gets_one_hello_between_them(life, v3_db):
    """Reconcile is saying hello to G1 when the join event for G2 (also in to_create) lands."""
    g2_chan = Chan(20)
    g2 = make_guild(G2, channels=[g2_chan])

    async def join_g2_meanwhile():
        await life.on_guild_join(g2)

    g1_chan = Chan(10, hook=join_g2_meanwhile)
    g1 = make_guild(G1, channels=[g1_chan])
    await life.reconcile(Client([g1, g2]))
    assert len(g1_chan.sent) == 1
    assert len(g2_chan.sent) == 1
    assert _ids(v3_db) == [G1, G2]


async def test_two_reconciles_at_once_do_not_double_say_hello_or_crash(life, v3_db):
    async def yield_once():
        await asyncio.sleep(0.005)

    ch = Chan(1, hook=yield_once)
    client = Client([make_guild(G1, channels=[ch])])
    await asyncio.gather(life.reconcile(client), life.reconcile(client))
    assert len(ch.sent) == 1
    assert _ids(v3_db) == [G1]


async def test_two_reconciles_at_once_delete_a_stale_row_once(life, v3_db):
    _seed(v3_db, G1, G2)

    async def slow_fetch(gid):
        await asyncio.sleep(0.005)
        raise _http(404, discord.NotFound, "gone")

    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G3, imported_at=NOW)
    client = Client([G2], fetch=slow_fetch)
    await asyncio.gather(life.reconcile(client), life.reconcile(client))
    assert _ids(v3_db) == [G2]


@pytest.mark.xfail(
    strict=True,
    reason="reconcile applies its start-of-run snapshot after awaits: a guild removed while "
    "reconcile is saying hello to an earlier guild still gets a row created afterward, for a "
    "server the bot has left. It stays until the next start (and always if the valve trips).",
)
async def test_remove_event_mid_reconcile_does_not_resurrect_the_row(life, v3_db):
    g2 = make_guild(G2, channels=[Chan(20)])
    client = Client()

    async def g2_is_kicked_meanwhile():
        client.guilds = [g for g in client.guilds if g.id != G2]
        await life.on_guild_remove(g2)

    g1 = make_guild(G1, channels=[Chan(10, hook=g2_is_kicked_meanwhile)])
    client.guilds = [g1, g2]
    await life.reconcile(client)
    assert _ids(v3_db) == [G1]


@pytest.mark.xfail(
    strict=True,
    reason="reconcile deletes stale rows from its snapshot after awaiting Discord (the imported "
    "guild's fetch): a guild the bot rejoins during that await has its fresh row deleted, and "
    "the join event sent no hello because the row still existed. Bot is in, no row, until restart.",
)
async def test_rejoin_during_reconcile_keeps_the_row(life, v3_db):
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G1, imported_at=NOW)
    _seed(v3_db, G2, G3)
    client = Client([G3])

    async def fetch(gid):
        # While Discord is being asked about G1, G2 is re-invited. Its row still exists, so the
        # join event finds it and does nothing.
        rejoined = make_guild(G2)
        client.guilds.append(rejoined)
        await life.on_guild_join(rejoined)
        raise _http(404, discord.NotFound, "gone")

    client._fetch = fetch
    await life.reconcile(client)
    assert G2 in _ids(v3_db)


async def test_join_during_setup_makes_a_fingerprinted_save_go_stale_not_corrupt(life, v3_db):
    """Wizard opened on a server with no row (a missed join); the join event then creates one."""
    with closing(connect(v3_db)) as conn:
        fp = repo.setup_fingerprint(repo.get_guild(conn, G1), repo.list_guild_games(conn, G1))
    await life.on_guild_join(make_guild(G1))
    with closing(connect(v3_db)) as conn, pytest.raises(repo.StaleSetupError):
        repo.apply_guild_setup(
            conn,
            G1,
            digest_time="10:00",
            timezone="UTC",
            games=[("palworld", 10)],
            expected_fingerprint=fp,
        )
    assert _count(v3_db, "guild_games") == 0


async def test_removal_during_setup_makes_the_save_go_stale_and_not_resurrect(life, v3_db):
    await life.on_guild_join(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        fp = repo.setup_fingerprint(repo.get_guild(conn, G1), repo.list_guild_games(conn, G1))
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn, pytest.raises(repo.StaleSetupError):
        repo.apply_guild_setup(
            conn,
            G1,
            digest_time="10:00",
            timezone="UTC",
            games=[("palworld", 10)],
            expected_fingerprint=fp,
        )
    assert _ids(v3_db) == []


async def test_removal_then_fresh_rejoin_during_setup_lets_the_save_through(life, v3_db):
    """Pinned: remove and re-add between open and Save yields the same fingerprint (defaults),
    so the Save lands on the fresh row. Harmless: the admin is the one who's still there."""
    await life.on_guild_join(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        fp = repo.setup_fingerprint(repo.get_guild(conn, G1), repo.list_guild_games(conn, G1))
    await life.on_guild_remove(make_guild(G1))
    await life.on_guild_join(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        repo.apply_guild_setup(
            conn, G1, digest_time="09:00", timezone="UTC", games=[("rust", 11)],
            expected_fingerprint=fp,
        )  # fmt: skip
    assert _count(v3_db, "guild_games", "guild_id = ?", (G1,)) == 1


async def test_an_unfingerprinted_setup_save_after_removal_recreates_the_row_quietly(life, v3_db):
    """Pinned behavior: apply_guild_setup without a fingerprint treats a missing row as a missed
    join and creates it, hello-less. A Save that races a kick leaves a row for a departed server."""
    await life.on_guild_join(make_guild(G1))
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        repo.apply_guild_setup(conn, G1, digest_time="09:00", timezone="UTC", games=[("rust", 11)])
    assert _ids(v3_db) == [G1]


# --- remove mid-flight: digests, SHiFT, settings writes ---


def _seed_full(db, gid, run_date="2026-09-30", code=CODE):
    now = NOW.isoformat()
    with closing(connect(db)) as conn:
        repo.create_guild(conn, gid, set_up=True)
        repo.follow_game(conn, gid, "palworld", 10)
        repo.add_notice(conn, gid, "note")
        conn.execute(
            "INSERT INTO guild_shift (guild_id, enabled, channel_id) VALUES (?, 1, 5)", (gid,)
        )
        conn.execute(
            "INSERT INTO guild_lounge (guild_id, channel_id) VALUES (?, 7)", (gid,)
        )  # fmt: skip
        conn.execute(
            "INSERT OR IGNORE INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://x.test/a', 't', 'e', 's', 'press', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO alerted_codes (code, first_seen_at, source_name, item_url, "
            "status) VALUES (?, ?, 's', 'https://x.test/a', 'posted')",
            (code, now),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
            "VALUES (?, ?, 'queued', ?)",
            (gid, code, now),
        )
        cur = conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at, guild_id) "
            "VALUES (?, 'pending', ?, ?, ?)",
            (run_date, now, now, gid),
        )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES ('palworld', ?, 's', 'official', ?, ?)",
            (f"h{gid}", cur.lastrowid, now),
        )
        conn.commit()
        return cur.lastrowid


_GUILD_TABLES = (
    "guilds",
    "guild_games",
    "guild_shift",
    "guild_lounge",
    "guild_notices",
    "guild_code_posts",
)


def _snapshot(db, gid):
    with closing(connect(db)) as conn:
        out = {}
        for table in _GUILD_TABLES:
            out[table] = [
                tuple(r)
                for r in conn.execute(
                    f"SELECT * FROM {table} WHERE guild_id = ? ORDER BY 1, 2",  # noqa: S608
                    (gid,),
                )
            ]
        out["digests"] = [
            tuple(r)
            for r in conn.execute("SELECT * FROM digests WHERE guild_id = ? ORDER BY id", (gid,))
        ]
        out["stories"] = [
            tuple(r) for r in conn.execute("SELECT * FROM stories WHERE headline = ?", (f"h{gid}",))
        ]
        return out


async def test_deleting_a_guild_leaves_every_row_of_another_guild_byte_identical(life, v3_db):
    _seed_full(v3_db, G1)
    _seed_full(v3_db, G2)
    before = _snapshot(v3_db, G2)
    shared = [
        _count(v3_db, t) for t in ("items", "alerted_codes", "game_summaries", "stories")
    ]  # fmt: skip
    await life.on_guild_remove(make_guild(G1))
    assert _snapshot(v3_db, G2) == before
    after = [_count(v3_db, t) for t in ("items", "alerted_codes", "game_summaries", "stories")]
    assert after == shared  # every shared row, and both stories, survive
    assert _count(v3_db, "guilds", "guild_id = ?", (G1,)) == 0
    assert _count(v3_db, "stories", "digest_id IS NULL") == 1  # A's story orphaned on purpose
    assert _count(v3_db, "digests") == 1


async def test_reconcile_deleting_a_stale_guild_leaves_the_rest_identical(life, v3_db):
    _seed_full(v3_db, G1)
    _seed_full(v3_db, G2)
    before = _snapshot(v3_db, G2)
    await life.reconcile(Client([G2]))
    assert _ids(v3_db) == [G2]
    assert _snapshot(v3_db, G2) == before


async def test_remove_during_an_inflight_digest_finishing_writes_nothing_and_does_not_crash(
    life, v3_db
):
    digest_id = _seed_full(v3_db, G1)
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        # The digest task wakes up and records its outcome against a row that's gone.
        repo.save_guild_digest(conn, digest_id, "ok", {"palworld": 1}, None, (NOW, NOW))
        repo.mark_guild_digest_failed(conn, digest_id, "boom", {"palworld": 1})
    assert _count(v3_db, "digests") == 0
    assert _count(v3_db, "guilds") == 0


async def test_remove_during_an_inflight_digest_then_a_late_claim_is_refused_loudly(life, v3_db):
    _seed_full(v3_db, G1)
    await life.on_guild_remove(make_guild(G1))
    window = (NOW - timedelta(days=1), NOW)
    with closing(connect(v3_db)) as conn, pytest.raises(sqlite3.IntegrityError):
        repo.claim_guild_digest(conn, G1, date(2026, 10, 1), force=False, window=window)
    assert _count(v3_db, "digests") == 0


async def test_remove_during_shift_delivery_cannot_spend_a_ping_or_recreate_settings(life, v3_db):
    _seed_full(v3_db, G1)
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        with pytest.raises(repo.StoreError):
            repo.claim_guild_codes(
                conn, G1, [CODE],
                pinged=True, local_day="2026-09-30",
            )  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):
            repo.set_shift(conn, G1, enabled=True, channel_id=5, ping="none")
    assert _count(v3_db, "guild_shift") == 0
    assert _count(v3_db, "guild_code_posts") == 0


async def test_late_settings_writes_to_a_removed_guild_do_not_resurrect_it(life, v3_db):
    _seed_full(v3_db, G1)
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        assert repo.update_guild_settings(conn, G1, permission_problems="late") is False
        with pytest.raises(sqlite3.IntegrityError):
            repo.add_notice(conn, G1, "late notice")
        with pytest.raises(sqlite3.IntegrityError):
            repo.follow_game(conn, G1, "rust", 11)
    assert _ids(v3_db) == []
    assert _count(v3_db, "guild_notices") == 0


async def test_channel_delete_racing_a_removal_does_not_crash_or_write(life, v3_db, caplog):
    _seed_full(v3_db, G1)
    await life.on_guild_remove(make_guild(G1))
    await life.on_guild_channel_delete(Chan(10, guild=make_guild(G1)))
    assert _count(v3_db, "guild_notices") == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- 3. the imported guild ---


def _imported_db(db, gid=G1):
    with closing(connect(db)) as conn:
        repo.create_guild(conn, gid, tier="comped", set_up=True, imported_at=NOW)
    _seed(db, G2)


@pytest.mark.parametrize(
    ("error", "deleted"),
    [
        (_http(404, discord.NotFound, "Unknown Guild"), True),
        (_http(403, discord.Forbidden, "Missing Access"), True),
        (_http(500), False),
        (_http(429), False),
        (discord.DiscordServerError(SimpleNamespace(status=503, reason="x"), "down"), False),
        (discord.RateLimited(1.0), False),
        (discord.GatewayNotFound(), False),
        (discord.LoginFailure("bad token"), False),
        (TimeoutError(), False),
        (ConnectionResetError(), False),
        (RuntimeError("anything"), False),
        (KeyError("x"), False),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
async def test_fetch_guild_raising_anything_but_notfound_or_forbidden_keeps_the_imported_row(
    life, v3_db, error, deleted
):
    _imported_db(v3_db)

    async def fetch(gid):
        raise error

    await life.reconcile(Client([G2], fetch=fetch))
    assert (G1 in _ids(v3_db)) is (not deleted)
    assert G2 in _ids(v3_db)


async def test_fetch_guild_cancellation_is_not_swallowed(life, v3_db):
    _imported_db(v3_db)

    async def fetch(gid):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await life.reconcile(Client([G2], fetch=fetch))
    assert G1 in _ids(v3_db)


@pytest.mark.parametrize(
    "returned", [None, object(), SimpleNamespace(id=G3), SimpleNamespace(id=G1)]
)
async def test_fetch_guild_returning_anything_at_all_keeps_the_imported_row(life, v3_db, returned):
    """Pinned: any non-error answer, even a different guild or None, means "still there"."""
    _imported_db(v3_db)

    async def fetch(gid):
        return returned

    await life.reconcile(Client([G2], fetch=fetch))
    assert G1 in _ids(v3_db)


@pytest.mark.xfail(
    strict=True,
    reason="_confirmed_gone has no timeout: a fetch_guild that never answers hangs reconcile "
    "forever, so the other stale deletes, the missing-row creates and comping never run.",
)
async def test_a_hanging_fetch_guild_does_not_hang_reconcile(life, v3_db):
    _imported_db(v3_db)

    async def fetch(gid):
        await asyncio.Event().wait()

    await asyncio.wait_for(life.reconcile(Client([G2], fetch=fetch)), timeout=1.0)


async def test_imported_guild_fetch_is_asked_once_per_guild_not_per_stale_row(life, v3_db):
    _imported_db(v3_db)
    client = Client([G2])
    await life.reconcile(client)
    assert client.fetched == [G1]


async def test_imported_guild_removed_readded_then_reconciled_is_one_fresh_free_row(v22_db, alerts):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    assert ensure_imported(v22_db, cfg) is not None
    home = cfg.legacy.guild_id
    life = GuildLifecycle(str(v22_db), cfg, _alert_into(alerts))
    ch = Chan(1)
    guild = make_guild(home, channels=[ch])
    await life.on_guild_remove(guild)
    await life.on_guild_join(guild)
    await life.reconcile(Client([guild]))
    await life.reconcile(Client([guild]))
    assert _ids(v22_db) == [home]
    assert len(ch.sent) == 1
    with closing(connect(v22_db)) as conn:
        row = repo.get_guild(conn, home)
    assert row.imported_at is None and row.set_up is False


async def test_imported_guilds_settings_die_on_a_single_remove_event(v22_db, alerts):
    """Pinned: a real remove event skips the confirm step the reconcile path uses. One event and
    the friend's server's settings are gone (the import marker stays, so nothing restores them)."""
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    ensure_imported(v22_db, cfg)
    home = cfg.legacy.guild_id
    with closing(connect(v22_db)) as conn:
        assert repo.list_guild_games(conn, home)
    life = GuildLifecycle(str(v22_db), cfg, _alert_into(alerts))
    await life.on_guild_remove(make_guild(home))
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild(conn, home) is None
        assert repo.list_guild_games(conn, home) == []


async def test_imported_guild_present_in_the_cache_is_never_fetched_or_touched(life, v3_db):
    _imported_db(v3_db)
    client = Client([G1, G2])
    await life.reconcile(client)
    assert client.fetched == []
    assert _ids(v3_db) == [G1, G2]
    assert _tier(v3_db, G1) == "comped"


# --- 4. first contact ---


async def test_reconcile_never_says_hello_to_an_existing_row(life, v3_db):
    ch = Chan(1)
    _seed(v3_db, G1)
    await life.reconcile(Client([make_guild(G1, channels=[ch])]))
    assert ch.sent == []


async def test_repeated_reconciles_say_hello_once(life, v3_db):
    ch = Chan(1)
    client = Client([make_guild(G1, channels=[ch])])
    for _ in range(3):
        await life.reconcile(client)
    assert len(ch.sent) == 1


async def test_reconcile_hello_is_mention_free_and_exactly_the_text(life):
    ch = Chan(1)
    await life.reconcile(Client([make_guild(G1, channels=[ch])]))
    content, kwargs = ch.sent[0]
    assert content == FIRST_CONTACT_TEXT
    assert set(kwargs) == {"allowed_mentions"}
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_a_guild_the_bot_just_left_gets_no_hello_from_the_remove_event_path(life, v3_db):
    ch = Chan(1)
    guild = make_guild(G1, channels=[ch])
    await life.on_guild_join(guild)
    await life.on_guild_remove(guild)
    await life.reconcile(Client([]))  # the cache no longer has it
    assert len(ch.sent) == 1 and _ids(v3_db) == []


async def test_reconcile_not_ready_says_nothing_even_for_unknown_guilds(life, v3_db):
    ch = Chan(1)
    assert await life.reconcile(Client([make_guild(G1, channels=[ch])], ready=False)) is None
    assert ch.sent == [] and _ids(v3_db) == []


@pytest.mark.parametrize(
    "error",
    [
        _http(403, discord.Forbidden, "Missing Access"),
        _http(404, discord.NotFound, "Unknown Channel"),
        _http(500),
        _http(429),
        discord.DiscordServerError(SimpleNamespace(status=503, reason="x"), "down"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_http_failures_on_the_hello_never_escape_and_keep_the_row(life, v3_db, error):
    ch = Chan(1, error=error)
    assert await life.on_guild_join(make_guild(channels=[ch])) is True
    assert _ids(v3_db) == [G1]


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), ConnectionResetError(), RuntimeError("x"), OSError("x")],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.xfail(
    strict=True,
    reason="_say_hello only catches discord.HTTPException. A TimeoutError, connection reset or "
    "any other exception from send escapes on_guild_join, and through it aborts reconcile's "
    "create loop and skips comping for everything after it.",
)
async def test_non_http_failures_on_the_hello_do_not_abort_reconcile(life, v3_db, error):
    bad = Chan(1, error=error)
    good = Chan(2)
    await life.reconcile(Client([make_guild(G1, channels=[bad]), make_guild(G2, channels=[good])]))
    assert _ids(v3_db) == [G1, G2]
    assert len(good.sent) == 1


@pytest.mark.parametrize(
    "error", [TimeoutError(), RuntimeError("x")], ids=lambda e: type(e).__name__
)
async def test_non_http_hello_failure_keeps_the_row_even_when_it_escapes(life, v3_db, error):
    """Whatever send raises, the row was written first, so the server isn't lost."""
    with contextlib.suppress(Exception):
        await life.on_guild_join(make_guild(channels=[Chan(1, error=error)]))
    assert _ids(v3_db) == [G1]


@pytest.mark.xfail(
    strict=True,
    reason="no timeout around the hello's send: one hung send blocks reconcile forever (and "
    "every later create and the comping pass with it).",
)
async def test_a_hung_hello_does_not_hang_reconcile(life, v3_db):
    async def never():
        await asyncio.Event().wait()

    await asyncio.wait_for(
        life.reconcile(Client([make_guild(G1, channels=[Chan(1, hook=never)])])), timeout=1.0
    )


async def test_a_hung_hello_on_a_join_event_only_blocks_that_event_task(life, v3_db):
    async def never():
        await asyncio.Event().wait()

    task = asyncio.create_task(life.on_guild_join(make_guild(G1, channels=[Chan(1, hook=never)])))
    await asyncio.sleep(0.01)
    assert _ids(v3_db) == [G1]  # the row was written before the hang
    await life.on_guild_join(make_guild(G2))  # other events still process
    assert _ids(v3_db) == [G1, G2]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_system_channel_that_cannot_send_at_all_is_still_picked_if_permissions_say_yes(
    life, v3_db
):
    class NoSend:  # a voice or category masquerading as the system channel, permissions and all
        id = 5
        position = 0

        def permissions_for(self, member):
            return SEND

    ok = Chan(7, position=2)
    guild = make_guild(system=NoSend(), channels=[ok])
    # Pinned: the picker trusts permissions_for; a channel that can't `send` would be picked.
    chosen = lifecycle.pick_first_contact_channel(guild)
    assert chosen is not ok
    assert not hasattr(chosen, "send")


async def test_me_with_no_permissions_anywhere_is_silent_not_an_error(life, v3_db):
    nothing = discord.Permissions.none()
    guild = make_guild(system=Chan(1, perms=nothing), channels=[Chan(2, perms=nothing)])
    assert await life.on_guild_join(guild) is True
    assert _ids(v3_db) == [G1]


async def test_view_without_send_everywhere_is_silent(life):
    chans = [Chan(i, perms=NO_SEND) for i in range(1, 20)]
    await life.on_guild_join(make_guild(channels=chans))
    assert all(c.sent == [] for c in chans)


def test_thousands_of_channels_are_picked_quickly_and_deterministically():
    chans = [Chan(i, position=i % 7) for i in range(1, 5001)]
    guild = make_guild(channels=chans)
    start = time.perf_counter()
    first = lifecycle.pick_first_contact_channel(guild)
    assert time.perf_counter() - start < 1.0
    assert first.id == 7 or (first.position == 0 and first.id == min(
        c.id for c in chans if c.position == 0
    ))  # fmt: skip
    assert first.position == 0


def test_the_lowest_sendable_position_wins_even_when_all_the_early_ones_are_muted():
    muted = [Chan(i, position=0, perms=NO_SEND) for i in range(1, 2000)]
    ok = Chan(9999, position=5)
    assert lifecycle.pick_first_contact_channel(make_guild(channels=[*muted, ok])) is ok


def test_position_ties_with_identical_ids_do_not_crash_the_sort():
    a, b = Chan(5, position=1), Chan(5, position=1)
    assert lifecycle.pick_first_contact_channel(make_guild(channels=[a, b])) in (a, b)


def test_guild_without_me_attribute_at_all_is_handled():
    guild = SimpleNamespace(id=G1, system_channel=Chan(1), text_channels=[Chan(2)])
    assert lifecycle.pick_first_contact_channel(guild) is None


@pytest.mark.xfail(
    strict=True,
    reason="can_speak_in says it never raises, but only the permissions_for call is inside the "
    "try; reading view_channel off a None result raises AttributeError. Discord never returns "
    "None, so this is a docstring-versus-code nit.",
)
def test_permissions_for_returning_garbage_is_not_a_yes():
    class Weird(Chan):
        def permissions_for(self, member):
            return None

    assert lifecycle.pick_first_contact_channel(make_guild(channels=[Weird(1)])) is None


def test_first_contact_text_fits_a_message_and_has_no_markup_that_pings():
    assert len(FIRST_CONTACT_TEXT) <= 2000
    for bad in ("@", "<@", "<#", "<&", "@everyone", "@here", "http", "\x00"):
        assert bad not in FIRST_CONTACT_TEXT
    assert FIRST_CONTACT_TEXT.count("`") % 2 == 0


# --- 5. comping ---


async def test_a_listed_guild_that_never_joined_gets_no_row(v3_db, cfg, alerts):
    life = GuildLifecycle(
        v3_db, cfg.model_copy(update={"comped_guild_ids": [G1, G2]}), _alert_into(alerts)
    )
    await life.reconcile(Client([G3]))
    assert _ids(v3_db) == [G3]


async def test_a_listed_guild_removed_and_rejoined_is_comped_again(v3_db, cfg, alerts):
    life = GuildLifecycle(
        v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), _alert_into(alerts)
    )
    await life.on_guild_join(make_guild(G1))
    await life.on_guild_remove(make_guild(G1))
    await life.on_guild_join(make_guild(G1))
    assert _tier(v3_db, G1) == "comped"


async def test_a_delisted_guild_stays_comped_through_join_and_reconcile(v3_db, cfg, alerts):
    _seed(v3_db, G1, tier="comped")
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": []}), _noop_alert())
    await life.on_guild_join(make_guild(G1))
    await life.reconcile(Client([G1]))
    assert _tier(v3_db, G1) == "comped"


async def test_a_delisted_guild_that_is_removed_and_rejoined_comes_back_free(v3_db, cfg):
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), _noop_alert())
    await life.on_guild_join(make_guild(G1))
    delisted = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": []}), _noop_alert())
    await delisted.on_guild_remove(make_guild(G1))
    await delisted.on_guild_join(make_guild(G1))
    assert _tier(v3_db, G1) == "free"


async def test_duplicate_join_for_a_listed_free_row_does_not_upgrade_until_reconcile(v3_db, cfg):
    _seed(v3_db, G1)
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), _noop_alert())
    assert await life.on_guild_join(make_guild(G1)) is False
    assert _tier(v3_db, G1) == "free"
    await life.reconcile(Client([G1]))
    assert _tier(v3_db, G1) == "comped"


async def test_comping_is_idempotent_and_leaves_other_settings_alone(v3_db, cfg):
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G1, set_up=True, digest_time="07:30", timezone="Europe/Paris")
        repo.follow_game(conn, G1, "palworld", 10)
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), _noop_alert())
    await life.reconcile(Client([G1]))
    await life.reconcile(Client([G1]))
    with closing(connect(v3_db)) as conn:
        row = repo.get_guild(conn, G1)
        assert (row.tier, row.digest_time, row.timezone, row.set_up) == (
            "comped", "07:30", "Europe/Paris", True,
        )  # fmt: skip
        assert len(repo.list_guild_games(conn, G1)) == 1


def _noop_alert():
    async def alert(text):
        return None

    return alert


# --- reconcile robustness ---


@pytest.mark.xfail(
    strict=True,
    reason="reconcile's delete loop has no per-guild error handling: one failing delete raises "
    "out of reconcile, skipping the remaining deletes, every create and the comping pass.",
)
async def test_one_failing_delete_does_not_abort_the_rest_of_reconcile(life, v3_db, monkeypatch):
    _seed(v3_db, G1, G2, G3)
    real = repo.delete_guild

    def flaky(conn, guild_id):
        if guild_id == G1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, guild_id)

    monkeypatch.setattr(repo, "delete_guild", flaky)
    new = BASE + 50
    await life.reconcile(Client([new]))  # G1, G2, G3 stale: 3 <= 3, all may go
    assert new in _ids(v3_db)
    assert G2 not in _ids(v3_db)


async def test_a_join_row_write_failure_is_swallowed_and_no_hello_is_sent(
    life, v3_db, monkeypatch, caplog
):
    def boom(conn, guild_id, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "create_guild", boom)
    ch = Chan(1)
    assert await life.on_guild_join(make_guild(channels=[ch])) is False
    assert ch.sent == []


async def test_a_remove_delete_failure_is_swallowed(life, v3_db, monkeypatch):
    _seed(v3_db, G1)

    def boom(conn, guild_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "delete_guild", boom)
    await life.on_guild_remove(make_guild(G1))
    assert _ids(v3_db) == [G1]


async def test_reconcile_with_a_thousand_guilds_is_fast(v3_db, cfg, alerts):
    ids = [BASE + i for i in range(1000)]
    life = GuildLifecycle(v3_db, cfg, _alert_into(alerts))
    _seed(v3_db, *ids)
    start = time.perf_counter()
    plan = await life.reconcile(Client(ids))
    assert time.perf_counter() - start < 5.0
    assert plan == lifecycle.ReconcilePlan((), (), ())


async def test_reconcile_with_a_thousand_new_guilds_creates_them_all_once(life, v3_db):
    ids = [BASE + i for i in range(1000)]
    await life.reconcile(Client(ids))
    assert _ids(v3_db) == sorted(ids)


# --- channel deletes ---


def _game_guild(db, gid=G1, game_chan=10):
    with closing(connect(db)) as conn:
        repo.create_guild(conn, gid, set_up=True)
        repo.follow_game(conn, gid, "palworld", game_chan)


async def test_channel_delete_for_a_guild_with_no_row_does_nothing_and_logs_no_error(
    life, v3_db, caplog
):
    await life.on_guild_channel_delete(Chan(10, guild=make_guild(G1)))
    assert _count(v3_db, "guild_notices") == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_channel_delete_notice_goes_only_to_the_owning_guild_even_with_the_same_id(
    life, v3_db
):
    """Channel ids are globally unique in Discord, but if a test or a bug ever aliased one, the
    event's own guild decides who hears about it."""
    _game_guild(v3_db, G1, 10)
    _game_guild(v3_db, G2, 10)
    await life.on_guild_channel_delete(Chan(10, guild=make_guild(G1)))
    with closing(connect(v3_db)) as conn:
        assert len(repo.recent_notices(conn, G1)) == 1
        assert repo.recent_notices(conn, G2) == []


async def test_channel_delete_twice_adds_two_notices_and_is_capped_by_the_store(life, v3_db):
    _game_guild(v3_db)
    for _ in range(30):
        await life.on_guild_channel_delete(Chan(10, guild=make_guild(G1)))
    with closing(connect(v3_db)) as conn:
        assert len(repo.recent_notices(conn, G1)) == 20


async def test_channel_delete_ignores_a_shift_channel_with_shift_disabled(life, v3_db):
    _game_guild(v3_db)
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, G1, enabled=False, channel_id=77, ping="none")
    await life.on_guild_channel_delete(Chan(77, guild=make_guild(G1)))
    assert _count(v3_db, "guild_notices") == 0


async def test_channel_delete_notice_for_shift_and_lounge_channels_names_the_use(life, v3_db):
    _game_guild(v3_db)
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, G1, enabled=True, channel_id=77, ping="none")
    await life.on_guild_channel_delete(Chan(77, guild=make_guild(G1)))
    with closing(connect(v3_db)) as conn:
        notices = repo.recent_notices(conn, G1)
    assert len(notices) == 1 and "SHiFT" in notices[0].text
    assert "@" not in notices[0].text and "<#" not in notices[0].text


async def test_channel_delete_of_a_channel_object_without_guild_attribute_is_ignored(life, v3_db):
    _game_guild(v3_db)
    await life.on_guild_channel_delete(SimpleNamespace(id=10))
    assert _count(v3_db, "guild_notices") == 0


async def test_channel_delete_database_failure_is_logged_not_raised(life, v3_db, monkeypatch):
    _game_guild(v3_db)

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(repo, "add_notice", boom)
    await life.on_guild_channel_delete(Chan(10, guild=make_guild(G1)))


async def test_a_thread_or_voice_channel_delete_with_a_used_id_still_only_notifies_the_owner(
    life, v3_db
):
    _game_guild(v3_db)
    await life.on_guild_channel_delete(Chan(10, guild=make_guild(G2)))
    with closing(connect(v3_db)) as conn:
        assert repo.recent_notices(conn, G1) == []


# --- 7. logging: names stay out ---


async def test_no_log_record_at_any_level_carries_a_guild_or_channel_name(life, v3_db, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Top Secret Club 🔥"  # noqa: S105
    hush = Chan(1, error=_http(403, discord.Forbidden, "Missing Access"))
    hush.name = "secret-channel-name"
    boom = Chan(2, error=RuntimeError("kaboom"))
    boom.name = "secret-channel-name"
    g1 = make_guild(G1, channels=[hush], name=secret)
    g2 = make_guild(G2, channels=[Chan(3, perms=NO_SEND)], name=secret)
    g3 = make_guild(G3, channels=[boom], name=secret)
    _game_guild(v3_db, G1, 50)
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, BASE + 9, imported_at=NOW)

    async def fetch(gid):
        raise _http(500, text="unhelpful")

    await life.on_guild_join(g1)
    await life.on_guild_join(g2)
    try:
        await life.on_guild_join(g3)
    except RuntimeError:
        pass
    await life.on_guild_channel_delete(Chan(50, guild=g1))
    await life.reconcile(Client([g1, g2, g3], fetch=fetch))
    await life.on_guild_remove(g1)
    for record in caplog.records:
        rendered = record.getMessage() + (record.exc_text or "")
        rendered += " ".join(str(v) for v in vars(record).values() if isinstance(v, str))
        assert "Top Secret" not in rendered
        assert "secret-channel-name" not in rendered
    assert "Top Secret" not in caplog.text


async def test_log_records_identify_guilds_by_id(life, caplog):
    caplog.set_level(logging.INFO)
    await life.on_guild_join(make_guild(G1))
    await life.on_guild_remove(make_guild(G1))
    assert str(G1) in caplog.text


# --- v2 isolation ---


def test_lifecycle_module_never_builds_an_everyone_mention():
    import inspect

    source = inspect.getsource(lifecycle)
    assert "everyone=True" not in source
    assert lifecycle.allowed_mentions().everyone is False
    assert lifecycle.allowed_mentions().roles is False
    assert lifecycle.allowed_mentions().users is False
    assert lifecycle.allowed_mentions().replied_user is False
