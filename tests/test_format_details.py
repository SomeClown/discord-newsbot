"""Tests for the details line and its reservation (`bot.format`, design.md §16, plan task 6).

The persisted line is `` `reward` 3 Golden Keys · `expires` Oct 14, 2026 (<t:1792047600:R>)
· `via` mentalmars `` (the labels are chips), and the whole packing scheme rests on one promise:
`discord_len(details_line(x))` never exceeds what `details_reserve` set aside. The
property test at the bottom is that promise's witness; the rest pin each shape, the
timestamp's instant on both sides of both DST changes, the relative wording that
`/shift history` keeps (`relative=True`), and what bidirectional controls and mentions do
on their way in.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from newsbot.bot.format import details_line, details_reserve, discord_len, esc
from newsbot.shift.details import end_of_day
from newsbot.store.models import CodeDetailsRow

PT = ZoneInfo("America/Los_Angeles")
CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
NOON_UTC = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # 12:00 PDT, Oct 9


def _row(**kwargs) -> CodeDetailsRow:
    defaults = dict(code=CODE, clock_start=NOON_UTC, updated_at=NOON_UTC)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _line(now=NOON_UTC, **kwargs) -> str | None:
    return details_line(_row(**kwargs), now=now)


def _rel(now=NOON_UTC, **kwargs) -> str | None:
    """The `/shift history` flavor: relative wording, fresh on every call."""
    return details_line(_row(**kwargs), now=now, relative=True)


def _stamp(instant: datetime) -> int:
    return int(instant.timestamp())


# The instants below are worked out by hand (Pacific midnight, as UTC), not by
# `end_of_day`, so the tests don't agree with the code merely by sharing its mistakes.
OCT_14_ENDS = _stamp(datetime(2026, 10, 15, 7, 0, tzinfo=UTC))  # PDT, UTC-7


# --- shapes ---


def test_the_full_line():
    line = _line(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    assert OCT_14_ENDS == 1792047600
    assert line == (
        "`reward` 3 Golden Keys · `expires` Oct 14, 2026 (<t:1792047600:R>) · `via` mentalmars"
    )


def test_reward_only():
    assert _line(reward="3 Golden Keys", reward_from="mentalmars") == (
        "`reward` 3 Golden Keys · `via` mentalmars"
    )


def test_expiry_only():
    line = _line(expires_on=date(2026, 12, 31), expires_from="Gearbox Blog")
    assert line == "`expires` Dec 31, 2026 (<t:1798790400:R>) · `via` Gearbox Blog"


def test_nothing_known_is_no_line():
    assert details_line(None, now=NOON_UTC) is None
    assert _line() is None


def test_a_category_alone_is_not_enough_for_a_line():
    assert _line(category="golden", category_from="mentalmars") is None


def test_reward_with_no_source_name_has_no_via():
    assert _line(reward="3 Golden Keys") == "`reward` 3 Golden Keys"


def test_via_names_both_sources_when_reward_and_expiry_differ():
    line = _line(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="Borderlands (Bluesky)",
    )
    assert line.endswith("`via` mentalmars and Borderlands (Bluesky)")


def test_via_names_one_source_once_when_both_come_from_it():
    line = _line(
        reward="x",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    assert line.count("mentalmars") == 1


def test_via_skips_the_name_of_a_field_that_is_not_shown():
    # The reward's source supplied nothing that's on screen if there's no reward.
    line = _line(reward_from="mentalmars", expires_on=date(2026, 10, 14), expires_from="Gearbox")
    assert line == "`expires` Oct 14, 2026 (<t:1792047600:R>) · `via` Gearbox"


def test_permanent_code_says_expires_never_and_credits_whoever_set_the_category():
    line = _line(
        reward="Cosmetic head",
        reward_from="mentalmars",
        category="permanent",
        category_from="Gearbox Blog",
        expires_on=date(2026, 10, 14),  # a stray date; permanent wins
        expires_from="mentalmars",
    )
    assert line == "`reward` Cosmetic head · `expires` never · `via` mentalmars and Gearbox Blog"


def test_permanent_alone_is_enough_for_a_line():
    assert _line(category="permanent", category_from="mentalmars") == (
        "`expires` never · `via` mentalmars"
    )


def test_names_are_escaped():
    line = _line(reward="x", reward_from="@everyone <@1> *bold*")
    assert "@everyone" not in line.replace("@\u200beveryone", "")
    assert esc("@everyone <@1> *bold*") in line


# --- the relative wording `/shift history` keeps, in Pacific time ---


@pytest.mark.parametrize(
    ("now", "expires", "expected"),
    [
        # Same Pacific day: hours to the next midnight, rounded down.
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 9), "`expires` in 12 hours"),
        (datetime(2026, 10, 10, 6, 0, tzinfo=UTC), date(2026, 10, 9), "`expires` in 1 hour"),
        (
            datetime(2026, 10, 10, 6, 30, tzinfo=UTC),
            date(2026, 10, 9),
            "`expires` in under an hour",
        ),
        (
            datetime(2026, 10, 10, 6, 59, 59, tzinfo=UTC),
            date(2026, 10, 9),
            "`expires` in under an hour",
        ),
        # The day before.
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 10), "`expires` tomorrow"),
        # Pacific midnight, not UTC's, turns the day: 03:00 UTC on the 14th is still the 13th.
        (datetime(2026, 10, 14, 3, 0, tzinfo=UTC), date(2026, 10, 14), "`expires` tomorrow"),
        # Two days out is a date.
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 11), "`expires` Oct 11, 2026"),
        # Gone.
        (datetime(2026, 10, 10, 7, 0, tzinfo=UTC), date(2026, 10, 9), "`expired` Oct 9, 2026"),
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 1), "`expired` Oct 1, 2026"),
    ],
)
def test_expiry_wording(now, expires, expected):
    line = _rel(now=now, expires_on=expires)
    assert line == expected


def test_spring_forward_day_is_23_hours_long():
    # 2026-03-08 has no 02:00 in Pacific time. At 00:30 PST, 22.5 hours of it remain.
    now = datetime(2026, 3, 8, 0, 30, tzinfo=PT)
    assert _rel(now=now, expires_on=date(2026, 3, 8)) == "`expires` in 22 hours"


def test_fall_back_day_is_25_hours_long():
    # 2026-11-01 repeats 01:00. At 00:30 PDT, 24.5 hours of it remain.
    now = datetime(2026, 11, 1, 0, 30, tzinfo=PT)
    assert _rel(now=now, expires_on=date(2026, 11, 1)) == "`expires` in 24 hours"


def test_the_night_before_each_dst_change_is_tomorrow():
    assert _rel(now=datetime(2026, 3, 7, 23, 59, tzinfo=PT), expires_on=date(2026, 3, 8)) == (
        "`expires` tomorrow"
    )
    assert _rel(now=datetime(2026, 10, 31, 23, 59, tzinfo=PT), expires_on=date(2026, 11, 1)) == (
        "`expires` tomorrow"
    )


def test_a_naive_clock_is_taken_as_utc():
    naive = datetime(2026, 10, 9, 19, 0)
    assert _rel(now=naive, expires_on=date(2026, 10, 9)) == "`expires` in 12 hours"


def test_hours_phrase_carries_the_details_via_name():
    line = _rel(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 9),
        expires_from="Gearbox Blog",
    )
    assert line == (
        "`reward` 3 Golden Keys · `expires` in 12 hours · `via` mentalmars and Gearbox Blog"
    )


# --- the persisted wording: a date, then Discord's own live timestamp ---

TOKEN_RE = re.compile(r"<t:(\d+):R>")
RELATIVE_WORDS_RE = re.compile(r"expires` in|tomorrow|expired|\bhours?\b|\bago\b", re.IGNORECASE)


def test_the_token_has_the_exact_shape():
    line = _line(expires_on=date(2026, 10, 14), expires_from="x")
    assert line == "`expires` Oct 14, 2026 (<t:1792047600:R>) · `via` x"
    assert re.fullmatch(r"`expires` [A-Z][a-z]{2} \d{1,2}, \d{4} \(<t:\d{10}:R>\) · `via` x", line)


@pytest.mark.parametrize(
    ("due", "ends_at_utc"),
    [
        # Midnight at the end of a Pacific day, written as the UTC instant it is.
        (date(2026, 3, 7), datetime(2026, 3, 8, 8, 0, tzinfo=UTC)),  # PST, UTC-8
        (date(2026, 3, 8), datetime(2026, 3, 9, 7, 0, tzinfo=UTC)),  # spring forward: now PDT
        (date(2026, 10, 31), datetime(2026, 11, 1, 7, 0, tzinfo=UTC)),  # PDT, UTC-7
        (date(2026, 11, 1), datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),  # fall back: now PST
        (date(2026, 12, 31), datetime(2027, 1, 1, 8, 0, tzinfo=UTC)),  # year's end rolls over
    ],
)
def test_the_timestamp_is_the_end_of_the_pacific_day_across_dst_and_new_year(due, ends_at_utc):
    line = _line(expires_on=due)
    (stamp,) = TOKEN_RE.findall(line)
    assert int(stamp) == _stamp(ends_at_utc)
    assert int(stamp) == _stamp(end_of_day(due))  # the same instant the code expires at


def test_the_dst_days_are_23_and_25_hours_apart_in_the_token():
    def stamp(due):
        return int(TOKEN_RE.findall(_line(expires_on=due))[0])

    assert stamp(date(2026, 3, 8)) - stamp(date(2026, 3, 7)) == 23 * 3600
    assert stamp(date(2026, 11, 1)) - stamp(date(2026, 10, 31)) == 25 * 3600


def test_the_token_does_not_depend_on_the_clock():
    row = _row(expires_on=date(2026, 10, 14), expires_from="x")
    lines = {
        details_line(row, now=now)
        for now in (
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 10, 14, 20, tzinfo=UTC),
            datetime(2026, 10, 15, 6, 59, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            datetime(2026, 10, 9, 19, 0),  # naive
        )
    }
    assert len(lines) == 1


def test_no_relative_words_in_a_persisted_line_whatever_the_clock_says():
    # Today, tomorrow, the last hour, and long gone: all the same shape, no "in 6 hours".
    nows = [
        datetime(2026, 10, 9, 19, 0, tzinfo=UTC),
        datetime(2026, 10, 10, 6, 30, tzinfo=UTC),
        datetime(2026, 10, 14, 3, 0, tzinfo=UTC),
        datetime(2027, 3, 1, tzinfo=UTC),
    ]
    for now in nows:
        for due in (date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 14), date(2026, 10, 1)):
            line = _line(now=now, expires_on=due, reward="Skin", reward_from="x", expires_from="x")
            assert not RELATIVE_WORDS_RE.search(line), line
            assert len(TOKEN_RE.findall(line)) == 1


def test_a_permanent_code_gets_no_token():
    line = _line(category="permanent", category_from="mentalmars", expires_on=date(2026, 10, 14))
    assert line == "`expires` never · `via` mentalmars"


def test_no_expiry_means_no_token():
    assert TOKEN_RE.search(_line(reward="3 Golden Keys", reward_from="x")) is None


def test_a_date_too_far_out_for_eleven_digits_is_the_date_alone():
    # A hand-edited row; the readers stop at 2100. 9999-09-30's timestamp has 12 digits.
    line = _line(expires_on=date(9999, 9, 30), expires_from="x")
    assert line == "`expires` Sep 30, 9999 · `via` x"
    assert "<t:" not in line


def test_scraped_text_never_makes_a_token_but_the_bots_own_stays_live():
    forged = "<t:1792047600:R> and <t:1:R> and <t:1792047600>"
    line = _line(
        reward=forged,
        reward_from=forged,
        category_from=forged,
        expires_on=date(2026, 12, 31),
        expires_from=forged,
    )
    # Exactly one live token, and it's the bot's: the one after the date.
    assert TOKEN_RE.findall(line) == ["1798790400"]
    assert "`expires` Dec 31, 2026 (<t:1798790400:R>)" in line
    # The scraped ones are the same text with the "<" defused, so they render as text.
    assert "<\u200bt:1792047600:R>" in line
    assert line.count("<t:") == 1
    assert esc(forged) in line


def test_a_forged_token_in_a_reward_stays_defused_with_no_expiry_too():
    line = _line(reward="<t:1792047600:R>", reward_from="x")
    assert "<t:" not in line
    assert line == "`reward` <\u200bt:1792047600:R> · `via` x"


# --- hostile text ---

BIDI = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # man, ZWJ, woman, ZWJ, girl


def test_every_bidi_control_is_stripped_from_the_reward():
    reward = "".join(f"a{ch}" for ch in BIDI) + "z"
    line = _line(reward=reward)
    assert line == "`reward` " + "a" * len(BIDI) + "z"
    assert not any(ch in line for ch in BIDI)


def test_an_unclosed_override_cannot_reverse_the_via_names():
    line = _line(reward="Gold\u202een Keys", reward_from="mentalmars")
    assert line == "`reward` Golden Keys · `via` mentalmars"


def test_zwj_emoji_sequences_survive():
    line = _line(reward=f"{FAMILY} outfit \u202e{FAMILY}")
    assert line == f"`reward` {FAMILY} outfit {FAMILY}"


def test_other_invisible_characters_are_left_alone():
    # Only the bidi overrides and isolates go; a zero-width space, LRM and RLM are not on the list.
    reward = "a\u200bb\u200ec\u200fd"
    assert _line(reward=reward) == f"`reward` {reward}"


def test_a_reward_of_only_bidi_controls_is_no_reward():
    assert _line(reward="\u202e\u2066\u2069") is None
    assert _line(reward="\u202e", expires_on=date(2026, 12, 1), expires_from="x") == (
        "`expires` Dec 1, 2026 (<t:1796198400:R>) · `via` x"
    )


def test_a_bidi_control_cannot_hide_a_mention_from_the_escaper():
    line = _line(reward="@\u202eeveryone <@\u202e&123456789012345678>")
    assert "@everyone" not in line
    assert "@\u200beveryone" in line
    assert "<@&" not in line
    assert "<@\u200b&123456789012345678>" in line


def test_markdown_and_links_in_a_reward_are_escaped():
    line = _line(reward="**big** https://evil.example discord.gg/x")
    assert "**" not in line.replace("\\*\\*", "")
    assert "https://" not in line
    assert "discord.gg/" not in line


def test_a_reward_over_the_cap_is_cut_as_shown():
    line = _line(reward="*" * 400)  # 800 units once escaped
    assert discord_len(line.removeprefix("`reward` ")) <= 300
    assert line.endswith("…")


# --- the reservation ---


def test_reserve_arithmetic():
    # 1 newline + 9 "`reward` " + 300 reward + 3 " · " + 10 "`expires` " + 32 value (a 12-unit
    # date, " (<t:" + 11 digits + ":R>)") + 3 " · " + 6 "`via` " + V + 5 " and " + V
    assert details_reserve(["mentalmars", "Gearbox"]) == 369 + 10 + 10


def test_reserve_measures_names_as_shown():
    assert details_reserve(["a*"]) == details_reserve(["abc"])  # "a\*" is 3 units as shown
    assert details_reserve(["*" * 100]) == 369 + 200 + 200


def test_reserve_with_no_names_still_covers_a_line_without_via():
    assert details_reserve([]) == 369


def _hostile_texts(rng: random.Random):
    pieces = [
        "*",
        "_",
        "~",
        "`",
        "|",
        ">",
        "#",
        "@everyone",
        "<@123>",
        "<#9>",
        "https://",
        "discord.gg/",
        "\U0001f600",
        FAMILY,
        "\u202e",
        "\u2066",
        "\u200b",
        "x",
        " ",
        "\\",
        "[",
        "](",
        "\n",
    ]
    for _ in range(300):
        yield "".join(rng.choice(pieces) for _ in range(rng.randint(0, 600)))
    yield "\U0001f600" * 500
    yield "*" * 500
    yield "@everyone" * 80
    yield "\u202e*" * 300
    yield "https:\u202e//" * 100


def test_property_the_line_never_outgrows_the_reserve():
    rng = random.Random(20261009)  # noqa: S311 (a seeded shuffle, not a secret)
    names = ["m", "mentalmars", "*" * 100, "\U0001f600" * 100, "@everyone" * 11 + "abc"]
    reserve = details_reserve(names)
    longest = max(names, key=lambda n: discord_len(esc(n)))
    nows = [
        NOON_UTC,
        datetime(2026, 10, 10, 6, 30, tzinfo=UTC),
        datetime(2026, 3, 8, 0, 30, tzinfo=PT),
    ]
    expiries = [
        None,
        date(2026, 10, 9),
        date(2026, 10, 10),
        date(2026, 12, 31),
        date(2026, 10, 1),
        date(2000, 1, 1),  # a 9-digit timestamp
        date(5000, 12, 31),  # the widest: 11 digits, and the widest date
        date(9999, 9, 30),  # too far out for a token; the date alone
    ]
    checked = 0
    for text in _hostile_texts(rng):
        for now in nows:
            for expires in (rng.choice(expiries), date(2099, 9, 30), date(5000, 12, 31)):
                for category in (None, "permanent"):
                    row = _row(
                        reward=text,
                        reward_from=longest,
                        category=category,
                        category_from=longest,
                        expires_on=expires,
                        expires_from="m" if rng.random() < 0.5 else longest,
                    )
                    line = details_line(row, now=now)
                    if line is not None:
                        assert discord_len(line) + 1 <= reserve, (text, now, expires)
                        checked += 1
    assert checked > 1000


def test_the_worst_case_line_is_exactly_the_reserve():
    # An 11-digit timestamp is the widest one there is (the year 5000 gets there).
    name_a, name_b = "A" * 100, "B" * 100
    row = _row(
        reward="x" * 300,
        reward_from=name_a,
        expires_on=date(5000, 12, 31),
        expires_from=name_b,
    )
    line = details_line(row, now=NOON_UTC)
    stamp = _stamp(end_of_day(date(5000, 12, 31)))
    assert len(str(stamp)) == 11
    assert line == (
        f"`reward` {'x' * 300} · `expires` Dec 31, 5000 (<t:{stamp}:R>)"
        f" · `via` {name_a} and {name_b}"
    )
    assert discord_len(line) + 1 == details_reserve([name_a, name_b])


def test_a_ten_digit_timestamp_leaves_one_spare_unit():
    # Today's timestamps have 10 digits; the reserve keeps the 11th in hand.
    name_a, name_b = "A" * 100, "B" * 100
    row = _row(
        reward="x" * 300,
        reward_from=name_a,
        expires_on=date(2026, 9, 30),
        expires_from=name_b,
    )
    line = details_line(row, now=NOON_UTC)
    assert discord_len(line) + 1 == details_reserve([name_a, name_b]) - 1


def test_no_expiry_value_is_longer_than_the_reserve_assumes():
    dues = [
        date(2026, 9, 30),  # a date with a two-digit day, three-letter month
        date(2026, 12, 31),
        date(2000, 1, 1),  # 9 digits
        date(2100, 12, 31),
        date(5000, 12, 31),  # 11 digits
        date(9999, 9, 30),  # too far out for a token
    ]
    for due in dues:
        value = _line(expires_on=due).removeprefix("`expires` ")
        assert len(value) <= 32, value
    assert len(_line(expires_on=date(2026, 9, 30)).removeprefix("`expires` ")) == 31
    assert len(_line(expires_on=date(5000, 12, 31)).removeprefix("`expires` ")) == 32


# --- an over-long stored `via` name is cut at render time ---


def test_a_via_name_stored_under_an_older_longer_limit_is_cut_when_drawn():
    line = _line(reward="r", reward_from="N" * 6000, expires_on=date(2026, 10, 14))
    assert discord_len(line) < 500
    assert line.endswith("…")
    assert "N" * 99 in line and "N" * 101 not in line


def test_a_100_character_via_name_is_drawn_whole():
    line = _line(reward="r", reward_from="N" * 100)
    assert line.endswith("`via` " + "N" * 100)


def test_the_reserve_still_covers_the_worst_line_with_cut_names():
    row = _row(
        reward="*" * 150,
        reward_from="*" * 6000,
        expires_on=date(2026, 10, 14),
        expires_from="_" * 6000,
    )
    names = ["*" * 6000, "_" * 6000, "short"]
    assert discord_len(details_line(row, now=NOON_UTC)) <= details_reserve(names)
