"""The daily quote run: pick a source, load it, draw a card, record it, post it.

This lives apart from `quotes.py` for a boring reason. `sources.py` imports
`quotes.py` for the fortune splitter and the `Quote` type, so a run that needs
both would make the two modules import each other, and Python has opinions
about that. Hence a third module whose whole job is to be the one that knows
the order of events.

The order of events, since it's the part that bites:

1. Unless forced, a cheap look at the database: if today's already done, stop
   before touching a single source.
2. Shuffle the sources with the injected rng and try them in that order, each
   at most once, until one gives us quotes. The first pick is uniform, so a
   1,000-quote page can't crowd out a 2-quote list; that's the point of
   choosing the source first and the quote second.
3. Draw from that source's own deck, claim it in the database (record), then
   post. Record-then-post means a post that fails leaves the quote used and
   the day done. One admin alert, no retry; a second attempt at a message
   Discord already refused once is how you get a second `Forbidden`.
4. If a source fell back to its saved copy, or one failed on the way to the
   post, say so in one admin message afterwards.

Everything an admin sees is built here from source descriptions and problem
strings that `sources.py` has already defused. Quote text, member names and
URL credentials never go into it. This module builds no Discord objects: the
caller injects `post` and `alert`, the way `SweepDeps` does.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx

from newsbot.config import QuoteSourceCfg
from newsbot.lounge.quotes import QuotePick, choose_quote, render_quote_message
from newsbot.lounge.sources import LoadedSource, cache_dir_for, describe, load_source
from newsbot.store.db import connect
from newsbot.store.repo import claim_quote, get_lounge_state, quote_deck_state
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

# Discord's message limit is 2,000; this leaves room for the ellipsis and a
# sense of proportion.
_ALERT_CHARS = 1900
_LINE_CHARS = 600
_REASON_CHARS = 200

Status = Literal["posted", "already_posted", "skipped", "post_failed"]


@dataclass
class QuoteDeps:
    """Everything `run_daily_quote` touches, injected so tests need no gateway.

    `local_day` is today's date in the digest timezone (the caller works it
    out with `local_run_date`). `cache_dir` defaults to the per-database
    directory `sources.py` would pick anyway.
    """

    sources: list[QuoteSourceCfg]
    db_path: str
    http: httpx.AsyncClient
    local_day: date
    now: Callable[[], datetime]
    post: Callable[[str], Awaitable[int]]
    alert: Callable[[str], Awaitable[None]]
    rng: random.Random
    cache_dir: Path | None = None


@dataclass(frozen=True)
class QuoteOutcome:
    """What a run did. `notes` are the admin lines about sources that misbehaved."""

    status: Status
    message_id: int | None = None
    source_key: str | None = None
    notes: tuple[str, ...] = ()


async def run_daily_quote(deps: QuoteDeps, *, force: bool = False) -> QuoteOutcome:
    """Run today's quote. `force` skips only the once-a-day guard (`/newsbot quote-now`)."""
    day = deps.local_day.isoformat()

    if not force and await asyncio.to_thread(_already_posted, deps.db_path, day):
        logger.info("Lounge quote for %s already posted; nothing to do", day)
        return QuoteOutcome("already_posted")

    cache_dir = deps.cache_dir or cache_dir_for(deps.db_path)
    # rng.sample, not a loop of rng.choice: each source gets at most one turn.
    order = deps.rng.sample(deps.sources, len(deps.sources))
    failures: list[tuple[QuoteSourceCfg, str]] = []
    chosen: tuple[QuoteSourceCfg, LoadedSource] | None = None
    for src in order:
        loaded = await load_source(src, http=deps.http, cache_dir=cache_dir, now=deps.now())
        if loaded.quotes:
            chosen = (src, loaded)
            break
        failures.append((src, loaded.problem or "no reason given"))

    if chosen is None:
        reasons = [f"{describe(src)}: {_reason(problem)}" for src, problem in failures]
        text = "newsbot: no lounge quote today. " + ("; ".join(reasons) or "no sources configured")
        logger.error("No lounge quote for %s: every source failed", day)
        await _tell_admin(deps, _cap(text))
        return QuoteOutcome("skipped", notes=tuple(reasons))

    src, loaded = chosen
    notes = _notes(failures, src, loaded)
    for note in notes:
        logger.warning("Lounge quote: %s", note)

    pick = await asyncio.to_thread(_draw_and_claim, deps, src.key, loaded, day, force)
    if pick is None:
        # Lost a race with the other trigger (job vs quote-now). They posted.
        logger.info("Lounge quote for %s was claimed by another run", day)
        return QuoteOutcome("already_posted", source_key=src.key)

    try:
        message_id = await deps.post(render_quote_message(pick.quote))
    except Exception as exc:
        logger.error("Posting the lounge quote failed (%s)", type(exc).__name__, exc_info=True)
        reason = _post_reason(exc, pick.quote.text)
        lines = [f"newsbot: couldn't post today's quote in the lounge: {reason}", *notes]
        await _tell_admin(deps, _cap("\n".join(lines)))
        return QuoteOutcome("post_failed", source_key=src.key, notes=tuple(notes))

    if notes:
        await _tell_admin(deps, _cap("\n".join(["newsbot: lounge quote source notes.", *notes])))
    return QuoteOutcome("posted", message_id, src.key, tuple(notes))


def _already_posted(db_path: str, day: str) -> bool:
    with closing(connect(db_path)) as conn:
        return get_lounge_state(conn).last_quote_date == day


def _draw_and_claim(
    deps: QuoteDeps, key: str, loaded: LoadedSource, day: str, force: bool
) -> QuotePick | None:
    """Read the deck, choose, claim; None if someone else claimed the day first."""
    with closing(connect(deps.db_path)) as conn:
        deck = quote_deck_state(conn, key)
        pick = choose_quote(loaded.quotes, deck, deps.rng)
        if pick is None:  # can't happen with a non-empty list; belt and suspenders
            return None
        won = claim_quote(
            conn,
            source_key=key,
            quote_hash=pick.hash,
            local_day=day,
            reshuffle=pick.reshuffle,
            force=force,
            now=deps.now,
        )
    return pick if won else None


def _notes(
    failures: list[tuple[QuoteSourceCfg, str]], used: QuoteSourceCfg, loaded: LoadedSource
) -> list[str]:
    """The admin lines for a run that found quotes: sources that failed, and a fallback."""
    lines = [
        plain_line(
            f"{describe(src)} failed with no saved copy ({_reason(problem)}); used another source.",
            _LINE_CHARS,
        )
        for src, problem in failures
    ]
    if loaded.origin == "fallback":
        saved = f"its saved copy from {loaded.saved_on}" if loaded.saved_on else "its saved copy"
        lines.append(
            plain_line(
                f"{describe(used)} failed ({_reason(loaded.problem or 'no reason given')}); "
                f"used {saved}.",
                _LINE_CHARS,
            )
        )
    return lines


def _reason(problem: str) -> str:
    return plain_line(problem, _REASON_CHARS)


def _post_reason(exc: Exception, quote_text: str) -> str:
    """A one-line reason for the admin, or just the class name if the message echoes the quote."""
    lines = [line for line in str(exc).splitlines() if line.strip()]
    first = plain_line(lines[0], _REASON_CHARS) if lines else ""
    if not first or (quote_text.strip() and quote_text.strip() in str(exc)):
        return type(exc).__name__
    return first


def _cap(text: str) -> str:
    return text if len(text) <= _ALERT_CHARS else text[: _ALERT_CHARS - 1].rstrip() + "…"


async def _tell_admin(deps: QuoteDeps, text: str) -> None:
    """Send an admin message; a dead alert channel mustn't undo an already recorded quote."""
    try:
        await deps.alert(text)
    except Exception:
        logger.warning("Couldn't send the lounge admin alert", exc_info=True)
