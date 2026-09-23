"""RSS/Atom collector: blogs, subreddits, YouTube channel feeds, all through one parser.

`feedparser` is happy to fetch a URL itself, but then httpx never sees the
request, which means no shared client, no timeout, and no way to hand it a
committed fixture in a test. So this module fetches the bytes with httpx
and hands them to `feedparser.parse` cold. The one wrinkle worth a comment:
Reddit 429s a burst of requests even from a well-behaved single client, so
a source whose host is reddit.com gets tagged with a `rate_limit_key` that
`run_collectors` uses to space its requests out, and a couple of retries
with backoff here besides.
"""

from __future__ import annotations

import asyncio
import calendar
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import struct_time

import feedparser
import httpx

from newsbot import text
from newsbot.collectors.base import RawItem
from newsbot.config import RssSource

_USER_AGENT = "discord-newsbot/1.0 (+contact)"
_HEADERS = {"User-Agent": _USER_AGENT}
_REDDIT_HOSTS = frozenset({"www.reddit.com", "reddit.com", "old.reddit.com"})

# Reddit's 429 usually clears after a short pause; a couple of retries here
# is cheap insurance on top of the per-host gap `run_collectors` already
# adds between Reddit sources.
_RETRY_BACKOFFS_S = (5.0, 15.0)


async def _fetch(
    http: httpx.AsyncClient,
    url: str,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> httpx.Response:
    response = await http.get(url, headers=_HEADERS, follow_redirects=True)
    for backoff in _RETRY_BACKOFFS_S:
        if response.status_code != 429:
            break
        await sleep(backoff)
        response = await http.get(url, headers=_HEADERS, follow_redirects=True)
    return response


def _entry_excerpt(entry: feedparser.FeedParserDict) -> str:
    content = entry.get("content")
    if content:
        value = content[0].get("value", "")
        if value:
            return value
    return entry.get("summary") or entry.get("description") or ""


def _entry_published(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)


class RssCollector:
    source_type = "rss"

    def __init__(
        self,
        source: RssSource,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._source = source
        self.name = source.name
        self._sleep = sleep or asyncio.sleep
        host = httpx.URL(str(source.url)).host
        self.rate_limit_key = "reddit" if host in _REDDIT_HOSTS else None

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        response = await _fetch(http, str(self._source.url), sleep=self._sleep)
        response.raise_for_status()

        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"feed did not parse: {parsed.get('bozo_exception')}")

        topics = tuple(self._source.topics) if self._source.topics else None
        items = []
        for entry in parsed.entries:
            url = entry.get("link")
            if not url:
                continue
            raw_excerpt = _entry_excerpt(entry)
            title = (entry.get("title") or "").strip()
            if not title:
                # Official Bluesky accounts come in through their RSS
                # mirror, and a post doesn't really have a headline --
                # feedparser gives us a bare description instead.
                title = text.first_line(raw_excerpt) or "(untitled)"
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    excerpt=text.clean_text(raw_excerpt),
                    source_name=self._source.name,
                    trust=self._source.trust,
                    published_at=_entry_published(entry),
                    topics=topics,
                )
            )
        return items
