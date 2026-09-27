"""One narrow gap next to test_config.py's `test_minimal_config_loads` and
`test_example_config_loads`: those pin that both files load and pin
`alerts.enabled is False` for the minimal one, but neither asserts a topic
actually carries a `channel_id` (v2.0's whole point -- there's no shared
digest channel anymore). `Topic.channel_id` is a required field, so a
missing one would already fail to load; this makes that guarantee explicit
instead of implicit in "the file loaded at all."
"""

from __future__ import annotations

from pathlib import Path

from newsbot.config import load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
MINIMAL = Path(__file__).parent.parent / "config.minimal.yaml"


def test_minimal_config_topic_has_a_channel_id(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(MINIMAL)
    assert len(cfg.topics) >= 1
    for topic in cfg.topics:
        assert isinstance(topic.channel_id, int)
        assert topic.channel_id > 0


def test_example_config_every_topic_has_a_channel_id(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(EXAMPLE)
    assert len(cfg.topics) >= 1
    for topic in cfg.topics:
        assert isinstance(topic.channel_id, int)
        assert topic.channel_id > 0
