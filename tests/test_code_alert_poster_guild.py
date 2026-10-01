"""Per-server pings for `DiscordCodeAlertPoster` and `mentions_for` (plan task 5).

The shape to hold: `mentions_for` is the one place a ping choice becomes an
`AllowedMentions`; a role ping is literally `everyone=False` plus exactly that
role; and a missing-permission note goes to the injected notifier (that
server's admin channel), not to the owner's `client.alert`.
"""

from __future__ import annotations

import discord

from newsbot.bot.client import _PING_EVERYONE, DiscordCodeAlertPoster, mentions_for
from newsbot.bot.format import RenderedAlert

ROLE_ID = 1234567890123456789


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class FakePermissions:
    def __init__(self, mention_everyone):
        self.mention_everyone = mention_everyone


class FakeRole:
    def __init__(self, mentionable):
        self.mentionable = mentionable


class FakeGuild:
    def __init__(self, roles):
        self.me = object()
        self._roles = roles

    def get_role(self, role_id):
        return self._roles.get(role_id)


class FakeChannel:
    def __init__(self, *, roles=None, can_mention_everyone=False):
        self.guild = FakeGuild(roles or {})
        self._can = can_mention_everyone
        self.sent: list[tuple[str, discord.AllowedMentions]] = []

    def permissions_for(self, _member):
        return FakePermissions(self._can)

    async def send(self, content, *, allowed_mentions, nonce=None):
        self.sent.append((content, allowed_mentions))
        return FakeMessage(1)


class FakeClient:
    def __init__(self, channel):
        self._channel = channel
        self.owner_alerts: list[str] = []

    def get_channel(self, _channel_id):
        return self._channel

    async def alert(self, text):
        self.owner_alerts.append(text)


def _alert(ping, prefix=""):
    return RenderedAlert(content=f"{prefix}**New SHiFT code**", codes=["X"], ping=ping)


# --- mentions_for ---


def test_everyone_is_the_one_constant():
    assert mentions_for("everyone") is _PING_EVERYONE


def test_role_ping_is_literal_everyone_false_and_only_that_role():
    mentions = mentions_for(str(ROLE_ID))
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.replied_user is False
    assert [r.id for r in mentions.roles] == [ROLE_ID]
    assert mentions.to_dict() == {"parse": [], "roles": [ROLE_ID]}


def test_none_and_garbage_ping_nobody():
    for value in ("none", None, "", "0", "-5", "<@&1>", "everyone ", "12x"):
        assert mentions_for(value).to_dict() == {"parse": []}, value


# --- the poster ---


async def test_role_choice_sends_the_role_mentions_not_everyone():
    channel = FakeChannel(roles={ROLE_ID: FakeRole(True)})
    poster = DiscordCodeAlertPoster(FakeClient(channel), 1, str(ROLE_ID))

    await poster.post(_alert(True, f"<@&{ROLE_ID}> "))

    assert channel.sent[0][1].to_dict() == {"parse": [], "roles": [ROLE_ID]}


async def test_none_choice_never_pings_even_if_the_alert_says_ping():
    channel = FakeChannel(can_mention_everyone=True)
    poster = DiscordCodeAlertPoster(FakeClient(channel), 1, "none")

    await poster.post(_alert(True))

    assert channel.sent[0][1].to_dict() == {"parse": []}


async def test_unpinged_alert_sends_no_mentions_whatever_the_choice():
    channel = FakeChannel(roles={ROLE_ID: FakeRole(True)})
    poster = DiscordCodeAlertPoster(FakeClient(channel), 1, str(ROLE_ID))

    await poster.post(_alert(False))

    assert channel.sent[0][1].to_dict() == {"parse": []}


async def test_unmentionable_role_posts_anyway_and_notifies_that_guild_once():
    channel = FakeChannel(roles={ROLE_ID: FakeRole(False)})
    client = FakeClient(channel)
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    poster = DiscordCodeAlertPoster(client, 1, str(ROLE_ID), notify)
    poster.begin_batch()
    await poster.post(_alert(True))
    await poster.post(_alert(True))

    assert len(channel.sent) == 2
    assert len(notes) == 1 and "role" in notes[0]
    assert client.owner_alerts == []


async def test_mentionable_role_needs_no_permission_and_sends_no_note():
    channel = FakeChannel(roles={ROLE_ID: FakeRole(True)})
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, 1, str(ROLE_ID), notify=None)

    await poster.post(_alert(True))

    assert client.owner_alerts == []


async def test_missing_role_is_treated_as_cant_ping():
    channel = FakeChannel(roles={})
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    await DiscordCodeAlertPoster(FakeClient(channel), 1, str(ROLE_ID), notify).post(_alert(True))

    assert len(notes) == 1


async def test_everyone_without_permission_notifies_the_injected_notifier_not_the_owner():
    channel = FakeChannel(can_mention_everyone=False)
    client = FakeClient(channel)
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    await DiscordCodeAlertPoster(client, 1, "everyone", notify).post(_alert(True))

    assert channel.sent[0][1].to_dict() == {"parse": ["everyone"]}
    assert len(notes) == 1 and "@everyone" in notes[0]
    assert client.owner_alerts == []


async def test_v2_defaults_still_alert_the_owner_path():
    channel = FakeChannel(can_mention_everyone=False)
    client = FakeClient(channel)

    await DiscordCodeAlertPoster(client, 1).post(_alert(True))

    assert len(client.owner_alerts) == 1


# --- a channel from another server is refused, like the router refuses it ---


async def test_a_channel_in_another_server_is_never_posted_to_and_is_final(caplog):
    import pytest

    from newsbot.bot.client import ChannelNotInGuildError

    channel = FakeChannel(can_mention_everyone=True)
    channel.guild.id = 8
    poster = DiscordCodeAlertPoster(FakeClient(channel), 5, "everyone", guild_id=7)

    with caplog.at_level("WARNING", logger="newsbot.bot.client"):
        with pytest.raises(ChannelNotInGuildError):
            await poster.post(_alert(True))

    assert channel.sent == []
    assert "isn't in guild 7 (resolved to guild 8)" in caplog.text


async def test_the_poster_posts_when_the_channel_is_in_its_guild():
    channel = FakeChannel(can_mention_everyone=True)
    channel.guild.id = 7
    poster = DiscordCodeAlertPoster(FakeClient(channel), 5, "everyone", guild_id=7)

    assert await poster.post(_alert(True)) == 1


async def test_a_foreign_channel_is_a_permanent_failure_for_the_retry_loop():
    from newsbot.shift.sweep import post_alert_with_retry

    channel = FakeChannel()
    channel.guild.id = 8
    poster = DiscordCodeAlertPoster(FakeClient(channel), 5, "none", guild_id=7)
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    message_id, error = await post_alert_with_retry(poster, sleep, _alert(False))

    assert message_id is None and error is not None
    assert sleeps == [] and channel.sent == []  # no backoff: waiting won't move a channel
