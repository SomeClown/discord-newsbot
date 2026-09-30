"""Adversarial tests for the welcome decision, beyond `test_lounge_welcome.py`.

The angle (test-engineer brief, 2026-09-29): `welcome_action` and
`RecentWelcomes` are the bouncer and his clipboard, and both have to survive
a night of odd guests. Odd here means: every boolean combination, values that
are truthy without being `True`, clocks in the wrong zone or the wrong decade,
IDs at the edges of what a snowflake can be, and a guest list ten thousand
deep. Everything is synthetic.

Where the code does something an owner might not expect (mixing naive and
aware datetimes raises; a zero window never remembers anyone), the test says
so and pins today's behavior, so a change is a decision and not a surprise.
The privacy tests are static: the map holds ints and datetimes and the module
never imports Discord.
"""

from __future__ import annotations

import ast
import itertools
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsbot.lounge import welcome as welcome_module
from newsbot.lounge.welcome import RecentWelcomes, welcome_action

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)
US = timedelta(microseconds=1)


# welcome_action


def _expected(in_guild, is_bot, pending):
    if not in_guild or is_bot:
        return "ignore"
    return "wait" if pending else "welcome"


@pytest.mark.parametrize(
    ("in_guild", "is_bot", "pending"), list(itertools.product([True, False], repeat=3))
)
def test_welcome_action_is_total_over_all_eight_combinations(in_guild, is_bot, pending):
    result = welcome_action(in_guild=in_guild, is_bot=is_bot, pending=pending)
    assert result == _expected(in_guild, is_bot, pending)
    assert result in {"ignore", "wait", "welcome"}


def test_welcome_action_only_welcomes_one_of_eight_combinations():
    results = [
        welcome_action(in_guild=g, is_bot=b, pending=p)
        for g, b, p in itertools.product([True, False], repeat=3)
    ]
    assert results.count("welcome") == 1
    assert results.count("wait") == 1
    assert results.count("ignore") == 6


def test_welcome_action_takes_keywords_only():
    with pytest.raises(TypeError):
        welcome_action(True, False, False)  # type: ignore[misc]


def test_welcome_action_none_pending_counts_as_not_pending():
    # discord.py types `pending` as bool, but a stub or a partial member could
    # hand over None. Falsy means welcome. Pinned so it's a known quantity.
    assert welcome_action(in_guild=True, is_bot=False, pending=None) == "welcome"  # type: ignore[arg-type]


def test_welcome_action_truthy_strings_are_taken_at_face_value():
    # No type check: the string "False" is truthy, so it waits. Signature says
    # bool; callers passing anything else get Python truthiness, not a guard.
    assert welcome_action(in_guild=True, is_bot=False, pending="False") == "wait"  # type: ignore[arg-type]
    assert welcome_action(in_guild="", is_bot=False, pending=False) == "ignore"  # type: ignore[arg-type]
    assert welcome_action(in_guild=1, is_bot=0, pending=0) == "welcome"  # type: ignore[arg-type]


# RecentWelcomes, time handling


def test_mixing_naive_and_aware_datetimes_raises_type_error():
    # Python refuses to compare the two. The bot only ever feeds aware UTC, so
    # this is a caller bug worth being loud about; pinned rather than "fixed".
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    with pytest.raises(TypeError):
        recent.check_and_record(2, T0.replace(tzinfo=None))


def test_naive_first_then_aware_also_raises():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0.replace(tzinfo=None))
    with pytest.raises(TypeError):
        recent.check_and_record(1, T0)


def test_all_naive_datetimes_work_consistently():
    recent = RecentWelcomes()
    naive = T0.replace(tzinfo=None)
    assert recent.check_and_record(1, naive) is True
    assert recent.check_and_record(1, naive + timedelta(hours=1)) is False
    assert recent.check_and_record(1, naive + DAY) is True


def test_same_instant_in_different_zones_is_the_same_moment():
    recent = RecentWelcomes()
    tokyo = timezone(timedelta(hours=9))
    la = timezone(timedelta(hours=-8))
    assert recent.check_and_record(1, T0.astimezone(tokyo)) is True
    assert recent.check_and_record(1, T0.astimezone(la)) is False
    # Exactly 24h later, expressed in yet another zone, is allowed again.
    assert recent.check_and_record(1, (T0 + DAY).astimezone(la)) is True


def test_zone_offset_does_not_shift_the_window():
    recent = RecentWelcomes()
    tokyo = timezone(timedelta(hours=9))
    recent.check_and_record(1, T0)
    # 23h59m later by the wall clock in Tokyo is still inside the window.
    assert recent.check_and_record(1, (T0 + DAY - timedelta(minutes=1)).astimezone(tokyo)) is False


def test_one_microsecond_before_the_window_is_refused():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0 + DAY - US) is False


def test_exactly_the_window_is_allowed():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0 + DAY) is True


def test_one_microsecond_after_the_window_is_allowed():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0 + DAY + US) is True


def test_refused_call_does_not_extend_the_window():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    recent.check_and_record(1, T0 + DAY - US)  # refused
    assert recent.check_and_record(1, T0 + DAY) is True


def test_same_instant_twice_is_refused():
    recent = RecentWelcomes()
    assert recent.check_and_record(1, T0) is True
    assert recent.check_and_record(1, T0) is False


# RecentWelcomes, ids


@pytest.mark.parametrize("user_id", [0, 1, 2**63, 2**64 - 1, 2**64, -1, -(2**63)])
def test_extreme_and_negative_ids_behave_like_any_other(user_id):
    recent = RecentWelcomes()
    assert recent.check_and_record(user_id, T0) is True
    assert recent.check_and_record(user_id, T0 + timedelta(hours=1)) is False
    assert recent.check_and_record(user_id, T0 + DAY) is True


def test_zero_id_is_not_confused_with_absent():
    # A `dict.get(uid)` truthiness slip would treat 0 as never seen.
    recent = RecentWelcomes()
    recent.check_and_record(0, T0)
    assert recent.check_and_record(0, T0) is False


def test_neighbouring_extreme_ids_do_not_collide():
    recent = RecentWelcomes()
    assert recent.check_and_record(2**64 - 1, T0) is True
    assert recent.check_and_record(2**64 - 2, T0) is True
    assert recent.check_and_record(0, T0) is True
    assert recent.check_and_record(-1, T0) is True


# RecentWelcomes, windows


def test_zero_window_never_remembers_anyone():
    # Entry at t is pruned when t > now - 0 is false, i.e. always at the same
    # instant. So a zero window means "welcome every time". Pinned; the owner
    # might prefer a constructor error for a window that can't dedupe.
    recent = RecentWelcomes(window=timedelta(0))
    assert recent.check_and_record(1, T0) is True
    assert recent.check_and_record(1, T0) is True
    assert len(recent._seen) == 1


def test_negative_window_never_remembers_anyone():
    recent = RecentWelcomes(window=timedelta(hours=-1))
    assert recent.check_and_record(1, T0) is True
    assert recent.check_and_record(1, T0) is True
    assert recent.check_and_record(1, T0 + timedelta(minutes=30)) is True


def test_tiny_window_boundary_is_one_microsecond():
    recent = RecentWelcomes(window=US)
    recent.check_and_record(1, T0)
    assert recent.check_and_record(1, T0) is False
    assert recent.check_and_record(1, T0 + US) is True


# RecentWelcomes, clocks


def test_clock_jumping_years_forward_forgets_and_back_is_safe():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    future = T0 + timedelta(days=365 * 5)
    assert recent.check_and_record(1, future) is True
    assert len(recent._seen) == 1
    # Now the clock snaps back to the present: the future record is still
    # "inside the window" (negative elapsed), so no double welcome, no crash.
    assert recent.check_and_record(1, T0 + timedelta(hours=1)) is False


def test_clock_jump_back_does_not_resurrect_pruned_entries():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    recent.check_and_record(2, T0 + timedelta(days=365))  # prunes user 1
    # User 1's record was dropped on the jump, so going back welcomes again.
    # That's a restart-shaped hole: fine, and documented here.
    assert recent.check_and_record(1, T0 + timedelta(hours=1)) is True


def test_far_future_entry_is_pruned_once_the_clock_catches_up():
    recent = RecentWelcomes()
    future = T0 + timedelta(days=365)
    recent.check_and_record(1, future)
    recent.check_and_record(2, future + DAY + US)
    assert 1 not in recent._seen


def test_year_9999_does_not_overflow():
    recent = RecentWelcomes()
    late = datetime(9999, 12, 31, tzinfo=UTC)
    assert recent.check_and_record(1, late) is True
    assert recent.check_and_record(1, late) is False


def test_earliest_datetime_overflows_on_subtract():
    # now - window at an aware datetime.min underflows. Nothing real gets
    # near year 1; pinned as an OverflowError rather than a silent wrap.
    recent = RecentWelcomes()
    with pytest.raises(OverflowError):
        recent.check_and_record(1, datetime.min.replace(tzinfo=UTC))


# RecentWelcomes, independence and scale


def test_one_users_calls_never_affect_another_users_expiry():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    recent.check_and_record(2, T0 + timedelta(hours=12))
    # Hammering user 2 (refused each time) must not extend user 1's window.
    for minutes in range(0, 600, 30):
        recent.check_and_record(2, T0 + timedelta(hours=12, minutes=minutes))
    assert recent.check_and_record(1, T0 + DAY) is True
    # And user 2 is still inside their own window, unaffected by user 1's re-welcome.
    assert recent.check_and_record(2, T0 + DAY) is False
    assert recent.check_and_record(2, T0 + timedelta(hours=36)) is True


def test_interleaved_users_expire_on_their_own_schedules():
    recent = RecentWelcomes()
    for i in range(5):
        recent.check_and_record(i, T0 + timedelta(hours=i))
    at = T0 + DAY + timedelta(hours=2)
    results = [recent.check_and_record(i, at) for i in range(5)]
    # Users 0, 1, 2 are past 24h; 3 and 4 are not.
    assert results == [True, True, True, False, False]


def test_a_refused_call_still_prunes_other_users():
    recent = RecentWelcomes()
    recent.check_and_record(1, T0)
    recent.check_and_record(2, T0 + timedelta(hours=23))
    assert recent.check_and_record(2, T0 + timedelta(hours=25)) is False
    assert 1 not in recent._seen


def test_hundred_thousand_distinct_users_in_one_window_stay_fast():
    recent = RecentWelcomes()
    start = time.perf_counter()
    # Same instant, so nothing expires and the map grows to 100k. Inserted
    # directly: going through check_and_record would rebuild the dict on every
    # call, which is the next test's problem. One real call at the end proves
    # a full-size map still answers.
    for user_id in range(100_000):
        recent._seen[user_id] = T0
    assert recent.check_and_record(100_000, T0) is True
    assert len(recent._seen) == 100_001
    assert time.perf_counter() - start < 5


def test_ten_thousand_calls_with_ten_thousand_live_entries_stay_bounded():
    recent = RecentWelcomes()
    for user_id in range(10_000):
        recent._seen[user_id] = T0
    start = time.perf_counter()
    for user_id in range(10_000):
        # Every call is a refusal, so the map never grows and each call
        # rebuilds all 10k entries: 100M dict operations worst case.
        assert recent.check_and_record(user_id, T0 + timedelta(seconds=1)) is False
    elapsed = time.perf_counter() - start
    assert len(recent._seen) == 10_000
    assert elapsed < 30, f"pruning 10k x 10k took {elapsed:.1f}s"


# Privacy


def test_map_holds_only_ints_and_datetimes():
    recent = RecentWelcomes()
    for user_id in (0, 5, 2**64 - 1):
        recent.check_and_record(user_id, T0)
    assert recent._seen
    for key, value in recent._seen.items():
        assert type(key) is int
        assert type(value) is datetime
    attrs = vars(recent)
    assert set(attrs) == {"_window", "_seen"}
    assert isinstance(attrs["_window"], timedelta)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_welcome_module_imports_nothing_from_discord_or_the_bot():
    imports = _imported_modules(Path(welcome_module.__file__))
    assert imports, "expected to find some imports"
    for name in imports:
        root = name.lstrip(".").split(".")[0]
        assert root != "discord", name
        assert not name.startswith("newsbot.bot"), name
        assert name.lstrip(".") != "bot" and not name.lstrip(".").startswith("bot."), name
    # The allowlist, so a new import is a conscious edit.
    assert {n.split(".")[0] for n in imports} == {
        "__future__",
        "re",
        "datetime",
        "typing",
        "collections",
    }
