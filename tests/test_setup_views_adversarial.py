"""Adversarial tests for the `/newsbot setup` wizard (plan task 10).

The wizard is a draft in memory and a single transaction at the end, so the questions
worth asking are about the seams: what happens when two admins run it at once, when the
draft has gone stale by the time Save lands, when the click isn't from the person who
opened it, and when the payload isn't something our own menus could have produced.
Discord's client only sends what we offered; a hand-rolled request has no such manners.

Clicks here go through `View._scheduled_task`, which is what discord.py itself runs: it
loads the forged values into the select, calls `interaction_check`, then the callback.
That way a test that says "a stranger pressed Cancel" really exercised the guard the
gateway would have.

Two findings were first pinned as strict xfails and have since been fixed: a Save that
lands after the bot left the server used to re-create the server's row, and a timeout
that fires during a failing Save used to leave a retry message on a view that had
already stopped listening. A third, a stale draft overwriting concurrent `/newsbot
follow` and `settings` edits, became a behavior change: Save now refuses instead. Other
behaviors are pinned as they are, with a comment where the owner might want them
different. Nothing here touches the network; the slow ones sleep for a tenth of a
second.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest
from v3_fakes import (
    GUILD_A,
    GUILD_B,
    MEMBER,
    FakeInteraction,
    command,
    make_guild,
    thin_channel,
)

from newsbot.bot import setup_views
from newsbot.bot.commands import make_guild_admin_group
from newsbot.bot.format import discord_len
from newsbot.bot.permissions import ChannelProblem, GuildCheck
from newsbot.bot.setup_views import (
    CURATED_ZONES,
    HOUR_TIMES,
    MSG_BOT_GONE,
    MSG_BROKEN,
    MSG_FINISHED,
    MSG_NOT_YOURS,
    MSG_REFUSED,
    MSG_STALE,
    MSG_TIMED_OUT_SAVE_LOST,
    MSG_TOO_MANY_GAMES,
    OTHER_ZONE,
    SetupView,
    most_used_channel,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import GuildGame

CT = discord.ChannelType
CH1, CH2, CH3, CH4 = (n * 111111111111111111 for n in (1, 2, 3, 4))
GUILD_C = 300000000000000003


# --- helpers ---


@pytest.fixture
def perm_lines(monkeypatch):
    """The problems `check_guild` will report, and whether it was asked."""
    st = SimpleNamespace(lines=[], asked=0)

    async def fake_check(client, db_path, guild_id, *, game_names=None):
        st.asked += 1
        found = tuple(ChannelProblem(1, "x", "missing_permissions", (), line) for line in st.lines)
        return GuildCheck(guild_id, found)

    monkeypatch.setattr(setup_views, "check_guild", fake_check)
    return st


@pytest.fixture
def writes(monkeypatch):
    """Every call that reached the database write, in order."""
    real = setup_views._save_setup_sync
    calls: list[tuple] = []

    def counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(setup_views, "_save_setup_sync", counting)
    return calls


def slow_write(monkeypatch, *, fail=False, delay=0.1):
    """Make the database write take `delay` seconds, then succeed or raise a lock error."""
    real = setup_views._save_setup_sync

    def slow(*args, **kwargs):
        time.sleep(delay)
        if fail:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(setup_views, "_save_setup_sync", slow)


def state(db_path, guild_id=GUILD_A):
    with closing(connect(db_path)) as conn:
        return repo.get_guild(conn, guild_id), repo.list_guild_games(conn, guild_id)


def every_table(db_path):
    """Every row of every table (minus FTS shadow tables): "nothing at all was written"."""
    out = {}
    with closing(connect(db_path)) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            if table.startswith("sqlite_") or "_fts" in table:
                continue
            out[table] = [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}"')]  # noqa: S608
    return out


def new_db(tmp_path, name):
    path = str(tmp_path / f"{name}.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def make_view(cfg, db_path, *, guild_id=GUILD_A, owner_id=1, tier="free", bot=None, **kwargs):
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, guild_id)
        followed = repo.list_guild_games(conn, guild_id)
    origin = FakeInteraction(guild_id=guild_id, user_id=owner_id)
    view = SetupView(
        cfg=cfg,
        bot=bot if bot is not None else SimpleNamespace(get_guild=lambda guild_id: object()),
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


async def dispatch(view, item, interaction, values=()):
    """Deliver a component payload the way discord.py does: refresh, check, callback."""
    interaction.data = {"custom_id": item.custom_id, "values": list(values)}
    await view._scheduled_task(item, interaction)
    return interaction


def draft(view):
    return (
        view.zone,
        view.zone_is_other,
        view.digest_time,
        list(view.game_keys),
        view.channel_id,
        view.channel_touched,
        view.finished,
    )


def components(view):
    return [item for item in (view.zone_select, view.time_select, view.games_select)] + [
        view.channel_select,
        view.save_button,
        view.cancel_button,
    ]


def catalog_of(cfg, n, name=lambda i: f"Game {i}"):
    base = cfg.catalog[0]
    return [base.model_copy(update={"key": f"game{i}", "name": name(i)}) for i in range(n)]


def defaults(select):
    return [o.value for o in select.options if o.default]


def summary_of(interaction):
    return interaction.original_edits[-1]["content"]


async def pick(view, *, games=("palworld",), ch=CH1):
    await view.on_games(click(), list(games))
    await view.on_channel(click(), [thin_channel(ch, GUILD_A)])


# --- two admins, one server ---


@pytest.mark.parametrize("round_", range(8))
async def test_two_admins_saving_at_once_leave_exactly_one_of_the_two_drafts(
    v3_cfg, tmp_path, perm_lines, round_
):
    db = new_db(tmp_path, f"race{round_}")
    one = make_view(v3_cfg, db, owner_id=1)
    two = make_view(v3_cfg, db, owner_id=2)
    await one.on_zone(click(), ["Asia/Tokyo"])
    await one.on_time(click(), ["07:00"])
    await pick(one, games=["palworld", "rust"], ch=CH1)
    await two.on_zone(click(user_id=2), ["Europe/Paris"])
    await two.on_time(click(user_id=2), ["08:00"])
    await pick(two, games=["borderlands4"], ch=CH2)

    await asyncio.gather(
        dispatch(one, one.save_button, click(user_id=1)),
        dispatch(two, two.save_button, click(user_id=2)),
    )

    guild, games = state(db)
    got = (
        guild.timezone,
        guild.digest_time,
        [(g.game_key, g.channel_id) for g in games],
    )
    first = ("Asia/Tokyo", "07:00", [("palworld", CH1), ("rust", CH1)])
    second = ("Europe/Paris", "08:00", [("borderlands4", CH2)])
    assert got in (first, second)


async def test_the_later_save_is_refused_and_the_earlier_one_stands(v3_cfg, v3_db, perm_lines):
    # Changed from "the later save replaces the earlier one whole": the second wizard's
    # snapshot is stale once the first has saved, so it now refuses instead of clobbering.
    one = make_view(v3_cfg, v3_db, owner_id=1)
    two = make_view(v3_cfg, v3_db, owner_id=2)
    await one.on_zone(click(), ["Asia/Tokyo"])
    await pick(one, games=["palworld", "rust"], ch=CH1)
    await two.on_zone(click(user_id=2), ["Europe/Paris"])
    await pick(two, games=["borderlands4"], ch=CH2)
    await dispatch(one, one.save_button, click(user_id=1))
    late = await dispatch(two, two.save_button, click(user_id=2))
    guild, games = state(v3_db)
    assert guild.timezone == "Asia/Tokyo"
    assert [(g.game_key, g.channel_id) for g in games] == [("palworld", CH1), ("rust", CH1)]
    assert summary_of(late) == MSG_STALE


async def test_the_first_admins_view_does_not_notice_the_second_admin_saving(
    v3_cfg, v3_db, perm_lines
):
    # Nothing tells the first wizard its draft went stale; it stays open until its Save,
    # which is where the stale check refuses (see the test above).
    one = make_view(v3_cfg, v3_db, owner_id=1)
    two = make_view(v3_cfg, v3_db, owner_id=2)
    await pick(two, games=["rust"], ch=CH2)
    await dispatch(two, two.save_button, click(user_id=2))
    assert one.finished is False
    assert await one.interaction_check(click(user_id=1)) is True


# Everything below: the wizard opened, somebody else changed the server with the ordinary
# commands, and then Save landed. Changed from "Save writes the draft it was seeded with,
# undoing the other change": Save now compares against what it saw when it opened and, if
# anything moved, writes nothing and ends with MSG_STALE.


async def test_a_stale_save_refuses_to_move_a_game_back_over_a_concurrent_follow(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    with closing(connect(v3_db)) as conn:
        repo.follow_game(conn, GUILD_A, "palworld", CH3)  # another admin: /newsbot follow
    save = await dispatch(view, view.save_button, click())
    assert [(g.game_key, g.channel_id) for g in state(v3_db)[1]] == [("palworld", CH3)]
    assert summary_of(save) == MSG_STALE and view.finished is True


async def test_a_stale_save_refuses_to_drop_a_game_followed_while_the_wizard_was_open(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    with closing(connect(v3_db)) as conn:
        repo.follow_game(conn, GUILD_A, "rust", CH2)
    save = await dispatch(view, view.save_button, click())
    assert [g.game_key for g in state(v3_db)[1]] == ["palworld", "rust"]
    assert summary_of(save) == MSG_STALE


async def test_a_stale_save_refuses_to_refollow_a_game_unfollowed_while_the_wizard_was_open(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2)])
    view = make_view(v3_cfg, v3_db)
    with closing(connect(v3_db)) as conn:
        repo.unfollow_game(conn, GUILD_A, "rust")
    save = await dispatch(view, view.save_button, click())
    assert [(g.game_key, g.channel_id) for g in state(v3_db)[1]] == [("palworld", CH1)]
    assert summary_of(save) == MSG_STALE


async def test_a_stale_save_refuses_to_overwrite_a_concurrent_settings_change(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, digest_time="06:00", timezone="Europe/Paris", games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, digest_time="18:00", timezone="Asia/Tokyo")
    save = await dispatch(view, view.save_button, click())
    guild, _ = state(v3_db)
    assert (guild.digest_time, guild.timezone) == ("18:00", "Asia/Tokyo")
    assert summary_of(save) == MSG_STALE


async def test_a_stale_save_leaves_the_admin_channel_and_shift_settings_alone(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, admin_channel_id=CH4, games=[("palworld", CH1)])
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_A, enabled=True, channel_id=CH3, ping="none")
        repo.add_notice(conn, GUILD_A, "a notice")
        repo.update_guild_settings(conn, GUILD_A, permission_problems="old problem")
        joined = repo.get_guild(conn, GUILD_A).joined_at
    view = make_view(v3_cfg, v3_db)
    await pick(view, games=["rust"], ch=CH2)
    await dispatch(view, view.save_button, click())
    with closing(connect(v3_db)) as conn:
        guild = repo.get_guild(conn, GUILD_A)
        assert guild.admin_channel_id == CH4
        assert guild.permission_problems == "old problem"
        assert guild.joined_at == joined
        assert repo.get_shift(conn, GUILD_A).channel_id == CH3
        assert len(repo.recent_notices(conn, GUILD_A, 5)) == 1


async def test_a_save_against_a_concurrently_filled_server_is_refused_not_a_limit_error(
    v3_cfg, v3_db, perm_lines
):
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25)})
    make_guild(v3_db, games=[("game0", CH1)])
    view = make_view(cfg, v3_db)
    with closing(connect(v3_db)) as conn:
        for i in range(10, 19):  # the other admin fills the server to ten
            repo.follow_game(conn, GUILD_A, f"game{i}", CH2)
    await view.on_games(click(), [f"game{i}" for i in range(1, 11)])
    save = await dispatch(view, view.save_button, click())
    # Changed: this used to succeed (the write deletes before it inserts, so the ten-game
    # trigger never sees eleven). Now the concurrent follows make the draft stale first.
    assert summary_of(save) == MSG_STALE
    assert len(state(v3_db)[1]) == 10


# --- Save: doubled, raced, failed ---


async def test_two_rapid_saves_write_once(v3_cfg, v3_db, perm_lines, writes, monkeypatch):
    slow_write(monkeypatch)  # wraps the counting wrapper from `writes`
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    first, second = click(), click()
    await asyncio.gather(
        dispatch(view, view.save_button, first), dispatch(view, view.save_button, second)
    )
    assert len(writes) == 1
    assert second.sent[-1]["content"] == MSG_FINISHED
    assert "Saved" in summary_of(first)


async def test_every_component_is_refused_while_a_save_is_in_flight(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    slow_write(monkeypatch)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    saving = asyncio.create_task(dispatch(view, view.save_button, click()))
    await asyncio.sleep(0.03)
    before = draft(view)
    for item, values in [
        (view.zone_select, ["Asia/Tokyo"]),
        (view.time_select, ["01:00"]),
        (view.games_select, ["rust"]),
        (view.channel_select, ["999"]),
        (view.cancel_button, []),
    ]:
        late = await dispatch(view, item, click(), values)
        assert late.sent[-1]["content"] == MSG_FINISHED
    assert draft(view) == before
    await saving
    assert [(g.game_key, g.channel_id) for g in state(v3_db)[1]] == [("palworld", CH1)]


async def test_a_timeout_during_a_good_save_does_not_overwrite_the_summary(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    slow_write(monkeypatch)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    save = click()
    saving = asyncio.create_task(dispatch(view, view.save_button, save))
    await asyncio.sleep(0.03)
    await view.on_timeout()
    await saving
    assert view.origin_fake.original_edits == []
    assert "Saved" in summary_of(save)
    assert state(v3_db)[0].set_up is True


async def test_a_timeout_just_before_save_means_save_is_refused_and_nothing_is_written(
    v3_cfg, v3_db, perm_lines, writes
):
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    await view.on_timeout()
    late = await dispatch(view, view.save_button, click())
    assert late.sent[-1]["content"] == MSG_FINISHED
    assert writes == []
    assert state(v3_db) == (None, [])


async def test_a_failed_save_after_a_mid_save_timeout_ends_with_the_timed_out_message(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    slow_write(monkeypatch, fail=True)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    save = click()
    saving = asyncio.create_task(view.on_save(save))
    await asyncio.sleep(0.03)
    view._dispatch_timeout()  # the timer fires mid-save, as it would after ten idle minutes
    await saving
    await asyncio.sleep(0)
    # The view stopped listening when the timer fired, so no retry is offered.
    assert save.original_edits[-1]["content"] == MSG_TIMED_OUT_SAVE_LOST
    assert save.original_edits[-1]["view"] is None
    assert view.finished is True and state(v3_db) == (None, [])


async def test_a_failed_save_leaves_the_view_usable_for_the_next_click(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    slow_write(monkeypatch, fail=True, delay=0)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    await dispatch(view, view.save_button, click())
    assert view._busy is False and view.finished is False
    again = click()
    assert await view.interaction_check(again) is True
    await dispatch(view, view.zone_select, click(), ["Asia/Tokyo"])
    assert view.zone == "Asia/Tokyo"


async def test_a_failure_that_is_not_a_database_error_replies_and_keeps_the_draft(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    real = setup_views._save_setup_sync

    def boom(*args, **kwargs):
        raise RuntimeError("thread pool on fire")

    monkeypatch.setattr(setup_views, "_save_setup_sync", boom)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    before = draft(view)
    save = await dispatch(view, view.save_button, click())
    assert save.sent[-1]["content"] == MSG_BROKEN
    assert state(v3_db) == (None, [])
    assert view._busy is False and draft(view) == before
    monkeypatch.setattr(setup_views, "_save_setup_sync", real)
    await dispatch(view, view.save_button, click())
    assert state(v3_db)[0].set_up is True


async def test_a_game_limit_error_from_the_write_ends_the_wizard_with_no_retry(
    v3_cfg, v3_db, perm_lines, monkeypatch
):
    def limit(*args, **kwargs):
        raise repo.GameLimitError("At most 10 games per server.")

    monkeypatch.setattr(setup_views, "_save_setup_sync", limit)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    save = await dispatch(view, view.save_button, click())
    # Changed from a "Press Save to try again" prompt: the same picks would fail again.
    assert summary_of(save) == MSG_TOO_MANY_GAMES
    assert save.original_edits[-1]["view"] is None and view.finished is True


@pytest.mark.parametrize(
    "error",
    [ValueError("Duplicate game key."), sqlite3.IntegrityError("CHECK constraint failed")],
)
async def test_other_deterministic_write_errors_end_the_wizard_with_no_retry(
    v3_cfg, v3_db, perm_lines, monkeypatch, error
):
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(setup_views, "_save_setup_sync", refuse)
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    save = await dispatch(view, view.save_button, click())
    assert summary_of(save) == MSG_REFUSED
    assert save.original_edits[-1]["view"] is None and view.finished is True


# --- the bot left the server ---


async def test_saving_after_the_bot_was_removed_does_not_recreate_the_server(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("palworld", CH1)])
    bot = SimpleNamespace(get_guild=lambda guild_id: None)
    view = make_view(v3_cfg, v3_db, bot=bot)
    await pick(view, games=["rust"], ch=CH2)
    with closing(connect(v3_db)) as conn:
        repo.delete_guild(conn, GUILD_A)  # on_guild_remove
    save = await dispatch(view, view.save_button, click())
    assert state(v3_db) == (None, [])
    assert summary_of(save) == MSG_BOT_GONE and view.finished is True


def _fingerprint(db_path):
    with closing(connect(db_path)) as conn:
        return repo.setup_fingerprint(
            repo.get_guild(conn, GUILD_A), repo.list_guild_games(conn, GUILD_A)
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda c: repo.follow_game(c, GUILD_A, "palworld", CH3),
        lambda c: repo.follow_game(c, GUILD_A, "rust", CH3),
        lambda c: repo.unfollow_game(c, GUILD_A, "palworld"),
        lambda c: repo.update_guild_settings(c, GUILD_A, digest_time="18:00"),
        lambda c: repo.update_guild_settings(c, GUILD_A, timezone="Asia/Tokyo"),
        lambda c: repo.delete_guild(c, GUILD_A),
    ],
)
def test_the_repo_refuses_a_setup_whose_fingerprint_no_longer_matches(v3_db, change):
    make_guild(v3_db, games=[("palworld", CH1)])
    expected = _fingerprint(v3_db)
    with closing(connect(v3_db)) as conn:
        change(conn)
    before = every_table(v3_db)
    with closing(connect(v3_db)) as conn:
        with pytest.raises(repo.StaleSetupError):
            repo.apply_guild_setup(conn, GUILD_A, expected_fingerprint=expected, **SETUP_KWARGS)
        assert conn.in_transaction is False
    assert every_table(v3_db) == before


def test_the_repo_writes_when_the_fingerprint_matches_and_ignores_game_order(v3_db):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2)])
    with closing(connect(v3_db)) as conn:
        expected = repo.setup_fingerprint(
            repo.get_guild(conn, GUILD_A), list(reversed(repo.list_guild_games(conn, GUILD_A)))
        )
        repo.apply_guild_setup(conn, GUILD_A, expected_fingerprint=expected, **SETUP_KWARGS)
    assert state(v3_db)[0].timezone == "Asia/Tokyo"


def test_the_fingerprint_check_holds_the_write_lock_before_it_looks(v3_db):
    # The race-free claim: once a setup has started its check, nobody else can write until
    # it commits. A second connection with no busy timeout is turned away at once.
    make_guild(v3_db, games=[("palworld", CH1)])
    expected = _fingerprint(v3_db)
    seen = []

    class Watcher:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn.__enter__()

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

        def execute(self, sql, params=()):
            if sql.startswith("SELECT") and not seen:
                rival = sqlite3.connect(v3_db, timeout=0)
                try:
                    rival.execute("UPDATE guilds SET digest_time = '22:00'")
                    seen.append("rival wrote")
                except sqlite3.OperationalError:
                    seen.append("rival locked out")
                finally:
                    rival.close()
            return self.conn.execute(sql, params)

    with closing(connect(v3_db)) as conn:
        watched = Watcher(conn)
        repo.apply_guild_setup(watched, GUILD_A, expected_fingerprint=expected, **SETUP_KWARGS)  # type: ignore[arg-type]
    assert seen == ["rival locked out"]
    assert state(v3_db)[0].digest_time == "05:00"


def test_apply_setup_on_a_missing_row_creates_only_that_server(v3_db):
    # The repo contract the resurrection bug leans on: a missing row is created, and
    # nobody else is touched while it is.
    make_guild(v3_db, GUILD_B, tier="comped", admin_channel_id=CH4, games=[("rust", CH3)])
    make_guild(v3_db, GUILD_A, games=[("palworld", CH1)])
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_B, enabled=True, channel_id=CH3, ping="none")
        repo.delete_guild(conn, GUILD_A)
    before_b = state(v3_db, GUILD_B)
    with closing(connect(v3_db)) as conn:
        repo.apply_guild_setup(
            conn, GUILD_A, digest_time="07:00", timezone="UTC", games=[("rust", CH2)]
        )
    assert state(v3_db)[0].set_up is True
    assert state(v3_db, GUILD_B) == before_b
    with closing(connect(v3_db)) as conn:
        assert repo.get_shift(conn, GUILD_B).channel_id == CH3
        assert repo.get_shift(conn, GUILD_A) is None


# --- forged payloads ---


@pytest.mark.parametrize(
    "bad",
    [
        ["Europe/Madrid"],
        ["utc"],
        ["america/chicago"],
        ["UTC\n"],
        [" UTC"],
        ["UTC", OTHER_ZONE],
        [OTHER_ZONE, OTHER_ZONE],
        [123],
        [["UTC"]],
        [{"UTC": 1}],
        ["Etc/GMT+5"],
    ],
)
async def test_forged_zone_values_change_nothing(v3_cfg, v3_db, bad):
    view = make_view(v3_cfg, v3_db)
    before = draft(view)
    await dispatch(view, view.zone_select, click(), bad)
    assert draft(view) == before
    assert "don't know that time zone" in view.render()


@pytest.mark.parametrize(
    "bad",
    [["24:00"], ["00:60"], ["09:00\n"], ["09:00 "], ["٠٩:٠٠"], ["09:00:00"], ["9:00"], [9], [None]],
)
async def test_forged_time_values_change_nothing(v3_cfg, v3_db, bad):
    view = make_view(v3_cfg, v3_db)
    before = draft(view)
    await dispatch(view, view.time_select, click(), bad)
    assert draft(view) == before
    assert "isn't a time I offered" in view.render()


@pytest.mark.parametrize(
    "bad",
    [["Palworld"], ["palworld "], [""], ["palworld", "retired"], [1], ["../rust"]],
)
async def test_forged_game_values_change_nothing(v3_cfg, v3_db, bad):
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["rust"])
    before = draft(view)
    await dispatch(view, view.games_select, click(), bad)
    assert draft(view) == before


async def test_a_forged_unhashable_game_value_is_an_error_not_a_state_change(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["rust"])
    before = draft(view)
    hit = await dispatch(view, view.games_select, click(), [{"a": 1}])
    assert hit.sent[-1]["content"] == MSG_BROKEN
    assert draft(view) == before and view._busy is False


async def test_fifty_copies_of_one_game_count_as_one(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await dispatch(view, view.games_select, click(), ["rust"] * 50)
    assert view.game_keys == ["rust"]


async def test_eleven_distinct_keys_hidden_among_duplicates_are_still_refused(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25)})
    view = make_view(cfg, v3_db)
    await view.on_games(click(), ["game0"])
    forged = [f"game{i}" for i in range(11)] * 3
    await dispatch(view, view.games_select, click(), forged)
    assert view.game_keys == ["game0"]
    assert "at most 10" in view.render()


async def test_forged_channel_ids_with_no_resolved_data_are_refused(v3_cfg, v3_db):
    # Without Discord's `resolved` block the select only has bare id strings, which can't
    # be tied to this server, so they're refused and don't count as touching the menu.
    make_guild(v3_db, games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    await dispatch(view, view.channel_select, click(), [str(CH2)])
    assert view.channel_id == CH1 and view.channel_touched is False
    assert "isn't in this server" in view.render()


@pytest.mark.parametrize(
    "kind",
    [
        CT.voice,
        CT.stage_voice,
        CT.forum,
        CT.category,
        CT.private,
        CT.group,
        CT.public_thread,
        CT.private_thread,
        CT.news_thread,
        CT.media,
    ],
)
async def test_a_channel_of_any_other_type_is_refused_and_is_not_a_touch(v3_cfg, v3_db, kind):
    make_guild(v3_db, games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [thin_channel(CH2, GUILD_A, kind)])
    assert view.channel_id == CH1 and view.channel_touched is False


@pytest.mark.parametrize("bad_id", [0, -5, True, "12", None, 1.5])
async def test_a_channel_with_a_nonsense_id_is_refused(v3_cfg, v3_db, bad_id):
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [SimpleNamespace(id=bad_id, type=CT.text, guild_id=GUILD_A)])
    assert view.channel_id is None and view.channel_touched is False


async def test_a_refused_channel_after_a_good_one_keeps_the_good_one(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2)])
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [thin_channel(CH3, GUILD_A)])
    await view.on_channel(click(), [thin_channel(CH4, GUILD_B)])
    await dispatch(view, view.save_button, click())
    assert {g.channel_id for g in state(v3_db)[1]} == {CH3}


async def test_no_channel_values_at_all_is_refused(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    await view.on_channel(click(), [])
    assert view.channel_id is None
    assert "exactly one" in view.render()


def test_two_views_never_share_a_custom_id(v3_cfg, v3_db):
    one, two = make_view(v3_cfg, v3_db), make_view(v3_cfg, v3_db)
    ids_one = {item.custom_id for item in one.children}
    ids_two = {item.custom_id for item in two.children}
    assert len(ids_one) == 6 and ids_one.isdisjoint(ids_two)
    assert all(len(i) <= 100 for i in ids_one | ids_two)


async def test_a_payload_for_one_view_cannot_move_the_draft_of_another(v3_cfg, v3_db):
    one, two = make_view(v3_cfg, v3_db), make_view(v3_cfg, v3_db)
    await dispatch(one, one.zone_select, click(), ["Asia/Tokyo"])
    assert one.zone == "Asia/Tokyo" and two.zone == "UTC"


@pytest.mark.parametrize("how", ["cancelled", "saved", "timed_out"])
async def test_values_arriving_after_the_view_ended_change_nothing(
    v3_cfg, v3_db, perm_lines, writes, how
):
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    if how == "cancelled":
        await view.on_cancel(click())
    elif how == "saved":
        await dispatch(view, view.save_button, click())
    else:
        await view.on_timeout()
    writes_before, rows_before, draft_before = len(writes), every_table(v3_db), draft(view)
    for item, values in [
        (view.zone_select, ["Asia/Tokyo"]),
        (view.time_select, ["01:00"]),
        (view.games_select, ["rust"]),
        (view.channel_select, [str(CH2)]),
        (view.save_button, []),
        (view.cancel_button, []),
    ]:
        late = await dispatch(view, item, click(), values)
        assert late.sent[-1]["content"] == MSG_FINISHED
    assert len(writes) == writes_before
    assert every_table(v3_db) == rows_before
    assert draft(view) == draft_before


# --- who may click ---


def _refusals(view):
    return [
        (view.zone_select, ["Asia/Tokyo"]),
        (view.time_select, ["01:00"]),
        (view.games_select, ["rust"]),
        (view.channel_select, [str(CH2)]),
        (view.save_button, []),
        (view.cancel_button, []),
    ]


@pytest.mark.parametrize("index", range(6))
async def test_a_stranger_cannot_use_any_component(v3_cfg, v3_db, perm_lines, writes, index):
    view = make_view(v3_cfg, v3_db, owner_id=1)
    await pick(view)
    before = draft(view)
    item, values = _refusals(view)[index]
    hit = await dispatch(view, item, click(user_id=2), values)
    assert hit.sent[-1]["content"] == MSG_NOT_YOURS and hit.sent[-1]["ephemeral"] is True
    assert draft(view) == before and writes == []
    assert view.is_finished() is False
    assert state(v3_db) == (None, [])


@pytest.mark.parametrize("index", range(6))
async def test_an_invoker_without_manage_server_cannot_use_any_component(
    v3_cfg, v3_db, perm_lines, writes, index
):
    view = make_view(v3_cfg, v3_db, owner_id=1)
    await pick(view)
    before = draft(view)
    item, values = _refusals(view)[index]
    for perms in (MEMBER, discord.Permissions.none(), discord.Permissions(manage_messages=True)):
        hit = await dispatch(view, item, click(permissions=perms), values)
        assert hit.sent[-1]["content"] == MSG_NOT_YOURS
    assert draft(view) == before and writes == []
    assert view.is_finished() is False


async def test_a_demoted_invoker_who_is_promoted_again_carries_on(v3_cfg, v3_db, perm_lines):
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    await dispatch(view, view.save_button, click(permissions=MEMBER))
    assert state(v3_db) == (None, [])
    await dispatch(view, view.save_button, click())
    assert state(v3_db)[0].set_up is True


@pytest.mark.parametrize("index", range(6))
async def test_a_dm_context_is_refused_even_with_every_permission(
    v3_cfg, v3_db, perm_lines, writes, index
):
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    item, values = _refusals(view)[index]
    hit = await dispatch(
        view, item, click(guild_id=None, permissions=discord.Permissions.all()), values
    )
    assert "inside a server" in hit.sent[-1]["content"]
    assert writes == [] and view.is_finished() is False


@pytest.mark.parametrize("index", range(6))
async def test_a_click_from_another_server_is_refused_everywhere(
    v3_cfg, v3_db, perm_lines, writes, index
):
    view = make_view(v3_cfg, v3_db)
    await pick(view)
    item, values = _refusals(view)[index]
    hit = await dispatch(view, item, click(guild_id=GUILD_B), values)
    assert "inside a server" in hit.sent[-1]["content"]
    assert writes == [] and state(v3_db, GUILD_B) == (None, [])


async def test_the_same_display_name_is_not_the_same_person(v3_cfg, v3_db, writes):
    view = make_view(v3_cfg, v3_db, owner_id=1)
    impostor = click(user_id=2)
    impostor.user = SimpleNamespace(id=2, display_name="Alice", name="Alice", global_name="Alice")
    await dispatch(view, view.cancel_button, impostor)
    assert impostor.sent[-1]["content"] == MSG_NOT_YOURS
    assert view.finished is False


@pytest.fixture
def setup_cmd(v3_cfg, v3_db, perm_lines):
    group = make_guild_admin_group(
        v3_cfg,
        SimpleNamespace(db_path=v3_db, get_guild=lambda guild_id: object()),
        digest_deps=lambda: None,
    )
    return command(group, "setup")


async def test_two_admins_each_get_a_wizard_that_only_obeys_them(setup_cmd, v3_db):
    one, two = FakeInteraction(user_id=1), FakeInteraction(user_id=2)
    await setup_cmd.callback(one)
    await setup_cmd.callback(two)
    view_one, view_two = one.sent[-1]["view"], two.sent[-1]["view"]
    assert view_one is not view_two
    assert (view_one.owner_id, view_two.owner_id) == (1, 2)
    hit = await dispatch(view_one, view_one.cancel_button, click(user_id=2))
    assert hit.sent[-1]["content"] == MSG_NOT_YOURS
    assert view_one.finished is False


async def test_opening_the_wizard_and_using_the_menus_writes_not_one_row(setup_cmd, v3_db):
    make_guild(v3_db, GUILD_B, games=[("rust", CH3)])
    make_guild(v3_db, GUILD_A, set_up=False, games=[("palworld", CH1)])
    before = every_table(v3_db)
    for _ in range(3):
        i = FakeInteraction()
        await setup_cmd.callback(i)
        view = i.sent[-1]["view"]
        await dispatch(view, view.zone_select, click(), ["Asia/Tokyo"])
        await dispatch(view, view.games_select, click(), ["rust"])
        await view.on_cancel(click())
    assert every_table(v3_db) == before


async def test_opening_the_wizard_in_a_server_with_no_row_does_not_create_one(setup_cmd, v3_db):
    for _ in range(2):
        await setup_cmd.callback(FakeInteraction())
    assert every_table(v3_db)["guilds"] == []


# --- text and limits ---


def check_limits(view):
    rows = view.to_components()
    assert 1 <= len(rows) <= 5
    for row in rows:
        assert len(row["components"]) <= 5
        for comp in row["components"]:
            assert len(comp["custom_id"]) <= 100
            assert len(comp.get("placeholder", "")) <= 150
            assert len(comp.get("label", "")) <= 80
            options = comp.get("options")
            if options is not None:
                assert 1 <= len(options) <= 25
                for o in options:
                    assert 1 <= len(o["label"]) <= 100 and len(o["value"]) <= 100
                    assert len(o.get("description", "")) <= 100
                assert 1 <= comp["min_values"] <= comp["max_values"] <= 25
                assert comp["max_values"] <= len(options)
                assert sum(1 for o in options if o.get("default")) <= comp["max_values"]
    assert len(defaults(view.zone_select)) == 1
    assert len(defaults(view.time_select)) <= 1


LONG_NAMES = {
    "star": lambda i: "*" * 100,
    "astral": lambda i: "\U0001f916" * 100,
    "mention": lambda i: "@everyone " * 10,
    "plain": lambda i: f"Game number {i} " + "x" * 120,
}


@pytest.mark.parametrize("flavor", LONG_NAMES)
async def test_every_render_state_fits_in_two_thousand_characters(v3_cfg, v3_db, flavor):
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25, LONG_NAMES[flavor])})
    view = make_view(cfg, v3_db)
    check_limits(view)
    await view.on_games(click(), [f"game{i}" for i in range(10)])
    await view.on_channel(click(), [thin_channel(CH1, GUILD_A)])
    texts = [view.render()]
    for step in (
        lambda: view.on_zone(click(), ["nope"]),
        lambda: view.on_time(click(), ["nope"]),
        lambda: view.on_games(click(), [f"game{i}" for i in range(11)]),
        lambda: view.on_games(click(), ["zzz"]),
        lambda: view.on_channel(click(), [thin_channel(CH2, GUILD_B)]),
        lambda: view.on_zone(click(), [OTHER_ZONE]),
    ):
        await step()
        texts.append(view.render())
    for text in texts:
        assert discord_len(text) <= 2000
    check_limits(view)


@pytest.mark.parametrize("flavor", LONG_NAMES)
async def test_the_summary_fits_even_with_ten_huge_game_names_and_a_huge_problem_list(
    v3_cfg, v3_db, perm_lines, flavor
):
    perm_lines.lines = [f"channel <#{CH1}>: missing Send Messages {'y' * 300}"] * 40
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25, LONG_NAMES[flavor])})
    view = make_view(cfg, v3_db)
    await pick(view, games=[f"game{i}" for i in range(10)])
    save = await dispatch(view, view.save_button, click())
    text = summary_of(save)
    assert discord_len(text) <= 2000
    assert text.startswith("Saved.")
    assert save.original_edits[-1]["allowed_mentions"].everyone is False
    assert len(state(v3_db)[1]) == 10


async def test_a_moderate_name_list_keeps_the_follow_hint_under_a_huge_problem_list(
    v3_cfg, v3_db, perm_lines
):
    perm_lines.lines = [f"channel <#{CH1}>: missing Send Messages {'y' * 300}"] * 40
    cfg = v3_cfg.model_copy(
        update={"catalog": catalog_of(v3_cfg, 25, lambda i: f"Game {i} " + "n" * 40)}
    )
    view = make_view(cfg, v3_db)
    await pick(view, games=[f"game{i}" for i in range(10)])
    save = await dispatch(view, view.save_button, click())
    text = summary_of(save)
    assert discord_len(text) <= 2000
    assert text.rstrip().endswith("its own channel.")
    assert "I can't do everything" in text


async def test_with_absurd_game_names_the_summary_loses_the_problems_and_the_hint(
    v3_cfg, v3_db, perm_lines
):
    # Pinned, and probably fine: the game lines alone eat the 2000 characters, and the
    # final cut drops whatever is last. Names that long aren't in the real catalog. But
    # the permission warnings are the most useful part of the summary, and they go first.
    perm_lines.lines = ["digest channel: missing Send Messages"]
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25, LONG_NAMES["star"])})
    view = make_view(cfg, v3_db)
    await pick(view, games=[f"game{i}" for i in range(10)])
    save = await dispatch(view, view.save_button, click())
    text = summary_of(save)
    assert "missing Send Messages" not in text
    assert "/newsbot follow" not in text


HOSTILE = [
    "@everyone",
    "@here",
    "<@123456789012345678>",
    "<@&123456789012345678>",
    "<#999>",
    "</ban:1>",
    "[click](https://evil.example)",
    "https://evil.example",
    "discord.gg/abc",
    "```\nfence```",
    "__under__ ~~strike~~ ||spoiler||",
]


@pytest.mark.parametrize("name", HOSTILE)
async def test_hostile_game_names_render_inert_everywhere(v3_cfg, v3_db, perm_lines, name):
    base = v3_cfg.catalog[0]
    cfg = v3_cfg.model_copy(
        update={"catalog": [base.model_copy(update={"key": "evil", "name": name})]}
    )
    view = make_view(cfg, v3_db)
    await pick(view, games=["evil"], ch=CH1)
    save = await dispatch(view, view.save_button, click())
    for text in (view.render(), summary_of(save)):
        assert not re.search(r"(?<!\u200b)@(everyone|here)", text)
        assert not re.search(r"<@(?!\u200b)", text)
        assert "<#999>" not in text and "</ban:1>" not in text
        assert "://" not in text  # a scheme is defused with a zero-width space
        assert "discord.gg/" not in text
    assert save.original_edits[-1]["allowed_mentions"].everyone is False
    assert save.original_edits[-1]["allowed_mentions"].users is False
    assert save.original_edits[-1]["allowed_mentions"].roles is False


@pytest.mark.parametrize("zone", ["@everyone", "<#999> </ban:1>", "**bold**", "Z" * 3000])
async def test_a_hostile_stored_zone_renders_inert_and_fits(v3_cfg, v3_db, perm_lines, zone):
    make_guild(v3_db, timezone=zone, games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    assert defaults(view.zone_select) == [OTHER_ZONE]
    check_limits(view)
    assert discord_len(view.render()) <= 2000
    assert "<#999>" not in view.render() and "</ban:1>" not in view.render()
    save = await dispatch(view, view.save_button, click())
    text = summary_of(save)
    assert discord_len(text) <= 2000
    assert "</ban:1>" not in text and "<#999>" not in text
    assert not re.search(r"(?<!\u200b)@everyone", text)
    # The wizard writes a zone it didn't offer straight back (it only ever "keeps" it).
    assert state(v3_db)[0].timezone == zone


async def test_a_hostile_stored_zone_gets_the_invalid_zone_line_not_a_timestamp(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, timezone="Mars/Olympus_Mons", games=[("palworld", CH1)])
    view = make_view(v3_cfg, v3_db)
    save = await dispatch(view, view.save_button, click())
    assert "Time zone or digest time is invalid" in summary_of(save)
    assert "<t:" not in summary_of(save)


def test_every_menu_stays_inside_discords_limits_across_states(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"catalog": catalog_of(v3_cfg, 25, LONG_NAMES["astral"])})
    make_guild(v3_db, timezone="Europe/Madrid", digest_time="09:30", games=[("game3", CH1)])
    check_limits(make_view(cfg, v3_db))
    check_limits(make_view(v3_cfg, v3_db))


async def test_redraws_keep_one_zone_default_and_at_most_one_time_default(v3_cfg, v3_db):
    view = make_view(v3_cfg, v3_db)
    for values in (["Asia/Tokyo"], [OTHER_ZONE], ["bogus"], ["UTC"]):
        await dispatch(view, view.zone_select, click(), values)
        assert len(defaults(view.zone_select)) == 1
    await dispatch(view, view.time_select, click(), ["23:00"])
    assert defaults(view.time_select) == ["23:00"]


# --- the next-digest timestamp ---


def expected_next(now, zone, hhmm):
    """The next local `hhmm` strictly after `now`, found by walking days forward."""
    tz = ZoneInfo(zone)
    hour, minute = (int(part) for part in hhmm.split(":"))
    day = now.astimezone(tz).date()
    for offset in range(4):
        d = day + timedelta(days=offset)
        due = datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).astimezone(UTC)
        if due > now:
            return due
    raise AssertionError("no due time in four days")


def freeze(monkeypatch, instant):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(setup_views, "datetime", Frozen)


def stamp(text):
    match = re.search(r"<t:(\d+):f>", text)
    assert match, text
    return datetime.fromtimestamp(int(match.group(1)), UTC)


def iso(text):
    return datetime.fromisoformat(text)


@pytest.mark.parametrize(
    ("zone", "hhmm", "now", "due"),
    [
        ("America/New_York", "09:00", "2026-03-07T20:00:00+00:00", "2026-03-08T13:00:00+00:00"),
        ("America/New_York", "09:00", "2026-03-08T12:59:59+00:00", "2026-03-08T13:00:00+00:00"),
        ("America/New_York", "09:00", "2026-03-08T13:00:00+00:00", "2026-03-09T13:00:00+00:00"),
        ("America/New_York", "09:00", "2026-10-31T20:00:00+00:00", "2026-11-01T14:00:00+00:00"),
        ("Australia/Sydney", "09:00", "2026-10-03T00:00:00+00:00", "2026-10-03T22:00:00+00:00"),
        ("Asia/Kolkata", "00:00", "2026-09-30T18:00:00+00:00", "2026-09-30T18:30:00+00:00"),
        ("Pacific/Honolulu", "09:00", "2026-09-30T23:00:00+00:00", "2026-10-01T19:00:00+00:00"),
    ],
)
def test_the_next_digest_timestamp_is_right_across_dst_and_odd_offsets(
    v3_cfg, v3_db, monkeypatch, zone, hhmm, now, due
):
    freeze(monkeypatch, iso(now))
    view = make_view(v3_cfg, v3_db)
    view.zone, view.digest_time = zone, hhmm
    assert stamp(view._next_line()) == iso(due)


def test_the_next_digest_is_right_for_every_zone_and_hour_around_the_transitions(
    v3_cfg, v3_db, monkeypatch
):
    instants = [
        "2026-03-08T06:30:00+00:00",
        "2026-03-29T00:30:00+00:00",
        "2026-04-04T14:00:00+00:00",
        "2026-10-03T14:30:00+00:00",
        "2026-10-25T00:30:00+00:00",
        "2026-11-01T05:30:00+00:00",
    ]
    view = make_view(v3_cfg, v3_db)
    for text in instants:
        now = iso(text)
        freeze(monkeypatch, now)
        for zone in CURATED_ZONES:
            view.zone = zone
            for hhmm in HOUR_TIMES:
                view.digest_time = hhmm
                got = stamp(view._next_line())
                assert got == expected_next(now, zone, hhmm), (zone, hhmm, text)
                assert now < got <= now + timedelta(hours=26), (zone, hhmm, text)


def test_a_time_inside_the_spring_forward_gap_still_gets_a_sensible_timestamp(
    v3_cfg, v3_db, monkeypatch
):
    now = iso("2026-03-07T20:00:00+00:00")
    freeze(monkeypatch, now)
    view = make_view(v3_cfg, v3_db)
    view.zone, view.digest_time = "America/New_York", "02:00"
    got = stamp(view._next_line())
    assert got == expected_next(now, "America/New_York", "02:00")
    assert now < got <= now + timedelta(hours=26)


async def test_the_summary_carries_the_same_timestamp(v3_cfg, v3_db, perm_lines, monkeypatch):
    now = iso("2026-03-07T20:00:00+00:00")
    freeze(monkeypatch, now)
    view = make_view(v3_cfg, v3_db)
    await view.on_zone(click(), ["America/New_York"])
    await pick(view)
    save = await dispatch(view, view.save_button, click())
    assert stamp(summary_of(save)) == iso("2026-03-08T13:00:00+00:00")
    assert "Digest at 09:00 America/New\\_York" in summary_of(save)


# --- what Save writes ---


async def test_a_rerun_that_changes_nothing_changes_nothing(v3_cfg, v3_db, perm_lines):
    make_guild(
        v3_db,
        digest_time="09:30",
        timezone="Europe/Madrid",
        games=[("rust", CH2), ("palworld", CH1), ("borderlands4", CH3)],
    )
    before = state(v3_db)
    view = make_view(v3_cfg, v3_db)
    await dispatch(view, view.save_button, click())
    after = state(v3_db)
    assert after[1] == before[1]
    assert (after[0].digest_time, after[0].timezone) == ("09:30", "Europe/Madrid")
    assert after[0].set_up is True


async def test_a_rerun_swaps_games_keeps_survivors_in_place_and_sends_newcomers_to_the_tiebreak(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("rust", CH1), ("palworld", CH2)])
    view = make_view(v3_cfg, v3_db)
    assert view.channel_id == CH1  # a tie: the first game added wins
    await view.on_games(click(), ["palworld", "borderlands4"])
    await dispatch(view, view.save_button, click())
    assert [(g.game_key, g.channel_id) for g in state(v3_db)[1]] == [
        ("palworld", CH2),
        ("borderlands4", CH1),
    ]


async def test_picking_the_channel_it_already_shows_still_moves_every_game(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2), ("borderlands4", CH2)])
    view = make_view(v3_cfg, v3_db)
    assert view.channel_id == CH2
    await view.on_channel(click(), [thin_channel(CH2, GUILD_A)])
    assert view.channel_touched is True
    await dispatch(view, view.save_button, click())
    assert {g.channel_id for g in state(v3_db)[1]} == {CH2}


async def test_a_followed_game_missing_from_the_catalog_is_dropped_on_save(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("retired", CH1), ("palworld", CH2), ("gone_too", CH3)])
    view = make_view(v3_cfg, v3_db)
    await dispatch(view, view.save_button, click())
    assert [g.game_key for g in state(v3_db)[1]] == ["palworld"]


def test_the_default_channel_ignores_games_the_catalog_no_longer_has():
    # Changed: retired games used to count, so two of them could outvote a live one. With
    # the catalog's keys passed in they no longer vote (without them, everything counts).
    followed = [
        GuildGame(1, "retired1", CH1),
        GuildGame(1, "retired2", CH1),
        GuildGame(1, "a", CH2),
    ]
    assert most_used_channel(followed, {"a"}) == CH2
    assert most_used_channel(followed, set()) is None
    assert most_used_channel(followed) == CH1


async def test_a_retired_games_channel_no_longer_becomes_the_default_for_a_new_game(
    v3_cfg, v3_db, perm_lines
):
    make_guild(v3_db, games=[("retired1", CH1), ("retired2", CH1), ("palworld", CH2)])
    view = make_view(v3_cfg, v3_db)
    await view.on_games(click(), ["palworld", "rust"])
    await dispatch(view, view.save_button, click())
    assert [(g.game_key, g.channel_id) for g in state(v3_db)[1]] == [
        ("palworld", CH2),
        ("rust", CH2),  # changed: was CH1, the retired games' channel
    ]


async def test_only_retired_games_followed_means_save_needs_a_fresh_pick(
    v3_cfg, v3_db, perm_lines, writes
):
    make_guild(v3_db, games=[("retired", CH1)])
    view = make_view(v3_cfg, v3_db)
    await dispatch(view, view.save_button, click())
    assert "at least one game" in view.render()
    assert writes == []


async def test_a_free_row_becomes_comped_when_the_config_comps_it(v3_cfg, v3_db, perm_lines):
    # Changed from "setup never upgrades": D6 says comped is set at join and at startup,
    # and Save now counts too, so an admin can't be stuck on free while listed as comped.
    make_guild(v3_db, tier="free", set_up=False)
    view = make_view(v3_cfg, v3_db, tier="comped")
    await pick(view)
    await dispatch(view, view.save_button, click())
    assert state(v3_db)[0].tier == "comped"


async def test_a_comped_row_is_never_downgraded_by_a_save(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, tier="comped", set_up=False)
    view = make_view(v3_cfg, v3_db, tier="free")
    await pick(view)
    await dispatch(view, view.save_button, click())
    assert state(v3_db)[0].tier == "comped"


async def test_save_moves_updated_at_but_not_joined_at(v3_cfg, v3_db, perm_lines):
    make_guild(v3_db, set_up=False, games=[("palworld", CH1)])
    with closing(connect(v3_db)) as conn:
        conn.execute("UPDATE guilds SET joined_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
    view = make_view(v3_cfg, v3_db)
    await dispatch(view, view.save_button, click())
    guild, _ = state(v3_db)
    assert guild.joined_at == datetime(2020, 1, 1, tzinfo=UTC)
    assert guild.updated_at > guild.joined_at


# --- apply_guild_setup, straight ---


class FailingConn:
    """A connection that raises on its Nth `execute`, for "what if it dies right here"."""

    def __init__(self, conn, fail_at):
        self.conn, self.fail_at, self.calls = conn, fail_at, 0

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *exc):
        return self.conn.__exit__(*exc)

    def execute(self, sql, params=()):
        self.calls += 1
        if self.calls == self.fail_at:
            raise sqlite3.OperationalError("injected failure")
        return self.conn.execute(sql, params)


SETUP_KWARGS = {
    "digest_time": "05:00",
    "timezone": "Asia/Tokyo",
    "games": [("palworld", CH3), ("rust", CH3), ("borderlands4", CH3)],
}


def _statement_count(db_path):
    with closing(connect(db_path)) as conn:
        probe = FailingConn(conn, fail_at=0)
        repo.apply_guild_setup(probe, GUILD_A, **SETUP_KWARGS)  # type: ignore[arg-type]
        return probe.calls


@pytest.mark.parametrize("existing", [True, False])
def test_a_failure_at_every_statement_rolls_back_to_exactly_the_old_state(tmp_path, existing):
    def fresh(name):
        db = new_db(tmp_path, name)
        make_guild(db, GUILD_B, games=[("rust", CH4)])
        if existing:
            make_guild(
                db,
                GUILD_A,
                set_up=False,
                digest_time="06:00",
                timezone="Europe/Paris",
                games=[("palworld", CH1), ("retired", CH2)],
            )
        return db

    total = _statement_count(fresh("probe"))
    assert total >= 6
    for n in range(1, total + 1):
        db = fresh(f"fail{n}")
        before = every_table(db)
        with closing(connect(db)) as conn:
            with pytest.raises(sqlite3.OperationalError, match="injected"):
                repo.apply_guild_setup(FailingConn(conn, n), GUILD_A, **SETUP_KWARGS)  # type: ignore[arg-type]
        assert every_table(db) == before, f"statement {n} left half a setup behind"


def test_the_connection_is_usable_after_a_rolled_back_setup(v3_db):
    make_guild(v3_db, games=[("palworld", CH1)])
    with closing(connect(v3_db)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            repo.apply_guild_setup(FailingConn(conn, 3), GUILD_A, **SETUP_KWARGS)  # type: ignore[arg-type]
        assert conn.in_transaction is False
        repo.apply_guild_setup(conn, GUILD_A, **SETUP_KWARGS)
    assert [g.game_key for g in state(v3_db)[1]] == ["palworld", "rust", "borderlands4"]


@pytest.mark.parametrize(
    "bad",
    [
        {"digest_time": "24:00"},
        {"digest_time": "9:00"},
        {"digest_time": ""},
        {"games": [("rust", 0)]},
        {"games": [("rust", -1)]},
        {"games": [("rust", CH1), ("palworld", 0)]},
        {"games": [("a", CH1), ("a", CH2)]},
        {"games": [(f"g{i}", CH1) for i in range(11)]},
    ],
)
def test_bad_values_are_refused_and_an_existing_server_is_untouched(v3_db, bad):
    make_guild(v3_db, GUILD_B, games=[("rust", CH4)])
    make_guild(v3_db, digest_time="06:00", timezone="Europe/Paris", games=[("palworld", CH1)])
    before = every_table(v3_db)
    kwargs = {"digest_time": "07:00", "timezone": "UTC", "games": [("rust", CH2)], **bad}
    with closing(connect(v3_db)) as conn:
        with pytest.raises((sqlite3.IntegrityError, ValueError, repo.GameLimitError)):
            repo.apply_guild_setup(conn, GUILD_A, **kwargs)
    assert every_table(v3_db) == before


@pytest.mark.parametrize("guild_id", [0, -1])
def test_a_nonsense_guild_id_creates_nothing(v3_db, guild_id):
    before = every_table(v3_db)
    with closing(connect(v3_db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            repo.apply_guild_setup(
                conn, guild_id, digest_time="07:00", timezone="UTC", games=[("rust", CH1)]
            )
    assert every_table(v3_db) == before


def test_the_repo_trusts_its_caller_about_zones_and_game_keys(v3_db):
    # Pinned: the wizard validates zones and keys before calling; the write itself only
    # enforces the CHECKs the schema has. An unknown zone is stored and the scheduler
    # skips that server later. The owner may want a CHECK or a repo-side guard.
    with closing(connect(v3_db)) as conn:
        repo.apply_guild_setup(
            conn, GUILD_A, digest_time="07:00", timezone="Mars/Olympus_Mons", games=[("", CH1)]
        )
    guild, games = state(v3_db)
    assert guild.timezone == "Mars/Olympus_Mons" and [g.game_key for g in games] == [""]


def test_setting_up_with_no_games_clears_them_or_refuses_but_never_half_does(v3_db):
    make_guild(v3_db, games=[("palworld", CH1), ("rust", CH2)])
    with closing(connect(v3_db)) as conn:
        try:
            repo.apply_guild_setup(conn, GUILD_A, digest_time="07:00", timezone="UTC", games=[])
        except sqlite3.Error, ValueError:
            assert len(state(v3_db)[1]) == 2
            return
    assert state(v3_db)[1] == []


def test_applying_setup_leaves_every_other_servers_rows_identical(v3_db):
    for guild_id, tier in ((GUILD_B, "comped"), (GUILD_C, "free")):
        make_guild(
            v3_db,
            guild_id,
            tier=tier,
            admin_channel_id=CH4,
            games=[("rust", CH3), ("palworld", CH3)],
        )
        with closing(connect(v3_db)) as conn:
            repo.set_shift(conn, guild_id, enabled=True, channel_id=CH3, ping="none")
            repo.add_notice(conn, guild_id, "notice")

    def others():
        snapshot = every_table(v3_db)
        return {
            table: [row for row in rows if GUILD_A not in row[:1]]
            for table, rows in snapshot.items()
            if table in {"guilds", "guild_games", "guild_shift", "guild_notices"}
        }

    scenarios = ("missing", "existing", "after_delete")
    for scenario in scenarios:
        if scenario == "existing":
            make_guild(v3_db, GUILD_A, games=[("palworld", CH1), ("retired", CH2)])
        if scenario == "after_delete":
            with closing(connect(v3_db)) as conn:
                repo.delete_guild(conn, GUILD_A)
        baseline = others()
        with closing(connect(v3_db)) as conn:
            repo.apply_guild_setup(
                conn, GUILD_A, digest_time="07:00", timezone="UTC", games=[("rust", CH1)]
            )
        assert others() == baseline, scenario
