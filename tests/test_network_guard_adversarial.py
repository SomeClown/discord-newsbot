"""Adversarial tests for the suite's network guard and the RSS collector's DNS time box.

The guard is `network_guard._no_real_network`: every lookup answers with a public test address,
connecting to anything but loopback raises on the spot, and every blocked attempt is recorded, so
a test that ends with one fails at teardown even if the error was swallowed. A guard that only
works against the caller who politely uses `socket.connect` isn't one, so these go through the
doors people really use: `httpx` with no transport, `asyncio.open_connection`,
`socket.create_connection`, an IPv6 literal, a name that resolves to loopback. They also check
the opt-out (`real_network`) really opts out, and that the RSS redirect check refuses a source
whose lookup stalls, whole path included, and still waves through the cases it always did.

Nothing here reaches a real network: a blocked connection is the thing under test, and the
opt-out test only compares function identities.
"""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest
from network_guard import PUBLIC_TEST_ADDRESS, NetworkBlockedError

from newsbot.collectors import rss as rss_module
from newsbot.collectors.rss import RssCollector, _reject_private_redirect
from newsbot.config import RssSource


async def test_an_httpx_client_with_no_transport_cannot_reach_out(blocked_attempts):
    """It fails, fast, but not as a bare `NetworkBlockedError`: anyio's task group wraps it."""
    started = time.monotonic()
    async with httpx.AsyncClient() as client:
        with pytest.raises(BaseExceptionGroup) as raised:
            await client.get("http://definitely-real.example/feed")
    assert raised.group_contains(NetworkBlockedError)
    assert time.monotonic() - started < 2  # no waiting on a timeout
    assert blocked_attempts.acknowledge() == [PUBLIC_TEST_ADDRESS]


async def test_an_except_exception_still_swallows_the_error_at_the_call_site(blocked_attempts):
    """The error is a `RuntimeError`, so app code that catches `Exception` (the collectors do,
    so one dead source can't sink a pass) turns a blocked connection into an ordinary 'source
    failed'. That's fine for the app. The guard doesn't depend on the error getting through:
    it recorded the attempt, and the test below shows that a test which swallowed one fails."""
    try:
        async with httpx.AsyncClient() as client:
            await client.get("http://definitely-real.example/feed")
    except Exception as exc:  # the app's habit
        swallowed = exc
    assert isinstance(swallowed, BaseExceptionGroup) and isinstance(swallowed, Exception)
    assert blocked_attempts.acknowledge() == [PUBLIC_TEST_ADDRESS]


_INI = """
[pytest]
markers =
    real_network: opt out
asyncio_default_fixture_loop_scope = function
"""

_ARGS = ("-p", "no:cacheprovider", "-W", "ignore::pytest.PytestAssertRewriteWarning")

_SWALLOWER = """
import socket

def test_swallows_the_error():
    try:
        with socket.socket() as sock:
            sock.connect(("93.184.215.14", 80))
    except Exception:
        pass

def test_leaves_the_error_alone():
    with socket.socket() as sock:
        try:
            sock.connect(("93.184.215.14", 80))
        except RuntimeError:
            pass
        else:
            raise AssertionError("the guard should have raised")
"""


def test_the_guards_error_cannot_be_swallowed_by_an_except_exception(pytester):
    """Run the guard in a throwaway session: a test that swallows the error passes its own body
    and still fails, at teardown, naming the host. Both of these connect, so both are caught
    (the second one saw the error, but didn't say it expected it)."""
    pytester.makeini(_INI)
    pytester.makeconftest('pytest_plugins = ("network_guard",)')
    pytester.makepyfile(_SWALLOWER)
    result = pytester.runpytest_inprocess(*_ARGS)
    result.assert_outcomes(passed=2, errors=2)
    result.stdout.fnmatch_lines(["*tried 1 real connection(s) (93.184.215.14)*"])


def test_a_test_that_takes_its_blocked_attempts_does_not_fail_and_real_network_skips_teardown(
    pytester,
):
    pytester.makeini(_INI)
    pytester.makeconftest('pytest_plugins = ("network_guard",)')
    pytester.makepyfile(
        """
        import socket
        import pytest

        def test_expected(blocked_attempts):
            with socket.socket() as sock:
                try:
                    sock.connect(("93.184.215.14", 80))
                except Exception:
                    pass
            assert blocked_attempts.acknowledge() == ["93.184.215.14"]

        @pytest.mark.real_network
        def test_opted_out():
            pass  # the guard is off, so there's nothing for teardown to report
        """
    )
    pytester.runpytest_inprocess(*_ARGS).assert_outcomes(passed=2)


async def test_asyncio_open_connection_cannot_reach_out(blocked_attempts):
    with pytest.raises(NetworkBlockedError):
        await asyncio.wait_for(asyncio.open_connection("93.184.215.14", 443), timeout=2)
    assert blocked_attempts.acknowledge() == ["93.184.215.14"]


async def test_asyncio_open_connection_by_name_resolves_to_the_blocked_test_address(
    blocked_attempts,
):
    with pytest.raises(NetworkBlockedError, match=PUBLIC_TEST_ADDRESS):
        await asyncio.wait_for(asyncio.open_connection("somewhere.example", 443), timeout=2)
    assert blocked_attempts.acknowledge() == [PUBLIC_TEST_ADDRESS]


def test_socket_create_connection_cannot_reach_out(blocked_attempts):
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("somewhere.example", 80), timeout=1)
    assert blocked_attempts.acknowledge() == [PUBLIC_TEST_ADDRESS]


@pytest.mark.parametrize("address", ["2001:db8::1", "2606:4700:4700::1111", "fe80::1"])
def test_a_non_loopback_ipv6_literal_is_blocked_too(address, blocked_attempts):
    with socket.socket(socket.AF_INET6) as sock, pytest.raises(NetworkBlockedError):
        sock.connect((address, 80, 0, 0))
    assert blocked_attempts.acknowledge() == [address]


@pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.1", "169.254.169.254"])  # noqa: S104
def test_private_and_unspecified_addresses_are_not_loopback_so_they_are_blocked(
    address, blocked_attempts
):
    with socket.socket() as sock, pytest.raises(NetworkBlockedError):
        sock.connect((address, 80))
    assert blocked_attempts.acknowledge() == [address]


def test_a_connection_to_localhost_by_name_gets_through_to_the_real_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        with socket.socket() as client:
            client.connect(("localhost", port))  # the guard lets it by; the OS does the rest


async def test_a_loopback_connection_through_asyncio_still_works():
    async def handle(reader, writer):
        writer.write(b"hi")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
        assert await reader.read(2) == b"hi"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


def test_the_stubbed_lookup_honors_the_socket_type_asked_for():
    infos = socket.getaddrinfo("anything.example", 53, type=socket.SOCK_DGRAM)
    assert infos[0][1] == socket.SOCK_DGRAM and infos[0][4] == (PUBLIC_TEST_ADDRESS, 53)


@pytest.mark.real_network
def test_the_opt_out_really_removes_the_guard_without_touching_the_network():
    """Only identities are compared: a real connection here would defeat the point."""
    assert socket.getaddrinfo.__name__ == "getaddrinfo"
    assert socket.socket.connect.__qualname__.startswith("socket.")  # not the guard's closure
    assert not socket.socket.connect.__qualname__.startswith("_no_real_network")


def test_without_the_opt_out_the_guard_is_in_place():
    assert socket.getaddrinfo.__name__ == "_fake_addrinfo"
    assert "guarded_connect" in socket.socket.connect.__qualname__


# --- the RSS redirect check's DNS time box ---


def feed_collector(url: str) -> RssCollector:
    return RssCollector(
        RssSource(type="rss", name="A Feed", url=url, trust="press"), sleep=_no_sleep
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def a_feed_transport() -> httpx.MockTransport:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"<item><title>x</title><link>https://example.com/1</link></item></channel></rss>"
    )
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body))


async def test_a_stalled_resolver_refuses_the_source_through_the_real_collector(monkeypatch):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(rss_module, "_DNS_TIMEOUT_S", 0.05)
    async with httpx.AsyncClient(transport=a_feed_transport()) as http:
        started = time.monotonic()
        with pytest.raises(ValueError, match="DNS lookup timed out"):
            await feed_collector("https://slow.example/feed").collect(http)
    assert time.monotonic() - started < 2


async def test_a_resolver_that_answers_inside_the_limit_is_not_refused(monkeypatch):
    async def quick(self, host, port, **kwargs):
        await asyncio.sleep(0.01)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_TEST_ADDRESS, 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", quick)
    monkeypatch.setattr(rss_module, "_DNS_TIMEOUT_S", 1.0)
    async with httpx.AsyncClient(transport=a_feed_transport()) as http:
        items = await feed_collector("https://fine.example/feed").collect(http)
    assert [i.title for i in items] == ["x"]


async def test_an_ip_literal_host_never_asks_the_resolver_at_all(monkeypatch):
    async def explode(self, host, port, **kwargs):
        raise AssertionError("an IP literal doesn't need DNS")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", explode)
    response = httpx.Response(200, request=httpx.Request("GET", f"https://{PUBLIC_TEST_ADDRESS}/f"))
    await _reject_private_redirect(response)


@pytest.mark.parametrize(
    "host",
    ["10.0.0.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "[::1]", "[::ffff:127.0.0.1]"],
)
async def test_private_literals_are_still_refused_whatever_the_resolver_is_doing(monkeypatch, host):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    response = httpx.Response(200, request=httpx.Request("GET", f"https://{host}/f"))
    with pytest.raises(ValueError, match="non-public host"):
        await _reject_private_redirect(response)


async def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    async def inside(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", inside)
    response = httpx.Response(200, request=httpx.Request("GET", "https://sneaky.example/f"))
    with pytest.raises(ValueError, match="non-public host"):
        await _reject_private_redirect(response)


async def test_an_outer_timeout_is_not_mistaken_for_a_dns_stall(monkeypatch):
    """A caller's own deadline cancels the lookup; that must surface as the caller's timeout,
    not as the redirect check's 'DNS lookup timed out' ValueError."""

    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(rss_module, "_DNS_TIMEOUT_S", 30.0)
    response = httpx.Response(200, request=httpx.Request("GET", "https://slow.example/f"))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _reject_private_redirect(response)
