"""Tests for `async_watchdog.py`, which fails a stuck async test instead of stalling the run.

The test runs the watchdog in a throwaway pytest session (the same trick as
`test_network_guard_adversarial.py`) with the limit cut to a fraction of a second, so the
"hang" costs a quarter of a second and not half a minute.
"""

from __future__ import annotations

import async_watchdog

_INI = """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
"""

_ARGS = ("-p", "no:cacheprovider", "-W", "ignore::pytest.PytestAssertRewriteWarning")

_TESTS = """
import asyncio


async def never_answered():
    await asyncio.get_running_loop().create_future()


async def test_waits_on_a_future_nobody_resolves():
    await asyncio.gather(never_answered(), asyncio.sleep(0))


async def test_finishes_in_time():
    await asyncio.sleep(0)


def test_plain_sync_test():
    assert True
"""


def _run(pytester, monkeypatch):
    monkeypatch.setattr(async_watchdog, "ASYNC_TEST_LIMIT_S", 0.25)
    pytester.makeini(_INI)
    pytester.makeconftest('pytest_plugins = ("async_watchdog",)')
    pytester.makepyfile(_TESTS)
    return pytester.runpytest_inprocess(*_ARGS)


def test_a_stuck_async_test_fails_and_names_the_await_it_was_stuck_on(pytester, monkeypatch):
    result = _run(pytester, monkeypatch)
    # The other two (one async, one not) pass untouched.
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*test_waits_on_a_future_nobody_resolves was still waiting after 0.25s*",
            "*in never_answered*",
            "*await asyncio.get_running_loop().create_future()*",
        ]
    )
