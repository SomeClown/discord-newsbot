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

from newsbot.config import load_config

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
    assert len(cfg.catalog) == 16


def test_arc_raiders_is_the_last_catalog_entry_with_steam_as_its_only_source():
    # Added 2026-10-04 at the end so the first three keep their order. Steam is its only
    # source (no official feed or Bluesky exists, and Reddit is codes only), and the studio
    # isn't an entity because Embark's other game is THE FINALS.
    game = load_config(EXAMPLE).catalog[-1]
    assert (game.key, game.name) == ("arcraiders", "ARC Raiders")
    assert game.aliases == ["ArcRaiders"] and game.entities == []
    assert game.match_name is True
    assert [(s.type, s.name, s.app_id, s.trust) for s in game.sources] == [
        ("steam_news", "ARC Raiders Steam", 1808500, "official")
    ]
    assert len(game.search_queries) == 2


def test_the_catalog_fits_under_every_discord_cap():
    # 25 options in the setup wizard's select menu; 25 autocomplete choices, one of them "All".
    games = len(load_config(EXAMPLE).catalog)
    assert games + 1 <= 25


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
