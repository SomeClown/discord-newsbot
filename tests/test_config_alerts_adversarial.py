"""Adversarial tests for the `alerts:` config block, beyond the implementer's
own bounds/unknown-key/warning coverage in test_config.py.

Two shapes the implementer's suite didn't try: an explicitly empty block
(`alerts: {}`) and an explicitly null one (`alerts:` with nothing after the
colon). Both are plausible fat-fingers in a hand-edited YAML file, and
whichever way `load_config` resolves them needs to be a deliberate,
pinned decision rather than whatever pydantic happens to do by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import ConfigError, load_config

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


def _load_with(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def test_alerts_explicitly_empty_block_gives_disabled_defaults(tmp_path):
    # `alerts: {}` is distinct from the block being absent entirely (that's
    # test_config.py's test_alerts_missing_block_gives_disabled_defaults),
    # but pydantic treats "no keys provided" the same way either path: every
    # field falls back to its own default.
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts: {{}}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.enabled is False
    assert cfg.alerts.interval_minutes == 60
    assert cfg.alerts.max_item_age_hours == 48
    assert cfg.alerts.max_pings_per_day == 3
    assert cfg.alerts.allow_test_command is False


def test_alerts_explicit_null_is_rejected_not_silently_defaulted(tmp_path):
    # `alerts:` with nothing after the colon parses as YAML null, which is
    # not the same thing as the key being missing -- AppConfig.alerts is
    # typed as AlertsCfg (not AlertsCfg | None), so an explicit null fails
    # pydantic's type check rather than falling back to the field default.
    # Pinning this as the decided (and safer) behavior: a config that
    # explicitly says "alerts: <nothing>" is a typo worth refusing to
    # start over, not a synonym for "alerts: {}" that silently disables
    # the feature the same way an absent block would.
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
"""
    with pytest.raises(ConfigError, match="alerts"):
        _load_with(tmp_path, text)


def test_alerts_null_explicit_key_with_other_keys_present_still_rejected(tmp_path):
    # Same case with a trailing "null" spelled out explicitly, in case a
    # template ever gets generated that way instead of a bare colon.
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts: null
"""
    with pytest.raises(ConfigError, match="alerts"):
        _load_with(tmp_path, text)
