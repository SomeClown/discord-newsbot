"""Adversarial tests for migration 009 and the retention it adds (design.md §16, plan task 3),
beyond test_migration_009.py and test_migration_009_v305_compat.py.

Angle (test-engineer brief, 2026-10-09): 009 is additive, which makes it easy to trust, so these
tests go after the ways "additive" can still hurt: an empty database, a v8 file with real rows,
a crash between any two statements (a polite exception after each one, and a real `os._exit`
at each traced step), several processes migrating the same file at once, a stray table in the
way, and the rollout marker being deleted afterwards. Then the two cleanup paths, removing a
server and retention, with the 30 day boundary taken to the microsecond.

Where the behavior is surprising it's pinned and labelled "documented".
"""

from __future__ import annotations

import random
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newsbot.collectors.base import ItemDetails
from newsbot.shift.details import DetailObservation
from newsbot.store import db, repo
from newsbot.store.db import connect, migrate

MIGRATION = Path(db.__file__).parent / "migrations" / "009_shift_details.sql"
OLD = "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWX1"
NEW_THINGS = {"code_details", "guild_alert_messages"}


def rng_for(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 (a reproducible sequence, not a secret)


@pytest.fixture
def v8_path(tmp_path, monkeypatch, migrations_up_to):
    """A v8 database with the kinds of rows prod has: servers, posted codes, message ids."""
    path = tmp_path / "v8.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "migrations_v8", 8))
        with closing(connect(path)) as conn:
            assert migrate(conn) == 8
            with conn:
                conn.execute(
                    "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
                    "message_id, pinged, status) VALUES (?, '2026-10-01T10:00:00+00:00', "
                    "'Feed', 'https://example.com/p', 999, 1, 'posted')",
                    (OLD,),
                )
                for guild_id in (7, 8):
                    conn.execute(
                        "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (?, 'n', 'n')",
                        (guild_id,),
                    )
                    conn.execute(
                        "INSERT INTO guild_shift (guild_id, enabled, channel_id, ping, enabled_at, "
                        "ping_day, ping_count) "
                        "VALUES (?, 1, 555, 'everyone', 'n', '2026-10-01', 2)",
                        (guild_id,),
                    )
                conn.execute(
                    "INSERT INTO guild_code_posts (guild_id, code, status, message_id, pinged, "
                    "claimed_at) VALUES (7, ?, 'posted', 999, 1, 'n')",
                    (OLD,),
                )
    return path


def _state(conn) -> dict:
    """Everything a half-applied 009 could leave a mark on: schema, rows and the version."""
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")
        if not r[0].startswith(("sqlite_", "items_fts", "stories_fts"))
    ]
    return {
        "version": conn.execute("PRAGMA user_version").fetchone()[0],
        "schema": sorted(
            tuple(r)
            for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        ),
        "rows": {
            t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}"))  # noqa: S608
            for t in tables
        },
    }


def _statements(text: str) -> list[str]:
    out, buffer = [], ""
    for line in text.splitlines():
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            out.append(buffer)
            buffer = ""
    assert buffer.strip() == ""
    return out


def _marker(conn):
    row = conn.execute("SELECT value FROM app_state WHERE key = 'shift_details_since'").fetchone()
    return datetime.fromisoformat(row[0]) if row else None


# --- an empty database ---


def test_an_empty_database_migrates_clean_and_every_new_table_starts_empty(tmp_path):
    with closing(connect(tmp_path / "fresh.db")) as conn:
        assert migrate(conn) == 9
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in NEW_THINGS:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        # The only app_state row 009 writes is the marker, and it's "now", with a zone.
        assert _marker(conn).tzinfo is not None
        assert abs(datetime.now(UTC) - _marker(conn)) < timedelta(minutes=5)


def test_a_fresh_database_and_a_migrated_v8_one_end_up_with_the_same_schema(tmp_path, v8_path):
    with closing(connect(tmp_path / "fresh.db")) as fresh, closing(connect(v8_path)) as old:
        migrate(fresh)
        migrate(old)
        assert _state(fresh)["schema"] == _state(old)["schema"]


def test_migrating_a_current_database_again_is_a_no_op_for_schema_rows_and_marker(tmp_path):
    with closing(connect(tmp_path / "x.db")) as conn:
        migrate(conn)
        before = _state(conn)
        assert migrate(conn) == 9
        assert _state(conn) == before


def test_a_deleted_marker_is_not_put_back_by_migrating_again_documented(tmp_path):
    # `migrate` only applies files newer than user_version. The repo reads a missing marker
    # as "now" (safe for old codes), so this is fine, but nobody should expect a repair.
    with closing(connect(tmp_path / "x.db")) as conn:
        migrate(conn)
        repo.app_state_delete(conn, repo.DETAILS_SINCE_KEY)
        assert migrate(conn) == 9
        assert _marker(conn) is None


# --- a v8 file with data ---


def test_every_pre_existing_code_predates_the_marker_so_the_rollout_rule_applies(v8_path):
    with closing(connect(v8_path)) as conn:
        migrate(conn)
        marker = _marker(conn)
        first_seen = datetime.fromisoformat(
            conn.execute("SELECT first_seen_at FROM alerted_codes").fetchone()[0]
        )
        assert first_seen < marker
        # And the real store, reading the real marker, hands the old code a fresh clock...
        at = marker + timedelta(hours=1)
        repo.upsert_code_details(
            conn,
            [DetailObservation(OLD, ItemDetails(OLD, "table", reward="x", listed=True), "m")],
            now=at,
        )
        assert repo.code_details_for(conn, [OLD])[OLD].clock_start == at
        # ... while a code first seen a minute after the marker keeps its first sighting.
        fresh = marker + timedelta(minutes=1)
        repo.record_released_codes(
            conn, [(CODE, "Feed", "https://example.com/z", False)], now=lambda: fresh
        )
        repo.upsert_code_details(
            conn,
            [DetailObservation(CODE, ItemDetails(CODE, "table", reward="x"), "m")],
            now=at,
        )
        assert repo.code_details_for(conn, [CODE])[CODE].clock_start == fresh


def test_the_marker_text_orders_like_the_datetime_against_isoformat_values_with_microseconds(
    v8_path,
):
    # The plan's claim: strftime's marker sorts correctly against Python's isoformat() text
    # because '+' sorts before '.'. Test it on a few hundred random neighbours of the marker.
    with closing(connect(v8_path)) as conn:
        migrate(conn)
        text = conn.execute(
            "SELECT value FROM app_state WHERE key = 'shift_details_since'"
        ).fetchone()[0]
    marker = datetime.fromisoformat(text)
    rng = rng_for(9)
    for _ in range(400):
        other = marker + timedelta(
            seconds=rng.choice([-2, -1, 0, 0, 0, 1, 2]), microseconds=rng.choice([-1, 0, 1, 500000])
        )
        assert (other.isoformat() < text) == (other < marker), other.isoformat()
        assert (other.isoformat() == text) == (other == marker), other.isoformat()


def test_two_thousand_message_rows_get_the_index_and_the_lookup_uses_it(v8_path):
    with closing(connect(v8_path)) as conn:
        with conn:
            codes = [f"{i:05d}-BBBBB-BBBBB-BBBBB-BBBBB" for i in range(2000)]
            conn.executemany(
                "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
                "VALUES (?, '2026-10-01T10:00:00+00:00', 'Feed', 'u', 'posted')",
                [(c,) for c in codes],
            )
            conn.executemany(
                "INSERT INTO guild_code_posts (guild_id, code, status, message_id, pinged, "
                "claimed_at) VALUES (7, ?, 'posted', ?, 0, 'n')",
                [(c, 10_000 + i // 3) for i, c in enumerate(codes)],
            )
        migrate(conn)
        plan = " ".join(
            r["detail"]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT code FROM guild_code_posts "
                "WHERE guild_id = 7 AND message_id = 10500 AND from_roundup = 0 ORDER BY rowid"
            )
        )
        rows = conn.execute(
            "SELECT code FROM guild_code_posts WHERE guild_id = 7 AND message_id = 10500 "
            "AND from_roundup = 0 ORDER BY rowid"
        ).fetchall()
    assert "idx_guild_code_posts_message" in plan
    assert [r[0] for r in rows] == [f"{i:05d}-BBBBB-BBBBB-BBBBB-BBBBB" for i in (1500, 1501, 1502)]


def test_a_stray_table_with_a_009_name_makes_it_fail_loudly_and_leave_v8_alone_documented(v8_path):
    with closing(connect(v8_path)) as conn:
        conn.execute("CREATE TABLE code_details (code TEXT)")
        conn.commit()
        before = _state(conn)
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            migrate(conn)
        assert not conn.in_transaction
        assert _state(conn) == before
        # A human removes the stray table, and then it goes through.
        conn.execute("DROP TABLE code_details")
        conn.commit()
        assert migrate(conn) == 9


def test_a_stray_marker_row_of_the_same_name_is_kept_not_overwritten(v8_path):
    with closing(connect(v8_path)) as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) "
            "VALUES ('shift_details_since', '2020-01-01T00:00:00+00:00')"
        )
        conn.commit()
        migrate(conn)
        assert _marker(conn) == datetime(2020, 1, 1, tzinfo=UTC)


# --- crashing in the middle ---


def test_an_exception_after_every_single_statement_of_009_leaves_v8_untouched(
    v8_path, tmp_path, monkeypatch, migrations_up_to
):
    text = MIGRATION.read_text()
    statements = _statements(text)
    # 2 tables, 3 indexes, 3 ALTERs (edits_blocked_at and the two code flags), the marker
    assert len(statements) == 9
    with closing(connect(v8_path)) as conn:
        before = _state(conn)
    assert before["version"] == 8

    for k in range(1, len(statements) + 1):
        directory = migrations_up_to(tmp_path / f"cut{k}", 8)
        broken = "".join(statements[:k]) + "SELECT * FROM newsbot_boom;\n" + "".join(statements[k:])  # noqa: S608
        (directory / MIGRATION.name).write_text(broken)
        work = tmp_path / f"copy{k}.db"
        shutil.copy(v8_path, work)
        with monkeypatch.context() as m:
            m.setattr(db, "_MIGRATIONS_DIR", directory)
            with closing(connect(work)) as conn:
                with pytest.raises(sqlite3.OperationalError, match="newsbot_boom"):
                    migrate(conn)
                assert not conn.in_transaction
                assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with closing(connect(work)) as conn:
            assert _state(conn) == before, f"statement {k} leaked: {statements[k - 1][:60]!r}"
            # And the real 009 still goes in afterwards.
            assert migrate(conn) == 9
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


_KILL_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from newsbot.store.db import connect, migrate
    kill_at = int(sys.argv[2])
    seen = [0]
    conn = connect(sys.argv[1])
    def trace(sql):
        seen[0] += 1
        if seen[0] == kill_at:
            os._exit(7)
    conn.set_trace_callback(trace)
    migrate(conn)
    os._exit(0)
    """
)


def test_a_process_killed_at_any_step_of_009_leaves_v8_and_the_next_start_finishes_it(
    v8_path, tmp_path, monkeypatch, migrations_up_to
):
    probe = tmp_path / "probe.db"
    shutil.copy(v8_path, probe)
    seen = [0]
    with closing(connect(probe)) as conn:
        conn.set_trace_callback(lambda sql: seen.__setitem__(0, seen[0] + 1))
        migrate(conn)
    total = seen[0]
    assert total >= 8
    # The statements a start that migrates nothing runs after the last COMMIT: the version read
    # and the check for columns 009 grew during the build (QA batch 1, fix 15).
    seen[0] = 0
    with closing(connect(probe)) as conn:
        conn.set_trace_callback(lambda sql: seen.__setitem__(0, seen[0] + 1))
        migrate(conn)
    tail = seen[0] - 1
    with closing(connect(v8_path)) as conn:
        before = _state(conn)

    killed_before_commit = 0
    for point in range(1, total + 1):
        work = tmp_path / f"kill{point}.db"
        shutil.copy(v8_path, work)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _KILL_SCRIPT, str(work), str(point)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 7, (point, result.stderr)
        with closing(connect(work)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 8:
                assert _state(conn) == before, f"kill at traced statement {point} left damage"
                killed_before_commit += 1
                # v3.0.5 (migrations 001 to 008 only) opens this file without a murmur...
                with monkeypatch.context() as m:
                    m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "v305_dir", 8))
                    assert migrate(conn) == 8
                    assert _state(conn) == before
            else:
                assert version == 9 and point >= total - tail, point
            # ... and 3.1.0 starting again finishes the job.
            assert migrate(conn) == 9
            assert NEW_THINGS <= {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert _marker(conn) is not None
    assert killed_before_commit >= total - tail - 1


def test_four_processes_migrating_one_file_at_once_all_see_9_and_write_one_marker(
    v8_path, tmp_path
):
    barrier = threading.Barrier(4)
    results, errors = [], []

    def run():
        try:
            with closing(connect(v8_path)) as conn:
                barrier.wait()
                results.append(migrate(conn))
        except BaseException as exc:  # noqa: BLE001 (the test reports it)
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert results == [9, 9, 9, 9]
    with closing(connect(v8_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM app_state WHERE key = 'shift_details_since'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --- retention and removing a server ---


@pytest.fixture
def conn(v8_path):
    with closing(connect(v8_path)) as c:
        migrate(c)
        yield c


def _message(conn, guild_id, message_id, posted_at, *, state="live"):
    with conn:
        conn.execute(
            "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
            "posted_at, rendered_at, state) VALUES (?, ?, 5, 'New SHiFT code!', ?, ?, ?)",
            (guild_id, message_id, posted_at.isoformat(), posted_at.isoformat(), state),
        )


def _left(conn):
    return sorted(r[0] for r in conn.execute("SELECT message_id FROM guild_alert_messages"))


def test_the_30_day_line_is_exact_to_the_microsecond_and_the_edge_itself_stays(conn):
    edge = NOW - timedelta(days=30)
    _message(conn, 7, 1, edge - timedelta(microseconds=1))
    _message(conn, 7, 2, edge)
    _message(conn, 7, 3, edge + timedelta(microseconds=1))
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: NOW)
    assert _left(conn) == [2, 3]


def test_the_boundary_does_not_care_what_fixed_offset_now_is_given_in(conn):
    edge = NOW - timedelta(days=30)
    _message(conn, 7, 1, edge - timedelta(seconds=1))
    _message(conn, 7, 2, edge + timedelta(seconds=1))
    offset_now = NOW.astimezone(timezone(timedelta(hours=-8)))
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: offset_now)
    assert _left(conn) == [2]


def test_the_boundary_does_not_care_what_named_zone_now_is_given_in(conn):
    edge = NOW - timedelta(days=30)
    _message(conn, 7, 1, edge - timedelta(seconds=1))
    _message(conn, 7, 2, edge + timedelta(seconds=1))
    pacific = NOW.astimezone(ZoneInfo("America/Los_Angeles"))  # Nov 20 PST; edge is in PDT
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: pacific)
    assert _left(conn) == [2]


def test_a_naive_now_is_read_as_utc_for_retention_documented(conn):
    edge = NOW - timedelta(days=30)
    _message(conn, 7, 1, edge - timedelta(seconds=1))
    _message(conn, 7, 2, edge + timedelta(seconds=1))
    repo.purge_guild_data_older_than(
        conn, NOW - timedelta(days=90), now=lambda: NOW.replace(tzinfo=None)
    )
    assert _left(conn) == [2]


def test_live_and_gone_records_are_both_dropped_when_old_and_both_kept_when_new(conn):
    old = NOW - timedelta(days=45)
    new = NOW - timedelta(days=2)
    _message(conn, 7, 1, old, state="live")
    _message(conn, 7, 2, old, state="gone")
    _message(conn, 8, 3, new, state="live")
    _message(conn, 8, 4, new, state="gone")
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: NOW)
    assert _left(conn) == [3, 4]


@pytest.mark.parametrize("cutoff_days", [1, 7, 90, 365])
def test_the_30_day_rule_does_not_depend_on_the_digest_retention_cutoff(conn, cutoff_days):
    _message(conn, 7, 1, NOW - timedelta(days=31))
    _message(conn, 7, 2, NOW - timedelta(days=29))
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=cutoff_days), now=lambda: NOW)
    assert _left(conn) == [2]


def test_the_real_call_site_shape_still_works_with_no_now_and_uses_the_real_clock(conn):
    # client.py calls purge_guild_data_older_than(conn, cutoff) with nothing else.
    real = datetime.now(UTC)
    _message(conn, 7, 1, real - timedelta(days=31))
    _message(conn, 7, 2, real - timedelta(days=29))
    assert repo.purge_guild_data_older_than(conn, real - timedelta(days=90)) == (0, 0)
    assert _left(conn) == [2]


def test_retention_never_touches_code_details_alerted_codes_posts_or_settings(conn):
    repo.upsert_code_details(
        conn,
        [DetailObservation(OLD, ItemDetails(OLD, "table", reward="x", listed=True), "m")],
        now=NOW - timedelta(days=2000),
    )
    _message(conn, 7, 1, NOW - timedelta(days=400))
    before = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        for t in ("code_details", "alerted_codes", "guild_code_posts", "guild_shift", "guilds")
    }
    repo.purge_guild_data_older_than(conn, NOW, now=lambda: NOW)
    repo.purge_older_than(conn, NOW)
    after = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        for t in before
    }
    assert after == before
    assert _left(conn) == []


def test_retention_with_nothing_to_delete_or_nothing_in_the_table_is_quiet(conn):
    assert repo.purge_guild_data_older_than(conn, NOW, now=lambda: NOW) == (0, 0)
    assert not conn.in_transaction


def test_removing_a_server_takes_its_alert_messages_and_settings_and_nobody_elses(conn):
    repo.upsert_code_details(
        conn, [DetailObservation(OLD, ItemDetails(OLD, "table", reward="x"), "m")], now=NOW
    )
    _message(conn, 7, 1, NOW)
    _message(conn, 7, 2, NOW)
    _message(conn, 8, 3, NOW)
    conn.execute("UPDATE guild_shift SET edits_blocked_at = 'n' WHERE guild_id = 7")
    conn.commit()
    assert repo.delete_guild(conn, 7) is True
    assert _left(conn) == [3]
    assert conn.execute("SELECT guild_id FROM guild_shift").fetchall()[0][0] == 8
    assert (
        conn.execute("SELECT COUNT(*) FROM guild_code_posts WHERE guild_id = 7").fetchone()[0] == 0
    )
    # Shared records are the bot's, not the server's.
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert repo.delete_guild(conn, 7) is False  # a second removal is a quiet no-op
    assert _left(conn) == [3]


def test_a_server_that_comes_back_does_not_inherit_the_old_servers_alert_messages(conn):
    _message(conn, 7, 1, NOW)
    repo.delete_guild(conn, 7)
    repo.create_guild(conn, 7)
    assert _left(conn) == []
    # The same message id can be recorded again for the returning server.
    _message(conn, 7, 1, NOW)
    assert _left(conn) == [1]
