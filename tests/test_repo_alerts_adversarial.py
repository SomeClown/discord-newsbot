"""Adversarial tests for the SHiFT alert repo functions, beyond
test_repo_alerts.py's own coverage.

Focus (test-engineer brief, 2026-09-25): what could cause a false or
duplicate @everyone ping, or a missed code: fail_pending_codes called more
than once, and migration 002 landing on a real, foreign-keys-on v1 database
that already has data and relationships in it. (The claim and ping-budget
tests that used to live here went with v2's `claim_codes`.)
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate

MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _now():
    return NOW


def _insert_pending_code(conn, *, pinged=0):
    """One `pending` code, written by hand (v2's `claim_codes` that made them is gone)."""
    with conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, pinged, "
            "status) VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', ?, 'Src', 'https://e/a', ?, "
            "'pending')",
            (NOW.isoformat(), pinged),
        )


# --- fail_pending_codes is idempotent ---


def test_fail_pending_codes_called_twice_second_call_flips_nothing(conn):
    _insert_pending_code(conn)
    first = repo.fail_pending_codes(conn)
    assert first == ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]

    second = repo.fail_pending_codes(conn)
    assert second == []

    row = conn.execute(
        "SELECT status FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
    ).fetchone()
    assert row["status"] == "failed"  # not reverted or touched again


# --- migration 002 on a real v1 schema, with data and relationships, FK on ---


def test_migration_002_applies_on_real_v1_schema_with_related_data_and_fk_on(tmp_path):
    db_path = tmp_path / "newsbot.db"
    v1_sql = (MIGRATIONS_DIR / "001_initial.sql").read_text()

    with closing(connect(db_path)) as conn:
        # connect() already turns PRAGMA foreign_keys on; confirm it stuck
        # before relying on it below.
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        with conn:
            conn.executescript(v1_sql)
            conn.execute("PRAGMA user_version = 1")

        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES ('2026-09-20', 'ok', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://e/a', 't', 'e', 's', 'official', 'now')"
        )
        conn.execute("INSERT INTO item_topics (item_id, topic_key) VALUES (1, 'palworld')")
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES ('palworld', 'h', 's', 'official', 1, 'now')"
        )
        conn.execute("INSERT INTO story_items (story_id, item_id) VALUES (1, 1)")
        conn.commit()

        version = migrate(conn)
        assert version == 8
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        # Data survived the upgrade.
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0] == 1

        # Foreign key enforcement is still live post-upgrade: cascade
        # deletes fire the same way they did before migration 002 touched
        # anything (002 only adds new, unrelated tables).
        conn.execute("DELETE FROM items WHERE id = 1")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0] == 0
        # is_update_of / stories themselves aren't cascade-deleted by an
        # item going away: only the join table row is.
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1

        # New tables are present and empty, and honor their own CHECK
        # constraints against real data, not just an empty schema.
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 0
        _insert_pending_code(conn, pinged=1)
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
