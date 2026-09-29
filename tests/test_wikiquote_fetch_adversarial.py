"""Adversarial tests for `fetch_page`, beyond `test_wikiquote_fetch.py`.

The brief (test-engineer, 2026-09-29): Wikimedia is a very reliable neighbor
right up until the morning it isn't, and whatever it says ends up, one way or
another, in a message an admin reads. So every misbehavior here goes through
`httpx.MockTransport` (never the network) and the question is always the same:
does the caller get a `WikiquoteError` with a short, one-line, useful message,
or does something else escape? Response bodies must not be echoed into that
message, with one deliberate exception: the API's own `info` string on an
error envelope, which is the whole point of the message.

The `xfail(strict=True)` tests are real bugs; when the app gets fixed the xfail
starts failing loudly and somebody deletes the marker.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import zlib
from collections.abc import AsyncIterator, Callable
from urllib.parse import parse_qs

import httpx
import pytest

from newsbot import useragent
from newsbot.lounge import wikiquote
from newsbot.lounge.wikiquote import MAX_API_BYTES, FetchedPage, WikiquoteError, fetch_page

JSON = {"content-type": "application/json; charset=utf-8"}
BODY_MARKER = "BODY_MARKER-BODY-MARKER-12345"


def _envelope(html: str = "<p>hi</p>", **overrides) -> bytes:
    parse = {"title": "T", "revid": 1, "text": html, **overrides}
    return json.dumps({"parse": {k: v for k, v in parse.items() if v is not None}}).encode()


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def _stream(data: bytes, size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """A body that arrives in chunks (so httpx doesn't see a single sized blob)."""

    async def gen():
        for i in range(0, len(data), size):
            yield data[i : i + size]

    return gen()


async def _fetch(response: httpx.Response, title: str = "T") -> FetchedPage:
    async with _client(lambda r: response) as http:
        return await fetch_page(http, title)


async def _message(response: httpx.Response, title: str = "T") -> str:
    with pytest.raises(WikiquoteError) as excinfo:
        await _fetch(response, title)
    return str(excinfo.value)


def _assert_one_readable_line(message: str) -> None:
    assert message.strip()
    assert "\n" not in message
    assert "\r" not in message
    assert "Traceback" not in message
    assert "<httpx" not in message


# --- Body size, chunked ---


async def test_a_chunked_body_of_exactly_the_cap_is_accepted():
    base = _envelope("")
    padded = _envelope("x" * (MAX_API_BYTES - len(base)))
    assert len(padded) == MAX_API_BYTES
    page = await _fetch(httpx.Response(200, content=_stream(padded), headers=JSON))
    assert page.title == "T"


async def test_a_chunked_body_one_byte_over_the_cap_is_rejected():
    padded = _envelope("x" * (MAX_API_BYTES - len(_envelope("")) + 1))
    assert len(padded) == MAX_API_BYTES + 1
    message = await _message(httpx.Response(200, content=_stream(padded), headers=JSON))
    assert message == "Wikiquote's response is over 4 MiB"


async def test_a_body_that_is_all_one_giant_chunk_over_the_cap_is_rejected():
    body = b"x" * (MAX_API_BYTES + 1)
    message = await _message(httpx.Response(200, content=_stream(body, len(body)), headers=JSON))
    assert "over 4 MiB" in message


async def test_a_gzip_bomb_is_measured_after_decompression():
    bomb = gzip.compress(b"x" * (MAX_API_BYTES + 1024))
    assert len(bomb) < 100_000
    headers = {**JSON, "content-encoding": "gzip"}
    message = await _message(httpx.Response(200, content=_stream(bomb), headers=headers))
    assert "over 4 MiB" in message


# --- Timeouts ---


async def test_a_slow_drip_that_finishes_inside_the_deadline_succeeds(monkeypatch):
    monkeypatch.setattr(wikiquote, "FETCH_TIMEOUT_S", 1.0)
    body = _envelope()

    async def drip():
        for i in range(0, len(body), 20):
            yield body[i : i + 20]
            await asyncio.sleep(0.01)

    page = await _fetch(httpx.Response(200, content=drip(), headers=JSON))
    assert page.html == "<p>hi</p>"


async def test_a_drip_that_outlasts_the_deadline_times_out_even_if_every_read_is_quick(monkeypatch):
    monkeypatch.setattr(wikiquote, "FETCH_TIMEOUT_S", 0.15)
    body = _envelope("x" * 2000)

    async def drip():
        for i in range(0, len(body), 10):
            yield body[i : i + 10]
            await asyncio.sleep(0.005)  # each gap is tiny; the sum is not

    message = await _message(httpx.Response(200, content=drip(), headers=JSON))
    assert message == "Wikiquote didn't answer within 0.15 seconds"


async def test_a_handler_that_never_answers_times_out_as_a_wikiquote_error(monkeypatch):
    monkeypatch.setattr(wikiquote, "FETCH_TIMEOUT_S", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, content=_envelope(), headers=JSON)

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match="within 0.05 seconds"):
            await asyncio.wait_for(fetch_page(http, "T"), timeout=3)


async def test_an_httpx_timeout_exception_is_reported_by_class_name():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError, match=r"couldn't reach Wikiquote \(ReadTimeout\)"):
            await fetch_page(http, "T")


# --- Content encodings and types ---


async def test_gzip_encoded_json_is_decoded():
    headers = {**JSON, "content-encoding": "gzip"}
    page = await _fetch(httpx.Response(200, content=gzip.compress(_envelope()), headers=headers))
    assert page.revid == 1


async def test_deflate_encoded_json_is_decoded():
    headers = {**JSON, "content-encoding": "deflate"}
    page = await _fetch(httpx.Response(200, content=zlib.compress(_envelope()), headers=headers))
    assert page.revid == 1


async def test_a_corrupt_gzip_body_is_a_wikiquote_error_not_a_decoding_error():
    headers = {**JSON, "content-encoding": "gzip"}
    message = await _message(httpx.Response(200, content=_stream(b"not gzip"), headers=headers))
    _assert_one_readable_line(message)
    assert "couldn't reach Wikiquote" in message


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "Application/JSON;charset=UTF-8",
        "  application/json  ;  charset=utf-8",
    ],
)
async def test_json_content_types_with_parameters_and_odd_case_are_accepted(content_type):
    page = await _fetch(
        httpx.Response(200, content=_envelope(), headers={"content-type": content_type})
    )
    assert page.title == "T"


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/json", "text/json"),
        ("text/html; charset=utf-8", "text/html"),
        ("application/json-seq", "application/json-seq"),
        ("application/vnd.api+json", "application/vnd.api+json"),
        ("", "no content type"),
        (";", "no content type"),
    ],
)
async def test_other_content_types_are_rejected_and_named(content_type, expected):
    response = httpx.Response(200, content=_envelope(), headers={"content-type": content_type})
    message = await _message(response)
    assert expected in message
    _assert_one_readable_line(message)


async def test_a_missing_content_type_header_is_rejected():
    response = httpx.Response(200, content=_envelope())
    response.headers.pop("content-type", None)
    assert "no content type" in await _message(response)


async def test_an_html_error_page_with_status_200_does_not_leak_its_body():
    body = f"<html><body>Oops {BODY_MARKER}</body></html>".encode()
    message = await _message(
        httpx.Response(200, content=body, headers={"content-type": "text/html"})
    )
    assert BODY_MARKER not in message
    _assert_one_readable_line(message)


async def test_an_html_error_page_served_as_json_is_just_invalid_json_and_not_echoed():
    body = f"<html><body>Oops {BODY_MARKER}</body></html>".encode()
    message = await _message(httpx.Response(200, content=body, headers=JSON))
    assert BODY_MARKER not in message
    _assert_one_readable_line(message)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503, 504, 204])
async def test_error_statuses_never_echo_the_response_body(status):
    body = f"upstream said {BODY_MARKER}\nsecond line".encode()
    response = httpx.Response(status, content=body, headers={"content-type": "text/plain"})
    message = await _message(response)
    assert BODY_MARKER not in message
    assert f"HTTP {status}" in message
    _assert_one_readable_line(message)


@pytest.mark.parametrize("headers", [{}, {"location": "https://evil.example/"}])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 304])
async def test_any_3xx_is_a_failure_with_or_without_a_location(status, headers):
    message = await _message(httpx.Response(status, headers=headers, content=BODY_MARKER.encode()))
    assert f"HTTP {status}" in message
    assert "evil.example" not in message
    assert BODY_MARKER not in message
    _assert_one_readable_line(message)


async def test_a_redirect_is_not_followed_even_when_the_client_would_follow_them():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/w/api.php"})

    async with _client(handler, follow_redirects=True) as http:
        with pytest.raises(WikiquoteError, match="redirect"):
            await fetch_page(http, "T")
    assert calls == ["en.wikiquote.org"]


# --- Bodies that decode badly ---


@pytest.mark.parametrize(
    "body",
    [
        b'\xff\xfe\xfd{"parse": 1}',
        b'{"parse": {"title": "T\xff", "revid": 1, "text": ""}}',
        b"\x00\x00\x00",
    ],
)
async def test_invalid_utf8_is_a_wikiquote_error(body):
    message = await _message(httpx.Response(200, content=body, headers=JSON))
    _assert_one_readable_line(message)


@pytest.mark.parametrize(
    "body", [b"null", b"true", b"42", b'"text"', b"[]", b"[{}]", b"{}", b"nan"]
)
async def test_valid_or_nearly_valid_json_of_the_wrong_kind_is_a_wikiquote_error(body):
    message = await _message(httpx.Response(200, content=body, headers=JSON))
    _assert_one_readable_line(message)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "json.loads raises RecursionError on about 100k nested brackets, which _unwrap only "
        "guards against ValueError; a 400 KB hostile body escapes fetch_page as a RecursionError"
    ),
)
async def test_deeply_nested_json_is_a_wikiquote_error_not_a_recursion_error():
    body = b"[" * 200_000 + b"]" * 200_000
    message = await _message(httpx.Response(200, content=body, headers=JSON))
    _assert_one_readable_line(message)


# --- Right JSON, wrong shape ---


@pytest.mark.parametrize(
    "payload",
    [
        {"parse": None},
        {"parse": []},
        {"parse": "text"},
        {"parse": {}},
        {"parse": {"title": "T", "revid": 1}},  # no text
        {"parse": {"title": "T", "text": "x"}},  # no revid
        {"parse": {"revid": 1, "text": "x"}},  # no title
        {"parse": {"title": "T", "revid": 1, "text": None}},
        {"parse": {"title": "T", "revid": 1, "text": 5}},
        {"parse": {"title": "T", "revid": 1, "text": ["<p>x</p>"]}},
        {"parse": {"title": None, "revid": 1, "text": "x"}},
        {"parse": {"title": 7, "revid": 1, "text": "x"}},
        {"parse": {"title": "T", "revid": None, "text": "x"}},
        {"parse": {"title": "T", "revid": "1", "text": "x"}},
        {"parse": {"title": "T", "revid": 1.5, "text": "x"}},
        {"parse": {"title": "T", "revid": [1], "text": "x"}},
    ],
)
async def test_a_page_object_that_is_not_quite_a_page_is_a_wikiquote_error(payload):
    message = await _message(
        httpx.Response(200, content=json.dumps(payload).encode(), headers=JSON)
    )
    assert message in {
        "Wikiquote's response had no page in it",
        "Wikiquote's response was missing the title, revision or text",
    }


async def test_a_page_with_an_empty_text_is_still_a_page():
    page = await _fetch(httpx.Response(200, content=_envelope(""), headers=JSON))
    assert page == FetchedPage("T", 1, "")


async def test_extra_unknown_keys_are_ignored():
    body = json.dumps(
        {
            "parse": {"title": "T", "revid": 1, "text": "x", "surprise": {"nested": [1, 2]}},
            "warnings": {"parse": {"warnings": "Something."}},
            "servedby": "mw-web.somewhere",
        }
    ).encode()
    assert (await _fetch(httpx.Response(200, content=body, headers=JSON))).html == "x"


async def test_a_lone_surrogate_in_the_json_survives_the_fetch_for_the_parser_to_handle():
    # JSON allows "\ud800". fetch_page hands it over untouched; what the parser does
    # with it is pinned in test_wikiquote_parse_adversarial.py.
    body = b'{"parse": {"title": "T", "revid": 1, "text": "a\\ud800b"}}'
    page = await _fetch(httpx.Response(200, content=body, headers=JSON))
    assert page.html == "a\ud800b"


# --- API error envelopes ---


def _error(payload: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode(), headers=JSON)


async def test_an_error_beats_a_parse_object_in_the_same_response():
    body = {"error": {"code": "internal_api_error", "info": "boom"}}
    body["parse"] = {"title": "T", "revid": 1, "text": "x"}  # type: ignore[assignment]
    message = await _message(_error(body))
    assert message == "Wikiquote said internal_api_error: boom"


async def test_a_null_error_next_to_a_good_page_is_not_an_error():
    body = {"error": None, "parse": {"title": "T", "revid": 1, "text": "x"}}
    page = await _fetch(_error(body))
    assert page.html == "x"


async def test_an_errors_array_from_another_errorformat_is_a_plain_failure():
    payload = {
        "errors": [{"code": "missingtitle", "text": "The page does not exist", "module": "parse"}]
    }
    message = await _message(_error(payload))
    assert message == "Wikiquote's response had no page in it"


@pytest.mark.parametrize("error", ["boom", 5, ["a", "b"], True, {}, {"code": None}])
async def test_an_error_that_is_not_the_documented_object_is_still_a_one_line_failure(error):
    message = await _message(_error({"error": error}))
    _assert_one_readable_line(message)
    assert message.startswith("Wikiquote said")


async def test_a_multi_line_info_is_collapsed_to_one_line():
    message = await _message(
        _error({"error": {"code": "x", "info": "line one\n\n  line\ttwo\r\nthree"}})
    )
    assert message == "Wikiquote said x: line one line two three"


async def test_missingtitle_names_the_requested_title_not_the_servers_words():
    message = await _message(
        _error({"error": {"code": "missingtitle", "info": BODY_MARKER}}), "Some Page"
    )
    assert message == "Wikiquote has no page called Some Page (renamed or deleted?)"
    assert BODY_MARKER not in message


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the error `code` is put in the admin message as-is (only `info` is whitespace-collapsed), "
        "so a code with a newline makes the message multi-line"
    ),
)
async def test_an_error_code_with_a_newline_still_gives_a_one_line_message():
    message = await _message(_error({"error": {"code": "bad\ncode", "info": "x"}}))
    _assert_one_readable_line(message)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the error `info` is echoed with no length cap, so a hostile or broken response can "
        "put up to 4 MiB into an admin message (Discord's limit is 2,000 characters)"
    ),
)
async def test_an_enormous_error_info_is_not_echoed_whole():
    message = await _message(_error({"error": {"code": "x", "info": "Z" * 100_000}}))
    assert len(message) < 500


# --- Transport failures ---


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom\nsecond line"),
        httpx.ConnectTimeout(""),
        httpx.ReadError("x"),
        httpx.RemoteProtocolError("peer closed connection\nwithout response"),
        httpx.WriteError("x"),
        httpx.PoolTimeout(""),
        httpx.ProxyError("x"),
        httpx.UnsupportedProtocol("x"),
        httpx.TooManyRedirects("x"),
    ],
)
async def test_every_httpx_error_becomes_a_one_line_wikiquote_error(exc):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with _client(handler) as http:
        with pytest.raises(WikiquoteError) as excinfo:
            await fetch_page(http, "T")
    message = str(excinfo.value)
    _assert_one_readable_line(message)
    assert type(exc).__name__ in message
    assert "second line" not in message


async def test_a_connection_that_dies_mid_body_is_a_wikiquote_error():
    async def dies():
        yield b'{"parse": '
        raise httpx.ReadError("connection reset")

    message = await _message(httpx.Response(200, content=dies(), headers=JSON))
    assert "ReadError" in message


async def test_cancellation_is_not_swallowed_as_a_wikiquote_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, content=_envelope(), headers=JSON)

    async with _client(handler) as http:
        task = asyncio.create_task(fetch_page(http, "T"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.xfail(
    strict=True,
    reason=(
        "a title httpx can't encode (a lone surrogate) raises UnicodeEncodeError out of "
        "fetch_page instead of a WikiquoteError; only reachable from a hand-escaped config value"
    ),
)
async def test_an_unencodable_title_is_a_wikiquote_error():
    async with _client(lambda r: httpx.Response(200, content=_envelope(), headers=JSON)) as http:
        with pytest.raises(WikiquoteError):
            await fetch_page(http, "half an emoji \ud83d")


# --- The request itself ---

EXPECTED_KEYS = {
    "action",
    "format",
    "formatversion",
    "redirects",
    "prop",
    "disableeditsection",
    "disablelimitreport",
    "disabletoc",
    "page",
}


async def _request_for(title: str) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_envelope(), headers=JSON)

    async with _client(handler) as http:
        await fetch_page(http, title)
    (request,) = seen
    return request


@pytest.mark.parametrize(
    "title",
    [
        "A&B",
        "A#B",
        "A=B&action=edit&format=xml",
        "x&redirects=0&page=Other",
        "a+b",
        "a%26b",
        "50% off",
        "Café",
        "\U0001f600",
        "line\nbreak",
        "tab\there",
        "a|b|c",
        "x#fragment&page=y",
        "",
    ],
)
async def test_the_title_is_one_param_and_cannot_inject_others(title):
    request = await _request_for(title)
    query = parse_qs(request.url.query.decode(), keep_blank_values=True)
    assert set(query) == EXPECTED_KEYS
    assert all(len(v) == 1 for v in query.values())
    assert query["page"] == [title]
    assert query["action"] == ["parse"]
    assert query["format"] == ["json"]
    assert query["formatversion"] == ["2"]
    assert query["redirects"] == ["1"]
    assert request.url.fragment == ""


async def test_the_request_carries_no_body_and_only_the_documented_path():
    request = await _request_for("T")
    assert request.method == "GET"
    assert request.content == b""
    assert request.url.host == "en.wikiquote.org"
    assert request.url.path == "/w/api.php"
    assert request.url.scheme == "https"


async def test_the_user_agent_comes_from_the_client_using_the_useragent_constants():
    headers = useragent.user_agent_headers({"NEWSBOT_CONTACT": "https://example.invalid/me"})
    ((name, value),) = headers.items()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_envelope(), headers=JSON)

    async with _client(handler, headers=headers) as http:
        await fetch_page(http, "T")
    assert seen[0].headers[name] == value
    assert value.startswith("discord-newsbot/")


async def test_fetch_page_adds_no_user_agent_of_its_own():
    request = await _request_for("T")
    ((name, value),) = useragent.user_agent_headers({}).items()
    assert value not in request.headers.get(name, "")
    assert "discord-newsbot" not in request.headers.get(name, "")
