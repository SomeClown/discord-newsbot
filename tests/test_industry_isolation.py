"""The industry entry stays out of keyword matching, Brave and other games' prompts.

Studio closures aren't about any one game, so the `industry` entry only ever gets
what its own feeds hand it. Everything below is a way it could leak into the
games that were here first: a keyword hit, a Brave query, a line in someone's
system prompt. The existing games' behavior is pinned elsewhere
(`test_filter.py`, `test_codes_only.py`, `test_prod_catalog_step.py`); this file
pins the new entry's side of the fence.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from test_collect import make_deps
from v3_fakes import load_v3_config, load_v3_industry_config

from newsbot.collectors.base import RawItem, build_catalog_collectors
from newsbot.config import AppConfig, Secrets
from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline.collect import CollectionOutcome, collect_web_search
from newsbot.pipeline.filter import TopicMatcher, build_matchers, filter_items
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.summaries import _all_topics
from newsbot.store.db import connect, migrate

HEADLINE = "Gaming industry layoffs hit Palworld studio Pocketpair"


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


def _item(title: str, *, topics=None) -> RawItem:
    return RawItem(
        url="https://example.com/story",
        title=title,
        excerpt="",
        source_name="Some Source",
        trust="press",
        published_at=datetime(2026, 10, 23, 12, tzinfo=UTC),
        topics=topics,
    )


def _forged(cfg: AppConfig, **update) -> AppConfig:
    """A config where industry has keyword terms, built past `load_config`'s checks."""
    catalog = [g.model_copy(update=update) if g.key == INDUSTRY_KEY else g for g in cfg.catalog]
    return cfg.model_copy(update={"catalog": catalog})


# --- keyword matching ---


def test_a_general_item_about_the_industry_matches_the_game_and_never_industry(cfg):
    grouped = filter_items([_item(HEADLINE)], cfg.catalog, 10)
    assert set(grouped) == {"palworld"}


def test_a_dedicated_industry_item_is_a_confident_match(cfg):
    grouped = filter_items([_item("A studio closes", topics=(INDUSTRY_KEY,))], cfg.catalog, 10)
    assert set(grouped) == {INDUSTRY_KEY}
    (only,) = grouped[INDUSTRY_KEY]
    assert only.uncertain is False


def test_a_forged_industry_with_aliases_and_a_name_match_still_never_keyword_matches(cfg):
    forged = _forged(
        cfg, aliases=["layoffs"], entities=["Pocketpair"], match_name=True, name="Gaming industry"
    )
    matchers = build_matchers(forged.catalog)
    assert matchers[INDUSTRY_KEY] == TopicMatcher(None, None)
    grouped = filter_items([_item(HEADLINE), _item("Gaming industry news")], forged.catalog, 10)
    assert INDUSTRY_KEY not in grouped


def test_a_forged_industry_still_takes_its_dedicated_items(cfg):
    forged = _forged(cfg, aliases=["layoffs"])
    grouped = filter_items([_item("Unrelated", topics=(INDUSTRY_KEY,))], forged.catalog, 10)
    assert [t.uncertain for t in grouped[INDUSTRY_KEY]] == [False]


# --- Brave ---


def _brave_games(collectors) -> list[list[str]]:
    return [
        [g.key for g in c._topics] for c in collectors if type(c).__name__ == "WebSearchCollector"
    ]


def test_brave_is_never_handed_industry_by_default(cfg):
    built = build_catalog_collectors(cfg, _secrets())
    assert _brave_games(built) == [["borderlands4", "palworld", "rust"]]


def test_brave_is_never_handed_industry_when_the_whole_catalog_is_passed(cfg):
    built = build_catalog_collectors(cfg, _secrets(), web_search_games=cfg.catalog)
    assert _brave_games(built) == [["borderlands4", "palworld", "rust"]]


def test_no_brave_collector_is_built_when_industry_was_all_that_was_asked_for(cfg):
    only = [g for g in cfg.catalog if g.key == INDUSTRY_KEY]
    built = build_catalog_collectors(cfg, _secrets(), web_search_games=only)
    assert _brave_games(built) == []


def test_the_games_collectors_are_the_same_with_and_without_industry(monkeypatch):
    plain = load_v3_config(monkeypatch)
    withit = load_v3_industry_config(monkeypatch)
    before = {c.name for c in build_catalog_collectors(plain, _secrets())}
    after = {c.name for c in build_catalog_collectors(withit, _secrets())}
    assert after - before == {"Industry Feed A", "Industry Feed B"}
    assert before <= after


async def test_collect_web_search_for_industry_alone_does_nothing(cfg, tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps = make_deps(cfg, path, client, [], [])
        outcome = await collect_web_search(deps, [INDUSTRY_KEY], "k")
    assert outcome == CollectionOutcome()
    assert seen == []


async def test_collect_web_search_skips_industry_when_asked_for_a_game_too(cfg, tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"results": []})

    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)

    async def no_sleep(_s: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps = make_deps(cfg, path, client, [], [])
        await collect_web_search(deps, ["palworld", INDUSTRY_KEY], "k", sleep=no_sleep)
    assert seen == ["Palworld news", "Palworld update OR patch OR leak"]


# --- the {games} list in other games' prompts ---


def test_all_topics_leaves_industry_out(cfg):
    game = next(g for g in cfg.catalog if g.key == "palworld")
    got = _all_topics(cfg, {"borderlands4", "palworld", INDUSTRY_KEY}, game)
    assert [g.key for g in got] == ["borderlands4", "palworld"]


def test_every_games_prompt_is_the_same_with_and_without_industry_followed(cfg):
    followed = {"borderlands4", "palworld", "rust"}
    for game in cfg.catalog:
        if game.key == INDUSTRY_KEY:
            continue
        without = build_prompt(
            game, [], [], all_topics=_all_topics(cfg, followed, game), subject=cfg.ai.subject
        )
        with_it = build_prompt(
            game,
            [],
            [],
            all_topics=_all_topics(cfg, followed | {INDUSTRY_KEY}, game),
            subject=cfg.ai.subject,
        )
        assert with_it == without
        assert "Gaming industry" not in with_it[0]
