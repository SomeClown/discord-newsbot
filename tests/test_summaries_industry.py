"""The summaries job makes the industry pick for servers of any tier.

A game's summary is made for comped servers only. The industry entry is the
exception: a free server follows it too, and the pick (up to five stories, no
Brave search, its own prompt) is made for whoever follows it. These tests pin
the exception and, just as much, that the rule for games hasn't moved.

The rig is the summaries file's (`World`: a counting fake model, a real
database, a clock that only moves when told), pointed at the catalog with the
industry entry in it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_summaries import BER, BERLIN, BERLIN_DUE, FREE, PAC, World, ago

from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline import summaries
from newsbot.pipeline.industry import INDUSTRY_SYSTEM_PROMPT
from newsbot.pipeline.summaries import dry_run_lookup, ensure_summary, prepare_summaries
from newsbot.store import repo
from newsbot.store.models import StoredItem

FREE2 = 4


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")


@pytest.fixture
def brave_spy(monkeypatch):
    """Every game key `collect_web_search` was asked about; the search itself does nothing."""
    asked: list[list[str]] = []

    async def spy(deps, game_keys, api_key, *, sleep=None):
        asked.append(list(game_keys))
        raise AssertionError("the spy has no search to offer")

    monkeypatch.setattr(summaries, "collect_web_search", spy)
    return asked


def _industry_rows(world):
    return world.summaries(INDUSTRY_KEY)


def _game_rows(world):
    return world.rows("SELECT * FROM game_summaries WHERE game_key != ?", (INDUSTRY_KEY,))


# --- who gets a pick ---


async def test_only_free_servers_following_industry_make_one_industry_row_and_no_game_rows(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY, "palworld"])
    world.guild(FREE2, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    world.item("palworld", "raid boss patch", ago(3))

    done = await prepare_summaries(world.deps())

    assert done == [INDUSTRY_KEY]
    assert len(_industry_rows(world)) == 1
    assert _game_rows(world) == []
    assert world.llm.games() == [INDUSTRY_KEY]


async def test_two_free_servers_on_one_zone_and_time_share_one_call(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE2, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())
    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1
    assert len(_industry_rows(world)) == 1


async def test_free_servers_twelve_hours_apart_get_one_call_each(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY], time="09:00")
    world.guild(FREE2, "free", BERLIN, [INDUSTRY_KEY], time="21:00")
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())
    assert len(world.llm.calls) == 1  # 21:00 is hours away yet

    world.item(INDUSTRY_KEY, "publisher sold", BERLIN_DUE + timedelta(hours=11))
    world.clock.t = BERLIN_DUE + timedelta(hours=12) - timedelta(minutes=20)
    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 2
    assert len(_industry_rows(world)) == 2


async def test_a_comped_server_following_palworld_and_industry_gets_one_call_for_each(world):
    world.guild(PAC, "comped", BERLIN, ["palworld", INDUSTRY_KEY])
    world.item("palworld", "raid boss patch", ago(3))
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    done = await prepare_summaries(world.deps())

    assert sorted(done) == [INDUSTRY_KEY, "palworld"]
    assert sorted(world.llm.games()) == [INDUSTRY_KEY, "palworld"]
    assert len(_game_rows(world)) == 1
    assert len(_industry_rows(world)) == 1
    by_game = dict(zip(world.llm.games(), world.llm.calls, strict=True))
    game_system, _ = by_game["palworld"]
    industry_system, _ = by_game[INDUSTRY_KEY]
    # `_all_topics` leaves the industry entry out of a game's own prompt.
    assert "Gaming industry" not in game_system
    assert industry_system == INDUSTRY_SYSTEM_PROMPT


async def test_a_comped_server_following_industry_is_not_counted_twice(world):
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())

    assert len(world.llm.calls) == 1


async def test_a_server_that_does_not_follow_industry_gets_no_pick(world):
    world.guild(FREE, "free", BERLIN, ["palworld"])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == []


async def test_a_cycle_with_no_industry_items_makes_no_call(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])

    await prepare_summaries(world.deps())

    assert world.llm.calls == []
    [row] = _industry_rows(world)
    assert row["status"] == "ok"


# --- the pick's own prompt, and no search ---


async def test_the_industry_call_uses_the_industry_system_prompt(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())

    [(system, user)] = world.llm.calls
    assert system == INDUSTRY_SYSTEM_PROMPT
    assert "studio closes" in user


async def test_brave_is_never_asked_about_industry(world, brave_spy):
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = world.deps(web=True)
    try:
        await prepare_summaries(deps)
    finally:
        await world.http.aclose()

    assert brave_spy == []
    assert world.brave == []
    assert len(world.llm.calls) == 1


async def test_ensure_summary_drops_search_and_coverage_for_industry(world, brave_spy):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    deps = world.deps(web=True)
    try:
        got = await ensure_summary(
            deps,
            INDUSTRY_KEY,
            BERLIN_DUE,
            run_date=BERLIN_DUE.date(),
            search=True,
            coverage=("web search skipped",),
        )
    finally:
        await world.http.aclose()

    assert got is not None
    assert brave_spy == []
    [row] = _industry_rows(world)
    assert row["coverage_notes"] == "[]"


async def test_coverage_notes_stay_empty_with_web_search_configured_and_no_key(world):
    assert world.cfg.web_search is not None
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    await prepare_summaries(world.deps())  # no collection deps: a game would say "skipped"

    [row] = _industry_rows(world)
    assert row["coverage_notes"] == "[]"


async def test_a_comped_games_coverage_note_is_unchanged(world):
    world.guild(PAC, "comped", BERLIN, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))

    await prepare_summaries(world.deps())

    [row] = _game_rows(world)
    assert row["coverage_notes"] == '["web search skipped"]'


# --- the dry run ---


async def test_dry_run_lookup_makes_an_industry_pick_in_memory_and_saves_nothing(world):
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))

    got = await dry_run_lookup(world.deps())(INDUSTRY_KEY, BERLIN_DUE, None)

    assert got is not None
    assert got.status == "ok"
    assert [s.headline for s in got.stories] == [f"{INDUSTRY_KEY} story"]
    assert world.llm.calls[0][0] == INDUSTRY_SYSTEM_PROMPT
    assert world.summaries() == []


# --- sharing ---


def _summary_row(world, game_key):
    [row] = world.summaries(game_key)
    with world.conn() as conn:
        return repo.game_summary_by_id(conn, row["id"])


async def test_a_comped_servers_industry_row_is_shared_when_a_free_server_lines_up(world):
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, [INDUSTRY_KEY])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())

    summary = _summary_row(world, INDUSTRY_KEY)
    with world.conn() as conn:
        assert repo.summary_is_shared(conn, summary, PAC, BERLIN_DUE.date())


async def test_a_comped_servers_game_row_is_not_shared_with_a_free_server(world):
    world.guild(PAC, "comped", BERLIN, ["palworld"])
    world.guild(FREE, "free", BERLIN, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))
    await prepare_summaries(world.deps())

    summary = _summary_row(world, "palworld")
    with world.conn() as conn:
        assert not repo.summary_is_shared(conn, summary, PAC, BERLIN_DUE.date())


async def test_an_industry_row_with_no_other_follower_is_not_shared(world):
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, ["palworld"])
    world.item(INDUSTRY_KEY, "studio closes", ago(3))
    await prepare_summaries(world.deps())

    summary = _summary_row(world, INDUSTRY_KEY)
    with world.conn() as conn:
        assert not repo.summary_is_shared(conn, summary, PAC, BERLIN_DUE.date())


# --- the repo helpers ---


def test_industry_follows_lists_every_tier_in_comped_follows_order(world):
    world.guild(BER, "free", BERLIN, [INDUSTRY_KEY, "palworld"])
    world.guild(PAC, "comped", BERLIN, [INDUSTRY_KEY])
    world.guild(FREE, "free", BERLIN, ["palworld"])
    with world.conn() as conn:
        follows = repo.industry_follows(conn)

    assert [(f.guild_id, f.game_key) for f in follows] == [(PAC, INDUSTRY_KEY), (BER, INDUSTRY_KEY)]
    assert follows[1].digest_time == "09:00"
    assert follows[1].timezone == BERLIN


def test_industry_follows_leaves_out_servers_that_are_not_set_up(world):
    world.guild(BER, "free", BERLIN, [INDUSTRY_KEY])
    with world.conn() as conn:
        with conn:
            conn.execute("UPDATE guilds SET set_up = 0 WHERE guild_id = ?", (BER,))
        assert repo.industry_follows(conn) == []


def test_headline_items_by_url_returns_known_urls_and_skips_unknown_ones(world):
    when = ago(2)
    with world.conn() as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url=f"https://example.com/industry/{n}",
                    title=f"Headline {n}",
                    excerpt="",
                    source_name="Trade Press",
                    trust="press",
                    published_at=when,
                    topics={INDUSTRY_KEY: False},
                )
                for n in range(3)
            ],
            now=lambda: when,
        )
        got = repo.headline_items_by_url(
            conn, ["https://example.com/industry/1", "https://example.com/nope"]
        )

    assert list(got) == ["https://example.com/industry/1"]
    item = got["https://example.com/industry/1"]
    assert item.title == "Headline 1"
    assert item.source_name == "Trade Press"
    assert item.trust == "press"
    assert item.uncertain is False
    assert item.published_at == when


def test_headline_items_by_url_handles_no_urls_and_more_than_one_chunk(world):
    count = repo._SQLITE_VARIABLE_CHUNK + 5
    when = ago(2)
    urls = [f"https://example.com/bulk/{n}" for n in range(count)]
    with world.conn() as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url=url,
                    title=f"Bulk {n}",
                    excerpt="",
                    source_name="Feed",
                    trust="press",
                    published_at=when,
                    topics={INDUSTRY_KEY: False},
                )
                for n, url in enumerate(urls)
            ],
            now=lambda: when,
        )
        assert repo.headline_items_by_url(conn, []) == {}
        assert len(repo.headline_items_by_url(conn, urls)) == count
