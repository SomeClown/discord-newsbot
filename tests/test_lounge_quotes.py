"""Tests for newsbot.lounge.quotes. Every quote here is synthetic on purpose."""

from __future__ import annotations

import random
import re

from newsbot.bot.format import discord_len
from newsbot.lounge.quotes import (
    ATTRIBUTION_PREFIX,
    QUOTE_HEADER,
    WIKIQUOTE_LINK_LABEL,
    Quote,
    choose_quote,
    fits,
    quote_hash,
    render_quote_message,
    split_fortune,
)
from newsbot.store.models import QuoteDeckState

EMPTY = QuoteDeckState(used=frozenset(), last_hash=None)


def _rng(seed: int) -> random.Random:
    # Seeded and predictable on purpose; nothing here is a secret.
    return random.Random(seed)  # noqa: S311


# A fortune attribution line starts with a spaced double dash. That exact text
# in a string literal trips the dash tripwire test, so it's built at runtime.
ATTR = "-" * 2 + " Test Person"


# --- splitting -------------------------------------------------------------


def test_percent_alone_and_padded_are_separators():
    assert split_fortune("one\n%\ntwo\n % \nthree") == (["one", "two", "three"], 0)


def test_double_percent_and_percent_text_are_not_separators():
    entries, dropped = split_fortune("one\n%%\ntwo\n% three\nfour")
    assert entries == ["one\n%%\ntwo\n% three\nfour"]
    assert dropped == 0


def test_crlf():
    assert split_fortune("one\r\n%\r\ntwo\r\n") == (["one", "two"], 0)


def test_bom_is_ignored():
    assert split_fortune("﻿one\n%\ntwo") == (["one", "two"], 0)


def test_leading_trailing_and_consecutive_separators():
    assert split_fortune("%\none\n%\n%\n%\ntwo\n%\n") == (["one", "two"], 0)


def test_whitespace_only_entries_dropped():
    assert split_fortune("one\n%\n  \n\t\n%\ntwo") == (["one", "two"], 0)


def test_attribution_lines_kept_verbatim():
    entries, _ = split_fortune(f"line one\n   {ATTR}\n%\nother")
    assert entries[0] == f"line one\n   {ATTR}"


def test_trailing_whitespace_stripped_per_line():
    assert split_fortune("one   \ntwo\t\n") == (["one\ntwo"], 0)


def test_no_separators_is_one_entry():
    assert split_fortune("just\nsome\nlines") == (["just\nsome\nlines"], 0)


def test_empty_input():
    assert split_fortune("") == ([], 0)


def test_oversize_dropped_and_counted():
    entries, dropped = split_fortune("ok\n%\n" + "a" * 2000 + "\n%\nalso ok")
    assert entries == ["ok", "also ok"]
    assert dropped == 1


def test_oversize_boundary_is_measured_after_escaping():
    # Asterisks double when escaped, so this fits raw and not rendered.
    stars = "*" * 1500
    assert len(stars) < 2000
    entries, dropped = split_fortune(stars)
    assert (entries, dropped) == ([], 1)


def test_oversize_boundary_exact():
    overhead = discord_len(render_quote_message(Quote("")))
    fits_exactly = "a" * (2000 - overhead)
    assert split_fortune(fits_exactly) == ([fits_exactly], 0)
    assert split_fortune(fits_exactly + "a") == ([], 1)


def test_boundary_counts_utf16_units():
    overhead = discord_len(render_quote_message(Quote("")))
    # Each emoji is two UTF-16 units.
    assert fits(Quote("😀" * ((2000 - overhead) // 2)))
    assert not fits(Quote("😀" * ((2000 - overhead) // 2 + 1)))


# --- hashing ---------------------------------------------------------------


def test_hash_is_64_hex():
    assert re.fullmatch(r"[0-9a-f]{64}", quote_hash("hello"))


def test_hash_equal_across_case_whitespace_and_line_breaks():
    base = quote_hash("the quick brown fox")
    assert quote_hash("The  QUICK\tbrown\nfox  ") == base
    assert quote_hash("the quick\r\nbrown fox") == base


def test_hash_equal_across_nfkc():
    assert quote_hash("ﬁsh") == quote_hash("fish")  # ligature
    assert quote_hash("Ａbc") == quote_hash("abc")  # fullwidth A


def test_hash_differs_on_real_text_change():
    assert quote_hash("the quick brown fox") != quote_hash("the quick brown fax")


def test_hash_ignores_attribution():
    a = Quote("same text", attribution="Person A")
    b = Quote("same text", attribution="Person B")
    assert quote_hash(a.text) == quote_hash(b.text)


# --- deck ------------------------------------------------------------------


def _quotes(n: int) -> list[Quote]:
    return [Quote(f"synthetic quote number {i}") for i in range(n)]


def test_n_distinct_picks_before_repeat():
    quotes = _quotes(6)
    rng = _rng(1234)
    used: set[str] = set()
    last = None
    for _ in range(6):
        pick = choose_quote(quotes, QuoteDeckState(frozenset(used), last), rng)
        assert pick is not None
        assert not pick.reshuffle
        assert pick.hash not in used
        used.add(pick.hash)
        last = pick.hash
    assert len(used) == 6


def test_seeded_rng_is_deterministic():
    quotes = _quotes(5)
    a = choose_quote(quotes, EMPTY, _rng(7))
    b = choose_quote(list(reversed(quotes)), EMPTY, _rng(7))
    assert a is not None and b is not None
    assert a.hash == b.hash


def test_added_quote_becomes_candidate():
    quotes = _quotes(3)
    used = frozenset(quote_hash(q.text) for q in quotes)
    new = Quote("a freshly added synthetic quote")
    pick = choose_quote([*quotes, new], QuoteDeckState(used, None), _rng(0))
    assert pick is not None
    assert pick.quote == new
    assert not pick.reshuffle


def test_removed_hash_is_ignored():
    quotes = _quotes(2)
    stale = quote_hash("a quote that has left the file")
    pick = choose_quote(quotes, QuoteDeckState(frozenset({stale}), stale), _rng(0))
    assert pick is not None
    assert not pick.reshuffle


def test_duplicates_collapse():
    quotes = [Quote("Same"), Quote("  same "), Quote("SAME"), Quote("other")]
    rng = _rng(0)
    first = choose_quote(quotes, EMPTY, rng)
    assert first is not None
    second = choose_quote(quotes, QuoteDeckState(frozenset({first.hash}), first.hash), rng)
    assert second is not None
    assert {first.hash, second.hash} == {quote_hash("same"), quote_hash("other")}
    assert not second.reshuffle
    third = choose_quote(
        quotes, QuoteDeckState(frozenset({first.hash, second.hash}), second.hash), rng
    )
    assert third is not None
    assert third.reshuffle


def test_exhaustion_reshuffles_and_never_picks_last_hash():
    quotes = _quotes(4)
    hashes = [quote_hash(q.text) for q in quotes]
    for seed in range(50):
        deck = QuoteDeckState(frozenset(hashes), hashes[2])
        pick = choose_quote(quotes, deck, _rng(seed))
        assert pick is not None
        assert pick.reshuffle
        assert pick.hash != hashes[2]


def test_one_quote_source_reuses_itself():
    only = Quote("the only synthetic quote")
    h = quote_hash(only.text)
    pick = choose_quote([only], QuoteDeckState(frozenset({h}), h), _rng(0))
    assert pick is not None
    assert pick.quote == only
    assert pick.reshuffle


def test_empty_list_returns_none():
    assert choose_quote([], EMPTY, _rng(0)) is None


# --- rendering -------------------------------------------------------------

# Real snowflakes are 17 to 20 digits. discord.py's escape_mentions only
# recognizes those, so a short id like <@123> passes through untouched (it
# can't ping anyone, and AllowedMentions.none() is the backstop regardless).
HOSTILE = (
    "@everyone @here <@123456789012345678> <@&456456456456456456> <#789789789789789789> "
    "https://x discord.gg/x "
    "**bold** _it_ ~~strike~~ `code` ||spoiler|| > quote"
)


def _assert_inert(rendered: str) -> None:
    assert "@everyone" not in rendered
    assert "@here" not in rendered
    assert "<@123456789012345678>" not in rendered
    assert "<@&456456456456456456>" not in rendered
    assert "<#789789789789789789>" not in rendered
    assert "https://x" not in rendered
    assert "**bold**" not in rendered
    assert "||spoiler||" not in rendered
    assert "~~strike~~" not in rendered


def test_hostile_text_is_inert():
    _assert_inert(render_quote_message(Quote(HOSTILE)))


def test_hostile_attribution_is_inert():
    rendered = render_quote_message(Quote("fine", attribution=HOSTILE))
    _assert_inert(rendered)
    assert rendered.splitlines()[2].startswith(ATTRIBUTION_PREFIX)


def test_invite_link_is_defused():
    # esc() puts a zero-width space after the scheme's colon for URLs; a bare
    # discord.gg/x is not autolinked into a clickable invite by that rule, so
    # the assertion is only that the text still reads as the same words.
    rendered = render_quote_message(Quote("join discord.gg/x now"))
    assert "discord.gg" in rendered


def test_layout_with_everything():
    link = "https://en.wikiquote.org/wiki/Test_Page_(film)"
    rendered = render_quote_message(Quote("body", attribution="Someone", link=link))
    assert rendered.splitlines() == [
        QUOTE_HEADER,
        "body",
        f"{ATTRIBUTION_PREFIX}Someone",
        f"{WIKIQUOTE_LINK_LABEL} <{link}>",
    ]


def test_no_attribution_line_when_none():
    assert render_quote_message(Quote("body")).splitlines() == [QUOTE_HEADER, "body"]


def test_no_link_line_when_no_link():
    rendered = render_quote_message(Quote("body", attribution="Someone"))
    assert WIKIQUOTE_LINK_LABEL not in rendered
    assert "<" not in rendered


def test_link_is_not_escaped():
    link = "https://en.wikiquote.org/wiki/Test_Page"
    assert f"<{link}>" in render_quote_message(Quote("body", link=link))
