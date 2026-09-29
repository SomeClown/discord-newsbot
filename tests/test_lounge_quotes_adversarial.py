"""Adversarial tests for `newsbot.lounge.quotes`, beyond `test_lounge_quotes.py`.

The angle here (test-engineer brief, 2026-09-29): a quote is scraped or typed
text that ends up in a public channel, so it's treated like a stranger with a
marker and a captive audience. Everything is synthetic; the IDs are made up,
the "evil" hosts are reserved `.example` names, and no real person is quoted.

Two questions get asked of every hostile string. Would Discord *ping* on it?
`AllowedMentions.none()` on the send is the real backstop for that, but the
text shouldn't even look live. And would it *reformat* the message (a heading,
a code fence swallowing the attribution)? Nothing at send time stops that,
so `esc()` is the only line of defense, and where it lets something through
the test says so out loud instead of pretending. Those tests pin today's
behavior so a change is a decision, not a surprise.
"""

from __future__ import annotations

import random
import re
import time
from urllib.parse import quote as urlquote

import pytest

from newsbot.bot.format import discord_len, esc
from newsbot.lounge.quotes import (
    ATTRIBUTION_PREFIX,
    QUOTE_HEADER,
    WIKIQUOTE_LINK_LABEL,
    Quote,
    QuotePick,
    choose_quote,
    fits,
    quote_hash,
    render_quote_message,
    split_fortune,
)
from newsbot.store.models import QuoteDeckState

EMPTY = QuoteDeckState(used=frozenset(), last_hash=None)
ZWSP = "​"
UID = "123456789012345678"  # 18 digits, synthetic
UID17 = "12345678901234567"
UID20 = "12345678901234567890"

# A fortune attribution line starts with a spaced double dash. That exact text
# in a string literal trips the dash tripwire test, so it's built at runtime.
ATTR = "-" * 2 + " Test Person"


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


def _overhead(quote: Quote) -> int:
    """UTF-16 units of everything in the message except the (empty) text."""
    return discord_len(render_quote_message(quote))


# What a *live* Discord token looks like in the rendered message.
LIVE_PATTERNS = {
    "user mention": re.compile(r"<@!?\d{17,20}>"),
    "role mention": re.compile(r"<@&\d{17,20}>"),
    "channel link": re.compile(r"<#\d{17,20}>"),
    "slash command": re.compile(r"</[^>\s]+:\d{17,20}>"),
    "everyone": re.compile(r"@everyone"),
    "here": re.compile(r"@here"),
    "url scheme": re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://"),
    "invite": re.compile(r"discord(?:app)?\.(?:gg|com/invite)/", re.IGNORECASE),
    "masked link": re.compile(r"(?<!\\)\[[^\]]*\]\("),
}


def _live(message: str) -> list[str]:
    return [name for name, pat in LIVE_PATTERNS.items() if pat.search(message)]


HOSTILE = [
    f"<@{UID}>",
    f"<@!{UID}>",
    f"<@&{UID}>",
    f"<#{UID}>",
    f"</cmd:{UID}>",
    f"</a b:{UID17}>",
    f"<@{UID20}>",
    f"<@{UID17}>",
    "@everyone",
    "@here",
    "@everyone@everyone@here",
    "@@everyone",
    f"<<@{UID}>>",
    "[x](https://evil.example)",
    "[x](<https://evil.example>)",
    "<https://evil.example>",
    "https://evil.example/path?q=1",
    "HTTPS://EVIL.EXAMPLE",
    "ftp://evil.example",
    "discord.gg/abcdef",
    "DISCORD.GG/abcdef",
    "discord.com/invite/abcdef",
    "discordapp.com/invite/abcdef",
    "text " + f"<@{UID}>" + " more @everyone https://evil.example discord.gg/x",
]


# --- rendering safety: pings and links ---------------------------------------


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_text_has_no_live_token_anywhere_in_message(hostile):
    assert _live(render_quote_message(Quote(hostile))) == []


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_attribution_has_no_live_token_anywhere_in_message(hostile):
    assert _live(render_quote_message(Quote("fine", attribution=hostile))) == []


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_text_and_attribution_together_with_a_real_link_line(hostile):
    link = "https://en.wikiquote.example/wiki/" + urlquote("Some Title")
    msg = render_quote_message(Quote(hostile, attribution=hostile, link=link))
    # Only the link line's own scheme is allowed to be live.
    body = msg.rsplit("\n", 1)[0]
    assert _live(body) == []
    assert msg.endswith(f"{WIKIQUOTE_LINK_LABEL} <{link}>")


def test_fullwidth_and_joined_variants_are_untouched_and_inert():
    # Discord only pings on the literal ASCII forms, so these are harmless even
    # though esc() leaves them alone. Pinned so nobody "fixes" them into mush.
    variants = [
        "＠everyone",
        "@‍everyone",
        "@​everyone",
        "@ everyone",
        f"＜＠{UID}＞",
        f"<@‍{UID}>",
    ]
    for v in variants:
        assert esc(v) == v or ZWSP in esc(v)
        assert _live(render_quote_message(Quote(v))) == []


def test_zero_width_defuse_does_not_double_up_on_already_defused_text():
    once = render_quote_message(Quote("@everyone"))
    twice = render_quote_message(Quote(esc("@everyone")))
    assert "@everyone" not in once
    assert "@everyone" not in twice


def test_bare_www_and_domains_pass_through_documented_gap():
    # esc() only defuses scheme://, and discord.gg style invites. A bare
    # "www.evil.example" reads as text to us; whether a client autolinks it is
    # Discord's call, and nothing here can stop it. AllowedMentions is irrelevant
    # to links, so this is a formatting spoof, not a ping.
    msg = render_quote_message(Quote("see www.evil.example or evil.example"))
    assert "www.evil.example" in msg


def test_timestamps_and_custom_emoji_pass_through_documented_gap():
    # <t:...> and <:name:id> render as a timestamp / an emoji. Neither pings.
    # esc() (shared with digests) doesn't touch them; a decision for the owner.
    text = f"<t:1700000000:R> <:name:{UID}> <a:name:{UID}>"
    assert text in render_quote_message(Quote(text))


# --- rendering safety: layout -------------------------------------------------

# A line starting with any of these would render as a heading, quote, list or fence.
_LAYOUT_STARTS = [
    "# heading",
    "## heading",
    "### heading",
    "-# subtext",
    "> quote",
    ">>> block quote",
    "- item",
    "* item",
    "```",
    "```py",
]


@pytest.mark.parametrize("start", _LAYOUT_STARTS)
def test_line_starts_that_format_are_escaped_on_every_line_of_text(start):
    text = f"first\n{start}\nlast"
    lines = render_quote_message(Quote(text)).split("\n")
    assert lines[1:] != [] and len(lines) == 4
    assert lines[2].startswith("\\"), lines[2]


@pytest.mark.parametrize("start", _LAYOUT_STARTS)
def test_line_starts_that_format_are_escaped_in_attribution(start):
    msg = render_quote_message(Quote("fine", attribution=f"first\n{start}"))
    last = msg.split("\n")[-1]
    assert last.startswith("\\"), last


def test_code_fence_cannot_swallow_attribution_and_link():
    link = "https://en.wikiquote.example/wiki/X"
    msg = render_quote_message(Quote("```\nopen fence", attribution="Test Person", link=link))
    # Every backtick is escaped, so no fence opens and the tail stays plain.
    assert re.search(r"(?<!\\)`", msg) is None
    assert msg.split("\n")[-2] == f"{ATTRIBUTION_PREFIX}Test Person"


@pytest.mark.parametrize("marker", ["||", "~~", "**", "__", "*", "_", "`"])
def test_inline_markdown_never_survives_unescaped(marker):
    text = f"a {marker}b{marker} c"
    body = render_quote_message(Quote(text)).split("\n")[1]
    assert re.search(r"(?<!\\)" + re.escape(marker[0]), body) is None


def test_unclosed_spoiler_cannot_span_into_attribution():
    msg = render_quote_message(Quote("open ||spoiler", attribution="Test Person"))
    assert re.search(r"(?<!\\)\|", msg) is None


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_trailing_backslashes_cannot_escape_the_newline(n):
    text_line = render_quote_message(Quote("x" + "\\" * n, attribution="Test Person")).split("\n")[
        1
    ]
    trailing = len(text_line) - len(text_line.rstrip("\\"))
    assert trailing % 2 == 0, "an odd trailing run would escape the newline"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_trailing_backslashes_in_attribution_cannot_escape_the_link_newline(n):
    link = "https://en.wikiquote.example/wiki/X"
    lines = render_quote_message(Quote("x", attribution="A" + "\\" * n, link=link)).split("\n")
    attr = lines[2]
    trailing = len(attr) - len(attr.rstrip("\\"))
    assert trailing % 2 == 0


def test_leading_backslash_in_attribution_cannot_eat_the_prefix_space():
    line = render_quote_message(Quote("x", attribution="\\ *y*")).split("\n")[2]
    assert line.startswith(ATTRIBUTION_PREFIX + "\\\\")


def test_numbered_list_marker_passes_through_documented_gap():
    # "1. x" renders as a list in Discord, and esc() doesn't escape it. It's
    # cosmetic (no ping, can't reach the attribution) so it's pinned, not failed.
    assert render_quote_message(Quote("1. one\n2. two")).split("\n")[1:3] == ["1. one", "2. two"]


def test_indented_heading_marker_passes_through_documented_gap():
    # Leading spaces or a tab hide the marker from esc()'s line-start rule.
    # Whether Discord then renders a heading is its call.
    assert render_quote_message(Quote("  # x")).split("\n")[1] == "  # x"


def test_bidi_and_zero_width_characters_survive_but_stay_on_their_own_line():
    text = "a‮b​c‍d⁦e"
    lines = render_quote_message(Quote(text, attribution="Test Person")).split("\n")
    # Untouched by esc() (a documented gap: nothing strips them) ...
    assert lines[1] == text
    # ... but bidi runs are per paragraph, and the attribution starts a new one.
    assert lines[2] == f"{ATTRIBUTION_PREFIX}Test Person"
    assert QUOTE_HEADER == lines[0]


def test_attribution_cannot_forge_a_live_link_line_when_no_link_given():
    forged = f"Test Person\n{WIKIQUOTE_LINK_LABEL} <https://evil.example/x>"
    msg = render_quote_message(Quote("fine", attribution=forged))
    assert _live(msg) == []
    assert "<https://" not in msg


def test_attribution_with_newline_can_fake_a_schemeless_link_line_documented():
    # A quote or attribution that contains a newline is free to *look* like the
    # link line. Without a scheme there's nothing clickable to defuse, so it's
    # just text. Multi-line attributions are what make this possible.
    forged = f"Test Person\n{WIKIQUOTE_LINK_LABEL} <evil.example>"
    msg = render_quote_message(Quote("fine", attribution=forged))
    assert msg.endswith(f"{WIKIQUOTE_LINK_LABEL} <evil.example>")


def test_text_can_forge_the_header_line_documented():
    # Nothing forbids a quote whose first line is the header text. Cosmetic only.
    msg = render_quote_message(Quote(QUOTE_HEADER))
    assert msg.split("\n")[1].startswith("\\*\\*")


def test_empty_and_whitespace_attribution_and_link_semantics():
    assert render_quote_message(Quote("t", attribution="")).count("\n") == 1
    assert render_quote_message(Quote("t", link="")).count("\n") == 1
    # Whitespace-only is truthy, so it does get a line. Documented, not fatal.
    assert render_quote_message(Quote("t", attribution=" ")).count("\n") == 2


# --- the link line contract ---------------------------------------------------

_HOSTILE_TITLES = [
    "Evil> [x](https://evil.example)",
    "a b\nc\r\nd\te",
    "> quote <b> </b>",
    "Title with ‮ bidi and ​ zwsp",
    "Ünïcödé 😀 title",
    "<https://evil.example>",
    "%3E already-encoded %20",
    "?query=1&x=y#frag",
    "  leading and trailing  ",
]


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_link_built_with_quote_from_hostile_title_keeps_line_intact(title):
    link = "https://en.wikiquote.example/wiki/" + urlquote(title)
    for bad in (">", " ", "\n", "\r", "\t", "<"):
        assert bad not in link[len("https://en.wikiquote.example/wiki/") :]
    msg = render_quote_message(Quote("fine", link=link))
    last = msg.split("\n")[-1]
    assert last == f"{WIKIQUOTE_LINK_LABEL} <{link}>"
    assert msg.count(">") == 1
    assert msg.count("\n") == 2


def test_link_line_only_when_link_is_set_and_wrapped_in_angle_brackets():
    assert WIKIQUOTE_LINK_LABEL not in render_quote_message(Quote("t", attribution="a"))
    assert render_quote_message(Quote("t", link="https://x.example/a")).endswith(
        f"{WIKIQUOTE_LINK_LABEL} <https://x.example/a>"
    )


def test_link_with_gt_or_space_is_emitted_raw_contract_violation_documented():
    # Contract: only urllib.parse.quote output. Break it and the line breaks:
    # the renderer trusts its caller. If this ever starts escaping, great; the
    # parser must still never hand over a raw link.
    msg = render_quote_message(Quote("t", link="https://x.example/a> [y](https://evil.example) <b"))
    assert "> [y](https://evil.example) <b>" in msg
    msg = render_quote_message(Quote("t", link="https://x.example/a b"))
    assert msg.endswith("<https://x.example/a b>")


# --- fits and the UTF-16 boundary ---------------------------------------------


def test_fits_exact_boundary_ascii():
    room = 2000 - _overhead(Quote(""))
    assert discord_len(render_quote_message(Quote("a" * room))) == 2000
    assert fits(Quote("a" * room))
    assert not fits(Quote("a" * (room + 1)))


def test_fits_boundary_with_attribution_and_link_counts_them():
    q = Quote("", attribution="Test Person", link="https://x.example/a")
    room = 2000 - _overhead(q)
    assert fits(Quote("a" * room, q.attribution, q.link))
    assert not fits(Quote("a" * (room + 1), q.attribution, q.link))


def test_astral_characters_count_two_units_each():
    room = 2000 - _overhead(Quote(""))
    n = room // 2 + 1
    text = "😀" * n
    msg = render_quote_message(Quote(text))
    assert len(msg) < 2000 <= discord_len(msg), "codepoints under, UTF-16 units over"
    assert not fits(Quote(text))
    exact = "😀" * (room // 2) + "a" * (room % 2)
    assert discord_len(render_quote_message(Quote(exact))) == 2000
    assert fits(Quote(exact))


def test_combining_marks_each_count_one_unit():
    room = 2000 - _overhead(Quote(""))
    text = "é" * (room // 2)  # 2 units per grapheme
    assert fits(Quote(text + "a" * (room % 2)))
    assert not fits(Quote(text + "a" * (room % 2) + "́"))


def test_quote_that_fits_raw_but_not_escaped():
    text = "*" * 1500
    assert discord_len(text) < 2000
    assert not fits(Quote(text))
    assert fits(Quote("*" * 900))


def test_escaping_expansion_exact_boundary():
    room = 2000 - _overhead(Quote(""))
    stars = room // 2
    text = "*" * stars + "a" * (room % 2)
    assert discord_len(render_quote_message(Quote(text))) == 2000
    assert fits(Quote(text))
    assert not fits(Quote(text + "a"))


def test_defuse_insertions_count_toward_the_limit():
    room = 2000 - _overhead(Quote(""))
    n = room // len("@everyone")
    text = "@everyone" * n
    msg = render_quote_message(Quote(text))
    assert discord_len(msg) == discord_len(QUOTE_HEADER) + 1 + n * 10
    assert fits(Quote(text)) == (discord_len(msg) <= 2000)


def test_fits_empty_text_is_fine():
    assert fits(Quote(""))


# --- split_fortune ------------------------------------------------------------


@pytest.mark.parametrize("ws", ["\t", " ", "　", " \t "])
def test_percent_line_padded_with_any_unicode_whitespace_is_a_separator(ws):
    assert split_fortune(f"one\n{ws}%{ws}\ntwo") == (["one", "two"], 0)


def test_percent_followed_by_text_after_a_tab_is_not_a_separator():
    assert split_fortune("one\n%\ttwo\nthree") == (["one\n%\ttwo\nthree"], 0)


def test_lone_percent_at_eof_without_newline():
    assert split_fortune("one\n%") == (["one"], 0)
    assert split_fortune("%") == ([], 0)


def test_cr_only_line_endings():
    assert split_fortune("one\r%\rtwo\r") == (["one", "two"], 0)


def test_form_feed_and_other_unicode_line_breaks_split_lines_documented():
    # str.splitlines() treats \x0c, \x1c-\x1e, \x85, U+2028/9 as line ends. So a
    # "%" fenced by form feeds is a separator, and U+2028 inside an entry
    # becomes a plain newline. Neither is harmful; both are surprising.
    assert split_fortune("a\x0c%\x0cb") == (["a", "b"], 0)
    assert split_fortune("a\x85%\x85b") == (["a", "b"], 0)
    assert split_fortune("a b") == (["a\nb"], 0)


def test_nul_bytes_are_kept_inside_an_entry():
    entries, dropped = split_fortune("a\x00b\n%\nc")
    assert entries == ["a\x00b", "c"]
    assert dropped == 0


def test_no_separator_means_one_entry_with_trailing_whitespace_stripped():
    assert split_fortune("  one\ntwo   \n\n\t \n") == (["one\ntwo"], 0)


def test_only_whitespace_and_empty_input():
    assert split_fortune("") == ([], 0)
    assert split_fortune("   \n\t\n　") == ([], 0)


def test_attribution_line_stays_in_entry_and_percent_inside_it_does_not_split():
    entries, _ = split_fortune(f"line\n{ATTR}\n%\nnext")
    assert entries == [f"line\n{ATTR}", "next"]


def test_inner_blank_lines_and_indentation_are_kept():
    assert split_fortune("a\n\n  b\n%\n") == (["a\n\n  b"], 0)


def test_too_long_counts_are_exact_at_the_boundary():
    room = 2000 - _overhead(Quote(""))
    good, bad = "a" * room, "a" * (room + 1)
    assert split_fortune(f"{good}\n%\n{bad}\n%\nok") == ([good, "ok"], 1)


def test_expansion_counts_as_too_long_for_split():
    kept, dropped = split_fortune("*" * 1500 + "\n%\nfine")
    assert kept == ["fine"]
    assert dropped == 1


def test_million_characters_one_entry_is_fast():
    start = time.perf_counter()
    kept, dropped = split_fortune("a" * 1_000_000)
    assert (kept, dropped) == ([], 1)
    assert time.perf_counter() - start < 5


def test_many_entries_are_fast():
    start = time.perf_counter()
    kept, dropped = split_fortune("a\n%\n" * 50_000)
    assert len(kept) == 50_000 and dropped == 0
    assert time.perf_counter() - start < 5


def test_million_characters_of_escapable_text_is_fast():
    start = time.perf_counter()
    assert split_fortune("*" * 1_000_000) == ([], 1)
    assert time.perf_counter() - start < 5


# --- quote_hash ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Ａ", "A"),  # fullwidth
        ("ﬁ", "fi"),  # ligature
        ("Straße", "STRASSE"),
        ("ẞ", "ss"),
        (" ", " "),
        ("a　b", "a b"),
        ("a\tb", "a b"),
        ("a\x0bb", "a b"),
        ("a\x1cb", "a b"),
        ("a\x85b", "a b"),
        ("a b", "a b"),
        ("a \n\r\t b", "a b"),
        ("é", "é"),
        ("Ω", "ω"),
    ],
)
def test_hash_intended_collisions(a, b):
    assert quote_hash(a) == quote_hash(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("x²", "x2"),  # NFKC: an exponent becomes a digit
        ("①", "1"),
        ("‼", "!!"),
        ("ǅ", "ǆ"),
    ],
)
def test_hash_nfkc_collisions_that_change_meaning_documented(a, b):
    # The owner accepted NFKC. These are the cases where it merges things a
    # human would call different; they only matter if two such quotes share a list.
    assert quote_hash(a) == quote_hash(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("I", "ı"),  # dotless i is its own letter
        ("İ", "i"),
        ("a​b", "ab"),  # zero-width space
        ("a‍b", "ab"),  # zero-width joiner
        ("a﻿b", "ab"),
        ("a­b", "ab"),  # soft hyphen
        ("a᠎b", "ab"),  # Mongolian vowel separator isn't \s in Python
        ("a\x00b", "ab"),
        ("e", "é"),
        ("a", "b"),
    ],
)
def test_hash_distinct_cases_documented(a, b):
    # Invisible characters make a distinct card: a retyped quote with a stray
    # zero-width space in it is a brand new quote. Accepted, not stripped.
    assert quote_hash(a) != quote_hash(b)


def test_hash_of_empty_and_whitespace_only_agree():
    assert quote_hash("") == quote_hash(" \t\n　")


def test_hash_is_stable_across_calls_and_pinned():
    assert quote_hash("Test Quote") == quote_hash("  test   QUOTE\n")
    assert quote_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_hash_survives_lone_surrogates_or_documents_the_crash():
    # A lone surrogate can't be UTF-8 encoded. The sources read files as
    # UTF-8 so it shouldn't arrive; if it ever does, that's a UnicodeEncodeError.
    with pytest.raises(UnicodeEncodeError):
        quote_hash("a\ud800b")


# --- choose_quote -------------------------------------------------------------


def _quotes(n: int) -> list[Quote]:
    return [Quote(f"synthetic quote number {i}") for i in range(n)]


def _advance(deck: QuoteDeckState, pick: QuotePick) -> QuoteDeckState:
    """Mimic `claim_quote`: a reshuffle clears the source's rows first, then records the pick."""
    used = frozenset() if pick.reshuffle else deck.used
    return QuoteDeckState(used=used | {pick.hash}, last_hash=pick.hash)


def _deal(quotes: list[Quote], draws: int, seed: int) -> list[QuotePick]:
    rng, deck, picks = _rng(seed), EMPTY, []
    for _ in range(draws):
        pick = choose_quote(quotes, deck, rng)
        assert pick is not None
        picks.append(pick)
        deck = _advance(deck, pick)
    return picks


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("size", [2, 3, 7])
def test_many_cycles_never_repeat_within_a_cycle_or_back_to_back(seed, size):
    picks = _deal(_quotes(size), size * 40, seed)
    cycle: list[str] = []
    for i, pick in enumerate(picks):
        if pick.reshuffle:
            assert i % size == 0 and i > 0, "reshuffle only when the deck is spent"
            cycle = []
        assert pick.hash not in cycle
        cycle.append(pick.hash)
        if i:
            assert pick.hash != picks[i - 1].hash
    assert [p.reshuffle for p in picks[:size]] == [False] * size


def test_first_draw_is_not_a_reshuffle_and_every_cycle_covers_every_quote():
    quotes = _quotes(6)
    picks = _deal(quotes, 18, 1)
    expected = {quote_hash(q.text) for q in quotes}
    for start in range(0, 18, 6):
        assert {p.hash for p in picks[start : start + 6]} == expected


def test_two_quote_source_alternates_across_reshuffles():
    picks = _deal(_quotes(2), 20, 3)
    hashes = [p.hash for p in picks]
    assert all(a != b for a, b in zip(hashes, hashes[1:], strict=False))
    assert [p.reshuffle for p in picks] == [i > 0 and i % 2 == 0 for i in range(20)]


def test_same_seed_same_sequence_and_different_seed_can_differ():
    quotes = _quotes(30)
    a = [p.hash for p in _deal(quotes, 60, 42)]
    b = [p.hash for p in _deal(quotes, 60, 42)]
    c = [p.hash for p in _deal(quotes, 60, 43)]
    assert a == b
    assert a != c


def test_input_order_does_not_change_the_result_for_a_given_seed():
    quotes = _quotes(20)
    a = choose_quote(quotes, EMPTY, _rng(9))
    b = choose_quote(list(reversed(quotes)), EMPTY, _rng(9))
    assert a is not None and b is not None
    assert a.hash == b.hash


def test_duplicates_never_count_as_two_cards():
    a, b = Quote("dupe one"), Quote("dupe two")
    quotes = [a, Quote("  DUPE   ONE "), Quote("ｄｕｐｅ one"), b, Quote("Dupe Two")]
    picks = _deal(quotes, 8, 5)
    assert [p.reshuffle for p in picks] == [False, False, True, False, True, False, True, False]
    assert {p.hash for p in picks[:2]} == {quote_hash("dupe one"), quote_hash("dupe two")}


def test_first_of_a_duplicate_group_is_the_one_returned():
    first = Quote("same words", attribution="Test Person")
    pick = choose_quote([first, Quote("SAME  WORDS", attribution="Other Person")], EMPTY, _rng(0))
    assert pick is not None
    assert pick.quote is first


def test_all_duplicates_is_a_one_quote_source():
    quotes = [Quote("x y"), Quote("X  Y"), Quote("x\ny")]
    deck = QuoteDeckState(used=frozenset({quote_hash("x y")}), last_hash=quote_hash("x y"))
    pick = choose_quote(quotes, deck, _rng(0))
    assert pick is not None
    assert pick.reshuffle is True
    assert pick.hash == quote_hash("x y")


def test_used_hashes_not_in_the_list_are_ignored():
    quotes = _quotes(3)
    stale = frozenset({"0" * 64, "f" * 64})
    pick = choose_quote(quotes, QuoteDeckState(used=stale, last_hash="0" * 64), _rng(0))
    assert pick is not None
    assert pick.reshuffle is False


def test_used_only_stale_plus_all_current_is_a_reshuffle_that_skips_last():
    quotes = _quotes(3)
    hashes = [quote_hash(q.text) for q in quotes]
    deck = QuoteDeckState(used=frozenset({*hashes, "0" * 64}), last_hash=hashes[1])
    for seed in range(20):
        pick = choose_quote(quotes, deck, _rng(seed))
        assert pick is not None and pick.reshuffle
        assert pick.hash != hashes[1]


def test_last_hash_not_in_list_reshuffle_offers_everything():
    quotes = _quotes(3)
    hashes = {quote_hash(q.text) for q in quotes}
    deck = QuoteDeckState(used=frozenset(hashes), last_hash="0" * 64)
    seen = {choose_quote(quotes, deck, _rng(s)).hash for s in range(60)}  # type: ignore[union-attr]
    assert seen == hashes


def test_last_hash_none_with_full_used_reshuffles_over_everything():
    quotes = _quotes(3)
    deck = QuoteDeckState(used=frozenset(quote_hash(q.text) for q in quotes), last_hash=None)
    pick = choose_quote(quotes, deck, _rng(0))
    assert pick is not None and pick.reshuffle


def test_empty_list_is_none_even_with_a_dirty_deck():
    deck = QuoteDeckState(used=frozenset({"0" * 64}), last_hash="0" * 64)
    assert choose_quote([], deck, _rng(0)) is None


def test_pick_is_never_a_used_hash_when_candidates_remain():
    quotes = _quotes(10)
    hashes = [quote_hash(q.text) for q in quotes]
    deck = QuoteDeckState(used=frozenset(hashes[:9]), last_hash=hashes[8])
    for seed in range(20):
        pick = choose_quote(quotes, deck, _rng(seed))
        assert pick is not None
        assert (pick.hash, pick.reshuffle) == (hashes[9], False)


def test_does_not_mutate_its_inputs():
    quotes = _quotes(4)
    snapshot = list(quotes)
    deck = QuoteDeckState(used=frozenset({quote_hash(quotes[0].text)}), last_hash=None)
    choose_quote(quotes, deck, _rng(0))
    assert quotes == snapshot
    assert deck.used == frozenset({quote_hash(quotes[0].text)})


def test_very_large_list_is_fast_and_valid():
    quotes = _quotes(20_000)
    start = time.perf_counter()
    picks = _deal(quotes[:20_000], 50, 0)
    assert time.perf_counter() - start < 10
    assert len({p.hash for p in picks}) == 50


def test_pick_carries_a_matching_hash():
    pick = choose_quote(_quotes(5), EMPTY, _rng(0))
    assert pick is not None
    assert pick.hash == quote_hash(pick.quote.text)
