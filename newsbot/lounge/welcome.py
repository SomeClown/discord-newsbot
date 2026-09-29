"""Decide who gets a welcome, then render it and check it will fit.

The welcome text lives in `config.yaml` and has two blanks to fill in:
`{member}` (a mention of whoever just walked in) and `{server}` (the
server's name). This module fills them in, and, more usefully, tells
`load_config` at startup whether a typo is lurking in there, so it fails
loudly on boot instead of posting a literal `{memebr}` to the lounge.

The one rule: no `str.format`. It would happily evaluate `{member.guild}`
and friends, which is a lot of reach for a message template. A regex over a
short whitelist does exactly one job.

The other half is the decision: `welcome_action` says whether a join event
deserves a welcome at all, and `RecentWelcomes` is the bouncer's clipboard
that stops the same person being greeted twice in a day. Both are pure, so
they know nothing about Discord. This module gets imported by `config`, and
dragging the gateway in behind it would be a poor way to start the morning.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

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


def welcome_action(
    *, in_guild: bool, is_bot: bool, pending: bool
) -> Literal["ignore", "wait", "welcome"]:
    """Say what to do about a member event: "ignore", "wait" or "welcome".

    Other guilds and bots (pending or not) are ignored. A member still
    pending rules screening waits; `on_member_update` asks again when
    `pending` flips to false. Everyone else is welcomed.

    This is the one predicate an Onboarding fallback would change later, if
    Onboarding turns out not to set `pending` the way screening does.
    Nothing else in the flow should need touching.
    """
    if not in_guild or is_bot:
        return "ignore"
    if pending:
        return "wait"
    return "welcome"


class RecentWelcomes:
    """Remember who was welcomed in the last `window` (24 hours by default).

    In memory only, never written to disk. The trade-off is that a restart
    forgets everything, so at worst someone who left and rejoined across the
    restart gets a second welcome. That's the price of never writing who
    joined to disk, and I'll take it.

    `check_and_record` records before the caller sends. Two events arriving
    close together can't both get through, and a send that fails isn't
    retried (a missing welcome beats a doubled one).
    """

    def __init__(self, window: timedelta = timedelta(hours=24)) -> None:
        self._window = window
        self._seen: dict[int, datetime] = {}

    def check_and_record(self, user_id: int, now: datetime) -> bool:
        """Return True (and record `now`) if `user_id` wasn't welcomed within the window.

        The boundary is inclusive: at exactly one window later, it's True.
        Every call first drops entries a full window old or older, so the
        map only ever holds the last day's welcomes.

        If the clock goes backwards, elapsed time is negative, which counts
        as inside the window: no crash, no double welcome, and the earlier
        record is kept. Such entries are pruned once the clock catches up.
        """
        cutoff = now - self._window
        self._seen = {uid: t for uid, t in self._seen.items() if t > cutoff}
        if user_id in self._seen:
            return False
        self._seen[user_id] = now
        return True
