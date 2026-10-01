"""A summary made before the news arrived must not be shown after it did.

The dev bot started at 13:52 Pacific with yesterday's digest owed. The digest job posted it
at 13:53 with every game at 0 items, and the lookup it used made (and saved) a summary per
game from the nothing there was yet. The first collection pass then stored 149 items. After a
restart, `/newsbot preview` reused those empty summaries ("Nothing would post right now"):
they started exactly where the server's coverage ended, ended after its last window, and were
well under six hours old, and nothing checked what they had *seen*. The reuse rule has a new
condition for a run that ends "now": a stored summary that stops short of the range's newest
id, with an item for its game in the gap, is skipped. The scheduled digest is the exception on
purpose: it ends at its due instant, and what the prepare job missed waits for the next window.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import timedelta

import pytest
from cutover_world import DUE, FRIEND, add_item, collect_these
from test_digest_first_pass_hold import PASS_ITEM, STARTED

from newsbot.pipeline.guild_digest import preview_guild_digest, run_guild_digest
from newsbot.pipeline.run import RunKind
from newsbot.store.db import connect

PASS_URL = PASS_ITEM["url"]


def dump(world) -> list[str]:
    with closing(connect(world.db_path)) as conn:
        return list(conn.iterdump())


def summaries(world) -> list[tuple]:
    return world.rows("SELECT game_key, status, items_upto FROM game_summaries ORDER BY id")


async def owed_digest_then_a_pass(world, monkeypatch, tmp_path):
    """20:52 start, 20:53 digest with nothing to say, 20:54 the first pass stores an item."""
    collect_these(monkeypatch, tmp_path, [PASS_ITEM])
    await world.ready()  # the hold lifted: the old code, which posted at 20:53
    await world.tick(STARTED + timedelta(minutes=1))
    world.set_now(STARTED + timedelta(minutes=2))
    await world.bot._collection_job()
    world.set_now(STARTED + timedelta(minutes=8))  # about 21:00


async def test_preview_after_the_pass_shows_the_pass_items(make_world, monkeypatch, tmp_path):
    world = await make_world(now=STARTED)
    await owed_digest_then_a_pass(world, monkeypatch, tmp_path)
    before = dump(world)

    preview = await preview_guild_digest(world.bot.guild_digest_deps(), FRIEND)

    assert preview is not None and PASS_URL in world.llm.urls_seen()
    assert "palworld" in [m.topic_key for m in preview.rendered.messages]
    assert dump(world) == before  # and a preview writes nothing, a repaired one included


async def test_run_now_after_the_pass_posts_the_pass_items(make_world, monkeypatch, tmp_path):
    world = await make_world(now=STARTED)
    await owed_digest_then_a_pass(world, monkeypatch, tmp_path)
    rows_before = len(summaries(world))

    outcome = await run_guild_digest(
        world.bot.guild_digest_deps(), FRIEND, kind=RunKind.RUN_NOW, force=True
    )

    assert outcome.status in ("ok", "partial")
    assert PASS_URL in world.llm.urls_seen()
    assert len(summaries(world)) > rows_before  # a fresh row, and the empty one stays history
    newest = [r for r in summaries(world) if r[0] == "palworld"][-1]
    assert newest[2] >= 1


async def test_the_empty_row_does_not_win_for_the_rest_of_the_day(
    make_world, monkeypatch, tmp_path
):
    world = await make_world(now=STARTED)
    await owed_digest_then_a_pass(world, monkeypatch, tmp_path)
    await run_guild_digest(world.bot.guild_digest_deps(), FRIEND, kind=RunKind.RUN_NOW, force=True)
    calls = len(world.llm.calls)

    # Preview again later the same evening: the fresh row is reused, not remade, not skipped.
    world.set_now(STARTED + timedelta(hours=2))
    await preview_guild_digest(world.bot.guild_digest_deps(), FRIEND)

    assert len(world.llm.calls) == calls


async def test_the_scheduled_digest_leaves_what_arrived_after_prepare_for_the_next_window(
    make_world,
):
    # Pinned semantics (plan 3.6): the summary ends where the prepare job read; an item
    # collected between then and the due instant is not in it, is not in this digest, and
    # is where the server's next summary starts.
    world = await make_world(now=DUE - timedelta(hours=1))
    add_item(world.db_path, "palworld", "early", DUE - timedelta(hours=2))
    await world.ready()
    world.set_now(DUE - timedelta(minutes=29))
    await world.bot._summaries_job()
    late = add_item(world.db_path, "palworld", "late", DUE - timedelta(minutes=10))
    calls = len(world.llm.calls)

    await world.tick(DUE)

    assert len(world.llm.calls) == calls  # the prepared summary was used as it stood
    assert late not in world.llm.urls_seen()
    (marks,) = world.rows(
        "SELECT game_items_upto FROM digests WHERE guild_id = ? AND run_date = '2026-10-01'",
        FRIEND,
    )
    assert marks[0] is not None
    world.set_now(DUE + timedelta(hours=24) - timedelta(minutes=29))
    await world.bot._summaries_job()
    assert late in world.llm.urls_seen()  # tomorrow's summary picks it up


async def test_startup_summaries_wait_for_the_first_pass(make_world, monkeypatch, tmp_path):
    world = await make_world(now=DUE - timedelta(minutes=20))  # 08:40, inside the lead
    add_item(world.db_path, "palworld", "early", DUE - timedelta(hours=2))
    collect_these(monkeypatch, tmp_path, [PASS_ITEM])
    await world.ready(hold=True)

    assert summaries(world) == []  # the startup run held off

    await world.bot._summaries_job()  # the five-minute job, still before the pass
    assert summaries(world) == []

    await world.bot._collection_job()
    await asyncio.gather(*world.bot._background_tasks)

    assert {r[0] for r in summaries(world)} >= {"palworld"}  # made right after the pass
    assert PASS_URL in world.llm.urls_seen()


@pytest.mark.parametrize("held", [True, False])
async def test_a_recent_pass_means_the_startup_run_goes_ahead(make_world, held):
    world = await make_world(now=DUE - timedelta(minutes=20))
    add_item(world.db_path, "palworld", "early", DUE - timedelta(hours=2))
    world.run(
        "INSERT INTO alert_state (key, value) VALUES ('last_sweep_at', ?)",
        (DUE - timedelta(minutes=40)).isoformat(),
    )

    await world.ready(hold=held)

    assert {r[0] for r in summaries(world)} >= {"palworld"}
