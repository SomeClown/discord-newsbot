"""Guild-scoped commands come back when a server re-joins (and the hash goes when it leaves).

Discord drops a guild's commands when the bot is removed; the stored hash still matches what
we'd send, so only a join-time sync (or clearing the hash) puts them back. Same harness as
`test_command_registration_v3.py`: a real `NewsBot`, `tree.sync` swapped for a recorder.
"""

from __future__ import annotations

import logging
from contextlib import closing
from types import SimpleNamespace

import discord
from test_command_registration_v3 import DEV1, DEV2, L1, make_bot
from v3_fakes import GUILD_A, HOME, make_guild

from newsbot.bot.registration import setup_commands
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import LoungeSettings


def _hash(db_path, scope):
    with closing(connect(db_path)) as conn:
        return repo.app_state_get(conn, f"commands:{scope}")


def _dev_cfg(v3_cfg):
    return v3_cfg.model_copy(update={"command_guild_ids": [DEV1, DEV2], "home_guild_id": HOME})


async def test_a_join_to_a_dev_guild_resyncs_even_though_the_hash_matches(v3_cfg, v3_db):
    bot = make_bot(_dev_cfg(v3_cfg), v3_db)
    await setup_commands(bot)
    assert (await setup_commands(make_bot(_dev_cfg(v3_cfg), v3_db))) == {
        str(DEV1): "unchanged",
        str(DEV2): "unchanged",
    }
    bot.synced.clear()

    await bot.on_guild_join(SimpleNamespace(id=DEV1))

    assert bot.synced == [DEV1]  # one sync, only that guild
    assert _hash(v3_db, DEV1) is not None


async def test_a_join_to_the_home_guild_resyncs(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    await setup_commands(bot)
    bot.synced.clear()
    await bot.on_guild_join(SimpleNamespace(id=HOME))
    assert bot.synced == [HOME]


async def test_a_join_to_a_lounge_guild_resyncs(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    make_guild(v3_db, L1)
    with closing(connect(v3_db)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(L1, 5, True, "hi", True, "08:00", [], None))
    await setup_commands(bot)
    bot.synced.clear()
    await bot.on_guild_join(SimpleNamespace(id=L1))
    assert bot.synced == [L1]


async def test_a_join_to_the_imported_guild_resyncs_its_one_time_clear(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    make_guild(v3_db, GUILD_A)
    with closing(connect(v3_db)) as conn, conn:
        conn.execute("UPDATE guilds SET imported_at = '2026-10-01T00:00:00+00:00'")
    await setup_commands(bot)
    bot.synced.clear()
    await bot.on_guild_join(SimpleNamespace(id=GUILD_A))
    assert bot.synced == [GUILD_A]


async def test_a_join_to_a_guild_that_is_not_a_scope_does_nothing(v3_cfg, v3_db):
    bot = make_bot(_dev_cfg(v3_cfg), v3_db)
    await setup_commands(bot)
    bot.synced.clear()
    await bot.on_guild_join(SimpleNamespace(id=GUILD_A))
    assert bot.synced == []
    assert _hash(v3_db, GUILD_A) is None


async def test_removal_clears_that_guilds_hash_and_a_later_start_resyncs_it(v3_cfg, v3_db):
    cfg = _dev_cfg(v3_cfg)
    bot = make_bot(cfg, v3_db)
    await setup_commands(bot)
    assert _hash(v3_db, DEV1) is not None

    await bot.on_guild_remove(SimpleNamespace(id=DEV1))

    assert _hash(v3_db, DEV1) is None
    assert _hash(v3_db, DEV2) is not None  # only that guild's
    restart = make_bot(cfg, v3_db)
    assert (await setup_commands(restart))[str(DEV1)] == "synced"
    assert restart.synced == [DEV1]


async def test_removing_a_guild_with_no_hash_is_fine(v3_cfg, v3_db):
    bot = make_bot(v3_cfg, v3_db)
    await bot.on_guild_remove(SimpleNamespace(id=GUILD_A))


async def test_a_failed_resync_is_logged_and_swallowed_and_stores_no_hash(v3_cfg, v3_db, caplog):
    cfg = _dev_cfg(v3_cfg)
    bot = make_bot(cfg, v3_db)
    await setup_commands(bot)
    with closing(connect(v3_db)) as conn:
        repo.app_state_delete(conn, f"commands:{DEV1}")

    async def boom(*, guild=None):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="nope"), "boom")

    bot.tree.sync = boom
    with caplog.at_level(logging.ERROR):
        await bot.on_guild_join(SimpleNamespace(id=DEV1))  # must not raise

    assert any("re-sync on join failed" in r.getMessage() for r in caplog.records)
    assert _hash(v3_db, DEV1) is None  # so the next start retries


async def test_a_join_that_hangs_on_discord_gives_up(v3_cfg, v3_db, monkeypatch):
    import asyncio

    from newsbot.bot import registration

    monkeypatch.setattr(registration, "_RESYNC_TIMEOUT_S", 0.01)
    bot = make_bot(_dev_cfg(v3_cfg), v3_db)

    async def hang(*, guild=None):
        await asyncio.sleep(3600)

    bot.tree.sync = hang
    assert await registration.resync_guild_commands(bot, DEV1) == "failed"
