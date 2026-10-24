"""The shipped example configs, held to what their own comments promise.

`config.example.yaml` and `config.minimal.yaml` are the first things a stranger
copies, and a doc that doesn't load is a doc that lies. `test_config.py` pins
that both load; this pins the parts the comments make claims about: the catalog
order the comped prompt depends on, the commented single-server block really being
the shape the lounge's one-time read expects, and the commented lounge block really being
a lounge that loads. The commented blocks are uncommented in memory and run
through `load_config`, so a comment can't drift from the loader without a test
noticing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.config import effective_codes_only, load_config

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "config.example.yaml"
MINIMAL = ROOT / "config.minimal.yaml"


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


def _uncomment(block: str) -> str:
    return "\n".join(
        line[2:] if line.startswith("# ") else ("" if line == "#" else line)
        for line in block.splitlines()
    )


def _commented_block(text: str, start: str, end: str) -> str:
    return _uncomment(text[text.index(start) : text.index(end)])


@pytest.mark.parametrize("path", [EXAMPLE, MINIMAL], ids=["example", "minimal"])
def test_shipped_example_loads_as_a_v3_config(path):
    cfg = load_config(path)
    assert cfg.catalog
    assert cfg.legacy is None  # no server in it: servers are set up with /newsbot setup


def test_the_example_catalog_keeps_the_three_original_games_first_and_in_order():
    # The comped summarizer's prompt lists games in catalog order, so reordering the first
    # three would change the friend's prompt (and need an owner-reviewed /newsbot preview).
    cfg = load_config(EXAMPLE)
    assert [g.key for g in cfg.catalog][:3] == ["borderlands4", "palworld", "diablo4"]
    assert len(cfg.catalog) == 22


def test_arc_raiders_has_steam_first_and_one_press_tag_feed():
    # Added 2026-10-04 at the end so the first three keep their order. Steam is its only
    # official source (no official feed or Bluesky exists, and Reddit is codes only);
    # Insider Gaming's tag feed joined on 2026-10-23, checked from the Droplet. The studio
    # isn't an entity because Embark's other game is THE FINALS.
    game = next(g for g in load_config(EXAMPLE).catalog if g.key == "arcraiders")
    assert (game.key, game.name) == ("arcraiders", "ARC Raiders")
    assert game.aliases == ["ArcRaiders"] and game.entities == []
    assert game.match_name is True
    assert [(s.type, s.name, s.trust) for s in game.sources] == [
        ("steam_news", "ARC Raiders Steam", "official"),
        ("rss", "Insider Gaming ARC Raiders", "press"),
    ]
    assert game.sources[0].app_id == 1808500
    assert len(game.search_queries) == 2


def test_the_five_games_added_for_a_comped_server_come_last_with_steam_first():
    # Added 2026-10-21 for a friend's new server, after ARC Raiders so the first three keep
    # their order (the industry entry now follows them, hence the slice that skips the last).
    # Each has its official Steam news; Valheim alone also has a verified, official Bluesky
    # account. None of the studios offers a usable feed (JavaScript sites, and DayZ's answers a
    # datacenter with a Cloudflare 403), so Steam is the backbone.
    games = load_config(EXAMPLE).catalog[-6:-1]
    assert [(g.key, g.name) for g in games] == [
        ("valheim", "Valheim"),
        ("scum", "SCUM"),
        ("dayz", "DayZ"),
        ("activematter", "Active Matter"),
        ("tarkov", "Escape from Tarkov"),
    ]
    steam = {g.key: [s.app_id for s in g.sources if s.type == "steam_news"] for g in games}
    assert steam == {
        "valheim": [892970],
        "scum": [513710],
        "dayz": [221100],
        "activematter": [2887580],
        "tarkov": [3932890],
    }
    assert all(s.trust == "official" for g in games for s in g.sources)
    valheim = games[0]
    assert [s.name for s in valheim.sources] == ["Valheim Steam", "Valheim Bluesky"]
    # Studios whose other games would flood a free server's headlines stay out.
    assert {g.key: g.entities for g in games} == {
        "valheim": ["Iron Gate"],
        "scum": ["Gamepires"],
        "dayz": [],
        "activematter": [],
        "tarkov": ["Battlestate Games", "Nikita Buyanov"],
    }
    assert games[4].aliases == ["Tarkov", "EFT", "EscapeFromTarkov"]
    # "active matter" on Bluesky is physics, so that game must never get a Bluesky search.
    assert all(s.type != "bluesky_search" for s in games[3].sources)


def test_the_industry_entry_is_last_with_no_keywords_and_five_press_feeds():
    # Added 2026-10-23 (docs/design.md section 18). It is matched by nothing: no aliases,
    # entities or search queries, and its name is never matched. The five feeds were each
    # fetched from the Droplet that day. PC Gamer's tag joined as the fifth on the owner's
    # word (2A) once the dedupe reading was corrected; see docs/sources-research.md.
    cfg = load_config(EXAMPLE)
    game = cfg.catalog[-1]
    assert (game.key, game.name) == ("industry", "Gaming industry")
    assert game.match_name is False
    assert game.aliases == [] and game.entities == [] and game.search_queries == []
    assert [(s.type, s.name, str(s.url), s.trust) for s in game.sources] == [
        ("rss", "GamesIndustry.biz", "https://www.gamesindustry.biz/feed/news", "press"),
        ("rss", "Game Developer", "https://www.gamedeveloper.com/rss.xml", "press"),
        (
            "rss",
            "VGC",
            "https://www.videogameschronicle.com/category/news/feed/",
            "press",
        ),
        ("rss", "Gematsu Industry", "https://www.gematsu.com/c/industry/feed", "press"),
        (
            "rss",
            "PC Gamer Industry",
            "https://www.pcgamer.com/feeds/tag/gaming-industry/",
            "press",
        ),
    ]
    assert not any("industry" in (s.games or []) for s in cfg.shared_sources)


def test_the_catalog_fits_under_every_discord_cap():
    # 25 options in the setup wizard's select menu; 25 autocomplete choices, one of them "All".
    games = len(load_config(EXAMPLE).catalog)
    assert games + 1 <= 25


def test_borderlands4_reads_mentalmars_for_codes_and_details():
    # E4 (2026-10-09): the example ships the mentalmars page under Borderlands 4, so a
    # self-hoster following that game gets the same code list and details production does.
    cfg = load_config(EXAMPLE)
    bl4 = next(g for g in cfg.catalog if g.key == "borderlands4")
    (table,) = [s for s in bl4.sources if s.type == "shift_table"]
    assert table.name == "mentalmars"
    assert str(table.url) == "https://mentalmars.com/game-news/borderlands-4-shift-codes/"
    assert table.trust == "community"
    assert table.codes_only is None and effective_codes_only(table)  # codes-only by being one
    assert "mentalmars" in cfg.codes_only_source_names()  # so nothing it reads is stored
    assert "borderlands4" in cfg.shift.games
    # Only that one game has a table, and it isn't also sitting in the shared list.
    assert [s.name for g in cfg.catalog for s in g.sources if s.type == "shift_table"] == [
        "mentalmars"
    ]
    assert all(s.type != "shift_table" for s in cfg.shared_sources)


def test_the_example_shift_games_exist_in_the_catalog():
    cfg = load_config(EXAMPLE)
    assert set(cfg.shift.games) <= {g.key for g in cfg.catalog}


def test_the_commented_single_server_keys_are_the_shape_the_import_reads(tmp_path):
    text = EXAMPLE.read_text()
    v2 = _commented_block(text, "# guild_id:", "#\n# The lounge works on that one server")
    path = tmp_path / "config.yaml"
    path.write_text(text + "\n" + v2)
    cfg = load_config(path)
    assert cfg.legacy is not None
    assert [key for key, _channel in cfg.legacy.games] == ["borderlands4", "palworld"]
    assert cfg.legacy.shift_enabled is True
    assert cfg.legacy.lounge is None


def test_the_commented_lounge_block_loads_as_the_lounge_the_import_reads(tmp_path):
    text = EXAMPLE.read_text()
    v2 = _commented_block(text, "# guild_id:", "#\n# The lounge works on that one server")
    lounge = _commented_block(text, "# lounge:\n", "#\n# To try it without")
    path = tmp_path / "config.yaml"
    path.write_text(text + "\n" + v2 + "\n" + lounge)
    cfg = load_config(path)
    assert cfg.legacy is not None
    assert cfg.legacy.lounge is not None
    assert cfg.legacy.lounge.welcome.enabled
    assert cfg.legacy.lounge.daily_quote.enabled
