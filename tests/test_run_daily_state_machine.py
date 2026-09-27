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
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import discord
import httpx
import pytest

from newsbot.bot.client import DiscordPublisher
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


class _RaisesUnexpectedlyAfterPartialPublish:
    """A publisher that partially posts, then blows up with something other than
    PublishError -- the exact shape `_run_post`'s `getattr(exc, "posted_ids", ...)`
    fallback exists for. Whatever ids it carries on the exception must actually
    make it into the failed row, not just the row's status.
    """

    def __init__(self):
        self.posted_ids = [111, 222]

    async def publish(self, r):
        raise RuntimeError("partial post, then a bug") from None


async def test_unexpected_exception_posted_ids_are_persisted_on_the_failed_row(
    db_path, http_client
):
    publisher = _RaisesUnexpectedlyAfterPartialPublish()

    class _Wrapper:
        async def publish(self, r):
            try:
                await publisher.publish(r)
            except RuntimeError as exc:
                exc.posted_ids = publisher.posted_ids
                raise

    deps = _make_deps(db_path, http_client)

    with pytest.raises(RuntimeError):
        await run_daily(deps, _Wrapper(), mode=RunMode.POST, sleep=_no_sleep)

    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status, posted_message_ids FROM digests WHERE run_date = ?",
            (RUN_DATE_LOCAL.isoformat(),),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert json.loads(row["posted_message_ids"]) == [111, 222]


async def test_normal_success_is_unaffected_by_the_baseexception_safety_net(db_path, http_client):
    # The outer `except BaseException` in `_run_post` must never catch and
    # swallow a normal successful run -- it should only ever see something
    # that escapes `_run_claimed`'s own handling.
    deps = _make_deps(db_path, http_client)

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM digests WHERE run_date = ?", (RUN_DATE_LOCAL.isoformat(),)
        ).fetchone()
    assert row["status"] == "ok"


# --- an all-empty day (no items collected at all) still saves ok with [] ---


async def test_all_empty_day_saves_ok_with_no_message_ids_and_no_sends(db_path, http_client):
    # No collectors at all -- every topic has literally nothing to
    # summarize, so render_digest (design.md §13, owner decision A) emits
    # zero TopicMessages. That's success ("nothing to report today"), not
    # a failure, and nothing should have been sent anywhere.
    deps = _make_deps(db_path, http_client, collectors=[])

    outcome = await run_daily(deps, PrintPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status == "ok"
    assert outcome.rendered is not None
    assert outcome.rendered.messages == []
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status, posted_message_ids FROM digests WHERE run_date = ?",
            (RUN_DATE_LOCAL.isoformat(),),
        ).fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["posted_message_ids"]) == []


# --- real DiscordPublisher wired through run_daily, hand-written channel fakes ---
#
# Everything above drives run_daily with PrintPublisher or a tiny stand-in.
# These use the real DiscordPublisher (newsbot/bot/client.py) against
# hand-written FakeClient/FakeChannel objects -- the same spirit as
# test_discord_publisher.py and test_publish_with_retry.py, but exercised
# through the whole pipeline (claim -> build -> publish -> save) instead of
# in isolation, since that's the only way to see what actually lands in
# the `digests` row.

_BL4_CHANNEL_ID = 123456789012345690
_PALWORLD_CHANNEL_ID = 123456789012345691
_DIABLO_CHANNEL_ID = 123456789012345692


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _FakeChannel:
    def __init__(self, *, next_id: int, raise_error: Exception | None = None) -> None:
        self._next_id = next_id
        self.raise_error = raise_error
        self.sent = 0

    async def send(self, content=None, *, embed=None, allowed_mentions=None):
        if self.raise_error is not None:
            raise self.raise_error
        self.sent += 1
        self._next_id += 1
        return _FakeMessage(self._next_id)


class _FakeGatewayClient:
    def __init__(self, channels: dict[int, _FakeChannel]) -> None:
        self._channels = channels

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        return self._channels[channel_id]


async def test_forced_run_now_after_a_partial_failure_reposts_every_topic(db_path, http_client):
    # design.md §13 plan section 4's documented v1 limitation: nothing
    # persists per-topic progress *across* separate run_daily calls (only
    # within one DiscordPublisher instance's own retries). A first run
    # that posts borderlands4 then permanently fails on palworld leaves a
    # `failed` row with borderlands4's id; a forced run-now with a fresh
    # publisher reposts *both* topics, including the one that already
    # made it out. Pinned here so a future fix to that limitation is a
    # deliberate change to this test, not a silent regression.
    first_channels = {
        _BL4_CHANNEL_ID: _FakeChannel(next_id=100),
        _PALWORLD_CHANNEL_ID: _FakeChannel(
            next_id=200, raise_error=discord.HTTPException(_fake_403(), "forbidden")
        ),
        _DIABLO_CHANNEL_ID: _FakeChannel(next_id=300),
    }
    deps = _make_deps(db_path, http_client)
    first_publisher = DiscordPublisher(_FakeGatewayClient(first_channels))

    first_outcome = await run_daily(deps, first_publisher, mode=RunMode.POST, sleep=_no_sleep)
    assert first_outcome.status == "failed"
    assert first_channels[_BL4_CHANNEL_ID].sent == 1
    assert first_channels[_PALWORLD_CHANNEL_ID].sent == 0

    # A fresh publisher, all channels healthy now, forced re-run.
    second_channels = {
        _BL4_CHANNEL_ID: _FakeChannel(next_id=400),
        _PALWORLD_CHANNEL_ID: _FakeChannel(next_id=500),
        _DIABLO_CHANNEL_ID: _FakeChannel(next_id=600),
    }
    second_publisher = DiscordPublisher(_FakeGatewayClient(second_channels))
    second_outcome = await run_daily(
        deps, second_publisher, mode=RunMode.POST, force=True, sleep=_no_sleep
    )

    assert second_outcome.status == "ok"
    # borderlands4 gets a second message even though its first one is
    # still sitting in the channel -- the documented limitation.
    assert second_channels[_BL4_CHANNEL_ID].sent == 1
    assert second_channels[_PALWORLD_CHANNEL_ID].sent == 1


def _fake_403():
    class _Resp:
        status = 403
        reason = "forbidden"
        headers = {}
        request_info = None

    return _Resp()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: DiscordPublisher.posted_ids tracks a topic posted right before a "
        "CancelledError on a later topic's send, but _run_post's outer "
        "`except BaseException` handler only reads `getattr(exc, 'posted_ids', "
        "None)` off the CancelledError itself (which never carries one) -- it "
        "never falls back to `publisher.posted_ids` the way client.py's own "
        "DiscordPublisher.posted_ids docstring says it does. The digest row "
        "gets saved `failed` with posted_message_ids == [] even though a "
        "topic's embed really did land in its channel, so the next catch-up "
        "or run-now (needs_confirmation/should_catch_up both read this same "
        "column) treats it as a clean failure and reposts that topic's "
        "embed a second time. newsbot/pipeline/run.py:424."
    ),
)
async def test_cancelled_error_mid_publish_still_records_what_the_publisher_posted(
    db_path, http_client
):
    channels = {
        _BL4_CHANNEL_ID: _FakeChannel(next_id=100),
        _PALWORLD_CHANNEL_ID: _FakeChannel(next_id=200, raise_error=asyncio.CancelledError()),
        _DIABLO_CHANNEL_ID: _FakeChannel(next_id=300),
    }
    deps = _make_deps(db_path, http_client)
    publisher = DiscordPublisher(_FakeGatewayClient(channels))

    with pytest.raises(asyncio.CancelledError):
        await run_daily(deps, publisher, mode=RunMode.POST, sleep=_no_sleep)

    # The publisher itself knows borderlands4's message landed...
    assert publisher.posted_ids != []
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status, posted_message_ids FROM digests WHERE run_date = ?",
            (RUN_DATE_LOCAL.isoformat(),),
        ).fetchone()
    assert row["status"] == "failed"
    # ...but the digest row should carry the same id, not lose it.
    assert json.loads(row["posted_message_ids"]) == publisher.posted_ids
