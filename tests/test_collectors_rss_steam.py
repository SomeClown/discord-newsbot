"""Tests for the RSS and Steam collectors: fixture parsing, no network, ever.

httpx.MockTransport stands in for the network. Nothing here calls a real
socket -- if it does, that's a bug, not a slow test.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import RawItem, run_collectors
from newsbot.collectors.rss import RssCollector
from newsbot.collectors.steam import SteamCollector
from newsbot.config import RssSource, SteamSource

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


async def _noop_sleep(_seconds: float) -> None:
    return None


def _transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("?")[0]
        if key not in responses:
            raise AssertionError(f"unexpected request to {request.url}")
        return responses[key]

    return httpx.MockTransport(handler)


# --- RSS 2.0 ---


async def test_rss_collector_parses_rss20_fixture():
    body = (FIXTURES / "rss20_sample.xml").read_bytes()
    source = RssSource(
        type="rss", name="PC Gamer", url="https://www.pcgamer.com/rss/", trust="press"
    )
    transport = _transport({"https://www.pcgamer.com/rss/": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert len(items) == 3
    first = items[0]
    assert first.title == "Palworld's next update adds a new island, patch notes inside"
    assert "Pocketpair has confirmed the next Palworld update" in first.excerpt
    assert first.source_name == "PC Gamer"
    assert first.trust == "press"
    assert first.published_at == datetime(2026, 9, 22, 14, 30, tzinfo=UTC)
    assert first.topics is None


def test_rss_item_with_no_pubdate_has_no_published_at():
    body = (FIXTURES / "rss20_sample.xml").read_bytes()
    import feedparser

    parsed = feedparser.parse(body)
    undated = parsed.entries[2]
    assert "published_parsed" not in undated or undated.get("published_parsed") is None


async def test_rss_collector_item_with_no_date_gives_none():
    body = (FIXTURES / "rss20_sample.xml").read_bytes()
    source = RssSource(
        type="rss", name="PC Gamer", url="https://www.pcgamer.com/rss/", trust="press"
    )
    transport = _transport({"https://www.pcgamer.com/rss/": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    undated = next(i for i in items if "roundup" in i.title)
    assert undated.published_at is None


# --- Atom (YouTube-style) ---


async def test_rss_collector_parses_atom_youtube_fixture():
    body = (FIXTURES / "atom_youtube_sample.xml").read_bytes()
    source = RssSource(
        type="rss",
        name="Diablo YouTube",
        url="https://www.youtube.com/feeds/videos.xml?channel_id=UCxn8csYeZg6awRnZS-aqg0g",
        topics=["diablo4"],
        trust="official",
    )
    transport = _transport(
        {
            "https://www.youtube.com/feeds/videos.xml": httpx.Response(200, content=body),
        }
    )
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert len(items) == 2
    assert items[0].title == "Diablo IV Season 12 Official Trailer"
    assert items[0].url == "https://www.youtube.com/watch?v=abc123"
    assert items[0].published_at == datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    assert items[0].topics == ("diablo4",)
    assert items[0].trust == "official"


# --- Subreddit Atom feed ---


async def test_rss_collector_parses_reddit_fixture_and_sets_rate_limit_key():
    body = (FIXTURES / "reddit_top_sample.xml").read_bytes()
    source = RssSource(
        type="rss",
        name="r/Palworld",
        url="https://www.reddit.com/r/Palworld/top/.rss?t=day",
        topics=["palworld"],
        trust="community",
    )
    collector = RssCollector(source, sleep=_noop_sleep)
    assert collector.rate_limit_key == "reddit"

    transport = _transport(
        {"https://www.reddit.com/r/Palworld/top/.rss": httpx.Response(200, content=body)}
    )
    async with httpx.AsyncClient(transport=transport) as http:
        items = await collector.collect(http)

    assert len(items) == 2
    assert items[0].title == "PSA: back up your save before the new patch"
    assert items[0].topics == ("palworld",)


def test_rss_collector_non_reddit_host_has_no_rate_limit_key():
    source = RssSource(
        type="rss", name="PC Gamer", url="https://www.pcgamer.com/rss/", trust="press"
    )
    assert RssCollector(source).rate_limit_key is None


# --- Bluesky official-account RSS mirror: no <title>, falls back to description ---


async def test_rss_collector_falls_back_to_first_line_of_description_when_title_missing():
    body = (FIXTURES / "bluesky_profile_sample.xml").read_bytes()
    source = RssSource(
        type="rss",
        name="Borderlands Bluesky",
        url="https://bsky.app/profile/did:plc:sxmjis6jypsib2k7rk6pjoau/rss",
        topics=["borderlands4"],
        trust="official",
    )
    transport = _transport(
        {
            "https://bsky.app/profile/did:plc:sxmjis6jypsib2k7rk6pjoau/rss": httpx.Response(
                200, content=body
            )
        }
    )
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert len(items) == 1
    assert items[0].title.startswith("Vault Hunters, the Season 2 update drops next week")


# --- Reddit 429 retry ---


async def test_rss_collector_retries_on_429_then_succeeds():
    body = (FIXTURES / "reddit_top_sample.xml").read_bytes()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, content=body)

    source = RssSource(
        type="rss",
        name="r/Palworld",
        url="https://www.reddit.com/r/Palworld/top/.rss?t=day",
        topics=["palworld"],
        trust="community",
    )
    sleeps = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=recording_sleep).collect(http)

    assert len(calls) == 3
    assert sleeps == [5.0, 15.0]
    assert len(items) == 2


async def test_rss_collector_gives_up_after_retries_and_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests")

    source = RssSource(
        type="rss",
        name="r/Palworld",
        url="https://www.reddit.com/r/Palworld/top/.rss?t=day",
        topics=["palworld"],
        trust="community",
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await RssCollector(source, sleep=_noop_sleep).collect(http)


# --- Bozo feed with no entries is an error ---


async def test_rss_collector_bozo_feed_with_no_entries_raises():
    source = RssSource(
        type="rss", name="Broken Feed", url="https://example.com/broken.xml", trust="press"
    )
    transport = _transport(
        {"https://example.com/broken.xml": httpx.Response(200, content=b"not xml at all <<<")}
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="did not parse"):
            await RssCollector(source, sleep=_noop_sleep).collect(http)


# --- Steam ---


async def test_steam_collector_parses_fixture():
    body = (FIXTURES / "steam_news_sample.json").read_bytes()
    source = SteamSource(
        type="steam_news",
        name="Palworld Steam",
        app_id=1623730,
        topics=["palworld"],
        trust="official",
    )
    transport = _transport(
        {
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/": httpx.Response(
                200, content=body
            )
        }
    )
    async with httpx.AsyncClient(transport=transport) as http:
        items = await SteamCollector(source).collect(http)

    assert len(items) == 2
    assert items[0].title == "Patch v0.6.2 Notes"
    assert items[0].excerpt == (
        "Patch v0.6.2 fixes several crash issues and adds new Pals. See below for the full list."
    )
    assert items[0].published_at == datetime.fromtimestamp(1758000000, tz=UTC)
    assert items[0].trust == "official"
    assert items[0].topics == ("palworld",)


async def test_steam_collector_500_becomes_error_via_run_collectors():
    source = SteamSource(
        type="steam_news",
        name="Palworld Steam",
        app_id=1623730,
        topics=["palworld"],
        trust="official",
    )
    transport = _transport(
        {"https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/": httpx.Response(500)}
    )
    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors([SteamCollector(source)], http)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].items == []


# --- run_collectors orchestration ---


async def test_run_collectors_one_slow_collector_does_not_block_others():
    class SlowCollector:
        name = "slow"
        source_type = "rss"
        rate_limit_key = None

        async def collect(self, http):
            import asyncio

            await asyncio.sleep(10)
            return []

    class FastCollector:
        name = "fast"
        source_type = "rss"
        rate_limit_key = None

        async def collect(self, http):
            return [
                RawItem(
                    url="https://example.com/a",
                    title="a",
                    excerpt="a",
                    source_name="fast",
                    trust="press",
                    published_at=None,
                )
            ]

    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors(
            [SlowCollector(), FastCollector()], http, timeout_s=0.05, sleep=_noop_sleep
        )

    by_name = {r.source_name: r for r in results}
    assert by_name["slow"].error is not None
    assert "timed out" in by_name["slow"].error
    assert by_name["fast"].items


async def test_run_collectors_serializes_shared_rate_limit_key_with_gap():
    order = []

    class TrackedCollector:
        source_type = "rss"
        rate_limit_key = "reddit"

        def __init__(self, name):
            self.name = name

        async def collect(self, http):
            order.append(self.name)
            return []

    sleeps = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        await run_collectors(
            [TrackedCollector("one"), TrackedCollector("two")],
            http,
            rate_limit_gap_s=35.0,
            sleep=recording_sleep,
        )

    assert order == ["one", "two"]
    assert sleeps == [35.0]


async def test_run_collectors_quota_exceeded_becomes_skipped_not_error():
    from newsbot.collectors.base import QuotaExceeded

    class QuotaCollector:
        name = "brave"
        source_type = "web_search"
        rate_limit_key = None

        async def collect(self, http):
            raise QuotaExceeded("quota")

    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors([QuotaCollector()], http, sleep=_noop_sleep)

    assert results[0].skipped == "quota"
    assert results[0].error is None
