"""The owner's alert channel and the friend's admin channel, as two keys.

One config key used to be both: the bot-wide owner channel (which has to be in the home
server) and what the v2 import copies in as the friend's own admin channel (which has to be
in the friend's server). Both can't be true of one number, so the owner channel got its own
key, `owner_channel_id`, and the old one stays as a fallback for single-server self-hosts and
v2-shaped files. These pin the split: which key wins, what the startup check looks at and
names, what the import still copies, and how the cutover morning looks end to end.
"""

from __future__ import annotations

import logging
from contextlib import closing
from types import SimpleNamespace

import pytest
from cutover_world import DUE, FRIEND_ADMIN_CH, OWNER_CH, OWNER_GUILD, PRODLIKE
from test_client_wiring_v3 import HOME, OWNER_CHANNEL, _secrets, _setup, _stopped
from test_cutover_owner_report_adversarial import clock as frozen_clock  # noqa: F401

from newsbot.bot.client import NewsBot
from newsbot.config import load_config
from newsbot.store import repo
from newsbot.store.db import connect

# --- the config keys ---


def _cfg(tmp_path, tail: str):
    path = tmp_path / "config.yaml"
    path.write_text(
        "catalog:\n"
        "  - key: palworld\n"
        '    name: "Palworld"\n'
        "    sources:\n"
        '      - {type: steam_news, name: "Palworld Steam", app_id: 1623730, trust: official}\n'
        + tail
    )
    return load_config(path)


def test_owner_channel_wins_when_both_are_set(tmp_path):
    cfg = _cfg(tmp_path, "owner_channel_id: 5\nadmin_channel_id: 6\n")
    assert cfg.effective_owner_channel_id == 5
    assert cfg.owner_channel_key == "owner_channel_id"


def test_admin_channel_is_the_fallback_when_owner_channel_is_unset(tmp_path):
    cfg = _cfg(tmp_path, "admin_channel_id: 6\n")
    assert cfg.effective_owner_channel_id == 6
    assert cfg.owner_channel_key == "admin_channel_id"


def test_neither_key_means_no_owner_channel(tmp_path):
    assert _cfg(tmp_path, "").effective_owner_channel_id is None


@pytest.mark.parametrize("value", [0, -5])
def test_a_placeholder_owner_channel_is_none(tmp_path, value):
    cfg = _cfg(tmp_path, f"owner_channel_id: {value}\n")
    assert cfg.owner_channel_id is None
    assert cfg.effective_owner_channel_id is None


def test_a_placeholder_owner_channel_still_lets_admin_channel_fall_back(tmp_path):
    cfg = _cfg(tmp_path, "owner_channel_id: 000000000000000000\nadmin_channel_id: 6\n")
    assert cfg.effective_owner_channel_id == 6


def test_a_v2_prod_config_with_owner_channel_added_keeps_the_friends_channel(tmp_path):
    text = PRODLIKE.read_text() + f"home_guild_id: {OWNER_GUILD}\nowner_channel_id: {OWNER_CH}\n"
    path = tmp_path / "config.yaml"
    path.write_text(text)
    cfg = load_config(path)
    assert cfg.effective_owner_channel_id == OWNER_CH
    assert cfg.home_guild_id == OWNER_GUILD
    assert cfg.legacy is not None and cfg.legacy.admin_channel_id == FRIEND_ADMIN_CH


def test_the_v2_prod_config_unchanged_still_loads_with_admin_as_the_owner_fallback():
    cfg = load_config(PRODLIKE)
    assert cfg.owner_channel_id is None
    assert cfg.effective_owner_channel_id == FRIEND_ADMIN_CH


# --- the startup check looks at the effective channel ---


@pytest.fixture
async def bot(v3_cfg, v3_db):
    b = NewsBot(v3_cfg, _secrets(), v3_db)
    await _setup(b)
    yield b
    await _stopped(b)


def _channel_in(guild_id: int):
    return SimpleNamespace(id=OWNER_CHANNEL, guild=SimpleNamespace(id=guild_id))


async def _errors(bot, monkeypatch, caplog, *, update: dict, channel) -> list[str]:
    bot.cfg = bot.cfg.model_copy(update={"home_guild_id": HOME, **update})
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel)
    with closing(connect(bot.db_path)) as conn:
        for gid in (31, 32, 33):
            repo.create_guild(conn, gid, set_up=False)
    caplog.set_level(logging.ERROR)
    await bot._check_owner_channel()
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


async def test_the_router_is_built_with_the_effective_owner_channel(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"owner_channel_id": 5, "admin_channel_id": 6})
    b = NewsBot(cfg, _secrets(), v3_db)
    assert b.router._owner_channel_id == 5
    cfg = v3_cfg.model_copy(update={"owner_channel_id": None, "admin_channel_id": 6})
    b = NewsBot(cfg, _secrets(), v3_db)
    assert b.router._owner_channel_id == 6


async def test_a_good_owner_channel_with_a_different_admin_channel_says_nothing(
    bot, monkeypatch, caplog
):
    # The friend's admin channel is somewhere else entirely; the check must not care.
    errors = await _errors(
        bot,
        monkeypatch,
        caplog,
        update={"owner_channel_id": OWNER_CHANNEL, "admin_channel_id": 99},
        channel=_channel_in(HOME),
    )
    assert errors == []


async def test_the_check_asks_for_the_owner_channel_not_the_admin_one(bot, monkeypatch, caplog):
    asked = []

    def get_channel(cid):
        asked.append(cid)
        return _channel_in(HOME)

    bot.cfg = bot.cfg.model_copy(
        update={"home_guild_id": HOME, "owner_channel_id": 5, "admin_channel_id": 6}
    )
    monkeypatch.setattr(bot, "get_channel", get_channel)
    await bot._check_owner_channel()
    assert asked == [5]


async def test_a_bad_owner_channel_names_owner_channel_id(bot, monkeypatch, caplog):
    errors = await _errors(
        bot,
        monkeypatch,
        caplog,
        update={"owner_channel_id": OWNER_CHANNEL, "admin_channel_id": None},
        channel=_channel_in(HOME + 1),
    )
    assert len(errors) == 1
    assert "owner_channel_id" in errors[0] and "admin_channel_id" not in errors[0]
    assert "Fix: set owner_channel_id" in errors[0]


async def test_a_bad_fallback_channel_names_admin_channel_id_and_tells_you_the_new_key(
    bot, monkeypatch, caplog
):
    errors = await _errors(
        bot,
        monkeypatch,
        caplog,
        update={"owner_channel_id": None, "admin_channel_id": OWNER_CHANNEL},
        channel=_channel_in(HOME + 1),
    )
    assert len(errors) == 1
    assert f"admin_channel_id {OWNER_CHANNEL} isn't in home_guild_id" in errors[0]
    assert "Fix: set owner_channel_id" in errors[0]


# --- the cutover morning ---


@pytest.mark.usefixtures("frozen_clock")
async def test_cutover_with_an_owner_channel_splits_the_two_audiences(make_world):
    world = await make_world(now=DUE, owner_channel=True)
    await world.ready()
    await world.tick(DUE)
    await world.bot._owner_report_job()

    # The friend's row kept their own admin channel, and their run report landed there.
    assert world.rows("SELECT admin_channel_id FROM guilds") == [(FRIEND_ADMIN_CH,)]
    friend_side = [m.content for m in world.sent(FRIEND_ADMIN_CH) if m.content]
    assert friend_side, "the friend's run report should land in their own admin channel"
    assert not any("Imported the v2 setup" in t or "newsbot daily" in t for t in friend_side)

    # The import notice and the owner report went to the owner's channel in the home server.
    owner_side = [m.content for m in world.sent(OWNER_CH) if m.content]
    assert any("Imported the v2 setup" in t for t in owner_side)
    assert any(t.startswith("newsbot daily") for t in owner_side)


async def test_cutover_without_an_owner_channel_falls_back_to_the_admin_channel(make_world):
    world = await make_world(now=DUE)
    await world.ready()

    assert world.cfg.owner_channel_id is None
    told = [m.content for m in world.sent(FRIEND_ADMIN_CH) if m.content]
    assert any("Imported the v2 setup" in t for t in told)
