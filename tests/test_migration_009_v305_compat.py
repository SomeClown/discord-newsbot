"""v3.0.5 must run on a migrated (v9) database (design.md §16, plan task 3).

Rolling back from 3.1.0 is a TAG change plus the config, so a v3.0.5 process opens a file
that migration 009 has already reshaped. This file runs v3.0.5's own code against one.
`fixtures/v305_repo_snapshot.txt` is `newsbot/store/repo.py` exactly as
`git show v3.0.5:newsbot/store/repo.py` prints it (checked in so CI doesn't need the tag),
loaded as a throwaway module, the same trick the v2.2.0 file plays. What's exercised is
v3.0.5's real SQL, not my memory of it.

v3.0.5's `migrate()` is today's `db.py` with one difference: it globs the migration files it
ships with (001 to 008). `git diff v3.0.5 -- newsbot/store/db.py newsbot/store/migrations`
was empty when this was written, so pointing today's runner at a directory holding only
001 to 008 is the same thing, and `test_v305_migrate_...` does exactly that.

The populated database is the `v22_db` fixture (real migrations 001 to 009, a v2.2 morning's
rows) plus servers, codes and, on top, rows in the 009 tables that v3.0.5 has never heard of.
"""

import types
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.collectors.base import ItemDetails
from newsbot.shift.details import DetailObservation
from newsbot.store import db, repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeView, QueuedCode

SNAPSHOT = Path(__file__).parent / "fixtures" / "v305_repo_snapshot.txt"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
NEW_CODE = "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ-ZZZ77"
GAME = "borderlands4"
UNKNOWN_TO_305 = (
    "code_details",
    "guild_alert_messages",
    "edits_blocked_at",
    "shift_details_since",
    "upsert_code_details",
)


@pytest.fixture(scope="module")
def v305() -> types.ModuleType:
    """The v3.0.5 repo module, loaded from the checked-in snapshot."""
    module = types.ModuleType("v305_repo")
    exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
    return module


@pytest.fixture
def conn(v22_db):
    with closing(connect(v22_db)) as c:
        yield c


def _clock():
    return NOW


def _dump(conn, table, order):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]  # noqa: S608


def _add_servers(v305, conn):
    """Two servers set up the v3.0.5 way, and a pair of 009 rows for each that it can't see."""
    for guild_id in (42, 43):
        v305.create_guild(conn, guild_id, set_up=True, now=lambda: SINCE - timedelta(days=30))
        v305.follow_game(conn, guild_id, GAME, 10)
        v305.set_shift(
            conn,
            guild_id,
            enabled=True,
            channel_id=guild_id * 100,
            ping="everyone",
            now=lambda: SINCE - timedelta(days=30),
        )
    with conn:
        for guild_id, message_id in ((42, 9001), (42, 9002), (43, 9003)):
            conn.execute(
                "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
                "posted_at, rendered_at) VALUES (?, ?, ?, '@everyone New SHiFT code!', ?, ?)",
                (guild_id, message_id, guild_id * 100, SINCE.isoformat(), SINCE.isoformat()),
            )
        conn.execute("UPDATE guild_shift SET edits_blocked_at = ? WHERE guild_id = 42", ("n",))


# --- the snapshot and v3.0.5's migrate ---


def test_the_snapshot_really_is_v305s_repo(v305):
    # Guards against someone "fixing" the snapshot to match today's file.
    source = SNAPSHOT.read_text()
    for name in UNKNOWN_TO_305:
        assert name not in source, name
    assert hasattr(v305, "queue_confirmed_followups") and hasattr(v305, "claim_guild_followups")
    assert hasattr(v305, "purge_guild_data_older_than")


def test_v305_migrate_would_be_a_noop_on_a_v9_database(
    v22_db, monkeypatch, tmp_path, migrations_up_to
):
    # v3.0.5 ships migrations 001 to 008; it reads user_version 9 and applies nothing.
    with closing(connect(v22_db)) as c:
        before = sorted(
            tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
        )
        assert c.execute("PRAGMA user_version").fetchone()[0] == 10
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "shipped_with_v305", 8))
        with closing(connect(v22_db)) as c:
            assert migrate(c) == 10
            assert migrate(c) == 10
            assert c.execute("PRAGMA user_version").fetchone()[0] == 10
            after = sorted(
                tuple(r)
                for r in c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
            )
    assert after == before


# --- v3.0.5's SHiFT paths on a v9 database ---


def test_v305_runs_a_whole_code_through_a_v9_database(v305, conn, codes):
    _add_servers(v305, conn)
    repo.app_state_set(conn, repo.DETAILS_SINCE_KEY, SINCE.isoformat())
    repo.upsert_code_details(
        conn,
        [
            DetailObservation(
                code=codes["posted"],
                details=ItemDetails(
                    code=codes["posted"],
                    rank="table",
                    reward="3 Golden Keys",
                    category="golden",
                    expires_on=date(2026, 10, 14),
                ),
                source_name="mentalmars",
            )
        ],
        now=SINCE,
    )
    kept = {
        "code_details": _dump(conn, "code_details", "code"),
        "guild_alert_messages": _dump(conn, "guild_alert_messages", "guild_id, message_id"),
    }

    # Release the code and queue it for both servers.
    released = v305.record_released_codes(
        conn,
        [(NEW_CODE, "Feed", "https://example.com/z", False)],
        now=_clock,
        queue_for_games=[GAME],
        flags={NEW_CODE: (True, False)},
    )
    assert released == [NEW_CODE]
    queued = v305.queued_guild_codes(conn, 42)
    assert [(q.code, q.golden, q.trusted) for q in queued] == [(NEW_CODE, True, False)]
    assert isinstance(queued[0], QueuedCode) and queued[0].details is None
    assert v305.queued_guild_ids(conn) == [42, 43]

    # Claim and post it for server 42, unpinged and follow-up eligible.
    claimed = v305.claim_guild_codes(
        conn,
        42,
        [NEW_CODE],
        pinged=False,
        local_day="2026-10-10",
        now=_clock,
        max_pings=3,
        followup_ok=True,
    )
    assert (claimed.codes, claimed.pinged) == ([NEW_CODE], False)
    v305.mark_guild_codes_posted(conn, 42, [NEW_CODE], message_id=5000)
    row = conn.execute(
        "SELECT status, message_id FROM guild_code_posts WHERE guild_id = 42 AND code = ?",
        (NEW_CODE,),
    ).fetchone()
    assert tuple(row) == ("posted", 5000)

    # A second source confirms it, so server 42 gets its one follow-up.
    v305.record_code_sightings(
        conn,
        [(NEW_CODE, "r/Borderlands4", False, False), (NEW_CODE, "Bluesky", False, False)],
        now=_clock,
    )
    assert v305.queue_confirmed_followups(conn, [NEW_CODE], now=_clock) == 1
    assert v305.queued_followup_guild_ids(conn) == [42]
    assert v305.claim_guild_followups(
        conn, 42, [NEW_CODE], local_day="2026-10-10", max_pings=3, now=_clock
    ) == [NEW_CODE]
    v305.mark_guild_followups_posted(conn, 42, [NEW_CODE], message_id=5001)

    # /shift codes, as v3.0.5 builds it.
    shown, total = v305.query_codes(conn, NOW - timedelta(days=30), 50, 0)
    assert total == len(shown) and NEW_CODE in {c.code for c in shown}
    assert all(isinstance(c, CodeView) and c.details is None for c in shown)

    # Settings: saving them again leaves the column it doesn't know about alone.
    v305.set_shift(conn, 42, enabled=True, channel_id=4200, ping="none", now=_clock)
    shift = v305.get_shift(conn, 42)
    assert (shift.channel_id, shift.ping) == (4200, "none")
    assert (
        conn.execute("SELECT edits_blocked_at FROM guild_shift WHERE guild_id = 42").fetchone()[0]
        == "n"
    )
    assert v305.eligible_shift_guilds(conn, [GAME], NOW)

    # Startup recovery and retention, none of which know the new tables exist.
    v305.fail_pending_guild_codes(conn)
    v305.fail_queued_guild_codes(conn, 43)
    v305.purge_older_than(conn, NOW - timedelta(days=90))
    v305.purge_guild_data_older_than(conn, NOW - timedelta(days=90))
    assert _dump(conn, "code_details", "code") == kept["code_details"]
    assert (
        _dump(conn, "guild_alert_messages", "guild_id, message_id") == kept["guild_alert_messages"]
    )

    # Removing a server cascades its alert-message records and nobody else's.
    assert v305.delete_guild(conn, 42) is True
    left = [
        (r["guild_id"], r["message_id"]) for r in conn.execute("SELECT * FROM guild_alert_messages")
    ]
    assert left == [(43, 9003)]
    assert _dump(conn, "code_details", "code") == kept["code_details"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v305_can_post_to_a_server_that_has_an_edits_blocked_stamp(v305, conn):
    # set_shift's INSERT names its columns, so a server whose edits are blocked still saves.
    _add_servers(v305, conn)
    v305.set_shift(conn, 42, enabled=False, channel_id=None, ping="none", now=_clock)
    assert v305.get_shift(conn, 42).enabled is False
    # A server v3.0.5 creates after the migration has the column too, as NULL.
    v305.create_guild(conn, 77, set_up=True, now=_clock)
    v305.set_shift(conn, 77, enabled=True, channel_id=7700, ping="everyone", now=_clock)
    row = conn.execute("SELECT edits_blocked_at FROM guild_shift WHERE guild_id = 77").fetchone()
    assert row[0] is None


def test_v305_leaves_the_009_tables_empty_when_it_runs_a_release_on_its_own(v305, conn):
    # On a database no 3.1.0 process has used, v3.0.5 never writes a row it doesn't know.
    _add_servers(v305, conn)
    with conn:
        conn.execute("DELETE FROM guild_alert_messages")
    v305.record_released_codes(
        conn,
        [(NEW_CODE, "Feed", "https://example.com/z", False)],
        now=_clock,
        queue_for_games=[GAME],
    )
    v305.claim_guild_codes(conn, 43, [NEW_CODE], pinged=True, local_day="2026-10-10", now=_clock)
    v305.mark_guild_codes_posted(conn, 43, [NEW_CODE], message_id=5000)
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM guild_alert_messages").fetchone()[0] == 0


# --- rolling forward again ---


def test_rolling_forward_after_v305_released_a_code_keeps_its_first_sighting_as_the_clock(
    v305, conn
):
    # v3.0.5 releases a code after 3.1.0 ran once. When 3.1.0 comes back and a table lists it,
    # the code's first sighting (after the marker) is its clock start, not the listing time.
    _add_servers(v305, conn)
    repo.app_state_set(conn, repo.DETAILS_SINCE_KEY, SINCE.isoformat())
    v305.record_released_codes(
        conn,
        [(NEW_CODE, "Feed", "https://example.com/z", False)],
        now=_clock,
        queue_for_games=[GAME],
    )
    later = NOW + timedelta(hours=6)
    repo.upsert_code_details(
        conn,
        [
            DetailObservation(
                code=NEW_CODE,
                details=ItemDetails(code=NEW_CODE, rank="table", reward="A skin", listed=True),
                source_name="mentalmars",
            )
        ],
        now=later,
    )
    got = repo.code_details_for(conn, [NEW_CODE])[NEW_CODE]
    assert got.clock_start == NOW and got.first_listed_at == later
    # And the server records v3.0.5 wrote are readable by today's code.
    assert [q.code for q in repo.queued_guild_codes(conn, 42)] == [NEW_CODE]


def test_the_009_columns_v305_does_not_name_are_all_optional(conn):
    # The reason a v3.0.5 INSERT can't break: nothing 009 added to an old table is required.
    info = {r["name"]: r for r in conn.execute("PRAGMA table_info(guild_shift)")}
    assert (
        info["edits_blocked_at"]["notnull"] == 0 and info["edits_blocked_at"]["dflt_value"] is None
    )
    # And there's no trigger or view 009 hung on the tables v3.0.5 writes.
    extras = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view') "
        "AND tbl_name IN ('code_details', 'guild_alert_messages', 'guild_shift', "
        "'guild_code_posts')"
    ).fetchall()
    assert extras == []
