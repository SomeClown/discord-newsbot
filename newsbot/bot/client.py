"""The discord.py client: gateway connection, scheduler wiring, and the heartbeat file.

Everything before this module could be tested without a bot token, a guild,
or an event loop discord.py owns. This is where that ends: `NewsBot` holds the
httpx client, the Anthropic client, the slash-command tree and the
APScheduler instance that drives every job, and it has to bring all of them up
in the right order relative to discord.py's own connection lifecycle.

The one rule that matters most here: **the scheduler has to start inside
`setup_hook`**, not in `__main__` before `client.run()` and not lazily on
first use. `setup_hook` runs after discord.py has created (but not yet
started spinning) its event loop, which is the only point where "the loop
APScheduler binds to" and "the loop discord.py's gateway actually runs on"
are guaranteed to be the same loop. Get this wrong and the jobs fire into a
loop nobody's listening on: a mistake that is, by design, invisible until
9 a.m. the first morning it matters.

The second rule is newer: the every-minute digest job stays inert until the
first `on_ready` has finished its startup work. The channel cache isn't
trustworthy before then, and a digest due at the moment of a restart is
better off waiting ninety seconds than posting into a cache that doesn't know
what a channel is yet.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import random
import re
import socket
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
import httpx
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from discord import app_commands

from newsbot.bot.format import (
    RenderedAlert,
    RenderedDigest,
    esc,
    outcome_from_digest,
    render_owner_report,
)
from newsbot.bot.permissions import (
    render_sweep_counts,
    requirements_from_db,
    sweep_guild_permissions,
)
from newsbot.collectors.base import RateLimitState
from newsbot.config import AppConfig, QuoteSourceCfg, Secrets
from newsbot.guilds import lifecycle
from newsbot.guilds.importer import ImportReport
from newsbot.guilds.notify import Router, client_sender
from newsbot.lounge.daily import QuoteDeps, QuoteOutcome, run_daily_quote
from newsbot.lounge.sources import cache_dir_for
from newsbot.lounge.welcome import RecentWelcomes, render_welcome, welcome_action
from newsbot.pipeline.collect import (
    CollectionDeps,
    build_collection_collectors,
    collection_job_options,
    run_collection,
)
from newsbot.pipeline.guild_digest import GuildDigestDeps, run_due_guilds
from newsbot.pipeline.publisher import PublishError
from newsbot.pipeline.run import local_run_date
from newsbot.pipeline.summaries import (
    SummaryDeps,
    dry_run_lookup,
    prepare_summaries,
    retry_lookup,
    summary_lookup,
)
from newsbot.pipeline.summarize import AnthropicLLM, LLMClient
from newsbot.shift.fanout import (
    FanoutDeps,
    deliver_queued_codes,
    make_shift_hook,
    recover_pending_guild_codes,
)
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import GuildSettings, LoungeSettings
from newsbot.text import plain_line
from newsbot.useragent import user_agent_headers, warn_if_contact_unset

logger = logging.getLogger(__name__)

# One id per process, generated at import time: not persisted, not
# configured, just enough to tell two log lines (or two admin alerts)
# apart when they might be coming from two different processes holding
# the same bot token (see CLAUDE.md's "never run two bot processes with
# the same token" rule, and _maybe_alert_two_instances below, which is
# what actually catches that happening).
_INSTANCE_ID = uuid.uuid4().hex[:8]
_HOSTNAME = socket.gethostname()

# Discord's JSON error codes (distinct from the HTTP status) for the two
# ways a second process racing this one on the same token shows up: it
# responds to an interaction gateway-delivered to this process too, loses
# the race, and its own attempt to respond either finds the interaction
# already gone (10062, "Unknown interaction") or already answered (40060,
# "Interaction has already been acknowledged").
_TWO_INSTANCE_ERROR_CODES = frozenset({10062, 40060})
_TWO_INSTANCE_ALERT_COOLDOWN = timedelta(hours=1)

# The first collection pass starts this long after `on_ready` finishes its steps.
_COLLECTION_FIRST_RUN_DELAY = timedelta(seconds=30)


def _alert_reason(exc: BaseException) -> str:
    """One inert line of `exc`'s text for a job-boundary crash alert.

    Exception messages are whatever the code that raised them felt like
    writing: multi-line, markdown, the occasional @everyone. Flatten it,
    cap it, escape it. (`send_alert` also truncates the whole message, as
    the backstop; this keeps the alert readable before it gets that far.)
    """
    return esc(plain_line(str(exc), 250))


def _http_detail(exc: BaseException) -> str:
    """` (status=..., code=...)` for a `discord.HTTPException`, else an empty string."""
    if not isinstance(exc, discord.HTTPException):
        return ""
    return f" (status={getattr(exc, 'status', None)}, code={getattr(exc, 'code', None)})"


def _should_alert_two_instances(
    last_alert: datetime | None, now: datetime, cooldown: timedelta = _TWO_INSTANCE_ALERT_COOLDOWN
) -> bool:
    """True if `cooldown` has passed since the last two-instance alert (or there wasn't one).

    A second process on the same token doesn't cause one 10062/40060 --
    it causes a steady stream of them, one per interaction it loses the
    race on. Alerting on every single one would just be a different,
    noisier way of drowning out the channel; this caps it at once an hour
    while the problem is still ongoing.
    """
    return last_alert is None or now - last_alert >= cooldown


# The container healthcheck (see healthcheck.py) polls this file's mtime,
# not the process directly: there's no port to poll, since the gateway
# is an outgoing connection. /tmp is fine for this: it's tmpfs in the
# compose config, and the file's only job is to exist and be recent.
HEARTBEAT = Path("/tmp/newsbot-heartbeat")  # noqa: S108 (tmpfs in compose, not a real tempfile race)

_RETENTION_DAYS = 90
# `app_state` key guarding the daily owner report against a second send.
_OWNER_REPORT_KEY = "owner_report_date"
# `app_state` key prefix remembering that a server was told about the digest v2.2 left stuck.
_STUCK_V22_KEY = repo.STUCK_V22_KEY_PREFIX
_HEARTBEAT_INTERVAL_S = 60


def _parse_digest_time(time_str: str) -> dt_time:
    hour, minute = (int(part) for part in time_str.split(":"))
    return dt_time(hour, minute)


def build_intents_for_lounges(lounges: Iterable[LoungeSettings]) -> discord.Intents:
    """The gateway intents: the defaults, plus Server Members iff any lounge row has welcomes on.

    The intent is privileged, so the portal switch has to match it, and a bot
    whose lounges only post quotes never asks. Presences and message content
    stay off; I have no use for either and Discord has opinions about people
    who ask for things they don't use. `__main__` builds this from
    `repo.list_lounges` after the import and the D5 re-sync, so the rows it
    sees are the ones the bot will actually run with.
    """
    intents = discord.Intents.default()
    intents.members = any(lounge.welcome_enabled for lounge in lounges)
    return intents


def schedule_guild_quote(
    scheduler: AsyncIOScheduler,
    guild_id: int,
    quote_time: str,
    timezone: str,
    callback: Callable[[], Awaitable[None]],
) -> Job:
    """Add one server's daily-quote cron job: `quote_time` in that server's `timezone`.

    Same terms as v2.2's job: a five-minute grace, no catch-up, missed means
    skipped. The id carries the guild so two servers' jobs can't replace each
    other (`replace_existing` lets a re-sync move one server's time).
    """
    tz = ZoneInfo(timezone)
    at = _parse_digest_time(quote_time)
    return scheduler.add_job(
        callback,
        CronTrigger(hour=at.hour, minute=at.minute, timezone=tz),
        id=f"daily-quote-{guild_id}",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )


# Errors worth retrying: the network blipped, or a request timed out
# before a response came back at all. `discord.HTTPException` is handled
# separately below, since whether *that* is retryable depends on the
# status code Discord actually sent back.
_TRANSIENT_ERRORS = (aiohttp.ClientError, OSError, TimeoutError)

# The one and only place `AllowedMentions(everyone=True, ...)` is allowed to
# appear in newsbot/ (design.md §12, A5): `tests/test_mentions_tripwire.py`
# scans the source tree to hold that line. Every other send in this file
# (the client's own default, DiscordPublisher's digest sends) stays
# `AllowedMentions.none()`; only a code alert that `decide.plan_alerts`
# actually decided should ping gets this one.
_PING_EVERYONE = discord.AllowedMentions(
    everyone=True, users=False, roles=False, replied_user=False
)

# re.ASCII keeps Unicode digits (fullwidth, Arabic-Indic) out of it.
_ROLE_ID = re.compile(r"[0-9]{17,20}", re.ASCII)


def mentions_for(ping: object) -> discord.AllowedMentions:
    """The one place a server's ping choice becomes an `AllowedMentions`.

    `"everyone"` hands back `_PING_EVERYONE` itself (never a second
    `everyone=True`; the tripwire test holds that line), an exact `str` of 17
    to 20 ASCII digits is a role id (a snowflake, which is how long Discord
    ids actually are) and pings only that role, and anything else pings
    nobody: `"none"`, `None`, garbage, a superscript two, an `int` someone
    swore was a string. It never raises. A bad value in the database should
    cost a notification, not wake a server up (or take the whole fan-out down
    with a `ValueError`, which an earlier draft of this function managed).
    """
    if type(ping) is not str:
        return discord.AllowedMentions.none()
    if ping == "everyone":
        return _PING_EVERYONE
    if _ROLE_ID.fullmatch(ping):
        return discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(id=int(ping))], replied_user=False
        )
    return discord.AllowedMentions.none()


class ChannelNotInGuildError(Exception):
    """A configured channel resolved to a different server's (or to no server at all).

    Not a `PublishError` on purpose: `post_alert_with_retry` retries those, and
    nothing about waiting eight seconds moves a channel into the right guild.
    """


class ForeignMessageError(Exception):
    """A send returned a message that isn't in the channel we sent to (a deduped nonce, say).

    Not a `PublishError`, for the same reason as `ChannelNotInGuildError`: no retry fixes it.
    """


def _foreign_channel(channel: object, guild_id: int | None, channel_id: int) -> bool:
    """True (after a WARNING, ids only) if `channel` isn't in `guild_id`; None means no check.

    Channel ids are numbers an admin typed into a setting, and nothing else stops one
    from naming another server's channel. The router refuses those at send time, and
    the digest and the code alerts get the same rule: a channel with no server at all
    (a stale id, a DM) counts as foreign too.
    """
    if guild_id is None:
        return False
    found = getattr(getattr(channel, "guild", None), "id", None)
    if found == guild_id:
        return False
    logger.warning(
        "not posting: channel %s isn't in guild %s (resolved to guild %s)",
        channel_id,
        guild_id,
        found,
    )
    return True


def _retry_after_s(exc: discord.HTTPException) -> float | None:
    """Seconds Discord asked us to wait, from the exception or the response header, if any."""
    for raw in (
        getattr(exc, "retry_after", None),
        getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After"),
    ):
        try:
            seconds = float(raw)
        except TypeError, ValueError:
            continue
        if seconds >= 0:
            return seconds
    return None


def _classify_send_error(exc: Exception, *, posted_ids: list[int] | None = None) -> None:
    """Classify a send/fetch failure for `DiscordCodeAlertPoster`.

    Wraps as `PublishError` (worth retrying: a 5xx, a 429, a network blip, a
    timeout) or re-raises `exc` unwrapped (any other 4xx, or anything else): a
    permissions problem shouldn't burn a retry budget, and
    `shift/sweep.py` already has its own claim/post/record bookkeeping
    that doesn't lean on `PublishError.posted_by_topic` the way
    `DiscordPublisher` does. `DiscordPublisher` uses the stricter
    `_classify_publish_error` below instead (design.md §13, D2): unlike a
    code alert, a digest publish needs every permanent failure to come
    back as a `PublishError` so `_publish_with_retry` knows to stop
    retrying it rather than escaping unwrapped.
    """
    if isinstance(exc, discord.HTTPException):
        # A 429 is Discord's rate limit, which waiting fixes (unlike a missing
        # permission). Until this was here a 429 that got past discord.py's own
        # retries counted as final, and that server never got its code.
        rate_limited = exc.status == 429
        if exc.status is not None and 400 <= exc.status < 500 and not rate_limited:
            raise exc
        raise PublishError(
            f"discord send failed: {exc}",
            posted_ids=list(posted_ids or []),
            retry_after=_retry_after_s(exc) if rate_limited else None,
            rejected=rate_limited,
        ) from exc
    if isinstance(exc, _TRANSIENT_ERRORS):
        raise PublishError(
            f"discord send failed: {exc}", posted_ids=list(posted_ids or [])
        ) from exc
    raise exc


def _classify_publish_error(
    exc: Exception, *, posted_by_topic: Mapping[str, int] | None = None
) -> None:
    """Classify a send/fetch failure for `DiscordPublisher`.

    Every failure comes back as a `PublishError` here (design.md §13,
    fixing a latent v1 bug): a 5xx, a network blip or a timeout is
    `retryable=True`; a 4xx (`discord.Forbidden` and friends included) is
    `retryable=False`, since no amount of backoff fixes a permissions
    problem or a channel that's gone. v1 let a 4xx escape unwrapped
    instead, on the theory that `_publish_with_retry` only ever caught
    `PublishError` anyway, which worked, but meant whatever *did* post
    before the 4xx only survived if the caller happened to stash it on the
    exception by hand. `posted_by_topic` carries `DiscordPublisher`'s own
    progress so far, so a permanent error on one topic's channel doesn't
    erase what already landed for the topics before it.
    """
    if isinstance(exc, discord.HTTPException):
        # 429 is a 4xx by number, but it's Discord's own rate limit --
        # exactly the kind of thing backing off and trying again actually
        # fixes, unlike the rest of the 4xx range (a permission that's
        # missing, a channel that's gone). Treating it like every other
        # 4xx meant `_publish_with_retry` gave up on a rate limit
        # immediately instead of backing off through it.
        retryable = exc.status is None or exc.status == 429 or not (400 <= exc.status < 500)
        raise PublishError(
            f"discord send failed: {exc}",
            posted_by_topic=posted_by_topic,
            retryable=retryable,
        ) from exc
    if isinstance(exc, _TRANSIENT_ERRORS):
        raise PublishError(f"discord send failed: {exc}", posted_by_topic=posted_by_topic) from exc
    raise exc


class DiscordPublisher:
    """The `Publisher` the real bot uses: one message per topic, to that topic's own channel.

    Implements the same `Publisher` protocol `PrintPublisher` does, so
    the per-server digest's guard, save and retry logic runs identically
    whether the digest is heading to a terminal or a set of channels: this
    class's only job is turning a `RenderedDigest` into Discord API calls
    and topic -> message id results.

    One instance is built fresh per run (see `NewsBot._publisher_for`) and
    then reused across every attempt `run.py`'s `_publish_with_retry` makes
    at it: that's what
    makes resumability possible. `self._posted` remembers which topics
    already got a message id back, so a retried `publish()` call skips
    straight past them instead of reposting a game's embed on every retry.
    Before this tracking existed (v1, one header + N embed-batch messages
    in a single channel), three transient failures in a row meant the
    channel got the same digest four times; the unit of progress is now
    the topic instead of the message, but the reasoning is the same.
    """

    def __init__(
        self,
        client: NewsBot,
        *,
        nonce_scope: str | None = None,
        on_posted: Callable[[str, int], Awaitable[None]] | None = None,
        already_posted: Mapping[str, int] | None = None,
        skip_permanent: bool = False,
        guild_id: int | None = None,
    ) -> None:
        # `NewsBot`, not plain `discord.Client`: kept for parity with v1 and
        # in case a future non-fatal side path (the old thread-creation
        # alert was one) needs `.alert()` again. Forward-referenced since
        # `NewsBot` is defined later in this same module.
        self._client = client
        # The keyword arguments are the per-server digest's (design.md
        # §15, plan task 6); left alone, this behaves exactly as v2.2 did.
        # `already_posted` preloads a resume's finished games, `on_posted`
        # hears about each one as it lands so the caller can write it through
        # to the database, and `skip_permanent` turns a dead channel into "skip
        # that game and carry on" (recorded in `skipped`) instead of ending the
        # whole server's digest. `guild_id` is the server being posted for: a channel that
        # isn't in it is refused (permanently), the same rule the router applies.
        self._guild_id = guild_id
        self._posted: dict[str, int] = dict(already_posted or {})
        self._on_posted = on_posted
        self._skip_permanent = skip_permanent
        self.skipped: dict[str, str] = {}
        self._channels: dict[int, discord.abc.Messageable] = {}
        # One salt per instance, not per send. discord.py 2.7.1 sends `enforce_nonce: true`
        # along with any nonce (`discord/http.py`, `handle_message_parameters`), and
        # `send()` mints a random one when you pass none, so Discord really does check:
        # a second send with the same nonce in the same channel returns the first message
        # instead of posting again. But it only remembers a nonce for a short while (a few
        # minutes; Discord doesn't say), so this is a seatbelt for a retry on *this*
        # instance (the same run, backing off after a transient failure, which reuses the
        # salt and so each topic's nonce and catches a send that landed before the retry
        # thought it failed). It is not what makes a restart safe. A resume after a crash
        # waits out the 10 minute lease, long past that window, and relies on the digest
        # row's write-through instead; the leftover risk is one duplicate game post (D7).
        # A confirmed run-now builds a fresh `DiscordPublisher` with a fresh scope, so its
        # deliberate repost isn't mistaken for a duplicate. With a `nonce_scope`
        # (`"{guild_id}|{run_date}"`) the salt is that instead of a random one, so a retry
        # after a quick restart can still be deduplicated.
        self._nonce_salt = nonce_scope if nonce_scope is not None else uuid.uuid4().hex

    @property
    def posted_by_game(self) -> dict[str, int]:
        """Game key -> message id for everything posted so far, including a resume's preload."""
        return dict(self._posted)

    @property
    def posted_ids(self) -> list[int]:
        """Every message id posted so far, in topic-post order. Read-only on purpose.

        `_run_post`'s cancellation fallback reads this when an exception
        escapes with no `posted_ids` of its own to report: a plain list,
        matching what `digests.posted_message_ids` has always stored,
        derived from (never mutable alongside) `self._posted`.
        """
        return list(self._posted.values())

    async def publish(self, r: RenderedDigest) -> dict[str, int]:
        for message in r.messages:
            if message.topic_key in self._posted or message.topic_key in self.skipped:
                continue
            try:
                channel = await self._resolve_channel(message.channel_id)
                nonce = hashlib.sha256(
                    f"{self._nonce_salt}|{message.topic_key}".encode()
                ).hexdigest()[:25]
                try:
                    sent = await channel.send(
                        embed=message.embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                        nonce=nonce,
                    )
                except Exception as exc:  # noqa: BLE001 (classified and re-raised below)
                    self._reraise_or_wrap(exc)
            except PublishError as exc:
                if not (self._skip_permanent and not exc.retryable):
                    raise
                self.skipped[message.topic_key] = str(exc)
                continue
            self._posted[message.topic_key] = sent.id
            if self._on_posted is not None:
                try:
                    await self._on_posted(message.topic_key, sent.id)
                except Exception:
                    # The message is out; failing the publish over a bookkeeping
                    # hiccup would post it again on the retry. The final save
                    # writes the full set anyway.
                    logger.exception("write-through of a posted game failed")
        return dict(self._posted)

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable:
        cached = self._channels.get(channel_id)
        if cached is not None:
            return cached
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception as exc:  # noqa: BLE001 (classified and re-raised below)
                self._reraise_or_wrap(exc)
        if not hasattr(channel, "send"):
            # A voice channel, a category, anything else that isn't
            # actually sendable: a misconfigured channel_id, not
            # something a retry ever fixes (design.md §13, D2).
            raise PublishError(
                f"channel {channel_id} can't receive messages (not a text channel)",
                posted_by_topic=self._posted,
                retryable=False,
            )
        if _foreign_channel(channel, self._guild_id, channel_id):
            raise PublishError(
                f"channel {channel_id} isn't in this server",
                posted_by_topic=self._posted,
                retryable=False,
            )
        self._channels[channel_id] = channel
        return channel

    def _reraise_or_wrap(self, exc: Exception) -> None:
        """Classify a send/fetch failure via the shared `_classify_send_error`.

        Every path through here carries `self._posted` (whatever this
        publisher already has an id back for), so a permanent error on
        one topic's channel doesn't erase what already landed for the
        topics before it.
        """
        _classify_publish_error(exc, posted_by_topic=self._posted)


_MISSING_MENTION_PERMISSION_ALERT = (
    "newsbot: a SHiFT code alert wanted to ping @everyone, but this bot's "
    "role is missing 'Mention @everyone, @here, and All Roles' in the "
    "SHiFT codes channel: Discord posts the message but silently drops "
    "the ping. Posted anyway; grant the permission (docs/deploy.md) if you "
    "want the next one to actually notify anyone."
)

_MISSING_ROLE_PERMISSION_ALERT = (
    "newsbot: a SHiFT code alert wanted to ping your chosen role, but that role "
    "isn't mentionable and this bot's role is missing 'Mention @everyone, @here, "
    "and All Roles' in the SHiFT codes channel: Discord posts the message but "
    "silently drops the ping. Posted anyway; make the role mentionable (or grant "
    "the permission) if you want the next one to actually notify anyone."
)


class DiscordCodeAlertPoster:
    """The `CodeAlertPoster` (`shift/sweep.py`) the real bot uses: one message per alert.

    Structurally the small sibling of `DiscordPublisher` above: same
    channel-resolution dance, same error classification (`_classify_send_error`,
    factored out of `DiscordPublisher._reraise_or_wrap` for exactly this
    reuse), but with none of that class's resumability bookkeeping,
    since `shift/fanout.py` already tracks per-message claim/post state of
    its own (`claim_guild_codes` and the `mark_guild_codes_*` functions) and
    only ever asks this to post one message at a time.

    `alert.ping` is the fan-out's call (`shift/fanout.py`), not this class's: all
    this does is turn that into the one `AllowedMentions` that's actually
    allowed to set `everyone=True` anywhere in this codebase (`_PING_EVERYONE`,
    A5), or `AllowedMentions.none()` otherwise. Before honoring a ping, it
    checks whether the bot's own role can actually mention `@everyone` in
    this channel: missing that permission doesn't stop the alert from
    still posting (the code itself is the important part), it just gets
    an admin alert instead of a silent, permission-dropped ping nobody
    would otherwise notice.
    """

    def __init__(
        self,
        client: NewsBot,
        channel_id: int,
        ping_choice: str = "everyone",
        notify: Callable[[str], Awaitable[None]] | None = None,
        guild_id: int | None = None,
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        # The server these alerts are for; a channel that isn't in it is never posted to.
        self._guild_id = guild_id
        # v3: one poster per server, so the ping is that server's choice
        # ("everyone", a role id, or "none") and a missing-permission note goes
        # to that server's admin channel via `notify`, never to the owner. The
        # defaults are exactly v2's: @everyone, told to the admin channel.
        self._ping_choice = ping_choice
        self._notify = notify if notify is not None else client.alert
        # Dedupes the missing-permission admin alert within one server's batch:
        # `begin_batch()` resets this at the start of each server's turn in
        # `shift/fanout.py`, so a batch that spills into several messages (or a
        # single message that `post_alert_with_retry` retries several times)
        # alerts an admin once per turn, not once per message and not once per retry.
        self._missing_permission_alerted = False

    def begin_batch(self) -> None:
        """Reset the missing-permission dedupe flag; called once per server's turn."""
        self._missing_permission_alerted = False

    @property
    def missing_ping_permission(self) -> bool:
        """True if this turn wanted a ping the bot's role can't deliver (the fan-out logs it)."""
        return self._missing_permission_alerted

    async def post(self, alert: RenderedAlert) -> int | None:
        """Send one alert message to the SHiFT codes channel and return its message id.

        Resolves and caches nothing (unlike `DiscordPublisher`, this class
        posts to one fixed channel, so there's no per-topic channel cache
        to keep); the channel is looked up fresh, then cached only on
        `self._client`'s own get/fetch path. Before honoring `alert.ping`,
        checks whether the bot's own role can actually mention `@everyone`
        in this channel (`_can_mention_everyone`); if not, it still posts
        with the ping's content intact (Discord silently drops the
        notification rather than the message) and sends one admin alert
        about the missing permission, deduped per turn by
        `self._missing_permission_alerted`. Any failure resolving the
        channel or sending the message goes through `_classify_send_error`,
        which raises `PublishError` for whatever's worth retrying (a 5xx, a
        network blip, a timeout) and re-raises everything else (a 4xx, a
        permissions problem) unwrapped, per `shift/fanout.py`'s own
        claim/post/record bookkeeping rather than `PublishError`'s.

        The returned message must say it landed in *this* channel. discord.py
        sends `enforce_nonce` with every nonce, and if Discord ever answers a send with some
        other message it deduped against, recording that as posted would lose
        a code without a trace. So a mismatch raises `ForeignMessageError`
        (final, no retry) and the code is marked failed instead.
        """
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(self._channel_id)
            except Exception as exc:  # noqa: BLE001 (classified and re-raised below)
                _classify_send_error(exc)

        if _foreign_channel(channel, self._guild_id, self._channel_id):
            raise ChannelNotInGuildError(f"channel {self._channel_id} isn't in this server")

        mentions = discord.AllowedMentions.none()
        if alert.ping:
            # Posts with the ping's intent either way: Discord just drops the
            # actual notification on its end when the bot's role lacks the
            # permission, same as the plan's owner checklist describes. Using
            # .none() instead would be strictly worse: it changes nothing about
            # who gets notified (still nobody) but throws away the chance the
            # permission gets granted *before* the next code shows up.
            mentions = mentions_for(self._ping_choice)
            if not self._can_ping(channel) and not self._missing_permission_alerted:
                self._missing_permission_alerted = True
                await self._notify(
                    _MISSING_MENTION_PERMISSION_ALERT
                    if self._ping_choice == "everyone"
                    else _MISSING_ROLE_PERMISSION_ALERT
                )

        try:
            message = await channel.send(
                alert.content, allowed_mentions=mentions, nonce=alert.nonce or None
            )
        except Exception as exc:  # noqa: BLE001 (classified and re-raised below)
            _classify_send_error(exc)
        # Fakes and old objects without a channel are skipped; a real message always has one.
        landed_in = getattr(getattr(message, "channel", None), "id", None)
        if landed_in is not None and landed_in != self._channel_id:
            logger.error(
                "SHiFT alert send returned a message from another channel",
                extra={"guild_id": self._guild_id, "channel_id": self._channel_id},
            )
            raise ForeignMessageError(
                f"send to channel {self._channel_id} returned a message from channel {landed_in}"
            )
        return message.id

    def _can_ping(self, channel: object) -> bool:
        """Whether this poster's ping choice can actually notify anyone in `channel`."""
        if self._ping_choice == "everyone":
            return self._can_mention_everyone(channel)
        if self._can_mention_everyone(channel):
            return True  # that permission covers every role, mentionable or not
        guild = getattr(channel, "guild", None)
        get_role = getattr(guild, "get_role", None)
        # Same rule as `mentions_for`, so a value that pings nobody can't raise here.
        is_role = type(self._ping_choice) is str and _ROLE_ID.fullmatch(self._ping_choice)
        if get_role is None or not is_role:
            return False
        role = get_role(int(self._ping_choice))
        return bool(role is not None and getattr(role, "mentionable", False))

    @staticmethod
    def _can_mention_everyone(channel: object) -> bool:
        """Whether this bot's role can actually ping `@everyone` in `channel`.

        `channel.guild.me` is `None` for a DM (not a real deployment
        shape here, but cheap to guard) or before the gateway has cached
        the guild member: either way, "can't tell" reads as "can't", the
        safe direction: it still posts, it just also alerts.
        """
        guild = getattr(channel, "guild", None)
        me = getattr(guild, "me", None) if guild is not None else None
        if me is None:
            return False
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for is None:
            return False
        return bool(permissions_for(me).mention_everyone)


# How long `is_owner` waits on Discord before saying no.
_OWNER_LOOKUP_TIMEOUT_S = 10.0

# How long one server's member chunk gets before we give up on it and move on.
# A request that never completes would otherwise stall every server after it.
_LOUNGE_CHUNK_TIMEOUT_S = 60.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NewsBot(discord.Client):
    """The bot process: gateway client, command tree, scheduler and job callbacks in one place.

    Holds the wiring and nothing clever. One `Router` decides who hears about
    what (`self.router`); one `GuildDigestDeps` (`guild_digest_deps()`) is
    shared by the minute job and every command, because the per-server locks
    that keep run-now, preview and the schedule from tripping over each other
    live inside it; and the SHiFT hook, the summaries and the collection pass
    all get their owner-alert and per-server-notice paths from that same router.
    Command registration is `bot/registration.py`'s job, called from
    `setup_hook`.
    """

    def __init__(
        self,
        cfg: AppConfig,
        secrets: Secrets,
        db_path: str,
        *,
        lounges: list[LoungeSettings] | None = None,
        intents: discord.Intents | None = None,
        import_report: ImportReport | None = None,
    ) -> None:
        # `lounges` is `repo.list_lounges` as of startup (after the import and
        # the D5 re-sync). The intents follow those rows, and members are
        # chunked per lounge guild in `chunk_lounge_guilds` instead of for every
        # guild at connect time. `__main__` builds the intents itself, as a
        # visible step in the startup order; left out, they're built from the rows here.
        lounge_rows = list(lounges or [])
        super().__init__(
            allowed_mentions=discord.AllowedMentions.none(),
            intents=intents if intents is not None else build_intents_for_lounges(lounge_rows),
            chunk_guilds_at_startup=False,
        )
        self.cfg = cfg
        self.secrets = secrets
        self.db_path = db_path
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self._on_command_error

        self.http_client: httpx.AsyncClient | None = None
        self.llm: LLMClient | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self._ready_once = False
        # The every-minute digest job does nothing until `on_ready` flips this.
        self._digests_enabled = False
        # The summaries job runs on its interval and once at startup; whichever gets here
        # second skips instead of making the same summaries twice.
        self._summaries_running = False
        self._last_two_instance_alert: datetime | None = None

        # Reddit's cross-call gap is honored across every collection pass,
        # not reset fresh each time one happens to run.
        self._rate_limit_state = RateLimitState()
        # `ensure_imported`'s report, if this start did the import; the first
        # `on_ready` tells the owner once and then forgets it.
        self._import_report = import_report
        # Legacy (pre-import) codes `fail_pending_codes()` flipped from
        # 'pending' to 'failed' at startup: a v2.2 process claimed them and
        # the ping budget, then died before confirming the send landed.
        # Reported once on the first on_ready, then never referenced again.
        self._interrupted_codes: list[str] = []
        # True once a job's crash has alerted the owner; cleared by the next
        # clean run, so a job that fails every interval pages once instead of
        # once an interval.
        self._crash_alerted: set[str] = set()
        # Filled on the first `is_owner` call; never guessed from config.
        self._owner_id: int | None = None
        # The one router: owner alerts, server notices and run reports all go
        # through it. `client_sender` plus `home_guild_id` is what switches on
        # the "this channel really belongs to that server" check at send time.
        self.router = Router(
            db_path, client_sender(self), cfg.admin_channel_id, home_guild_id=cfg.home_guild_id
        )
        # Join, remove and channel-delete handlers. A separate class so the
        # events aren't dispatched by name to code that has no rows to write.
        self.lifecycle = GuildLifecycle(db_path, cfg, self.router.alert_owner)
        # The per-guild lounge path (design.md §15, plan 3.11). The welcome map
        # is keyed (guild_id, user_id), the quote locks by guild, and
        # `guild_notifier` is where a lounge problem goes (the router's
        # `notify_guild`).
        self._lounges: dict[int, LoungeSettings] = {
            lounge.guild_id: lounge for lounge in lounge_rows
        }
        self._guild_welcomes = RecentWelcomes()
        self._guild_quote_locks: dict[int, asyncio.Lock] = {}
        self.guild_notifier: Callable[[int, str], Awaitable[None]] | None = self.router.notify_guild
        # Built in `setup_hook`, once the httpx and Anthropic clients exist.
        self._fanout_deps: FanoutDeps | None = None
        self._collection_deps: CollectionDeps | None = None
        self._summary_deps: SummaryDeps | None = None
        self._digest_deps: GuildDigestDeps | None = None

    async def is_owner(self, user: discord.abc.User) -> bool:
        """True iff `user` owns this application (the team's owner, for a team-owned one).

        `discord.Client` doesn't have this (only `commands.Bot` does, and this isn't
        one), so it asks Discord who owns the application and remembers the answer. A
        failed lookup says no: `/owner` is locked unless we can prove it isn't.
        """
        if self._owner_id is None:
            # Any failure says no, a hang included: `/owner` staying shut for
            # a minute beats a slash command that never answers.
            try:
                app = await asyncio.wait_for(
                    self.application_info(), timeout=_OWNER_LOOKUP_TIMEOUT_S
                )
            except discord.HTTPException, OSError, TimeoutError:
                logger.warning("couldn't look up the application owner")
                return False
            self._owner_id = app.team.owner_id if app.team else app.owner.id
        return user.id == self._owner_id

    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Log a failed command and tell the person who ran it.

        Without this, a handler that raises after `defer()` leaves Discord
        showing "thinking..." until the heat death of the interaction token,
        which is how the first live `/newsbot status` announced its bug.
        """
        logger.error(
            "command failed",
            extra={"command": getattr(interaction.command, "qualified_name", None)},
            exc_info=error,
        )
        original = getattr(error, "original", error)
        if (
            isinstance(original, discord.HTTPException)
            and original.code in _TWO_INSTANCE_ERROR_CODES
        ):
            await self._maybe_alert_two_instances(original.code)
        message = "Something went wrong running that command. The details are in the bot's log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.warning("couldn't report a command failure back to the user")

    async def _maybe_alert_two_instances(self, error_code: int) -> None:
        """Alert the admin channel that another process may be using this bot's token.

        Rate-limited to once an hour (`_should_alert_two_instances`) so a
        genuine collision (which shows up as a steady stream of 10062s
        and 40060s, one per lost race, not a single one) doesn't turn
        into an alert storm on top of the collision itself.
        """
        now = datetime.now(UTC)
        if not _should_alert_two_instances(self._last_two_instance_alert, now):
            return
        self._last_two_instance_alert = now
        logger.error(
            "possible two-instance collision on this bot token",
            extra={
                "discord_error_code": error_code,
                "instance_id": _INSTANCE_ID,
                "hostname": _HOSTNAME,
            },
        )
        await self.alert(
            f"newsbot: got Discord error code {error_code} responding to an interaction, "
            "which usually means another process is using this bot's token. "
            f"This instance: {_HOSTNAME} ({_INSTANCE_ID})."
        )

    async def setup_hook(self) -> None:
        """Wire up everything that needs a live event loop, in the order that matters.

        Runs after discord.py has created its event loop but before the
        gateway connection starts spinning, which is the only window where
        the scheduler started here binds to the same loop the gateway
        actually runs on (see this module's own docstring). Order within
        this function matters too: the clients and the shared deps come
        first (everything below needs them); commands are registered and
        synced next, since there's no reason to hold up the ones that don't
        need the scheduler; the legacy `fail_pending_codes` runs before any
        job exists, so a code a v2.2 process claimed and never confirmed is
        already flipped to `'failed'` before anything else can touch
        `alerted_codes`; and `self.scheduler.start()` is the last line, so
        nothing fires before the rest of setup has actually finished.

        The per-server pieces of startup (the import notice, pending-code
        notices, reconciliation, the lounges, the permission sweep) need a
        connected client, so they live in `on_ready`.
        """
        # Imported here, not at module scope: registration.py imports NewsBot
        # (for type hints on its factories' `bot` argument), and importing it
        # back at module scope would make a circular import out of what is
        # otherwise a plain layering.
        from newsbot.bot import registration

        logger.info("newsbot starting", extra={"instance_id": _INSTANCE_ID, "hostname": _HOSTNAME})
        warn_if_contact_unset()

        self.http_client = httpx.AsyncClient(headers=user_agent_headers())
        self.llm = AnthropicLLM(self.secrets.anthropic_api_key.get_secret_value())
        self._wire()

        await registration.setup_commands(self)

        # Pre-import rows only: a v2.2 process could have died mid-send. Per-server
        # claims are recovered (and their servers told) in `on_ready`.
        self._interrupted_codes = await asyncio.to_thread(self._fail_pending_codes_sync)

        tz = ZoneInfo(self.cfg.owner_report.timezone)
        self.scheduler = AsyncIOScheduler(timezone=tz)
        # The hourly shared collection is added in `on_ready` (`_start_collection_job`):
        # its SHiFT fan-out needs the guild cache, and a pass before it has
        # filled would see no bot member and cry "missing permission" at servers.
        # Every minute: whichever servers are due. Inert until `on_ready` is done.
        self.scheduler.add_job(
            self._digest_job,
            IntervalTrigger(minutes=1),
            id="guild-digests",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )
        # The comped servers' summaries, made ahead of time so a digest never
        # waits on Claude.
        self.scheduler.add_job(
            self._summaries_job,
            IntervalTrigger(minutes=5),
            id="summaries",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
        # 3:30 a.m. in the owner's zone: late enough that nobody's awake to
        # notice a slow purge.
        self.scheduler.add_job(
            self._retention_job,
            CronTrigger(hour=3, minute=30, timezone=tz),
            id="retention",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        report_at = _parse_digest_time(self.cfg.owner_report.time)
        self.scheduler.add_job(
            self._owner_report_job,
            CronTrigger(hour=report_at.hour, minute=report_at.minute, timezone=tz),
            id="owner-report",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._heartbeat_job,
            IntervalTrigger(seconds=_HEARTBEAT_INTERVAL_S),
            id="heartbeat",
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()

    def _wire(self) -> None:
        """Build the shared deps, once the httpx and Anthropic clients exist.

        Everything that tells a human something goes through `self.router`:
        the owner's alerts through `alert_owner`, a server's through
        `notify_guild`, a server's run report through `send_report`.
        """
        if self.http_client is None or self.llm is None:
            raise RuntimeError("_wire() called before the clients exist")
        router = self.router
        self._fanout_deps = FanoutDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            now=_utcnow,
            poster_for=self._poster_for,
            notify_guild=router.notify_guild,
            alert_owner=router.alert_owner,
        )
        # `collectors` is filled fresh before every pass (see `_collection_job`).
        self._collection_deps = CollectionDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            http=self.http_client,
            collectors=[],
            now=_utcnow,
            alert=router.alert_owner,
            rate_limit_state=self._rate_limit_state,
            shift_hook=make_shift_hook(self._fanout_deps),
        )
        brave = self.secrets.brave_api_key
        self._summary_deps = SummaryDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            llm=self.llm,
            now=_utcnow,
            alert=router.alert_owner,
            collection=self._collection_deps,
            brave_api_key=brave.get_secret_value() if brave is not None else None,
        )
        self._digest_deps = GuildDigestDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            now=_utcnow,
            publisher_for=self._publisher_for,
            notify_guild=router.notify_guild,
            summary_for=summary_lookup(self._summary_deps),
            preview_summary_for=dry_run_lookup(self._summary_deps),
            retry_summary=retry_lookup(self._summary_deps),
            send_report=router.send_report,
        )

    def guild_digest_deps(self) -> GuildDigestDeps:
        """The one `GuildDigestDeps` the minute job and every command share.

        The same object every call, on purpose: the per-server locks live in
        it, and a fresh one per call would let run-now, preview and the
        schedule all publish at once.
        """
        if self._digest_deps is None:
            raise RuntimeError("guild_digest_deps() called before setup_hook() finished")
        return self._digest_deps

    def _publisher_for(
        self,
        guild: GuildSettings,
        scope: str,
        already: dict[str, int],
        on_posted: Callable[[str, int], Awaitable[None]],
    ) -> DiscordPublisher:
        return DiscordPublisher(
            self,
            nonce_scope=scope,
            on_posted=on_posted,
            already_posted=already,
            skip_permanent=True,
            guild_id=guild.guild_id,
        )

    def _poster_for(
        self, guild_id: int, channel_id: int, ping: str, notify: Callable[[str], Awaitable[None]]
    ) -> DiscordCodeAlertPoster:
        return DiscordCodeAlertPoster(self, channel_id, ping, notify, guild_id=guild_id)

    def _fail_pending_codes_sync(self) -> list[str]:
        with closing(connect(self.db_path)) as conn:
            return repo.fail_pending_codes(conn)

    async def on_ready(self) -> None:
        """First-connection-only startup work, in the order that matters.

        Guarded by `self._ready_once`: discord.py fires `on_ready` on every
        reconnect, and none of this should repeat just because a network
        blip forced a new gateway session. On the genuine first connection:

        1. the import notice, if this start did the import, and a notice for today's
           digest if v2.2 left it pending or half-posted;
        2. reconciliation, so servers the bot left are dropped and servers it
           joined while down get a row, then a check that the owner's admin channel and
           home server are set sensibly (it complains loudly and carries on);
        3. SHiFT codes a crash left pending (the owner hears a count, each
           server hears its own), then whatever is still queued is delivered;
        4. the lounges: reload, chunk members, schedule the quotes;
        5. the permission sweep, with its counts to the owner;
        6. only then, the minute digest job is switched on, the hourly collection
           is scheduled (its first pass is a breath later, after the startup
           delivery above has finished, so the two can't overlap), and the
           summaries job gets its first run right away instead of waiting out
           its five minutes.

        Each step is wrapped on its own and only logs when it blows up: a bug
        in one of them must never be the thing that stops the bot from
        starting, or leaves every server without a digest.
        """
        # discord.py fires on_ready on every reconnect, not just the first
        # connection: without this flag, a network blip a week into
        # uptime would re-run all of this.
        if self._ready_once:
            return
        self._ready_once = True
        await self._startup_step("import notice", self._send_import_notice)
        await self._startup_step("stuck v2.2 digest", self._report_stuck_v22_digest)
        await self._startup_step("reconcile", self._reconcile)
        await self._startup_step("owner channel check", self._check_owner_channel)
        await self._startup_step("pending codes", self._recover_codes)
        await self._startup_step("lounge reload", self.reload_lounges)
        await self._startup_step("lounge chunking", self.chunk_lounge_guilds)
        await self._startup_step("lounge quotes", self.schedule_lounge_quotes)
        await self._startup_step("permission sweep", self._permission_sweep)
        self._digests_enabled = True
        logger.info("startup finished; per-server digests are on")
        await self._startup_step("collection job", self._start_collection_job)
        # The five-minute job's first turn is five minutes away, and a deploy at 08:58 would
        # otherwise reach 09:00 with no summary ready: the digest makes one inline, which never
        # searches the web, and posts a "reduced coverage" footer. So run it once now, after the
        # minute job is on (a slow model call mustn't hold up a due digest).
        await self._startup_step("summaries", self._summaries_job)

    async def _start_collection_job(self) -> None:
        """Schedule the hourly collection; only `on_ready` calls this, once.

        Starting it here (not in `setup_hook`) is what guarantees no pass runs
        before the guild cache is warm and startup's SHiFT delivery is done.
        """
        if self.scheduler is None:
            raise RuntimeError("_start_collection_job() called before setup_hook() finished")
        tz = ZoneInfo(self.cfg.owner_report.timezone)
        self.scheduler.add_job(
            self._collection_job,
            id="collection",
            next_run_time=datetime.now(tz) + _COLLECTION_FIRST_RUN_DELAY,
            **collection_job_options(self.cfg),
        )

    async def _startup_step(self, name: str, step: Callable[[], Awaitable[object]]) -> None:
        try:
            await step()
        except Exception as exc:
            logger.exception("startup step %r failed", name)
            await self.alert(f"newsbot: startup step {name!r} failed: {_alert_reason(exc)}")

    async def _send_import_notice(self) -> None:
        report = self._import_report
        if report is not None:
            await self.alert(report.owner_notice())
            self._import_report = None

    def _stuck_v22_sync(self, now: datetime) -> list[tuple[int, str, bool]]:
        """Today's v2.2 digest rows (imported server only) that nobody can safely finish.

        A v2.2 row has no window, which is how it's told from one v3 wrote (v3 resumes its own).
        `pending` means v2.2 died before it finished; `failed` with something posted means a
        half-posted day. v3 won't post over either, so the server has to be told, once: each
        is remembered in `app_state` so a restart doesn't repeat it. Returns
        `(guild_id, run_date, has_admin_channel)` for the ones not yet told.
        """
        stuck: list[tuple[int, str, bool]] = []
        with closing(connect(self.db_path)) as conn:
            for guild in repo.list_guilds(conn):
                if guild.imported_at is None:
                    continue
                try:
                    today = local_run_date(now, guild.timezone)
                except ZoneInfoNotFoundError, ValueError, OSError:
                    continue
                row = repo.get_guild_digest(conn, guild.guild_id, today)
                if row is None or row.window_end is not None:
                    continue
                posted_any = bool(row.posted_by_game) or bool(row.posted_message_ids)
                if not (row.status == "pending" or (row.status == "failed" and posted_any)):
                    continue
                key = f"{_STUCK_V22_KEY}{guild.guild_id}:{today.isoformat()}"
                if repo.app_state_get(conn, key) is not None:
                    continue
                repo.app_state_set(conn, key, row.status)
                stuck.append(
                    (guild.guild_id, today.isoformat(), guild.admin_channel_id is not None)
                )
        return stuck

    async def _report_stuck_v22_digest(self) -> None:
        """Tell a server (or the owner, if it has no admin channel) that v2.2 left its digest stuck.

        v2.2 alerted its admin channel about this at startup; v3 skips such a row, correctly,
        and without this the friend's digest just wouldn't arrive until somebody noticed.
        """
        for guild_id, run_date, has_admin_channel in await asyncio.to_thread(
            self._stuck_v22_sync, _utcnow()
        ):
            text = (
                f"newsbot: {run_date}'s digest from the previous version was left unfinished "
                "when it stopped, so I won't post over it. Check the game channels, and if it "
                "didn't land, `/newsbot run-now` will post it."
            )
            await self.router.notify_guild(guild_id, text)
            if not has_admin_channel:
                await self.alert(f"server {guild_id}: {text}")

    async def _recover_codes(self) -> None:
        """Tell people about codes a crash left pending, then deliver what's still queued."""
        if self._interrupted_codes:
            # A one-time read: this list isn't re-checked on a later reconnect.
            await self.alert(
                "newsbot: found SHiFT code(s) left 'pending' from a prior crash: "
                + ", ".join(self._interrupted_codes)
                + ". They were never confirmed posted; check the SHiFT codes channel."
            )
        if self._fanout_deps is None:
            raise RuntimeError("_recover_codes() called before setup_hook() finished")
        # Each server hears about its own; the owner hears how many, and nothing
        # about whose.
        recovered = await recover_pending_guild_codes(self.db_path, self.router.notify_guild)
        if recovered:
            await self.alert(
                f"newsbot: {recovered} SHiFT code alert(s) were in flight when the bot last "
                "stopped. They may or may not have posted; each server was told."
            )
        await deliver_queued_codes(self._fanout_deps, startup=True)

    async def _reconcile(self) -> None:
        await self.lifecycle.reconcile(self)

    async def _check_owner_channel(self) -> None:
        """Say so, loudly, if the owner's alert channel or home server is misconfigured.

        Every bot-wide alert goes to `admin_channel_id`, which has to be in `home_guild_id`
        (the router refuses to post it anywhere else). Get either wrong and the owner hears
        nothing, which looks exactly like a quiet day. So at startup: the log gets an ERROR
        and stderr gets the same line, each with a one-line fix. It never exits, because the
        friend's digests are working fine and shouldn't pay for the owner's typo.
        """
        cfg = self.cfg
        servers = await asyncio.to_thread(self._server_count_sync)
        problems: list[str] = []
        if cfg.home_guild_id is None and servers > 1:
            problems.append(
                f"home_guild_id isn't set and I'm in {servers} servers, so nothing can tell "
                "which one is yours. Fix: set home_guild_id in config.yaml to your own server's id."
            )
        if cfg.admin_channel_id is None:
            if servers > 1:
                problems.append(
                    "admin_channel_id isn't set, so bot-wide alerts go nowhere. "
                    "Fix: set admin_channel_id in config.yaml to a channel in your home server."
                )
        else:
            channel = self.get_channel(cfg.admin_channel_id)
            if channel is None:
                problems.append(
                    f"admin_channel_id {cfg.admin_channel_id} isn't a channel I can see. "
                    "Fix: set it to a channel in your home server that I can view."
                )
            elif cfg.home_guild_id is not None:
                found = getattr(getattr(channel, "guild", None), "id", None)
                if found != cfg.home_guild_id:
                    problems.append(
                        f"admin_channel_id {cfg.admin_channel_id} isn't in home_guild_id "
                        f"{cfg.home_guild_id}. Fix: set admin_channel_id to a channel "
                        "in your home server."
                    )
        for problem in problems:
            message = f"newsbot: owner alerts won't arrive: {problem}"
            logger.error(message)
            print(message, file=sys.stderr)

    def _server_count_sync(self) -> int:
        with closing(connect(self.db_path)) as conn:
            return repo.guild_counts(conn)["servers"]

    async def _permission_sweep(self) -> None:
        """Check every set-up server's channels; each is told on change, the owner gets counts."""
        names = {game.key: game.name for game in self.cfg.catalog}
        result = await sweep_guild_permissions(
            self, self.db_path, self.router.notify_guild, game_names=names
        )
        line = render_sweep_counts(result)
        if line:
            await self.alert(line)

    async def alert(self, text: str) -> None:
        """Tell the owner something bot-wide (their channel in the home guild). Never raises."""
        await self.router.alert_owner(text)

    # --- The jobs ---

    async def _alert_once(self, key: str, text: str) -> None:
        """Alert the owner about a job crash, once, until that job next runs clean."""
        if key not in self._crash_alerted:
            self._crash_alerted.add(key)
            await self.alert(text)

    async def _collection_job(self) -> None:
        """The hourly shared collection pass; nothing raised in here may take the process down.

        The collectors are rebuilt every pass, and for an extra reason specific
        to Bluesky: its session JWT lasts about two hours with no refresh, so a
        pass that logs in again is what keeps that source working pass after pass.
        A crash alerts the owner the first time only; the next clean pass re-arms it.
        """
        deps = self._collection_deps
        if deps is None:
            raise RuntimeError("_collection_job() ran before setup_hook() finished")
        try:
            deps.collectors = build_collection_collectors(self.cfg, self.secrets)
            outcome = await run_collection(deps)
            if outcome.skipped:
                logger.info("collection pass skipped: run lock held")
            else:
                self._crash_alerted.discard("collection")
                logger.info(
                    "collection pass finished",
                    extra={
                        "summary": outcome.summary,
                        "duration_s": round(outcome.duration_s, 1),
                    },
                )
        except Exception as exc:  # the job boundary
            logger.exception("collection pass crashed at the job boundary")
            await self._alert_once(
                "collection", f"newsbot: collection pass crashed: {_alert_reason(exc)}"
            )

    async def _digest_job(self) -> None:
        """The every-minute tick: run the digest of every server that's due.

        A no-op until the first `on_ready` has finished its startup work (the
        channel cache isn't ready before then). `run_due_guilds` already
        isolates one server's failure from the next; this is the boundary for
        whatever it can't (a locked database, say), and it alerts once, not once a minute.
        """
        if not self._digests_enabled:
            return
        try:
            outcomes = await run_due_guilds(self.guild_digest_deps())
            self._crash_alerted.discard("digests")
            if outcomes:
                logger.info(
                    "digest tick finished",
                    extra={
                        "servers": len(outcomes),
                        "failed": sum(1 for o in outcomes if o.status == "failed"),
                    },
                )
        except Exception as exc:  # the job boundary
            logger.exception("digest tick crashed at the job boundary")
            await self._alert_once("digests", f"newsbot: digest tick crashed: {_alert_reason(exc)}")

    async def _summaries_job(self) -> None:
        """Every five minutes, and once at startup: make the summaries comped digests will need."""
        if self._summary_deps is None:
            raise RuntimeError("_summaries_job() ran before setup_hook() finished")
        if self._summaries_running:
            logger.info("summaries job skipped: another run is still going")
            return
        self._summaries_running = True
        try:
            done = await prepare_summaries(self._summary_deps)
            self._crash_alerted.discard("summaries")
            if done:
                logger.info("summaries prepared", extra={"games": done})
        except Exception as exc:  # the job boundary
            logger.exception("summaries job crashed at the job boundary")
            await self._alert_once(
                "summaries", f"newsbot: summaries job crashed: {_alert_reason(exc)}"
            )
        finally:
            self._summaries_running = False

    def _owner_report_sync(self, now: datetime, today: str) -> str | None:
        """The report text, or None if today's has already gone out.

        The date is written *before* sending (and after the numbers are
        gathered): a crash in between loses one report, which beats sending
        two. The day's digests are the newest row per server touched in the
        last 24 hours; `partial` counts as posted
        (it did post, with something skipped or degraded), and `pending` and
        `failed` are the failures, grouped by `outcome_from_digest`'s reasons.
        """
        with closing(connect(self.db_path)) as conn:
            if repo.app_state_get(conn, _OWNER_REPORT_KEY) == today:
                return None
            rows = repo.recent_guild_digests(conn, now - timedelta(hours=24))
            failing = repo.failing_sources(conn)
            repo.app_state_set(conn, _OWNER_REPORT_KEY, today)
        outcomes = [outcome_from_digest(status, notes) for status, notes in rows]
        return render_owner_report(outcomes, failing)

    async def _owner_report_job(self) -> None:
        """Once a day, at `owner_report.time`: the all-servers summary, to the owner."""
        try:
            now = datetime.now(UTC)
            today = now.astimezone(ZoneInfo(self.cfg.owner_report.timezone)).date().isoformat()
            text = await asyncio.to_thread(self._owner_report_sync, now, today)
            if text is not None:
                await self.alert(text)
        except Exception as exc:  # the job boundary
            logger.exception("owner report crashed at the job boundary")
            await self.alert(f"newsbot: daily report crashed: {_alert_reason(exc)}")

    # --- Gateway events ---

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.lifecycle.on_guild_join(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.lifecycle.on_guild_remove(guild)
        if self._digest_deps is not None:
            self._digest_deps.forget_guild(guild.id)
        # Its lounge row went with the guild's; the quote job would notice at its
        # next fire and remove itself, but there's no reason to wait.
        self._lounges.pop(guild.id, None)
        job_id = f"daily-quote-{guild.id}"
        if self.scheduler is not None and self.scheduler.get_job(job_id) is not None:
            self.scheduler.remove_job(job_id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.lifecycle.on_guild_channel_delete(channel)

    async def on_member_join(self, member: discord.Member) -> None:
        await self.handle_member_join(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Welcome a member who just got through rules screening (`pending` true to false).

        Nickname changes, role changes and everything else that fires this
        event are none of our business.
        """
        await self.handle_member_update(before, after)

    # --- The per-guild lounge (design.md §15, plan 3.11) ---
    #
    # Everything from here to `_retention_job` is the lounge. Settings come
    # from `guild_lounge` rows (the `lounge:` block in config.yaml only feeds
    # the imported server's row, at startup); a server with no row (or
    # welcomes off) is simply not part of the conversation.

    def _list_lounges_sync(self) -> list[LoungeSettings]:
        with closing(connect(self.db_path)) as conn:
            return repo.list_lounges(conn)

    async def _load_lounges(self) -> list[LoungeSettings]:
        rows = await asyncio.to_thread(self._list_lounges_sync)
        self._lounges = {row.guild_id: row for row in rows}
        return rows

    async def reload_lounges(self) -> list[LoungeSettings]:
        """Re-read every lounge row into the welcome lookup, then re-schedule the quote jobs.

        Call after the import, a re-sync or a settings change. The jobs
        follow the rows: a moved `quote_time` or zone moves its job, a
        re-enabled quote gets one back, and a removed or disabled server
        loses its own. Safe to call twice (job ids carry the guild). Before
        the scheduler exists there's nothing to re-schedule, and the rows
        are just loaded.
        """
        rows = await self._load_lounges()
        if self.scheduler is not None:
            await self._sync_quote_jobs(rows)
        return rows

    async def chunk_lounge_guilds(self) -> None:
        """Fill the member cache for lounge guilds with welcomes on, and only those.

        `on_member_update` only fires for cached members, and the client
        doesn't chunk at startup in this mode (hundreds of servers' member
        lists is a lot of memory for events nobody listens to). One failed
        chunk (an error or a timeout) is logged and skipped; the welcome for
        that server still works on joins.
        """
        for lounge in list(self._lounges.values()):
            if not lounge.welcome_enabled:
                continue
            guild = self.get_guild(lounge.guild_id)
            if guild is None or guild.chunked:
                continue
            try:
                await asyncio.wait_for(guild.chunk(), timeout=_LOUNGE_CHUNK_TIMEOUT_S)
            except Exception as exc:  # includes the timeout; the log line has the guild id only
                logger.warning(
                    "couldn't chunk members for lounge guild %s (%s)",
                    lounge.guild_id,
                    type(exc).__name__,
                )

    async def notify_lounge_guild(self, guild_id: int, text: str) -> None:
        """Tell one server's admins about a lounge problem. Never raises.

        No notifier wired means the text is dropped and a line
        with the guild id only is logged; it still never goes to the owner,
        because a stranger's broken welcome isn't the owner's page.
        """
        if self.guild_notifier is None:
            logger.warning("lounge notice for guild %s dropped: no notifier wired", guild_id)
            return
        try:
            await self.guild_notifier(guild_id, text)
        except Exception:
            logger.exception("couldn't send a lounge notice", extra={"guild_id": guild_id})

    def _welcoming_lounge(self, member: discord.Member) -> LoungeSettings | None:
        """The lounge that welcomes this member's server, if there is one.

        A member with no server (discord.py never builds one, but I have been
        wrong about "never" before) has nobody to tell and nowhere to welcome
        them, so it's the same as a server with no lounge.
        """
        guild_id = getattr(getattr(member, "guild", None), "id", None)
        lounge = self._lounges.get(guild_id) if guild_id is not None else None
        return lounge if lounge is not None and lounge.welcome_enabled else None

    async def handle_member_join(self, member: discord.Member) -> None:
        lounge = self._welcoming_lounge(member)
        if lounge is None:
            return
        await self._welcome_member(member, lounge, "join")

    async def handle_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Welcome a member who just got through rules screening, in a lounge guild only."""
        lounge = self._welcoming_lounge(after)
        if lounge is None:
            return
        if before.pending and not after.pending:
            await self._welcome_member(after, lounge, "accepted")

    async def _welcome_member(
        self, member: discord.Member, lounge: LoungeSettings, event: str
    ) -> None:
        """Decide, record, render and post one welcome; problems go to that server's admins.

        The log line carries the event, `pending`, `flags.value` and the
        decision, and never the user ID or name: I want to see how Onboarding
        behaves without keeping a guest list in the logs. The 24-hour map is
        keyed `(guild_id, user_id)`, so a welcome in one server doesn't use up
        the same person's welcome in another. The failure notice goes to this
        server's admins and names nobody.
        """
        guild_id = member.guild.id
        try:
            action = welcome_action(in_guild=True, is_bot=member.bot, pending=member.pending)
            decision: str = action
            if action == "welcome" and not self._guild_welcomes.check_and_record(
                (guild_id, member.id), datetime.now(UTC)
            ):
                decision = "recent"
            logger.info(
                "member event %s: guild=%s pending=%s flags=%s decision=%s",
                event,
                guild_id,
                member.pending,
                member.flags.value,
                decision,
                extra={
                    "welcome_event": event,
                    "guild_id": guild_id,
                    "pending": member.pending,
                    "flags": member.flags.value,
                    "decision": decision,
                },
            )
            if decision != "welcome":
                return
            text = render_welcome(
                lounge.welcome_message,
                member_mention=member.mention,
                server_name=member.guild.name,
            )
            channel = await self._channel_by_id(lounge.channel_id, guild_id)
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=[member], roles=False, replied_user=False
                ),
            )
        except Exception as exc:
            # No traceback, on purpose: it ends with the exception's message,
            # so nothing about a member reaches the logs.
            logger.error(
                "welcome failed in guild %s: %s%s", guild_id, type(exc).__name__, _http_detail(exc)
            )
            await self.notify_lounge_guild(
                guild_id,
                f"newsbot: couldn't post a welcome in the lounge ({type(exc).__name__}). "
                "Check that I can view and send in the lounge channel.",
            )

    async def _channel_by_id(self, channel_id: int, guild_id: int) -> discord.abc.Messageable:
        """The lounge channel, refused (`ChannelNotInGuildError`) if it isn't in `guild_id`."""
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if _foreign_channel(channel, guild_id, channel_id):
            raise ChannelNotInGuildError(f"channel {channel_id} isn't in this server")
        return channel  # type: ignore[return-value]

    def build_guild_quote_deps(self, lounge: LoungeSettings, timezone: str) -> QuoteDeps:
        """Assemble a fresh `QuoteDeps` for one server's quote run (its job or `/lounge quote-now`).

        Sources, channel and deck come from the row; `local_day` is today in
        the server's own zone (a bad zone raises `ZoneInfoNotFoundError` or
        `ValueError`, which `run_guild_quote` turns into a notice). `alert`
        is that server's notice path, so source trouble reaches its admins.
        """
        if self.http_client is None:
            raise RuntimeError("build_guild_quote_deps() called before setup_hook() finished")
        channel_id = lounge.channel_id

        async def post(text: str) -> int:
            channel = await self._channel_by_id(channel_id, lounge.guild_id)
            sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return sent.id

        return QuoteDeps(
            sources=[QuoteSourceCfg(**src) for src in lounge.quote_sources],
            db_path=self.db_path,
            http=self.http_client,
            local_day=local_run_date(datetime.now(UTC), timezone),
            now=lambda: datetime.now(UTC),
            post=post,
            alert=functools.partial(self.notify_lounge_guild, lounge.guild_id),
            rng=random.SystemRandom(),
            cache_dir=cache_dir_for(self.db_path),
            guild_id=lounge.guild_id,
        )

    def _quote_inputs_sync(self, guild_id: int) -> tuple[LoungeSettings | None, str | None]:
        """The guild's lounge row and time zone, read fresh (a re-sync or removal shows up)."""
        with closing(connect(self.db_path)) as conn:
            guild = repo.get_guild(conn, guild_id)
            lounge = repo.get_lounge(conn, guild_id)
        return lounge, guild.timezone if guild else None

    async def run_guild_quote(self, guild_id: int, force: bool) -> QuoteOutcome:
        """Run one server's quote under that server's lock (job and `/lounge quote-now` take turns).

        Different servers don't wait on each other. A server with no lounge
        row, no server row, or quotes turned off gets `no_lounge` and no
        noise.
        """
        lock = self._guild_quote_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            lounge, timezone = await asyncio.to_thread(self._quote_inputs_sync, guild_id)
            if lounge is None or timezone is None or not lounge.quote_enabled:
                logger.info("lounge quote for guild %s skipped: no lounge quote set up", guild_id)
                return QuoteOutcome("no_lounge")
            try:
                deps = self.build_guild_quote_deps(lounge, timezone)
            except ZoneInfoNotFoundError, ValueError, OSError:
                logger.warning("lounge quote for guild %s skipped: unusable time zone", guild_id)
                await self.notify_lounge_guild(
                    guild_id,
                    "newsbot: no lounge quote today, because this server's time zone "
                    "setting isn't usable. Fix it with /newsbot settings.",
                )
                return QuoteOutcome("skipped")
            return await run_daily_quote(deps, force=force)

    async def _guild_quote_job(self, guild_id: int) -> None:
        try:
            outcome = await self.run_guild_quote(guild_id, False)
            logger.info(
                "guild quote job finished",
                extra={"status": outcome.status, "guild_id": guild_id},
            )
            if outcome.status == "no_lounge" and self.scheduler is not None:
                # The server left or turned the quote off; its cron has nothing left to do.
                job_id = f"daily-quote-{guild_id}"
                if self.scheduler.get_job(job_id) is not None:
                    self.scheduler.remove_job(job_id)
        except Exception as exc:  # the job boundary: nothing here may take the process down
            logger.exception("guild quote job crashed at the job boundary")
            await self.notify_lounge_guild(
                guild_id, f"newsbot: daily quote job crashed: {_alert_reason(exc)}"
            )

    async def schedule_lounge_quotes(self) -> list[int]:
        """Reload the rows and make the quote jobs match them; return the scheduled guild ids."""
        if self.scheduler is None:
            raise RuntimeError("schedule_lounge_quotes() called before the scheduler exists")
        return await self._sync_quote_jobs(await self._load_lounges())

    async def _sync_quote_jobs(self, rows: list[LoungeSettings]) -> list[int]:
        """Add or move a cron job for every server whose quote is on, and drop the rest.

        Reads the zones fresh. A server whose zone won't load is told once
        and left out (its old job goes too) rather than taking the others
        down with it. Any `daily-quote-<guild id>` job not in the result is
        removed; the v2.2 `daily-quote` job has no guild suffix and isn't ours.
        """
        scheduler = self.scheduler
        if scheduler is None:
            return []
        scheduled: list[int] = []
        for lounge in rows:
            if not lounge.quote_enabled:
                continue
            _, timezone = await asyncio.to_thread(self._quote_inputs_sync, lounge.guild_id)
            if timezone is None:
                continue
            try:
                schedule_guild_quote(
                    scheduler,
                    lounge.guild_id,
                    lounge.quote_time,
                    timezone,
                    functools.partial(self._guild_quote_job, lounge.guild_id),
                )
            except ZoneInfoNotFoundError, ValueError, OSError:
                logger.warning("no quote job for guild %s: unusable time zone", lounge.guild_id)
                await self.notify_lounge_guild(
                    lounge.guild_id,
                    "newsbot: the daily quote isn't scheduled, because this server's time "
                    "zone setting isn't usable. Fix it with /newsbot settings.",
                )
                continue
            scheduled.append(lounge.guild_id)
        wanted = {f"daily-quote-{gid}" for gid in scheduled}
        for job in scheduler.get_jobs():
            suffix = job.id.removeprefix("daily-quote-")
            if suffix.isdigit() and job.id not in wanted:
                scheduler.remove_job(job.id)
        return scheduled

    async def _retention_job(self) -> None:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)
            deleted = await asyncio.to_thread(self._purge_sync, cutoff)
            logger.info(
                "retention purge finished",
                extra={
                    "items_deleted": deleted[0],
                    "stories_deleted": deleted[1],
                    "summaries_deleted": deleted[2],
                    "notices_deleted": deleted[3],
                },
            )
        except Exception as exc:
            logger.exception("retention job crashed")
            await self.alert(f"newsbot: retention job crashed: {_alert_reason(exc)}")

    def _purge_sync(self, cutoff: datetime) -> tuple[int, int, int, int]:
        """Items and stories, then (the public app's additions) summaries and server notices."""
        with closing(connect(self.db_path)) as conn:
            items, stories = repo.purge_older_than(conn, cutoff)
            summaries, notices = repo.purge_guild_data_older_than(conn, cutoff)
        return items, stories, summaries, notices

    async def _heartbeat_job(self) -> None:
        # Written iff the gateway is actually connected and the scheduler
        # (the thing whose absence would mean "nothing fires ever again")
        # is running. A process that's merely alive but disconnected from
        # Discord, or one whose scheduler silently died, should read as
        # unhealthy, not healthy-because-the-loop-is-still-spinning.
        if self.is_ready() and not self.is_closed() and self.scheduler and self.scheduler.running:
            await asyncio.to_thread(HEARTBEAT.write_text, str(datetime.now(UTC).timestamp()))

    async def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        if self.http_client is not None:
            await self.http_client.aclose()
        await super().close()


class GuildLifecycle:
    """Join, removal, channel-delete and startup reconciliation handlers (plan task 11).

    These are deliberately not methods on `NewsBot`: discord.py dispatches
    `on_guild_join` and friends by name, so defining them there would switch
    them on for code that has no `guilds` rows to write. `NewsBot` builds one of
    these and forwards the events to it.

    `alert_owner` is the owner alert path (`Router.alert_owner`). Logs carry
    guild ids only; a server's name is its own business.
    """

    def __init__(
        self,
        db_path: str,
        cfg: AppConfig,
        alert_owner: Callable[[str], Awaitable[None]],
    ) -> None:
        self.db_path = db_path
        self.cfg = cfg
        self._alert_owner = alert_owner

    def _create_row(self, guild_id: int) -> bool:
        """Insert the guild's row (comped if `comped_guild_ids` lists it). True if it's new."""
        tier = "comped" if guild_id in self.cfg.comped_guild_ids else "free"
        with closing(connect(self.db_path)) as conn:
            return repo.create_guild(conn, guild_id, tier=tier)

    async def on_guild_join(self, guild: discord.Guild) -> bool:
        """Create the row and, only if it was new, say hello once. True if the row was new.

        A duplicate join event finds the row and sends nothing. A removal
        deletes the row, so a real re-join is new again and gets one hello.
        """
        try:
            created = self._create_row(guild.id)
        except Exception:
            logger.exception("guild join: could not write the row", extra={"guild_id": guild.id})
            return False
        if not created:
            logger.info("guild join: row already exists", extra={"guild_id": guild.id})
            # A duplicate join still upgrades a listed free row; it never downgrades.
            self._comp_one(guild.id)
            return False
        logger.info("guild join: new server %d", guild.id, extra={"guild_id": guild.id})
        await self._say_hello(guild)
        return True

    async def _say_hello(self, guild: discord.Guild) -> None:
        """One attempt at the hello. Logs and swallows any failure (cancellation excepted)."""
        try:
            channel = lifecycle.pick_first_contact_channel(guild)
            if channel is None:
                logger.info(
                    "guild %d: nowhere the bot may speak; no first-contact message", guild.id
                )
                return
            async with asyncio.timeout(lifecycle.HELLO_TIMEOUT):
                await channel.send(
                    lifecycle.FIRST_CONTACT_TEXT, allowed_mentions=lifecycle.allowed_mentions()
                )
        except Exception:
            # Permissions change between the check and the send, sockets reset,
            # and sends occasionally never come back (the timeout). One attempt,
            # then quiet: the hello is a nicety, not a promise, and it must not
            # take reconcile's remaining servers down with it.
            logger.warning("guild %d: first-contact message failed", guild.id, exc_info=True)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Delete the guild's rows now. Shared items, stories and summaries stay."""
        try:
            with closing(connect(self.db_path)) as conn:
                deleted = repo.delete_guild(conn, guild.id)
        except Exception:
            logger.exception("guild remove: delete failed", extra={"guild_id": guild.id})
            return
        logger.info("guild remove: %d (rows deleted: %s)", guild.id, deleted)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """If a saved setting used this channel, add one notice naming the use.

        Settings stay as they are; nothing picks a replacement. The notice is
        there so an admin finds out before the next digest skips the game.
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        names = {g.key: g.name for g in self.cfg.catalog}
        try:
            with closing(connect(self.db_path)) as conn:
                for req in requirements_from_db(conn, guild.id, game_names=names):
                    if req.channel_id == channel.id:
                        repo.add_notice(
                            conn,
                            guild.id,
                            f"A channel used for {req.purpose} was deleted, so that is skipped "
                            "until you pick a new one with /newsbot setup.",
                        )
                        return
        except Exception:
            logger.exception("channel delete: notice failed", extra={"guild_id": guild.id})

    async def reconcile(self, client: discord.Client) -> lifecycle.ReconcilePlan | None:
        """Startup: drop rows for servers the bot left, add rows for ones it missed, apply comping.

        Call it from the first `on_ready`, after the import. Does nothing
        (returns None) if the client isn't ready, since a half-filled guild
        cache is exactly how a cleanup wipes the wrong servers. See
        `lifecycle.reconcile_plan` for the valve; when it trips, the stale rows
        are checked with Discord one at a time (capped per start) and only the
        confirmed-gone ones are deleted, then the owner gets counts. Guilds the
        bot is in but has no row for get the join treatment, hello included,
        per plan section 3.8.

        The plan is a snapshot and this method awaits Discord between steps, so
        every create and delete re-asks `client.get_guild` right before it
        acts. No await sits between that check and the write, which makes the
        pair atomic on the event loop; a lock held across the hello or the
        fetch would only make join events queue behind a slow send. Each step
        runs on its own so one failure can't skip the rest.
        """
        if not client.is_ready():
            logger.warning("reconcile: client not ready; skipping")
            return None
        # `client.guilds` includes guilds Discord has marked unavailable,
        # which is what "unavailable counts as present" needs.
        live = {g.id: g for g in client.guilds}
        with closing(connect(self.db_path)) as conn:
            rows = repo.list_guilds(conn)
        plan = lifecycle.reconcile_plan(
            [r.guild_id for r in rows],
            live.keys(),
            protected_ids=[r.guild_id for r in rows if r.imported_at is not None],
        )
        if plan.valve_tripped:
            logger.warning("reconcile: cleanup paused, %s", plan.valve_reason)
            deleted, unconfirmed, skipped = await self._self_heal(client, plan.to_verify)
            await self._alert_owner(
                f"Startup cleanup paused: {plan.valve_reason}. Asked Discord about the stale "
                f"servers: {deleted} confirmed gone and deleted, {unconfirmed} still unconfirmed "
                f"(kept), {skipped} skipped over the limit of {lifecycle.VALVE_CHECK_CAP} per "
                "start (the next start checks more)."
            )
        deletes = list(plan.to_delete)
        for guild_id in plan.to_confirm:
            if await self._confirmed_gone(client, guild_id):
                deletes.append(guild_id)
        for guild_id in deletes:
            self._delete_if_still_gone(client, guild_id)
        for guild_id in plan.to_create:
            try:
                guild = client.get_guild(guild_id)
                if guild is None:
                    logger.info("reconcile: server %d left mid-run; no row created", guild_id)
                    continue
                await self.on_guild_join(guild)
            except Exception:
                logger.exception("reconcile: create failed", extra={"guild_id": guild_id})
        self._apply_comping()
        return plan

    async def _self_heal(
        self, client: discord.Client, stale: tuple[int, ...]
    ) -> tuple[int, int, int]:
        """Valve tripped: ask Discord about stale rows, delete the confirmed ones.

        Sequential, at most `VALVE_CHECK_CAP` per call, with a pause between
        asks. Returns (deleted, still unconfirmed, skipped over the cap).
        """
        batch = stale[: lifecycle.VALVE_CHECK_CAP]
        deleted = 0
        for n, guild_id in enumerate(batch):
            if n:
                await asyncio.sleep(lifecycle.VALVE_CHECK_PAUSE)
            if await self._confirmed_gone(client, guild_id) and self._delete_if_still_gone(
                client, guild_id
            ):
                deleted += 1
        return deleted, len(batch) - deleted, len(stale) - len(batch)

    def _delete_if_still_gone(self, client: discord.Client, guild_id: int) -> bool:
        """Delete the row unless the bot has (re)joined since the plan was made. True if deleted."""
        if client.get_guild(guild_id) is not None:
            logger.info("reconcile: server %d is back; row kept", guild_id)
            return False
        try:
            with closing(connect(self.db_path)) as conn:
                repo.delete_guild(conn, guild_id)
        except Exception:
            logger.exception("reconcile: delete failed", extra={"guild_id": guild_id})
            return False
        logger.info("reconcile: removed stale server %d", guild_id)
        return True

    async def _confirmed_gone(self, client: discord.Client, guild_id: int) -> bool:
        """Ask Discord directly whether the bot is out of `guild_id`; any doubt says no."""
        try:
            async with asyncio.timeout(lifecycle.FETCH_TIMEOUT):
                await client.fetch_guild(guild_id)
        except discord.NotFound, discord.Forbidden:
            return True
        except Exception:
            # Includes the timeout: no answer is not an answer, so the row stays.
            logger.warning("reconcile: couldn't confirm server %d is gone", guild_id, exc_info=True)
            return False
        return False

    def _comp_one(self, guild_id: int) -> None:
        """D6: a listed guild whose row is free becomes comped. Never downgrades, never raises."""
        if guild_id not in self.cfg.comped_guild_ids:
            return
        try:
            with closing(connect(self.db_path)) as conn:
                row = repo.get_guild(conn, guild_id)
                if row is not None and row.tier == "free":
                    repo.update_guild_settings(conn, guild_id, tier="comped")
                    logger.info("server %d comped (comped_guild_ids)", guild_id)
        except Exception:
            logger.exception("comping failed", extra={"guild_id": guild_id})

    def _apply_comping(self) -> None:
        """D6: listed guilds that have a row and are free become comped. Never downgrades."""
        if not self.cfg.comped_guild_ids:
            return
        try:
            with closing(connect(self.db_path)) as conn:
                tiers = {r.guild_id: r.tier for r in repo.list_guilds(conn)}
        except Exception:
            logger.exception("reconcile: comping could not read the guild rows")
            return
        for guild_id in lifecycle.comp_candidates(self.cfg.comped_guild_ids, tiers):
            self._comp_one(guild_id)


__all__ = [
    "HEARTBEAT",
    "DiscordCodeAlertPoster",
    "DiscordPublisher",
    "GuildLifecycle",
    "NewsBot",
    "build_intents_for_lounges",
    "mentions_for",
    "schedule_guild_quote",
]
