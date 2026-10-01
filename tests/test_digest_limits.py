"""Bounds on retrying, waiting and remembering (qa review, fixes 3, 4, 6 and 7).

Four ways a digest could keep going when it shouldn't: a row that times out forever, a summary
lookup that hangs inside a digest, an old unfinished row that gets resurrected days later, and
a claim that keeps getting lost. And one way to keep something it shouldn't: a server's
bookkeeping in `app_state` after the server left.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from test_guild_digest_adversarial import BL4, DAY, DUE, G1, G2, NOW, PAL, World, ch
from test_summaries import BERLIN_DUE
from test_summaries import World as SummaryWorld

from newsbot.guilds.schedule import MAX_ATTEMPTS, due_guilds
from newsbot.pipeline import summaries
from newsbot.pipeline.guild_digest import GameSummary, run_due_guilds, run_guild_digest
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import ensure_summary
from newsbot.pipeline.summarize import _FALLBACK_NOTE, StoryDraft
from newsbot.store import repo
from newsbot.store.models import DueCandidate


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- 3. timeouts and resumes count against the attempt cap ---


async def test_a_digest_that_times_out_every_time_is_abandoned_once_with_one_notice(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    world.channels[ch(G1, 1)].hang = True  # Discord never answers
    deps = world.deps(guild_timeout_s=0.1)
    world.clock = DUE + timedelta(seconds=20)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await run_due_guilds(deps)  # the tick gives up on it; the row stays pending
        row = world.digest(G1)
        assert (row.status, row.attempts) == ("pending", attempt)
        world.clock += timedelta(minutes=11)  # the lease goes stale; the next tick resumes it
    assert world.notices == []

    # Out of attempts: the next tick marks it failed and tells the server, once.
    outcomes = await run_due_guilds(deps)
    assert [o.status for o in outcomes] == ["failed"]
    row = world.digest(G1)
    assert row.status == "failed" and "gave up after 3 attempts" in row.error_notes
    [(guild_id, text)] = world.notices
    assert guild_id == G1 and "run-now" in text and "@" not in text
    # And that's the end of it: no more resumes, no more notices.
    for _ in range(3):
        world.clock += timedelta(minutes=11)
        assert await run_due_guilds(deps) == []
    assert len(world.notices) == 1 and world.channels[ch(G1, 1)].attempts == MAX_ATTEMPTS


async def test_what_posted_before_the_cutoff_stays_recorded_and_the_next_day_starts_fresh(world):
    world.add_guild(G1)
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    world.add_item(PAL, "item-2", DUE - timedelta(hours=2))
    world.channels[ch(G1, 2)].hang = True  # palworld never posts; borderlands does
    deps = world.deps(guild_timeout_s=0.1)
    world.clock = DUE + timedelta(seconds=20)
    for _ in range(MAX_ATTEMPTS):
        await run_due_guilds(deps)
        world.clock += timedelta(minutes=11)
    await run_due_guilds(deps)
    row = world.digest(G1)
    assert row.status == "failed" and list(row.posted_by_game) == [BL4]

    world.channels[ch(G1, 2)].hang = False
    world.add_item(PAL, "item-3", DUE + timedelta(hours=3))
    world.clock = DUE + timedelta(days=1, seconds=20)
    await run_due_guilds(deps)
    assert world.digest(G1, DAY + timedelta(days=1)).status == "ok"
    # It chains from the abandoned digest (BL4 went out; its window counts as delivered).
    assert world.titles(ch(G1, 2)) == ["item-3"]


def test_the_claim_refuses_a_stale_pending_row_that_used_its_attempts(world):
    world.add_guild(G1, games=(BL4,))
    window = (DUE - timedelta(hours=24), DUE)
    with world.conn() as conn:
        for n in range(MAX_ATTEMPTS):
            repo.claim_guild_digest(
                conn,
                G1,
                DAY,
                force=False,
                window=window,
                resume=True,
                now=lambda n=n: NOW + timedelta(minutes=11 * n),
            )
        later = lambda: NOW + timedelta(hours=2)  # noqa: E731
        assert (
            repo.claim_guild_digest(
                conn, G1, DAY, force=False, window=window, resume=True, now=later
            )
            is None
        )
        # An admin's run-now (force) is still allowed to start over.
        assert repo.claim_guild_digest(conn, G1, DAY, force=True, window=window, now=later)


def test_abandoning_only_touches_a_stale_pending_row_that_is_out_of_attempts(world):
    world.add_guild(G1, games=(BL4,))
    window = (DUE - timedelta(hours=24), DUE)
    with world.conn() as conn:
        repo.claim_guild_digest(conn, G1, DAY, force=False, window=window, now=lambda: NOW)

        def abandon(at):
            return repo.abandon_guild_digest(conn, G1, DAY, max_attempts=1, now=lambda: NOW + at)

        assert abandon(timedelta(minutes=5)) is False  # the lease is still fresh
        assert (
            repo.abandon_guild_digest(
                conn, G1, DAY, max_attempts=2, now=lambda: NOW + timedelta(hours=1)
            )
            is False
        )  # attempts left
        assert (
            repo.abandon_guild_digest(
                conn,
                G1,
                DAY + timedelta(days=1),
                max_attempts=1,
                now=lambda: NOW + timedelta(hours=1),
            )
            is False
        )  # no such row
        assert abandon(timedelta(hours=1)) is True
        assert abandon(timedelta(hours=2)) is False  # already failed: nothing to do twice


# --- 3. the inline summary has its own, shorter deadline ---


def comped_world(world, **over):
    world.add_guild(G1, tier="comped", games=(BL4, PAL))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    world.add_item(PAL, "item-2", DUE - timedelta(hours=2))
    world.clock = DUE + timedelta(seconds=20)
    return world.deps(
        guild_timeout_s=30,
        summary_timeout_s=0.05,
        summary_budget_s=0.08,
        summary_min_s=0.01,
        **over,
    )


async def test_a_summary_that_never_comes_costs_that_game_its_summary_and_not_the_digest(world):
    async def hangs(game_key, due_at, after=None):
        await asyncio.Event().wait()

    deps = comped_world(world, summary_for=hangs)
    started = asyncio.get_running_loop().time()
    outcome = await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    assert asyncio.get_running_loop().time() - started < 2
    # Not cancelled, not failed: both games posted, as headlines, with the usual footer.
    assert outcome.status == "partial" and set(outcome.posted_by_game) == {BL4, PAL}
    assert world.digest(G1).status == "partial"
    for index, title in ((1, "item-1"), (2, "item-2")):
        [embed] = world.channels[ch(G1, index)].embeds
        assert title in embed.description and _FALLBACK_NOTE in embed.description


async def test_a_game_with_a_stored_summary_still_gets_it_when_another_game_ran_out_the_clock(
    world,
):
    story = StoryDraft(
        "A real story", "Words.", "official", ["https://example.com/palworld/x"], None
    )

    async def lookup(game_key, due_at, after=None):
        if game_key == BL4:
            await asyncio.Event().wait()  # the first game hangs through the whole budget
        return GameSummary("ok", [story], [])

    deps = comped_world(world, summary_for=lookup)
    outcome = await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "partial"
    [embed_pal] = world.channels[ch(G1, 2)].embeds
    assert "A real story" in str(embed_pal.to_dict())  # palworld read its stored summary
    [embed_bl4] = world.channels[ch(G1, 1)].embeds
    assert "item-1" in embed_bl4.description  # borderlands fell back to headlines


def test_the_inline_deadline_sits_well_under_the_guild_timeout():
    from newsbot.pipeline import guild_digest

    assert guild_digest.SUMMARY_BUDGET_S < guild_digest.GUILD_TIMEOUT_S / 1.5
    assert guild_digest.SUMMARY_TIMEOUT_S <= guild_digest.SUMMARY_BUDGET_S


# --- 4. an old unfinished row is left alone ---


async def test_a_clean_failure_from_days_ago_is_not_retried_and_the_next_digest_starts_fresh(world):
    world.add_guild(G1, games=(BL4,))
    old_day = DAY - timedelta(days=3)
    old_window = (DUE - timedelta(days=4), DUE - timedelta(days=3))
    with world.conn() as conn:
        repo.claim_guild_digest(
            conn, G1, old_day, force=False, window=old_window, now=lambda: NOW - timedelta(days=3)
        )
        repo.mark_guild_digest_failed(conn, 1, "boom", now=lambda: NOW - timedelta(days=3))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=5))

    world.clock = DUE + timedelta(seconds=20)
    outcomes = await run_due_guilds(world.deps())
    assert [(o.run_date, o.status) for o in outcomes] == [(DAY, "ok")]  # today's, not the old one
    assert world.titles(ch(G1, 1)) == ["item-1"]
    assert world.digest(G1, old_day).status == "failed"  # left exactly as it was
    assert world.digest(G1, old_day).attempts == 1


async def test_a_pending_row_from_days_ago_is_not_resumed_either(world):
    world.add_guild(G1, games=(BL4,))
    old_day = DAY - timedelta(days=2)
    with world.conn() as conn:
        repo.claim_guild_digest(
            conn,
            G1,
            old_day,
            force=False,
            window=(DUE - timedelta(days=3), DUE - timedelta(days=2)),
            now=lambda: NOW - timedelta(days=2),
        )
    world.add_item(BL4, "item-1", DUE - timedelta(hours=5))
    world.clock = DUE + timedelta(seconds=20)
    outcomes = await run_due_guilds(world.deps())
    assert [(o.run_date, o.status) for o in outcomes] == [(DAY, "ok")]
    assert world.digest(G1, old_day).status == "pending"


def row(**kw) -> DueCandidate:
    base = dict(guild_id=1, digest_time="09:00", timezone="America/Los_Angeles", tier="free")
    return DueCandidate(**{**base, **kw})


def test_only_yesterday_or_todays_unfinished_row_is_worth_finishing():
    now = utc(2026, 9, 30, 20, 0)  # 13:00 PDT on the 30th
    for run_date, expected in (
        (date(2026, 9, 30), ["resume"]),
        (date(2026, 9, 29), ["resume"]),  # yesterday: still worth finishing
        (date(2026, 9, 28), ["first"]),  # two days back: left alone, and today starts fresh
    ):
        c = row(
            run_date=run_date,
            status="pending",
            attempts=1,
            window_end=utc(2026, 9, 28, 16, 0),
            updated_at=utc(2026, 9, 28, 16, 0),
        )
        assert [d.reason for d in due_guilds([c], now)] == expected, run_date


def test_a_pending_row_out_of_attempts_is_abandoned_not_resumed():
    now = utc(2026, 9, 30, 20, 0)
    pending = dict(
        run_date=date(2026, 9, 30),
        status="pending",
        window_end=utc(2026, 9, 30, 16, 0),
        updated_at=utc(2026, 9, 30, 16, 1),
    )
    assert [d.reason for d in due_guilds([row(attempts=MAX_ATTEMPTS - 1, **pending)], now)] == [
        "resume"
    ]
    assert [d.reason for d in due_guilds([row(attempts=MAX_ATTEMPTS, **pending)], now)] == [
        "abandon"
    ]
    # A failed row that's out of attempts is simply finished (the existing rule).
    failed = {**pending, "status": "failed"}
    assert due_guilds([row(attempts=MAX_ATTEMPTS, **failed)], now) == []


# --- 6. the ensure_summary loop ---


async def test_a_claim_that_keeps_getting_taken_over_stops_after_a_few_laps(tmp_path, monkeypatch):
    world = SummaryWorld(tmp_path, monkeypatch)
    world.guild(1, "comped", "Europe/Berlin", ["palworld"])
    world.item("palworld", "a thing", BERLIN_DUE - timedelta(hours=2))
    laps = 0

    async def lost(deps, game, run_date, coverage, search, row, token, after):
        nonlocal laps
        laps += 1
        with world.conn() as conn:
            repo.release_game_summary_claim(conn, game.key, token)
        return None  # the claim was taken over while we worked

    monkeypatch.setattr(summaries, "_make", lost)
    world.clock.t = BERLIN_DUE
    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))
    assert got is None and laps == summaries._MAX_LOST_CLAIMS
    assert world.llm.calls == []  # and nobody paid for the laps


# --- 7. per-server app_state keys go with the server ---


def test_removing_a_server_clears_its_own_bookkeeping_and_nobody_elses(world):
    world.add_guild(G1)
    world.add_guild(G2)
    keys = {
        f"guild_digest_crash:{G1}": "mine",
        f"stuck_v22_digest:{G1}:2026-09-30": "mine",
        f"stuck_v22_digest:{G1}:2026-10-01": "mine",
        f"guild_digest_crash:{G2}": "theirs",
        f"stuck_v22_digest:{G2}:2026-09-30": "theirs",
        # An id that merely starts with this server's digits is somebody else's.
        f"guild_digest_crash:{G1}0": "theirs",
        f"stuck_v22_digest:{G1}0:2026-09-30": "theirs",
        "import": "{}",
        "commands:global": "hash",
        "summary_claim:palworld": "claim",
    }
    with world.conn() as conn:
        for key, value in keys.items():
            repo.app_state_set(conn, key, value)
        assert repo.delete_guild(conn, G1) is True
        left = {k: repo.app_state_get(conn, k) for k in keys}
    assert {k for k, v in left.items() if v is None} == {
        f"guild_digest_crash:{G1}",
        f"stuck_v22_digest:{G1}:2026-09-30",
        f"stuck_v22_digest:{G1}:2026-10-01",
    }
    assert all(v == keys[k] for k, v in left.items() if v is not None)


def test_the_keys_the_code_writes_are_the_keys_removal_clears(world):
    from newsbot.bot import client
    from newsbot.pipeline import guild_digest

    assert guild_digest._crash_key(G1) == repo.guild_crash_key(G1)
    assert client._STUCK_V22_KEY == repo.STUCK_V22_KEY_PREFIX
