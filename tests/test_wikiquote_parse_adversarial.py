"""Adversarial tests for `parse_page`, beyond `test_wikiquote_parse.py`.

The brief (test-engineer, 2026-09-29): the parser reads HTML that strangers
can edit, on a wiki that promises balanced markup and has never once been
asked to prove it. So this file feeds it the stuff the fixtures would never
contain: thousands of nested lists, stray end tags, entities that spell out
pings, headings in the wrong places, and a lone surrogate that JSON is
perfectly happy to hand over. Everything is synthetic; the IDs are made up
and no real person is quoted.

Two kinds of test live here. Most pin what the parser does today, including
a few behaviors the owner might not have picked on purpose (they're named
`test_documented_...` so a change is a decision, not a surprise). The
bugs it found (quadratic parsing, a lone surrogate) are fixed and their xfail
markers are gone, so a failure there is a regression.
"""

from __future__ import annotations

import re
import time

import pytest

from newsbot.lounge.quotes import (
    ATTRIBUTION_PREFIX,
    QUOTE_HEADER,
    WIKIQUOTE_LINK_LABEL,
    Quote,
    render_quote_message,
)
from newsbot.lounge.wikiquote import MAX_WIKIQUOTE_CHARS, page_url, parse_page

ZWSP = "​"
UID = "123456789012345678"  # 18 digits, synthetic
H2 = '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'


def _parse(body: str, title: str = "Made Up Person", pre: str = H2):
    return parse_page(title, pre + body)


def _texts(body: str, **kwargs) -> list[str]:
    return [q.text for q in _parse(body, **kwargs).quotes]


def _cite(citation_html: str, title: str = "Made Up Person") -> str | None:
    """The attribution of one quote whose nested citation item holds `citation_html`."""
    (q,) = _parse(f"<ul><li>t<ul><li>{citation_html}</li></ul></li></ul>", title=title).quotes
    return q.attribution


def _letters(letters: str) -> str:
    return "".join(
        f'<div class="mw-heading mw-heading2"><h2 id="{c}">{c}</h2></div>' for c in letters
    )


# --- Hostile structure: depth, size, unclosed and stray tags ---


def test_thousands_of_nested_lists_do_not_recurse_or_crash():
    depth = 3000
    html = "<ul><li>outer" + "<ul><li>x" * depth + "</li></ul>" * depth + "</li></ul>"
    page = _parse(html)
    assert [q.text for q in page.quotes] == ["outer"]


def test_thousands_of_nested_bare_lists_yield_nothing_and_do_not_crash():
    depth = 3000
    page = _parse("<ul>" * depth + "<li>too deep to be a quote</li>" + "</ul>" * depth)
    assert page.quotes == []


def test_a_couple_hundred_thousand_open_divs_are_fine():
    page = _parse("<div>" * 200_000 + "<ul><li>never closed, still nothing</li></ul>")
    assert page.kind == "author"


def test_a_multi_megabyte_balanced_page_parses_fast():
    item = "<ul><li>quote number %d is here<ul><li>cite %d</li></ul></li></ul>\n"
    html = H2 + "".join(item % (i, i) for i in range(45_000))
    assert len(html) > 3_000_000
    start = time.perf_counter()
    page = parse_page("Made Up Person", html)
    elapsed = time.perf_counter() - start
    assert len(page.quotes) == 45_000
    assert elapsed < 15, f"a 3 MB page took {elapsed:.1f}s"


def test_stray_end_tags_alone_are_cheap_and_change_nothing():
    html = "<ul><li>keeper</li></ul></b></i></p></div></span>" * 4000
    start = time.perf_counter()
    page = _parse(html)
    assert time.perf_counter() - start < 5
    assert len(page.quotes) == 4000


def test_a_stray_end_tag_does_not_swallow_the_next_quote():
    assert _texts("<ul><li>one</li></ul></b></table></p><ul><li>two</li></ul>") == ["one", "two"]


def test_unclosed_inline_tags_inside_a_quote_are_closed_by_the_list_item():
    assert _texts("<ul><li>one <b><i>bold and italic<li>two</li></ul>") == [
        "one bold and italic",
        "two",
    ]


def test_a_missing_li_end_tag_before_the_next_li_still_splits_quotes():
    assert _texts("<ul><li>one<li>two<li>three</ul>") == ["one", "two", "three"]


def test_documented_an_item_still_open_at_end_of_input_is_dropped_not_a_crash():
    # Truncated input loses the quote it was in the middle of; nothing before it is lost.
    assert _texts("<ul><li>kept</li><li>tail <b>quote <i>never closed") == ["kept"]


def test_unclosed_nested_list_items_parse_in_linear_time():
    html = "<ul><li>x <b><i>" * 6000
    start = time.perf_counter()
    _parse(html)
    assert time.perf_counter() - start < 0.5


def test_a_lone_open_tag_with_attributes_and_junk_is_survivable():
    junk = '<ul <li class="a>b">text</li></ul><li \x00 data="x">y</li><<<>>>&&&;;;'
    _parse(junk)  # must simply not raise


# --- Hostile content: entities, comments, scripts, breaks ---


def test_br_variants_all_become_line_breaks():
    assert _texts("<ul><li>a<br>b<br/>c<BR />d<br\n>e</li></ul>") == ["a\nb\nc\nd\ne"]


def test_br_at_the_edges_and_doubled_leaves_no_blank_lines():
    assert _texts("<ul><li><br>a<br><br><br>b<br></li></ul>") == ["a\nb"]


def test_entities_and_numeric_references_are_decoded_once():
    (text,) = _texts(f"<ul><li>&amp; &#x40;everyone &lt;@{UID}&gt; &amp;amp; &#64;here</li></ul>")
    assert text == f"& @everyone <@{UID}> &amp; @here"


def test_broken_numeric_references_become_replacement_characters_not_errors():
    (text,) = _texts("<ul><li>a &#0; b &#xD800; c &#x110000; d &#99999999999; e</li></ul>")
    assert text.startswith("a ")
    assert text.endswith(" e")
    assert "\x00" not in text
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def test_comments_cdata_and_processing_instructions_are_not_text():
    html = "<ul><li>a<!-- hidden --> b<![CDATA[ smuggled ]]> c<?php echo 1 ?> d</li></ul>"
    assert _texts(html) == ["a b c d"]


def test_script_and_style_contents_are_never_quotes():
    html = (
        "<ul><li>keep<script>document.write('<ul><li>FAKE ONE</li></ul>')</script> "
        "tail<style>li { content: 'FAKE TWO' }</style></li></ul>"
        "<script><ul><li>FAKE THREE</li></ul></script>"
        "<ul><li>after</li></ul>"
    )
    assert _texts(html) == ["keep tail", "after"]


def test_documented_an_unclosed_script_swallows_the_rest_of_the_page():
    # html.parser treats everything after an unclosed <script> as script text. The
    # quotes before it survive, nothing after it does, and nothing blows up.
    assert _texts("<ul><li>before</li></ul><script>oops<ul><li>lost</li></ul>") == ["before"]


def test_superscript_wrapped_around_the_whole_quote_leaves_nothing_to_post():
    assert _texts("<ul><li><sup>the whole thing is a footnote</sup></li></ul>") == []


def test_superscript_inside_a_quote_is_cut_out_of_it():
    assert _texts("<ul><li>before<sup>[1]</sup> after<sup><b>[2]</b></sup>.</li></ul>") == [
        "before after."
    ]


def test_references_class_on_any_element_hides_its_subtree():
    html = (
        '<ul><li>real<span class="reference references">FOOTNOTE</span></li></ul>'
        '<div class="references"><ul><li>BIBLIO ENTRY</li></ul></div>'
        '<div class="mw-references-wrap"><ul><li>WRAPPED ENTRY</li></ul></div>'
        '<ul class="noprint"><li>NOPRINT ENTRY</li></ul>'
        "<ul><li>after</li></ul>"
    )
    assert _texts(html) == ["real", "after"]


def test_a_table_is_ignored_wherever_it_sits():
    html = (
        "<ul><li>one</li></ul>"
        "<table><tr><td><ul><li>IN TABLE</li></ul></td></tr></table>"
        "<ul><li>two</li></ul>"
    )
    assert _texts(html) == ["one", "two"]


def test_a_heading_inside_a_table_does_not_start_or_end_a_section():
    html = (
        "<ul><li>one</li></ul>"
        "<table><tr><td><h2>Disputed</h2></td></tr></table>"
        "<ul><li>two</li></ul>"
    )
    assert _texts(html) == ["one", "two"]


def test_a_table_inside_a_quote_drops_the_table_not_the_quote():
    assert _texts("<ul><li>text<table><tr><td>CELL</td></tr></table> more</li></ul>") == [
        "text more"
    ]


def test_documented_a_heading_nested_inside_a_list_item_still_counts_as_a_heading():
    # A <h2>Disputed</h2> inside an <li> skips what follows, same as a top-level one.
    html = "<ul><li>a<h2>Disputed</h2>b</li></ul><ul><li>after</li></ul>"
    assert _texts(html) == ["ab"]


def test_headings_outside_h2_to_h4_are_not_sections():
    html = "<h5>Disputed</h5><ul><li>one</li></ul><h1>Notes</h1><ul><li>two</li></ul>"
    assert _texts(html) == ["one", "two"]


def test_quote_text_before_the_first_h2_is_never_a_quote():
    assert _texts("<ul><li>lead</li></ul>", pre="") == []


# --- Skipped-section headings in odd spellings ---


@pytest.mark.parametrize(
    "heading",
    [
        "<h2>Disputed</h2>",
        "<h2>DISPUTED</h2>",
        "<h2>disputed</h2>",
        "<h2>  Disputed  </h2>",
        "<h2>\n\tDisputed\n</h2>",
        "<h2><span>Disputed</span></h2>",
        "<h2>Mis<b>attributed</b></h2>",
        "<h2>Mis<!-- x -->attributed</h2>",
        "<h2>Quotes about <i>Somebody</i></h2>",
        "<h2>Notes:</h2>",
        "<h2>Cast of characters</h2>",
        "<h2>See<br>also</h2>",
        "<h3>Disputed</h3>",
        "<h4>Disputed</h4>",
    ],
)
def test_odd_spellings_of_a_skipped_heading_still_skip(heading):
    html = f"<ul><li>ok</li></ul>{heading}<ul><li>HIDDEN</li></ul>"
    assert _texts(html) == ["ok"]


@pytest.mark.parametrize(
    "heading",
    [
        "<h2>Dis puted</h2>",
        "<h2>Castle</h2>",
        "<h2>Notesque</h2>",
        "<h2>Dispute</h2>",
        "<h2>Works</h2>",
    ],
)
def test_lookalike_headings_do_not_skip(heading):
    html = f"<ul><li>ok</li></ul>{heading}<ul><li>SHOWN</li></ul>"
    assert _texts(html) == ["ok", "SHOWN"]


def test_a_skipped_h3_ends_at_the_next_h3_and_not_at_an_h4():
    html = (
        "<h3>Disputed</h3><ul><li>HIDDEN ONE</li></ul>"
        "<h4>Deeper</h4><ul><li>HIDDEN TWO</li></ul>"
        "<h3>Fine</h3><ul><li>shown</li></ul>"
    )
    assert _texts(html) == ["shown"]


def test_a_skipped_h4_ends_at_a_higher_heading():
    html = "<h4>Disputed</h4><ul><li>HIDDEN</li></ul><h2>Later</h2><ul><li>shown</li></ul>"
    assert _texts(html) == ["shown"]


def test_a_second_skipped_heading_inside_a_skipped_section_does_not_extend_it():
    html = (
        "<h2>Disputed</h2><h3>Notes</h3><ul><li>HIDDEN</li></ul>"
        "<h2>Back</h2><ul><li>shown</li></ul>"
    )
    assert _texts(html) == ["shown"]


# --- Page kind detection ---


@pytest.mark.parametrize(
    ("title", "body", "kind"),
    [
        # theme
        ("Someone", _letters("ABC"), "theme"),
        ("Someone", _letters("AB"), "author"),
        ("Someone", _letters("A") + "<h2>Quotes</h2>" + _letters("BC"), "theme"),
        ("Someone", "<h2>AB</h2><h2>a</h2><h2>b</h2><h2>c</h2>", "author"),
        ("Someone", "<h2>A</h2><h3>B</h3><h4>C</h4>", "theme"),
        # cast/dialogue beat everything, but only as an h2, case-insensitive
        ("Someone", "<h2>Cast</h2>", "work"),
        ("Someone", "<h2>cast</h2>", "work"),
        ("Someone", "<h2>Cast </h2>", "work"),
        ("Someone", "<h2><span>Cast</span></h2>", "work"),
        ("Someone", "<h2>Dialogue</h2>", "work"),
        ("Someone", "<h2>Cast</h2>" + _letters("ABC"), "work"),
        ("Someone", "<h3>Cast</h3>", "author"),
        ("Someone", "<h2>Cast list</h2>", "author"),
        # titles: only the last parenthetical counts, and only its whole words
        ("Made Up (film)", "", "work"),
        ("Made Up (Film)", "", "work"),
        ("Made Up (2004 film)", "", "work"),
        ("Made Up (TV series)", "", "work"),
        ("Made Up (tv)", "", "work"),
        ("Made Up (series)", "", "work"),
        ("Made Up (film) ", "", "work"),
        ("Made Up (film)", _letters("ABC"), "work"),
        ("Made Up (filmography)", "", "author"),
        ("Made Up (film) (novel)", "", "author"),
        ("Made Up (novel)", "", "author"),
        # Changed on the owner's lead's call: this is a gaming server, so a
        # "(video game)" page is a work and gets "Character, Title".
        ("Made Up (video game)", "", "work"),
        ("Made Up (film) part 2", "", "author"),
        ("Made Up film", "", "author"),
    ],
)
def test_documented_page_kind_classification(title, body, kind):
    assert parse_page(title, body).kind == kind


def test_documented_a_work_page_without_film_in_the_title_but_with_dialogue_is_a_work():
    html = (
        "<h2>Quotes</h2><ul><li>hi</li></ul>"
        "<h2>Dialogue</h2><dl><dd>A: one</dd><dd>B: two</dd></dl>"
    )
    page = parse_page("Some Show", html)
    assert page.kind == "work"
    assert [q.text for q in page.quotes] == ["hi", "A: one\nB: two"]


def test_documented_a_video_game_page_is_treated_as_a_work_page():
    # This used to pin the author style. Changed on the owner's lead's call:
    # it's a gaming server, so a game page's quotes read "Character, Title".
    html = "<h2>Quotes</h2><h3>Some Character</h3><ul><li>line</li></ul>"
    (q,) = parse_page("Made Up (video game)", html).quotes
    assert q.attribution == "Some Character, Made Up (video game)"


def test_dialogue_on_an_author_or_theme_page_is_not_a_quote():
    html = "<h2>Quotes</h2><dl><dd>Full text online</dd></dl>"
    assert parse_page("Someone", html).quotes == []
    assert parse_page("Someone", _letters("ABC") + html).quotes == []


def test_a_work_page_dialogue_exchange_keeps_line_order_and_skips_nested_lists():
    html = (
        "<h2>Quotes</h2><h2>Dialogue</h2>"
        "<dl><dd>one</dd><dd>two<ul><li>NESTED LIST</li></ul></dd><dd>three</dd></dl>"
        "<hr><dl><dd>solo</dd></dl>"
    )
    assert [q.text for q in parse_page("Made Up (film)", html).quotes] == [
        "one\ntwo\nthree",
        "solo",
    ]


def test_an_hr_inside_a_dialogue_list_ends_the_exchange():
    html = "<h2>Quotes</h2><dl><dd>one</dd><hr><dd>two</dd></dl>"
    assert [q.text for q in parse_page("Made Up (film)", html).quotes] == ["one", "two"]


def test_a_theme_page_with_a_one_line_quote_and_no_citation_is_dropped_and_counted():
    html = _letters("ABC") + (
        "<h2>Quotes</h2><ul><li>cited<ul><li>Some Person</li></ul></li><li>orphan</li></ul>"
    )
    page = parse_page("Made Up Theme", html)
    assert [(q.text, q.attribution) for q in page.quotes] == [("cited", "Some Person")]
    assert page.dropped_unattributed == 1


def test_a_theme_page_never_borrows_its_own_title_as_the_speaker():
    html = _letters("ABC") + "<h2>Quotes</h2><ul><li>orphan</li></ul>"
    page = parse_page("Made Up Theme", html)
    assert page.quotes == []
    assert page.dropped_unattributed == 1


# --- Attribution correctness ---


@pytest.mark.parametrize("name", ["Quotes", "QUOTES", "Sourced", "Quotations", "quotations "])
def test_generic_section_headings_never_appear_in_an_attribution(name):
    html = f"<h2>{name}</h2><h3>{name}</h3><ul><li>t<ul><li>cite</li></ul></li></ul>"
    (q,) = parse_page("Made Up Person", html).quotes
    assert q.attribution == "Made Up Person, cite"


def test_generic_heading_names_are_dropped_from_the_chain_but_real_ones_stay():
    html = "<h2>Sourced</h2><h3>A Book</h3><h4>Chapter 2</h4><ul><li>t</li></ul>"
    (q,) = parse_page("Made Up Person", html).quotes
    assert q.attribution == "Made Up Person, A Book, Chapter 2"


def test_dialogue_h3_is_generic_on_a_work_page_too():
    html = "<h2>Dialogue</h2><h3>Dialogue</h3><dl><dd>x</dd></dl>"
    (q,) = parse_page("Made Up (film)", html).quotes
    assert q.attribution == "Made Up (film)"


def test_citation_over_150_characters_is_cut_to_149_plus_an_ellipsis():
    attribution = _cite("x" * 400)
    assert attribution == "Made Up Person, " + "x" * 149 + "…"


def test_citation_of_exactly_150_characters_is_left_alone():
    assert _cite("y" * 150) == "Made Up Person, " + "y" * 150


def test_citation_cut_does_not_leave_a_space_before_the_ellipsis():
    assert _cite("x" * 148 + " " + "yz") == "Made Up Person, " + "x" * 148 + "…"


def test_citation_of_astral_characters_is_cut_between_whole_code_points():
    attribution = _cite("😀" * 200)
    assert attribution is not None
    assert attribution.endswith("😀" * 149 + "…")
    attribution.encode("utf-16-le")  # a mid-surrogate cut would raise here


def test_cut_citation_still_renders_within_a_message():
    quote = _parse(f"<ul><li>t<ul><li>{'😀' * 400}</li></ul></li></ul>").quotes[0]
    assert render_quote_message(quote).count("\n") == 3


@pytest.mark.parametrize("empty", ["", "   ", "<sup>[1]</sup>", "<b></b>", "<!-- c -->"])
def test_an_empty_citation_leaves_just_the_subject(empty):
    assert _cite(empty) == "Made Up Person"


def test_documented_a_bare_bracket_marker_citation_is_kept_as_text():
    # Wikiquote wraps footnote markers in <sup>, which is stripped. A literal
    # "[1]" typed into a citation isn't, so it's attributed as if it meant something.
    assert _cite("[1]") == "Made Up Person, [1]"


def test_a_citation_that_is_a_nested_list_of_several_items_gives_no_citation_at_all():
    # Only a list one level down is read as the citation; a third level is skipped
    # entirely, so this doesn't get "first item only", it gets nothing.
    assert _cite("<ul><li>one</li><li>two</li></ul>") == "Made Up Person"


def test_only_the_first_of_several_sibling_citation_items_is_used():
    (q,) = _parse("<ul><li>t<ul><li>first</li><li>second</li></ul></li></ul>").quotes
    assert q.attribution == "Made Up Person, first"


def test_a_second_nested_list_after_the_first_is_ignored():
    html = "<ul><li>t<ul><li>c1</li></ul><ul><li>c2</li></ul></li></ul>"
    assert _parse(html).quotes[0].attribution == "Made Up Person, c1"


def test_documented_an_empty_first_citation_item_uses_up_the_citation_slot():
    html = "<ul><li>t<ul><li></li><li>second</li></ul></li></ul>"
    assert _parse(html).quotes[0].attribution == "Made Up Person"


def test_a_citation_never_leaks_into_the_quote_text():
    (q,) = _parse("<ul><li>the words<ul><li>the source</li></ul></li></ul>").quotes
    assert q.text == "the words"


def test_a_deeper_list_inside_the_citation_stays_out_of_both():
    html = "<ul><li>t<ul><li>c1<ul><li>DEEP</li></ul></li></ul></li></ul>"
    (q,) = _parse(html).quotes
    assert q.text == "t"
    assert "DEEP" not in (q.attribution or "")


def test_ordered_lists_are_never_quotes():
    assert _texts("<ol><li>numbered</li></ol>") == []


# --- Size limits ---


def test_a_quote_of_exactly_the_limit_is_kept_and_one_over_is_dropped():
    page = _parse(
        f"<ul><li>{'a' * MAX_WIKIQUOTE_CHARS}</li><li>{'b' * (MAX_WIKIQUOTE_CHARS + 1)}</li></ul>"
    )
    assert [len(q.text) for q in page.quotes] == [MAX_WIKIQUOTE_CHARS]
    assert page.dropped_long == 1


def test_every_kept_quote_renders_inside_a_discord_message():
    from newsbot.lounge.quotes import fits

    page = _parse(f"<ul><li>{'*' * MAX_WIKIQUOTE_CHARS}<ul><li>{'_' * 400}</li></ul></li></ul>")
    assert page.quotes
    assert all(fits(q) for q in page.quotes)


def test_a_lone_surrogate_in_the_page_html_does_not_crash_the_parse():
    import json

    html = json.loads('"<h2>Quotes</h2><ul><li>half an emoji \\ud83d here</li><li>fine</li></ul>"')
    page = parse_page("Made Up Person", html)
    assert [q.text for q in page.quotes] == ["fine"]


# --- Mentions and links, end to end through the renderer ---

_LIVE_EVERYONE_RE = re.compile(r"@(?!" + ZWSP + r")(?:everyone|here)")
_LIVE_MENTION_RE = re.compile(r"<(?!" + ZWSP + r")(?:@|#|/)[!&]?[\w:-]")
_LIVE_INVITE_RE = re.compile(r"discord(?:app)?\.(?:gg|com/invite)(?!" + ZWSP + r")/", re.I)


def _assert_inert(message: str) -> None:
    """The message can't ping, embed or invite anyone, apart from its one genuine link line."""
    lines = message.split("\n")
    prefix = f"{WIKIQUOTE_LINK_LABEL} <"
    body = lines
    if lines[-1].startswith(prefix):
        link = lines[-1][len(prefix) : -1]
        assert lines[-1].endswith(">")
        assert re.fullmatch(r"https://en\.wikiquote\.org/wiki/[^\s<>`\\|\"]*", link), lines[-1]
        body = lines[:-1]
    joined = "\n".join(body)
    assert "://" not in joined, joined
    assert not _LIVE_EVERYONE_RE.search(joined), joined
    assert not _LIVE_MENTION_RE.search(joined), joined
    assert not _LIVE_INVITE_RE.search(joined), joined
    assert lines[0] == QUOTE_HEADER
    assert (
        sum(1 for line in lines if line.startswith(WIKIQUOTE_LINK_LABEL)) <= 1 or "<br" in message
    )


HOSTILE_SNIPPETS = [
    "&#x40;everyone",
    "&#64;here",
    "@&#101;veryone",
    f"&lt;@{UID}&gt;",
    f"&lt;@&#33;{UID}&gt;",
    f"&lt;@&amp;{UID}&gt;",
    f"&lt;#{UID}&gt;",
    f"&lt;/cmd:{UID}&gt;",
    "[click here](https://evil.example/x)",
    "&lt;https://evil.example/x&gt;",
    "https://evil.example/x",
    "discord.gg/abcdef",
    "https://discord.com/invite/abcdef",
    "```code fence```",
    "# heading?",
    "||spoiler||",
    "**bold** __under__ ~~strike~~",
    "&#x202e;rtl override",
]


@pytest.mark.parametrize("snippet", HOSTILE_SNIPPETS)
def test_hostile_quote_text_is_inert_after_render(snippet):
    (q,) = _parse(f"<ul><li>{snippet}</li></ul>").quotes
    _assert_inert(render_quote_message(q))


@pytest.mark.parametrize("snippet", HOSTILE_SNIPPETS)
def test_hostile_citation_is_inert_after_render(snippet):
    q = _parse(f"<ul><li>safe<ul><li>{snippet}</li></ul></li></ul>").quotes[0]
    _assert_inert(render_quote_message(q))


@pytest.mark.parametrize("snippet", HOSTILE_SNIPPETS)
def test_hostile_heading_context_is_inert_after_render(snippet):
    (q,) = parse_page(
        "Made Up Person", f"<h2>Quotes</h2><h3>{snippet}</h3><ul><li>safe</li></ul>"
    ).quotes
    _assert_inert(render_quote_message(q))


@pytest.mark.parametrize("snippet", HOSTILE_SNIPPETS)
def test_hostile_page_title_is_inert_after_render(snippet):
    import html as html_lib

    title = html_lib.unescape(snippet)
    (q,) = parse_page(title, f"{H2}<ul><li>safe</li></ul>").quotes
    _assert_inert(render_quote_message(q))


def test_br_cannot_forge_a_second_link_line_that_survives_render():
    forged = f"real words<br>{WIKIQUOTE_LINK_LABEL} &lt;https://evil.example/x&gt;"
    (q,) = _parse(f"<ul><li>{forged}</li></ul>").quotes
    message = render_quote_message(q)
    assert "https://evil.example" not in message
    assert message.endswith(f"<{page_url('Made Up Person')}>")


def test_br_cannot_forge_an_attribution_line_with_a_live_link():
    (q,) = _parse("<ul><li>words<br>~ Somebody https://evil.example/y</li></ul>").quotes
    assert "https://evil.example" not in render_quote_message(q)


def test_multi_line_citation_html_cannot_fake_the_link_line():
    forged = f"cite<br>{WIKIQUOTE_LINK_LABEL} &lt;https://evil.example/z&gt;"
    q = _parse(f"<ul><li>t<ul><li>{forged}</li></ul></li></ul>").quotes[0]
    lines = render_quote_message(q).split("\n")
    assert sum(1 for line in lines if line.startswith(ATTRIBUTION_PREFIX)) == 1
    assert "https://evil.example" not in "\n".join(lines)
    assert lines[-1] == f"{WIKIQUOTE_LINK_LABEL} <{page_url('Made Up Person')}>"


# --- page_url from hostile titles ---

_LINK_LINE_RE = re.compile(r"https://en\.wikiquote\.org/wiki/[A-Za-z0-9._~%/()',:@!$&*+;=?#-]*")

HOSTILE_TITLES = [
    "a b",
    "a>b",
    "a<b",
    "a`b",
    "a\nb",
    "a\r\nb",
    "a\tb",
    "a?b=c",
    "a#frag",
    "a&b=c",
    "é",
    "日本語",
    "😀 party",
    "Caf%C3%A9",
    "100%",
    "a\\b",
    "a|b",
    'a"b',
    "[x](y)",
    "a\u2028b",
    "a\u200bb",
    "a\x00b",
    "/",
    "..",
    "a/../b",
    "@everyone",
    f"<@{UID}>",
    "x> https://evil.example <y",
    "  padded  ",
]


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_page_url_of_a_hostile_title_is_a_link_the_renderer_accepts(title):
    link = page_url(title)
    assert link.startswith("https://en.wikiquote.org/wiki/")
    rendered = render_quote_message(Quote("text", "someone", link))
    assert rendered.endswith(f"\n{WIKIQUOTE_LINK_LABEL} <{link}>")
    assert rendered.count("\n") == 3


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_page_url_never_emits_a_character_that_could_break_the_wrapper(title):
    tail = page_url(title).removeprefix("https://en.wikiquote.org/wiki/")
    assert not re.search(r"[\s<>`\\|\"\[\]{}^]", tail)
    assert not any(ord(c) > 0x7E or ord(c) < 0x21 for c in tail)


@pytest.mark.parametrize("title", ["a?b", "a#b", "a&b", "a/b", "Caf%C3%A9", "a%zz"])
def test_page_url_round_trips_reserved_characters_through_percent_encoding(title):
    from urllib.parse import unquote

    tail = page_url(title).removeprefix("https://en.wikiquote.org/wiki/")
    assert unquote(tail) == title.replace(" ", "_")


def test_an_already_percent_encoded_title_is_encoded_again_not_trusted():
    assert page_url("Caf%C3%A9") == "https://en.wikiquote.org/wiki/Caf%25C3%25A9"
    assert page_url("Caf%C3%A9") != page_url("Café")


def test_page_url_spaces_become_underscores_like_the_wiki_does():
    assert page_url("Oscar Wilde") == "https://en.wikiquote.org/wiki/Oscar_Wilde"


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_a_parsed_page_with_a_hostile_title_renders_with_its_layout_intact(title):
    page = parse_page(title, f"{H2}<ul><li>safe<ul><li>cite</li></ul></li></ul>")
    (q,) = page.quotes
    lines = render_quote_message(q).split("\n")
    assert len(lines) == 4
    assert lines[0] == QUOTE_HEADER
    assert lines[1] == "safe"
    assert lines[2].startswith(ATTRIBUTION_PREFIX)
    assert lines[3].startswith(f"{WIKIQUOTE_LINK_LABEL} <https://en.wikiquote.org/wiki/")
    assert lines[3].endswith(">")


def test_documented_an_empty_title_yields_a_bare_wiki_link_the_renderer_accepts():
    # Not a real page, but not a crash or a broken layout either.
    assert page_url("") == "https://en.wikiquote.org/wiki/"
    assert render_quote_message(Quote("t", None, page_url(""))).endswith(
        f"{WIKIQUOTE_LINK_LABEL} <https://en.wikiquote.org/wiki/>"
    )
