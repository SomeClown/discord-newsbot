"""Adversarial tests for the startup check of the owner's alert channel and home server.

Get `admin_channel_id` or `home_guild_id` wrong and every bot-wide alert goes nowhere, which
looks exactly like a quiet week. So at startup the bot complains, loudly and once, and then
carries on, because the friend's digests are working and shouldn't pay for the owner's typo.
The existing tests pin each misconfiguration's message. These ask the other questions: how
often does it complain (once per process, however many times Discord reconnects), what if
both settings are wrong at once, and what if the check itself blows up. Whatever happens, the
bot has to finish starting.

The rig is the client-wiring file's: a real `NewsBot` with its gateway never opened.
"""

from __future__ import annotations

import logging
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_client_wiring_v3 import HOME, OWNER_CHANNEL, _secrets, _setup, _stopped

from newsbot.bot.client import NewsBot
from newsbot.store import repo
from newsbot.store.db import connect


@pytest.fixture
async def bot(v3_cfg, v3_db):
    b = NewsBot(v3_cfg, _secrets(), v3_db)
    await _setup(b)
    yield b
    await _stopped(b)


def several_servers(bot: NewsBot, count: int = 3) -> None:
    with closing(connect(bot.db_path)) as conn:
        for gid in range(31, 31 + count):
            repo.create_guild(conn, gid, set_up=False)


def configured(bot: NewsBot, monkeypatch, *, home, channel_id, channel):
    bot.cfg = bot.cfg.model_copy(update={"home_guild_id": home, "admin_channel_id": channel_id})
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel)


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


async def test_a_misconfiguration_is_logged_once_however_many_times_the_gateway_reconnects(
    bot, monkeypatch, caplog, capsys
):
    several_servers(bot)
    configured(
        bot,
        monkeypatch,
        home=HOME,
        channel_id=OWNER_CHANNEL,
        channel=SimpleNamespace(id=OWNER_CHANNEL, guild=SimpleNamespace(id=HOME + 1)),
    )
    caplog.set_level(logging.ERROR)
    for _ in range(4):  # discord.py fires on_ready on every reconnect
        await bot.on_ready()
    ours = [e for e in errors(caplog) if "owner alerts won't arrive" in e]
    assert len(ours) == 1
    assert capsys.readouterr().err.count("owner alerts won't arrive") == 1


async def test_two_things_wrong_at_once_are_two_lines_each_with_its_own_fix(
    bot, monkeypatch, caplog, capsys
):
    several_servers(bot)
    configured(bot, monkeypatch, home=None, channel_id=None, channel=None)
    caplog.set_level(logging.ERROR)
    await bot._check_owner_channel()
    lines = errors(caplog)
    assert len(lines) == 2
    assert all(
        "Fix:" in line and line.startswith("newsbot: owner alerts won't arrive") for line in lines
    )
    assert capsys.readouterr().err.strip().splitlines() == lines


async def test_a_home_server_that_is_set_and_an_admin_channel_that_is_unseen_is_one_line(
    bot, monkeypatch, caplog
):
    several_servers(bot)
    configured(bot, monkeypatch, home=HOME, channel_id=OWNER_CHANNEL, channel=None)
    caplog.set_level(logging.ERROR)
    await bot._check_owner_channel()
    assert len(errors(caplog)) == 1


async def test_a_channel_with_no_guild_at_all_is_reported_as_not_in_the_home_server(
    bot, monkeypatch, caplog
):
    """A DM-style channel object (no `.guild`) can't be in the home server."""
    several_servers(bot)
    configured(
        bot,
        monkeypatch,
        home=HOME,
        channel_id=OWNER_CHANNEL,
        channel=SimpleNamespace(id=OWNER_CHANNEL),
    )
    caplog.set_level(logging.ERROR)
    await bot._check_owner_channel()
    assert len(errors(caplog)) == 1 and "isn't in home_guild_id" in errors(caplog)[0]


async def test_a_visible_admin_channel_with_no_home_setting_in_a_lone_server_says_nothing(
    bot, monkeypatch, caplog
):
    several_servers(bot, 1)
    configured(
        bot,
        monkeypatch,
        home=None,
        channel_id=OWNER_CHANNEL,
        channel=SimpleNamespace(id=OWNER_CHANNEL, guild=SimpleNamespace(id=HOME)),
    )
    caplog.set_level(logging.ERROR)
    await bot._check_owner_channel()
    assert errors(caplog) == []


async def test_a_check_that_blows_up_is_an_owner_alert_and_startup_still_finishes(
    bot, monkeypatch, caplog
):
    alerts: list[str] = []

    async def alert(text):
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", alert)

    def broken(_cid):
        raise RuntimeError("the cache fell over")

    bot.cfg = bot.cfg.model_copy(update={"home_guild_id": HOME, "admin_channel_id": OWNER_CHANNEL})
    monkeypatch.setattr(bot, "get_channel", broken)
    await bot.on_ready()
    assert bot._digests_enabled is True
    assert bot.scheduler.get_job("collection") is not None  # the later steps all ran
    assert len(alerts) == 1 and "owner channel check" in alerts[0]


async def test_a_database_that_cannot_count_servers_does_not_stop_startup(bot, monkeypatch):
    alerts: list[str] = []

    async def alert(text):
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", alert)

    def no_count():
        raise OSError("disk went away")

    monkeypatch.setattr(bot, "_server_count_sync", no_count)
    await bot.on_ready()
    assert bot._digests_enabled is True
    assert [a for a in alerts if "owner channel check" in a] != []


async def test_the_check_never_sends_anything_to_any_server_or_channel(bot, monkeypatch):
    several_servers(bot)
    configured(bot, monkeypatch, home=None, channel_id=OWNER_CHANNEL, channel=None)
    sent: list[object] = []

    async def alert(text):
        sent.append(text)

    monkeypatch.setattr(bot, "alert", alert)
    await bot._check_owner_channel()
    assert sent == []  # it can only talk to the log and stderr: the channel is what's broken


# --- the collection job that on_ready starts ---


async def test_the_collection_job_is_scheduled_once_however_many_times_on_ready_fires(bot):
    assert bot.scheduler.get_job("collection") is None  # not before on_ready
    for _ in range(3):
        await bot.on_ready()
    assert [job.id for job in bot.scheduler.get_jobs()].count("collection") == 1


async def test_a_collection_job_that_cannot_be_scheduled_is_reported_and_digests_stay_on(
    bot, monkeypatch
):
    alerts: list[str] = []

    async def alert(text):
        alerts.append(text)

    async def boom():
        raise RuntimeError("scheduler said no")

    monkeypatch.setattr(bot, "alert", alert)
    monkeypatch.setattr(bot, "_start_collection_job", boom)
    await bot.on_ready()
    assert bot._digests_enabled is True
    assert any("collection job" in a for a in alerts)


async def test_asking_for_the_collection_job_before_setup_is_a_loud_error_not_a_silent_skip(
    v3_cfg, v3_db
):
    unready = NewsBot(v3_cfg, _secrets(), v3_db)
    with pytest.raises(RuntimeError, match="before setup_hook"):
        await unready._start_collection_job()
