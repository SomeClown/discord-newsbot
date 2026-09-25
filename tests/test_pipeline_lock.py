"""Tests for `pipeline.lock`: the shared run lock, and `run_lock_or_skip`'s honesty about it.

QA item 10: `run_lock_or_skip`'s docstring used to claim it never waits.
`asyncio.Lock` is a *fair* lock -- `.locked()` can read `False` for a
moment after `release()` while an already-queued waiter hasn't yet
resumed and re-locked it, and a `run_lock_or_skip()` call that lands in
exactly that window ends up queued behind that waiter instead of getting
an immediate answer either way. The straightforward cases (lock free,
lock held by something that isn't about to release) still behave exactly
as before; the race test below exists to pin the corrected claim, not to
argue the function is broken.
"""

from __future__ import annotations

import asyncio

from newsbot.pipeline.lock import _run_lock, is_run_in_progress, run_lock_or_skip


async def test_free_lock_is_acquired_immediately():
    async with run_lock_or_skip() as acquired:
        assert acquired is True
        assert is_run_in_progress() is True
    assert is_run_in_progress() is False


async def test_held_lock_skips_without_waiting():
    await _run_lock.acquire()
    try:
        async with run_lock_or_skip() as acquired:
            assert acquired is False
    finally:
        _run_lock.release()


async def test_skip_leaves_the_lock_state_untouched():
    await _run_lock.acquire()
    try:
        async with run_lock_or_skip():
            pass
        # Still held by the original acquirer -- a skip must never have
        # released (or double-acquired) anything.
        assert is_run_in_progress() is True
    finally:
        _run_lock.release()
    assert is_run_in_progress() is False


async def test_run_lock_or_skip_can_wait_behind_an_already_queued_waiter():
    # Deterministic, not flaky: asyncio's cooperative scheduling means
    # each `await asyncio.sleep(0)` below runs exactly the event-loop
    # steps this test needs and no more.
    await _run_lock.acquire()  # the long-running holder
    order: list[str] = []

    async def waiter() -> None:
        async with _run_lock:
            order.append("waiter")

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # let `waiter` reach its own acquire() and queue behind the holder
    assert _run_lock.locked() is True

    _run_lock.release()  # flips the internal flag synchronously; `waiter` hasn't resumed yet
    # `run_lock_or_skip`'s own `.locked()` check can now read False, but
    # `waiter` is still queued -- `_run_lock.acquire()` below has to wait
    # its turn behind it rather than skip *or* return immediately.
    async with run_lock_or_skip() as acquired:
        order.append("skip-caller" if acquired else "skip-caller-skipped")

    await waiter_task
    # The already-queued waiter got the lock first, exactly as a fair
    # lock promises -- run_lock_or_skip's own attempt landed second, and
    # it waited for it rather than racing past it.
    assert order == ["waiter", "skip-caller"]
