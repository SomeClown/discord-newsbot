"""The User-Agent tripwire (self-host plan, task 1): one builder, not three copies.

Same shape as test_mentions_tripwire.py: not testing behavior so much as
guarding against a future edit. `newsbot/useragent.py` is the one place
that's allowed to build a User-Agent string; every collector and client
should import `build_user_agent` from it rather than growing its own
hardcoded copy the way `pipeline/run.py`, `bot/client.py` and
`collectors/rss.py` each used to.

A plain substring search for `"User-Agent"` is enough here (unlike the
mentions tripwire, there's no AST-dodging shape to worry about: a literal
header key has to be spelled exactly that way for httpx to honor it), but
it does need to know its own home is exempt, since this module's
docstring and code both say the words "User-Agent" for good reason.
"""

from __future__ import annotations

from pathlib import Path

_SRC_ROOT = Path(__file__).parent.parent / "newsbot"
_EXEMPT = {_SRC_ROOT / "useragent.py"}


def _user_agent_literal_hits() -> list[tuple[Path, int]]:
    hits = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path in _EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "User-Agent" in line:
                hits.append((path, lineno))
    return hits


def test_no_hardcoded_user_agent_literal_outside_useragent_module():
    hits = _user_agent_literal_hits()
    assert hits == [], f"'User-Agent' found outside newsbot/useragent.py: {hits}"
