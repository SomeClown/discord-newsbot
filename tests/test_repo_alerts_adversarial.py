"""Adversarial tests for the SHiFT alert repo functions, beyond
test_repo_alerts.py's own coverage.

Focus (test-engineer brief, 2026-09-25): what could cause a false or
duplicate @everyone ping, or a missed code -- an empty claim batch, the
ping-day boundary treated as an exact string rather than a fuzzy "close
enough" comparison, two connections racing to claim the same code,
fail_pending_codes called more than once, alert_status's counts by
status, and migration 002 landing on a real, foreign-keys-on v1 database
that already has data and relationships in it.
"""

from __future__ import annotations

import sqlite3
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


# --- claim_codes with an empty list ---


def test_claim_codes_empty_list_writes_no_rows(conn):
    repo.claim_codes(conn, [], pinged=False, local_day="2026-09-25", now=_now)
    assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


def test_claim_codes_empty_list_does_not_spend_a_ping_even_when_pinged_true(conn):
    # No longer pinning the old behavior: an empty `codes` now returns
    # before touching `alert_state` at all (plan step 8's process_items
    # is supposed to make "pinged=True with nothing to claim" impossible
    # in the first place, via `ping = bool(to_post) and ...`, but this
    # layer guards it too rather than trusting every future caller to get
    # that right).
    repo.claim_codes(conn, [], pinged=True, local_day="2026-09-25", now=_now)
    state = repo.get_alert_state(conn)
    assert state.ping_count == 0
    assert state.ping_day is None
    assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


# --- ping_count / local_day: exact string match, not a fuzzy day comparison ---


def test_ping_day_boundary_is_exact_string_equality_not_calendar_adjacency(conn):
    # local_day is an opaque caller-supplied string (cfg.digest.timezone's
    # local date, per A8) -- claim_codes has no zoneinfo or date math of
    # its own. A single-character difference has to be treated as a full
    # day boundary crossing (reset), never as "close enough, keep
    # counting" -- which is exactly what a real local-midnight rollover
    # looks like from this layer's point of view.
    repo.claim_codes(
        conn,
        [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.claim_codes(
        conn,
        [("BBBBB-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b")],
        pinged=True,
        local_day="2026-09-26",  # the very next local day, one calendar day later
        now=_now,
    )
    state = repo.get_alert_state(conn)
    assert state.ping_day == "2026-09-26"
    assert state.ping_count == 1  # reset, not accumulated to 2


def test_three_claims_across_a_local_midnight_rollover_reset_exactly_once(conn):
    # 23:59 local day D, 00:00 local day D+1, then another later on D+1:
    # the count should track only the current local_day's claims.
    repo.claim_codes(
        conn,
        [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.claim_codes(
        conn,
        [("BBBBB-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    assert repo.get_alert_state(conn).ping_count == 2

    repo.claim_codes(
        conn,
        [("CCCCC-CCCCC-CCCCC-CCCCC-CCCCC", "Src", "https://e/c")],
        pinged=True,
        local_day="2026-09-26",
        now=_now,
    )
    assert repo.get_alert_state(conn).ping_count == 1

    repo.claim_codes(
        conn,
        [("DDDDD-DDDDD-DDDDD-DDDDD-DDDDD", "Src", "https://e/d")],
        pinged=True,
        local_day="2026-09-26",
        now=_now,
    )
    assert repo.get_alert_state(conn).ping_count == 2


# --- concurrent claim from two connections of the same code ---


def test_concurrent_claim_of_same_code_from_two_connections_fails_cleanly(tmp_path):
    db_path = tmp_path / "newsbot.db"
    with closing(connect(db_path)) as setup_conn:
        migrate(setup_conn)

    conn1 = connect(db_path)
    conn2 = connect(db_path)
    try:
        repo.claim_codes(
            conn1,
            [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
            pinged=True,
            local_day="2026-09-25",
            now=_now,
        )
        # A second connection racing to claim the exact same code hits the
        # PRIMARY KEY constraint and rolls back cleanly -- it never sees,
        # let alone spends, the ping budget.
        with pytest.raises(sqlite3.IntegrityError):
            repo.claim_codes(
                conn2,
                [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
                pinged=True,
                local_day="2026-09-25",
                now=_now,
            )
        # The first claim's ping spend and pending row must still stand,
        # untouched by the second connection's failed attempt.
        state = repo.get_alert_state(conn1)
        assert state.ping_count == 1
        row = conn1.execute(
            "SELECT status FROM alerted_codes WHERE code = 'AAAAA-AAAAA-AAAAA-AAAAA-AAAAA'"
        ).fetchone()
        assert row["status"] == "pending"
    finally:
        conn1.close()
        conn2.close()


# --- fail_pending_codes is idempotent ---


def test_fail_pending_codes_called_twice_second_call_flips_nothing(conn):
    repo.claim_codes(
        conn,
        [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
    )
    first = repo.fail_pending_codes(conn)
    assert first == ["AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"]

    second = repo.fail_pending_codes(conn)
    assert second == []

    row = conn.execute(
        "SELECT status FROM alerted_codes WHERE code = 'AAAAA-AAAAA-AAAAA-AAAAA-AAAAA'"
    ).fetchone()
    assert row["status"] == "failed"  # not reverted or touched again


# --- alert_status counts by status ---


def test_alert_status_counts_only_posted_not_pending_or_failed(conn):
    repo.claim_codes(
        conn,
        [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.mark_codes_posted(conn, ["AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"], message_id=1)

    repo.claim_codes(
        conn,
        [("BBBBB-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
    )
    repo.mark_codes_failed(conn, ["BBBBB-BBBBB-BBBBB-BBBBB-BBBBB"])

    repo.claim_codes(
        conn,
        [("CCCCC-CCCCC-CCCCC-CCCCC-CCCCC", "Src", "https://e/c")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
    )
    # CCCCC is left "pending" -- an interrupted claim that never got to
    # mark_codes_posted/failed.

    repo.record_silent_codes(
        conn,
        [("DDDDD-DDDDD-DDDDD-DDDDD-DDDDD", "Src", "https://e/d", "too_old")],
        now=_now,
        mark_seeded=True,
    )

    status = repo.alert_status(conn, "2026-09-25", enabled=True, max_pings=3)
    assert status.codes_alerted == 1  # only the posted one


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
        assert version == 2
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
        # item going away -- only the join table row is.
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1

        # New tables are present and empty, and honor their own CHECK
        # constraints against real data, not just an empty schema.
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 0
        repo.claim_codes(
            conn,
            [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
            pinged=True,
            local_day="2026-09-25",
            now=_now,
        )
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
