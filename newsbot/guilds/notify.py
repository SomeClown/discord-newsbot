"""Who hears about what: the one place a message picks its audience.

The public bot has two kinds of bad news and they must never cross. A
server's problem (its digest couldn't post, a channel it picked got
deleted) belongs to that server's admins and nobody else; the owner has 38
other servers to worry about and no business reading someone's channel
names. A bot-wide problem (a source has been dead since Tuesday, a job
crashed) belongs to the owner's admin channel in the home guild and must
not show up in a stranger's server looking like their fault.

So there are exactly three doors, and each one only opens onto its own
room:

- `Router.alert_owner(text)`: the owner's channel. Nothing else ever goes
  there.
- `Router.notify_guild(guild_id, text)`: always written to `guild_notices`
  (so `/newsbot status` can show it), and posted to that guild's admin
  channel if it has one. No admin channel means no post; I'd rather lose a
  message than DM-spam a server that never asked for one. There is no
  fallback to the owner channel, on purpose.
- `Router.send_report(guild_id, admin_channel_id, text)`: a per-server run
  report, posted to the channel it's handed. Not a problem, so no notice.

Every send has mentions switched off and the text cut to Discord's limit,
and none of the three raises: an alert path that can knock over its caller
is a pager that sets the building on fire. Nor can one hang: each send gets
`_SEND_TIMEOUT_S` and a timeout counts as a failed send. Blank text (empty,
whitespace, NULs) is dropped at every door, because Discord answers an empty
message with a 400 and nobody needs that.

Channel ids live in the database and are just numbers, so a server could
name someone else's channel as its admin channel and our own notices would
cheerfully post there. The real sender (`client_sender`) therefore checks,
at send time, that the channel it resolved belongs to the guild being told;
if it doesn't (or can't be tied to any guild), nothing is posted and a
WARNING with ids only is logged. The notice stays in `guild_notices`. The
owner door does the same check against the home guild, when one is
configured, except that "couldn't verify" still sends: that channel is the
owner's own setting. The actual Discord send is injected (`Send`), so all of
this is testable without a gateway; a bare `Send` has no way to know about
guilds, so only `client_sender`'s version carries the check.

Callers escape their own scraped text (headlines, exception messages). The
router's job is the mention backstop: `@everyone` and friends get defused
here no matter what, belt to `AllowedMentions.none()`'s suspenders.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

import discord

from newsbot.alerts import send_to_channel, send_to_guild_channel
from newsbot.bot.format import _ALERT_CONTENT_LIMIT, _truncate_utf16
from newsbot.store import repo
from newsbot.store.db import connect

logger = logging.getLogger(__name__)

# One routed send gets this long. Discord's own client gives up well before
# this on a healthy day; the point is that an unhealthy one can't park a
# digest run (or the startup sweep) behind a send that never comes back.
_SEND_TIMEOUT_S = 15.0

# (channel_id, text) -> True if it went out. Must never raise.
Send = Callable[[int, str], Awaitable[bool]]


class ClientSender:
    """The real `Send`, plus `in_guild`, which refuses channels from other servers."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def __call__(self, channel_id: int, text: str) -> bool:
        return await send_to_channel(self._client, channel_id, text)

    async def in_guild(
        self, guild_id: int, channel_id: int, text: str, *, unverified_ok: bool = False
    ) -> bool:
        """Post only if `channel_id` belongs to `guild_id`; see `send_to_guild_channel`."""
        return await send_to_guild_channel(
            self._client, guild_id, channel_id, text, unverified_ok=unverified_ok
        )


def client_sender(client: discord.Client) -> ClientSender:
    """The real `Send`: post through `client`, guild-aware where the router asks."""
    return ClientSender(client)


def _safe_text(text: str) -> str:
    """Defuse mentions and NULs, then cap at Discord's limit."""
    text = discord.utils.escape_mentions(text.replace("\x00", ""))
    return _truncate_utf16(text, _ALERT_CONTENT_LIMIT, suffix="…")


class Router:
    """Routes owner alerts, server notices and run reports. See the module docstring."""

    def __init__(
        self,
        db_path: str | Path,
        send: Send,
        owner_channel_id: int | None,
        *,
        home_guild_id: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = db_path
        self._send = send
        self._owner_channel_id = owner_channel_id
        self._home_guild_id = home_guild_id
        self._now = now

    async def alert_owner(self, text: str) -> None:
        """Bot-wide news, to the owner's admin channel only. Never raises."""
        if self._owner_channel_id is None:
            logger.warning("owner alert with no owner channel configured: %.200s", text)
            return
        await self._deliver(self._owner_channel_id, text, guild_id=self._home_guild_id, owner=True)

    async def notify_guild(self, guild_id: int, text: str) -> None:
        """Record a notice for `guild_id` and post it to that guild's admin channel, if set.

        Never raises, never posts anywhere but the guild's own admin
        channel. A failed send (deleted channel, revoked permission) leaves
        the notice recorded, which is the point of recording it first.
        """
        text = _safe_text(text)
        if not text.strip():
            logger.debug("dropping a blank guild notice", extra={"guild_id": guild_id})
            return
        admin_channel_id: int | None = None
        try:
            admin_channel_id = await asyncio.to_thread(self._record, guild_id, text)
        except Exception:
            logger.exception("couldn't record a guild notice", extra={"guild_id": guild_id})
        if admin_channel_id is not None:
            await self._deliver(admin_channel_id, text, guild_id=guild_id)

    async def send_report(self, guild_id: int, admin_channel_id: int, text: str) -> None:
        """Post a run report to `admin_channel_id`, if it's in `guild_id`. Never raises."""
        await self._deliver(admin_channel_id, text, guild_id=guild_id)

    async def _deliver(
        self, channel_id: int, text: str, *, guild_id: int | None = None, owner: bool = False
    ) -> bool:
        """One send: blank dropped, timed out, guild-checked if the sender can. False on failure."""
        text = _safe_text(text)
        if not text.strip():
            logger.debug("dropping a blank routed message", extra={"channel_id": channel_id})
            return False
        in_guild = getattr(self._send, "in_guild", None)
        try:
            if guild_id is not None and in_guild is not None:
                coro = in_guild(guild_id, channel_id, text, unverified_ok=owner)
            else:
                coro = self._send(channel_id, text)
            return bool(await asyncio.wait_for(coro, _SEND_TIMEOUT_S))
        except TimeoutError:
            logger.warning(
                "a routed send timed out after %.0fs",
                _SEND_TIMEOUT_S,
                extra={"channel_id": channel_id},
            )
        except Exception:
            logger.exception("couldn't send a routed message", extra={"channel_id": channel_id})
        return False

    def _record(self, guild_id: int, text: str) -> int | None:
        """Write the notice, then return the guild's admin channel id (or None)."""
        with closing(connect(self._db_path)) as conn:
            guild = repo.get_guild(conn, guild_id)
            if guild is None:
                return None
            repo.add_notice(conn, guild_id, text, now=self._now)
            return guild.admin_channel_id
