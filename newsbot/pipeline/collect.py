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

So Reddit rotates. The subreddits that need to be fresh (SHiFT games, whose
codes go stale fast, and games a comped server follows, whose digest reads
them) go every pass. The rest take turns, a few per pass, stalest first, so
each lands about every `reddit_rotation_hours`; fifteen games is seven
requests a pass instead of fifteen. Within a pass everything goes stalest
first, priority included, so if Reddit only lets one request through they
take turns instead of the first in config order eating it every time. The
last-fetch times live in `app_state` so a restart doesn't reshuffle the queue.
And a 429 ends Reddit for the pass (and, if Reddit sent a Retry-After, for
the passes that period covers): the sources left over were never asked, so
they aren't failures, they just stay due and go first next time.

Reddit is also codes-only since v3.0.3 (Reddit denied the bot's API request,
and its terms bar sharing its content): `r/Borderlands4` is still fetched and
its items still go to the SHiFT hook, but `storable_items` keeps them out of the
store, so no digest, summary, search or prompt can ever contain one.

Source health is global and hourly, which changes what "three failures in a
row" means: v2's threshold was three days, and three hours is just a
Tuesday. The owner hears about a source once, at twelve in a row, and the
counter re-arms on the first success.

This module doesn't touch Discord or the scheduler. SHiFT detection is an
injected hook (`shift/fanout.py` supplies the real one), and `NewsBot` runs
`run_collection` on `collection_job_options`' schedule.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from newsbot.collectors.base import (
    _DEFAULT_RATE_LIMIT_GAP_S,
    SKIP_BACKED_OFF,
    SKIP_RATE_LIMITED,
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
from newsbot.pipeline.normalize import canonicalize_items, normalize
from newsbot.reddit import is_reddit_url
from newsbot.store.db import connect
from newsbot.store.models import StoredItem
from newsbot.store.repo import (
    app_state_get,
    app_state_set,
    comped_follows,
    existing_urls,
    record_source_result,
    record_sweep,
    store_items,
)
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

# D8 (owner, 2026-09-30): alert once at 12 consecutive failed collections,
# half a day at the default interval. "Exactly" 12, not "at least": the
# counter passing through 12 is the one moment worth a message, and a
# success resets it to zero, which re-arms the alert.
SOURCE_FAILURE_ALERT_THRESHOLD = 12

_COLLECT_TIMEOUT_S = 20.0

# The SHiFT hook reads pages and talks to Discord, and a hook that never
# comes back would hold the run lock (and every later pass) hostage. Two
# minutes is generous for work that's mostly regex; past it, it's a failure.
_SHIFT_HOOK_TIMEOUT_S = 120.0

# A source name goes into an owner alert verbatim, and names are config, so
# the cap is for the day somebody pastes a paragraph in there.
_ALERT_NAME_CAP = 100

# A pass that takes more than this share of the interval gets a warning.
_SLOW_PASS_FRACTION = 0.5

_REDDIT_KEY = "reddit"
_REDDIT_FETCHED_PREFIX = "reddit_fetched:"

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
    # True once the owner has been told the hook is failing; cleared by the
    # next pass where it succeeds. In memory, like the rate-limit state: a
    # DB write on the failure path could fail too, and a restart re-alerting
    # once about a hook that's still broken is a feature.
    shift_hook_alerted: bool = False
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


@dataclass(frozen=True)
class RedditPlan:
    """Which Reddit sources this pass fetches, and how many sat it out."""

    selected: list[Collector]
    rotated_out: int


def _reddit_games(cfg: AppConfig) -> dict[str, frozenset[str] | None]:
    """Source name -> the games it feeds (None: a shared source with no game list)."""
    games: dict[str, frozenset[str] | None] = {}
    for game in cfg.catalog:
        for source in game.sources:
            if source.name:
                games[source.name] = frozenset({game.key})
    for shared in cfg.shared_sources:
        if shared.type != "web_search" and shared.name:
            covers = getattr(shared, "games", None)
            games[shared.name] = frozenset(covers) if covers else None
    return games


def priority_games(cfg: AppConfig, db_path: str) -> frozenset[str]:
    """The games whose subreddits go every pass: SHiFT's, and any comped server's.

    Worked out fresh each pass, so a comped server following a new game moves
    that game's subreddit into the hourly set on the next one.
    """
    with closing(connect(db_path)) as conn:
        comped = {follow.game_key for follow in comped_follows(conn)}
    return frozenset(cfg.shift.games) | comped


def _read_fetch_times(db_path: str, names: Sequence[str]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    with closing(connect(db_path)) as conn:
        for name in names:
            raw = app_state_get(conn, _REDDIT_FETCHED_PREFIX + name)
            if raw is None:
                continue
            try:
                out[name] = datetime.fromisoformat(raw)
            except ValueError:
                continue  # junk reads as "never fetched", which is the safe way to be wrong
    return out


def _write_fetch_times(db_path: str, names: Sequence[str], now: datetime) -> None:
    with closing(connect(db_path)) as conn:
        for name in names:
            app_state_set(conn, _REDDIT_FETCHED_PREFIX + name, now.isoformat())


def plan_reddit(
    collectors: Sequence[Collector],
    cfg: AppConfig,
    priority: frozenset[str],
    fetched: dict[str, datetime],
) -> RedditPlan:
    """Pick this pass's Reddit sources and put them in the order they'll be asked.

    Priority sources always go. The rest are ranked stalest first (never
    fetched counts as stalest; ties keep config order) and the top
    `ceil(n / passes per rotation)` go. A never-fetched source gets no
    special pass around that cap: after a deploy the whole rotating pile is
    "never fetched", and letting them all through is the burst this exists to
    prevent. Then everything that's going, priority included, is ordered by
    that same staleness. This matters when Reddit lets one request through
    per pass: with priority sources in config order, the first one always
    got the request and the others waited at the back of a line that never
    moved. Now they take turns. Non-Reddit sources come first, untouched.
    """
    games = _reddit_games(cfg)
    reddit = [c for c in collectors if getattr(c, "rate_limit_key", None) == _REDDIT_KEY]
    rotating = [c for c in reddit if not (games.get(c.name) or frozenset()) & priority]
    passes = max(1, (cfg.collection.reddit_rotation_hours * 60) // cfg.collection.interval_minutes)
    quota = math.ceil(len(rotating) / passes)
    oldest = datetime.min.replace(tzinfo=UTC)

    def stalest_first(pool: list[Collector]) -> list[Collector]:
        return sorted(
            pool,
            key=lambda c: (fetched.get(c.name, oldest).astimezone(UTC), reddit.index(c)),
        )

    chosen = stalest_first(rotating)[:quota]
    rotating_ids = {id(c) for c in rotating}
    going = [c for c in reddit if id(c) not in rotating_ids] + chosen
    others = [c for c in collectors if getattr(c, "rate_limit_key", None) != _REDDIT_KEY]
    selected = others + stalest_first(going)
    return RedditPlan(selected=selected, rotated_out=len(rotating) - len(chosen))


def collection_job_options(cfg: AppConfig) -> dict[str, object]:
    """The scheduler settings `NewsBot` uses for this job.

    `max_instances=1` and `coalesce` are the scheduler's half of "never
    stack passes"; `run_collection` skipping on a held lock is the other
    half, and it holds even if somebody forgets these. The bot schedules the
    first run from `on_ready` (`NewsBot._start_collection_job`), not at setup.
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
    """Collapse the per-game grouping into one row per item with its game tags."""
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


def _merge_loser_tags(
    stored: list[StoredItem], winners: dict[str, RawItem], extra: dict[str, list[TopicItem]]
) -> list[StoredItem]:
    """Add the losing copies' game tags to the winners' items.

    `extra` is the catalog filter's verdict on the copies dedupe threw away.
    A tag the winner already has keeps the winner's flag, unless the loser
    was confident and the winner wasn't. A URL whose winner matched no game
    at all but whose loser did is stored too, with the winner's content.
    These tags ride along on items being stored anyway, so they don't
    compete for the per-game cap.
    """
    by_url = {item.url: item for item in stored}
    for topic_items in extra.values():
        for topic_item in topic_items:
            url = topic_item.item.url
            if url not in by_url:
                won = winners[url]
                by_url[url] = StoredItem(
                    url=url,
                    title=won.title,
                    excerpt=won.excerpt,
                    source_name=won.source_name,
                    trust=won.trust,
                    published_at=won.published_at,
                    topics={},
                )
            topics = by_url[url].topics
            topics[topic_item.topic_key] = topics.get(topic_item.topic_key, True) and (
                topic_item.uncertain
            )
    return list(by_url.values())


def storable_items(collected: list[RawItem], cfg: AppConfig) -> list[RawItem]:
    """What may be stored: everything except codes-only sources' items and Reddit links.

    A codes-only source's items exist to be searched for SHiFT codes (the hook
    gets them straight from the collector results, with their URLs for the
    alert's link) and then forgotten. The URL check is the net under the net:
    web search can hand back a reddit.com thread under Brave's name, and Reddit's
    content doesn't get to ride in on a different source's ticket.
    """
    codes_only = cfg.codes_only_source_names()
    return [
        item
        for item in collected
        if item.source_name not in codes_only and not is_reddit_url(item.url)
    ]


def _store_sync(db_path: str, collected: list[RawItem], cfg: AppConfig, now: datetime) -> int:
    """Normalize, filter against the whole catalog, and store. Returns the new item count.

    Codes-only items are dropped first (`storable_items`), so nothing below, and
    nothing that reads the store, ever sees them.

    When several sources carry one URL, dedupe keeps the most trusted copy's
    content, but the stored item gets the game tags of every copy (a
    Palworld-only community feed and an official feed that mentions
    Borderlands 4 make one item tagged both). Across passes it stays
    first-come: a stored URL is dropped before any of this happens.
    """
    collected = storable_items(collected, cfg)
    lookback = timedelta(hours=cfg.collection.lookback_hours)
    with closing(connect(db_path)) as conn:
        losers: list[RawItem] = []
        deduped = canonicalize_items(collected, losers=losers)
        normalized = normalize(deduped, lambda urls: existing_urls(conn, urls), now, lookback)
        grouped = filter_items(normalized, cfg.catalog, cfg.collection.max_items_per_game)
        stored = _to_stored_items(grouped)

        winners = {item.url: item for item in normalized}
        new_losers = [item for item in losers if item.url in winners]
        if new_losers:
            # Uncapped: only the tags are used, and they never take a slot.
            extra = filter_items(new_losers, cfg.catalog, len(new_losers))
            stored = _merge_loser_tags(stored, winners, extra)
        return store_items(conn, stored, lambda: now)


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


async def _note_reddit(
    deps: CollectionDeps, plan: RedditPlan, results: list[CollectorResult], now: datetime
) -> None:
    """Stamp the Reddit sources that were actually asked, and log the pass's one summary line.

    A source that was backed off or 429'd isn't stamped: it stays the stalest
    and goes first next pass. One that ran and failed is stamped, or a dead
    feed would sit at the front of the queue forever. (I tried stamping the
    429'd one too. Whoever is second in line gets the 429 every pass, so it
    was stamped fresh every pass and never fetched once. Unstamped, it just
    moves up the line until it's first, and the first one always gets through.)
    """
    reddit_names = {
        c.name for c in plan.selected if getattr(c, "rate_limit_key", None) == _REDDIT_KEY
    }
    if not reddit_names:
        return
    mine = [r for r in results if r.source_name in reddit_names]
    asked = [r.source_name for r in mine if r.skipped is None]
    backed_off = sum(1 for r in mine if r.skipped in (SKIP_RATE_LIMITED, SKIP_BACKED_OFF))
    try:
        await asyncio.to_thread(_write_fetch_times, deps.db_path, asked, now)
    except Exception:
        logger.exception("couldn't record the Reddit fetch times")
    retry_after_s = next((r.retry_after_s for r in mine if r.retry_after_s), None)
    suffix = "" if retry_after_s is None else f", reddit_retry_after_s {retry_after_s:.0f}"
    extra: dict[str, object] = {
        "reddit_fetched": len(asked),
        "reddit_rotated_out": plan.rotated_out,
        "reddit_backed_off": backed_off,
    }
    if retry_after_s is not None:
        extra["reddit_retry_after_s"] = retry_after_s
    logger.info(
        "Reddit fetched %d, rotated out %d, backed off %d%s",
        len(asked),
        plan.rotated_out,
        backed_off,
        suffix,
        extra=extra,
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
    flagged: list[str] = []
    try:
        flagged = await asyncio.to_thread(_record_health_sync, deps.db_path, results, now)
    except Exception:
        # Bookkeeping isn't worth the items: a locked database here costs a
        # health update, not the pass.
        logger.exception("couldn't record source health")
    for name in flagged:
        await _alert_owner(
            deps,
            f"newsbot: source {plain_line(name, _ALERT_NAME_CAP)!r} has failed "
            f"{SOURCE_FAILURE_ALERT_THRESHOLD} collections in a row",
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
        try:
            priority = await asyncio.to_thread(priority_games, deps.cfg, deps.db_path)
            fetched = await asyncio.to_thread(
                _read_fetch_times,
                deps.db_path,
                [
                    c.name
                    for c in deps.collectors
                    if getattr(c, "rate_limit_key", None) == _REDDIT_KEY
                ],
            )
        except Exception:
            # No rotation state is a worse day, not a dead pass: fetch the
            # priority-less, never-fetched picture and carry on.
            logger.exception("couldn't read the Reddit rotation state")
            priority, fetched = frozenset(deps.cfg.shift.games), {}
        plan = plan_reddit(deps.collectors, deps.cfg, priority, fetched)
        estimate = estimate_pass_seconds(plan.selected)
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
            plan.selected,
            deps.http,
            timeout_s=_COLLECT_TIMEOUT_S,
            sleep=deps.sleep,
            rate_limit_state=deps.rate_limit_state,
            clock=deps.clock,
        )
        _log_results(results)
        await _note_reddit(deps, plan, results, now)

        new_items, flagged = await _health_and_store(deps, results, now)

        new_codes = 0
        try:
            new_codes = await asyncio.wait_for(
                deps.shift_hook([i for r in results for i in r.items], results),
                timeout=_SHIFT_HOOK_TIMEOUT_S,
            )
            deps.shift_hook_alerted = False
        except Exception:
            # Codes are the loud feature, but a detector bug shouldn't cost
            # every stored item this pass; the owner hears about it instead,
            # once, until the hook works again. A hook that hangs past the
            # timeout lands here too (TimeoutError is an Exception).
            logger.exception("SHiFT detection failed")
            if not deps.shift_hook_alerted:
                deps.shift_hook_alerted = True
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
    "RedditPlan",
    "build_collection_collectors",
    "collect_web_search",
    "collection_job_options",
    "estimate_pass_seconds",
    "plan_reddit",
    "priority_games",
    "run_collection",
]
