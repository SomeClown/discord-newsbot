"""Joining and leaving: the pure half of the guild lifecycle (design.md §15, plan §3.8).

A server invites the bot, the bot writes one row and says hello once. A
server kicks the bot, the rows go. In between, the bot has to reconcile what
it believes (the database) with what it can see (Discord's guild cache),
and those two disagree more often than you'd think: joins and removals both
happen while the bot is down.

Everything here takes plain values and returns plain values, so it can be
tested without a gateway. The handlers that touch Discord and the database
live in `bot/client.py`. The one rule that shaped this file: a guild list
that comes back short must never be read as "everyone left". A Discord
outage that reports zero guilds is precisely the day a naive cleanup would
delete every server's settings, and I'd like not to learn that in production.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import discord

from newsbot.bot.format import _INVISIBLE_EXTRA, discord_len, esc
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

# The one message the bot ever sends unprompted. Plain text, no mentions: the
# backticks are a code span, not a ping, and `send` passes
# AllowedMentions.none() as well, belt and suspenders.
FIRST_CONTACT_TEXT = (
    "Thanks for letting me in. I post a daily news digest for the games you pick, "
    "plus SHiFT codes for Borderlands. An admin with Manage Server can run "
    "`/newsbot setup` to pick games, a channel and a time. Until then I'll stay quiet."
)

# How long to wait on Discord for the hello and for `fetch_guild` before giving
# up. Both run inside startup reconciliation, where a hang stalls every server
# after it; fifteen seconds is generous for one HTTP call and stingy for a
# stuck one.
HELLO_TIMEOUT = 15.0
FETCH_TIMEOUT = 15.0
# A server picks its own name, so the owner's alert gets at most this many
# characters of it (the ellipsis counts), and the offline summaries list at
# most this many servers before they say "and K more".
NAME_CAP = 100
LIST_CAP = 10
# When the valve trips, the bot asks Discord about this many stale rows per
# start, one at a time with a short pause between, so a mass offline kick
# clears itself over a few restarts without hammering the API.
VALVE_CHECK_CAP = 50
VALVE_CHECK_PAUSE = 0.25

# Below this many stale rows the valve never trips on percentage alone.
VALVE_MIN_DELETES = 3
# Above this share of all rows, a cleanup is treated as a bad guild list.
VALVE_FRACTION = 0.10


# Control characters (newlines and tabs included; `plain_line` turns those into
# spaces first) plus the bidi overrides and isolates, which can make a name read
# backwards over the text after it. Nobody's server is called that on purpose.
_NAME_JUNK_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
# A lone surrogate can't be encoded, and Discord's own client would choke on it.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
# Longest name a summary shows per server. Ten of them, escaped, plus ids has to
# fit in one message, so the summaries are stricter than a lone join alert.
SUMMARY_NAME_CAP = 60
# A summary is trimmed to this many UTF-16 units, a little under Discord's 2,000,
# so "Now in N servers." always survives the router's own cut.
SUMMARY_LIMIT = 1900


# A space, a control or format character, or one of `format._INVISIBLE_EXTRA` (fillers, the
# braille blank, the variation selectors and the like). Only used to decide whether a name is
# "(unnamed)"; a visible name keeps its joiners, or every family emoji would fall apart.
def _is_invisible(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") or ch in _INVISIBLE_EXTRA


def guild_label(guild: Any, *, comped: bool = False, cap: int = NAME_CAP) -> str:
    """`Name (id)` for the owner's alerts, or `Name (id, comped)`. Safe to paste into a message.

    The name is untrusted text: lone surrogates replaced, junk characters out,
    whitespace collapsed, capped at `cap` shown characters, then run through
    `esc` so mentions, links and markdown can't come alive in the owner's
    channel. A name with nothing visible in it (spaces, zero-width characters,
    fillers) shows as "(unnamed)", same as a server with no name at all. Reading
    `guild.name` can raise on a strange guild; that's the caller's to catch.
    """
    raw = _LONE_SURROGATE_RE.sub("\ufffd", str(getattr(guild, "name", None) or ""))
    raw = _NAME_JUNK_RE.sub(" ", raw)
    name = plain_line(raw, cap)
    if all(_is_invisible(ch) for ch in name):
        name = "(unnamed)"
    else:
        name = esc(name)
    return f"{name} ({guild.id}, comped)" if comped else f"{name} ({guild.id})"


def servers(count: int) -> str:
    """`1 server`, `0 servers`, `12 servers`."""
    return f"{count} server" if count == 1 else f"{count} servers"


def summary_text(
    prefix: str, labels: Sequence[str], tail: str, *, limit: int = SUMMARY_LIMIT
) -> str:
    """`prefix` + the labels + `tail`, listing at most `LIST_CAP` and fitting in `limit` units.

    Labels go in one at a time until the next would push the whole text past
    `limit` (measured the way Discord measures), and the rest become "and K
    more". `tail` is never cut, which is the point: it carries the count.
    """

    def build(shown: Sequence[str]) -> str:
        rest = len(labels) - len(shown)
        body = ", ".join(shown)
        if rest:
            body = f"{body}, and {rest} more" if shown else f"{rest} in all"
        return f"{prefix}{body}.{tail}"

    shown: list[str] = []
    for label in labels[:LIST_CAP]:
        if discord_len(build([*shown, label])) > limit:
            break
        shown.append(label)
    return build(shown)


def can_speak_in(channel: Any, me: Any) -> bool:
    """True if `me` can see `channel` and send messages in it. Never raises."""
    if channel is None or me is None:
        return False
    try:
        perms = channel.permissions_for(me)
        return bool(perms.view_channel and perms.send_messages)
    except Exception:
        # Includes a result that isn't a Permissions at all. Discord never
        # sends one; I'd still rather not find out the hard way.
        logger.debug("permissions_for failed while picking a first-contact channel")
        return False


def pick_first_contact_channel(guild: Any) -> Any | None:
    """Where to say hello: the system channel if the bot may speak there, else the first that works.

    "First" is lowest position (ties broken by id, so the answer is stable).
    Returns None when there's nowhere to speak, in which case the caller
    logs and says nothing; a bot that can't find a door doesn't climb
    through a window.
    """
    me = getattr(guild, "me", None)
    system = getattr(guild, "system_channel", None)
    if can_speak_in(system, me):
        return system
    for channel in sorted(guild.text_channels, key=lambda c: (c.position, c.id)):
        if can_speak_in(channel, me):
            return channel
    return None


def allowed_mentions() -> discord.AllowedMentions:
    """What the first-contact message is allowed to ping: nobody."""
    return discord.AllowedMentions.none()


@dataclass(frozen=True)
class ReconcilePlan:
    """What startup reconciliation intends to do.

    `to_delete` are stale rows that are safe to delete outright. `to_confirm`
    are stale rows the plan won't delete on the cache's say-so alone (the
    imported server: see `reconcile_plan`); the I/O step asks Discord
    directly. `to_create` are guilds the bot is in and has no row for.
    `valve_tripped` means there were stale rows and the plan refused to
    delete any of them; `valve_reason` says why, for the owner alert, and
    `to_verify` lists the stale rows the I/O step should ask Discord about
    one by one (deleting only the ones Discord confirms are gone).
    """

    to_delete: tuple[int, ...]
    to_confirm: tuple[int, ...]
    to_create: tuple[int, ...]
    valve_tripped: bool = False
    valve_reason: str = ""
    to_verify: tuple[int, ...] = ()


def reconcile_plan(
    db_ids: Collection[int],
    live_ids: Collection[int],
    *,
    protected_ids: Collection[int] = (),
) -> ReconcilePlan:
    """Compare the database's guild ids with the ones the bot is really in. Pure.

    Stale rows (in the database, not live) are deleted only if the valve
    allows it. The valve trips when:
    - the live list is empty but the database isn't (an outage, not a mass
      exodus; this catches the small databases the percentage rule can't);
    - or the database has rows, the live list is non-empty, and the two share
      nothing (a wrong guild list looks exactly like this, and a three-row
      database would otherwise lose every row to it);
    - or stale rows would exceed max(3, 10% of all rows).
    When it trips, nothing is deleted here, `valve_reason` is set and every
    stale row goes in `to_verify`: the caller checks each with Discord.
    Creating missing rows is never blocked: that can only add a free row.

    `protected_ids` (the imported server) are never put in `to_delete`;
    they go to `to_confirm` so the caller can check with Discord before
    dropping the one server that has real settings in it. Unavailable guilds
    must be in `live_ids` (the caller's job); they count as present.
    """
    db = set(db_ids)
    live = set(live_ids)
    protected = set(protected_ids)
    stale = sorted(db - live)
    to_create = tuple(sorted(live - db))
    if not stale:
        return ReconcilePlan((), (), to_create)
    if not live:
        return ReconcilePlan(
            (),
            (),
            to_create,
            True,
            "the bot sees no servers at all, which looks like an outage and not a mass removal",
            tuple(stale),
        )
    if not db & live:
        return ReconcilePlan(
            (),
            (),
            to_create,
            True,
            "none of the saved servers are among the ones the bot sees, which looks like a "
            "wrong server list and not a mass removal",
            tuple(stale),
        )
    limit = max(VALVE_MIN_DELETES, VALVE_FRACTION * len(db))
    if len(stale) > limit:
        return ReconcilePlan(
            (),
            (),
            to_create,
            True,
            f"{len(stale)} of {len(db)} servers look gone, over the limit of {limit:g}",
            tuple(stale),
        )
    return ReconcilePlan(
        tuple(g for g in stale if g not in protected),
        tuple(g for g in stale if g in protected),
        to_create,
    )


def comp_candidates(listed_ids: Collection[int], tiers: Mapping[int, str]) -> list[int]:
    """Listed guilds (`comped_guild_ids`) that have a row and aren't comped yet. Pure.

    Only ever upgrades: a comped guild that isn't listed is left alone, and a
    listed guild with no row isn't created here (it isn't one of ours until
    the bot is actually in it).
    """
    return sorted(g for g in set(listed_ids) if tiers.get(g) == "free")
