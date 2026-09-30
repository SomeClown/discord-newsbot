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


async def send_to_channel(client: discord.Client, channel_id: int, text: str) -> bool:
    """Post `text` to `channel_id` with no mentions, capped at Discord's limit. Never raises.

    The shared plumbing under every admin-ish message: `send_alert` (v2's
    owner path) and the per-server router in `newsbot.guilds.notify`. True
    if the message went out, False if anything went wrong (the failure is
    logged, not raised). The cap and `AllowedMentions.none()` live here so
    no caller can forget them.
    """
    text = _truncate_utf16(text, _ALERT_CONTENT_LIMIT, suffix="\u2026")
    try:
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.exception("failed to send admin alert", extra={"admin_channel_id": channel_id})
        return False
    return True


async def send_alert(client: discord.Client, admin_channel_id: int | None, text: str) -> None:
    """Post `text` to the admin channel, if one is configured. Never raises.

    A failure here (channel deleted, permissions revoked, gateway hiccup)
    is logged and swallowed rather than propagated; the code calling
    `send_alert` is usually already in an exception handler, and an alert
    system that can knock over its own caller defeats the point of having
    one.

    Anything over Discord's 2,000-unit message limit is cut (ellipsis
    included) before sending. Discord refuses an oversized message outright,
    and refusing is the one thing an alert can't be allowed to have happen to
    it: the crash alert with a huge exception in it is exactly the one
    somebody needs to see.
    """
    if admin_channel_id is None:
        return
    await send_to_channel(client, admin_channel_id, text)


__all__ = ["send_alert", "send_to_channel"]
