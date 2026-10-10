"""`DiscordCodeAlertEditor`: the real bot's way of editing an alert in place (plan task 8).

Same spirit as `test_code_alert_poster.py`: tiny hand-written fakes for the few discord.py
calls the class makes. What matters is that an edit never carries a mention (the one
`everyone=True` in the codebase stays the poster's), that Discord's errors come out as the
three outcomes the edit pass understands (gone, forbidden, retry later), and that the editor
refuses a channel that isn't in the server it was built for.
"""

from __future__ import annotations

import aiohttp
import discord
import pytest

from newsbot.bot.client import DiscordCodeAlertEditor
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.sweep import EditForbiddenError, MessageGoneError

GUILD = 300000000000000001
CHANNEL = 55


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers: dict[str, str] = {}
        self.request_info = None


def _error(cls, status: int, code: int = 0, message: str = "nope", **attrs):
    exc = cls(_Response(status), {"code": code, "message": message})
    for name, value in attrs.items():
        setattr(exc, name, value)
    return exc


class _Partial:
    def __init__(self, channel: _Channel, message_id: int) -> None:
        self._channel = channel
        self._message_id = message_id

    async def edit(self, **kwargs) -> None:
        if self._channel.error is not None:
            raise self._channel.error
        self._channel.edits.append((self._message_id, kwargs))


class _Channel:
    def __init__(self, guild_id: int | None = GUILD) -> None:
        self.guild = type("G", (), {"id": guild_id})() if guild_id is not None else None
        self.edits: list[tuple[int, dict]] = []
        self.error: Exception | None = None

    def get_partial_message(self, message_id: int) -> _Partial:
        return _Partial(self, message_id)


class _NoPartials:
    guild = type("G", (), {"id": GUILD})()


class _Client:
    def __init__(
        self, channel=None, *, cached: bool = True, fetch_error: Exception | None = None
    ) -> None:
        self._channel = channel
        self._cached = cached
        self.fetch_error = fetch_error
        self.fetches = 0

    def get_channel(self, _channel_id):
        return self._channel if self._cached else None

    async def fetch_channel(self, _channel_id):
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self._channel


def _editor(channel, **kwargs) -> DiscordCodeAlertEditor:
    return DiscordCodeAlertEditor(_Client(channel, **kwargs), CHANNEL, guild_id=GUILD)


async def test_an_edit_goes_to_the_message_with_every_mention_switched_off():
    channel = _Channel()

    await _editor(channel).edit(9001, "@everyone **New SHiFT code**\n\n```\nCODE\n```")

    ((message_id, kwargs),) = channel.edits
    assert message_id == 9001
    assert kwargs["content"].startswith("@everyone ")  # the text stays; it just can't ping
    mentions = kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles
    assert not mentions.replied_user


async def test_an_uncached_channel_is_fetched():
    channel = _Channel()
    client = _Client(channel, cached=False)

    await DiscordCodeAlertEditor(client, CHANNEL, guild_id=GUILD).edit(1, "x")

    assert client.fetches == 1 and len(channel.edits) == 1


async def test_a_channel_in_another_server_is_never_edited():
    channel = _Channel(guild_id=GUILD + 1)

    with pytest.raises(MessageGoneError):
        await _editor(channel).edit(1, "x")

    assert channel.edits == []


async def test_a_channel_with_no_server_is_never_edited():
    channel = _Channel(guild_id=None)

    with pytest.raises(MessageGoneError):
        await _editor(channel).edit(1, "x")


async def test_a_channel_that_cant_hold_messages_is_gone():
    with pytest.raises(MessageGoneError):
        await _editor(_NoPartials()).edit(1, "x")


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (_error(discord.NotFound, 404, 10008, "Unknown Message"), MessageGoneError),
        (_error(discord.HTTPException, 404, 10008, "Unknown Message"), MessageGoneError),
        (_error(discord.NotFound, 404, 10003, "Unknown Channel"), MessageGoneError),
        (_error(discord.Forbidden, 403, 50013, "Missing Permissions"), EditForbiddenError),
        (_error(discord.Forbidden, 403, 50001, "Missing Access"), EditForbiddenError),
        (_error(discord.HTTPException, 403, 0, "plain 403"), EditForbiddenError),
        (_error(discord.HTTPException, 400, 50013, "odd but listed"), EditForbiddenError),
        (_error(discord.HTTPException, 429, 0, "slow down", retry_after=2.5), PublishError),
        (_error(discord.HTTPException, 503, 0, "unavailable"), PublishError),
        (aiohttp.ClientError("reset"), PublishError),
        (TimeoutError(), PublishError),
    ],
)
async def test_discords_errors_come_out_as_the_three_outcomes(error, outcome):
    channel = _Channel()
    channel.error = error

    with pytest.raises(outcome):
        await _editor(channel).edit(1, "x")


async def test_a_429_carries_what_the_retry_needs():
    channel = _Channel()
    channel.error = _error(discord.HTTPException, 429, 0, "slow down", retry_after=2.5)

    with pytest.raises(PublishError) as caught:
        await _editor(channel).edit(1, "x")

    assert caught.value.rejected is True
    assert caught.value.retry_after == 2.5


async def test_a_5xx_is_not_marked_rejected():
    channel = _Channel()
    channel.error = _error(discord.HTTPException, 503, 0, "unavailable")

    with pytest.raises(PublishError) as caught:
        await _editor(channel).edit(1, "x")

    assert caught.value.rejected is False


async def test_some_other_4xx_is_not_swallowed():
    channel = _Channel()
    channel.error = _error(discord.HTTPException, 400, 50035, "Invalid Form Body")

    with pytest.raises(discord.HTTPException):
        await _editor(channel).edit(1, "x")


async def test_an_unknown_channel_when_fetching_is_gone():
    error = _error(discord.NotFound, 404, 10003, "Unknown Channel")

    with pytest.raises(MessageGoneError):
        await _editor(None, cached=False, fetch_error=error).edit(1, "x")


async def test_a_channel_the_bot_cant_see_when_fetching_is_forbidden():
    error = _error(discord.Forbidden, 403, 50001, "Missing Access")

    with pytest.raises(EditForbiddenError):
        await _editor(None, cached=False, fetch_error=error).edit(1, "x")
