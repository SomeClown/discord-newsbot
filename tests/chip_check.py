"""A test helper (not a test file): find the inline-code chips in a rendered SHiFT message.

The terminal look lives on one rule: chips hold only the fixed words from
`newsbot/bot/terminal.py`, never a reward, a source name or anything else a stranger typed.
A chip is the one place Discord draws its own monospace box, so a stray backtick in
scraped text is the whole attack. `chips()` reads a message the way Discord would and
returns what's inside every inline-code span; `ALLOWED_CHIPS` is everything that's allowed
to be in there. A test that renders hostile text and asserts `set(chips(text)) <=
ALLOWED_CHIPS` is checking the rule from the outside, with its own parser, so a bug in
`esc()` and a bug in this file would have to agree with each other to get past it.
"""

from __future__ import annotations

import re

from newsbot.bot import terminal

_FENCE_RE = re.compile(r"```(.*?)```", re.DOTALL)


def fence_bodies(text: str) -> list[str]:
    """The text inside each ``` fence, without the newline after the opener or before the closer."""
    return [m.group(1).removeprefix("\n").removesuffix("\n") for m in _FENCE_RE.finditer(text)]


def chips(text: str) -> list[str]:
    """The contents of every inline-code span in `text`, fences left out.

    A backslash makes the next character literal, so `` \\` `` doesn't open or close a chip
    (that's what `esc()` turns a scraped backtick into) and `\\\\` followed by a backtick
    does. A span that never closes counts too, as everything after its opener: if one ever
    shows up, the subset check should fail loudly, not look away.
    """
    text = _FENCE_RE.sub("", text)
    found: list[str] = []
    opened_at: int | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            if opened_at is None:
                opened_at = i + 1
            else:
                found.append(text[opened_at:i])
                opened_at = None
        i += 1
    if opened_at is not None:
        found.append(text[opened_at:])
    return found


def _prompts() -> set[str]:
    """Every prompt chip's contents: the alert headers, roundup and follow-up headers."""
    texts = {
        terminal.ROUNDUP_HEADER,
        terminal.ROUNDUP_CONTINUATION,
        terminal.FOLLOWUP_HEADER,
        terminal.FOLLOWUP_CONTINUATION,
    }
    for test in ("", "[test] "):
        texts.add(f"{test}new SHiFT codes (continued)")
        for kind in ("SHiFT", "Golden Key"):
            for suffix in ("", "s"):
                texts.add(f"{test}new {kind} code{suffix}")
    # `prompt()` wraps the text in backticks; a chip's contents are what's between them.
    return {terminal.prompt(text).strip("`") for text in texts}


def _groups() -> set[str]:
    """The `[golden keys]` headings, and their "(continued)" forms."""
    words = (
        terminal.GROUP_GOLDEN_KEYS,
        terminal.GROUP_COSMETICS,
        terminal.GROUP_PERMANENT,
        terminal.GROUP_OTHER,
    )
    return {f"[{word}]" for word in words} | {f"[{word} (continued)]" for word in words}


ALLOWED_CHIPS: frozenset[str] = frozenset(
    {
        terminal.REWARD,
        terminal.EXPIRES,
        terminal.EXPIRED,
        terminal.VIA,
        terminal.SRC,
        terminal.FIRST_SEEN,
    }
    | _prompts()
    | _groups()
)
