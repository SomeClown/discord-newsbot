"""The setup wizard and the Gaming industry entry (plan task 3).

The entry rides along: ten games plus the industry post is a legal pick, eleven games is
not, with or without it. The "(N of M)" count and the select's size say the same thing.
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest
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
from newsbot.industry import INDUSTRY_KEY
from newsbot.store import repo
from newsbot.store.db import connect

CH1 = 111111111111111111


@pytest.fixture
def industry_cfg(monkeypatch):
    return load_v3_industry_config(monkeypatch)


def with_games(cfg, n):
    """The industry config with `n` made-up games ahead of the industry entry."""
    base = cfg.catalog[0]
    games = [base.model_copy(update={"key": f"game{i}", "name": f"Game {i}"}) for i in range(n)]
    industry = next(g for g in cfg.catalog if g.key == INDUSTRY_KEY)
    return cfg.model_copy(update={"catalog": [*games, industry]})


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


def keys(n, *, industry=True):
    return [f"game{i}" for i in range(n)] + ([INDUSTRY_KEY] if industry else [])


def test_the_games_menu_has_room_for_ten_plus_industry(industry_cfg, v3_db):
    view = make_view(with_games(industry_cfg, 12), v3_db)
    assert view.max_games == 10
    assert view.games_select.max_values == 11
    assert len(view.games_select.options) == 13
    assert view.games_select.placeholder == "Games to follow (up to 10, plus Gaming industry)"


def test_the_menu_never_asks_for_more_values_than_it_has_options(industry_cfg, v3_db):
    view = make_view(industry_cfg, v3_db)
    assert len(view.catalog) == 4
    assert view.games_select.max_values == 4
    view = make_view(with_games(industry_cfg, 10), v3_db)
    assert view.games_select.max_values == 11


def test_without_the_entry_the_menu_is_what_it_was(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    assert view.games_select.max_values == 3
    assert view.games_select.placeholder == "Games to follow (up to 3)"


def test_the_placeholder_uses_the_entrys_name_escaped_and_cut(industry_cfg, v3_db):
    cfg = with_games(industry_cfg, 12)
    industry = cfg.catalog[-1].model_copy(update={"name": "**" + "Gaming industry" * 20})
    cfg = cfg.model_copy(update={"catalog": [*cfg.catalog[:-1], industry]})
    placeholder = make_view(cfg, v3_db).games_select.placeholder
    assert placeholder.startswith("Games to follow (up to 10, plus \\*\\*Gaming industry")
    assert len(placeholder) <= 150


async def test_ten_games_plus_industry_is_accepted(industry_cfg, v3_db):
    view = make_view(with_games(industry_cfg, 12), v3_db)
    await view.on_games(click(), keys(10))
    assert view.game_keys == keys(10)
    assert view.note == ""
    assert "(10 of 10)" in view.render()


async def test_eleven_games_are_refused_with_or_without_industry(industry_cfg, v3_db):
    view = make_view(with_games(industry_cfg, 12), v3_db)
    await view.on_games(click(), keys(3))
    for picked in (keys(11), keys(11, industry=False)):
        await view.on_games(click(), picked)
        assert view.game_keys == keys(3)
        assert "at most 10 games" in view.note
        assert "you picked 11" in view.note


async def test_industry_alone_is_a_pick_and_counts_as_none_of_ten(industry_cfg, v3_db):
    view = make_view(with_games(industry_cfg, 12), v3_db)
    await view.on_games(click(), [INDUSTRY_KEY])
    assert view.game_keys == [INDUSTRY_KEY]
    assert "(0 of 10)" in view.render()


async def test_saving_ten_games_plus_industry_writes_all_eleven(industry_cfg, v3_db, monkeypatch):
    async def fake_check(client, db_path, guild_id, *, game_names=None):
        return GuildCheck(guild_id, ())

    monkeypatch.setattr(setup_views, "check_guild", fake_check)
    make_guild(v3_db, GUILD_A)
    view = make_view(with_games(industry_cfg, 12), v3_db)
    await view.on_games(click(), keys(10))
    await view.on_channel(click(), [thin_channel(CH1, GUILD_A)])
    await view.on_save(click())
    with closing(connect(v3_db)) as conn:
        assert [g.game_key for g in repo.list_guild_games(conn, GUILD_A)] == keys(10)
