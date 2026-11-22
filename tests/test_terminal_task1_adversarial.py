"""Adversarial round on the v3.2.0 Task 1 seam: the vocabulary and the edit of an old header.

Three things get leaned on here.

1. `prompt`, against every padding and ping form I could think of. (Its old sibling,
   `is_terminal_header`, left the application in v3.5.0 with the v3.1 layout; the test-side
   copy in `terminal_look.py` is what these tests ask now.)
2. `render_alert_edit` with a v3.1 header, or anything that isn't a terminal one: since v3.5.0
   it is edited in today's layout, the header goes back out as stored, and hostile input
   either fits in 2,000 units or raises ValueError.
3. The v3.1.4 packer's `ping_prefix`, nonces and codes still agree with today's. The old
   `format.py` is read out of git (commit 1fcd04b) as a throwaway module; if git or that
   commit isn't around (a shallow clone), that test skips with a reason rather than passing
   by accident.
"""

from __future__ import annotations

import random
import subprocess
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from terminal_look import is_terminal_header

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
# Roundup and follow-up headers from v3.1 were never stored for an edit, but an edit has to
# take any header, so the shapes are here anyway.
V31_OTHER = [
    "**SHiFT codes from a roundup** · r/Borderlands4 · <https://example.com/x>",
    "**SHiFT code confirmed by a second source**",
]


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
    assert is_terminal_header(got)


@pytest.mark.parametrize("text", ["a\nb", "\n", "a\r\nb", "a b", "\t", " ", "@everyone", "▌"])
def test_prompt_with_odd_whitespace_or_pings_keeps_exactly_two_backticks(text):
    got = terminal.prompt(text)
    assert got.count("`") == 2
    assert got.startswith("`> ")
    assert got.endswith(" ▌`")
    assert is_terminal_header(got)


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


# --- an old or unknown header is edited in today's layout ---


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


def test_a_stored_header_is_not_trimmed_or_normalized():
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


def test_a_few_hundred_hostile_edits_either_fit_or_raise_and_keep_the_stored_header():
    from chip_check import fence_bodies

    rng = random.Random(20261022)  # noqa: S311
    ok = err = 0
    for i in range(600):
        header, cands, details, golden_prefix = _random_case(rng)
        got = _outcome(
            render_alert_edit, header, cands, golden_prefix=golden_prefix, details=details, now=NOW
        )
        if got[0] == "err":
            assert got[1] == "ValueError", f"case {i}: {got}"
            err += 1
            continue
        ok += 1
        text = got[1]
        # What it makes fits, starts with the stored header as it was (garbage included),
        # and keeps every code alone in its own plain fence.
        assert discord_len(text) <= 2000, f"case {i}"
        assert text.startswith(header + "\n\n"), f"case {i}: header={header!r}"
        assert fence_bodies(text[len(header) :]) == [c.code for c in cands], f"case {i}"
    # Both outcomes get exercised, or the loop proves little.
    assert ok > 100
    assert err > 5


def test_a_bad_code_raises_a_value_error():
    got = _outcome(lambda: _edit("**New SHiFT code**", [_cand("not-a-code")]))
    assert got[:2] == ("err", "ValueError")
    assert "not a SHiFT code" in got[2]


def test_an_empty_candidate_list_is_just_the_header():
    assert _outcome(lambda: _edit("**New SHiFT code**", [])) == ("ok", "**New SHiFT code**")


def test_the_too_big_error_names_the_size_and_the_codes():
    cands = [_cand(f"AAAA{i % 10}-AAAAA-AAAAA-AAAAA-A{i:04d}", source="s" * 100) for i in range(30)]
    got = _outcome(lambda: _edit("**New SHiFT codes**", cands))
    assert got[:2] == ("err", "ValueError")
    assert "exceeds 2000 UTF-16 units" in got[2]
    assert "even without links" in got[2]
    assert cands[0].code in got[2]


def test_details_given_without_details_for_a_code_is_just_the_bare_block():
    got = _edit("**New SHiFT code**", [_cand()], details={"OTHER-CODE": _row("OTHER-CODE")})
    assert got == _edit("**New SHiFT code**", [_cand()], details={})
    assert got.startswith(f"**New SHiFT code**\n\n```\n{CODE}\n```\n")
    assert "`reward`" not in got


# --- pings, nonces and golden prefix are untouched ---


@pytest.mark.parametrize("ping", PINGS)
@pytest.mark.parametrize("body", V31_BODIES)
def test_a_v31_header_keeps_its_ping_text_exactly_for_the_edit_to_repeat(ping, body):
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


def test_golden_prefix_false_never_prefixes_even_a_golden_candidate_under_a_v31_header():
    got = _edit("**New Golden Key code**", [_cand(golden=True)], golden_prefix=False)
    assert "Golden Key:" not in got.split("\n\n", 1)[1]


def test_golden_prefix_true_prefixes_only_golden_candidates_under_a_v31_header():
    got = _edit(
        "**New SHiFT codes**",
        [_cand(golden=True), _cand("BBBB2-BBBBB-BBBBB-BBBBB-BBBBB")],
        golden_prefix=True,
    )
    assert got.count("Golden Key: r/Borderlands4") == 1


def test_nonces_and_ping_prefix_from_the_v314_packer_are_unchanged(old):
    # The nonce is what makes a retried send safe to repeat, so a changed look must not
    # change it: today's packer and v3.1.4's agree on nonce, ping_prefix and codes.
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
    # and a closing backtick apart, or a source's name could make a header look like a
    # terminal one to anything that asks.
    header = f"**SHiFT codes from a roundup** · {esc(name)} · <https://example.com/x>"
    assert is_terminal_header(header) is False
