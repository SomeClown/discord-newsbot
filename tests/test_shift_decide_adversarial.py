"""Adversarial tests for the details half of newsbot.shift.decide (design.md §16, Task 4).

`test_shift_decide.py` pins the happy attach rules. This file goes after the seams:

- which item gets a reader, and which never does (community and press, roundups, posts
  with more than one code, rows whose details are about some other code);
- `golden`, which is now "the text says so, or the details do";
- `aggregate(undated_stale_sources=...)` with names that differ in case, whitespace and
  Unicode folding, and its interplay with the second-source rule;
- one megabyte of official post going through the whole attach path.

Pure functions, no database, no clock beyond the `now` each test hands in.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.shift import decide
from newsbot.shift.decide import CodeSighting, aggregate, sightings_from_items

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
CODE_C = "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"
NOW = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=48)


def _item(**kwargs) -> RawItem:
    defaults = dict(
        url="https://example.com/a",
        title="A post",
        excerpt="",
        source_name="Src",
        trust="official",
        published_at=NOW,
        topics=None,
    )
    defaults.update(kwargs)
    return RawItem(**defaults)


def _sighting(**kwargs) -> CodeSighting:
    defaults = dict(
        code=CODE_A,
        golden=False,
        source_name="Src",
        item_url="https://example.com/a",
        trust="community",
        published_at=NOW,
    )
    defaults.update(kwargs)
    return CodeSighting(**defaults)


def _row(code=CODE_A, **kwargs) -> ItemDetails:
    defaults = dict(code=code, rank="table", reward="A reward", listed=True)
    defaults.update(kwargs)
    return ItemDetails(**defaults)


def _sights(*items, **kwargs) -> list[CodeSighting]:
    return sightings_from_items(list(items), topics=[], alert_topics=[], **kwargs)


# --- which items get a reader ---


class _Spy:
    """Stands in for `read_official_details` and remembers whether anyone called it."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def spy(monkeypatch):
    fake = _Spy()
    monkeypatch.setattr(decide, "read_official_details", fake)
    return fake


@pytest.mark.parametrize("trust", ["community", "press"])
def test_community_and_press_items_never_reach_the_reader(spy, trust):
    text = f"{CODE_A}: 3 Golden Keys, expires October 14, 2026"
    sightings = _sights(
        _item(trust=trust, title=text),
        _item(trust=trust, title="x", full_text=text, url="https://example.com/b"),
        _item(trust=trust, title="x", excerpt=text, url="https://example.com/c"),
    )
    assert len(sightings) == 3
    assert all(s.details is None for s in sightings)
    assert spy.calls == []


def test_a_community_table_row_keeps_its_own_details_and_still_gets_no_reader(spy):
    row = _row(reward="Cosmetic")
    (sighting,) = _sights(_item(trust="community", title=CODE_A, details=row))
    assert sighting.details is row
    assert spy.calls == []


def test_an_official_item_with_matching_details_keeps_them_and_skips_the_reader(spy):
    row = _row(rank="official", reward="Table said so", listed=False)
    (sighting,) = _sights(
        _item(title=f"{CODE_A} 3 Golden Keys, expires Oct 14", details=row),
    )
    assert sighting.details is row
    assert spy.calls == []


def test_the_reader_is_asked_about_the_one_code_with_the_posts_own_publish_time(spy):
    _sights(_item(title=f"{CODE_A} expires Oct 14"), now=NOW + timedelta(days=9))
    ((args, kwargs),) = spy.calls
    assert args[1] == CODE_A
    assert args[2] == NOW
    assert kwargs == {"now": NOW + timedelta(days=9)}


def test_the_reader_is_not_asked_when_a_post_has_two_codes(spy):
    sightings = _sights(_item(title=f"{CODE_A} {CODE_B}"))
    assert [s.code for s in sightings] == [CODE_A, CODE_B]
    assert spy.calls == []


# --- table rows whose details are about some other code ---


def test_a_table_row_whose_details_name_another_code_carries_none_and_leaks_nothing():
    mismatched = _row(code=CODE_B, category="golden", reward="3 Golden Keys")
    (sighting,) = _sights(_item(trust="community", title=CODE_A, details=mismatched))
    assert sighting.code == CODE_A
    assert sighting.details is None
    assert not sighting.golden  # B's golden category must not make A golden


def test_details_for_a_code_that_is_in_the_text_but_is_not_the_only_one_are_dropped():
    row = _row(code=CODE_A, category="golden")
    sightings = _sights(_item(trust="community", title=f"{CODE_A} and {CODE_B}", details=row))
    assert [s.code for s in sightings] == [CODE_A, CODE_B]
    assert all(s.details is None for s in sightings)
    assert not any(s.golden for s in sightings)


def test_an_official_item_with_mismatched_details_still_falls_back_to_the_reader():
    mismatched = _row(code=CODE_B)
    (sighting,) = _sights(
        _item(title=f"{CODE_A} 3 Golden Keys", details=mismatched),
    )
    assert sighting.details is not None
    assert sighting.details.code == CODE_A
    assert sighting.details.rank == "official"


# --- posts with more than one code ---


@pytest.mark.parametrize("count", [2, 3, 5])
def test_a_multi_code_official_post_sights_every_code_and_records_no_details(count):
    codes = [f"{chr(65 + i) * 4}{i + 1}-{chr(65 + i) * 5}-AAAAA-AAAAA-AAAAA" for i in range(count)]
    post = _item(title="Codes! 3 Golden Keys each, expires October 14", full_text=" ".join(codes))
    sightings = _sights(post)
    assert [s.code for s in sightings] == codes
    assert all(s.details is None for s in sightings)
    assert not any(s.roundup for s in sightings)
    assert all(s.golden for s in sightings)  # "Golden Keys" in the text, as before


def test_six_codes_is_a_roundup_and_carries_no_details_either():
    codes = [f"{chr(65 + i) * 4}{i + 1}-{chr(65 + i) * 5}-AAAAA-AAAAA-AAAAA" for i in range(6)]
    sightings = _sights(
        _item(title="3 Golden Keys, expires October 14", full_text=" ".join(codes)),
        max_codes_per_item=5,
    )
    assert len(sightings) == 6
    assert all(s.roundup and s.details is None for s in sightings)


def test_the_same_code_twice_in_one_post_is_one_code_and_gets_its_details():
    post = _item(title=f"{CODE_A} 3 Golden Keys", full_text=f"{CODE_A}\nexpires Oct 14\n{CODE_A}")
    (sighting,) = _sights(post)
    assert sighting.details is not None
    assert (sighting.details.reward, sighting.details.expires_on) == (
        "3 Golden Keys",
        date(2026, 10, 14),
    )


def test_the_same_code_in_two_cases_is_one_code_and_gets_its_details():
    post = _item(title="3 Golden Keys", full_text=f"{CODE_A.lower()}\n{CODE_A}")
    (sighting,) = _sights(post)
    assert sighting.code == CODE_A
    assert sighting.details is not None


@pytest.mark.parametrize(
    "lookalike",
    [
        "https://example.com/redeem/CCCC3-CCCCC-CCCCC-CCCCC-CCCCC/",  # a path segment
        "xCCCC3-CCCCC-CCCCC-CCCCC-CCCCC",  # glued to a letter
        "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC-CCCCC",  # six groups
        "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE",  # the placeholder from every example post
    ],
)
def test_a_code_shaped_scrap_does_not_make_a_post_multi_code(lookalike):
    (sighting,) = _sights(
        _item(title=f"{CODE_A} 3 Golden Keys", full_text=f"{CODE_A}\n{lookalike}")
    )
    assert sighting.code == CODE_A
    assert sighting.details is not None
    assert sighting.details.reward == "3 Golden Keys"


def test_a_code_in_a_query_string_is_a_code_and_a_date_in_the_path_is_not_an_expiry():
    url = f"https://shift.example.com/2026/10/20/redeem?code={CODE_A}"
    (sighting,) = _sights(_item(title="3 Golden Keys", full_text=url))
    assert sighting.code == CODE_A
    assert sighting.details is not None
    assert sighting.details.expires_on is None
    assert sighting.details.reward == "3 Golden Keys"


def test_a_code_only_present_as_a_path_segment_is_no_sighting_at_all():
    assert _sights(_item(title="3 Golden Keys", full_text=f"https://example.com/{CODE_A}/")) == []


# --- what the reader sees: title plus full text (or excerpt), nothing else ---


def test_the_excerpt_is_ignored_when_there_is_full_text():
    item = _item(title=CODE_A, excerpt="expires Oct 14, 2026", full_text="nothing to see")
    (sighting,) = _sights(item)
    assert sighting.details is None


def test_the_excerpt_is_read_when_there_is_no_full_text():
    (sighting,) = _sights(_item(title=CODE_A, excerpt="expires Oct 14, 2026", full_text=None))
    assert sighting.details is not None
    assert sighting.details.expires_on == date(2026, 10, 14)


def test_a_conflicting_pair_of_facts_split_across_title_and_body_records_nothing():
    item = _item(title=f"{CODE_A}: expires Oct 14", full_text="valid until Oct 21")
    (sighting,) = _sights(item)
    assert sighting.details is None


# --- `now`, the fallback ---


def test_now_is_ignored_when_the_post_has_a_publish_time():
    item = _item(title=f"{CODE_A} expires October 14", published_at=NOW)
    (sighting,) = _sights(item, now=datetime(2030, 1, 1, tzinfo=UTC))
    assert sighting.details is not None
    assert sighting.details.expires_on == date(2026, 10, 14)


def test_with_neither_publish_time_nor_now_a_year_less_date_is_left_unread_but_still_sighted():
    item = _item(title=f"{CODE_A} 3 Golden Keys, expires October 14", published_at=None)
    (sighting,) = _sights(item)
    assert sighting.code == CODE_A
    assert sighting.published_at is None
    assert sighting.details is not None
    assert sighting.details.expires_on is None
    assert sighting.details.reward == "3 Golden Keys"


def test_a_naive_now_does_not_crash_the_attach_path():
    item = _item(title=f"{CODE_A} expires October 14", published_at=None)
    (sighting,) = _sights(item, now=datetime(2026, 10, 6, 17, 0))
    assert sighting.details is not None
    assert sighting.details.expires_on == date(2026, 10, 14)


# --- golden: the text says so, or the details do ---


@pytest.mark.parametrize(
    ("category", "title", "golden"),
    [
        ("golden", CODE_A, True),
        ("cosmetic", CODE_A, False),
        ("permanent", CODE_A, False),
        ("other", CODE_A, False),
        (None, CODE_A, False),
        ("cosmetic", f"{CODE_A} Golden Key skin", True),
        ("permanent", f"{CODE_A} 2 Golden Keys", True),
        ("other", f"{CODE_A} golden keys", True),
    ],
)
def test_golden_comes_from_the_category_or_the_text_whichever_says_yes(category, title, golden):
    row = _row(category=category, reward="")
    (sighting,) = _sights(_item(trust="community", title=title, details=row))
    assert sighting.golden is golden


def test_an_official_expiry_only_post_is_not_golden():
    (sighting,) = _sights(_item(title=f"{CODE_A} expires October 14"))
    assert sighting.details is not None
    assert sighting.details.category is None
    assert not sighting.golden


def test_a_post_the_reader_gives_up_on_is_still_golden_by_its_words():
    # Two different counts: no reward recorded, but "Golden Keys" is plainly in the text.
    (sighting,) = _sights(_item(title=f"{CODE_A}: 3 Golden Keys now, 5 Golden Keys later"))
    assert sighting.details is None
    assert sighting.golden


def test_golden_is_per_item_not_per_code_in_a_multi_code_post():
    sightings = _sights(_item(title="Golden Key day", full_text=f"{CODE_A} {CODE_B}"))
    assert [s.golden for s in sightings] == [True, True]


def test_a_roundup_row_with_a_golden_category_is_golden_only_if_it_says_so():
    # Details never attach to a roundup, so a category can't make one golden.
    codes = [f"{chr(65 + i) * 4}{i + 1}-{chr(65 + i) * 5}-AAAAA-AAAAA-AAAAA" for i in range(4)]
    row = _row(code=codes[0], category="golden")
    sightings = _sights(
        _item(trust="community", title=" ".join(codes), details=row), max_codes_per_item=3
    )
    assert len(sightings) == 4
    assert not any(s.golden for s in sightings)


# --- the undated rule for a table's first listing ---


def _fresh(sightings, stale=frozenset(), now=NOW):
    return aggregate(sightings, now=now, max_age=MAX_AGE, undated_stale_sources=stale)


@pytest.mark.parametrize(
    "source_name",
    ["mentalmars", "MENTALMARS", "MentalMars", "mEnTaLmArS"],
)
def test_the_stale_name_matches_the_source_in_any_case(source_name):
    (candidate,) = _fresh(
        [_sighting(source_name=source_name, published_at=None)], frozenset({"mentalmars"})
    )
    assert not candidate.fresh


@pytest.mark.parametrize("stale_name", ["MentalMars", "MENTALMARS", "mentalmars"])
def test_the_stale_set_may_use_any_case_too(stale_name):
    (candidate,) = _fresh(
        [_sighting(source_name="mentalmars", published_at=None)], frozenset({stale_name})
    )
    assert not candidate.fresh


def test_names_are_compared_with_full_case_folding_not_just_lower():
    # casefold turns the German sharp s into "ss"; lower() would not.
    (candidate,) = _fresh(
        [_sighting(source_name="Straße Codes", published_at=None)],
        frozenset({"STRASSE CODES"}),
    )
    assert not candidate.fresh


@pytest.mark.parametrize("padded", [" mentalmars", "mentalmars ", "mentalmars\n", "mental mars"])
def test_whitespace_is_significant_in_a_source_name_exactly_as_in_the_second_source_rule(padded):
    """Characterization, not a spec line: names are compared as written, apart from case.

    Config doesn't strip names and the stale set is built from the same config strings, so a
    padded name only misses if the caller invents one. If the owner wants names stripped,
    `_is_fresh` and the second-source rule in `aggregate` both have to change together.
    """
    (candidate,) = _fresh(
        [_sighting(source_name=padded, published_at=None)], frozenset({"mentalmars"})
    )
    assert candidate.fresh


def test_two_names_that_differ_only_by_padding_still_count_as_two_sources():
    # The same "exactly as written" rule the stale check uses, seen from the trust side.
    sightings = [_sighting(source_name="mentalmars"), _sighting(source_name="mentalmars ")]
    (candidate,) = _fresh(sightings)
    assert candidate.trusted


def test_a_stale_source_not_named_exactly_leaves_other_sources_undated_rows_fresh():
    (candidate,) = _fresh(
        [_sighting(source_name="Bluesky search", published_at=None)], frozenset({"mentalmars"})
    )
    assert candidate.fresh


def test_an_old_dated_sighting_from_the_stale_source_is_stale_by_age_not_by_the_rule():
    old = _sighting(source_name="mentalmars", published_at=NOW - timedelta(hours=49))
    (candidate,) = _fresh([old], frozenset({"mentalmars"}))
    assert not candidate.fresh
    (without,) = _fresh([old])
    assert not without.fresh


def test_a_dated_sighting_exactly_at_the_age_limit_is_fresh_even_from_the_stale_source():
    edge = _sighting(source_name="mentalmars", published_at=NOW - MAX_AGE)
    (candidate,) = _fresh([edge], frozenset({"mentalmars"}))
    assert candidate.fresh


def test_a_stale_undated_row_beside_an_old_dated_one_from_the_same_source_is_not_fresh():
    sightings = [
        _sighting(source_name="mentalmars", published_at=None),
        _sighting(source_name="mentalmars", published_at=NOW - timedelta(days=30)),
    ]
    (candidate,) = _fresh(sightings, frozenset({"mentalmars"}))
    assert not candidate.fresh


def test_a_community_find_in_the_same_pass_keeps_the_code_fresh_and_makes_it_trusted():
    # The B+ story: Reddit saw it just now, mentalmars lists it with no date.
    sightings = [
        _sighting(source_name="mentalmars", published_at=None),
        _sighting(source_name="r/Borderlands4", published_at=NOW - timedelta(hours=2)),
    ]
    (candidate,) = _fresh(sightings, frozenset({"mentalmars"}))
    assert candidate.fresh
    assert candidate.trusted


def test_the_rule_only_touches_freshness_not_trust_golden_or_order():
    sightings = [
        _sighting(code=CODE_A, source_name="mentalmars", published_at=None, golden=True),
        _sighting(code=CODE_B, source_name="Src", published_at=None, trust="official"),
    ]
    candidates = _fresh(sightings, frozenset({"mentalmars"}))
    assert [c.code for c in candidates] == [CODE_A, CODE_B]
    assert [c.fresh for c in candidates] == [False, True]
    assert candidates[0].golden
    assert candidates[1].trusted


def test_an_empty_stale_set_changes_nothing_for_any_source():
    for name in ("mentalmars", "Bluesky", ""):
        (candidate,) = _fresh([_sighting(source_name=name, published_at=None)], frozenset())
        assert candidate.fresh


def test_a_roundup_only_group_from_a_stale_source_is_not_fresh_either():
    sightings = [_sighting(source_name="mentalmars", published_at=None, roundup=True)]
    (candidate,) = _fresh(sightings, frozenset({"mentalmars"}))
    assert candidate.roundup
    assert not candidate.fresh


def test_the_stale_set_is_not_modified_by_aggregate():
    stale = frozenset({"MentalMars"})
    _fresh([_sighting(source_name="mentalmars", published_at=None)], stale)
    assert stale == frozenset({"MentalMars"})


# --- one megabyte through the whole attach path ---


def test_a_megabyte_official_post_goes_through_the_attach_path_in_bounded_time():
    junk = ("expires Oct 1 3 golden keys valid   until a golden " * 25_000)[:1_000_000]
    post = _item(title="Big post", full_text=f"{junk}\n{CODE_A}\n")
    started = time.perf_counter()
    sightings = _sights(post)
    elapsed = time.perf_counter() - started
    assert [s.code for s in sightings] == [CODE_A]
    assert elapsed < 10, f"took {elapsed:.1f}s"
