"""Tests for the read path (`/news` commands and `/newsbot status`) in newsbot.store.repo."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, StoryToSave, Usage


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _save_story(
    conn, run_date, *, topic_key="palworld", headline, summary="A summary.", label="official", url
):
    """Save a story and backdate its timestamps to `run_date`.

    `save_run` always stamps `created_at`/`collected_at` with the real wall
    clock (there's no injected `now` in its signature -- see the plan), so
    tests that need stories to look like they landed on a particular day
    have to backdate them by hand afterward.
    """
    item = StoredItem(
        url=url,
        title=headline,
        excerpt=summary,
        source_name="Palworld Steam",
        trust="official",
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        topics={topic_key: False},
    )
    story = StoryToSave(
        topic_key=topic_key,
        headline=headline,
        summary=summary,
        label=label,
        item_urls=[url],
        update_of_story_id=None,
    )
    digest_id = repo.claim_digest(conn, run_date, force=False)
    repo.save_run(conn, digest_id, [item], [story], "ok", [], None, Usage(100, 50))

    stamp = datetime(run_date.year, run_date.month, run_date.day, 12, tzinfo=UTC).isoformat()
    with conn:
        conn.execute("UPDATE items SET collected_at = ? WHERE url = ?", (stamp, url))
        conn.execute(
            "UPDATE stories SET created_at = ? WHERE digest_id = ? AND headline = ?",
            (stamp, digest_id, headline),
        )
        conn.execute(
            "UPDATE digests SET created_at = ?, updated_at = ? WHERE id = ?",
            (stamp, stamp, digest_id),
        )
    return digest_id


# --- query_stories ---


def test_query_stories_paging_totals(conn):
    for i in range(15):
        _save_story(
            conn,
            date(2026, 9, 1) + timedelta(days=i),
            headline=f"Story {i}",
            url=f"https://example.com/{i}",
        )
    page, total = repo.query_stories(
        conn, ["palworld"], datetime(2026, 1, 1, tzinfo=UTC), None, limit=6, offset=0
    )
    assert total == 15
    assert len(page) == 6

    page2, total2 = repo.query_stories(
        conn, ["palworld"], datetime(2026, 1, 1, tzinfo=UTC), None, limit=6, offset=12
    )
    assert total2 == 15
    assert len(page2) == 3


def test_query_stories_topic_filter(conn):
    _save_story(conn, date(2026, 9, 1), topic_key="palworld", headline="P", url="https://e/p")
    _save_story(conn, date(2026, 9, 2), topic_key="diablo4", headline="D", url="https://e/d")

    page, total = repo.query_stories(
        conn, ["diablo4"], datetime(2026, 1, 1, tzinfo=UTC), None, limit=10, offset=0
    )
    assert total == 1
    assert page[0].headline == "D"


def test_query_stories_no_topic_filter_means_all(conn):
    _save_story(conn, date(2026, 9, 1), topic_key="palworld", headline="P", url="https://e/p")
    _save_story(conn, date(2026, 9, 2), topic_key="diablo4", headline="D", url="https://e/d")

    page, total = repo.query_stories(
        conn, [], datetime(2026, 1, 1, tzinfo=UTC), None, limit=10, offset=0
    )
    assert total == 2


def test_query_stories_label_filter(conn):
    _save_story(conn, date(2026, 9, 1), headline="Official", label="official", url="https://e/1")
    _save_story(conn, date(2026, 9, 2), headline="Rumor", label="rumor", url="https://e/2")

    page, total = repo.query_stories(
        conn, ["palworld"], datetime(2026, 1, 1, tzinfo=UTC), "rumor", limit=10, offset=0
    )
    assert total == 1
    assert page[0].headline == "Rumor"


def test_query_stories_days_filter(conn):
    _save_story(conn, date(2026, 1, 1), headline="Old", url="https://e/old")
    _save_story(conn, date(2026, 9, 20), headline="New", url="https://e/new")

    page, total = repo.query_stories(
        conn, ["palworld"], datetime(2026, 9, 1, tzinfo=UTC), None, limit=10, offset=0
    )
    assert total == 1
    assert page[0].headline == "New"


def test_query_stories_includes_urls(conn):
    _save_story(conn, date(2026, 9, 1), headline="P", url="https://e/p")
    page, _ = repo.query_stories(
        conn, ["palworld"], datetime(2026, 1, 1, tzinfo=UTC), None, limit=10, offset=0
    )
    assert page[0].urls == ["https://e/p"]


# --- search_stories / fts_escape ---


def test_search_stories_finds_match(conn):
    _save_story(conn, date(2026, 9, 1), headline="Big Patch Notes", url="https://e/1")
    _save_story(conn, date(2026, 9, 2), headline="Unrelated Story", url="https://e/2")

    page, total = repo.search_stories(conn, "patch", datetime(2026, 1, 1, tzinfo=UTC), 10, 0)
    assert total == 1
    assert page[0].headline == "Big Patch Notes"


def test_search_stories_ranks_by_bm25_then_recency(conn):
    _save_story(conn, date(2026, 9, 1), headline="Patch patch patch notes", url="https://e/1")
    _save_story(conn, date(2026, 9, 2), headline="A minor patch", url="https://e/2")

    page, _ = repo.search_stories(conn, "patch", datetime(2026, 1, 1, tzinfo=UTC), 10, 0)
    assert page[0].headline == "Patch patch patch notes"


@pytest.mark.parametrize("bad_query", ['foo" OR bar*', "NEAR(", "", "   ", '"""'])
def test_search_stories_never_raises_on_malformed_input(conn, bad_query):
    page, total = repo.search_stories(conn, bad_query, datetime(2026, 1, 1, tzinfo=UTC), 10, 0)
    assert page == []
    assert total == 0


def test_search_stories_empty_query_returns_nothing(conn):
    _save_story(conn, date(2026, 9, 1), headline="Something", url="https://e/1")
    page, total = repo.search_stories(conn, "", datetime(2026, 1, 1, tzinfo=UTC), 10, 0)
    assert page == []
    assert total == 0


def test_fts_escape_quotes_each_token():
    assert repo.fts_escape("hello world") == '"hello" "world"'


def test_fts_escape_escapes_embedded_quotes():
    assert repo.fts_escape('foo"bar') == '"foo""bar"'


def test_fts_escape_empty_string():
    assert repo.fts_escape("") == ""


# --- status_snapshot ---


def test_status_snapshot_last_digest_and_counts(conn):
    _save_story(conn, date(2026, 9, 23), headline="Today", url="https://e/today")

    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    snap = repo.status_snapshot(conn, now, datetime(2026, 9, 1, tzinfo=UTC), [])

    assert snap.last_digest is not None
    assert snap.last_digest.status == "ok"
    assert snap.items_last_24h == 1
    assert snap.stories_last_24h == 1


def test_status_snapshot_source_health(conn):
    repo.record_source_result(conn, "Blizzard News", datetime(2026, 9, 23, tzinfo=UTC), "timeout")
    snap = repo.status_snapshot(
        conn, datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), ["Blizzard News"]
    )
    assert len(snap.source_health) == 1
    assert snap.source_health[0].source_name == "Blizzard News"
    assert snap.source_health[0].consecutive_failures == 1
    assert snap.source_health[0].never_run is False


def test_status_snapshot_month_token_sums_respect_boundary(conn):
    _save_story(conn, date(2026, 8, 31), headline="Last month", url="https://e/aug")
    _save_story(conn, date(2026, 9, 5), headline="This month", url="https://e/sep")

    snap = repo.status_snapshot(
        conn, datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), []
    )
    # Each _save_story call uses Usage(100, 50); only the September one
    # should count toward the September snapshot.
    assert snap.month_input_tokens == 100
    assert snap.month_output_tokens == 50


def test_status_snapshot_omits_removed_source(conn):
    # A source that used to be configured (and recorded health) but has
    # since been dropped from config.yaml -- the IGN scenario -- shouldn't
    # show up just because it kept its row.
    repo.record_source_result(conn, "IGN", datetime(2026, 9, 23, tzinfo=UTC), "403")
    repo.record_source_result(conn, "Blizzard News", datetime(2026, 9, 23, tzinfo=UTC), None)

    snap = repo.status_snapshot(
        conn, datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), ["Blizzard News"]
    )
    names = [s.source_name for s in snap.source_health]
    assert names == ["Blizzard News"]


def test_status_snapshot_never_run_source_reported_not_omitted(conn):
    # "Palworld Steam" is configured but has never recorded health (e.g.
    # just added to config.yaml); it should still show up, flagged as
    # never having run, rather than silently disappearing from the list.
    repo.record_source_result(conn, "Blizzard News", datetime(2026, 9, 23, tzinfo=UTC), None)

    snap = repo.status_snapshot(
        conn,
        datetime(2026, 9, 23, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
        ["Blizzard News", "Palworld Steam"],
    )
    by_name = {s.source_name: s for s in snap.source_health}
    assert set(by_name) == {"Blizzard News", "Palworld Steam"}
    assert by_name["Palworld Steam"].never_run is True
    assert by_name["Palworld Steam"].consecutive_failures == 0
    assert by_name["Blizzard News"].never_run is False
