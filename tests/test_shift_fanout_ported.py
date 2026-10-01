"""The v2 SHiFT sweep's ping, roundup and retry rules, asked of the per-server fan-out.

The sweep (`shift/sweep.py`'s `process_items`, one server, one budget) retired with the daily
job. Its tests were the safety net under the part of this bot that can wake a whole server up,
so none of them were just deleted: the ones the fan-out tests (`test_shift_fanout.py` and
`test_shift_fanout_adversarial.py`) already asked in other words are mapped in the report
for this change, and the rest live here, in the same harness, with the same assertions:

- who a batch pings (press counts, community alone doesn't, trusted codes lead the message);
- the roundup rules (never pinged, never spends the budget, the 50 code cap, stale ones
  are recorded and never alerted, one code in two roundup items posts once);
- a retry never risks a second live ping that could have landed (the real
  `DiscordCodeAlertPoster`, a channel that lands the message and raises anyway, and
  one that refuses with a 429);
- huge urls and source names never strand claimed rows.

Nothing here talks to Discord; the clock only moves when a test moves it.
"""

from __future__ import annotations

import dataclasses
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import discord
import pytest
from test_shift_fanout import (
    CODE_A,
    CODE_B,
    CODE_C,
    NOW,
    Harness,
    add_guild,
    item,
    post_status,
    rows,
    seed,
    shift_row,
)

from newsbot.bot.client import DiscordCodeAlertPoster
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.decide import CodeCandidate, plan_alerts
from newsbot.shift.fanout import detect_and_fan_out
from newsbot.store.db import connect, migrate

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def h(db_path):
    harness = Harness(db_path, load_config(V3), NOW)
    seed(db_path)
    return harness


def with_shift(h, **updates):
    h.cfg = h.cfg.model_copy(update={"shift": h.cfg.shift.model_copy(update=updates)})


def many(n: int, prefix: str = "Z") -> list[str]:
    return [f"{prefix}{i:04d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(n)]


# --- who a batch pings (v2: test_shift_sweep.py) ---


async def test_a_press_only_batch_pings(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run([item(CODE_A, trust="press")])

    (alert,) = h.sent(1)
    assert alert.ping and alert.content.startswith("@everyone ")
    assert shift_row(h.db_path, 1).ping_count == 1


async def test_a_trusted_and_a_community_code_make_one_message_with_one_ping(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run([item(CODE_A, trust="community"), item(CODE_B, trust="official")])

    (alert,) = h.sent(1)
    assert alert.ping and CODE_A in alert.content and CODE_B in alert.content
    assert shift_row(h.db_path, 1).ping_count == 1  # one ping for the batch, not one per code


async def test_trusted_codes_lead_the_batch_so_the_ping_message_carries_one(h):
    add_guild(h.db_path, 1, ping="everyone")

    # The community code was seen first; the trusted one must still come first in the message.
    await h.run([item(CODE_A, trust="community"), item(CODE_B, trust="official")])

    (alert,) = h.sent(1)
    assert alert.content.index(CODE_B) < alert.content.index(CODE_A)
    assert list(alert.codes) == [CODE_B, CODE_A]


# --- roundups (v2: test_shift_sweep.py and test_shift_sweep_roundup_adversarial.py) ---


async def test_the_roundup_threshold_is_configurable(h):
    add_guild(h.db_path, 1, ping="everyone")
    with_shift(h, max_codes_per_item=2)

    await h.run([item(CODE_A, CODE_B, CODE_C)])  # three codes in one item, over a limit of two

    (alert,) = h.sent(1)
    assert not alert.ping  # a roundup is never pinged
    assert rows(h.db_path, "SELECT from_roundup FROM alerted_codes") == [(1,), (1,), (1,)]
    assert shift_row(h.db_path, 1).ping_count == 0


async def test_the_same_code_in_two_roundup_items_posts_once(h):
    add_guild(h.db_path, 1)
    shared = "SHAR3-AAAAA-AAAAA-AAAAA-AAAAA"
    item_a = item(*many(6, "A"), shared, url="https://example.com/roundup-a")
    item_b = item(*many(6, "B"), shared, url="https://example.com/roundup-b")

    await h.run([item_a, item_b])

    carried = [c for alert in h.sent(1) for c in alert.codes]
    assert carried.count(shared) == 1  # exactly one message carries it
    assert len(carried) == 13  # each filler set plus the shared code, once
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE code = ?", shared) == [(1,)]


async def test_a_roundup_batch_that_fails_for_good_is_marked_failed_and_never_pings(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [RuntimeError("the channel is gone")]  # not a PublishError: final at once
    codes = many(6)

    await h.run([item(*codes, url="https://example.com/roundup")])

    assert h.sent(1) == []  # nothing landed
    assert set(post_status(h.db_path, 1).values()) == {"failed"}
    assert rows(h.db_path, "SELECT DISTINCT pinged FROM guild_code_posts") == [(0,)]
    assert any("couldn't be posted" in text for _g, text in h.notices)
    assert not any("@everyone" in text for _g, text in h.notices)


async def test_a_roundup_batch_that_fails_once_then_posts_unpinged_after_the_retry(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [PublishError("blip")]  # the first attempt fails, the retry succeeds
    codes = many(6)

    await h.run([item(*codes, url="https://example.com/roundup")])

    (alert,) = h.sent(1)
    assert alert.ping is False
    assert set(post_status(h.db_path, 1).values()) == {"posted"}
    assert rows(h.db_path, "SELECT DISTINCT pinged FROM guild_code_posts") == [(0,)]
    assert h.notices == []


async def test_a_roundup_next_to_a_capped_normal_batch_never_touches_the_ping_count(h):
    add_guild(h.db_path, 1, ping="everyone")
    with_shift(h, max_pings_per_day=0)  # the budget is spent from the first code onward
    normal_code = "N0RML-AAAAA-AAAAA-AAAAA-AAAAA"

    await h.run([item(normal_code), item(*many(6, "R"), url="https://example.com/roundup")])

    alerts = h.sent(1)
    assert len(alerts) == 2 and not any(a.ping for a in alerts)
    assert normal_code in alerts[0].content  # still posts, just without a ping
    assert shift_row(h.db_path, 1).ping_count == 0  # neither batch spent it


def _roundup_candidate(code: str) -> CodeCandidate:
    return CodeCandidate(
        code=code,
        golden=False,
        source_name="Reddit Megathread",
        item_url="https://example.com/roundup",
        fresh=True,
        trusted=True,
        roundup=True,
    )


def test_exactly_fifty_fresh_roundup_codes_all_post_with_no_overflow():
    plan = plan_alerts(
        [_roundup_candidate(c) for c in many(50)],
        known=set(),
        seeded=True,
        seeding_ok=True,
        pings_today=0,
        max_pings=3,
    )
    assert len(plan.roundup_to_post) == 50
    assert plan.silent == []


def test_exactly_fifty_one_fresh_roundup_codes_cap_at_fifty_with_one_silent():
    candidates = [_roundup_candidate(c) for c in many(51)]
    plan = plan_alerts(
        candidates, known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert len(plan.roundup_to_post) == 50
    assert plan.silent == [(candidates[50], "roundup")]


async def test_fifty_one_roundup_codes_post_fifty_and_the_owner_hears_once(h):
    add_guild(h.db_path, 1)
    owner: list[str] = []

    async def alert_owner(text: str) -> None:
        owner.append(text)

    deps = h.deps()
    deps.alert_owner = alert_owner

    released = await detect_and_fan_out(
        deps, [item(*many(51), url="https://example.com/big-roundup")], seeding_ok=True
    )

    assert released == 50
    assert sum(len(a.codes) for a in h.posters[1].sent) == 50
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE status = 'roundup'") == [(1,)]
    cap_alerts = [t for t in owner if "roundup" in t.lower() and "cap" in t.lower()]
    assert len(cap_alerts) == 1 and "1" in cap_alerts[0]


async def test_a_stale_roundup_only_code_is_recorded_too_old_with_the_roundup_flag(h):
    add_guild(h.db_path, 1)
    stale = item(
        *many(6), url="https://example.com/stale-roundup", published=NOW - timedelta(hours=49)
    )

    await h.run([stale])

    assert h.sent(1) == []
    assert rows(h.db_path, "SELECT DISTINCT status, from_roundup FROM alerted_codes") == [
        ("too_old", 1)
    ]


async def test_a_stale_roundup_code_never_alerts_even_on_a_later_fresh_sighting(h):
    add_guild(h.db_path, 1)
    codes = many(6)
    stale = item(
        *codes, url="https://example.com/stale-roundup", published=NOW - timedelta(hours=49)
    )
    await h.run([stale])

    released = await h.run([item(*codes, url="https://example.com/fresh-roundup-again")])

    assert released == 0
    assert h.sent(1) == []  # A11: a code recorded too old never gets another chance


async def test_a_huge_url_and_source_name_never_strand_claimed_rows_pending(h):
    # `_roundup_header` once couldn't shrink, so a ~2010 character url plus a very long source
    # name could blow the 2000 unit cap and raise *after* the claim had marked the rows pending,
    # stranding them with nothing posted. Render first, claim second, and it just works.
    add_guild(h.db_path, 1)
    huge = item(*many(6), url="https://example.com/" + "a" * 2010)
    huge = dataclasses.replace(huge, source_name="Reddit Megathread " * 40)

    await h.run([huge])

    assert sum(len(a.codes) for a in h.sent(1)) == 6
    assert set(post_status(h.db_path, 1).values()) == {"posted"}
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE status = 'pending'") == [
        (0,)
    ]


# --- a retry never risks a second live ping that could have landed (v2: ping_retry tests) ---


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _FakePermissions:
    mention_everyone = True


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers = {}
        self.request_info = None


class _SucceedsButRaisesChannel:
    """`send()` "lands" the message (it's recorded) but still raises `fail_times` times.

    Simulating a send whose HTTP response never made it back: to the retry loop that looks
    exactly like a failure worth retrying, which is the ambiguous case nonce dedup (and the
    ping-stripping belt and suspenders) exist for.
    """

    def __init__(self, *, fail_times: int) -> None:
        self.guild = type("G", (), {"me": object()})()
        self.fail_times = fail_times
        self.calls = 0
        self.sent: list[tuple[str, discord.AllowedMentions, str | None]] = []

    def permissions_for(self, member: object) -> _FakePermissions:
        return _FakePermissions()

    async def send(self, content, *, allowed_mentions, nonce=None):
        self.calls += 1
        self.sent.append((content, allowed_mentions, nonce))
        if self.calls <= self.fail_times:
            raise discord.HTTPException(_FakeResponse(503), "unavailable")
        return _FakeMessage(1000 + self.calls)


class _FakeClient:
    def __init__(self, channel) -> None:
        self._channel = channel

    def get_channel(self, channel_id: int):
        return self._channel

    async def fetch_channel(self, channel_id: int):
        return self._channel


def use_the_real_poster(h, channel) -> None:
    client = _FakeClient(channel)
    h.factory = lambda gid, cid, ping, notify: DiscordCodeAlertPoster(client, cid, ping, notify)


async def test_a_retry_after_a_send_that_landed_but_errored_carries_the_ping_at_most_once(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = _SucceedsButRaisesChannel(fail_times=2)  # fails twice, succeeds on the third
    use_the_real_poster(h, channel)

    await h.run([item(CODE_A)])

    assert channel.calls == 3  # the first attempt plus two retries
    # Every attempt reused the same nonce: Discord's own dedup can catch a message that
    # actually landed on an earlier attempt that errored out on our side.
    nonces = {n for _content, _mentions, n in channel.sent}
    assert len(nonces) == 1 and nonces.pop()
    # Only the first attempt could ever have carried a live ping; every retry went out with
    # AllowedMentions.none() and no literal "@everyone " prefix, whether or not the first landed.
    first_content, first_mentions, _ = channel.sent[0]
    assert first_mentions.to_dict() == {"parse": ["everyone"]}
    assert first_content.startswith("@everyone ")
    for content, mentions, _ in channel.sent[1:]:
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()
        assert not content.startswith("@everyone ")
    assert set(post_status(h.db_path, 1).values()) == {"posted"}
    assert shift_row(h.db_path, 1).ping_count == 1  # the budget was spent once, at claim time


async def test_a_retry_never_sends_more_than_one_ping_even_when_every_attempt_fails(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = _SucceedsButRaisesChannel(fail_times=999)  # never succeeds
    use_the_real_poster(h, channel)

    await h.run([item(CODE_A)])

    assert set(post_status(h.db_path, 1).values()) == {"failed"}
    ping_bearing = [m for _c, m, _n in channel.sent if m.to_dict() == {"parse": ["everyone"]}]
    assert len(ping_bearing) <= 1
    assert h.notices and "couldn't be posted" in h.notices[0][1]


# The precise invariant (owner decision D13): at most one ping-bearing send that could have
# landed. A 429 is Discord refusing the message, so nobody was pinged and a retry keeps the
# ping; a run of 429s can therefore make several ping-bearing *attempts*. Any ambiguous
# failure (timeout, 5xx, connection error) might have landed and pinged, so every attempt
# after it goes out without one, even if a later failure is a 429.


class _ScriptedChannel:
    """`send()` follows `script`, one outcome per attempt: "429", "503", "timeout" or "ok".

    Only "503" and "timeout" model a send that may have landed (the response got lost); a
    "429" never lands. `maybe_landed` records the mentions of each send that could have
    delivered, the thing the invariant is about.
    """

    def __init__(self, script: list[str]) -> None:
        self.guild = type("G", (), {"me": object()})()
        self.script = script
        self.sent: list[tuple[str, discord.AllowedMentions, str | None]] = []
        self.maybe_landed: list[discord.AllowedMentions] = []

    def permissions_for(self, member: object) -> _FakePermissions:
        return _FakePermissions()

    async def send(self, content, *, allowed_mentions, nonce=None):
        outcome = self.script[len(self.sent)] if len(self.sent) < len(self.script) else "ok"
        self.sent.append((content, allowed_mentions, nonce))
        if outcome == "429":
            raise discord.HTTPException(_FakeResponse(429), "rate limited")
        self.maybe_landed.append(allowed_mentions)
        if outcome == "503":
            raise discord.HTTPException(_FakeResponse(503), "unavailable")
        if outcome == "timeout":
            raise TimeoutError("no response")
        return _FakeMessage(2000 + len(self.sent))


def _pings(channel: _ScriptedChannel) -> list[bool]:
    """Per attempt: did that send carry a live ping?"""
    return [m.to_dict() == {"parse": ["everyone"]} for _c, m, _n in channel.sent]


@pytest.mark.parametrize(
    ("script", "attempt_pings"),
    [
        (["429", "ok"], [True, True]),
        (["429", "429", "ok"], [True, True, True]),
        (["timeout", "429", "ok"], [True, False, False]),
        (["503", "429", "ok"], [True, False, False]),
        (["429", "timeout", "ok"], [True, True, False]),
        (["429", "503", "429", "ok"], [True, True, False, False]),
    ],
)
async def test_a_429_keeps_the_ping_and_an_ambiguous_failure_ends_it(h, script, attempt_pings):
    add_guild(h.db_path, 1, ping="everyone")
    channel = _ScriptedChannel(script)
    use_the_real_poster(h, channel)

    await h.run([item(CODE_A)])

    assert _pings(channel) == attempt_pings
    # Same nonce on every attempt, and the ping budget claimed once however many retries ran.
    nonces = {n for _c, _m, n in channel.sent}
    assert len(nonces) == 1 and nonces.pop()
    # The invariant: at most one ping-bearing send that could have landed.
    landed_pings = [m.to_dict() == {"parse": ["everyone"]} for m in channel.maybe_landed]
    assert sum(landed_pings) <= 1
    # Content and mentions agree on every attempt: the "@everyone " prefix goes with the ping.
    for (content, _m, _n), pinged in zip(channel.sent, attempt_pings, strict=True):
        assert content.startswith("@everyone ") == pinged
    assert set(post_status(h.db_path, 1).values()) == {"posted"}
    assert shift_row(h.db_path, 1).ping_count == 1
    assert rows(h.db_path, "SELECT DISTINCT pinged FROM guild_code_posts") == [(1,)]


async def test_retries_exhausted_on_429s_fail_as_before_without_double_counting(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = _ScriptedChannel(["429"] * 999)
    use_the_real_poster(h, channel)

    await h.run([item(CODE_A)])

    assert len(channel.sent) == 4  # the first attempt plus the three backoff steps
    assert _pings(channel) == [True] * 4  # all refused, so the ping was never lost or spent
    assert channel.maybe_landed == []  # nothing landed, nobody was pinged
    assert set(post_status(h.db_path, 1).values()) == {"failed"}
    assert shift_row(h.db_path, 1).ping_count == 1  # claimed once, never again
    assert h.notices and "couldn't be posted" in h.notices[0][1]
