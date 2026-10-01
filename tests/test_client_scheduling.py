"""Tests for the pure decision functions behind newsbot.bot.client and newsbot.healthcheck.

Per plan section 5, the bot layer is tested only at this level: no gateway
mocking, no fake `discord.Client`. `_should_alert_two_instances`,
`local_run_date`, and the healthcheck's staleness check are all plain functions
that don't need a running bot to exercise.

The v2 pieces that used to be here are gone with the daily job: `should_catch_up`
(startup catch-up) and the CronTrigger DST checks for the 09:00 digest. Every one of
those assertions has a per-server equivalent in `test_schedule.py` and
`test_schedule_adversarial.py`, where the due check replaced both: before the time is
not due, at the time is due, a finished row is not due again, a clean failure retries
only after ten minutes and three attempts, a failure that posted something never retries
alone, a v2.2 `pending` row waits for an admin, and the spring-forward gap, the
fall-back repeat and a restart just after local midnight all give the right answer.
(The quote's own cron still has its DST checks in `test_lounge_client_adversarial.py`.)
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from newsbot.bot.client import _should_alert_two_instances
from newsbot.healthcheck import is_healthy
from newsbot.pipeline.run import local_run_date

# --- _should_alert_two_instances (QA step 20, group 6i) ---


def test_should_alert_two_instances_first_time_no_prior_alert():
    assert _should_alert_two_instances(None, datetime(2026, 9, 23, tzinfo=UTC), timedelta(hours=1))


def test_should_alert_two_instances_true_after_cooldown_elapses():
    last = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    now = datetime(2026, 9, 23, 10, 1, tzinfo=UTC)
    assert _should_alert_two_instances(last, now, timedelta(hours=1)) is True


def test_should_alert_two_instances_false_within_cooldown():
    last = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    now = datetime(2026, 9, 23, 9, 30, tzinfo=UTC)
    assert _should_alert_two_instances(last, now, timedelta(hours=1)) is False


def test_should_alert_two_instances_false_exactly_at_cooldown_boundary():
    # >=, so the boundary itself counts as "cooled down".
    last = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    assert _should_alert_two_instances(last, now, timedelta(hours=1)) is True


# --- local_run_date ---


def test_local_run_date_matches_utc_date_at_midday():
    now = datetime(2026, 9, 23, 20, 0, tzinfo=ZoneInfo("UTC"))  # 13:00 PDT
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_differs_from_utc_date_near_midnight_utc():
    # 02:00 UTC on the 24th is still 19:00 PDT on the 23rd: one calendar
    # day earlier locally than UTC's date.
    now = datetime(2026, 9, 24, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_at_utc_midnight_is_still_previous_day_in_la():
    # UTC midnight isn't local midnight in a UTC-7 zone: it's 17:00 the
    # evening before, so local_run_date should land on UTC's *previous*
    # calendar date, not the one UTC just rolled into.
    now = datetime(2026, 9, 24, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_one_minute_before_la_local_midnight():
    # 06:59 UTC on the 24th is 23:59 PDT on the 23rd: one minute shy of
    # the local day actually turning over.
    now = datetime(2026, 9, 24, 6, 59, tzinfo=ZoneInfo("UTC"))
    assert local_run_date(now, "America/Los_Angeles") == date(2026, 9, 23)


def test_local_run_date_exactly_at_la_local_midnight_rolls_over():
    # 07:00 UTC on the 24th is exactly 00:00 PDT on the 24th: the local
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
    # stale: "less than" doesn't include "equal to".
    path = tmp_path / "heartbeat"
    path.write_text("1")
    assert is_healthy(path, now=path.stat().st_mtime + 180) is False


def test_healthcheck_ignores_file_contents_garbage_is_still_healthy(tmp_path):
    # is_healthy never reads the file, only its mtime. Garbage bytes
    # (the heartbeat job only ever writes a timestamp string, but nothing
    # stops a stray `echo` from clobbering it) shouldn't matter.
    path = tmp_path / "heartbeat"
    path.write_bytes(b"\x00\xffnot a timestamp at all\xfe")
    assert is_healthy(path, now=path.stat().st_mtime + 10) is True


def test_healthcheck_unreadable_file_contents_still_healthy_via_mtime(tmp_path):
    # Even a file this process can't read the *contents* of is fine, as
    # long as stat() can still see it: is_healthy only ever calls
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
