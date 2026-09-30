"""Find SHiFT codes once, then tell every server that asked.

v2 had one server, so "find a code" and "announce a code" were one step and
nobody had to think about the gap between them. v3 has as many servers as
will have us, and the gap is the whole design. Detection is global and happens
once per collection pass: the regex, trust, age limit, roundup handling and
the silent first sweep all behave exactly as design.md §12 decided, against
the one `alerted_codes` table. Then the codes released in *this pass* (and only
this pass, which is why enabling alerts never produces a backlog) fan out to
every eligible server, each with its own channel, its own ping choice and its
own daily ping budget in its own local day. Think of it as one town crier who
reads the news once and then visits each house, rather than each house hiring
a crier.

Two rules carry over from v2 without apology: a code's claim is written
*before* anything is sent (a crash loses an alert, never doubles a ping), and
a failure in one server's channel is that server's problem. It goes to that
server's own admin channel and never stops the next server in line. The owner
hears about the detector breaking (`pipeline/collect.py` does that), not about
someone deleting their SHiFT channel.

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
    CodeCandidate,
    aggregate,
    plan_alerts,
    seeding_healthy,
    sightings_from_items,
)
from newsbot.shift.sweep import CodeAlertPoster, post_alert_with_retry
from newsbot.store.db import connect
from newsbot.store.models import GuildSettings, ShiftSettings
from newsbot.store.repo import (
    claim_guild_codes,
    eligible_shift_guilds,
    fail_pending_guild_codes,
    get_alert_state,
    guild_posted_codes,
    known_codes,
    mark_guild_codes_failed,
    mark_guild_codes_posted,
    record_released_codes,
    record_silent_codes,
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


@dataclass
class FanoutDeps:
    cfg: AppConfig
    db_path: str
    now: Callable[[], datetime]
    poster_for: PosterFactory
    notify_guild: NotifyGuild
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    pace_s: float = _GUILD_PACE_S


@dataclass(frozen=True)
class GuildOutcome:
    """What one server's turn did, for logs and tests."""

    guild_id: int
    posted: int = 0
    roundup_posted: int = 0
    failed: int = 0
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


def _unposted_sync(db_path: str, guild_id: int, codes: list[str]) -> set[str]:
    with closing(connect(db_path)) as conn:
        return set(codes) - guild_posted_codes(conn, guild_id, codes)


async def _claim(
    deps: FanoutDeps,
    guild_id: int,
    codes: list[str],
    *,
    pinged: bool,
    local_day: str,
    now: datetime,
    from_roundup: bool,
) -> bool:
    def _sync() -> bool:
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

    return await asyncio.to_thread(_sync)


async def _fan_out_one(
    deps: FanoutDeps,
    guild: GuildSettings,
    shift: ShiftSettings,
    to_post: list[CodeCandidate],
    roundup_to_post: list[CodeCandidate],
    now: datetime,
) -> GuildOutcome:
    """One server's turn. Raises freely; `_fan_out` catches it so nobody else waits."""
    guild_id = guild.guild_id
    channel_id = shift.channel_id
    if channel_id is None:  # eligible_shift_guilds never returns one; belt and suspenders
        return GuildOutcome(guild_id)

    async def notify(text: str) -> None:
        await deps.notify_guild(guild_id, text)

    # A server's own history decides what's news to it: a code it already has a
    # row for (the import copies v2.2's posted codes in) never posts again.
    fresh_codes = await asyncio.to_thread(
        _unposted_sync, deps.db_path, guild_id, [c.code for c in to_post + roundup_to_post]
    )
    normal = [c for c in to_post if c.code in fresh_codes]
    roundup = [c for c in roundup_to_post if c.code in fresh_codes]
    if not normal and not roundup:
        return GuildOutcome(guild_id)

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
        # blew up afterwards would sit there until the next startup.)
        rendered = render_code_alerts(normal, ping=want_ping, ping_mention=mention)
        final_ping = await _claim(
            deps,
            guild_id,
            [c.code for c in normal],
            pinged=want_ping,
            local_day=local_day,
            now=now,
            from_roundup=False,
        )
        if want_ping and not final_ping:
            cap_reached = True
            rendered = render_code_alerts(normal, ping=False)
        posted, failed = await _post_batch(deps, guild_id, poster, rendered, notify)
        if cap_reached and deps.cfg.shift.max_pings_per_day > 0:
            await _safe_notify(
                notify, "newsbot: SHiFT code alert daily ping cap reached; posted without a ping"
            )

    if roundup:
        # Never pinged, never counted against the cap; it claims with its own flag.
        rendered_roundup = render_roundup_alerts(roundup)
        await _claim(
            deps,
            guild_id,
            [c.code for c in roundup],
            pinged=False,
            local_day=local_day,
            now=now,
            from_roundup=True,
        )
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


async def _fan_out(
    deps: FanoutDeps,
    to_post: list[CodeCandidate],
    roundup_to_post: list[CodeCandidate],
    now: datetime,
) -> list[GuildOutcome]:
    def _eligible_sync() -> list[tuple[GuildSettings, ShiftSettings]]:
        with closing(connect(deps.db_path)) as conn:
            return eligible_shift_guilds(conn, deps.cfg.shift.games, now)

    eligible = await asyncio.to_thread(_eligible_sync)
    outcomes: list[GuildOutcome] = []
    for i, (guild, shift) in enumerate(eligible):
        if i:
            await deps.sleep(deps.pace_s)
        try:
            outcomes.append(await _fan_out_one(deps, guild, shift, to_post, roundup_to_post, now))
        except Exception as exc:
            # One server's broken channel (or a bug that only its settings
            # tickle) is its own business; the rest of the list still gets its turn.
            logger.exception("SHiFT fan-out failed for a guild", extra={"guild_id": guild.guild_id})
            outcomes.append(GuildOutcome(guild.guild_id, error=str(exc)))
            await _safe_notify(
                lambda text, g=guild.guild_id: deps.notify_guild(g, text),
                "newsbot: a SHiFT code alert couldn't be posted to your server this time.",
            )
    return outcomes


async def detect_and_fan_out(deps: FanoutDeps, items: list[RawItem], *, seeding_ok: bool) -> int:
    """Detect codes in `items` once, then post the new ones to every eligible server.

    Returns how many codes were released (posted or roundup-posted) this pass.
    `items` is everything collected, before dedupe: a code edited into an
    already-seen thread still counts, and the code tables do their own dedupe.
    """
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

    def _plan_and_record_sync() -> tuple[list[CodeCandidate], list[CodeCandidate], int]:
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
            )
            won = set(released)
            overflow = sum(1 for _, status in plan.silent if status == "roundup")
            return (
                [c for c in plan.to_post if c.code in won],
                [c for c in plan.roundup_to_post if c.code in won],
                overflow,
            )

    to_post, roundup_to_post, roundup_overflow = await asyncio.to_thread(_plan_and_record_sync)
    if roundup_overflow:
        logger.warning(
            "roundup codes recorded silently past the per-check cap",
            extra={"overflow": roundup_overflow},
        )
    if to_post or roundup_to_post:
        await _fan_out(deps, to_post, roundup_to_post, now)
    return len(to_post) + len(roundup_to_post)


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


async def recover_pending_guild_codes(db_path: str, notify_guild: NotifyGuild) -> int:
    """Startup: flip every `pending` server code to `failed` and tell each affected server.

    Returns the total code count; the owner is told that number and nothing
    more (which servers lost which codes is those servers' business).
    """

    def _sync() -> dict[int, list[str]]:
        with closing(connect(db_path)) as conn:
            return fail_pending_guild_codes(conn)

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
    "detect_and_fan_out",
    "make_shift_hook",
    "recover_pending_guild_codes",
]
