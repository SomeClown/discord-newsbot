"""Brave News search collector, for the single `web_search` source.

Unlike the other collector types, one configured source here expands into
several requests: `queries_per_topic` templates, formatted per topic, so
three topics and two templates means six searches. Brave's free tier is
good for about one request a second, so those six run one after another
with a pause between them, not concurrently; there's no `httpx` retry
for "the whole run got 429'd because we fired six requests at once".
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import httpx

from newsbot import netguard, text
from newsbot.collectors.base import QuotaExceeded, RawItem
from newsbot.config import GameInfo, WebSearchSource

_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
_QUERY_GAP_S = 1.1

# Brave's proxy URL for a thumbnail carries the original image after this segment.
_ORIGIN_MARKER = "/g:ce/"
# Reddit's media-only domain: preview, external-preview, cf.preview, i, v and whatever it
# grows next. I first listed hosts one by one, and then a cf.preview one walked right past me.
_REDDIT_MEDIA_DOMAIN = "redd.it"
# Bing's thumbnail service: ts1, ts2, tse1 and friends, all answering at /th?q=<title>.
_BING_THUMB_SUFFIX = ".mm.bing.net"
_BING_THUMB_PATH = "/th"

logger = logging.getLogger(__name__)


def _parse_page_age(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_excluded(url: str, domains: Sequence[str]) -> bool:
    """Whether `url`'s host is one of `domains` or a subdomain of one.

    Hosts compare the way the rest of the bot compares them (IDNA 2008, lowercased),
    with a trailing dot ignored. A URL whose host can't be read isn't excluded here;
    the other checks can have it.
    """
    try:
        host = netguard.host_name(httpx.URL(url)).removesuffix(".")
    except httpx.InvalidURL:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _thumbnail_origin(result: dict) -> httpx.URL | None:
    """The URL a result's thumbnail was copied from, or None if it can't be read.

    Brave only sends its own proxy URL, with the original image base64url-encoded after
    `/g:ce/`, with a `/` inserted every 16 characters. Nobody promised me that format, so
    anything that doesn't decode cleanly to an http(s) URL with a host is None, and None
    means "nothing to say about this result".
    """
    thumbnail = result.get("thumbnail")
    if not isinstance(thumbnail, dict):
        return None
    src = thumbnail.get("src")
    if not isinstance(src, str):
        return None
    _, marker, encoded = src.partition(_ORIGIN_MARKER)
    encoded = encoded.replace("/", "")
    if not (marker and encoded):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        origin = httpx.URL(decoded)
    except ValueError, httpx.InvalidURL:
        # ValueError is on purpose: bad base64 (binascii.Error), bytes that aren't UTF-8
        # (UnicodeDecodeError) and a non-ASCII str all land here. Narrow it to
        # binascii.Error and the other two crash the whole search.
        return None
    if origin.scheme not in ("http", "https") or not origin.raw_host:
        return None
    return origin


def _is_farm_thumbnail(result: dict) -> bool:
    """Whether the result's picture is hotlinked from Reddit's media domain or Bing's thumbnails.

    Reddit's is `redd.it` and every subdomain of it; Bing's is `*.mm.bing.net` under `/th`.

    The content-farm pages that turn up for small games (a language model's rewrite of a
    Reddit thread, on a domain that used to sell wine) name the game, so the name check keeps
    them. What they can't fake is that they have no pictures of their own: they borrow
    Reddit's or Bing's. Real press hosts its own images, and none of the real results I
    looked at did this. Hosts compare the way `_is_excluded` does them.
    """
    origin = _thumbnail_origin(result)
    if origin is None:
        return False
    host = netguard.host_name(origin).removesuffix(".")
    # httpx escapes what it can't parse as a host (a space, a stray bracket) instead of
    # refusing it, and "redd.it" at the end of that is no proof of anything. Fail open.
    if "%" in host or "" in host.split("."):
        return False
    if host == _REDDIT_MEDIA_DOMAIN or host.endswith("." + _REDDIT_MEDIA_DOMAIN):
        return True
    return host.endswith(_BING_THUMB_SUFFIX) and origin.path.startswith(_BING_THUMB_PATH)


class WebSearchCollector:
    source_type = "web_search"
    rate_limit_key = None

    def __init__(
        self,
        source: WebSearchSource,
        topics: Sequence[GameInfo],
        api_key: str,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._source = source
        self._topics = topics
        self._api_key = api_key
        self._sleep = sleep or asyncio.sleep
        self.name = source.name

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        items: list[RawItem] = []
        first = True
        for topic in self._topics:
            queries = self._queries_for(topic)
            for query in queries:
                if not first:
                    await self._sleep(_QUERY_GAP_S)
                first = False
                items.extend(await self._search(http, query, topic.key))
        return items

    def _queries_for(self, topic: GameInfo) -> list[str]:
        # A topic with its own search_queries (BL4's press coverage never
        # says "Borderlands 4 news" the way the default template guesses)
        # skips the templates entirely, still capped at queries_per_topic.
        # Everyone else falls back to the shared templates, formatted per
        # topic name, same as before this existed.
        if topic.search_queries:
            return topic.search_queries[: self._source.queries_per_topic]
        templates = self._source.query_templates[: self._source.queries_per_topic]
        return [template.format(name=topic.name) for template in templates]

    async def _search(self, http: httpx.AsyncClient, query: str, topic_key: str) -> list[RawItem]:
        response = await http.get(
            _NEWS_URL,
            params={"q": query, "freshness": "pd", "count": 20},
            headers={"X-Subscription-Token": self._api_key},
        )
        if response.status_code in (429, 402):
            raise QuotaExceeded("quota")
        response.raise_for_status()

        data = response.json()
        results = []
        farm_dropped = 0
        for result in data.get("results", []):
            if not isinstance(result, dict):
                # Brave's shape is trusted about as far as any third-party
                # API's is: a malformed entry (not even an object) skips,
                # rather than taking the rest of this query's results with
                # it.
                continue
            url = result.get("url")
            title = result.get("title")
            if not (url and title):
                continue
            # A truthy title that isn't text (2026, true) is just as malformed, and
            # the matcher downstream would rather not be handed a number to read.
            if not (isinstance(url, str) and isinstance(title, str)):
                continue
            # Some sites stamp every page with the moment it's served, and Brave's
            # `page_age` believes them (a June article looked a day old). Clearing the
            # date wouldn't help: an undated item is kept. So the result goes.
            if _is_excluded(url, self._source.exclude_domains):
                continue
            # The page borrows its picture from Reddit or Bing; real press hosts its own
            # (see `_is_farm_thumbnail`). The name check can't tell, since the farm names the game.
            if _is_farm_thumbnail(result):
                farm_dropped += 1
                continue
            # `.get(..., "")` only covers a *missing* key; Brave has sent
            # an explicit `"description": null` in the wild, which sails
            # right past that default and into `clean_text(None)`, which
            # doesn't have a good day.
            description = result.get("description") or ""
            results.append(
                RawItem(
                    url=url,
                    title=title,
                    excerpt=text.clean_text(description),
                    source_name=self._source.name,
                    trust=self._source.trust,
                    published_at=_parse_page_age(result.get("page_age")),
                    topics=(topic_key,),
                    # The query that found this isn't a vote that it's on topic.
                    needs_match=True,
                    # Brave only ever gives us a snippet, never full-page
                    # text: this is the same source `excerpt` is built
                    # from, just uncapped at 500 characters, not some
                    # additional fetch of the whole article.
                    full_text=text.plain_text(description) if description else None,
                )
            )
        if farm_dropped:
            logger.info(
                "%s: dropped %d content-farm result(s) for %s",
                self._source.name,
                farm_dropped,
                topic_key,
                extra={
                    "source_name": self._source.name,
                    "topic": topic_key,
                    "farm_dropped": farm_dropped,
                },
            )
        return results
