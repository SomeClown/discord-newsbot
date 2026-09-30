"""The `/newsbot setup` wizard (plan task 10): driven through its real callbacks.

Real temp-file SQLite, real `SetupView`, hand-written interactions. What's under test,
over and over: the draft lives in memory, nothing is written until Save, Save is one
transaction, and only the admin who opened the wizard can touch it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import discord
import pytest
from v3_fakes import (
    GUILD_A,
    GUILD_B,
    MEMBER,
    FakeInteraction,
    channel,
    command,
    make_guild,
    thin_channel,
)

from newsbot.bot import setup_views
from newsbot.bot.commands import make_guild_admin_group, resolve_timezone
from newsbot.bot.permissions import ChannelProblem, GuildCheck
from newsbot.bot.setup_views import CURATED_ZONES, HOUR_TIMES, OTHER_ZONE, SetupView
from newsbot.store import repo
from newsbot.store.db import connect

CT = discord.ChannelType
CH1, CH2, CH3 = 111111111111111111, 222222222222222222, 333333333333333333


@pytest.fixture
def perm_lines(monkeypatch):
    """The problems `check_guild` will report, and whether it was asked."""
    state = SimpleNamespace(lines=[], asked=0, boom=False)

    async def fake_check(client, db_path, guild_id, *, game_names=None):
        state.asked += 1
        if state.boom:
            raise RuntimeError("discord is having a day")
        found = tuple(
            ChannelProblem(1, "x", "missing_permissions", (), line) for line in state.lines
        )
        return GuildCheck(guild_id, found)

    monkeypatch.setattr(setup_views, "check_guild", fake_check)
    return state


def state(db_path, guild_id=GUILD_A):
    with closing(connect(db_path)) as conn:
        return repo.get_guild(conn, guild_id), repo.list_guild_games(conn, guild_id)


def make_view(cfg, db_path, *, guild_id=GUILD_A, owner_id=1, tier="free", **kwargs):
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, guild_id)
        followed = repo.list_guild_games(conn, guild_id)
    origin = FakeInteraction(guild_id=guild_id, user_id=owner_id)
    view = SetupView(
        cfg=cfg,
        bot=SimpleNamespace(),
        db_path=db_path,
        owner_id=owner_id,
        guild_id=guild_id,
        tier=tier,
        guild=guild,
        followed=followed,
        origin=origin,
        **kwargs,
    )
    view.origin_fake = origin
    return view


def click(guild_id=GUILD_A, user_id=1, permissions=None):
    kwargs = {} if permissions is None else {"permissions": permissions}
    return FakeInteraction(guild_id=guild_id, user_id=user_id, **kwargs)


def catalog_of(cfg, n):
    base = cfg.catalog[0]
    return [base.model_copy(update={"key": f"game{i}", "name": f"Game {i}"}) for i in range(n)]


def option_values(select):
    return [o.value for o in select.options]


def defaults(select):
    return [o.value for o in select.options if o.default]


async def pick_everything(view, *, games=("palworld",), ch=CH1):
    await view.on_games(click(), list(games))
    await view.on_channel(click(), [thin_channel(ch, GUILD_A)])


# --- the menus ---


def test_zone_menu_is_within_limits_and_every_zone_is_real(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    options = view.zone_select.options
    assert len(options) == 25
    assert len(CURATED_ZONES) == 24 and len(set(CURATED_ZONES)) == 24
    for zone in CURATED_ZONES:
        assert resolve_timezone(zone) == zone
    assert OTHER_ZONE in option_values(view.zone_select)
    for o in options:
        assert len(o.label) <= 100 and len(o.value) <= 100 and len(o.description or "") <= 100
    assert len(view.zone_select.placeholder) <= 150


def test_time_menu_is_the_24_hours_with_a_fresh_server_on_nine(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    assert option_values(view.time_select) == list(HOUR_TIMES)
    assert defaults(view.time_select) == ["09:00"]
    assert defaults(view.zone_select) == ["UTC"]


def test_layout_fits_discords_five_rows(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    rows = {item.row for item in view.children}
    assert rows == {0, 1, 2, 3, 4}
    assert len(view.children) == 6
    assert view.channel_select.channel_types == [CT.text, CT.news]


@pytest.mark.parametrize(("size", "expected_max"), [(1, 1), (3, 3), (10, 10), (11, 10), (25, 10)])
def test_games_menu_sizes(v3_cfg, v3_db, size, expected_max):
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, size)})
    view = make_view(cfg, v3_db)
    assert len(view.games_select.options) == size
    assert view.games_select.min_values == 1
    assert view.games_select.max_values == expected_max
    assert len(view.games_select.placeholder) <= 150


def test_a_long_game_name_is_cut_to_the_label_limit(v3_cfg, v3_db):
    base = v3_cfg.catalog[0]
    cfg = v3_cfg.model_copy(update={"catalog": [base.model_copy(update={"name": "N" * 300})]})
    view = make_view(cfg, v3_db)
    assert len(view.games_select.options[0].label) <= 100


# --- pre-fill on a re-run ---


def test_rerun_prefills_the_current_settings(v3_cfg, v3_db):
    make_guild(
        v3_db,
        digest_time="17:00",
        timezone="Europe/Paris",
        games=[("palworld", CH2), ("rust", CH2), ("borderlands4", CH1)],
    )
    view = make_view(v3_cfg, v3_db)
    assert defaults(view.zone_select) == ["Europe/Paris"]
    assert defaults(view.time_select) == ["17:00"]
    assert sorted(defaults(view.games_select)) == ["borderlands4", "palworld", "rust"]
    assert [d.id for d in view.channel_select.default_values] == [CH2]
    assert view.channel_touched is False
    assert f"<#{CH2}>" in view.render()


def test_an_uncurated_zone_preselects_other_and_an_odd_time_shows_in_the_placeholder(v3_cfg, v3_db):
    make_guild(v3_db, digest_time="09:30", timezone="Europe/Madrid")
    view = make_view(v3_cfg, v3_db)
    assert defaults(view.zone_select) == [OTHER_ZONE]
    assert defaults(view.time_select) == []
    assert "09:30" in view.time_select.placeholder
    assert view.zone == "Europe/Madrid" and view.digest_time == "09:30"


def test_followed_games_no_longer_in_the_catalog_are_not_prefilled(v3_cfg, v3_db):
    make_guild(v3_db, games=[("palworld", CH1), ("retired", CH1)])
    view = make_view(v3_cfg, v3_db)
    assert defaults(view.games_select) == ["palworld"]


# --- selecting ---


async def test_selects_update_the_draft_and_redraw(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    i = click()
    await view.on_zone(i, ["America/Chicago"])
    await view.on_time(i, ["07:00"])
    await view.on_games(i, ["palworld", "rust"])
    await view.on_channel(i, [thin_channel(CH3, GUILD_A)])
    assert (view.zone, view.digest_time, view.game_keys, view.channel_id) == (
        "America/Chicago",
        "07:00",
        ["palworld", "rust"],
        CH3,
    )
    assert len(i.response.edits) == 4
    assert defaults(view.zone_select) == ["America/Chicago"]
    assert defaults(view.time_select) == ["07:00"]
    assert [d.id for d in view.channel_select.default_values] == [CH3]
    assert i.response.edits[-1]["allowed_mentions"].everyone is False


async def test_the_other_zone_keeps_the_previous_zone_and_says_so(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, timezone="Europe/Madrid", games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    i = click()
    await view.on_zone(i, ["Asia/Tokyo"])
    await view.on_zone(i, [OTHER_ZONE])
    assert view.zone == "Europe/Madrid"
    assert "/newsbot settings timezone:" in view.render()
    save = click()
    await view.on_save(save)
    guild, _ = state(v3_db)
    assert guild.timezone == "Europe/Madrid"
    assert "/newsbot settings timezone:" in save.original_edits[-1]["content"]


@pytest.mark.parametrize(
    "bad", [["Mars/Olympus_Mons"], ["../../etc/passwd"], [""], [], ["UTC", "UTC"]]
)
async def test_a_zone_we_never_offered_is_refused(v3_cfg, v3_db, bad):
    view = make_view(v3_cfg, v3_db)
    await view.on_zone(click(), bad)
    assert view.zone == "UTC"
    assert "don't know that time zone" in view.render()


@pytest.mark.parametrize(
    "bad", [["25:00"], ["09:30"], ["9:00"], ["tea time"], [], ["09:00", "10:00"]]
)
async def test_a_time_we_never_offered_is_refused(v3_cfg, v3_db, bad):
    view = make_view(v3_cfg, v3_db)
    await view.on_time(click(), bad)
    assert view.digest_time == "09:00"
    assert "isn't a time I offered" in view.render()


async def test_eleven_games_are_refused_and_the_draft_is_unchanged(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25)})
    view = make_view(cfg, v3_db)
    await view.on_games(click(), ["game0", "game1"])
    await view.on_games(click(), [f"game{i}" for i in range(11)])
    assert view.game_keys == ["game0", "game1"]
    assert "at most 10" in view.render()
    await view.on_games(click(), [f"game{i}" for i in range(10)])
    assert len(view.game_keys) == 10


async def test_a_game_outside_the_catalog_or_no_game_is_refused(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["palworld"])
    await view.on_games(click(), ["palworld", "fortnite"])
    assert view.game_keys == ["palworld"]
    await view.on_games(click(), [])
    assert view.game_keys == ["palworld"]
    assert "at least one game" in view.render()


async def test_duplicate_game_values_count_once(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["rust", "rust", "palworld"])
    assert view.game_keys == ["palworld", "rust"]


@pytest.mark.parametrize(
    "bad",
    [
        channel(CH1, GUILD_B),
        thin_channel(CH1, GUILD_B),
        channel(CH1, GUILD_A, CT.voice),
        thin_channel(CH1, GUILD_A, CT.public_thread),
        channel(CH1, None),
        SimpleNamespace(id=CH1, type=CT.text),
    ],
)
async def test_a_channel_from_elsewhere_or_of_the_wrong_type_is_refused(v3_cfg, v3_db, bad):
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [thin_channel(CH2, GUILD_A)])
    await view.on_channel(click(), [bad])
    assert view.channel_id == CH2
    assert str(CH1) not in view.render()


async def test_news_channels_and_uncached_channels_are_fine(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [thin_channel(CH1, GUILD_A, CT.news)])
    assert view.channel_id == CH1
    await view.on_channel(click(), [channel(CH2, GUILD_A)])
    assert view.channel_id == CH2


async def test_two_channels_at_once_are_refused(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [channel(CH1), channel(CH2)])
    assert view.channel_id is None


async def test_the_component_callbacks_read_their_own_selects(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    view.zone_select._values = ["Asia/Tokyo"]
    view.time_select._values = ["06:00"]
    view.games_select._values = ["rust"]
    view.channel_select._values = [thin_channel(CH2, GUILD_A)]
    for select in (view.zone_select, view.time_select, view.games_select, view.channel_select):
        await select.callback(click())
    assert (view.zone, view.digest_time, view.game_keys, view.channel_id) == (
        "Asia/Tokyo",
        "06:00",
        ["rust"],
        CH2,
    )


# --- Save ---


async def test_save_with_nothing_picked_is_refused_and_writes_nothing(v3_cfg, v3_db, perm_lines):
    view = make_view(v3_cfg, v3_db)
    i = click()
    await view.on_save(i)
    assert "at least one game" in view.render()
    assert state(v3_db) == (None, [])
    assert perm_lines.asked == 0
    assert view.finished is False
    await view.on_games(click(), ["rust"])
    await view.on_save(click())
    assert "Pick a channel" in view.render()
    assert state(v3_db) == (None, [])


async def test_nothing_is_written_before_save(v3_cfg, v3_db):
    make_guild(v3_db, timezone="Europe/Paris", games=[("palworld", CH1)])
    before = state(v3_db)
    view = make_view(v3_cfg, v3_db)
    i = click()
    await view.on_zone(i, ["Asia/Tokyo"])
    await view.on_time(i, ["01:00"])
    await view.on_games(i, ["rust"])
    await view.on_channel(i, [channel(CH3)])
    assert state(v3_db) == before


async def test_save_writes_everything_and_creates_the_row(v3_cfg, v3_db, perm_lines):
    view = make_view(v3_cfg, v3_db)
    i = click()
    await view.on_zone(i, ["America/Chicago"])
    await view.on_time(i, ["07:00"])
    await pick_everything(view, games=["palworld", "rust"], ch=CH2)
    save = click()
    await view.on_save(save)
    guild, games = state(v3_db)
    assert (guild.digest_time, guild.timezone, guild.set_up, guild.tier) == (
        "07:00",
        "America/Chicago",
        True,
        "free",
    )
    assert [(g.game_key, g.channel_id) for g in games] == [("palworld", CH2), ("rust", CH2)]
    assert save.response.is_done()
    edit = save.original_edits[-1]
    assert edit["view"] is None
    assert edit["allowed_mentions"].everyone is False
    assert view.finished is True


async def test_save_keeps_a_comped_servers_tier(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, tier="comped", set_up=False)
    view = make_view(v3_cfg, v3_db, tier="free")
    await pick_everything(view)
    await view.on_save(click())
    guild, _ = state(v3_db)
    assert guild.tier == "comped" and guild.set_up is True


async def test_a_missing_row_is_created_with_the_servers_tier(v3_cfg, v3_db, perm_lines):
    view = make_view(v3_cfg, v3_db, tier="comped")
    await pick_everything(view)
    await view.on_save(click())
    assert state(v3_db)[0].tier == "comped"


async def test_rerun_with_an_untouched_channel_keeps_each_games_channel(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2), ("borderlands4", CH2)])
    view = make_view(v3_cfg, v3_db)
    assert view.channel_id == CH2
    await view.on_games(click(), ["palworld", "rust"])
    await view.on_save(click())
    _, games = state(v3_db)
    assert [(g.game_key, g.channel_id) for g in games] == [("palworld", CH1), ("rust", CH2)]


async def test_rerun_new_games_go_to_the_default_channel_when_untouched(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["palworld", "rust"])
    await view.on_save(click())
    _, games = state(v3_db)
    assert [(g.game_key, g.channel_id) for g in games] == [("palworld", CH1), ("rust", CH1)]


async def test_rerun_with_a_touched_channel_moves_every_selected_game(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2), ("borderlands4", CH2)])
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["palworld", "rust", "borderlands4"])
    await view.on_channel(click(), [thin_channel(CH3, GUILD_A)])
    await view.on_save(click())
    _, games = state(v3_db)
    assert {g.channel_id for g in games} == {CH3} and len(games) == 3


async def test_rerun_deselected_games_are_dropped_and_order_is_kept(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, games=[("rust", CH1), ("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["palworld", "borderlands4"])
    await view.on_save(click())
    _, games = state(v3_db)
    assert [g.game_key for g in games] == ["palworld", "borderlands4"]


async def test_another_servers_rows_are_untouched(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, GUILD_B, timezone="Asia/Tokyo", games=[("rust", CH3)])
    before = state(v3_db, GUILD_B)
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view)
    await view.on_save(click())
    assert state(v3_db, GUILD_B) == before


async def test_save_reports_permission_problems_and_the_hints(v3_cfg, v3_db, perm_lines):
    perm_lines.lines = [f"digest channel <#{CH1}>: missing Send Messages"]
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view, games=["borderlands4", "palworld"])
    save = click()
    await view.on_save(save)
    assert perm_lines.asked == 1
    text = save.original_edits[-1]["content"]
    assert "missing Send Messages" in text
    assert "I can't do everything" in text
    assert "/newsbot follow" in text and "its own channel" in text
    assert "/newsbot shift" in text and "Borderlands 4" in text
    assert "next:" in text
    assert len(text) <= 2000


async def test_a_clean_check_says_so_and_skips_the_shift_hint_without_bl4(
    v3_cfg, v3_db, perm_lines
):
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view, games=["palworld"])
    save = click()
    await view.on_save(save)
    text = save.original_edits[-1]["content"]
    assert "permissions check out" in text
    assert "/newsbot shift" not in text
    assert "/newsbot follow" in text


async def test_many_permission_problems_still_leave_the_follow_hint(v3_cfg, v3_db, perm_lines):
    perm_lines.lines = [f"channel <#{CH1}>: missing Send Messages {'x' * 150}"] * 30
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view, games=["borderlands4"])
    save = click()
    await view.on_save(save)
    text = save.original_edits[-1]["content"]
    assert len(text) <= 2000
    assert text.rstrip().endswith("its own channel.")
    assert "/newsbot shift" in text


async def test_a_failing_permission_check_does_not_hide_the_save(v3_cfg, v3_db, perm_lines):
    perm_lines.boom = True
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view)
    save = click()
    await view.on_save(save)
    assert state(v3_db)[0].set_up is True
    assert "couldn't check" in save.original_edits[-1]["content"]


async def test_hostile_game_names_are_escaped_and_mention_proof(v3_cfg, v3_db, perm_lines):
    base = v3_cfg.catalog[0]
    evil = base.model_copy(update={"key": "evil", "name": "@everyone **bold** <@123>"})
    cfg = v3_cfg.model_copy(update={"catalog": [evil]})
    view = make_view(cfg, v3_db)
    await view.on_games(click(), ["evil"])
    assert "@everyone" not in view.render().replace("@​everyone", "")
    await view.on_channel(click(), [thin_channel(CH1, GUILD_A)])
    save = click()
    await view.on_save(save)
    text = save.original_edits[-1]["content"]
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "**bold**" not in text
    assert save.original_edits[-1]["allowed_mentions"].everyone is False


async def test_a_database_failure_changes_nothing_and_the_admin_can_retry(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    make_guild(v3_db, timezone="Europe/Paris", games=[("palworld", CH1)])
    before = state(v3_db)
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["rust"])
    await view.on_channel(click(), [thin_channel(CH2, GUILD_A)])
    real = setup_views._save_setup_sync

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(setup_views, "_save_setup_sync", boom)
    save = click()
    await view.on_save(save)
    assert state(v3_db) == before
    edit = save.original_edits[-1]
    assert "nothing was changed" in edit["content"]
    assert edit["view"] is view
    assert view.finished is False and perm_lines.asked == 0
    # Same view, database back: Save works.
    monkeypatch.setattr(setup_views, "_save_setup_sync", real)
    await view.on_save(click())
    assert [(g.game_key, g.channel_id) for g in state(v3_db)[1]] == [("rust", CH2)]


def test_the_write_is_one_transaction_a_failure_midway_rolls_everything_back(v3_db):
    make_guild(v3_db, timezone="Europe/Paris", digest_time="06:00", games=[("palworld", CH1)])
    before = state(v3_db)
    with closing(connect(v3_db)) as conn:
        # The second game's channel breaks a CHECK after the row and first game are written.
        with pytest.raises(sqlite3.IntegrityError):
            repo.apply_guild_setup(
                conn,
                GUILD_A,
                digest_time="07:00",
                timezone="Asia/Tokyo",
                games=[("rust", CH2), ("borderlands4", 0)],
            )
    assert state(v3_db) == before


def test_a_fresh_server_is_not_half_created_when_the_write_fails(v3_db):
    with closing(connect(v3_db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            repo.apply_guild_setup(
                conn, GUILD_A, digest_time="07:00", timezone="UTC", games=[("rust", 0)]
            )
    assert state(v3_db) == (None, [])


def test_the_write_refuses_eleven_games_and_duplicates_before_writing(v3_db):
    make_guild(v3_db, games=[("palworld", CH1)])
    before = state(v3_db)
    with closing(connect(v3_db)) as conn:
        with pytest.raises(repo.GameLimitError):
            repo.apply_guild_setup(
                conn,
                GUILD_A,
                digest_time="07:00",
                timezone="UTC",
                games=[(f"g{i}", CH1) for i in range(11)],
            )
        with pytest.raises(ValueError, match="Duplicate"):
            repo.apply_guild_setup(
                conn,
                GUILD_A,
                digest_time="07:00",
                timezone="UTC",
                games=[("a", CH1), ("a", CH1)],
            )
    assert state(v3_db) == before


def test_the_write_can_replace_ten_games_with_ten_others(v3_db):
    make_guild(v3_db, games=[(f"old{i}", CH1) for i in range(10)])
    with closing(connect(v3_db)) as conn:
        repo.apply_guild_setup(
            conn,
            GUILD_A,
            digest_time="07:00",
            timezone="UTC",
            games=[(f"new{i}", CH2) for i in range(10)],
        )
    assert [g.game_key for g in state(v3_db)[1]] == [f"new{i}" for i in range(10)]


# --- cancel, timeout, stale clicks, who can click ---


async def test_cancel_writes_nothing_and_says_so(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view)
    i = click()
    await view.on_cancel(i)
    assert state(v3_db) == (None, [])
    edit = i.response.edits[-1]
    assert "Nothing was saved" in edit["content"] and edit["view"] is None
    assert view.finished is True and view.is_finished()


async def test_timeout_writes_nothing_edits_the_message_and_later_clicks_are_refused(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view)
    await view.on_timeout()
    assert state(v3_db) == (None, [])
    edit = view.origin_fake.original_edits[-1]
    assert "timed out" in edit["content"] and "nothing was saved" in edit["content"]
    assert edit["view"] is None
    stale = click()
    assert await view.interaction_check(stale) is False
    assert "finished" in stale.sent[-1]["content"]
    assert stale.sent[-1]["ephemeral"] is True


async def test_a_timeout_after_the_message_vanished_is_quiet(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)

    async def gone(**kwargs):
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "Unknown Webhook")

    view.origin_fake.edit_original_response = gone
    await view.on_timeout()
    assert view.finished is True


async def test_a_timeout_after_save_leaves_the_summary_alone(v3_cfg, v3_db, perm_lines):
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view)
    await view.on_save(click())
    await view.on_timeout()
    assert view.origin_fake.original_edits == []


async def test_save_twice_is_refused_the_second_time(v3_cfg, v3_db, perm_lines):
    view = make_view(v3_cfg, v3_db)
    await pick_everything(view)
    await view.on_save(click())
    again = click()
    assert await view.interaction_check(again) is False


async def test_only_the_invoker_can_use_the_wizard(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db, owner_id=1)
    other = click(user_id=2)
    assert await view.interaction_check(other) is False
    assert "Only the admin who ran setup" in other.sent[0]["content"]
    assert other.sent[0]["ephemeral"] is True
    assert await view.interaction_check(click(user_id=1)) is True


async def test_an_owner_who_lost_manage_server_is_refused(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    demoted = click(permissions=MEMBER)
    assert await view.interaction_check(demoted) is False


async def test_a_click_from_another_server_is_refused(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    foreign = click(guild_id=GUILD_B)
    assert await view.interaction_check(foreign) is False
    assert await view.interaction_check(click(guild_id=None)) is False


async def test_an_unexpected_error_replies_and_writes_nothing(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    i = click()
    await view.on_error(i, RuntimeError("kaboom"), view.save_button)
    assert "nothing was saved" in i.sent[-1]["content"]
    assert i.sent[-1]["ephemeral"] is True
    assert state(v3_db) == (None, [])


# --- the command ---


@pytest.fixture
def setup_cmd(v3_cfg, v3_db, perm_lines):
    bot = SimpleNamespace(db_path=v3_db)
    group = make_guild_admin_group(v3_cfg, bot, digest_deps=lambda: None)
    return command(group, "setup")


def test_setup_is_a_registered_command_within_limits(setup_cmd):
    assert setup_cmd.name == "setup"
    assert 1 <= len(setup_cmd.description) <= 100
    assert setup_cmd.parameters == []


async def test_setup_opens_an_ephemeral_wizard_and_writes_nothing(setup_cmd, v3_db):
    i = FakeInteraction(user_id=7)
    await setup_cmd.callback(i)
    sent = i.sent[-1]
    assert sent["ephemeral"] is True and sent["allowed_mentions"].everyone is False
    assert isinstance(sent["view"], SetupView) and sent["view"].owner_id == 7
    assert sent["view"].timeout == 600
    assert "Nothing is saved until you press Save" in sent["content"]
    assert state(v3_db) == (None, [])


async def test_setup_on_a_rerun_prefills(setup_cmd, v3_db):
    make_guild(v3_db, digest_time="18:00", timezone="Asia/Tokyo", games=[("rust", CH3)])
    i = FakeInteraction()
    await setup_cmd.callback(i)
    view = i.sent[-1]["view"]
    assert defaults(view.zone_select) == ["Asia/Tokyo"]
    assert defaults(view.time_select) == ["18:00"]
    assert defaults(view.games_select) == ["rust"]
    assert [d.id for d in view.channel_select.default_values] == [CH3]


async def test_setup_refuses_a_non_admin(setup_cmd, v3_db):
    i = FakeInteraction(permissions=MEMBER)
    await setup_cmd.callback(i)
    assert "permission" in i.sent[-1]["content"]
    assert "view" not in i.sent[-1]
    assert state(v3_db) == (None, [])


async def test_setup_refuses_a_dm(setup_cmd):
    i = FakeInteraction(guild_id=None)
    await setup_cmd.callback(i)
    assert "inside a server" in i.sent[-1]["content"]
    assert "view" not in i.sent[-1]


async def test_setup_keeps_a_comped_server_comped(v3_cfg, v3_db, perm_lines):
    cfg = v3_cfg.model_copy(update={"comped_guild_ids": [GUILD_A]})
    make_guild(v3_db, tier="comped", set_up=False)
    group = make_guild_admin_group(cfg, SimpleNamespace(db_path=v3_db), digest_deps=lambda: None)
    i = FakeInteraction()
    await command(group, "setup").callback(i)
    view = i.sent[-1]["view"]
    await pick_everything(view)
    await view.on_save(click())
    assert state(v3_db)[0].tier == "comped"
