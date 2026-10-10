"""Tests for newsbot.shift.official: the careful reader of Gearbox's own posts.

The reader's whole personality is "when in doubt, say nothing", so half of
these are cases where it should hand back None, and the other half are the
plain patterns it's allowed to be sure about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from newsbot.collectors.base import ItemDetails
from newsbot.shift.official import read_official_details

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
PUBLISHED = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)  # Oct 6, 10:00 Pacific


def _read(text, published_at=PUBLISHED, **kwargs):
    return read_official_details(f"{text}\n{CODE}", CODE, published_at, **kwargs)


# --- the Golden Key count ---


@pytest.mark.parametrize(
    ("text", "reward"),
    [
        ("Redeem 3 Golden Keys", "3 Golden Keys"),
        ("Here is a Golden Key for you", "1 Golden Key"),
        ("grab an golden key", "1 Golden Key"),
        ("Five Golden Keys, on us", "5 Golden Keys"),
        ("10 GOLDEN KEYS", "10 Golden Keys"),
        ("1 Golden Key", "1 Golden Key"),
        ("2 Golden-Keys", "2 Golden Keys"),
        ("three golden\nkeys", "3 Golden Keys"),
    ],
)
def test_a_golden_key_count_sets_reward_and_category(text, reward):
    details = _read(text)
    assert details == ItemDetails(code=CODE, rank="official", reward=reward, category="golden")


def test_the_same_count_said_twice_in_different_words_is_still_one_count():
    details = _read("3 Golden Keys! That's three golden keys, folks.")
    assert details is not None
    assert details.reward == "3 Golden Keys"


@pytest.mark.parametrize(
    "text",
    [
        "3 Golden Keys now, 5 Golden Keys later",
        "a Golden Key or two Golden Keys",
        "0 Golden Keys",
        "Golden Keys for everyone",
        "a golden keyboard",
        "2x Golden Keys",
    ],
)
def test_it_gives_up_on_an_unclear_count(text):
    assert _read(text) is None


# --- the expiry ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Expires October 14", date(2026, 10, 14)),
        ("expires Oct 14", date(2026, 10, 14)),
        ("Expiring Oct 14th", date(2026, 10, 14)),
        ("valid until Oct 14, 2026", date(2026, 10, 14)),
        ("Valid  until\nOctober 14, 2026", date(2026, 10, 14)),
        ("valid through 10/14/2026", date(2026, 10, 14)),
        ("redeemable through Oct 14", date(2026, 10, 14)),
        ("good through Oct 14", date(2026, 10, 14)),
        ("expires: Oct. 14, 2026", date(2026, 10, 14)),
        ("expires on Wednesday, October 14", date(2026, 10, 14)),
        ("expires on Wed, Oct 14", date(2026, 10, 14)),
        ("expires Sept 30, 2027", date(2027, 9, 30)),
        ("redeem it, good through Dec 31, 2027 at the latest", date(2027, 12, 31)),
        ("expires Oct 14, 10:00 AM PT", date(2026, 10, 14)),
    ],
)
def test_an_expiry_with_a_month_name_or_a_full_numeric_date(text, expected):
    """The weekday cases used to say "Tuesday, October 14", which the reader happily took.

    Oct 14, 2026 is a Wednesday, so that post was wrong about one of its own facts, and the
    reader now gives up on it (`test_a_weekday_that_does_not_match_the_date_gives_no_expiry`).
    These two say the right weekday.
    """
    details = _read(text)
    assert details == ItemDetails(code=CODE, rank="official", expires_on=expected)


def test_a_count_and_an_expiry_in_one_post_come_back_together():
    details = _read("3 Golden Keys, expires October 14")
    assert details == ItemDetails(
        code=CODE,
        rank="official",
        reward="3 Golden Keys",
        category="golden",
        expires_on=date(2026, 10, 14),
    )


def test_the_same_date_said_twice_is_one_date():
    details = _read("expires Oct 14. Seriously, valid until October 14, 2026.")
    assert details is not None
    assert details.expires_on == date(2026, 10, 14)


def test_a_date_without_a_year_takes_the_next_occurrence_after_the_publish_day():
    assert _read("expires October 14").expires_on == date(2026, 10, 14)
    # Published Oct 25: Oct 14 passed eleven days ago, so it means next year's.
    late = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
    assert _read("expires October 14", late).expires_on == date(2027, 10, 14)


def test_a_year_less_date_in_the_week_before_the_publish_day_is_not_rolled_over():
    # Six days back is more likely a typo or past tense than a deadline eleven months out.
    recent = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)
    assert _read("expires October 14", recent) is None
    assert _read("expires October 13", recent) is None  # exactly seven days back
    assert _read("expires October 12", recent).expires_on == date(2027, 10, 12)
    # With a year, the date is read as written, but not if it falls before the post's own day
    # (QA batch 1, fix 1: a code announced already dead is a misread).
    assert _read("expires October 14, 2026", recent) is None
    assert _read("expires October 25, 2026", recent).expires_on == date(2026, 10, 25)


def test_a_year_less_date_just_after_new_year_looks_back_across_the_year_boundary():
    jan_3 = datetime(2027, 1, 3, 20, 0, tzinfo=UTC)
    assert _read("expires December 30", jan_3) is None
    assert _read("expires December 20", jan_3).expires_on == date(2027, 12, 20)


def test_a_date_on_the_publish_day_itself_is_today_not_next_year():
    assert _read("expires October 6").expires_on == date(2026, 10, 6)


def test_the_publish_day_is_a_pacific_day():
    # 02:00 UTC on Oct 7 is still the evening of Oct 6 in Los Angeles.
    evening = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    assert _read("expires October 6", evening).expires_on == date(2026, 10, 6)


def test_a_year_less_leap_day_waits_for_a_leap_year():
    pub = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)
    assert _read("valid until Feb 29", pub).expires_on == date(2028, 2, 29)


def test_a_year_less_date_with_no_publish_time_uses_now():
    now = datetime(2026, 10, 30, 17, 0, tzinfo=UTC)
    assert _read("expires October 14", None, now=now).expires_on == date(2027, 10, 14)


def test_a_year_less_date_with_nothing_to_anchor_it_is_not_read():
    assert _read("expires October 14", None) is None


def test_a_full_date_needs_no_anchor():
    assert _read("expires October 14, 2026", None).expires_on == date(2026, 10, 14)


@pytest.mark.parametrize(
    "text",
    [
        "expires 10/14",
        "expires 10/14/26",
        "expires 14/10/2026",
        "expires 1/2/3/4",
        "through the SHiFT site",
        "expired October 14",
        "expires soon",
        "valid until Feb 30",
        "expires Feb 30, 2026",
        "expires Oct 14, 1999",
        "expires Oct 14, 2101",
        "expires in 3 days",
        "expires October 14 or maybe through October 21",
        "valid until Oct 14, 2026 and expires 10/21/2026",
        "expires 10/14, valid until Oct 20, 2026",
        "Oct 14 is the day (no keyword)",
    ],
)
def test_it_gives_up_on_a_date_it_cannot_be_sure_of(text):
    assert _read(text) is None


def test_two_dates_make_it_drop_the_expiry_but_a_clear_count_survives():
    details = _read("2 Golden Keys, expires Oct 14 or through Oct 21")
    assert details == ItemDetails(
        code=CODE, rank="official", reward="2 Golden Keys", category="golden"
    )


def test_a_post_with_nothing_to_read_gives_none():
    assert _read("Enjoy the new code, vault hunters.") is None


def test_the_code_on_the_details_is_the_one_asked_about():
    other = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
    details = read_official_details(f"a Golden Key {other}", other, PUBLISHED)
    assert details is not None
    assert details.code == other


def test_hostile_text_is_read_in_bounded_time():
    junk = "expires " * 20000 + "valid\n" * 20000 + "golden " * 20000
    assert read_official_details(junk, CODE, PUBLISHED) is None


# --- the count has to be plainly a quantity ---


@pytest.mark.parametrize(
    "text",
    [
        "1 Golden Keys",
        "a Golden Keys",
        "3 Golden Key",
        "Borderlands 4 Golden Keys",
        "Patch 2 Golden Keys",
        "Season 3 Golden Keys",
        "Part 2 Golden Keys",
        "Episode 4 Golden Keys",
        "Day 5 Golden Keys",
        "Week 2 Golden Keys",
        "Sept 14 Golden Keys",
        "Oct. 14 Golden Keys",
        "a Golden Key code",
        "a Golden Key SHiFT code",
        "3 Golden Keys code",
        "1:2 Golden Keys",
        "9-5 Golden Keys",
        "9–5 Golden Keys",
        "1,5 Golden Keys",
        "5 for PC, 3 for Xbox, 3 for PlayStation",
        "5 on PC and 3 on Switch",
        "2 for the PS5",
    ],
)
def test_a_number_that_is_not_plainly_the_quantity_gives_no_count(text):
    assert _read(text) is None


def test_a_name_before_one_count_doesnt_hide_a_real_count_later():
    details = _read("New Borderlands 4 Golden Key code! Redeem 3 Golden Keys")
    assert details is not None
    assert details.reward == "3 Golden Keys"


def test_a_code_label_on_the_next_line_isnt_a_compound_noun():
    details = _read("Redeem 3 Golden Keys\nSHiFT code:")
    assert details is not None
    assert details.reward == "3 Golden Keys"


def test_the_number_after_a_bullet_or_a_colon_with_a_space_is_still_a_count():
    assert _read("- 3 Golden Keys").reward == "3 Golden Keys"
    assert _read("Reward: 3 Golden Keys").reward == "3 Golden Keys"


# --- an expiry the post takes back, or only half of ---


@pytest.mark.parametrize(
    "text",
    [
        "Not valid until Oct 14",
        "this code isn't valid until 10/14/2026",
        "this code isn’t valid until Oct 14",
        "It is not valid until October 14, 2026",
        "That won't be valid until Oct 14",
        "Not yet valid until Oct 14",
        "doesn't expire until Oct 14",
        "valid from Oct 7, expires Oct 14",
        "Valid  from Oct 7 until Oct 14, 2026. Expires Oct 14",
        "The event starts Oct 7. Code expires Oct 14",
    ],
)
def test_a_negation_or_a_start_date_gives_no_expiry(text):
    assert _read(text) is None


def test_a_negation_in_an_earlier_sentence_does_not_cancel_the_expiry():
    assert _read("Don't miss out! Expires Oct 14").expires_on == date(2026, 10, 14)
    assert _read("Don't wait, expires Oct 14").expires_on == date(2026, 10, 14)
    assert _read("Not for long: expires Oct 14").expires_on == date(2026, 10, 14)


@pytest.mark.parametrize(
    "text",
    [
        "expires Oct 14 or 15",
        "expires Oct 14 or maybe through Oct 21",
        "expires Oct 14 and Oct 15",
        "expires Oct 14 - Oct 21",
        "expires Oct 14-21",
        "expires Oct 14 – Oct 21",
        "expires Oct 14–21",
        "expires October 14/15",
        "expires Oct 14 to 21",
        "expires Oct 14 to Oct 21",
        "expires Oct 14 through the 21st",
        "expires Oct 14 until Oct 21",
        "expires 10/14/2026-10/21/2026",
        "expires 10/14/2026 or 10/21/2026",
        "expires Oct 14, 2026 - Oct 21, 2026",
        "expires Oct 14, 2026 & Oct 21, 2026",
    ],
)
def test_a_range_or_an_alternative_gives_no_expiry(text):
    assert _read(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "expires Oct 14 to redeem it",
        "expires Oct 14 - hurry",
        "expires Oct 14, so hurry",
        "expires Oct 14 and then it's gone",
        "expires Oct 14 or earlier",
        "expires Oct 14\n10/21 is something else",
    ],
)
def test_words_after_a_date_that_arent_another_date_leave_it_alone(text):
    assert _read(text).expires_on == date(2026, 10, 14)


# --- "through" has to be "valid/redeemable/good through" ---


@pytest.mark.parametrize(
    "text",
    [
        "Event runs through Oct 20",
        "through Oct 14",
        "through 10/14/2026",
        "available through October 14",
        "Playing through Oct 14",
    ],
)
def test_a_bare_through_is_not_an_expiry(text):
    assert _read(text) is None


def test_a_bare_through_beside_a_real_keyword_is_ignored():
    assert _read("Event runs through Oct 20. Expires Oct 14").expires_on == date(2026, 10, 14)


# --- a weekday has to agree with its date ---


@pytest.mark.parametrize(
    "text",
    [
        "expires on Tuesday, October 14",
        "expires on Tue, Oct 14",
        "expires Monday, October 14, 2026",
        "valid until Friday 10/14/2026",
        "expires Sunday Oct 14",
    ],
)
def test_a_weekday_that_does_not_match_the_date_gives_no_expiry(text):
    assert _read(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "expires Wednesday, October 14",
        "expires Wed Oct 14",
        "expires Wed., Oct 14, 2026",
        "valid until Wednesday 10/14/2026",
        "expires on Wednesday Oct 14th",
    ],
)
def test_a_weekday_that_matches_the_date_is_fine(text):
    assert _read(text).expires_on == date(2026, 10, 14)


def test_a_year_less_weekday_is_checked_against_the_year_it_rolled_to():
    # Oct 14, 2027 is a Thursday. Published 11 days after Oct 14, 2026, the date rolls to 2027.
    late = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
    assert _read("expires Thursday, October 14", late).expires_on == date(2027, 10, 14)
    assert _read("expires Wednesday, October 14", late) is None
