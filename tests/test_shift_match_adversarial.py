"""Adversarial tests for newsbot.shift.match, beyond the implementer's own suite.

The angle here is specifically "what could cause a false or duplicate
@everyone ping, or a missed code later" (test-engineer brief, 2026-09-25):
markup survivors after `text.plain_text` (HTML/BBCode remnants, entities,
`&nbsp;`), codes sitting inside URLs, Markdown code spans, emoji/CRLF/tab
adjacency, idempotent uppercasing, and a 100-code batch for first-seen
ordering. `CODE_RE` itself is exercised through `find_codes`/`is_code`
exactly like `test_shift_match.py`; nothing here reaches into the regex
directly.
"""

from __future__ import annotations

from newsbot.shift.match import find_codes, mentions_golden_key
from newsbot.text import plain_text

CODE = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"


# --- markup survivors after plain_text: the matcher only ever sees text.py's output ---


def test_code_split_by_html_bold_tag_still_matches():
    # <b> is not a block tag (no newline inserted), so stripping it leaves
    # the two halves glued back together with nothing between them --
    # which is exactly what should happen when the tag sits *inside* the
    # code's boundary, as opposed to right after it (see the xfail below).
    assert find_codes(plain_text("<b>AAAAA</b>-BBBBB-CCCCC-DDDDD-EEEEE")) == [CODE]


def test_code_split_by_bbcode_bold_tag_still_matches():
    assert find_codes(plain_text("[b]AAAAA[/b]-BBBBB-CCCCC-DDDDD-EEEEE")) == [CODE]


def test_code_with_numeric_entity_hyphen_still_matches():
    # &#45; is a numeric character reference for "-"; HTMLParser decodes it
    # (convert_charrefs=True) before the matcher ever sees the text.
    assert find_codes(plain_text("AAAAA&#45;BBBBB-CCCCC-DDDDD-EEEEE")) == [CODE]


def test_code_surrounded_by_nbsp_still_matches():
    # &nbsp; decodes to U+00A0, which _collapse's \s+ regex treats as
    # ordinary whitespace and folds down to a single space -- same
    # boundary as a real space, not a glued character.
    text = plain_text("code&nbsp;AAAAA-BBBBB-CCCCC-DDDDD-EEEEE&nbsp;end")
    assert find_codes(text) == [CODE]


def test_nbsp_directly_inside_a_hyphen_slot_breaks_the_code():
    # Not a realistic code shape, but worth pinning: &nbsp; folds to a
    # literal space, and a space where a hyphen should be breaks the
    # five-group shape entirely (no false positive, no crash).
    text = plain_text("AAAAA&nbsp;-BBBBB-CCCCC-DDDDD-EEEEE")
    assert find_codes(text) == []


# --- codes inside URLs/query strings: the regex has no notion of "inside a URL" ---


def test_code_in_a_query_string_is_matched():
    # CODE_RE's boundary lookarounds only look at the characters
    # immediately touching the code; "=" isn't a letter/digit/hyphen, so
    # a code embedded in ?code=... matches like anywhere else. Pinning
    # this as the documented contract, not asserting it "should" be
    # excluded -- match.py's docstring never scopes the search to
    # "outside of a URL", and Gearbox does post codes as reward-page
    # query params.
    assert find_codes(f"https://shift.gearboxsoftware.com/rewards?code={CODE}&src=x") == [CODE]


def test_code_as_a_bare_url_path_segment_is_matched():
    assert find_codes(f"https://example.com/redeem/{CODE}") == [CODE]


# --- Markdown code spans ---


def test_code_in_markdown_code_span_matches():
    assert find_codes(f"Redeem: `{CODE}` today") == [CODE]


def test_code_in_markdown_bold_span_matches():
    # A code inside **bold** (not <b> tags) never goes through text.py at
    # all -- Markdown asterisks aren't markup this matcher strips, they're
    # just punctuation next to the code, exactly like the existing
    # surrounded-by-parens/backticks cases.
    assert find_codes(f"**{CODE}**") == [CODE]


# --- trailing punctuation the implementer's suite didn't cover ---


def test_code_followed_by_exclamation_point():
    assert find_codes(f"New code {CODE}!") == [CODE]


def test_code_followed_by_closing_paren():
    assert find_codes(f"(see {CODE})") == [CODE]


# --- emoji, CRLF, tabs ---


def test_code_adjacent_to_emoji_matches():
    assert find_codes(f"🎉{CODE}🎉") == [CODE]


def test_code_on_its_own_crlf_line_matches():
    assert find_codes(f"line one\r\n{CODE}\r\nline two") == [CODE]


def test_code_surrounded_by_tabs_matches():
    assert find_codes(f"\t{CODE}\t") == [CODE]


# --- uppercase normalization is idempotent ---


def test_uppercasing_a_code_twice_is_a_noop():
    once = find_codes(CODE.lower())
    twice = find_codes(" ".join(once))
    assert once == twice == [CODE]


# --- find_codes on a batch of 100 codes: order and completeness ---


def test_find_codes_on_one_hundred_codes_returns_all_in_first_seen_order():
    codes = [f"{i:05d}-BBBBB-CCCCC-DDDDD-EEEEE" for i in range(100)]
    text = " ".join(codes)
    assert find_codes(text) == codes


def test_find_codes_on_one_hundred_codes_dedupes_repeats():
    codes = [f"{i:05d}-BBBBB-CCCCC-DDDDD-EEEEE" for i in range(100)]
    text = " ".join(codes) + " " + " ".join(reversed(codes))
    assert find_codes(text) == codes


# --- golden key mention next to a stripped tag ---


def test_golden_key_mention_survives_bbcode_stripping():
    assert mentions_golden_key(plain_text("[b]Golden Keys[/b] incoming")) is True


# --- real bugs found while writing the above ---


def test_bbcode_tag_abutting_following_text_does_not_swallow_the_code():
    text = plain_text(f"[b]{CODE}[/b]Golden Keys!")
    assert find_codes(text) == [CODE]


def test_html_inline_tag_abutting_following_text_does_not_swallow_the_code():
    text = plain_text(f"<b>{CODE}</b>Golden Keys!")
    assert find_codes(text) == [CODE]
