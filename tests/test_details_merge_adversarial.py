"""Adversarial tests for `shift/details.py` (design.md §16, plan §3.3).

These go beyond test_details_merge.py.

Angle (test-engineer brief, 2026-10-09): the precedence rules are stated in four sentences and
have to hold for every ordering of every mix of sources, which is exactly the sort of claim a
handful of hand-picked examples can't carry. So the first half is property-style: seeded random
sequences of observations checked against invariants (and, where the invariant is "the answer
doesn't depend on the order", against each other). The second half is the clock and Pacific
time: DST, year and leap-day rollovers, the 180-day instant, naive datetimes, expiries already
in the past.

Where the behavior is surprising it's pinned and labelled "documented". Real bugs are
`xfail(strict=True)` with the reason spelled out, so the day somebody fixes one the test says so.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.bot.format import details_line
from newsbot.collectors.base import ItemDetails
from newsbot.shift.details import (
    CLOCK_DAYS,
    RANK,
    SHIFT_TZ,
    DetailObservation,
    current_until,
    end_of_day,
    is_current,
    merge_details,
    merge_observations,
    sort_observations,
)
from newsbot.store.models import CodeDetailsRow

CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWX1"
T0 = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
LATER = T0 + timedelta(hours=1)
SINCE = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
SEEDS = range(300)

NAMES = {"table": ["mentalmars", "otherlist"], "official": ["Gearbox", "Borderlands (Bluesky)"]}
REWARDS = ["", "", "3 Golden Keys", "1 Golden Key", "A skin", "Gold Key (x2)"]
CATEGORIES = [None, None, "golden", "cosmetic", "permanent", "other"]
EXPIRIES = [None, None, date(2026, 10, 14), date(2026, 10, 20), date(2026, 11, 1), date(2027, 1, 1)]
FIELDS = (
    ("reward", "reward_source", "reward_from"),
    ("category", "category_source", "category_from"),
    ("expires_on", "expires_source", "expires_from"),
)


def rng_for(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 (a reproducible sequence, not a secret)


def make(rank, name, *, published_at=None, code=CODE, **fields):
    return DetailObservation(
        code=code,
        details=ItemDetails(code=code, rank=rank, **fields),
        source_name=name,
        published_at=published_at,
    )


def merge(old, o, *, now=T0, first_seen_at=None, since=SINCE):
    return merge_details(old, o, now=now, first_seen_at=first_seen_at, details_since=since)


def fold(observations, *, now=T0, first_seen_at=None, since=SINCE, start=None):
    """Sort a pass the way the store does, then fold it through `merge_observations`."""
    return merge_observations(
        start,
        sort_observations(observations),
        now=now,
        first_seen_at=first_seen_at,
        details_since=since,
    )


def random_pass(rng: random.Random, *, n: int | None = None, distinct_times=True):
    """A random mix of observations. With `distinct_times`, every `published_at` is unique."""
    n = n if n is not None else rng.randint(1, 9)
    minutes = rng.sample(range(1, 600), n)
    out = []
    for i in range(n):
        rank = rng.choice(["table", "official"])
        fields = {
            "reward": rng.choice(REWARDS),
            "category": rng.choice(CATEGORIES),
            "expires_on": rng.choice(EXPIRIES),
        }
        if rank == "table":
            fields["added_on"] = rng.choice([None, date(2026, 10, 1), date(2026, 10, 5)])
            fields["origin"] = rng.choice(["", "", "Twitch drop", "Shift event"])
            fields["listed"] = rng.random() < 0.7
        published = T0 - timedelta(minutes=minutes[i]) if distinct_times else None
        out.append(make(rank, rng.choice(NAMES[rank]), published_at=published, **fields))
    return out


def supplied(o: DetailObservation, field: str):
    """The value an observation supplies for `field`, or None for "didn't say"."""
    value = getattr(o.details, field)
    if field == "reward":
        return value if value.strip() else None
    return value


def expected_winner(observations, field):
    """(value, rank, name) a field should end on, from first principles, or None if never said.

    The highest rank that said anything wins; inside it, the last one in time order sets the
    value. The name is the first of the final run of readings that agree on that value: a
    source that merely repeats what's stored doesn't get its name on it (owner decision).
    """
    said = [o for o in sort_observations(observations) if supplied(o, field) is not None]
    if not said:
        return None
    top = max(RANK[o.details.rank] for o in said)
    at_top = [o for o in said if RANK[o.details.rank] == top]
    value = supplied(at_top[-1], field)
    run_start = len(at_top) - 1
    while run_start > 0 and supplied(at_top[run_start - 1], field) == value:
        run_start -= 1
    return value, top_name(top), at_top[run_start].source_name


def top_name(rank_number):
    return {v: k for k, v in RANK.items()}[rank_number]


def shown(row):
    return (row.reward, row.category, row.expires_on)


# A moment long before any date these tests use, so no expiry phrase is "expired", "tomorrow" or
# "in N hours": every distinct date reads as a distinct "expires Oct 14, 2026".
_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


def displayed(row):
    """What an alert shows: the details line itself, which is the only honest judge.

    QA batch 1, fix 14: `shift.details.displayed` is meant to be exactly what `details_line`
    prints, so a change is "a change" when the printed line differs. A category that never
    reaches the line (golden to cosmetic) is no change; the "via" names count because they
    are in the line. None means the line is left out.
    """
    return details_line(row, now=_LONG_AGO)


# --- precedence: the answer doesn't depend on the order, beyond "newer wins" ---


def test_property_every_field_ends_on_the_highest_rank_and_the_newest_inside_it():
    for seed in SEEDS:
        passes = random_pass(rng_for(seed))
        row = fold(passes)
        for field, source, origin in FIELDS:
            want = expected_winner(passes, field)
            got = (getattr(row, field), getattr(row, source), getattr(row, origin))
            if want is None:
                assert got in ((None, "", ""), ("", "", "")), f"seed {seed} {field}: {got}"
            else:
                assert got == want, f"seed {seed} {field}: {got} != {want}"


def test_property_the_order_fed_in_changes_nothing_when_times_differ():
    for seed in SEEDS:
        rng = rng_for(seed)
        passes = random_pass(rng)
        want = fold(passes)
        for _ in range(4):
            shuffled = passes[:]
            rng.shuffle(shuffled)
            assert fold(shuffled) == want, f"seed {seed}"


def test_property_official_never_loses_a_field_to_a_table():
    for seed in SEEDS:
        passes = random_pass(rng_for(seed))
        row = fold(passes)
        for field, source, _ in FIELDS:
            if any(supplied(o, field) is not None and o.details.rank == "official" for o in passes):
                assert getattr(row, source) == "official", f"seed {seed} {field}"


def test_property_a_field_that_has_a_value_never_goes_back_to_empty():
    for seed in SEEDS:
        rng = rng_for(seed)
        row = None
        had = {field: False for field, _, _ in FIELDS}
        for step, o in enumerate(random_pass(rng, n=rng.randint(2, 12))):
            row = merge(row, o, now=T0 + timedelta(minutes=step))
            for field, _, _ in FIELDS:
                value = getattr(row, field)
                present = value not in (None, "")
                assert present or not had[field], f"seed {seed} step {step}: {field} erased"
                had[field] = present


def test_property_ranks_never_go_down():
    for seed in SEEDS:
        rng = rng_for(seed)
        row, ranks = None, {f: 0 for f, _, _ in FIELDS}
        for step, o in enumerate(random_pass(rng, n=rng.randint(2, 12))):
            row = merge(row, o, now=T0 + timedelta(minutes=step))
            for field, source, _ in FIELDS:
                rank = RANK[getattr(row, source)]
                assert rank >= ranks[field], f"seed {seed} step {step}: {field} lost rank"
                ranks[field] = rank


def consistent_pass(rng: random.Random):
    """Observations whose sources never contradict each other inside a rank.

    Each rank has one opinion per field, and each observation voices some of them. That is the
    "stable page, polled every hour" case, where nothing should ever look like news.
    """
    opinions = {
        rank: {
            "reward": rng.choice(REWARDS[2:]),
            "category": rng.choice(CATEGORIES[2:]),
            "expires_on": rng.choice(EXPIRIES[2:]),
        }
        for rank in ("table", "official")
    }
    out = []
    for minute in rng.sample(range(1, 600), rng.randint(1, 9)):
        rank = rng.choice(["table", "official"])
        fields = {k: v for k, v in opinions[rank].items() if rng.random() < 0.6}
        if rank == "table":
            fields["listed"] = True
        name = rng.choice(NAMES[rank])
        out.append(make(rank, name, published_at=T0 - timedelta(minutes=minute), **fields))
    return out


def test_property_applying_the_last_observation_again_changes_nothing():
    # (The *last* one: an older reading re-applied after a newer same-rank one legitimately
    # wins again, because newer-by-arrival is the rule. See the "later pass" test below.)
    for seed in SEEDS:
        passes = random_pass(rng_for(seed))
        row = fold(passes)
        last = sort_observations(passes)[-1]
        again = merge(row, last, now=LATER)
        # Only the two clocks that say "when did we last look" may move.
        assert (
            replace(again, updated_at=row.updated_at, last_listed_at=row.last_listed_at) == row
        ), f"seed {seed}"


def test_property_a_stable_page_polled_again_is_a_no_op_including_details_changed_at():
    for seed in SEEDS:
        passes = consistent_pass(rng_for(seed))
        row = fold(passes)
        again = fold(passes, now=LATER, start=row)
        assert again.details_changed_at == row.details_changed_at, f"seed {seed}"
        assert (
            replace(again, updated_at=row.updated_at, last_listed_at=row.last_listed_at) == row
        ), f"seed {seed}"


def test_property_the_whole_pass_again_keeps_every_value_even_when_sources_disagree():
    # With contradicting readings the *values* still settle on the same answer pass after pass.
    for seed in SEEDS:
        passes = random_pass(rng_for(seed))
        row = fold(passes)
        again = fold(passes, now=LATER, start=row)
        assert shown(again) == shown(row), f"seed {seed}"
        assert (again.clock_start, again.first_listed_at) == (row.clock_start, row.first_listed_at)


def test_a_pass_whose_net_effect_is_nothing_does_not_move_details_changed_at():
    steady = fold([make("table", "B", expires_on=date(2026, 10, 20), published_at=T0)])
    flipflop = [
        make("table", "A", expires_on=date(2026, 10, 14), published_at=T0 - timedelta(minutes=5)),
        make("table", "B", expires_on=date(2026, 10, 20), published_at=T0),
    ]
    again = fold(flipflop, now=LATER, start=steady)
    assert again.expires_on == steady.expires_on
    assert again.details_changed_at == steady.details_changed_at


def test_property_splitting_a_timeline_into_passes_does_not_change_the_values():
    # Time moves forward between passes, so each pass's posts are newer than the last one's.
    for seed in SEEDS:
        rng = rng_for(seed)
        passes = sorted(random_pass(rng, n=rng.randint(3, 10)), key=lambda o: o.published_at)
        cuts = sorted(rng.sample(range(1, len(passes)), rng.randint(1, min(2, len(passes) - 1))))
        pieces = [passes[a:b] for a, b in zip([0, *cuts], [*cuts, len(passes)], strict=True)]
        row = None
        for i, piece in enumerate(pieces):
            row = fold(piece, now=T0 + timedelta(hours=i), start=row)
        once = fold(passes)
        for field, source, origin in FIELDS:
            assert (getattr(row, field), getattr(row, source), getattr(row, origin)) == (
                getattr(once, field),
                getattr(once, source),
                getattr(once, origin),
            ), f"seed {seed} {field}"
        assert (row.added_on, row.origin) == (once.added_on, once.origin), f"seed {seed}"


def test_property_details_changed_at_moves_exactly_when_a_shown_value_does():
    for seed in SEEDS:
        rng = rng_for(seed)
        row = None
        for step, o in enumerate(random_pass(rng, n=rng.randint(2, 10))):
            before = row
            now = T0 + timedelta(minutes=step)
            row = merge(row, o, now=now)
            if before is None:
                moved = row.details_changed_at == now
                assert moved == (displayed(row) is not None), f"seed {seed}"
            else:
                moved = row.details_changed_at != before.details_changed_at
                assert moved == (displayed(row) != displayed(before)), f"seed {seed} step {step}"


def test_property_added_on_and_origin_follow_tables_only_and_are_never_erased():
    for seed in SEEDS:
        passes = random_pass(rng_for(seed))
        row = fold(passes)
        tables = [o for o in sort_observations(passes) if o.details.rank == "table"]
        said_added = [o.details.added_on for o in tables if o.details.added_on]
        said_origin = [o.details.origin for o in tables if o.details.origin.strip()]
        assert row.added_on == (said_added[-1] if said_added else None), f"seed {seed}"
        assert row.origin == (said_origin[-1] if said_origin else ""), f"seed {seed}"


# --- precedence: the specific cases the brief names ---


def test_equal_publish_times_fall_back_to_input_order_documented():
    # Two readings with the same (or no) publish time: whichever was fed in last wins.
    a = make("table", "first", expires_on=date(2026, 10, 14))
    b = make("table", "second", expires_on=date(2026, 10, 20))
    assert fold([a, b]).expires_from == "second"
    assert fold([b, a]).expires_from == "first"


def test_a_table_correcting_its_expiry_earlier_then_later_follows_it_both_ways():
    row = merge(None, make("table", "mentalmars", expires_on=date(2026, 10, 20)))
    earlier = merge(row, make("table", "mentalmars", expires_on=date(2026, 10, 14)), now=LATER)
    assert earlier.expires_on == date(2026, 10, 14)
    assert earlier.details_changed_at == LATER
    much_later = LATER + timedelta(days=1)
    later = merge(
        earlier, make("table", "mentalmars", expires_on=date(2026, 11, 3)), now=much_later
    )
    assert later.expires_on == date(2026, 11, 3)
    assert later.details_changed_at == much_later
    assert current_until(later, None) == end_of_day(date(2026, 11, 3))


def test_a_table_cannot_correct_an_official_expiry_even_to_an_earlier_date():
    row = merge(None, make("official", "Gearbox", expires_on=date(2026, 10, 20)))
    after = merge(row, make("table", "mentalmars", expires_on=date(2026, 10, 14)), now=LATER)
    assert (after.expires_on, after.details_changed_at) == (date(2026, 10, 20), T0)


def test_a_table_that_goes_back_to_unknown_does_not_take_the_expiry_away():
    # The page changes "Oct 14, 2026" to "Unknown": §16 says a missing value never replaces
    # a present one, so the expiry (and who said it, and when it changed) stays.
    row = merge(None, make("table", "mentalmars", expires_on=date(2026, 10, 14), reward="A skin"))
    after = merge(row, make("table", "mentalmars", reward="A skin", listed=True), now=LATER)
    assert after.expires_on == date(2026, 10, 14)
    assert (after.expires_source, after.expires_from) == ("table", "mentalmars")
    assert after.details_changed_at == T0
    assert is_current(after, None, now=datetime(2026, 10, 15, 6, 0, tzinfo=UTC))


def test_a_reward_cleared_on_the_page_stays_and_a_whitespace_one_is_empty():
    row = merge(None, make("table", "mentalmars", reward="3 Golden Keys"))
    for blank in ("", " ", "\t\n", "\u00a0"):
        after = merge(row, make("table", "mentalmars", reward=blank), now=LATER)
        # NBSP counts as whitespace to str.strip(), so all four are "empty".
        assert after.reward == "3 Golden Keys", repr(blank)


def test_category_and_reward_are_decided_separately_documented():
    # Official says golden; the table, later, says the reward is a skin. Field by field means
    # the stored row is "golden" with a "skin" reward. That's the rule, odd as it looks.
    row = merge(None, make("official", "Gearbox", category="golden"))
    after = merge(row, make("table", "mentalmars", reward="A skin", category="cosmetic"), now=LATER)
    assert (after.category, after.category_source) == ("golden", "official")
    assert (after.reward, after.reward_source) == ("A skin", "table")


def test_a_tables_other_category_is_a_value_and_replaces_an_earlier_table_reading():
    # "other" is what the collector says when it can't tell; it still counts as saying something,
    # so a table going from "golden" to "other" does overwrite (same rank, newer wins).
    row = merge(None, make("table", "mentalmars", category="golden"))
    after = merge(row, make("table", "mentalmars", category="other"), now=LATER)
    assert after.category == "other"


def test_official_added_on_and_origin_are_ignored_even_when_the_table_has_none():
    row = merge(None, make("official", "Gearbox", added_on=date(2026, 10, 1), origin="Twitch"))
    assert (row.added_on, row.origin) == (None, "")
    row = merge(row, make("table", "mentalmars", added_on=date(2026, 10, 2), origin="Event"))
    again = merge(
        row, make("official", "Gearbox", added_on=date(2020, 1, 1), origin="x"), now=LATER
    )
    assert (again.added_on, again.origin) == (date(2026, 10, 2), "Event")


def test_a_whitespace_origin_does_not_erase_an_origin():
    row = merge(None, make("table", "m", origin="Event"))
    assert merge(row, make("table", "m", origin="   "), now=LATER).origin == "Event"


def test_listing_times_ignore_official_observations_with_listed_true():
    # A producer that sets listed=True on an official reading (it shouldn't) still counts:
    # `listed` is what the flag says, not what the rank implies. Pinned so nobody is surprised.
    row = merge(None, make("official", "Gearbox", reward="x", listed=True))
    assert row.first_listed_at == T0


def test_ranks_are_the_only_thing_that_decides_not_the_names():
    # Two different table sources: the newer reading wins whoever it is.
    row = merge(None, make("table", "zzz", reward="old"))
    after = merge(row, make("table", "aaa", reward="new"), now=LATER)
    assert (after.reward, after.reward_from) == ("new", "aaa")


def test_a_later_pass_wins_inside_a_rank_even_when_its_post_is_older_documented():
    # "Newer" is the order the bot read things, not the order they were written: an older post
    # that turns up in a later pass (a second official source, an outage catching up) overwrites
    # a newer one it can't see. Within one pass, publish time does decide. Worth a decision.
    newer_post = make("official", "Gearbox", published_at=T0, expires_on=date(2026, 10, 20))
    older_post = make(
        "official", "Steam", published_at=T0 - timedelta(days=3), expires_on=date(2026, 10, 14)
    )
    first = fold([newer_post])
    second = fold([older_post], now=LATER, start=first)
    assert (second.expires_on, second.expires_from) == (date(2026, 10, 14), "Steam")
    assert fold([newer_post, older_post]).expires_on == date(2026, 10, 20)


def test_sort_handles_naive_aware_and_missing_publish_times_together():
    a = make("table", "a", published_at=datetime(2026, 10, 9, 12, 0))  # naive, taken as UTC
    b = make("table", "b", published_at=datetime(2026, 10, 9, 5, 0, tzinfo=SHIFT_TZ))  # 12:00 UTC
    c = make("table", "c", published_at=None)
    d = make("table", "d", published_at=datetime(2026, 10, 9, 11, 59, tzinfo=UTC))
    # a and b are the same instant, so input order breaks the tie; c (unknown) sorts first.
    assert [o.source_name for o in sort_observations([a, b, c, d])] == ["c", "d", "a", "b"]


# --- the clock ---


def test_a_code_first_seen_one_microsecond_either_side_of_the_marker():
    before = merge(
        None, make("table", "m", reward="x"), first_seen_at=SINCE - timedelta(microseconds=1)
    )
    after = merge(
        None, make("table", "m", reward="x"), first_seen_at=SINCE + timedelta(microseconds=1)
    )
    assert before.clock_start == T0
    assert after.clock_start == SINCE + timedelta(microseconds=1)


def test_a_first_sighting_in_the_future_is_clamped_to_now():
    # Clock skew: alerted_codes says the code was first seen tomorrow. The clock starts no
    # later than the moment we're looking at it.
    future = T0 + timedelta(days=1)
    row = merge(None, make("table", "m", reward="x"), first_seen_at=future)
    assert row.clock_start == T0
    assert current_until(row, None) == T0 + timedelta(days=CLOCK_DAYS)


def test_the_marker_in_the_future_makes_every_known_code_fresh():
    row = merge(
        None, make("table", "m", reward="x"), first_seen_at=T0, since=T0 + timedelta(days=5)
    )
    assert row.clock_start == T0  # the listing time, because first_seen < marker


def test_an_old_code_re_mentioned_by_gearbox_twice_gets_one_fresh_clock_not_two():
    first = merge(
        None,
        make("official", "Gearbox", reward="3 Golden Keys"),
        first_seen_at=SINCE - timedelta(days=9),
    )
    second = merge(
        first,
        make("official", "Gearbox", reward="3 Golden Keys", expires_on=date(2026, 10, 30)),
        now=T0 + timedelta(days=20),
        first_seen_at=SINCE - timedelta(days=9),
    )
    assert first.clock_start == T0
    assert second.clock_start == T0  # the fresh clock was granted once, at row creation


def test_the_clock_start_is_always_aware_utc():
    row = merge(
        None,
        make("table", "m", reward="x"),
        now=T0.astimezone(SHIFT_TZ),
        first_seen_at=(SINCE + timedelta(hours=1)).astimezone(SHIFT_TZ),
        since=SINCE.astimezone(SHIFT_TZ),
    )
    assert row.clock_start == SINCE + timedelta(hours=1)
    assert row.clock_start.utcoffset() == timedelta(0)


# --- Pacific time ---


def test_end_of_day_sweeps_four_years_without_a_gap_or_a_double_midnight():
    d = date(2026, 1, 1)
    previous = end_of_day(d - timedelta(days=1))
    lengths = set()
    while d < date(2030, 1, 1):
        end = end_of_day(d)
        local = end.astimezone(SHIFT_TZ)
        assert (local.date(), local.hour, local.minute, local.second) == (
            d + timedelta(days=1),
            0,
            0,
            0,
        ), d
        lengths.add(end - previous)
        previous = end
        d += timedelta(days=1)
    assert lengths == {timedelta(hours=23), timedelta(hours=24), timedelta(hours=25)}


@pytest.mark.parametrize(
    ("expiry", "ends_at_utc"),
    [
        (date(2026, 12, 31), datetime(2027, 1, 1, 8, 0, tzinfo=UTC)),  # year rollover, PST
        (date(2027, 12, 31), datetime(2028, 1, 1, 8, 0, tzinfo=UTC)),
        (date(2028, 2, 28), datetime(2028, 2, 29, 8, 0, tzinfo=UTC)),  # leap year
        (date(2028, 2, 29), datetime(2028, 3, 1, 8, 0, tzinfo=UTC)),
        (date(2027, 2, 28), datetime(2027, 3, 1, 8, 0, tzinfo=UTC)),  # not a leap year
        (date(2100, 2, 28), datetime(2100, 3, 1, 8, 0, tzinfo=UTC)),  # century, not a leap year
        (date(2026, 3, 8), datetime(2026, 3, 9, 7, 0, tzinfo=UTC)),  # DST starts that day
        (date(2026, 11, 1), datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),  # DST ends that day
        (date(2026, 11, 2), datetime(2026, 11, 3, 8, 0, tzinfo=UTC)),
    ],
)
def test_end_of_day_known_instants(expiry, ends_at_utc):
    assert end_of_day(expiry) == ends_at_utc


@pytest.mark.parametrize(
    ("expiry", "last_ok", "first_gone"),
    [
        # the 23 hour day: midnight to midnight is 08:00 UTC to 07:00 UTC
        (
            date(2026, 3, 8),
            datetime(2026, 3, 9, 6, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 3, 9, 7, 0, tzinfo=UTC),
        ),
        # the 25 hour day
        (
            date(2026, 11, 1),
            datetime(2026, 11, 2, 7, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 11, 2, 8, 0, tzinfo=UTC),
        ),
    ],
)
def test_a_code_is_current_to_the_last_microsecond_of_its_pacific_day(expiry, last_ok, first_gone):
    row = CodeDetailsRow(code=CODE, clock_start=T0, updated_at=T0, expires_on=expiry)
    assert is_current(row, None, now=last_ok)
    assert not is_current(row, None, now=first_gone)


def test_pacific_evening_is_still_the_expiry_day_though_utc_has_turned_over():
    row = CodeDetailsRow(code=CODE, clock_start=T0, updated_at=T0, expires_on=date(2026, 10, 14))
    assert is_current(row, None, now=datetime(2026, 10, 15, 3, 0, tzinfo=UTC))  # 8 pm Oct 14 PDT
    assert not is_current(row, None, now=datetime(2026, 10, 15, 7, 0, tzinfo=UTC))


def test_the_180_day_boundary_is_exact_to_the_microsecond():
    row = CodeDetailsRow(code=CODE, clock_start=T0, updated_at=T0)
    edge = T0 + timedelta(days=180)
    assert is_current(row, None, now=edge - timedelta(microseconds=1))
    assert not is_current(row, None, now=edge)
    assert not is_current(row, None, now=edge + timedelta(microseconds=1))


def test_the_180_days_are_180_times_24_hours_whatever_dst_does_in_between_documented():
    # 2026-08-01 + 180 days crosses the November fall-back. The clock counts elapsed time
    # (4,320 hours), so in Pacific wall-clock terms the code ends an hour "early" on the clock
    # face. Nobody will notice, and it never hides a working code early by more than that.
    start = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)
    row = CodeDetailsRow(code=CODE, clock_start=start, updated_at=start)
    until = current_until(row, None)
    assert until - start == timedelta(hours=24 * 180)
    assert until.astimezone(SHIFT_TZ).hour == 10  # it started at 11:00 PDT


def test_a_naive_now_is_read_as_utc_not_rejected_documented():
    # Everything else in the store takes a naive datetime to be UTC, and so does this.
    row = CodeDetailsRow(code=CODE, clock_start=T0, updated_at=T0, expires_on=date(2026, 10, 14))
    for aware, naive in (
        (datetime(2026, 10, 15, 6, 59, tzinfo=UTC), datetime(2026, 10, 15, 6, 59)),
        (datetime(2026, 10, 15, 7, 0, tzinfo=UTC), datetime(2026, 10, 15, 7, 0)),
    ):
        assert is_current(row, None, now=naive) == is_current(row, None, now=aware)


def test_a_naive_clock_start_and_first_sighting_do_not_blow_up_the_comparison():
    row = CodeDetailsRow(code=CODE, clock_start=T0.replace(tzinfo=None), updated_at=T0)
    assert current_until(row, None) == T0 + timedelta(days=180)
    assert current_until(None, T0.replace(tzinfo=None)) == T0 + timedelta(days=180)
    assert is_current(None, T0.replace(tzinfo=None), now=T0 + timedelta(days=1))


def test_a_non_utc_now_is_converted_not_misread():
    row = CodeDetailsRow(code=CODE, clock_start=T0, updated_at=T0, expires_on=date(2026, 10, 14))
    pacific_midnight = datetime(2026, 10, 15, 0, 0, tzinfo=SHIFT_TZ)
    assert not is_current(row, None, now=pacific_midnight)
    assert is_current(row, None, now=pacific_midnight - timedelta(microseconds=1))


def test_an_expiry_already_in_the_past_is_stored_and_simply_not_current():
    past = date(2026, 10, 1)
    row = merge(None, make("table", "m", expires_on=past, reward="Old key"))
    assert row.expires_on == past
    assert row.details_changed_at == T0  # still a shown change, so an alert would say "expired"
    assert not is_current(row, None, now=T0)


def test_a_permanent_code_with_a_past_expiry_stays_current_and_a_later_fix_unpermanents_it():
    row = merge(None, make("table", "m", category="permanent", expires_on=date(2020, 1, 1)))
    assert is_current(row, None, now=T0)
    fixed = merge(row, make("official", "Gearbox", category="golden"), now=LATER)
    assert not is_current(fixed, None, now=LATER)  # the old expiry now applies


def test_the_last_representable_expiry_day_does_not_crash_the_clock():
    assert end_of_day(date(9999, 12, 30)) == datetime(9999, 12, 31, 8, 0, tzinfo=UTC)
    assert end_of_day(date.max) == datetime.max.replace(tzinfo=UTC)


def test_a_far_future_expiry_does_not_crash_current_until():
    row = CodeDetailsRow(code=CODE, clock_start=T0, updated_at=T0, expires_on=date.max)
    assert is_current(row, None, now=T0)
