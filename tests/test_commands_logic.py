"""Tests for the pure logic behind /news: paging math and the pager owner check.

Per plan section 5, no gateway mocking: `_page_count` is a plain function,
and `is_command_owner` is tested as what it actually is (an id comparison),
not through a fake `discord.Interaction`. (The v2 `resolve_query_args` and
`/newsbot test-alert` helpers that used to be tested here are gone; where
their assertions went is in `test_member_commands_scoped.py` and
`test_shift_match.py`.)
"""

from __future__ import annotations

from newsbot.bot.commands import _page_count
from newsbot.bot.views import PagerView, is_command_owner

# --- paging math ---


def test_page_count_exact_multiple():
    assert _page_count(12, 6) == 2  # 12 stories / 6 per page


def test_page_count_rounds_up():
    assert _page_count(13, 6) == 3  # 13 stories needs a 3rd page for the leftover 1


def test_page_count_zero_is_still_one_page():
    # A PagerView always needs at least one page to show "No stories
    # found." on, even when the query came back empty.
    assert _page_count(0, 6) == 1


def test_page_count_single_page():
    assert _page_count(3, 6) == 1


def test_page_count_respects_a_different_page_size():
    # /shift codes uses 8, not /news' 6: _page_count takes page_size as
    # its own argument now (design.md §13) precisely so both can share it.
    assert _page_count(17, 8) == 3


# --- PagerView button state ---
#
# Constructing a PagerView and reading its buttons' `.disabled` doesn't
# need a live interaction or an event loop: `_sync_buttons` runs
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
    total_pages = _page_count(18, 6)
    assert total_pages == 3
    view = PagerView(1, render_page=_render_page, total_pages=total_pages, page=total_pages)
    assert view.next_button.disabled is True


# --- pager/confirm owner check ---


def test_is_command_owner_matching_ids():
    assert is_command_owner(user_id=42, owner_id=42) is True


def test_is_command_owner_different_ids():
    assert is_command_owner(user_id=42, owner_id=99) is False
