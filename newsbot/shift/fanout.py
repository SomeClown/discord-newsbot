"""Find SHiFT codes once, then tell every server that asked.

v2 had one server, so "find a code" and "announce a code" were one step and
nobody had to think about the gap between them. v3 has as many servers as
will have us, and the gap is the whole design. Detection is global and happens
once per collection pass: the regex, trust, age limit, roundup handling and
the silent first sweep all behave exactly as design.md §12 decided, against
the one `alerted_codes` table. Then each code released in *this pass* (and only
this pass, which is why enabling alerts never produces a backlog) is queued
for every server eligible at that instant, in the same transaction as the
release. Delivery is a separate step that works through the queue, server by
server, each with its own channel, its own ping choice and its own daily ping
budget in its own local day. Think of it as one town crier who reads the news
once, writes down every house that wants it, and then walks the list.

The queue exists because my first draft delivered inside the collection hook's
120 second timeout and recorded nobody until their turn came. A few hundred
servers at a pace of 0.2 s each is a long walk, the timeout is not patient,
and a server the crier never reached had no record of ever being owed the
code. Now the list is written before the walk starts, the walk stops starting
new servers at 100 of the hook's 120 seconds (so the timeout never cuts a send
in half), and whoever's left stays queued for the next pass (or startup) to
pick up where it stopped. At a server's turn its *current* settings decide what happens: alerts
switched off or the channel gone means `skipped`; a new ping choice is the
one used. Codes are not stale-checked beyond that; a queued code for a server
that's still listening is news to it.

Two rules carry over from v2 without apology: a code's claim (queued to
`pending`) is written *before* anything is sent (a crash loses an alert, never
doubles a ping; a `pending` row that's gone stale is marked `failed`, never
re-sent), and a failure in one server's channel is that server's problem. It
goes to that server's own admin channel and never stops the next server in
line. The owner hears about the detector breaking (`pipeline/collect.py` does
that) and about a roundup too big for the cap, not about someone deleting
their SHiFT channel.

Posting is injected (`FanoutDeps.poster_for`), and so is editing an alert after the
fact (`FanoutDeps.editor_for`, run by `shift/edits.py` after the walk), so all of this
is testable without a Discord in sight.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from newsbot.bot.format import (
    FOLLOWUP_MAX_CODES,
    RenderedAlert,
    details_reserve,
    fit_followup_codes,
    render_code_alerts,
    render_followup_alert,
    render_roundup_alerts,
)
from newsbot.collectors.base import CollectorResult, RawItem
from newsbot.config import AppConfig
from newsbot.pipeline.normalize import canonicalize, canonicalize_items
from newsbot.pipeline.run import local_run_date
from newsbot.shift.decide import (
    MAX_ROUNDUP_CODES,
    CodeCandidate,
    CodeSighting,
    aggregate,
    plan_alerts,
    seeding_healthy,
    sightings_from_items,
)
from newsbot.shift.details import DetailObservation, expired_by_expiry, silenced_by_expiry
from newsbot.shift.edits import edit_changed_alerts
from newsbot.shift.sweep import (
    _ROUNDUP_CAP_ALERT,
    CodeAlertEditor,
    CodeAlertPoster,
    post_alert_with_retry,
)
from newsbot.store.db import connect
from newsbot.store.models import (
    DETAILS_STAMP_FLOOR,
    AlertMessageRecord,
    CodeDetailsRow,
    GuildSettings,
    QueuedCode,
    ShiftSettings,
)
from newsbot.store.repo import (
    ClaimedCodes,
    claim_guild_codes,
    claim_guild_followups,
    code_details_for,
    current_shift_delivery,
    fail_pending_guild_codes,
    fail_pending_guild_followups,
    fail_queued_guild_codes,
    fail_queued_guild_followups,
    get_alert_state,
    known_codes,
    mark_guild_codes_failed,
    mark_guild_codes_posted,
    mark_guild_followups_failed,
    mark_guild_followups_posted,
    mark_table_listed,
    queue_confirmed_followups,
    queued_followup_guild_ids,
    queued_guild_codes,
    queued_guild_followups,
    queued_guild_ids,
    record_code_sightings,
    record_released_codes,
    record_silent_codes,
    skip_queued_guild_codes,
    skip_queued_guild_followups,
    stored_detail_source_names,
    table_kinds_listed,
    table_silenced_codes,
    upsert_code_details,
)

logger = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]
# (guild_id, channel_id, ping choice, that guild's notifier) -> a poster for
# that one channel. The real one builds a `DiscordCodeAlertPoster`.
PosterFactory = Callable[[int, int, str, Notify], CodeAlertPoster]
NotifyGuild = Callable[[int, str], Awaitable[None]]
# (guild_id, channel_id) -> an editor for messages in that one channel (`DiscordCodeAlertEditor`).
EditorFactory = Callable[[int, int], CodeAlertEditor]

# A breath between servers so a hundred guilds don't become a hundred sends in
# the same instant; Discord's rate limiter is patient, but not infinitely.
_GUILD_PACE_S = 0.2

# The collection hook gets 120 s in all (`pipeline/collect.py`). The walk stops
# *starting* servers at 100 s and leaves the rest `queued` for the next pass; the
# hard stop is the backstop for a server whose sends were already under way.
_WALK_STOP_S = 100.0
_WALK_HARD_S = 115.0
# A walk that starts late still gets this long before the hard stop.
_WALK_FLOOR_S = 5.0

# A `pending` row older than this is a send that died, not one in flight (the
# hook's own timeout is 120 s). Startup doesn't need the wait: nothing is in flight.
_PENDING_STALE_S = 600.0

# A database too busy to list the queue is busy for every pass until it isn't, and the
# owner doesn't need to hear it every pass. At most one alert an hour, from memory (a
# restart forgives it, which is the right amount of grudge).
_BUSY_ALERT_EVERY = timedelta(hours=1)


@dataclass
class FanoutDeps:
    cfg: AppConfig
    db_path: str
    now: Callable[[], datetime]
    poster_for: PosterFactory
    notify_guild: NotifyGuild
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    pace_s: float = _GUILD_PACE_S
    # The bot-wide owner alert path (never a server's channel). None means
    # nobody is listening, which is how tests that don't care run.
    alert_owner: Notify | None = None
    clock: Callable[[], float] = time.monotonic
    # Walks (a pass's, the startup one) take turns on this: startup fails every
    # `pending` row, which is only safe when no pass has a send in flight.
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Release and print only: never claim, mark or spend a ping (and no
    # follow-ups, D14). The CLI's mode.
    preview: bool = False
    # When the owner was last told the database was too busy to walk the queue.
    busy_alerted_at: datetime | None = None
    # Room (UTF-16 units) every alert sets aside per code for a details line that may
    # arrive after it posts (`reserve_for_config`; design.md §16). Zero packs alerts the
    # old way, which is what tests that don't care about details get.
    details_reserve: int = 0
    # Builds the editor the edit pass uses (`shift/edits.py`). None means no edits at all:
    # the CLI's mode, and what tests that don't care about edits get.
    editor_for: EditorFactory | None = None


def reserve_for_config(cfg: AppConfig) -> int:
    """The per-code details reservation for `cfg`'s sources (`format.details_reserve`).

    This is the config's floor. Delivery widens it each turn with the "details via" names still
    sitting in `code_details` rows (`stored_detail_source_names`), which can be longer than
    anything the config says today after a source is renamed.

    Only a source that can supply details can put its name in a "details via" clause: an
    official source (Gearbox's own posts, read by the official reader) or a
    `shift_table`. And only one that detection can ever read for a code: a source under a
    game in `shift.games` (empty means every game, the v2 reading), or a shared source that
    isn't limited to other games. A press feed for a game with no SHiFT codes can't put its
    name in an alert, so it doesn't get to make every alert's slot bigger. Config caps every
    source name at 100 characters, so the longest of these is a bounded number and the
    worst-case line has a worst case.
    """
    shift_games = set(cfg.shift.games)
    sources = [
        *(
            s
            for game in cfg.catalog
            if not shift_games or game.key in shift_games
            for s in game.sources
        ),
        *(
            s
            for s in cfg.shared_sources
            if not shift_games or not getattr(s, "games", None) or shift_games.intersection(s.games)
        ),
    ]
    return details_reserve(
        [
            s.name
            for s in sources
            if getattr(s, "name", None) and (s.type == "shift_table" or s.trust == "official")
        ]
    )


@dataclass(frozen=True)
class GuildOutcome:
    """What one server's turn did, for logs and tests."""

    guild_id: int
    posted: int = 0
    roundup_posted: int = 0
    failed: int = 0
    skipped: int = 0
    pinged: bool = False
    cap_reached: bool = False
    # "Confirmed by a second source" follow-ups (D14): codes named in the one
    # pinged message this turn sent, if any.
    followups_posted: int = 0
    error: str | None = None
    # Why a server's alert went out without a ping (or lost it on the way); for logs.
    reasons: tuple[str, ...] = ()


def _mention_for(ping: str) -> str:
    """The text a ping starts a message with: `@everyone`, or `<@&id>` for a role."""
    return "@everyone" if ping == "everyone" else f"<@&{ping}>"


# How long to wait before each retry of a write that has to land after a message did (SQLite's
# own busy timeout has already waited five seconds by the time it gives up, so these are short).
_MARK_RETRY_DELAYS_S = (0.2, 0.5, 1.0)


class MarkLostError(Exception):
    """A message went out but the database stayed busy through every retry of recording it."""


async def _record(deps: FanoutDeps, write: Callable[..., None], *args: object) -> None:
    """Run a sync database write that follows a send, riding out a busy database.

    The send already happened (or failed for good), so the write is the only thing left to
    do, and giving up is expensive: the row stays `pending`, where nothing ever sends it
    again, and an admin is later told it "may or may not have posted". So a few short retries
    first. If they all fail this raises `MarkLostError`.
    """
    for delay in (*_MARK_RETRY_DELAYS_S, None):
        try:
            await asyncio.to_thread(write, *args)
            return
        except sqlite3.OperationalError as exc:
            if delay is None:
                raise MarkLostError(str(exc)) from exc
            logger.warning("SHiFT bookkeeping hit a busy database; retrying in %.1f s", delay)
            await deps.sleep(delay)


async def _post_batch(
    deps: FanoutDeps,
    guild_id: int,
    poster: CodeAlertPoster,
    rendered: list[RenderedAlert],
    notify: Notify,
    reasons: set[str] | None = None,
    record: Callable[[RenderedAlert, bool], AlertMessageRecord] | None = None,
) -> tuple[int, int]:
    """Post `rendered` in order to one server; return (posted, failed) code counts.

    `record` (normal alerts only; roundups never pass it) builds the
    `guild_alert_messages` row for a message that posted, given the alert and whether
    its ping was stripped on the way. It's written with the posted mark, so the edit
    pass can find the message later.

    Once a message fails for good, everything after it is marked failed
    without being sent: a batch reads in order ("(continued)" is literal), and
    message 3 without message 2 would confuse more than it helps.

    Each message's mark (posted or failed) is retried through a busy database. If one
    still can't be written, `MarkLostError` ends the batch on the spot: that message's
    codes and every later alert's stay `pending`, which is the safe side, because a
    `pending` row is never sent again (nothing is double-sent; the later alerts are
    simply not sent). Recovery fails them once they're stale and tells the server they
    "may or may not have posted", which is true of the one that did and a gentle fib
    about the rest.
    """
    posted = 0
    failed_codes: list[str] = []
    failed_from_here = False
    for alert in rendered:
        message_id: int | None = None
        stripped = False

        def _on_stripped() -> None:
            nonlocal stripped
            stripped = True
            if reasons is not None:
                reasons.add("ping_stripped_after_ambiguous_failure")

        if not failed_from_here:
            message_id, error = await post_alert_with_retry(
                poster, deps.sleep, alert, on_ping_stripped=_on_stripped
            )
            if error is not None:
                failed_from_here = True
                logger.error(
                    "guild code alert post failed",
                    extra={"guild_id": guild_id, "codes": alert.codes, "error": str(error)},
                )
        if failed_from_here:
            failed_codes.extend(alert.codes)
            await _record(deps, _mark_failed_sync, deps.db_path, guild_id, list(alert.codes))
            continue
        posted += len(alert.codes)
        message = record(alert, stripped) if record is not None else None
        await _record(
            deps, _mark_posted_sync, deps.db_path, guild_id, list(alert.codes), message_id, message
        )
    if failed_codes:
        await _safe_notify(
            notify,
            "newsbot: a SHiFT code alert couldn't be posted in your SHiFT channel; "
            "codes never posted: " + ", ".join(failed_codes),
        )
    return posted, len(failed_codes)


def _mark_failed_sync(db_path: str, guild_id: int, codes: list[str]) -> None:
    with closing(connect(db_path)) as conn:
        mark_guild_codes_failed(conn, guild_id, codes)


def _mark_posted_sync(
    db_path: str,
    guild_id: int,
    codes: list[str],
    message_id: int | None,
    message: AlertMessageRecord | None = None,
) -> None:
    with closing(connect(db_path)) as conn:
        mark_guild_codes_posted(conn, guild_id, codes, message_id=message_id, message=message)


async def _safe_notify(notify: Notify, text: str) -> None:
    # Telling a server about a problem must not become the next problem.
    try:
        await notify(text)
    except Exception:
        logger.exception("couldn't send a guild notice")


async def _claim(
    deps: FanoutDeps,
    guild_id: int,
    codes: list[str],
    *,
    pinged: bool,
    ping_codes: set[str] | None,
    timezone: str,
    from_roundup: bool,
    followup_ok: bool = False,
) -> ClaimedCodes:
    """Claim queued -> pending; returns what was really claimed (maybe nothing).

    The clock is read here, at claim time: a walk that's been going a while
    must not claim against the moment it started (or spend a ping on the
    wrong local day). A busy or locked database claims nothing and leaves
    the codes `queued` for the next pass; it isn't this server's fault.
    """

    def _sync() -> ClaimedCodes:
        now = deps.now()
        with closing(connect(deps.db_path)) as conn:
            return claim_guild_codes(
                conn,
                guild_id,
                codes,
                pinged=pinged,
                local_day=local_run_date(now, timezone).isoformat(),
                now=lambda: now,
                max_pings=None if from_roundup else deps.cfg.shift.max_pings_per_day,
                from_roundup=from_roundup,
                ping_codes=ping_codes,
                followup_ok=followup_ok,
            )

    try:
        return await asyncio.to_thread(_sync)
    except sqlite3.OperationalError:
        logger.warning(
            "SHiFT claim hit a busy database; codes stay queued",
            extra={"guild_id": guild_id, "codes": len(codes)},
        )
        return ClaimedCodes([], False)


def _candidate(queued: QueuedCode) -> CodeCandidate:
    return CodeCandidate(
        queued.code,
        golden=queued.golden,
        source_name=queued.source_name,
        item_url=queued.item_url,
        fresh=True,
        trusted=queued.trusted,
        roundup=queued.from_roundup,
    )


@dataclass(frozen=True)
class _Turn:
    """What a server's turn read once, up front: the clock, the packing reserve, the dead codes."""

    read_at: datetime
    reserve: int
    expired: int


def _alert_record(
    alert: RenderedAlert,
    stripped: bool,
    channel_id: int,
    details: dict[str, CodeDetailsRow],
    *,
    posted_at: datetime,
    ping: str = "none",
) -> AlertMessageRecord:
    """What to remember about a posted alert so it can be edited: what went out, not what we meant.

    `rendered_at` is the message's version stamp: the largest `details_changed_at` among its
    codes' rows as they were read for rendering, or the epoch when none had details. It is
    compared against other `details_changed_at` values and never against a clock, so a
    release that took its own `now` a moment before this delivery read (and committed after)
    can't leave a change that looks older than a render that never saw it.

    A retry that stripped the ping posted a message without the ping prefix, so the
    stored header loses it too, and the stored ping choice is "none": an edit sends the
    choice back as the message's mentions (Discord rebuilds them from the edit), and an edit
    that put `@everyone` back into a message that never had it would be a new ping.
    `ping` is the server's choice; it is only kept for a message that actually carried it.
    """
    header = alert.header
    if stripped and alert.ping_prefix and header.startswith(alert.ping_prefix):
        header = header[len(alert.ping_prefix) :]
    pinged = alert.ping and not stripped and bool(alert.ping_prefix)
    return AlertMessageRecord(
        channel_id=channel_id,
        header=header,
        golden_prefix=alert.golden_prefix,
        posted_at=posted_at,
        ping=ping if pinged else "none",
        rendered_at=max(
            (
                row.details_changed_at
                for code in alert.codes
                if (row := details.get(code)) is not None and row.details_changed_at is not None
            ),
            default=DETAILS_STAMP_FLOOR,
        ),
    )


async def _deliver_one(deps: FanoutDeps, guild_id: int) -> GuildOutcome:
    """One server's turn. Raises freely; `deliver_queued_codes` catches it so nobody else waits."""

    def _load_sync() -> tuple[GuildSettings, ShiftSettings, list[QueuedCode], _Turn] | int:
        # Current settings, read at this server's turn: they (not the ones from
        # release time) decide whether to post, with which ping, under which cap.
        with closing(connect(deps.db_path)) as conn:
            read_at = deps.now()
            queued = queued_guild_codes(conn, guild_id)
            state = current_shift_delivery(conn, guild_id, deps.cfg.shift.games)
            if state is None:
                if not deps.preview:
                    skip_queued_guild_codes(conn, guild_id, [q.code for q in queued])
                return len(queued)
            guild, shift = state
            # Alerts switched off and back on since the release: the no-backlog
            # rule says a re-enabled server starts from codes found after that.
            stale = [
                q for q in queued if shift.enabled_at is None or shift.enabled_at > q.queued_at
            ]
            if stale and not deps.preview:
                skip_queued_guild_codes(conn, guild_id, [q.code for q in stale])
            queued = [q for q in queued if q not in stale]
            # A code can die between its release and this turn (a queued pass, a busy
            # database). Announcing a code whose known expiry has passed is worse than
            # silence, so it's skipped like a follow-up would be; the live ones go out.
            # The believability rule is release's, word for word (`silenced_by_expiry`,
            # fed by what release recorded in `official_seen_at`): a table's expiry is a
            # stranger's typing, so it can't silence a code an official source named, and
            # an official expiry always can. An earlier draft used "trusted" here instead,
            # which let a table silence a press-trusted code at release but not at
            # delivery. Roundups don't get the check: their details live in /shift codes.
            dead = [
                q.code
                for q in queued
                if not q.from_roundup
                and silenced_by_expiry(q.details, read_at, official_seen=q.official_seen)
            ]
            if dead:
                if not deps.preview:
                    skip_queued_guild_codes(conn, guild_id, dead)
                queued = [q for q in queued if q.code not in dead]
            # Packing leaves room for the worst "details via" line any name in the config, or
            # still stored on a row, can make; a renamed source mustn't outgrow an edit.
            # `deps.details_reserve` is the config-only floor, and zero means reserving is
            # off (tests, mostly), so the stored names only widen a floor that's on.
            reserve = deps.details_reserve
            stored_names = stored_detail_source_names(conn) if reserve else []
            if stored_names:
                reserve = max(reserve, details_reserve(stored_names))
            return guild, shift, queued, _Turn(read_at, reserve, 0 if deps.preview else len(dead))

    loaded = await asyncio.to_thread(_load_sync)
    if isinstance(loaded, int):
        return GuildOutcome(guild_id, skipped=0 if deps.preview else loaded)
    guild, shift, queued, turn = loaded
    read_at, reserve = turn.read_at, turn.reserve
    channel_id = shift.channel_id
    # The first can't happen; the second is a turn with nothing left to send (all stale, or
    # all expired by now), and it creates no poster.
    if channel_id is None or not queued:
        return GuildOutcome(guild_id, skipped=turn.expired)

    async def notify(text: str) -> None:
        await deps.notify_guild(guild_id, text)

    normal = [_candidate(q) for q in queued if not q.from_roundup]
    roundup = [_candidate(q) for q in queued if q.from_roundup]
    details = {q.code: q.details for q in queued if q.details is not None}

    def record(alert: RenderedAlert, stripped: bool) -> AlertMessageRecord:
        return _alert_record(
            alert, stripped, channel_id, details, posted_at=deps.now(), ping=shift.ping
        )

    # Two servers sending the same batch must not share nonces (Discord may
    # answer the second with the first's message); a retry to the same channel must.
    scope = f"{guild_id}|{channel_id}"
    poster = deps.poster_for(guild_id, channel_id, shift.ping, notify)
    begin_batch = getattr(poster, "begin_batch", None)
    if begin_batch is not None:
        begin_batch()

    posted = roundup_posted = failed = 0
    final_ping = cap_reached = False
    reasons: set[str] = set()

    if normal:
        trusted_codes = {c.code for c in normal if c.trusted}
        want_ping = shift.ping != "none" and bool(trusted_codes)
        if shift.ping == "none":
            reasons.add("ping_off")
        elif not trusted_codes:
            reasons.add("untrusted")
        mention = _mention_for(shift.ping)
        # Render before claiming, worst case first: a claim can only turn a ping
        # off, and a message that fits under the longer pinged header fits
        # under the shorter one. (A row stranded `pending` by a render that
        # blew up afterwards would sit there until it goes stale.)
        rendered = render_code_alerts(
            normal,
            ping=want_ping,
            ping_mention=mention,
            nonce_scope=scope,
            details=details,
            now=read_at,
            reserve=reserve,
        )
        if deps.preview:
            # Print and carry on: the roundup codes queued beside these get printed too.
            await _print_batch(poster, rendered)
            posted = sum(len(r.codes) for r in rendered)
            claim = ClaimedCodes([], False)
        else:
            claim = await _claim(
                deps,
                guild_id,
                [c.code for c in normal],
                pinged=want_ping,
                ping_codes=trusted_codes,
                timezone=guild.timezone,
                from_roundup=False,
                # D14: a batch with nothing trusted in it, to a server with pinging
                # on, is the one kind of post a second source can follow up. Not a
                # mixed batch: its untrusted codes rode the ping, or lost it to the cap.
                followup_ok=shift.ping != "none" and not trusted_codes,
            )
        if claim.codes:  # codes a concurrent pass took are its to send; ours are the rest
            sent = [c for c in normal if c.code in set(claim.codes)]
            final_ping = claim.pinged
            if want_ping and any(c.trusted for c in sent) and not final_ping:
                cap_reached = True
                reasons.add("cap_reached")
            if len(sent) != len(normal) or final_ping != want_ping:
                rendered = render_code_alerts(
                    sent,
                    ping=final_ping,
                    ping_mention=mention,
                    nonce_scope=scope,
                    details=details,
                    now=read_at,
                    reserve=reserve,
                )
            posted, failed = await _post_batch(
                deps, guild_id, poster, rendered, notify, reasons, record
            )
            if cap_reached and deps.cfg.shift.max_pings_per_day > 0:
                await _safe_notify(
                    notify,
                    "newsbot: SHiFT code alert daily ping cap reached; posted without a ping",
                )

    if roundup:
        reasons.add("roundup")
        # Never pinged, never counted against the cap; it claims with its own flag.
        rendered_roundup = render_roundup_alerts(
            roundup, nonce_scope=scope, details=details, now=read_at
        )
        if deps.preview:
            await _print_batch(poster, rendered_roundup)
            roundup_posted = sum(len(r.codes) for r in rendered_roundup)
            claim = ClaimedCodes([], False)
        else:
            claim = await _claim(
                deps,
                guild_id,
                [c.code for c in roundup],
                pinged=False,
                ping_codes=None,
                timezone=guild.timezone,
                from_roundup=True,
            )
        if claim.codes:
            if len(claim.codes) != len(roundup):
                rendered_roundup = render_roundup_alerts(
                    [c for c in roundup if c.code in set(claim.codes)],
                    nonce_scope=scope,
                    details=details,
                    now=read_at,
                )
            roundup_posted, roundup_failed = await _post_batch(
                deps, guild_id, poster, rendered_roundup, notify, reasons
            )
            failed += roundup_failed

    if getattr(poster, "missing_ping_permission", False):
        reasons.add("no_permission")
    return GuildOutcome(
        guild_id,
        posted=posted,
        roundup_posted=roundup_posted,
        failed=failed,
        skipped=turn.expired,
        pinged=final_ping,
        cap_reached=cap_reached,
        reasons=tuple(sorted(reasons)),
    )


async def _deliver_followups(deps: FanoutDeps, guild_id: int) -> GuildOutcome:
    """One server's "confirmed by a second source" follow-up (D14), through the usual machinery.

    Everything queued for this server goes out as one pinged message (one unit
    of its daily ping budget, claimed before the send, with the cap re-checked
    under the write lock); `post_alert_with_retry` gives it the same nonce
    reuse, the same ping kept on a 429 and the same ping stripped after an
    ambiguous failure as any other alert. The server's *current* settings
    decide: alerts off or ping off means `skipped`, and a spent budget means
    `skipped` too, quietly: an unpinged "confirmed" repost would be the
    same news without the only reason to post it. A failed follow-up isn't
    retried and isn't announced to the admin channel; the original alert
    already landed, and a missed nudge isn't worth a notice.
    """

    def _load_sync() -> (
        tuple[GuildSettings, int, str, list[str], dict[str, CodeDetailsRow], datetime] | None
    ):
        with closing(connect(deps.db_path)) as conn:
            queued = queued_guild_followups(conn, guild_id)
            if not queued:
                return None
            # A code can die between the confirmation and the send. A pinged "confirmed"
            # for a dead code is worse than none, so it's skipped, and spends no ping.
            details = code_details_for(conn, queued)
            now = deps.now()
            dead = [code for code in queued if expired_by_expiry(details.get(code), now)]
            if dead:
                skip_queued_guild_followups(conn, guild_id, dead)
                queued = [code for code in queued if code not in dead]
                if not queued:
                    return None
            state = current_shift_delivery(conn, guild_id, deps.cfg.shift.games)
            if state is None or state[1].ping == "none" or state[1].channel_id is None:
                skip_queued_guild_followups(conn, guild_id, queued)
                return None
            guild, shift = state
            # A follow-up is never edited, so it fits by its real size: as many of the
            # queued codes as the message holds go now, and the rest wait their turn.
            codes = fit_followup_codes(
                queued[:FOLLOWUP_MAX_CODES],
                ping_mention=_mention_for(shift.ping),
                details=details,
                now=now,
            )
            if not codes:
                # Not even one code fits, which takes a ping mention close to 2,000 units.
                # Retrying it every hour wouldn't change that, so it's skipped, loudly.
                logger.error("a SHiFT follow-up can't fit one code", extra={"guild_id": guild_id})
                skip_queued_guild_followups(conn, guild_id, queued)
                return None
            return guild, shift.channel_id, shift.ping, codes, details, now

    loaded = await asyncio.to_thread(_load_sync)
    if loaded is None:
        return GuildOutcome(guild_id)
    guild, channel_id, ping, codes, details, read_at = loaded

    async def notify(text: str) -> None:
        await deps.notify_guild(guild_id, text)

    # Its own nonce scope marker (`render_followup_alert` adds "followup|"), so
    # it can't be mistaken for the original alert's send.
    scope = f"{guild_id}|{channel_id}"
    # Render before claiming, for the same reason as the alerts: a claim that
    # a render error strands is a row stuck `pending`.
    rendered = render_followup_alert(
        codes, ping_mention=_mention_for(ping), nonce_scope=scope, details=details, now=read_at
    )

    def _claim_sync() -> list[str]:
        now = deps.now()
        with closing(connect(deps.db_path)) as conn:
            return claim_guild_followups(
                conn,
                guild_id,
                codes,
                local_day=local_run_date(now, guild.timezone).isoformat(),
                max_pings=deps.cfg.shift.max_pings_per_day,
                now=lambda: now,
            )

    try:
        claimed = await asyncio.to_thread(_claim_sync)
    except sqlite3.OperationalError:
        logger.warning(
            "SHiFT follow-up claim hit a busy database; follow-ups stay queued",
            extra={"guild_id": guild_id},
        )
        return GuildOutcome(guild_id)
    if not claimed:
        return GuildOutcome(guild_id)
    if claimed != codes:
        rendered = render_followup_alert(
            claimed,
            ping_mention=_mention_for(ping),
            nonce_scope=scope,
            details=details,
            now=read_at,
        )

    poster = deps.poster_for(guild_id, channel_id, ping, notify)
    begin_batch = getattr(poster, "begin_batch", None)
    if begin_batch is not None:
        begin_batch()
    message_id, error = await post_alert_with_retry(poster, deps.sleep, rendered)
    if error is not None:
        logger.error(
            "guild SHiFT follow-up post failed",
            extra={"guild_id": guild_id, "codes": claimed, "error": str(error)},
        )
        await _record(deps, _followup_mark_sync, deps.db_path, guild_id, claimed, None, False)
        return GuildOutcome(guild_id)
    await _record(deps, _followup_mark_sync, deps.db_path, guild_id, claimed, message_id, True)
    return GuildOutcome(guild_id, followups_posted=len(claimed))


def _followup_mark_sync(
    db_path: str, guild_id: int, codes: list[str], message_id: int | None, posted: bool
) -> None:
    with closing(connect(db_path)) as conn:
        if posted:
            mark_guild_followups_posted(conn, guild_id, codes, message_id=message_id)
        else:
            mark_guild_followups_failed(conn, guild_id, codes)


async def _print_batch(poster: CodeAlertPoster, rendered: list[RenderedAlert]) -> None:
    """Preview mode: hand each message to the (printing) poster, record nothing."""
    for alert in rendered:
        await poster.post(alert)


def _log_outcome(outcome: GuildOutcome) -> None:
    """One INFO line per server's turn: ids and counts only, never message text."""
    if outcome.error is not None or not (
        outcome.posted or outcome.roundup_posted or outcome.failed or outcome.followups_posted
    ):
        return
    logger.info(
        "SHiFT delivery for a guild",
        extra={
            "guild_id": outcome.guild_id,
            "codes": outcome.posted
            + outcome.roundup_posted
            + outcome.failed
            + outcome.followups_posted,
            "pinged": outcome.pinged,
            "unpinged_or_stripped_because": ",".join(outcome.reasons) or "none",
        },
    )


async def deliver_queued_codes(
    deps: FanoutDeps, *, startup: bool = False, deadline: float | None = None
) -> list[GuildOutcome]:
    """Work through the delivery queue: every server with `queued` codes, in guild id order.

    Runs at the end of every collection pass (so a walk cut short is finished
    by the next one) and can be called at startup (`startup=True`) to do the
    same after a crash. It starts with crash recovery: a `pending` row is a
    claim whose send never got confirmed, so it becomes `failed` and its
    server is told; at startup that is every `pending` row, mid-run only the
    stale ones. Nothing `pending` is ever sent again.

    `deadline` is a `deps.clock()` reading: once it passes, no new server is
    started and everyone who hasn't had a turn stays `queued` for the next
    pass. (A server already in progress finishes; that's the point of
    stopping early instead of letting the hook's timeout cut a send in half.)

    One walk at a time per `deps` (`delivery_lock`): startup's fail-everything
    recovery can't land in the middle of a pass's sends. Two *processes* or
    separate `deps` can still overlap, and the claim copes: it takes whatever
    is still `queued` and sends only that.
    """
    async with deps.delivery_lock:
        if not deps.preview:
            now = deps.now()
            stale_before = None if startup else now - timedelta(seconds=_PENDING_STALE_S)
            await recover_pending_guild_codes(
                deps.db_path, deps.notify_guild, claimed_before=stale_before
            )

        def _queued_sync() -> list[int]:
            with closing(connect(deps.db_path)) as conn:
                # Follow-ups (D14) ride the same walk: same order, pacing and deadline.
                return sorted({*queued_guild_ids(conn), *queued_followup_guild_ids(conn)})

        outcomes: list[GuildOutcome] = []
        guild_ids = await asyncio.to_thread(_queued_sync)
        for i, guild_id in enumerate(guild_ids):
            if i:
                await deps.sleep(deps.pace_s)
            if deadline is not None and deps.clock() >= deadline:
                logger.warning(
                    "SHiFT delivery out of time; the rest stay queued for the next pass",
                    extra={"servers_left": len(guild_ids) - i},
                )
                break
            try:
                outcome = await _deliver_one(deps, guild_id)
                if not deps.preview:
                    followup = await _deliver_followups(deps, guild_id)
                    outcome = replace(outcome, followups_posted=followup.followups_posted)
            except MarkLostError:
                # The message went out and the database never let us say so. What was
                # claimed stays `pending` (never re-sent); recovery fails it once it's stale.
                logger.error(
                    "SHiFT message sent but the database stayed busy; its codes stay pending "
                    "(not re-sent) and the rest of this server's batch is not sent",
                    extra={"guild_id": guild_id},
                )
                outcomes.append(GuildOutcome(guild_id, error="database busy while recording"))
                continue
            except sqlite3.OperationalError:
                # A busy database is the next pass's problem, not this server's.
                logger.warning(
                    "SHiFT delivery hit a busy database; codes stay queued",
                    extra={"guild_id": guild_id},
                )
                outcomes.append(GuildOutcome(guild_id, error="database busy"))
                continue
            except Exception as exc:
                # One server's broken channel (or a bug that only its settings
                # tickle) is its own business; the rest of the list still gets its turn.
                # What it hadn't claimed yet is failed, not left queued: a bug
                # doesn't fix itself by the next pass, and an hourly notice isn't a fix.
                logger.exception("SHiFT delivery failed for a guild", extra={"guild_id": guild_id})
                outcomes.append(GuildOutcome(guild_id, error=str(exc)))
                if not deps.preview:
                    await _fail_queued_safely(deps, guild_id)
                    await _safe_notify(
                        lambda text, g=guild_id: deps.notify_guild(g, text),
                        "newsbot: a SHiFT code alert couldn't be posted to your server this time.",
                    )
                continue
            _log_outcome(outcome)
            outcomes.append(outcome)
        return outcomes


async def _fail_queued_safely(deps: FanoutDeps, guild_id: int) -> None:
    try:
        await asyncio.to_thread(_fail_queued_sync, deps.db_path, guild_id)
    except Exception:
        # The handler that called this must not become the walk's next casualty.
        logger.exception("couldn't fail a guild's queued codes", extra={"guild_id": guild_id})


def _fail_queued_sync(db_path: str, guild_id: int) -> None:
    with closing(connect(db_path)) as conn:
        fail_queued_guild_codes(conn, guild_id)
        fail_queued_guild_followups(conn, guild_id)


async def _deliver_then_edit(deps: FanoutDeps, deadline: float) -> None:
    """One pass's walk: deliveries and follow-ups first, then the edits, all before `deadline`.

    Edits are the lowest priority on purpose: a new code waiting for its alert matters more
    than a reward line on an old one. They get whatever's left of the walk's budget, and the
    same hard stop covers both (`detect_and_fan_out` wraps this in one `wait_for`).
    """
    await deliver_queued_codes(deps, deadline=deadline)
    if deps.preview or deps.clock() >= deadline:
        return
    try:
        await edit_changed_alerts(deps, deadline=deadline)
    except Exception:
        # A bug in the proofreading is not worth the owner's phone: deliveries are done.
        logger.exception("SHiFT alert edits failed")


async def detect_and_fan_out(deps: FanoutDeps, items: list[RawItem], *, seeding_ok: bool) -> int:
    """Detect codes in `items` once, queue the new ones, then deliver the queue.

    Returns how many codes were released (queued for servers or roundup-queued)
    this pass. `items` is everything collected, before dedupe: a code edited
    into an already-seen thread still counts, and the code tables do their own
    dedupe. Delivery runs even when nothing new was found, because the queue
    may still hold codes from a pass that ran out of time. After delivery, within the
    same time budget, the edit pass brings already-posted alerts up to date with any
    details learned since (`shift/edits.py`).

    Delivery trouble (a timeout, a bug) stays here: the codes are released and
    queued, so reporting it as "detection failed" would be telling the owner
    the wrong thing. Only detection's own failures propagate to the hook's caller.
    """
    started = deps.clock()
    released = await _release(deps, items, seeding_ok=seeding_ok)
    if deps.preview:
        await deliver_queued_codes(deps)
        return released
    hard_stop = max(started + _WALK_HARD_S - deps.clock(), _WALK_FLOOR_S)
    try:
        await asyncio.wait_for(_deliver_then_edit(deps, started + _WALK_STOP_S), timeout=hard_stop)
    except TimeoutError:
        logger.warning(
            "SHiFT delivery timed out; unsent codes stay queued or fail on recovery, "
            "and unfinished edits stay due"
        )
    except sqlite3.OperationalError:
        logger.warning("SHiFT delivery hit a busy database; the queue waits for the next pass")
        now = deps.now()
        if deps.alert_owner is not None and (
            deps.busy_alerted_at is None or now - deps.busy_alerted_at >= _BUSY_ALERT_EVERY
        ):
            deps.busy_alerted_at = now
            await _safe_notify(
                deps.alert_owner,
                "newsbot: the database was too busy to deliver SHiFT codes during collection; "
                "they stay queued (I'll say this at most once an hour)",
            )
    except Exception:
        logger.exception("SHiFT delivery failed")
        if deps.alert_owner is not None:
            await _safe_notify(deps.alert_owner, "newsbot: SHiFT delivery failed during collection")
    return released


# More than this many fresh, new codes from one table source in one pass is a flood, not news
# (owner, 2026-10-09): a site that restructures, or a source that suddenly lists a season's
# worth, shouldn't turn into a wall of messages. They go out the way a roundup does.
TABLE_FLOOD_LIMIT = 5


def _dead_on_arrival(
    row: CodeDetailsRow | None, code: str, now: datetime, official_codes: set[str]
) -> bool:
    """Whether a not-yet-released code has already expired, and the expiry is believable.

    A table's expiry is a stranger's typing. If Gearbox itself named the code in this very
    pass, a typo in someone else's table doesn't get to silence Gearbox: only an expiry that
    came from an official source can, and only then does an official mention not help.
    Delivery asks the same question of a queued code (`silenced_by_expiry`, with what this
    pass recorded in `official_seen_at`), so release and delivery can't disagree.
    """
    return silenced_by_expiry(row, now, official_seen=code in official_codes)


def _table_only_codes(sightings: list[CodeSighting]) -> set[str]:
    """Codes whose every sighting this pass was a table row, and at least one carried a date.

    These are the codes a table's "Added" date can make stale all by itself: no other source
    said anything about their age (so nobody else's date is being overruled), and a dated row
    is how a typo'd date gets in (an undated row on a first listing is E1's business, not
    this). Judged the way `aggregate` judges a code: a normal sighting beats a roundup one.
    """
    by_code: dict[str, list[CodeSighting]] = {}
    for s in sightings:
        by_code.setdefault(s.code, []).append(s)
    only: set[str] = set()
    for code, group in by_code.items():
        effective = [s for s in group if not s.roundup] or group
        if all(s.details is not None and s.details.listed for s in effective) and any(
            s.published_at is not None for s in effective
        ):
            only.add(code)
    return only


def _flag_first_listings(
    sightings: list[CodeSighting], by_kind: set[tuple[str, str]]
) -> list[CodeSighting]:
    """Mark table rows from a table kind their source has never listed here before (E1).

    Per source *and* kind, so a first page that only came back with its main table doesn't
    make the cosmetic and permanent tables a flood the next hour, and a site adding a table
    later gets the same quiet welcome. A code listed in several tables counts as read before
    if any of them was. A row with no kind at all (not from the collector) is judged by the
    source alone.
    """
    with_kinds = {name for name, _ in by_kind}

    def read_before(name: str, kinds: tuple[str, ...]) -> bool:
        if not kinds:
            return name in with_kinds
        return any((name, kind) in by_kind for kind in kinds)

    return [
        replace(s, first_listing=True)
        if s.details is not None
        and s.details.listed
        and not read_before(s.source_name, s.details.listed_in)
        else s
        for s in sightings
    ]


def _flood_to_roundup(
    candidates: list[CodeCandidate],
    sightings: list[CodeSighting],
    known: set[str],
    table_sources: set[str],
) -> list[CodeCandidate]:
    """Turn a table source's flood of fresh new codes into roundup candidates.

    "Introduced by" a table source means every normal sighting of the code is that source's,
    so a code Reddit also saw keeps its own standing. The existing roundup path then does the
    rest: unpinged, the cap of 50, and no follow-ups (those only follow `followup_ok` posts,
    and roundup posts never get the stamp).
    """
    sources_of: dict[str, set[str]] = {}
    for sighting in sightings:
        if not sighting.roundup:
            sources_of.setdefault(sighting.code, set()).add(sighting.source_name)
    introduced: dict[str, list[str]] = {}
    for c in candidates:
        names = sources_of.get(c.code, set())
        if c.fresh and not c.roundup and c.code not in known and len(names) == 1:
            (name,) = names
            if name in table_sources:
                introduced.setdefault(name, []).append(c.code)
    flood = {
        code for codes in introduced.values() if len(codes) > TABLE_FLOOD_LIMIT for code in codes
    }
    if flood:
        logger.warning(
            "a table source introduced a flood of new codes", extra={"codes": len(flood)}
        )
    return [replace(c, roundup=True, trusted=False) if c.code in flood else c for c in candidates]


def _canonical_batch(items: list[RawItem]) -> list[RawItem]:
    """Canonicalize every URL, deduping by URL only for items that don't carry details.

    `canonicalize_items` treats "same canonical URL" as "same item", and a fragment doesn't
    survive canonicalization, so a code table's thirty rows (one page, thirty `RawItem`s)
    would all be the same item to it and twenty-nine would be shown the door. An item with
    `details` is its own row of its own table: it gets a canonical URL and nothing else,
    and keeps its place behind the deduped rest.
    """
    plain = [item for item in items if item.details is None]
    rows: list[RawItem] = []
    for item in items:
        if item.details is None:
            continue
        url = canonicalize(item.url)
        if url is not None:
            rows.append(replace(item, url=url))
    return canonicalize_items(plain) + rows


async def _release(deps: FanoutDeps, items: list[RawItem], *, seeding_ok: bool) -> int:
    now = deps.now()
    shift_cfg = deps.cfg.shift
    sightings = sightings_from_items(
        _canonical_batch(items),
        topics=deps.cfg.catalog,
        alert_topics=shift_cfg.games,
        max_codes_per_item=shift_cfg.max_codes_per_item,
        now=now,
    )

    def _mark_seeded_sync() -> None:
        with closing(connect(deps.db_path)) as conn:
            record_silent_codes(conn, [], now=lambda: now, mark_seeded=True)

    if not sightings:
        if seeding_ok:
            # A healthy pass that found nothing is still the first healthy pass;
            # it has to flip the seeded marker or the next real code gets
            # swallowed as "history" (A1).
            await asyncio.to_thread(_mark_seeded_sync)
        return 0

    ping_trust = tuple(shift_cfg.ping_trust)
    max_age = timedelta(hours=shift_cfg.max_item_age_hours)

    def _plan_and_record_sync() -> tuple[int, int]:
        with closing(connect(deps.db_path)) as conn:
            # Every sighting is remembered, known code or not: a later source
            # seeing a code we already posted is the whole point (D14). Written
            # before the release so a crash between the two loses nothing.
            record_code_sightings(
                conn,
                [(s.code, s.source_name, s.trust in ping_trust, s.roundup) for s in sightings],
                now=lambda: now,
            )
            # What the sightings said about their codes (a table row's reward, an official
            # post's expiry) is stored before anything is planned, so the plan can see it.
            # Known codes included: that is how a listing fills in what an old alert lacked.
            # A crash after this and before the release redoes both next pass, harmlessly.
            observations = [
                DetailObservation(s.code, s.details, s.source_name, s.published_at)
                for s in sightings
                if s.details is not None
            ]
            changed = upsert_code_details(conn, observations, now=now) if observations else []
            if changed:
                logger.info("SHiFT code details changed", extra={"codes": len(changed)})

            # A table's first listing, kind by kind, is a bookshelf and not news: its undated
            # rows can't be told from ones that have sat there for a year (E1).
            listed_kinds: dict[str, set[str]] = {}
            for s in sightings:
                if s.details is not None and s.details.listed:
                    listed_kinds.setdefault(s.source_name, set()).update(s.details.listed_in)
            table_sources = set(listed_kinds)
            by_kind = table_kinds_listed(conn, table_sources) if table_sources else set()
            candidates = aggregate(
                _flag_first_listings(sightings, by_kind),
                now=now,
                max_age=max_age,
                ping_trust=ping_trust,
            )
            known = known_codes(conn, [c.code for c in candidates])
            official_codes = {s.code for s in sightings if s.trust == "official"}

            # A fan site's typo doesn't get to silence Gearbox for good. A code recorded
            # `too_old` only because of a table's data (see below) is released after all if an
            # official source names it within 72 hours of the bot first seeing it. Until then
            # it is "known"; from here to the plan it is treated as new, so the usual rules
            # (fresh, trusted, dead on arrival, the flood guard, the ping) judge it as they
            # would any code. The release itself is an UPDATE, guarded, in the same
            # transaction as the queueing, so a second pass or a crash can't make it twice.
            revivable = table_silenced_codes(conn, sorted(known & official_codes), now=now)
            plan_known = known - revivable

            # A code that's already expired the first time we release it isn't news either.
            # Recorded as `too_old` like any stale code, which leaves `alerted_codes`' CHECK
            # alone; `/shift history` reads the expiry itself to say "expired".
            unknown_details = code_details_for(
                conn, [c.code for c in candidates if c.code not in plan_known]
            )
            table_only = _table_only_codes(sightings)
            table_silenced: set[str] = set()
            checked: list[CodeCandidate] = []
            for c in candidates:
                if c.code not in plan_known:
                    row = unknown_details.get(c.code)
                    if _dead_on_arrival(row, c.code, now, official_codes):
                        # Silenced by an expiry; a table's, unless an official source said so.
                        if row is not None and row.expires_source == "table":
                            table_silenced.add(c.code)
                        c = replace(c, fresh=False)
                    elif not c.fresh and c.code in table_only:
                        table_silenced.add(c.code)  # too old by a table's "Added" date alone
                checked.append(c)
            candidates = _flood_to_roundup(checked, sightings, plan_known, table_sources)

            state = get_alert_state(conn)
            # Pings are decided per server, so the global plan only gets to
            # say silent / to_post / roundup; its own ping verdict is unused.
            plan = plan_alerts(
                candidates,
                known=plan_known,
                seeded=state.seeded,
                seeding_ok=seeding_ok,
                pings_today=0,
                max_pings=1,
            )
            silent_rows = [
                (c.code, c.source_name, c.item_url, status, c.roundup) for c, status in plan.silent
            ]
            if silent_rows or plan.mark_seeded:
                record_silent_codes(
                    conn,
                    silent_rows,
                    now=lambda: now,
                    mark_seeded=plan.mark_seeded,
                    table_silenced=table_silenced,
                )
            released = record_released_codes(
                conn,
                [(c.code, c.source_name, c.item_url, False) for c in plan.to_post]
                + [(c.code, c.source_name, c.item_url, True) for c in plan.roundup_to_post],
                now=lambda: now,
                queue_for_games=shift_cfg.games,
                flags={c.code: (c.golden, c.trusted) for c in plan.to_post + plan.roundup_to_post},
                official=official_codes,
                revive=revivable,
            )
            # D14: any of these codes that a second independent source (or a
            # trusted one) has now confirmed, inside 24 hours of its first
            # sighting, queues its follow-ups. Idempotent, so every pass can ask.
            queue_confirmed_followups(conn, [c.code for c in candidates], now=lambda: now)
            # Last of all, so a crash anywhere above makes the next pass a first listing
            # again, which it can redo safely: whatever it recorded is known by then.
            for name in sorted(table_sources):
                # (A row that names no kind didn't come from the collector; "main" is the kind
                # `_flag_first_listings` will look for on it next time, which is any kind.)
                mark_table_listed(conn, name, now, sorted(listed_kinds[name]) or ["main"])
            overflow = sum(1 for _, status in plan.silent if status == "roundup")
            return len(released), overflow

    released, roundup_overflow = await asyncio.to_thread(_plan_and_record_sync)
    if roundup_overflow:
        logger.warning(
            "roundup codes recorded silently past the per-check cap",
            extra={"overflow": roundup_overflow},
        )
        if deps.alert_owner is not None:
            # One line per check, not per trimmed code (design.md §13, D3), and
            # to the owner: this is about the detector's cap, not any server.
            await _safe_notify(
                deps.alert_owner,
                _ROUNDUP_CAP_ALERT.format(count=MAX_ROUNDUP_CODES, overflow=roundup_overflow),
            )
    return released


def make_shift_hook(
    deps: FanoutDeps,
) -> Callable[[list[RawItem], list[CollectorResult]], Awaitable[int]]:
    """The `ShiftHook` `pipeline/collect.py` calls with every collected item.

    Returns the number of newly released codes. Anything it raises is
    `collect.py`'s to catch (once, to the owner); per-server trouble never
    gets that far.
    """

    async def hook(items: list[RawItem], results: list[CollectorResult]) -> int:
        return await detect_and_fan_out(deps, items, seeding_ok=seeding_healthy(results))

    return hook


async def recover_pending_guild_codes(
    db_path: str, notify_guild: NotifyGuild, *, claimed_before: datetime | None = None
) -> int:
    """Flip `pending` server codes to `failed` and tell each affected server.

    Everything `pending` by default (startup); with `claimed_before`, only
    rows claimed earlier than that (a pass that must not step on a send still
    in flight). Returns the total code count; the owner is told that number
    and nothing more (which servers lost which codes is those servers' business).
    """

    def _sync() -> dict[int, list[str]]:
        with closing(connect(db_path)) as conn:
            # A follow-up cut off mid-send is failed too, quietly: the original
            # alert landed long ago, so there's nothing a server needs told.
            fail_pending_guild_followups(conn, claimed_before=claimed_before)
            return fail_pending_guild_codes(conn, claimed_before=claimed_before)

    by_guild = await asyncio.to_thread(_sync)
    for guild_id, codes in by_guild.items():
        await _safe_notify(
            lambda text, g=guild_id: notify_guild(g, text),
            (
                "newsbot restarted while a SHiFT code alert was in flight; it may or may not "
                "have posted. "
                if claimed_before is None
                else "a SHiFT code alert was cut off before newsbot could confirm it; it may "
                "or may not have posted. "
            )
            + "Codes not retried: "
            + ", ".join(codes),
        )
    return sum(len(codes) for codes in by_guild.values())


__all__ = [
    "FanoutDeps",
    "GuildOutcome",
    "deliver_queued_codes",
    "detect_and_fan_out",
    "make_shift_hook",
    "recover_pending_guild_codes",
]
