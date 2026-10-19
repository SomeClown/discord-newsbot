"""Adversarial tests for newsbot.shift.official: Gearbox's prose, as written by people in a hurry.

The reader's contract (design.md §16) is "when it isn't sure, record nothing; a wrong
expiry is worse than unknown". So every case below gets one of three verdicts:

- a value is right (the post says it plainly): pinned exactly;
- nothing is right (the post is ambiguous, or says something we don't read): pinned as
  None;
- either is acceptable, but a *wrong* value never is ("miss is fine, wrong is a bug"):
  pinned with `_never_wrong`, which allows nothing or the truth and nothing else.

The posts are invented by hand in the styles Gearbox actually uses on Bluesky, Steam and
X. The bugs the round found were pinned `xfail(strict=True)` until the reader was fixed;
they're ordinary tests now, in the section near the bottom.

Also here: the regexes against 1 MB of hostile text (nothing quadratic), and the lazy
import in `official.py` against every import order, each in a fresh `python -I`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from newsbot.collectors.base import ItemDetails
from newsbot.shift.official import read_official_details

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
PUBLISHED = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)  # Tue Oct 6, 10:00 Pacific
OCT_14 = date(2026, 10, 14)


def _read(text, published_at=PUBLISHED, **kwargs):
    return read_official_details(f"{text}\n{CODE}", CODE, published_at, **kwargs)


def _never_wrong(text, *, reward="", expires=None, published_at=PUBLISHED):
    """Nothing or exactly the truth for each field; anything else fails."""
    details = _read(text, published_at)
    got_reward = details.reward if details else ""
    got_expiry = details.expires_on if details else None
    assert got_reward in ("", reward), f"wrong reward {got_reward!r} for {text!r}"
    assert got_expiry in (None, expires), f"wrong expiry {got_expiry!r} for {text!r}"


# --- the plain patterns, in the voice Gearbox posts them in ---


def test_a_bluesky_style_post_gives_the_count_and_leaves_a_year_less_slash_date_alone():
    # "10/14" has no year, and §16 only trusts a numeric date with a full year.
    details = _read("Redeem for 3 Golden Keys! Code expires 10/14 at 9 AM PT")
    assert details == ItemDetails(
        code=CODE, rank="official", reward="3 Golden Keys", category="golden"
    )


def test_an_x_style_post_with_a_time_and_a_time_zone_still_reads_the_date():
    details = _read("expires October 14th at 10:00 AM CT")
    assert details == ItemDetails(code=CODE, rank="official", expires_on=OCT_14)


def test_a_colon_and_a_full_numeric_date():
    assert _read("Expires: 10/14/2026").expires_on == OCT_14


def test_valid_through_with_a_dotted_month():
    assert _read("valid through Oct. 14").expires_on == OCT_14


def test_an_all_caps_post_reads_the_same_as_a_polite_one():
    details = _read("REDEEM 3 GOLDEN KEYS! EXPIRES OCTOBER 14")
    assert details == ItemDetails(
        code=CODE, rank="official", reward="3 Golden Keys", category="golden", expires_on=OCT_14
    )


def test_a_spelled_out_count_in_capitals():
    assert _read("THREE golden keys").reward == "3 Golden Keys"


def test_a_golden_key_and_some_other_key_is_one_golden_key():
    details = _read("a Golden Key and 2 Skeleton Keys")
    assert details is not None
    assert details.reward == "1 Golden Key"


def test_a_number_before_some_other_kind_of_key_is_not_a_golden_key_count():
    details = _read("3 Golden Keys and 2 Skeleton Keys")
    assert details is not None
    assert details.reward == "3 Golden Keys"
    assert _read("2 Skeleton Keys") is None


# --- "available until": the miss is fine, a wrong date is not ---


@pytest.mark.parametrize(
    "text",
    [
        "available until Oct 14",
        "🔑 Redeem 🔑 3 🔑 Golden Keys 🔑 expires 🗓 Oct 14",
        "Expires 14 October",
        "expires 14th Oct",
        "expires 2026-10-14",
        "expires on the 14th of October",
        "expires at the end of the month",
        "through Sunday",
        "until further notice",
        "valid until further notice, event ends Oct 14",
        "does not expire",
        "expires soon",
        "expires by Oct 14",
        "expires no later than Oct 14",
        "valid until 9 AM PT on Oct 14",
    ],
)
def test_a_phrasing_outside_the_two_patterns_never_yields_a_wrong_expiry(text):
    _never_wrong(text, expires=OCT_14)


@pytest.mark.parametrize(
    "text",
    [
        "expires at the end of the month",
        "through Sunday",
        "until further notice",
        "valid until further notice, event ends Oct 14",
        "does not expire",
        "expires soon",
    ],
)
def test_a_vague_deadline_records_nothing(text):
    assert _read(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "3x Golden Keys",
        "Golden Keys (3)",
        "3️⃣ Golden Keys",
        "3 🔑 Golden Keys",
        "x3 Golden Keys",
        "Golden Key x3",
        "three (3) golden keys",
        "3 free Golden Keys",
        "uno golden key",
        "½ Golden Key",
        "eleven Golden Keys",
        "1,500 Golden Keys",
        "3 #GoldenKeys",
    ],
)
def test_a_count_in_an_unusual_shape_never_yields_a_wrong_reward(text):
    details = _read(text)
    assert details is None or details.reward in ("3 Golden Keys", "1 Golden Key", "")


@pytest.mark.parametrize("text", ["3x Golden Keys", "Golden Keys (3)", "1,500 Golden Keys"])
def test_a_count_it_cannot_place_records_nothing(text):
    assert _read(text) is None


# --- the Pacific publish day, at the edges ---


def test_a_post_one_second_before_pacific_midnight_still_means_today():
    just_before = datetime(2026, 10, 7, 6, 59, 59, tzinfo=UTC)  # Oct 6, 23:59:59 PDT
    assert _read("expires October 6", just_before).expires_on == date(2026, 10, 6)


def test_a_post_at_pacific_midnight_means_tomorrow_so_yesterday_is_not_a_deadline():
    midnight = datetime(2026, 10, 7, 7, 0, 0, tzinfo=UTC)  # Oct 7, 00:00:00 PDT
    # Oct 6 is now a day ago, which the reader calls a typo, not a deadline (it used to
    # roll to 2027; the week-before rule gives up instead). A week-plus-a-day ago still rolls.
    assert _read("expires October 6", midnight) is None
    assert _read("expires October 7", midnight).expires_on == date(2026, 10, 7)
    assert _read("expires September 29", midnight).expires_on == date(2027, 9, 29)


def test_the_pacific_day_follows_the_clocks_going_back():
    # After the fall-back (Nov 1, 2026) Pacific is UTC-8, so midnight is 08:00 UTC.
    just_before = datetime(2026, 11, 2, 7, 59, 59, tzinfo=UTC)  # Nov 1, 23:59:59 PST
    at_midnight = datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC)  # Nov 2, 00:00:00 PST
    assert _read("expires November 1", just_before).expires_on == date(2026, 11, 1)
    assert _read("expires November 1", at_midnight) is None  # yesterday: a typo, not a deadline
    assert _read("expires October 24", at_midnight).expires_on == date(2027, 10, 24)


def test_a_publish_time_in_another_zone_is_converted_before_the_day_is_taken():
    tokyo = timezone(timedelta(hours=9))
    # 08:30 on Oct 7 in Tokyo is 16:30 on Oct 6 in Los Angeles.
    published = datetime(2026, 10, 7, 8, 30, tzinfo=tokyo)
    assert _read("expires October 6", published).expires_on == date(2026, 10, 6)


def test_a_naive_publish_time_is_read_as_utc_and_does_not_raise():
    naive = datetime(2026, 10, 6, 17, 0)
    assert _read("expires October 14", naive) == _read("expires October 14", PUBLISHED)


def test_a_year_less_date_at_the_year_end_looks_into_january():
    dec_30 = datetime(2026, 12, 30, 20, 0, tzinfo=UTC)
    assert _read("valid through Jan 3", dec_30).expires_on == date(2027, 1, 3)
    assert _read("expires December 31", dec_30).expires_on == date(2026, 12, 31)
    assert _read("valid through December 20", dec_30).expires_on == date(2027, 12, 20)


def test_new_years_eve_evening_in_los_angeles_is_still_december_31():
    # 07:30 UTC on Jan 1 is 23:30 on Dec 31 in Los Angeles.
    eve = datetime(2027, 1, 1, 7, 30, tzinfo=UTC)
    assert _read("expires December 31", eve).expires_on == date(2026, 12, 31)
    assert _read("expires January 1", eve).expires_on == date(2027, 1, 1)


def test_new_years_midnight_in_los_angeles_pushes_december_31_a_year_out():
    midnight = datetime(2027, 1, 1, 8, 0, tzinfo=UTC)  # Jan 1, 00:00 PST
    # Yesterday is a typo, not a deadline; eight days ago rolls to next December.
    assert _read("expires December 31", midnight) is None
    assert _read("expires December 24", midnight).expires_on == date(2027, 12, 24)


def test_a_year_less_slash_date_at_the_year_end_is_still_refused():
    dec_30 = datetime(2026, 12, 30, 20, 0, tzinfo=UTC)
    assert _read("expires 1/3", dec_30) is None
    assert _read("expires 12/31", dec_30) is None


def test_an_explicit_year_is_checked_against_the_publish_day_and_nothing_else():
    # QA batch 1, fix 1: this used to say the publish day was ignored. It still doesn't change
    # how a year is read, but a date before the post's own day is a misread, with or without it.
    assert _read("expires Oct 14, 2026", datetime(2031, 1, 1, tzinfo=UTC)) is None
    assert _read("expires Oct 14, 2026", datetime(2020, 1, 1, tzinfo=UTC)).expires_on == OCT_14
    assert (
        _read("expires Oct 14, 2026", datetime(2026, 10, 14, 20, tzinfo=UTC)).expires_on == OCT_14
    )


# --- markup, entities, urls, emoji ---


@pytest.mark.parametrize(
    "text",
    [
        "3&nbsp;Golden&nbsp;Keys expires&nbsp;Oct&nbsp;14",
        "Rewards: 3 Golden Keys &amp; expires Oct 14",
        "3 Golden Keys &lt;3 expires Oct 14",
        "expires Oct 14&#44; 2026",
        "**3 Golden Keys** expires _October 14_",
        "[3 Golden Keys](https://example.com/2026/10/20/post) expires `Oct 14`",
        "> 3 Golden Keys\n> expires Oct 14",
        "<b>3 Golden Keys</b> expires <i>Oct 14</i>",
        "🔑3 Golden Keys🔑 expires Oct 14 🎉",
    ],
)
def test_markup_and_entities_never_yield_a_wrong_value(text):
    _never_wrong(text, reward="3 Golden Keys", expires=OCT_14)


def test_a_literal_ampersand_entity_between_the_count_and_the_date_is_harmless():
    details = _read("Rewards: 3 Golden Keys &amp; expires Oct 14")
    assert details is not None
    assert (details.reward, details.expires_on) == ("3 Golden Keys", OCT_14)


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/2026/10/14/expires-oct-20 3 golden keys",
        "3 golden keys https://example.com/expires/10/14/2026",
        "3 golden keys https://example.com/post?expires=10/14/2026",
        "3 golden keys https://example.com/valid-until/October-14-2026",
        "3 golden keys https://example.com/through/Oct/14",
        "3 golden keys https://shift.gearboxsoftware.com/rewards?expires=Oct-14&through=Oct+20",
        "3 golden keys https://example.com/2026/10/20/ (published 10/20/2026)",
    ],
)
def test_a_date_inside_a_url_is_not_an_expiry(text):
    details = _read(text)
    assert details is not None
    assert details.expires_on is None
    assert details.reward == "3 Golden Keys"


def test_a_real_expiry_beside_a_url_with_a_date_still_reads_the_real_one():
    details = _read("expires Oct 14 https://example.com/2026/10/20/post")
    assert details is not None
    assert details.expires_on == OCT_14


def test_a_url_that_embeds_a_golden_key_phrase_gives_a_count_only_with_a_separator():
    # "3-golden-keys" in a slug has hyphens between the words; the reader accepts a short
    # hyphen run between "golden" and "keys", but nothing makes "3" and "golden" adjacent.
    assert _read("https://example.com/redeem-3-golden-keys-now") is None


@pytest.mark.parametrize(
    "text",
    [
        "Canjea 3 Llaves Doradas, vence el 14 de octubre",
        "3 Goldene Schlüssel, läuft ab am 14. Oktober",
        "Échangez 3 Clés d'or, expire le 14 octobre",
        "ゴールデンキー3個、10月14日まで",
        "Получите 3 золотых ключа до 14 октября",
    ],
)
def test_text_in_another_language_records_nothing(text):
    assert _read(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "３ Golden Keys",  # fullwidth digit
        "٣ Golden Keys",  # Arabic-Indic digit
        "expires Oct １４",
        "expıres Oct 14",  # dotless i
        "3 golden \u212aeys",  # Kelvin sign, which IGNORECASE would fold to k without re.ASCII
        "valid unt\u0131l Oct 14",  # dotless i again
        "expires \u039fct 14",  # Greek capital omicron for the O in Oct
    ],
)
def test_lookalike_characters_are_not_read_as_the_ascii_they_imitate(text):
    assert _read(text) is None


def test_zero_width_characters_inside_the_count_make_it_unreadable_not_wrong():
    assert _read("3\u200bGolden Keys") is None
    assert _read("3 G\u200bolden Keys") is None


# --- the same code twice, and the same fact twice ---


def test_the_count_and_the_date_said_twice_each_is_still_one_of_each():
    details = _read(
        "3 Golden Keys! expires Oct 14. Again: THREE golden keys, valid until October 14."
    )
    assert details == ItemDetails(
        code=CODE, rank="official", reward="3 Golden Keys", category="golden", expires_on=OCT_14
    )


def test_the_code_repeated_in_the_text_does_not_confuse_the_reader():
    text = f"{CODE}\n3 Golden Keys\n{CODE}\nexpires Oct 14\n{CODE.lower()}"
    details = read_official_details(text, CODE, PUBLISHED)
    assert details is not None
    assert (details.reward, details.expires_on) == ("3 Golden Keys", OCT_14)


# --- two readings of the same field: say nothing ---


@pytest.mark.parametrize(
    "text",
    [
        "3 Golden Keys now, 5 Golden Keys later",
        "5 Golden Keys, then 3 Golden Keys",
        "2 Golden Keys + 1 Golden Key",
        "a Golden Key and two Golden Keys",
    ],
)
def test_two_different_counts_record_no_reward(text):
    assert _read(text) is None


# --- bugs the round found (each was a wrong value, which §16 calls worse than blank; all fixed) ---

_COUNT_BUGS = [
    pytest.param(
        "New Borderlands 4 Golden Key code!",
        id="game-title-digit",
    ),
    pytest.param(
        "BORDERLANDS 4 GOLDEN KEY SHiFT CODE",
        id="game-title-digit-caps",
    ),
    pytest.param("Borderlands 4 Golden Keys: redeem this", id="game-title-digit-plural"),
    pytest.param("10/14 Golden Key code", id="date-digits-before-golden-key"),
    pytest.param("Oct 14 Golden Keys", id="day-of-month-before-golden-keys"),
    pytest.param("Here's a Golden Key SHiFT code", id="a-golden-key-code-is-a-modifier"),
    pytest.param("Here is a Golden Key code", id="a-golden-key-code-is-a-modifier-2"),
    pytest.param("3 Golden Key codes this week", id="count-of-codes-not-keys"),
    pytest.param("2.5 Golden Keys", id="decimal-count"),
    pytest.param("Update 1.2 Golden Keys", id="version-number"),
    pytest.param("twenty-five Golden Keys", id="hyphenated-number-word"),
    pytest.param("thirty-three Golden Keys", id="hyphenated-number-word-2"),
    pytest.param("5 Golden Keys for PC and 3 for console", id="platform-split-count"),
]


@pytest.mark.parametrize("text", _COUNT_BUGS)
def test_a_number_that_is_not_the_count_records_no_reward(text):
    assert _read(text) is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Not valid until Oct 14", id="negated"),
        pytest.param("Not valid until Oct 14, 2026", id="negated-full-year"),
        pytest.param("this code isn't valid until 10/14/2026", id="negated-numeric"),
    ],
)
def test_a_negated_valid_until_is_not_an_expiry(text):
    assert _read(text) is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("expires Oct 14 or 15", id="or-second-day"),
        pytest.param("expires Oct 14 and Oct 15", id="and-second-date"),
        pytest.param("expires Oct 14 - Oct 21", id="dash-range"),
        pytest.param("expires Oct 14–21", id="en-dash-range"),
        pytest.param("expires October 14/15", id="slash-pair"),
        pytest.param("expires 10/14/2026-10/21/2026", id="numeric-range"),
    ],
)
def test_a_range_or_alternative_after_the_keyword_records_no_expiry(text):
    assert _read(text) is None


# --- ReDoS: 1 MB of hostile text, nothing may go quadratic ---

_MB = 1_000_000


def _fill(unit: str) -> str:
    return (unit * (_MB // len(unit) + 1))[:_MB]


_HOSTILE = {
    "keyword-flood": _fill("expires "),
    "keyword-then-month-flood": _fill("expires Oct 1 "),
    "valid-until-flood": _fill("valid   until "),
    "valid-then-endless-whitespace": "valid" + " " * _MB + "until Oct 14",
    "keyword-then-endless-whitespace": "expires" + " " * _MB + "Oct 14",
    "slash-flood": _fill("1/"),
    "slash-chain-after-keyword": "expires " + "1/" * (_MB // 2),
    "endless-year-digits": "expires 1/1/" + "9" * _MB,
    "digit-flood": _fill("1"),
    "article-then-endless-whitespace": "a" + " " * _MB + "golden keys",
    "golden-flood": _fill("golden   "),
    "article-golden-flood": _fill("a golden "),
    "count-flood": _fill("3 golden key "),
    "golden-then-endless-hyphens": "3 golden" + "-" * _MB,
    "weekday-flood": _fill("expires on Tuesday,  "),
    "through-flood": _fill("through "),
    "nbsp-flood": _fill("a\u00a0golden\u00a0keys\u00a0"),
    "month-word-flood": _fill("Oct "),
    "date-flood": _fill("expires Oct 14th, 2026 "),
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_a_megabyte_of_hostile_text_is_read_in_linear_time(name):
    text = _HOSTILE[name]
    assert len(text) >= _MB
    started = time.perf_counter()
    read_official_details(text, CODE, PUBLISHED)
    elapsed = time.perf_counter() - started
    # Measured at well under a second each on a laptop; a quadratic regex would take minutes.
    assert elapsed < 10, f"{name} took {elapsed:.1f}s"


def test_a_megabyte_of_junk_with_one_real_fact_at_the_end_is_still_read():
    text = (
        _fill("expires soon, valid whenever, through the door, a golden ")
        + f"\n3 Golden Keys, expires Oct 14 {CODE}\n"
    )
    details = read_official_details(text, CODE, PUBLISHED)
    assert details is not None
    assert details.reward == "3 Golden Keys"
    assert details.expires_on == OCT_14


# --- the lazy import, in every order, in a fresh interpreter ---

_MODULES = [
    "newsbot.shift.official",
    "newsbot.shift.decide",
    "newsbot.collectors.shift_table",
    "newsbot.bot.format",
]


def _orders():
    """Each module first, with the other three in both directions (24 orders cost 20 s)."""
    for first in _MODULES:
        rest = [m for m in _MODULES if m != first]
        yield (first, *rest)
        yield (first, *reversed(rest))


_ORDERS = list(_orders())

_SCRIPT = """
import importlib, sys
from datetime import UTC, date, datetime

for name in sys.argv[1:]:
    importlib.import_module(name)

from newsbot.shift.official import read_official_details

code = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
when = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
# A year-less month-name date and a full numeric one both go through the lazy import.
one = read_official_details("3 Golden Keys, expires October 14 " + code, code, when)
two = read_official_details("valid through 10/14/2026 " + code, code, when)
assert one is not None and one.expires_on == date(2026, 10, 14), one
assert one.reward == "3 Golden Keys", one
assert two is not None and two.expires_on == date(2026, 10, 14), two
print("ok")
"""


@pytest.mark.parametrize("order", _ORDERS, ids="-".join)
def test_every_import_order_works_in_a_fresh_interpreter(order, tmp_path):
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", _SCRIPT, *order],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_the_reader_alone_does_not_pull_in_the_table_collector(tmp_path):
    script = (
        "import sys, newsbot.shift.official;print('newsbot.collectors.shift_table' in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
