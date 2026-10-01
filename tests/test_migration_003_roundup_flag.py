"""Tests for migration 003: `alerted_codes.from_roundup` (plan step 2, design.md §13).

Companion to test_db.py's own migration-runner coverage; this file is
specifically about what 003 does to `alerted_codes` data, not the runner
machinery itself.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from newsbot.store.db import connect, migrate

MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "newsbot.db"


def test_fresh_db_reaches_user_version_four(db_path):
    with closing(connect(db_path)) as conn:
        version = migrate(conn)
    assert version == 7
    with closing(connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7


def test_from_roundup_column_exists_and_defaults_to_zero(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'seeded')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT from_roundup FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
        ).fetchone()
        assert row["from_roundup"] == 0


def test_from_roundup_rejects_values_outside_zero_or_one(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO alerted_codes "
                    "(code, first_seen_at, source_name, item_url, status, from_roundup) "
                    "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', "
                    "'seeded', 2)"
                )


def test_existing_roundup_rows_are_backfilled_to_from_roundup_true(db_path):
    # Simulates a v1.2/v1.3 database (only migration 002 applied) that
    # already has 'roundup'-status rows from before this column existed --
    # 003 has to mark those retroactively, not just new inserts.
    m1 = (MIGRATIONS_DIR / "001_initial.sql").read_text()
    m2 = (MIGRATIONS_DIR / "002_shift_alerts.sql").read_text()
    with closing(connect(db_path)) as conn:
        with conn:
            conn.executescript(m1)
            conn.executescript(m2)
            conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'roundup')"
        )
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES ('BBBB2-BBBBB-BBBBB-BBBBB-BBBBB', 'now', 'Src', 'https://e/b', 'posted')"
        )
        conn.commit()

        version = migrate(conn)
        assert version == 7

        rows = {
            row["code"]: row["from_roundup"]
            for row in conn.execute("SELECT code, from_roundup FROM alerted_codes")
        }
        assert rows == {
            "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA": 1,
            "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB": 0,
        }


def test_v1_3_shaped_insert_omitting_the_column_still_works(db_path):
    # Rollback proof (design.md §13): a v1.3.0 process running against a
    # v2.0 database never mentions from_roundup at all: its INSERT
    # statement predates the column entirely. This has to keep working so
    # a rollback (restore config.v1.yaml + TAG=1.3.0) doesn't also need a
    # DB restore.
    with closing(connect(db_path)) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'posted')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, from_roundup FROM alerted_codes "
            "WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
        ).fetchone()
        assert row["status"] == "posted"
        assert row["from_roundup"] == 0
