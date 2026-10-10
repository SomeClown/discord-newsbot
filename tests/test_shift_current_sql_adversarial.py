"""Adversarial round on `repo.current_codes` and `repo.history_codes` (design.md §16, plan task 9).

`current_codes` is `shift.details.is_current` written out in SQL, and two spellings of one rule
agree right up until they don't. So most of this file is a property test: random worlds, many
seeds, every boundary the rule has (expiry at Pacific midnight on ordinary, 23-hour, 25-hour and
year-end days; the 180-day clock to the microsecond; permanent codes with and without a stray
expiry; no details row; `clock_start` against `first_seen_at`; every status), judged against the
Python rule on the same rows. Then the things SQL can't see (malformed text, a details-only code),
the history window, and a reader running against a writer.

Defects this round found (a malformed date, a naive or zoned clock, a torn read) are fixed and
pinned as ordinary tests. Invisible or non-ASCII characters are written as escapes.
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot.shift.details import CLOCK_DAYS, end_of_day, is_current
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import CodeDetailsRow

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
TODAY = date(2026, 10, 9)
RELEASED = frozenset({"seeded", "too_old", "roundup", "posted"})
STATUSES = ("seeded", "too_old", "roundup", "posted", "pending", "failed")
CATEGORIES = (None, "golden", "cosmetic", "permanent", "other")
ONE_US = timedelta(microseconds=1)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Zones without daylight saving, so spelling an instant in one can't change what "180 days
# earlier" means. (Pacific, which does have it, gets its own test below.)
FIXED_ZONES = [
    UTC,
    ZoneInfo("Asia/Tokyo"),
    ZoneInfo("Pacific/Kiritimati"),
    ZoneInfo("Pacific/Pago_Pago"),
]
ZONES = [*FIXED_ZONES, PT]  # history_codes does no date arithmetic, so any zone will do

# Pacific midnights worth standing on: an ordinary one, both ends of the 23-hour day (spring
# forward, 2026-03-08), both ends of the 25-hour day (fall back, 2026-11-01) and New Year.
ANCHORS = [
    datetime(2026, 10, 10, 7, 0, tzinfo=UTC),
    datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
    datetime(2026, 3, 9, 7, 0, tzinfo=UTC),
    datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
    datetime(2026, 11, 2, 8, 0, tzinfo=UTC),
    datetime(2027, 1, 1, 8, 0, tzinfo=UTC),
    datetime(2028, 2, 29, 8, 0, tzinfo=UTC),
    datetime(2028, 3, 1, 8, 0, tzinfo=UTC),
]
INTERESTING_DATES = [
    date(2026, 3, 8),
    date(2026, 3, 9),
    date(2026, 11, 1),
    date(2026, 11, 2),
    date(2026, 12, 31),
    date(2027, 1, 1),
    date(2028, 2, 29),
    date(2000, 1, 1),
    date(2100, 12, 31),
    date(9999, 12, 31),
]


def _code(n: int) -> str:
    return f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA"


def _us(moment: datetime) -> int:
    return (moment - EPOCH) // ONE_US


def _put_alerted(conn, n: int, seen: datetime | str, status: str = "posted") -> str:
    text = seen if isinstance(seen, str) else seen.astimezone(UTC).isoformat()
    conn.execute(
        "INSERT INTO alerted_codes "
        "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
        "VALUES (?, ?, 'Feed', 'https://example.com/p', ?, ?, 0)",
        (_code(n), text, status, int(status == "roundup")),
    )
    return _code(n)


def _put_details(conn, n: int, clock_start: datetime, **fields) -> None:
    repo._write_details(
        conn, CodeDetailsRow(code=_code(n), clock_start=clock_start, updated_at=NOW, **fields)
    )


class World:
    """Rows as a test wrote them, so the Python rule can be asked about the same data."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, datetime, str]] = []  # code, first_seen, status, in insert order

    def add(self, conn, n: int, seen: datetime, status: str) -> str:
        self.rows.append((_put_alerted(conn, n, seen, status), seen, status))
        return _code(n)


def _random_instant(rng: random.Random, now0: datetime) -> datetime:
    kind = rng.randrange(5)
    if kind == 0:
        moment = (
            now0
            - timedelta(days=CLOCK_DAYS)
            + rng.choice(
                [-ONE_US, timedelta(0), ONE_US, timedelta(seconds=-1), timedelta(seconds=1)]
            )
        )
    elif kind == 1:
        moment = now0 - timedelta(days=CLOCK_DAYS) + timedelta(days=rng.choice([-1, 1]))
    elif kind == 2:
        moment = now0 - timedelta(seconds=rng.uniform(0, 400 * 86400))
    elif kind == 3:
        moment = now0 + timedelta(days=2)  # a clock that ran ahead
    else:
        moment = now0 - timedelta(seconds=rng.uniform(0, 3600))
    if rng.random() < 0.3:
        moment = moment.replace(microsecond=0)  # isoformat() then leaves the fraction off
    return moment


def _random_expiry(rng: random.Random, now0: datetime) -> date | None:
    kind = rng.randrange(4)
    if kind == 0:
        return None
    if kind == 1:
        return now0.astimezone(PT).date() + timedelta(days=rng.randint(-2, 2))
    if kind == 2:
        return rng.choice(INTERESTING_DATES)
    return now0.astimezone(PT).date() + timedelta(days=rng.randint(-400, 400))


def _build_world(conn, rng: random.Random, now0: datetime, size: int = 80) -> World:
    world = World()
    for n in range(size):
        seen = _random_instant(rng, now0)
        status = rng.choice(STATUSES)
        world.add(conn, n, seen, status)
        if rng.random() < 0.65:
            with conn:
                _put_details(
                    conn,
                    n,
                    _random_instant(rng, now0),
                    category=rng.choice(CATEGORIES),
                    expires_on=_random_expiry(rng, now0),
                )
    conn.commit()
    return world


def _boundary_nows(rng: random.Random, conn, world: World, now0: datetime) -> list[datetime]:
    nows = [now0 - ONE_US, now0, now0 + ONE_US, now0 + timedelta(hours=1)]
    details = repo.code_details_for(conn, [code for code, _, _ in world.rows])
    for row in rng.sample(list(details.values()), k=min(4, len(details))):
        if row.expires_on is not None and row.expires_on.year < 9999:
            edge = end_of_day(row.expires_on)
            nows += [edge - ONE_US, edge, edge + ONE_US]
        nows += [row.clock_start + timedelta(days=CLOCK_DAYS) + d for d in (-ONE_US, ONE_US)]
    for _code_text, seen, _status in rng.sample(world.rows, k=4):
        nows += [seen + timedelta(days=CLOCK_DAYS) + d for d in (-ONE_US, timedelta(0), ONE_US)]
    return nows


def _expected_current(conn, world: World, now: datetime) -> list[str]:
    details = repo.code_details_for(conn, [code for code, _, _ in world.rows])
    keep = [
        (index, code, seen)
        for index, (code, seen, status) in enumerate(world.rows)
        if status in RELEASED and is_current(details.get(code), seen, now=now)
    ]
    keep.sort(key=lambda item: (-_us(item[2]), item[0]))
    return [code for _, code, _ in keep]


@pytest.mark.parametrize("seed", range(60))
def test_current_codes_agrees_with_is_current_on_random_worlds(v3_db, seed):
    rng = random.Random(seed)  # noqa: S311
    now0 = rng.choice(ANCHORS) + rng.choice([-ONE_US, timedelta(0), ONE_US])
    if seed % 5 == 4:
        now0 = NOW + timedelta(seconds=rng.uniform(-9e6, 9e6))
    with closing(connect(v3_db)) as conn:
        world = _build_world(conn, rng, now0)
        for now in _boundary_nows(rng, conn, world, now0):
            asked = now.astimezone(rng.choice(FIXED_ZONES))  # the same instant, spelled in any zone
            got = [c.code for c in repo.current_codes(conn, now=asked)]
            assert got == _expected_current(conn, world, now), (seed, now.isoformat())


def test_current_codes_hands_back_what_was_stored(v3_db):
    with closing(connect(v3_db)) as conn:
        seen = NOW - timedelta(days=3, microseconds=17)
        with conn:
            _put_alerted(conn, 1, seen)
            _put_details(
                conn,
                1,
                NOW - timedelta(days=1),
                reward="3 Golden Keys",
                category="golden",
                expires_on=TODAY + timedelta(days=4),
                origin="somewhere",
            )
        [entry] = repo.current_codes(conn, now=NOW)
        stored = repo.code_details_for(conn, [_code(1)])[_code(1)]
    assert entry.code == _code(1)
    assert entry.first_seen_at == seen
    assert entry.details == stored
    assert entry.details.reward == "3 Golden Keys"


# --- named boundaries, so a failure reads as a sentence ---


@pytest.mark.parametrize(
    ("label", "expires_on", "edge_utc"),
    [
        ("an ordinary day (PDT)", date(2026, 10, 9), datetime(2026, 10, 10, 7, 0, tzinfo=UTC)),
        (
            "the 23-hour spring forward day",
            date(2026, 3, 8),
            datetime(2026, 3, 9, 7, 0, tzinfo=UTC),
        ),
        ("the 25-hour fall back day", date(2026, 11, 1), datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),
        ("New Year's Eve", date(2026, 12, 31), datetime(2027, 1, 1, 8, 0, tzinfo=UTC)),
        ("a leap day", date(2028, 2, 29), datetime(2028, 3, 1, 8, 0, tzinfo=UTC)),
        ("the day before a leap day", date(2027, 2, 28), datetime(2027, 3, 1, 8, 0, tzinfo=UTC)),
    ],
)
def test_an_expiry_day_lasts_until_the_next_pacific_midnight(v3_db, label, expires_on, edge_utc):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, edge_utc - timedelta(days=10))
            _put_details(conn, 1, edge_utc - timedelta(days=10), expires_on=expires_on)
        before = repo.current_codes(conn, now=edge_utc - ONE_US)
        at = repo.current_codes(conn, now=edge_utc)
        # Same instants, asked from the zone the expiry is written in.
        before_pt = repo.current_codes(conn, now=(edge_utc - ONE_US).astimezone(PT))
        at_pt = repo.current_codes(conn, now=edge_utc.astimezone(PT))
    assert [c.code for c in before] == [_code(1)], label
    assert at == [], label
    assert [c.code for c in before_pt] == [_code(1)], label
    assert at_pt == [], label


def test_the_far_future_expiry_the_check_constraint_allows_does_not_crash(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
            _put_details(conn, 1, NOW, expires_on=date(9999, 12, 31))
        assert [c.code for c in repo.current_codes(conn, now=NOW)] == [_code(1)]


def test_a_permanent_code_is_current_with_or_without_a_stray_expiry(v3_db):
    long_ago = NOW - timedelta(days=5000)
    with closing(connect(v3_db)) as conn:
        with conn:
            for n, expiry in ((1, None), (2, date(2001, 1, 1)), (3, TODAY - timedelta(days=1))):
                _put_alerted(conn, n, long_ago)
                _put_details(conn, n, long_ago, category="permanent", expires_on=expiry)
            _put_alerted(conn, 4, long_ago)
            _put_details(conn, 4, long_ago, category="other", expires_on=date(2001, 1, 1))
        got = {c.code for c in repo.current_codes(conn, now=NOW)}
    assert got == {_code(1), _code(2), _code(3)}  # the "other" one with the same date is dead


def test_permanent_codes_still_obey_the_status_filter(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            for n, status in enumerate(STATUSES, start=1):
                _put_alerted(conn, n, NOW, status)
                _put_details(conn, n, NOW, category="permanent")
        got = {c.code for c in repo.current_codes(conn, now=NOW)}
    assert got == {_code(n) for n, status in enumerate(STATUSES, start=1) if status in RELEASED}


def test_a_code_without_a_row_runs_the_clock_from_first_seen(v3_db):
    start = NOW - timedelta(days=CLOCK_DAYS)
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, start + ONE_US)
            _put_alerted(conn, 2, start)
            _put_alerted(conn, 3, start.replace(microsecond=0) + timedelta(seconds=1))
        got = {c.code for c in repo.current_codes(conn, now=NOW)}
    assert got == {_code(1)} | ({_code(3)} if start.microsecond == 0 else set())


def test_the_rows_clock_start_beats_first_seen_in_both_directions(v3_db):
    start = NOW - timedelta(days=CLOCK_DAYS)
    with closing(connect(v3_db)) as conn:
        with conn:
            # Seen long ago, clock restarted recently (the rollout rule): current.
            _put_alerted(conn, 1, NOW - timedelta(days=500))
            _put_details(conn, 1, NOW - timedelta(days=2))
            # Seen yesterday, but the row says its clock ran out: gone.
            _put_alerted(conn, 2, NOW - timedelta(days=1))
            _put_details(conn, 2, start)
            # Seen yesterday, clock one microsecond from running out: still here.
            _put_alerted(conn, 3, NOW - timedelta(days=1))
            _put_details(conn, 3, start + ONE_US)
        got = {c.code for c in repo.current_codes(conn, now=NOW)}
    assert got == {_code(1), _code(3)}


def test_a_known_expiry_ignores_the_clock_in_both_directions(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            # Clock long dead, expiry tomorrow: current.
            _put_alerted(conn, 1, NOW - timedelta(days=900))
            _put_details(conn, 1, NOW - timedelta(days=900), expires_on=TODAY + timedelta(days=1))
            # Clock fresh, expiry yesterday: gone.
            _put_alerted(conn, 2, NOW)
            _put_details(conn, 2, NOW, expires_on=TODAY - timedelta(days=1))
        got = {c.code for c in repo.current_codes(conn, now=NOW)}
    assert got == {_code(1)}


@pytest.mark.parametrize("status", STATUSES)
def test_each_status_is_judged_by_the_same_rules(v3_db, status):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW - timedelta(days=1), status)
        got = [c.code for c in repo.current_codes(conn, now=NOW)]
    assert got == ([_code(1)] if status in RELEASED else [])


# --- a code in code_details that was never released ---
#
# Decision (design.md §16): "current" is a property of a *released* code. A table row can name a
# code the bot hasn't released yet; the plan's SQL starts from `alerted_codes` on purpose, and a
# code only joins it by being alerted or quietly recorded in the same pass. A details row with no
# release is a code that detection hasn't accepted (or hasn't finished with), so listing it would
# show members a code the bot itself hasn't vouched for and would walk around the pending and
# failed exclusions. It shows in neither `/shift codes` nor `/shift history`.


def test_a_details_only_code_is_listed_nowhere(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_details(
                conn,
                1,
                NOW,
                reward="9 Golden Keys",
                category="permanent",
                expires_on=TODAY + timedelta(days=30),
            )
            _put_alerted(conn, 2, NOW)
        assert [c.code for c in repo.current_codes(conn, now=NOW)] == [_code(2)]
        assert [v.code for v in repo.history_codes(conn, NOW - timedelta(days=90))] == [_code(2)]


# --- malformed stored text ---
#
# The CHECK on `code_details.expires_on` is a GLOB for the *shape* of a date, so '2026-19-39'
# passes it; `alerted_codes.first_seen_at` and `code_details.clock_start` have no CHECK at all.
# Nothing in the app writes any of these (dates come out of `date.isoformat()`), so this is
# damage by hand or by disk. The question is what one bad row does to everyone: it must not
# raise out of the repo call and answer "Something went wrong" to every member until somebody
# edits the file. The bad row is skipped, with one warning for the call, and the good code lives.


def _poison_expires_on(conn, bad: str) -> None:
    _put_alerted(conn, 2, NOW)
    _put_details(conn, 2, NOW)
    conn.execute("UPDATE code_details SET expires_on = ? WHERE code = ?", (bad, _code(2)))


def _poison_clock_start(conn, bad: str) -> None:
    _put_alerted(conn, 2, NOW)
    _put_details(conn, 2, NOW)
    conn.execute("UPDATE code_details SET clock_start = ? WHERE code = ?", (bad, _code(2)))


def _poison_first_seen(conn, bad: str) -> None:
    _put_alerted(conn, 2, bad)


@pytest.mark.parametrize(
    ("poison", "bad"),
    [
        pytest.param(_poison_expires_on, "2026-19-39", id="expires-month-19"),
        pytest.param(_poison_expires_on, "2026-02-30", id="expires-feb-30"),
        pytest.param(_poison_clock_start, "yesterday", id="clock-start-word"),
        pytest.param(_poison_clock_start, "", id="clock-start-empty"),
        pytest.param(_poison_first_seen, "garbage", id="first-seen-word"),
        pytest.param(_poison_first_seen, "9999-99-99", id="first-seen-digits"),
    ],
)
def test_one_malformed_row_does_not_take_the_current_list_down(v3_db, poison, bad):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)  # a healthy code next to the damage
            poison(conn, bad)
        got = {c.code for c in repo.current_codes(conn, now=NOW)}
    assert _code(1) in got


@pytest.mark.parametrize(
    ("poison", "bad"),
    [
        pytest.param(_poison_expires_on, "2026-19-39", id="expires-month-19"),
        pytest.param(_poison_clock_start, "yesterday", id="clock-start-word"),
        pytest.param(_poison_first_seen, "garbage", id="first-seen-word"),
    ],
)
def test_one_malformed_row_does_not_take_the_history_down(v3_db, poison, bad):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
            poison(conn, bad)
        got = {v.code for v in repo.history_codes(conn, NOW - timedelta(days=90))}
    assert _code(1) in got


def test_many_bad_rows_make_one_warning_per_call(v3_db, caplog):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
            for n in range(2, 8):
                _put_alerted(conn, n, "garbage")
        with caplog.at_level("WARNING", logger="newsbot.store.repo"):
            assert [c.code for c in repo.current_codes(conn, now=NOW)] == [_code(1)]
            assert len(caplog.records) == 1
            assert "skipped 6" in caplog.records[0].getMessage()
            caplog.clear()
            repo.history_codes(conn, NOW - timedelta(days=90))
            assert len(caplog.records) == 1


def test_a_clean_read_logs_nothing(v3_db, caplog):
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
        with caplog.at_level("WARNING", logger="newsbot.store.repo"):
            repo.current_codes(conn, now=NOW)
            repo.history_codes(conn, NOW - timedelta(days=90))
    assert not caplog.records


def test_a_malformed_row_in_an_unreleased_status_is_never_read(v3_db):
    """Damage in a row the statuses already exclude can't hurt: SQL drops it before parsing."""
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
            _put_alerted(conn, 2, "garbage", "failed")
            _put_alerted(conn, 3, NOW, "pending")
            _put_details(conn, 3, NOW)
            conn.execute("UPDATE code_details SET clock_start = 'x' WHERE code = ?", (_code(3),))
        assert [c.code for c in repo.current_codes(conn, now=NOW)] == [_code(1)]


def test_a_malformed_expiry_that_is_in_the_past_by_text_is_simply_dead(v3_db):
    """'2026-01-99' sorts before today, so SQL drops the row before Python ever parses it."""
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
            _put_alerted(conn, 2, NOW)
            _put_details(conn, 2, NOW)
            conn.execute(
                "UPDATE code_details SET expires_on = '2026-01-39' WHERE code = ?", (_code(2),)
            )
        assert [c.code for c in repo.current_codes(conn, now=NOW)] == [_code(1)]


# --- naive clocks ---


@pytest.fixture
def machine_in_pago_pago():
    """The process thinks it is in UTC-11, whatever the CI box thinks; put back afterwards."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Pago_Pago"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_a_naive_now_is_read_as_utc_like_the_rest_of_the_store(v3_db, machine_in_pago_pago):
    naive = datetime(2026, 10, 10, 6, 0)  # 23:00 on Oct 9 in Los Angeles, still the expiry day
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, NOW)
            _put_details(conn, 1, NOW, expires_on=date(2026, 10, 9))
        stored = repo.code_details_for(conn, [_code(1)]).get(_code(1))
        assert is_current(stored, NOW, now=naive) is True
        got = [c.code for c in repo.current_codes(conn, now=naive)]
    assert got == [_code(1)]


# --- a Pacific-aware clock across a daylight saving change ---
#
# "180 days earlier" is 180 * 24 hours. `now - timedelta(days=180)` on a datetime carrying a
# real zone is *wall clock* subtraction, so when a DST change sits in between, the cutoff lands
# an hour off. The repo turns `now` into UTC before it subtracts anything.


@pytest.mark.parametrize(
    ("label", "now_utc"),
    [
        ("now in PST, the cutoff in PDT", datetime(2028, 3, 1, 8, 0, tzinfo=UTC)),
        ("now in PDT, the cutoff in PST", datetime(2026, 6, 1, 18, 0, tzinfo=UTC)),
    ],
)
def test_a_pacific_clock_gets_the_same_180_day_cutoff_as_utc(v3_db, label, now_utc):
    exact = now_utc - timedelta(days=CLOCK_DAYS)  # UTC: plain 24-hour days
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, exact)  # exactly 180 days old: gone
            _put_alerted(conn, 2, exact + ONE_US)  # a microsecond younger: here
            _put_alerted(conn, 3, exact - timedelta(minutes=30))  # half an hour past: gone
            _put_alerted(conn, 4, exact + timedelta(minutes=30))  # half an hour left: here
        in_utc = {c.code for c in repo.current_codes(conn, now=now_utc)}
        in_pacific = {c.code for c in repo.current_codes(conn, now=now_utc.astimezone(PT))}
    assert in_utc == {_code(2), _code(4)}, label
    assert in_pacific == in_utc, label


# --- history_codes ---


def _history_expected(world: World, since: datetime) -> list[str]:
    keep = [
        (index, code, seen)
        for index, (code, seen, status) in enumerate(world.rows)
        if status in RELEASED and seen >= since
    ]
    keep.sort(key=lambda item: (-_us(item[2]), item[0]))
    return [code for _, code, _ in keep]


@pytest.mark.parametrize("seed", range(20))
def test_history_codes_matches_the_window_rule_on_random_worlds(v3_db, seed):
    rng = random.Random(1000 + seed)  # noqa: S311
    with closing(connect(v3_db)) as conn:
        world = _build_world(conn, rng, NOW)
        candidates = [NOW - timedelta(days=d) for d in (1, 14, 90)]
        for _code_text, seen, _status in rng.sample(world.rows, k=5):
            candidates += [seen - ONE_US, seen, seen + ONE_US]
        for since in candidates:
            got = [v.code for v in repo.history_codes(conn, since.astimezone(rng.choice(ZONES)))]
            assert got == _history_expected(world, since), (seed, since.isoformat())


def test_history_window_includes_the_instant_it_starts_at(v3_db):
    since = NOW - timedelta(days=14)
    with closing(connect(v3_db)) as conn:
        with conn:
            _put_alerted(conn, 1, since - ONE_US)
            _put_alerted(conn, 2, since)
            _put_alerted(conn, 3, since + ONE_US)
        got = [v.code for v in repo.history_codes(conn, since)]
    assert got == [_code(3), _code(2)]


def test_history_ties_break_on_insertion_order_and_exclude_unreleased(v3_db):
    with closing(connect(v3_db)) as conn:
        with conn:
            for n, status in ((5, "posted"), (3, "pending"), (9, "too_old"), (1, "failed")):
                _put_alerted(conn, n, NOW, status)
            _put_alerted(conn, 7, NOW + timedelta(seconds=1), "seeded")
        got = [(v.code, v.status) for v in repo.history_codes(conn, NOW - timedelta(days=1))]
    assert got == [(_code(7), "seeded"), (_code(5), "posted"), (_code(9), "too_old")]


def test_history_codes_has_no_row_cap_and_joins_details_across_every_chunk(v3_db):
    """No LIMIT anywhere: 10,000 codes in the window all come back, each with its own details.

    The details are fetched 900 codes at a time (SQLite's variable limit), so a mix-up at a
    chunk edge would show as one code wearing its neighbor's reward. The practical cap is the
    window itself: 90 days at most. This pins "uncapped" so that adding a LIMIT later has to
    be a decision, and has to touch this test.
    """
    total = 10_000
    with closing(connect(v3_db)) as conn:
        with conn:
            conn.executemany(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
                "VALUES (?, ?, 'Feed', 'https://example.com/p', 'posted', 0, 0)",
                [(_code(n), (NOW - timedelta(seconds=n)).isoformat()) for n in range(total)],
            )
            conn.executemany(
                "INSERT INTO code_details (code, reward, clock_start, updated_at) "
                "VALUES (?, ?, ?, ?)",
                [(_code(n), f"reward {n}", NOW.isoformat(), NOW.isoformat()) for n in range(total)],
            )
        started = time.perf_counter()
        views = repo.history_codes(conn, NOW - timedelta(days=90))
        elapsed = time.perf_counter() - started
    assert len(views) == total
    assert [v.code for v in views] == [_code(n) for n in range(total)]  # newest first
    assert all(
        v.details is not None and v.details.reward == f"reward {i}" for i, v in enumerate(views)
    )
    assert elapsed < 10, f"10,000 history rows took {elapsed:.1f}s"


def test_current_codes_at_five_thousand_is_quick(v3_db):
    total = 5_000
    with closing(connect(v3_db)) as conn:
        with conn:
            conn.executemany(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
                "VALUES (?, ?, 'Feed', 'https://example.com/p', 'posted', 0, 0)",
                [(_code(n), (NOW - timedelta(seconds=n)).isoformat()) for n in range(total)],
            )
            conn.executemany(
                "INSERT INTO code_details (code, reward, category, expires_on, clock_start, "
                "updated_at) VALUES (?, 'k', 'golden', '2026-12-01', ?, ?)",
                [(_code(n), NOW.isoformat(), NOW.isoformat()) for n in range(total)],
            )
        started = time.perf_counter()
        entries = repo.current_codes(conn, now=NOW)
        elapsed = time.perf_counter() - started
    assert len(entries) == total
    assert elapsed < 10, f"5,000 current codes took {elapsed:.1f}s"


# --- a reader against a writer (WAL) ---


def test_a_reader_never_sees_a_writers_uncommitted_batch_and_is_never_blocked(v3_db):
    with closing(connect(v3_db)) as setup, setup:
        _put_alerted(setup, 1, NOW)
    with closing(connect(v3_db)) as writer, closing(connect(v3_db)) as reader:
        writer.execute("BEGIN IMMEDIATE")
        for n in range(10, 20):
            _put_alerted(writer, n, NOW)
            _put_details(writer, n, NOW, category="golden", expires_on=TODAY + timedelta(days=3))
        started = time.perf_counter()
        during = [c.code for c in repo.current_codes(reader, now=NOW)]
        during_history = [v.code for v in repo.history_codes(reader, NOW - timedelta(days=1))]
        waited = time.perf_counter() - started
        writer.execute("COMMIT")
        after = {c.code for c in repo.current_codes(reader, now=NOW)}
    assert during == [_code(1)] and during_history == [_code(1)]
    assert waited < 2, "a read waited on a write lock; WAL readers shouldn't"
    assert after == {_code(1), *(_code(n) for n in range(10, 20))}


def test_readers_during_a_stream_of_atomic_batches_only_ever_see_whole_batches(v3_db):
    """Each batch is `BATCH` released codes with details, committed in one transaction.

    A reader that looks while the writer is mid-stream must see a multiple of `BATCH`, and
    every code it sees must carry the details that were committed with it.
    """
    batch, rounds = 12, 40
    stop = threading.Event()
    problems: list[str] = []

    def write() -> None:
        with closing(connect(v3_db)) as conn:
            for r in range(rounds):
                with conn:
                    for i in range(batch):
                        n = r * batch + i
                        _put_alerted(conn, n, NOW)
                        _put_details(
                            conn,
                            n,
                            NOW,
                            reward=f"batch {r}",
                            category="golden",
                            expires_on=TODAY + timedelta(days=5),
                        )
        stop.set()

    def read() -> None:
        with closing(connect(v3_db)) as conn:
            while not stop.is_set():
                try:
                    entries = repo.current_codes(conn, now=NOW)
                except sqlite3.Error as exc:  # a locked database would be the bug
                    problems.append(f"sqlite error {exc!r}")
                    return
                if len(entries) % batch:
                    problems.append(f"saw {len(entries)} codes, not a whole number of batches")
                for entry in entries:
                    n = int(entry.code[:5])
                    if entry.details is None or entry.details.reward != f"batch {n // batch}":
                        problems.append(f"{entry.code} lost its details")
                        return

    threads = [threading.Thread(target=read) for _ in range(3)] + [threading.Thread(target=write)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert problems == []
    with closing(connect(v3_db)) as conn:
        assert len(repo.current_codes(conn, now=NOW)) == batch * rounds


def test_a_write_landing_between_the_two_reads_cannot_tear_the_snapshot(v3_db, monkeypatch):
    """`current_codes` runs two statements (the filter, then the details) on an autocommit
    connection, so they are two snapshots. A hook commit between them can leave a code that
    SQL kept on its old details (current) drawn with its new ones (here: expired yesterday,
    reward changed). A consistent read shows the code as it was, or not at all, never a mix.
    """
    with closing(connect(v3_db)) as setup, setup:
        _put_alerted(setup, 1, NOW)
        _put_details(setup, 1, NOW, reward="OLD", expires_on=TODAY + timedelta(days=3))
    real = repo.code_details_for

    def details_after_a_write(conn, codes):
        with closing(connect(v3_db)) as other, other:
            other.execute(
                "UPDATE code_details SET reward = 'NEW', expires_on = ? WHERE code = ?",
                ((TODAY - timedelta(days=1)).isoformat(), _code(1)),
            )
        return real(conn, codes)

    monkeypatch.setattr(repo, "code_details_for", details_after_a_write)
    with closing(connect(v3_db)) as conn:
        entries = repo.current_codes(conn, now=NOW)
    shown = [(e.code, e.details.reward) for e in entries]
    assert shown in ([], [(_code(1), "OLD")]), shown
