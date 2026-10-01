"""Adversarial tests for `DiscordPublisher`'s opt-in per-server options (plan task 6).

The four new keyword arguments have one job each and one rule in common: leave
a caller who passes none of them exactly where v2.2 left them. So half of this
file is "the default path didn't move" (same send arguments, same per-instance
nonce, a permanent error still ends the run) and half is the new options under
stress: write-through ordering, a cancellation in the middle of it, a preload
that a caller keeps mutating, every kind of Discord error landing on the skip
rule, and an embed that Discord itself refuses.

Same hand-written fakes as the neighbouring publisher tests (copied, not
imported: test modules aren't a package). No network.
"""

from __future__ import annotations

import asyncio
from datetime import date

import aiohttp
import discord
import pytest

from newsbot.bot.client import DiscordPublisher
from newsbot.bot.format import RenderedDigest, TopicMessage
from newsbot.pipeline.publisher import PublishError


class _Response:
    def __init__(self, status: int) -> None:
        self.status, self.reason, self.headers, self.request_info = status, "x", {}, None


class FakeChannel:
    def __init__(self, first_id: int = 100, log: list | None = None, name: str = "") -> None:
        self.sends: list[dict] = []
        self.attempt_nonces: list[str | None] = []
        self.fail: list[BaseException] = []
        self._next = first_id
        self._log = log
        self._name = name

    async def send(self, **kwargs):
        self.attempt_nonces.append(kwargs.get("nonce"))
        if self._log is not None:
            self._log.append(("send", self._name))
        if self.fail:
            raise self.fail.pop(0)
        self.sends.append(kwargs)
        self._next += 1
        return type("Msg", (), {"id": self._next})()


class FakeClient:
    def __init__(self, channels: dict[int, object]) -> None:
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


def http(status: int) -> discord.HTTPException:
    return discord.HTTPException(_Response(status), "x")


# --- the v2.2 path did not move ---


async def test_the_default_send_carries_exactly_embed_mentions_and_nonce():
    channel = FakeChannel()
    await DiscordPublisher(FakeClient({1: channel})).publish(rendered(("a", 1)))
    [sent] = channel.sends
    assert set(sent) == {"embed", "allowed_mentions", "nonce"}
    mentions = sent["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (
        False,
        False,
        False,
        False,
    )


async def test_every_new_option_leaves_the_send_arguments_unchanged():
    plain, loaded = FakeChannel(), FakeChannel()

    async def heard(key, message_id):
        pass

    await DiscordPublisher(FakeClient({1: plain})).publish(rendered(("a", 1)))
    await DiscordPublisher(
        FakeClient({1: loaded}),
        nonce_scope="s",
        on_posted=heard,
        already_posted={},
        skip_permanent=True,
    ).publish(rendered(("a", 1)))
    assert set(plain.sends[0]) == set(loaded.sends[0])
    assert loaded.sends[0]["allowed_mentions"].everyone is False


async def test_a_default_instance_reuses_its_nonce_on_retry_and_never_across_instances():
    first, second = FakeChannel(), FakeChannel()
    first.fail = [aiohttp.ClientError("reset")]
    publisher = DiscordPublisher(FakeClient({1: first}))
    with pytest.raises(PublishError):
        await publisher.publish(rendered(("a", 1)))
    await publisher.publish(rendered(("a", 1)))
    assert len(set(first.attempt_nonces)) == 1 and len(first.attempt_nonces) == 2
    await DiscordPublisher(FakeClient({1: second})).publish(rendered(("a", 1)))
    assert second.attempt_nonces[0] != first.attempt_nonces[0]


async def test_a_default_publisher_with_a_permanent_error_loses_nothing_it_had_posted():
    ok, bad = FakeChannel(), FakeChannel()
    bad.fail = [discord.Forbidden(_Response(403), "no")]
    publisher = DiscordPublisher(FakeClient({1: ok, 2: bad}))
    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(rendered(("a", 1), ("b", 2)))
    assert excinfo.value.retryable is False
    assert dict(excinfo.value.posted_by_topic) == {"a": 101}
    assert publisher.skipped == {}


async def test_posted_ids_and_posted_by_game_agree_and_are_copies():
    publisher = DiscordPublisher(FakeClient({1: FakeChannel(), 2: FakeChannel(200)}))
    await publisher.publish(rendered(("a", 1), ("b", 2)))
    assert publisher.posted_ids == list(publisher.posted_by_game.values()) == [101, 201]
    publisher.posted_by_game["x"] = 1
    assert publisher.posted_by_game == {"a": 101, "b": 201}


# --- nonce scope ---


@pytest.mark.parametrize("scope", ["", "1|2026-09-30", "ключ|日付", "x" * 10_000, "a|b\nc"])
async def test_any_scope_gives_a_25_character_hex_nonce_that_is_stable(scope):
    one, two = FakeChannel(), FakeChannel()
    await DiscordPublisher(FakeClient({1: one}), nonce_scope=scope).publish(rendered(("a", 1)))
    await DiscordPublisher(FakeClient({1: two}), nonce_scope=scope).publish(rendered(("a", 1)))
    nonce = one.attempt_nonces[0]
    assert nonce == two.attempt_nonces[0]
    assert len(nonce) == 25 and all(c in "0123456789abcdef" for c in nonce)


async def test_different_games_and_different_scopes_never_share_a_nonce():
    channel = FakeChannel()
    publisher = DiscordPublisher(FakeClient({1: channel}), nonce_scope="1|2026-09-30")
    await publisher.publish(rendered(("a", 1), ("b", 1), ("c", 1)))
    other = FakeChannel()
    await DiscordPublisher(FakeClient({1: other}), nonce_scope="2|2026-09-30").publish(
        rendered(("a", 1))
    )
    everything = [*channel.attempt_nonces, *other.attempt_nonces]
    assert len(set(everything)) == 4


async def test_two_games_sharing_one_channel_post_separately():
    channel = FakeChannel()
    publisher = DiscordPublisher(FakeClient({1: channel}), nonce_scope="s")
    assert await publisher.publish(rendered(("a", 1), ("b", 1))) == {"a": 101, "b": 102}
    assert len(channel.sends) == 2


# --- preload ---


async def test_the_preload_is_copied_not_shared():
    mine = {"a": 555}
    channel = FakeChannel()
    publisher = DiscordPublisher(FakeClient({1: channel, 2: FakeChannel()}), already_posted=mine)
    mine["b"] = 777  # the caller keeps using its dict
    mine.pop("a")
    assert await publisher.publish(rendered(("a", 1), ("b", 2))) == {"a": 555, "b": 101}
    assert channel.sends == []


async def test_a_preloaded_game_the_render_does_not_mention_still_comes_back():
    publisher = DiscordPublisher(FakeClient({2: FakeChannel()}), already_posted={"gone": 9})
    assert await publisher.publish(rendered(("b", 2))) == {"gone": 9, "b": 101}


async def test_an_error_after_a_preload_reports_the_preload_as_posted():
    bad = FakeChannel()
    bad.fail = [aiohttp.ClientError("reset")]
    publisher = DiscordPublisher(FakeClient({2: bad}), already_posted={"a": 555})
    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(rendered(("a", 1), ("b", 2)))
    assert dict(excinfo.value.posted_by_topic) == {"a": 555}


async def test_on_posted_is_never_called_for_a_preloaded_game():
    heard: list[str] = []

    async def on_posted(key, message_id):
        heard.append(key)

    publisher = DiscordPublisher(
        FakeClient({2: FakeChannel()}), already_posted={"a": 555}, on_posted=on_posted
    )
    await publisher.publish(rendered(("a", 1), ("b", 2)))
    assert heard == ["b"]


# --- write-through ordering and failure ---


async def test_each_write_through_finishes_before_the_next_send_starts():
    log: list[tuple[str, str]] = []

    async def on_posted(key, message_id):
        log.append(("start-write", key))
        await asyncio.sleep(0.01)  # a database that isn't instant
        log.append(("end-write", key))

    channels = {1: FakeChannel(log=log, name="a"), 2: FakeChannel(log=log, name="b")}
    await DiscordPublisher(FakeClient(channels), on_posted=on_posted).publish(
        rendered(("a", 1), ("b", 2))
    )
    assert log == [
        ("send", "a"),
        ("start-write", "a"),
        ("end-write", "a"),
        ("send", "b"),
        ("start-write", "b"),
        ("end-write", "b"),
    ]


async def test_a_cancellation_inside_the_write_through_is_not_swallowed():
    channel = FakeChannel()

    async def on_posted(key, message_id):
        raise asyncio.CancelledError

    publisher = DiscordPublisher(FakeClient({1: channel}), on_posted=on_posted)
    with pytest.raises(asyncio.CancelledError):
        await publisher.publish(rendered(("a", 1)))
    assert publisher.posted_by_game == {"a": 101}  # the send did land; the record didn't


@pytest.mark.parametrize("skip", [False, True])
async def test_a_cancellation_inside_a_send_propagates_untouched(skip):
    channel = FakeChannel()
    channel.fail = [asyncio.CancelledError()]
    publisher = DiscordPublisher(FakeClient({1: channel}), skip_permanent=skip)
    with pytest.raises(asyncio.CancelledError):
        await publisher.publish(rendered(("a", 1)))
    assert publisher.skipped == {}


async def test_a_non_discord_exception_in_a_send_is_not_mistaken_for_a_skippable_one():
    channel = FakeChannel()
    channel.fail = [RuntimeError("a bug, not a dead channel")]
    publisher = DiscordPublisher(FakeClient({1: channel}), skip_permanent=True)
    with pytest.raises(RuntimeError):
        await publisher.publish(rendered(("a", 1)))
    assert publisher.skipped == {}


# --- skip-and-continue against every error shape ---


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 413, 422])
async def test_client_errors_skip_the_game_and_carry_on(status):
    dead, fine = FakeChannel(), FakeChannel()
    dead.fail = [http(status)]
    publisher = DiscordPublisher(FakeClient({1: dead, 2: fine}), skip_permanent=True)
    assert await publisher.publish(rendered(("a", 1), ("b", 2))) == {"b": 101}
    assert set(publisher.skipped) == {"a"} and str(status) in publisher.skipped["a"]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_rate_limits_and_server_errors_are_retried_not_skipped(status):
    flaky = FakeChannel()
    flaky.fail = [http(status)]
    publisher = DiscordPublisher(FakeClient({1: flaky}), skip_permanent=True)
    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(rendered(("a", 1)))
    assert excinfo.value.retryable is True and publisher.skipped == {}
    assert await publisher.publish(rendered(("a", 1))) == {"a": 101}
    assert flaky.attempt_nonces[0] == flaky.attempt_nonces[1]  # same nonce: Discord can dedupe


async def test_every_game_dead_returns_nothing_posted_and_everything_skipped():
    channels = {}
    for ch in (1, 2, 3):
        channels[ch] = FakeChannel()
        channels[ch].fail = [discord.Forbidden(_Response(403), "no")]
    publisher = DiscordPublisher(FakeClient(channels), skip_permanent=True)
    assert await publisher.publish(rendered(("a", 1), ("b", 2), ("c", 3))) == {}
    assert set(publisher.skipped) == {"a", "b", "c"}


async def test_a_skip_then_a_transient_error_keeps_both_facts_across_the_retry():
    dead, flaky, tail = FakeChannel(), FakeChannel(), FakeChannel(300)
    dead.fail = [discord.Forbidden(_Response(403), "no")]
    flaky.fail = [aiohttp.ClientError("reset")]
    publisher = DiscordPublisher(FakeClient({1: dead, 2: flaky, 3: tail}), skip_permanent=True)
    message = rendered(("a", 1), ("b", 2), ("c", 3))
    with pytest.raises(PublishError) as excinfo:
        await publisher.publish(message)
    assert excinfo.value.retryable is True and set(publisher.skipped) == {"a"}
    assert await publisher.publish(message) == {"b": 101, "c": 301}
    assert dead.attempt_nonces == [dead.attempt_nonces[0]]  # the dead channel was tried once


async def test_a_skipped_channel_is_not_resolved_again_on_the_retry():
    lookups: list[int] = []

    class CountingClient(FakeClient):
        def get_channel(self, channel_id):
            lookups.append(channel_id)
            return super().get_channel(channel_id)

    flaky = FakeChannel()
    flaky.fail = [aiohttp.ClientError("reset")]
    publisher = DiscordPublisher(CountingClient({2: flaky}), skip_permanent=True)
    message = rendered(("a", 1), ("b", 2))  # channel 1 doesn't exist
    with pytest.raises(PublishError):
        await publisher.publish(message)
    await publisher.publish(message)
    assert lookups.count(1) == 1


async def test_an_embed_discord_refuses_is_skipped_like_a_dead_channel():
    # A 400 from a too-large or malformed embed looks, to the skip rule, just like a
    # deleted channel. Documents current behavior: the guild's notice will say "channel
    # may be deleted or missing permission", which isn't the whole truth for this case.
    refused = FakeChannel()
    refused.fail = [http(400)]
    publisher = DiscordPublisher(FakeClient({1: refused}), skip_permanent=True)
    assert await publisher.publish(rendered(("a", 1))) == {}
    assert "400" in publisher.skipped["a"]
