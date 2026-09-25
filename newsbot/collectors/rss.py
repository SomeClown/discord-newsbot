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
import ipaddress
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
# adds between Reddit sources. Kept well under pipeline/run.py's 20s
# per-collector timeout (_COLLECT_TIMEOUT_S) -- the old (5.0, 15.0) pair
# summed to 20s in sleeps *alone*, before any request had actually gone
# out, which meant the third (and only successful) attempt could never
# land inside its own budget. Found the hard way: a "retries and succeeds"
# path that could never actually succeed under the real timeout.
_RETRY_BACKOFFS_S = (3.0, 6.0)

# A feed has no business being bigger than this. Protects against a huge
# or malicious response parking this collector on an unbounded read --
# the truncated body just fails to parse as valid XML/Atom, which comes
# back as the same "feed did not parse" error a genuinely broken feed
# gives.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


async def _reject_private_redirect(response: httpx.Response) -> None:
    """Raise if `response.url`'s host resolves to a private/loopback/link-local address.

    `follow_redirects=True` means whatever server first answered a feed
    URL gets to redirect this client anywhere it wants -- including, if
    nothing stopped it, the cloud metadata endpoint at
    `169.254.169.254` or a service only reachable from inside this
    container's own network. This runs after redirects are followed
    (`response.url` is the final URL), so it catches a redirect chain
    that *ends* somewhere it shouldn't, not just an initially-configured
    bad URL (config-time URLs are an owner's own problem to get right).
    """
    host = response.url.host
    if not host:
        return
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        # Not a literal IP -- resolve it. A DNS failure here isn't this
        # function's problem to report; raise_for_status()/feedparser
        # will have their own opinion about a response that never came.
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, None)
        except OSError:
            return
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addresses:
        # `is_global` rather than a list of is_private/is_loopback/...: the
        # list missed CGNAT space (100.64.0.0/10), and I'd rather let the
        # stdlib keep the catalogue of non-public ranges than keep it myself.
        if not addr.is_global:
            raise ValueError(f"redirected to a non-public host: {host} ({addr})")


async def _fetch_body(
    http: httpx.AsyncClient,
    url: str,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> bytes:
    """Fetch `url`'s body, streamed and capped at `_MAX_RESPONSE_BYTES`, retrying 429s."""
    attempts = 1 + len(_RETRY_BACKOFFS_S)
    for attempt in range(attempts):
        if attempt:
            await sleep(_RETRY_BACKOFFS_S[attempt - 1])
        async with http.stream("GET", url, headers=_HEADERS, follow_redirects=True) as response:
            await _reject_private_redirect(response)
            if response.status_code == 429 and attempt < attempts - 1:
                continue
            response.raise_for_status()
            chunks = []
            total = 0
            async for chunk in response.aiter_bytes():
                remaining = _MAX_RESPONSE_BYTES - total
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    # Slice rather than just stop after this chunk -- a
                    # transport is free to hand back the whole body as
                    # one chunk (a test double does exactly that), and
                    # appending it whole would blow the cap it was just
                    # about to enforce.
                    chunks.append(chunk[:remaining])
                    break
                chunks.append(chunk)
                total += len(chunk)
            return b"".join(chunks)
    raise AssertionError("unreachable: the loop above always returns or raises")


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
        body = await _fetch_body(http, str(self._source.url), sleep=self._sleep)
        # feedparser.parse() and clean_text() are both plain synchronous
        # CPU work (XML parsing, regex-based HTML stripping) -- running
        # them straight on the event loop would stall every other
        # collector and the gateway's heartbeat for however long a large
        # feed takes to chew through. One to_thread call for the whole
        # batch, not one per entry: entries share nothing that needs the
        # event loop back in between.
        return await asyncio.to_thread(self._parse_items, body)

    def _parse_items(self, body: bytes) -> list[RawItem]:
        parsed = feedparser.parse(body)
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
