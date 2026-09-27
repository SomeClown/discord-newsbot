"""Tests for `bot.format.render_code_page` (`/shift codes`, design.md §13, plan step 6).

Same split as the alert-rendering tests: this file only pins what one
page of the embed looks like, given a list of `CodeView`s -- the query
that produces them (`repo.query_codes`) and the command that drives
paging (`commands.make_shift_group`) are covered elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime

from newsbot.bot.format import discord_len, render_code_page
from newsbot.store.models import CodeView

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"


def _view(**kwargs) -> CodeView:
    defaults = dict(
        code=CODE_A,
        first_seen_at=datetime(2026, 9, 25, 20, 0, tzinfo=UTC),
        source_name="Gearbox Blog",
        item_url="https://e.com/a",
        status="posted",
        from_roundup=False,
    )
    defaults.update(kwargs)
    return CodeView(**defaults)


# --- basic shape ---


def test_code_appears_in_a_fenced_block():
    embed = render_code_page(
        [_view()], title="/shift codes", page=1, pages=1, timezone="America/Los_Angeles"
    )
    assert f"```\n{CODE_A}\n```" in embed.description


def test_first_seen_date_shown_in_the_given_timezone():
    # 2026-09-25 20:00 UTC is still 2026-09-25 13:00 PDT -- same calendar
    # day either way here, so pick an instant near UTC midnight where the
    # two timezones actually disagree on the date.
    late_utc = datetime(2026, 9, 26, 3, 30, tzinfo=UTC)  # 2026-09-25 20:30 PDT
    embed = render_code_page(
        [_view(first_seen_at=late_utc)],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "First seen 2026-09-25" in embed.description
    assert "2026-09-26" not in embed.description


def test_footer_has_page_count_and_expiry_disclaimer():
    embed = render_code_page(
        [_view()], title="/shift codes", page=2, pages=5, timezone="America/Los_Angeles"
    )
    assert embed.footer.text == (
        "Page 2 of 5 · I don't know when codes expire; older ones may have stopped working."
    )


def test_empty_window_message():
    embed = render_code_page(
        [], title="/shift codes", page=1, pages=1, timezone="America/Los_Angeles"
    )
    assert embed.description == "No codes seen in that window."
    assert embed.footer.text.startswith("Page 1 of 1")


# --- markers (D5) ---


def test_roundup_marker_shown():
    embed = render_code_page(
        [_view(from_roundup=True)],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "(from a roundup)" in embed.description


def test_too_old_marker_shown():
    embed = render_code_page(
        [_view(status="too_old")],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "(old post)" in embed.description


def test_seeded_marker_shown():
    embed = render_code_page(
        [_view(status="seeded")],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "(already around when alerts started)" in embed.description


def test_from_roundup_wins_over_too_old_status():
    # A roundup code that also went stale (both true at once) shows the
    # roundup marker, not the too-old one -- the one D5 asks for.
    embed = render_code_page(
        [_view(status="too_old", from_roundup=True)],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "(from a roundup)" in embed.description
    assert "(old post)" not in embed.description


def test_plain_posted_code_has_no_marker():
    embed = render_code_page(
        [_view(status="posted")],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "(" not in embed.description


# --- escaping / safety ---


def test_hostile_source_name_is_escaped():
    hostile = "@everyone <@123456> __pwned__"
    embed = render_code_page(
        [_view(source_name=hostile)],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert "@everyone" not in embed.description


def test_unsafe_url_omits_the_link_but_keeps_the_code():
    embed = render_code_page(
        [_view(item_url="javascript:alert(1)")],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert CODE_A in embed.description
    assert "<javascript:" not in embed.description


def test_long_source_name_is_truncated():
    embed = render_code_page(
        [_view(source_name="S" * 500)],
        title="/shift codes",
        page=1,
        pages=1,
        timezone="America/Los_Angeles",
    )
    assert discord_len(embed.description) <= 4096


def test_a_full_page_of_ordinary_codes_stays_under_the_4096_cap():
    # /shift codes pages 8 at a time (commands._SHIFT_PAGE_SIZE) -- a full
    # page of ordinary-length entries is nowhere near the cap.
    codes = [
        _view(code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", item_url=f"https://e.com/{i}")
        for i in range(8)
    ]
    embed = render_code_page(
        codes, title="/shift codes", page=1, pages=1, timezone="America/Los_Angeles"
    )
    assert discord_len(embed.description) <= 4096


# --- per-entry budgeting (QA follow-up: truncation must not drop codes) ---


def test_a_full_page_of_codes_with_huge_urls_keeps_every_code_and_balances_fences():
    codes = [
        _view(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            item_url="https://example.com/" + "a" * 400,
        )
        for i in range(8)
    ]
    embed = render_code_page(
        codes, title="/shift codes", page=1, pages=1, timezone="America/Los_Angeles"
    )
    assert discord_len(embed.description) <= 4096
    for c in codes:
        assert f"```\n{c.code}\n```" in embed.description
    assert embed.description.count("```") % 2 == 0


def test_a_full_page_of_codes_with_huge_source_names_keeps_every_code():
    codes = [
        _view(code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", source_name="Reddit Megathread " * 40)
        for i in range(8)
    ]
    embed = render_code_page(
        codes, title="/shift codes", page=1, pages=1, timezone="America/Los_Angeles"
    )
    assert discord_len(embed.description) <= 4096
    for c in codes:
        assert f"```\n{c.code}\n```" in embed.description
