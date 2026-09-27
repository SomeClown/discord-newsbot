"""Tests for `DiscordPublisher`'s resumability and error classification.

Per plan section 5 the bot layer normally gets no gateway mocking -- but
`DiscordPublisher` *is* the seam between the pipeline and discord.py, so
there's no pure function to extract the interesting behavior into. Instead
of mocking discord.py's `Client`/`Message` internals, these tests hand it
tiny hand-written fakes that implement only the handful of calls
`DiscordPublisher` actually makes (`get_channel`, `fetch_channel`,
`channel.send`) -- the same spirit as `FixtureCollector`/`StubLLM` standing
in for the real thing elsewhere in this codebase.

design.md §13 moved the unit of progress from "message" to "topic": each
topic gets its own embed, in its own channel, so the behavior worth
pinning is now: a retried `publish()` call must never repost a topic it
already got an id back for, transient errors (network blips, 5xx) must
come back wrapped as `PublishError(retryable=True)` carrying whatever
posted so far, and a permanent error (a 4xx, or a channel that isn't
sendable) must come back as `PublishError(retryable=False)` -- so
`_publish_with_retry` doesn't waste backoff retrying a permissions problem
that will never fix itself, while still keeping whatever did land.
"""

from __future__ import annotations

import aiohttp
import discord
import pytest

from newsbot.bot.client import DiscordPublisher
from newsbot.bot.format import RenderedDigest, TopicMessage
from newsbot.pipeline.publisher import PublishError


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeChannel:
    """Records every `send()` call; can be told to fail on a given call index."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self._next_id = 100
        self.fail_on_call: dict[int, Exception] = {}
        self._call_count = 0

    async def send(self, content: str | None = None, *, embed=None, allowed_mentions=None):
        call_index = self._call_count
        self._call_count += 1
        if call_index in self.fail_on_call:
            raise self.fail_on_call[call_index]
        self._next_id += 1
        message = FakeMessage(self._next_id)
        self.sent.append(embed)
        return message


class FakeClient:
    def __init__(self, channels: dict[int, FakeChannel]) -> None:
        self._channels = channels
        self.alerts: list[str] = []
        self.fetch_error: Exception | None = None

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        if self.fetch_error is not None:
            raise self.fetch_error
        channel = self._channels.get(channel_id)
        if channel is None:
            raise discord.HTTPException(_fake_response(404), "not found")
        return channel

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


def _topic_message(topic_key: str, channel_id: int) -> TopicMessage:
    return TopicMessage(
        topic_key=topic_key,
        topic_name=topic_key,
        channel_id=channel_id,
        embed=discord.Embed(title=topic_key),
    )


def _rendered(*messages: TopicMessage) -> RenderedDigest:
    from datetime import date

    return RenderedDigest(run_date=date(2026, 9, 23), messages=list(messages), coverage_notes=[])


async def test_publish_happy_path_posts_one_message_per_topic_to_its_own_channel():
    bl4 = FakeChannel()
    palworld = FakeChannel()
    client = FakeClient({1: bl4, 2: palworld})
    publisher = DiscordPublisher(client)

    ids = await publisher.publish(
        _rendered(_topic_message("borderlands4", 1), _topic_message("palworld", 2))
    )

    assert set(ids) == {"borderlands4", "palworld"}
    assert len(bl4.sent) == 1
    assert len(palworld.sent) == 1


async def test_retry_after_one_topics_failure_does_not_repost_the_other():
    bl4 = FakeChannel()
    palworld = FakeChannel()
    # borderlands4 (call 0 on its own channel) succeeds; palworld (call 0
    # on its own channel) fails transiently on the first attempt only.
    palworld.fail_on_call = {0: aiohttp.ClientError("connection reset")}
    client = FakeClient({1: bl4, 2: palworld})
    publisher = DiscordPublisher(client)
    rendered = _rendered(_topic_message("borderlands4", 1), _topic_message("palworld", 2))

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(rendered)
    assert list(excinfo.value.posted_by_topic) == ["borderlands4"]
    assert len(bl4.sent) == 1
    assert len(palworld.sent) == 0

    # Retry the same publisher instance -- it must resume, not repost
    # borderlands4's embed.
    palworld.fail_on_call = {}
    ids = await publisher.publish(rendered)
    assert set(ids) == {"borderlands4", "palworld"}
    assert len(bl4.sent) == 1  # not reposted
    assert len(palworld.sent) == 1


async def test_transient_network_error_is_wrapped_as_retryable_publish_error():
    channel = FakeChannel()
    channel.fail_on_call = {0: OSError("network unreachable")}
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(_topic_message("borderlands4", 1)))
    assert excinfo.value.retryable is True


async def test_forbidden_is_wrapped_and_not_retryable():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.Forbidden(_fake_response(403), "missing access")}
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(_topic_message("borderlands4", 1)))
    assert excinfo.value.retryable is False


async def test_other_4xx_is_wrapped_and_not_retryable():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.HTTPException(_fake_response(400), "bad request")}
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(_topic_message("borderlands4", 1)))
    assert excinfo.value.retryable is False


async def test_discord_5xx_is_wrapped_and_retryable():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.HTTPException(_fake_response(503), "service unavailable")}
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(_topic_message("borderlands4", 1)))
    assert excinfo.value.retryable is True


async def test_channel_fetch_failure_is_wrapped_as_retryable_publish_error():
    client = FakeClient(channels={})
    client.fetch_error = aiohttp.ClientError("dns failure")
    publisher = DiscordPublisher(client)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(_topic_message("borderlands4", 1)))
    assert excinfo.value.retryable is True


async def test_non_sendable_channel_is_a_permanent_publish_error():
    # A resolved "channel" with no send() at all -- a category, a voice
    # channel misconfigured into channel_id -- isn't something a retry
    # ever fixes.
    class _NotSendable:
        pass

    client = FakeClient({1: _NotSendable()})
    publisher = DiscordPublisher(client)

    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(_rendered(_topic_message("borderlands4", 1)))
    assert excinfo.value.retryable is False


async def test_posted_ids_property_reflects_progress_so_far():
    bl4 = FakeChannel()
    palworld = FakeChannel()
    palworld.fail_on_call = {0: aiohttp.ClientError("blip")}
    client = FakeClient({1: bl4, 2: palworld})
    publisher = DiscordPublisher(client)
    rendered = _rendered(_topic_message("borderlands4", 1), _topic_message("palworld", 2))

    with pytest.raises(PublishError):
        await publisher.publish(rendered)

    assert len(publisher.posted_ids) == 1


async def test_second_topic_channel_resolution_is_cached_across_calls():
    # publish() is called with the same rendered digest twice on a
    # publisher that's already posted everything -- the second call
    # shouldn't repost, and shouldn't need to re-fetch a channel either.
    channel = FakeChannel()
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)
    rendered = _rendered(_topic_message("borderlands4", 1))

    ids = await publisher.publish(rendered)
    ids_again = await publisher.publish(rendered)

    assert ids == ids_again
    assert len(channel.sent) == 1


# --- adversarial: multiple retries, several topics failing in sequence ---


async def test_retry_survives_two_consecutive_transient_failures_before_succeeding():
    channel = FakeChannel()
    channel.fail_on_call = {0: aiohttp.ClientError("blip 1")}
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)
    rendered = _rendered(_topic_message("borderlands4", 1))

    with pytest.raises(PublishError):
        await publisher.publish(rendered)
    assert len(channel.sent) == 0

    channel.fail_on_call = {1: aiohttp.ClientError("blip 2")}
    with pytest.raises(PublishError):
        await publisher.publish(rendered)
    assert len(channel.sent) == 0

    channel.fail_on_call = {}
    ids = await publisher.publish(rendered)
    assert ids == {"borderlands4": channel._next_id}
    assert len(channel.sent) == 1  # sent exactly once total, across all three attempts


async def test_publish_with_no_topics_returns_empty_and_is_resumable():
    client = FakeClient({})
    publisher = DiscordPublisher(client)

    ids = await publisher.publish(_rendered())
    assert ids == {}

    ids_again = await publisher.publish(_rendered())
    assert ids_again == {}
