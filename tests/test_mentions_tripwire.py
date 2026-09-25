"""The mentions tripwire (plan step 9, A5): `AllowedMentions(everyone=True, ...)` may appear once.

This isn't testing behavior so much as guarding against a future edit --
every send path in this codebase should stay `AllowedMentions.none()`
except the one SHiFT code alert path that's actually allowed to ping. A
unit test on `_PING_EVERYONE` itself (see `test_code_alert_poster.py`)
can't catch someone adding a *second* `everyone=True` call somewhere else;
only scanning the source can.

Parsed with `ast`, not a text/regex scan: a plain substring search for
"everyone=True" would also trip on this docstring, or on any comment
explaining the rule (which every module in this feature has at least one
of). Walking the AST for an actual `AllowedMentions(everyone=True, ...)`
call finds only real code, never prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).parent.parent / "newsbot"


def _everyone_true_calls() -> list[tuple[Path, int]]:
    hits: list[tuple[Path, int]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "AllowedMentions":
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "everyone"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    hits.append((path, node.lineno))
    return hits


def test_everyone_true_appears_exactly_once_in_newsbot():
    hits = _everyone_true_calls()
    assert len(hits) == 1, (
        f"expected exactly one AllowedMentions(everyone=True, ...), found: {hits}"
    )


def test_the_one_everyone_true_call_is_ping_everyone_in_bot_client():
    ((path, lineno),) = _everyone_true_calls()
    assert path.name == "client.py"
    assert path.parent.name == "bot"
    lines = path.read_text().splitlines()
    # _PING_EVERYONE's assignment starts a line or two above the call
    # itself (it's wrapped across lines for line length) -- look a few
    # lines back for the name that owns this call, rather than requiring
    # the assignment and the everyone=True keyword on the same line.
    nearby = "\n".join(lines[max(0, lineno - 4) : lineno])
    assert "_PING_EVERYONE" in nearby


def test_client_default_and_publisher_sends_stay_none():
    source = (_SRC_ROOT / "bot" / "client.py").read_text()
    # DiscordPublisher's own sends (header + embed batches) and the
    # client's constructor-level default must both still be
    # AllowedMentions.none() -- a coarse belt-and-suspenders next to the
    # AST check above, pinned against the specific lines that matter most
    # (a regression here is a scraped headline pinging the whole server).
    assert "allowed_mentions=discord.AllowedMentions.none()" in source
    assert source.count("AllowedMentions.none()") >= 2
