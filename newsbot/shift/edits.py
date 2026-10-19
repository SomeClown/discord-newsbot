"""Edit the SHiFT alerts we already posted when we learn something new about their codes.

An alert goes out the moment a code turns up, which is usually before anyone knows what
the code is worth. A few hours later a table somewhere lists the reward and the expiry,
and the message in the channel is still the bare code. This module is the proofreader who
comes back through the channel with a pen: it finds every posted alert (inside two weeks)
whose codes have learned something since the message was written, re-renders the whole
message from the same codes in the same order, and edits it in place. No new message, no
ping, nobody's phone buzzes. (Discord doesn't notify anyone for an edit of a mention the
message already had.)

"Learned something since" is a version stamp, not a clock. `guild_alert_messages.rendered_at`
is the largest `details_changed_at` the message was rendered with, and a message is due when
any of its codes has a newer one (`repo.alert_messages_to_edit`). I did try a real clock
first; two passes that took `now` a few milliseconds apart could make a change look older
than the render that missed it. The stamps only compare with each other, so that can't
happen. It also makes a reward that flaps between two values boring: the stamp moves once
per net change per pass, so each message is edited at most once per pass, and whatever the
flap does, it can't multiply the edits.

An edit keeps the mentions its message already had. Discord rebuilds a message's mentions
from the edit's `allowed_mentions`, so sending none strips the highlight from an alert that
pinged (the text stays, the notification was long since delivered or not). The message's
ping choice is stored when it posts (`guild_alert_messages.ping`) and goes back with the
edit, but only while the stored header still starts with that ping's own text. A header
that lost its ping to the ambiguous-failure retry stores "none", so an edit can't add a
mention the message never carried, and an edit never makes a notification of its own.

The edits ride the delivery walk's pacing and its time budget, after the deliveries and
follow-ups (`fanout._deliver_then_edit`). Whatever the deadline cuts off keeps its old stamp
and goes first next pass.

The queue is taken in turns, like a deli counter: each server's first due message, then each
server's second, and so on, with at most 20 of any one server's per pass. A server with a
pile of due messages can't spend the pass while the small ones wait, and a message that keeps
failing is remembered (`edit_tried_at`) and goes to the back of its own server's line, so it
can't hold the head of the queue for two weeks. A server that fails an edit (without being
gone or forbidden) is skipped for the rest of that pass; a sick channel costs one try.

How a message's edit can end:

- the message is gone (Discord 10008, or its channel is 10003): marked `gone`, never retried,
  nobody told;
- the bot may not edit there (403): the server's admin channel hears about it once, its edits
  stop (`guild_shift.edits_blocked_at`), and saving `/newsbot shift` starts them again;
- the message won't render (a config that grew a longer source name than the message was
  packed for): logged once, and its stamp advances so it isn't retried every hour;
- a 5xx or a timeout: left as it is for the next pass.

Roundups and alerts from before 3.1.0 have no record, so they are never found here.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from newsbot.bot.format import render_alert_edit
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.sweep import (
    ChannelGoneError,
    EditForbiddenError,
    MessageGoneError,
    ping_prefix_for,
)
from newsbot.store.db import connect
from newsbot.store.models import DETAILS_STAMP_FLOOR, EditTarget, QueuedCode
from newsbot.store.repo import (
    alert_message_codes,
    alert_messages_to_edit,
    block_guild_edits,
    mark_alert_message_gone,
    mark_alert_message_rendered,
    mark_alert_message_tried,
    mark_channel_alert_messages_gone,
)

if TYPE_CHECKING:
    from newsbot.shift.fanout import EditorFactory, FanoutDeps

logger = logging.getLogger(__name__)

# One pass looks at this many due messages at most, spread across servers by
# `alert_messages_to_edit` (a server gets a handful of turns, not the whole batch); the rest
# are due next pass too.
_EDIT_BATCH = 200

# A 429 is retried this many times in all, each after the wait Discord asked for (or these
# steps when it didn't say), as long as the wait fits in the walk's budget.
_RATE_LIMIT_TRIES = 3
_RATE_LIMIT_BACKOFF_S = (2.0, 4.0)

_BLOCKED_NOTICE = (
    "newsbot: I couldn't update a SHiFT alert in <#{channel_id}> with new code details "
    "(missing permission). I'll stop trying until /newsbot shift is saved again."
)


def _schema_error(exc: sqlite3.OperationalError) -> bool:
    """Whether `exc` is a database that lacks something we need, as opposed to one that's busy.

    A locked database fixes itself by the next pass. A missing column or table (a development
    database that ran an earlier draft of migration 009 and slipped past the startup check)
    never will, and calling it "busy" every hour is how it would stay unnoticed.
    """
    text = str(exc).lower()
    return "no such column" in text or "no such table" in text


def _ping_for(target: EditTarget) -> str:
    """The ping choice an edit of `target` sends back: the stored one, if the header still wears it.

    A header that doesn't start with its ping's text (stripped by a retry, or edited by
    hand in the database) gets "none", so an edit can't put a mention on a message that
    hasn't got one.
    """
    prefix = ping_prefix_for(target.ping)
    return target.ping if prefix and target.header.startswith(prefix) else "none"


@dataclass(frozen=True)
class EditOutcome:
    """What one edit pass did, for logs and tests.

    `waiting` counts due messages the pass never got to (the deadline, or a server that
    stalled); they keep their old stamp and are due again next pass. `failed` counts messages
    that were tried and left due (a 5xx, a timeout, an error nobody planned for).
    """

    edited: int = 0
    gone: int = 0
    blocked_guilds: int = 0
    unrenderable: int = 0
    failed: int = 0
    waiting: int = 0


# What one message's attempt came to. `_BROKEN` is a failure on our side of the wire (a read or
# a render that blew up), counted as failed; unlike `_FAILED` it says nothing about the server.
(
    _EDITED,
    _GONE,
    _CHANNEL_GONE,
    _FORBIDDEN,
    _UNRENDERABLE,
    _FAILED,
    _BROKEN,
    _LIMITED,
) = (
    "edited",
    "gone",
    "channel_gone",
    "forbidden",
    "unrenderable",
    "failed",
    "broken",
    "limited",
)


def _targets_sync(deps: FanoutDeps) -> list[EditTarget]:
    with closing(connect(deps.db_path)) as conn:
        return alert_messages_to_edit(conn, now=deps.now(), limit=_EDIT_BATCH)


def _candidate(queued: QueuedCode) -> CodeCandidate:
    return CodeCandidate(
        queued.code,
        golden=queued.golden,
        source_name=queued.source_name,
        item_url=queued.item_url,
        fresh=True,
        trusted=queued.trusted,
    )


def _stamp_of(codes: list[QueuedCode]) -> datetime:
    """The version stamp these codes' details add up to: the newest change, or the floor."""
    return max(
        (
            c.details.details_changed_at
            for c in codes
            if c.details is not None and c.details.details_changed_at is not None
        ),
        default=DETAILS_STAMP_FLOOR,
    )


def _read_sync(deps: FanoutDeps, target: EditTarget) -> list[QueuedCode]:
    with closing(connect(deps.db_path)) as conn:
        return alert_message_codes(conn, target.guild_id, target.message_id)


async def _write(
    deps: FanoutDeps, target: EditTarget, write: Callable[[sqlite3.Connection], object]
) -> None:
    """Record an outcome, tolerating a busy database.

    A lost write is cheap here: the message stays due, and next pass re-renders the same
    thing and edits the same text again. So one try, a warning, and on we go.
    """

    def _sync() -> None:
        with closing(connect(deps.db_path)) as conn:
            write(conn)

    try:
        await asyncio.to_thread(_sync)
    except sqlite3.OperationalError as exc:
        if _schema_error(exc):
            logger.error(
                "SHiFT edit bookkeeping can't use the database: %s",
                exc,
                extra={"guild_id": target.guild_id, "message_id": target.message_id},
            )
            raise
        logger.warning(
            "SHiFT edit bookkeeping hit a busy database (%s); the message stays due",
            exc,
            extra={"guild_id": target.guild_id, "message_id": target.message_id},
        )


async def _gone(deps: FanoutDeps, target: EditTarget) -> None:
    await _write(
        deps, target, lambda c: mark_alert_message_gone(c, target.guild_id, target.message_id)
    )


async def _tried(deps: FanoutDeps, target: EditTarget) -> None:
    """Note an attempt that didn't take; the message goes to the back of its server's line."""
    now = deps.now()
    await _write(
        deps,
        target,
        lambda c: mark_alert_message_tried(c, target.guild_id, target.message_id, now),
    )


async def _rendered(deps: FanoutDeps, target: EditTarget, stamp: datetime) -> None:
    now = deps.now()
    await _write(
        deps,
        target,
        lambda c: mark_alert_message_rendered(
            c, target.guild_id, target.message_id, stamp, tried_at=now
        ),
    )


async def _broken(deps: FanoutDeps, target: EditTarget, what: str) -> str:
    """Log (with the traceback; call this from the `except`) and remember the try. Moves on."""
    logger.exception(what, extra={"guild_id": target.guild_id, "message_id": target.message_id})
    await _tried(deps, target)
    return _BROKEN


async def _edit_one(
    deps: FanoutDeps, editor_for: EditorFactory, target: EditTarget, deadline: float | None
) -> str:
    """Re-render and edit one message. Returns which outcome it came to.

    Nothing that goes wrong with *this* message gets out of here: a read or a render that
    blows up is logged with its traceback and left due (a `ValueError` from the render is the
    one expected kind, handled below). The pass has other servers to see to. The one thing
    that does get out is a database that lacks a column or table (`_schema_error`), because
    no amount of moving on will fix that.
    """
    try:
        codes = await asyncio.to_thread(_read_sync, deps, target)
        if not codes:
            # Its codes are gone from under it (a purge, a hand edit). Nothing to render, ever.
            logger.warning(
                "a SHiFT alert record has no codes; leaving it alone",
                extra={"guild_id": target.guild_id, "message_id": target.message_id},
            )
            await _gone(deps, target)
            return _GONE
        details = {c.code: c.details for c in codes if c.details is not None}
        stamp = _stamp_of(codes)
        candidates = [_candidate(c) for c in codes]
    except Exception:
        return await _broken(deps, target, "couldn't read a SHiFT alert's codes for an edit")

    try:
        content = render_alert_edit(
            target.header,
            candidates,
            golden_prefix=target.golden_prefix,
            details=details,
            now=deps.now(),
        )
    except ValueError as exc:
        # Said once: the stamp moves up to what we tried, so the same change isn't retried
        # (and re-complained about) every hour. A newer change gets a fresh try.
        logger.error(
            "a SHiFT alert can't be re-rendered for an edit; leaving it as it is",
            extra={"guild_id": target.guild_id, "message_id": target.message_id, "error": str(exc)},
        )
        await _rendered(deps, target, stamp)
        return _UNRENDERABLE
    except Exception:
        # Not the expected kind of failure, so no stamp advance: whatever it is, it may be fixed
        # by the next deploy, and the message should get its turn again.
        return await _broken(deps, target, "SHiFT alert render failed unexpectedly")

    editor = editor_for(target.guild_id, target.channel_id)
    for attempt in range(_RATE_LIMIT_TRIES):
        try:
            await editor.edit(target.message_id, content, ping=_ping_for(target))
        except ChannelGoneError:
            return _CHANNEL_GONE
        except MessageGoneError:
            await _gone(deps, target)
            return _GONE
        except EditForbiddenError:
            return _FORBIDDEN
        except PublishError as exc:
            if not exc.rejected:
                # A 5xx or a timeout. It may or may not have landed; either way the next pass
                # renders the same thing and edits it again, which is harmless.
                logger.warning(
                    "SHiFT alert edit failed; it stays due",
                    extra={"guild_id": target.guild_id, "message_id": target.message_id},
                )
                await _tried(deps, target)
                return _FAILED
            wait = (
                exc.retry_after
                if exc.retry_after is not None
                else _RATE_LIMIT_BACKOFF_S[min(attempt, len(_RATE_LIMIT_BACKOFF_S) - 1)]
            )
            last_try = attempt == _RATE_LIMIT_TRIES - 1
            if last_try or (deadline is not None and deps.clock() + wait >= deadline):
                await _tried(deps, target)
                return _LIMITED
            await deps.sleep(wait)
        except Exception:
            logger.exception(
                "SHiFT alert edit failed unexpectedly",
                extra={"guild_id": target.guild_id, "message_id": target.message_id},
            )
            await _tried(deps, target)
            return _FAILED
        else:
            await _rendered(deps, target, stamp)
            return _EDITED
    await _tried(deps, target)
    return _LIMITED


async def _block(deps: FanoutDeps, target: EditTarget) -> None:
    """Stop this server's edits and say so, once: only the call that flips the flag speaks."""
    try:
        first = await asyncio.to_thread(_block_sync, deps, target.guild_id)
    except sqlite3.OperationalError as exc:
        if _schema_error(exc):
            logger.error("couldn't block a server's edits: %s", exc)
            raise
        # Not recorded, so not announced either: next pass will hit the 403 again and try.
        logger.warning("couldn't block a server's edits (busy database: %s)", exc)
        return
    if not first:
        return
    try:
        await deps.notify_guild(
            target.guild_id, _BLOCKED_NOTICE.format(channel_id=target.channel_id)
        )
    except Exception:
        # Telling a server about a problem must not become the next problem.
        logger.exception("couldn't send a guild notice")


def _block_sync(deps: FanoutDeps, guild_id: int) -> bool:
    with closing(connect(deps.db_path)) as conn:
        return block_guild_edits(conn, guild_id, deps.now())


async def edit_changed_alerts(deps: FanoutDeps, *, deadline: float | None = None) -> EditOutcome:
    """Edit every due alert message, within `deadline` (a `deps.clock()` reading).

    Round-robin across servers (each one's first due message, then each one's second, at most
    20 of any one server's), a breath (`deps.pace_s`) between edits. A server drops out of the
    rest of the pass when:
    - it's forbidden (it is also blocked, with one notice);
    - one of its edits fails in a way waiting might fix (a 5xx, a 429 past the budget, an
      error nobody planned for), so a sick channel costs one try and not twenty;
    - its channel is gone, which finishes every message in that channel in one go.

    The others carry on. Does nothing in preview mode or without an editor. Takes the delivery
    lock, so it can't run alongside startup's recovery.
    """
    editor_for = deps.editor_for
    if deps.preview or editor_for is None:
        return EditOutcome()
    async with deps.delivery_lock:
        try:
            targets = await asyncio.to_thread(_targets_sync, deps)
        except sqlite3.OperationalError as exc:
            if _schema_error(exc):
                logger.error("SHiFT edits can't read the database: %s", exc)
                raise
            logger.warning("SHiFT edits hit a busy database (%s); they wait for the next pass", exc)
            return EditOutcome()
        counts = dict.fromkeys((_EDITED, _GONE, _UNRENDERABLE, _FAILED), 0)
        blocked_guilds: set[int] = set()
        stalled: set[int] = set()
        dead_channels: set[tuple[int, int]] = set()
        waiting = 0
        tried = False
        for i, target in enumerate(targets):
            if target.guild_id in blocked_guilds or target.guild_id in stalled:
                waiting += 1
                continue
            if (target.guild_id, target.channel_id) in dead_channels:
                counts[_GONE] += 1  # already finished off with the rest of its channel
                continue
            if tried:
                await deps.sleep(deps.pace_s)
            if deadline is not None and deps.clock() >= deadline:
                waiting += len(targets) - i
                logger.warning(
                    "SHiFT edits out of time; the rest stay due for the next pass",
                    extra={"messages_left": len(targets) - i},
                )
                break
            tried = True
            outcome = await _edit_one(deps, editor_for, target, deadline)
            if outcome == _FORBIDDEN:
                blocked_guilds.add(target.guild_id)
                await _block(deps, target)
            elif outcome == _CHANNEL_GONE:
                dead_channels.add((target.guild_id, target.channel_id))
                counts[_GONE] += 1
                await _write(
                    deps,
                    target,
                    lambda c, t=target: mark_channel_alert_messages_gone(
                        c, t.guild_id, t.channel_id
                    ),
                )
            elif outcome == _LIMITED:
                stalled.add(target.guild_id)
                waiting += 1
            elif outcome == _BROKEN:
                counts[_FAILED] += 1
            else:
                counts[outcome] += 1
                if outcome == _FAILED:
                    stalled.add(target.guild_id)
        result = EditOutcome(
            edited=counts[_EDITED],
            gone=counts[_GONE],
            blocked_guilds=len(blocked_guilds),
            unrenderable=counts[_UNRENDERABLE],
            failed=counts[_FAILED],
            waiting=waiting,
        )
    if targets:
        logger.info(
            "SHiFT alert edits",
            extra={
                "edited": result.edited,
                "gone": result.gone,
                "blocked_servers": result.blocked_guilds,
                "unrenderable": result.unrenderable,
                "failed": result.failed,
                "waiting": result.waiting,
            },
        )
    return result


__all__ = ["EditOutcome", "edit_changed_alerts"]
