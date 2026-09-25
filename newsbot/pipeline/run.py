"""Orchestrate one daily run, and give it a front door that doesn't need Discord.

Everything upstream of this module (collectors, normalize, filter,
summarize, format) is a pure function or close to one: given inputs, it
returns outputs, and none of it knows the word "Discord" exists. This
module is where that stops being true -- it's the one place that has to
juggle a database, a language model, a publish target and a wall clock all
at once, and get the ordering right when any one of them fails partway
through.

The ordering it protects is the double-post guard (SPEC-DEV 2): claim
today's slot with a `pending` row *before* doing anything slow, publish,
and only then save. A crash between claim and save leaves a `pending` row
and nothing else -- annoying (it blocks the next automatic run until
someone force-reclaims it), but never a duplicate post, which was the
actual failure mode worth avoiding.

`python -m newsbot.pipeline.run --dry-run` runs this whole thing with no
Discord connection at all, printing the digest to a terminal instead. That
CLI is also most of how this module gets tested: a fixture-backed run
exercises the real guard, the real save transaction and the real retry
logic against a temp SQLite file, with nothing on the other end of the
`httpx.AsyncClient` but canned JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from newsbot.bot.format import RenderedDigest, render_digest
from newsbot.collectors.base import (
    Collector,
    CollectorResult,
    RateLimitState,
    RawItem,
    build_collectors,
    run_collectors,
)
from newsbot.config import AppConfig, ConfigError, load_config, load_secrets
from newsbot.logging_setup import configure_logging
from newsbot.pipeline.filter import TopicItem, filter_items
from newsbot.pipeline.normalize import normalize
from newsbot.pipeline.publisher import PrintPublisher, Publisher, PublishError
from newsbot.pipeline.summarize import (
    AnthropicLLM,
    LLMClient,
    LLMResult,
    StoriesOut,
    StoryOut,
    TopicSummary,
    summarize_topic,
)
from newsbot.store.db import assert_fts5, connect, migrate
from newsbot.store.models import PriorStory, StoredItem, StoryToSave, Usage
from newsbot.store.repo import (
    claim_digest,
    existing_urls,
    mark_digest_failed,
    recent_headlines,
    record_source_result,
    save_run,
)

if TYPE_CHECKING:
    from newsbot.shift.sweep import CodeAlertPoster

logger = logging.getLogger(__name__)

# Discord asks for a User-Agent that identifies the bot and a way to
# reach its operator; Reddit in particular is unforgiving about generic
# ones (see docs/sources-research.md).
_USER_AGENT = "discord-newsbot/1.0 (+https://github.com/, contact: owner)"

_PRIOR_HEADLINE_WINDOW = timedelta(days=3)
_COLLECT_TIMEOUT_S = 20.0
_PUBLISH_BACKOFF_S = (2.0, 4.0, 8.0)  # 3 retries after the first attempt


class RunMode(StrEnum):
    POST = "post"
    PREVIEW = "preview"


@dataclass
class Deps:
    cfg: AppConfig
    db_path: str
    http: httpx.AsyncClient
    llm: LLMClient
    collectors: list[Collector]
    now: Callable[[], datetime]
    alert: Callable[[str], Awaitable[None]]
    # Shared with the SHiFT alert sweep (design.md §12) so Reddit's gap is
    # honored across the daily job and every hourly sweep, not reset fresh
    # each time. `None` (the default) keeps this run's own collection
    # exactly as it's always behaved -- nothing about the daily job
    # requires cross-call state on its own.
    rate_limit_state: RateLimitState | None = None
    # Set by the bot layer when alerts.enabled; None means "no poster
    # configured", which is also every existing test's default -- the
    # daily POST hook (added in `_run_claimed`) is a no-op without one.
    code_alert_poster: CodeAlertPoster | None = None


@dataclass
class PipelineOutcome:
    status: Literal["ok", "partial", "failed", "skipped"]
    rendered: RenderedDigest | None
    notes: list[str]
    usage: Usage


# One lock, shared by every entry point that can trigger a run (the daily
# job, `/newsbot run-now`, `/newsbot preview`). Two runs racing each other
# would fight over the same claim row and, worse, could both build a
# digest before either saved -- the guard protects the database, this
# protects the two of them from stepping on each other in memory first.
_run_lock = asyncio.Lock()


def local_run_date(now: datetime, timezone: str) -> date:
    """The local calendar date `now` falls on in `timezone`.

    Not UTC's date -- the digest is keyed by the *local* day, because the
    owner reads it in the morning, not at midnight UTC. Near midnight UTC
    the two dates genuinely differ (a run at 02:00 UTC is still "yesterday"
    at 09:00 America/Los_Angeles), which is exactly the case worth a test.
    """
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo(timezone)).date()


def is_run_in_progress() -> bool:
    """True if `_run_lock` is currently held by another run.

    Exists so the bot layer (`/newsbot run-now`) can give a quick "a run
    is already going" reply instead of blocking on the lock for however
    long a pipeline run takes -- nobody wants a slash command to sit there
    looking hung for two minutes.
    """
    return _run_lock.locked()


def _normalize_sync(
    items: list[RawItem], db_path: str, now: datetime, lookback: timedelta
) -> list[RawItem]:
    # Runs inside asyncio.to_thread: the sqlite connection it opens for
    # the known-urls lookback is created and used entirely within this
    # worker thread, never touching the event loop.
    with closing(connect(db_path)) as conn:
        return normalize(items, lambda urls: existing_urls(conn, urls), now, lookback)


def _prior_headlines_sync(
    db_path: str, topic_keys: list[str], since: datetime
) -> dict[str, list[PriorStory]]:
    with closing(connect(db_path)) as conn:
        return {key: recent_headlines(conn, key, since) for key in topic_keys}


async def build_digest(
    deps: Deps, run_date: date
) -> tuple[
    RenderedDigest,
    list[StoredItem],
    list[StoryToSave],
    str,
    list[str],
    Usage,
    list[CollectorResult],
]:
    """Run collect, normalize, filter and summarize. Writes nothing.

    Returns everything `run_daily` needs to decide what happened and, in
    POST mode, what to save: the rendered digest, the items and stories
    ready for `repo.save_run`, an overall status (`ok`/`partial`), header
    notes, token usage, and the raw per-collector results (for source
    health bookkeeping, which happens one level up).
    """
    cfg = deps.cfg
    now = deps.now()

    results = await run_collectors(
        deps.collectors,
        deps.http,
        timeout_s=_COLLECT_TIMEOUT_S,
        rate_limit_state=deps.rate_limit_state,
    )
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
    collected = [item for result in results for item in result.items]

    lookback = timedelta(hours=cfg.digest.lookback_hours)
    normalized = await asyncio.to_thread(_normalize_sync, collected, deps.db_path, now, lookback)

    grouped = filter_items(normalized, cfg.topics, cfg.digest.max_items_per_topic)

    since = now - _PRIOR_HEADLINE_WINDOW
    prior_by_topic = await asyncio.to_thread(
        _prior_headlines_sync, deps.db_path, [t.key for t in cfg.topics], since
    )

    summaries: dict[str, TopicSummary] = {}
    for topic in cfg.topics:
        topic_items = grouped.get(topic.key)
        if not topic_items:
            continue
        summaries[topic.key] = await summarize_topic(
            deps.llm,
            topic,
            topic_items,
            prior_by_topic.get(topic.key, []),
            all_topics=cfg.topics,
        )

    stored_items = _build_stored_items(grouped)
    stories_to_save = [
        StoryToSave(
            topic_key=topic_key,
            headline=draft.headline,
            summary=draft.summary,
            label=draft.label,
            item_urls=draft.item_urls,
            update_of_story_id=draft.update_of_story_id,
        )
        for topic_key, summary in summaries.items()
        for draft in summary.stories
    ]

    fallback_items = {
        topic_key: grouped.get(topic_key, [])
        for topic_key, summary in summaries.items()
        if summary.fallback
    }

    coverage_notes = [
        f"{result.source_name}: {result.skipped}" for result in results if result.skipped
    ]
    status = "partial" if coverage_notes or any(s.fallback for s in summaries.values()) else "ok"

    usage = Usage(
        input_tokens=sum(s.usage.input_tokens for s in summaries.values()),
        output_tokens=sum(s.usage.output_tokens for s in summaries.values()),
    )

    rendered = render_digest(run_date, cfg.topics, summaries, fallback_items, coverage_notes)
    return rendered, stored_items, stories_to_save, status, coverage_notes, usage, results


def _build_stored_items(grouped: dict[str, list[TopicItem]]) -> list[StoredItem]:
    """Collapse `filter_items`' per-topic grouping back into one row per item.

    An item that matched two topics shows up in two of `grouped`'s lists,
    but it's one row in `items` -- `item_topics` is what carries the
    one-to-many relationship, so this rebuilds a url -> item map alongside
    a url -> {topic_key: uncertain} map and zips them back together.
    """
    item_by_url: dict[str, RawItem] = {}
    topics_by_url: dict[str, dict[str, bool]] = {}
    for topic_key, topic_items in grouped.items():
        for topic_item in topic_items:
            item_by_url[topic_item.item.url] = topic_item.item
            topics_by_url.setdefault(topic_item.item.url, {})[topic_key] = topic_item.uncertain

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


async def run_daily(
    deps: Deps,
    publisher: Publisher,
    *,
    mode: RunMode,
    force: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PipelineOutcome:
    """Run one day's pipeline, in POST or PREVIEW mode.

    PREVIEW never claims, never writes source health, and never saves --
    it reads the store (for dedupe and prior headlines) and nothing more,
    so running a preview can never change what the real job sees later.

    POST claims the day first (SPEC-DEV 2), builds the digest, records
    source health, publishes with retry, and only then saves items,
    stories and the final status in one transaction. A publish failure
    after the claim leaves the digest `failed` with nothing saved, so a
    retry recollects instead of finding "no new items" against data that
    never actually reached anyone.
    """
    async with _run_lock:
        if mode is RunMode.PREVIEW:
            return await _run_preview(deps, publisher)
        return await _run_post(deps, publisher, force=force, sleep=sleep)


async def _run_preview(deps: Deps, publisher: Publisher) -> PipelineOutcome:
    run_date = local_run_date(deps.now(), deps.cfg.digest.timezone)
    try:
        rendered, _items, _stories, status, notes, usage, _results = await build_digest(
            deps, run_date
        )
        await publisher.publish(rendered)
    except Exception as exc:  # a preview must never take the bot down with it
        logger.exception("preview run failed")
        await deps.alert(f"newsbot: preview failed: {exc}")
        return PipelineOutcome(status="failed", rendered=None, notes=[str(exc)], usage=Usage(0, 0))
    return PipelineOutcome(status=status, rendered=rendered, notes=notes, usage=usage)


async def _run_post(
    deps: Deps, publisher: Publisher, *, force: bool, sleep: Callable[[float], Awaitable[None]]
) -> PipelineOutcome:
    run_date = local_run_date(deps.now(), deps.cfg.digest.timezone)

    digest_id = await asyncio.to_thread(_claim_sync, deps.db_path, run_date, force, deps.now)
    if digest_id is None:
        return PipelineOutcome(
            status="skipped",
            rendered=None,
            notes=["today's digest is already claimed"],
            usage=Usage(0, 0),
        )

    try:
        return await _run_claimed(deps, publisher, digest_id, run_date, sleep=sleep)
    except BaseException as exc:
        # build_digest failing and publish failing after retries both
        # already have their own handling below, and return a normal
        # PipelineOutcome instead of raising. If something gets past both
        # of those, it's a shape of failure this module didn't
        # anticipate -- a bug, a publisher raising something other than
        # PublishError, the run getting cancelled -- and the one thing
        # that must not happen is `digest_id` staying `pending` forever,
        # since that blocks every run (and, pre-group-4, every admin)
        # after it. Record it failed with whatever got posted (a
        # PublishError-shaped exception carries that; anything else
        # didn't get far enough to post anything) and keep propagating:
        # this function isn't the place to decide whether the caller can
        # recover from it.
        posted_ids = list(getattr(exc, "posted_ids", None) or [])
        logger.exception("unhandled error after claiming %s; marking it failed", run_date)
        await asyncio.to_thread(
            _mark_failed_sync,
            deps.db_path,
            digest_id,
            f"unhandled error: {exc!r}",
            posted_ids,
            deps.now,
        )
        raise


async def _run_claimed(
    deps: Deps,
    publisher: Publisher,
    digest_id: int,
    run_date: date,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> PipelineOutcome:
    """The part of `_run_post` that runs once the day is claimed.

    Split out from `_run_post` so that function's outer `except
    BaseException` reads as what it is: a last-resort net around
    everything below, not the normal control flow. Every failure this
    function already knows how to handle (a bad collect/summarize, a
    publish that never recovers) returns a `PipelineOutcome` instead of
    raising; anything that raises past here is `_run_post`'s problem.
    """
    try:
        rendered, items, stories, status, notes, usage, results = await build_digest(deps, run_date)
    except Exception as exc:
        logger.exception("build_digest failed")
        await asyncio.to_thread(
            _mark_failed_sync, deps.db_path, digest_id, f"build failed: {exc}", [], deps.now
        )
        await deps.alert(f"newsbot: digest build failed: {exc}")
        return PipelineOutcome(status="failed", rendered=None, notes=[str(exc)], usage=Usage(0, 0))

    await _record_source_health(deps, results)

    message_ids, publish_error = await _publish_with_retry(publisher, rendered, sleep=sleep)
    if publish_error is not None:
        await asyncio.to_thread(
            _mark_failed_sync,
            deps.db_path,
            digest_id,
            f"publish failed: {publish_error}",
            message_ids,
            deps.now,
        )
        await deps.alert(f"newsbot: publish failed after retries: {publish_error}")
        return PipelineOutcome(
            status="failed", rendered=rendered, notes=[*notes, str(publish_error)], usage=usage
        )

    await asyncio.to_thread(
        _save_run_sync,
        deps.db_path,
        digest_id,
        items,
        stories,
        status,
        message_ids,
        "; ".join(notes) or None,
        usage,
        deps.now,
    )
    return PipelineOutcome(status=status, rendered=rendered, notes=notes, usage=usage)


def _claim_sync(
    db_path: str, run_date: date, force: bool, now: Callable[[], datetime]
) -> int | None:
    with closing(connect(db_path)) as conn:
        return claim_digest(conn, run_date, force=force, now=now)


def _mark_failed_sync(
    db_path: str, digest_id: int, notes: str, message_ids: list[int], now: Callable[[], datetime]
) -> None:
    with closing(connect(db_path)) as conn:
        mark_digest_failed(conn, digest_id, notes, message_ids, now=now)


def _save_run_sync(
    db_path: str,
    digest_id: int,
    items: list[StoredItem],
    stories: list[StoryToSave],
    status: str,
    message_ids: list[int],
    notes: str | None,
    usage: Usage,
    now: Callable[[], datetime],
) -> None:
    with closing(connect(db_path)) as conn:
        save_run(conn, digest_id, items, stories, status, message_ids, notes, usage, now=now)


def _record_health_sync(db_path: str, results: list[CollectorResult], now: datetime) -> list[str]:
    """Update source_health for every non-skipped result.

    Returns the names of sources that just hit exactly 3 consecutive
    failures, so the caller knows who to alert about.
    """
    newly_flagged = []
    with closing(connect(db_path)) as conn:
        for result in results:
            if result.skipped is not None:
                continue
            consecutive = record_source_result(conn, result.source_name, now, result.error)
            if result.error is not None and consecutive == 3:
                newly_flagged.append(result.source_name)
    return newly_flagged


async def _record_source_health(deps: Deps, results: list[CollectorResult]) -> None:
    flagged = await asyncio.to_thread(_record_health_sync, deps.db_path, results, deps.now())
    for source_name in flagged:
        await deps.alert(f"newsbot: source {source_name!r} has failed 3 runs in a row")


async def _publish_with_retry(
    publisher: Publisher, rendered: RenderedDigest, *, sleep: Callable[[float], Awaitable[None]]
) -> tuple[list[int], PublishError | None]:
    """Retry `publisher.publish()` on transient failure, with backoff.

    Only `PublishError` is caught here -- that's the contract the
    `Publisher` protocol documents, and a resumable publisher (see
    `DiscordPublisher`) is exactly what makes retrying the *same*
    publisher instance safe: each attempt picks up where the last one
    left off instead of reposting what already made it through. On final
    failure, the ids returned come from the error itself
    (`PublishError.posted_ids`), not an empty list -- those ids are real
    messages sitting in the channel, and `_run_post` needs them to record
    against the `failed` row so a human (or `needs_confirmation`) knows
    part of the digest already posted.
    """
    last_error: PublishError | None = None
    for attempt in range(len(_PUBLISH_BACKOFF_S) + 1):
        try:
            return await publisher.publish(rendered), None
        except PublishError as exc:
            last_error = exc
            if attempt < len(_PUBLISH_BACKOFF_S):
                await sleep(_PUBLISH_BACKOFF_S[attempt])
    return (last_error.posted_ids if last_error else []), last_error


# --- Offline running: fixture collectors and a canned-JSON stub LLM ---
#
# `--fixtures` and `--stub-llm` exist so the whole pipeline, guard and all,
# can be exercised (in CI, or by hand) without a network connection or an
# API key. Neither is meant to resemble a real collector or a real
# AnthropicLLM; they exist purely to hand the rest of the pipeline
# realistic-shaped data from a file.


class FixtureCollector:
    """Reads pre-collected `RawItem`s from a JSON file instead of the network.

    One file, one "collector". The file's own name becomes the source
    name shown in logs and coverage notes -- there's no config source to
    borrow a name from, since fixtures replace the whole collector list.
    """

    source_type = "fixture"
    rate_limit_key = None

    def __init__(self, path: Path) -> None:
        self._path = path
        self.name = path.stem

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        payload = json.loads(self._path.read_text())
        items = []
        for raw in payload:
            published_at = raw.get("published_at")
            items.append(
                RawItem(
                    url=raw["url"],
                    title=raw["title"],
                    excerpt=raw.get("excerpt", ""),
                    source_name=raw.get("source_name", self.name),
                    trust=raw.get("trust", "community"),
                    published_at=(datetime.fromisoformat(published_at) if published_at else None),
                    topics=tuple(raw["topics"]) if raw.get("topics") else None,
                    full_text=raw.get("full_text"),
                )
            )
        return items


def build_fixture_collectors(fixtures_dir: Path) -> list[Collector]:
    """One `FixtureCollector` per `*.json` file in `fixtures_dir`.

    `llm.json` is excluded on purpose: the CLI's `--stub-llm` file lives
    in the same directory as the feed fixtures (see the verify command in
    the plan), and it's shaped like canned stories, not like a collected
    item list -- globbing it in here would turn "no LLM configured" into
    a mysteriously broken collector instead of a clear error.
    """
    return [
        FixtureCollector(path)
        for path in sorted(fixtures_dir.glob("*.json"))
        if path.name != "llm.json"
    ]


_TOPIC_KEY_RE = re.compile(r"\(key: (\w+)\)")


class StubLLM:
    """Returns canned stories from a JSON file instead of calling Claude.

    The file is `{"<topic_key>": [<StoryOut-shaped dict>, ...], ...}`.
    `build_prompt` always puts `(key: <topic_key>)` on the user message's
    first line, so this pulls the topic back out of the prompt rather
    than needing the `LLMClient` protocol to grow a topic-aware method
    just for testing.
    """

    def __init__(self, path: Path) -> None:
        payload = json.loads(path.read_text())
        self._by_topic: dict[str, StoriesOut] = {
            topic_key: StoriesOut(stories=[StoryOut(**story) for story in stories])
            for topic_key, stories in payload.items()
        }

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        match = _TOPIC_KEY_RE.search(user)
        topic_key = match.group(1) if match else None
        stories = self._by_topic.get(topic_key, StoriesOut(stories=[]))
        return LLMResult(stories=stories, input_tokens=0, output_tokens=0)


async def _stdout_alert(text: str) -> None:
    print(f"[alert] {text}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m newsbot.pipeline.run")
    parser.add_argument("--config", default=os.environ.get("NEWSBOT_CONFIG"))
    parser.add_argument("--db", default=os.environ.get("NEWSBOT_DB", "./data/newsbot.db"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="PREVIEW mode with PrintPublisher (the default; this flag just makes it explicit)",
    )
    parser.add_argument(
        "--post-to-stdout",
        action="store_true",
        help="POST mode with PrintPublisher: exercises the guard/save, prints instead of posting",
    )
    parser.add_argument(
        "--fixtures", help="directory of *.json fixture files, replacing collectors"
    )
    parser.add_argument(
        "--stub-llm", help="JSON file of canned stories, replacing the Claude client"
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    configure_logging()

    if not args.config:
        print("newsbot: --config (or $NEWSBOT_CONFIG) is required", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Secrets are only needed for the pieces --fixtures/--stub-llm didn't
    # replace: real collectors want BRAVE_API_KEY (and Bluesky's, if
    # configured), the real LLM wants ANTHROPIC_API_KEY. A fully offline
    # run (both flags given) needs neither.
    secrets = None
    if not args.fixtures or not args.stub_llm:
        try:
            secrets = load_secrets(require_discord=False)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    db_parent = Path(args.db).parent
    if str(db_parent) not in ("", "."):
        db_parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(args.db)) as conn:
        migrate(conn)
        assert_fts5(conn)

    if args.fixtures:
        collectors: list[Collector] = build_fixture_collectors(Path(args.fixtures))
    else:
        collectors = build_collectors(cfg, secrets)

    llm: LLMClient
    if args.stub_llm:
        llm = StubLLM(Path(args.stub_llm))
    else:
        llm = AnthropicLLM(secrets.anthropic_api_key.get_secret_value())

    mode = RunMode.POST if args.post_to_stdout else RunMode.PREVIEW

    async def _run() -> PipelineOutcome:
        async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as http:
            deps = Deps(
                cfg=cfg,
                db_path=args.db,
                http=http,
                llm=llm,
                collectors=collectors,
                now=lambda: datetime.now(UTC),
                alert=_stdout_alert,
            )
            return await run_daily(deps, PrintPublisher(), mode=mode, force=args.force)

    outcome = asyncio.run(_run())
    logger.info(
        "pipeline run finished",
        extra={
            "status": outcome.status,
            "input_tokens": outcome.usage.input_tokens,
            "output_tokens": outcome.usage.output_tokens,
            "notes": outcome.notes,
        },
    )
    return 0 if outcome.status in ("ok", "partial", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Deps",
    "FixtureCollector",
    "PipelineOutcome",
    "RunMode",
    "StubLLM",
    "build_digest",
    "build_fixture_collectors",
    "is_run_in_progress",
    "local_run_date",
    "main",
    "run_daily",
]
