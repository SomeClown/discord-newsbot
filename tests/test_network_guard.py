"""The suite's network guard (conftest's `_no_real_network`) and the RSS DNS timeout.

A guard nobody tests is a guard that quietly stops guarding, so: lookups answer with the
public test address, connecting anywhere but loopback raises, and a resolver that never
answers refuses the source instead of hanging the pass.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from conftest import PUBLIC_TEST_ADDRESS, NetworkBlockedError

from newsbot.collectors import rss as rss_module
from newsbot.collectors.rss import _reject_private_redirect


async def test_the_loop_resolves_every_name_to_the_public_test_address():
    infos = await asyncio.get_running_loop().getaddrinfo("anything.example", 443)
    assert infos[0][4][0] == PUBLIC_TEST_ADDRESS


def test_socket_getaddrinfo_is_stubbed_too():
    assert socket.getaddrinfo("anything.example", 80)[0][4][0] == PUBLIC_TEST_ADDRESS


@pytest.mark.parametrize("address", ["93.184.215.14", "10.0.0.1", "example.com"])
def test_connecting_to_a_non_loopback_address_fails_fast(address):
    with socket.socket() as sock, pytest.raises(NetworkBlockedError, match="fake the transport"):
        sock.connect((address, 80))


def test_connect_ex_is_guarded_too():
    with socket.socket() as sock, pytest.raises(NetworkBlockedError):
        sock.connect_ex(("93.184.215.14", 80))


def test_loopback_is_still_allowed():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.socket() as client:
            client.connect(server.getsockname())


async def test_a_stalled_resolver_refuses_the_source(monkeypatch):
    async def never(self, host, port, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", never)
    monkeypatch.setattr(rss_module, "_DNS_TIMEOUT_S", 0.05)
    response = httpx.Response(200, request=httpx.Request("GET", "https://slow.example/feed"))
    with pytest.raises(ValueError, match="DNS lookup timed out"):
        await _reject_private_redirect(response)


async def test_a_failed_lookup_is_still_not_a_refusal(monkeypatch):
    async def broken(self, host, port, **kwargs):
        raise OSError("no such host")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", broken)
    response = httpx.Response(200, request=httpx.Request("GET", "https://gone.example/feed"))
    await _reject_private_redirect(response)
