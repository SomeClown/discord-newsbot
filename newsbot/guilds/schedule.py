"""Decide which servers are owed a digest right now. Pure: no clock, no database, no Discord.

v2 had one server and one cron line, and cron is very good at "09:00
America/Los_Angeles" right up until the clocks change. v3 has a digest time
and a time zone per server, which is a few hundred tiny cron lines, each with
its own opinion about daylight saving. So there is no cron for digests. A job
wakes every minute, SQL fetches the set-up servers with their newest digest
row (`repo.due_candidates`), and `due_guilds` below answers one question per
server: has its local digest time passed on its local date, with nothing
finished for that date yet? Polling instants instead of scheduling them
sidesteps the whole class of "the job was due in an hour that didn't exist"
bugs, and catch-up after downtime needs no code of its own: the first tick
after a restart finds every server whose time already went by.

The rules, all of them decided on purpose (the DST ones were decided after
reading the `zoneinfo` docs twice):

- **A time in the spring-forward gap** (02:30 on the night 02:00 becomes
  03:00) resolves to the same number of minutes into the gap, so 03:30 local.
  That's what `fold=0` does, and it runs exactly once, since 02:30 never
  happens for the "already ran today" check to disagree with.
- **A time in the fall-back hour** (01:30 happens twice) means the first
  one. The second can't run it again: the day's row exists by then.
- **Changing the time or the zone mid-day** changes nothing retroactively.
  Each tick judges the current settings against the newest row. A day that
  already posted doesn't post again because someone edited a setting. A day
  that hasn't posted yet, whose new time already passed, posts on the next
  tick (same as a missed digest: the server asked for "09:00" and it's
  10:30, so it's late, not cancelled). A zone change that moves the local
  date forward starts a new local day, which can mean two digests inside
  one real day; their windows chain, so no item is posted twice. A zone
  change that moves the date backward is not due (the newest row is
  "tomorrow"), which waits the difference out.
- **Nothing is due when the newest row is dated after the local date** (a
  clock step, or the westward zone change above).
- **An unfinished row keeps its own date.** A retryable `failed` row or a
  resumable `pending` one is judged for the day it was written, even after the
  server's local date has moved on (a 23:59 digest that fails at 23:59 would
  otherwise be abandoned at midnight, and its unposted games with it). Only when
  nothing is left to retry or resume does the next day's digest start. While an
  unfinished row is merely *waiting* (inside the retry gap, or a `pending` row
  whose lease hasn't gone stale), the newer day waits too, because its window
  would chain onto a digest that hasn't finished.
- **A `pending` row is a lease.** The process publishing it refreshes
  `updated_at` as each game lands and on a heartbeat, so another process (or a
  restart) may only resume it once that stamp is `LEASE_STALE_AFTER` old. The
  `running` set only knows about this process; the lease is what stops a
  second one from resuming a digest the first is still busy posting.

The window rule lives here too (`digest_window`), so all the time arithmetic
is in one file I can be wrong in.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from newsbot.store.models import DueCandidate

logger = logging.getLogger(__name__)

# v2's clean-failure retry, bounded so a broken server can't be hammered:
# three attempts in all, at least ten minutes apart.
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=10)
# A `pending` row is resumable once nobody has touched it for this long. The
# publisher refreshes it on a heartbeat (`guild_digest.HEARTBEAT_S`, 60 s) and
# scheduled runs are capped well under this (`guild_digest.GUILD_TIMEOUT_S`,
# 5 min), so a live publisher is never this stale. The price is that a bot
# restarted after a crash waits up to this long before resuming.
LEASE_STALE_AFTER = timedelta(minutes=10)
# A run that starts this long after its due instant is reported as catch-up
# rather than scheduled. The minute job is never exactly on time, so a
# little lag is normal; this is "the bot was down" territory.
CATCH_UP_AFTER = timedelta(minutes=5)
# The first digest covers a day; a chained one is floored at two, so a
# server that was offline for a week doesn't get seven days in one embed.
DEFAULT_WINDOW = timedelta(hours=24)
WINDOW_FLOOR = timedelta(hours=48)

DueReason = Literal["first", "retry", "resume"]


@dataclass(frozen=True)
class DueGuild:
    """One server owed a digest: which local day, when it was due, and why it's due."""

    guild_id: int
    run_date: date
    due_at: datetime
    reason: DueReason
    # True when this isn't the on-time run: it's a retry or a resume, or it
    # started `CATCH_UP_AFTER` or more past its due instant (the bot was down).
    catch_up: bool = False


def local_due_instant(day: date, hhmm: str, timezone: str) -> datetime:
    """The UTC instant at which local `hhmm` on `day` happens in `timezone`.

    Builds the wall-clock time with `fold=0` and lets `zoneinfo` do the
    arithmetic: a nonexistent time (spring forward) lands the same distance
    past the gap's start, and an ambiguous one (fall back) is its first
    occurrence. Raises `ValueError` for a malformed time and
    `ZoneInfoNotFoundError` for an unknown zone; `due_guilds` turns both into
    "skip this server".
    """
    hour, minute = (int(part) for part in hhmm.split(":"))
    wall = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(timezone))
    return wall.astimezone(UTC)


def lease_is_stale(updated_at: datetime | None, now: datetime) -> bool:
    """True when a `pending` row has gone `LEASE_STALE_AFTER` without a sign of life.

    A row with no readable timestamp counts as stale: a lease nobody can read
    would otherwise hold its server hostage forever.
    """
    return updated_at is None or now - updated_at > LEASE_STALE_AFTER


def retry_is_ready(
    *, posted_any: bool, attempts: int, updated_at: datetime | None, now: datetime
) -> bool:
    """True when a `failed` row may be retried on its own.

    Only a *clean* failure retries. One that got some games out is an admin's
    call (run-now asks first), as it was in v2. The claim re-checks this under
    its lock, so two ticks that read the same stale row can't both retry it.
    """
    return (
        not posted_any
        and attempts < MAX_ATTEMPTS
        and updated_at is not None
        and now - updated_at > RETRY_AFTER
    )


def _unfinished(
    candidate: DueCandidate, now: datetime, running: Collection[int]
) -> tuple[DueReason | None, bool]:
    """`(reason, waiting)` for the guild's newest row when it isn't finished.

    `reason` is `retry` or `resume` when it should run now. `waiting` is true
    when it's unfinished and will be ready later (inside the retry gap, or a
    lease that hasn't gone stale), so the next day's digest must not jump the
    queue. Neither set means the row is finished, or nobody will ever touch it.
    """
    if candidate.status == "failed":
        if candidate.posted_any or candidate.attempts >= MAX_ATTEMPTS:
            return None, False
        if candidate.updated_at is None:
            return None, False
        if retry_is_ready(
            posted_any=False,
            attempts=candidate.attempts,
            updated_at=candidate.updated_at,
            now=now,
        ):
            return "retry", False
        return None, True
    if candidate.status == "pending":
        # One with no window was written by v2.2, which recorded nothing per
        # game, so nobody knows what it posted: that one waits for an admin's
        # run-now, as it always did.
        if candidate.window_end is None:
            return None, False
        if candidate.guild_id in running:
            return None, True
        if lease_is_stale(candidate.updated_at, now):
            return "resume", False
        return None, True
    return None, False


def due_guilds(
    candidates: Iterable[DueCandidate], now: datetime, running: Collection[int] = ()
) -> list[DueGuild]:
    """The servers owed a digest at `now`, oldest due instant first.

    `running` is the guilds this process is publishing right now, so a slow
    digest isn't mistaken for a crashed one. A server with a bad time zone or
    time string is logged and skipped; it never stops the others from being
    judged.
    """
    due: list[DueGuild] = []
    for candidate in candidates:
        try:
            local_date = now.astimezone(ZoneInfo(candidate.timezone)).date()
            due_at = local_due_instant(local_date, candidate.digest_time, candidate.timezone)
            row_due_at = due_at
            if candidate.run_date is not None and candidate.run_date < local_date:
                row_due_at = local_due_instant(
                    candidate.run_date, candidate.digest_time, candidate.timezone
                )
        except ValueError, ZoneInfoNotFoundError, OSError:
            logger.warning(
                "skipping guild %s: bad digest time or zone", candidate.guild_id, exc_info=True
            )
            continue
        if candidate.run_date is not None:
            if candidate.run_date > local_date:
                continue
            reason, waiting = _unfinished(candidate, now, running)
            if reason is not None:
                due.append(
                    DueGuild(candidate.guild_id, candidate.run_date, row_due_at, reason, True)
                )
                continue
            if waiting or candidate.run_date == local_date:
                continue
        if now < due_at:
            continue
        catch_up = now - due_at >= CATCH_UP_AFTER
        due.append(DueGuild(candidate.guild_id, local_date, due_at, "first", catch_up))
    due.sort(key=lambda d: (d.due_at, d.guild_id))
    return due


def digest_window(end: datetime, previous_end: datetime | None) -> tuple[datetime, datetime]:
    """`(start, end)` for a digest that ends at `end`, chained onto the last one.

    Items are selected by `collected_at` in `(start, end]`. Chaining each
    window onto the previous digest's end leaves no gap and no overlap, which
    is how a run-now at 08:00 followed by tomorrow's 09:00 digest stays honest.
    The start is floored at 48 hours back; a first digest (no previous end)
    covers 24. A previous end at or after this one (a zone change that pulled
    the next digest earlier, or a clock step) gives an empty window that starts
    at the previous end: nothing new, and above all nothing shown twice.
    Instants, not calendar days, so a 23-hour or 25-hour DST day just works.
    """
    if previous_end is None:
        return end - DEFAULT_WINDOW, end
    return max(previous_end, end - WINDOW_FLOOR), end
