"""Adversarial round for the three small accepted items (list escape, labels, spend line).

The builder's own file, `test_small_accepted_items.py`, covers the brief's examples. This one
goes looking for the cracks: digit runs in other scripts, every kind of whitespace after the dot,
strings whose Unicode folding changes length, labels hidden behind invisible characters,
input shaped to make the label stripper slow, and the owner report's two time windows at the
awkward edges of a month in a few awkward time zones.

Some tests here fail on purpose. They are the findings: each one says in its name or docstring
whether it is a plain bug or a place where the brief is silent and a decision is needed.
"""

from __future__ import annotations

import asyncio
import random
import time
import unicodedata
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot.bot.client import NewsBot
from newsbot.bot.format import (
    _MONTH_NAMES,
    DigestOutcome,
    _ai_line,
    _headline_line,
    _one_line,
    _without_labels,
    discord_len,
    esc,
    escape_list_start,
    render_guild_digest,
    render_owner_report,
    render_story_page,
)
from newsbot.config import GameCfg, Secrets, Topic, load_config
from newsbot.industry import INDUSTRY_KEY
from newsbot.lounge.quotes import QUOTE_HEADER, Quote, fits, render_quote_message
from newsbot.pipeline.summarize import StoryDraft, estimate_spend_usd
from newsbot.store.db import connect, migrate
from newsbot.store.models import HeadlineItem, SourceHealthRow, StoryView

RUN_DATE = date(2026, 10, 25)
BL4 = GameCfg(key="borderlands4", name="Borderlands 4")
CHANNELS = {"borderlands4": 22, INDUSTRY_KEY: 33}
URL = "https://example.com/a"
ZWSP = "\u200b"
ZWJ = "\u200d"
FE0F = "\ufe0f"
EM_DASH = "\u2014"
EN_DASH = "\u2013"
BOLD_OFFICIAL = "".join(chr(0x1D400 + ord(c) - ord("A")) for c in "OFFICIAL")


def _story(headline="Real headline", summary="Body.", label="reported"):
    return StoryDraft(
        headline=headline,
        summary=summary,
        label=label,
        item_urls=[URL],
        update_of_story_id=None,
    )


def _digest_description(stories):
    rendered = render_guild_digest(
        RUN_DATE,
        [BL4],
        CHANNELS,
        stories_by_game={"borderlands4": stories},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    (message,) = rendered.messages
    return message.embed.description


def _summary_line(summary):
    return _digest_description([_story(summary=summary)]).split("\n")[1]


def _headline(title, trust="community"):
    now = datetime(2026, 10, 25, tzinfo=UTC)
    return HeadlineItem(
        url=URL,
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=now,
        collected_at=now,
        uncertain=False,
    )


def _shown_title(title, trust="community"):
    line = _headline_line(_headline(title, trust))
    assert line.startswith("\u2022 ") and line.endswith(f" {EM_DASH} <{URL}>")
    return line[2 : -len(f" {EM_DASH} <{URL}>")]


# --- 1. escape_list_start and the sites that use it ---


@pytest.mark.parametrize(
    "text",
    [
        "\u0661. x",  # Arabic-Indic one
        "\u0661\u0662\u0663. x",
        "\uff11. x",  # full-width one
        "\U0001d7cf. x",  # mathematical bold one
        "\u0e51. x",  # Thai one
        "1\u200b. x",  # zero-width space inside the run
        "\u200b1. x",  # zero-width space in front
        "1\u2024 x",  # one dot leader
        "1\uff0e x",  # full-width full stop
        "1\u3002 x",  # ideographic full stop
        "1) x",
        "1: x",
        ".1. x",
        "x1. y",
        "1,000. x",
        "1.5. x",
        "1.x",
        "1..",
        "1.\u200b x",  # zero-width space after the dot is neither whitespace nor the end
    ],
)
def test_text_that_is_not_ascii_digits_dot_whitespace_is_left_alone(text):
    assert escape_list_start(text) == text


@pytest.mark.parametrize(
    "space",
    [" ", "\t", "\u00a0", "\u2003", "\u3000", "\u2028", "\x0b", "\x0c", "\x85", "\x1c", "\u1680"],
)
def test_every_kind_of_whitespace_after_the_dot_counts(space):
    assert escape_list_start(f"12.{space}x") == f"12\\.{space}x"


def test_a_line_ending_in_a_carriage_return_still_counts_as_whitespace_after_the_dot():
    assert escape_list_start("1.\r\n2.\r\nx") == "1\\.\r\n2\\.\r\nx"


def test_a_lone_carriage_return_does_not_start_a_new_line_for_this_helper():
    # Discord breaks lines on "\n" only, and so does the helper. Pinned so a change is a choice.
    assert escape_list_start("a\r1. b") == "a\r1. b"


def test_applying_the_escape_twice_changes_nothing_more():
    once = escape_list_start("1. a\n22. b")
    assert escape_list_start(once) == once


def test_only_the_first_run_on_each_line_is_touched_and_nothing_else_changes():
    assert escape_list_start("1. 2. 3.\nfoo 4. bar\n5. 6.") == "1\\. 2. 3.\nfoo 4. bar\n5\\. 6."


def test_empty_blank_and_newline_only_text_comes_back_unchanged():
    for text in ["", "\n", "\n\n", " ", "."]:
        assert escape_list_start(text) == text


def test_a_hundred_thousand_digits_is_not_slow():
    text = "1" * 100_000 + ". x"
    start = time.perf_counter()
    assert escape_list_start(text) == "1" * 100_000 + "\\. x"
    assert time.perf_counter() - start < 1.0


def test_a_hundred_thousand_digits_with_no_dot_is_not_slow():
    text = ("1" * 100_000 + "\n") * 3
    start = time.perf_counter()
    assert escape_list_start(text) == text
    assert time.perf_counter() - start < 1.0


def test_the_helper_only_ever_inserts_a_backslash_before_a_dot_at_a_line_start():
    rng = random.Random(7)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = ["1", "23", ".", " ", "\n", "x", "\\", "*", "#", "-", "\u0661", "\t", "\r", "\u00a0"]
    for _ in range(3000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 14)))
        out = escape_list_start(text)
        lines_in, lines_out = text.split("\n"), out.split("\n")
        assert len(lines_in) == len(lines_out)
        for before, after in zip(lines_in, lines_out, strict=True):
            assert after == before or after.replace("\\.", ".", 1) == before


@pytest.mark.parametrize(
    ("summary", "shown"),
    [
        ("*1. x*", "\\*1. x\\*"),
        ("1. *x*", "1\\. \\*x\\*"),
        ("1.*x*", "1.\\*x\\*"),
        ("1\\. x", "1\\\\. x"),
        ("\\1. x", "\\\\1. x"),
        ("-1. x", "\\-1. x"),
        ("_1. x", "\\_1. x"),
        ("1. ", "1\\."),
    ],
)
def test_esc_never_puts_anything_in_front_of_digits_that_the_escape_would_have_to_see(
    summary, shown
):
    assert _summary_line(summary) == shown


@pytest.mark.parametrize(
    "summary",
    [
        "\u2705 OFFICIAL: 1. Valve",
        "UPDATE \U0001f7e2 OFFICIAL : 1. Valve",  # uncovered by the mid-line pass
        "\U0001f7e2 OFFICIAL 1. Valve",  # no separator at all
        "\u200b\u2705 OFFICIAL \u00b7 1. Valve",
        "\uff2f\uff26\uff26\uff29\uff23\uff29\uff21\uff2c: 1. Valve",
    ],
)
def test_a_summary_that_only_becomes_a_list_start_after_the_labels_come_off_is_escaped(summary):
    assert _summary_line(summary) == "1\\. Valve"


def test_a_mid_line_marker_between_two_numbers_does_not_make_a_list_start():
    # The marker comes out of the middle, so "1." is mid-line and stays as it is.
    assert _summary_line("Top 1. \U0001f7e2 OFFICIAL \u00b7 2. Valve") == "Top 1. 2. Valve"


def _kept_and_cut(description):
    blocks = description.split("\n\n")
    cut = 0
    if blocks[-1].startswith("+") and "more, use /news" in blocks[-1]:
        cut = int(blocks[-1].split()[0][1:])
        blocks = blocks[:-1]
    return len(blocks), cut


def test_the_description_cap_counts_the_added_backslashes_at_every_boundary():
    total = 15
    differed = False
    for length in range(120, 260):
        filler = "w" * length
        for summary, control in ((f"1. {filler}", f"a. {filler}"),):
            escaped = _digest_description([_story(f"H{n}", summary) for n in range(total)])
            plain = _digest_description([_story(f"H{n}", control) for n in range(total)])
            assert discord_len(escaped) <= 4096
            kept, cut = _kept_and_cut(escaped)
            assert kept + cut == total
            assert (kept == total) == ("more, use /news" not in escaped)
            plain_kept, _ = _kept_and_cut(plain)
            assert kept <= plain_kept
            differed = differed or kept < plain_kept
    assert differed, "the sweep never put the backslashes across the limit; widen it"


def test_a_digest_of_numbered_summaries_never_goes_over_the_limit_whatever_the_count():
    for count in (1, 2, 5, 9, 30, 100):
        stories = [_story(f"H{n}", "1. " + "word " * 75) for n in range(count)]
        assert discord_len(_digest_description(stories)) <= 4096


def _page_description(summaries):
    views = [
        StoryView(
            id=n + 1,
            topic_key="borderlands4",
            headline=f"H{n}",
            summary=summary,
            label="reported",
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
            urls=[URL],
            is_update_of=None,
        )
        for n, summary in enumerate(summaries)
    ]
    topics = {"borderlands4": Topic(key="borderlands4", name="Borderlands 4", channel_id=1)}
    return render_story_page(views, topics, "Recent", 1, 1).description


def test_a_story_page_cut_never_strands_a_backslash_in_front_of_the_ellipsis():
    # The page is hard-cut at 4096 units. A cut that lands between the backslash and the dot
    # of "1\." leaves a lone backslash in front of the ellipsis. Sweep the first block's length
    # so one of the cuts has to land there.
    stranded = []
    for pad in range(3700, 4100):
        text = _page_description(["w" * pad, "1. second"])
        assert discord_len(text) <= 4096
        trailing = len(text.removesuffix("\u2026")) - len(text.removesuffix("\u2026").rstrip("\\"))
        if text.endswith("\u2026") and trailing % 2 == 1:
            stranded.append(pad)
    assert stranded == []


def _quote_of_numbered_lines(count):
    return Quote("\n".join("1. a" for _ in range(count)))


def test_the_lounge_fits_check_counts_the_added_backslashes():
    tight = False
    for count in range(300, 420):
        quote = _quote_of_numbered_lines(count)
        rendered = render_quote_message(quote)
        without = "\n".join([QUOTE_HEADER, esc(quote.text)])
        assert fits(quote) == (discord_len(rendered) <= 2000)
        if discord_len(without) <= 2000 < discord_len(rendered):
            tight = True
            assert not fits(quote)
    assert tight, "the sweep never put the backslashes across the limit; widen it"


def test_a_lounge_quote_that_fits_renders_under_the_message_limit():
    for count in range(1, 420):
        quote = _quote_of_numbered_lines(count)
        if fits(quote):
            assert discord_len(render_quote_message(quote)) <= 2000


def test_lounge_quote_with_only_unicode_digits_renders_as_written():
    assert render_quote_message(Quote("\u0661. x")).split("\n")[1] == "\u0661. x"


# --- 2. look-alike labels ---

LOOKALIKES = [
    "\U0001f534",
    "\U0001f535",
    *(chr(cp) for cp in range(0x1F7E0, 0x1F7EC)),
    "\u26aa",
    "\u26ab",
    "\u2b1b",
    "\u2b1c",
    "\u2705",
    "\u2611",
    "\u2714",
    *(chr(cp) for cp in range(0x1F501, 0x1F505)),
]
WORDS = ["OFFICIAL", "REPORTED", "RUMOR", "UPDATE"]


@pytest.mark.parametrize("emoji", LOOKALIKES, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("selector", ["", FE0F], ids=["bare", "fe0f"])
def test_every_listed_lookalike_emoji_strips_with_each_word_and_with_or_without_a_separator(
    emoji, selector
):
    for word in WORDS:
        assert _ai_line(f"{emoji}{selector} {word} \u00b7 tail") == "tail"
        assert _ai_line(f"{emoji}{selector} {word} tail") == "tail"
        assert _ai_line(f"{emoji}{selector}{word} tail") == "tail"


@pytest.mark.parametrize(
    "emoji",
    [
        "\U0001f533",  # white square button
        "\U0001f536",  # large orange diamond
        "\U0001f505",  # low brightness symbol
        "\U0001f500",  # twisted arrows
        "\u2610",  # ballot box
        "\u2b1d",  # black very small square
        "\u26a9",
        "\u26ac",
        "\u2704",
        "\U0001f7ec",  # one past the colored squares
        "\U0001f7df",  # one before the colored circles
    ],
    ids=lambda c: f"U+{ord(c):04X}",
)
def test_emoji_just_outside_the_listed_ranges_are_not_lookalikes(emoji):
    text = f"{emoji} OFFICIAL tail"
    assert _ai_line(text) == text


@pytest.mark.parametrize("emoji", ["\U0001f7e2", "\U0001f7e1", "\U0001f534", "\U0001f501"])
@pytest.mark.parametrize("selector", ["", FE0F])
def test_the_bots_four_emoji_alone_strip_with_or_without_the_variation_selector(emoji, selector):
    assert _ai_line(f"{emoji}{selector} tail") == "tail"
    assert _ai_line(f"{emoji}{selector}tail") == "tail"


@pytest.mark.parametrize("sep", ["\u00b7", ":", "-", "|", EN_DASH, EM_DASH])
def test_each_listed_separator_strips_with_trailing_space_and_at_the_end_of_the_text(sep):
    assert _ai_line(f"OFFICIAL {sep} tail") == "tail"
    assert _ai_line(f"OFFICIAL{sep} tail") == "tail"
    assert _ai_line(f"\u2705 OFFICIAL {sep} tail") == "tail"
    # At the end of the text the whole line is labels, so the original is kept.
    assert _ai_line(f"Update{sep}") == f"Update{sep}"
    assert _ai_line(f"tail 1 {sep}") == f"tail 1 {sep}"


@pytest.mark.parametrize("sep", ["\u00b7", ":", "-", "|", EN_DASH, EM_DASH])
def test_a_separator_glued_to_the_next_word_is_ordinary_text(sep):
    for text in (f"OFFICIAL{sep}tail", f"Update {sep}tail", f"rumor{sep}5% off"):
        assert _ai_line(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Official patch notes",
        "OFFICIAL patch notes",
        "Update 1.2 adds a boss",
        "Update 1.2: adds a boss",
        "Rumor mill",
        "Reported speed",
        "Official-looking",
        "Updated: yesterday",
        "UPDATES - weekly",
        "Rumors: many",
        "Officially: no",
        "Officiall - no",
        "RUMOR_X - y",
        "Official\u2019s: statement",
    ],
)
def test_bare_words_and_longer_words_stay(text):
    assert _ai_line(text) == text


def test_mid_line_label_words_with_separators_stay():
    for text in (
        "Valve says OFFICIAL: no",
        "He said the official - and only - patch",
        "Patch UPDATE \u00b7 hotfix",
        "The \u2705 OFFICIAL patch",
        "Is it \U0001f7e2 official?",
    ):
        assert _ai_line(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a \U0001f7e2 OFFICIAL", "a"),
        ("a \U0001f7e2 OFFICIAL \u00b7", "a"),
        ("a \U0001f7e2 OFFICIAL \U0001f7e1 REPORTED b", "a b"),
        ("a \U0001f7e2 OFFICIAL\U0001f7e1 REPORTED b", "a b"),
        ("a \U0001f7e2 OFFICIAL \u00b7\U0001f7e1 REPORTED \u00b7 b", "a b"),
        ("a \U0001f534 RUMOR \U0001f501 UPDATE \U0001f7e2 OFFICIAL b", "a b"),
        ("a \U0001f501\ufe0f UPDATE \u00b7 b", "a b"),
        ("\U0001f7e2 OFFICIAL \u00b7 a \U0001f7e2 OFFICIAL \u00b7", "a"),
    ],
)
def test_exact_markers_mid_line_come_out_alone_adjacent_and_at_the_end(text, expected):
    assert _ai_line(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a \U0001f7e2 RUMOR b",
        "a \U0001f534 OFFICIAL b",
        "a \U0001f501 REPORTED b",
        "a \U0001f7e1 UPDATE b",
        "a \U0001f7e2 OFFICIALS b",
        "a \U0001f7e2 OFFICIAL_ b",
        "a \U0001f7e2 OFFICIAL2 b",
        "a \U0001f7e2 Official b",
        "a \U0001f7e2OFFICIAL b",
    ],
)
def test_pairs_the_bot_never_renders_stay_mid_line(text):
    assert _ai_line(text) == _one_line(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The cut is made in the original: everything after the label comes back as written.
        (
            "\u2705 OFFICIAL \u00b7 \u00df stra\u00dfe \ufb03 e\u0301 \u00f1",
            "\u00df stra\u00dfe \ufb03 e\u0301 \u00f1",
        ),
        ("\uff2f\uff26\uff26\uff29\uff23\uff29\uff21\uff2c: \ufb01x", "\ufb01x"),
        (
            "\U0001d40e\U0001d405\U0001d405\U0001d408\U0001d402\U0001d408\U0001d400\U0001d40b: x",
            "x",
        ),
        ("O\ufb03cial: \ufb03", "\ufb03"),
        ("\u2705 \uff35\uff30\uff24\uff21\uff34\uff25 tail", "tail"),
        ("\u212a\u2105 OFFICIAL: x", "\u212a\u2105 OFFICIAL: x"),
    ],
)
def test_nfkc_folding_that_changes_length_does_not_shift_the_cut(text, expected):
    assert _ai_line(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "OFFICIAL\ufb01: x",  # the word runs on into a ligature, so it is not the word
        "UPDATE\ufb03 - x",
        "\u2705 OFFICIAL\ufb01 x",
        "\u2705 RUMOR\u00df x",
    ],
)
def test_a_ligature_straddling_the_end_of_the_label_word_is_not_a_label(text):
    assert _ai_line(text) == text


def test_dotless_i_folds_to_a_plain_i_so_it_is_a_lookalike():
    # The brief says case-insensitive after NFKC; upper-casing makes this one match. Pinned as a
    # behaviour note (harmless: it strips more, never less).
    assert _ai_line("OFF\u0131CIAL: x") == "x"


@pytest.mark.parametrize(
    "text", ["\u0130OFFICIAL: x", "OFF\u0130CIAL: x", "\u2705 OFFICIAL\u017f x"]
)
def test_dotted_capital_i_and_long_s_do_not_create_a_label(text):
    assert _ai_line(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "\u2705 OFFICIAL\u0301 patch",
        "\u2705 OFFICIAL\u0301: patch",
        "\U0001f7e2 OFFICIAL\u0301 \u00b7 patch",
        "x \U0001f7e2 OFFICIAL\u0301 patch",
    ],
)
def test_a_combining_mark_on_the_last_letter_is_not_left_behind_on_its_own(text):
    """BUG: the label word ends inside a grapheme cluster ("OFFICIA" + L with an accent).

    The word boundary check is `(?!\\w)`, and a combining mark is not a word character, so the
    letter L plus its accent is cut in half and the accent is left standing alone at the start
    of the line (or in the middle of it). Either the text should stay as it is (the accented L
    is not an L), or the whole cluster should go.
    """
    out = _ai_line(text)
    assert not unicodedata.category(out.lstrip()[0]).startswith("M")
    assert " \u0301" not in out and not out.startswith("\u0301")


@pytest.mark.parametrize(
    "text",
    [
        "\u200b OFFICIAL: tail",
        "\u2705 \u200b OFFICIAL: tail",
        "\u2705\u200bOFFICIAL tail",
        "\U0001f7e2\u200d OFFICIAL \u00b7 tail",
        "OFFICIAL\u200b: tail",
    ],
)
def test_invisible_characters_in_the_middle_of_a_label_do_not_hide_it(text):
    """AMBIGUOUS BRIEF: invisibles are allowed only in front of the whole run.

    The brief lets invisibles precede the match ("after any leading invisibles, as now") but
    says nothing about invisibles between the emoji and the word, or a space after the
    invisible. Each of these reads as a label to a person. The code leaves them as they are.
    """
    assert _ai_line(text) == "tail"


@pytest.mark.parametrize(
    "text",
    [
        "\U0001f7e2\U0001f3fd OFFICIAL \u00b7 tail",  # emoji with a skin-tone swatch after it
        "\U0001f7e2\u200d\U0001f7e2 OFFICIAL \u00b7 tail",  # ZWJ sequence
    ],
)
def test_a_skin_tone_or_zwj_sequence_after_the_emoji_does_not_leave_a_fake_label(text):
    """AMBIGUOUS BRIEF: the rules are silent on modifiers and joiners after the emoji.

    The bot's own emoji comes off and the rest, a swatch or a joiner followed by
    "OFFICIAL . tail", stays and reads as a label.
    """
    assert "OFFICIAL" not in _ai_line(text)


def test_stripping_everything_falls_back_to_the_collapsed_original():
    for text in [
        "\u2705 OFFICIAL",
        "\U0001f7e2",
        "UPDATE \u00b7",
        "\U0001f7e2 OFFICIAL \u00b7 \U0001f534 RUMOR",
    ]:
        assert _ai_line(f" {text}\n\n") == text
    # A title that is only a label keeps the flattened title, minus every look-alike emoji.
    assert _shown_title("\u2705  OFFICIAL\n") == "OFFICIAL"


def test_invisible_characters_in_front_of_text_that_is_not_a_label_are_left_alone():
    for text in [ZWSP + "Valve", "\u034fValve", "\u180eOfficial patch", "\u202eUpdate 1.2"]:
        assert _ai_line(text) == text


def test_leading_invisibles_come_off_with_the_label_they_were_in_front_of():
    for lead in [ZWSP, "\u034f", "\u180e", "\u202e", "\u00ad", "\ufeff", ZWSP * 50]:
        assert (
            _ai_line(f"{lead}\u2705 OFFICIAL \u00b7 {lead}\U0001f7e2 OFFICIAL \u00b7 tail")
            == "tail"
        )


def test_a_label_run_that_mid_line_removal_uncovers_is_stripped_to_the_end():
    assert _ai_line("UPDATE \U0001f7e2 OFFICIAL : tail") == "tail"
    assert _ai_line("\u2705 OFFICIAL \u00b7 \U0001f534 RUMOR \u00b7 UPDATE: tail") == "tail"


def test_the_stripper_is_idempotent_over_a_pool_of_hostile_pieces():
    pool = [
        "\U0001f7e2", "\U0001f7e1", "\U0001f534", "\U0001f501", "\u2705", "\U0001f535", FE0F,
        " ", "  ", "\u00b7", ":", "-", "|", EN_DASH, EM_DASH, "OFFICIAL", "REPORTED", "RUMOR",
        "UPDATE", "official", "Update", "\uff2f\uff26\uff26\uff29\uff23\uff29\uff21\uff2c",
        "\ufb03", ZWSP, ZWJ, "\u034f", "\u180e", "\U0001f3fd", "x", "1.", "\u0131", "\u0130",
        "\u00df", "\u0301", "A", "\uff1a", "\ufe63", "\u00a0", "\n",
    ]  # fmt: skip
    rng = random.Random(20261025)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(30_000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 10)))
        once = _ai_line(text)
        assert once or not text.strip(), f"empty result for {text!r}"
        assert _ai_line(once) == once, f"not stable for {text!r}"


def test_text_with_no_label_in_it_comes_out_exactly_as_one_line_leaves_it():
    pool = [
        "\u2705", "\U0001f535", "OFFICIAL", "Update", "RUMOR", "\u00b7", ":", "-", "|",
        "\u00df", "\ufb03", "\u0130", "\u017f", "e\u0301", ZWSP, "1.", FE0F, "x", "\u0131",
    ]  # fmt: skip
    rng = random.Random(99)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(20_000):
        text = "Valve " + " ".join(rng.choice(pool) for _ in range(rng.randint(0, 8)))
        assert _ai_line(text) == _one_line(text), repr(text)


def test_a_title_stripped_before_the_cut_is_never_cut_into_a_label():
    for k in range(280, 312):
        shown = _shown_title("x" * k + " \U0001f7e2 OFFICIAL \u00b7 " + "y" * 60)
        assert "OFFICIAL" not in shown and "\U0001f7e2" not in shown
        assert len(shown) <= 300
    for k in range(280, 312):
        shown = _shown_title("\u2705 OFFICIAL \u00b7 " * 25 + "x" * k)
        assert shown == "x" * min(k, 300) if k <= 300 else shown == "x" * 299 + "\u2026"


def test_a_cut_title_is_not_a_label_either():
    # A title of 299 characters and then a look-alike: the cut is made after stripping, so
    # whatever is left at the end is plain text.
    shown = _shown_title("x" * 299 + "\u2705 OFFICIAL: z")
    assert shown.endswith("\u2026") and "OFFICIAL" not in shown


def test_a_headline_list_cannot_show_a_community_post_as_official_in_any_listed_disguise():
    disguises = [
        "\u2705 OFFICIAL \u00b7 free codes",
        "\U0001f7e2 OFFICIAL \u00b7 free codes",
        "\U0001f7e2\ufe0f OFFICIAL \u00b7 free codes",
        BOLD_OFFICIAL + ": free codes",
        "\u200b\u2611 OFFICIAL - free codes",
        "free \U0001f7e2 OFFICIAL \u00b7 codes",
        "UPDATE: free codes",
    ]
    for title in disguises:
        line = _headline_line(_headline(title))
        assert "OFFICIAL" not in line and "UPDATE" not in line, title
        assert "free" in line and "codes" in line


# Speed. The brief asks for termination and speed on adversarial input.


def _timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


@pytest.mark.parametrize(
    "unit",
    [
        "\u2705 OFFICIAL \u00b7 ",
        "UPDATE - ",
        "\U0001f7e2 ",
        "OFFICIAL: ",
        "\u2705 OFFICIAL " + EM_DASH + " \u200b",
        "UPDATE \U0001f7e2 OFFICIAL : ",
    ],
)
def test_a_hundred_thousand_characters_of_alternating_labels_and_separators_is_fast(unit):
    text = unit * (100_000 // len(unit)) + "tail"
    out, took = _timed(_ai_line, text)
    assert out.endswith("tail")
    assert took < 2.0


def test_a_hundred_thousand_plain_characters_are_fast():
    _, took = _timed(_ai_line, "word " * 20_000)
    assert took < 1.0


def test_a_hundred_thousand_spaces_after_a_label_word_are_fast():
    _, took = _timed(_without_labels, "OFFICIAL" + " " * 100_000 + "x")
    assert took < 2.0


def test_nested_exact_markers_do_not_make_the_stripper_quadratic():
    """BUG: removing an inner marker glues a new one together, and every round re-folds it all.

    "x " + d x "<green> " + d x "OFFICIAL " collapses one marker per round (the removal leaves
    spaces, the split collapses them, and the next marker is whole). Each round folds the entire
    string again, so cost grows with the square of the length. An RSS title has no length cap
    before `_headline_line` runs this, so a feed can stall the digest render for minutes
    (about 6 seconds at 30,000 characters, 16 seconds at 44,000, and a minute and a half at
    100,000 on the machine this was written on).
    """
    depth = 1500
    title = "x " + "\U0001f7e2 " * depth + "OFFICIAL " * depth + "tail"
    out, took = _timed(_headline_line, _headline(title))
    assert "OFFICIAL" not in out
    assert took < 1.0, f"{took:.1f} s for {len(title)} characters"


# --- 3. the spend line ---


def _report(day=0.04, month=0.62, name="October", outcomes=(), failing=()):
    return render_owner_report(
        list(outcomes),
        list(failing),
        spend_24h_usd=day,
        spend_month_usd=month,
        month_name=name,
    )


@pytest.mark.parametrize(
    ("usd", "day_text", "month_text"),
    [
        (0, "$0.00", "$0.00"),
        (0.0, "$0.00", "$0.00"),
        (0.004, "under $0.01", "under $0.01"),
        (0.005, "under $0.01", "under $0.01"),
        (0.0099999, "under $0.01", "under $0.01"),
        (0.01, "about $0.01", "$0.01"),
        (0.0100001, "about $0.01", "$0.01"),
        (0.04, "about $0.04", "$0.04"),
        (0.62, "about $0.62", "$0.62"),
        (1234.5, "about $1234.50", "$1234.50"),
    ],
)
def test_the_amount_strings_for_the_values_in_the_brief(usd, day_text, month_text):
    line = _report(day=usd, month=usd).splitlines()[1]
    assert line == (
        f"Claude spend: {day_text} in the last 24 hours, {month_text} so far in October (estimate)"
    )


def test_the_day_and_month_amounts_are_formatted_independently():
    line = _report(day=0.003, month=1234.5).splitlines()[1]
    assert line == (
        "Claude spend: under $0.01 in the last 24 hours, $1234.50 so far in October (estimate)"
    )
    line = _report(day=5.0, month=0.0).splitlines()[1]
    assert (
        line == "Claude spend: about $5.00 in the last 24 hours, $0.00 so far in October (estimate)"
    )


def test_the_month_never_gets_its_own_about():
    for usd in (0.0, 0.004, 0.01, 7.0):
        line = _report(day=usd, month=usd).splitlines()[1]
        assert line.count("about") == (1 if usd >= 0.01 else 0)
        assert "about $" not in line.split("hours, ")[1]


def test_the_month_names_are_the_twelve_english_names_in_order():
    assert _MONTH_NAMES == (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )


def test_the_cap_keeps_the_first_two_lines_when_every_failing_source_is_as_long_as_it_can_be():
    failing = [
        SourceHealthRow(
            f"*_{i}_*" + "\U0001f600" * 80,
            None,
            datetime(2026, 9, 30, tzinfo=UTC),
            "*" * 300 + " https://user:pw@example.com/path?token=abc",
            10**12,
        )
        for i in range(1000)
    ]
    report = _report(
        outcomes=[DigestOutcome(False, "*" * 60 + str(n)) for n in range(500)], failing=failing
    )
    lines = report.splitlines()
    assert discord_len(report) <= 2000
    assert lines[0].startswith("newsbot daily: digests posted to 0 of 500 servers; 500 failed (")
    assert (
        lines[1]
        == "Claude spend: about $0.04 in the last 24 hours, $0.62 so far in October (estimate)"
    )
    assert lines[2].startswith("1000 failing sources:")
    assert report.endswith("\u2026")


def test_the_cap_with_a_giant_month_figure_still_keeps_both_lines():
    report = _report(
        day=1e15,
        month=1e300,
        failing=[SourceHealthRow("n" * 500, None, datetime(2026, 9, 30, tzinfo=UTC), "e" * 500, 5)]
        * 50,
    )
    assert report.splitlines()[1].startswith("Claude spend: about $")
    assert discord_len(report) <= 2000


# The bot over a scratch database, never started: only `_owner_report_sync` runs.

CONFIG = """\
home_guild_id: 100000000000000001
catalog:
  - key: palworld
    name: Palworld
"""
SECRETS = Secrets(
    discord_token=None,
    anthropic_api_key="k",
    brave_api_key=None,
    bluesky_handle=None,
    bluesky_app_password=None,
)


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _insert(conn, created_at, input_tokens, output_tokens, game_key="palworld", status="ok"):
    stamp = created_at.isoformat()
    conn.execute(
        "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
        "input_tokens, output_tokens, created_at, items_after, items_upto) "
        "VALUES (?, '2026-10-01', ?, ?, ?, ?, ?, ?, 0, 0)",
        (game_key, status, stamp, stamp, input_tokens, output_tokens, stamp),
    )


async def _report_at(tmp_path, db_path, now, timezone=None):
    path = tmp_path / "config.yaml"
    extra = f"owner_report:\n  timezone: {timezone}\n" if timezone else ""
    path.write_text(CONFIG + extra)
    bot = NewsBot(load_config(path), SECRETS, db_path)
    try:
        return await asyncio.to_thread(bot._owner_report_sync, now, "2026-11-01")
    finally:
        await bot.close()


def _dollars(rows):
    return estimate_spend_usd(sum(r[1] for r in rows), sum(r[2] for r in rows))


def _amount(usd, *, about):
    if usd <= 0:
        return "$0.00"
    if usd < 0.01:
        return "under $0.01"
    return f"{'about ' if about else ''}${usd:.2f}"


def _expected_line(rows, now, zone):
    """The spend line worked out the slow, obvious way, from the rows and the clock."""
    local = now.astimezone(ZoneInfo(zone))
    start = datetime(local.year, local.month, 1, tzinfo=ZoneInfo(zone)).astimezone(UTC)
    day = [r for r in rows if r[0] >= now - timedelta(hours=24)]
    month = [r for r in rows if r[0] >= start]
    return (
        f"Claude spend: {_amount(_dollars(day), about=True)} in the last 24 hours, "
        f"{_amount(_dollars(month), about=False)} so far in "
        f"{_MONTH_NAMES[local.month - 1]} (estimate)"
    )


async def _check_window(tmp_path, db_path, now, zone, extra_rows=()):
    """Insert rows just inside and just outside both windows, run the sync, return both lines."""
    local = now.astimezone(ZoneInfo(zone))
    start = datetime(local.year, local.month, 1, tzinfo=ZoneInfo(zone)).astimezone(UTC)
    one = timedelta(seconds=1)
    rows = [
        (start - one, 3_000_000, 0),  # last second of last month
        (start, 0, 1_000_000),  # first second of this month
        (start + one, 20_000_000, 0),
        (now - timedelta(hours=24) - one, 0, 4_000_000),  # just outside the day
        (now - timedelta(hours=24), 50_000_000, 0),  # exactly 24 hours ago: inside
        (now, 0, 2_000_000),
        (now - timedelta(days=45), 90_000_000, 0),
        *extra_rows,
    ]
    with closing(connect(db_path)) as conn, conn:
        for created, i, o in rows:
            _insert(conn, created, i, o)
    report = await _report_at(tmp_path, db_path, now, zone)
    return report.splitlines()[1], _expected_line(rows, now, zone)


WINDOW_CASES = [
    # Exactly local midnight on the 1st, and one second before it.
    ("America/Los_Angeles", datetime(2026, 11, 1, 7, 0, 0, tzinfo=UTC)),  # DST ends this day
    ("America/Los_Angeles", datetime(2026, 11, 1, 6, 59, 59, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2026, 12, 1, 8, 0, 0, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2026, 12, 1, 7, 59, 59, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2026, 3, 8, 11, 0, 0, tzinfo=UTC)),  # spring forward day
    ("America/Los_Angeles", datetime(2026, 3, 31, 12, 0, 0, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2026, 4, 1, 6, 59, 59, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2026, 4, 1, 7, 0, 0, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2027, 1, 1, 7, 59, 59, tzinfo=UTC)),  # year turns
    ("America/Los_Angeles", datetime(2027, 1, 1, 8, 0, 0, tzinfo=UTC)),
    ("America/Los_Angeles", datetime(2026, 11, 1, 4, 0, 0, tzinfo=UTC)),  # brief's own example
    # Zones ahead of UTC.
    ("Asia/Tokyo", datetime(2026, 10, 31, 15, 0, 0, tzinfo=UTC)),
    ("Asia/Tokyo", datetime(2026, 10, 31, 14, 59, 59, tzinfo=UTC)),
    ("Asia/Kolkata", datetime(2026, 10, 31, 18, 30, 0, tzinfo=UTC)),
    ("Asia/Kolkata", datetime(2026, 10, 31, 18, 29, 59, tzinfo=UTC)),
    ("Pacific/Kiritimati", datetime(2026, 12, 31, 10, 0, 0, tzinfo=UTC)),
    ("Pacific/Kiritimati", datetime(2026, 12, 31, 9, 59, 59, tzinfo=UTC)),
    ("Australia/Sydney", datetime(2026, 10, 31, 13, 0, 0, tzinfo=UTC)),  # AEDT, +11
    ("Australia/Sydney", datetime(2026, 10, 15, 3, 0, 0, tzinfo=UTC)),  # after DST began
    ("Australia/Lord_Howe", datetime(2026, 10, 31, 13, 30, 0, tzinfo=UTC)),
    ("Pacific/Pago_Pago", datetime(2026, 11, 1, 11, 0, 0, tzinfo=UTC)),
    ("Europe/London", datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)),  # BST ends this day
    ("Europe/London", datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)),
    ("Europe/London", datetime(2026, 4, 1, 23, 0, 0, tzinfo=UTC)),
    ("UTC", datetime(2026, 10, 25, 4, 0, 0, tzinfo=UTC)),
    ("UTC", datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)),
    # Midnight on the 1st does not exist in Paraguay on 2017-10-01 (clocks jumped 00:00 to 01:00).
    ("America/Asuncion", datetime(2017, 10, 15, 12, 0, 0, tzinfo=UTC)),
]


@pytest.mark.parametrize(
    ("zone", "now"),
    WINDOW_CASES,
    ids=[f"{zone}@{now:%Y-%m-%dT%H:%M:%S}" for zone, now in WINDOW_CASES],
)
async def test_both_windows_include_rows_at_their_start_and_exclude_one_second_before(
    tmp_path, db_path, zone, now
):
    actual, expected = await _check_window(tmp_path, db_path, now, zone)
    assert actual == expected


async def test_the_paraguayan_month_that_starts_in_a_skipped_hour_starts_at_0400_utc(
    tmp_path, db_path
):
    # Pinned by hand, not by the oracle: clocks went from 00:00 (UTC-4) to 01:00 (UTC-3) at
    # 04:00 UTC on 2017-10-01, so the month's first instant is 04:00 UTC.
    now = datetime(2017, 10, 15, 12, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, datetime(2017, 10, 1, 3, 59, 59, tzinfo=UTC), 0, 10_000_000)
        _insert(conn, datetime(2017, 10, 1, 4, 0, 0, tzinfo=UTC), 0, 2_000_000)
    report = await _report_at(tmp_path, db_path, now, "America/Asuncion")
    assert report.splitlines()[1] == (
        "Claude spend: $0.00 in the last 24 hours, $1.00 so far in October (estimate)"
    )


@pytest.mark.parametrize("hour", [4, 5, 6], ids=["first pass", "repeated hour", "after it"])
async def test_the_month_start_is_the_same_instant_in_a_zone_whose_midnight_happens_twice(
    tmp_path, db_path, hour
):
    """BUG (very low severity): `replace(hour=0)` keeps the clock reading's `fold`.

    Havana ends daylight time at 01:00 on 2026-11-01, so local midnight on the 1st happens twice
    (04:00 UTC, then 05:00 UTC). The first one is the month's start. When `now` falls in the
    repeated hour (05:00 to 06:00 UTC) the code keeps fold=1 and computes 05:00 UTC, so a row
    written between 04:00 and 05:00 UTC is dropped from the month total. The default zone and
    the default 21:00 report time can never get here; a Cuban owner reporting at 00:30 could.
    """
    now = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, datetime(2026, 11, 1, 4, 10, tzinfo=UTC), 0, 2_000_000)
    report = await _report_at(tmp_path, db_path, now, "America/Havana")
    assert "$1.00 so far in November" in report.splitlines()[1]


async def test_industry_rows_and_fallback_rows_count_in_both_windows(tmp_path, db_path):
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, now - timedelta(hours=2), 0, 1_000_000, game_key=INDUSTRY_KEY)
        _insert(conn, now - timedelta(hours=3), 0, 1_000_000, game_key="palworld")
        _insert(
            conn, now - timedelta(hours=4), 2_000_000, 0, game_key=INDUSTRY_KEY, status="fallback"
        )
        _insert(conn, now - timedelta(days=5), 0, 1_000_000, game_key=INDUSTRY_KEY)
    report = await _report_at(tmp_path, db_path, now, "America/Los_Angeles")
    assert report.splitlines()[1] == (
        "Claude spend: about $1.20 in the last 24 hours, $1.70 so far in October (estimate)"
    )


async def test_a_row_in_the_window_counts_once_in_each_not_twice_in_one(tmp_path, db_path):
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, now - timedelta(hours=1), 0, 1_000_000)
    report = await _report_at(tmp_path, db_path, now, "America/Los_Angeles")
    assert report.splitlines()[1] == (
        "Claude spend: about $0.50 in the last 24 hours, $0.50 so far in October (estimate)"
    )


async def test_the_day_can_be_bigger_than_the_month_figure_only_across_a_month_boundary(
    tmp_path, db_path
):
    # 21:00 Pacific on the 1st: yesterday's rows are in the day window and not in the month.
    now = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, datetime(2026, 11, 1, 6, 0, tzinfo=UTC), 0, 10_000_000)  # Oct 31, 23:00 PDT
        _insert(conn, datetime(2026, 11, 1, 20, 0, tzinfo=UTC), 0, 2_000_000)
    report = await _report_at(tmp_path, db_path, now, "America/Los_Angeles")
    assert report.splitlines()[1] == (
        "Claude spend: about $6.00 in the last 24 hours, $1.00 so far in November (estimate)"
    )


@pytest.mark.parametrize("month", range(1, 13))
async def test_every_month_reports_its_own_english_name(tmp_path, db_path, month):
    now = datetime(2026, month, 15, 12, tzinfo=UTC)
    report = await _report_at(tmp_path, db_path, now, "UTC")
    assert f"so far in {_MONTH_NAMES[month - 1]} (estimate)" in report.splitlines()[1]


async def test_a_database_with_a_row_of_nothing_says_zero_twice(tmp_path, db_path):
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, now - timedelta(hours=1), 0, 0)
    report = await _report_at(tmp_path, db_path, now, "America/Los_Angeles")
    assert report.splitlines()[1] == (
        "Claude spend: $0.00 in the last 24 hours, $0.00 so far in October (estimate)"
    )


async def test_one_token_is_under_a_cent_not_zero(tmp_path, db_path):
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _insert(conn, now - timedelta(hours=1), 1, 0)
    report = await _report_at(tmp_path, db_path, now, "America/Los_Angeles")
    assert report.splitlines()[1] == (
        "Claude spend: under $0.01 in the last 24 hours, under $0.01 so far in October (estimate)"
    )


# --- round 2: the rewritten stripper -------------------------------------------------------

BOT_EMOJI = {
    "OFFICIAL": "\U0001f7e2",
    "REPORTED": "\U0001f7e1",
    "RUMOR": "\U0001f534",
    "UPDATE": "\U0001f501",
}
RIDERS = ["\ufe0f", "\U0001f3fb", "\U0001f3fd", "\U0001f3ff", "\u0301", "\u20e3", "\u0308"]


BLANK_GLYPHS = "\u115f\u1160\u2800\u3164\uffa0"
# Characters that print nothing and are not format characters: written out here on purpose, so
# the reference does not borrow the list it is checking.
NOTHING_EXTRA = (
    {"\u034f", "\u17b4", "\u17b5"}
    | {chr(c) for c in range(0x180B, 0x180F)}
    | {chr(c) for c in range(0xFE00, 0xFE0F)}
    | {chr(c) for c in range(0xE0100, 0xE01F0)}
)
# Capitals that draw like a Latin letter of a label word (Cyrillic, then Greek).
CONFUSABLE = {
    "\u0410": "A", "\u0421": "C", "\u0415": "E", "\u0406": "I", "\u04c0": "I", "\u041c": "M",
    "\u041e": "O", "\u0420": "P", "\u0422": "T",
    "\u0391": "A", "\u0395": "E", "\u0399": "I", "\u039c": "M", "\u039f": "O", "\u03a1": "P",
    "\u03a4": "T",
}  # fmt: skip


def _nothing(ch):
    return unicodedata.category(ch) == "Cf" or ch in NOTHING_EXTRA


def _accent(ch):
    return unicodedata.category(ch) in ("Mn", "Mc", "Me") and ch != "\ufe0f" and not _nothing(ch)


def _rider(ch):
    return ch == "\ufe0f" or "\U0001f3fb" <= ch <= "\U0001f3ff" or _accent(ch)


def _draws_nothing(text):
    """For each character: does it draw as nothing? Worked out by looking backwards.

    A format character and the listed oddities (selectors, fillers that are not blanks, the
    grapheme joiner) draw nothing. So does U+FE0F unless the nearest character in front of it
    that is not itself a rider (another selector, a skin-tone swatch, an accent) is a symbol
    that is not a blank glyph, or a keycap base (0-9, # or *), with no invisible in between.
    """
    flags = []
    for index, ch in enumerate(text):
        if _nothing(ch):
            flags.append(True)
        elif ch != "\ufe0f":
            flags.append(False)
        else:
            back = index - 1
            while back >= 0 and _rider(text[back]) and not _nothing(text[back]):
                back -= 1
            base = text[back] if back >= 0 else ""
            rides = (
                bool(base)
                and not _nothing(base)
                and base not in BLANK_GLYPHS
                and (unicodedata.category(base).startswith("S") or base in "0123456789#*")
            )
            flags.append(not rides)
    return flags


def _reference_round(current):
    """One left-to-right pass: look at the drawn characters only, then cut in the original.

    The drawn characters are projected out with their positions. A marker is the bot's emoji,
    riders, one space (or a blank glyph), the word (confusable capitals read as the Latin
    letter), a word end, and the " \u00b7" or "\u00b7" if there is one, all in the
    projection. Cutting it from the original takes the hidden characters touching it on both
    sides.
    """
    gone = _draws_nothing(current)
    shown = [k for k, g in enumerate(gone) if not g]
    proj = "".join(current[k] for k in shown)
    latin = proj.translate(str.maketrans(CONFUSABLE))
    spaces = " " + BLANK_GLYPHS
    cuts = []
    p = 0
    while p < len(proj):
        end = None
        for word, emoji in BOT_EMOJI.items():
            if proj[p] != emoji:
                continue
            q = p + 1
            while q < len(proj) and _rider(proj[q]):
                q += 1
            if q >= len(proj) or proj[q] not in spaces or not latin.startswith(word, q + 1):
                continue
            q += 1 + len(word)
            nxt = proj[q] if q < len(proj) else ""
            if nxt and (nxt.isalnum() and nxt not in BLANK_GLYPHS or nxt == "_" or _accent(nxt)):
                continue
            # Any run of spaces and blank glyphs may sit between the word and its dot.
            r = q
            while r < len(proj) and proj[r] in spaces:
                r += 1
            if r < len(proj) and proj[r] == "\u00b7":
                q = r + 1
            end = q
            break
        if end is None:
            p += 1
            continue
        first = shown[p]
        while first > 0 and gone[first - 1]:
            first -= 1
        last = shown[end - 1] + 1
        while last < len(current) and gone[last]:
            last += 1
        cuts.append((first, last))
        p = end
    out, at = [], 0
    for first, last in cuts:
        out.append(current[at:first])
        out.append(" ")
        at = last
    out.append(current[at:])
    return " ".join("".join(out).split())


def _reference_drop(text):
    """Exact-marker removal done the slow, obvious way: rounds until nothing changes."""
    current = " ".join(text.split())
    while True:
        nxt = _reference_round(current)
        if nxt == current:
            return current
        current = nxt


EXACT_POOL = [
    "\U0001f7e2", "\U0001f7e1", "\U0001f534", "\U0001f501", "\u2705", "OFFICIAL", "REPORTED",
    "RUMOR", "UPDATE", "OFFICIALS", "official", "\u00b7", " \u00b7", " ", " ", " ", "x", "_", "1",
    "\ufe0f", "\U0001f3fd", "\u0301", "\u200d", "\u200b", "\ue000", "\ufffd", "\u20e3",
    "\u180e", "\u034f", "*", "\u2605",
]  # fmt: skip


def test_the_marker_scan_agrees_with_the_rounds_it_replaced_on_random_strings():
    from newsbot.bot.format import _drop_exact_markers

    rng = random.Random(424242)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(60_000):
        text = "".join(rng.choice(EXACT_POOL) for _ in range(rng.randint(1, 14)))
        single = " ".join(text.split())
        assert _drop_exact_markers(single) == _reference_drop(single), repr(text)


def test_the_marker_scan_agrees_with_the_rounds_on_nested_and_glued_markers():
    from newsbot.bot.format import _drop_exact_markers

    green, yellow = BOT_EMOJI["OFFICIAL"], BOT_EMOJI["REPORTED"]
    for text in [
        f"x {green} {yellow} REPORTED OFFICIAL y",
        f"x {green} {green} {green} OFFICIAL OFFICIAL OFFICIAL y",
        f"{green}\u0301 {green}\ufe0f OFFICIAL OFFICIAL",
        f"a {green} OFFICIAL\u00b7{yellow} REPORTED\u00b7 b",
        f"a {green} OFFICIAL \u00b7 \u00b7 b",
        f"{green} {green} OFFICIAL\u0301 OFFICIAL",
        f"{green} OFFICIAL {green} OFFICIAL {green} OFFICIAL",
        f"{green}\U0001f3fd\u0301\ufe0f OFFICIAL tail",
    ]:
        assert _drop_exact_markers(text) == _reference_drop(text), repr(text)


def test_the_whole_stripper_is_the_leading_strip_and_the_marker_scan_run_to_a_fixed_point():
    from newsbot.bot.format import _strip_leading_labels

    rng = random.Random(777)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = EXACT_POOL + [":", "-", "|", "\u2013", "\u2014", "\u2b1c", "\U0001f3ff", "\U0001f3fb"]
    for _ in range(30_000):
        text = " ".join("".join(rng.choice(pool) for _ in range(rng.randint(1, 12))).split())
        kept = text
        while True:
            nxt = _reference_drop(_strip_leading_labels(kept))
            if nxt == kept:
                break
            kept = nxt
        assert _without_labels(text) == (kept or text), repr(text)


def test_text_with_no_marker_and_no_leading_label_comes_back_byte_for_byte():
    rng = random.Random(31337)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = EXACT_POOL + ["\u034f", "\u180e", "\u202e", "\U000f0000", "\ue001", "\u2605"]
    checked = 0
    for _ in range(60_000):
        lead = rng.choice(["Valve ", ZWSP + "Valve ", "\u034fValve ", "\u0301Valve "])
        text = lead + "".join(rng.choice(pool) for _ in range(rng.randint(0, 12)))
        single = " ".join(text.split())
        if _reference_drop(single) != single:
            continue
        checked += 1
        assert _without_labels(single) == single, repr(text)
    assert checked > 5000


def test_a_pre_check_hit_with_nothing_to_remove_does_not_collapse_or_trim_anything():
    # The hint pattern fires on any space before a label word; the scan then finds no marker.
    for text in [
        "a OFFICIAL b",
        "x \U0001f7e2OFFICIAL y",
        "a \U0001f7e2 OFFICIALS",
        "a UPDATE \u00b7 b",
    ]:
        assert _without_labels(text) == text


@pytest.mark.parametrize("char", ["\ue000", "\ue001", "\U000f0000", "\ufffd", "\u2605"])
def test_a_private_use_or_replacement_character_after_the_label_word_is_just_punctuation(char):
    # Only a combining mark stops the word. U+E000 is the sentinel for one inside the folding,
    # and a real one in the text must not be able to pose as a mark.
    assert _ai_line(f"\u2705 OFFICIAL{char} x") == f"{char} x"
    assert _ai_line(f"\u2705 UPDATE \u00b7 {char}{char} x") == f"{char}{char} x"


@pytest.mark.parametrize("char", ["\ue000", "\ue001", "\U000f0000", "\ufffd"])
def test_a_private_use_character_does_not_ride_on_the_emoji_like_a_mark_does(char):
    leading = f"\u2705{char} OFFICIAL x"
    assert _ai_line(leading) == leading
    middle = f"y \U0001f7e2{char} OFFICIAL x"
    assert _ai_line(middle) == middle
    assert _ai_line(f"{char}\u2705 OFFICIAL x") == f"{char}\u2705 OFFICIAL x"
    assert _ai_line(f"OFFICIAL{char}: x") == f"OFFICIAL{char}: x"


def test_a_real_sentinel_character_in_the_tail_comes_back_as_it_was_written():
    assert _ai_line("\u2705 OFFICIAL \u00b7 \ue000\ufffd\ue001 end") == "\ue000\ufffd\ue001 end"
    assert _ai_line("\ue000 \u2705 OFFICIAL") == "\ue000 \u2705 OFFICIAL"


def test_the_sentinel_does_not_make_a_real_mark_vanish_from_the_tail():
    assert _ai_line("\u2705 OFFICIAL \u00b7 e\u0301\u0301 x") == "e\u0301\u0301 x"


@pytest.mark.parametrize("rider", RIDERS)
def test_whatever_rides_on_a_lookalike_emoji_belongs_to_it_at_the_front(rider):
    assert _ai_line(f"\u2705{rider} OFFICIAL \u00b7 tail") == "tail"
    assert _ai_line(f"\u2705{rider}{rider} UPDATE tail") == "tail"
    assert _ai_line(f"\u2705{rider}\u2705{rider} RUMOR: tail") == "tail"
    assert _ai_line(f"\U0001f7e2{rider} tail") == "tail"


@pytest.mark.parametrize("rider", RIDERS)
def test_whatever_rides_on_the_bots_emoji_belongs_to_it_in_the_middle_too(rider):
    for word, emoji in BOT_EMOJI.items():
        assert _ai_line(f"a {emoji}{rider} {word} b") == "a b"
        assert _ai_line(f"a {emoji}{rider}{rider} {word} \u00b7 b") == "a b"


def test_riders_in_any_order_and_any_number_still_belong_to_the_emoji():
    rng = random.Random(5)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(500):
        riders = "".join(rng.choice(RIDERS) for _ in range(rng.randint(0, 9)))
        assert _ai_line(f"a \U0001f7e1{riders} REPORTED \u00b7 b") == "a b"
        assert _ai_line(f"\U0001f535{riders} REPORTED \u00b7 b") == "b"


def test_a_rider_that_is_not_a_swatch_or_a_mark_does_not_belong_to_the_emoji():
    # U+1F3FA is next to the swatches (an amphora), U+1F3FC is the last-but-one swatch.
    text = "a \U0001f7e2\U0001f3fa OFFICIAL b"
    assert _ai_line(text) == text
    assert _ai_line("a \U0001f7e2\U0001f3fc OFFICIAL b") == "a b"
    assert _ai_line("a \U0001f7e2\U0001f400 OFFICIAL b") == "a \U0001f7e2\U0001f400 OFFICIAL b"


def test_a_variation_selector_standing_between_the_space_and_the_label_word_hides_nothing():
    """AMBIGUOUS BRIEF: U+FE0F after the space is not riding on the emoji, but it draws as nothing.

    "<green> <FE0F>OFFICIAL" reads as the bot's marker. The rulings say what belongs to the
    emoji; they do not say what to do with a selector that has been moved behind the space.
    """
    assert "OFFICIAL" not in _ai_line("x \U0001f7e2 \ufe0fOFFICIAL y")
    assert "OFFICIAL" not in _ai_line("\U0001f7e2 \ufe0fOFFICIAL y")


def test_a_variation_selector_after_the_label_word_is_not_left_standing_alone():
    """AMBIGUOUS BRIEF: the word ends before U+FE0F (it is not counted as a mark), so the marker
    comes out and the selector is left in the middle of the line with nothing to select.
    """
    out = _ai_line("x \U0001f7e2 OFFICIAL\ufe0f y")
    assert "\ufe0f" not in out


def test_a_zero_width_joiner_between_the_emoji_and_the_space_hides_a_middle_marker():
    """AMBIGUOUS BRIEF: invisibles are skipped inside the leading run (the rulings say so) but
    the mid-line scan does not skip them, so `<green><ZWJ> OFFICIAL` survives in the middle of
    a line and not at the front.
    """
    assert _ai_line("\U0001f7e2\u200d OFFICIAL \u00b7 tail") == "tail"
    assert "OFFICIAL" not in _ai_line("x \U0001f7e2\u200d OFFICIAL y")


def test_invisibles_inside_the_leading_run_are_skipped_everywhere_in_it():
    for inv in [ZWSP, ZWJ, "\u034f", "\u180e", "\u202e", "\u00ad", "\ufeff"]:
        assert _ai_line(f"\u2705{inv} {inv}OFFICIAL{inv} \u00b7 {inv}tail") == "tail"
        assert _ai_line(f"{inv}\u2705{inv}\u2705{inv} OFF{inv}ICIAL tail") == "tail"
        assert _ai_line(f"UPD{inv}ATE {inv}: tail") == "tail"
        assert _ai_line(f"OFFICIAL{inv}S: tail") == f"OFFICIAL{inv}S: tail"


def test_a_combining_mark_after_the_word_is_not_hidden_by_an_invisible_in_between():
    for text in [
        "\u2705 OFFICIAL\u200d\u0301 x",
        "\u2705 OFFICIAL\u200b\u0301 patch",
        "UPDATE\u200d\u0301: x",
    ]:
        assert _ai_line(text) == text


def test_a_run_of_lookalike_emoji_strips_in_front_of_a_word_and_only_then():
    assert _ai_line("\u2705\u2705\u2705 OFFICIAL tail") == "tail"
    assert _ai_line("\u2705 \u2611\ufe0f \U0001f7e2 \U0001f504 REPORTED: tail") == "tail"
    assert _ai_line("\u2705\u2705 tail") == "\u2705\u2705 tail"
    assert _ai_line("\u2705\u2705 Official patch") == "patch"
    assert _ai_line("\u2705 x \u2705 OFFICIAL tail") == "\u2705 x \u2705 OFFICIAL tail"


def test_a_run_of_bot_emoji_alone_strips_with_whatever_rides_on_each():
    assert (
        _ai_line("\U0001f7e2\U0001f3fd \U0001f7e1\ufe0f\U0001f534\u0301\U0001f501 tail") == "tail"
    )


def test_the_stripper_is_idempotent_with_marks_riders_and_the_sentinel_in_the_pool():
    rng = random.Random(2026)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = EXACT_POOL + [
        ":",
        "-",
        "|",
        "\u2014",
        "\u2b1c",
        "\U0001f3ff",
        "\u034f",
        "\u180e",
        "\u0131",
    ]
    for _ in range(40_000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 12)))
        once = _ai_line(text)
        assert _ai_line(once) == once, repr(text)


def test_the_result_never_contains_a_whole_exact_marker_or_starts_with_a_label():
    from newsbot.bot.format import _strip_leading_labels

    rng = random.Random(8)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(30_000):
        text = "".join(rng.choice(EXACT_POOL) for _ in range(rng.randint(1, 12)))
        out = _ai_line(text)
        if out == " ".join(text.split()):
            continue  # nothing was taken off; the fallback or an unmatched text
        single = " ".join(out.split())
        assert _reference_drop(single) == single, repr((text, out))
        assert _strip_leading_labels(out) == out, repr((text, out))


# Speed on the new code. The limits are generous (the real figures are a tenth of these).


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("a run of look-alikes then a plain word", "\u2705 " * 50_000 + "x"),
        ("look-alikes then a longer word", "\u2705 " * 30_000 + "OFFICIALS"),
        ("alternating bot and look-alike emoji", "\U0001f7e2 \u2705 " * 50_000 + "x"),
        ("selectors on look-alikes", "\u2705\ufe0f" * 50_000 + " x"),
        ("marks on look-alikes", "\u2705\u0301" * 50_000 + " x"),
        ("a hundred thousand marks first", "\u0301" * 100_000 + "\u2705 OFFICIAL"),
        ("a hundred thousand invisibles first", ZWSP * 100_000 + "\u2705 OFFICIAL tail"),
        ("riders, a space, a word", "x" + "\u0301" * 100_000 + " OFFICIAL" * 10),
        ("many short rider runs", ("a" + "\u0301" * 1000 + " OFFICIAL") * 90),
        ("riders on the bot emoji, nested", "x " + "\U0001f7e2\u0301 " * 5000 + "OFFICIAL " * 5000),
        ("plain nesting", "x " + "\U0001f7e2 " * 20_000 + "OFFICIAL " * 20_000),
        ("two-level nesting", "x " + "\U0001f7e2 \U0001f7e1 REPORTED OFFICIAL " * 8000),
        ("leading labels and mid markers alternate", "UPDATE: \U0001f7e2 OFFICIAL " * 8000 + "x"),
        ("a word that never completes a marker", "\u2705 OFFICIALS " * 8000),
        ("bot emoji and a word that never completes", "\U0001f7e2 \u2705 OFFICIALS " * 8000),
        ("a long run of one bot emoji with marks", ("\U0001f7e2\u0301" * 50_000) + " tail"),
        ("sentinel characters", "\ue000\ufffd" * 50_000 + "\u2705 OFFICIAL tail"),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 60 and v.isascii() else None,
)
def test_the_rewritten_stripper_is_fast_on_adversarial_input(name, text):
    _, took = _timed(_without_labels, text)
    assert took < 2.0, f"{name}: {took:.1f} s for {len(text)} characters"


def test_the_nested_title_that_was_quadratic_is_now_fast_through_the_headline_path():
    depth = 20_000
    title = "x " + "\U0001f7e2 " * depth + "OFFICIAL " * depth + "tail"
    out, took = _timed(_headline_line, _headline(title))
    assert "OFFICIAL" not in out
    assert took < 2.0


def test_doubling_the_nesting_depth_does_not_quadruple_the_time():
    def run(depth):
        text = "x " + "\U0001f7e2 " * depth + "OFFICIAL " * depth + "tail"
        best = min(_timed(_without_labels, text)[1] for _ in range(3))
        return max(best, 1e-4)

    assert run(40_000) < run(10_000) * 10  # linear would be 4, quadratic 16


# `_truncate_description` and its backslash.


def _expected_cut(text, limit):
    if len(text) <= limit:
        return text
    head = text[: limit - 1].rstrip()
    if (len(head) - len(head.rstrip("\\"))) % 2:
        head = head[:-1]
    return head + "\u2026"


def test_a_cut_after_an_odd_run_of_backslashes_drops_one_and_after_an_even_run_keeps_them():
    from newsbot.bot.format import _truncate_description

    for run in range(0, 8):
        text = "ab" + "\\" * run + "tail that is long"
        for limit in range(3, len(text)):
            assert _truncate_description(text, limit) == _expected_cut(text, limit)


def test_an_escaped_backslash_before_the_cut_stays_whole():
    from newsbot.bot.format import _truncate_description

    # esc("\\") is two backslashes; a cut right after both is a complete escaped backslash.
    text = "abc" + "\\\\" + "defghij"
    assert _truncate_description(text, 6) == "abc\\\\\u2026"
    # A cut between the two would leave one that escapes the ellipsis, so it goes.
    assert _truncate_description(text, 5) == "abc\u2026"


def test_a_cut_after_an_escape_and_an_escaped_backslash_keeps_the_pair_and_drops_the_escape():
    from newsbot.bot.format import _truncate_description

    text = "abc" + "\\\\" + "\\*" + "defghij"  # three backslashes, then the star
    assert _truncate_description(text, 7) == "abc\\\\\u2026"


def test_a_description_that_fits_is_returned_untouched_even_if_it_ends_in_a_backslash():
    from newsbot.bot.format import _truncate_description

    for text in ["abc\\", "abc\\\\", "\\", "", "x" * 4096]:
        assert _truncate_description(text) == text
    assert _truncate_description("a" * 7 + "\\", 8) == "a" * 7 + "\\"


def test_a_cut_through_a_text_that_is_all_backslashes_never_exceeds_the_limit():
    from newsbot.bot.format import _truncate_description

    for count in (1, 2, 3, 10, 5000):
        for limit in (1, 2, 3, 4, 100):
            out = _truncate_description("\\" * count, limit)
            assert discord_len(out) <= limit or count <= limit
            if count > limit:
                body = out.removesuffix("\u2026")
                assert (len(body) - len(body.rstrip("\\"))) % 2 == 0


def test_random_cuts_through_text_with_backslashes_agree_with_the_simple_rule():
    from newsbot.bot.format import _truncate_description

    rng = random.Random(11)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(5000):
        text = "".join(
            rng.choice(["a", "\\", "\\", "*", "\u00e9"]) for _ in range(rng.randint(0, 40))
        )
        limit = rng.randint(1, 45)
        assert _truncate_description(text, limit) == _expected_cut(text, limit), (text, limit)


def test_the_other_truncation_helper_still_leaves_the_backslash_alone():
    from newsbot.bot.format import _truncate_utf16

    # Pinned by older tests for source names; the fix is in `_truncate_description` only.
    assert _truncate_utf16("abc\\\\defg", 5, suffix="\u2026") == "abc\\\u2026"


def test_a_story_page_cut_leaves_an_even_run_of_backslashes_in_front_of_the_ellipsis():
    for pad in range(3900, 4100):
        text = _page_description(["w" * pad, "1. second *bold* \\ x"])
        if text.endswith("\u2026"):
            body = text.removesuffix("\u2026")
            assert (len(body) - len(body.rstrip("\\"))) % 2 == 0
            assert discord_len(text) <= 4096


# --- round 3: characters that draw as nothing, inside a mid-line marker ---------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("x \U0001f534 \u200bRUMOR y", "x y"),  # invisible between the space and the word
        ("x \U0001f534 \u200dRUMOR y", "x y"),  # a joiner there
        ("x \U0001f7e1\u180e REPORTED y", "x y"),  # U+180E between the emoji and the space
        ("x \U0001f7e1 REPORTED\u200dREPORTED y", "x \U0001f7e1 REPORTED\u200dREPORTED y"),
        ("x \U0001f7e2 OFF\u200bICIAL y", "x y"),  # inside the word
        ("x \U0001f7e2 OFFICIAL\u200b \u00b7 y", "x y"),  # before the dot
        ("x \U0001f7e2 OFFICIAL \u200b\u00b7 y", "x y"),
        ("x \U0001f7e2 OFFICIAL\u200b\u00b7\u200d y", "x y"),
        ("x \U0001f7e2 \ufe0fOFFICIAL y", "x y"),  # a selector with no emoji to ride on
        ("x \U0001f7e2 OFFICIAL\ufe0f y", "x y"),
        ("x \U0001f7e2\u200d OFFICIAL y", "x y"),
        ("x\u200b \u200b\U0001f7e2\u200d OFFICIAL\ufe0f y", "x\u200b y"),
        ("x \u2605\ufe0f \U0001f7e2 OFFICIAL y", "x \u2605\ufe0f y"),  # a riding selector stays
        ("x \U0001f7e2 OFFICIAL\u200b\u0301 y", "x \U0001f7e2 OFFICIAL\u200b\u0301 y"),
        ("a\u200b\U0001f7e2 OFFICIAL\u200bb", "a\u200b\U0001f7e2 OFFICIAL\u200bb"),
        ("x \U0001f7e2 OFFICIAL\u200bs y", "x \U0001f7e2 OFFICIAL\u200bs y"),
        ("x \U0001f7e2 OFFICIAL\u200b_ y", "x \U0001f7e2 OFFICIAL\u200b_ y"),
        ("x \U0001f7e2 OFFICIAL\u200b1 y", "x \U0001f7e2 OFFICIAL\u200b1 y"),
    ],
)
def test_hidden_characters_inside_a_mid_line_marker_do_not_save_it(text, expected):
    assert _ai_line(text) == expected


def test_the_word_end_check_looks_past_invisibles_the_way_the_leading_strip_does():
    # "REPORTED<ZWJ>REPORTED" is one long word to a reader, so neither strip treats the first
    # REPORTED as a word of its own.
    text = "\u2705 REPORTED\u200dREPORTED tail"
    assert _ai_line(text) == text
    assert (
        _ai_line("x \U0001f7e1 REPORTED\u200dREPORTED y") == "x \U0001f7e1 REPORTED\u200dREPORTED y"
    )
    assert _ai_line("\u2705 REPORTED\u200d tail") == "tail"


def test_a_removed_marker_takes_only_the_hidden_characters_touching_it():
    # The joiner beside "x" has a space between it and the marker, so it stays.
    assert _ai_line("x\u200d \U0001f7e2 OFFICIAL y") == "x\u200d y"
    # Touching on the left, it goes.
    assert _ai_line("x \u200d\U0001f7e2 OFFICIAL y") == "x y"
    # Touching on the right, it goes too (after the word, after the dot).
    assert _ai_line("x \U0001f7e2 OFFICIAL\u200b\u200d y") == "x y"
    assert _ai_line("x \U0001f7e2 OFFICIAL \u00b7\u200b\u200d y") == "x y"
    # Behind the next word it stays.
    assert _ai_line("x \U0001f7e2 OFFICIAL y\u200b") == "x y\u200b"


@pytest.mark.parametrize("base", ["0", "5", "#", "*", "\u2605", "\u2764", "\u00a9"])
def test_a_selector_on_a_symbol_or_keycap_base_rides_and_is_left_alone(base):
    # The selector on the base is not hidden, so it is not taken by a marker after the base.
    assert _ai_line(f"y {base}\ufe0f \U0001f7e2 OFFICIAL z") == f"y {base}\ufe0f z"


@pytest.mark.parametrize("base", ["a", "A", "\u00e9", "\u3042", "\u4e2d", ")", ".", "\u00b7"])
def test_a_selector_after_a_letter_or_punctuation_draws_nothing_and_hides_a_label(base):
    assert (
        _ai_line(f"y \U0001f7e2 {base}\ufe0f OFFICIAL z") == f"y \U0001f7e2 {base}\ufe0f OFFICIAL z"
    )
    assert _ai_line("y \U0001f7e2 \ufe0fOFFICIAL z") == "y z"


def _structured_text(rng):
    hidden = [
        "",
        "",
        "",
        ZWSP,
        ZWJ,
        "\u180e",
        "\u034f",
        "\ufe0f",
        "\u00ad",
        "\ufeff",
        "\u0301",
        "\ufe0f\ufe0f",
    ]

    def marker():
        word = rng.choice(list(BOT_EMOJI))
        parts = [BOT_EMOJI[word], rng.choice(hidden), " ", rng.choice(hidden)]
        for letter in word:
            parts += [letter, rng.choice(hidden)]
        if rng.random() < 0.6:
            parts += [rng.choice([" ", ""]), rng.choice(hidden), "\u00b7", rng.choice(hidden)]
        if rng.random() < 0.2:
            parts.append(rng.choice(["s", "1", "_", "\u0301", "x"]))
        return "".join(parts)

    pieces = [marker, marker, lambda: "x", lambda: " ", lambda: "y z", lambda: "\u00b7"]
    pieces += [lambda: rng.choice(hidden), lambda: "\U0001f7e2", lambda: "OFFICIAL"]
    return "".join(rng.choice(pieces)() for _ in range(rng.randint(1, 5)))


def test_markers_built_with_hidden_characters_everywhere_agree_with_the_reference():
    from newsbot.bot.format import _drop_exact_markers

    rng = random.Random(3)  # noqa: S311 (a reproducible sequence, not a secret)
    changed = 0
    for _ in range(60_000):
        single = " ".join(_structured_text(rng).split())
        expected = _reference_drop(single)
        changed += expected != single
        assert _drop_exact_markers(single) == expected, repr(single)
    assert changed > 5000, "the generator stopped producing markers"


def test_the_whole_stripper_agrees_with_the_reference_on_hidden_character_markers():
    from newsbot.bot.format import _strip_leading_labels

    rng = random.Random(4)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(30_000):
        text = " ".join(_structured_text(rng).split())
        kept = text
        while True:
            nxt = _reference_drop(_strip_leading_labels(kept))
            if nxt == kept:
                break
            kept = nxt
        assert _without_labels(text) == (kept or text), repr(text)


def test_hidden_character_markers_are_still_stable_and_marker_free_after_one_pass():
    rng = random.Random(5)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(30_000):
        text = _structured_text(rng)
        once = _ai_line(text)
        assert _ai_line(once) == once, repr(text)
        if once != " ".join(text.split()):
            single = " ".join(once.split())
            assert _reference_drop(single) == single, repr((text, once))


@pytest.mark.parametrize(
    "hidden",
    [ZWSP, ZWJ, "\u180e", "\u034f", "\ufe0f", "\u00ad", "\ufeff"],
)
def test_text_with_hidden_characters_but_no_marker_comes_back_byte_for_byte(hidden):
    for text in [
        f"Valve{hidden} buys{hidden} Nintendo",
        f"Valve \U0001f7e2{hidden} Nintendo",
        f"Valve OFFI{hidden}CIAL \u00b7 news",
        f"Valve {hidden}\U0001f7e2 RUMOR{hidden}S",
        f"Valve \u2705{hidden} OFFICIAL news",
    ]:
        assert _ai_line(text) == text


def test_a_hundred_thousand_hidden_characters_in_and_around_markers_are_fast():
    unit = "\U0001f7e2\u200d \u200bOFF\u200bICIAL\ufe0f \u200b\u00b7\u200d "
    for text in [
        "x " + unit * 5000 + "tail",
        "x " + "\U0001f7e2 " * 10_000 + "\u200bOFFICIAL " * 10_000,
    ]:
        out, took = _timed(_without_labels, text)
        assert took < 2.0, f"{took:.1f} s for {len(text)} characters"
        assert "OFFICIAL" not in out


# --- fix batch 1: the full invisible list, blank glyphs, confusables, the 2,000 cap ---------

import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

from newsbot.bot.format import _headline_title, _without_bot_emoji  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIRST_COMMIT = "cefc37f"  # the first round, before the full invisible list
BASE_COMMIT = "1680edc"  # main as the branch started

HANGUL_BLANKS = ["\u115f", "\u1160", "\u3164", "\uffa0"]
ALL_BLANKS = [*HANGUL_BLANKS, "\u2800"]
SELECTORS = ["\ufe00", "\ufe01", "\ufe0e", "\U000e0100", "\U000e01ef"]
OTHER_NOTHINGS = ["\u180b", "\u180c", "\u180d", "\u180e", "\u17b4", "\u17b5", "\u034f"]


def _module_at(commit, relpath, name):
    """`relpath` as of `commit`, run as a throwaway module called `name`."""
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{commit}:{relpath}"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {commit} is not available here")
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture(scope="module")
def first_round_format():
    module = _module_at(FIRST_COMMIT, "newsbot/bot/format.py", "_newsbot_format_first_round")
    yield module
    sys.modules.pop(module.__name__, None)


@pytest.fixture(scope="module")
def base_lifecycle():
    module = _module_at(BASE_COMMIT, "newsbot/guilds/lifecycle.py", "_newsbot_lifecycle_base")
    yield module
    sys.modules.pop(module.__name__, None)


# Every probe from the qa pass: stripped in both paths.
QA_PROBES = [
    ("\U0001f7e9 OFFICIAL\ufe0e \u00b7 free codes", "free codes"),
    ("a \U0001f7e2 OFFICIAL\ufe0e b", "a b"),
    ("UPDATE\ufe00: free codes", "free codes"),
    ("\U0001f7e9 OFFICIAL\U000e0100 \u00b7 free codes", "free codes"),
    ("\U0001f7e9 OFFICIAL\u180b \u00b7 free codes", "free codes"),
    ("\U0001f7e9 OFFICIAL\u17b4 \u00b7 free codes", "free codes"),
    ("\U0001f7e9\u2800OFFICIAL \u00b7 free codes", "free codes"),
    ("\U0001f7e2\u3164OFFICIAL \u00b7 x", "x"),
    ("a \U0001f7e2\u2800OFFICIAL b", "a b"),
    ("\U0001f7e9 \u041eFFICIAL \u00b7 free codes", "free codes"),
    ("\u041eFFICIAL: free codes", "free codes"),
    ("\U0001f7e2 OFFICIAL \u00b7x", "x"),
]


@pytest.mark.parametrize(("text", "expected"), QA_PROBES)
def test_every_qa_probe_is_stripped_in_the_ai_line_and_in_the_headline_title(text, expected):
    assert _ai_line(text) == expected
    assert _shown_title(text) == expected


@pytest.mark.parametrize("glyph", ALL_BLANKS, ids=lambda g: f"U+{ord(g):04X}")
def test_a_blank_glyph_stands_in_for_the_space_in_every_position_it_can_be_read_as_one(glyph):
    # Between the emoji and the word, at the front and in the middle.
    assert _ai_line(f"\u2705{glyph}OFFICIAL \u00b7 tail") == "tail"
    assert _ai_line(f"\U0001f7e2{glyph}OFFICIAL tail") == "tail"
    assert _ai_line(f"x \U0001f7e2{glyph}OFFICIAL y") == "x y"
    assert _ai_line(f"x \U0001f7e2{glyph}\u200bOFFICIAL y") == "x y"
    # Between the word and the dot.
    assert _ai_line(f"OFFICIAL{glyph}: tail") == "tail"
    # In front of the whole run.
    assert _ai_line(f"{glyph}\u2705 OFFICIAL tail") == "tail"
    assert _ai_line(f"{glyph}{glyph}\u2705{glyph}UPDATE{glyph}: tail") == "tail"


@pytest.mark.parametrize("glyph", HANGUL_BLANKS, ids=lambda g: f"U+{ord(g):04X}")
def test_a_hangul_filler_after_the_label_word_ends_the_word_in_the_middle_of_a_line(glyph):
    """BUG: the word-end check uses `str.isalnum()`, and the Hangul fillers are letters (Lo).

    The brief for this batch says a blank glyph counts as a space when matching. After the
    label word it does at the front of a line (`<check> OFFICIAL<filler>y` strips), and for
    U+2800 (a symbol) in the middle, but a filler after the word in the middle of a line is read
    as one more letter of it, so `x <green> OFFICIAL<filler>y` keeps its marker.
    """
    for text in [
        f"x \U0001f7e2 OFFICIAL{glyph}y",
        f"x \U0001f7e2 OFFICIAL{glyph}",
        f"x \U0001f7e2 OFFICIAL{glyph}\u00b7 y",
        f"x \U0001f7e2 OFFICIAL{glyph}\u00b7y",
    ]:
        assert "OFFICIAL" not in _ai_line(text), repr(text)
        assert _ai_line(text) == _reference_drop(text), repr(text)


def test_a_blank_glyph_is_not_kept_out_of_text_that_has_no_label():
    for glyph in ALL_BLANKS:
        for text in [
            f"Valve{glyph}buys{glyph}Nintendo",
            f"{glyph}Valve buys",
            f"Valve buys{glyph}",
            f"Valve \U0001f7e2{glyph}Nintendo",
            f"Valve OFFICIAL{glyph}patch",
            f"Valve \u2705{glyph}Official patch",
        ]:
            assert _ai_line(text) == text, repr(text)


def test_a_blank_glyph_does_not_stop_a_later_label_word_from_being_a_different_word():
    # A blank glyph is a space, so "OFFICIAL<blank>S" is the word then an S, but a letter glued
    # on without one still makes a longer word.
    assert _ai_line("\u2705 OFFICIALS: tail") == "\u2705 OFFICIALS: tail"
    assert _ai_line("\u2705 OFFICIAL\u2800s: tail") == "s: tail"


@pytest.mark.parametrize("selector", SELECTORS, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("other", OTHER_NOTHINGS, ids=lambda c: f"U+{ord(c):04X}")
def test_every_invisible_in_the_full_list_is_skipped_at_the_front_and_in_the_middle(
    selector, other
):
    for hidden in (selector, other):
        assert _ai_line(f"\u2705{hidden} OFFICIAL{hidden} \u00b7{hidden} tail") == "tail"
        assert _ai_line(f"{hidden}\u2705 {hidden}OFF{hidden}ICIAL{hidden}: tail") == "tail"
        assert _ai_line(f"x \U0001f7e2{hidden} {hidden}OFF{hidden}ICIAL{hidden} y") == "x y"
        assert _ai_line(f"x \U0001f7e2 OFFICIAL{hidden} \u00b7{hidden} y") == "x y"


def test_an_invisible_mark_does_not_make_the_last_letter_an_accented_one():
    # U+0301 is a real accent: "OFFICIAL" + acute stays. The selectors and Khmer vowels are not.
    for hidden in [*SELECTORS, "\u17b4", "\u17b5", "\u180b", "\u034f"]:
        assert _ai_line(f"\u2705 OFFICIAL{hidden} tail") == "tail"
        assert _ai_line(f"x \U0001f7e2 OFFICIAL{hidden} y") == "x y"
    assert _ai_line("\u2705 OFFICIAL\u0301 tail") == "\u2705 OFFICIAL\u0301 tail"


def test_a_variation_selector_after_a_symbol_still_rides_and_after_a_blank_glyph_it_does_not():
    assert _ai_line("y \u2605\ufe0f \U0001f7e2 OFFICIAL z") == "y \u2605\ufe0f z"
    # A braille blank is category So (a symbol) but draws nothing for a selector to turn into
    # an emoji, so the selector after it is as invisible as one after a letter.
    assert _ai_line("y \U0001f7e2\u2800\ufe0fOFFICIAL z") == "y z"
    assert _ai_line("y \U0001f7e2 OFFICIAL\u2800\ufe0f\u00b7 z") == "y z"


# Confusable capitals.

CYRILLIC = {"A": "\u0410", "C": "\u0421", "E": "\u0415", "I": "\u0406", "M": "\u041c",
            "O": "\u041e", "P": "\u0420", "T": "\u0422"}  # fmt: skip
GREEK = {"A": "\u0391", "E": "\u0395", "I": "\u0399", "M": "\u039c", "O": "\u039f",
         "P": "\u03a1", "T": "\u03a4"}  # fmt: skip


def _confuse(word, table, which):
    return "".join(table.get(ch, ch) if i in which else ch for i, ch in enumerate(word))


@pytest.mark.parametrize("table", [CYRILLIC, GREEK], ids=["cyrillic", "greek"])
@pytest.mark.parametrize("word", WORDS)
def test_confusable_capitals_in_a_label_word_do_not_hide_it_in_any_position(table, word):
    every = set(range(len(word)))
    for which in (every, {0}, {len(word) - 1}, {1, 2}):
        said = _confuse(word, table, which)
        assert _ai_line(f"\u2705 {said} \u00b7 tail") == "tail"
        assert _ai_line(f"{said}: tail") == "tail"
        assert _shown_title(f"\U0001f7e2 {said}: tail") == "tail"
        emoji = BOT_EMOJI[word]
        assert _ai_line(f"x {emoji} {said} y") == "x y"
        assert _shown_title(f"x {emoji} {said} \u00b7 y") == "x y"


def test_lower_case_confusables_count_at_the_front_where_the_match_is_case_blind():
    assert _ai_line("\u2705 \u043e\u0444\u0438\u0446\u0438\u0430\u043b: tail") == (
        "\u2705 \u043e\u0444\u0438\u0446\u0438\u0430\u043b: tail"
    )
    assert _ai_line("\u2705 \u043efficial: tail") == "tail"
    assert _ai_line("\u2705 up\u0123ate: tail") == "\u2705 up\u0123ate: tail"


def test_a_confusable_in_a_bare_word_or_a_wrong_pair_is_still_ordinary_text():
    for text in [
        "\u041efficial patch notes",
        "Update 1.2 \u0410dds",
        "x \U0001f7e2 \u041eFFICIALS y",
        "x \U0001f7e1 \u041eFFICIAL y",  # wrong emoji for the word
        "x \U0001f7e2 \u0430FFICIAL y",  # lower case Cyrillic a is not a capital
    ]:
        assert _ai_line(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "\u041e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\u043e: \u043f\u0430\u0442\u0447 \u0432\u044b\u0448\u0435\u043b",  # noqa: E501
        "\u041e\u0424\u0418\u0426\u0418\u0410\u041b\u042c\u041d\u041e \u2014 \u043d\u043e\u0432\u043e\u0441\u0442\u0438",  # noqa: E501
        "\u0420\u0443\u043c\u043e\u0440: \u0432\u044b\u0448\u043b\u043e \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435",  # noqa: E501
        "\u0422\u0435\u043c\u0430 \u0434\u043d\u044f: \u041c\u0435\u0440\u0430 \u0420\u043e\u0441\u0430",  # noqa: E501
        "\u0395\u03c0\u03af\u03c3\u03b7\u03bc\u03b7 \u03b5\u03bd\u03b7\u03bc\u03ad\u03c1\u03c9\u03c3\u03b7: \u039f\u039c\u0391\u0394\u0391 \u03a0\u0391\u03a4\u03a4\u0397\u03a3",  # noqa: E501
        "\u039f\u03a6\u03a6\u0399\u039a\u0399\u0391\u039b \u03a0\u0391\u03a4\u03a7",
        "\u2705 \u041e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\u043e: \u043f\u0430\u0442\u0447",  # noqa: E501
    ],
)
def test_real_russian_and_greek_text_comes_back_byte_for_byte(text):
    # No pure-Cyrillic or pure-Greek word can fold into a label word: R, U, D, F and L have
    # no look-alike capital in either script.
    assert _ai_line(text) == text
    assert _shown_title(text) == text


def test_text_with_blank_glyphs_selectors_and_confusables_but_no_label_is_left_alone():
    pool = [
        *ALL_BLANKS, *SELECTORS, *OTHER_NOTHINGS, "\u041e", "\u0410", "\u0395", "\u039f",
        "\u2605", "\u2705", "x", "1.", ":", "\u00b7", "-", "\u0301", "Update", "OFFICIAL", " ",
    ]  # fmt: skip
    rng = random.Random(61)  # noqa: S311 (a reproducible sequence, not a secret)
    checked = 0
    for _ in range(60_000):
        text = "Valve " + "".join(rng.choice(pool) for _ in range(rng.randint(0, 10)))
        single = " ".join(text.split())
        if _reference_drop(single) != single:
            continue
        checked += 1
        assert _without_labels(single) == single, repr(single)
    assert checked > 5000


def test_the_marker_scan_still_agrees_with_the_reference_when_the_pool_has_the_new_characters():
    from newsbot.bot.format import _drop_exact_markers

    rng = random.Random(62)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [
        *EXACT_POOL, *ALL_BLANKS, *SELECTORS, *OTHER_NOTHINGS, "\u041e", "\u0410", "\u0421",
        "\u0395", "\u039f", "\u2605", ":",
    ]  # fmt: skip
    failures = []
    for _ in range(60_000):
        single = " ".join("".join(rng.choice(pool) for _ in range(rng.randint(1, 14))).split())
        if _drop_exact_markers(single) != _reference_drop(single):
            failures.append(single)
    # Hangul fillers after a word are the known bug above; everything else must agree.
    unexplained = [
        s
        for s in failures
        if _drop_exact_markers(s.translate(dict.fromkeys(map(ord, HANGUL_BLANKS), "\u2800")))
        != _reference_drop(s.translate(dict.fromkeys(map(ord, HANGUL_BLANKS), "\u2800")))
    ]
    assert unexplained == []
    assert failures == [], f"{len(failures)} disagreements, first {failures[0]!r}"


def test_markers_built_with_blank_glyphs_confusables_and_hidden_characters_agree():
    from newsbot.bot.format import _drop_exact_markers

    rng = random.Random(63)  # noqa: S311 (a reproducible sequence, not a secret)
    hidden = ["", "", "", ZWSP, ZWJ, "\u180e", "\ufe0f", "\ufe0e", "\U000e0100", "\u17b4", "\u0301"]
    letters = {"O": "\u041e", "A": "\u0410", "E": "\u0415", "I": "\u0406", "C": "\u0421",
               "P": "\u0420", "T": "\u0422", "M": "\u041c"}  # fmt: skip

    def space():
        return rng.choice([" ", " ", " ", rng.choice(ALL_BLANKS)])

    def marker():
        word = rng.choice(list(BOT_EMOJI))
        parts = [BOT_EMOJI[word], rng.choice(hidden), space(), rng.choice(hidden)]
        for letter in word:
            shown = letters.get(letter, letter) if rng.random() < 0.15 else letter
            parts += [shown, rng.choice(hidden)]
        if rng.random() < 0.6:
            parts += [space() if rng.random() < 0.5 else "", rng.choice(hidden), "\u00b7"]
            parts.append(rng.choice(hidden))
        if rng.random() < 0.25:
            parts.append(rng.choice(["s", "1", "_", "\u0301", "x", rng.choice(ALL_BLANKS), " "]))
        return "".join(parts)

    pieces = [marker, marker, lambda: "x", lambda: " ", lambda: "y z", lambda: "\u00b7"]
    pieces += [lambda: rng.choice(hidden), lambda: "OFFICIAL", lambda: rng.choice(ALL_BLANKS)]
    failures, changed = [], 0
    for _ in range(60_000):
        single = " ".join("".join(rng.choice(pieces)() for _ in range(rng.randint(1, 4))).split())
        expected = _reference_drop(single)
        changed += expected != single
        if _drop_exact_markers(single) != expected:
            failures.append(single)
    assert changed > 5000
    plain = dict.fromkeys(map(ord, HANGUL_BLANKS), "\u2800")
    unexplained = [
        s
        for s in failures
        if _drop_exact_markers(s.translate(plain)) != _reference_drop(s.translate(plain))
    ]
    assert unexplained == []
    assert failures == [], f"{len(failures)} disagreements, first {failures[0]!r}"


# The shared invisible list and lifecycle.

OLD_EXTRA = (
    frozenset("\u034f\u115f\u1160\u17b4\u17b5\u2800\u3164\uffa0")
    | frozenset(chr(c) for c in range(0x180B, 0x180F))
    | frozenset(chr(c) for c in range(0xFE00, 0xFE10))
    | frozenset(chr(c) for c in range(0xE0100, 0xE01F0))
)


def test_the_shared_invisible_list_is_exactly_the_one_lifecycle_used_to_own():
    from newsbot.bot.format import _INVISIBLE_EXTRA
    from newsbot.guilds import lifecycle

    assert _INVISIBLE_EXTRA == OLD_EXTRA
    assert lifecycle._INVISIBLE_EXTRA is _INVISIBLE_EXTRA


def test_lifecycle_calls_a_character_invisible_for_exactly_the_code_points_it_did_before():
    from newsbot.guilds import lifecycle

    def old(ch):
        return ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") or ch in OLD_EXTRA

    different = [
        hex(cp) for cp in range(0x110000) if lifecycle._is_invisible(chr(cp)) != old(chr(cp))
    ]
    assert different == []


def test_lifecycle_is_invisible_agrees_with_the_version_on_the_base_commit(base_lifecycle):
    from newsbot.guilds import lifecycle

    different = [
        hex(cp)
        for cp in range(0x110000)
        if lifecycle._is_invisible(chr(cp)) != base_lifecycle._is_invisible(chr(cp))
    ]
    assert different == []


def test_guild_labels_come_out_the_same_as_on_the_base_commit(base_lifecycle):
    from types import SimpleNamespace

    from newsbot.guilds import lifecycle

    rng = random.Random(64)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [
        *ALL_BLANKS, *SELECTORS, *OTHER_NOTHINGS, ZWSP, ZWJ, "\u202e", "\u00ad", " ", "\t", "\n",
        "a", "B", "*", "_", "@everyone", "<@123>", "\u2605", "\U0001f468", "\ud800", "\x00",
        "\u0301", "\u3000", "\u2800", "x" * 80,
    ]  # fmt: skip
    for _ in range(20_000):
        name = "".join(rng.choice(pool) for _ in range(rng.randint(0, 8)))
        for comped in (False, True):
            guild = SimpleNamespace(name=name, id=123456789012345678)
            assert lifecycle.guild_label(guild, comped=comped) == base_lifecycle.guild_label(
                guild, comped=comped
            ), repr(name)
    for name in [None, "", "   ", "\u2800\u3164", "\ufe0f", "\U000e0100", "\u17b4", ZWSP]:
        guild = SimpleNamespace(name=name, id=1)
        assert lifecycle.guild_label(guild) == base_lifecycle.guild_label(guild)


def test_the_first_round_and_this_one_strip_the_same_text_when_no_new_feature_is_involved(
    first_round_format,
):
    """The re-collapse that `_without_labels` used to do was removed as a no-op; check that."""
    import newsbot.bot.format as current

    rng = random.Random(65)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [*EXACT_POOL, ":", "-", "|", EM_DASH, "\u2b1c", "\U0001f3ff", "\u0131", "*", "\u2605"]
    compared = 0
    for _ in range(80_000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 14)))
        # A dot glued to the next word is the one thing this batch changed at the front.
        if any(
            c == "\u00b7" and i + 1 < len(text) and text[i + 1] != " " for i, c in enumerate(text)
        ):
            continue
        compared += 1
        assert current._ai_line(text) == first_round_format._ai_line(text), repr(text)
    assert compared > 20_000


def test_both_callers_hand_over_text_with_single_spaces_so_nothing_needs_collapsing_again():
    from newsbot.bot.format import _strip_labels

    rng = random.Random(66)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [*EXACT_POOL, *ALL_BLANKS, *SELECTORS, ":", "-", "\u2b1c", "\u041e", "\u2605"]
    for _ in range(60_000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 14)))
        single = " ".join(text.split())
        out = _strip_labels(single)
        # Single-spaced in, single-spaced out; no leading or trailing space either.
        assert out == " ".join(out.split()), repr((single, out))


# A leading dot glued to the next word.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\U0001f7e2 OFFICIAL \u00b7x", "x"),
        ("\U0001f7e2 OFFICIAL\u00b7x", "x"),
        ("\u2705 REPORTED \u00b7 \u00b7x", "\u00b7x"),
        ("\u2705 UPDATE \u00b7\u00b7x", "\u00b7x"),
        ("\u2705 RUMOR\u00b7\u2705 RUMOR\u00b7x", "x"),
        ("OFFICIAL \u00b7x", "OFFICIAL \u00b7x"),  # a bare word has no emoji to glue to
        ("OFFICIAL\u00b7x", "OFFICIAL\u00b7x"),
        ("\u2705 OFFICIAL:x", ":x"),
    ],
)
def test_a_dot_glued_to_the_next_word_goes_after_emoji_and_word_and_only_there(text, expected):
    assert _ai_line(text) == expected


# The headline title: cap, fallback, strip after the cut.


def _title(title, trust="community"):
    return _shown_title(title, trust)


def test_a_flattened_title_is_read_only_up_to_two_thousand_characters():
    # 1,950 characters of look-alike labels, then 100 of text: only 50 of those 100 are inside
    # the cap, and that is all that is left to show.
    title = "\u2705 OFFICIAL \u00b7 " * 150 + "x" * 100
    assert _title(title) == "x" * 50
    # A real title just under the cap is read whole.
    assert _title("\u2705 OFFICIAL \u00b7 " + "x" * 1980).startswith("x" * 100)
    assert _title("\u2705 OFFICIAL \u00b7 " + "x" * 1980).endswith("\u2026")
    # Nothing after the cap can show, even a plain title.
    assert _title("a" * 1999 + "b" * 1000) == "a" * 299 + "\u2026"


def test_a_title_that_is_labels_all_the_way_through_the_cap_is_never_empty_or_just_an_ellipsis():
    # Real text only after 2,000 characters of labels: the fallback shows what is inside the cap.
    look_alike = _title("\u2705 OFFICIAL \u00b7 " * 200 + "free codes")
    assert look_alike.startswith("OFFICIAL \u00b7 OFFICIAL")
    assert "\u2705" not in look_alike
    bot = _title("\U0001f7e2 OFFICIAL \u00b7 " * 200 + "free codes")
    for shown in (look_alike, bot):
        assert shown.replace("\u2026", "").strip() != "", repr(shown)
        assert discord_len(shown) <= 300
    assert "\U0001f7e2" not in bot


@pytest.mark.parametrize("k", range(270, 300))
def test_the_cut_cannot_complete_a_marker_the_first_stripping_did_not_see(k):
    # "OFFICIALLY" is not the word; cut after the L (with the ellipsis added) it would be.
    for tail in ("\U0001f7e2 OFFICIALLY x", "\U0001f7e1 REPORTEDLY x", "\U0001f534 RUMORS x"):
        shown = _title("a" * k + " " + tail + " " + "z" * 40)
        assert discord_len(shown) <= 300
        assert _reference_drop(shown) == shown, repr(shown)


def test_the_title_cut_inside_a_marker_loses_the_marker_not_the_whole_title():
    # Cut right after "OFFICIAL" the marker is whole, so it goes, and the text in front stays.
    for k in range(280, 292):
        shown = _title("a" * k + " \U0001f7e2 OFFICIALLY x")
        assert shown.startswith("a" * min(k, 280)), repr(shown)
        assert _reference_drop(shown) == shown
    cut_after_the_word = None
    for k in range(270, 300):
        shown = _title("a" * k + " \U0001f7e2 OFFICIALLY x")
        if shown.endswith("OFFICIAL\u2026") and "\U0001f7e2" in shown:
            cut_after_the_word = shown
    assert cut_after_the_word is None


def test_stripping_after_the_cut_only_ever_shortens_the_title():
    rng = random.Random(67)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [
        "\U0001f7e2 ",
        "OFFICIAL",
        "OFFICIALLY ",
        "x",
        " ",
        "\u2705 ",
        "\u00b7",
        "yy ",
        "UPDATE: ",
    ]
    for _ in range(3000):
        title = "".join(rng.choice(pool) for _ in range(rng.randint(60, 200)))
        shown = _headline_title(title)
        assert discord_len(shown) <= 300
        flat = " ".join(title.split())[:2000]
        assert len(shown) <= max(len(flat), 1) + 1


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("\U0001f7e2 OFFICIAL", "OFFICIAL"),
        ("\U0001f7e2 OFFICIAL \u00b7 \U0001f534 RUMOR", "OFFICIAL \u00b7 RUMOR"),
        ("\U0001f7e2\ufe0f\U0001f3fd OFFICIAL", "OFFICIAL"),
        (
            "\U0001f7e2 \U0001f7e1 \U0001f534 \U0001f501",
            "\U0001f7e2 \U0001f7e1 \U0001f534 \U0001f501",
        ),
        ("\u2705 OFFICIAL", "OFFICIAL"),  # a look-alike emoji goes with the bot's own
        (
            "\u2705\ufe0f \U0001f535 \u2b1c OFFICIAL \u00b7 \U0001f504 UPDATE",
            "OFFICIAL \u00b7 UPDATE",
        ),
        ("\u2611\U0001f3fd \u2714\u0301 RUMOR", "RUMOR"),
        ("\u2605 OFFICIAL", "\u2605 OFFICIAL"),  # a star is no look-alike
        ("  \U0001f7e2   OFFICIAL  ", "OFFICIAL"),
        ("\U0001f7e2\u3164", "\u3164"),
        ("\U0001f7e2", "\U0001f7e2"),
        ("OFFICIAL", "OFFICIAL"),
        ("\u0301\ufe0f", "\u0301\ufe0f"),
        (ZWSP, ZWSP),
    ],
)
def test_a_title_with_nothing_but_labels_falls_back_without_the_bots_own_emoji(title, shown):
    assert _title(title) == shown


def test_the_fallback_never_shows_the_bots_emoji_next_to_a_label_word():
    rng = random.Random(68)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [
        *BOT_EMOJI.values(),
        *BOT_EMOJI,
        " ",
        "\u00b7",
        ":",
        "\ufe0f",
        "\U0001f3fd",
        ZWSP,
        "\u2800",
    ]
    for _ in range(40_000):
        title = "".join(rng.choice(pool) for _ in range(rng.randint(1, 10)))
        shown = _headline_title(title)
        single = " ".join(shown.split())
        assert _reference_drop(single) == single, repr((title, shown))


def test_a_label_only_official_item_still_gets_its_one_real_marker():
    line = _headline_line(_headline("\U0001f7e2 OFFICIAL", "official"))
    assert line == f"\u2022 \U0001f7e2 OFFICIAL \u00b7 OFFICIAL {EM_DASH} <{URL}>"


def test_without_bot_emoji_takes_the_riders_with_each_emoji_and_collapses_the_spaces():
    assert _without_bot_emoji("a \U0001f7e2\ufe0f\U0001f3fd\u0301 b") == "a b"
    assert _without_bot_emoji("\U0001f7e2\U0001f7e1x") == "x"
    assert _without_bot_emoji("x\U0001f534") == "x"
    assert _without_bot_emoji("\u2705 x") == "x"
    assert _without_bot_emoji("a \U0001f535\ufe0f\U0001f3fd b \u2611 c \u2714\u0301 d") == "a b c d"
    assert _without_bot_emoji("\u2b1b\u2b1c\U0001f7e0\U0001f7eb\u26aa\u26ab x") == "x"
    assert _without_bot_emoji("\U0001f501\U0001f502\U0001f503\U0001f504 x") == "x"
    assert _without_bot_emoji("\u2605 \U0001f600 \u2b1d x") == "\u2605 \U0001f600 \u2b1d x"
    assert _without_bot_emoji("") == ""


def test_a_title_the_list_already_shows_is_stable_when_it_goes_through_again():
    rng = random.Random(69)  # noqa: S311 (a reproducible sequence, not a secret)
    from newsbot.bot.format import _strip_labels

    pool = ["\U0001f7e2", "\U0001f7e1", "\u2705", " ", "OFFICIAL", "UPDATE", "\u00b7", ":",
            "\u2800", "\ufe0f", ZWSP, "\u0301", "x", "\U0001f3fd", "-", "\u041e", "yy"]  # fmt: skip
    for _ in range(60_000):
        title = "".join(rng.choice(pool) for _ in range(rng.randint(1, 14)))
        flat = " ".join(title.split())
        if not _strip_labels(flat):
            continue  # the fallback keeps look-alikes on purpose, so it is not stable
        once = _headline_title(title)
        assert _headline_title(once) == once, repr(title)


# Speed and size.


def _heavy(title):
    return _timed(_headline_line, _headline(title))


@pytest.mark.parametrize(
    "title",
    [
        "a " * 2_500_000,
        "a" * 5_000_000,
        "\u2705 OFFICIAL \u00b7 " * 350_000,
        "\u2800\u200b" * 2_500_000,
        "\U0001f7e2 " * 1_000_000 + "OFFICIAL " * 500_000,
    ],
    ids=["five million words", "one word", "labels", "blank and hidden", "nested"],
)
def test_a_five_million_character_title_costs_well_under_a_second(title):
    _, took = _heavy(title)
    assert took < 1.5, f"{took:.2f} s for {len(title)} characters"


def test_a_five_million_character_title_does_not_hold_hundreds_of_megabytes():
    import tracemalloc

    tracemalloc.start()
    try:
        _heavy("\U0001f7e2 " * 1_000_000 + "OFFICIAL " * 500_000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 300_000_000


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("nested with blank glyphs", "x " + "\U0001f7e2 " * 50_000 + "\u2800OFFICIAL " * 22_000),
        ("nested with confusables", "x " + "\U0001f7e2 " * 50_000 + "\u041eFFICIAL " * 22_000),
        ("nested with hangul fillers", "x " + "\U0001f7e2 " * 50_000 + "\u3164OFFICIAL " * 22_000),
        ("selectors in every position", "\u2705\ufe0f \u2800" * 60_000 + "x"),
        ("a blank run in front", "\u2800" * 200_000 + "\u2705 OFFICIAL"),
        ("blank glyph separators", "\u2705\u2800OFFICIAL\u2800\u00b7\u2800" * 10_000 + "x"),
        ("alternating blank and filler", "\u2800\u3164" * 100_000 + "\u2705 OFFICIAL tail"),
        ("riders and hidden mixed", ("\U0001f7e2\u0301\ufe0f\u200b " + "OFFICIAL ") * 20_000),
        ("a million invisibles inside one word", "x \U0001f7e2 OFF" + ZWSP * 1_000_000 + "ICIAL y"),
    ],
)
def test_ai_text_of_a_few_hundred_thousand_characters_is_not_slow_on_any_shape(name, text):
    out, took = _timed(_ai_line, text)
    assert took < 2.0, f"{name}: {took:.1f} s for {len(text)} characters"


def test_a_million_and_a_half_characters_of_nested_markers_stays_around_two_seconds():
    # Haiku can write about sixteen thousand tokens, nowhere near this; it is a ceiling check.
    text = "x " + "\U0001f7e2 " * 250_000 + "\u2800OFFICIAL " * 110_000
    _, took = _timed(_ai_line, text)
    assert took < 6.0, f"{took:.1f} s for {len(text)} characters"


def test_the_strip_loop_takes_few_rounds_whatever_the_shape():
    import newsbot.bot.format as current

    calls = {"n": 0}
    original = current._strip_leading_labels

    def counted(text):
        calls["n"] += 1
        return original(text)

    rng = random.Random(70)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = ["\U0001f7e2", "\U0001f7e1", "\u2705", " ", "OFFICIAL", "REPORTED", "UPDATE", "RUMOR",
            "\u00b7", ":", "\u2800", "\u3164", "\ufe0f", ZWSP, "\u0301", "\u041e", "x",
            "\U0001f3fd", "-"]  # fmt: skip
    most = 0
    current._strip_leading_labels = counted
    try:
        for _ in range(60_000):
            text = " ".join("".join(rng.choice(pool) for _ in range(rng.randint(1, 14))).split())
            calls["n"] = 0
            _without_labels(text)
            most = max(most, calls["n"])
    finally:
        current._strip_leading_labels = original
    assert most <= 4, f"{most} rounds of the leading strip for one short text"


# --- ruling C and D: spaces before the dot, and the fallback without any look-alike emoji ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("x \U0001f7e2 OFFICIAL \u2800\u00b7 y", "x y"),
        ("x \U0001f7e2 OFFICIAL \u3164 \u00b7 y", "x y"),
        ("x \U0001f7e2 OFFICIAL\u2800 \u2800\u00b7y", "x y"),
        ("x \U0001f7e2 OFFICIAL \u200b\u2800\u200d\u00b7\u200b y", "x y"),
        ("x \U0001f7e2 OFFICIAL \u2800 y", "x \u2800 y"),  # no dot: the blank glyph stays
        ("x \U0001f7e2 OFFICIAL \u2800", "x \u2800"),
        ("x \U0001f7e2 OFFICIAL \u2800\u00b7", "x"),
        ("x \U0001f7e2 OFFICIAL \u2800\u00b7\u00b7 y", "x \u00b7 y"),  # only the first dot
        ("x \U0001f7e2 OFFICIAL \u2800: y", "x \u2800: y"),  # a colon is not the dot
    ],
)
def test_mid_line_any_run_of_spaces_and_blank_glyphs_before_the_dot_goes_with_the_marker(
    text, expected
):
    assert _ai_line(text) == expected
    assert _ai_line(text) == _reference_drop(" ".join(text.split()))


def test_a_look_alike_only_headline_shows_the_word_without_the_emoji():
    assert _title("\u2705 OFFICIAL") == "OFFICIAL"
    line = _headline_line(_headline("\u2705 OFFICIAL", "official"))
    assert line == f"\u2022 \U0001f7e2 OFFICIAL \u00b7 OFFICIAL {EM_DASH} <{URL}>"
    assert (
        _title("\u2705\u2705 \U0001f7e2 REPORTED \u00b7 \U0001f535 RUMOR")
        == "REPORTED \u00b7 RUMOR"
    )


def test_the_fallback_never_shows_any_look_alike_emoji_when_something_else_is_left():
    from newsbot.bot.format import _strip_labels

    rng = random.Random(71)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [
        *LOOKALIKES,
        *BOT_EMOJI.values(),
        *BOT_EMOJI,
        " ",
        "\u00b7",
        ":",
        "\ufe0f",
        "\U0001f3fd",
    ]
    checked = 0
    for _ in range(30_000):
        title = "".join(rng.choice(pool) for _ in range(rng.randint(1, 10)))
        flat = " ".join(title.split())
        if _strip_labels(flat) or not _without_bot_emoji(flat):
            continue  # stripped normally, or nothing but emoji (then it is shown as it came)
        checked += 1
        shown = _headline_title(title)
        assert not any(ch in shown for ch in LOOKALIKES), repr((title, shown))
    assert checked > 2000
