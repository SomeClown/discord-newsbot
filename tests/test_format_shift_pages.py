"""Tests for `render_current_pages` and `render_history_pages` (design.md §16, plan task 6).

Both fill an embed description by size instead of by count, so the interesting cases
are the long ones: 300-unit rewards and origins, a hundred entries, and a group that
runs over a page break. The rules they hold to are Discord's: 4,096 units per
description, 6,000 across a whole embed, and no entry ever cut in half.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from newsbot.bot.format import discord_len, render_current_pages, render_history_pages
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # noon in Pacific time, Oct 9
SEEN = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
FOOTER = "Codes from Gearbox and mentalmars.com · redeem at shift.gearboxsoftware.com"
EMPTY = "No current codes. I'll post new ones in <#123> when they show up."
BIDI = "\u202e\u2066"


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _details(code: str, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code, clock_start=SEEN, updated_at=SEEN)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _current(i: int = 0, details: dict | None = None, **kwargs) -> CurrentCode:
    code = _code(i)
    row = _details(code, **details) if details is not None else None
    defaults = dict(code=code, first_seen_at=SEEN, details=row)
    defaults.update(kwargs)
    return CurrentCode(**defaults)


def _pages(entries, kind="all"):
    return render_current_pages(entries, kind=kind, now=NOW, footer=FOOTER, empty=EMPTY)


def _all_text(embeds) -> str:
    return "\n".join(e.description for e in embeds)


def _embed_total(embed) -> int:
    return sum(
        discord_len(part or "") for part in (embed.title, embed.description, embed.footer.text)
    )


# --- /shift codes: one page ---


def test_groups_run_golden_cosmetic_permanent_other():
    entries = [
        _current(1, {"category": "permanent"}),
        _current(2),
        _current(3, {"category": "cosmetic"}),
        _current(4, {"category": "golden"}),
        _current(5, {"category": "other"}),
    ]
    [embed] = _pages(entries)
    text = embed.description
    order = [
        text.index(h) for h in ("**Golden Keys**", "**Cosmetics**", "**Permanent**", "**Other**")
    ]
    assert order == sorted(order)
    # Other holds both the code with no details and the code whose category is "other".
    assert text.index(_code(2)) > text.index("**Other**")
    assert text.index(_code(5)) > text.index("**Other**")


def test_empty_groups_have_no_heading():
    [embed] = _pages([_current(1, {"category": "golden"})])
    assert "**Golden Keys**" in embed.description
    assert "Cosmetics" not in embed.description
    assert "Permanent" not in embed.description


def test_newest_first_by_added_day_else_first_sighting():
    entries = [
        _current(1, {"category": "golden", "added_on": date(2026, 10, 1)}),
        _current(2, {"category": "golden", "added_on": date(2026, 10, 7)}),
        # No Added column: its first sighting (Oct 4) puts it between the two.
        _current(3, {"category": "golden"}, first_seen_at=datetime(2026, 10, 4, 12, tzinfo=UTC)),
        _current(4, None, first_seen_at=datetime(2026, 9, 1, tzinfo=UTC)),
    ]
    [embed] = _pages(entries)
    text = embed.description
    assert text.index(_code(2)) < text.index(_code(3)) < text.index(_code(1))


def test_the_type_filter_keeps_one_group():
    entries = [
        _current(1, {"category": "golden"}),
        _current(2, {"category": "cosmetic"}),
        _current(3, {"category": "permanent"}),
        _current(4),
    ]
    for kind, keep in (("golden", 1), ("cosmetic", 2), ("permanent", 3)):
        [embed] = _pages(entries, kind)
        assert _code(keep) in embed.description
        assert sum(_code(i) in embed.description for i in (1, 2, 3, 4)) == 1
    [embed] = _pages(entries, "all")
    assert all(_code(i) in embed.description for i in (1, 2, 3, 4))


def test_the_title_names_the_filter():
    assert _pages([_current(1)])[0].title == "Current SHiFT codes"
    assert _pages([_current(1, {"category": "golden"})], "golden")[0].title == (
        "Current SHiFT codes: Golden Keys"
    )


def test_an_unknown_type_is_an_error():
    with pytest.raises(ValueError, match="type"):
        _pages([_current(1)], "everything")


def test_footer_and_page_numbers():
    [embed] = _pages([_current(1)])
    assert embed.footer.text == f"{FOOTER} · Page 1 of 1"


def test_empty_list_says_so():
    [embed] = _pages([])
    assert embed.description == EMPTY
    assert embed.footer.text == f"{FOOTER} · Page 1 of 1"


def test_a_filter_that_matches_nothing_is_the_empty_page_too():
    [embed] = _pages([_current(1, {"category": "golden"})], "permanent")
    assert embed.description == EMPTY


# --- /shift codes: entry shapes ---


def _entry_of(code: CurrentCode) -> str:
    [embed] = _pages([code])
    return embed.description.split("\n", 1)[1]  # past the heading


def test_entry_with_reward_and_far_expiry():
    entry = _entry_of(
        _current(
            1,
            {"category": "golden", "reward": "3 Golden Keys", "expires_on": date(2026, 10, 14)},
        )
    )
    assert entry == f"`{_code(1)}`\n3 Golden Keys · expires Oct 14 (in 5 days)"


def test_the_year_shows_when_it_is_not_this_pacific_year():
    entry = _entry_of(
        _current(1, {"reward": "Skin", "expires_on": date(2027, 1, 3), "category": "cosmetic"})
    )
    assert entry.endswith("Skin · expires Jan 3, 2027 (in 86 days)")


def test_near_expiries_keep_their_relative_wording_that_alerts_no_longer_have():
    hours = _entry_of(_current(1, {"reward": "x", "expires_on": date(2026, 10, 9)}))
    tomorrow = _entry_of(_current(2, {"reward": "x", "expires_on": date(2026, 10, 10)}))
    assert hours.endswith("x · expires in 12 hours")
    assert tomorrow.endswith("x · expires tomorrow")


def test_shift_codes_is_relative_all_the_way_and_has_no_live_timestamp():
    # `/shift codes` is drawn fresh on every call, so it can say "in 12 hours" and mean it.
    entries = [
        _current(1, {"reward": "x", "expires_on": date(2026, 10, 9)}),
        _current(2, {"reward": "x", "expires_on": date(2026, 10, 10)}),
        _current(3, {"reward": "x", "expires_on": date(2026, 10, 14)}),
    ]
    text = _all_text(_pages(entries))
    assert "expires in 12 hours" in text
    assert "expires tomorrow" in text
    assert "expires Oct 14 (in 5 days)" in text
    assert "<t:" not in text


def test_unknowns_say_so():
    assert _entry_of(_current(1)) == f"`{_code(1)}`\nreward unknown · expiry unknown"
    entry = _entry_of(_current(1, {"category": "golden", "reward": "3 Golden Keys"}))
    assert entry.endswith("3 Golden Keys · expiry unknown")
    entry = _entry_of(_current(1, {"category": "golden", "expires_on": date(2026, 10, 10)}))
    assert entry.endswith("reward unknown · expires tomorrow")


def test_a_permanent_code_shows_where_it_came_from():
    entry = _entry_of(
        _current(1, {"category": "permanent", "reward": "Head", "origin": "Twitch drop"})
    )
    assert entry == f"`{_code(1)}`\nHead · Twitch drop"
    entry = _entry_of(_current(1, {"category": "permanent", "reward": "Head"}))
    assert entry.endswith("Head · permanent")


def test_permanent_ignores_a_stray_expiry():
    entry = _entry_of(
        _current(
            1,
            {"category": "permanent", "reward": "Head", "expires_on": date(2020, 1, 1)},
        )
    )
    assert entry.endswith("Head · permanent")


def test_scraped_text_is_escaped_and_bidi_stripped_on_the_page():
    entry = _entry_of(
        _current(
            1,
            {
                "category": "permanent",
                "reward": f"{BIDI}@everyone **bold**",
                "origin": f"a{BIDI}b <@123456789012345678>",
            },
        )
    )
    assert not any(ch in entry for ch in BIDI)
    assert "@everyone" not in entry
    assert "ab <@\u200b123456789012345678>" in entry or "ab <\u200b@" in entry
    assert "**bold**" not in entry


def test_zwj_sequences_survive_on_the_page():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    entry = _entry_of(_current(1, {"category": "cosmetic", "reward": f"{BIDI}{family} outfit"}))
    assert f"{family} outfit" in entry


# --- /shift codes: paging by size ---


def _long_entries(n: int, *, category: str = "permanent", start: int = 0) -> list[CurrentCode]:
    return [
        _current(
            i,
            {
                "category": category,
                "reward": "\U0001f600" * 149 + "ab",  # 300 units shown
                "origin": "x" * 299 + "y",
            },
            first_seen_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        for i in range(start, start + n)
    ]


def test_long_entries_fill_pages_by_size_and_never_split():
    entries = _long_entries(60)
    embeds = _pages(entries)
    assert len(embeds) > 3
    for i, embed in enumerate(embeds, start=1):
        assert discord_len(embed.description) <= 4096
        assert _embed_total(embed) <= 6000
        assert embed.footer.text.endswith(f"Page {i} of {len(embeds)}")
    text = _all_text(embeds)
    for entry in entries:
        assert text.count(f"`{entry.code}`") == 1
    # Every entry is whole: its code line and its reward line sit on the same page.
    for embed in embeds:
        for block in embed.description.split("\n\n"):
            if "`" in block:
                assert block.count("\n") >= 1
                assert block.endswith("x" * 299 + "y")


def test_pages_are_actually_full():
    embeds = _pages(_long_entries(60))
    for embed in embeds[:-1]:
        assert discord_len(embed.description) > 4096 - 700  # no page leaves a whole entry's room


def test_a_group_that_runs_onto_another_page_gets_its_heading_again():
    embeds = _pages(_long_entries(30))
    assert embeds[0].description.startswith("**Permanent**\n")
    for embed in embeds[1:]:
        assert embed.description.startswith("**Permanent (continued)**\n")


def test_a_new_group_starts_on_the_page_where_the_last_one_ended_if_it_fits():
    entries = [_current(1, {"category": "golden"}), _current(2, {"category": "permanent"})]
    [embed] = _pages(entries)
    assert "**Golden Keys**" in embed.description
    assert "**Permanent**" in embed.description


def test_a_second_group_spilling_over_repeats_only_its_own_heading():
    entries = _long_entries(2, category="golden") + _long_entries(
        20, category="permanent", start=100
    )
    embeds = _pages(entries)
    assert embeds[0].description.startswith("**Golden Keys**")
    for embed in embeds[1:]:
        assert "Golden Keys" not in embed.description
        assert embed.description.startswith("**Permanent (continued)**")


def test_a_hundred_mixed_entries_stay_inside_discords_limits():
    entries = []
    for i in range(100):
        entries.append(
            _current(
                i,
                {
                    "category": ("golden", "cosmetic", "permanent", "other")[i % 4],
                    "reward": ("*" * 150) if i % 3 else "\U0001f600" * 149,
                    "origin": "@everyone" * 33 if i % 2 else "",
                    "expires_on": date(2026, 12, 1) if i % 5 else None,
                },
            )
        )
    embeds = _pages(entries)
    assert sum(_all_text(embeds).count(f"`{e.code}`") for e in entries) == 100
    for embed in embeds:
        assert discord_len(embed.description) <= 4096
        assert _embed_total(embed) <= 6000


# --- /shift history ---


def _view(i: int = 0, details: dict | None = None, **kwargs) -> CodeView:
    code = _code(i)
    row = _details(code, **details) if details is not None else None
    defaults = dict(
        code=code,
        first_seen_at=SEEN,
        source_name="Gearbox Blog",
        item_url="https://e.com/a",
        status="posted",
        from_roundup=False,
        details=row,
    )
    defaults.update(kwargs)
    return CodeView(**defaults)


def _history(views):
    return render_history_pages(
        views, timezone="America/Los_Angeles", now=NOW, title="/shift history"
    )


def test_history_shows_the_details_line_under_the_source_line():
    view = _view(1, {"reward": "3 Golden Keys", "reward_from": "mentalmars"})
    [embed] = _history([view])
    assert embed.description == (
        f"```\n{_code(1)}\n```\nFirst seen 2026-09-25 · Gearbox Blog · <https://e.com/a>\n"
        "3 Golden Keys · details via mentalmars"
    )


def test_history_keeps_its_relative_wording_and_has_no_live_timestamp():
    views = [
        _view(1, {"reward": "x", "reward_from": "m", "expires_on": date(2026, 10, 9)}),
        _view(2, {"reward": "x", "reward_from": "m", "expires_on": date(2026, 10, 10)}),
        _view(3, {"reward": "x", "reward_from": "m", "expires_on": date(2026, 10, 14)}),
    ]
    [embed] = _history(views)
    assert "x · expires in 12 hours · details via m" in embed.description
    assert "x · expires tomorrow · details via m" in embed.description
    assert "x · expires Oct 14, 2026 · details via m" in embed.description
    assert "<t:" not in embed.description


def test_history_without_details_looks_like_before():
    [embed] = _history([_view(1)])
    assert embed.description == (
        f"```\n{_code(1)}\n```\nFirst seen 2026-09-25 · Gearbox Blog · <https://e.com/a>"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"from_roundup": True},
        {"status": "too_old"},
        {"status": "seeded"},
        {"from_roundup": True, "status": "too_old"},
    ],
)
def test_expired_wins_over_every_other_marker(kwargs):
    view = _view(1, {"expires_on": date(2026, 10, 1), "reward": "x"}, **kwargs)
    [embed] = _history([view])
    assert "(expired)" in embed.description
    for other in ("roundup", "old post", "already around"):
        assert other not in embed.description


def test_a_code_that_has_not_expired_keeps_its_old_marker():
    view = _view(1, {"expires_on": date(2026, 10, 9)}, status="seeded")
    [embed] = _history([view])
    assert "(already around when alerts started)" in embed.description
    assert "expired" not in embed.description.replace("expires in", "")


def test_a_permanent_code_is_never_expired_in_history():
    view = _view(1, {"category": "permanent", "expires_on": date(2020, 1, 1)})
    [embed] = _history([view])
    assert "(expired)" not in embed.description


def test_history_pages_by_size():
    views = [
        _view(
            i,
            {
                "reward": "*" * 150,
                "reward_from": "m" * 100,
                "expires_on": date(2026, 12, 31),
                "expires_from": "n" * 100,
            },
            source_name="s" * 100,
            item_url="https://e.com/" + "a" * 500,
        )
        for i in range(40)
    ]
    embeds = _history(views)
    assert len(embeds) > 1
    text = _all_text(embeds)
    for view in views:
        assert text.count(f"```\n{view.code}\n```") == 1
    for i, embed in enumerate(embeds, start=1):
        assert discord_len(embed.description) <= 4096
        assert _embed_total(embed) <= 6000
        assert f"Page {i} of {len(embeds)}" in embed.footer.text


def test_history_empty_and_title():
    [embed] = _history([])
    assert embed.description == "No codes seen in that window."
    assert embed.title == "/shift history"
    [escaped] = render_history_pages(
        [], timezone="America/Los_Angeles", now=NOW, title="@everyone *x*"
    )
    assert "@everyone" not in escaped.title


def test_history_bidi_in_a_reward_is_stripped():
    view = _view(1, {"reward": f"Gold{BIDI}en Key", "reward_from": "m"})
    [embed] = _history([view])
    assert "Golden Key · details via m" in embed.description
