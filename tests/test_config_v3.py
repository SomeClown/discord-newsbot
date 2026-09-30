"""Tests for the v3 config shape: catalog, shared sources, and every validation message.

Ids are fake; the games are real only because inventing a fake Borderlands
felt like more work than it was worth.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from newsbot.collectors.base import build_catalog_collectors
from newsbot.config import (
    ConfigError,
    GameCfg,
    Secrets,
    SharedRssSource,
    configured_source_names,
    load_config,
)
from newsbot.pipeline.filter import build_matchers

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"

_MIN = """
catalog:
  - key: palworld
    name: "Palworld"
    sources:
      - {type: steam_news, name: "Palworld Steam", app_id: 1623730, trust: official}
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def _errors(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    return str(exc_info.value)


def _secrets(brave: str | None = None) -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key="x",
        brave_api_key=brave,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


# --- the shape itself ---


def test_v3_fixture_loads(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    cfg = load_config(V3)
    assert [g.key for g in cfg.catalog] == ["borderlands4", "palworld", "rust"]
    assert cfg.home_guild_id == 200000000000000001
    assert cfg.comped_guild_ids == [200000000000000003]
    assert cfg.command_guild_ids == []
    assert cfg.web_search is not None and cfg.web_search.queries_per_game == 2
    assert cfg.shift.games == ["borderlands4"]
    assert cfg.collection.max_items_per_game == 60
    assert cfg.ai.subject == "video games"
    assert cfg.legacy is None
    assert cfg.owner_report.time == "21:00"


def test_shared_source_games_restriction_is_kept(monkeypatch):
    cfg = load_config(V3)
    by_name = {s.name: s for s in cfg.shared_sources}
    assert by_name["PC Gamer"].games is None
    assert by_name["2K Newsroom"].games == ["borderlands4"]
    assert isinstance(by_name["PC Gamer"], SharedRssSource)


def test_bluesky_default_name_is_filled_in_for_catalog_sources():
    cfg = load_config(V3)
    names = [s.name for s in cfg.catalog[0].sources]
    assert "Bluesky: Borderlands 4" in names


def test_home_guild_defaults_to_the_legacy_guild_id(tmp_path):
    text = _MIN + "guild_id: 42\ndigest: {time: '09:00', timezone: UTC}\n"
    cfg = load_config(_write(tmp_path, text))
    assert cfg.home_guild_id == 42


def test_explicit_home_guild_id_wins_over_the_legacy_guild_id(tmp_path):
    text = _MIN + "guild_id: 42\nhome_guild_id: 7\ndigest: {time: '09:00', timezone: UTC}\n"
    assert load_config(_write(tmp_path, text)).home_guild_id == 7


def test_no_guild_id_and_no_home_guild_id_leaves_home_unset(tmp_path):
    assert load_config(_write(tmp_path, _MIN)).home_guild_id is None


# --- validation messages ---


def test_neither_shape(tmp_path):
    msg = _errors(tmp_path, "admin_permission: manage_guild\n")
    assert "config needs a catalog: (v3), or the v2 topics: and sources: to import from" in msg


def test_too_many_games(tmp_path):
    games = "\n".join(f'  - {{key: g{i}, name: "G{i}"}}' for i in range(26))
    msg = _errors(tmp_path, "catalog:\n" + games + "\n")
    assert "catalog has 26 games; the limit is 25 (a Discord select menu holds 25 options)" in msg


def test_25_games_is_fine(tmp_path):
    games = "\n".join(f'  - {{key: g{i}, name: "G{i}"}}' for i in range(25))
    assert len(load_config(_write(tmp_path, "catalog:\n" + games + "\n")).catalog) == 25


def test_bad_key_and_duplicate_key(tmp_path):
    text = """
catalog:
  - {key: "Bad Key", name: "A"}
  - {key: ok, name: "B"}
  - {key: ok, name: "C"}
"""
    msg = _errors(tmp_path, text)
    assert "catalog[0].key 'Bad Key' must match ^[a-z0-9_]+$" in msg
    assert "duplicate game key 'ok'" in msg


def test_topics_on_a_catalog_source(tmp_path):
    text = """
catalog:
  - key: palworld
    name: "Palworld"
    sources:
      - {type: steam_news, name: "Palworld Steam", app_id: 1, topics: [palworld], trust: official}
"""
    msg = _errors(tmp_path, text)
    assert (
        "catalog[0] (palworld).sources[0] (Palworld Steam): remove topics; "
        "a source listed under a game belongs to that game" in msg
    )


def test_web_search_inside_a_game(tmp_path):
    text = """
catalog:
  - key: palworld
    name: "Palworld"
    sources:
      - {type: web_search, trust: press}
"""
    msg = _errors(tmp_path, text)
    assert (
        "catalog[0] (palworld).sources[0]: web_search goes in the top-level web_search: block"
        in msg
    )


def test_web_search_inside_shared_sources(tmp_path):
    text = _MIN + "shared_sources:\n  - {type: web_search, trust: press}\n"
    msg = _errors(tmp_path, text)
    assert "shared_sources[0]: web_search goes in the top-level web_search: block" in msg


def test_shared_source_unknown_game_and_topics_keyword(tmp_path):
    text = (
        _MIN
        + """
shared_sources:
  - {type: rss, name: "Wire", url: "https://example.com/a.rss", games: [nope], trust: press}
  - {type: rss, name: "Old", url: "https://example.com/b.rss", topics: [palworld], trust: press}
"""
    )
    msg = _errors(tmp_path, text)
    assert "shared_sources[0] (Wire) names unknown game 'nope'" in msg
    assert "shared_sources[1] (Old): use games:, not topics:" in msg


def test_shift_games_unknown(tmp_path):
    msg = _errors(tmp_path, _MIN + "shift:\n  games: [ghost]\n")
    assert "shift.games names unknown game 'ghost'" in msg


def test_duplicate_source_name_across_catalog_shared_and_web_search(tmp_path):
    text = (
        _MIN
        + """
shared_sources:
  - {type: rss, name: "Palworld Steam", url: "https://example.com/a.rss", trust: press}
web_search:
  name: "Palworld Steam"
"""
    )
    msg = _errors(tmp_path, text)
    assert msg.count("duplicate source name 'Palworld Steam'") == 2


def test_match_name_false_needs_aliases(tmp_path):
    text = 'catalog:\n  - {key: rust, name: "Rust", match_name: false}\n'
    msg = _errors(tmp_path, text)
    assert "match_name is false for rust but it has no aliases, so nothing would ever match" in msg


def test_channel_id_inside_catalog(tmp_path):
    text = 'catalog:\n  - {key: rust, name: "Rust", channel_id: 5}\n'
    msg = _errors(tmp_path, text)
    assert "catalog[0].channel_id: channels are per server now; use /newsbot follow" in msg


@pytest.mark.parametrize("key", ["home_guild_id", "comped_guild_ids", "command_guild_ids"])
@pytest.mark.parametrize("bad", ["0", "-5", "true"])
def test_guild_ids_must_be_positive_non_bool(tmp_path, key, bad):
    value = f"[{bad}]" if key.endswith("ids") else bad
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _MIN + f"{key}: {value}\n"))


def test_owner_report_time_and_zone(tmp_path):
    msg = _errors(tmp_path, _MIN + 'owner_report: {time: "25:99", timezone: "Mars/Olympus"}\n')
    assert "owner_report.time '25:99' is not HH:MM (24-hour)" in msg
    assert "owner_report.timezone 'Mars/Olympus' is not a known IANA zone" in msg
    assert "Value error" not in msg


def test_negative_max_pings(tmp_path):
    msg = _errors(tmp_path, _MIN + "shift:\n  max_pings_per_day: -1\n")
    assert "shift.max_pings_per_day" in msg


def test_several_errors_arrive_in_one_config_error(tmp_path):
    text = """
catalog:
  - {key: "Bad", name: "A", channel_id: 1}
  - {key: rust, name: "Rust", match_name: false}
shift: {games: [ghost]}
"""
    msg = _errors(tmp_path, text)
    for piece in ("catalog[0].key", "catalog[0].channel_id", "match_name is false", "ghost"):
        assert piece in msg


def test_digest_channel_id_precheck_still_applies(tmp_path):
    msg = _errors(tmp_path, _MIN + "digest: {time: '09:00', timezone: UTC, channel_id: 5}\n")
    assert "digest.channel_id was removed in v2.0" in msg


def test_v2_missing_keys_keep_pydantic_wording(tmp_path):
    msg = _errors(tmp_path, "topics: []\n")
    assert "sources: Field required" in msg
    assert "guild_id: Field required" in msg


def test_guild_id_without_digest_is_an_error(tmp_path):
    msg = _errors(tmp_path, _MIN + "guild_id: 5\n")
    assert "guild_id is set but digest: is missing" in msg


def test_shared_sources_without_catalog_is_an_error(tmp_path):
    text = """
guild_id: 1
digest: {time: "09:00", timezone: UTC}
topics: [{key: a, name: "A", channel_id: 2}]
sources: []
shared_sources: []
"""
    assert "shared_sources needs a catalog:" in _errors(tmp_path, text)


# --- match_name ---


def test_match_name_false_matches_on_aliases_only():
    cfg = load_config(V3)
    rust = next(g for g in cfg.catalog if g.key == "rust")
    matcher = build_matchers([rust])["rust"]
    assert matcher.match("Rust gets a new update") is None
    assert matcher.match("Facepunch shipped the monthly update") == "confident"
    assert matcher.match("the Rust game is on sale") == "confident"


def test_match_name_true_matches_the_name():
    game = GameCfg(key="palworld", name="Palworld")
    assert build_matchers([game])["palworld"].match("Palworld patch notes") == "confident"


# --- collectors and names ---


def test_catalog_collectors_tag_items_with_their_game(monkeypatch):
    cfg = load_config(V3)
    collectors = build_catalog_collectors(cfg, _secrets())
    by_name = {c.name: c for c in collectors}
    assert by_name["Borderlands 4 Steam"]._source.topics == ["borderlands4"]
    assert by_name["Palworld Steam"]._source.topics == ["palworld"]
    assert by_name["PC Gamer"]._source.topics is None
    assert by_name["2K Newsroom"]._source.topics == ["borderlands4"]
    assert "Brave Search" not in by_name


def test_catalog_collectors_add_web_search_only_with_a_key():
    cfg = load_config(V3)
    without = build_catalog_collectors(cfg, _secrets())
    with_key = build_catalog_collectors(cfg, _secrets("k"))
    assert len(with_key) == len(without) + 1
    off = build_catalog_collectors(cfg, _secrets("k"), include_web_search=False)
    assert len(off) == len(without)


def test_configured_source_names_covers_catalog_and_shared(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    names = configured_source_names(load_config(V3))
    assert names == {
        "Borderlands 4 Steam",
        "Bluesky: Borderlands 4",
        "Palworld Steam",
        "PC Gamer",
        "2K Newsroom",
        "Brave Search",
    }


def test_configured_source_names_leaves_out_brave_without_a_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert "Brave Search" not in configured_source_names(load_config(V3))


def test_missing_brave_key_warns_once_for_a_v3_web_search_block(monkeypatch, caplog):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        load_config(V3)
    assert sum("BRAVE_API_KEY is not set" in r.message for r in caplog.records) == 1
