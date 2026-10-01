"""Tests for migration 004: the lounge tables (plan task 2, design.md §14).

Companion to test_db.py's runner coverage; this file is about what 004
creates and what it leaves alone, not the runner machinery itself.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate

MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"
HASH = "a" * 64


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "newsbot.db"


def _build_v3(conn):
    with conn:
        for name in ("001_initial.sql", "002_shift_alerts.sql", "003_code_roundup_flag.sql"):
            conn.executescript((MIGRATIONS_DIR / name).read_text())
        conn.execute("PRAGMA user_version = 3")


def test_fresh_db_reaches_user_version_four(db_path):
    with closing(connect(db_path)) as conn:
        assert migrate(conn) == 6
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_lounge_tables_exist(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"lounge_quotes_used", "lounge_state"} <= names


@pytest.mark.parametrize("bad_hash", ["", "a" * 63, "a" * 65])
def test_quote_hash_must_be_64_characters(db_path, bad_hash):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) "
                    "VALUES ('wikiquote:Oscar Wilde', ?, 'now')",
                    (bad_hash,),
                )


def test_empty_source_key_is_rejected(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) "
                    "VALUES ('', ?, 'now')",
                    (HASH,),
                )


def test_migrate_is_idempotent_and_keeps_lounge_rows(db_path):
    with closing(connect(db_path)) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) "
            "VALUES ('wikiquote:Oscar Wilde', ?, 'now')",
            (HASH,),
        )
        conn.commit()
        assert migrate(conn) == migrate(conn) == 6
        assert conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 1


def test_002_and_003_data_survive_the_upgrade_from_3_to_4(db_path):
    with closing(connect(db_path)) as conn:
        _build_v3(conn)
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup) "
            "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'roundup', 1)"
        )
        conn.execute("INSERT INTO alert_state (key, value) VALUES ('ping_count', '2')")
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES ('2026-09-20', 'ok', 'now', 'now')"
        )
        conn.commit()

        assert migrate(conn) == 6

        row = conn.execute("SELECT status, from_roundup FROM alerted_codes").fetchone()
        assert (row["status"], row["from_roundup"]) == ("roundup", 1)
        assert conn.execute("SELECT value FROM alert_state").fetchone()["value"] == "2"
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM lounge_state").fetchone()[0] == 0


def test_v3_shaped_workflow_still_works_on_a_v4_database(db_path):
    # Rollback proof (design.md §14): a v2.1.1 process only ever touches
    # digests and alerted_codes, and has to keep working on a v2.2 database
    # so going back to TAG=2.1.1 needs no restore.
    with closing(connect(db_path)) as conn:
        migrate(conn)
        now = lambda: datetime(2026, 9, 29, 12, 0, tzinfo=UTC)  # noqa: E731
        digest_id = repo.claim_digest(conn, date(2026, 9, 29), force=False, now=now)
        assert digest_id is not None
        pinged = repo.claim_codes(
            conn,
            [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
            pinged=True,
            local_day="2026-09-29",
            now=now,
        )
        assert pinged is True
        assert repo.get_alert_state(conn).ping_count == 1
        assert repo.known_codes(conn, ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]) == {
            "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
        }
