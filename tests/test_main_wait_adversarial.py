"""Adversarial tests for `_wait_before_exit`, beyond `test_main_privileged_intents.py`.

The brief (test-engineer, 2026-09-29): the wait exists so a restart loop can't
spend the bot's logins, and it works by swapping the process's SIGTERM and
SIGINT handlers for a minute and then putting them back. Swapping handlers
is the sort of thing that goes wrong exactly once, in production, as PID 1.
So this file checks the boring guarantees: the old handlers come back no
matter how the wait ends, a signal that wakes the wait doesn't also fire the
old handler (it'd kill the process we're trying to exit politely), and the
whole thing stays quiet on stdout, where the JSON logs live.

Nothing here sleeps for real: the delay is patched to a fraction of a second,
and the few real signals are sent by a timer a tenth of a second in. A signal
is never sent while the default handler is installed; the process under test
is pytest, and pytest would very much take the hint.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

import newsbot.__main__ as entrypoint

SIGNALS = (signal.SIGTERM, signal.SIGINT)


@pytest.fixture
def recorder():
    """Install recording handlers for both signals; put the originals back afterwards."""
    seen: list[int] = []

    def handler(signum: int, frame: object) -> None:
        seen.append(signum)

    originals = {s: signal.signal(s, handler) for s in SIGNALS}
    try:
        yield handler, seen
    finally:
        for s, old in originals.items():
            signal.signal(s, old)


def _handlers() -> dict[int, object]:
    return {s: signal.getsignal(s) for s in SIGNALS}


def _send_later(sig: int, after: float = 0.1) -> threading.Timer:
    timer = threading.Timer(after, os.kill, (os.getpid(), sig))
    timer.start()
    return timer


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_signal_wakes_the_wait_and_the_old_handler_is_not_called(monkeypatch, recorder, sig):
    handler, seen = recorder
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 30)
    timer = _send_later(sig)
    started = time.monotonic()
    try:
        entrypoint._wait_before_exit()
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
    assert seen == []
    assert _handlers() == {s: handler for s in SIGNALS}


@pytest.mark.parametrize("sig", SIGNALS)
def test_after_the_wait_the_restored_handler_is_the_one_that_hears_the_next_signal(
    monkeypatch, recorder, sig
):
    _, seen = recorder
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0)
    entrypoint._wait_before_exit()
    os.kill(os.getpid(), sig)
    # CPython runs Python-level handlers between bytecodes on the main thread.
    time.sleep(0.05)
    assert seen == [sig]


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_signal_before_the_wait_starts_goes_to_the_old_handler_and_does_not_shorten_it(
    monkeypatch, recorder, sig
):
    handler, seen = recorder
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0.3)
    os.kill(os.getpid(), sig)
    time.sleep(0.05)
    assert seen == [sig]

    started = time.monotonic()
    entrypoint._wait_before_exit()
    # The early signal was somebody else's business; this wait still waits.
    assert time.monotonic() - started >= 0.25
    assert _handlers() == {s: handler for s in SIGNALS}


def test_a_delay_of_zero_returns_at_once_and_restores_the_handlers(monkeypatch, recorder):
    handler, _ = recorder
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0)
    started = time.monotonic()
    entrypoint._wait_before_exit()
    assert time.monotonic() - started < 1
    assert _handlers() == {s: handler for s in SIGNALS}


def test_a_negative_delay_does_not_raise(monkeypatch, recorder):
    # Event.wait treats a negative timeout as "don't wait". Nobody sets it
    # negative, but a typo in a constant shouldn't turn into a crash loop.
    handler, _ = recorder
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", -5)
    entrypoint._wait_before_exit()
    assert _handlers() == {s: handler for s in SIGNALS}


def test_the_previous_handlers_are_restored_even_if_the_wait_raises(monkeypatch, recorder):
    handler, _ = recorder

    class ExplodingEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout=None) -> bool:
            raise RuntimeError("the wait fell over")

    monkeypatch.setattr(entrypoint.threading, "Event", ExplodingEvent)
    with pytest.raises(RuntimeError, match="fell over"):
        entrypoint._wait_before_exit()
    assert _handlers() == {s: handler for s in SIGNALS}


def test_the_handlers_are_ours_while_waiting(monkeypatch, recorder):
    handler, _ = recorder
    during: dict[int, object] = {}

    class SpyEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout=None) -> bool:
            during.update(_handlers())
            return False

    monkeypatch.setattr(entrypoint.threading, "Event", SpyEvent)
    entrypoint._wait_before_exit()
    assert set(during) == set(SIGNALS)
    assert all(h is not handler and callable(h) for h in during.values())


def test_a_sigint_during_the_wait_does_not_raise_keyboard_interrupt(monkeypatch, recorder):
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 30)
    timer = _send_later(signal.SIGINT)
    try:
        entrypoint._wait_before_exit()  # a KeyboardInterrupt here would fail the test
    finally:
        timer.cancel()


def test_the_wait_writes_nothing_to_stdout_or_stderr(monkeypatch, recorder, capsys):
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0.05)
    entrypoint._wait_before_exit()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_wait_is_a_no_op_for_other_signals(monkeypatch):
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0)
    before = signal.getsignal(signal.SIGHUP)
    entrypoint._wait_before_exit()
    assert signal.getsignal(signal.SIGHUP) == before
