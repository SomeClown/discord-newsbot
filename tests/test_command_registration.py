"""Tests that the command tree is built the way SPEC-DEV says it should be.

Per plan section 5, this stays at the "cheap object-level checks" level:
building `app_commands.Group` objects and reading their metadata (option
names, choices, bounds, `default_permissions`) is plain object
construction that discord.py supports with no gateway connection, no
event loop, and no bot token. It's not the same thing as *syncing* those
commands to Discord, which is exactly the part this suite doesn't try to
cover.
"""

from __future__ import annotations

import discord
import pytest
from pydantic import SecretStr

from newsbot.bot.client import NewsBot
from newsbot.bot.commands import make_admin_group, make_news_group
from newsbot.config import Secrets, load_config

FIXTURE = "tests/fixtures/config_valid.yaml"


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    return load_config(FIXTURE)


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _command(group: discord.app_commands.Group, name: str) -> discord.app_commands.Command:
    return next(c for c in group.commands if c.name == name)


def _param(command: discord.app_commands.Command, name: str) -> discord.app_commands.Parameter:
    return next(p for p in command.parameters if p.name == name)


# --- /news recent ---


def test_recent_game_choices_include_every_topic_and_all(cfg):
    group = make_news_group(cfg, ":memory:")
    game = _param(_command(group, "recent"), "game")
    values = [c.value for c in game.choices]
    assert values == ["borderlands4", "palworld", "diablo4", "all"]
    assert [c.name for c in game.choices][-1] == "All"


def test_recent_choices_stay_within_discords_25_choice_limit(cfg):
    # config_valid.yaml has 3 topics; the real limit that matters is
    # config.py's _MAX_TOPICS = 24 (25 minus the "All" choice), checked
    # here as "whatever config.py allowed through actually fits."
    group = make_news_group(cfg, ":memory:")
    game = _param(_command(group, "recent"), "game")
    assert len(game.choices) <= 25


def test_recent_label_choices_match_the_three_labels(cfg):
    group = make_news_group(cfg, ":memory:")
    label = _param(_command(group, "recent"), "label")
    assert [c.value for c in label.choices] == ["official", "reported", "rumor"]


def test_recent_days_option_bounds_are_one_to_thirty(cfg):
    group = make_news_group(cfg, ":memory:")
    days = _param(_command(group, "recent"), "days")
    assert days.min_value == 1
    assert days.max_value == 30


# --- /news search ---


def test_search_query_option_length_bounds_are_one_to_a_hundred(cfg):
    group = make_news_group(cfg, ":memory:")
    query = _param(_command(group, "search"), "query")
    assert query.min_value == 1
    assert query.max_value == 100


def test_search_days_option_bounds_are_one_to_thirty(cfg):
    group = make_news_group(cfg, ":memory:")
    days = _param(_command(group, "search"), "days")
    assert days.min_value == 1
    assert days.max_value == 30


# --- /newsbot admin group ---


def test_admin_group_has_default_permissions_set(cfg):
    class _FakeBot:
        db_path = ":memory:"

    group = make_admin_group(cfg, _FakeBot())
    assert group.default_permissions is not None
    assert group.default_permissions.manage_guild is True


def test_admin_group_default_permissions_follow_configured_admin_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    config_text = """
guild_id: 1
admin_permission: kick_members
digest:
  channel_id: 2
  time: "09:00"
  timezone: "America/Los_Angeles"
topics:
  - key: palworld
    name: "Palworld"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text)
    cfg = load_config(config_path)

    class _FakeBot:
        db_path = ":memory:"

    group = make_admin_group(cfg, _FakeBot())
    assert group.default_permissions.kick_members is True
    assert group.default_permissions.manage_guild is False


def test_admin_group_has_status_run_now_and_preview_commands(cfg):
    class _FakeBot:
        db_path = ":memory:"

    group = make_admin_group(cfg, _FakeBot())
    assert {c.name for c in group.commands} == {"status", "run-now", "preview"}


# --- NewsBot construction ---


def test_bot_constructs_with_allowed_mentions_none(cfg):
    # AllowedMentions doesn't define __eq__, so this compares the fields
    # that actually matter: nothing the digest posts should ever be able
    # to ping @everyone, a role, or an arbitrary user.
    bot = NewsBot(cfg, _secrets(), ":memory:")
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False
    assert bot.allowed_mentions.replied_user is False


def test_bot_constructs_with_default_non_privileged_intents(cfg):
    # The three privileged intents (members, presences, message_content)
    # all require an approved application in the Discord dev portal --
    # this bot doesn't use any of them, and shouldn't accidentally start
    # asking for one.
    bot = NewsBot(cfg, _secrets(), ":memory:")
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.message_content is False
    assert bot.intents == discord.Intents.default()


# --- /newsbot test-alert (plan step 10): registered only when allow_test_command ---


class _FakeBot:
    db_path = ":memory:"


def _cfg_with_test_alert(tmp_path, monkeypatch, *, allow_test_command: bool):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    config_text = f"""
guild_id: 1
digest:
  channel_id: 2
  time: "09:00"
  timezone: "America/Los_Angeles"
topics:
  - key: palworld
    name: "Palworld"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
alerts:
  enabled: true
  allow_test_command: {"true" if allow_test_command else "false"}
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text)
    return load_config(config_path)


def test_test_alert_registered_when_allow_test_command_true(tmp_path, monkeypatch):
    cfg = _cfg_with_test_alert(tmp_path, monkeypatch, allow_test_command=True)
    group = make_admin_group(cfg, _FakeBot())
    assert {c.name for c in group.commands} == {"status", "run-now", "preview", "test-alert"}


def test_test_alert_absent_when_allow_test_command_false(tmp_path, monkeypatch):
    cfg = _cfg_with_test_alert(tmp_path, monkeypatch, allow_test_command=False)
    group = make_admin_group(cfg, _FakeBot())
    assert {c.name for c in group.commands} == {"status", "run-now", "preview"}


def test_test_alert_absent_by_default(cfg):
    # The fixture config carries no alerts: block at all, which defaults
    # allow_test_command to False -- the same "never surprise a prod
    # config" default AlertsCfg documents for the whole feature.
    group = make_admin_group(cfg, _FakeBot())
    assert "test-alert" not in {c.name for c in group.commands}


def test_test_alert_code_option_is_exactly_29_characters(tmp_path, monkeypatch):
    cfg = _cfg_with_test_alert(tmp_path, monkeypatch, allow_test_command=True)
    group = make_admin_group(cfg, _FakeBot())
    code = _param(_command(group, "test-alert"), "code")
    assert code.min_value == 29
    assert code.max_value == 29


def test_test_alert_golden_option_defaults_false(tmp_path, monkeypatch):
    cfg = _cfg_with_test_alert(tmp_path, monkeypatch, allow_test_command=True)
    group = make_admin_group(cfg, _FakeBot())
    golden = _param(_command(group, "test-alert"), "golden")
    assert golden.default is False
