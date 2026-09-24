"""Tests for the pure logic behind /news: option resolution, paging math, and the pager owner check.

Per plan section 5, no gateway mocking: `resolve_query_args` and
`_page_count` are plain functions, and `is_command_owner` is tested as
what it actually is (an id comparison), not through a fake
`discord.Interaction`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from newsbot.bot.commands import _page_count, resolve_query_args
from newsbot.bot.views import PagerView, is_command_owner
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


def test_resolve_query_args_days_lower_bound_one():
    _keys, since, _label = resolve_query_args(TOPICS, "all", 1, None, _NOW)
    assert since == _NOW - timedelta(days=1)


def test_resolve_query_args_days_upper_bound_thirty():
    _keys, since, _label = resolve_query_args(TOPICS, "all", 30, None, _NOW)
    assert since == _NOW - timedelta(days=30)


def test_resolve_query_args_out_of_range_days_pass_through_unclamped():
    # resolve_query_args does no bounds checking of its own -- 1-30 is
    # enforced by app_commands.Range on the `days` option before the
    # handler ever calls this function (see the command-registration
    # tests), so this documents that this function trusts its caller
    # rather than re-validating. If that trust is ever misplaced, it'd
    # show up here as "since" going further back or forward than the
    # slash command's own UI claims is possible.
    _keys, since, _label = resolve_query_args(TOPICS, "all", 0, None, _NOW)
    assert since == _NOW
    _keys, since, _label = resolve_query_args(TOPICS, "all", 365, None, _NOW)
    assert since == _NOW - timedelta(days=365)


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


# --- PagerView button state ---
#
# Constructing a PagerView and reading its buttons' `.disabled` doesn't
# need a live interaction or an event loop -- `_sync_buttons` runs
# synchronously in `__init__`, so this checks the paging math actually
# wired up to the UI without any gateway involved.


async def _render_page(_page: int):  # pragma: no cover - never actually called here
    return None


def test_pager_view_single_page_disables_both_buttons():
    # An empty (or one-page) result: nowhere to page to in either
    # direction.
    view = PagerView(1, render_page=_render_page, total_pages=1)
    assert view.prev_button.disabled is True
    assert view.next_button.disabled is True


def test_pager_view_first_page_of_many_disables_only_prev():
    view = PagerView(1, render_page=_render_page, total_pages=5, page=1)
    assert view.prev_button.disabled is True
    assert view.next_button.disabled is False


def test_pager_view_last_page_of_many_disables_only_next():
    view = PagerView(1, render_page=_render_page, total_pages=5, page=5)
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is True


def test_pager_view_middle_page_enables_both_buttons():
    view = PagerView(1, render_page=_render_page, total_pages=5, page=3)
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is False


def test_pager_view_total_pages_at_zero_clamps_to_one():
    # `_page_count` already guarantees this never happens with a real
    # query result (empty still yields 1 page), but PagerView's own
    # `max(total_pages, 1)` is a second line of defense worth pinning
    # directly.
    view = PagerView(1, render_page=_render_page, total_pages=0)
    assert view.total_pages == 1
    assert view.prev_button.disabled is True
    assert view.next_button.disabled is True


def test_pager_view_total_pages_exact_multiple_of_page_size():
    # 18 stories / 6 per page is exactly 3 pages, with the last page
    # full rather than a leftover partial page.
    total_pages = _page_count(18)
    assert total_pages == 3
    view = PagerView(1, render_page=_render_page, total_pages=total_pages, page=total_pages)
    assert view.next_button.disabled is True


# --- pager/confirm owner check ---


def test_is_command_owner_matching_ids():
    assert is_command_owner(user_id=42, owner_id=42) is True


def test_is_command_owner_different_ids():
    assert is_command_owner(user_id=42, owner_id=99) is False
