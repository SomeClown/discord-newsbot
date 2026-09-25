"""Tests for the RSS and Steam collectors: fixture parsing, no network, ever.

httpx.MockTransport stands in for the network. Nothing here calls a real
socket -- if it does, that's a bug, not a slow test.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import RawItem, build_collectors, run_collectors
from newsbot.collectors.rss import _RETRY_BACKOFFS_S, RssCollector, _fetch_body
from newsbot.collectors.steam import SteamCollector
from newsbot.config import (
    AppConfig,
    BlueskySource,
    DigestCfg,
    RssSource,
    Secrets,
    SteamSource,
    Topic,
    WebSearchSource,
)

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
    # QA step 20, group 6f: the old backoffs (5.0, 15.0 -- 20s just in
    # sleeps, before any request latency) didn't fit inside
    # pipeline/run.py's 20s _COLLECT_TIMEOUT_S, so the third (successful)
    # attempt could never actually land within the budget. Shrunk so the
    # full retry sequence has real room left for request round trips too.
    assert sleeps == [_RETRY_BACKOFFS_S[0], _RETRY_BACKOFFS_S[1]]
    assert sum(_RETRY_BACKOFFS_S) < 15.0  # leaves headroom under the 20s collector timeout
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


# --- byte cap (QA step 20, group 6f) ---


async def test_fetch_body_caps_response_at_5mb():
    # A feed (broken, malicious, or just enormous) that tries to hand back
    # more than 5MB shouldn't get to make this collector buffer the whole
    # thing into memory -- _fetch_body is the one place that reads the
    # response body, so the cap is tested directly against it rather than
    # through collect()'s parse step (whether an oversized-then-truncated
    # body happens to still parse is feedparser's business, not this
    # cap's).
    oversized = b"a" * (6 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        body = await _fetch_body(http, "https://example.com/huge.xml", sleep=_noop_sleep)
    assert len(body) == 5 * 1024 * 1024


# --- redirect-to-private-host rejection (QA step 20, group 6g) ---


async def test_rss_collector_rejects_redirect_to_link_local_metadata_host():
    # 169.254.169.254 is the cloud-provider instance-metadata address --
    # the canonical SSRF target. A feed URL that redirects there should
    # never get followed to completion.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200, content=b"secret metadata, never seen")

    source = RssSource(
        type="rss", name="Redirecting Feed", url="https://example.com/feed.xml", trust="press"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="non-public"):
            await RssCollector(source, sleep=_noop_sleep).collect(http)


async def test_rss_collector_allows_a_redirect_to_a_normal_public_host():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "https://cdn.example.net/feed.xml"})
        return httpx.Response(200, content=(FIXTURES / "rss20_empty.xml").read_bytes())

    source = RssSource(
        type="rss", name="Redirecting Feed", url="https://example.com/feed.xml", trust="press"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)
    assert items == []


# --- adversarial: every private/loopback/link-local IP shape from a literal redirect ---


@pytest.mark.parametrize(
    "location_host",
    [
        "127.0.0.1",
        "[::1]",  # IPv6 literals need brackets in a URL authority
        "10.4.5.6",
        "0.0.0.0",  # noqa: S104 -- a redirect target under test, not a bind address
        "169.254.169.254",
        "192.168.1.1",
        "172.16.0.1",
    ],
)
async def test_rss_collector_rejects_redirect_to_every_private_ip_shape(location_host):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": f"http://{location_host}/x"})
        return httpx.Response(200, content=b"never seen")

    source = RssSource(
        type="rss", name="Redirecting Feed", url="https://example.com/feed.xml", trust="press"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="non-public"):
            await RssCollector(source, sleep=_noop_sleep).collect(http)


async def test_rss_collector_rejects_redirect_to_a_hostname_that_resolves_to_a_private_address(
    monkeypatch,
):
    # The literal-IP cases above never exercise the DNS-resolution branch
    # of _reject_private_redirect -- a redirect to a *hostname* (not an IP
    # literal) that happens to resolve to a private address needs the
    # same rejection, and only mocking the resolver can prove that branch
    # actually runs and actually rejects.
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "internal.attacker.example":
            return [(2, 1, 6, "", ("10.13.14.15", 0))]
        raise OSError(f"unexpected resolution attempt for {host!r}")

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "http://internal.attacker.example/x"})
        return httpx.Response(200, content=b"never seen")

    source = RssSource(
        type="rss", name="Redirecting Feed", url="https://example.com/feed.xml", trust="press"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="non-public"):
            await RssCollector(source, sleep=_noop_sleep).collect(http)


async def test_rss_collector_rejects_redirect_to_cgnat_shared_address_space():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "http://100.64.0.1/x"})
        return httpx.Response(200, content=b"never seen")

    source = RssSource(
        type="rss", name="Redirecting Feed", url="https://example.com/feed.xml", trust="press"
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="non-public"):
            await RssCollector(source, sleep=_noop_sleep).collect(http)


# --- adversarial: 5MB cap with no Content-Length header, and exactly-at-cap body ---


async def test_fetch_body_at_exactly_5mb_is_not_truncated():
    exact = b"a" * (5 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=exact)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        body = await _fetch_body(http, "https://example.com/exact.xml", sleep=_noop_sleep)
    assert len(body) == 5 * 1024 * 1024
    assert body == exact


async def test_fetch_body_cap_applies_even_without_a_content_length_header():
    # _fetch_body reads via aiter_bytes() chunk-by-chunk and never
    # consults Content-Length to decide when to stop -- this pins that a
    # response streamed without that header (a chunked-transfer response,
    # which is exactly what a malicious or misconfigured server might
    # send to dodge a length-based guard) is still capped correctly.
    oversized = b"b" * (6 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=oversized)
        del response.headers["content-length"]
        return response

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        body = await _fetch_body(http, "https://example.com/huge.xml", sleep=_noop_sleep)
    assert len(body) == 5 * 1024 * 1024


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


async def test_rss_collector_empty_feed_returns_no_items_without_raising():
    body = (FIXTURES / "rss20_empty.xml").read_bytes()
    source = RssSource(
        type="rss", name="Empty Feed", url="https://example.com/empty.xml", trust="press"
    )
    transport = _transport({"https://example.com/empty.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)
    assert items == []


async def test_rss_collector_malformed_but_recoverable_xml_still_yields_entries():
    # feedparser sets bozo=1 for a raw unescaped "&" but still parses the entry --
    # this must not be treated the same as a feed that gave us nothing at all.
    body = (FIXTURES / "rss20_malformed_recoverable.xml").read_bytes()
    source = RssSource(
        type="rss", name="Sloppy Feed", url="https://example.com/sloppy.xml", trust="press"
    )
    transport = _transport({"https://example.com/sloppy.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)
    assert len(items) == 1
    assert items[0].title == "Bugs & Fixes in the new patch"


async def test_rss_collector_decodes_entities_and_truncates_long_html_description():
    body = (FIXTURES / "rss20_messy_content.xml").read_bytes()
    source = RssSource(
        type="rss", name="Messy Feed", url="https://example.com/messy.xml", trust="press"
    )
    transport = _transport({"https://example.com/messy.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert len(items) == 1
    item = items[0]
    # Entities in the title are decoded by feedparser itself.
    assert item.title == "Patch notes & the <b>big</b> balance pass"
    # The excerpt is run through clean_text: BBCode brackets and HTML tags gone,
    # entities decoded, and truncated with an ellipsis well under the raw length.
    assert "[b]" not in item.excerpt
    assert "<p>" not in item.excerpt
    assert '"infinite ammo"' in item.excerpt
    assert item.excerpt.endswith("…")
    assert len(item.excerpt) <= 500


# --- full_text (plan step 4): untruncated companion to excerpt ---


async def test_rss_collector_sets_full_text_from_description_when_no_content_tag():
    body = (FIXTURES / "rss20_sample.xml").read_bytes()
    source = RssSource(
        type="rss", name="PC Gamer", url="https://www.pcgamer.com/rss/", trust="press"
    )
    transport = _transport({"https://www.pcgamer.com/rss/": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert items[0].full_text == (
        "Pocketpair has confirmed the next Palworld update adds a new island to explore."
    )


async def test_rss_collector_full_text_is_none_when_entry_has_no_body_text():
    body = (FIXTURES / "rss20_empty.xml").read_bytes()
    source = RssSource(
        type="rss", name="Empty Feed", url="https://example.com/empty.xml", trust="press"
    )
    transport = _transport({"https://example.com/empty.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)
    assert items == []


async def test_rss_collector_code_past_500_chars_is_in_full_text_not_excerpt():
    # The whole point of full_text: excerpt (500 chars, ellipsis-truncated)
    # never gets far enough into this post to see the code; full_text does.
    body = (FIXTURES / "rss20_full_text_code.xml").read_bytes()
    source = RssSource(
        type="rss", name="SHiFT Feed", url="https://example.com/shift.xml", trust="press"
    )
    transport = _transport({"https://example.com/shift.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert len(items) == 1
    item = items[0]
    assert item.excerpt.endswith("…")
    assert len(item.excerpt) <= 500
    assert "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE" not in item.excerpt
    assert item.full_text is not None
    assert "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE" in item.full_text
    assert len(item.full_text) > 500


async def test_rss_collector_full_text_combines_content_and_differing_summary():
    body = (FIXTURES / "atom_youtube_sample.xml").read_bytes()
    source = RssSource(
        type="rss",
        name="Diablo YouTube",
        url="https://www.youtube.com/feeds/videos.xml?channel_id=UCxn8csYeZg6awRnZS-aqg0g",
        topics=["diablo4"],
        trust="official",
    )
    transport = _transport(
        {"https://www.youtube.com/feeds/videos.xml": httpx.Response(200, content=body)}
    )
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert items[0].full_text == "Get ready. Season 12 arrives soon."


async def test_rss_collector_excerpt_byte_identical_on_existing_fixture_with_full_text_added():
    # Pins that adding full_text didn't disturb excerpt's own output on an
    # existing fixture -- same excerpt string test_rss_collector_decodes_
    # entities_and_truncates_long_html_description already checks, plus
    # the new full_text field, which should hold the untruncated body.
    body = (FIXTURES / "rss20_messy_content.xml").read_bytes()
    source = RssSource(
        type="rss", name="Messy Feed", url="https://example.com/messy.xml", trust="press"
    )
    transport = _transport({"https://example.com/messy.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)

    assert items[0].excerpt.endswith("…")
    assert len(items[0].excerpt) <= 500
    assert items[0].full_text is not None
    assert items[0].full_text.endswith("ratione voluptatem sequi nesciunt.")
    assert len(items[0].full_text) > len(items[0].excerpt)


async def test_rss_collector_unparseable_pubdate_gives_none_not_a_crash():
    body = (FIXTURES / "rss20_messy_content.xml").read_bytes()
    source = RssSource(
        type="rss", name="Messy Feed", url="https://example.com/messy.xml", trust="press"
    )
    transport = _transport({"https://example.com/messy.xml": httpx.Response(200, content=body)})
    async with httpx.AsyncClient(transport=transport) as http:
        items = await RssCollector(source, sleep=_noop_sleep).collect(http)
    assert items[0].published_at is None


# --- run_collectors: 429 exhaustion counts as a failure, not a skip ---


async def test_rss_collector_429_exhaustion_becomes_error_via_run_collectors():
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
        results = await run_collectors(
            [RssCollector(source, sleep=_noop_sleep)], http, sleep=_noop_sleep
        )

    assert results[0].error is not None
    assert results[0].skipped is None
    assert results[0].items == []


async def test_run_collectors_exception_in_one_reddit_collector_does_not_sink_its_group_mate():
    calls = []

    class BoomCollector:
        name = "boom"
        source_type = "rss"
        rate_limit_key = "reddit"

        async def collect(self, http):
            calls.append(self.name)
            raise RuntimeError("feed parser choked")

    class OkCollector:
        name = "ok"
        source_type = "rss"
        rate_limit_key = "reddit"

        async def collect(self, http):
            calls.append(self.name)
            return [
                RawItem(
                    url="https://example.com/a",
                    title="a",
                    excerpt="a",
                    source_name="ok",
                    trust="community",
                    published_at=None,
                )
            ]

    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors(
            [BoomCollector(), OkCollector()], http, rate_limit_gap_s=0.0, sleep=_noop_sleep
        )

    by_name = {r.source_name: r for r in results}
    assert calls == ["boom", "ok"]  # ran serially, in order, within the shared group
    assert "feed parser choked" in by_name["boom"].error
    assert by_name["ok"].error is None
    assert by_name["ok"].items


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
    assert items[0].full_text == (
        "Patch v0.6.2 fixes several crash issues and adds new Pals. See below for the full list."
    )


async def test_steam_collector_full_text_is_none_for_null_contents():
    body = json.dumps(
        {
            "appnews": {
                "newsitems": [
                    {
                        "url": "https://store.steampowered.com/news/app/1/view/1",
                        "title": "Null contents",
                        "contents": None,
                        "date": 1758000000,
                    }
                ]
            }
        }
    ).encode()
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
    assert items[0].full_text is None


async def test_steam_collector_skips_malformed_items_but_keeps_the_good_ones():
    # QA step 20, group 6c: one bad item (missing url, missing title, a
    # null contents field, a missing date) shouldn't take the other,
    # perfectly good items in the same response down with it.
    body = json.dumps(
        {
            "appnews": {
                "newsitems": [
                    {
                        "title": "Missing URL",
                        "contents": "no url here",
                        "date": 1758000000,
                    },
                    {
                        "url": "https://store.steampowered.com/news/app/1/view/2",
                        "contents": "no title here",
                        "date": 1758000000,
                    },
                    {
                        "url": "https://store.steampowered.com/news/app/1/view/3",
                        "title": "Missing date",
                        "contents": "no date here",
                    },
                    {
                        "url": "https://store.steampowered.com/news/app/1/view/4",
                        "title": "Null contents",
                        "contents": None,
                        "date": 1758000000,
                    },
                    {
                        "url": "https://store.steampowered.com/news/app/1/view/5",
                        "title": "The good one",
                        "contents": "perfectly fine",
                        "date": 1758000000,
                    },
                ]
            }
        }
    ).encode()
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

    titles = {item.title for item in items}
    assert "Null contents" in titles  # null contents shouldn't drop the item
    assert "The good one" in titles
    assert "Missing URL" not in titles
    assert "Missing date" not in titles
    assert len(items) == 2  # only the two items with url/title/date all present


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


# --- build_collectors: wiring every source type from a full config ---


def _full_config() -> AppConfig:
    return AppConfig(
        guild_id=1,
        digest=DigestCfg(channel_id=1, time="09:00", timezone="UTC"),
        topics=[Topic(key="palworld", name="Palworld")],
        sources=[
            RssSource(
                type="rss", name="PC Gamer", url="https://www.pcgamer.com/rss/", trust="press"
            ),
            SteamSource(type="steam_news", name="Palworld Steam", app_id=1623730, trust="official"),
            BlueskySource(
                type="bluesky_search", query="Palworld", topics=["palworld"], trust="community"
            ),
            WebSearchSource(type="web_search", trust="press"),
        ],
    )


def test_build_collectors_wires_every_source_type_from_a_full_config():
    cfg = _full_config()
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="anthropic-key",
        brave_api_key="brave-key",
        bluesky_handle="bot.bsky.social",
        bluesky_app_password="app-password",  # noqa: S106 -- test fixture, not a real secret
    )

    collectors = build_collectors(cfg, secrets)

    source_types = [c.source_type for c in collectors]
    assert source_types == ["rss", "steam_news", "bluesky_search", "web_search"]
    assert isinstance(collectors[0], RssCollector)
    assert isinstance(collectors[1], SteamCollector)
    from newsbot.collectors.bluesky import BlueskyCollector, BlueskySession
    from newsbot.collectors.web_search import WebSearchCollector

    assert isinstance(collectors[2], BlueskyCollector)
    assert isinstance(collectors[2]._session, BlueskySession)  # auth is wired through
    assert isinstance(collectors[3], WebSearchCollector)


def test_build_collectors_skips_web_search_without_brave_key():
    cfg = _full_config()
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="anthropic-key",
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )

    collectors = build_collectors(cfg, secrets)

    assert "web_search" not in [c.source_type for c in collectors]
    assert len(collectors) == 3


def test_build_collectors_bluesky_has_no_session_without_bluesky_secrets():
    cfg = AppConfig(
        guild_id=1,
        digest=DigestCfg(channel_id=1, time="09:00", timezone="UTC"),
        topics=[Topic(key="palworld", name="Palworld")],
        sources=[BlueskySource(type="bluesky_search", query="Palworld", trust="community")],
    )
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="anthropic-key",
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )

    collectors = build_collectors(cfg, secrets)

    assert len(collectors) == 1
    assert collectors[0]._session is None
