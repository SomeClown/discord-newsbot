"""`_publish_with_retry` x `DiscordPublisher` orchestration (design.md §13).

`test_discord_publisher.py` pins `DiscordPublisher.publish()` itself in
isolation; `test_run_daily_state_machine.py` pins the whole `run_daily`
state machine. This file sits in between: it exercises the actual retry
loop (`newsbot/pipeline/run.py::_publish_with_retry`) driving a real
`DiscordPublisher` against hand-written channel/client fakes, across
several topics and several attempts -- the shape of bug that only shows up
once retries and multiple topics are both in play at once (a topic posted
on attempt 1 getting silently reposted on attempt 3, say). Same
hand-written-fakes spirit as `test_discord_publisher.py`: no gateway
mocking, just the handful of calls `DiscordPublisher` actually makes.
"""

from __future__ import annotations

from datetime import date

import aiohttp
import discord

from newsbot.bot.client import DiscordPublisher
from newsbot.bot.format import RenderedDigest, TopicMessage
from newsbot.pipeline.run import _publish_with_retry


async def _no_sleep(_seconds: float) -> None:
    return None


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeChannel:
    """Records every `send()` call; can be told to fail on given call indices."""

    def __init__(self, next_id: int = 100) -> None:
        self.sent: list[object] = []
        self._next_id = next_id
        self.fail_on_call: dict[int, Exception] = {}
        self._call_count = 0

    async def send(
        self, content: str | None = None, *, embed=None, allowed_mentions=None, nonce=None
    ):
        call_index = self._call_count
        self._call_count += 1
        if call_index in self.fail_on_call:
            raise self.fail_on_call[call_index]
        self._next_id += 1
        message = FakeMessage(self._next_id)
        self.sent.append(embed)
        return message


class FakeClient:
    def __init__(self, channels: dict[int, object]) -> None:
        self._channels = channels

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        channel = self._channels.get(channel_id)
        if channel is None:
            raise discord.HTTPException(_fake_response(404), "not found")
        return channel


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
    return RenderedDigest(run_date=date(2026, 9, 23), messages=list(messages), coverage_notes=[])


async def test_transient_failure_on_middle_topic_then_success_sends_exactly_once_per_topic():
    # Three topics; the second one's channel blips once. The retry must
    # resume from there -- topic 1 and topic 3 must never be sent twice.
    bl4 = FakeChannel()
    palworld = FakeChannel()
    palworld.fail_on_call = {0: aiohttp.ClientError("connection reset")}
    diablo = FakeChannel()
    client = FakeClient({1: bl4, 2: palworld, 3: diablo})
    publisher = DiscordPublisher(client)
    rendered = _rendered(
        _topic_message("borderlands4", 1),
        _topic_message("palworld", 2),
        _topic_message("diablo4", 3),
    )

    posted, error = await _publish_with_retry(publisher, rendered, sleep=_no_sleep)

    assert error is None
    assert set(posted) == {"borderlands4", "palworld", "diablo4"}
    assert len(bl4.sent) == 1
    assert len(palworld.sent) == 1
    assert len(diablo.sent) == 1


async def test_transient_twice_on_first_topic_then_permanent_on_second_stops_and_keeps_first_id():
    # Attempt 1: topic 1 blips (transient). Attempt 2: topic 1 blips again
    # (transient). Attempt 3: topic 1 finally lands, but topic 2 comes
    # back permanent (a 4xx) -- the loop must stop right there, never
    # trying a 4th attempt, and must keep topic 1's id.
    bl4 = FakeChannel()
    bl4.fail_on_call = {0: aiohttp.ClientError("blip 1"), 1: aiohttp.ClientError("blip 2")}
    palworld = FakeChannel()
    palworld.fail_on_call = {0: discord.HTTPException(_fake_response(403), "forbidden")}
    client = FakeClient({1: bl4, 2: palworld})
    publisher = DiscordPublisher(client)
    rendered = _rendered(_topic_message("borderlands4", 1), _topic_message("palworld", 2))

    posted, error = await _publish_with_retry(publisher, rendered, sleep=_no_sleep)

    assert error is not None
    assert error.retryable is False
    assert set(posted) == {"borderlands4"}
    assert posted["borderlands4"] == publisher.posted_ids[0]
    assert list(error.posted_by_topic) == ["borderlands4"]
    # Exactly 3 attempts total: two failed sends on topic 1's channel plus
    # the one that finally landed. Topic 2's channel is only ever tried
    # once (the permanent failure), never retried past that.
    assert bl4._call_count == 3
    assert palworld._call_count == 1
    assert len(bl4.sent) == 1  # landed exactly once, not reposted on a later attempt


async def test_channel_fetch_not_found_is_a_permanent_error():
    client = FakeClient(channels={})  # fetch_channel raises 404 for anything
    publisher = DiscordPublisher(client)

    posted, error = await _publish_with_retry(
        publisher, _rendered(_topic_message("borderlands4", 1)), sleep=_no_sleep
    )

    assert posted == {}
    assert error is not None
    assert error.retryable is False


async def test_two_topics_sharing_one_channel_both_post_and_are_both_tracked():
    shared = FakeChannel()
    client = FakeClient({1: shared})
    publisher = DiscordPublisher(client)
    rendered = _rendered(
        _topic_message("borderlands4", 1),
        _topic_message("palworld", 1),
    )

    posted, error = await _publish_with_retry(publisher, rendered, sleep=_no_sleep)

    assert error is None
    assert len(shared.sent) == 2
    assert len(posted) == 2
    assert posted["borderlands4"] != posted["palworld"]


async def test_topic_channel_id_equal_to_the_admin_channel_id_still_posts_normally():
    # design.md §13 doesn't forbid a topic sharing its channel_id with the
    # admin channel -- DiscordPublisher has no concept of "the admin
    # channel" at all, so it should just post there like anywhere else.
    admin_and_topic_channel = FakeChannel()
    client = FakeClient({42: admin_and_topic_channel})
    publisher = DiscordPublisher(client)

    posted, error = await _publish_with_retry(
        publisher, _rendered(_topic_message("borderlands4", 42)), sleep=_no_sleep
    )

    assert error is None
    assert len(admin_and_topic_channel.sent) == 1
    assert "borderlands4" in posted


async def test_permanent_failure_after_no_prior_success_reports_empty_posted_by_topic():
    channel = FakeChannel()
    channel.fail_on_call = {0: discord.HTTPException(_fake_response(400), "bad request")}
    client = FakeClient({1: channel})
    publisher = DiscordPublisher(client)

    posted, error = await _publish_with_retry(
        publisher, _rendered(_topic_message("borderlands4", 1)), sleep=_no_sleep
    )

    assert posted == {}
    assert error is not None
    assert error.posted_by_topic == {}
    assert error.retryable is False
