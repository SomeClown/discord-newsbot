"""The lounge's text-file fetcher looks at every hop before it asks, and at the connection after.

Same idea as `test_fetch_redirects.py` for the feeds: the transport writes down every URL it is
asked for, because "refused" isn't the claim, "the private address was never asked" is.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime

import httpx
import pytest

from newsbot import netguard
from newsbot.config import QuoteSourceCfg
from newsbot.lounge.sources import load_source

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
START = "https://example.test/quotes.txt"
FORTUNE = "The teapot is not a suspect.\n%\nMabel Quince knew where the biscuits were.\n"
TEXT_HEADERS = {"content-type": "text/plain; charset=utf-8"}


def _text(**kwargs) -> httpx.Response:
    return httpx.Response(200, content=FORTUNE.encode(), headers=TEXT_HEADERS, **kwargs)


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": location})


async def _load(tmp_path, routes, url: str = START):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) not in routes:
            raise AssertionError(f"unexpected request to {request.url}")
        return routes[str(request.url)]

    src = QuoteSourceCfg(kind="url", value=url)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await load_source(src, http=http, cache_dir=tmp_path / "cache", now=NOW)
    return result, seen


@pytest.mark.parametrize(
    "target",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://192.168.1.1/x",
        "https://100.64.0.1/x",
        "https://[::1]/x",
    ],
)
async def test_a_redirect_to_a_private_literal_is_never_requested(tmp_path, target):
    result, seen = await _load(tmp_path, {START: _redirect(target)})
    assert result.quotes == []
    assert "was refused on a redirect: refusing a non-public host" in result.problem
    assert seen == [START]


async def test_a_redirect_to_a_hostname_that_resolves_privately_is_never_requested(
    tmp_path, monkeypatch
):
    async def resolver(self, host, port, **kwargs):
        address = "10.13.14.15" if host == "internal.attacker.example" else "93.184.215.14"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    result, seen = await _load(tmp_path, {START: _redirect("https://internal.attacker.example/x")})
    assert "non-public host: internal.attacker.example" in result.problem
    assert seen == [START]


@pytest.mark.parametrize("url", ["https://169.254.169.254/quotes.txt", "https://[::1]/q.txt"])
async def test_a_configured_url_pointing_private_is_refused_before_any_request(tmp_path, url):
    result, seen = await _load(tmp_path, {}, url)
    assert result.quotes == []
    assert "was refused: refusing a non-public host" in result.problem
    assert seen == []


async def test_a_configured_url_resolving_privately_is_refused_before_any_request(
    tmp_path, monkeypatch
):
    async def resolver(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    result, seen = await _load(tmp_path, {})
    assert "non-public host: example.test" in result.problem
    assert seen == []


async def test_a_dns_stall_refuses_the_source(tmp_path, monkeypatch):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(netguard, "DNS_TIMEOUT_S", 0.05)
    result, seen = await _load(tmp_path, {})
    assert "DNS lookup timed out" in result.problem
    assert seen == []


async def test_a_plain_dns_failure_passes_through_to_the_request(tmp_path, monkeypatch):
    async def broken(self, host, port, **kwargs):
        raise OSError("no such host")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", broken)
    result, seen = await _load(tmp_path, {START: _text()})
    assert len(result.quotes) == 2
    assert seen == [START]


async def test_the_refusal_does_not_echo_the_url_query(tmp_path):
    # `shown_url` keeps the path on purpose (it's how an admin finds the file) and drops the
    # query, where a token would ride.
    url = "https://example.test/gist/s3cr3t-token/quotes.txt?key=hunter2"
    result, _ = await _load(tmp_path, {url: _redirect("https://10.0.0.5/x")}, url)
    assert "hunter2" not in result.problem


class _FakeStream:
    def __init__(self, server_addr) -> None:
        self._server_addr = server_addr

    def get_extra_info(self, info: str):
        return self._server_addr if info == "server_addr" else None


class _WatchedBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = False

    async def __aiter__(self):
        self.read = True
        yield FORTUNE.encode()


async def test_a_connection_to_a_non_public_address_is_refused_before_the_body_is_read(tmp_path):
    body = _WatchedBody()
    response = httpx.Response(
        200,
        headers=TEXT_HEADERS,
        stream=body,
        extensions={"network_stream": _FakeStream(("10.0.0.9", 443))},
    )
    result, _ = await _load(tmp_path, {START: response})
    assert result.quotes == []
    assert "connected to a non-public address: example.test (10.0.0.9)" in result.problem
    assert not body.read


async def test_a_redirect_answered_from_a_non_public_address_is_refused(tmp_path):
    response = httpx.Response(
        302,
        headers={"Location": "https://other.test/q.txt"},
        extensions={"network_stream": _FakeStream(("127.0.0.1", 443))},
    )
    result, seen = await _load(tmp_path, {START: response})
    assert "connected to a non-public address" in result.problem
    assert seen == [START]


async def test_a_public_connection_is_fine(tmp_path):
    response = _text(extensions={"network_stream": _FakeStream(("93.184.215.14", 443))})
    result, _ = await _load(tmp_path, {START: response})
    assert len(result.quotes) == 2


@pytest.mark.parametrize("server_addr", [None, "/var/run/proxy.sock", ()])
async def test_a_missing_peer_address_skips_the_check(tmp_path, server_addr):
    response = _text(extensions={"network_stream": _FakeStream(server_addr)})
    result, _ = await _load(tmp_path, {START: response})
    assert len(result.quotes) == 2


async def test_no_network_stream_at_all_skips_the_check(tmp_path):
    result, _ = await _load(tmp_path, {START: _text()})
    assert len(result.quotes) == 2


@pytest.mark.parametrize(
    "location", ["https:evil.example", "data:,hi", "javascript:alert(1)", "https://xn--/f"]
)
async def test_an_unusable_location_is_a_clean_source_error_naming_the_source(tmp_path, location):
    result, seen = await _load(tmp_path, {START: _redirect(location)})
    assert result.quotes == []
    assert result.problem == f"{START} redirected to an address that isn't a valid URL"
    assert seen == [START]


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "64:ff9b:1::1"])
async def test_a_nat64_peer_embedding_a_private_address_is_refused(tmp_path, address):
    response = _text(extensions={"network_stream": _FakeStream((address, 443))})
    result, _ = await _load(tmp_path, {START: response})
    assert result.quotes == []
    assert "connected to a non-public address" in result.problem
