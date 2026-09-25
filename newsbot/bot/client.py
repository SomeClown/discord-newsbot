"""The discord.py client: gateway connection, scheduler wiring, and the heartbeat file.

Everything before this module could be tested without a bot token, a guild,
or an event loop discord.py owns. This is where that ends -- `NewsBot`
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
into a loop nobody's listening on -- a mistake that is, by design, invisible
until 9 a.m. the first morning it matters.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from discord import app_commands

from newsbot.alerts import send_alert
from newsbot.bot.format import RenderedAlert, RenderedDigest
from newsbot.collectors.base import RateLimitState, build_collectors
from newsbot.config import AppConfig, Secrets
from newsbot.pipeline.publisher import PublishError
from newsbot.pipeline.run import Deps, RunMode, local_run_date, run_daily
from newsbot.pipeline.summarize import AnthropicLLM, LLMClient
from newsbot.shift.sweep import CodeAlertPoster, SweepDeps, run_code_sweep
from newsbot.store.db import connect
from newsbot.store.models import DigestRow
from newsbot.store.repo import fail_pending_codes, get_digest, purge_older_than

logger = logging.getLogger(__name__)

# One id per process, generated at import time -- not persisted, not
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
# not the process directly -- there's no port to poll, since the gateway
# is an outgoing connection. /tmp is fine for this: it's tmpfs in the
# compose config, and the file's only job is to exist and be recent.
HEARTBEAT = Path("/tmp/newsbot-heartbeat")  # noqa: S108 -- tmpfs in compose, not a real tempfile race

# Same string as pipeline/run.py's collector User-Agent (Reddit in
# particular wants one that names the bot and a way to reach its
# operator). Duplicated rather than imported because run.py's copy is a
# module-private constant for its own CLI's httpx client; this one is for
# the bot's, and the two clients happen to want the same string, not a
# shared one.
_USER_AGENT = "discord-newsbot/1.0 (+https://github.com/, contact: owner)"

_RETENTION_DAYS = 90
_HEARTBEAT_INTERVAL_S = 60
_PENDING_STARTUP_ALERT = (
    "newsbot: found a 'pending' digest row at startup. That usually means "
    "the process crashed mid-run last time -- it may or may not have "
    "already posted. Check the channel and `/newsbot status`, then "
    "`/newsbot run-now` if you want to retry -- it'll ask you to confirm "
    "before posting again, since we can't tell whether today already went out."
)
_PARTIAL_FAILURE_STARTUP_ALERT = (
    "newsbot: today's digest row is 'failed' but some messages already "
    "posted before it died. Check the channel and `/newsbot status`, then "
    "`/newsbot run-now` if you want to retry -- it'll ask you to confirm "
    "before posting again, since part of today's digest is already out there."
)


def should_catch_up(now_local: datetime, digest_time: dt_time, existing: DigestRow | None) -> bool:
    """Decide whether startup should run today's digest right now.

    True iff local time is past `digest_time` and there's no digest row
    for today, or today's row is `failed` with nothing posted (a clean
    failure -- collection or summarizing blew up before anything reached
    Discord, so a retry is safe). False before the scheduled time, if a
    row already exists as `ok`/`partial` (already posted), if it's
    `pending` (ambiguous -- see `_PENDING_STARTUP_ALERT`; the caller
    alerts instead of guessing), or if it's `failed` but
    `posted_message_ids` is non-empty (some of the digest made it to the
    channel before publishing failed -- an unattended retry here would
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


# Errors worth retrying: the network blipped, or a request timed out
# before a response came back at all. `discord.HTTPException` is handled
# separately below, since whether *that* is retryable depends on the
# status code Discord actually sent back.
_TRANSIENT_ERRORS = (aiohttp.ClientError, OSError, TimeoutError)

# The one and only place `AllowedMentions(everyone=True, ...)` is allowed to
# appear in newsbot/ (design.md §12, A5) -- `tests/test_mentions_tripwire.py`
# scans the source tree to hold that line. Every other send in this file
# (the client's own default, DiscordPublisher's digest sends) stays
# `AllowedMentions.none()`; only a code alert that `decide.plan_alerts`
# actually decided should ping gets this one.
_PING_EVERYONE = discord.AllowedMentions(
    everyone=True, users=False, roles=False, replied_user=False
)


def _classify_send_error(exc: Exception, *, posted_ids: list[int] | None = None) -> None:
    """Classify a send/fetch failure shared by `DiscordPublisher` and `DiscordCodeAlertPoster`.

    Wraps as `PublishError` (worth retrying: a 5xx, a network blip, a
    timeout) or re-raises `exc` unwrapped (a 4xx, or anything else) --
    see `DiscordPublisher._reraise_or_wrap`'s original docstring for why
    a permissions problem shouldn't burn a retry budget. `posted_ids`
    only means anything to `DiscordPublisher`'s resumable multi-message
    publish; `DiscordCodeAlertPoster` posts one message at a time and
    always passes `None` (which becomes an empty list).
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


class DiscordPublisher:
    """The `Publisher` the real bot uses: posts the digest, then opens a discussion thread on it.

    Implements the same `Publisher` protocol `PrintPublisher` does, so
    `run_daily`'s guard, save and retry logic runs identically whether the
    digest is heading to a terminal or a channel -- this class's only job
    is turning a `RenderedDigest` into Discord API calls and message ids.

    One instance is built fresh per run (see `NewsBot.publisher_for_today`
    and the `/newsbot run-now` handler) and then reused across every
    attempt `run.py`'s `_publish_with_retry` makes at it -- that's what
    makes resumability possible. It tracks which messages it's already
    gotten an id back for on `self`, so a retried `publish()` call picks
    up where the last attempt left off instead of reposting the header (and
    however many embed messages already landed) on every retry. Before
    this tracking existed, three transient failures in a row meant the
    channel got the same digest four times.
    """

    def __init__(self, client: NewsBot, channel_id: int, run_date: date) -> None:
        # `NewsBot`, not plain `discord.Client`: this needs `.alert()` for
        # the non-fatal thread-creation path below, which only `NewsBot`
        # has. Forward-referenced since `NewsBot` is defined later in this
        # same module.
        self._client = client
        self._channel_id = channel_id
        self._run_date = run_date
        self._header_msg: discord.Message | None = None
        self._thread_created = False
        self._posted_ids: list[int] = []

    async def publish(self, r: RenderedDigest) -> list[int]:
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(self._channel_id)
            except Exception as exc:  # noqa: BLE001 -- classified and re-raised below
                self._reraise_or_wrap(exc)

        if self._header_msg is None:
            try:
                self._header_msg = await channel.send(
                    r.header, allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception as exc:  # noqa: BLE001 -- classified and re-raised below
                self._reraise_or_wrap(exc)
            self._posted_ids.append(self._header_msg.id)

        if not self._thread_created:
            # The thread hangs off the header message, not the channel --
            # that's what makes it show up as a reply thread on today's
            # digest instead of a bare, disconnected discussion channel.
            # Losing the thread isn't worth losing the digest over, though:
            # the header and embeds below are the part anyone's actually
            # here to read, so a thread failure gets logged and alerted,
            # not raised.
            try:
                await self._header_msg.create_thread(name=f"News {self._run_date.isoformat()}")
            except Exception as exc:  # noqa: BLE001 -- deliberately swallowed, see above
                logger.error(
                    "couldn't create the discussion thread for %s; posting without one",
                    self._run_date,
                    exc_info=exc,
                )
                await self._client.alert(f"newsbot: couldn't create the discussion thread: {exc}")
            self._thread_created = True

        # `self._posted_ids` already has the header (and, on a retry, any
        # embed messages a prior attempt got through) -- skip straight to
        # the first one this attempt hasn't sent yet.
        already_sent = len(self._posted_ids) - 1
        for embeds in r.embed_messages[already_sent:]:
            try:
                sent = await channel.send(
                    embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception as exc:  # noqa: BLE001 -- classified and re-raised below
                self._reraise_or_wrap(exc)
            self._posted_ids.append(sent.id)

        return list(self._posted_ids)

    def _reraise_or_wrap(self, exc: Exception) -> None:
        """Classify a send/fetch failure: wrap it as `PublishError` if it's
        worth retrying, or let it propagate as-is if it isn't.

        `discord.Forbidden` and friends (any 4xx) mean the request will
        never succeed no matter how many times `_publish_with_retry`
        tries it -- wrapping those would just burn the retry budget on a
        permissions problem that needs a human, not a backoff timer.
        Delegates to `_classify_send_error`, shared with
        `DiscordCodeAlertPoster` below.
        """
        _classify_send_error(exc, posted_ids=self._posted_ids)


class NullPublisher:
    """A `Publisher` that posts nowhere and returns no ids.

    `/newsbot preview` runs the real pipeline through `run_daily`, which
    always calls `publisher.publish()` -- but a preview is only supposed
    to go to the admin who asked, as an ephemeral followup, not to the
    digest channel. This publisher lets `run_daily`'s machinery run
    unchanged while the actual sending happens afterwards, from
    `PipelineOutcome.rendered`, in the command handler.
    """

    async def publish(self, r: RenderedDigest) -> list[int]:
        return []


_MISSING_MENTION_PERMISSION_ALERT = (
    "newsbot: a SHiFT code alert wanted to ping @everyone, but this bot's "
    "role is missing 'Mention @everyone, @here, and All Roles' in the "
    "digest channel -- Discord posts the message but silently drops the "
    "ping. Posted anyway; grant the permission (docs/deploy.md) if you "
    "want the next one to actually notify anyone."
)


class DiscordCodeAlertPoster:
    """The `CodeAlertPoster` (`shift/sweep.py`) the real bot uses: one message per alert.

    Structurally the small sibling of `DiscordPublisher` above -- same
    channel-resolution dance, same error classification (`_classify_send_error`,
    factored out of `DiscordPublisher._reraise_or_wrap` for exactly this
    reuse) -- but with none of that class's resumability bookkeeping,
    since `shift/sweep.py` already tracks per-message claim/post state of
    its own (`claim_codes`/`mark_codes_posted`) and only ever asks this to
    post one message at a time.

    `alert.ping` is `decide.plan_alerts`'s call, not this class's -- all
    this does is turn that into the one `AllowedMentions` that's actually
    allowed to set `everyone=True` anywhere in this codebase (`_PING_EVERYONE`,
    A5), or `AllowedMentions.none()` otherwise. Before honoring a ping, it
    checks whether the bot's own role can actually mention `@everyone` in
    this channel -- missing that permission doesn't stop the alert from
    still posting (the code itself is the important part), it just gets
    an admin alert instead of a silent, permission-dropped ping nobody
    would otherwise notice.
    """

    def __init__(self, client: NewsBot, channel_id: int) -> None:
        self._client = client
        self._channel_id = channel_id

    async def post(self, alert: RenderedAlert) -> int | None:
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(self._channel_id)
            except Exception as exc:  # noqa: BLE001 -- classified and re-raised below
                _classify_send_error(exc)

        mentions = discord.AllowedMentions.none()
        if alert.ping:
            if self._can_mention_everyone(channel):
                mentions = _PING_EVERYONE
            else:
                # Still posts with _PING_EVERYONE's intent -- Discord just
                # drops the actual notification on its end when the role
                # lacks the permission, same as the plan's owner checklist
                # describes. Using .none() here instead would be strictly
                # worse: it changes nothing about who gets notified (still
                # nobody) but throws away the chance the permission gets
                # granted *before* the next code shows up.
                mentions = _PING_EVERYONE
                await self._client.alert(_MISSING_MENTION_PERMISSION_ALERT)

        try:
            message = await channel.send(alert.content, allowed_mentions=mentions)
        except Exception as exc:  # noqa: BLE001 -- classified and re-raised below
            _classify_send_error(exc)
        return message.id

    @staticmethod
    def _can_mention_everyone(channel: object) -> bool:
        """Whether this bot's role can actually ping `@everyone` in `channel`.

        `channel.guild.me` is `None` for a DM (not a real deployment
        shape here, but cheap to guard) or before the gateway has cached
        the guild member -- either way, "can't tell" reads as "can't", the
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
    the httpx and Anthropic clients exist -- both groups need `self` (for
    `build_deps`, and for the admin group's access to the client for
    `DiscordPublisher`).
    """

    def __init__(self, cfg: AppConfig, secrets: Secrets, db_path: str) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
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
        # it with the daily job -- so Reddit's cross-call gap is honored
        # across the daily 09:00 run and every hourly sweep alike, not
        # reset fresh each time one or the other happens to run.
        self._rate_limit_state = RateLimitState()
        # Set below in setup_hook() when alerts.enabled; stays None
        # otherwise, which is also build_deps()'s existing default -- an
        # alerts-off bot behaves exactly as it did before this feature.
        self.code_alert_poster: CodeAlertPoster | None = None
        # Codes fail_pending_codes() flips from 'pending' to 'failed' at
        # startup (R4) -- a prior process claimed them and the ping
        # budget, then died before confirming the send landed. Reported
        # once on the first on_ready, then never referenced again.
        self._interrupted_codes: list[str] = []
        # True once a sweep crash has alerted the admin channel; cleared
        # by the next successful sweep, so a sweep that's failing on every
        # interval pages once instead of once an hour.
        self._sweep_crash_alerted = False

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
        genuine collision -- which shows up as a steady stream of 10062s
        and 40060s, one per lost race, not a single one -- doesn't turn
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
        # Imported here, not at module scope: commands.py imports NewsBot
        # (for type hints on the factories' `bot` argument), and importing
        # it back at module scope would make a circular import out of what
        # is otherwise a plain layering.
        from newsbot.bot.commands import make_admin_group, make_news_group

        logger.info("newsbot starting", extra={"instance_id": _INSTANCE_ID, "hostname": _HOSTNAME})

        self.http_client = httpx.AsyncClient(headers={"User-Agent": _USER_AGENT})
        self.llm = AnthropicLLM(self.secrets.anthropic_api_key.get_secret_value())

        self.tree.add_command(make_news_group(self.cfg, self.db_path))
        self.tree.add_command(make_admin_group(self.cfg, self))

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
            self.code_alert_poster = DiscordCodeAlertPoster(self, self.cfg.digest.channel_id)
            # A prior process may have died between claiming a code (and
            # spending the ping budget on it) and confirming the Discord
            # send landed (R4) -- flip those back to 'failed' before
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

        self.scheduler.start()

    def _fail_pending_codes_sync(self) -> list[str]:
        with closing(connect(self.db_path)) as conn:
            return fail_pending_codes(conn)

    async def on_ready(self) -> None:
        # discord.py fires on_ready on every reconnect, not just the first
        # connection -- without this flag, a network blip a week into
        # uptime would re-run the startup catch-up check.
        if self._ready_once:
            return
        self._ready_once = True
        if self._interrupted_codes:
            # Reported once, before catch-up runs -- an admin reading this
            # should see "these may be stuck" before anything else happens
            # that could distract from it, and it's a one-time read: this
            # list isn't re-checked on a later reconnect.
            await self.alert(
                "newsbot: found SHiFT code(s) left 'pending' from a prior crash: "
                + ", ".join(self._interrupted_codes)
                + ". They were never confirmed posted; check the digest channel."
            )
        await self._catch_up()

    async def _catch_up(self) -> None:
        now = datetime.now(UTC)
        now_local = now.astimezone(ZoneInfo(self.cfg.digest.timezone))
        digest_time = _parse_digest_time(self.cfg.digest.time)
        run_date = local_run_date(now, self.cfg.digest.timezone)

        existing = await asyncio.to_thread(self._get_digest_sync, run_date)
        if should_catch_up(now_local, digest_time, existing):
            logger.info("catch-up: running today's digest at startup")
            await self._daily_job()
        elif existing is not None and existing.status == "pending":
            await self.alert(_PENDING_STARTUP_ALERT)
        elif existing is not None and existing.status == "failed" and existing.posted_message_ids:
            await self.alert(_PARTIAL_FAILURE_STARTUP_ALERT)

    def _get_digest_sync(self, run_date: date) -> DigestRow | None:
        with closing(connect(self.db_path)) as conn:
            return get_digest(conn, run_date)

    def build_deps(self) -> Deps:
        """Assemble a fresh `Deps` for one pipeline run.

        Collectors are rebuilt each call rather than cached on `self`:
        they're cheap to construct and stateless between runs, and
        rebuilding sidesteps any question of whether a collector instance
        is safe to reuse across concurrent-in-theory (but lock-serialized
        in practice) runs.
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
        run_date = local_run_date(datetime.now(UTC), self.cfg.digest.timezone)
        return DiscordPublisher(self, self.cfg.digest.channel_id, run_date)

    async def _daily_job(self) -> None:
        try:
            deps = self.build_deps()
            outcome = await run_daily(deps, self.publisher_for_today(), mode=RunMode.POST)
            logger.info(
                "daily job finished",
                extra={"status": outcome.status, "notes": outcome.notes},
            )
        except Exception as exc:  # the job boundary: nothing here may take the process down
            logger.exception("daily job crashed at the job boundary")
            await self.alert(f"newsbot: daily job crashed: {exc}")

    async def _sweep_job(self) -> None:
        """The job boundary for the hourly SHiFT alert sweep (design.md §12).

        Same shape as `_daily_job`: nothing raised in here may take the
        process down. Unlike the two-instance alert, a crash here alerts
        only the *first* time (`_sweep_crash_alerted`) -- a sweep that
        fails every interval would otherwise page an admin channel once an
        hour for the same underlying problem, which teaches everyone to
        ignore the channel. The next *successful* sweep clears the flag,
        so a fixed problem goes back to paging on its next failure.
        """
        try:
            outcome = await run_code_sweep(self.build_sweep_deps())
            self._sweep_crash_alerted = False
            if outcome is None:
                logger.info("code sweep skipped: run lock held")
            else:
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
                await self.alert(f"newsbot: SHiFT code sweep crashed: {exc}")

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
            await self.alert(f"newsbot: retention job crashed: {exc}")

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
    "should_catch_up",
]
