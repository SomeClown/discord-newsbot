"""`repo.claim_codes([])` must be a no-op, ping budget included.

Split out from `test_repo_alerts.py` (owned by a parallel test pass)
instead of appended to it, so the two don't collide on the same file.
"""

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


def _now():
    return datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_claim_codes_empty_list_does_not_spend_a_ping(conn):
    repo.claim_codes(conn, [], pinged=True, local_day="2026-09-25", now=_now)
    state = repo.get_alert_state(conn)
    assert state.ping_day is None
    assert state.ping_count == 0


def test_claim_codes_empty_list_after_prior_claims_leaves_budget_untouched(conn):
    repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day="2026-09-25",
        now=_now,
    )
    repo.claim_codes(conn, [], pinged=True, local_day="2026-09-25", now=_now)
    assert repo.get_alert_state(conn).ping_count == 1
