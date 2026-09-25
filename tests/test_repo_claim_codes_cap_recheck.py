"""Tests for QA item 7: `claim_codes`'s `max_pings` re-check and its `BEGIN IMMEDIATE` write lock.

Before this, `shift/sweep.py` decided whether a batch would ping
(`decide.plan_alerts`) from a plain read of `alert_state` taken moments
earlier, then told `claim_codes` to spend it -- a second caller (an hourly
sweep and a `/newsbot test-alert` landing in the same instant) could both
see "budget available" and both claim a ping, over-spending the cap.
`claim_codes` now takes `max_pings` and re-checks the cap itself, against
whatever the count actually is once it holds SQLite's write lock.
"""

from __future__ import annotations

import sqlite3
import threading
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


# --- max_pings re-check ---


def test_max_pings_none_skips_the_recheck_same_as_before(conn):
    # The default -- every caller from before max_pings existed keeps
    # spending exactly what it asked for, cap or no cap.
    for i in range(5):
        actual = repo.claim_codes(
            conn,
            [(f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
            pinged=True,
            local_day="2026-09-25",
            now=_now,
        )
        assert actual is True
    assert repo.get_alert_state(conn).ping_count == 5


def test_max_pings_downgrades_a_ping_once_the_cap_is_reached(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
        max_pings=1,
    )
    actual = repo.claim_codes(
        conn,
        [("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", "Src", "https://e/b")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
        max_pings=1,
    )
    assert actual is False
    assert repo.get_alert_state(conn).ping_count == 1  # the second claim never spent one
    row = conn.execute(
        "SELECT pinged FROM alerted_codes WHERE code = 'BBBB2-BBBBB-BBBBB-BBBBB-BBBBB'"
    ).fetchone()
    assert row["pinged"] == 0  # the row itself reflects what actually happened, not what was asked


def test_max_pings_leaves_an_unpinged_request_unpinged(conn):
    actual = repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=False,
        local_day="2026-09-25",
        now=_now,
        max_pings=3,
    )
    assert actual is False
    assert repo.get_alert_state(conn).ping_count == 0


def test_max_pings_under_the_cap_still_pings(conn):
    actual = repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
        max_pings=3,
    )
    assert actual is True


def test_empty_codes_returns_false_and_spends_nothing(conn):
    actual = repo.claim_codes(conn, [], pinged=True, local_day="2026-09-25", now=_now, max_pings=3)
    assert actual is False
    assert repo.get_alert_state(conn).ping_count == 0


# --- BEGIN IMMEDIATE: a genuine second-connection race sees the committed count ---


def test_two_connections_racing_the_last_ping_slot_only_one_gets_it(tmp_path):
    db_path = tmp_path / "newsbot.db"
    with closing(connect(db_path)) as setup_conn:
        migrate(setup_conn)

    results: dict[str, bool] = {}
    barrier = threading.Barrier(2)

    def _claim(name: str, code: str) -> None:
        conn = connect(db_path)
        try:
            barrier.wait(timeout=5)
            actual = repo.claim_codes(
                conn,
                [(code, "Src", f"https://e/{code}")],
                pinged=True,
                local_day="2026-09-25",
                now=_now,
                max_pings=1,
            )
            results[name] = actual
        finally:
            conn.close()

    t1 = threading.Thread(target=_claim, args=("t1", "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"))
    t2 = threading.Thread(target=_claim, args=("t2", "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # BEGIN IMMEDIATE serializes the two writers -- exactly one of them
    # sees "budget available" and actually spends it; whichever runs
    # second sees the first one's committed count and gets downgraded,
    # rather than each reading the same stale zero and both pinging.
    assert sorted(results.values()) == [False, True]
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).ping_count == 1


def test_duplicate_code_still_rolls_back_cleanly_with_max_pings_set(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
        max_pings=3,
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.claim_codes(
            conn,
            [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
            pinged=True,
            local_day="2026-09-25",
            now=_now,
            max_pings=3,
        )
    # The failed second attempt didn't leave a half-open transaction or
    # spend a second ping it never should have gotten to.
    assert repo.get_alert_state(conn).ping_count == 1
