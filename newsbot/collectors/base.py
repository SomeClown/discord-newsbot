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


async def run_collectors(
    collectors: Sequence[Collector],
    http: httpx.AsyncClient,
    timeout_s: float = 20.0,
    *,
    rate_limit_gap_s: float = _DEFAULT_RATE_LIMIT_GAP_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[CollectorResult]:
    """Run every collector, concurrently except within a shared `rate_limit_key`.

    Collectors with no `rate_limit_key` (or a `None` one) each get their own
    solo group and run alongside everything else. Collectors that share a
    key (currently just the Reddit sources) run one at a time within their
    group, `rate_limit_gap_s` apart -- but that group itself still runs
    concurrently with every other group, so a throttled Reddit fetch
    doesn't hold up an RSS feed that has nothing to do with it.
    """
    groups: dict[object, list[Collector]] = {}
    for collector in collectors:
        key = getattr(collector, "rate_limit_key", None)
        groups.setdefault(key if key is not None else object(), []).append(collector)

    async def run_group(members: list[Collector]) -> list[CollectorResult]:
        results = []
        for i, collector in enumerate(members):
            if i:
                await sleep(rate_limit_gap_s)
            results.append(await _run_one(collector, http, timeout_s))
        return results

    grouped = await asyncio.gather(*(run_group(members) for members in groups.values()))
    return [result for group in grouped for result in group]


def build_collectors(cfg: AppConfig, secrets: Secrets) -> list[Collector]:
    """Turn every configured source into its matching collector.

    A `web_search` source with no `BRAVE_API_KEY` is skipped here too, as a
    second line of defense -- `config.py` already warns and is expected to
    have dropped it, but a collector built without a key it needs would
    just fail on every run instead of being invisible, which is worse.
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
            if not secrets.brave_api_key:
                continue
            collectors.append(
                WebSearchCollector(source, cfg.topics, secrets.brave_api_key.get_secret_value())
            )
    return collectors
