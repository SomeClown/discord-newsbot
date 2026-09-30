"""Adversarial tests for the per-guild due check and window arithmetic (plan task 6).

The due check's one big promise is "every server gets exactly one digest per
local day, at its local time, and never two", and the people I'd be making that
promise to live in zones that have half-hour offsets, forty-five-minute
offsets, a +14 that is tomorrow for everyone else, and clocks that change at
midnight (which is a bad time for anything that thinks in local dates; ask me
how I know). So most of this file is one property, run against a pile of real
zones on their actual 2026 transition days: tick every minute, act on whatever
`due_guilds` says, and count the digests per local day. The rest attacks the
small rules (retry spacing, resume, catch-up labelling), hostile settings
strings, a clock that steps backwards, and a few thousand servers at once.

Pure functions, no database, no network, no real clock. The holes this found (a
late-night digest time couldn't retry or resume across local midnight) are
fixed, and their xfail markers are gone.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot.guilds.schedule import (
    CATCH_UP_AFTER,
    LEASE_STALE_AFTER,
    MAX_ATTEMPTS,
    RETRY_AFTER,
    WINDOW_FLOOR,
    digest_window,
    due_guilds,
    local_due_instant,
)
from newsbot.store.models import DueCandidate

LA = "America/Los_Angeles"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def cand(guild_id=1, digest_time="09:00", tz=LA, **latest) -> DueCandidate:
    return DueCandidate(
        guild_id=guild_id, digest_time=digest_time, timezone=tz, tier="free", **latest
    )


# --- the offset classes ---


@pytest.mark.parametrize(
    ("zone", "due_utc", "local_date"),
    [
        ("Asia/Kolkata", utc(2026, 9, 30, 3, 30), date(2026, 9, 30)),
        ("Asia/Kathmandu", utc(2026, 9, 30, 3, 15), date(2026, 9, 30)),
        # +14: 09:00 there is 19:00 *yesterday* in UTC, and the local date is
        # a day ahead of UTC's for most of the UTC day.
        ("Pacific/Kiritimati", utc(2026, 9, 29, 19, 0), date(2026, 9, 30)),
    ],
)
def test_odd_offsets_are_due_at_exactly_their_local_nine(zone, due_utc, local_date):
    c = cand(tz=zone)
    assert due_guilds([c], due_utc - timedelta(seconds=1)) == []
    [due] = due_guilds([c], due_utc)
    assert (due.run_date, due.due_at, due.reason, due.catch_up) == (
        local_date,
        due_utc,
        "first",
        False,
    )


def test_lord_howe_thirty_minute_dst_gap_lands_the_same_distance_into_the_gap():
    # 2026-10-04: 02:00 (+10:30) becomes 02:30 (+11:00); 02:15 doesn't exist.
    assert local_due_instant(date(2026, 10, 4), "02:15", "Australia/Lord_Howe") == utc(
        2026, 10, 3, 15, 45
    )


def test_a_zone_that_skipped_a_whole_day_is_not_applicable_but_does_not_crash():
    # Pacific/Apia skipped 2011-12-30 entirely. Nobody's clock is set there
    # in 2026, so this only checks the arithmetic stays an answer, not an error.
    assert isinstance(local_due_instant(date(2011, 12, 30), "09:00", "Pacific/Apia"), datetime)


# --- exactly one digest per local day, every minute of the transition days ---

_TRANSITIONS = [
    (LA, utc(2026, 3, 7, 12)),
    (LA, utc(2026, 10, 31, 12)),
    ("Australia/Sydney", utc(2026, 10, 3, 0)),
    ("Australia/Sydney", utc(2026, 4, 4, 0)),
    ("Australia/Lord_Howe", utc(2026, 10, 3, 0)),
    ("Australia/Lord_Howe", utc(2026, 4, 4, 0)),
    ("America/Santiago", utc(2026, 9, 5, 0)),
    ("America/Santiago", utc(2026, 4, 3, 12)),
    ("Africa/Cairo", utc(2026, 10, 28, 0)),
    ("Africa/Cairo", utc(2026, 4, 23, 0)),
    ("Europe/London", utc(2026, 3, 28, 0)),
    ("Europe/London", utc(2026, 10, 24, 0)),
    ("America/Havana", utc(2026, 3, 7, 12)),
    ("Asia/Kathmandu", utc(2026, 9, 29, 0)),
]


def _simulate(zone: str, digest_time: str, start: datetime, days: int = 3):
    """Tick once a minute; every time a guild is due, it posts `ok` for that day."""
    row: tuple[date, datetime] | None = None
    posts: list[tuple[date, datetime, datetime]] = []
    for minute in range(days * 1440):
        now = start + timedelta(minutes=minute)
        c = cand(
            digest_time=digest_time,
            tz=zone,
            **({"run_date": row[0], "status": "ok", "updated_at": row[1]} if row else {}),
        )
        due = due_guilds([c], now)
        if due:
            assert len(due) == 1
            posts.append((due[0].run_date, now, due[0].due_at))
            row = (due[0].run_date, now)
    return posts


@pytest.mark.parametrize("digest_time", ["00:00", "00:30", "01:30", "02:30", "23:59"])
@pytest.mark.parametrize(("zone", "start"), _TRANSITIONS)
def test_every_local_day_gets_exactly_one_digest_on_time(zone, start, digest_time):
    posts = _simulate(zone, digest_time, start)

    dates = [p[0] for p in posts]
    assert len(set(dates)) == len(dates), "a local day got two digests"
    assert all(b - a == timedelta(days=1) for a, b in zip(dates, dates[1:], strict=False)), (
        f"a local day was skipped: {dates}"
    )
    # Ticks are on whole minutes and every due instant is too, so on-time is exact.
    # (The first post is skipped: the simulation starts mid-day, so it may be late.)
    assert all(now == due_at for _, now, due_at in posts[1:])
    assert len(posts) >= 2


def test_a_digest_timed_at_the_very_last_minute_is_on_the_right_local_day():
    # Kiritimati's 23:59 is 09:59 UTC of the *same* UTC date; its date label is its own.
    c = cand(digest_time="23:59", tz="Pacific/Kiritimati")
    [due] = due_guilds([c], utc(2026, 9, 30, 9, 59))
    assert due.run_date == date(2026, 9, 30)
    assert due_guilds([c], utc(2026, 9, 30, 9, 58)) == []


# --- retry, resume, and the catch-up label ---


def _failed(attempts=1, age=RETRY_AFTER + timedelta(seconds=1), **kw):
    return cand(
        run_date=date(2026, 9, 30),
        status="failed",
        attempts=attempts,
        updated_at=NOW - age,
        **kw,
    )


NOW = utc(2026, 9, 30, 16, 30)


def test_a_clean_failure_is_not_retried_at_exactly_ten_minutes_but_is_just_after():
    assert due_guilds([_failed(age=RETRY_AFTER)], NOW) == []
    [due] = due_guilds([_failed(age=RETRY_AFTER + timedelta(microseconds=1))], NOW)
    assert (due.reason, due.catch_up) == ("retry", True)


def test_retries_stop_at_the_attempt_cap():
    assert due_guilds([_failed(attempts=MAX_ATTEMPTS - 1)], NOW)
    assert due_guilds([_failed(attempts=MAX_ATTEMPTS)], NOW) == []
    assert due_guilds([_failed(attempts=MAX_ATTEMPTS + 7)], NOW) == []


def test_a_failure_with_posts_is_never_retried_however_old_or_few_the_attempts():
    c = _failed(attempts=1, age=timedelta(days=0, hours=5), posted_any=True)
    assert due_guilds([c], NOW) == []


def test_a_failed_row_with_no_timestamp_is_not_retried_and_does_not_crash():
    c = cand(run_date=date(2026, 9, 30), status="failed", attempts=1, updated_at=None)
    assert due_guilds([c], NOW) == []


def test_a_failure_dated_after_the_local_date_is_not_retried():
    c = cand(
        run_date=date(2026, 10, 1),
        status="failed",
        attempts=1,
        updated_at=NOW - timedelta(hours=1),
    )
    assert due_guilds([c], NOW) == []


def test_running_does_not_suppress_a_first_or_a_retry_only_a_resume():
    # `running` exists so a slow digest isn't mistaken for a dead one. A "first" or a
    # "retry" isn't that mistake, and the per-guild lock is what serializes those.
    assert due_guilds([cand(guild_id=1)], NOW, running={1})
    assert due_guilds([_failed(guild_id=1)], NOW, running={1})
    pending = cand(
        guild_id=1, run_date=date(2026, 9, 30), status="pending", window_end=NOW, attempts=1
    )
    assert due_guilds([pending], NOW, running={1}) == []
    assert [d.reason for d in due_guilds([pending], NOW, running={2})] == ["resume"]
    assert [d.reason for d in due_guilds([pending], NOW, running=frozenset())] == ["resume"]


def test_a_pending_row_without_a_window_is_never_resumed_however_old():
    c = cand(run_date=date(2026, 9, 30), status="pending", window_end=None, attempts=1)
    for hours in (0, 1, 7):
        assert due_guilds([c], NOW + timedelta(hours=hours)) == []


@pytest.mark.parametrize("status", ["ok", "partial", "skipped", "", "weird"])
def test_finished_or_unknown_statuses_for_today_are_not_due(status):
    c = cand(run_date=date(2026, 9, 30), status=status, attempts=1, updated_at=NOW)
    assert due_guilds([c], NOW) == []


def test_catch_up_is_labelled_at_five_minutes_and_not_at_four_fifty_nine():
    due_at = utc(2026, 9, 30, 16, 0)
    [on_time] = due_guilds([cand()], due_at + CATCH_UP_AFTER - timedelta(seconds=1))
    [late] = due_guilds([cand()], due_at + CATCH_UP_AFTER)
    assert (on_time.catch_up, late.catch_up) == (False, True)
    assert on_time.due_at == late.due_at == due_at


def test_a_guild_set_up_after_its_time_has_passed_is_due_on_the_next_tick_not_tomorrow():
    # "Late, not cancelled": no row at all, 14:00 local, digest time 09:00.
    [due] = due_guilds([cand()], utc(2026, 9, 30, 21, 0))
    assert (due.reason, due.catch_up, due.run_date) == ("first", True, date(2026, 9, 30))


# --- hostile and broken settings ---


@pytest.mark.parametrize(
    "zone",
    [
        "",
        "America",
        "..",
        "/etc/passwd",
        "a\x00b",
        "zone.tab",
        "UTC ",
        "Not/AZone",
        "America/Los_Angeles/",
    ],
)
def test_a_bad_zone_string_is_skipped_and_the_others_are_still_judged(zone):
    bad = cand(guild_id=1, tz=zone)
    good = cand(guild_id=2)
    assert [d.guild_id for d in due_guilds([bad, good], NOW)] == [2]
    assert [d.guild_id for d in due_guilds([good, bad], NOW)] == [2]


@pytest.mark.parametrize("hhmm", ["", "9", "24:00", "09:60", "ab:cd", "09:00:00", "-1:00", "9:0x"])
def test_a_bad_digest_time_is_skipped_and_the_others_are_still_judged(hhmm):
    bad = cand(guild_id=1, digest_time=hhmm)
    good = cand(guild_id=2)
    assert [d.guild_id for d in due_guilds([bad, good], NOW)] == [2]


def test_a_zone_that_was_valid_when_chosen_and_vanishes_later_only_hurts_that_guild():
    # tzdata renames zones now and then; the stored string keeps its old spelling.
    candidates = [
        cand(guild_id=i, tz="Europe/Kyiv" if i % 2 else "Europe/Kiev-Old") for i in range(1, 9)
    ]
    assert [d.guild_id for d in due_guilds(candidates, utc(2026, 9, 30, 12, 0))] == [1, 3, 5, 7]


def test_a_digest_time_with_stray_whitespace_is_either_handled_or_skipped_never_fatal():
    # int() tolerates spaces, so " 9: 00" happens to parse. The point is no exception.
    due_guilds([cand(digest_time=" 9: 00")], NOW)
    due_guilds([cand(digest_time="9:00 ")], NOW)


# --- the clock misbehaves ---


def test_a_clock_stepped_back_does_not_repost_and_does_not_retry_early():
    done = cand(run_date=date(2026, 9, 30), status="ok", attempts=1, updated_at=NOW)
    for back in (timedelta(minutes=1), timedelta(hours=3), timedelta(days=2)):
        assert due_guilds([done], NOW - back) == []
    failed = _failed()
    assert due_guilds([failed], failed.updated_at - timedelta(minutes=30)) == []


def test_the_newest_row_dated_tomorrow_waits_out_a_clock_that_went_back_across_midnight():
    tomorrow = cand(run_date=date(2026, 10, 1), status="ok", attempts=1, updated_at=NOW)
    assert due_guilds([tomorrow], utc(2026, 10, 1, 5, 0)) == []  # still Sep 30 in LA
    assert [d.run_date for d in due_guilds([tomorrow], utc(2026, 10, 2, 16, 1))] == [
        date(2026, 10, 2)
    ]


# --- a great many servers at once ---


def test_thousands_of_due_guilds_come_back_ordered_and_fast():
    zones = [LA, "Asia/Kolkata", "Europe/London", "Asia/Kathmandu", "Pacific/Kiritimati"]
    candidates = [
        cand(guild_id=i, tz=zones[i % len(zones)], digest_time=f"{i % 24:02d}:{i % 60:02d}")
        for i in range(1, 5001)
    ]
    # Sprinkle garbage: it must neither raise nor stall the rest.
    candidates[10] = cand(guild_id=11, tz="Nope/Nope")
    candidates[20] = cand(guild_id=21, digest_time="99:99")

    started = time.perf_counter()
    due = due_guilds(candidates, utc(2026, 9, 30, 23, 59))
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0
    assert 11 not in {d.guild_id for d in due} and 21 not in {d.guild_id for d in due}
    keys = [(d.due_at, d.guild_id) for d in due]
    assert keys == sorted(keys)
    assert len(due) == len({d.guild_id for d in due})


def test_equal_due_instants_break_ties_by_guild_id_not_input_order():
    candidates = [cand(guild_id=g) for g in (9, 3, 7, 1)]
    assert [d.guild_id for d in due_guilds(candidates, NOW)] == [1, 3, 7, 9]


def test_due_guilds_accepts_a_generator_and_is_not_confused_by_an_empty_input():
    assert due_guilds((c for c in [cand(guild_id=4)]), NOW)[0].guild_id == 4
    assert due_guilds([], NOW) == []


# --- digest_window ---


def test_window_with_no_previous_digest_is_the_last_24_hours():
    end = utc(2026, 9, 30, 16)
    assert digest_window(end, None) == (end - timedelta(hours=24), end)


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=1), timedelta(days=3)])
def test_a_previous_end_at_or_after_this_end_gives_an_empty_window_at_that_end(offset):
    # Changed from "falls back to 24 hours": that window overlapped the one already posted
    # (a zone change that pulls the next digest earlier repeated its items). Now the window
    # starts at the previous end, so it's empty, and nothing is shown twice.
    end = utc(2026, 9, 30, 16)
    assert digest_window(end, end + offset) == (end + offset, end)


def test_window_chains_onto_the_previous_end_and_floors_at_48_hours():
    end = utc(2026, 9, 30, 16)
    assert digest_window(end, end - timedelta(hours=25)) == (end - timedelta(hours=25), end)
    assert digest_window(end, end - WINDOW_FLOOR) == (end - WINDOW_FLOOR, end)
    assert digest_window(end, end - WINDOW_FLOOR - timedelta(seconds=1)) == (
        end - WINDOW_FLOOR,
        end,
    )
    assert digest_window(end, end - timedelta(days=30)) == (end - WINDOW_FLOOR, end)


def test_window_arithmetic_is_in_instants_across_a_dst_day():
    # LA's 2026-03-08 is 23 hours long: 09:00 PST the day before to 09:00 PDT.
    prev_end = local_due_instant(date(2026, 3, 7), "09:00", LA)
    end = local_due_instant(date(2026, 3, 8), "09:00", LA)
    assert end - prev_end == timedelta(hours=23)
    assert digest_window(end, prev_end) == (prev_end, end)


def test_spring_forward_zone_conversion_round_trips_to_the_shifted_wall_clock():
    instant = local_due_instant(date(2026, 3, 8), "02:30", LA)
    assert instant.astimezone(ZoneInfo(LA)).strftime("%H:%M") == "03:30"


# --- unfinished rows across local midnight ---


def test_a_late_night_digest_that_fails_at_midnight_is_retried_after_midnight():
    failed_at = utc(2026, 10, 1, 6, 59, 30)  # 23:59:30 PDT on Sep 30
    c = cand(
        digest_time="23:59",
        run_date=date(2026, 9, 30),
        status="failed",
        attempts=1,
        updated_at=failed_at,
    )
    due = due_guilds([c], failed_at + RETRY_AFTER + timedelta(minutes=1))  # 00:10 on Oct 1
    assert [(d.run_date, d.reason) for d in due] == [(date(2026, 9, 30), "retry")]


def test_a_late_night_pending_row_is_resumed_after_midnight():
    c = cand(
        digest_time="23:59",
        run_date=date(2026, 9, 30),
        status="pending",
        attempts=1,
        window_end=utc(2026, 10, 1, 6, 59),
        updated_at=utc(2026, 10, 1, 6, 59, 30),
    )
    # 00:10 on Oct 1: ten and a half minutes after the last sign of life, so the lease
    # (LEASE_STALE_AFTER, ten minutes) has just gone stale.
    due = due_guilds([c], utc(2026, 10, 1, 7, 10))
    assert [(d.run_date, d.reason) for d in due] == [(date(2026, 9, 30), "resume")]


def _pending(age, **kw):
    return cand(
        run_date=date(2026, 9, 30),
        status="pending",
        attempts=1,
        window_end=NOW - timedelta(hours=1),
        updated_at=NOW - age,
        **kw,
    )


def test_a_pending_row_is_a_lease_and_resumes_only_once_it_is_stale():
    assert due_guilds([_pending(LEASE_STALE_AFTER)], NOW) == []  # exactly ten minutes: not yet
    [due] = due_guilds([_pending(LEASE_STALE_AFTER + timedelta(seconds=1))], NOW)
    assert (due.reason, due.catch_up) == ("resume", True)
    assert due_guilds([_pending(timedelta(seconds=5))], NOW) == []
    assert due_guilds([_pending(-timedelta(minutes=30))], NOW) == []  # a clock stepped back


def test_a_pending_row_with_no_readable_timestamp_counts_as_stale():
    c = cand(run_date=date(2026, 9, 30), status="pending", window_end=NOW, updated_at=None)
    assert [d.reason for d in due_guilds([c], NOW)] == ["resume"]


def test_the_next_day_waits_for_an_unfinished_row_that_is_not_ready_yet():
    # 00:05 on Oct 1, and yesterday's 23:59 row is a retry still inside its gap, or a
    # live lease. Starting today's digest now would chain its window onto a digest
    # that hasn't finished.
    now = utc(2026, 10, 1, 7, 5)
    failed = cand(
        digest_time="00:00",
        run_date=date(2026, 9, 30),
        status="failed",
        attempts=1,
        updated_at=now - timedelta(minutes=2),
    )
    live = cand(
        digest_time="00:00",
        run_date=date(2026, 9, 30),
        status="pending",
        attempts=1,
        window_end=now,
        updated_at=now - timedelta(minutes=2),
    )
    assert due_guilds([failed], now) == [] and due_guilds([live], now) == []
    later = now + timedelta(minutes=9)
    assert [(d.run_date, d.reason) for d in due_guilds([failed], later)] == [
        (date(2026, 9, 30), "retry")
    ]


def test_the_next_day_starts_once_nothing_is_left_to_retry_or_resume():
    now = utc(2026, 10, 1, 7, 5)
    for status, extra in [
        ("failed", {"attempts": MAX_ATTEMPTS}),  # out of tries
        ("failed", {"attempts": 1, "posted_any": True}),  # an admin's call
        ("pending", {"attempts": 1, "window_end": None}),  # a v2.2 row nobody can resume
        ("ok", {"attempts": 1}),
    ]:
        c = cand(
            digest_time="00:00",
            run_date=date(2026, 9, 30),
            status=status,
            updated_at=now - timedelta(hours=1),
            **extra,
        )
        assert [(d.run_date, d.reason) for d in due_guilds([c], now)] == [
            (date(2026, 10, 1), "first")
        ], (status, extra)


def test_a_retry_or_resume_of_an_earlier_day_uses_that_days_due_instant():
    c = cand(
        run_date=date(2026, 9, 29),
        status="failed",
        attempts=1,
        updated_at=NOW - timedelta(hours=5),
    )
    [due] = due_guilds([c], NOW)
    assert (due.run_date, due.reason) == (date(2026, 9, 29), "retry")
    assert due.due_at == local_due_instant(date(2026, 9, 29), "09:00", LA)
