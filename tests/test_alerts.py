"""Tests for newsbot.alerts.send_alert: the "never raises" contract.

Per plan section 5, no gateway mocking: these use trivial fake client
objects that satisfy the tiny surface `send_alert` actually touches
(`get_channel`, `fetch_channel`, and a channel with `.send`), not a mock
of `discord.Client` itself.
"""

from __future__ import annotations

import logging

from newsbot.alerts import send_alert


class _FakeChannel:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.sent: list[str] = []

    async def send(self, text: str, **_kwargs) -> None:
        if self._raises is not None:
            raise self._raises
        self.sent.append(text)


class _FakeClient:
    """Mimics the real client's `get_channel`/`fetch_channel` pair, no gateway required."""

    def __init__(self, *, cached: _FakeChannel | None = None, fetch_result=None, fetch_error=None):
        self._cached = cached
        self._fetch_result = fetch_result
        self._fetch_error = fetch_error
        self.fetch_called = False

    def get_channel(self, channel_id: int):
        return self._cached

    async def fetch_channel(self, channel_id: int):
        self.fetch_called = True
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._fetch_result


async def test_send_alert_does_nothing_when_no_admin_channel_configured():
    client = _FakeClient()
    await send_alert(client, None, "hello")
    assert client.fetch_called is False


async def test_send_alert_uses_cached_channel_without_fetching():
    channel = _FakeChannel()
    client = _FakeClient(cached=channel)
    await send_alert(client, 123, "hello")
    assert channel.sent == ["hello"]
    assert client.fetch_called is False


async def test_send_alert_falls_back_to_fetch_channel_when_not_cached():
    channel = _FakeChannel()
    client = _FakeClient(cached=None, fetch_result=channel)
    await send_alert(client, 123, "hello")
    assert channel.sent == ["hello"]
    assert client.fetch_called is True


async def test_send_alert_never_raises_when_fetch_channel_fails():
    client = _FakeClient(cached=None, fetch_error=RuntimeError("channel deleted"))
    await send_alert(client, 123, "hello")  # must not raise


async def test_send_alert_never_raises_when_channel_send_fails():
    channel = _FakeChannel(raises=RuntimeError("missing permissions"))
    client = _FakeClient(cached=channel)
    await send_alert(client, 123, "hello")  # must not raise


async def test_send_alert_logs_when_it_swallows_a_failure(caplog):
    client = _FakeClient(cached=None, fetch_error=RuntimeError("channel deleted"))
    with caplog.at_level(logging.ERROR, logger="newsbot.alerts"):
        await send_alert(client, 123, "hello")
    assert "failed to send admin alert" in caplog.text
