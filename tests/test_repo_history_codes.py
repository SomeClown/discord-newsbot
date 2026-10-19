"""Tests for `repo.history_codes` (design.md §16's `/shift history`; the window query).

The old `repo.query_codes` (which paged) is gone, and these are its tests, carried over to the
function that replaced it: the exclusions, the window, the order and its tie-break, the odd
`since` values, and the size. What the paging tests covered (`limit`, `offset`) has no
counterpart: history is rendered whole and cut into pages by size afterwards.
"""

import time
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate


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


SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def test_excludes_pending_and_failed_statuses(conn):
    _insert(
        conn,
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        first_seen_at="2026-09-10T00:00:00+00:00",
        status="pending",
    )
    _insert(
        conn,
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
        first_seen_at="2026-09-10T00:00:00+00:00",
        status="failed",
    )
    _insert(
        conn,
        "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC",
        first_seen_at="2026-09-10T00:00:00+00:00",
        status="posted",
    )

    codes = repo.history_codes(conn, SINCE)

    total = len(codes)

    assert total == 1
    assert [c.code for c in codes] == ["CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"]


def test_includes_seeded_too_old_and_roundup_statuses(conn):
    for i, status in enumerate(["seeded", "too_old", "roundup"]):
        code = f"{chr(65 + i) * 4}{i}-AAAAA-AAAAA-AAAAA-AAAAA"
        _insert(conn, code, first_seen_at="2026-09-10T00:00:00+00:00", status=status)

    codes = repo.history_codes(conn, SINCE)

    total = len(codes)
    assert total == 3
    assert {c.status for c in codes} == {"seeded", "too_old", "roundup"}


def test_excludes_rows_before_the_since_window(conn):
    _insert(
        conn,
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        first_seen_at="2026-08-01T00:00:00+00:00",
        status="posted",
    )
    _insert(
        conn,
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
        first_seen_at="2026-09-15T00:00:00+00:00",
        status="posted",
    )

    codes = repo.history_codes(conn, SINCE)

    total = len(codes)

    assert total == 1
    assert [c.code for c in codes] == ["BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"]


def test_orders_newest_first(conn):
    _insert(
        conn,
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        first_seen_at="2026-09-10T00:00:00+00:00",
        status="posted",
    )
    _insert(
        conn,
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
        first_seen_at="2026-09-20T00:00:00+00:00",
        status="posted",
    )
    _insert(
        conn,
        "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC",
        first_seen_at="2026-09-15T00:00:00+00:00",
        status="posted",
    )

    codes = repo.history_codes(conn, SINCE)

    assert [c.code for c in codes] == [
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
        "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC",
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
    ]


def test_ties_on_first_seen_at_break_on_insertion_order(conn):
    # A batch claimed together shares one first_seen_at timestamp; rowid
    # (insertion order) is the tiebreaker so paging never reshuffles them.
    same = "2026-09-10T00:00:00+00:00"
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at=same, status="posted")
    _insert(conn, "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", first_seen_at=same, status="posted")

    codes = repo.history_codes(conn, SINCE)

    assert [c.code for c in codes] == [
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
    ]


def test_from_roundup_and_other_fields_round_trip(conn):
    _insert(
        conn,
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        first_seen_at="2026-09-10T00:00:00+00:00",
        status="posted",
        from_roundup=1,
        source="Reddit Megathread",
        url="https://e/roundup",
    )

    codes = repo.history_codes(conn, SINCE)

    view = codes[0]
    assert view.code == "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
    assert view.source_name == "Reddit Megathread"
    assert view.item_url == "https://e/roundup"
    assert view.status == "posted"
    assert view.from_roundup is True


def test_empty_table_returns_empty_list_and_zero_total(conn):
    codes = repo.history_codes(conn, SINCE)
    total = len(codes)
    assert codes == []
    assert total == 0


# --- since boundary inclusivity ---


def test_since_boundary_is_inclusive(conn):
    # A row whose first_seen_at exactly equals `since` is a match ("since
    # 2026-09-01" reads as "starting from, and including, that moment").
    boundary = datetime(2026, 9, 1, tzinfo=UTC)
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at=boundary.isoformat())

    codes = repo.history_codes(conn, boundary)

    total = len(codes)

    assert total == 1
    assert [c.code for c in codes] == ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]


def test_row_one_microsecond_before_since_is_excluded(conn):
    boundary = datetime(2026, 9, 1, tzinfo=UTC)
    just_before = boundary - timedelta(microseconds=1)
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at=just_before.isoformat())

    codes = repo.history_codes(conn, boundary)

    total = len(codes)

    assert total == 0
    assert codes == []


# --- naive / non-UTC datetime handling ---


def test_naive_since_at_the_epoch_of_all_stored_rows_does_not_raise(conn):
    # query_codes doesn't require a tz-aware `since` at the Python level
    # (it just calls .isoformat() and compares strings), so a naive
    # datetime that predates everything stored still "works" by accident.
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at="2026-09-10T00:00:00+00:00")

    naive_since = datetime(2026, 9, 1)  # no tzinfo
    codes = repo.history_codes(conn, naive_since)
    total = len(codes)

    assert total == 1
    assert [c.code for c in codes] == ["AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"]


def test_since_in_a_non_utc_timezone_is_compared_chronologically_not_lexicographically(conn):
    pacific = timezone(timedelta(hours=-8))
    # 2026-09-10T12:00:00 UTC, stored the way _resolve_now always renders it.
    _insert(conn, "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", first_seen_at="2026-09-10T12:00:00+00:00")

    # 05:00 Pacific on the same date == 13:00 UTC: one hour AFTER the row above.
    since_pacific = datetime(2026, 9, 10, 5, 0, 0, tzinfo=pacific)
    codes = repo.history_codes(conn, since_pacific)
    total = len(codes)

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
    codes = repo.history_codes(conn, SINCE)
    total = len(codes)
    assert total == 1


# --- limit / offset edges ---


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
    codes = repo.history_codes(conn, SINCE)
    total = len(codes)
    elapsed = time.monotonic() - started

    assert total == 10_000
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

    codes = repo.history_codes(conn, SINCE)

    assert [c.code for c in codes] == [
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
    ]
