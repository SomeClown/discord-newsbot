"""Adversarial tests for the `industry` catalog entry (industry news, task 1).

Angle (test-engineer brief 2026-10-23): the entry is special because of one
string, so this file leans on that string. It tries the key in the wrong place
(first, middle, twice), in the wrong case, and as a near miss, and it tries
every way a keyword, a search, a codes-only feed or a shared feed could sneak
in through a side door. It also checks the exemption stays exactly as wide as
the brief says: other games still get the "match_name is false" error.

Where the loader accepts something today that a stricter owner might not want,
the test pins what happens and says so. Nothing here touches the network or
the filesystem outside pytest's tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import ConfigError, load_config
from newsbot.industry import INDUSTRY_KEY, counts_toward_limit

_HEAD = """
catalog:
  - key: palworld
    name: "Palworld"
    sources:
      - {type: steam_news, name: "Palworld Steam", app_id: 1623730, trust: official}
"""

_FEED = (
    '      - {type: rss, name: "Industry Feed A", url: "https://a.example.com/f", trust: press}\n'
)

_V2_DIGEST = (
    "guild_id: 123456789012345678\n"
    "digest: {time: '09:00', timezone: 'America/Los_Angeles', lookback_hours: 24,"
    " max_items_per_topic: 60}\n"
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def _errors(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    return str(exc_info.value)


def _entry(
    key: str = "industry",
    extra: str = "    match_name: false\n",
    sources: str | None = _FEED,
) -> str:
    out = f'  - key: {key}\n    name: "Gaming industry"\n{extra}'
    if sources is not None:
        out += "    sources:\n" + sources
    return out


def _game(key: str, extra: str = "") -> str:
    return f'  - key: {key}\n    name: "{key}"\n    aliases: ["{key} alias"]\n{extra}'


# --- counts_toward_limit: exact match only ---


@pytest.mark.parametrize(
    "key",
    ["Industry", "INDUSTRY", " industry", "industry ", "industry\n", "industries", "", "industr"],
)
def test_counts_toward_limit_is_true_for_anything_but_the_exact_key(key):
    assert counts_toward_limit(key) is True


def test_counts_toward_limit_is_false_for_the_constant():
    assert counts_toward_limit(INDUSTRY_KEY) is False


# --- position ---


def test_industry_first_is_refused_with_its_index(tmp_path):
    text = "catalog:\n" + _entry() + _game("palworld")
    msg = _errors(tmp_path, text)
    assert "catalog[0] (industry): the industry entry must be the last catalog entry" in msg


def test_industry_in_the_middle_is_refused_with_its_index(tmp_path):
    text = "catalog:\n" + _game("a") + _entry() + _game("b")
    msg = _errors(tmp_path, text)
    assert "catalog[1] (industry): the industry entry must be the last catalog entry" in msg


def test_industry_as_the_only_entry_loads(tmp_path):
    cfg = load_config(_write(tmp_path, "catalog:\n" + _entry()))
    assert [g.key for g in cfg.catalog] == ["industry"]


def test_industry_last_among_many_loads(tmp_path):
    text = "catalog:\n" + _game("a") + _game("b") + _game("c") + _entry()
    cfg = load_config(_write(tmp_path, text))
    assert cfg.catalog[-1].key == "industry"


def test_two_industry_entries_are_refused_and_both_rules_speak(tmp_path):
    text = "catalog:\n" + _entry() + _entry().replace("Feed A", "Feed B")
    msg = _errors(tmp_path, text)
    assert "duplicate game key 'industry'" in msg
    assert "catalog[0] (industry): the industry entry must be the last catalog entry" in msg
    assert "catalog[1] (industry): the industry entry must be the last catalog entry" not in msg


# --- the key is exact: near misses are ordinary games ---


@pytest.mark.parametrize("key", ["Industry", "INDUSTRY"])
def test_a_capitalised_key_is_not_industry_and_fails_the_key_rule(tmp_path, key):
    msg = _errors(tmp_path, _HEAD + _entry(key=key, extra="    aliases: [x]\n"))
    assert f"catalog[1].key '{key}' must match ^[a-z0-9_]+$" in msg
    assert "(industry)" not in msg


@pytest.mark.parametrize("key", ["industry2", "industrial", "industry_news", "my_industry"])
def test_near_miss_keys_keep_the_match_name_error(tmp_path, key):
    text = _HEAD + f'  - key: {key}\n    name: "Rust"\n    match_name: false\n'
    msg = _errors(tmp_path, text)
    assert f"match_name is false for {key} but it has no aliases" in msg
    assert "the industry entry" not in msg


@pytest.mark.parametrize("key", ["industry2", "industry_news"])
def test_near_miss_keys_may_have_aliases_and_sit_anywhere(tmp_path, key):
    text = "catalog:\n" + _game(key) + _entry()
    cfg = load_config(_write(tmp_path, text))
    assert [g.key for g in cfg.catalog] == [key, "industry"]


def test_a_game_merely_named_gaming_industry_is_an_ordinary_game(tmp_path):
    text = (
        "catalog:\n"
        '  - key: news\n    name: "Gaming industry"\n    aliases: ["games business"]\n'
        '    search_queries: ["gaming industry news"]\n'
        "    match_name: true\n" + _entry()
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.catalog[0].aliases == ["games business"]


def test_the_exemption_does_not_leak_to_other_games_in_the_same_file(tmp_path):
    text = "catalog:\n" + '  - key: rust\n    name: "Rust"\n    match_name: false\n' + _entry()
    msg = _errors(tmp_path, text)
    assert "match_name is false for rust but it has no aliases" in msg
    assert "match_name is false for industry" not in msg


# --- empty vs missing vs wrong-typed matching fields ---


def test_explicit_empty_lists_are_the_same_as_missing(tmp_path):
    extra = "    match_name: false\n    aliases: []\n    entities: []\n    search_queries: []\n"
    cfg = load_config(_write(tmp_path, _HEAD + _entry(extra=extra)))
    industry = cfg.catalog[-1]
    assert industry.aliases == [] and industry.entities == [] and industry.search_queries == []


@pytest.mark.parametrize("field", ["aliases", "entities", "search_queries"])
def test_a_null_field_is_a_config_error_not_a_crash(tmp_path, field):
    msg = _errors(tmp_path, _HEAD + _entry(extra=f"    match_name: false\n    {field}: null\n"))
    assert "Invalid config" in msg


@pytest.mark.parametrize("field", ["aliases", "entities"])
def test_a_blank_term_is_still_refused_on_industry(tmp_path, field):
    msg = _errors(tmp_path, _HEAD + _entry(extra=f'    match_name: false\n    {field}: [" "]\n'))
    assert "can't have blank entries" in msg


def test_all_three_matching_fields_are_reported_together(tmp_path):
    extra = (
        "    match_name: false\n    aliases: [a]\n    entities: [b]\n    search_queries: ['c d']\n"
    )
    msg = _errors(tmp_path, _HEAD + _entry(extra=extra))
    for field in ("aliases", "entities", "search_queries"):
        assert f"catalog[1] (industry): {field} must be empty" in msg


def test_match_name_true_with_aliases_gets_both_refusals(tmp_path):
    msg = _errors(tmp_path, _HEAD + _entry(extra="    match_name: true\n    aliases: [a]\n"))
    assert "match_name must be false" in msg
    assert "aliases must be empty" in msg


def test_match_name_left_out_means_true_and_is_refused(tmp_path):
    # The model's default is True, so "I didn't say" is a refusal, not a pass.
    msg = _errors(tmp_path, _HEAD + _entry(extra=""))
    assert "catalog[1] (industry): match_name must be false" in msg


# --- sources: missing, empty, null ---


def test_sources_missing_needs_a_source(tmp_path):
    msg = _errors(tmp_path, _HEAD + _entry(sources=None))
    assert "catalog[1] (industry): the industry entry needs at least one source" in msg


def test_sources_with_nothing_under_it_is_a_config_error_not_a_crash(tmp_path):
    # `sources:` with nothing under it is YAML null, which pydantic rejects on its own.
    msg = _errors(tmp_path, _HEAD + _entry(sources=""))
    assert "Invalid config" in msg


def test_sources_as_an_inline_empty_list_needs_a_source(tmp_path):
    text = _HEAD + '  - key: industry\n    name: "I"\n    match_name: false\n    sources: []\n'
    assert "the industry entry needs at least one source" in _errors(tmp_path, text)


# --- codes-only, in all its spellings ---


@pytest.mark.parametrize(
    "source",
    [
        '{type: steam_news, name: "S", app_id: 10, trust: official, codes_only: true}',
        '{type: bluesky_search, name: "B", query: "games", trust: community, codes_only: true}',
        '{type: rss, name: "R", url: "https://a.example.com/f", trust: press, codes_only: true}',
        '{type: rss, name: "R", url: "https://www.reddit.com/r/x/.rss", trust: community}',
        '{type: rss, name: "R", url: "https://old.reddit.com/r/x/.rss", trust: community}',
        '{type: rss, name: "R", url: "https://reddit.com/r/x/.rss", trust: community}',
    ],
)
def test_every_codes_only_spelling_is_refused(tmp_path, source):
    msg = _errors(tmp_path, _HEAD + _entry(sources=f"      - {source}\n"))
    assert "catalog[1] (industry).sources[0] (" in msg
    assert "a codes-only source can't feed the industry entry" in msg


def test_codes_only_false_on_a_plain_feed_is_fine(tmp_path):
    feed = (
        '      - {type: rss, name: "R", url: "https://a.example.com/f", trust: press, '
        "codes_only: false}\n"
    )
    cfg = load_config(_write(tmp_path, _HEAD + _entry(sources=feed)))
    assert cfg.catalog[-1].sources[0].codes_only is False


def test_a_reddit_lookalike_host_is_not_codes_only(tmp_path):
    feed = '      - {type: rss, name: "R", url: "https://notreddit.com/r/x/.rss", trust: press}\n'
    cfg = load_config(_write(tmp_path, _HEAD + _entry(sources=feed)))
    assert len(cfg.catalog[-1].sources) == 1


def test_codes_only_false_on_reddit_gets_the_existing_error_only(tmp_path):
    feed = (
        '      - {type: rss, name: "R", url: "https://www.reddit.com/r/x/.rss", '
        "trust: community, codes_only: false}\n"
    )
    msg = _errors(tmp_path, _HEAD + _entry(sources=feed))
    assert "codes_only can't be false for a Reddit source" in msg


def test_only_the_bad_source_is_named_and_its_index_is_right(tmp_path):
    feeds = (
        _FEED + '      - {type: rss, name: "Bad", url: "https://b.example.com/f", trust: press,'
        " codes_only: true}\n"
    )
    msg = _errors(tmp_path, _HEAD + _entry(sources=feeds))
    assert "catalog[1] (industry).sources[1] (Bad): a codes-only source" in msg
    assert "sources[0]" not in msg.split("a codes-only source")[0].split("\n")[-1]


def test_a_shift_table_under_industry_is_refused_once(tmp_path):
    table = (
        '      - {type: shift_table, name: "T", url: "https://a.example.com/t", trust: community}\n'
    )
    msg = _errors(tmp_path, "shift: {games: [palworld]}\n" + _HEAD + _entry(sources=table + _FEED))
    assert "(T): shift_table only feeds SHiFT detection, and industry isn't in shift.games" in msg
    assert "codes-only source can't feed" not in msg


def test_a_shift_table_with_industry_in_shift_games_is_still_refused(tmp_path):
    table = (
        '      - {type: shift_table, name: "T", url: "https://a.example.com/t", trust: community}\n'
    )
    text = "shift: {games: [industry]}\n" + _HEAD + _entry(sources=table + _FEED)
    msg = _errors(tmp_path, text)
    assert "shift.games can't include industry (it has no SHiFT codes)" in msg


def test_a_web_search_source_under_industry_is_still_refused(tmp_path):
    ws = '      - {type: web_search, name: "Brave", trust: press}\n'
    msg = _errors(tmp_path, _HEAD + _entry(sources=ws + _FEED))
    assert "web_search goes in the top-level web_search: block" in msg


def test_a_source_with_topics_under_industry_is_still_refused(tmp_path):
    feed = (
        '      - {type: rss, name: "R", url: "https://a.example.com/f", trust: press, '
        "topics: [palworld]}\n"
    )
    msg = _errors(tmp_path, _HEAD + _entry(sources=feed))
    assert "a source listed under a game belongs to that game" in msg


# --- shift.games ---


def test_shift_games_naming_only_industry_is_refused(tmp_path):
    msg = _errors(tmp_path, "shift: {games: [industry]}\n" + _HEAD + _entry())
    assert "shift.games can't include industry (it has no SHiFT codes)" in msg


def test_shift_games_naming_industry_twice_is_refused_each_time(tmp_path):
    msg = _errors(tmp_path, "shift: {games: [industry, industry]}\n" + _HEAD + _entry())
    assert msg.count("shift.games can't include industry") == 2


def test_shift_games_naming_industry_when_it_is_not_in_the_catalog_is_unknown(tmp_path):
    msg = _errors(tmp_path, "shift: {games: [industry]}\n" + _HEAD)
    assert "shift.games names unknown game 'industry'" in msg
    assert "can't include industry" not in msg


def test_shift_games_without_industry_loads(tmp_path):
    cfg = load_config(_write(tmp_path, "shift: {games: [palworld]}\n" + _HEAD + _entry()))
    assert cfg.shift.games == ["palworld"]


def test_leaving_shift_out_does_not_put_industry_in_shift_games(tmp_path):
    text = 'catalog:\n  - key: borderlands4\n    name: "BL4"\n    aliases: [BL4]\n' + _entry()
    cfg = load_config(_write(tmp_path, text))
    assert INDUSTRY_KEY not in cfg.shift.games


# --- shared sources ---


def _shared(games: str, name: str = "Wide Feed", kind: str = "rss") -> str:
    body = {
        "rss": f'type: rss, name: "{name}", url: "https://wide.example.com/f", trust: press',
        "steam_news": f'type: steam_news, name: "{name}", app_id: 7, trust: official',
        "bluesky_search": f'type: bluesky_search, name: "{name}", query: "x", trust: community',
    }[kind]
    return f"  - {{{body}, games: {games}}}\n"


@pytest.mark.parametrize("kind", ["rss", "steam_news", "bluesky_search"])
def test_a_shared_source_of_any_kind_may_not_list_industry(tmp_path, kind):
    text = "shared_sources:\n" + _shared("[industry]", kind=kind) + _HEAD + _entry()
    msg = _errors(tmp_path, text)
    assert "shared_sources[0] (Wide Feed): the industry entry takes only its own dedicated" in msg


def test_industry_among_other_games_is_still_refused(tmp_path):
    text = "shared_sources:\n" + _shared("[industry, palworld]") + _HEAD + _entry()
    assert "the industry entry takes only its own dedicated" in _errors(tmp_path, text)


def test_each_offending_shared_source_is_named_and_a_clean_one_is_not(tmp_path):
    text = (
        "shared_sources:\n"
        + _shared("[industry]", name="One")
        + _shared("[palworld]", name="Clean")
        + _shared("[palworld, industry]", name="Three")
        + _HEAD
        + _entry()
    )
    msg = _errors(tmp_path, text)
    assert "shared_sources[0] (One): the industry entry takes only" in msg
    assert "shared_sources[2] (Three): the industry entry takes only" in msg
    assert "Clean" not in msg


def test_a_shared_source_naming_industry_when_it_is_absent_is_unknown(tmp_path):
    text = "shared_sources:\n" + _shared("[industry]") + _HEAD
    msg = _errors(tmp_path, text)
    assert "names unknown game 'industry'" in msg
    assert "takes only its own dedicated" not in msg


def test_shared_sources_with_empty_games_or_no_games_load(tmp_path):
    text = (
        "shared_sources:\n"
        + _shared("[]", name="Empty")
        + '  - {type: rss, name: "All", url: "https://all.example.com/f", trust: press}\n'
        + _HEAD
        + _entry()
    )
    cfg = load_config(_write(tmp_path, text))
    assert [s.name for s in cfg.shared_sources] == ["Empty", "All"]


def test_a_shared_source_with_games_null_loads(tmp_path):
    text = "shared_sources:\n" + _shared("null") + _HEAD + _entry()
    assert load_config(_write(tmp_path, text)).shared_sources[0].games is None


# --- v2 shape and hybrid ---


def _topic(key: str, channel: int) -> str:
    return f'  - {{key: {key}, name: "{key}", channel_id: {channel}}}\n'


def test_v2_refusal_uses_the_topics_own_index(tmp_path):
    text = (
        _V2_DIGEST
        + "topics:\n"
        + _topic("palworld", 111111111111111111)
        + _topic("industry", 111111111111111112)
    )
    assert "topics[1] (industry): the industry entry needs the v3 catalog: shape" in _errors(
        tmp_path, text
    )


def test_v2_shape_without_industry_is_untouched_by_the_rule(tmp_path):
    text = _V2_DIGEST + "topics:\n" + _topic("palworld", 111111111111111111) + "sources: []\n"
    cfg = load_config(_write(tmp_path, text))
    assert [g.key for g in cfg.catalog] == ["palworld"]


@pytest.mark.parametrize("key", ["Industry", "industry2"])
def test_v2_near_miss_topic_keys_are_not_refused_by_the_industry_rule(tmp_path, key):
    text = _V2_DIGEST + "topics:\n" + _topic(key, 111111111111111111) + "sources: []\n"
    try:
        load_config(_write(tmp_path, text))
    except ConfigError as exc:
        assert "the industry entry needs the v3 catalog" not in str(exc)


def test_v2_shape_with_a_source_scoped_to_industry_is_refused(tmp_path):
    text = (
        _V2_DIGEST
        + "topics:\n"
        + _topic("palworld", 111111111111111111)
        + _topic("industry", 111111111111111112)
        + "sources:\n"
        + '  - {type: rss, name: "W", url: "https://w.example.com/f", trust: press, '
        "topics: [industry]}\n"
    )
    assert "the industry entry needs the v3 catalog: shape" in _errors(tmp_path, text)


def test_hybrid_with_industry_topic_still_enforces_the_catalog_rules(tmp_path):
    text = (
        _V2_DIGEST
        + "topics:\n"
        + _topic("palworld", 111111111111111111)
        + _topic("industry", 111111111111111112)
        + "catalog:\n"
        + _entry()
        + _game("palworld")
    )
    msg = _errors(tmp_path, text)
    assert "catalog[0] (industry): the industry entry must be the last catalog entry" in msg
    assert "the industry entry needs the v3 catalog: shape" not in msg


def test_hybrid_with_industry_topic_but_no_catalog_entry_is_refused(tmp_path):
    # Pins the existing import rule: every old topic has to be in the catalog.
    text = _V2_DIGEST + "topics:\n" + _topic("industry", 111111111111111112) + _HEAD
    assert "topics[0] (industry) isn't in catalog" in _errors(tmp_path, text)


def test_hybrid_industry_topic_alongside_another_game_loads(tmp_path):
    text = (
        _V2_DIGEST
        + "topics:\n"
        + _topic("palworld", 111111111111111111)
        + _topic("industry", 111111111111111112)
        + _HEAD
        + _entry()
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.legacy is not None
    assert [k for k, _ in cfg.legacy.games] == ["palworld", "industry"]
