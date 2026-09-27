"""Adversarial tests for `topics[].channel_id`/`alerts.channel_id`, beyond
test_config_channel_ids.py's own missing-field/zero/negative coverage.

Angle (test-engineer brief, 2026-09-26): what a `channel_id` field actually
accepts type-wise (pydantic coerces more than a snowflake-shaped int), and
three "is this allowed?" questions the plan/owner decisions answer but
nothing pins in a test yet -- duplicate channel ids across topics (§5
"Topics sharing a channel: allowed"), `alerts.channel_id` equal to a topic's
channel (same decision, no cross-field validation exists), and
`digest.channel_id` still being present while a topic is missing its own id
(step 1: `digest.channel_id` is optional-but-present is not a substitute for
a topic's own required field).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import ConfigError, load_config

VALID_SRC = """
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""


def _load_with(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


# --- type coercion on channel_id: pydantic's int coercion is looser than "a snowflake" ---


def test_channel_id_as_quoted_string_is_coerced_to_int(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: "123456789"
{VALID_SRC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.topics[0].channel_id == 123456789
    assert isinstance(cfg.topics[0].channel_id, int)


def test_channel_id_as_whole_number_float_is_coerced_to_int(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: 123.0
{VALID_SRC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.topics[0].channel_id == 123


def test_channel_id_as_fractional_float_is_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: 123.7
{VALID_SRC}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_channel_id_larger_than_a_63_bit_signed_int_is_accepted(tmp_path):
    # gt=0 is the only bound on channel_id; nothing caps it at a plausible
    # Discord snowflake width (current real snowflakes fit well under
    # 2**63). Pinning today's permissive behavior, not endorsing it.
    huge = 2**63 + 1
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: {huge}
{VALID_SRC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.topics[0].channel_id == huge


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: pydantic's int coercion accepts bool for an int field (True -> 1, "
        "False -> 0), so a `channel_id: true` typo in config.yaml silently loads "
        "as channel_id=1 instead of failing config validation -- Topic.channel_id "
        "needs strict=True (or an explicit bool rejection) to catch this at load "
        "time instead of at the first confusing Discord API error."
    ),
)
def test_channel_id_as_bool_is_rejected_not_silently_coerced_to_one(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: true
{VALID_SRC}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


# --- "is this allowed?" cross-field questions the plan/owner decisions settle ---


def test_duplicate_channel_ids_across_topics_are_allowed(tmp_path):
    # Owner decision (plan §5): "Topics sharing a channel: allowed (no
    # validation)." Pinning that load_config doesn't second-guess it.
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: 5
  - key: borderlands4
    name: Borderlands 4
    channel_id: 5
sources:
  - type: steam_news
    name: "Palworld Steam"
    app_id: 1623730
    topics: [palworld]
    trust: official
"""
    cfg = _load_with(tmp_path, text)
    assert [t.channel_id for t in cfg.topics] == [5, 5]


def test_alerts_channel_id_equal_to_a_topic_channel_id_is_allowed(tmp_path):
    # No validation cross-checks alerts.channel_id against topics[].channel_id
    # either -- same "no validation" decision extends here since nothing in
    # the plan singles this combination out as an error.
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: 5
{VALID_SRC}
alerts:
  enabled: true
  channel_id: 5
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.channel_id == cfg.topics[0].channel_id == 5


def test_digest_channel_id_present_does_not_excuse_a_missing_topic_channel_id(tmp_path):
    # A leftover v1 digest.channel_id in the config doesn't fall back to
    # cover a topic that never got its own channel_id -- step 1's message
    # still fires, naming the topic, not the digest block.
    text = f"""
guild_id: 1
digest:
  channel_id: 99
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
{VALID_SRC}
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    message = str(exc_info.value)
    assert "topics[0] (palworld): channel_id is required" in message


def test_empty_topics_list_loads_with_no_topics_and_no_error(tmp_path):
    # Nothing in load_config requires at least one topic; an owner who
    # emptied the list (accidentally or deliberately) gets a bot with
    # nothing to post, not a config error pointing at a rule that doesn't
    # exist.
    text = """
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
topics: []
sources: []
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.topics == []
