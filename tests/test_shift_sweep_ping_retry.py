"""Tests for QA item 1: a retried post must never risk a second live ping.

`shift/sweep.py._post_with_retry` only ever retries on `PublishError`, which
means "our client gave up waiting for a response" -- it is never proof the
message didn't actually land. Two things protect against that turning into
a duplicate `@everyone`:

1. Every attempt at the same `RenderedAlert` reuses its `nonce`
   (`bot/format.py`), so Discord's own dedup window can catch a message
   that landed the first time even though our client saw an error.
2. Independent of Discord's dedup, a ping-bearing alert that fails once
   retries with the ping already stripped (`_strip_ping`) -- the first
   attempt is the only one that could ever have pinged, landed or not.

The fake channel here mimics the scenario nonce dedup exists for: `send()`
actually appends the message (as if it reached Discord) but still raises,
simulating a response that never made it back to us.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import discord
import httpx
import pytest

from newsbot.bot.client import DiscordCodeAlertPoster
from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.shift.sweep import SweepDeps, process_items
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"


def _cfg(**alert_overrides):
    cfg = load_config(CONFIG_PATH)
    alerts = cfg.alerts.model_copy(update={"enabled": True, **alert_overrides})
    return cfg.model_copy(update={"alerts": alerts})


def _seed(db_path) -> None:
    from newsbot.store import repo

    with closing(connect(db_path)) as conn:
        repo.record_silent_codes(conn, [], now=lambda: NOW, mark_seeded=True)


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _FakePermissions:
    def __init__(self, *, mention_everyone: bool) -> None:
        self.mention_everyone = mention_everyone


class _FakeMember:
    pass


class _FakeGuild:
    def __init__(self) -> None:
        self.me = _FakeMember()


class _SucceedsButRaisesChannel:
    """`send()` "lands" the message (it's recorded) but still raises `fail_times` times.

    Simulating a send whose HTTP response never made it back -- from
    `_post_with_retry`'s point of view this looks exactly like a plain
    failure worth retrying, which is exactly the ambiguous case nonce
    dedup (and the ping-stripping belt-and-suspenders) exist for.
    """

    def __init__(self, *, fail_times: int) -> None:
        self.guild = _FakeGuild()
        self.fail_times = fail_times
        self.calls = 0
        self.sent: list[tuple[str, discord.AllowedMentions, str | None]] = []

    def permissions_for(self, member: object) -> _FakePermissions:
        return _FakePermissions(mention_everyone=True)

    async def send(self, content, *, allowed_mentions, nonce=None):
        self.calls += 1
        self.sent.append((content, allowed_mentions, nonce))
        if self.calls <= self.fail_times:
            raise discord.HTTPException(_FakeResponse(503), "unavailable")
        return _FakeMessage(1000 + self.calls)


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers = {}
        self.request_info = None


class _FakeClient:
    def __init__(self, channel) -> None:
        self._channel = channel
        self.alerts: list[str] = []

    def get_channel(self, channel_id: int):
        return self._channel

    async def fetch_channel(self, channel_id: int):
        return self._channel

    async def alert(self, text: str) -> None:
        self.alerts.append(text)


async def _no_sleep(_seconds: float) -> None:
    return None


def _item(code: str) -> RawItem:
    return RawItem(
        url=f"https://example.com/{code}",
        title=f"New code: {code}",
        excerpt="",
        source_name="Gearbox Blog",
        trust="official",
        published_at=NOW,
        topics=None,
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


async def test_retry_after_send_that_landed_but_errored_carries_the_ping_at_most_once(db_path):
    _seed(db_path)
    channel = _SucceedsButRaisesChannel(fail_times=2)  # fails twice, succeeds on the 3rd attempt
    client = _FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    async with httpx.AsyncClient() as http:
        deps = SweepDeps(
            cfg=_cfg(),
            db_path=db_path,
            http=http,
            collectors=[],
            now=lambda: NOW,
            alert=client.alert,
            poster=poster,
            sleep=_no_sleep,
        )
        outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)

    assert outcome.posted == 1
    assert channel.calls == 3  # first attempt + 2 retries, third one finally "landed" for us
    # Every attempt reused the same nonce -- Discord's own dedup can catch
    # a message that actually landed on an earlier, errored-out attempt.
    nonces = {n for _content, _mentions, n in channel.sent}
    assert len(nonces) == 1
    assert nonces.pop()  # non-empty
    # Only the *first* attempt could ever have carried a live ping --
    # every retry after it went out with AllowedMentions.none() and no
    # literal "@everyone " prefix, regardless of whether the first
    # attempt actually reached Discord.
    first_content, first_mentions, _ = channel.sent[0]
    assert first_mentions.to_dict() == {"parse": ["everyone"]}
    assert first_content.startswith("@everyone ")
    for content, mentions, _ in channel.sent[1:]:
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()
        assert not content.startswith("@everyone ")

    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status, pinged FROM alerted_codes WHERE code = ?", (CODE_A,)
        ).fetchone()
    assert row["status"] == "posted"
    assert row["pinged"] == 1  # the budget was spent once, at claim time, unaffected by retries


async def test_retry_never_exceeds_one_ping_bearing_send_even_when_every_attempt_fails(db_path):
    _seed(db_path)
    channel = _SucceedsButRaisesChannel(fail_times=999)  # never succeeds
    client = _FakeClient(channel)
    poster = DiscordCodeAlertPoster(client, channel_id=1)

    async with httpx.AsyncClient() as http:
        deps = SweepDeps(
            cfg=_cfg(),
            db_path=db_path,
            http=http,
            collectors=[],
            now=lambda: NOW,
            alert=client.alert,
            poster=poster,
            sleep=_no_sleep,
        )
        outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)

    assert outcome.posted == 0
    assert outcome.failed == 1
    ping_bearing = [m for _c, m, _n in channel.sent if m.to_dict() == {"parse": ["everyone"]}]
    assert len(ping_bearing) <= 1
