"""Edge cases for the digest claim/publish/save guard beyond the happy-path
tests in test_repo_write.py.

The interesting failure mode for a daily cron job is the one where it dies
halfway through: `claim_digest` leaves a `pending` row behind, and nothing
in the repo layer knows how long ago that was. That's intentional -- the
plan (SPEC-DEV 2) puts staleness detection and admin alerting one layer up,
in the scheduler that hasn't been built yet -- but it means `claim_digest`
itself has to keep refusing a `pending` row forever, not just for a bit.
These tests pin that "forever" on purpose, so a future change to the guard
has to break a test to change it, rather than drifting quietly.
"""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import Usage


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _backdate_pending(conn, run_date: date, when: datetime) -> None:
    """Make an existing pending row look like it's been sitting there a while."""
    with conn:
        conn.execute(
            "UPDATE digests SET created_at = ?, updated_at = ? WHERE run_date = ?",
            (when.isoformat(), when.isoformat(), run_date.isoformat()),
        )


def test_stale_pending_still_blocks_without_force(conn):
    """A week-old pending row is still a pending row; the guard doesn't guess at staleness."""
    repo.claim_digest(conn, date(2026, 9, 23), force=False)
    _backdate_pending(conn, date(2026, 9, 23), datetime(2026, 9, 16, tzinfo=UTC))
    assert repo.claim_digest(conn, date(2026, 9, 23), force=False) is None


def test_stale_pending_still_blocks_with_force(conn):
    """`force` is documented to only override ok/partial, never pending --
    age doesn't change that."""
    repo.claim_digest(conn, date(2026, 9, 23), force=False)
    _backdate_pending(conn, date(2026, 9, 23), datetime(2026, 9, 16, tzinfo=UTC))
    assert repo.claim_digest(conn, date(2026, 9, 23), force=True) is None


def test_failed_allows_reclaim_with_force_too(conn):
    """force=True against a failed row behaves the same as force=False: it's always reclaimable."""
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.mark_digest_failed(conn, digest_id, "boom", [])
    reclaimed = repo.claim_digest(conn, date(2026, 9, 23), force=True)
    assert reclaimed == digest_id


def test_get_digest_returns_none_for_unclaimed_date(conn):
    assert repo.get_digest(conn, date(2099, 1, 1)) is None


def test_claiming_two_different_dates_does_not_interfere(conn):
    first = repo.claim_digest(conn, date(2026, 9, 22), force=False)
    second = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    assert first is not None
    assert second is not None
    assert first != second
    assert repo.get_digest(conn, date(2026, 9, 22)).status == "pending"
    assert repo.get_digest(conn, date(2026, 9, 23)).status == "pending"


def test_force_on_fresh_unclaimed_date_behaves_like_no_force(conn):
    """force=True on a date with no row yet just claims it, same as force=False."""
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=True)
    assert digest_id is not None
    assert repo.get_digest(conn, date(2026, 9, 23)).status == "pending"


def test_partial_status_reclaim_keeps_same_digest_id_with_force(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [], [], "partial", [1], "fallback used", Usage(0, 0))
    reclaimed = repo.claim_digest(conn, date(2026, 9, 23), force=True)
    assert reclaimed == digest_id
    # Reclaiming resets status to pending but doesn't touch prior error notes
    # or message ids until a new save_run/mark_digest_failed writes them.
    row = repo.get_digest(conn, date(2026, 9, 23))
    assert row.status == "pending"


def test_claim_digest_updated_at_advances_on_reclaim(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [], [], "ok", [1], None, Usage(0, 0))
    _backdate_pending(conn, date(2026, 9, 23), datetime(2020, 1, 1, tzinfo=UTC))
    before = conn.execute("SELECT updated_at FROM digests WHERE id = ?", (digest_id,)).fetchone()[0]
    repo.claim_digest(conn, date(2026, 9, 23), force=True)
    after = conn.execute("SELECT updated_at FROM digests WHERE id = ?", (digest_id,)).fetchone()[0]
    assert after != before
    assert datetime.fromisoformat(after) > datetime.fromisoformat(before) + timedelta(days=1)
