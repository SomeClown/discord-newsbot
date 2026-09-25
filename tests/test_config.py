"""Tests for newsbot.config: loading config.yaml and secrets from the env."""

from pathlib import Path

import pytest

from newsbot.config import (
    BlueskySource,
    ConfigError,
    RssSource,
    Secrets,
    SteamSource,
    Topic,
    WebSearchSource,
    configured_source_names,
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


# --- configured_source_names ---


def test_configured_source_names_includes_every_source_type(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    names = configured_source_names(cfg)
    assert names == {
        "Blizzard News",  # rss, explicit name
        "Palworld Steam",  # steam_news, explicit name
        "Bluesky: Palworld",  # bluesky_search, default name from the query
        "Brave Search",  # web_search, default name
    }


def test_configured_source_names_matches_what_collectors_actually_record(monkeypatch):
    # The whole point of this helper is that it can't drift from what
    # build_collectors wires up -- every Collector sets `self.name =
    # source.name`, so these two sets have to be exactly equal.
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="anthropic-key",
        brave_api_key="brave-key",
        bluesky_handle=None,
        bluesky_app_password=None,
    )

    from newsbot.collectors.base import build_collectors

    collectors = build_collectors(cfg, secrets)

    assert configured_source_names(cfg) == {c.name for c in collectors}


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


def test_admin_permission_value_is_rejected(tmp_path):
    # "value", "all", "none", etc. are real attributes on
    # discord.Permissions (a property and two classmethods), but none of
    # them name an actual permission flag -- hasattr() alone can't tell
    # the difference, which used to let "value" (and friends) sail
    # through as a configured admin_permission that then has no bit to
    # check against a real user's permissions.
    text = f"""
guild_id: 1
admin_permission: value
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_admin_permission_all_classmethod_is_rejected(tmp_path):
    # "all" and "none" are classmethods on discord.Permissions, same
    # shape of false-positive as "value" -- hasattr() would have called
    # either "a real flag" too.
    text = f"""
guild_id: 1
admin_permission: all
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_admin_permission_none_classmethod_is_rejected(tmp_path):
    text = f"""
guild_id: 1
admin_permission: none
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_every_real_permission_flag_name_is_accepted(tmp_path):
    # The flip side of the "value"/"all"/"none" false-positive cases:
    # every name VALID_FLAGS actually considers a real permission bit
    # must still load cleanly -- the fix shouldn't have narrowed the
    # accepted set to less than what it's supposed to be.
    import discord

    for flag_name in discord.Permissions.VALID_FLAGS:
        text = f"""
guild_id: 1
admin_permission: {flag_name}
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
        cfg = _load_with(tmp_path, text)
        assert cfg.admin_permission == flag_name


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


def test_example_config_alerts_block_is_commented_out_and_reads_as_default(monkeypatch):
    # The entire `alerts:` block in config.example.yaml is commented out
    # (plan step 11: shown, not enabled, since it names a real
    # @everyone-capable feature) -- loading the example file as-is should
    # produce exactly AlertsCfg()'s untouched defaults, not whatever the
    # commented-out values happen to say.
    from newsbot.config import AlertsCfg

    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(EXAMPLE)
    assert cfg.alerts == AlertsCfg()
    assert cfg.alerts.enabled is False


def test_example_config_alerts_comment_documents_the_same_defaults_as_the_code(monkeypatch):
    # The example file's comment block (interval_minutes: 60,
    # max_item_age_hours: 48, max_pings_per_day: 3, allow_test_command:
    # false) is meant to describe AlertsCfg's real defaults for an owner
    # who's about to uncomment it -- if config.py's defaults ever drift
    # from that comment, this catches the documentation going stale
    # rather than an owner finding out by uncommenting a wrong number.
    from newsbot.config import AlertsCfg

    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    example_text = EXAMPLE.read_text()
    alerts_comment_lines = [
        line
        for line in example_text.splitlines()
        if line.strip().startswith("#") and "alerts" not in line.lower()
    ]
    commented_block = "\n".join(alerts_comment_lines)
    defaults = AlertsCfg()
    assert f"interval_minutes: {defaults.interval_minutes}" in commented_block
    assert f"max_item_age_hours: {defaults.max_item_age_hours}" in commented_block
    assert f"max_pings_per_day: {defaults.max_pings_per_day}" in commented_block
    assert f"allow_test_command: {str(defaults.allow_test_command).lower()}" in commented_block


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


# --- alerts: config block (v1.2 SHiFT code alerts, plan step 1) ---


def test_alerts_missing_block_gives_disabled_defaults(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.enabled is False
    assert cfg.alerts.interval_minutes == 60
    assert cfg.alerts.max_item_age_hours == 48
    assert cfg.alerts.max_pings_per_day == 3
    assert cfg.alerts.allow_test_command is False


def test_alerts_full_block_parses(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  interval_minutes: 30
  max_item_age_hours: 24
  max_pings_per_day: 5
  allow_test_command: true
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.enabled is True
    assert cfg.alerts.interval_minutes == 30
    assert cfg.alerts.max_item_age_hours == 24
    assert cfg.alerts.max_pings_per_day == 5
    assert cfg.alerts.allow_test_command is True


def test_alerts_unknown_key_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  role_id: 12345
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


@pytest.mark.parametrize(
    "field, value",
    [
        ("interval_minutes", 14),
        ("interval_minutes", 1441),
        ("max_item_age_hours", 0),
        ("max_item_age_hours", 721),
        ("max_pings_per_day", -1),
    ],
)
def test_alerts_out_of_bounds_values_rejected(tmp_path, field, value):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  {field}: {value}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_alerts_bounds_are_inclusive(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  interval_minutes: 15
  max_item_age_hours: 1
  max_pings_per_day: 0
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.interval_minutes == 15
    assert cfg.alerts.max_item_age_hours == 1
    assert cfg.alerts.max_pings_per_day == 0


def test_alerts_max_item_age_hours_upper_bound_is_inclusive_at_720(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  max_item_age_hours: 720
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.max_item_age_hours == 720


def test_allow_test_command_true_with_enabled_false_is_a_config_error(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: false
  allow_test_command: true
"""
    with pytest.raises(ConfigError, match="allow_test_command"):
        _load_with(tmp_path, text)


def test_allow_test_command_true_with_enabled_true_is_fine(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  allow_test_command: true
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.allow_test_command is True


def test_allow_test_command_false_with_enabled_false_is_fine(tmp_path):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: false
  allow_test_command: false
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.alerts.allow_test_command is False


def test_alerts_allow_test_command_true_logs_a_warning(tmp_path, caplog):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  allow_test_command: true
"""
    with caplog.at_level("WARNING"):
        _load_with(tmp_path, text)
    assert any("allow_test_command" in record.message for record in caplog.records)


def test_alerts_allow_test_command_false_logs_no_warning(tmp_path, caplog):
    text = f"""
guild_id: 1
digest:
  channel_id: 1
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  allow_test_command: false
"""
    with caplog.at_level("WARNING"):
        _load_with(tmp_path, text)
    assert not any("allow_test_command" in record.message for record in caplog.records)
