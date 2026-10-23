"""Adversarial round on the v3.2.0 Task 1 seam: the vocabulary and the classic edit dispatch.

Three things get leaned on here.

1. `is_terminal_header` and `prompt`, against every lookalike, padding and ping form I could
   think of. The contract is a plain substring test (`"▌`" in header`), so the lookalikes
   below are all supposed to come out false and the hand-made ones true.
2. `render_alert_edit` with a v3.1 header (or anything that isn't a terminal one) against
   the v3.1.4 code itself. The pre-change `format.py` is read out of git (commit 1fcd04b, the
   base of this branch) and loaded as a throwaway module, so "byte-identical to before" is
   checked against the real before and not against my memory of it. If git or that commit
   isn't around (a shallow clone, a source tarball), those tests skip with a reason rather
   than pass by accident; the literal-string pins in `test_format_classic_edits.py` still
   run everywhere.
3. The v3.1.4 promise, end to end: pack a batch with the *old* packer under the *old*
   reserve (asserted to be 364 + 2L, hard-coded), then edit every message it made with the
   worst details the config allows, and check the result still fits with its links.

If a later task changes a shared helper (`esc`, `_detail_text`, ...) so classic output
moves, the differential tests are the ones that will say so.
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from newsbot.bot import terminal
from newsbot.bot.format import discord_len, esc, render_alert_edit
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.edits import _ping_for
from newsbot.store.models import CodeDetailsRow

REPO = Path(__file__).resolve().parent.parent
BASE_COMMIT = "1fcd04b"
NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"


# --- the pre-change format.py, as a throwaway module ---


@pytest.fixture(scope="module")
def old():
    """`newsbot/bot/format.py` as of the base commit, loaded under another name."""
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{BASE_COMMIT}:newsbot/bot/format.py"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {BASE_COMMIT} is not available here")
    name = "_newsbot_format_v314_throwaway"
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module  # dataclasses look their module up by name
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


def _both(old, header, cands, details, golden_prefix):
    """The v3.1.4 render and today's, as comparable outcomes."""
    want = _outcome(
        old.render_alert_edit,
        header,
        cands,
        golden_prefix=golden_prefix,
        details=details,
        now=NOW,
    )
    got = _outcome(
        render_alert_edit, header, cands, golden_prefix=golden_prefix, details=details, now=NOW
    )
    return want, got


def _outcome(fn, *args, **kwargs):
    """Return value, or the exception's type and text; both sides must agree on either."""
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("err", type(exc).__name__, str(exc))


# --- headers ---

PINGS = ["", "@everyone ", "@here ", "<@&123> ", "<@&18446744073709551615> "]
V31_BODIES = [
    "**New SHiFT code**",
    "**New SHiFT codes**",
    "**New Golden Key code**",
    "**New Golden Key codes**",
    "**[TEST] New SHiFT code**",
    "**[TEST] New SHiFT codes**",
    "**[TEST] New Golden Key code**",
    "**[TEST] New Golden Key codes**",
    "**(continued)**",
    "**[TEST] (continued)**",
]
V31_HEADERS = [ping + body for ping in PINGS for body in V31_BODIES]
# Roundup and follow-up headers from v3.1 were never stored for an edit, but "unknown means
# classic" has to hold for the shapes anyway.
V31_OTHER = [
    "**SHiFT codes from a roundup** · r/Borderlands4 · <https://example.com/x>",
    "**SHiFT code confirmed by a second source**",
]


@pytest.mark.parametrize("header", V31_HEADERS + V31_OTHER)
def test_no_v31_header_in_any_ping_form_is_terminal(header):
    assert terminal.is_terminal_header(header) is False


@pytest.mark.parametrize("ping", PINGS)
def test_a_terminal_header_is_terminal_with_any_ping_form_or_none(ping):
    for text in ("new SHiFT code", "[test] new Golden Key codes", "new SHiFT codes (continued)"):
        assert terminal.is_terminal_header(ping + terminal.prompt(text)) is True


def test_a_ping_stripped_terminal_header_is_still_terminal():
    full = "@everyone " + terminal.prompt("new SHiFT code")
    assert terminal.is_terminal_header(full[len("@everyone ") :]) is True


@pytest.mark.parametrize(
    "header",
    [
        "▌`",  # the bare marker
        "x▌`y",  # in the middle of anything
        "**New SHiFT code** ▌`",  # a v3.1 title wearing the marker is terminal by the rule
        "  `> new SHiFT code ▌`  ",  # padded with spaces
        "\n`> new SHiFT code ▌`\n",  # padded with newlines
        "\t`> new SHiFT code ▌`",
        "`> ▌`",  # empty prompt text
        "`> a ▌` `> b ▌`",
        "@everyone  `> new SHiFT code ▌`",  # two spaces after the ping
        "`> new SHiFT code ▌`@everyone ",  # ping in the wrong place
        "@everyone`> new SHiFT code ▌`",  # no space after the ping
        "▌`▌`",
        "`▌`",  # a backtick on both sides still has ▌ then `
    ],
)
def test_hand_made_headers_with_the_marker_are_terminal(header):
    assert terminal.is_terminal_header(header) is True


@pytest.mark.parametrize(
    "header",
    [
        "",
        " ",
        "\n",
        "@everyone",
        "▌",  # cursor only
        "▌ ",
        "`",
        "``",
        "`▌",  # backtick before the cursor instead of after
        "`> new SHiFT code ▌",  # no closing backtick
        "`> new SHiFT code ▌ `",  # a space between the cursor and the backtick
        "`> new SHiFT code ▌\n`",
        "`> new SHiFT code`",  # a chip with no cursor
        "`> x`",
        "> new SHiFT code ▌",  # no backticks at all
        "@everyone `> new SHiFT code`",
        "▌'",
        "▌´",  # acute accent
        "▌ˋ",  # modifier letter grave accent
        "▌˴",  # modifier letter middle grave accent
        "▌`̀"[:1],  # cursor alone, written the long way
        "▌̀",  # combining grave
        "▌｀",  # fullwidth grave accent
        "▌‘",  # left single quote
        "▌‛",
        "▌​`",  # zero-width space between
        "▌⁠`",  # word joiner between
        "▌︎`",  # variation selector between
        "▌‍`",  # zero-width joiner between
        "▌‮`",  # right-to-left override between
        "▐`",  # right half block
        "▍`",  # left three-eighths block
        "▎`",
        "█`",  # full block
        "▀`",
        "▄`",
        "▐▌`"[:1] + "`",
        "▂`",
        "|`",
        "│`",
        "_`",
        "▋`",  # left five-eighths block
    ],
)
def test_lookalikes_and_half_markers_are_not_terminal(header):
    assert terminal.is_terminal_header(header) is False


def test_a_decomposed_form_does_not_sneak_in():
    # The cursor has no decomposition, so NFC/NFD of a real terminal header is itself.
    import unicodedata

    header = "@everyone " + terminal.prompt("new SHiFT code")
    for form in ("NFC", "NFD", "NFKC", "NFKD"):
        assert terminal.is_terminal_header(unicodedata.normalize(form, header)) is True


def test_is_terminal_header_is_a_pure_substring_rule_on_a_huge_header():
    assert terminal.is_terminal_header("a" * 100_000) is False
    assert terminal.is_terminal_header("a" * 100_000 + "▌`") is True


# --- prompt ---


@pytest.mark.parametrize(
    "text",
    ["`", "``", "```", "a`", "`a", "a`b", " ` ", "x\n`\ny", "`> x ▌`", "▌`", "a" * 500 + "`"],
)
def test_prompt_refuses_a_backtick_wherever_it_sits(text):
    with pytest.raises(ValueError, match="backtick"):
        terminal.prompt(text)


def test_prompt_error_names_the_offender_but_does_not_return_a_chip():
    with pytest.raises(ValueError) as caught:
        terminal.prompt("a`b")
    assert "a`b" in str(caught.value)


def test_prompt_shape_with_ordinary_text():
    assert terminal.prompt("new SHiFT code") == "`> new SHiFT code ▌`"


def test_prompt_of_empty_text_is_still_one_well_formed_chip():
    got = terminal.prompt("")
    assert got == "`>  ▌`"
    assert got.count("`") == 2
    assert terminal.is_terminal_header(got)


@pytest.mark.parametrize("text", ["a\nb", "\n", "a\r\nb", "a b", "\t", " ", "@everyone", "▌"])
def test_prompt_with_odd_whitespace_or_pings_keeps_exactly_two_backticks(text):
    got = terminal.prompt(text)
    assert got.count("`") == 2
    assert got.startswith("`> ")
    assert got.endswith(" ▌`")
    assert terminal.is_terminal_header(got)


@pytest.mark.parametrize("text", ["｀", "ˋ", "̀", "‘"])
def test_prompt_lets_backtick_lookalikes_through_because_discord_does_not_treat_them_as_one(text):
    # Pinned so a future "improvement" doesn't start rejecting fixed strings by accident.
    assert terminal.prompt(text) == f"`> {text} ▌`"


@pytest.mark.parametrize("bad", [None, 5, b"x"])
def test_prompt_of_a_non_string_raises_rather_than_returning_a_chip(bad):
    with pytest.raises((TypeError, ValueError)):
        terminal.prompt(bad)


def test_every_fixed_word_makes_a_safe_prompt_and_label():
    words = [
        terminal.REWARD,
        terminal.EXPIRES,
        terminal.EXPIRED,
        terminal.VIA,
        terminal.SRC,
        terminal.FIRST_SEEN,
        terminal.GROUP_GOLDEN_KEYS,
        terminal.GROUP_COSMETICS,
        terminal.GROUP_PERMANENT,
        terminal.GROUP_OTHER,
    ]
    for word in words:
        assert "`" not in word
        assert terminal.prompt(word).count("`") == 2
        assert terminal.label(word) == f"`{word}`"


def test_the_page_titles_are_plain_text_with_no_chip():
    for title in (terminal.PAGE_TITLE_CURRENT, terminal.PAGE_TITLE_HISTORY):
        assert "`" not in title
        assert title.endswith(terminal.CURSOR)
        assert title.startswith("> ")


def test_the_cursor_is_one_utf16_unit_and_the_green_is_the_site_green():
    assert terminal.CURSOR == "▌"
    assert discord_len(terminal.CURSOR) == 1
    assert terminal.SHIFT_GREEN == 0x3FE37F


def test_terminal_imports_nothing_from_the_app():
    # "This module imports nothing, on purpose": a cycle-proof leaf.
    source = Path(terminal.__file__).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["from __future__ import annotations"]


# --- the dispatch picks classic for every v3.1 header, and for unknown ones ---


def _cand(code=CODE, *, golden=False, source="r/Borderlands4", url="https://example.com/post"):
    return CodeCandidate(code=code, golden=golden, source_name=source, item_url=url, fresh=True)


def _row(code=CODE, **kwargs):
    defaults = dict(code=code, clock_start=NOW, updated_at=NOW)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _edit(header, candidates, *, details=None, golden_prefix=False):
    return render_alert_edit(
        header, candidates, golden_prefix=golden_prefix, details=details, now=NOW
    )


GARBAGE_HEADERS = [
    "",
    " ",
    "x",
    "@everyone",
    "▌",
    "`> x`",
    "`> new SHiFT code ▌",
    "`▌",
    "▌​`",
    "**New SHiFT code**▌",
    "New SHiFT code",
    "**New SHiFT code** `",
    "▐`",
    "**New SHiFT code**\n\nextra paragraph",
]


def _classic_reference_case():
    """A case whose classic and terminal renders differ (a details line), so dispatch shows."""
    row = _row(reward="3 Golden Keys", reward_from="mentalmars")
    return [_cand()], {CODE: row}


@pytest.mark.parametrize("header", V31_HEADERS + V31_OTHER + GARBAGE_HEADERS)
def test_every_non_terminal_header_gets_the_classic_details_wording(header):
    cands, details = _classic_reference_case()
    got = _edit(header, cands, details=details)
    assert got == (
        f"{header}\n\n```\n{CODE}\n```\n3 Golden Keys · details via mentalmars\n"
        f"r/Borderlands4 · <https://example.com/post>"
    )


@pytest.mark.parametrize("header", V31_HEADERS + V31_OTHER + GARBAGE_HEADERS)
def test_every_non_terminal_header_is_byte_identical_to_the_v314_render(old, header):
    cands = [_cand(golden=True), _cand("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB", source="s" * 100)]
    details = {
        CODE: _row(
            reward="3 Golden Keys",
            reward_from="mentalmars",
            expires_on=date(2026, 10, 14),
            expires_from="Gearbox",
        )
    }
    for golden_prefix in (False, True):
        want, got = _both(old, header, cands, details, golden_prefix)
        assert got == want


def test_a_header_is_not_trimmed_or_normalized_on_the_classic_path():
    header = "  @everyone **New SHiFT code**  \n"
    got = _edit(header, [_cand()])
    assert got.startswith(header + "\n\n```\n")


# The pools below are meant to be nasty: markdown, mentions, timestamps, astral emoji,
# bidi controls, backticks and the new layout's own vocabulary.
_REWARDS = [
    "",
    " ",
    "3 Golden Keys",
    "*" * 400,
    "@everyone " * 40,
    "<@&1>" * 80,
    "<t:1:R>" * 60,
    "\U0001f600" * 200,
    "\\" * 400,
    "`" * 50,
    "`reward` ▌` > x",
    "line one\nline two\n> quoted",
    "‮" + "x" * 50,
    "x" * 1000,
    "||spoiler||" * 30,
    "[a](https://evil.example)",
]
_NAMES = [
    "",
    "mentalmars",
    "Gearbox",
    "n" * 100,
    "*" * 100,
    "_" * 100,
    "`" * 100,
    "@everyone" * 11,
    "<t:1:R>" * 14,
    "x" * 150,
    "‮" * 50,
    "▌`" * 40,
    "https://evil.example/" * 4,
    "a b" * 30,
]
_DATES = [
    None,
    date(2026, 10, 14),
    date(2026, 9, 30),
    date(2026, 10, 9),
    date(2026, 1, 1),
    date(1970, 1, 1),
    date(2099, 12, 31),
    date(2100, 1, 1),
    date(2262, 4, 11),
    date(9999, 12, 31),
]
_URLS = [
    "https://example.com/post",
    "https://example.com/" + "p" * 300,
    "https://example.com/a b",
    "javascript:alert(1)",
    "",
    "https://example.com/>(x)",
    "http://[::1]/",
    "https://example.com/" + "‮" * 20,
]
_SOURCES = ["r/Borderlands4", "s" * 100, "*" * 100, "@everyone", "", "`" * 10, "▌`"]


def _code(rng: random.Random) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
    return "-".join("".join(rng.choice(alphabet) for _ in range(5)) for _ in range(5))


def _random_case(rng: random.Random):
    count = rng.choice([1, 1, 2, 3, 5, 8])
    codes = [_code(rng) for _ in range(count)]
    cands = [
        _cand(
            c,
            golden=rng.random() < 0.4,
            source=rng.choice(_SOURCES),
            url=rng.choice(_URLS),
        )
        for c in codes
    ]
    details = {}
    for c in codes:
        if rng.random() < 0.7:
            details[c] = _row(
                c,
                reward=rng.choice(_REWARDS),
                reward_from=rng.choice(_NAMES),
                category=rng.choice([None, None, "permanent"]),
                category_from=rng.choice(_NAMES),
                expires_on=rng.choice(_DATES),
                expires_from=rng.choice(_NAMES),
            )
    header = rng.choice(V31_HEADERS + GARBAGE_HEADERS)
    return header, cands, details or rng.choice([None, {}, details]), rng.random() < 0.5


# Fix batch 1 changed two helpers the classic path shares with the new look, on purpose:
# `esc()` now escapes markdown inside URL-shaped text too (a backtick in "https://x.y/`" used to
# slip through and could close a chip or open a fence), and `_detail_text` collapses runs of
# whitespace, newlines included, to single spaces. So byte-identity with v3.1.4 still holds for
# every input where the two can't differ, and for the rest only the properties a reader would
# care about hold. (Round 1 of the fix also made esc() split on "](", so any text holding one
# is in the weak half too.) The differential below is strict for the first kind and weaker
# for the second.
_URL_SHAPED_RE = re.compile(r"<[^: >]+:/[^ >]+>|(?:https?|steam)://[^\s<]+[^<.,:;\"'\]\s]")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")


def _changes_on_purpose(text: str) -> bool:
    """Whether fix batch 1 can make `text` render differently from v3.1.4."""
    text = _BIDI.sub("", text)
    if "](" in text:  # esc() cuts there now and escapes the "]" too, so the backslashes differ
        return True
    if " ".join(text.split()) != text.strip():
        return True
    return any(set("*_~|`\\") & set(m.group(0)) for m in _URL_SHAPED_RE.finditer(text))


def _case_strings(cands, details):
    for c in cands:
        yield c.source_name
    for row in (details or {}).values():
        yield from (row.reward, row.reward_from, row.category_from, row.expires_from)


def _link_count(text: str) -> int:
    return text.count(" · <https://") + text.count(" · <http://")


def test_classic_edits_match_the_v314_render_on_a_few_hundred_hostile_inputs(old):
    from chip_check import chips, fence_bodies

    rng = random.Random(20261022)  # noqa: S311
    strict_ok = strict_err = weak = 0
    for i in range(600):
        header, cands, details, golden_prefix = _random_case(rng)
        want, got = _both(old, header, cands, details, golden_prefix)
        if not any(_changes_on_purpose(t) for t in _case_strings(cands, details)):
            assert got == want, f"case {i}: header={header!r}"
            if want[0] == "ok":
                strict_ok += 1
            else:
                strict_err += 1
            continue
        weak += 1
        # The new render only ever shrinks text, so it can't fail where v3.1.4 fit ...
        if want[0] == "ok":
            assert got[0] == "ok", f"case {i}: a v3.1.4 edit that fit now fails: {got}"
        if got[0] != "ok":
            continue
        text = got[1]
        # ... what it makes still fits, starts with the stored header as it was, and keeps
        # every code alone in its own plain fence (nothing else in there) ...
        assert discord_len(text) <= 2000, f"case {i}"
        assert text.startswith(header), f"case {i}"
        body = text[len(header) :]  # the header is whatever was stored, garbage included
        assert fence_bodies(body) == [c.code for c in cands], f"case {i}"
        # ... a classic edit has no chips, and the hostile text can't conjure one ...
        assert chips(body) == [], f"case {i}: {chips(body)}"
        # ... and a link kept by v3.1.4 is kept now (shorter text can only keep more).
        if want[0] == "ok":
            assert _link_count(text) >= _link_count(want[1]), f"case {i}"
    # Both kinds get exercised, or the comparison is hollow.
    assert strict_ok > 100
    assert strict_err > 5
    assert weak > 50


def test_a_bad_code_raises_the_same_error_on_the_classic_path(old):
    cands = [_cand("not-a-code")]
    want = _outcome(
        lambda: old.render_alert_edit(
            "**New SHiFT code**", cands, golden_prefix=False, details=None, now=NOW
        )
    )
    got = _outcome(lambda: _edit("**New SHiFT code**", cands))
    assert want[0] == "err"
    assert got == want


def test_an_empty_candidate_list_is_just_the_header_on_both_paths(old):
    want = _outcome(
        lambda: old.render_alert_edit(
            "**New SHiFT code**", [], golden_prefix=False, details=None, now=NOW
        )
    )
    assert _outcome(lambda: _edit("**New SHiFT code**", [])) == want == ("ok", "**New SHiFT code**")


def test_the_too_big_error_text_is_unchanged(old):
    cands = [_cand(f"AAAA{i % 10}-AAAAA-AAAAA-AAAAA-A{i:04d}", source="s" * 100) for i in range(30)]
    want = _outcome(
        lambda: old.render_alert_edit(
            "**New SHiFT codes**", cands, golden_prefix=False, details=None, now=NOW
        )
    )
    got = _outcome(lambda: _edit("**New SHiFT codes**", cands))
    assert want[0] == "err"
    assert got == want


def test_details_given_without_details_for_a_code_is_just_the_bare_block():
    got = _edit("**New SHiFT code**", [_cand()], details={"OTHER-CODE": _row("OTHER-CODE")})
    assert (
        got
        == f"**New SHiFT code**\n\n```\n{CODE}\n```\nr/Borderlands4 · <https://example.com/post>"
    )


def test_the_classic_path_does_not_need_a_clock():
    # `now` is a required argument, but the classic details line never reads it. Pinning
    # that nothing blows up if a caller's clock is a plain datetime from any zone.
    from datetime import timedelta, timezone

    other = datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=14)))
    row = _row(expires_on=date(2026, 10, 14))
    a = render_alert_edit(
        "**New SHiFT code**", [_cand()], golden_prefix=False, details={CODE: row}, now=NOW
    )
    b = render_alert_edit(
        "**New SHiFT code**", [_cand()], golden_prefix=False, details={CODE: row}, now=other
    )
    assert a == b


def test_a_classic_edit_never_contains_a_terminal_marker_even_if_the_scraped_text_has_one():
    row = _row(reward="▌` `reward` > x", reward_from="▌`")
    got = _edit("**New SHiFT code**", [_cand(source="▌`")], details={CODE: row})
    # The stored header is what decides the layout next time round, and it is the header
    # alone; the body may hold whatever a source said, but the header string must stay put.
    assert got.startswith("**New SHiFT code**\n\n")
    assert terminal.is_terminal_header(got.split("\n\n", 1)[0]) is False


# --- the v3.1.4 reserve holds, using the old packer for the packing ---

_PACK_NAMES = ["n" * 100, "*" * 100, "_" * 100, "@everyone" * 11, "<t:1:R>" * 14, "\\" * 100]
_PACK_REWARDS = ["*" * 400, "\U0001f600" * 200, "\\" * 400, "@everyone " * 40, "<@&1>" * 80]


def _v314_reserve(longest: int) -> int:
    # History, hard-coded: 364 + 2L, L the longest escaped source name (at most 200).
    return 364 + 2 * longest


@pytest.mark.parametrize(
    ("ping", "ping_mention", "test"),
    [
        (False, None, False),
        (True, None, False),
        (True, "<@&18446744073709551615>", False),
        (True, "<@&123>", True),
        (False, None, True),
    ],
)
@pytest.mark.parametrize("name", _PACK_NAMES)
@pytest.mark.parametrize("mixed", [False, True])
def test_every_message_the_old_packer_made_still_fits_its_worst_classic_edit(
    old, ping, ping_mention, test, name, mixed
):
    longest = discord_len(esc(name[:100]))
    other = "m" * 100
    names = [name, other]
    reserve = old.details_reserve(names)
    assert reserve == _v314_reserve(longest), "the old reserve is not 364 + 2L; test premise broke"

    url = "https://example.com/" + "p" * 200
    n = 14
    cands = [
        _cand(
            f"AAAA{i % 10}-AAAAA-AAAAA-AAAAA-A{i:04d}",
            golden=(i % 2 == 0) if mixed else True,
            source=name,
            url=url,
        )
        for i in range(n)
    ]
    packed = old.render_code_alerts(
        cands,
        ping=ping,
        ping_mention=ping_mention,
        test=test,
        nonce_scope="1:2",
        details=None,
        now=NOW,
        reserve=reserve,
    )
    assert len(packed) >= 2, "the batch has to span messages or the packing isn't being tested"

    by_code = {c.code: c for c in cands}
    for alert in packed:
        msg_cands = [by_code[code] for code in alert.codes]
        worst = {
            c.code: _row(
                c.code,
                reward=_PACK_REWARDS[i % len(_PACK_REWARDS)],
                reward_from=name,
                expires_on=date(2026, 9, 30),
                expires_from=other,
            )
            for i, c in enumerate(msg_cands)
        }
        for header in {alert.header, alert.header.removeprefix(alert.ping_prefix)}:
            got = render_alert_edit(
                header, msg_cands, golden_prefix=alert.golden_prefix, details=worst, now=NOW
            )
            assert discord_len(got) <= 2000, (alert.codes, discord_len(got))
            assert got.count(f"<{url}>") == len(msg_cands), "a link shed that the reserve covers"
            assert got.startswith(header + "\n\n")
            want = old.render_alert_edit(
                header, msg_cands, golden_prefix=alert.golden_prefix, details=worst, now=NOW
            )
            assert got == want


def test_the_edit_is_tight_against_the_old_reserve_not_trivially_short(old):
    # If the worst line were far under the reserve the test above would prove nothing.
    name = "*" * 100
    reserve = old.details_reserve([name, name])
    assert reserve == _v314_reserve(200)
    row = _row(
        reward="*" * 400,
        reward_from=name,
        expires_on=date(2026, 9, 30),
        expires_from="_" * 100,
    )
    got = _edit("**New SHiFT code**", [_cand(source="x")], details={CODE: row})
    line = got.split("\n")[5]
    assert reserve - 4 <= discord_len(line) + 1 <= reserve


# --- pings, nonces and golden prefix are untouched ---


@pytest.mark.parametrize("ping", PINGS)
@pytest.mark.parametrize("body", V31_BODIES)
def test_the_classic_header_keeps_its_ping_text_exactly_for_the_edit_to_repeat(ping, body):
    header = ping + body
    got = _edit(header, [_cand()])
    assert got.split("\n\n", 1)[0] == header


@pytest.mark.parametrize(
    ("stored_ping", "header", "expected"),
    [
        ("everyone", "@everyone **New SHiFT code**", "everyone"),
        ("everyone", "**New SHiFT code**", "none"),
        ("123456789012345678", "<@&123456789012345678> **New SHiFT codes**", "123456789012345678"),
        ("123456789012345678", "**New SHiFT codes**", "none"),
        ("none", "@everyone **New SHiFT code**", "none"),
        ("everyone", "@everyone `> new SHiFT code ▌`", "everyone"),
        ("everyone", "`> new SHiFT code ▌`", "none"),
        ("everyone", " @everyone **New SHiFT code**", "none"),
        ("everyone", "@everyone", "none"),  # no trailing space: not the ping's own text
    ],
)
def test_the_edit_repeats_a_ping_only_while_the_header_still_wears_it(
    stored_ping, header, expected
):
    target = SimpleNamespace(ping=stored_ping, header=header)
    assert _ping_for(target) == expected


def test_golden_prefix_false_never_prefixes_even_a_golden_candidate_on_classic():
    got = _edit("**New Golden Key code**", [_cand(golden=True)], golden_prefix=False)
    assert "Golden Key:" not in got.split("\n\n", 1)[1]


def test_golden_prefix_true_prefixes_only_golden_candidates_on_classic():
    got = _edit(
        "**New SHiFT codes**",
        [_cand(golden=True), _cand("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB")],
        golden_prefix=True,
    )
    assert got.count("Golden Key: r/Borderlands4") == 1


def test_classic_nonces_and_ping_prefix_from_the_untouched_packer_are_unchanged(old):
    # Task 1 doesn't touch the packer, so today's packer and the old one must agree on
    # ping_prefix, header and nonce for the classic shapes it still writes. (Task 2 changes
    # the headers on purpose; at that point this test is the one to retire, not to edit.)
    from newsbot.bot import format as new

    cands = [_cand(golden=True), _cand("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB")]
    for kwargs in (
        dict(ping=True),
        dict(ping=True, ping_mention="<@&123>"),
        dict(ping=False),
        dict(ping=True, test=True),
    ):
        a = new.render_code_alerts(cands, nonce_scope="1:2", **kwargs)
        b = old.render_code_alerts(cands, nonce_scope="1:2", **kwargs)
        assert [x.nonce for x in a] == [x.nonce for x in b]
        assert [x.ping_prefix for x in a] == [x.ping_prefix for x in b]
        assert [x.codes for x in a] == [x.codes for x in b]


@pytest.mark.parametrize("name", ["▌`", "x▌`y", "▌ `", "▌\\`", "`▌`", "▌``", "▌" * 20 + "`" * 20])
def test_an_escaped_source_name_cannot_forge_the_marker_in_a_header(name):
    # Roundup-style headers carry an escaped source name. `esc()` has to keep the cursor
    # and a closing backtick apart, or a source could talk a v3.1 record into the new layout.
    header = f"**SHiFT codes from a roundup** · {esc(name)} · <https://example.com/x>"
    assert terminal.is_terminal_header(header) is False
