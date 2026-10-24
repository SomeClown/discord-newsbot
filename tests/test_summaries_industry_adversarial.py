"""Adversarial round for Task 5: the industry pick made for servers of any tier.

The builder's file (`test_summaries_industry.py`) pins the happy paths. This one
counts model calls with the summaries file's fake (a spy that records every
(system, user) pair), and leans on the awkward neighbours: several cycles, a
comped server sharing a cycle with a free one, a DST change, unfollows, servers
that aren't set up, a failed pick and its retry, and the repo helpers at the
edges of their chunking.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_summaries import (
    BER,
    BERLIN,
    BERLIN_DUE,
    FREE,
    PAC,
    FakePublisher,
    World,
    _noop_notify,
    ago,
)

from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline import summaries
from newsbot.pipeline.guild_digest import GuildDigestDeps, run_guild_digest
from newsbot.pipeline.industry import INDUSTRY_SYSTEM_PROMPT
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import (
    _TIMEOUT_NOTE,
    dry_run_lookup,
    ensure_summary,
    prepare_summaries,
    retry_lookup,
    summary_lookup,
)
from newsbot.pipeline.summarize import LLMError, LLMFatalError, LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.models import StoredItem

FREE2, FREE3 = 4, 5
LONDON = "Europe/London"
NEW_YORK = "America/New_York"
LOS_ANGELES = "America/Los_Angeles"
MOSCOW = "Europe/Moscow"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")


@pytest.fixture
def brave_spy(monkeypatch):
    asked: list[list[str]] = []

    async def spy(deps, game_keys, api_key, *, sleep=None):
        asked.append(list(game_keys))
        raise AssertionError("the industry pick must never reach the search")

    monkeypatch.setattr(summaries, "collect_web_search", spy)
    return asked


def _industry(world):
    return world.summaries(INDUSTRY_KEY)


def _games(world):
    return world.rows("SELECT * FROM game_summaries WHERE game_key != ?", (INDUSTRY_KEY,))


def _items_in(user: str) -> list[dict]:
    return json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])


def _follow(world, guild_id, games):
    with world.conn() as conn:
        repo.set_guild_games(conn, guild_id, [(g, 100 + i) for i, g in enumerate(games)])


def _item_at(world, game, title, when, *, topics=None):
    with world.conn() as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url=f"https://example.com/{game}/{title.replace(' ', '-')}",
                    title=title,
                    excerpt="",
                    source_name="Feed",
                    trust="press",
                    published_at=when,
                    topics=topics or {game: False},
                )
            ],
            now=lambda: when,
        )


async def _window_of_passes(world, due, *, before=30, after=30, step=5):
    """A prepare pass every `step` minutes from `before` minutes ahead of `due` to `after` past."""
    for minutes in range(-before, after + 1, step):
        world.clock.t = due + timedelta(minutes=minutes)
        await prepare_summaries(world.deps())


# --- several cycles, DST, shared cycles ---


async def test_three_daily_cycles_across_the_berlin_dst_change_make_three_calls(world):
    # Berlin leaves summer time on 2026-10-25: 09:00 is 07:00 UTC on the 24th, 08:00 UTC after.
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    dues = [
        datetime(2026, 10, 24, 7, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
    ]
    for n, due in enumerate(dues):
        _item_at(world, INDUSTRY_KEY, f"day {n} news", due - timedelta(hours=3))
        await _window_of_passes(world, due)

    assert world.llm.games() == [INDUSTRY_KEY] * 3
    assert len(_industry(world)) == 3


async def test_a_pass_every_five_minutes_for_a_whole_day_makes_exactly_one_call(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    _item_at(world, INDUSTRY_KEY, "studio closes", ago(3))
    start = BERLIN_DUE - timedelta(hours=8)
    for step in range(0, 24 * 12, 1):
        world.clock.t = start + timedelta(minutes=5 * step)
        await prepare_summaries(world.deps())
        if world.clock.t > BERLIN_DUE + timedelta(hours=3):
            break

    assert len(world.llm.calls) == 1


async def test_two_zones_whose_digests_land_on_one_instant_share_a_call_across_the_dst_gap(world):
    # 2026-10-25: Berlin 09:00 CET and London 08:00 GMT are both 08:00 UTC.
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE2, "free", LONDON, [INDUSTRY_KEY], time="08:00")
    due = datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
    _item_at(world, INDUSTRY_KEY, "studio closes", due - timedelta(hours=3))

    await _window_of_passes(world, due)

    assert len(world.llm.calls) == 1


async def test_an_ambiguous_fall_back_hour_still_makes_one_call_for_the_day(world):
    # 02:30 on 2026-10-25 in Berlin happens twice (00:30 UTC and 01:30 UTC).
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY], time="02:30")
    _item_at(world, INDUSTRY_KEY, "studio closes", datetime(2026, 10, 24, 22, 0, tzinfo=UTC))
    start = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)
    for step in range(0, 12 * 4):
        world.clock.t = start + timedelta(minutes=5 * step)
        await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1


async def test_a_spring_forward_gap_time_still_makes_one_call_for_the_day(world):
    # 02:30 on 2026-03-29 does not exist in Berlin; zoneinfo lands it at 03:30 CEST (01:30 UTC).
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY], time="02:30")
    _item_at(world, INDUSTRY_KEY, "studio closes", datetime(2026, 3, 28, 22, 0, tzinfo=UTC))
    start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    for step in range(0, 12 * 4):
        world.clock.t = start + timedelta(minutes=5 * step)
        await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1


async def test_free_servers_two_hours_apart_share_a_call_and_nine_hours_apart_do_not(world):
    # Berlin 09:00 is 07:00 UTC, Moscow 12:00 is 09:00 UTC, Los Angeles 09:00 is 16:00 UTC.
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE2, "free", MOSCOW, [INDUSTRY_KEY], time="12:00")
    world.guild(FREE3, "free", LOS_ANGELES, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    world.item(INDUSTRY_KEY, "late news", BERLIN_DUE + timedelta(hours=8))

    for hours in (-0.5, 1.5, 8.5):
        world.clock.t = BERLIN_DUE + timedelta(hours=hours)
        await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 2


async def test_a_comped_and_a_free_server_sharing_a_cycle_cost_one_palworld_and_one_industry_call(
    world,
):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY, "palworld"])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())
    await prepare_summaries(world.deps())

    assert sorted(world.llm.games()) == [INDUSTRY_KEY, "palworld"]
    assert len(_games(world)) == 1
    assert len(_industry(world)) == 1


async def test_many_free_servers_on_one_cycle_cost_one_call(world):
    for n in range(12):
        world.guild(100 + n, "free", BERLIN, [INDUSTRY_KEY], time="09:00")
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())
    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1


async def test_a_comped_server_following_every_game_and_industry_makes_one_call_each(world):
    games = [g.key for g in world.cfg.catalog if g.key != INDUSTRY_KEY]
    world.guild(PAC, "comped", BERLIN, [*games, INDUSTRY_KEY])
    for key in [*games, INDUSTRY_KEY]:
        world.item(key, f"{key} news", ago(3))

    await prepare_summaries(world.deps())
    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == len(games) + 1
    assert sorted(world.llm.games()) == sorted([*games, INDUSTRY_KEY])


# --- who is asked, who is not ---


async def test_industry_items_with_no_follower_make_no_call_and_no_row(world):
    world.guild(FREE, "free", BERLIN, ["palworld"])
    world.guild(PAC, "comped", BERLIN, ["palworld"])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert world.llm.calls == []  # palworld has no items either
    assert _industry(world) == []


async def test_a_server_that_is_not_set_up_gets_no_pick(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    with world.conn() as conn, conn:
        conn.execute("UPDATE guilds SET set_up = 0 WHERE guild_id = ?", (FREE,))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == []


async def test_a_comped_server_that_is_not_set_up_gets_no_pick(world):
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    with world.conn() as conn, conn:
        conn.execute("UPDATE guilds SET set_up = 0 WHERE guild_id = ?", (PAC,))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == []


async def test_a_server_that_unfollows_industry_before_its_cycle_costs_nothing(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY, "palworld"])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    _follow(world, FREE, ["palworld"])

    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == []


async def test_a_server_that_unfollows_after_the_pick_leaves_the_row_and_makes_no_new_call(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())
    _follow(world, FREE, ["palworld"])
    world.item(INDUSTRY_KEY, "publisher sold", ago(2))
    world.clock.t = BERLIN_DUE + timedelta(minutes=10)

    assert await prepare_summaries(world.deps()) == []

    assert len(world.llm.calls) == 1
    assert len(_industry(world)) == 1


async def test_the_other_follower_still_gets_the_pick_when_one_unfollows_mid_cycle(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE2, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    _follow(world, FREE, [])

    assert await prepare_summaries(world.deps()) == [INDUSTRY_KEY]
    assert len(world.llm.calls) == 1


async def test_a_server_with_an_unusable_zone_is_skipped_without_hurting_the_others(world):
    world.guild(FREE, "free", "Mars/Olympus_Mons", [INDUSTRY_KEY])
    world.guild(FREE2, "free", "", [INDUSTRY_KEY])
    world.guild(FREE3, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    assert await prepare_summaries(world.deps()) == [INDUSTRY_KEY]
    assert len(world.llm.calls) == 1


async def test_a_database_that_follows_industry_but_a_catalog_without_it_is_quiet(
    tmp_path, monkeypatch
):
    plain = World(tmp_path, monkeypatch)  # config_v3.yaml has no industry entry
    plain.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    plain.item(INDUSTRY_KEY, "studio closes", ago(3))

    assert await prepare_summaries(plain.deps()) == []
    assert plain.llm.calls == []
    assert (
        await ensure_summary(plain.deps(), INDUSTRY_KEY, BERLIN_DUE, run_date=BERLIN_DUE.date())
        is None
    )


async def test_a_server_following_only_industry_gets_one_call_and_no_game_rows(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    world.item("palworld", "raid boss patch", ago(3))

    await prepare_summaries(world.deps())

    assert world.llm.games() == [INDUSTRY_KEY]
    assert _games(world) == []


# --- what the model is told ---


async def test_a_comped_game_call_is_byte_identical_to_what_it_was_without_industry(world):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())

    palworld = next(g for g in world.cfg.catalog if g.key == "palworld")
    expected_system, _ = build_prompt(
        palworld, [], [], all_topics=[palworld], subject=world.cfg.ai.subject
    )
    by_game = dict(zip(world.llm.games(), world.llm.calls, strict=True))
    assert by_game["palworld"][0] == expected_system
    assert "Gaming industry" not in by_game["palworld"][0]
    assert "studio closes" not in by_game["palworld"][1]
    assert by_game[INDUSTRY_KEY][0] == INDUSTRY_SYSTEM_PROMPT
    assert "raid boss patch" not in by_game[INDUSTRY_KEY][1]


async def test_a_comped_game_lists_only_followed_games_even_when_a_free_server_follows_more(world):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, ["borderlands4", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))

    await prepare_summaries(world.deps())

    [(system, _)] = world.llm.calls
    palworld = next(g for g in world.cfg.catalog if g.key == "palworld")
    assert (
        system
        == build_prompt(palworld, [], [], all_topics=[palworld], subject=world.cfg.ai.subject)[0]
    )


async def test_an_item_stored_for_a_game_and_for_industry_reaches_both_calls(world):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    _item_at(
        world,
        "palworld",
        "studio behind palworld closes",
        ago(3),
        topics={"palworld": False, INDUSTRY_KEY: False},
    )

    await prepare_summaries(world.deps())

    assert sorted(world.llm.games()) == [INDUSTRY_KEY, "palworld"]
    for _, user in world.llm.calls:
        assert "studio behind palworld closes" in user


async def test_the_industry_call_is_fed_no_more_than_the_per_game_item_cap(world):
    cap = world.cfg.collection.max_items_per_game
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    for n in range(cap + 15):
        world.item(INDUSTRY_KEY, f"story {n}", ago(3) + timedelta(seconds=n))

    await prepare_summaries(world.deps())

    [(_, user)] = world.llm.calls
    assert len(_items_in(user)) == cap


async def test_the_second_days_pick_is_told_the_first_days_headlines(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())
    # The first day's digest has to go out for the server to have been shown the pick: a
    # cycle's prior stories are the ones its servers were shown (`repo.chain_headlines`).
    world.clock.t = BERLIN_DUE
    digest_deps = GuildDigestDeps(
        cfg=world.cfg,
        db_path=world.db_path,
        now=world.clock,
        publisher_for=lambda *a: FakePublisher(),
        notify_guild=_noop_notify,
        summary_for=summary_lookup(world.deps()),
        sleep=world.sleep,
        pace_s=0,
    )
    await run_guild_digest(digest_deps, FREE, kind=RunKind.SCHEDULED)
    next_due = BERLIN_DUE + timedelta(days=1)
    world.item(INDUSTRY_KEY, "publisher sold", next_due - timedelta(hours=3))
    world.clock.t = next_due - timedelta(minutes=20)

    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 2
    second_user = world.llm.calls[1][1]
    assert "<prior_stories>" in second_user
    assert f"{INDUSTRY_KEY} story" in second_user.split("<items>")[0]


# --- no search, no coverage notes ---


async def test_brave_is_not_asked_about_industry_when_a_comped_server_also_follows_a_game(
    world, brave_spy
):
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = world.deps(web=True)
    try:
        await prepare_summaries(deps)
    finally:
        await world.http.aclose()

    assert brave_spy == []
    assert world.brave == []


async def test_a_games_search_still_runs_beside_an_industry_pick_and_never_names_industry(world):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = world.deps(web=True)
    try:
        await prepare_summaries(deps)
    finally:
        await world.http.aclose()

    assert world.brave, "the game should still be searched"
    assert not any("industry" in q.lower() for q in world.brave)
    [game_row] = _games(world)
    assert game_row["coverage_notes"] == "[]"
    [industry_row] = _industry(world)
    assert industry_row["coverage_notes"] == "[]"


@pytest.mark.parametrize("status", [429, 500])
async def test_a_failing_search_never_leaves_a_coverage_note_on_the_industry_row(world, status):
    world.brave_status = status
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = world.deps(web=True)
    try:
        await prepare_summaries(deps)
    finally:
        await world.http.aclose()

    [industry_row] = _industry(world)
    assert industry_row["coverage_notes"] == "[]"


@pytest.mark.parametrize("lookup", [summary_lookup, retry_lookup])
async def test_the_digest_lookups_leave_no_search_skipped_note_on_the_industry_row(world, lookup):
    assert world.cfg.web_search is not None
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    got = await lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE)

    assert got is not None
    assert got.coverage_notes == []
    [row] = _industry(world)
    assert row["coverage_notes"] == "[]"


async def test_asking_ensure_summary_to_search_on_a_game_still_does(world):
    world.guild(PAC, "comped", BERLIN, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))
    deps = world.deps(web=True)
    try:
        await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=BERLIN_DUE.date(), search=True)
    finally:
        await world.http.aclose()

    assert world.brave


# --- what is saved ---


class _ManyStories:
    """A model that finds eight stories, one per item it was shown, most significant first."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def emit_stories(self, system, user):
        self.calls.append((system, user))
        items = _items_in(user)
        stories = [
            StoryOut(
                headline=f"headline {n}",
                summary="What happened.",
                label="reported",
                item_urls=[item["url"]],
                relevant=True,
            )
            for n, item in enumerate(items[:8])
        ]
        return LLMResult(StoriesOut(stories=stories), 10, 5)


async def test_the_saved_pick_is_cut_to_five_in_the_models_order(world):
    llm = _ManyStories()
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    for n in range(8):
        world.item(INDUSTRY_KEY, f"story {n}", ago(3) + timedelta(seconds=n))

    await prepare_summaries(world.deps(llm=llm))

    got = await summary_lookup(world.deps(llm=llm))(INDUSTRY_KEY, BERLIN_DUE)
    assert got is not None
    assert [s.headline for s in got.stories] == [f"headline {n}" for n in range(5)]
    assert len(llm.calls) == 1


async def test_every_url_in_a_saved_pick_resolves_to_a_stored_item(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())

    got = await summary_lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE)

    assert got is not None and got.stories
    urls = [u for story in got.stories for u in story.item_urls]
    with world.conn() as conn:
        found = repo.headline_items_by_url(conn, urls)
    assert set(found) == set(urls)
    assert found[urls[0]].title == "studio closes"


# --- failure, retry, reuse ---


async def test_a_failed_pick_is_a_fallback_row_with_three_attempts_and_no_coverage_note(world):
    world.llm.fail = {INDUSTRY_KEY: LLMError("boom", input_tokens=7, output_tokens=2)}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 3
    [row] = _industry(world)
    assert row["status"] == "fallback"
    assert row["coverage_notes"] == "[]"
    assert row["input_tokens"] >= 21
    assert world.alerts == ["newsbot: summary for Gaming industry fell back to headlines"]


async def test_a_fatal_pick_error_is_one_attempt(world):
    world.llm.fail = {INDUSTRY_KEY: LLMFatalError("400")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1
    assert _industry(world)[0]["status"] == "fallback"


async def test_an_unexpected_error_in_the_pick_becomes_a_fallback_not_a_crash(world):
    world.llm.fail = {INDUSTRY_KEY: RuntimeError("never met this one")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(PAC, "comped", BERLIN, ["palworld"])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    world.item("palworld", "raid boss patch", ago(3))

    done = await prepare_summaries(world.deps())

    assert sorted(done) == [INDUSTRY_KEY, "palworld"]
    assert _industry(world)[0]["status"] == "fallback"
    assert _games(world)[0]["status"] == "ok"


async def test_a_hung_pick_times_out_into_a_fallback_and_does_not_block_the_games(world):
    class HangsOnIndustry:
        def __init__(self, inner):
            self.inner = inner
            self.calls = inner.calls

        async def emit_stories(self, system, user):
            if system == INDUSTRY_SYSTEM_PROMPT:
                self.calls.append((system, user))
                await asyncio.sleep(3600)
            return await self.inner.emit_stories(system, user)

    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps(llm=HangsOnIndustry(world.llm), summarize_timeout_s=0.05))

    [row] = _industry(world)
    assert row["status"] == "fallback"
    assert row["note"] == _TIMEOUT_NOTE
    assert _games(world)[0]["status"] == "ok"


async def test_a_second_pass_reuses_the_stored_pick_even_when_it_is_a_fallback(world):
    world.llm.fail = {INDUSTRY_KEY: LLMFatalError("400")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())
    world.clock.t = BERLIN_DUE
    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1
    assert len(_industry(world)) == 1


async def test_a_confirmed_run_now_retry_remakes_a_failed_pick_once_and_replaces_the_row(world):
    world.llm.fail = {INDUSTRY_KEY: LLMFatalError("400")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())
    world.llm.fail = {}

    got = await retry_lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE)

    assert got is not None and got.status == "ok"
    assert len(world.llm.calls) == 2
    assert world.llm.calls[1][0] == INDUSTRY_SYSTEM_PROMPT
    [row] = _industry(world)
    assert row["status"] == "ok"
    assert row["coverage_notes"] == "[]"
    # And the ok row is now just reused.
    again = await retry_lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE)
    assert again is not None and len(world.llm.calls) == 2


async def test_a_retry_that_fails_again_stays_one_fallback_row(world):
    world.llm.fail = {INDUSTRY_KEY: LLMFatalError("400")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())

    got = await retry_lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE)

    assert got is not None and got.status == "fallback"
    assert len(world.llm.calls) == 2
    assert len(_industry(world)) == 1


async def test_racing_callers_for_one_industry_cycle_make_one_call(world):
    world.llm.yields = 5
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE2, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    world.clock.t = BERLIN_DUE
    deps = world.deps()

    results = await asyncio.gather(
        *(
            ensure_summary(deps, INDUSTRY_KEY, BERLIN_DUE, run_date=BERLIN_DUE.date())
            for _ in range(5)
        ),
        prepare_summaries(deps),
    )

    assert len(world.llm.calls) == 1
    assert all(r is not None for r in results[:5])
    assert len(_industry(world)) == 1


async def test_an_empty_cycle_saves_an_ok_row_and_the_next_pass_still_makes_no_call(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))

    await prepare_summaries(world.deps())
    world.clock.t = BERLIN_DUE
    await prepare_summaries(world.deps())

    assert world.llm.calls == []
    [row] = _industry(world)
    assert row["status"] == "ok"
    assert row["coverage_notes"] == "[]"


# --- the dry run ---


def _everything_stored(world):
    tables = ("game_summaries", "stories", "story_items", "app_state", "items")
    with world.conn() as conn:
        return {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
            for t in tables
        }


async def test_dry_run_lookup_for_industry_writes_nothing_anywhere_and_never_searches(
    world, brave_spy
):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    before = _everything_stored(world)
    deps = world.deps(web=True)
    try:
        got = await dry_run_lookup(deps)(INDUSTRY_KEY, BERLIN_DUE, None)
    finally:
        await world.http.aclose()

    assert got is not None and got.status == "ok"
    assert got.coverage_notes == []
    assert brave_spy == []
    assert _everything_stored(world) == before


async def test_dry_run_lookup_keeps_an_industry_pick_in_memory_for_a_second_preview(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    lookup = dry_run_lookup(world.deps())

    first = await lookup(INDUSTRY_KEY, BERLIN_DUE, None)
    second = await lookup(INDUSTRY_KEY, BERLIN_DUE, None)

    assert first is second
    assert len(world.llm.calls) == 1
    assert world.summaries() == []


async def test_dry_run_lookup_does_not_keep_a_failed_industry_pick(world):
    world.llm.fail = {INDUSTRY_KEY: LLMFatalError("400")}
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    lookup = dry_run_lookup(world.deps())

    first = await lookup(INDUSTRY_KEY, BERLIN_DUE, None)
    world.llm.fail = {}
    second = await lookup(INDUSTRY_KEY, BERLIN_DUE, None)

    assert first is not None and first.status == "fallback"
    assert second is not None and second.status == "ok"
    assert world.summaries() == []


async def test_dry_run_lookup_reuses_a_stored_industry_pick_without_a_call(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())

    got = await dry_run_lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE, None)

    assert got is not None and got.status == "ok"
    assert len(world.llm.calls) == 1


# --- summary_is_shared for every tier mix ---


@pytest.mark.parametrize("asker_tier", ["comped", "free"])
@pytest.mark.parametrize("other_tier", ["comped", "free"])
@pytest.mark.parametrize("other_follows", [True, False])
@pytest.mark.parametrize("other_set_up", [True, False])
@pytest.mark.parametrize("game_key", [INDUSTRY_KEY, "palworld"])
async def test_summary_is_shared_follows_the_tier_rule_for_every_mix(
    world, asker_tier, other_tier, other_follows, other_set_up, game_key
):
    world.guild(PAC, asker_tier, BERLIN, [game_key])
    world.guild(BER, other_tier, BERLIN, [game_key] if other_follows else ["borderlands4"])
    if not other_set_up:
        with world.conn() as conn, conn:
            conn.execute("UPDATE guilds SET set_up = 0 WHERE guild_id = ?", (BER,))
    world.item(game_key, "something happened", ago(3))
    made = await ensure_summary(world.deps(), game_key, BERLIN_DUE, run_date=BERLIN_DUE.date())
    assert made is not None
    [row] = world.summaries(game_key)
    with world.conn() as conn:
        summary = repo.game_summary_by_id(conn, row["id"])
        shared = repo.summary_is_shared(conn, summary, PAC, BERLIN_DUE.date())

    other_counts = other_follows and other_set_up
    if game_key != INDUSTRY_KEY:
        other_counts = other_counts and other_tier == "comped"
    assert shared is other_counts


async def test_an_industry_row_is_not_shared_with_the_asking_server_itself(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())
    [row] = _industry(world)
    with world.conn() as conn:
        summary = repo.game_summary_by_id(conn, row["id"])
        assert not repo.summary_is_shared(conn, summary, FREE, BERLIN_DUE.date())


async def test_an_industry_row_is_shared_with_two_free_servers_from_either_side(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE2, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())
    [row] = _industry(world)
    with world.conn() as conn:
        summary = repo.game_summary_by_id(conn, row["id"])
        assert repo.summary_is_shared(conn, summary, FREE, BERLIN_DUE.date())
        assert repo.summary_is_shared(conn, summary, FREE2, BERLIN_DUE.date())


# --- industry_follows ---


def test_industry_follows_gives_each_server_once_even_when_it_follows_many_games(world):
    world.guild(FREE, "free", BERLIN, ["palworld", INDUSTRY_KEY, "borderlands4"])
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    with world.conn() as conn:
        follows = repo.industry_follows(conn)

    assert [(f.guild_id, f.game_key) for f in follows] == [
        (PAC, INDUSTRY_KEY),
        (FREE, INDUSTRY_KEY),
    ]


def test_industry_follows_is_empty_when_nobody_follows(world):
    world.guild(FREE, "free", BERLIN, ["palworld"])
    world.guild(PAC, "comped", BERLIN, ["palworld"])
    with world.conn() as conn:
        assert repo.industry_follows(conn) == []


def test_follows_sync_has_each_industry_pair_once_and_other_comped_games_unchanged(world):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY, "palworld"])
    got = summaries._follows_sync(world.db_path)

    assert sorted((f.guild_id, f.game_key) for f in got) == [
        (PAC, INDUSTRY_KEY),
        (PAC, "palworld"),
        (FREE, INDUSTRY_KEY),
    ]


# --- headline_items_by_url ---


def _store(world, urls, *, topics=None, published=None):
    when = ago(2)
    with world.conn() as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url=url,
                    title=f"Title of {url}",
                    excerpt="",
                    source_name="Feed",
                    trust="press",
                    published_at=published,
                    topics=topics or {INDUSTRY_KEY: False},
                )
                for url in urls
            ],
            now=lambda: when,
        )


@pytest.mark.parametrize("known", [1, 899, 900, 901, 1800, 1801])
def test_headline_items_by_url_returns_every_known_url_at_each_chunk_boundary(world, known):
    chunk = repo._SQLITE_VARIABLE_CHUNK
    assert chunk == 900
    urls = [f"https://example.com/k/{n}" for n in range(known)]
    _store(world, urls)
    unknown = [f"https://example.com/u/{n}" for n in range(chunk + 3)]
    asked = [u for pair in zip(urls, unknown, strict=False) for u in pair] + unknown[len(urls) :]
    asked += urls[len(unknown) :]

    with world.conn() as conn:
        got = repo.headline_items_by_url(conn, asked)

    assert set(got) == set(urls)
    assert all(got[u].title == f"Title of {u}" for u in urls)


def test_headline_items_by_url_collapses_duplicates_across_chunks(world):
    _store(world, ["https://example.com/one"])
    asked = ["https://example.com/one"] * 2500

    with world.conn() as conn:
        got = repo.headline_items_by_url(conn, asked)

    assert list(got) == ["https://example.com/one"]


def test_headline_items_by_url_takes_a_generator_and_a_set(world):
    urls = [f"https://example.com/g/{n}" for n in range(1000)]
    _store(world, urls)

    with world.conn() as conn:
        from_generator = repo.headline_items_by_url(conn, (u for u in urls))
        from_set = repo.headline_items_by_url(conn, set(urls))

    assert set(from_generator) == set(urls) == set(from_set)


def test_headline_items_by_url_skips_purged_urls(world):
    _store(world, ["https://example.com/old", "https://example.com/new"])
    with world.conn() as conn:
        with conn:
            conn.execute(
                "UPDATE items SET collected_at = ? WHERE url = ?",
                ("2020-01-01T00:00:00+00:00", "https://example.com/old"),
            )
        repo.purge_older_than(conn, datetime(2021, 1, 1, tzinfo=UTC))
        got = repo.headline_items_by_url(
            conn, ["https://example.com/old", "https://example.com/new"]
        )

    assert list(got) == ["https://example.com/new"]


def test_headline_items_by_url_reads_an_item_with_no_publish_date(world):
    _store(world, ["https://example.com/undated"], published=None)

    with world.conn() as conn:
        got = repo.headline_items_by_url(conn, ["https://example.com/undated"])

    assert got["https://example.com/undated"].published_at is None


def test_headline_items_by_url_finds_an_item_stored_for_a_different_game(world):
    _store(world, ["https://example.com/pal"], topics={"palworld": True})

    with world.conn() as conn:
        got = repo.headline_items_by_url(conn, ["https://example.com/pal"])

    assert list(got) == ["https://example.com/pal"]


def test_headline_items_by_url_does_not_match_a_lookalike_url(world):
    _store(world, ["https://example.com/a"])

    with world.conn() as conn:
        got = repo.headline_items_by_url(
            conn,
            ["https://example.com/a/", "https://example.com/A", "https://example.com/a%", "%", "_"],
        )

    assert got == {}


def test_headline_items_by_url_survives_a_quote_in_a_url(world):
    _store(world, ["https://example.com/it's"])

    with world.conn() as conn:
        got = repo.headline_items_by_url(
            conn, ["https://example.com/it's", "x'); DROP TABLE items;"]
        )
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    assert list(got) == ["https://example.com/it's"]
