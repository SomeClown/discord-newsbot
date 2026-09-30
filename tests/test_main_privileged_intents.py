"""`python -m newsbot` and the Server Members intent (design.md §14).

Welcomes need a privileged intent. If the Developer Portal switch is off,
discord.py raises `PrivilegedIntentsRequired` out of `bot.run()` after the
gateway has already said no. The entrypoint's job is to turn that into one
readable line and exit 2, instead of a traceback that reads like the bot
is broken. Nothing here touches a network: `NewsBot` is a stub whose `run`
raises (or doesn't).
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import discord
import pytest
from pydantic import SecretStr

import newsbot.__main__ as entrypoint
from newsbot.config import Secrets

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"


def _secrets() -> Secrets:
    return Secrets(
        discord_token=SecretStr("not-a-real-token"),
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _wire(monkeypatch, tmp_path, run) -> None:
    class StubBot:
        def __init__(self, cfg, secrets, db_path) -> None:
            pass

        def run(self, token: str, **kwargs) -> None:
            run()

    monkeypatch.setenv("NEWSBOT_CONFIG", str(CONFIG_PATH))
    monkeypatch.setenv("NEWSBOT_DB", str(tmp_path / "newsbot.db"))
    monkeypatch.setattr(entrypoint, "load_secrets", _secrets)
    monkeypatch.setattr(entrypoint, "NewsBot", StubBot)


@pytest.fixture(autouse=True)
def _no_exit_delay(monkeypatch):
    # The real delay is ten minutes; the suite shouldn't feel it.
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0)


def _raise_intents() -> None:
    raise discord.PrivilegedIntentsRequired(None)


def test_privileged_intents_required_exits_2_with_one_line(monkeypatch, tmp_path, capsys):
    def run() -> None:
        raise discord.PrivilegedIntentsRequired(None)

    _wire(monkeypatch, tmp_path, run)

    assert entrypoint.main() == 2

    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1
    assert "Server Members" in err
    assert "Developer Portal" in err
    assert "lounge.welcome.enabled" in err


def test_a_clean_run_still_exits_0(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path, lambda: None)

    assert entrypoint.main() == 0
    assert capsys.readouterr().err == ""


def test_privileged_intents_required_logs_that_it_will_wait(monkeypatch, tmp_path, capsys):
    # capsys, not caplog: main() calls configure_logging, which swaps the root
    # handlers (caplog's included) for one that writes JSON to stdout.
    _wire(monkeypatch, tmp_path, _raise_intents)

    assert entrypoint.main() == 2

    out = capsys.readouterr().out
    assert out.count("Waiting 0 seconds before exiting") == 1


def test_the_delay_is_ten_minutes_by_default(monkeypatch):
    monkeypatch.undo()  # drops the autouse zero
    assert entrypoint.INTENT_EXIT_DELAY_S == 600


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_wait_ends_promptly_on_a_signal_and_restores_the_handlers(monkeypatch, sig):
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 60)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    # `docker stop` is a SIGTERM arriving mid-wait; the timer plays the daemon.
    timer = threading.Timer(0.1, os.kill, (os.getpid(), sig))
    timer.start()
    started = time.monotonic()
    try:
        entrypoint._wait_before_exit()
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
    assert {s: signal.getsignal(s) for s in before} == before


def test_the_wait_actually_waits_when_nothing_interrupts_it(monkeypatch):
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0.2)
    started = time.monotonic()
    entrypoint._wait_before_exit()
    assert time.monotonic() - started >= 0.15


def test_main_returns_2_after_a_signal_cuts_the_wait_short(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _raise_intents)
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 60)
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        assert entrypoint.main() == 2
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10
