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

Item 9 (QA, 2026-09-25) widens the scan past the one literal
`everyone=True` shape, to close off the ways around it that a text match
on that exact shape would miss: `AllowedMentions.all()` (equivalent to
`everyone=True, users=True, roles=True`, and just as capable of pinging),
`everyone=<some variable>` (a non-constant value sidesteps a check that
only ever looked for the literal `True`), and `AllowedMentions(**kwargs)`
(unpacking a dict CODE_RE can't see the shape of at all). None of these
are hypothetical -- they're exactly the three ways someone reasonably
"fixing a bug" could reintroduce a second ping path without ever writing
the literal string this test used to look for.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).parent.parent / "newsbot"


def _allowed_mentions_calls_in_tree(tree: ast.AST) -> list[ast.Call]:
    """Every `AllowedMentions(...)` call node in `tree`, however it's spelled."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "AllowedMentions":
            calls.append(node)
    return calls


def _everyone_true_calls_in_tree(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in _allowed_mentions_calls_in_tree(tree)
        for kw in node.keywords
        if kw.arg == "everyone" and isinstance(kw.value, ast.Constant) and kw.value.value is True
    ]


def _allowed_mentions_all_calls_in_tree(tree: ast.AST) -> list[int]:
    """Every `AllowedMentions.all()` call node in `tree` -- ping-equivalent to `everyone=True`."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "all"):
            continue
        owner = func.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", None)
        if owner_name == "AllowedMentions":
            hits.append(node.lineno)
    return hits


def _everyone_non_constant_calls_in_tree(tree: ast.AST) -> list[int]:
    """`AllowedMentions(everyone=<not a literal True/False>, ...)` calls in `tree`."""
    return [
        node.lineno
        for node in _allowed_mentions_calls_in_tree(tree)
        for kw in node.keywords
        if kw.arg == "everyone" and not isinstance(kw.value, ast.Constant)
    ]


def _allowed_mentions_star_kwargs_calls_in_tree(tree: ast.AST) -> list[int]:
    """`AllowedMentions(**something)` calls in `tree`, opaque to a plain text scan."""
    return [
        node.lineno
        for node in _allowed_mentions_calls_in_tree(tree)
        for kw in node.keywords
        if kw.arg is None
    ]


def _scan_src_root(scanner) -> list[tuple[Path, int]]:
    hits: list[tuple[Path, int]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        hits.extend((path, lineno) for lineno in scanner(tree))
    return hits


def _everyone_true_calls() -> list[tuple[Path, int]]:
    return _scan_src_root(_everyone_true_calls_in_tree)


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


# --- item 9: the three ways around a plain "everyone=True" text search ---


def test_no_allowed_mentions_all_calls_in_newsbot():
    hits = _scan_src_root(_allowed_mentions_all_calls_in_tree)
    assert hits == [], f"AllowedMentions.all() found (ping-equivalent to everyone=True): {hits}"


def test_no_allowed_mentions_everyone_kwarg_is_a_non_constant_value():
    hits = _scan_src_root(_everyone_non_constant_calls_in_tree)
    assert hits == [], f"AllowedMentions(everyone=<non-constant>, ...) found: {hits}"


def test_no_allowed_mentions_call_uses_star_star_kwargs():
    hits = _scan_src_root(_allowed_mentions_star_kwargs_calls_in_tree)
    assert hits == [], f"AllowedMentions(**kwargs) found (opaque to this scan): {hits}"


# --- the detectors above aren't vacuous: each one actually catches its shape ---


def test_detector_catches_allowed_mentions_dot_all():
    tree = ast.parse("m = discord.AllowedMentions.all()")
    assert _allowed_mentions_all_calls_in_tree(tree) == [1]


def test_detector_ignores_dot_all_on_something_that_is_not_allowed_mentions():
    tree = ast.parse("m = SomeOtherClass.all()")
    assert _allowed_mentions_all_calls_in_tree(tree) == []


def test_detector_catches_everyone_kwarg_set_to_a_variable():
    tree = ast.parse("m = AllowedMentions(everyone=flag)")
    assert _everyone_non_constant_calls_in_tree(tree) == [1]


def test_detector_catches_everyone_kwarg_set_to_a_function_call():
    tree = ast.parse("m = AllowedMentions(everyone=should_ping())")
    assert _everyone_non_constant_calls_in_tree(tree) == [1]


def test_detector_does_not_flag_a_literal_everyone_kwarg():
    tree = ast.parse("m = AllowedMentions(everyone=True)")
    assert _everyone_non_constant_calls_in_tree(tree) == []
    tree_false = ast.parse("m = AllowedMentions(everyone=False)")
    assert _everyone_non_constant_calls_in_tree(tree_false) == []


def test_detector_catches_allowed_mentions_star_star_kwargs():
    tree = ast.parse("m = AllowedMentions(**mention_kwargs)")
    assert _allowed_mentions_star_kwargs_calls_in_tree(tree) == [1]


def test_detector_does_not_flag_an_ordinary_allowed_mentions_call():
    tree = ast.parse("m = AllowedMentions(everyone=False, users=False, roles=False)")
    assert _allowed_mentions_star_kwargs_calls_in_tree(tree) == []
    assert _everyone_non_constant_calls_in_tree(tree) == []
    assert _allowed_mentions_all_calls_in_tree(tree) == []
