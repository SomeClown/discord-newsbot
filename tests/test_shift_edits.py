"""Edits in place (plan task 8; design.md §16).

A posted alert learns a reward or an expiry after the fact, and the edit pass goes back and
fixes the message. What these pin: the right messages are found (every server's, inside 14
days, never a roundup or a pre-3.1.0 alert), each is re-rendered whole with its own header,
and the ways an edit can end (gone, forbidden, rate limited, a 5xx, a message that won't
fit) each do exactly what the design says, once. The pass also shares the delivery walk's
time budget, goes after it, and can't be turned into an edit storm by a reward that flaps.

Real temp SQLite with the real migrations, a fake editor per channel, an injected clock
(wall and monotonic). No network.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_shift_delivery_details import (
    BIG_RESERVE,
    GEARBOX,
    MENTALMARS,
    DetailsHarness,
    message_rows,
    store_details,
)
from test_shift_fanout import (
    CODE_A,
    CODE_B,
    CODE_C,
    NOW,
    ROLE,
    V3,
    FakePoster,
    add_guild,
    item,
    rows,
    seed,
)
from v3_fakes import FakeEditors

from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.details import DetailObservation
from newsbot.shift.edits import edit_changed_alerts
from newsbot.shift.fanout import FanoutDeps, deliver_queued_codes, detect_and_fan_out
from newsbot.shift.sweep import EditForbiddenError, MessageGoneError
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import DETAILS_STAMP_FLOOR

HOUR = timedelta(hours=1)


class _Poster(FakePoster):
    """A poster whose message ids keep counting across runs, like Discord's, and can be slow."""

    def __init__(self, harness, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._harness = harness

    async def post(self, alert):
        if self.fail:
            raise self.fail.pop(0)
        self._harness.mono += self._harness.post_cost
        self.sent.append(alert)
        ids = self._harness.last_id
        ids[self.guild_id] = ids.get(self.guild_id, 9000) + 1
        return ids[self.guild_id]


class EditHarness(DetailsHarness):
    """The details harness with an editor factory and a monotonic clock the test moves.

    Message ids count on across runs per server (9001, 9002, ...), so a second alert to the
    same server is a different message, as it would be on Discord. `post_cost` is how many
    monotonic seconds each post takes.
    """

    def __init__(self, db_path, cfg, now):
        super().__init__(db_path, cfg, now)
        self.editors = FakeEditors()
        self.mono = 0.0
        self.sleeps: list[float] = []
        self.last_id: dict[int, int] = {}
        self.post_cost = 0.0

    def factory(self, guild_id, channel_id, ping, notify):
        poster = _Poster(self, guild_id, channel_id, ping, fail=self.fail_with.get(guild_id))
        self.posters[guild_id] = poster
        return poster

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(seconds)
            self.mono += seconds

    def deps(self) -> FanoutDeps:
        return replace(
            super().deps(),
            editor_for=self.editors,
            pace_s=0.0,
            clock=lambda: self.mono,
            sleep=self._sleep,
        )


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
    harness = EditHarness(db_path, cfg, NOW)
    seed(db_path)
    return harness


def learn(h, code, reward, *, at):
    """The bot learns `code`'s reward at `at` (a table row, stored the way detection stores it)."""
    store_details(h.db_path, code, reward=reward, now=at)


def message_state(db_path, guild_id=None):
    sql = "SELECT guild_id, message_id, state, rendered_at FROM guild_alert_messages"
    args: tuple = ()
    if guild_id is not None:
        sql += " WHERE guild_id = ?"
        args = (guild_id,)
    return rows(db_path, sql + " ORDER BY guild_id, message_id", *args)


def blocked_at(db_path, guild_id):
    return rows(db_path, "SELECT edits_blocked_at FROM guild_shift WHERE guild_id = ?", guild_id)[
        0
    ][0]


# --- what gets edited, and how ---


async def test_a_details_change_edits_every_servers_message_with_its_own_header(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    body = (
        f"**New SHiFT code**\n\n```\n{CODE_A}\n```\n"
        f"3 Golden Keys · details via {MENTALMARS}\n{GEARBOX} · <https://example.com/AAAA>"
    )
    assert [(c.guild_id, c.channel_id, c.message_id, c.content) for c in h.editors.calls] == [
        (1, 100, 9001, f"@everyone {body}"),
        (2, 200, 9001, f"<@&{ROLE}> {body}"),
    ]
    for _guild, _message, state, rendered_at in message_state(h.db_path):
        assert state == "live"
        assert datetime.fromisoformat(rendered_at) == NOW + HOUR  # the stamp, not the clock


async def test_nothing_to_edit_when_nothing_changed(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])

    h.clock = NOW + HOUR
    await h.run([])

    assert h.editors.attempts == []


async def test_an_edit_is_done_once_not_every_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    for hours in (2, 3, 4):
        h.clock = NOW + hours * HOUR
        await h.run([])

    assert len(h.editors.calls) == 1


async def test_the_clock_alone_never_makes_an_alert_due_for_an_edit(h):
    # An expiry shows as a date and Discord's own live timestamp, so the text never needs
    # a refresh as the deadline nears (it once said "in 6 hours" and would have had to).
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    obs = DetailObservation(
        CODE_A,
        ItemDetails(
            code=CODE_A, rank="table", reward="3 Golden Keys", expires_on=date(2026, 10, 3)
        ),
        MENTALMARS,
        None,
    )
    with closing(connect(h.db_path)) as conn:
        repo.upsert_code_details(conn, [obs], now=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    (call,) = h.editors.calls
    stamp = int(
        datetime(2026, 10, 4, 7, 0, tzinfo=UTC).timestamp()
    )  # Oct 3 ends at Pacific midnight
    assert f"expires Oct 3, 2026 (<t:{stamp}:R>)" in call.content

    # Two days later, then the final hour, then after the deadline: nothing is due.
    for moment in (
        NOW + timedelta(days=2),
        datetime(2026, 10, 4, 6, 30, tzinfo=UTC),
        NOW + timedelta(days=9),
    ):
        h.clock = moment
        await h.run([])
    assert len(h.editors.calls) == 1


async def test_a_message_of_several_codes_is_rerendered_whole_in_its_original_order(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE  # two to a message: A and B, then C
    golden = item(CODE_A, title="Borderlands 4 Golden Key SHiFT code: " + CODE_A, url="https://e/a")
    await h.run([golden, item(CODE_B, url="https://e/b"), item(CODE_C, url="https://e/c")])
    first_alert = h.sent(1)[0]
    learn(h, CODE_B, "A skin", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert call.message_id == 9001  # C's message (9002) holds nothing that changed
    assert call.content.startswith("@everyone **New SHiFT codes**\n\n")
    assert call.content.index(CODE_A) < call.content.index(CODE_B)
    assert CODE_C not in call.content
    assert f"Golden Key: {GEARBOX}" in call.content  # the batch was mixed, and still is
    assert f"```\n{CODE_B}\n```\nA skin · details via {MENTALMARS}\n{GEARBOX}" in call.content
    # The same blocks it went out with, plus the one new line.
    assert call.content.replace(f"A skin · details via {MENTALMARS}\n", "") == first_alert.content


async def test_only_the_servers_holding_the_changed_code_are_touched(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await h.run([item(CODE_A)])  # servers 1 and 2: message 9001 each
    add_guild(h.db_path, 3, ping="everyone")  # joins after A went out
    await h.run([item(CODE_B, url="https://example.com/b")])  # 9002 for 1 and 2, 9001 for 3

    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert [(c.guild_id, c.message_id) for c in h.editors.calls] == [(1, 9001), (2, 9001)]
    assert all(CODE_A in c.content and CODE_B not in c.content for c in h.editors.calls)

    learn(h, CODE_B, "A skin", at=NOW + 3 * HOUR)
    h.clock = NOW + 4 * HOUR
    await h.run([])
    assert [(c.guild_id, c.message_id) for c in h.editors.calls[2:]] == [
        (1, 9002),
        (2, 9002),
        (3, 9001),
    ]
    assert all(CODE_B in c.content and CODE_A not in c.content for c in h.editors.calls[2:])


async def test_a_servers_message_is_never_found_through_another_servers_code(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    add_guild(h.db_path, 2, ping="everyone", enabled_at=NOW + HOUR)  # opts in after A
    h.clock = NOW + 2 * HOUR
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    await h.run([])

    assert [c.guild_id for c in h.editors.calls] == [1]
    with closing(connect(h.db_path)) as conn:
        assert repo.alert_messages_to_edit(conn, now=h.clock) == []


# --- the 14-day window ---


@pytest.mark.parametrize(("age_days", "edited"), [(13, True), (15, False)])
async def test_only_alerts_posted_in_the_last_14_days_are_edited(h, age_days, edited):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "UPDATE guild_alert_messages SET posted_at = ?",
            ((NOW - timedelta(days=age_days)).isoformat(),),
        )
    learn(h, CODE_A, "3 Golden Keys", at=NOW - HOUR)

    await h.run([])

    assert bool(h.editors.calls) is edited


# --- things that have no record ---


async def test_roundups_are_never_edited(h):
    add_guild(h.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]
    await h.run([item(*many, url="https://example.com/megathread")])
    assert h.sent(1) and message_rows(h.db_path) == []
    learn(h, many[0], "A skin", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert h.editors.attempts == []


async def test_an_alert_from_before_the_records_existed_is_never_edited(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("DELETE FROM guild_alert_messages")  # what a 3.0.5 alert looks like
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert h.editors.attempts == []


async def test_no_editor_means_no_edits_and_no_complaints(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    deps = replace(h.deps(), editor_for=None)
    outcome = await edit_changed_alerts(deps)

    assert (outcome.edited, outcome.waiting) == (0, 0)
    assert h.editors.attempts == []


async def test_the_cli_preview_never_edits(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    await detect_and_fan_out(replace(h.deps(), preview=True), [], seeding_ok=True)

    assert h.editors.attempts == []
    assert message_state(h.db_path)[0][3] == DETAILS_STAMP_FLOOR.isoformat()


async def test_startup_delivery_does_no_edits(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    await deliver_queued_codes(h.deps(), startup=True)

    assert h.editors.attempts == []


# --- a message that's gone ---


async def test_a_deleted_message_is_done_quietly_and_never_retried(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [MessageGoneError("10008")]

    h.clock = NOW + 2 * HOUR
    await h.run([])
    learn(h, CODE_A, "2 Golden Keys", at=NOW + 3 * HOUR)  # a later change finds nothing to edit
    h.clock = NOW + 4 * HOUR
    await h.run([])

    assert len(h.editors.attempts) == 1
    assert [(s[2]) for s in message_state(h.db_path)] == ["gone"]
    assert h.notices == []


# --- missing permission ---


async def _two_messages_each(h, guilds):
    for g in guilds:
        add_guild(h.db_path, g, ping="everyone")
    h.reserve = BIG_RESERVE  # A and B in message 9001, C in 9002
    await h.run(
        [
            item(CODE_A, url="https://e/a"),
            item(CODE_B, url="https://e/b"),
            item(CODE_C, url="https://e/c"),
        ]
    )


async def test_a_forbidden_edit_sends_one_notice_and_stops_that_server(h):
    await _two_messages_each(h, (1, 2))
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    learn(h, CODE_C, "A skin", at=NOW + HOUR)
    h.editors.fail[9001] = [EditForbiddenError("50013")]

    h.clock = NOW + 2 * HOUR
    await h.run([])

    # Server 1: one attempt (its first message) and then nothing, so 9002 is skipped without
    # asking; server 2 is a different story and gets both of its edits.
    assert [(c.guild_id, c.message_id) for c in h.editors.attempts] == [
        (1, 9001),
        (2, 9001),
        (2, 9002),
    ]
    assert sorted((c.guild_id, c.message_id) for c in h.editors.calls) == [(2, 9001), (2, 9002)]
    assert len(h.notices) == 1
    guild_id, text = h.notices[0]
    assert guild_id == 1
    assert "<#100>" in text and "/newsbot shift" in text and "permission" in text
    assert blocked_at(h.db_path, 1) is not None
    assert blocked_at(h.db_path, 2) is None


async def test_a_blocked_server_stays_quiet_until_its_shift_settings_are_saved(h):
    await _two_messages_each(h, (1,))
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [EditForbiddenError("50001")]
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert len(h.notices) == 1

    # More changes, more passes: not one more attempt, not one more notice.
    learn(h, CODE_A, "2 Golden Keys", at=NOW + 3 * HOUR)
    for hours in (4, 5):
        h.clock = NOW + hours * HOUR
        await h.run([])
    assert len(h.editors.attempts) == 1
    assert len(h.notices) == 1

    # An admin fixes the permission and saves /newsbot shift: edits resume, from the new state.
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="everyone")
    assert blocked_at(h.db_path, 1) is None
    h.clock = NOW + 6 * HOUR
    await h.run([])
    assert [c.message_id for c in h.editors.calls] == [9001]
    assert "2 Golden Keys" in h.editors.calls[0].content

    # And if it breaks again, the server hears about it again: one notice per block.
    learn(h, CODE_A, "1 Golden Key", at=NOW + 7 * HOUR)
    h.editors.fail[9001] = [EditForbiddenError("50013")]
    h.clock = NOW + 8 * HOUR
    await h.run([])
    assert len(h.notices) == 2


async def test_a_notice_that_cant_be_sent_does_not_stop_the_pass(h):
    await _two_messages_each(h, (1, 2))
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [EditForbiddenError("50013")]

    async def broken(_guild_id, _text):
        raise RuntimeError("admin channel is gone too")

    h.notify_guild = broken
    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert 2 in {c.guild_id for c in h.editors.calls}
    assert blocked_at(h.db_path, 1) is not None


# --- a 5xx, a 429, the unexpected ---


async def test_a_5xx_leaves_the_message_due_for_the_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [PublishError("discord is having a day")]

    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert h.editors.calls == []
    assert datetime.fromisoformat(message_state(h.db_path)[0][3]) == DETAILS_STAMP_FLOOR

    h.clock = NOW + 3 * HOUR
    await h.run([])
    assert [c.message_id for c in h.editors.calls] == [9001]


async def test_a_429_waits_what_discord_asked_and_tries_again(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [PublishError("slow down", rejected=True, retry_after=3.5)]

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert h.sleeps == [3.5]
    assert len(h.editors.attempts) == 2 and len(h.editors.calls) == 1


async def test_a_429_that_outlasts_the_budget_waits_for_the_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [PublishError("slow down", rejected=True, retry_after=600.0)]

    h.clock = NOW + 2 * HOUR
    await h.run([])

    # Server 1's wait doesn't fit in the walk's 100 seconds, so it isn't taken. Server 2 is a
    # different channel with a different bucket, and goes ahead (message ids match: 9001 each).
    assert h.sleeps == []
    assert [(c.guild_id, c.message_id) for c in h.editors.attempts] == [(1, 9001), (2, 9001)]
    assert [c.guild_id for c in h.editors.calls] == [2]

    h.clock = NOW + 3 * HOUR
    await h.run([])
    assert [c.guild_id for c in h.editors.calls] == [2, 1]


async def test_a_message_that_keeps_getting_429s_gives_up_for_this_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [PublishError("slow", rejected=True, retry_after=1.0)] * 5

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert len(h.editors.attempts) == 3
    assert h.editors.calls == []
    assert datetime.fromisoformat(message_state(h.db_path)[0][3]) == DETAILS_STAMP_FLOOR


async def test_an_unexpected_error_is_logged_and_the_rest_carry_on(h, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [RuntimeError("somebody's bug")]

    h.clock = NOW + 2 * HOUR
    with caplog.at_level(logging.ERROR):
        await h.run([])

    assert [c.guild_id for c in h.editors.calls] == [2]
    assert any("unexpectedly" in r.getMessage() for r in caplog.records)
    assert h.notices == []


# --- a message that won't fit ---


def _eight_codes():
    names = ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV", "WWWW", "XXXX")
    return [f"{n}1-AAAAA-AAAAA-AAAAA-AAAAA" for n in names]


async def test_a_message_that_cannot_be_rerendered_is_logged_once_and_not_retried(h, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    codes = _eight_codes()
    # No reserve: all eight share one message, which is exactly what a config that gained a
    # longer source name later looks like to an old, tightly packed message.
    await h.run([item(c, url=f"https://example.com/{c[:4]}") for c in codes])
    assert [a.codes for a in h.sent(1)] == [codes]
    for i, code in enumerate(codes):
        learn(h, code, "r" * 300, at=NOW + HOUR + timedelta(seconds=i))

    h.clock = NOW + 2 * HOUR
    with caplog.at_level(logging.ERROR):
        await h.run([])
        await h.run([])
        await h.run([])

    assert h.editors.attempts == []
    assert len([r for r in caplog.records if "re-rendered" in r.getMessage()]) == 1
    (_guild, _message, state, rendered_at) = message_state(h.db_path)[0]
    assert state == "live"
    assert datetime.fromisoformat(rendered_at) == NOW + HOUR + timedelta(seconds=7)

    # A newer change is a new stamp, and gets its own try (and its own, single, complaint).
    learn(h, codes[0], "s" * 300, at=NOW + 3 * HOUR)
    h.clock = NOW + 4 * HOUR
    with caplog.at_level(logging.ERROR):
        await h.run([])
    assert len([r for r in caplog.records if "re-rendered" in r.getMessage()]) == 2


# --- the budget ---


async def test_deliveries_go_first_then_the_edits(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    seen: list[int] = []

    def when_edited(_call):
        seen.append(len(h.sent(1)))

    h.editors.on_edit = when_edited
    await h.run([item(CODE_B, url="https://example.com/b")])

    assert seen == [1]  # the new alert had already been posted when the edit began
    assert [a.codes for a in h.sent(1)] == [[CODE_B]]


async def test_a_deadline_mid_edits_leaves_the_rest_flagged_and_the_next_pass_does_them(h):
    guilds = (1, 2, 3, 4, 5)
    for g in guilds:
        add_guild(h.db_path, g, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    def each_edit_takes_40_seconds(_call):
        h.mono += 40.0

    h.editors.on_edit = each_edit_takes_40_seconds
    h.clock = NOW + 2 * HOUR
    await h.run([])
    # Edits start at 0, 40 and 80 seconds; the one due at 120 is past the walk's 100.
    assert [c.guild_id for c in h.editors.calls] == [1, 2, 3]

    await h.run([])
    assert [c.guild_id for c in h.editors.calls] == [1, 2, 3, 4, 5]
    await h.run([])
    assert len(h.editors.calls) == 5


async def test_deliveries_that_use_up_the_budget_leave_every_edit_for_the_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR

    h.post_cost = 150.0  # the delivery alone outlasts the 100 second walk
    await h.run([item(CODE_B, url="https://example.com/b")])
    assert h.editors.attempts == []

    h.post_cost = 0.0
    await h.run([])
    assert [c.message_id for c in h.editors.calls] == [9001]


async def test_the_edit_pass_is_paced_like_the_delivery_walk(h):
    for g in (1, 2, 3):
        add_guild(h.db_path, g, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    paced: list[float] = []

    async def sleep(seconds: float) -> None:
        paced.append(seconds)

    deps = replace(h.deps(), pace_s=0.2, sleep=sleep)
    h.clock = NOW + 2 * HOUR
    outcome = await edit_changed_alerts(deps, deadline=100.0)

    assert outcome.edited == 3
    assert paced == [0.2, 0.2]  # between edits, not before the first or after the last


# --- a busy database ---


async def test_a_busy_database_waits_for_the_next_pass(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    def busy(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("newsbot.shift.edits.alert_messages_to_edit", busy)
    outcome = await edit_changed_alerts(h.deps())
    assert outcome.edited == 0 and h.editors.attempts == []


# --- the edit storm ---


def table_row(code, reward):
    """What the shift_table collector hands over for one row of mentalmars' main table."""
    return RawItem(
        url="https://codes.example/borderlands-4-shift-codes/",
        title=code,
        excerpt=code,
        source_name=MENTALMARS,
        trust="community",
        published_at=None,
        topics=("borderlands4",),
        full_text=code,
        details=ItemDetails(
            code=code, rank="table", reward=reward, listed=True, listed_in=("main",)
        ),
    )


async def test_a_reward_that_flaps_costs_at_most_one_edit_per_message_per_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    await h.run([item(CODE_A)])

    per_pass: list[int] = []
    for n in range(8):
        h.clock = NOW + (n + 1) * HOUR
        before = len(h.editors.calls)
        await h.run([table_row(CODE_A, "1 Golden Key" if n % 2 == 0 else "2 Golden Keys")])
        per_pass.append(len(h.editors.calls) - before)

    # Every pass really changes the value, so every pass edits: but only each message, once.
    assert per_pass == [2] * 8
    assert [c.message_id for c in h.editors.calls] == [9001] * 16
    # And a pass that repeats the value it already has edits nothing.
    before = len(h.editors.calls)
    h.clock = NOW + 10 * HOUR
    await h.run([table_row(CODE_A, "2 Golden Keys")])
    assert len(h.editors.calls) == before


async def test_a_flap_inside_one_pass_is_judged_on_where_it_ends(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await h.run([item(CODE_A)])
    h.clock = NOW + HOUR
    await h.run([table_row(CODE_A, "1 Golden Key")])
    settled = len(h.editors.calls)
    assert settled == 2

    # Three readings in one pass that end where they started: no net change, no edits.
    h.clock = NOW + 2 * HOUR
    flap = [table_row(CODE_A, r) for r in ("2 Golden Keys", "1 Golden Key", "2 Golden Keys")]
    await h.run([*flap, table_row(CODE_A, "1 Golden Key")])
    assert len(h.editors.calls) == settled

    # Four readings that end somewhere new: one edit per message, not one per reading.
    h.clock = NOW + 3 * HOUR
    await h.run([*flap, table_row(CODE_A, "5 Golden Keys")][:4])
    assert len(h.editors.calls) == settled + 2
    assert all("5 Golden Keys" in c.content for c in h.editors.calls[settled:])


# --- the repo pieces on their own ---


async def test_block_guild_edits_answers_true_only_for_the_call_that_did_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    with closing(connect(h.db_path)) as conn:
        assert repo.block_guild_edits(conn, 1, NOW) is True
        assert repo.block_guild_edits(conn, 1, NOW + HOUR) is False
        assert repo.block_guild_edits(conn, 404, NOW) is False  # no such server, nothing to block
    assert datetime.fromisoformat(blocked_at(h.db_path, 1)) == NOW  # the first time sticks


async def test_a_rendered_stamp_only_moves_forward(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn:
        repo.mark_alert_message_rendered(conn, 1, 9001, NOW + 2 * HOUR)
        repo.mark_alert_message_rendered(conn, 1, 9001, NOW + HOUR)
    assert datetime.fromisoformat(message_state(h.db_path)[0][3]) == NOW + 2 * HOUR


async def test_a_gone_or_blocked_message_is_not_a_candidate(h):
    for g in (1, 2):
        add_guild(h.db_path, g, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    with closing(connect(h.db_path)) as conn:
        assert [t.guild_id for t in repo.alert_messages_to_edit(conn, now=NOW)] == [1, 2]
        repo.mark_alert_message_gone(conn, 1, 9001)
        repo.block_guild_edits(conn, 2, NOW)
        assert repo.alert_messages_to_edit(conn, now=NOW) == []


async def test_a_removed_server_takes_its_alert_records_with_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    with closing(connect(h.db_path)) as conn:
        repo.delete_guild(conn, 1)
        assert repo.alert_messages_to_edit(conn, now=NOW) == []
