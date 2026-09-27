"""Startup permission check: does the bot actually have what it configured itself to need?

Config validation (`newsbot.config.load_config`) already checks that every
channel id in `config.yaml` is a positive int and that the pieces fit
together -- it has no way to ask Discord whether the bot's role can
actually post there, because that answer doesn't exist until the gateway
connects and the guild's permission overwrites are in hand. This module is
that second, later check: on the first `on_ready`, walk every channel the
bot is configured to use and ask Discord directly. Anything wrong -- a
missing permission, a channel that's gone, a channel that belongs to some
other guild entirely -- becomes one line in one admin alert. The bot still
starts either way; a permission problem on day one is annoying, but it's a
lot less annoying than a bot that refuses to come up at all because
`#palworld` forgot Embed Links.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

from newsbot.bot.format import _truncate_utf16
from newsbot.config import AppConfig

logger = logging.getLogger(__name__)

# Discord's plain-message content cap -- same constant `format.py` uses for
# a code alert, reused here rather than imported since this is the only
# other place in the codebase that needs it and a second admin-alert
# constant module felt like overkill for one int.
_ALERT_LIMIT = 2000

# Every permission flag this module ever asks about, in a fixed order so
# "missing X, Y" reads the same way every time instead of shuffling with
# whatever order a frozenset happens to iterate in this process.
_PERMISSION_ORDER = ("view_channel", "send_messages", "embed_links", "mention_everyone")
_PERMISSION_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "mention_everyone": "Mention @everyone",
}


@dataclass(frozen=True)
class ChannelRequirement:
    """What one channel needs to do its job, and why."""

    channel_id: int
    purpose: str
    needed: frozenset[str]


def required_channels(cfg: AppConfig) -> list[ChannelRequirement]:
    """Every channel the config points at, and the permission set each one needs.

    design.md §13: game channels need View Channel, Send Messages, Embed
    Links; the SHiFT channel (only when alerts are enabled) additionally
    needs Mention @everyone; the admin channel needs View Channel and Send
    Messages. "Topics sharing a channel" is an owner-approved shape (§5 of
    the plan), so requirements are merged by channel id rather than
    reported once per topic that names it -- two topics pointed at the
    same channel end up as one requirement with the union of what either
    of them needs, not two separate alerts about the same channel.
    """
    by_channel: dict[int, ChannelRequirement] = {}

    def _add(channel_id: int, purpose: str, needed: frozenset[str]) -> None:
        existing = by_channel.get(channel_id)
        if existing is None:
            by_channel[channel_id] = ChannelRequirement(channel_id, purpose, needed)
            return
        merged_purpose = (
            existing.purpose if purpose in existing.purpose else f"{existing.purpose} / {purpose}"
        )
        by_channel[channel_id] = ChannelRequirement(
            channel_id, merged_purpose, existing.needed | needed
        )

    for topic in cfg.topics:
        _add(
            topic.channel_id,
            topic.name,
            frozenset({"view_channel", "send_messages", "embed_links"}),
        )

    if cfg.alerts.enabled and cfg.alerts.channel_id is not None:
        needed = {"view_channel", "send_messages"}
        # max_pings_per_day == 0 means pinging is off on purpose (sweep.py
        # already suppresses the "cap reached" alert for the same reason)
        # -- flagging a missing Mention @everyone permission that will
        # never actually get used would just be noise.
        if cfg.alerts.max_pings_per_day > 0:
            needed.add("mention_everyone")
        _add(cfg.alerts.channel_id, "SHiFT codes", frozenset(needed))

    if cfg.admin_channel_id is not None:
        _add(cfg.admin_channel_id, "admin", frozenset({"view_channel", "send_messages"}))

    return list(by_channel.values())


def missing(perms: discord.Permissions, needed: frozenset[str]) -> list[str]:
    """Which of `needed`'s flags `perms` doesn't grant, in `_PERMISSION_ORDER`."""
    return [flag for flag in _PERMISSION_ORDER if flag in needed and not getattr(perms, flag)]


async def _resolve_channel(client: discord.Client, channel_id: int) -> object:
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    return await client.fetch_channel(channel_id)


# Voice and stage channels are, technically, `Messageable` in discord.py
# (Discord grew text chat in voice channels a while back) -- but they're
# not what an owner means by "post the digest here", so they stay flagged
# even though they'd otherwise pass the check below.
_EXCLUDED_CHANNEL_TYPES = (discord.VoiceChannel, discord.StageChannel)


def _is_sendable_guild_channel(channel: object) -> bool:
    """True for anything `_check_one` should treat as a normal text destination.

    A thread isn't a `discord.abc.GuildChannel` (it's its own class), but
    it's exactly as sendable as the channel it lives in, and there's no
    design.md reason a game or SHiFT channel couldn't be one -- excluding
    it just because it fails an `isinstance(..., TextChannel)` check was
    the actual bug here. Forum and category channels fail this on their
    own (neither is `Messageable`); voice and stage channels are excluded
    explicitly, above.
    """
    if isinstance(channel, _EXCLUDED_CHANNEL_TYPES):
        return False
    if isinstance(channel, discord.Thread):
        return True
    return isinstance(channel, discord.abc.Messageable) and isinstance(
        channel, discord.abc.GuildChannel
    )


async def _check_one(client: discord.Client, guild_id: int, req: ChannelRequirement) -> str | None:
    """Return one problem line for `req`, or None if the channel checks out clean."""
    try:
        channel = await _resolve_channel(client, req.channel_id)
    except discord.NotFound, discord.Forbidden:
        return f"{req.purpose} channel <#{req.channel_id}>: not found or not visible to the bot"

    guild = getattr(channel, "guild", None)
    if guild is None or guild.id != guild_id:
        return f"{req.purpose} channel <#{req.channel_id}>: not in the configured guild"
    if not _is_sendable_guild_channel(channel):
        return f"{req.purpose} channel <#{req.channel_id}>: not a text channel"

    me = guild.me
    if me is None:
        return (
            f"{req.purpose} channel <#{req.channel_id}>: "
            "can't tell this bot's own permissions there"
        )

    missing_flags = missing(channel.permissions_for(me), req.needed)
    if not missing_flags:
        return None
    names = ", ".join(_PERMISSION_LABELS[flag] for flag in missing_flags)
    return f"{req.purpose} channel <#{req.channel_id}>: missing {names}"


async def check_channels(client: discord.Client, cfg: AppConfig) -> list[str]:
    """Check every configured channel; return one human-readable problem per broken one.

    Never raises: a crash resolving one channel (a network blip mid-check,
    an unexpected discord.py exception) becomes its own problem line rather
    than aborting the rest of the walk -- one bad channel shouldn't hide
    problems in every other one. The caller (`NewsBot.on_ready`) wraps this
    whole call in its own try/except anyway, on the theory that a startup
    check should never be able to block startup, but there's no reason a
    single channel's failure needs to reach that outer net when it can be
    handled right here and the walk can just continue.
    """
    problems: list[str] = []
    for req in required_channels(cfg):
        try:
            problem = await _check_one(client, cfg.guild_id, req)
        except Exception as exc:  # noqa: BLE001 -- one bad channel must not stop the rest
            logger.exception("permission check failed for channel %d", req.channel_id)
            problem = f"{req.purpose} channel <#{req.channel_id}>: permission check failed ({exc})"
        if problem is not None:
            problems.append(problem)
    return problems


def render_permission_alert(problems: list[str]) -> str:
    """One admin-alert message naming every problem `check_channels` found.

    Capped at Discord's 2000-unit message content limit, same as every
    other plain-text send in this codebase -- an owner with enough
    misconfigured channels to blow past that has bigger problems than a
    truncated alert, but it should still truncate cleanly instead of
    bouncing off Discord entirely.
    """
    if not problems:
        return ""
    lines = ["newsbot: startup permission check found problems:"]
    lines.extend(f"- {p}" for p in problems)
    return _truncate_utf16("\n".join(lines), _ALERT_LIMIT, suffix="…")


__all__ = [
    "ChannelRequirement",
    "check_channels",
    "missing",
    "render_permission_alert",
    "required_channels",
]
