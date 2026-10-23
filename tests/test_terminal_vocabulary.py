"""The terminal look's vocabulary (`newsbot.bot.terminal`): its helpers and the layout marker."""

from __future__ import annotations

import pytest

from newsbot.bot import terminal


def test_prompt_wraps_its_text_with_a_prompt_and_the_cursor():
    assert terminal.prompt("new SHiFT code") == "`> new SHiFT code ▌`"


@pytest.mark.parametrize("text", ["a`b", "`", "``", "new `SHiFT` code", "x▌`"])
def test_prompt_refuses_a_backtick(text):
    with pytest.raises(ValueError, match="backtick"):
        terminal.prompt(text)


def test_label_is_a_chip():
    assert terminal.label("reward") == "`reward`"


def test_the_constants_are_the_contract():
    assert terminal.CURSOR == "▌"
    assert len(terminal.CURSOR.encode("utf-16-le")) == 2  # one UTF-16 unit
    assert terminal.SHIFT_GREEN == 0x3FE37F
    assert (
        terminal.REWARD,
        terminal.EXPIRES,
        terminal.EXPIRED,
        terminal.VIA,
        terminal.SRC,
        terminal.FIRST_SEEN,
    ) == ("reward", "expires", "expired", "via", "src", "first seen")
    assert (
        terminal.GROUP_GOLDEN_KEYS,
        terminal.GROUP_COSMETICS,
        terminal.GROUP_PERMANENT,
        terminal.GROUP_OTHER,
    ) == ("golden keys", "cosmetics", "permanent", "other")
    assert terminal.PAGE_TITLE_CURRENT == "> current SHiFT codes ▌"
    assert terminal.PAGE_TITLE_HISTORY == "> SHiFT code history ▌"


_PINGS = ["", "@everyone ", "<@&123> "]
_BODIES = [
    "new SHiFT code",
    "new SHiFT codes",
    "new Golden Key code",
    "new Golden Key codes",
    "[test] new SHiFT code",
    "[test] new Golden Key codes",
    "new SHiFT codes (continued)",
    "[test] new SHiFT codes (continued)",
    "confirmed by a second source",
]


@pytest.mark.parametrize("ping", _PINGS)
@pytest.mark.parametrize("body", _BODIES)
def test_every_new_header_shape_is_terminal(ping, body):
    assert terminal.is_terminal_header(ping + terminal.prompt(body))


@pytest.mark.parametrize("count", ["1 code", "7 codes"])
def test_a_roundup_header_is_terminal(count):
    header = f"{terminal.prompt('SHiFT roundup')} {count} from Some Site · <https://example.com/x>"
    assert terminal.is_terminal_header(header)
    assert terminal.is_terminal_header(terminal.prompt("SHiFT roundup (continued)"))


@pytest.mark.parametrize("ping", _PINGS)
@pytest.mark.parametrize(
    "header",
    [
        "**New SHiFT code**",
        "**New SHiFT codes**",
        "**New Golden Key code**",
        "**New Golden Key codes**",
        "**[TEST] New SHiFT code**",
        "**[TEST] New Golden Key codes**",
    ],
)
def test_every_v31_header_shape_is_not_terminal(ping, header):
    assert not terminal.is_terminal_header(ping + header)


def test_the_v31_continuation_and_roundup_headers_are_not_terminal():
    assert not terminal.is_terminal_header("**(continued)**")
    assert not terminal.is_terminal_header("**SHiFT codes from a roundup** · Some Site")
    assert not terminal.is_terminal_header(
        "**SHiFT codes from a roundup** · Some Site · <https://example.com/x>"
    )


@pytest.mark.parametrize(
    "header",
    [
        "",
        " ",
        "@everyone",
        "▌",  # a cursor with no closing backtick
        "`> x`",  # a chip with no cursor
        "> new SHiFT code ▌",  # a cursor with no backtick after it
        "`▌",  # a backtick *before* the cursor is not the marker
    ],
)
def test_garbage_is_not_terminal(header):
    assert not terminal.is_terminal_header(header)
