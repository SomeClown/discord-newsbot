"""The terminal look on `/shift codes` and `/shift history` (plan 2026-10-22, Task 4).

Both commands are paged embeds, and the look comes from four things: a green stripe on every
page (the empty one too), a fixed prompt title, `[golden keys]` headings that say
"(continued)" when a group runs onto the next page, and an entry that is a fence with its
labels underneath. This file pins those, and the one rule the whole look hangs on: chips
hold fixed words only, and the code is alone in its fence, however nasty the reward, origin
or source name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies

from newsbot.bot import terminal
from newsbot.bot.format import (
    _code_page_block,
    discord_len,
    render_current_pages,
    render_history_pages,
)
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # noon in Pacific time, Oct 9
SEEN = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
FOOTER = "Codes from Gearbox and mentalmars.com"
EMPTY = "No current codes."
KINDS = ("all", "golden", "cosmetic", "permanent")
TITLES = {
    "all": "> current SHiFT codes ▌",
    "golden": "> current SHiFT codes: golden keys ▌",
    "cosmetic": "> current SHiFT codes: cosmetics ▌",
    "permanent": "> current SHiFT codes: permanent ▌",
}
HOSTILE = (
    "`@everyone`",
    "```",
    "``` \n@here",
    "[x](http://evil.example) <@&123456789012345678>",
    "\\` `",
    "> quote\n# big",
)


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _row(code: str, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code, clock_start=SEEN, updated_at=SEEN)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _current(i: int = 0, details: dict | None = None, **kwargs) -> CurrentCode:
    row = _row(_code(i), **details) if details is not None else None
    defaults = dict(code=_code(i), first_seen_at=SEEN - timedelta(minutes=i), details=row)
    defaults.update(kwargs)
    return CurrentCode(**defaults)


def _view(i: int = 0, details: dict | None = None, **kwargs) -> CodeView:
    row = _row(_code(i), **details) if details is not None else None
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


def _pages(entries, kind="all"):
    return render_current_pages(entries, kind=kind, now=NOW, footer=FOOTER, empty=EMPTY)


def _history(views):
    return render_history_pages(views, timezone="America/Los_Angeles", now=NOW)


def _big(n: int, category: str, start: int = 0) -> list[CurrentCode]:
    return [
        _current(i, {"category": category, "reward": "r" * 300, "origin": "o" * 300})
        for i in range(start, start + n)
    ]


# --- the stripe ---


def test_the_green_is_the_site_green():
    assert terminal.SHIFT_GREEN == 0x3FE37F


@pytest.mark.parametrize("kind", KINDS)
def test_every_page_of_every_type_is_green_the_empty_one_included(kind):
    for entries in (
        [],
        _big(40, "golden") + _big(40, "cosmetic", 100) + _big(40, "permanent", 200),
    ):
        embeds = _pages(entries, kind)
        assert embeds
        assert {e.color.value for e in embeds} == {terminal.SHIFT_GREEN}


def test_every_page_of_history_is_green_the_empty_one_included():
    assert {e.color.value for e in _history([])} == {0x3FE37F}
    views = [_view(i, {"reward": "x" * 300}, source_name="s" * 100) for i in range(80)]
    embeds = _history(views)
    assert len(embeds) > 1
    assert {e.color.value for e in embeds} == {0x3FE37F}


# --- the titles ---


@pytest.mark.parametrize("kind", KINDS)
def test_the_titles_are_exactly_these(kind):
    for entries in (
        [],
        _big(30, "golden") + _big(30, "cosmetic", 100) + _big(30, "permanent", 200),
    ):
        assert {e.title for e in _pages(entries, kind)} == {TITLES[kind]}
    assert terminal.PAGE_TITLES_CURRENT[kind] == TITLES[kind]


def test_the_history_title_is_exactly_this_with_no_backslash_in_it():
    for views in ([], [_view(0)]):
        assert {e.title for e in _history(views)} == {"> SHiFT code history ▌"}


def test_history_takes_no_title():
    with pytest.raises(TypeError):
        render_history_pages([], timezone="UTC", now=NOW, title="@everyone")  # type: ignore[call-arg]


# --- headings ---


def test_the_headings_are_the_lowercase_words_as_chips():
    entries = [
        _current(1, {"category": "golden"}),
        _current(2, {"category": "cosmetic"}),
        _current(3, {"category": "permanent"}),
        _current(4),
    ]
    [embed] = _pages(entries)
    assert embed.description.splitlines()[0] == "`[golden keys]`"
    for heading in ("`[golden keys]`", "`[cosmetics]`", "`[permanent]`", "`[other]`"):
        assert embed.description.count(heading) == 1
    assert "**" not in embed.description


def test_a_group_onto_a_second_page_gets_a_continued_heading_and_only_it_does():
    embeds = _pages(_big(3, "golden") + _big(14, "permanent", 100))
    assert len(embeds) >= 2
    assert embeds[0].description.startswith("`[golden keys]`\n")
    assert embeds[0].description.count("`[permanent]`") == 1
    for embed in embeds[1:]:
        assert embed.description.startswith("`[permanent (continued)]`\n")
        assert "`[golden keys" not in embed.description
    assert "(continued)" not in embeds[0].description


def test_every_heading_the_pages_draw_is_an_allowed_chip():
    embeds = _pages(_big(10, "golden") + _big(10, "cosmetic", 100) + _big(10, "permanent", 200))
    drawn = {c for e in embeds for c in chips(e.description) if c.startswith("[")}
    assert drawn
    assert drawn <= ALLOWED_CHIPS


# --- the entry ---


def test_a_300_unit_reward_and_a_300_unit_origin_fit_a_page():
    entry = _current(1, {"category": "permanent", "reward": "r" * 300, "origin": "o" * 300})
    [embed] = _pages([entry])
    assert embed.description == (
        f"`[permanent]`\n```\n{_code(1)}\n```\n`reward` {'r' * 300} · `expires` never · {'o' * 300}"
    )
    assert discord_len(embed.description) < 4096


def test_the_origin_is_plain_text_after_never():
    [embed] = _pages([_current(1, {"category": "permanent", "reward": "Head", "origin": "Twitch"})])
    assert embed.description.endswith("`reward` Head · `expires` never · Twitch")
    assert chips(embed.description) == ["[permanent]", "reward", "expires"]


def test_a_current_entry_with_nothing_known():
    [embed] = _pages([_current(1)])
    assert embed.description.endswith("`reward` unknown · `expires` unknown")


def test_history_entry_layout_and_a_plain_marker():
    view = _view(1, {"reward": "3 Golden Keys", "reward_from": "mentalmars"}, status="seeded")
    [embed] = _history([view])
    assert embed.description == (
        f"```\n{_code(1)}\n```\n"
        "`reward` 3 Golden Keys · `via` mentalmars\n"
        "`src` Gearbox Blog · <https://e.com/a>\n"
        "`first seen` 2026-09-25 · already around when alerts started"
    )
    # The marker is a value, not a label: it is not a chip.
    assert chips(embed.description) == ["reward", "via", "src", "first seen"]


# --- the chip and fence invariants ---


@pytest.mark.parametrize("hostile", HOSTILE)
def test_current_pages_keep_text_out_of_chips_and_the_code_alone_in_its_fence(hostile):
    entries = [
        _current(
            i,
            {"category": cat, "reward": hostile, "origin": hostile},
            first_seen_at=SEEN - timedelta(minutes=i),
        )
        for i, cat in enumerate(("golden", "cosmetic", "permanent", "other"))
    ]
    for kind in KINDS:
        for embed in _pages(entries, kind):
            assert set(chips(embed.description)) <= ALLOWED_CHIPS
            assert all(body.count("\n") == 0 for body in fence_bodies(embed.description))
            assert set(fence_bodies(embed.description)) <= {e.code for e in entries}


@pytest.mark.parametrize("hostile", HOSTILE)
def test_history_pages_keep_text_out_of_chips_and_the_code_alone_in_its_fence(hostile):
    views = [
        _view(
            i,
            {"reward": hostile, "reward_from": hostile, "expires_from": hostile},
            source_name=hostile,
            item_url="https://e.com/" + "a" * 50,
            status=("posted", "too_old", "seeded")[i % 3],
            from_roundup=i % 4 == 0,
        )
        for i in range(12)
    ]
    for embed in _history(views):
        assert set(chips(embed.description)) <= ALLOWED_CHIPS
        assert set(fence_bodies(embed.description)) <= {v.code for v in views}
        assert len(fence_bodies(embed.description)) == embed.description.count("```") // 2


def test_a_hostile_title_cannot_be_asked_for_by_a_type_value():
    with pytest.raises(ValueError, match="type"):
        _pages([_current(1)], "`> pwned ▌`")


# --- history: the shedding order ---


def test_a_4000_unit_url_sheds_its_link_and_keeps_everything_else():
    view = _view(
        1, {"reward": "x" * 300, "reward_from": "m" * 100}, item_url="https://e.com/" + "a/" * 2000
    )
    [embed] = _history([view])
    assert "<https://" not in embed.description
    assert discord_len(embed.description) <= 4096
    assert embed.description.endswith("`src` Gearbox Blog\n`first seen` 2026-09-25")
    assert "x" * 300 in embed.description


def test_when_the_link_is_gone_and_it_still_does_not_fit_the_source_name_is_cut():
    view = _view(
        1,
        {"reward": "x" * 300, "reward_from": "m" * 100},
        source_name="Reddit " + "m" * 93,  # 100 units: the most a name shows
        item_url="https://e.com/" + "a/" * 2000,
    )
    uncut = _code_page_block(view, "UTC", max_len=4096, now=NOW)
    assert "…" not in uncut
    tight = discord_len(uncut) - 40
    cut = _code_page_block(view, "UTC", max_len=tight, now=NOW)
    assert discord_len(cut) <= tight
    assert "<https://" not in cut
    assert "…\n`first seen` 2026-09-25" in cut
    # Everything but the source name is untouched, fence and details included.
    assert cut.startswith(uncut.split("`src`")[0] + "`src` Reddit m")
    assert cut.endswith("`first seen` 2026-09-25")


def test_the_marker_survives_the_cut_and_so_does_the_fence():
    view = _view(1, source_name="s" * 100, status="too_old", item_url="https://e.com/" + "a" * 500)
    for max_len in range(60, 400, 7):
        block = _code_page_block(view, "UTC", max_len=max_len, now=NOW)
        assert block.startswith(f"```\n{_code(1)}\n```\n`src` ")
        assert block.endswith("`first seen` 2026-09-25 · old post")
        assert block.count("```") == 2
