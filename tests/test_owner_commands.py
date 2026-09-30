"""`/owner servers` (plan task 9, D3): home guild only, owner only, numbers only."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from v3_fakes import GUILD_A, GUILD_B, HOME, OWNER_ID, FakeInteraction, command, make_guild

from newsbot.bot.owner_commands import make_owner_group, render_owner_servers, summarize_digests
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import DueCandidate

DENIED = "You don't have permission to run this."


@pytest.fixture
def owner(v3_cfg, v3_db):
    asked = []

    async def is_owner(user):
        asked.append(user.id)
        return user.id == OWNER_ID

    bot = SimpleNamespace(db_path=v3_db, is_owner=is_owner)
    group = make_owner_group(v3_cfg, bot)
    return SimpleNamespace(call=command(group, "servers").callback, asked=asked, group=group)


def test_the_group_shape(owner):
    assert owner.group.name == "owner"
    assert owner.group.guild_only is True
    assert owner.group.default_permissions.administrator is True
    assert [c.name for c in owner.group.commands] == ["servers"]


async def test_the_owner_in_the_home_guild_gets_the_numbers(owner, v3_db):
    make_guild(v3_db, GUILD_A, games=[("rust", 1)])
    make_guild(v3_db, GUILD_B, tier="comped", set_up=False)
    with closing(connect(v3_db)) as conn, conn:
        conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "input_tokens, output_tokens, created_at) VALUES "
            "('rust', '2026-09-30', 'ok', 'a', 'b', 1000000, 0, ?)",
            (datetime.now(UTC).isoformat(),),
        )
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await owner.call(interaction)
    text = interaction.text
    assert "Servers: 2 (1 set up)" in text
    assert "Tiers: 1 free, 1 comped" in text
    assert "Today's digests:" in text
    assert "Summary spend, " in text and "about $" in text and "about $0.00" not in text
    assert interaction.sent[-1]["ephemeral"] is True
    assert interaction.sent[-1]["allowed_mentions"].everyone is False


async def test_a_non_owner_in_the_home_guild_is_refused(owner):
    interaction = FakeInteraction(guild_id=HOME, user_id=7)
    await owner.call(interaction)
    assert interaction.text == DENIED
    assert owner.asked == [7]


async def test_even_the_owner_is_refused_outside_the_home_guild(owner):
    interaction = FakeInteraction(guild_id=GUILD_A, user_id=OWNER_ID)
    await owner.call(interaction)
    assert interaction.text == DENIED
    assert owner.asked == []  # never even asked


async def test_no_guild_or_no_home_guild_configured(v3_cfg, v3_db):
    async def is_owner(user):
        return True

    cfg = v3_cfg.model_copy(update={"home_guild_id": None})
    group = make_owner_group(cfg, SimpleNamespace(db_path=v3_db, is_owner=is_owner))
    for guild_id in (None, HOME):
        interaction = FakeInteraction(guild_id=guild_id, user_id=OWNER_ID)
        await command(group, "servers").callback(interaction)
        assert interaction.text == DENIED


async def test_an_admin_is_not_the_owner(owner):
    interaction = FakeInteraction(guild_id=HOME, user_id=7)  # has Manage Server, isn't the owner
    await owner.call(interaction)
    assert interaction.text == DENIED


async def test_the_reply_names_no_server_or_channel(owner, v3_db):
    make_guild(v3_db, GUILD_A, games=[("rust", 4242)], admin_channel_id=9191)
    with closing(connect(v3_db)) as conn:
        repo.add_notice(conn, GUILD_A, "secret notice text")
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await owner.call(interaction)
    for needle in (str(GUILD_A), "4242", "9191", "secret", "Rust"):
        assert needle not in interaction.text


# --- pure pieces ---

NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)  # 10:00 in Los Angeles


def cand(guild_id, *, run_date=None, status=None, tz="America/Los_Angeles", at="09:00"):
    return DueCandidate(guild_id, at, tz, "free", run_date, status)


def test_summarize_digests_buckets():
    today = date(2026, 9, 30)
    counts = summarize_digests(
        [
            cand(1, run_date=today, status="ok"),
            cand(2, run_date=today, status="ok"),
            cand(3, run_date=today, status="partial"),
            cand(4, run_date=today, status="failed"),
            cand(5, run_date=today, status="pending"),
            cand(6, run_date=date(2026, 9, 29), status="ok"),  # yesterday's: nothing today yet
            cand(7),  # never had one, 09:00 already passed
            cand(8, at="23:00"),  # not yet due
            cand(9, tz="Mars/Olympus"),
            cand(10, at="99:99"),
        ],
        NOW,
    )
    assert dict(counts) == {
        "ok": 2,
        "partial": 1,
        "failed": 1,
        "pending": 1,
        "due, none yet": 2,
        "not yet due": 1,
        "bad zone": 2,
    }


def test_summarize_digests_uses_each_servers_own_local_day():
    # 17:00 UTC is already tomorrow in Auckland: yesterday's row isn't today's there.
    row = date(2026, 9, 30)
    counts = summarize_digests([cand(1, run_date=row, status="ok", tz="Pacific/Auckland")], NOW)
    assert "ok" not in counts


def test_render_owner_servers_is_plain_lines():
    counts = {
        "servers": 3,
        "set_up": 2,
        "free": 2,
        "comped": 1,
        "shift_enabled": 1,
        "permission_problems": 0,
    }
    from collections import Counter

    text = render_owner_servers(counts, Counter(ok=2), 1.5, "2026-09")
    assert text.splitlines()[0] == "Servers: 3 (2 set up)"
    assert "2 ok, 0 partial" in text and "about $1.50" in text


# --- NewsBot.is_owner ---


def _bot(v3_cfg, v3_db, app_info):
    from pydantic import SecretStr

    from newsbot.bot.client import NewsBot
    from newsbot.config import Secrets

    secrets = Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("k"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    bot = NewsBot(v3_cfg, secrets, v3_db)
    calls = []

    async def application_info():
        calls.append(1)
        if isinstance(app_info, Exception):
            raise app_info
        return app_info

    bot.application_info = application_info
    return bot, calls


async def test_is_owner_uses_the_application_owner_and_caches_it(v3_cfg, v3_db):
    app = SimpleNamespace(team=None, owner=SimpleNamespace(id=OWNER_ID))
    bot, calls = _bot(v3_cfg, v3_db, app)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is True
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID + 1)) is False
    assert len(calls) == 1


async def test_is_owner_for_a_team_app_means_the_teams_owner(v3_cfg, v3_db):
    app = SimpleNamespace(team=SimpleNamespace(owner_id=OWNER_ID), owner=SimpleNamespace(id=1))
    bot, _ = _bot(v3_cfg, v3_db, app)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is True
    assert await bot.is_owner(SimpleNamespace(id=1)) is False


async def test_is_owner_says_no_when_discord_cant_be_asked(v3_cfg, v3_db):
    import discord

    error = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    bot, calls = _bot(v3_cfg, v3_db, error)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is False
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is False
    assert len(calls) == 2  # a failed lookup isn't cached
