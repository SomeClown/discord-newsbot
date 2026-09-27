"""Adversarial tests for `repo.query_codes`, beyond test_repo_query_codes.py's
own exclusion/window/ordering/paging/round-trip coverage.

Angle (test-engineer brief, 2026-09-26): the `since` boundary's exact
inclusivity, what happens with a naive `datetime` versus one in a non-UTC
timezone (the docstring only promises "UTC-aware `since`" -- nothing
enforces it), `limit=0`, an `offset` past the end of the result set, that
`since`'s SQL parameterization can't be abused (it's bound, not
interpolated, so there's no injection angle -- the real risk is type
enforcement, or the lack of it), a 10k-row sanity check, and a rowid
tie-break across two separate transactions rather than one.
"""

from __future__ import annotations

import time
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _insert(
    conn, code, *, first_seen_at, status="posted", from_roundup=0, source="Src", url="https://e/x"
):
    conn.execute(
        "INSERT INTO alerted_codes "
        "(code, first_seen_at, source_name, item_url, status, from_roundup) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (code, first_seen_at, source, url, status, from_roundup),
    )
    conn.commit()


# --- since boundary inclusivity ---


def test_since_boundary_is_inclusive(conn):
    # A row whose first_seen_at exactly equals `since` is a match ("since
    # 2026-09-01" reads as "starting from, and including, that moment").
    boundary = datetime(2026, 9, 1, tzinfo=UTC)
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at=boundary.isoformat())

    codes, total = repo.query_codes(conn, boundary, limit=10, offset=0)

    assert total == 1
    assert [c.code for c in codes] == ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]


def test_row_one_microsecond_before_since_is_excluded(conn):
    boundary = datetime(2026, 9, 1, tzinfo=UTC)
    just_before = boundary - timedelta(microseconds=1)
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at=just_before.isoformat())

    codes, total = repo.query_codes(conn, boundary, limit=10, offset=0)

    assert total == 0
    assert codes == []


# --- naive / non-UTC datetime handling ---


def test_naive_since_at_the_epoch_of_all_stored_rows_does_not_raise(conn):
    # query_codes doesn't require a tz-aware `since` at the Python level
    # (it just calls .isoformat() and compares strings), so a naive
    # datetime that predates everything stored still "works" by accident.
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at="2026-09-10T00:00:00+00:00")

    naive_since = datetime(2026, 9, 1)  # no tzinfo
    codes, total = repo.query_codes(conn, naive_since, limit=10, offset=0)

    assert total == 1
    assert [c.code for c in codes] == ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]


def test_since_in_a_non_utc_timezone_is_compared_chronologically_not_lexicographically(conn):
    pacific = timezone(timedelta(hours=-8))
    # 2026-09-10T12:00:00 UTC, stored the way _resolve_now always renders it.
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at="2026-09-10T12:00:00+00:00")

    # 05:00 Pacific on the same date == 13:00 UTC: one hour AFTER the row above.
    since_pacific = datetime(2026, 9, 10, 5, 0, 0, tzinfo=pacific)
    codes, total = repo.query_codes(conn, since_pacific, limit=10, offset=0)

    assert total == 0
    assert codes == []


def test_since_as_a_non_datetime_raises_instead_of_silently_matching_everything(conn):
    # query_codes has no runtime type check of its own; it just calls
    # `.isoformat()` on whatever it's given. A plain string blows up with
    # AttributeError rather than getting concatenated into SQL (the query
    # is fully parameterized: `since` is always a bound value, never
    # spliced into the SQL text, so there's no injection angle here, only
    # a duck-typed contract that fails loudly instead of quietly).
    malicious_since = "2026-09-01T00:00:00+00:00; DROP TABLE alerted_codes;--"
    with pytest.raises(AttributeError):
        repo.query_codes(conn, malicious_since, limit=10, offset=0)

    # And the table is still there and still usable.
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at="2026-09-10T00:00:00+00:00")
    codes, total = repo.query_codes(conn, SINCE, limit=10, offset=0)
    assert total == 1


# --- limit / offset edges ---


def test_limit_zero_returns_no_rows_but_a_correct_total(conn):
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at="2026-09-10T00:00:00+00:00")
    _insert(conn, "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", first_seen_at="2026-09-11T00:00:00+00:00")

    codes, total = repo.query_codes(conn, SINCE, limit=0, offset=0)

    assert codes == []
    assert total == 2


def test_offset_past_the_end_of_the_result_set_returns_no_rows(conn):
    for i in range(3):
        code = f"{chr(65 + i) * 4}{i}-AAAAA-AAAAA-AAAAA-AAAAA"
        _insert(conn, code, first_seen_at=f"2026-09-{10 + i:02d}T00:00:00+00:00")

    codes, total = repo.query_codes(conn, SINCE, limit=10, offset=100)

    assert codes == []
    assert total == 3


# --- scale and cross-transaction ordering ---


def test_10k_rows_query_returns_correct_page_promptly(conn):
    now = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        (
            f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            (now + timedelta(seconds=i)).isoformat(),
            "Src",
            "https://e/x",
            "posted",
            0,
        )
        for i in range(10_000)
    ]
    with conn:
        conn.executemany(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    started = time.monotonic()
    codes, total = repo.query_codes(conn, SINCE, limit=8, offset=0)
    elapsed = time.monotonic() - started

    assert total == 10_000
    assert len(codes) == 8
    # Newest first: the last-inserted row (i=9999) sorts first.
    assert codes[0].code == "09999-AAAAA-AAAAA-AAAAA-AAAAA"
    # Generous bound: this is a sanity check against an accidentally
    # unindexed full scan gone quadratic, not a strict perf benchmark.
    assert elapsed < 5.0


def test_rowid_tiebreak_holds_across_separate_transactions_with_identical_timestamps(conn):
    # test_repo_query_codes.py's own tie-break test inserts both rows before
    # any commit; here each insert is its own transaction (its own commit),
    # matching how two separate sweep runs could plausibly claim codes with
    # the same first_seen_at (a claim batch stamps one timestamp per call,
    # but two different calls a moment apart could still round-trip to the
    # same second). rowid still only ever increases, so insertion order
    # survives regardless of transaction boundaries.
    same = "2026-09-10T00:00:00+00:00"
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at=same)  # its own commit
    _insert(conn, "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", first_seen_at=same)  # a later, separate commit

    codes, _ = repo.query_codes(conn, SINCE, limit=10, offset=0)

    assert [c.code for c in codes] == [
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
    ]
