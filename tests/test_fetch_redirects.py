"""`_fetch` follows redirects by hand and looks at every hop before it asks.

The old shape let httpx follow redirects and judged the final URL afterwards, so a feed server
could point the bot at the Droplet's metadata endpoint and the request had gone out before
anything objected. These tests all use a `MockTransport` that writes down every URL it is asked
for, because "was refused" isn't enough: the point is that the private address was never asked.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

from newsbot import netguard
from newsbot.collectors import rss as rss_module
from newsbot.collectors.base import RateLimited
from newsbot.collectors.rss import _MAX_REDIRECTS, _RETRY_BACKOFFS_S, _fetch
from newsbot.collectors.shift_table import PageValidators, ShiftTableCollector, Validators
from newsbot.config import ShiftTableSource

START = "https://feeds.example/feed.xml"
PAGE = (Path(__file__).parent / "fixtures" / "shift_table" / "page.html").read_bytes()


async def _noop_sleep(_seconds: float) -> None:
    return None


class Site:
    """A scripted web: `routes` maps a full URL to its response; `seen` is every URL asked."""

    def __init__(self, routes: dict[str, httpx.Response | list[httpx.Response]]) -> None:
        self.routes = routes
        self.seen: list[str] = []
        self.headers: list[httpx.Headers] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        self.headers.append(request.headers)
        if url not in self.routes:
            raise AssertionError(f"unexpected request to {url}")
        answer = self.routes[url]
        if isinstance(answer, list):
            return answer.pop(0)
        return answer

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _redirect(location: str | None, status: int = 302) -> httpx.Response:
    headers = {"Location": location} if location is not None else {}
    return httpx.Response(status, headers=headers)


def _ok(body: bytes = b"<rss/>") -> httpx.Response:
    return httpx.Response(200, content=body)


async def _get(site: Site, url: str = START, **kwargs):
    async with site.client() as http:
        return await _fetch(http, url, sleep=_noop_sleep, **kwargs)


# --- a private destination is refused before anything asks it ---


@pytest.mark.parametrize(
    "target",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://192.168.1.1/x",
        "https://100.64.0.1/x",
        "https://[::1]/x",
        "https://[fd00::1]/x",
    ],
)
async def test_a_redirect_to_a_private_literal_is_refused_before_it_is_requested(target):
    site = Site({START: _redirect(target)})
    with pytest.raises(ValueError, match="non-public host"):
        await _get(site)
    assert site.seen == [START]


@pytest.mark.parametrize(
    "target",
    ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/x", "http://[::1]/x"],
)
async def test_a_plain_http_redirect_to_a_private_literal_is_refused_too(target):
    # From an https feed this is a downgrade first and a private address second; either way no
    # request goes out, and the downgrade is the cheaper one to notice, so it speaks first.
    site = Site({START: _redirect(target)})
    with pytest.raises(ValueError, match="https to http"):
        await _get(site)
    assert site.seen == [START]


async def test_a_private_http_redirect_from_an_http_feed_is_refused_as_non_public():
    site = Site({"http://old.example/feed": _redirect("http://169.254.169.254/latest/")})
    with pytest.raises(ValueError, match="non-public host"):
        await _get(site, "http://old.example/feed")
    assert site.seen == ["http://old.example/feed"]


async def test_a_redirect_to_a_hostname_that_resolves_privately_is_never_requested(monkeypatch):
    async def resolver(self, host, port, **kwargs):
        address = "10.13.14.15" if host == "internal.attacker.example" else "93.184.215.14"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    site = Site({START: _redirect("https://internal.attacker.example/x")})
    with pytest.raises(ValueError, match="non-public host: internal.attacker.example"):
        await _get(site)
    assert site.seen == [START]


async def test_one_private_answer_among_public_ones_is_enough_to_refuse(monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    site = Site({})
    with pytest.raises(ValueError, match="non-public host"):
        await _get(site)
    assert site.seen == []


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/latest/", "https://127.0.0.1/feed", "http://[::1]/f"]
)
async def test_the_configured_url_pointing_private_is_refused_before_any_request(url):
    site = Site({})
    with pytest.raises(ValueError, match="non-public host"):
        await _get(site, url)
    assert site.seen == []


async def test_the_configured_url_resolving_privately_is_refused_before_any_request(monkeypatch):
    async def resolver(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    site = Site({})
    with pytest.raises(ValueError, match="non-public host: feeds.example"):
        await _get(site)
    assert site.seen == []


async def test_a_private_hop_in_the_middle_of_a_chain_stops_the_chain():
    site = Site(
        {
            START: _redirect("https://b.example/1"),
            "https://b.example/1": _redirect("https://10.0.0.5/2"),
        }
    )
    with pytest.raises(ValueError, match="non-public host"):
        await _get(site)
    assert site.seen == [START, "https://b.example/1"]


# --- DNS: a stall refuses, a plain failure passes through ---


async def test_a_dns_stall_on_a_redirect_target_refuses_the_source(monkeypatch):
    async def resolver(self, host, port, **kwargs):
        if host == "slow.example":
            await asyncio.sleep(3600)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    monkeypatch.setattr(netguard, "DNS_TIMEOUT_S", 0.05)
    site = Site({START: _redirect("https://slow.example/f")})
    with pytest.raises(ValueError, match="slow.example.*DNS lookup timed out"):
        await _get(site)
    assert site.seen == [START]


async def test_a_dns_stall_on_the_configured_url_refuses_before_any_request(monkeypatch):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(netguard, "DNS_TIMEOUT_S", 0.05)
    site = Site({})
    with pytest.raises(ValueError, match="DNS lookup timed out"):
        await _get(site)
    assert site.seen == []


async def test_a_plain_dns_failure_passes_through_to_the_request(monkeypatch):
    async def broken(self, host, port, **kwargs):
        raise OSError("no such host")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", broken)
    site = Site({START: _ok(b"fine")})
    fetched = await _get(site)
    assert fetched.body == b"fine"
    assert site.seen == [START]


# --- the chain itself ---


@pytest.mark.parametrize("hops", range(1, _MAX_REDIRECTS + 1))
async def test_a_public_chain_of_up_to_five_hops_is_followed(hops):
    urls = [START, *(f"https://hop{i}.example/feed" for i in range(1, hops + 1))]
    routes = {urls[i]: _redirect(urls[i + 1]) for i in range(hops)}
    routes[urls[-1]] = _ok(b"the end")
    site = Site(routes)
    fetched = await _get(site)
    assert fetched.body == b"the end"
    assert site.seen == urls


async def test_a_sixth_redirect_is_refused():
    urls = [START, *(f"https://hop{i}.example/feed" for i in range(1, _MAX_REDIRECTS + 2))]
    routes = {urls[i]: _redirect(urls[i + 1]) for i in range(len(urls) - 1)}
    routes[urls[-1]] = _ok()
    site = Site(routes)
    with pytest.raises(ValueError, match="feeds.example|hop5.example") as excinfo:
        await _get(site)
    assert str(excinfo.value).endswith("redirected more than 5 times")
    assert site.seen == urls[:-1]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_redirect_status_is_followed(status):
    site = Site({START: _redirect("https://b.example/f", status), "https://b.example/f": _ok(b"x")})
    assert (await _get(site)).body == b"x"


async def test_a_300_is_not_a_redirect_here():
    site = Site({START: httpx.Response(300, headers={"Location": "https://b.example/f"})})
    with pytest.raises(httpx.HTTPStatusError):
        await _get(site)
    assert site.seen == [START]


async def test_relative_locations_are_resolved_against_the_current_url():
    site = Site(
        {
            START: _redirect("/moved/feed.xml"),
            "https://feeds.example/moved/feed.xml": _redirect("next.xml"),
            "https://feeds.example/moved/next.xml": _redirect("//cdn.example/final.xml"),
            "https://cdn.example/final.xml": _ok(b"arrived"),
        }
    )
    assert (await _get(site)).body == b"arrived"
    assert site.seen == [
        START,
        "https://feeds.example/moved/feed.xml",
        "https://feeds.example/moved/next.xml",
        "https://cdn.example/final.xml",
    ]


@pytest.mark.parametrize("location", [None, ""])
async def test_a_redirect_with_no_location_is_an_error(location):
    site = Site({START: _redirect(location)})
    with pytest.raises(ValueError, match="feeds.example answered HTTP 302 with no Location"):
        await _get(site)


async def test_a_location_that_is_not_a_url_is_an_error():
    site = Site({START: _redirect("https://host:abc/feed")})
    with pytest.raises(ValueError, match="feeds.example redirected to an address that isn't"):
        await _get(site)
    assert site.seen == [START]


async def test_a_redirect_to_another_scheme_is_refused():
    site = Site({START: _redirect("ftp://files.example/feed.xml")})
    with pytest.raises(ValueError, match="non-http address"):
        await _get(site)
    assert site.seen == [START]


# --- https to http ---


async def test_an_https_to_http_downgrade_is_refused():
    site = Site({START: _redirect("http://plain.example/feed.xml")})
    with pytest.raises(ValueError, match="feeds.example redirected from https to http"):
        await _get(site)
    assert site.seen == [START]


async def test_a_downgrade_later_in_the_chain_is_refused_too():
    site = Site(
        {
            START: _redirect("https://b.example/f"),
            "https://b.example/f": _redirect("http://c.example/f"),
        }
    )
    with pytest.raises(ValueError, match="b.example redirected from https to http"):
        await _get(site)
    assert site.seen == [START, "https://b.example/f"]


async def test_an_http_configured_url_is_still_allowed_and_may_stay_on_http_or_upgrade():
    site = Site(
        {
            "http://old.example/feed": _redirect("http://old.example/feed2"),
            "http://old.example/feed2": _redirect("https://old.example/feed3"),
            "https://old.example/feed3": _ok(b"upgraded"),
        }
    )
    assert (await _get(site, "http://old.example/feed")).body == b"upgraded"


# --- the connection's own address ---


class _FakeStream:
    """Just enough of httpcore's network stream for `get_extra_info`."""

    def __init__(self, server_addr) -> None:
        self._server_addr = server_addr

    def get_extra_info(self, info: str):
        return self._server_addr if info == "server_addr" else None


class _WatchedBody(httpx.AsyncByteStream):
    """A body that remembers whether anybody read it or closed it."""

    def __init__(self, data: bytes = b"<rss/>") -> None:
        self.data = data
        self.read = False
        self.closed = False

    async def __aiter__(self):
        self.read = True
        yield self.data

    async def aclose(self) -> None:
        self.closed = True


def _from(address, *, status: int = 200, headers=None, body=None) -> httpx.Response:
    extensions = {"network_stream": _FakeStream((address, 443))}
    return httpx.Response(
        status, headers=headers, stream=body or _WatchedBody(), extensions=extensions
    )


@pytest.mark.parametrize(
    "address", ["10.0.0.9", "127.0.0.1", "169.254.169.254", "::1", "100.64.0.1"]
)
async def test_a_connection_to_a_non_public_address_is_refused_before_the_body_is_read(address):
    body = _WatchedBody()
    site = Site({START: _from(address, body=body)})
    with pytest.raises(ValueError, match="connected to a non-public address: feeds.example"):
        await _get(site)
    assert not body.read


async def test_a_redirect_answered_from_a_non_public_address_is_refused_too():
    site = Site({START: _from("10.0.0.9", status=302, headers={"Location": "https://b.example/f"})})
    with pytest.raises(ValueError, match="connected to a non-public address"):
        await _get(site)
    assert site.seen == [START]


async def test_the_peer_is_checked_on_every_hop_not_just_the_first():
    site = Site(
        {
            START: _from("93.184.215.14", status=302, headers={"Location": "https://b.example/f"}),
            "https://b.example/f": _from("10.0.0.9"),
        }
    )
    with pytest.raises(ValueError, match="connected to a non-public address: b.example"):
        await _get(site)


async def test_a_public_connection_is_fine():
    site = Site({START: _from("93.184.215.14")})
    assert (await _get(site)).body == b"<rss/>"


@pytest.mark.parametrize("server_addr", [None, "/var/run/proxy.sock", (), ("not-an-ip", 443)])
async def test_a_missing_or_unreadable_peer_address_skips_the_check(server_addr):
    response = httpx.Response(
        200, content=b"fine", extensions={"network_stream": _FakeStream(server_addr)}
    )
    assert (await _get(Site({START: response}))).body == b"fine"


async def test_a_response_with_no_network_stream_skips_the_check():
    assert (await _get(Site({START: _ok(b"fine")}))).body == b"fine"


# --- what the redirect rewrite must not break ---


async def test_each_redirect_response_is_closed_before_the_next_request():
    first = _WatchedBody()
    state = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == START:
            return httpx.Response(302, headers={"Location": "https://b.example/f"}, stream=first)
        state["first_closed_when_second_asked"] = first.closed
        return _ok(b"done")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _fetch(http, START, sleep=_noop_sleep)
    assert state == {"first_closed_when_second_asked": True}


async def test_a_429_retry_walks_the_redirects_again_from_the_start():
    sleeps = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    site = Site(
        {
            START: [_redirect("https://b.example/f"), _redirect("https://b.example/f")],
            "https://b.example/f": [httpx.Response(429), _ok(b"second time")],
        }
    )
    async with site.client() as http:
        fetched = await _fetch(http, START, sleep=sleep)
    assert fetched.body == b"second time"
    assert sleeps == [_RETRY_BACKOFFS_S[0]]
    assert site.seen == [START, "https://b.example/f", START, "https://b.example/f"]


async def test_the_redirect_allowance_starts_over_on_every_attempt():
    urls = [START, *(f"https://hop{i}.example/feed" for i in range(1, _MAX_REDIRECTS + 1))]
    routes: dict[str, httpx.Response | list[httpx.Response]] = {
        urls[i]: [_redirect(urls[i + 1]), _redirect(urls[i + 1])] for i in range(_MAX_REDIRECTS)
    }
    routes[urls[-1]] = [httpx.Response(429), _ok(b"made it")]
    site = Site(routes)
    assert (await _get(site)).body == b"made it"
    assert len(site.seen) == 2 * len(urls)


async def test_a_reddit_style_429_after_a_redirect_still_raises_rate_limited():
    site = Site(
        {
            START: _redirect("https://b.example/f"),
            "https://b.example/f": httpx.Response(429, headers={"Retry-After": "30"}),
        }
    )
    with pytest.raises(RateLimited) as excinfo:
        await _get(site, retry_429=False)
    assert excinfo.value.retry_after_s == 30
    assert len(site.seen) == 2


async def test_conditional_headers_go_on_every_hop():
    site = Site({START: _redirect("https://b.example/f"), "https://b.example/f": _ok()})
    await _get(
        site,
        headers={"If-None-Match": '"v1"', "If-Modified-Since": "Wed, 07 Oct 2026 12:00:00 GMT"},
    )
    assert len(site.headers) == 2
    for headers in site.headers:
        assert headers["if-none-match"] == '"v1"'
        assert headers["if-modified-since"] == "Wed, 07 Oct 2026 12:00:00 GMT"


async def test_a_304_after_a_redirect_is_still_an_answer():
    site = Site(
        {
            START: _redirect("https://b.example/f"),
            "https://b.example/f": httpx.Response(304, headers={"ETag": '"v1"'}),
        }
    )
    fetched = await _get(site, headers={"If-None-Match": '"v1"'})
    assert (fetched.status, fetched.body, fetched.etag) == (304, b"", '"v1"')


async def test_the_size_cap_still_applies_after_a_redirect():
    big = b"a" * (rss_module._MAX_RESPONSE_BYTES + 10)
    site = Site({START: _redirect("https://b.example/f"), "https://b.example/f": _ok(big)})
    assert len((await _get(site)).body) == rss_module._MAX_RESPONSE_BYTES


# --- shift_table shares _fetch ---


def _table_collector(validators: PageValidators | None = None) -> ShiftTableCollector:
    source = ShiftTableSource(type="shift_table", name="mentalmars", url=START, trust="community")
    return ShiftTableCollector(source, validators=validators, sleep=_noop_sleep)


async def test_shift_table_follows_a_public_redirect():
    site = Site({START: _redirect("https://b.example/codes"), "https://b.example/codes": _ok(PAGE)})
    async with site.client() as http:
        items = await _table_collector().collect(http)
    assert items
    assert site.seen == [START, "https://b.example/codes"]


async def test_shift_table_never_requests_a_private_redirect_target():
    site = Site({START: _redirect("https://169.254.169.254/latest/")})
    async with site.client() as http:
        with pytest.raises(ValueError, match="non-public host"):
            await _table_collector().collect(http)
    assert site.seen == [START]


async def test_shift_table_sends_its_validators_on_the_redirected_hop_and_takes_the_304():
    validators = PageValidators({START: Validators('"v1"', None)})
    site = Site(
        {
            START: _redirect("https://b.example/codes"),
            "https://b.example/codes": httpx.Response(304),
        }
    )
    async with site.client() as http:
        assert await _table_collector(validators).collect(http) == []
    assert [h["if-none-match"] for h in site.headers] == ['"v1"', '"v1"']


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "64:ff9b:1::1"])
async def test_a_nat64_address_embedding_a_private_ipv4_is_refused_before_it_is_requested(address):
    site = Site({START: _redirect(f"https://[{address}]/x")})
    with pytest.raises(ValueError, match="non-public host"):
        await _get(site)
    assert site.seen == [START]


async def test_a_nat64_address_embedding_a_public_ipv4_is_allowed():
    # 64:ff9b::5db8:d70e is 93.184.215.14 behind a gateway.
    site = Site(
        {
            START: _redirect("https://[64:ff9b::5db8:d70e]/x"),
            "https://[64:ff9b::5db8:d70e]/x": _ok(b"ok"),
        }
    )
    assert (await _get(site)).body == b"ok"


# --- qa round (2026-10-23) ---


async def test_httpx_still_words_an_unparseable_location_the_way_stream_hop_expects():
    # `_stream_hop` translates this one RemoteProtocolError by its message. If an httpx
    # upgrade rewords it, this fails, instead of the translation quietly stopping.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://feeds.example:abc/x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.RemoteProtocolError) as caught:
            async with http.stream("GET", START, follow_redirects=False):
                pass
    assert str(caught.value).startswith(rss_module._HTTPX_BAD_LOCATION)


async def test_an_unparseable_location_is_a_value_error_naming_the_host():
    site = Site({START: _redirect("https://feeds.example:abc/x")})
    with pytest.raises(ValueError, match="feeds.example redirected to an address that isn't a"):
        await _get(site)


@pytest.mark.parametrize("host", ["a" * 64 + ".example", "a..example"])
async def test_a_name_no_resolver_could_look_up_is_refused_in_plain_words(host, monkeypatch):
    # The suite's DNS stub answers anything; the real `getaddrinfo` encodes the name with
    # Python's IDNA codec first, which refuses these two (checked by hand on 3.14).
    loop = asyncio.get_running_loop()

    async def encoding_getaddrinfo(name, port, *args, **kwargs):
        name.encode("idna")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]

    monkeypatch.setattr(loop, "getaddrinfo", encoding_getaddrinfo)
    site = Site({START: _redirect(f"https://{host}/x")})
    with pytest.raises(netguard.AddressRefused, match="isn't a name that can be looked up"):
        await _get(site)
    assert site.seen == [START]


async def test_an_error_while_reading_a_body_keeps_its_own_name():
    # Only opening the stream is translated; a failure after that is not a "redirect".
    class Boom(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "made up")
            yield b""  # pragma: no cover

    site = Site({START: httpx.Response(200, stream=Boom())})
    with pytest.raises(UnicodeDecodeError):
        await _get(site)
