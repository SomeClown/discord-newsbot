"""QA round 2, areas 5 and 6: table guards and the 304 rule (batch d36d492).

Area 5 attacks the page-level guards from the outside, with the real collector reading
hand-made HTML: the 500 code cap at the edge and across tables, the "expired" heading in its
disguises (nested markup, every heading level, one heading over several tables), codes that
sit in both an expired and a live table, and code-shaped text in rewards. Area 6 attacks the
"a 304 only answers a conditional request" rule from the server's side: a server that ignores
the condition, a 304 with a body or a Location, a 304 reaching the collection pass.

Invisible and non-ASCII characters are built with `chr()` or escapes, never typed.
"""

from __future__ import annotations

import httpx
import pytest
from test_shift_details_detection import T0, X, Y, code_n, main_table, page
from test_shift_details_detection_adversarial import read_named

from newsbot.collectors.base import run_collectors
from newsbot.collectors.rss import _MAX_RESPONSE_BYTES
from newsbot.collectors.shift_table import (
    _MAX_CODES,
    PageValidators,
    ShiftTableCollector,
    Validators,
)
from newsbot.config import ShiftTableSource
from newsbot.shift.match import find_codes

URL = "https://codes.example/borderlands-4-shift-codes/"
LIVE_TABLE = main_table(("1 Golden Key", "Oct 9, 2026", X, "Unknown"))
A_CODE = "ABCDE-12345-FGHIJ-67890-KLMNO"


def _plain_table(heading: str, *rows: tuple[str, str]) -> str:
    """A reward/code table under `heading` (markup allowed in it, since that's the point)."""
    body = "".join(f"<tr><td>{reward}</td><td>{code}</td></tr>" for reward, code in rows)
    return f"{heading}<table><tr><th>Reward</th><th>Code</th></tr>{body}</table>"


async def _titles(html: str) -> list[str]:
    return sorted(i.title for i in await read_named(html, T0))


# --- 500 versus 501 codes ---


def _rows(start: int, count: int) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(("1 Golden Key", "Unknown", code_n(start + i), "Unknown") for i in range(count))


async def test_five_hundred_codes_across_two_tables_pass_and_five_hundred_and_one_fail():
    assert _MAX_CODES == 500
    ok = page(main_table(*_rows(0, 250)), main_table(*_rows(250, 250)))
    assert len(await read_named(ok, T0)) == 500
    too_many = page(main_table(*_rows(0, 250)), main_table(*_rows(250, 251)))
    with pytest.raises(ValueError, match="too many rows"):
        await read_named(too_many, T0)


async def test_five_hundred_distinct_codes_plus_repeats_of_them_still_pass():
    html = page(main_table(*_rows(0, 500)), main_table(*_rows(0, 500)))
    assert len(await read_named(html, T0)) == 500


async def test_the_501st_code_in_an_expired_table_does_not_count():
    html = page(
        main_table(*_rows(0, 500)),
        _plain_table("<h2>Expired codes</h2>", (("old", code_n(9000)))),
    )
    assert len(await read_named(html, T0)) == 500


async def test_the_cap_error_names_the_count_and_the_limit():
    with pytest.raises(ValueError) as excinfo:
        await read_named(page(main_table(*_rows(0, 501))), T0)
    assert "501" in str(excinfo.value) and "500" in str(excinfo.value)


# --- "expired" in a heading, in disguise ---

DISGUISES = [
    "<h2>Expired codes</h2>",
    "<h2><span>Exp</span>ired <b>codes</b></h2>",
    "<h3><a href='#x'><em>EXPIRED</em></a></h3>",
    "<h4>Expi&shy;red</h4>",
    "<h2>&#69;xpired</h2>",
    "<h2>Exp<!-- a comment -->ired</h2>",
    "<h2>Expired<br>codes</h2>",
    "<H2>EXPIRED</H2>",
    "<h2 class='codes' id='active'>Expired (do not redeem)</h2>",
    "<h2>Exp" + chr(0x200B) + "ired</h2>",
    "<h2>Exp" + chr(0xAD) + "ired</h2>",
]


@pytest.mark.parametrize("heading", DISGUISES)
async def test_an_expired_heading_in_any_disguise_skips_its_table(heading):
    html = _plain_table(heading, ("old", Y)) + LIVE_TABLE
    assert await _titles(html) == [X]


@pytest.mark.parametrize("level", ["h1", "h5", "h6"])
async def test_an_expired_heading_at_any_level_skips_its_table(level):
    html = _plain_table(f"<{level}>Expired codes</{level}>", ("old", Y)) + LIVE_TABLE
    assert await _titles(html) == [X]


async def test_one_expired_heading_covers_every_table_until_the_next_heading():
    html = (
        "<h2>Expired</h2>"
        + _plain_table("", ("a", Y))
        + _plain_table("", ("b", code_n(7)))
        + _plain_table("<h2>Active codes</h2>", ("c", code_n(8)))
        + LIVE_TABLE
    )
    assert await _titles(html) == sorted([code_n(8), X])


async def test_an_expired_heading_inside_a_div_still_covers_a_table_in_the_next_div():
    html = "<div><h2>Expired</h2></div><div>" + _plain_table("", ("old", Y)) + "</div>" + LIVE_TABLE
    assert await _titles(html) == [X]


async def test_an_expired_table_that_also_lists_live_codes_is_skipped_whole():
    # The page's own word for the table wins over the codes that might still work: a code
    # that is really live is on the page's live table too, and gets read from there.
    expired = _plain_table("<h2>Expired codes</h2>", ("old", Y), ("still works?", code_n(5)))
    assert await _titles(expired + LIVE_TABLE) == [X]


async def test_a_code_in_both_an_expired_and_a_live_table_takes_the_live_rows_details():
    html = _plain_table("<h2>Expired codes</h2>", ("5 Golden Keys", X)) + main_table(
        ("2 Golden Keys", "Oct 9, 2026", X, "Unknown")
    )
    (item,) = await read_named(html, T0)
    assert item.details.reward == "2 Golden Keys"


async def test_a_blank_heading_after_an_expired_one_resets_it():
    html = "<h2>Expired</h2><h2> </h2>" + _plain_table("", ("fine", Y))
    assert await _titles(html) == [Y]


# --- [code] replacement next to the row's own code ---


async def test_a_reward_that_repeats_its_own_rows_code_is_defused_and_the_row_keeps_its_code():
    (item,) = await read_named(page(main_table((f"Use {X} for keys", "Oct 9, 2026", X, "x"))), T0)
    assert item.title == X
    assert item.details.reward == "Use [code] for keys"
    assert find_codes(f"{item.title}\n{item.full_text}") == [X]


@pytest.mark.parametrize(
    ("reward", "shown"),
    [
        (f"Use {X.lower()} now", "Use [code] now"),
        (f"({X})", "([code])"),
        (f"{X} and {A_CODE}", "[code] and [code]"),
        ("Use ABCDE-<b>12345</b>-FGHIJ-67890-KLMNO now", "Use [code] now"),
        ("say [code] ok", "say [code] ok"),
    ],
)
async def test_code_shaped_reward_text_in_any_spelling_becomes_the_placeholder(reward, shown):
    (item,) = await read_named(page(main_table((reward, "Oct 9, 2026", Y, "x"))), T0)
    assert item.details.reward == shown


@pytest.mark.parametrize(
    "reward",
    [
        "ABCDE-12345-FGHIJ-67890-KLMN",  # one character short
        "ABCDE-12345-FGHIJ-67890-K" + chr(0x200B) + "LMNO",  # split by an invisible character
        "ABCDE" + chr(0xFF0D) + "12345" + chr(0xFF0D) + "FGHIJ" + chr(0xFF0D) + "67890",
    ],
)
async def test_text_that_is_not_code_shaped_is_left_alone(reward):
    (item,) = await read_named(page(main_table((reward, "Oct 9, 2026", Y, "x"))), T0)
    assert item.details.reward == reward


async def test_a_reward_made_of_many_codes_is_defused_before_the_cap_not_after():
    reward = " ".join([A_CODE] * 60)  # 1,800 characters of codes; "[code] " * 60 is 420
    (item,) = await read_named(page(main_table((reward, "Oct 9, 2026", Y, "x"))), T0)
    assert find_codes(item.details.reward) == []
    assert "ABCDE" not in item.details.reward
    assert len(item.details.reward) <= 300
    assert item.details.reward.startswith("[code] [code]")


async def test_a_second_code_in_the_reward_cell_never_becomes_a_second_item():
    html = f"<table><tr><th>Reward</th><th>Code</th></tr><tr><td>{X}</td><td>{Y}</td></tr></table>"
    items = await read_named(html, T0)
    assert [i.title for i in items] == [Y]
    assert items[0].details.reward == "[code]"


# --- 304: the server's side of the conversation ---


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_seconds):
    return None


def _collector(validators):
    source = ShiftTableSource(type="shift_table", name="mentalmars", url=URL, trust="community")
    return ShiftTableCollector(source, validators=validators, sleep=_no_sleep, clock=lambda: T0)


async def test_a_server_that_ignores_the_condition_and_sends_the_whole_page_is_read_normally():
    validators = PageValidators({URL: Validators('"v1"', None)})
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, content=page(LIVE_TABLE).encode(), headers={"ETag": '"v2"'})

    async with _client(handler) as http:
        items = await _collector(validators).collect(http)

    assert seen == ['"v1"']
    assert [i.title for i in items] == [X]
    assert validators.pending == {URL: Validators('"v2"', None)}  # staged for the owner to commit
    assert validators == {URL: Validators('"v1"', None)}  # but not current until then


async def test_a_server_that_ignores_the_condition_every_pass_gives_the_same_items_every_pass():
    validators = PageValidators()
    body = page(LIVE_TABLE).encode()

    def handler(request):
        return httpx.Response(200, content=body, headers={"ETag": '"same"'})

    async with _client(handler) as http:
        first = await _collector(validators).collect(http)
        validators.commit_all()
        second = await _collector(validators).collect(http)

    assert [i.title for i in first] == [i.title for i in second] == [X]


async def test_a_200_with_an_empty_body_to_a_conditional_request_is_a_failed_pass_not_unchanged():
    validators = PageValidators({URL: Validators('"v1"', None)})
    async with _client(lambda request: httpx.Response(200, content=b"")) as http:
        with pytest.raises(ValueError, match="no SHiFT code tables found"):
            await _collector(validators).collect(http)
    assert validators == {URL: Validators('"v1"', None)}
    assert validators.pending == {}


async def test_a_304_with_a_location_header_is_not_followed():
    validators = PageValidators({URL: Validators('"v1"', None)})
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(304, headers={"Location": "http://169.254.169.254/latest/"})

    async with _client(handler) as http:
        assert await _collector(validators).collect(http) == []
    assert requests == [URL]


async def test_a_304_with_a_huge_body_is_not_read():
    validators = PageValidators({URL: Validators('"v1"', None)})
    read = []

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3 * _MAX_RESPONSE_BYTES // 65536):
                read.append(1)
                yield b"x" * 65536

    async with _client(lambda request: httpx.Response(304, stream=Endless())) as http:
        assert await _collector(validators).collect(http) == []
    assert read == []  # the answer was "nothing changed"; nobody asked for the rest


async def test_a_304_in_the_collection_pass_is_a_healthy_source_with_no_items():
    validators = PageValidators({URL: Validators('"v1"', None)})
    async with _client(lambda request: httpx.Response(304)) as http:
        (result,) = await run_collectors([_collector(validators)], http)
    assert result.error is None
    assert result.items == []


async def test_an_unrequested_304_in_the_collection_pass_is_a_failed_source():
    async with _client(lambda request: httpx.Response(304)) as http:
        (result,) = await run_collectors([_collector(PageValidators())], http)
    assert result.error is not None
    assert result.items == []
