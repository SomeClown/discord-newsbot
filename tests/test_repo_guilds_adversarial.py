"""Adversarial tests for the guild repo functions, beyond test_repo_guilds.py.

Angle (test-engineer brief, 2026-09-30): every one of these functions takes a
`guild_id`, and the whole pitch of the public app is that one server can't
see, touch, or pay for another's rows. So the first half is a sweep: two
fully populated guilds, every function aimed at one, and a fingerprint of the
other before and after. A second test makes the sweep notice new functions
(if someone adds one that takes a `guild_id` and forgets to add it here, that
test fails instead of the sweep quietly not covering it). The rest is the
usual trouble: the CHECK constraints poked with awkward values, the 10-game
cap under racing connections, FTS syntax thrown at `search_items`, and ids at
the ends of the integer line.

Things that are odd but consistent are pinned and labelled "documented".
The three strict xfails that used to live here were real (small) bugs; they're fixed
and are plain tests now.
"""

from __future__ import annotations

import inspect
import itertools
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings, StoredItem

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
G1, G2 = 1001, 2002
_SERIAL = itertools.count()


def _clock(moment=T0):
    return lambda: moment


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "n.db"
    with closing(connect(path)) as setup:
        migrate(setup)
    return path


@pytest.fixture
def conn(db_path):
    with closing(connect(db_path)) as c:
        yield c


def _store(conn, url, title, topics, collected_at=T0, excerpt="body"):
    repo.save_run(
        conn,
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES (?, 'ok', 'n', 'n')",
            (f"{url}#{next(_SERIAL)}",),
        ).lastrowid,
        [
            StoredItem(
                url=url,
                title=title,
                excerpt=excerpt,
                source_name="Feed",
                trust="press",
                published_at=None,
                topics=topics,
            )
        ],
        [],
        "ok",
        [],
        None,
        repo.Usage(0, 0),
        now=_clock(collected_at),
    )


def _lounge(guild_id, **over):
    fields = {
        "guild_id": guild_id,
        "channel_id": 50 + guild_id % 7,
        "welcome_enabled": True,
        "welcome_message": f"Welcome to {guild_id}",
        "quote_enabled": True,
        "quote_time": "08:00",
        "quote_sources": [{"kind": "wikiquote", "value": "Oscar Wilde"}],
        "last_quote_date": "2026-09-29",
    }
    fields.update(over)
    return LoungeSettings(**fields)


@pytest.fixture
def world(conn):
    """Two guilds with a row in every per-guild table, plus shared items and summaries."""
    with conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status, "
            "from_roundup) VALUES ('AAAAA-AAAAA-AAAAA-AAAAA-AAAA1', 'n', 'Feed', 'https://e/c', "
            "'posted', 0)"
        )
        conn.execute(  # released but claimed by no guild yet, for claim_guild_codes to take
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status, "
            "from_roundup) VALUES ('AAAAA-AAAAA-AAAAA-AAAAA-AAAA2', 'n', 'Feed', 'https://e/d', "
            "'posted', 0)"
        )
        conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "created_at) VALUES ('bl4', '2026-09-30', 'ok', 'a', 'b', 'c')"
        )
    for gid, games in ((G1, ["bl4", "palworld"]), (G2, ["bl4", "fortnite"])):
        repo.create_guild(conn, gid, tier="comped", set_up=True, now=_clock())
        repo.set_guild_games(conn, gid, [(g, 70 + i) for i, g in enumerate(games)])
        repo.set_shift(
            conn,
            gid,
            enabled=True,
            channel_id=80,
            ping="everyone",
            now=_clock(T0 - timedelta(days=1)),
        )
        repo.upsert_lounge(conn, _lounge(gid))
        repo.add_notice(conn, gid, f"notice for {gid}", now=_clock())
        with conn:
            digest_id = conn.execute(
                "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
                "VALUES (?, '2026-09-30', 'ok', 'n', 'n')",
                (gid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
                "VALUES ('bl4', ?, 's', 'official', ?, 'n')",
                (f"story of {gid}", digest_id),
            )
            conn.execute(
                "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
                "VALUES (?, 'AAAAA-AAAAA-AAAAA-AAAAA-AAAA1', 'posted', 'n')",
                (gid,),
            )
            conn.execute(  # claim_guild_codes moves queued rows, so each guild has one queued
                "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
                "VALUES (?, 'AAAAA-AAAAA-AAAAA-AAAAA-AAAA2', 'queued', ?)",
                (gid, "2026-09-30T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at, guild_id) "
                "VALUES (?, ?, 'n', ?)",
                (f"wikiquote:{gid}", "a" * 64, gid),
            )
    _store(conn, "https://e/bl4", "Borderlands patch zebracorn", {"bl4": False})
    _store(conn, "https://e/pal", "Palworld raid zebracorn", {"palworld": False})
    _store(conn, "https://e/fort", "Fortnite crossover zebracorn", {"fortnite": False})
    _store(conn, "https://e/none", "Unfollowed game zebracorn", {"starfield": False})
    return conn


_PER_GUILD_TABLES = [
    "guilds",
    "guild_games",
    "guild_shift",
    "guild_code_posts",
    "guild_lounge",
    "guild_notices",
    "digests",
    "lounge_quotes_used",
]


def _fingerprint(conn, gid):
    """Every row that belongs to `gid`, including its stories, as plain tuples."""
    out = {}
    for table in _PER_GUILD_TABLES:
        rows = conn.execute(f"SELECT * FROM {table} WHERE guild_id = ? ORDER BY 1, 2", (gid,))  # noqa: S608
        out[table] = [tuple(r) for r in rows]
    out["stories"] = [
        tuple(r)
        for r in conn.execute(
            "SELECT * FROM stories WHERE digest_id IN (SELECT id FROM digests WHERE guild_id = ?)",
            (gid,),
        )
    ]
    return out


def _shared(conn):
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("items", "item_topics", "alerted_codes", "game_summaries")
    }


# --- cross-guild bleed: every function, aimed at one guild, leaves the other alone ---

_MUTATIONS = {
    "create_guild": lambda c, g: repo.create_guild(c, g, timezone="Asia/Tokyo", tier="free"),
    "update_guild_settings": lambda c, g: repo.update_guild_settings(
        c,
        g,
        digest_time="23:45",
        timezone="X/Y",
        admin_channel_id=None,
        tier="free",
        set_up=False,
        permission_problems="nope",
    ),
    "set_guild_games": lambda c, g: repo.set_guild_games(c, g, [("z1", 9), ("z2", 9)]),
    "apply_guild_setup": lambda c, g: repo.apply_guild_setup(
        c, g, digest_time="23:45", timezone="Asia/Tokyo", games=[("z1", 9), ("bl4", 8)]
    ),
    "follow_game": lambda c, g: (
        repo.follow_game(c, g, "bl4", 99),
        repo.follow_game(c, g, "new", 99),
    ),
    "unfollow_game": lambda c, g: repo.unfollow_game(c, g, "bl4"),
    "set_shift": lambda c, g: repo.set_shift(
        c, g, enabled=False, channel_id=None, ping="123456789012345678"
    ),
    "upsert_lounge": lambda c, g: repo.upsert_lounge(
        c, _lounge(g, welcome_message="changed", last_quote_date="1999-01-01")
    ),
    "add_notice": lambda c, g: [repo.add_notice(c, g, f"n{i}") for i in range(25)],
    "adopt_orphan_digests": lambda c, g: repo.adopt_orphan_digests(c, g),
    "delete_guild": lambda c, g: repo.delete_guild(c, g),
    "claim_guild_codes": lambda c, g: repo.claim_guild_codes(
        c, g, ["AAAAA-AAAAA-AAAAA-AAAAA-AAAA2"], pinged=True, local_day="2026-09-30", max_pings=3
    ),
    "mark_guild_codes_posted": lambda c, g: repo.mark_guild_codes_posted(
        c, g, ["AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"], message_id=7
    ),
    "mark_guild_codes_failed": lambda c, g: repo.mark_guild_codes_failed(
        c, g, ["AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"]
    ),
    "skip_queued_guild_codes": lambda c, g: repo.skip_queued_guild_codes(
        c, g, ["AAAAA-AAAAA-AAAAA-AAAAA-AAAA2"]
    ),
    "fail_queued_guild_codes": lambda c, g: repo.fail_queued_guild_codes(c, g),
    "claim_guild_digest": lambda c, g: [
        repo.claim_guild_digest(
            c, g, date(2026, 9, 30), force=True, window=(SINCE, SINCE + timedelta(days=1))
        ),
        repo.claim_guild_digest(
            c, g, date(2026, 10, 1), force=False, window=(SINCE, SINCE + timedelta(days=1))
        ),
    ],
}

_READS = {
    "get_guild": lambda c, g: [repo.get_guild(c, g).guild_id],
    "list_guild_games": lambda c, g: [x.guild_id for x in repo.list_guild_games(c, g)],
    "get_shift": lambda c, g: [repo.get_shift(c, g).guild_id],
    "get_lounge": lambda c, g: [repo.get_lounge(c, g).guild_id],
    "current_shift_delivery": lambda c, g: [repo.current_shift_delivery(c, g, [])[0].guild_id],
    "recent_notices": lambda c, g: [x.guild_id for x in repo.recent_notices(c, g)],
}

# Functions the sweep covers by a different shape (they don't take `guild_id` directly,
# or their result isn't a guild id to compare).
#
# The three import backfills are here too: they only ever run inside the one-time
# import, on an empty `guilds` table, so "another guild's rows" can't exist yet
# (tests/test_import_v2.py covers them, including refusing to run beside a guild row).
_OTHER_COVERAGE = {
    "query_items",
    "search_items",
    "upsert_lounge",
    "adopt_orphan_digests_in_tx",
    "backfill_guild_code_posts",
    "backfill_lounge_quotes_guild",
    "guild_posted_codes",  # see the test just below
    "queued_guild_codes",  # likewise, and it returns codes rather than guild ids
    # The per-guild digest reads: tests/test_repo_guild_digest.py pins that each
    # only sees its own guild's rows (items_for_window through guild_games,
    # last_window_end and get_guild_digest through the digests guild_id).
    "get_guild_digest",
    "latest_guild_digest",  # tests/test_repo_scoped_reads.py
    "last_window_end",
    "items_for_window",
    # The lounge deck and date guard: tests/test_repo_lounge_guilds.py (task 12).
    "claim_quote",
    "quote_deck_state",
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
@pytest.mark.parametrize(("mine", "theirs"), [(G1, G2), (G2, G1)])
def test_a_guild_mutation_never_changes_the_other_guilds_rows(world, name, mine, theirs):
    with world:
        world.execute(  # an orphan for adopt_orphan_digests to chew on, and the import marker
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES ('2026-08-01', 'ok', 'n', 'n')"
        )
        world.execute("UPDATE guilds SET imported_at = 'x' WHERE guild_id = ?", (mine,))
    before = _fingerprint(world, theirs)
    shared_before = _shared(world)
    _MUTATIONS[name](world, mine)
    assert _fingerprint(world, theirs) == before
    if name != "delete_guild":
        assert _shared(world) == shared_before
    assert world.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("name", sorted(_READS))
@pytest.mark.parametrize("gid", [G1, G2])
def test_a_guild_read_only_ever_returns_that_guilds_rows(world, name, gid):
    assert set(_READS[name](world, gid)) == {gid}


def test_queued_guild_codes_only_reports_that_guilds_queue(world):
    with world:
        world.execute(
            "DELETE FROM guild_code_posts WHERE guild_id = ? AND status = 'queued'", (G2,)
        )
    assert [q.code for q in repo.queued_guild_codes(world, G1)] == ["AAAAA-AAAAA-AAAAA-AAAAA-AAAA2"]
    assert repo.queued_guild_codes(world, G2) == []


def test_guild_posted_codes_only_reports_that_guilds_posts(world):
    code = "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"
    with world:
        world.execute("DELETE FROM guild_code_posts WHERE guild_id = ?", (G2,))
    assert repo.guild_posted_codes(world, G1, [code]) == {code}
    assert repo.guild_posted_codes(world, G2, [code]) == set()


def test_the_sweep_knows_about_every_repo_function_that_takes_a_guild_id():
    covered = set(_MUTATIONS) | set(_READS) | _OTHER_COVERAGE
    takes_guild = {
        name
        for name, fn in inspect.getmembers(repo, inspect.isfunction)
        if fn.__module__ == repo.__name__
        and not name.startswith("_")
        and "guild_id" in inspect.signature(fn).parameters
    }
    assert {"follow_game", "search_items", "delete_guild"} <= takes_guild  # the scan sees things
    assert takes_guild <= covered, f"not swept: {sorted(takes_guild - covered)}"


def test_item_reads_for_one_guild_never_show_the_others_games(world):
    g1_items, total = repo.query_items(world, G1, [], SINCE, 50, 0)
    assert total == 2 and {i.url for i in g1_items} == {"https://e/bl4", "https://e/pal"}
    for item in g1_items:
        assert set(item.topic_keys) <= {"bl4", "palworld"}
    # Asking for the other guild's game by name (and other hostile keys) gets nothing extra.
    for topics in (["fortnite"], ["fortnite", "starfield"], ["' OR 1=1 /*"], ["%"], [""]):
        assert repo.query_items(world, G1, topics, SINCE, 50, 0) == ([], 0)
    hits = repo.search_items(world, G1, "zebracorn", SINCE, 50, 0)[0]
    assert {i.url for i in hits} == {"https://e/bl4", "https://e/pal"}
    hits = repo.search_items(world, G2, "zebracorn", SINCE, 50, 0)[0]
    assert {i.url for i in hits} == {"https://e/bl4", "https://e/fort"}


def test_list_lounges_spans_guilds_by_design_and_nothing_else_does(world):
    assert [x.guild_id for x in repo.list_lounges(world)] == [G1, G2]
    assert [g.guild_id for g in repo.list_set_up_guilds(world)] == [G1, G2]


# --- deletion ---


def test_delete_guild_cascades_everywhere_and_shared_data_survives(world):
    g2_before = _fingerprint(world, G2)
    shared = _shared(world)
    stories_before = world.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    null_quotes = world.execute(
        "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES ('v22', ?, 'n')",
        ("b" * 64,),
    ).rowcount
    world.commit()
    assert null_quotes == 1

    assert repo.delete_guild(world, G1) is True
    for table in _PER_GUILD_TABLES:
        assert (
            world.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (G1,)).fetchone()[0]  # noqa: S608
            == 0
        )
    assert _fingerprint(world, G2) == g2_before
    assert _shared(world) == shared
    # The guild's story stays, detached from the digest that went with the guild.
    assert world.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == stories_before
    row = world.execute(
        "SELECT digest_id FROM stories WHERE headline = ?", (f"story of {G1}",)
    ).fetchone()
    assert row["digest_id"] is None
    # A v2.2-era quote row (no guild) is nobody's to delete.
    assert (
        world.execute("SELECT COUNT(*) FROM lounge_quotes_used WHERE guild_id IS NULL").fetchone()[
            0
        ]
        == 1
    )
    world.execute("INSERT INTO stories_fts (stories_fts) VALUES ('integrity-check')")
    world.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
    assert world.execute("PRAGMA foreign_key_check").fetchall() == []
    assert repo.search_items(world, G2, "zebracorn", SINCE, 50, 0)[1] == 2


def test_deleting_twice_is_false_and_a_rejoined_guild_starts_clean(world):
    assert repo.delete_guild(world, G1) is True
    assert repo.delete_guild(world, G1) is False
    assert repo.create_guild(world, G1, now=_clock()) is True
    guild = repo.get_guild(world, G1)
    assert (guild.tier, guild.set_up, guild.digest_time) == ("free", False, "09:00")
    assert repo.list_guild_games(world, G1) == []
    assert repo.get_shift(world, G1) is None and repo.get_lounge(world, G1) is None
    assert repo.recent_notices(world, G1) == []
    assert (
        world.execute("SELECT COUNT(*) FROM digests WHERE guild_id = ?", (G1,)).fetchone()[0] == 0
    )


def test_delete_guild_with_foreign_keys_off_leaves_orphans_documented(db_path):
    # Documented: the cascade is the database's, so a connection that skipped connect()
    # (foreign keys default to off in raw sqlite3) deletes just the guild row.
    with closing(connect(db_path)) as setup:
        repo.create_guild(setup, G1)
        repo.follow_game(setup, G1, "bl4", 5)
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DELETE FROM guilds WHERE guild_id = ?", (G1,))
        raw.commit()
        assert raw.execute("SELECT COUNT(*) FROM guild_games").fetchone()[0] == 1
    finally:
        raw.close()


# --- the ten-game limit ---


def _follow(path, guild_id, key, channel, outcomes, barrier=None):
    with closing(connect(path)) as c:
        if barrier:
            barrier.wait(timeout=10)
        try:
            outcomes.append(repo.follow_game(c, guild_id, key, channel))
        except repo.GameLimitError:
            outcomes.append("limit")
        except Exception as exc:
            outcomes.append(exc)


@pytest.mark.parametrize("round_number", range(3))
def test_two_connections_following_ten_each_from_empty_end_with_exactly_ten(db_path, round_number):
    with closing(connect(db_path)) as setup:
        repo.create_guild(setup, G1)
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def worker(prefix):
        with closing(connect(db_path)) as c:
            barrier.wait(timeout=10)
            for i in range(10):
                try:
                    outcomes.append(repo.follow_game(c, G1, f"{prefix}{i}", 5))
                except repo.GameLimitError:
                    outcomes.append("limit")
                except Exception as exc:
                    outcomes.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert outcomes.count(True) == 10 and outcomes.count("limit") == 10, outcomes
    with closing(connect(db_path)) as c:
        assert len(repo.list_guild_games(c, G1)) == 10


def test_two_connections_following_the_same_new_game_get_one_true_and_one_move(db_path):
    with closing(connect(db_path)) as setup:
        repo.create_guild(setup, G1)
        repo.set_guild_games(setup, G1, [(f"g{i}", 5) for i in range(9)])
    outcomes: list[object] = []
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_follow, args=(db_path, G1, "tenth", ch, outcomes, barrier))
        for ch in (11, 22)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(map(str, outcomes)) == ["False", "True"], (
        outcomes
    )  # never a limit error, never a crash
    with closing(connect(db_path)) as c:
        games = repo.list_guild_games(c, G1)
        assert len(games) == 10 and games[-1].channel_id in (11, 22)


def test_set_guild_games_racing_follow_game_never_leaves_more_than_ten(db_path):
    with closing(connect(db_path)) as setup:
        repo.create_guild(setup, G1)
        repo.set_guild_games(setup, G1, [(f"s{i}", 5) for i in range(8)])
    outcomes: list[object] = []
    barrier = threading.Barrier(7)

    def replacer():
        with closing(connect(db_path)) as c:
            barrier.wait(timeout=10)
            try:
                repo.set_guild_games(c, G1, [(f"r{i}", 6) for i in range(10)])
                outcomes.append("replaced")
            except Exception as exc:
                outcomes.append(exc)

    threads = [threading.Thread(target=replacer)] + [
        threading.Thread(target=_follow, args=(db_path, G1, f"f{i}", 7, outcomes, barrier))
        for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    bad = [o for o in outcomes if isinstance(o, Exception)]
    assert bad == [], bad
    with closing(connect(db_path)) as c:
        games = repo.list_guild_games(c, G1)
        keys = [g.game_key for g in games]
        assert len(keys) == len(set(keys)) and 1 <= len(keys) <= 10
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unfollow_then_refollow_moves_a_game_to_the_end_and_refollowing_at_ten_is_refused(conn):
    repo.create_guild(conn, G1)
    for i in range(10):
        repo.follow_game(conn, G1, f"g{i}", 5)
    assert repo.unfollow_game(conn, G1, "g3") is True
    assert repo.follow_game(conn, G1, "g3", 6) is True  # there's room again
    assert [g.game_key for g in repo.list_guild_games(conn, G1)][-1] == "g3"  # documented order
    assert (
        repo.unfollow_game(conn, G1, "g3") is True and repo.unfollow_game(conn, G1, "g3") is False
    )
    assert repo.follow_game(conn, G1, "brand-new", 5) is True  # takes the freed slot
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, G1, "g3", 5)  # and now g3 is an eleventh
    assert len(repo.list_guild_games(conn, G1)) == 10


def test_repointing_a_followed_game_keeps_its_place_in_line(conn):
    repo.create_guild(conn, G1)
    for i in range(10):
        repo.follow_game(conn, G1, f"g{i}", 5)
    assert repo.follow_game(conn, G1, "g0", 99) is False
    games = repo.list_guild_games(conn, G1)
    assert [g.game_key for g in games][0] == "g0" and games[0].channel_id == 99 and len(games) == 10


def test_a_failed_eleventh_follow_changes_nothing(conn):
    repo.create_guild(conn, G1)
    repo.set_guild_games(conn, G1, [(f"g{i}", 5) for i in range(10)])
    before = repo.list_guild_games(conn, G1)
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, G1, "extra", 5)
    assert repo.list_guild_games(conn, G1) == before
    assert not conn.in_transaction


def test_the_limit_is_per_guild(conn):
    repo.create_guild(conn, G1)
    repo.create_guild(conn, G2)
    repo.set_guild_games(conn, G1, [(f"g{i}", 5) for i in range(10)])
    assert repo.follow_game(conn, G2, "g0", 5) is True


# --- create_guild racing itself ---


def test_create_guild_racing_itself_has_one_winner_and_the_winners_settings_stick(db_path):
    outcomes: dict[str, object] = {}
    barrier = threading.Barrier(8)

    def creator(zone):
        with closing(connect(db_path)) as c:
            barrier.wait(timeout=10)
            try:
                outcomes[zone] = repo.create_guild(c, G1, timezone=zone, now=_clock())
            except Exception as exc:
                outcomes[zone] = exc

    zones = [f"Zone/{i}" for i in range(8)]
    threads = [threading.Thread(target=creator, args=(z,)) for z in zones]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    winners = [z for z, o in outcomes.items() if o is True]
    assert len(winners) == 1 and sum(o is False for o in outcomes.values()) == 7, outcomes
    with closing(connect(db_path)) as c:
        assert repo.get_guild(c, G1).timezone == winners[0]


# --- CHECK constraints, poked with awkward values ---


# Changed from "any positive integer": ping is now none, everyone, or a 17 to 20 digit
# snowflake, so the short ids that used to pass ("1", "9") moved to the rejects below.
@pytest.mark.parametrize(
    "ping", ["none", "everyone", "12345678901234567", "123456789012345678", "9" * 20]
)
def test_shift_ping_accepts_none_everyone_and_snowflake_shaped_ids(conn, ping):
    repo.create_guild(conn, G1)
    repo.set_shift(conn, G1, enabled=False, channel_id=None, ping=ping)
    assert repo.get_shift(conn, G1).ping == ping


@pytest.mark.parametrize(
    "ping",
    [
        "",
        "0",
        "1",
        "9",
        "05",
        "1234567890123456",
        "9" * 21,
        "9" * 50,
        "0" + "1" * 17,
        "-1",
        "+5",
        "1.5",
        "1e3",
        " 5",
        "5 ",
        "5\n",
        "EVERYONE",
        "Everyone",
        "٣",
        "@everyone",
        "none ",
    ],
)
def test_shift_ping_rejects_everything_else(conn, ping):
    repo.create_guild(conn, G1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_shift(conn, G1, enabled=False, channel_id=None, ping=ping)
    assert repo.get_shift(conn, G1) is None  # the failed write left no row behind


def test_shift_ping_of_none_python_none_is_refused(conn):
    repo.create_guild(conn, G1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_shift(conn, G1, enabled=False, channel_id=None, ping=None)


def test_shift_ping_passed_as_an_int_is_stored_as_text_documented(conn):
    # Documented: TEXT affinity turns the int into text, so a caller passing a role id as an
    # int works. (Was 5 until ping had to look like a snowflake; 5 is refused now.)
    repo.create_guild(conn, G1)
    repo.set_shift(conn, G1, enabled=False, channel_id=None, ping=123456789012345678)
    assert repo.get_shift(conn, G1).ping == "123456789012345678"


def test_shift_enabled_without_a_channel_is_refused_and_leaves_the_old_row_alone(conn):
    repo.create_guild(conn, G1)
    repo.set_shift(conn, G1, enabled=True, channel_id=44, ping="none", now=_clock())
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_shift(conn, G1, enabled=True, channel_id=None, ping="none", now=_clock())
    shift = repo.get_shift(conn, G1)
    assert shift.enabled and shift.channel_id == 44


@pytest.mark.parametrize("channel", [0, -1])
def test_shift_channel_id_must_be_positive_like_every_other_channel_column(conn, channel):
    repo.create_guild(conn, G1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_shift(conn, G1, enabled=True, channel_id=channel, ping="none")


def test_shift_keeps_a_stale_enabled_at_after_a_disable_documented(conn):
    # Documented: turning alerts off leaves the old enabled_at; only the next off-to-on
    # transition replaces it.
    repo.create_guild(conn, G1)
    repo.set_shift(conn, G1, enabled=True, channel_id=4, ping="none", now=_clock(T0))
    repo.set_shift(
        conn, G1, enabled=False, channel_id=None, ping="none", now=_clock(T0 + timedelta(days=1))
    )
    assert repo.get_shift(conn, G1).enabled_at == T0
    repo.set_shift(
        conn, G1, enabled=True, channel_id=4, ping="none", now=_clock(T0 + timedelta(days=2))
    )
    assert repo.get_shift(conn, G1).enabled_at == T0 + timedelta(days=2)


def test_shift_changing_channel_or_ping_while_enabled_does_not_restamp(conn):
    repo.create_guild(conn, G1)
    repo.set_shift(conn, G1, enabled=True, channel_id=4, ping="none", now=_clock(T0))
    repo.set_shift(
        conn, G1, enabled=True, channel_id=5, ping="everyone", now=_clock(T0 + timedelta(hours=9))
    )
    shift = repo.get_shift(conn, G1)
    assert (shift.channel_id, shift.ping, shift.enabled_at) == (5, "everyone", T0)


@pytest.mark.parametrize("value", ["23:59", "00:00", "19:59", "20:00", "09:30"])
def test_digest_time_accepts_every_real_clock_time(conn, value):
    repo.create_guild(conn, G1, digest_time=value)
    assert repo.get_guild(conn, G1).digest_time == value


# Changed from "documented: 24:00 and 29:59 are stored". The CHECK now wants hours 00-23.
@pytest.mark.parametrize("value", ["29:59", "24:00", "24:30", "25:00"])
def test_digest_time_refuses_impossible_clock_times(conn, value):
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_guild(conn, G1, digest_time=value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "9:00",
        "09:0",
        "09:000",
        "0900",
        "09-00",
        "30:00",
        "09:60",
        "09:00 ",
        " 09:00",
        "09:00\n",
        "٠٩:00",
        "ab:cd",
    ],
)
def test_digest_time_refuses_anything_not_shaped_like_hh_mm(conn, value):
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_guild(conn, G1, digest_time=value)
    assert repo.get_guild(conn, G1) is None


def test_timezone_has_no_check_documented(conn):
    # Documented: nothing in the database validates timezone (IANA names are the command
    # layer's job). quote_time used to be in here too; it has a real-time CHECK now.
    repo.create_guild(conn, G1, timezone="Not/AZone")
    assert repo.get_guild(conn, G1).timezone == "Not/AZone"


@pytest.mark.parametrize("value", ["99:99", "24:00", "08:60", "8:00", "0800", "", "08:00\n"])
def test_lounge_quote_time_must_be_a_real_clock_time(conn, value):
    repo.create_guild(conn, G1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_lounge(conn, _lounge(G1, quote_time=value))
    assert repo.get_lounge(conn, G1) is None


@pytest.mark.parametrize("value", ["00:00", "08:00", "23:59"])
def test_lounge_quote_time_accepts_real_clock_times(conn, value):
    repo.create_guild(conn, G1)
    repo.upsert_lounge(conn, _lounge(G1, quote_time=value))
    assert repo.get_lounge(conn, G1).quote_time == value


# --- guild_id at the ends of the integer line ---


def test_the_largest_signed_64_bit_id_works_through_every_function(conn):
    big = 2**63 - 1
    assert repo.create_guild(conn, big, now=_clock()) is True
    assert repo.get_guild(conn, big).guild_id == big
    assert repo.follow_game(conn, big, "bl4", big) is True
    repo.set_shift(conn, big, enabled=True, channel_id=big, ping=str(big), now=_clock())
    repo.upsert_lounge(conn, _lounge(big, channel_id=big))
    repo.add_notice(conn, big, "hello")
    assert repo.get_shift(conn, big).channel_id == big
    assert [n.guild_id for n in repo.recent_notices(conn, big)] == [big]
    assert repo.update_guild_settings(conn, big, admin_channel_id=big) is True
    assert repo.list_set_up_guilds(conn) == []
    assert repo.delete_guild(conn, big) is True
    assert conn.execute("SELECT COUNT(*) FROM guild_games").fetchone()[0] == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda c, g: repo.create_guild(c, g),
        lambda c, g: repo.get_guild(c, g),
        lambda c, g: repo.delete_guild(c, g),
        lambda c, g: repo.follow_game(c, g, "bl4", 5),
        lambda c, g: repo.list_guild_games(c, g),
        lambda c, g: repo.add_notice(c, g, "x"),
        lambda c, g: repo.query_items(c, g, [], SINCE, 5, 0),
    ],
)
def test_a_guild_id_one_past_64_bits_raises_overflow_rather_than_wrapping(conn, call):
    # Documented: sqlite3 refuses to bind it, so it surfaces as OverflowError from every
    # function. Discord snowflakes are below 2**63, so this is unreachable from real events.
    with pytest.raises(OverflowError):
        call(conn, 2**63)
    assert conn.execute("SELECT COUNT(*) FROM guilds").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [0, -1, -(2**63)])
def test_zero_and_negative_guild_ids(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_guild(conn, bad)
    assert repo.get_guild(conn, bad) is None
    assert repo.delete_guild(conn, bad) is False
    assert repo.update_guild_settings(conn, bad, timezone="UTC") is False
    assert repo.list_guild_games(conn, bad) == []
    assert repo.unfollow_game(conn, bad, "bl4") is False
    assert repo.get_shift(conn, bad) is None and repo.get_lounge(conn, bad) is None
    assert repo.recent_notices(conn, bad) == []
    assert repo.query_items(conn, bad, [], SINCE, 5, 0) == ([], 0)
    assert repo.adopt_orphan_digests(conn, bad) == 0
    with pytest.raises(sqlite3.IntegrityError):  # a foreign key error, not GameLimitError
        repo.follow_game(conn, bad, "bl4", 5)


# --- notices ---


@pytest.mark.parametrize(("length", "stored"), [(1, 1), (2000, 2000), (2001, 2000), (50_000, 2000)])
def test_add_notice_length_boundaries(conn, length, stored):
    repo.create_guild(conn, G1)
    repo.add_notice(conn, G1, "x" * length)
    assert len(repo.recent_notices(conn, G1)[0].text) == stored


def test_add_notice_counts_characters_not_bytes(conn):
    repo.create_guild(conn, G1)
    repo.add_notice(conn, G1, "\U0001f37a" * 2500)
    assert repo.recent_notices(conn, G1)[0].text == "\U0001f37a" * 2000


# Changed from "documented: whitespace-only text is accepted". A notice nobody can see
# is an empty notice, so it gets the same ValueError an empty string does.
@pytest.mark.parametrize("text", ["   ", "\n\t ", "\x00", " \x00 \x00"])
def test_add_notice_whitespace_or_nul_only_text_is_refused_like_empty(conn, text):
    repo.create_guild(conn, G1)
    with pytest.raises(ValueError):
        repo.add_notice(conn, G1, text)
    assert repo.recent_notices(conn, G1) == []


# Changed from "stored whole": NULs are stripped now, wherever they sit.
def test_add_notice_with_an_embedded_nul_in_the_middle_has_it_stripped(conn):
    repo.create_guild(conn, G1)
    repo.add_notice(conn, G1, "a\x00b")
    assert repo.recent_notices(conn, G1)[0].text == "ab"


def test_add_notice_with_a_leading_nul_does_not_blow_up_the_caller(conn):
    repo.create_guild(conn, G1)
    repo.add_notice(conn, G1, "\x00permission error")
    assert repo.recent_notices(conn, G1)[0].text == "permission error"


def test_a_notice_stamped_older_than_the_twenty_newest_is_pruned_immediately_documented(conn):
    # Documented: pruning goes by created_at, so a late-arriving notice with an old timestamp
    # (a clock that stepped backwards) is inserted and then deleted in the same call.
    repo.create_guild(conn, G1)
    for i in range(20):
        repo.add_notice(conn, G1, f"n{i}", now=_clock(T0 + timedelta(minutes=i)))
    repo.add_notice(conn, G1, "stale", now=_clock(T0 - timedelta(days=1)))
    texts = [n.text for n in repo.recent_notices(conn, G1)]
    assert len(texts) == 20 and "stale" not in texts


@pytest.mark.parametrize(("limit", "expected"), [(0, 0), (1, 1), (5, 5), (1000, 20)])
def test_recent_notices_limit(conn, limit, expected):
    repo.create_guild(conn, G1)
    for i in range(30):
        repo.add_notice(conn, G1, f"n{i}", now=_clock(T0 + timedelta(seconds=i)))
    assert len(repo.recent_notices(conn, G1, limit=limit)) == expected


# --- lounge ---


def test_upsert_lounge_on_an_existing_row_ignores_last_quote_date_even_when_clearing(conn):
    repo.create_guild(conn, G1)
    repo.upsert_lounge(conn, _lounge(G1, last_quote_date="2026-09-30"))
    repo.upsert_lounge(conn, _lounge(G1, last_quote_date=None))
    assert repo.get_lounge(conn, G1).last_quote_date == "2026-09-30"
    # Documented: there's no repo function that changes it after the row exists.
    # Whatever task 12 builds for "quote sent today" needs its own write.
    repo.upsert_lounge(conn, _lounge(G1, welcome_message="x", last_quote_date="2000-01-01"))
    assert repo.get_lounge(conn, G1).last_quote_date == "2026-09-30"


def test_upsert_lounge_new_row_carries_last_quote_date_and_a_none_one_stays_none(conn):
    repo.create_guild(conn, G1)
    repo.create_guild(conn, G2)
    repo.upsert_lounge(conn, _lounge(G1, last_quote_date="2026-09-30"))
    repo.upsert_lounge(conn, _lounge(G2, last_quote_date=None))
    assert repo.get_lounge(conn, G1).last_quote_date == "2026-09-30"
    assert repo.get_lounge(conn, G2).last_quote_date is None


def test_lounge_text_and_sources_round_trip_unicode_and_huge_values(conn):
    repo.create_guild(conn, G1)
    sources = [{"kind": "wikiquote", "value": "Björk \U0001f37a “quoted”"}] * 50
    message = "Willkommen, 欢迎, \U0001f44b " * 5000
    repo.upsert_lounge(conn, _lounge(G1, welcome_message=message, quote_sources=sources))
    loaded = repo.get_lounge(conn, G1)
    assert loaded.welcome_message == message and loaded.quote_sources == sources


def test_upsert_lounge_with_unserializable_sources_raises_before_writing(conn):
    repo.create_guild(conn, G1)
    repo.upsert_lounge(conn, _lounge(G1))
    with pytest.raises(TypeError):
        repo.upsert_lounge(conn, _lounge(G1, quote_sources=[object()]))
    assert repo.get_lounge(conn, G1) == _lounge(G1)


@pytest.mark.parametrize("channel", [0, -5])
def test_upsert_lounge_refuses_a_non_positive_channel_and_keeps_the_old_row(conn, channel):
    repo.create_guild(conn, G1)
    repo.upsert_lounge(conn, _lounge(G1))
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_lounge(conn, _lounge(G1, channel_id=channel))
    assert repo.get_lounge(conn, G1) == _lounge(G1)


# --- app_state ---


def test_app_state_handles_empty_unicode_and_huge_values_and_case_sensitive_keys(conn):
    repo.app_state_set(conn, "k", "")
    assert repo.app_state_get(conn, "k") == ""  # empty is a value, not "missing"
    repo.app_state_set(conn, "K", "upper")
    assert repo.app_state_get(conn, "k") == "" and repo.app_state_get(conn, "K") == "upper"
    repo.app_state_set(conn, "é\U0001f37a", "x" * 1_000_000)
    assert len(repo.app_state_get(conn, "é\U0001f37a")) == 1_000_000
    with pytest.raises(sqlite3.IntegrityError):
        repo.app_state_set(conn, "nullish", None)


# --- adopt_orphan_digests ---


def _orphan(conn, day, status="ok"):
    with conn:
        return conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES (?, ?, 'n', 'n')",
            (day, status),
        ).lastrowid


def test_adopt_mixed_orphans_adopts_the_free_days_and_leaves_the_colliding_one(conn):
    repo.create_guild(conn, G1, imported_at=T0)
    repo.create_guild(conn, G2)
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        _orphan(conn, day)
    with conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, '2026-09-02', 'ok', 'n', 'n')",
            (G1,),
        )
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, '2026-09-01', 'ok', 'n', 'n')",
            (G2,),
        )
    assert repo.adopt_orphan_digests(conn, G1) == 2  # 09-02 collided and was skipped
    left = conn.execute("SELECT run_date FROM digests WHERE guild_id IS NULL").fetchall()
    assert [r[0] for r in left] == ["2026-09-02"]
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id = ?", (G2,)).fetchone()[0] == 1
    assert repo.adopt_orphan_digests(conn, G1) == 0


def test_adopt_refuses_a_guild_that_is_not_the_imported_one_and_touches_nothing(conn):
    repo.create_guild(conn, G1, imported_at=T0)
    repo.create_guild(conn, G2)
    _orphan(conn, "2026-09-01")
    assert repo.adopt_orphan_digests(conn, G2) == 0
    assert repo.adopt_orphan_digests(conn, 999_999) == 0  # no such guild at all
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id IS NULL").fetchone()[0] == 1


def test_adopt_with_two_imported_guilds_only_the_first_row_can_adopt_documented(conn):
    # Documented: "the imported guild" is a scalar subquery, so if two rows ever carry
    # imported_at (the import is supposed to run once) only the lowest-id one gets orphans.
    repo.create_guild(conn, G1, imported_at=T0)
    repo.create_guild(conn, G2, imported_at=T0)
    _orphan(conn, "2026-09-01")
    assert repo.adopt_orphan_digests(conn, G2) == 0
    assert repo.adopt_orphan_digests(conn, G1) == 1


def test_adopt_leaves_other_guilds_digests_and_keeps_orphan_stories_attached(conn):
    repo.create_guild(conn, G1, imported_at=T0)
    repo.create_guild(conn, G2)
    orphan = _orphan(conn, "2026-09-01")
    with conn:
        conn.execute(
            "INSERT INTO stories (id, topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES (1, 'bl4', 'h', 's', 'official', ?, 'n')",
            (orphan,),
        )
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, '2026-09-05', 'ok', 'n', 'n')",
            (G2,),
        )
    repo.adopt_orphan_digests(conn, G1)
    assert conn.execute("SELECT guild_id FROM digests WHERE id = ?", (orphan,)).fetchone()[0] == G1
    assert conn.execute("SELECT digest_id FROM stories WHERE id = 1").fetchone()[0] == orphan
    assert (
        conn.execute("SELECT guild_id FROM digests WHERE run_date = '2026-09-05'").fetchone()[0]
        == G2
    )


def test_the_orphan_index_frees_the_date_once_its_row_is_adopted(conn):
    repo.create_guild(conn, G1, imported_at=T0)
    _orphan(conn, "2026-09-01")
    with pytest.raises(sqlite3.IntegrityError):
        _orphan(conn, "2026-09-01")  # the partial unique index: one orphan per day
    repo.adopt_orphan_digests(conn, G1)
    _orphan(conn, "2026-09-01")  # the day is free again for a new v2.2 orphan


# --- FTS: search_items under hostile and strange input ---

_HOSTILE = [
    '"',
    '""',
    '"""',
    '" OR "',
    'zebracorn" OR starfield OR "',
    "zebracorn OR starfield",
    "zebracorn AND NOT patch",
    "NOT",
    "AND",
    "OR",
    "NEAR",
    "NEAR(zebracorn patch)",
    "NEAR/3",
    "zebra*",
    "*",
    "**",
    "^zebracorn",
    "-zebracorn",
    "+zebracorn",
    "(",
    ")",
    "((zebracorn)",
    "zebracorn))",
    "{title}: zebracorn",
    "title:zebracorn",
    "excerpt:body",
    "title : starfield",
    "items_fts:zebracorn",
    "rank",
    "bm25(items_fts)",
    "'; DROP TABLE items; /*",
    "zebracorn; SELECT 1",
    "\\",
    '\\"',
    "%",
    "_",
    "a" * 100_000,
    " ".join(["zebracorn"] * 5000),
    "\u0000",
    "zebra\u0000corn",
    "‮esrever",
    "Ünïcödé café",
    "欢迎 テスト",
    "\U0001f37a \U0001f37a",
    "é",
    "\t\n\r zebracorn",
]


@pytest.mark.parametrize("query", _HOSTILE, ids=[f"q{i}" for i in range(len(_HOSTILE))])
def test_search_items_never_raises_and_never_leaves_followed_games(world, query):
    items, total = repo.search_items(world, G1, query, SINCE, 20, 0)
    assert total == len(items) or total > len(items)
    for item in items:
        assert item.url in {"https://e/bl4", "https://e/pal"}
        assert set(item.topic_keys) <= {"bl4", "palworld"}
    assert world.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4  # nothing dropped


def test_hostile_queries_cannot_reach_items_of_games_the_guild_does_not_follow(world):
    # starfield is tagged on an item but followed by nobody; fortnite only by G2.
    for query in (
        "starfield",
        "Unfollowed",
        "title:starfield OR zebracorn",
        "Fortnite",
        "crossover*",
    ):
        items, total = repo.search_items(world, G1, query, SINCE, 20, 0)
        urls = {i.url for i in items}
        assert urls <= {"https://e/bl4", "https://e/pal"} and total == len(urls)
    assert repo.search_items(world, G1, "Fortnite", SINCE, 20, 0) == ([], 0)
    assert repo.search_items(world, G2, "Fortnite", SINCE, 20, 0)[1] == 1


def test_search_items_a_guild_that_follows_nothing_gets_nothing(world):
    repo.create_guild(world, 3003)
    assert repo.search_items(world, 3003, "zebracorn", SINCE, 20, 0) == ([], 0)
    assert repo.query_items(world, 3003, [], SINCE, 20, 0) == ([], 0)


@pytest.mark.parametrize("query", ["", " ", "\t\n", " ", "　"])
def test_search_items_blank_queries_return_nothing(world, query):
    assert repo.search_items(world, G1, query, SINCE, 20, 0) == ([], 0)


def test_search_items_unicode_and_case_folding_find_what_a_person_would_expect(conn):
    repo.create_guild(conn, G1)
    repo.follow_game(conn, G1, "bl4", 5)
    _store(conn, "https://e/u", "Café Überraschung im Spiel", {"bl4": False}, excerpt="欢迎")
    assert repo.search_items(conn, G1, "cafe", SINCE, 5, 0)[1] == 1  # unicode61 folds accents
    assert repo.search_items(conn, G1, "ÜBERRASCHUNG", SINCE, 5, 0)[1] == 1
    assert repo.search_items(conn, G1, "欢迎", SINCE, 5, 0)[1] == 1


def test_search_items_with_a_lone_surrogate_does_not_raise(world):
    assert repo.search_items(world, G1, "\ud800", SINCE, 20, 0) == ([], 0)


def test_search_items_pagination_total_is_the_full_count(conn):
    repo.create_guild(conn, G1)
    repo.follow_game(conn, G1, "bl4", 5)
    for i in range(25):
        _store(
            conn, f"https://e/{i}", f"Needle number {i}", {"bl4": False}, T0 - timedelta(minutes=i)
        )
    page, total = repo.search_items(conn, G1, "needle", SINCE, 10, 20)
    assert total == 25 and len(page) == 5
    assert repo.search_items(conn, G1, "needle", SINCE, 10, 99) == ([], 25)
    assert repo.search_items(conn, G1, "needle", SINCE, 0, 0) == ([], 25)


def test_query_items_crosses_the_variable_chunk_boundary(conn):
    repo.create_guild(conn, G1)
    repo.follow_game(conn, G1, "bl4", 5)
    repo.follow_game(conn, G1, "palworld", 5)
    with conn:
        conn.executemany(
            "INSERT INTO items (id, url, title, excerpt, source_name, trust, collected_at) "
            "VALUES (?, ?, ?, 'x', 'Feed', 'press', ?)",
            [
                (i, f"https://e/{i}", f"Item {i}", (T0 - timedelta(seconds=i)).isoformat())
                for i in range(1, 1201)
            ],
        )
        conn.executemany(
            "INSERT INTO item_topics (item_id, topic_key) VALUES (?, ?)",
            [(i, "bl4") for i in range(1, 1201)] + [(i, "palworld") for i in range(1, 1201, 2)],
        )
    items, total = repo.query_items(conn, G1, [], SINCE, 1200, 0)
    assert total == 1200 and len(items) == 1200
    assert all(i.topic_keys == (["bl4", "palworld"] if i.id % 2 else ["bl4"]) for i in items)


# Changed from "documented: a negative limit means unlimited". SQLite reads LIMIT -1 as
# "no limit"; query_items now clamps limit to at least 1 and offset to at least 0.
def test_query_items_clamps_a_negative_limit_and_offset(conn):
    repo.create_guild(conn, G1)
    repo.follow_game(conn, G1, "bl4", 5)
    for i in range(12):
        _store(conn, f"https://e/{i}", f"Thing {i}", {"bl4": False}, T0 - timedelta(minutes=i))
    page, total = repo.query_items(conn, G1, [], SINCE, -1, 0)
    assert len(page) == 1 and total == 12
    assert repo.query_items(conn, G1, [], SINCE, 0, 0)[0] == page
    newest_two = repo.query_items(conn, G1, [], SINCE, 2, 0)[0]
    assert repo.query_items(conn, G1, [], SINCE, 2, -5)[0] == newest_two


# --- items_fts staying honest through the ways items actually change ---


def _assert_items_fts_ok(conn):
    conn.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")


def test_items_fts_survives_update_delete_duplicate_save_and_purge(conn):
    repo.create_guild(conn, G1)
    repo.follow_game(conn, G1, "bl4", 5)
    _store(conn, "https://e/1", "Original zebracorn", {"bl4": False}, T0 - timedelta(days=30))
    _store(conn, "https://e/2", "Second gadget", {"bl4": False}, T0)
    _store(conn, "https://e/2", "Second gadget", {"bl4": False}, T0)  # same URL again
    assert repo.search_items(conn, G1, "gadget", SINCE, 10, 0)[1] == 1
    with conn:
        conn.execute("UPDATE items SET title = 'Renamed quartz' WHERE url = 'https://e/2'")
    assert repo.search_items(conn, G1, "gadget", SINCE, 10, 0)[1] == 0
    assert repo.search_items(conn, G1, "quartz", SINCE, 10, 0)[1] == 1
    _assert_items_fts_ok(conn)
    repo.purge_older_than(conn, T0 - timedelta(days=1))
    assert repo.search_items(conn, G1, "zebracorn", SINCE, 10, 0)[1] == 0
    with conn:
        conn.execute("DELETE FROM items")
    assert repo.search_items(conn, G1, "quartz", SINCE, 10, 0)[1] == 0
    _assert_items_fts_ok(conn)


def test_items_fts_follows_a_guild_delete_without_touching_shared_items(world):
    before = world.execute(
        "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'zebracorn'"
    ).fetchone()[0]
    repo.delete_guild(world, G1)
    repo.delete_guild(world, G2)
    after = world.execute(
        "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'zebracorn'"
    ).fetchone()[0]
    assert before == after == 4
    _assert_items_fts_ok(world)
