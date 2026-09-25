"""Tests for `DiscordCodeAlertPoster` (plan step 9) and the `_PING_EVERYONE` constant.

Same spirit as `test_discord_publisher.py`: tiny hand-written fakes for the
handful of discord.py calls this class actually makes, rather than mocking
the real gateway objects. The behavior worth pinning: a ping only ever
attaches `_PING_EVERYONE`, never anywhere else; a missing "Mention
@everyone, @here, and All Roles" permission doesn't stop the post, it just
adds an admin alert; and the error classification is the same one
`DiscordPublisher` uses (5xx/network -> `PublishError`, 4xx -> propagates).
"""

from __future__ import annotations

import aiohttp
import discord
import pytest

from newsbot.bot.client import _PING_EVERYONE, DiscordCodeAlertPoster
from newsbot.bot.format import RenderedAlert
from newsbot.pipeline.publisher import PublishError


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakePermissions:
    def __init__(self, *, mention_everyone: bool) -> None:
        self.mention_everyone = mention_everyone


class FakeMember:
    pass


class FakeGuild:
    def __init__(self, *, me: FakeMember | None) -> None:
        self.me = me


class FakeChannel:
    def __init__(self, *, guild: FakeGuild | None = None, can_mention: bool = True) -> None:
        self.guild = guild
        self._can_mention = can_mention
        self.sent: list[tuple[str, discord.AllowedMentions]] = []
        self.nonces: list[str | None] = []
        self.next_error: Exception | None = None
        self._next_id = 100

    def permissions_for(self, member: object) -> FakePermissions:
        return FakePermissions(mention_everyone=self._can_mention)

    async def send(
        self, content: str, *, allowed_mentions: discord.AllowedMentions, nonce: str | None = None
    ):
        if self.next_error is not None:
            raise self.next_error
        self._next_id += 1
        self.sent.append((content, allowed_mentions))
        self.nonces.append(nonce)
        return FakeMessage(self._next_id)


class FakeClient:
    def __init__(self, channel: FakeChannel | None) -> None:
        self._channel = channel
        self.alerts: list[str] = []

    def get_channel(self, channel_id: int):
        return self._channel

    async def fetch_channel(self, channel_id: int):
        if self._channel is None:
            raise discord.HTTPException(_fake_response(404), "not found")
        return self._channel

    async def alert(self, text: str) -> None:
        self.alerts.append(text)


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers = {}
        self.request_info = None


def _fake_response(status: int):
    return _FakeResponse(status)


def _alert(*, ping: bool, codes: list[str] | None = None) -> RenderedAlert:
    header = "@everyone " if ping else ""
    return RenderedAlert(
        content=f"{header}**New SHiFT code**",
        codes=codes or ["AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"],
        ping=ping,
    )


# --- _PING_EVERYONE itself ---


def test_ping_everyone_to_dict_is_exactly_everyone():
    assert _PING_EVERYONE.to_dict() == {"parse": ["everyone"]}


def test_ping_everyone_merged_with_none_is_still_everyone_only():
    merged = discord.AllowedMentions.none().merge(_PING_EVERYONE)
    assert merged.to_dict() == {"parse": ["everyone"]}


# --- ping mentions only when ping=True ---


async def test_post_with_ping_uses_ping_everyone_mentions():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=True)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    message_id = await poster.post(_alert(ping=True))

    assert message_id is not None
    assert channel.sent[0][1].to_dict() == {"parse": ["everyone"]}


async def test_literal_everyone_text_in_content_cannot_ping_when_ping_false():
    # The content string itself can contain the literal text "@everyone"
    # (e.g. baked into a collected URL's path by a hostile or just weird
    # source, since `render_code_alerts` never scrubs URLs the way `esc()`
    # scrubs headline text) -- Discord only turns that into a real mention
    # if `allowed_mentions` says so. With ping=False, this must go out
    # with AllowedMentions.none() regardless of what the text contains.
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=True)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)
    alert = RenderedAlert(
        content="**New SHiFT code**\n```\nAAAAA-AAAAA-AAAAA-AAAAA-AAAAA\n```\n"
        "Some Source · <https://example.com/@everyone/path>",
        codes=["AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"],
        ping=False,
    )

    await poster.post(alert)

    assert "@everyone" in channel.sent[0][0]  # the text is there...
    assert channel.sent[0][1].to_dict() == discord.AllowedMentions.none().to_dict()  # ...but inert


async def test_post_without_ping_uses_none_mentions():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=True)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    await poster.post(_alert(ping=False))

    assert channel.sent[0][1].to_dict() == discord.AllowedMentions.none().to_dict()
    assert client.alerts == []


# --- missing permission: still posts, admin alerted ---


async def test_missing_mention_permission_still_posts_and_alerts_admin():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=False)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    message_id = await poster.post(_alert(ping=True))

    assert message_id is not None
    assert len(channel.sent) == 1
    assert len(client.alerts) == 1
    assert "permission" in client.alerts[0].lower() or "mention" in client.alerts[0].lower()


async def test_present_permission_does_not_alert_admin():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=True)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    await poster.post(_alert(ping=True))

    assert client.alerts == []


async def test_unping_unaffected_by_missing_permission_no_alert():
    # No ping was ever going to happen, so a missing permission is moot --
    # only a ping that actually needed the permission is worth an alert.
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=False)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    await poster.post(_alert(ping=False))

    assert client.alerts == []


async def test_no_guild_cached_yet_reads_as_missing_permission():
    channel = FakeChannel(guild=None, can_mention=True)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    await poster.post(_alert(ping=True))

    assert len(client.alerts) == 1


# --- error classification, shared with DiscordPublisher ---


async def test_5xx_is_wrapped_as_publish_error():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()))
    channel.next_error = discord.HTTPException(_fake_response(503), "unavailable")
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    with pytest.raises(PublishError):
        await poster.post(_alert(ping=False))


async def test_4xx_propagates_unwrapped():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()))
    channel.next_error = discord.Forbidden(_fake_response(403), "missing access")
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    with pytest.raises(discord.Forbidden):
        await poster.post(_alert(ping=False))


async def test_transient_network_error_is_wrapped():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()))
    channel.next_error = aiohttp.ClientError("connection reset")
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    with pytest.raises(PublishError):
        await poster.post(_alert(ping=False))


async def test_channel_fetched_when_not_cached():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()))
    client = FakeClient(None)
    client._channel = None
    # get_channel() returns None; fetch_channel() is the fallback, mirroring
    # DiscordPublisher's own channel-resolution dance.
    poster = DiscordCodeAlertPoster(client, channel_id=1)
    client.fetch_channel = _make_fetch(channel)

    message_id = await poster.post(_alert(ping=False))
    assert message_id is not None


def _make_fetch(channel: FakeChannel):
    async def fetch_channel(channel_id: int):
        return channel

    return fetch_channel
