"""Render the owner's welcome message and check it will fit.

The welcome text lives in `config.yaml` and has two blanks to fill in:
`{member}` (a mention of whoever just walked in) and `{server}` (the
server's name). This module fills them in, and, more usefully, tells
`load_config` at startup whether a typo is lurking in there, so it fails
loudly on boot instead of posting a literal `{memebr}` to the lounge.

The one rule: no `str.format`. It would happily evaluate `{member.guild}`
and friends, which is a lot of reach for a message template. A regex over a
short whitelist does exactly one job.
"""

from __future__ import annotations

import re

ALLOWED_PLACEHOLDERS = frozenset({"member", "server"})
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

# The longest a real mention can get (a snowflake ID is at most 20 digits),
# and a server name of the maximum 100 characters, all of them emoji, which
# cost two UTF-16 units apiece. Checking the worst case at startup means a
# long welcome can't fail on the one day a member with a long ID joins.
WORST_CASE_MENTION = "<@" + "9" * 20 + ">"
WORST_CASE_SERVER = "😀" * 100


def unknown_placeholders(template: str) -> list[str]:
    """Return the `{...}` names in `template` that aren't allowed, in order, once each."""
    seen: list[str] = []
    for name in _PLACEHOLDER_RE.findall(template):
        if name not in ALLOWED_PLACEHOLDERS and name not in seen:
            seen.append(name)
    return seen


def render_welcome(template: str, *, member_mention: str, server_name: str) -> str:
    """Fill `{member}` and `{server}` in `template`; anything else is left as written.

    `{server}` goes in unescaped. Whoever can edit the server name has
    Manage Server, which is the same trust level as whoever wrote the
    welcome text, so escaping it would only protect the owner from the owner.
    """
    values = {"member": member_mention, "server": server_name}

    def _fill(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_fill, template)


def worst_case_length(template: str) -> int:
    """The rendered length of `template` in UTF-16 units, with the longest possible blanks."""
    rendered = render_welcome(
        template, member_mention=WORST_CASE_MENTION, server_name=WORST_CASE_SERVER
    )
    # Discord counts UTF-16 units. `bot.format.discord_len` does this too, but
    # importing it from here would loop back through `config`, so this is the
    # one-liner again.
    return len(rendered.encode("utf-16-le")) // 2
