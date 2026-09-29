"""Tests for `fetch_page`: httpx.MockTransport stands in for Wikiquote, no network.

The JSON envelopes are built around the saved fixtures, so the happy path
carries real page HTML and the parser gets a look at what the fetch returned.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from newsbot import useragent
from newsbot.lounge import wikiquote
from newsbot.lounge.wikiquote import (
    API_URL,
    MAX_API_BYTES,
    FetchedPage,
    WikiquoteError,
    fetch_page,
    parse_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wikiquote"
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}


def _envelope(title: str, revid: int, html: str) -> bytes:
    return json.dumps(
        {
            "parse": {
                "title": title,
                "pageid": 1,
                "revid": revid,
                "text": html,
                "displaytitle": title,
            }
        }
    ).encode()


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def _error_response(code: str, info: str) -> httpx.Response:
    body = json.dumps({"error": {"code": code, "info": info, "docref": "see the API docs"}})
    return httpx.Response(200, content=body.encode(), headers=JSON_HEADERS)


async def test_fetch_returns_title_revid_and_html_and_the_html_parses():
    html = (FIXTURES / "oscar_wilde.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_envelope("Oscar Wilde", 3988830, html), headers=JSON_HEADERS
        )

    async with _client(handler) as http:
        page = await fetch_page(http, "Oscar Wilde")

    assert page == FetchedPage("Oscar Wilde", 3988830, html)
    assert parse_page(page.title, page.html).kind == "author"


async def test_fetch_sends_the_documented_parameters():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=_envelope("Oscar Wilde", 1, "<p>x</p>"), headers=JSON_HEADERS
        )

    async with _client(handler) as http:
        await fetch_page(http, "Oscar Wilde")

    (request,) = seen
    assert request.method == "GET"
    assert f"{request.url.scheme}://{request.url.host}{request.url.path}" == API_URL
    query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
    assert query == {
        "action": "parse",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
        "prop": "text|revid|displaytitle",
        "disableeditsection": "1",
        "disablelimitreport": "1",
        "disabletoc": "1",
        "page": "Oscar Wilde",
    }


async def test_fetch_uses_the_injected_clients_user_agent():
    headers = useragent.user_agent_headers({"NEWSBOT_CONTACT": "https://example.invalid/contact"})
    ((name, value),) = headers.items()
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(name))
        return httpx.Response(200, content=_envelope("T", 1, ""), headers=JSON_HEADERS)

    async with _client(handler, headers=headers) as http:
        await fetch_page(http, "T")

    assert seen == [value]
    assert "https://example.invalid/contact" in value


async def test_fetch_resolves_a_redirect_the_api_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        # redirects=1 makes the API answer with the target page's own title.
        return httpx.Response(
            200, content=_envelope("Oscar Wilde", 7, "<p>x</p>"), headers=JSON_HEADERS
        )

    async with _client(handler) as http:
        page = await fetch_page(http, "Oscar Fingal O'Flahertie Wills Wilde")

    assert page.title == "Oscar Wilde"


async def test_missingtitle_names_the_page_and_suggests_a_rename():
    async with _client(
        lambda r: _error_response("missingtitle", "The page you specified doesn't exist.")
    ) as http:
        with pytest.raises(WikiquoteError) as excinfo:
            await fetch_page(http, "Oscar Wild")
    assert str(excinfo.value) == "Wikiquote has no page called Oscar Wild (renamed or deleted?)"


async def test_invalidtitle_says_the_title_is_invalid():
    async with _client(lambda r: _error_response("invalidtitle", 'Bad title "x|y".')) as http:
        with pytest.raises(WikiquoteError) as excinfo:
            await fetch_page(http, "x|y")
    assert str(excinfo.value) == "x|y isn't a valid Wikiquote page title"


async def test_other_api_errors_pass_along_code_and_info_on_one_line():
    async with _client(
        lambda r: _error_response("readapidenied", "You need read\npermission.")
    ) as http:
        with pytest.raises(WikiquoteError) as excinfo:
            await fetch_page(http, "Oscar Wilde")
    assert str(excinfo.value) == "Wikiquote said readapidenied: You need read permission."


@pytest.mark.parametrize("status", [429, 503, 500, 404])
async def test_http_failures_are_plain_failures_with_the_status(status):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=b"nope", headers={"content-type": "text/html"})

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match=f"HTTP {status}"):
            await fetch_page(http, "Oscar Wilde")
    assert len(calls) == 1  # no retry loop


async def test_a_redirect_is_a_failure_and_is_not_followed():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(301, headers={"location": "https://en.wikiquote.org/w/other.php"})

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match="redirect.*301"):
            await fetch_page(http, "Oscar Wilde")
    assert len(calls) == 1


async def test_a_200_served_as_html_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>hi</html>", headers={"content-type": "text/html"}
        )

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match="text/html, not JSON"):
            await fetch_page(http, "Oscar Wilde")


async def test_a_200_with_no_content_type_is_rejected():
    async with _client(lambda r: httpx.Response(200, content=b"{}")) as http:
        with pytest.raises(WikiquoteError, match="no content type"):
            await fetch_page(http, "Oscar Wilde")


@pytest.mark.parametrize("body", [b"not json at all", b"", b"[1, 2]", b'{"nothing": true}'])
async def test_non_json_or_wrong_shape_is_a_failure(body):
    async with _client(lambda r: httpx.Response(200, content=body, headers=JSON_HEADERS)) as http:
        with pytest.raises(WikiquoteError):
            await fetch_page(http, "Oscar Wilde")


@pytest.mark.parametrize(
    "parse",
    [
        {"title": "T", "revid": 1},  # no text
        {"title": "T", "revid": "1", "text": ""},  # revid isn't an int
        {"title": "T", "revid": 1, "text": {"*": "<p>x</p>"}},  # formatversion 1's shape
    ],
)
async def test_a_parse_object_missing_its_pieces_is_a_failure(parse):
    body = json.dumps({"parse": parse}).encode()
    async with _client(lambda r: httpx.Response(200, content=body, headers=JSON_HEADERS)) as http:
        with pytest.raises(WikiquoteError, match="missing the title, revision or text"):
            await fetch_page(http, "T")


async def test_a_body_over_the_cap_is_rejected_while_streaming():
    sent = 0

    async def endless():
        nonlocal sent
        chunk = b"x" * (1024 * 1024)
        for _ in range(64):
            sent += len(chunk)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless(), headers=JSON_HEADERS)

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match="over 4 MiB"):
            await fetch_page(http, "Oscar Wilde")
    assert MAX_API_BYTES < sent < 64 * 1024 * 1024  # it quit reading, it didn't slurp everything


async def test_a_body_exactly_at_the_cap_is_not_rejected_for_size():
    # A valid envelope padded out to exactly MAX_API_BYTES.
    base = _envelope("T", 1, "")
    padded = _envelope("T", 1, "x" * (MAX_API_BYTES - len(base)))
    assert len(padded) == MAX_API_BYTES
    async with _client(lambda r: httpx.Response(200, content=padded, headers=JSON_HEADERS)) as http:
        page = await fetch_page(http, "T")
    assert len(page.html) == MAX_API_BYTES - len(base)


async def test_a_slow_drip_past_the_deadline_times_out(monkeypatch):
    # The real deadline is 10 seconds; nobody wants a test that sleeps that long.
    monkeypatch.setattr(wikiquote, "FETCH_TIMEOUT_S", 0.05)

    async def drip():
        yield b'{"parse": '
        await asyncio.sleep(5)
        yield b"{}}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=drip(), headers=JSON_HEADERS)

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match="didn't answer within 0.05 seconds"):
            await asyncio.wait_for(fetch_page(http, "Oscar Wilde"), timeout=3)


def test_the_real_deadline_is_ten_seconds():
    assert wikiquote.FETCH_TIMEOUT_S == 10.0


async def test_a_transport_error_becomes_a_wikiquote_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match="couldn't reach Wikiquote .*ConnectError"):
            await fetch_page(http, "Oscar Wilde")
