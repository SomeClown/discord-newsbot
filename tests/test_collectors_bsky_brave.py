"""Tests for the Bluesky search and Brave web-search collectors, no network."""

import json
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import QuotaExceeded, run_collectors
from newsbot.collectors.bluesky import BlueskyCollector, BlueskySession
from newsbot.collectors.web_search import WebSearchCollector
from newsbot.config import BlueskySource, Topic, WebSearchSource

FIXTURES = Path(__file__).parent / "fixtures"


async def _noop_sleep(_seconds: float) -> None:
    return None


# --- Bluesky: unauthenticated search ---


async def test_bluesky_collector_parses_fixture_unauthenticated():
    body = (FIXTURES / "bluesky_search.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "public.api.bsky.app" in str(request.url)
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=body)

    source = BlueskySource(
        type="bluesky_search", query="Palworld", topics=["palworld"], trust="community"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        items = await BlueskyCollector(source, session=None).collect(http)

    assert len(items) == 2
    assert items[0].url == "https://bsky.app/profile/palworldfan.bsky.social/post/3l6abcdefg"
    assert items[0].title.startswith("Palworld's new update just dropped")
    assert items[0].topics == ("palworld",)
    assert items[0].published_at is not None


async def test_bluesky_collector_403_html_body_is_skipped_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content=b"<html>blocked by CDN</html>", headers={"content-type": "text/html"}
        )

    source = BlueskySource(
        type="bluesky_search", query="Palworld", topics=["palworld"], trust="community"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(QuotaExceeded):
            await BlueskyCollector(source, session=None).collect(http)


async def test_bluesky_collector_403_becomes_skipped_via_run_collectors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"<html>blocked</html>")

    source = BlueskySource(
        type="bluesky_search", query="Palworld", topics=["palworld"], trust="community"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors(
            [BlueskyCollector(source, session=None)], http, sleep=_noop_sleep
        )

    assert results[0].skipped == "auth"
    assert results[0].error is None
    assert results[0].items == []


async def test_bluesky_collector_401_is_also_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "AuthMissing"})

    source = BlueskySource(
        type="bluesky_search", query="Palworld", topics=["palworld"], trust="community"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(QuotaExceeded):
            await BlueskyCollector(source, session=None).collect(http)


# --- Bluesky: authenticated path ---


async def test_bluesky_collector_authenticated_calls_create_session_once_and_reuses_token():
    body = (FIXTURES / "bluesky_search.json").read_bytes()
    session_calls = []
    search_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("createSession"):
            session_calls.append(request)
            return httpx.Response(200, json={"accessJwt": "tok-123", "did": "did:plc:bot"})
        search_calls.append(request)
        assert request.headers["Authorization"] == "Bearer tok-123"
        assert "bsky.social" in str(request.url)
        return httpx.Response(200, content=body)

    session = BlueskySession("bot.bsky.social", "app-password")
    sources = [
        BlueskySource(
            type="bluesky_search", query="Palworld", topics=["palworld"], trust="community"
        ),
        BlueskySource(
            type="bluesky_search", query="Diablo 4", topics=["diablo4"], trust="community"
        ),
    ]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        for source in sources:
            await BlueskyCollector(source, session=session).collect(http)

    assert len(session_calls) == 1
    assert len(search_calls) == 2


# --- Brave web search ---


async def test_web_search_collector_parses_fixture():
    body = (FIXTURES / "brave_news.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "brave-key"
        return httpx.Response(200, content=body)

    topics = [Topic(key="diablo4", name="Diablo IV", aliases=["Diablo 4"], entities=["Blizzard"])]
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        items = await WebSearchCollector(source, topics, "brave-key", sleep=_noop_sleep).collect(
            http
        )

    assert len(items) == 2
    assert items[0].title == "Diablo IV's next season adds a new class-defining mechanic"
    assert items[0].topics == ("diablo4",)
    assert items[0].published_at is not None
    assert items[1].published_at is None  # no page_age in the fixture's second result


async def test_web_search_collector_query_expansion_equals_topics_times_queries_per_topic():
    body = json.dumps({"results": []}).encode()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["q"])
        return httpx.Response(200, content=body)

    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=[], entities=[]),
        Topic(key="palworld", name="Palworld", aliases=[], entities=[]),
    ]
    source = WebSearchSource(
        type="web_search",
        queries_per_topic=2,
        query_templates=["{name} news", "{name} update OR patch"],
        trust="press",
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        await WebSearchCollector(source, topics, "brave-key", sleep=_noop_sleep).collect(http)

    assert len(calls) == len(topics) * source.queries_per_topic
    assert "Borderlands 4 news" in calls
    assert "Palworld update OR patch" in calls


async def test_web_search_collector_paces_queries_with_injected_sleep():
    body = json.dumps({"results": []}).encode()
    sleeps = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    topics = [Topic(key="palworld", name="Palworld", aliases=[], entities=[])]
    source = WebSearchSource(type="web_search", queries_per_topic=2, trust="press")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        await WebSearchCollector(source, topics, "brave-key", sleep=recording_sleep).collect(http)

    assert sleeps == [1.1]  # one gap between the two queries, none before the first


async def test_web_search_collector_429_raises_quota_exceeded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    topics = [Topic(key="palworld", name="Palworld", aliases=[], entities=[])]
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(QuotaExceeded):
            await WebSearchCollector(source, topics, "brave-key", sleep=_noop_sleep).collect(http)


async def test_web_search_collector_429_becomes_skipped_via_run_collectors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    topics = [Topic(key="palworld", name="Palworld", aliases=[], entities=[])]
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")
    transport = httpx.MockTransport(handler)
    collector = WebSearchCollector(source, topics, "brave-key", sleep=_noop_sleep)
    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors([collector], http, sleep=_noop_sleep)

    assert results[0].skipped == "quota"
    assert results[0].error is None
