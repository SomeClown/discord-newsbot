"""Tests for the `lounge:` config block (design.md §14, lounge plan task 1).

Every quote-ish string here is fake on purpose (Mabel Quince has never said
anything to anyone); real quotes only ever come from saved Wikiquote pages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import ConfigError, QuoteSourceCfg, load_config
from newsbot.lounge.default_sources import DEFAULT_WIKIQUOTE_PAGES
from newsbot.lounge.welcome import (
    render_welcome,
    unknown_placeholders,
    worst_case_length,
)

FIXTURE = Path(__file__).parent / "fixtures" / "config_lounge.yaml"
VALID = Path(__file__).parent / "fixtures" / "config_valid.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
MINIMAL = Path(__file__).parent.parent / "config.minimal.yaml"

_BASE = """
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
    topics: [palworld]
    trust: official
"""


def _load(tmp_path: Path, lounge: str):
    p = tmp_path / "config.yaml"
    p.write_text(_BASE + lounge)
    return load_config(p)


def _errors(tmp_path: Path, lounge: str) -> str:
    with pytest.raises(ConfigError) as exc_info:
        _load(tmp_path, lounge)
    return str(exc_info.value)


def _sources_block(*lines: str) -> str:
    body = "\n".join(f"      - {line}" for line in lines)
    return f"lounge:\n  daily_quote:\n    sources:\n{body}\n"


# --- absent block and defaults ---


def test_absent_block_means_both_features_off():
    cfg = load_config(VALID)
    assert cfg.lounge.channel_id is None
    assert cfg.lounge.welcome.enabled is False
    assert cfg.lounge.daily_quote.enabled is False


def test_shipped_examples_still_load():
    load_config(EXAMPLE)
    load_config(MINIMAL)


def test_fixture_loads_with_both_features_on():
    cfg = load_config(FIXTURE)
    assert cfg.lounge.channel_id == 123456789012345700
    assert cfg.lounge.welcome.enabled and cfg.lounge.daily_quote.enabled
    kinds = [s.kind for s in cfg.lounge.daily_quote.sources]
    assert kinds == ["wikiquote", "wikiquote", "file", "url"]


def test_default_time_is_eight_am(tmp_path):
    assert _load(tmp_path, "lounge: {}\n").lounge.daily_quote.time == "08:00"


def test_sources_omitted_gives_the_default_list_in_order(tmp_path):
    cfg = _load(tmp_path, "lounge:\n  daily_quote:\n    enabled: false\n")
    sources = cfg.lounge.daily_quote.sources
    assert [s.value for s in sources] == list(DEFAULT_WIKIQUOTE_PAGES)
    assert {s.kind for s in sources} == {"wikiquote"}


def test_sources_null_is_the_same_as_omitted(tmp_path):
    cfg = _load(tmp_path, "lounge:\n  daily_quote:\n    sources: null\n")
    assert [s.value for s in cfg.lounge.daily_quote.sources] == list(DEFAULT_WIKIQUOTE_PAGES)


def test_empty_sources_list_is_an_error(tmp_path):
    msg = _errors(tmp_path, "lounge:\n  daily_quote:\n    sources: []\n")
    assert (
        "lounge.daily_quote.sources is empty; remove it to use the built-in list, "
        "or set lounge.daily_quote.enabled to false"
    ) in msg


# --- source shape errors ---


def test_source_with_two_keys_lists_what_was_found(tmp_path):
    msg = _errors(tmp_path, _sources_block('{wikiquote: "A", file: b.txt}'))
    assert "lounge.daily_quote.sources[0] must have exactly one of wikiquote, file or url" in msg
    assert "(found: wikiquote, file)" in msg


@pytest.mark.parametrize("entry", ["{}", "just a string", "[a, b]"])
def test_source_that_is_not_a_one_key_mapping_fails(tmp_path, entry):
    msg = _errors(tmp_path, _sources_block(entry))
    assert "lounge.daily_quote.sources[0] must have exactly one of wikiquote, file or url" in msg


def test_unknown_source_key_is_named(tmp_path):
    msg = _errors(tmp_path, _sources_block("{feed: x}"))
    assert "sources[0] has an unknown key 'feed'; use wikiquote, file or url" in msg


@pytest.mark.parametrize("value", ['""', '"   "', "5", "[a]"])
def test_blank_or_non_string_value_fails(tmp_path, value):
    msg = _errors(tmp_path, _sources_block(f"{{wikiquote: {value}}}"))
    assert "lounge.daily_quote.sources[0].wikiquote must be a non-empty string" in msg


@pytest.mark.parametrize(
    "title",
    [
        "https://en.wikiquote.org/wiki/Oscar_Wilde",
        "http://x",
        "Bad|Title",
        "Bad<Title",
        "Bad#Title",
        "Bad[Title]",
        "Bad{Title}",
        "x" * 256,
    ],
)
def test_bad_wikiquote_titles_fail(tmp_path, title):
    msg = _errors(tmp_path, _sources_block(f'{{wikiquote: "{title}"}}'))
    assert "isn't a page title" in msg


def test_a_255_character_title_is_fine(tmp_path):
    cfg = _load(tmp_path, _sources_block(f'{{wikiquote: "{"x" * 255}"}}'))
    assert len(cfg.lounge.daily_quote.sources) == 1


@pytest.mark.parametrize("url", ["http://example.invalid/q.txt", "HTTP://x"])
def test_plain_http_url_is_rejected_by_name(tmp_path, url):
    msg = _errors(tmp_path, _sources_block(f'{{url: "{url}"}}'))
    assert "uses plain http; only https:// addresses are allowed" in msg


@pytest.mark.parametrize("url", ["ftp://example.invalid/q.txt", "https://", "example.invalid/q"])
def test_other_url_schemes_and_hostless_urls_are_rejected(tmp_path, url):
    msg = _errors(tmp_path, _sources_block(f'{{url: "{url}"}}'))
    assert "isn't an https:// address" in msg


def test_https_url_is_accepted(tmp_path):
    cfg = _load(tmp_path, _sources_block('{url: "https://example.invalid/q.txt"}'))
    assert cfg.lounge.daily_quote.sources[0].key == "url:https://example.invalid/q.txt"


def test_duplicate_after_normalization_is_an_error(tmp_path):
    msg = _errors(
        tmp_path, _sources_block('{wikiquote: "Oscar Wilde"}', '{wikiquote: "oscar_Wilde"}')
    )
    assert "lounge.daily_quote.sources[1] repeats sources[0]" in msg


def test_several_shape_problems_arrive_in_one_config_error(tmp_path):
    msg = _errors(tmp_path, _sources_block("{feed: x}", "{}", '{wikiquote: ""}'))
    assert "sources[0] has an unknown key" in msg
    assert "sources[1] must have exactly one" in msg
    assert "sources[2].wikiquote must be a non-empty string" in msg


def test_several_value_problems_arrive_in_one_config_error(tmp_path):
    msg = _errors(
        tmp_path,
        _sources_block('{url: "http://x.invalid"}', '{wikiquote: "a|b"}', '{url: "https://"}'),
    )
    assert "plain http" in msg
    assert "isn't a page title" in msg
    assert "isn't an https:// address" in msg


def test_shapes_are_checked_even_while_the_feature_is_disabled(tmp_path):
    lounge = (
        "lounge:\n  daily_quote:\n    enabled: false\n    sources:\n      - {wikiquote: 'a|b'}\n"
    )
    assert "isn't a page title" in _errors(tmp_path, lounge)


# --- keys ---


def test_key_normalizes_wikiquote_titles():
    keys = {
        QuoteSourceCfg(kind="wikiquote", value=v).key
        for v in ("Oscar Wilde", "oscar_Wilde", "  oscar   Wilde ", "Oscar_Wilde")
    }
    assert keys == {"wikiquote:Oscar Wilde"}


def test_key_is_stable_across_reordering_and_unrelated_edits(tmp_path):
    a = _load(
        tmp_path,
        "lounge:\n  channel_id: 5\n  daily_quote:\n    time: '08:00'\n    sources:\n"
        "      - {wikiquote: A}\n      - {url: 'https://x.invalid/q'}\n",
    )
    b = _load(
        tmp_path,
        "lounge:\n  channel_id: 9\n  daily_quote:\n    time: '17:30'\n    sources:\n"
        "      - {url: 'https://x.invalid/q'}\n      - {wikiquote: A}\n      - {wikiquote: B}\n",
    )
    keys_a = {s.key for s in a.lounge.daily_quote.sources}
    keys_b = {s.key for s in b.lounge.daily_quote.sources}
    assert keys_a <= keys_b


def test_relative_file_path_resolves_against_the_config_directory(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "conf"
    cfg_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (cfg_dir / "config.yaml").write_text(_BASE + _sources_block("{file: quotes.txt}"))
    monkeypatch.chdir(elsewhere)
    cfg = load_config(cfg_dir / "config.yaml")
    src = cfg.lounge.daily_quote.sources[0]
    assert src.value == str(cfg_dir.absolute() / "quotes.txt")
    assert src.key == f"file:{cfg_dir.absolute() / 'quotes.txt'}"


def test_missing_file_is_not_a_load_time_error(tmp_path):
    cfg = _load(tmp_path, _sources_block("{file: nope.txt}"))
    assert not Path(cfg.lounge.daily_quote.sources[0].value).exists()


# --- the default list ---


def test_default_list_is_nonempty_and_unique():
    assert DEFAULT_WIKIQUOTE_PAGES
    assert len(set(DEFAULT_WIKIQUOTE_PAGES)) == len(DEFAULT_WIKIQUOTE_PAGES)


def test_default_list_entries_pass_the_same_title_validation(tmp_path):
    lines = [f'{{wikiquote: "{t}"}}' for t in DEFAULT_WIKIQUOTE_PAGES]
    cfg = _load(tmp_path, _sources_block(*lines))
    assert len(cfg.lounge.daily_quote.sources) == len(DEFAULT_WIKIQUOTE_PAGES)


def test_no_modern_work_is_an_active_default_entry():
    assert "Fight Club (film)" not in DEFAULT_WIKIQUOTE_PAGES


# --- welcome, time, channel ---


@pytest.mark.parametrize(
    "block",
    [
        "lounge:\n  welcome:\n    enabled: true\n    message: hi\n",
        "lounge:\n  daily_quote:\n    enabled: true\n",
    ],
)
def test_enabled_feature_without_channel_is_an_error(tmp_path, block):
    assert "lounge.channel_id is required" in _errors(tmp_path, block)


def test_disabled_block_needs_no_channel(tmp_path):
    cfg = _load(tmp_path, "lounge:\n  welcome:\n    enabled: false\n")
    assert cfg.lounge.channel_id is None


def test_enabled_welcome_needs_a_message(tmp_path):
    msg = _errors(
        tmp_path, "lounge:\n  channel_id: 5\n  welcome:\n    enabled: true\n    message: '  '\n"
    )
    assert "lounge.welcome.message is required" in msg


@pytest.mark.parametrize("bad", ["true", "0", "-4"])
def test_bad_channel_ids_fail(tmp_path, bad):
    with pytest.raises(ConfigError):
        _load(tmp_path, f"lounge:\n  channel_id: {bad}\n")


@pytest.mark.parametrize("t", ["24:00", "8:00", "08:60", "0800", ""])
def test_bad_times_fail(tmp_path, t):
    msg = _errors(tmp_path, f"lounge:\n  daily_quote:\n    time: '{t}'\n")
    assert "is not HH:MM (24-hour)" in msg


@pytest.mark.parametrize("t", ["00:00", "23:59"])
def test_time_edges_pass(tmp_path, t):
    assert (
        _load(tmp_path, f"lounge:\n  daily_quote:\n    time: '{t}'\n").lounge.daily_quote.time == t
    )


@pytest.mark.parametrize(
    "block",
    [
        "lounge:\n  bogus: 1\n",
        "lounge:\n  welcome:\n    bogus: 1\n",
        "lounge:\n  daily_quote:\n    bogus: 1\n",
    ],
)
def test_unknown_keys_fail_at_every_level(tmp_path, block):
    with pytest.raises(ConfigError):
        _load(tmp_path, block)


def _welcome(message: str, enabled: bool = True) -> str:
    return (
        "lounge:\n  channel_id: 5\n  welcome:\n"
        f"    enabled: {str(enabled).lower()}\n    message: {message!r}\n"
    )


def test_unknown_placeholder_is_named(tmp_path):
    msg = _errors(tmp_path, _welcome("hi {memebr}"))
    assert "unknown placeholder {memebr}" in msg


def test_unknown_placeholder_fails_even_while_disabled(tmp_path):
    assert "unknown placeholder {name}" in _errors(tmp_path, _welcome("hi {name}", enabled=False))


def test_known_placeholders_pass(tmp_path):
    _load(tmp_path, _welcome("hi {member}, welcome to {server}"))


def test_length_boundary_2000_passes_and_2001_fails(tmp_path):
    _load(tmp_path, _welcome("x" * 2000))
    assert "Discord's limit is 2000" in _errors(tmp_path, _welcome("x" * 2001))


def test_ninety_mentions_blow_the_limit(tmp_path):
    assert "Discord's limit is 2000" in _errors(tmp_path, _welcome("{member}" * 90))


# --- welcome helpers ---


def test_unknown_placeholders_reports_each_once_in_order():
    assert unknown_placeholders("{b} {member} {a} {b} {server}") == ["b", "a"]


def test_render_fills_only_the_whitelist_and_never_evaluates_anything():
    out = render_welcome(
        "{member} at {server} {member.guild} {0} {x}", member_mention="<@1>", server_name="S"
    )
    assert out == "<@1> at S {member.guild} {0} {x}"


def test_render_does_not_reexpand_substituted_text():
    out = render_welcome("{server}", member_mention="<@1>", server_name="{member}")
    assert out == "{member}"


def test_worst_case_length_counts_utf16_units():
    # 23 for the mention, 200 for one hundred emoji at two units each.
    assert worst_case_length("{member}") == 23
    assert worst_case_length("{server}") == 200
    assert worst_case_length("😀") == 2
