"""Adversarial tests for the Brave topic-match fix (`RawItem.needs_match`).

The builder's tests prove an off-topic Brave result is dropped and an on-topic
one survives. These go looking for the seams around that: names that nearly
match, the same URL arriving from two games' queries or from two kinds of
source, a flag that has to survive `dataclasses.replace`, and the places that
call `filter_items` for a reason that isn't the digest (the dedupe loser pass,
`--check-sources`, the SHiFT sweep's game scope).

The contract these pin (owner-approved):

- title plus excerpt naming the game or an alias is a confident match for that game;
- naming only an entity is an uncertain match;
- naming neither is no `item_topics` row, never stored, never in a headline list;
- dedicated single-topic sources that aren't web search are unchanged;
- a Brave item is never tagged to a game other than the one whose query found it.

No network, no real sleeping, no real config: fakes and `MockTransport`.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from newsbot.collectors.base import RawItem
from newsbot.collectors.web_search import WebSearchCollector
from newsbot.config import GameCfg, Secrets, Topic, WebSearchSource, load_config
from newsbot.pipeline.collect import CollectionDeps, _store_sync, collect_web_search
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.normalize import canonicalize_items, normalize
from newsbot.pipeline.run import render_check_sources_report, run_check_sources
from newsbot.shift.decide import sightings_from_items
from newsbot.store import repo
from newsbot.store.db import connect, migrate

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)
CODE = "K4M7P-2QXZR-9TLWB-6NHVC-J3FDS"

BL4 = Topic(
    key="borderlands4",
    name="Borderlands 4",
    channel_id=1,
    aliases=["BL4", "Borderlands4"],
    entities=["Gearbox"],
)
PALWORLD = Topic(key="palworld", name="Palworld", channel_id=1, entities=["Pocketpair"])
TOPICS = [BL4, PALWORLD]

BL4_URL = "https://news.test/bl4"


def _raw(title="", excerpt="", *, topics=("borderlands4",), needs_match=True, **kw):
    fields = dict(
        url="https://x.test/a",
        source_name="Some Source",
        trust="press",
        published_at=RECENT,
    )
    fields.update(kw)
    return RawItem(title=title, excerpt=excerpt, topics=topics, needs_match=needs_match, **fields)


def _match(item: RawItem, topics=TOPICS, cap=10) -> dict[str, list[bool]]:
    """game key -> the `uncertain` flag of each match, so a test reads as a table."""
    return {
        key: [t.uncertain for t in found]
        for key, found in filter_items([item], topics, cap).items()
    }


# --- names that nearly match ---


@pytest.mark.parametrize(
    "title",
    [
        "Borderlands 4",
        "borderlands 4 patch",
        "BORDERLANDS 4 PATCH",
        "Borderlands 4's first DLC",
        "Borderlands 4’s first DLC",  # a typographic apostrophe, as news sites love
        "(Borderlands 4) review",
        "Borderlands 4: a review",
        "Is this Borderlands 4?",
        "bl4 hotfix",
        "#BL4 hotfix",
        "BL4!",
        "Borderlands4 hotfix",
        "Borderlands 4-ish chaos",
    ],
)
def test_name_and_alias_hits_are_confident_whatever_the_case_or_punctuation(title):
    assert _match(_raw(title)) == {"borderlands4": [False]}


@pytest.mark.parametrize(
    "title",
    [
        "Borderlands 40 is a fan mod",
        "Borderlands 4x speedrun",
        "BL4X weekly",
        "xBL4 is a gamertag",
        "BL44 is a model number",
        "_BL4_ snake case",
        "Borderlands 3 patch notes",
        "Borderlands",
        "Border lands 4",
        "BL 4",
    ],
)
def test_near_misses_are_not_a_match(title):
    assert _match(_raw(title)) == {}


def test_a_name_split_across_title_and_excerpt_is_not_the_name():
    """Title ends in "Borderlands", excerpt opens with "4 reasons": that is not the game."""
    item = _raw("Why I keep going back to Borderlands", "4 reasons the loot chase works")
    assert _match(item) == {}


def test_a_name_only_in_the_url_does_not_count():
    item = _raw("Patch notes", "Fixes for the loot goblins", url="https://x.test/borderlands-4-bl4")
    assert _match(item) == {}


def test_a_name_in_a_non_breaking_space_title_still_counts():
    """Titles aren't cleaned like excerpts; Brave hands them over as the site wrote them."""
    assert _match(_raw("Borderlands 4 gets a patch")) == {"borderlands4": [False]}


def test_a_name_with_a_doubled_space_still_counts():
    assert _match(_raw("Borderlands  4 gets a patch")) == {"borderlands4": [False]}


@pytest.mark.parametrize("excerpt", ["", "   ", None])
def test_empty_or_missing_excerpt_leaves_the_title_to_decide(excerpt):
    assert _match(_raw("Borderlands 4 gets a patch", excerpt)) == {"borderlands4": [False]}
    assert _match(_raw("Patch notes", excerpt)) == {}


def test_both_empty_is_no_match_and_no_crash():
    assert _match(_raw("", "")) == {}


def test_a_hit_inside_the_excerpt_alone_is_confident():
    assert _match(_raw("Patch notes", "co-op fixes for BL4 players")) == {"borderlands4": [False]}


def test_an_entity_beside_an_unrelated_name_is_uncertain_not_confident():
    item = _raw("Gearbox: no news on Borderlands 3 this week")
    assert _match(item) == {"borderlands4": [True]}


def test_name_beats_entity_when_both_are_present():
    assert _match(_raw("Gearbox ships Borderlands 4 hotfix")) == {"borderlands4": [False]}


# --- an item for game A that names game B ---


def test_a_bl4_query_result_naming_palworld_is_tagged_to_neither():
    assert _match(_raw("Palworld update lands", "Pocketpair patches crashes")) == {}


def test_a_bl4_query_result_naming_both_is_tagged_only_to_bl4():
    item = _raw("Palworld and Borderlands 4 both patch")
    assert _match(item) == {"borderlands4": [False]}


def test_a_bl4_query_result_with_bl4_entity_and_palworld_name_is_only_uncertain_bl4():
    assert _match(_raw("Gearbox fans try Palworld")) == {"borderlands4": [True]}


def test_a_palworld_query_result_cannot_be_tagged_bl4_by_naming_it():
    assert _match(_raw("Borderlands 4 patch", topics=("palworld",))) == {}


# --- topics shapes ---


def test_needs_match_with_two_topics_checks_each_by_keyword():
    item = _raw("Palworld patch", topics=("borderlands4", "palworld"))
    assert _match(item) == {"palworld": [False]}


def test_needs_match_with_two_topics_and_no_name_matches_nothing():
    assert _match(_raw("Modern Warfare 4", topics=("borderlands4", "palworld"))) == {}


def test_needs_match_with_no_topics_is_checked_against_every_game():
    item = _raw("Borderlands 4 vs Palworld", topics=None)
    assert _match(item) == {"borderlands4": [False], "palworld": [False]}
    assert _match(_raw("Modern Warfare 4", topics=None)) == {}


def test_needs_match_with_an_empty_topics_tuple_behaves_like_none():
    assert _match(_raw("Palworld patch", topics=())) == {"palworld": [False]}


def test_needs_match_for_a_game_outside_the_catalog_matches_nothing():
    assert _match(_raw("Borderlands 4 patch", topics=("ghost",))) == {}


def test_a_dedicated_item_for_a_game_outside_the_catalog_still_matches_nothing():
    assert _match(_raw("Borderlands 4 patch", topics=("ghost",), needs_match=False)) == {}


def test_two_topic_dedicated_item_is_keyword_checked_as_before():
    item = _raw("Hotfix 1.2.3", topics=("borderlands4", "palworld"), needs_match=False)
    assert _match(item) == {}


def test_single_topic_dedicated_item_with_an_unrelated_title_is_still_confident():
    """A game-restricted shared feed (`games: [borderlands4]`) needn't name the game."""
    assert _match(_raw("Hotfix 1.2.3", needs_match=False)) == {"borderlands4": [False]}


def test_the_industry_entry_can_never_be_matched_by_a_brave_item():
    industry = Topic(key="industry", name="Gaming industry", channel_id=1, aliases=["layoffs"])
    brave = _raw("Gaming industry layoffs", topics=("industry",))
    assert _match(brave, [BL4, industry]) == {}
    own_feed = _raw("Studio closes", topics=("industry",), needs_match=False)
    assert _match(own_feed, [BL4, industry]) == {"industry": [False]}


# --- a game whose name is an ordinary word ---

RUST = GameCfg(
    key="rust", name="Rust", aliases=["Rust game"], entities=["Facepunch"], match_name=False
)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Rust patch notes", {}),
        ("Rusty gates need oil", {}),
        ("Rust in Rust: the language gets a release", {}),
        ("Rust game gets a wipe", {"rust": [False]}),
        ("Facepunch announces a studio move", {"rust": [True]}),
    ],
)
def test_a_match_name_false_game_needs_an_alias_or_entity_in_brave_text(title, expected):
    assert _match(_raw(title, topics=("rust",)), [RUST]) == expected


def test_a_match_name_false_game_is_still_confident_without_text_when_dedicated():
    assert _match(_raw("Wipe day", topics=("rust",), needs_match=False), [RUST]) == {
        "rust": [False]
    }


# --- the cap ---


def test_an_off_topic_brave_item_cannot_take_a_cap_slot_from_an_on_topic_one():
    brave_off = _raw("Modern Warfare 4 patch", url="https://x.test/off", trust="official")
    community = _raw(
        "Borderlands 4 notes",
        url="https://x.test/on",
        trust="community",
        published_at=RECENT - timedelta(hours=20),
        needs_match=False,
    )
    found = filter_items([brave_off, community], TOPICS, max_per_topic=1)
    assert [t.item.url for t in found["borderlands4"]] == ["https://x.test/on"]


def test_a_flood_of_off_topic_brave_items_leaves_the_cap_to_the_real_ones():
    flood = [_raw(f"Modern Warfare {n}", url=f"https://x.test/f{n}") for n in range(20)]
    real = [_raw("Borderlands 4 news", url=f"https://x.test/r{n}") for n in range(3)]
    found = filter_items([*flood, *real], TOPICS, max_per_topic=5)
    assert sorted(t.item.url for t in found["borderlands4"]) == [
        "https://x.test/r0",
        "https://x.test/r1",
        "https://x.test/r2",
    ]


def test_an_uncertain_brave_item_ranks_below_a_confident_older_one():
    entity = _raw("Gearbox hires", url="https://x.test/entity", trust="official")
    confident = _raw(
        "Borderlands 4 hotfix",
        url="https://x.test/name",
        trust="community",
        published_at=RECENT - timedelta(hours=20),
    )
    found = filter_items([entity, confident], TOPICS, max_per_topic=1)
    assert [t.item.url for t in found["borderlands4"]] == ["https://x.test/name"]


# --- the flag travels ---


def test_the_flag_is_part_of_equality_and_hash():
    plain = _raw("Same", needs_match=False)
    flagged = _raw("Same", needs_match=True)
    assert plain != flagged
    assert len({plain, flagged}) == 2
    assert flagged == _raw("Same", needs_match=True, full_text="ignored by equality")
    assert hash(flagged) == hash(_raw("Same", needs_match=True, full_text="other"))


def test_needs_match_defaults_to_false_so_every_other_collector_is_untouched():
    item = RawItem(
        url="https://x.test/a",
        title="t",
        excerpt="e",
        source_name="s",
        trust="press",
        published_at=None,
    )
    assert item.needs_match is False


def test_the_flag_survives_canonicalize_and_normalize():
    item = _raw("Borderlands 4", url="https://WWW.X.test/a/?utm_source=z")
    (canon,) = canonicalize_items([item])
    assert canon.needs_match is True
    (kept,) = normalize([item], lambda _urls: set(), NOW, timedelta(hours=24))
    assert kept.needs_match is True
    assert replace(item, url="https://x.test/b").needs_match is True


def test_a_losing_copy_keeps_its_flag_in_the_losers_list():
    brave = _raw("Gearbox hires", url="https://x.test/a", trust="community")
    official = _raw("Same story", url="https://x.test/a", trust="official", needs_match=False)
    losers: list[RawItem] = []
    (winner,) = canonicalize_items([brave, official], losers=losers)
    assert winner.needs_match is False
    assert [loser.needs_match for loser in losers] == [True]


# --- the collector ---


async def test_every_result_for_every_game_comes_back_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"results": [{"url": f"https://n.test/{request.url.params['q']}", "title": "Hi"}]}
        return httpx.Response(200, json=body)

    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")

    async def no_sleep(_s: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        items = await WebSearchCollector(source, TOPICS, "k", sleep=no_sleep).collect(http)
    assert len(items) == 2
    assert all(item.needs_match for item in items)
    assert sorted(item.topics for item in items) == [("borderlands4",), ("palworld",)]


# --- through the store ---

CONFIG = """\
home_guild_id: 100000000000000001
web_search:
  queries_per_game: 1
catalog:
  - key: borderlands4
    name: Borderlands 4
    aliases: [BL4, Borderlands4]
    entities: [Gearbox]
    search_queries: ["bl4 query"]
  - key: palworld
    name: Palworld
    entities: [Pocketpair]
    search_queries: ["pal query"]
  - key: rust
    name: Rust
    aliases: [Rust game]
    entities: [Facepunch]
    match_name: false
    search_queries: ["rust query"]
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    return load_config(path)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _tags(db_path) -> dict[str, dict[str, bool]]:
    """url -> {game: uncertain} for every stored item (an untagged item shows as {})."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT i.url, t.topic_key, t.uncertain FROM items i "
            "LEFT JOIN item_topics t ON t.item_id = i.id"
        ).fetchall()
    out: dict[str, dict[str, bool]] = {}
    for row in rows:
        out.setdefault(row["url"], {})
        if row["topic_key"]:
            out[row["url"]][row["topic_key"]] = bool(row["uncertain"])
    return out


def _brave_transport(by_query: dict[str, list[tuple[str, str, str]]]):
    """A fake Brave: each query gets its own (url, title, description) results."""

    def handler(request: httpx.Request) -> httpx.Response:
        found = by_query.get(request.url.params["q"], [])
        results = [
            {"url": u, "title": t, "description": d, "page_age": RECENT.isoformat()}
            for u, t, d in found
        ]
        return httpx.Response(200, json={"results": results})

    return httpx.MockTransport(handler)


async def _search(cfg, db_path, by_query, games):
    async def no_sleep(_s: float) -> None:
        return None

    async def alert(_message: str) -> None:
        return None

    async with httpx.AsyncClient(transport=_brave_transport(by_query)) as http:
        deps = CollectionDeps(
            cfg=cfg, db_path=db_path, http=http, collectors=[], now=lambda: NOW, alert=alert
        )
        return await collect_web_search(deps, games, "k", sleep=no_sleep)


async def test_the_same_url_from_two_games_queries_earns_each_tag_separately(cfg, db_path):
    url = "https://news.test/shared"
    both = [(url, "Borderlands 4 meets Palworld", "")]
    await _search(
        cfg, db_path, {"bl4 query": both, "pal query": both}, ["borderlands4", "palworld"]
    )
    assert _tags(db_path) == {url: {"borderlands4": False, "palworld": False}}


async def test_the_same_url_naming_only_one_game_is_tagged_only_to_that_game(cfg, db_path):
    url = "https://news.test/shared"
    only_pal = [(url, "Palworld gets a patch", "")]
    await _search(
        cfg, db_path, {"bl4 query": only_pal, "pal query": only_pal}, ["borderlands4", "palworld"]
    )
    assert _tags(db_path) == {url: {"palworld": False}}


async def test_the_same_url_from_the_other_games_query_first_still_only_earns_its_own_tag(
    cfg, db_path
):
    url = "https://news.test/shared"
    only_bl4 = [(url, "Borderlands 4 gets a patch", "")]
    await _search(
        cfg, db_path, {"bl4 query": only_bl4, "pal query": only_bl4}, ["palworld", "borderlands4"]
    )
    assert _tags(db_path) == {url: {"borderlands4": False}}


async def test_the_same_url_naming_neither_game_is_not_stored_at_all(cfg, db_path):
    url = "https://news.test/shared"
    neither = [(url, "Modern Warfare 4 patch notes", "A shooter gets fixes.")]
    outcome = await _search(
        cfg, db_path, {"bl4 query": neither, "pal query": neither}, ["borderlands4", "palworld"]
    )
    assert _tags(db_path) == {}
    assert outcome.new_items == 0


async def test_one_games_entity_and_the_others_name_gives_one_uncertain_and_one_confident(
    cfg, db_path
):
    url = "https://news.test/shared"
    mixed = [(url, "Palworld maker Pocketpair meets Gearbox", "")]
    await _search(
        cfg, db_path, {"bl4 query": mixed, "pal query": mixed}, ["borderlands4", "palworld"]
    )
    assert _tags(db_path) == {url: {"borderlands4": True, "palworld": False}}


async def test_description_markup_does_not_hide_a_name(cfg, db_path):
    """Brave wraps the words that matched the query in <strong>; the match still sees them."""
    url = "https://news.test/marked"
    marked = [(url, "Patch notes", "New <strong>Borderlands</strong> <strong>4</strong> hotfix")]
    await _search(cfg, db_path, {"bl4 query": marked}, ["borderlands4"])
    assert _tags(db_path) == {url: {"borderlands4": False}}


async def test_a_null_description_and_an_off_topic_title_is_dropped_without_a_crash(cfg, db_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "results": [{"url": "https://n.test/a", "title": "Modern Warfare", "description": None}]
        }
        return httpx.Response(200, json=body)

    async def no_sleep(_s: float) -> None:
        return None

    async def alert(_message: str) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        deps = CollectionDeps(
            cfg=cfg, db_path=db_path, http=http, collectors=[], now=lambda: NOW, alert=alert
        )
        outcome = await collect_web_search(deps, ["borderlands4"], "k", sleep=no_sleep)
    assert outcome.new_items == 0
    assert _tags(db_path) == {}


async def test_rust_needs_its_alias_in_a_brave_result(cfg, db_path):
    results = [
        ("https://n.test/plain", "Rust patch notes", "The language ships a point release."),
        ("https://n.test/alias", "Rust game wipe day", ""),
        ("https://n.test/entity", "Facepunch hires a designer", ""),
    ]
    await _search(cfg, db_path, {"rust query": results}, ["rust"])
    assert _tags(db_path) == {
        "https://n.test/alias": {"rust": False},
        "https://n.test/entity": {"rust": True},
    }


async def test_an_off_topic_brave_result_never_reaches_a_free_servers_headlines(cfg, db_path):
    results = [
        (BL4_URL, "Borderlands 4 hotfix", ""),
        ("https://news.test/off", "Modern Warfare 4 patch notes", "A shooter gets fixes."),
    ]
    await _search(cfg, db_path, {"bl4 query": results}, ["borderlands4"])
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 42, tier="free", set_up=True, now=lambda: NOW)
        repo.set_guild_games(conn, 42, [("borderlands4", 7)])
        window = repo.items_for_window(conn, 42, NOW - timedelta(days=1), NOW + timedelta(hours=1))
        listed, total = repo.query_items(conn, 42, [], NOW - timedelta(days=1), 50, 0)
    assert [h.url for h in window["borderlands4"]] == [BL4_URL]
    assert [v.url for v in listed] == [BL4_URL]
    assert total == 1


# --- dedupe: a Brave copy beside another source's copy of the same URL ---

SAME = "https://news.test/same"


def _dedicated(game, title="Hotfix 1.2.3", *, trust="official", excerpt="", url=SAME):
    return _raw(title, excerpt, topics=(game,), needs_match=False, trust=trust, url=url)


def _store(cfg, db_path, items):
    return _store_sync(db_path, items, cfg, NOW)


def test_a_brave_copy_cannot_lend_a_tag_to_the_dedicated_winner(cfg, db_path):
    official = _dedicated("palworld")
    brave = _raw("Modern Warfare 4 patch notes", topics=("borderlands4",), url=SAME)
    _store(cfg, db_path, [official, brave])
    assert _tags(db_path) == {SAME: {"palworld": False}}


def test_a_brave_copy_naming_another_game_lends_that_game_nothing(cfg, db_path):
    official = _dedicated("borderlands4")
    brave = _raw("Palworld patch notes", topics=("borderlands4",), url=SAME)
    _store(cfg, db_path, [official, brave])
    assert _tags(db_path) == {SAME: {"borderlands4": False}}


def test_a_brave_winner_does_not_strip_a_losing_dedicated_copys_earned_tag(cfg, db_path):
    community = _dedicated("palworld", trust="community")
    brave = _raw("Modern Warfare 4 patch notes", topics=("palworld",), trust="press", url=SAME)
    _store(cfg, db_path, [community, brave])
    assert _tags(db_path) == {SAME: {"palworld": False}}
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT title FROM items").fetchone()[0] == brave.title


def test_a_brave_winner_for_another_game_still_lets_the_dedicated_loser_keep_its_tag(cfg, db_path):
    community = _dedicated("palworld", trust="community")
    brave = _raw("Modern Warfare 4 patch notes", topics=("borderlands4",), trust="press", url=SAME)
    _store(cfg, db_path, [brave, community])
    assert _tags(db_path) == {SAME: {"palworld": False}}


def test_a_confident_dedicated_loser_upgrades_a_brave_winners_uncertain_tag(cfg, db_path):
    community = _dedicated("borderlands4", trust="community")
    brave = _raw("Gearbox hires a designer", topics=("borderlands4",), trust="press", url=SAME)
    _store(cfg, db_path, [community, brave])
    assert _tags(db_path) == {SAME: {"borderlands4": False}}


def test_an_uncertain_brave_loser_does_not_downgrade_a_confident_dedicated_winner(cfg, db_path):
    official = _dedicated("borderlands4")
    brave = _raw("Gearbox hires a designer", topics=("borderlands4",), trust="press", url=SAME)
    _store(cfg, db_path, [brave, official])
    assert _tags(db_path) == {SAME: {"borderlands4": False}}


def test_a_brave_loser_that_names_its_game_adds_the_tag_the_winner_lacks(cfg, db_path):
    official = _dedicated("palworld")
    brave = _raw("Borderlands 4 and Palworld", topics=("borderlands4",), url=SAME)
    _store(cfg, db_path, [official, brave])
    assert _tags(db_path) == {SAME: {"palworld": False, "borderlands4": False}}


def test_the_order_the_copies_arrive_in_does_not_change_the_tags(cfg, db_path, tmp_path):
    copies = [
        _dedicated("palworld", trust="community"),
        _raw("Gearbox hires", topics=("borderlands4",), trust="press", url=SAME),
        _raw("Modern Warfare 4", topics=("palworld",), trust="press", url=SAME),
    ]
    _store(cfg, db_path, copies)
    other = str(tmp_path / "other.db")
    with closing(connect(other)) as conn:
        migrate(conn)
    _store(cfg, other, list(reversed(copies)))
    assert _tags(db_path) == _tags(other) == {SAME: {"palworld": False, "borderlands4": True}}


def test_two_brave_copies_for_the_same_game_with_different_text_earn_one_tag(cfg, db_path):
    first = _raw("Modern Warfare 4", topics=("borderlands4",), url=SAME)
    second = _raw("Borderlands 4 returns", topics=("borderlands4",), url=SAME)
    _store(cfg, db_path, [first, second])
    assert _tags(db_path) == {SAME: {"borderlands4": False}}


def test_an_off_topic_brave_copy_stored_nowhere_does_not_block_a_later_dedicated_copy(cfg, db_path):
    brave = _raw("Modern Warfare 4 patch notes", topics=("borderlands4",), url=SAME)
    _store(cfg, db_path, [brave])
    assert _tags(db_path) == {}
    _store(cfg, db_path, [_dedicated("borderlands4")])
    assert _tags(db_path) == {SAME: {"borderlands4": False}}


# --- --check-sources ---

SECRETS = Secrets(
    discord_token=None,
    anthropic_api_key="k",
    brave_api_key="k",
    bluesky_handle=None,
    bluesky_app_password=None,
)


async def test_check_sources_counts_only_brave_items_that_name_their_game(cfg):
    now = datetime.now(UTC)
    by_query = {
        "bl4 query": [
            ("https://n.test/1", "Borderlands 4 patch", ""),
            ("https://n.test/2", "Modern Warfare 4 patch", ""),
            ("https://n.test/3", "Gearbox hires", ""),
        ],
        "pal query": [
            ("https://n.test/4", "Palworld patch", ""),
            ("https://n.test/5", "Nope", ""),
        ],
        "rust query": [("https://n.test/6", "Rust patch notes", "")],
    }

    # The check runs on the real clock and `_brave_transport` stamps results 2026-10-01, which
    # the 24-hour lookback would drop whole. So this one builds its transport with today's date.
    def handler(request: httpx.Request) -> httpx.Response:
        results = [
            {"url": u, "title": t, "description": d, "page_age": now.isoformat()}
            for u, t, d in by_query.get(request.url.params["q"], [])
        ]
        return httpx.Response(200, json={"results": results})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await run_check_sources(cfg, SECRETS, http)
    assert report.topic_counts == {"borderlands4": 2, "palworld": 1, "rust": 0}
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert "borderlands4 (Borderlands 4): 2 item(s)" in text
    assert "3 item(s) matched across 3 game(s)" in text


async def test_check_sources_for_one_game_ignores_other_games_off_topic_results(cfg):
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "results": [
                {
                    "url": "https://n.test/x",
                    "title": "Modern Warfare 4 patch",
                    "description": "",
                    "page_age": now.isoformat(),
                }
            ]
        }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await run_check_sources(cfg, SECRETS, http, games=["palworld"])
    assert report.topic_counts == {"palworld": 0}


# --- SHiFT sightings from Brave items ---


def _brave_code(title="", excerpt="", **kw):
    return _raw(title, excerpt, full_text=f"{title} {excerpt}", **kw)


def _scoped(items):
    return sightings_from_items(items, topics=TOPICS, alert_topics=["borderlands4"])


def test_a_brave_code_post_naming_the_alert_game_is_a_sighting():
    sightings = _scoped([_brave_code(f"New Borderlands 4 SHiFT code {CODE}")])
    assert [s.code for s in sightings] == [CODE]


def test_a_brave_code_post_naming_an_alias_in_the_excerpt_is_a_sighting():
    sightings = _scoped([_brave_code("New code", f"BL4 players: {CODE}")])
    assert [s.code for s in sightings] == [CODE]


def test_a_brave_code_post_that_names_no_game_is_not_a_sighting():
    assert _scoped([_brave_code(f"Free skin code {CODE}")]) == []


def test_a_brave_code_post_naming_only_another_game_is_not_a_sighting():
    assert _scoped([_brave_code(f"Palworld gift code {CODE}")]) == []


def test_a_brave_code_post_naming_only_the_alert_games_entity_is_not_a_sighting():
    """Entity-only is uncertain, and an uncertain match never scopes an alert."""
    assert _scoped([_brave_code(f"Gearbox shows off a code {CODE}")]) == []


def test_a_dedicated_code_post_with_no_game_name_is_still_a_sighting():
    item = _brave_code(f"v1.2 notes {CODE}", needs_match=False)
    assert [s.code for s in _scoped([item])] == [CODE]


def test_a_palworld_brave_post_naming_bl4_is_not_a_bl4_sighting():
    """The Palworld query found it, so Palworld is the only game it could be about."""
    item = _brave_code(f"Borderlands 4 code {CODE}", topics=("palworld",))
    assert _scoped([item]) == []


def test_one_off_topic_brave_item_does_not_hide_an_on_topic_one_beside_it():
    off = _brave_code(f"Free skin code {CODE}", url="https://x.test/off")
    on = _brave_code(f"Borderlands 4 code {CODE}", url="https://x.test/on")
    assert [s.item_url for s in _scoped([off, on])] == ["https://x.test/on"]
