"""Tests for the pure decision functions behind newsbot.bot.client and newsbot.healthcheck.

Per plan section 5, the bot layer is tested only at this level: no gateway
mocking, no fake `discord.Client`. `should_catch_up`, the CronTrigger's
DST behavior, `local_run_date`, and the healthcheck's staleness check are
all plain functions (or, for CronTrigger, a library object we can query
directly) that don't need a running bot to exercise.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from newsbot.bot.client import should_catch_up
from newsbot.healthcheck import is_healthy
from newsbot.pipeline.run import local_run_date
from newsbot.store.models import DigestRow

_TZ = ZoneInfo("America/Los_Angeles")
_DIGEST_TIME = time(9, 0)


def _digest_row(status: str) -> DigestRow:
    return DigestRow(
        id=1, run_date=date(2026, 9, 23), status=status, posted_message_ids=[], error_notes=None
    )


# --- should_catch_up ---


def test_should_catch_up_before_scheduled_time_is_false():
    now_local = datetime(2026, 9, 23, 8, 59, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is False


def test_should_catch_up_after_time_no_row_is_true():
    now_local = datetime(2026, 9, 23, 9, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is True


def test_should_catch_up_after_time_ok_row_is_false():
    now_local = datetime(2026, 9, 23, 9, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, _digest_row("ok")) is False


def test_should_catch_up_after_time_partial_row_is_false():
    now_local = datetime(2026, 9, 23, 9, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, _digest_row("partial")) is False


def test_should_catch_up_after_time_failed_row_is_true():
    now_local = datetime(2026, 9, 23, 9, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, _digest_row("failed")) is True


def test_should_catch_up_after_time_pending_row_is_false():
    # A pending row means we don't know whether it already posted (the
    # process may have crashed between publish and save) -- the caller is
    # responsible for alerting an admin in this case, not for guessing.
    now_local = datetime(2026, 9, 23, 9, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, _digest_row("pending")) is False


def test_should_catch_up_exactly_at_scheduled_time_is_true():
    now_local = datetime(2026, 9, 23, 9, 0, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is True


def test_should_catch_up_restart_just_after_local_midnight_is_false():
    # A restart at 00:01 local is a new local day that hasn't reached its
    # digest time yet -- there's no digest row for *today* to be confused
    # by, and 00:01 < 09:00 regardless.
    now_local = datetime(2026, 9, 24, 0, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is False


def test_should_catch_up_on_spring_forward_day_before_digest_time():
    # 2026-03-08 is the US spring-forward Sunday (2 a.m. -> 3 a.m.) in
    # America/Los_Angeles. should_catch_up only ever sees an already
    # resolved local wall-clock time, so the missing hour shouldn't be
    # able to confuse a plain time-of-day comparison.
    now_local = datetime(2026, 3, 8, 8, 59, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is False


def test_should_catch_up_on_spring_forward_day_after_digest_time():
    now_local = datetime(2026, 3, 8, 9, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is True


def test_should_catch_up_on_fall_back_day_before_digest_time():
    # 2026-11-01 is the US fall-back Sunday (2 a.m. -> 1 a.m., so 1 a.m.
    # happens twice). Nothing here is anywhere near that hour, but it's
    # worth pinning that a fall-back day is otherwise an unremarkable day
    # as far as this function is concerned.
    now_local = datetime(2026, 11, 1, 8, 59, tzinfo=_TZ)
    assert should_catch_up(now_local, _DIGEST_TIME, None) is False


def test_should_catch_up_digest_time_inside_dst_skipped_hour_before_the_jump():
    # A digest configured for 02:30 local names a wall-clock moment that
    # literally never happens on spring-forward day. should_catch_up
    # doesn't know or care -- it's still just comparing two time-of-day
    # values, so "real local clock hasn't reached 02:30 yet" still reads
    # as False even though the clock is about to skip past it entirely.
    digest_time = time(2, 30)
    now_local = datetime(2026, 3, 8, 1, 59, tzinfo=_TZ)
    assert should_catch_up(now_local, digest_time, None) is False


def test_should_catch_up_digest_time_inside_dst_skipped_hour_after_the_jump():
    # Local clocks jump straight from 01:59:59 to 03:00:00. The first real
    # moment after that jump (03:01) is later in wall-clock time than the
    # nominal-but-nonexistent 02:30 digest time, so catch-up still fires.
    digest_time = time(2, 30)
    now_local = datetime(2026, 3, 8, 3, 1, tzinfo=_TZ)
    assert should_catch_up(now_local, digest_time, None) is True


def test_should_catch_up_ambiguous_fall_back_time_agrees_on_both_occurrences():
    # 01:30 local happens twice on fall-back day (once at UTC-7, once at
    # UTC-8). should_catch_up only looks at .time(), so both occurrences
    # of "01:30 local" should get the same answer against a 09:00 digest
    # time -- the ambiguity is real, but it doesn't matter here.
    digest_time = _DIGEST_TIME
    first_occurrence = datetime(2026, 11, 1, 1, 30, tzinfo=_TZ, fold=0)
    second_occurrence = datetime(2026, 11, 1, 1, 30, tzinfo=_TZ, fold=1)
    assert should_catch_up(first_occurrence, digest_time, None) is False
    assert should_catch_up(second_occurrence, digest_time, None) is False


# --- CronTrigger DST behavior ---


def test_cron_trigger_next_fire_lands_on_local_9am_around_spring_forward():
    # 2026-03-08 is the US spring-forward Sunday (2 a.m. -> 3 a.m.) in
    # America/Los_Angeles.
    trigger = CronTrigger(hour=9, minute=0, timezone=_TZ)
    now = datetime(2026, 3, 7, 12, 0, tzinfo=_TZ)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire.astimezone(_TZ).date() == date(2026, 3, 8)
    assert next_fire.astimezone(_TZ).time() == time(9, 0)


def test_cron_trigger_next_fire_lands_on_local_9am_around_fall_back():
    # 2026-11-01 is the US fall-back Sunday (2 a.m. -> 1 a.m.).
    trigger = CronTrigger(hour=9, minute=0, timezone=_TZ)
    now = datetime(2026, 10, 31, 12, 0, tzinfo=_TZ)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire.astimezone(_TZ).date() == date(2026, 11, 1)
    assert next_fire.astimezone(_TZ).time() == time(9, 0)


def test_cron_trigger_next_fire_same_day_before_scheduled_time():
    trigger = CronTrigger(hour=9, minute=0, timezone=_TZ)
    now = datetime(2026, 9, 23, 2, 0, tzinfo=_TZ)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire.astimezone(_TZ).date() == date(2026, 9, 23)


# --- local_run_date ---


def test_local_run_date_matches_utc_date_at_midday():
    now = datetime(2026, 9, 23, 20, 0, tzinfo=ZoneInfo("UTC"))  # 13:00 PDT
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_differs_from_utc_date_near_midnight_utc():
    # 02:00 UTC on the 24th is still 19:00 PDT on the 23rd -- one calendar
    # day earlier locally than UTC's date.
    now = datetime(2026, 9, 24, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_at_utc_midnight_is_still_previous_day_in_la():
    # UTC midnight isn't local midnight in a UTC-7 zone -- it's 17:00 the
    # evening before, so local_run_date should land on UTC's *previous*
    # calendar date, not the one UTC just rolled into.
    now = datetime(2026, 9, 24, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_one_minute_before_la_local_midnight():
    # 06:59 UTC on the 24th is 23:59 PDT on the 23rd -- one minute shy of
    # the local day actually turning over.
    now = datetime(2026, 9, 24, 6, 59, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_exactly_at_la_local_midnight_rolls_over():
    # 07:00 UTC on the 24th is exactly 00:00 PDT on the 24th -- the local
    # date should flip right here, not a minute early or late.
    now = datetime(2026, 9, 24, 7, 0, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 24)


# --- healthcheck staleness ---


def test_healthcheck_fresh_file_is_healthy(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("1")
    assert is_healthy(path, now=path.stat().st_mtime + 10) is True


def test_healthcheck_stale_file_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("1")
    assert is_healthy(path, now=path.stat().st_mtime + 181) is False


def test_healthcheck_missing_file_is_unhealthy(tmp_path):
    assert is_healthy(tmp_path / "does-not-exist", now=0) is False


def test_healthcheck_boundary_just_under_limit_is_healthy(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("1")
    assert is_healthy(path, now=path.stat().st_mtime + 179) is True


def test_healthcheck_boundary_exactly_at_limit_is_unhealthy(tmp_path):
    # The check is a strict `<`, so exactly 180s old already counts as
    # stale -- "less than" doesn't include "equal to".
    path = tmp_path / "heartbeat"
    path.write_text("1")
    assert is_healthy(path, now=path.stat().st_mtime + 180) is False


def test_healthcheck_ignores_file_contents_garbage_is_still_healthy(tmp_path):
    # is_healthy never reads the file -- only its mtime. Garbage bytes
    # (the heartbeat job only ever writes a timestamp string, but nothing
    # stops a stray `echo` from clobbering it) shouldn't matter.
    path = tmp_path / "heartbeat"
    path.write_bytes(b"\x00\xffnot a timestamp at all\xfe")
    assert is_healthy(path, now=path.stat().st_mtime + 10) is True


def test_healthcheck_unreadable_file_contents_still_healthy_via_mtime(tmp_path):
    # Even a file this process can't read the *contents* of is fine, as
    # long as stat() can still see it -- is_healthy only ever calls
    # stat(), never open(). (Running as root defeats chmod-based
    # permission tests, so this only asserts what should hold regardless
    # of who's running it.)
    path = tmp_path / "heartbeat"
    path.write_text("1")
    path.chmod(0o000)
    try:
        assert is_healthy(path, now=path.stat().st_mtime + 10) is True
    finally:
        path.chmod(0o644)


def test_healthcheck_future_mtime_is_healthy(tmp_path):
    # A clock skew or a heartbeat write that lands slightly ahead of
    # `now` shouldn't read as stale: `current - mtime` goes negative,
    # which is still less than the staleness threshold.
    path = tmp_path / "heartbeat"
    path.write_text("1")
    future_mtime = path.stat().st_mtime + 3600
    os.utime(path, (future_mtime, future_mtime))
    assert is_healthy(path, now=future_mtime - 5) is True


def test_healthcheck_broken_symlink_is_unhealthy(tmp_path):
    # A heartbeat path that exists as a directory entry but whose target
    # is gone (container restart raced a bind-mount, say) should fail
    # stat() with an OSError, same as a missing file.
    target = tmp_path / "gone"
    target.write_text("1")
    link = tmp_path / "heartbeat-link"
    link.symlink_to(target)
    target.unlink()
    assert is_healthy(link, now=0) is False
