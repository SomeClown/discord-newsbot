"""Tests for `alerts.topics` scoping (A6, plan step 5b).

Kept in its own file rather than folded into `test_config.py` -- that file
already covers the `alerts:` block's other fields (plan step 1) and is
under separate development in parallel with this one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import ConfigError, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"


def test_alerts_topics_defaults_to_empty_list(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    assert cfg.alerts.topics == []


def test_alerts_topics_accepts_known_topic_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    path = tmp_path / "config.yaml"
    path.write_text(
        FIXTURE.read_text()
        + """
alerts:
  topics: [borderlands4]
"""
    )
    cfg = load_config(path)
    assert cfg.alerts.topics == ["borderlands4"]


def test_alerts_topics_rejects_unknown_topic_key(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    path = tmp_path / "config.yaml"
    path.write_text(
        FIXTURE.read_text()
        + """
alerts:
  topics: [not_a_real_topic]
"""
    )
    with pytest.raises(ConfigError, match="alerts.topics references unknown topic"):
        load_config(path)


def test_alerts_topics_empty_list_means_all_topics(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    path = tmp_path / "config.yaml"
    path.write_text(
        FIXTURE.read_text()
        + """
alerts:
  topics: []
"""
    )
    cfg = load_config(path)
    assert cfg.alerts.topics == []
