"""A pretend Discord with a speed limit, and a few hundred servers to point it at.

Shared by `test_load_many_guilds.py` (virtual clock, finishes in seconds) and
`scripts/loadtest_digests.py` (real clock, finishes whenever the bot does).
Only two things are fake: Discord and the clock. The database is a real SQLite
file, the repo is the real repo, and the publisher is the real
`DiscordPublisher` (or `DiscordCodeAlertPoster`), so the retry, the pacing and
the lease all run the code that will run at 09:00 on a bad day.

`FakeDiscord` is the part that is rude on purpose. It admits about 50 sends a
second across the whole bot and 5 per 5 seconds per channel, which are the
numbers the plan promised the real thing would enforce, and it answers anything
over the line with a 429, the way a real one would if discord.py ever let a 429
out of its own retry loop. On top of that it injects 429s at random (seeded, so a
failure reproduces) because the natural limits are surprisingly hard to hit
when the bot paces itself. Which is the point of the pacing, I suppose, but a
rate limit nobody ever trips isn't much of a test.

It does not model Discord's nonce dedupe. A resume that re-sends a game the dead
process had already landed therefore shows up as a duplicate here, which is the
documented worst case (design.md, D7) and the conservative one.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord

from newsbot.bot.client import DiscordPublisher
from newsbot.config import AppConfig, GameCfg, load_config
from newsbot.pipeline.guild_digest import GuildDigestDeps, GuildDigestOutcome, run_due_guilds
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
# The plan's numbers: about 50 sends a second for the whole bot, 5 per 5 s per channel.
GLOBAL_PER_S = 50
CHANNEL_PER_5S = 5
# A server's id, times this, plus a slot is the id of its channels, so a channel always
# knows whose it is (`channel_id // CHANNEL_SPAN`) without a lookup table.
CHANNEL_SPAN = 100
SHIFT_SLOT = 99


class _Response:
    def __init__(self, status: int) -> None:
        self.status, self.reason, self.headers, self.request_info = status, "x", {}, None


def too_many_requests() -> discord.HTTPException:
    return discord.HTTPException(_Response(429), "You are being rate limited.")


@dataclass
class Landed:
    """One message that made it into a channel."""

    id: int
    channel_id: int
    at: float
    content: str | None
    everyone: bool
    roles: bool


@dataclass
class CrashPlan:
    """Die on `guild_id`'s `nth` send, either just before the message lands or just after.

    `kill` is the driver's way of taking the whole process down with it (it cancels every
    tick), since a real crash doesn't stop at the one coroutine that tripped over it.
    """

    guild_id: int
    nth: int
    when: str  # "before" or "after"
    kill: Callable[[], None]
    seen: int = 0
    fired: bool = False


@dataclass
class FakeDiscord:
    """Discord's side of the table: limits, 429s, latency, and a ledger of what landed."""

    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    rng: random.Random
    latency_s: float = 0.05
    p429: float = 0.0
    crash: CrashPlan | None = None
    landed: dict[int, list[Landed]] = field(default_factory=dict)
    attempts: int = 0
    natural_429s: int = 0
    injected_429s: int = 0
    # guild id -> how many 429s that guild's sends were handed (for the backoff arithmetic)
    rejected_by_guild: dict[int, int] = field(default_factory=dict)
    _global: deque = field(default_factory=deque)
    _by_channel: dict[int, deque] = field(default_factory=dict)
    _next_id: int = 5000

    def _admit(self, channel_id: int) -> bool:
        now = self.clock()
        while self._global and self._global[0] <= now - 1.0:
            self._global.popleft()
        window = self._by_channel.setdefault(channel_id, deque())
        while window and window[0] <= now - 5.0:
            window.popleft()
        if len(self._global) >= GLOBAL_PER_S or len(window) >= CHANNEL_PER_5S:
            return False
        self._global.append(now)
        window.append(now)
        return True

    async def send(self, channel_id: int, content, embed, allowed_mentions) -> SimpleNamespace:
        guild_id = channel_id // CHANNEL_SPAN
        self.attempts += 1
        await self.sleep(self.latency_s)  # the round trip, which is when a cancel lands
        reject = None
        if self.p429 and self.rng.random() < self.p429:
            self.injected_429s += 1
            reject = too_many_requests()
        elif not self._admit(channel_id):
            self.natural_429s += 1
            reject = too_many_requests()
        if reject is not None:
            self.rejected_by_guild[guild_id] = self.rejected_by_guild.get(guild_id, 0) + 1
            raise reject
        plan = self.crash
        crashing = False
        if plan is not None and not plan.fired and guild_id == plan.guild_id:
            plan.seen += 1
            crashing = plan.seen == plan.nth
        if crashing and plan.when == "before":
            plan.fired = True
            plan.kill()
            raise asyncio.CancelledError
        self._next_id += 1
        self.landed.setdefault(channel_id, []).append(
            Landed(
                self._next_id,
                channel_id,
                self.clock(),
                content,
                bool(allowed_mentions and allowed_mentions.everyone),
                bool(allowed_mentions and allowed_mentions.roles),
            )
        )
        if crashing:  # "after": it landed, and nobody got to write that down
            plan.fired = True
            plan.kill()
            raise asyncio.CancelledError
        return SimpleNamespace(id=self._next_id)

    def messages_in(self, channel_id: int) -> int:
        return len(self.landed.get(channel_id, []))

    @property
    def total_landed(self) -> int:
        return sum(len(v) for v in self.landed.values())


class FakeChannel:
    def __init__(self, discord_: FakeDiscord, channel_id: int) -> None:
        self._discord = discord_
        self.id = channel_id
        self.type = discord.ChannelType.text
        # `me` and `permissions_for` exist so the SHiFT poster's ping check has something to ask.
        self.guild = SimpleNamespace(id=channel_id // CHANNEL_SPAN, me=object())

    def permissions_for(self, _member) -> SimpleNamespace:
        return SimpleNamespace(mention_everyone=True)

    async def send(self, content=None, *, embed=None, allowed_mentions=None, nonce=None):
        assert allowed_mentions is not None
        return await self._discord.send(self.id, content, embed, allowed_mentions)


class FakeClient:
    """The bit of `NewsBot` the publishers touch: find a channel by id."""

    def __init__(self, discord_: FakeDiscord) -> None:
        self._discord = discord_
        self._channels: dict[int, FakeChannel] = {}

    def get_channel(self, channel_id: int) -> FakeChannel:
        if channel_id not in self._channels:
            self._channels[channel_id] = FakeChannel(self._discord, channel_id)
        return self._channels[channel_id]

    async def fetch_channel(self, channel_id: int):
        raise discord.NotFound(_Response(404), "unknown channel")

    async def alert(self, text: str) -> None:  # the poster's default notifier; nobody's listening
        return None


# --- the world: catalog, servers, items ---


def catalog_of(n: int) -> list[GameCfg]:
    return [GameCfg(key=f"game{i:02d}", name=f"Game {i}") for i in range(n)]


def load_load_config(games: int = 12) -> AppConfig:
    """The v3 fixture config with a bigger catalog. Needs `BRAVE_API_KEY` set (any value)."""
    cfg = load_config(V3)
    return cfg.model_copy(update={"catalog": catalog_of(games)})


GUILD0 = 10_000


def guild_id_for(i: int) -> int:
    return GUILD0 + i


def channel_for(guild_id: int, slot: int) -> int:
    return guild_id * CHANNEL_SPAN + slot


def build_digest_world(
    db_path: str,
    *,
    guilds: int,
    games_max: int,
    seed: int,
    zones: tuple[str, ...],
    due: datetime,
    digest_time: str = "09:00",
    catalog_size: int = 12,
    min_games: dict[int, int] | None = None,
    now: datetime | None = None,
) -> dict[int, list[tuple[str, int]]]:
    """Create the database, `guilds` set-up servers and two stored items per game.

    Each server follows 1 to `games_max` games, each in its own channel, and gets one of
    `zones` in turn. `min_games` pins a server (by index) to at least that many, for tests
    that want a digest with more than one message to be cut off partway. Items are stamped
    two hours before `due`, inside every first digest's window. Returns server id ->
    `[(game_key, channel_id)]`.
    """
    rng = random.Random(seed)  # noqa: S311 (a shuffle, not a secret)
    stamp = now or due - timedelta(hours=3)
    with closing(connect(db_path)) as conn:
        migrate(conn)
        plan: dict[int, list[tuple[str, int]]] = {}
        for i in range(guilds):
            gid = guild_id_for(i)
            count = min(max(rng.randint(1, games_max), (min_games or {}).get(i, 1)), catalog_size)
            keys = rng.sample([g.key for g in catalog_of(catalog_size)], count)
            followed = [(key, channel_for(gid, slot)) for slot, key in enumerate(sorted(keys))]
            repo.create_guild(
                conn,
                gid,
                digest_time=digest_time,
                timezone=zones[i % len(zones)],
                tier="free",
                set_up=True,
                now=lambda: stamp,
            )
            repo.set_guild_games(conn, gid, followed)
            plan[gid] = followed
    add_items(db_path, catalog_size, due - timedelta(hours=2))
    return plan


def add_items(db_path: str, catalog_size: int, collected: datetime) -> None:
    items = [
        StoredItem(
            url=f"https://example.com/{game.key}/{collected:%Y%m%d}/{n}",
            title=f"{game.name} headline {n}",
            excerpt="",
            source_name="Feed",
            trust="press",
            published_at=None,
            topics={game.key: False},
        )
        for game in catalog_of(catalog_size)
        for n in range(2)
    ]
    with closing(connect(db_path)) as conn:
        repo.store_items(conn, items, now=lambda: collected)


@dataclass
class DigestWorld:
    """Everything a digest run needs and everything it said, in one place."""

    cfg: AppConfig
    db_path: str
    plan: dict[int, list[tuple[str, int]]]
    discord: FakeDiscord
    now: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]
    notices: list[tuple[int, str]] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    publishers: list = field(default_factory=list)  # weakrefs, so a test can see what's left

    def deps(self, **overrides) -> GuildDigestDeps:
        import weakref

        async def notify(guild_id: int, text: str) -> None:
            self.notices.append((guild_id, text))

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            await self.sleep(seconds)

        client = FakeClient(self.discord)

        def publisher_for(guild, scope, already, on_posted):
            publisher = DiscordPublisher(
                client,
                nonce_scope=scope,
                on_posted=on_posted,
                already_posted=already,
                skip_permanent=True,
                guild_id=guild.guild_id,
            )
            self.publishers.append(weakref.ref(publisher))
            return publisher

        args = dict(
            cfg=self.cfg,
            db_path=self.db_path,
            now=self.now,
            publisher_for=publisher_for,
            notify_guild=notify,
            sleep=sleep,
        )
        args.update(overrides)
        return GuildDigestDeps(**args)

    def statuses(self, run_date) -> dict[str, int]:
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM digests WHERE run_date = ? GROUP BY status",
                (run_date.isoformat(),),
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def done(self, run_date) -> bool:
        found = self.statuses(run_date)
        return sum(found.get(s, 0) for s in ("ok", "partial")) >= len(self.plan)


class TickDriver:
    """The scheduler's minute job, minus APScheduler: a tick every `every_s`, until `done()`.

    `overlap=False` is what the real job does (`max_instances=1`: a tick that's still
    running swallows the ones that would have started). `overlap=True` is what happens if
    that guard ever fails, a second tick starting on top of the first, which is the case
    the claim and the lease exist for. Each tick runs as its own task so `kill()` can take
    all of them down at once.
    """

    def __init__(self, deps: GuildDigestDeps, *, overlap: bool, clock: Callable[[], float]) -> None:
        self.deps = deps
        self.overlap = overlap
        self.clock = clock
        self.tasks: list[asyncio.Task] = []
        self.ticks: list[list[GuildDigestOutcome]] = []
        self.spans: list[tuple[float, float]] = []
        self.killed = False
        self._main: asyncio.Task | None = None

    def kill(self) -> None:
        self.killed = True
        for task in self.tasks:
            task.cancel()
        if self._main is not None:
            self._main.cancel()

    async def _tick(self) -> None:
        started = self.clock()
        outcomes = await run_due_guilds(self.deps)
        self.ticks.append(outcomes)
        self.spans.append((started, self.clock()))

    async def run(
        self, done: Callable[[], bool], *, every_s: float = 60.0, max_ticks: int = 60
    ) -> None:
        self._main = asyncio.current_task()
        try:
            for _ in range(max_ticks):
                begun = self.clock()
                task = asyncio.create_task(self._tick())
                self.tasks.append(task)
                if not self.overlap:
                    await task
                if done():
                    break
                spent = self.clock() - begun
                await asyncio.sleep(every_s - (spent % every_s) if not self.overlap else every_s)
            await asyncio.gather(*self.tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # `kill()`: the process is gone, which is the whole experiment.
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.killed = True

    @property
    def outcomes(self) -> list[GuildDigestOutcome]:
        return [o for tick in self.ticks for o in tick]


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)
