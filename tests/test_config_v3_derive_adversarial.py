"""Adversarial tests for derive mode: does the derived catalog mean what v2 meant?

Angle (public app, task 1, test-engineer brief 2026-09-30): the friend's
server runs on a v2 config that nobody is going to rewrite for me, so the
derived catalog has to carry every topic, every source assignment and every
match rule across without a single opinion of its own. The central test
builds collectors the old way (`_old_build_collectors`, a copy of v2's
`build_collectors` kept in this file as the oracle once the real one retired
at the cutover) and the new way (`build_catalog_collectors`) from one file and
compares what comes out, by name, type, endpoint and game tag. If those two
ever disagree, the running bot changes behavior on upgrade, which is the one
thing this task must not do.

Where derive mode differs from v2 on purpose, the test pins the difference
and says so. Where it differs by accident, the test is `xfail(strict=True)`.
No network; everything lives in tmp_path.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from newsbot.collectors.base import RawItem, build_catalog_collectors
from newsbot.config import (
    BlueskySource,
    ConfigError,
    RssSource,
    Secrets,
    SteamSource,
    WebSearchSource,
    _RawConfig,
    _with_default_name,
    is_reddit_source,
    load_config,
)
from newsbot.lounge.default_sources import DEFAULT_WIKIQUOTE_PAGES
from newsbot.pipeline.filter import build_matchers, filter_items

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
PRODLIKE = FIXTURES / "config_v2_prodlike.yaml"
# v3's own examples are catalog-shaped, so these tests use the pre-v3 shipped examples,
# kept verbatim as fixtures: they are what an untouched v2 config looks like to the loader.
EXAMPLE = FIXTURES / "config_v2_example.yaml"
MINIMAL = FIXTURES / "config_v2_minimal.yaml"
VALID = FIXTURES / "config_valid.yaml"

_HEAD = """
guild_id: 1
digest: {time: "09:00", timezone: UTC%s}
topics:
  - {key: a, name: "Alpha", channel_id: 11, aliases: ["A1"], entities: ["AlphaCorp"]}
  - {key: b, name: "Beta", channel_id: 12, search_queries: ["beta q1", "beta q2", "beta q3"]}
  - {key: c, name: "Gamma", channel_id: 13}
"""


def _v2(tmp_path: Path, sources: str, digest_extra: str = "", tail: str = "") -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(_HEAD % digest_extra + "sources:\n" + sources + tail)
    return p


def _secrets(brave: bool) -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key="x",
        brave_api_key="k" if brave else None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _endpoint(c) -> object:
    src = c._source
    if c.source_type == "rss":
        return str(src.url)
    if c.source_type == "steam_news":
        return src.app_id
    if c.source_type == "bluesky_search":
        return src.query
    return (src.queries_per_topic, tuple(src.query_templates))


def _signature(c) -> tuple:
    """Everything an outsider can observe about a collector: identity, endpoint, tagging, trust."""
    if c.source_type == "web_search":
        tag = tuple(t.key for t in c._topics)
    else:
        tag = tuple(c._source.topics) if c._source.topics else None
    return (c.name, c.source_type, _endpoint(c), tag, c._source.trust)


def _raw(path) -> _RawConfig:
    """The v2 keys as the file spells them, before the loader derives anything.

    Until the cutover these were `cfg.topics`, `cfg.sources` and friends on the loaded config,
    and they were the yardstick every derived value was measured against. The loaded config
    doesn't carry them any more, so the yardstick parses the file itself.
    """
    import yaml

    return _RawConfig.model_validate(yaml.safe_load(Path(path).read_text()))


def _skipped_since_v303(raw, source) -> bool:
    """v3.0.3's one deliberate difference from v2: a Reddit feed for a game that has no SHiFT codes.

    Reddit is only read to spot codes now, so a subreddit whose game isn't in `alerts.topics`
    (when that list is non-empty; empty means every game) is warned about and not fetched.
    """
    if not is_reddit_source(source) or not raw.alerts.topics:
        return False
    return not set(source.topics or []).intersection(raw.alerts.topics) and bool(source.topics)


def _old_build_collectors(path, secrets, *, include_web_search=True):
    """v2's `build_collectors`, as it was, reading the old keys straight from the file.

    The real one was deleted at the cutover (the v2 fields left `AppConfig`). It lives on here
    as the oracle for the one claim this file exists to make: a derived catalog builds the
    collectors the old config would have.
    """
    from newsbot.collectors.bluesky import BlueskyCollector, BlueskySession
    from newsbot.collectors.rss import RssCollector
    from newsbot.collectors.steam import SteamCollector
    from newsbot.collectors.web_search import WebSearchCollector

    raw = _raw(path)
    session = None
    if secrets.bluesky_handle and secrets.bluesky_app_password:
        session = BlueskySession(
            secrets.bluesky_handle, secrets.bluesky_app_password.get_secret_value()
        )
    collectors = []
    for source in (_with_default_name(src) for src in raw.sources):
        if _skipped_since_v303(raw, source):
            continue
        if isinstance(source, RssSource):
            collectors.append(RssCollector(source))
        elif isinstance(source, SteamSource):
            collectors.append(SteamCollector(source))
        elif isinstance(source, BlueskySource):
            collectors.append(BlueskyCollector(source, session))
        elif isinstance(source, WebSearchSource):
            if not include_web_search or not secrets.brave_api_key:
                continue
            collectors.append(
                WebSearchCollector(source, raw.topics, secrets.brave_api_key.get_secret_value())
            )
    return collectors


def _sigs(collectors) -> list[tuple]:
    return sorted((_signature(c) for c in collectors), key=repr)


_SOURCES = """
  - {type: steam_news, name: "A Steam", app_id: 1, topics: [a], trust: official}
  - {type: rss, name: "B Feed", url: "https://example.com/b", topics: [b], trust: official}
  - {type: rss, name: "Wire", url: "https://example.com/w", topics: [a, b], trust: press}
  - {type: rss, name: "Everything", url: "https://example.com/e", trust: press}
  - {type: rss, name: "Empty Scope", url: "https://example.com/z", topics: [], trust: press}
  - {type: rss, name: "Twice", url: "https://example.com/t", topics: [c, c], trust: press}
  - {type: rss, name: "Triple", url: "https://example.com/3", topics: [a, b, c], trust: press}
  - {type: bluesky_search, query: "alpha", topics: [a], trust: community}
  - {type: bluesky_search, query: "anything", trust: community}
  - {type: web_search, queries_per_topic: 1, query_templates: ["{name} x"], trust: press}
"""

_VARIANTS = {
    "prodlike": lambda tmp: PRODLIKE,
    "example": lambda tmp: EXAMPLE,
    "minimal": lambda tmp: MINIMAL,
    "valid_fixture": lambda tmp: VALID,
    "every_scope_shape": lambda tmp: _v2(tmp, _SOURCES),
    "no_sources_at_all": lambda tmp: _v2(tmp, "  []\n"),
    "only_web_search": lambda tmp: _v2(tmp, "  - {type: web_search, trust: press}\n"),
    "only_shared": lambda tmp: _v2(
        tmp, '  - {type: rss, name: "S", url: "https://example.com/s", trust: press}\n'
    ),
}


@pytest.fixture(params=sorted(_VARIANTS))
def v2_path(request, tmp_path):
    return _VARIANTS[request.param](tmp_path)


@pytest.fixture(params=[True, False], ids=["brave", "no_brave"])
def brave(request, monkeypatch):
    if request.param:
        monkeypatch.setenv("BRAVE_API_KEY", "k")
    else:
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    return request.param


# --- the key check: same collectors, old builder and new ---


def test_catalog_collectors_equal_the_old_collectors_by_name_type_endpoint_and_tag(v2_path, brave):
    cfg = load_config(v2_path)
    old = _old_build_collectors(v2_path, _secrets(brave))
    new = build_catalog_collectors(cfg, _secrets(brave))
    assert _sigs(new) == _sigs(old)


def test_the_same_holds_with_web_search_switched_off(v2_path, brave):
    cfg = load_config(v2_path)
    old = _old_build_collectors(v2_path, _secrets(brave), include_web_search=False)
    new = build_catalog_collectors(cfg, _secrets(brave), include_web_search=False)
    assert _sigs(new) == _sigs(old)


def test_web_search_sees_the_same_games_with_the_same_queries(v2_path, brave):
    cfg = load_config(v2_path)
    old_ws = [
        c for c in _old_build_collectors(v2_path, _secrets(brave)) if c.source_type == "web_search"
    ]
    new_ws = [
        c for c in build_catalog_collectors(cfg, _secrets(brave)) if c.source_type == "web_search"
    ]
    assert len(old_ws) == len(new_ws)
    for old, new in zip(old_ws, new_ws, strict=True):
        assert [old._queries_for(t) for t in old._topics] == [
            new._queries_for(g) for g in new._topics
        ]


def test_derived_collector_names_are_unique(v2_path, brave):
    names = [c.name for c in build_catalog_collectors(load_config(v2_path), _secrets(brave))]
    assert len(names) == len(set(names))


# --- match behavior ---


def _battery(topics) -> list[RawItem]:
    """Headlines aimed at every name, alias and entity, plus prose that should hit nothing."""
    words: list[str] = ["a quiet news day", "Hello, world", "  ", ""]
    for t in topics:
        words += [t.name, t.name.lower(), f"{t.name}'s big patch", f"pre{t.name}post"]
        words += list(t.aliases) + list(t.entities)
    scopes: list[tuple[str, ...] | None] = [None]
    scopes += [(t.key,) for t in topics]
    scopes.append(tuple(t.key for t in topics[:2]))
    return [
        RawItem(
            url=f"https://example.com/{i}-{j}",
            title=w,
            excerpt=w,
            source_name="S",
            trust="press",
            published_at=None,
            topics=scope,
        )
        for i, w in enumerate(words)
        for j, scope in enumerate(scopes)
    ]


def test_derived_matchers_are_the_v2_matchers(v2_path):
    cfg = load_config(v2_path)
    old = build_matchers(_raw(v2_path).topics)
    new = build_matchers(cfg.catalog)
    assert list(new) == list(old)
    for key in old:
        assert new[key] == old[key], key


def test_filtering_a_battery_of_items_is_identical_under_the_derived_catalog(v2_path):
    cfg = load_config(v2_path)
    topics = _raw(v2_path).topics
    items = _battery(topics)
    cap = cfg.collection.max_items_per_game
    got = filter_items(items, cfg.catalog, cap)
    assert got == filter_items(items, topics, cap)
    assert got, "the battery should hit something, or this test proves nothing"


def test_every_derived_game_matches_by_name_because_v2_topics_always_did(v2_path):
    assert all(g.match_name for g in load_config(v2_path).catalog)


def test_derived_games_carry_topic_fields_verbatim(v2_path):
    cfg = load_config(v2_path)
    topics = _raw(v2_path).topics
    assert [g.key for g in cfg.catalog] == [t.key for t in topics]
    for game, topic in zip(cfg.catalog, topics, strict=True):
        assert (game.name, game.aliases, game.entities, game.search_queries) == (
            topic.name,
            topic.aliases,
            topic.entities,
            topic.search_queries,
        )


def test_every_v2_source_lands_in_exactly_one_place(v2_path, brave):
    cfg = load_config(v2_path)
    placed = [s.name for g in cfg.catalog for s in g.sources] + [s.name for s in cfg.shared_sources]
    raw = _raw(v2_path)
    v2_names = [
        _with_default_name(s).name
        for s in raw.sources
        if s.type != "web_search" and not _skipped_since_v303(raw, s)
    ]
    assert sorted(placed) == sorted(v2_names)


def test_assignment_rules_on_every_scope_shape(tmp_path):
    cfg = load_config(_v2(tmp_path, _SOURCES))
    games = {g.key: [s.name for s in g.sources] for g in cfg.catalog}
    assert games == {
        "a": ["A Steam", "Bluesky: alpha"],
        "b": ["B Feed"],
        "c": [],
    }
    shared = {s.name: s.games for s in cfg.shared_sources}
    assert shared == {
        "Wire": ["a", "b"],
        "Everything": None,
        "Empty Scope": None,
        "Twice": ["c", "c"],
        "Triple": ["a", "b", "c"],
        "Bluesky: anything": None,
    }


def test_a_source_scoped_to_one_topic_twice_stays_shared_with_its_restriction(tmp_path):
    # Pinned: `topics: [c, c]` is not "exactly one key", so it's shared and
    # keyword-matched, as v2's filter always did (dedicated needs len == 1).
    path = _v2(
        tmp_path,
        "  - {type: rss, name: T, url: 'https://e.com/t', topics: [c, c], trust: press}\n",
    )
    cfg = load_config(path)
    (twice,) = cfg.shared_sources
    assert twice.games == ["c", "c"]
    item = RawItem("https://e.com/1", "unrelated", "", "T", "press", None, ("c", "c"))
    grouped = filter_items([item], cfg.catalog, 60)
    assert grouped == filter_items([item], _raw(path).topics, 60) == {}


# --- errors in derive mode must stay ConfigErrors ---


def test_a_source_scoped_to_an_unknown_topic_is_a_config_error_not_a_key_error(tmp_path):
    src = '  - {type: rss, name: S, url: "https://example.com/s", topics: [ghost], trust: press}\n'
    with pytest.raises(ConfigError) as exc_info:
        load_config(_v2(tmp_path, src))
    assert "source 'S' references unknown topic 'ghost'" in str(exc_info.value)


def test_a_source_scoped_to_an_unknown_and_a_known_topic_is_a_config_error(tmp_path):
    src = (
        '  - {type: rss, name: S, url: "https://example.com/s", topics: [a, ghost], trust: press}\n'
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(_v2(tmp_path, src))
    assert "unknown topic 'ghost'" in str(exc_info.value)


def test_a_duplicate_topic_key_in_derive_mode_is_a_config_error(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "guild_id: 1\ndigest: {time: '09:00', timezone: UTC}\n"
        "topics: [{key: a, name: A, channel_id: 2}, {key: a, name: B, channel_id: 3}]\n"
        "sources:\n  - {type: steam_news, name: S, app_id: 1, topics: [a], trust: official}\n"
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    assert "duplicate topic key 'a'" in str(exc_info.value)


def test_25_topics_is_still_over_the_v2_limit_even_though_the_catalog_takes_25(tmp_path):
    # Pinned: derive mode keeps v2's 24-topic rule (the "All" choice), so a
    # 25-game catalog only works written as catalog:, never as topics:.
    topics = ", ".join(f"{{key: t{i}, name: T{i}, channel_id: {i + 1}}}" for i in range(25))
    p = tmp_path / "c.yaml"
    p.write_text(
        f"guild_id: 1\ndigest: {{time: '09:00', timezone: UTC}}\ntopics: [{topics}]\nsources: []\n"
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    assert "25 topics exceeds the limit of 24" in str(exc_info.value)


def test_a_v2_error_and_a_derive_time_condition_arrive_together(tmp_path):
    p = _v2(tmp_path, "  []\n", tail="shared_sources: []\nalerts: {enabled: true}\n")
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    msg = str(exc_info.value)
    assert "shared_sources needs a catalog:" in msg
    assert "alerts.channel_id is required" in msg


# --- values v2 accepted must still load (the running bot's upgrade path) ---
# A config that loaded under v2.2 has to load under v3, or the friend's bot
# refuses to start after an upgrade nobody asked for. The derived blocks
# have tighter numeric bounds than the v2 fields they're built from; when
# the two disagree the loader dies with pydantic's raw ValidationError,
# which is neither a load nor a ConfigError.

_V2_ZERO_CASES = {
    "lookback_hours_0": (_HEAD % ", lookback_hours: 0" + "sources: []\n"),
    "max_items_per_topic_0": (_HEAD % ", max_items_per_topic: 0" + "sources: []\n"),
    "queries_per_topic_0": (
        _HEAD % "" + "sources:\n  - {type: web_search, queries_per_topic: 0, trust: press}\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_V2_ZERO_CASES))
def test_v2_zero_values_still_load_clamped_to_one_with_a_warning(tmp_path, case, caplog):
    # v2.2 accepted these and, at runtime, quietly collected nothing (nothing
    # is "recent", the cap slices to [], the query list slices to []). v3 can't
    # say "nothing", so the loader clamps to 1 and warns naming the key.
    p = tmp_path / "c.yaml"
    p.write_text(_V2_ZERO_CASES[case])
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(p)
    key, derived = {
        "lookback_hours_0": ("digest.lookback_hours", cfg.collection.lookback_hours),
        "max_items_per_topic_0": ("digest.max_items_per_topic", cfg.collection.max_items_per_game),
        "queries_per_topic_0": (
            "queries_per_topic",
            cfg.web_search and cfg.web_search.queries_per_game,
        ),
    }[case]
    assert derived == 1
    assert f"{key} is 0" in caplog.text


def test_a_negative_v2_lookback_is_clamped_too(tmp_path, caplog):
    p = tmp_path / "c.yaml"
    p.write_text(_HEAD % ", lookback_hours: -5" + "sources: []\n")
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(p)
    assert cfg.collection.lookback_hours == 1
    # The v2 view keeps what the file said; only the derived block is clamped.
    assert _raw(p).digest.lookback_hours == -5
    assert "digest.lookback_hours is -5" in caplog.text


# --- scalar mapping: digest, alerts, lounge ---


def test_derive_maps_alerts_interval_to_collection_interval(tmp_path):
    # Pinned: the collection cadence is the *alerts* sweep interval, even
    # when alerts are disabled (v2's only hourly loop), and 60 when absent.
    p = _v2(tmp_path, "  []\n", tail="alerts: {enabled: false, interval_minutes: 45}\n")
    assert load_config(p).collection.interval_minutes == 45
    assert load_config(_v2(tmp_path, "  []\n")).collection.interval_minutes == 60


def test_derive_maps_every_digest_and_alerts_field(tmp_path):
    p = _v2(
        tmp_path,
        "  []\n",
        digest_extra=(
            ", lookback_hours: 36, max_items_per_topic: 9, report_to_admin: false, subject: cards"
        ),
        tail=(
            "alerts:\n  enabled: true\n  channel_id: 5\n  topics: [a, c]\n"
            "  interval_minutes: 20\n  max_item_age_hours: 12\n  max_pings_per_day: 0\n"
            "  ping_trust: [official]\n  max_codes_per_item: 2\n  allow_test_command: true\n"
        ),
    )
    cfg = load_config(p)
    assert (cfg.collection.lookback_hours, cfg.collection.max_items_per_game) == (36, 9)
    assert cfg.collection.interval_minutes == 20
    assert (cfg.ai.subject, cfg.run_report) == ("cards", False)
    assert cfg.shift.model_dump() == {
        "games": ["a", "c"],
        "max_item_age_hours": 12,
        "max_pings_per_day": 0,
        "ping_trust": ["official"],
        "max_codes_per_item": 2,
        "allow_test_command": True,
    }


def test_derive_lookback_and_cap_match_what_the_v2_digest_used(tmp_path):
    path = _v2(tmp_path, "  []\n", digest_extra=", lookback_hours: 6")
    cfg, digest = load_config(path), _raw(path).digest
    assert digest is not None
    assert cfg.collection.lookback_hours == digest.lookback_hours == 6
    assert cfg.collection.max_items_per_game == digest.max_items_per_topic


def test_derive_without_alerts_gives_the_v2_default_shift(tmp_path):
    cfg = load_config(_v2(tmp_path, "  []\n"))
    assert cfg.shift.games == []
    assert cfg.legacy is not None and cfg.legacy.shift_enabled is False


# --- explicit v3 blocks in a v2-shaped file ---


def test_an_explicit_collection_block_replaces_the_derived_one_wholesale(tmp_path):
    # Pinned, and one the owner may want changed: explicit blocks don't merge
    # field by field. A digest.lookback_hours of 48 is dropped the moment a
    # collection: block mentions any other field.
    p = _v2(
        tmp_path,
        "  []\n",
        digest_extra=", lookback_hours: 48",
        tail="collection: {interval_minutes: 30}\n",
    )
    cfg = load_config(p)
    assert cfg.collection.interval_minutes == 30
    assert cfg.collection.lookback_hours == 24


def test_an_explicit_web_search_block_beats_the_v2_web_search_source(tmp_path):
    src = "  - {type: web_search, name: Old, queries_per_topic: 5, trust: community}\n"
    cfg = load_config(_v2(tmp_path, src, tail='web_search: {name: "New", queries_per_game: 1}\n'))
    assert cfg.web_search is not None
    assert (cfg.web_search.name, cfg.web_search.queries_per_game) == ("New", 1)


def test_an_explicit_null_web_search_switches_the_derived_one_off(tmp_path):
    # Pinned: `web_search: ~` is "explicitly none", not "absent", so the v2
    # Brave source is not derived. (The v2 sources list still names it.)
    src = "  - {type: web_search, trust: press}\n"
    cfg = load_config(_v2(tmp_path, src, tail="web_search: ~\n"))
    assert cfg.web_search is None


def test_an_explicit_shift_block_replaces_alerts_derivation(tmp_path):
    p = _v2(tmp_path, "  []\n", tail="alerts: {topics: [a]}\nshift: {games: [b]}\n")
    assert load_config(p).shift.games == ["b"]


def test_an_explicit_shift_block_naming_an_unknown_game_is_an_error_in_derive_mode(tmp_path):
    # The cross-check now runs in derive mode for an explicit shift: block.
    with pytest.raises(ConfigError):
        load_config(_v2(tmp_path, "  []\n", tail="shift: {games: [ghost]}\n"))


def test_an_explicit_ai_block_beats_digest_subject(tmp_path):
    p = _v2(tmp_path, "  []\n", digest_extra=", subject: cards", tail='ai: {subject: "dice"}\n')
    assert load_config(p).ai.subject == "dice"


def test_an_explicit_run_report_beats_report_to_admin(tmp_path):
    p = _v2(tmp_path, "  []\n", digest_extra=", report_to_admin: true", tail="run_report: false\n")
    assert load_config(p).run_report is False


def test_an_explicit_home_guild_id_beats_the_derived_default(tmp_path):
    assert load_config(_v2(tmp_path, "  []\n", tail="home_guild_id: 99\n")).home_guild_id == 99


def test_comped_and_command_guild_ids_work_in_derive_mode(tmp_path):
    p = _v2(tmp_path, "  []\n", tail="comped_guild_ids: [7]\ncommand_guild_ids: [8]\n")
    cfg = load_config(p)
    assert (cfg.comped_guild_ids, cfg.command_guild_ids) == ([7], [8])


def test_shared_sources_in_a_v2_file_is_rejected_even_when_empty(tmp_path):
    p = _v2(tmp_path, "  []\n", tail="shared_sources: []\n")
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    assert "shared_sources needs a catalog:" in str(exc_info.value)


# --- multiple web_search sources ---


def _two_brave(tmp_path: Path) -> Path:
    src = (
        "  - {type: web_search, name: First, queries_per_topic: 1, trust: press}\n"
        "  - {type: web_search, name: Second, queries_per_topic: 4, trust: community}\n"
    )
    return _v2(tmp_path, src)


def test_the_first_of_several_web_search_sources_wins_and_the_second_is_named_in_a_warning(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(_two_brave(tmp_path))
    assert cfg.web_search is not None
    assert (cfg.web_search.name, cfg.web_search.queries_per_game) == ("First", 1)
    (warning,) = [
        r.getMessage() for r in caplog.records if "more than one web_search" in r.getMessage()
    ]
    assert "'Second'" in warning


def test_two_web_search_sources_build_one_catalog_collector_where_v2_built_two(
    tmp_path, monkeypatch
):
    # Pinned difference, on purpose: v3 has one global Brave block, so the
    # running bot's second Brave source (if anyone ever had one) stops
    # searching after the cutover. The warning above is the only notice.
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    path = _two_brave(tmp_path)
    cfg = load_config(path)
    old = [
        c.name for c in _old_build_collectors(path, _secrets(True)) if c.source_type == "web_search"
    ]
    new = [
        c.name
        for c in build_catalog_collectors(cfg, _secrets(True))
        if c.source_type == "web_search"
    ]
    assert (old, new) == (["First", "Second"], ["First"])


def test_two_web_search_sources_without_a_key_warn_about_the_key_not_twice(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        load_config(_two_brave(tmp_path))
    assert sum("BRAVE_API_KEY is not set" in r.getMessage() for r in caplog.records) == 2


# --- the legacy view in derive mode ---


def test_legacy_games_keep_topic_order_and_channels(tmp_path):
    cfg = load_config(_v2(tmp_path, "  []\n"))
    assert cfg.legacy is not None
    assert cfg.legacy.games == [("a", 11), ("b", 12), ("c", 13)]


def test_legacy_lounge_is_present_in_derive_mode_when_the_block_is(tmp_path):
    cfg = load_config(_v2(tmp_path, "  []\n", tail="lounge: {}\n"))
    assert cfg.legacy is not None and cfg.legacy.lounge is not None


def test_legacy_lounge_is_none_in_derive_mode_when_the_block_is_absent(tmp_path):
    cfg = load_config(_v2(tmp_path, "  []\n"))
    assert cfg.legacy is not None and cfg.legacy.lounge is None


def test_legacy_admin_channel_is_the_top_level_one(tmp_path):
    cfg = load_config(_v2(tmp_path, "  []\n", tail="admin_channel_id: 77\n"))
    assert cfg.legacy is not None and cfg.legacy.admin_channel_id == 77


def test_legacy_lounge_matches_the_v2_lounge_object(tmp_path):
    path = _v2(
        tmp_path,
        "  []\n",
        tail="lounge:\n  channel_id: 5\n  welcome: {enabled: true, message: 'Hi {member}'}\n",
    )
    cfg, raw = load_config(path), _raw(path).lounge
    assert cfg.legacy is not None
    # The loader resolves the quote sources (the file named none, so: the built-in list);
    # everything the file did say comes through untouched.
    assert (cfg.legacy.lounge.channel_id, cfg.legacy.lounge.welcome) == (
        raw.channel_id,
        raw.welcome,
    )
    assert raw.daily_quote.sources is None
    assert [s.value for s in cfg.legacy.lounge.daily_quote.sources] == list(DEFAULT_WIKIQUOTE_PAGES)


@pytest.mark.parametrize("n, expected", [(1, "everyone"), (3, "everyone"), (0, "none")])
def test_shift_ping_is_everyone_for_any_positive_cap(tmp_path, n, expected):
    p = _v2(tmp_path, "  []\n", tail=f"alerts: {{max_pings_per_day: {n}}}\n")
    legacy = load_config(p).legacy
    assert legacy is not None
    assert (legacy.shift_ping, legacy.alerts_max_pings) == (expected, n)


def test_shift_ping_only_ever_takes_the_two_documented_values():
    from newsbot.config import LegacySetup

    assert set(LegacySetup.model_fields["shift_ping"].annotation.__args__) == {"everyone", "none"}


def test_a_disabled_alerts_block_still_reports_its_channel_and_cap(tmp_path):
    p = _v2(
        tmp_path, "  []\n", tail="alerts: {enabled: false, channel_id: 9, max_pings_per_day: 0}\n"
    )
    legacy = load_config(p).legacy
    assert legacy is not None
    assert (legacy.shift_enabled, legacy.shift_channel_id, legacy.shift_ping) == (False, 9, "none")


def test_the_shipped_examples_derive_the_same_way_twice(tmp_path):
    # Loading is pure: same file, same catalog. Guards accidental shared
    # mutable defaults (GameCfg.sources is appended to during derive).
    first = load_config(EXAMPLE)
    second = load_config(EXAMPLE)
    assert first.catalog == second.catalog
    assert first.shared_sources == second.shared_sources


def test_derive_does_not_leak_sources_between_games_or_configs(tmp_path):
    cfg = load_config(_v2(tmp_path, _SOURCES))
    other = load_config(_v2(tmp_path, "  []\n"))
    assert [s.name for g in other.catalog for s in g.sources] == []
    assert len({id(g.sources) for g in cfg.catalog}) == len(cfg.catalog)
