"""Proves the `# noqa: S608` sites in newsbot/store/repo.py are safe.

Three functions build a SQL string with an f-string instead of a plain
literal: `existing_urls`, `_story_urls` (via query_stories/search_stories),
and `query_stories` itself. In every case the comment next to the noqa
claims the f-string only assembles placeholder counts and fixed clause
fragments, never a *value* a member or a malformed source could control.
Comments lie eventually; this file is here so that if one ever does, a
test fails instead of a member typing a topic name into `/news recent`
getting to run their own SQL.
"""

from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, StoryToSave, Usage

SINCE = datetime(2020, 1, 1, tzinfo=UTC)

INJECTION_PAYLOADS = [
    "palworld' OR '1'='1",
    "palworld'; DROP TABLE stories; --",
    "palworld) UNION SELECT id,topic_key,headline,summary,label,"
    "created_at,is_update_of FROM stories --",
    "'||(SELECT sql FROM sqlite_master)||'",
]


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _seed_two_stories(conn):
    for i, (topic_key, headline) in enumerate([("palworld", "P story"), ("diablo4", "D story")]):
        digest_id = repo.claim_digest(conn, date(2026, 9, 20 + i), force=False)
        item = StoredItem(
            url=f"https://e/{i}",
            title=headline,
            excerpt="x",
            source_name="src",
            trust="official",
            published_at=None,
            topics={topic_key: False},
        )
        story = StoryToSave(
            topic_key=topic_key,
            headline=headline,
            summary="x",
            label="official",
            item_urls=[f"https://e/{i}"],
            update_of_story_id=None,
        )
        repo.save_run(conn, digest_id, [item], [story], "ok", [], None, Usage(0, 0))


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_query_stories_topic_keys_are_bound_not_interpolated(conn, payload):
    _seed_two_stories(conn)
    # A malicious topic key is just a value that matches nothing; it must
    # not change the shape of the query or leak rows from other topics.
    page, total = repo.query_stories(conn, [payload], SINCE, None, limit=10, offset=0)
    assert page == []
    assert total == 0
    # The table survives; a real DROP TABLE would make this raise instead.
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_query_stories_label_is_bound_not_interpolated(conn, payload):
    _seed_two_stories(conn)
    page, total = repo.query_stories(conn, [], SINCE, payload, limit=10, offset=0)
    assert page == []
    assert total == 0
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_existing_urls_values_are_bound_not_interpolated(conn, payload):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    item = StoredItem(
        url="https://e/real",
        title="x",
        excerpt="x",
        source_name="src",
        trust="official",
        published_at=None,
        topics={"palworld": False},
    )
    repo.save_run(conn, digest_id, [item], [], "ok", [], None, Usage(0, 0))

    found = repo.existing_urls(conn, [payload, "https://e/real"])
    assert found == {"https://e/real"}
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_query_stories_many_topic_keys_including_payloads_still_binds_correctly(conn):
    """The placeholder count in the f-string must always match
    len(topic_keys), even with junk mixed in."""
    _seed_two_stories(conn)
    keys = [*INJECTION_PAYLOADS, "palworld"]
    page, total = repo.query_stories(conn, keys, SINCE, None, limit=10, offset=0)
    assert total == 1
    assert page[0].headline == "P story"
