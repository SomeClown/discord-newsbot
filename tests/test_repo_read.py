"""Tests for the read path (`/news` commands) in newsbot.store.repo."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from v2_seed import seed_run

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, StoryToSave


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _save_story(
    conn, run_date, *, topic_key="palworld", headline, summary="A summary.", label="official", url
):
    """Save a story and backdate its timestamps to `run_date`.

    `seed_run` stamps `created_at`/`collected_at` with the real wall clock,
    so tests that need stories to look like they landed on a particular day
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
    digest_id = seed_run(conn, run_date, [item], [story])

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


def test_query_stories_clamps_a_negative_limit_and_offset(conn):
    # SQLite reads LIMIT -1 as "no limit", so -1 must not mean everything.
    for day in (1, 2, 3):
        _save_story(conn, date(2026, 9, day), headline=f"S{day}", url=f"https://e/{day}")
    since = datetime(2026, 1, 1, tzinfo=UTC)

    page, total = repo.query_stories(conn, [], since, None, limit=-1, offset=-3)
    assert total == 3
    assert [s.headline for s in page] == ["S3"]


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


# "\ud800" is a lone surrogate: a valid Python str that can't be bound as UTF-8.
@pytest.mark.parametrize(
    "bad_query", ['foo" OR bar*', "NEAR(", "", "   ", '"""', "\ud800", "patch \udfff"]
)
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
