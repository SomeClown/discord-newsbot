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
    GameInfo,
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


# What a rate-limited and a backed-off result say in `CollectorResult.skipped`.
SKIP_RATE_LIMITED = "rate limited"
SKIP_BACKED_OFF = "backed off"

# Retry-After is the other party's say-so, and "come back in a week" shouldn't
# park a source for a week. Six hours is four missed passes at the default
# interval, which is plenty of manners for one subreddit.
MAX_BACKOFF_S = 6 * 3600.0


class RateLimited(QuotaExceeded):
    """Raised when a rate-limited host says 429 and we're done asking for now.

    It's a skip, not a failure: nothing is wrong with the source, we were
    just too eager. `run_collectors` also takes it as the cue to stop sending
    anything else to the same `rate_limit_key` for the rest of the call.
    `retry_after_s` is the host's `Retry-After`, when it gave one.
    """

    def __init__(self, retry_after_s: float | None = None) -> None:
        super().__init__(SKIP_RATE_LIMITED)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class RawItem:
    """One collected item, before normalization, dedupe or topic matching.

    `topics` restricts (or, for a single-topic source, assigns) which
    topics this item can match: `None` means "let keyword matching
    against every topic decide" (see `pipeline/filter.py`, SPEC-DEV 3).

    `full_text` is the untruncated companion to `excerpt`, added for the
    SHiFT code sweep (design.md §12): a code five paragraphs into a
    patch-notes post never shows up in a 500-character excerpt. It's
    memory-only: `compare=False` and `repr=False` keep it out of
    equality checks and log lines, and `StoredItem` (what actually reaches
    the database) has no field for it at all, so there's no code path that
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
    # Set with `skipped == SKIP_RATE_LIMITED` when the host sent a usable Retry-After.
    retry_after_s: float | None = None


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
    except RateLimited as exc:
        return CollectorResult(
            collector.name,
            collector.source_type,
            [],
            skipped=SKIP_RATE_LIMITED,
            retry_after_s=exc.retry_after_s,
        )
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
    returned. Threading one `RateLimitState` through every call (the
    daily job's and every sweep's alike) is what makes the gap a real
    cross-call throttle instead of a fresh burst every hour.
    """

    last_fetch: dict[str, float] = field(default_factory=dict)
    # key -> the `clock()` reading before which that key gets no requests,
    # set when a host's 429 came with a Retry-After. Lives as long as the
    # process, like `last_fetch`: a restart forgets it, and the cost of that
    # is one more polite 429.
    backoff_until: dict[str, float] = field(default_factory=dict)


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
    apart, but that group itself still runs concurrently with every
    other group, so a throttled Reddit fetch doesn't hold up an RSS feed
    that has nothing to do with it.

    Without `rate_limit_state` (the default, and today's behavior), the
    gap only applies *within* one call: the first collector in a group
    never waits, and the clock resets to zero the next time this function
    is called. With a `rate_limit_state`, every keyed collector (the
    first in its group included) waits out whatever's left of the gap
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
        halted = False
        for i, collector in enumerate(members):
            if halted or (
                rate_limit_state is not None
                and clock() < rate_limit_state.backoff_until.get(key, float("-inf"))
            ):
                # Backed off: not a failure, and it never touched the host.
                results.append(
                    CollectorResult(
                        collector.name, collector.source_type, [], skipped=SKIP_BACKED_OFF
                    )
                )
                continue
            if rate_limit_state is not None:
                last = rate_limit_state.last_fetch.get(key)
                if last is not None:
                    wait = rate_limit_gap_s - (clock() - last)
                    if wait > 0:
                        await sleep(wait)
            elif i:
                await sleep(rate_limit_gap_s)
            result = await _run_one(collector, http, timeout_s)
            results.append(result)
            if rate_limit_state is not None:
                rate_limit_state.last_fetch[key] = clock()
            if result.skipped == SKIP_RATE_LIMITED:
                # One 429 and the whole key sits down; hammering on is how
                # a polite 429 turns into a long one.
                halted = True
                if rate_limit_state is not None and result.retry_after_s:
                    wait_s = min(result.retry_after_s, MAX_BACKOFF_S)
                    rate_limit_state.backoff_until[key] = clock() + wait_s
        return results

    async def run_solo(collector: Collector) -> list[CollectorResult]:
        return [await _run_one(collector, http, timeout_s)]

    tasks = [run_keyed_group(key, members) for key, members in keyed.items()]
    tasks += [run_solo(collector) for collector in unkeyed]
    grouped = await asyncio.gather(*tasks)
    return [result for group in grouped for result in group]


def build_catalog_collectors(
    cfg: AppConfig,
    secrets: Secrets,
    *,
    include_web_search: bool = True,
    web_search_games: Sequence[GameInfo] | None = None,
) -> list[Collector]:
    """Build a collector for every catalog and shared source, each tagged with its game.

    A source listed under a game gets that game as its `topics`, so its
    items are confident matches for it (design.md §4, unchanged). A shared
    source gets its `games` restriction as `topics`, or none, and the keyword
    matcher decides.

    Web search is built only if `BRAVE_API_KEY` is set (same second line of
    defense as above), and searches `web_search_games`, which defaults to
    the whole catalog; the daily job narrows it to comped servers' games.
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

    def one(source: RssSource | SteamSource | BlueskySource) -> Collector:
        if isinstance(source, RssSource):
            return RssCollector(source)
        if isinstance(source, SteamSource):
            return SteamCollector(source)
        return BlueskyCollector(source, bluesky_session)

    collectors: list[Collector] = []
    for game in cfg.catalog:
        for source in game.sources:
            if isinstance(source, WebSearchSource):
                continue  # load_config already rejected this; belt and braces
            collectors.append(one(source.model_copy(update={"topics": [game.key]})))
    for shared in cfg.shared_sources:
        if isinstance(shared, WebSearchSource):
            continue
        collectors.append(one(shared.model_copy(update={"topics": shared.games})))

    if include_web_search and cfg.web_search is not None and secrets.brave_api_key:
        as_source = WebSearchSource(
            type="web_search",
            name=cfg.web_search.name,
            queries_per_topic=cfg.web_search.queries_per_game,
            query_templates=cfg.web_search.query_templates,
            trust=cfg.web_search.trust,
        )
        games = cfg.catalog if web_search_games is None else web_search_games
        collectors.append(
            WebSearchCollector(as_source, games, secrets.brave_api_key.get_secret_value())
        )
    return collectors
