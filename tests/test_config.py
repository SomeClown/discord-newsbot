"""Tests for newsbot.config: loading config.yaml and secrets from the env.

Most of these load a v2-shaped file (`guild_id`, `digest`, `topics`, `sources`, `alerts`) and
check what the loader made of it. Since the cutover the loaded `AppConfig` has no v2 fields:
the games and sources are the catalog (and shared sources), the old `alerts:` numbers are
`shift:` and `collection:`, the digest subject is `ai.subject`, and the one server's old
setup is `legacy`. The assertions are the same ones; they read from where the values live now.
"""

from pathlib import Path

import pytest

from newsbot.collectors.base import build_catalog_collectors
from newsbot.config import (
    BlueskySource,
    ConfigError,
    RssSource,
    Secrets,
    ShiftCfg,
    SteamSource,
    Topic,
    load_config,
    load_secrets,
)

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
MINIMAL = Path(__file__).parent.parent / "config.minimal.yaml"


def _all_sources(cfg):
    """Every source the catalog and the shared list carry (web search is its own block)."""
    return [s for g in cfg.catalog for s in g.sources] + list(cfg.shared_sources)


def test_valid_fixture_loads(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    assert cfg.legacy.guild_id == 123456789012345678
    assert cfg.home_guild_id == 123456789012345678
    assert cfg.legacy.timezone == "America/Los_Angeles"
    assert len(cfg.catalog) == 3
    assert len(_all_sources(cfg)) + (cfg.web_search is not None) == 4


def test_source_union_routes_each_type(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    types = {type(s) for s in _all_sources(cfg)}
    assert types == {RssSource, SteamSource, BlueskySource}
    assert cfg.web_search is not None  # the fourth type, now its own block


# --- the collectors' names ---


def test_collector_names_include_every_source_type(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    names = {c.name for c in build_catalog_collectors(cfg, _secrets(brave="brave-key"))}
    assert names == {
        "Blizzard News",  # rss, explicit name
        "Palworld Steam",  # steam_news, explicit name
        "Bluesky: Palworld",  # bluesky_search, default name from the query
        "Brave Search",  # web_search, default name
    }


def _secrets(brave: str | None = None) -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key="anthropic-key",
        brave_api_key=brave,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _load_with(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


VALID_TAIL = """
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


def test_bad_timezone_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
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
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_admin_permission_value_is_rejected(tmp_path):
    # "value", "all", "none", etc. are real attributes on
    # discord.Permissions (a property and two classmethods), but none of
    # them name an actual permission flag: hasattr() alone can't tell
    # the difference, which used to let "value" (and friends) sail
    # through as a configured admin_permission that then has no bit to
    # check against a real user's permissions.
    text = f"""
guild_id: 1
admin_permission: value
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_admin_permission_all_classmethod_is_rejected(tmp_path):
    # "all" and "none" are classmethods on discord.Permissions, same
    # shape of false-positive as "value": hasattr() would have called
    # either "a real flag" too.
    text = f"""
guild_id: 1
admin_permission: all
digest:
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
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_every_real_permission_flag_name_is_accepted(tmp_path):
    # The flip side of the "value"/"all"/"none" false-positive cases:
    # every name VALID_FLAGS actually considers a real permission bit
    # must still load cleanly: the fix shouldn't have narrowed the
    # accepted set to less than what it's supposed to be.
    import discord

    for flag_name in discord.Permissions.VALID_FLAGS:
        text = f"""
guild_id: 1
admin_permission: {flag_name}
digest:
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
  - key: palworld
    name: "Palworld Again"
    channel_id: 3
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: "Not Valid!"
    name: "Palworld"
    channel_id: 2
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
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
sources:
  - type: bluesky_search
    query: "Palworld"
    topics: [palworld]
    trust: community
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.catalog[0].sources[0].name == "Bluesky: Palworld"


def test_web_search_without_brave_key_disables_source(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    text = f"""
guild_id: 1
digest:
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
    # Web search is quietly off when BRAVE_API_KEY isn't set: configured, but nothing records
    # health under its name and no collector is built for it.
    assert all(c.source_type != "web_search" for c in build_catalog_collectors(cfg, _secrets()))


def test_example_config_loads(monkeypatch):
    # config.example.yaml is what the owner copies to config.yaml on day one;
    # if this doesn't load, the README's setup instructions are lying. It's the v3
    # shape: a catalog of 15 games, the first three in the order the comped prompt
    # depends on, and no server in it (servers are set up from Discord).
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(EXAMPLE)
    keys = [g.key for g in cfg.catalog]
    assert len(keys) == 15
    assert keys[:3] == ["borderlands4", "palworld", "diablo4"]
    assert cfg.legacy is None


def test_minimal_config_loads(monkeypatch):
    # config.minimal.yaml (self-host plan task 4) is the other end of
    # config.example.yaml: the smallest starting point a fork copies to
    # config.yaml. If this doesn't load, the setup guide it's meant to
    # anchor is lying.
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(MINIMAL)
    assert {g.key for g in cfg.catalog} == {"yourgame"}
    assert len(_all_sources(cfg)) + (cfg.web_search is not None) == 3
    assert cfg.legacy is None


def test_minimal_config_loads_without_brave_key_too(monkeypatch):
    # BRAVE_API_KEY is optional; a fork that never sets it should still
    # get a working bot with the web_search source quietly disabled, not
    # a config error.
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    cfg = load_config(MINIMAL)
    assert "Brave Search" not in {c.name for c in build_catalog_collectors(cfg, _secrets())}


def test_example_config_shift_block_says_what_the_code_defaults_to(monkeypatch):
    # The example's live `shift:` block spells out every setting. It's meant to describe
    # ShiftCfg's real defaults for an owner who's about to change a number, so if
    # config.py's defaults ever drift from it, this catches the documentation going
    # stale rather than an owner finding out by trusting a wrong number. (Only `games`
    # differs on purpose: a v3 file that leaves it out means borderlands4.)
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(EXAMPLE)
    assert cfg.shift == ShiftCfg(games=["borderlands4"])


def test_topic_search_queries_default_empty():
    topic = Topic(key="palworld", name="Palworld", channel_id=1)
    assert topic.search_queries == []


def test_topic_search_queries_accepts_list():
    topic = Topic(
        key="borderlands4",
        name="Borderlands 4",
        channel_id=1,
        search_queries=["Borderlands 4 news"],
    )
    assert topic.search_queries == ["Borderlands 4 news"]


def test_topic_search_queries_rejects_blank_entries(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
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


def test_digest_subject_defaults_to_video_games(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(FIXTURE)
    assert cfg.ai.subject == "video games"


def test_digest_subject_is_overridable(tmp_path):
    text = """
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
  subject: "tabletop RPGs"
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
    cfg = _load_with(tmp_path, text)
    assert cfg.ai.subject == "tabletop RPGs"


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
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.legacy.shift_enabled is False
    assert cfg.collection.interval_minutes == 60
    assert cfg.shift.max_item_age_hours == 48
    assert cfg.shift.max_pings_per_day == 3
    assert cfg.shift.allow_test_command is False
    assert cfg.shift.ping_trust == ["official", "press"]
    assert cfg.shift.max_codes_per_item == 5


def test_alerts_full_block_parses(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  channel_id: 5
  interval_minutes: 30
  max_item_age_hours: 24
  max_pings_per_day: 5
  allow_test_command: true
  ping_trust: [official]
  max_codes_per_item: 10
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.legacy.shift_enabled is True
    assert cfg.legacy.shift_channel_id == 5
    assert cfg.collection.interval_minutes == 30
    assert cfg.shift.max_item_age_hours == 24
    assert cfg.shift.max_pings_per_day == 5
    assert cfg.legacy.shift_ping == "everyone"  # a nonzero cap is the v2 "@everyone" behavior
    assert cfg.shift.allow_test_command is True
    assert cfg.shift.ping_trust == ["official"]
    assert cfg.shift.max_codes_per_item == 10


def test_alerts_max_codes_per_item_must_be_at_least_one(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  max_codes_per_item: 0
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_alerts_ping_trust_rejects_unknown_trust_level(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  ping_trust: [official, rumor]
"""
    with pytest.raises(ConfigError):
        _load_with(tmp_path, text)


def test_alerts_ping_trust_empty_list_is_accepted():
    # An owner who wants no ping ever, from any source, can set this to
    # [] directly: plan_alerts never finds a "trusted" candidate, so
    # every batch posts without pinging, same as ping_trust never
    # matching anything. Not the same as max_pings_per_day: 0 (that still
    # spends nothing either way; this at least documents the intent).
    from newsbot.config import AlertsCfg

    assert AlertsCfg(ping_trust=[]).ping_trust == []


def test_alerts_unknown_key_rejected(tmp_path):
    text = f"""
guild_id: 1
digest:
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
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  interval_minutes: 15
  max_item_age_hours: 1
  max_pings_per_day: 0
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.collection.interval_minutes == 15
    assert cfg.shift.max_item_age_hours == 1
    assert cfg.shift.max_pings_per_day == 0
    assert cfg.legacy.shift_ping == "none"  # a cap of 0 is "never ping", which is the ping choice


def test_alerts_max_item_age_hours_upper_bound_is_inclusive_at_720(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  max_item_age_hours: 720
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.shift.max_item_age_hours == 720


def test_allow_test_command_true_with_enabled_false_is_a_config_error(tmp_path):
    text = f"""
guild_id: 1
digest:
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
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  channel_id: 5
  allow_test_command: true
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.shift.allow_test_command is True


def test_allow_test_command_false_with_enabled_false_is_fine(tmp_path):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: false
  allow_test_command: false
"""
    cfg = _load_with(tmp_path, text)
    assert cfg.shift.allow_test_command is False


def test_alerts_allow_test_command_true_logs_a_warning(tmp_path, caplog):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  channel_id: 5
  allow_test_command: true
"""
    with caplog.at_level("WARNING"):
        _load_with(tmp_path, text)
    assert any("allow_test_command" in record.message for record in caplog.records)


def test_alerts_allow_test_command_false_logs_no_warning(tmp_path, caplog):
    text = f"""
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
{VALID_TAIL}
alerts:
  enabled: true
  channel_id: 5
  allow_test_command: false
"""
    with caplog.at_level("WARNING"):
        _load_with(tmp_path, text)
    assert not any("allow_test_command" in record.message for record in caplog.records)


def test_digest_time_with_trailing_newline_rejected(tmp_path):
    # Same `$`-matches-before-newline trap the lounge's time setting had;
    # both share _TIME_RE, so both get the regression test. The `\n` below
    # is a real newline inside the YAML double-quoted string.
    text = f"""
guild_id: 1
digest:
  time: "09:00\\n"
  timezone: "UTC"
{VALID_TAIL}
"""
    with pytest.raises(ConfigError, match="digest.time"):
        _load_with(tmp_path, text)
