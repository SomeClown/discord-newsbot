"""QA fix batch 1, fix 5: `/shift codes` doesn't list unvetted codes.

A code recorded quietly (`roundup`, `too_old`, `seeded`) or from a roundup post is something
nobody told a channel about, so the list of current codes shows it only when something besides
one stranger's say-so stands behind it:

- a `code_details` row with a table- or official-rank source;
- a trusted sighting;
- sightings under two or more different source names (compared casefolded).

A posted code that isn't from a roundup is listed as before, `/shift history` is unchanged (it
shows markers), and the vetted codes still have to be current. The random-world parity test in
`test_shift_current_sql_adversarial.py` holds the SQL to a Python oracle with all of this in it;
these are the named cases.
"""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta

import pytest
from test_shift_current_sql_adversarial import (
    NOW,
    TODAY,
    _code,
    _put_alerted,
    _put_details,
)

from newsbot.store import repo
from newsbot.store.db import connect

QUIET_STATUSES = ["roundup", "too_old", "seeded"]


def _listed(conn) -> set[str]:
    return {c.code for c in repo.current_codes(conn, now=NOW)}


def _sight(conn, n: int, *pairs: tuple[str, bool], roundup: bool = False) -> None:
    with conn:
        repo.record_code_sightings(
            conn, [(_code(n), name, trusted, roundup) for name, trusted in pairs], now=lambda: NOW
        )


@pytest.mark.parametrize("status", QUIET_STATUSES)
def test_a_quiet_code_nobody_vouches_for_is_not_listed(v3_db, status):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW - timedelta(days=1), status)
        assert _listed(conn) == set()
        # ... but history still shows it, with its marker.
        assert [v.code for v in repo.history_codes(conn, NOW - timedelta(days=90))] == [_code(1)]


def test_a_posted_roundup_code_is_unvetted_too(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW, "posted", from_roundup=True)
        assert _listed(conn) == set()


def test_a_posted_code_that_is_not_from_a_roundup_needs_no_vouching(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW, "posted")
        assert _listed(conn) == {_code(1)}


@pytest.mark.parametrize("status", QUIET_STATUSES)
@pytest.mark.parametrize("field", ["reward_source", "category_source", "expires_source"])
@pytest.mark.parametrize("rank", ["table", "official"])
def test_a_ranked_details_row_vets_a_quiet_code(v3_db, status, field, rank):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW, status)
            _put_details(conn, 1, NOW, **{field: rank})
        assert _listed(conn) == {_code(1)}


def test_a_details_row_with_no_ranked_source_vets_nothing(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW, "too_old")
            _put_details(conn, 1, NOW, reward="3 Golden Keys", category="golden")
        assert _listed(conn) == set()


@pytest.mark.parametrize("status", QUIET_STATUSES)
def test_one_trusted_sighting_vets_a_quiet_code(v3_db, status):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, status)
        conn.commit()
        _sight(conn, 1, ("Gearbox Blog", True))
        assert _listed(conn) == {_code(1)}


def test_a_trusted_sighting_in_a_roundup_still_counts(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, ("Gearbox Blog", True), roundup=True)
        assert _listed(conn) == {_code(1)}


def test_one_untrusted_sighting_is_not_enough(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "too_old")
        conn.commit()
        _sight(conn, 1, ("r/Borderlands4", False))
        assert _listed(conn) == set()


def test_two_different_sources_vet_a_quiet_code(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, ("r/Borderlands4", False), ("Bluesky", False))
        assert _listed(conn) == {_code(1)}


def test_two_spellings_of_one_source_are_one_source(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, ("r/Borderlands4", False), ("R/BORDERLANDS4", False))
        assert _listed(conn) == set()


def test_the_casefold_is_a_real_one_not_ascii_lower(v3_db):
    # "Strasse" and "STRASSE" are one name under casefold ("ss"), which SQLite's lower() can't
    # see, and the sharp s spelling only pairs with them under a real casefold.
    sharp = "Stra\N{LATIN SMALL LETTER SHARP S}e"
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        _put_alerted(conn, 2, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, (sharp, False), ("STRASSE", False))
        _sight(conn, 2, (sharp, False), ("Strasse", False), ("Other", False))
        assert _listed(conn) == {_code(2)}


def test_a_vetted_code_still_has_to_be_current(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW - timedelta(days=1), "too_old")
            _put_details(conn, 1, NOW, expires_on=TODAY - timedelta(days=1), expires_source="table")
            _put_alerted(conn, 2, NOW - timedelta(days=1), "too_old")
            _put_details(conn, 2, NOW, expires_on=TODAY + timedelta(days=1), expires_source="table")
        assert _listed(conn) == {_code(2)}


def test_pending_and_failed_stay_out_whatever_vouches_for_them(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "pending")
        _put_alerted(conn, 2, NOW, "failed")
        conn.commit()
        for n in (1, 2):
            _sight(conn, n, ("Gearbox Blog", True))
        assert _listed(conn) == set()


def test_a_released_flagged_code_is_posted_and_listed(v3_db):
    """The fix 3 path ends in a posted, non-roundup code, which needs no vouching."""
    with closing(connect(v3_db)) as conn:
        repo.record_silent_codes(
            conn,
            [(_code(1), "mentalmars", "https://example.com/p", "too_old", False)],
            now=lambda: NOW,
            mark_seeded=False,
            table_silenced=[_code(1)],
        )
        assert _listed(conn) == set()
        repo.record_released_codes(
            conn,
            [(_code(1), "Gearbox", "https://example.com/g", False)],
            now=lambda: NOW + timedelta(hours=1),
            revive=[_code(1)],
            official=[_code(1)],
        )
        assert _listed(conn) == {_code(1)}


def test_the_list_keeps_its_order_after_filtering(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW - timedelta(hours=3), "posted")
            _put_alerted(conn, 2, NOW - timedelta(hours=2), "too_old")  # unvetted: out
            _put_alerted(conn, 3, NOW - timedelta(hours=1), "posted")
        assert [c.code for c in repo.current_codes(conn, now=NOW)] == [_code(3), _code(1)]
