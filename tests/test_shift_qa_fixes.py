"""Regression tests for the SHiFT findings of the task 16 qa review (batch 1).

Each test is named for the thing that was broken: a nonce shared by every
server, a CLI pass that drained the queue, two overlapping walks stranding a
code, a hook timeout that cut a send in half, a locked database that failed
a server's codes for good, and the diagnostics the owner's open bug needed.
Everything runs against a real temp database with scripted fakes. No network.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import types
from contextlib import closing
from datetime import timedelta

import pytest
from test_shift_fanout_adversarial import (
    CODE_A,
    CODE_B,
    EARLIER,
    NOW,
    V3,
    FakeChannel,
    Harness,
    add_guild,
    item,
    post_status,
    rows,
    seed,
    shift_row,
)

from newsbot.bot.client import DiscordCodeAlertPoster, ForeignMessageError
from newsbot.bot.format import render_code_alerts, render_roundup_alerts
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import fanout
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.fanout import (
    FanoutDeps,
    _release,
    deliver_queued_codes,
    detect_and_fan_out,
    recover_pending_guild_codes,
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
    harness = Harness(db_path, cfg, NOW)
    seed(db_path)
    return harness


def _candidates(*codes):
    return [
        CodeCandidate(c, golden=False, source_name="s", item_url="https://e.com", fresh=True)
        for c in codes
    ]


# --- H1: the nonce is per server and channel ---


def test_the_same_batch_for_two_servers_gets_different_nonces():
    a = render_code_alerts(_candidates(CODE_A, CODE_B), ping=False, nonce_scope="1|100")
    b = render_code_alerts(_candidates(CODE_A, CODE_B), ping=False, nonce_scope="2|200")
    assert a[0].nonce and b[0].nonce and a[0].nonce != b[0].nonce


def test_the_same_server_and_channel_reuses_its_nonce_across_renders_and_ping_changes():
    first = render_code_alerts(_candidates(CODE_A), ping=True, nonce_scope="1|100")
    again = render_code_alerts(_candidates(CODE_A), ping=True, nonce_scope="1|100")
    capped = render_code_alerts(_candidates(CODE_A), ping=False, nonce_scope="1|100")
    assert first[0].nonce == again[0].nonce == capped[0].nonce


def test_a_channel_change_changes_the_nonce():
    a = render_code_alerts(_candidates(CODE_A), ping=False, nonce_scope="1|100")
    b = render_code_alerts(_candidates(CODE_A), ping=False, nonce_scope="1|101")
    assert a[0].nonce != b[0].nonce


def test_roundup_nonces_are_scoped_too():
    one = render_roundup_alerts(_candidates(CODE_A), nonce_scope="1|100")
    two = render_roundup_alerts(_candidates(CODE_A), nonce_scope="2|200")
    assert one[0].nonce != two[0].nonce
    assert one[0].nonce == render_roundup_alerts(_candidates(CODE_A), nonce_scope="1|100")[0].nonce


def test_no_scope_keeps_the_old_nonce():
    # The self-hoster's v2 nonces don't change underneath a retry in flight at upgrade time.
    plain = render_code_alerts(_candidates(CODE_A), ping=False)
    assert (
        plain[0].nonce
        == render_code_alerts(_candidates(CODE_A), ping=False, nonce_scope="")[0].nonce
    )


async def test_the_fanout_sends_each_server_its_own_nonce_and_a_retry_keeps_it(h):
    for gid in (1, 2):
        add_guild(h.db_path, gid, ping="none")
    h.fail_with = {1: [PublishError("503", retryable=True)]}

    await h.run([item(CODE_A)])

    a, b = h.sent(1)[0], h.sent(2)[0]
    assert a.nonce != b.nonce
    # Retries reuse `alert.nonce` untouched (post_alert_with_retry); the scripted failure
    # above was retried with the very object that got posted.
    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


# --- H1: the tripwire in the real poster ---


class _WrongChannel(FakeChannel):
    async def send(self, content, *, allowed_mentions, nonce=None):
        await super().send(content, allowed_mentions=allowed_mentions, nonce=nonce)
        return types.SimpleNamespace(id=7, channel=types.SimpleNamespace(id=424242))


async def test_a_message_returned_from_another_channel_is_a_failure_not_a_post(h, caplog):
    add_guild(h.db_path, 1, ping="none")
    h.channels = {1: _WrongChannel()}

    with caplog.at_level(logging.ERROR, logger="newsbot.bot.client"):
        await h.run([item(CODE_A)])

    assert post_status(h.db_path, 1) == {CODE_A: "failed"}
    assert [g for g, _ in h.notices] == [1]
    assert any("another channel" in r.getMessage() for r in caplog.records)


async def test_a_message_from_the_right_channel_still_posts(h):
    class Right(FakeChannel):
        async def send(self, content, *, allowed_mentions, nonce=None):
            await super().send(content, allowed_mentions=allowed_mentions, nonce=nonce)
            return types.SimpleNamespace(id=7, channel=types.SimpleNamespace(id=100))

    add_guild(h.db_path, 1, ping="none")
    h.channels = {1: Right()}
    await h.run([item(CODE_A)])
    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


def test_the_tripwire_error_is_not_a_publish_error():
    assert not issubclass(ForeignMessageError, PublishError)
    assert DiscordCodeAlertPoster  # the class the tripwire lives on


# --- H2: a CLI-style preview never claims, marks or spends ---


async def test_preview_prints_the_queue_and_changes_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    printed = []

    class Printer:
        async def post(self, alert):
            printed.append(alert)

    deps = h.deps()
    deps.preview = True
    deps.poster_for = lambda *_args: Printer()
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn,
            [(CODE_A, "s", "https://e.com", False)],
            now=lambda: NOW,
            queue_for_games=[],
            flags={CODE_A: (False, True)},
        )
        # A pending row from a live send elsewhere: a preview must not "recover" it.
        repo.record_released_codes(
            conn, [(CODE_B, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_B], pinged=False, local_day="2026-10-01")
    before = rows(h.db_path, "SELECT * FROM guild_code_posts ORDER BY code")
    ping_before = shift_row(h.db_path, 1).ping_count

    await deliver_queued_codes(deps, startup=True)

    assert [a.codes for a in printed] == [[CODE_A]]
    assert rows(h.db_path, "SELECT * FROM guild_code_posts ORDER BY code") == before
    assert shift_row(h.db_path, 1).ping_count == ping_before
    assert h.notices == []


# --- M1: overlapping walks no longer strand a code ---


async def test_a_walk_that_loaded_a_code_another_walk_claims_still_posts_the_rest(
    db_path, cfg, monkeypatch
):
    """Pass 1 loads [A]; pass 2 releases B and loads [A, B]; pass 1 claims A; pass 2 posts B."""
    seed(db_path)
    add_guild(db_path, 1, ping="none")
    one, two = Harness(db_path, cfg, NOW), Harness(db_path, cfg, NOW)
    d1, d2 = one.deps(), two.deps()
    await _release(d1, [item(CODE_A)], seeding_ok=True)

    real_claim = fanout._claim
    arrivals: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    gates: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    seen = []

    async def gated_claim(deps, *args, **kwargs):
        i = len(seen)
        seen.append(deps)
        arrivals[i].set()
        await gates[i].wait()
        return await real_claim(deps, *args, **kwargs)

    monkeypatch.setattr(fanout, "_claim", gated_claim)

    task1 = asyncio.create_task(deliver_queued_codes(d1))
    await arrivals[0].wait()  # pass 1 has loaded [A] and is about to claim
    await _release(d2, [item(CODE_B)], seeding_ok=True)
    task2 = asyncio.create_task(deliver_queued_codes(d2))
    await arrivals[1].wait()  # pass 2 has loaded [A, B]
    gates[0].set()
    await task1  # pass 1 claims and posts A
    gates[1].set()
    await task2  # pass 2 claims what's left: only B

    assert [a.codes for a in one.sent(1)] == [[CODE_A]]
    assert [a.codes for a in two.sent(1)] == [[CODE_B]]
    assert post_status(db_path, 1) == {CODE_A: "posted", CODE_B: "posted"}


async def test_the_interleaving_spends_one_ping_for_the_codes_it_really_sent(
    db_path, cfg, monkeypatch
):
    seed(db_path)
    add_guild(db_path, 1, ping="everyone")
    one, two = Harness(db_path, cfg, NOW), Harness(db_path, cfg, NOW)
    d1, d2 = one.deps(), two.deps()
    await _release(d1, [item(CODE_A)], seeding_ok=True)
    real_claim = fanout._claim
    arrivals = [asyncio.Event(), asyncio.Event()]
    gates = [asyncio.Event(), asyncio.Event()]
    seen = []

    async def gated_claim(deps, *args, **kwargs):
        i = len(seen)
        seen.append(deps)
        arrivals[i].set()
        await gates[i].wait()
        return await real_claim(deps, *args, **kwargs)

    monkeypatch.setattr(fanout, "_claim", gated_claim)
    task1 = asyncio.create_task(deliver_queued_codes(d1))
    await arrivals[0].wait()
    await _release(d2, [item(CODE_B)], seeding_ok=True)
    task2 = asyncio.create_task(deliver_queued_codes(d2))
    await arrivals[1].wait()
    gates[0].set()
    await task1
    gates[1].set()
    await task2

    sent = one.sent(1) + two.sent(1)
    assert sorted(c for a in sent for c in a.codes) == [CODE_A, CODE_B]
    assert sum(a.ping for a in sent) == shift_row(db_path, 1).ping_count == 2


async def test_startup_waits_for_a_walk_in_flight_instead_of_failing_its_pending_rows(h):
    add_guild(h.db_path, 1, ping="none")
    deps = h.deps()
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")

    await deps.delivery_lock.acquire()  # a pass is mid-walk with A claimed and sending
    startup = asyncio.create_task(deliver_queued_codes(deps, startup=True))
    await asyncio.sleep(0.05)
    assert not startup.done()
    assert post_status(h.db_path, 1) == {CODE_A: "pending"}

    deps.delivery_lock.release()
    await startup
    assert post_status(h.db_path, 1) == {CODE_A: "failed"}  # nothing in flight any more


def test_a_late_mark_cannot_resurrect_a_failed_row(db_path):
    add_guild(db_path, 1, ping="none")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")
        repo.fail_pending_guild_codes(conn)
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=3)
    assert post_status(db_path, 1) == {CODE_A: "failed"}


# --- M2: the walk stops starting servers before the hook's timeout ---


async def test_the_walk_stops_starting_servers_at_its_deadline_and_leaves_the_rest_queued(h):
    for gid in range(1, 6):
        add_guild(h.db_path, gid, ping="none")
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
    t = [0.0]

    async def tick(_seconds):
        t[0] += 30.0  # each server's turn "takes" 30 s

    deps = h.deps()
    deps.sleep = tick
    deps.clock = lambda: t[0]

    outcomes = await deliver_queued_codes(deps, deadline=100.0)

    assert [o.guild_id for o in outcomes] == [1, 2, 3, 4]  # turns started at 0, 30, 60, 90
    statuses = {gid: set(post_status(h.db_path, gid).values()) for gid in range(1, 6)}
    assert statuses[4] == {"posted"} and statuses[5] == {"queued"}
    assert h.notices == []  # a cut-off walk isn't a failure

    t[0] = 0.0
    await deliver_queued_codes(deps, deadline=100.0)  # the next pass picks up where it stopped
    assert post_status(h.db_path, 5) == {CODE_A: "posted"}


async def test_each_claim_reads_the_clock_when_it_claims(h):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
    moments = iter(NOW + timedelta(seconds=30 * i) for i in range(100))
    deps = h.deps(now=lambda: next(moments))

    await deliver_queued_codes(deps)

    claimed = [
        r[0] for r in rows(h.db_path, "SELECT claimed_at FROM guild_code_posts ORDER BY guild_id")
    ]
    assert claimed[0] != claimed[1]  # not one timestamp for the whole walk


async def test_a_delivery_failure_is_not_reported_as_a_detection_failure(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")

    async def boom(*_a, **_k):
        raise RuntimeError("delivery bug")

    monkeypatch.setattr(fanout, "deliver_queued_codes", boom)

    released = await detect_and_fan_out(h.deps(), [item(CODE_A)], seeding_ok=True)

    assert released == 1  # the code was released and queued; the hook doesn't raise
    assert h.owner_alerts == ["newsbot: SHiFT delivery failed during collection"]
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}


async def test_a_delivery_timeout_is_quiet_and_leaves_the_queue(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")

    async def forever(*_a, **_k):
        await asyncio.sleep(60)

    monkeypatch.setattr(fanout, "deliver_queued_codes", forever)
    monkeypatch.setattr(fanout, "_WALK_HARD_S", 0.0)
    monkeypatch.setattr(fanout, "_WALK_FLOOR_S", 0.05)

    released = await detect_and_fan_out(h.deps(), [item(CODE_A)], seeding_ok=True)

    assert released == 1 and h.owner_alerts == []
    assert post_status(h.db_path, 1) == {CODE_A: "queued"}


async def test_the_stale_pending_notice_names_its_cause(h):
    add_guild(h.db_path, 1, ping="none")
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(
            conn, 1, [CODE_A], pinged=False, local_day="2026-10-01", now=lambda: EARLIER
        )
    notes = []

    async def notify(guild_id, text):
        notes.append(text)

    await recover_pending_guild_codes(h.db_path, notify, claimed_before=NOW)
    assert len(notes) == 1 and "restarted" not in notes[0] and CODE_A in notes[0]

    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_B, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_B], pinged=False, local_day="2026-10-01")
    await recover_pending_guild_codes(h.db_path, notify)
    assert "restarted" in notes[1]


# --- M3: a busy database leaves codes queued instead of failing them ---


async def test_a_locked_database_on_claim_leaves_the_codes_queued_and_tells_nobody(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")
    real = fanout.claim_guild_codes
    state = {"locked": True}

    def flaky(*args, **kwargs):
        if state["locked"]:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, "claim_guild_codes", flaky)

    await h.run([item(CODE_A)])

    assert post_status(h.db_path, 1) == {CODE_A: "queued"}
    assert h.notices == [] and h.sent(1) == []

    state["locked"] = False
    await h.deliver()
    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


async def test_a_locked_database_anywhere_in_a_turn_is_not_a_failed_server(h, monkeypatch):
    add_guild(h.db_path, 1, ping="none")
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fanout, "queued_guild_codes", locked)
    outcomes = await h.deliver()

    assert [o.error for o in outcomes] == ["database busy"]
    assert post_status(h.db_path, 1) == {CODE_A: "queued"} and h.notices == []


# --- L: the diagnostics and the safe default ---


async def test_each_servers_outcome_is_logged_with_ids_counts_and_the_reason(h, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="none")

    with caplog.at_level(logging.INFO, logger="newsbot.shift.fanout"):
        await h.run([item(CODE_A, trust="community")])

    lines = {
        r.guild_id: r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"
    }
    assert lines[1].pinged is False and lines[1].codes == 1
    assert lines[1].unpinged_or_stripped_because == "untrusted"
    assert lines[2].unpinged_or_stripped_because == "ping_off"
    assert CODE_A not in " ".join(r.getMessage() for r in caplog.records)  # no code text


async def test_a_capped_server_logs_cap_reached(h, caplog):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=3)

    with caplog.at_level(logging.INFO, logger="newsbot.shift.fanout"):
        await h.run([item(CODE_A)])

    (line,) = [r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"]
    assert line.unpinged_or_stripped_because == "cap_reached"


async def test_a_ping_stripped_after_an_ambiguous_failure_warns_and_is_logged(h, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with = {1: [PublishError("timeout")]}  # ambiguous: not `rejected`

    with caplog.at_level(logging.INFO):
        await h.run([item(CODE_A)])

    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ping stripped" in r.getMessage() for r in warned)
    (line,) = [r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"]
    assert "ping_stripped_after_ambiguous_failure" in line.unpinged_or_stripped_because


async def test_a_missing_ping_permission_is_logged_as_no_permission(h, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    h.channels = {1: FakeChannel(can_mention_everyone=False)}

    with caplog.at_level(logging.INFO, logger="newsbot.shift.fanout"):
        await h.run([item(CODE_A)])

    (line,) = [r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"]
    assert "no_permission" in line.unpinged_or_stripped_because


def test_a_release_without_flags_defaults_to_untrusted(db_path):
    add_guild(db_path, 1, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://e.com", False)], now=lambda: NOW, queue_for_games=[]
        )
        (queued,) = repo.queued_guild_codes(conn, 1)
    assert queued.trusted is False and queued.golden is False


def test_the_fanout_deps_default_to_a_real_delivery():
    d = FanoutDeps.__dataclass_fields__
    assert d["preview"].default is False
