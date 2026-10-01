"""The collection pass's lock: one shared collection at a time, and no queueing behind it.

This used to be the one lock the daily digest, `/newsbot run-now`, the preview
and the hourly SHiFT sweep all shared. Then v3 split the work up: the digests
went per-server (`pipeline/guild_digest.py` keeps its own per-guild locks), and
the only thing that holds this lock now is `run_collection`
(`pipeline/collect.py`), whose SHiFT delivery rides inside the pass and so
inside the lock. A pass that finds it held skips, writing nothing (A10): the
next interval is an hour away at most, and waiting up to a pass's length
behind the previous one is how passes stack. Startup's SHiFT delivery doesn't
use this lock; it takes the fan-out's own delivery lock (`FanoutDeps.delivery_lock`),
because it has no business waiting behind a nine-minute collection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# One process-wide lock. A collection pass holds it for its whole run, so no
# two passes can ever be collecting, storing, or delivering SHiFT codes at once.
_run_lock = asyncio.Lock()


def is_run_in_progress() -> bool:
    """True if `_run_lock` is currently held (that is, a collection pass is running).

    Nothing in the bot calls this today; it's here for a command that wants a
    quick "already running" instead of looking hung.
    """
    return _run_lock.locked()


@asynccontextmanager
async def run_lock_or_skip() -> AsyncIterator[bool]:
    """Acquire `_run_lock`, skipping instead of queuing up behind a long wait, almost always.

    Yields `True` (lock held) if it looked free, or `False` (lock
    untouched) if something else already had it: a collection pass uses this
    to skip its turn entirely rather than queue up behind a pass that
    could still be going when the *next* interval arrives too.

    This is *not* a strict, always-non-blocking try-acquire, and it's
    worth being honest about that rather than claiming otherwise:
    `asyncio.Lock` is a fair lock, granting access to queued waiters in
    FIFO order, and `.locked()` can read `False` for a moment after
    `release()` while an already-queued waiter hasn't yet resumed and
    re-locked it (`release()` flips the internal flag synchronously; the
    woken waiter's own coroutine resumes on a later iteration of the
    event loop). If this function's own `.locked()` check happens to land
    in exactly that window, `_run_lock.acquire()` below still takes the
    fast path only when *no* waiter is already queued; otherwise it
    waits its turn behind that waiter, same as any other `acquire()`
    would. In practice that's a handful of event-loop iterations, not a
    multi-minute collection pass, so it doesn't change the shape of what this
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
