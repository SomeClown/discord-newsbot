"""Tests for newsbot.text: HTML/BBCode stripping, truncation, and the first-line fallback."""

from newsbot.text import clean_text, first_line


def test_clean_text_strips_nested_html():
    assert clean_text("<div><p>Hello <b>world</b></p></div>") == "Hello world"


def test_clean_text_strips_bbcode():
    assert clean_text("[b]Patch 1.2[/b] notes [url=https://x]here[/url]") == "Patch 1.2 notes here"


def test_clean_text_decodes_entities():
    assert clean_text("Tips &amp; Tricks &mdash; a guide") == "Tips & Tricks — a guide"


def test_clean_text_collapses_whitespace_and_newlines():
    assert clean_text("line one\n\n   line two\t\ttabbed") == "line one line two tabbed"


def test_clean_text_truncates_on_word_boundary_with_ellipsis():
    text = "one two three four five six seven eight nine ten"
    result = clean_text(text, limit=20)
    assert result.endswith("…")
    assert len(result) <= 21
    assert not result[:-1].endswith(" ")


def test_clean_text_hard_cuts_a_single_long_word():
    text = "a" * 50
    result = clean_text(text, limit=10)
    assert result == "a" * 10 + "…"


def test_clean_text_short_input_is_unchanged():
    assert clean_text("short") == "short"


def test_first_line_takes_first_nonblank_line():
    html = "<p></p><p>First real line</p><p>Second line</p>"
    assert first_line(html) == "First real line"


def test_first_line_strips_tags_and_truncates():
    html = "<p>" + ("word " * 40) + "</p>"
    result = first_line(html, limit=20)
    assert len(result) <= 21
    assert result.endswith("…")


def test_first_line_of_empty_text_is_empty_string():
    assert first_line("   \n  \n") == ""
