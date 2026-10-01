"""Adversarial tests for the v3 config shape, beyond test_config_v3.py.

Angle (public app, task 1, test-engineer brief 2026-09-30): the catalog is a
hand-edited YAML file that a stranger's typo can quietly reshape, so this
file feeds it every wrong shape I could think of and watches what comes out
the other end. That means duplicate keys, ids of the wrong type, blank
aliases, catalogs that are dicts, and secrets stuffed in URLs to see if the
error messages repeat them (they don't; good).

Where the loader accepts something a stricter owner might not want, the test
pins what happens today and says so in a comment. Those are documentation
with teeth, not endorsements. Where the loader is plainly wrong, the test is
`xfail(strict=True)` with the reason attached, so the day somebody fixes it
the suite tells them to delete the marker. No network, and nothing touches
the filesystem outside pytest's tmp_path.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from newsbot.collectors.base import RawItem, build_catalog_collectors
from newsbot.config import (
    ConfigError,
    GameCfg,
    Secrets,
    Topic,
    load_config,
)
from newsbot.pipeline.filter import build_matchers, filter_items

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
PRODLIKE = Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml"

_MIN = """
catalog:
  - key: palworld
    name: "Palworld"
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


def _game(catalog_yaml: str) -> str:
    return "catalog:\n" + catalog_yaml


# --- game keys ---


def test_keys_differing_only_by_case_are_rejected_as_bad_keys_not_as_duplicates(tmp_path):
    # Uppercase never passes the key regex, so "Rust" and "rust" can't collide;
    # the loader says the uppercase one is malformed instead.
    msg = _errors(tmp_path, _game('  - {key: rust, name: "R"}\n  - {key: Rust, name: "R2"}\n'))
    assert "catalog[1].key 'Rust' must match ^[a-z0-9_]+$" in msg
    assert "duplicate game key" not in msg


def test_a_key_with_a_trailing_newline_is_rejected(tmp_path):
    # `\Z`, not `$`; the block scalar below is one YAML feature away from a real typo.
    text = _game('  - key: "rust\\n"\n    name: "R"\n')
    assert "must match ^[a-z0-9_]+$" in _errors(tmp_path, text)


@pytest.mark.parametrize("key", ['""', "'a b'", "'a-b'", "'ünï'", "'a.b'"])
def test_odd_keys_are_rejected(tmp_path, key):
    assert "must match ^[a-z0-9_]+$" in _errors(tmp_path, _game(f'  - {{key: {key}, name: "R"}}\n'))


@pytest.mark.parametrize("key", ["123", "true", "null", "1.5"])
def test_yaml_typed_keys_are_a_config_error_not_a_crash(tmp_path, key):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _game(f'  - {{key: {key}, name: "R"}}\n')))


def test_three_way_duplicate_key_reports_each_repeat(tmp_path):
    text = _game('  - {key: a, name: "1"}\n  - {key: a, name: "2"}\n  - {key: a, name: "3"}\n')
    assert _errors(tmp_path, text).count("duplicate game key 'a'") == 2


def test_key_shape_is_checked_even_past_the_cap(tmp_path):
    games = "".join(f'  - {{key: g{i}, name: "G{i}"}}\n' for i in range(25))
    games += '  - {key: "Bad", name: "B"}\n'
    msg = _errors(tmp_path, _game(games))
    assert "catalog has 26 games" in msg
    assert "catalog[25].key 'Bad'" in msg


def test_an_empty_catalog_loads(tmp_path):
    # Pinned, not endorsed: `catalog: []` is a bot with nothing to follow.
    # The plan doesn't say it's an error, and the setup wizard would simply
    # offer an empty menu.
    assert load_config(_write(tmp_path, "catalog: []\n")).catalog == []


@pytest.mark.parametrize("text", ["catalog:\n", "catalog: {key: a, name: b}\n", "catalog: rust\n"])
def test_a_catalog_that_is_not_a_list_is_one_clean_error(tmp_path, text):
    assert "catalog: Input should be a valid list" in _errors(tmp_path, text)


@pytest.mark.parametrize("entry", ["rust", "null", "[a, b]", "{}"])
def test_a_catalog_entry_that_is_not_a_mapping_is_a_config_error(tmp_path, entry):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"catalog:\n  - {entry}\n"))


# --- sources inside games ---


def test_a_game_with_zero_sources_loads(tmp_path):
    # Its news comes from shared sources and keyword matching, or nowhere.
    cfg = load_config(_write(tmp_path, _MIN))
    assert cfg.catalog[0].sources == []


def test_a_game_no_source_can_ever_reach_is_not_flagged(tmp_path):
    # Pinned: zero sources, no shared sources, no web_search. It's a dead game
    # and the loader doesn't say a word. A "game has no way to get news"
    # warning would be a kindness; nobody asked for one.
    cfg = load_config(_write(tmp_path, _MIN))
    assert cfg.shared_sources == [] and cfg.web_search is None


@pytest.mark.parametrize("sources", ["hello", "[hello]", "{type: rss}", "null"])
def test_sources_of_the_wrong_yaml_shape_are_config_errors(tmp_path, sources):
    text = _MIN + f"    sources: {sources}\n"
    msg = _errors(tmp_path, text)
    assert "catalog.0.sources" in msg


def test_a_source_of_unknown_type_names_the_tags(tmp_path):
    text = _MIN + "    sources: [{type: carrier_pigeon, name: X, trust: press}]\n"
    assert "carrier_pigeon" in _errors(tmp_path, text)


def test_two_sources_in_one_game_with_the_same_name_collide(tmp_path):
    text = (
        _MIN
        + """    sources:
      - {type: steam_news, name: "Same", app_id: 1, trust: official}
      - {type: steam_news, name: "Same", app_id: 2, trust: official}
"""
    )
    assert "duplicate source name 'Same'" in _errors(tmp_path, text)


def test_two_bluesky_sources_with_the_same_query_collide_on_the_default_name(tmp_path):
    # No `name`, same query: both get "Bluesky: q", and source_health is keyed by name.
    text = (
        _MIN
        + """    sources:
      - {type: bluesky_search, query: "q", trust: community}
      - {type: bluesky_search, query: "q", trust: community}
"""
    )
    assert "duplicate source name 'Bluesky: q'" in _errors(tmp_path, text)


def test_source_name_uniqueness_is_case_sensitive(tmp_path):
    # Pinned: "PC Gamer" and "pc gamer" are two sources with two health rows.
    text = (
        _MIN
        + """    sources:
      - {type: steam_news, name: "Name", app_id: 1, trust: official}
      - {type: steam_news, name: "name", app_id: 2, trust: official}
"""
    )
    assert len(load_config(_write(tmp_path, text)).catalog[0].sources) == 2


def test_a_topics_key_on_a_catalog_source_is_rejected_even_when_empty_list(tmp_path):
    text = (
        _MIN + "    sources: [{type: steam_news, name: X, app_id: 1, topics: [], trust: press}]\n"
    )
    assert "remove topics" in _errors(tmp_path, text)


def test_a_null_topics_key_on_a_catalog_source_is_treated_as_absent(tmp_path):
    # Pinned: `topics: null` is indistinguishable from no key after parsing.
    text = _MIN + "    sources: [{type: steam_news, name: X, app_id: 1, topics: ~, trust: press}]\n"
    assert load_config(_write(tmp_path, text)).catalog[0].sources[0].name == "X"


# --- shared sources ---


def test_shared_source_game_names_are_case_sensitive(tmp_path):
    text = (
        _MIN
        + """shared_sources:
  - {type: rss, name: W, url: "https://example.com/a", games: [Palworld], trust: press}
"""
    )
    assert "names unknown game 'Palworld'" in _errors(tmp_path, text)


def test_shared_source_with_an_empty_games_list_means_unrestricted(tmp_path):
    # Pinned: `games: []` is "no restriction", not "no games". An owner who
    # meant "switch this off" wants to delete the source instead.
    text = (
        _MIN
        + """shared_sources:
  - {type: rss, name: W, url: "https://example.com/a", games: [], trust: press}
"""
    )
    cfg = load_config(_write(tmp_path, text))
    (collector,) = build_catalog_collectors(cfg, _secrets())
    assert collector.name == "W"
    assert not collector._source.topics


def test_shared_source_games_may_repeat(tmp_path):
    text = (
        _MIN
        + """shared_sources:
  - {type: rss, name: W, url: "https://example.com/a", games: [palworld, palworld], trust: press}
"""
    )
    assert load_config(_write(tmp_path, text)).shared_sources[0].games == ["palworld", "palworld"]


@pytest.mark.parametrize("value", ["{a: b}", "null", "hello"])
def test_shared_sources_of_the_wrong_shape_are_config_errors(tmp_path, value):
    assert "shared_sources" in _errors(tmp_path, _MIN + f"shared_sources: {value}\n")


def test_shared_sources_without_a_catalog_reports_once_beside_the_v2_errors(tmp_path):
    text = "shared_sources: []\ntopics: []\n"
    msg = _errors(tmp_path, text)
    assert "sources: Field required" in msg


def test_shift_games_may_repeat_and_still_validates_each(tmp_path):
    msg = _errors(tmp_path, _MIN + "shift: {games: [ghost, ghost, palworld]}\n")
    assert msg.count("shift.games names unknown game 'ghost'") == 2


# --- aliases, entities and names ---


@pytest.mark.parametrize(
    "field",
    [
        pytest.param('aliases: [""]', id="empty-alias"),
        pytest.param('aliases: ["  "]', id="blank-alias"),
        pytest.param('entities: [""]', id="empty-entity"),
    ],
)
def test_blank_aliases_and_entities_are_rejected(tmp_path, field):
    text = f'catalog:\n  - {{key: rust, name: "Rust", {field}}}\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_a_blank_game_name_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, 'catalog:\n  - {key: rust, name: ""}\n'))


@pytest.mark.parametrize("field", ["aliases", "entities", "name"])
def test_a_blank_term_cannot_even_be_built_into_a_game(field):
    # The loader's rejection sits in the model, so nothing that builds a
    # GameCfg by hand can smuggle a match-everything regex to filter.py either.
    kwargs = {"key": "rust", "name": "Rust", "aliases": ["R"], "match_name": True}
    kwargs[field] = "  " if field == "name" else ["ok", "  "]
    with pytest.raises(ValidationError):
        GameCfg(**kwargs)


def test_blank_term_messages_are_plain_and_name_the_field(tmp_path):
    msg = _errors(tmp_path, 'catalog:\n  - {key: rust, name: "Rust", aliases: [""]}\n')
    assert "catalog.0.aliases: aliases can't have blank entries" in msg
    assert "Value error" not in msg
    msg = _errors(tmp_path, 'catalog:\n  - {key: rust, name: " "}\n')
    assert "catalog.0.name: can't be blank" in msg


def test_a_v2_topic_drops_blank_aliases_and_entities_with_a_warning(tmp_path, caplog):
    # v2.2 loaded these, so an upgrade must too: drop, warn, carry on.
    text = (
        "guild_id: 1\ndigest: {time: '09:00', timezone: UTC}\n"
        'topics: [{key: rust, name: "Rust", channel_id: 2, aliases: ["", "Rusty"], '
        'entities: [" "]}]\nsources: []\n'
    )
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(_write(tmp_path, text))
    assert cfg.catalog[0].aliases == ["Rusty"] and cfg.catalog[0].entities == []
    assert build_matchers(cfg.catalog)["rust"].match("Hello, world") is None
    assert "dropping 1 blank aliases" in caplog.text


def test_a_v2_topic_with_a_blank_name_is_an_error(tmp_path):
    text = (
        "guild_id: 1\ndigest: {time: '09:00', timezone: UTC}\n"
        'topics: [{key: rust, name: "", channel_id: 2}]\nsources: []\n'
    )
    assert "topics.0.name: can't be blank" in _errors(tmp_path, text)


def test_duplicate_aliases_are_accepted(tmp_path):
    # Pinned: harmless (one regex alternation), so not worth an error.
    text = 'catalog:\n  - {key: rust, name: "Rust", aliases: [A, A, a]}\n'
    assert load_config(_write(tmp_path, text)).catalog[0].aliases == ["A", "A", "a"]


def test_an_alias_equal_to_the_name_with_match_name_false_still_matches_the_name(tmp_path):
    # Pinned loophole: match_name false plus an alias spelled like the name
    # is the same as match_name true. The validator can't tell and doesn't try.
    text = 'catalog:\n  - {key: rust, name: "Rust", aliases: ["rust"], match_name: false}\n'
    game = load_config(_write(tmp_path, text)).catalog[0]
    assert build_matchers([game])["rust"].match("rusty gate? no, Rust the game") == "confident"


def test_match_name_false_with_only_entities_is_still_an_error(tmp_path):
    # Entities give `uncertain` hits, never confident ones, so the "nothing
    # would ever match" wording is a touch strong; the loader errs strict.
    text = 'catalog:\n  - {key: rust, name: "Rust", entities: [Facepunch], match_name: false}\n'
    assert "match_name is false for rust" in _errors(tmp_path, text)


@pytest.mark.parametrize("value", ["false", "no", "off", "0"])
def test_match_name_accepts_yaml_string_falsehoods(tmp_path, value):
    # Pinned: pydantic's lax bool. A quoted "false" counts as false, which is
    # the friendly reading; "maybe" doesn't parse at all (next test).
    text = f'catalog:\n  - {{key: rust, name: "Rust", aliases: [X], match_name: "{value}"}}\n'
    assert load_config(_write(tmp_path, text)).catalog[0].match_name is False


def test_match_name_garbage_is_a_config_error(tmp_path):
    text = 'catalog:\n  - {key: rust, name: "Rust", aliases: [X], match_name: maybe}\n'
    assert "match_name" in _errors(tmp_path, text)


@pytest.mark.parametrize(
    "field", ["aliases: BL4", "aliases: [1, 2]", "aliases:", "entities: X", "search_queries: X"]
)
def test_list_fields_of_the_wrong_type_are_config_errors(tmp_path, field):
    text = f'catalog:\n  - {{key: rust, name: "Rust", {field}}}\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_blank_search_query_is_rejected_for_catalog_games(tmp_path):
    text = 'catalog:\n  - {key: rust, name: "Rust", search_queries: ["ok", " "]}\n'
    assert "search_queries entries must be non-empty strings" in _errors(tmp_path, text)


# --- unknown keys ---
# These used to be pinned as "silently ignored". The owner decided that's how
# typos turn into silent misbehavior (`comped_guild_id` costs a server its
# premium tier; `match_names: false` leaves a common-word game matching every
# rusty gate), so the new v3 blocks are extra="forbid" now. The top level
# stays open (v2.2 and hybrid files carry keys we don't read) but warns.
# alerts/lounge are untouched: they were already forbid in v2.2.


@pytest.mark.parametrize(
    "extra, key, where",
    [
        ("shift: {gaems: [palworld]}\n", "gaems", "shift"),
        ("collection: {interval_minute: 15}\n", "interval_minute", "collection"),
        ("ai: {subjct: cards}\n", "subjct", "ai"),
        ("owner_report: {tz: UTC}\n", "tz", "owner_report"),
        ("web_search: {queries_per_topic: 9}\n", "queries_per_topic", "web_search"),
    ],
)
def test_unknown_keys_in_the_v3_blocks_are_errors_naming_key_and_place(tmp_path, extra, key, where):
    msg = _errors(tmp_path, _MIN + extra)
    assert f"unknown key {key!r} in {where}" in msg
    assert "Value error" not in msg


def test_unknown_keys_inside_a_game_are_errors(tmp_path):
    text = 'catalog:\n  - {key: rust, name: "Rust", alises: [X], match_names: false}\n'
    msg = _errors(tmp_path, text)
    assert "unknown key 'alises' in catalog.0" in msg
    assert "unknown key 'match_names' in catalog.0" in msg


def test_unknown_keys_inside_a_shared_source_are_errors(tmp_path):
    text = (
        _MIN + "shared_sources: [{type: rss, name: W, url: 'https://example.com/a', trust: press,"
        " game: [palworld]}]\n"
    )
    assert "unknown key 'game' in shared_sources.0.rss" in _errors(tmp_path, text)


def test_a_games_key_on_a_catalog_source_is_still_ignored(tmp_path):
    # Pinned: catalog sources reuse the v2 source models (which stay open for
    # v2.2 compatibility), so `games:` here is dropped. Follow-up candidate.
    text = (
        _MIN + "    sources: [{type: steam_news, name: X, app_id: 1, games: [zzz], trust: press}]\n"
    )
    assert load_config(_write(tmp_path, text)).catalog[0].sources[0].name == "X"


def test_unknown_top_level_keys_load_but_are_warned_about_once(tmp_path, caplog):
    text = _MIN + "comped_guild_id: [5]\nowner_reports: {time: '01:00'}\n"
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(_write(tmp_path, text))
    assert cfg.comped_guild_ids == []
    warnings = [r for r in caplog.records if "unknown top-level keys" in r.getMessage()]
    assert len(warnings) == 1
    assert "comped_guild_id, owner_reports" in warnings[0].getMessage()


def test_known_v3_and_legacy_top_level_keys_do_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        load_config(V3)
        load_config(PRODLIKE)
    assert "unknown top-level keys" not in caplog.text


# --- guild ids ---


@pytest.mark.parametrize("value", ["[5, 5, 5]", "['12', 13.0]"])
def test_comped_ids_accept_duplicates_and_lax_numerics(tmp_path, value):
    # Pinned: duplicates are harmless, and a quoted snowflake is what people
    # paste from Discord anyway.
    cfg = load_config(_write(tmp_path, _MIN + f"comped_guild_ids: {value}\n"))
    assert all(isinstance(i, int) and i > 0 for i in cfg.comped_guild_ids)


@pytest.mark.parametrize(
    "value", ["[abc]", "[1.5]", "[[1]]", "[~]", "5", "~", "{a: 1}", "[0]", "[-1]"]
)
def test_comped_ids_reject_the_wrong_shapes(tmp_path, value):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _MIN + f"comped_guild_ids: {value}\n"))


def test_one_bad_comped_id_fails_the_whole_list_once(tmp_path):
    msg = _errors(tmp_path, _MIN + "comped_guild_ids: [5, 0, -1]\n")
    assert msg.count("comped_guild_ids") == 1


@pytest.mark.parametrize("key", ["home_guild_id", "comped_guild_ids", "command_guild_ids"])
def test_bool_guild_id_message_is_plain_and_names_the_right_field(tmp_path, key):
    value = "[true]" if key.endswith("ids") else "true"
    msg = _errors(tmp_path, _MIN + f"{key}: {value}\n")
    assert "Value error" not in msg
    assert "channel_id" not in msg


def test_non_positive_guild_id_message_has_no_value_error_prefix(tmp_path):
    # The guild ids have their own validator now (they borrowed the channel one).
    assert "Value error" not in _errors(tmp_path, _MIN + "comped_guild_ids: [0]\n")


def test_home_guild_id_null_is_the_same_as_absent(tmp_path):
    assert load_config(_write(tmp_path, _MIN + "home_guild_id: ~\n")).home_guild_id is None


def test_home_guild_id_null_falls_back_to_the_legacy_guild_id(tmp_path):
    text = _MIN + "home_guild_id: ~\nguild_id: 42\ndigest: {time: '09:00', timezone: UTC}\n"
    assert load_config(_write(tmp_path, text)).home_guild_id == 42


@pytest.mark.parametrize("bad", ["0", "true", "-3"])
def test_a_bad_legacy_guild_id_does_not_become_the_home_guild(tmp_path, bad):
    # The legacy guild_id is validated like home_guild_id itself; model_copy
    # used to carry a bad one across unchecked.
    text = _MIN + f"guild_id: {bad}\ndigest: {{time: '09:00', timezone: UTC}}\n"
    try:
        cfg = load_config(_write(tmp_path, text))
    except ConfigError:
        return
    assert cfg.home_guild_id is None


# --- numbers in the global blocks ---


@pytest.mark.parametrize(
    "block",
    [
        "collection: {interval_minutes: 14}",
        "collection: {interval_minutes: 1441}",
        "collection: {lookback_hours: 0}",
        "collection: {max_items_per_game: 0}",
        "shift: {max_item_age_hours: 0}",
        "shift: {max_item_age_hours: 721}",
        "shift: {max_codes_per_item: 0}",
        "shift: {ping_trust: [gossip]}",
        "web_search: {queries_per_game: 0}",
        "web_search: {trust: gossip}",
        "owner_report: {time: '24:00'}",
        "owner_report: {time: '9:00'}",
        'owner_report: {time: "08:00\\n"}',
        "collection: 5",
        "shift: [a]",
        "run_report: maybe",
    ],
)
def test_out_of_range_values_in_global_blocks_are_config_errors(tmp_path, block):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _MIN + block + "\n"))


@pytest.mark.parametrize(
    "block",
    [
        "collection: {interval_minutes: 15}",
        "collection: {interval_minutes: 1440}",
        "shift: {max_pings_per_day: 0}",
        "shift: {max_item_age_hours: 720}",
        "shift: {ping_trust: []}",
    ],
)
def test_the_boundaries_themselves_load(tmp_path, block):
    load_config(_write(tmp_path, _MIN + block + "\n"))


# --- top-level junk ---


@pytest.mark.parametrize("text", ["- a\n- b\n", "hello\n", "42\n"])
def test_a_non_mapping_config_is_a_config_error(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize(
    "text, kind",
    [("- a\n- b\n", "a list"), ("hello\n", "plain text"), ("42\n", "a bare int value")],
)
def test_a_non_mapping_config_says_what_it_was(tmp_path, text, kind):
    msg = _errors(tmp_path, text)
    assert f"must be a set of `key: value` settings, not {kind}" in msg
    assert "AttributeError" not in msg


@pytest.mark.parametrize("text", ["", "# only a comment\n", "{}\n", "~\n"])
def test_empty_files_say_what_shape_is_missing(tmp_path, text):
    assert "config needs a catalog: (v3)" in _errors(tmp_path, text)


# --- error aggregation and wording ---


def test_a_pydantic_failure_hides_the_cross_check_errors(tmp_path):
    # Pinned: type errors come out first and alone, so an owner fixes those,
    # restarts, and only then meets "duplicate game key". The plan says one
    # ConfigError per pass; this is the seam where that stops being true.
    text = """
catalog:
  - {key: a, name: "A"}
  - {key: a, name: "B"}
collection: {interval_minutes: 1}
"""
    msg = _errors(tmp_path, text)
    assert "collection.interval_minutes" in msg
    assert "duplicate game key" not in msg


def test_cross_check_errors_arrive_together(tmp_path):
    text = """
catalog:
  - {key: a, name: "A", sources: [{type: web_search, trust: press}]}
  - {key: a, name: "B", match_name: false}
  - {key: c, name: "C", channel_id: 9}
shared_sources:
  - {type: rss, name: W, url: "https://example.com/a", games: [zzz], topics: [a], trust: press}
shift: {games: [yyy]}
admin_permission: value
"""
    msg = _errors(tmp_path, text)
    for piece in (
        "web_search goes in the top-level web_search: block",
        "duplicate game key 'a'",
        "match_name is false for a",
        "catalog[2].channel_id",
        "names unknown game 'zzz'",
        "use games:, not topics:",
        "shift.games names unknown game 'yyy'",
        "admin_permission 'value'",
    ):
        assert piece in msg, piece


@pytest.mark.parametrize(
    "text",
    [
        "catalog: 5\n",
        _MIN + "comped_guild_ids: [abc]\n",
        _MIN + "collection: {interval_minutes: 1}\n",
        _MIN + "owner_report: {time: nope}\n",
        _MIN + "shift: {ping_trust: [gossip]}\n",
        _MIN + "    sources: [{type: rss, name: X, url: 'ftp://h/x', trust: press}]\n",
        'catalog:\n  - {key: "Bad Key", name: "A", channel_id: 1}\n',
    ],
)
def test_messages_carry_no_raw_pydantic_furniture_or_dashes(tmp_path, text):
    msg = _errors(tmp_path, text)
    # Spelled in pieces so this file passes the dash tripwire it is checking for.
    assert (" " + "-" * 2 + " ") not in msg
    assert "[type=" not in msg
    assert "pydantic.dev" not in msg
    assert "validation error for" not in msg


@pytest.mark.parametrize(
    "url",
    [
        "ftp://user:hunter2@example.com/feed?token=hunter2",
        "https://example.com:99999/feed?token=hunter2",
        "not a url with hunter2 in it",
        "https://user:hunter2@",
    ],
)
def test_a_bad_catalog_url_never_echoes_its_secret(tmp_path, url):
    text = _MIN + f'    sources: [{{type: rss, name: X, url: "{url}", trust: press}}]\n'
    assert "hunter2" not in _errors(tmp_path, text)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://user:hunter2@example.com/feed?token=hunter2",
        "https://example.com:99999/feed?token=hunter2",
    ],
)
def test_a_bad_shared_source_url_never_echoes_its_secret(tmp_path, url):
    text = _MIN + f'shared_sources:\n  - {{type: rss, name: X, url: "{url}", trust: press}}\n'
    assert "hunter2" not in _errors(tmp_path, text)


def test_a_hybrid_lounge_url_secret_stays_hidden_next_to_a_v3_error(tmp_path):
    text = (
        'catalog:\n  - {key: rust, name: "Rust", channel_id: 5}\n'
        "lounge:\n  channel_id: 9\n  daily_quote:\n    enabled: true\n    sources:\n"
        '      - url: "http://u:hunter2@example.com/q?k=hunter2"\n'
    )
    msg = _errors(tmp_path, text)
    assert "hunter2" not in msg
    assert "catalog[0].channel_id" in msg


def test_a_duplicate_source_name_is_echoed_because_the_owner_chose_it(tmp_path):
    # Names are echoed on purpose (the message is useless without one), so
    # nobody should put a secret in a source name. Pinned so it's a decision.
    text = (
        _MIN
        + """shared_sources:
  - {type: rss, name: "tok3n", url: "https://example.com/a", trust: press}
web_search: {name: "tok3n"}
"""
    )
    assert "duplicate source name 'tok3n'" in _errors(tmp_path, text)


def _errors_or_empty(tmp_path: Path, text: str) -> str:
    # Palworld the game and "Palworld" the source don't share a namespace, so
    # this config is fine apart from the two sources; return "" if it loads.
    try:
        load_config(_write(tmp_path, text))
    except ConfigError as exc:
        return str(exc)
    return ""


def test_game_names_and_source_names_do_not_share_a_namespace(tmp_path):
    assert _errors_or_empty(tmp_path, _MIN + 'web_search: {name: "Palworld"}\n') == ""


# --- hybrid configs ---


def _hybrid(tmp_path: Path, extra: str) -> Path:
    return _write(tmp_path, _MIN + extra)


@pytest.mark.parametrize(
    "extra, named",
    [
        ("topics: []\n", "topics"),
        ("sources: []\n", "sources"),
        ("alerts: {enabled: false}\n", "alerts"),
        ("lounge: {}\n", "lounge"),
        ("digest: {time: '09:00', timezone: UTC}\n", "digest"),
    ],
)
def test_the_deletable_keys_line_names_only_the_keys_present(tmp_path, caplog, extra, named):
    with caplog.at_level(logging.INFO, logger="newsbot.config"):
        load_config(_hybrid(tmp_path, extra))
    (line,) = [r.getMessage() for r in caplog.records if "can be deleted" in r.getMessage()]
    assert line.endswith(": " + named)


def test_the_deletable_keys_line_uses_a_fixed_order_not_the_files(tmp_path, caplog):
    extra = (
        "lounge: {}\n"
        "sources: []\n"
        "guild_id: 5\n"
        "digest: {time: '09:00', timezone: UTC}\n"
        "topics: []\n"
        "alerts: {}\n"
    )
    with caplog.at_level(logging.INFO, logger="newsbot.config"):
        load_config(_hybrid(tmp_path, extra))
    (line,) = [r.getMessage() for r in caplog.records if "can be deleted" in r.getMessage()]
    assert line.endswith(": guild_id, digest, topics, sources, alerts, lounge")


def test_a_pure_v3_config_logs_no_deletable_keys_line(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.config"):
        load_config(_hybrid(tmp_path, "shift: {games: [palworld]}\nweb_search: {}\n"))
    assert not any("can be deleted" in r.getMessage() for r in caplog.records)


def test_the_deletable_keys_line_is_not_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.config"):
        load_config(_hybrid(tmp_path, "topics: []\n"))
    (record,) = [r for r in caplog.records if "can be deleted" in r.getMessage()]
    assert record.levelno == logging.INFO


def test_hybrid_ignores_the_old_digest_for_collection_ai_and_run_report(tmp_path):
    # Old keys are read only by the legacy view; they must not leak into v3 blocks.
    extra = (
        "guild_id: 5\n"
        "digest:\n  time: '09:00'\n  timezone: UTC\n  lookback_hours: 72\n"
        "  max_items_per_topic: 7\n  report_to_admin: false\n  subject: cards\n"
        "alerts:\n  interval_minutes: 30\n  max_pings_per_day: 0\n  topics: [palworld]\n"
    )
    cfg = load_config(_hybrid(tmp_path, extra))
    assert cfg.collection.lookback_hours == 24
    assert cfg.collection.max_items_per_game == 60
    assert cfg.collection.interval_minutes == 60
    assert cfg.ai.subject == "video games"
    assert cfg.run_report is True
    # Changed with the v3 shift.games rule: left out means borderlands4, not every game.
    assert cfg.shift.games == ["borderlands4"] and cfg.shift.max_pings_per_day == 3
    # ...while the legacy view still sees them.
    assert cfg.legacy is not None and cfg.legacy.shift_ping == "none"
    assert cfg.legacy.digest_time == "09:00"


def test_hybrid_ignores_legacy_sources_when_building_catalog_collectors(tmp_path):
    extra = (
        "topics: []\n"
        "sources:\n"
        "  - {type: steam_news, name: Ghost, app_id: 1, topics: [nope], trust: official}\n"
        "  - {type: steam_news, name: Ghost, app_id: 2, trust: official}\n"
    )
    cfg = load_config(_hybrid(tmp_path, extra))
    assert [c.name for c in build_catalog_collectors(cfg, _secrets())] == []


def test_hybrid_skips_the_v2_only_source_checks(tmp_path):
    # Pinned: in v3 mode, unknown-topic and duplicate-name checks on the
    # legacy `sources` are skipped (the plan's "v2-only checks"). The import
    # would have to cope with such a list; it never sees a catalog reference.
    extra = (
        "topics: []\n"
        "sources:\n"
        "  - {type: steam_news, name: Dup, app_id: 1, topics: [nope], trust: official}\n"
        "  - {type: steam_news, name: Dup, app_id: 2, trust: official}\n"
    )
    assert load_config(_hybrid(tmp_path, extra)).catalog[0].key == "palworld"


def test_collector_names_in_a_hybrid_do_not_list_the_legacy_sources(tmp_path, monkeypatch):
    # This used to be pinned the other way, as a cutover to-do: the v2 `sources` list fed the
    # status page's source names, and a hybrid config reported health rows for sources that
    # `build_catalog_collectors` never runs. The v2 fields left `AppConfig` at the cutover, so
    # a phantom row can't happen, and this holds the line.
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    extra = (
        "topics: []\nsources:\n  - {type: steam_news, name: Legacy, app_id: 1, trust: official}\n"
    )
    cfg = load_config(_hybrid(tmp_path, extra))
    assert "Legacy" not in {c.name for c in build_catalog_collectors(cfg, _secrets())}


def test_hybrid_still_enforces_the_v22_alerts_rules(tmp_path):
    msg = _errors(tmp_path, _MIN + "alerts: {enabled: true}\n")
    assert "alerts.channel_id is required when alerts.enabled is true" in msg


def test_hybrid_alerts_may_not_carry_v3_keys(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_hybrid(tmp_path, "alerts: {games: [palworld]}\n"))


def test_hybrid_lounge_may_not_carry_v3_keys(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_hybrid(tmp_path, "lounge: {home_guild_id: 5}\n"))


def test_lounge_without_a_guild_id_leaves_legacy_empty(tmp_path):
    # Pinned: no guild_id, no LegacySetup, so a lounge block alone is not
    # importable. It still validates.
    cfg = load_config(_hybrid(tmp_path, "lounge: {}\n"))
    assert cfg.legacy is None


def test_legacy_lounge_is_set_even_when_the_block_is_disabled_and_empty(tmp_path):
    extra = "guild_id: 5\ndigest: {time: '09:00', timezone: UTC}\nlounge: {}\n"
    legacy = load_config(_hybrid(tmp_path, extra)).legacy
    assert legacy is not None and legacy.lounge is not None
    assert legacy.lounge.welcome.enabled is False


def test_legacy_lounge_sources_are_filled_in_with_the_builtin_list(tmp_path):
    extra = "guild_id: 5\ndigest: {time: '09:00', timezone: UTC}\nlounge: {}\n"
    legacy = load_config(_hybrid(tmp_path, extra)).legacy
    assert legacy.lounge.daily_quote.sources


def test_hybrid_legacy_topics_must_all_be_in_the_catalog_by_key_not_name(tmp_path):
    extra = (
        "guild_id: 5\ndigest: {time: '09:00', timezone: UTC}\n"
        "topics: [{key: other, name: Palworld, channel_id: 2}]\n"
        "sources: []\n"
    )
    assert "topics[0] (other) isn't in catalog" in _errors(tmp_path, _MIN + extra)


def test_hybrid_topics_without_channel_ids_keep_the_v2_wording(tmp_path):
    extra = (
        "guild_id: 5\ndigest: {time: '09:00', timezone: UTC}\ntopics: [{key: palworld, name: P}]\n"
    )
    assert "topics[0] (palworld): channel_id is required" in _errors(tmp_path, _MIN + extra)


# --- match_name through the filter ---


def _item(title: str, excerpt: str = "", topics: tuple[str, ...] | None = None) -> RawItem:
    return RawItem(
        url="https://example.com/x",
        title=title,
        excerpt=excerpt,
        source_name="S",
        trust="press",
        published_at=None,
        topics=topics,
    )


_COMMON_WORDS = [
    GameCfg(key="rust", name="Rust", aliases=["Rust game", "Facepunch"], match_name=False),
    GameCfg(key="destiny", name="Destiny", aliases=["Destiny 2", "Bungie"], match_name=False),
    GameCfg(key="apex", name="Apex", aliases=["Apex Legends"], match_name=False),
]

_PROSE = [
    "The rust on the gate got worse",
    "Rust never sleeps",
    "Rusty, but trusty",
    "Destiny of the nation",
    "A destiny fulfilled",
    "Apex predator spotted in the garden",
    "Vertex and apex of the triangle",
    "trust the process",
    "Destiny2 spelled wrong",
]


@pytest.mark.parametrize("title", _PROSE)
def test_common_word_games_do_not_match_plain_prose(title):
    matchers = build_matchers(_COMMON_WORDS)
    assert {k: m.match(title) for k, m in matchers.items()} == {
        "rust": None,
        "destiny": None,
        "apex": None,
    }


@pytest.mark.parametrize(
    "title, key",
    [
        ("New Rust game update lands", "rust"),
        ("facepunch ships wipe day", "rust"),
        ("Destiny 2 patch notes", "destiny"),
        ("DESTINY 2: the weekly reset", "destiny"),
        ("Bungie's latest, explained", "destiny"),
        ("Apex Legends season 30", "apex"),
        ("what's new in apex legends?", "apex"),
    ],
)
def test_aliases_still_match_for_common_word_games(title, key):
    assert build_matchers(_COMMON_WORDS)[key].match(title) == "confident"


def test_match_name_false_prose_search_through_filter_items():
    items = [_item("The rust on the gate"), _item("Facepunch confirms the wipe date")]
    grouped = filter_items(items, _COMMON_WORDS, 10)
    assert [t.item.title for t in grouped["rust"]] == ["Facepunch confirms the wipe date"]
    assert "destiny" not in grouped and "apex" not in grouped


def test_a_dedicated_source_item_is_confident_even_when_match_name_is_false():
    # SPEC-DEV 3: a dedicated feed needs no keyword at all.
    grouped = filter_items([_item("Rusty gates", topics=("rust",))], _COMMON_WORDS, 10)
    assert [t.uncertain for t in grouped["rust"]] == [False]


def test_entities_on_a_match_name_false_game_are_still_uncertain_hits():
    game = GameCfg(
        key="rust", name="Rust", aliases=["Rust game"], entities=["Facepunch"], match_name=False
    )
    assert build_matchers([game])["rust"].match("Facepunch hires") == "uncertain"


def test_match_name_true_behaves_as_before():
    game = GameCfg(key="rust", name="Rust", aliases=["Rust game"], entities=["Facepunch"])
    m = build_matchers([game])["rust"]
    assert m.match("Rust patch") == "confident"
    assert m.match("The Rust game") == "confident"
    assert m.match("Facepunch hires") == "uncertain"
    assert m.match("rusty") is None


def test_a_v2_topic_always_matches_its_name():
    topic = Topic(key="rust", name="Rust", channel_id=1)
    assert topic.match_name is True
    assert build_matchers([topic])["rust"].match("Rust patch") == "confident"


def test_a_v2_topic_cannot_opt_out_of_its_name(tmp_path):
    # Pinned: `match_name` on a v2 topic is dropped like any unknown key,
    # so an old-shape config can't use the feature. Only catalog games can.
    text = (
        "guild_id: 1\ndigest: {time: '09:00', timezone: UTC}\n"
        'topics: [{key: rust, name: "Rust", channel_id: 2, match_name: false}]\n'
        "sources: []\n"
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.catalog[0].match_name is True


# --- build_catalog_collectors ---


@pytest.fixture
def v3_cfg(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    return load_config(V3)


def _web_search_collectors(collectors):
    return [c for c in collectors if c.source_type == "web_search"]


def test_web_search_is_off_when_include_web_search_is_false(v3_cfg):
    got = build_catalog_collectors(v3_cfg, _secrets("k"), include_web_search=False)
    assert _web_search_collectors(got) == []


def test_web_search_needs_the_secret_even_if_the_config_block_exists(v3_cfg, monkeypatch):
    # Config keys off the environment; the builder keys off the Secrets it's
    # handed. A missing Secrets key wins: no key, no collector.
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    assert _web_search_collectors(build_catalog_collectors(v3_cfg, _secrets(None))) == []


def test_web_search_is_absent_without_a_web_search_block(tmp_path):
    cfg = load_config(_write(tmp_path, _MIN))
    assert _web_search_collectors(build_catalog_collectors(cfg, _secrets("k"))) == []


def test_web_search_games_defaults_to_the_whole_catalog(v3_cfg):
    (ws,) = _web_search_collectors(build_catalog_collectors(v3_cfg, _secrets("k")))
    assert [g.key for g in ws._topics] == ["borderlands4", "palworld", "rust"]


def test_web_search_games_narrows_the_search(v3_cfg):
    chosen = [g for g in v3_cfg.catalog if g.key == "palworld"]
    got = build_catalog_collectors(v3_cfg, _secrets("k"), web_search_games=chosen)
    (ws,) = _web_search_collectors(got)
    assert [g.key for g in ws._topics] == ["palworld"]


def test_an_empty_web_search_games_list_still_builds_a_collector_that_searches_nothing(v3_cfg):
    # Pinned: [] is "search no games", not "default to all". The collector
    # exists (and will report a healthy, empty run) but makes no requests.
    got = build_catalog_collectors(v3_cfg, _secrets("k"), web_search_games=[])
    (ws,) = _web_search_collectors(got)
    assert list(ws._topics) == []


def test_web_search_games_without_include_web_search_is_ignored(v3_cfg):
    got = build_catalog_collectors(
        v3_cfg, _secrets("k"), include_web_search=False, web_search_games=v3_cfg.catalog
    )
    assert _web_search_collectors(got) == []


def test_web_search_carries_the_global_block_settings(tmp_path):
    text = _MIN + 'web_search: {name: "Brave", queries_per_game: 1, trust: community}\n'
    cfg = load_config(_write(tmp_path, text))
    (ws,) = _web_search_collectors(build_catalog_collectors(cfg, _secrets("k")))
    assert (ws.name, ws._source.queries_per_topic, ws._source.trust) == ("Brave", 1, "community")


def test_a_game_with_no_search_queries_falls_back_to_the_templates(v3_cfg):
    (ws,) = _web_search_collectors(build_catalog_collectors(v3_cfg, _secrets("k")))
    palworld = next(g for g in v3_cfg.catalog if g.key == "palworld")
    assert ws._queries_for(palworld) == ["Palworld news", "Palworld update OR patch OR leak"]


def test_a_game_own_queries_are_capped_by_queries_per_game(tmp_path):
    text = (
        'catalog:\n  - {key: a, name: "A", search_queries: [q1, q2, q3]}\n'
        "web_search: {queries_per_game: 2}\n"
    )
    cfg = load_config(_write(tmp_path, text))
    (ws,) = _web_search_collectors(build_catalog_collectors(cfg, _secrets("k")))
    assert ws._queries_for(cfg.catalog[0]) == ["q1", "q2"]


def test_a_match_name_false_game_without_search_queries_searches_its_bare_name(v3_cfg):
    # Pinned, and one the owner may want changed: `match_name: false` keeps
    # "Rust" out of the *matcher*, but web search still asks Brave for
    # "Rust news". Give such games search_queries, or accept the noise.
    (ws,) = _web_search_collectors(build_catalog_collectors(v3_cfg, _secrets("k")))
    rust = next(g for g in v3_cfg.catalog if g.key == "rust")
    assert ws._queries_for(rust)[0] == "Rust news"


def test_collector_names_are_unique_for_a_valid_v3_config(v3_cfg):
    names = [c.name for c in build_catalog_collectors(v3_cfg, _secrets("k"))]
    assert len(names) == len(set(names))


def test_building_collectors_leaves_the_config_untouched(v3_cfg):
    before = v3_cfg.model_dump()
    build_catalog_collectors(v3_cfg, _secrets("k"))
    assert v3_cfg.model_dump() == before
    assert all(s.topics is None for g in v3_cfg.catalog for s in g.sources)


def test_a_game_source_tags_only_its_own_game_not_a_shared_list(v3_cfg):
    built = {c.name: c for c in build_catalog_collectors(v3_cfg, _secrets())}
    assert built["Borderlands 4 Steam"]._source.topics == ["borderlands4"]
    # Mutating one collector's list must not reach another game's source.
    built["Borderlands 4 Steam"]._source.topics.append("x")
    assert built["Palworld Steam"]._source.topics == ["palworld"]


def test_two_games_listing_the_same_feed_under_different_names_fetch_it_twice(tmp_path):
    # Pinned: no URL dedupe. Hourly collection would hit the feed once per
    # listing; the owner's catalog research should avoid the pattern.
    text = """
catalog:
  - key: a
    name: "A"
    sources: [{type: rss, name: "A feed", url: "https://example.com/f", trust: press}]
  - key: b
    name: "B"
    sources: [{type: rss, name: "B feed", url: "https://example.com/f", trust: press}]
"""
    cfg = load_config(_write(tmp_path, text))
    urls = [str(c._source.url) for c in build_catalog_collectors(cfg, _secrets())]
    assert urls == ["https://example.com/f", "https://example.com/f"]


def test_a_shared_and_a_game_source_with_one_name_collide_at_load_not_at_build(tmp_path):
    text = (
        'catalog:\n  - key: a\n    name: "A"\n'
        "    sources: [{type: steam_news, name: Dup, app_id: 1, trust: official}]\n"
        'shared_sources:\n  - {type: rss, name: Dup, url: "https://example.com/f", trust: press}\n'
    )
    assert "duplicate source name 'Dup'" in _errors(tmp_path, text)


def test_bluesky_default_names_are_filled_for_shared_sources_too(tmp_path):
    text = _MIN + 'shared_sources:\n  - {type: bluesky_search, query: "q", trust: community}\n'
    cfg = load_config(_write(tmp_path, text))
    assert [c.name for c in build_catalog_collectors(cfg, _secrets())] == ["Bluesky: q"]


def test_derived_prodlike_config_builds_unique_collector_names(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    cfg = load_config(PRODLIKE)
    names = [c.name for c in build_catalog_collectors(cfg, _secrets("k"))]
    assert len(names) == len(set(names))
