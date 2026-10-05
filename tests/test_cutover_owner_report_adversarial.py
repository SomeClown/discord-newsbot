"""The owner's daily report after the cutover: what it counts, how it's guarded, how it fails.

Nobody asked for this one. The owner report is the single new message v3 sends on a schedule
to somebody who didn't ask for a message, and the somebody is me, so at least the complaint
department is close. It still has to be right, because it's the only place a failed digest in
someone else's server shows up before that someone else mentions it in a review. Three things
can go wrong with it: it can count wrong (a partial digest read as a failure, a v2.2 row that
belongs to nobody read as a server), it can say the same thing twice (two restarts in one
day), or it can say nothing for a day it should have (the guard is written before the
send, which is the trade I made on purpose and want written down with a test next to it).

`outcome_from_digest` is the other half: a handful of pattern checks that turn a failed
row's notes into a reason. They used to be bare substrings, which is fine for the notes the bot
writes and less fine for notes that happen to contain a Discord channel id, because a snowflake
is a long string of digits and "403" is a short one. They match on word boundaries now.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from cutover_world import DUE, OWNER_CH, TODAY

import newsbot.bot.client as client_module
from newsbot.bot.format import DigestOutcome, outcome_from_digest
from newsbot.store import repo
from newsbot.store.db import connect

REPORT_AT = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)  # 21:00 PDT on the first of October


@pytest.fixture
def clock(monkeypatch):
    """Move the owner report job's own `datetime.now` (it asks the module, not the bot)."""
    state = {"now": REPORT_AT}

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"].astimezone(tz) if tz else state["now"].replace(tzinfo=None)

    monkeypatch.setattr(client_module, "datetime", FrozenDatetime)
    return state


def digest(world, guild_id, status, *, notes=None, updated=None, run_date=TODAY):
    stamp = (updated or REPORT_AT - timedelta(hours=5)).isoformat()
    with closing(connect(world.db_path)) as conn, conn:
        repo.create_guild(conn, guild_id, set_up=True)
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, error_notes, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, run_date, status, notes, stamp, stamp),
        )


def clean_slate(world) -> None:
    """Forget the fixture's old digests, so a test counts only the rows it writes itself."""
    world.run("DELETE FROM digests")


def reports(world) -> list[str]:
    return [m.content for m in world.sent(OWNER_CH) if m.content.startswith("newsbot daily")]


# --- outcome_from_digest: every status, every reason ---


@pytest.mark.parametrize(
    ("status", "notes", "expected"),
    [
        ("ok", None, DigestOutcome(True)),
        ("partial", "palworld: skipped (403 Forbidden)", DigestOutcome(True)),
        ("partial", None, DigestOutcome(True)),
        ("pending", None, DigestOutcome(False, "interrupted")),
        ("pending", "stale lease 403", DigestOutcome(False, "interrupted")),
        ("failed", "publish failed: 403 Forbidden (error code: 50013)", None),
        ("failed", "publish failed: Missing Permissions", None),
        ("failed", "PUBLISH FAILED: FORBIDDEN", None),
        ("failed", "publish failed: 404 Not Found (error code: 10003): Unknown Channel", None),
        ("failed", "publish failed: 429 Too Many Requests", None),
        ("failed", "publish failed: you are being rate limited", None),
        ("failed", "build failed: the model call timed out", None),
        ("failed", "publish failed: request timeout", None),
        ("failed", "build failed: no such table: game_summaries", None),
        ("failed", "unhandled error: ValueError('nope')", None),
        ("failed", "", None),
        ("failed", None, None),
    ],
)
def test_every_status_lands_in_the_category_the_report_will_count_it_under(status, notes, expected):
    outcome = outcome_from_digest(status, notes)

    if expected is not None:
        assert outcome == expected
        return
    reason = {
        "publish failed: 403 Forbidden (error code: 50013)": "missing permissions",
        "publish failed: Missing Permissions": "missing permissions",
        "PUBLISH FAILED: FORBIDDEN": "missing permissions",
        "publish failed: 404 Not Found (error code: 10003): Unknown Channel": "channel gone",
        "publish failed: 429 Too Many Requests": "rate limited",
        "publish failed: you are being rate limited": "rate limited",
        "build failed: the model call timed out": "timed out",
        "publish failed: request timeout": "timed out",
        "build failed: no such table: game_summaries": "couldn't build",
        "unhandled error: ValueError('nope')": "other",
        "": "other",
        None: "other",
    }[notes]
    assert outcome == DigestOutcome(False, reason)


def test_a_note_that_matches_two_categories_takes_the_first_in_the_list():
    # "most specific first": permissions beat a timeout that happened on the way to them.
    outcome = outcome_from_digest("failed", "publish failed: 403 Forbidden after a timeout")

    assert outcome.reason == "missing permissions"


def test_a_status_nobody_writes_counts_as_a_failure_not_as_a_post():
    assert outcome_from_digest("skipped", None) == DigestOutcome(False, "other")


@pytest.mark.parametrize(
    "notes",
    [
        "publish failed: channel 1452403235274240221 can't receive messages (not a text channel)",
        "publish failed: channel 1429017235274240221 can't receive messages (not a text channel)",
    ],
)
def test_digits_in_a_channel_id_do_not_pick_the_reason(notes):
    assert outcome_from_digest("failed", notes).reason == "other"


# --- what the day's report counts ---


async def test_the_first_report_after_cutover_counts_the_friends_adopted_v22_digest(
    make_world, v22_db, clock
):
    from cutover_world import v22_digest

    v22_digest(v22_db, TODAY, "ok", posted_ids="[301]")  # v2.2 posted at 09:00 PDT
    world = await make_world(now=DUE + timedelta(hours=1), owner_channel=True)

    await world.bot._owner_report_job()

    (text,) = reports(world)
    assert text.splitlines()[0] == "newsbot daily: digests posted to 1 of 1 server"


async def test_partial_counts_as_posted_and_pending_and_failures_are_grouped(make_world, clock):
    world = await make_world(now=DUE, owner_channel=True)
    clean_slate(world)
    digest(world, 11, "ok")
    digest(world, 12, "partial", notes="palworld: skipped (404)")
    digest(world, 13, "failed", notes="publish failed: 403 Forbidden")
    digest(world, 14, "failed", notes="publish failed: 403 Forbidden")
    digest(world, 15, "pending")

    await world.bot._owner_report_job()

    (text,) = reports(world)
    assert text.splitlines()[0] == (
        "newsbot daily: digests posted to 2 of 5 servers; "
        "3 failed (2 missing permissions, 1 interrupted)"
    )


async def test_rows_nobody_owns_and_rows_older_than_a_day_are_not_servers(make_world, clock):
    from cutover_world import v22_digest

    world = await make_world(now=DUE, owner_channel=True)
    clean_slate(world)
    v22_digest(world.db_path, "2026-10-01", "failed", notes="orphan")  # no server, no vote
    digest(world, 21, "failed", notes="403", updated=REPORT_AT - timedelta(hours=25))
    digest(world, 22, "ok", updated=REPORT_AT - timedelta(hours=23, minutes=59))

    await world.bot._owner_report_job()

    (text,) = reports(world)
    assert text.splitlines()[0] == "newsbot daily: digests posted to 1 of 1 server"


async def test_a_server_with_two_recent_rows_is_counted_once_by_its_newest(make_world, clock):
    world = await make_world(now=DUE, owner_channel=True)
    clean_slate(world)
    older = REPORT_AT - timedelta(hours=20)
    digest(world, 31, "failed", notes="403", run_date="2026-09-30", updated=older)
    digest(world, 31, "ok", run_date="2026-10-01", updated=REPORT_AT - timedelta(hours=5))

    await world.bot._owner_report_job()

    (text,) = reports(world)
    assert text.splitlines()[0] == "newsbot daily: digests posted to 1 of 1 server"


# --- the once-a-day guard ---


async def test_two_process_restarts_in_one_day_send_one_report(make_world, clock):
    world = await make_world(now=DUE, owner_channel=True)
    digest(world, 41, "ok")
    await world.bot._owner_report_job()

    again = await make_world(now=DUE, owner_channel=True, channels=world.channels)
    third = await make_world(now=DUE, owner_channel=True, channels=world.channels)
    await again.bot._owner_report_job()
    await third.bot._owner_report_job()

    assert len(reports(world)) == 1


async def test_the_guard_is_the_owners_local_date_not_utc(make_world, clock):
    world = await make_world(now=DUE, owner_channel=True)
    digest(world, 51, "ok")

    clock["now"] = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)  # 21:00 on the 1st in Pacific
    await world.bot._owner_report_job()
    clock["now"] = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)  # 23:00 on the 1st: same day
    await world.bot._owner_report_job()
    assert len(reports(world)) == 1

    clock["now"] = datetime(2026, 10, 2, 7, 30, tzinfo=UTC)  # 00:30 on the 2nd: a new day
    await world.bot._owner_report_job()
    assert len(reports(world)) == 2
    with closing(connect(world.db_path)) as conn:
        assert repo.app_state_get(conn, "owner_report_date") == "2026-10-02"


async def test_a_crash_after_the_date_is_written_costs_that_day_its_report(
    make_world, clock, monkeypatch
):
    # Pinned. The date is written before rendering and sending, so a crash in between (the
    # renderer here) means the owner gets the "daily report crashed" alert instead of the
    # report, and a retry later the same day sends nothing. That loses one report; the
    # alternative (write the date last) sends two when the crash lands after the send. I'd
    # take the lost report both times: it's a convenience, and the crash alert says so.
    world = await make_world(now=DUE, owner_channel=True)
    digest(world, 61, "ok")

    def boom(outcomes, failing):
        raise RuntimeError("renderer fell over")

    with monkeypatch.context() as broken:
        broken.setattr(client_module, "render_owner_report", boom)
        await world.bot._owner_report_job()
    await world.bot._owner_report_job()  # the renderer is fine again

    texts = [m.content for m in world.sent(OWNER_CH)]
    assert [t for t in texts if "daily report crashed" in t] != []
    assert reports(world) == []  # and the retry stayed quiet, as pinned


async def test_a_report_nobody_can_receive_is_still_marked_as_sent(make_world, clock):
    # The same trade from the other side: with no owner channel there's nowhere to send, and
    # the day is consumed anyway. Setting the channel later the same day won't resend.
    from cutover_world import PRODLIKE

    from newsbot.config import load_config

    cfg = load_config(PRODLIKE).model_copy(update={"admin_channel_id": None})
    world = await make_world(now=DUE, cfg=cfg)

    await world.bot._owner_report_job()

    with closing(connect(world.db_path)) as conn:
        assert repo.app_state_get(conn, "owner_report_date") == TODAY


async def test_the_report_goes_to_the_owner_channel_and_not_to_the_friend(make_world, clock):
    # With the owner's own channel configured, the friend's admin channel hears about the
    # friend's digests (the run report) and nothing about the owner's day.
    world = await make_world(now=DUE, owner_channel=True)
    digest(world, 71, "failed", notes="403")

    await world.bot._owner_report_job()

    from cutover_world import FRIEND_ADMIN_CH

    assert len(reports(world)) == 1
    assert [m for m in world.sent(FRIEND_ADMIN_CH) if "newsbot daily" in m.content] == []


async def test_a_source_dropped_from_the_config_leaves_the_failing_list(make_world, clock):
    # `source_health` keeps every name it ever saw. The Lodestone feeds were dropped
    # from the catalog after a week of 403s from the Droplet; without the filter they'd
    # sit in every nightly report forever, frozen at their last count.
    world = await make_world(now=DUE, owner_channel=True)
    digest(world, 61, "ok")
    configured = sorted(world.bot.cfg.configured_source_names())
    assert configured, "the prod-like config should have sources"
    with closing(connect(world.db_path)) as conn:
        for _ in range(3):
            repo.record_source_result(conn, "FFXIV Lodestone News", DUE, "403 Forbidden")
            repo.record_source_result(conn, configured[0], DUE, "500 Server Error")

    await world.bot._owner_report_job()

    (text,) = reports(world)
    assert configured[0] in text
    assert "Lodestone" not in text
