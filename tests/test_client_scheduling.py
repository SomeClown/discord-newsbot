"""Tests for the pure decision functions behind newsbot.bot.client and newsbot.healthcheck.

Per plan section 5, the bot layer is tested only at this level: no gateway
mocking, no fake `discord.Client`. `should_catch_up`, the CronTrigger's
DST behavior, `local_run_date`, and the healthcheck's staleness check are
all plain functions (or, for CronTrigger, a library object we can query
directly) that don't need a running bot to exercise.
"""

from __future__ import annotations

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
