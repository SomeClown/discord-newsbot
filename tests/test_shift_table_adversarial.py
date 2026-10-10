"""An adversarial round on the shift_table collector (design.md §16, plan Task 1).

The first test file was written against fixtures the implementer typed from memory. This
one starts from the real page's header wording (hand-copied shapes, invented codes, no
article text) and then leans on the parser: odd headers, hostile cells, strange dates.
Where the collector was wrong, the test went in as `xfail(strict=True)` with the reason;
the fixes landed and the markers came off.

No network: `httpx.MockTransport` hands back bytes, and the suite's network guard fails
any test that so much as thinks about a real socket.
"""

import html
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.bot.format import cap_shown, discord_len, esc
from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.collectors.shift_table import ShiftTableCollector, parse_shift_date, parse_tables
from newsbot.config import ShiftTableSource
from newsbot.shift.match import find_codes, is_code

FIXTURE = Path(__file__).parent / "fixtures" / "shift_table" / "real_shape.html"
PAGE_URL = "https://codes.example/borderlands-4-shift-codes/"

# real_shape.html, by what each code is there for (all invented).
K5 = "7KQ2M-4XNTB-8RWLD-3HVCP-ZJ6FS"
K3 = "2WB9T-6LHXN-5QDRC-8MVKP-YJ3GF"
K1 = "9DR4N-3TXQK-7LWBH-2VPMC-FS8GZ"
K2 = "5XM8C-1QHTV-4RLNB-9KWDP-GJ2ZF"
SKIN = "3NH7V-8BKQD-2TXLR-6WMPC-ZF4GJ"
BOTH = "4TV8K-2RHQN-7XWLB-5DMPC-JF9ZG"  # main table and cosmetic table
BANNER = "6PC3Z-9GJ5F-2KWBN-8LRHD-4XMQV"
HEAD = "8JF2R-5CVNX-1LQTK-7WBDM-3HZPG"
OLD_BOOT = "2R8WC-6LNXT-3QHDB-9MVKP-4GZFJ"  # cosmetic table and permanent table
SUNRISE = "1ZV6D-7MQKX-4NBHL-9RTWC-2PGFJ"
FOUNDERS = "A3M9K-5TNXQ-8DLWB-2HRVC-7PFZJ"

# Two more, for hand-built pages.
C1 = "AB12C-DE34F-GH56J-KL78M-NP90Q"
C2 = "QR12S-TU34V-WX56Y-ZA78B-CD90E"


# The fetch day for these tests: after every "Added" date they use, so none is "in the future".
NOW = datetime(2027, 1, 1, 12, tzinfo=UTC)


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


async def _collect_body(body: bytes | str) -> list[RawItem]:
    if isinstance(body, str):
        body = body.encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as http:
        collector = ShiftTableCollector(_source(), sleep=_noop_sleep, clock=lambda: NOW)
        return await collector.collect(http)


def _details(item: RawItem) -> ItemDetails:
    assert item.details is not None
    return item.details


def _by_code(items: list[RawItem]) -> dict[str, ItemDetails]:
    return {i.title: _details(i) for i in items}


def _table(header: list[str], *rows: list[str], heading: str = "Codes") -> str:
    head = "".join(f"<th>{h}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<h2>{heading}</h2><table><tr>{head}</tr>{body}</table>"


async def _one(header: list[str], row: list[str], heading: str = "Codes") -> ItemDetails:
    (item,) = await _collect_body(_table(header, row, heading=heading))
    return _details(item)


# --- the real page's shape ---


@pytest.fixture(scope="module")
def real_page() -> bytes:
    return FIXTURE.read_bytes()


async def test_real_shaped_page_gives_one_item_per_code_in_first_seen_order(real_page):
    items = await _collect_body(real_page)
    assert [i.title for i in items] == [
        K5,
        K3,
        K1,
        K2,
        SKIN,
        BOTH,
        BANNER,
        HEAD,
        OLD_BOOT,
        SUNRISE,
        FOUNDERS,
    ]


async def test_real_shaped_page_reads_every_row_without_skips_or_bad_dates(real_page, caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.shift_table"):
        await _collect_body(real_page)
    lines = [r.getMessage() for r in caplog.records if r.name == "newsbot.collectors.shift_table"]
    # 6 main rows, 4 cosmetic, 3 permanent. "Unknown" and "???" are honest answers, not bad dates.
    assert lines == ["shift_table mentalmars: 13 rows read, 0 skipped, 0 unparseable dates"]


@pytest.mark.parametrize(
    ("code", "reward", "category", "expires", "added", "origin"),
    [
        (K5, "5 Golden Keys", "golden", date(2026, 10, 20), date(2026, 10, 6), ""),
        (K3, "3 Golden Keys", "golden", date(2026, 9, 30), date(2026, 9, 20), ""),
        (K1, "1 Golden Key", "golden", None, date(2026, 9, 2), ""),
        (K2, "2 Golden Keys", "golden", None, date(2026, 8, 28), ""),
        (SKIN, "Vault Hunter ’Mirage’ skin", "cosmetic", date(2030, 12, 31), date(2026, 8, 1), ""),
        (BANNER, "Banner: “Vault Door”", "cosmetic", None, None, "Pre-order bonus"),
        (HEAD, "Head: Plain Bob", "cosmetic", date(2030, 12, 31), None, "Newsletter signup"),
        (SUNRISE, "Weapon skin: ‘Sunrise’", "permanent", None, None, "Launch stream"),
        (
            FOUNDERS,
            "Founders’ pack",
            "permanent",
            None,
            None,
            "Pre-launch “Founders” promo",
        ),
    ],
)
async def test_real_shaped_rows_field_by_field(
    real_page, code, reward, category, expires, added, origin
):
    details = _by_code(await _collect_body(real_page))[code]
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


async def test_real_shaped_code_in_main_and_cosmetic_tables_merges_to_the_cosmetic_reading(
    real_page,
):
    details = _by_code(await _collect_body(real_page))[BOTH]
    assert details.category == "cosmetic"
    assert details.reward == "Trinket: ‘Gilded Gnome’"  # the cosmetic table's, not "Trinket"
    assert details.origin == "Launch event"
    # The cosmetic table says "Unknown"; the main table's date fills the gap instead of vanishing.
    assert details.expires_on == date(2026, 9, 30)
    assert details.added_on == date(2026, 9, 15)  # only the main table has an Added column


async def test_real_shaped_code_in_cosmetic_and_permanent_tables_is_permanent(real_page):
    details = _by_code(await _collect_body(real_page))[OLD_BOOT]
    assert details.category == "permanent"
    assert details.reward == "Trinket: Old Boot (permanent)"
    assert details.origin == "Newsletter"
    assert details.expires_on is None  # "???" in the cosmetic table, no column in the permanent one
    assert details.added_on is None


async def test_real_shaped_published_at_is_midnight_pacific_of_the_added_day(real_page):
    items = {i.title: i for i in await _collect_body(real_page)}
    # All PDT (UTC-7) in 2026 before Nov 1.
    assert items[K5].published_at == datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    assert items[K3].published_at == datetime(2026, 9, 20, 7, 0, tzinfo=UTC)
    assert items[K2].published_at == datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
    assert items[BOTH].published_at == datetime(2026, 9, 15, 7, 0, tzinfo=UTC)
    # Cosmetic and permanent rows have no Added column, so no publish time.
    assert items[BANNER].published_at is None
    assert items[SUNRISE].published_at is None
    assert items[OLD_BOOT].published_at is None


async def test_real_shaped_nbsp_and_curly_quotes_survive_as_plain_text(real_page):
    by_code = _by_code(await _collect_body(real_page))
    assert "\xa0" not in by_code[K5].reward  # "5&nbsp;Golden Keys" reads like normal text
    assert by_code[SKIN].reward == "Vault Hunter ’Mirage’ skin"  # curly quotes are kept as-is


async def test_real_shaped_items_each_hold_exactly_their_own_code(real_page):
    for item in await _collect_body(real_page):
        assert find_codes(item.full_text or "") == [item.title]
        assert is_code(item.title)
        assert _details(item).code == item.title
        assert item.url == PAGE_URL


# --- header matching ---


@pytest.mark.parametrize(
    "header",
    [
        ["REWARD", "ADDED", "BORDERLANDS 4 SHIFT CODE", "EXPIRE DATE"],
        ["reward", "added", "borderlands 4 shift code", "expire date"],
        ["  Reward  ", "\nAdded\t", "Borderlands   4\n SHiFT   Code ", "  Expire    Date"],
        [
            "<b>Reward</b>",
            "<i>Added</i>",
            "<span>Borderlands 4 SHiFT Code</span>",
            "Expire&nbsp;Date",
        ],
    ],
)
async def test_headers_match_whatever_the_case_and_spacing(header):
    details = await _one(header, ["3 Golden Keys", "Oct 6, 2026", C1, "Oct 20, 2026"])
    assert (details.reward, details.added_on, details.expires_on) == (
        "3 Golden Keys",
        date(2026, 10, 6),
        date(2026, 10, 20),
    )


@pytest.mark.parametrize("word", ["Expire Date", "Expires", "Expiry", "Expiration", "Expired on"])
async def test_every_spelling_of_expiry_finds_the_expiry_column(word):
    details = await _one(["Reward", "Code", word], ["x", C1, "Oct 20, 2026"])
    assert details.expires_on == date(2026, 10, 20)


async def test_header_cells_in_td_instead_of_th_still_make_a_table():
    page = (
        f"<table><tr><td>Reward</td><td>Added</td><td>SHiFT Code</td></tr>"
        f"<tr><td>3 Golden Keys</td><td>Oct 6, 2026</td><td>{C1}</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert _details(item).reward == "3 Golden Keys"
    assert _details(item).added_on == date(2026, 10, 6)


async def test_a_table_with_thead_tbody_and_tfoot_reads_the_same_as_a_bare_one():
    page = (
        "<table><thead><tr><th>Reward</th><th>Code</th></tr></thead>"
        f"<tbody><tr><td>3 Golden Keys</td><td>{C1}</td></tr></tbody>"
        "<tfoot><tr><td>Total</td><td>1 code</td></tr></tfoot></table>"
    )
    (item,) = await _collect_body(page)
    assert item.title == C1


async def test_a_header_row_repeated_mid_table_is_skipped_and_both_sides_are_read(caplog):
    header = "<tr><th>Reward</th><th>Borderlands 4 SHiFT Code</th></tr>"
    page = (
        f"<h2>Codes</h2><table>{header}<tr><td>one</td><td>{C1}</td></tr>"
        f"{header}<tr><td>two</td><td>{C2}</td></tr></table>"
    )
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.shift_table"):
        items = await _collect_body(page)
    assert _by_code(items)[C1].reward == "one"
    assert _by_code(items)[C2].reward == "two"
    assert "2 rows read, 1 skipped" in caplog.text


async def test_an_extra_unrelated_column_does_not_shift_the_real_ones():
    details = await _one(
        ["Votes", "Reward", "Platform", "Added", "SHiFT Code", "Comments", "Expire Date"],
        ["912", "3 Golden Keys", "All", "Oct 6, 2026", C1, "works!", "Oct 20, 2026"],
    )
    assert details.reward == "3 Golden Keys"
    assert details.added_on == date(2026, 10, 6)
    assert details.expires_on == date(2026, 10, 20)


async def test_an_unrelated_column_that_mentions_a_role_word_cannot_steal_another_columns_role():
    # Two columns say "reward"; the first one wins and the second is just a column.
    details = await _one(["Reward", "Reward notes", "Code"], ["3 Golden Keys", "not this one", C1])
    assert details.reward == "3 Golden Keys"


async def test_a_row_shorter_than_the_header_is_read_with_the_missing_fields_empty():
    page = (
        "<table><tr><th>Reward</th><th>Code</th><th>Added</th><th>Expire</th></tr>"
        f"<tr><td>3 Golden Keys</td><td>{C1}</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    details = _details(item)
    assert (details.reward, details.added_on, details.expires_on) == ("3 Golden Keys", None, None)
    assert item.published_at is None


async def test_a_row_longer_than_the_header_is_read_and_the_extra_cells_ignored():
    page = (
        "<table><tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>3 Golden Keys</td><td>{C1}</td><td>surprise</td><td>Oct 6, 2026</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert _details(item).reward == "3 Golden Keys"


async def test_a_table_with_no_code_heading_is_ignored_even_if_a_cell_holds_a_code():
    page = _table(["Platform", "Notes"], ["PC", C2]) + _table(["Reward", "Code"], ["x", C1])
    assert [i.title for i in await _collect_body(page)] == [C1]


async def test_the_same_code_twice_in_one_cell_is_one_code():
    (item,) = await _collect_body(_table(["Code"], [f"{C1} (or {C1.lower()})"]))
    assert item.title == C1


async def test_a_lowercase_code_is_reported_in_uppercase():
    (item,) = await _collect_body(_table(["Code"], [C1.lower()]))
    assert item.title == C1


async def test_a_code_that_only_appears_in_a_link_target_is_not_read():
    page = _table(["Code"], [f'<a href="https://x.invalid/?c={C1}">Redeem</a>'], [C2])
    assert [i.title for i in await _collect_body(page)] == [C2]


async def test_two_different_codes_in_one_cell_skip_the_row_rather_than_guess():
    page = _table(["Reward", "Code"], ["both", f"{C1}<br>{C2}"], ["single", C2])
    assert _by_code(await _collect_body(page)) == {
        C2: ItemDetails(code=C2, rank="table", reward="single", category="other", listed=True)
    }


async def test_a_code_with_an_invisible_character_inside_is_not_a_code():
    broken = C1[:11] + "\u200b" + C1[11:]  # "AB12C-DE34F\u200b-GH56J-..."
    page = _table(["Code"], [broken], [C2])
    assert [i.title for i in await _collect_body(page)] == [C2]


async def test_a_homoglyph_code_is_not_a_code():
    spoofed = C1.replace("A", "А", 1)  # Cyrillic capital A
    page = _table(["Code"], [spoofed], [C2])
    assert [i.title for i in await _collect_body(page)] == [C2]


@pytest.mark.parametrize(
    ("heading", "category"),
    [
        ("PERMANENT SHiFT CODES", "permanent"),
        ("Permanent&nbsp;Codes", "permanent"),
        ("<span id='x'>Cosmetic</span> Reward <em>SHiFT</em> Codes", "cosmetic"),
        ("Every Borderlands 4 SHiFT Code for Golden Keys", "other"),
        ("", "other"),
    ],
)
async def test_headings_decide_the_kind_however_they_are_dressed(heading, category):
    details = await _one(["Reward", "Code"], ["x", C1], heading=heading)
    assert details.category == category


async def test_a_heading_after_a_table_does_not_reclassify_it():
    page = f"<table><tr><th>Code</th></tr><tr><td>{C1}</td></tr></table><h2>Permanent codes</h2>"
    (item,) = await _collect_body(page)
    assert _details(item).category == "other"


async def test_one_heading_covers_every_table_until_the_next_heading():
    one = f"<table><tr><th>Code</th></tr><tr><td>{C1}</td></tr></table>"
    two = f"<table><tr><th>Code</th></tr><tr><td>{C2}</td></tr></table>"
    items = await _collect_body(f"<h2>Permanent codes</h2>{one}{two}")
    assert {i.title: _details(i).category for i in items} == {C1: "permanent", C2: "permanent"}


async def test_a_heading_hidden_in_a_comment_does_not_count():
    page = (
        f"<h2>Permanent codes</h2><!-- <h2>Cosmetic</h2> -->"
        f"<table><tr><th>Code</th></tr><tr><td>{C1}</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert _details(item).category == "permanent"


async def test_the_main_and_cosmetic_tables_in_that_order_do_not_leak_a_kind_into_each_other(
    real_page,
):
    by_code = _by_code(await _collect_body(real_page))
    assert by_code[K5].category == "golden"  # not dragged to cosmetic by the later section
    assert by_code[BANNER].category == "cosmetic"


# Real bugs: the parser ignores table geometry.


async def test_a_reward_cell_spanning_two_rows_does_not_lose_the_second_code():
    page = (
        "<table><tr><th>Reward</th><th>Code</th></tr>"
        f'<tr><td rowspan="2">3 Golden Keys</td><td>{C1}</td></tr>'
        f"<tr><td>{C2}</td></tr></table>"
    )
    assert [i.title for i in await _collect_body(page)] == [C1, C2]


async def test_a_header_cell_spanning_two_columns_keeps_later_columns_aligned():
    page = (
        '<table><tr><th>Reward</th><th colspan="2">SHiFT Code</th><th>Expire</th></tr>'
        f"<tr><td>x</td><td>{C1}</td><td>extra</td><td>Oct 20, 2026</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert _details(item).expires_on == date(2026, 10, 20)


async def test_a_title_row_above_the_header_does_not_hide_the_table():
    page = (
        '<table><tr><td colspan="3">Latest SHiFT codes</td></tr>'
        "<tr><th>Reward</th><th>Code</th><th>Expire</th></tr>"
        f"<tr><td>x</td><td>{C1}</td><td>Oct 20, 2026</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert _details(item).expires_on == date(2026, 10, 20)


# --- hostile input ---

MENTIONS = [
    "@everyone",
    "@here",
    "<@123456789012345678>",
    "<@!123456789012345678>",
    "<@&123456789012345678>",
    "<#123456789012345678>",
    "</shift:123456789012345678>",
    "@everyone @here <@&123456789012345678> <@234567890123456789> "
    "<#345678901234567890> </x:456789012345678901>",
]


@pytest.mark.parametrize("mention", MENTIONS)
async def test_mentions_in_reward_and_origin_arrive_as_text_and_defuse_when_escaped(mention):
    safe = html.escape(mention)
    page = _table(["Reward", "Source", "Code"], [safe, safe, C1], heading="Permanent codes")
    (item,) = await _collect_body(page)
    details = _details(item)
    assert details.reward == mention
    assert details.origin == mention
    for shown in (esc(details.reward), esc(details.origin)):
        assert "@everyone" not in shown
        assert "@here" not in shown
        # A zero-width space after the "@" or "<" is how esc() defuses these.
        assert not re.search(r"<@[!&]?\d|<#\d|</\w+:\d", shown)


@pytest.mark.parametrize(
    "text",
    [
        "**bold** __under__ ~~strike~~ ||spoiler||",
        "`code` ``double`` ```fence\nblock```",
        "> quote\n# heading\n- list",
        "[click](https://evil.example/x) https://evil.example/y discord.gg/abc",
        "\\*escaped\\* \\\\ \\`",
        "\u202eevil text\u202c normal \u200bzero\u200bwidth\u200b \u200f\u200e \ufeff",
        "\u200b" * 1000,
        "\u202e" * 1000,
        "\U0001f468\u200d\U0001f469\u200d\U0001f467 family " * 80,
        "\U0001f1fa\U0001f1f8" * 400,
        "*" * 5000,
        "<@&1>" * 1000,
    ],
)
async def test_whatever_a_cell_holds_the_stored_text_fits_an_alert_when_shown(text):
    page = _table(
        ["Reward", "Source", "Code"],
        [html.escape(text), html.escape(text), C1],
        heading="Permanent codes",
    )
    (item,) = await _collect_body(page)
    details = _details(item)
    for stored in (details.reward, details.origin):
        assert discord_len(esc(stored)) <= 300
        assert stored == "" or stored == cap_shown(stored, 300)


async def test_a_reward_is_not_cut_when_it_fits_exactly_and_is_when_one_unit_over():
    exact = _details((await _collect_body(_table(["Reward", "Code"], ["a" * 300, C1])))[0])
    over = _details((await _collect_body(_table(["Reward", "Code"], ["a" * 301, C1])))[0])
    assert exact.reward == "a" * 300
    assert over.reward == "a" * 299 + "…"


async def test_an_emoji_at_the_cut_is_dropped_whole_not_split():
    # 299 units of "a" then an emoji (2 units) would be 301: the emoji must go, and the cut
    # text must stay valid UTF-16 (no lone surrogate, which encode() would choke on).
    page = _table(["Reward", "Code"], ["a" * 299 + "\U0001f52b" + "tail", C1])
    (item,) = await _collect_body(page)
    reward = _details(item).reward
    assert reward == "a" * 299 + "…"
    reward.encode("utf-16-le")  # raises on a lone surrogate


@pytest.mark.parametrize(
    "text",
    [
        "x" * 290 + "https://evil.example/path",
        "x" * 287 + "discord.gg/abc",
        "x" * 295 + "<@&123456789012345678>",
        "*" * 149 + "@everyone",
        "\U0001f52b" * 150 + "tail",
        "ab" + "\U0001f468\u200d\U0001f469\u200d\U0001f467" * 40,
    ],
)
@pytest.mark.parametrize("limit", [1, 2, 3, 7, 40, 299, 300])
def test_cap_shown_fits_at_every_limit_even_when_escaping_changes_near_the_cut(text, limit):
    capped = cap_shown(text, limit)
    assert discord_len(esc(capped)) <= limit
    capped.encode("utf-16-le")
    assert cap_shown(capped, limit) == capped  # capping twice changes nothing


async def test_a_numeric_surrogate_charref_does_not_crash_the_encoder():
    page = _table(["Reward", "Code"], ["bad &#xD800; and &#55357; refs &#x110000;", C1])
    (item,) = await _collect_body(page)
    discord_len(_details(item).reward)  # raises if a lone surrogate got through


async def test_script_and_style_inside_a_cell_are_not_part_of_the_cell():
    page = (
        "<table><tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>a<script>var x='{C2}';</script>b<style>.c{{content:'{C2}'}}</style>c</td>"
        f"<td>{C1}</td></tr></table>"
    )
    (item,) = await _collect_body(page)
    assert item.title == C1
    assert _details(item).reward == "abc"


async def test_a_self_closing_script_does_not_swallow_the_table_after_it():
    page = f'<script src="/x.js"/>{_table(["Code"], [C1])}'
    assert [i.title for i in await _collect_body(page)] == [C1]


async def test_an_unclosed_script_hides_only_what_follows_it():
    page = _table(["Code"], [C1]) + f"<script>var x = 1; {_table(['Code'], [C2])}"
    assert [i.title for i in await _collect_body(page)] == [C1]


async def test_a_table_inside_a_comment_is_ignored():
    page = f"<!-- {_table(['Code'], [C2])} -->{_table(['Code'], [C1])}"
    assert [i.title for i in await _collect_body(page)] == [C1]


async def test_a_page_whose_only_table_is_in_a_comment_is_a_failed_pass():
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(f"<!-- {_table(['Code'], [C1])} -->")


async def test_a_table_in_a_conditional_comment_is_ignored():
    page = f"<!--[if IE]>{_table(['Code'], [C2])}<![endif]-->{_table(['Code'], [C1])}"
    assert [i.title for i in await _collect_body(page)] == [C1]


@pytest.mark.parametrize(
    "page",
    [
        "<<<>>><table><tr><td><<<",
        "<table <tr <td>",
        '<table><tr><td class="x>' + C1,
        "<table><tr><td>" + C1 + "<tr><td>" + C2,
        "</td></tr></table></table></table>" + _table(["Code"], [C1]),
        "<table><tr><th>Code</th></tr><tr><td><b><i>" + C1 + "</u></b>",
        "<td>" + C1 + "</td>",
        "<tr><th>Code</th></tr><tr><td>" + C1 + "</td></tr>",
        "<table><caption>Code</caption>" + C1 + "</table>",
        "<table><tr><th>Code</th></tr><tr><td>" + C1 + "</td></table><table></tr>",
        "<!--" + C1,
        "<![CDATA[ " + C1 + " ]]>",
        "<?php echo '" + C1 + "'; ?>",
        "&" * 10_000 + "<table>",
    ],
)
async def test_malformed_markup_is_either_read_or_a_failed_pass_never_a_crash(page):
    try:
        items = await _collect_body(page)
    except ValueError as exc:
        assert "no SHiFT code tables found" in str(exc)
    else:
        assert all(is_code(i.title) for i in items)


async def test_unclosed_cells_and_rows_are_still_read():
    page = (
        "<table><tr><th>Reward<th>Code<th>Expire"
        f"<tr><td>3 Golden Keys<td>{C1}<td>Oct 20, 2026"
        f"<tr><td>1 Golden Key<td>{C2}<td>Unknown"
    )
    by_code = _by_code(await _collect_body(page))
    assert by_code[C1].expires_on == date(2026, 10, 20)
    assert by_code[C2].reward == "1 Golden Key"


async def test_ten_thousand_nested_tables_neither_recurse_nor_hide_the_neighbors():
    nested = "<table><tr><td>" * 10_000 + C2 + "</td></tr></table>" * 10_000
    page = _table(["Code"], [C1]) + nested + _table(["Code"], ["N9P2Q-3R4S5-T6V7W-X8Y9Z-A2B3C"])
    titles = [i.title for i in await _collect_body(page)]
    assert titles[0] == C1
    assert titles[-1] == "N9P2Q-3R4S5-T6V7W-X8Y9Z-A2B3C"
    assert C2 not in titles


async def test_a_deep_pile_of_unclosed_inline_tags_is_fine():
    page = "<b>" * 100_000 + _table(["Code"], [C1])
    assert [i.title for i in await _collect_body(page)] == [C1]


async def test_a_hundred_thousand_empty_rows_are_a_failed_pass_not_a_hang():
    page = "<table><tr><th>Code</th></tr>" + "<tr><td></td></tr>" * 100_000 + "</table>"
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(page)


async def test_a_thousand_code_tables_of_one_row_each_stop_at_the_row_budget():
    tables = "".join(
        f"<table><tr><th>Code</th></tr><tr><td>{i:05d}-{i:05d}-{i:05d}-{i:05d}-{i:05d}</td></tr></table>"
        for i in range(1, 3001)
    )
    # Two rows a table; the page-wide cap is five thousand rows. That is the parser's budget,
    # asked of the parser: since QA batch 1 (fix 8) a collector will not hand over more than 500
    # codes from one page, so the 2,500 that survive the budget make a failed pass instead.
    parsed, truncated = parse_tables(tables)
    assert sum(len(t.rows) for t in parsed) == 5000
    assert truncated
    with pytest.raises(ValueError, match="too many rows"):
        await _collect_body(tables)


async def test_bytes_that_are_not_utf8_become_replacement_characters_and_the_page_still_reads():
    body = (
        b"<table><tr><th>Reward</th><th>Code</th></tr><tr><td>Caf\xe9 \x92s skin \xff\xfe</td><td>"
        + C1.encode()
        + b"</td></tr></table>"
    )
    (item,) = await _collect_body(body)
    assert item.title == C1
    assert "�" in _details(item).reward
    assert _details(item).category == "cosmetic"  # the word "skin" survived the bad bytes


async def test_an_invalid_byte_glued_to_a_code_does_not_hide_it():
    body = b"<table><tr><th>Code</th></tr><tr><td>\xff" + C1.encode() + b"\xc3</td></tr></table>"
    assert [i.title for i in await _collect_body(body)] == [C1]


async def test_a_utf8_bom_and_a_truncated_multibyte_tail_are_harmless():
    page = (_table(["Code"], [C1])).encode()
    body = b"\xef\xbb\xbf" + page + "é".encode()[:1]
    assert [i.title for i in await _collect_body(body)] == [C1]


async def test_a_page_in_utf16_is_a_failed_pass_not_garbage_codes():
    body = _table(["Code"], [C1]).encode("utf-16")
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await _collect_body(body)


def test_parse_tables_keeps_cells_aligned_when_a_cell_is_empty():
    tables, _ = parse_tables("<table><tr><td></td><td>b</td><td></td></tr></table>")
    assert tables[0].rows == [["", "b", ""]]


# --- dates ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("September 30 2026", date(2026, 9, 30)),  # no comma
        ("oct 6th 2026", date(2026, 10, 6)),
        ("Feb 29, 2028", date(2028, 2, 29)),  # a leap year
        ("Feb 29, 2032", date(2032, 2, 29)),
        ("Sept. 30, 2026", date(2026, 9, 30)),
        ("Oct 1st, 2026", date(2026, 10, 1)),
        ("Oct 22nd, 2026", date(2026, 10, 22)),
        ("Oct 23rd, 2026", date(2026, 10, 23)),
        ("Oct 06, 2026", date(2026, 10, 6)),
    ],
)
def test_more_date_shapes_that_do_parse(text, expected):
    assert parse_shift_date(text) == expected


def test_an_all_caps_ordinal_parses_like_an_all_caps_month():
    assert parse_shift_date("OCT 6TH, 2026") == date(2026, 10, 6)


@pytest.mark.parametrize(
    "text",
    [
        "Feb 29, 2027",  # not a leap year
        "Feb 29, 2100",  # divisible by 100, not 400
        "Sept 31, 2026",
        "Apr 31, 2026",
        "Oct 32, 2026",
        "Oct 6th 26",
        "Oct 6, 2026 (maybe)",
        "Oct 6, 2026 or so",
        "maybe Oct 6, 2026",
        "Oct 6 2026 - Oct 9 2026",
        "Oct 6, 2026 Oct 9, 2026",
        "2026-10-06",
        "10/06/2026",
        "6 October 2026",
        "October, 2026",
        "Q4 2026",
        "TBD",
        "N/A",
        "Never",
        "-",
        "Oct 6, 2026\x00",
        "Oct 6,\n2026 ; DROP TABLE",
        "Oct 6, " + "2" * 5000,
        "O" * 10_000 + " 6, 2026",
        "Oct " + "6" * 10_000 + ", 2026",
    ],
)
def test_dates_that_should_not_parse_are_none(text):
    assert parse_shift_date(text) is None


async def test_an_unparseable_expiry_means_no_expiry_and_leaves_the_rest_of_the_row_alone(caplog):
    page = _table(
        ["Reward", "Added", "Code", "Expire"],
        ["3 Golden Keys", "Oct 6, 2026", C1, "Oct 20, 2026 (maybe)"],
        ["1 Golden Key", "Oct 7, 2026", C2, "Feb 29, 2027"],
    )
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.shift_table"):
        items = await _collect_body(page)
    by_code = _by_code(items)
    assert by_code[C1].expires_on is None
    assert by_code[C1].reward == "3 Golden Keys"
    assert by_code[C1].added_on == date(2026, 10, 6)
    assert by_code[C2].expires_on is None
    assert "2 unparseable dates" in caplog.text  # a count, once per pass, not a line a row
    assert caplog.text.count("unparseable") == 1


async def test_an_unparseable_added_date_means_no_publish_time_but_keeps_the_expiry():
    details_and_item = await _collect_body(
        _table(["Added", "Code", "Expire"], ["Feb 29, 2027", C1, "Oct 20, 2026"])
    )
    (item,) = details_and_item
    assert item.published_at is None
    assert _details(item).added_on is None
    assert _details(item).expires_on == date(2026, 10, 20)


async def test_a_date_split_by_nbsp_and_tags_still_reads_as_a_date():
    page = _table(["Code", "Expire"], [C1, "<b>Oct</b>&nbsp;<i>6th</i>,&nbsp;2026"])
    (item,) = await _collect_body(page)
    assert _details(item).expires_on == date(2026, 10, 6)


@pytest.mark.parametrize("junk", ["\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u200e"])
async def test_an_invisible_character_beside_a_date_does_not_erase_the_expiry(junk):
    page = _table(["Code", "Expire"], [C1, f"Sept 30, 2026{junk}"])
    (item,) = await _collect_body(page)
    assert _details(item).expires_on == date(2026, 9, 30)


async def test_expiry_at_a_dst_boundary_is_just_a_calendar_date():
    page = _table(["Code", "Added", "Expire"], [C1, "Nov 1, 2026", "Nov 1, 2026"])
    (item,) = await _collect_body(page)
    # Nov 1, 2026 is the day the clocks go back; midnight is still PDT.
    assert item.published_at == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert _details(item).expires_on == date(2026, 11, 1)


async def test_the_spring_forward_day_starts_at_midnight_pst():
    page = _table(["Code", "Added"], [C1, "Mar 8, 2026"])
    (item,) = await _collect_body(page)
    assert item.published_at == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
