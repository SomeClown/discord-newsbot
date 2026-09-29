"""`python -m newsbot` and the Server Members intent (design.md §14).

Welcomes need a privileged intent. If the Developer Portal switch is off,
discord.py raises `PrivilegedIntentsRequired` out of `bot.run()` after the
gateway has already said no. The entrypoint's job is to turn that into one
readable line and exit 2, instead of a traceback that reads like the bot
is broken. Nothing here touches a network: `NewsBot` is a stub whose `run`
raises (or doesn't).
"""

from __future__ import annotations

from pathlib import Path

import discord
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
