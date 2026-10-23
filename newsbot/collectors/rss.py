"""RSS/Atom collector: blogs, subreddits, YouTube channel feeds, all through one parser.

`feedparser` is happy to fetch a URL itself, but then httpx never sees the
request, which means no shared client, no timeout, and no way to hand it a
committed fixture in a test. So this module fetches the bytes with httpx
and hands them to `feedparser.parse` cold. The one wrinkle worth a comment:
Reddit 429s a burst of requests even from a well-behaved single client, so
a source whose host is reddit.com gets tagged with a `rate_limit_key` that
`run_collectors` uses to space its requests out. Other hosts get a couple of
retries with backoff here; Reddit doesn't, because a 429 from it means "stop
asking" and `run_collectors` is the one that acts on it (see `RateLimited`).
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import struct_time

import feedparser
import httpx

from newsbot import text
from newsbot.collectors.base import RateLimited, RawItem
from newsbot.config import RssSource
from newsbot.netguard import host_name, reject_private_host, reject_private_peer
from newsbot.reddit import is_reddit_host
from newsbot.useragent import user_agent_headers

logger = logging.getLogger(__name__)

_HEADERS = user_agent_headers()

# A non-Reddit host's 429 usually clears after a short pause; a couple of
# retries here is cheap insurance. (Reddit's retries used to live here too;
# they were three more requests to a host that had just asked us to stop.)
# Kept well under pipeline/run.py's 20s
# per-collector timeout (_COLLECT_TIMEOUT_S): the old (5.0, 15.0) pair
# summed to 20s in sleeps *alone*, before any request had actually gone
# out, which meant the third (and only successful) attempt could never
# land inside its own budget. Found the hard way: a "retries and succeeds"
# path that could never actually succeed under the real timeout.
_RETRY_BACKOFFS_S = (3.0, 6.0)

# A feed has no business being bigger than this. Protects against a huge
# or malicious response parking this collector on an unbounded read;
# the truncated body just fails to parse as valid XML/Atom, which comes
# back as the same "feed did not parse" error a genuinely broken feed
# gives.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Redirect hops `_fetch` follows by hand before giving up, matching the lounge's
# `MAX_REDIRECTS`. Five is plenty for a feed that moved.
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _redirect_target(current: httpx.URL, response: httpx.Response) -> httpx.URL:
    """Where a redirect response sends us next, or a ValueError saying why it can't."""
    sender = host_name(current)
    location = response.headers.get("location")
    if not location:
        raise ValueError(f"{sender} answered HTTP {response.status_code} with no Location")
    target = current.join(location)
    if target.scheme not in ("http", "https"):
        raise ValueError(f"{sender} redirected to a non-http address")
    if current.scheme == "https" and target.scheme == "http":
        raise ValueError(f"{sender} redirected from https to http ({host_name(target)})")
    return target


@asynccontextmanager
async def _stream_hop(
    http: httpx.AsyncClient, url: httpx.URL, headers: Mapping[str, str]
) -> AsyncIterator[httpx.Response]:
    """One GET, streamed, with no redirect followed: that's `_fetch`'s job, hop by hop.

    httpx parses a redirect's `Location` even when told not to follow it, and a junk one
    comes out as a `RemoteProtocolError`, an `InvalidURL` or (a malformed IDNA label like
    `xn--`) a bare `UnicodeError`, none of which says whose redirect it was. Here each
    becomes a `ValueError` naming the host, like every other way a redirect can be wrong.
    """
    try:
        async with http.stream("GET", url, headers=headers, follow_redirects=False) as response:
            yield response
    except httpx.InvalidURL, UnicodeError:
        raise ValueError(
            f"{host_name(url)} redirected to an address that isn't a valid URL"
        ) from None
    except httpx.RemoteProtocolError as exc:
        if str(exc).startswith("Invalid URL in location header"):
            raise ValueError(
                f"{host_name(url)} redirected to an address that isn't a valid URL"
            ) from None
        raise


def _retry_after_s(response: httpx.Response) -> float | None:
    """Retry-After in seconds (a number or an HTTP date), or None if absent or junk."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError:
            return None
    return seconds if seconds > 0 else None


def _describe_retry_after(response: httpx.Response, seconds: float | None) -> str:
    """For the log: seconds if it parsed, the raw header if it didn't, else "none"."""
    if seconds is not None:
        return f"{seconds:.0f}s"
    return response.headers.get("retry-after") or "none"


_CONDITIONAL_HEADERS = frozenset({"if-none-match", "if-modified-since"})


def _asked_for_304(request_headers: Mapping[str, str]) -> bool:
    """Whether the request carried a conditional header, which is what makes a 304 an answer."""
    return any(name.lower() in _CONDITIONAL_HEADERS for name in request_headers)


@dataclass(frozen=True, slots=True)
class Fetched:
    """What `_fetch` got back: the status, the (capped) body and the page's cache validators.

    A 304 comes back with an empty `body`; it's an answer, not an error, but only to a
    caller that sent `If-None-Match` or `If-Modified-Since` (`_asked_for_304`). A 304 to
    anyone else is a server being strange, and it raises like any other non-2xx.
    """

    status: int
    body: bytes
    etag: str | None
    last_modified: str | None


async def _fetch(
    http: httpx.AsyncClient,
    url: str,
    *,
    sleep: Callable[[float], Awaitable[None]],
    retry_429: bool = True,
    source_name: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Fetched:
    """Fetch `url`, streamed and capped at `_MAX_RESPONSE_BYTES`, retrying 429s.

    With `retry_429` off, a 429 raises `RateLimited` straight away instead of
    asking again: that's Reddit's mode, where the right response to "slow
    down" is to stop for the whole pass, not to try again in three seconds.
    `headers` are extras laid over the standard identifying ones (the conditional
    request headers, for the one caller that sends them). A 304 to a request that
    carried one of those is returned, not raised, with an empty body; to any other
    request it's an error (`raise_for_status`), because "nothing changed" is an
    answer to a question nobody asked.

    Redirects are followed by hand, up to `_MAX_REDIRECTS`, and every URL gets its host checked
    before it's requested (`reject_private_host`), the configured one included, so a feed can't
    send the bot to a private address and have the request land first. Where the connection
    actually went is checked too, before any body is read (`reject_private_peer`). Both refuse
    with a `ValueError` that names the host, as does a redirect that loops on, drops from
    https to http, or has no usable `Location`.
    """
    attempts = 1 + len(_RETRY_BACKOFFS_S) if retry_429 else 1
    request_headers = {**_HEADERS, **(headers or {})}
    for attempt in range(attempts):
        if attempt:
            await sleep(_RETRY_BACKOFFS_S[attempt - 1])
        # Every attempt walks the redirects again from the configured URL.
        current = httpx.URL(url)
        redirects = 0
        while True:
            await reject_private_host(current)
            async with _stream_hop(http, current, request_headers) as response:
                reject_private_peer(current, response)
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects == _MAX_REDIRECTS:
                        raise ValueError(
                            f"{host_name(current)} redirected more than {_MAX_REDIRECTS} times"
                        )
                    next_url = _redirect_target(current, response)
                else:
                    if response.status_code == 429 and attempt < attempts - 1:
                        break
                    if response.status_code == 429 and not retry_429:
                        retry_after = _retry_after_s(response)
                        logger.info(
                            "Reddit 429 for %s (%s), Retry-After: %s",
                            source_name or url,
                            url,
                            _describe_retry_after(response, retry_after),
                            extra={"source_name": source_name, "retry_after_s": retry_after},
                        )
                        raise RateLimited(retry_after)
                    etag = response.headers.get("etag")
                    last_modified = response.headers.get("last-modified")
                    if response.status_code == 304 and _asked_for_304(request_headers):
                        return Fetched(304, b"", etag, last_modified)
                    response.raise_for_status()
                    chunks = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        remaining = _MAX_RESPONSE_BYTES - total
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            # Slice rather than just stop after this chunk: a
                            # transport is free to hand back the whole body as
                            # one chunk (a test double does exactly that), and
                            # appending it whole would blow the cap it was just
                            # about to enforce.
                            chunks.append(chunk[:remaining])
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    return Fetched(response.status_code, b"".join(chunks), etag, last_modified)
            # Only a redirect falls out of the block above (everything else
            # returned, raised or broke out to the next attempt), and leaving
            # the block is what closed its response.
            current = next_url
            redirects += 1
    raise AssertionError("unreachable: the loop above always returns or raises")


async def _fetch_body(
    http: httpx.AsyncClient,
    url: str,
    *,
    sleep: Callable[[float], Awaitable[None]],
    retry_429: bool = True,
    source_name: str | None = None,
) -> bytes:
    """`_fetch`, minus everything but the body: what the feed collectors have always wanted."""
    fetched = await _fetch(http, url, sleep=sleep, retry_429=retry_429, source_name=source_name)
    return fetched.body


def _entry_excerpt(entry: feedparser.FeedParserDict) -> str:
    content = entry.get("content")
    if content:
        value = content[0].get("value", "")
        if value:
            return value
    return entry.get("summary") or entry.get("description") or ""


def _entry_full_text(entry: feedparser.FeedParserDict) -> str:
    """Every bit of body text a feed entry offers, for the SHiFT code matcher.

    `entry.content` can hold more than one representation of the same
    post (text and HTML, say); all of it goes in, on the theory that a
    code buried in any of them is a code worth finding. `summary` is
    appended too, but only when a feed used it for something `content`
    didn't already say: most feeds that have `content` at all just
    repeat the same text in `summary`, and there's no reason to scan it
    twice.
    """
    parts = [value for c in (entry.get("content") or []) if (value := c.get("value"))]
    summary = entry.get("summary") or entry.get("description") or ""
    if summary and summary not in parts:
        parts.append(summary)
    return "\n".join(parts)


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
        self.rate_limit_key = "reddit" if is_reddit_host(host) else None

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        body = await _fetch_body(
            http,
            str(self._source.url),
            sleep=self._sleep,
            retry_429=self.rate_limit_key is None,
            source_name=self.name,
        )
        # feedparser.parse() and clean_text() are both plain synchronous
        # CPU work (XML parsing, regex-based HTML stripping): running
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
                # mirror, and a post doesn't really have a headline;
                # feedparser gives us a bare description instead.
                title = text.first_line(raw_excerpt) or "(untitled)"
            raw_full_text = _entry_full_text(entry)
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    excerpt=text.clean_text(raw_excerpt),
                    source_name=self._source.name,
                    trust=self._source.trust,
                    published_at=_entry_published(entry),
                    topics=topics,
                    full_text=text.plain_text(raw_full_text) if raw_full_text else None,
                )
            )
        return items
