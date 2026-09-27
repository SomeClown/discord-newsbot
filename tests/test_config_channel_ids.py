"""Tests for the v2.0 per-channel config fields (plan step 1, design.md §13).

`topics[].channel_id` is now required (each game posts to its own channel)
and `alerts.channel_id` is required whenever `alerts.enabled` is true (SHiFT
alerts move to their own channel). `digest.channel_id` itself is gone as of
step 4 -- see `test_config_validation_messages.py` for the removal error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import ConfigError, load_config


def _load_with(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def test_missing_topic_channel_id_gives_a_friendly_message(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
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
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert (
        "topics[0] (palworld): channel_id is required -- each game posts "
        "to its own channel as of v2.0" in message
    )


def test_missing_topic_channel_id_names_the_right_topic_among_several(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: borderlands4
    name: "Borderlands 4"
    channel_id: 2
  - key: palworld
    name: "Palworld"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert "topics[1] (palworld): channel_id is required" in message


def test_topic_channel_id_zero_is_rejected(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 0
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_topic_channel_id_negative_is_rejected(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: -1
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


VALID_TOPIC = """
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""


def test_alerts_enabled_without_channel_id_is_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TOPIC}
alerts:
  enabled: true
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert (
        "alerts.channel_id is required when alerts.enabled is true "
        "(v2.0: SHiFT alerts post to their own channel)" in message
    )


def test_alerts_enabled_with_channel_id_is_fine(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TOPIC}
alerts:
  enabled: true
  channel_id: 5
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.channel_id == 5


def test_alerts_disabled_without_channel_id_is_fine(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TOPIC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.enabled is False
    assert cfg.alerts.channel_id is None


def test_alerts_channel_id_zero_is_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TOPIC}
alerts:
  enabled: true
  channel_id: 0
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_multiple_missing_topic_channel_ids_all_listed_together(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: borderlands4
    name: "Borderlands 4"
  - key: palworld
    name: "Palworld"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert "topics[0] (borderlands4): channel_id is required" in message
    assert "topics[1] (palworld): channel_id is required" in message
    assert message.count("\n  - ") >= 2
