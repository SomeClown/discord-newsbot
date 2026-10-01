"""Admin-channel notifications.

When something in the daily job goes sideways (a source dies three days
running, a publish fails after every retry, the process finds a stale
`pending` row at startup), somebody should hear about it without needing
to tail container logs at 6 a.m. That somebody is `admin_channel_id`, if
the owner configured one.

This module is deliberately the dumbest possible pager: one function, no
queue, no rate limiting, no retry of its own. An alert that fails to send
gets logged and dropped, not retried into an alert storm. If it turns out
we need more than that, the yak has been sighted and can be shaved later.
"""

from __future__ import annotations

import logging

import discord

from newsbot.bot.format import _ALERT_CONTENT_LIMIT, _truncate_utf16

logger = logging.getLogger(__name__)


async def _post(
    client: discord.Client,
    channel_id: int,
    text: str,
    guild_id: int | None,
    *,
    unverified_ok: bool = False,
) -> bool:
    """Resolve `channel_id`, optionally check its server, and post. Never raises.

    `guild_id` None means "post wherever it points" (v2). An int means the
    channel must resolve to one whose `guild.id` equals it. A channel that
    positively belongs to a different server is never posted to; one that
    can't be tied to any server (no `guild`) is refused too, unless
    `unverified_ok`, which the owner path uses: the owner's channel is the
    owner's own setting.
    """
    text = _truncate_utf16(text, _ALERT_CONTENT_LIMIT, suffix="\u2026")
    try:
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        if guild_id is not None:
            found = getattr(getattr(channel, "guild", None), "id", None)
            if found != guild_id and not (found is None and unverified_ok):
                # Ids only: a channel's name or contents are another server's business.
                logger.warning(
                    "not posting: channel %s isn't in guild %s (resolved to guild %s)",
                    channel_id,
                    guild_id,
                    found,
                )
                return False
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.exception("failed to send admin alert", extra={"admin_channel_id": channel_id})
        return False
    return True


async def send_to_channel(client: discord.Client, channel_id: int, text: str) -> bool:
    """Post `text` to `channel_id` with no mentions, capped at Discord's limit. Never raises.

    The owner door of the router in `newsbot.guilds.notify` goes through here.
    True if the message went out, False if anything went wrong (the failure is
    logged, not raised).
    The cap and `AllowedMentions.none()` live here so no caller can forget
    them. It doesn't ask whose channel it is; for a message that belongs to
    one server, use `send_to_guild_channel`.
    """
    return await _post(client, channel_id, text, None)


async def send_to_guild_channel(
    client: discord.Client,
    guild_id: int,
    channel_id: int,
    text: str,
    *,
    unverified_ok: bool = False,
) -> bool:
    """`send_to_channel`, but only if the channel really belongs to `guild_id`. Never raises.

    The stored channel id is whatever an admin typed into a setting, and ids
    are just numbers; nothing else stops a server from naming somebody
    else's channel. So the check happens here, at send time, against what
    Discord says. A channel in another server, or one with no server at all
    (a DM, a stale id), gets a False and a WARNING with ids only.
    `unverified_ok` lets the no-server case through (the owner's channel).
    """
    return await _post(client, channel_id, text, guild_id, unverified_ok=unverified_ok)


__all__ = ["send_to_channel", "send_to_guild_channel"]
