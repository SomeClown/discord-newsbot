"""Adversarial tests for migration 005 and the foreign-keys-off runner, beyond
test_migration_005.py's own happy paths and single-failure coverage.

Angle (test-engineer brief, 2026-09-30): 005 drops and renames the two tables
that hold the friend's real history, inside a runner that turns foreign keys
off to do it. That's a lot of trust to put in one SQL file, so these tests go
looking for the ways it could be handed a worse database than the tidy one it
was written against: a pre-existing foreign key violation, a stray table with
a name 005 wants, tens of thousands of rows, a process killed between any
two statements (for real, with `os._exit`, not a polite exception), a reader
sitting in the middle of a WAL snapshot, a writer hogging the lock.

Where the behavior is surprising it's pinned and labelled "documented".
Two of those matter for the production deploy: a pre-existing foreign key
violation makes the migration refuse to run (loudly, naming the table, and
leaving the file at v4), and so does any stray object that shares a name with
one 005 creates. Both fail safe, and both need a human before the retry.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import types
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from newsbot.store import db, repo
from newsbot.store.db import StoreError, connect, migrate

_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"
SNAPSHOT = Path(__file__).parent / "fixtures" / "v22_repo_snapshot.txt"
TABLES = ["items", "item_topics", "digests", "stories", "story_items", "alerted_codes"]


def _dump(conn, sql):
    return [tuple(row) for row in conn.execute(sql).fetchall()]


def _state(conn):
    """Everything that should be byte-for-byte unchanged when a migration rolls back."""
    return (
        conn.execute("PRAGMA user_version").fetchone()[0],
        _dump(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2"),
        {t: _dump(conn, f"SELECT * FROM {t} ORDER BY 1, 2") for t in TABLES},  # noqa: S608
    )


def _assert_healthy(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for name in ("stories_fts", "items_fts"):
        conn.execute(f"INSERT INTO {name} ({name}) VALUES ('integrity-check')")  # noqa: S608


@pytest.fixture(scope="module")
def v22() -> types.ModuleType:
    module = types.ModuleType("v22_repo_adv")
    exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
    return module


# --- a database that already has a foreign key violation ---


def _break_with_fk_off(path, *statements):
    with closing(connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for sql in statements:
            conn.execute(sql)
        conn.commit()


@pytest.mark.parametrize(
    ("statements", "tables"),
    [
        pytest.param(
            ["INSERT INTO story_items (story_id, item_id) VALUES (9999, 10)"],
            ["story_items"],
            id="orphan-story-item",
        ),
        pytest.param(
            ["INSERT INTO story_items (story_id, item_id) VALUES (100, 9999)"],
            ["story_items"],
            id="story-item-missing-item",
        ),
        pytest.param(
            ["UPDATE stories SET digest_id = 9999 WHERE id = 100"],
            ["stories"],
            id="story-missing-digest",
        ),
        pytest.param(
            ["INSERT INTO item_topics (item_id, topic_key) VALUES (9999, 'x')"],
            ["item_topics"],
            id="orphan-topic",
        ),
        pytest.param(
            ["UPDATE stories SET is_update_of = 9999 WHERE id = 102"],
            ["stories"],
            id="dangling-update-of",
        ),
        pytest.param(
            [
                "INSERT INTO story_items (story_id, item_id) VALUES (9999, 10)",
                "INSERT INTO item_topics (item_id, topic_key) VALUES (9999, 'x')",
            ],
            ["item_topics", "story_items"],
            id="two-tables",
        ),
    ],
)
def test_a_preexisting_fk_violation_fails_loudly_names_the_table_and_leaves_v4(
    v4_db, statements, tables
):
    # Documented (deploy note): the old tables get copied, so a violation that
    # was already there is still there afterwards, and the runner's
    # foreign_key_check refuses to commit. Fail-safe, but someone has to clean the
    # orphan rows by hand (delete them, or restore a good backup) and rerun.
    _break_with_fk_off(v4_db, *statements)
    with closing(connect(v4_db)) as conn:
        before = _state(conn)

    with closing(connect(v4_db)) as conn:
        with pytest.raises(StoreError) as excinfo:
            migrate(conn)
        message = str(excinfo.value)
        assert "005_public_app.sql" in message and "Rolled back" in message
        assert ", ".join(tables) in message
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.in_transaction

    with closing(connect(v4_db)) as conn:
        assert _state(conn) == before  # nothing half-applied, not even a stray *_new table


def test_after_the_orphan_rows_are_cleaned_up_the_same_file_migrates(v4_db):
    _break_with_fk_off(v4_db, "INSERT INTO story_items (story_id, item_id) VALUES (9999, 10)")
    with closing(connect(v4_db)) as conn:
        with pytest.raises(StoreError):
            migrate(conn)
        conn.execute("DELETE FROM story_items WHERE story_id = 9999")
        conn.commit()
        assert migrate(conn) == 8
        _assert_healthy(conn)


# --- a database with a stray object that shares a name with something 005 creates ---


@pytest.mark.parametrize(
    "stray_sql",
    [
        "CREATE TABLE guilds (x INTEGER)",
        "CREATE TABLE items_fts (x INTEGER)",
        "CREATE TABLE app_state (x INTEGER)",
        "CREATE TABLE game_summaries (x INTEGER)",
        "CREATE TABLE digests_new (x INTEGER)",
        "CREATE TABLE stories_new (x INTEGER)",
        "CREATE INDEX idx_item_topics_topic ON source_health (source_name)",
        "CREATE TRIGGER items_ai AFTER INSERT ON source_health BEGIN SELECT 1; END",
    ],
)
def test_a_stray_object_with_a_005_name_aborts_the_migration_and_survives(v4_db, stray_sql):
    # Documented (deploy note): 005 uses plain CREATE, not IF NOT EXISTS, on purpose:
    # silently adopting somebody's table of the same name is worse than stopping.
    with closing(connect(v4_db)) as conn:
        conn.execute(stray_sql)
        conn.commit()
        before = _state(conn)

    with closing(connect(v4_db)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            migrate(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with closing(connect(v4_db)) as conn:
        assert _state(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- size ---


def test_tens_of_thousands_of_rows_migrate_quickly_and_the_fts_rebuild_keeps_up(v4_db, v22):
    n = 30_000
    with closing(connect(v4_db)) as conn:
        with conn:
            conn.executemany(
                "INSERT INTO items (id, url, title, excerpt, source_name, trust, published_at, "
                "collected_at) VALUES (?, ?, ?, ?, 'Feed', 'press', NULL, ?)",
                [
                    (
                        1000 + i,
                        f"https://example.com/bulk/{i}",
                        f"Bulk headline {i} {'needle' if i % 1000 == 0 else 'hay'}",
                        f"Excerpt {i} about nothing",
                        "2026-09-29T10:00:00+00:00",
                    )
                    for i in range(n)
                ],
            )
            conn.executemany(
                "INSERT INTO item_topics (item_id, topic_key, uncertain) VALUES (?, 'bl4', 0)",
                [(1000 + i,) for i in range(n)],
            )
            conn.executemany(
                "INSERT INTO stories (id, topic_key, headline, summary, label, digest_id, "
                "created_at) VALUES (?, 'bl4', ?, 'Summary', 'reported', 7, ?)",
                [
                    (
                        5000 + i,
                        f"Story {i} {'gadget' if i % 1000 == 0 else 'filler'}",
                        "2026-09-29T10:00:00+00:00",
                    )
                    for i in range(n)
                ],
            )
            conn.executemany(
                "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)",
                [(5000 + i, 1000 + i) for i in range(n)],
            )

    started = time.monotonic()
    with closing(connect(v4_db)) as conn:
        assert migrate(conn) == 8
    elapsed = time.monotonic() - started
    assert elapsed < 60, f"migration took {elapsed:.1f}s for {n} items and {n} stories"

    with closing(connect(v4_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == n + 4
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == n + 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
        conn.execute("INSERT INTO stories_fts (stories_fts) VALUES ('integrity-check')")
        rows, total = v22.search_stories(conn, "gadget", _EPOCH, 5, 0)
        assert total == 30 and len(rows) == 5
        hits = conn.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'needle'")
        assert hits.fetchone()[0] == 30


# --- being killed mid-migration ---


def _statements(text: str) -> list[str]:
    """Split a migration file into statements (complete_statement knows about trigger bodies)."""
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


def test_an_exception_after_every_single_statement_leaves_v4_untouched(
    v4_db, tmp_path, monkeypatch, migrations_up_to
):
    text = (MIGRATIONS_DIR / "005_public_app.sql").read_text()
    statements = _statements(text)
    assert len(statements) > 30  # the splitter really did split
    header = text.splitlines()[0]
    with closing(connect(v4_db)) as conn:
        before = _state(conn)

    for k in range(1, len(statements) + 1):
        directory = migrations_up_to(tmp_path / f"cut{k}", 4)
        boom = "SELECT * FROM newsbot_boom;\n"
        broken = "".join(statements[:k]) + boom + "".join(statements[k:])  # noqa: S608
        (directory / "005_public_app.sql").write_text(header + "\n" + broken)
        work = tmp_path / f"copy{k}.db"
        shutil.copy(v4_db, work)
        with monkeypatch.context() as m:
            m.setattr(db, "_MIGRATIONS_DIR", directory)
            with closing(connect(work)) as conn:
                with pytest.raises(sqlite3.OperationalError, match="newsbot_boom"):
                    migrate(conn)
                assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert not conn.in_transaction
        with closing(connect(work)) as conn:
            assert _state(conn) == before, f"statement {k} leaked: {statements[k - 1][:60]!r}"


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


def _traced_statement_count(path) -> int:
    seen = [0]
    with closing(connect(path)) as conn:
        conn.set_trace_callback(lambda sql: seen.__setitem__(0, seen[0] + 1))
        migrate(conn)
    return seen[0]


def test_a_process_killed_at_any_point_leaves_a_database_that_still_migrates(v4_db, tmp_path):
    probe = tmp_path / "probe.db"
    shutil.copy(v4_db, probe)
    total = _traced_statement_count(probe)
    assert total > 40
    with closing(connect(v4_db)) as conn:
        before = _state(conn)

    points = sorted(
        {1, 2, 3, total // 4, total // 2, (3 * total) // 4, total - 2, total - 1, total}
    )
    rolled_back = 0
    for point in points:
        work = tmp_path / f"kill{point}.db"
        shutil.copy(v4_db, work)
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
            if version == 4:
                # Killed before the COMMIT: nothing at all is left behind.
                assert _state(conn) == before, f"kill at traced statement {point} left damage"
                rolled_back += 1
            elif version == 5:
                # 005 committed and the kill landed inside 006 (which has its own
                # transaction): 006 must have left nothing behind either.
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                assert not tables & {"code_sightings", "guild_code_followups"}, point
                rolled_back += 1
            elif version == 6:
                # The same for 007: its columns are all or nothing.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)")}
                assert not columns & {"items_after", "items_upto", "game_items_upto"}, point
                rolled_back += 1
            elif version == 7:
                # And 008, the items rebuild: the old table or the new one, never a
                # missing table or a leftover `items_new`.
                names = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                assert "items" in names and "items_new" not in names, point
                ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='items'").fetchone()
                assert "AUTOINCREMENT" not in ddl[0], point
                rolled_back += 1
            else:
                # The last traced statement is the pragma that runs after COMMIT, so a kill
                # there finds a finished migration, never a half-done one.
                assert version == 8 and point == total
            assert migrate(conn) == 8
            _assert_healthy(conn)
    # Every kill before a COMMIT lands is rolled back (the last traced statement
    # of 006 is its COMMIT, which a kill there pre-empts); only a kill after one
    # finds a finished migration.
    assert rolled_back >= len(points) - 1


# --- WAL: other connections while the migration runs ---


def test_a_reader_holding_a_wal_snapshot_neither_blocks_nor_is_corrupted_by_the_migration(v4_db):
    reader = connect(v4_db)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 4
        with closing(connect(v4_db)) as migrator:
            started = time.monotonic()
            assert migrate(migrator) == 8
            assert time.monotonic() - started < 4  # did not sit out the 5s busy timeout
        # Documented: the open read transaction keeps its pre-migration snapshot.
        assert reader.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 4
        old_world = {r[0] for r in reader.execute("SELECT name FROM sqlite_master")}
        assert "guilds" not in old_world
        reader.execute("COMMIT")
        assert reader.execute("SELECT COUNT(*) FROM guilds").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 4
        _assert_healthy(reader)
    finally:
        reader.close()


def test_a_writer_holding_the_lock_makes_the_migration_wait_and_then_keeps_its_row(v4_db):
    ready, release = threading.Event(), threading.Event()

    def writer():
        with closing(connect(v4_db)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO items (id, url, title, excerpt, source_name, trust, collected_at) "
                "VALUES (900, 'https://example.com/late', 'Zebracorn sighting', 'x', 'Feed', "
                "'press', '2026-09-30T00:00:00+00:00')"
            )
            ready.set()
            release.wait(timeout=10)
            conn.commit()

    thread = threading.Thread(target=writer)
    thread.start()
    assert ready.wait(timeout=10)
    threading.Timer(0.4, release.set).start()
    started = time.monotonic()
    with closing(connect(v4_db)) as conn:
        assert migrate(conn) == 8
    assert time.monotonic() - started >= 0.3  # it really did wait for the writer
    thread.join(timeout=10)

    with closing(connect(v4_db)) as conn:
        # The row written before the migration is in the FTS rebuild, not lost to it.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'zebracorn'"
            ).fetchone()[0]
            == 1
        )
        _assert_healthy(conn)


def test_six_openers_with_a_writer_active_all_reach_the_latest_version_and_the_writers_rows_survive(
    v4_db,
):
    errors: list[BaseException] = []
    versions: list[int] = []
    stop = threading.Event()
    written: list[int] = []
    barrier = threading.Barrier(7)

    def writer():
        try:
            with closing(connect(v4_db)) as conn:
                barrier.wait(timeout=10)
                i = 0
                while not stop.is_set() and i < 60:
                    with conn:
                        conn.execute(
                            "INSERT INTO source_health (source_name, consecutive_failures) "
                            "VALUES (?, 0)",
                            (f"w{i}",),
                        )
                    written.append(i)
                    i += 1
                    time.sleep(0.005)
        except BaseException as exc:
            errors.append(exc)

    def opener():
        try:
            with closing(connect(v4_db)) as conn:
                barrier.wait(timeout=10)
                versions.append(migrate(conn))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=opener) for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads[1:]:
        t.join(timeout=60)
    stop.set()
    threads[0].join(timeout=60)

    assert errors == []
    assert versions == [8] * 6
    with closing(connect(v4_db)) as conn:
        names = conn.execute(
            "SELECT COUNT(*) FROM source_health WHERE source_name LIKE 'w%'"
        ).fetchone()[0]
        assert names == len(written) and names > 0
        _assert_healthy(conn)


def test_a_migration_that_cannot_get_the_lock_restores_the_connection_and_stays_at_v4(v4_db):
    holder = connect(v4_db)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with closing(connect(v4_db)) as conn:
            conn.execute("PRAGMA busy_timeout = 50")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                migrate(conn)
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert not conn.in_transaction
            assert conn.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
    finally:
        holder.rollback()
        holder.close()
    with closing(connect(v4_db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert migrate(conn) == 8


def test_a_connection_that_had_foreign_keys_off_gets_them_left_off(v4_db):
    # The runner puts the pragma back the way it found it; it doesn't "helpfully" turn it on.
    conn = sqlite3.connect(v4_db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert migrate(conn) == 8
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


# --- what the rebuild does to everything that pointed at the rebuilt tables ---


def test_no_index_or_trigger_from_v4_is_lost_and_no_scratch_name_leaks(v4_db):
    with closing(connect(v4_db)) as conn:
        old = {(r["type"], r["name"]) for r in conn.execute("SELECT type, name FROM sqlite_master")}
        migrate(conn)
        new_rows = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    new = {(r["type"], r["name"]) for r in new_rows}
    lost = {(t, n) for t, n in old - new if not n.startswith("sqlite_autoindex")}
    assert lost == set()
    for row in new_rows:
        sql = row["sql"] or ""
        assert "digests_new" not in sql and "stories_new" not in sql, row["name"]


def test_child_tables_still_point_at_the_rebuilt_parents_and_cascade(v22_db):
    with closing(connect(v22_db)) as conn:
        parents = {r["table"] for r in conn.execute("PRAGMA foreign_key_list(story_items)")}
        assert parents == {"stories", "items"}
        # A story's links go with it; a story that was an update of it loses the pointer.
        conn.execute("DELETE FROM stories WHERE id = 100")
        conn.commit()
        assert (
            conn.execute("SELECT COUNT(*) FROM story_items WHERE story_id = 100").fetchone()[0] == 0
        )
        assert conn.execute("SELECT is_update_of FROM stories WHERE id = 102").fetchone()[0] is None
        _assert_healthy(conn)


def test_new_ids_continue_after_the_old_maximum(v22_db, v22):
    with closing(connect(v22_db)) as conn:
        digest_id = v22.claim_digest(conn, date(2026, 10, 5), force=False)
        assert digest_id == 13
        with conn:
            cur = conn.execute(
                "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
                "VALUES ('bl4', 'h', 's', 'official', 13, 'now')"
            )
        assert cur.lastrowid == 105


# --- a v2.2 process that was already connected when the migration happened ---


def test_a_long_lived_v22_connection_keeps_working_after_another_process_migrates(v4_db, v22):
    old_process = connect(v4_db)
    try:
        # Warm its statement cache and schema against the v4 layout.
        assert v22.get_digest(old_process, date(2026, 9, 30)).id == 12
        assert (
            v22.query_stories(old_process, [], datetime(2020, 1, 1, tzinfo=UTC), None, 10, 0)[1]
            == 5
        )
        with closing(connect(v4_db)) as other:
            assert migrate(other) == 8
        # sqlite re-prepares on a schema change; the old connection just carries on.
        assert v22.get_digest(old_process, date(2026, 9, 30)).id == 12
        digest_id = v22.claim_digest(old_process, date(2026, 10, 1), force=False)
        assert digest_id == 13
        assert (
            v22.query_stories(old_process, [], datetime(2020, 1, 1, tzinfo=UTC), None, 10, 0)[1]
            == 5
        )
        assert (
            old_process.execute("SELECT guild_id FROM digests WHERE id = 13").fetchone()[0] is None
        )
        assert repo.get_guild(old_process, 1) is None  # and v3 code sees the new tables too
    finally:
        old_process.close()
