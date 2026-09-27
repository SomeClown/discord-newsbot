"""The " -- " dash tripwire (CLAUDE.md's dash rule, cleaned up repo-wide in
commit 60709ae). Guards against a future edit reintroducing `--` as a dash
in a `newsbot/` string literal, not the SHiFT code hyphen format
(`XXXXX-XXXXX-...`, single hyphens with no surrounding spaces, untouched by
this rule and by this scan).

Parsed with `ast`, restricted to string literal nodes: a plain text/regex
scan would also trip on a stray `--` in a shell-flag example inside a
comment (comments aren't part of the AST at all, so they're naturally out
of scope here) or a docstring code sample showing `--check-sources` (no
spaces around it, so `" -- "` with spaces on both sides doesn't match that
either, but the AST-only scope keeps this test's intent narrow regardless).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).parent.parent / "newsbot"


def _double_dash_literal_hits() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if " -- " in node.value:
                    hits.append((path, node.lineno, node.value))
    return hits


def test_no_double_dash_used_as_a_dash_in_any_newsbot_string_literal():
    hits = _double_dash_literal_hits()
    assert hits == [], f"' -- ' found in a newsbot/ string literal: {hits}"


def test_detector_catches_a_double_dash_in_a_string_literal():
    tree = ast.parse('x = "a -- b"')
    hits = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and " -- " in node.value
    ]
    assert hits == ["a -- b"]


def test_detector_ignores_a_shift_code_style_single_hyphen_run():
    tree = ast.parse('x = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY"')
    hits = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and " -- " in node.value
    ]
    assert hits == []
