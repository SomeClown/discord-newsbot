"""The per-guild digest guard and the reads the digest leans on (plan task 6).

Real SQLite with the real migrations. The interesting cases are the claim's
branches (v2's meanings, per server, plus the D7 resume) and the two time
boundaries that decide which items a digest sees.
"""

from __future__ import annotations

import itertools
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import Coverage, StoredItem

T0 = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
DAY = date(2026, 9, 30)
WINDOW = (T0 - timedelta(hours=24), T0)


def clock(moment: datetime = T0):
    return lambda: moment


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "t.db")) as c:
        migrate(c)
        yield c


@pytest.fixture
def guilds(conn):
    for guild_id in (1, 2):
        repo.create_guild(conn, guild_id, set_up=True, now=clock())
        repo.set_guild_games(conn, guild_id, [("borderlands4", 11 * guild_id)])
    return conn


def claim(conn, guild_id=1, run_date=DAY, **kwargs):
    kwargs.setdefault("force", False)
    kwargs.setdefault("window", WINDOW)
    kwargs.setdefault("now", clock())
    return repo.claim_guild_digest(conn, guild_id, run_date, **kwargs)


def row(conn, guild_id=1, run_date=DAY):
    return repo.get_guild_digest(conn, guild_id, run_date)


# --- claim_guild_digest ---


def test_first_claim_inserts_a_pending_row_with_the_window(guilds):
    got = claim(guilds)
    assert got.posted_by_game == {} and got.resumed is False
    assert (got.window_start, got.window_end) == WINDOW
    r = row(guilds)
    assert (r.status, r.attempts, r.window_start, r.window_end) == ("pending", 1, *WINDOW)
    assert r.posted_by_game == {} and r.posted_message_ids == []


def test_a_second_claim_the_same_day_is_refused_in_every_state(guilds):
    first = claim(guilds)
    assert claim(guilds) is None  # pending
    assert claim(guilds, resume=False) is None
    repo.save_guild_digest(guilds, first.digest_id, "ok", {"borderlands4": 5}, None, WINDOW)
    assert claim(guilds) is None and claim(guilds, resume=True) is None  # ok
    repo.save_guild_digest(guilds, first.digest_id, "partial", {}, "x", WINDOW)
    assert claim(guilds) is None  # partial


def test_get_guild_digest_only_returns_that_guilds_row(guilds):
    claim(guilds, 1)
    assert row(guilds, 2) is None and row(guilds, 1).guild_id == 1
    claim(guilds, 2)
    assert row(guilds, 2).guild_id == 2 and row(guilds, 1).guild_id == 1


def test_guilds_do_not_share_a_day(guilds):
    assert claim(guilds, 1) is not None
    assert claim(guilds, 2) is not None
    assert claim(guilds, 1, date(2026, 10, 1)) is not None


def test_a_pending_row_resumes_with_its_window_and_what_it_posted(guilds):
    first = claim(guilds)
    # The write-through's clock is pinned: it refreshes the row's lease, and the resume
    # below is only allowed once that lease has gone stale.
    repo.record_posted_game(guilds, first.digest_id, "borderlands4", 555, now=clock())
    later_window = (T0, T0 + timedelta(hours=3))
    got = claim(guilds, resume=True, window=later_window, now=clock(T0 + timedelta(hours=1)))
    assert got.digest_id == first.digest_id and got.resumed is True
    assert got.posted_by_game == {"borderlands4": 555}
    assert (got.window_start, got.window_end) == WINDOW  # the row's own, not the new one
    r = row(guilds)
    assert (r.status, r.attempts) == ("pending", 2)
    assert r.posted_by_game == {"borderlands4": 555}


def test_a_pending_row_without_a_window_is_a_v22_row_and_never_resumes(guilds):
    guilds.execute(
        "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
        "VALUES (1, ?, 'pending', 't', 't')",
        (DAY.isoformat(),),
    )
    guilds.commit()
    assert claim(guilds, resume=True) is None
    assert claim(guilds, force=True) is not None  # an admin's confirmed run-now still can


def test_a_clean_failure_is_reclaimed_with_a_fresh_window(guilds):
    first = claim(guilds)
    repo.mark_guild_digest_failed(guilds, first.digest_id, "boom")
    later = (T0, T0 + timedelta(hours=1))
    got = claim(guilds, window=later)
    assert got.digest_id == first.digest_id and got.resumed is False
    assert (got.window_start, got.window_end) == later
    assert row(guilds).attempts == 2


def test_a_failure_that_posted_continues_from_where_it_stopped(guilds):
    first = claim(guilds)
    repo.record_posted_game(guilds, first.digest_id, "borderlands4", 555)
    repo.mark_guild_digest_failed(guilds, first.digest_id, "boom")
    got = claim(guilds, window=(T0, T0 + timedelta(hours=1)))
    assert got.resumed is True and got.posted_by_game == {"borderlands4": 555}
    assert (got.window_start, got.window_end) == WINDOW


def test_force_replaces_the_row_in_place_and_forgets_what_posted(guilds):
    first = claim(guilds)
    repo.record_posted_game(guilds, first.digest_id, "borderlands4", 555)
    repo.save_guild_digest(guilds, first.digest_id, "ok", {"borderlands4": 555}, None, WINDOW)
    new_window = (T0, T0 + timedelta(hours=2))
    got = claim(guilds, force=True, window=new_window)
    assert got.digest_id == first.digest_id and got.posted_by_game == {}
    r = row(guilds)
    assert (r.status, r.attempts, r.posted_by_game) == ("pending", 2, {})
    assert (r.window_start, r.window_end) == new_window
    assert guilds.execute("SELECT COUNT(*) FROM digests WHERE guild_id = 1").fetchone()[0] == 1


def test_force_replaces_a_pending_row_too(guilds):
    first = claim(guilds)
    assert claim(guilds, force=True).digest_id == first.digest_id


# --- write-through, save, fail ---


def test_record_posted_game_writes_the_map_and_the_flat_list(guilds):
    got = claim(guilds)
    repo.record_posted_game(guilds, got.digest_id, "borderlands4", 555)
    repo.record_posted_game(guilds, got.digest_id, "palworld", 556)
    r = row(guilds)
    assert r.posted_by_game == {"borderlands4": 555, "palworld": 556}
    assert r.posted_message_ids == [555, 556]


def test_record_posted_game_for_a_missing_digest_raises(conn):
    with pytest.raises(repo.StoreError):
        repo.record_posted_game(conn, 999, "borderlands4", 1)


def test_save_writes_status_posted_notes_and_window(guilds):
    got = claim(guilds)
    repo.save_guild_digest(
        guilds, got.digest_id, "partial", {"a": 1, "b": 2}, "b: skipped", (T0, T0 + timedelta(1))
    )
    r = row(guilds)
    assert (r.status, r.posted_by_game, r.posted_message_ids) == (
        "partial",
        {"a": 1, "b": 2},
        [1, 2],
    )
    assert r.error_notes == "b: skipped" and r.window_end == T0 + timedelta(1)


def test_mark_failed_merges_posted_and_never_drops_any(guilds):
    got = claim(guilds)
    repo.record_posted_game(guilds, got.digest_id, "a", 1)
    repo.mark_guild_digest_failed(guilds, got.digest_id, "boom", {"b": 2})
    r = row(guilds)
    assert (r.status, r.error_notes, r.posted_by_game) == ("failed", "boom", {"a": 1, "b": 2})
    assert r.posted_message_ids == [1, 2]


# --- last_window_end ---


def test_last_window_end_counts_only_digests_that_posted_and_skips_its_own_day(guilds):
    d = lambda n: DAY - timedelta(days=n)  # noqa: E731
    w = lambda n: (T0 - timedelta(days=n + 1), T0 - timedelta(days=n))  # noqa: E731
    ok = claim(guilds, run_date=d(3), window=w(3))
    repo.save_guild_digest(guilds, ok.digest_id, "ok", {}, None, w(3))
    failed_clean = claim(guilds, run_date=d(2), window=w(2))
    repo.mark_guild_digest_failed(guilds, failed_clean.digest_id, "boom")
    failed_posted = claim(guilds, run_date=d(1), window=w(1))
    repo.record_posted_game(guilds, failed_posted.digest_id, "a", 1)
    repo.mark_guild_digest_failed(guilds, failed_posted.digest_id, "boom")
    claim(guilds, run_date=DAY)  # today's own pending row
    assert repo.last_window_end(guilds, 1, exclude_run_date=DAY) == w(1)[1]
    assert repo.last_window_end(guilds, 1, exclude_run_date=d(1)) == w(3)[1]  # 2 was clean
    assert repo.last_window_end(guilds, 2, exclude_run_date=DAY) is None


def test_last_window_end_ignores_v22_rows_with_no_window(guilds):
    guilds.execute(
        "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
        "VALUES (1, '2026-09-29', 'ok', 't', 't')"
    )
    guilds.commit()
    assert repo.last_window_end(guilds, 1, exclude_run_date=DAY) is None


# --- due_candidates ---


def test_due_candidates_only_set_up_guilds_with_games_and_their_newest_row(conn):
    repo.create_guild(conn, 1, set_up=True, timezone="Europe/London", now=clock())
    repo.set_guild_games(conn, 1, [("a", 11)])
    repo.create_guild(conn, 2, set_up=False, now=clock())  # not set up
    repo.set_guild_games(conn, 2, [("a", 22)])
    repo.create_guild(conn, 3, set_up=True, now=clock())  # follows nothing
    repo.create_guild(conn, 4, set_up=True, tier="comped", now=clock())
    repo.set_guild_games(conn, 4, [("a", 44)])
    old = claim(conn, 4, date(2026, 9, 29), window=(T0 - timedelta(2), T0 - timedelta(1)))
    repo.save_guild_digest(conn, old.digest_id, "ok", {"a": 1}, None, WINDOW)
    new = claim(conn, 4, DAY)
    repo.record_posted_game(conn, new.digest_id, "a", 2)

    got = {c.guild_id: c for c in repo.due_candidates(conn)}
    assert set(got) == {1, 4}
    assert (got[1].timezone, got[1].run_date, got[1].status, got[1].posted_any) == (
        "Europe/London",
        None,
        None,
        False,
    )
    assert (got[4].tier, got[4].run_date, got[4].status) == ("comped", DAY, "pending")
    assert got[4].posted_any is True and got[4].attempts == 1 and got[4].window_end == T0


def test_due_candidates_sees_a_v22_flat_id_list_as_posted(guilds):
    guilds.execute(
        "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
        "updated_at) VALUES (1, ?, 'failed', '[131]', 't', 't')",
        (DAY.isoformat(),),
    )
    guilds.commit()
    got = {c.guild_id: c for c in repo.due_candidates(guilds)}
    assert got[1].posted_any is True


# --- items_for_window ---


def _store(conn, url, game, collected, *, trust="press", uncertain=False, title=None):
    repo.store_items(
        conn,
        [
            StoredItem(
                url=url,
                title=title or url,
                excerpt="",
                source_name="Feed",
                trust=trust,
                published_at=None,
                topics={game: uncertain},
            )
        ],
        now=clock(collected),
    )


def test_items_for_window_start_is_exclusive_end_is_inclusive(guilds):
    start, end = WINDOW
    _store(guilds, "https://x.example/at-start", "borderlands4", start)
    _store(guilds, "https://x.example/just-in", "borderlands4", start + timedelta(seconds=1))
    _store(guilds, "https://x.example/at-end", "borderlands4", end)
    _store(guilds, "https://x.example/after", "borderlands4", end + timedelta(seconds=1))
    got = repo.items_for_window(guilds, 1, start, end)
    assert [i.url for i in got["borderlands4"]] == [
        "https://x.example/at-end",
        "https://x.example/just-in",
    ]


def test_items_for_window_only_returns_games_that_guild_follows(guilds):
    mid = T0 - timedelta(hours=3)
    _store(guilds, "https://x.example/bl4", "borderlands4", mid)
    _store(guilds, "https://x.example/rust", "rust", mid)
    both = StoredItem(
        "https://x.example/both", "t", "", "Feed", "press", None, {"rust": 0, "borderlands4": 1}
    )
    repo.store_items(guilds, [both], now=clock(mid))
    got = repo.items_for_window(guilds, 1, *WINDOW)
    assert set(got) == {"borderlands4"}
    assert {i.url: i.uncertain for i in got["borderlands4"]} == {
        "https://x.example/bl4": False,
        "https://x.example/both": True,
    }


# --- get_game_summary / summary_stories ---


_SUMMARY_DAYS = itertools.count(1)


def _summary(conn, game, window_end, *, status="ok", notes=("n1",), note=None, items_after=5):
    cur = conn.execute(
        "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
        "coverage_notes, note, created_at, items_after, items_upto) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 't', ?, 9)",
        (
            game,
            f"2026-01-{next(_SUMMARY_DAYS):02d}",  # only has to be unique per game
            status,
            (window_end - timedelta(hours=24)).isoformat(),
            window_end.isoformat(),
            json.dumps(list(notes)),
            note,
            items_after,
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_get_game_summary_reuse_rule_boundaries(conn):
    # Was a (due - 24h, due + 30min] window. Now: ended strictly after `after`, and at or
    # after due - 6h, with no upper bound.
    due = T0
    _summary(conn, "g", due - repo.SUMMARY_MAX_AGE - timedelta(seconds=1))  # too old
    assert repo.get_game_summary(conn, "g", due) is None
    edge = _summary(conn, "g", due - repo.SUMMARY_MAX_AGE)  # exactly six hours: in
    assert repo.get_game_summary(conn, "g", due).id == edge
    newer = _summary(conn, "g", due + timedelta(hours=3))  # after the due time is fine
    got = repo.get_game_summary(conn, "g", due)
    assert got.id == newer and got.coverage_notes == ["n1"] and got.status == "ok"
    # `after` is exclusive: a summary ending exactly at the last digest's window end is a repeat.
    # (And it has to start at item 5, where this server's coverage left off.)
    assert (
        repo.get_game_summary(conn, "g", due, after=Coverage(due + timedelta(hours=3), 5)) is None
    )
    earlier = due + timedelta(hours=3) - timedelta(seconds=1)
    assert repo.get_game_summary(conn, "g", due, after=Coverage(earlier, 5)).id == newer
    assert repo.get_game_summary(conn, "g", due, after=Coverage(earlier, 4)) is None
    assert repo.get_game_summary(conn, "g", due, after=Coverage(earlier, 6)) is None
    assert repo.get_game_summary(conn, "other", due) is None


def test_summary_stories_returns_its_own_stories_with_urls_in_order(conn):
    mid = T0 - timedelta(hours=2)
    _store(conn, "https://x.example/a", "g", mid)
    _store(conn, "https://x.example/b", "g", mid)
    sid, other = _summary(conn, "g", T0), _summary(conn, "h", T0)
    for summary_id, headline in ((sid, "First"), (sid, "Second"), (other, "Elsewhere")):
        cur = conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, summary_id, created_at) "
            "VALUES ('g', ?, 's', 'official', ?, ?)",
            (headline, summary_id, mid.isoformat()),
        )
        conn.execute(
            "INSERT INTO story_items (story_id, item_id) SELECT ?, id FROM items", (cur.lastrowid,)
        )
    conn.commit()
    got = repo.summary_stories(conn, sid)
    assert [s.headline for s in got] == ["First", "Second"]
    assert got[0].urls == ["https://x.example/a", "https://x.example/b"]
