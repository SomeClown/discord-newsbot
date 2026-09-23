"""Hostile input for `fts_escape` and `search_stories`.

test_repo_read.py already checks that a handful of FTS5 operator characters
don't blow up the query. This file leans harder on the same idea: every
token a member could type into `/news search`, no matter how much it looks
like FTS5 query syntax or SQL, should turn into a literal phrase match and
nothing else. If any of these ever reaches SQLite as anything other than a
bound parameter, the `stories` table should still be standing at the end of
the test to prove it.
"""

from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, StoryToSave, Usage

SINCE = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _seed_one_story(conn):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    item = StoredItem(
        url="https://e/1",
        title="Big Patch Notes",
        excerpt="A patch dropped.",
        source_name="Palworld Steam",
        trust="official",
        published_at=None,
        topics={"palworld": False},
    )
    story = StoryToSave(
        topic_key="palworld",
        headline="Big Patch Notes",
        summary="A patch dropped.",
        label="official",
        item_urls=["https://e/1"],
        update_of_story_id=None,
    )
    repo.save_run(conn, digest_id, [item], [story], "ok", [], None, Usage(0, 0))


# --- fts_escape: every token becomes a quoted phrase, operators included ---


@pytest.mark.parametrize(
    "user_query",
    [
        '"; DROP TABLE stories; --',
        "'; DROP TABLE stories; --",
        "headline:hack",  # FTS5 column-filter syntax
        "patch NOT notes",
        "patch OR notes AND leak",
        "NEAR(patch notes, 2)",
        "*" * 50,
        '"' * 50,
        "patch)",
        "(patch",
        "\x00null\x00byte",
        "a" * 5000,  # one very long token
        "café あいう",  # non-ASCII, including CJK
        "‮patch‬",  # RTL override characters
    ],
)
def test_fts_escape_never_lets_syntax_or_control_chars_through_unquoted(user_query):
    escaped = repo.fts_escape(user_query)
    # Every non-empty token must come out wrapped in a matched pair of
    # quotes; that's what turns FTS5 operators into inert phrase text.
    for token in escaped.split(" "):
        if not token:
            continue
        assert token.startswith('"')
        assert token.endswith('"')


@pytest.mark.parametrize(
    "user_query",
    [
        '"; DROP TABLE stories; --',
        "'; DROP TABLE stories; --",
        "headline:hack",
        "patch NOT notes",
        "patch OR notes AND leak",
        "NEAR(patch notes, 2)",
        "*" * 50,
        '"' * 50,
        "patch)",
        "(patch",
        "a" * 5000,
        "café あいう",
        "‮patch‬",
    ],
)
def test_search_stories_survives_hostile_query_without_raising(conn, user_query):
    _seed_one_story(conn)
    # Should never raise, regardless of what the member typed, and the
    # `stories` table (and its data) should be untouched afterward.
    repo.search_stories(conn, user_query, SINCE, 10, 0)
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1


def test_search_stories_hostile_query_does_not_match_real_data(conn):
    _seed_one_story(conn)
    page, total = repo.search_stories(conn, '"; DROP TABLE stories; --', SINCE, 10, 0)
    assert page == []
    assert total == 0
    # The injection attempt didn't do anything: the table's still there
    # with the story we seeded.
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1


def test_search_stories_null_byte_query_does_not_raise(conn):
    _seed_one_story(conn)
    # Python's sqlite3 module raises ValueError on an embedded NUL in a bound
    # string, which would surface here as an unhandled exception rather than
    # the "no results" behavior every other malformed query gets. This pins
    # the current (safe) behavior so a future change to fts_escape's token
    # handling can't reintroduce that without a test noticing.
    page, total = repo.search_stories(conn, "foo\x00bar", SINCE, 10, 0)
    assert page == []
    assert total == 0
