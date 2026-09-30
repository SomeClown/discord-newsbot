"""The hourly shared collection: fetch the whole catalog once, whoever follows it.

v2 collected once a day for one server. v3 has many servers and one
internet, so the fetching got pulled out of the digest: every interval, one
pass walks every catalog game's sources plus the shared ones, canonicalizes,
dedupes, matches items to games and stores the lot tagged by game. A
server's digest later reads what's stored; nothing is fetched at digest time,
and a source is fetched once per pass no matter how many servers follow it.

The part that needs watching is Reddit. Fifteen subreddits at 35 seconds
apart is about nine minutes of sleeping per pass, and Reddit still 429s
some of them at that spacing. That fits inside an hour with room to spare,
but "fits" is doing some work, so there are three safeguards. A pass that's
still running when the next one is due is skipped, never stacked (the run
lock, plus `max_instances=1` and `coalesce` on the job; see
`collection_job_options`). Every pass logs how long it took and warns if
that's more than half the interval. And `estimate_pass_seconds` does the
worst-case arithmetic up front, so a config that can't possibly fit says so.

Source health is global and hourly, which changes what "three failures in a
row" means: v2's threshold was three days, and three hours is just a
Tuesday. The owner hears about a source once, at twelve in a row, and the
counter re-arms on the first success.

This module doesn't touch Discord or the scheduler. SHiFT detection is an
injected hook (a no-op until the per-guild fan-out exists) and nothing calls
`run_collection` from the running bot yet; the cutover does that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from newsbot.collectors.base import (
    _DEFAULT_RATE_LIMIT_GAP_S,
    Collector,
    CollectorResult,
    RateLimitState,
    RawItem,
    build_catalog_collectors,
    run_collectors,
)
from newsbot.config import AppConfig, Secrets, WebSearchSource
from newsbot.pipeline.filter import TopicItem, filter_items
from newsbot.pipeline.lock import run_lock_or_skip
from newsbot.pipeline.normalize import normalize
from newsbot.store.db import connect
from newsbot.store.models import StoredItem
from newsbot.store.repo import existing_urls, record_source_result, record_sweep, store_items

logger = logging.getLogger(__name__)

# D8 (owner, 2026-09-30): alert once at 12 consecutive failed collections,
# half a day at the default interval. "Exactly" 12, not "at least": the
# counter passing through 12 is the one moment worth a message, and a
# success resets it to zero, which re-arms the alert.
SOURCE_FAILURE_ALERT_THRESHOLD = 12

_COLLECT_TIMEOUT_S = 20.0

# A pass that takes more than this share of the interval gets a warning.
_SLOW_PASS_FRACTION = 0.5

# Called with every collected item (before dedupe, so a code edited into an
# already-seen Reddit thread still counts) and the raw results; returns how
# many new SHiFT codes it found. Task 5 supplies the real one.
ShiftHook = Callable[[list[RawItem], list[CollectorResult]], Awaitable[int]]


async def _no_shift(_items: list[RawItem], _results: list[CollectorResult]) -> int:
    return 0


@dataclass
class CollectionDeps:
    cfg: AppConfig
    db_path: str
    http: httpx.AsyncClient
    collectors: list[Collector]
    now: Callable[[], datetime]
    # The owner's alert channel. Never a guild's.
    alert: Callable[[str], Awaitable[None]]
    # One state for the life of the process, so Reddit's gap survives from
    # one pass to the next instead of starting a fresh burst every hour.
    rate_limit_state: RateLimitState = field(default_factory=RateLimitState)
    shift_hook: ShiftHook = _no_shift
    # Injected so the pacing tests can run a nine-minute pass in no time.
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class CollectionOutcome:
    """What one pass did. `skipped` means it never started (the run lock was held)."""

    skipped: bool = False
    sources_ok: int = 0
    sources_total: int = 0  # sources that ran; quota skips aren't counted
    new_items: int = 0
    new_codes: int = 0
    duration_s: float = 0.0
    failing_sources: tuple[str, ...] = ()
    alerted_sources: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        word = "code" if self.new_codes == 1 else "codes"
        return (
            f"{self.sources_ok}/{self.sources_total} sources ok, "
            f"{self.new_items} new items, {self.new_codes} new {word}"
        )


@dataclass(frozen=True)
class PassEstimate:
    """The Reddit-shaped arithmetic: how long a pass takes, typically and at worst."""

    throttled_feeds: int
    typical_s: float
    worst_s: float


def estimate_pass_seconds(
    collectors: Sequence[Collector],
    *,
    gap_s: float = _DEFAULT_RATE_LIMIT_GAP_S,
    timeout_s: float = _COLLECT_TIMEOUT_S,
) -> PassEstimate:
    """Estimate a pass's length from its largest rate-limited group.

    Groups run side by side, so the longest one sets the pace: n feeds means
    n - 1 gaps (the first one doesn't wait on a fresh process). Typical
    assumes instant fetches; worst assumes every fetch burns its whole
    timeout, and the first one also waits out a full gap left over from the
    previous pass. Fifteen subreddits: 490 s and 825 s.
    """
    groups: dict[str, int] = defaultdict(int)
    for collector in collectors:
        key = getattr(collector, "rate_limit_key", None)
        if key is not None:
            groups[key] += 1
    biggest = max(groups.values(), default=0)
    if biggest == 0:
        return PassEstimate(0, 0.0, timeout_s)
    return PassEstimate(
        throttled_feeds=biggest,
        typical_s=(biggest - 1) * gap_s,
        worst_s=gap_s + biggest * timeout_s + (biggest - 1) * gap_s,
    )


def collection_job_options(cfg: AppConfig) -> dict[str, object]:
    """The scheduler settings the cutover should use for this job.

    `max_instances=1` and `coalesce` are the scheduler's half of "never
    stack passes"; `run_collection` skipping on a held lock is the other
    half, and it holds even if somebody forgets these. First run two minutes
    after startup, per the plan.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    interval = timedelta(minutes=cfg.collection.interval_minutes)
    return {
        "trigger": IntervalTrigger(minutes=cfg.collection.interval_minutes),
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": int(interval.total_seconds() // 2),
    }


def build_collection_collectors(cfg: AppConfig, secrets: Secrets) -> list[Collector]:
    """Every catalog and shared source, and never web search (that's once a day)."""
    return build_catalog_collectors(cfg, secrets, include_web_search=False)


def _to_stored_items(grouped: dict[str, list[TopicItem]]) -> list[StoredItem]:
    """Collapse the per-game grouping into one row per item with its game tags.

    (`run.py` has the same thing for v2; it goes away with `run_daily`.)
    """
    item_by_url: dict[str, RawItem] = {}
    topics_by_url: dict[str, dict[str, bool]] = {}
    for game_key, topic_items in grouped.items():
        for topic_item in topic_items:
            item_by_url[topic_item.item.url] = topic_item.item
            topics_by_url.setdefault(topic_item.item.url, {})[game_key] = topic_item.uncertain
    return [
        StoredItem(
            url=url,
            title=item.title,
            excerpt=item.excerpt,
            source_name=item.source_name,
            trust=item.trust,
            published_at=item.published_at,
            topics=topics_by_url[url],
        )
        for url, item in item_by_url.items()
    ]


def _record_health_sync(db_path: str, results: list[CollectorResult], now: datetime) -> list[str]:
    """Update source_health for every non-skipped result.

    Returns the sources that just hit exactly the alert threshold.
    """
    newly_flagged = []
    with closing(connect(db_path)) as conn:
        for result in results:
            if result.skipped is not None:
                continue
            consecutive = record_source_result(conn, result.source_name, now, result.error)
            if result.error is not None and consecutive == SOURCE_FAILURE_ALERT_THRESHOLD:
                newly_flagged.append(result.source_name)
    return newly_flagged


def _store_sync(db_path: str, collected: list[RawItem], cfg: AppConfig, now: datetime) -> int:
    """Normalize, filter against the whole catalog, and store. Returns the new item count."""
    lookback = timedelta(hours=cfg.collection.lookback_hours)
    with closing(connect(db_path)) as conn:
        normalized = normalize(collected, lambda urls: existing_urls(conn, urls), now, lookback)
        grouped = filter_items(normalized, cfg.catalog, cfg.collection.max_items_per_game)
        return store_items(conn, _to_stored_items(grouped), lambda: now)


def _log_results(results: list[CollectorResult]) -> None:
    for result in results:
        logger.info(
            "collector finished",
            extra={
                "source_name": result.source_name,
                "source_type": result.source_type,
                "item_count": len(result.items),
                "error": result.error,
                "skipped": result.skipped,
            },
        )


async def _alert_owner(deps: CollectionDeps, message: str) -> None:
    # An alert that can't be sent must not take the pass down with it.
    try:
        await deps.alert(message)
    except Exception:
        logger.exception("couldn't send an owner alert")


async def _health_and_store(
    deps: CollectionDeps, results: list[CollectorResult], now: datetime
) -> tuple[int, list[str]]:
    """Steps 4 and 2 and 3 of a pass: health, then dedupe, filter and store."""
    flagged = await asyncio.to_thread(_record_health_sync, deps.db_path, results, now)
    for name in flagged:
        await _alert_owner(
            deps,
            f"newsbot: source {name!r} has failed {SOURCE_FAILURE_ALERT_THRESHOLD} "
            "collections in a row",
        )
    collected = [item for result in results for item in result.items]
    new_items = await asyncio.to_thread(_store_sync, deps.db_path, collected, deps.cfg, now)
    return new_items, flagged


async def run_collection(deps: CollectionDeps) -> CollectionOutcome:
    """Run one shared collection pass. Skips, writing nothing, if the run lock is held."""
    async with run_lock_or_skip() as acquired:
        if not acquired:
            logger.warning(
                "collection pass skipped: the previous pass or a digest still has the lock"
            )
            return CollectionOutcome(skipped=True)

        started = deps.clock()
        now = deps.now()
        interval_s = deps.cfg.collection.interval_minutes * 60
        estimate = estimate_pass_seconds(deps.collectors)
        if estimate.worst_s > interval_s:
            logger.warning(
                "worst-case pass length exceeds the interval",
                extra={
                    "throttled_feeds": estimate.throttled_feeds,
                    "worst_case_s": estimate.worst_s,
                    "interval_s": interval_s,
                },
            )

        results = await run_collectors(
            deps.collectors,
            deps.http,
            timeout_s=_COLLECT_TIMEOUT_S,
            sleep=deps.sleep,
            rate_limit_state=deps.rate_limit_state,
            clock=deps.clock,
        )
        _log_results(results)

        new_items, flagged = await _health_and_store(deps, results, now)

        new_codes = 0
        try:
            new_codes = await deps.shift_hook([i for r in results for i in r.items], results)
        except Exception:
            # Codes are the loud feature, but a detector bug shouldn't cost
            # every stored item this pass; the owner hears about it instead.
            logger.exception("SHiFT detection failed")
            await _alert_owner(deps, "newsbot: SHiFT detection failed during collection")

        ran = [r for r in results if r.skipped is None]
        duration_s = deps.clock() - started
        outcome = CollectionOutcome(
            sources_ok=sum(1 for r in ran if r.error is None),
            sources_total=len(ran),
            new_items=new_items,
            new_codes=new_codes,
            duration_s=duration_s,
            failing_sources=tuple(r.source_name for r in ran if r.error is not None),
            alerted_sources=tuple(flagged),
        )

        def _record_sweep_sync() -> None:
            with closing(connect(deps.db_path)) as conn:
                record_sweep(conn, lambda: now, outcome.summary)

        await asyncio.to_thread(_record_sweep_sync)

        log = logger.warning if duration_s > interval_s * _SLOW_PASS_FRACTION else logger.info
        log(
            "collection pass finished",
            extra={
                "duration_s": round(duration_s, 1),
                "interval_s": interval_s,
                "summary": outcome.summary,
            },
        )
        return outcome


async def collect_web_search(
    deps: CollectionDeps,
    game_keys: Collection[str],
    api_key: str | None,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> CollectionOutcome:
    """Search the web for exactly these games and store what comes back (comped only).

    The summaries job calls this once per game per day, for the games a
    comped server follows; `run_collection` never does (D1). Keys that
    aren't in the catalog are ignored, and so is a missing web_search block
    or API key: there's nothing to search with, which is not an error. No
    lock here; the caller decides what it needs, and the writes are
    transactions either way.
    """
    from newsbot.collectors.web_search import WebSearchCollector

    cfg = deps.cfg
    games = [g for g in cfg.catalog if g.key in set(game_keys)]
    if cfg.web_search is None or not api_key or not games:
        return CollectionOutcome()

    source = WebSearchSource(
        type="web_search",
        name=cfg.web_search.name,
        queries_per_topic=cfg.web_search.queries_per_game,
        query_templates=cfg.web_search.query_templates,
        trust=cfg.web_search.trust,
    )
    collector = WebSearchCollector(source, games, api_key, sleep=sleep or deps.sleep)
    started = deps.clock()
    now = deps.now()
    results = await run_collectors(
        [collector],
        deps.http,
        timeout_s=_web_search_timeout(len(games), cfg.web_search.queries_per_game),
    )
    _log_results(results)
    new_items, flagged = await _health_and_store(deps, results, now)
    ran = [r for r in results if r.skipped is None]
    return CollectionOutcome(
        sources_ok=sum(1 for r in ran if r.error is None),
        sources_total=len(ran),
        new_items=new_items,
        duration_s=deps.clock() - started,
        failing_sources=tuple(r.source_name for r in ran if r.error is not None),
        alerted_sources=tuple(flagged),
    )


def _web_search_timeout(game_count: int, queries_per_game: int) -> float:
    # The collector pauses ~1.1 s between queries, so its time grows with the
    # games searched; the flat 20 s would cut a 10-game search off mid-way.
    return _COLLECT_TIMEOUT_S + game_count * queries_per_game * 1.5


__all__ = [
    "SOURCE_FAILURE_ALERT_THRESHOLD",
    "CollectionDeps",
    "CollectionOutcome",
    "PassEstimate",
    "build_collection_collectors",
    "collect_web_search",
    "collection_job_options",
    "estimate_pass_seconds",
    "run_collection",
]
