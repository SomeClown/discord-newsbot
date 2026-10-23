"""An adversarial round on the SSRF hardening: `netguard.py`, `_fetch`'s hand-walked redirects,
and the lounge's `_get_text`.

The implementer's tests (`test_fetch_redirects.py`, `test_lounge_sources_netguard.py`) cover the
obvious private literals, the hop limit and the peer check with a fake stream. This file goes after
the places a guard like this usually leaks:

- host spellings (IPv4-mapped IPv6, 6to4, zone ids, decimal/octal/hex IPv4, trailing dots,
  userinfo, protocol-relative and backslash Locations, IDNA, percent-encoding), and whether the
  host the check judged is the host httpx hands to the connection (a recording network backend
  stands in for the socket, so it is httpx's own URL handling being tested, not mine);
- `getaddrinfo` answers that aren't tidy (empty, mixed, scoped);
- resource handling: redirect bodies never read, every response closed, errors raised mid-body
  arriving unchanged;
- the real httpcore stack, over a loopback server, with a resolver that answers one thing to the
  check and another to the connection (DNS rebinding), because the peer check's unit tests use
  a fake stream and a fake stream can't tell whether httpcore really reports `server_addr`;
- what ends up in an admin-facing line.

Nothing here leaves the machine: names resolve through the suite's guard (or through the real
resolver, numeric spellings only, which never touches DNS), and the only sockets are loopback.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import socket
import ssl
import subprocess
from datetime import timedelta

import httpcore
import httpx
import pytest
from test_fetch_redirects import START, Site, _noop_sleep, _ok, _redirect, _WatchedBody
from test_lounge_sources_netguard import FORTUNE, TEXT_HEADERS
from test_lounge_sources_netguard import START as LOUNGE_START

from newsbot import netguard
from newsbot.bot.format import ReportSources, render_guild_run_report
from newsbot.collectors.base import _run_one
from newsbot.collectors.rss import _MAX_REDIRECTS, _RETRY_BACKOFFS_S, RssCollector, _fetch
from newsbot.config import GameCfg, QuoteSourceCfg, RssSource
from newsbot.lounge.sources import load_source
from newsbot.netguard import AddressRefused, reject_private_host, reject_private_peer

# Captured at import, before the suite's guard patches it for each test: the one real call the
# numeric-spelling tests make, always with AI_NUMERICHOST, so it parses the text and nothing else.
_REAL_GETADDRINFO = socket.getaddrinfo
PUBLIC = "93.184.215.14"
_NUMERIC_NAME = re.compile(r"^[0-9a-fA-Fx.]+$")


def _answer(address: str, port: int = 0, family: int = socket.AF_INET):
    sockaddr = (address, port) if family == socket.AF_INET else (address, port, 0, 0)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


class Lookups:
    """Every name the host check asked the resolver about, in order."""

    def __init__(self) -> None:
        self.names: list[str] = []


@pytest.fixture
def lookups(monkeypatch) -> Lookups:
    """The host check's resolver, wrapped: names are public, except that an all-digits-and-dots
    (or 0x...) name gets the operating system's own reading of it, which is how
    `getaddrinfo("2130706433")` turns into 127.0.0.1. Records what it was asked."""
    seen = Lookups()

    async def resolver(self, host, port, **kwargs):
        seen.names.append(host)
        if _NUMERIC_NAME.match(host):
            try:
                infos = _REAL_GETADDRINFO(
                    host, None, flags=socket.AI_NUMERICHOST, type=socket.SOCK_STREAM
                )
            except OSError:
                raise
            return infos
        return [_answer(PUBLIC)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    return seen


def _system_reads_as_loopback(spelling: str) -> bool:
    try:
        infos = _REAL_GETADDRINFO(
            spelling, None, flags=socket.AI_NUMERICHOST, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    return all(info[4][0] == "127.0.0.1" for info in infos)


def _loc(location: str, status: int = 302) -> httpx.Response:
    """A redirect whose Location header is raw UTF-8 bytes, as a server can send it."""
    return httpx.Response(status, headers=[(b"location", location.encode("utf-8"))])


def _as_getaddrinfo_reads(name: str) -> str:
    """The name `socket.getaddrinfo` actually looks up for a `str` host: it encodes with
    Python's built-in `idna` codec (IDNA 2003), which is not the encoding httpx connects with."""
    return name.encode("idna").decode("ascii")


def _url(host: str) -> httpx.URL:
    return httpx.URL(f"https://{host}/feed.xml")


# --- literal spellings the check must refuse ---


@pytest.mark.parametrize(
    "host",
    [
        "[::ffff:127.0.0.1]",
        "[::ffff:7f00:1]",
        "[0:0:0:0:0:ffff:7f00:1]",
        "[::ffff:169.254.169.254]",
        "[::ffff:a9fe:a9fe]",
        "[::ffff:10.0.0.1]",
        "[::ffff:100.64.0.1]",
        "[2002:a9fe:a9fe::]",
        "[2002:7f00:1::]",
        "[fe80::1%25eth0]",
        "[fe80::1%eth0]",
        "[fe80::1]",
        "[fd00::1]",
        "[::]",
        "[::1]",
        "0.0.0.0",  # noqa: S104
        "255.255.255.255",
        "169.254.169.254",
    ],
)
async def test_non_public_literals_are_refused_in_every_spelling(host):
    with pytest.raises(AddressRefused, match="non-public host"):
        await reject_private_host(_url(host))


@pytest.mark.parametrize("host", ["[::ffff:93.184.215.14]", "[2606:4700::1111]", "93.184.215.14"])
async def test_public_literals_pass_including_a_public_ipv4_mapped_one(host):
    await reject_private_host(_url(host))


@pytest.mark.parametrize(
    "spelling",
    ["2130706433", "0x7f.1", "017700000001", "127.1", "0x7f000001", "127.0.1", "0x7f.0x0.0x0.0x1"],
)
async def test_numeric_spellings_of_loopback_are_refused_once_the_system_reads_them(
    spelling, lookups
):
    """`ipaddress` doesn't read these, so they fall through to the resolver, which does. The
    refusal rests on that, so this test uses the operating system's own reading of the text."""
    if not _system_reads_as_loopback(spelling):
        pytest.skip(f"this system's resolver doesn't read {spelling!r} as 127.0.0.1")
    with pytest.raises(AddressRefused, match="non-public host"):
        await reject_private_host(_url(spelling))
    assert lookups.names == [spelling]


async def test_zero_as_a_bare_number_is_refused(lookups):
    try:
        infos = _REAL_GETADDRINFO("0", None, flags=socket.AI_NUMERICHOST)
    except OSError:
        pytest.skip("this system's resolver doesn't read '0'")
    if any(
        info[4][0] != "0.0.0.0"  # noqa: S104
        for info in infos
    ):
        pytest.skip("'0' doesn't mean 0.0.0.0 here")
    with pytest.raises(AddressRefused):
        await reject_private_host(_url("0"))


@pytest.mark.parametrize("host", ["localhost.", "LOCALHOST", "LocalHost.", "ip6-localhost"])
async def test_loopback_names_are_judged_by_what_they_resolve_to(host, monkeypatch):
    """Whatever spelling httpx keeps (it lowercases, and keeps a trailing dot), that exact
    string is what the resolver is asked, and loopback answers are refused."""
    asked = []

    async def resolver(self, name, port, **kwargs):
        asked.append(name)
        return [_answer("127.0.0.1"), _answer("::1", family=socket.AF_INET6)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    url = _url(host)
    with pytest.raises(AddressRefused, match="non-public host"):
        await reject_private_host(url)
    assert asked == [url.host]


@pytest.mark.parametrize("value", ["64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1"])
async def test_a_nat64_literal_embedding_a_private_address_is_refused(value):
    with pytest.raises(AddressRefused):
        await reject_private_host(_url(f"[{value}]"))


# --- getaddrinfo answers that aren't tidy ---


async def test_an_empty_answer_neither_crashes_nor_refuses(monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return []

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    await reject_private_host(_url("feeds.example"))


@pytest.mark.parametrize(
    "order",
    [("93.184.215.14", "10.0.0.1"), ("10.0.0.1", "93.184.215.14"), ("93.184.215.14", "127.0.0.1")],
)
async def test_a_private_answer_refuses_whatever_its_position(order, monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return [_answer(address) for address in order]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    with pytest.raises(AddressRefused, match="non-public host: feeds.example"):
        await reject_private_host(_url("feeds.example"))


@pytest.mark.parametrize("scoped", ["fe80::1%lo0", "fe80::1%1", "fe80::abcd%en0", "::1%lo0"])
async def test_a_scoped_ipv6_answer_is_refused_not_a_crash(scoped, monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return [_answer(scoped, family=socket.AF_INET6)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    with pytest.raises(AddressRefused, match="non-public host"):
        await reject_private_host(_url("feeds.example"))


async def test_a_scoped_public_ipv6_answer_passes(monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return [_answer("2606:4700::1111%lo0", family=socket.AF_INET6)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    await reject_private_host(_url("feeds.example"))


@pytest.mark.parametrize(
    "mapped", ["::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"]
)
async def test_an_ipv4_mapped_private_answer_from_the_resolver_is_refused(mapped, monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return [_answer(mapped, family=socket.AF_INET6)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    with pytest.raises(AddressRefused):
        await reject_private_host(_url("feeds.example"))


async def test_a_refusal_names_the_host_and_nothing_from_the_path_or_query():
    url = httpx.URL("https://user:s3cret@10.0.0.5/gist/tok3n/feed.xml?key=hunter2#frag")
    with pytest.raises(AddressRefused) as excinfo:
        await reject_private_host(url)
    message = str(excinfo.value)
    for secret in ("s3cret", "user", "tok3n", "hunter2", "frag", "feed.xml"):
        assert secret not in message


# --- peer check: shapes of server_addr ---


class _Stream:
    def __init__(self, server_addr) -> None:
        self._server_addr = server_addr

    def get_extra_info(self, name: str):
        return self._server_addr if name == "server_addr" else None


def _peer_response(server_addr) -> httpx.Response:
    return httpx.Response(200, extensions={"network_stream": _Stream(server_addr)})


@pytest.mark.parametrize(
    "server_addr",
    [
        ("::1", 443, 0, 0),
        ("::ffff:127.0.0.1", 443, 0, 0),
        ("fe80::1%lo0", 443, 0, 1),
        ("::ffff:169.254.169.254", 443, 0, 0),
        ("0.0.0.0", 443),  # noqa: S104
    ],
)
def test_the_peer_check_reads_ipv6_four_tuples_and_scoped_and_mapped_addresses(server_addr):
    with pytest.raises(AddressRefused, match="connected to a non-public address"):
        reject_private_peer(httpx.URL(START), _peer_response(server_addr))


def test_a_public_ipv6_four_tuple_peer_is_fine():
    reject_private_peer(httpx.URL(START), _peer_response(("2606:4700::1111", 443, 0, 0)))


# --- does the check look at the host that the connection uses? ---


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Stands in for the socket layer: records where httpcore was told to connect, then fails."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host,
        port,
        timeout=None,  # noqa: ASYNC109
        local_address=None,
        socket_options=None,
    ):
        self.connects.append((host, port))
        raise httpcore.ConnectError("recording backend: no connection made")

    async def connect_unix_socket(
        self,
        path,
        timeout=None,  # noqa: ASYNC109
        socket_options=None,
    ):
        raise httpcore.ConnectError("recording backend: no connection made")

    async def sleep(self, seconds: float) -> None:
        return None


def _recording_client() -> tuple[httpx.AsyncClient, _RecordingBackend]:
    backend = _RecordingBackend()
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(network_backend=backend)
    return httpx.AsyncClient(transport=transport), backend


@pytest.mark.parametrize(
    ("url", "connect_host", "port"),
    [
        ("https://FEEDS.example/f", "feeds.example", 443),
        ("https://feeds.example./f", "feeds.example.", 443),
        ("https://localhost./f", "localhost.", 443),
        ("https://bücher.example/f", "xn--bcher-kva.example", 443),
        ("https://user:pw@feeds.example:8443/f", "feeds.example", 8443),
        ("https://%31%32%37.0.0.1/f", "%31%32%37.0.0.1", 443),
        ("https://[2606:4700::1111]/f", "2606:4700::1111", 443),
        ("https://[::ffff:93.184.215.14]/f", "::ffff:93.184.215.14", 443),
    ],
)
async def test_the_connection_is_made_to_the_very_host_the_check_looked_up(
    url, connect_host, port, lookups
):
    http, backend = _recording_client()
    async with http:
        with pytest.raises(httpx.ConnectError):
            await _fetch(http, url, sleep=_noop_sleep)
    assert backend.connects == [(connect_host, port)]
    if lookups.names:
        assert [_as_getaddrinfo_reads(n) for n in lookups.names] == [connect_host]


@pytest.mark.parametrize(
    "url",
    [
        "https://feeds.example@169.254.169.254/f",
        "https://example.com\\@127.0.0.1/f",
        "https://169.254.169.254:80@feeds.example@10.0.0.1/f",
        "https://[::ffff:127.0.0.1]/f",
        "https://[::ffff:a9fe:a9fe]/f",
        "https://[fe80::1%25eth0]/f",
        "https://0.0.0.0/f",
    ],
)
async def test_a_private_configured_url_is_refused_before_the_connection_layer(url):
    http, backend = _recording_client()
    async with http:
        with pytest.raises(AddressRefused):
            await _fetch(http, url, sleep=_noop_sleep)
    assert backend.connects == []


async def test_a_numeric_spelling_of_loopback_never_reaches_the_connection_layer(lookups):
    if not _system_reads_as_loopback("2130706433"):
        pytest.skip("this system's resolver doesn't read 2130706433 as 127.0.0.1")
    http, backend = _recording_client()
    async with http:
        with pytest.raises(AddressRefused):
            await _fetch(http, "https://2130706433/f", sleep=_noop_sleep)
    assert backend.connects == []


@pytest.mark.parametrize("url", ["https:///x", "https://:443/x"])
async def test_a_url_with_no_host_makes_no_connection_even_though_the_check_waves_it_through(url):
    """`reject_private_host` returns quietly for an empty host. It's safe only because httpx
    refuses to send such a request; this pins that the refusal happens before any connect."""
    http, backend = _recording_client()
    async with http:
        with pytest.raises(httpx.UnsupportedProtocol):
            await _fetch(http, url, sleep=_noop_sleep)
    assert backend.connects == []


# --- Location spellings ---


def _hosts(site: Site) -> list[str | None]:
    return [httpx.URL(u).host for u in site.seen]


@pytest.mark.parametrize(
    "location",
    [
        "//169.254.169.254/x",
        "//[::1]/x",
        "//user:pw@10.0.0.5/x",
        "https://public.example@169.254.169.254/x",
        "https://169.254.169.254:8080/x",
        "https://169.254.169.254:443/x",
        "https://[::ffff:169.254.169.254]/x",
        "https://[::ffff:a9fe:a9fe]/x",
        "https://[2002:a9fe:a9fe::]/x",
        "https://[fe80::1%25eth0]/x",
        "https://0.0.0.0/x",
        "HTTPS://169.254.169.254/x",
        "https://example.com\\@127.0.0.1/x",
    ],
)
async def test_hostile_location_spellings_are_refused_without_a_request(location):
    site = Site({START: _redirect(location)})
    async with site.client() as http:
        with pytest.raises(AddressRefused):
            await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START]


@pytest.mark.parametrize(
    "spelling", ["2130706433", "0x7f.1", "017700000001", "127.1", "0x7f000001", "0"]
)
async def test_numeric_loopback_spellings_in_a_location_are_never_requested(spelling, lookups):
    try:
        infos = _REAL_GETADDRINFO(spelling, None, flags=socket.AI_NUMERICHOST)
    except OSError:
        pytest.skip(f"this system's resolver doesn't read {spelling!r}")
    if any(not info[4][0].startswith(("127.", "0.")) for info in infos):
        pytest.skip(f"{spelling!r} isn't loopback on this system")
    site = Site({START: _redirect(f"https://{spelling}/x")})
    async with site.client() as http:
        with pytest.raises(AddressRefused):
            await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START]


@pytest.mark.parametrize(
    "location",
    [
        "/\\169.254.169.254/x",
        "\\\\169.254.169.254\\x",
        "https:\\\\169.254.169.254/x",
        "https:/169.254.169.254/x",
        "https:169.254.169.254",
        "///169.254.169.254/x",
        "  https://169.254.169.254/x",
        "\thttps://169.254.169.254/x",
        "%2f%2f169.254.169.254/x",
        "/%2f169.254.169.254/x",
    ],
)
async def test_locations_httpx_reads_as_paths_stay_on_the_same_host(location):
    """Backslashes, a missing slash, leading whitespace: a browser might read some of these as a
    different host, but the check and the request both go through httpx's URL, and in httpx's
    reading they're paths on the host that sent them. What matters is that nothing leaves
    `feeds.example`, and that either the request is made there or the redirect is an error."""
    site = Site({START: _redirect(location)})
    site.routes = {}

    def handler(request: httpx.Request) -> httpx.Response:
        site.seen.append(str(request.url))
        return _redirect(location) if request.url.path == "/feed.xml" else _ok(b"stayed home")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        try:
            await _fetch(http, START, sleep=_noop_sleep)
        except ValueError, httpx.InvalidURL:
            pass
    assert set(_hosts(site)) == {"feeds.example"}


async def test_a_percent_encoded_host_is_looked_up_as_written_and_requested_as_written(lookups):
    site = Site({START: _redirect("https://%31%32%37.0.0.1/x"), "https://%31%32%37.0.0.1/x": _ok()})
    async with site.client() as http:
        await _fetch(http, START, sleep=_noop_sleep)
    assert lookups.names == ["feeds.example", "%31%32%37.0.0.1"]
    assert _hosts(site)[1] == "%31%32%37.0.0.1"


async def test_an_idna_location_is_looked_up_and_requested_as_its_punycode_form(lookups):
    target = "https://bücher.example/f"
    site = Site({START: _loc(target), "https://xn--bcher-kva.example/f": _ok(b"x")})
    async with site.client() as http:
        await _fetch(http, START, sleep=_noop_sleep)
    assert [_as_getaddrinfo_reads(n) for n in lookups.names] == [
        "feeds.example",
        "xn--bcher-kva.example",
    ]


@pytest.fixture
def sharp_s_world(monkeypatch):
    """A DNS where the two readings of `faß.example` differ: Python's resolver path reads it as
    `fass.example` (IDNA 2003: ß becomes ss), which is public; httpx connects to its IDNA 2008
    form, `xn--fa-hia.example`, which an attacker has pointed at loopback."""
    table = {"fass.example": PUBLIC, "xn--fa-hia.example": "127.0.0.1"}
    seen = Lookups()

    async def resolver(self, host, port, **kwargs):
        host = host.decode("ascii") if isinstance(host, bytes) else host
        seen.names.append(host)
        return [_answer(table.get(_as_getaddrinfo_reads(host), PUBLIC), port or 0)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    return seen


async def test_a_configured_idna_host_is_judged_by_the_name_the_connection_uses(sharp_s_world):
    http, backend = _recording_client()
    async with http:
        with pytest.raises(AddressRefused):
            await _fetch(http, "https://faß.example/f", sleep=_noop_sleep)
    assert backend.connects == []


async def test_a_redirect_to_an_idna_host_is_judged_by_the_name_the_connection_uses(
    sharp_s_world,
):
    site = Site({START: _loc("https://faß.example/f"), "https://xn--fa-hia.example/f": _ok()})
    async with site.client() as http:
        with pytest.raises(AddressRefused):
            await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START]


async def test_a_location_with_non_ascii_bytes_is_followed_as_a_percent_encoded_path():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers=[(b"location", "/café".encode())])
        return _ok()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _fetch(http, START, sleep=_noop_sleep)
    assert seen == [START, "https://feeds.example/caf%C3%A9"]


async def test_a_redirect_to_another_port_of_a_public_host_is_followed():
    site = Site({START: _redirect("https://b.example:8443/f"), "https://b.example:8443/f": _ok()})
    async with site.client() as http:
        await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START, "https://b.example:8443/f"]


async def test_userinfo_does_not_follow_a_redirect_to_another_host():
    seen_auth = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "feeds.example":
            return _redirect("//b.example/f")
        return _ok()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _fetch(http, "https://user:pw@feeds.example/f", sleep=_noop_sleep)
    assert seen_auth[0][1] is not None
    assert seen_auth[1] == ("b.example", None)


@pytest.mark.parametrize(
    "location",
    [
        "https://host:abc/f",
        "https://[::1/f",
        "https://[v1.fe80::a+en1]/f",
        "https://0177.0.0.1/f",
        "https://256.1.1.1/f",
        "https://ⅼlocalhost/f",
        "https://１２７.０.０.１/f",
        "https://127.0.0.1\x00/f",
        "x" * 70_000,
        "ftp://files.example/f",
    ],
)
async def test_a_junk_location_is_a_clean_error_and_never_a_request(location):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers=[(b"location", location.encode("utf-8"))])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError) as excinfo:
            await _fetch(http, START, sleep=_noop_sleep)
    assert seen == [START]
    assert "xxxxxxxxxx" not in str(excinfo.value)


@pytest.mark.parametrize(
    "location",
    ["https:evil.example", "https:169.254.169.254:80", "data:,hi", "javascript:alert(1)"],
)
async def test_every_unusable_location_is_a_value_error_naming_the_host(location):
    site = Site({START: _redirect(location)})
    async with site.client() as http:
        with pytest.raises(ValueError, match="feeds.example redirected to an address"):
            await _fetch(http, START, sleep=_noop_sleep)


@pytest.mark.parametrize("location", ["https://xn--/f", "https://xn--a/f"])
async def test_a_malformed_idna_location_names_the_host_that_sent_it(location):
    site = Site({START: _redirect(location)})
    async with site.client() as http:
        with pytest.raises(ValueError, match="feeds.example"):
            await _fetch(http, START, sleep=_noop_sleep)


async def test_a_long_but_valid_location_is_followed():
    path = "/" + "a" * 60_000
    site = Site({START: _redirect(path), f"https://feeds.example{path}": _ok(b"long")})
    async with site.client() as http:
        assert (await _fetch(http, START, sleep=_noop_sleep)).body == b"long"


async def test_a_client_built_to_follow_redirects_does_not_follow_them_behind_fetchs_back():
    """`_fetch` is handed a shared client; if somebody builds it with `follow_redirects=True`,
    the private hop must still be seen by the check rather than followed by httpx."""
    site = Site({START: _redirect("https://169.254.169.254/latest/")})
    transport = httpx.MockTransport(site.handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as http:
        with pytest.raises(AddressRefused):
            await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START]


# --- loops, hop counts, retries ---


async def test_a_two_host_redirect_loop_stops_after_the_limit():
    a, b = START, "https://b.example/feed.xml"
    site = Site({a: _redirect(b), b: _redirect(a)})
    async with site.client() as http:
        with pytest.raises(ValueError, match=f"redirected more than {_MAX_REDIRECTS} times"):
            await _fetch(http, a, sleep=_noop_sleep)
    assert len(site.seen) == _MAX_REDIRECTS + 1


async def test_a_self_redirect_stops_after_the_limit():
    site = Site({START: _redirect(START)})
    async with site.client() as http:
        with pytest.raises(ValueError, match="redirected more than 5 times"):
            await _fetch(http, START, sleep=_noop_sleep)
    assert len(site.seen) == _MAX_REDIRECTS + 1


def _chain(hops: int) -> list[str]:
    return [START, *(f"https://hop{i}.example/feed" for i in range(1, hops + 1))]


async def test_six_hops_on_the_retry_attempt_are_refused_even_though_five_would_pass():
    """Attempt one is a 429 at the start; attempt two walks six redirects from scratch. The
    limit is per walk, so attempt two gets its full five and no more."""
    urls = _chain(_MAX_REDIRECTS + 1)
    routes: dict[str, httpx.Response | list[httpx.Response]] = {
        START: [httpx.Response(429), _redirect(urls[1])]
    }
    for i in range(1, len(urls) - 1):
        routes[urls[i]] = _redirect(urls[i + 1])
    routes[urls[-1]] = _ok()
    site = Site(routes)
    sleeps = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with site.client() as http:
        with pytest.raises(ValueError, match="redirected more than 5 times"):
            await _fetch(http, START, sleep=sleep)
    assert sleeps == [_RETRY_BACKOFFS_S[0]]
    assert len(site.seen) == 1 + (_MAX_REDIRECTS + 1)


async def test_a_429_at_the_end_of_a_redirect_chain_on_the_last_attempt_is_an_http_error():
    routes: dict[str, httpx.Response | list[httpx.Response]] = {
        START: [_redirect("https://b.example/f") for _ in range(1 + len(_RETRY_BACKOFFS_S))],
        "https://b.example/f": [httpx.Response(429) for _ in range(1 + len(_RETRY_BACKOFFS_S))],
    }
    site = Site(routes)
    async with site.client() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch(http, START, sleep=_noop_sleep)
    assert len(site.seen) == 2 * (1 + len(_RETRY_BACKOFFS_S))


async def test_a_private_redirect_on_the_retry_attempt_is_refused_too():
    """The retry walks again from the configured URL, and the check runs on that walk too: a
    server that redirects politely once and hostilely the second time gets nowhere."""
    site = Site(
        {
            START: [httpx.Response(429), _redirect("https://169.254.169.254/latest/")],
        }
    )
    async with site.client() as http:
        with pytest.raises(AddressRefused):
            await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START, START]


# --- the 304 path ---


async def test_a_304_that_carries_a_location_is_an_answer_not_a_redirect():
    site = Site({START: httpx.Response(304, headers={"Location": "https://169.254.169.254/x"})})
    async with site.client() as http:
        fetched = await _fetch(http, START, headers={"If-None-Match": '"v1"'}, sleep=_noop_sleep)
    assert fetched.status == 304
    assert site.seen == [START]


async def test_an_unasked_for_304_is_an_error_and_follows_nothing():
    site = Site({START: httpx.Response(304, headers={"Location": "https://169.254.169.254/x"})})
    async with site.client() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch(http, START, sleep=_noop_sleep)
    assert site.seen == [START]


async def test_a_private_hop_on_the_conditional_path_is_refused_like_any_other():
    site = Site({START: _redirect("https://10.0.0.5/x")})
    async with site.client() as http:
        with pytest.raises(AddressRefused):
            await _fetch(http, START, headers={"If-None-Match": '"v1"'}, sleep=_noop_sleep)
    assert site.seen == [START]


# --- bodies and connections ---


def _watched(status: int, location: str | None = None) -> tuple[httpx.Response, _WatchedBody]:
    body = _WatchedBody(b"x" * 100)
    headers = {"Location": location} if location else None
    return httpx.Response(status, headers=headers, stream=body), body


async def test_redirect_bodies_are_never_read_and_every_response_is_closed():
    redirect, redirect_body = _watched(302, "https://b.example/f")
    final, final_body = _watched(200)
    site = Site({START: redirect, "https://b.example/f": final})
    async with site.client() as http:
        await _fetch(http, START, sleep=_noop_sleep)
    assert not redirect_body.read
    assert redirect_body.closed
    assert final_body.read and final_body.closed


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (302, "https://10.0.0.5/x"),  # refused host
        (302, "http://plain.example/x"),  # downgrade
        (302, "ftp://files.example/x"),  # scheme
        (302, "https://host:abc/x"),  # httpx's own parse error
        (302, None),  # no Location
        (404, None),  # raise_for_status
        (500, None),
    ],
)
async def test_a_hop_that_ends_in_an_error_is_closed_and_its_body_unread(status, location):
    response, body = _watched(status, location)
    site = Site({START: response})
    async with site.client() as http:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await _fetch(http, START, sleep=_noop_sleep)
    assert body.closed
    assert not body.read


async def test_the_sixth_redirect_response_is_closed_and_unread():
    routes: dict[str, httpx.Response | list[httpx.Response]] = {}
    urls = _chain(_MAX_REDIRECTS + 1)
    bodies = []
    for i, url in enumerate(urls):
        response, body = _watched(302, urls[i + 1] if i + 1 < len(urls) else "https://z.example/")
        routes[url] = response
        bodies.append(body)
    site = Site(routes)
    async with site.client() as http:
        with pytest.raises(ValueError, match="more than 5 times"):
            await _fetch(http, START, sleep=_noop_sleep)
    requested = bodies[: _MAX_REDIRECTS + 1]
    assert all(b.closed for b in requested)
    assert not any(b.read for b in bodies)


async def test_a_429_that_is_retried_is_closed_and_its_body_unread():
    first, first_body = _watched(429)
    site = Site({START: [first, _ok(b"later")]})
    async with site.client() as http:
        fetched = await _fetch(http, START, sleep=_noop_sleep)
    assert fetched.body == b"later"
    assert first_body.closed and not first_body.read


async def test_a_304_response_is_closed_and_its_body_unread():
    response, body = _watched(304)
    site = Site({START: response})
    async with site.client() as http:
        await _fetch(http, START, headers={"If-None-Match": '"v1"'}, sleep=_noop_sleep)
    assert body.closed and not body.read


class _BodyThatFails(httpx.AsyncByteStream):
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.closed = False

    async def __aiter__(self):
        yield b"<rss>partial"
        raise self.exc

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "exc",
    [
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        httpx.RemoteProtocolError("Invalid URL in the body, not the header"),
        httpx.ReadError("connection reset"),
        httpx.ReadTimeout("slow"),
        httpx.DecodingError("bad gzip"),
    ],
)
async def test_an_error_raised_mid_body_arrives_unchanged_and_the_response_is_closed(exc):
    body = _BodyThatFails(exc)
    site = Site({START: httpx.Response(200, stream=body)})
    async with site.client() as http:
        with pytest.raises(type(exc)) as excinfo:
            await _fetch(http, START, sleep=_noop_sleep)
    assert str(excinfo.value) == str(exc)
    assert body.closed


async def test_an_error_mid_body_after_a_redirect_is_not_reported_as_a_redirect_problem():
    body = _BodyThatFails(httpx.RemoteProtocolError("incomplete chunked read"))
    site = Site(
        {
            START: _redirect("https://b.example/f"),
            "https://b.example/f": httpx.Response(200, stream=body),
        }
    )
    async with site.client() as http:
        with pytest.raises(httpx.RemoteProtocolError, match="incomplete chunked read"):
            await _fetch(http, START, sleep=_noop_sleep)


class _BodyThatStalls(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b"first"
        await asyncio.sleep(3600)

    async def aclose(self) -> None:
        self.closed = True


async def test_a_timeout_that_cancels_a_stalled_body_still_closes_the_response():
    body = _BodyThatStalls()
    site = Site({START: httpx.Response(200, stream=body)})
    async with site.client() as http:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await _fetch(http, START, sleep=_noop_sleep)
    assert body.closed


# --- real httpcore, loopback sockets, and a resolver that changes its answer ---


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl to make a throwaway certificate with")
    directory = tmp_path_factory.mktemp("tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(  # noqa: S603 (a fixed argv, no shell, a path we just made)
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=feeds.example",
            "-addext",
            "subjectAltName=DNS:feeds.example,DNS:other.example",
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    return cert, key


class LoopbackServer:
    """A server on a loopback address that answers every request with `reply` (raw bytes)."""

    def __init__(self, reply: bytes, host: str, ssl_context: ssl.SSLContext | None) -> None:
        self.reply = reply
        self.host = host
        self.ssl_context = ssl_context
        self.requests = 0
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            writer.write(self.reply)
            await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError, ssl.SSLError:
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> LoopbackServer:
        self._server = await asyncio.start_server(self._handle, self.host, 0, ssl=self.ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


def _http_reply(status: str, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _server_context(tls_files) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*map(str, tls_files))
    return context


def _client_verify(tls_files) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(tls_files[0]))


def _rebind(monkeypatch, address: str, family: int = socket.AF_INET, after: int = 1) -> Lookups:
    """The first lookup of `feeds.example` (the host check's) answers a public address; every
    later one (the connection's) answers `address`, on loopback. Other names stay public.
    `after=0` is for a test with no host check in front: every lookup is the connection's."""
    seen = Lookups()

    async def resolver(self, host, port, **kwargs):
        host = host.decode("ascii") if isinstance(host, bytes) else host
        seen.names.append(host)
        if host == "feeds.example" and seen.names.count(host) > after:
            return [_answer(address, port or 0, family)]
        return [_answer(PUBLIC, port or 0)]

    def blocking_resolver(host, port, *args, **kwargs):
        # Whatever path the connection takes to a name, the answer is the same.
        if host == "feeds.example":
            return [_answer(address, port or 0, family)]
        return [_answer(PUBLIC, port or 0)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    monkeypatch.setattr(socket, "getaddrinfo", blocking_resolver)
    return seen


def _can_bind_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


LOOPBACKS = [
    pytest.param("127.0.0.1", socket.AF_INET, id="ipv4"),
    pytest.param(
        "::1",
        socket.AF_INET6,
        id="ipv6",
        marks=pytest.mark.skipif(not _can_bind_ipv6_loopback(), reason="no IPv6 loopback here"),
    ),
]


@pytest.mark.parametrize(("address", "family"), LOOPBACKS)
@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
async def test_dns_rebinding_to_loopback_is_caught_by_the_real_peer_check(
    address, family, tls, tls_files, monkeypatch
):
    """The host check saw a public answer; the connection landed on loopback. With a real
    httpcore stream behind the response, the peer check has to notice, and the body has to stay
    on the server's side. (The request itself goes out; that is the documented limit.)"""
    context = _server_context(tls_files) if tls else None
    reply = _http_reply("200 OK", {"Content-Type": "application/rss+xml"}, b"<rss>secret</rss>")
    async with LoopbackServer(reply, address, context) as server:
        _rebind(monkeypatch, address, family)
        scheme = "https" if tls else "http"
        kwargs = {"verify": _client_verify(tls_files)} if tls else {}
        async with httpx.AsyncClient(**kwargs) as http:
            with pytest.raises(AddressRefused, match="connected to a non-public address") as exc:
                await _fetch(
                    http, f"{scheme}://feeds.example:{server.port}/feed", sleep=_noop_sleep
                )
    assert f"feeds.example ({address})" in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
async def test_a_rebound_redirect_hop_is_refused_before_its_location_is_followed(
    tls, tls_files, monkeypatch
):
    context = _server_context(tls_files) if tls else None
    scheme = "https" if tls else "http"
    reply = _http_reply("302 Found", {"Location": f"{scheme}://other.example/next"})
    async with LoopbackServer(reply, "127.0.0.1", context) as server:
        seen = _rebind(monkeypatch, "127.0.0.1")
        kwargs = {"verify": _client_verify(tls_files)} if tls else {}
        async with httpx.AsyncClient(**kwargs) as http:
            with pytest.raises(AddressRefused, match="connected to a non-public address"):
                await _fetch(
                    http, f"{scheme}://feeds.example:{server.port}/feed", sleep=_noop_sleep
                )
    assert "other.example" not in seen.names


@pytest.mark.parametrize(("address", "family"), LOOPBACKS)
@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
async def test_httpcore_reports_the_server_address_the_peer_check_reads(
    address, family, tls, tls_files, monkeypatch
):
    """Pins the library contract the peer check depends on: a real response carries a
    `network_stream` whose `server_addr` is the socket's peer, as a tuple (a 4-tuple for IPv6).
    If an httpx or httpcore upgrade ever stopped reporting it, the check would skip itself
    silently, and this is the test that would say so."""
    context = _server_context(tls_files) if tls else None
    async with LoopbackServer(_http_reply("200 OK", {}, b"hi"), address, context) as server:
        _rebind(monkeypatch, address, family, after=0)
        scheme = "https" if tls else "http"
        kwargs = {"verify": _client_verify(tls_files)} if tls else {}
        async with httpx.AsyncClient(**kwargs) as http:
            async with http.stream("GET", f"{scheme}://feeds.example:{server.port}/") as response:
                stream = response.extensions["network_stream"]
                server_addr = stream.get_extra_info("server_addr")
    assert isinstance(server_addr, tuple)
    assert server_addr[0] == address
    assert server_addr[1] == server.port
    assert len(server_addr) in (2, 4)


async def test_the_lounge_catches_dns_rebinding_through_a_real_tls_stream(
    tls_files, monkeypatch, tmp_path
):
    reply = _http_reply("200 OK", TEXT_HEADERS, FORTUNE.encode())
    async with LoopbackServer(reply, "127.0.0.1", _server_context(tls_files)) as server:
        _rebind(monkeypatch, "127.0.0.1")
        src = QuoteSourceCfg(
            kind="url", value=f"https://feeds.example:{server.port}/q.txt?key=hunter2"
        )
        async with httpx.AsyncClient(verify=_client_verify(tls_files)) as http:
            from datetime import UTC, datetime

            result = await load_source(
                src, http=http, cache_dir=tmp_path / "cache", now=datetime(2026, 10, 23, tzinfo=UTC)
            )
    assert result.quotes == []
    assert "connected to a non-public address: feeds.example (127.0.0.1)" in result.problem
    assert "hunter2" not in result.problem


# --- the lounge's `_get_text` ---


async def _lounge(tmp_path, routes, url: str = LOUNGE_START):
    from test_lounge_sources_netguard import _load

    return await _load(tmp_path, routes, url)


def _lounge_redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": location})


@pytest.mark.parametrize(
    "location",
    [
        "//169.254.169.254/x",
        "//[::1]/x",
        "https://example.test@169.254.169.254/x",
        "https://[::ffff:169.254.169.254]/x",
        "https://[2002:a9fe:a9fe::]/x",
        "https://[fe80::1%25eth0]/x",
        "https://0.0.0.0/x",
        "https://169.254.169.254:8443/x",
    ],
)
async def test_lounge_hostile_location_spellings_are_never_requested(tmp_path, location):
    result, seen = await _lounge(tmp_path, {LOUNGE_START: _lounge_redirect(location)})
    assert result.quotes == []
    assert "was refused on a redirect: refusing a non-public host" in result.problem
    assert seen == [LOUNGE_START]


async def test_lounge_numeric_loopback_spelling_is_never_requested(tmp_path, lookups):
    if not _system_reads_as_loopback("2130706433"):
        pytest.skip("this system's resolver doesn't read 2130706433 as 127.0.0.1")
    result, seen = await _lounge(tmp_path, {LOUNGE_START: _lounge_redirect("https://2130706433/x")})
    assert "non-public host" in result.problem
    assert seen == [LOUNGE_START]


async def test_lounge_redirect_loop_stops_at_the_limit(tmp_path):
    a, b = LOUNGE_START, "https://other.test/q.txt"
    result, seen = await _lounge(tmp_path, {a: _lounge_redirect(b), b: _lounge_redirect(a)})
    assert "redirected more than" in result.problem
    assert len(seen) == 6


@pytest.mark.parametrize(
    "location", ["https://xn--/q.txt", "https:evil.test", "https://host:abc/q", "x" * 70_000]
)
async def test_lounge_junk_locations_are_a_problem_line_and_never_a_request(tmp_path, location):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers=[(b"location", location.encode())])

    from datetime import UTC, datetime

    src = QuoteSourceCfg(kind="url", value=LOUNGE_START)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await load_source(
            src, http=http, cache_dir=tmp_path / "cache", now=datetime(2026, 10, 23, tzinfo=UTC)
        )
    assert result.quotes == []
    assert result.problem
    assert "xxxxxxxxxx" not in result.problem
    assert seen == [LOUNGE_START]


SECRET_URL = "https://example.test/gist/quotes.txt?key=hunter2&sig=abc123"  # noqa: S105


@pytest.mark.parametrize("target_path", ["/x?key=hunter2", "/x/tok3n-in-target", "/x#hunter2"])
async def test_lounge_refusal_of_a_redirect_target_never_echoes_its_path_or_query(
    tmp_path, target_path
):
    routes = {SECRET_URL: _lounge_redirect(f"https://10.0.0.5{target_path}")}
    result, _ = await _lounge(tmp_path, routes, SECRET_URL)
    assert "10.0.0.5" in result.problem
    for secret in ("hunter2", "tok3n", "abc123", "key=", "sig="):
        assert secret not in result.problem


async def test_lounge_peer_refusal_never_echoes_the_query(tmp_path):
    class Stream:
        def get_extra_info(self, name):
            return ("10.0.0.9", 443) if name == "server_addr" else None

    response = httpx.Response(
        200, headers=TEXT_HEADERS, content=b"x", extensions={"network_stream": Stream()}
    )
    result, _ = await _lounge(tmp_path, {SECRET_URL: response}, SECRET_URL)
    assert "non-public address" in result.problem
    for secret in ("hunter2", "abc123"):
        assert secret not in result.problem


async def test_lounge_dns_timeout_never_echoes_the_query(tmp_path, monkeypatch):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(netguard, "DNS_TIMEOUT_S", 0.05)
    result, seen = await _lounge(tmp_path, {}, SECRET_URL)
    assert "DNS lookup timed out" in result.problem
    for secret in ("hunter2", "abc123"):
        assert secret not in result.problem
    assert seen == []


async def test_lounge_judges_an_idna_host_by_the_name_the_connection_uses(tmp_path, sharp_s_world):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return _loc("https://faß.example/q.txt")
        return httpx.Response(200, content=FORTUNE.encode(), headers=TEXT_HEADERS)

    from datetime import UTC, datetime

    src = QuoteSourceCfg(kind="url", value=LOUNGE_START)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await load_source(
            src, http=http, cache_dir=tmp_path / "cache", now=datetime(2026, 10, 23, tzinfo=UTC)
        )
    assert result.quotes == []
    assert seen == [LOUNGE_START]


async def test_lounge_userinfo_in_the_configured_url_is_not_echoed(tmp_path):
    url = "https://token:hunter2@10.0.0.5/quotes.txt"
    result, seen = await _lounge(tmp_path, {}, url)
    assert "hunter2" not in result.problem
    assert "token" not in result.problem
    assert seen == []


# --- what the owner and the admins read ---


GAME = GameCfg(key="borderlands4", name="Borderlands 4")


def _report_line(error: str | None, name: str = "Leaky Feed") -> str:
    text = render_guild_run_report(
        status="ok",
        run_date=__import__("datetime").date(2026, 10, 23),
        run_kind="scheduled",
        games=[GAME],
        counts={"borderlands4": 3},
        channels={"borderlands4": 11},
        posted_by_game={"borderlands4": 777},
        skipped={},
        duration=timedelta(seconds=3),
        notes=[],
        guild_id=42,
        sources=ReportSources(1, 0, [(name, error)]),
        timezone="UTC",
    )
    [line] = [ln for ln in text.splitlines() if ln.startswith("Sources:")]
    return line


def _leaky_source() -> RssSource:
    return RssSource(
        type="rss",
        name="Leaky Feed",
        url="https://feeds.example/gist/s3cr3t-token/feed.xml?key=hunter2",
        trust="press",
    )


LEAKY_URL = "https://feeds.example/gist/s3cr3t-token/feed.xml?key=hunter2"


@pytest.mark.parametrize(
    "location",
    [
        "https://10.0.0.5/x?key=hunter2",
        "http://plain.example/x?key=hunter2",
        "https://host:abc/x?key=hunter2",
        "ftp://files.example/x?key=hunter2",
    ],
)
async def test_a_refused_redirect_reaches_the_run_report_without_the_query_or_path(location):
    site = Site({LEAKY_URL: _redirect(location)})
    async with site.client() as http:
        result = await _run_one(RssCollector(_leaky_source(), sleep=_noop_sleep), http, 10.0)
    assert result.error
    line = _report_line(result.error)
    assert line.startswith("Sources: 0 of 1 ok (Leaky Feed: ")
    for secret in ("hunter2", "s3cr3t", "feed.xml", "key="):
        assert secret not in result.error
        assert secret not in line


async def test_a_refused_configured_host_reads_cleanly_in_the_run_report():
    source = RssSource(
        type="rss",
        name="Metadata",
        url="https://169.254.169.254/latest/?key=hunter2",
        trust="press",
    )
    site = Site({})
    async with site.client() as http:
        result = await _run_one(RssCollector(source, sleep=_noop_sleep), http, 10.0)
    assert result.error == "refusing a non-public host: 169.254.169.254 (169.254.169.254)"
    assert "hunter2" not in _report_line(result.error, "Metadata")


async def test_a_peer_refusal_reaches_the_run_report_with_host_and_address_only():
    class Stream:
        def get_extra_info(self, name):
            return ("10.0.0.9", 443) if name == "server_addr" else None

    response = httpx.Response(200, content=b"x", extensions={"network_stream": Stream()})
    site = Site({LEAKY_URL: response})
    async with site.client() as http:
        result = await _run_one(RssCollector(_leaky_source(), sleep=_noop_sleep), http, 10.0)
    assert result.error == "connected to a non-public address: feeds.example (10.0.0.9)"
    line = _report_line(result.error)
    assert "hunter2" not in line and "s3cr3t" not in line


async def test_a_dns_stall_reaches_the_run_report_as_a_refusal(monkeypatch):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(netguard, "DNS_TIMEOUT_S", 0.05)
    async with Site({}).client() as http:
        result = await _run_one(RssCollector(_leaky_source(), sleep=_noop_sleep), http, 10.0)
    assert result.error == "could not check where feeds.example points: DNS lookup timed out"
    assert "hunter2" not in _report_line(result.error)


async def test_a_hostile_long_hostname_cannot_flood_or_format_the_run_report_line(monkeypatch):
    """The refusal quotes the host, and the host came from somebody else's Location header: up to
    253 characters, with markdown characters allowed. The report line is capped and escaped."""
    host = ".".join(["a_b*c" * 10] * 4) + ".example"

    async def resolver(self, name, port, **kwargs):
        return [_answer("10.0.0.9" if name == host else PUBLIC)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    site = Site({LEAKY_URL: _redirect(f"https://{host}/x")})
    async with site.client() as http:
        result = await _run_one(RssCollector(_leaky_source(), sleep=_noop_sleep), http, 10.0)
    assert result.error and host in result.error
    line = _report_line(result.error)
    assert len(line) < 400
    assert "a_b*c" not in line  # escaped, not raw markdown
