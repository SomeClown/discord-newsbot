"""Adversarial tests for full_text handling: the cap boundary, BBCode-heavy
Steam bodies, RSS multi-part content dedup, and canonicalize_items leaving
full_text alone.

Companion to test_text.py (plain_text's own contract) and
test_collectors_rss_steam.py (collector-level full_text fixtures). These
go after the specific failure mode test-engineer was asked to chase: a
code that straddles the 100,000-char cap should never come out as a
partial, garbled match, and a source's own markup shouldn't be able to
either duplicate a code's announcement or hide it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from newsbot.collectors.base import RawItem
from newsbot.collectors.rss import _entry_full_text
from newsbot.pipeline.normalize import canonicalize_items
from newsbot.shift.match import find_codes
from newsbot.text import plain_text

CODE = "AAAA1-BBBBB-CCCCC-DDDDD-EEEEE"


# --- 100,000-char cap boundary ---


def test_code_straddling_the_cap_is_dropped_not_partially_matched():
    # _truncate cuts on the last space at-or-before the limit. A code has
    # no internal spaces, so when the cut lands inside it, rsplit(" ", 1)
    # backs up past the *whole* code (to the space right before it) --
    # the code disappears rather than surviving as a mangled fragment
    # that could, worse, false-match as something else.
    filler = "word " * 19995  # 99,975 chars; the code starts at 99,975 and ends past 100,000
    body = filler + CODE + " tail filler text"
    result = plain_text(body)
    assert len(result) <= 100_001  # +1 for _truncate's ellipsis
    assert CODE not in result
    assert find_codes(result) == []


def test_code_placed_safely_before_the_cap_survives():
    # Sanity check for the above: a code that fits entirely inside the cap
    # (with room for a trailing space before the cut) is unaffected.
    filler = "word " * 100  # 500 chars, nowhere near the cap
    body = f"{CODE} " + filler
    result = plain_text(body, limit=100_000)
    assert find_codes(result) == [CODE]


def test_body_exactly_at_the_cap_length_is_unchanged():
    body = "a" * 100_000
    assert plain_text(body) == body


def test_body_one_char_past_the_cap_is_truncated_with_ellipsis():
    body = "a" * 100_001
    result = plain_text(body)
    assert result.endswith("…")
    assert len(result) <= 100_001


# --- Steam BBCode-heavy contents ---


def test_code_survives_nested_list_and_quote_bbcode():
    steam = (
        f"[list][*]Fixed a crash[*]New SHiFT code: {CODE} [/list][quote]Thanks for playing![/quote]"
    )
    assert find_codes(plain_text(steam)) == [CODE]


def test_code_survives_url_and_img_bbcode_tags():
    steam = f"[url=https://example.com]Read more[/url] code {CODE} [img]https://x/y.png[/img]"
    assert find_codes(plain_text(steam)) == [CODE]


def test_golden_key_mention_survives_heavy_bbcode():
    from newsbot.shift.match import mentions_golden_key

    steam = "[b][color=gold]Golden Keys[/color][/b] are back this weekend!"
    assert mentions_golden_key(plain_text(steam)) is True


# --- RSS entries with multiple content parts: no duplication ---


def test_entry_full_text_combines_distinct_content_parts():
    entry = {
        "content": [{"value": "Text part one."}, {"value": "Text part two with a code."}],
        "summary": "Text part one.",
    }
    result = _entry_full_text(entry)
    assert result == "Text part one.\nText part two with a code."
    # The summary duplicated content[0] verbatim; it must not appear twice.
    assert result.count("Text part one.") == 1


def test_entry_full_text_summary_identical_to_only_content_part_not_duplicated():
    entry = {"content": [{"value": "Same text."}], "summary": "Same text."}
    assert _entry_full_text(entry) == "Same text."


def test_entry_full_text_summary_differing_from_content_is_appended():
    entry = {"content": [{"value": "Body text."}], "summary": "A different summary line."}
    result = _entry_full_text(entry)
    assert result == "Body text.\nA different summary line."


def test_entry_full_text_three_content_parts_all_kept_once():
    entry = {
        "content": [
            {"value": "Part A."},
            {"value": "Part B."},
            {"value": "Part A."},  # a feed repeating itself across content entries
        ],
        "summary": "Part A.",
    }
    result = _entry_full_text(entry)
    # Duplicate-suppression only guards the summary append, not content
    # entries against each other -- pinning the current (permissive)
    # behavior rather than assuming a stronger guarantee that isn't there.
    assert result == "Part A.\nPart B.\nPart A."


def test_entry_full_text_no_content_falls_back_to_description():
    entry = {"description": "Only a description here."}
    assert _entry_full_text(entry) == "Only a description here."


def test_entry_full_text_all_empty_returns_empty_string():
    entry = {}
    assert _entry_full_text(entry) == ""


# --- canonicalize_items leaves full_text alone ---


def test_canonicalize_items_preserves_full_text():
    item = RawItem(
        url="https://example.com/a?utm_source=feed",
        title="Title",
        excerpt="Excerpt",
        source_name="Src",
        trust="community",
        published_at=datetime(2026, 9, 25, tzinfo=UTC),
        full_text=f"Full body with a code: {CODE}",
    )
    [result] = canonicalize_items([item])
    assert result.full_text == f"Full body with a code: {CODE}"
    assert result.url == "https://example.com/a"


def test_canonicalize_items_dedupe_keeps_higher_trust_items_full_text():
    lower = RawItem(
        url="https://example.com/a",
        title="Title",
        excerpt="Excerpt",
        source_name="Community Blog",
        trust="community",
        published_at=None,
        full_text="community version, no code here",
    )
    higher = RawItem(
        url="https://example.com/a",
        title="Title",
        excerpt="Excerpt",
        source_name="Official",
        trust="official",
        published_at=None,
        full_text=f"official version with {CODE}",
    )
    [result] = canonicalize_items([lower, higher])
    assert result.full_text == f"official version with {CODE}"
