"""The `code_details` store: upsert, read-back, table-listed markers, retention (migration 009).

`merge_details` has its own tests (test_details_merge.py). These check that the repo feeds it the
right inputs (the existing row, the code's first sighting, the rollout marker), writes what it
hands back, and reports what changed.
"""

import logging
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.collectors.base import ItemDetails
from newsbot.shift.details import DetailObservation
from newsbot.store import repo
from newsbot.store.db import connect

CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWX1"
OTHER = "BBBBB-BBBBB-BBBBB-BBBBB-BBBB2"
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)


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


def one(conn, code=CODE):
    return repo.code_details_for(conn, [code])[code]


def see(conn, code, first_seen_at):
    """Record `code` as released at `first_seen_at`, the way detection does."""
    repo.record_released_codes(
        conn, [(code, "Feed", "https://example.com/p", False)], now=lambda: first_seen_at
    )


def test_a_new_row_round_trips_with_every_field(conn):
    changed = repo.upsert_code_details(
        conn,
        [
            obs(
                reward="3 Golden Keys",
                category="golden",
                expires_on=date(2026, 10, 14),
                added_on=date(2026, 10, 8),
                origin="Shift event",
                listed=True,
            )
        ],
        now=NOW,
    )
    assert changed == [CODE]
    got = repo.code_details_for(conn, [CODE])[CODE]
    assert (got.reward, got.reward_source, got.reward_from) == (
        "3 Golden Keys",
        "table",
        "mentalmars",
    )
    assert (got.category, got.category_source) == ("golden", "table")
    assert (got.expires_on, got.expires_source) == (date(2026, 10, 14), "table")
    assert (got.added_on, got.origin) == (date(2026, 10, 8), "Shift event")
    assert (got.clock_start, got.updated_at, got.details_changed_at) == (NOW, NOW, NOW)
    assert (got.first_listed_at, got.last_listed_at) == (NOW, NOW)
    assert got.clock_start.tzinfo is not None


def test_a_codes_with_no_row_are_simply_missing(conn):
    assert repo.code_details_for(conn, [CODE, OTHER]) == {}
    assert repo.code_details_for(conn, []) == {}


def test_more_codes_than_one_sqlite_chunk(conn):
    codes = [f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(1000)]
    repo.upsert_code_details(conn, [obs(code=c, reward="x") for c in codes], now=NOW)
    assert len(repo.code_details_for(conn, codes)) == 1000


def test_a_second_identical_pass_changes_nothing_and_says_so(conn):
    first = obs(reward="A skin", category="cosmetic", listed=True)
    assert repo.upsert_code_details(conn, [first], now=NOW) == [CODE]
    later = NOW + timedelta(hours=1)
    assert repo.upsert_code_details(conn, [first], now=later) == []
    got = repo.code_details_for(conn, [CODE])[CODE]
    assert got.details_changed_at == NOW
    assert (got.updated_at, got.last_listed_at, got.first_listed_at) == (later, later, NOW)


def test_a_changed_expiry_is_reported_and_stamped(conn):
    repo.upsert_code_details(conn, [obs(expires_on=date(2026, 10, 14))], now=NOW)
    later = NOW + timedelta(hours=1)
    changed = repo.upsert_code_details(conn, [obs(expires_on=date(2026, 10, 20))], now=later)
    assert changed == [CODE]
    got = repo.code_details_for(conn, [CODE])[CODE]
    assert (got.expires_on, got.details_changed_at, got.clock_start) == (
        date(2026, 10, 20),
        later,
        NOW,
    )


def test_official_outranks_table_across_passes(conn):
    repo.upsert_code_details(
        conn, [obs("official", "Gearbox", expires_on=date(2026, 10, 14))], now=NOW
    )
    repo.upsert_code_details(
        conn, [obs(expires_on=date(2026, 10, 20), reward="A skin")], now=NOW + timedelta(hours=1)
    )
    got = repo.code_details_for(conn, [CODE])[CODE]
    assert (got.expires_on, got.expires_from) == (date(2026, 10, 14), "Gearbox")
    assert (got.reward, got.reward_source) == ("A skin", "table")


def test_inside_one_pass_observations_fold_oldest_first(conn):
    older = obs(name="old", published_at=NOW - timedelta(days=2), expires_on=date(2026, 10, 14))
    newer = obs(name="new", published_at=NOW - timedelta(days=1), expires_on=date(2026, 10, 20))
    repo.upsert_code_details(conn, [newer, older], now=NOW)
    got = repo.code_details_for(conn, [CODE])[CODE]
    assert (got.expires_on, got.expires_from) == (date(2026, 10, 20), "new")


def test_codes_are_independent_and_only_changed_ones_are_reported(conn):
    repo.upsert_code_details(conn, [obs(reward="A")], now=NOW)
    changed = repo.upsert_code_details(
        conn, [obs(reward="A"), obs(code=OTHER, reward="B")], now=NOW + timedelta(hours=1)
    )
    assert changed == [OTHER]


def test_a_table_row_with_nothing_to_show_is_stored_but_not_reported(conn):
    assert repo.upsert_code_details(conn, [obs(origin="Shift event", listed=True)], now=NOW) == []
    got = repo.code_details_for(conn, [CODE])[CODE]
    assert got.details_changed_at is None and got.origin == "Shift event"


# --- the clock reads alerted_codes and the marker ---


def test_a_code_first_seen_after_the_marker_keeps_that_first_sighting(conn):
    seen = SINCE + timedelta(minutes=30)
    see(conn, CODE, seen)
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert repo.code_details_for(conn, [CODE])[CODE].clock_start == seen


def test_a_code_first_seen_before_the_marker_starts_fresh(conn):
    see(conn, CODE, SINCE - timedelta(days=20))
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert repo.code_details_for(conn, [CODE])[CODE].clock_start == NOW


def test_a_code_the_bot_has_not_released_starts_now(conn):
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert repo.code_details_for(conn, [CODE])[CODE].clock_start == NOW


def test_the_clock_does_not_move_when_the_code_is_released_later(conn):
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    see(conn, CODE, NOW + timedelta(minutes=5))
    repo.upsert_code_details(conn, [obs(reward="y")], now=NOW + timedelta(hours=1))
    assert repo.code_details_for(conn, [CODE])[CODE].clock_start == NOW


def test_a_missing_marker_reads_as_now_so_old_codes_get_a_fresh_clock(conn):
    repo.app_state_delete(conn, repo.DETAILS_SINCE_KEY)
    see(conn, CODE, NOW - timedelta(days=20))
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert repo.code_details_for(conn, [CODE])[CODE].clock_start == NOW


def test_the_migrations_own_marker_is_there_on_a_fresh_database(v3_db):
    with closing(connect(v3_db)) as c:
        marker = datetime.fromisoformat(repo.app_state_get(c, repo.DETAILS_SINCE_KEY))
    assert marker.tzinfo is not None and abs(datetime.now(UTC) - marker) < timedelta(minutes=5)


# --- bad input and no input ---


def test_an_observation_the_store_would_refuse_is_skipped_not_fatal(conn, caplog):
    too_long = obs(code=OTHER, reward="x" * 301)
    with caplog.at_level(logging.WARNING, logger="newsbot.store.repo"):
        changed = repo.upsert_code_details(conn, [too_long, obs(reward="fine")], now=NOW)
    assert changed == [CODE]
    assert set(repo.code_details_for(conn, [CODE, OTHER])) == {CODE}
    assert "skipped details" in caplog.text


def test_no_observations_is_a_quiet_no_op(conn):
    assert repo.upsert_code_details(conn, [], now=NOW) == []
    assert not conn.in_transaction


def test_a_failed_upsert_rolls_back_and_leaves_the_connection_usable(conn):
    # A schema that has gone wrong shouldn't leave a lock held either.
    conn.execute("DROP TABLE code_details")
    conn.execute("CREATE TABLE code_details (code TEXT PRIMARY KEY)")
    with pytest.raises(Exception, match="column"):
        repo.upsert_code_details(conn, [obs(reward="x")], now=NOW)
    assert not conn.in_transaction


# --- which table sources have listed ---


def test_table_listed_markers(conn):
    assert repo.table_listed_sources(conn, ["mentalmars", "other site"]) == set()
    repo.mark_table_listed(conn, "mentalmars", NOW)
    assert repo.table_listed_sources(conn, ["mentalmars", "other site"]) == {"mentalmars"}
    assert repo.table_listed_sources(conn, []) == set()


def test_the_first_listing_time_sticks(conn):
    repo.mark_table_listed(conn, "mentalmars", NOW)
    repo.mark_table_listed(conn, "mentalmars", NOW + timedelta(days=3))
    value = repo.app_state_get(conn, f"{repo.TABLE_LISTED_PREFIX}mentalmars")
    assert datetime.fromisoformat(value) == NOW


def test_source_names_are_bound_not_built_into_the_sql(conn):
    nasty = "x'); DROP TABLE code_details; --"
    repo.mark_table_listed(conn, nasty, NOW)
    assert repo.table_listed_sources(conn, [nasty, "mentalmars"]) == {nasty}
    assert conn.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 0


def test_a_name_is_not_confused_with_one_that_contains_it(conn):
    repo.mark_table_listed(conn, "mentalmars", NOW)
    assert repo.table_listed_sources(conn, ["mentalmars 2", "mental"]) == set()


# --- retention for guild_alert_messages ---


def _message(conn, guild_id, message_id, posted_at):
    with conn:
        conn.execute(
            "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
            "posted_at, rendered_at) VALUES (?, ?, 5, 'New SHiFT code!', ?, ?)",
            (guild_id, message_id, posted_at.isoformat(), posted_at.isoformat()),
        )


def test_alert_message_records_go_after_30_days_and_not_before(conn):
    repo.create_guild(conn, 42)
    _message(conn, 42, 1, NOW - timedelta(days=31))
    _message(conn, 42, 2, NOW - timedelta(days=29))
    _message(conn, 42, 3, NOW - timedelta(days=100))
    # The cutoff is the 90 day one the retention job passes; the 30 day rule is its own.
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: NOW)
    left = [r[0] for r in conn.execute("SELECT message_id FROM guild_alert_messages")]
    assert left == [2]


def test_the_retention_return_value_still_counts_summaries_and_notices(conn):
    repo.create_guild(conn, 42)
    _message(conn, 42, 1, NOW - timedelta(days=60))
    assert repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: NOW) == (
        0,
        0,
    )


def test_code_details_are_kept_forever(conn):
    repo.upsert_code_details(conn, [obs(reward="x")], now=NOW - timedelta(days=1000))
    repo.purge_guild_data_older_than(conn, NOW, now=lambda: NOW)
    repo.purge_older_than(conn, NOW)
    assert CODE in repo.code_details_for(conn, [CODE])


def test_a_garbled_marker_logs_once_and_reads_as_now(conn, caplog):
    see(conn, CODE, NOW - timedelta(days=20))
    repo.app_state_set(conn, repo.DETAILS_SINCE_KEY, "hand set")
    with caplog.at_level(logging.WARNING, logger="newsbot.store.repo"):
        repo.upsert_code_details(conn, [obs(reward="x"), obs(code=OTHER, reward="y")], now=NOW)
    assert repo.code_details_for(conn, [CODE])[CODE].clock_start == NOW
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_an_out_of_range_expiry_is_stored_as_unknown_and_the_reward_is_kept(conn):
    assert repo.upsert_code_details(
        conn, [obs(reward="A skin", expires_on=date(9999, 12, 31))], now=NOW
    ) == [CODE]
    got = one(conn)
    assert (got.reward, got.expires_on) == ("A skin", None)


def test_a_batch_that_flips_a_value_and_back_changes_nothing(conn):
    repo.upsert_code_details(conn, [obs(name="B", expires_on=date(2026, 10, 20))], now=NOW)
    later = NOW + timedelta(hours=1)
    assert (
        repo.upsert_code_details(
            conn,
            [
                obs(name="A", expires_on=date(2026, 10, 14), published_at=later - timedelta(1)),
                obs(name="B", expires_on=date(2026, 10, 20), published_at=later),
            ],
            now=later,
        )
        == []
    )
    assert one(conn).details_changed_at == NOW


def test_the_same_now_with_a_real_change_is_still_reported(conn):
    assert repo.upsert_code_details(conn, [obs(reward="one")], now=NOW) == [CODE]
    assert repo.upsert_code_details(conn, [obs(reward="two")], now=NOW) == [CODE]


def test_a_named_zone_clock_gets_the_same_30_day_line_as_utc(conn):
    from zoneinfo import ZoneInfo

    repo.create_guild(conn, 42)
    edge = NOW - timedelta(days=30)
    _message(conn, 42, 1, edge - timedelta(seconds=1))
    _message(conn, 42, 2, edge + timedelta(seconds=1))
    pacific = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    repo.purge_guild_data_older_than(conn, NOW - timedelta(days=90), now=lambda: pacific)
    assert [r[0] for r in conn.execute("SELECT message_id FROM guild_alert_messages")] == [2]
