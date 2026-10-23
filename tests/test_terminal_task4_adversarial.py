"""Adversarial round for Task 4 of the terminal look: the `/shift codes` and `/shift history` pages.

Task 4's own file (`test_format_terminal_pages.py`) pins the layout with friendly input and a
handful of hostile strings. This one goes looking for the seams. Every `type:` and history,
through the real command handlers and the renderers; exact titles with the bytes spelled out
here (so a typo in `terminal.py` can't agree with itself); green on every embed, the empty one
included; entries landing on exactly 4,096 UTF-16 units and on 4,097; a group running over
three or more pages; each history marker; hostile rewards, origins, source names and URLs run
through `chip_check` (its own parser, so `esc()` and the check would both have to be wrong in
the same way); and the relative expiry wording at the Pacific DST edges and the New Year, against
an oracle written here from `zoneinfo` and nothing else.

The oracle for lengths is `len(s.encode("utf-16-le")) // 2`, not `discord_len`, for the same
reason.
"""

from __future__ import annotations

import inspect
import re
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies
from v3_fakes import GUILD_A, FakeInteraction, command
from v3_fakes import make_guild as _make_guild

from newsbot.bot import format as fmt
from newsbot.bot import terminal
from newsbot.bot.commands import make_member_shift_group
from newsbot.bot.format import (
    _code_page_block,
    render_current_pages,
    render_history_pages,
)
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)  # 11:00 PDT, Friday, Oct 9, 2026
SEEN = NOW - timedelta(days=1)
CHANNEL = 777000000000000001
FOOTER = "Codes from Gearbox and mentalmars.com"
EMPTY = "No current codes."
CURSOR = "▌"
KINDS = ("all", "golden", "cosmetic", "permanent")

# Spelled out with escapes on purpose: nothing here is built from `terminal`.
TITLES = {
    "all": f"> current SHiFT codes {CURSOR}",
    "golden": f"> current SHiFT codes: golden keys {CURSOR}",
    "cosmetic": f"> current SHiFT codes: cosmetics {CURSOR}",
    "permanent": f"> current SHiFT codes: permanent {CURSOR}",
}
HISTORY_TITLE = f"> SHiFT code history {CURSOR}"
HISTORY_FOOTER_NOTE = "Where the expiry is unknown, older codes may have stopped working."
HEADINGS = {
    "golden": "`[golden keys]`",
    "cosmetic": "`[cosmetics]`",
    "permanent": "`[permanent]`",
    "other": "`[other]`",
}
CONTINUED = {
    "golden": "`[golden keys (continued)]`",
    "cosmetic": "`[cosmetics (continued)]`",
    "permanent": "`[permanent (continued)]`",
    "other": "`[other (continued)]`",
}

HOSTILE = (
    "`",
    "``",
    "```",
    "` `reward` `",
    "`reward`",
    "`expires` never",
    "`[golden keys]`",
    "[golden keys]",
    "`[golden keys (continued)]`",
    f"{CURSOR}",
    f"`> new SHiFT code {CURSOR}`",
    "> quote",
    "x\n> quote",
    "x\n>>> quote",
    "x\n# big",
    "x\n- item",
    "x\n```\nEVIL\n```",
    "\\",
    "\\`",
    "ends in a backslash\\",
    "@everyone",
    "@here",
    "<@&123456789012345678>",
    "<@123456789012345678>",
    "<#123456789012345678>",
    "<t:1:R>",
    "<t:1:R> `",
    "‮reversed‬",
    "⁦isolated",
    "x‮`reward`",
    "\U0001f916" * 150,
    "\U0001f916`",
    "a‍\U0001f468‍\U0001f469",
    "https://evil.example/x",
    "discord.gg/abc",
    "[click](http://evil.example)",
    "||spoiler||",
    "~~strike~~ **bold** __under__ *it*",
    "*" * 300,
)


def u16(text: str) -> int:
    """UTF-16 units, counted without the code under test."""
    return len(text.encode("utf-16-le")) // 2


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


def _pages(entries, kind="all", now=NOW):
    return render_current_pages(entries, kind=kind, now=now, footer=FOOTER, empty=EMPTY)


def _history(views, now=NOW, timezone="America/Los_Angeles"):
    return render_history_pages(views, timezone=timezone, now=now)


def _outside_fences(text: str) -> list[str]:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL).split("\n")


def assert_clean(description: str, codes: list[str]) -> None:
    """The rule the whole look hangs on, read from the outside."""
    assert set(chips(description)) <= ALLOWED_CHIPS
    # Each fence holds one code and only that code, in the order drawn.
    assert fence_bodies(description) == codes
    assert description.count("```") == 2 * len(codes)
    # Links in angle brackets are the history page's own `<url>`; a mention-shaped URL is
    # just a URL. Everything else must be defused (esc puts a zero-width space in).
    prose = re.sub(r"<https?://[^>\s]*>", "", description)
    for live in (r"<@[!&]?\d", r"<#\d", r"</", r"<t:\d", r"@everyone", r"@here"):
        assert not re.search(live, prose), live
    for line in _outside_fences(description):
        assert not re.match(r"(>|#|-#|- |\* )", line), repr(line)
    assert u16(description) <= 4096


# ===========================================================================
# The literal strings, pinned without terminal.py's help
# ===========================================================================


def test_the_vocabulary_is_spelled_the_way_the_plan_spells_it():
    assert terminal.CURSOR == "▌"
    assert (terminal.GROUP_GOLDEN_KEYS, terminal.GROUP_COSMETICS) == ("golden keys", "cosmetics")
    assert (terminal.GROUP_PERMANENT, terminal.GROUP_OTHER) == ("permanent", "other")
    assert terminal.PAGE_TITLE_CURRENT == "> current SHiFT codes ▌"
    assert terminal.PAGE_TITLE_HISTORY == "> SHiFT code history ▌"
    assert terminal.PAGE_TITLES_CURRENT == TITLES


def test_the_allowed_chip_set_holds_the_literal_headings_and_nothing_extra_for_groups():
    literal = {"[golden keys]", "[cosmetics]", "[permanent]", "[other]"}
    literal |= {f"{h[:-1]} (continued)]" for h in literal}
    assert literal <= ALLOWED_CHIPS
    bracketed = {c for c in ALLOWED_CHIPS if c.startswith("[") and c.endswith("]")}
    assert bracketed == literal


@pytest.mark.parametrize("group", ("golden", "cosmetic", "permanent", "other"))
def test_each_heading_is_the_literal_chip_in_the_page(group):
    category = None if group == "other" else group
    entry = _current(1, {"category": category, "reward": "x"})
    [embed] = _pages([entry])
    assert embed.description.split("\n")[0] == HEADINGS[group]


def test_heading_and_continued_heading_have_exactly_the_plan_s_shape():
    assert terminal.heading("golden keys") == "`[golden keys]`"
    assert terminal.heading("golden keys", continued=True) == "`[golden keys (continued)]`"


# ===========================================================================
# Titles and color through the renderers
# ===========================================================================


@pytest.mark.parametrize("kind", KINDS)
def test_title_is_the_constant_byte_for_byte_on_every_page(kind):
    entries = [_current(i, {"category": "golden", "reward": "r" * 300}) for i in range(40)]
    entries += [_current(100 + i, {"category": "cosmetic", "reward": "r" * 300}) for i in range(40)]
    entries += [_current(200 + i, {"category": "permanent"}) for i in range(40)]
    for pool in ([], entries):
        embeds = _pages(pool, kind)
        assert {e.title.encode("utf-8") for e in embeds} == {TITLES[kind].encode("utf-8")}
        assert {e.color.value for e in embeds} == {0x3FE37F}


def test_the_history_title_is_the_constant_byte_for_byte_on_every_page_empty_included():
    views = [_view(i, {"reward": "r" * 300}) for i in range(60)]
    for pool in ([], views):
        embeds = _history(pool)
        assert {e.title.encode("utf-8") for e in embeds} == {HISTORY_TITLE.encode("utf-8")}
        assert {e.color.value for e in embeds} == {0x3FE37F}


def test_no_title_has_a_backslash_or_a_chip_or_runs_over_the_cap():
    for title in (*TITLES.values(), HISTORY_TITLE):
        assert "\\" not in title and "`" not in title
        assert title.startswith("> ") and title.endswith(CURSOR)
        assert u16(title) <= 256


def test_history_refuses_a_title_argument():
    with pytest.raises(TypeError):
        render_history_pages([], timezone="UTC", now=NOW, title="> pwned")  # type: ignore[call-arg]
    assert "title" not in inspect.signature(render_history_pages).parameters


def test_nothing_that_renders_a_page_takes_a_title_from_a_caller():
    # `_page_embeds` is the only place a title enters an embed; the two public renderers
    # must be the only callers and must feed it constants.
    assert "title" not in inspect.signature(render_current_pages).parameters
    callers = [
        name
        for name, fn in inspect.getmembers(fmt, inspect.isfunction)
        if fn.__module__ == fmt.__name__ and "_page_embeds(" in inspect.getsource(fn)
    ]
    assert sorted(callers) == ["_page_embeds", "render_current_pages", "render_history_pages"]


@pytest.mark.parametrize("bad", ("> pwned", "", "GOLDEN", "all ", "`x`", "▌"))
def test_an_unknown_kind_raises_instead_of_inventing_a_title(bad):
    with pytest.raises(ValueError):
        _pages([_current(1)], bad)


@pytest.mark.parametrize("kind", KINDS)
def test_footers_are_unchanged_and_say_page_i_of_n(kind):
    entries = [_current(i, {"category": "permanent", "reward": "r" * 300}) for i in range(30)]
    embeds = _pages(entries, kind if kind in ("all", "permanent") else "all")
    n = len(embeds)
    assert n > 2
    assert [e.footer.text for e in embeds] == [
        f"{FOOTER} · Page {i} of {n}" for i in range(1, n + 1)
    ]
    [empty] = _pages([], kind)
    assert empty.footer.text == f"{FOOTER} · Page 1 of 1"
    assert empty.description == EMPTY


def test_history_footer_is_the_old_note_and_the_empty_page_is_the_old_sentence():
    [empty] = _history([])
    assert empty.description == "No codes seen in that window."
    assert empty.footer.text == f"{HISTORY_FOOTER_NOTE} · Page 1 of 1"


# ===========================================================================
# 4,096 exactly, and one past it
# ===========================================================================


def _sized(units: int, *, astral: bool, filler: str) -> str:
    """Text that is exactly `units` UTF-16 units long, in emoji pairs when `astral`."""
    if astral:
        return "\U0001f916" * (units // 2) + filler * (units % 2)
    return filler * units


def _tuned_current(target: int, *, category: str = "golden", astral: bool = False):
    """Entries of `category` whose one page is exactly `target` units long (oracle count).

    N full entries (a 300 unit reward, and a 300 unit origin when permanent) fill most of the
    page; the last two have their reward (and origin) sized between them to land on `target`.
    The second return value is the same list with the last entry one unit longer.
    """
    fields = ("reward", "origin") if category == "permanent" else ("reward",)
    slots = [(j, f) for j in (0, 1) for f in fields]

    def build(n_full: int, sizes: dict):
        entries = []
        for i in range(n_full):
            d = {"category": category, "reward": "r" * 300}
            if category == "permanent":
                d["origin"] = "o" * 300
            entries.append(_current(i, d))
        for j in (0, 1):
            d = {"category": category}
            for f in fields:
                d[f] = _sized(sizes[(j, f)], astral=astral, filler="x")
            entries.append(_current(n_full + j, d))
        return entries

    for n_full in range(0, 20):
        sizes = {slot: 1 for slot in slots}
        pages = _pages(build(n_full, sizes), category)
        if len(pages) != 1:
            break
        extra = target - u16(pages[0].description)
        # Keep one unit of room in the last entry for the "one more" variant.
        if not 0 <= extra <= 299 * len(slots) - 1:
            continue
        for slot in slots:
            take = min(extra, 299)
            sizes[slot] += take
            extra -= take
        tuned = build(n_full, sizes)
        pages = _pages(tuned, category)
        assert len(pages) == 1 and u16(pages[0].description) == target
        bumped = dict(sizes)
        bump = next(slot for slot in reversed(slots) if slot[0] == 1 and sizes[slot] < 300)
        bumped[bump] += 1
        return tuned, build(n_full, bumped)
    raise AssertionError(f"could not tune a {category} page to {target}")


@pytest.mark.parametrize("category", ("golden", "cosmetic", "permanent"))
@pytest.mark.parametrize("astral", (False, True))
def test_a_page_that_lands_on_exactly_4096_units_stays_one_page(category, astral):
    exact, one_more = _tuned_current(4096, category=category, astral=astral)
    [page] = _pages(exact, category)
    assert u16(page.description) == 4096
    assert_clean(page.description, [e.code for e in exact])
    # One more unit does not fit: the last entry moves over with a continued heading.
    pages = _pages(one_more, category)
    assert len(pages) == 2
    assert all(u16(p.description) <= 4096 for p in pages)
    assert pages[1].description.split("\n")[0] == CONTINUED[category]
    assert fence_bodies(pages[1].description) == [one_more[-1].code]
    assert [c for p in pages for c in fence_bodies(p.description)] == [e.code for e in one_more]


def test_a_group_ending_at_exactly_4096_lets_the_next_group_start_fresh_not_continued():
    exact, _ = _tuned_current(4096, category="golden")
    cosmetic = _current(900, {"category": "cosmetic", "reward": "x"})
    pages = _pages([*exact, cosmetic])
    assert len(pages) == 2
    assert u16(pages[0].description) == 4096
    assert pages[1].description.split("\n")[0] == HEADINGS["cosmetic"]
    assert "(continued)" not in pages[1].description


def test_one_unit_past_4096_continues_the_group_and_the_next_group_follows_it():
    _, over = _tuned_current(4096, category="golden")
    cosmetic = _current(900, {"category": "cosmetic", "reward": "x"})
    pages = _pages([*over, cosmetic])
    assert len(pages) == 2
    lines = pages[1].description.split("\n")
    assert lines[0] == CONTINUED["golden"]
    assert HEADINGS["cosmetic"] in lines
    assert lines.index(HEADINGS["cosmetic"]) > 0


def test_history_entry_at_exactly_4096_keeps_its_link_and_at_4097_sheds_it():
    base = _view(1, {"reward": "x" * 300, "reward_from": "m" * 100})

    def with_url(n: int) -> CodeView:
        return _view(
            1,
            {"reward": "x" * 300, "reward_from": "m" * 100},
            item_url="https://e.com/" + "a" * n,
        )

    probe = _history([with_url(1000)])[0].description
    n = 1000 + (4096 - u16(probe))
    [exact] = _history([with_url(n)])
    assert u16(exact.description) == 4096
    assert f"<https://e.com/{'a' * n}>" in exact.description
    [over] = _history([with_url(n + 1)])
    assert "<https://" not in over.description
    assert u16(over.description) <= 4096
    assert over.description.endswith("`src` Gearbox Blog\n`first seen` 2026-10-08")
    assert base.code in over.description


def test_history_entries_pack_to_exactly_4096_with_the_blank_line_between_them():
    # Two entries, the second tuned so their page (A, blank line, B) is exactly 4096 units.
    def mk(n: int) -> list[CodeView]:
        return [
            _view(1, {"reward": "x" * 300}, item_url="https://e.com/" + "a" * 1500),
            _view(2, {"reward": "x" * 300}, item_url="https://e.com/" + "b" * n),
        ]

    n = 1000
    first = _history(mk(n))[0].description
    n += 4096 - u16(first)
    pages = _history(mk(n))
    assert len(pages) == 1 and u16(pages[0].description) == 4096
    assert "<https://e.com/" + "b" * n + ">" in pages[0].description
    pages = _history(mk(n + 1))
    assert all(u16(p.description) <= 4096 for p in pages)
    # The second entry doesn't fit beside the first, but it sheds its link before it moves.
    assert [c for p in pages for c in fence_bodies(p.description)] == [_code(1), _code(2)]


# ===========================================================================
# A group across three (and more) pages; headings only where they belong
# ===========================================================================


def test_a_group_on_five_pages_has_one_plain_heading_then_continued_ones():
    entries = [
        _current(i, {"category": "permanent", "reward": "r" * 300, "origin": "o" * 300})
        for i in range(30)
    ]
    pages = _pages(entries)
    assert len(pages) >= 4
    for number, page in enumerate(pages):
        lines = page.description.split("\n")
        headings = [ln for ln in lines if ln.startswith("`[")]
        expected = HEADINGS["permanent"] if number == 0 else CONTINUED["permanent"]
        assert headings == [expected]
        assert lines[0] == expected  # the heading rides on the page's first entry
        assert_clean(page.description, fence_bodies(page.description))
    assert [c for p in pages for c in fence_bodies(p.description)] == [e.code for e in entries]


def test_three_groups_across_many_pages_follow_the_continued_rules():
    golden = [_current(i, {"category": "golden", "reward": "g" * 300}) for i in range(0, 14)]
    cosmetic = [_current(100 + i, {"category": "cosmetic", "reward": "c" * 300}) for i in range(14)]
    perm = [_current(200 + i, {"category": "permanent", "origin": "o" * 300}) for i in range(14)]
    other = [_current(300 + i, {"category": None, "reward": "n" * 300}) for i in range(14)]
    pages = _pages([*golden, *cosmetic, *perm, *other])
    assert len(pages) >= 4
    seen_groups: set[str] = set()
    drawn_codes: list[str] = []
    for page in pages:
        lines = page.description.split("\n")
        on_this_page: set[str] = set()
        for line in lines:
            for g in HEADINGS:
                if line in (HEADINGS[g], CONTINUED[g]):
                    assert g not in on_this_page, f"{g} headed twice on one page"
                    on_this_page.add(g)
                    # Plain the first time a group appears anywhere, continued every time after.
                    assert line == (CONTINUED[g] if g in seen_groups else HEADINGS[g])
                    seen_groups.add(g)
        assert on_this_page
        drawn_codes += fence_bodies(page.description)
        # The page never ends on a stranded heading.
        assert not lines[-1].startswith("`[")
    assert drawn_codes == [e.code for e in [*golden, *cosmetic, *perm, *other]]


def test_each_code_sits_under_its_own_groups_heading_on_every_page():
    entries = []
    for base, category in ((0, "golden"), (100, "cosmetic"), (200, "permanent"), (300, None)):
        entries += [
            _current(base + i, {"category": category, "reward": "z" * 300}) for i in range(10)
        ]
    pages = _pages(entries)
    kind_of = {0: "golden", 1: "cosmetic", 2: "permanent", 3: "other"}
    heading_to_group = {v: k for k, v in HEADINGS.items()} | {v: k for k, v in CONTINUED.items()}
    for page in pages:
        group = None
        for line in page.description.split("\n"):
            if line in heading_to_group:
                group = heading_to_group[line]
            m = re.fullmatch(r"(\d{4})A-BCDEF-GHJKL-MNPQR-STUVW", line)
            if m:
                assert kind_of[int(m.group(1)) // 100] == group, line


def test_no_page_ends_on_a_heading_and_no_page_is_empty_or_over_the_cap():
    for n in range(1, 25):
        entries = [
            _current(i, {"category": "golden", "reward": "r" * (60 * n % 300)}) for i in range(n)
        ]
        entries += [
            _current(100 + i, {"category": "cosmetic", "reward": "c" * 290}) for i in range(n)
        ]
        for page in _pages(entries):
            assert page.description.strip()
            assert u16(page.description) <= 4096
            assert not page.description.rstrip().split("\n")[-1].startswith("`[")


def test_the_type_filter_keeps_only_that_group_and_its_title():
    entries = [
        _current(1, {"category": "golden", "reward": "g"}),
        _current(2, {"category": "cosmetic", "reward": "c"}),
        _current(3, {"category": "permanent"}),
        _current(4, {"category": None, "reward": "n"}),
    ]
    want = {"golden": 1, "cosmetic": 2, "permanent": 3}
    for kind, i in want.items():
        [embed] = _pages(entries, kind)
        assert fence_bodies(embed.description) == [_code(i)]
        assert embed.title == TITLES[kind]
        assert embed.description.split("\n")[0] == HEADINGS[kind]
    [everything] = _pages(entries, "all")
    assert fence_bodies(everything.description) == [_code(i) for i in (1, 2, 3, 4)]
    assert HEADINGS["other"] in everything.description


# ===========================================================================
# The /shift codes entry
# ===========================================================================


def test_permanent_with_origin_text_reads_origin_after_never_as_plain_text():
    [embed] = _pages(
        [_current(1, {"category": "permanent", "reward": "Skin", "origin": "Shift site"})]
    )
    assert embed.description == (
        f"`[permanent]`\n```\n{_code(1)}\n```\n`reward` Skin · `expires` never · Shift site"
    )


@pytest.mark.parametrize("origin", ("", "   ", "‮", " ⁦‬ "))
def test_permanent_with_no_real_origin_has_no_trailing_separator(origin):
    [embed] = _pages([_current(1, {"category": "permanent", "reward": "Skin", "origin": origin})])
    assert embed.description.endswith("`reward` Skin · `expires` never")


def test_an_origin_on_a_code_that_is_not_permanent_is_never_shown():
    for category in ("golden", "cosmetic", None):
        [embed] = _pages(
            [_current(1, {"category": category, "reward": "R", "origin": "SECRETORIGIN"})]
        )
        assert "SECRETORIGIN" not in embed.description


def test_a_permanent_code_says_never_whatever_date_a_stray_row_left_on_it():
    for due in (date(2020, 1, 1), date(2026, 10, 9), date(2026, 10, 10), date(2030, 5, 5)):
        [embed] = _pages([_current(1, {"category": "permanent", "expires_on": due})])
        assert "`expires` never" in embed.description
        assert "`expired`" not in embed.description


def test_a_blank_or_bidi_only_reward_is_unknown_and_a_missing_row_is_unknown_twice():
    for reward in ("", "   ", "‮‬"):
        [embed] = _pages([_current(1, {"category": "golden", "reward": reward})])
        assert "`reward` unknown · `expires` unknown" in embed.description
    [embed] = _pages([_current(1)])
    assert embed.description.endswith("`reward` unknown · `expires` unknown")


def test_a_code_that_has_already_expired_swaps_the_label_and_keeps_the_year():
    row = {"category": "golden", "reward": "k", "expires_on": date(2026, 10, 8)}
    [embed] = _pages([_current(1, row)])
    assert embed.description.endswith("`reward` k · `expired` Oct 8, 2026")
    assert chips(embed.description) == ["[golden keys]", "reward", "expired"]


def test_current_entry_never_shows_a_via_a_src_or_a_link():
    row = {"category": "golden", "reward": "k", "reward_from": "mentalmars", "expires_from": "X"}
    [embed] = _pages([_current(1, row)])
    assert "via" not in chips(embed.description)
    assert "src" not in chips(embed.description)
    assert "mentalmars" not in embed.description


# ===========================================================================
# Relative wording against an oracle (Pacific, DST edges, New Year)
# ===========================================================================


def _oracle(now: datetime, due: date, *, with_days: bool) -> tuple[str, str]:
    today = now.astimezone(LA).date()
    mon = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    def short(d: date, year: bool) -> str:
        return f"{mon[d.month - 1]} {d.day}" + (f", {d.year}" if year else "")

    if due < today:
        return "expired", short(due, True)
    if due == today:
        end = datetime.combine(due + timedelta(days=1), datetime.min.time(), tzinfo=LA)
        hours = int((end - now) // timedelta(hours=1))
        if hours < 1:
            return "expires", "in under an hour"
        return "expires", f"in {hours} hour" + ("" if hours == 1 else "s")
    if due == today + timedelta(days=1):
        return "expires", "tomorrow"
    if with_days:
        return "expires", f"{short(due, due.year != today.year)} (in {(due - today).days} days)"
    return "expires", short(due, True)


def _utc(y, mo, d, h=0, mi=0, s=0, us=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, us, tzinfo=UTC)


EDGES = [
    # An ordinary day.
    _utc(2026, 10, 9, 18, 0),
    # Last second of a Pacific day, and the first of the next (PDT, UTC-7).
    _utc(2026, 10, 10, 6, 59, 59, 999999),
    _utc(2026, 10, 10, 7, 0, 0),
    # One hour left, then one hour and a second left, then under an hour.
    _utc(2026, 10, 10, 6, 0, 0),
    _utc(2026, 10, 10, 5, 59, 59),
    _utc(2026, 10, 10, 6, 0, 1),
    # Fall back (Nov 1, 2026): a 25 hour day. Midnight PDT, 1:30 PDT, the repeated 1:30 PST.
    _utc(2026, 11, 1, 7, 0),
    _utc(2026, 11, 1, 8, 30),
    _utc(2026, 11, 1, 9, 30),
    _utc(2026, 11, 2, 7, 30),  # 23:30 PST on Nov 1
    # Spring forward (Mar 8, 2026): a 23 hour day.
    _utc(2026, 3, 8, 8, 0),
    _utc(2026, 3, 8, 9, 59),
    _utc(2026, 3, 8, 10, 0),  # 03:00 PDT, the hour that never was
    _utc(2026, 3, 9, 6, 59, 59),
    # New Year: it is already January in UTC and still December in Los Angeles.
    _utc(2027, 1, 1, 3, 0),
    _utc(2027, 1, 1, 7, 59),
    _utc(2027, 1, 1, 8, 0),
    _utc(2026, 12, 31, 22, 0),
]


def _due_dates(now: datetime) -> list[date]:
    today = now.astimezone(LA).date()
    offsets = [-400, -2, -1, 0, 1, 2, 3, 5, 30, 90, 200, 400]
    return [today + timedelta(days=o) for o in offsets]


@pytest.mark.parametrize("now", EDGES, ids=lambda n: n.isoformat())
def test_current_expiry_wording_matches_the_oracle(now):
    for due in _due_dates(now):
        word, value = _oracle(now, due, with_days=True)
        entry = _current(1, {"category": "golden", "reward": "R", "expires_on": due})
        [embed] = _pages([entry], now=now)
        assert embed.description.endswith(f"`reward` R · `{word}` {value}"), (now, due)


@pytest.mark.parametrize("now", EDGES, ids=lambda n: n.isoformat())
def test_history_expiry_wording_matches_the_oracle(now):
    for due in _due_dates(now):
        word, value = _oracle(now, due, with_days=False)
        view = _view(1, {"expires_on": due})
        [embed] = _history([view], now=now)
        detail = embed.description.split("\n")[3]
        if word == "expired":
            # The marker already says it once; the details line hides the expiry.
            assert detail.startswith("`src`"), (now, due, detail)
            assert embed.description.endswith(" · expired")
        else:
            assert detail == f"`expires` {value}", (now, due)


def test_fall_back_day_counts_twenty_five_hours_from_midnight():
    entry = _current(1, {"category": "golden", "reward": "R", "expires_on": date(2026, 11, 1)})
    [embed] = _pages([entry], now=_utc(2026, 11, 1, 7, 0))
    assert embed.description.endswith("`expires` in 25 hours")
    [embed] = _pages([entry], now=_utc(2026, 11, 1, 9, 0))  # 01:00 PST, the second time round
    assert embed.description.endswith("`expires` in 23 hours")


def test_spring_forward_day_is_twenty_three_hours_long():
    entry = _current(1, {"category": "golden", "reward": "R", "expires_on": date(2026, 3, 8)})
    [embed] = _pages([entry], now=_utc(2026, 3, 8, 8, 0))
    assert embed.description.endswith("`expires` in 23 hours")


def test_one_hour_is_singular_and_the_last_hour_is_under_an_hour():
    entry = _current(1, {"category": "golden", "reward": "R", "expires_on": date(2026, 10, 9)})
    end = _utc(2026, 10, 10, 7, 0)
    [embed] = _pages([entry], now=end - timedelta(hours=1))
    assert embed.description.endswith("`expires` in 1 hour")
    [embed] = _pages([entry], now=end - timedelta(minutes=59, seconds=59))
    assert embed.description.endswith("`expires` in under an hour")
    [embed] = _pages([entry], now=end - timedelta(microseconds=1))
    assert embed.description.endswith("`expires` in under an hour")


def test_the_instant_a_day_ends_the_history_marker_flips_and_the_chip_label_never_does():
    view = _view(1, {"expires_on": date(2026, 10, 9), "reward": "R"})
    end = _utc(2026, 10, 10, 7, 0)
    [before] = _history([view], now=end - timedelta(microseconds=1))
    [after] = _history([view], now=end)
    assert before.description.endswith("`first seen` 2026-10-08")
    assert "`expires` in under an hour" in before.description
    assert after.description.endswith("`first seen` 2026-10-08 · expired")
    assert "expire" not in "".join(chips(after.description))


def test_another_year_gets_its_year_and_this_year_does_not_on_the_codes_page():
    now = _utc(2026, 10, 9, 18)
    cases = {
        date(2026, 12, 25): "Dec 25 (in 77 days)",
        date(2027, 1, 5): "Jan 5, 2027 (in 88 days)",
    }
    for due, text in cases.items():
        [embed] = _pages([_current(1, {"category": "golden", "reward": "R", "expires_on": due})])
        assert embed.description.endswith(f"`expires` {text}")
    assert (date(2026, 12, 25) - now.astimezone(LA).date()).days == 77


def test_naive_now_is_taken_to_be_utc_not_a_crash():
    entry = _current(1, {"category": "golden", "reward": "R", "expires_on": date(2026, 10, 10)})
    [embed] = _pages([entry], now=datetime(2026, 10, 9, 18, 0))
    assert embed.description.endswith("`expires` tomorrow")


def test_the_last_date_a_datetime_can_hold_is_a_date_not_a_crash():
    entry = _current(1, {"category": "golden", "reward": "R", "expires_on": date.max})
    [embed] = _pages([entry])
    assert "`expires` " in embed.description and "9999" in embed.description
    [embed] = _history([_view(1, {"expires_on": date.max})])
    assert "`expires` Dec 31, 9999" in embed.description


# ===========================================================================
# /shift history entries: each marker, and what a marker hides
# ===========================================================================


MARKERS = {
    "expired": dict(details={"expires_on": date(2026, 10, 1), "reward": "R"}),
    "from a roundup": dict(from_roundup=True),
    "old post": dict(status="too_old"),
    "already around when alerts started": dict(status="seeded"),
}


@pytest.mark.parametrize("marker", MARKERS)
def test_each_marker_is_plain_text_on_the_first_seen_line_and_never_a_chip(marker):
    spec = dict(MARKERS[marker])
    details = spec.pop("details", None)
    [embed] = _history([_view(1, details, **spec)])
    lines = embed.description.split("\n")
    assert lines[-1] == f"`first seen` 2026-10-08 · {marker}"
    assert marker not in chips(embed.description)
    assert sum(marker in ln for ln in lines) == 1


def test_a_code_with_no_marker_ends_on_its_date_with_nothing_after():
    [embed] = _history([_view(1)])
    assert embed.description.endswith("\n`first seen` 2026-10-08")


def test_expired_beats_roundup_old_post_and_seeded_and_roundup_beats_the_status_ones():
    gone = {"expires_on": date(2026, 10, 1)}
    [embed] = _history([_view(1, gone, from_roundup=True, status="too_old")])
    assert embed.description.endswith("· expired")
    [embed] = _history([_view(1, from_roundup=True, status="seeded")])
    assert embed.description.endswith("· from a roundup")
    [embed] = _history([_view(1, status="too_old")])
    assert embed.description.endswith("· old post")


def test_expired_hides_the_expiry_and_the_credit_for_it_but_keeps_the_reward_and_its_credit():
    d = {
        "reward": "3 Golden Keys",
        "reward_from": "RewardSource",
        "expires_on": date(2026, 10, 1),
        "expires_from": "ExpirySource",
    }
    [embed] = _history([_view(1, d)])
    assert embed.description.split("\n")[3] == "`reward` 3 Golden Keys · `via` RewardSource"
    assert "ExpirySource" not in embed.description
    assert "Oct 1" not in embed.description
    assert "`expires`" not in embed.description and "`expired`" not in embed.description


def test_expired_with_no_reward_has_no_details_line_at_all():
    [embed] = _history([_view(1, {"expires_on": date(2026, 10, 1), "expires_from": "X"})])
    assert embed.description.split("\n")[3].startswith("`src`")
    assert "X" not in embed.description.replace("`src` Gearbox Blog", "")


def test_a_live_expiry_shows_and_credits_both_sources_when_they_differ():
    d = {
        "reward": "R",
        "reward_from": "A",
        "expires_on": date(2026, 10, 10),
        "expires_from": "B",
    }
    [embed] = _history([_view(1, d, status="too_old")])
    assert embed.description.split("\n")[3] == "`reward` R · `expires` tomorrow · `via` A and B"
    assert embed.description.endswith("`first seen` 2026-10-08 · old post")


def test_a_permanent_code_in_history_reads_never_and_credits_whoever_set_the_category():
    d = {
        "reward": "R",
        "category": "permanent",
        "category_from": "CatSource",
        "expires_on": date(2020, 1, 1),
        "expires_from": "StaleDate",
    }
    [embed] = _history([_view(1, d)])
    assert embed.description.split("\n")[3] == "`reward` R · `expires` never · `via` CatSource"
    assert "· expired" not in embed.description


@pytest.mark.parametrize(
    ("first_seen", "zone", "expected"),
    [
        (_utc(2026, 11, 2, 7, 30), "America/Los_Angeles", "2026-11-01"),
        (_utc(2026, 11, 2, 8, 30), "America/Los_Angeles", "2026-11-02"),
        (_utc(2026, 3, 8, 9, 59), "America/Los_Angeles", "2026-03-08"),
        (_utc(2026, 3, 9, 6, 59, 59), "America/Los_Angeles", "2026-03-08"),
        (_utc(2026, 3, 9, 7, 0), "America/Los_Angeles", "2026-03-09"),
        (_utc(2027, 1, 1, 3, 0), "America/Los_Angeles", "2026-12-31"),
        (_utc(2027, 1, 1, 3, 0), "UTC", "2027-01-01"),
        (_utc(2026, 10, 9, 23, 0), "Pacific/Kiritimati", "2026-10-10"),
    ],
)
def test_first_seen_is_the_date_in_the_servers_zone(first_seen, zone, expected):
    [embed] = _history([_view(1, first_seen_at=first_seen)], timezone=zone)
    assert embed.description.endswith(f"`first seen` {expected}")


def test_a_view_with_no_details_row_has_no_details_line():
    [embed] = _history([_view(1)])
    assert embed.description.split("\n")[3].startswith("`src`")


def test_a_missing_url_means_no_separator_and_the_source_line_ends_with_the_name():
    [embed] = _history([_view(1, item_url="")])
    assert "`src` Gearbox Blog\n`first seen`" in embed.description
    [embed] = _history([_view(1, item_url="javascript:alert(1)")])
    assert "javascript" not in embed.description


def test_the_source_is_cut_to_100_units_with_an_ellipsis_and_a_pair_is_never_split():
    for name in ("s" * 150, "\U0001f916" * 150, "*" * 150, "\U0001f916" * 49 + "ab" * 40):
        [embed] = _history([_view(1, source_name=name)])
        src = next(ln for ln in embed.description.split("\n") if ln.startswith("`src` "))
        shown = src.removeprefix("`src` ").split(" · <")[0]
        assert u16(shown) <= 100
        assert shown.endswith("…")
        embed.description.encode("utf-8")  # a lone surrogate would raise here
        assert_clean(embed.description, [_code(1)])


def test_a_source_name_that_esc_doubles_still_fits_the_page_and_the_chips_stay_clean():
    view = _view(
        1,
        {
            "reward": "*" * 300,
            "reward_from": "*" * 100,
            "expires_on": date(2026, 10, 10),
            "expires_from": "_" * 100,
        },
        source_name="*" * 100,
        item_url="https://e.com/" + "a/" * 2000,
    )
    [embed] = _history([view])
    assert u16(embed.description) <= 4096
    assert "<https://" not in embed.description
    assert_clean(embed.description, [_code(1)])


def test_with_a_tight_max_len_the_link_goes_first_then_the_source_and_nothing_else_moves():
    view = _view(
        1,
        {"reward": "x" * 300, "reward_from": "m" * 100},
        source_name="Reddit " + "m" * 93,
        item_url="https://e.com/" + "a" * 300,
        status="too_old",
    )
    full = _code_page_block(view, "UTC", max_len=10_000, now=NOW)
    head, tail = full.split("`src` ")
    assert " · <https://e.com/" in tail
    lens = []
    for max_len in range(u16(full), 80, -13):
        block = _code_page_block(view, "UTC", max_len=max_len, now=NOW)
        lens.append(u16(block))
        assert block.startswith(head + "`src` ")
        assert block.endswith("`first seen` 2026-10-08 · old post")
        assert block.count("```") == 2
        # A cut can only take the link or shorten the name; the link is gone before the name moves.
        if "…" in block.split("`first seen`")[0].split("`src` ")[1]:
            assert "<https://" not in block
    assert lens == sorted(lens, reverse=True)


# ===========================================================================
# Hostile text, through the renderers
# ===========================================================================


@pytest.mark.parametrize("hostile", HOSTILE, ids=lambda h: repr(h)[:24])
def test_hostile_text_in_every_field_of_a_current_page_never_makes_or_enters_a_chip(hostile):
    entries = [
        _current(
            1,
            {"category": "permanent", "reward": hostile, "origin": hostile, "reward_from": hostile},
        ),
        _current(2, {"category": "golden", "reward": hostile, "origin": hostile}),
        _current(3, {"category": "cosmetic", "reward": hostile}),
        _current(4, {"category": None, "reward": hostile}),
    ]
    for kind in KINDS:
        for page in _pages(entries, kind):
            assert_clean(page.description, fence_bodies(page.description))
    pages = _pages(entries)
    assert [c for p in pages for c in fence_bodies(p.description)] == [
        _code(i) for i in (2, 3, 1, 4)
    ]
    # And the chips are exactly the labels and headings, in order, with no value inside one.
    assert chips("\n\n".join(p.description for p in pages)) == [
        "[golden keys]",
        "reward",
        "expires",
        "[cosmetics]",
        "reward",
        "expires",
        "[permanent]",
        "reward",
        "expires",
        "[other]",
        "reward",
        "expires",
    ]


@pytest.mark.parametrize("hostile", HOSTILE, ids=lambda h: repr(h)[:24])
def test_hostile_text_in_every_field_of_a_history_page_never_makes_or_enters_a_chip(hostile):
    details = {
        "reward": hostile,
        "reward_from": hostile,
        "expires_on": date(2026, 10, 12),
        "expires_from": hostile + "x",
        "category": "permanent" if hostile.startswith("\\") else None,
        "category_from": hostile,
    }
    views = [
        _view(1, details, source_name=hostile, item_url=f"https://e.com/{hostile}"),
        _view(2, details, source_name=hostile, item_url=hostile, from_roundup=True),
        _view(
            3, {**details, "expires_on": date(2026, 10, 1)}, source_name=hostile, status="too_old"
        ),
        _view(4, None, source_name=hostile, status="seeded"),
    ]
    pages = _history(views)
    for page in pages:
        assert_clean(page.description, fence_bodies(page.description))
    assert [c for p in pages for c in fence_bodies(p.description)] == [
        _code(i) for i in (1, 2, 3, 4)
    ]
    drawn = set(chips("\n".join(p.description for p in pages)))
    assert drawn <= ALLOWED_CHIPS
    assert drawn >= {"src", "first seen"}


@pytest.mark.parametrize("hostile", HOSTILE, ids=lambda h: repr(h)[:24])
def test_a_hostile_history_entry_keeps_its_chips_clean_at_every_max_len(hostile):
    view = _view(
        1,
        {"reward": hostile, "reward_from": hostile, "expires_on": date(2026, 10, 12)},
        source_name=hostile,
        item_url="https://e.com/" + "a" * 200,
        status="seeded",
    )
    for max_len in (4096, 700, 500, 400, 300, 200, 120, 60):
        block = _code_page_block(view, "UTC", max_len=max_len, now=NOW)
        block.encode("utf-8")
        assert set(chips(block)) <= ALLOWED_CHIPS, (max_len, block)
        assert fence_bodies(block) == [_code(1)]
        assert block.count("```") == 2


@pytest.mark.parametrize("hostile", HOSTILE, ids=lambda h: repr(h)[:24])
def test_hostile_text_cannot_move_a_title_or_a_color_or_a_footer(hostile):
    [embed] = _pages([_current(1, {"category": "permanent", "reward": hostile, "origin": hostile})])
    assert embed.title == TITLES["all"]
    assert embed.color.value == 0x3FE37F
    assert embed.footer.text == f"{FOOTER} · Page 1 of 1"
    [embed] = _history([_view(1, {"reward": hostile}, source_name=hostile)])
    assert embed.title == HISTORY_TITLE
    assert embed.footer.text == f"{HISTORY_FOOTER_NOTE} · Page 1 of 1"


def test_a_heading_lookalike_in_a_reward_is_escaped_text_not_a_second_heading():
    entry = _current(1, {"category": "golden", "reward": "`[golden keys]`\n`[golden keys]`"})
    [embed] = _pages([entry])
    assert chips(embed.description) == ["[golden keys]", "reward", "expires"]
    assert embed.description.count("`[golden keys]`") == 1


def test_astral_emoji_rewards_are_counted_in_utf16_so_the_page_stays_under_the_cap():
    entries = [
        _current(i, {"category": "golden", "reward": "\U0001f916" * 150, "origin": "x"})
        for i in range(30)
    ]
    for page in _pages(entries):
        page.description.encode("utf-8")
        assert u16(page.description) <= 4096
        assert page.title is not None


def test_a_huge_reward_is_cut_to_300_shown_units_and_its_entry_still_fits_a_page():
    entry = _current(1, {"category": "permanent", "reward": "*" * 5000, "origin": "`" * 5000})
    [embed] = _pages([entry])
    assert u16(embed.description) < 4096
    assert_clean(embed.description, [_code(1)])
    reward_line = embed.description.split("\n")[-1]
    reward_part, _, rest = reward_line.partition(" · `expires` never · ")
    assert u16(reward_part.removeprefix("`reward` ")) <= 300
    assert u16(rest) <= 300


# ===========================================================================
# Through the real handlers
# ===========================================================================


def _guild(db_path: str) -> None:
    _make_guild(db_path, GUILD_A, games=[("borderlands4", 11)])
    with closing(connect(db_path)) as conn:
        repo.set_shift(conn, GUILD_A, enabled=True, channel_id=CHANNEL, ping="none")


def _hcode(n: int) -> str:
    return f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA"


def _seen(
    db_path: str,
    n: int,
    *,
    first_seen_at: datetime = SEEN,
    status: str = "posted",
    from_roundup: bool = False,
    source: str = "Feed",
    url: str = "https://example.com/post",
) -> str:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                _hcode(n),
                first_seen_at.astimezone(UTC).isoformat(),
                source,
                url,
                status,
                int(from_roundup),
            ),
        )
    return _hcode(n)


def _details(db_path: str, n: int, **kwargs) -> None:
    row = CodeDetailsRow(code=_hcode(n), clock_start=SEEN, updated_at=NOW, **kwargs)
    with closing(connect(db_path)) as conn, conn:
        repo._write_details(conn, row)


def _known(db_path: str, n: int, **details) -> str:
    seen = _seen(
        db_path,
        n,
        source=details.pop("source", "Feed"),
        url=details.pop("url", "https://example.com/post"),
    )
    _details(db_path, n, **details)
    return seen


def _codes_cb(cfg, db_path, moment=NOW):
    return command(make_member_shift_group(cfg, db_path, now=lambda: moment), "codes").callback


def _history_cb(cfg, db_path, moment=NOW):
    return command(make_member_shift_group(cfg, db_path, now=lambda: moment), "history").callback


def _sent(interaction):
    [call] = [m for m in interaction.sent if m.get("embed") is not None]
    return call


def _seed_every_category(db_path: str) -> None:
    far = date(2026, 11, 30)
    _known(db_path, 1, category="golden", reward="3 Golden Keys", expires_on=far)
    _known(db_path, 2, category="cosmetic", reward="A Skin", expires_on=far)
    _known(db_path, 3, category="permanent", reward="Head", origin="Launch")
    _known(db_path, 4)


@pytest.mark.parametrize("kind", KINDS)
async def test_handler_every_type_has_the_exact_title_green_and_footer(v3_cfg, v3_db, kind):
    _guild(v3_db)
    _seed_every_category(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes_cb(v3_cfg, v3_db)(interaction, type=kind)

    call = _sent(interaction)
    embed = call["embed"]
    assert embed.title.encode("utf-8") == TITLES[kind].encode("utf-8")
    assert embed.color.value == 0x3FE37F
    assert embed.footer.text.endswith(" · Page 1 of 1")
    assert embed.footer.text.startswith("Codes from Gearbox")
    assert call["allowed_mentions"].everyone is False
    codes = fence_bodies(embed.description)
    assert codes and set(chips(embed.description)) <= ALLOWED_CHIPS


@pytest.mark.parametrize("kind", KINDS)
async def test_handler_empty_result_is_still_green_and_titled_for_every_type(v3_cfg, v3_db, kind):
    _guild(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes_cb(v3_cfg, v3_db)(interaction, type=kind)

    embed = _sent(interaction)["embed"]
    assert embed.title == TITLES[kind]
    assert embed.color.value == 0x3FE37F
    assert embed.description.startswith("No current ")
    assert embed.footer.text.endswith(" · Page 1 of 1")


@pytest.mark.parametrize("kind", ("golden", "cosmetic", "permanent"))
async def test_handler_a_type_with_nothing_in_it_is_empty_while_others_have_codes(
    v3_cfg, v3_db, kind
):
    _guild(v3_db)
    others = {"golden", "cosmetic", "permanent"} - {kind}
    n = 1
    for cat in others:
        _known(v3_db, n, category=cat, reward="R", expires_on=date(2026, 11, 30))
        n += 1
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes_cb(v3_cfg, v3_db)(interaction, type=kind)

    embed = _sent(interaction)["embed"]
    assert embed.title == TITLES[kind] and embed.color.value == 0x3FE37F
    assert embed.description.startswith("No current ") and "```" not in embed.description


@pytest.mark.parametrize(
    ("typed", "title"),
    [
        ("> pwned ▌", TITLES["all"]),
        ("`> x ▌`", TITLES["all"]),
        ("", TITLES["all"]),
        ("GOLDEN", TITLES["golden"]),
        ("  Cosmetic ", TITLES["cosmetic"]),
        (None, TITLES["all"]),
        (7, TITLES["all"]),
    ],
)
async def test_handler_a_type_a_stranger_makes_up_never_reaches_a_title(
    v3_cfg, v3_db, typed, title
):
    _guild(v3_db)
    _seed_every_category(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes_cb(v3_cfg, v3_db)(interaction, type=typed)

    embed = _sent(interaction)["embed"]
    assert embed.title == title
    assert embed.color.value == 0x3FE37F


async def test_handler_history_has_the_exact_title_green_and_footer(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history_cb(v3_cfg, v3_db)(interaction, days=14)

    call = _sent(interaction)
    embed = call["embed"]
    assert embed.title.encode("utf-8") == HISTORY_TITLE.encode("utf-8")
    assert embed.color.value == 0x3FE37F
    assert embed.footer.text == f"{HISTORY_FOOTER_NOTE} · Page 1 of 1"
    assert call["allowed_mentions"].everyone is False


async def test_handler_empty_history_is_green_and_titled(v3_cfg, v3_db):
    _guild(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history_cb(v3_cfg, v3_db)(interaction)

    embed = _sent(interaction)["embed"]
    assert (embed.title, embed.color.value) == (HISTORY_TITLE, 0x3FE37F)
    assert embed.description == "No codes seen in that window."
    assert embed.footer.text == f"{HISTORY_FOOTER_NOTE} · Page 1 of 1"


async def test_handler_history_markers_all_four_through_the_database(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1, status="too_old")
    _seen(v3_db, 2, status="seeded")
    _seen(v3_db, 3, from_roundup=True)
    _seen(v3_db, 4)
    _seen(v3_db, 5)
    _details(v3_db, 5, reward="Old", expires_on=date(2026, 10, 1), expires_from="Somebody")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history_cb(v3_cfg, v3_db)(interaction)

    text = _sent(interaction)["embed"].description
    blocks = {n: next(b for b in text.split("\n\n") if _hcode(n) in b) for n in range(1, 6)}
    assert blocks[1].endswith("`first seen` 2026-10-08 · old post")
    assert blocks[2].endswith("`first seen` 2026-10-08 · already around when alerts started")
    assert blocks[3].endswith("`first seen` 2026-10-08 · from a roundup")
    assert blocks[4].endswith("`first seen` 2026-10-08")
    assert blocks[5].endswith("`first seen` 2026-10-08 · expired")
    assert "Somebody" not in text and "`expires`" not in text
    assert set(chips(text)) <= ALLOWED_CHIPS


@pytest.mark.parametrize("hostile", HOSTILE[::3], ids=lambda h: repr(h)[:24])
async def test_handler_hostile_database_text_stays_out_of_chips_titles_and_fences(
    v3_cfg, v3_db, hostile
):
    _guild(v3_db)
    far = date(2026, 11, 30)
    _known(
        v3_db,
        1,
        category="golden",
        reward=hostile,
        expires_on=far,
        reward_from=hostile,
        source=hostile,
        url=f"https://e.com/{hostile}",
    )
    _known(v3_db, 2, category="permanent", reward=hostile, origin=hostile, source=hostile)
    _known(v3_db, 3, category="cosmetic", reward=hostile, expires_on=far, source=hostile)
    _known(v3_db, 4, reward=hostile, source=hostile)

    codes = FakeInteraction(guild_id=GUILD_A)
    await _codes_cb(v3_cfg, v3_db)(codes)
    embed = _sent(codes)["embed"]
    assert embed.title == TITLES["all"] and embed.color.value == 0x3FE37F
    assert_clean(embed.description, fence_bodies(embed.description))
    assert sorted(fence_bodies(embed.description)) == [_hcode(i) for i in (1, 2, 3, 4)]

    hist = FakeInteraction(guild_id=GUILD_A)
    await _history_cb(v3_cfg, v3_db)(hist)
    embed = _sent(hist)["embed"]
    assert embed.title == HISTORY_TITLE and embed.color.value == 0x3FE37F
    assert sorted(fence_bodies(embed.description)) == [_hcode(i) for i in (1, 2, 3, 4)]
    assert set(chips(embed.description)) <= ALLOWED_CHIPS
    assert embed.description.count("```") == 8


async def test_handler_pages_flip_with_the_same_title_color_and_footer_and_continued_headings(
    v3_cfg, v3_db
):
    _guild(v3_db)
    for n in range(1, 31):
        _known(v3_db, n, category="permanent", reward="r" * 300, origin="o" * 300)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes_cb(v3_cfg, v3_db)(interaction, type="permanent")

    call = _sent(interaction)
    first, view = call["embed"], call["view"]
    assert view.total_pages >= 4
    assert first.description.split("\n")[0] == HEADINGS["permanent"]
    seen_codes = list(fence_bodies(first.description))
    for page in range(2, view.total_pages + 1):
        click = FakeInteraction(guild_id=GUILD_A)
        await view._turn_page(click, 1)
        [edit] = click.response.edits
        embed = edit["embed"]
        assert embed.title == TITLES["permanent"]
        assert embed.color.value == 0x3FE37F
        assert embed.footer.text.endswith(f"Page {page} of {view.total_pages}")
        assert embed.description.split("\n")[0] == CONTINUED["permanent"]
        assert edit["allowed_mentions"].everyone is False
        seen_codes += fence_bodies(embed.description)
    assert sorted(seen_codes) == [_hcode(n) for n in range(1, 31)]


async def test_handler_history_pages_flip_and_stay_green(v3_cfg, v3_db):
    _guild(v3_db)
    for n in range(1, 41):
        _seen(v3_db, n, source="s" * 100, url="https://e.com/" + "a" * 100)
        _details(v3_db, n, reward="r" * 300, reward_from="m" * 100)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history_cb(v3_cfg, v3_db)(interaction, days=30)

    call = _sent(interaction)
    view = call["view"]
    assert view.total_pages >= 3
    for _page in range(2, view.total_pages + 1):
        click = FakeInteraction(guild_id=GUILD_A)
        await view._turn_page(click, 1)
        embed = click.response.edits[0]["embed"]
        assert embed.title == HISTORY_TITLE and embed.color.value == 0x3FE37F
        assert "(continued)" not in embed.description  # history has no groups
        assert u16(embed.description) <= 4096


async def test_handler_dst_edge_wording_comes_out_of_the_database_clock(v3_cfg, v3_db):
    _guild(v3_db)
    _known(v3_db, 1, category="golden", reward="R", expires_on=date(2026, 11, 1))
    interaction = FakeInteraction(guild_id=GUILD_A)
    moment = _utc(2026, 11, 1, 7, 0)  # midnight PDT on the 25 hour day

    await _codes_cb(v3_cfg, v3_db, moment)(interaction, type="golden")

    assert _sent(interaction)["embed"].description.endswith("`reward` R · `expires` in 25 hours")

    after = FakeInteraction(guild_id=GUILD_A)
    await _codes_cb(v3_cfg, v3_db, _utc(2026, 11, 2, 8, 0))(after, type="golden")
    # Past the end of Nov 1 in Los Angeles the code is no longer current at all.
    assert _sent(after)["embed"].description.startswith("No current golden codes.")


@pytest.mark.parametrize(
    "name",
    ("m" * 100, "\U0001f916" * 50, "*" * 100, "ab" * 50),
    ids=("ascii", "astral", "escape-heavy", "pairs"),
)
def test_the_cut_obeys_max_len_to_the_unit_and_does_not_waste_room(name):
    view = _view(
        1,
        {"reward": "x" * 300, "reward_from": "m" * 100},
        source_name=name,
        item_url="https://e.com/" + "a" * 300,
        status="seeded",
    )
    # The smallest block that can exist: head, an ellipsis for a name, the date line.
    floor = u16(_code_page_block(view, "UTC", max_len=0, now=NOW))
    uncut = u16(
        _code_page_block(
            view.__class__(**{**view.__dict__, "item_url": ""}), "UTC", max_len=10_000, now=NOW
        )
    )
    for max_len in range(floor, uncut + 2):
        block = _code_page_block(view, "UTC", max_len=max_len, now=NOW)
        assert u16(block) <= max_len, max_len
        if max_len < uncut:
            # A name is cut a character at a time (two units for an emoji), so no more than
            # one unit may go unused.
            assert u16(block) >= max_len - 1, max_len
        block.encode("utf-8")
