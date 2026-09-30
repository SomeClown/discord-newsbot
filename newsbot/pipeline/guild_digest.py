"""Post one server's digest, and find the servers that are owed one.

`run_daily` in `run.py` is the v2 version of this: one server, one claim, one
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

A `pending` row is also a lease (the rules are in `guilds/schedule.py`). The
publisher refreshes it as each game lands and on a `HEARTBEAT_S` heartbeat, so a
second process only resumes a digest once the first has been quiet for
`LEASE_STALE_AFTER`. A graceful cancel (a deploy) deliberately leaves the row
`pending` for the same reason: it's a pause, not a failure.

The bot's scheduler isn't wired to any of this yet (that's the cutover, plan
task 13), so for now the only thing calling it is the test suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal
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
    local_due_instant,
)
from newsbot.pipeline.publisher import Publisher
from newsbot.pipeline.run import RunKind, _publish_with_retry, local_run_date
from newsbot.pipeline.summarize import _FALLBACK_NOTE, StoryDraft
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import GuildClaim, GuildDigestRow, GuildGame, GuildSettings
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

# A breath between servers so a few hundred digests due at 09:00 don't become a
# few hundred bursts in the same second. discord.py handles the per-route 429s;
# this is just manners.
_GUILD_PACE_S = 1.0
# The most one server's digest may take inside the tick before the tick gives up
# on it and moves on (the row stays `pending`; the lease brings it back). Well
# over a real digest (a few seconds of sends, plus at most 14 s of retry backoff)
# and well under `schedule.LEASE_STALE_AFTER`, so a timed-out guild is never
# resumed while its old publisher could still be writing.
GUILD_TIMEOUT_S = 300.0
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


# (game key, the digest's due instant) -> that game's summary for the day, or None
# if there isn't one. Task 7 can wrap the stored lookup with an inline "make one now".
SummaryLookup = Callable[[str, datetime], Awaitable[GameSummary | None]]


@dataclass
class GuildDigestDeps:
    cfg: AppConfig
    db_path: str
    now: Callable[[], datetime]
    publisher_for: PublisherFactory
    notify_guild: NotifyGuild
    # None reads whatever `game_summaries` already holds.
    summary_for: SummaryLookup | None = None
    send_report: ReportSender | None = None
    sleep: Sleep = asyncio.sleep
    pace_s: float = _GUILD_PACE_S
    guild_timeout_s: float = GUILD_TIMEOUT_S
    heartbeat_s: float = HEARTBEAT_S
    # Guilds this process is publishing for right now, so a slow digest isn't
    # mistaken for a crashed one by the due check.
    running: set[int] = field(default_factory=set)
    _locks: dict[int, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        """One lock per server: the scheduler, run-now and preview take turns on it."""
        return self._locks.setdefault(guild_id, asyncio.Lock())


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
    return deps.lock_for(guild_id).locked()


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


async def _stored_summary(db_path: str, game_key: str, due_at: datetime) -> GameSummary | None:
    def _sync() -> GameSummary | None:
        with closing(connect(db_path)) as conn:
            row = repo.get_game_summary(conn, game_key, due_at)
            if row is None:
                return None
            views = repo.summary_stories(conn, row.id)
        stories = [
            StoryDraft(
                headline=v.headline,
                summary=v.summary,
                label=v.label,
                item_urls=list(v.urls),
                update_of_story_id=v.is_update_of,
            )
            for v in views
        ]
        return GameSummary(row.status, stories, list(row.coverage_notes), row.note)

    return await asyncio.to_thread(_sync)


async def _build(
    deps: GuildDigestDeps,
    guild: GuildSettings,
    followed: list[GuildGame],
    window: tuple[datetime, datetime],
    due_at: datetime,
    run_date: date,
) -> _Built:
    """Render one server's digest from stored data. Reads only; writes nothing."""
    channels = {g.game_key: g.channel_id for g in followed}
    games = [g for g in deps.cfg.catalog if g.key in channels]
    notes = [
        f"{key}: not in the catalog any more, skipped"
        for key in channels
        if key not in {g.key for g in games}
    ]

    def _items_sync():
        with closing(connect(deps.db_path)) as conn:
            return repo.items_for_window(conn, guild.guild_id, window[0], window[1])

    items_by_game = await asyncio.to_thread(_items_sync)

    stories_by_game: dict[str, list[StoryDraft]] = {}
    notes_by_game: dict[str, str | None] = {}
    coverage: list[str] = []
    degraded = bool(notes)
    if guild.tier == "comped":
        lookup = deps.summary_for or (lambda key, at: _stored_summary(deps.db_path, key, at))
        for game in games:
            try:
                summary = await lookup(game.key, due_at)
            except Exception:
                # A broken lookup costs this game its summary, not the server its digest.
                logger.exception("summary lookup failed for %s", game.key)
                summary = None
            if summary is not None and summary.status == "ok":
                stories_by_game[game.key] = summary.stories
                coverage.extend(n for n in summary.coverage_notes if n not in coverage)
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
    return _Built(rendered, counts, games, channels, notes, degraded)


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
    preview can't interleave with its real digest.
    """
    async with deps.lock_for(guild_id):
        loaded = await asyncio.to_thread(_load_sync, deps.db_path, guild_id)
        if loaded is None:
            return None
        guild, followed = loaded
        run_date, due_at, window_end = _when(deps, guild, RunKind.RUN_NOW, None)
        window = await asyncio.to_thread(
            _window_sync, deps.db_path, guild_id, run_date, window_end, deps.now(), late=False
        )
        built = await _build(deps, guild, followed, window, due_at, run_date)
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
    async with deps.lock_for(guild_id):
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
        built = await _build(deps, guild, followed, window, due_at, run_date)
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
    to_post = RenderedDigest(
        run_date,
        [m for m in built.rendered.messages if m.topic_key not in already],
        built.rendered.coverage_notes,
    )

    async def on_posted(game_key: str, message_id: int) -> None:
        await asyncio.to_thread(
            _record_posted_sync, deps.db_path, claim.digest_id, game_key, message_id, deps.now
        )

    # A forced re-run gets a fresh nonce scope: Discord would otherwise dedupe the
    # deliberate repost against the original and quietly hand back the old message.
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
    return f"guild_digest_crash:{guild_id}"


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
