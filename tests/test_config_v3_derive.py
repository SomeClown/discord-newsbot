"""Tests for derive mode and the legacy view (design.md §15, plan section 3.1).

With no `catalog:`, the v2 `topics` and `sources` are regrouped into one, so
the unchanged production config keeps loading. With both, the catalog wins
and the old keys only feed the one-time import.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from newsbot.config import (
    AlertsCfg,
    ConfigError,
    LoungeCfg,
    SharedRssSource,
    configured_source_names,
    load_config,
)

FIXTURES = Path(__file__).parent / "fixtures"
PRODLIKE = FIXTURES / "config_v2_prodlike.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
MINIMAL = Path(__file__).parent.parent / "config.minimal.yaml"


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


def _games(cfg):
    return {g.key: g for g in cfg.catalog}


def test_shipped_examples_load_in_derive_mode():
    for path in (EXAMPLE, MINIMAL):
        cfg = load_config(path)
        assert cfg.catalog
        assert cfg.legacy is not None


def test_derived_catalog_has_one_game_per_topic_without_channels():
    cfg = load_config(PRODLIKE)
    assert list(_games(cfg)) == ["borderlands4", "palworld", "diablo4"]
    bl4 = _games(cfg)["borderlands4"]
    assert bl4.name == "Borderlands 4"
    assert bl4.aliases == ["BL4", "Borderlands4"]
    assert bl4.entities == ["Gearbox"]
    assert bl4.search_queries[0] == "Borderlands 4 news"
    assert bl4.match_name is True
    assert not hasattr(bl4, "channel_id")


def test_single_topic_sources_go_under_their_game_with_topics_dropped():
    games = _games(load_config(PRODLIKE))
    assert [s.name for s in games["diablo4"].sources] == [
        "Diablo IV Steam",
        "Diablo IV Blizzard Tracker",
    ]
    assert [s.name for s in games["borderlands4"].sources] == [
        "Borderlands 4 Steam",
        "r/Borderlands4",
        'Bluesky: "Borderlands 4"',
    ]
    for game in games.values():
        assert all(s.topics is None for s in game.sources)


def test_unscoped_and_multi_topic_sources_are_shared():
    cfg = load_config(PRODLIKE)
    by_name = {s.name: s for s in cfg.shared_sources}
    assert set(by_name) == {"2K Newsroom", "Pocketpair and Gearbox Wire", "PC Gamer", "Eurogamer"}
    assert by_name["PC Gamer"].games is None
    assert by_name["Pocketpair and Gearbox Wire"].games == ["borderlands4", "palworld"]
    assert all(isinstance(s, SharedRssSource) and s.topics is None for s in cfg.shared_sources)


def test_web_search_source_becomes_the_global_block():
    cfg = load_config(PRODLIKE)
    assert cfg.web_search is not None
    assert cfg.web_search.name == "Brave Search"
    assert cfg.web_search.queries_per_game == 2
    assert cfg.web_search.trust == "press"


def test_alerts_become_shift():
    shift = load_config(PRODLIKE).shift
    assert shift.games == ["borderlands4"]
    assert shift.max_item_age_hours == 48
    assert shift.max_pings_per_day == 3
    assert shift.ping_trust == ["official", "press"]
    assert shift.max_codes_per_item == 5
    assert shift.allow_test_command is False


def test_digest_becomes_collection_ai_and_run_report():
    cfg = load_config(PRODLIKE)
    assert cfg.collection.lookback_hours == 24
    assert cfg.collection.max_items_per_game == 60
    assert cfg.collection.interval_minutes == 60
    assert cfg.ai.subject == "video games"
    assert cfg.run_report is True


def test_derive_carries_non_default_values(tmp_path):
    text = PRODLIKE.read_text().replace("report_to_admin: true", "report_to_admin: false")
    text = text.replace('subject: "video games"', 'subject: "tabletop games"')
    text = text.replace("interval_minutes: 60", "interval_minutes: 30")
    p = tmp_path / "c.yaml"
    p.write_text(text)
    cfg = load_config(p)
    assert cfg.run_report is False
    assert cfg.ai.subject == "tabletop games"
    assert cfg.collection.interval_minutes == 30


def test_explicit_v3_blocks_win_over_derived_ones(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(PRODLIKE.read_text() + '\nai:\n  subject: "card games"\n')
    assert load_config(p).ai.subject == "card games"


def test_the_v2_setup_is_in_legacy_and_nothing_else_carries_the_v2_keys():
    # Before the cutover the v2 fields stayed on `AppConfig` for the running bot. They're gone
    # now: the one server's old setup is `legacy` (read once, by the import), and everything
    # else the bot needs lives in the catalog and the v3 blocks.
    cfg = load_config(PRODLIKE)
    assert cfg.legacy.guild_id == 100000000000000001
    assert len(cfg.catalog) == 3
    assert sum(len(g.sources) for g in cfg.catalog) + len(cfg.shared_sources) + 1 == 11
    assert cfg.web_search is not None  # the eleventh source, now its own block
    assert cfg.legacy.digest_time == "09:00"
    assert cfg.legacy.shift_enabled and cfg.legacy.lounge.daily_quote.enabled
    for gone in ("guild_id", "digest", "topics", "sources", "alerts", "lounge"):
        assert not hasattr(cfg, gone), gone


def test_derive_without_a_brave_key_still_derives_web_search_but_drops_the_source(
    monkeypatch, caplog
):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(PRODLIKE)
    assert cfg.web_search is not None
    assert "Brave Search" not in configured_source_names(cfg)  # derived, but nothing runs it
    assert sum("BRAVE_API_KEY is not set" in r.message for r in caplog.records) == 1


def test_home_guild_defaults_to_guild_id_in_derive_mode():
    assert load_config(PRODLIKE).home_guild_id == 100000000000000001


def test_derived_config_does_not_log_the_deletable_keys_line(caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.config"):
        load_config(PRODLIKE)
    assert not any("can be deleted" in r.message for r in caplog.records)


# --- LegacySetup ---


def test_legacy_setup_contents():
    legacy = load_config(PRODLIKE).legacy
    assert legacy is not None
    assert legacy.guild_id == 100000000000000001
    assert legacy.admin_channel_id == 100000000000000002
    assert legacy.digest_time == "09:00"
    assert legacy.timezone == "America/Los_Angeles"
    assert legacy.games == [
        ("borderlands4", 1452017235274240221),
        ("palworld", 1542581309845799013),
        ("diablo4", 1531681353681211524),
    ]
    assert legacy.shift_enabled is True
    assert legacy.shift_channel_id == 1553597251933438122
    assert legacy.shift_ping == "everyone"
    assert legacy.alerts_max_pings == 3
    assert legacy.lounge is not None
    assert legacy.lounge.channel_id == 1401806745898061826
    assert len(legacy.lounge.daily_quote.sources) == 8


def test_legacy_ping_is_none_when_max_pings_is_zero(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(PRODLIKE.read_text().replace("max_pings_per_day: 3", "max_pings_per_day: 0"))
    legacy = load_config(p).legacy
    assert legacy.shift_ping == "none"
    assert legacy.alerts_max_pings == 0


def test_legacy_lounge_is_none_when_the_block_is_absent():
    assert load_config(FIXTURES / "config_valid.yaml").legacy.lounge is None


def test_legacy_shift_is_off_when_alerts_are_absent():
    legacy = load_config(FIXTURES / "config_valid.yaml").legacy
    assert legacy.shift_enabled is False
    assert legacy.shift_channel_id is None


# --- hybrid: old keys plus catalog ---


def _hybrid(tmp_path, catalog_keys=("borderlands4", "palworld", "diablo4")):
    games = "\n".join(f'  - {{key: {k}, name: "{k}"}}' for k in catalog_keys)
    p = tmp_path / "hybrid.yaml"
    p.write_text(PRODLIKE.read_text() + "\ncatalog:\n" + games + "\n")
    return p


def test_hybrid_catalog_wins_and_legacy_is_still_built(tmp_path):
    cfg = load_config(_hybrid(tmp_path))
    # The derived catalog would have sources; the explicit one has none.
    assert all(g.sources == [] for g in cfg.catalog)
    assert cfg.shared_sources == []
    assert cfg.legacy is not None
    assert len(cfg.legacy.games) == 3


def test_hybrid_logs_the_keys_that_can_be_deleted(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.config"):
        load_config(_hybrid(tmp_path))
    lines = [r.message for r in caplog.records if "can be deleted" in r.message]
    assert len(lines) == 1
    assert "guild_id, digest, topics, sources, alerts, lounge" in lines[0]


def test_hybrid_legacy_topic_missing_from_the_catalog_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_hybrid(tmp_path, catalog_keys=("borderlands4", "palworld")))
    assert (
        "topics[2] (diablo4) isn't in catalog; "
        "the v2 import needs every old game in the catalog" in str(exc_info.value)
    )


def test_hybrid_alerts_topics_are_checked_against_the_catalog(tmp_path):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_hybrid(tmp_path, catalog_keys=("palworld", "diablo4")))
    assert "alerts.topics references unknown topic 'borderlands4'" in str(exc_info.value)


def test_a_hybrid_config_keeps_the_v22_alerts_and_lounge_key_sets(tmp_path):
    """Nothing v3 may ever appear inside alerts: or lounge:, or TAG=2.2.0 can't load the file.

    v2.2's blocks are extra="forbid"; this pins their field sets so a
    well-meaning new key can't sneak in and break rollback.
    """
    assert set(AlertsCfg.model_fields) == {
        "enabled",
        "channel_id",
        "interval_minutes",
        "max_item_age_hours",
        "max_pings_per_day",
        "allow_test_command",
        "topics",
        "ping_trust",
        "max_codes_per_item",
    }
    assert set(LoungeCfg.model_fields) == {"channel_id", "welcome", "daily_quote"}
    # And the hybrid file itself loads with those blocks exactly as v2.2 wrote them.
    cfg = load_config(_hybrid(tmp_path))
    assert cfg.legacy.shift_enabled and cfg.legacy.lounge.welcome.enabled
