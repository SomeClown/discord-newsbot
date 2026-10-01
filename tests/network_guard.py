"""The suite's network guard: no real DNS and no real outbound connections from a test.

The suite used to make about 41 real DNS lookups (the RSS redirect check resolves hosts even
under `MockTransport`) and at least one real connection to Steam, and an occasional stalled
resolver looked like a hung test run. Now every lookup answers with a public test address,
and connecting to anything but loopback or a unix socket raises on the spot.

Raising isn't enough on its own, and I learned why the dull way: the error is a
`RuntimeError`, the collectors (rightly) catch `Exception` so one dead source can't sink a
pass, and so a blocked connection turned into an ordinary "source failed" while the test went
on to pass. So the guard also keeps a list of every attempt, and a test that ends with
anything in it fails at teardown, whatever the code under test did with the exception. A test
that blocks a connection on purpose says so with `blocked_attempts.acknowledge()`, which
hands the attempts back for it to assert on. A test marked `real_network` opts out entirely.

It lives in its own module, not in `conftest.py`, so a throwaway pytest session (see
`test_network_guard_adversarial.py`) can load the very same code as a plugin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest

# What every hostname resolves to during a test (a public address, so the redirect guard sees a
# perfectly ordinary host). Tests that care about a particular answer patch their own.
PUBLIC_TEST_ADDRESS = "93.184.215.14"


class NetworkBlockedError(RuntimeError):
    """A test tried to open a real connection. It should have used a fake."""


class BlockedAttempts(list):
    """The hosts a test tried to connect to, in order. Anything left in it fails the test."""

    def acknowledge(self) -> list[str]:
        """Take the attempts (and clear them): the test expected them and has checked."""
        taken = list(self)
        self.clear()
        return taken


def _fake_addrinfo(host, port, family=0, type=0, proto=0, flags=0):
    kind = type or socket.SOCK_STREAM
    return [
        (socket.AF_INET, kind, proto or socket.IPPROTO_TCP, "", (PUBLIC_TEST_ADDRESS, port or 0))
    ]


@pytest.fixture
def blocked_attempts() -> BlockedAttempts:
    """Every connection the guard blocked during this test (see the module docstring)."""
    return BlockedAttempts()


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch, blocked_attempts):
    """Guard the network for one test (`real_network` marker opts out); fail it if it was hit."""
    if request.node.get_closest_marker("real_network"):
        yield
        return

    async def fake_loop_getaddrinfo(self, host, port, **kwargs):
        return _fake_addrinfo(host, port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_loop_getaddrinfo)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(address) -> None:
        if isinstance(address, tuple):
            host = str(address[0])
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                blocked_attempts.append(host)
                raise NetworkBlockedError(
                    f"test tried to connect to {host}; fake the transport instead"
                )

    def guarded_connect(self, address):
        check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    yield

    if blocked_attempts:
        pytest.fail(
            f"the test tried {len(blocked_attempts)} real connection(s) "
            f"({', '.join(blocked_attempts)}), blocked, but the error may have been swallowed "
            "on the way (an `except Exception`), so the test passed anyway. Fake the transport; "
            "or, if blocking is the point, take them with `blocked_attempts.acknowledge()`; "
            "or mark the test `real_network`.",
            pytrace=False,
        )
