"""A yardstick for "is this stored alert header in the terminal look?".

`terminal.is_terminal_header` used to live in the application, where it picked between the
v3.1 layout and the terminal one when an alert was edited. The v3.1 layout is gone, so the
function went with it, but plenty of tests still want to ask the question of today's output
("every header `render_code_alerts` makes is in the terminal look"). This is the old logic,
word for word, kept where only tests can see it.
"""

from __future__ import annotations

import types

from newsbot.bot.terminal import CURSOR


def is_terminal_header(header: str) -> bool:
    """True when the header carries the cursor followed by the chip's closing backtick."""
    return f"{CURSOR}`" in header


def give_old_format_the_marker_check(old):
    """Hand a throwaway copy of an old `format.py` the `terminal.is_terminal_header` it calls.

    Differential tests run `format.py` from before v3.5.0 under another name, and that copy
    asks `terminal` which layout a stored header is in. The real `terminal` no longer
    answers, so the copy gets a stand-in with everything the real one has plus the check.
    Only the copy's own `terminal` name is swapped; nothing global changes.
    """
    from newsbot.bot import terminal as real

    stand_in = types.ModuleType("terminal_with_marker_check")
    stand_in.__dict__.update(vars(real))
    stand_in.is_terminal_header = is_terminal_header
    old.terminal = stand_in
    return old
