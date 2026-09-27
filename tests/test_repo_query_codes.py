"""Tests for `repo.query_codes` (plan step 2, design.md §13's `/shift codes`).

Companion to test_repo_alerts.py -- kept separate since this is new, read-
only surface area rather than an addition to the write-path tests already
there.
"""

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _insert(conn, code, *, first_seen_at, status, from_roundup=0, source="Src", url="https://e/x"):
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

    codes, total = repo.query_codes(conn, SINCE, limit=10, offset=0)

    assert total == 1
    assert [c.code for c in codes] == ["CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"]


def test_includes_seeded_too_old_and_roundup_statuses(conn):
    for i, status in enumerate(["seeded", "too_old", "roundup"]):
        code = f"{chr(65 + i) * 4}{i}-AAAAA-AAAAA-AAAAA-AAAAA"
        _insert(conn, code, first_seen_at="2026-09-10T00:00:00+00:00", status=status)

    codes, total = repo.query_codes(conn, SINCE, limit=10, offset=0)
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

    codes, total = repo.query_codes(conn, SINCE, limit=10, offset=0)

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

    codes, _ = repo.query_codes(conn, SINCE, limit=10, offset=0)

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

    codes, _ = repo.query_codes(conn, SINCE, limit=10, offset=0)

    assert [c.code for c in codes] == [
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB",
    ]


def test_paging_offsets_through_results(conn):
    for i in range(5):
        code = f"{chr(65 + i) * 4}{i}-AAAAA-AAAAA-AAAAA-AAAAA"
        _insert(
            conn,
            code,
            first_seen_at=(datetime(2026, 9, 10, tzinfo=UTC) + timedelta(minutes=i)).isoformat(),
            status="posted",
        )

    page1, total = repo.query_codes(conn, SINCE, limit=2, offset=0)
    page2, _ = repo.query_codes(conn, SINCE, limit=2, offset=2)

    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert {c.code for c in page1}.isdisjoint({c.code for c in page2})


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

    codes, _ = repo.query_codes(conn, SINCE, limit=10, offset=0)

    view = codes[0]
    assert view.code == "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
    assert view.source_name == "Reddit Megathread"
    assert view.item_url == "https://e/roundup"
    assert view.status == "posted"
    assert view.from_roundup is True


def test_empty_table_returns_empty_list_and_zero_total(conn):
    codes, total = repo.query_codes(conn, SINCE, limit=10, offset=0)
    assert codes == []
    assert total == 0
