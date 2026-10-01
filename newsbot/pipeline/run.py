"""The command-line front door: run the pipeline's pieces without a Discord connection.

This module used to be the pipeline. `run_daily` collected, summarized,
claimed, published and saved one server's digest in one long breath, and it
was the one place that had to juggle a database, a model, a publish target and
a wall clock at once. The public app split that into pieces that each do one
job: `pipeline/collect.py` fetches the whole catalog every hour,
`pipeline/summaries.py` makes the comped servers' summaries ahead of time, and
`pipeline/guild_digest.py` claims, posts and saves one server's digest from
what's already stored. `run_daily` retired at the cutover. What's left here is
the front door that lets a person run those pieces from a terminal, plus the
few helpers they all still share (`RunKind`, `local_run_date`, the publish
retry loop).

`python -m newsbot.pipeline.run` has these modes:

- `--check-sources [--game KEY ...]` runs the real sources once and reports.
  It needs no database, no Anthropic key and no Discord token.
- `--collect` runs one collection pass into `--db`. SHiFT detection runs and
  newly found codes are released (queued for each server), but delivery is a
  preview: the queue is printed, and nothing is claimed, marked posted or
  failed, and no server's ping count moves. The real bot's own walk delivers
  the queue, so a CLI run next to it can't drain it. (`--sweep` is the old
  name; it still works for one release, with a note on stderr.)
- `--dry-run` (the default) previews one server's next digest from what's
  stored. It writes nothing, not even the one-time import or the schema
  migration: it works on a throwaway copy of `--db`, so the real file comes
  out byte-identical. (Pointing it at a v2.2 database and having it quietly
  freeze the SHiFT ping budget at import time was the lesson there.)
- `--post-to-stdout [--force]` is the same server's real run, printed instead
  of posted. It claims, prints and saves, so it exercises the per-server guard.
- `--fixtures DIR` swaps the collectors for canned JSON and implies one
  collection pass first, so `--fixtures --stub-llm --dry-run` stays a fully
  offline run, which is how most of this gets tested.
- `--now` is the clock for every step, the due check and the window math
  included.

Every mode but `--check-sources` runs the v2 import first (`ensure_imported`),
so a self-hoster's first `--dry-run` after upgrading just works. Only the
read-only mode, a digest preview with no collection pass (no `--collect`, no
`--fixtures`), runs it against the throwaway copy; `--collect`, `--fixtures` and
`--post-to-stdout` write for real, so they import for real. Which means none of
those three belongs anywhere near a live production database.

A reminder for anyone adding an import: `guild_digest.py` and `summaries.py`
import from this module, so this module imports them lazily, inside the
functions that need them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import httpx

from newsbot.bot.format import to_text
from newsbot.collectors.base import (
    Collector,
    CollectorResult,
    RawItem,
    build_catalog_collectors,
    run_collectors,
)
from newsbot.config import (
    AppConfig,
    ConfigError,
    GameCfg,
    Secrets,
    count_configured_web_search_sources,
    load_check_sources_secrets,
    load_config,
    load_secrets,
)
from newsbot.logging_setup import configure_logging
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.normalize import normalize
from newsbot.pipeline.publisher import PrintPublisher, Publisher, PublishError
from newsbot.pipeline.summarize import (
    AnthropicLLM,
    LLMClient,
    LLMResult,
    StoriesOut,
    StoryOut,
)
from newsbot.store import repo
from newsbot.store.db import assert_fts5, connect, migrate
from newsbot.useragent import user_agent_headers, warn_if_contact_unset

logger = logging.getLogger(__name__)

_PRIOR_HEADLINE_WINDOW = timedelta(days=3)
_COLLECT_TIMEOUT_S = 20.0
_PUBLISH_BACKOFF_S = (2.0, 4.0, 8.0)  # 3 retries after the first attempt


class RunKind(StrEnum):
    """Why a server's digest is running, shown on its run report (design.md §6).

    Deliberately doesn't say *who* ran `/newsbot run-now`: the privacy
    policy promises we don't keep user ids around, and this is not the
    place to start.
    """

    SCHEDULED = "scheduled"
    CATCH_UP = "catch-up"
    RUN_NOW = "run-now"


def local_run_date(now: datetime, timezone: str) -> date:
    """The local calendar date `now` falls on in `timezone`.

    Not UTC's date: a digest is keyed by the *local* day, because people read
    it in the morning, not at midnight UTC. Near midnight UTC the two dates
    genuinely differ (a run at 02:00 UTC is still "yesterday" at 09:00
    America/Los_Angeles), which is exactly the case worth a test.
    """
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo(timezone)).date()


async def _publish_with_retry(
    publisher: Publisher, rendered, *, sleep: Callable[[float], Awaitable[None]]
) -> tuple[dict[str, int], PublishError | None]:
    """Retry `publisher.publish()` on transient failure, with backoff.

    Only `PublishError` is caught here: that's the contract the
    `Publisher` protocol documents, and a resumable publisher (see
    `DiscordPublisher`) is exactly what makes retrying the *same*
    publisher instance safe: each attempt picks up where the last one
    left off instead of reposting what already made it through. A
    permanent per-channel error (`exc.retryable is False`, design.md §13's
    D2, a 4xx, a channel that's gone, one lacking permission) stops the
    loop immediately instead of burning the rest of the backoff schedule
    on something no amount of waiting fixes. On final failure, the
    mapping returned comes from the error itself
    (`PublishError.posted_by_topic`), not an empty one: those ids are
    real messages sitting in real channels, and the caller needs them to
    record against the `failed` row so a human (or the run-now
    confirmation) knows part of the digest already posted.
    """
    last_error: PublishError | None = None
    for attempt in range(len(_PUBLISH_BACKOFF_S) + 1):
        try:
            return await publisher.publish(rendered), None
        except PublishError as exc:
            last_error = exc
            if not exc.retryable:
                break
            if attempt < len(_PUBLISH_BACKOFF_S):
                await sleep(_PUBLISH_BACKOFF_S[attempt])
    return (dict(last_error.posted_by_topic) if last_error else {}), last_error


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
    name shown in logs and coverage notes: there's no config source to
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
    item list: globbing it in here would turn "no LLM configured" into
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


async def _stdout_guild_notice(guild_id: int, text: str) -> None:
    print(f"[notice for server {guild_id}] {text}", file=sys.stderr)


class _DropWebSearchKeyWarning(logging.Filter):
    """Filters out load_config's "BRAVE_API_KEY is not set" warnings, and nothing else."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "BRAVE_API_KEY is not set" not in message


@contextmanager
def _quiet_web_search_key_warning():
    """Silence config.py's "web search ... BRAVE_API_KEY is not set" warning for one call.

    Only ever used around `load_config` when `--fixtures` is given:
    `--fixtures` throws every real collector away, web search included,
    so a config that happens to have one configured has nothing to warn
    about here: the warning exists to flag a real run that's about to
    quietly lose a source, not an offline demo that was never going to
    call out to Brave regardless. Scoped to this one message, on this one
    call, so a real run (no --fixtures) still sees it, and so does
    anything else `load_config` might have to say.
    """
    config_logger = logging.getLogger("newsbot.config")
    warning_filter = _DropWebSearchKeyWarning()
    config_logger.addFilter(warning_filter)
    try:
        yield
    finally:
        config_logger.removeFilter(warning_filter)


# --- The modes ---


def _parse_now(text: str) -> datetime:
    """`--now` as an aware datetime (a bare timestamp is read as UTC). Raises `ValueError`."""
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _run_collect_cli(
    cfg: AppConfig, db_path: str, collectors: list[Collector], now: Callable[[], datetime]
):
    """One collection pass into `db_path`; SHiFT codes are released and printed, never delivered.

    The fan-out runs in preview mode: no claim, no posted or failed marks, no
    ping spent, no pending recovery. Delivering for real is the bot's job.
    """
    from newsbot.pipeline.collect import CollectionDeps, run_collection
    from newsbot.shift.fanout import FanoutDeps, make_shift_hook
    from newsbot.shift.sweep import PrintCodeAlertPoster

    fanout = FanoutDeps(
        cfg=cfg,
        db_path=db_path,
        now=now,
        poster_for=lambda _guild_id, _channel_id, _ping, _notify: PrintCodeAlertPoster(),
        notify_guild=_stdout_guild_notice,
        alert_owner=_stdout_alert,
        preview=True,
    )
    async with httpx.AsyncClient(headers=user_agent_headers()) as http:
        deps = CollectionDeps(
            cfg=cfg,
            db_path=db_path,
            http=http,
            collectors=collectors,
            now=now,
            alert=_stdout_alert,
            shift_hook=make_shift_hook(fanout),
        )
        return await run_collection(deps)


def _pick_guild(db_path: str, wanted: int | None) -> tuple[int | None, str | None]:
    """The server a digest mode runs for, or `(None, why not)`.

    `--guild` names one. Without it, exactly one set-up server is the
    self-hoster's case and gets picked; none or several is an error that says
    what there is to choose from.
    """
    with closing(connect(db_path)) as conn:
        guilds = repo.list_set_up_guilds(conn)
    ids = sorted(g.guild_id for g in guilds)
    if wanted is not None:
        if wanted not in ids:
            listed = ", ".join(str(i) for i in ids) or "none"
            return None, f"server {wanted} isn't set up in this database (set up: {listed})"
        return wanted, None
    if not ids:
        return None, "no server is set up in this database yet"
    if len(ids) > 1:
        return None, "several servers are set up; pick one with --guild (" + ", ".join(
            str(i) for i in ids
        ) + ")"
    return ids[0], None


async def _run_digest_cli(
    cfg: AppConfig,
    db_path: str,
    guild_id: int,
    now: Callable[[], datetime],
    llm: LLMClient | None,
    *,
    post: bool,
    force: bool,
) -> int:
    """Preview (`post=False`) or really run (`post=True`) one server's digest; print, don't post."""
    from newsbot.guilds.schedule import due_guilds
    from newsbot.pipeline.guild_digest import (
        GuildDigestDeps,
        GuildTimeZoneError,
        preview_guild_digest,
        run_guild_digest,
    )
    from newsbot.pipeline.summaries import (
        SummaryDeps,
        dry_run_lookup,
        retry_lookup,
        summary_lookup,
    )

    summary_for = retry_for = None
    if llm is not None:
        summary_deps = SummaryDeps(cfg=cfg, db_path=db_path, llm=llm, now=now, alert=_stdout_alert)
        # A rehearsal must not leave summaries behind for the real digest to reuse.
        summary_for = summary_lookup(summary_deps) if post else dry_run_lookup(summary_deps)
        retry_for = retry_lookup(summary_deps) if post else None
    deps = GuildDigestDeps(
        cfg=cfg,
        db_path=db_path,
        now=now,
        publisher_for=lambda _guild, _scope, _already, _on_posted: PrintPublisher(),
        notify_guild=_stdout_guild_notice,
        summary_for=summary_for,
        retry_summary=retry_for,
    )
    try:
        if not post:
            preview = await preview_guild_digest(deps, guild_id)
            if preview is None:
                print(
                    f"newsbot: server {guild_id} isn't set up or follows no games", file=sys.stderr
                )
                return 1
            print(to_text(preview.rendered))
            for note in preview.notes:
                print(f"[note] {note}", file=sys.stderr)
            return 0

        if force:
            outcome = await run_guild_digest(deps, guild_id, kind=RunKind.RUN_NOW, force=True)
        else:
            with closing(connect(db_path)) as conn:
                candidates = repo.due_candidates(conn)
            due = next(
                (d for d in due_guilds(candidates, now(), deps.running) if d.guild_id == guild_id),
                None,
            )
            if due is None:
                print(
                    f"newsbot: server {guild_id} isn't due a digest at {now().isoformat()} "
                    "(not yet its time, or today's is already claimed); use --force to run anyway",
                    file=sys.stderr,
                )
                return 0
            kind = RunKind.CATCH_UP if due.catch_up else RunKind.SCHEDULED
            outcome = await run_guild_digest(deps, guild_id, kind=kind, due=due)
    except GuildTimeZoneError as exc:
        print(f"newsbot: {exc}", file=sys.stderr)
        return 1
    logger.info(
        "guild digest run finished",
        extra={"guild_id": guild_id, "status": outcome.status, "notes": outcome.notes},
    )
    for note in outcome.notes:
        print(f"[note] {note}", file=sys.stderr)
    return 1 if outcome.status == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    """`python -m newsbot.pipeline.run`: the CLI front door for a run with no Discord.

    See the module docstring for the modes. `--config` (or `$NEWSBOT_CONFIG`)
    is always required. Secrets (`ANTHROPIC_API_KEY`, and `BRAVE_API_KEY` and
    Bluesky's if configured) are only loaded for whatever `--fixtures` and
    `--stub-llm` didn't replace, so a fully offline run needs none of them.

    `--check-sources` is its own thing entirely, checked before any database
    is touched: it never loads `ANTHROPIC_API_KEY` or `DISCORD_TOKEN`, so it
    works for sanity-checking a new `config.yaml` before spending an
    Anthropic call or a Discord token on it.
    """
    parser = argparse.ArgumentParser(prog="python -m newsbot.pipeline.run")
    parser.add_argument("--config", default=os.environ.get("NEWSBOT_CONFIG"))
    parser.add_argument("--db", default=os.environ.get("NEWSBOT_DB", "./data/newsbot.db"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "preview one server's next digest from stored items (the default mode); works on a "
            "throwaway copy of --db, so nothing is written, the one-time import included"
        ),
    )
    mode.add_argument(
        "--post-to-stdout",
        action="store_true",
        help=(
            "run one server's digest for real (claim, print, save), printing instead of "
            "posting; writes to --db, so never point it at a live database"
        ),
    )
    parser.add_argument(
        "--guild",
        type=int,
        help="the server for --dry-run and --post-to-stdout; optional when exactly one is set up",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help=(
            "run one collection pass into --db (new SHiFT codes are released and printed; "
            "nothing is delivered, claimed or marked posted, and no ping is spent); "
            "still writes to --db, so never point it at a live database"
        ),
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="the old name for --collect; kept as an alias for one release",
    )
    parser.add_argument(
        "--fixtures",
        help="directory of *.json fixture files, replacing collectors (implies one --collect pass)",
    )
    parser.add_argument(
        "--stub-llm", help="JSON file of canned stories, replacing the Claude client"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--now",
        help=(
            "ISO 8601 timestamp to use as every step's clock instead of the real wall "
            "clock (e.g. 2026-09-23T17:00:00Z); mainly for --fixtures, whose canned "
            "published_at values are pinned to a fixed date and drift out of "
            "collection.lookback_hours the moment 'today' moves on without them"
        ),
    )
    parser.add_argument(
        "--check-sources",
        action="store_true",
        help=(
            "run the configured sources for real and report items/errors per source and "
            "game, then exit; needs no Anthropic or Discord credential"
        ),
    )
    parser.add_argument(
        "--game",
        action="append",
        metavar="KEY",
        help="with --check-sources: only check this catalog game (repeatable)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    warn_if_contact_unset()

    if not args.config:
        print("newsbot: --config (or $NEWSBOT_CONFIG) is required", file=sys.stderr)
        return 2
    if args.game and not args.check_sources:
        print("newsbot: --game only works together with --check-sources", file=sys.stderr)
        return 2
    try:
        with _quiet_web_search_key_warning() if args.fixtures else nullcontext():
            cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
    if args.now:
        try:
            parsed_now = _parse_now(args.now)
        except ValueError as exc:
            print(
                f"newsbot: --now {args.now!r} is not a valid ISO 8601 timestamp: {exc}",
                file=sys.stderr,
            )
            return 2
        now = lambda: parsed_now  # noqa: E731

    if args.check_sources:
        unknown = [key for key in args.game or [] if key not in {g.key for g in cfg.catalog}]
        if unknown:
            print(
                "newsbot: --game names a game that isn't in the catalog: " + ", ".join(unknown),
                file=sys.stderr,
            )
            return 2
        return asyncio.run(_run_check_sources_cli(cfg, args.config, games=args.game))

    if args.sweep:
        print(
            "newsbot: --sweep is now --collect (SHiFT detection runs inside the collection "
            "pass); the old name stays as an alias for one release",
            file=sys.stderr,
        )
        args.collect = True
    want_collect = args.collect or bool(args.fixtures)
    want_digest = args.dry_run or args.post_to_stdout or not args.collect

    # The one mode that must leave the file alone: a digest preview with no collection pass.
    read_only = want_digest and not args.post_to_stdout and not want_collect
    with _read_only_copy(args.db) if read_only else nullcontext(args.db) as db_path:
        return _run_modes(args, db_path, cfg, now, want_collect, want_digest, read_only=read_only)


def _run_modes(
    args: argparse.Namespace,
    db_path: str,
    cfg: AppConfig,
    now: Callable[[], datetime],
    want_collect: bool,
    want_digest: bool,
    *,
    read_only: bool,
) -> int:
    """Everything `main` does once it knows which database file it's really working on."""
    # Secrets are only needed for the pieces --fixtures and --stub-llm didn't
    # replace: real collectors want BRAVE_API_KEY (and Bluesky's, if
    # configured), the real LLM wants ANTHROPIC_API_KEY.
    secrets = None
    needs_collectors = want_collect and not args.fixtures
    needs_llm = want_digest and not args.stub_llm
    if needs_collectors or needs_llm:
        try:
            secrets = load_secrets(require_discord=False)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    db_parent = Path(db_path).parent
    if str(db_parent) not in ("", "."):
        db_parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        migrate(conn)
        assert_fts5(conn)

    from newsbot.guilds.importer import ImportFailedError, ensure_imported

    try:
        report = ensure_imported(db_path, cfg, now)
    except ImportFailedError as exc:
        print(f"newsbot: {exc}", file=sys.stderr)
        return 2
    if report is not None:
        print(f"[import] {report.owner_notice()}", file=sys.stderr)
        if read_only:
            print(
                "[import] that happened in a throwaway copy; a dry run leaves your database alone",
                file=sys.stderr,
            )

    if want_collect:
        if args.fixtures:
            collectors = build_fixture_collectors(Path(args.fixtures))
        else:
            from newsbot.pipeline.collect import build_collection_collectors

            collectors = build_collection_collectors(cfg, secrets)
        outcome = asyncio.run(_run_collect_cli(cfg, db_path, collectors, now))
        if outcome.skipped:
            print("newsbot: collection skipped, a run is already in progress", file=sys.stderr)
        else:
            print(f"collection: {outcome.summary}")

    if not want_digest:
        return 0

    guild_id, why_not = _pick_guild(db_path, args.guild)
    if guild_id is None:
        print(f"newsbot: {why_not}", file=sys.stderr)
        return 2

    llm: LLMClient | None
    if args.stub_llm:
        llm = StubLLM(Path(args.stub_llm))
    else:
        llm = AnthropicLLM(secrets.anthropic_api_key.get_secret_value())
    return asyncio.run(
        _run_digest_cli(
            cfg, db_path, guild_id, now, llm, post=args.post_to_stdout, force=args.force
        )
    )


@contextmanager
def _read_only_copy(db_path: str):
    """Yield a throwaway copy of the database at `db_path`, so a rehearsal can't touch the real one.

    The import, the migrations and `connect()`'s own WAL pragma all write, and a
    dry run that "only" imports has already spent a v2.2 server's SHiFT ping
    budget at the wrong moment. So the file is opened read-only, copied with
    sqlite's backup API (which is safe against a live writer) into a temp
    directory, and everything runs there. A path that doesn't exist yet just
    starts empty in the temp directory; nothing gets created at the real one.
    """
    with tempfile.TemporaryDirectory(prefix="newsbot-dry-run-") as scratch:
        copy = Path(scratch) / "copy.db"
        if Path(db_path).exists():
            source = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
            try:
                with closing(sqlite3.connect(copy)) as target:
                    source.backup(target)
            finally:
                source.close()
        yield str(copy)


# --- --check-sources ---


@dataclass
class CheckSourcesReport:
    """What `--check-sources` found: per-source results and per-game match counts.

    Deliberately holds nothing that touches a database or a wall clock
    beyond what `run_check_sources` needs internally: this is meant to be
    easy to build by hand in a test and easy to render without re-running
    anything.
    """

    results: list[CollectorResult]
    topic_counts: dict[str, int]


def _restricted(cfg: AppConfig, games: Sequence[str] | None) -> AppConfig:
    """`cfg` cut down to `games`: their own sources, and the shared ones that could cover them.

    A shared source with no `games` restriction stays (the keyword matcher
    decides what it's about, and with only these games in the catalog that's
    all it can say); one restricted to other games goes.
    """
    if not games:
        return cfg
    wanted = set(games)
    return cfg.model_copy(
        update={
            "catalog": [g for g in cfg.catalog if g.key in wanted],
            "shared_sources": [
                s for s in cfg.shared_sources if not s.games or wanted.intersection(s.games)
            ],
        }
    )


async def run_check_sources(
    cfg: AppConfig, secrets: Secrets, http: httpx.AsyncClient, *, games: Sequence[str] | None = None
) -> CheckSourcesReport:
    """Run the catalog's and shared sources once (and web search, if keyed) and report.

    The same collect, normalize, filter shape the hourly pass uses, minus
    everything downstream of "did this source have anything relevant to say":
    no summarizing (no Anthropic call, ever), no store dedupe (the
    `existing_urls` lookup is an empty set, since this is a stateless sanity
    check and shouldn't need a writable database), and nothing saved anywhere.
    `games` limits it to those games' own sources plus the shared sources
    that could cover them, matched only against those games.
    `build_catalog_collectors` itself declines to build web search without
    `secrets.brave_api_key`; the CLI wrapper turns that absence into a
    readable note instead of a source that silently isn't in the results.
    """
    sub = _restricted(cfg, games)
    collectors = build_catalog_collectors(sub, secrets, web_search_games=sub.catalog)
    timeout_s = _COLLECT_TIMEOUT_S
    if sub.web_search is not None and secrets.brave_api_key is not None:
        # The search collector pauses between queries, so its time grows with the games
        # searched; the flat 20 s would cut a big catalog off mid-way.
        from newsbot.pipeline.collect import _web_search_timeout

        timeout_s = max(
            timeout_s, _web_search_timeout(len(sub.catalog), sub.web_search.queries_per_game)
        )
    results = await run_collectors(collectors, http, timeout_s=timeout_s)
    collected = [item for result in results for item in result.items]
    lookback = timedelta(hours=sub.collection.lookback_hours)
    normalized = normalize(collected, lambda _urls: set(), datetime.now(UTC), lookback)
    grouped = filter_items(normalized, sub.catalog, sub.collection.max_items_per_game)
    topic_counts = {game.key: len(grouped.get(game.key, [])) for game in sub.catalog}
    return CheckSourcesReport(results=results, topic_counts=topic_counts)


def _group_results(
    cfg: AppConfig, results: Sequence[CollectorResult]
) -> list[tuple[str, list[CollectorResult]]]:
    """Results grouped for the table: each game's own sources in catalog order, then the rest."""
    owner: dict[str, GameCfg] = {s.name: g for g in cfg.catalog for s in g.sources}
    by_game: dict[str, list[CollectorResult]] = {g.key: [] for g in cfg.catalog}
    other: list[CollectorResult] = []
    for result in results:
        game = owner.get(result.source_name)
        (by_game[game.key] if game is not None else other).append(result)
    groups = [(f"{g.name}", by_game[g.key]) for g in cfg.catalog if by_game[g.key]]
    if other:
        groups.append(("Shared and web search", other))
    return groups


def render_check_sources_report(
    cfg: AppConfig,
    report: CheckSourcesReport,
    *,
    web_search_note: str | None,
    web_search_skipped: int = 0,
) -> str:
    """Turn a `CheckSourcesReport` into the table `--check-sources` prints.

    Exit code 0 either way (`main` never looks at `report` to decide
    that): a source that timed out or 404'd is exactly the kind of thing
    an owner is running this to find out about, not a reason to make the
    command itself look like it failed. The summary line at the bottom
    is what carries that news instead.

    `web_search_skipped` is how many *unkeyed* web searches
    `web_search_note` is talking about. They never become a
    `CollectorResult` with `skipped` set (no collector is built without a
    key), so the summary's own `skipped` count would otherwise read "0
    skipped" right above a table note saying web search was skipped: true
    of the results list, misleading about what actually happened.
    """
    lines = ["Sources:"]
    if report.results:
        name_w = max(len("SOURCE"), *(len(r.source_name) for r in report.results))
        type_w = max(len("TYPE"), *(len(r.source_type) for r in report.results))
        for heading, group in _group_results(cfg, report.results):
            lines.append(f"  {heading}")
            lines.append(f"    {'SOURCE':<{name_w}}  {'TYPE':<{type_w}}  {'ITEMS':>5}  FIRST ERROR")
            for r in group:
                first_line = (
                    (r.error or r.skipped or "").splitlines()[0] if (r.error or r.skipped) else ""
                )
                lines.append(
                    f"    {r.source_name:<{name_w}}  {r.source_type:<{type_w}}  "
                    f"{len(r.items):>5}  {first_line}"
                )
    else:
        lines.append("  (no sources configured)")
    if web_search_note:
        lines.append(f"  {web_search_note}")

    lines.append("")
    lines.append("Games matched:")
    for game in cfg.catalog:
        if game.key not in report.topic_counts:
            continue
        count = report.topic_counts[game.key]
        lines.append(f"  {game.key} ({game.name}): {count} item(s)")

    ok = sum(1 for r in report.results if r.error is None and r.skipped is None)
    failed = sum(1 for r in report.results if r.error is not None)
    skipped = sum(1 for r in report.results if r.skipped is not None) + web_search_skipped
    total_items = sum(report.topic_counts.values())
    lines.append("")
    lines.append(
        f"Summary: {ok} source(s) ok, {failed} failed, {skipped} skipped; "
        f"{total_items} item(s) matched across {len(report.topic_counts)} game(s)."
    )
    return "\n".join(lines)


def _web_search_note(cfg: AppConfig, secrets: Secrets, config_path: str) -> str | None:
    """The one extra line `--check-sources` prints about web search, or `None`.

    `None` when web search isn't configured at all (nothing to say) or
    when it's configured *and* keyed (it ran, and shows up in the
    ordinary source table like anything else): a note only earns its
    place when there's a search in config.yaml that didn't get a chance
    to run.
    """
    configured = count_configured_web_search_sources(config_path)
    if configured == 0:
        return None
    if secrets.brave_api_key is None:
        return (
            f"web_search: {configured} source(s) configured but BRAVE_API_KEY is not set; skipped"
        )
    return None


async def _run_check_sources_cli(
    cfg: AppConfig, config_path: str, games: Sequence[str] | None = None
) -> int:
    """`python -m newsbot.pipeline.run --check-sources`: the CLI wrapper around `run_check_sources`.

    Never reads `ANTHROPIC_API_KEY` or `DISCORD_TOKEN`, directly or
    indirectly: `load_check_sources_secrets` is the only secrets loader
    this path calls, and it's built specifically to not need either.
    """
    secrets = load_check_sources_secrets()
    async with httpx.AsyncClient(headers=user_agent_headers()) as http:
        report = await run_check_sources(cfg, secrets, http, games=games)
    web_search_note = _web_search_note(cfg, secrets, config_path)
    web_search_skipped = count_configured_web_search_sources(config_path) if web_search_note else 0
    print(
        render_check_sources_report(
            _restricted(cfg, games),
            report,
            web_search_note=web_search_note,
            web_search_skipped=web_search_skipped,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CheckSourcesReport",
    "FixtureCollector",
    "RunKind",
    "StubLLM",
    "build_fixture_collectors",
    "local_run_date",
    "main",
    "render_check_sources_report",
    "run_check_sources",
]
