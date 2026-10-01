"""Guild lifecycle at the cutover: the first `on_ready` meets servers it has never heard of.

Until today the bot lived in exactly one server and had never had to wonder who else was in
the building. Now `on_ready` runs reconciliation, which compares the database with whatever
Discord's cache says and acts on the difference, and on cutover day that comparison is
unusually lopsided: the database knows the friend's server (just imported) and the cache
knows the friend's server plus the owner's own home or test server, which has no row and is
about to get a free one and a hello. That's all intended. The things worth being suspicious
about are the edges: the friend's imported server must never be treated as new (no hello, no
second row, no downgrade), an outage that shows an empty guild list must not read as
everyone leaving, and a join that lands in the middle of startup must say hello once, not
twice, because two joins racing to be first is how a bot ends up introducing itself twice.

Removal and re-invite are here too. The import happens once, ever, so a friend who kicks the
bot and invites it back gets a fresh, unconfigured server. Whether that server is comped
depends on `comped_guild_ids`: left out, it comes back free (pinned below); listed, which is
what the owner decided on 2026-10-01 for prod, it comes back comped. Either way the old
games, SHiFT settings and lounge are gone and it starts at `/newsbot setup`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from cutover_world import (
    DUE,
    FRIEND,
    OWNER_GUILD,
    Chan,
    add_item,
    fake_gateway,
    guild_stub,
)

from newsbot.guilds import lifecycle

T_NOON = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)


def friend_stub() -> SimpleNamespace:
    return guild_stub(FRIEND, name="The Friend's Server", channels=[Chan(91, FRIEND)])


def owner_stub() -> SimpleNamespace:
    return guild_stub(OWNER_GUILD, name="The Owner's Test Server", channels=[Chan(92, OWNER_GUILD)])


def hellos(stub) -> list:
    return [m for chan in stub.text_channels for m in chan.sent]


def guild_rows(world):
    return world.rows("SELECT guild_id, tier, set_up FROM guilds ORDER BY guild_id")


# --- the first on_ready after the import ---


async def test_the_home_server_gets_a_free_row_and_one_hello_and_the_friend_gets_nothing(
    make_world, monkeypatch
):
    world = await make_world(now=T_NOON, owner_channel=True)
    friend, owner = friend_stub(), owner_stub()
    fake_gateway(world.bot, monkeypatch, [friend, owner])

    await world.ready()

    assert guild_rows(world) == [(FRIEND, "comped", 1), (OWNER_GUILD, "free", 0)]
    (hello,) = hellos(owner)
    assert hello.content == lifecycle.FIRST_CONTACT_TEXT
    mentions = hello.allowed_mentions
    assert (mentions.everyone, mentions.roles, mentions.users) == (False, False, False)
    assert hellos(friend) == []  # an imported server has already been introduced
    assert world.rows("SELECT imported_at IS NOT NULL FROM guilds WHERE guild_id = ?", FRIEND) == [
        (1,)
    ]


async def test_a_second_reconcile_and_a_reconnect_say_nothing_more(make_world, monkeypatch):
    world = await make_world(now=T_NOON, owner_channel=True)
    friend, owner = friend_stub(), owner_stub()
    fake_gateway(world.bot, monkeypatch, [friend, owner])
    await world.ready()

    await world.ready()
    await world.bot.lifecycle.reconcile(world.bot)

    assert len(hellos(owner)) == 1 and hellos(friend) == []


async def test_the_free_home_server_does_not_change_the_prompt_the_friend_gets(
    make_world, monkeypatch
):
    # The plan's prompt rule: the AI prompt names every game a comped server follows. A home
    # server that has a row but follows nothing, free or comped, adds nothing to it.
    from cutover_world import PRODLIKE

    from newsbot.config import load_config

    cfg = load_config(PRODLIKE).model_copy(update={"comped_guild_ids": [OWNER_GUILD]})
    world = await make_world(now=DUE - timedelta(minutes=1), cfg=cfg, owner_channel=True)
    fake_gateway(world.bot, monkeypatch, [friend_stub(), owner_stub()])
    add_item(world.db_path, "palworld", "p", DUE - timedelta(hours=1))

    await world.ready()
    await world.tick(DUE)

    assert world.rows("SELECT tier FROM guilds WHERE guild_id = ?", OWNER_GUILD) == [("comped",)]
    assert world.llm.systems  # a summary really was made
    assert all("Borderlands 4, Palworld and Diablo IV" in system for system in world.llm.systems)


async def test_an_outage_that_shows_no_servers_does_not_delete_the_friend(make_world, monkeypatch):
    world = await make_world(now=T_NOON, owner_channel=True)
    fake_gateway(world.bot, monkeypatch, [])

    async def still_there(guild_id):
        return SimpleNamespace(id=guild_id)

    monkeypatch.setattr(world.bot, "fetch_guild", still_there)

    await world.ready()

    assert guild_rows(world) == [(FRIEND, "comped", 1)]
    assert world.bot._digests_enabled is True
    told = [m.content for ch in world.channels.values() for m in ch.sent if m.content]
    assert any("Startup cleanup paused" in t for t in told)


async def test_a_friend_who_kicked_the_bot_while_it_was_down_is_cleaned_up_once_discord_agrees(
    make_world, monkeypatch
):
    world = await make_world(now=T_NOON, owner_channel=True)
    fake_gateway(world.bot, monkeypatch, [owner_stub()])

    async def not_a_member(guild_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Guild")

    monkeypatch.setattr(world.bot, "fetch_guild", not_a_member)

    await world.ready()

    assert [row[0] for row in guild_rows(world)] == [OWNER_GUILD]
    assert world.bot._digests_enabled is True


# --- removal, re-invite and the import marker ---


async def test_kicking_the_bot_drops_the_quote_the_lounge_and_the_rows_but_not_the_marker(
    make_world,
):
    world = await make_world(now=T_NOON)
    await world.ready()
    assert world.bot.scheduler.get_job(f"daily-quote-{FRIEND}") is not None

    await world.bot.on_guild_remove(SimpleNamespace(id=FRIEND))

    assert world.bot.scheduler.get_job(f"daily-quote-{FRIEND}") is None
    assert FRIEND not in world.bot._lounges
    for table in ("guilds", "guild_games", "guild_shift", "guild_lounge"):
        assert world.rows(f"SELECT COUNT(*) FROM {table}") == [(0,)], table  # noqa: S608
    assert world.rows("SELECT COUNT(*) FROM app_state WHERE key = 'import'") == [(1,)]


async def test_a_restart_after_the_kick_does_not_resurrect_the_server_or_post_to_it(
    make_world, monkeypatch
):
    world = await make_world(now=DUE - timedelta(minutes=2))
    await world.ready()
    await world.bot.on_guild_remove(SimpleNamespace(id=FRIEND))
    add_item(world.db_path, "palworld", "p", DUE - timedelta(hours=1))

    again = await make_world(now=DUE, channels=world.channels)
    assert again.report is None  # the import marker outlives the rows
    fake_gateway(again.bot, monkeypatch, [])
    await again.ready()
    await again.tick(DUE)

    assert guild_rows(again) == []
    assert [m for ch in again.channels.values() for m in ch.sent if m.embed] == []


async def test_inviting_the_bot_back_makes_an_ordinary_free_server_and_says_hello_once(
    make_world,
):
    # Pinned for the owner: nothing remembers that this server used to be the comped one.
    world = await make_world(now=T_NOON)
    await world.ready()
    await world.bot.on_guild_remove(SimpleNamespace(id=FRIEND))
    again = friend_stub()

    await world.bot.on_guild_join(again)
    await world.bot.on_guild_join(again)  # Discord sometimes says it twice

    assert guild_rows(world) == [(FRIEND, "free", 0)]
    assert len(hellos(again)) == 1


async def test_a_listed_friend_who_kicks_and_reinvites_comes_back_comped_but_unconfigured(
    make_world, monkeypatch
):
    # Owner decision 2026-10-01: prod lists the friend's guild in comped_guild_ids.
    from cutover_world import PRODLIKE

    from newsbot.config import load_config

    cfg = load_config(PRODLIKE).model_copy(update={"comped_guild_ids": [FRIEND]})
    world = await make_world(now=T_NOON, cfg=cfg)
    await world.ready()
    assert guild_rows(world) == [(FRIEND, "comped", 1)]
    await world.bot.on_guild_remove(SimpleNamespace(id=FRIEND))
    assert guild_rows(world) == []
    again = friend_stub()

    await world.bot.on_guild_join(again)
    await world.bot.on_guild_join(again)  # Discord sometimes says it twice

    # Comped, but a brand new row: not set up, so no digest until /newsbot setup.
    assert guild_rows(world) == [(FRIEND, "comped", 0)]
    assert len(hellos(again)) == 1
    assert hellos(again)[0].content == lifecycle.FIRST_CONTACT_TEXT
    assert world.rows("SELECT imported_at FROM guilds WHERE guild_id = ?", FRIEND) == [(None,)]
    for table in ("guild_games", "guild_shift", "guild_lounge"):
        assert world.rows(f"SELECT COUNT(*) FROM {table}") == [(0,)], table  # noqa: S608

    # A restart doesn't bring the old setup back: no second import, no lounge re-sync.
    again_world = await make_world(now=T_NOON, cfg=cfg, channels=world.channels)
    assert again_world.report is None
    fake_gateway(again_world.bot, monkeypatch, [again])
    await again_world.ready()
    assert guild_rows(again_world) == [(FRIEND, "comped", 0)]
    for table in ("guild_games", "guild_shift", "guild_lounge"):
        assert again_world.rows(f"SELECT COUNT(*) FROM {table}") == [(0,)], table  # noqa: S608


# --- joins that land mid-startup ---


async def test_a_join_that_lands_before_reconcile_runs_is_not_greeted_twice(
    make_world, monkeypatch
):
    world = await make_world(now=T_NOON, owner_channel=True)
    stranger = guild_stub(777, channels=[Chan(7771, 777)])
    fake_gateway(world.bot, monkeypatch, [friend_stub(), owner_stub(), stranger])

    await world.bot.on_guild_join(stranger)  # the event arrives while setup is still going
    await world.ready()  # and then reconcile finds the guild in the cache

    assert len(hellos(stranger)) == 1
    assert sorted(row[0] for row in guild_rows(world)) == [777, FRIEND, OWNER_GUILD]


async def test_a_join_racing_reconcile_is_greeted_once(make_world, monkeypatch):
    world = await make_world(now=T_NOON, owner_channel=True)
    stranger = guild_stub(778, channels=[Chan(7781, 778)])
    fake_gateway(world.bot, monkeypatch, [friend_stub(), stranger])

    await asyncio.gather(
        world.bot.on_guild_join(stranger), world.bot.lifecycle.reconcile(world.bot)
    )

    assert len(hellos(stranger)) == 1
    assert sorted(row[0] for row in guild_rows(world)) == [778, FRIEND]


async def test_a_server_the_bot_cannot_speak_in_still_gets_its_row_and_no_hello(
    make_world, monkeypatch
):
    world = await make_world(now=T_NOON, owner_channel=True)
    mute = Chan(7791, 779, permissions=discord.Permissions.none())
    stranger = guild_stub(779, channels=[mute])
    fake_gateway(world.bot, monkeypatch, [friend_stub(), stranger])

    await world.ready()

    assert (779, "free", 0) in guild_rows(world)
    assert mute.sent == []


@pytest.mark.parametrize("name", ["@everyone", "<@&123456789012345678> @here", "[x](http://evil)"])
async def test_a_hostile_server_name_never_reaches_the_hello(make_world, monkeypatch, name):
    world = await make_world(now=T_NOON, owner_channel=True)
    stranger = guild_stub(780, name=name, channels=[Chan(7801, 780)])
    fake_gateway(world.bot, monkeypatch, [friend_stub(), stranger])

    await world.ready()

    (hello,) = hellos(stranger)
    assert hello.content == lifecycle.FIRST_CONTACT_TEXT
    assert hello.allowed_mentions.everyone is False and hello.allowed_mentions.roles is False
    owner_side = [m.content for ch in world.channels.values() for m in ch.sent if m.content]
    assert all(name not in text for text in owner_side)  # nobody else hears the name either
