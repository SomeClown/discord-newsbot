"""Brave results are tagged with the game whose query found them; that tag isn't proof.

A Steam app feed for Palworld needn't say "Palworld" (SPEC-DEV 3), so a single
topic on an item means "confident, no keyword check". Brave matches loosely,
though, and an article about some other game's patch notes came back from a
Borderlands query and got filed under Borderlands anyway. These tests pin the
fix: Brave items still only consider the game that found them, but they have to
earn the match the way a press item does. Dedicated feeds are untouched.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import RawItem
from newsbot.collectors.web_search import WebSearchCollector
from newsbot.config import Topic, WebSearchSource, load_config
from newsbot.pipeline.collect import CollectionDeps, collect_web_search
from newsbot.pipeline.filter import filter_items
from newsbot.store.db import connect, migrate

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)

BL4 = Topic(
    key="borderlands4", name="Borderlands 4", channel_id=1, aliases=["BL4"], entities=["Gearbox"]
)
PALWORLD = Topic(key="palworld", name="Palworld", channel_id=1, aliases=[], entities=["Pocketpair"])
TOPICS = [BL4, PALWORLD]


def _item(
    title="", excerpt="", *, topics=("borderlands4",), needs_match=False, url="https://x.test/a"
):
    return RawItem(
        url=url,
        title=title,
        excerpt=excerpt,
        source_name="Some Source",
        trust="press",
        published_at=RECENT,
        topics=topics,
        needs_match=needs_match,
    )


def _brave(title="", excerpt="", **kw):
    return _item(title, excerpt, needs_match=True, **kw)


# --- filter_items ---


def test_off_topic_brave_item_is_not_a_match():
    """The bug: an Assassin's Creed article tagged to Borderlands came out confident."""
    result = filter_items([_brave("Assassin's Creed IV patch notes")], TOPICS, max_per_topic=10)
    assert result == {}


def test_brave_item_naming_the_game_is_confident():
    result = filter_items([_brave("Borderlands 4 gets a patch")], TOPICS, max_per_topic=10)
    assert [(t.topic_key, t.uncertain) for t in result["borderlands4"]] == [("borderlands4", False)]


def test_brave_item_naming_an_alias_is_confident():
    result = filter_items([_brave("New BL4 hotfix", "")], TOPICS, max_per_topic=10)
    assert [(t.topic_key, t.uncertain) for t in result["borderlands4"]] == [("borderlands4", False)]


def test_brave_item_matches_on_the_excerpt_too():
    result = filter_items(
        [_brave("Patch notes", "Fixes for Borderlands 4 co-op")], TOPICS, max_per_topic=10
    )
    assert result["borderlands4"][0].uncertain is False


def test_brave_item_naming_only_an_entity_is_uncertain():
    result = filter_items([_brave("Gearbox announces a new studio")], TOPICS, max_per_topic=10)
    assert [(t.topic_key, t.uncertain) for t in result["borderlands4"]] == [("borderlands4", True)]


def test_brave_item_only_ever_matches_the_game_that_found_it():
    """It names Palworld, but a Borderlands query found it: it stays out of Palworld's list."""
    result = filter_items([_brave("Palworld update lands")], TOPICS, max_per_topic=10)
    assert result == {}


def test_dedicated_single_topic_item_still_needs_no_keyword():
    """Regression guard for SPEC-DEV 3: a game's own feed needn't name the game."""
    result = filter_items([_item("Hotfix 1.2.3 notes")], TOPICS, max_per_topic=10)
    assert [(t.topic_key, t.uncertain) for t in result["borderlands4"]] == [("borderlands4", False)]


# --- the collector sets the flag ---


async def test_web_search_collector_marks_its_items_as_needing_a_match():
    body = {"results": [{"url": "https://news.test/a", "title": "A headline"}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")

    async def no_sleep(_seconds: float) -> None:
        return None

    async with httpx.AsyncClient(transport=transport) as http:
        items = await WebSearchCollector(source, [PALWORLD], "k", sleep=no_sleep).collect(http)
    assert [(i.topics, i.needs_match) for i in items] == [(("palworld",), True)]


# --- end to end: collector, filter and store ---


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


async def test_off_topic_brave_result_never_reaches_item_topics(db_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    cfg = load_config(V3)
    results = [
        ("https://news.test/on", "Palworld gets a big update", ""),
        ("https://news.test/entity", "Pocketpair talks funding", ""),
        ("https://news.test/off", "Modern Warfare 4 patch notes", "A shooter gets fixes."),
    ]
    body = {
        "results": [
            {"url": u, "title": t, "description": d, "page_age": RECENT.isoformat()}
            for u, t, d in results
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))

    async def no_sleep(_seconds: float) -> None:
        return None

    async def alert(_message: str) -> None:
        return None

    async with httpx.AsyncClient(transport=transport) as http:
        deps = CollectionDeps(
            cfg=cfg, db_path=db_path, http=http, collectors=[], now=lambda: NOW, alert=alert
        )
        await collect_web_search(deps, ["palworld"], "k", sleep=no_sleep)

    with closing(connect(db_path)) as conn:
        stored = {r["url"] for r in conn.execute("SELECT url FROM items")}
        tagged = {
            r["url"]: bool(r["uncertain"])
            for r in conn.execute(
                "SELECT i.url, t.uncertain FROM items i JOIN item_topics t ON t.item_id = i.id"
            )
        }
    assert stored == {"https://news.test/on", "https://news.test/entity"}
    assert tagged == {"https://news.test/on": False, "https://news.test/entity": True}


async def test_a_result_with_a_non_string_title_or_url_is_skipped_not_fatal(db_path, monkeypatch):
    """A truthy title that isn't text (2026, true) once got as far as the matcher and raised."""
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    cfg = load_config(V3)
    page_age = RECENT.isoformat()
    body = {
        "results": [
            {"url": "https://news.test/number", "title": 2026, "page_age": page_age},
            {"url": "https://news.test/bool", "title": True, "page_age": page_age},
            {"url": 12345, "title": "Palworld gets a patch", "page_age": page_age},
            {
                "url": "https://news.test/good",
                "title": "Palworld gets a patch",
                "page_age": page_age,
            },
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))

    async def no_sleep(_seconds: float) -> None:
        return None

    async def alert(_message: str) -> None:
        return None

    async with httpx.AsyncClient(transport=transport) as http:
        deps = CollectionDeps(
            cfg=cfg, db_path=db_path, http=http, collectors=[], now=lambda: NOW, alert=alert
        )
        outcome = await collect_web_search(deps, ["palworld"], "k", sleep=no_sleep)

    with closing(connect(db_path)) as conn:
        stored = {r["url"] for r in conn.execute("SELECT url FROM items")}
    assert outcome.failing_sources == ()
    assert stored == {"https://news.test/good"}
