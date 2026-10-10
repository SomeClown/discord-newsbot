"""QA fix batch 1, fix 1: the official reader only reads near the code.

Long official posts (Steam patch notes, say) carry a SHiFT code stuck in a screenful of
unrelated prose. The reader used to take any "expires <date>" or "<n> Golden Keys" anywhere
in the post and file it under the code. Now a count or an expiry has to sit in the code's own
paragraph and within about 200 characters of it; a date before the post's own Pacific day is
a misread whether or not it has a year; and "was valid until" and "expired" are dead on
arrival like a negation is.

The two reviewer repros come first, end to end through detection, because the unit
behavior is only worth having if the code really posts.
"""

from __future__ import annotations

import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from test_shift_details_detection import T0, X, Y
from test_shift_details_detection_adversarial import Ledger, details_of, official, statuses
from test_shift_fanout import V3, add_guild, seed

from newsbot.config import load_config
from newsbot.shift.official import read_official_details
from newsbot.store.db import connect, migrate

# Oct 9, 2026, 10:00 Pacific.
PUBLISHED = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)
CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
OTHER = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"


def _read(text, published_at=PUBLISHED, **kwargs):
    return read_official_details(text, CODE, published_at, **kwargs)


def _patch_notes(code: str) -> str:
    """The qa repro: a bonus with an expiry, a Season Pass bug, and a code, far apart."""
    filler = " ".join(
        f"Fixed an issue with item {n} not showing the right icon." for n in range(12)
    )
    return (
        "Patch 1.2.3 notes\n\n"
        "The Vault Hunter event bonus expires October 20, 2026. Enjoy the loot!\n\n"
        f"{filler}\n\n"
        "Known issues: 5 Golden Keys from the Season Pass weren't granted to some players.\n"
        f"{filler}\n\n"
        f"Here's a SHiFT code: {code}\n"
    )


# --- the repros, through the real detection path ---


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def h(db_path):
    harness = Ledger(db_path, load_config(V3), T0)
    seed(db_path)
    return harness


def test_the_patch_notes_repro_gives_the_code_no_reward_and_no_expiry():
    assert read_official_details(_patch_notes(X), X, PUBLISHED) is None


async def test_the_patch_notes_repro_posts_the_code_with_no_details(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run([official(_patch_notes(X), at=T0 - timedelta(hours=1))], at=T0)

    assert h.original_codes(1) == [X]
    assert [a.ping for a in h.originals(1)] == [True]
    assert details_of(h.db_path, X) is None


def test_a_week_two_post_with_a_past_tense_deadline_gives_no_expiry():
    text = (
        "Week 2 rewards were valid until October 2, 2026. Week 3 is live now.\n"
        f"New SHiFT code: {CODE}"
    )
    assert _read(text) is None


async def test_a_week_two_post_still_posts_its_code(h):
    add_guild(h.db_path, 1, ping="everyone")
    text = (
        f"Week 2 rewards were valid until October 2, 2026. Week 3 is live now.\nNew SHiFT code: {X}"
    )

    await h.run([official(text, at=T0 - timedelta(hours=1))], at=T0)

    assert h.original_codes(1) == [X]
    assert statuses(h.db_path) == {X: "posted"}
    assert details_of(h.db_path, X) is None  # no expiry was recorded for it


# --- (a) the window: the code's paragraph, 200 characters either side ---


def test_a_count_and_an_expiry_in_the_codes_own_paragraph_are_read():
    details = _read(f"Here's a SHiFT code: {CODE} for 3 Golden Keys, expires October 14.")
    assert details is not None
    assert details.reward == "3 Golden Keys"
    assert details.expires_on == date(2026, 10, 14)


def test_facts_before_the_code_in_the_same_paragraph_count():
    details = _read(f"3 Golden Keys, expires October 14. Redeem {CODE} in game.")
    assert details is not None
    assert details.reward == "3 Golden Keys"
    assert details.expires_on == date(2026, 10, 14)


def test_a_blank_line_ends_the_paragraph_before_and_after_the_code():
    text = f"3 Golden Keys expires October 14\n\n{CODE}\n\n5 Golden Keys expires October 20"
    assert _read(text) is None


@pytest.mark.parametrize("blank", ["\n\n", "\n \n", "\n\t\n", "\r\n\r\n", "\n\N{NO-BREAK SPACE}\n"])
def test_every_kind_of_blank_line_splits_paragraphs(blank):
    assert _read(f"expires October 14{blank}{CODE}") is None
    assert _read(f"{CODE}{blank}3 Golden Keys") is None


def test_a_single_newline_is_not_a_paragraph_break():
    assert _read(f"3 Golden Keys\n{CODE}") is not None


def test_a_mention_in_range_is_read_and_one_past_the_snap_is_not():
    def pad(n: int) -> str:
        return " ".join(["so"] * (n // 3))

    near = f"3 Golden Keys {pad(150)} {CODE}"
    # QA batch 2: an edge snaps outward to a clause boundary, up to 200 characters more, so
    # "far" has to be past 200 + 200 and not just past 200.
    far = f"3 Golden Keys {pad(500)} {CODE}"
    assert _read(near) is not None
    assert _read(far) is None
    near_after = f"{CODE} {pad(150)} expires October 14"
    far_after = f"{CODE} {pad(500)} expires October 14"
    assert _read(near_after) is not None
    assert _read(far_after) is None


def test_a_code_missing_from_the_text_reads_nothing():
    assert _read(f"3 Golden Keys, expires October 14. {OTHER}") is None


def test_the_code_is_found_in_either_case():
    assert _read(f"3 Golden Keys {CODE.lower()}") is not None


def test_two_places_that_disagree_drop_that_part_and_two_that_agree_keep_it():
    split = f"3 Golden Keys {CODE}\n\n{CODE} 5 Golden Keys"
    assert _read(split) is None
    same = f"3 Golden Keys {CODE}\n\n{CODE} three golden keys"
    assert _read(same).reward == "3 Golden Keys"
    one_each = f"expires October 14 {CODE}\n\n{CODE} 3 Golden Keys"
    details = _read(one_each)
    assert details.reward == "3 Golden Keys" and details.expires_on == date(2026, 10, 14)


def test_a_post_that_repeats_the_code_endlessly_is_given_up_on_in_bounded_time():
    text = (f"3 Golden Keys {CODE}\n\n") * 40000
    started = time.perf_counter()
    result = _read(text)
    assert time.perf_counter() - started < 5
    assert result is None


def test_a_long_post_costs_the_same_as_a_short_one():
    text = "filler words go here. " * 60000 + f"\n\nexpires October 14 {CODE}"
    started = time.perf_counter()
    details = _read(text)
    assert time.perf_counter() - started < 5
    assert details is not None and details.expires_on == date(2026, 10, 14)


# --- (b) a date before the post's own day is a misread, year or no year ---


@pytest.mark.parametrize(
    "text",
    [
        "valid until October 2, 2026",
        "expires Oct 8, 2026",
        "valid through 10/02/2026",
        "expires 10/8/2026",
        "valid until Sept 30, 2025",
    ],
)
def test_an_explicit_date_before_the_post_gives_no_expiry(text):
    assert _read(f"{text} {CODE}") is None


@pytest.mark.parametrize("text", ["expires October 9, 2026", "valid until 10/09/2026"])
def test_a_date_on_the_posts_own_pacific_day_is_fine(text):
    assert _read(f"{text} {CODE}").expires_on == date(2026, 10, 9)


def test_the_posts_day_is_its_pacific_day_not_its_utc_day():
    # 03:00 UTC on Oct 10 is still Oct 9 in Pacific.
    late_utc = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
    assert _read(f"expires October 9, 2026 {CODE}", late_utc).expires_on == date(2026, 10, 9)
    assert _read(f"expires October 8, 2026 {CODE}", late_utc) is None


def test_a_year_less_date_that_already_passed_still_never_reads_as_a_past_deadline():
    assert _read(f"valid until October 2 {CODE}") is None  # within the week back: past tense
    assert _read(f"expires October 14 {CODE}").expires_on == date(2026, 10, 14)


def test_with_no_publish_time_and_no_now_a_dated_expiry_cannot_be_checked_and_is_taken():
    assert _read(f"expires October 14, 2026 {CODE}", None).expires_on == date(2026, 10, 14)
    assert _read(f"expires October 14 {CODE}", None) is None  # year-less needs an anchor


def test_now_stands_in_for_a_missing_publish_time():
    now = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    assert _read(f"expires October 2, 2026 {CODE}", None, now=now) is None


# --- (c) past tense is not a deadline ---


@pytest.mark.parametrize(
    "text",
    [
        "The old code was valid until October 14, 2026",
        "Those codes were valid until October 14",
        "These were all valid until October 14",
        "It was only good through October 14",
        "That was redeemable through October 14",
        "This code expired on October 14",
        "Expired October 14",
        "The earlier code has expired. The new one expires October 14",
    ],
)
def test_past_tense_lead_ins_give_no_expiry(text):
    assert _read(f"{text}. {CODE}") is None


def test_the_present_tense_still_reads():
    assert _read(f"This code is valid until October 14. {CODE}").expires_on == date(2026, 10, 14)
    assert _read(f"It expires October 14. {CODE}").expires_on == date(2026, 10, 14)


def test_a_was_further_back_than_two_words_or_across_punctuation_does_not_count():
    text = f"It was announced last week; valid until October 14. {CODE}"
    assert _read(text).expires_on == date(2026, 10, 14)
    text = f"This was great news. Valid until October 14. {CODE}"
    assert _read(text).expires_on == date(2026, 10, 14)


def test_expired_in_another_paragraph_does_not_spoil_this_one():
    text = f"Last week's codes expired.\n\nThis one is valid until October 14. {CODE}"
    assert _read(text).expires_on == date(2026, 10, 14)


def test_a_golden_key_count_is_unaffected_by_a_past_tense_expiry():
    details = _read(f"3 Golden Keys {CODE}, last week's were valid until October 2")
    assert details is not None
    assert details.reward == "3 Golden Keys"
    assert details.expires_on is None


def test_the_second_code_can_be_asked_about_separately_in_a_long_post():
    text = f"3 Golden Keys {CODE}\n\n" + "x " * 400 + f"\n\nexpires October 14 {OTHER}"
    assert _read(text).reward == "3 Golden Keys"
    other = read_official_details(text, OTHER, PUBLISHED)
    assert other is not None and other.reward == "" and other.expires_on == date(2026, 10, 14)


async def test_a_single_code_post_with_an_in_window_expiry_still_stores_it(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run([official(f"{Y} is a Golden Key code, expires October 14", at=T0)], at=T0)

    assert details_of(h.db_path, Y).expires_on == date(2026, 10, 14)
