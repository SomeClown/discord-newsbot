"""The discord.py client: gateway connection, scheduler wiring, and the heartbeat file.

Everything before this module could be tested without a bot token, a guild,
or an event loop discord.py owns. This is where that ends: `NewsBot`
holds the httpx client, the Anthropic client, the slash-command tree and
the APScheduler instance that drives the daily job, and it has to bring
all of them up in the right order relative to discord.py's own connection
lifecycle.

The one rule that matters most here: **the scheduler has to start inside
`setup_hook`**, not in `__main__` before `client.run()` and not lazily on
first use. `setup_hook` runs after discord.py has created (but not yet
started spinning) its event loop, which is the only point where "the loop
APScheduler binds to" and "the loop discord.py's gateway actually runs on"
are guaranteed to be the same loop. Get this wrong and the daily job fires
into a loop nobody's listening on: a mistake that is, by design, invisible
until 9 a.m. the first morning it matters.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
import httpx
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from discord import app_commands

from newsbot.alerts import send_alert
from newsbot.bot.format import RenderedAlert, RenderedDigest, esc
from newsbot.bot.permissions import check_channels, render_permission_alert
from newsbot.collectors.base import RateLimitState, build_collectors
from newsbot.config import AppConfig, Secrets
from newsbot.lounge.daily import QuoteDeps, QuoteOutcome, run_daily_quote
from newsbot.lounge.sources import cache_dir_for
from newsbot.lounge.welcome import RecentWelcomes, render_welcome, welcome_action
from newsbot.pipeline.publisher import PublishError
from newsbot.pipeline.run import Deps, RunKind, RunMode, local_run_date, run_daily
from newsbot.pipeline.summarize import AnthropicLLM, LLMClient
from newsbot.shift.sweep import CodeAlertPoster, SweepDeps, run_code_sweep
from newsbot.store.db import connect
from newsbot.store.models import DigestRow
from newsbot.store.repo import fail_pending_codes, get_digest, purge_older_than
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
_HEARTBEAT_INTERVAL_S = 60
_PENDING_STARTUP_ALERT = (
    "newsbot: found a 'pending' digest row at startup. That usually means "
    "the process crashed mid-run last time (it may or may not have "
    "already posted). Check the game channels and `/newsbot status`, then "
    "`/newsbot run-now` if you want to retry: it'll ask you to confirm "
    "before posting again, since we can't tell whether today already went out."
)
_PARTIAL_FAILURE_STARTUP_ALERT = (
    "newsbot: today's digest row is 'failed' but some messages already "
    "posted before it died. Check the game channels and `/newsbot status`, then "
    "`/newsbot run-now` if you want to retry: it'll ask you to confirm "
    "before posting again, since part of today's digest is already out there."
)


def should_catch_up(now_local: datetime, digest_time: dt_time, existing: DigestRow | None) -> bool:
    """Decide whether startup should run today's digest right now.

    True iff local time is past `digest_time` and there's no digest row
    for today, or today's row is `failed` with nothing posted (a clean
    failure, collection or summarizing blew up before anything reached
    Discord, so a retry is safe). False before the scheduled time, if a
    row already exists as `ok`/`partial` (already posted), if it's
    `pending` (ambiguous, see `_PENDING_STARTUP_ALERT`; the caller
    alerts instead of guessing), or if it's `failed` but
    `posted_message_ids` is non-empty (some of the digest made it to the
    channel before publishing failed: an unattended retry here would
    double-post the header or the parts that landed; the caller alerts
    instead, same as `pending`).
    """
    if now_local.time() < digest_time:
        return False
    if existing is None:
        return True
    return existing.status == "failed" and not existing.posted_message_ids


def _parse_digest_time(time_str: str) -> dt_time:
    hour, minute = (int(part) for part in time_str.split(":"))
    return dt_time(hour, minute)


def build_intents(cfg: AppConfig) -> discord.Intents:
    """The gateway intents: the defaults, plus Server Members only if welcomes are on.

    Server Members is a privileged intent, which means it also has to be
    switched on in the Developer Portal or Discord closes the connection
    (`__main__` turns that into a readable message). The daily quote needs
    nothing privileged, so a bot with welcomes off never asks. Presences and
    message content stay off; I have no use for either and Discord has
    opinions about people who ask for things they don't use.

    Member chunking stays at its default: `on_member_update` only fires for
    members in the cache, and chunking is how they get there.
    """
    intents = discord.Intents.default()
    intents.members = cfg.lounge.welcome.enabled
    return intents


def schedule_daily_quote(
    scheduler: AsyncIOScheduler, cfg: AppConfig, callback: Callable[[], Awaitable[None]]
) -> Job:
    """Add the daily-quote cron job: `daily_quote.time` in `digest.timezone`.

    A five-minute grace and no more. The job store is in memory, so a bot
    that was down at quote time simply skips the day, and one that wakes up
    an hour late doesn't post a "morning" quote at lunch. There's no catch-up
    in `on_ready` either; missed means skipped, on purpose.
    """
    tz = ZoneInfo(cfg.digest.timezone)
    quote_time = _parse_digest_time(cfg.lounge.daily_quote.time)
    return scheduler.add_job(
        callback,
        CronTrigger(hour=quote_time.hour, minute=quote_time.minute, timezone=tz),
        id="daily-quote",
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


def _classify_send_error(exc: Exception, *, posted_ids: list[int] | None = None) -> None:
    """Classify a send/fetch failure for `DiscordCodeAlertPoster`.

    Wraps as `PublishError` (worth retrying: a 5xx, a network blip, a
    timeout) or re-raises `exc` unwrapped (a 4xx, or anything else): a
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
        if exc.status is not None and 400 <= exc.status < 500:
            raise exc
        raise PublishError(
            f"discord send failed: {exc}", posted_ids=list(posted_ids or [])
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
    `run_daily`'s guard, save and retry logic runs identically whether the
    digest is heading to a terminal or a set of channels: this class's
    only job is turning a `RenderedDigest` into Discord API calls and
    topic -> message id results.

    One instance is built fresh per run (see `NewsBot.publisher_for_today`
    and the `/newsbot run-now` handler) and then reused across every
    attempt `run.py`'s `_publish_with_retry` makes at it: that's what
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
    ) -> None:
        # `NewsBot`, not plain `discord.Client`: kept for parity with v1 and
        # in case a future non-fatal side path (the old thread-creation
        # alert was one) needs `.alert()` again. Forward-referenced since
        # `NewsBot` is defined later in this same module.
        self._client = client
        # The four keyword arguments are the per-server digest's (design.md
        # §15, plan task 6); left alone, this behaves exactly as v2.2 did.
        # `already_posted` preloads a resume's finished games, `on_posted`
        # hears about each one as it lands so the caller can write it through
        # to the database, and `skip_permanent` turns a dead channel into "skip
        # that game and carry on" (recorded in `skipped`) instead of ending the
        # whole server's digest.
        self._posted: dict[str, int] = dict(already_posted or {})
        self._on_posted = on_posted
        self._skip_permanent = skip_permanent
        self.skipped: dict[str, str] = {}
        self._channels: dict[int, discord.abc.Messageable] = {}
        # One salt per instance, not per send: a retry on *this* instance
        # (the same run, backing off after a transient failure) reuses the
        # salt and so reuses each topic's nonce, letting Discord's own
        # dedup catch a send that actually landed before the retry thought
        # it failed. A confirmed run-now builds a fresh `DiscordPublisher`,
        # hence a fresh salt: that repost is deliberate, not a dupe.
        # With a `nonce_scope` (`"{guild_id}|{run_date}"`) the salt is that
        # instead, stable across a restart, so a resume after a crash mid-send
        # can be deduplicated by Discord too. (A forced re-run passes a fresh
        # scope: that repost is deliberate.)
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


class NullPublisher:
    """A `Publisher` that posts nowhere and returns no ids.

    `/newsbot preview` runs the real pipeline through `run_daily`, which
    always calls `publisher.publish()`, but a preview is only supposed
    to go to the admin who asked, as a set of ephemeral followups, not to
    the game channels. This publisher lets `run_daily`'s machinery run
    unchanged while the actual sending happens afterwards, from
    `PipelineOutcome.rendered`, in the command handler.
    """

    async def publish(self, r: RenderedDigest) -> dict[str, int]:
        return {}


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
    since `shift/sweep.py` already tracks per-message claim/post state of
    its own (`claim_codes`/`mark_codes_posted`) and only ever asks this to
    post one message at a time.

    `alert.ping` is `decide.plan_alerts`'s call, not this class's: all
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
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        # v3: one poster per server, so the ping is that server's choice
        # ("everyone", a role id, or "none") and a missing-permission note goes
        # to that server's admin channel via `notify`, never to the owner. The
        # defaults are exactly v2's: @everyone, told to the admin channel.
        self._ping_choice = ping_choice
        self._notify = notify if notify is not None else client.alert
        # Dedupes the missing-permission admin alert within one sweep
        # (design.md §12 step 8): `begin_batch()` resets this at the start
        # of every `shift/sweep.py._apply_plan` call, so a batch that
        # spills into several messages (or a single message that
        # `_post_with_retry` retries several times) alerts an admin once
        # per sweep, not once per message and not once per retry.
        self._missing_permission_alerted = False

    def begin_batch(self) -> None:
        """Reset the missing-permission dedupe flag; called once per sweep."""
        self._missing_permission_alerted = False

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
        about the missing permission, deduped per sweep by
        `self._missing_permission_alerted`. Any failure resolving the
        channel or sending the message goes through `_classify_send_error`,
        which raises `PublishError` for whatever's worth retrying (a 5xx, a
        network blip, a timeout) and re-raises everything else (a 4xx, a
        permissions problem) unwrapped, per `shift/sweep.py`'s own
        claim/post/record bookkeeping rather than `PublishError`'s.
        """
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(self._channel_id)
            except Exception as exc:  # noqa: BLE001 (classified and re-raised below)
                _classify_send_error(exc)

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


class NewsBot(discord.Client):
    """The bot process: gateway client, command tree, scheduler and job callbacks in one place.

    Command registration is layered in by `bot/commands.py`'s factories
    (`make_news_group`, `make_admin_group`), called from `setup_hook` once
    the httpx and Anthropic clients exist: both groups need `self` (for
    `build_deps`, and for the admin group's access to the client for
    `DiscordPublisher`).
    """

    def __init__(self, cfg: AppConfig, secrets: Secrets, db_path: str) -> None:
        super().__init__(
            intents=build_intents(cfg), allowed_mentions=discord.AllowedMentions.none()
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
        self._last_two_instance_alert: datetime | None = None

        # Shared with build_sweep_deps() the same way build_deps() shares
        # it with the daily job, so Reddit's cross-call gap is honored
        # across the daily 09:00 run and every hourly sweep alike, not
        # reset fresh each time one or the other happens to run.
        self._rate_limit_state = RateLimitState()
        # Set below in setup_hook() when alerts.enabled; stays None
        # otherwise, which is also build_deps()'s existing default: an
        # alerts-off bot behaves exactly as it did before this feature.
        self.code_alert_poster: CodeAlertPoster | None = None
        # Codes fail_pending_codes() flips from 'pending' to 'failed' at
        # startup (R4): a prior process claimed them and the ping
        # budget, then died before confirming the send landed. Reported
        # once on the first on_ready, then never referenced again.
        self._interrupted_codes: list[str] = []
        # True once a sweep crash has alerted the admin channel; cleared
        # by the next successful sweep, so a sweep that's failing on every
        # interval pages once instead of once an hour.
        self._sweep_crash_alerted = False
        # In memory only, on purpose (design.md §14): who was welcomed in the
        # last day. A restart forgets it.
        self._recent_welcomes = RecentWelcomes()
        # The scheduled quote and `/newsbot quote-now` take turns.
        self._quote_lock = asyncio.Lock()

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
        this function matters too: command groups (including the
        conditional `/shift codes` and, gated separately, `test-alert`)
        are registered and synced before the scheduler exists, since
        there's no reason to hold up the ones that don't need it;
        `fail_pending_codes` runs before the code-sweep job is added, so a
        code a prior process claimed and never confirmed posting for is
        already flipped back to `'failed'` before anything else can touch
        `alerted_codes`; and `self.scheduler.start()` is the last line, so
        nothing fires before the rest of setup has actually finished.
        """
        # Imported here, not at module scope: commands.py imports NewsBot
        # (for type hints on the factories' `bot` argument), and importing
        # it back at module scope would make a circular import out of what
        # is otherwise a plain layering.
        from newsbot.bot.commands import make_admin_group, make_news_group, make_shift_group

        logger.info("newsbot starting", extra={"instance_id": _INSTANCE_ID, "hostname": _HOSTNAME})
        warn_if_contact_unset()

        self.http_client = httpx.AsyncClient(headers=user_agent_headers())
        self.llm = AnthropicLLM(self.secrets.anthropic_api_key.get_secret_value())

        self.tree.add_command(make_news_group(self.cfg, self.db_path))
        self.tree.add_command(make_admin_group(self.cfg, self))
        if self.cfg.alerts.enabled:
            # D4 (design.md §13): a codes list from a feature that's off
            # would always be empty: no point registering a third
            # top-level group for it.
            self.tree.add_command(make_shift_group(self.cfg, self.db_path))

        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info(
            "synced %d commands to guild",
            len(synced),
            extra={"guild_id": self.cfg.guild_id},
        )

        tz = ZoneInfo(self.cfg.digest.timezone)
        digest_time = _parse_digest_time(self.cfg.digest.time)
        self.scheduler = AsyncIOScheduler(timezone=tz)
        self.scheduler.add_job(
            self._daily_job,
            CronTrigger(hour=digest_time.hour, minute=digest_time.minute, timezone=tz),
            id="daily-digest",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        # 3:30 a.m. local: late enough that it never overlaps a 9 a.m.
        # digest, early enough nobody's awake to notice a slow purge.
        self.scheduler.add_job(
            self._retention_job,
            CronTrigger(hour=3, minute=30, timezone=tz),
            id="retention",
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

        if self.cfg.alerts.enabled:
            # cfg.alerts.channel_id is required (validated in load_config)
            # whenever cfg.alerts.enabled is True, so it's never None here.
            self.code_alert_poster = DiscordCodeAlertPoster(self, self.cfg.alerts.channel_id)
            # A prior process may have died between claiming a code (and
            # spending the ping budget on it) and confirming the Discord
            # send landed (R4): flip those back to 'failed' before
            # anything else can touch alerted_codes, and remember which
            # ones so the first on_ready can tell an admin.
            self._interrupted_codes = await asyncio.to_thread(self._fail_pending_codes_sync)
            self.scheduler.add_job(
                self._sweep_job,
                IntervalTrigger(minutes=self.cfg.alerts.interval_minutes, timezone=tz),
                id="code-sweep",
                # The first sweep two minutes after startup, not
                # immediately: setup_hook is still finishing (the gateway
                # hasn't necessarily cached guild/channel state yet), and
                # a sweep at t=0 would race that.
                next_run_time=datetime.now(tz) + timedelta(minutes=2),
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )

        if self.cfg.lounge.daily_quote.enabled:
            schedule_daily_quote(self.scheduler, self.cfg, self._quote_job)

        self.scheduler.start()

    def _fail_pending_codes_sync(self) -> list[str]:
        with closing(connect(self.db_path)) as conn:
            return fail_pending_codes(conn)

    async def on_ready(self) -> None:
        """First-connection-only startup work: interrupted-codes alert, permission check, catch-up.

        Guarded by `self._ready_once`: discord.py fires `on_ready` on
        every reconnect, and none of this should repeat just because a
        network blip forced a new gateway session. On the genuine first
        connection, order matters the same way it does in `setup_hook`: an
        admin hears about SHiFT codes a prior crash may have left stuck
        before anything else runs (so it doesn't get buried under later
        noise), then the channel permission check runs (so a permission
        problem is on record before a run that might hit it), and only
        then does the startup catch-up check decide whether to run today's
        digest right now.
        """
        # discord.py fires on_ready on every reconnect, not just the first
        # connection: without this flag, a network blip a week into
        # uptime would re-run the startup catch-up check.
        if self._ready_once:
            return
        self._ready_once = True
        if self._interrupted_codes:
            # Reported once, before catch-up runs: an admin reading this
            # should see "these may be stuck" before anything else happens
            # that could distract from it, and it's a one-time read: this
            # list isn't re-checked on a later reconnect.
            await self.alert(
                "newsbot: found SHiFT code(s) left 'pending' from a prior crash: "
                + ", ".join(self._interrupted_codes)
                + ". They were never confirmed posted; check the SHiFT codes channel."
            )
        await self._check_permissions()
        await self._catch_up()

    async def _check_permissions(self) -> None:
        """One admin alert naming every channel permission problem, or none if clean.

        design.md §13: this runs once, on the first `on_ready`, after the
        interrupted-codes alert (an admin should hear about codes that may
        already be stuck before anything else) and before catch-up (so a
        permission problem is on record before a run that might hit it).
        Wrapped in its own try/except that only logs: a bug in the check
        itself must never be the thing that stops the bot from starting or
        from running today's digest.
        """
        try:
            problems = await check_channels(self, self.cfg)
            if problems:
                await self.alert(render_permission_alert(problems))
        except Exception:
            logger.exception("startup permission check crashed")

    async def _catch_up(self) -> None:
        now = datetime.now(UTC)
        now_local = now.astimezone(ZoneInfo(self.cfg.digest.timezone))
        digest_time = _parse_digest_time(self.cfg.digest.time)
        run_date = local_run_date(now, self.cfg.digest.timezone)

        existing = await asyncio.to_thread(self._get_digest_sync, run_date)
        if should_catch_up(now_local, digest_time, existing):
            logger.info("catch-up: running today's digest at startup")
            await self._daily_job(RunKind.CATCH_UP)
        elif existing is not None and existing.status == "pending":
            await self.alert(_PENDING_STARTUP_ALERT)
        elif existing is not None and existing.status == "failed" and existing.posted_message_ids:
            await self.alert(_PARTIAL_FAILURE_STARTUP_ALERT)

    def _get_digest_sync(self, run_date: date) -> DigestRow | None:
        with closing(connect(self.db_path)) as conn:
            return get_digest(conn, run_date)

    def build_deps(self, run_kind: RunKind | None = None) -> Deps:
        """Assemble a fresh `Deps` for one pipeline run.

        Collectors are rebuilt each call rather than cached on `self`:
        they're cheap to construct and stateless between runs, and
        rebuilding sidesteps any question of whether a collector instance
        is safe to reuse across concurrent-in-theory (but lock-serialized
        in practice) runs.

        `run_kind` is `None` for `/newsbot preview` (which never reports
        regardless; see `_maybe_send_run_report`) and every caller that
        predates the admin-channel run report (design.md §6); `_daily_job`
        and `/newsbot run-now` pass their own so the report can say
        `scheduled`/`catch-up`/`run-now` without saying who ran it.
        """
        if self.http_client is None or self.llm is None:
            raise RuntimeError("build_deps() called before setup_hook() finished")
        return Deps(
            cfg=self.cfg,
            db_path=self.db_path,
            http=self.http_client,
            llm=self.llm,
            collectors=build_collectors(self.cfg, self.secrets),
            now=lambda: datetime.now(UTC),
            alert=self.alert,
            rate_limit_state=self._rate_limit_state,
            code_alert_poster=self.code_alert_poster,
            run_kind=run_kind,
        )

    def build_sweep_deps(self) -> SweepDeps:
        """Assemble a fresh `SweepDeps` for one hourly code sweep.

        Collectors are built fresh here too, same as `build_deps`, and for
        an extra reason specific to Bluesky: its session JWT lasts about
        two hours with no refresh, so a sweep every `interval_minutes`
        rebuilding its own `BlueskyCollector` (and logging in again) is
        what keeps that source working sweep after sweep instead of going
        silently stale partway through the day.
        """
        if self.http_client is None or self.code_alert_poster is None:
            raise RuntimeError("build_sweep_deps() called before alerts were set up")
        return SweepDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            http=self.http_client,
            collectors=build_collectors(self.cfg, self.secrets, include_web_search=False),
            now=lambda: datetime.now(UTC),
            alert=self.alert,
            poster=self.code_alert_poster,
            rate_limit_state=self._rate_limit_state,
        )

    async def alert(self, text: str) -> None:
        await send_alert(self, self.cfg.admin_channel_id, text)

    def publisher_for_today(self) -> DiscordPublisher:
        return DiscordPublisher(self)

    async def _daily_job(self, run_kind: RunKind = RunKind.SCHEDULED) -> None:
        try:
            deps = self.build_deps(run_kind)
            outcome = await run_daily(deps, self.publisher_for_today(), mode=RunMode.POST)
            logger.info(
                "daily job finished",
                extra={"status": outcome.status, "notes": outcome.notes},
            )
        except Exception as exc:  # the job boundary: nothing here may take the process down
            logger.exception("daily job crashed at the job boundary")
            await self.alert(f"newsbot: daily job crashed: {_alert_reason(exc)}")

    async def _sweep_job(self) -> None:
        """The job boundary for the hourly SHiFT alert sweep (design.md §12).

        Same shape as `_daily_job`: nothing raised in here may take the
        process down. Unlike the two-instance alert, a crash here alerts
        only the *first* time (`_sweep_crash_alerted`): a sweep that
        fails every interval would otherwise page an admin channel once an
        hour for the same underlying problem, which teaches everyone to
        ignore the channel. The next *successful* sweep clears the flag,
        so a fixed problem goes back to paging on its next failure.
        """
        try:
            outcome = await run_code_sweep(self.build_sweep_deps())
            if outcome is None:
                logger.info("code sweep skipped: run lock held")
            else:
                self._sweep_crash_alerted = False
                logger.info(
                    "code sweep finished",
                    extra={
                        "posted": outcome.posted,
                        "silent": outcome.silent,
                        "failed": outcome.failed,
                        "ping": outcome.ping,
                    },
                )
        except Exception as exc:
            logger.exception("code sweep crashed at the job boundary")
            if not self._sweep_crash_alerted:
                self._sweep_crash_alerted = True
                await self.alert(f"newsbot: SHiFT code sweep crashed: {_alert_reason(exc)}")

    async def on_member_join(self, member: discord.Member) -> None:
        if not self.cfg.lounge.welcome.enabled:
            return
        await self._maybe_welcome(member, "join")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Welcome a member who just got through rules screening (`pending` true to false).

        Nickname changes, role changes and everything else that fires this
        event are none of our business.
        """
        if not self.cfg.lounge.welcome.enabled:
            return
        if before.pending and not after.pending:
            await self._maybe_welcome(after, "accepted")

    async def _maybe_welcome(self, member: discord.Member, event: str) -> None:
        """Decide, record, render and post one welcome; problems go to the admin channel.

        The log line carries the event, `pending`, `flags.value` and the
        decision, and never the user ID or name: I want to see how Onboarding
        behaves in the test guild without keeping a guest list in the logs.
        The failure alert doesn't name the member either.
        """
        try:
            action = welcome_action(
                in_guild=member.guild.id == self.cfg.guild_id,
                is_bot=member.bot,
                pending=member.pending,
            )
            decision: str = action
            # No await between the check and the record (they're one call), so
            # two events for the same member can't both get through.
            if action == "welcome" and not self._recent_welcomes.check_and_record(
                member.id, datetime.now(UTC)
            ):
                decision = "recent"
            logger.info(
                "member event %s: pending=%s flags=%s decision=%s",
                event,
                member.pending,
                member.flags.value,
                decision,
                extra={
                    "welcome_event": event,
                    "pending": member.pending,
                    "flags": member.flags.value,
                    "decision": decision,
                },
            )
            if decision != "welcome":
                return
            text = render_welcome(
                self.cfg.lounge.welcome.message,
                member_mention=member.mention,
                server_name=member.guild.name,
            )
            channel = await self._lounge_channel()
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=[member], roles=False, replied_user=False
                ),
            )
        except Exception as exc:
            # No traceback, on purpose: it ends with the exception's message,
            # and the design promises nothing about members reaches the logs.
            # The class name and (for Discord) the status and code are enough
            # to tell what happened, and none of it can name a person.
            logger.error("welcome failed: %s%s", type(exc).__name__, _http_detail(exc))
            await self.alert(
                f"newsbot: couldn't post a welcome in the lounge ({type(exc).__name__}). "
                "The details are in the bot's log."
            )

    async def _lounge_channel(self) -> discord.abc.Messageable:
        channel_id = self.cfg.lounge.channel_id
        if channel_id is None:
            raise RuntimeError("lounge.channel_id is not set")
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    def build_quote_deps(self) -> QuoteDeps:
        """Assemble a fresh `QuoteDeps` for one quote run (the job or `quote-now`)."""
        sources = self.cfg.lounge.daily_quote.sources
        if self.http_client is None or sources is None:
            raise RuntimeError("build_quote_deps() called before setup_hook() finished")

        async def post(text: str) -> int:
            channel = await self._lounge_channel()
            sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return sent.id

        return QuoteDeps(
            sources=sources,
            db_path=self.db_path,
            http=self.http_client,
            local_day=local_run_date(datetime.now(UTC), self.cfg.digest.timezone),
            now=lambda: datetime.now(UTC),
            post=post,
            alert=self.alert,
            rng=random.SystemRandom(),
            cache_dir=cache_dir_for(self.db_path),
        )

    async def run_quote(self, force: bool) -> QuoteOutcome:
        """Run the daily quote under the lock, so the job and `quote-now` can't overlap."""
        async with self._quote_lock:
            return await run_daily_quote(self.build_quote_deps(), force=force)

    async def _quote_job(self) -> None:
        try:
            outcome = await self.run_quote(False)
            logger.info("quote job finished", extra={"status": outcome.status})
        except Exception as exc:  # the job boundary: nothing here may take the process down
            logger.exception("daily quote job crashed at the job boundary")
            await self.alert(f"newsbot: daily quote job crashed: {_alert_reason(exc)}")

    async def _retention_job(self) -> None:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)
            items_deleted, stories_deleted = await asyncio.to_thread(self._purge_sync, cutoff)
            logger.info(
                "retention purge finished",
                extra={"items_deleted": items_deleted, "stories_deleted": stories_deleted},
            )
        except Exception as exc:
            logger.exception("retention job crashed")
            await self.alert(f"newsbot: retention job crashed: {_alert_reason(exc)}")

    def _purge_sync(self, cutoff: datetime) -> tuple[int, int]:
        with closing(connect(self.db_path)) as conn:
            return purge_older_than(conn, cutoff)

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


__all__ = [
    "HEARTBEAT",
    "DiscordCodeAlertPoster",
    "DiscordPublisher",
    "NewsBot",
    "NullPublisher",
    "build_intents",
    "schedule_daily_quote",
    "should_catch_up",
]
