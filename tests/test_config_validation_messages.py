"""Tests for the content of `ConfigError` messages, not just that one gets raised.

test_config.py already proves each validator fires. What it doesn't check
is the promise in `load_config`'s docstring: every problem gets listed, not
just the first one found, so fixing a config file isn't a game of
whack-a-mole against a stack trace. These tests configure more than one
failure at once and check the message actually names all of them, plus that
a couple of individual messages contain the offending value (so a report
like "topic key is invalid" isn't the whole story).
"""

from pathlib import Path

import pytest

from newsbot.config import ConfigError, load_config


def _load_with(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def test_multiple_failures_are_all_listed_together(tmp_path):
    text = """
guild_id: 1
admin_permission: not_a_real_permission
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: "Not Valid!"
    name: "Bad Topic"
  - key: "Not Valid!"
    name: "Duplicate Of Bad Topic"
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1
    topics: [not_a_real_topic]
    trust: official
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)

    message = str(exc_info.value)
    # Four independent problems planted above: a bad admin_permission, a
    # topic key that doesn't match the identifier pattern, that same key
    # duplicated, and a source referencing a topic that doesn't exist.
    # Pydantic-level and post-pydantic checks both have to survive into one
    # combined message for this to be more than a single error report.
    assert "admin_permission" in message
    assert "not_a_real_permission" in message
    assert "Not Valid!" in message
    assert "duplicate topic key" in message
    assert "not_a_real_topic" in message
    assert message.count("\n  - ") >= 4


def test_too_many_topics_message_names_the_limit_and_actual_count(tmp_path):
    topics = "\n".join(f'  - key: "topic{i}"\n    name: "Topic {i}"' for i in range(26))
    sources = "\n".join(
        f'  - type: steam_news\n    name: "Source {i}"\n    app_id: {i}\n    '
        f"topics: [topic{i}]\n    trust: official"
        for i in range(26)
    )
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
{topics}
sources:
{sources}
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert "26 topics exceeds the limit of 24" in message


def test_pydantic_level_error_message_names_the_field_path(tmp_path):
    """A pydantic type error (missing required field) still gets folded
    into the same "Invalid config" format."""
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
    topics: [palworld]
    trust: official
"""
    # app_id is required on SteamSource and missing here.
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert "Invalid config:" in message
    assert "app_id" in message
