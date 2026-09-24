"""Tests for newsbot.config: loading config.yaml and secrets from the env."""

from pathlib import Path

import pytest

from newsbot.config import (
    BlueskySource,
    ConfigError,
    RssSource,
    SteamSource,
    Topic,
    WebSearchSource,
    load_config,
    load_secrets,
)

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_valid_fixture_loads(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    assert cfg.guild_id == 123456789012345678
    assert cfg.digest.timezone == "America/Los_Angeles"
    assert len(cfg.topics) == 3
    assert len(cfg.sources) == 4


def test_source_union_routes_each_type(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    types = {type(s) for s in cfg.sources}
    assert types == {RssSource, SteamSource, BlueskySource, WebSearchSource}


def _load_with(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


VALID_TAIL = """
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


def test_bad_timezone_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "Not/AZone"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_bad_time_format_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "9am"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_unknown_topic_reference_rejected(tmp_path):
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [not_a_real_topic]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_duplicate_source_names_rejected(tmp_path):
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
sources:
  - type: steam_news
    name: "Dupe"
    app_id: 1
    topics: [palworld]
    trust: official
  - type: steam_news
    name: "Dupe"
    app_id: 2
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_bad_admin_permission_rejected(tmp_path):
    text = f"""
guild_id: 1
admin_permission: not_a_real_permission
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_duplicate_topic_keys_rejected(tmp_path):
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
  - key: palworld
    name: "Palworld Again"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_bad_topic_key_format_rejected(tmp_path):
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: "Not Valid!"
    name: "Palworld"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1
    topics: ["Not Valid!"]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_bluesky_source_default_name(tmp_path):
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
sources:
  - type: bluesky_search
    query: "Palworld"
    topics: [palworld]
    trust: community
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.sources[0].name == "Bluesky: Palworld"


def test_web_search_without_brave_key_disables_source(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
  - type: web_search
    queries_per_topic: 2
    trust: press
"""
    cfg = _load_with(tmp_path, text)
    # web_search source is dropped when BRAVE_API_KEY isn't set.
    assert all(s.type != "web_search" for s in cfg.sources)


def test_example_config_loads(monkeypatch):
    # config.example.yaml is what the owner copies to config.yaml on day one;
    # if this doesn't load, the README's setup instructions are lying.
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(EXAMPLE)
    assert {t.key for t in cfg.topics} == {"borderlands4", "palworld", "diablo4"}


def test_topic_search_queries_default_empty():
    topic = Topic(key="palworld", name="Palworld")
    assert topic.search_queries == []


def test_topic_search_queries_accepts_list():
    topic = Topic(key="borderlands4", name="Borderlands 4", search_queries=["Borderlands 4 news"])
    assert topic.search_queries == ["Borderlands 4 news"]


def test_topic_search_queries_rejects_blank_entries(tmp_path):
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    search_queries: ["Palworld news", "   "]
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_load_secrets_reads_env():
    secrets = load_secrets(
        {
            "DISCORD_TOKEN": "d-token",
            "ANTHROPIC_API_KEY": "a-key",
            "BRAVE_API_KEY": "b-key",
        }
    )
    assert secrets.discord_token.get_secret_value() == "d-token"
    assert secrets.anthropic_api_key.get_secret_value() == "a-key"
    assert secrets.brave_api_key.get_secret_value() == "b-key"
    assert secrets.bluesky_handle is None


def test_load_secrets_requires_discord_token_by_default():
    with pytest.raises(ConfigError):
        load_secrets({"ANTHROPIC_API_KEY": "a-key"})


def test_load_secrets_can_skip_discord_requirement():
    secrets = load_secrets({"ANTHROPIC_API_KEY": "a-key"}, require_discord=False)
    assert secrets.discord_token is None


def test_secrets_never_leak_in_repr():
    secrets = load_secrets(
        {
            "DISCORD_TOKEN": "super-secret-token",
            "ANTHROPIC_API_KEY": "super-secret-key",
            "BRAVE_API_KEY": "super-secret-brave",
        }
    )
    text = repr(secrets)
    assert "super-secret-token" not in text
    assert "super-secret-key" not in text
    assert "super-secret-brave" not in text


def test_bluesky_handle_leading_at_is_stripped():
    from newsbot.config import load_secrets

    env = {"ANTHROPIC_API_KEY": "x", "BLUESKY_HANDLE": "@someone.bsky.social"}
    assert load_secrets(env, require_discord=False).bluesky_handle == "someone.bsky.social"
