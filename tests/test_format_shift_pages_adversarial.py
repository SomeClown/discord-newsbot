"""Adversarial round on `render_current_pages` and `render_history_pages` (design.md \u00a716).

`test_format_shift_pages.py` pins the shapes. This file pushes on the edges Discord enforces
and the ones the paging has to get right by itself: a description of exactly 4,096 units
and one unit more, an embed total of 6,000, a title of 256, a footer of 2,048, one page
against a hundred, "(continued)" headings across three pages, every `type` filter against
an empty list, the sort order at the instant a table's Pacific "Added" day turns into
a first sighting, permanent codes, the "unknown" wording, `CodeView`s from before 3.1.0
(`details=None`) and the "expired" marker at the exact instant a day ends.

Invisible and bidi characters are `\\uXXXX` escapes on purpose; this file stays ASCII.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies

from newsbot.bot import terminal
from newsbot.bot.format import (
    discord_len,
    render_current_pages,
    render_history_pages,
)
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # noon PDT, Oct 9
SEEN = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
FOOTER = "Codes from Gearbox and mentalmars.com \u00b7 redeem at shift.gearboxsoftware.com"
EMPTY = "No current codes. I'll post new ones in <#123> when they show up."
KINDS = ("all", "golden", "cosmetic", "permanent")
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _details(code: str, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code, clock_start=SEEN, updated_at=SEEN)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _current(i: int = 0, details: dict | None = None, **kwargs) -> CurrentCode:
    row = _details(_code(i), **details) if details is not None else None
    defaults = dict(code=_code(i), first_seen_at=SEEN, details=row)
    defaults.update(kwargs)
    return CurrentCode(**defaults)


def _pages(entries, kind="all", *, now=NOW, footer=FOOTER, empty=EMPTY):
    return render_current_pages(entries, kind=kind, now=now, footer=footer, empty=empty)


def _text(embeds) -> str:
    return "\n".join(e.description for e in embeds)


def _within_discords_limits(embed) -> None:
    assert discord_len(embed.description) <= 4096
    assert discord_len(embed.title) <= 256
    assert discord_len(embed.footer.text) <= 2048
    total = (
        discord_len(embed.title) + discord_len(embed.description) + discord_len(embed.footer.text)
    )
    assert total <= 6000


def _hostile_entry(i: int, category: str | None) -> CurrentCode:
    """An entry as big as the caps let it get, in the nastiest mix of units."""
    details = {
        "reward": [
            "*" * 150,
            "\U0001f600" * 150,
            "@everyone" * 30,
            "e\u0301" * 150,
            FAMILY * 37 + "xxxx",
            "x" * 300,
        ][i % 6],
        "origin": ["_" * 150, "@here" * 50, "\U0001f601" * 150, "y" * 300][i % 4],
        "expires_on": [None, date(2026, 10, 9), date(2027, 3, 1), date(2026, 10, 10)][i % 4],
        "added_on": [None, date(2026, 10, 1)][i % 2],
    }
    if category is not None:
        details["category"] = category
    return _current(i, details, first_seen_at=SEEN - timedelta(minutes=i))


def _mixed(n: int) -> list[CurrentCode]:
    cats = ("golden", "cosmetic", "permanent", None, "other")
    return [_hostile_entry(i, cats[i % len(cats)]) for i in range(n)]


# --- Discord's limits, one page to many ---


@pytest.mark.parametrize("n", [0, 1, 2, 7, 8, 9, 30, 100, 300])
@pytest.mark.parametrize("kind", KINDS)
def test_current_pages_stay_inside_every_discord_limit(n, kind):
    entries = _mixed(n)
    embeds = _pages(entries, kind)
    assert embeds
    for embed in embeds:
        _within_discords_limits(embed)
    wanted = [
        e.code for e in entries if kind == "all" or (e.details and e.details.category == kind)
    ]
    text = _text(embeds)
    assert sorted(b for e in embeds for b in fence_bodies(e.description)) == sorted(wanted)
    for code in wanted:
        assert text.count(f"```\n{code}\n```") == 1
    for embed in embeds:
        assert set(chips(embed.description)) <= ALLOWED_CHIPS
        assert embed.color.value == terminal.SHIFT_GREEN


def test_one_page_is_page_1_of_1_and_a_hundred_pages_count_up_to_a_hundred():
    [only] = _pages(_mixed(3))
    assert only.footer.text == f"{FOOTER} \u00b7 Page 1 of 1"
    big = _pages(_big(700, "permanent"))
    assert len(big) >= 100
    assert [e.footer.text for e in big] == [
        f"{FOOTER} \u00b7 Page {i} of {len(big)}" for i in range(1, len(big) + 1)
    ]
    assert len({e.title for e in big}) == 1


def test_a_footer_is_passed_through_untouched_apart_from_the_page_count():
    # The footer is the command's fixed text; the pager adds only "Page i of n" to it.
    footer = "f" * 100
    [embed] = _pages(_mixed(1), footer=footer)
    assert embed.footer.text == f"{footer} \u00b7 Page 1 of 1"


def _entry_with_reward(length: int, i: int = 0) -> CurrentCode:
    return _current(i, {"reward": "r" * length})


def test_a_description_of_exactly_4096_units_is_one_page_and_one_unit_more_is_two():
    # Other-group entries: "`[other]`\n" then entries joined by a blank line.
    def build(last: int):
        return [_entry_with_reward(50, i) for i in range(33)] + [_entry_with_reward(last, 33)]

    exact = None
    for last in range(0, 301):
        embeds = _pages(build(last))
        if len(embeds) == 1 and discord_len(embeds[0].description) == 4096:
            exact = last
            break
    assert exact is not None, "a 4096-unit page should be reachable by tuning the last reward"
    [full] = _pages(build(exact))
    assert discord_len(full.description) == 4096
    over = _pages(build(exact + 1))
    assert len(over) == 2
    assert over[1].description.startswith("`[other (continued)]`")
    assert f"```\n{_code(33)}\n```" in over[1].description
    assert all(discord_len(e.description) <= 4096 for e in over)


def test_the_title_is_one_line_inside_256_whatever_the_filter():
    for kind in KINDS:
        for embed in _pages(_mixed(30), kind):
            assert discord_len(embed.title) <= 256
            assert "\n" not in embed.title


def test_the_title_is_the_constant_whatever_the_input():
    # No caller can hand either command a title: `/shift codes` picks one by `type:` and
    # `/shift history` has no title parameter at all. The constants are plain text with a
    # prompt and a cursor in them, and none of them goes through `esc()`.
    for kind, want in terminal.PAGE_TITLES_CURRENT.items():
        for embed in _pages(_mixed(30), kind):
            assert embed.title == want
            assert "\\" not in embed.title
    [embed] = render_history_pages([_view(0)], timezone="America/Los_Angeles", now=NOW)
    assert embed.title == terminal.PAGE_TITLE_HISTORY
    assert "\\" not in embed.title
    with pytest.raises(TypeError):
        render_history_pages([], timezone="UTC", now=NOW, title="@everyone")  # type: ignore[call-arg]


# --- headings and "(continued)" ---


def _big(n: int, category: str, start: int = 0) -> list[CurrentCode]:
    return [
        _current(
            i,
            {"category": category, "reward": "\U0001f600" * 149 + "ab", "origin": "o" * 300},
            first_seen_at=SEEN - timedelta(minutes=i),
        )
        for i in range(start, start + n)
    ]


def test_a_group_over_three_pages_says_continued_on_the_second_and_third_only():
    embeds = _pages(_big(25, "permanent"))
    assert len(embeds) >= 3
    assert embeds[0].description.startswith("`[permanent]`\n")
    for embed in embeds[1:]:
        assert embed.description.startswith("`[permanent (continued)]`\n")
    for embed in embeds:
        assert embed.description.count("`[permanent") == 1


def test_every_page_starts_with_a_heading_and_none_ends_on_one():
    embeds = _pages(_big(9, "golden") + _big(9, "cosmetic", 100) + _big(9, "permanent", 200))
    for embed in embeds:
        first = embed.description.splitlines()[0]
        assert re.fullmatch(r"`\[[a-z ]+( \(continued\))?\]`", first), first
        assert not embed.description.rstrip().endswith("]`")


def test_a_heading_says_continued_exactly_when_the_group_was_already_shown():
    embeds = _pages(
        _big(9, "golden")
        + _big(9, "cosmetic", 100)
        + _big(9, "permanent", 200)
        + _big(9, None, 300)
    )
    seen = set()
    for embed in embeds:
        headings = re.findall(
            r"(?m)^`\[(golden keys|cosmetics|permanent|other)( \(continued\))?\]`$",
            embed.description,
        )
        assert len({name for name, _ in headings}) == len(headings), (
            "one heading per group per page"
        )
        for name, continued in headings:
            assert bool(continued) == (name in seen), (name, continued)
            seen.add(name)
    assert seen == {"golden keys", "cosmetics", "permanent", "other"}


def test_each_groups_first_heading_appears_once_without_continued():
    embeds = _pages(_big(14, "golden") + _big(14, "cosmetic", 100) + _big(14, "permanent", 200))
    text = _text(embeds)
    for name in ("golden keys", "cosmetics", "permanent"):
        assert text.count(f"`[{name}]`") == 1
        assert f"`[{name} (continued)]`" in text
    first = [text.index(f"`[{name}]`") for name in ("golden keys", "cosmetics", "permanent")]
    assert first == sorted(first)


def test_the_filter_title_and_only_that_groups_heading():
    embeds = _pages(_mixed(40), "cosmetic")
    assert {e.title for e in embeds} == {"> current SHiFT codes: cosmetics \u258c"}
    for embed in embeds:
        assert "golden keys" not in embed.description
        assert "`[permanent" not in embed.description


def test_an_entry_is_never_split_across_pages():
    entries = _big(30, "permanent")
    embeds = _pages(entries)
    single = {e.code: _pages([e])[0].description.split("\n", 1)[1] for e in entries}
    for entry in entries:
        homes = [e for e in embeds if single[entry.code] in e.description]
        assert len(homes) == 1, entry.code


# --- empty results ---


@pytest.mark.parametrize("kind", KINDS)
def test_no_entries_is_the_empty_text_for_every_filter(kind):
    [embed] = _pages([], kind)
    assert embed.description == EMPTY
    assert embed.footer.text == f"{FOOTER} \u00b7 Page 1 of 1"
    assert embed.title == terminal.PAGE_TITLES_CURRENT[kind]
    assert embed.color.value == terminal.SHIFT_GREEN


@pytest.mark.parametrize("kind", ["golden", "cosmetic", "permanent"])
def test_entries_in_other_groups_only_is_the_empty_text_with_the_filters_title(kind):
    others = [
        _current(i, {"category": c})
        for i, c in enumerate({"golden", "cosmetic", "permanent", "other"} - {kind})
    ] + [_current(9)]
    [embed] = _pages(others, kind)
    assert embed.description == EMPTY
    assert embed.title == terminal.PAGE_TITLES_CURRENT[kind]
    assert embed.color.value == terminal.SHIFT_GREEN


@pytest.mark.parametrize("kind", ["", "ALL", "Golden", "other", "everything", "all ", None, 0])
def test_an_unknown_type_is_an_error_even_with_nothing_to_show(kind):
    with pytest.raises(ValueError, match="type"):
        _pages([], kind)
    with pytest.raises(ValueError, match="type"):
        _pages([_current(1)], kind)


# --- order ---


def test_newest_first_flips_exactly_at_pacific_midnight_of_the_added_day():
    added = _current(1, {"category": "golden", "added_on": date(2026, 10, 7)})
    midnight = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)  # Oct 7, 00:00 PDT
    before = _current(2, {"category": "golden"}, first_seen_at=midnight - timedelta(seconds=1))
    after = _current(3, {"category": "golden"}, first_seen_at=midnight + timedelta(seconds=1))
    text = _text(_pages([before, added, after]))
    assert text.index(_code(3)) < text.index(_code(1)) < text.index(_code(2))


def test_a_tie_keeps_the_order_it_was_given():
    entries = [_current(i, {"category": "golden", "added_on": date(2026, 10, 1)}) for i in range(8)]
    text = _text(_pages(entries))
    assert [text.index(_code(i)) for i in range(8)] == sorted(
        text.index(_code(i)) for i in range(8)
    )
    text = _text(_pages(list(reversed(entries))))
    positions = [text.index(_code(i)) for i in reversed(range(8))]
    assert positions == sorted(positions)


def test_a_naive_first_sighting_sorts_as_utc_next_to_aware_ones():
    naive = _current(1, None, first_seen_at=datetime(2026, 10, 5, 12, 0))
    aware_earlier = _current(2, None, first_seen_at=datetime(2026, 10, 5, 11, 0, tzinfo=UTC))
    aware_later = _current(3, None, first_seen_at=datetime(2026, 10, 5, 13, 0, tzinfo=UTC))
    text = _text(_pages([aware_earlier, naive, aware_later]))
    assert text.index(_code(3)) < text.index(_code(1)) < text.index(_code(2))


def test_the_order_holds_across_a_page_break():
    entries = [
        _current(
            i,
            {"category": "permanent", "reward": "r" * 300, "origin": "o" * 300},
            first_seen_at=SEEN + timedelta(minutes=i),
        )
        for i in range(40)
    ]
    embeds = _pages(entries)
    assert len(embeds) > 2
    codes_in_order = re.findall(r"```\n(\d{4})A-BCDEF", _text(embeds))
    assert codes_in_order == [f"{i:04d}" for i in reversed(range(40))]


def test_group_order_does_not_depend_on_input_order():
    entries = [
        _current(1, {"category": "permanent"}),
        _current(2, None),
        _current(3, {"category": "cosmetic"}),
        _current(4, {"category": "golden"}),
    ]
    forward = _text(_pages(entries))
    backward = _text(_pages(list(reversed(entries))))
    assert forward == backward


# --- entry wording ---


def test_a_permanent_code_with_no_reward_and_no_origin():
    [embed] = _pages([_current(1, {"category": "permanent"})])
    assert embed.description.endswith(
        f"```\n{_code(1)}\n```\n`reward` unknown \u00b7 `expires` never"
    )


def test_a_permanent_code_with_an_origin_and_no_reward():
    [embed] = _pages([_current(1, {"category": "permanent", "origin": "Twitch drop"})])
    assert embed.description.endswith(
        f"```\n{_code(1)}\n```\n`reward` unknown \u00b7 `expires` never \u00b7 Twitch drop"
    )


def test_a_row_with_nothing_in_it_is_other_with_both_unknowns():
    [embed] = _pages([_current(1, {})])
    assert embed.description == (
        f"`[other]`\n```\n{_code(1)}\n```\n`reward` unknown \u00b7 `expires` unknown"
    )


def test_an_expired_code_that_slipped_into_the_list_says_so():
    [embed] = _pages([_current(1, {"reward": "r", "expires_on": date(2026, 10, 8)})])
    assert embed.description.endswith("`reward` r \u00b7 `expired` Oct 8, 2026")


def test_a_multiline_reward_cannot_forge_a_heading_or_a_second_entry():
    reward = "line one\n\n**Golden Keys**\n`" + _code(99) + "`\n# big\n-# small\n> quote"
    [embed] = _pages([_current(1, {"category": "cosmetic", "reward": reward})])
    assert "**Golden Keys**" not in embed.description
    assert "`[golden keys]`" not in embed.description
    assert fence_bodies(embed.description) == [_code(1)]  # the entry's own code only
    assert set(chips(embed.description)) <= ALLOWED_CHIPS


def test_the_reward_is_cut_to_300_shown_units_on_the_page_too():
    [embed] = _pages([_current(1, {"reward": "*" * 400})])
    reward_line = embed.description.splitlines()[-1].split(" \u00b7 ")[0].removeprefix("`reward` ")
    assert discord_len(reward_line) <= 300
    assert reward_line.endswith("\u2026")


# --- /shift history ---


def _view(i: int = 0, details: dict | None = None, **kwargs) -> CodeView:
    row = _details(_code(i), **details) if details is not None else None
    defaults = dict(
        code=_code(i),
        first_seen_at=SEEN,
        source_name="Gearbox Blog",
        item_url="https://e.com/a",
        status="posted",
        from_roundup=False,
        details=row,
    )
    defaults.update(kwargs)
    return CodeView(**defaults)


def _history(views, *, now=NOW):
    return render_history_pages(views, timezone="America/Los_Angeles", now=now)


def test_history_with_no_details_is_the_old_page_block_for_block():
    # The old page renderer is gone; its output for these four views is kept as a fixed string.
    views = [
        _view(0),
        _view(1, status="too_old"),
        _view(2, from_roundup=True),
        _view(4, source_name="*" * 100),
    ]
    [new] = _history(views)

    def block(i, src, marker=""):
        return f"```\n{_code(i)}\n```\n`src` {src}\n`first seen` 2026-09-25{marker}"

    link = "<https://e.com/a>"
    assert new.description == "\n\n".join(
        [
            block(0, f"Gearbox Blog · {link}"),
            block(1, f"Gearbox Blog · {link}", " · old post"),
            block(2, f"Gearbox Blog · {link}", " · from a roundup"),
            block(4, "\\*" * 49 + "\\\N{HORIZONTAL ELLIPSIS}" + f" · {link}"),
        ]
    )


def test_history_marks_a_code_expired_only_from_the_instant_its_day_ends():
    d = date(2026, 10, 9)
    end = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)  # the end of Oct 9 in Pacific time
    view = _view(0, {"expires_on": d, "reward": "r"})
    [alive] = _history([view], now=end - timedelta(microseconds=1))
    [dead] = _history([view], now=end)
    assert "\u00b7 expired" not in alive.description
    assert dead.description.endswith("\u00b7 expired")


@pytest.mark.parametrize(
    "kwargs",
    [dict(status="too_old"), dict(from_roundup=True), dict(status="too_old", from_roundup=True)],
)
def test_expired_beats_every_other_marker_and_replaces_it(kwargs):
    view = _view(0, {"expires_on": date(2026, 10, 1)}, **kwargs)
    [embed] = _history([view])
    assert embed.description.endswith("\u00b7 expired")
    assert "from a roundup" not in embed.description
    assert "old post" not in embed.description


def test_a_permanent_code_with_a_past_expiry_is_never_marked_expired_in_history():
    view = _view(0, {"category": "permanent", "expires_on": date(2020, 1, 1)}, from_roundup=True)
    [embed] = _history([view])
    assert "expired" not in embed.description
    assert embed.description.endswith("\u00b7 from a roundup")


def test_a_row_without_an_expiry_keeps_its_old_marker():
    view = _view(0, {"reward": "r"}, status="too_old")
    [embed] = _history([view])
    assert "expired" not in embed.description
    assert embed.description.endswith("\u00b7 old post")


def test_history_mixes_pre_and_post_3_1_0_codes_on_one_page():
    views = [
        _view(0),  # a code from before 3.1.0: no details row at all
        _view(1, {"reward": "3 Golden Keys", "reward_from": "mentalmars"}),
        _view(2, {"expires_on": date(2026, 10, 1)}),
    ]
    [embed] = _history(views)
    blocks = embed.description.split("\n\n")
    assert len(blocks) == 3
    assert blocks[0].count("\n") == 4  # fence (3 lines), then `src` and `first seen`, no more
    assert "`reward` 3 Golden Keys \u00b7 `via` mentalmars" in blocks[1]
    assert "expired" in blocks[2]


def test_history_keeps_the_order_it_is_given():
    views = [_view(i, first_seen_at=SEEN - timedelta(days=i)) for i in (3, 1, 2)]
    text = _text(_history(views))
    assert text.index(_code(3)) < text.index(_code(1)) < text.index(_code(2))


def test_history_with_nothing_is_one_page_with_the_old_empty_text():
    [embed] = _history([])
    assert embed.description == "No codes seen in that window."
    assert embed.title == terminal.PAGE_TITLE_HISTORY
    assert embed.color.value == terminal.SHIFT_GREEN
    assert embed.footer.text.endswith("Page 1 of 1")


@pytest.mark.parametrize("n", [1, 8, 9, 50, 100, 600])
def test_history_pages_stay_inside_every_discord_limit(n):
    views = [
        _view(
            i,
            {
                "reward": ["*" * 150, "\U0001f600" * 150, "@everyone" * 30][i % 3],
                "reward_from": "m" * 100,
                "expires_on": [None, date(2026, 10, 9), date(2026, 10, 1)][i % 3],
                "expires_from": "n" * 100,
            }
            if i % 4
            else None,
            source_name=["*" * 100, "\U0001f600" * 100, "g" * 10][i % 3],
            item_url=["https://e.com/a", "https://e.com/" + "a/" * 1000, ""][i % 3],
            status=("posted", "too_old")[i % 2],
            from_roundup=i % 5 == 0,
        )
        for i in range(n)
    ]
    embeds = _history(views)
    for embed in embeds:
        _within_discords_limits(embed)
    text = _text(embeds)
    for view in views:
        assert text.count(view.code) == 1
    assert [e.footer.text.split("Page ")[-1] for e in embeds] == [
        f"{i} of {len(embeds)}" for i in range(1, len(embeds) + 1)
    ]


def test_history_over_a_hundred_pages_counts_up_to_the_end():
    views = [
        _view(
            i,
            {
                "reward": "\U0001f600" * 149 + "ab",
                "reward_from": "m" * 100,
                "expires_from": "n" * 100,
            },
            source_name="*" * 100,
        )
        for i in range(600)
    ]
    embeds = _history(views)
    assert len(embeds) >= 100
    assert embeds[-1].footer.text.endswith(f"Page {len(embeds)} of {len(embeds)}")
    assert sum(_text(embeds).count(v.code) for v in views) == 600


def test_history_entry_is_never_split_even_when_it_has_to_shed_its_link():
    views = [_view(i, item_url="https://e.com/" + "a/" * 1000) for i in range(30)]
    embeds = _history(views)
    for embed in embeds:
        for block in embed.description.split("\n\n"):
            assert block.startswith("```\n") and block.count("```") == 2
