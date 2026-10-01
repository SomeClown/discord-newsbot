"""`/newsbot preview` for a comped server, and what it does and doesn't leave behind.

The plan says a preview "writes nothing; comped guilds use a reusable summary or compute one
in memory". The CLI's `--dry-run` did that (`dry_run_lookup`); the bot's preview used to run
the shared saving lookup instead, so an admin of a comped server (which today means the
friend) typing `/newsbot preview` at 08:30 left a `game_summaries` row behind, and the 09:00
digest, whose reuse rule is "newer than my last digest, no more than six hours older than my
due time", cheerfully reused it. If the preview had run before the prepare job's half-hour
lead it hadn't searched the web, and if it had hit the model on a bad minute it saved a
`fallback` that the reuse rule treated as an answer. Either one reached the friend's channel
at 09:00 as a worse digest than they'd have had if nobody had looked.

Now the bot's preview uses `dry_run_lookup` too. These tests pin the trade-off that bought:
a preview reuses what the prepare job stored, makes the rest in memory, saves nothing, and
so costs a model call of its own every time. Previews are rare and owner-driven; that's fine.
"""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

from cutover_world import BL4_CH, DUE, FRIEND, PAL_CH, add_item

import newsbot.pipeline.summaries as summaries_module
from newsbot.pipeline.guild_digest import preview_guild_digest
from newsbot.store.db import connect

GAMES = ("borderlands4", "palworld")


def dump(world) -> list[str]:
    with closing(connect(world.db_path)) as conn:
        return list(conn.iterdump())


async def morning(make_world, **kwargs):
    """The friend's world on the morning of the first of October, with news collected at 07:00."""
    world = await make_world(now=DUE - timedelta(hours=1), **kwargs)
    for game in GAMES:
        add_item(world.db_path, game, "news", DUE - timedelta(hours=2))
    await world.ready()
    return world


async def preview(world, at):
    world.set_now(at)
    return await preview_guild_digest(world.bot.guild_digest_deps(), FRIEND)


async def prepare_and_post(world):
    """The 08:31 prepare tick and the 09:00 minute tick, the way the scheduler would run them."""
    world.set_now(DUE - timedelta(minutes=29))
    await world.bot._summaries_job()
    await world.tick(DUE)


def digest_status(world) -> str:
    return world.rows(
        "SELECT status FROM digests WHERE guild_id = ? AND run_date = '2026-10-01'", FRIEND
    )[0][0]


# --- what a preview costs ---


async def test_a_preview_at_0830_costs_a_call_of_its_own(make_world):
    # The accepted trade-off (plan section 3.7): a preview saves nothing, so the digest can't
    # reuse it and makes its own calls. Previews are rare and owner-driven; a saved summary
    # that quietly decided the morning's digest was the worse deal.
    world = await morning(make_world)

    shown = await preview(world, DUE - timedelta(minutes=30))
    await prepare_and_post(world)

    assert shown is not None
    assert len(world.llm.calls) == 2 * len(GAMES)  # the preview's per game, then the digest's
    assert len(world.sent(BL4_CH)) == 1 and len(world.sent(PAL_CH)) == 1


async def test_without_a_preview_the_same_morning_makes_the_same_calls(make_world):
    world = await morning(make_world)

    await prepare_and_post(world)

    assert len(world.llm.calls) == len(GAMES)


async def test_every_preview_costs_a_call_because_none_of_them_saves_anything(make_world):
    world = await morning(make_world)
    await preview(world, DUE - timedelta(minutes=30))
    calls = len(world.llm.calls)

    await preview(world, DUE - timedelta(minutes=20))
    await preview(world, DUE - timedelta(minutes=10))

    assert len(world.llm.calls) == calls + 2 * len(GAMES)


async def test_a_preview_hours_early_leaves_nothing_for_the_digest_to_reuse(make_world):
    world = await morning(make_world)
    for game in GAMES:  # news that was already there at 02:00, so the preview has something to say
        add_item(world.db_path, game, "early", DUE - timedelta(hours=8))

    await preview(world, DUE - timedelta(hours=7))
    await prepare_and_post(world)

    assert len(world.llm.calls) == 2 * len(GAMES)


async def test_a_preview_inside_six_hours_no_longer_delays_the_mornings_news(make_world):
    # Used to be the other way round: the summary a 04:00 preview saved ended at 04:00, the
    # 09:00 digest reused it, and what was collected in between waited a day. A preview that
    # saves nothing can't do that.
    world = await morning(make_world)
    for game in GAMES:  # what there was to say at 05:00
        add_item(world.db_path, game, "early", DUE - timedelta(hours=6))
    late = add_item(world.db_path, "palworld", "late", DUE - timedelta(hours=1, minutes=30))

    await preview(world, DUE - timedelta(hours=4))
    await prepare_and_post(world)

    assert late in world.llm.urls_seen()  # the 09:00 digest saw the 07:30 item


async def test_a_preview_for_a_free_server_costs_nothing_and_writes_nothing(make_world):
    world = await morning(make_world)
    world.run("UPDATE guilds SET tier = 'free'")
    before = dump(world)

    shown = await preview(world, DUE - timedelta(minutes=30))

    assert shown is not None and world.llm.calls == []
    assert dump(world) == before


async def test_a_preview_that_only_reuses_a_summary_writes_nothing(make_world):
    world = await morning(make_world)
    world.set_now(DUE - timedelta(minutes=29))
    await world.bot._summaries_job()  # the real prepare job makes the summaries
    before = dump(world)

    await preview(world, DUE - timedelta(minutes=10))

    assert dump(world) == before


# --- where looking changes what's posted ---


async def test_a_preview_writes_nothing_even_when_it_has_to_make_the_summary(make_world):
    world = await morning(make_world)
    before = dump(world)

    await preview(world, DUE - timedelta(minutes=30))

    assert dump(world) == before


async def _with_web_search(make_world, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    searched: list[list[str]] = []

    async def fake_search(deps, game_keys, key, *, sleep):
        searched.append(list(game_keys))
        return SimpleNamespace(sources_total=1)

    monkeypatch.setattr(summaries_module, "collect_web_search", fake_search)
    world = await morning(make_world, brave_key="test-key")
    return world, searched


async def test_without_a_preview_the_prepare_job_searches_and_the_digest_is_clean(
    make_world, monkeypatch
):
    world, searched = await _with_web_search(make_world, monkeypatch)

    await prepare_and_post(world)

    assert [g for batch in searched for g in batch]  # the prepare job did search
    assert digest_status(world) == "ok"


async def test_a_preview_before_the_prepare_window_does_not_cost_the_digest_its_search(
    make_world, monkeypatch
):
    world, searched = await _with_web_search(make_world, monkeypatch)

    await preview(world, DUE - timedelta(hours=1))
    await prepare_and_post(world)

    assert [g for batch in searched for g in batch]
    assert digest_status(world) == "ok"


async def test_a_preview_that_hit_a_failing_model_does_not_decide_the_mornings_summary(
    make_world, monkeypatch
):
    # With a Brave key and a (fake) search, so "ok" is reachable at all: without one the prepare
    # job records "web search skipped" for every digest and the status is always partial.
    world, _searched = await _with_web_search(make_world, monkeypatch)

    world.llm.fail = True
    await preview(world, DUE - timedelta(minutes=45))
    world.llm.fail = False
    await prepare_and_post(world)

    assert digest_status(world) == "ok"
    embed = world.sent(PAL_CH)[0].embed
    assert "Summary unavailable" not in (embed.description or "")
