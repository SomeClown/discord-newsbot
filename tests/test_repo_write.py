"""Tests for the write path in newsbot.store.repo: source health, purging, urls, headlines."""

from contextlib import closing
from datetime import UTC, date, datetime

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


def _item(url="https://example.com/a", topics=None):
    return StoredItem(
        url=url,
        title="Big Patch Notes",
        excerpt="A patch dropped.",
        source_name="Palworld Steam",
        trust="official",
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        topics=topics or {"palworld": False},
    )


def _story(item_urls=None, **overrides):
    fields = {
        "topic_key": "palworld",
        "headline": "Big patch lands",
        "summary": "A summary.",
        "label": "official",
        "item_urls": item_urls or ["https://example.com/a"],
        "update_of_story_id": None,
    }
    fields.update(overrides)
    return StoryToSave(**fields)


# --- record_source_result ---


def test_record_source_result_increments_and_resets(conn):
    now = datetime(2026, 9, 23, tzinfo=UTC)
    assert repo.record_source_result(conn, "Blizzard News", now, "timeout") == 1
    assert repo.record_source_result(conn, "Blizzard News", now, "timeout") == 2
    assert repo.record_source_result(conn, "Blizzard News", now, "timeout") == 3
    assert repo.record_source_result(conn, "Blizzard News", now, None) == 0


# --- purge_older_than ---


def test_purge_older_than_cascades(conn):
    seed_run(conn, date(2026, 9, 23), [_item()], [_story()], message_ids=[1])

    items_deleted, stories_deleted = repo.purge_older_than(conn, datetime(2027, 1, 1, tzinfo=UTC))
    assert items_deleted == 1
    assert stories_deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stories_fts").fetchone()[0] == 0


def test_purge_older_than_nulls_is_update_of(conn):
    seed_run(conn, date(2026, 9, 22), [_item(url="https://example.com/a")], [_story()])
    original_id = conn.execute("SELECT id FROM stories").fetchone()[0]

    seed_run(
        conn,
        date(2026, 9, 23),
        [_item(url="https://example.com/b")],
        [_story(item_urls=["https://example.com/b"], update_of_story_id=original_id)],
    )

    # Purge only the old story, not the update.
    repo.purge_older_than(conn, datetime(2026, 9, 23, tzinfo=UTC))

    remaining = conn.execute("SELECT is_update_of FROM stories").fetchone()
    assert remaining["is_update_of"] is None


# --- existing_urls ---


def test_existing_urls_finds_known(conn):
    seed_run(conn, date(2026, 9, 23), [_item()], [])

    found = repo.existing_urls(conn, ["https://example.com/a", "https://example.com/missing"])
    assert found == {"https://example.com/a"}


def test_existing_urls_chunks_over_sqlite_variable_limit(conn):
    urls = [f"https://example.com/{i}" for i in range(1500)]
    # None of these are in the DB; this test is really about not raising
    # sqlite3.OperationalError: too many SQL variables.
    found = repo.existing_urls(conn, urls)
    assert found == set()


# --- recent_headlines ---


def test_recent_headlines_filters_by_topic_and_time(conn):
    seed_run(conn, date(2026, 9, 23), [_item()], [_story()])

    headlines = repo.recent_headlines(conn, "palworld", datetime(2026, 9, 1, tzinfo=UTC))
    assert len(headlines) == 1
    assert headlines[0].headline == "Big patch lands"

    assert repo.recent_headlines(conn, "diablo4", datetime(2026, 9, 1, tzinfo=UTC)) == []
    assert repo.recent_headlines(conn, "palworld", datetime(2027, 1, 1, tzinfo=UTC)) == []
