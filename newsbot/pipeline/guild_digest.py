"""Post one server's digest, and find the servers that are owed one.

v2's `run_daily` was the ancestor of this: one server, one claim, one
publish, one save. This is the same guard with the server as a parameter. The
part I'd rather not get wrong is unchanged from v2: claim the day with a
`pending` row *before* anything slow, publish, write what landed, and only then
say the day is done. A crash between those steps leaves evidence on the row
instead of a silent gap, and (a change from v2, owner decision D7) the next
start *resumes* it: each game's message id is written through to the row as it
lands, so a resume posts only the games that are missing. The worst case is one
duplicate game post, if the process died between Discord saying "sent" and the
row hearing about it. That's a trade I'll take over a server that never gets
its digest until an admin notices.

Nothing here fetches anything. Collection already stored the items (plan
§3.4); a free server's digest is a headline list built from the last window's
stored items, and a comped server's is the shared Claude summary for that game
and day (`game_summaries`, written by `summaries.py`, task 7), or headlines
with a "Summary unavailable" note if there isn't one. Publishing is injected
(`GuildDigestDeps.publisher_for`), as is every way of telling a human
something, so all of it runs in tests without a Discord in sight. Problems
that belong to a server go to that server's own notifier, never the owner.

Servers are processed one after another with a pause between servers that
actually sent something, and one server's failure is logged and left at that; it
doesn't get to delay or break the next one. That includes a hung one: each server
gets `GUILD_TIMEOUT_S` and then the tick moves on, leaving its row `pending`.
A row that keeps getting cut off doesn't get unlimited tries: every resume is an
attempt, and after `MAX_ATTEMPTS` the row is marked failed and its server told once.

A `pending` row is also a lease (the rules are in `guilds/schedule.py`). The
publisher refreshes it as each game lands and on a `HEARTBEAT_S` heartbeat, so a
second process only resumes a digest once the first has been quiet for
`LEASE_STALE_AFTER`. A graceful cancel (a deploy) deliberately leaves the row
`pending` for the same reason: it's a pause, not a failure.

`NewsBot` runs the tick every minute (once its startup work is done), and the
CLI's `--dry-run` and `--post-to-stdout` call the same functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfoNotFoundError

from newsbot.bot.format import (
    RenderedDigest,
    esc,
    render_guild_digest,
    render_guild_run_report,
)
from newsbot.config import AppConfig, GameCfg
from newsbot.guilds.schedule import (
    CATCH_UP_AFTER,
    MAX_ATTEMPTS,
    RETRY_AFTER,
    DueGuild,
    digest_window,
    due_guilds,
    item_floor,
    local_due_instant,
)
from newsbot.pipeline.publisher import Publisher
from newsbot.pipeline.run import RunKind, _publish_with_retry, local_run_date
from newsbot.pipeline.summarize import _FALLBACK_NOTE, StoryDraft
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import (
    Coverage,
    GameSummaryRow,
    GuildClaim,
    GuildDigestRow,
    GuildGame,
    GuildSettings,
    ItemRange,
)
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

# A breath between servers so a few hundred digests due at 09:00 don't become a
# few hundred bursts in the same second. discord.py handles the per-route 429s;
# this is just manners.
_GUILD_PACE_S = 1.0
# The most one server's digest may take inside the tick before the tick gives up
# on it and moves on. Well over a real digest (a few seconds of sends, plus at most
# 14 s of retry backoff), and it exists so one hung server can't hold up every
# server behind it. Giving up cancels the run, so its publisher (and heartbeat)
# stop; the row stays `pending`, its lease goes stale after
# `schedule.LEASE_STALE_AFTER`, and the next tick resumes it, as a new attempt.
# That's the part I got wrong the first time: a digest that hangs every time was
# resumed and cut off every ten minutes forever, so resumes now count against
# `MAX_ATTEMPTS`. (A database write already handed to a thread can't be cancelled
# and may finish after the timeout; those are single transactions, and the resume
# reads the row as they left it.)
GUILD_TIMEOUT_S = 300.0
# What a comped digest may spend finding its summaries when the prepare job didn't
# make them (downtime, or a server set up late): an inline model call is allowed this
# long per game, and this long across the whole digest, both well under
# `GUILD_TIMEOUT_S`. When the time is up the game posts as headlines with the usual
# "summary unavailable" footer, instead of the whole digest being cancelled.
SUMMARY_TIMEOUT_S = 120.0
SUMMARY_BUDGET_S = 180.0
# Even with the budget spent, a lookup gets this long, so a summary that's already
# stored (a database read) isn't skipped just because an earlier game ate the clock.
SUMMARY_MIN_S = 1.0
# How often a publishing digest refreshes its row's lease.
HEARTBEAT_S = 60.0
# Backoff for a server whose run crashes: it's not retried until this long after
# the last crash, doubling each time up to the cap.
_CRASH_BACKOFF_CAP = timedelta(hours=6)

Sleep = Callable[[float], Awaitable[None]]
NotifyGuild = Callable[[int, str], Awaitable[None]]
OnPosted = Callable[[str, int], Awaitable[None]]
# (guild, nonce scope, games already posted, write-through callback) -> a publisher
# for that server's digest. The real one builds a `DiscordPublisher` with
# `skip_permanent=True`.
PublisherFactory = Callable[[GuildSettings, str, dict[str, int], OnPosted], Publisher]
# (guild id, admin channel id, text). Only called when the guild has an admin channel.
ReportSender = Callable[[int, int, str], Awaitable[None]]


@dataclass(frozen=True)
class GameSummary:
    """A stored per-game summary, as a comped digest needs it (the interface task 7 fills).

    `status` is `ok` (the stories are the summary) or `fallback` (Claude failed; the
    digest shows headlines with `note`). `coverage_notes` ride in the footer.
    """

    status: Literal["ok", "fallback"]
    stories: list[StoryDraft]
    coverage_notes: list[str]
    note: str | None = None
    # The newest item id the summary covers, which is where this server's next summary
    # for the game must start. `None` for a summary that doesn't know (a stub, an old row).
    items_upto: int | None = None


class SummaryLookup(Protocol):
    """(game key, the digest's due instant, the server's coverage of that game) -> a summary.

    `None` if there isn't one. The coverage (where the server's last digest left off)
    is `None` for a server's first digest. The reuse rule (see `repo.get_game_summary`)
    needs all three;
    `summaries.summary_lookup` wraps the stored lookup with an inline "make one now".
    """

    def __call__(
        self, game_key: str, due_at: datetime, after: Coverage | None = None, /
    ) -> Awaitable[GameSummary | None]: ...


@dataclass
class GuildDigestDeps:
    cfg: AppConfig
    db_path: str
    now: Callable[[], datetime]
    publisher_for: PublisherFactory
    notify_guild: NotifyGuild
    # None reads whatever `game_summaries` already holds. `summaries.summary_lookup`
    # builds the real one, which makes a missing summary inline.
    summary_for: SummaryLookup | None = None
    # What a preview uses instead of `summary_for`, when set: `summaries.dry_run_lookup`, which
    # reuses a stored summary or makes one in memory and never saves it. A preview that saved
    # its summary would hand it to the real digest (plan §3.7: preview writes nothing).
    preview_summary_for: SummaryLookup | None = None
    # Retries a game's `fallback` summary once, for a confirmed run-now (plan §3.6).
    # None means run-now just shows what's stored.
    retry_summary: SummaryLookup | None = None
    send_report: ReportSender | None = None
    sleep: Sleep = asyncio.sleep
    pace_s: float = _GUILD_PACE_S
    guild_timeout_s: float = GUILD_TIMEOUT_S
    summary_timeout_s: float = SUMMARY_TIMEOUT_S
    summary_budget_s: float = SUMMARY_BUDGET_S
    summary_min_s: float = SUMMARY_MIN_S
    heartbeat_s: float = HEARTBEAT_S
    # Guilds this process is publishing for right now, so a slow digest isn't
    # mistaken for a crashed one by the due check.
    running: set[int] = field(default_factory=set)
    _locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    # How many `guild_lock` blocks are inside (holding or waiting) for each server.
    # A server with no entry here has nobody who could be about to take its lock.
    _users: dict[int, int] = field(default_factory=dict)

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        """One lock per server: the scheduler, run-now and preview take turns on it.

        This hands out the lock and never takes it back, so a caller that only wants
        to take turns should use `guild_lock`, which also tidies up after itself.
        """
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @asynccontextmanager
    async def guild_lock(self, guild_id: int) -> AsyncIterator[None]:
        """Hold this server's lock, and drop its table entry on the way out if nobody's left.

        Without the pruning the table keeps a lock for every server the process has
        ever digested (300 servers, 300 locks; a server that left keeps its entry
        too). Pruning is safe because it only happens here, in `forget_guild`, and
        nowhere else, and none of it awaits: `lock_for` plus the user count below
        run with no suspension point between them, and so does the check and pop in
        `forget_guild`. On the one event loop that makes each of them atomic, so a
        task can't be handed a lock that's about to be popped, and two tasks can't
        end up holding different locks for one server. (A count, rather than asking
        the lock whether it has waiters, because a lock that was just released has
        woken its next waiter without that waiter having taken it yet, and from the
        outside that looks exactly like idle.)
        """
        lock = self.lock_for(guild_id)
        self._users[guild_id] = self._users.get(guild_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            left = self._users[guild_id] - 1
            if left:
                self._users[guild_id] = left
            else:
                del self._users[guild_id]
                self.forget_guild(guild_id)

    def forget_guild(self, guild_id: int) -> None:
        """Drop a server's lock if it's idle: not held, not running, nobody inside `guild_lock`.

        Also called when the bot leaves a server. A server that's mid-digest at that
        moment keeps its entry; its own run prunes it when it finishes.
        """
        lock = self._locks.get(guild_id)
        if lock is None or lock.locked() or guild_id in self.running or guild_id in self._users:
            return
        del self._locks[guild_id]


@dataclass(frozen=True)
class GuildDigestOutcome:
    """What one server's digest did, for the tick's log and for run-now's reply."""

    guild_id: int
    status: Literal["ok", "partial", "failed", "skipped"]
    run_date: date | None = None
    posted_by_game: dict[str, int] = field(default_factory=dict)
    skipped_games: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rendered: RenderedDigest | None = None
    # Messages this run actually sent (games a resume found already posted don't count).
    # The tick paces on it: a server that sent nothing costs the next one no pause.
    sent: int = 0


class GuildTimeZoneError(Exception):
    """A server's stored time zone (or digest time) can't be used.

    Raised by the command-path helpers (`todays_guild_digest`, `preview_guild_digest`,
    a `run_guild_digest` that isn't on the schedule) instead of the `zoneinfo`
    error underneath, which is what you get when tzdata renames a zone out from
    under a stored setting. The command layer turns it into "your time zone setting
    is invalid; fix it with /newsbot settings". The scheduled path doesn't raise it:
    the due check logs and skips such a server.
    """


def _local_date(now: datetime, guild: GuildSettings) -> date:
    try:
        return local_run_date(now, guild.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise GuildTimeZoneError(
            f"server {guild.guild_id}'s time zone {guild.timezone!r} is invalid"
        ) from exc


@dataclass(frozen=True)
class GuildPreview:
    """What `/newsbot preview` shows: the digest as it would post right now, posted nowhere."""

    rendered: RenderedDigest
    notes: list[str]


def guild_needs_confirmation(existing: GuildDigestRow | None) -> bool:
    """True if `run-now` should ask before running again (v2's rule, per server).

    `ok`/`partial` (already posted), `pending` (something died mid-run, or is still
    running) and a `failed` row that got anything out all need an admin's "yes";
    no row, or a failure that never reached Discord, doesn't.
    """
    if existing is None:
        return False
    if existing.status in ("ok", "partial", "pending"):
        return True
    return existing.status == "failed" and bool(
        existing.posted_by_game or existing.posted_message_ids
    )


def is_guild_busy(deps: GuildDigestDeps, guild_id: int) -> bool:
    """True while a digest (scheduled, run-now or preview) holds this server's lock."""
    # `.get`, not `lock_for`: asking whether a server is busy mustn't create its lock.
    lock = deps._locks.get(guild_id)
    return lock is not None and lock.locked()


async def todays_guild_digest(deps: GuildDigestDeps, guild_id: int) -> GuildDigestRow | None:
    """The server's digest row for its current local date, for `run-now`'s confirmation check."""

    def _sync() -> GuildDigestRow | None:
        with closing(connect(deps.db_path)) as conn:
            guild = repo.get_guild(conn, guild_id)
            if guild is None:
                return None
            run_date = _local_date(deps.now(), guild)
            return repo.get_guild_digest(conn, guild_id, run_date)

    return await asyncio.to_thread(_sync)


# --- Building the digest ---


@dataclass
class _Built:
    rendered: RenderedDigest
    counts: dict[str, int]
    games: list[GameCfg]
    channels: dict[str, int]
    notes: list[str]
    degraded: bool
    # Per game, the newest item id a shared summary covered (see `repo.record_game_coverage`).
    game_upto: dict[str, int] = field(default_factory=dict)


def summary_from_row(conn, row: GameSummaryRow) -> GameSummary:
    """A stored summary row plus its stories, as the digest's `GameSummary`."""
    stories = [
        StoryDraft(
            headline=v.headline,
            summary=v.summary,
            label=v.label,
            item_urls=list(v.urls),
            update_of_story_id=v.is_update_of,
        )
        for v in repo.summary_stories(conn, row.id)
    ]
    return GameSummary(row.status, stories, list(row.coverage_notes), row.note, row.items_upto)


async def _stored_summary(
    db_path: str, game_key: str, due_at: datetime, after: Coverage | None
) -> GameSummary | None:
    def _sync() -> GameSummary | None:
        with closing(connect(db_path)) as conn:
            row = repo.get_game_summary(conn, game_key, due_at, after)
            return summary_from_row(conn, row) if row is not None else None

    return await asyncio.to_thread(_sync)


async def _build(
    deps: GuildDigestDeps,
    guild: GuildSettings,
    followed: list[GuildGame],
    window: tuple[datetime, datetime],
    rng: ItemRange,
    due_at: datetime,
    run_date: date,
    *,
    retry_fallback: bool = False,
    summary_for: SummaryLookup | None = None,
) -> _Built:
    """Render one server's digest from stored data.

    `window` is what the digest says it covers; `rng` is which items it really covers
    (ids, see `repo.item_range`). Writes nothing itself; a comped server's lookup may make
    a missing summary (see `summaries.py`), inside a deadline (`SUMMARY_TIMEOUT_S`, and
    `SUMMARY_BUDGET_S` for the whole digest) after which that game posts as headlines.
    `retry_fallback` (a confirmed run-now) asks for one more go at a summary that already
    fell back.
    """
    channels = {g.game_key: g.channel_id for g in followed}
    games = [g for g in deps.cfg.catalog if g.key in channels]
    notes = [
        f"{key}: not in the catalog any more, skipped"
        for key in channels
        if key not in {g.key for g in games}
    ]

    def _items_sync():
        with closing(connect(deps.db_path)) as conn:
            # Where this server's last digest left off, which a summary has to start at
            # to be neither a gap nor a repeat for it.
            coverage = repo.last_coverage(conn, guild.guild_id, exclude_run_date=run_date)
            after_by_game = (
                {key: coverage.for_game(key).item_id for key in channels} if coverage else {}
            )
            items = repo.items_for_window(
                conn,
                guild.guild_id,
                rng.floor,
                window[1],
                after_id=rng.after,
                upto_id=rng.upto,
                after_by_game=after_by_game,
            )
            return items, coverage

    items_by_game, coverage_before = await asyncio.to_thread(_items_sync)

    stories_by_game: dict[str, list[StoryDraft]] = {}
    notes_by_game: dict[str, str | None] = {}
    coverage: list[str] = []
    game_upto: dict[str, int] = {}
    degraded = bool(notes)
    if guild.tier == "comped":
        lookup = (
            summary_for
            or deps.summary_for
            or (lambda key, at, after: _stored_summary(deps.db_path, key, at, after))
        )
        clock = asyncio.get_running_loop().time
        spend_until = clock() + deps.summary_budget_s
        for game in games:
            before = coverage_before.for_game(game.key) if coverage_before else None
            allowed = max(min(deps.summary_timeout_s, spend_until - clock()), deps.summary_min_s)
            try:
                async with asyncio.timeout(allowed) as limit:
                    summary = await lookup(game.key, due_at, before)
                    if (
                        retry_fallback
                        and deps.retry_summary is not None
                        and summary is not None
                        and summary.status == "fallback"
                    ):
                        summary = await deps.retry_summary(game.key, due_at, before) or summary
            except Exception:
                # A broken or slow lookup costs this game its summary, not the server its
                # digest. (A timeout says so in one line; a bug gets its traceback.)
                if limit.expired():
                    logger.warning(
                        "summary for %s took more than %.0f s; posting headlines",
                        game.key,
                        allowed,
                        extra={"guild_id": guild.guild_id},
                    )
                else:
                    logger.exception("summary lookup failed for %s", game.key)
                summary = None
            if summary is not None and summary.status == "ok":
                stories_by_game[game.key] = summary.stories
                coverage.extend(n for n in summary.coverage_notes if n not in coverage)
                if summary.items_upto is not None:
                    game_upto[game.key] = summary.items_upto
                continue
            notes_by_game[game.key] = (summary.note if summary else None) or _FALLBACK_NOTE
            # Headlines standing in for a summary is a degraded digest, but only
            # where there were headlines to stand in: a quiet game isn't a failure.
            if items_by_game.get(game.key):
                degraded = True
    if coverage:
        degraded = True

    rendered = render_guild_digest(
        run_date,
        games,
        channels,
        stories_by_game=stories_by_game,
        items_by_game=items_by_game,
        notes_by_game=notes_by_game,
        coverage_notes=coverage,
    )
    counts = {
        game.key: (
            len(stories_by_game[game.key])
            if game.key in stories_by_game
            else len(items_by_game.get(game.key, []))
        )
        for game in games
    }
    return _Built(rendered, counts, games, channels, notes, degraded, game_upto)


def _window_sync(
    db_path: str, guild_id: int, run_date: date, end: datetime, now: datetime, *, late: bool
) -> tuple[datetime, datetime]:
    with closing(connect(db_path)) as conn:
        previous = repo.last_window_end(conn, guild_id, exclude_run_date=run_date)
    # A server's very first digest, when it's running late (the server was set up after
    # its digest time, or the bot was down), ends now instead of at the due instant it
    # missed: one set up at 14:00 gets news up to 14:00, not a digest already five hours
    # stale. Catch-up for a server that already has digests keeps ending at the due
    # instant, so what piled up during the downtime lands in its next window.
    if previous is None and late:
        end = now
    return digest_window(end, previous)


def _range_sync(db_path: str, guild_id: int, run_date: date, end: datetime) -> ItemRange:
    """The item range a preview would cover (a real digest gets its range from its claim)."""
    with closing(connect(db_path)) as conn:
        return repo.item_range(conn, guild_id, exclude_run_date=run_date, end=end)


def _claim_range(claim: GuildClaim) -> ItemRange:
    """The claim's item range: its stored ids, and the floor its window end implies."""
    return ItemRange(
        claim.items_after,
        claim.items_upto or 0,
        item_floor(claim.window_end, claim.items_after is not None),
    )


def _load_sync(db_path: str, guild_id: int) -> tuple[GuildSettings, list[GuildGame]] | None:
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, guild_id)
        if guild is None or not guild.set_up:
            return None
        games = repo.list_guild_games(conn, guild_id)
    return (guild, games) if games else None


def _when(deps: GuildDigestDeps, guild: GuildSettings, kind: RunKind, due: DueGuild | None):
    """(local run date, due instant, window end) for this run."""
    now = deps.now()
    if due is not None:
        run_date, due_at = due.run_date, due.due_at
    else:
        run_date = _local_date(now, guild)
        try:
            due_at = local_due_instant(run_date, guild.digest_time, guild.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise GuildTimeZoneError(f"server {guild.guild_id}'s digest time is invalid") from exc
    # Scheduled and catch-up windows end at the due instant, so items collected
    # while the bot was down land in the next window instead of being lost or
    # double-counted; run-now ends at "now".
    window_end = now if kind is RunKind.RUN_NOW else due_at
    return run_date, due_at, window_end


async def preview_guild_digest(deps: GuildDigestDeps, guild_id: int) -> GuildPreview | None:
    """Build what this server's digest would be right now, without claiming or posting anything.

    `None` if the server isn't set up or follows nothing. Takes the server's lock, so a
    preview can't interleave with its real digest. Saves nothing, a comped server's summary
    included: with `preview_summary_for` set, a missing summary is made in memory and dropped
    when the preview ends, which is why a preview costs a model call of its own.
    """
    async with deps.guild_lock(guild_id):
        loaded = await asyncio.to_thread(_load_sync, deps.db_path, guild_id)
        if loaded is None:
            return None
        guild, followed = loaded
        run_date, due_at, window_end = _when(deps, guild, RunKind.RUN_NOW, None)
        window = await asyncio.to_thread(
            _window_sync, deps.db_path, guild_id, run_date, window_end, deps.now(), late=False
        )
        rng = await asyncio.to_thread(_range_sync, deps.db_path, guild_id, run_date, window[1])
        built = await _build(
            deps,
            guild,
            followed,
            window,
            rng,
            due_at,
            run_date,
            summary_for=deps.preview_summary_for,
        )
        return GuildPreview(built.rendered, built.notes)


# --- Publishing ---


def _claim_sync(
    db_path: str,
    guild_id: int,
    run_date: date,
    *,
    force: bool,
    resume: bool,
    window: tuple[datetime, datetime],
    now: Callable[[], datetime],
) -> GuildClaim | None:
    with closing(connect(db_path)) as conn:
        return repo.claim_guild_digest(
            conn, guild_id, run_date, force=force, window=window, resume=resume, now=now
        )


def _record_posted_sync(
    db_path: str, digest_id: int, game_key: str, message_id: int, now: Callable[[], datetime]
) -> None:
    with closing(connect(db_path)) as conn:
        repo.record_posted_game(conn, digest_id, game_key, message_id, now=now)


def _save_sync(
    db_path: str,
    digest_id: int,
    status: str,
    posted: dict[str, int],
    notes: str | None,
    window: tuple[datetime, datetime],
    now: Callable[[], datetime],
) -> None:
    with closing(connect(db_path)) as conn:
        repo.save_guild_digest(conn, digest_id, status, posted, notes, window, now=now)


def _mark_failed_sync(
    db_path: str, digest_id: int, notes: str, posted: dict[str, int], now: Callable[[], datetime]
) -> None:
    with closing(connect(db_path)) as conn:
        repo.mark_guild_digest_failed(conn, digest_id, notes, posted, now=now)


def _coverage_sync(db_path: str, digest_id: int, game_upto: dict[str, int], keep: set[str]) -> None:
    with closing(connect(db_path)) as conn:
        repo.record_game_coverage(conn, digest_id, game_upto, keep=keep)


def _touch_sync(db_path: str, digest_id: int, now: Callable[[], datetime]) -> None:
    with closing(connect(db_path)) as conn:
        repo.touch_guild_digest(conn, digest_id, now=now)


def _status_sync(db_path: str, guild_id: int, run_date: date) -> str | None:
    with closing(connect(db_path)) as conn:
        row = repo.get_guild_digest(conn, guild_id, run_date)
    return row.status if row else None


async def _safe_notify(deps: GuildDigestDeps, guild_id: int, text: str) -> None:
    # Telling a server about a problem must not become the next problem.
    try:
        await deps.notify_guild(guild_id, text)
    except Exception:
        logger.exception("couldn't send a guild notice", extra={"guild_id": guild_id})


def _names(built: _Built, keys: list[str]) -> str:
    by_key = {g.key: g.name for g in built.games}
    return ", ".join(esc(by_key.get(k, k)) for k in keys) or "none"


async def run_guild_digest(
    deps: GuildDigestDeps,
    guild_id: int,
    *,
    kind: RunKind,
    force: bool = False,
    due: DueGuild | None = None,
) -> GuildDigestOutcome:
    """Claim, publish and save one server's digest for its current local day.

    `kind` is why it's running (shown on the run report). `due` is the due check's
    verdict for scheduled and catch-up runs, so a run that starts just past local
    midnight still files under the day it was due. `force=True` is a confirmed
    `run-now` (see `guild_needs_confirmation`): it replaces the day's row and
    reposts every game. Without it, a `pending` row left by a dead process is resumed
    (scheduled and catch-up only) and a finished one is refused (`skipped`).

    Takes the server's lock for the whole run. Anything unexpected after the claim
    leaves the row `failed` with whatever did post, then propagates. A cancellation
    (a deploy, or the tick's timeout) is the exception: it leaves the row `pending`,
    window and posted games intact, for a resume once the lease goes stale.

    Raises `GuildTimeZoneError` before claiming anything if the server's stored
    zone can't be used and `due` didn't already settle the date.
    """
    async with deps.guild_lock(guild_id):
        deps.running.add(guild_id)
        try:
            return await _run_locked(deps, guild_id, kind=kind, force=force, due=due)
        finally:
            deps.running.discard(guild_id)


async def _run_locked(
    deps: GuildDigestDeps, guild_id: int, *, kind: RunKind, force: bool, due: DueGuild | None
) -> GuildDigestOutcome:
    loaded = await asyncio.to_thread(_load_sync, deps.db_path, guild_id)
    if loaded is None:
        return GuildDigestOutcome(guild_id, "skipped", notes=["not set up, or follows no games"])
    guild, followed = loaded
    started = deps.now()
    run_date, due_at, window_end = _when(deps, guild, kind, due)
    late = started - due_at >= CATCH_UP_AFTER
    window = await asyncio.to_thread(
        _window_sync, deps.db_path, guild_id, run_date, window_end, started, late=late
    )
    claim = await asyncio.to_thread(
        _claim_sync,
        deps.db_path,
        guild_id,
        run_date,
        force=force,
        resume=kind is not RunKind.RUN_NOW,
        window=window,
        now=deps.now,
    )
    if claim is None:
        return GuildDigestOutcome(
            guild_id, "skipped", run_date, notes=["this day's digest is already claimed"]
        )

    heartbeat = asyncio.create_task(_heartbeat(deps, claim.digest_id))
    try:
        return await _publish_claimed(
            deps, guild, followed, claim, run_date, due_at, kind, force, started
        )
    except asyncio.CancelledError:
        # A deploy, or the tick giving up on a hung publisher. The row is already
        # `pending` with its window, and every game that landed was written through,
        # so the right move is to leave it alone: once its lease goes stale the next
        # tick resumes it and posts only what's missing. (It used to be marked
        # `failed`, which only an admin's run-now could ever finish.)
        logger.warning("guild %s's digest was cancelled; leaving it pending to resume", guild_id)
        raise
    except BaseException as exc:
        # Everything this function knows how to handle returns an outcome. What gets
        # here is a bug or a publisher raising something odd, and the one thing that
        # can't happen is the row staying `pending` with nobody the wiser. Unless the
        # digest already saved: an error inside the report must not relabel a good
        # digest as failed.
        status = await asyncio.to_thread(_status_sync, deps.db_path, guild_id, run_date)
        if status in ("ok", "partial"):
            logger.warning("error after guild %s's digest saved as %s", guild_id, status)
            raise
        logger.exception("unhandled error after claiming guild %s's digest", guild_id)
        await asyncio.to_thread(
            _mark_failed_sync,
            deps.db_path,
            claim.digest_id,
            f"unhandled error: {exc!r}",
            {},
            deps.now,
        )
        raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def _heartbeat(deps: GuildDigestDeps, digest_id: int) -> None:
    """Refresh the row's lease while the digest publishes, so nobody else resumes it.

    Real `asyncio.sleep`, not `deps.sleep`: that one is the tick's pacing and the
    retry backoff, and tests replace it with something instant.
    """
    while True:
        await asyncio.sleep(deps.heartbeat_s)
        try:
            await asyncio.to_thread(_touch_sync, deps.db_path, digest_id, deps.now)
        except Exception:
            logger.exception("couldn't refresh a digest's lease")


def _will_retry(claim: GuildClaim, posted: bool) -> bool:
    """True if the schedule will try this row again by itself (clean failure, tries left)."""
    return not posted and claim.attempts < MAX_ATTEMPTS


async def _publish_claimed(
    deps: GuildDigestDeps,
    guild: GuildSettings,
    followed: list[GuildGame],
    claim: GuildClaim,
    run_date: date,
    due_at: datetime,
    kind: RunKind,
    force: bool,
    started: datetime,
) -> GuildDigestOutcome:
    guild_id = guild.guild_id
    window = (claim.window_start, claim.window_end)
    try:
        built = await _build(
            deps,
            guild,
            followed,
            window,
            _claim_range(claim),
            due_at,
            run_date,
            retry_fallback=force and kind is RunKind.RUN_NOW,
        )
    except Exception as exc:
        logger.exception("building guild %s's digest failed", guild_id)
        await asyncio.to_thread(
            _mark_failed_sync, deps.db_path, claim.digest_id, f"build failed: {exc}", {}, deps.now
        )
        if _will_retry(claim, bool(claim.posted_by_game)):
            tail = "I'll try again shortly."
        else:
            tail = (
                "That was my last automatic try; once it's sorted out, "
                "an admin can run /newsbot run-now."
            )
        await _safe_notify(deps, guild_id, f"newsbot: I couldn't build today's digest. {tail}")
        return GuildDigestOutcome(guild_id, "failed", run_date, notes=[str(exc)])

    already = dict(claim.posted_by_game)
    await asyncio.to_thread(
        _coverage_sync, deps.db_path, claim.digest_id, built.game_upto, set(already)
    )
    to_post = RenderedDigest(
        run_date,
        [m for m in built.rendered.messages if m.topic_key not in already],
        built.rendered.coverage_notes,
    )

    async def on_posted(game_key: str, message_id: int) -> None:
        await asyncio.to_thread(
            _record_posted_sync, deps.db_path, claim.digest_id, game_key, message_id, deps.now
        )

    # A forced re-run gets a fresh nonce scope. discord.py 2.7.1 sends `enforce_nonce: true`
    # with every nonce (`discord/http.py`, `handle_message_parameters`), and Discord then
    # hands back the earlier message instead of posting when it has seen the same nonce in
    # that channel recently, so a deliberate repost a minute later would quietly vanish.
    # "Recently" is a few minutes and Discord doesn't promise more, so don't read this
    # as the resume's safety net: a resume waits out the 10 minute lease and is long past it.
    scope = f"{guild_id}|{run_date.isoformat()}" + (f"|{uuid.uuid4().hex}" if force else "")
    publisher = deps.publisher_for(guild, scope, already, on_posted)
    posted_new, error = await _publish_with_retry(publisher, to_post, sleep=deps.sleep)
    posted = {**already, **posted_new}
    sent = len(set(posted) - set(already))
    skipped_games = dict(getattr(publisher, "skipped", {}) or {})
    finished_at = deps.now()

    if error is not None:
        await asyncio.to_thread(
            _mark_failed_sync,
            deps.db_path,
            claim.digest_id,
            f"publish failed: {error}",
            posted,
            deps.now,
        )
        missing = [m.topic_key for m in built.rendered.messages if m.topic_key not in posted]
        if _will_retry(claim, bool(posted)):
            tail = "I'll try again shortly; if that fails, an admin can run /newsbot run-now."
        else:
            tail = "I won't retry on my own; once that's fixed, an admin can run /newsbot run-now."
        await _safe_notify(
            deps,
            guild_id,
            "newsbot: today's digest didn't fully post "
            f"({esc(plain_line(str(error), 200))}). Posted: {_names(built, list(posted))}; "
            f"didn't post: {_names(built, missing)}. "
            f"Check that I can still send messages and embeds in those channels. {tail}",
        )
        return GuildDigestOutcome(
            guild_id,
            "failed",
            run_date,
            posted,
            skipped_games,
            [*built.notes, str(error)],
            built.rendered,
            sent,
        )

    notes = list(built.notes)
    for key, reason in skipped_games.items():
        notes.append(f"{key}: skipped ({plain_line(reason, 200)})")
    partial = built.degraded or bool(skipped_games)
    status: Literal["ok", "partial"] = "partial" if partial else "ok"
    await asyncio.to_thread(
        _save_sync,
        deps.db_path,
        claim.digest_id,
        status,
        posted,
        "; ".join(notes) or None,
        (claim.window_start, claim.window_end),
        deps.now,
    )

    if skipped_games:
        await _safe_notify(
            deps,
            guild_id,
            f"newsbot: I couldn't post {_names(built, list(skipped_games))} in today's digest. "
            "The channel may have been deleted, or I may be missing permission to send "
            "messages and embeds there. Use /newsbot follow to point the game at a channel I "
            "can use. The rest of the digest posted.",
        )
    await _send_report(
        deps,
        guild,
        built,
        status,
        run_date,
        kind,
        posted,
        skipped_games,
        notes,
        finished_at - started,
    )
    return GuildDigestOutcome(
        guild_id, status, run_date, posted, skipped_games, notes, built.rendered, sent
    )


async def _send_report(
    deps: GuildDigestDeps,
    guild: GuildSettings,
    built: _Built,
    status: Literal["ok", "partial"],
    run_date: date,
    kind: RunKind,
    posted: dict[str, int],
    skipped_games: dict[str, str],
    notes: list[str],
    duration,
) -> None:
    """The run report goes to this server's admin channel and nowhere else, if it has one.

    Runs after the digest is saved, so anything going wrong in here is logged and
    dropped; it can't change what already landed.
    """
    if deps.send_report is None or guild.admin_channel_id is None or not deps.cfg.run_report:
        return
    try:
        text = render_guild_run_report(
            status=status,
            run_date=run_date,
            run_kind=kind.value,
            games=built.games,
            counts=built.counts,
            channels=built.channels,
            posted_by_game=posted,
            skipped=skipped_games,
            duration=duration,
            notes=notes,
            guild_id=guild.guild_id,
        )
        await deps.send_report(guild.guild_id, guild.admin_channel_id, text)
    except Exception:
        logger.exception("failed to build or send a guild run report")


# --- The every-minute tick ---


def _candidates_sync(db_path: str):
    with closing(connect(db_path)) as conn:
        return repo.due_candidates(conn)


# A run that crashes (as opposed to failing cleanly, which leaves a row with its own
# retry rules) can't be trusted to leave any evidence, least of all before its claim,
# so the tick keeps its own record: `app_state`, one small JSON value per server.
# Why there: it's bookkeeping about the scheduler, not a setting (no `guilds` column,
# no migration), it survives the every-minute loop and a restart, and it needs no
# retention policy (it's cleared the next time the server's run gets anywhere).
# `guild_notices` is a capped log for admins to read, not something to query for dedupe.
def _crash_key(guild_id: int) -> str:
    return repo.guild_crash_key(guild_id)


@dataclass(frozen=True)
class _CrashRecord:
    run_date: date
    attempts: int
    last_at: datetime


def _crash_get_sync(db_path: str, guild_id: int) -> _CrashRecord | None:
    with closing(connect(db_path)) as conn:
        raw = repo.app_state_get(conn, _crash_key(guild_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return _CrashRecord(
            date.fromisoformat(data["run_date"]),
            int(data["attempts"]),
            datetime.fromisoformat(data["last_at"]),
        )
    except ValueError, KeyError, TypeError:
        return None  # garbage is as good as no record


def _crash_set_sync(db_path: str, guild_id: int, record: _CrashRecord) -> None:
    value = json.dumps(
        {
            "run_date": record.run_date.isoformat(),
            "attempts": record.attempts,
            "last_at": record.last_at.isoformat(),
        }
    )
    with closing(connect(db_path)) as conn:
        repo.app_state_set(conn, _crash_key(guild_id), value)


def _crash_clear_sync(db_path: str, guild_id: int) -> None:
    with closing(connect(db_path)) as conn:
        repo.app_state_delete(conn, _crash_key(guild_id))


def _crash_backoff(attempts: int) -> timedelta:
    return min(RETRY_AFTER * 2 ** max(attempts - 1, 0), _CRASH_BACKOFF_CAP)


async def run_due_guilds(deps: GuildDigestDeps) -> list[GuildDigestOutcome]:
    """One tick: run the digest of every server that's due, one after another.

    Waits `deps.pace_s` after a server that actually sent something, and not after one
    that had nothing to say. One server's exception is logged, reported to that
    server (once per digest day, with a growing backoff before it's tried again),
    and forgotten; the next server goes ahead on schedule. A server that runs past
    `deps.guild_timeout_s` is left `pending` for the lease to bring back, and the tick
    moves on. (An outside cancellation isn't an `Exception`, and propagates.)
    """
    now = deps.now()
    candidates = await asyncio.to_thread(_candidates_sync, deps.db_path)
    outcomes: list[GuildDigestOutcome] = []
    paced = False
    for due in due_guilds(candidates, now, deps.running):
        if due.reason == "abandon":
            outcomes.append(await _abandon(deps, due))
            continue
        crash = await asyncio.to_thread(_crash_get_sync, deps.db_path, due.guild_id)
        if crash is not None and crash.run_date == due.run_date:
            if deps.now() - crash.last_at <= _crash_backoff(crash.attempts):
                continue
        if paced:
            await deps.sleep(deps.pace_s)
        kind = RunKind.CATCH_UP if due.catch_up else RunKind.SCHEDULED
        try:
            async with asyncio.timeout(deps.guild_timeout_s) as limit:
                outcome = await run_guild_digest(deps, due.guild_id, kind=kind, due=due)
        except Exception:
            if limit.expired():
                logger.warning(
                    "guild %s's digest timed out after %ss; left pending to resume",
                    due.guild_id,
                    deps.guild_timeout_s,
                    extra={"guild_id": due.guild_id},
                )
                outcome = GuildDigestOutcome(
                    due.guild_id, "failed", due.run_date, notes=["timed out; left pending"]
                )
            else:
                logger.exception("guild digest crashed", extra={"guild_id": due.guild_id})
                outcome = await _record_crash(deps, due, crash)
        else:
            if crash is not None:
                await asyncio.to_thread(_crash_clear_sync, deps.db_path, due.guild_id)
        outcomes.append(outcome)
        paced = outcome.sent > 0
    return outcomes


def _abandon_sync(db_path: str, due: DueGuild, now: Callable[[], datetime]) -> bool:
    with closing(connect(db_path)) as conn:
        return repo.abandon_guild_digest(
            conn, due.guild_id, due.run_date, max_attempts=MAX_ATTEMPTS, now=now
        )


async def _abandon(deps: GuildDigestDeps, due: DueGuild) -> GuildDigestOutcome:
    """Give up on a `pending` row that's used all its attempts, and tell its server once.

    Each resume is an attempt, so a digest that times out or dies mid-send every time
    reaches here after `MAX_ATTEMPTS` instead of being resumed until midnight. The row
    becomes `failed` (what posted stays recorded), which the due check treats as done;
    only an admin's run-now will touch it again. The notice carries no mentions, and
    only the tick that actually flips the row sends it, so it goes out once.
    """
    try:
        gave_up = await asyncio.to_thread(_abandon_sync, deps.db_path, due, deps.now)
    except Exception:
        logger.exception("couldn't abandon a guild's digest", extra={"guild_id": due.guild_id})
        return GuildDigestOutcome(due.guild_id, "failed", due.run_date)
    if gave_up:
        logger.warning(
            "guild %s's digest used all %d attempts; marked failed",
            due.guild_id,
            MAX_ATTEMPTS,
            extra={"guild_id": due.guild_id},
        )
        await _safe_notify(
            deps,
            due.guild_id,
            f"newsbot: today's digest was interrupted {MAX_ATTEMPTS} times (it ran too long "
            "or I restarted partway through), so I've stopped retrying it. Whatever already "
            "posted is still there. Once things look healthy, an admin can run "
            "/newsbot run-now; /newsbot status has the details.",
        )
    return GuildDigestOutcome(
        due.guild_id, "failed", due.run_date, notes=["gave up after repeated interruptions"]
    )


async def _record_crash(
    deps: GuildDigestDeps, due: DueGuild, previous: _CrashRecord | None
) -> GuildDigestOutcome:
    """Note a crashed run for the backoff, and tell the server about it the first time only."""
    first = previous is None or previous.run_date != due.run_date
    record = _CrashRecord(due.run_date, 1 if first else previous.attempts + 1, deps.now())
    try:
        await asyncio.to_thread(_crash_set_sync, deps.db_path, due.guild_id, record)
    except Exception:
        logger.exception("couldn't record a crashed guild digest", extra={"guild_id": due.guild_id})
    if first:
        await _safe_notify(
            deps,
            due.guild_id,
            "newsbot: something went wrong posting today's digest. This is the only message "
            "I'll send about it today. If the digest doesn't show up, an admin can run "
            "/newsbot run-now; /newsbot status has the details.",
        )
    return GuildDigestOutcome(due.guild_id, "failed", due.run_date)
