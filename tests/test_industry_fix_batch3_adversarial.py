"""Adversarial round on fix batch 3 (industry sources, setup wording, four feeds).

Three things changed: every source under the industry entry must be a plain `rss` feed
with `trust: press`; the setup wizard's "up to N" and "(N of M)" count games only; and
PC Gamer's tag left the example config's industry entry. These tests lean on all three
from the outside: through `load_config`, through `SetupView`, and through the real
example file. A config forged around `load_config` is out of scope.
"""

from __future__ import annotations

import re
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from v3_fakes import (
    GUILD_A,
    FakeInteraction,
    load_v3_industry_config,
    make_guild,
    thin_channel,
)

from newsbot.bot import setup_views
from newsbot.bot.permissions import GuildCheck
from newsbot.bot.setup_views import SetupView
from newsbot.config import ConfigError, load_config
from newsbot.industry import INDUSTRY_KEY
from newsbot.store import repo
from newsbot.store.db import connect

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"
CH1 = 111111111111111111

PC_GAMER_TAG_URL = "https://www.pcgamer.com/feeds/tag/gaming-industry/"
PC_GAMER_TAG_LINE = (
    f'      - {{type: rss, name: "PC Gamer Industry", url: "{PC_GAMER_TAG_URL}", trust: press}}'
)

FOUR_FEEDS = [
    ("GamesIndustry.biz", "https://www.gamesindustry.biz/feed/news"),
    ("Game Developer", "https://www.gamedeveloper.com/rss.xml"),
    ("VGC", "https://www.videogameschronicle.com/category/news/feed/"),
    ("Gematsu Industry", "https://www.gematsu.com/c/industry/feed"),
]


@pytest.fixture(autouse=True)
def _brave(monkeypatch):
    # The example config carries a web_search block, which wants a key at load time.
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")


# --- config: what an industry source may be ---

_HEAD = """
catalog:
  - key: palworld
    name: "Palworld"
    sources:
      - {type: steam_news, name: "Palworld Steam", app_id: 1623730, trust: official}
  - key: industry
    name: "Gaming industry"
    match_name: false
    sources:
"""

GOOD_FEED = (
    '      - {type: rss, name: "Good Feed", url: "https://good.example.com/feed", trust: press}'
)


def _config_text(*source_lines: str) -> str:
    return _HEAD + "\n".join(source_lines) + "\n"


def _refusals(tmp_path: Path, *source_lines: str) -> list[str]:
    """The error lines load_config gives for these industry sources (empty if it loads)."""
    path = tmp_path / "config.yaml"
    path.write_text(_config_text(*source_lines))
    try:
        load_config(path)
    except ConfigError as err:
        return [ln.strip() for ln in str(err).splitlines() if ln.strip().startswith("- ")]
    return []


def _src(kind: str, trust: str | None, extra: str = "", name: str = "Bad Feed") -> str:
    trust_part = f", trust: {trust}" if trust is not None else ""
    bodies = {
        "rss": f'type: rss, name: "{name}", url: "https://bad.example.com/feed"',
        "steam_news": f'type: steam_news, name: "{name}", app_id: 1623730',
        "bluesky_search": f'type: bluesky_search, name: "{name}", query: "layoffs"',
        "web_search": f'type: web_search, name: "{name}"',
        "shift_table": f'type: shift_table, name: "{name}", url: "https://bad.example.com/codes"',
        "reddit": f'type: rss, name: "{name}", url: "https://www.reddit.com/r/gaming/.rss"',
    }
    return f"      - {{{bodies[kind]}{trust_part}{extra}}}"


def test_the_real_example_config_still_loads_with_industry_last():
    cfg = load_config(EXAMPLE)
    assert cfg.catalog[-1].key == INDUSTRY_KEY
    assert all(s.type == "rss" and s.trust == "press" for s in cfg.catalog[-1].sources)


def test_a_good_press_rss_feed_alone_loads(tmp_path):
    assert _refusals(tmp_path, GOOD_FEED) == []


def test_press_rss_with_codes_only_false_spelled_out_still_loads(tmp_path):
    # `codes_only: false` on a plain rss feed is legal elsewhere, so the new rule must not
    # trip over it.
    assert _refusals(tmp_path, _src("rss", "press", ", codes_only: false")) == []


@pytest.mark.parametrize(
    ("kind", "trust", "extra", "fragment"),
    [
        ("rss", "official", "", "trust: official"),
        ("rss", "community", "", "trust: community"),
        ("steam_news", "press", "", "steam_news source"),
        ("bluesky_search", "press", "", "bluesky_search source"),
        ("reddit", "press", "", "a codes-only source"),
        ("rss", "press", ", codes_only: true", "a codes-only source"),
        ("shift_table", "community", "", "shift_table only feeds SHiFT detection"),
        ("web_search", "press", "", "web_search goes in the top-level web_search: block"),
        ("web_search", "community", "", "web_search goes in the top-level web_search: block"),
    ],
)
def test_one_wrong_thing_about_a_source_gives_exactly_one_refusal(
    tmp_path, kind, trust, extra, fragment
):
    lines = _refusals(tmp_path, _src(kind, trust, extra))
    assert len(lines) == 1, lines
    assert fragment in lines[0]
    assert "catalog[1] (industry)" in lines[0]


@pytest.mark.parametrize("trust", [None, "bogus", "Press", "PRESS", '""'])
def test_a_missing_or_misspelled_trust_is_one_clear_refusal(tmp_path, trust):
    lines = _refusals(tmp_path, _src("rss", trust))
    assert len(lines) == 1, lines
    assert "trust" in lines[0]


@pytest.mark.parametrize("kind", ["reddit", "steam_news", "bluesky_search"])
def test_a_source_that_breaks_two_rules_is_at_least_refused_and_named(tmp_path, kind):
    # One line per broken rule is intended (owner fixes both at once); pin that the source
    # is named in every line and the config does not load.
    trust = "community" if kind != "steam_news" else "official"
    lines = _refusals(tmp_path, _src(kind, trust))
    assert lines
    assert all("sources[0] (Bad Feed)" in ln for ln in lines)


def test_each_bad_source_in_a_list_gets_its_own_line_and_the_good_one_none(tmp_path):
    lines = _refusals(
        tmp_path,
        _src("rss", "official", name="First Bad"),
        GOOD_FEED,
        _src("steam_news", "press", name="Second Bad"),
    )
    assert len(lines) == 2
    assert "sources[0] (First Bad)" in lines[0]
    assert "sources[2] (Second Bad)" in lines[1]
    assert not any("Good Feed" in ln for ln in lines)


def test_the_same_feed_under_a_normal_game_keeps_its_trust_and_type(tmp_path):
    # The rule is about the industry key only: an official steam_news and a community rss
    # under a game must still load.
    path = tmp_path / "config.yaml"
    path.write_text(
        """
catalog:
  - key: palworld
    name: "Palworld"
    sources:
      - {type: steam_news, name: "Palworld Steam", app_id: 1623730, trust: official}
      - {type: rss, name: "Fan Feed", url: "https://fan.example.com/feed", trust: community}
      - {type: bluesky_search, name: "Pal Search", query: "palworld", trust: community}
  - key: industry
    name: "Gaming industry"
    match_name: false
    sources:
"""
        + GOOD_FEED
        + "\n"
    )
    cfg = load_config(path)
    assert [s.type for s in cfg.catalog[0].sources] == ["steam_news", "rss", "bluesky_search"]


def test_a_prod_config_that_still_carries_the_pc_gamer_line_keeps_loading(tmp_path):
    # The owner may have appended the five-feed block to prod before the tag was dropped.
    # The line is a legal press rss feed, so it must not become a startup error.
    text = EXAMPLE.read_text()
    anchor = re.search(r'^( +- \{type: rss, name: "Gematsu Industry".*)$', text, re.M)
    assert anchor is not None
    patched = text.replace(anchor.group(1), anchor.group(1) + "\n" + PC_GAMER_TAG_LINE)
    path = tmp_path / "config.yaml"
    path.write_text(patched)
    cfg = load_config(path)
    assert [s.name for s in cfg.catalog[-1].sources][-1] == "PC Gamer Industry"


# --- the example config's industry entry ---


def _catalog_step_block() -> str:
    text = EXAMPLE.read_text()
    start = text.index("# >>> catalog-step: start")
    end = text.index("# <<< catalog-step: end")
    return text[start:end]


def test_the_industry_entry_has_exactly_four_feeds_in_order():
    sources = load_config(EXAMPLE).catalog[-1].sources
    assert [(s.name, str(s.url)) for s in sources] == FOUR_FEEDS


def test_no_pc_gamer_industry_tag_url_is_left_in_the_catalog_step_block():
    block = _catalog_step_block()
    assert "feeds/tag/gaming-industry" not in block
    assert "PC Gamer Industry" not in block


def test_no_pc_gamer_industry_tag_url_is_left_anywhere_in_the_example_config():
    text = EXAMPLE.read_text()
    assert PC_GAMER_TAG_URL not in text
    assert "PC Gamer Industry" not in text


def test_the_pc_gamer_main_feed_is_still_a_shared_source_outside_industry():
    # The whole reason the tag left is that PC Gamer's main feed stays keyword-matched.
    cfg = load_config(EXAMPLE)
    pcgamer = [s for s in cfg.shared_sources if "pcgamer.com/rss" in str(s.url)]
    assert len(pcgamer) == 1
    assert INDUSTRY_KEY not in (pcgamer[0].games or [])


def test_the_comment_and_the_entry_agree_on_four():
    block = _catalog_step_block()
    industry_start = block.index("# >>> industry-step: start")
    industry = block[industry_start : block.index("# <<< industry-step: end")]
    assert "Only the four feeds below" in industry
    assert "All four came back 200" in industry
    assert not re.search(r"\b(five|fifth)\b", industry, re.I)


def test_the_industry_step_block_parses_to_four_press_rss_sources():
    block = _catalog_step_block()
    start = block.index("# >>> industry-step: start")
    end = block.index("# <<< industry-step: end")
    parsed = yaml.safe_load("catalog:\n" + block[start:end])
    sources = parsed["catalog"][0]["sources"]
    assert len(sources) == 4
    assert {(s["type"], s["trust"]) for s in sources} == {("rss", "press")}


# --- setup: the wording of the limit on any size of catalog ---


@pytest.fixture
def industry_cfg(monkeypatch):
    return load_v3_industry_config(monkeypatch)


def catalog_of(cfg, n, *, industry=True):
    base = cfg.catalog[0]
    entries = [base.model_copy(update={"key": f"game{i}", "name": f"Game {i}"}) for i in range(n)]
    if industry:
        entries.append(next(g for g in cfg.catalog if g.key == INDUSTRY_KEY))
    return cfg.model_copy(update={"catalog": entries})


def make_view(cfg, db_path):
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, GUILD_A)
        followed = repo.list_guild_games(conn, GUILD_A)
    return SetupView(
        cfg=cfg,
        bot=SimpleNamespace(get_guild=lambda guild_id: object()),
        db_path=db_path,
        owner_id=1,
        guild_id=GUILD_A,
        tier="free",
        guild=guild,
        followed=followed,
        origin=FakeInteraction(guild_id=GUILD_A, user_id=1),
    )


def click():
    return FakeInteraction(guild_id=GUILD_A, user_id=1)


def picks(n, *, industry):
    return [f"game{i}" for i in range(n)] + ([INDUSTRY_KEY] if industry else [])


SIZES = [0, 1, 9, 10, 11, 21]


@pytest.mark.parametrize("n", SIZES)
def test_placeholder_and_max_values_with_the_entry(industry_cfg, v3_db, n):
    view = make_view(catalog_of(industry_cfg, n), v3_db)
    games = min(10, n)
    assert view.max_games == games
    assert view.games_select.placeholder == f"Games to follow (up to {games}, plus Gaming industry)"
    assert view.games_select.max_values == min(games + 1, n + 1)
    assert view.games_select.max_values <= len(view.games_select.options)


@pytest.mark.parametrize("n", [s for s in SIZES if s > 0])
def test_placeholder_and_max_values_without_the_entry(industry_cfg, v3_db, n):
    view = make_view(catalog_of(industry_cfg, n, industry=False), v3_db)
    games = min(10, n)
    assert view.max_games == games
    assert view.games_select.placeholder == f"Games to follow (up to {games})"
    assert view.games_select.max_values == games


@pytest.mark.parametrize("n", [s for s in SIZES if s > 0])
async def test_the_n_of_m_figure_uses_games_only_at_the_top_pick(industry_cfg, v3_db, n):
    games = min(10, n)
    for industry in (True, False):
        view = make_view(catalog_of(industry_cfg, n, industry=industry), v3_db)
        await view.on_games(click(), picks(games, industry=industry))
        assert view.note == ""
        assert f"({games} of {games})" in view.render()


@pytest.mark.parametrize("n", [11, 21])
async def test_one_game_over_the_limit_is_refused_and_the_wording_matches(industry_cfg, v3_db, n):
    games = 10
    view = make_view(catalog_of(industry_cfg, n), v3_db)
    await view.on_games(click(), picks(games, industry=True))
    await view.on_games(click(), picks(games + 1, industry=True))
    assert f"at most {games} games" in view.note
    assert f"you picked {games + 1}" in view.note
    assert f"({games} of {games})" in view.render()


@pytest.mark.parametrize("n", [1, 9, 10, 11, 21])
@pytest.mark.parametrize("industry", [True, False])
async def test_picking_the_most_the_menu_allows_still_saves(
    industry_cfg, v3_db, monkeypatch, n, industry
):
    async def fake_check(client, db_path, guild_id, *, game_names=None):
        return GuildCheck(guild_id, ())

    monkeypatch.setattr(setup_views, "check_guild", fake_check)
    make_guild(v3_db, GUILD_A)
    games = min(10, n)
    view = make_view(catalog_of(industry_cfg, n, industry=industry), v3_db)
    # The select's own max_values is the most a user can tick in one go.
    assert view.games_select.max_values == games + (1 if industry else 0)
    chosen = picks(games, industry=industry)
    assert len(chosen) == view.games_select.max_values
    await view.on_games(click(), chosen)
    await view.on_channel(click(), [thin_channel(CH1, GUILD_A)])
    await view.on_save(click())
    assert view.finished
    with closing(connect(v3_db)) as conn:
        saved = [g.game_key for g in repo.list_guild_games(conn, GUILD_A)]
    assert saved == chosen


async def test_an_industry_only_catalog_says_up_to_zero_and_still_saves(
    industry_cfg, v3_db, monkeypatch
):
    async def fake_check(client, db_path, guild_id, *, game_names=None):
        return GuildCheck(guild_id, ())

    monkeypatch.setattr(setup_views, "check_guild", fake_check)
    make_guild(v3_db, GUILD_A)
    view = make_view(catalog_of(industry_cfg, 0), v3_db)
    assert view.games_select.placeholder == "Games to follow (up to 0, plus Gaming industry)"
    assert (view.games_select.min_values, view.games_select.max_values) == (1, 1)
    await view.on_games(click(), [INDUSTRY_KEY])
    assert view.note == ""
    assert "(0 of 0)" in view.render()
    await view.on_channel(click(), [thin_channel(CH1, GUILD_A)])
    await view.on_save(click())
    assert view.finished
    with closing(connect(v3_db)) as conn:
        assert [g.game_key for g in repo.list_guild_games(conn, GUILD_A)] == [INDUSTRY_KEY]


async def test_a_one_game_catalog_with_the_entry_refuses_an_unknown_key(industry_cfg, v3_db):
    view = make_view(catalog_of(industry_cfg, 1), v3_db)
    assert view.games_select.max_values == 2
    await view.on_games(click(), ["game0", "game_not_there"])
    assert "I don't know one of those games" in view.note
    assert view.game_keys == []


def test_the_real_example_catalog_still_reads_up_to_ten_plus_the_entry(v3_db):
    cfg = load_config(EXAMPLE)
    view = make_view(cfg, v3_db)
    assert len(cfg.catalog) == 22
    assert view.max_games == 10
    assert view.games_select.placeholder == "Games to follow (up to 10, plus Gaming industry)"
    assert view.games_select.max_values == 11
