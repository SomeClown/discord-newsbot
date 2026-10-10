"""QA round 2, area 1: attacking the official reader's windowing (batch d36d492).

The reader now looks only at the code's paragraph, about 200 characters either side. A window
is a hard edge, and the attack is the edge: a count, a date or a warning word ("not", "was",
a range's second half) that sits across it. The rule for every test below is the one from
design.md section 16: when the reader isn't sure it records nothing, because a wrong expiry is
worse than "unknown". So the property under test is "right, or nothing", never "something else".

The bugs this round found (a date or a negation cut by the edge, a foreign digit glued to a
day) were fixed in QA batch 2; their tests are plain tests now.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.shift.official import _CODE_WINDOW, _windows, read_official_details

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
# Oct 9, 2026, 10:00 Pacific.
PUBLISHED = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)


def _read(text, published_at=PUBLISHED, **kwargs):
    return read_official_details(text, CODE, published_at, **kwargs)


def _expiry(text, published_at=PUBLISHED, **kwargs):
    found = _read(text, published_at, **kwargs)
    return None if found is None else found.expires_on


def _reward(text, published_at=PUBLISHED, **kwargs):
    found = _read(text, published_at, **kwargs)
    return None if found is None else found.reward


# --- the code at the very start or end of a post ---


def test_a_code_at_the_very_start_of_the_post_reads_what_follows_it():
    assert _expiry(f"{CODE} expires October 14") == date(2026, 10, 14)


def test_a_code_at_the_very_end_of_the_post_reads_what_precedes_it():
    assert _expiry(f"expires October 14: {CODE}") == date(2026, 10, 14)


def test_a_post_that_is_only_the_code_says_nothing():
    assert _read(CODE) is None


def test_an_empty_post_says_nothing():
    assert _read("") is None


# --- the code appearing twice with different nearby details ---


def test_two_mentions_with_different_expiries_drop_the_expiry():
    text = f"{CODE} expires October 14.\n\nReminder: {CODE} expires October 20."
    assert _expiry(text) is None


def test_two_mentions_with_different_expiries_keep_an_agreed_reward():
    text = (
        f"{CODE} 3 Golden Keys, expires October 14.\n\n"
        f"Reminder: {CODE} 3 Golden Keys, expires October 20."
    )
    found = _read(text)
    assert found is not None
    assert found.reward == "3 Golden Keys"
    assert found.expires_on is None


def test_two_mentions_with_different_rewards_drop_the_reward_and_keep_an_agreed_expiry():
    text = (
        f"{CODE} 3 Golden Keys, expires October 14.\n\n"
        f"Reminder: {CODE} 5 Golden Keys, expires October 14."
    )
    found = _read(text)
    assert found is not None
    assert found.reward == ""
    assert found.category is None
    assert found.expires_on == date(2026, 10, 14)


def test_a_second_mention_that_says_nothing_does_not_contradict_the_first():
    text = f"{CODE} expires October 14.\n\nAgain, {CODE}, because people ask."
    assert _expiry(text) == date(2026, 10, 14)


# --- paragraphs: one newline, a blank line, CRLF ---


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_single_line_break_of_any_flavour_stays_in_the_paragraph(newline):
    assert _expiry(f"Code {CODE}{newline}expires October 14") == date(2026, 10, 14)


@pytest.mark.parametrize(
    "blank", ["\n\n", "\r\n\r\n", "\r\n \r\n", "\n\r\n", "\n\t\n", "\n\u00a0\n"]
)
def test_a_blank_line_of_any_flavour_ends_the_paragraph_in_both_directions(blank):
    assert _expiry(f"Code {CODE}{blank}expires October 14") is None
    assert _expiry(f"expires October 14{blank}Code {CODE}") is None


def test_a_blank_line_further_away_than_the_date_does_not_hide_it():
    text = f"Intro\n\nCode {CODE}\nexpires October 14\n\nMore news."
    assert _expiry(text) == date(2026, 10, 14)


def test_crlf_blank_lines_split_the_windows_themselves():
    (window,) = _windows(f"before\r\n\r\nthe code {CODE} here\r\n\r\nafter", CODE)
    assert "before" not in window
    assert "after" not in window
    assert CODE in window


# --- the 200-character boundary ---


def test_the_window_constant_is_what_the_docs_say():
    assert _CODE_WINDOW == 200


@pytest.mark.parametrize("pad_char", ["\U0001f600", "\u00e9", "\u4e2d", "\u200d"])
def test_what_the_padding_is_made_of_never_changes_the_answer(pad_char):
    """Slide the date across both window edges: the answer depends on the distance only.

    Padding with emoji, accented letters, CJK or a zero-width joiner must give exactly what
    padding with "x" gives at the same character distance (or the window is counting bytes
    or UTF-16 units, or a multi-byte run is splitting something). No exceptions either.
    """
    for pad in range(0, 230):
        after = f"{CODE} {{}} expires October 14, 2027"
        before = f"expires October 14, 2027 {{}} {CODE}"
        for template in (after, before):
            assert _expiry(template.format(pad_char * pad)) == _expiry(template.format("x" * pad))


@pytest.mark.parametrize("pad_char", ["\U0001f600", "\u00e9", "\u4e2d"])
def test_the_window_counts_characters_not_bytes_or_utf16_units(pad_char):
    text = f"{CODE} {pad_char * 150} expires October 14"
    # 150 wide characters plus the keyword and date is well inside 200 *characters*, though it
    # would be 300 UTF-16 units for the emoji and 450 UTF-8 bytes for the CJK.
    assert _expiry(text) == date(2026, 10, 14)


# --- the window cutting a date in half: the reader must give up, not misread ---

_CUT_TAILS = [
    " expires December 31, 2027",
    " expires December 31, 2026",
    " expires Dec 31, 2027",
    " valid until 12/31/2027",
    " expires October 14, 2027",
]


def _misreads_after_the_code(tail, true_date):
    wrong = []
    for pad in range(0, 230):
        found = _expiry(f"{CODE} " + "x" * pad + tail)
        if found not in (None, true_date):
            wrong.append((pad, found))
    return wrong


@pytest.mark.parametrize(
    ("tail", "true_date"),
    [
        (" expires December 31, 2026", date(2026, 12, 31)),
        (" expires Dec 31, 2026", date(2026, 12, 31)),
        (" expires December 31, 2027", date(2027, 12, 31)),
        (" expires October 14, 2027", date(2027, 10, 14)),
    ],
)
def test_a_date_cut_by_the_end_of_the_window_is_given_up_not_misread(tail, true_date):
    assert _misreads_after_the_code(tail, true_date) == []


def test_a_numeric_date_cut_by_the_end_of_the_window_is_given_up_not_misread():
    # (Currently fine: a half-written numeric date doesn't match the numeric pattern at all.)
    assert _misreads_after_the_code(" valid until 12/31/2027", date(2027, 12, 31)) == []


def test_a_date_before_the_code_is_not_cut_by_the_front_of_the_window():
    """The keyword leads its date, so the front edge can eat the keyword but never half a date."""
    wrong = []
    for pad in range(0, 230):
        found = _expiry(f"expires December 31, 2027 {'x' * pad} {CODE}")
        if found not in (None, date(2027, 12, 31)):
            wrong.append((pad, found))
    assert wrong == []


def test_a_range_whose_tail_is_cut_off_is_given_up():
    found = []
    for pad in range(0, 230):
        text = f"{CODE} " + "x" * pad + " expires October 14 - October 21"
        if _expiry(text) is not None:  # a range is not one expiry, cut or not
            found.append(pad)
    assert found == []


@pytest.mark.parametrize(
    "lead", ["This is not valid until October 14. ", "These were valid until October 14. "]
)
def test_a_negation_cut_off_by_the_front_of_the_window_does_not_flip_the_meaning(lead):
    wrong = [pad for pad in range(0, 260) if _expiry(lead + "x" * pad + f" {CODE}") is not None]
    assert wrong == []


def test_a_negation_inside_the_window_is_still_honoured_at_every_distance():
    for pad in range(0, 150):
        assert _expiry(f"not valid until October 14. {'x' * pad} {CODE}") is None
        assert _expiry(f"{CODE} {'x' * pad} not valid until October 14") is None


@pytest.mark.parametrize("digit", ["\u0664", "\uff14", "\u0969"])
def test_a_day_with_a_foreign_digit_glued_on_is_not_read_as_the_ascii_prefix(digit):
    assert _expiry(f"{CODE} valid until October 1{digit}") is None


# --- "expired" in an unrelated word ---


def test_unexpired_is_not_expired_and_the_date_is_read():
    assert _expiry(f"{CODE} is unexpired and valid until October 14") == date(2026, 10, 14)


@pytest.mark.parametrize(
    "word", ["expired", "Expired", "EXPIRED", "expired.", "(expired)", "non-expired", "expired:"]
)
def test_the_word_expired_anywhere_in_the_window_gives_up(word):
    assert _expiry(f"{CODE} valid until October 14, {word} codes are listed below") is None


@pytest.mark.parametrize(
    "word", ["unexpired", "preexpired", "expiredx", "reexpired", "expiredness"]
)
def test_a_longer_word_that_merely_contains_expired_does_not_trip_the_rule(word):
    assert _expiry(f"{CODE} valid until October 14, {word}") == date(2026, 10, 14)


def test_will_expire_is_a_keyword_and_not_the_past_tense():
    assert _expiry(f"{CODE} will expire on October 14") == date(2026, 10, 14)


# --- 23:59 Pacific, "expires today", and no publish time at all ---

LATE = datetime(2026, 10, 10, 6, 59, tzinfo=UTC)  # Oct 9, 23:59 PDT
JUST_AFTER_MIDNIGHT = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)  # Oct 10, 00:00 PDT


def test_expires_today_has_no_date_to_read_and_records_nothing():
    assert _read(f"{CODE} expires today", LATE) is None


def test_a_late_night_post_that_names_its_own_day_is_kept():
    assert _expiry(f"{CODE} expires October 9", LATE) == date(2026, 10, 9)


def test_the_same_post_a_minute_later_is_a_day_stale_and_gives_up():
    assert _expiry(f"{CODE} expires October 9", JUST_AFTER_MIDNIGHT) is None


def test_a_late_night_post_with_a_year_less_date_in_the_next_year_rolls_over():
    late_dec = datetime(2027, 1, 1, 7, 30, tzinfo=UTC)  # Dec 31, 2026, 23:30 PST
    assert _expiry(f"{CODE} expires December 31", late_dec) == date(2026, 12, 31)
    assert _expiry(f"{CODE} expires January 2", late_dec) == date(2027, 1, 2)


def test_the_dst_boundary_day_is_still_one_pacific_day():
    # DST ended Nov 1, 2026 at 09:00 UTC; Nov 2, 07:59 UTC is Nov 1, 23:59 PST.
    last_minute = datetime(2026, 11, 2, 7, 59, tzinfo=UTC)
    assert _expiry(f"{CODE} expires November 1", last_minute) == date(2026, 11, 1)
    assert _expiry(f"{CODE} expires November 1", last_minute + timedelta(minutes=1)) is None


def test_no_publish_time_with_now_uses_nows_pacific_day():
    now = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)
    assert _expiry(f"{CODE} expires October 14", None, now=now) == date(2026, 10, 14)
    assert _expiry(f"{CODE} expires October 2", None, now=now) is None


def test_no_publish_time_and_no_now_cannot_anchor_a_year_less_date():
    assert _expiry(f"{CODE} expires October 14", None) is None


def test_no_publish_time_and_no_now_takes_a_dated_year_as_it_comes():
    assert _expiry(f"{CODE} expires October 14, 2026", None) == date(2026, 10, 14)


def test_a_naive_publish_time_does_not_crash_the_reader():
    # Collectors hand over aware datetimes; a naive one is somebody's bug, not a reason to
    # lose the whole pass.
    found = _read(f"{CODE} expires October 14", datetime(2026, 10, 9, 17, 0))
    assert found is None or found.expires_on == date(2026, 10, 14)
