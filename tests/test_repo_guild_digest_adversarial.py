"""Adversarial tests for the per-guild digest's database functions (plan task 6).

The claim is the bouncer: `BEGIN IMMEDIATE`, then look, then write, so two
claimers can't both see an empty table. Everything else here is either a read
that must stay inside its own guild, a timestamp comparison that happens as
text (so a format slip is a wrong answer and not an error), or a write that
must not quietly undo somebody else's. Real SQLite, real migrations, real
threads with their own connections, because "two processes" on one database
file is exactly two connections and nothing cleverer.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem

T0 = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
DAY = date(2026, 9, 30)
WINDOW = (T0 - timedelta(hours=24), T0)


def clock(moment: datetime = T0):
    return lambda: moment


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def conn(db_path):
    with closing(connect(db_path)) as c:
        yield c


def make_guild(conn, guild_id, games=(("borderlands4", 1),), **kwargs):
    repo.create_guild(conn, guild_id, set_up=True, now=clock(), **kwargs)
    repo.set_guild_games(conn, guild_id, [(k, c + guild_id * 100) for k, c in games])


def claim(conn, guild_id=1, run_date=DAY, **kwargs):
    kwargs.setdefault("force", False)
    kwargs.setdefault("window", WINDOW)
    kwargs.setdefault("now", clock())
    return repo.claim_guild_digest(conn, guild_id, run_date, **kwargs)


def put_item(conn, title, collected, topics, *, url=None):
    repo.store_items(
        conn,
        [
            StoredItem(
                url=url or f"https://example.com/{title}",
                title=title,
                excerpt="",
                source_name="Feed",
                trust="press",
                published_at=None,
                topics=topics,
            )
        ],
        now=clock(collected),
    )


def raw_row(conn, guild_id=1, run_date=DAY):
    return dict(
        conn.execute(
            "SELECT * FROM digests WHERE guild_id = ? AND run_date = ?",
            (guild_id, run_date.isoformat()),
        ).fetchone()
    )


# --- the claim ---


def test_eight_threads_racing_the_first_claim_produce_exactly_one_winner(db_path):
    with closing(connect(db_path)) as c:
        make_guild(c, 1)
    barrier = threading.Barrier(8)
    results: list[object] = []

    def racer():
        with closing(connect(db_path)) as c:
            barrier.wait()
            results.append(claim(c))

    threads = [threading.Thread(target=racer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    winners = [r for r in results if r is not None]
    assert len(results) == 8 and len(winners) == 1
    with closing(connect(db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
        assert raw_row(c)["attempts"] == 1


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_a_refused_claim_leaves_the_row_exactly_as_it_was(conn, status):
    make_guild(conn, 1)
    first = claim(conn)
    repo.save_guild_digest(
        conn, first.digest_id, status, {"borderlands4": 5}, "n", WINDOW, now=clock()
    )
    before = raw_row(conn)
    for kwargs in ({}, {"resume": True}, {"window": (T0, T0 + timedelta(days=1))}):
        assert claim(conn, now=clock(T0 + timedelta(hours=3)), **kwargs) is None
    assert raw_row(conn) == before


def test_a_pending_row_refused_without_resume_is_untouched_too(conn):
    make_guild(conn, 1)
    claim(conn)
    before = raw_row(conn)
    assert claim(conn, now=clock(T0 + timedelta(hours=1))) is None
    assert raw_row(conn) == before


def test_a_v22_failed_row_with_only_flat_ids_and_no_window_is_reclaimed_fresh(conn):
    # What an adopted v2.2 failure looks like: message ids in the flat list, nothing
    # per game, no window. A claim has no way to know which game each id belonged to, so
    # it starts over (the due check never auto-retries it; an admin's run-now asks first).
    make_guild(conn, 1)
    conn.execute(
        "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
        "updated_at) VALUES (1, ?, 'failed', '[131]', 't', 't')",
        (DAY.isoformat(),),
    )
    conn.commit()
    got = claim(conn)
    assert got is not None and got.resumed is False and got.posted_by_game == {}
    assert (got.window_start, got.window_end) == WINDOW


def test_attempts_count_every_successful_claim_across_branches(conn):
    make_guild(conn, 1)
    first = claim(conn)
    claim(conn, resume=True)  # resume a pending row
    repo.mark_guild_digest_failed(conn, first.digest_id, "x", {}, now=clock())
    claim(conn)  # reclaim a clean failure
    claim(conn, force=True)  # forced replace
    assert raw_row(conn)["attempts"] == 4


def test_windows_are_stored_in_utc_whatever_zone_they_arrive_in(conn):
    make_guild(conn, 1)
    pdt = timezone(timedelta(hours=-7))
    claim(conn, window=(WINDOW[0].astimezone(pdt), WINDOW[1].astimezone(pdt)))
    row = raw_row(conn)
    assert row["window_start"] == WINDOW[0].isoformat()
    assert row["window_end"] == WINDOW[1].isoformat()
    assert repo.get_guild_digest(conn, 1, DAY).window_end == T0


def test_a_naive_window_is_taken_as_utc(conn):
    make_guild(conn, 1)
    claim(conn, window=(WINDOW[0].replace(tzinfo=None), WINDOW[1].replace(tzinfo=None)))
    assert repo.get_guild_digest(conn, 1, DAY).window_end == T0


def test_a_claim_that_blows_up_leaves_no_half_row_and_no_lock_behind(conn, db_path):
    make_guild(conn, 1)
    with pytest.raises(AttributeError):
        claim(conn, window=("not", "datetimes"))  # _utc_iso chokes on the bad window
    with closing(connect(db_path)) as other:
        assert claim(other) is not None  # no lock left behind, no half row
    assert not conn.in_transaction


def test_the_unique_index_is_per_guild_and_day(conn):
    make_guild(conn, 1)
    make_guild(conn, 2)
    assert claim(conn, guild_id=1) and claim(conn, guild_id=2)
    assert claim(conn, guild_id=1, run_date=DAY + timedelta(days=1))
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 3


# --- write-through and the final save ---


def test_recording_the_same_game_twice_keeps_one_entry_and_the_latest_id(conn):
    make_guild(conn, 1)
    got = claim(conn)
    repo.record_posted_game(conn, got.digest_id, "borderlands4", 10, now=clock())
    repo.record_posted_game(conn, got.digest_id, "palworld", 11, now=clock())
    repo.record_posted_game(conn, got.digest_id, "borderlands4", 12, now=clock())
    row = repo.get_guild_digest(conn, 1, DAY)
    assert row.posted_by_game == {"borderlands4": 12, "palworld": 11}
    assert row.posted_message_ids == [12, 11]


@pytest.mark.parametrize("key", ['a"b', "a'); DROP TABLE digests;--", "ключ", "x‮y", "{}", ""])
def test_hostile_game_keys_round_trip_through_the_json_column(conn, key):
    make_guild(conn, 1)
    got = claim(conn)
    repo.record_posted_game(conn, got.digest_id, key, 2**53, now=clock())
    row = repo.get_guild_digest(conn, 1, DAY)
    assert row.posted_by_game == {key: 2**53}
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1


def test_a_write_through_moves_updated_at_so_a_watchdog_could_see_life(conn):
    make_guild(conn, 1)
    got = claim(conn, now=clock(T0))
    repo.record_posted_game(
        conn, got.digest_id, "borderlands4", 1, now=clock(T0 + timedelta(minutes=2))
    )
    assert raw_row(conn)["updated_at"] == (T0 + timedelta(minutes=2)).isoformat()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "record_posted_game reads the JSON map and then writes it back without an "
        "exclusive transaction, so two writers on the same row lose each other's games. "
        "Latent today (one publisher writes its own row in sequence); it becomes a real "
        "lost update if two processes ever work the same digest."
    ),
)
def test_two_writers_recording_different_games_do_not_lose_each_other(db_path):
    with closing(connect(db_path)) as c:
        make_guild(c, 1)
        digest_id = claim(c).digest_id

    def writer(prefix: str):
        with closing(connect(db_path)) as c:
            for i in range(200):
                repo.record_posted_game(c, digest_id, f"{prefix}{i}", i + 1, now=clock())

    threads = [threading.Thread(target=writer, args=(p,)) for p in "abc"]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with closing(connect(db_path)) as c:
        assert len(repo.get_guild_digest(c, 1, DAY).posted_by_game) == 600


def test_mark_failed_with_nothing_new_keeps_everything_already_written_through(conn):
    make_guild(conn, 1)
    got = claim(conn)
    repo.record_posted_game(conn, got.digest_id, "borderlands4", 7, now=clock())
    for passed in (None, {}):
        repo.mark_guild_digest_failed(conn, got.digest_id, "boom", passed, now=clock())
    row = repo.get_guild_digest(conn, 1, DAY)
    assert row.status == "failed" and row.posted_by_game == {"borderlands4": 7}
    assert row.posted_message_ids == [7]


def test_the_write_functions_only_touch_the_row_they_were_given(conn):
    make_guild(conn, 1)
    make_guild(conn, 2)
    mine = claim(conn, guild_id=1)
    theirs = claim(conn, guild_id=2)
    before = raw_row(conn, 2)
    repo.record_posted_game(conn, mine.digest_id, "borderlands4", 1, now=clock())
    repo.save_guild_digest(
        conn, mine.digest_id, "ok", {"borderlands4": 1}, None, WINDOW, now=clock()
    )
    repo.mark_guild_digest_failed(conn, mine.digest_id, "x", {}, now=clock())
    assert raw_row(conn, 2) == before and theirs.digest_id != mine.digest_id


# --- last_window_end ---


def test_last_window_end_takes_the_latest_end_of_qualifying_rows_only(conn):
    make_guild(conn, 1)
    for offset, status, posted in [
        (5, "ok", {"borderlands4": 1}),
        (4, "partial", {}),
        (3, "failed", {}),  # clean failure: doesn't count
        (2, "pending", {}),  # nobody posted: doesn't count
        (1, "failed", {"borderlands4": 2}),  # a failure that got something out counts
    ]:
        day = DAY - timedelta(days=offset)
        got = claim(
            conn,
            run_date=day,
            window=(T0 - timedelta(days=offset + 1), T0 - timedelta(days=offset)),
        )
        repo.save_guild_digest(
            conn,
            got.digest_id,
            status,
            posted,
            None,
            (T0 - timedelta(days=offset + 1), T0 - timedelta(days=offset)),
            now=clock(),
        )
    assert repo.last_window_end(conn, 1, exclude_run_date=DAY) == T0 - timedelta(days=1)
    # skipping the newest by excluding its date falls back to the next qualifying one
    assert repo.last_window_end(
        conn, 1, exclude_run_date=DAY - timedelta(days=1)
    ) == T0 - timedelta(days=4)


def test_last_window_end_is_none_for_a_guild_with_no_history_and_never_another_guilds(conn):
    make_guild(conn, 1)
    make_guild(conn, 2)
    other = claim(conn, guild_id=2, run_date=DAY - timedelta(days=1))
    repo.save_guild_digest(
        conn, other.digest_id, "ok", {"borderlands4": 1}, None, WINDOW, now=clock()
    )
    assert repo.last_window_end(conn, 1, exclude_run_date=DAY) is None
    assert repo.last_window_end(conn, 999, exclude_run_date=DAY) is None


# --- due_candidates ---


def test_due_candidates_picks_the_newest_row_by_date_not_by_insertion_order(conn):
    make_guild(conn, 1)
    newer = claim(conn, run_date=DAY)
    repo.save_guild_digest(
        conn, newer.digest_id, "ok", {"borderlands4": 1}, None, WINDOW, now=clock()
    )
    claim(conn, run_date=DAY - timedelta(days=3))  # inserted later, dated earlier
    [candidate] = repo.due_candidates(conn)
    assert (candidate.run_date, candidate.status) == (DAY, "ok")


def test_due_candidates_survives_an_unreadable_timestamp_in_one_guild(conn):
    make_guild(conn, 1)
    make_guild(conn, 2)
    claim(conn, guild_id=1)
    claim(conn, guild_id=2)
    conn.execute(
        "UPDATE digests SET updated_at = 'yesterday-ish', window_end = 'soon' WHERE guild_id = 1"
    )
    conn.commit()
    by_id = {c.guild_id: c for c in repo.due_candidates(conn)}
    assert by_id[1].updated_at is None and by_id[1].window_end is None
    assert by_id[2].updated_at == T0 and by_id[2].window_end == T0


def test_due_candidates_ignores_orphan_v22_rows_with_no_guild(conn):
    make_guild(conn, 1)
    conn.execute(
        "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
        "VALUES (NULL, ?, 'ok', 't', 't')",
        (DAY.isoformat(),),
    )
    conn.commit()
    [candidate] = repo.due_candidates(conn)
    assert candidate.run_date is None and candidate.status is None


def test_thousands_of_guilds_come_back_from_one_query_quickly(conn):
    games = (("borderlands4", 1),)
    with conn:
        for gid in range(1, 3001):
            conn.execute(
                "INSERT INTO guilds (guild_id, digest_time, timezone, tier, set_up, joined_at, "
                "updated_at) VALUES (?, '09:00', 'America/Los_Angeles', 'free', 1, 't', 't')",
                (gid,),
            )
            conn.execute(
                "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)",
                (gid, games[0][0], gid),
            )
            for d in range(3):
                conn.execute(
                    "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
                    "VALUES (?, ?, 'ok', 't', 't')",
                    (gid, (DAY - timedelta(days=d)).isoformat()),
                )
    started = time.perf_counter()
    candidates = repo.due_candidates(conn)
    assert time.perf_counter() - started < 3.0
    assert [c.guild_id for c in candidates] == list(range(1, 3001))
    assert {c.run_date for c in candidates} == {DAY}


# --- items_for_window ---


def test_items_for_window_with_naive_or_foreign_zone_bounds_matches_utc_bounds(conn):
    make_guild(conn, 1)
    put_item(conn, "inside", T0 - timedelta(hours=1), {"borderlands4": False})
    put_item(conn, "outside", T0 + timedelta(hours=1), {"borderlands4": False})
    pdt = timezone(timedelta(hours=-7))
    want = repo.items_for_window(conn, 1, *WINDOW)
    assert [i.title for i in want["borderlands4"]] == ["inside"]
    assert (
        repo.items_for_window(conn, 1, WINDOW[0].astimezone(pdt), WINDOW[1].astimezone(pdt)) == want
    )
    assert (
        repo.items_for_window(
            conn, 1, WINDOW[0].replace(tzinfo=None), WINDOW[1].replace(tzinfo=None)
        )
        == want
    )


def test_an_empty_or_backwards_window_returns_nothing(conn):
    make_guild(conn, 1)
    put_item(conn, "x", T0 - timedelta(hours=1), {"borderlands4": False})
    assert repo.items_for_window(conn, 1, T0, T0) == {}
    assert repo.items_for_window(conn, 1, T0, T0 - timedelta(days=1)) == {}


def test_an_item_matching_two_followed_games_appears_under_both_with_its_own_uncertainty(conn):
    make_guild(conn, 1, games=(("borderlands4", 1), ("palworld", 2)))
    put_item(conn, "both", T0 - timedelta(hours=1), {"borderlands4": False, "palworld": True})
    got = repo.items_for_window(conn, 1, *WINDOW)
    assert got["borderlands4"][0].uncertain is False and got["palworld"][0].uncertain is True
    assert got["borderlands4"][0].url == got["palworld"][0].url


def test_a_guild_following_nothing_sees_nothing_and_unfollowed_games_never_leak(conn):
    make_guild(conn, 1, games=())
    make_guild(conn, 2, games=(("palworld", 1),))
    put_item(conn, "p", T0 - timedelta(hours=1), {"palworld": False})
    put_item(conn, "b", T0 - timedelta(hours=1), {"borderlands4": False})
    assert repo.items_for_window(conn, 1, *WINDOW) == {}
    assert list(repo.items_for_window(conn, 2, *WINDOW)) == ["palworld"]
    assert repo.items_for_window(conn, 424242, *WINDOW) == {}


def test_items_come_back_newest_first_with_ties_broken_by_id_descending(conn):
    make_guild(conn, 1)
    for n in range(3):
        put_item(conn, f"t{n}", T0 - timedelta(hours=1), {"borderlands4": False})
    put_item(conn, "older", T0 - timedelta(hours=2), {"borderlands4": False})
    titles = [i.title for i in repo.items_for_window(conn, 1, *WINDOW)["borderlands4"]]
    assert titles == ["t2", "t1", "t0", "older"]


def test_a_window_over_thousands_of_items_is_fast_and_exact(conn):
    make_guild(conn, 1)
    with conn:
        for n in range(6000):
            conn.execute(
                "INSERT INTO items (url, title, excerpt, source_name, trust, published_at, "
                "collected_at) VALUES (?, ?, '', 'Feed', 'press', NULL, ?)",
                (f"https://example.com/{n}", f"t{n}", (T0 - timedelta(minutes=n)).isoformat()),
            )
            conn.execute(
                "INSERT INTO item_topics (item_id, topic_key, uncertain) "
                "VALUES (?, 'borderlands4', 0)",
                (n + 1,),
            )
    started = time.perf_counter()
    got = repo.items_for_window(conn, 1, T0 - timedelta(minutes=1440), T0)["borderlands4"]
    assert time.perf_counter() - started < 2.0
    assert len(got) == 1440  # minutes 0..1439 back from T0: start exclusive, end inclusive


# --- stored summaries ---


_summary_days = iter(range(1, 10_000))


def _summary(conn, game, end, *, status="ok"):
    run_date = (date(2020, 1, 1) + timedelta(days=next(_summary_days))).isoformat()
    cur = conn.execute(
        "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
        "coverage_notes, created_at) VALUES (?, ?, ?, ?, ?, '[]', 't')",
        (game, run_date, status, (end - timedelta(hours=24)).isoformat(), end.isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def test_the_summary_reuse_window_is_exclusive_below_inclusive_above_and_per_game(conn):
    _summary(conn, "palworld", T0)  # someone else's game never matches
    assert repo.get_game_summary(conn, "borderlands4", T0) is None
    exactly_low = _summary(conn, "borderlands4", T0 - timedelta(hours=24))
    assert repo.get_game_summary(conn, "borderlands4", T0) is None  # due - 24h is excluded
    just_inside = _summary(
        conn, "borderlands4", T0 - timedelta(hours=24) + timedelta(microseconds=1)
    )
    assert repo.get_game_summary(conn, "borderlands4", T0).id == just_inside
    top = _summary(conn, "borderlands4", T0 + timedelta(minutes=30))
    assert repo.get_game_summary(conn, "borderlands4", T0).id == top  # due + 30m is included
    _summary(conn, "borderlands4", T0 + timedelta(minutes=30, microseconds=1))
    assert repo.get_game_summary(conn, "borderlands4", T0).id == top
    assert exactly_low != just_inside


def test_two_summaries_ending_at_the_same_instant_resolve_to_the_newest_row(conn):
    _summary(conn, "borderlands4", T0 - timedelta(hours=1), status="fallback")
    newer = _summary(conn, "borderlands4", T0 - timedelta(hours=1))
    assert repo.get_game_summary(conn, "borderlands4", T0).id == newer


def test_a_summary_with_no_stories_has_an_empty_story_list(conn):
    assert repo.summary_stories(conn, _summary(conn, "borderlands4", T0)) == []
    assert repo.summary_stories(conn, 424242) == []


# --- every function that takes a guild id stays inside it ---


def test_two_guilds_with_identical_days_and_games_never_see_each_others_digest_state(conn):
    for gid in (1, 2):
        make_guild(conn, gid)
    mine = claim(conn, guild_id=1)
    repo.save_guild_digest(
        conn, mine.digest_id, "ok", {"borderlands4": 1}, "mine", WINDOW, now=clock()
    )
    assert repo.get_guild_digest(conn, 2, DAY) is None
    assert repo.last_window_end(conn, 2, exclude_run_date=DAY - timedelta(days=9)) is None
    by_id = {c.guild_id: c for c in repo.due_candidates(conn)}
    assert by_id[1].status == "ok" and by_id[2].status is None
    assert claim(conn, guild_id=2) is not None  # its own day is still open


def test_json_columns_stay_valid_after_every_writer(conn):
    make_guild(conn, 1)
    got = claim(conn)
    repo.record_posted_game(conn, got.digest_id, "borderlands4", 1, now=clock())
    repo.mark_guild_digest_failed(conn, got.digest_id, "x", {"palworld": 2}, now=clock())
    repo.save_guild_digest(
        conn, got.digest_id, "ok", {"borderlands4": 1, "palworld": 2}, None, WINDOW, now=clock()
    )
    row = raw_row(conn)
    assert json.loads(row["posted_by_game"]) == {"borderlands4": 1, "palworld": 2}
    assert json.loads(row["posted_message_ids"]) == [1, 2]
