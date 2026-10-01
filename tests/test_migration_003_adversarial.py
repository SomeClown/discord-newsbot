"""Adversarial tests for migration 003 (`alerted_codes.from_roundup`), beyond
test_migration_003_roundup_flag.py's own backfill/CHECK/rollback coverage.

Angle (test-engineer brief, 2026-09-26): a v2 database that already has
every status `alerted_codes` can hold (not just `roundup` and `posted`),
and the specific rollback shape that matters in practice -- a v1.3.0
process, which only knows about migrations 001/002, opening a database
that's already at `user_version = 4`. `db.migrate()` picks up migration
files from a fixed directory, so "v1.3.0's migrate()" is simulated here by
monkeypatching that directory to hold only 001/002, the same way
test_migration_003_roundup_flag.py's own rollback-proof test simulates a
v1.3.0 *insert* rather than a v1.3.0 *migrate*.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from newsbot.store import db
from newsbot.store.db import connect, migrate

MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"

ALL_STATUSES = ["pending", "posted", "failed", "seeded", "too_old", "roundup"]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "newsbot.db"


def test_v2_db_with_every_known_status_migrates_cleanly(db_path):
    # Every status alerted_codes can hold, inserted before 003 runs (a v1.2
    # database that saw a full range of sweep outcomes), not just the
    # 'roundup'/'posted' pair the implementer's own test covers.
    m1 = (MIGRATIONS_DIR / "001_initial.sql").read_text()
    m2 = (MIGRATIONS_DIR / "002_shift_alerts.sql").read_text()
    with closing(connect(db_path)) as conn:
        with conn:
            conn.executescript(m1)
            conn.executescript(m2)
            conn.execute("PRAGMA user_version = 2")
        for i, status in enumerate(ALL_STATUSES):
            code = f"{chr(65 + i) * 4}{i}-AAAAA-AAAAA-AAAAA-AAAAA"
            conn.execute(
                "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
                "VALUES (?, 'now', 'Src', 'https://e/x', ?)",
                (code, status),
            )
        conn.commit()

        version = migrate(conn)
        assert version == 7

        rows = {
            row["code"]: (row["status"], row["from_roundup"])
            for row in conn.execute("SELECT code, status, from_roundup FROM alerted_codes")
        }
        assert len(rows) == len(ALL_STATUSES)
        for i, status in enumerate(ALL_STATUSES):
            code = f"{chr(65 + i) * 4}{i}-AAAAA-AAAAA-AAAAA-AAAAA"
            expected_flag = 1 if status == "roundup" else 0
            assert rows[code] == (status, expected_flag), (
                f"status {status!r} backfilled from_roundup wrong"
            )


def test_migrate_twice_on_the_populated_v2_db_is_still_a_noop(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        # from_roundup is stamped explicitly here (as record_silent_codes and
        # claim_codes always do post-v2.0): migration 003's own backfill
        # UPDATE only ever runs once, at migration time, not on later inserts.
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'roundup', 1)"
        )
        conn.commit()
        first = migrate(conn)
        second = migrate(conn)
    assert first == second == 7
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT from_roundup FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
        ).fetchone()
        assert row["from_roundup"] == 1


def test_v1_3_0_migrate_against_a_newer_db_is_a_noop_and_keeps_its_version(db_path, monkeypatch):
    # Build a real v2.0 database first (migrations 001-003 applied), then
    # simulate a v1.3.0 process's migrate() (which only ever globs
    # 001/002 out of its own migrations/ directory) running against it.
    # No migration file it knows about has a version > 3, so `migrate()`'s
    # own "skip anything <= current" loop leaves user_version at 3 and
    # touches nothing, which is exactly what makes downgrading TAG back to
    # 1.3.0 safe without a DB restore (design.md §13, plan §8 rollback).
    with closing(connect(db_path)) as conn:
        migrate(conn)
        # from_roundup stamped explicitly, same reasoning as the
        # migrate-twice test above.
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'roundup', 1)"
        )
        conn.commit()

    v1_3_migrations_dir = db_path.parent / "v1_3_migrations"
    v1_3_migrations_dir.mkdir()
    (v1_3_migrations_dir / "001_initial.sql").write_text(
        (MIGRATIONS_DIR / "001_initial.sql").read_text()
    )
    (v1_3_migrations_dir / "002_shift_alerts.sql").write_text(
        (MIGRATIONS_DIR / "002_shift_alerts.sql").read_text()
    )
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", v1_3_migrations_dir)

    with closing(connect(db_path)) as conn:
        version = migrate(conn)
        assert version == 7
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        row = conn.execute(
            "SELECT status, from_roundup FROM alerted_codes "
            "WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
        ).fetchone()
        # from_roundup still exists and holds its value: v1.3.0's migrate()
        # never touches it, but it also never has to know it's there.
        assert row["status"] == "roundup"
        assert row["from_roundup"] == 1


def test_check_constraint_rejects_from_roundup_two_via_a_direct_update(db_path):
    # Companion to test_migration_003_roundup_flag.py's insert-side CHECK
    # test: pinning that the same CHECK also fires on UPDATE, not just
    # INSERT (a plausible second write path if anything ever "corrects" the
    # flag after the fact).
    with closing(connect(db_path)) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'seeded')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "UPDATE alerted_codes SET from_roundup = 2 "
                    "WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
                )
