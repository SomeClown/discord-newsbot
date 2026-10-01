"""Tests for the SHiFT code alert tables' repo functions (design.md §12, plan step 2).

Everything here talks to a fresh temp DB through the same repo module the
rest of the store's tests use -- no fixture reaches into `data/dev.db`,
ever.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _now():
    return NOW


# --- get_alert_state ---


def test_get_alert_state_on_empty_db_is_all_falsy_defaults(conn):
    state = repo.get_alert_state(conn)
    assert state.seeded is False
    assert state.last_sweep_at is None
    assert state.last_sweep_summary is None
    assert state.ping_day is None
    assert state.ping_count == 0


# --- known_codes ---


def test_known_codes_returns_only_recorded_subset(conn):
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded", False)],
        now=_now,
        mark_seeded=True,
    )
    known = repo.known_codes(
        conn, ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"]
    )
    assert known == {"AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"}


def test_known_codes_chunks_past_sqlite_variable_limit(conn):
    codes = [f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(1200)]
    rows = [(c, "Src", "https://e/x", "seeded", False) for c in codes]
    repo.record_silent_codes(conn, rows, now=_now, mark_seeded=False)
    assert repo.known_codes(conn, codes) == set(codes)


# --- record_silent_codes ---


def test_record_silent_codes_sets_seeded_marker_when_requested(conn):
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded", False)],
        now=_now,
        mark_seeded=True,
    )
    assert repo.get_alert_state(conn).seeded is True


def test_record_silent_codes_does_not_set_marker_when_unhealthy(conn):
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded", False)],
        now=_now,
        mark_seeded=False,
    )
    assert repo.get_alert_state(conn).seeded is False


def test_record_silent_codes_does_not_reset_marker_once_set(conn):
    repo.record_silent_codes(conn, [], now=_now, mark_seeded=True)
    first_state = repo.get_alert_state(conn)
    assert first_state.seeded is True

    later = datetime(2026, 9, 26, tzinfo=UTC)
    repo.record_silent_codes(conn, [], now=lambda: later, mark_seeded=True)
    row = conn.execute("SELECT value FROM alert_state WHERE key = 'seeded_at'").fetchone()
    assert row["value"] == NOW.isoformat()  # unchanged by the second call


def test_record_silent_codes_conflict_keeps_first_recorded_status(conn):
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded", False)],
        now=_now,
        mark_seeded=True,
    )
    # Recorded again with a different status; ON CONFLICT DO NOTHING means
    # this should be a no-op, not overwrite "seeded" with "too_old".
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "too_old", False)],
        now=_now,
        mark_seeded=False,
    )
    row = conn.execute(
        "SELECT status FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
    ).fetchone()
    assert row["status"] == "seeded"


# --- fail_pending_codes ---


def _insert_code(conn, code, status, *, pinged=0, message_id=None):
    """A code row written by hand: v2's `claim_codes` is gone and this is all the test needs."""
    with conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, pinged, "
            "status, message_id) VALUES (?, ?, 'Src', ?, ?, ?, ?)",
            (code, NOW.isoformat(), f"https://e/{code[:1]}", pinged, status, message_id),
        )


def test_fail_pending_codes_flips_only_pending_rows(conn):
    _insert_code(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "pending")
    repo.record_silent_codes(
        conn,
        [
            ("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b", "seeded", False),
            ("CCCC3-CCCCC-CCCCC-CCCCC-CCCCC", "Src", "https://e/c", "too_old", False),
        ],
        now=_now,
        mark_seeded=True,
    )
    _insert_code(conn, "DDDD4-DDDDD-DDDDD-DDDDD-DDDDD", "posted", pinged=1, message_id=1)

    flipped = repo.fail_pending_codes(conn)

    assert flipped == ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]
    statuses = {
        row["code"]: row["status"] for row in conn.execute("SELECT code, status FROM alerted_codes")
    }
    assert statuses == {
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA": "failed",
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB": "seeded",
        "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC": "too_old",
        "DDDD4-DDDDD-DDDDD-DDDDD-DDDDD": "posted",
    }


def test_fail_pending_codes_on_empty_table_returns_empty_list(conn):
    assert repo.fail_pending_codes(conn) == []


# --- record_sweep ---


def test_record_sweep_updates_state(conn):
    repo.record_sweep(conn, _now, "17/19 sources ok, 1 new code")
    state = repo.get_alert_state(conn)
    assert state.last_sweep_at == NOW
    assert state.last_sweep_summary == "17/19 sources ok, 1 new code"


# --- CHECK constraints ---


def test_alerted_codes_rejects_wrong_length_code(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status) "
                "VALUES ('TOO-SHORT', 'now', 'Src', 'https://e/a', 'seeded')"
            )


def test_alerted_codes_rejects_invalid_status(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status) "
                "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 'bogus')"
            )


def test_alerted_codes_rejects_invalid_pinged_value(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, pinged, status) "
                "VALUES ('AAAA1-AAAAA-AAAAA-AAAAA-AAAAA', 'now', 'Src', 'https://e/a', 2, 'seeded')"
            )


# --- from_roundup plumbing (migration 003, plan step 2) ---


def test_record_silent_codes_stores_from_roundup_flag(conn):
    repo.record_silent_codes(
        conn,
        [
            ("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded", False),
            ("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b", "roundup", True),
        ],
        now=_now,
        mark_seeded=True,
    )
    rows = {
        row["code"]: row["from_roundup"]
        for row in conn.execute("SELECT code, from_roundup FROM alerted_codes")
    }
    assert rows == {
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA": 0,
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB": 1,
    }
