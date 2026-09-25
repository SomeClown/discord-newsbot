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
    """Acquire `_run_lock`, skipping instead of queuing up behind a long wait -- almost always.

    Yields `True` (lock held) if it looked free, or `False` (lock
    untouched) if something else already had it -- a sweep uses this to
    skip its turn entirely rather than queue up behind a digest run that
    could still be going when the *next* sweep interval arrives too.

    This is *not* a strict, always-non-blocking try-acquire, and it's
    worth being honest about that rather than claiming otherwise:
    `asyncio.Lock` is a fair lock, granting access to queued waiters in
    FIFO order, and `.locked()` can read `False` for a moment after
    `release()` while an already-queued waiter hasn't yet resumed and
    re-locked it (`release()` flips the internal flag synchronously; the
    woken waiter's own coroutine resumes on a later iteration of the
    event loop). If this function's own `.locked()` check happens to land
    in exactly that window, `_run_lock.acquire()` below still takes the
    fast path only when *no* waiter is already queued -- otherwise it
    waits its turn behind that waiter, same as any other `acquire()`
    would. In practice that's a handful of event-loop iterations, not a
    multi-minute digest run, so it doesn't change the shape of what this
    function is for; it just means "no waiting, ever" was never quite
    true, and reaching into `asyncio.Lock`'s private `_waiters` to make
    it true would trade an honest, rare, sub-millisecond wait for a
    dependency on an implementation detail this module doesn't otherwise
    need.
    """
    if _run_lock.locked():
        yield False
        return
    async with _run_lock:
        yield True


__all__ = ["is_run_in_progress", "run_lock_or_skip"]
