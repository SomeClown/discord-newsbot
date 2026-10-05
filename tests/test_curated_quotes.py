"""Tests for quotes/rage-quit-tavern.txt, Rage Quit Tavern's curated lounge list.

The file is data, not code, so these tests read it straight off disk (no
network, nothing fetched) and push it through the same loader and renderer
the lounge uses. If someone adds a quote that's too long, a duplicate, or
missing its attribution line, this is where the bot would have found out at
08:00 on a Tuesday instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.bot.format import discord_len, esc
from newsbot.lounge.quotes import (
    ATTRIBUTION_PREFIX,
    QUOTE_HEADER,
    WIKIQUOTE_LINK_LABEL,
    Quote,
    fits,
    quote_hash,
    render_quote_message,
)
from newsbot.lounge.sources import MAX_SOURCE_BYTES, _parse_fortune

QUOTES_FILE = Path(__file__).parent.parent / "quotes" / "rage-quit-tavern.txt"


@pytest.fixture(scope="module")
def raw() -> bytes:
    return QUOTES_FILE.read_bytes()


@pytest.fixture(scope="module")
def parsed(raw: bytes) -> tuple[list[Quote], int]:
    # Same decoding as sources._read_file_sync, then the real fortune parser.
    return _parse_fortune(raw.decode("utf-8-sig"))


@pytest.fixture(scope="module")
def quotes(parsed: tuple[list[Quote], int]) -> list[Quote]:
    return parsed[0]


def test_file_is_utf8_without_a_bom_and_under_the_source_cap(raw: bytes):
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("utf-8")
    assert len(raw) < MAX_SOURCE_BYTES


def test_file_parses_into_a_few_hundred_quotes_and_none_are_too_long(
    parsed: tuple[list[Quote], int],
):
    loaded, too_long = parsed
    assert too_long == 0
    # About 400 was the brief; sources that couldn't fill their quota left it
    # shorter (see quotes/rage-quit-tavern-review.md). A cliff either way is a mistake.
    assert 300 <= len(loaded) <= 450


def test_every_entry_fits_in_a_discord_message(quotes: list[Quote]):
    for q in quotes:
        assert fits(q), q.text[:60]
        assert discord_len(render_quote_message(q)) <= 2000


def test_no_duplicates_by_quote_hash(quotes: list[Quote]):
    hashes = [quote_hash(q.text) for q in quotes]
    assert len(hashes) == len(set(hashes))


def test_no_duplicate_quote_bodies_even_with_different_attributions(quotes: list[Quote]):
    # The hash covers the attribution line too (it's part of the entry), so a
    # line copied from two pages would hash differently. Compare bodies.
    bodies = [quote_hash("\n".join(q.text.split("\n")[:-1])) for q in quotes]
    assert len(bodies) == len(set(bodies))


def test_every_entry_ends_with_one_attribution_line(quotes: list[Quote]):
    for q in quotes:
        lines = q.text.split("\n")
        assert len(lines) >= 2, q.text
        last = lines[-1]
        assert last.startswith(ATTRIBUTION_PREFIX), q.text[:60]
        assert last[len(ATTRIBUTION_PREFIX) :].strip(), q.text[:60]
        # Only the last line is the attribution; the body can't have one hiding in it.
        assert not any(line.startswith(ATTRIBUTION_PREFIX) for line in lines[:-1]), q.text[:60]


def test_entries_carry_no_link_line_and_no_footnote_markers(quotes: list[Quote]):
    for q in quotes:
        assert WIKIQUOTE_LINK_LABEL not in q.text
        assert q.link is None
        assert "[1]" not in q.text


def test_rendered_message_looks_like_the_wikiquote_layout(quotes: list[Quote]):
    # Header, body, attribution: what a Wikiquote quote posts, minus the link line.
    for q in (quotes[0], quotes[len(quotes) // 2], quotes[-1]):
        message = render_quote_message(q)
        lines = message.split("\n")
        assert lines[0] == QUOTE_HEADER
        # esc() backslashes the tilde of a file entry's attribution line
        # ("\~ Name"), which Discord draws as a plain "~". The Wikiquote path
        # prefixes after escaping, so it posts a bare "~ ". Same look on screen.
        assert lines[-1] == esc(q.text.split("\n")[-1])
        assert lines[-1].lstrip("\\").startswith(ATTRIBUTION_PREFIX)
        assert message == QUOTE_HEADER + "\n" + esc(q.text)


def test_a_dialogue_entry_keeps_each_speakers_name_on_their_line(quotes: list[Quote]):
    grail = [q for q in quotes if "Monty Python and the Holy Grail" in q.text.split("\n")[-1]]
    assert grail, "expected at least one Holy Grail entry"
    ni = next(q for q in grail if "Knights who say Ni" in q.text)
    body = ni.text.split("\n")[:-1]
    assert body[0].startswith("Head Knight:")
    assert any(line.startswith("King Arthur:") for line in body)


def test_hitchhikers_entries_name_their_author(quotes: list[Quote]):
    adams = [
        q
        for q in quotes
        if q.text.split("\n")[-1].startswith(ATTRIBUTION_PREFIX + "Douglas Adams, ")
    ]
    assert len(adams) >= 50


def test_attributions_are_short_and_plain(quotes: list[Quote]):
    # Author or speaker, work, year: no chapters, page numbers, ISBNs, or asterisks
    # (esc() would post them as literal backslash-asterisks).
    for q in quotes:
        attribution = q.text.split("\n")[-1][len(ATTRIBUTION_PREFIX) :]
        assert len(attribution) <= 110, attribution
        assert "*" not in attribution, attribution
        for noise in ("ISBN", "Chapter ", "Ch. ", " p. ", "Letter to"):
            assert noise not in attribution, attribution
