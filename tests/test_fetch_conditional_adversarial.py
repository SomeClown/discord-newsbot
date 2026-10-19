"""An adversarial round on conditional fetching (design.md §16, plan Task 1, §3.7).

Everything here goes through `httpx.MockTransport`; the suite's network guard fails any
test that opens a real socket, and the last test in this file proves that guard would
catch this collector in particular (a collector that swallowed the error would otherwise
look like an ordinary failed source).
"""

import httpx
import pytest

from newsbot.collectors.rss import _MAX_RESPONSE_BYTES, _RETRY_BACKOFFS_S, _fetch
from newsbot.collectors.shift_table import PageValidators, ShiftTableCollector, Validators
from newsbot.config import ShiftTableSource

URL = "https://codes.example/borderlands-4-shift-codes/"
OTHER_URL = "https://other.example/shift-codes/"
C1 = "AB12C-DE34F-GH56J-KL78M-NP90Q"
C2 = "QR12S-TU34V-WX56Y-ZA78B-CD90E"


def _page(*codes: str) -> bytes:
    rows = "".join(f"<tr><td>3 Golden Keys</td><td>{c}</td></tr>" for c in codes)
    return f"<h2>Codes</h2><table><tr><th>Reward</th><th>Code</th></tr>{rows}</table>".encode()


PAGE = _page(C1)
BAD_PAGE = b"<html><body><p>Nothing to see here.</p></body></html>"


async def _noop_sleep(_seconds: float) -> None:
    return None


def _collector(validators: PageValidators | None = None, *, url: str = URL, sleep=_noop_sleep):
    source = ShiftTableSource(type="shift_table", name="mentalmars", url=url, trust="community")
    return ShiftTableCollector(source, validators=validators, sleep=sleep)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- 304 ---


async def test_a_429_then_a_304_is_a_healthy_pass_and_the_retry_stays_conditional():
    validators = PageValidators({URL: Validators('"v1"', "Wed, 07 Oct 2026 12:00:00 GMT")})
    seen = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(429 if len(seen) == 1 else 304)

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with _client(handler) as http:
        items = await _collect(validators, http, sleep=recording_sleep)
    assert items == []
    assert len(seen) == 2
    for headers in seen:  # the second ask must carry the validators too
        assert headers["if-none-match"] == '"v1"'
        assert headers["if-modified-since"] == "Wed, 07 Oct 2026 12:00:00 GMT"
    assert sleeps == [_RETRY_BACKOFFS_S[0]]
    assert validators == {URL: Validators('"v1"', "Wed, 07 Oct 2026 12:00:00 GMT")}


async def _collect(validators, http, *, url: str = URL, sleep=_noop_sleep):
    return await _collector(validators, url=url, sleep=sleep).collect(http)


async def test_a_304_nobody_asked_for_is_an_error_and_stages_nothing():
    """QA batch 1, fix 10: "not modified" answers a question; this request asked none."""
    validators = PageValidators()
    async with _client(lambda request: httpx.Response(304)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _collect(validators, http)
    assert validators == {}
    assert validators.pending == {}


async def test_a_304_that_arrives_with_a_body_ignores_the_body():
    validators = PageValidators({URL: Validators('"v1"', None)})
    async with _client(lambda request: httpx.Response(304, content=PAGE)) as http:
        assert await _collect(validators, http) == []


async def test_a_304_after_a_redirect_to_a_public_host_is_healthy():
    validators = PageValidators({URL: Validators('"v1"', None)})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "codes.example":
            return httpx.Response(301, headers={"Location": "https://mirror.example/codes/"})
        assert request.headers["if-none-match"] == '"v1"'  # the validators follow the redirect
        return httpx.Response(304)

    async with _client(handler) as http:
        assert await _collect(validators, http) == []
    assert validators == {URL: Validators('"v1"', None)}


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/codes",
        "http://192.168.1.1/",
        "http://172.16.0.9/",
        "http://100.64.0.1/",  # CGNAT
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
    ],
)
async def test_a_redirect_to_a_non_public_literal_is_refused_even_when_it_would_say_304(location):
    validators = PageValidators({URL: Validators('"v1"', None)})
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        if request.url.host == "codes.example":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(304)

    async with _client(handler) as http:
        with pytest.raises(ValueError, match="non-public"):
            await _collect(validators, http)
    assert validators == {URL: Validators('"v1"', None)}


async def test_a_redirect_to_a_public_ip_literal_is_not_over_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "codes.example":
            return httpx.Response(302, headers={"Location": "http://93.184.215.14/codes"})
        return httpx.Response(200, content=PAGE)

    async with _client(handler) as http:
        items = await _collect(PageValidators(), http)
    assert [i.title for i in items] == [C1]


# --- validators ---


async def test_the_validators_are_stored_under_the_configured_url_not_the_redirect_target():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "codes.example":
            return httpx.Response(301, headers={"Location": "https://mirror.example/codes/"})
        return httpx.Response(200, content=PAGE, headers={"ETag": '"m1"'})

    validators = PageValidators()
    async with _client(handler) as http:
        await _collect(validators, http)
    validators.commit_all()
    assert validators == {URL: Validators('"m1"', None)}


async def test_each_url_keeps_its_own_validators_and_never_sends_anothers():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("if-none-match")))
        etag = '"a"' if request.url.host == "codes.example" else '"b"'
        return httpx.Response(200, content=PAGE, headers={"ETag": etag})

    validators = PageValidators()
    async with _client(handler) as http:
        await _collect(validators, http, url=URL)
        await _collect(validators, http, url=OTHER_URL)
        validators.commit_all()
        await _collect(validators, http, url=URL)
        await _collect(validators, http, url=OTHER_URL)
        validators.commit_all()
    assert seen == [
        ("codes.example", None),
        ("other.example", None),
        ("codes.example", '"a"'),
        ("other.example", '"b"'),
    ]
    assert validators == {URL: Validators('"a"', None), OTHER_URL: Validators('"b"', None)}


async def test_one_urls_failed_parse_leaves_the_other_urls_validators_alone():
    good = Validators('"a"', None)
    validators = PageValidators({URL: good})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BAD_PAGE, headers={"ETag": '"bad"'})

    async with _client(handler) as http:
        with pytest.raises(ValueError, match="no SHiFT code tables found"):
            await _collect(validators, http, url=OTHER_URL)
    assert validators == {URL: good}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(503, headers={"Retry-After": "120"}),
        httpx.Response(404),
        httpx.Response(403),
        httpx.Response(200, content=b""),
        httpx.Response(200, content=BAD_PAGE),
        httpx.Response(200, content=BAD_PAGE, headers={"ETag": '"new"'}),
    ],
)
async def test_any_failed_pass_keeps_the_last_good_validators(response):
    good = Validators('"good"', "Wed, 07 Oct 2026 12:00:00 GMT")
    validators = PageValidators({URL: good})
    async with _client(lambda request: response) as http:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await _collect(validators, http)
    assert validators == {URL: good}


async def test_a_transport_error_keeps_the_last_good_validators():
    good = Validators('"good"', None)
    validators = PageValidators({URL: good})

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as http:
        with pytest.raises(httpx.ConnectError):
            await _collect(validators, http)
    assert validators == {URL: good}


async def test_a_200_with_no_rows_says_why_in_the_error_text():
    async with _client(lambda request: httpx.Response(200, content=BAD_PAGE)) as http:
        with pytest.raises(ValueError) as excinfo:
            await _collect(PageValidators(), http)
    assert str(excinfo.value) == "no SHiFT code tables found"


async def test_a_200_after_a_304_replaces_the_validators():
    validators = PageValidators({URL: Validators('"v1"', None)})
    responses = [httpx.Response(304), httpx.Response(200, content=PAGE, headers={"ETag": '"v2"'})]
    async with _client(lambda request: responses.pop(0)) as http:
        assert await _collect(validators, http) == []
        assert [i.title for i in await _collect(validators, http)] == [C1]
    validators.commit_all()
    assert validators == {URL: Validators('"v2"', None)}


@pytest.mark.parametrize(
    "etag", ['W/"weak-1"', '"strong"', "unquoted-etag", '"a,b"', '"with space"']
)
async def test_an_etag_is_sent_back_exactly_as_it_was_received(etag):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, content=PAGE, headers={"ETag": etag})

    validators = PageValidators()
    async with _client(handler) as http:
        await _collect(validators, http)
        validators.commit_all()
        await _collect(validators, http)
    assert seen == [None, etag]


async def test_an_empty_etag_header_is_not_replayed_as_an_empty_condition():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, content=PAGE, headers={"ETag": ""})

    async with _client(handler) as http:
        validators = PageValidators()
        await _collect(validators, http)
        validators.commit_all()
        await _collect(validators, http)
    assert "if-none-match" not in seen[1]


@pytest.mark.parametrize(
    "header_bytes",
    [
        pytest.param((b"etag", b'"caf\xc3\xa9"'), id="etag-utf8"),
        pytest.param((b"etag", b'"caf\xe9"'), id="etag-latin1"),
        pytest.param((b"last-modified", b"Wed, 07 Oct 2026 \xe9 GMT"), id="last-modified-latin1"),
    ],
)
async def test_a_validator_with_non_ascii_bytes_does_not_wedge_the_source(header_bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PAGE, headers=[header_bytes])

    validators = PageValidators()
    async with _client(handler) as http:
        await _collect(validators, http)
        validators.commit_all()
        second = await _collect(validators, http)  # used to raise UnicodeEncodeError
    assert [i.title for i in second] == [C1]


async def test_a_very_long_etag_round_trips():
    etag = '"' + "x" * 4000 + '"'
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, content=PAGE, headers={"ETag": etag})

    validators = PageValidators()
    async with _client(handler) as http:
        await _collect(validators, http)
        validators.commit_all()
        await _collect(validators, http)
    assert seen == [None, etag]


# --- the 5 MB cap ---


class _Chunks(httpx.AsyncByteStream):
    """A body that hands out `total` bytes in `chunk` sized pieces and counts what was asked for."""

    def __init__(self, total: int, chunk: int = 64 * 1024) -> None:
        self.total = total
        self.chunk = chunk
        self.sent = 0

    async def __aiter__(self):
        while self.sent < self.total:
            size = min(self.chunk, self.total - self.sent)
            self.sent += size
            yield b"a" * size


@pytest.mark.parametrize("extra", [-1, 0])
async def test_a_body_at_or_just_under_the_cap_arrives_whole(extra):
    size = _MAX_RESPONSE_BYTES + extra
    async with _client(lambda request: httpx.Response(200, content=b"a" * size)) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep)
    assert len(fetched.body) == size


@pytest.mark.parametrize("extra", [1, 4097, 3 * 1024 * 1024])
async def test_a_body_over_the_cap_is_cut_to_exactly_the_cap(extra):
    size = _MAX_RESPONSE_BYTES + extra
    async with _client(lambda request: httpx.Response(200, content=b"a" * size)) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep)
    assert len(fetched.body) == _MAX_RESPONSE_BYTES


async def test_a_streamed_body_is_not_read_to_the_end_once_the_cap_is_hit():
    stream = _Chunks(total=200 * 1024 * 1024)  # 200 MB on offer
    async with _client(lambda request: httpx.Response(200, stream=stream)) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep)
    assert len(fetched.body) == _MAX_RESPONSE_BYTES
    assert stream.sent < _MAX_RESPONSE_BYTES + 2 * stream.chunk  # stopped within a chunk or two


async def test_a_page_cut_by_the_cap_mid_code_keeps_the_rows_before_the_cut_and_no_half_code():
    head = (
        b"<table><tr><th>Reward</th><th>Code</th></tr>"
        + f"<tr><td>3 Golden Keys</td><td>{C1}</td></tr>".encode()
    )
    tail = f"<tr><td>1 Golden Key</td><td>{C2}</td></tr></table>".encode()
    start_of_c2 = tail.index(C2.encode())
    # Pad with an HTML comment so that the cap lands 14 characters into the second code.
    pad = _MAX_RESPONSE_BYTES - len(head) - start_of_c2 - 14
    padding = b"<!--" + b"a" * (pad - 7) + b"-->"
    body = head + padding + tail
    assert body.index(C2.encode()) + 14 == _MAX_RESPONSE_BYTES
    async with _client(lambda request: httpx.Response(200, content=body)) as http:
        items = await _collect(PageValidators(), http)
    assert [i.title for i in items] == [C1]


# --- sleeping and retrying ---


async def test_three_429s_then_a_page_uses_every_backoff_and_then_succeeds():
    calls = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return (
            httpx.Response(429)
            if len(calls) <= len(_RETRY_BACKOFFS_S)
            else httpx.Response(200, content=PAGE)
        )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with _client(handler) as http:
        items = await _collect(PageValidators(), http, sleep=sleep)
    assert len(items) == 1
    assert sleeps == list(_RETRY_BACKOFFS_S)


async def test_a_429_with_validators_that_never_clears_keeps_them():
    validators = PageValidators({URL: Validators('"v1"', None)})
    async with _client(lambda request: httpx.Response(429)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _collect(validators, http)
    assert validators == {URL: Validators('"v1"', None)}


# --- the network guard ---


async def test_the_network_guard_catches_this_collector_if_it_ever_reaches_for_a_socket(
    blocked_attempts,
):
    # A client with no mock transport is exactly what a test must never do by accident. The
    # guard has to turn it into an error *and* a record, because collectors get wrapped in
    # `except Exception` upstream and a swallowed error would otherwise look like a bad day
    # at the source.
    async with httpx.AsyncClient() as http:
        with pytest.raises(Exception):  # noqa: B017 (the type is the guard's business)
            await _collect(PageValidators(), http)
    attempts = blocked_attempts.acknowledge()
    assert attempts, "the guard recorded no attempt; a real request could have gone out"
