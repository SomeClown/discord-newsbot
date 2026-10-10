"""QA round 2, area 4: attacking `/shift codes` vetting (batch d36d492).

A code that was only ever recorded quietly (or came from a roundup) is listed in `/shift codes`
only when something besides one stranger's say-so stands behind it: a ranked details source, a
trusted sighting, or two different source names. "Different" is a `str.casefold()` comparison,
the same one the B+ follow-up rule uses, and these tests hold the two together: ASCII-only
folding, no Unicode normalisation, the sharp s, the Turkish i and the full-width forms are all
named cases, and the SQL path is held to an independently written Python oracle on random
worlds with a nastier name pool than the first round used.
"""

from __future__ import annotations

import itertools
import random
from contextlib import closing
from datetime import timedelta

import pytest
from test_shift_current_sql_adversarial import (
    NOW,
    _code,
    _put_alerted,
    _put_details,
)

from newsbot.store import repo
from newsbot.store.db import connect

SHARP = "Stra\u00dfe"
# (a, b, one_source): is the pair a single source under casefold? Written out by hand so the
# test pins what the rule does and not just that it agrees with itself.
PAIRS = [
    ("Alpha", "alpha", True),
    (SHARP, "STRASSE", True),
    (SHARP, "strasse", True),
    (SHARP, "Strasse", True),
    (
        "\u039f\u0394\u03a5\u03a3\u03a3\u0395\u03a5\u03a3",
        "\u03bf\u03b4\u03c5\u03c3\u03c3\u03b5\u03c5\u03c2",
        True,
    ),  # final sigma
    ("\u01c5", "\u01c6", True),  # a titlecase digraph and its lowercase
    ("\ufb01nd", "FIND", True),  # the fi ligature
    ("\u0130stanbul", "i\u0307stanbul", True),  # dotted capital I and its two-character fold
    ("I", "\u0131", False),  # dotless i is its own letter
    ("\u0130", "i", False),  # the fold of dotted I carries a combining dot
    ("\uff22\uff2c\uff35\uff25", "blue", False),  # full-width forms are not compatibility-folded
    ("\uff22\uff2c\uff35\uff25", "\uff42\uff4c\uff55\uff45", True),
    ("caf\u00e9", "cafe\u0301", False),  # NFC and NFD spellings are not normalised
    (
        "Alpha",
        "Alpha ",
        False,
    ),  # padding is part of the name (config refuses it; a database may not)
    ("Alpha", "Alph\u0430", False),  # a Cyrillic a
]
POOL = sorted({name for a, b, _ in PAIRS for name in (a, b)} | {"Beta", "mentalmars", "MentalMars"})


def _listed(conn) -> set[str]:
    return {c.code for c in repo.current_codes(conn, now=NOW)}


def _sight(conn, n, *pairs, roundup=False):
    with conn:
        repo.record_code_sightings(
            conn, [(_code(n), name, trusted, roundup) for name, trusted in pairs], now=lambda: NOW
        )


# --- named casefold cases ---


@pytest.mark.parametrize(("a", "b", "one_source"), PAIRS)
def test_two_names_are_one_source_exactly_when_casefold_says_so(v3_db, a, b, one_source):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, (a, False), (b, False))
        assert (_code(1) in _listed(conn)) is (not one_source)
        assert (_code(1) in repo._named_by_two_sources(conn, [_code(1)])) is (not one_source)


@pytest.mark.parametrize(("a", "b", "one_source"), PAIRS)
def test_the_follow_up_rule_and_the_list_agree_on_every_pair(v3_db, a, b, one_source):
    """B+ confirms a posted unpinged code on two names; the list vets a quiet code on two names.

    Both must call a pair "one source" or "two" the same way, or a code could be confirmed
    in the channel and absent from the list (or the other way around).
    """
    with closing(connect(v3_db)) as conn:
        with conn:
            repo.create_guild(conn, 1, timezone="UTC", set_up=True, now=lambda: NOW)
            repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="everyone", now=lambda: NOW)
            for n in (1, 2):
                _put_alerted(conn, n, NOW - timedelta(hours=1), "roundup" if n == 1 else "posted")
            conn.execute(
                "INSERT INTO guild_code_posts (guild_id, code, status, pinged, claimed_at, "
                "followup_ok, message_id) VALUES (1, ?, 'posted', 0, ?, 1, 9001)",
                (_code(2), NOW.isoformat()),
            )
        _sight(conn, 1, (a, False), (b, False))
        _sight(conn, 2, (a, False), (b, False))

        vetted = _code(1) in _listed(conn)
        with conn:
            queued = repo.queue_confirmed_followups(conn, [_code(2)], now=lambda: NOW)

        assert vetted == (queued == 1) == (not one_source)


# --- the table and its case variants ---


def test_the_table_and_a_case_variant_of_its_own_name_are_one_source(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "too_old")
        conn.commit()
        _sight(conn, 1, ("mentalmars", False), ("MentalMars", False), ("MENTALMARS", False))
        assert _listed(conn) == set()


def test_the_table_and_a_genuinely_different_name_are_two(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "too_old")
        conn.commit()
        _sight(conn, 1, ("mentalmars", False), ("MentalMars", False), ("Bluesky", False))
        assert _listed(conn) == {_code(1)}


def test_a_ranked_table_detail_vets_a_quiet_code_with_a_single_sighting(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW, "too_old")
            _put_details(conn, 1, NOW, reward="1 Golden Key", reward_source="table")
        _sight(conn, 1, ("mentalmars", False))
        assert _listed(conn) == {_code(1)}


# --- trusted sightings that were roundups: the asymmetry, pinned on purpose ---


def test_a_trusted_roundup_sighting_vets_the_list_but_would_not_confirm_a_follow_up(v3_db):
    """The list counts roundup sightings (a Gearbox roundup is still Gearbox); B+ does not.

    If either side changes, this test is the place that says the two rules have been
    separated or joined on purpose.
    """
    with closing(connect(v3_db)) as conn:
        with conn:
            repo.create_guild(conn, 1, timezone="UTC", set_up=True, now=lambda: NOW)
            repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="everyone", now=lambda: NOW)
            _put_alerted(conn, 1, NOW - timedelta(hours=1), "posted", from_roundup=True)
            conn.execute(
                "INSERT INTO guild_code_posts (guild_id, code, status, pinged, claimed_at, "
                "followup_ok, message_id) VALUES (1, ?, 'posted', 0, ?, 1, 9001)",
                (_code(1), NOW.isoformat()),
            )
        _sight(conn, 1, ("Gearbox Blog", True), roundup=True)

        assert _listed(conn) == {_code(1)}
        with conn:
            assert repo.queue_confirmed_followups(conn, [_code(1)], now=lambda: NOW) == 0


def test_two_community_roundup_sightings_under_different_names_vet_the_list(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, ("r/Borderlands4", False), ("Bluesky", False), roundup=True)
        assert _listed(conn) == {_code(1)}


def test_the_same_source_in_a_roundup_and_in_a_normal_item_is_still_one_source(v3_db):
    with closing(connect(v3_db)) as conn:
        _put_alerted(conn, 1, NOW, "roundup")
        conn.commit()
        _sight(conn, 1, ("r/Borderlands4", False), roundup=True)
        _sight(conn, 1, ("r/Borderlands4", False))
        assert _listed(conn) == set()


# --- random worlds against an independent oracle ---


def _oracle(world: dict) -> bool:
    """Whether the list should show a code, from the rule as the plan words it.

    Written without looking at the implementation: quiet means a quiet status or a roundup
    origin; a quiet code needs a ranked source, a trusted sighting, or two names that differ
    under casefold, checked pairwise rather than by counting a set.
    """
    if world["status"] in ("pending", "failed"):
        return False
    quiet = world["status"] in ("roundup", "too_old", "seeded") or world["from_roundup"]
    if not quiet:
        return True
    if any(world["ranked"]):
        return True
    names = [name for name, _, _ in world["sightings"]]
    if any(trusted for _, trusted, _ in world["sightings"]):
        return True
    return any(a.casefold() != b.casefold() for a, b in itertools.combinations(names, 2))


@pytest.mark.parametrize("seed", range(40))
def test_the_list_matches_an_independent_oracle_on_wild_names(v3_db, seed):
    rng = random.Random(seed)  # noqa: S311
    statuses = ["posted", "roundup", "too_old", "seeded", "pending", "failed"]
    worlds = []
    with closing(connect(v3_db)) as conn:
        for n in range(1, 61):
            world = {
                "n": n,
                "status": rng.choice(statuses),
                "from_roundup": rng.random() < 0.25,
                "ranked": [],
                "sightings": [],
            }
            with conn:
                _put_alerted(
                    conn,
                    n,
                    NOW - timedelta(hours=rng.randint(0, 30)),
                    world["status"],
                    from_roundup=world["from_roundup"],
                )
                if rng.random() < 0.4:
                    sources = [rng.choice(["", "", "table", "official"]) for _ in range(3)]
                    _put_details(
                        conn,
                        n,
                        NOW - timedelta(hours=rng.randint(0, 30)),
                        reward_source=sources[0],
                        category_source=sources[1],
                        expires_source=sources[2],
                    )
                    world["ranked"] = [s for s in sources if s]
            picked = {}
            for _ in range(rng.randint(0, 4)):
                name = rng.choice(POOL)
                picked[name] = picked.get(name, False) or rng.random() < 0.12
            for name, trusted in picked.items():
                roundup = rng.random() < 0.3
                _sight(conn, n, (name, trusted), roundup=roundup)
                world["sightings"].append((name, trusted, roundup))
            worlds.append(world)

        expected = {_code(w["n"]) for w in worlds if _oracle(w)}
        assert _listed(conn) == expected
        # Both outcomes have to occur, or the oracle isn't being asked anything.
        assert 0 < len(expected) < len(worlds)
