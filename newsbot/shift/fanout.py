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
code. Now the list is written before the walk starts, a walk cut short leaves
the rest of the list queued, and the next pass (or startup) picks up where it
stopped. At a server's turn its *current* settings decide what happens: alerts
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
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsbot.bot.format import RenderedAlert, render_code_alerts, render_roundup_alerts
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
    ClaimLostError,
    claim_guild_codes,
    current_shift_delivery,
    fail_pending_guild_codes,
    fail_queued_guild_codes,
    get_alert_state,
    known_codes,
    mark_guild_codes_failed,
    mark_guild_codes_posted,
    queued_guild_codes,
    queued_guild_ids,
    record_released_codes,
    record_silent_codes,
    skip_queued_guild_codes,
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
    error: str | None = None


def _mention_for(ping: str) -> str:
    """The text a ping starts a message with: `@everyone`, or `<@&id>` for a role."""
    return "@everyone" if ping == "everyone" else f"<@&{ping}>"


async def _post_batch(
    deps: FanoutDeps,
    guild_id: int,
    poster: CodeAlertPoster,
    rendered: list[RenderedAlert],
    notify: Notify,
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
            message_id, error = await post_alert_with_retry(poster, deps.sleep, alert)
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
    local_day: str,
    now: datetime,
    from_roundup: bool,
) -> bool | None:
    """Claim queued -> pending; None if another pass beat us to these codes."""

    def _sync() -> bool | None:
        with closing(connect(deps.db_path)) as conn:
            return claim_guild_codes(
                conn,
                guild_id,
                codes,
                pinged=pinged,
                local_day=local_day,
                now=lambda: now,
                max_pings=None if from_roundup else deps.cfg.shift.max_pings_per_day,
                from_roundup=from_roundup,
            )

    try:
        return await asyncio.to_thread(_sync)
    except ClaimLostError:
        return None


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


async def _deliver_one(deps: FanoutDeps, guild_id: int, now: datetime) -> GuildOutcome:
    """One server's turn. Raises freely; `deliver_queued_codes` catches it so nobody else waits."""

    def _load_sync() -> tuple[GuildSettings, ShiftSettings, list[QueuedCode]] | int:
        # Current settings, read at this server's turn: they (not the ones from
        # release time) decide whether to post, with which ping, under which cap.
        with closing(connect(deps.db_path)) as conn:
            queued = queued_guild_codes(conn, guild_id)
            state = current_shift_delivery(conn, guild_id, deps.cfg.shift.games)
            if state is None:
                skip_queued_guild_codes(conn, guild_id, [q.code for q in queued])
                return len(queued)
            guild, shift = state
            # Alerts switched off and back on since the release: the no-backlog
            # rule says a re-enabled server starts from codes found after that.
            stale = [
                q for q in queued if shift.enabled_at is None or shift.enabled_at > q.queued_at
            ]
            if stale:
                skip_queued_guild_codes(conn, guild_id, [q.code for q in stale])
            return guild, shift, [q for q in queued if q not in stale]

    loaded = await asyncio.to_thread(_load_sync)
    if isinstance(loaded, int):
        return GuildOutcome(guild_id, skipped=loaded)
    guild, shift, queued = loaded
    channel_id = shift.channel_id
    if channel_id is None or not queued:  # the first can't happen; the second is a stale-only turn
        return GuildOutcome(guild_id, skipped=0)

    async def notify(text: str) -> None:
        await deps.notify_guild(guild_id, text)

    normal = [_candidate(q) for q in queued if not q.from_roundup]
    roundup = [_candidate(q) for q in queued if q.from_roundup]

    local_day = local_run_date(now, guild.timezone).isoformat()
    poster = deps.poster_for(guild_id, channel_id, shift.ping, notify)
    begin_batch = getattr(poster, "begin_batch", None)
    if begin_batch is not None:
        begin_batch()

    posted = roundup_posted = failed = 0
    final_ping = cap_reached = False

    if normal:
        want_ping = shift.ping != "none" and any(c.trusted for c in normal)
        mention = _mention_for(shift.ping)
        # Render before claiming, worst case first: a claim can only turn a ping
        # off, and a message that fits under the longer pinged header fits
        # under the shorter one. (A row stranded `pending` by a render that
        # blew up afterwards would sit there until it goes stale.)
        rendered = render_code_alerts(normal, ping=want_ping, ping_mention=mention)
        claimed = await _claim(
            deps,
            guild_id,
            [c.code for c in normal],
            pinged=want_ping,
            local_day=local_day,
            now=now,
            from_roundup=False,
        )
        if claimed is not None:  # None: a concurrent pass has these, and will send them
            final_ping = claimed
            if want_ping and not final_ping:
                cap_reached = True
                rendered = render_code_alerts(normal, ping=False)
            posted, failed = await _post_batch(deps, guild_id, poster, rendered, notify)
            if cap_reached and deps.cfg.shift.max_pings_per_day > 0:
                await _safe_notify(
                    notify,
                    "newsbot: SHiFT code alert daily ping cap reached; posted without a ping",
                )

    if roundup:
        # Never pinged, never counted against the cap; it claims with its own flag.
        rendered_roundup = render_roundup_alerts(roundup)
        claimed = await _claim(
            deps,
            guild_id,
            [c.code for c in roundup],
            pinged=False,
            local_day=local_day,
            now=now,
            from_roundup=True,
        )
        if claimed is not None:
            roundup_posted, roundup_failed = await _post_batch(
                deps, guild_id, poster, rendered_roundup, notify
            )
            failed += roundup_failed

    return GuildOutcome(
        guild_id,
        posted=posted,
        roundup_posted=roundup_posted,
        failed=failed,
        pinged=final_ping,
        cap_reached=cap_reached,
    )


async def deliver_queued_codes(deps: FanoutDeps, *, startup: bool = False) -> list[GuildOutcome]:
    """Work through the delivery queue: every server with `queued` codes, in guild id order.

    Runs at the end of every collection pass (so a walk the hook timeout cut
    short is finished by the next one) and can be called at startup
    (`startup=True`) to do the same after a crash. It starts with crash
    recovery: a `pending` row is a claim whose send never got confirmed, so it
    becomes `failed` and its server is told; at startup that is every
    `pending` row, mid-run only the stale ones. Nothing `pending` is ever
    sent again.

    A timeout or cancellation leaves whatever hasn't had its turn `queued`.
    Two passes running at once can't double-send: the queued -> pending claim
    is atomic, and the loser sees it lost and moves on.
    """
    now = deps.now()
    stale_before = None if startup else now - timedelta(seconds=_PENDING_STALE_S)
    await recover_pending_guild_codes(deps.db_path, deps.notify_guild, claimed_before=stale_before)

    def _queued_sync() -> list[int]:
        with closing(connect(deps.db_path)) as conn:
            return queued_guild_ids(conn)

    outcomes: list[GuildOutcome] = []
    for i, guild_id in enumerate(await asyncio.to_thread(_queued_sync)):
        if i:
            await deps.sleep(deps.pace_s)
        try:
            outcomes.append(await _deliver_one(deps, guild_id, now))
        except Exception as exc:
            # One server's broken channel (or a bug that only its settings
            # tickle) is its own business; the rest of the list still gets its turn.
            # What it hadn't claimed yet is failed, not left queued: a bug
            # doesn't fix itself by the next pass, and an hourly notice isn't a fix.
            logger.exception("SHiFT delivery failed for a guild", extra={"guild_id": guild_id})
            outcomes.append(GuildOutcome(guild_id, error=str(exc)))
            await asyncio.to_thread(_fail_queued_sync, deps.db_path, guild_id)
            await _safe_notify(
                lambda text, g=guild_id: deps.notify_guild(g, text),
                "newsbot: a SHiFT code alert couldn't be posted to your server this time.",
            )
    return outcomes


def _fail_queued_sync(db_path: str, guild_id: int) -> None:
    with closing(connect(db_path)) as conn:
        fail_queued_guild_codes(conn, guild_id)


async def detect_and_fan_out(deps: FanoutDeps, items: list[RawItem], *, seeding_ok: bool) -> int:
    """Detect codes in `items` once, queue the new ones, then deliver the queue.

    Returns how many codes were released (queued for servers or roundup-queued)
    this pass. `items` is everything collected, before dedupe: a code edited
    into an already-seen thread still counts, and the code tables do their own
    dedupe. Delivery runs even when nothing new was found, because the queue
    may still hold codes from a pass that ran out of time.
    """
    released = await _release(deps, items, seeding_ok=seeding_ok)
    await deliver_queued_codes(deps)
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

    candidates = aggregate(
        sightings,
        now=now,
        max_age=timedelta(hours=shift_cfg.max_item_age_hours),
        ping_trust=tuple(shift_cfg.ping_trust),
    )

    def _plan_and_record_sync() -> tuple[int, int]:
        with closing(connect(deps.db_path)) as conn:
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
            return fail_pending_guild_codes(conn, claimed_before=claimed_before)

    by_guild = await asyncio.to_thread(_sync)
    for guild_id, codes in by_guild.items():
        await _safe_notify(
            lambda text, g=guild_id: notify_guild(g, text),
            "newsbot restarted while a SHiFT code alert was in flight; it may or may not "
            "have posted. Codes not retried: " + ", ".join(codes),
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
