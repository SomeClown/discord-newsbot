"""`merge_details` and the clock (design.md §16, plan §3.3): precedence, changes, current-until.

The function is pure, so these tests build rows and observations by hand and never
touch a database. (The store round trip is in test_repo_code_details.py.)
"""

from datetime import UTC, date, datetime, timedelta

import pytest

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
SINCE = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)  # the 009 marker
BEFORE_SINCE = SINCE - timedelta(days=3)
AFTER_SINCE = SINCE + timedelta(minutes=30)


def obs(rank="table", name="mentalmars", *, published_at=None, code=CODE, **fields):
    return DetailObservation(
        code=code,
        details=ItemDetails(code=code, rank=rank, **fields),
        source_name=name,
        published_at=published_at,
    )


def merge(old, o, *, now=T0, first_seen_at=None, since=SINCE):
    return merge_details(old, o, now=now, first_seen_at=first_seen_at, details_since=since)


def row(**fields):
    base = {"code": CODE, "clock_start": T0, "updated_at": T0}
    return CodeDetailsRow(**{**base, **fields})


# --- precedence, field by field ---

# (field, source column, from column, a value, a different value)
FIELDS = [
    ("reward", "reward_source", "reward_from", "3 Golden Keys", "1 Golden Key"),
    ("category", "category_source", "category_from", "golden", "cosmetic"),
    ("expires_on", "expires_source", "expires_from", date(2026, 10, 14), date(2026, 10, 20)),
]


@pytest.mark.parametrize(("field", "source", "origin", "value", "other"), FIELDS)
def test_a_new_row_takes_the_value_and_records_who_said_it(field, source, origin, value, other):
    merged = merge(None, obs("table", "mentalmars", **{field: value}))
    assert getattr(merged, field) == value
    assert (getattr(merged, source), getattr(merged, origin)) == ("table", "mentalmars")


@pytest.mark.parametrize(("field", "source", "origin", "value", "other"), FIELDS)
def test_official_beats_table(field, source, origin, value, other):
    first = merge(None, obs("table", "mentalmars", **{field: value}))
    merged = merge(first, obs("official", "Borderlands (Bluesky)", **{field: other}))
    assert getattr(merged, field) == other
    assert (getattr(merged, source), getattr(merged, origin)) == (
        "official",
        "Borderlands (Bluesky)",
    )


@pytest.mark.parametrize(("field", "source", "origin", "value", "other"), FIELDS)
def test_table_never_beats_official(field, source, origin, value, other):
    first = merge(None, obs("official", "Borderlands (Bluesky)", **{field: value}))
    merged = merge(first, obs("table", "mentalmars", **{field: other}))
    assert getattr(merged, field) == value
    assert (getattr(merged, source), getattr(merged, origin)) == (
        "official",
        "Borderlands (Bluesky)",
    )


@pytest.mark.parametrize("rank", ["table", "official"])
@pytest.mark.parametrize(("field", "source", "origin", "value", "other"), FIELDS)
def test_inside_a_rank_the_newer_reading_wins(field, source, origin, value, other, rank):
    first = merge(None, obs(rank, "Site A", **{field: value}))
    merged = merge(first, obs(rank, "Site B", **{field: other}), now=LATER)
    assert getattr(merged, field) == other
    assert (getattr(merged, source), getattr(merged, origin)) == (rank, "Site B")


@pytest.mark.parametrize("rank", ["table", "official"])
@pytest.mark.parametrize(("field", "source", "origin", "value", "other"), FIELDS)
def test_an_empty_value_never_erases_a_present_one(field, source, origin, value, other, rank):
    first = merge(None, obs(rank, "Site A", **{field: value}))
    # Same rank, nothing to say about this field: the old value, source and name all stay.
    merged = merge(first, obs(rank, "Site B"), now=LATER)
    assert getattr(merged, field) == value
    assert (getattr(merged, source), getattr(merged, origin)) == (rank, "Site A")


def test_a_whitespace_only_reward_counts_as_empty():
    first = merge(None, obs(reward="3 Golden Keys"))
    merged = merge(first, obs("official", "Gearbox", reward="   "), now=LATER)
    assert (merged.reward, merged.reward_source) == ("3 Golden Keys", "table")


def test_fields_are_independent_of_each_other():
    # Official says the expiry, the table says the reward: each keeps its own source.
    first = merge(None, obs("table", "mentalmars", reward="3 Golden Keys", category="golden"))
    merged = merge(first, obs("official", "Gearbox", expires_on=date(2026, 10, 14)), now=LATER)
    assert merged.reward == "3 Golden Keys" and merged.reward_from == "mentalmars"
    assert merged.expires_on == date(2026, 10, 14) and merged.expires_from == "Gearbox"
    assert merged.category == "golden" and merged.category_source == "table"


def test_any_rank_beats_nobody():
    assert RANK[""] < RANK["table"] < RANK["official"]
    merged = merge(row(), obs("table", "mentalmars", reward="A skin"))
    assert (merged.reward, merged.reward_source) == ("A skin", "table")


# --- added_on and origin (table only) ---


def test_added_on_and_origin_come_from_a_table_and_are_never_erased():
    first = merge(None, obs(added_on=date(2026, 10, 8), origin="Shift event", reward="A skin"))
    again = merge(first, obs(reward="A skin"), now=LATER)
    assert (again.added_on, again.origin) == (date(2026, 10, 8), "Shift event")
    corrected = merge(again, obs(added_on=date(2026, 10, 9), origin="Twitch drop"), now=LATER)
    assert (corrected.added_on, corrected.origin) == (date(2026, 10, 9), "Twitch drop")


def test_an_official_observation_leaves_added_on_and_origin_alone():
    first = merge(None, obs(added_on=date(2026, 10, 8), origin="Shift event"))
    merged = merge(first, obs("official", "Gearbox", reward="3 Golden Keys"), now=LATER)
    assert (merged.added_on, merged.origin) == (date(2026, 10, 8), "Shift event")


# --- details_changed_at moves only when a shown field changes ---


def test_a_new_row_with_something_to_show_is_marked_changed():
    assert merge(None, obs(reward="A skin")).details_changed_at == T0
    assert merge(None, obs(category="other")).details_changed_at == T0
    assert merge(None, obs(expires_on=date(2026, 10, 14))).details_changed_at == T0


def test_a_new_row_with_nothing_to_show_is_not():
    merged = merge(None, obs(added_on=date(2026, 10, 8), origin="Shift event", listed=True))
    assert merged.details_changed_at is None


def test_the_same_values_again_do_not_move_it():
    first = merge(None, obs(reward="A skin", category="cosmetic", expires_on=date(2026, 10, 14)))
    again = merge(
        first, obs(reward="A skin", category="cosmetic", expires_on=date(2026, 10, 14)), now=LATER
    )
    assert again.details_changed_at == T0
    assert again.updated_at == LATER


@pytest.mark.parametrize(("field", "source", "origin", "value", "other"), FIELDS)
def test_a_changed_shown_field_moves_it(field, source, origin, value, other):
    first = merge(None, obs(**{field: value}))
    assert merge(first, obs(**{field: other}), now=LATER).details_changed_at == LATER


def test_a_lower_rank_reading_that_is_ignored_does_not_move_it():
    first = merge(None, obs("official", "Gearbox", expires_on=date(2026, 10, 14)))
    ignored = merge(first, obs(expires_on=date(2026, 10, 20)), now=LATER)
    assert ignored.details_changed_at == T0


def test_a_higher_rank_confirming_a_value_takes_the_via_name_and_counts_as_a_change():
    # Gearbox repeating mentalmars' expiry: the line should now read "via Gearbox" (owner
    # decision, design.md §16: who supplied a detail is part of what an alert shows).
    first = merge(None, obs(expires_on=date(2026, 10, 14)))
    again = merge(first, obs("official", "Gearbox", expires_on=date(2026, 10, 14)), now=LATER)
    assert (again.expires_on, again.expires_source, again.expires_from) == (
        date(2026, 10, 14),
        "official",
        "Gearbox",
    )
    assert again.details_changed_at == LATER


def test_an_equal_or_lower_rank_agreeing_with_the_stored_value_keeps_the_name():
    first = merge(None, obs("table", "A", reward="same"))
    swapped = merge(first, obs("table", "B", reward="same"), now=LATER)
    assert (swapped.reward_from, swapped.details_changed_at) == ("A", T0)
    official = merge(first, obs("official", "Gearbox", reward="same"), now=LATER)
    lower = merge(official, obs("table", "B", reward="same"), now=LATER + timedelta(hours=1))
    assert (lower.reward_from, lower.reward_source, lower.details_changed_at) == (
        "Gearbox",
        "official",
        LATER,
    )


def test_added_on_origin_and_listing_times_do_not_move_it():
    first = merge(None, obs(reward="A skin"))
    again = merge(
        first, obs(reward="A skin", added_on=date(2026, 10, 1), origin="x", listed=True), now=LATER
    )
    assert again.details_changed_at == T0


# --- listing times ---


def test_first_listed_is_set_once_and_last_listed_follows_every_listing():
    first = merge(None, obs(reward="A skin", listed=True))
    assert (first.first_listed_at, first.last_listed_at) == (T0, T0)
    again = merge(first, obs(reward="A skin", listed=True), now=LATER)
    assert (again.first_listed_at, again.last_listed_at) == (T0, LATER)


def test_an_unlisted_observation_never_touches_the_listing_times():
    first = merge(None, obs("official", "Gearbox", reward="3 Golden Keys"))
    assert (first.first_listed_at, first.last_listed_at) == (None, None)
    listed = merge(first, obs(reward="3 Golden Keys", listed=True), now=LATER)
    after = merge(
        listed, obs("official", "Gearbox", reward="3 Golden Keys"), now=LATER + timedelta(hours=1)
    )
    assert (after.first_listed_at, after.last_listed_at) == (LATER, LATER)


# --- the clock: three starting points, then it never moves ---


def test_a_brand_new_code_starts_its_clock_now():
    assert merge(None, obs(reward="x"), first_seen_at=None).clock_start == T0


def test_a_code_first_seen_after_the_marker_keeps_its_first_sighting():
    merged = merge(None, obs(reward="x"), first_seen_at=AFTER_SINCE)
    assert merged.clock_start == AFTER_SINCE


def test_a_code_first_seen_exactly_at_the_marker_counts_as_after():
    assert merge(None, obs(reward="x"), first_seen_at=SINCE).clock_start == SINCE


def test_an_older_code_gets_the_listing_time_not_its_old_sighting():
    # The owner's rollout rule: everything on the list starts a fresh clock.
    merged = merge(None, obs(reward="x"), first_seen_at=BEFORE_SINCE)
    assert merged.clock_start == T0


def test_an_older_code_re_mentioned_by_an_official_post_also_gets_a_fresh_clock():
    merged = merge(
        None, obs("official", "Gearbox", reward="3 Golden Keys"), first_seen_at=BEFORE_SINCE
    )
    assert merged.clock_start == T0


def test_the_clock_is_set_at_creation_and_never_moves():
    first = merge(None, obs(reward="x"), first_seen_at=BEFORE_SINCE)
    for rank, name in (("table", "mentalmars"), ("official", "Gearbox")):
        again = merge(first, obs(rank, name, reward="y"), now=LATER, first_seen_at=AFTER_SINCE)
        assert again.clock_start == T0


def test_naive_datetimes_are_taken_to_be_utc():
    merged = merge(
        None,
        obs(reward="x"),
        now=T0.replace(tzinfo=None),
        first_seen_at=AFTER_SINCE.replace(tzinfo=None),
    )
    assert merged.clock_start == AFTER_SINCE
    assert merged.updated_at.tzinfo is UTC


# --- bad input ---


def test_a_mismatched_code_is_refused():
    bad = DetailObservation(
        code=CODE,
        details=ItemDetails(code="ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZ9", rank="table", reward="x"),
        source_name="mentalmars",
    )
    with pytest.raises(ValueError, match="carries details for"):
        merge(None, bad)


def test_a_code_that_is_not_29_characters_is_refused():
    with pytest.raises(ValueError, match="29 characters"):
        merge(None, obs(code="SHORT", reward="x"))


@pytest.mark.parametrize("field", ["reward", "origin"])
def test_text_over_300_characters_is_refused(field):
    with pytest.raises(ValueError, match="300"):
        merge(None, obs(**{field: "x" * 301}))
    merge(None, obs(**{field: "x" * 300}))  # exactly the cap is fine


def test_an_unknown_rank_is_refused():
    with pytest.raises(ValueError, match="rank"):
        merge(None, obs("press", reward="x"))


# --- ordering inside a pass ---


def test_sorted_by_publish_time_then_input_order_with_unknown_first():
    a = obs(name="a", published_at=T0)
    b = obs(name="b", published_at=T0 - timedelta(days=1))
    c = obs(name="c", published_at=None)
    d = obs(name="d", published_at=T0)
    assert [o.source_name for o in sort_observations([a, b, c, d])] == ["c", "b", "a", "d"]


def test_the_later_reading_in_a_pass_wins_inside_a_rank():
    old = obs(name="old", published_at=T0 - timedelta(days=2), expires_on=date(2026, 10, 14))
    new = obs(name="new", published_at=T0 - timedelta(days=1), expires_on=date(2026, 10, 20))
    folded = None
    for o in sort_observations([new, old]):
        folded = merge(folded, o)
    assert (folded.expires_on, folded.expires_from) == (date(2026, 10, 20), "new")


# --- end_of_day ---


def test_end_of_day_is_the_start_of_the_next_pacific_day_as_utc():
    assert end_of_day(date(2026, 10, 14)) == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)  # PDT
    assert end_of_day(date(2026, 12, 31)) == datetime(2027, 1, 1, 8, 0, tzinfo=UTC)  # PST


def test_end_of_day_across_both_dst_changes():
    # Spring forward (2026-03-08) is a 23 hour day; fall back (2026-11-01) is 25.
    spring = end_of_day(date(2026, 3, 8)) - end_of_day(date(2026, 3, 7))
    fall = end_of_day(date(2026, 11, 1)) - end_of_day(date(2026, 10, 31))
    ordinary = end_of_day(date(2026, 3, 9)) - end_of_day(date(2026, 3, 8))
    assert (spring, fall, ordinary) == (
        timedelta(hours=23),
        timedelta(hours=25),
        timedelta(hours=24),
    )
    # And the instants themselves: the day before spring forward ends at 08:00 UTC (PST),
    # the day of it ends at 07:00 UTC (PDT).
    assert end_of_day(date(2026, 3, 7)) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
    assert end_of_day(date(2026, 3, 8)) == datetime(2026, 3, 9, 7, 0, tzinfo=UTC)
    assert end_of_day(date(2026, 10, 31)) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert end_of_day(date(2026, 11, 1)) == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def test_the_tz_is_pacific():
    assert SHIFT_TZ.key == "America/Los_Angeles"


# --- current_until and is_current ---


def test_a_permanent_code_never_ends_even_with_an_expiry_on_file():
    permanent = row(category="permanent", expires_on=date(2020, 1, 1))
    assert current_until(permanent, T0) is None
    assert is_current(permanent, T0, now=T0 + timedelta(days=10_000))


def test_a_known_expiry_ends_with_that_pacific_day():
    r = row(expires_on=date(2026, 10, 14))
    assert current_until(r, T0) == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert is_current(r, T0, now=datetime(2026, 10, 15, 6, 59, 59, tzinfo=UTC))
    assert not is_current(r, T0, now=datetime(2026, 10, 15, 7, 0, tzinfo=UTC))


def test_a_known_expiry_beats_the_clock():
    r = row(expires_on=date(2026, 10, 14), clock_start=T0 - timedelta(days=500))
    assert current_until(r, T0 - timedelta(days=900)) == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)


def test_with_no_expiry_the_clock_runs_180_days_from_clock_start():
    r = row(clock_start=T0)
    assert CLOCK_DAYS == 180
    assert current_until(r, T0 - timedelta(days=400)) == T0 + timedelta(days=180)
    assert is_current(r, None, now=T0 + timedelta(days=179, hours=23))
    assert not is_current(r, None, now=T0 + timedelta(days=180))


def test_a_code_with_no_row_counts_from_its_first_sighting():
    seen = T0 - timedelta(days=100)
    assert current_until(None, seen) == seen + timedelta(days=180)
    assert is_current(None, seen, now=T0)
    assert not is_current(None, seen, now=seen + timedelta(days=181))


def test_no_row_and_no_sighting_is_an_error():
    with pytest.raises(ValueError, match="first sighting"):
        current_until(None, None)


# --- what the store would refuse, and the net-change rules ---


def test_a_category_outside_the_four_and_unencodable_or_nul_text_are_refused():
    for bad in (
        obs(category="legendary", reward="x"),
        obs(reward="ok\ud800"),
        obs(origin="o\0"),
        obs(name="bad\ud800name", reward="x"),
        obs(name="nul\0", reward="x"),
        obs(code="ABCDE-FGHIJ-KLMNO-PQRST-UVW\0X", reward="x"),
    ):
        with pytest.raises(ValueError):
            merge(None, bad)


def test_the_cap_is_measured_as_shown():
    merge(None, obs(reward="*" * 150))  # 300 units once escaped
    for text in ("*" * 151, "\U0001f600" * 151):
        with pytest.raises(ValueError, match="as shown"):
            merge(None, obs(reward=text))
        with pytest.raises(ValueError, match="as shown"):
            merge(None, obs(origin=text))


def test_dates_outside_2000_to_2100_are_dropped_as_unknown_and_the_rest_is_kept():
    for year in (1999, 2101, 1, 9999):
        row = merge(None, obs(reward="x", expires_on=date(year, 5, 1), added_on=date(year, 4, 1)))
        assert (row.reward, row.expires_on, row.added_on) == ("x", None, None), year
    kept = merge(None, obs(expires_on=date(2100, 12, 31), added_on=date(2000, 1, 1)))
    assert (kept.expires_on, kept.added_on) == (date(2100, 12, 31), date(2000, 1, 1))


def test_a_dropped_date_does_not_erase_a_good_one():
    first = merge(None, obs(expires_on=date(2026, 10, 14)))
    after = merge(first, obs(expires_on=date(9999, 1, 1)), now=LATER)
    assert after.expires_on == date(2026, 10, 14)


def test_the_change_stamp_always_moves_forward_and_the_clock_never_starts_in_the_future():
    first = merge(None, obs(reward="one"), now=LATER)
    after = merge(first, obs(reward="two"), now=T0)
    # The stamp is strictly forward (a change must look newer than the render that missed it);
    # `updated_at` is just "last touched", and holds still.
    assert (after.details_changed_at, after.updated_at) == (
        LATER + timedelta(microseconds=1),
        LATER,
    )
    future = merge(None, obs(reward="x"), first_seen_at=T0 + timedelta(days=3))
    assert future.clock_start == T0


def test_a_batch_that_ends_where_it_started_restores_the_stamp():
    start = merge(None, obs("table", "B", expires_on=date(2026, 10, 20)))
    flipflop = [
        obs("table", "A", expires_on=date(2026, 10, 14)),
        obs("table", "B", expires_on=date(2026, 10, 20)),
    ]
    row = merge_observations(start, flipflop, now=LATER, first_seen_at=None, details_since=SINCE)
    assert row.details_changed_at == T0
    assert row.updated_at == LATER


def test_a_batch_with_a_net_change_stamps_it_and_a_new_row_with_nothing_does_not():
    start = merge(None, obs("table", "B", expires_on=date(2026, 10, 20)))
    net = merge_observations(
        start,
        [
            obs("table", "A", expires_on=date(2026, 10, 14)),
            obs("table", "C", expires_on=date(2026, 10, 30)),
        ],
        now=LATER,
        first_seen_at=None,
        details_since=SINCE,
    )
    assert (net.expires_on, net.details_changed_at) == (date(2026, 10, 30), LATER)
    quiet = merge_observations(
        None, [obs(origin="Event")], now=T0, first_seen_at=None, details_since=SINCE
    )
    assert quiet.details_changed_at is None


def test_a_net_change_made_of_several_readings_is_one_microsecond_ahead_not_several():
    start = merge(None, obs("table", "B", expires_on=date(2026, 10, 20)), now=LATER)
    row = merge_observations(
        start,
        [
            obs("table", "A", expires_on=date(2026, 10, 14)),
            obs("table", "C", expires_on=date(2026, 10, 30)),
            obs("table", "D", expires_on=date(2026, 11, 3)),
        ],
        now=LATER,  # the clock hasn't moved since the last change
        first_seen_at=None,
        details_since=SINCE,
    )
    assert row.details_changed_at == LATER + timedelta(microseconds=1)


def test_merge_observations_needs_something_to_merge():
    with pytest.raises(ValueError, match="at least one"):
        merge_observations(None, [], now=T0, first_seen_at=None, details_since=SINCE)


def test_end_of_day_never_raises():
    assert end_of_day(date.max) == datetime.max.replace(tzinfo=UTC)
    assert end_of_day(date.min).year == 1
    assert is_current(row(expires_on=date.max), None, now=T0)
