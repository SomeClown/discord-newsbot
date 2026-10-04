"""Adversarial tests for the SHiFT delivery walk: claims, the lock, the deadline, a busy database.

The walk takes whatever is still `queued`, sends it, and writes down that it did. Everything
that can go wrong lives in the gaps between those three verbs: a second walk that read the
same list, a deadline that arrives mid-list, a database that says "locked" at the worst
moment, a hard stop that cuts a send in half. The promises are small and absolute:

- nothing is sent twice, and nothing is spent (a ping) that wasn't sent;
- a code is stranded `queued` only because somebody ran out of time, never because
  somebody else got there first;
- a send that was cut off is never retried, and is owned up to once, after it's clearly dead.

Real temp databases and scripted posters; the clocks are fakes that move when told. A bare
loop of `asyncio.sleep(0)` inside the posters is what makes walks actually interleave.
"""

from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import threading
from contextlib import closing
from datetime import timedelta
from itertools import count

import pytest
from test_shift_fanout_adversarial import (
    CODE_A,
    CODE_B,
    CODE_C,
    NOW,
    V3,
    Harness,
    _code,
    _with_shift,
    add_guild,
    item,
    post_status,
    rows,
    seed,
    shift_row,
)

from newsbot.bot.format import render_code_alerts
from newsbot.config import load_config
from newsbot.shift import fanout
from newsbot.shift.fanout import (
    _PENDING_STALE_S,
    FanoutDeps,
    _release,
    deliver_queued_codes,
    detect_and_fan_out,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate


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
    seed(db_path)
    return Harness(db_path, cfg, NOW)


class Yielding:
    """A poster that gives the other walks a turn before and after it 'sends'."""

    def __init__(self, log: list, guild_id: int, ping: str) -> None:
        self.log, self.guild_id, self.ping = log, guild_id, ping

    def begin_batch(self) -> None:
        return None

    async def post(self, alert) -> int:
        for _ in range(3):
            await asyncio.sleep(0)
        self.log.append((self.guild_id, alert))
        await asyncio.sleep(0)
        return 7000 + len(self.log)


def walkers(db_path, cfg, n, log):
    """`n` walks with their own deps (so their own locks): the overlap two processes would have."""
    deps = []
    for _ in range(n):
        harness = Harness(db_path, cfg, NOW)
        d = harness.deps()
        d.poster_for = lambda gid, cid, ping, notify, log=log: Yielding(log, gid, ping)
        d.pace_s = 0
        deps.append(d)
    return deps


def queue_codes(db_path, cfg, batches):
    """Release each batch of items as its own pass would, with no delivery."""
    deps = Harness(db_path, cfg, NOW).deps()

    async def go():
        for items in batches:
            await _release(deps, items, seeding_ok=True)

    return go()


# --- many overlapping walks ---


@pytest.mark.parametrize("walks", [2, 5, 12])
async def test_overlapping_walks_send_every_code_once_and_never_exceed_the_cap(db_path, cfg, walks):
    cfg = _with_shift(cfg, max_pings_per_day=2)
    seed(db_path)
    for gid in (1, 2, 3):
        add_guild(db_path, gid, ping="everyone")
    codes = [_code(i) for i in range(6)]
    await queue_codes(
        db_path,
        cfg,
        [
            [item(*codes[:3])],
            [item(*codes[3:], url="https://example.com/second")],
        ],
    )
    log: list = []
    await asyncio.gather(*(deliver_queued_codes(d) for d in walkers(db_path, cfg, walks, log)))

    for gid in (1, 2, 3):
        sent = [alert for g, alert in log if g == gid]
        flat = [code for alert in sent for code in alert.codes]
        assert sorted(flat) == sorted(codes), f"guild {gid}: {flat}"  # each exactly once
        pings = sum(1 for alert in sent if alert.ping)
        assert pings <= 2 and pings == shift_row(db_path, gid).ping_count
        assert set(post_status(db_path, gid).values()) == {"posted"}  # nothing stranded


async def test_a_cap_of_one_is_spent_once_however_the_walks_race(db_path, cfg):
    cfg = _with_shift(cfg, max_pings_per_day=1)
    seed(db_path)
    for gid in (1, 2):
        add_guild(db_path, gid, ping="everyone")
    await queue_codes(
        db_path,
        cfg,
        [[item(_code(1))], [item(_code(2), url="https://example.com/two")]],
    )
    log: list = []
    await asyncio.gather(*(deliver_queued_codes(d) for d in walkers(db_path, cfg, 6, log)))
    for gid in (1, 2):
        assert sum(1 for g, alert in log if g == gid and alert.ping) == 1
        assert shift_row(db_path, gid).ping_count == 1


async def test_walks_racing_a_new_release_strand_nothing_and_send_nothing_twice(db_path, cfg):
    seed(db_path)
    add_guild(db_path, 1, ping="none")
    log: list = []
    deps = walkers(db_path, cfg, 4, log)
    releaser = Harness(db_path, cfg, NOW).deps()

    async def keep_releasing():
        for i in range(5):
            await _release(
                releaser, [item(_code(i), url=f"https://example.com/r{i}")], seeding_ok=True
            )
            await asyncio.sleep(0)

    await asyncio.gather(keep_releasing(), *(deliver_queued_codes(d) for d in deps))
    await deliver_queued_codes(deps[0])  # whatever landed after the walks finished
    flat = sorted(code for _, alert in log for code in alert.codes)
    assert flat == sorted(_code(i) for i in range(5))
    assert set(post_status(db_path, 1).values()) == {"posted"}


async def test_a_walk_holding_a_stale_list_still_sends_the_code_nobody_else_saw(
    db_path, cfg, monkeypatch
):
    # The exact interleaving that used to flake the overlapping-passes cap test about one
    # full run in three: walk one reads [A], B is released, walk two reads [A, B], walk
    # one claims A, then walk two claims [A, B]. The all-or-nothing claim lost A, rolled
    # back B with it, and B sat `queued` until the next pass because walk two assumed
    # whoever beat it would send the lot. Walk one never saw B. The test above can't
    # catch that: its walks race on `sleep(0)` luck and a cleanup pass mops up the
    # straggler. Here the order is forced with thread events, and nobody mops.
    cfg = _with_shift(cfg, max_pings_per_day=1)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone")
    await queue_codes(db_path, cfg, [[item(CODE_A)]])
    one, two = walkers(db_path, cfg, 2, log := [])
    walker = contextvars.ContextVar("walker")
    one_loaded, two_loaded, one_claimed = threading.Event(), threading.Event(), threading.Event()
    two_claims: list[tuple[list[str], list[str]]] = []
    real_queued, real_claim = fanout.queued_guild_codes, fanout.claim_guild_codes

    def queued(conn, guild_id):
        got = real_queued(conn, guild_id)
        (one_loaded if walker.get() == "one" else two_loaded).set()
        return got

    def claim(conn, guild_id, codes, **kwargs):
        if walker.get() == "one":
            assert two_loaded.wait(5), "walk two never read the queue"
            try:
                return real_claim(conn, guild_id, codes, **kwargs)
            finally:
                one_claimed.set()
        assert one_claimed.wait(5), "walk one never claimed"
        got = real_claim(conn, guild_id, codes, **kwargs)
        two_claims.append((list(codes), got.codes))
        return got

    monkeypatch.setattr(fanout, "queued_guild_codes", queued)
    monkeypatch.setattr(fanout, "claim_guild_codes", claim)

    async def walk(deps, name):
        walker.set(name)
        return await deliver_queued_codes(deps)

    first = asyncio.create_task(walk(one, "one"))
    assert await asyncio.to_thread(one_loaded.wait, 5), "walk one never read the queue"
    await _release(Harness(db_path, cfg, NOW).deps(), [item(CODE_B)], seeding_ok=True)
    second = asyncio.create_task(walk(two, "two"))
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

    assert two_claims == [([CODE_A, CODE_B], [CODE_B])]  # the stale list, and what it won
    assert post_status(db_path, 1) == {CODE_A: "posted", CODE_B: "posted"}
    assert sorted(code for _, alert in log for code in alert.codes) == [CODE_A, CODE_B]
    assert sum(1 for _, alert in log if alert.ping) == 1 == shift_row(db_path, 1).ping_count


# --- the deadline, at its edges ---


async def test_a_turn_that_starts_exactly_at_the_deadline_is_not_started(h):
    for gid in range(1, 4):
        add_guild(h.db_path, gid, ping="none")
    await _release(h.deps(), [item(CODE_A)], seeding_ok=True)
    ticks = count()
    deps = h.deps()
    deps.clock = lambda: float(next(ticks))  # each turn reads the clock once: 0, 1, 2
    outcomes = await deliver_queued_codes(deps, deadline=2.0)
    assert [o.guild_id for o in outcomes] == [1, 2]
    assert set(post_status(h.db_path, 3).values()) == {"queued"} and h.notices == []


async def test_a_turn_that_starts_just_before_the_deadline_is_started(h):
    for gid in range(1, 4):
        add_guild(h.db_path, gid, ping="none")
    await _release(h.deps(), [item(CODE_A)], seeding_ok=True)
    ticks = count()
    deps = h.deps()
    deps.clock = lambda: float(next(ticks))
    outcomes = await deliver_queued_codes(deps, deadline=2.000001)
    assert [o.guild_id for o in outcomes] == [1, 2, 3]


async def test_a_deadline_already_gone_delivers_nothing_and_fails_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await _release(h.deps(), [item(CODE_A)], seeding_ok=True)
    deps = h.deps()
    deps.clock = lambda: 500.0
    assert await deliver_queued_codes(deps, deadline=500.0) == []
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}
    assert shift_row(h.db_path, 1).ping_count == 0 and h.notices == []


async def test_a_pass_whose_release_ran_past_the_walk_deadline_leaves_the_queue_for_the_next(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="none")
    t = [0.0]
    real_release = fanout._release

    async def slow_release(deps, items, *, seeding_ok):
        released = await real_release(deps, items, seeding_ok=seeding_ok)
        t[0] += 200.0  # detection took longer than the walk's whole budget
        return released

    monkeypatch.setattr(fanout, "_release", slow_release)
    deps = h.deps()
    deps.clock = lambda: t[0]
    assert await detect_and_fan_out(deps, [item(CODE_A)], seeding_ok=True) == 1
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}
    assert h.owner_alerts == [] and h.notices == []

    monkeypatch.setattr(fanout, "_release", real_release)
    t[0] = 0.0  # the next pass, with a budget of its own
    await detect_and_fan_out(deps, [item(CODE_B)], seeding_ok=True)
    assert post_status(h.db_path, 1) == {CODE_A: "posted", CODE_B: "posted"}


# --- the hard stop, mid-send ---


class Hangs:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    def begin_batch(self) -> None:
        return None

    async def post(self, alert):
        self.started.set()
        await asyncio.Event().wait()


async def test_a_send_cut_off_by_the_hard_stop_stays_pending_and_is_never_sent_again(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    monkeypatch.setattr(fanout, "_WALK_STOP_S", 0.2)
    monkeypatch.setattr(fanout, "_WALK_HARD_S", 0.3)
    monkeypatch.setattr(fanout, "_WALK_FLOOR_S", 0.05)
    deps = h.deps()
    hung = Hangs()
    real_factory = deps.poster_for
    deps.poster_for = lambda gid, cid, ping, notify: (
        hung if gid == 1 else real_factory(gid, cid, ping, notify)
    )

    assert await detect_and_fan_out(deps, [item(CODE_A)], seeding_ok=True) == 1
    assert hung.started.is_set()
    assert post_status(h.db_path, 1) == {CODE_A: "pending"}  # claimed, never confirmed
    assert post_status(h.db_path, 2) == {CODE_A: "queued"}  # never reached
    assert h.owner_alerts == [] and h.notices == []

    # The next pass: server 1's row isn't stale yet, so it's left alone and never re-sent.
    deps.poster_for = real_factory
    h.posters.clear()
    await deliver_queued_codes(deps)
    assert post_status(h.db_path, 1) == {CODE_A: "pending"} and h.sent(1) == []
    assert post_status(h.db_path, 2) == {CODE_A: "posted"}


async def test_a_pending_row_goes_stale_strictly_after_ten_minutes(h):
    add_guild(h.db_path, 1, ping="none")
    await _release(h.deps(), [item(CODE_A)], seeding_ok=True)
    with closing(connect(h.db_path)) as conn:
        repo.claim_guild_codes(
            conn, 1, [CODE_A], pinged=False, local_day="2026-10-01", now=lambda: NOW
        )
    for seconds, expected, notices in (
        (_PENDING_STALE_S - 1, "pending", 0),
        (_PENDING_STALE_S, "pending", 0),  # exactly ten minutes old is not yet dead
        (_PENDING_STALE_S + 1, "failed", 1),
        (_PENDING_STALE_S + 60, "failed", 1),  # and the notice goes out once
    ):
        h.clock = NOW + timedelta(seconds=seconds)
        await h.deliver()
        assert post_status(h.db_path, 1) == {CODE_A: expected}, seconds
        assert len(h.notices) == notices, seconds
    assert CODE_A in h.notices[0][1] and "may or may not" in h.notices[0][1]


# --- a busy database ---


async def test_a_busy_database_when_listing_the_queue_leaves_it_alone_and_does_not_spin(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="none")
    calls = []

    def locked(_conn):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fanout, "queued_guild_ids", locked)
    released = await asyncio.wait_for(
        detect_and_fan_out(h.deps(), [item(CODE_A)], seeding_ok=True), timeout=5
    )
    assert released == 1 and len(calls) == 1  # asked once, not in a loop
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}
    assert h.sent(1) == []
    assert len(h.owner_alerts) == 1 and "too busy" in h.owner_alerts[0]  # the owner hears once

    monkeypatch.undo()
    await h.deliver()
    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


async def test_the_busy_database_alert_to_the_owner_is_once_an_hour_not_every_pass(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")

    def locked(_conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fanout, "queued_guild_ids", locked)
    deps = h.deps()  # one set of deps for the process, as the bot has
    for minutes in (0, 20, 40, 59):
        h.clock = NOW + timedelta(minutes=minutes)
        await detect_and_fan_out(deps, [], seeding_ok=True)
    assert len(h.owner_alerts) == 1
    h.clock = NOW + timedelta(hours=1)
    await detect_and_fan_out(deps, [], seeding_ok=True)
    assert len(h.owner_alerts) == 2  # an hour on, the owner is told it's still going on
    h.clock = NOW + timedelta(hours=1, minutes=30)
    await detect_and_fan_out(deps, [], seeding_ok=True)
    assert len(h.owner_alerts) == 2


async def test_a_busy_database_while_marking_a_sent_code_is_retried_and_lands(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")
    real = fanout.mark_guild_codes_posted
    failures = {"left": 2}

    def flaky(*args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, "mark_guild_codes_posted", flaky)
    await h.run([item(CODE_A)])
    assert len(h.sent(1)) == 1 and post_status(h.db_path, 1) == {CODE_A: "posted"}
    assert h.owner_alerts == [] and h.notices == []


async def test_a_database_that_stays_busy_while_marking_never_sends_anything_again(h, monkeypatch):
    """The send happened and the 'posted' mark never could. The row stays `pending` (so it's
    never re-sent), the rest of that server's batch isn't sent (it stays `pending` too), and
    once it's stale it's owned up to as 'may or may not have posted'."""
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    real = fanout.mark_guild_codes_posted

    def busy_for_one(conn, guild_id, *args, **kwargs):
        if guild_id == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, guild_id, *args, **kwargs)

    monkeypatch.setattr(fanout, "mark_guild_codes_posted", busy_for_one)
    await h.run([item(CODE_A)])
    assert len(h.sent(1)) == 1 and post_status(h.db_path, 1) == {CODE_A: "pending"}
    assert post_status(h.db_path, 2) == {CODE_A: "posted"}  # the next server isn't held up

    h.posters.clear()
    await h.deliver()
    assert h.sent(1) == []  # not sent a second time

    h.clock = NOW + timedelta(seconds=_PENDING_STALE_S + 1)
    await h.deliver()
    assert post_status(h.db_path, 1) == {CODE_A: "failed"}
    assert h.sent(1) == [] and len(h.notices) == 1 and "may or may not" in h.notices[0][1]


async def test_a_mark_that_cannot_land_mid_batch_leaves_the_rest_of_the_batch_unsent(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="none")
    many = [item(code) for code in (CODE_A, CODE_B, CODE_C)]
    real = fanout.mark_guild_codes_posted

    def busy(conn, guild_id, codes, **kwargs):
        if CODE_A in codes:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, guild_id, codes, **kwargs)

    monkeypatch.setattr(fanout, "mark_guild_codes_posted", busy)
    monkeypatch.setattr(fanout, "render_code_alerts", _one_code_per_message)
    await h.run(many)
    sent_codes = [code for alert in h.sent(1) for code in alert.codes]
    assert sent_codes == [CODE_A]  # nothing after the unrecorded one went out
    assert set(post_status(h.db_path, 1).values()) == {"pending"}


def _one_code_per_message(candidates, **kwargs):
    """The real renderer, but a message per code, so a short batch spans several messages."""
    return [alert for c in candidates for alert in render_code_alerts([c], **kwargs)]


async def test_a_busy_claim_for_one_server_does_not_hold_up_the_next(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    real = fanout.claim_guild_codes

    def flaky(conn, guild_id, *args, **kwargs):
        if guild_id == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, guild_id, *args, **kwargs)

    monkeypatch.setattr(fanout, "claim_guild_codes", flaky)
    await h.run([item(CODE_A)])
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}
    assert post_status(h.db_path, 2) == {CODE_A: "posted"}
    assert h.notices == []


# --- startup versus a live walk ---


async def test_startup_delivery_waits_for_a_live_walk_then_finds_nothing_left_to_fail(h):
    add_guild(h.db_path, 1, ping="none")
    await _release(h.deps(), [item(CODE_A)], seeding_ok=True)
    log: list = []
    deps = h.deps()
    deps.poster_for = lambda gid, cid, ping, notify: Yielding(log, gid, ping)
    live = asyncio.create_task(deliver_queued_codes(deps))
    await asyncio.sleep(0)  # the live walk has taken the lock and is mid-claim or mid-send
    startup = asyncio.create_task(deliver_queued_codes(deps, startup=True))
    await asyncio.gather(live, startup)
    assert [g for g, _ in log] == [1]  # sent once
    assert post_status(h.db_path, 1) == {CODE_A: "posted"} and h.notices == []


def test_the_fanout_deps_each_get_their_own_delivery_lock():
    one = FanoutDeps(cfg=None, db_path="x", now=None, poster_for=None, notify_guild=None)
    two = FanoutDeps(cfg=None, db_path="x", now=None, poster_for=None, notify_guild=None)
    assert one.delivery_lock is not two.delivery_lock


async def test_untouched_rows_prove_the_queue_is_ordered_by_guild_id_not_by_insert_order(h):
    for gid in (3, 1, 2):
        add_guild(h.db_path, gid, ping="none")
    await _release(h.deps(), [item(CODE_A)], seeding_ok=True)
    outcomes = await h.deliver()
    assert [o.guild_id for o in outcomes] == [1, 2, 3]
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE status = 'posted'") == [
        (3,)
    ]
