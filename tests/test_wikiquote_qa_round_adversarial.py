"""Adversarial tests for the Task 11 fixes to the Wikiquote parser.

The brief (test-engineer, 2026-09-29): the QA round taught the parser three
new tricks (headings hidden inside ignored subtrees can still start a skip,
the skip list learned the difference between a prefix and a whole heading,
and theme pages pick their citation by looking for a wiki link). Each trick
is a small rule with a lot of edges, and edges are where a hand-rolled state
machine keeps its bodies. So this file walks the edges: skips nested in skips,
harmless headings that must keep their hands to themselves, markup inside
hidden headings, and every spelling of "See also" a tired wiki editor might
produce at midnight.

Everything is synthetic markup, no network. Where a test pins a behavior the
owner might not have chosen on purpose, it's named `test_documented_...` and
says why in a comment, so a change is a decision and not a surprise.
The rendered-message test at the bottom is the one that matters for safety:
whatever a citation says, it goes through `esc()` on the way out.
"""

from __future__ import annotations

import re

import pytest

from newsbot.bot.format import esc
from newsbot.lounge.quotes import ATTRIBUTION_PREFIX, render_quote_message
from newsbot.lounge.wikiquote import parse_page

H2 = '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'


def _li(text: str) -> str:
    return f"<ul><li>{text}</li></ul>"


def _texts(body: str, pre: str = H2, title: str = "Made Up Person") -> list[str]:
    return [q.text for q in parse_page(title, pre + body).quotes]


def _attributions(body: str, pre: str = H2, title: str = "Made Up Person") -> list[str | None]:
    return [q.attribution for q in parse_page(title, pre + body).quotes]


# --- M1: skip headings hidden in ignored subtrees ---


def test_a_skip_heading_in_a_table_inside_a_noprint_box_still_skips():
    html = (
        _li("one")
        + '<div class="noprint"><table><tr><td><h2>Misattributed</h2></td></tr></table></div>'
        + _li("HIDDEN")
    )
    assert _texts(html) == ["one"]


def test_a_hidden_skip_heading_inside_an_already_skipped_section_changes_nothing():
    # The outer skip is the one in charge; the hidden one must not shorten it
    # or leave a skip level behind that swallows the section after it.
    html = (
        "<h2>Disputed</h2>"
        + _li("HIDDEN ONE")
        + "<table><tr><td><h3>Misattributed</h3></td></tr></table>"
        + _li("HIDDEN TWO")
        + "<h2>Real</h2>"
        + _li("shown")
    )
    assert _texts(html) == ["shown"]


def test_a_harmless_hidden_heading_neither_ends_a_skip_nor_joins_the_attribution():
    html = (
        "<h2>Disputed</h2>"
        + _li("HIDDEN")
        + "<table><tr><td><h2>Trivia</h2></td></tr></table>"
        + _li("ALSO HIDDEN")
        + "<h2>Back</h2>"
        + _li("shown")
    )
    page = parse_page("Made Up Person", H2 + html)
    assert [(q.text, q.attribution) for q in page.quotes] == [("shown", "Made Up Person, Back")]


def test_a_hidden_h3_skip_inside_an_h2_section_ends_at_the_next_h3():
    html = (
        "<h2>Work</h2>"
        + _li("before")
        + '<div class="noprint"><h3>Disputed</h3></div>'
        + _li("HIDDEN")
        + "<h3>Fine</h3>"
        + _li("after")
        + "<h2>Next</h2>"
        + _li("last")
    )
    assert _texts(html) == ["before", "after", "last"]


def test_a_hidden_h3_skip_inside_an_h2_section_ends_at_the_next_h2():
    html = (
        "<h2>Work</h2>"
        + "<table><tr><td><h3>Disputed</h3></td></tr></table>"
        + _li("HIDDEN")
        + "<h2>Next</h2>"
        + _li("shown")
    )
    assert _texts(html) == ["shown"]


def test_a_hidden_h3_skip_does_not_run_to_the_end_of_the_page():
    html = (
        "<h2>Work</h2>"
        + '<div class="noprint"><h3>Disputed</h3></div>'
        + _li("HIDDEN")
        + "<h2>Alpha</h2>"
        + _li("one")
        + "<h3>Beta</h3>"
        + _li("two")
        + "<h2>Gamma</h2>"
        + _li("three")
    )
    assert _texts(html) == ["one", "two", "three"]


def test_a_hidden_h4_skip_ends_at_the_next_h4_and_not_before():
    html = (
        "<h3>Section</h3>"
        + "<table><tr><td><h4>Disputed</h4></td></tr></table>"
        + _li("HIDDEN")
        + "<h4>Fine</h4>"
        + _li("shown")
    )
    assert _texts(html) == ["shown"]


def test_a_hidden_skip_heading_with_markup_inside_is_read_as_one_phrase():
    html = _li("one") + "<table><tr><td><h2><span>Mis</span>attributed</h2></td></tr></table>"
    assert _texts(html + _li("HIDDEN")) == ["one"]


def test_a_hidden_skip_heading_before_the_first_visible_h2_still_skips_its_section():
    # The hidden heading opens a skip before `_seen_h2` is true; the visible
    # h2 that follows is at the same level, so it ends the skip cleanly.
    html = "<table><tr><td><h2>Disputed</h2></td></tr></table>" + H2 + _li("shown")
    assert _texts(html, pre="") == ["shown"]


def test_a_second_hidden_skip_heading_does_not_restart_or_extend_the_skip():
    html = (
        '<div class="noprint"><h2>Disputed</h2></div>'
        + _li("HIDDEN")
        + '<div class="noprint"><h2>Notes</h2></div>'
        + _li("ALSO HIDDEN")
        + "<h2>Real</h2>"
        + _li("shown")
    )
    assert _texts(H2 + html, pre="") == ["shown"]


# --- M1: frame classes ---


def test_nested_skipped_frames_hide_everything_inside_and_stop_at_the_outer_close():
    html = (
        _li("one")
        + '<div class="disputed-begin"><div class="misattributed-begin">'
        + _li("HIDDEN")
        + "</div>"
        + _li("STILL HIDDEN")
        + "</div>"
        + _li("two")
    )
    assert _texts(html) == ["one", "two"]


@pytest.mark.parametrize("cls", ["DISPUTED-BEGIN", "Misattributed-Begin", "AtTrIbUtEd-BeGiN"])
def test_frame_classes_match_in_any_case(cls):
    html = _li("one") + f'<div class="{cls}">' + _li("HIDDEN") + "</div>" + _li("two")
    assert _texts(html) == ["one", "two"]


@pytest.mark.parametrize(
    "cls", ["not-disputed-beginning", "undisputed-begin-x", "x-attributed-begin-y"]
)
def test_documented_a_class_token_merely_containing_a_frame_marker_is_skipped_too(cls):
    # The check is substring-in-token, not token equality: the parser was
    # written to catch "disputed-begin" and its cousins with prefixes or
    # suffixes on them. The cost is that a hypothetical class with the words
    # buried in it goes too. Nothing on Wikiquote is named like that today.
    html = _li("one") + f'<div class="{cls}">' + _li("HIDDEN") + "</div>" + _li("two")
    assert _texts(html) == ["one", "two"]


def test_a_frame_marker_split_across_two_class_tokens_is_not_a_marker():
    html = _li("one") + '<div class="disputed begin">' + _li("shown") + "</div>"
    assert _texts(html) == ["one", "shown"]


# --- Skip list: exact-match words ---


@pytest.mark.parametrize(
    "heading",
    ["Notes:", "See also:", "Notes.", "Cast:", "Sources (5)", "References and notes", "See also x"],
)
def test_documented_exact_words_with_punctuation_or_company_are_ordinary_headings(heading):
    # Exact match means exact: a trailing colon, a full stop or a second word
    # makes it a different heading, and its quotes stay. Wikiquote's own
    # headings don't have colons, so the price is small.
    assert _texts(f"<h2>{heading}</h2>" + _li("shown"), pre="") == ["shown"]


@pytest.mark.parametrize(
    "heading",
    [
        "See  also",
        "See \n also",
        "  Notes  ",
        "NOTES",
        "See<br>also",
        "See&nbsp;also",
        "Cast&nbsp;",
    ],
)
def test_exact_words_survive_whitespace_case_and_nbsp(heading):
    # Internal whitespace collapses to one space before the comparison, and
    # str.split() counts a non-breaking space as whitespace.
    assert _texts(f"<h2>{heading}</h2>" + _li("HIDDEN"), pre="") == []


@pytest.mark.parametrize("heading", ["Notes​", "See​also", "See also﻿", "No­tes"])
def test_documented_zero_width_and_soft_hyphen_characters_defeat_the_skip_list(heading):
    # Zero-width space, BOM and soft hyphen aren't whitespace to Python, so
    # the heading reads as something new and its quotes stay. Nobody types
    # these into a heading by accident, and someone who does it on purpose
    # can already put a quote under a plain heading, so I left it alone.
    assert _texts(f"<h2>{heading}</h2>" + _li("shown"), pre="") == ["shown"]


# --- Skip list: prefix words ---


@pytest.mark.parametrize(
    "word",
    [
        "Disputed",
        "Misattributed",
        "Attributed",
        "Quotes about",
        "Quotations about",
        "About",
        "Said about",
        "Unsourced",
        "Doubtful",
        "Apocryphal",
        "Spurious",
    ],
)
def test_every_prefix_word_skips_as_a_whole_heading(word):
    assert _texts(f"<h2>{word}</h2>" + _li("HIDDEN"), pre="") == []


@pytest.mark.parametrize("heading", ["Disputed:", "Disputed-quotes", "About: the author"])
def test_documented_a_prefix_word_followed_by_punctuation_still_skips(heading):
    # `\b` is a word boundary, and a colon or hyphen is one.
    assert _texts(f"<h2>{heading}</h2>" + _li("HIDDEN"), pre="") == []


@pytest.mark.parametrize(
    "heading",
    ["Disputedly", "Aboutness", "Attribution", "Unattributed", "Apocrypha", "Quotesabout X"],
)
def test_words_that_only_start_like_a_prefix_word_are_not_skipped(heading):
    assert _texts(f"<h2>{heading}</h2>" + _li("shown"), pre="") == ["shown"]


def test_documented_underscore_is_a_word_character_so_disputed_underscore_is_not_skipped():
    assert _texts("<h2>Disputed_quotes</h2>" + _li("shown"), pre="") == ["shown"]


def test_documented_a_leading_word_and_not_the_topic_decides_about():
    # "About" is a prefix, so a heading that starts with it goes, whatever
    # the rest says. "Cast Away" and "Notes from Underground" are the titles
    # that motivated the exact list; "About Schmidt" is the same trap the
    # other way round, and it's still caught. Rare enough that I'm leaving it.
    assert _texts("<h2>About Schmidt</h2>" + _li("HIDDEN"), pre="") == []
    assert _texts("<h2>Cast Away</h2>" + _li("shown"), pre="") == ["shown"]
    assert _texts("<h2>Notes from Underground</h2>" + _li("shown"), pre="") == ["shown"]


@pytest.mark.parametrize("heading", ["Спорные цитаты", "争议", "Disputé", "Dis​puted"])
def test_documented_headings_in_other_scripts_or_with_zero_width_characters_are_not_skipped(
    heading,
):
    # The skip list is English, because the wiki is. A Russian "Disputed"
    # heading, or an English one with a zero-width space in it, is just a
    # heading.
    assert _texts(f"<h2>{heading}</h2>" + _li("shown"), pre="") == ["shown"]


def test_documented_a_fullwidth_latin_spelling_of_disputed_is_not_skipped():
    assert _texts("<h2>Ｄisputed</h2>" + _li("shown"), pre="") == ["shown"]


# --- Theme pages: which nested line is the citation ---

_LETTERS = "".join(f'<div class="mw-heading mw-heading2"><h2>{c}</h2></div>' for c in "ABC")


def _theme(*lines: str) -> tuple[list[str | None], int]:
    """Attributions (and the unattributed count) for one quote with `lines` as its nested items."""
    items = "".join(f"<li>{line}</li>" for line in lines)
    page = parse_page("Friendship", _LETTERS + f"<ul><li>the quote<ul>{items}</ul></li></ul>")
    assert page.kind == "theme"
    return [q.attribution for q in page.quotes], page.dropped_unattributed


def _link(href: str, text: str = "Linked Person", extra: str = "") -> str:
    return f'<a href="{href}"{extra}>{text}</a>'


def test_a_source_line_wins_over_an_earlier_translation_line():
    assert _theme("A translation, more or less.", _link("/wiki/Bob", "Bob") + ", Book") == (
        ["Bob, Book"],
        0,
    )


def test_with_several_linked_lines_the_first_one_wins():
    assert _theme(_link("/wiki/A", "First"), _link("/wiki/B", "Second")) == (["First"], 0)


def test_with_no_linked_line_the_first_non_blank_line_is_the_fallback():
    assert _theme("  ", "&nbsp;", "plain note", "another") == (["plain note"], 0)


@pytest.mark.parametrize("wrapper", ["<b>{}</b>", "<i>{}</i>", "<span>{}</span>"])
def test_documented_a_link_wrapped_in_b_or_i_still_makes_the_line_a_source_line(wrapper):
    # The rule is "the line's first link, with only whitespace before it",
    # not "the line's first child is an <a>". Wikiquote bolds author names
    # on some theme pages, and those lines should count.
    assert _theme("plain first", wrapper.format(_link("/wiki/Bob", "Bob"))) == (["Bob"], 0)


def test_a_link_after_text_does_not_make_the_line_a_source_line():
    assert _theme("plain first", "see " + _link("/wiki/Bob", "Bob")) == (["plain first"], 0)


@pytest.mark.parametrize(
    "href",
    [
        "/wiki/File:Pic.jpg",
        "/wiki/Special:Search",
        "/wiki/Category:People",
        "/wiki/",
        " /wiki/Bob ",
        "/wiki/Bob#Section",
    ],
)
def test_documented_any_href_starting_with_slash_wiki_counts_as_an_internal_link(href):
    # No namespace filter and no check that a page follows the slash. A File:
    # or Special: link opening a nested line is very unlikely in a real
    # citation, and if it happens it's at least a line the editor put first.
    assert _theme("plain first", _link(href, "Linked")) == (["Linked"], 0)


@pytest.mark.parametrize(
    "href",
    [
        "wiki/Bob",
        "./Bob",
        "../wiki/Bob",
        "/w/index.php?title=Bob",
        "//en.wikiquote.org/wiki/Bob",
        "https://en.wikiquote.org/wiki/Bob",
        "/WIKI/Bob",
        "https://example.com/",
        "#cite_note-1",
        "",
    ],
)
def test_other_shapes_of_href_do_not_count_as_internal(href):
    assert _theme("plain first", _link(href, "Linked")) == (["plain first"], 0)


def test_documented_an_absolute_link_to_wikiquote_itself_is_not_internal():
    # MediaWiki writes internal links as /wiki/Title. Somebody pasting a full
    # https://en.wikiquote.org address gets an external-looking link, so the
    # line falls back to "first non-blank" if nothing better exists.
    assert _theme(_link("https://en.wikiquote.org/wiki/Bob", "Bob")) == (["Bob"], 0)


def test_a_link_without_an_href_does_not_count():
    assert _theme("plain first", "<a>Bare</a>") == (["plain first"], 0)


@pytest.mark.parametrize("cls", ["extiw", "external", "EXTIW", "new extiw"])
def test_interwiki_and_external_classes_disqualify_a_slash_wiki_link(cls):
    assert _theme("plain first", _link("/wiki/Bob", "Bob", f' class="{cls}"')) == (
        ["plain first"],
        0,
    )


def test_documented_a_red_link_counts_as_internal():
    # A page that doesn't exist yet still gets /wiki/ in its href (with
    # `class="new"`). It's a wiki link to the author, which is all we asked.
    assert _theme("plain first", _link("/wiki/Nobody_Yet", "Nobody", ' class="new"')) == (
        ["Nobody"],
        0,
    )


def test_a_citation_list_nested_deeper_than_one_level_is_not_read():
    # Only the second list level is a citation. A third level is somebody's
    # footnote to a footnote, and its links don't get a vote.
    items = "<li>note<ul><li>" + _link("/wiki/Deep", "Deep") + "</li></ul></li>"
    page = parse_page("Friendship", _LETTERS + f"<ul><li>quote<ul>{items}</ul></li></ul>")
    assert [q.attribution for q in page.quotes] == ["note"]


def test_a_theme_quote_whose_only_nested_lines_are_blank_is_dropped_and_counted():
    assert _theme("", "  ", "<br>") == ([], 1)


def test_a_theme_quote_with_no_nested_list_is_dropped_and_counted():
    page = parse_page("Friendship", _LETTERS + "<ul><li>bare</li></ul>")
    assert (page.quotes, page.dropped_unattributed) == ([], 1)


def test_a_long_linked_citation_is_cut_to_the_cap_with_an_ellipsis():
    (attribution,), _ = _theme(_link("/wiki/Bob", "Bob") + " " + "x" * 400)
    assert attribution is not None
    assert len(attribution) == 150
    assert attribution.endswith("…")


@pytest.mark.parametrize(
    "link_text",
    [
        "@everyone",
        "@here @everyone",
        "&#64;everyone",
        "**bold** __under__ `code` ~~strike~~ ||spoiler||",
        "[click](https://example.invalid/)",
        "&lt;@123456789012345678&gt;",
        "# heading",
    ],
)
def test_a_hostile_link_text_is_escaped_when_the_message_is_rendered(link_text):
    (q,) = parse_page(
        "Friendship",
        _LETTERS
        + f'<ul><li>the quote<ul><li><a href="/wiki/Bob">{link_text}</a></li></ul></li></ul>',
    ).quotes
    message = render_quote_message(q)
    line = next(ln for ln in message.splitlines() if ln.startswith(ATTRIBUTION_PREFIX))
    # The line is exactly the prefix plus the escaped citation: nothing the
    # page said reaches the message unescaped. `esc()` has its own tests for
    # what it does; this one is about the parser handing it the whole text.
    assert line == ATTRIBUTION_PREFIX + esc(q.attribution or "")
    assert not re.search(r"@(?!\u200b)(?:everyone|here)", message)
    assert "](https://" not in message
