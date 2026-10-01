"""Adversarial tests for the item watermarks: the riskiest change in the task 16 fixes.

Digests used to cover a time range, and the time range lost items. They cover item ids now,
which can't be overtaken by a slow writer but come with their own ways to go wrong. A mark is
a number the database hands out; databases hand out numbers again once they've forgotten
the old ones (a full purge), servers change what they follow, and a rolled-back v2.2 writes
rows that have never heard of marks at all. This file asks the questions that keep me up:

1. Does every item land in exactly one digest per server, however the claims, retries,
   run-nows and slow collection passes interleave? (A seeded random timeline, thirty of them.)
2. What do purges and id restarts do to a mark?
3. What does a newly followed game see? (Pinned, not judged: see the comments.)
4. Do free and comped servers on one window cover the same ids, each its own way?
5. What happens around an abandoned digest, and a rolled-back v2.2 digest with no marks?

Real databases, the digest tests' fake channels, no network. Items are `item-N`, so the
rig can read them back out of the embeds the way a member would.
"""

from __future__ import annotations

import random
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import discord
import pytest
from test_guild_digest_adversarial import (
    BL4,
    DAY,
    DUE,
    G1,
    G2,
    NOW,
    PAL,
    World,
    _Response,
    ch,
)

from newsbot.guilds.schedule import MAX_ATTEMPTS
from newsbot.pipeline.guild_digest import GameSummary, run_due_guilds, run_guild_digest
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store import repo
from newsbot.store.db import connect


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def shown(world, guild_id=G1, game_index=1) -> list[str]:
    return world.titles(ch(guild_id, game_index))


def at_due(day: int, seconds: int = 20) -> datetime:
    return DUE + timedelta(days=day, seconds=seconds)


def a_500():
    return discord.HTTPException(_Response(500), "boom")


def times_shown(world, title, guild_id=G1, game_index=1) -> int:
    return shown(world, guild_id, game_index).count(title)


# --- 1. exactly once, in any interleaving ---


@pytest.mark.parametrize("seed", range(30))
async def test_a_random_week_of_slow_passes_failures_and_run_nows_shows_every_item_once(
    tmp_path, seed
):
    """Items arrive late (stamped before the digest, stored after), digests fail cleanly and
    retry, and an admin sometimes runs the day early. Nobody sees anything twice or never."""
    rng = random.Random(seed)  # noqa: S311 (a reproducible timeline, not a secret)
    world = World(tmp_path)
    world.add_guild(G1, games=(BL4,))
    world.add_guild(G2, games=(BL4,))
    n = 0
    days = 6
    for day in range(days):
        due = DUE + timedelta(days=day)
        # The collection passes of the day: items stamped up to ten minutes before they land.
        for _ in range(rng.randint(0, 4)):
            n += 1
            landed = due - timedelta(hours=rng.uniform(0, 23))
            stamp = landed - timedelta(minutes=rng.uniform(0, 10))
            world.add_item(BL4, f"item-{n}", stamp)
        if rng.random() < 0.4:  # an admin runs the day early (not forced: it's the day's first)
            world.clock = due - timedelta(hours=rng.randint(1, 8))
            await run_guild_digest(world.deps(), G2, kind=RunKind.RUN_NOW)
            n += 1  # a pass lands right after it, stamped from before
            world.add_item(BL4, f"item-{n}", world.clock - timedelta(seconds=30))
        if rng.random() < 0.3:  # the first try fails cleanly; the retry comes ten minutes on
            world.channels[ch(G1, 1)].always_fail = a_500()
            world.clock = due + timedelta(seconds=20)
            await run_due_guilds(world.deps())
            n += 1
            world.add_item(BL4, f"item-{n}", due - timedelta(seconds=30))  # lands in between
            world.channels[ch(G1, 1)].always_fail = None
            world.clock = due + timedelta(minutes=11)
            await run_due_guilds(world.deps())
        else:
            world.clock = due + timedelta(seconds=20)
            await run_due_guilds(world.deps())
            n += 1
            world.add_item(BL4, f"item-{n}", due - timedelta(seconds=30))  # right after the claim

    # One more tick a day later picks up the last slow pass.
    world.clock = DUE + timedelta(days=days, seconds=20)
    await run_due_guilds(world.deps())
    for guild_id in (G1, G2):
        everything = shown(world, guild_id)
        assert len(everything) == len(set(everything)), f"seed {seed}: a repeat for {guild_id}"
        assert set(everything) == {f"item-{k}" for k in range(1, n + 1)}, (
            f"seed {seed}: guild {guild_id} missed "
            f"{sorted({f'item-{k}' for k in range(1, n + 1)} - set(everything))}"
        )


async def test_a_resume_after_a_crash_between_games_reads_the_ids_its_claim_fixed(world):
    """The process dies after the first game's embed; a late pass lands; the resume must not
    pull the late item into the second game's embed (it'd show tomorrow too)."""
    world.add_guild(G1, games=(BL4, PAL))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=3))
    world.add_item(PAL, "item-2", DUE - timedelta(hours=3))
    world.clock = NOW
    world.channels[ch(G1, 2)].always_fail = a_500()  # Palworld's channel is down
    await run_due_guilds(world.deps())
    assert world.digest(G1).status == "failed"  # BL4 posted, so an admin's call, not a retry
    # The late item lands for Palworld; the next day's digest must be the only one to show it.
    world.add_item(PAL, "item-3", DUE - timedelta(seconds=30))
    world.channels[ch(G1, 2)].always_fail = None
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert times_shown(world, "item-3", game_index=2) == 1
    assert times_shown(world, "item-1", game_index=1) == 1


# --- 2. purge and id restarts ---


async def test_a_retention_purge_of_old_items_leaves_the_next_digest_exactly_right(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=5))
    world.add_item(BL4, "item-2", DUE - timedelta(hours=4))
    world.clock = at_due(0)
    await run_due_guilds(world.deps())
    world.add_item(BL4, "item-3", DUE + timedelta(hours=2))
    with world.conn() as conn:  # retention takes items 1 and 2: below the mark
        assert repo.purge_older_than(conn, DUE)[0] == 2
    world.add_item(BL4, "item-4", DUE + timedelta(hours=3))
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert [times_shown(world, f"item-{n}") for n in (1, 2, 3, 4)] == [1, 1, 1, 1]


async def test_a_purge_that_takes_items_above_the_mark_before_anyone_saw_them_is_harmless(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=5))
    world.clock = at_due(0)
    await run_due_guilds(world.deps())
    world.add_item(BL4, "item-2", DUE + timedelta(hours=1))  # id 2, above the mark
    world.add_item(BL4, "item-3", DUE + timedelta(hours=9))
    with world.conn() as conn:
        repo.purge_older_than(conn, DUE + timedelta(hours=2))  # takes item-2 only
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-1", "item-3"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "items has no AUTOINCREMENT, so a purge that empties it makes SQLite number from 1 again. "
        "`last_coverage` only distrusts a mark that is higher than the newest id, so a burst of "
        "more new items than the old mark is hidden behind it: ids 1..mark are new items the "
        "digest believes it already covered. Needs an empty store (collection stalled for the "
        "whole retention period), which is rare, and silent when it happens."
    ),
)
async def test_ids_that_restart_after_a_full_purge_do_not_hide_a_burst_bigger_than_the_old_mark(
    world,
):
    world.add_guild(G1, games=(BL4,))
    for n in range(1, 4):
        world.add_item(BL4, f"item-{n}", DUE - timedelta(hours=10 - n))
    world.clock = at_due(0)
    await run_due_guilds(world.deps())  # covers ids 1..3
    with world.conn() as conn:
        repo.purge_older_than(conn, DUE + timedelta(days=30))  # retention empties the table
    for n in range(4, 9):  # five new items get ids 1..5
        world.add_item(BL4, f"item-{n}", DUE + timedelta(days=1) - timedelta(hours=10 - n))
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert sorted(set(shown(world)) - {"item-1", "item-2", "item-3"}) == [
        f"item-{n}" for n in range(4, 9)
    ]


async def test_ids_that_restart_after_a_full_purge_still_show_a_burst_smaller_than_the_old_mark(
    world,
):
    """The half of the restart case the existing derivation does handle, pinned so it stays."""
    world.add_guild(G1, games=(BL4,))
    for n in range(1, 6):
        world.add_item(BL4, f"item-{n}", DUE - timedelta(hours=10 - n))
    world.clock = at_due(0)
    await run_due_guilds(world.deps())  # covers ids 1..5
    with world.conn() as conn:
        repo.purge_older_than(conn, DUE + timedelta(days=30))
    world.add_item(BL4, "item-6", DUE + timedelta(days=1) - timedelta(hours=3))  # id 1 again
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert times_shown(world, "item-6") == 1


# --- 3. a game that wasn't followed yesterday ---


async def test_a_newly_followed_game_starts_at_the_servers_mark_not_at_48_hours_back(world):
    """Pinned by design (the owner may want it otherwise): following Palworld on day 2 shows
    Palworld items stored after day 1's digest, not the 26 hours of Palworld news before it."""
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=3))
    world.add_item(PAL, "item-2", DUE - timedelta(hours=2))  # stored while unfollowed
    world.clock = at_due(0)
    await run_due_guilds(world.deps())
    with world.conn() as conn:
        repo.follow_game(conn, G1, PAL, ch(G1, 2))
    world.add_item(PAL, "item-3", DUE + timedelta(hours=6))
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert shown(world, game_index=2) == ["item-3"]  # item-2 is 26 hours old and never appears


async def test_a_game_unfollowed_and_refollowed_does_not_replay_what_it_missed_either(world):
    world.add_guild(G1, games=(BL4, PAL))
    world.add_item(PAL, "item-1", DUE - timedelta(hours=3))
    world.clock = at_due(0)
    await run_due_guilds(world.deps())
    with world.conn() as conn:
        repo.set_guild_games(conn, G1, [(BL4, ch(G1, 1))])  # drops Palworld
    world.add_item(PAL, "item-2", DUE + timedelta(hours=3))  # news while unfollowed
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    with world.conn() as conn:
        repo.set_guild_games(conn, G1, [(BL4, ch(G1, 1)), (PAL, ch(G1, 2))])
    world.add_item(PAL, "item-3", at_due(1) + timedelta(hours=2))
    world.clock = at_due(2)
    await run_due_guilds(world.deps())
    assert shown(world, game_index=2) == ["item-1", "item-3"]  # item-2 fell in the gap


async def test_a_server_that_follows_a_game_for_the_first_time_gets_the_floor_not_everything(
    world,
):
    world.add_guild(G1, games=(BL4,))
    world.add_item(PAL, "item-1", DUE - timedelta(hours=30))  # older than a first digest's day
    world.add_item(PAL, "item-2", DUE - timedelta(hours=20))
    with world.conn() as conn:
        repo.set_guild_games(conn, G1, [(PAL, ch(G1, 1))])
    world.clock = at_due(0)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-2"]


# --- 4. free and comped on one window ---


def story(headline: str) -> StoryDraft:
    return StoryDraft(
        headline=headline, summary="s", label="official", item_urls=[], update_of_story_id=None
    )


async def test_free_and_comped_servers_on_one_window_each_resume_from_their_own_marks(world):
    world.add_guild(G1, tier="free", games=(BL4,))
    world.add_guild(G2, tier="comped", games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=4))
    world.add_item(BL4, "item-2", DUE - timedelta(hours=3))
    asked: list[object] = []

    async def lookup(game_key, due_at, after=None):
        asked.append(after)
        # The shared summary was made before the third item landed.
        return GameSummary("ok", [story("the summary")], [], None, 2)

    # item-3 arrives after the summary was made but before the digest claimed its ids.
    world.add_item(BL4, "item-3", DUE - timedelta(minutes=5))
    world.clock = at_due(0)
    await run_due_guilds(world.deps(summary_for=lookup))

    assert shown(world, G1) == ["item-3", "item-2", "item-1"]  # free: all three, as headlines
    with world.conn() as conn:
        free = repo.last_coverage(conn, G1, exclude_run_date=DAY + timedelta(days=1))
        comped = repo.last_coverage(conn, G2, exclude_run_date=DAY + timedelta(days=1))
    assert free.item_id == 3 and free.for_game(BL4).item_id == 3
    assert comped.item_id == 3 and comped.for_game(BL4).item_id == 2  # item-3 is for tomorrow

    world.add_item(BL4, "item-4", DUE + timedelta(hours=5))
    world.clock = at_due(1)
    await run_due_guilds(world.deps(summary_for=lookup))
    assert asked[-1].item_id == 2  # the comped server's summary has to start at its own mark
    assert times_shown(world, "item-3", G1) == 1 and times_shown(world, "item-4", G1) == 1


async def test_a_fallback_game_in_a_comped_digest_chains_from_the_digests_mark_not_a_summarys(
    world,
):
    world.add_guild(G2, tier="comped", games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=3))
    world.add_item(BL4, "item-2", DUE - timedelta(hours=2))

    async def failed(game_key, due_at, after=None):
        return GameSummary(
            "fallback", [], [], "the model had a nap", 1
        )  # claims 1, shows headlines

    world.clock = at_due(0)
    await run_due_guilds(world.deps(summary_for=failed))
    assert shown(world, G2) == ["item-2", "item-1"]
    with world.conn() as conn:
        coverage = repo.last_coverage(conn, G2, exclude_run_date=DAY + timedelta(days=1))
    assert coverage.for_game(BL4).item_id == 2  # not the fallback's 1: item-2 was shown


# --- 5. DST, the abandoned row, and rolled-back v2.2 ---


async def test_across_the_spring_forward_day_every_item_lands_in_exactly_one_digest(world):
    world.add_guild(G1, games=(BL4,))
    dues = [
        datetime(2027, 3, 12, 17, 0, tzinfo=UTC),  # 09:00 PST
        datetime(2027, 3, 13, 17, 0, tzinfo=UTC),
        datetime(2027, 3, 14, 16, 0, tzinfo=UTC),  # the 23-hour day: 09:00 PDT is an hour earlier
        datetime(2027, 3, 15, 16, 0, tzinfo=UTC),
        datetime(2027, 3, 16, 16, 0, tzinfo=UTC),
    ]
    n = 0
    at = dues[0] - timedelta(hours=20)
    for due in dues:
        while at <= due and due < dues[-1]:
            n += 1
            world.add_item(BL4, f"item-{n}", at)
            at += timedelta(minutes=50)
        world.clock = due + timedelta(seconds=20)
        await run_due_guilds(world.deps())
        n += 1
        world.add_item(BL4, f"item-{n}", due - timedelta(seconds=30))
    everything = shown(world)
    assert len(everything) == len(set(everything))
    assert set(everything) == {f"item-{k}" for k in range(1, n)}


def exhaust_the_attempts(world, day_offset: int = 1):
    """Day `day_offset` is a `pending` row that has used all its attempts, with a stale lease.

    Day 0 is an ordinary digest first, so the abandoned day has a previous digest to chain on
    (a server's very first digest has none, which is a different question).
    """
    due = DUE + timedelta(days=day_offset)
    day = DAY + timedelta(days=day_offset)
    with world.conn() as conn:
        claim = repo.claim_guild_digest(
            conn, G1, day, force=False, window=(due - timedelta(hours=24), due), now=lambda: due
        )
        conn.execute(
            "UPDATE digests SET attempts = ?, updated_at = ? WHERE id = ?",
            (MAX_ATTEMPTS, due.isoformat(), claim.digest_id),
        )
        conn.commit()
    return due


async def a_normal_first_day(world, *items: tuple[str, str, timedelta]):
    world.add_guild(G1, games=(BL4, PAL))
    for game, title, before in items:
        world.add_item(game, title, DUE - before)
    world.clock = at_due(0)
    await run_due_guilds(world.deps())


async def test_an_abandoned_digest_that_posted_nothing_hands_its_items_to_the_next_day(world):
    await a_normal_first_day(world, (BL4, "item-1", timedelta(hours=3)))
    world.add_item(BL4, "item-2", DUE + timedelta(hours=5))  # the abandoned day's news
    due = exhaust_the_attempts(world)
    world.clock = due + timedelta(minutes=30)  # the lease went stale long ago
    await run_due_guilds(world.deps())
    assert world.digest(G1, DAY + timedelta(days=1)).status == "failed"
    assert shown(world) == ["item-1"]
    assert [t for g, t in world.notices if g == G1 and "interrupted" in t] != []

    world.add_item(BL4, "item-3", due + timedelta(hours=3))
    world.clock = at_due(2)
    await run_due_guilds(world.deps())
    assert sorted(shown(world)) == ["item-1", "item-2", "item-3"]  # item-2 survived the abandon


async def test_the_abandon_notice_goes_out_once_and_tomorrows_digest_is_unaffected(world):
    await a_normal_first_day(world, (BL4, "item-1", timedelta(hours=3)))
    due = exhaust_the_attempts(world)
    for minutes in (30, 31, 45):
        world.clock = due + timedelta(minutes=minutes)
        await run_due_guilds(world.deps())
    assert len([t for g, t in world.notices if g == G1 and "interrupted" in t]) == 1
    world.clock = at_due(2)
    outcomes = await run_due_guilds(world.deps())
    assert [o.status for o in outcomes if o.guild_id == G1] == ["ok"]
    assert world.digest(G1, DAY + timedelta(days=2)).attempts == 1


async def test_an_abandoned_digest_that_posted_one_game_still_counts_as_covering_them_all(world):
    """Pinned: a failed row with anything posted is the server's last coverage, so the games it
    never got to are not retried by the next day's digest. (Retrying them is run-now's job.)"""
    await a_normal_first_day(world)
    world.add_item(PAL, "item-1", DUE + timedelta(hours=3))
    due = exhaust_the_attempts(world)
    with world.conn() as conn:
        digest_id = world.digest(G1, DAY + timedelta(days=1)).id
        repo.record_posted_game(conn, digest_id, BL4, 4242, now=lambda: due)
    world.clock = due + timedelta(minutes=30)
    await run_due_guilds(world.deps())
    world.clock = at_due(2)
    await run_due_guilds(world.deps())
    assert shown(world, game_index=2) == []  # Palworld's item-1 never showed up anywhere


async def test_a_servers_very_first_digest_abandoned_leaves_only_a_day_of_history_for_the_next(
    world,
):
    """Pinned: with no earlier digest to chain on, the next one is a first digest again, and its
    floor is a day, so the abandoned day's older items are gone."""
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=3))
    due = exhaust_the_attempts(world, day_offset=0)
    world.clock = due + timedelta(minutes=30)
    await run_due_guilds(world.deps())
    world.add_item(BL4, "item-2", DUE + timedelta(hours=3))
    world.clock = at_due(1)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-2"]


def v22_row(world, *, posted_at: datetime, run_date: date, status="ok"):
    """What a rolled-back v2.2 writes: no server, no window, no marks, only a finish time."""
    with closing(connect(world.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO digests (run_date, status, posted_message_ids, created_at, updated_at) "
            "VALUES (?, ?, '[131]', ?, ?)",
            (run_date.isoformat(), status, posted_at.isoformat(), posted_at.isoformat()),
        )


async def test_a_v22_digest_written_after_the_watermark_migration_seeds_the_next_v3_digest(
    world,
):
    """The rollback window: v3 digested day 1, v2.2 ran day 2 (no marks on its row), v3 is back."""
    world.add_guild(G1, games=(BL4,))
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (NOW.isoformat(), G1))
        conn.commit()
    world.add_item(BL4, "item-1", DUE - timedelta(hours=3))
    world.clock = at_due(0)
    await run_due_guilds(world.deps())

    v22_at = DUE + timedelta(days=1, seconds=7)
    world.add_item(BL4, "item-2", v22_at - timedelta(hours=5))  # v2.2 posted this one
    v22_row(world, posted_at=v22_at, run_date=DAY + timedelta(days=1))
    world.add_item(BL4, "item-3", v22_at + timedelta(hours=4))  # v3's, after the rollback ended
    with world.conn() as conn:
        assert repo.adopt_orphan_digests(conn, G1) == 1  # what startup does on the way back up

    world.clock = at_due(2)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-1", "item-3"]  # v2.2's item-2 is not shown a second time
    with world.conn() as conn:
        row = conn.execute(
            "SELECT items_after, items_upto FROM digests WHERE run_date = ?",
            ((DAY + timedelta(days=2)).isoformat(),),
        ).fetchone()
    assert tuple(row) == (2, 3)


async def test_two_v22_days_in_a_row_chain_on_the_later_one(world):
    world.add_guild(G1, games=(BL4,))
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (NOW.isoformat(), G1))
        conn.commit()
    first = DUE + timedelta(seconds=7)
    second = first + timedelta(days=1)
    world.add_item(BL4, "item-1", first - timedelta(hours=1))
    v22_row(world, posted_at=first, run_date=DAY)
    world.add_item(BL4, "item-2", second - timedelta(hours=1))
    v22_row(world, posted_at=second, run_date=DAY + timedelta(days=1))
    world.add_item(BL4, "item-3", second + timedelta(hours=1))
    with world.conn() as conn:
        assert repo.adopt_orphan_digests(conn, G1) == 2
    world.clock = at_due(2)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-3"]
