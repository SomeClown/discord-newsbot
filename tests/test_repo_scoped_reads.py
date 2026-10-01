"""Repo reads task 9 added: `search_stories(topic_keys=)`, `latest_guild_digest`, `guild_counts`."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=30)


@pytest.fixture
def conn(v3_db):
    with closing(connect(v3_db)) as c:
        yield c


def _story(conn, topic_key, headline, digest_id=1):
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO digests (id, run_date, status, created_at, updated_at) "
            "VALUES (?, ?, 'ok', 'n', 'n')",
            (digest_id, f"2026-09-{digest_id:02d}"),
        )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES (?, ?, 'zebracorn summary', 'official', ?, ?)",
            (topic_key, headline, digest_id, NOW.isoformat()),
        )


@pytest.fixture
def stories(conn):
    _story(conn, "borderlands4", "zebracorn in Borderlands")
    _story(conn, "palworld", "zebracorn in Palworld")
    _story(conn, "rust", "zebracorn in Rust")


def _keys(result):
    return sorted(s.topic_key for s in result[0])


def test_search_stories_without_topic_keys_searches_everything(conn, stories):
    assert _keys(repo.search_stories(conn, "zebracorn", SINCE, 10, 0)) == [
        "borderlands4",
        "palworld",
        "rust",
    ]


def test_search_stories_limits_to_the_topic_keys_given_and_counts_only_those(conn, stories):
    result = repo.search_stories(conn, "zebracorn", SINCE, 10, 0, topic_keys=["palworld", "rust"])
    assert _keys(result) == ["palworld", "rust"]
    assert result[1] == 2


def test_search_stories_with_an_empty_topic_list_returns_nothing(conn, stories):
    # The opposite of query_stories' "empty means All", on purpose: see the docstring.
    assert repo.search_stories(conn, "zebracorn", SINCE, 10, 0, topic_keys=[]) == ([], 0)


@pytest.mark.parametrize("hostile", ["' OR 1=1 --", "%", "palworld' --", "", "rust\x00"])
def test_search_stories_topic_keys_are_bound_not_interpolated(conn, stories, hostile):
    assert repo.search_stories(conn, "zebracorn", SINCE, 10, 0, topic_keys=[hostile]) == ([], 0)


def test_latest_guild_digest_is_the_newest_by_date_and_only_that_guilds(conn):
    repo.create_guild(conn, 1)
    repo.create_guild(conn, 2)
    for guild_id, day, status in [(1, 28, "ok"), (1, 29, "failed"), (2, 30, "ok")]:
        with conn:
            conn.execute(
                "INSERT INTO digests (run_date, status, created_at, updated_at, guild_id) "
                "VALUES (?, ?, 'n', 'n', ?)",
                (f"2026-09-{day}", status, guild_id),
            )
    latest = repo.latest_guild_digest(conn, 1)
    assert (latest.run_date, latest.status, latest.guild_id) == (date(2026, 9, 29), "failed", 1)
    assert repo.latest_guild_digest(conn, 3) is None


def test_guild_counts(conn):
    repo.create_guild(conn, 1, set_up=True)
    repo.create_guild(conn, 2, tier="comped")
    repo.create_guild(conn, 3, set_up=True)
    repo.update_guild_settings(conn, 3, permission_problems="oops")
    repo.set_shift(conn, 1, enabled=True, channel_id=5, ping="none")
    repo.set_shift(conn, 2, enabled=False, channel_id=None, ping="none")
    assert repo.guild_counts(conn) == {
        "servers": 3,
        "set_up": 2,
        "free": 2,
        "comped": 1,
        "permission_problems": 1,
        "shift_enabled": 1,
    }


def test_guild_counts_on_an_empty_database(conn):
    assert repo.guild_counts(conn) == {
        "servers": 0,
        "set_up": 0,
        "free": 0,
        "comped": 0,
        "permission_problems": 0,
        "shift_enabled": 0,
    }
