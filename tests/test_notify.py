"""Routing matrix for newsbot.guilds.notify (plan task 8).

A spy `send` stands in for Discord. The rule under test: owner alerts reach
only the owner channel, server notices reach only that server's admin
channel, and nothing ever raises into the caller.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from newsbot.alerts import send_to_channel
from newsbot.guilds.notify import Router
from newsbot.store import repo
from newsbot.store.db import connect, migrate

OWNER = 9000
ADMIN1 = 1001
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class Spy:
    def __init__(self, fail_for=()):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = set(fail_for)

    async def __call__(self, channel_id, text):
        if channel_id in self.fail_for:
            raise RuntimeError("discord is having a day")
        self.sent.append((channel_id, text))
        return True

    def to(self, channel_id):
        return [t for c, t in self.sent if c == channel_id]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "n.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.create_guild(conn, 1, admin_channel_id=ADMIN1)
        repo.create_guild(conn, 2)  # no admin channel
    return path


def _router(db_path, spy, owner=OWNER):
    return Router(db_path, spy, owner)


def _notices(db_path, guild_id):
    with closing(connect(db_path)) as conn:
        return repo.recent_notices(conn, guild_id)


async def test_owner_alert_goes_only_to_the_owner_channel(db_path):
    spy = Spy()
    await _router(db_path, spy).alert_owner("a source died")
    assert spy.sent == [(OWNER, "a source died")]
    assert _notices(db_path, 1) == [] and _notices(db_path, 2) == []


async def test_guild_notice_goes_to_its_admin_channel_and_is_recorded(db_path):
    spy = Spy()
    await _router(db_path, spy).notify_guild(1, "your digest failed")
    assert spy.sent == [(ADMIN1, "your digest failed")]
    assert [n.text for n in _notices(db_path, 1)] == ["your digest failed"]


async def test_guild_problem_never_reaches_the_owner_channel(db_path):
    spy = Spy()
    router = _router(db_path, spy)
    await router.notify_guild(1, "a")
    await router.notify_guild(2, "b")
    assert spy.to(OWNER) == []


async def test_no_admin_channel_means_a_notice_and_nothing_posted(db_path):
    spy = Spy()
    await _router(db_path, spy).notify_guild(2, "quiet problem")
    assert spy.sent == []
    assert [n.text for n in _notices(db_path, 2)] == ["quiet problem"]


async def test_unknown_guild_posts_nothing_and_does_not_raise(db_path):
    spy = Spy()
    await _router(db_path, spy).notify_guild(404, "who?")
    assert spy.sent == []


async def test_failing_admin_channel_records_the_notice_and_does_not_raise(db_path, caplog):
    spy = Spy(fail_for={ADMIN1})
    with caplog.at_level(logging.ERROR):
        await _router(db_path, spy).notify_guild(1, "still recorded")
    assert [n.text for n in _notices(db_path, 1)] == ["still recorded"]
    assert spy.sent == []


async def test_failing_owner_channel_does_not_raise(db_path):
    await _router(db_path, Spy(fail_for={OWNER})).alert_owner("x")


async def test_no_owner_channel_configured_is_a_quiet_no_op(db_path):
    spy = Spy()
    await _router(db_path, spy, owner=None).alert_owner("x")
    assert spy.sent == []


async def test_a_broken_database_does_not_raise(tmp_path):
    spy = Spy()
    await Router(tmp_path / "missing" / "n.db", spy, OWNER).notify_guild(1, "x")
    assert spy.sent == []


async def test_send_report_posts_to_the_given_channel_only(db_path):
    spy = Spy()
    await _router(db_path, spy).send_report(1, ADMIN1, "run report")
    assert spy.sent == [(ADMIN1, "run report")]
    assert _notices(db_path, 1) == []  # a report isn't a problem


async def test_send_report_failure_does_not_raise(db_path):
    await _router(db_path, Spy(fail_for={ADMIN1})).send_report(1, ADMIN1, "r")


async def test_mentions_are_defused_everywhere(db_path):
    spy = Spy()
    router = _router(db_path, spy)
    nasty = "@everyone @here <@123456789012345678> <@&456456456456456456> hi"
    await router.alert_owner(nasty)
    await router.notify_guild(1, nasty)
    await router.send_report(1, ADMIN1, nasty)
    for _, text in spy.sent:
        assert "@everyone" not in text and "@here" not in text
        assert "<@123456789012345678>" not in text and "<@&456456456456456456>" not in text
    assert "@everyone" not in _notices(db_path, 1)[0].text


async def test_long_text_is_capped_for_sends_and_notices(db_path):
    spy = Spy()
    router = _router(db_path, spy)
    await router.alert_owner("x" * 5000)
    await router.notify_guild(1, "y" * 5000)
    assert all(len(t) <= 2000 for _, t in spy.sent)
    assert len(spy.sent) == 2
    assert len(_notices(db_path, 1)[0].text) <= 2000


async def test_astral_text_is_capped_in_discord_units(db_path):
    from newsbot.bot.format import discord_len

    spy = Spy()
    await _router(db_path, spy).alert_owner("\U0001f600" * 1500)
    assert discord_len(spy.sent[0][1]) <= 2000


async def test_notices_keep_only_the_newest_twenty(db_path):
    spy = Spy()
    clock = [T0]

    def now():
        clock[0] += timedelta(seconds=1)
        return clock[0]

    router = Router(db_path, spy, OWNER, now=now)
    for i in range(25):
        await router.notify_guild(2, f"n{i}")
    texts = [n.text for n in _notices(db_path, 2)]
    assert len(texts) == 20 and texts[0] == "n24" and texts[-1] == "n5"


async def test_whitespace_only_notice_is_dropped_quietly(db_path):
    spy = Spy()
    await _router(db_path, spy).notify_guild(1, "  \x00 ")
    assert spy.sent == [] and _notices(db_path, 1) == []


async def test_guild_whose_admin_channel_is_the_owner_channel_gets_it_via_its_own_door(db_path):
    with closing(connect(db_path)) as conn:
        repo.update_guild_settings(conn, 2, admin_channel_id=OWNER)
    spy = Spy()
    await _router(db_path, spy).notify_guild(2, "home guild problem")
    assert spy.to(OWNER) == ["home guild problem"]


# --- the shared send_to_channel under both paths ---


class _Chan:
    def __init__(self):
        self.calls = []

    async def send(self, text, **kwargs):
        self.calls.append((text, kwargs))


class _Client:
    def __init__(self, chan):
        self.chan = chan

    def get_channel(self, cid):
        return self.chan

    async def fetch_channel(self, cid):
        return self.chan


async def test_send_to_channel_caps_and_disables_mentions():
    import discord

    chan = _Chan()
    assert await send_to_channel(_Client(chan), 1, "z" * 4000) is True
    text, kwargs = chan.calls[0]
    assert len(text) <= 2000
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_send_to_channel_returns_false_on_failure():
    class Bad:
        def get_channel(self, cid):
            raise RuntimeError("x")

    assert await send_to_channel(Bad(), 1, "hi") is False
