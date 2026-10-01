"""Adversarial tests for the SHiFT fan-out (plan task 5): the part that pings strangers.

v2 woke up one server, and the one server was a group of friends who'd
forgive a misfire. v3 wakes up everybody who installs the thing, and a
stranger whose phone buzzes at 3 a.m. because of my bug does not send a nice
email. So most of this file is one question asked many ways: can a guild that
said "no pings" (or "just this role") ever be handed a live `@everyone`, on
any path, including the ugly ones (retries, capped batches, half-failed
batches, hostile settings values)? The rest attacks the cap under
concurrency, local days at the DST seams, once-per-code-per-guild, one
guild's trouble leaking into another's, and the hook timeout, which turns out
to be a real design limit rather than a test problem.

The ping checks use the real `DiscordCodeAlertPoster` against a fake channel,
so what gets asserted is what would actually reach Discord: message text plus
the `AllowedMentions` wire format. Everything else uses scripted fakes and a
real temp database with the real migrations. No network, and the pace sleep is
patched out.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
import pytest

from newsbot.bot.client import _PING_EVERYONE, DiscordCodeAlertPoster, mentions_for
from newsbot.bot.format import RenderedAlert, render_code_alerts
from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.pipeline.collect import _SHIFT_HOOK_TIMEOUT_S
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.fanout import (
    _GUILD_PACE_S,
    _PENDING_STALE_S,
    FanoutDeps,
    _release,
    deliver_queued_codes,
    detect_and_fan_out,
    recover_pending_guild_codes,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)
LONG_AGO = datetime(2027, 1, 1, tzinfo=UTC)

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
CODE_C = "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"
ROLE = "1234567890123456789"
OTHER_ROLE = "9876543210987654321"


def _code(i: int) -> str:
    return f"K{i:04d}-ABCDE-FGHIJ-KLMNO-PQRST"


async def _no_sleep(_seconds: float) -> None:
    return None


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers = {}
        self.request_info = None


def _http(status: int) -> discord.HTTPException:
    return discord.HTTPException(_FakeResponse(status), "boom")


# --- fakes: a scripted poster, and a fake channel under the real poster ---


class FakePoster:
    def __init__(self, guild_id, channel_id, ping, *, fail=None, on_post=None):
        self.guild_id = guild_id
        self.ping = ping
        self.fail = list(fail or [])
        self.sent: list[RenderedAlert] = []
        self.on_post = on_post

    def begin_batch(self):
        return None

    async def post(self, alert):
        if self.on_post is not None:
            self.on_post()
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(alert)
        return 9000 + len(self.sent)


class FakeRole:
    def __init__(self, mentionable):
        self.mentionable = mentionable


class FakePermissions:
    def __init__(self, mention_everyone):
        self.mention_everyone = mention_everyone


class FakeGuild:
    def __init__(self, roles):
        self.me = object()
        self._roles = roles

    def get_role(self, role_id):
        return self._roles.get(role_id)


class FakeChannel:
    def __init__(self, *, roles=None, can_mention_everyone=False, script=None):
        self.guild = FakeGuild(roles or {})
        self._can = can_mention_everyone
        self.script = list(script or [])  # exceptions raised by successive sends
        self.sent: list[tuple[str, dict]] = []

    def permissions_for(self, _member):
        return FakePermissions(self._can)

    async def send(self, content, *, allowed_mentions, nonce=None):
        if self.script:
            raise self.script.pop(0)
        self.sent.append((content, allowed_mentions.to_dict()))
        return type("Msg", (), {"id": len(self.sent)})()


class FakeClient:
    def __init__(self, channels):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise _http(404)

    async def alert(self, text):
        raise AssertionError(f"the owner's channel must never hear about a guild: {text}")


class Harness:
    """Deps plus everything the fakes saw; `channels` switches on the real poster."""

    def __init__(self, db_path, cfg, now):
        self.db_path = db_path
        self.cfg = cfg
        self.clock = now
        self.posters: dict[int, FakePoster] = {}
        self.fail_with: dict[int, list[Exception]] = {}
        self.channels: dict[int, FakeChannel] | None = None
        self.notices: list[tuple[int, str]] = []
        self.sleeps: list[float] = []
        self.sleep = _no_sleep
        self.notify_raises: set[int] = set()
        self.owner_alerts: list[str] = []

    def factory(self, guild_id, channel_id, ping, notify):
        if self.channels is not None:
            client = FakeClient({gid * 100: ch for gid, ch in self.channels.items()})
            return DiscordCodeAlertPoster(client, channel_id, ping, notify)
        poster = FakePoster(guild_id, channel_id, ping, fail=self.fail_with.get(guild_id))
        self.posters[guild_id] = poster
        return poster

    async def notify_guild(self, guild_id, text):
        self.notices.append((guild_id, text))
        if guild_id in self.notify_raises:
            raise RuntimeError("notice channel is gone too")

    async def alert_owner(self, text):
        self.owner_alerts.append(text)

    def deps(self, *, now=None):
        return FanoutDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            now=now or (lambda: self.clock),
            poster_for=self.factory,
            notify_guild=self.notify_guild,
            sleep=self.sleep,
            alert_owner=self.alert_owner,
        )

    async def deliver(self, *, startup=False):
        """Just the delivery step: what a later pass (or startup) does with the queue."""
        self.posters.clear()
        return await deliver_queued_codes(self.deps(), startup=startup)

    async def run(self, items, *, seeding_ok=True):
        self.posters.clear()
        return await detect_and_fan_out(self.deps(), items, seeding_ok=seeding_ok)

    def sent(self, guild_id) -> list[RenderedAlert]:
        poster = self.posters.get(guild_id)
        return poster.sent if poster else []


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def cfg():
    return load_config(V3)


def _with_shift(cfg, **updates):
    return cfg.model_copy(update={"shift": cfg.shift.model_copy(update=updates)})


def seed(db_path):
    with closing(connect(db_path)) as conn:
        repo.record_silent_codes(conn, [], now=lambda: EARLIER, mark_seeded=True)


@pytest.fixture
def h(db_path, cfg):
    harness = Harness(db_path, cfg, NOW)
    seed(db_path)
    return harness


def add_guild(
    db_path,
    guild_id,
    *,
    ping="none",
    tz="UTC",
    games=("borderlands4",),
    enabled_at=EARLIER,
    ping_day=None,
    ping_count=0,
):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, guild_id, timezone=tz, set_up=True, now=lambda: EARLIER)
        for game in games:
            repo.follow_game(conn, guild_id, game, 10)
        repo.set_shift(
            conn,
            guild_id,
            enabled=True,
            channel_id=guild_id * 100,
            ping=ping,
            now=lambda: enabled_at,
        )
        with conn:
            conn.execute(
                "UPDATE guild_shift SET ping_day = ?, ping_count = ? WHERE guild_id = ?",
                (ping_day, ping_count, guild_id),
            )


def item(*codes, trust="official", published=None, url=None, title=None):
    return RawItem(
        url=url or f"https://example.com/{'-'.join(c[:5] for c in codes)}",
        title=title or "Borderlands 4 new SHiFT code: " + " ".join(codes),
        excerpt="",
        source_name="Gearbox Blog",
        trust=trust,
        published_at=published or NOW - timedelta(hours=1),
    )


def rows(db_path, sql, *args):
    with closing(connect(db_path)) as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def shift_row(db_path, guild_id):
    with closing(connect(db_path)) as conn:
        return repo.get_shift(conn, guild_id)


def post_status(db_path, guild_id):
    return dict(
        rows(db_path, "SELECT code, status FROM guild_code_posts WHERE guild_id = ?", guild_id)
    )


def assert_no_live_mention(channel: FakeChannel):
    """Every message this channel got: no mention text, and nothing the wire would honor."""
    assert channel.sent, "expected at least one message to inspect"
    for content, wire in channel.sent:
        assert "@everyone" not in content and "@here" not in content
        assert "<@" not in content
        assert wire.get("parse", []) == [] and "roles" not in wire and "users" not in wire


# --- ping safety: a guild that said none never hears a mention ---


def _scenario_trusted_batch(h):
    return [item(CODE_A, CODE_B)], {}


def _scenario_untrusted(h):
    return [item(CODE_A, trust="community")], {}


def _scenario_roundup(h):
    many = [_code(i) for i in range(8)]
    return [item(*many, url="https://example.com/mega")], {}


def _scenario_retry_then_ok(h):
    return [item(CODE_A)], {"script": [_http(503), _http(502)]}


def _scenario_long_batch_tail_fails(h):
    items = [item(_code(i), url=f"https://example.com/{i}") for i in range(40)]
    return items, {"script_after_first": True}


@pytest.mark.parametrize(
    "scenario",
    [
        _scenario_trusted_batch,
        _scenario_untrusted,
        _scenario_roundup,
        _scenario_retry_then_ok,
        _scenario_long_batch_tail_fails,
    ],
)
async def test_none_guild_never_gets_a_live_mention_on_any_path(h, scenario):
    add_guild(h.db_path, 1, ping="none")
    items, opts = scenario(h)
    channel = FakeChannel(can_mention_everyone=True, script=opts.get("script"))
    if opts.get("script_after_first"):
        original = channel.send

        async def flaky(content, *, allowed_mentions, nonce=None):
            if channel.sent:
                raise _http(403)
            return await original(content, allowed_mentions=allowed_mentions, nonce=nonce)

        channel.send = flaky
    h.channels = {1: channel}

    await h.run(items)

    assert_no_live_mention(channel)


async def test_everyone_guild_continuations_and_roundups_carry_no_mention(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = FakeChannel(can_mention_everyone=True)
    h.channels = {1: channel}
    items = [item(_code(i), url=f"https://example.com/{i}") for i in range(40)]
    items.append(item(*[_code(100 + i) for i in range(8)], url="https://example.com/mega"))

    await h.run(items)

    assert len(channel.sent) > 2
    first_content, first_wire = channel.sent[0]
    assert first_content.startswith("@everyone ") and first_wire["parse"] == ["everyone"]
    for content, wire in channel.sent[1:]:
        assert "@everyone" not in content
        assert wire.get("parse", []) == [] and "roles" not in wire


async def test_role_guild_never_gets_everyone_and_mentions_only_that_role(h):
    add_guild(h.db_path, 1, ping=ROLE)
    channel = FakeChannel(roles={int(ROLE): FakeRole(mentionable=True)})
    h.channels = {1: channel}
    items = [item(_code(i), url=f"https://example.com/{i}") for i in range(40)]

    await h.run(items)

    assert len(channel.sent) > 1
    content, wire = channel.sent[0]
    assert content.startswith(f"<@&{ROLE}> ")
    assert wire["parse"] == [] and wire["roles"] == [int(ROLE)]
    for content, wire in channel.sent:
        assert "@everyone" not in content and "@here" not in content
        assert "everyone" not in wire.get("parse", [])
    for content, wire in channel.sent[1:]:
        assert "<@" not in content and "roles" not in wire


async def test_capped_role_guild_posts_unpinged_with_no_role_text_or_mentions(h):
    add_guild(h.db_path, 1, ping=ROLE, ping_day="2026-10-01", ping_count=3)
    channel = FakeChannel(roles={int(ROLE): FakeRole(mentionable=True)})
    h.channels = {1: channel}

    await h.run([item(CODE_A, CODE_B)])

    assert_no_live_mention(channel)


async def test_retry_after_a_role_ping_failure_resends_without_prefix_or_mentions(h):
    add_guild(h.db_path, 1, ping=ROLE)
    channel = FakeChannel(roles={int(ROLE): FakeRole(mentionable=True)}, script=[_http(503)])
    h.channels = {1: channel}

    await h.run([item(CODE_A)])

    (content, wire) = channel.sent[0]  # the failed first attempt never landed
    assert "<@" not in content and "roles" not in wire and wire.get("parse", []) == []
    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


async def test_everyone_guild_failing_after_first_message_sends_no_second_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = FakeChannel(can_mention_everyone=True)
    original = channel.send

    async def dies_second(content, *, allowed_mentions, nonce=None):
        if channel.sent:
            raise _http(403)
        return await original(content, allowed_mentions=allowed_mentions, nonce=nonce)

    channel.send = dies_second
    h.channels = {1: channel}

    await h.run([item(_code(i), url=f"https://example.com/{i}") for i in range(40)])

    assert len(channel.sent) == 1
    assert channel.sent[0][0].startswith("@everyone ")


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param({}, id="role deleted or from another guild (get_role is None)"),
        pytest.param({int(ROLE): FakeRole(mentionable=False)}, id="role not mentionable"),
    ],
)
async def test_unpingable_role_still_posts_mentions_only_that_role_and_tells_that_guild(h, roles):
    add_guild(h.db_path, 1, ping=ROLE)
    add_guild(h.db_path, 2, ping="none")
    h.channels = {1: FakeChannel(roles=roles), 2: FakeChannel()}

    await h.run([item(_code(i), url=f"https://example.com/{i}") for i in range(40)])

    (content, wire) = h.channels[1].sent[0]
    assert wire["parse"] == [] and wire["roles"] == [int(ROLE)]
    assert [g for g, _ in h.notices] == [1]  # once per batch, to that guild only
    assert_no_live_mention(h.channels[2])


async def test_a_roleid_belonging_to_another_guild_cannot_reach_beyond_its_own_entry(h):
    # Guild 1 is configured with guild 2's role. Its channel can't resolve it, so it
    # posts with that one role id allowed (Discord ignores a foreign role) and never widens.
    add_guild(h.db_path, 1, ping=OTHER_ROLE)
    add_guild(h.db_path, 2, ping=ROLE)
    h.channels = {
        1: FakeChannel(roles={int(ROLE): FakeRole(True)}),
        2: FakeChannel(roles={int(ROLE): FakeRole(True)}),
    }

    await h.run([item(CODE_A)])

    _, wire1 = h.channels[1].sent[0]
    assert wire1["roles"] == [int(OTHER_ROLE)] and "everyone" not in wire1["parse"]
    _, wire2 = h.channels[2].sent[0]
    assert wire2["roles"] == [int(ROLE)]


async def test_budget_denied_rerender_removes_the_prefix_from_every_message(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=3)
    add_guild(h.db_path, 2, ping=ROLE, ping_day="2026-10-01", ping_count=3)

    await h.run([item(_code(i), url=f"https://example.com/{i}") for i in range(40)])

    for gid in (1, 2):
        alerts = h.sent(gid)
        assert len(alerts) > 1
        for a in alerts:
            assert a.ping_prefix == "" and not a.ping
            assert "@everyone" not in a.content and "<@&" not in a.content


# --- mentions_for under hostile values ---


@pytest.mark.parametrize(
    "value",
    ["everyone ", " everyone", "Everyone", "EVERYONE", "@everyone", "@here", "here", "", "0",
     "-5", "+5", "1.5", "1e9", "0x10", "none", "None", "12 34", "<@&123>", "5\n", "١٢"],
)  # fmt: skip
def test_only_the_exact_word_everyone_is_ever_the_everyone_object(value):
    result = mentions_for(value)
    assert result is not _PING_EVERYONE
    assert "everyone" not in result.to_dict().get("parse", [])


def test_the_exact_word_returns_the_shared_everyone_object():
    assert mentions_for("everyone") is _PING_EVERYONE


@pytest.mark.parametrize("value", [None, "", "0", "-5", "none", "garbage", "0" * 25])
def test_bad_values_ping_nobody(value):
    assert mentions_for(value).to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.parametrize("digits", [17, 18, 19, 20])
def test_snowflake_length_digit_strings_are_roles_never_everyone(digits):
    wire = mentions_for("1" * digits).to_dict()
    assert wire["parse"] == [] and wire["roles"] == [int("1" * digits)]


@pytest.mark.parametrize("digits", [1, 16, 21, 25])
def test_digit_strings_that_cannot_be_snowflakes_ping_nobody(digits):
    # Changed with the fail-closed fix: a 21-digit string used to be a role (any run of
    # digits was); only 17 to 20 ASCII digits are now, and the rest ping nobody.
    assert mentions_for("1" * digits).to_dict() == discord.AllowedMentions.none().to_dict()


def test_a_trailing_newline_is_not_a_role():
    assert mentions_for(ROLE + "\n").to_dict() == discord.AllowedMentions.none().to_dict()


def test_a_str_subclass_is_not_trusted_as_a_role_or_as_everyone():
    class Sneaky(str):
        pass

    assert mentions_for(Sneaky("everyone")) is not _PING_EVERYONE
    assert mentions_for(Sneaky(ROLE)).to_dict() == discord.AllowedMentions.none().to_dict()


def test_fullwidth_digits_never_become_everyone():
    assert "everyone" not in mentions_for("９９").to_dict().get("parse", [])


def test_superscript_digit_fails_closed_instead_of_raising():
    assert mentions_for("²").to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.parametrize("value", [123, True, 1.5])
def test_non_string_values_fail_closed_instead_of_raising(value):
    assert mentions_for(value).to_dict() == discord.AllowedMentions.none().to_dict()


# --- the cap under concurrency ---


def _cap_cfg(cfg, n):
    return _with_shift(cfg, max_pings_per_day=n)


async def test_two_overlapping_passes_never_exceed_the_cap(db_path, cfg):
    cfg = _cap_cfg(cfg, 1)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone")
    one, two = Harness(db_path, cfg, NOW), Harness(db_path, cfg, NOW)

    await asyncio.gather(one.run([item(CODE_A)]), two.run([item(CODE_B)]))

    # Changed with the delivery queue: each pass delivers *every* queued row, so one
    # pass may carry both codes in a single message and the other finds nothing left.
    # What must hold is unchanged: both codes go out exactly once, one ping total.
    alerts = one.sent(1) + two.sent(1)
    assert sorted(c for a in alerts for c in a.codes) == [CODE_A, CODE_B]
    assert sum(a.ping for a in alerts) == 1
    assert shift_row(db_path, 1).ping_count == 1
    assert post_status(db_path, 1) == {CODE_A: "posted", CODE_B: "posted"}


async def test_two_overlapping_passes_with_the_same_code_post_it_once_per_guild(db_path, cfg):
    seed(db_path)
    for gid in (1, 2, 3):
        add_guild(db_path, gid, ping="everyone")
    one, two = Harness(db_path, cfg, NOW), Harness(db_path, cfg, NOW)

    released = await asyncio.gather(one.run([item(CODE_A)]), two.run([item(CODE_A)]))

    assert sorted(released) == [0, 1]  # one writer won the code, the other released nothing
    for gid in (1, 2, 3):
        assert len(one.sent(gid)) + len(two.sent(gid)) == 1
    assert rows(db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(3,)]
    assert rows(db_path, "SELECT COUNT(*) FROM alerted_codes") == [(1,)]
    assert one.notices == [] and two.notices == []


def test_many_threads_claiming_for_one_guild_spend_exactly_the_cap(db_path):
    add_guild(db_path, 1, ping="everyone")
    results: list[bool] = []
    lock = threading.Lock()
    with closing(connect(db_path)) as conn:
        # Queued at release: a claim moves `queued` rows, so the guild needs some.
        repo.record_released_codes(
            conn,
            [(_code(i), "s", "https://e.com", False) for i in range(8)],
            now=lambda: NOW,
            queue_for_games=[],
        )
    barrier = threading.Barrier(8)

    def worker(i):
        with closing(connect(db_path)) as conn:
            barrier.wait()
            got = repo.claim_guild_codes(
                conn, 1, [_code(i)], pinged=True, local_day="2026-10-01", max_pings=3
            )
        with lock:
            results.append(got.pinged)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 3
    assert shift_row(db_path, 1).ping_count == 3
    assert rows(db_path, "SELECT SUM(pinged), COUNT(*) FROM guild_code_posts") == [(3, 8)]


# --- local days, at the seams ---


async def _ping_at(h, when, code):
    h.clock = when
    h.posters.clear()
    await detect_and_fan_out(
        h.deps(), [item(code, published=when - timedelta(hours=1))], seeding_ok=True
    )
    return h.sent(1)[-1].ping


async def test_cap_holds_across_the_spring_forward_day_and_resets_at_local_midnight(db_path, cfg):
    h = Harness(db_path, _cap_cfg(cfg, 1), LONG_AGO)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone", tz="America/Los_Angeles", enabled_at=LONG_AGO)
    # 2027-03-14: the clocks jump at 10:00Z, so the local day is 23 hours long,
    # 08:00Z to 07:00Z next morning.
    assert await _ping_at(h, datetime(2027, 3, 14, 8, 30, tzinfo=UTC), CODE_A)
    assert not await _ping_at(h, datetime(2027, 3, 15, 6, 59, tzinfo=UTC), CODE_B)
    assert await _ping_at(h, datetime(2027, 3, 15, 7, 0, tzinfo=UTC), CODE_C)


async def test_cap_holds_across_the_25_hour_fall_back_day(db_path, cfg):
    h = Harness(db_path, _cap_cfg(cfg, 1), LONG_AGO)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone", tz="America/Los_Angeles", enabled_at=LONG_AGO)
    # 2027-11-07: 07:00Z to 08:00Z next day is one local day (25 hours).
    assert await _ping_at(h, datetime(2027, 11, 7, 7, 5, tzinfo=UTC), CODE_A)
    assert not await _ping_at(h, datetime(2027, 11, 8, 7, 59, tzinfo=UTC), CODE_B)
    assert await _ping_at(h, datetime(2027, 11, 8, 8, 0, tzinfo=UTC), CODE_C)


async def test_both_passes_inside_the_repeated_hour_share_one_day(db_path, cfg):
    h = Harness(db_path, _cap_cfg(cfg, 1), LONG_AGO)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone", tz="America/Los_Angeles", enabled_at=LONG_AGO)
    # 01:30 local happens twice on 2027-11-07 (08:30Z PDT, then 09:30Z PST).
    assert await _ping_at(h, datetime(2027, 11, 7, 8, 30, tzinfo=UTC), CODE_A)
    assert not await _ping_at(h, datetime(2027, 11, 7, 9, 30, tzinfo=UTC), CODE_B)


async def test_changing_timezone_mid_day_resets_the_budget_so_the_cap_can_be_dodged(h):
    # Documented behavior, not an endorsement: the budget is keyed by the local date
    # string, so moving a guild's zone to one where "today" has a different date
    # hands it a fresh budget inside the same UTC day. Needs an admin flipping
    # /settings back and forth, but it is a cap dodge.
    h.cfg = _cap_cfg(h.cfg, 1)
    add_guild(h.db_path, 1, ping="everyone", tz="UTC")
    first = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    assert await _ping_at(h, first, CODE_A)
    assert not await _ping_at(h, first + timedelta(minutes=30), CODE_B)

    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guilds SET timezone = 'America/Los_Angeles' WHERE guild_id = 1")

    assert await _ping_at(h, first + timedelta(hours=1), CODE_C)
    assert shift_row(h.db_path, 1).ping_count == 1


# --- once per code per guild ---


async def test_a_guild_enabled_exactly_at_the_pass_time_is_eligible(h):
    add_guild(h.db_path, 1, ping="none", enabled_at=NOW)

    await h.run([item(CODE_A)])

    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


async def test_code_published_before_enabling_but_first_seen_after_is_new_news(h):
    add_guild(h.db_path, 1, enabled_at=NOW - timedelta(minutes=5))

    await h.run([item(CODE_A, published=NOW - timedelta(hours=30))])

    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


async def test_a_code_released_while_no_guild_was_eligible_never_reaches_a_later_guild(h):
    assert await h.run([item(CODE_A)]) == 1  # recorded globally, nobody to tell
    assert rows(h.db_path, "SELECT status FROM alerted_codes") == [("posted",)]

    h.clock = NOW + timedelta(hours=1)
    add_guild(h.db_path, 1, enabled_at=EARLIER)  # enabled before the code, added after
    await h.run([item(CODE_A, published=h.clock)])

    assert post_status(h.db_path, 1) == {}


async def test_a_code_in_both_a_normal_item_and_a_roundup_posts_once_with_no_error(h):
    add_guild(h.db_path, 1, ping="none")
    mega = item(CODE_A, *[_code(i) for i in range(8)], url="https://example.com/mega")

    await h.run([item(CODE_A), mega])

    count = rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE code = ?", CODE_A)
    assert count == [(1,)]
    assert h.notices == []


async def test_a_failed_code_is_not_retried_by_a_later_pass(h):
    add_guild(h.db_path, 1, ping="none")
    h.fail_with[1] = [RuntimeError("no access")]
    await h.run([item(CODE_A)])
    assert post_status(h.db_path, 1) == {CODE_A: "failed"}

    h.fail_with.clear()
    await h.run([item(CODE_A)])

    assert h.sent(1) == []
    assert post_status(h.db_path, 1) == {CODE_A: "failed"}


async def test_recovery_flips_only_pending_rows_and_tells_only_that_guild(h):
    for gid in (1, 2):
        add_guild(h.db_path, gid, ping="none")
    await h.run([item(CODE_A)])  # both guilds: posted
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_B, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_B], pinged=False, local_day="2026-10-01")
    h.notices.clear()

    total = await recover_pending_guild_codes(h.db_path, h.notify_guild)

    assert total == 1
    assert [g for g, _ in h.notices] == [1] and CODE_B in h.notices[0][1]
    assert post_status(h.db_path, 1) == {CODE_A: "posted", CODE_B: "failed"}
    # Changed with the queue: guild 2 has CODE_B queued (recovery leaves queued rows alone).
    assert post_status(h.db_path, 2) == {CODE_A: "posted", CODE_B: "queued"}
    await h.run([item(CODE_B)])  # recovery never turns into a re-post
    # Changed with the queue: guild 2 never claimed CODE_B, it was queued at release, so
    # this pass delivers it there. Guild 1's recovered (failed) row is what stays unsent.
    assert h.sent(1) == []
    assert post_status(h.db_path, 1) == {CODE_A: "posted", CODE_B: "failed"}
    assert post_status(h.db_path, 2) == {CODE_A: "posted", CODE_B: "posted"}


async def test_recovery_with_nothing_pending_says_nothing_to_anyone(h):
    add_guild(h.db_path, 1)
    await h.run([item(CODE_A)])
    h.notices.clear()

    assert await recover_pending_guild_codes(h.db_path, h.notify_guild) == 0
    assert h.notices == []


async def test_a_guild_deleted_mid_fanout_does_not_stop_the_next_one(h):
    for gid in (1, 2, 3):
        add_guild(h.db_path, gid, ping="everyone")

    async def sleep(_s):
        # Runs before guild 2's turn (and again before guild 3's); delete 2 once.
        with closing(connect(h.db_path)) as conn:
            repo.delete_guild(conn, 2)

    h.sleep = sleep

    await h.run([item(CODE_A)])

    # Changed with the queue: guild 2's queued row went with it (the cascade), so its
    # turn finds nothing and there is no longer a failed attempt to tell anyone about.
    assert len(h.sent(1)) == 1 and len(h.sent(3)) == 1 and h.sent(2) == []
    assert post_status(h.db_path, 2) == {}
    assert h.notices == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE guild_id = 2") == [(0,)]


async def test_a_guild_disabled_between_release_and_delivery_is_skipped_and_not_posted(h):
    # Changed with the queue (was: a stale up-front read still posted to it): delivery
    # re-reads the guild's settings at its turn, so turning alerts off in the gap wins.
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")

    async def sleep(_s):
        with closing(connect(h.db_path)) as conn:
            repo.set_shift(conn, 2, enabled=False, channel_id=200, ping="none")

    h.sleep = sleep

    await h.run([item(CODE_A)])

    assert len(h.sent(1)) == 1 and h.sent(2) == []
    assert post_status(h.db_path, 2) == {CODE_A: "skipped"}
    assert h.notices == []


@pytest.mark.parametrize("change", ["no_channel", "unfollowed"])
async def test_a_guild_that_lost_its_channel_or_its_game_is_skipped(h, change):
    add_guild(h.db_path, 1)
    add_guild(h.db_path, 2)
    await _release_only(h, [item(CODE_A)])
    with closing(connect(h.db_path)) as conn:
        if change == "unfollowed":
            repo.unfollow_game(conn, 2, "borderlands4")
        else:
            with conn:
                conn.execute(
                    "UPDATE guild_shift SET channel_id = NULL, enabled = 0 WHERE guild_id = 2"
                )

    await h.deliver()

    assert len(h.sent(1)) == 1 and h.sent(2) == []
    assert post_status(h.db_path, 2) == {CODE_A: "skipped"}


async def test_a_guild_that_turned_alerts_off_and_on_again_gets_no_backlog(h):
    add_guild(h.db_path, 1)
    await _release_only(h, [item(CODE_A)])
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=False, channel_id=100, ping="none", now=lambda: NOW)
        repo.set_shift(
            conn, 1, enabled=True, channel_id=100, ping="none", now=lambda: NOW + timedelta(hours=1)
        )

    await h.deliver()

    assert h.sent(1) == []
    assert post_status(h.db_path, 1) == {CODE_A: "skipped"}


async def test_ping_choice_changed_to_none_before_delivery_posts_unpinged(h):
    # Changed with the queue (was: the stale read still pinged it): the guild's choice at
    # its turn is the one used, and no ping budget is spent on a ping that didn't happen.
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")

    async def sleep(_s):
        with closing(connect(h.db_path)) as conn:
            repo.set_shift(conn, 2, enabled=True, channel_id=200, ping="none")

    h.sleep = sleep

    await h.run([item(CODE_A)])

    assert h.sent(1)[0].ping is True
    assert h.sent(2)[0].ping is False and h.sent(2)[0].ping_prefix == ""
    assert post_status(h.db_path, 2) == {CODE_A: "posted"}
    assert shift_row(h.db_path, 2).ping_count == 0


async def test_a_ping_choice_changed_to_a_role_before_delivery_uses_the_role(h):
    add_guild(h.db_path, 1, ping="everyone")
    await _release_only(h, [item(CODE_A)])
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping=ROLE)

    await h.deliver()

    assert h.sent(1)[0].ping is True and f"<@&{ROLE}>" in h.sent(1)[0].content
    assert "@everyone" not in h.sent(1)[0].content


async def test_the_cap_at_delivery_time_is_the_one_in_force_then(h):
    add_guild(h.db_path, 1, ping="everyone")
    await _release_only(h, [item(CODE_A)])
    h.cfg = _cap_cfg(h.cfg, 0)  # owner lowered the cap between release and delivery

    await h.deliver()

    assert h.sent(1)[0].ping is False
    assert shift_row(h.db_path, 1).ping_count == 0


async def _release_only(h, items):
    """Release and queue codes without delivering (what a timeout before the walk leaves)."""
    h.posters.clear()
    await _release(h.deps(), items, seeding_ok=True)


# --- the delivery queue ---


async def test_release_queues_a_row_for_every_eligible_guild_in_the_same_pass(h):
    for gid in (1, 2, 3):
        add_guild(h.db_path, gid)
    add_guild(h.db_path, 4, enabled_at=NOW + timedelta(hours=1))  # not yet: no backlog rule

    await _release_only(h, [item(CODE_A)])

    assert rows(h.db_path, "SELECT guild_id, status FROM guild_code_posts ORDER BY guild_id") == [
        (1, "queued"),
        (2, "queued"),
        (3, "queued"),
    ]


async def test_a_pass_with_nothing_new_still_delivers_what_was_left_queued(h):
    add_guild(h.db_path, 1)
    await _release_only(h, [item(CODE_A)])
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}

    assert await h.run([]) == 0  # the next hourly pass found nothing new

    assert post_status(h.db_path, 1) == {CODE_A: "posted"}
    assert len(h.sent(1)) == 1


async def test_delivery_is_in_guild_id_order_with_the_pace_between_guilds(h):
    for gid in (3, 1, 2):
        add_guild(h.db_path, gid)
    order: list[int] = []
    real = h.factory

    def factory(gid, cid, ping, notify):
        order.append(gid)
        return real(gid, cid, ping, notify)

    h.factory = factory
    await h.run([item(CODE_A)])

    assert order == [1, 2, 3]


async def test_a_queued_code_reaches_a_later_pass_in_the_same_batch_as_new_ones(h):
    add_guild(h.db_path, 1)
    await _release_only(h, [item(CODE_A)])

    await h.run([item(CODE_B)])

    assert [c for a in h.sent(1) for c in a.codes] == [CODE_A, CODE_B]


class _Crash(BaseException):
    """What a dying process looks like from inside: nothing in `except Exception` sees it."""


async def test_a_crash_between_claim_and_send_never_double_sends(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [_Crash()]
    with pytest.raises(_Crash):
        await h.run([item(CODE_A)])
    assert post_status(h.db_path, 1) == {CODE_A: "pending"}  # claimed, never confirmed

    # A pass right after: the row is fresh, so it's a send that may still be in flight.
    h.fail_with.clear()
    await h.run([])
    assert h.sent(1) == [] and post_status(h.db_path, 1) == {CODE_A: "pending"}

    # Startup: nothing can be in flight, so it's failed, the guild is told, and it is
    # never sent. No later pass changes that.
    h.notices.clear()
    await h.deliver(startup=True)
    assert post_status(h.db_path, 1) == {CODE_A: "failed"}
    assert [g for g, _ in h.notices] == [1] and CODE_A in h.notices[0][1]
    await h.run([item(CODE_A)])
    assert h.sent(1) == [] and post_status(h.db_path, 1) == {CODE_A: "failed"}


async def test_a_stale_pending_row_is_failed_by_a_running_pass_not_resent(h):
    add_guild(h.db_path, 1)
    h.fail_with[1] = [_Crash()]
    with pytest.raises(_Crash):
        await h.run([item(CODE_A)])

    h.fail_with.clear()
    h.clock = NOW + timedelta(seconds=_PENDING_STALE_S + 1)
    h.notices.clear()
    await h.run([])

    assert h.sent(1) == [] and post_status(h.db_path, 1) == {CODE_A: "failed"}
    assert [g for g, _ in h.notices] == [1]


async def test_a_crash_leaves_other_queued_guilds_for_the_next_pass(h):
    for gid in (1, 2, 3):
        add_guild(h.db_path, gid)
    h.fail_with[2] = [_Crash()]
    with pytest.raises(_Crash):
        await h.run([item(CODE_A)])
    assert post_status(h.db_path, 3) == {CODE_A: "queued"}

    h.fail_with.clear()
    await h.run([])

    assert post_status(h.db_path, 3) == {CODE_A: "posted"}
    assert post_status(h.db_path, 2) == {CODE_A: "pending"}  # left for recovery, not re-sent
    assert h.sent(2) == []


async def test_two_passes_delivering_the_same_queue_send_each_code_once_per_guild(db_path, cfg):
    seed(db_path)
    for gid in (1, 2, 3, 4):
        add_guild(db_path, gid, ping="everyone")
    one, two = Harness(db_path, cfg, NOW), Harness(db_path, cfg, NOW)
    await _release(one.deps(), [item(CODE_A, CODE_B)], seeding_ok=True)
    assert rows(db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE status = 'queued'") == [(8,)]

    await asyncio.gather(one.deliver(), two.deliver())

    for gid in (1, 2, 3, 4):
        sent = one.sent(gid) + two.sent(gid)
        assert sorted(c for a in sent for c in a.codes) == [CODE_A, CODE_B]
        assert post_status(db_path, gid) == {CODE_A: "posted", CODE_B: "posted"}
        assert shift_row(db_path, gid).ping_count == 1
    assert one.notices == [] and two.notices == []


# --- isolation ---


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("x"),
        ValueError("x"),
        KeyError("x"),
        OSError("x"),
        TimeoutError(),
        ZeroDivisionError(),
        PublishError("retries exhausted", posted_ids=[]),
        _http(403),
        _http(404),
        _http(500),
    ],
    ids=[
        "RuntimeError",
        "ValueError",
        "KeyError",
        "OSError",
        "TimeoutError",
        "ZeroDivisionError",
        "PublishError",
        "http403",
        "http404",
        "http500",
    ],
)
async def test_any_poster_exception_stays_inside_that_guild(h, error):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    h.fail_with[1] = [error] * 10  # outlasts the retry budget

    await h.run([item(CODE_A)])

    assert post_status(h.db_path, 1) == {CODE_A: "failed"}
    assert post_status(h.db_path, 2) == {CODE_A: "posted"}
    assert len(h.sent(2)) == 1
    assert [g for g, _ in h.notices] == [1]


async def test_a_notice_channel_that_also_raises_does_not_stop_the_next_guild(h):
    add_guild(h.db_path, 1)
    add_guild(h.db_path, 2)
    h.fail_with[1] = [RuntimeError("no access")]
    h.notify_raises = {1}

    await h.run([item(CODE_A)])

    assert len(h.sent(2)) == 1


async def test_begin_batch_raising_is_contained_to_that_guild(h):
    add_guild(h.db_path, 1)
    add_guild(h.db_path, 2)
    real = h.factory

    def factory(gid, cid, ping, notify):
        poster = real(gid, cid, ping, notify)
        if gid == 1:
            poster.begin_batch = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        return poster

    h.factory = factory

    await h.run([item(CODE_A)])

    assert len(h.sent(2)) == 1
    assert [g for g, _ in h.notices] == [1]


async def test_a_failed_send_still_burns_that_guilds_ping_budget(h):
    # Documented behavior (same as v2): the ping is spent when the claim is written,
    # before anything is sent, so a dead channel eats the day's budget.
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [RuntimeError("no access")]

    await h.run([item(CODE_A)])

    assert shift_row(h.db_path, 1).ping_count == 1
    assert post_status(h.db_path, 1) == {CODE_A: "failed"}


async def test_no_cap_note_for_a_guild_that_never_asked_for_a_ping(h):
    add_guild(h.db_path, 1, ping="none", ping_day="2026-10-01", ping_count=3)

    await h.run([item(CODE_A)])

    assert h.notices == []
    assert shift_row(h.db_path, 1).ping_count == 3


async def test_cap_note_goes_to_the_capped_guild_only(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=3)
    add_guild(h.db_path, 2, ping="everyone")

    await h.run([item(CODE_A)])

    assert [g for g, _ in h.notices] == [1]
    assert h.sent(2)[0].ping is True


# --- the hook timeout: a real design limit ---


def test_pace_alone_caps_how_many_guilds_one_hook_can_serve():
    # Changed with the queue: the arithmetic is the same, but it's no longer a ceiling on
    # who hears about a code, only on how many guilds one *pass* can serve before the
    # timeout cuts the walk and the rest stay queued for the next one.
    # 120 s budget, 0.2 s between guilds (none before the first): sleeping alone
    # uses the whole budget at 601 guilds, before a single message is sent.
    # Real sends take far longer than zero, so the practical per-pass figure is lower; at
    # an assumed half second per guild (one DB claim, one Discord send, one
    # mark) it is about 170. The numbers come from the constants, so this
    # test fails loudly if either is changed without revisiting them.
    pace_only = int(_SHIFT_HOOK_TIMEOUT_S / _GUILD_PACE_S) + 1
    assert (_SHIFT_HOOK_TIMEOUT_S, _GUILD_PACE_S, pace_only) == (120.0, 0.2, 601)
    per_pass = int(_SHIFT_HOOK_TIMEOUT_S / (_GUILD_PACE_S + 0.5))
    assert per_pass == 171
    # The walk always starts at the lowest queued guild id, so each pass makes progress:
    # 1000 guilds all owed one code is fully served in ceil(1000 / 171) hourly passes.
    assert math.ceil(1000 / per_pass) == 6


async def test_pace_is_slept_between_guilds_not_before_the_first(h):
    for gid in range(1, 6):
        add_guild(h.db_path, gid)

    async def sleep(seconds):
        h.sleeps.append(seconds)

    h.sleep = sleep
    await h.run([item(CODE_A)])

    assert h.sleeps == [_GUILD_PACE_S] * 4


async def _timed_out_pass(h, guilds, slow_s, timeout_s, *, cut_after=6):
    # A deterministic stand-in for "the hook's timeout fired mid fan-out". The
    # first version used a real 0.3 s wall-clock timeout, which on a loaded
    # machine could land inside a guild's claim and leave a `pending` row; by
    # design that row is failed rather than re-sent (no double posts), so the
    # test flaked on the clock, not on a bug. Cutting at the pace sleep puts
    # the cancellation exactly between guilds, after one finished and before
    # the next is claimed, which is the case the timeout tests are about.
    # `slow_s` and `timeout_s` are kept so the call sites read the same.
    del slow_s, timeout_s
    reached = asyncio.Event()
    calls = 0

    async def sleep(_seconds):
        nonlocal calls
        calls += 1
        if calls >= cut_after:
            reached.set()
            await asyncio.Event().wait()  # park here until cancelled

    h.sleep = sleep
    h.posters.clear()
    task = asyncio.create_task(detect_and_fan_out(h.deps(), [item(CODE_A)], seeding_ok=True))
    await asyncio.wait_for(reached.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return guilds


async def test_timeout_mid_fanout_leaves_consistent_rows_and_no_stuck_pending(h):
    guilds = list(range(1, 21))
    for gid in guilds:
        add_guild(h.db_path, gid, ping="everyone")

    await _timed_out_pass(h, guilds, slow_s=0.05, timeout_s=0.3)

    # Changed with the queue: every eligible guild has a row from release, so the cut-off
    # shows up as posted (reached) versus queued (waiting), never as "no row at all".
    status = {gid: post_status(h.db_path, gid) for gid in guilds}
    done = {gid for gid in guilds if status[gid] == {CODE_A: "posted"}}
    waiting = {gid for gid in guilds if status[gid] == {CODE_A: "queued"}}
    assert 0 < len(done) < len(guilds)
    assert done | waiting == set(guilds)  # no half-way rows, nobody lost
    assert done == set(range(1, len(done) + 1))  # the walk is in guild id order
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE status = 'pending'") == [
        (0,)
    ]
    assert rows(h.db_path, "SELECT status FROM alerted_codes") == [("posted",)]
    assert h.notices == []


async def test_guilds_cut_off_by_the_timeout_still_get_the_code_later(h):
    guilds = list(range(1, 21))
    for gid in guilds:
        add_guild(h.db_path, gid, ping="none")
    await _timed_out_pass(h, guilds, slow_s=0.05, timeout_s=0.3)

    h.sleep = _no_sleep
    await recover_pending_guild_codes(h.db_path, h.notify_guild)
    await h.run([item(CODE_A)])  # the next hourly pass sees the same thread again

    for gid in guilds:
        assert post_status(h.db_path, gid) == {CODE_A: "posted"}, f"guild {gid} never heard"


# --- seeding and roundups ---


async def test_first_ever_pass_is_silent_even_with_eligible_guilds(db_path, cfg):
    add_guild(db_path, 1, ping="everyone")
    h = Harness(db_path, cfg, NOW)

    released = await h.run([item(CODE_A, CODE_B)])

    assert released == 0 and h.sent(1) == []
    assert rows(db_path, "SELECT DISTINCT status FROM alerted_codes") == [("seeded",)]
    assert rows(db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(0,)]
    assert shift_row(db_path, 1).ping_count == 0


async def test_unhealthy_first_pass_is_silent_and_leaves_the_marker_off(db_path, cfg):
    add_guild(db_path, 1, ping="everyone")
    h = Harness(db_path, cfg, NOW)

    assert await h.run([item(CODE_A)], seeding_ok=False) == 0

    assert h.sent(1) == []
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is False


async def test_a_roundup_past_the_fifty_code_cap_posts_fifty_and_only_logs_the_rest(h, cfg, caplog):
    h.cfg = _with_shift(cfg, max_codes_per_item=5)
    add_guild(h.db_path, 1, ping="everyone")
    mega = item(*[_code(i) for i in range(60)], url="https://example.com/mega")

    with caplog.at_level(logging.WARNING, logger="newsbot.shift.fanout"):
        released = await h.run([mega])

    assert released == 50
    assert len(post_status(h.db_path, 1)) == 50
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE status = 'roundup'") == [(10,)]
    assert any("past the per-check cap" in r.message for r in caplog.records)
    # Changed: the overflow now reaches the owner as one capped line (v2 did this), and
    # still no guild hears of it.
    assert h.owner_alerts == [
        "newsbot: SHiFT roundup code cap reached (50 per check); "
        "10 code(s) recorded silently, not posted this check."
    ]
    assert h.notices == []
    assert all(not a.ping and a.ping_prefix == "" for a in h.sent(1))


async def test_roundup_beside_a_trusted_code_spends_one_ping_and_only_on_the_normal_message(h):
    add_guild(h.db_path, 1, ping="everyone")
    mega = item(*[_code(i) for i in range(8)], url="https://example.com/mega")

    await h.run([item(CODE_A), mega])

    alerts = h.sent(1)
    assert [a.ping for a in alerts].count(True) == 1 and alerts[0].ping
    assert shift_row(h.db_path, 1).ping_count == 1
    assert rows(h.db_path, "SELECT SUM(pinged) FROM guild_code_posts") == [(1,)]


# --- the v2 path stays put ---


def _candidate(code, *, golden):
    return CodeCandidate(
        code, golden=golden, source_name="Gearbox Blog", item_url="https://e.com/x", fresh=True
    )


def _candidates():
    return [_candidate(CODE_A, golden=False), _candidate(CODE_B, golden=True)]


def test_render_defaults_are_v2s_everyone_header_byte_for_byte():
    default = render_code_alerts(_candidates(), ping=True)
    explicit = render_code_alerts(_candidates(), ping_mention="@everyone", ping=True)
    assert default == explicit
    assert default[0].content.startswith("@everyone **New SHiFT codes**")
    assert default[0].ping_prefix == "@everyone "


def test_an_unpinged_render_ignores_the_mention_text_entirely():
    plain = render_code_alerts(_candidates(), ping=False)
    with_mention = render_code_alerts(_candidates(), ping=False, ping_mention="<@&123>")
    assert plain == with_mention
    assert all(a.ping_prefix == "" for a in plain)


async def test_poster_defaults_are_v2s_everyone_ping_and_owner_alerts():
    channel = FakeChannel(can_mention_everyone=False)
    owner: list[str] = []

    class Client(FakeClient):
        async def alert(self, text):
            owner.append(text)

    poster = DiscordCodeAlertPoster(Client({5: channel}), 5)
    alert = RenderedAlert(content="@everyone **New SHiFT code**", codes=[CODE_A], ping=True)

    await poster.post(alert)

    assert channel.sent[0][1]["parse"] == ["everyone"]
    assert len(owner) == 1
