"""Integration tests for the admin-channel run report hook in `run_daily` (design.md §6).

Same offline harness as test_pipeline_integration.py -- fixture collectors,
a stub LLM, a real temp SQLite file. What's new here is `deps.run_kind`:
`None` (every existing test's default) means "no report", so none of
those tests needed to change; these exercise the report path itself.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.config import load_config
from newsbot.pipeline.publisher import PrintPublisher, PublishError
from newsbot.pipeline.run import (
    Deps,
    RunKind,
    RunMode,
    StubLLM,
    build_fixture_collectors,
    run_daily,
)
from newsbot.pipeline.summarize import LLMError
from newsbot.store.db import connect, migrate

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "integration"
CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)  # 02:00 America/Los_Angeles the same day


async def _no_sleep(_seconds: float) -> None:
    return None


class _AlwaysFailsPublisher:
    async def publish(self, r) -> list[int]:
        raise PublishError("simulated publish failure")


class _AlwaysFailsLLM:
    async def emit_stories(self, system: str, user: str):
        raise LLMError("simulated total outage")


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _make_deps(
    db_path: str,
    http_client: httpx.AsyncClient,
    *,
    llm=None,
    run_kind=RunKind.SCHEDULED,
    report_to_admin: bool | None = None,
) -> Deps:
    cfg = load_config(CONFIG_PATH)
    if report_to_admin is not None:
        cfg = cfg.model_copy(
            update={"digest": cfg.digest.model_copy(update={"report_to_admin": report_to_admin})}
        )
    alerts: list[str] = []

    async def alert(text: str) -> None:
        alerts.append(text)

    deps = Deps(
        cfg=cfg,
        db_path=db_path,
        http=http_client,
        llm=llm or StubLLM(FIXTURES_DIR / "llm.json"),
        collectors=build_fixture_collectors(FIXTURES_DIR),
        now=lambda: NOW,
        alert=alert,
        run_kind=run_kind,
    )
    deps.alerts = alerts  # type: ignore[attr-defined]  # test-only convenience
    return deps


# --- sent once on ok / partial ---


async def test_report_sent_once_on_a_successful_post(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    reports = [a for a in deps.alerts if "Digest posted" in a]
    assert len(reports) == 1
    assert reports[0].startswith("✅ **Digest posted** ·")
    assert "(scheduled)" in reports[0]


async def test_report_sent_once_on_a_partial_post(db_path, http_client, monkeypatch):
    monkeypatch.setattr("newsbot.pipeline.summarize._BACKOFF_S", (0.0, 0.0, 0.0))
    deps = _make_deps(db_path, http_client, llm=_AlwaysFailsLLM())

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "partial"
    reports = [a for a in deps.alerts if "Digest posted" in a]
    assert len(reports) == 1
    assert reports[0].startswith("⚠️ **Digest posted with gaps** ·")


async def test_report_reflects_the_run_kind_passed_on_deps(db_path, http_client):
    deps = _make_deps(db_path, http_client, run_kind=RunKind.RUN_NOW)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    reports = [a for a in deps.alerts if "Digest posted" in a]
    assert "(run-now)" in reports[0]


# --- never on failed / skipped / preview ---


async def test_report_never_sent_on_a_failed_publish(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, _AlwaysFailsPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "failed"
    assert not any("Digest posted" in a for a in deps.alerts)
    # the existing failure alert still fires -- this feature doesn't touch it
    assert any("publish failed" in a for a in deps.alerts)


async def test_report_never_sent_on_a_skipped_run(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    first = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert first.status == "ok"
    deps.alerts.clear()  # type: ignore[attr-defined]

    second = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert second.status == "skipped"
    assert not any("Digest posted" in a for a in deps.alerts)


async def test_report_never_sent_in_preview_mode(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.PREVIEW, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert not any("Digest posted" in a for a in deps.alerts)


async def test_report_never_sent_when_run_kind_is_not_set(db_path, http_client):
    # The default every pre-existing caller (and every pre-existing test)
    # gets: no run_kind means no report, full stop.
    deps = _make_deps(db_path, http_client, run_kind=None)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert not any("Digest posted" in a for a in deps.alerts)


# --- disabled via config ---


async def test_report_disabled_via_config_sends_nothing(db_path, http_client):
    deps = _make_deps(db_path, http_client, report_to_admin=False)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert not any("Digest posted" in a for a in deps.alerts)


# --- an exception building/sending the report never changes the outcome ---


async def test_broken_report_rendering_does_not_change_the_digest_outcome(
    db_path, http_client, monkeypatch
):
    monkeypatch.setattr(
        "newsbot.pipeline.run.render_run_report",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("report renderer is on fire")),
    )
    deps = _make_deps(db_path, http_client)

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"  # unaffected by the reporting bug
    assert not any("Digest posted" in a for a in deps.alerts)


async def test_broken_alert_send_during_reporting_does_not_change_the_digest_outcome(
    db_path, http_client
):
    cfg = load_config(CONFIG_PATH)

    async def _broken_alert(text: str) -> None:
        raise RuntimeError("discord is down")

    deps = Deps(
        cfg=cfg,
        db_path=db_path,
        http=http_client,
        llm=StubLLM(FIXTURES_DIR / "llm.json"),
        collectors=build_fixture_collectors(FIXTURES_DIR),
        now=lambda: NOW,
        alert=_broken_alert,
        run_kind=RunKind.SCHEDULED,
    )

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"  # unaffected by the broken alert path
