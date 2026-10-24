"""Adversarial round for fix batch 2: where the industry pick is made, which links it keeps.

The builder's tests pin the four rules once each. These go after the seams:

1. A free build never makes or waits on the pick. A spy model counts every call across every
   path (scheduled, catch-up, preview, run-now), a live claim held by somebody else must not
   be polled, and a tick of twenty free servers against a hung API must finish at once and
   leave the model untouched. The prepare job takes the industry entry first even when nine
   slow comped games sit ahead of it in the catalog, and a server with no stored pick shows
   the newest five headlines without losing its place in the chain.
2. A stored pick keeps the model's link order. A merged story whose first URL has the higher
   item id keeps it first through storage, for the free server's headline and for a comped
   server's link line. Game stories keep item-id order, checked against the base commit's
   `repo.py` (read out of git, like `test_terminal_task1_adversarial.py` does) and not against
   my memory of it.
3. The skip rule for a finished digest never hides a summary a not-yet-posted server needs:
   across a DST change, a late catch-up, two servers sharing a due time where one posted.
4. The owner hears about an industry fallback once a day, not once a chain, and a game's
   fallback alerts exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
import types
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_guild_digest import DUE, G1, G2, NOW, FakeChannel
from test_guild_digest_industry import BL4, CHANNELS, PAL, SCHED, draft, titles
from test_guild_digest_industry_adversarial import DAY, AdvWorld, PickLLM
from test_summaries import BER, BERLIN, BERLIN_DUE, FREE, PAC, FakeLLM, ago
from test_summaries import World as SummariesWorld

from newsbot.config import load_config
from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline.guild_digest import (
    GameSummary,
    GuildDigestDeps,
    preview_guild_digest,
    run_due_guilds,
    run_guild_digest,
    summary_from_row,
)
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import (
    SummaryDeps,
    ensure_summary,
    prepare_summaries,
    summary_lookup,
)
from newsbot.pipeline.summarize import LLMError, LLMResult
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import Coverage, StoryToSave, Usage

REPO = Path(__file__).resolve().parent.parent
BASE_COMMIT = "f694f37"
FIXTURES = REPO / "tests" / "fixtures"
HOUR = timedelta(hours=1)


# --- shared rigs ---


@pytest.fixture
def world(tmp_path):
    return AdvWorld(tmp_path)


@pytest.fixture
def sworld(tmp_path, monkeypatch):
    """The summaries rig on the industry catalog."""
    return SummariesWorld(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")


class HangingLLM(PickLLM):
    """A model that never answers. Counts the calls it was asked for."""

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        self.calls.append(user)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


def spy_deps(world, llm=None, *, sleeps=None, **overrides):
    """Real lookups wired to `llm` (default: the world's counting model), with a sleep spy."""
    if llm is not None:
        world.llm = llm

    async def alert(text: str) -> None:
        return None

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        await asyncio.sleep(0)

    sd = SummaryDeps(
        cfg=world.cfg,
        db_path=world.db_path,
        llm=world.llm,
        now=lambda: world.clock,
        alert=alert,
        sleep=sleep,
    )
    return world.deps(summary_for=summary_lookup(sd), **overrides)


def summary_rows(world, game_key=INDUSTRY_KEY) -> int:
    with world.conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM game_summaries WHERE game_key = ?", (game_key,)
        ).fetchone()[0]


def many_free_servers(world, count, *, first_id=2000):
    """`count` free servers that follow industry only, each with a channel of its own."""
    ids = []
    for n in range(count):
        guild_id, channel_id = first_id + n, 5000 + n
        world.channels[channel_id] = FakeChannel()
        with world.conn() as conn:
            repo.create_guild(
                conn,
                guild_id,
                digest_time="09:00",
                timezone="America/Los_Angeles",
                admin_channel_id=None,
                tier="free",
                set_up=True,
                now=lambda: NOW,
            )
            repo.set_guild_games(conn, guild_id, [(INDUSTRY_KEY, channel_id)])
        ids.append((guild_id, channel_id))
    return ids


# --- 1. a free build never makes or waits on the pick ---


@pytest.mark.parametrize("path", ["scheduled", "catch_up", "run_now", "forced_run_now", "preview"])
async def test_no_free_build_on_any_path_calls_the_model_or_claims_the_game(world, path):
    for n in range(7):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1)
    deps = spy_deps(world)
    if path == "preview":
        preview = await preview_guild_digest(deps, G1)
        [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
        shown = titles(str(message.embed.description).split("\n"))
    else:
        kind = {
            "scheduled": SCHED,
            "catch_up": RunKind.CATCH_UP,
            "run_now": RunKind.RUN_NOW,
            "forced_run_now": RunKind.RUN_NOW,
        }[path]
        outcome = await run_guild_digest(deps, G1, kind=kind, force=path == "forced_run_now")
        assert outcome.status == "ok"
        shown = world.industry_titles()
    assert world.llm.calls == []
    assert summary_rows(world) == 0
    with world.conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM app_state WHERE key LIKE '%summary%'").fetchone()[0]
            == 0
        )
    assert shown == [f"Story {n}" for n in range(5)]


async def test_a_free_build_does_not_poll_a_claim_somebody_else_holds(world):
    # The old inline path polled a busy claim every 2 s for up to 10 minutes of the tick.
    for n in range(3):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1)
    with world.conn() as conn:
        state, _ = repo.claim_game_summary(
            conn,
            INDUSTRY_KEY,
            DUE,
            token="someone-else",  # noqa: S106
            now=lambda: NOW,
        )
    assert state == "claimed"
    sleeps: list[float] = []
    await run_guild_digest(spy_deps(world, sleeps=sleeps), G1, kind=SCHED)
    assert sleeps == [] and world.llm.calls == []
    assert world.industry_titles() == ["Story 0", "Story 1", "Story 2"]


async def test_a_free_build_ignores_a_stored_pick_that_only_a_model_call_could_replace(world):
    # A stored fallback row is "reusable" for a comped inline lookup but a retry would call
    # the model. A free build must take the row as it stands.
    for n in range(3):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1)
    world.store_pick([], status="fallback")
    await run_guild_digest(spy_deps(world), G1, kind=RunKind.RUN_NOW, force=True)
    assert world.llm.calls == []
    assert summary_rows(world) == 1


@pytest.mark.parametrize("late", [timedelta(seconds=30), timedelta(hours=3)])
async def test_a_tick_of_twenty_free_servers_against_a_hung_api_never_touches_the_model(
    world, late
):
    servers = many_free_servers(world, 20)
    for n in range(6):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.clock = DUE + late
    deps = spy_deps(
        world,
        HangingLLM(),
        summary_timeout_s=0.5,
        summary_budget_s=0.5,
        summary_min_s=0.5,
        pace_s=0.0,
    )
    started = time.monotonic()
    outcomes = await asyncio.wait_for(run_due_guilds(deps), timeout=20)
    elapsed = time.monotonic() - started
    assert world.llm.calls == []
    assert len(outcomes) == 20 and {o.status for o in outcomes} == {"ok"}
    for _, channel_id in servers:
        [embed] = world.channels[channel_id].embeds
        assert titles(str(embed.description).split("\n")) == [f"Story {n}" for n in range(5)]
    # Half a second each would be 10 s. The honest cost is database reads.
    assert elapsed < 6


async def test_the_tick_time_does_not_grow_with_the_number_of_free_servers(world):
    # K=2 and K=20 against the same hung API: neither waits on the model, so the larger tick
    # is not an order of magnitude slower. (A regression costs the 0.4 s deadline per server.)
    world.news("Story", hours_before_due=1)
    timings = {}
    first_id = 3000
    for count in (2, 20):
        many_free_servers(world, count, first_id=first_id)
        first_id += 100
        world.clock = NOW
        deps = spy_deps(
            world,
            HangingLLM(),
            summary_timeout_s=0.4,
            summary_budget_s=0.4,
            summary_min_s=0.4,
            pace_s=0.0,
        )
        started = time.monotonic()
        await asyncio.wait_for(run_due_guilds(deps), timeout=20)
        timings[count] = time.monotonic() - started
        assert world.llm.calls == []
        with world.conn() as conn:
            conn.execute("DELETE FROM digests")
            conn.commit()
            conn.execute("DELETE FROM guilds")
            conn.commit()
    assert timings[20] < 0.4 * 10


async def test_a_comped_server_behind_free_ones_is_not_held_up_by_them(world):
    # The free servers sort ahead of the comped one (same due instant, lower ids). The only
    # model lookups in the tick are the comped server's own: at most one per followed entry.
    many_free_servers(world, 12, first_id=100)
    world.follow(G1, "comped")
    for key in (BL4, PAL):
        world.news(f"{key} patch", game=key)
    world.news("Studio closes")
    world.clock = NOW
    deps = spy_deps(
        world,
        HangingLLM(),
        summary_timeout_s=0.3,
        summary_budget_s=1.0,
        summary_min_s=0.1,
        pace_s=0.0,
    )
    started = time.monotonic()
    outcomes = await asyncio.wait_for(run_due_guilds(deps), timeout=20)
    assert len(outcomes) == 13
    assert len(world.llm.calls) <= 3
    assert time.monotonic() - started < 6


async def test_a_stored_pick_for_a_comped_cycle_is_never_remade_by_a_free_server(world):
    # Free servers read; the comped one's inline lookup made the row. One call total.
    for n in range(3):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1)
    world.follow(G2, "comped")
    world.clock = NOW
    await run_guild_digest(spy_deps(world), G2, kind=SCHED)
    made = len(world.llm.calls)
    assert made == 1
    await run_guild_digest(spy_deps(world), G1, kind=SCHED)
    assert len(world.llm.calls) == made


# --- 1b. the prepare job takes the industry entry first ---


def _config_with_games(tmp_path, extra: int) -> Path:
    """The industry fixture with `extra` more games ahead of the industry entry."""
    text = (FIXTURES / "config_v3_industry.yaml").read_text()
    games = "".join(
        f'  - key: extra{n}\n    name: "Extra {n}"\n    sources: []\n' for n in range(extra)
    )
    marker = "  - key: industry\n"
    assert marker in text
    path = tmp_path / "config_many.yaml"
    path.write_text(text.replace(marker, games + marker))
    return path


def _many_games_world(tmp_path, monkeypatch, extra=7, *, comped_industry=True, **llm_args):
    world = SummariesWorld(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")
    world.cfg = load_config(_config_with_games(tmp_path, extra))
    world.llm = FakeLLM(**llm_args)
    keys = ["borderlands4", "palworld", *[f"extra{n}" for n in range(extra)], INDUSTRY_KEY]
    followed = keys if comped_industry else [k for k in keys if k != INDUSTRY_KEY]
    world.guild(BER, "comped", BERLIN, followed)
    for key in keys:
        world.item(key, f"{key} news", ago(3))
    return world, keys


async def test_the_industry_pick_is_first_even_with_nine_slow_comped_games_ahead(
    tmp_path, monkeypatch
):
    world, keys = _many_games_world(tmp_path, monkeypatch, yields=25)
    assert len(keys) - 1 == 9  # nine comped games, then the industry entry
    done = await prepare_summaries(world.deps())
    assert world.llm.games()[0] == INDUSTRY_KEY
    assert sorted(done) == sorted(keys)
    # The games behind it stay in catalog order.
    assert world.llm.games()[1:] == [k for k in keys if k != INDUSTRY_KEY]


async def test_a_pass_cut_off_after_its_first_call_has_made_the_industry_pick(
    tmp_path, monkeypatch
):
    world, _ = _many_games_world(tmp_path, monkeypatch)
    first_call = asyncio.Event()

    class StallingLLM(FakeLLM):
        async def emit_stories(self, system, user):
            self.calls.append((system, user))
            first_call.set()
            await asyncio.sleep(3600)

    world.llm = StallingLLM()
    task = asyncio.create_task(prepare_summaries(world.deps()))
    await asyncio.wait_for(first_call.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert world.llm.games() == [INDUSTRY_KEY]


async def test_the_industry_pick_for_a_free_server_goes_first_among_comped_games(
    tmp_path, monkeypatch
):
    # The comped server follows games only; the one server that needs the pick is a free one.
    world, _ = _many_games_world(tmp_path, monkeypatch, extra=4, comped_industry=False)
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    done = await prepare_summaries(world.deps())
    assert world.llm.games()[0] == INDUSTRY_KEY
    assert INDUSTRY_KEY in done and len(done) == 7


def marks_on(world, guild_id, run_date):
    """The coverage marks one digest row recorded (`AdvWorld.marks` reads a server's first row)."""
    with world.conn() as conn:
        row = conn.execute(
            "SELECT game_items_upto FROM digests WHERE guild_id = ? AND run_date = ?",
            (guild_id, run_date),
        ).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def previous_digest(world, guild_id, run_date, mark, *, end):
    """A finished digest row that left the server's industry coverage at item id `mark`."""
    with world.conn() as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, "
            "posted_by_game, window_start, window_end, attempts, created_at, updated_at, "
            "items_after, items_upto, game_items_upto) "
            "VALUES (?, ?, 'ok', '[]', '{}', ?, ?, 1, ?, ?, 0, ?, ?)",
            (
                guild_id,
                run_date,
                (end - timedelta(hours=24)).isoformat(),
                end.isoformat(),
                end.isoformat(),
                end.isoformat(),
                mark,
                json.dumps({INDUSTRY_KEY: mark}),
            ),
        )


# --- 1c. no stored pick: newest five headlines, and the chain keeps going ---


async def test_a_server_that_saw_fallback_headlines_is_never_shown_them_again_nor_misses_news(
    world,
):
    # G2 (free) has an older digest that left its industry mark at item 1, so it is on a
    # different chain from G1 (comped, first digest). Day one: the prepared pick is G1's, so G2
    # has no pick it may read and shows the newest headlines; its mark moves to the range end.
    # Day two: whichever chain the prepare job serves, G2 sees what is new to it, once.
    world.add_item("industry", "Day zero", DUE - DAY - timedelta(hours=2))
    world.news("Day one A", hours_before_due=3)
    world.news("Day one B", hours_before_due=2)
    world.follow(G2)
    world.follow(G1, "comped")
    previous_digest(world, G2, "2026-09-29", 1, end=DUE - DAY)
    await world.prepare(DUE - timedelta(minutes=20))
    world.add_item("industry", "Day one late", DUE - timedelta(minutes=10))
    world.clock = NOW
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    assert set(world.industry_titles(G2)) == {"Day one A", "Day one B", "Day one late"}
    assert marks_on(world, G2, "2026-09-30")[INDUSTRY_KEY] == 4
    assert marks_on(world, G1, "2026-09-30")[INDUSTRY_KEY] == 3

    world.add_item("industry", "Day two", DUE + DAY - timedelta(hours=3))
    await world.prepare(DUE + DAY - timedelta(minutes=20))
    world.clock = NOW + DAY
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    assert world.industry_titles(G2) == ["Day two"]
    second_comped = str(world.channels[13].embeds[-1].description)
    assert "Day two" in second_comped and "Day one late" in second_comped
    assert "Day one A" not in second_comped and "Day one B" not in second_comped

    # Day three: still nothing repeated, nothing dropped.
    world.add_item("industry", "Day three", DUE + 2 * DAY - timedelta(hours=3))
    await world.prepare(DUE + 2 * DAY - timedelta(minutes=20))
    world.clock = NOW + 2 * DAY
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    assert world.industry_titles(G2) == ["Day three"]


async def test_the_pick_after_a_fallback_day_still_knows_the_stories_before_it(world):
    # G1 is shown a pick on day zero, then a fallback day (the prepare job missed it: no pick
    # for its chain), then gets a pick again. The day-two prompt must still carry day zero's
    # stories as prior, because the chain walks to the newest pick at or before the free
    # server's mark even though the mark is a range end and not a pick's end.
    world.news("Old A", hours_before_due=3)
    world.news("Old B", hours_before_due=2)
    world.follow(G1)
    await world.prepare(DUE - timedelta(minutes=20))
    world.clock = NOW
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    assert set(world.industry_titles()) == {"Old A", "Old B"}
    calls_after_day_zero = len(world.llm.calls)

    world.add_item("industry", "New one", DUE + DAY - timedelta(hours=3))
    world.clock = NOW + DAY
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    assert world.industry_titles() == ["New one"]
    assert len(world.llm.calls) == calls_after_day_zero  # a free build made nothing

    world.add_item("industry", "New two", DUE + 2 * DAY - timedelta(hours=3))
    await world.prepare(DUE + 2 * DAY - timedelta(minutes=20))
    assert len(world.llm.calls) == calls_after_day_zero + 1
    prompt = world.llm.calls[-1]
    assert "New two" in prompt and "New one" not in prompt
    assert "Old A" in prompt and "Old B" in prompt
    world.clock = NOW + 2 * DAY
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    assert world.industry_titles() == ["New two"]


async def test_a_stored_pick_made_for_another_servers_chain_is_not_shown_to_a_free_server(world):
    first = world.news("Seen already", hours_before_due=3)
    world.news("Fresh", hours_before_due=1)
    world.follow(G1)
    previous_digest(world, G1, "2026-09-29", 1, end=DUE - DAY)
    # A pick from the first-digest chain (after = None) that lists the already-seen item.
    world.store_pick([first])
    await run_guild_digest(spy_deps(world), G1, kind=SCHED)
    assert world.llm.calls == []
    assert world.industry_titles() == ["Fresh"]
    assert marks_on(world, G1, "2026-09-30")[INDUSTRY_KEY] == 2


# --- 2. link order ---


def save_summary(world, game_key, stories, *, due=DUE):
    """A stored `ok` summary; `stories` is a list of (headline, [urls in the model's order])."""
    with world.conn() as conn:
        newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0]
        return repo.save_game_summary(
            conn,
            game_key=game_key,
            run_date=due.date(),
            status="ok",
            window_start=due - timedelta(hours=24),
            window_end=due - timedelta(minutes=30),
            coverage_notes=[],
            note=None,
            usage=Usage(10, 5),
            stories=[
                StoryToSave(game_key, headline, "Model text.", "reported", urls, None)
                for headline, urls in stories
            ],
            token="t",  # noqa: S106
            now=lambda: due,
            items_after=None,
            items_upto=newest,
        )


def two_items(world, game, stem):
    """Two stored items for one story: (low id, high id) as URLs."""
    low = world.news(f"{stem} low", game=game, hours_before_due=3)
    high = world.news(f"{stem} high", game=game, hours_before_due=2)
    return low, high


async def test_a_free_server_shows_the_models_first_item_of_a_merged_story_even_if_newer(world):
    low, high = two_items(world, INDUSTRY_KEY, "Merged")
    other_low, other_high = two_items(world, INDUSTRY_KEY, "Second")
    world.follow(G1)
    save_summary(
        world,
        INDUSTRY_KEY,
        [("Story one", [high, low]), ("Story two", [other_low, other_high])],
    )
    await run_guild_digest(spy_deps(world), G1, kind=SCHED)
    assert world.industry_titles() == ["Merged high", "Second low"]
    assert world.llm.calls == []


async def test_a_free_preview_and_run_now_show_the_same_first_item(world):
    low, high = two_items(world, INDUSTRY_KEY, "Merged")
    world.follow(G1)
    save_summary(world, INDUSTRY_KEY, [("Story one", [high, low])])
    preview = await preview_guild_digest(spy_deps(world), G1)
    [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
    assert titles(str(message.embed.description).split("\n")) == ["Merged high"]
    await run_guild_digest(spy_deps(world), G1, kind=RunKind.RUN_NOW, force=True)
    assert world.industry_titles() == ["Merged high"]


async def test_a_comped_industry_story_lists_its_links_in_the_models_order(world):
    low, high = two_items(world, INDUSTRY_KEY, "Merged")
    world.follow(G1, "comped")
    save_summary(world, INDUSTRY_KEY, [("Story one", [high, low])])
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    text = str(world.channels[13].embeds[-1].description)
    assert high in text and low in text
    assert text.index(high) < text.index(low)


async def test_a_comped_game_story_keeps_its_links_in_item_id_order(world):
    low, high = two_items(world, PAL, "Merged")
    world.follow(G1, "comped")
    save_summary(world, PAL, [("Story one", [high, low])])
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    text = str(world.channels[12].embeds[-1].description)
    assert text.index(low) < text.index(high)


async def test_the_freshly_made_pick_returned_to_its_maker_is_in_the_models_order(sworld):
    # `ensure_summary` hands back the row it just saved, read through the same path.
    first = "https://example.com/industry/first"
    second = "https://example.com/industry/second"
    sworld.item(INDUSTRY_KEY, "first", ago(5), url=first)
    sworld.item(INDUSTRY_KEY, "second", ago(4), url=second)

    class ReversingLLM(FakeLLM):
        async def emit_stories(self, system, user):
            result = await super().emit_stories(system, user)
            result.stories.stories[0].item_urls = [second, first]
            return result

    sworld.llm = ReversingLLM()
    sworld.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    got = await ensure_summary(sworld.deps(), INDUSTRY_KEY, BERLIN_DUE, run_date=BERLIN_DUE.date())
    assert got is not None
    assert got.stories[0].item_urls == [second, first]
    with sworld.conn() as conn:
        again = summary_from_row(conn, repo.game_summary_by_id(conn, got.summary_id))
    assert again.stories[0].item_urls == [second, first]


async def test_link_order_survives_pruning_the_first_item_and_a_vacuum(world):
    low = world.news("Low", hours_before_due=4)
    mid = world.news("Mid", hours_before_due=3)
    high = world.news("High", hours_before_due=2)
    sid = save_summary(world, INDUSTRY_KEY, [("S", [mid, high, low])])
    with world.conn() as conn:
        row = repo.game_summary_by_id(conn, sid)
        assert summary_from_row(conn, row).stories[0].item_urls == [mid, high, low]
    with world.conn() as conn, conn:
        conn.execute("DELETE FROM items WHERE url = ?", (mid,))
    with world.conn() as conn:
        conn.isolation_level = None
        conn.execute("VACUUM")
        row = repo.game_summary_by_id(conn, sid)
        assert summary_from_row(conn, row).stories[0].item_urls == [high, low]


async def test_link_order_holds_across_the_query_chunk_boundary(world, monkeypatch):
    # The story ids are fetched in chunks of `_SQLITE_VARIABLE_CHUNK`; shrink it so five
    # stories span three chunks and check every story still comes back in its own order.
    monkeypatch.setattr(repo, "_SQLITE_VARIABLE_CHUNK", 2)
    stories = []
    for n in range(5):
        low, high = two_items(world, INDUSTRY_KEY, f"Merged {n}")
        stories.append((f"Story {n}", [high, low]))
    sid = save_summary(world, INDUSTRY_KEY, stories)
    with world.conn() as conn:
        views = repo.summary_stories(conn, sid, link_order=True)
        plain = repo.summary_stories(conn, sid)
    assert [v.urls for v in views] == [urls for _, urls in stories]
    assert [v.urls for v in plain] == [urls[::-1] for _, urls in stories]


async def test_an_item_shared_by_two_stories_keeps_each_storys_own_order(world):
    low, high = two_items(world, INDUSTRY_KEY, "Shared")
    third = world.news("Third", hours_before_due=1)
    sid = save_summary(world, INDUSTRY_KEY, [("A", [high, low]), ("B", [third, high, low])])
    with world.conn() as conn:
        stories = summary_from_row(conn, repo.game_summary_by_id(conn, sid)).stories
    assert [s.item_urls for s in stories] == [[high, low], [third, high, low]]


async def test_the_plain_story_queries_still_sort_industry_stories_by_item_id(world):
    low, high = two_items(world, INDUSTRY_KEY, "Merged")
    sid = save_summary(world, INDUSTRY_KEY, [("S", [high, low])])
    with world.conn() as conn:
        assert [v.urls for v in repo.summary_stories(conn, sid)] == [[low, high]]
        assert [v.urls for v in repo.summary_stories(conn, sid, link_order=True)] == [[high, low]]


@pytest.fixture(scope="module")
def base_repo():
    """`newsbot/store/repo.py` as of the base commit, loaded under another name."""
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{BASE_COMMIT}:newsbot/store/repo.py"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {BASE_COMMIT} is not available here")
    name = "_newsbot_repo_f694f37_throwaway"
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


def _game_shapes(world):
    """Several stories per game with links deliberately listed out of id order."""
    items = [world.news(f"Item {n}", game=PAL, hours_before_due=5 - n / 10) for n in range(6)]
    return [
        ("one", [items[3], items[0]]),
        ("two", [items[5], items[1], items[4]]),
        ("three", [items[2]]),
        ("four", [items[4], items[3], items[2], items[1], items[0]]),
    ]


async def test_comped_game_story_views_are_identical_to_the_base_commits(world, base_repo):
    stories = _game_shapes(world)
    sid = save_summary(world, PAL, stories)
    other = save_summary(world, BL4, [], due=DUE)
    with world.conn() as conn:
        now_views = repo.summary_stories(conn, sid)
        base_views = base_repo.summary_stories(conn, sid)
        assert now_views == base_views
        assert repo.summary_stories(conn, other) == base_repo.summary_stories(conn, other)
        window = (DUE - timedelta(days=3), None, 50, 0)
        got, total = repo.query_stories(conn, [PAL], *window)
        want, want_total = base_repo.query_stories(conn, [PAL], *window)
        assert (got, total) == (want, want_total)
    # Spelled out, so the comparison above can't pass with both sides wrong the same way.
    assert [v.urls[0] for v in now_views][0] == min(stories[0][1], key=lambda u: u)


async def test_a_comped_game_embed_is_byte_identical_to_one_rendered_from_the_base_commit(
    world, base_repo
):
    from newsbot.bot.format import render_guild_digest
    from newsbot.pipeline.summarize import StoryDraft

    stories = _game_shapes(world)
    sid = save_summary(world, PAL, stories)
    world.follow(G1, "comped", games=(PAL,))
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    posted = world.channels[12].embeds[-1].to_dict()

    with world.conn() as conn:
        views = base_repo.summary_stories(conn, sid)
    drafts = [
        StoryDraft(
            headline=v.headline,
            summary=v.summary,
            label=v.label,
            item_urls=list(v.urls),
            update_of_story_id=v.is_update_of,
        )
        for v in views
    ]
    pal = next(g for g in world.cfg.catalog if g.key == PAL)
    expected = render_guild_digest(
        DUE.date(),
        [pal],
        {PAL: 12},
        stories_by_game={PAL: drafts},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    assert [m.embed.to_dict() for m in expected.messages][-1] == posted


async def test_the_pick_as_a_comped_industry_story_differs_from_base_only_in_link_order(
    world, base_repo
):
    low, high = two_items(world, INDUSTRY_KEY, "Merged")
    sid = save_summary(world, INDUSTRY_KEY, [("S", [high, low])])
    with world.conn() as conn:
        mine = repo.summary_stories(conn, sid, link_order=True)
        base = base_repo.summary_stories(conn, sid)
    assert [v.urls for v in base] == [[low, high]]
    assert [v.urls for v in mine] == [[high, low]]
    assert [replace_urls(v) for v in mine] == [replace_urls(v) for v in base]


def replace_urls(view):
    return (view.id, view.headline, view.summary, view.label, view.is_update_of)


# --- 3. the finished-digest skip rule ---


def digest_row(
    world, guild_id, run_date, status, *, marks=None, window_end=None, upto=None, posted="{}"
):
    """A digest row the way the scheduler leaves it, optionally with a coverage mark."""
    end = (window_end or BERLIN_DUE).isoformat()
    with world.conn() as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, "
            "posted_by_game, window_start, window_end, attempts, created_at, updated_at, "
            "items_after, items_upto, game_items_upto) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, 1, ?, ?, 0, ?, ?)",
            (
                guild_id,
                run_date,
                status,
                posted,
                (BERLIN_DUE - timedelta(days=2)).isoformat(),
                end,
                end,
                end,
                upto,
                marks,
            ),
        )


async def test_a_finished_digest_for_yesterday_does_not_hide_todays_summaries(sworld):
    sworld.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    sworld.item("palworld", "raid boss patch", ago(3))
    sworld.item(INDUSTRY_KEY, "studio closes", ago(3))
    digest_row(sworld, BER, "2026-09-30", "ok")
    sworld.clock.t = BERLIN_DUE - timedelta(minutes=10)
    done = await prepare_summaries(sworld.deps())
    assert sorted(done) == [INDUSTRY_KEY, "palworld"]


async def test_a_finished_digest_in_the_grace_window_hides_only_its_own_server(sworld):
    # PAC (Pacific, 09:00 local = 16:00 UTC) is finished; BER (Berlin) is a different clock
    # whose own due is later in the grace window. At 07:10 UTC Berlin is in its grace.
    sworld.guild(PAC, "comped", "America/Los_Angeles", ["palworld"])
    sworld.guild(BER, "comped", BERLIN, ["palworld"])
    sworld.item("palworld", "raid boss patch", ago(3))
    digest_row(sworld, BER, "2026-10-01", "ok")
    digest_row(sworld, PAC, "2026-10-01", "ok")
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=10)
    # Berlin is done; Pacific's digest is nine hours away (outside the lead).
    assert await prepare_summaries(sworld.deps()) == []
    assert sworld.llm.calls == []


@pytest.mark.parametrize("sibling", ["none", "pending"])
async def test_two_servers_sharing_a_due_time_where_one_posted_still_make_the_other_its_summary(
    sworld, sibling
):
    sworld.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    sworld.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    sworld.item("palworld", "raid boss patch", ago(3))
    sworld.item(INDUSTRY_KEY, "studio closes", ago(3))
    digest_row(sworld, PAC, "2026-10-01", "ok")
    if sibling == "pending":
        digest_row(sworld, BER, "2026-10-01", "pending")
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=3)
    done = await prepare_summaries(sworld.deps())
    assert sorted(done) == [INDUSTRY_KEY, "palworld"]


async def test_the_summary_made_for_the_unposted_server_follows_that_servers_own_chain(sworld):
    # PAC posted and has a mark of 1 for palworld; BER has never had a digest. The first
    # server in the loop is skipped, so the summary must start where BER's coverage ends
    # (the beginning), not where PAC's does.
    sworld.guild(PAC, "comped", BERLIN, ["palworld"])
    sworld.guild(BER, "comped", BERLIN, ["palworld"])
    for n in range(3):
        sworld.item("palworld", f"patch {n}", ago(6 - n))
    yesterday = BERLIN_DUE - timedelta(days=1)
    digest_row(
        sworld, PAC, "2026-09-30", "ok", marks='{"palworld": 1}', window_end=yesterday, upto=1
    )
    digest_row(sworld, PAC, "2026-10-01", "ok")
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=3)
    assert await prepare_summaries(sworld.deps()) == ["palworld"]
    [row] = sworld.summaries("palworld")
    assert row["items_after"] is None
    [call] = sworld.llm.calls
    for n in range(3):
        assert f"patch {n}" in call[1]


async def test_the_unfinished_server_keeps_its_own_chain_when_the_finished_one_is_the_later_id(
    sworld,
):
    sworld.guild(PAC, "comped", BERLIN, ["palworld"])
    sworld.guild(BER, "comped", BERLIN, ["palworld"])
    for n in range(3):
        sworld.item("palworld", f"patch {n}", ago(6 - n))
    yesterday = BERLIN_DUE - timedelta(days=1)
    digest_row(
        sworld, PAC, "2026-09-30", "ok", marks='{"palworld": 1}', window_end=yesterday, upto=1
    )
    digest_row(sworld, BER, "2026-10-01", "failed")
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=3)
    # PAC is the unfinished one here (no row today), and has a mark of 1.
    assert await prepare_summaries(sworld.deps()) == ["palworld"]
    [row] = sworld.summaries("palworld")
    assert row["items_after"] == 1


@pytest.mark.parametrize("finished_tier", ["comped", "free"])
async def test_a_finished_server_of_one_tier_does_not_hide_an_unfinished_one_of_the_other(
    sworld, finished_tier
):
    other = "free" if finished_tier == "comped" else "comped"
    sworld.guild(PAC, finished_tier, BERLIN, [INDUSTRY_KEY])
    sworld.guild(BER, other, BERLIN, [INDUSTRY_KEY])
    sworld.item(INDUSTRY_KEY, "studio closes", ago(3))
    digest_row(sworld, PAC, "2026-10-01", "ok")
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=3)
    assert await prepare_summaries(sworld.deps()) == [INDUSTRY_KEY]


async def test_after_the_grace_window_a_finished_digest_changes_nothing(sworld):
    sworld.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    sworld.item("palworld", "raid boss patch", ago(3))
    sworld.item(INDUSTRY_KEY, "studio closes", ago(3))
    digest_row(sworld, BER, "2026-10-01", "ok")
    # 31 minutes past: the next due is tomorrow's, far outside the lead. Nothing to do.
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=31)
    assert await prepare_summaries(sworld.deps()) == []
    assert sworld.llm.calls == []


async def test_the_next_days_summaries_are_still_made_after_a_finished_day(sworld):
    sworld.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    sworld.item("palworld", "raid boss patch", ago(3))
    sworld.item(INDUSTRY_KEY, "studio closes", ago(3))
    digest_row(sworld, BER, "2026-10-01", "ok")
    tomorrow = BERLIN_DUE + timedelta(days=1)
    sworld.item("palworld", "next day patch", tomorrow - timedelta(hours=3))
    sworld.item(INDUSTRY_KEY, "next day news", tomorrow - timedelta(hours=3))
    sworld.clock.t = tomorrow - timedelta(minutes=20)
    done = await prepare_summaries(sworld.deps())
    assert sorted(done) == [INDUSTRY_KEY, "palworld"]


async def test_a_normal_comped_day_makes_each_summary_exactly_once(sworld):
    # Walk the clock every five minutes from forty minutes before the digest to forty after,
    # the way the prepare job runs; the digest finishes at the due instant. Nothing is made
    # twice, nothing is skipped, and nothing is made after the digest is done.
    sworld.guild(BER, "comped", BERLIN, ["borderlands4", "palworld", INDUSTRY_KEY])
    for key in ("borderlands4", "palworld", INDUSTRY_KEY):
        sworld.item(key, f"{key} news", ago(3))
    seen_after_done = None
    for minute in range(-40, 45, 5):
        sworld.clock.t = BERLIN_DUE + timedelta(minutes=minute)
        if minute == 0:
            digest_row(sworld, BER, "2026-10-01", "ok")
        await prepare_summaries(sworld.deps())
        if minute == 0:
            seen_after_done = len(sworld.llm.calls)
    assert sorted(sworld.llm.games()) == ["borderlands4", INDUSTRY_KEY, "palworld"]
    assert len(sworld.llm.calls) == seen_after_done == 3


async def test_a_normal_day_with_the_digest_late_still_gets_every_summary(sworld):
    # The digest is pending the whole time (a slow day): summaries are made as before.
    sworld.guild(BER, "comped", BERLIN, ["borderlands4", "palworld", INDUSTRY_KEY])
    for key in ("borderlands4", "palworld", INDUSTRY_KEY):
        sworld.item(key, f"{key} news", ago(3))
    digest_row(sworld, BER, "2026-10-01", "pending")
    sworld.clock.t = BERLIN_DUE + timedelta(minutes=25)
    done = await prepare_summaries(sworld.deps())
    assert sorted(done) == ["borderlands4", INDUSTRY_KEY, "palworld"]


@pytest.mark.parametrize(
    ("tz", "due_utc", "run_date", "yesterday"),
    [
        # Berlin falls back at 01:00 UTC on 2026-10-25: 09:00 is 08:00 UTC that day.
        (BERLIN, datetime(2026, 10, 25, 8, 0, tzinfo=UTC), "2026-10-25", "2026-10-24"),
        # Berlin springs forward 2027-03-28: 09:00 is 07:00 UTC.
        (BERLIN, datetime(2027, 3, 28, 7, 0, tzinfo=UTC), "2027-03-28", "2027-03-27"),
        # Los Angeles falls back 2026-11-01: 09:00 is 17:00 UTC.
        (
            "America/Los_Angeles",
            datetime(2026, 11, 1, 17, 0, tzinfo=UTC),
            "2026-11-01",
            "2026-10-31",
        ),
        # Los Angeles springs forward 2027-03-14: 09:00 is 16:00 UTC.
        (
            "America/Los_Angeles",
            datetime(2027, 3, 14, 16, 0, tzinfo=UTC),
            "2027-03-14",
            "2027-03-13",
        ),
    ],
)
async def test_the_skip_rule_follows_the_local_date_across_a_dst_change(
    sworld, tz, due_utc, run_date, yesterday
):
    sworld.guild(BER, "comped", tz, ["palworld", INDUSTRY_KEY])
    sworld.item("palworld", "raid boss patch", due_utc - timedelta(hours=3))
    sworld.item(INDUSTRY_KEY, "studio closes", due_utc - timedelta(hours=3))
    # Yesterday's digest finished; today's hasn't: both summaries are due.
    digest_row(sworld, BER, yesterday, "ok", window_end=due_utc - timedelta(days=1))
    sworld.clock.t = due_utc + timedelta(minutes=5)
    done = await prepare_summaries(sworld.deps())
    assert sorted(done) == [INDUSTRY_KEY, "palworld"]
    # Today's finishes: nothing more is made for it, a minute later or at the edge of grace.
    digest_row(sworld, BER, run_date, "ok", window_end=due_utc)
    calls = len(sworld.llm.calls)
    for minute in (6, 20, 30):
        sworld.clock.t = due_utc + timedelta(minutes=minute)
        assert await prepare_summaries(sworld.deps()) == []
    assert len(sworld.llm.calls) == calls


async def test_a_catch_up_after_a_long_outage_does_not_get_its_summaries_skipped(sworld):
    # The bot was down across the due instant and the grace window. The server's digest for the
    # day is not done, so nothing finished hides it; the prepare job then looks at tomorrow's
    # due (too far off), and the catch-up digest makes its summary inline as it always did.
    sworld.guild(BER, "comped", BERLIN, ["palworld"])
    sworld.item("palworld", "raid boss patch", ago(3))
    sworld.clock.t = BERLIN_DUE + timedelta(hours=3)
    assert await prepare_summaries(sworld.deps()) == []
    assert sworld.llm.calls == []
    digest_row(sworld, BER, "2026-10-02", "pending")
    sworld.clock.t = BERLIN_DUE + timedelta(days=1) - timedelta(minutes=10)
    assert await prepare_summaries(sworld.deps()) == ["palworld"]


@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_a_retried_failed_digest_still_posts_the_industry_entry_with_no_summary_prepared(
    world, tier
):
    # A clean failure is retried ten minutes on, possibly before the prepare job's lead
    # window. The skip rule hides that server from the prepare job, so the retry builds
    # without a prepared pick: a comped server makes it inline, a free one shows headlines.
    # Neither may post nothing, and neither may be left `failed` for want of a summary.
    world.news("Studio closes", hours_before_due=2)
    world.news("Publisher sold", hours_before_due=1)
    world.follow(G1, tier)
    with world.conn() as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, "
            "posted_by_game, window_start, window_end, attempts, created_at, updated_at, "
            "items_after, items_upto) VALUES (?, '2026-09-30', 'failed', '[]', '{}', ?, ?, 1, "
            "?, ?, 0, 2)",
            (
                G1,
                (DUE - DAY).isoformat(),
                DUE.isoformat(),
                (NOW - timedelta(minutes=30)).isoformat(),
                (NOW - timedelta(minutes=30)).isoformat(),
            ),
        )
    world.clock = NOW - timedelta(minutes=35)
    assert await prepare_summaries(world.summary_deps()) == []
    assert world.llm.calls == []
    world.clock = NOW
    [outcome] = await run_due_guilds(world.real_deps())
    assert outcome.status in ("ok", "partial") and INDUSTRY_KEY in outcome.posted_by_game
    text = str(world.channels[13].embeds[-1].description)
    assert "Studio closes" in text and "Publisher sold" in text
    assert len(world.llm.calls) == (1 if tier == "comped" else 0)


# --- 4. one industry alert a day ---


async def test_many_chains_in_one_outage_send_one_industry_alert(sworld):
    sworld.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    sworld.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    for n in range(8):
        sworld.item(INDUSTRY_KEY, f"studio {n}", ago(6 - n / 2))
    deps = sworld.deps()
    for mark in (None, 1, 2, 3, 4, 5):
        after = None if mark is None else Coverage(BERLIN_DUE - timedelta(days=1), mark)
        got = await ensure_summary(
            deps, INDUSTRY_KEY, BERLIN_DUE, run_date=BERLIN_DUE.date(), after=after
        )
        assert got is not None and got.status == "fallback"
    assert sworld.alerts == ["newsbot: summary for Gaming industry fell back to headlines"]
    assert summary_rows(sworld) == 6


def _outage_world(sworld, items=8, **fail):
    sworld.llm.fail = fail or {INDUSTRY_KEY: LLMError("503")}
    sworld.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    for n in range(items):
        sworld.item(INDUSTRY_KEY, f"studio {n}", ago(6 - n / 2))
        sworld.item("palworld", f"patch {n}", ago(6 - n / 2))


def _chain(n: int) -> Coverage:
    return Coverage(BERLIN_DUE - timedelta(days=1), n)


async def test_the_industry_alert_comes_back_after_midnight_utc_and_not_before(sworld):
    sworld.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    sworld.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    due = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    for n in range(6):
        sworld.item(
            INDUSTRY_KEY, f"studio {n}", datetime(2026, 9, 30, 20, 0, tzinfo=UTC) + n * HOUR / 2
        )
    deps = sworld.deps()
    stamps = [
        datetime(2026, 9, 30, 23, 50, tzinfo=UTC),
        datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 1, 0, 0, 1, tzinfo=UTC),
    ]
    for n, stamp in enumerate(stamps):
        sworld.clock.t = stamp
        got = await ensure_summary(
            deps, INDUSTRY_KEY, due, run_date=due.date(), after=Coverage(due - 2 * HOUR * 12, n)
        )
        assert got is not None and got.status == "fallback"
    assert len(sworld.alerts) == 2


async def test_a_good_pick_between_two_same_day_fallbacks_does_not_rearm_the_alert(sworld):
    _outage_world(sworld)
    deps = sworld.deps()
    day = BERLIN_DUE.date()
    first = await ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=day, after=_chain(0))
    sworld.llm.fail = {}
    good = await ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=day, after=_chain(1))
    sworld.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    last = await ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=day, after=_chain(2))
    assert [first.status, good.status, last.status] == ["fallback", "ok", "fallback"]
    assert len(sworld.alerts) == 1


async def test_a_game_fallback_does_not_use_up_the_industry_alert_and_vice_versa(sworld):
    _outage_world(sworld, **{"palworld": LLMError("503"), INDUSTRY_KEY: LLMError("503")})
    deps = sworld.deps()
    day = BERLIN_DUE.date()
    plan = [
        ("palworld", 0),
        (INDUSTRY_KEY, 0),
        ("palworld", 1),
        (INDUSTRY_KEY, 1),
        ("palworld", 2),
    ]
    for key, mark in plan:
        got = await ensure_summary(deps, key, BERLIN_DUE, run_date=day, after=_chain(mark))
        assert got is not None and got.status == "fallback"
    pal = "newsbot: summary for Palworld fell back to headlines"
    industry = "newsbot: summary for Gaming industry fell back to headlines"
    assert sworld.alerts == [pal, industry, pal, pal]


async def test_a_failing_alert_does_not_break_the_pick_or_the_next_one(sworld):
    _outage_world(sworld)

    async def broken(text: str) -> None:
        raise RuntimeError("alert channel is gone")

    deps = sworld.deps(alert=broken)
    day = BERLIN_DUE.date()
    first = await ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=day, after=_chain(0))
    second = await ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=day, after=_chain(1))
    assert first is not None and second is not None
    assert first.status == second.status == "fallback"


async def test_each_summary_deps_keeps_its_own_alert_day(sworld):
    # `alerted_on` is per instance, like `save_failures`: the bot builds one SummaryDeps and
    # keeps it, so one outage is one alert. A second instance (the CLI's) alerts afresh.
    _outage_world(sworld)
    day = BERLIN_DUE.date()
    for n in range(2):
        await ensure_summary(sworld.deps(), INDUSTRY_KEY, BERLIN_DUE, run_date=day, after=_chain(n))
    assert len(sworld.alerts) == 2


async def test_prepare_passes_five_minutes_apart_alert_once_for_one_outage(sworld):
    sworld.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    sworld.guild(BER, "comped", BERLIN, [INDUSTRY_KEY])
    sworld.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    sworld.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = sworld.deps()
    for minute in (-25, -20, -15, -10, -5):
        sworld.clock.t = BERLIN_DUE + timedelta(minutes=minute)
        await prepare_summaries(deps)
    assert len(sworld.alerts) == 1


def test_the_deps_alert_day_is_not_shared_between_instances():
    a = SummaryDeps(cfg=None, db_path="x", llm=None, now=lambda: NOW, alert=None)  # type: ignore[arg-type]
    b = SummaryDeps(cfg=None, db_path="y", llm=None, now=lambda: NOW, alert=None)  # type: ignore[arg-type]
    a.alerted_on["industry"] = NOW.date()
    assert b.alerted_on == {}


# keep imports that only some rigs use honest
_ = (GameSummary, GuildDigestDeps, CHANNELS, draft, closing, connect, re, DAY)
