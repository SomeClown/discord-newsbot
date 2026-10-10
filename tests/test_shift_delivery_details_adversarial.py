"""Adversarial tests for delivery with details (plan task 7; design.md §16).

The record of a posted alert is a promise to the edit pass: "this message looked like
this, held these codes, and was rendered at this moment". Every test below attacks one
way that promise could quietly become a lie, because an edit built on a wrong record is
worse than no edit (it rewrites a message that real people are looking at).

The groups, in the order a stranger would notice them breaking:
1. Recording: the write fails, a claim is partial, a batch is several messages, the
   server changes its mind (or leaves) mid-delivery, the header outgrows its column.
2. `rendered_at` against details that change while a message is in flight.
3. Ping integrity: what the stored header says against what the message carried.
4. The reserve: `reserve_for_config` on every config shape, renamed "via" names, and
   packing with it against the walk's time budget and pacing.
5. Follow-ups that fit by size and drain over several passes.
6. Roundups, the CLI preview, retention.

Real temp SQLite with the real migrations, scripted fake posters, an injected clock. No
network. Invisible characters are `\\uXXXX` escapes on purpose; this file stays visible.
"""

from __future__ import annotations

import ast
import asyncio
import random
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_shift_delivery_details import (
    BIG_RESERVE,
    BLUESKY,
    GEARBOX,
    MENTALMARS,
    REDDIT,
    DetailsHarness,
    message_rows,
    numbered,
    sighting,
    store_details,
    with_details,
)
from test_shift_fanout import (
    CODE_A,
    CODE_B,
    CODE_C,
    CODE_D,
    NOW,
    ROLE,
    V3,
    add_guild,
    item,
    rows,
    seed,
    shift_row,
)

from newsbot.bot.format import RenderedAlert, details_line, details_reserve, discord_len
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import fanout
from newsbot.shift.details import DetailObservation
from newsbot.shift.fanout import (
    FanoutDeps,
    deliver_queued_codes,
    detect_and_fan_out,
    reserve_for_config,
)
from newsbot.shift.sweep import PrintCodeAlertPoster
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import DETAILS_STAMP_FLOOR, AlertMessageRecord

ROOT = Path(__file__).parent.parent
# A mention that would notify someone if mentions were on: users, roles, channels, broadcasts.
LIVE_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here")
ROLE_B = "9876543210987654321"
STAR_NAME = "*" * 100  # escapes to 200 units, the worst a config-legal name can be


# --- scaffolding ---


@dataclass
class Delivered:
    """One message a fake poster accepted: where, as which message, with what text."""

    guild_id: int
    channel_id: int
    message_id: int
    alert: RenderedAlert


class Rig(DetailsHarness):
    """The details harness with message ids that stay unique across passes, a failure script
    per server, hooks for "something happens while this message is in flight", and a log.

    `FakePoster` numbers its messages from 9001 on every pass, which would make two passes'
    messages the same message as far as `(guild_id, message_id)` is concerned. Real Discord
    never does that, so neither does this.
    """

    def __init__(self, db_path, cfg, now):
        super().__init__(db_path, cfg, now)
        self.next_id = 5000
        self.script: dict[int, list[Exception | None]] = {}
        self.on_begin = None  # (guild_id) -> None, runs where a pass's claim race would
        self.on_post = None  # (guild_id, alert) -> None, runs inside the send
        self.delivered: list[Delivered] = []
        self.attempts: list[tuple[int, RenderedAlert, Exception | None]] = []
        self.sleeps: list[float] = []
        self.mono = 0.0  # the walk's monotonic clock, which only a test moves
        self.created: list[tuple[int, int, str]] = []

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def deps(self, **changes):
        changes.setdefault("clock", lambda: self.mono)
        return replace(super().deps(), sleep=self._sleep, **changes)

    def factory(self, guild_id, channel_id, ping, notify):
        self.created.append((guild_id, channel_id, ping))
        return RigPoster(self, guild_id, channel_id, ping)

    def sent(self, guild_id) -> list[RenderedAlert]:
        return [d.alert for d in self.delivered if d.guild_id == guild_id]

    async def deliver(self):
        return await deliver_queued_codes(self.deps())

    async def run(self, items, *, seeding_ok=True):
        return await detect_and_fan_out(self.deps(), items, seeding_ok=seeding_ok)


class RigPoster:
    def __init__(self, rig: Rig, guild_id: int, channel_id: int, ping: str):
        self.rig = rig
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.ping = ping

    def begin_batch(self):
        if self.rig.on_begin is not None:
            self.rig.on_begin(self.guild_id)

    async def post(self, alert):
        rig = self.rig
        if rig.on_post is not None:
            rig.on_post(self.guild_id, alert)
        script = rig.script.get(self.guild_id)
        outcome = script.pop(0) if script else None
        rig.attempts.append((self.guild_id, alert, outcome))
        if outcome is not None:
            raise outcome
        rig.next_id += 1
        rig.delivered.append(Delivered(self.guild_id, self.channel_id, rig.next_id, alert))
        return rig.next_id


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def cfg():
    return load_config(V3)


@pytest.fixture
def h(db_path, cfg):
    rig = Rig(db_path, cfg, NOW)
    seed(db_path)
    return rig


def distinct(*codes, trust="official", **kw):
    """One item per code, so a batch of them is a batch of separate finds, not a roundup."""
    return [item(c, trust=trust, url=f"https://example.com/{c[:5]}", **kw) for c in codes]


def n_codes(n: int) -> list[str]:
    return [numbered(i) for i in range(1, n + 1)]


def first_paragraph(content: str) -> str:
    return content.split("\n\n")[0]


def posts_of(db_path, guild_id, message_id):
    return [
        code
        for (code,) in rows(
            db_path,
            "SELECT code FROM guild_code_posts WHERE guild_id = ? AND message_id = ? "
            "AND from_roundup = 0 ORDER BY rowid",
            guild_id,
            message_id,
        )
    ]


def assert_records_match_what_posted(rig: Rig):
    """The record holds what went out: its header, its channel, its codes in order.

    "What went out" is the text the poster really accepted, which after a stripped ping
    is not the text that was rendered. Also the other direction: every normal message that
    posted has a record, and nothing else does.
    """
    records = {(r[0], r[1]): r for r in message_rows(rig.db_path)}
    by_message = {(d.guild_id, d.message_id): d for d in rig.delivered}
    assert set(records) <= set(by_message), "a record for a message nobody posted"
    for key, delivered in by_message.items():
        codes = posts_of(rig.db_path, *key)
        if not codes:
            continue  # a roundup or a follow-up: never recorded
        assert key in records, f"normal message {key} posted without a record"
        _guild, _message, channel, header, golden, _posted, _rendered = records[key]
        assert header == first_paragraph(delivered.alert.content)
        assert channel == delivered.channel_id
        assert golden == int(delivered.alert.golden_prefix)
        assert codes == delivered.alert.codes


# --- 1. recording ---


async def test_a_busy_database_while_recording_is_retried_and_the_record_lands_once(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    real = fanout.mark_guild_codes_posted
    failures = [sqlite3.OperationalError("database is locked")] * 2

    def flaky(conn, guild_id, codes, **kw):
        if failures:
            raise failures.pop()
        return real(conn, guild_id, codes, **kw)

    monkeypatch.setattr(fanout, "mark_guild_codes_posted", flaky)

    await h.run([item(CODE_A)])

    assert len(h.delivered) == 1  # retried the write, never the send
    assert rows(h.db_path, "SELECT status FROM guild_code_posts") == [("posted",)]
    assert len(message_rows(h.db_path)) == 1
    assert_records_match_what_posted(h)


async def test_a_write_that_never_lands_leaves_neither_the_mark_nor_the_record(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE

    def locked(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fanout, "mark_guild_codes_posted", locked)

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    # One message went out, and the database never heard about it. Posted mark and record
    # are one transaction, so both are absent: the record can't claim a message the posted
    # mark disowns, or the other way around.
    assert "SHiFT message sent but the database stayed busy" in caplog.text
    assert len(h.delivered) == 1
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_posts")} == {"pending"}
    assert message_rows(h.db_path) == []
    # The rest of the batch isn't sent behind its back, and nothing pending is ever resent.
    monkeypatch.undo()
    await h.deliver()
    assert len(h.delivered) == 1


def test_a_record_the_table_refuses_does_not_undo_the_posted_mark(db_path):
    add_guild(db_path, 1, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")
        record = AlertMessageRecord(100, "**New SHiFT code**", False, NOW, NOW)
        try:
            repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=0, message=record)
        except sqlite3.IntegrityError:
            pass
    assert rows(db_path, "SELECT status FROM guild_code_posts") == [("posted",)]


def claim_elsewhere(h: Rig, guild_id: int, code: str, message_id: int = 7777):
    """Make `code` look like a concurrent pass already claimed and posted it for this server."""
    with closing(connect(h.db_path)) as conn:
        repo.claim_guild_codes(conn, guild_id, [code], pinged=False, local_day="2026-10-01")
        repo.mark_guild_codes_posted(conn, guild_id, [code], message_id=message_id)


async def test_a_partial_claim_records_exactly_the_codes_that_went_out(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE  # two to a message, so the claimed-away code shifts the packing
    h.on_begin = lambda guild_id: claim_elsewhere(h, guild_id, CODE_B)

    await h.run(distinct(CODE_A, CODE_B, CODE_C, CODE_D))

    # B went out in somebody else's message; the rest repacked around the gap.
    assert [d.alert.codes for d in h.delivered] == [[CODE_A, CODE_C], [CODE_D]]
    assert_records_match_what_posted(h)
    assert posts_of(h.db_path, 1, 7777) == [CODE_B]
    assert 7777 not in {r[1] for r in message_rows(h.db_path)}  # not ours to record


async def test_a_partial_claim_that_removes_the_only_golden_code_drops_the_golden_prefix(h):
    add_guild(h.db_path, 1, ping="everyone")
    golden = item(CODE_A, title="Borderlands 4 Golden Key SHiFT code: " + CODE_A, url="https://e/a")
    plain = item(CODE_B, url="https://example.com/b")
    h.on_begin = lambda guild_id: claim_elsewhere(h, guild_id, CODE_A)

    await h.run([golden, plain])

    ((_g, _m, _c, header, golden_prefix, *_),) = message_rows(h.db_path)
    assert header == "@everyone **New SHiFT code**"
    assert golden_prefix == 0  # the batch that posted wasn't mixed any more
    assert "Golden Key: " not in h.delivered[0].alert.content
    assert_records_match_what_posted(h)


async def test_a_partial_claim_that_removes_the_plain_code_stores_the_golden_title(h):
    add_guild(h.db_path, 1, ping=ROLE)
    golden = item(CODE_A, title="Borderlands 4 Golden Key SHiFT code: " + CODE_A, url="https://e/a")
    plain = item(CODE_B, url="https://example.com/b")
    h.on_begin = lambda guild_id: claim_elsewhere(h, guild_id, CODE_B)

    await h.run([golden, plain])

    ((_g, _m, _c, header, golden_prefix, *_),) = message_rows(h.db_path)
    assert header == f"<@&{ROLE}> **New Golden Key code**"
    assert golden_prefix == 0
    assert_records_match_what_posted(h)


async def test_a_multi_message_batch_gets_one_record_per_message_with_its_own_codes(h):
    add_guild(h.db_path, 1, ping=ROLE)
    h.reserve = BIG_RESERVE
    codes = n_codes(7)

    await h.run(distinct(*codes))

    got = message_rows(h.db_path)
    assert len(got) == len(h.delivered) >= 4
    assert [r[3] for r in got] == [f"<@&{ROLE}> **New SHiFT codes**"] + ["**(continued)**"] * (
        len(got) - 1
    )
    # Disjoint, in release order, and all of them: nobody's code lives in two records.
    per_message = [posts_of(h.db_path, 1, r[1]) for r in got]
    assert [c for group in per_message for c in group] == codes
    assert len({c for group in per_message for c in group}) == len(codes)
    assert_records_match_what_posted(h)


async def test_two_servers_sharing_a_batch_keep_their_own_records(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    h.reserve = BIG_RESERVE

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    by_guild = {}
    for guild_id, message_id, channel_id, header, *_ in message_rows(h.db_path):
        by_guild.setdefault(guild_id, []).append((message_id, channel_id, header))
    assert {g: {c for _, c, _ in v} for g, v in by_guild.items()} == {1: {100}, 2: {200}}
    assert by_guild[1][0][2].startswith("@everyone ")
    assert by_guild[2][0][2].startswith(f"<@&{ROLE}> ")
    assert_records_match_what_posted(h)


async def test_a_channel_and_role_changed_after_the_load_leave_the_record_of_what_posted(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    changed = []

    def change_settings(guild_id, _alert):
        if changed:
            return
        changed.append(True)
        with closing(connect(h.db_path)) as conn:
            repo.set_shift(
                conn, guild_id, enabled=True, channel_id=999, ping=ROLE_B, now=lambda: NOW
            )

    h.on_post = change_settings

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    got = message_rows(h.db_path)
    assert len(got) == 2
    # The poster was built for channel 100 and pinging @everyone before the change; the
    # record says so for every message of the batch, not what the settings say now.
    assert {r[2] for r in got} == {100}
    assert got[0][3] == "@everyone **New SHiFT codes**"
    assert got[1][3] == "**(continued)**"
    assert shift_row(h.db_path, 1).channel_id == 999
    assert_records_match_what_posted(h)


async def test_a_ping_switched_off_after_the_load_still_records_the_ping_that_went_out(h):
    add_guild(h.db_path, 1, ping="everyone")

    def switch_off(guild_id, _alert):
        with closing(connect(h.db_path)) as conn:
            repo.set_shift(
                conn, guild_id, enabled=True, channel_id=100, ping="none", now=lambda: NOW
            )

    h.on_post = switch_off

    await h.run([item(CODE_A)])

    ((_g, _m, _c, header, *_),) = message_rows(h.db_path)
    assert header == "@everyone **New SHiFT code**"
    assert h.delivered[0].alert.ping is True


async def test_a_server_removed_mid_delivery_leaves_no_orphan_record_and_no_error(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    h.reserve = BIG_RESERVE

    def remove_guild_one(guild_id, _alert):
        if guild_id == 1:
            with closing(connect(h.db_path)) as conn:
                repo.delete_guild(conn, 1)

    h.on_post = remove_guild_one

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_alert_messages WHERE guild_id = 1") == [(0,)]
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE guild_id = 1") == [(0,)]
    # The other server is untouched, and still gets a record per message.
    assert {r[0] for r in message_rows(h.db_path)} == {2}
    assert len(message_rows(h.db_path)) == 2


async def test_a_server_that_leaves_and_rejoins_mid_delivery_gets_no_record_for_the_old_message(h):
    add_guild(h.db_path, 1, ping="everyone")

    def leave_and_rejoin(guild_id, _alert):
        with closing(connect(h.db_path)) as conn:
            repo.delete_guild(conn, guild_id)
        add_guild(h.db_path, guild_id, ping="everyone", enabled_at=NOW + timedelta(minutes=1))

    h.on_post = leave_and_rejoin

    await h.run([item(CODE_A)])

    assert message_rows(h.db_path) == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(0,)]


async def test_two_alerts_given_the_same_message_id_keep_the_first_record_and_do_not_crash(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE

    class SameIdPoster(RigPoster):
        async def post(self, alert):
            await super().post(alert)
            self.rig.next_id = 5000  # the next send is handed the same id again
            return 5001

    h.factory = lambda g, c, p, n: SameIdPoster(h, g, c, p)

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    assert len(message_rows(h.db_path)) == 1
    assert message_rows(h.db_path)[0][3] == "@everyone **New SHiFT codes**"
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_posts")} == {"posted"}


# --- header limits (the table accepts 1 to 200 characters) ---


def _pending_one(db_path, guild_id=1):
    add_guild(db_path, guild_id, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, guild_id, [CODE_A], pinged=False, local_day="2026-10-01")


@pytest.mark.parametrize(
    ("header", "recorded"),
    [
        ("x" * 200, True),
        ("x" * 201, False),
        ("", False),
        ("\U0001f600" * 200, True),  # 200 characters, 400 UTF-16 units: the table counts characters
        ("\U0001f600" * 201, False),
    ],
)
def test_a_header_outside_the_tables_limits_skips_the_record_and_keeps_the_posted_mark(
    db_path, header, recorded
):
    _pending_one(db_path)
    record = AlertMessageRecord(100, header, False, NOW, NOW)
    with closing(connect(db_path)) as conn:
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=42, message=record)
    assert rows(db_path, "SELECT status, message_id FROM guild_code_posts") == [("posted", 42)]
    assert len(message_rows(db_path)) == (1 if recorded else 0)


async def test_the_longest_real_header_fits_with_room_to_spare(h):
    add_guild(h.db_path, 1, ping="1" * 20)  # the longest role id the table allows
    golden = [
        item(c, title="Borderlands 4 Golden Key SHiFT code: " + c, url=f"https://e/{c[:3]}")
        for c in (CODE_A, CODE_B)
    ]

    await h.run(golden)

    ((_g, _m, _c, header, *_),) = message_rows(h.db_path)
    assert header == f"<@&{'1' * 20}> **New Golden Key codes**"
    assert len(header) < 60


def test_a_guild_alert_messages_row_cannot_be_written_for_a_missing_server(db_path):
    add_guild(db_path, 1)
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.mark_guild_codes_posted(
            conn, 77, [CODE_A], message_id=5, message=AlertMessageRecord(1, "h", False, NOW, NOW)
        )
    assert message_rows(db_path) == []


# --- 2. rendered_at against details that change in flight ---

# The edit pass's selection rule (plan section 3.5): a live message whose code's
# `details_changed_at` is newer than the message's `rendered_at`. Spelled out here because the
# repo function that does it belongs to task 8; the rule is the contract this task has to meet.
_NEEDS_AN_EDIT = (
    "SELECT m.message_id FROM guild_alert_messages m WHERE EXISTS ("
    "SELECT 1 FROM guild_code_posts p JOIN code_details d ON d.code = p.code "
    "WHERE p.guild_id = m.guild_id AND p.message_id = m.message_id AND p.from_roundup = 0 "
    "AND d.details_changed_at > m.rendered_at) ORDER BY m.message_id"
)


def needs_an_edit(db_path) -> list[int]:
    return [m for (m,) in rows(db_path, _NEEDS_AN_EDIT)]


async def test_details_that_land_between_the_read_and_the_post_look_newer_than_the_render(h):
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=NOW - timedelta(hours=1))

    def details_change_in_flight(_guild_id, _alert):
        h.clock = NOW + timedelta(seconds=2)
        store_details(h.db_path, CODE_A, reward="5 Golden Keys", now=h.clock)

    h.on_post = details_change_in_flight

    await h.run([item(CODE_A)])

    (alert,) = h.sent(1)
    assert "2 Golden Keys" in alert.content  # what the message said when it went out
    assert needs_an_edit(h.db_path) == [h.delivered[0].message_id]


async def test_details_already_in_the_message_do_not_ask_for_an_edit(h):
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=NOW - timedelta(hours=1))

    await h.run([item(CODE_A)])

    assert "2 Golden Keys" in h.sent(1)[0].content
    assert needs_an_edit(h.db_path) == []


async def test_a_change_a_millisecond_after_the_read_is_still_newer_than_the_render(h):
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=NOW - timedelta(hours=1))

    def late_change(_guild_id, _alert):
        # The smallest step the clock can honestly take: still strictly newer than the render.
        h.clock = NOW + timedelta(milliseconds=1)
        store_details(h.db_path, CODE_A, reward="5 Golden Keys", now=h.clock)

    h.on_post = late_change
    await h.run([item(CODE_A)])

    assert needs_an_edit(h.db_path) == [h.delivered[0].message_id]


async def test_a_change_stamped_by_a_clock_that_started_before_the_read_is_still_found(h):
    """Overlapping hooks: a release that took its `now` before a delivery read, committed after."""
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=NOW - timedelta(hours=1))

    def stale_stamp(_guild_id, _alert):
        store_details(h.db_path, CODE_A, reward="5 Golden Keys", now=NOW - timedelta(seconds=1))

    h.on_post = stale_stamp
    await h.run([item(CODE_A)])

    assert needs_an_edit(h.db_path) == [h.delivered[0].message_id]


async def test_rendered_at_is_a_stamp_so_a_slow_pass_does_not_move_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    changed = NOW - timedelta(hours=5)
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=changed)

    def slow_send(_guild_id, _alert):
        h.clock += timedelta(minutes=3)

    h.on_post = slow_send

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    got = message_rows(h.db_path)
    # Stamps follow the codes' details, per message, whatever the clock did while sending.
    assert [datetime.fromisoformat(r[6]) for r in got] == [changed, DETAILS_STAMP_FLOOR]
    assert [datetime.fromisoformat(r[5]) for r in got] == [
        NOW + timedelta(minutes=3),
        NOW + timedelta(minutes=6),
    ]


# --- 3. ping integrity: the stored header never claims a ping the message didn't carry ---

AMBIGUOUS = PublishError("timeout")


def refused():
    return PublishError("slow down", rejected=True, retry_after=0.0)


def carries_a_ping(content: str) -> bool:
    return content.startswith(("@everyone", "<@&"))


def assert_ping_integrity(rig: Rig):
    """The invariants a ping owes everyone, checked against everything the poster saw.

    - A stored header has a ping exactly when the message that posted had one.
    - Once an attempt failed ambiguously, no later attempt at that message carries a ping.
    - At most one delivered message per server per batch carries a ping.
    """
    for key, delivered in {(d.guild_id, d.message_id): d for d in rig.delivered}.items():
        for _g, _m, _c, header, *_ in (r for r in message_rows(rig.db_path) if (r[0], r[1]) == key):
            assert carries_a_ping(header) == carries_a_ping(delivered.alert.content)
    ambiguous_seen: set[tuple[int, str]] = set()
    for guild_id, alert, outcome in rig.attempts:
        marker = (guild_id, alert.nonce)
        if marker in ambiguous_seen:
            assert not carries_a_ping(alert.content) and not alert.ping
        if isinstance(outcome, PublishError) and not outcome.rejected:
            ambiguous_seen.add(marker)


@pytest.mark.parametrize(
    ("script", "stored_header", "message_pinged"),
    [
        ([AMBIGUOUS], "**New SHiFT codes**", False),
        ([refused(), AMBIGUOUS], "**New SHiFT codes**", False),
        ([AMBIGUOUS, refused()], "**New SHiFT codes**", False),
        ([refused(), refused()], "@everyone **New SHiFT codes**", True),
        ([refused(), AMBIGUOUS, refused()], "**New SHiFT codes**", False),
        ([], "@everyone **New SHiFT codes**", True),
    ],
    ids=["timeout", "429-then-timeout", "timeout-then-429", "two-429s", "429-timeout-429", "clean"],
)
async def test_the_stored_header_matches_the_ping_the_first_message_really_carried(
    h, script, stored_header, message_pinged
):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    h.script[1] = list(script)

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    got = message_rows(h.db_path)
    assert got[0][3] == stored_header
    assert carries_a_ping(h.delivered[0].alert.content) is message_pinged
    assert got[1][3] == "**(continued)**"
    assert sum(carries_a_ping(d.alert.content) for d in h.delivered) == int(message_pinged)
    assert_ping_integrity(h)
    assert_records_match_what_posted(h)


async def test_a_role_ping_stripped_after_a_timeout_is_stripped_from_the_record_too(h):
    add_guild(h.db_path, 1, ping=ROLE)
    h.script[1] = [AMBIGUOUS]

    await h.run([item(CODE_A)])

    ((_g, _m, _c, header, *_),) = message_rows(h.db_path)
    assert header == "**New SHiFT code**"
    assert ROLE not in header and ROLE not in h.delivered[0].alert.content
    assert_ping_integrity(h)


async def test_a_timeout_on_a_continuation_message_has_no_ping_to_strip_and_records_none(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    h.script[1] = [None, AMBIGUOUS]  # first message fine, second times out once

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    got = message_rows(h.db_path)
    assert [r[3] for r in got] == ["@everyone **New SHiFT codes**", "**(continued)**"]
    assert_ping_integrity(h)
    assert_records_match_what_posted(h)


async def test_a_batch_that_fails_for_good_records_nothing_and_never_pings_twice(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    h.script[1] = [AMBIGUOUS] * 20

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    assert message_rows(h.db_path) == []
    assert h.delivered == []
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_posts")} == {"failed"}
    assert_ping_integrity(h)


async def test_a_second_message_that_fails_leaves_the_first_recorded_and_only_the_first(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    h.script[1] = [None] + [AMBIGUOUS] * 20

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    got = message_rows(h.db_path)
    assert len(got) == 1 and got[0][3] == "@everyone **New SHiFT codes**"
    assert dict(rows(h.db_path, "SELECT code, status FROM guild_code_posts")) == {
        CODE_A: "posted",
        CODE_B: "posted",
        CODE_C: "failed",
    }
    assert_records_match_what_posted(h)


async def test_a_cap_hit_before_a_multi_message_batch_stores_unpinged_headers_throughout(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=3)
    h.reserve = BIG_RESERVE

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    got = message_rows(h.db_path)
    assert [r[3] for r in got] == ["**New SHiFT codes**", "**(continued)**"]
    assert not any(carries_a_ping(d.alert.content) for d in h.delivered)
    assert shift_row(h.db_path, 1).ping_count == 3
    assert_ping_integrity(h)
    assert_records_match_what_posted(h)


async def test_the_cap_runs_out_between_two_batches_and_the_second_stores_no_ping(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=2)

    await h.run([item(CODE_A)])  # the third ping of the day
    await h.run([item(CODE_B)])  # the cap

    got = message_rows(h.db_path)
    assert [r[3] for r in got] == ["@everyone **New SHiFT code**", "**New SHiFT code**"]
    assert shift_row(h.db_path, 1).ping_count == 3
    assert_ping_integrity(h)
    assert_records_match_what_posted(h)


async def test_a_cap_of_zero_never_pings_and_never_stores_one(db_path, cfg):
    cfg0 = cfg.model_copy(update={"shift": cfg.shift.model_copy(update={"max_pings_per_day": 0})})
    rig = Rig(db_path, cfg0, NOW)
    seed(db_path)
    add_guild(db_path, 1, ping=ROLE)

    await rig.run([item(CODE_A)])

    assert [r[3] for r in message_rows(db_path)] == ["**New SHiFT code**"]
    assert_ping_integrity(rig)


async def test_untrusted_codes_and_ping_off_both_store_the_plain_header(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="none")

    await h.run([item(CODE_A, trust="community")])

    assert sorted((r[0], r[3]) for r in message_rows(h.db_path)) == [
        (1, "**New SHiFT code**"),
        (2, "**New SHiFT code**"),
    ]
    assert_ping_integrity(h)


@pytest.mark.parametrize("seed_value", range(12))
async def test_random_failures_never_leave_a_header_that_lies_about_its_ping(
    db_path, cfg, seed_value
):
    rng = random.Random(seed_value)  # noqa: S311
    rig = Rig(db_path, cfg, NOW)
    seed(db_path)
    rig.reserve = BIG_RESERVE
    for guild_id, ping in ((1, "everyone"), (2, ROLE), (3, "none")):
        add_guild(db_path, guild_id, ping=ping)
        rig.script[guild_id] = [
            rng.choice([None, None, AMBIGUOUS, refused()]) for _ in range(rng.randint(0, 9))
        ]
    codes = n_codes(rng.randint(1, 8))

    await rig.run(distinct(*codes, trust=rng.choice(["official", "community"])))

    assert_ping_integrity(rig)
    assert_records_match_what_posted(rig)


def test_no_second_ping_path_arrived_with_delivery_details():
    """The mentions tripwire scans every file; this one keeps the delivery files out of it."""
    for name in ("shift/fanout.py", "store/repo.py", "bot/format.py", "shift/sweep.py"):
        tree = ast.parse((ROOT / "newsbot" / name).read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == "AllowedMentions"
        ]
        assert calls == [], f"{name} builds AllowedMentions"
    client = ast.parse((ROOT / "newsbot" / "bot" / "client.py").read_text())
    everyone_true = [
        node
        for node in ast.walk(client)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None)) == "AllowedMentions"
        for kw in node.keywords
        if kw.arg == "everyone" and getattr(kw.value, "value", None) is True
    ]
    assert len(everyone_true) == 1


@pytest.mark.parametrize(
    "hostile",
    [
        "@everyone",
        "@here is a reward",
        "<@&123456789012345678>",
        "<@123456789012345678>",
        "<#123456789012345678>",
        "@\u200beveryone",
        "@\u202eeveryone",
        "[click](https://evil.example) now",
        "```\n@everyone\n```",
    ],
)
async def test_hostile_reward_text_never_adds_a_ping_or_breaks_the_fences(h, hostile):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="everyone")
    store_details(h.db_path, CODE_A, reward=hostile)

    await h.run([item(CODE_A)])

    unpinged, pinged = h.sent(1)[0], h.sent(2)[0]
    for alert in (unpinged, pinged):
        body = alert.content[len(alert.ping_prefix) :]
        assert LIVE_MENTION.search(body) is None
        assert alert.content.count("```") == 2  # only the code's own fence
    assert not LIVE_MENTION.search(unpinged.content)
    assert pinged.content.startswith("@everyone ")
    assert not unpinged.ping


# --- 4. the reserve ---

PACIFIC_LATE = datetime(2026, 10, 2, 6, 30, tzinfo=UTC)  # 23:30 on Oct 1 in Los Angeles
LONGEST_PHRASE_DAY = date(2026, 10, 1)  # "expires in under an hour" at PACIFIC_LATE


def with_sources(cfg, *, catalog=None, shared=None, trust=None):
    """`cfg` with its sources replaced (or re-trusted), by editing the models directly."""
    games = []
    for game in cfg.catalog:
        sources = game.sources if catalog is None else catalog(game)
        if trust is not None:
            sources = [s.model_copy(update={"trust": trust}) for s in sources]
        games.append(game.model_copy(update={"sources": sources}))
    shared_sources = cfg.shared_sources if shared is None else shared
    if trust is not None:
        shared_sources = [s.model_copy(update={"trust": trust}) for s in shared_sources]
    return cfg.model_copy(update={"catalog": games, "shared_sources": shared_sources})


def table_source(name):
    from newsbot.config import ShiftTableSource

    return ShiftTableSource(
        type="shift_table", name=name, url="https://codes.example/page/", trust="community"
    )


def worst_row(code, *, reward_from, expires_from, reward=None):
    """The biggest details row a code can have: 300 shown units, an expiry, two "via" names."""
    return repo.CodeDetailsRow(
        code=code,
        clock_start=NOW,
        updated_at=NOW,
        reward=reward if reward is not None else "x" * 300,
        reward_source="table",
        reward_from=reward_from,
        expires_on=LONGEST_PHRASE_DAY,
        expires_source="table",
        expires_from=expires_from,
        details_changed_at=NOW,
    )


def test_a_config_with_no_official_or_table_source_still_reserves_a_nameless_line(cfg):
    quiet = with_sources(cfg, trust="community")

    assert reserve_for_config(quiet) == details_reserve([])
    assert reserve_for_config(quiet) == 1 + 300 + 3 + 24 + 7 + 5  # every part but the names


def test_a_config_with_no_sources_at_all_does_not_crash_the_reserve(cfg):
    empty = with_sources(cfg, catalog=lambda _game: [], shared=[])

    assert reserve_for_config(empty) == details_reserve([])


def test_the_reserve_with_a_100_character_markdown_name_is_exactly_the_worst_line(cfg):
    other = "_" * 100
    wide = with_sources(
        cfg,
        catalog=lambda game: (
            [*game.sources, table_source(STAR_NAME)] if game.key == "borderlands4" else game.sources
        ),
    )

    reserve = reserve_for_config(wide)

    assert reserve == 1 + 300 + 3 + 24 + 7 + 200 + 5 + 200 == 740
    row = worst_row(CODE_A, reward_from=STAR_NAME, expires_from=other)
    line = details_line(row, now=PACIFIC_LATE)
    assert "expires in under an hour" in line
    assert discord_len(line) + 1 == reserve  # tight to the unit: nothing to spare, nothing over


def test_a_table_source_counts_even_though_it_is_only_community(cfg):
    wide = with_sources(
        cfg,
        catalog=lambda game: (
            [*game.sources, table_source("T" * 100)] if game.key == "borderlands4" else game.sources
        ),
    )

    assert reserve_for_config(wide) == details_reserve(["T" * 100])


def test_a_community_source_with_a_long_name_does_not_widen_the_reserve(cfg):
    rss = cfg.shared_sources[0].model_copy(update={"name": "C" * 100, "trust": "community"})
    chatty = cfg.model_copy(update={"shared_sources": [*cfg.shared_sources, rss]})

    assert reserve_for_config(chatty) == reserve_for_config(cfg)


def test_a_shared_official_source_with_a_long_name_widens_the_reserve(cfg):
    shared = [
        s.model_copy(update={"name": "S" * 100}) if s.trust == "official" else s
        for s in cfg.shared_sources
    ]

    assert reserve_for_config(with_sources(cfg, shared=shared)) == details_reserve(["S" * 100])


def _raw_names(path: Path) -> set[str]:
    """Official or table source names that can reach a SHiFT alert, read straight out of the YAML.

    The slow independent way. QA batch 1, fix 6: only sources detection can read for a code
    count: under a game in `shift.games` (v3), or on a topic in `alerts.topics` (v2) or with no
    topic at all; an empty list means every game, the v2 reading, and a v3 file that leaves
    `shift.games` out means Borderlands 4.
    """
    import yaml

    raw = yaml.safe_load(path.read_text())
    games = set(
        (raw.get("shift") or {}).get("games") or (raw.get("alerts") or {}).get("topics") or []
    )
    if not games and "catalog" in raw:
        games = {"borderlands4"}

    def in_scope(source: dict, key: str) -> bool:
        listed = source.get(key)
        return not games or not listed or bool(games & set(listed))

    sources = [s for s in raw.get("sources", []) if in_scope(s, "topics")]
    sources += [s for s in raw.get("shared_sources", []) if in_scope(s, "games")]
    for game in raw.get("catalog", []):
        if not games or game["key"] in games:
            sources += game.get("sources", [])
    names = set()
    for source in sources:
        if source.get("trust") == "official" or source.get("type") == "shift_table":
            names.add(source.get("name") or f"Bluesky: {source.get('query')}")
    return names


@pytest.mark.parametrize(
    "name",
    [
        "tests/fixtures/config_v2_example.yaml",
        "tests/fixtures/config_v2_minimal.yaml",
        "tests/fixtures/config_v2_prodlike.yaml",
        "tests/fixtures/config_lounge.yaml",
        "tests/fixtures/config_valid.yaml",
        "tests/fixtures/config_v3.yaml",
        "config.example.yaml",
        "config.minimal.yaml",
    ],
)
def test_the_reserve_follows_the_names_in_every_shape_of_config_we_ship(name):
    path = ROOT / name

    reserve = reserve_for_config(load_config(path))

    assert reserve == details_reserve(_raw_names(path))
    assert reserve >= details_reserve([])


# A first render never exceeds Discord's cap, whatever the "via" names stored on a row say.


async def test_a_row_whose_via_name_left_the_config_still_posts_within_2000(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = reserve_for_config(h.cfg)  # small: the config no longer has a 100-character name
    codes = [CODE_A, CODE_B, CODE_C, CODE_D]
    for code in codes:
        store_details(h.db_path, code, reward="r" * 200, name=STAR_NAME)

    await h.run(distinct(*codes))

    assert h.delivered, "nothing posted"
    assert all(discord_len(d.alert.content) <= 2000 for d in h.delivered)
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_posts")} == {"posted"}
    assert_records_match_what_posted(h)


@pytest.mark.parametrize("reward_length", [1, 100, 150, 195, 203, 250, 300])
async def test_worst_case_stale_rows_in_bulk_never_push_a_first_render_over_2000(h, reward_length):
    add_guild(h.db_path, 1, ping=ROLE)
    h.reserve = reserve_for_config(h.cfg)
    codes = n_codes(12)
    for code in codes:
        store_details(h.db_path, code, reward="r" * reward_length, name=STAR_NAME)

    await h.run(distinct(*codes))

    assert all(discord_len(d.alert.content) <= 2000 for d in h.delivered)
    assert [c for d in h.delivered for c in d.alert.codes] == codes
    assert_records_match_what_posted(h)


def _candidates(*codes):
    from newsbot.shift.decide import CodeCandidate

    return [
        CodeCandidate(
            code=c,
            golden=False,
            source_name="Gearbox Blog",
            item_url=f"https://example.com/{c[:4]}",
            fresh=True,
        )
        for c in codes
    ]


def _stale_then_grown_edit(*, first_from, grown_expires_from, reward_length, stored_names=()):
    """Render four codes as first posted, then the edit a later details change would make.

    `stored_names` are the "via" names still on rows in the database; delivery hands them to
    the reserve (`stored_detail_source_names`), and so does this.
    """
    from newsbot.bot.format import render_alert_edit, render_code_alerts

    cs = _candidates(CODE_A, CODE_B, CODE_C, CODE_D)
    first = {
        c.code: repo.CodeDetailsRow(
            code=c.code,
            clock_start=NOW,
            updated_at=NOW,
            reward="r" * reward_length,
            reward_source="table",
            reward_from=first_from,
            details_changed_at=NOW,
        )
        for c in cs
    }
    # What `_deliver_one` does: the config's floor, widened by the names still on rows.
    reserve = reserve_for_config(load_config(V3))
    if stored_names:
        reserve = max(reserve, details_reserve(stored_names))
    alerts = render_code_alerts(
        cs, ping=True, ping_mention="@everyone", details=first, now=NOW, reserve=reserve
    )
    grown = {
        code: replace(
            row,
            expires_on=date(2026, 10, 30),
            expires_source="table",
            expires_from=grown_expires_from,
        )
        for code, row in first.items()
    }
    # A wider reserve may split the four codes over several messages; the longest edit is the
    # one that has to fit.
    edits = [
        render_alert_edit(
            alert.header,
            [c for c in cs if c.code in alert.codes],
            golden_prefix=False,
            details=grown,
            now=NOW,
        )
        for alert in alerts
    ]
    return max(edits, key=discord_len)


@pytest.mark.parametrize("reward_length", range(150, 301, 5))
def test_an_edit_that_adds_an_expiry_fits_when_the_via_names_are_in_the_config(reward_length):
    edited = _stale_then_grown_edit(
        first_from="Borderlands 4 Steam",
        grown_expires_from="2K Newsroom",
        reward_length=reward_length,
    )
    assert discord_len(edited) <= 2000


def test_an_edit_that_adds_an_expiry_still_fits_after_a_via_name_left_the_config():
    edited = _stale_then_grown_edit(
        first_from=STAR_NAME,
        grown_expires_from="2K Newsroom",
        reward_length=200,
        stored_names=[STAR_NAME],
    )
    assert discord_len(edited) <= 2000


# Packing with the reserve, against the walk's time budget and pacing.


async def test_a_server_already_started_finishes_its_batch_and_the_rest_wait_for_the_next_pass(h):
    for guild_id in (1, 2, 3, 4):
        add_guild(h.db_path, guild_id, ping="everyone")
    h.reserve = BIG_RESERVE

    def each_send_takes_a_minute(_guild_id, _alert):
        h.mono += 60.0

    h.on_post = each_send_takes_a_minute

    await h.run(distinct(CODE_A, CODE_B, CODE_C))

    # Server 1's two messages both went out (120 s in), then the walk stopped starting servers.
    assert {d.guild_id for d in h.delivered} == {1}
    assert len(h.delivered) == 2
    assert h.sleeps == [0.2]  # paced once before the deadline check cut the walk
    assert [
        (g, s) for g, s in rows(h.db_path, "SELECT guild_id, status FROM guild_code_posts") if g > 1
    ] == [(g, "queued") for g in (2, 2, 2, 3, 3, 3, 4, 4, 4)]

    h.mono = 0.0
    h.on_post = None
    await h.deliver()  # the next pass

    assert sorted(d.guild_id for d in h.delivered) == [1, 1, 2, 2, 3, 3, 4, 4]
    assert len(message_rows(h.db_path)) == 8  # one record per message, none twice
    assert_records_match_what_posted(h)
    assert h.sleeps == [0.2] * 3  # one more walk over servers 2 to 4: paced between servers only


async def test_many_servers_are_paced_once_each_and_each_batch_packs_under_2000(h):
    for guild_id in range(1, 7):
        add_guild(h.db_path, guild_id, ping="everyone")
    h.reserve = reserve_for_config(h.cfg)
    codes = n_codes(25)

    await h.run(distinct(*codes))

    assert h.sleeps == [0.2] * 5
    assert all(discord_len(d.alert.content) <= 2000 for d in h.delivered)
    per_guild = {}
    for d in h.delivered:
        per_guild.setdefault(d.guild_id, []).append(d.alert)
    assert set(per_guild) == set(range(1, 7))
    for alerts in per_guild.values():
        assert [c for a in alerts for c in a.codes] == codes
        assert [a.ping for a in alerts] == [True] + [False] * (len(alerts) - 1)
    assert_ping_integrity(h)
    assert_records_match_what_posted(h)


async def test_a_send_cancelled_mid_batch_leaves_no_record_and_is_never_resent(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    h.script[1] = [None]
    sent = []

    def cancel_second(_guild_id, alert):
        sent.append(alert)
        if len(sent) == 2:
            raise asyncio.CancelledError

    h.on_post = cancel_second

    with pytest.raises(asyncio.CancelledError):
        await deliver_after_release(h)

    # The first message is posted and recorded; the cancelled one is `pending` with no record.
    assert [r[3] for r in message_rows(h.db_path)] == ["@everyone **New SHiFT codes**"]
    assert dict(rows(h.db_path, "SELECT code, status FROM guild_code_posts")) == {
        CODE_A: "posted",
        CODE_B: "posted",
        CODE_C: "pending",
    }
    h.on_post = None
    await h.deliver()
    assert len(h.delivered) == 1  # pending is never sent again


async def deliver_after_release(h: Rig):
    """Release three codes (nobody delivers: no poster raises), then deliver so a hook can fire."""
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn,
            [
                (c, "Gearbox Blog", f"https://example.com/{c[:4]}", False)
                for c in (CODE_A, CODE_B, CODE_C)
            ],
            now=lambda: NOW,
            queue_for_games=["borderlands4"],
            flags={c: (False, True) for c in (CODE_A, CODE_B, CODE_C)},
        )
    return await h.deliver()


# --- 5. follow-ups that fit by size and drain over several passes ---

LONG_REWARD = "x" * 300  # five of these (plus their codes) fill a follow-up; six don't


async def first_pass_and_confirm(h: Rig, codes: list[str], *, details=True) -> None:
    await h.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    if details:
        for code in codes:
            store_details(h.db_path, code, reward=LONG_REWARD)
    h.clock = NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])


def followups(h: Rig) -> list[RenderedAlert]:
    return [a for a in h.sent(1) if a.content.startswith(("@everyone", "<@&"))]


def followup_statuses(h: Rig) -> dict[str, str]:
    return dict(rows(h.db_path, "SELECT code, status FROM guild_code_followups"))


async def test_a_backlog_of_followups_drains_over_passes_with_one_ping_each_and_no_repeats(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(14)

    await first_pass_and_confirm(h, codes)
    for _ in range(3):
        h.clock += timedelta(hours=1)
        await h.run([])

    sent = [a for a in h.sent(1) if "Confirmed by a second source" in a.content]
    assert [len(a.codes) for a in sent] == [5, 5, 4]
    assert [c for a in sent for c in a.codes] == codes  # in queue order, none twice, none lost
    assert all(a.ping and a.content.startswith("@everyone ") for a in sent)
    assert all(discord_len(a.content) <= 2000 for a in sent)
    assert shift_row(h.db_path, 1).ping_count == 3
    assert set(followup_statuses(h).values()) == {"posted"}
    assert (
        len(message_rows(h.db_path)) == 1
    )  # the 14 originals went out as one alert; follow-ups add none


async def test_a_fourth_pass_is_stopped_by_the_daily_cap_and_nothing_goes_out_twice(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(16)

    await first_pass_and_confirm(h, codes)
    for _ in range(4):  # one more than the cap allows
        h.clock += timedelta(hours=1)
        await h.run([])

    sent = [a for a in h.sent(1) if "Confirmed by a second source" in a.content]
    assert [len(a.codes) for a in sent] == [5, 5, 5]
    assert shift_row(h.db_path, 1).ping_count == 3  # the cap, never 4
    statuses = followup_statuses(h)
    assert statuses[codes[-1]] == "skipped"  # not "queued forever", and not posted unpinged
    assert [s for c, s in statuses.items() if c != codes[-1]] == ["posted"] * 15
    posted_codes = [c for a in sent for c in a.codes]
    assert len(posted_codes) == len(set(posted_codes))


async def test_one_ping_per_pass_even_when_a_pass_has_both_new_codes_and_followups(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(8)
    await first_pass_and_confirm(h, codes)  # the confirming pass already sent follow-up number 1
    before = shift_row(h.db_path, 1).ping_count

    h.clock += timedelta(hours=1)
    await h.run(
        [item(CODE_A, url="https://example.com/fresh")]
    )  # an official code, and a follow-up due

    pings = [a for a in h.sent(1) if carries_a_ping(a.content)]
    assert shift_row(h.db_path, 1).ping_count == before + 2  # one alert ping, one follow-up ping
    assert len(pings) == 3  # the confirming pass's follow-up, then this pass's alert and follow-up
    assert_records_match_what_posted(h)


async def test_a_code_that_expired_inside_a_queued_batch_is_skipped_and_costs_no_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(8)
    await first_pass_and_confirm(h, codes)  # 5 went out; codes[5:] are queued
    assert followup_statuses(h)[codes[5]] == "queued"
    store_expiry(h.db_path, codes[6], date(2026, 9, 30), now=h.clock)

    h.clock += timedelta(hours=1)
    await h.run([])

    last = [a for a in h.sent(1) if "Confirmed by a second source" in a.content][-1]
    assert last.codes == [codes[5], codes[7]]
    assert codes[6] not in last.content
    assert followup_statuses(h)[codes[6]] == "skipped"
    assert shift_row(h.db_path, 1).ping_count == 2


async def test_a_batch_of_nothing_but_expired_codes_posts_nothing_and_spends_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(2)
    await h.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    h.clock = NOW + timedelta(hours=20)
    # Confirmed and queued, but not delivered yet (release only, no walk)...
    await fanout._release(
        h.deps(), [sighting(BLUESKY, "community", c, h.clock) for c in codes], seeding_ok=True
    )
    assert set(followup_statuses(h).values()) == {"queued"}
    # ...and then both die before the walk gets to them.
    for code in codes:
        store_expiry(h.db_path, code, date(2026, 9, 30), now=h.clock)
    h.created.clear()

    await h.deliver()

    assert not any("Confirmed" in a.content for a in h.sent(1))
    assert h.created == []  # no poster was even asked for
    assert shift_row(h.db_path, 1).ping_count == 0
    assert set(followup_statuses(h).values()) == {"skipped"}


async def test_a_code_known_to_be_expired_at_confirmation_is_never_queued(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(2)
    await h.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    for code in codes:
        store_expiry(h.db_path, code, date(2026, 9, 30), now=NOW)
    h.clock = NOW + timedelta(hours=20)

    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])

    assert not any("Confirmed" in a.content for a in h.sent(1))
    assert shift_row(h.db_path, 1).ping_count == 0


async def test_details_that_change_between_passes_show_as_they_stand_in_each_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(7)
    await first_pass_and_confirm(h, codes)  # 5 sent, with LONG_REWARD
    first = [a for a in h.sent(1) if "Confirmed" in a.content][0]
    assert LONG_REWARD in first.content

    store_details(
        h.db_path, codes[5], reward="Now: 9 Golden Keys", now=h.clock + timedelta(minutes=1)
    )
    h.clock += timedelta(hours=1)
    await h.run([])

    second = [a for a in h.sent(1) if "Confirmed" in a.content][1]
    assert f"`{codes[5]}` · Now: 9 Golden Keys · via {MENTALMARS}" in second.content
    assert f"`{codes[6]}` · {LONG_REWARD}" in second.content


async def test_an_expiry_phrase_is_recomputed_for_a_followup_that_waited_a_day(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(6)
    await first_pass_and_confirm(h, codes)  # 5 sent at Oct 2 01:00 Pacific; codes[5] waits
    store_expiry(h.db_path, codes[5], date(2026, 10, 3), now=h.clock)  # "tomorrow" today

    h.clock += timedelta(days=1, hours=18)  # Oct 3 19:00 Pacific
    await h.run([])

    last = [a for a in h.sent(1) if "Confirmed" in a.content][-1]
    assert "expires in 5 hours" in last.content


async def test_a_followup_mixing_codes_with_and_without_details_keeps_plain_rows(h):
    add_guild(h.db_path, 1, ping=ROLE)
    codes = n_codes(3)
    await h.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    store_details(h.db_path, codes[1], reward="A skin")
    h.clock = NOW + timedelta(hours=20)

    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])

    (followup,) = [a for a in h.sent(1) if "Confirmed" in a.content]
    assert followup.content == (
        f"<@&{ROLE}> Confirmed by a second source:\n"
        f"`{codes[0]}`\n"
        f"`{codes[1]}` · A skin · via {MENTALMARS}\n"
        f"`{codes[2]}`"
    )


async def test_twenty_five_plain_followups_send_twenty_then_five(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = n_codes(25)

    await first_pass_and_confirm(h, codes, details=False)
    h.clock += timedelta(hours=1)
    await h.run([])

    sent = [a for a in h.sent(1) if "Confirmed" in a.content]
    assert [len(a.codes) for a in sent] == [20, 5]
    assert [c for a in sent for c in a.codes] == codes
    assert sent[0].content.startswith("@everyone Confirmed by a second source: `")  # one-liner


async def test_a_followup_with_a_worst_case_line_per_code_still_fits_and_waits_for_the_rest(h):
    add_guild(h.db_path, 1, ping=ROLE)
    codes = n_codes(6)
    await h.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    for code in codes:
        with closing(connect(h.db_path)) as conn:
            repo.upsert_code_details(
                conn,
                [
                    DetailObservation(
                        code,
                        ItemDetails(
                            code=code,
                            rank="table",
                            reward="*" * 150,
                            expires_on=date(2026, 12, 25),
                            listed=True,
                        ),
                        STAR_NAME,
                        None,
                    )
                ],
                now=NOW,
            )
    h.clock = NOW + timedelta(hours=20)

    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])

    (first,) = [a for a in h.sent(1) if "Confirmed" in a.content]
    assert discord_len(first.content) <= 2000
    assert 0 < len(first.codes) < 6
    waiting = [c for c, s in followup_statuses(h).items() if s == "queued"]
    assert waiting == codes[len(first.codes) :]


def store_expiry(db_path, code, day, *, now):
    obs = DetailObservation(
        code, ItemDetails(code=code, rank="table", expires_on=day, listed=True), MENTALMARS, None
    )
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(conn, [obs], now=now)


# --- 6a. roundups: never recorded, shown with details, never reserved for ---

MANY = [f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")]


async def test_a_roundup_beside_a_normal_alert_records_only_the_normal_message(h):
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, MANY[0], reward="A skin")

    await h.run([item(CODE_A), item(*MANY, url="https://example.com/megathread")])

    roundup_ids = {
        m
        for (m,) in rows(
            h.db_path, "SELECT DISTINCT message_id FROM guild_code_posts WHERE from_roundup = 1"
        )
    }
    recorded = {r[1] for r in message_rows(h.db_path)}
    assert roundup_ids and recorded and not (roundup_ids & recorded)
    assert len(recorded) == 1
    assert_records_match_what_posted(h)
    assert any("A skin" in a.content for a in h.sent(1))


async def test_roundup_text_is_the_same_with_the_reserve_on_or_off(tmp_path, cfg):
    contents = {}
    for name, reserve in (("off", 0), ("on", BIG_RESERVE)):
        rig = Rig(str(tmp_path / f"{name}.db"), cfg, NOW)
        with closing(connect(rig.db_path)) as conn:
            migrate(conn)
        seed(rig.db_path)
        add_guild(rig.db_path, 1, ping="everyone")
        rig.reserve = reserve
        for code in MANY:
            store_details(rig.db_path, code, reward="Reward " + code[:4])

        await rig.run([item(*MANY, url="https://example.com/megathread")])

        contents[name] = [(a.content, a.codes, a.nonce) for a in rig.sent(1)]

    assert contents["off"] == contents["on"]
    assert "Reward QQQQ" in contents["off"][0][0]


async def test_worst_case_details_on_a_roundup_still_post_every_code_under_2000(h):
    add_guild(h.db_path, 1, ping="everyone")
    for code in MANY:
        with closing(connect(h.db_path)) as conn:
            repo.upsert_code_details(
                conn,
                [
                    DetailObservation(
                        code,
                        ItemDetails(
                            code=code,
                            rank="table",
                            reward="*" * 150,
                            expires_on=date(2026, 12, 25),
                            listed=True,
                        ),
                        STAR_NAME,
                        None,
                    )
                ],
                now=NOW,
            )
    nasty = replace(item(*MANY, url="https://example.com/" + "a/" * 400), source_name=STAR_NAME)

    await h.run([nasty])

    assert [c for d in h.delivered for c in d.alert.codes] == MANY
    assert all(discord_len(d.alert.content) <= 2000 for d in h.delivered)
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_posts")} == {"posted"}
    assert message_rows(h.db_path) == []


async def test_a_partial_roundup_claim_keeps_details_and_still_records_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, MANY[2], reward="Kept reward")
    h.on_begin = lambda guild_id: claim_elsewhere(h, guild_id, MANY[0])

    await h.run([item(*MANY, url="https://example.com/megathread")])

    sent_codes = [c for d in h.delivered for c in d.alert.codes]
    assert MANY[0] not in sent_codes and MANY[2] in sent_codes
    assert any("Kept reward" in d.alert.content for d in h.delivered)
    assert message_rows(h.db_path) == []


# --- 6b. bulk: one server with a very long queue ---


def test_a_long_queue_reads_back_with_every_details_row(db_path):
    add_guild(db_path, 1, ping="none")
    codes = [f"{i:05d}-AAAAA-BBBBB-CCCCC-DDDDD" for i in range(1, 1201)]
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn,
            [(c, "Gearbox Blog", f"https://example.com/{c[:5]}", False) for c in codes],
            now=lambda: NOW,
            queue_for_games=["borderlands4"],
            flags={},
        )
        repo.upsert_code_details(
            conn,
            [
                DetailObservation(
                    c, ItemDetails(code=c, rank="table", reward=f"r{c[:5]}", listed=True), "m", None
                )
                for c in codes
            ],
            now=NOW,
        )
        queued = repo.queued_guild_codes(conn, 1)

    assert [q.code for q in queued] == codes
    assert all(q.details is not None and q.details.reward == f"r{q.code[:5]}" for q in queued)


# --- 6c. the CLI preview ---


def snapshot_everything(db_path) -> dict[str, list[tuple]]:
    with closing(connect(db_path)) as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            t: sorted((tuple(r) for r in conn.execute(f"SELECT * FROM {t}")), key=repr)  # noqa: S608
            for t in sorted(tables)
        }


async def test_the_cli_preview_changes_nothing_durable_and_prints_details(h, capsys):
    for guild_id in (1, 2, 3, 4):
        add_guild(h.db_path, guild_id, ping="everyone")
    h.reserve = reserve_for_config(h.cfg)
    # A community code with a follow-up waiting, a roundup, and normal codes with details.
    community = [numbered(1), numbered(2)]
    await h.run([sighting(REDDIT, "community", c, NOW) for c in community])
    h.clock = NOW + timedelta(hours=20)
    await fanout._release(
        h.deps(), [sighting(BLUESKY, "community", c, h.clock) for c in community], seeding_ok=True
    )
    store_details(h.db_path, CODE_A, reward="3 Golden Keys")
    fresh = [with_details(item(CODE_A), CODE_A, expires_on=date(2026, 10, 3))]
    fresh += distinct(CODE_B, CODE_C)
    await fanout._release(h.deps(), fresh, seeding_ok=True)
    # Guild 3 was switched on after the release (stale), guild 4 was switched off (skipped).
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "UPDATE guild_shift SET enabled_at = ? WHERE guild_id = 3",
            ((h.clock + timedelta(hours=2)).isoformat(),),
        )
        conn.execute("UPDATE guild_shift SET enabled = 0 WHERE guild_id = 4")
    assert {
        s
        for (s,) in rows(
            h.db_path, "SELECT status FROM guild_code_posts WHERE code NOT IN (?, ?)", *community
        )
    } == {"queued"}
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_followups")} == {"queued"}
    before = snapshot_everything(h.db_path)
    delivered_before = len(h.delivered)
    capsys.readouterr()

    preview = h.deps(preview=True, poster_for=lambda *_a: PrintCodeAlertPoster())
    await deliver_queued_codes(preview)
    await detect_and_fan_out(preview, [], seeding_ok=True)
    await deliver_queued_codes(preview)

    printed = capsys.readouterr().out
    assert "3 Golden Keys" in printed and CODE_B in printed
    assert snapshot_everything(h.db_path) == before
    assert len(h.delivered) == delivered_before


async def test_the_cli_preview_of_a_roundup_alone_prints_it_with_details_and_changes_nothing(
    h, capsys
):
    add_guild(h.db_path, 1, ping="everyone")
    store_details(h.db_path, MANY[0], reward="A skin")
    await fanout._release(
        h.deps(), [item(*MANY, url="https://example.com/megathread")], seeding_ok=True
    )
    before = snapshot_everything(h.db_path)
    capsys.readouterr()

    preview = h.deps(preview=True, poster_for=lambda *_a: PrintCodeAlertPoster())
    await deliver_queued_codes(preview)

    printed = capsys.readouterr().out
    assert MANY[0] in printed and "A skin" in printed
    assert snapshot_everything(h.db_path) == before


async def test_the_cli_preview_prints_roundup_codes_that_ride_along_with_normal_ones(h, capsys):
    add_guild(h.db_path, 1, ping="everyone")
    await fanout._release(
        h.deps(),
        [item(CODE_A), item(*MANY, url="https://example.com/megathread")],
        seeding_ok=True,
    )
    capsys.readouterr()

    preview = h.deps(preview=True, poster_for=lambda *_a: PrintCodeAlertPoster())
    await deliver_queued_codes(preview)

    printed = capsys.readouterr().out
    assert CODE_A in printed and MANY[0] in printed


async def test_the_cli_preview_release_writes_no_alert_message_record(h, capsys):
    add_guild(h.db_path, 1, ping="everyone")
    preview = h.deps(preview=True, poster_for=lambda *_a: PrintCodeAlertPoster())

    await detect_and_fan_out(preview, distinct(CODE_A, CODE_B), seeding_ok=True)

    assert CODE_A in capsys.readouterr().out
    assert message_rows(h.db_path) == []
    assert {s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_posts")} == {"queued"}
    assert shift_row(h.db_path, 1).ping_count == 0


async def test_the_collect_cli_hands_its_fanout_the_reserve_and_the_preview_flag(
    cfg, db_path, monkeypatch
):
    from newsbot.pipeline import run

    made = []

    class Spy(FanoutDeps):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(fanout, "FanoutDeps", Spy)

    await run._run_collect_cli(cfg, db_path, [], lambda: NOW)

    (deps,) = made
    assert deps.preview is True
    assert deps.details_reserve == reserve_for_config(cfg) > 0


async def test_the_bot_wires_the_reserve_into_its_fanout(v3_cfg, db_path):
    from test_client_wiring_v3 import _secrets, _setup, _stopped

    from newsbot.bot.client import NewsBot

    bot = NewsBot(v3_cfg, _secrets(), db_path)
    await _setup(bot)
    try:
        assert bot._fanout_deps.details_reserve == reserve_for_config(v3_cfg)
        assert bot._fanout_deps.details_reserve > details_reserve([])
        assert bot._fanout_deps.preview is False
    finally:
        await _stopped(bot)


# --- 6d. retention ---


def put_record(db_path, guild_id, message_id, posted_at):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO guild_alert_messages "
            "(guild_id, message_id, channel_id, header, golden_prefix, posted_at, rendered_at) "
            "VALUES (?, ?, 100, 'h', 0, ?, ?)",
            (guild_id, message_id, posted_at.isoformat(), posted_at.isoformat()),
        )


def purge(db_path, now):
    with closing(connect(db_path)) as conn:
        repo.purge_guild_data_older_than(conn, now - timedelta(days=90), now=lambda: now)


def surviving(db_path) -> set[int]:
    return {m for (m,) in rows(db_path, "SELECT message_id FROM guild_alert_messages")}


def test_records_older_than_30_days_are_purged_and_younger_ones_stay(db_path):
    add_guild(db_path, 1)
    edge = timedelta(days=30)
    for message_id, age in (
        (1, edge + timedelta(seconds=1)),
        (2, edge),
        (3, edge - timedelta(seconds=1)),
        (4, timedelta(days=90)),
        (5, timedelta(0)),
    ):
        put_record(db_path, 1, message_id, NOW - age)

    purge(db_path, NOW)

    assert surviving(db_path) == {2, 3, 5}  # exactly 30 days old is not yet "older than"


def test_no_record_is_purged_while_its_codes_are_still_inside_the_14_day_edit_window(db_path):
    add_guild(db_path, 1)
    edit_window = timedelta(days=14)
    ages = [timedelta(hours=h) for h in range(0, 14 * 24 + 1)] + [
        edit_window,
        edit_window + timedelta(seconds=1),
        edit_window + timedelta(days=1),
    ]
    for message_id, age in enumerate(ages, start=1):
        put_record(db_path, 1, message_id, NOW - age)

    purge(db_path, NOW)
    # Whatever the pass that purges is doing now, a purge a minute from now keeps them too.
    purge(db_path, NOW + timedelta(minutes=1))

    assert surviving(db_path) == set(range(1, len(ages) + 1))
    assert repo.ALERT_MESSAGE_RETENTION > edit_window


def test_the_purge_counts_30_days_in_absolute_time_across_a_dst_change(db_path):
    from zoneinfo import ZoneInfo

    add_guild(db_path, 1)
    la = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 11, 3, 12, 0, tzinfo=la)  # DST ended on Nov 1: 30 wall-clock days back
    cutoff = now.astimezone(UTC) - timedelta(days=30)  # is not the same instant as 30 elapsed
    put_record(db_path, 1, 1, cutoff - timedelta(minutes=30))
    put_record(db_path, 1, 2, cutoff + timedelta(minutes=30))

    purge(db_path, now)

    assert surviving(db_path) == {2}


def test_the_purge_leaves_the_posts_and_other_servers_records_alone(db_path):
    add_guild(db_path, 1)
    add_guild(db_path, 2)
    old = NOW - timedelta(days=31)
    put_record(db_path, 1, 1, old)
    put_record(db_path, 2, 1, NOW)
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn,
            [(CODE_A, "s", "https://x.test", False)],
            now=lambda: NOW,
            queue_for_games=["borderlands4"],
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-09-01")
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=1)

    purge(db_path, NOW)

    assert rows(db_path, "SELECT guild_id, message_id FROM guild_alert_messages") == [(2, 1)]
    assert rows(db_path, "SELECT guild_id, status, message_id FROM guild_code_posts") == [
        (1, "posted", 1),
        (2, "queued", None),
    ]
    purge(db_path, NOW)  # idempotent
    assert rows(db_path, "SELECT guild_id FROM guild_alert_messages") == [(2,)]


async def test_a_record_written_by_delivery_survives_a_purge_a_fortnight_later(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])

    purge(h.db_path, NOW + timedelta(days=14, hours=23))

    assert len(message_rows(h.db_path)) == 1
    purge(h.db_path, NOW + timedelta(days=31))
    assert message_rows(h.db_path) == []


# --- more ping and packing attacks, added once the groups above were standing ---


async def test_a_golden_code_in_the_first_message_only_still_stores_the_mixed_flag_for_both(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE
    golden = item(CODE_A, title="Borderlands 4 Golden Key SHiFT code: " + CODE_A, url="https://e/a")

    await h.run([golden, *distinct(CODE_B, CODE_C, CODE_D)])

    got = message_rows(h.db_path)
    assert len(got) == 2
    assert [r[4] for r in got] == [1, 1]  # the continuation holds no golden code, same flag
    assert f"Golden Key: {GEARBOX}" in h.delivered[0].alert.content
    assert "Golden Key: " not in h.delivered[1].alert.content
    assert_records_match_what_posted(h)


async def test_the_cap_spent_by_someone_else_between_render_and_claim_stores_no_ping(h):
    add_guild(h.db_path, 1, ping="everyone")

    def another_pass_spends_the_last_ping(guild_id):
        with closing(connect(h.db_path)) as conn, conn:
            conn.execute(
                "UPDATE guild_shift SET ping_day = '2026-10-01', ping_count = 3 WHERE guild_id = ?",
                (guild_id,),
            )

    h.on_begin = another_pass_spends_the_last_ping

    await h.run([item(CODE_A)])

    assert not carries_a_ping(h.delivered[0].alert.content)
    assert [r[3] for r in message_rows(h.db_path)] == ["**New SHiFT code**"]
    assert shift_row(h.db_path, 1).ping_count == 3
    assert_ping_integrity(h)


async def test_the_only_trusted_code_claimed_elsewhere_drops_the_ping_from_header_and_record(h):
    add_guild(h.db_path, 1, ping="everyone")
    trusted = item(CODE_A, url="https://example.com/a")
    untrusted = item(CODE_B, trust="community", url="https://example.com/b")
    h.on_begin = lambda guild_id: claim_elsewhere(h, guild_id, CODE_A)

    await h.run([trusted, untrusted])

    assert [d.alert.codes for d in h.delivered] == [[CODE_B]]
    assert [r[3] for r in message_rows(h.db_path)] == ["**New SHiFT code**"]
    assert shift_row(h.db_path, 1).ping_count == 0
    assert_ping_integrity(h)
    assert_records_match_what_posted(h)


def _blocks(content: str) -> list[str]:
    return content.split("\n\n")[1:]


@pytest.mark.parametrize("reserve", [0, 100, 340, 378, 740, BIG_RESERVE])
async def test_packing_with_a_reserve_is_greedy_and_leaves_room_for_every_later_edit(
    db_path, cfg, reserve
):
    rig = Rig(db_path, cfg, NOW)
    seed(db_path)
    add_guild(db_path, 1, ping=ROLE)
    rig.reserve = reserve
    rng = random.Random(reserve)  # noqa: S311
    codes = n_codes(21)
    items = [replace(it, source_name="n" * rng.randint(5, 100)) for it in distinct(*codes)]

    await rig.run(items)

    messages = [d.alert for d in rig.delivered]
    assert [c for m in messages for c in m.codes] == codes
    for i, message in enumerate(messages):
        blocks = _blocks(message.content)
        assert len(blocks) == len(message.codes)
        header = first_paragraph(message.content)
        # The promise: give every code its biggest possible line and the message still fits.
        assert discord_len(message.content) + reserve * len(blocks) <= 2000
        # Greedy: the next message's first code wouldn't have fitted into this one.
        if i + 1 < len(messages):
            next_first = _blocks(messages[i + 1].content)[0]
            used = discord_len(header) + sum(discord_len(b) + reserve + 2 for b in blocks)
            assert used + discord_len(next_first) + reserve + 2 > 2000
    assert_records_match_what_posted(rig)


# --- a seeded random walk over several passes, checking the invariants rather than the text ---

HOSTILE_REWARDS = [
    "",
    "1 Golden Key",
    "@everyone free skins",
    "*" * 150,
    "r" * 300,
    "<@&123456789012345678> and <#123456789012345678>",
    "[x](https://evil.example/" + "a" * 100 + ")",
    "\U0001f600" * 140,
    "line one\nline two\n\n```code```",
]


@pytest.mark.parametrize("seed_value", range(25))
async def test_a_random_week_of_passes_keeps_every_invariant(db_path, cfg, seed_value):
    rng = random.Random(1000 + seed_value)  # noqa: S311
    rig = Rig(db_path, cfg, NOW)
    seed(db_path)
    rig.reserve = rng.choice([0, 340, reserve_for_config(cfg), 740, BIG_RESERVE])
    guilds = {1: "everyone", 2: ROLE, 3: "none"}
    for guild_id, ping in guilds.items():
        add_guild(db_path, guild_id, ping=ping)
    fresh_code = iter(f"W{seed_value:02d}{i:02d}-ABCDE-FGHJK-LMNPQ-RSTVW" for i in range(400))
    known: list[str] = []
    names = ["mentalmars", "Borderlands 4 Steam", STAR_NAME, "2K Newsroom"]

    def maybe_fail(guild_id, _alert):
        if rng.random() < 0.08:
            rig.script.setdefault(guild_id, []).append(rng.choice([AMBIGUOUS, refused()]))

    rig.on_post = maybe_fail
    for _pass in range(rng.randint(3, 7)):
        rig.clock += timedelta(hours=rng.choice([1, 1, 2, 7, 30]))
        batch = [next(fresh_code) for _ in range(rng.choice([0, 1, 2, 4, 9]))]
        for code in batch:
            reward = rng.choice(HOSTILE_REWARDS)
            if reward or rng.random() < 0.3:
                with closing(connect(db_path)) as conn:
                    repo.upsert_code_details(
                        conn,
                        [
                            DetailObservation(
                                code,
                                ItemDetails(
                                    code=code,
                                    rank="table",
                                    reward=reward,
                                    expires_on=rng.choice(
                                        [None, date(2026, 10, 2), date(2026, 12, 25)]
                                    ),
                                    listed=True,
                                ),
                                rng.choice(names),
                                None,
                            )
                        ],
                        now=rig.clock,
                    )
        trust = rng.choice(["official", "community"])
        items = distinct(*batch, trust=trust, published=rig.clock - timedelta(minutes=30))
        # A confirmation of some older community code now and then, to feed the follow-ups.
        if known and rng.random() < 0.5:
            items += [
                sighting(BLUESKY, "community", c, rig.clock)
                for c in rng.sample(known, min(3, len(known)))
            ]
        known += batch
        # Settings change mid-flight sometimes.
        if rng.random() < 0.3:
            with closing(connect(db_path)) as conn:
                repo.set_shift(
                    conn,
                    rng.choice(list(guilds)),
                    enabled=True,
                    channel_id=rng.choice([100, 200, 300, 999]),
                    ping=rng.choice(["everyone", ROLE_B, "none"]),
                    now=lambda: EARLIER_SETTINGS,
                )
        await rig.run(items)

        assert all(discord_len(d.alert.content) <= 2000 for d in rig.delivered)
        assert_ping_integrity(rig)
        assert_records_match_what_posted(rig)
        assert rows(db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE status = 'pending'") == [
            (0,)
        ]
        seen: set[tuple[int, str]] = set()
        for guild_id, message_id, *_ in message_rows(db_path):
            for code in posts_of(db_path, guild_id, message_id):
                assert (guild_id, code) not in seen, "a code is in two recorded messages"
                seen.add((guild_id, code))
        for guild_id in guilds:
            assert shift_row(db_path, guild_id).ping_count <= 3


EARLIER_SETTINGS = NOW - timedelta(days=1)
