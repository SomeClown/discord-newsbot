"""End-to-end `run_daily` state-machine edge cases, beyond test_pipeline_integration.py.

Same offline harness as that file (fixture collectors, a stub LLM, a real
temp SQLite file -- nothing here touches a network or an API key). That
file already pins the core happy path and the publish-fails-then-recovers
path; this one goes after the corners the implementer's tests didn't:
a stale `pending` row blocking even a forced run, a collector that raises
instead of returning, and every topic falling back at once still landing
on `partial` rather than `failed`.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.bot.format import to_text
from newsbot.config import load_config
from newsbot.pipeline.publisher import PrintPublisher
from newsbot.pipeline.run import Deps, RunMode, StubLLM, build_fixture_collectors, run_daily
from newsbot.pipeline.summarize import LLMError
from newsbot.store import repo
from newsbot.store.db import connect, migrate

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "integration"
CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)  # 02:00 America/Los_Angeles the same day
RUN_DATE_LOCAL = datetime(2026, 9, 23, 2, 0).date()  # what _local_run_date resolves NOW to


async def _no_sleep(_seconds: float) -> None:
    return None


def _row_counts(db_path: str) -> tuple[int, int, int, int]:
    with closing(connect(db_path)) as conn:
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        stories = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
        story_items = conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0]
        digests = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    return items, stories, story_items, digests


class _AlwaysFailsLLM:
    """Every topic fails every attempt -- summarize_topic's own fallback kicks in."""

    async def emit_stories(self, system: str, user: str):
        raise LLMError("simulated total outage")


class _ThrowingCollector:
    """A collector whose `collect` raises instead of returning -- a bug in someone
    else's parser, a feed that returns HTML instead of XML, whatever. The
    contract (`collectors/base.py`) is that this must never take the whole
    run down; `_run_one` is supposed to catch it and turn it into an errored
    `CollectorResult`.
    """

    name = "cursed_source"
    source_type = "rss"
    rate_limit_key = None

    async def collect(self, http: httpx.AsyncClient):
        raise RuntimeError("this parser has never worked and today is no exception")


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


def _make_deps(db_path: str, http_client: httpx.AsyncClient, *, llm=None, collectors=None) -> Deps:
    cfg = load_config(CONFIG_PATH)
    alerts: list[str] = []

    async def alert(text: str) -> None:
        alerts.append(text)

    deps = Deps(
        cfg=cfg,
        db_path=db_path,
        http=http_client,
        llm=llm or StubLLM(FIXTURES_DIR / "llm.json"),
        collectors=collectors if collectors is not None else build_fixture_collectors(FIXTURES_DIR),
        now=lambda: NOW,
        alert=alert,
    )
    deps.alerts = alerts  # type: ignore[attr-defined]  # test-only convenience
    return deps


# --- stale pending blocks even a forced auto-run ---


async def test_stale_pending_row_blocks_a_plain_run(db_path, http_client):
    with closing(connect(db_path)) as conn:
        repo.claim_digest(conn, RUN_DATE_LOCAL, force=False)

    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "skipped"
    items, stories, _story_items, digests = _row_counts(db_path)
    assert (items, stories, digests) == (0, 0, 1)  # only the pre-existing pending row


async def test_stale_pending_row_is_reclaimed_with_force(db_path, http_client):
    # Mirrors repo.claim_digest's own contract (test_repo_guard_edge_cases.py),
    # changed deliberately in QA step 20 group 4: force now overrides
    # pending too, not just ok/partial, since the only way run_daily ever
    # sees a stale pending row is a prior crash (the in-process _run_lock
    # already rules out a concurrent live run). Pinned again here at the
    # run_daily level, since that's the boundary an operator running
    # `--force` by hand actually sees.
    with closing(connect(db_path)) as conn:
        repo.claim_digest(conn, RUN_DATE_LOCAL, force=False)

    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(
        deps, PrintPublisher(), mode=RunMode.POST, force=True, sleep=_no_sleep
    )

    assert outcome.status == "ok"
    items, stories, _story_items, digests = _row_counts(db_path)
    assert digests == 1
    assert items > 0


async def test_preview_ignores_a_stale_pending_row_entirely(db_path, http_client):
    # PREVIEW never claims, so a stale pending row left by a crashed real
    # run shouldn't block a preview from rendering.
    with closing(connect(db_path)) as conn:
        repo.claim_digest(conn, RUN_DATE_LOCAL, force=False)

    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.PREVIEW, sleep=_no_sleep)

    assert outcome.status == "ok"
    items, stories, _story_items, digests = _row_counts(db_path)
    assert (items, stories, digests) == (0, 0, 1)  # unchanged: still just the pending row


# --- a throwing collector doesn't sink the run ---


async def test_a_throwing_collector_does_not_fail_the_whole_run(db_path, http_client):
    collectors = [*build_fixture_collectors(FIXTURES_DIR), _ThrowingCollector()]
    deps = _make_deps(db_path, http_client, collectors=collectors)

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert "Patch 1.2 fixes crashes" in to_text(outcome.rendered)
    items, stories, _story_items, digests = _row_counts(db_path)
    assert items == 3  # the good sources' items still made it in
    assert stories == 2
    assert digests == 1


async def test_a_throwing_collector_is_recorded_against_source_health(db_path, http_client):
    collectors = [*build_fixture_collectors(FIXTURES_DIR), _ThrowingCollector()]
    deps = _make_deps(db_path, http_client, collectors=collectors)

    await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, last_error FROM source_health WHERE source_name = ?",
            ("cursed_source",),
        ).fetchone()
    assert row is not None
    assert row["consecutive_failures"] == 1
    assert "this parser has never worked" in row["last_error"]


# --- every topic falling back still lands on partial, not failed ---


async def test_every_topic_falling_back_gives_partial_not_failed(db_path, http_client, monkeypatch):
    monkeypatch.setattr("newsbot.pipeline.summarize._BACKOFF_S", (0.0, 0.0, 0.0))
    deps = _make_deps(db_path, http_client, llm=_AlwaysFailsLLM())

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "partial"
    text = to_text(outcome.rendered)
    assert text.count("Summary unavailable") == 2  # borderlands4 and palworld both fell back

    items, stories, _story_items, digests = _row_counts(db_path)
    assert items == 3  # still saved for dedupe purposes even though summarizing failed
    assert stories == 0
    assert digests == 1
    with closing(connect(db_path)) as conn:
        status = conn.execute("SELECT status FROM digests").fetchone()[0]
    assert status == "partial"


# --- an unanticipated exception after the claim still marks the row failed ---
#
# build_digest failing and publish failing after retries both already had
# their own try/except that marks the row failed and returns a normal
# PipelineOutcome. This is the case QA flagged that neither of those
# covers: something else goes wrong after the claim (a bug, a publisher
# that raises something other than PublishError, a cancellation) and
# nothing catches it -- leaving a bare `pending` row that blocks every
# future run and every future admin forever, since nothing ever calls
# mark_digest_failed for it.


class _RaisesUnexpectedly:
    """A publisher that raises a plain RuntimeError instead of PublishError.

    Simulates a bug, or any exception type `_publish_with_retry` was never
    told to expect -- the retry loop only catches `PublishError`, so this
    is exactly the shape of thing that used to escape `_run_post` entirely.
    """

    async def publish(self, r):
        raise RuntimeError("the publisher itself has a bug")


async def test_unexpected_exception_after_claim_marks_the_row_failed_not_pending(
    db_path, http_client
):
    deps = _make_deps(db_path, http_client)

    with pytest.raises(RuntimeError, match="the publisher itself has a bug"):
        await run_daily(deps, _RaisesUnexpectedly(), mode=RunMode.POST, sleep=_no_sleep)

    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM digests WHERE run_date = ?", (RUN_DATE_LOCAL.isoformat(),)
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"


class _RaisesCancelled:
    """Simulates the run being cancelled mid-publish (e.g. process shutdown)."""

    async def publish(self, r):
        raise asyncio.CancelledError()


async def test_cancellation_after_claim_marks_the_row_failed_not_pending(db_path, http_client):
    deps = _make_deps(db_path, http_client)

    with pytest.raises(asyncio.CancelledError):
        await run_daily(deps, _RaisesCancelled(), mode=RunMode.POST, sleep=_no_sleep)

    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM digests WHERE run_date = ?", (RUN_DATE_LOCAL.isoformat(),)
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
