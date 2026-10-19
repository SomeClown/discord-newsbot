"""Adversarial tests for the `code_details` store (migration 009, design.md §16), beyond
test_repo_code_details.py.

Angle (test-engineer brief, 2026-10-09): `upsert_code_details` is a read-modify-write under
`BEGIN IMMEDIATE`, fed text a stranger's web page wrote, in the middle of a pass that also
records sightings and releases codes. So these tests go after the three things that can
actually hurt: two writers (threads on their own connections, a held lock, the busy timeout),
one bad observation in a batch of good ones, and text that is strange to SQL, to SQLite's
`length()` and to UTF-16. They also pin what the returned "changed" list promises, the clock's
edges as the store feeds them (the marker, the first sighting, a clock that goes backwards),
and what a change to only a `*_from` name does to an alert's "via" line.

Where the behavior is surprising it's pinned and labelled "documented". Real bugs are
`xfail(strict=True)` with the reason spelled out.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot.bot.format import details_line, discord_len, esc
from newsbot.collectors.base import ItemDetails
from newsbot.shift.details import DetailObservation
from newsbot.store import repo
from newsbot.store.db import connect

CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWX1"
OTHER = "BBBBB-BBBBB-BBBBB-BBBBB-BBBB2"
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def conn(v3_db):
    with closing(connect(v3_db)) as c:
        repo.app_state_set(c, repo.DETAILS_SINCE_KEY, SINCE.isoformat())
        yield c


def obs(rank="table", name="mentalmars", *, code=CODE, published_at=None, **fields):
    return DetailObservation(
        code=code,
        details=ItemDetails(code=code, rank=rank, **fields),
        source_name=name,
        published_at=published_at,
    )


def see(conn, code, first_seen_at):
    repo.record_released_codes(
        conn, [(code, "Feed", "https://example.com/p", False)], now=lambda: first_seen_at
    )


def one(conn, code=CODE):
    return repo.code_details_for(conn, [code])[code]


def rng_for(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 (a reproducible sequence, not a secret)


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# --- text that is strange to SQL, to SQLite and to Python ---

NASTY = [
    "x'); DROP TABLE code_details; --",
    '"; DELETE FROM alerted_codes; --',
    "Robert'); --",
    "100% of _wildcards_ and \\backslashes\\",
    "\u202eRTL override\u202c and a zero\u200bwidth space",
    "line one\nline two\r\nline three\ttabbed",
    "emoji \U0001f600\U0001f3ae and CJK \U00020bb7 and combining é",
    "@everyone <@123> <#456> [link](https://example.com)",
    "?" * 50,
    " leading and trailing ",
]


@pytest.mark.parametrize("text", NASTY)
def test_strange_text_round_trips_byte_for_byte_and_hurts_nothing(conn, text):
    see(conn, OTHER, NOW)
    repo.upsert_code_details(conn, [obs(reward=text, origin=text)], now=NOW)
    got = one(conn)
    assert (got.reward, got.origin) == (text, text)
    # Every table is still there and nothing else was touched.
    assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 1


@pytest.mark.parametrize("text", NASTY)
def test_strange_text_in_a_source_name_round_trips_too(conn, text):
    repo.upsert_code_details(
        conn, [obs(name=text, reward="x", expires_on=date(2026, 10, 14))], now=NOW
    )
    got = one(conn)
    assert (got.reward_from, got.expires_from) == (text, text)


@pytest.mark.parametrize("field", ["reward", "origin", "name", "code"])
def test_a_nul_anywhere_in_the_text_is_refused_not_stored(conn, field, caplog):
    # SQLite's length() stops at a NUL, so a CHECK on it can be talked past. The observation
    # is skipped (once, with a warning) and the rest of the batch goes through.
    bad = {
        "reward": obs(reward="a\0b"),
        "origin": obs(origin="o\0"),
        "name": obs(name="m\0", reward="x"),
        "code": obs(code="ABCDE-FGHIJ-KLMNO-PQRST-UVW\0X", reward="x"),
    }[field]
    with caplog.at_level(logging.WARNING, logger="newsbot.store.repo"):
        changed = repo.upsert_code_details(conn, [bad, obs(code=OTHER, reward="fine")], now=NOW)
    assert changed == [OTHER]
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 1
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_the_check_counts_code_points_and_stops_at_a_nul_so_it_is_weaker_than_the_cap_documented(
    conn,
):
    # The plan's cap is "escaped, in UTF-16 units, at most 300". The CHECK is `length() <= 300`,
    # which SQLite counts in code points and ends at the first NUL. Honest rows always pass it
    # (at most 300 shown units means at most 300 code points). Dishonest ones can too.
    def insert(reward):
        conn.execute(
            "INSERT INTO code_details (code, reward, clock_start, updated_at) "
            "VALUES (?, ?, 'n', 'n')",
            (f"{len(reward):05d}-AAAAA-AAAAA-AAAAA-AAAAA"[:29], reward),
        )

    insert("\U0001f600" * 300)  # 300 code points, 600 UTF-16 units: the CHECK is fine with it
    with pytest.raises(sqlite3.IntegrityError):
        insert("\U0001f600" * 301)
    insert("x\0" + "y" * 500)  # 501 characters, and the CHECK saw one
    stored = conn.execute("SELECT reward FROM code_details WHERE length(reward) = 1").fetchone()
    assert len(stored["reward"]) == 502


def test_300_bmp_characters_pass_and_301_are_skipped_for_reward_and_origin(conn, caplog):
    for field in ("reward", "origin"):
        repo.upsert_code_details(conn, [obs(**{field: "x" * 300})], now=NOW)
        assert getattr(one(conn), field) == "x" * 300
    for field in ("reward", "origin"):
        with caplog.at_level(logging.WARNING, logger="newsbot.store.repo"):
            changed = repo.upsert_code_details(
                conn, [obs(code=OTHER, **{field: "x" * 301})], now=NOW
            )
        assert changed == []
        assert repo.code_details_for(conn, [OTHER]) == {}


def test_the_cap_is_measured_as_shown_so_300_emoji_are_refused(conn):
    # The plan's cap is "escaped, in UTF-16 units": 300 emoji are 600 of them. The store
    # double-checks the producers' promise (`cap_shown`) instead of trusting it.
    reward = "\U0001f600" * 300
    assert discord_len(esc(reward)) == 600
    assert utf16(reward) == 600
    assert repo.upsert_code_details(conn, [obs(reward=reward)], now=NOW) == []
    assert repo.code_details_for(conn, [CODE]) == {}


def test_301_emoji_are_refused_but_150_emoji_under_the_shown_cap_pass(conn):
    repo.upsert_code_details(conn, [obs(reward="\U0001f600" * 150)], now=NOW)
    assert discord_len(esc(one(conn).reward)) == 300  # exactly the shown cap
    assert (
        repo.upsert_code_details(conn, [obs(code=OTHER, reward="\U0001f600" * 301)], now=NOW) == []
    )
    assert repo.code_details_for(conn, [OTHER]) == {}


def test_markdown_that_doubles_when_escaped_is_refused_too(conn):
    # 300 asterisks is 300 raw characters and 600 shown (every one gets a backslash); 150 fit.
    assert discord_len(esc("*" * 300)) == 600
    assert repo.upsert_code_details(conn, [obs(reward="*" * 300)], now=NOW) == []
    assert repo.upsert_code_details(conn, [obs(reward="*" * 150)], now=NOW) == [CODE]
    assert discord_len(esc(one(conn).reward)) == 300


# --- one bad observation in a batch ---


def test_a_bad_observation_is_skipped_once_and_the_log_says_nothing_secret(conn, caplog):
    secret = "TOPSECRETREWARDTEXT" + "x" * 290
    assert len(secret) == 309
    batch = [
        obs(code="CCCCC-CCCCC-CCCCC-CCCCC-CCCC3", reward="first good"),
        obs(code=OTHER, reward=secret, origin=secret),
        obs(reward="last good", expires_on=date(2026, 10, 14)),
    ]
    with caplog.at_level(logging.DEBUG):
        changed = repo.upsert_code_details(conn, batch, now=NOW)
    assert sorted(changed) == sorted(["CCCCC-CCCCC-CCCCC-CCCCC-CCCC3", CODE])
    assert set(repo.code_details_for(conn, [CODE, OTHER, "CCCCC-CCCCC-CCCCC-CCCCC-CCCC3"])) == {
        CODE,
        "CCCCC-CCCCC-CCCCC-CCCCC-CCCC3",
    }
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "TOPSECRET" not in caplog.text
    assert "last good" not in caplog.text and "first good" not in caplog.text


def test_a_bad_observation_for_a_code_does_not_spoil_a_good_one_for_the_same_code(conn):
    bad = obs(reward="x" * 301)
    good = obs(reward="fine", expires_on=date(2026, 10, 14))
    for batch in ([bad, good], [good, bad]):
        got = repo.upsert_code_details(conn, batch, now=NOW)
        assert got == [CODE]
        assert one(conn).reward == "fine"
        conn.execute("DELETE FROM code_details")
        conn.commit()


def test_a_wrong_rank_and_a_mismatched_code_are_each_skipped_not_fatal(conn, caplog):
    wrong_rank = obs("press", reward="x")
    mismatched = DetailObservation(
        code=OTHER, details=ItemDetails(code=CODE, rank="table", reward="x"), source_name="m"
    )
    with caplog.at_level(logging.WARNING, logger="newsbot.store.repo"):
        changed = repo.upsert_code_details(
            conn, [wrong_rank, mismatched, obs(reward="ok")], now=NOW
        )
    assert changed == [CODE]
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2
    assert repo.code_details_for(conn, [OTHER]) == {}


def test_a_batch_of_nothing_but_bad_observations_writes_nothing_and_holds_no_lock(conn):
    assert repo.upsert_code_details(conn, [obs(reward="x" * 301)], now=NOW) == []
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 0


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"category": "legendary", "reward": "x"}, id="a-category-the-check-refuses"),
        pytest.param({"reward": "ok\ud800"}, id="a-lone-surrogate-sqlite-cannot-encode"),
    ],
)
def test_an_observation_the_database_cannot_hold_is_skipped_not_fatal(conn, bad):
    good = obs(code=OTHER, reward="a good one")
    repo.upsert_code_details(conn, [good, obs(**bad)], now=NOW)
    assert OTHER in repo.code_details_for(conn, [OTHER, CODE])


def test_a_generator_of_observations_works_and_duplicates_are_harmless(conn):
    changed = repo.upsert_code_details(
        conn, (obs(reward="same", expires_on=date(2026, 10, 14)) for _ in range(5)), now=NOW
    )
    assert changed == [CODE]
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 1


# --- the returned list ---


def test_each_changed_code_is_listed_once_in_the_order_its_first_change_happens(conn):
    third, second = "CCCCC-CCCCC-CCCCC-CCCCC-CCCC3", "DDDDD-DDDDD-DDDDD-DDDDD-DDDD4"
    batch = [
        obs(code=OTHER, reward="a", published_at=NOW - timedelta(minutes=1)),
        obs(code=OTHER, reward="b", published_at=NOW - timedelta(minutes=2)),
        obs(code=CODE, reward="c", published_at=NOW - timedelta(minutes=3)),
        obs(code=third, reward="d", published_at=NOW - timedelta(minutes=4)),
        obs(code=second, reward="e", published_at=NOW - timedelta(minutes=5)),
    ]
    changed = repo.upsert_code_details(conn, batch, now=NOW)
    # Oldest publish time first, each code once.
    assert changed == [second, third, CODE, OTHER]


def test_a_code_whose_only_news_is_added_on_origin_or_a_listing_is_not_listed(conn):
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    changed = repo.upsert_code_details(
        conn, [obs(reward="x", added_on=date(2026, 10, 1), origin="Event", listed=True)], now=LATER
    )
    assert changed == []


def test_a_lower_rank_reading_that_loses_is_not_listed(conn):
    repo.upsert_code_details(
        conn, [obs("official", "Gearbox", expires_on=date(2026, 10, 14))], now=NOW
    )
    assert repo.upsert_code_details(conn, [obs(expires_on=date(2026, 12, 1))], now=LATER) == []
    assert one(conn).details_changed_at == NOW


def test_a_skipped_bad_observation_is_never_listed(conn):
    assert repo.upsert_code_details(conn, [obs(reward="x" * 301)], now=NOW) == []


def test_property_the_list_is_exactly_the_codes_whose_details_changed_at_moved(conn):
    codes = [f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(8)]
    for seed in range(120):
        rng = rng_for(seed)

        def batch(codes=codes, rng=rng):
            out = []
            for _ in range(rng.randint(1, 10)):
                rank = rng.choice(["table", "official"])
                out.append(
                    obs(
                        rank,
                        rng.choice(["mentalmars", "Gearbox", "other"]),
                        code=rng.choice(codes),
                        published_at=NOW - timedelta(minutes=rng.randint(0, 500)),
                        reward=rng.choice(["", "A", "B", "3 Golden Keys"]),
                        category=rng.choice([None, "golden", "other", "cosmetic"]),
                        expires_on=rng.choice([None, date(2026, 10, 14), date(2026, 10, 20)]),
                    )
                )
            return out

        conn.execute("DELETE FROM code_details")
        conn.commit()
        repo.upsert_code_details(conn, batch(), now=NOW)
        before = repo.code_details_for(conn, codes)
        changed = repo.upsert_code_details(conn, batch(), now=LATER)
        after = repo.code_details_for(conn, codes)

        moved = {
            c
            for c, row in after.items()
            if row.details_changed_at != (before[c].details_changed_at if c in before else None)
        }
        assert set(changed) == moved, f"seed {seed}"
        assert len(changed) == len(set(changed)), f"seed {seed}"
        # Soundness for alerts: a net change to anything the details line shows is never
        # missed (QA batch 1, fix 14: "shown" is the printed line, not the raw columns; a
        # long-ago clock keeps every distinct date a distinct phrase).
        long_ago = datetime(2000, 1, 1, tzinfo=UTC)
        for c, row in after.items():
            if c in before and details_line(row, now=long_ago) != details_line(
                before[c], now=long_ago
            ):
                assert c in changed, f"seed {seed}: missed an edit for {c}"
            if c not in before and details_line(row, now=long_ago) is not None:
                assert c in changed, f"seed {seed}: missed a new row for {c}"


def test_a_batch_that_ends_where_it_started_reports_no_change(conn):
    repo.upsert_code_details(conn, [obs(name="B", expires_on=date(2026, 10, 20))], now=NOW)
    changed = repo.upsert_code_details(
        conn,
        [
            obs(name="A", expires_on=date(2026, 10, 14), published_at=LATER - timedelta(minutes=5)),
            obs(name="B", expires_on=date(2026, 10, 20), published_at=LATER),
        ],
        now=LATER,
    )
    assert one(conn).expires_on == date(2026, 10, 20)
    assert changed == []
    assert one(conn).details_changed_at == NOW


def test_two_real_changes_at_the_same_instant_are_both_listed(conn):
    assert repo.upsert_code_details(conn, [obs(reward="one")], now=NOW) == [CODE]
    assert repo.upsert_code_details(conn, [obs(reward="two")], now=NOW) == [CODE]


# --- display-relevant: a change to only a *_from name ---


def test_a_higher_rank_confirming_a_reward_takes_the_via_name_and_flags_an_edit(conn):
    repo.upsert_code_details(conn, [obs(reward="3 Golden Keys")], now=NOW)
    changed = repo.upsert_code_details(
        conn, [obs("official", "Gearbox", reward="3 Golden Keys")], now=LATER
    )
    got = one(conn)
    assert changed == [CODE]
    assert (got.reward_from, got.reward_source) == ("Gearbox", "official")
    assert got.details_changed_at == LATER  # an alert rendered before LATER now says "via Gearbox"


def test_the_via_line_changes_shape_and_flags_an_edit_when_one_field_changes_hands(conn):
    # Reward and expiry both from mentalmars: "via mentalmars". Gearbox then repeats the same
    # expiry: the line reads "via mentalmars and Gearbox", and the alert gets edited.
    repo.upsert_code_details(conn, [obs(reward="A skin", expires_on=date(2026, 10, 14))], now=NOW)
    changed = repo.upsert_code_details(
        conn, [obs("official", "Gearbox", expires_on=date(2026, 10, 14))], now=LATER
    )
    got = one(conn)
    assert changed == [CODE]
    assert {got.reward_from, got.expires_from} == {"mentalmars", "Gearbox"}
    assert got.details_changed_at == LATER


def test_a_permanent_codes_category_from_name_flags_an_edit_too(conn):
    repo.upsert_code_details(conn, [obs(category="permanent")], now=NOW)
    assert repo.upsert_code_details(
        conn, [obs("official", "Gearbox", category="permanent")], now=LATER
    ) == [CODE]
    assert (one(conn).category_from, one(conn).details_changed_at) == ("Gearbox", LATER)


def test_two_sources_that_agree_never_swap_the_via_name_and_never_cause_an_edit(conn):
    repo.upsert_code_details(conn, [obs(name="A", reward="same")], now=NOW)
    for i, name in enumerate(["B", "A", "B"], start=1):
        assert (
            repo.upsert_code_details(
                conn, [obs(name=name, reward="same")], now=NOW + timedelta(hours=i)
            )
            == []
        )
        assert one(conn).reward_from == "A"
    assert one(conn).details_changed_at == NOW


def test_a_changed_value_does_carry_its_new_name_with_the_edit(conn):
    repo.upsert_code_details(conn, [obs(reward="old")], now=NOW)
    changed = repo.upsert_code_details(conn, [obs("official", "Gearbox", reward="new")], now=LATER)
    got = one(conn)
    assert changed == [CODE]
    assert (got.reward, got.reward_from, got.details_changed_at) == ("new", "Gearbox", LATER)


# --- the clock, as the store feeds it ---


def test_a_first_sighting_exactly_at_the_marker_and_a_microsecond_either_side(conn):
    for i, (delta, expect_fresh) in enumerate(
        [
            (timedelta(0), False),
            (timedelta(microseconds=-1), True),
            (timedelta(microseconds=1), False),
        ]
    ):
        code = f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA"
        seen = SINCE + delta
        see(conn, code, seen)
        repo.upsert_code_details(conn, [obs(code=code, reward="x")], now=NOW)
        assert one(conn, code).clock_start == (NOW if expect_fresh else seen)


def test_the_marker_survives_the_trip_through_text_when_it_has_microseconds_and_an_offset(conn):
    repo.app_state_set(
        conn, repo.DETAILS_SINCE_KEY, "2026-10-09T09:00:00.500000-07:00"
    )  # 16:00:00.5 UTC
    see(conn, CODE, datetime(2026, 10, 9, 16, 0, 0, 400000, tzinfo=UTC))
    see(conn, OTHER, datetime(2026, 10, 9, 16, 0, 0, 600000, tzinfo=UTC))
    repo.upsert_code_details(conn, [obs(reward="x"), obs(code=OTHER, reward="x")], now=NOW)
    assert one(conn).clock_start == NOW
    assert one(conn, OTHER).clock_start == datetime(2026, 10, 9, 16, 0, 0, 600000, tzinfo=UTC)


def test_a_marker_without_a_time_zone_is_read_as_utc(conn):
    repo.app_state_set(conn, repo.DETAILS_SINCE_KEY, "2026-10-09T16:00:00")
    see(conn, CODE, SINCE + timedelta(minutes=1))
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert one(conn).clock_start == SINCE + timedelta(minutes=1)


@pytest.mark.parametrize("junk", ["hand set", ""])
def test_a_garbled_marker_reads_as_missing_not_as_a_failed_pass(conn, junk):
    repo.app_state_set(conn, repo.DETAILS_SINCE_KEY, junk)
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert one(conn).clock_start == NOW


def test_a_missing_marker_gives_even_a_minute_old_code_the_listing_time_documented(conn):
    # Safe direction, as the docstring says, but note it is "now", not the sighting.
    repo.app_state_delete(conn, repo.DETAILS_SINCE_KEY)
    see(conn, CODE, NOW - timedelta(minutes=1))
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert one(conn).clock_start == NOW


def test_an_old_code_mentioned_by_gearbox_twice_over_two_passes_gets_one_fresh_clock(conn):
    see(conn, CODE, SINCE - timedelta(days=40))
    repo.upsert_code_details(conn, [obs("official", "Gearbox", reward="3 Golden Keys")], now=NOW)
    assert one(conn).clock_start == NOW
    repo.upsert_code_details(
        conn,
        [obs("official", "Gearbox", reward="3 Golden Keys", expires_on=date(2026, 12, 1))],
        now=NOW + timedelta(days=30),
    )
    assert one(conn).clock_start == NOW


def test_a_listing_after_the_official_post_does_not_restart_the_clock(conn):
    see(conn, CODE, SINCE - timedelta(days=40))
    repo.upsert_code_details(conn, [obs("official", "Gearbox", reward="x")], now=NOW)
    repo.upsert_code_details(conn, [obs(reward="x", listed=True)], now=NOW + timedelta(days=3))
    got = one(conn)
    assert got.clock_start == NOW
    assert got.first_listed_at == NOW + timedelta(days=3)


def test_a_first_sighting_in_the_future_is_clamped_to_now(conn):
    future = NOW + timedelta(days=2)
    see(conn, CODE, future)
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert one(conn).clock_start == NOW


def test_first_seen_lookups_survive_more_codes_than_one_sqlite_chunk(conn):
    codes = [f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(1000)]
    seen = SINCE + timedelta(minutes=5)
    with conn:
        conn.executemany(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES (?, ?, 'Feed', 'u', 'posted')",
            [(c, seen.isoformat()) for c in codes],
        )
    repo.upsert_code_details(conn, [obs(code=c, reward="x") for c in codes], now=NOW)
    assert {r.clock_start for r in repo.code_details_for(conn, codes).values()} == {seen}


def test_the_clock_stepping_backwards_still_moves_the_change_stamp_forward(conn):
    # Edits look for details_changed_at newer than the message's rendered_at. If the system
    # clock steps back between a render and the next change, a stamp that went backwards
    # would hide the change from them, so the stamp of a real change is always strictly after
    # the one before it (by a microsecond when the clock is no help).
    repo.upsert_code_details(conn, [obs(reward="one")], now=NOW + timedelta(hours=2))
    assert repo.upsert_code_details(conn, [obs(reward="two")], now=NOW + timedelta(hours=1)) == [
        CODE
    ]
    got = one(conn)
    assert got.reward == "two"
    assert got.details_changed_at == NOW + timedelta(hours=2, microseconds=1)
    assert got.updated_at == NOW + timedelta(hours=2)


# --- precedence through the store, across passes ---


def test_a_table_correcting_its_expiry_earlier_then_later_across_passes(conn):
    repo.upsert_code_details(conn, [obs(expires_on=date(2026, 10, 20))], now=NOW)
    assert repo.upsert_code_details(conn, [obs(expires_on=date(2026, 10, 14))], now=LATER) == [CODE]
    assert one(conn).expires_on == date(2026, 10, 14)
    assert repo.upsert_code_details(
        conn, [obs(expires_on=date(2026, 11, 3))], now=LATER + timedelta(hours=1)
    ) == [CODE]
    assert one(conn).expires_on == date(2026, 11, 3)


def test_a_page_that_forgets_an_expiry_leaves_it_alone_across_passes(conn):
    repo.upsert_code_details(conn, [obs(expires_on=date(2026, 10, 14), reward="A")], now=NOW)
    assert repo.upsert_code_details(conn, [obs(reward="A", listed=True)], now=LATER) == []
    got = one(conn)
    assert (got.expires_on, got.expires_from, got.details_changed_at) == (
        date(2026, 10, 14),
        "mentalmars",
        NOW,
    )


def test_official_and_table_in_one_batch_end_the_same_whichever_comes_first(conn):
    a = obs(
        "official", "Gearbox", expires_on=date(2026, 10, 14), published_at=NOW - timedelta(hours=5)
    )
    b = obs(expires_on=date(2026, 10, 20), published_at=NOW)
    repo.upsert_code_details(conn, [a, b], now=NOW)
    first = one(conn)
    conn.execute("DELETE FROM code_details")
    conn.commit()
    repo.upsert_code_details(conn, [b, a], now=NOW)
    assert one(conn) == first
    assert (first.expires_on, first.expires_source) == (date(2026, 10, 14), "official")


def test_a_date_in_the_past_at_first_sight_are_stored_and_reported(conn):
    assert repo.upsert_code_details(
        conn, [obs(expires_on=date(2026, 1, 1), reward="old")], now=NOW
    ) == [CODE]
    assert one(conn).expires_on == date(2026, 1, 1)


def test_naive_now_is_stored_as_utc(conn):
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW.replace(tzinfo=None))
    got = one(conn)
    assert got.updated_at == NOW and got.updated_at.utcoffset() == timedelta(0)
    stored = conn.execute("SELECT updated_at, clock_start FROM code_details").fetchone()
    assert stored["updated_at"].endswith("+00:00") and stored["clock_start"].endswith("+00:00")


def test_a_non_utc_now_is_stored_as_the_same_instant_in_utc(conn):
    pacific = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    repo.upsert_code_details(conn, [obs(reward="x")], now=pacific)
    stored = conn.execute("SELECT updated_at FROM code_details").fetchone()[0]
    assert stored == NOW.isoformat()


# --- two writers ---


def _in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 (the test reports it)
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_a_writer_waits_for_the_lock_and_then_goes_through(v3_db):
    holder = connect(v3_db)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")

    def writer():
        with closing(connect(v3_db)) as c:
            return repo.upsert_code_details(c, [obs(reward="waited")], now=NOW)

    thread, box = _in_thread(writer)
    time.sleep(0.4)
    assert thread.is_alive(), "the writer should be waiting on the busy timeout"
    holder.execute("COMMIT")
    thread.join(10)
    holder.close()
    assert box.get("result") == [CODE], box
    with closing(connect(v3_db)) as c:
        assert one(c).reward == "waited"


def test_a_writer_that_times_out_leaves_no_trace_and_a_usable_connection(v3_db):
    holder = connect(v3_db)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    try:
        with closing(connect(v3_db)) as c:
            c.execute("PRAGMA busy_timeout = 50")
            mode = c.isolation_level
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                repo.upsert_code_details(c, [obs(reward="never")], now=NOW)
            assert not c.in_transaction
            assert c.isolation_level == mode
    finally:
        holder.execute("COMMIT")
        holder.close()
    with closing(connect(v3_db)) as c:
        assert repo.code_details_for(c, [CODE]) == {}
        assert repo.upsert_code_details(c, [obs(reward="now it works")], now=NOW) == [CODE]


def test_a_failing_writer_does_not_release_a_lock_it_never_held(v3_db):
    # The loser's ROLLBACK path must not touch the winner's transaction (separate connections).
    holder = connect(v3_db)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO code_details (code, clock_start, updated_at) VALUES (?, 'n', 'n')", (OTHER,)
    )
    try:
        with closing(connect(v3_db)) as c:
            c.execute("PRAGMA busy_timeout = 50")
            with pytest.raises(sqlite3.OperationalError):
                repo.upsert_code_details(c, [obs(reward="x")], now=NOW)
        assert holder.in_transaction
    finally:
        holder.execute("COMMIT")
        holder.close()
    with closing(connect(v3_db)) as c:
        assert c.execute("SELECT code FROM code_details").fetchall()[0][0] == OTHER


def test_eight_racing_writers_lose_no_update_and_never_tear_a_row(v3_db):
    # One official and seven tables hit the same code at once; each table has its own reward,
    # category and expiry. Whatever order the lock hands them out in, official wins what it
    # said, and every table-won field's value and name must belong to the same writer.
    tables = {
        f"site{i}": (
            f"reward {i}",
            ["golden", "cosmetic", "other", "permanent"][i % 4],
            date(2026, 11, 1 + i),
        )
        for i in range(7)
    }
    own_codes = [f"{i:05d}-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ" for i in range(8)]
    barrier = threading.Barrier(8)

    def table_writer(name, i):
        reward, category, expiry = tables[name]
        with closing(connect(v3_db)) as c:
            barrier.wait()
            return repo.upsert_code_details(
                c,
                [
                    obs(name=name, reward=reward, category=category, expires_on=expiry),
                    obs(name=name, code=own_codes[i], reward=name),
                ],
                now=NOW + timedelta(seconds=i),
            )

    def official_writer():
        with closing(connect(v3_db)) as c:
            barrier.wait()
            return repo.upsert_code_details(
                c,
                [
                    obs("official", "Gearbox", reward="OFFICIAL", expires_on=date(2026, 10, 30)),
                    obs("official", "Gearbox", code=own_codes[7], reward="gearbox"),
                ],
                now=NOW + timedelta(seconds=9),
            )

    runs = [_in_thread(lambda n=n, i=i: table_writer(n, i)) for i, n in enumerate(tables)]
    runs.append(_in_thread(official_writer))
    for thread, _ in runs:
        thread.join(30)
    assert [box for _, box in runs if "error" in box] == []

    with closing(connect(v3_db)) as c:
        got = one(c)
        assert (got.reward, got.reward_source, got.reward_from) == (
            "OFFICIAL",
            "official",
            "Gearbox",
        )
        assert (got.expires_on, got.expires_source) == (date(2026, 10, 30), "official")
        assert got.category_source == "table"
        assert got.category == tables[got.category_from][1]
        assert got.clock_start in {NOW + timedelta(seconds=s) for s in (*range(7), 9)}
        assert len(repo.code_details_for(c, own_codes)) == 8
        assert c.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 9
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
