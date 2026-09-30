"""The per-guild due check and window arithmetic (design.md §15, plan task 6).

Pure functions, so no database and no clock: every test hands `due_guilds` a
candidate and an instant. The DST cases are the ones that earn the file its
keep; they're written in UTC instants on purpose, so a wrong answer can't hide
behind a wrong local conversion in the test itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot.guilds.schedule import (
    CATCH_UP_AFTER,
    DueGuild,
    digest_window,
    due_guilds,
    local_due_instant,
)
from newsbot.store.models import DueCandidate

LA = "America/Los_Angeles"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def cand(guild_id=1, time="09:00", tz=LA, **latest) -> DueCandidate:
    return DueCandidate(guild_id=guild_id, digest_time=time, timezone=tz, tier="free", **latest)


def ids(due: list[DueGuild]) -> list[int]:
    return [d.guild_id for d in due]


# --- local_due_instant ---


def test_local_due_instant_ordinary_day():
    assert local_due_instant(date(2026, 9, 30), "09:00", LA) == utc(2026, 9, 30, 16, 0)
    assert local_due_instant(date(2026, 12, 1), "09:00", LA) == utc(2026, 12, 1, 17, 0)


def test_spring_forward_gap_resolves_to_same_offset_into_the_gap():
    # 2026-03-08: 02:00 PST jumps to 03:00 PDT, so 02:30 doesn't exist.
    assert local_due_instant(date(2026, 3, 8), "02:30", LA) == utc(2026, 3, 8, 10, 30)
    # 10:30Z is 03:30 PDT.
    local = local_due_instant(date(2026, 3, 8), "02:30", LA).astimezone(ZoneInfo(LA))
    assert (local.hour, local.minute) == (3, 30)


def test_fall_back_ambiguous_time_is_the_first_occurrence():
    # 2026-11-01: 02:00 PDT falls back to 01:00 PST, so 01:30 happens twice.
    assert local_due_instant(date(2026, 11, 1), "01:30", LA) == utc(2026, 11, 1, 8, 30)


def test_southern_hemisphere_gap_and_overlap():
    syd = "Australia/Sydney"
    # 2026-10-04: 02:00 AEST becomes 03:00 AEDT; 02:30 lands at 03:30 AEDT (16:30Z the day before).
    assert local_due_instant(date(2026, 10, 4), "02:30", syd) == utc(2026, 10, 3, 16, 30)
    # 2027-04-04: 03:00 AEDT falls back to 02:00 AEST; 02:30 is the first (AEDT) one.
    assert local_due_instant(date(2027, 4, 4), "02:30", syd) == utc(2027, 4, 3, 15, 30)


@pytest.mark.parametrize("bad", ["29:99", "ab:cd", ""])
def test_a_malformed_time_raises_value_error(bad):
    with pytest.raises(ValueError):
        local_due_instant(date(2026, 9, 30), bad, LA)


# --- due_guilds: the basic rule ---


def test_not_due_before_the_local_time_and_due_at_it():
    c = cand()
    assert due_guilds([c], utc(2026, 9, 30, 15, 59)) == []
    [due] = due_guilds([c], utc(2026, 9, 30, 16, 0))
    assert (due.guild_id, due.run_date, due.reason, due.catch_up) == (
        1,
        date(2026, 9, 30),
        "first",
        False,
    )
    assert due.due_at == utc(2026, 9, 30, 16, 0)


def test_a_finished_row_for_the_local_date_is_not_due():
    for status in ("ok", "partial"):
        c = cand(run_date=date(2026, 9, 30), status=status, posted_any=True)
        assert due_guilds([c], utc(2026, 9, 30, 20, 0)) == []


def test_yesterdays_row_does_not_block_today():
    c = cand(run_date=date(2026, 9, 29), status="ok", posted_any=True)
    assert ids(due_guilds([c], utc(2026, 9, 30, 16, 1))) == [1]


def test_a_row_dated_after_the_local_date_is_not_due():
    c = cand(run_date=date(2026, 10, 1), status="ok", posted_any=True)
    assert due_guilds([c], utc(2026, 9, 30, 20, 0)) == []


def test_utc_midnight_crossing_uses_the_local_date():
    # 17:00 Pacific on 2026-09-30 is 00:00Z on 2026-10-01; the local day is still the 30th.
    c = cand(time="17:00", run_date=date(2026, 9, 30), status="ok", posted_any=True)
    assert due_guilds([c], utc(2026, 10, 1, 0, 0)) == []
    c = cand(time="17:00", run_date=date(2026, 9, 29), status="ok", posted_any=True)
    [due] = due_guilds([c], utc(2026, 10, 1, 0, 0))
    assert due.run_date == date(2026, 9, 30)


def test_a_morning_time_for_a_far_east_zone_is_due_on_the_previous_utc_day():
    # 09:00 in Auckland (NZST, UTC+12 in late June) is 21:00Z the day before.
    c = cand(time="09:00", tz="Pacific/Auckland")
    [due] = due_guilds([c], utc(2026, 6, 29, 21, 0))
    assert due.run_date == date(2026, 6, 30)
    assert due_guilds([c], utc(2026, 6, 29, 20, 59)) == []


# --- several guilds at once ---


def test_guilds_in_five_zones_come_due_at_their_own_moments_oldest_first():
    zones = {
        1: "Pacific/Auckland",  # 09:00 NZDT = 20:00Z the day before
        2: "Asia/Kolkata",  # 09:00 = 03:30Z
        3: "Europe/London",  # 09:00 BST = 08:00Z
        4: "America/New_York",  # 09:00 EDT = 13:00Z
        5: "America/Los_Angeles",  # 09:00 PDT = 16:00Z
    }
    candidates = [cand(guild_id=g, tz=tz) for g, tz in zones.items()]
    # At 13:00Z Kolkata, London and New York are due. Auckland is already into
    # its next morning (02:00 on the 1st), and Los Angeles hasn't had its 09:00.
    due = due_guilds(candidates, utc(2026, 9, 30, 13, 0))
    assert ids(due) == [2, 3, 4]  # oldest due instant first
    # By 20:00Z Auckland's 09:00 on October 1st has arrived, and so has LA's;
    # Kolkata has rolled over into October 1st itself (01:30), so it's waiting again.
    due = due_guilds(candidates, utc(2026, 9, 30, 20, 0))
    assert ids(due) == [3, 4, 5, 1]
    assert {d.guild_id: d.run_date for d in due}[1] == date(2026, 10, 1)


def test_two_guilds_in_different_zones_are_due_at_different_moments():
    a, b = cand(guild_id=1, tz=LA), cand(guild_id=2, tz="America/New_York")
    assert ids(due_guilds([a, b], utc(2026, 9, 30, 13, 0))) == [2]
    assert ids(due_guilds([a, b], utc(2026, 9, 30, 16, 0))) == [2, 1]


def test_same_due_instant_breaks_ties_by_guild_id():
    assert ids(due_guilds([cand(guild_id=9), cand(guild_id=3)], utc(2026, 9, 30, 16, 0))) == [3, 9]


def test_a_bad_zone_or_time_skips_that_guild_only():
    bad_zone = cand(guild_id=1, tz="Mars/Olympus_Mons")
    bad_time = cand(guild_id=2, time="99:99")
    good = cand(guild_id=3)
    assert ids(due_guilds([bad_zone, bad_time, good], utc(2026, 9, 30, 16, 0))) == [3]


# --- DST ---


def test_spring_forward_time_in_the_gap_runs_once_at_the_shifted_instant():
    c = cand(time="02:30")
    # 10:29Z is 03:29 PDT: the shifted 03:30 hasn't arrived.
    assert due_guilds([c], utc(2026, 3, 8, 10, 29)) == []
    [due] = due_guilds([c], utc(2026, 3, 8, 10, 30))
    assert due.run_date == date(2026, 3, 8)
    # Once it has a row, every later tick that day is quiet.
    done = cand(time="02:30", run_date=date(2026, 3, 8), status="ok", posted_any=True)
    assert due_guilds([done], utc(2026, 3, 8, 11, 30)) == []


def test_the_day_before_a_gap_still_fires_at_the_ordinary_offset():
    c = cand(time="02:30")
    assert ids(due_guilds([c], utc(2026, 3, 7, 10, 30))) == [1]  # 02:30 PST is 10:30Z


def test_fall_back_time_runs_at_the_first_occurrence_and_not_the_second():
    c = cand(time="01:30")
    assert due_guilds([c], utc(2026, 11, 1, 8, 29)) == []
    [due] = due_guilds([c], utc(2026, 11, 1, 8, 30))  # first 01:30, PDT
    assert due.run_date == date(2026, 11, 1)
    # The second 01:30 (PST) is 09:30Z. The row exists by then, so nothing is due.
    done = cand(time="01:30", run_date=date(2026, 11, 1), status="ok", posted_any=True)
    assert due_guilds([done], utc(2026, 11, 1, 9, 30)) == []
    assert due_guilds([done], utc(2026, 11, 1, 9, 31)) == []


def test_fall_back_time_only_seen_during_the_second_hour_still_runs_once():
    # A bot that was down for the first 01:30 comes back during the repeated hour.
    c = cand(time="01:30")
    [due] = due_guilds([c], utc(2026, 11, 1, 9, 40))
    assert due.catch_up is True


def test_southern_hemisphere_dst_transitions():
    syd = "Australia/Sydney"
    c = cand(time="02:30", tz=syd)
    # 2026-10-04 spring forward: the shifted instant is 16:30Z on the 3rd.
    assert due_guilds([c], utc(2026, 10, 3, 16, 29)) == []
    [due] = due_guilds([c], utc(2026, 10, 3, 16, 30))
    assert due.run_date == date(2026, 10, 4)
    # 2027-04-04 fall back: first 02:30 is 15:30Z on the 3rd; the second, 16:30Z, is quiet.
    [due] = due_guilds([c], utc(2027, 4, 3, 15, 30))
    assert due.run_date == date(2027, 4, 4)
    done = cand(time="02:30", tz=syd, run_date=date(2027, 4, 4), status="ok", posted_any=True)
    assert due_guilds([done], utc(2027, 4, 3, 16, 30)) == []


# --- catch-up ---


def test_a_three_hour_outage_makes_the_guild_due_once_the_bot_is_back():
    c = cand()
    assert due_guilds([c], utc(2026, 9, 30, 15, 0)) == []  # before the time
    # Bot was down 15:30Z to 19:00Z (the 09:00 PDT digest fell inside it).
    [due] = due_guilds([c], utc(2026, 9, 30, 19, 0))
    assert due.catch_up is True and due.reason == "first"
    assert due.due_at == utc(2026, 9, 30, 16, 0)  # the window ends at the due instant, not "now"


def test_a_tick_a_minute_late_is_not_catch_up():
    [due] = due_guilds([cand()], utc(2026, 9, 30, 16, 1))
    assert due.catch_up is False
    [due] = due_guilds([cand()], utc(2026, 9, 30, 16, 0) + CATCH_UP_AFTER)
    assert due.catch_up is True


# --- changing the time or the zone mid-day ---


def test_moving_the_time_later_before_it_passes_defers_the_digest():
    # Was 09:00; at 08:30 PDT the admin moved it to 10:00.
    c = cand(time="10:00")
    assert due_guilds([c], utc(2026, 9, 30, 16, 0)) == []
    assert ids(due_guilds([c], utc(2026, 9, 30, 17, 0))) == [1]


def test_moving_the_time_earlier_after_todays_digest_posted_does_not_repost():
    # 09:00 already posted; at 10:00 the admin moved it to 08:00.
    c = cand(time="08:00", run_date=date(2026, 9, 30), status="ok", posted_any=True)
    assert due_guilds([c], utc(2026, 9, 30, 17, 0)) == []
    # ...but tomorrow it runs at the new time.
    tomorrow = cand(time="08:00", run_date=date(2026, 9, 30), status="ok", posted_any=True)
    assert ids(due_guilds([tomorrow], utc(2026, 10, 1, 15, 0))) == [1]


def test_moving_the_time_to_an_already_passed_moment_with_nothing_posted_is_due_now():
    # The documented rule: late, not cancelled.
    c = cand(time="08:00")
    assert ids(due_guilds([c], utc(2026, 9, 30, 17, 0))) == [1]


def test_a_zone_change_westward_is_not_due_until_the_local_date_catches_up():
    # Posted for 2026-10-01 (Tokyo's date); now the guild is Pacific and it's still the 30th.
    c = cand(run_date=date(2026, 10, 1), status="ok", posted_any=True)
    assert due_guilds([c], utc(2026, 9, 30, 20, 0)) == []
    assert ids(due_guilds([c], utc(2026, 10, 2, 16, 0))) == [1]


def test_a_zone_change_eastward_starts_a_new_local_day():
    # Posted 09:00 PDT on the 30th; moved to Tokyo, where it's now the 1st and 09:00 has passed.
    c = cand(tz="Asia/Tokyo", run_date=date(2026, 9, 30), status="ok", posted_any=True)
    [due] = due_guilds([c], utc(2026, 10, 1, 0, 30))  # 09:30 JST on the 1st
    assert due.run_date == date(2026, 10, 1)


# --- failed and pending rows ---

NOW = utc(2026, 9, 30, 18, 0)
TODAY = date(2026, 9, 30)


def test_a_clean_failure_retries_after_ten_minutes_up_to_three_attempts():
    base = dict(run_date=TODAY, status="failed", posted_any=False)
    assert due_guilds([cand(**base, attempts=1, updated_at=NOW - timedelta(minutes=9))], NOW) == []
    [due] = due_guilds([cand(**base, attempts=1, updated_at=NOW - timedelta(minutes=11))], NOW)
    assert (due.reason, due.catch_up) == ("retry", True)
    assert due_guilds([cand(**base, attempts=2, updated_at=NOW - timedelta(hours=1))], NOW) != []
    assert due_guilds([cand(**base, attempts=3, updated_at=NOW - timedelta(hours=1))], NOW) == []


def test_a_failure_that_posted_something_never_retries_on_its_own():
    c = cand(
        run_date=TODAY,
        status="failed",
        posted_any=True,
        attempts=1,
        updated_at=NOW - timedelta(hours=2),
    )
    assert due_guilds([c], NOW) == []


def test_a_pending_row_from_a_dead_process_resumes():
    c = cand(
        run_date=TODAY,
        status="pending",
        attempts=1,
        updated_at=NOW - timedelta(minutes=30),
        window_end=utc(2026, 9, 30, 16, 0),
    )
    [due] = due_guilds([c], NOW)
    assert due.reason == "resume" and due.catch_up is True


def test_a_pending_row_this_process_is_running_is_left_alone():
    c = cand(run_date=TODAY, status="pending", attempts=1, window_end=utc(2026, 9, 30, 16, 0))
    assert due_guilds([c], NOW, running={1}) == []
    assert ids(due_guilds([c], NOW, running={2})) == [1]


def test_a_pending_row_with_no_window_came_from_v22_and_waits_for_an_admin():
    c = cand(run_date=TODAY, status="pending", attempts=0, window_end=None)
    assert due_guilds([c], NOW) == []


# --- digest_window ---


def test_first_digest_covers_twenty_four_hours():
    end = utc(2026, 9, 30, 16, 0)
    assert digest_window(end, None) == (end - timedelta(hours=24), end)


def test_windows_chain_with_no_gap_and_no_overlap():
    run_now = utc(2026, 9, 30, 15, 0)  # run-now at 08:00 PDT
    tomorrow = utc(2026, 10, 1, 16, 0)
    assert digest_window(tomorrow, run_now) == (run_now, tomorrow)


def test_dst_days_are_23_and_25_hours_long_in_instants():
    mar7 = utc(2026, 3, 7, 17, 0)  # 09:00 PST
    mar8 = utc(2026, 3, 8, 16, 0)  # 09:00 PDT
    start, end = digest_window(mar8, mar7)
    assert end - start == timedelta(hours=23)
    oct31 = utc(2026, 10, 31, 16, 0)  # 09:00 PDT
    nov1 = utc(2026, 11, 1, 17, 0)  # 09:00 PST, after the fall back
    start, end = digest_window(nov1, oct31)
    assert end - start == timedelta(hours=25)


def test_the_window_is_floored_at_forty_eight_hours():
    end = utc(2026, 9, 30, 16, 0)
    assert digest_window(end, end - timedelta(days=9)) == (end - timedelta(hours=48), end)


def test_a_previous_end_that_is_not_before_this_one_falls_back_to_a_day():
    end = utc(2026, 9, 30, 16, 0)
    assert digest_window(end, end) == (end - timedelta(hours=24), end)
    assert digest_window(end, end + timedelta(hours=3)) == (end - timedelta(hours=24), end)
