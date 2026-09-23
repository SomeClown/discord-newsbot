"""Tests for the write path and guards in newsbot.store.repo."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, StoryToSave, Usage


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


# --- claim_digest guard ---


def test_claim_digest_fresh_day_claims(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    assert digest_id is not None
    row = repo.get_digest(conn, date(2026, 9, 23))
    assert row.status == "pending"


def test_claim_digest_blocked_by_ok(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [], [], "ok", [1], None, Usage(0, 0))
    assert repo.claim_digest(conn, date(2026, 9, 23), force=False) is None


def test_claim_digest_blocked_by_partial(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [], [], "partial", [1], "fallback", Usage(0, 0))
    assert repo.claim_digest(conn, date(2026, 9, 23), force=False) is None


def test_claim_digest_pending_blocks_even_with_force(conn):
    repo.claim_digest(conn, date(2026, 9, 23), force=False)
    assert repo.claim_digest(conn, date(2026, 9, 23), force=True) is None


def test_claim_digest_failed_allows_reclaim_without_force(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.mark_digest_failed(conn, digest_id, "publish failed", [])
    reclaimed = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    assert reclaimed == digest_id


def test_claim_digest_force_replaces_ok_in_place(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [], [], "ok", [1], None, Usage(0, 0))
    reclaimed = repo.claim_digest(conn, date(2026, 9, 23), force=True)
    assert reclaimed == digest_id
    row = repo.get_digest(conn, date(2026, 9, 23))
    assert row.status == "pending"


# --- save_run ---


def test_save_run_persists_items_and_stories(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [_item()], [_story()], "ok", [111], None, Usage(10, 20))

    row = repo.get_digest(conn, date(2026, 9, 23))
    assert row.status == "ok"
    assert row.posted_message_ids == [111]

    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert count == 1
    story_count = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    assert story_count == 1
    link_count = conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0]
    assert link_count == 1


def test_save_run_is_atomic_on_failure(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_run(
            conn, digest_id, [_item()], [_story()], "not-a-real-status", [1], None, Usage(0, 0)
        )
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0


def test_save_run_sums_token_usage_across_calls(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [_item()], [_story()], "ok", [1], None, Usage(10, 20))
    row = conn.execute(
        "SELECT input_tokens, output_tokens FROM digests WHERE id=?", (digest_id,)
    ).fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (10, 20)


# --- mark_digest_failed ---


def test_mark_digest_failed(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.mark_digest_failed(conn, digest_id, "boom", [])
    row = repo.get_digest(conn, date(2026, 9, 23))
    assert row.status == "failed"
    assert row.error_notes == "boom"


# --- record_source_result ---


def test_record_source_result_increments_and_resets(conn):
    now = datetime(2026, 9, 23, tzinfo=UTC)
    assert repo.record_source_result(conn, "Blizzard News", now, "timeout") == 1
    assert repo.record_source_result(conn, "Blizzard News", now, "timeout") == 2
    assert repo.record_source_result(conn, "Blizzard News", now, "timeout") == 3
    assert repo.record_source_result(conn, "Blizzard News", now, None) == 0


# --- purge_older_than ---


def test_purge_older_than_cascades(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [_item()], [_story()], "ok", [1], None, Usage(0, 0))

    items_deleted, stories_deleted = repo.purge_older_than(conn, datetime(2027, 1, 1, tzinfo=UTC))
    assert items_deleted == 1
    assert stories_deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stories_fts").fetchone()[0] == 0


def test_purge_older_than_nulls_is_update_of(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 22), force=False)
    repo.save_run(
        conn,
        digest_id,
        [_item(url="https://example.com/a")],
        [_story()],
        "ok",
        [1],
        None,
        Usage(0, 0),
    )
    original_id = conn.execute("SELECT id FROM stories").fetchone()[0]

    digest_id2 = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(
        conn,
        digest_id2,
        [_item(url="https://example.com/b")],
        [_story(item_urls=["https://example.com/b"], update_of_story_id=original_id)],
        "ok",
        [2],
        None,
        Usage(0, 0),
    )

    # Purge only the old story, not the update.
    repo.purge_older_than(conn, datetime(2026, 9, 23, tzinfo=UTC))

    remaining = conn.execute("SELECT is_update_of FROM stories").fetchone()
    assert remaining["is_update_of"] is None


# --- existing_urls ---


def test_existing_urls_finds_known(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [_item()], [], "ok", [], None, Usage(0, 0))

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
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [_item()], [_story()], "ok", [], None, Usage(0, 0))

    headlines = repo.recent_headlines(conn, "palworld", datetime(2026, 9, 1, tzinfo=UTC))
    assert len(headlines) == 1
    assert headlines[0].headline == "Big patch lands"

    assert repo.recent_headlines(conn, "diablo4", datetime(2026, 9, 1, tzinfo=UTC)) == []
    assert repo.recent_headlines(conn, "palworld", datetime(2027, 1, 1, tzinfo=UTC)) == []
