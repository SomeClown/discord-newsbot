"""Boundary tests for `purge_older_than`.

test_repo_write.py already covers the happy path (cascades, nulls
`is_update_of`). What's left is the actual boundary: `purge_older_than`
compares with a strict `<`, so a row stamped exactly at the cutoff survives
and one a tick before it doesn't. Off-by-one errors here are the kind that
either keep a year of items around forever or delete today's digest along
with last month's; both are bad enough to earn their own tests.
"""

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


def _save_stamped(conn, run_date: date, url: str, stamp: datetime) -> None:
    digest_id = repo.claim_digest(conn, run_date, force=False)
    item = StoredItem(
        url=url,
        title="x",
        excerpt="x",
        source_name="src",
        trust="official",
        published_at=None,
        topics={"palworld": False},
    )
    story = StoryToSave(
        topic_key="palworld",
        headline=f"story for {url}",
        summary="x",
        label="official",
        item_urls=[url],
        update_of_story_id=None,
    )
    repo.save_run(conn, digest_id, [item], [story], "ok", [], None, Usage(0, 0))
    stamp_iso = stamp.isoformat()
    with conn:
        conn.execute("UPDATE items SET collected_at = ? WHERE url = ?", (stamp_iso, url))
        conn.execute(
            "UPDATE stories SET created_at = ? WHERE digest_id = ?", (stamp_iso, digest_id)
        )


def test_row_exactly_at_cutoff_survives(conn):
    """purge_older_than uses `<`, not `<=`: a row stamped exactly at the
    cutoff is not older than it."""
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)
    _save_stamped(conn, date(2026, 8, 31), "https://e/at-cutoff", cutoff)

    items_deleted, stories_deleted = repo.purge_older_than(conn, cutoff)

    assert (items_deleted, stories_deleted) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_row_one_microsecond_before_cutoff_is_purged(conn):
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)
    just_before = cutoff - timedelta(microseconds=1)
    _save_stamped(conn, date(2026, 8, 31), "https://e/before-cutoff", just_before)

    items_deleted, stories_deleted = repo.purge_older_than(conn, cutoff)

    assert (items_deleted, stories_deleted) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_purge_on_empty_database_returns_zero_zero(conn):
    assert repo.purge_older_than(conn, datetime(2030, 1, 1, tzinfo=UTC)) == (0, 0)


def test_purge_older_than_never_touches_alerted_codes_or_alert_state(conn):
    # design.md §12 / plan §4: retention has no lookback for "have we ever
    # alerted this code before" -- purge_older_than only ever compares
    # items.collected_at and stories.created_at, so a code recorded years
    # ago (and the seeded/last-sweep state alongside it) must survive a
    # purge that would happily delete an item from the same moment.
    from newsbot.store import repo

    repo.record_silent_codes(
        conn,
        [("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a", "seeded")],
        now=lambda: datetime(2000, 1, 1, tzinfo=UTC),
        mark_seeded=True,
    )
    repo.record_sweep(conn, lambda: datetime(2000, 1, 1, tzinfo=UTC), "old sweep")

    items_deleted, stories_deleted = repo.purge_older_than(conn, datetime(2030, 1, 1, tzinfo=UTC))

    assert (items_deleted, stories_deleted) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] > 0


def test_purge_independent_item_and_story_cutoffs(conn):
    """items.collected_at and stories.created_at are compared
    independently: one can survive without the other."""
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)
    digest_id = repo.claim_digest(conn, date(2026, 8, 31), force=False)
    item = StoredItem(
        url="https://e/mixed",
        title="x",
        excerpt="x",
        source_name="src",
        trust="official",
        published_at=None,
        topics={"palworld": False},
    )
    story = StoryToSave(
        topic_key="palworld",
        headline="mixed timestamps",
        summary="x",
        label="official",
        item_urls=["https://e/mixed"],
        update_of_story_id=None,
    )
    repo.save_run(conn, digest_id, [item], [story], "ok", [], None, Usage(0, 0))
    # Item is old (should purge); story is recent (should survive) even
    # though it points at an item that's about to disappear.
    with conn:
        conn.execute(
            "UPDATE items SET collected_at = ? WHERE url = ?",
            ((cutoff - timedelta(days=1)).isoformat(), "https://e/mixed"),
        )
        conn.execute(
            "UPDATE stories SET created_at = ? WHERE digest_id = ?",
            ((cutoff + timedelta(days=1)).isoformat(), digest_id),
        )

    items_deleted, stories_deleted = repo.purge_older_than(conn, cutoff)

    assert (items_deleted, stories_deleted) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1
    # story_items for the deleted item cascades away even though the story stays.
    assert conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0] == 0
