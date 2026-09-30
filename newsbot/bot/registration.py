"""Which slash commands exist where, and telling Discord about them only when they changed.

The public bot has three kinds of command. Most are global: `/news`, `/shift` and
`/newsbot` show up in every server that installs the app. Two are not: `/owner` is
for the owner's home guild alone, and `/lounge` is for the servers that have a
lounge. Discord registers those two per guild, so `plan_scopes` (pure, and the
part worth testing) works out which guilds get what.

There are two modes, picked by `command_guild_ids`. Empty is production: sync the
global set, plus the home and lounge guilds for their own commands. Non-empty is
dev (and self-hosting): copy everything into just those guilds and never sync
globally, because a global change takes up to an hour to show and waiting an hour
to find out you misspelled a description is a hobby, not a workflow.

Syncing overwrites whatever Discord has, and doing that on every restart is how a
bot ends up rate-limited by its own enthusiasm. So each scope's commands are
hashed (the same JSON Discord would be sent) and compared with the hash stored in
`app_state` after the last successful sync; nothing changed, nothing sent. A failed
sync stores no hash, so the next start tries again.

One known gap: a guild that *used* to have guild commands and no longer does (its
lounge row was deleted) isn't in the plan, so its stale `/lounge` isn't cleared
here. The command refuses to do anything in such a guild (it checks the row), so
it's clutter, not a hole.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands

from newsbot.bot.client import NewsBot
from newsbot.bot.commands import (
    make_guild_admin_group,
    make_lounge_group,
    make_member_news_group,
    make_member_shift_group,
)
from newsbot.bot.owner_commands import make_owner_group
from newsbot.config import AppConfig
from newsbot.store import repo
from newsbot.store.db import connect

logger = logging.getLogger(__name__)

_HASH_KEY_PREFIX = "commands:"


@dataclass(frozen=True)
class ScopePlan:
    """Where each command set goes. Every tuple is de-duplicated.

    `copy_global_to` is non-empty only in dev mode. `sync_guilds` is every guild whose
    command set gets synced; `register_global` says whether the global set is synced.
    """

    register_global: bool
    copy_global_to: tuple[int, ...]
    owner_guilds: tuple[int, ...]
    lounge_guilds: tuple[int, ...]
    sync_guilds: tuple[int, ...]


def _unique(ids: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(ids))


def plan_scopes(cfg: AppConfig, lounge_guild_ids: Iterable[int]) -> ScopePlan:
    """Work out the registration scopes from the config and the lounge guilds. Pure.

    Production (`command_guild_ids` empty): global set, `/owner` in the home guild,
    `/lounge` in each lounge guild, and a sync of each of those guilds.

    Dev and self-host (`command_guild_ids` non-empty): nothing global; the global set
    is copied into each listed guild and only those guilds are synced. `/owner` and
    `/lounge` come along only for listed guilds (a home guild that isn't listed gets no
    `/owner`, which `register_commands` logs).
    """
    lounges = _unique(sorted(lounge_guild_ids))
    if not cfg.command_guild_ids:
        owner = (cfg.home_guild_id,) if cfg.home_guild_id is not None else ()
        return ScopePlan(True, (), owner, lounges, _unique([*owner, *lounges]))
    listed = _unique(cfg.command_guild_ids)
    owner = (cfg.home_guild_id,) if cfg.home_guild_id in listed else ()
    return ScopePlan(False, listed, owner, tuple(g for g in lounges if g in listed), listed)


def register_commands(bot: NewsBot, plan: ScopePlan) -> None:
    """Add the v3 command groups to `bot.tree` according to `plan`. Sends nothing to Discord."""
    tree, cfg = bot.tree, bot.cfg
    tree.add_command(make_member_news_group(cfg, bot.db_path))
    tree.add_command(make_member_shift_group(cfg, bot.db_path))
    tree.add_command(make_guild_admin_group(cfg, bot))
    for guild_id in plan.copy_global_to:
        tree.copy_global_to(guild=discord.Object(id=guild_id))
    if plan.owner_guilds:
        owner = make_owner_group(cfg, bot)
        for guild_id in plan.owner_guilds:
            tree.add_command(owner, guild=discord.Object(id=guild_id))
    elif cfg.home_guild_id is not None:
        logger.warning("no /owner command: the home guild isn't in command_guild_ids")
    if plan.lounge_guilds:
        tree.add_command(
            make_lounge_group(cfg, bot), guilds=[discord.Object(id=g) for g in plan.lounge_guilds]
        )


def commands_hash(tree: app_commands.CommandTree, guild: discord.abc.Snowflake | None) -> str:
    """A stable hash of the commands that would be synced for `guild` (None is the global set)."""
    payload = sorted(
        (command.to_dict(tree) for command in tree.get_commands(guild=guild)),
        key=lambda d: d["name"],
    )
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _stored_hash_sync(db_path: str | Path, key: str) -> str | None:
    with closing(connect(db_path)) as conn:
        return repo.app_state_get(conn, key)


def _store_hash_sync(db_path: str | Path, key: str, value: str) -> None:
    with closing(connect(db_path)) as conn:
        repo.app_state_set(conn, key, value)


async def sync_commands(bot: NewsBot, plan: ScopePlan) -> dict[str, str]:
    """Sync each scope whose command hash changed. Returns scope to `synced`/`unchanged`/`failed`.

    Scopes are `global` and each guild id as text. A Discord error on one scope is
    logged and leaves its hash unstored (so the next start retries); it doesn't stop
    the others, and it doesn't stop the bot from starting.
    """
    scopes: list[tuple[str, discord.Object | None]] = []
    if plan.register_global:
        scopes.append(("global", None))
    scopes.extend((str(g), discord.Object(id=g)) for g in plan.sync_guilds)

    results: dict[str, str] = {}
    for scope, guild in scopes:
        key = _HASH_KEY_PREFIX + scope
        digest = commands_hash(bot.tree, guild)
        if await asyncio.to_thread(_stored_hash_sync, bot.db_path, key) == digest:
            results[scope] = "unchanged"
            continue
        try:
            synced = await bot.tree.sync(guild=guild)
        # Not just HTTPException: a dropped connection mid-sync is an OSError,
        # and the promise here is that one bad scope never stops the bot
        # starting. CancelledError still propagates (it's a BaseException).
        except Exception:
            logger.exception("command sync failed", extra={"scope": scope})
            results[scope] = "failed"
            continue
        await asyncio.to_thread(_store_hash_sync, bot.db_path, key, digest)
        results[scope] = "synced"
        logger.info("synced %d commands", len(synced), extra={"scope": scope})
    return results


def lounge_guild_ids_sync(db_path: str | Path) -> list[int]:
    """Guilds with a lounge row where the daily quote is on: the guilds that get `/lounge`."""
    with closing(connect(db_path)) as conn:
        return [lounge.guild_id for lounge in repo.list_lounges(conn) if lounge.quote_enabled]


async def setup_commands(bot: NewsBot) -> dict[str, str]:
    """Register the v3 command set on `bot.tree` and sync what changed. `setup_hook` calls this."""
    lounge_ids = await asyncio.to_thread(lounge_guild_ids_sync, bot.db_path)
    plan = plan_scopes(bot.cfg, lounge_ids)
    register_commands(bot, plan)
    return await sync_commands(bot, plan)


__all__ = [
    "ScopePlan",
    "commands_hash",
    "lounge_guild_ids_sync",
    "plan_scopes",
    "register_commands",
    "setup_commands",
    "sync_commands",
]
