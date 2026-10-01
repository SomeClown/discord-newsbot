"""Guild lifecycle (plan task 11): join, removal, reconciliation, channel deletes.

Hand-written fakes, like the permission tests: a guild here is whatever the
handlers actually touch (`id`, `me`, `system_channel`, `text_channels`).
"""

from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from v3_fakes import load_v3_config

from newsbot.bot.client import GuildLifecycle, NewsBot
from newsbot.config import load_config
from newsbot.guilds import lifecycle
from newsbot.guilds.importer import ensure_imported
from newsbot.guilds.lifecycle import (
    FIRST_CONTACT_TEXT,
    comp_candidates,
    pick_first_contact_channel,
    reconcile_plan,
)
from newsbot.store import repo
from newsbot.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
G1, G2, G3 = 9001, 9002, 9003
SEND = discord.Permissions(view_channel=True, send_messages=True)
NO_SEND = discord.Permissions(view_channel=True)
NO_VIEW = discord.Permissions(send_messages=True)


class FakeChannel:
    def __init__(self, channel_id, position=0, perms=SEND, guild=None, fail=False):
        self.id = channel_id
        self.position = position
        self._perms = perms
        self.guild = guild
        self.sent: list[tuple[str, dict]] = []
        self._fail = fail

    def permissions_for(self, member):
        return self._perms

    async def send(self, content=None, **kwargs):
        if self._fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Access")
        self.sent.append((content, kwargs))


def make_guild(guild_id=G1, system=None, channels=(), me="bot"):
    return SimpleNamespace(id=guild_id, me=me, system_channel=system, text_channels=list(channels))


class FakeClient:
    def __init__(self, guild_ids=(), ready=True, gone=None, errors=None):
        self.guilds = [make_guild(g) for g in guild_ids]
        self._ready = ready
        self._gone = set(gone or ())
        self._errors = errors or {}
        self.fetched: list[int] = []

    def is_ready(self):
        return self._ready

    def get_guild(self, guild_id):
        return next((g for g in self.guilds if g.id == guild_id), None)

    async def fetch_guild(self, guild_id):
        self.fetched.append(guild_id)
        if guild_id in self._errors:
            raise self._errors[guild_id]
        if guild_id in self._gone:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "Unknown Guild")
        return object()


@pytest.fixture(autouse=True)
def _no_valve_pause(monkeypatch):
    # The self-healing valve sleeps between Discord checks; tests don't need to wait.
    monkeypatch.setattr(lifecycle, "VALVE_CHECK_PAUSE", 0)


@pytest.fixture
def cfg(monkeypatch):
    return load_v3_config(monkeypatch)


@pytest.fixture
def alerts():
    return []


@pytest.fixture
def life(v3_db, cfg, alerts):
    async def alert(text):
        alerts.append(text)

    return GuildLifecycle(v3_db, cfg, alert)


def _guild_ids(db):
    with closing(connect(db)) as conn:
        return [g.guild_id for g in repo.list_guilds(conn)]


def _tier(db, guild_id):
    with closing(connect(db)) as conn:
        return repo.get_guild(conn, guild_id).tier


# --- first-contact channel choice (pure) ---


def test_system_channel_wins_when_it_can_speak():
    system = FakeChannel(5, position=9)
    other = FakeChannel(6, position=0)
    assert pick_first_contact_channel(make_guild(system=system, channels=[other, system])) is system


def test_no_system_channel_takes_the_first_by_position():
    late, early = FakeChannel(1, position=5), FakeChannel(2, position=1)
    assert pick_first_contact_channel(make_guild(channels=[late, early])) is early


def test_system_channel_without_send_falls_through_to_the_first_sendable():
    system = FakeChannel(5, perms=NO_SEND)
    muted = FakeChannel(6, position=0, perms=NO_SEND)
    ok = FakeChannel(7, position=2)
    assert pick_first_contact_channel(make_guild(system=system, channels=[muted, ok])) is ok


def test_system_channel_without_view_falls_through():
    system = FakeChannel(5, perms=NO_VIEW)
    ok = FakeChannel(7)
    assert pick_first_contact_channel(make_guild(system=system, channels=[ok])) is ok


def test_position_ties_break_by_id():
    a, b = FakeChannel(20, position=1), FakeChannel(10, position=1)
    assert pick_first_contact_channel(make_guild(channels=[a, b])) is b


def test_no_speakable_channel_at_all_is_none():
    guild = make_guild(
        system=FakeChannel(5, perms=NO_SEND), channels=[FakeChannel(6, perms=NO_VIEW)]
    )
    assert pick_first_contact_channel(guild) is None
    assert pick_first_contact_channel(make_guild()) is None


def test_unknown_self_means_no_channel():
    ok = FakeChannel(7)
    assert pick_first_contact_channel(make_guild(system=ok, channels=[ok], me=None)) is None


def test_a_channel_whose_permission_lookup_blows_up_is_skipped():
    class Bad(FakeChannel):
        def permissions_for(self, member):
            raise RuntimeError("cache miss")

    ok = FakeChannel(7, position=3)
    assert pick_first_contact_channel(make_guild(channels=[Bad(1, position=0), ok])) is ok


# --- the text ---


def test_first_contact_text_is_within_limits_and_mention_free():
    assert len(FIRST_CONTACT_TEXT) < 2000
    for bad in ("@", "<@", "<#", "<&", "--", "—", "–"):
        assert bad not in FIRST_CONTACT_TEXT
    assert "!" not in FIRST_CONTACT_TEXT
    assert "/newsbot setup" in FIRST_CONTACT_TEXT
    assert "Manage Server" in FIRST_CONTACT_TEXT
    assert "SHiFT" in FIRST_CONTACT_TEXT
    assert lifecycle.allowed_mentions().to_dict() == discord.AllowedMentions.none().to_dict()


# --- join ---


async def test_join_creates_a_free_unconfigured_row_and_one_message(life, v3_db):
    system = FakeChannel(5)
    assert await life.on_guild_join(make_guild(G1, system=system, channels=[system])) is True
    with closing(connect(v3_db)) as conn:
        row = repo.get_guild(conn, G1)
    assert (row.tier, row.set_up) == ("free", False)
    assert len(system.sent) == 1
    content, kwargs = system.sent[0]
    assert content == FIRST_CONTACT_TEXT
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_join_without_system_channel_uses_the_first_sendable(life):
    first, second = FakeChannel(1, position=0, perms=NO_SEND), FakeChannel(2, position=1)
    third = FakeChannel(3, position=2)
    await life.on_guild_join(make_guild(channels=[third, second, first]))
    assert [len(c.sent) for c in (first, second, third)] == [0, 1, 0]


async def test_join_with_no_speakable_channel_writes_the_row_and_logs(life, v3_db, caplog):
    caplog.set_level(logging.INFO)
    assert await life.on_guild_join(make_guild(channels=[FakeChannel(1, perms=NO_SEND)])) is True
    assert _guild_ids(v3_db) == [G1]
    assert "nowhere the bot may speak" in caplog.text


async def test_rejoin_event_with_the_row_present_sends_nothing(life):
    ch = FakeChannel(1)
    guild = make_guild(channels=[ch])
    await life.on_guild_join(guild)
    assert await life.on_guild_join(guild) is False
    assert len(ch.sent) == 1


async def test_a_failed_send_does_not_raise_and_keeps_the_row(life, v3_db):
    ch = FakeChannel(1, fail=True)
    assert await life.on_guild_join(make_guild(channels=[ch])) is True
    assert _guild_ids(v3_db) == [G1]


async def test_join_comps_a_listed_guild(v3_db, cfg, alerts):
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G2]}), _noop)
    await life.on_guild_join(make_guild(G1))
    await life.on_guild_join(make_guild(G2))
    assert (_tier(v3_db, G1), _tier(v3_db, G2)) == ("free", "comped")


async def _noop(text):
    return None


# --- removal ---


async def test_remove_deletes_the_guild_and_only_the_guild(life, v3_db):
    with closing(connect(v3_db)) as conn:
        for gid, chan in ((G1, 10), (G2, 20)):
            repo.create_guild(conn, gid, set_up=True)
            repo.follow_game(conn, gid, "palworld", chan)
            repo.add_notice(conn, gid, "hello")
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        assert repo.get_guild(conn, G1) is None
        assert repo.list_guild_games(conn, G1) == []
        assert repo.recent_notices(conn, G1) == []
        assert [g.channel_id for g in repo.list_guild_games(conn, G2)] == [20]
        assert len(repo.recent_notices(conn, G2)) == 1


async def test_remove_cascades_shift_lounge_and_keeps_shared_data(life, v3_db):
    from datetime import UTC, datetime

    from newsbot.store.models import LoungeSettings

    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G1)
        conn.execute(
            "INSERT INTO guild_shift (guild_id, enabled, channel_id) VALUES (?, 1, 5)", (G1,)
        )
        repo.upsert_lounge(conn, LoungeSettings(G1, 7, True, "hi", False, "08:00", [], None))
        now = datetime.now(UTC).isoformat()
        code = "A" * 5 + "-" + "B" * 5 + "-" + "C" * 5 + "-" + "D" * 5 + "-" + "E" * 5
        conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://x.test/a', 't', 'e', 's', 'press', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES (?, ?, 's', 'https://x.test/a', 'posted')",
            (code, now),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
            "VALUES (?, ?, 'posted', ?)",
            (G1, code, now),
        )
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at, guild_id) "
            "VALUES ('2026-09-30', 'ok', ?, ?, ?)",
            (now, now, G1),
        )
        conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "created_at) VALUES ('palworld', '2026-09-30', 'ok', ?, ?, ?)",
            (now, now, now),
        )
        conn.commit()
    await life.on_guild_remove(make_guild(G1))
    with closing(connect(v3_db)) as conn:
        for table in ("guilds", "guild_shift", "guild_lounge", "guild_code_posts", "digests"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 1


async def test_remove_of_an_unknown_guild_is_harmless(life, v3_db):
    await life.on_guild_remove(make_guild(G3))
    assert _guild_ids(v3_db) == []


async def test_remove_logs_ids_not_names(life, caplog):
    caplog.set_level(logging.INFO)
    guild = make_guild(G1)
    guild.name = "Secret Club Name"
    await life.on_guild_remove(guild)
    assert "Secret Club Name" not in caplog.text
    assert str(G1) in caplog.text


async def test_join_then_immediate_removal_leaves_nothing(life, v3_db):
    ch = FakeChannel(1)
    guild = make_guild(channels=[ch])
    await life.on_guild_join(guild)
    await life.on_guild_remove(guild)
    assert _guild_ids(v3_db) == []


async def test_removal_then_rejoin_starts_fresh_and_says_hello_again(life, v3_db):
    ch = FakeChannel(1)
    guild = make_guild(channels=[ch])
    await life.on_guild_join(guild)
    with closing(connect(v3_db)) as conn:
        repo.follow_game(conn, G1, "palworld", 10)
        repo.update_guild_settings(conn, G1, set_up=True)
    await life.on_guild_remove(guild)
    await life.on_guild_join(guild)
    with closing(connect(v3_db)) as conn:
        assert repo.get_guild(conn, G1).set_up is False
        assert repo.list_guild_games(conn, G1) == []
    assert len(ch.sent) == 2


# --- reconcile_plan (pure) ---


def test_plan_stale_and_missing():
    plan = reconcile_plan([1, 2, 3], [2, 3, 4])
    assert (plan.to_delete, plan.to_create, plan.valve_tripped) == ((1,), (4,), False)


def test_plan_nothing_to_do():
    plan = reconcile_plan([1, 2], [1, 2])
    assert plan == lifecycle.ReconcilePlan((), (), ())


def test_plan_empty_live_list_trips_even_a_tiny_database():
    plan = reconcile_plan([1, 2], [])
    assert plan.valve_tripped and plan.to_delete == ()
    assert "outage" in plan.valve_reason


def test_plan_empty_live_and_empty_db_is_quiet():
    assert not reconcile_plan([], []).valve_tripped


def test_plan_ten_percent_passes_and_eleven_trips():
    db = list(range(100))
    ok = reconcile_plan(db, db[10:])
    assert not ok.valve_tripped and len(ok.to_delete) == 10
    tripped = reconcile_plan(db, db[11:])
    assert tripped.valve_tripped and tripped.to_delete == ()


def test_plan_three_stale_rows_always_pass_in_a_bigger_db():
    plan = reconcile_plan(range(10), range(3, 10))
    assert not plan.valve_tripped and plan.to_delete == (0, 1, 2)


def test_plan_four_of_ten_trips():
    plan = reconcile_plan(range(10), range(4, 10))
    assert plan.valve_tripped


def test_plan_tripped_valve_still_creates_missing_rows():
    plan = reconcile_plan([1, 2, 3, 4, 5], [9])
    assert plan.valve_tripped and plan.to_create == (9,)


def test_plan_protected_ids_go_to_confirm_not_delete():
    plan = reconcile_plan([1, 2, 3], [3], protected_ids=[1])
    assert (plan.to_delete, plan.to_confirm) == ((2,), (1,))


def test_comp_candidates_only_upgrade_free_rows_that_exist():
    tiers = {1: "free", 2: "comped", 3: "free"}
    assert comp_candidates([1, 2, 99], tiers) == [1]
    assert comp_candidates([], tiers) == []


# --- reconcile (I/O) ---


def _seed(db, *ids, **kw):
    with closing(connect(db)) as conn:
        for gid in ids:
            repo.create_guild(conn, gid, **kw)


async def test_reconcile_deletes_a_guild_removed_while_offline(life, v3_db):
    _seed(v3_db, G1, G2)
    await life.reconcile(FakeClient([G2]))
    assert _guild_ids(v3_db) == [G2]


async def test_reconcile_creates_a_missing_row_and_says_hello(life, v3_db):
    ch = FakeChannel(1)
    client = FakeClient()
    client.guilds = [make_guild(G1, channels=[ch])]
    plan = await life.reconcile(client)
    assert plan.to_create == (G1,)
    assert _tier(v3_db, G1) == "free"
    assert len(ch.sent) == 1


async def test_reconcile_with_an_empty_cache_deletes_nothing_and_alerts(life, v3_db, alerts):
    _seed(v3_db, G1, G2)
    plan = await life.reconcile(FakeClient([]))
    assert plan.valve_tripped
    assert _guild_ids(v3_db) == [G1, G2]
    assert len(alerts) == 1 and "skipped" in alerts[0]
    assert str(G1) not in alerts[0]


async def test_reconcile_with_a_partial_cache_over_the_limit_deletes_nothing(v3_db, cfg, alerts):
    ids = list(range(1000, 1030))
    _seed(v3_db, *ids)
    life = GuildLifecycle(v3_db, cfg, _collect(alerts))
    await life.reconcile(FakeClient(ids[:20]))
    assert _guild_ids(v3_db) == ids
    assert len(alerts) == 1


def _collect(alerts):
    async def alert(text):
        alerts.append(text)

    return alert


async def test_reconcile_before_ready_does_nothing(life, v3_db):
    _seed(v3_db, G1)
    assert await life.reconcile(FakeClient([], ready=False)) is None
    assert _guild_ids(v3_db) == [G1]


async def test_reconcile_counts_unavailable_guilds_as_present(life, v3_db):
    _seed(v3_db, G1, G2)
    client = FakeClient([G1, G2])
    client.guilds[0].unavailable = True
    await life.reconcile(client)
    assert _guild_ids(v3_db) == [G1, G2]


async def test_reconcile_comps_listed_guilds_and_never_downgrades(v3_db, cfg):
    _seed(v3_db, G1, G2)
    _seed(v3_db, G3, tier="comped")
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1, 777]}), _noop)
    await life.reconcile(FakeClient([G1, G2, G3]))
    assert [_tier(v3_db, g) for g in (G1, G2, G3)] == ["comped", "free", "comped"]
    assert _guild_ids(v3_db) == [G1, G2, G3]


async def test_reconcile_comps_a_guild_it_creates(v3_db, cfg):
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), _noop)
    await life.reconcile(FakeClient([G1]))
    assert _tier(v3_db, G1) == "comped"


async def test_reconcile_asks_discord_before_dropping_the_imported_guild(v3_db, life):
    _seed(v3_db, G2)
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G1, tier="comped", set_up=True, imported_at=_now())
    client = FakeClient(
        [G2], errors={G1: discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")}
    )
    await life.reconcile(client)
    assert client.fetched == [G1]
    assert _guild_ids(v3_db) == [G1, G2]


async def test_reconcile_drops_the_imported_guild_when_discord_says_gone(v3_db, life):
    _seed(v3_db, G2)
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G1, tier="comped", set_up=True, imported_at=_now())
    await life.reconcile(FakeClient([G2], gone=[G1]))
    assert _guild_ids(v3_db) == [G2]


async def test_reconcile_with_empty_cache_asks_about_the_imported_guild_and_keeps_it_unless_gone(
    v3_db, life
):
    # Changed: with the self-healing valve an empty cache no longer skips the per-row check. The
    # imported row still goes only on an explicit NotFound or Forbidden, same rule as before.
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G1, tier="comped", set_up=True, imported_at=_now())
    client = FakeClient(
        [], errors={G1: discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")}
    )
    await life.reconcile(client)
    assert client.fetched == [G1]
    assert _guild_ids(v3_db) == [G1]
    await life.reconcile(FakeClient([], gone=[G1]))
    assert _guild_ids(v3_db) == []


def _now():
    from datetime import UTC, datetime

    return datetime(2026, 9, 30, tzinfo=UTC)


# --- the imported guild and the once-ever import ---


async def test_removing_the_imported_guild_does_not_reimport_or_crash(v22_db, alerts):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    assert ensure_imported(v22_db, cfg) is not None
    imported = cfg.legacy.guild_id
    life = GuildLifecycle(str(v22_db), cfg, _collect(alerts))
    await life.on_guild_remove(make_guild(imported))
    assert _guild_ids(v22_db) == []
    # The next start: import runs again and finds the marker.
    assert ensure_imported(v22_db, cfg) is None
    assert _guild_ids(v22_db) == []
    # And reconciliation with the bot back in the guild treats it as a plain new join.
    client = FakeClient([imported])
    await life.reconcile(client)
    assert _tier(v22_db, imported) == "free"


# --- channel deletes ---


def _setup_guild(db):
    with closing(connect(db)) as conn:
        repo.create_guild(conn, G1, set_up=True, admin_channel_id=30)
        repo.follow_game(conn, G1, "palworld", 10)
        repo.follow_game(conn, G1, "rust", 11)


async def test_channel_delete_adds_one_notice_naming_the_use(life, v3_db):
    _setup_guild(v3_db)
    await life.on_guild_channel_delete(FakeChannel(10, guild=make_guild(G1)))
    with closing(connect(v3_db)) as conn:
        notices = repo.recent_notices(conn, G1)
        assert len(notices) == 1
        assert "Palworld" in notices[0].text
        assert [g.channel_id for g in repo.list_guild_games(conn, G1)] == [10, 11]


async def test_channel_delete_for_a_shared_channel_is_still_one_notice(life, v3_db):
    _setup_guild(v3_db)
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, G1, admin_channel_id=10)
    await life.on_guild_channel_delete(FakeChannel(10, guild=make_guild(G1)))
    with closing(connect(v3_db)) as conn:
        notices = repo.recent_notices(conn, G1)
    assert len(notices) == 1 and "admin" in notices[0].text and "Palworld" in notices[0].text


async def test_channel_delete_nobody_uses_adds_nothing(life, v3_db):
    _setup_guild(v3_db)
    await life.on_guild_channel_delete(FakeChannel(999, guild=make_guild(G1)))
    await life.on_guild_channel_delete(FakeChannel(10, guild=make_guild(G3)))
    await life.on_guild_channel_delete(FakeChannel(10, guild=None))
    with closing(connect(v3_db)) as conn:
        assert repo.recent_notices(conn, G1) == []


async def test_channel_delete_in_another_guild_does_not_touch_this_one(life, v3_db):
    _setup_guild(v3_db)
    with closing(connect(v3_db)) as conn:
        repo.create_guild(conn, G2, set_up=True)
        repo.follow_game(conn, G2, "palworld", 10)
    await life.on_guild_channel_delete(FakeChannel(10, guild=make_guild(G2)))
    with closing(connect(v3_db)) as conn:
        assert repo.recent_notices(conn, G1) == []
        assert len(repo.recent_notices(conn, G2)) == 1


# --- wired into the client, through the lifecycle class ---


def test_the_client_forwards_the_guild_events_and_the_lifecycle_class_has_the_work():
    # v2 kept these off `NewsBot` so the v2 bot wouldn't pick the events up by name. At the
    # cutover the client has the three handlers and each one forwards to `GuildLifecycle`,
    # which is where the behavior (and the tests above) live.
    for name in ("on_guild_join", "on_guild_remove", "on_guild_channel_delete"):
        assert hasattr(NewsBot, name)
        assert hasattr(GuildLifecycle, name)
