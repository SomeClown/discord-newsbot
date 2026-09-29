"""Tests for `parse_page` against saved Wikiquote pages, no network.

Two of the fixtures are real (Oscar Wilde, Friendship; trimmed by deleting
whole elements, never by editing text) and one is synthetic, because modern
film pages are the copyright risk and public-domain ones rarely have
dialogue. Expected strings are copied from the fixture files, and each
"this must be absent" check first asserts that the marker really is in the
fixture, so a vacuous pass can't sneak in.
"""

import logging
from pathlib import Path

import pytest

from newsbot.lounge.quotes import fits
from newsbot.lounge.wikiquote import MAX_WIKIQUOTE_CHARS, ParsedPage, page_url, parse_page

FIXTURES = Path(__file__).parent / "fixtures" / "wikiquote"
FILM_TITLE = "The Teapot Heist (film)"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wilde() -> ParsedPage:
    return parse_page("Oscar Wilde", _load("oscar_wilde.html"))


@pytest.fixture(scope="module")
def friendship() -> ParsedPage:
    return parse_page("Friendship", _load("friendship.html"))


@pytest.fixture(scope="module")
def film() -> ParsedPage:
    return parse_page(FILM_TITLE, _load("synthetic_film.html"))


def _blob(page: ParsedPage) -> str:
    """Every quote's text and attribution, for "this never came through" checks."""
    return "\n".join(f"{q.text}\n{q.attribution}" for q in page.quotes)


def _by_start(page: ParsedPage, start: str):
    found = [q for q in page.quotes if q.text.startswith(start)]
    assert len(found) == 1, f"expected exactly one quote starting {start!r}, got {len(found)}"
    return found[0]


def _assert_absent(page: ParsedPage, fixture: str, marker: str) -> None:
    assert marker in _load(fixture), f"{marker!r} isn't in {fixture}; the check would be vacuous"
    assert marker not in _blob(page)


# --- Kinds ---


def test_kinds(wilde, friendship, film):
    assert wilde.kind == "author"
    assert friendship.kind == "theme"
    assert film.kind == "work"


def test_kind_is_logged_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.lounge.wikiquote"):
        parse_page(FILM_TITLE, _load("synthetic_film.html"))
    assert "work" in caplog.text


def test_work_kind_from_a_parenthetical_title_alone():
    html = '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div><ul><li>Fake line.</li></ul>'
    assert parse_page("Made Up Show (TV series)", html).kind == "work"
    assert parse_page("Made Up Book (novel)", html).kind == "author"


def test_theme_needs_three_single_letter_headings():
    def page(letters: str) -> str:
        return "".join(
            f'<div class="mw-heading mw-heading2"><h2 id="{c}">{c}</h2></div>' for c in letters
        )

    assert parse_page("Made Up Theme", page("AB")).kind == "author"
    assert parse_page("Made Up Theme", page("ABC")).kind == "theme"


# --- Author page ---


def test_author_page_counts(wilde):
    assert len(wilde.quotes) == 68
    assert wilde.dropped_long == 2
    assert wilde.dropped_unattributed == 0


def test_author_item_directly_under_quotes_gets_subject_plus_citation(wilde):
    q = _by_start(wilde, "Consistency is the last refuge of the unimaginative.")
    assert q.attribution == (
        'Oscar Wilde, "The Relation of Dress to Art", The Pall Mall Gazette (February 28, 1885)'
    )


def test_author_attribution_is_subject_only_with_no_headings_or_citation():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'
        "<ul><li>A made-up line with nothing to cite.</li></ul>"
    )
    (q,) = parse_page("Made Up Person", html).quotes
    assert q.attribution == "Made Up Person"


def test_author_subsection_gets_subject_work_heading_and_citation(wilde):
    q = _by_start(wilde, "Tread Lightly, she is near")
    assert q.attribution == 'Oscar Wilde, Poems (1881), "Requiescat", st. 1'


def test_author_part_headings_give_h3_then_h4(wilde):
    q = _by_start(wilde, "Anybody can make history. Only a great man can write it.")
    assert q.attribution == "Oscar Wilde, The Critic as Artist (1891), Part I"
    q = _by_start(wilde, "There is no sin except stupidity.")
    assert q.attribution == "Oscar Wilde, The Critic as Artist (1891), Part II"


def test_author_play_quote_gets_character_and_act_from_the_citation(wilde):
    q = _by_start(wilde, "Divorces are made in Heaven.")
    assert q.attribution == "Oscar Wilde, The Importance of Being Earnest (1895), Algernon, Act I"


def test_author_only_the_first_nested_line_is_the_citation(wilde):
    q = _by_start(wilde, "Jack: That, my dear Algy, is the whole truth pure and simple.")
    assert q.attribution == "Oscar Wilde, The Importance of Being Earnest (1895), Act I"
    assert "Often quoted as" not in _blob(wilde)


@pytest.mark.parametrize(
    "marker",
    [
        "I have the simplest tastes. I am always satisfied with the best.",  # Attributed
        "A pessimist is one who, when he has the choice of two evils, chooses both.",  # Disputed
        "Always forgive your enemies",  # Misattributed
        "From the beginning Wilde performed his life",  # Quotes about Wilde
        "Retrieved from University of California Libraries",  # Notes
        "Online Books by Oscar Wilde",  # External links
        "Full text online",  # a <dl> under a subsection
        "See also:",  # the lead's <dl>, before the first h2
        "was an Irish dramatist",  # the lead paragraph
    ],
)
def test_author_skipped_sections_and_ignored_parts_never_come_through(wilde, marker):
    _assert_absent(wilde, "oscar_wilde.html", marker)


def test_author_no_reference_markers_or_dead_link_notes(wilde):
    html = _load("oscar_wilde.html")
    assert "dead link" in html and "autonumber" in html  # the fixture does contain them
    blob = _blob(wilde)
    assert "dead link" not in blob
    assert "[1]" not in blob and "[2]" not in blob
    assert "cite_ref" not in blob


def test_author_line_breaks_are_kept(wilde):
    q = _by_start(wilde, "And down the long and silent street,")
    assert q.text == (
        "And down the long and silent street,\n"
        "The dawn, with silver-sandalled feet,\n"
        "Crept like a frightened girl."
    )


def test_author_every_quote_is_postable_and_linked(wilde):
    for q in wilde.quotes:
        assert len(q.text) <= MAX_WIKIQUOTE_CHARS
        assert fits(q)
        assert q.link == "https://en.wikiquote.org/wiki/Oscar_Wilde"


# --- Theme page ---


def test_theme_page_counts(friendship):
    assert len(friendship.quotes) == 27
    assert friendship.dropped_long == 1
    assert friendship.dropped_unattributed == 2


def test_theme_attribution_is_the_citation_alone(friendship):
    q = _by_start(friendship, "Friends are born, not made.")
    assert q.attribution == "Henry Adams, The Education of Henry Adams (1907), Ch. VII."
    q = _by_start(friendship, "my friend He was quite a dear")
    assert q.attribution == "June Lockhart"
    assert "Petticoat Junction" not in _blob(friendship)  # the second nested line


def test_theme_quotes_with_no_citation_are_dropped_and_counted(friendship):
    _assert_absent(
        friendship, "friendship.html", "A friend is he whose absence also proves the friendship."
    )
    _assert_absent(friendship, "friendship.html", "If you intend to cut yourself off from a friend")
    assert friendship.dropped_unattributed == 2
    assert all(q.attribution for q in friendship.quotes)


@pytest.mark.parametrize(
    "marker",
    [
        "The wicked have only accomplices",  # a figure caption
        "I would've died rather than betray my friends!",  # a figure caption
        "Quotes reported in",  # a <small> <dl>
        "redirects here, for the television series",  # a <small> <dl> in the lead
        "At Wikiversity, you can learn about",  # External links, inside a noprint box
        "Arranged alphabetically by author or source",  # navigation, before the first h2
    ],
)
def test_theme_captions_small_dls_and_boilerplate_never_come_through(friendship, marker):
    _assert_absent(friendship, "friendship.html", marker)


def test_theme_see_also_and_external_links_are_skipped(friendship):
    html = _load("friendship.html")
    assert 'id="See_also"' in html and 'id="External_links"' in html
    assert not [q for q in friendship.quotes if q.text == "Love"]


def test_theme_later_sections_are_parsed_like_the_letter_ones(friendship):
    # The Hoyt section is a heading with an italic title, not a letter.
    q = _by_start(friendship, "Great souls by instinct to each other turn,")
    assert q.attribution == "Joseph Addison, The Campaign, line 102."
    assert (
        q.text
        == "Great souls by instinct to each other turn,\nDemand alliance, and in friendship burn."
    )


# --- Work page (synthetic) ---


def test_film_page_counts(film):
    assert len(film.quotes) == 6
    assert film.dropped_long == 1
    assert film.dropped_unattributed == 0


def test_film_character_item_is_attributed_to_character_and_title(film):
    q = _by_start(film, "A heist is just a picnic with worse sandwiches.")
    assert q.attribution == "Prudence Fennimore, The Teapot Heist (film)"


def test_film_nested_note_is_not_the_attribution_and_stage_direction_is_kept(film):
    assert "Imaginary Institute" not in _blob(film)
    q = _by_start(film, "[to the kettle]")
    assert q.text == (
        "[to the kettle] You have been whistling since Tuesday, "
        "and I have decided to take it personally."
    )


def test_film_each_exchange_is_one_quote_one_speaker_per_line(film):
    dialogue = [q for q in film.quotes if q.attribution == FILM_TITLE]
    assert [q.text for q in dialogue] == [
        "Prudence: Did you bring the map?\n"
        "Barnaby: I brought a map. It is a map of somewhere else, but it is a very good map.\n"
        "Prudence: [sighing] Of course it is.",
        "Guard: Halt. Who goes there?\n"
        "Barnaby: Nobody. We are nobody.\n"
        "...\n"
        "Guard: Carry on, then.",
        "Prudence: Tell me the plan one more time.\n"
        "Barnaby: We walk in, we look like we belong, and we walk out.",
    ]


@pytest.mark.parametrize(
    "marker",
    [
        "MISATTRIBUTED LINE",
        "TAGLINE LINE",
        "FIGCAPTION PULLQUOTE",
        "EXTERNAL LINK",
        "Directed by Nobody Real",
    ],
)
def test_film_skipped_sections_never_come_through(film, marker):
    _assert_absent(film, "synthetic_film.html", marker)


def test_film_cast_section_is_skipped(film):
    assert "- Barnaby Quillfeather</li>" in _load("synthetic_film.html")
    assert "Nobody Else" not in _blob(film)


def test_film_over_400_line_is_dropped_and_counted(film):
    assert "I have rehearsed this speech in the shower" in _load("synthetic_film.html")
    assert "I have rehearsed this speech" not in _blob(film)
    assert film.dropped_long == 1


def test_film_at_everyone_quote_is_kept_verbatim_for_render_to_defuse(film):
    q = _by_start(film, "@everyone, the teapot is on the move")
    assert q.attribution == "Barnaby Quillfeather, The Teapot Heist (film)"


# --- Robustness and rules that no fixture pins ---


def test_stray_unmatched_end_tags_do_not_crash_or_lose_quotes():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div></span></li></ul>'
        "<ul><li>First made-up line.</b></li></ul></p>"
        "<ul><li>Second made-up line.</li></ul></div></div>"
    )
    page = parse_page("Made Up Person", html)
    assert [q.text for q in page.quotes] == ["First made-up line.", "Second made-up line."]


def test_empty_body_gives_zero_quotes():
    page = parse_page("Made Up Person", "")
    assert page.quotes == []
    assert (page.dropped_long, page.dropped_unattributed) == (0, 0)


def test_page_with_no_h2_gives_zero_quotes():
    html = "<p>Lead.</p><ul><li>Not under any h2.</li></ul><h3>Sub</h3><ul><li>Nor this.</li></ul>"
    assert parse_page("Made Up Person", html).quotes == []


def test_skip_heading_needs_a_word_boundary():
    # "Cast" is a skipped section; "Castiel" is a character. The plan's
    # "starts with" is read with a word boundary for exactly this reason.
    html = (
        '<div class="mw-heading mw-heading2"><h2>Castiel</h2></div>'
        "<ul><li>A made-up line.</li></ul>"
        '<div class="mw-heading mw-heading2"><h2>Cast of characters</h2></div>'
        "<ul><li>Not a quote.</li></ul>"
    )
    page = parse_page("Made Up Show (TV series)", html)
    assert [q.text for q in page.quotes] == ["A made-up line."]


def test_skipped_section_ends_at_a_heading_of_the_same_or_higher_level():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'
        '<div class="mw-heading mw-heading3"><h3>Disputed</h3></div><ul><li>Skipped line.</li></ul>'
        '<div class="mw-heading mw-heading3"><h3>Fine</h3></div><ul><li>Kept line.</li></ul>'
    )
    page = parse_page("Made Up Person", html)
    assert [(q.text, q.attribution) for q in page.quotes] == [
        ("Kept line.", "Made Up Person, Fine")
    ]


def test_citation_is_capped_at_150_characters_with_an_ellipsis():
    cite = "word " * 60
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'
        f"<ul><li>A made-up line.<ul><li>{cite}</li></ul></li></ul>"
    )
    (q,) = parse_page("Person", html).quotes
    citation = q.attribution.removeprefix("Person, ")
    assert len(citation) == 150
    assert citation.endswith("…")


def test_exactly_400_characters_is_kept_and_401_is_dropped():
    def page(n: int) -> str:
        return (
            f'<div class="mw-heading mw-heading2"><h2>Quotes</h2></div><ul><li>{"a" * n}</li></ul>'
        )

    assert len(parse_page("Person", page(MAX_WIKIQUOTE_CHARS)).quotes) == 1
    dropped = parse_page("Person", page(MAX_WIKIQUOTE_CHARS + 1))
    assert dropped.quotes == []
    assert dropped.dropped_long == 1


def test_a_quote_that_is_short_but_will_not_fit_a_message_is_dropped(monkeypatch):
    # 400 characters can't outgrow 2,000 units even after escaping, so this
    # forces `fits` to say no and checks the drop is counted, not silent.
    monkeypatch.setattr("newsbot.lounge.wikiquote.fits", lambda quote: False)
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div><ul><li>A made-up line.</li></ul>'
    )
    page = parse_page("Person", html)
    assert page.quotes == []
    assert page.dropped_long == 1


def test_ignored_subtrees_inside_a_quote_vanish():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'
        "<ul><li>Kept<sup>[<i>dead link</i>]</sup> text<style>.x{}</style>"
        '<span class="noprint">hidden</span> here.'
        '<a class="external autonumber" href="http://x.invalid">[3]</a></li></ul>'
    )
    (q,) = parse_page("Person", html).quotes
    assert q.text == "Kept text here."


def test_ordered_list_items_are_not_quotes():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'
        "<ol><li>Numbered, so not a quote.</li></ol>"
    )
    assert parse_page("Person", html).quotes == []


def test_hr_inside_a_dl_splits_the_exchange():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Dialogue</h2></div>'
        "<dl><dd><b>A</b>: One.</dd><hr><dd><b>B</b>: Two.</dd></dl>"
    )
    page = parse_page("Made Up Show (TV series)", html)
    assert [q.text for q in page.quotes] == ["A: One.", "B: Two."]


def test_dialogue_dl_on_an_author_page_is_ignored():
    html = (
        '<div class="mw-heading mw-heading2"><h2>Quotes</h2></div>'
        "<dl><dd>Quotes reported in a made-up book.</dd></dl>"
    )
    assert parse_page("Made Up Person", html).quotes == []


# --- Link ---


def test_page_url_plain_and_parenthesized_titles():
    assert page_url("Casablanca (film)") == "https://en.wikiquote.org/wiki/Casablanca_(film)"
    assert page_url("Oscar Wilde") == "https://en.wikiquote.org/wiki/Oscar_Wilde"


def test_page_url_percent_encodes_awkward_characters():
    assert page_url("Q&A") == "https://en.wikiquote.org/wiki/Q%26A"
    assert page_url("Who? What?") == "https://en.wikiquote.org/wiki/Who%3F_What%3F"
    assert page_url("Nicolò Machiavelli") == "https://en.wikiquote.org/wiki/Nicol%C3%B2_Machiavelli"
    assert (
        page_url("A>B <c> `d` e|f") == "https://en.wikiquote.org/wiki/A%3EB_%3Cc%3E_%60d%60_e%7Cf"
    )
