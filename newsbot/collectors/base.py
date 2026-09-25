"""Shared collector types, and the orchestration that runs all of them.

A `Collector` is anything that can turn one configured source into a list
of `RawItem`s. That's the whole contract: fetch, parse, return, and let the
caller worry about timeouts, concurrency and what to do when a source is
having a bad day. `run_collectors` is that caller. It's also where the
Reddit problem lives: fire five subreddit feeds at once and four of them
come back 429, so collectors that share a `rate_limit_key` get throttled to
one at a time, while everything else still runs concurrently.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

from newsbot.config import (
    AppConfig,
    BlueskySource,
    RssSource,
    Secrets,
    SteamSource,
    Trust,
    WebSearchSource,
)

# Default gap between requests that share a `rate_limit_key`. Reddit still
# 429s a burst of requests from a single well-behaved client even from a
# residential IP (see docs/sources-research.md); ~30-45s between requests
# cleared it in testing.
_DEFAULT_RATE_LIMIT_GAP_S = 35.0


class QuotaExceeded(Exception):
    """Raised by a collector to say "skip me this run", not "I failed".

    The message becomes `CollectorResult.skipped` (e.g. "quota", "auth"),
    which turns into a coverage note in the digest header rather than a
    source-health failure. Brave raises this on a 429/402; Bluesky raises
    it when an unauthenticated search gets a 401/403.
    """


@dataclass(frozen=True, slots=True)
class RawItem:
    """One collected item, before normalization, dedupe or topic matching.

    `topics` restricts (or, for a single-topic source, assigns) which
    topics this item can match -- `None` means "let keyword matching
    against every topic decide" (see `pipeline/filter.py`, SPEC-DEV 3).

    `full_text` is the untruncated companion to `excerpt`, added for the
    SHiFT code sweep (design.md §12): a code five paragraphs into a
    patch-notes post never shows up in a 500-character excerpt. It's
    memory-only -- `compare=False` and `repr=False` keep it out of
    equality checks and log lines, and `StoredItem` (what actually reaches
    `save_run`) has no field for it at all, so there's no code path that
    could persist it or hand it to the LLM even by accident.
    """

    url: str
    title: str
    excerpt: str
    source_name: str
    trust: Trust
    published_at: datetime | None
    topics: tuple[str, ...] | None = None
    full_text: str | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class CollectorResult:
    """The outcome of running one collector: its items, or why there aren't any."""

    source_name: str
    source_type: str
    items: list[RawItem]
    error: str | None = None  # a real failure -> counts against source_health
    skipped: str | None = None  # e.g. "quota", "auth" -> a coverage note, not a failure


class Collector(Protocol):
    name: str
    source_type: str
    rate_limit_key: str | None

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        """Fetch and parse this source. Raise on failure; never return partial silence."""
        ...


async def _run_one(
    collector: Collector, http: httpx.AsyncClient, timeout_s: float
) -> CollectorResult:
    try:
        async with asyncio.timeout(timeout_s):
            items = await collector.collect(http)
        return CollectorResult(collector.name, collector.source_type, items)
    except QuotaExceeded as exc:
        return CollectorResult(
            collector.name, collector.source_type, [], skipped=str(exc) or "quota"
        )
    except TimeoutError:
        return CollectorResult(
            collector.name, collector.source_type, [], error=f"timed out after {timeout_s}s"
        )
    except Exception as exc:  # a collector's own bug must never take the whole run down
        return CollectorResult(
            collector.name, collector.source_type, [], error=str(exc) or type(exc).__name__
        )


@dataclass
class RateLimitState:
    """The one piece of state a Reddit-shaped rate limit needs across calls.

    `run_collectors`' old in-call-only gap (space members of one group
    `rate_limit_gap_s` apart, reset every call) is exactly right for one
    daily run, but the SHiFT alert sweep (design.md §12) calls
    `run_collectors` every `interval_minutes` from the same process, and
    Reddit doesn't reset its patience just because the previous call
    returned. Threading one `RateLimitState` through every call -- the
    daily job's and every sweep's alike -- is what makes the gap a real
    cross-call throttle instead of a fresh burst every hour.
    """

    last_fetch: dict[str, float] = field(default_factory=dict)


async def run_collectors(
    collectors: Sequence[Collector],
    http: httpx.AsyncClient,
    timeout_s: float = 20.0,
    *,
    rate_limit_gap_s: float = _DEFAULT_RATE_LIMIT_GAP_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rate_limit_state: RateLimitState | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[CollectorResult]:
    """Run every collector, concurrently except within a shared `rate_limit_key`.

    Collectors with no `rate_limit_key` (or a `None` one) each get their own
    solo group and run alongside everything else, and never wait on
    anything. Collectors that share a key (currently just the Reddit
    sources) run one at a time within their group, `rate_limit_gap_s`
    apart -- but that group itself still runs concurrently with every
    other group, so a throttled Reddit fetch doesn't hold up an RSS feed
    that has nothing to do with it.

    Without `rate_limit_state` (the default, and today's behavior), the
    gap only applies *within* one call: the first collector in a group
    never waits, and the clock resets to zero the next time this function
    is called. With a `rate_limit_state`, every keyed collector -- the
    first in its group included -- waits out whatever's left of the gap
    since that key's *last* fetch, tracked across calls. That's what lets
    the hourly sweep and the daily job share one Reddit throttle instead
    of each starting a fresh burst.
    """
    keyed: dict[str, list[Collector]] = {}
    unkeyed: list[Collector] = []
    for collector in collectors:
        key = getattr(collector, "rate_limit_key", None)
        if key is None:
            unkeyed.append(collector)
        else:
            keyed.setdefault(key, []).append(collector)

    async def run_keyed_group(key: str, members: list[Collector]) -> list[CollectorResult]:
        results = []
        for i, collector in enumerate(members):
            if rate_limit_state is not None:
                last = rate_limit_state.last_fetch.get(key)
                if last is not None:
                    wait = rate_limit_gap_s - (clock() - last)
                    if wait > 0:
                        await sleep(wait)
            elif i:
                await sleep(rate_limit_gap_s)
            results.append(await _run_one(collector, http, timeout_s))
            if rate_limit_state is not None:
                rate_limit_state.last_fetch[key] = clock()
        return results

    async def run_solo(collector: Collector) -> list[CollectorResult]:
        return [await _run_one(collector, http, timeout_s)]

    tasks = [run_keyed_group(key, members) for key, members in keyed.items()]
    tasks += [run_solo(collector) for collector in unkeyed]
    grouped = await asyncio.gather(*tasks)
    return [result for group in grouped for result in group]


def build_collectors(
    cfg: AppConfig, secrets: Secrets, *, include_web_search: bool = True
) -> list[Collector]:
    """Turn every configured source into its matching collector.

    A `web_search` source with no `BRAVE_API_KEY` is skipped here too, as a
    second line of defense -- `config.py` already warns and is expected to
    have dropped it, but a collector built without a key it needs would
    just fail on every run instead of being invisible, which is worse.

    `include_web_search=False` is the SHiFT alert sweep's own reason to
    call this (design.md §12): Brave News has a modest free allowance, and
    an hourly sweep calling it 24x a day on top of the daily digest's own
    calls would eat through it for a collector type that's the least
    likely place to find a redeem code anyway.
    """
    from newsbot.collectors.bluesky import BlueskyCollector, BlueskySession
    from newsbot.collectors.rss import RssCollector
    from newsbot.collectors.steam import SteamCollector
    from newsbot.collectors.web_search import WebSearchCollector

    bluesky_session = None
    if secrets.bluesky_handle and secrets.bluesky_app_password:
        bluesky_session = BlueskySession(
            secrets.bluesky_handle, secrets.bluesky_app_password.get_secret_value()
        )

    collectors: list[Collector] = []
    for source in cfg.sources:
        if isinstance(source, RssSource):
            collectors.append(RssCollector(source))
        elif isinstance(source, SteamSource):
            collectors.append(SteamCollector(source))
        elif isinstance(source, BlueskySource):
            collectors.append(BlueskyCollector(source, bluesky_session))
        elif isinstance(source, WebSearchSource):
            if not include_web_search or not secrets.brave_api_key:
                continue
            collectors.append(
                WebSearchCollector(source, cfg.topics, secrets.brave_api_key.get_secret_value())
            )
    return collectors
