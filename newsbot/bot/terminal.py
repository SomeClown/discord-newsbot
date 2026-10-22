"""The vocabulary of the terminal look: a prompt, a cursor, a handful of labels, one green.

Discord won't let a plain message pick a color or a font, so the green-screen feel of the
SHiFT alerts is a costume: an inline-code chip here, a `> ` and a block there, and a
cursor on the end. Everything the costume is made of lives in this one file so the
renderers in `format.py` can't drift apart on how a prompt is spelled, and so changing
the look later means editing a word here instead of hunting through string literals.

The rule this module exists to enforce: no user, config or scraped text ever goes inside
a chip. A chip is the one place Discord draws its own monospace box, which makes it the
last place I want a stranger's `@everyone` or a link, so chips hold only the fixed words
below. `prompt()` refuses a backtick for the same reason (a backtick in the text would
close the chip early and let the rest of the string out); if it ever raises, somebody
passed it something that wasn't a fixed string, and the raise is the bug report.

The cursor is a single block character and it does not blink. Discord can't animate text,
and I'd rather have a cursor that holds still than a promise of one that doesn't. It
never will.

This module imports nothing, on purpose, so anything can use it without a cycle.
"""

from __future__ import annotations

# The cursor: a solid block (U+258C, left half block), one UTF-16 unit. It also doubles as
# the layout marker for stored alert headers (see `is_terminal_header`).
CURSOR = "▌"

# The green from the PacketQueue site, as an embed stripe color.
SHIFT_GREEN = 0x3FE37F

# The label words, as chips: `reward`, `expires`, and so on. `EXPIRED` is what `expires`
# turns into when a date has already passed.
REWARD = "reward"
EXPIRES = "expires"
EXPIRED = "expired"
VIA = "via"
SRC = "src"
FIRST_SEEN = "first seen"

# The group headings on `/shift codes` and `/shift history`, as `[golden keys]` chips.
GROUP_GOLDEN_KEYS = "golden keys"
GROUP_COSMETICS = "cosmetics"
GROUP_PERMANENT = "permanent"
GROUP_OTHER = "other"

# Embed titles are plain text, not chips: a title can't hold inline code. They're fixed
# strings, and deliberately don't go through `esc()`, which would turn the "> " into "\> ".
PAGE_TITLE_CURRENT = f"> current SHiFT codes {CURSOR}"
PAGE_TITLE_HISTORY = f"> SHiFT code history {CURSOR}"


def prompt(text: str) -> str:
    """A prompt chip: `` `> text ▌` ``.

    Raises ValueError if `text` has a backtick in it, because it would end the chip early.
    Only fixed strings belong here; the guard is a tripwire for anyone who forgets that.
    """
    if "`" in text:
        raise ValueError(f"a prompt can't hold a backtick: {text!r}")
    return f"`> {text} {CURSOR}`"


def label(word: str) -> str:
    """A label chip: `` `word` ``. Same rules as `prompt`, minus the guard; pass the words above."""
    return f"`{word}`"


def is_terminal_header(header: str) -> bool:
    """Whether a stored alert header was written in the terminal look rather than v3.1's.

    Every terminal header carries the cursor followed by the chip's closing backtick, and
    no v3.1 header can: those are a closed set of fixed strings (plus a ping), none of
    which has a block character in it. So the header says which layout its message was
    packed for, and an edit can be rendered to match without a database column. Anything
    that doesn't look like a terminal header, garbage included, counts as v3.1.
    """
    return f"{CURSOR}`" in header
