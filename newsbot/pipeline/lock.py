"""The one run lock, shared by the daily job and the SHiFT alert sweep.

This used to live entirely inside `pipeline/run.py`, which was fine right
up until the sweep (design.md §12, `shift/sweep.py`) needed the same lock
for a different reason: the daily job *waits* for it (nobody wants
`/newsbot run-now` silently skipped because an hourly sweep happened to be
running), but a sweep should just skip and try again next interval (A10)
-- waiting up to two minutes behind a digest run isn't worth it for
something that runs 24 times a day anyway. Pulling the lock itself out
here, with both waiting and skipping styles of access on top of it, is
what lets both callers share one lock without either one importing the
other's module.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# One process-wide lock. Every entry point that can trigger a run -- the
# daily job, `/newsbot run-now`, `/newsbot preview`, and now the hourly
# sweep -- holds this for the duration of its work, so no two of them can
# ever be collecting, summarizing, or posting at the same time.
_run_lock = asyncio.Lock()


def is_run_in_progress() -> bool:
    """True if `_run_lock` is currently held by anything.

    Exists so a slash command can give a quick "already running" reply
    instead of blocking on the lock for however long a run takes --
    nobody wants a command to sit there looking hung for two minutes.
    """
    return _run_lock.locked()


@asynccontextmanager
async def run_lock_or_skip() -> AsyncIterator[bool]:
    """Acquire `_run_lock` without waiting, or say no.

    Yields `True` (lock held) if it was free, or `False` (lock untouched)
    if something else already had it -- a sweep uses this to skip its turn
    entirely rather than queue up behind a digest run that could still be
    going when the *next* sweep interval arrives too. The check-then-
    acquire has no `await` between them, so nothing else on this event
    loop can slip in and grab the lock in between; `asyncio.Lock.acquire`
    only actually suspends when it has to wait, and this path only calls
    it when it won't.
    """
    if _run_lock.locked():
        yield False
        return
    async with _run_lock:
        yield True


__all__ = ["is_run_in_progress", "run_lock_or_skip"]
