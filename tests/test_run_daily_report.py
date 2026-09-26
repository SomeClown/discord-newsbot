"""Integration tests for the admin-channel run report hook in `run_daily` (design.md §6).

Same offline harness as test_pipeline_integration.py -- fixture collectors,
a stub LLM, a real temp SQLite file. What's new here is `deps.run_kind`:
`None` (every existing test's default) means "no report", so none of
those tests needed to change; these exercise the report path itself.
"""

from __future__ import annotations

import asyncio
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
    local_run_date,
    run_daily,
)
from newsbot.pipeline.summarize import LLMError
from newsbot.store.db import connect, migrate
from newsbot.store.repo import get_digest

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


# --- a bug the report can't be allowed to have: CancelledError during reporting ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: _maybe_send_run_report only catches `except Exception`, so a "
        "CancelledError raised while building/sending the report escapes it, "
        "hits _run_post's `except BaseException`, and that handler "
        "unconditionally marks the digest_id row `failed` even though "
        "save_run already committed it `ok` -- see newsbot/pipeline/run.py "
        "_maybe_send_run_report and _run_post's outer except BaseException."
    ),
)
async def test_cancelled_error_during_report_rendering_does_not_flip_a_saved_ok_digest_to_failed(
    db_path, http_client, monkeypatch
):
    """Pins design.md §8's promise for the one exception `except Exception:` can't catch.

    `_maybe_send_run_report` only wraps `render_run_report`/`deps.alert` in
    `except Exception:` (pipeline/run.py). `asyncio.CancelledError` is a
    `BaseException`, not an `Exception`, since Python 3.8 -- it sails
    straight past that guard, out of `_run_claimed`, and into
    `_run_post`'s own `except BaseException`, which unconditionally marks
    the *already-saved* digest row `failed`. That's exactly the outcome
    design.md §8 says must never happen: "a bug in the report must never
    change the digest's already-recorded status." See
    newsbot/pipeline/run.py's `_maybe_send_run_report` (needs `except
    BaseException` or an explicit `except asyncio.CancelledError: raise`
    placed *before* anything that could still mutate `digest_id`'s row).
    """
    monkeypatch.setattr(
        "newsbot.pipeline.run.render_run_report",
        lambda **kwargs: (_ for _ in ()).throw(asyncio.CancelledError()),
    )
    cfg = load_config(CONFIG_PATH)
    deps = _make_deps(db_path, http_client)

    with pytest.raises(asyncio.CancelledError):
        await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    with closing(connect(db_path)) as conn:
        row = get_digest(conn, local_run_date(NOW, cfg.digest.timezone))
    assert row is not None
    assert row.status == "ok"


# --- ordering: the report is sent strictly after the digest row is saved ---


async def test_report_is_sent_only_after_the_digest_row_is_already_saved(db_path, http_client):
    cfg = load_config(CONFIG_PATH)
    run_date = local_run_date(NOW, cfg.digest.timezone)
    seen_statuses: list[str | None] = []

    async def alert(text: str) -> None:
        if "Digest posted" not in text:
            return
        # Read the DB from a *separate* connection at the moment the
        # report fires -- if the report were ever sent before save_run's
        # transaction committed, this would see `pending` (or nothing),
        # not the finished row.
        with closing(connect(db_path)) as conn:
            row = get_digest(conn, run_date)
        seen_statuses.append(row.status if row else None)

    deps = Deps(
        cfg=cfg,
        db_path=db_path,
        http=http_client,
        llm=StubLLM(FIXTURES_DIR / "llm.json"),
        collectors=build_fixture_collectors(FIXTURES_DIR),
        now=lambda: NOW,
        alert=alert,
        run_kind=RunKind.SCHEDULED,
    )

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert seen_statuses == ["ok"]


# --- force re-run via run-now reports again with the new outcome ---


async def test_force_rerun_sends_a_second_report_reflecting_the_new_outcome(
    db_path, http_client, monkeypatch
):
    deps = _make_deps(db_path, http_client, run_kind=RunKind.RUN_NOW)
    first = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert first.status == "ok"
    first_reports = [a for a in deps.alerts if "Digest posted" in a]  # type: ignore[attr-defined]
    assert len(first_reports) == 1

    # Second run: publish fails after retries this time (a different
    # outcome from the first), forced past the existing `ok` row the way
    # `/newsbot run-now`'s confirm dialog would.
    monkeypatch.setattr("newsbot.pipeline.run._PUBLISH_BACKOFF_S", ())
    second = await run_daily(
        deps, _AlwaysFailsPublisher(), mode=RunMode.POST, force=True, sleep=_no_sleep
    )

    assert second.status == "failed"
    second_reports = [a for a in deps.alerts if "Digest posted" in a]  # type: ignore[attr-defined]
    # A failed run gets no report of its own (design.md: failed/skipped
    # send no report) -- so forcing a re-run that turns out worse must
    # *not* add a second "Digest posted" message on top of the first.
    assert len(second_reports) == 1

    # Now force a third run that succeeds again: this is the case the
    # task actually asks about -- force re-run reporting again with a
    # *new* ok outcome, not just re-showing the first one.
    third = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, force=True, sleep=_no_sleep)
    assert third.status == "ok"
    third_reports = [a for a in deps.alerts if "Digest posted" in a]  # type: ignore[attr-defined]
    assert len(third_reports) == 2  # the original ok report, plus this new one
