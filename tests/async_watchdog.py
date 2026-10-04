"""The suite's stuck-test alarm: an async test that sits still too long fails, with its task stacks.

A hung async test is one of the less helpful things a test run can do. The event loop parks
in `selectors.select` with nothing scheduled, CPU drops to zero, and pytest's own
`faulthandler_timeout` dumps the threads, which shows the main thread waiting in `select`
and not one word about which `await` never came back. (I know because that's the whole
trace I had for the quote-now race in `test_commands_quote_now_adversarial.py`.)
`pytest-timeout` would kill the run sooner, but it shows the same `select` frame.

So every async test gets a timer on its own loop. If the test is still going after
`ASYNC_TEST_LIMIT_S`, the timer prints every pending task's stack (the bit that names the
stuck `await`), cancels the test, and fails it with that dump as the reason. The loop being
idle is exactly when a timer on it can still fire, so this catches the "awaited a future
nobody will ever resolve" kind of hang. It doesn't catch a test stuck in synchronous code
(the loop never gets back round to the timer), which `faulthandler_timeout` in
`pyproject.toml` is there for, or a hang inside an async fixture, which runs before the timer
starts.

It lives in its own module, like `network_guard.py`, and `conftest.py` loads it as a plugin.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import io

import pytest

# Past the slowest async test (the 300-server load tests, about 7 seconds on a laptop and
# about 1.4 times that on CI) and well short of "someone notices the run is stuck and
# goes looking". If a legitimately slow test trips it, raise this; don't mark the test.
ASYNC_TEST_LIMIT_S = 30.0


def _task_stacks(loop: asyncio.AbstractEventLoop) -> str:
    out = io.StringIO()
    for task in asyncio.all_tasks(loop):
        out.write(f"\n{task!r}\n")
        task.print_stack(limit=30, file=out)
    return out.getvalue()


def _watched(test, nodeid: str, limit: float):
    @functools.wraps(test)
    async def run(*args, **kwargs):
        loop = asyncio.get_running_loop()
        test_task = asyncio.current_task()
        stacks: list[str] = []

        def stuck() -> None:
            stacks.append(_task_stacks(loop))
            test_task.cancel()

        timer = loop.call_later(limit, stuck)
        try:
            return await test(*args, **kwargs)
        except asyncio.CancelledError:
            if not stacks:
                raise
        finally:
            timer.cancel()
        # Out here and not in the `except`, so the report doesn't open with a
        # CancelledError traceback that's only there because we did the cancelling.
        pytest.fail(
            f"{nodeid} was still waiting after {limit:g}s. The pending tasks, "
            f"whose last frames are the awaits that never finished:\n{stacks[0]}",
            pytrace=False,
        )

    return run


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    test = getattr(item, "obj", None)
    if not inspect.iscoroutinefunction(test):
        return (yield)
    # pytest-asyncio reads `item.obj` when the test runs, so swapping it here wraps the
    # test body inside the loop pytest-asyncio is about to run it on.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(item, "obj", _watched(test, item.nodeid, ASYNC_TEST_LIMIT_S))
        return (yield)
