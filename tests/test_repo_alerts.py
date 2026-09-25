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
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded")],
        now=_now,
        mark_seeded=True,
    )
    known = repo.known_codes(
        conn, ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"]
    )
    assert known == {"AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"}


def test_known_codes_chunks_past_sqlite_variable_limit(conn):
    codes = [f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(1200)]
    rows = [(c, "Src", "https://e/x", "seeded") for c in codes]
    repo.record_silent_codes(conn, rows, now=_now, mark_seeded=False)
    assert repo.known_codes(conn, codes) == set(codes)


# --- record_silent_codes ---


def test_record_silent_codes_sets_seeded_marker_when_requested(conn):
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded")],
        now=_now,
        mark_seeded=True,
    )
    assert repo.get_alert_state(conn).seeded is True


def test_record_silent_codes_does_not_set_marker_when_unhealthy(conn):
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded")],
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
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded")],
        now=_now,
        mark_seeded=True,
    )
    # Recorded again with a different status; ON CONFLICT DO NOTHING means
    # this should be a no-op, not overwrite "seeded" with "too_old".
    repo.record_silent_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "too_old")],
        now=_now,
        mark_seeded=False,
    )
    row = conn.execute(
        "SELECT status FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
    ).fetchone()
    assert row["status"] == "seeded"


# --- claim_codes: atomicity and the daily ping budget ---


def test_claim_codes_inserts_pending_rows_and_spends_ping(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    row = conn.execute(
        "SELECT status, pinged FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["pinged"] == 1
    state = repo.get_alert_state(conn)
    assert state.ping_day == "2026-09-25"
    assert state.ping_count == 1


def test_claim_codes_unpinged_does_not_spend_budget(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
    )
    state = repo.get_alert_state(conn)
    assert state.ping_day == "2026-09-25"
    assert state.ping_count == 0


def test_claim_codes_accumulates_ping_count_same_day(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.claim_codes(
        conn,
        [("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    assert repo.get_alert_state(conn).ping_count == 2


def test_claim_codes_resets_count_on_a_new_local_day(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.claim_codes(
        conn,
        [("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b")],
        pinged=True,
        local_day="2026-09-26",
        now=_now,
    )
    state = repo.get_alert_state(conn)
    assert state.ping_day == "2026-09-26"
    assert state.ping_count == 1


def test_claim_codes_duplicate_code_rolls_back_rows_and_ping_count(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.claim_codes(
            conn,
            [
                ("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b"),
                ("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a"),  # dup, aborts the txn
            ],
            pinged=True,
            local_day="2026-09-25",
            now=_now,
        )
    # Neither the new code nor the extra ping spend survived the rollback.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM alerted_codes WHERE code = 'BBBB2-BBBBB-BBBBB-BBBBB-BBBBB'"
        ).fetchone()[0]
        == 0
    )
    assert repo.get_alert_state(conn).ping_count == 1


# --- mark_codes_posted / mark_codes_failed / fail_pending_codes ---


def test_mark_codes_posted_sets_status_and_shared_message_id(conn):
    repo.claim_codes(
        conn,
        [
            ("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a"),
            ("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b"),
        ],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.mark_codes_posted(
        conn,
        ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"],
        message_id=42,
    )
    rows = conn.execute("SELECT code, status, message_id FROM alerted_codes").fetchall()
    assert {(r["code"], r["status"], r["message_id"]) for r in rows} == {
        ("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "posted", 42),
        ("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "posted", 42),
    }


def test_mark_codes_failed_sets_status(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
    )
    repo.mark_codes_failed(conn, ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"])
    row = conn.execute(
        "SELECT status FROM alerted_codes WHERE code = 'AAAA1-AAAAA-AAAAA-AAAAA-AAAAA'"
    ).fetchone()
    assert row["status"] == "failed"


def test_fail_pending_codes_flips_only_pending_rows(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
    )
    repo.record_silent_codes(
        conn,
        [
            ("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b", "seeded"),
            ("CCCC3-CCCCC-CCCCC-CCCCC-CCCCC", "Src", "https://e/c", "too_old"),
        ],
        now=_now,
        mark_seeded=True,
    )
    repo.claim_codes(
        conn,
        [("DDDD4-DDDDD-DDDDD-DDDDD-DDDDD", "Src", "https://e/d")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.mark_codes_posted(conn, ["DDDD4-DDDDD-DDDDD-DDDDD-DDDDD"], message_id=1)

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


# --- record_sweep / alert_status ---


def test_record_sweep_updates_state(conn):
    repo.record_sweep(conn, _now, "17/19 sources ok, 1 new code")
    state = repo.get_alert_state(conn)
    assert state.last_sweep_at == NOW
    assert state.last_sweep_summary == "17/19 sources ok, 1 new code"


def test_alert_status_reports_seeded_sweep_and_pings(conn):
    repo.record_silent_codes(conn, [], now=_now, mark_seeded=True)
    repo.record_sweep(conn, _now, "17/19 sources ok, 1 new code")
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.mark_codes_posted(conn, ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"], message_id=1)

    status = repo.alert_status(conn, "2026-09-25", enabled=True, max_pings=3)

    assert status.enabled is True
    assert status.seeded is True
    assert status.last_sweep_summary == "17/19 sources ok, 1 new code"
    assert status.codes_alerted == 1
    assert status.pings_today == 1
    assert status.max_pings == 3


def test_alert_status_pings_today_is_zero_on_a_new_day(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    status = repo.alert_status(conn, "2026-09-26", enabled=True, max_pings=3)
    assert status.pings_today == 0


def test_alert_status_unseeded_and_disabled(conn):
    status = repo.alert_status(conn, "2026-09-25", enabled=False, max_pings=3)
    assert status.enabled is False
    assert status.seeded is False
    assert status.codes_alerted == 0
    assert status.pings_today == 0


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
