"""Tests for the `industry` catalog entry's config rules (industry news, task 1).

The entry is marked by its key, so every rule here is a rule about one key:
nothing about it may be keyword-matched or searched, it sits last, it brings
its own feeds, and it never borrows a shared one. Config problems are startup
errors, not warnings (owner decision 4A, 2026-10-23).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from v3_fakes import load_v3_industry_config

from newsbot.config import ConfigError, load_config
from newsbot.industry import INDUSTRY_KEY, INDUSTRY_MAX_STORIES, counts_toward_limit

FIXTURES = Path(__file__).parent / "fixtures"
INDUSTRY_FIXTURE = FIXTURES / "config_v3_industry.yaml"

_ENTRY = """
  - key: industry
    name: "Gaming industry"
{extra}    sources:
{sources}
"""

_FEED = (
    '      - {type: rss, name: "Industry Feed A", '
    'url: "https://industry-a.example.com/feed", trust: press}'
)

_HEAD = """
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


def _with_entry(extra: str = "    match_name: false\n", sources: str = _FEED) -> str:
    return _HEAD + _ENTRY.format(extra=extra, sources=sources)


# --- the module itself ---


def test_the_key_and_the_cap():
    assert INDUSTRY_KEY == "industry"
    assert INDUSTRY_MAX_STORIES == 5


def test_only_the_industry_key_skips_the_game_limit():
    assert counts_toward_limit("industry") is False
    assert counts_toward_limit("palworld") is True
    assert counts_toward_limit("Industry") is True


# --- the fixture ---


def test_the_fixture_loads_with_industry_last(monkeypatch):
    cfg = load_v3_industry_config(monkeypatch)
    assert [g.key for g in cfg.catalog] == ["borderlands4", "palworld", "rust", "industry"]
    industry = cfg.catalog[-1]
    assert industry.match_name is False
    assert industry.aliases == [] and industry.entities == [] and industry.search_queries == []
    assert [s.name for s in industry.sources] == ["Industry Feed A", "Industry Feed B"]


def test_the_helper_applies_overrides(monkeypatch):
    cfg = load_v3_industry_config(monkeypatch, comped_guild_ids=[])
    assert cfg.comped_guild_ids == []


def test_a_minimal_valid_entry_loads(tmp_path):
    cfg = load_config(_write(tmp_path, _with_entry()))
    assert cfg.catalog[-1].key == "industry"


# --- the refusals ---


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ('    aliases: ["games business"]\n', "aliases"),
        ('    entities: ["Some Studio"]\n', "entities"),
        ('    search_queries: ["gaming industry news"]\n', "search_queries"),
    ],
)
def test_matching_and_search_terms_are_refused(tmp_path, extra, field):
    msg = _errors(tmp_path, _with_entry("    match_name: false\n" + extra))
    assert (
        f"catalog[1] (industry): {field} must be empty "
        "(the industry entry is never keyword-matched or searched)"
    ) in msg


def test_match_name_true_is_refused(tmp_path):
    msg = _errors(tmp_path, _with_entry(extra=""))
    assert "catalog[1] (industry): match_name must be false" in msg


def test_industry_must_be_last(tmp_path):
    text = _with_entry() + '  - key: rust\n    name: "Rust"\n    aliases: ["Facepunch"]\n'
    msg = _errors(tmp_path, text)
    assert "catalog[1] (industry): the industry entry must be the last catalog entry" in msg


def test_industry_needs_a_source(tmp_path):
    msg = _errors(
        tmp_path, _HEAD + '  - key: industry\n    name: "Gaming industry"\n    match_name: false\n'
    )
    assert "catalog[1] (industry): the industry entry needs at least one source" in msg


def test_a_codes_only_source_is_refused(tmp_path):
    feed = _FEED[:-1] + ", codes_only: true}"
    msg = _errors(tmp_path, _with_entry(sources=feed))
    assert (
        "catalog[1] (industry).sources[0] (Industry Feed A): a codes-only source can't feed "
        "the industry entry (its items are never stored as news)"
    ) in msg


def test_a_reddit_source_is_refused_as_codes_only(tmp_path):
    feed = (
        '      - {type: rss, name: "Reddit Feed", '
        'url: "https://www.reddit.com/r/gaming/.rss", trust: community}'
    )
    msg = _errors(tmp_path, _with_entry(sources=feed))
    assert "a codes-only source can't feed the industry entry" in msg


def test_a_shift_table_source_is_refused(tmp_path):
    table = (
        '      - {type: shift_table, name: "Table", url: "https://example.com/codes", '
        "trust: community}"
    )
    msg = _errors(tmp_path, _with_entry(sources=table))
    assert "catalog[1] (industry).sources[0] (Table): shift_table only feeds SHiFT detection" in msg


def test_shift_games_may_not_name_industry(tmp_path):
    text = "shift:\n  games: [palworld, industry]\n" + _with_entry()
    msg = _errors(tmp_path, text)
    assert "shift.games can't include industry (it has no SHiFT codes)" in msg


def test_a_shared_source_may_not_list_industry(tmp_path):
    text = (
        "shared_sources:\n"
        '  - {type: rss, name: "Wide Feed", url: "https://wide.example.com/feed", '
        "trust: press, games: [palworld, industry]}\n" + _with_entry()
    )
    msg = _errors(tmp_path, text)
    assert (
        "shared_sources[0] (Wide Feed): the industry entry takes only its own dedicated "
        "sources, so a shared source can't list it in games:"
    ) in msg


def test_a_shared_source_without_games_is_fine(tmp_path):
    text = (
        "shared_sources:\n"
        '  - {type: rss, name: "Wide Feed", url: "https://wide.example.com/feed", trust: press}\n'
        + _with_entry()
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.shared_sources[0].games is None


# --- match_name: false needs aliases, except for industry ---


def test_match_name_false_without_aliases_still_errors_for_a_normal_game(tmp_path):
    text = _HEAD + '  - key: rust\n    name: "Rust"\n    match_name: false\n'
    msg = _errors(tmp_path, text)
    assert "match_name is false for rust but it has no aliases, so nothing would ever match" in msg


def test_match_name_false_without_aliases_is_fine_for_industry(tmp_path):
    cfg = load_config(_write(tmp_path, _with_entry()))
    assert cfg.catalog[-1].aliases == []


def test_every_problem_is_reported_together(tmp_path):
    text = _HEAD + (
        '  - key: industry\n    name: "Gaming industry"\n    aliases: ["x"]\n    sources: []\n'
    )
    msg = _errors(tmp_path, text)
    assert "aliases must be empty" in msg
    assert "match_name must be false" in msg
    assert "needs at least one source" in msg


# --- the v2 shape and the hybrid ---


def test_the_v2_shape_refuses_an_industry_topic(tmp_path):
    text = (
        "guild_id: 123456789012345678\n"
        "digest: {time: '09:00', timezone: 'America/Los_Angeles', lookback_hours: 24,"
        " max_items_per_topic: 60}\n"
        "topics:\n"
        '  - {key: industry, name: "Gaming industry", channel_id: 111111111111111112}\n'
    )
    msg = _errors(tmp_path, text)
    assert "topics[0] (industry): the industry entry needs the v3 catalog: shape" in msg


def test_a_hybrid_file_with_an_industry_topic_loads(tmp_path):
    text = (
        "guild_id: 123456789012345678\n"
        "digest: {time: '09:00', timezone: 'America/Los_Angeles', lookback_hours: 24,"
        " max_items_per_topic: 60}\n"
        "topics:\n"
        '  - {key: palworld, name: "Palworld", channel_id: 111111111111111111}\n'
        '  - {key: industry, name: "Gaming industry", channel_id: 111111111111111112}\n'
    ) + _with_entry()
    cfg = load_config(_write(tmp_path, text))
    assert cfg.legacy is not None
    assert ("industry", 111111111111111112) in cfg.legacy.games
    assert cfg.catalog[-1].key == "industry"
