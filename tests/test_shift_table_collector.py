"""Tests for the shift_table collector: the hand-made page fixtures, field by field.

No network, ever: httpx.MockTransport hands back a fixture file. The fixtures
are typed by hand to look like the shape of a SHiFT code list (three tables,
"Unknown" and "???", a code listed twice) and hold none of anyone's article text.
"""

import logging
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from newsbot.bot.format import cap_shown, discord_len, esc
from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.collectors.shift_table import (
    PageValidators,
    ShiftTableCollector,
    _is_no_date,
    parse_shift_date,
    parse_tables,
)
from newsbot.config import ShiftTableSource
from newsbot.shift.match import find_codes

FIXTURES = Path(__file__).parent / "fixtures" / "shift_table"
PAGE_URL = "https://codes.example/borderlands-4-shift-codes/"

# Codes in page.html, by what each one is there for.
GOLD3 = "K4M7P-2QXZR-9TLWB-6NHVC-J3FDS"
GOLD1 = "R8T2W-5HNXC-1PQVL-7DKMZ-B4GYE"
GOLD5 = "W6N3Q-8LFHD-4XRTK-2MVBP-Z9CSJ"
SKIN = "H2X9B-7VKQM-3NTLR-5WDPF-C8GZY"
TRINKET = "P5L8D-1RWNX-6HQKT-9BVMC-F2JZS"  # main table and cosmetic table
GOLD2 = "T3Q7V-4MHBK-8XLRN-2DWPC-G6YZJ"
FOUNDERS = "Q2R7N-5DLTK-8XWMB-4VHPC-J9FZS"  # main table and permanent table
MYSTERY = "C3N6T-7XKQM-4RLWB-8DHVP-Z2FGS"
BANNER = "N9C4F-6TKWH-2QLXB-7RDMV-S5PZJ"
EMOTE = "L7B2R-9XNQD-5KWTM-3HVFC-Y8GZP"
HEAD = "X4D8K-3PQMW-7TNLB-1RHVC-F6SZJ"
SUNRISE = "M6V2T-8KHXQ-4DNWB-9LRPC-G3YZF"
PROSE = "D8W3L-6QKXN-2TMRB-7HVPC-F4GZJ"  # only in a paragraph and a script
TWO_IN_A_CELL = ("B7H3V-9NXQK-1LTRW-5MDPC-S2GZF", "V5K8D-2RLXN-6QTMB-3WHPC-Y7FZJ")


async def _noop_sleep(_seconds: float) -> None:
    return None


def _source(**overrides) -> ShiftTableSource:
    fields = {
        "type": "shift_table",
        "name": "mentalmars",
        "url": PAGE_URL,
        "trust": "community",
        **overrides,
    }
    return ShiftTableSource(**fields)


# The fetch day for these tests: after every "Added" date they use, so none is "in the future".
NOW = datetime(2027, 1, 1, 12, tzinfo=UTC)


async def _collect_body(
    body: bytes | str, *, now: datetime = NOW, **source_overrides
) -> list[RawItem]:
    if isinstance(body, str):
        body = body.encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as http:
        collector = ShiftTableCollector(
            _source(**source_overrides), sleep=_noop_sleep, clock=lambda: now
        )
        return await collector.collect(http)


async def _collect(fixture: str, **source_overrides) -> dict[str, RawItem]:
    items = await _collect_body((FIXTURES / fixture).read_bytes(), **source_overrides)
    return {item.title: item for item in items}


def _details(item: RawItem) -> ItemDetails:
    assert item.details is not None
    return item.details


# --- the page, row by row ---


async def test_page_gives_one_item_per_code_in_page_order():
    items = await _collect("page.html")
    assert list(items) == [
        GOLD3,
        GOLD1,
        GOLD5,
        SKIN,
        TRINKET,
        GOLD2,
        FOUNDERS,
        MYSTERY,
        BANNER,
        EMOTE,
        HEAD,
        SUNRISE,
    ]


async def test_rows_that_are_not_code_rows_are_not_read():
    items = await _collect("page.html")
    for stranger in (*TWO_IN_A_CELL, PROSE):
        assert stranger not in items


async def test_item_text_is_just_the_code_so_prose_cannot_add_codes():
    items = await _collect("page.html")
    for code, item in items.items():
        assert item.title == item.excerpt == item.full_text == code
        assert find_codes(item.full_text) == [code]
        assert item.url == PAGE_URL
        assert item.source_name == "mentalmars"
        assert item.trust == "community"
        assert item.topics is None


async def test_topics_come_from_the_source():
    items = await _collect("shuffled.html", topics=["borderlands4"])
    assert all(item.topics == ("borderlands4",) for item in items.values())


@pytest.mark.parametrize(
    ("code", "reward", "category", "expires", "added", "origin"),
    [
        (GOLD3, "3 Golden Keys", "golden", date(2026, 10, 20), date(2026, 10, 6), ""),
        (GOLD1, "1 Golden Key", "golden", None, date(2026, 9, 30), ""),
        (GOLD5, "5 Golden Keys", "golden", None, date(2026, 9, 12), ""),
        (SKIN, "Shiny vault hunter skin", "cosmetic", date(2030, 12, 31), date(2026, 8, 1), ""),
        (GOLD2, "2 Golden Keys", "golden", date(2026, 10, 14), date(2026, 10, 2), ""),
        (MYSTERY, "Mystery bundle", "other", None, None, ""),
        (BANNER, "Banner: Vault Door", "cosmetic", None, None, "Pre-order bonus"),
        (EMOTE, "Emote: Shrug", "cosmetic", date(2030, 12, 31), None, "Convention giveaway"),
        (HEAD, "Head: Plain Bob", "permanent", None, None, "Newsletter signup"),
        (SUNRISE, "Weapon skin: Sunrise", "permanent", None, None, "Launch stream"),
    ],
)
async def test_each_row_field_by_field(code, reward, category, expires, added, origin):
    details = _details((await _collect("page.html"))[code])
    assert details == ItemDetails(
        code=code,
        rank="table",
        reward=reward,
        category=category,
        expires_on=expires,
        added_on=added,
        origin=origin,
        listed=True,
    )


async def test_a_code_in_the_main_and_cosmetic_tables_takes_the_cosmetic_reading():
    details = _details((await _collect("page.html"))[TRINKET])
    assert details.category == "cosmetic"
    assert details.reward == "Trinket: Tiny Gnome"  # the cosmetic table's, the more specific
    assert details.origin == "Community event"
    assert details.expires_on == date(2026, 11, 3)  # only the main table had one
    assert details.added_on == date(2026, 10, 1)


async def test_a_code_in_the_main_and_permanent_tables_is_permanent():
    details = _details((await _collect("page.html"))[FOUNDERS])
    assert details.category == "permanent"
    assert details.reward == "Skin: Founders"
    assert details.origin == "Founders' pack"
    assert details.added_on == date(2026, 7, 4)
    assert details.expires_on is None


async def test_published_at_is_the_start_of_the_added_day_in_pacific():
    items = await _collect("page.html")
    # Oct 6, 2026 is PDT (UTC-7); Sept 12 too. Midnight Pacific is 07:00 UTC.
    assert items[GOLD3].published_at == datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    assert items[GOLD5].published_at == datetime(2026, 9, 12, 7, 0, tzinfo=UTC)
    assert items[BANNER].published_at is None
    assert items[HEAD].published_at is None


async def test_published_at_is_pacific_standard_time_after_the_fall_change():
    page = """<h2>Codes</h2><table><tr><th>Code</th><th>Added</th></tr>
    <tr><td>K4M7P-2QXZR-9TLWB-6NHVC-J3FDS</td><td>Nov 10, 2026</td></tr></table>"""
    (item,) = await _collect_body(page)
    assert item.published_at == datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


async def test_shuffled_columns_read_the_same():
    page = await _collect("page.html")
    shuffled = await _collect("shuffled.html")
    assert list(shuffled) == [GOLD3, GOLD1, HEAD]
    for code, item in shuffled.items():
        assert _details(item) == _details(page[code])


async def test_the_info_line_counts_rows_skipped_rows_and_bad_dates(caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.shift_table"):
        await _collect("page.html")
    lines = [r.getMessage() for r in caplog.records if r.name == "newsbot.collectors.shift_table"]
    # "Soonish" and "Next week, maybe" are the only unparseable dates: "Unknown", "???" and
    # an empty cell are honest answers. Skipped: a two-code cell, "Coming soon", a footnote.
    assert lines == ["shift_table mentalmars: 14 rows read, 3 skipped, 2 unparseable dates"]


# --- health ---


async def test_a_page_without_code_tables_is_a_failed_pass():
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect("no_tables.html")


@pytest.mark.parametrize("body", [b"", b"<html></html>", b"\xff\xfe not html at all", b"<table>"])
async def test_empty_and_garbage_pages_are_failed_passes_not_crashes(body):
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(body)


async def test_an_unclosed_table_is_still_a_table():
    page = (
        "<h2>Codes</h2><table><tr><th>Code<th>Reward<tr>"
        "<td>K4M7P-2QXZR-9TLWB-6NHVC-J3FDS<td>3 Golden Keys"
    )
    (item,) = await _collect_body(page)
    assert _details(item).reward == "3 Golden Keys"


async def test_a_header_row_alone_is_a_failed_pass():
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body("<table><tr><th>Code</th><th>Reward</th></tr></table>")


async def test_only_the_first_row_is_the_header():
    # A "code" cell in a later row mustn't turn a table into a code table.
    page = f"""<table><tr><th>Platform</th><th>Notes</th></tr>
    <tr><td>PC</td><td>Code {GOLD3}</td></tr></table>"""
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(page)


# --- dates ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Oct 6, 2026", date(2026, 10, 6)),
        ("Sept 30, 2026", date(2026, 9, 30)),
        ("Sep 30, 2026", date(2026, 9, 30)),
        ("Dec 31, 2030", date(2030, 12, 31)),
        ("October 14th, 2026", date(2026, 10, 14)),
        ("Oct. 14, 2026", date(2026, 10, 14)),
        ("sept. 3rd 2026", date(2026, 9, 3)),
        ("MAY 1, 2027", date(2027, 5, 1)),
        ("  Oct 6, 2026  ", date(2026, 10, 6)),
    ],
)
def test_dates_in_the_pages_formats_parse(text, expected):
    assert parse_shift_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Unknown",
        "???",
        "Feb 30, 2026",
        "Octember 5, 2026",
        "Oc 5, 2026",
        "5 Oct 2026",
        "Oct 5",
        "Oct 5, 26",
        "Oct 5, 9999",
        "Oct 5, 1999",
        "Oct 0, 2026",
        "Oct 5, 2026 (maybe)",
        "Oct ٥, ٢٠٢٦",  # Arabic-Indic digits are not a date here
    ],
)
def test_everything_else_is_none(text):
    assert parse_shift_date(text) is None


@pytest.mark.parametrize(
    ("text", "no_date"),
    [("", True), ("  ", True), ("Unknown", True), ("UNKNOWN", True), ("???", True), ("?", True)]
    + [("Soonish", False), ("Oct 5", False), ("N/A", False)],
)
def test_which_nones_are_honest_answers(text, no_date):
    assert _is_no_date(text) is no_date


# --- categories ---


@pytest.mark.parametrize(
    ("reward", "category"),
    [
        ("3 Golden Keys", "golden"),
        ("A GoldenKey", "golden"),
        ("Cosmetic pack", "cosmetic"),
        ("Vault hunter skin", "cosmetic"),
        ("Victory emote", "cosmetic"),
        ("Weapon trinket", "cosmetic"),
        ("Outfit", "cosmetic"),
        ("Banner", "cosmetic"),
        ("Head", "cosmetic"),
        ("Golden Key and a skin", "cosmetic"),  # cosmetic is checked before golden
        ("Mystery bundle", "other"),
        ("Skinny dipping lessons", "other"),  # a word boundary, not a substring
        ("", "other"),
    ],
)
async def test_main_table_category_comes_from_the_reward(reward, category):
    page = f"""<h2>Codes</h2><table><tr><th>Reward</th><th>Code</th></tr>
    <tr><td>{reward}</td><td>{GOLD3}</td></tr></table>"""
    (item,) = await _collect_body(page)
    assert _details(item).category == category


async def test_the_heading_decides_a_tables_kind():
    def table(code: str) -> str:
        return f"<table><tr><th>Code</th></tr><tr><td>{code}</td></tr></table>"

    page = (
        f"<h2>Permanent SHiFT codes</h2>{table(GOLD3)}"
        f"<h4>Cosmetics</h4>{table(GOLD1)}"
        f"<h3>Everything else</h3>{table(GOLD5)}"
    )
    items = {i.title: _details(i).category for i in await _collect_body(page)}
    assert items == {GOLD3: "permanent", GOLD1: "cosmetic", GOLD5: "other"}


# --- hostile cells ---


async def test_hostile_page_text_stays_text_and_stays_small():
    items = await _collect("hostile.html")
    for item in items.values():
        details = _details(item)
        assert discord_len(esc(details.reward)) <= 300
        assert discord_len(esc(details.origin)) <= 300


async def test_a_mention_in_a_cell_survives_as_text_for_the_renderer_to_defuse():
    items = await _collect("hostile.html")
    reward = _details(items["A1B2C-D3E4F-G5H6J-K7L8M-N9P2Q"]).reward
    assert reward == "@everyone free keys"
    assert "@everyone" not in esc(reward).replace("@\u200beveryone", "")


async def test_markup_and_pings_are_kept_verbatim_below_the_cap():
    reward = _details((await _collect("hostile.html"))["B2C3D-E4F5G-H6J7K-L8M9N-P2Q3R"]).reward
    assert reward.startswith("<@&123456789012345678> <@&123456789012345679> <#98765> **bold**")
    assert "[click](https://evil.example/x)" in reward


async def test_a_5000_character_reward_is_cut_with_an_ellipsis():
    reward = _details((await _collect("hostile.html"))["C3D4E-F5G6H-J7K8L-M9N2P-Q3R4S"]).reward
    assert reward.endswith("…")
    assert 250 < len(reward) <= 300
    assert discord_len(esc(reward)) <= 300


async def test_astral_characters_are_cut_whole_and_measured_in_utf16():
    reward = _details((await _collect("hostile.html"))["D4E5F-G6H7J-K8L9M-N2P3Q-R4S5T"]).reward
    assert reward.endswith("…")
    assert set(reward[:-1]) == {"\U0001f52b"}
    assert len(reward) == 150  # 149 pistols at two units each, plus an ellipsis
    assert discord_len(reward) == 299


async def test_a_nested_table_adds_nothing_to_the_cell_around_it():
    items = await _collect("hostile.html")
    outer = _details(items["E5F6G-H7J8K-L9M2N-P3Q4R-S5T6V"])
    assert outer.reward == "outer reward tail text"
    # The nested table is a table in its own right, with its own header and rows.
    nested = _details(items["F6G7H-J8K9L-M2N3P-Q4R5S-T6V7W"])
    assert nested.reward == "nested reward"


async def test_script_contents_are_never_read():
    items = await _collect("hostile.html")
    assert "H8J9K-L2M3N-P4Q5R-S6T7V-W8X9Y" not in items
    assert _details(items["G7H8J-K9L2M-N3P4Q-R5S6T-V7W8X"]).reward == "Real reward"
    # And the fixture's own script-only codes, in page.html.
    assert PROSE not in await _collect("page.html")


async def test_a_5000_character_origin_is_capped_too():
    details = _details((await _collect("hostile.html"))["J9K2L-M3N4P-Q5R6S-T7V8W-X9Y2Z"])
    assert details.origin.startswith("@here word word")
    assert details.origin.endswith("…")
    assert discord_len(esc(details.origin)) <= 300


def _rows_page(n: int) -> str:
    rows = "".join(
        f"<tr><td>{i:05d}-{i:05d}-{i:05d}-{i:05d}-{i:05d}</td></tr>" for i in range(1, n + 1)
    )
    return f"<h2>Codes</h2><table><tr><th>Code</th></tr>{rows}</table>"


async def test_a_page_stops_being_read_after_a_thousand_rows():
    # The parser's budget, asked of the parser: since QA batch 1 (fix 8) a collector fails a
    # pass that has more than 500 codes, and this page has 999 after the cut.
    parsed, truncated = parse_tables(_rows_page(1200))
    # The header row counts toward the limit, so a thousand rows is 999 codes.
    assert [len(t.rows) for t in parsed] == [1000]
    assert truncated
    with pytest.raises(ValueError, match="too many rows"):
        await _collect_body(_rows_page(1200))


async def test_tables_nested_too_deep_are_ignored_without_breaking_the_rest():
    deep = "<table><tr><td>" * 20 + f"{GOLD1}" + "</td></tr></table>" * 20
    page = f"""<h2>Codes</h2><table><tr><th>Code</th></tr><tr><td>{GOLD3}</td></tr></table>{deep}
    <table><tr><th>Code</th></tr><tr><td>{GOLD5}</td></tr></table>"""
    items = await _collect_body(page)
    assert [i.title for i in items] == [GOLD3, GOLD5]
    _, truncated = parse_tables(page)
    assert truncated


async def test_one_enormous_cell_is_bounded_while_it_is_collected():
    page = f"""<h2>Codes</h2><table><tr><th>Reward</th><th>Code</th></tr>
    <tr><td>{"x" * 2_000_000}</td><td>{GOLD3}</td></tr></table>"""
    (item,) = await _collect_body(page)
    assert discord_len(esc(_details(item).reward)) <= 300
    tables, truncated = parse_tables(page)
    assert truncated
    assert len(tables[0].rows[1][0]) == 4000


def test_parse_tables_collapses_whitespace_and_keeps_inline_tags_glued():
    tables, _ = parse_tables(
        "<table><tr><th>  Code \n </th></tr>"
        "<tr><td>Gold<b>en</b> <br>Key&nbsp;&nbsp;x</td></tr></table>"
    )
    assert tables[0].rows == [["Code"], ["Golden Key x"]]


# --- the config class ---


@pytest.mark.parametrize("name", ["", "   "])
def test_a_shift_table_source_needs_a_name(name):
    with pytest.raises(ValidationError, match="needs a name"):
        _source(name=name)


def test_a_shift_table_source_needs_an_http_url_and_a_trust():
    with pytest.raises(ValidationError):
        _source(url="ftp://codes.example/x")
    with pytest.raises(ValidationError):
        _source(trust="rumor")


def test_the_collector_is_not_rate_limited_and_says_what_it_is():
    collector = ShiftTableCollector(_source())
    assert collector.source_type == "shift_table"
    assert collector.rate_limit_key is None
    assert collector.name == "mentalmars"


def test_validators_are_a_plain_dict_the_owner_hands_in():
    shared = PageValidators()
    assert ShiftTableCollector(_source(), validators=shared)._validators is shared


# --- cap_shown ---


def test_cap_shown_leaves_short_text_alone():
    assert cap_shown("3 Golden Keys", 300) == "3 Golden Keys"
    assert cap_shown("", 300) == ""


def test_cap_shown_measures_the_escaped_text_not_the_raw_text():
    raw = "*" * 200  # 200 raw characters, 400 shown
    capped = cap_shown(raw, 300)
    assert capped.endswith("…")
    assert discord_len(esc(capped)) <= 300
    assert len(capped) < 200


@pytest.mark.parametrize(
    "text",
    [
        "a" * 1000,
        "@everyone " * 200,
        "<@&123456789012345678>" * 40,
        "https://example.com/" * 40,
        "discord.gg/abc " * 60,
        "\U0001f52b" * 500,
        "a\U0001f52b" * 300,
        "_*~`|>" * 100,
        "word " * 1000,
    ],
)
@pytest.mark.parametrize("limit", [1, 2, 10, 300])
def test_cap_shown_always_fits(text, limit):
    capped = cap_shown(text, limit)
    assert discord_len(esc(capped)) <= limit
    assert capped == text or capped.endswith("…")


def test_cap_shown_does_not_cut_more_than_it_needs_to():
    capped = cap_shown("a" * 1000, 300)
    assert capped == "a" * 299 + "…"


# --- the 3.1.0 adversarial round's fixes ---


async def test_a_row_limit_is_per_table_so_junk_cannot_starve_the_code_tables():
    junk_rows = "<tr><td>noise</td></tr>" * 1200
    page = f"""<h2>Unrelated</h2><table><tr><th>Platform</th></tr>{junk_rows}</table>
    <h2>Codes</h2><table><tr><th>Code</th></tr><tr><td>{GOLD3}</td></tr></table>"""
    items = await _collect_body(page)
    assert [i.title for i in items] == [GOLD3]


async def test_the_page_as_a_whole_stops_at_five_thousand_rows():
    def table(n: int) -> str:
        rows = "".join(
            f"<tr><td>{n:02d}{i:03d}-{n:02d}{i:03d}-{n:02d}{i:03d}-{n:02d}{i:03d}-{n:02d}{i:03d}</td></tr>"
            for i in range(900)
        )
        return f"<table><tr><th>Code</th></tr>{rows}</table>"

    parsed, truncated = parse_tables("".join(table(n) for n in range(8)))
    # Eight tables of 901 rows is 7,208; the page-wide budget of 5,000 rows is spent first. (As
    # for the thousand-row test, the collector's 500-code cap now fails such a page outright.)
    assert sum(len(t.rows) for t in parsed) == 5000  # five and a half tables
    assert truncated


@pytest.mark.parametrize(
    ("added", "kept"),
    [
        ("Jan 1, 2027", True),  # the fetch day itself
        ("Jan 2, 2027", True),  # a day of slack for a site writing from a zone ahead of Pacific
        ("Jan 3, 2027", False),
        ("Dec 31, 2099", False),
    ],
)
async def test_an_added_date_after_the_fetch_day_counts_as_no_date(added, kept, caplog):
    page = f"""<h2>Codes</h2><table><tr><th>Code</th><th>Added</th><th>Expire</th></tr>
    <tr><td>{GOLD3}</td><td>{added}</td><td>Dec 31, 2099</td></tr></table>"""
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.shift_table"):
        (item,) = await _collect_body(page)
    assert (item.published_at is not None) is kept
    assert (_details(item).added_on is not None) is kept
    assert _details(item).expires_on == date(2099, 12, 31)  # only "Added" is held to the clock
    assert ("1 added in the future" in caplog.text) is (not kept)
    assert "unparseable" in caplog.text  # still the same single line, with the count in it


async def test_the_fetch_day_is_a_pacific_day():
    page = f"""<table><tr><th>Code</th><th>Added</th></tr>
    <tr><td>{GOLD3}</td><td>Jan 2, 2027</td></tr></table>"""
    # 03:00 UTC on Jan 1 is still Dec 31 in Pacific; Jan 2 is then two days ahead.
    (item,) = await _collect_body(page, now=datetime(2027, 1, 1, 3, tzinfo=UTC))
    assert item.published_at is None


async def test_a_reward_spanning_rows_is_repeated_for_each_code_it_covers():
    page = (
        "<table><tr><th>Reward</th><th>Code</th></tr>"
        f'<tr><td rowspan="3">3 Golden Keys</td><td>{GOLD3}</td></tr>'
        f"<tr><td>{GOLD1}</td></tr><tr><td>{GOLD5}</td></tr>"
        f"<tr><td>after the span</td><td>{SKIN}</td></tr></table>"
    )
    items = {i.title: _details(i).reward for i in await _collect_body(page)}
    assert items == {
        GOLD3: "3 Golden Keys",
        GOLD1: "3 Golden Keys",
        GOLD5: "3 Golden Keys",
        SKIN: "after the span",
    }


async def test_a_cell_spanning_rows_and_columns_blanks_its_extra_columns():
    page = (
        "<table><tr><th>Reward</th><th>Code</th><th>Expire</th><th>Added</th></tr>"
        f'<tr><td rowspan="2" colspan="2">Big reward</td><td>{GOLD3}</td><td>Oct 6, 2026</td></tr>'
        f"<tr><td>{GOLD1}</td><td>Oct 7, 2026</td></tr></table>"
    )
    # The span covers the reward and code columns, so the codes sit in the Expire column
    # and nothing can be read as a code: skipped, not misread.
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(page)


async def test_spans_are_bounded():
    page = (
        '<table><tr><th colspan="999999">Code</th><th>Reward</th></tr>'
        f'<tr><td colspan="999999" rowspan="999999">{GOLD3}</td><td>x</td></tr>'
        f"<tr><td>{GOLD1}</td></tr></table>"
    )
    tables, _ = parse_tables(page)
    assert max(len(row) for row in tables[0].rows) <= 64
    # The huge cell really does cover the code column of the next row too, so GOLD1
    # (pushed to the far side of the span) isn't read as a code.
    assert [i.title for i in await _collect_body(page)] == [GOLD3]


async def test_a_data_cell_with_a_colspan_keeps_later_columns_aligned():
    page = (
        "<table><tr><th>Reward</th><th>Code</th><th>Expire</th></tr>"
        f"<tr><td>x</td><td>{GOLD3}</td><td>Oct 20, 2026</td></tr>"
        f'<tr><td colspan="2">{GOLD1}</td><td>Oct 21, 2026</td></tr></table>'
    )
    items = {i.title: _details(i) for i in await _collect_body(page)}
    # The second row's code is in the first column, under "Reward": no code there.
    assert list(items) == [GOLD3]


async def test_a_lone_code_header_still_reads_a_bare_table():
    items = await _collect_body(
        f"<table><tr><th>Code</th></tr><tr><td>{GOLD3}</td></tr><tr><td>{GOLD1}</td></tr></table>"
    )
    assert [i.title for i in items] == [GOLD3, GOLD1]


async def test_the_header_is_the_first_row_that_names_a_code_column_and_another_role():
    page = (
        '<table><tr><td colspan="3">Latest SHiFT codes</td></tr>'
        "<tr><td>Updated daily</td></tr>"
        "<tr><th>Reward</th><th>Code</th><th>Expiry</th></tr>"
        f"<tr><td>3 Golden Keys</td><td>{GOLD3}</td><td>Oct 20, 2026</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert _details(item).reward == "3 Golden Keys"
    assert _details(item).expires_on == date(2026, 10, 20)


async def test_rows_above_the_header_are_not_data():
    page = (
        '<table><tr><td colspan="3">Latest SHiFT codes</td></tr>'
        f"<tr><td>x</td><td>{GOLD1}</td><td>Oct 1, 2026</td></tr>"
        "<tr><th>Reward</th><th>Code</th><th>Expiry</th></tr>"
        f"<tr><td>y</td><td>{GOLD3}</td><td>Oct 20, 2026</td></tr></table>"
    )
    assert [i.title for i in await _collect_body(page)] == [GOLD3]


async def test_a_header_this_far_down_is_not_looked_for():
    filler = "<tr><td>filler</td></tr>" * 5
    page = (
        f"<table>{filler}<tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>x</td><td>{GOLD3}</td></tr></table>"
    )
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(page)


async def test_an_invisible_character_in_a_header_cell_does_not_hide_the_column():
    page = f"""<table><tr><th>Re\u200bward</th><th>Co\u200bde</th></tr>
    <tr><td>3 Golden Keys</td><td>{GOLD3}</td></tr></table>"""
    (item,) = await _collect_body(page)
    assert _details(item).reward == "3 Golden Keys"


async def test_an_invisible_character_is_still_fatal_to_a_code():
    broken = GOLD3[:11] + "\u200b" + GOLD3[11:]
    items = await _collect_body(
        f"<table><tr><th>Code</th></tr><tr><td>{broken}</td></tr><tr><td>{GOLD1}</td></tr></table>"
    )
    assert [i.title for i in items] == [GOLD1]


@pytest.mark.parametrize(
    "text", ["OCT 6TH, 2026", "oct 6TH 2026", "Oct 6Th, 2026", "SEPT. 6ND, 2026"]
)
def test_dates_are_case_insensitive_all_the_way_through(text):
    assert parse_shift_date(text) is not None


@pytest.mark.parametrize("text", ["???\u200b", "\u200bUnknown", "\ufeff"])
def test_an_invisible_character_does_not_turn_no_date_into_a_bad_date(text):
    assert _is_no_date(text)
