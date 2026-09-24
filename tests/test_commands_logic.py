"""Tests for the pure logic behind /news: option resolution, paging math, and the pager owner check.

Per plan section 5, no gateway mocking: `resolve_query_args` and
`_page_count` are plain functions, and `is_command_owner` is tested as
what it actually is (an id comparison), not through a fake
`discord.Interaction`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from newsbot.bot.commands import _page_count, resolve_query_args
from newsbot.bot.views import is_command_owner
from newsbot.config import Topic

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
TOPICS = [
    Topic(key="borderlands4", name="Borderlands 4", aliases=[], entities=[]),
    Topic(key="palworld", name="Palworld", aliases=[], entities=[]),
]


# --- resolve_query_args ---


def test_resolve_query_args_all_gives_empty_topic_keys():
    topic_keys, since, label = resolve_query_args(TOPICS, "all", 7, None, _NOW)
    assert topic_keys == []
    assert since == _NOW - timedelta(days=7)
    assert label is None


def test_resolve_query_args_one_game_gives_single_topic_key():
    topic_keys, _since, _label = resolve_query_args(TOPICS, "palworld", 7, None, _NOW)
    assert topic_keys == ["palworld"]


def test_resolve_query_args_passes_label_through():
    _keys, _since, label = resolve_query_args(TOPICS, "all", 7, "rumor", _NOW)
    assert label == "rumor"


def test_resolve_query_args_since_uses_days_option():
    _keys, since, _label = resolve_query_args(TOPICS, "all", 30, None, _NOW)
    assert since == _NOW - timedelta(days=30)


# --- paging math ---


def test_page_count_exact_multiple():
    assert _page_count(12) == 2  # 12 stories / 6 per page


def test_page_count_rounds_up():
    assert _page_count(13) == 3  # 13 stories needs a 3rd page for the leftover 1


def test_page_count_zero_is_still_one_page():
    # A PagerView always needs at least one page to show "No stories
    # found." on, even when the query came back empty.
    assert _page_count(0) == 1


def test_page_count_single_page():
    assert _page_count(3) == 1


# --- pager/confirm owner check ---


def test_is_command_owner_matching_ids():
    assert is_command_owner(user_id=42, owner_id=42) is True


def test_is_command_owner_different_ids():
    assert is_command_owner(user_id=42, owner_id=99) is False
