"""Adversarial tests for `/owner servers`, `is_owner` and command registration (plan task 9).

The owner command is the one place the bot looks at every server at once, so the
interesting questions are who can run it (only the application's owner, only in the
home guild, and a team member is not the team's owner) and what it tells them (counts,
and not one name or id of anyone else's server). The registration half asks the D3
question the long way round: across every mix of home guild, lounge guilds and dev
guilds the plan can produce, does any guild ever end up with two commands of the same
name, and what does a failed or repeated sync leave behind?

Some findings are pinned rather than fixed, each with a comment saying why the owner
might care: `is_owner` has no timeout; a sync that fails with something other than an
HTTP error stops the remaining scopes; and a guild that once had commands and no
longer does keeps them (the registration module's own docstring admits the last one).

No gateway, no network; `tree.sync` is a recorder.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from pydantic import SecretStr
from v3_fakes import GUILD_A, GUILD_B, HOME, OWNER_ID, FakeInteraction, command, make_guild

from newsbot.bot.client import NewsBot
from newsbot.bot.commands import (
    make_admin_group,
    make_guild_admin_group,
    make_member_news_group,
    make_member_shift_group,
    make_news_group,
    make_shift_group,
)
from newsbot.bot.owner_commands import make_owner_group
from newsbot.bot.registration import (
    commands_hash,
    plan_scopes,
    register_commands,
    setup_commands,
    sync_commands,
)
from newsbot.config import Secrets, load_config
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import LoungeSettings

DENIED = "You don't have permission to run this."
L1, L2 = 500000000000000001, 500000000000000002
DEV1, DEV2 = 600000000000000001, 600000000000000002
TEAM_MEMBER = 77


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("k"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="nope"), "boom")


def make_bot(cfg, db_path, app_info=None):
    """A real `NewsBot` (no gateway): `tree.sync` records, `application_info` is scripted."""
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.synced = []
    bot.lookups = []

    async def sync(*, guild=None):
        bot.synced.append(guild.id if guild is not None else None)
        return [SimpleNamespace()]

    async def application_info():
        bot.lookups.append(1)
        if isinstance(app_info, BaseException):
            raise app_info
        if callable(app_info):
            return await app_info()
        return app_info

    bot.tree.sync = sync
    bot.application_info = application_info
    return bot


def owned_by(owner_id=OWNER_ID):
    return SimpleNamespace(team=None, owner=SimpleNamespace(id=owner_id))


def team_owned(team_owner=OWNER_ID, pseudo_owner=1):
    return SimpleNamespace(
        team=SimpleNamespace(owner_id=team_owner), owner=SimpleNamespace(id=pseudo_owner)
    )


def servers_callback(cfg, bot):
    return command(make_owner_group(cfg, bot), "servers").callback


def lounge_row(db_path, guild_id, *, quote=True):
    with closing(connect(db_path)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(guild_id, 5, True, "hi", quote, "08:00", [], None))


# --- who can run /owner servers ---


@pytest.mark.parametrize(
    ("who", "guild", "app", "allowed"),
    [
        (OWNER_ID, HOME, owned_by(), True),
        (OWNER_ID + 1, HOME, owned_by(), False),  # a non-owner inside the home guild
        (OWNER_ID, GUILD_A, owned_by(), False),  # the owner, anywhere else
        (OWNER_ID, None, owned_by(), False),  # the owner, in a DM
        (OWNER_ID, HOME, team_owned(), True),  # a team app: the team's owner
        (TEAM_MEMBER, HOME, team_owned(), False),  # a team member who isn't
        (1, HOME, team_owned(), False),  # the team's placeholder "owner" user
        (OWNER_ID, GUILD_B, team_owned(), False),
    ],
    ids=[
        "owner-home",
        "stranger-home",
        "owner-elsewhere",
        "owner-dm",
        "team-owner-home",
        "team-member-home",
        "team-placeholder-user",
        "team-owner-elsewhere",
    ],
)
async def test_only_the_application_owner_in_the_home_guild_gets_the_numbers(
    v3_cfg, v3_db, who, guild, app, allowed
):
    bot = make_bot(v3_cfg, v3_db, app)
    interaction = FakeInteraction(guild_id=guild, user_id=who)
    await servers_callback(v3_cfg, bot)(interaction)
    assert (DENIED not in interaction.text) is allowed
    if allowed:
        assert interaction.text.startswith("Servers: ")
    else:
        assert interaction.text == DENIED and interaction.sent[0]["ephemeral"] is True
    assert all(m["ephemeral"] is True for m in interaction.sent)


async def test_outside_the_home_guild_nobody_is_even_asked_who_they_are(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db, owned_by())
    for guild in (GUILD_A, None, HOME + 1):
        await servers_callback(v3_cfg, bot)(FakeInteraction(guild_id=guild, user_id=OWNER_ID))
    assert bot.lookups == []  # no application_info call, so no REST call an outsider can trigger


async def test_with_no_home_guild_configured_even_the_owner_is_refused(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"home_guild_id": None})
    bot = make_bot(cfg, v3_db, owned_by())
    interaction = FakeInteraction(guild_id=None, user_id=OWNER_ID)
    await servers_callback(cfg, bot)(interaction)
    assert interaction.text == DENIED


async def test_a_denied_owner_command_changes_nothing(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("rust", 1)])
    bot = make_bot(v3_cfg, v3_db, owned_by())
    with closing(connect(v3_db)) as conn:
        before = conn.execute("SELECT COUNT(*), SUM(set_up) FROM guilds").fetchone()[:]
    await servers_callback(v3_cfg, bot)(FakeInteraction(guild_id=HOME, user_id=9))
    with closing(connect(v3_db)) as conn:
        assert conn.execute("SELECT COUNT(*), SUM(set_up) FROM guilds").fetchone()[:] == before


# --- is_owner: failure modes ---


async def test_a_team_owner_lookup_is_cached_and_a_team_member_never_becomes_the_owner(
    v3_cfg, v3_db
):
    bot = make_bot(v3_cfg, v3_db, team_owned())
    assert await bot.is_owner(SimpleNamespace(id=TEAM_MEMBER)) is False
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is True
    assert await bot.is_owner(SimpleNamespace(id=1)) is False
    assert len(bot.lookups) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
async def test_a_failed_lookup_denies_every_time_and_the_next_success_is_remembered(
    v3_cfg, v3_db, status
):
    flaky = [_http_error(status), _http_error(status)]

    async def app():
        if flaky:
            raise flaky.pop()
        return owned_by()

    bot = make_bot(v3_cfg, v3_db, app)
    stranger = SimpleNamespace(id=OWNER_ID + 5)
    owner = SimpleNamespace(id=OWNER_ID)
    assert await bot.is_owner(owner) is False  # Discord couldn't say: deny, even the owner
    assert await bot.is_owner(stranger) is False
    assert await bot.is_owner(owner) is True  # recovered, and now it's cached
    assert await bot.is_owner(stranger) is False
    assert len(bot.lookups) == 3


async def test_a_failed_lookup_is_not_cached_as_a_denial_for_the_real_owner(v3_cfg, v3_db):
    calls = []

    async def app():
        calls.append(1)
        if len(calls) == 1:
            raise _http_error()
        return owned_by()

    bot = make_bot(v3_cfg, v3_db, app)
    owner = SimpleNamespace(id=OWNER_ID)
    assert await bot.is_owner(owner) is False
    assert await bot.is_owner(owner) is True


@pytest.mark.parametrize("exc", [OSError("dns"), TimeoutError()])
async def test_a_network_failure_in_the_lookup_is_a_quiet_no(v3_cfg, v3_db, exc):
    # Changed from "propagates": a dropped connection or a timeout now gets the same
    # quiet denial as an HTTP error, and nothing is cached, so the next try asks again.
    bot = make_bot(v3_cfg, v3_db, exc)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is False
    assert bot._owner_id is None


async def test_an_unexpected_lookup_error_still_propagates(v3_cfg, v3_db):
    # Programming errors aren't swallowed; the tree's error handler reports them.
    bot = make_bot(v3_cfg, v3_db, RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await bot.is_owner(SimpleNamespace(id=OWNER_ID))
    assert bot._owner_id is None


async def test_a_lookup_that_never_answers_is_cut_off_and_says_no(v3_cfg, v3_db, monkeypatch):
    # Changed from "never cut off": the lookup now has a timeout, so a hung
    # `application_info` becomes a quiet no instead of a command that never answers.
    import newsbot.bot.client as client_module

    monkeypatch.setattr(client_module, "_OWNER_LOOKUP_TIMEOUT_S", 0.05)
    never = asyncio.Event()

    async def app():
        await never.wait()

    bot = make_bot(v3_cfg, v3_db, app)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is False
    assert bot._owner_id is None  # the timed-out lookup didn't poison the cache


async def test_the_owner_command_with_a_hung_lookup_sends_nothing_and_grants_nothing(v3_cfg, v3_db):
    never = asyncio.Event()

    async def app():
        await never.wait()

    bot = make_bot(v3_cfg, v3_db, app)
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(servers_callback(v3_cfg, bot)(interaction), timeout=0.05)
    assert interaction.sent == []


async def test_a_lookup_that_raises_inside_the_owner_command_leaks_nothing(v3_cfg, v3_db):
    # Changed with the owner-lookup hardening: a network failure is a quiet no, so the
    # command answers with its ordinary denial and reveals nothing.
    make_guild(v3_db, GUILD_A, set_up=True)
    bot = make_bot(v3_cfg, v3_db, OSError("dns"))
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await servers_callback(v3_cfg, bot)(interaction)
    assert all("Servers:" not in str(m) for m in interaction.sent)


# --- what /owner servers reveals ---

LINE = re.compile(
    r"^(Servers: \d+ \(\d+ set up\)"
    r"|Tiers: \d+ free, \d+ comped"
    r"|SHiFT alerts on: \d+"
    r"|Servers with permission problems: \d+"
    r"|Today's digests: (\d+ [a-z ,]+?)(, \d+ [a-z ,]+?)*"
    r"|Summary spend, \d{4}-\d{2}: about \$\d+\.\d{2})$"
)


async def test_the_owner_reply_is_six_lines_of_numbers_whatever_the_servers_contain(v3_cfg, v3_db):
    hostile = "@everyone <@&1> [x](https://evil.example) secret-name"
    for i in range(1, 8):
        gid = 900000000000000000 + i
        make_guild(
            v3_db,
            gid,
            set_up=i % 2 == 0,
            tier="comped" if i % 3 == 0 else "free",
            admin_channel_id=800000000000000000 + i,
            games=[("rust", 700000000000000000 + i)],
        )
        with closing(connect(v3_db)) as conn:
            repo.add_notice(conn, gid, hostile)
            repo.set_shift(
                conn, gid, enabled=i % 2 == 1, channel_id=600000000000000000 + i, ping="everyone"
            )
            repo.update_guild_settings(conn, gid, permission_problems=f"#{i} {hostile}")
    bot = make_bot(v3_cfg, v3_db, owned_by())
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await servers_callback(v3_cfg, bot)(interaction)
    lines = interaction.text.split("\n")
    assert len(lines) == 6 and all(LINE.match(line) for line in lines), lines
    assert "secret" not in interaction.text and "evil" not in interaction.text
    assert not re.search(r"\d{10,}", interaction.text)  # no snowflake of any kind
    assert len(interaction.text) < 2000
    for message in interaction.sent:
        assert message["ephemeral"] is True
        assert message["allowed_mentions"].everyone is False
        assert message["allowed_mentions"].roles is False


async def test_the_counts_add_up_to_what_was_stored(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, set_up=True, tier="free", games=[("rust", 1)])
    make_guild(v3_db, GUILD_B, set_up=False, tier="comped")
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_A, enabled=True, channel_id=1, ping="none")
        repo.update_guild_settings(conn, GUILD_B, permission_problems="x")
    bot = make_bot(v3_cfg, v3_db, owned_by())
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await servers_callback(v3_cfg, bot)(interaction)
    lines = interaction.text.split("\n")
    assert lines[0] == "Servers: 2 (1 set up)"
    assert lines[1] == "Tiers: 1 free, 1 comped"
    assert lines[2] == "SHiFT alerts on: 1"
    assert lines[3] == "Servers with permission problems: 1"


async def test_a_server_with_a_broken_zone_is_counted_not_fatal(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("rust", 1)])
    make_guild(v3_db, GUILD_B, set_up=True, games=[("rust", 1)])
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, timezone="Mars/Olympus")
    bot = make_bot(v3_cfg, v3_db, owned_by())
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await servers_callback(v3_cfg, bot)(interaction)
    assert "1 bad zone" in interaction.text


async def test_an_empty_database_still_answers(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db, owned_by())
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await servers_callback(v3_cfg, bot)(interaction)
    assert interaction.text.startswith("Servers: 0 (0 set up)")
    assert f"Summary spend, {datetime.now(UTC).strftime('%Y-%m')}: about $0.00" in interaction.text


# --- plan_scopes: every mix ---

HOMES = [None, HOME]
LOUNGES = [[], [L1], [HOME], [L2, L1], [L1, L1], [HOME, L1, DEV1]]
DEVS = [[], [DEV1], [HOME], [L1, DEV1], [DEV1, DEV1], [HOME, L1, DEV1, DEV2]]
COMPED = [[], [GUILD_A], [HOME, L1, DEV1]]


@pytest.mark.parametrize(
    ("home", "lounges", "dev", "comped"),
    list(itertools.product(HOMES, LOUNGES, DEVS, COMPED)),
)
def test_plan_scopes_invariants_hold_for_every_mix(v3_cfg, home, lounges, dev, comped):
    cfg = v3_cfg.model_copy(
        update={"home_guild_id": home, "command_guild_ids": dev, "comped_guild_ids": comped}
    )
    plan = plan_scopes(cfg, iter(lounges))  # a one-shot iterator: it must be read once
    for group in (
        plan.copy_global_to,
        plan.owner_guilds,
        plan.lounge_guilds,
        plan.sync_guilds,
    ):
        assert len(group) == len(set(group))
    assert set(plan.owner_guilds) <= ({home} if home is not None else set())
    assert set(plan.lounge_guilds) <= set(lounges)
    assert set(plan.sync_guilds) == set(plan.owner_guilds) | set(plan.lounge_guilds) | set(
        plan.copy_global_to
    )
    if not dev:  # production
        assert plan.register_global is True and plan.copy_global_to == ()
        assert set(plan.owner_guilds) == ({home} if home is not None else set())
        assert set(plan.lounge_guilds) == set(lounges)
    else:  # dev and self-host: never global, only the listed guilds
        assert plan.register_global is False
        assert set(plan.sync_guilds) == set(dev)
        assert set(plan.copy_global_to) == set(dev)
        assert set(plan.lounge_guilds) == set(lounges) & set(dev)
        assert set(plan.owner_guilds) == ({home} & set(dev) if home is not None else set())


def test_comped_guilds_never_change_what_is_registered_where(v3_cfg):
    base = plan_scopes(v3_cfg.model_copy(update={"comped_guild_ids": []}), [L1])
    comped = plan_scopes(v3_cfg.model_copy(update={"comped_guild_ids": [L1, HOME, GUILD_A]}), [L1])
    assert base == comped


def test_the_plan_is_frozen_so_a_caller_cannot_quietly_redirect_a_sync(v3_cfg):
    plan = plan_scopes(v3_cfg, [L1])
    with pytest.raises(AttributeError):
        plan.register_global = False  # type: ignore[misc]


def test_a_home_guild_outside_the_dev_list_warns_and_gets_no_owner_command(v3_cfg, v3_db, caplog):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [DEV1]})
    bot = make_bot(cfg, v3_db)
    with caplog.at_level(logging.WARNING):
        register_commands(bot, plan_scopes(cfg, []))
    assert "no /owner command" in caplog.text
    assert [c.name for c in bot.tree.get_commands(guild=discord.Object(id=HOME))] == []


# --- D3: no guild ever sees the same command name twice ---


def visible_names(bot, plan, guild_id):
    """What Discord would show in `guild_id`: the guild's own synced set, plus the global one."""
    scoped = [c.name for c in bot.tree.get_commands(guild=discord.Object(id=guild_id))]
    shown = scoped + ([c.name for c in bot.tree.get_commands()] if plan.register_global else [])
    return shown


@pytest.mark.parametrize(
    ("dev", "lounges"),
    [
        ([], []),
        ([], [L1]),
        ([], [HOME, L1]),
        ([HOME, DEV1], []),
        ([HOME, DEV1], [HOME, L1]),
        ([L1, DEV1], [L1]),
        ([DEV1, DEV2], [DEV1, DEV2]),
    ],
)
async def test_no_guild_shows_two_commands_with_the_same_name(v3_cfg, v3_db, dev, lounges):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": dev})
    for guild_id in lounges:
        make_guild(v3_db, guild_id)
        lounge_row(v3_db, guild_id)
    bot = make_bot(cfg, v3_db)
    plan = plan_scopes(cfg, lounges)
    register_commands(bot, plan)
    for guild_id in {HOME, L1, L2, DEV1, DEV2, GUILD_A, *dev, *lounges}:
        shown = visible_names(bot, plan, guild_id)
        assert len(shown) == len(set(shown)), (guild_id, shown)


async def test_the_friends_guild_is_cleaned_of_v2s_newsbot_only_because_it_is_synced(v3_cfg, v3_db):
    # D3's worry. v2 synced a guild-scoped `/newsbot` set into the friend's server. In v3
    # the same name is global, so a guild that *isn't* synced keeps v2's copy next to the
    # global one (two `/newsbot`s). A guild that *is* synced gets its guild set replaced
    # wholesale by what v3 registers for it, which never includes `/newsbot`.
    make_guild(v3_db, L1)
    lounge_row(v3_db, L1)
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    assert L1 in bot.synced
    body = [c.to_dict(bot.tree)["name"] for c in bot.tree.get_commands(guild=discord.Object(id=L1))]
    assert body == ["lounge"]  # the sync body for the friend's server: no /newsbot in it


@pytest.mark.parametrize(
    "lounge_state",
    ["no-row", "quote-off"],
)
async def test_a_friends_guild_without_a_live_lounge_is_not_synced_so_v2s_newsbot_survives(
    v3_cfg, v3_db, lounge_state
):
    # The gap task 13 has to close (and `docs/deploy.md` should say so): with no lounge
    # quote, nothing syncs this guild, so v2's guild-scoped `/newsbot` stays next to the
    # new global one. Pinned here so the clean-up is a tracked to-do, not a surprise.
    make_guild(v3_db, L1)
    if lounge_state == "quote-off":
        lounge_row(v3_db, L1, quote=False)
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    assert L1 not in bot.synced


async def test_registering_v3_on_top_of_v2s_tree_fails_loudly_instead_of_doubling_up(v3_db):
    cfg = load_config("tests/fixtures/config_valid.yaml")
    bot = make_bot(cfg, v3_db)
    bot.tree.add_command(make_news_group(cfg, v3_db))
    bot.tree.add_command(make_admin_group(cfg, bot))
    with pytest.raises(app_commands.CommandAlreadyRegistered):
        register_commands(bot, plan_scopes(cfg, []))


async def test_registering_twice_fails_loudly_too(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    plan = plan_scopes(v3_cfg, [])
    register_commands(bot, plan)
    with pytest.raises(app_commands.CommandAlreadyRegistered):
        register_commands(bot, plan)


# --- Discord's limits, for every command the v3 bot can register ---


def _walk(payload):
    yield payload
    for option in payload.get("options", []):
        yield from _walk(option)


def _chars(payload) -> int:
    total = 0
    for node in _walk(payload):
        total += len(node.get("name", "")) + len(node.get("description", ""))
        for choice in node.get("choices", []):
            total += len(str(choice["name"])) + len(str(choice["value"]))
    return total


async def test_every_registered_command_obeys_discords_shape_limits(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [HOME, L1]})
    make_guild(v3_db, L1)
    lounge_row(v3_db, L1)
    bot = make_bot(cfg, v3_db)
    await setup_commands(bot)
    name_ok = re.compile(r"^[\w-]{1,32}$")
    top_level = 0
    for guild in (None, discord.Object(id=HOME), discord.Object(id=L1)):
        commands = bot.tree.get_commands(guild=guild)
        assert len(commands) <= 100
        for top in commands:
            top_level += 1
            payload = top.to_dict(bot.tree)
            assert _chars(payload) <= 8000
            for node in _walk(payload):
                assert name_ok.match(node["name"]) and node["name"] == node["name"].lower()
                assert 1 <= len(node["description"]) <= 100, node["name"]
                assert len(node.get("options", [])) <= 25
                assert len(node.get("choices", [])) <= 25
                for choice in node.get("choices", []):
                    assert 1 <= len(choice["name"]) <= 100
                    if isinstance(choice["value"], str):
                        assert 1 <= len(choice["value"]) <= 100
    assert top_level >= 6  # the walk actually saw the commands


def test_no_subcommand_has_two_options_with_the_same_name(v3_cfg, v3_db):
    bot = SimpleNamespace(db_path=v3_db)
    for group in (
        make_member_news_group(v3_cfg, v3_db),
        make_member_shift_group(v3_cfg, v3_db),
        make_guild_admin_group(v3_cfg, bot),
    ):
        for sub in group.commands:
            names = [p.name for p in sub.parameters]
            assert len(names) == len(set(names)), (group.name, sub.name)


# --- the hash gate ---


def _digest_rows(db_path):
    with closing(connect(db_path)) as conn:
        return dict(
            conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'commands:%'").fetchall()
        )


async def test_a_hash_stored_for_one_scope_is_not_believed_for_another(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    register_commands(bot, plan_scopes(v3_cfg, []))
    with closing(connect(v3_db)) as conn:
        repo.app_state_set(
            conn, "commands:global", commands_hash(bot.tree, discord.Object(id=HOME))
        )
    results = await sync_commands(bot, plan_scopes(v3_cfg, []))
    assert results["global"] == "synced"  # the global hash was wrong, so it re-synced


@pytest.mark.parametrize("junk", ["", "0", "not-a-hash", "x" * 10_000, "😀"])
async def test_a_garbage_stored_hash_just_means_sync_again(v3_cfg, v3_db, junk):
    bot = make_bot(v3_cfg, v3_db)
    register_commands(bot, plan_scopes(v3_cfg, []))
    with closing(connect(v3_db)) as conn:
        repo.app_state_set(conn, "commands:global", junk)
    results = await sync_commands(bot, plan_scopes(v3_cfg, []))
    assert results["global"] == "synced"
    assert _digest_rows(v3_db)["commands:global"] == commands_hash(bot.tree, None)


async def test_a_failed_sync_keeps_the_old_hash_so_the_next_start_retries(v3_cfg, v3_db):
    first = make_bot(v3_cfg, v3_db)
    await setup_commands(first)
    old = _digest_rows(v3_db)
    changed = v3_cfg.model_copy(update={"admin_permission": "kick_members"})
    second = make_bot(changed, v3_db)

    async def boom(*, guild=None):
        raise _http_error()

    second.tree.sync = boom
    results = await setup_commands(second)
    assert results["global"] == "failed"
    assert _digest_rows(v3_db)["commands:global"] == old["commands:global"]  # not advanced
    third = make_bot(changed, v3_db)
    assert (await setup_commands(third))["global"] == "synced"


async def test_only_the_failing_scope_goes_without_a_hash(v3_cfg, v3_db):
    make_guild(v3_db, L1)
    lounge_row(v3_db, L1)
    bot = make_bot(v3_cfg, v3_db)

    async def flaky(*, guild=None):
        if guild is not None and guild.id == HOME:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Missing Access"), "no")
        bot.synced.append(guild.id if guild else None)
        return []

    bot.tree.sync = flaky
    results = await setup_commands(bot)
    assert results == {"global": "synced", str(HOME): "failed", str(L1): "synced"}
    assert set(_digest_rows(v3_db)) == {"commands:global", f"commands:{L1}"}


async def test_a_sync_error_that_is_not_an_http_error_skips_only_its_own_scope(v3_cfg, v3_db):
    # Changed: `sync_commands` now catches any Exception per scope, as its docstring
    # always promised, so a dropped connection costs that scope its sync (and its
    # hash) but the others still run and the bot still starts.
    make_guild(v3_db, L1)
    lounge_row(v3_db, L1)
    bot = make_bot(v3_cfg, v3_db)
    seen = []

    async def dropped(*, guild=None):
        seen.append(guild.id if guild else None)
        if guild is not None and guild.id == HOME:
            raise OSError("connection reset")
        return []

    bot.tree.sync = dropped
    await setup_commands(bot)
    assert seen == [None, HOME, L1]  # L1 still got its turn
    assert set(_digest_rows(v3_db)) == {"commands:global", f"commands:{L1}"}


async def test_the_hash_moves_when_anything_a_member_could_see_changes(v3_cfg, v3_db):
    def hash_with(cfg):
        bot = make_bot(cfg, v3_db)
        register_commands(bot, plan_scopes(cfg, []))
        return commands_hash(bot.tree, None)

    base = hash_with(v3_cfg)
    assert hash_with(v3_cfg) == base
    assert hash_with(v3_cfg.model_copy(update={"admin_permission": "kick_members"})) != base
    # A config change that doesn't touch any command leaves the hash alone.
    assert hash_with(v3_cfg.model_copy(update={"run_report": False})) == base


async def test_changing_the_owner_group_moves_its_own_hash_and_not_the_global_one(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    register_commands(bot, plan_scopes(v3_cfg, []))
    home = discord.Object(id=HOME)
    global_before, home_before = commands_hash(bot.tree, None), commands_hash(bot.tree, home)
    bot.tree.get_commands(guild=home)[0].description = "changed"
    assert commands_hash(bot.tree, None) == global_before
    assert commands_hash(bot.tree, home) != home_before


async def test_switching_from_dev_to_production_resyncs_the_shared_guild(v3_cfg, v3_db):
    # The same guild id is a dev copy-target one day and a home guild the next; its
    # scope hash covers different commands, so the switch re-syncs it (and replaces the
    # dev copies with just `/owner`).
    dev = v3_cfg.model_copy(update={"command_guild_ids": [HOME]})
    await setup_commands(make_bot(dev, v3_db))
    prod = make_bot(v3_cfg, v3_db)
    results = await setup_commands(prod)
    assert results[str(HOME)] == "synced"


async def test_a_dev_guild_dropped_from_the_list_is_never_cleaned_up(v3_cfg, v3_db):
    # Documented gap (the registration docstring owns up to its lounge twin): removing a
    # guild from `command_guild_ids`, or moving to production, leaves its old command
    # copies in place. Nothing here clears them; `bot.synced` never mentions the guild.
    await setup_commands(
        make_bot(v3_cfg.model_copy(update={"command_guild_ids": [DEV1, DEV2]}), v3_db)
    )
    prod = make_bot(v3_cfg, v3_db)
    await setup_commands(prod)
    assert DEV1 not in prod.synced and DEV2 not in prod.synced


async def test_a_lounge_guild_that_loses_its_lounge_is_not_cleared(v3_cfg, v3_db):
    make_guild(v3_db, L1)
    lounge_row(v3_db, L1)
    await setup_commands(make_bot(v3_cfg, v3_db))
    lounge_row(v3_db, L1, quote=False)
    bot = make_bot(v3_cfg, v3_db)
    results = await setup_commands(bot)
    assert str(L1) not in results  # still carries its old `/lounge`; the handler refuses it


async def test_a_new_lounge_guild_is_synced_alone(v3_cfg, v3_db):
    make_guild(v3_db, L1)
    lounge_row(v3_db, L1)
    await setup_commands(make_bot(v3_cfg, v3_db))
    make_guild(v3_db, L2)
    lounge_row(v3_db, L2)
    bot = make_bot(v3_cfg, v3_db)
    results = await setup_commands(bot)
    assert results == {
        "global": "unchanged",
        str(HOME): "unchanged",
        str(L1): "unchanged",
        str(L2): "synced",
    }
    assert bot.synced == [L2]


# --- v2 is still what the bot registers today ---


async def test_the_running_bots_setup_path_still_registers_the_v2_set_only(v3_db):
    cfg = load_config("tests/fixtures/config_valid.yaml")
    bot = make_bot(cfg, v3_db)
    try:
        await bot.setup_hook()
    finally:
        if bot.scheduler is not None:
            bot.scheduler.shutdown(wait=False)
    guild = discord.Object(id=cfg.guild_id)
    names = {c.name for c in bot.tree.get_commands(guild=guild)}
    assert names <= {"news", "newsbot", "shift"} and {"news", "newsbot"} <= names
    assert "owner" not in names and "lounge" not in names
    assert bot.synced == [cfg.guild_id]  # per-guild, never global, exactly as in v2
    sub = {
        c.name
        for g in bot.tree.get_commands(guild=guild)
        if g.name == "newsbot"
        for c in g.commands
    }
    assert {"status", "run-now", "preview"} <= sub
    assert not {"follow", "unfollow", "games", "settings"} & sub  # v3's admin verbs aren't there


def test_the_v2_factories_still_build_their_v2_shapes(v3_db):
    cfg = load_config("tests/fixtures/config_valid.yaml")
    assert {c.name for c in make_news_group(cfg, v3_db).commands} == {"recent", "search"}
    assert {c.name for c in make_shift_group(cfg, v3_db).commands} == {"codes"}
    admin = make_admin_group(cfg, SimpleNamespace(db_path=v3_db))
    assert {"status", "run-now", "preview"} <= {c.name for c in admin.commands}
    assert not {"follow", "unfollow", "games", "settings", "shift"} & {
        c.name for c in admin.commands
    }


def test_v3_builds_its_own_groups_without_touching_v2s(v3_cfg, v3_db):
    # The v3 factories share names with v2's on purpose (same slash commands) but are
    # distinct objects with distinct commands; building one set must not mutate the other.
    v2 = make_news_group(v3_cfg, v3_db)
    v3 = make_member_news_group(v3_cfg, v3_db)
    assert v2 is not v3
    assert [c.name for c in v2.commands] == ["recent", "search"]
    v3.commands[0].description = "changed"
    assert v2.commands[0].description != "changed"
