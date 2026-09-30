"""The welcome decision: who gets one, and who's already had theirs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from newsbot.lounge.welcome import RecentWelcomes, welcome_action

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("pending", [True, False])
def test_bot_is_always_ignored(pending):
    assert welcome_action(in_guild=True, is_bot=True, pending=pending) == "ignore"


@pytest.mark.parametrize("pending", [True, False])
def test_other_guild_is_ignored(pending):
    assert welcome_action(in_guild=False, is_bot=False, pending=pending) == "ignore"


def test_pending_waits():
    assert welcome_action(in_guild=True, is_bot=False, pending=True) == "wait"


def test_not_pending_welcomes():
    assert welcome_action(in_guild=True, is_bot=False, pending=False) == "welcome"


def test_first_call_records_and_second_is_refused():
    recent = RecentWelcomes()
    assert recent.check_and_record(1, T0) is True
    assert recent.check_and_record(1, T0 + timedelta(hours=23, minutes=59)) is False


def test_exactly_the_window_later_is_allowed():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0 + timedelta(hours=24)) is True


def test_allowed_call_restarts_the_window():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    recent.check_and_record(1, T0 + timedelta(hours=24))
    assert recent.check_and_record(1, T0 + timedelta(hours=30)) is False


def test_users_are_independent():
    recent = RecentWelcomes()
    assert recent.check_and_record(1, T0) is True
    assert recent.check_and_record(2, T0) is True
    assert recent.check_and_record(1, T0) is False


def test_window_is_overridable():
    recent = RecentWelcomes(window=timedelta(minutes=5))
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0 + timedelta(minutes=4)) is False
    assert recent.check_and_record(1, T0 + timedelta(minutes=5)) is True


def test_pruning_keeps_the_map_bounded():
    recent = RecentWelcomes()
    for user_id in range(10_000):
        recent.check_and_record(user_id, T0)
    recent.check_and_record(99_999, T0 + timedelta(hours=25))
    assert len(recent._seen) == 1


def test_clock_going_backwards_does_not_crash_or_double_welcome():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0 - timedelta(hours=1)) is False
    # The refused call must not have moved the record backwards.
    assert recent.check_and_record(1, T0 + timedelta(hours=23)) is False
    assert recent.check_and_record(1, T0 + timedelta(hours=24)) is True
