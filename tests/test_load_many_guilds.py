"""What happens at 09:00 when three hundred servers want their digest at once (plan task 14).

Everything runs on a virtual clock: a hand-rolled event loop whose idea of "wait
five minutes" is to add 300 to a number and carry on. Nothing sleeps for real,
so a simulated morning takes a few seconds of actual time (mostly SQLite opening
connections, which is fair; it's about three thousand of them). The two real things
that need the clock (the lease heartbeat and the per-server timeout) use the
loop's time, so they run on the same fake wall as the tick's pacing and the
retry backoff, and nothing is quietly cheating on the side.

The other rule is that only Discord and the clock are fake. The database is a
real SQLite file, `run_due_guilds` is the real tick, the publisher is the real
`DiscordPublisher`, and the SHiFT side is the real `DiscordCodeAlertPoster`
behind the real fan-out. The one liberty: `asyncio.to_thread` runs inline here.
A virtual clock and real worker threads have opinions about each other (the
clock can't know a thread is about to finish), and SQLite doesn't need the
thread to be correct, only to be polite to an event loop.

Why `load_world.py` has the fake Discord: so the real-time script can use the
same one.
"""

from __future__ import annotations

import asyncio
import gc
import random
import selectors
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest
from load_world import (
    SHIFT_SLOT,
    CrashPlan,
    DigestWorld,
    FakeClient,
    FakeDiscord,
    TickDriver,
    build_digest_world,
    channel_for,
    guild_id_for,
    load_load_config,
    utc,
)

from newsbot.bot.client import DiscordCodeAlertPoster
from newsbot.collectors.base import RawItem
from newsbot.guilds.schedule import local_due_instant
from newsbot.lounge.welcome import RecentWelcomes
from newsbot.pipeline.collect import _SHIFT_HOOK_TIMEOUT_S
from newsbot.pipeline.guild_digest import GuildDigestDeps
from newsbot.pipeline.run import _PUBLISH_BACKOFF_S
from newsbot.shift.fanout import FanoutDeps, detect_and_fan_out
from newsbot.store import repo
from newsbot.store.db import connect, migrate

GUILDS = 300
# Four zones that all sit at UTC-7 on 2026-09-30 (Pacific daylight time, plus three that
# don't observe it or use it year round), so all 300 servers are due in the same minute.
# That's the worst case, and the only honest one for a load test.
ZONES = ("America/Los_Angeles", "America/Vancouver", "America/Phoenix", "America/Whitehorse")
DAY = date(2026, 9, 30)
DUE = utc(2026, 9, 30, 16, 0)
# The first tick lands half a minute early (nothing due yet), then every minute after.
START = DUE - timedelta(seconds=30)
SEED = 20260930
MIN_GAMES = {149: 3}  # the server that dies mid-digest in the crash test has games to spare


def seeded() -> random.Random:
    return random.Random(SEED)  # noqa: S311 (a repeatable shuffle, not a secret)


class VirtualClock:
    """The loop's time, as a number a test can read and move."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self, start: datetime = START) -> datetime:
        return start + timedelta(seconds=self.value)


class _VirtualSelector:
    """Wraps the real selector: poll for I/O, but never actually wait. Waiting adds to the clock."""

    def __init__(self, real: selectors.BaseSelector, clock: VirtualClock) -> None:
        self._real = real
        self._clock = clock

    def select(self, timeout=None):
        events = self._real.select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            raise RuntimeError("virtual loop deadlock: nothing scheduled and nothing ready")
        self._clock.value += timeout
        return events

    def __getattr__(self, name):
        return getattr(self._real, name)


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self._clock = clock
        self._selector = _VirtualSelector(self._selector, clock)

    def time(self) -> float:
        return self._clock.value


def run_virtual(clock: VirtualClock, coro):
    with asyncio.Runner(loop_factory=lambda: VirtualLoop(clock)) as runner:
        return runner.run(coro)


async def _inline(func, /, *args, **kwargs):
    return func(*args, **kwargs)


def _patch_world(patch: pytest.MonkeyPatch) -> None:
    patch.setattr(asyncio, "to_thread", _inline)


@pytest.fixture(autouse=True)
def fake_world_plumbing(monkeypatch):
    _patch_world(monkeypatch)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    return load_load_config()


@dataclass
class Rush:
    """One finished morning rush, shared by the tests that only look at what it left behind."""

    clock: VirtualClock
    world: DigestWorld
    deps: GuildDigestDeps
    driver: TickDriver


@pytest.fixture(scope="module")
def rush(tmp_path_factory):
    """Three hundred servers, one sequential tick loop, run once (SQLite fsyncs add up)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("BRAVE_API_KEY", "test-key")
        _patch_world(patch)
        clock = VirtualClock()
        world = make_world(tmp_path_factory.mktemp("rush"), load_load_config(), clock)
        deps = world.deps()
        driver = tick_driver(world, deps, clock)
        run_virtual(clock, driver.run(lambda: world.done(DAY)))
        return Rush(clock, world, deps, driver)


def make_world(tmp_path, cfg, clock: VirtualClock, *, p429=0.03, latency=0.05) -> DigestWorld:
    plan = build_digest_world(
        str(tmp_path / "load.db"),
        guilds=GUILDS,
        games_max=10,
        seed=SEED,
        zones=ZONES,
        due=DUE,
        min_games=MIN_GAMES,
    )
    discord_ = FakeDiscord(
        clock=lambda: clock.value,
        sleep=asyncio.sleep,
        rng=seeded(),
        latency_s=latency,
        p429=p429,
    )
    return DigestWorld(
        cfg=cfg,
        db_path=str(tmp_path / "load.db"),
        plan=plan,
        discord=discord_,
        now=clock.now,
        sleep=asyncio.sleep,
    )


def tick_driver(world, deps, clock, *, overlap=False) -> TickDriver:
    return TickDriver(deps, overlap=overlap, clock=lambda: clock.value)


def every_channel_exactly_once(world: DigestWorld) -> None:
    for gid, followed in world.plan.items():
        for _game, channel_id in followed:
            assert world.discord.messages_in(channel_id) == 1, (gid, channel_id)


# --- the morning rush ---


def test_three_hundred_guilds_all_post_ok_and_the_pacing_adds_up(rush, record_property):
    world, deps, driver = rush.world, rush.deps, rush.driver

    assert world.statuses(DAY) == {"ok": GUILDS}
    every_channel_exactly_once(world)
    sends = sum(len(v) for v in world.plan.values())
    assert world.discord.total_landed == sends
    # The real publisher paced itself: nothing was told "too many" by the limits themselves.
    # The 429s it did see were ours, and the real retry code walked through every one.
    assert world.discord.natural_429s == 0
    assert world.discord.injected_429s > 0
    assert world.notices == []

    # One tick did all of it (the minute job never starts a second one on top of the first).
    busy = [(span, tick) for span, tick in zip(driver.spans, driver.ticks, strict=True) if tick]
    assert len(busy) == 1
    (began, ended), outcomes = busy[0]
    assert sorted(o.guild_id for o in outcomes) == sorted(world.plan)
    assert {o.status for o in outcomes} == {"ok"}

    # The sleeps: a pace before every server but the first, and the retry backoff for each 429.
    pace_sleeps = [s for s in world.sleeps if s == deps.pace_s]
    backoffs = [s for s in world.sleeps if s != deps.pace_s]
    assert len(pace_sleeps) == GUILDS - 1
    expected_backoff = sum(
        sum(_PUBLISH_BACKOFF_S[:k]) for k in world.discord.rejected_by_guild.values()
    )
    assert sum(backoffs) == pytest.approx(expected_backoff)
    # And every virtual second of the tick is accounted for: pacing, backoff, and the
    # round trip of every send attempt (the rejected ones paid for a round trip too).
    accounted = len(pace_sleeps) * deps.pace_s + expected_backoff + world.discord.attempts * 0.05
    assert ended - began == pytest.approx(accounted, abs=1e-6)

    since_due = ended - (DUE - START).total_seconds()
    record_property("virtual_seconds_after_0900", round(since_due, 1))
    record_property("sends", sends)
    record_property("attempts", world.discord.attempts)
    record_property("injected_429s", world.discord.injected_429s)
    print(
        f"\n300 guilds: {since_due:.1f} virtual s after 09:00 ({since_due / 60:.1f} min), "
        f"{sends} messages, {world.discord.attempts} send attempts, "
        f"{world.discord.injected_429s} 429s ({len(backoffs)} backoff sleeps)"
    )
    assert since_due < 15 * 60


def test_the_four_zones_really_are_due_together():
    # If tzdata ever moves one of them, the rush above stops being a rush. Better to hear it here.
    assert {local_due_instant(DAY, "09:00", zone) for zone in ZONES} == {DUE}


def test_overlapping_ticks_never_claim_a_guild_twice(tmp_path, cfg, record_property):
    clock = VirtualClock()
    world = make_world(tmp_path, cfg, clock)
    deps = world.deps()
    driver = tick_driver(world, deps, clock, overlap=True)

    run_virtual(clock, driver.run(lambda: world.done(DAY)))

    assert len(driver.ticks) > 2  # the point is that several were alive at once
    assert world.statuses(DAY) == {"ok": GUILDS}
    every_channel_exactly_once(world)
    ran = [o for o in driver.outcomes if o.status != "skipped"]
    assert sorted(o.guild_id for o in ran) == sorted(world.plan)  # each guild exactly once
    assert sum(o.sent for o in ran) == sum(len(v) for v in world.plan.values())
    assert world.discord.natural_429s == 0
    record_property("overlapping_ticks", len(driver.ticks))
    record_property("skipped_by_claim", len(driver.outcomes) - len(ran))


# --- memory ---


def test_nothing_per_guild_is_kept_but_one_lock(rush):
    world, deps = rush.world, rush.deps
    assert deps.running == set()
    gc.collect()
    # 300 digests, 300 publishers, and none of them (or their channel caches) outlive their run.
    assert len(world.publishers) == GUILDS
    assert [ref for ref in world.publishers if ref() is not None] == []
    # The lock table used to be the one thing that remembered servers; it prunes now.
    assert deps._locks == {} and deps._users == {}
    # (No tracemalloc here: it would measure SQLite's page cache and the fake's ledger as
    # happily as anything of ours, and a flaky memory test is worse than none.)


def test_idle_locks_are_released_after_the_run(rush):
    assert len(rush.deps._locks) == 0


def test_recent_welcomes_holds_one_day_and_no_more():
    welcomes = RecentWelcomes()
    start = utc(2026, 9, 30, 9, 0)
    for guild in range(GUILDS):
        for member in range(5):
            assert welcomes.check_and_record((guild, member), start + timedelta(seconds=guild))
    assert len(welcomes._seen) == GUILDS * 5
    # A day later the first call prunes all of it; only the new entry is left.
    assert welcomes.check_and_record((0, 0), start + timedelta(days=2))
    assert len(welcomes._seen) == 1


# --- a crash at guild 150, and a restart ---


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_crash_midway_resumes_without_double_posts(tmp_path, cfg, when, record_property):
    clock = VirtualClock()
    world = make_world(tmp_path, cfg, clock)
    victim = guild_id_for(149)  # the 150th server in due order, which is guild id order
    driver = tick_driver(world, world.deps(), clock)
    world.discord.crash = CrashPlan(victim, 2, when, driver.kill)

    run_virtual(clock, driver.run(lambda: world.done(DAY)))

    assert driver.killed and world.discord.crash.fired
    # 149 servers finished, and the 150th is mid-digest: one game recorded, row `pending`.
    assert world.statuses(DAY) == {"ok": 149, "pending": 1}
    with closing(connect(world.db_path)) as conn:
        row = repo.get_guild_digest(conn, victim, DAY)
    assert row is not None and row.status == "pending"
    assert list(row.posted_by_game) == [world.plan[victim][0][0]]

    # The restart: a fresh process (new deps, so no locks and no `running`), once the dead
    # one's lease has gone stale. Nothing in memory survives; only the database does.
    clock.value += 11 * 60
    deps = world.deps()
    resumed = tick_driver(world, deps, clock)
    run_virtual(clock, resumed.run(lambda: world.done(DAY)))

    assert world.statuses(DAY) == {"ok": GUILDS}
    kinds = {o.guild_id: o for o in resumed.outcomes if o.status != "skipped"}
    assert sorted(kinds) == sorted(g for g in world.plan if g >= victim)  # nobody ran twice
    for followed in world.plan.values():
        for _game, channel_id in followed:
            assert world.discord.messages_in(channel_id) >= 1  # no lost post
    doubles = [ch for ch, msgs in world.discord.landed.items() if len(msgs) > 1]
    if when == "before":
        assert doubles == []
    else:
        # The documented worst case (D7): Discord said "sent", the row never heard. One game
        # in one server, and not a message more.
        assert doubles == [world.plan[victim][1][1]]
        assert len(world.discord.landed[doubles[0]]) == 2
    assert world.notices == []
    record_property("resumed_guilds", len(kinds))


# --- the SHiFT fan-out at scale ---

SHIFT_START = utc(2026, 10, 1, 8, 0)  # 01:00 in all four zones, the same local day
EARLIER = SHIFT_START - timedelta(days=1)


def shift_code(n: int) -> str:
    letter = "ABCDEFGHJK"[n]
    return f"{letter * 4}{n}-{letter * 5}-{letter * 5}-{letter * 5}-{letter * 5}"


def shift_item(n: int, at: datetime) -> RawItem:
    return RawItem(
        url=f"https://example.com/shift/{n}",
        title=f"Game 0 new SHiFT code: {shift_code(n)}",
        excerpt="",
        source_name="Gearbox Blog",
        trust="official",
        published_at=at - timedelta(hours=1),
    )


def shift_db(tmp_path) -> str:
    path = str(tmp_path / "shift.db")
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.record_silent_codes(conn, [], now=lambda: EARLIER, mark_seeded=True)
        for i in range(GUILDS):
            gid = guild_id_for(i)
            repo.create_guild(
                conn, gid, timezone=ZONES[i % len(ZONES)], set_up=True, now=lambda: EARLIER
            )
            repo.follow_game(conn, gid, "game00", channel_for(gid, 0))
            repo.set_shift(
                conn,
                gid,
                enabled=True,
                channel_id=channel_for(gid, SHIFT_SLOT),
                ping="everyone",
                now=lambda: EARLIER,
            )
    return path


@dataclass
class ShiftRun:
    deps: FanoutDeps
    discord: FakeDiscord
    clock: VirtualClock
    notices: list[tuple[int, str]]
    timeouts: int = 0
    passes_run: int = 0
    queued_after: list[int] = None

    def counts(self) -> dict[str, int]:
        with closing(connect(self.deps.db_path)) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM guild_code_posts GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def make_shift_run(tmp_path, cfg, clock, *, latency, rng=None) -> ShiftRun:
    cfg = cfg.model_copy(update={"shift": cfg.shift.model_copy(update={"games": ["game00"]})})
    discord_ = FakeDiscord(
        clock=lambda: clock.value,
        sleep=asyncio.sleep,
        rng=rng or seeded(),
        latency_s=latency,
    )
    client = FakeClient(discord_)
    notices: list[tuple[int, str]] = []

    async def notify_guild(guild_id: int, text: str) -> None:
        notices.append((guild_id, text))

    def poster_for(guild_id, channel_id, ping, notify):
        return DiscordCodeAlertPoster(client, channel_id, ping, notify, guild_id=guild_id)

    deps = FanoutDeps(
        cfg=cfg,
        db_path=shift_db(tmp_path),
        now=lambda: clock.now(SHIFT_START),
        poster_for=poster_for,
        notify_guild=notify_guild,
        sleep=asyncio.sleep,
        clock=lambda: clock.value,  # the walk's 100 s deadline runs on the virtual clock too
    )
    return ShiftRun(deps, discord_, clock, notices, queued_after=[])


async def hourly_passes(run: ShiftRun, drops: list[list[int]], *, max_passes: int) -> None:
    """The collection job, once an hour: `drops[k]` is the codes found in pass k (maybe none).

    Each pass has the hook's real timeout, so a delivery that doesn't fit is cut off exactly
    where the real one would be. Stops once nothing is queued and every drop has been through.
    """
    t0 = run.clock.value
    for k in range(max_passes):
        await asyncio.sleep(max(0.0, t0 + 3600 * k - run.clock.value))
        found = drops[k] if k < len(drops) else []
        items = [shift_item(n, run.clock.now(SHIFT_START)) for n in found]
        try:
            await asyncio.wait_for(
                detect_and_fan_out(run.deps, items, seeding_ok=True), _SHIFT_HOOK_TIMEOUT_S
            )
        except TimeoutError:
            run.timeouts += 1
        run.passes_run += 1
        queued = run.counts().get("queued", 0)
        run.queued_after.append(queued)
        if queued == 0 and k + 1 >= len(drops):
            return


@pytest.mark.parametrize(
    ("latency", "expected_passes", "expected_timeouts"),
    [
        # Per server a turn costs the 0.2 s pace plus one round trip. At 0.1 s that's 0.3 s and
        # all 300 fit in the walk's 100 s; at 0.5 s it's 0.7 s and only about 143 do; at 1 s
        # (Discord having a bad day) about 84 do. Changed with QA M2: the walk now stops
        # *starting* servers at 100 s of the hook's 120, so it ends a pass early (one more
        # pass than before at the slow speeds) instead of being cut off mid-send (no timeouts,
        # nobody stranded `failed`).
        (0.1, 1, 0),
        (0.5, 3, 0),
        (1.0, 4, 0),
    ],
)
def test_one_code_drop_to_300_guilds_drains_over_hourly_passes(
    tmp_path, cfg, latency, expected_passes, expected_timeouts, record_property
):
    clock = VirtualClock()
    run = make_shift_run(tmp_path, cfg, clock, latency=latency)

    run_virtual(clock, hourly_passes(run, [[0]], max_passes=6))

    final = run.counts()
    # Pinned: how many hourly passes the drain takes, and how many hit the hook's timeout.
    assert run.passes_run == expected_passes
    assert run.timeouts == expected_timeouts
    # The queue is what survives a timeout: after a cut-off pass there's still a list to work.
    assert all(q > 0 for q in run.queued_after[:-1]) and run.queued_after[-1] == 0
    assert "queued" not in final
    # Nobody is pinged twice, and nobody is posted to twice.
    for gid in range(GUILDS):
        channel_id = channel_for(guild_id_for(gid), SHIFT_SLOT)
        assert run.discord.messages_in(channel_id) <= 1
        assert sum(m.everyone for m in run.discord.landed.get(channel_id, [])) <= 1
    # Every server was owed one code. A timeout would strand the one server it caught mid-send
    # (its claim is written first, so a cut-off send is `failed`, never re-sent: lose one, never
    # double one), and that server is told. The deadline means there are none now; the
    # arithmetic below still allows for them. Everyone else got theirs.
    posted = final.get("posted", 0)
    failed = final.get("failed", 0)
    assert posted + failed == GUILDS
    assert failed <= run.timeouts
    assert run.discord.total_landed in (posted, posted + failed)  # a cut-off send may have landed
    assert len(run.notices) == failed
    assert run.discord.natural_429s == 0
    record_property("passes", run.passes_run)
    record_property("timeouts", run.timeouts)
    record_property("queued_after_each_pass", run.queued_after)
    print(
        f"\nSHiFT 300 guilds, round trip {latency}s: {run.passes_run} pass(es), "
        f"{run.timeouts} timeout(s), queue after each pass {run.queued_after}, "
        f"posted {posted}, failed {failed}"
    )


def test_the_ping_cap_holds_across_five_drops_at_300_guilds(tmp_path, cfg):
    clock = VirtualClock()
    run = make_shift_run(tmp_path, cfg, clock, latency=0.1)  # fast enough to drain each drop
    drops = [[n] for n in range(5)]  # five separate codes, an hour apart, one local day

    run_virtual(clock, hourly_passes(run, drops, max_passes=8))

    assert run.timeouts == 0
    cap = cfg.shift.max_pings_per_day
    assert cap == 3
    for i in range(GUILDS):
        gid = guild_id_for(i)
        msgs = run.discord.landed[channel_for(gid, SHIFT_SLOT)]
        assert len(msgs) == 5
        assert sum(m.everyone for m in msgs) == cap  # three pinged, then the cap
        assert [m.everyone for m in msgs] == [True, True, True, False, False]
    with closing(connect(run.deps.db_path)) as conn:
        assert {repo.get_shift(conn, guild_id_for(i)).ping_count for i in range(GUILDS)} == {cap}
    # Each server heard about its cap twice (the fourth and fifth alerts), and nothing else.
    assert len(run.notices) == 2 * GUILDS
    assert all("daily ping cap reached" in text for _, text in run.notices)


class _Script:
    """A `random.Random` stand-in that says "reject" once, then never again."""

    def __init__(self) -> None:
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return 0.0 if self.calls == 1 else 1.0


def test_a_429_on_a_code_alert_is_retried(tmp_path, cfg):
    clock = VirtualClock()
    run = make_shift_run(tmp_path, cfg, clock, latency=0.1, rng=_Script())
    run.discord.p429 = 0.5  # the script's first answer (0.0) rejects once; the rest pass

    run_virtual(clock, hourly_passes(run, [[0]], max_passes=1))

    landed = sum(
        run.discord.messages_in(channel_for(guild_id_for(i), SHIFT_SLOT)) for i in range(GUILDS)
    )
    assert landed == GUILDS  # every server got its code, the 429 included
    # The 429 was Discord refusing the message, so the retry kept its ping (D13): one server
    # was rejected once and still got its @everyone, and nobody got two.
    assert run.discord.injected_429s == 1
    pinged = [
        sum(m.everyone for m in run.discord.landed[channel_for(guild_id_for(i), SHIFT_SLOT)])
        for i in range(GUILDS)
    ]
    assert pinged == [1] * GUILDS


# --- the real-time script's safety rail ---


def _load_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "loadtest_digests.py"
    spec = importlib.util.spec_from_file_location("loadtest_digests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_loadtest_script_refuses_data_and_files_it_didnt_create(tmp_path):
    script = _load_script()
    with pytest.raises(SystemExit, match="inside data/"):
        script.check_db_path(script.ROOT / "data" / "newsbot.db")
    with pytest.raises(SystemExit, match="inside data/"):
        script.check_db_path(script.ROOT / "data" / ".." / "data" / "sub" / "x.db")
    existing = tmp_path / "mine.db"
    existing.write_text("precious")
    with pytest.raises(SystemExit, match="already exists"):
        script.check_db_path(existing)
    assert existing.read_text() == "precious"
    assert script.check_db_path(tmp_path / "fresh.db") == (tmp_path / "fresh.db").resolve()
