"""Admin-channel notifications.

When something in the daily job goes sideways -- a source dies three days
running, a publish fails after every retry, the process finds a stale
`pending` row at startup -- somebody should hear about it without needing
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

logger = logging.getLogger(__name__)


async def send_alert(client: discord.Client, admin_channel_id: int | None, text: str) -> None:
    """Post `text` to the admin channel, if one is configured. Never raises.

    A failure here (channel deleted, permissions revoked, gateway hiccup)
    is logged and swallowed rather than propagated -- the code calling
    `send_alert` is usually already in an exception handler, and an alert
    system that can knock over its own caller defeats the point of having
    one.
    """
    if admin_channel_id is None:
        return
    try:
        channel = client.get_channel(admin_channel_id)
        if channel is None:
            channel = await client.fetch_channel(admin_channel_id)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.exception("failed to send admin alert", extra={"admin_channel_id": admin_channel_id})


__all__ = ["send_alert"]
