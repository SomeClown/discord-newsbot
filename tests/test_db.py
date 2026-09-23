"""Tests for newsbot.store.db: connection setup and the migration runner."""

import sqlite3
from contextlib import closing

import pytest

from newsbot.store.db import StoreError, assert_fts5, connect, migrate


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "newsbot.db"


def test_migrate_fresh_db_reaches_version_one(db_path):
    with closing(connect(db_path)) as conn:
        version = migrate(conn)
    assert version == 1
    with closing(connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migrate_twice_is_a_noop(db_path):
    with closing(connect(db_path)) as conn:
        first = migrate(conn)
    with closing(connect(db_path)) as conn:
        second = migrate(conn)
    assert first == second == 1


def test_connect_enables_wal_and_foreign_keys(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_sets_busy_timeout_and_row_factory(db_path):
    with closing(connect(db_path)) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.row_factory is sqlite3.Row


def test_fts_triggers_keep_search_in_sync(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES ('2026-09-23', 'ok', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES ('palworld', 'Big Patch Notes', 'A patch dropped.', 'official', 1, 'now')"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM stories_fts WHERE stories_fts MATCH 'patch'"
        ).fetchall()
        assert len(rows) == 1

        conn.execute("DELETE FROM stories WHERE id = 1")
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM stories_fts WHERE stories_fts MATCH 'patch'"
        ).fetchall()
        assert len(rows) == 0


def test_assert_fts5_passes_locally(db_path):
    with closing(connect(db_path)) as conn:
        assert_fts5(conn)  # should not raise


def test_assert_fts5_raises_storeerror_when_missing():
    # We can't easily build a real SQLite without FTS5 compiled in (both
    # Homebrew's and Debian's builds have it), so this stands in a fake
    # connection that raises the way a real one would if it didn't.
    class NoFts5Connection:
        def execute(self, sql, *args, **kwargs):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            raise AssertionError("unexpected query")

    with pytest.raises(StoreError):
        assert_fts5(NoFts5Connection())
