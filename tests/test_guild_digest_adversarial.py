"""Adversarial tests for the per-guild digest (plan task 6): never twice, never bleed, never stall.

The happy path is in `test_guild_digest.py` and it's a nice, well-behaved
afternoon. This file is the other kind of afternoon. The questions, in order
of how much they'd hurt if I got them wrong:

1. Can a server get the same digest twice? (Two ticks, two processes, a slow
   publisher that a second process mistakes for a dead one, a crash in the
   one-line gap between Discord saying "sent" and the row hearing about it.)
2. Can an item show up in two consecutive digests, or vanish between them?
   (Catch-up, forced re-runs, zone changes, DST days, a week of downtime.)
3. Can one server's bad day, or hung publisher, or deleted channel, reach
   another server, or the owner?
4. Does a comped server ever get free headlines when it has a real summary?

Everything runs the real `DiscordPublisher` against hand-written fake channels
and a real temp database with the real migrations. No network, and both the
retry backoff and the pace between servers are patched out. Strict xfails
are real holes in the implementation; each says what it found.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import discord
import pytest

from newsbot.bot.client import DiscordPublisher
from newsbot.config import load_config
from newsbot.guilds.schedule import MAX_ATTEMPTS, RETRY_AFTER
from newsbot.pipeline.guild_digest import (
    GameSummary,
    GuildDigestDeps,
    guild_needs_confirmation,
    is_guild_busy,
    preview_guild_digest,
    run_due_guilds,
    run_guild_digest,
    todays_guild_digest,
)
from newsbot.pipeline.run import _PUBLISH_BACKOFF_S, RunKind
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import GuildDigestRow, StoredItem

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
LA = "America/Los_Angeles"
DUE = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)  # 09:00 PDT
NOW = DUE + timedelta(seconds=30)
DAY = date(2026, 9, 30)
G1, G2, G3 = 1001, 1002, 1003
BL4, PAL, RUST = "borderlands4", "palworld", "rust"


def ch(guild_id: int, index: int) -> int:
    """Channel ids are derived: a guild's game channels are 1..8, its admin channel is 9."""
    return guild_id * 100 + index


class _Response:
    def __init__(self, status: int) -> None:
        self.status, self.reason, self.headers, self.request_info = status, "x", {}, None


class FakeChannel:
    def __init__(self) -> None:
        self.embeds: list[discord.Embed] = []
        self.contents: list[str] = []
        self.nonces: list[str | None] = []
        self.fail: list[Exception] = []
        self.always_fail: Exception | None = None
        self.gate: asyncio.Event | None = None  # first send waits for this
        self.entered = asyncio.Event()
        self.hang = False
        self.on_send = None  # a hook: called (sync) right before a send lands
        self.attempts = 0

    async def send(self, content=None, *, embed=None, allowed_mentions=None, nonce=None):
        self.attempts += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.gate is not None:
            gate, self.gate = self.gate, None
            self.entered.set()
            await gate.wait()
        if self.always_fail is not None:
            raise self.always_fail
        if self.fail:
            raise self.fail.pop(0)
        assert allowed_mentions is not None and allowed_mentions.everyone is False
        assert allowed_mentions.users is False and allowed_mentions.roles is False
        if self.on_send is not None:
            self.on_send()
        if embed is None:
            self.contents.append(content)
        else:
            self.embeds.append(embed)
            self.nonces.append(nonce)
        return type("Msg", (), {"id": World.next_message_id()})()


class FakeClient:
    def __init__(self, world: World) -> None:
        self.world = world

    def get_channel(self, channel_id):
        return None if channel_id in self.world.gone else self.world.channels[channel_id]

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(_Response(404), "unknown channel")


class World:
    _ids = 70000

    @classmethod
    def next_message_id(cls) -> int:
        cls._ids += 1
        return cls._ids

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = str(tmp_path / "t.db")
        with closing(connect(self.db_path)) as conn:
            migrate(conn)
        self.cfg = load_config(V3)
        self.clock = NOW
        self.channels: defaultdict[int, FakeChannel] = defaultdict(FakeChannel)
        self.gone: set[int] = set()
        self.notices: list[tuple[int, str]] = []
        self.reports: list[tuple[int, int, str]] = []
        self.sleeps: list[float] = []
        self.summaries: dict[str, GameSummary | None] = {}
        self.factory_boom: dict[int, BaseException] = {}
        self.notify_boom: set[int] = set()
        self.report_boom: set[int] = set()

    def deps(self, **overrides) -> GuildDigestDeps:
        async def notify(guild_id: int, text: str) -> None:
            if guild_id in self.notify_boom:
                raise RuntimeError("notifier down")
            self.notices.append((guild_id, text))

        async def send_report(guild_id: int, channel_id: int, text: str) -> None:
            if guild_id in self.report_boom:
                raise RuntimeError("report channel gone")
            self.reports.append((guild_id, channel_id, text))

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        async def summary_for(game_key: str, due_at: datetime):
            return self.summaries.get(game_key)

        def publisher_for(guild, scope, already, on_posted):
            if guild.guild_id in self.factory_boom:
                raise self.factory_boom[guild.guild_id]
            return DiscordPublisher(
                FakeClient(self),
                nonce_scope=scope,
                on_posted=on_posted,
                already_posted=already,
                skip_permanent=True,
            )

        args = dict(
            cfg=self.cfg,
            db_path=self.db_path,
            now=lambda: self.clock,
            publisher_for=publisher_for,
            notify_guild=notify,
            send_report=send_report,
            summary_for=summary_for,
            sleep=sleep,
        )
        args.update(overrides)
        return GuildDigestDeps(**args)

    def conn(self):
        return closing(connect(self.db_path))

    def add_guild(
        self, guild_id, *, tier="free", games=(BL4, PAL), time="09:00", tz=LA, admin=True
    ):
        with self.conn() as conn:
            repo.create_guild(
                conn,
                guild_id,
                digest_time=time,
                timezone=tz,
                admin_channel_id=ch(guild_id, 9) if admin else None,
                tier=tier,
                set_up=True,
                now=lambda: NOW,
            )
            repo.set_guild_games(
                conn, guild_id, [(k, ch(guild_id, i + 1)) for i, k in enumerate(games)]
            )

    def add_item(self, game, title, collected, *, trust="press", uncertain=False, url=None):
        with self.conn() as conn:
            repo.store_items(
                conn,
                [
                    StoredItem(
                        url=url or f"https://example.com/{game}/{title.replace(' ', '-')}",
                        title=title,
                        excerpt="",
                        source_name="Feed",
                        trust=trust,
                        published_at=None,
                        topics={game: uncertain},
                    )
                ],
                now=lambda: collected,
            )

    def digest(self, guild_id, run_date=DAY) -> GuildDigestRow | None:
        with self.conn() as conn:
            return repo.get_guild_digest(conn, guild_id, run_date)

    def set_status(self, guild_id, status, run_date=DAY):
        with self.conn() as conn:
            conn.execute(
                "UPDATE digests SET status = ? WHERE guild_id = ? AND run_date = ?",
                (status, guild_id, run_date.isoformat()),
            )
            conn.commit()

    def titles(self, channel_id: int) -> list[str]:
        """Every `item-N` headline shown in a channel's embeds, in post order."""
        return [t for e in self.channels[channel_id].embeds for t in shown_in(e)]

    def sends(self, channel_id: int) -> int:
        return len(self.channels[channel_id].embeds)


def shown_in(embed) -> list[str]:
    """The `item-N` titles in one embed's description (not the URLs, which repeat the slug)."""
    return re.findall(r"• (?:🟢 OFFICIAL · )?(item-\d+) — ", embed.description or "")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def in_window(hours_before_due: float = 2) -> datetime:
    return DUE - timedelta(hours=hours_before_due)


# --- never post twice ---


async def test_two_overlapping_ticks_in_one_process_post_each_game_once(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    deps = world.deps()

    first, second = await asyncio.gather(run_due_guilds(deps), run_due_guilds(deps))

    assert world.sends(ch(G1, 1)) == 1 and world.sends(ch(G1, 2)) == 1
    statuses = sorted(o.status for o in [*first, *second])
    assert statuses == ["ok", "skipped"]
    assert world.digest(G1).attempts == 1


async def test_a_slow_publisher_is_not_resumed_by_the_next_tick_of_the_same_process(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    release = asyncio.Event()
    world.channels[ch(G1, 1)].gate = release
    deps = world.deps()

    slow = asyncio.create_task(run_due_guilds(deps))
    await world.channels[ch(G1, 1)].entered.wait()
    assert is_guild_busy(deps, G1)
    world.clock = NOW + timedelta(minutes=30)
    assert await asyncio.wait_for(run_due_guilds(deps), 5) == []
    release.set()
    await slow

    assert world.sends(ch(G1, 1)) == 1 and world.sends(ch(G1, 2)) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "`running` is per process and the claim doesn't look at how fresh a `pending` "
        "row is, so a second process's tick sees the first process's in-flight digest "
        "as a dead one, resumes it, and both publish. Real double post."
    ),
)
async def test_a_second_process_does_not_resume_a_digest_the_first_is_still_publishing(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    release = asyncio.Event()
    world.channels[ch(G1, 1)].gate = release
    process_a, process_b = world.deps(), world.deps()  # separate `running` sets and locks

    in_flight = asyncio.create_task(run_due_guilds(process_a))
    await world.channels[ch(G1, 1)].entered.wait()
    world.clock = NOW + timedelta(seconds=60)
    await asyncio.wait_for(run_due_guilds(process_b), 5)
    release.set()
    await in_flight

    assert world.sends(ch(G1, 1)) == 1
    assert world.sends(ch(G1, 2)) == 1


async def test_run_now_without_force_during_a_scheduled_run_posts_nothing_extra(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    release = asyncio.Event()
    world.channels[ch(G1, 1)].gate = release
    deps = world.deps()

    scheduled = asyncio.create_task(run_due_guilds(deps))
    await world.channels[ch(G1, 1)].entered.wait()
    run_now = asyncio.create_task(run_guild_digest(deps, G1, kind=RunKind.RUN_NOW))
    await asyncio.sleep(0)
    assert not run_now.done()  # queued behind the lock, not racing the claim
    release.set()
    [scheduled_outcome] = await scheduled
    run_now_outcome = await run_now

    assert scheduled_outcome.status == "ok" and run_now_outcome.status == "skipped"
    assert world.sends(ch(G1, 1)) == 1


async def test_a_confirmed_run_now_during_a_scheduled_run_waits_and_then_reposts_on_purpose(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    release = asyncio.Event()
    world.channels[ch(G1, 1)].gate = release
    deps = world.deps()

    scheduled = asyncio.create_task(run_due_guilds(deps))
    await world.channels[ch(G1, 1)].entered.wait()
    forced = asyncio.create_task(run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True))
    await asyncio.sleep(0)
    release.set()
    await scheduled
    assert (await forced).status == "ok"

    assert world.sends(ch(G1, 1)) == 2  # the admin said yes to a repost
    assert len(set(world.channels[ch(G1, 1)].nonces)) == 2  # and Discord is told it's a new one
    assert world.digest(G1).attempts == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Two ticks that both read 'no row yet' queue on the guild lock; the second then "
        "claims the clean `failed` row the first just wrote, with no 10-minute gap and no "
        "look at the attempt count, so a broken server is hit again immediately."
    ),
)
async def test_the_second_overlapping_tick_does_not_retry_a_failure_inside_the_ten_minute_gap(
    world,
):
    # The claim doesn't know about the retry spacing; only the due check does, and two ticks
    # that both read "no row yet" get past it. A broken server gets hit twice, back to back.
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    world.channels[ch(G1, 1)].always_fail = discord.HTTPException(_Response(500), "boom")
    deps = world.deps()

    await asyncio.gather(run_due_guilds(deps), run_due_guilds(deps))

    first_tick_attempts = 1 + len(_PUBLISH_BACKOFF_S)  # one try plus the whole backoff schedule
    assert world.channels[ch(G1, 1)].attempts <= first_tick_attempts


@pytest.mark.parametrize("crash_on", [BL4, PAL])
@pytest.mark.parametrize("how", ["killed", "cancelled"])
async def test_a_crash_between_send_and_write_through_duplicates_at_most_that_one_game(
    world, crash_on, how
):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    state = {"crashed": False}

    class Crash(BaseException):
        pass

    def factory(guild, scope, already, on_posted):
        async def wrapped(key, message_id):
            if key == crash_on and not state["crashed"]:
                state["crashed"] = True
                raise Crash
            await on_posted(key, message_id)

        return DiscordPublisher(
            FakeClient(world),
            nonce_scope=scope,
            on_posted=wrapped,
            already_posted=already,
            skip_permanent=True,
        )

    deps = world.deps(publisher_for=factory)
    with pytest.raises(Crash):
        await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    if how == "killed":
        world.set_status(G1, "pending")  # a SIGKILL never gets to write "failed"

    world.clock = NOW + RETRY_AFTER + timedelta(minutes=1)
    await run_due_guilds(deps)
    world.clock += timedelta(hours=1)
    await run_due_guilds(deps)

    sends = {k: world.sends(ch(G1, i)) for i, k in ((1, BL4), (2, PAL))}
    other = PAL if crash_on == BL4 else BL4
    if how == "killed" or crash_on == BL4:
        # Exactly one duplicate, of exactly the game whose write-through was lost.
        assert sends == {crash_on: 2, other: 1}
        assert world.digest(G1).status == "ok"
    else:
        # A graceful cancel after the first game is out leaves `failed` with posts, which
        # no tick retries (see the gap test below), so nothing is duplicated and nothing resumes.
        assert sends == {BL4: 1, PAL: 1}
        assert world.digest(G1).status == "failed"
    if how == "killed":
        # Discord can dedupe the repeat: the resume reuses the same nonce for that game.
        nonces = world.channels[ch(G1, 1 if crash_on == BL4 else 2)].nonces
        assert nonces[0] == nonces[1]


async def test_a_write_through_failure_is_logged_and_the_digest_still_posts_once(
    world, monkeypatch, caplog
):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("newsbot.pipeline.guild_digest.repo.record_posted_game", boom)
    with caplog.at_level(logging.ERROR):
        outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)

    assert outcome.status == "ok"
    assert world.sends(ch(G1, 1)) == 1 and world.sends(ch(G1, 2)) == 1
    assert "write-through" in caplog.text
    assert set(world.digest(G1).posted_by_game) == {BL4, PAL}  # the final save still wrote both


async def test_a_second_tick_after_a_finished_digest_posts_nothing(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    deps = world.deps()
    await run_due_guilds(deps)
    for minutes in (1, 10, 60, 600):
        world.clock = NOW + timedelta(minutes=minutes)
        assert await run_due_guilds(deps) == []
    assert world.sends(ch(G1, 1)) == 1


async def test_a_clock_stepped_back_after_a_digest_does_not_repost(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    deps = world.deps()
    await run_due_guilds(deps)
    world.clock = NOW - timedelta(days=1, hours=3)
    assert await run_due_guilds(deps) == []
    world.clock = NOW + timedelta(seconds=1)
    assert await run_due_guilds(deps) == []
    assert world.sends(ch(G1, 1)) == 1


async def test_a_failure_with_posts_is_never_auto_retried_but_a_forced_run_reposts_everything(
    world,
):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    world.channels[ch(G1, 2)].always_fail = discord.HTTPException(_Response(500), "boom")
    deps = world.deps()
    outcome = await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "failed" and list(outcome.posted_by_game) == [BL4]
    row = world.digest(G1)
    assert guild_needs_confirmation(row) is True

    world.channels[ch(G1, 2)].always_fail = None
    world.clock = NOW + timedelta(hours=3)
    assert await run_due_guilds(deps) == []  # an admin's call, as in v2
    forced = await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True)

    assert forced.status == "ok"
    assert world.sends(ch(G1, 1)) == 2  # force means everything again
    assert set(world.digest(G1).posted_by_game) == {BL4, PAL}
    assert world.digest(G1).posted_by_game[BL4] != row.posted_by_game[BL4]


async def test_a_cancelled_digest_with_one_game_out_is_not_finished_by_a_later_tick(world):
    # Documents a gap, not a pass/fail claim about the plan: a hard kill leaves `pending`,
    # which D7 resumes; a graceful cancel (a deploy) leaves `failed` with posts, which only
    # an admin's run-now finishes. The second game of that digest is simply late until then.
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    world.channels[ch(G1, 2)].hang = True
    deps = world.deps()
    task = asyncio.create_task(run_guild_digest(deps, G1, kind=RunKind.SCHEDULED))
    for _ in range(500):  # real threads do the database writes, so poll rather than spin
        await asyncio.sleep(0.01)
        if world.channels[ch(G1, 2)].attempts:
            break
    assert world.channels[ch(G1, 2)].attempts == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    world.channels[ch(G1, 2)].hang = False

    world.clock = NOW + timedelta(hours=2)
    assert await run_due_guilds(deps) == []
    assert world.sends(ch(G1, 2)) == 0
    assert world.digest(G1).status == "failed"


# --- the due rule, end to end ---


async def test_a_clean_failure_is_retried_after_ten_minutes_and_not_before(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    world.channels[ch(G1, 1)].fail = [discord.HTTPException(_Response(500), "x")] * 5
    deps = world.deps()
    assert (await run_due_guilds(deps))[0].status == "failed"

    world.clock = NOW + RETRY_AFTER  # exactly ten minutes: not yet ("more than")
    assert await run_due_guilds(deps) == []
    world.clock = NOW + RETRY_AFTER + timedelta(seconds=1)
    [retried] = await run_due_guilds(deps)
    assert retried.status == "ok"
    assert world.sends(ch(G1, 1)) == 1


async def test_retries_stop_after_the_attempt_cap_and_the_digest_stays_failed(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    world.channels[ch(G1, 1)].always_fail = discord.HTTPException(_Response(500), "x")
    deps = world.deps()
    for attempt in range(MAX_ATTEMPTS + 3):
        world.clock = NOW + attempt * (RETRY_AFTER + timedelta(minutes=1))
        await run_due_guilds(deps)
    assert world.digest(G1).attempts == MAX_ATTEMPTS
    assert world.digest(G1).status == "failed"


async def test_a_guild_set_up_after_its_time_gets_a_late_digest_ending_at_the_due_instant(world):
    # "Late, not cancelled." The window still ends at 09:00, not at 14:00, so what
    # was collected between is in tomorrow's digest instead of lost. The catch: a
    # brand-new server's first digest is up to a few hours stale.
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=1))
    world.add_item(BL4, "item-2", DUE + timedelta(hours=3))
    world.clock = DUE + timedelta(hours=5)  # 14:00 local

    [outcome] = await run_due_guilds(world.deps())

    assert outcome.status == "ok"
    assert world.titles(ch(G1, 1)) == ["item-1"]
    assert "catch-up" in world.reports[0][2]
    world.clock = DUE + timedelta(days=1, seconds=30)
    await run_due_guilds(world.deps())
    assert world.titles(ch(G1, 1)) == ["item-1", "item-2"]


async def test_the_report_calls_a_run_catch_up_at_five_minutes_late_not_before(world):
    world.add_guild(G1, games=(BL4,))
    world.add_guild(G2, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    with world.conn() as conn:
        repo.update_guild_settings(conn, G2, digest_time="09:01")
    world.clock = DUE + timedelta(minutes=5) + timedelta(seconds=59)  # G1 late by 5:59, G2 by 4:59
    await run_due_guilds(world.deps())
    by_guild = {g: text for g, _, text in world.reports}
    assert "(catch-up)" in by_guild[G1]
    assert "(scheduled)" in by_guild[G2]


async def test_a_bad_zone_guild_is_skipped_and_the_others_still_post(world):
    world.add_guild(G1, games=(BL4,))
    world.add_guild(G2, games=(BL4,))
    world.add_guild(G3, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET timezone = 'Mars/Olympus' WHERE guild_id = ?", (G1,))
        conn.commit()

    outcomes = await run_due_guilds(world.deps())

    assert [(o.guild_id, o.status) for o in outcomes] == [(G2, "ok"), (G3, "ok")]
    assert world.notices == []


async def test_run_now_for_a_guild_whose_zone_vanished_raises_before_claiming_anything(world):
    # Documents current behavior: the scheduled path skips a bad zone quietly, but the
    # command paths (run-now, preview, the confirmation check) let the error out. The
    # command layer has to catch it, and nothing is left `pending`.
    world.add_guild(G1, games=(BL4,))
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET timezone = 'Mars/Olympus' WHERE guild_id = ?", (G1,))
        conn.commit()
    deps = world.deps()
    with pytest.raises(ZoneInfoNotFoundError):
        await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True)
    with pytest.raises(ZoneInfoNotFoundError):
        await preview_guild_digest(deps, G1)
    with pytest.raises(ZoneInfoNotFoundError):
        await todays_guild_digest(deps, G1)
    with world.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert not is_guild_busy(deps, G1) and G1 not in deps.running


async def test_a_zone_whose_local_date_moves_forward_starts_a_new_day_with_chained_windows(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    deps = world.deps()
    await run_due_guilds(deps)  # LA 09:00 on the 30th, window ends 16:00Z

    world.add_item(BL4, "item-2", DUE + timedelta(hours=2))
    with world.conn() as conn:
        repo.update_guild_settings(conn, G1, timezone="Asia/Tokyo")
    # Tokyo's 09:00 on Oct 1 is 00:00Z on Oct 1, eight hours after the LA digest's end.
    world.clock = datetime(2026, 10, 1, 0, 0, 30, tzinfo=UTC)
    await run_due_guilds(deps)

    assert world.titles(ch(G1, 1)) == ["item-1", "item-2"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "digest_window falls back to a fresh 24h window when the previous end is not "
        "before this end. A zone change that makes the new local day's due instant land "
        "at or before the last digest's end therefore re-shows the last digest's items."
    ),
)
async def test_a_zone_change_that_pulls_the_next_digest_earlier_does_not_repeat_items(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())
    deps = world.deps()
    await run_due_guilds(deps)  # window ends 16:00Z on the 30th

    # Switch to Tokyo at 01:00: on Oct 1 (JST) that is 16:00Z on the 30th, the same
    # instant the last window ended. It's due right now, and its window is...
    with world.conn() as conn:
        repo.update_guild_settings(conn, G1, timezone="Asia/Tokyo", digest_time="01:00")
    world.clock = DUE + timedelta(hours=1)
    await run_due_guilds(deps)

    assert world.titles(ch(G1, 1)).count("item-1") == 1


async def test_a_clock_step_back_across_a_local_midnight_does_not_double_a_day(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE + timedelta(hours=2))
    deps = world.deps()
    world.clock = DUE + timedelta(days=1, seconds=30)  # digest for Oct 1
    await run_due_guilds(deps)
    world.clock = DUE + timedelta(hours=10)  # clock falls back to Sep 30 evening
    assert await run_due_guilds(deps) == []
    world.clock = DUE + timedelta(days=1, hours=1)
    assert await run_due_guilds(deps) == []
    assert world.sends(ch(G1, 1)) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A crash that leaves a day's digest half-posted is only resumed on its own local "
        "date. Restart the next day and that day is 'first', the old row is abandoned, and "
        "its chained window end hides the unposted game's items from every later digest."
    ),
)
async def test_a_game_a_crashed_digest_never_posted_is_not_lost_when_the_restart_is_a_day_late(
    world,
):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", in_window())
    world.add_item(PAL, "item-2", in_window())
    window = (DUE - timedelta(hours=24), DUE)
    with world.conn() as conn:
        claim = repo.claim_guild_digest(conn, G1, DAY, force=False, window=window, now=lambda: NOW)
        repo.record_posted_game(conn, claim.digest_id, BL4, 424242)
    world.clock = DUE + timedelta(days=1, minutes=1)  # the bot is back, a day later
    deps = world.deps()
    await run_due_guilds(deps)
    world.clock = DUE + timedelta(days=2, minutes=1)
    await run_due_guilds(deps)

    assert "item-2" in world.titles(ch(G1, 2))


# --- windows ---


def stream_items(world, start: datetime, count: int, *, step_min=60, game=BL4, prefix=0):
    """`count` uniquely named items, `step_min` apart, the first one step after `start`."""
    for n in range(count):
        world.add_item(
            game, f"item-{prefix + n:04d}", start + timedelta(minutes=step_min * (n + 1))
        )


def numbers(embed) -> set[int]:
    return {int(t[5:]) for t in shown_in(embed)}


async def test_a_catch_up_window_ends_at_the_due_instant_and_the_next_digest_picks_up_the_rest(
    world,
):
    world.add_guild(G1, games=(BL4,))
    stream_items(world, DUE - timedelta(days=1), 72)  # hourly, DUE-23h through DUE+48h
    deps = world.deps()
    world.clock = DUE + timedelta(hours=4)  # the bot was down at 09:00
    [late] = await run_due_guilds(deps)
    assert late.status == "ok"
    world.clock = DUE + timedelta(days=1, seconds=30)
    await run_due_guilds(deps)

    first, second = (world.digest(G1, DAY), world.digest(G1, DAY + timedelta(days=1)))
    assert first.window_end == DUE and second.window_start == DUE
    one, two = world.channels[ch(G1, 1)].embeds
    assert numbers(one) == set(range(0, 24))  # the four hours of downtime are not in it...
    assert numbers(two) == set(range(24, 48))  # ...they're at the front of the next one


async def test_consecutive_digests_across_a_dst_day_partition_the_items_exactly(world):
    world.add_guild(G1, games=(BL4,))
    first_end = datetime(2026, 3, 6, 17, 0, tzinfo=UTC)  # 09:00 PST, Mar 6
    stream_items(world, first_end - timedelta(hours=24), 24 * 4)  # hourly, 96 items
    deps = world.deps()
    for clock in (
        datetime(2026, 3, 6, 17, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 7, 17, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 8, 16, 0, 30, tzinfo=UTC),  # 09:00 PDT: a 23-hour day
        datetime(2026, 3, 9, 16, 0, 30, tzinfo=UTC),
    ):
        world.clock = clock
        await run_due_guilds(deps)

    sets = [numbers(e) for e in world.channels[ch(G1, 1)].embeds]
    assert [len(x) for x in sets] == [24, 24, 23, 24]
    assert set().union(*sets) == set(range(95))  # every item, exactly once
    assert sum(len(x) for x in sets) == 95


async def test_items_exactly_on_the_window_edges(world):
    world.add_guild(G1, games=(BL4,))
    start = DUE - timedelta(hours=24)
    world.add_item(BL4, "item-0001", start)  # on the start: excluded
    world.add_item(BL4, "item-0002", start + timedelta(microseconds=1))  # just inside
    world.add_item(BL4, "item-0003", DUE)  # on the end: included
    world.add_item(BL4, "item-0004", DUE + timedelta(microseconds=1))  # just past: tomorrow's
    deps = world.deps()
    await run_due_guilds(deps)
    world.clock = DUE + timedelta(days=1, seconds=30)
    await run_due_guilds(deps)

    today, tomorrow = world.channels[ch(G1, 1)].embeds
    assert shown_in(today) == ["item-0003", "item-0002"]
    assert shown_in(tomorrow) == ["item-0004"]


async def test_an_item_with_a_collected_at_in_the_future_waits_for_its_own_digest(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.add_item(BL4, "item-0002", DUE + timedelta(days=3))  # a clock-skewed collector
    deps = world.deps()
    await run_due_guilds(deps)
    assert world.titles(ch(G1, 1)) == ["item-0001"]
    for day in (1, 2, 3, 4):
        world.clock = DUE + timedelta(days=day, seconds=30)
        await run_due_guilds(deps)
    shown = world.titles(ch(G1, 1))
    assert sorted(shown) == ["item-0001", "item-0002"]


async def test_an_outage_over_48_hours_drops_the_oldest_and_never_repeats_afterwards(world):
    world.add_guild(G1, games=(BL4,))
    stream_items(world, DUE - timedelta(days=1), 24)
    deps = world.deps()
    await run_due_guilds(deps)  # day 0 posts normally; then the bot is down for five days
    # Item 1000+n is collected at DUE + 2h*(n+1): 72 of them, six days' worth.
    stream_items(world, DUE, 72, step_min=120, prefix=1000)
    world.clock = DUE + timedelta(days=5, hours=2)  # back on the 5th, 11:00 local
    [late] = await run_due_guilds(deps)
    assert late.run_date == DAY + timedelta(days=5)
    world.clock = DUE + timedelta(days=6, seconds=30)
    await run_due_guilds(deps)

    _, outage, after = world.channels[ch(G1, 1)].embeds
    # Only (end - 48h, end] made it, not the five days since the last digest.
    assert numbers(outage) == {1000 + n for n in range(36, 60)}
    assert numbers(after) == {1000 + n for n in range(60, 72)}
    everything = world.titles(ch(G1, 1))
    assert len(everything) == len(set(everything))


async def test_a_forced_rerun_covers_its_own_day_again_and_the_next_digest_chains_from_it(world):
    world.add_guild(G1, games=(BL4,))
    deps = world.deps()
    world.add_item(BL4, "item-0000", DUE - timedelta(hours=30))
    world.clock = DUE - timedelta(days=1) + timedelta(seconds=30)
    await run_due_guilds(deps)  # the 29th
    world.add_item(BL4, "item-0001", DUE - timedelta(hours=3))
    world.clock = NOW
    await run_due_guilds(deps)  # the 30th
    original = world.digest(G1)
    world.add_item(BL4, "item-0002", DUE + timedelta(minutes=20))
    world.clock = DUE + timedelta(hours=1)
    await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True)
    forced = world.digest(G1)
    world.add_item(BL4, "item-0003", DUE + timedelta(hours=2))
    world.clock = DUE + timedelta(days=1, seconds=30)
    await run_due_guilds(deps)

    assert forced.id == original.id
    assert forced.window_start == original.window_start  # its own day, again
    assert forced.window_end == DUE + timedelta(hours=1)
    assert world.digest(G1, DAY + timedelta(days=1)).window_start == forced.window_end
    shown = [shown_in(e) for e in world.channels[ch(G1, 1)].embeds]
    assert shown == [
        ["item-0000"],
        ["item-0001"],
        ["item-0002", "item-0001"],  # the repost, on purpose
        ["item-0003"],  # tomorrow shows nothing the forced run already showed
    ]


async def test_a_run_now_before_the_scheduled_time_ends_that_days_digest_without_losing_items(
    world,
):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-0001", DUE - timedelta(hours=3))
    deps = world.deps()
    world.clock = DUE - timedelta(hours=2)  # 07:00 local
    await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW)
    world.add_item(BL4, "item-0002", DUE - timedelta(hours=1))
    world.clock = DUE + timedelta(seconds=30)
    assert await run_due_guilds(deps) == []  # the day is already done
    world.clock = DUE + timedelta(days=1, seconds=30)
    await run_due_guilds(deps)
    assert world.titles(ch(G1, 1)) == ["item-0001", "item-0002"]


# --- comped guilds ---


def story(headline, label="official"):
    return StoryDraft(
        headline, "Summary text.", label, ["https://example.com/borderlands4/s"], None
    )


async def test_a_comped_ok_summary_with_no_stories_posts_nothing_and_never_shows_free_headlines(
    world,
):
    world.add_guild(G1, tier="comped", games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.summaries[BL4] = GameSummary("ok", [], [])
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok"
    assert world.channels[ch(G1, 1)].embeds == []


async def test_a_comped_ok_summary_never_mixes_in_the_headline_list(world):
    world.add_guild(G1, tier="comped", games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.summaries[BL4] = GameSummary("ok", [story("Vault patch")], [])
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    [embed] = world.channels[ch(G1, 1)].embeds
    assert "item-0001" not in embed.description and "•" not in embed.description


async def test_a_comped_fallback_or_lookup_failure_with_no_items_is_ok_and_posts_nothing(world):
    world.add_guild(G1, tier="comped", games=(BL4, PAL))
    world.summaries[BL4] = GameSummary("fallback", [], [], None)

    async def boom(game_key, due_at):
        if game_key == PAL:
            raise RuntimeError("lookup broke")
        return world.summaries[game_key]

    outcome = await run_guild_digest(world.deps(summary_for=boom), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok"
    assert all(not world.channels[ch(G1, i)].embeds for i in (1, 2))


async def test_a_comped_mixed_day_is_partial_with_stories_for_one_game_and_headlines_for_the_other(
    world,
):
    world.add_guild(G1, tier="comped", games=(BL4, PAL))
    world.add_item(BL4, "item-0001", in_window())
    world.add_item(PAL, "item-0002", in_window())
    world.summaries[BL4] = GameSummary("ok", [story("Vault patch")], [])
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)  # palworld: None
    assert outcome.status == "partial"
    assert "Vault patch" in world.channels[ch(G1, 1)].embeds[0].description
    palworld = world.channels[ch(G1, 2)].embeds[0].description
    assert palworld.startswith("Summary unavailable") and "item-0002" in palworld


async def test_a_hostile_summary_note_and_coverage_note_are_inert(world):
    world.add_guild(G1, tier="comped", games=(BL4, PAL))
    world.add_item(PAL, "item-0002", in_window())
    world.summaries[BL4] = GameSummary(
        "ok", [story("x")], ["@everyone <@123> [a](http://evil.example)"]
    )
    world.summaries[PAL] = GameSummary("fallback", [], [], "@everyone **now** <#999>")
    await run_guild_digest(
        world.deps(), G1, kind=RunKind.SCHEDULED
    )  # FakeChannel asserts no mentions
    for index in (1, 2):
        embed = world.channels[ch(G1, index)].embeds[0]
        blob = f"{embed.description}\n{embed.footer.text}"
        assert "@everyone" not in blob.replace("@\u200beveryone", "")
    # A footer is plain text in Discord (no mentions, no markdown), so only the body is held
    # to "no live syntax". The fallback note is the body's untrusted part.
    body = world.channels[ch(G1, 2)].embeds[0].description
    assert "<@123>" not in body and "<#999>" not in body and "**now**" not in body


async def test_a_comped_guild_whose_tier_is_free_again_gets_headlines_not_stale_stories(world):
    world.add_guild(G1, tier="comped", games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.summaries[BL4] = GameSummary("ok", [story("Vault patch")], [])
    with world.conn() as conn:
        repo.update_guild_settings(conn, G1, tier="free")
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    [embed] = world.channels[ch(G1, 1)].embeds
    assert "item-0001" in embed.description and "Vault patch" not in embed.description


# --- isolation ---


async def test_one_guilds_dead_channels_notify_only_that_guild_and_leave_the_others_alone(world):
    for g in (G1, G2, G3):
        world.add_guild(g, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.gone.add(ch(G2, 1))
    deps = world.deps()

    outcomes = await run_due_guilds(deps)

    assert [(o.guild_id, o.status) for o in outcomes] == [(G1, "ok"), (G2, "partial"), (G3, "ok")]
    assert {g for g, _ in world.notices} == {G2}
    assert sorted((g, c) for g, c, _ in world.reports) == [
        (G1, ch(G1, 9)),
        (G2, ch(G2, 9)),
        (G3, ch(G3, 9)),
    ]
    assert world.sends(ch(G1, 1)) == 1 and world.sends(ch(G3, 1)) == 1
    for g, _, text in world.reports:
        assert all(str(other) not in text for other in (G1, G2, G3) if other != g)
        assert all(str(ch(other, 1)) not in text for other in (G1, G2, G3) if other != g)


@pytest.mark.parametrize("boom", [RuntimeError("factory bug"), ValueError("bad"), OSError("disk")])
async def test_a_factory_exception_for_one_guild_is_reported_to_it_alone(world, boom):
    for g in (G1, G2):
        world.add_guild(g, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.factory_boom[G1] = boom
    outcomes = await run_due_guilds(world.deps())
    assert [(o.guild_id, o.status) for o in outcomes] == [(G1, "failed"), (G2, "ok")]
    assert [g for g, _ in world.notices] == [G1]
    assert world.digest(G1).status == "failed"  # never left `pending`
    assert world.sends(ch(G2, 1)) == 1


async def test_a_notifier_and_a_report_channel_that_both_raise_cost_nobody_anything(world):
    for g in (G1, G2):
        world.add_guild(g, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.gone.add(ch(G1, 1))
    world.notify_boom.add(G1)
    world.report_boom.add(G1)
    outcomes = await run_due_guilds(world.deps())
    assert [o.status for o in outcomes] == ["partial", "ok"]
    assert world.digest(G1).status == "partial"
    assert world.sends(ch(G2, 1)) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "run_due_guilds runs guilds one at a time with no per-guild timeout, so one "
        "publisher that never returns stalls every guild after it, forever."
    ),
)
async def test_a_hung_publisher_does_not_stall_the_guilds_behind_it(world):
    for g in (G1, G2):
        world.add_guild(g, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.channels[ch(G1, 1)].hang = True
    try:
        await asyncio.wait_for(run_due_guilds(world.deps()), 1.0)
    except TimeoutError:
        pass
    assert world.sends(ch(G2, 1)) == 1


async def test_a_guild_deleted_during_the_tick_is_skipped_and_the_rest_carry_on(world):
    for g in (G1, G2, G3):
        world.add_guild(g, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())

    def delete_g2():
        with world.conn() as conn:
            repo.delete_guild(conn, G2)

    world.channels[ch(G1, 1)].on_send = delete_g2  # G2 leaves while G1 is being posted
    outcomes = await run_due_guilds(world.deps())

    assert [(o.guild_id, o.status) for o in outcomes] == [(G1, "ok"), (G2, "skipped"), (G3, "ok")]
    assert world.sends(ch(G2, 1)) == 0
    with world.conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id = ?", (G2,)).fetchone()[0]
            == 0
        )


async def test_a_guild_deleted_mid_publish_does_not_crash_the_tick_or_resurrect_rows(world):
    world.add_guild(G1)
    world.add_guild(G2, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    world.add_item(PAL, "item-0002", in_window())

    def delete_g1():
        with world.conn() as conn:
            repo.delete_guild(conn, G1)

    world.channels[ch(G1, 1)].on_send = delete_g1
    outcomes = await run_due_guilds(world.deps())

    assert [o.guild_id for o in outcomes] == [G1, G2]
    assert outcomes[1].status == "ok"
    with world.conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id = ?", (G1,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM guilds WHERE guild_id = ?", (G1,)).fetchone()[0] == 0
        )


async def test_a_guild_whose_games_are_all_unfollowed_between_ticks_is_skipped_cleanly(world):
    world.add_guild(G1)
    with world.conn() as conn:
        repo.set_guild_games(conn, G1, [])
    assert await run_due_guilds(world.deps()) == []
    assert world.digest(G1) is None


async def test_reports_never_go_anywhere_but_that_guilds_admin_channel(world):
    for g in (G1, G2):
        world.add_guild(g, games=(BL4,))
    world.add_guild(G3, games=(BL4,), admin=False)
    world.add_item(BL4, "item-0001", in_window())
    await run_due_guilds(world.deps())
    assert sorted((g, c) for g, c, _ in world.reports) == [(G1, ch(G1, 9)), (G2, ch(G2, 9))]


# --- pacing and scale ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The tick paces after every guild that isn't `skipped`, including ones whose "
        "digest was a quiet day that sent nothing, so a few hundred quiet servers still "
        "cost a second apiece."
    ),
)
async def test_pacing_applies_only_after_a_guild_that_sent_something(world):
    for g in (G1, G2, G3):
        world.add_guild(g, games=(BL4,))
    # G1 has news, G2 has none (a quiet day: ok, nothing sent), G3 has news.
    with world.conn() as conn:
        repo.set_guild_games(conn, G2, [(RUST, ch(G2, 1))])
    world.add_item(BL4, "item-0001", in_window())
    outcomes = await run_due_guilds(world.deps())
    assert [o.status for o in outcomes] == ["ok", "ok", "ok"]
    assert world.sleeps == [1.0]  # only G1 -> G2; G2 sent nothing, so no pause before G3


async def test_pacing_uses_the_configured_pause_and_never_sleeps_for_a_lone_guild(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    await run_due_guilds(world.deps(pace_s=0.25))
    assert world.sleeps == []
    world.add_guild(G2, games=(BL4,))
    world.clock = DUE + timedelta(days=1, seconds=30)
    world.add_item(BL4, "item-0002", DUE + timedelta(hours=1))
    await run_due_guilds(world.deps(pace_s=0.25))
    assert world.sleeps == [0.25]


async def test_a_couple_hundred_guilds_due_at_once_all_post_once_in_order_and_fast(world):
    guilds = list(range(3000, 3200))
    for g in guilds:
        world.add_guild(g, games=(BL4,))
    # Poison a few: they must not stall or reorder the rest.
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET timezone = 'Nope/Nope' WHERE guild_id = 3007")
        conn.commit()
    world.factory_boom[3010] = RuntimeError("one bad guild")
    world.gone.add(ch(3020, 1))
    world.add_item(BL4, "item-0001", in_window())

    started = time.perf_counter()
    outcomes = await run_due_guilds(world.deps())
    elapsed = time.perf_counter() - started

    assert elapsed < 60
    assert [o.guild_id for o in outcomes] == [g for g in guilds if g != 3007]
    by_status = {o.guild_id: o.status for o in outcomes}
    assert by_status[3010] == "failed" and by_status[3020] == "partial"
    assert all(s == "ok" for g, s in by_status.items() if g not in (3010, 3020))
    assert all(world.sends(ch(g, 1)) == 1 for g in guilds if g not in (3007, 3010, 3020))
    assert {g for g, _ in world.notices} == {3010, 3020}


# --- the command-layer helpers ---


def test_guild_needs_confirmation_covers_every_status_and_the_v22_flat_id_list():
    def row(status, by_game=None, ids=None):
        return GuildDigestRow(1, 1, DAY, status, ids or [], by_game or {}, None, None, 1, None)

    assert guild_needs_confirmation(None) is False
    assert [guild_needs_confirmation(row(s)) for s in ("ok", "partial", "pending")] == [True] * 3
    assert guild_needs_confirmation(row("failed")) is False
    assert guild_needs_confirmation(row("failed", by_game={BL4: 5})) is True
    assert guild_needs_confirmation(row("failed", ids=[5])) is True
    assert guild_needs_confirmation(row("something-else")) is False


async def test_todays_digest_for_a_deleted_guild_is_none_and_preview_of_one_is_none(world):
    deps = world.deps()
    assert await todays_guild_digest(deps, 424242) is None
    assert await preview_guild_digest(deps, 424242) is None


async def test_a_preview_waits_for_a_running_digest_and_claims_nothing(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-0001", in_window())
    release = asyncio.Event()
    world.channels[ch(G1, 1)].gate = release
    deps = world.deps()
    scheduled = asyncio.create_task(run_due_guilds(deps))
    await world.channels[ch(G1, 1)].entered.wait()
    preview = asyncio.create_task(preview_guild_digest(deps, G1))
    await asyncio.sleep(0)
    assert not preview.done()
    release.set()
    await scheduled
    result = await preview
    assert result is not None
    assert world.digest(G1).attempts == 1 and world.sends(ch(G1, 1)) == 1
    assert not is_guild_busy(deps, G1)


async def test_the_third_failed_attempts_notice_still_promises_a_retry_that_is_not_coming(
    world, monkeypatch
):
    # Documents current wording: a build failure says "I'll try again shortly", including
    # after the last allowed attempt.
    world.add_guild(G1, games=(BL4,))
    deps = world.deps()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("newsbot.pipeline.guild_digest.repo.items_for_window", boom)
    for attempt in range(MAX_ATTEMPTS + 2):
        world.clock = NOW + attempt * (RETRY_AFTER + timedelta(minutes=1))
        await run_due_guilds(deps)
    assert world.digest(G1).attempts == MAX_ATTEMPTS
    assert len(world.notices) == MAX_ATTEMPTS
    assert "try again shortly" in world.notices[-1][1]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A failure before the claim (here: the window lookup) leaves no row, so the attempt "
        "cap and the ten-minute spacing never apply: the guild is retried, and told "
        "'something went wrong', on every single tick, forever."
    ),
)
async def test_a_guild_that_crashes_before_its_claim_is_not_retried_and_noticed_every_minute(
    world, monkeypatch
):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", in_window())

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("newsbot.pipeline.guild_digest.repo.last_window_end", boom)
    deps = world.deps()
    for minute in range(5):
        world.clock = NOW + timedelta(minutes=minute)
        await run_due_guilds(deps)
    assert len(world.notices) <= 1
