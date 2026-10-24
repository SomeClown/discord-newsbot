"""Adversarial round for the industry entry's isolation (Task 2 of the industry-news plan).

`test_industry_isolation.py` pins the happy paths. This file leans on the fence:
odd casing and Unicode in headlines, items tagged for two topics, forged configs
that skipped `load_config`'s refusals, Brave asked for the entry in every
combination, and every subset of followed games walking through `_all_topics`
and `build_prompt`. Nothing here touches the network; Brave and the feeds are
`httpx.MockTransport`.
"""

from __future__ import annotations

import itertools
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_collect import make_deps
from test_summaries import PACIFIC_DUE, World, ago
from v3_fakes import load_v3_config, load_v3_industry_config

from newsbot.collectors.base import RawItem, build_catalog_collectors
from newsbot.config import AppConfig, Secrets
from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline.collect import CollectionOutcome, collect_web_search
from newsbot.pipeline.filter import TopicMatcher, build_matchers, filter_items
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.run import run_check_sources
from newsbot.pipeline.summaries import SummaryDeps, _all_topics, prepare_summaries
from newsbot.store.db import connect, migrate

GAMES = ["borderlands4", "palworld", "rust"]
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


@pytest.fixture
def cfg(monkeypatch) -> AppConfig:
    return load_v3_industry_config(monkeypatch)


def _secrets(brave: str | None = "k") -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key="x",
        brave_api_key=brave,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _item(title: str, *, excerpt: str = "", topics=None, url: str | None = None) -> RawItem:
    return RawItem(
        url=url or f"https://example.com/{abs(hash((title, excerpt, topics)))}",
        title=title,
        excerpt=excerpt,
        source_name="Some Source",
        trust="press",
        published_at=NOW,
        topics=topics,
    )


def _forged(cfg: AppConfig, **update) -> AppConfig:
    """A config whose industry entry has terms, built past `load_config`'s checks."""
    catalog = [g.model_copy(update=update) if g.key == INDUSTRY_KEY else g for g in cfg.catalog]
    return cfg.model_copy(update={"catalog": catalog})


def _brave_collectors(collectors) -> list:
    return [c for c in collectors if type(c).__name__ == "WebSearchCollector"]


def _brave_keys(collectors) -> list[list[str]]:
    return [[g.key for g in c._topics] for c in _brave_collectors(collectors)]


def _db(tmp_path) -> str:
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


# --- keyword matching: headlines that say the quiet part out loud ---

INDUSTRY_WORDING = [
    "Gaming industry news",
    "GAMING INDUSTRY NEWS",
    "gaming   industry news",
    "The industry is changing",
    "INDUSTRY",
    "industry",
    "Industry-wide layoffs",
    "Gaming İndustry",  # dotted capital I
    "Gaming ınduſtry",  # dotless i and long s
    "Ｇａｍｉｎｇ ｉｎｄｕｓｔｒｙ",  # full-width
    "Gaming industry",  # no-break space
    "Gaming​industry",  # zero-width space
    "gaming-industry",
    "Gaming Industry: key=industry",
]


@pytest.mark.parametrize("wording", INDUSTRY_WORDING)
def test_industry_wording_in_a_general_title_never_tags_industry(cfg, wording):
    grouped = filter_items([_item(wording)], cfg.catalog, 10)
    assert INDUSTRY_KEY not in grouped


@pytest.mark.parametrize("wording", INDUSTRY_WORDING)
def test_industry_wording_in_a_general_excerpt_never_tags_industry(cfg, wording):
    grouped = filter_items([_item("A headline", excerpt=wording)], cfg.catalog, 10)
    assert INDUSTRY_KEY not in grouped


@pytest.mark.parametrize("wording", INDUSTRY_WORDING)
def test_a_forged_industry_that_matches_by_name_still_never_keyword_matches(cfg, wording):
    forged = _forged(
        cfg,
        name="Gaming industry",
        match_name=True,
        aliases=["industry", "Gaming industry", "layoffs", "studio"],
        entities=["industry", "Pocketpair", "studio"],
    )
    grouped = filter_items(
        [_item(wording), _item("x", excerpt=wording), _item("Studio layoffs")], forged.catalog, 10
    )
    assert INDUSTRY_KEY not in grouped


def test_a_forged_industry_with_every_term_shape_gets_an_inert_matcher(cfg):
    forged = _forged(cfg, match_name=True, aliases=["a"], entities=["b"], name="Gaming industry")
    assert build_matchers(forged.catalog)[INDUSTRY_KEY] == TopicMatcher(None, None)


def test_the_industry_matcher_is_inert_wherever_the_entry_sits_in_the_catalog(cfg):
    forged = _forged(cfg, match_name=True, aliases=["layoffs"], entities=["studio"])
    reordered = [g for g in forged.catalog if g.key == INDUSTRY_KEY] + [
        g for g in forged.catalog if g.key != INDUSTRY_KEY
    ]
    assert build_matchers(reordered)[INDUSTRY_KEY] == TopicMatcher(None, None)
    grouped = filter_items([_item("Studio layoffs")], reordered, 10)
    assert INDUSTRY_KEY not in grouped


def test_the_other_games_matchers_are_identical_with_and_without_industry_in_the_catalog(
    monkeypatch,
):
    plain = load_v3_config(monkeypatch)
    withit = load_v3_industry_config(monkeypatch)
    before = build_matchers(plain.catalog)
    after = build_matchers(withit.catalog)
    assert set(after) - set(before) == {INDUSTRY_KEY}
    for key, matcher in before.items():
        assert after[key] == matcher


def test_other_games_grouping_is_identical_with_and_without_industry_in_the_catalog(monkeypatch):
    plain = load_v3_config(monkeypatch)
    withit = load_v3_industry_config(monkeypatch)
    items = [
        *(_item(w) for w in INDUSTRY_WORDING),
        _item("Palworld patch notes", url="https://example.com/pal"),
        _item("Borderlands 4 gaming industry hot take", url="https://example.com/bl4"),
        _item("Rust game update", url="https://example.com/rust"),
        _item("Pocketpair studio news", url="https://example.com/pp"),
        _item("dedicated", topics=("palworld",), url="https://example.com/ded"),
    ]
    before = filter_items(items, plain.catalog, 10)
    after = filter_items(items, withit.catalog, 10)
    assert set(after) <= set(before)
    assert before == after


def test_a_dedicated_industry_item_lands_only_under_industry(cfg):
    item = _item("Palworld Borderlands 4 Rust Gaming industry", topics=(INDUSTRY_KEY,))
    grouped = filter_items([item], cfg.catalog, 10)
    assert set(grouped) == {INDUSTRY_KEY}
    assert [t.uncertain for t in grouped[INDUSTRY_KEY]] == [False]


def test_a_dedicated_game_item_saying_industry_lands_only_under_that_game(cfg):
    item = _item("Gaming industry roundup", topics=("palworld",))
    grouped = filter_items([item], cfg.catalog, 10)
    assert set(grouped) == {"palworld"}


def test_a_dedicated_industry_item_is_dropped_when_the_catalog_has_no_industry(monkeypatch):
    plain = load_v3_config(monkeypatch)
    grouped = filter_items([_item("Studio closes", topics=(INDUSTRY_KEY,))], plain.catalog, 10)
    assert grouped == {}


def test_a_two_topic_item_with_industry_does_not_reach_industry_by_keyword(cfg):
    item = _item("Gaming industry layoffs hit Palworld studio", topics=(INDUSTRY_KEY, "palworld"))
    grouped = filter_items([item], cfg.catalog, 10)
    assert set(grouped) == {"palworld"}


def test_a_two_topic_item_with_industry_and_no_game_words_goes_nowhere(cfg):
    item = _item("Gaming industry layoffs", topics=(INDUSTRY_KEY, "palworld"))
    assert filter_items([item], cfg.catalog, 10) == {}


def test_the_cap_does_not_let_dedicated_industry_items_push_games_out(cfg):
    items = [_item(f"story {n}", topics=(INDUSTRY_KEY,)) for n in range(5)]
    items.append(_item("Palworld patch"))
    grouped = filter_items(items, cfg.catalog, 2)
    assert len(grouped[INDUSTRY_KEY]) == 2
    assert [t.item.title for t in grouped["palworld"]] == ["Palworld patch"]


# --- Brave ---


def test_brave_gets_only_the_games_when_industry_is_mixed_in_first(cfg):
    industry = [g for g in cfg.catalog if g.key == INDUSTRY_KEY]
    games = [g for g in cfg.catalog if g.key != INDUSTRY_KEY]
    built = build_catalog_collectors(cfg, _secrets(), web_search_games=industry + games)
    assert _brave_keys(built) == [GAMES]


def test_brave_keeps_the_callers_order_and_duplicates_but_drops_industry(cfg):
    by_key = {g.key: g for g in cfg.catalog}
    asked = [by_key["rust"], by_key[INDUSTRY_KEY], by_key["palworld"], by_key[INDUSTRY_KEY]]
    built = build_catalog_collectors(cfg, _secrets(), web_search_games=asked)
    assert _brave_keys(built) == [["rust", "palworld"]]


def test_brave_asked_for_industry_twice_builds_no_collector(cfg):
    industry = next(g for g in cfg.catalog if g.key == INDUSTRY_KEY)
    built = build_catalog_collectors(cfg, _secrets(), web_search_games=[industry, industry])
    assert _brave_keys(built) == []


def test_brave_accepts_a_tuple_of_games_with_industry(cfg):
    built = build_catalog_collectors(cfg, _secrets(), web_search_games=tuple(cfg.catalog))
    assert _brave_keys(built) == [GAMES]


def test_brave_is_not_built_at_all_without_a_key_even_for_games(cfg):
    built = build_catalog_collectors(cfg, _secrets(brave=None))
    assert _brave_keys(built) == []


def test_brave_is_not_built_when_web_search_is_switched_off_for_the_pass(cfg):
    built = build_catalog_collectors(cfg, _secrets(), include_web_search=False)
    assert _brave_keys(built) == []


async def test_a_forged_industry_with_search_queries_never_reaches_a_brave_request(cfg):
    forged = _forged(cfg, search_queries=["industry layoffs", "studio closure"], match_name=True)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"results": []})

    async def no_sleep(_s: float) -> None:
        return None

    (brave,) = _brave_collectors(build_catalog_collectors(forged, _secrets()))
    brave._sleep = no_sleep
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await brave.collect(client)
    assert seen
    assert not any("industry" in q.lower() or "studio closure" in q for q in seen)


async def test_check_sources_for_industry_alone_makes_no_brave_request(cfg, monkeypatch):
    brave_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            brave_hosts.append(str(request.url))
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, content=b"<rss version='2.0'><channel></channel></rss>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_check_sources(cfg, _secrets(), client, games=[INDUSTRY_KEY])
    assert brave_hosts == []
    assert INDUSTRY_KEY in report.topic_counts


async def test_check_sources_for_the_whole_catalog_never_searches_for_industry(cfg, monkeypatch):
    import newsbot.collectors.web_search as web_search

    monkeypatch.setattr(web_search, "_QUERY_GAP_S", 0)
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            queries.append(request.url.params["q"])
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, content=b"<rss version='2.0'><channel></channel></rss>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_check_sources(cfg, _secrets(), client)
    assert queries
    assert not any("industry" in q.lower() for q in queries)


# --- collect_web_search ---


async def _search(cfg, tmp_path, keys, *, key="k"):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"results": []})

    async def no_sleep(_s: float) -> None:
        return None

    path = _db(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps = make_deps(cfg, path, client, [], [])
        outcome = await collect_web_search(deps, keys, key, sleep=no_sleep)
    return outcome, seen


@pytest.mark.parametrize("keys", [[INDUSTRY_KEY, INDUSTRY_KEY], {INDUSTRY_KEY}, (INDUSTRY_KEY,)])
async def test_collect_web_search_for_industry_in_any_collection_shape_does_nothing(
    cfg, tmp_path, keys
):
    outcome, seen = await _search(cfg, tmp_path, keys)
    assert outcome == CollectionOutcome()
    assert seen == []


async def test_collect_web_search_with_a_forged_industry_asks_nothing_about_it(cfg, tmp_path):
    forged = _forged(cfg, search_queries=["industry layoffs"], aliases=["layoffs"])
    outcome, seen = await _search(forged, tmp_path, [INDUSTRY_KEY])
    assert outcome == CollectionOutcome()
    assert seen == []


async def test_collect_web_search_for_every_key_searches_each_game_and_not_industry(cfg, tmp_path):
    _, seen = await _search(cfg, tmp_path, [g.key for g in cfg.catalog])
    assert seen
    assert not any("industry" in q.lower() for q in seen)
    assert {q.split()[0] for q in seen} >= {"Palworld", "Rust"}


async def test_collect_web_search_with_industry_mixed_in_matches_the_games_alone(cfg, tmp_path):
    _, alone = await _search(cfg, tmp_path, ["palworld", "rust"])
    _, mixed = await _search(cfg, tmp_path, [INDUSTRY_KEY, "rust", INDUSTRY_KEY, "palworld"])
    assert mixed == alone


# --- _all_topics and the {games} list ---

FOLLOW_SETS = [
    set(combo)
    for size in range(len(GAMES) + 2)
    for combo in itertools.combinations([*GAMES, INDUSTRY_KEY], size)
]


def test_all_topics_never_lists_industry_for_any_game_and_any_followed_set(cfg):
    for game in cfg.catalog:
        for followed in FOLLOW_SETS:
            got = [g.key for g in _all_topics(cfg, followed, game)]
            assert INDUSTRY_KEY not in got, (game.key, followed)


def test_all_topics_for_a_game_ignores_whether_industry_is_followed(cfg):
    for game in cfg.catalog:
        for followed in FOLLOW_SETS:
            assert _all_topics(cfg, followed | {INDUSTRY_KEY}, game) == _all_topics(
                cfg, followed - {INDUSTRY_KEY}, game
            )


def test_all_topics_keeps_catalog_order_and_the_game_itself(cfg):
    palworld = next(g for g in cfg.catalog if g.key == "palworld")
    got = _all_topics(cfg, {"rust", INDUSTRY_KEY}, palworld)
    assert [g.key for g in got] == ["palworld", "rust"]


def test_all_topics_for_a_comped_server_following_only_industry_is_just_the_game(cfg):
    for game in cfg.catalog:
        if game.key == INDUSTRY_KEY:
            continue
        assert [g.key for g in _all_topics(cfg, {INDUSTRY_KEY}, game)] == [game.key]


def test_all_topics_drops_a_forged_industry_wherever_it_sits_in_the_catalog(cfg):
    forged = _forged(cfg, aliases=["x"], match_name=True)
    first = forged.model_copy(
        update={
            "catalog": [g for g in forged.catalog if g.key == INDUSTRY_KEY]
            + [g for g in forged.catalog if g.key != INDUSTRY_KEY]
        }
    )
    palworld = next(g for g in first.catalog if g.key == "palworld")
    got = _all_topics(first, {INDUSTRY_KEY, "palworld", "rust"}, palworld)
    assert [g.key for g in got] == ["palworld", "rust"]


def test_prompts_are_byte_identical_for_every_game_and_every_followed_set(cfg):
    for game in cfg.catalog:
        if game.key == INDUSTRY_KEY:
            continue
        for followed in FOLLOW_SETS:
            without = build_prompt(
                game,
                [],
                [],
                all_topics=_all_topics(cfg, followed - {INDUSTRY_KEY}, game),
                subject=cfg.ai.subject,
            )
            with_it = build_prompt(
                game,
                [],
                [],
                all_topics=_all_topics(cfg, followed | {INDUSTRY_KEY}, game),
                subject=cfg.ai.subject,
            )
            assert with_it == without, (game.key, followed)
            assert "industry" not in with_it[0].lower(), (game.key, followed)


def test_the_games_sentence_for_a_comped_server_following_a_game_and_industry(cfg):
    palworld = next(g for g in cfg.catalog if g.key == "palworld")
    system, _ = build_prompt(
        palworld,
        [],
        [],
        all_topics=_all_topics(cfg, {"palworld", "rust", INDUSTRY_KEY}, palworld),
        subject=cfg.ai.subject,
    )
    assert "video games Palworld and Rust." in system


# --- through the real summaries job ---


async def test_a_comped_server_following_a_game_and_industry_gets_a_clean_prompt_and_no_search(
    tmp_path, monkeypatch
):
    world = World(tmp_path, monkeypatch, "config_v3_industry.yaml")
    world.guild(1, "comped", "America/Los_Angeles", ["palworld", "rust", INDUSTRY_KEY])
    world.item("palworld", "Raid boss patch", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)

    deps: SummaryDeps = world.deps(web=True)
    try:
        await prepare_summaries(deps)
    finally:
        if world.http is not None:
            await world.http.aclose()

    assert world.llm.calls, "palworld should have been summarized"
    for system, user in world.llm.calls:
        if "(key: palworld)" in user:
            assert "video games Palworld and Rust." in system
        assert "Gaming industry" not in system
    assert not any("industry" in q.lower() for q in world.brave)


async def test_a_comped_server_following_only_industry_adds_nothing_to_any_games_prompt(
    tmp_path, monkeypatch
):
    world = World(tmp_path, monkeypatch, "config_v3_industry.yaml")
    world.guild(1, "comped", "America/Los_Angeles", ["palworld"])
    world.guild(2, "comped", "America/Los_Angeles", [INDUSTRY_KEY])
    world.item("palworld", "Raid boss patch", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)

    deps = world.deps(web=True)
    try:
        await prepare_summaries(deps)
    finally:
        if world.http is not None:
            await world.http.aclose()

    palworld_prompts = [s for s, u in world.llm.calls if "(key: palworld)" in u]
    assert palworld_prompts
    for system in palworld_prompts:
        assert "about the video games Palworld." in system
        assert "industry" not in system.lower()
    assert not any("industry" in q.lower() for q in world.brave)
