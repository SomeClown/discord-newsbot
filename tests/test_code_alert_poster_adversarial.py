"""Adversarial tests for `DiscordCodeAlertPoster`, beyond `test_code_alert_poster.py`.

The angle here (test-engineer brief, 2026-09-25): a missing "Mention
@everyone" permission alerts admin once *per call* (not once ever, and not
zero times on a second offense), channel-not-found/fetch failure goes
through the shared `_classify_send_error` the same as a plain send failure,
and a whole `render_code_alerts` -> `DiscordCodeAlertPoster.post` pipeline
never lets a continuation message carry a live ping.
"""

from __future__ import annotations

import discord
import pytest

from newsbot.bot.client import DiscordCodeAlertPoster
from newsbot.bot.format import RenderedAlert, render_code_alerts
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.decide import CodeCandidate


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
        self._next_id = 100

    def permissions_for(self, member: object) -> FakePermissions:
        return FakePermissions(mention_everyone=self._can_mention)

    async def send(self, content: str, *, allowed_mentions: discord.AllowedMentions):
        self._next_id += 1
        self.sent.append((content, allowed_mentions))
        return FakeMessage(self._next_id)


class FakeClient:
    def __init__(self, channel: FakeChannel | None) -> None:
        self._channel = channel
        self.alerts: list[str] = []
        self.fetch_error: Exception | None = None

    def get_channel(self, channel_id: int):
        return self._channel

    async def fetch_channel(self, channel_id: int):
        if self.fetch_error is not None:
            raise self.fetch_error
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


# --- missing permission: alerts once per call, not once ever ---


async def test_missing_permission_alerts_admin_once_per_call_not_once_ever():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=False)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    await poster.post(_alert(ping=True))
    await poster.post(_alert(ping=True))

    # Two separate pings, both missing the permission -- two separate
    # admin alerts, not a single one-shot warning that stops repeating.
    assert len(client.alerts) == 2
    assert len(channel.sent) == 2
    for _content, mentions in channel.sent:
        assert mentions.to_dict() == {"parse": ["everyone"]}


async def test_missing_permission_then_permission_granted_only_alerts_for_the_first_call():
    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=False)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    await poster.post(_alert(ping=True))
    channel._can_mention = True
    await poster.post(_alert(ping=True))

    assert len(client.alerts) == 1
    assert len(channel.sent) == 2


# --- channel not found / fetch failure ---


async def test_channel_fetch_404_propagates_unwrapped():
    # 404 ("channel not found") is a 4xx -- final, not worth retrying --
    # so it propagates unwrapped, same as any other 4xx classification.
    client = FakeClient(None)
    poster = DiscordCodeAlertPoster(client, channel_id=999)

    with pytest.raises(discord.HTTPException) as exc_info:
        await poster.post(_alert(ping=False))
    assert exc_info.value.status == 404


async def test_channel_fetch_5xx_wraps_as_publish_error():
    client = FakeClient(None)
    client.fetch_error = discord.HTTPException(_fake_response(503), "unavailable")
    poster = DiscordCodeAlertPoster(client, channel_id=999)

    with pytest.raises(PublishError):
        await poster.post(_alert(ping=False))


async def test_channel_fetch_403_propagates_unwrapped():
    client = FakeClient(None)
    client.fetch_error = discord.Forbidden(_fake_response(403), "missing access")
    poster = DiscordCodeAlertPoster(client, channel_id=999)

    with pytest.raises(discord.Forbidden):
        await poster.post(_alert(ping=False))


async def test_channel_fetch_transient_network_error_wraps_as_publish_error():
    import aiohttp

    client = FakeClient(None)
    client.fetch_error = aiohttp.ClientError("connection reset")
    poster = DiscordCodeAlertPoster(client, channel_id=999)

    with pytest.raises(PublishError):
        await poster.post(_alert(ping=False))


# --- full pipeline: render_code_alerts -> poster, continuations always none() ---


def _candidate(**kwargs) -> CodeCandidate:
    defaults = dict(
        code="AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
        golden=False,
        source_name="Gearbox Blog",
        item_url="https://e.com/a",
        fresh=True,
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


async def test_continuation_messages_always_post_with_allowed_mentions_none():
    candidates = [
        _candidate(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            item_url=f"https://example.com/{'a' * 300}/{i}",
        )
        for i in range(12)
    ]
    rendered = render_code_alerts(candidates, ping=True)
    assert len(rendered) > 1  # sanity: this really does split

    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=True)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    for message in rendered:
        await poster.post(message)

    # First send is the only one that used _PING_EVERYONE; every
    # continuation used AllowedMentions.none(), end to end through the
    # poster, not just at the RenderedAlert.ping level.
    assert channel.sent[0][1].to_dict() == {"parse": ["everyone"]}
    for _content, mentions in channel.sent[1:]:
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_continuation_messages_none_even_when_permission_missing():
    # A missing mention_everyone permission only matters to the message
    # that's actually trying to ping -- continuations never try, so they
    # should never trigger the missing-permission admin alert either.
    candidates = [
        _candidate(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            item_url=f"https://example.com/{'a' * 300}/{i}",
        )
        for i in range(12)
    ]
    rendered = render_code_alerts(candidates, ping=True)
    assert len(rendered) > 1

    channel = FakeChannel(guild=FakeGuild(me=FakeMember()), can_mention=False)
    client = FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    for message in rendered:
        await poster.post(message)

    # Exactly one admin alert (for the first, ping=True message); the
    # continuations' ping=False never consults the permission at all.
    assert len(client.alerts) == 1
