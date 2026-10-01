"""Which v3 commands are registered where, and the hash gate on syncing (plan task 9, §3.7).

`plan_scopes` is pure and gets plain tests. The rest builds a real `NewsBot` (no gateway,
no token, same as `test_command_registration.py`), swaps `tree.sync` for a recorder, and
checks what would have been sent to Discord and for which scope.
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import discord
from pydantic import SecretStr
from v3_fakes import GUILD_A, GUILD_B, HOME, make_guild

from newsbot.bot.client import NewsBot
from newsbot.bot.registration import (
    commands_hash,
    lounge_guild_ids_sync,
    plan_scopes,
    setup_commands,
)
from newsbot.config import Secrets
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import LoungeSettings

L1, L2 = 500000000000000001, 500000000000000002
DEV1, DEV2 = 600000000000000001, 600000000000000002


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def make_bot(cfg, db_path):
    """A `NewsBot` whose `tree.sync` records `(guild id or None)` and returns fake commands."""
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.synced = []

    async def sync(*, guild=None):
        bot.synced.append(guild.id if guild is not None else None)
        return [SimpleNamespace()]

    bot.tree.sync = sync
    return bot


def names(bot, guild_id=None):
    guild = discord.Object(id=guild_id) if guild_id is not None else None
    return sorted(c.name for c in bot.tree.get_commands(guild=guild))


# --- plan_scopes ---


def test_production_plan(v3_cfg):
    plan = plan_scopes(v3_cfg, [L2, L1])
    assert plan.register_global is True
    assert plan.copy_global_to == ()
    assert plan.owner_guilds == (HOME,)
    assert plan.lounge_guilds == (L1, L2)
    assert plan.sync_guilds == (HOME, L1, L2)


def test_production_without_a_home_guild_has_no_owner_command(v3_cfg):
    plan = plan_scopes(v3_cfg.model_copy(update={"home_guild_id": None}), [L1])
    assert plan.owner_guilds == () and plan.sync_guilds == (L1,)


def test_the_home_guild_being_a_lounge_guild_syncs_once(v3_cfg):
    assert plan_scopes(v3_cfg, [HOME]).sync_guilds == (HOME,)


def test_dev_plan_is_guild_scoped_and_never_global(v3_cfg):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [DEV1, DEV2, DEV1]})
    plan = plan_scopes(cfg, [L1, DEV2])
    assert plan.register_global is False
    assert plan.copy_global_to == (DEV1, DEV2)
    assert plan.sync_guilds == (DEV1, DEV2)
    assert plan.lounge_guilds == (DEV2,)  # only listed lounge guilds
    assert plan.owner_guilds == ()  # the home guild isn't listed


def test_dev_plan_with_the_home_guild_listed_gets_owner(v3_cfg):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [HOME, DEV1]})
    assert plan_scopes(cfg, []).owner_guilds == (HOME,)


# --- registration on a real tree ---


async def test_production_registration(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    make_guild(v3_db, L1)
    with closing(connect(v3_db)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(L1, 5, True, "hi", True, "08:00", [], None))
    results = await setup_commands(bot)
    assert names(bot) == ["news", "newsbot", "shift"]
    assert names(bot, HOME) == ["owner"]
    assert names(bot, L1) == ["lounge"]
    assert names(bot, GUILD_A) == []
    assert bot.synced == [None, HOME, L1]
    assert results == {"global": "synced", str(HOME): "synced", str(L1): "synced"}


async def test_dev_registration_syncs_only_the_listed_guilds(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [HOME, DEV1]})
    bot = make_bot(cfg, v3_db)
    await setup_commands(bot)
    assert bot.synced == [HOME, DEV1]  # never None: nothing global
    assert names(bot, DEV1) == ["news", "newsbot", "shift"]
    assert names(bot, HOME) == ["news", "newsbot", "owner", "shift"]


async def test_nothing_but_the_home_guild_ever_gets_owner(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    for guild_id in (GUILD_A, GUILD_B, L1, DEV1):
        assert "owner" not in names(bot, guild_id)
    assert "owner" not in names(bot)


async def test_lounge_needs_the_quote_on_in_a_lounge_row(v3_cfg, v3_db):
    make_guild(v3_db, L1)
    make_guild(v3_db, L2)
    with closing(connect(v3_db)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(L1, 5, True, "hi", False, "08:00", [], None))
        repo.upsert_lounge(conn, LoungeSettings(L2, 5, False, "hi", True, "08:00", [], None))
    assert lounge_guild_ids_sync(v3_db) == [L2]
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    assert names(bot, L2) == ["lounge"] and names(bot, L1) == []


async def test_v2s_quote_now_is_not_in_the_v3_admin_group(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    admin = next(c for c in bot.tree.get_commands() if c.name == "newsbot")
    assert "quote-now" not in {c.name for c in admin.commands}
    assert "test-alert" not in {c.name for c in admin.commands}


async def test_global_groups_are_guild_only_and_admin_gated(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    by_name = {c.name: c for c in bot.tree.get_commands()}
    for group in by_name.values():
        assert group.guild_only is True
        assert group.allowed_installs.guild is True and group.allowed_installs.user is False
    assert by_name["newsbot"].default_permissions.manage_guild is True
    assert by_name["news"].default_permissions is None
    sent = by_name["newsbot"].to_dict(bot.tree)
    assert sent["default_member_permissions"] == discord.Permissions(manage_guild=True).value


async def test_every_registered_name_and_description_fits(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [HOME]})
    make_guild(v3_db, HOME)
    with closing(connect(v3_db)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(HOME, 5, True, "hi", True, "08:00", [], None))
    bot = make_bot(cfg, v3_db)
    await setup_commands(bot)

    def walk(node):
        assert 1 <= len(node.name) <= 32, node.name
        assert 1 <= len(node.description) <= 100, node.name
        for param in getattr(node, "parameters", []):
            assert 1 <= len(param.name) <= 32 and 1 <= len(param.description) <= 100, param.name
        for child in getattr(node, "commands", []):
            walk(child)

    seen = 0
    for top in bot.tree.get_commands(guild=discord.Object(id=HOME)):
        walk(top)
        seen += 1
    assert seen == 5  # news, shift, newsbot, owner, lounge


# --- the hash gate ---


async def test_a_second_start_syncs_nothing(v3_cfg, v3_db):
    first = make_bot(v3_cfg, v3_db)
    await setup_commands(first)
    assert first.synced
    second = make_bot(v3_cfg, v3_db)
    results = await setup_commands(second)
    assert second.synced == []
    assert set(results.values()) == {"unchanged"}


async def test_a_changed_command_syncs_only_its_scope(v3_cfg, v3_db):
    await setup_commands(make_bot(v3_cfg, v3_db))
    changed = v3_cfg.model_copy(update={"admin_permission": "kick_members"})
    bot = make_bot(changed, v3_db)
    results = await setup_commands(bot)
    # The admin group is global, so the global set changed; `/owner` is the same.
    assert results["global"] == "synced"
    assert results[str(HOME)] == "unchanged"


async def test_a_failed_sync_stores_no_hash_and_is_retried(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)

    async def boom(*, guild=None):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="nope"), "boom")

    bot.tree.sync = boom
    results = await setup_commands(bot)
    assert set(results.values()) == {"failed"}
    retry = make_bot(v3_cfg, v3_db)
    assert set((await setup_commands(retry)).values()) == {"synced"}


async def test_one_failing_scope_does_not_stop_the_others(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    calls = []

    async def flaky(*, guild=None):
        calls.append(guild.id if guild else None)
        if guild is None:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="nope"), "boom")
        return []

    bot.tree.sync = flaky
    results = await setup_commands(bot)
    assert results == {"global": "failed", str(HOME): "synced"}
    assert calls == [None, HOME]


async def test_the_hash_is_stable_and_order_independent(v3_cfg, v3_db):
    a, b = make_bot(v3_cfg, v3_db), make_bot(v3_cfg, v3_db)
    await setup_commands(a)
    await setup_commands(b)
    assert commands_hash(a.tree, None) == commands_hash(b.tree, None)
    assert commands_hash(a.tree, None) != commands_hash(a.tree, discord.Object(id=HOME))
