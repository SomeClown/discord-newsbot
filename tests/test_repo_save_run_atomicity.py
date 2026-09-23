"""More `save_run` atomicity edge cases than the single bad-status test in
test_repo_write.py covers.

The whole point of `save_run` doing everything inside one `with conn:` block
is that a run either fully lands or leaves no trace. These tests fail a
transaction partway through the *loop* (after at least one item insert has
already happened in-memory) rather than only at the final digest update, and
check the specific things a partial write would otherwise leave behind:
orphaned items with no stories, or a digest still claiming `ok` after a
rollback.
"""

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


def _item(url):
    return StoredItem(
        url=url,
        title="x",
        excerpt="x",
        source_name="src",
        trust="official",
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        topics={"palworld": False},
    )


def test_bad_story_label_rolls_back_items_inserted_earlier_in_the_same_call(conn):
    """A CHECK-constraint failure on the *second* story still rolls back the *first* item insert."""
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    good_story = StoryToSave(
        topic_key="palworld",
        headline="Fine",
        summary="x",
        label="official",
        item_urls=["https://e/a"],
        update_of_story_id=None,
    )
    bad_story = StoryToSave(
        topic_key="palworld",
        headline="Bad",
        summary="x",
        label="not-a-real-label",
        item_urls=["https://e/a"],
        update_of_story_id=None,
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_run(
            conn,
            digest_id,
            [_item("https://e/a")],
            [good_story, bad_story],
            "ok",
            [1],
            None,
            Usage(0, 0),
        )

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
    # The claim's pending status survives; a failed save_run doesn't silently
    # promote the digest, and it isn't left half-updated either.
    assert repo.get_digest(conn, date(2026, 9, 23)).status == "pending"


def test_failed_save_run_does_not_advance_token_usage(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    bad_story = StoryToSave(
        topic_key="palworld",
        headline="Bad",
        summary="x",
        label="not-a-real-label",
        item_urls=["https://e/a"],
        update_of_story_id=None,
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_run(
            conn, digest_id, [_item("https://e/a")], [bad_story], "ok", [1], None, Usage(999, 999)
        )
    row = conn.execute(
        "SELECT input_tokens, output_tokens FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)


def test_story_with_no_matching_item_urls_is_skipped_not_orphaned(conn):
    """A story referencing only URLs absent from `items` gets dropped, not written with no links."""
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    orphan_story = StoryToSave(
        topic_key="palworld",
        headline="Orphan",
        summary="x",
        label="official",
        item_urls=["https://e/never-collected"],
        update_of_story_id=None,
    )
    repo.save_run(conn, digest_id, [], [orphan_story], "ok", [1], None, Usage(0, 0))
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0


def test_save_run_second_call_reuses_existing_item_row_on_conflict(conn):
    """ON CONFLICT DO NOTHING means a URL collected twice keeps its original item id."""
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    repo.save_run(conn, digest_id, [_item("https://e/a")], [], "ok", [], None, Usage(0, 0))
    first_id = conn.execute("SELECT id FROM items WHERE url = ?", ("https://e/a",)).fetchone()[0]

    digest_id2 = repo.claim_digest(conn, date(2026, 9, 24), force=False)
    story = StoryToSave(
        topic_key="palworld",
        headline="Same item again",
        summary="x",
        label="official",
        item_urls=["https://e/a"],
        update_of_story_id=None,
    )
    repo.save_run(conn, digest_id2, [_item("https://e/a")], [story], "ok", [], None, Usage(0, 0))

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    second_id = conn.execute("SELECT id FROM items WHERE url = ?", ("https://e/a",)).fetchone()[0]
    assert first_id == second_id
    linked = conn.execute("SELECT item_id FROM story_items").fetchone()[0]
    assert linked == first_id
