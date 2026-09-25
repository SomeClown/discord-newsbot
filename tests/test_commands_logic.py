"""Tests for the pure logic behind /news: option resolution, paging math, and the pager owner check.

Per plan section 5, no gateway mocking: `resolve_query_args` and
`_page_count` are plain functions, and `is_command_owner` is tested as
what it actually is (an id comparison), not through a fake
`discord.Interaction`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from newsbot.bot.commands import (
    _page_count,
    invalid_test_alert_code_message,
    resolve_query_args,
    summarize_test_alert,
)
from newsbot.bot.views import PagerView, is_command_owner
from newsbot.config import Topic
from newsbot.shift.sweep import CodeCheckOutcome

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


# --- invalid_test_alert_code_message (plan step 10) ---


def test_invalid_test_alert_code_message_accepts_a_real_code():
    assert invalid_test_alert_code_message("AAAA1-BBBBB-CCCCC-DDDDD-EEEEE") is None


def test_invalid_test_alert_code_message_accepts_lowercase():
    assert invalid_test_alert_code_message("aaaa1-bbbbb-ccccc-ddddd-eeeee") is None


def test_invalid_test_alert_code_message_rejects_wrong_group_sizes():
    assert invalid_test_alert_code_message("AAAA-BBBBB-CCCCC-DDDDD-EEEEE") is not None


def test_invalid_test_alert_code_message_rejects_wrong_group_count():
    assert invalid_test_alert_code_message("AAAAA-BBBBB-CCCCC-DDDDD") is not None


def test_invalid_test_alert_code_message_rejects_fullwidth_lookalikes():
    # Fullwidth Latin/digits (U+FF21 "Ａ" etc.) look right to a human eye
    # but aren't in CODE_RE's ASCII-only character class -- exactly the
    # kind of thing a copy-paste from a phone keyboard could produce.
    fullwidth = "ＡＡＡＡＡ-BBBBB-CCCCC-DDDDD-EEEEE"
    assert invalid_test_alert_code_message(fullwidth) is not None


def test_invalid_test_alert_code_message_rejects_spaces_instead_of_hyphens():
    assert invalid_test_alert_code_message("AAAAA BBBBB CCCCC DDDDD EEEEE") is not None


def test_invalid_test_alert_code_message_is_stable_text():
    # Not asserting exact wording elsewhere risks the message silently
    # drifting into something unhelpful; pin it once here.
    message = invalid_test_alert_code_message("not-a-code")
    assert message is not None
    assert "SHiFT code" in message


# --- summarize_test_alert (plan step 10) ---


def _outcome(**overrides) -> CodeCheckOutcome:
    base = dict(new_candidates=1, posted=0, silent=0, failed=0, ping=False, cap_reached=False)
    base.update(overrides)
    return CodeCheckOutcome(**base)


def test_summarize_test_alert_posted_with_ping():
    assert summarize_test_alert(_outcome(posted=1, ping=True)) == "posted with ping"


def test_summarize_test_alert_posted_without_ping_cap():
    assert (
        summarize_test_alert(_outcome(posted=1, ping=False, cap_reached=True))
        == "posted without ping (cap)"
    )


def test_summarize_test_alert_already_alerted():
    assert summarize_test_alert(_outcome()) == "already alerted, nothing posted"


def test_summarize_test_alert_failed_takes_priority_over_posted_flags():
    outcome = _outcome(posted=0, failed=1, ping=False)
    assert summarize_test_alert(outcome) == "post failed; check the bot's log"
