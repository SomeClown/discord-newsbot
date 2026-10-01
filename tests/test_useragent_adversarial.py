"""Adversarial tests for `newsbot.useragent`, beyond test_useragent.py's own
happy-path coverage.

Angle (self-host plan, task 1, test-engineer brief 2026-09-26): what a
malicious or merely careless `NEWSBOT_CONTACT` value actually does to the
header httpx sends, whether the startup warning really only fires once per
process, and whether the three former hardcoded call sites (pipeline/run.py,
bot/client.py, collectors/rss.py) still send the header that
`user_agent_headers` builds, not some other copy.

httpx.MockTransport stands in for the network throughout; nothing here
touches a real socket.
"""

from __future__ import annotations

import logging
import tomllib
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import httpx
from pydantic import SecretStr

from newsbot.config import Secrets, load_config
from newsbot.useragent import _FALLBACK_VERSION, build_user_agent, warn_if_contact_unset

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
PYPROJECT_PATH = Path(__file__).parent.parent / "pyproject.toml"


def test_installed_version_matches_pyproject():
    # An editable install (`pip install -e . --no-deps`) writes its version
    # metadata once, at install time; it won't notice a later hand-edit of
    # pyproject.toml's own [project].version on its own. This is the
    # tripwire useragent.py's own docstring says doesn't exist: a bump here
    # that never gets an `--upgrade` reinstall to match now fails loudly in
    # CI instead of quietly shipping a stale User-Agent.
    pyproject_version = tomllib.loads(PYPROJECT_PATH.read_text())["project"]["version"]
    assert version("newsbot") == pyproject_version


def test_fallback_version_matches_pyproject():
    # The fallback is only used when the package metadata is missing, which
    # is exactly when nobody would notice it had gone stale.
    pyproject_version = tomllib.loads(PYPROJECT_PATH.read_text())["project"]["version"]
    assert _FALLBACK_VERSION == pyproject_version


def test_user_agent_reports_the_installed_version():
    ua = build_user_agent({})
    assert f"discord-newsbot/{version('newsbot')} " in ua


def _capturing_transport(
    response: httpx.Response | None = None,
) -> tuple[httpx.MockTransport, list]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response or httpx.Response(200, content=b"")

    return httpx.MockTransport(handler), captured


# --- NEWSBOT_CONTACT: whitespace, empty string, very long, non-ASCII ---


def test_contact_whitespace_only_is_treated_as_unset():
    # Blank after trimming means nobody to contact, so it gets the same
    # fallback (and the same startup warning) as an unset variable.
    ua = build_user_agent({"NEWSBOT_CONTACT": "   \t "})
    assert "contact unset" in ua


def test_contact_empty_string_falls_back_to_unset():
    # env.get("NEWSBOT_CONTACT") or _UNSET_CONTACT: an empty string is
    # falsy, so this is the one "set but treated as unset" case.
    ua = build_user_agent({"NEWSBOT_CONTACT": ""})
    assert "contact unset" in ua


def test_contact_very_long_value_is_passed_through_whole():
    long_contact = "https://example.com/" + "a" * 4000
    ua = build_user_agent({"NEWSBOT_CONTACT": long_contact})
    assert long_contact in ua
    assert len(ua) > 4000


def test_contact_non_ascii_value_is_rejected_because_headers_must_be_ascii():
    # httpx refuses a non-ASCII header value (UnicodeEncodeError), which
    # would fail every outbound request; the builder falls back instead.
    ua = build_user_agent({"NEWSBOT_CONTACT": "mailto:owner@ex\u00e4mple.com"})
    assert "contact unset" in ua
    ua.encode("ascii")


def test_contact_surrounding_whitespace_is_trimmed():
    ua = build_user_agent({"NEWSBOT_CONTACT": "  https://example.com/me  "})
    assert "(+https://example.com/me)" in ua


# --- newline/CR injection: the interesting adversarial case ---


def _assert_wire_encoding_accepts(header_value: str) -> None:
    """Drive h11 (httpx's real HTTP/1.1 encoder) to prove the value serializes.

    `httpx.MockTransport` never touches the wire encoder, so it can't tell
    a legal header from one that would fail on a real connection.
    """
    import h11

    conn = h11.Connection(h11.CLIENT)
    conn.send(
        h11.Request(
            method="GET",
            target="/",
            headers=[(b"host", b"example.com"), (b"user-agent", header_value.encode("ascii"))],
        )
    )


def test_contact_with_crlf_is_rejected_before_it_reaches_a_header():
    contact = "evil\r\nX-Injected: header"
    ua = build_user_agent({"NEWSBOT_CONTACT": contact})
    assert "\r" not in ua and "\n" not in ua
    assert "contact unset" in ua
    _assert_wire_encoding_accepts(ua)


def test_contact_with_bare_newline_is_rejected_too():
    ua = build_user_agent({"NEWSBOT_CONTACT": "evil\nX-Injected: header"})
    assert "\n" not in ua
    _assert_wire_encoding_accepts(ua)


def test_rejected_contact_gets_its_own_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.useragent"):
        warn_if_contact_unset({"NEWSBOT_CONTACT": "evil\r\nX: y"})
    assert len(caplog.records) == 1
    assert "can't carry" in caplog.records[0].getMessage()


# --- warning logged exactly once per process-start call, not once per request ---


def test_warn_if_contact_unset_logs_once_per_call_not_accumulating_across_calls(caplog):
    # warn_if_contact_unset has no internal "already warned" state (it's
    # called once from setup_hook and once from the CLI's main, not once
    # per request), so calling it N times logs N warnings, not one. This
    # pins that it's the *caller's* job (one call per process front door)
    # to keep it to one, not the function's.
    with caplog.at_level(logging.WARNING, logger="newsbot.useragent"):
        warn_if_contact_unset({})
        warn_if_contact_unset({})
    assert len(caplog.records) == 2


def test_warn_if_contact_unset_single_call_logs_exactly_one_record(caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.useragent"):
        warn_if_contact_unset({})
    assert len(caplog.records) == 1


# --- all three former hardcoded call sites actually send the built header ---


async def test_rss_collector_call_site_sends_the_user_agent_header():
    from newsbot.collectors.rss import RssCollector
    from newsbot.config import RssSource

    source = RssSource(type="rss", name="Test Feed", url="https://example.com/feed", trust="press")
    transport, captured = _capturing_transport(httpx.Response(200, content=b"<rss></rss>"))
    async with httpx.AsyncClient(transport=transport) as http:
        await RssCollector(source).collect(http)

    assert len(captured) == 1
    assert captured[0].headers["User-Agent"] == build_user_agent()


async def test_check_sources_cli_call_site_sends_the_user_agent_header(monkeypatch, tmp_path):
    # pipeline/run.py's `_run_check_sources_cli` builds its own
    # `httpx.AsyncClient(headers=user_agent_headers())`; patch the module's
    # `httpx.AsyncClient` so the constructed client keeps that header but
    # talks to a MockTransport instead of the network, so the header
    # actually sent on the wire (not just the argument passed in) is what
    # gets asserted on.
    import newsbot.pipeline.run as run_module
    from newsbot.config import SharedRssSource

    transport, captured = _capturing_transport(httpx.Response(200, content=b"<rss></rss>"))
    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(run_module.httpx, "AsyncClient", _patched_async_client)

    cfg = load_config(CONFIG_PATH)
    source = SharedRssSource(
        type="rss", name="Test Feed", url="https://example.com/feed", trust="press"
    )
    # One source and nothing else, so the one request on the wire is this one.
    cfg = cfg.model_copy(
        update={
            "catalog": [g.model_copy(update={"sources": []}) for g in cfg.catalog],
            "shared_sources": [source],
            "web_search": None,
        }
    )
    code = await run_module._run_check_sources_cli(cfg, str(CONFIG_PATH))

    assert code == 0
    assert len(captured) == 1
    assert captured[0].headers["User-Agent"] == build_user_agent()


async def test_bot_client_setup_hook_call_site_sends_the_user_agent_header(monkeypatch, tmp_path):
    # bot/client.py's setup_hook builds `self.http_client =
    # httpx.AsyncClient(headers=user_agent_headers())`. Same patch trick as
    # above, plus the no-network tree.sync() stub test_client_scheduling_
    # adversarial.py already established as the one acceptable exception to
    # "no gateway mocking" in this suite.
    import newsbot.bot.client as client_module
    from newsbot.store.db import connect, migrate

    transport, captured = _capturing_transport()
    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", _patched_async_client)

    cfg = load_config(CONFIG_PATH)
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    db_path = str(tmp_path / "newsbot.db")
    with closing(connect(db_path)) as conn:
        migrate(conn)

    bot = client_module.NewsBot(cfg, secrets, db_path)

    async def _fake_sync(*args, **kwargs):
        return []

    bot.tree.sync = _fake_sync
    try:
        await bot.setup_hook()
        await bot.http_client.get("https://example.com/ping")
    finally:
        if bot.scheduler is not None:
            bot.scheduler.shutdown(wait=False)
        if bot.http_client is not None:
            await bot.http_client.aclose()

    assert len(captured) == 1
    assert captured[0].headers["User-Agent"] == build_user_agent()
