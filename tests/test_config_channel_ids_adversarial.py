"""Adversarial tests for `topics[].channel_id`/`alerts.channel_id`, beyond
test_config_channel_ids.py's own missing-field/zero/negative coverage.

Angle (test-engineer brief, 2026-09-26): what a `channel_id` field actually
accepts type-wise (pydantic coerces more than a snowflake-shaped int), and
three "is this allowed?" questions the plan/owner decisions answer but
nothing pins in a test yet -- duplicate channel ids across topics (§5
"Topics sharing a channel: allowed"), `alerts.channel_id` equal to a topic's
channel (same decision, no cross-field validation exists), and
`digest.channel_id` still being present while a topic is missing its own id
(step 4: `digest.channel_id`'s removal message and a topic's own missing-id
message both fire together, not one instead of the other).
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: "123456789"
{VALID_SRC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.legacy.games[0][1] == 123456789
    assert isinstance(cfg.legacy.games[0][1], int)


def test_channel_id_as_whole_number_float_is_coerced_to_int(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: 123.0
{VALID_SRC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.legacy.games[0][1] == 123


def test_channel_id_as_fractional_float_is_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: {huge}
{VALID_SRC}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.legacy.games[0][1] == huge


def test_channel_id_as_bool_is_rejected_not_silently_coerced_to_one(tmp_path):
    # Was a bug: pydantic's int coercion accepts bool for an int field
    # (True -> 1, False -> 0), so a `channel_id: true` typo in config.yaml
    # silently loaded as channel_id=1 instead of failing config validation.
    # Topic.channel_id and AlertsCfg.channel_id both now reject bool
    # explicitly (config.py's `_reject_bool_channel_id`), ahead of
    # pydantic's normal coercion, without giving up the string/whole-float
    # coercions the tests above this one still pin as intentional.
    text = f"""
guild_id: 1
digest:
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
    assert [channel for _key, channel in cfg.legacy.games] == [5, 5]


def test_alerts_channel_id_equal_to_a_topic_channel_id_is_allowed(tmp_path):
    # No validation cross-checks alerts.channel_id against topics[].channel_id
    # either: same "no validation" decision extends here since nothing in
    # the plan singles this combination out as an error.
    text = f"""
guild_id: 1
digest:
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
    assert cfg.legacy.shift_channel_id == cfg.legacy.games[0][1] == 5


def test_leftover_digest_channel_id_does_not_excuse_a_missing_topic_channel_id(tmp_path):
    # A leftover v1 digest.channel_id in the config doesn't fall back to
    # cover a topic that never got its own channel_id: both the step 4
    # removal message and the step 1 missing-channel-id message fire
    # together, naming the topic, not just the digest block.
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
    assert "digest.channel_id was removed in v2.0" in message
    assert "topics[0] (palworld): channel_id is required" in message


def test_digest_channel_id_removal_message_matches_the_plans_exact_wording(tmp_path):
    # Plan step 4 specifies this message verbatim; pinned exactly (not just
    # a substring) so a future edit to the wording is a deliberate change
    # to this test, not an accidental drift nobody notices.
    text = f"""
guild_id: 1
digest:
  channel_id: 99
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: Palworld
    channel_id: 5
{VALID_SRC}
"""
    with pytest.raises(ConfigError) as exc_info:
        _load_with(tmp_path, text)
    assert (
        "digest.channel_id was removed in v2.0: move it to a channel_id "
        "on each topic (topics[].channel_id); there is no fallback"
    ) in str(exc_info.value)


def test_empty_topics_list_loads_with_no_topics_and_no_error(tmp_path):
    # Nothing in load_config requires at least one topic; an owner who
    # emptied the list (accidentally or deliberately) gets a bot with
    # nothing to post, not a config error pointing at a rule that doesn't
    # exist.
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics: []
sources: []
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.legacy.games == [] and cfg.catalog == []
