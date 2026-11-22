"""What a server's SHiFT turn says it did: the outcome and the INFO line built from it.

Two small bookkeeping fixes. First, a turn that sent nothing but a pinging "confirmed by
a second source" follow-up used to log `pinged: false`, because only the follow-up's code
count was copied onto the turn's outcome. Second, a `MarkLostError` (a message went out,
the database stayed busy) used to replace the whole outcome with a bare error, forgetting
the alerts and follow-up messages that had landed and been recorded before it.

Nothing here changes what is posted or marked; it only checks what gets reported. Real
temp databases and the delivery tests' own fakes, no network.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing

import pytest
from test_shift_fanout_adversarial import (
    V3,
    Harness,
    _code,
    add_guild,
    seed,
)
from test_shift_followups import (
    BLUESKY,
    REDDIT,
    T0,
    burst,
    script_posts,
    sighting_item,
    status_of,
)

from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import fanout
from newsbot.shift.fanout import GuildOutcome, deliver_queued_codes
from newsbot.store.db import connect, migrate


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def h(db_path):
    seed(db_path)
    return Harness(db_path, load_config(V3), T0)


def hold_delivery(monkeypatch):
    """Make the next detection pass queue its codes without sending them; returns the release."""

    async def nothing(deps, guild_id):
        return GuildOutcome(guild_id)

    real_one, real_followups = fanout._deliver_one, fanout._deliver_followups
    monkeypatch.setattr(fanout, "_deliver_one", nothing)
    monkeypatch.setattr(fanout, "_deliver_followups", nothing)

    def release():
        monkeypatch.setattr(fanout, "_deliver_one", real_one)
        monkeypatch.setattr(fanout, "_deliver_followups", real_followups)

    return release


async def one_turn(h, monkeypatch, items):
    """Queue what `items` cause without sending it, then run one real delivery walk."""
    release = hold_delivery(monkeypatch)
    await h.run(items)
    release()
    return await deliver_queued_codes(h.deps())


def lose_followup_marks(monkeypatch, *, from_nth_call: int):
    """From the Nth follow-up mark on, the database stays busy through every retry."""
    real = fanout._followup_mark_sync
    calls = {"n": 0}

    def flaky(db_path, guild_id, codes, message_id, posted):
        calls["n"] += 1
        if calls["n"] >= from_nth_call:
            raise sqlite3.OperationalError("database is locked")
        real(db_path, guild_id, codes, message_id, posted)

    monkeypatch.setattr(fanout, "_followup_mark_sync", flaky)


def delivery_lines(caplog):
    return [r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"]


# --- fix 1: a follow-up that pinged says so ---


async def test_a_followup_only_turn_that_pinged_reports_pinged(h, monkeypatch, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 1)
    caplog.set_level(logging.INFO, logger=fanout.logger.name)
    caplog.clear()  # the burst's own first alert logged a line too

    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item(BLUESKY, "community", codes[0], at=h.clock)]
    )

    assert outcome.followups_posted == 1 and outcome.posted == 0
    assert outcome.pinged is True
    [line] = delivery_lines(caplog)
    assert line.pinged is True and line.codes == 1


async def test_a_followup_that_went_out_without_its_ping_does_not_report_pinged(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 1)
    # An ambiguous failure: the retry goes out with the ping stripped, and lands.
    made = script_posts(h, PublishError("timeout"))
    caplog.set_level(logging.INFO, logger=fanout.logger.name)
    caplog.clear()  # the burst's own first alert logged a line too

    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item(BLUESKY, "community", codes[0], at=h.clock)]
    )

    [poster] = made
    assert len(poster.sent) == 1 and poster.sent[0].ping is False
    assert outcome.followups_posted == 1
    assert outcome.pinged is False
    [line] = delivery_lines(caplog)
    assert line.pinged is False


async def test_a_followup_that_never_landed_does_not_report_pinged(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 1)
    script_posts(h, RuntimeError("the channel is gone"))

    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item(BLUESKY, "community", codes[0], at=h.clock)]
    )

    assert outcome.followups_posted == 0
    assert outcome.pinged is False


async def test_unpinged_alerts_plus_a_pinging_followup_report_pinged(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    [old] = await burst(h, 1)
    new = _code(2)

    # A fresh community code (posts unpinged) and the old one's confirmation, in one turn.
    [outcome] = await one_turn(
        h,
        monkeypatch,
        [
            sighting_item(REDDIT, "community", new, at=h.clock),
            sighting_item(BLUESKY, "community", old, at=h.clock),
        ],
    )

    assert outcome.posted == 1 and outcome.followups_posted == 1
    assert outcome.pinged is True


# --- fix 2: a lost mark keeps the count of what did land ---


async def test_a_lost_followup_mark_keeps_the_alerts_that_posted(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    [old] = await burst(h, 1)
    lose_followup_marks(monkeypatch, from_nth_call=1)

    # An official code pings its own alert; the old code's follow-up then loses its mark.
    [outcome] = await one_turn(
        h,
        monkeypatch,
        [
            sighting_item("Gearbox", "official", _code(2), at=h.clock),
            sighting_item(BLUESKY, "community", old, at=h.clock),
        ],
    )

    assert outcome.error == "database busy while recording"
    assert outcome.posted == 1 and outcome.failed == 0
    assert outcome.pinged is True
    assert outcome.followups_posted == 0  # the message that went out wasn't recorded
    assert status_of(h.db_path)[old] == "pending"


async def test_a_lost_mark_on_a_later_followup_message_counts_only_the_recorded_ones(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 50)  # messages of 20, 20 and 10
    lose_followup_marks(monkeypatch, from_nth_call=2)

    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item(BLUESKY, "community", c, at=h.clock) for c in codes]
    )

    assert outcome.error == "database busy while recording"
    assert outcome.followups_posted == 20
    assert outcome.pinged is True  # the first message pinged, and it was recorded
    statuses = status_of(h.db_path)
    assert [statuses[c] for c in codes] == ["posted"] * 20 + ["pending"] * 30


async def test_a_lost_mark_on_the_first_followup_message_counts_nothing(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 25)
    lose_followup_marks(monkeypatch, from_nth_call=1)

    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item(BLUESKY, "community", c, at=h.clock) for c in codes]
    )

    assert outcome.error is not None
    assert outcome.followups_posted == 0 and outcome.pinged is False


async def test_a_lost_alert_mark_keeps_the_batches_recorded_before_it(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = [_code(i) for i in range(1, 61)]
    real = fanout._mark_posted_sync
    calls = {"n": 0}

    def flaky(*args):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sqlite3.OperationalError("database is locked")
        real(*args)

    monkeypatch.setattr(fanout, "_mark_posted_sync", flaky)
    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item("Gearbox", "official", c, at=h.clock) for c in codes]
    )

    assert outcome.error == "database busy while recording"
    sent = h.sent(1)
    assert len(sent) >= 2  # the second message went out; its mark is the one that was lost
    assert outcome.posted == len(sent[0].codes)
    assert outcome.pinged is True


async def test_a_lost_mark_still_logs_the_error_and_leaves_the_codes_pending(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 25)
    lose_followup_marks(monkeypatch, from_nth_call=1)
    caplog.set_level(logging.INFO, logger=fanout.logger.name)
    caplog.clear()  # the burst's own first alert logged a line too

    [outcome] = await one_turn(
        h, monkeypatch, [sighting_item(BLUESKY, "community", c, at=h.clock) for c in codes]
    )

    assert outcome.error is not None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("stayed busy" in r.getMessage() and r.guild_id == 1 for r in errors)
    assert delivery_lines(caplog) == []  # an errored turn still logs no INFO line
    statuses = status_of(h.db_path)
    assert [statuses[c] for c in codes[:20]] == ["pending"] * 20
