"""Edge cases for the digest claim/publish/save guard beyond the happy-path
tests in test_repo_write.py.

The interesting failure mode for a daily cron job is the one where it dies
halfway through: `claim_digest` leaves a `pending` row behind, and nothing
in the repo layer knows how long ago that was. `claim_digest` itself still
refuses a plain (unforced) `pending` row forever, not just for a bit --
staleness detection and admin alerting live one layer up, in
`needs_confirmation`/`should_catch_up` (bot/commands.py, bot/client.py).

`force=True` *does* now override `pending` (QA step 20, group 4): the
in-process `_run_lock` in `pipeline/run.py` already rules out two runs
racing each other, so a `pending` row a force-claim can see is always a
crash artifact, never a live run -- refusing to reclaim it just left
admins stuck waiting on `mark_digest_failed`, which a crashed process
never got to run. This module's tests pin that new contract on purpose,
same as they pinned the old one.
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


def test_stale_pending_is_reclaimed_with_force(conn):
    """Behavior change (QA step 20, group 4): `force` now overrides a `pending`
    row too, not just ok/partial. The in-process `_run_lock` in run.py already
    stops two runs from claiming concurrently, so a `pending` row surviving to
    the *next* run is always a crash artifact, not an active run -- forcing
    past it is what lets an admin recover from a stuck row without waiting
    for `mark_digest_failed` to run first. (Was pinned the other way as
    test_stale_pending_still_blocks_with_force; deliberately changed here,
    not a regression.)"""
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    _backdate_pending(conn, date(2026, 9, 23), datetime(2026, 9, 16, tzinfo=UTC))
    reclaimed = repo.claim_digest(conn, date(2026, 9, 23), force=True)
    assert reclaimed == digest_id


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
