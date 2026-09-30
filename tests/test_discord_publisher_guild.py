"""`DiscordPublisher`'s per-server options: write-through, preload, nonce scope, skip-and-continue.

Same hand-written fakes as `test_discord_publisher.py` (copied, not imported:
test modules aren't a package). The default behavior, with none of these
options, is pinned by that file and isn't repeated here.
"""

from __future__ import annotations

from datetime import date

import aiohttp
import discord
import pytest

from newsbot.bot.client import DiscordPublisher
from newsbot.bot.format import RenderedDigest, TopicMessage
from newsbot.pipeline.publisher import PublishError


class FakeChannel:
    def __init__(self, first_id: int = 100) -> None:
        self.sent = 0
        self.nonces: list[str | None] = []
        self.fail: list[Exception] = []
        self._next = first_id

    async def send(self, content=None, *, embed=None, allowed_mentions=None, nonce=None):
        if self.fail:
            raise self.fail.pop(0)
        self.nonces.append(nonce)
        self.sent += 1
        self._next += 1
        return type("Msg", (), {"id": self._next})()


class _Response:
    def __init__(self, status: int) -> None:
        self.status, self.reason, self.headers, self.request_info = status, "x", {}, None


class FakeClient:
    def __init__(self, channels: dict[int, FakeChannel]) -> None:
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(_Response(404), "gone")


def rendered(*pairs: tuple[str, int]) -> RenderedDigest:
    return RenderedDigest(
        date(2026, 9, 30),
        [TopicMessage(k, k, ch, discord.Embed(title=k)) for k, ch in pairs],
        [],
    )


async def test_on_posted_hears_each_game_as_it_lands_before_a_later_failure():
    ok, bad = FakeChannel(), FakeChannel()
    bad.fail = [aiohttp.ClientError("reset")]
    heard: list[tuple[str, int]] = []

    async def on_posted(key, message_id):
        heard.append((key, message_id))

    publisher = DiscordPublisher(FakeClient({1: ok, 2: bad}), on_posted=on_posted)
    with pytest.raises(PublishError):
        await publisher.publish(rendered(("a", 1), ("b", 2)))
    assert heard == [("a", 101)]  # written through before b's failure
    await publisher.publish(rendered(("a", 1), ("b", 2)))
    assert heard == [("a", 101), ("b", 101)]


async def test_a_failing_on_posted_does_not_fail_or_repeat_the_post():
    channel = FakeChannel()

    async def on_posted(key, message_id):
        raise RuntimeError("database is locked")

    publisher = DiscordPublisher(FakeClient({1: channel}), on_posted=on_posted)
    assert await publisher.publish(rendered(("a", 1))) == {"a": 101}
    assert await publisher.publish(rendered(("a", 1))) == {"a": 101}
    assert channel.sent == 1


async def test_already_posted_games_are_skipped_and_included_in_the_result():
    a, b = FakeChannel(), FakeChannel()
    publisher = DiscordPublisher(FakeClient({1: a, 2: b}), already_posted={"a": 555})
    assert await publisher.publish(rendered(("a", 1), ("b", 2))) == {"a": 555, "b": 101}
    assert a.sent == 0 and b.sent == 1
    assert publisher.posted_by_game == {"a": 555, "b": 101}


async def test_a_nonce_scope_makes_nonces_stable_across_instances_and_per_game():
    first, second = FakeChannel(), FakeChannel()
    await DiscordPublisher(FakeClient({1: first}), nonce_scope="1|2026-09-30").publish(
        rendered(("a", 1))
    )
    await DiscordPublisher(FakeClient({1: second}), nonce_scope="1|2026-09-30").publish(
        rendered(("a", 1))
    )
    assert first.nonces == second.nonces and len(first.nonces[0]) == 25
    third = FakeChannel()
    await DiscordPublisher(FakeClient({1: third}), nonce_scope="1|2026-10-01").publish(
        rendered(("a", 1))
    )
    assert third.nonces != first.nonces


async def test_without_a_scope_each_instance_gets_its_own_nonce_as_before():
    first, second = FakeChannel(), FakeChannel()
    await DiscordPublisher(FakeClient({1: first})).publish(rendered(("a", 1)))
    await DiscordPublisher(FakeClient({1: second})).publish(rendered(("a", 1)))
    assert first.nonces != second.nonces


async def test_skip_permanent_skips_a_dead_channel_and_carries_on():
    good = FakeChannel()
    forbidden = FakeChannel()
    forbidden.fail = [discord.Forbidden(_Response(403), "missing access")]
    publisher = DiscordPublisher(FakeClient({1: forbidden, 3: good}), skip_permanent=True)
    # Channel 2 doesn't exist (404 on fetch); channel 1 is forbidden; 3 is fine.
    result = await publisher.publish(rendered(("a", 1), ("b", 2), ("c", 3)))
    assert result == {"c": 101}
    assert set(publisher.skipped) == {"a", "b"}
    assert "403" in publisher.skipped["a"] and "404" in publisher.skipped["b"]


async def test_a_skipped_game_is_not_retried_on_the_next_attempt():
    channel = FakeChannel()
    channel.fail = [discord.Forbidden(_Response(403), "missing access")]
    publisher = DiscordPublisher(FakeClient({1: channel}), skip_permanent=True)
    await publisher.publish(rendered(("a", 1)))
    await publisher.publish(rendered(("a", 1)))
    assert channel.sent == 0 and list(publisher.skipped) == ["a"]


async def test_a_non_text_channel_is_skipped_too():
    voice = object()  # no send()
    publisher = DiscordPublisher(FakeClient({1: voice}), skip_permanent=True)
    assert await publisher.publish(rendered(("a", 1))) == {}
    assert "not a text channel" in publisher.skipped["a"]


async def test_skip_permanent_still_raises_on_transient_errors():
    channel = FakeChannel()
    channel.fail = [aiohttp.ClientError("reset")]
    publisher = DiscordPublisher(FakeClient({1: channel}), skip_permanent=True)
    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(rendered(("a", 1)))
    assert excinfo.value.retryable is True and publisher.skipped == {}


async def test_default_publisher_still_stops_at_a_permanent_error():
    channel = FakeChannel()
    channel.fail = [discord.Forbidden(_Response(403), "missing access")]
    publisher = DiscordPublisher(FakeClient({1: channel}))
    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(rendered(("a", 1)))
    assert excinfo.value.retryable is False
