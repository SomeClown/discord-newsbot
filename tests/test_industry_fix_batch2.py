"""Fix batch 2 from the final qa pass: where the industry pick gets made, and which links it keeps.

Four small rules, each pinned here because each one was a quiet way to waste a model call
or show the wrong link:

1. Only the prepare job makes an industry pick (a free server's digest build reads what is
   stored and never starts or waits on a call; that half lives in `test_guild_digest_industry.py`).
   The prepare job also takes the industry entry first, so slow game calls can't starve it.
2. A stored pick keeps the model's link order (`stories` linked in the order the model listed
   them), not the lowest item id first. Game stories keep their id order, byte for byte.
3. The prepare job doesn't make a summary for a server whose digest for the day is already
   done.
4. A fallback pick alerts the owner once per day, not once per diverged chain.

The summaries rig is `test_summaries.World` with the industry catalog.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_summaries import BER, BERLIN, BERLIN_DUE, FREE, World, ago

from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline.guild_digest import summary_from_row
from newsbot.pipeline.summaries import ensure_summary, prepare_summaries
from newsbot.pipeline.summarize import LLMError
from newsbot.store import repo
from newsbot.store.models import Coverage, StoryToSave, Usage


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")


def _save(world, game_key, urls):
    """A stored `ok` summary holding one story that links `urls` in the order given."""
    with world.conn() as conn:
        summary_id = repo.save_game_summary(
            conn,
            game_key=game_key,
            run_date=BERLIN_DUE.date(),
            status="ok",
            window_start=BERLIN_DUE - timedelta(hours=24),
            window_end=BERLIN_DUE - timedelta(minutes=30),
            coverage_notes=[],
            note=None,
            usage=Usage(1, 1),
            stories=[StoryToSave(game_key, "A story", "Text.", "reported", urls, None)],
            token="t",  # noqa: S106
            now=lambda: BERLIN_DUE,
        )
        return summary_from_row(conn, repo.game_summary_by_id(conn, summary_id))


# --- 2. link order ---


def test_a_stored_industry_story_keeps_the_models_first_link_even_when_it_has_the_higher_id(world):
    low = "https://example.com/industry/low"
    high = "https://example.com/industry/high"
    world.item(INDUSTRY_KEY, "low", ago(5), url=low)
    world.item(INDUSTRY_KEY, "high", ago(4), url=high)
    summary = _save(world, INDUSTRY_KEY, [high, low])
    assert summary.stories[0].item_urls == [high, low]


def test_a_stored_game_story_still_lists_its_links_by_item_id(world):
    low = "https://example.com/palworld/low"
    high = "https://example.com/palworld/high"
    world.item("palworld", "low", ago(5), url=low)
    world.item("palworld", "high", ago(4), url=high)
    summary = _save(world, "palworld", [high, low])
    assert summary.stories[0].item_urls == [low, high]


def test_the_plain_story_query_is_unchanged_for_the_industry_entry(world):
    low = "https://example.com/industry/low"
    high = "https://example.com/industry/high"
    world.item(INDUSTRY_KEY, "low", ago(5), url=low)
    world.item(INDUSTRY_KEY, "high", ago(4), url=high)
    _save(world, INDUSTRY_KEY, [high, low])
    with world.conn() as conn:
        stories, _ = repo.query_stories(
            conn, [INDUSTRY_KEY], BERLIN_DUE - timedelta(days=2), None, 10, 0
        )
    [story] = stories
    assert story.urls == [low, high]


# --- 1. the prepare job takes the industry entry first ---


async def test_the_prepare_job_makes_the_industry_pick_before_any_game_summary(world):
    world.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())
    assert world.llm.games() == [INDUSTRY_KEY, "palworld"]


async def test_a_game_summary_is_not_starved_by_an_industry_pick_that_fails(world):
    world.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    world.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    done = await prepare_summaries(world.deps())
    assert sorted(done) == [INDUSTRY_KEY, "palworld"]


# --- 3. a finished digest needs no new summary ---


def _digest(world, guild_id, status):
    with world.conn() as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, error_notes, created_at, "
            "updated_at) VALUES (?, ?, ?, NULL, ?, ?)",
            (
                guild_id,
                "2026-10-01",
                status,
                BERLIN_DUE.isoformat(),
                BERLIN_DUE.isoformat(),
            ),
        )


@pytest.mark.parametrize("status", ["ok", "partial", "failed"])
@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_a_server_whose_digest_for_the_day_finished_gets_no_new_pick(world, status, tier):
    world.guild(FREE, tier, BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    _digest(world, FREE, status)
    world.clock.t = BERLIN_DUE + timedelta(minutes=5)
    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == []


async def test_a_finished_comped_digest_needs_no_game_summary_either(world):
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))
    _digest(world, BER, "ok")
    world.clock.t = BERLIN_DUE + timedelta(minutes=5)
    assert await prepare_summaries(world.deps()) == []


@pytest.mark.parametrize("pending", [False, True])
async def test_an_unfinished_digest_still_gets_its_summaries_in_the_grace_period(world, pending):
    world.guild(BER, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    if pending:
        _digest(world, BER, "pending")
    world.clock.t = BERLIN_DUE + timedelta(minutes=5)
    done = await prepare_summaries(world.deps())
    assert sorted(done) == [INDUSTRY_KEY, "palworld"]


async def test_a_finished_server_does_not_hide_another_servers_need(world):
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(FREE + 1, "comped", BERLIN, ["palworld"], time="09:05")
    world.item("palworld", "raid boss patch", ago(3))
    _digest(world, BER, "ok")
    world.clock.t = BERLIN_DUE + timedelta(minutes=2)
    assert await prepare_summaries(world.deps()) == ["palworld"]


# --- 4. one alert per day ---


async def test_fallback_picks_alert_the_owner_once_a_day_not_once_per_chain(world):
    world.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    for n in range(4):
        world.item(INDUSTRY_KEY, f"studio {n}", ago(3 - n / 10))
    deps = world.deps()
    due = BERLIN_DUE
    run_date = due.date()
    # Three servers on three different chains: each asks for its own pick.
    for after_id in (None, 1, 2):
        after = None if after_id is None else Coverage(due - timedelta(days=1), after_id)
        await ensure_summary(deps, INDUSTRY_KEY, due, run_date=run_date, after=after)
    assert world.alerts == ["newsbot: summary for Gaming industry fell back to headlines"]


async def test_the_alert_comes_again_the_next_day(world):
    world.llm.fail = {INDUSTRY_KEY: LLMError("503")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = world.deps()
    await ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=BERLIN_DUE.date())
    world.item(INDUSTRY_KEY, "publisher sold", BERLIN_DUE + timedelta(hours=2))
    world.clock.t += timedelta(days=1)
    tomorrow = BERLIN_DUE + timedelta(days=1)
    await ensure_summary(deps, INDUSTRY_KEY, tomorrow, run_date=tomorrow.date())
    assert len(world.alerts) == 2


async def test_a_game_fallback_alerts_every_time_as_before(world):
    world.llm.fail = {"palworld": LLMError("503")}
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))
    world.item("palworld", "second patch", ago(2))
    deps = world.deps()
    await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=BERLIN_DUE.date())
    await ensure_summary(
        deps,
        "palworld",
        BERLIN_DUE,
        run_date=BERLIN_DUE.date(),
        after=Coverage(BERLIN_DUE - timedelta(days=1), 1),
    )
    assert world.alerts == ["newsbot: summary for Palworld fell back to headlines"] * 2
