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

Posting is injected (`FanoutDeps.poster_for`), so all of this is testable
without a Discord in sight.
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
    render_code_alerts,
    render_followup_alert,
    render_roundup_alerts,
)
from newsbot.collectors.base import CollectorResult, RawItem
from newsbot.config import AppConfig
from newsbot.pipeline.normalize import canonicalize_items
from newsbot.pipeline.run import local_run_date
from newsbot.shift.decide import (
    MAX_ROUNDUP_CODES,
    CodeCandidate,
    aggregate,
    plan_alerts,
    seeding_healthy,
    sightings_from_items,
)
from newsbot.shift.sweep import _ROUNDUP_CAP_ALERT, CodeAlertPoster, post_alert_with_retry
from newsbot.store.db import connect
from newsbot.store.models import GuildSettings, QueuedCode, ShiftSettings
from newsbot.store.repo import (
    ClaimedCodes,
    claim_guild_codes,
    claim_guild_followups,
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
)

logger = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]
# (guild_id, channel_id, ping choice, that guild's notifier) -> a poster for
# that one channel. The real one builds a `DiscordCodeAlertPoster`.
PosterFactory = Callable[[int, int, str, Notify], CodeAlertPoster]
NotifyGuild = Callable[[int, str], Awaitable[None]]

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


async def _post_batch(
    deps: FanoutDeps,
    guild_id: int,
    poster: CodeAlertPoster,
    rendered: list[RenderedAlert],
    notify: Notify,
    reasons: set[str] | None = None,
) -> tuple[int, int]:
    """Post `rendered` in order to one server; return (posted, failed) code counts.

    Once a message fails for good, everything after it is marked failed
    without being sent: a batch reads in order ("(continued)" is literal), and
    message 3 without message 2 would confuse more than it helps.
    """
    posted = 0
    failed_codes: list[str] = []
    failed_from_here = False
    for alert in rendered:
        message_id: int | None = None
        if not failed_from_here:
            message_id, error = await post_alert_with_retry(
                poster,
                deps.sleep,
                alert,
                on_ping_stripped=lambda: (
                    reasons.add("ping_stripped_after_ambiguous_failure")
                    if reasons is not None
                    else None
                ),
            )
            if error is not None:
                failed_from_here = True
                logger.error(
                    "guild code alert post failed",
                    extra={"guild_id": guild_id, "codes": alert.codes, "error": str(error)},
                )
        if failed_from_here:
            failed_codes.extend(alert.codes)
            await asyncio.to_thread(_mark_failed_sync, deps.db_path, guild_id, list(alert.codes))
            continue
        posted += len(alert.codes)
        await asyncio.to_thread(
            _mark_posted_sync, deps.db_path, guild_id, list(alert.codes), message_id
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
    db_path: str, guild_id: int, codes: list[str], message_id: int | None
) -> None:
    with closing(connect(db_path)) as conn:
        mark_guild_codes_posted(conn, guild_id, codes, message_id=message_id)


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


async def _deliver_one(deps: FanoutDeps, guild_id: int) -> GuildOutcome:
    """One server's turn. Raises freely; `deliver_queued_codes` catches it so nobody else waits."""

    def _load_sync() -> tuple[GuildSettings, ShiftSettings, list[QueuedCode]] | int:
        # Current settings, read at this server's turn: they (not the ones from
        # release time) decide whether to post, with which ping, under which cap.
        with closing(connect(deps.db_path)) as conn:
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
            return guild, shift, [q for q in queued if q not in stale]

    loaded = await asyncio.to_thread(_load_sync)
    if isinstance(loaded, int):
        return GuildOutcome(guild_id, skipped=0 if deps.preview else loaded)
    guild, shift, queued = loaded
    channel_id = shift.channel_id
    if channel_id is None or not queued:  # the first can't happen; the second is a stale-only turn
        return GuildOutcome(guild_id, skipped=0)

    async def notify(text: str) -> None:
        await deps.notify_guild(guild_id, text)

    normal = [_candidate(q) for q in queued if not q.from_roundup]
    roundup = [_candidate(q) for q in queued if q.from_roundup]

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
            normal, ping=want_ping, ping_mention=mention, nonce_scope=scope
        )
        if deps.preview:
            await _print_batch(poster, rendered)
            return GuildOutcome(guild_id, posted=sum(len(r.codes) for r in rendered))
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
                    sent, ping=final_ping, ping_mention=mention, nonce_scope=scope
                )
            posted, failed = await _post_batch(deps, guild_id, poster, rendered, notify, reasons)
            if cap_reached and deps.cfg.shift.max_pings_per_day > 0:
                await _safe_notify(
                    notify,
                    "newsbot: SHiFT code alert daily ping cap reached; posted without a ping",
                )

    if roundup:
        reasons.add("roundup")
        # Never pinged, never counted against the cap; it claims with its own flag.
        rendered_roundup = render_roundup_alerts(roundup, nonce_scope=scope)
        if deps.preview:
            await _print_batch(poster, rendered_roundup)
            return GuildOutcome(
                guild_id, roundup_posted=sum(len(r.codes) for r in rendered_roundup)
            )
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
                    [c for c in roundup if c.code in set(claim.codes)], nonce_scope=scope
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

    def _load_sync() -> tuple[GuildSettings, int, str, list[str]] | None:
        with closing(connect(deps.db_path)) as conn:
            queued = queued_guild_followups(conn, guild_id)
            if not queued:
                return None
            state = current_shift_delivery(conn, guild_id, deps.cfg.shift.games)
            if state is None or state[1].ping == "none" or state[1].channel_id is None:
                skip_queued_guild_followups(conn, guild_id, queued)
                return None
            guild, shift = state
            return guild, shift.channel_id, shift.ping, queued[:FOLLOWUP_MAX_CODES]

    loaded = await asyncio.to_thread(_load_sync)
    if loaded is None:
        return GuildOutcome(guild_id)
    guild, channel_id, ping, codes = loaded

    async def notify(text: str) -> None:
        await deps.notify_guild(guild_id, text)

    # Its own nonce scope marker (`render_followup_alert` adds "followup|"), so
    # it can't be mistaken for the original alert's send.
    scope = f"{guild_id}|{channel_id}"
    # Render before claiming, for the same reason as the alerts: a claim that
    # a render error strands is a row stuck `pending`.
    rendered = render_followup_alert(codes, ping_mention=_mention_for(ping), nonce_scope=scope)

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
            claimed, ping_mention=_mention_for(ping), nonce_scope=scope
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
        await asyncio.to_thread(_followup_mark_sync, deps.db_path, guild_id, claimed, None, False)
        return GuildOutcome(guild_id)
    await asyncio.to_thread(_followup_mark_sync, deps.db_path, guild_id, claimed, message_id, True)
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


async def detect_and_fan_out(deps: FanoutDeps, items: list[RawItem], *, seeding_ok: bool) -> int:
    """Detect codes in `items` once, queue the new ones, then deliver the queue.

    Returns how many codes were released (queued for servers or roundup-queued)
    this pass. `items` is everything collected, before dedupe: a code edited
    into an already-seen thread still counts, and the code tables do their own
    dedupe. Delivery runs even when nothing new was found, because the queue
    may still hold codes from a pass that ran out of time.

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
        await asyncio.wait_for(
            deliver_queued_codes(deps, deadline=started + _WALK_STOP_S), timeout=hard_stop
        )
    except TimeoutError:
        logger.warning("SHiFT delivery timed out; unsent codes stay queued or fail on recovery")
    except Exception:
        logger.exception("SHiFT delivery failed")
        if deps.alert_owner is not None:
            await _safe_notify(deps.alert_owner, "newsbot: SHiFT delivery failed during collection")
    return released


async def _release(deps: FanoutDeps, items: list[RawItem], *, seeding_ok: bool) -> int:
    now = deps.now()
    shift_cfg = deps.cfg.shift
    sightings = sightings_from_items(
        canonicalize_items(items),
        topics=deps.cfg.catalog,
        alert_topics=shift_cfg.games,
        max_codes_per_item=shift_cfg.max_codes_per_item,
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
    candidates = aggregate(
        sightings,
        now=now,
        max_age=timedelta(hours=shift_cfg.max_item_age_hours),
        ping_trust=ping_trust,
    )

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
            known = known_codes(conn, [c.code for c in candidates])
            state = get_alert_state(conn)
            # Pings are decided per server, so the global plan only gets to
            # say silent / to_post / roundup; its own ping verdict is unused.
            plan = plan_alerts(
                candidates,
                known=known,
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
                    conn, silent_rows, now=lambda: now, mark_seeded=plan.mark_seeded
                )
            released = record_released_codes(
                conn,
                [(c.code, c.source_name, c.item_url, False) for c in plan.to_post]
                + [(c.code, c.source_name, c.item_url, True) for c in plan.roundup_to_post],
                now=lambda: now,
                queue_for_games=shift_cfg.games,
                flags={c.code: (c.golden, c.trusted) for c in plan.to_post + plan.roundup_to_post},
            )
            # D14: any of these codes that a second independent source (or a
            # trusted one) has now confirmed, inside 24 hours of its first
            # sighting, queues its follow-ups. Idempotent, so every pass can ask.
            queue_confirmed_followups(conn, [c.code for c in candidates], now=lambda: now)
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
