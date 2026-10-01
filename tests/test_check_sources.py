"""Tests for `--check-sources` (self-host plan task 3).

httpx.MockTransport stands in for the network throughout: nothing here
touches a real socket, or a real Anthropic/Discord credential.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from newsbot.config import (
    _SHARED_BY_TYPE,
    AppConfig,
    CollectionCfg,
    GameCfg,
    RssSource,
    SteamSource,
    Topic,
    WebSearchCfg,
    WebSearchSource,
    count_configured_web_search_sources,
    load_check_sources_secrets,
)
from newsbot.pipeline.run import (
    CheckSourcesReport,
    _run_check_sources_cli,
    _web_search_note,
    main,
    render_check_sources_report,
    run_check_sources,
)

RSS_FIXTURE = (Path(__file__).parent / "fixtures" / "feeds" / "rss20_sample.xml").read_bytes()


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


# --- run_check_sources: real collector, fake transport ---


async def test_run_check_sources_counts_items_and_matches_topic():
    source = RssSource(
        type="rss", name="Palworld Blog", url="https://example.com/feed", trust="press"
    )
    cfg = _cfg([source])
    transport = _transport({"https://example.com/feed": httpx.Response(200, content=RSS_FIXTURE)})
    async with httpx.AsyncClient(transport=transport) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)

    assert len(report.results) == 1
    assert report.results[0].source_name == "Palworld Blog"
    assert report.results[0].error is None
    assert report.topic_counts["palworld"] >= 1  # the fixture's Palworld item matches


async def test_run_check_sources_records_first_error_line_on_failure():
    source = SteamSource(type="steam_news", name="Broken Steam", app_id=1, trust="official")
    cfg = _cfg([source])
    transport = _transport(
        {
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/": httpx.Response(
                500, text="server exploded"
            )
        }
    )
    async with httpx.AsyncClient(transport=transport) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)

    assert len(report.results) == 1
    assert report.results[0].error is not None
    assert report.topic_counts["palworld"] == 0


async def test_run_check_sources_never_calls_out_for_web_search_without_a_key():
    # build_collectors already drops an unkeyed web_search source, so this
    # is really "no request happens", proven by never registering a
    # matching transport route for it.
    source = WebSearchSource(type="web_search", trust="press")
    cfg = _cfg([source])
    async with httpx.AsyncClient(transport=_transport({})) as http:
        report = await run_check_sources(cfg, load_check_sources_secrets({}), http)
    assert report.results == []


# --- render_check_sources_report: pure formatting, no network at all ---


def test_render_report_lists_each_source_with_counts_and_first_error():
    from newsbot.collectors.base import CollectorResult, RawItem

    ok_item = RawItem(
        url="https://example.com/a",
        title="Palworld patch notes",
        excerpt="",
        source_name="Palworld Blog",
        trust="press",
        published_at=None,
    )
    results = [
        CollectorResult("Palworld Blog", "rss", [ok_item]),
        CollectorResult("Broken Steam", "steam_news", [], error="boom\nsecond line"),
    ]
    cfg = _cfg([])
    report = CheckSourcesReport(results=results, topic_counts={"palworld": 1})
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert "Palworld Blog" in text
    assert "Broken Steam" in text
    assert "boom" in text
    assert "second line" not in text  # only the first error line is shown
    assert "palworld (Palworld): 1 item(s)" in text
    assert "Summary: 1 source(s) ok, 1 failed, 0 skipped" in text


def test_render_report_includes_web_search_note_when_given():
    cfg = _cfg([])
    report = CheckSourcesReport(results=[], topic_counts={"palworld": 0})
    text = render_check_sources_report(
        cfg,
        report,
        web_search_note="web_search: 1 source(s) configured but BRAVE_API_KEY is not set; skipped",
    )
    assert "BRAVE_API_KEY is not set; skipped" in text


def test_render_report_counts_unkeyed_web_search_in_the_summary():
    # An unkeyed web_search source never becomes a CollectorResult (it's
    # dropped before run_check_sources builds a collector at all), so the
    # summary's "N skipped" has to be told about it separately, or it reads
    # "0 skipped" right under a table note saying otherwise.
    cfg = _cfg([])
    report = CheckSourcesReport(results=[], topic_counts={"palworld": 0})
    text = render_check_sources_report(
        cfg,
        report,
        web_search_note="web_search: 1 source(s) configured but BRAVE_API_KEY is not set; skipped",
        web_search_skipped=1,
    )
    assert "Summary: 0 source(s) ok, 0 failed, 1 skipped" in text


def test_render_report_handles_no_sources_configured():
    cfg = _cfg([])
    report = CheckSourcesReport(results=[], topic_counts={"palworld": 0})
    text = render_check_sources_report(cfg, report, web_search_note=None)
    assert "(no sources configured)" in text


# --- _web_search_note ---


def test_web_search_note_is_none_when_not_configured(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sources: []\n")
    cfg = _cfg([])
    secrets = load_check_sources_secrets({})
    assert _web_search_note(cfg, secrets, str(path)) is None


def test_web_search_note_flags_configured_but_unkeyed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sources:\n  - type: web_search\n    trust: press\n")
    cfg = _cfg([])
    secrets = load_check_sources_secrets({})
    note = _web_search_note(cfg, secrets, str(path))
    assert note is not None
    assert "BRAVE_API_KEY is not set" in note


def test_web_search_note_is_none_when_configured_and_keyed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sources:\n  - type: web_search\n    trust: press\n")
    cfg = _cfg([])
    secrets = load_check_sources_secrets({"BRAVE_API_KEY": "a-key"})
    assert _web_search_note(cfg, secrets, str(path)) is None


def test_count_configured_web_search_sources(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "sources:\n"
        "  - type: web_search\n    trust: press\n"
        "  - type: rss\n    name: x\n    url: https://x.example/feed\n    trust: press\n"
    )
    assert count_configured_web_search_sources(path) == 1


# --- CLI: argparse wiring and secrets gating ---


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


def test_check_sources_flag_never_requires_anthropic_or_discord_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_MINIMAL_CONFIG)

    code = main(["--config", str(config_path), "--check-sources"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Summary:" in out


async def test_check_sources_cli_prints_report_for_a_config_with_no_sources(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_MINIMAL_CONFIG)
    from newsbot.config import load_config

    cfg = load_config(config_path)
    code = await _run_check_sources_cli(cfg, str(config_path))
    assert code == 0
    out = capsys.readouterr().out
    assert "(no sources configured)" in out
    assert "palworld (Palworld): 0 item(s)" in out


async def test_check_sources_cli_counts_unkeyed_web_search_as_skipped(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        _MINIMAL_CONFIG.replace("sources: []", "sources:\n  - type: web_search\n    trust: press")
    )
    from newsbot.config import load_config

    cfg = load_config(config_path)
    code = await _run_check_sources_cli(cfg, str(config_path))
    assert code == 0
    out = capsys.readouterr().out
    assert "web_search" in out and "skipped" in out
    assert "Summary: 0 source(s) ok, 0 failed, 1 skipped" in out


@pytest.mark.parametrize("flag", ["--fixtures", "--stub-llm"])
def test_check_sources_flag_takes_priority_over_offline_flags(tmp_path, monkeypatch, flag):
    # --check-sources short-circuits before any of --fixtures/--stub-llm/
    # --sweep's own branching, so combining them (a user mistake, not a
    # supported mode) still exits 0 as a check-sources run, not an error
    # about a missing fixtures directory.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_MINIMAL_CONFIG)
    code = main(["--config", str(config_path), "--check-sources", flag, "/nonexistent"])
    assert code == 0
