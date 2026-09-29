"""Adversarial tests for migration 004 (the lounge tables), beyond
test_migration_004_lounge.py's own schema, CHECK and 3-to-4 coverage.

Angle (test-engineer brief, 2026-09-29): what happens when 004 meets a
database that's messier than the tidy one it was written against. A v3
database with rows in every table; a `lounge_state` table that somehow
already exists (documented, not redesigned); a handful of processes opening
a v3 file at once; a v2.1.1 process, which only knows migrations up to 003,
opening a v4 file; and the nightly backup, which is sqlite's backup API and
should carry the new tables along without being asked. Where the behavior
is surprising I've pinned it and said so, because "surprising but
consistent" is a fine thing to write down and a lousy thing to discover at
3 a.m.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsbot.store import db, repo
from newsbot.store.db import connect, migrate

MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"
H1, H2 = "1" * 64, "2" * 64


def _dir_up_to(tmp_path: Path, version: int) -> Path:
    """A migrations directory holding only files numbered <= `version`."""
    target = tmp_path / f"migrations_up_to_{version}"
    target.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if int(path.name.split("_", 1)[0]) <= version:
            (target / path.name).write_text(path.read_text())
    return target


@pytest.fixture
def v3_path(tmp_path, monkeypatch):
    """A v3 database on disk with a row in every table 001-003 created."""
    path = tmp_path / "newsbot.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", _dir_up_to(tmp_path, 3))
        with closing(connect(path)) as conn:
            assert migrate(conn) == 3
            conn.execute(
                "INSERT INTO digests (run_date, status, created_at, updated_at) "
                "VALUES ('2026-09-01', 'ok', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status, from_roundup) "
                "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'posted', 0)"
            )
            conn.execute("INSERT INTO alert_state (key, value) VALUES ('seeded_at', 'now')")
            conn.commit()
    return path


def _counts(conn, tables):
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (fixed names)
        for t in tables
    }


def test_v3_with_rows_everywhere_upgrades_and_starts_with_empty_lounge_tables(v3_path):
    tables = ["digests", "alerted_codes", "alert_state"]
    with closing(connect(v3_path)) as conn:
        before = _counts(conn, tables)
        assert migrate(conn) == 4
        assert _counts(conn, tables) == before
        assert _counts(conn, ["lounge_quotes_used", "lounge_state"]) == {
            "lounge_quotes_used": 0,
            "lounge_state": 0,
        }
        assert repo.get_lounge_state(conn).last_quote_date is None


def test_an_existing_lounge_state_table_makes_004_fail_loudly_and_leave_v3_at_v3(v3_path):
    # Documented, not redesigned: 004 uses plain CREATE TABLE, so a stray
    # `lounge_state` (say, a hand-made one) stops the migration. The
    # database must not claim to be v4 afterwards, and the stray table's
    # contents must survive.
    with closing(connect(v3_path)) as conn:
        conn.execute("CREATE TABLE lounge_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO lounge_state VALUES ('last_quote_date', 'stray')")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert repo.get_lounge_state(conn).last_quote_date == "stray"


def test_a_failed_004_never_leaves_a_half_built_v4_that_reports_success(v3_path):
    # Same stray-table setup. executescript isn't transactional for DDL, so
    # `lounge_quotes_used` may or may not be left behind; what matters is
    # that a retry after the owner drops the stray table gets to v4.
    with closing(connect(v3_path)) as conn:
        conn.execute("CREATE TABLE lounge_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn)
        conn.execute("DROP TABLE lounge_state")
        conn.execute("DROP TABLE IF EXISTS lounge_quotes_used")
        conn.commit()
        assert migrate(conn) == 4
        assert repo.claim_quote(
            conn,
            source_key="s",
            quote_hash=H1,
            local_day="2026-09-29",
            reshuffle=False,
            force=False,
        )


def test_several_processes_opening_a_v3_file_at_once_all_end_up_on_v4(v3_path):
    barrier = threading.Barrier(6)
    versions: list[int] = []
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            with closing(connect(v3_path)) as conn:
                barrier.wait(timeout=5)
                versions.append(migrate(conn))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert versions == [4] * 6
    with closing(connect(v3_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_v2_1_1_opening_a_v4_database_neither_fails_nor_touches_lounge_rows(tmp_path, monkeypatch):
    path = tmp_path / "newsbot.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.claim_quote(
            conn,
            source_key="wikiquote:Oscar Wilde",
            quote_hash=H1,
            local_day="2026-09-29",
            reshuffle=False,
            force=False,
            now=lambda: datetime(2026, 9, 29, tzinfo=UTC),
        )
        before_rows = conn.execute("SELECT * FROM lounge_quotes_used").fetchall()
        before_state = conn.execute("SELECT * FROM lounge_state").fetchall()
        before_rows = [tuple(r) for r in before_rows]
        before_state = [tuple(r) for r in before_state]

    monkeypatch.setattr(db, "_MIGRATIONS_DIR", _dir_up_to(tmp_path, 3))
    with closing(connect(path)) as conn:
        assert migrate(conn) == 4  # returns the DB's version; it applies nothing
        # v2.1.1's normal day: claim a digest, purge, read status. None of it
        # may disturb the lounge tables.
        repo.claim_digest(conn, datetime(2026, 9, 30, tzinfo=UTC).date(), force=False)
        repo.purge_older_than(conn, datetime(2100, 1, 1, tzinfo=UTC))
        assert [tuple(r) for r in conn.execute("SELECT * FROM lounge_quotes_used")] == before_rows
        assert [tuple(r) for r in conn.execute("SELECT * FROM lounge_state")] == before_state


def test_purge_ignores_a_lounge_row_with_an_ancient_used_at(tmp_path):
    path = tmp_path / "newsbot.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO lounge_quotes_used VALUES ('s', ?, '1970-01-01T00:00:00+00:00')", (H1,)
        )
        conn.commit()
        repo.purge_older_than(conn, datetime(2100, 1, 1, tzinfo=UTC))
        assert conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 1


def test_backup_api_round_trip_carries_the_lounge_tables_and_version(tmp_path):
    # scripts/backup.sh uses sqlite's `.backup`; Python's Connection.backup
    # is the same online-backup API, so this is the honest stand-in.
    src_path = tmp_path / "live.db"
    dst_path = tmp_path / "backup.db"
    with closing(connect(src_path)) as src:
        migrate(src)
        repo.claim_quote(
            src,
            source_key="file:/srv/quotes.txt",
            quote_hash=H2,
            local_day="2026-09-29",
            reshuffle=False,
            force=False,
            now=lambda: datetime(2026, 9, 29, 15, tzinfo=UTC),
        )
        with closing(sqlite3.connect(dst_path)) as dst:
            src.backup(dst)

    with closing(connect(dst_path)) as restored:
        assert restored.execute("PRAGMA user_version").fetchone()[0] == 4
        assert migrate(restored) == 4
        assert repo.get_lounge_state(restored).last_quote_date == "2026-09-29"
        deck = repo.quote_deck_state(restored, "file:/srv/quotes.txt")
        assert deck.used == frozenset({H2})
        assert deck.last_hash == H2
        # And the restored copy still enforces the schema's CHECKs.
        with pytest.raises(sqlite3.IntegrityError):
            restored.execute("INSERT INTO lounge_quotes_used VALUES ('s', 'short', 'now')")
