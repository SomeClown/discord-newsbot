"""End-to-end pipeline tests: fixture feeds, a stub LLM, a real temp SQLite file.

Nothing here touches a network or an API key -- `run.build_fixture_collectors`
reads `tests/fixtures/integration/*.json` instead of the real collectors, and
`run.StubLLM` reads `tests/fixtures/integration/llm.json` instead of calling
Claude. What *is* real: the guard, the save transaction, the retry loop and
SQLite itself. This is the same offline path `python -m newsbot.pipeline.run
--dry-run --fixtures ... --stub-llm ...` exercises by hand.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.bot.format import to_text
from newsbot.config import load_config
from newsbot.pipeline.publisher import PrintPublisher, PublishError
from newsbot.pipeline.run import Deps, RunMode, build_fixture_collectors, run_daily
from newsbot.pipeline.summarize import LLMError
from newsbot.store.db import connect, migrate

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "integration"
CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)  # 02:00 America/Los_Angeles the same day


async def _no_sleep(_seconds: float) -> None:
    return None


def _row_counts(db_path: str) -> tuple[int, int, int, int]:
    with closing(connect(db_path)) as conn:
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        stories = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
        story_items = conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0]
        digests = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    return items, stories, story_items, digests


class _StaticLLM:
    """Wraps a working `LLMClient`, forcing one topic to always fail.

    Used only for the "a fallback topic gives partial" case -- the JSON
    `--stub-llm` format has no way to express "and then fail three times",
    so this is assembled in Python instead.
    """

    def __init__(self, working, fail_topic_key: str) -> None:
        self._working = working
        self._fail_topic_key = fail_topic_key

    async def emit_stories(self, system: str, user: str):
        if f"(key: {self._fail_topic_key})" in user:
            raise LLMError("simulated failure")
        return await self._working.emit_stories(system, user)


class _AlwaysFailsPublisher:
    async def publish(self, r) -> list[int]:
        raise PublishError("simulated publish failure")


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


def _make_deps(db_path: str, http_client: httpx.AsyncClient, *, llm=None) -> Deps:
    from newsbot.pipeline.run import StubLLM

    cfg = load_config(CONFIG_PATH)
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
    )
    deps.alerts = alerts  # type: ignore[attr-defined]  # test-only convenience
    return deps


# --- POST mode: save, guard, rendering ---


async def test_post_run_saves_items_stories_and_ok_status(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert "Patch 1.2 fixes crashes" in to_text(outcome.rendered)
    assert "Palworld 0.6.2 fixes crash bugs" in to_text(outcome.rendered)

    items, stories, story_items, digests = _row_counts(db_path)
    assert items == 3  # 2 borderlands4 items + 1 palworld item; diablo4 has none
    assert stories == 2
    assert story_items == 2
    assert digests == 1

    with closing(connect(db_path)) as conn:
        status = conn.execute("SELECT status FROM digests").fetchone()[0]
    assert status == "ok"


async def test_second_run_same_day_is_skipped(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    first = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert first.status == "ok"

    second = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert second.status == "skipped"

    # Nothing extra got written on the skipped attempt.
    items, stories, _story_items, digests = _row_counts(db_path)
    assert items == 3
    assert stories == 2
    assert digests == 1


async def test_force_replaces_row_and_finds_zero_new_items(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    first = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert first.status == "ok"

    # Same fixture URLs again: normalize() drops them as already-known, so
    # this run collects nothing new even though force=True lets it reclaim
    # today's slot.
    second = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, force=True, sleep=_no_sleep)
    assert second.status == "ok"
    assert "No new stories today." in to_text(second.rendered)

    items, stories, _story_items, digests = _row_counts(db_path)
    assert items == 3  # unchanged: nothing new to insert
    assert stories == 2  # the previous stories are untouched by this run
    assert digests == 1  # force updates the same row in place


async def test_publisher_failing_gives_failed_with_nothing_saved_then_rerun_succeeds(
    db_path, http_client
):
    deps = _make_deps(db_path, http_client)
    outcome = await run_daily(deps, _AlwaysFailsPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert outcome.status == "failed"

    items, stories, story_items, digests = _row_counts(db_path)
    assert (items, stories, story_items) == (0, 0, 0)
    assert digests == 1  # the claimed row exists, marked failed
    with closing(connect(db_path)) as conn:
        status = conn.execute("SELECT status FROM digests").fetchone()[0]
    assert status == "failed"

    # A failed digest doesn't need `force` to reclaim -- the guard only
    # blocks ok/partial/pending.
    rerun = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert rerun.status == "ok"
    items, stories, _story_items, digests = _row_counts(db_path)
    assert items == 3
    assert stories == 2
    assert digests == 1  # same row, reclaimed rather than duplicated


# --- PREVIEW mode ---


async def test_preview_writes_nothing(db_path, http_client):
    deps = _make_deps(db_path, http_client)
    before = _row_counts(db_path)

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.PREVIEW, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert "Patch 1.2 fixes crashes" in to_text(outcome.rendered)
    assert _row_counts(db_path) == before == (0, 0, 0, 0)


# --- fallback -> partial ---


async def test_fallback_topic_gives_partial(db_path, http_client, monkeypatch):
    # summarize_topic's own retry backoff would otherwise add several real
    # seconds to this test for a failure path we already unit-test in
    # test_summarize.py; zeroing it here keeps the wait itself untested
    # (that's summarize's job) while still exercising three real attempts.
    monkeypatch.setattr("newsbot.pipeline.summarize._BACKOFF_S", (0.0, 0.0, 0.0))

    from newsbot.pipeline.run import StubLLM

    working = StubLLM(FIXTURES_DIR / "llm.json")
    deps = _make_deps(db_path, http_client, llm=_StaticLLM(working, fail_topic_key="palworld"))

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "partial"
    text = to_text(outcome.rendered)
    assert "Patch 1.2 fixes crashes" in text  # borderlands4 still summarized fine
    assert "Summary unavailable" in text  # palworld fell back

    items, stories, _story_items, _digests = _row_counts(db_path)
    assert items == 3  # the palworld item is still saved, for dedupe (SPEC-DEV 9)
    assert stories == 1  # only borderlands4 produced a story
