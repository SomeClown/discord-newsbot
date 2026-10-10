"""Tests for the details line and its reservation (`bot.format`, design.md §16, plan task 6).

The line is `3 Golden Keys · expires Oct 14, 2026 · via mentalmars`, and the whole
packing scheme rests on one promise: `discord_len(details_line(x))` never exceeds what
`details_reserve` set aside. The property test at the bottom is that promise's
witness; the rest pin each shape, the 48-hour wording on both sides of both DST
changes, and what bidirectional controls and mentions do on their way in.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from newsbot.bot.format import details_line, details_reserve, discord_len, esc
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


# --- shapes ---


def test_the_full_line():
    line = _line(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    assert line == "3 Golden Keys · expires Oct 14, 2026 · via mentalmars"


def test_reward_only():
    assert _line(reward="3 Golden Keys", reward_from="mentalmars") == (
        "3 Golden Keys · via mentalmars"
    )


def test_expiry_only():
    line = _line(expires_on=date(2026, 12, 31), expires_from="Gearbox Blog")
    assert line == "expires Dec 31, 2026 · via Gearbox Blog"


def test_nothing_known_is_no_line():
    assert details_line(None, now=NOON_UTC) is None
    assert _line() is None


def test_a_category_alone_is_not_enough_for_a_line():
    assert _line(category="golden", category_from="mentalmars") is None


def test_reward_with_no_source_name_has_no_via():
    assert _line(reward="3 Golden Keys") == "3 Golden Keys"


def test_via_names_both_sources_when_reward_and_expiry_differ():
    line = _line(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="Borderlands (Bluesky)",
    )
    assert line.endswith("via mentalmars and Borderlands (Bluesky)")


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
    assert line == "expires Oct 14, 2026 · via Gearbox"


def test_permanent_code_says_permanent_and_credits_whoever_set_the_category():
    line = _line(
        reward="Cosmetic head",
        reward_from="mentalmars",
        category="permanent",
        category_from="Gearbox Blog",
        expires_on=date(2026, 10, 14),  # a stray date; permanent wins
        expires_from="mentalmars",
    )
    assert line == "Cosmetic head · permanent · via mentalmars and Gearbox Blog"


def test_permanent_alone_is_enough_for_a_line():
    assert _line(category="permanent", category_from="mentalmars") == "permanent · via mentalmars"


def test_names_are_escaped():
    line = _line(reward="x", reward_from="@everyone <@1> *bold*")
    assert "@everyone" not in line.replace("@\u200beveryone", "")
    assert esc("@everyone <@1> *bold*") in line


# --- the 48-hour wording, in Pacific time ---


@pytest.mark.parametrize(
    ("now", "expires", "expected"),
    [
        # Same Pacific day: hours to the next midnight, rounded down.
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 9), "expires in 12 hours"),
        (datetime(2026, 10, 10, 6, 0, tzinfo=UTC), date(2026, 10, 9), "expires in 1 hour"),
        (datetime(2026, 10, 10, 6, 30, tzinfo=UTC), date(2026, 10, 9), "expires in under an hour"),
        (
            datetime(2026, 10, 10, 6, 59, 59, tzinfo=UTC),
            date(2026, 10, 9),
            "expires in under an hour",
        ),
        # The day before.
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 10), "expires tomorrow"),
        # Pacific midnight, not UTC's, turns the day: 03:00 UTC on the 14th is still the 13th.
        (datetime(2026, 10, 14, 3, 0, tzinfo=UTC), date(2026, 10, 14), "expires tomorrow"),
        # Two days out is a date.
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 11), "expires Oct 11, 2026"),
        # Gone.
        (datetime(2026, 10, 10, 7, 0, tzinfo=UTC), date(2026, 10, 9), "expired"),
        (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), date(2026, 10, 1), "expired"),
    ],
)
def test_expiry_wording(now, expires, expected):
    line = _line(now=now, expires_on=expires)
    assert line == expected


def test_spring_forward_day_is_23_hours_long():
    # 2026-03-08 has no 02:00 in Pacific time. At 00:30 PST, 22.5 hours of it remain.
    now = datetime(2026, 3, 8, 0, 30, tzinfo=PT)
    assert _line(now=now, expires_on=date(2026, 3, 8)) == "expires in 22 hours"


def test_fall_back_day_is_25_hours_long():
    # 2026-11-01 repeats 01:00. At 00:30 PDT, 24.5 hours of it remain.
    now = datetime(2026, 11, 1, 0, 30, tzinfo=PT)
    assert _line(now=now, expires_on=date(2026, 11, 1)) == "expires in 24 hours"


def test_the_night_before_each_dst_change_is_tomorrow():
    assert _line(now=datetime(2026, 3, 7, 23, 59, tzinfo=PT), expires_on=date(2026, 3, 8)) == (
        "expires tomorrow"
    )
    assert _line(now=datetime(2026, 10, 31, 23, 59, tzinfo=PT), expires_on=date(2026, 11, 1)) == (
        "expires tomorrow"
    )


def test_a_naive_clock_is_taken_as_utc():
    naive = datetime(2026, 10, 9, 19, 0)
    assert _line(now=naive, expires_on=date(2026, 10, 9)) == "expires in 12 hours"


def test_hours_phrase_carries_the_via_name():
    line = _line(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 9),
        expires_from="Gearbox Blog",
    )
    assert line == "3 Golden Keys · expires in 12 hours · via mentalmars and Gearbox Blog"


# --- hostile text ---

BIDI = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # man, ZWJ, woman, ZWJ, girl


def test_every_bidi_control_is_stripped_from_the_reward():
    reward = "".join(f"a{ch}" for ch in BIDI) + "z"
    line = _line(reward=reward)
    assert line == "a" * len(BIDI) + "z"
    assert not any(ch in line for ch in BIDI)


def test_an_unclosed_override_cannot_reverse_the_via_names():
    line = _line(reward="Gold\u202een Keys", reward_from="mentalmars")
    assert line == "Golden Keys · via mentalmars"


def test_zwj_emoji_sequences_survive():
    line = _line(reward=f"{FAMILY} outfit \u202e{FAMILY}")
    assert line == f"{FAMILY} outfit {FAMILY}"


def test_other_invisible_characters_are_left_alone():
    # Only the bidi overrides and isolates go; a zero-width space, LRM and RLM are not on the list.
    reward = "a\u200bb\u200ec\u200fd"
    assert _line(reward=reward) == reward


def test_a_reward_of_only_bidi_controls_is_no_reward():
    assert _line(reward="\u202e\u2066\u2069") is None
    assert _line(reward="\u202e", expires_on=date(2026, 12, 1), expires_from="x") == (
        "expires Dec 1, 2026 · via x"
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
    assert discord_len(line) <= 300
    assert line.endswith("…")


# --- the reservation ---


def test_reserve_arithmetic():
    # 1 newline + 300 reward + 3 + 24 expiry + 7 " · via " + V + 5 " and " + V
    assert details_reserve(["mentalmars", "Gearbox"]) == 1 + 300 + 3 + 24 + 7 + 10 + 5 + 10


def test_reserve_measures_names_as_shown():
    assert details_reserve(["a*"]) == details_reserve(["abc"])  # "a\*" is 3 units as shown
    assert details_reserve(["*" * 100]) == 1 + 300 + 3 + 24 + 7 + 200 + 5 + 200


def test_reserve_with_no_names_still_covers_a_line_without_via():
    assert details_reserve([]) == 1 + 300 + 3 + 24 + 7 + 5


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
    expiries = [None, date(2026, 10, 9), date(2026, 10, 10), date(2026, 12, 31), date(2026, 10, 1)]
    checked = 0
    for text in _hostile_texts(rng):
        for now in nows:
            for expires in (rng.choice(expiries), date(2099, 9, 30)):
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
    name_a, name_b = "A" * 100, "B" * 100
    row = _row(
        reward="x" * 300,
        reward_from=name_a,
        expires_on=date(2026, 10, 9),
        expires_from=name_b,
    )
    line = details_line(row, now=datetime(2026, 10, 10, 6, 30, tzinfo=UTC))
    assert line == f"{'x' * 300} · expires in under an hour · via {name_a} and {name_b}"
    assert discord_len(line) + 1 == details_reserve([name_a, name_b])


def test_no_expiry_phrase_is_longer_than_the_reserve_assumes():
    cases = [
        (date(2026, 10, 9), datetime(2026, 10, 10, 6, 30, tzinfo=UTC)),  # under an hour
        (date(2026, 10, 9), datetime(2026, 10, 9, 7, 5, tzinfo=UTC)),  # 24 hours (25-hour-ish)
        (date(2026, 11, 1), datetime(2026, 11, 1, 0, 0, tzinfo=PT)),  # 25 hours
        (date(2026, 9, 30), datetime(2026, 9, 1, 19, tzinfo=UTC)),  # a date
        (date(9999, 9, 30), NOON_UTC),  # a date, widest year
        (date(2026, 10, 10), NOON_UTC),  # tomorrow
        (date(2026, 10, 1), NOON_UTC),  # expired
    ]
    for due, now in cases:
        phrase = _line(now=now, expires_on=due)
        assert len(phrase) <= 24, phrase
    assert (
        len(_line(now=datetime(2026, 10, 10, 6, 30, tzinfo=UTC), expires_on=date(2026, 10, 9)))
        == 24
    )


# --- an over-long stored "via" name is cut at render time ---


def test_a_via_name_stored_under_an_older_longer_limit_is_cut_when_drawn():
    line = _line(reward="r", reward_from="N" * 6000, expires_on=date(2026, 10, 14))
    assert discord_len(line) < 500
    assert line.endswith("…")
    assert "N" * 99 in line and "N" * 101 not in line


def test_a_100_character_via_name_is_drawn_whole():
    line = _line(reward="r", reward_from="N" * 100)
    assert line.endswith("via " + "N" * 100)


def test_the_reserve_still_covers_the_worst_line_with_cut_names():
    row = _row(
        reward="*" * 150,
        reward_from="*" * 6000,
        expires_on=date(2026, 10, 14),
        expires_from="_" * 6000,
    )
    names = ["*" * 6000, "_" * 6000, "short"]
    assert discord_len(details_line(row, now=NOON_UTC)) <= details_reserve(names)
