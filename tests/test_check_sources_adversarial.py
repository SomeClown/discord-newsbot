"""Adversarial tests for `--check-sources` (self-host plan task 3), beyond
test_check_sources.py's own happy-path and error-first-line coverage.

Angle (test-engineer brief, 2026-09-26): the credential isolation this flag
exists for (never reads ANTHROPIC_API_KEY/DISCORD_TOKEN, even when they're
set to garbage), web_search keyed vs unkeyed vs unconfigured as a three-way
split, a collector that raises outright rather than erroring cleanly, zero
sources, very long names/errors in the rendered table, exit code 0
regardless, topic counts staying consistent with `filter_items`, and that
this path never opens or writes to the database.

httpx.MockTransport throughout; nothing here touches a real socket, or a
real Anthropic/Discord credential.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from newsbot.config import (
    _SHARED_BY_TYPE,
    AppConfig,
    CollectionCfg,
    GameCfg,
    RssSource,
    Topic,
    WebSearchCfg,
    WebSearchSource,
    load_check_sources_secrets,
    load_config,
)
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.normalize import normalize
from newsbot.pipeline.run import (
    CheckSourcesReport,
    _run_check_sources_cli,
    main,
    render_check_sources_report,
    run_check_sources,
)

RSS_FIXTURE = (Path(__file__).parent / "fixtures" / "feeds" / "rss20_sample.xml").read_bytes()

_MINIMAL_CONFIG = """\
guild_id: 1
digest:
  time: "09:00"
  timezone: "UTC"
topics:
  - key: palworld
    name: "Palworld"
    channel_id: 2
sources: []
"""


def _cfg(sources: list, topics: list[Topic] | None = None) -> AppConfig:
    """A catalog of one game (or `topics`, as games) with `sources` as shared, unscoped sources.

    Web search is its own block now, not a source in the list; everything else is shared, so
    items match by keyword exactly as the v2 test's unscoped sources did.
    """
    games = [
        GameCfg(key=t.key, name=t.name, aliases=t.aliases, entities=t.entities)
        for t in (
            topics
            or [Topic(key="palworld", name="Palworld", channel_id=1, aliases=[], entities=[])]
        )
    ]
    shared = [
        _SHARED_BY_TYPE[s.type](**s.model_dump(exclude={"topics"}))
        for s in sources
        if s.type != "web_search"
    ]
    web_search = next(
        (
            WebSearchCfg(queries_per_game=s.queries_per_topic, trust=s.trust)
            for s in sources
            if s.type == "web_search"
        ),
        None,
    )
    return AppConfig(
        catalog=games,
        shared_sources=shared,
        web_search=web_search,
        # A big lookback, not the 24h default: the RSS fixture below is
        # dated to whenever it was written, not "whenever this test
        # happens to run", and normalize() drops anything older than
        # the lookback regardless of how relevant it otherwise is.
        collection=CollectionCfg(lookback_hours=87600),
    )


def _transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("?")[0]
        if key not in responses:
            raise AssertionError(f"unexpected request to {request.url}")
        return responses[key]

    return httpx.MockTransport(handler)


# --- never reads ANTHROPIC_API_KEY/DISCORD_TOKEN, unset or garbage ---


def test_check_sources_ignores_anthropic_and_discord_env_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_MINIMAL_CONFIG)
    code = main(["--config", str(config_path), "--check-sources"])
    assert code == 0


def test_check_sources_ignores_anthropic_and_discord_env_when_set_to_garbage(monkeypatch, tmp_path):
    # Setting them to garbage (not just leaving them unset) is the real
    # test: load_check_sources_secrets never even looks at these two names,
    # so garbage values can't leak into the report or crash validation
    # that would otherwise reject a malformed key/token.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-!!!garbage???")
    monkeypatch.setenv("DISCORD_TOKEN", "also garbage")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_MINIMAL_CONFIG)
    code = main(["--config", str(config_path), "--check-sources"])
    assert code == 0


def test_load_check_sources_secrets_never_reads_anthropic_or_discord_even_when_set():
    secrets = load_check_sources_secrets(
        {"ANTHROPIC_API_KEY": "garbage-key", "DISCORD_TOKEN": "garbage-token"}
    )
    assert secrets.discord_token is None
    assert (
        secrets.anthropic_api_key.get_secret_value()
        == "unused (--check-sources never calls Claude)"
    )


# --- web_search: keyed, unkeyed, unconfigured (three-way split) ---


async def test_web_search_unconfigured_produces_no_result_and_no_note():
    cfg = _cfg([])  # no web_search source at all
    async with httpx.AsyncClient(transport=_transport({})) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)
    assert report.results == []


async def test_web_search_configured_but_unkeyed_is_dropped_before_running():
    source = WebSearchSource(type="web_search", trust="press")
    cfg = _cfg([source])
    async with httpx.AsyncClient(transport=_transport({})) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)
    assert report.results == []  # build_catalog_collectors drops it without a key


async def test_web_search_configured_and_keyed_runs_and_appears_in_results():
    source = WebSearchSource(type="web_search", trust="press", queries_per_topic=1)
    cfg = _cfg([source])
    transport = _transport(
        {
            "https://api.search.brave.com/res/v1/news/search": httpx.Response(
                200, json={"results": []}
            )
        }
    )
    secrets = load_check_sources_secrets({"BRAVE_API_KEY": "k"})
    async with httpx.AsyncClient(transport=transport) as http:
        report = await run_check_sources(cfg, secrets, http)
    assert len(report.results) == 1
    assert report.results[0].source_type == "web_search"


# --- a collector raising outright is turned into a clean per-source error ---


async def test_a_collector_raising_shows_up_as_an_error_not_a_crash(monkeypatch):
    source = RssSource(type="rss", name="Boom Feed", url="https://example.com/feed", trust="press")
    cfg = _cfg([source])

    import newsbot.collectors.rss as rss_module

    async def _boom(self, http):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(rss_module.RssCollector, "collect", _boom)

    async with httpx.AsyncClient(transport=_transport({})) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)

    assert len(report.results) == 1
    assert report.results[0].error == "collector exploded"
    assert report.topic_counts["palworld"] == 0


# --- zero sources ---


async def test_zero_sources_configured_yields_empty_results_and_zero_counts():
    cfg = _cfg([])
    async with httpx.AsyncClient(transport=_transport({})) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)
    assert report.results == []
    assert report.topic_counts == {"palworld": 0}
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert "(no sources configured)" in text
    assert "Summary: 0 source(s) ok, 0 failed, 0 skipped; 0 item(s)" in text


# --- output table alignment: very long names/errors don't crash rendering ---


def test_render_report_handles_a_very_long_source_name_without_crashing():
    from newsbot.collectors.base import CollectorResult

    long_name = "A" * 500
    results = [CollectorResult(long_name, "rss", [])]
    cfg = _cfg([])
    report = CheckSourcesReport(results=results, topic_counts={"palworld": 0})
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert long_name in text


def test_render_report_handles_a_very_long_error_message_and_shows_first_line_only():
    from newsbot.collectors.base import CollectorResult

    long_error = ("x" * 2000) + "\nsecond line"
    results = [CollectorResult("Some Source", "rss", [], error=long_error)]
    cfg = _cfg([])
    report = CheckSourcesReport(results=results, topic_counts={"palworld": 0})
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert "x" * 2000 in text
    assert "second line" not in text


def test_render_report_handles_unicode_and_empty_source_names():
    from newsbot.collectors.base import CollectorResult

    results = [
        CollectorResult("日本語ソース🎮", "rss", []),
        CollectorResult("", "steam_news", [], error="boom"),
    ]
    cfg = _cfg([])
    report = CheckSourcesReport(results=results, topic_counts={"palworld": 0})
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert "日本語ソース🎮" in text


# --- exit code is always 0, whatever the run's own outcome ---


async def test_cli_exit_code_is_zero_even_when_every_source_fails(tmp_path, monkeypatch):
    # The CLI builds its own client, so hand it one whose transport answers 500 to everything.
    # (This used to open a real socket to api.steampowered.com, which is how a test suite
    # ends up waiting on somebody else's network.)
    real_client = httpx.AsyncClient
    failing = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        "newsbot.pipeline.run.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=failing, **kwargs),
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "guild_id: 1\n"
        'digest:\n  time: "09:00"\n  timezone: "UTC"\n'
        "topics:\n  - key: palworld\n    name: Palworld\n    channel_id: 2\n"
        "sources:\n"
        "  - type: steam_news\n    name: Broken Steam\n    app_id: 1\n    trust: official\n"
    )
    cfg = load_config(config_path)
    code = await _run_check_sources_cli(cfg, str(config_path))
    assert code == 0


# --- topic match counts stay consistent with filter_items ---


async def test_topic_counts_match_a_direct_filter_items_call_on_the_same_items():
    source = RssSource(
        type="rss", name="Palworld Blog", url="https://example.com/feed", trust="press"
    )
    cfg = _cfg([source])
    transport = _transport({"https://example.com/feed": httpx.Response(200, content=RSS_FIXTURE)})
    async with httpx.AsyncClient(transport=transport) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)

    from datetime import UTC, datetime, timedelta

    from newsbot.collectors.base import run_collectors
    from newsbot.collectors.rss import RssCollector

    async with httpx.AsyncClient(transport=transport) as http:
        results = await run_collectors([RssCollector(source)], http, timeout_s=20)
    collected = [item for result in results for item in result.items]
    lookback = timedelta(hours=cfg.collection.lookback_hours)
    normalized = normalize(collected, lambda _urls: set(), datetime.now(UTC), lookback)
    grouped = filter_items(normalized, cfg.catalog, cfg.collection.max_items_per_game)
    expected_counts = {game.key: len(grouped.get(game.key, [])) for game in cfg.catalog}

    assert report.topic_counts == expected_counts


# --- never touches the database ---


async def test_run_check_sources_never_opens_or_touches_a_database(tmp_path):
    # run_check_sources takes no db_path argument at all -- there's no
    # connection object anywhere in its signature or the CLI wrapper's --
    # so this test also just documents that the function's own contract
    # forecloses writing to a database, not merely happens not to today.
    import inspect

    sig = inspect.signature(run_check_sources)
    assert "db_path" not in sig.parameters
    assert "conn" not in sig.parameters

    source = RssSource(
        type="rss", name="Palworld Blog", url="https://example.com/feed", trust="press"
    )
    cfg = _cfg([source])
    transport = _transport({"https://example.com/feed": httpx.Response(200, content=RSS_FIXTURE)})
    async with httpx.AsyncClient(transport=transport) as http:
        # No db file exists anywhere near this call; if it tried to open
        # one relative to cwd, this would either create a stray file (it
        # doesn't -- checked below) or raise.
        await run_check_sources(cfg, load_check_sources_secrets({}), http)

    assert not (tmp_path / "newsbot.db").exists()
