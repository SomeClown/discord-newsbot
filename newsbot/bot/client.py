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
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from discord import app_commands

from newsbot.alerts import send_alert
from newsbot.bot.format import RenderedDigest
from newsbot.collectors.base import build_collectors
from newsbot.config import AppConfig, Secrets
from newsbot.pipeline.publisher import PublishError
from newsbot.pipeline.run import Deps, RunMode, local_run_date, run_daily
from newsbot.pipeline.summarize import AnthropicLLM, LLMClient
from newsbot.store.db import connect
from newsbot.store.models import DigestRow
from newsbot.store.repo import get_digest, purge_older_than

logger = logging.getLogger(__name__)

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
    "`/newsbot run-now` (with force, if it asks) once you know which."
)


def should_catch_up(now_local: datetime, digest_time: dt_time, existing: DigestRow | None) -> bool:
    """Decide whether startup should run today's digest right now.

    True iff local time is past `digest_time` and there's no digest row
    for today, or today's row is `failed` (a `failed` row means today
    genuinely never posted, so a retry is safe). False before the
    scheduled time, or if a row already exists as `ok`/`partial` (already
    posted) or `pending` (ambiguous -- see `_PENDING_STARTUP_ALERT`; the
    caller alerts instead of guessing).
    """
    if now_local.time() < digest_time:
        return False
    if existing is None:
        return True
    return existing.status == "failed"


def _parse_digest_time(time_str: str) -> dt_time:
    hour, minute = (int(part) for part in time_str.split(":"))
    return dt_time(hour, minute)


class DiscordPublisher:
    """The `Publisher` the real bot uses: posts the digest, then opens a discussion thread on it.

    Implements the same `Publisher` protocol `PrintPublisher` does, so
    `run_daily`'s guard, save and retry logic runs identically whether the
    digest is heading to a terminal or a channel -- this class's only job
    is turning a `RenderedDigest` into Discord API calls and message ids.
    """

    def __init__(self, client: discord.Client, channel_id: int, run_date: date) -> None:
        self._client = client
        self._channel_id = channel_id
        self._run_date = run_date

    async def publish(self, r: RenderedDigest) -> list[int]:
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(self._channel_id)
            except discord.HTTPException as exc:
                raise PublishError(f"can't reach digest channel {self._channel_id}: {exc}") from exc

        try:
            header_msg = await channel.send(
                r.header, allowed_mentions=discord.AllowedMentions.none()
            )
            # The thread hangs off the header message, not the channel --
            # that's what makes it show up as a reply thread on today's
            # digest instead of a bare, disconnected discussion channel.
            await header_msg.create_thread(name=f"News {self._run_date.isoformat()}")

            message_ids = [header_msg.id]
            for embeds in r.embed_messages:
                sent = await channel.send(
                    embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
                )
                message_ids.append(sent.id)
        except discord.HTTPException as exc:
            raise PublishError(f"discord send failed: {exc}") from exc
        return message_ids


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

        self.http_client: httpx.AsyncClient | None = None
        self.llm: LLMClient | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self._ready_once = False

    async def setup_hook(self) -> None:
        # Imported here, not at module scope: commands.py imports NewsBot
        # (for type hints on the factories' `bot` argument), and importing
        # it back at module scope would make a circular import out of what
        # is otherwise a plain layering.
        from newsbot.bot.commands import make_news_group

        self.http_client = httpx.AsyncClient(headers={"User-Agent": _USER_AGENT})
        self.llm = AnthropicLLM(self.secrets.anthropic_api_key.get_secret_value())

        self.tree.add_command(make_news_group(self.cfg, self.db_path))
        # The /newsbot admin group is registered here too, added in step 16.

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
        self.scheduler.start()

    async def on_ready(self) -> None:
        # discord.py fires on_ready on every reconnect, not just the first
        # connection -- without this flag, a network blip a week into
        # uptime would re-run the startup catch-up check.
        if self._ready_once:
            return
        self._ready_once = True
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
    "DiscordPublisher",
    "NewsBot",
    "NullPublisher",
    "should_catch_up",
]
