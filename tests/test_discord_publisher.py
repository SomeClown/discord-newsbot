"""Tests for `DiscordPublisher`'s resumability and error classification.

Per plan section 5 the bot layer normally gets no gateway mocking -- but
`DiscordPublisher` *is* the seam between the pipeline and discord.py, so
there's no pure function to extract the interesting behavior into. Instead
of mocking discord.py's `Client`/`Message` internals, these tests hand it
tiny hand-written fakes that implement only the handful of calls
`DiscordPublisher` actually makes (`get_channel`, `fetch_channel`,
`channel.send`, `message.create_thread`, `client.alert`) -- the same spirit
as `FixtureCollector`/`StubLLM` standing in for the real thing elsewhere in
this codebase.

The behavior worth pinning: a retried `publish()` call must never repost a
header or embed message it already got an id back for, transient errors
(network blips, 5xx) must come back wrapped as `PublishError` carrying
whatever got posted so far, and a 4xx like `discord.Forbidden` must escape
unwrapped so `_publish_with_retry` doesn't waste backoff retrying a
permissions problem that will never fix itself.
"""

from __future__ import annotations

from datetime import date

import aiohttp
import discord
import pytest

from newsbot.bot.client import DiscordPublisher
from newsbot.bot.format import RenderedDigest
from newsbot.pipeline.publisher import PublishError

RUN_DATE = date(2026, 9, 23)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.thread_created_with: str | None = None
        self.thread_error: Exception | None = None

    async def create_thread(self, *, name: str) -> None:
        if self.thread_error is not None:
            raise self.thread_error
        self.thread_created_with = name


class FakeChannel:
    """Records every `send()` call; can be told to fail on a given call index."""

    def __init__(self) -> None:
        self.sent: list[tuple[str | None, object]] = []
        self._next_id = 100
        self.fail_on_call: dict[int, Exception] = {}
        self._call_count = 0

    async def send(self, content: str | None = None, *, embeds=None, allowed_mentions=None):
        call_index = self._call_count
        self._call_count += 1
        if call_index in self.fail_on_call:
            raise self.fail_on_call[call_index]
        self._next_id += 1
        message = FakeMessage(self._next_id)
        self.sent.append((content, embeds))
        return message


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


def _rendered(n_embed_messages: int) -> RenderedDigest:
    embed = discord.Embed(title="story")
    return RenderedDigest(
        header="# News for 2026-09-23",
        embed_messages=[[embed] for _ in range(n_embed_messages)],
    )


async def test_publish_happy_path_posts_header_thread_and_embeds():
    channel = FakeChannel()
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    ids = await publisher.publish(_rendered(2))

    assert len(ids) == 3  # header + 2 embed messages
    assert len(channel.sent) == 3
    assert channel.sent[0][0] == "# News for 2026-09-23"


async def test_retry_after_embed_failure_does_not_repost_header():
    channel = FakeChannel()
    # Header (call 0) and first embed (call 1) succeed; second embed
    # (call 2) fails transiently on the first attempt only.
    channel.fail_on_call = {2: aiohttp.ClientError("connection reset")}
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(2))
    assert len(excinfo.value.posted_ids) == 2  # header + first embed, both already posted
    assert len(channel.sent) == 2

    # Retry the same publisher instance -- it must resume, not repost
    # the header or the first embed.
    channel.fail_on_call = {}
    ids = await publisher.publish(_rendered(2))
    assert len(ids) == 3
    assert len(channel.sent) == 3  # only the missing embed got sent on retry


async def test_transient_network_error_is_wrapped_as_publish_error():
    channel = FakeChannel()
    channel.fail_on_call = {0: OSError("network unreachable")}
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    with pytest.raises(PublishError):
        await publisher.publish(_rendered(1))


async def test_forbidden_is_not_wrapped_and_not_retryable():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.Forbidden(_fake_response(403), "missing access")}
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    with pytest.raises(discord.Forbidden):
        await publisher.publish(_rendered(1))


async def test_other_4xx_is_not_wrapped():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.HTTPException(_fake_response(400), "bad request")}
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    with pytest.raises(discord.HTTPException):
        await publisher.publish(_rendered(1))


async def test_discord_5xx_is_wrapped_and_retryable():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.HTTPException(_fake_response(503), "service unavailable")}
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    with pytest.raises(PublishError):
        await publisher.publish(_rendered(1))


async def test_thread_creation_failure_is_non_fatal_and_alerts_admin():
    channel = FakeChannel()
    client = FakeClient(channel)
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    # Make the header message's create_thread blow up, without touching
    # publish()'s own send/receive path.
    real_send = channel.send

    async def send_and_break_thread(content=None, *, embeds=None, allowed_mentions=None):
        message = await real_send(content, embeds=embeds, allowed_mentions=allowed_mentions)
        if content is not None:  # the header call
            message.thread_error = discord.HTTPException(_fake_response(500), "thread failed")
        return message

    channel.send = send_and_break_thread

    ids = await publisher.publish(_rendered(1))

    assert len(ids) == 2  # header + embed message still both posted
    assert len(client.alerts) == 1
    assert "thread" in client.alerts[0].lower()


async def test_channel_fetch_failure_is_wrapped_as_publish_error():
    client = FakeClient(channel=None)
    client.fetch_error = aiohttp.ClientError("dns failure")
    publisher = DiscordPublisher(client, channel_id=1, run_date=RUN_DATE)

    with pytest.raises(PublishError):
        await publisher.publish(_rendered(1))
