"""v2.2.0 must still run against a v5 database (design.md §15's rollback promise).

Rolling back to v2.2.0 is a TAG change, which means a v2.2.0 process opens
a database that migration 005 has already reshaped. This file runs v2.2.0's
own code against one. `fixtures/v22_repo_snapshot.txt` is `newsbot/store/repo.py`
exactly as `git show v2.2.0:newsbot/store/repo.py` prints it (checked in so CI
doesn't need the tag), loaded as a throwaway module, so what's exercised is
v2.2's real SQL and not my memory of it.

The populated database comes from the `v22_db` fixture: real migrations 001
to 004, realistic rows, then 005.
"""

import sqlite3
import types
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, StoryToSave, Usage

SNAPSHOT = Path(__file__).parent / "fixtures" / "v22_repo_snapshot.txt"
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
TODAY = date(2026, 9, 30)
TOMORROW = date(2026, 10, 1)


@pytest.fixture(scope="module")
def v22() -> types.ModuleType:
    """The v2.2.0 repo module, loaded from the checked-in snapshot."""
    module = types.ModuleType("v22_repo")
    exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
    return module


@pytest.fixture
def conn(v22_db):
    with closing(connect(v22_db)) as c:
        yield c


def _clock():
    return NOW


def _item(url, topics=None, title="Zebracorn spotted in Palworld"):
    return StoredItem(
        url=url,
        title=title,
        excerpt="Stripes everywhere.",
        source_name="Feed",
        trust="press",
        published_at=NOW,
        topics=topics or {"palworld": False},
    )


def _assert_healthy(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for name in ("stories_fts", "items_fts"):
        conn.execute(f"INSERT INTO {name} ({name}) VALUES ('integrity-check')")  # noqa: S608


def test_the_snapshot_really_is_v22s_repo(v22):
    # Guards against someone "fixing" the snapshot to match today's file.
    assert not hasattr(v22, "create_guild")
    assert hasattr(v22, "claim_digest") and hasattr(v22, "claim_quote")


def test_v22_migrate_would_be_a_noop(v22_db):
    # v2.2's own runner is today's runner minus 005 (it globs the files it ships with,
    # and 005 isn't one of them): it sees user_version 5 >= its newest, and skips.
    with closing(connect(v22_db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert migrate(conn) == 5


# --- the digest guard ---


def test_get_digest_reads_the_pre_migration_rows(v22, conn):
    row = v22.get_digest(conn, TODAY)
    assert (row.id, row.status, row.posted_message_ids) == (12, "ok", [131, 132])
    assert v22.get_digest(conn, date(2026, 9, 29)).status == "failed"
    assert v22.get_digest(conn, TOMORROW) is None


def test_claim_digest_still_blocks_a_day_that_already_posted(v22, conn):
    assert v22.claim_digest(conn, TODAY, force=False, now=_clock) is None
    assert v22.claim_digest(conn, date(2026, 9, 28), force=False, now=_clock) is None  # partial


def test_claim_digest_reclaims_a_failed_day_keeping_its_id(v22, conn):
    assert v22.claim_digest(conn, date(2026, 9, 29), force=False, now=_clock) == 9
    assert v22.get_digest(conn, date(2026, 9, 29)).status == "pending"


def test_claim_digest_force_updates_in_place(v22, conn):
    assert v22.claim_digest(conn, TODAY, force=True, now=_clock) == 12
    assert (
        conn.execute("SELECT COUNT(*) FROM digests WHERE run_date = '2026-09-30'").fetchone()[0]
        == 1
    )


def test_claim_digest_for_a_new_day_inserts_a_row_with_no_guild(v22, conn):
    digest_id = v22.claim_digest(conn, TOMORROW, force=False, now=_clock)
    assert digest_id is not None and digest_id > 12
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["guild_id"] is None
    assert (row["status"], row["posted_by_game"], row["attempts"]) == ("pending", "{}", 0)
    assert v22.claim_digest(conn, TOMORROW, force=False, now=_clock) is None  # pending blocks


def test_v22_digest_guard_is_still_safe_once_a_v3_guild_has_posted_that_day(v22, conn):
    # After going public, a guild's row for the day is visible to v2.2's
    # `WHERE run_date = ?`, so a rolled-back v2.2 refuses instead of double posting.
    repo.create_guild(conn, 42)
    with conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (42, '2026-10-01', 'ok', 'n', 'n')"
        )
    assert v22.claim_digest(conn, TOMORROW, force=False, now=_clock) is None


# --- save_run: items, stories, story_items, FTS ---


def test_save_run_writes_items_stories_and_updates_the_digest(v22, conn):
    digest_id = v22.claim_digest(conn, TOMORROW, force=False, now=_clock)
    url = "https://example.com/zebra"
    v22.save_run(
        conn,
        digest_id,
        [_item(url, {"palworld": False, "borderlands4": True})],
        [
            StoryToSave(
                topic_key="palworld",
                headline="Zebracorn joins the roster",
                summary="It is striped.",
                label="official",
                item_urls=[url],
                update_of_story_id=101,
            )
        ],
        "ok",
        [777],
        None,
        Usage(120, 30),
        now=_clock,
    )
    story = conn.execute("SELECT * FROM stories WHERE headline LIKE 'Zebracorn%'").fetchone()
    assert (story["digest_id"], story["summary_id"], story["is_update_of"]) == (
        digest_id,
        None,
        101,
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM story_items WHERE story_id = ?", (story["id"],)
        ).fetchone()[0]
        == 1
    )
    digest = v22.get_digest(conn, TOMORROW)
    assert (digest.status, digest.posted_message_ids) == ("ok", [777])
    assert (
        conn.execute("SELECT input_tokens FROM digests WHERE id = ?", (digest_id,)).fetchone()[0]
        == 120
    )
    # The new items_fts triggers fire for v2.2's item inserts without v2.2 knowing.
    assert (
        conn.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'zebracorn'").fetchone()[
            0
        ]
        == 1
    )
    _assert_healthy(conn)


def test_v22_fts_search_finds_old_and_new_stories(v22, conn):
    old, old_total = v22.search_stories(conn, "vault", SINCE, 10, 0)
    assert old_total == 2 and {s.id for s in old} == {100, 102}

    digest_id = v22.claim_digest(conn, TOMORROW, force=False, now=_clock)
    v22.save_run(
        conn,
        digest_id,
        [_item("https://example.com/zebra")],
        [
            StoryToSave(
                "palworld",
                "Zebracorn news",
                "Striped.",
                "reported",
                ["https://example.com/zebra"],
                None,
            )
        ],
        "ok",
        [1],
        None,
        Usage(0, 0),
        now=_clock,
    )
    new, total = v22.search_stories(conn, "zebracorn", SINCE, 10, 0)
    assert total == 1 and new[0].headline == "Zebracorn news"
    assert new[0].urls == ["https://example.com/zebra"]
    # Old stories are still found after the new insert (the index wasn't disturbed).
    assert v22.search_stories(conn, "vault", SINCE, 10, 0)[1] == 2


def test_v22_query_stories_recent_headlines_and_status_snapshot(v22, conn):
    stories, total = v22.query_stories(conn, [], SINCE, None, 10, 0)
    assert total == 5 and stories[0].id in (103, 104)
    only_bl4, bl4_total = v22.query_stories(conn, ["borderlands4"], SINCE, "official", 10, 0)
    assert bl4_total == 2 and {s.id for s in only_bl4} == {100, 102}
    assert [h.id for h in v22.recent_headlines(conn, "borderlands4", SINCE)] == [104, 102, 100]

    snap = v22.status_snapshot(
        conn,
        datetime(2026, 9, 30, 20, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
        ["Feed", "Never Run"],
    )
    assert (snap.last_digest.id, snap.last_digest.status) == (12, "ok")
    assert [h.source_name for h in snap.source_health] == ["Feed", "Never Run"]


def test_v22_mark_digest_failed_and_purge(v22, conn):
    assert v22.claim_digest(conn, date(2026, 9, 29), force=False, now=_clock) == 9
    v22.mark_digest_failed(conn, 9, "again", [5], now=_clock)
    assert v22.get_digest(conn, date(2026, 9, 29)).posted_message_ids == [5]

    cutoff = datetime(2026, 9, 29, tzinfo=UTC)
    items_deleted, stories_deleted = v22.purge_older_than(conn, cutoff)
    assert (items_deleted, stories_deleted) == (4, 3)
    assert v22.search_stories(conn, "vault", SINCE, 10, 0)[1] == 0
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2
    # The purge went through both FTS delete triggers cleanly.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'borderlands'"
        ).fetchone()[0]
        == 0
    )
    _assert_healthy(conn)


def test_v22_source_health(v22, conn):
    assert v22.record_source_result(conn, "Feed", NOW, "boom") == 3
    assert v22.record_source_result(conn, "Feed", NOW, None) == 0


# --- SHiFT ---


def test_v22_shift_claim_post_and_read_paths(v22, conn, codes):
    new_code = "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZ9"
    assert v22.known_codes(conn, [*codes.values(), new_code]) == set(codes.values())
    state = v22.get_alert_state(conn)
    assert (state.seeded, state.ping_day, state.ping_count) == (True, "2026-09-30", 2)

    # Same day: the cap re-check sees the stored count of 2 and still allows the third ping.
    pinged = v22.claim_codes(
        conn,
        [(new_code, "Feed", "https://example.com/z")],
        pinged=True,
        local_day="2026-09-30",
        now=_clock,
        max_pings=3,
    )
    assert pinged is True
    assert v22.get_alert_state(conn).ping_count == 3
    assert (
        conn.execute("SELECT status FROM alerted_codes WHERE code = ?", (new_code,)).fetchone()[0]
        == "pending"
    )

    v22.mark_codes_posted(conn, [new_code], message_id=4242)
    row = conn.execute(
        "SELECT status, message_id, pinged FROM alerted_codes WHERE code = ?", (new_code,)
    ).fetchone()
    assert tuple(row) == ("posted", 4242, 1)

    # An already-known code aborts the whole claim, budget included (v2.2's record-then-post rule).
    with pytest.raises(sqlite3.IntegrityError):
        v22.claim_codes(
            conn,
            [(codes["posted"], "Feed", "u")],
            pinged=True,
            local_day="2026-09-30",
            now=_clock,
            max_pings=99,
        )
    assert v22.get_alert_state(conn).ping_count == 3

    # Startup cleanup flips the fixture's pending code; the read paths still work.
    assert v22.fail_pending_codes(conn) == [codes["pending"]]
    v22.record_sweep(conn, _clock, "18/19 sources ok")
    status = v22.alert_status(conn, "2026-09-30", enabled=True, max_pings=3)
    assert (status.codes_alerted, status.pings_today, status.last_sweep_summary) == (
        2,
        3,
        "18/19 sources ok",
    )
    shown, total = v22.query_codes(conn, SINCE, 20, 0)
    assert total == 5  # seeded, too_old, posted, roundup and the new one; pending/failed hidden
    assert {c.code for c in shown} == {
        codes["seeded"],
        codes["too_old"],
        codes["posted"],
        codes["roundup"],
        new_code,
    }
    _assert_healthy(conn)


def test_v22_record_silent_codes_and_roundup_claim(v22, conn):
    v22.record_silent_codes(
        conn,
        [("YYYYY-YYYYY-YYYYY-YYYYY-YYYY8", "Feed", "u", "roundup", True)],
        now=_clock,
        mark_seeded=True,
    )
    row = conn.execute(
        "SELECT status, from_roundup FROM alerted_codes WHERE code LIKE 'YYYYY%'"
    ).fetchone()
    assert tuple(row) == ("roundup", 1)
    assert (
        v22.claim_codes(
            conn,
            [("XXXXX-XXXXX-XXXXX-XXXXX-XXXX7", "Feed", "u")],
            pinged=False,
            local_day="2026-10-01",
            now=_clock,
            from_roundup=True,
        )
        is False
    )
    assert v22.get_alert_state(conn).ping_day == "2026-10-01"


# --- the lounge quote ---


def test_v22_claim_quote_guard_deck_and_reshuffle(v22, conn):
    twain = "wikiquote:Mark Twain"
    deck = v22.quote_deck_state(conn, twain)
    assert deck.used == frozenset({"b" * 64}) and deck.last_hash == "b" * 64
    assert v22.get_lounge_state(conn).last_quote_date == "2026-09-30"

    # Today is already done; tomorrow claims and writes a row with no guild.
    assert (
        v22.claim_quote(
            conn,
            source_key=twain,
            quote_hash="d" * 64,
            local_day="2026-09-30",
            reshuffle=False,
            force=False,
            now=_clock,
        )
        is False
    )
    assert (
        v22.claim_quote(
            conn,
            source_key=twain,
            quote_hash="d" * 64,
            local_day="2026-10-01",
            reshuffle=False,
            force=False,
            now=_clock,
        )
        is True
    )
    row = conn.execute(
        "SELECT guild_id FROM lounge_quotes_used WHERE quote_hash = ?", ("d" * 64,)
    ).fetchone()
    assert row["guild_id"] is None
    assert v22.get_lounge_state(conn).last_quote_date == "2026-10-01"
    assert v22.quote_deck_state(conn, twain).last_hash == "d" * 64

    # Reshuffle deletes only that source's rows.
    assert (
        v22.claim_quote(
            conn,
            source_key=twain,
            quote_hash="e" * 64,
            local_day="2026-10-02",
            reshuffle=True,
            force=False,
            now=_clock,
        )
        is True
    )
    assert v22.quote_deck_state(conn, twain).used == frozenset({"e" * 64})
    assert v22.quote_deck_state(conn, "wikiquote:Oscar Wilde").used == frozenset({"a" * 64})
    _assert_healthy(conn)


# --- rolling forward again ---


def test_after_v22_writes_v3_adopts_its_orphan_digest_and_the_day_is_not_due_twice(v22, conn):
    digest_id = v22.claim_digest(conn, TOMORROW, force=False, now=_clock)
    v22.save_run(conn, digest_id, [], [], "ok", [9], None, Usage(0, 0), now=_clock)
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id IS NULL").fetchone()[0] == 5

    # Nothing to adopt until the import has created the guild.
    assert repo.adopt_orphan_digests(conn, 42) == 0
    repo.create_guild(conn, 42, tier="comped", set_up=True, imported_at=NOW)
    assert repo.adopt_orphan_digests(conn, 42) == 5
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id = 42").fetchone()[0] == 5
    _assert_healthy(conn)


def test_after_migration_the_v22_pipeline_helpers_survive_a_second_open(v22_db, v22):
    # A brand new process (v2.2 opening the file at boot) sees a consistent database.
    with closing(connect(v22_db)) as conn:
        assert migrate(conn) == 5
        assert v22.get_digest(conn, TODAY + timedelta(days=0)).id == 12
        _assert_healthy(conn)
