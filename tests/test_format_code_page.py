"""Tests for the code blocks `/shift history` is made of (`render_history_pages`).

This file used to hold the old `/shift codes` page renderer's tests; that renderer is gone, and
its block (the fenced code, the first-seen line, the marker) is what `/shift history` still
draws. What's pinned here is what one page looks like given a list of `CodeView`s, with fixed
expected strings where the exact text matters. The query that produces the views
(`repo.history_codes`) and the command that drives them are covered elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime

from newsbot.bot.format import discord_len, render_history_pages
from newsbot.store.models import CodeView

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
LA = "America/Los_Angeles"


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


def _pages(views, timezone=LA):
    return render_history_pages(views, timezone=timezone, now=NOW, title="/shift history")


def _text(views, timezone=LA) -> str:
    return "\n\n".join(embed.description for embed in _pages(views, timezone))


# --- basic shape ---


def test_one_plain_code_is_exactly_this_page():
    (embed,) = _pages([_view()])
    assert embed.title == "/shift history"
    assert embed.description == (
        f"```\n{CODE_A}\n```\nFirst seen 2026-09-25 · Gearbox Blog · <https://e.com/a>"
    )
    assert embed.footer.text == (
        "Where the expiry is unknown, older codes may have stopped working. · Page 1 of 1"
    )


def test_code_appears_in_a_fenced_block():
    assert f"```\n{CODE_A}\n```" in _text([_view()])


def test_first_seen_date_shown_in_the_given_timezone():
    # 2026-09-26 03:30 UTC is still 2026-09-25 20:30 in Los Angeles: pick an instant near UTC
    # midnight where the two calendars actually disagree on the date.
    late_utc = datetime(2026, 9, 26, 3, 30, tzinfo=UTC)
    text = _text([_view(first_seen_at=late_utc)])
    assert "First seen 2026-09-25" in text
    assert "2026-09-26" not in text
    assert "First seen 2026-09-26" in _text([_view(first_seen_at=late_utc)], timezone="UTC")


def test_empty_window_message():
    (embed,) = _pages([])
    assert embed.description == "No codes seen in that window."
    assert embed.footer.text.endswith("Page 1 of 1")


# --- markers (D5) ---


def test_roundup_marker_shown():
    assert "· (from a roundup)" in _text([_view(from_roundup=True)])


def test_too_old_marker_shown():
    assert "· (old post)" in _text([_view(status="too_old")])


def test_seeded_marker_shown():
    assert "· (already around when alerts started)" in _text([_view(status="seeded")])


def test_from_roundup_wins_over_too_old_status():
    # A roundup code that also went stale (both true at once) shows the roundup marker, not the
    # too-old one: the one D5 asks for.
    text = _text([_view(status="too_old", from_roundup=True)])
    assert "(from a roundup)" in text
    assert "(old post)" not in text


def test_plain_posted_code_has_no_marker():
    assert "(" not in _text([_view(status="posted")])


# --- escaping / safety ---


def test_hostile_source_name_is_escaped():
    text = _text([_view(source_name="@everyone <@123456> __pwned__")])
    assert "@everyone" not in text


def test_unsafe_url_omits_the_link_but_keeps_the_code():
    text = _text([_view(item_url="javascript:alert(1)")])
    assert CODE_A in text
    assert "<javascript:" not in text
    assert text.endswith("First seen 2026-09-25 · Gearbox Blog")


def test_long_source_name_is_truncated():
    (embed,) = _pages([_view(source_name="S" * 500)])
    assert discord_len(embed.description) <= 4096
    assert "S" * 101 not in embed.description


def test_a_full_page_of_ordinary_codes_stays_under_the_4096_cap():
    # Eight ordinary-length entries are nowhere near the cap (`/shift history` pages by size,
    # so there is no fixed eight any more).
    codes = [
        _view(code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", item_url=f"https://e.com/{i}")
        for i in range(8)
    ]
    (embed,) = _pages(codes)
    assert discord_len(embed.description) <= 4096


# --- size budgeting: nothing is dropped, nothing is cut in half ---


def test_codes_with_huge_urls_keep_every_code_and_balance_fences_across_pages():
    codes = [
        _view(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            item_url="https://example.com/" + "a" * 400,
        )
        for i in range(8)
    ]
    embeds = _pages(codes)
    text = "\n\n".join(e.description for e in embeds)
    for embed in embeds:
        assert discord_len(embed.description) <= 4096
        assert embed.description.count("```") % 2 == 0
    for c in codes:
        assert f"```\n{c.code}\n```" in text


def test_codes_with_huge_source_names_keep_every_code():
    codes = [
        _view(code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", source_name="Reddit Megathread " * 40)
        for i in range(8)
    ]
    embeds = _pages(codes)
    text = "\n\n".join(e.description for e in embeds)
    assert all(discord_len(e.description) <= 4096 for e in embeds)
    for c in codes:
        assert f"```\n{c.code}\n```" in text


def test_order_is_the_order_given_and_pages_are_numbered():
    codes = [
        _view(code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", source_name="N" * 100, item_url="")
        for i in range(30)
    ]
    embeds = _pages(codes)
    assert len(embeds) >= 1
    for i, embed in enumerate(embeds, start=1):
        assert embed.footer.text.endswith(f"Page {i} of {len(embeds)}")
    text = "\n".join(e.description for e in embeds)
    assert text.index(codes[0].code) < text.index(codes[-1].code)
