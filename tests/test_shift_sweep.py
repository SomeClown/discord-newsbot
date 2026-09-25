"""Tests for newsbot.shift.sweep: the I/O side of the SHiFT alert sweep (plan step 8).

`test_shift_decide.py` already covers the pure planning; this file is
where a plan actually turns into database writes and (fake) Discord
messages -- silent seeding, the once-per-code guard, the daily ping cap,
a poster that fails, a sweep cancelled mid-post, the run lock, and the
daily digest run's own post-publish code check.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.pipeline.lock import _run_lock
from newsbot.pipeline.publisher import PublishError
from newsbot.pipeline.run import Deps, RunMode, StubLLM, build_fixture_collectors, run_daily
from newsbot.shift.sweep import PrintCodeAlertPoster, SweepDeps, process_items, run_code_sweep
from newsbot.store import repo
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
INTEGRATION_FIXTURES = Path(__file__).parent / "fixtures" / "integration"

# 13:00 America/Los_Angeles on 2026-09-25 (PDT, UTC-7) -- an ordinary
# daytime instant, nowhere near a local-midnight edge case.
NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
NEXT_LA_DAY = NOW + timedelta(days=1)

CODE_A = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBBB-BBBBB-BBBBB-BBBBB-BBBBB"
CODE_C = "CCCCC-CCCCC-CCCCC-CCCCC-CCCCC"
CODE_D = "DDDDD-DDDDD-DDDDD-DDDDD-DDDDD"
CODE_E = "EEEEE-EEEEE-EEEEE-EEEEE-EEEEE"


async def _no_sleep(_seconds: float) -> None:
    return None


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


def _cfg(**alert_overrides):
    cfg = load_config(CONFIG_PATH)
    alerts = cfg.alerts.model_copy(update={"enabled": True, **alert_overrides})
    return cfg.model_copy(update={"alerts": alerts})


def _item(code: str, *, url: str | None = None, published_at=NOW, trust="official") -> RawItem:
    return RawItem(
        url=url or f"https://example.com/{code}",
        title=f"New code: {code}",
        excerpt="",
        source_name="Gearbox Blog",
        trust=trust,
        published_at=published_at,
        topics=None,
    )


class _FakePoster:
    def __init__(self, *, fail_times: int = 0, error_factory=None) -> None:
        self.sent: list = []
        self.calls = 0
        self.fail_times = fail_times
        self.error_factory = error_factory or (lambda: PublishError("simulated failure"))

    async def post(self, alert):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error_factory()
        self.sent.append(alert)
        return 1000 + self.calls


def _deps(db_path, http_client, *, cfg=None, poster=None, now=None, alerts=None, sleep=None):
    alerts = alerts if alerts is not None else []

    async def alert(text: str) -> None:
        alerts.append(text)

    deps = SweepDeps(
        cfg=cfg or _cfg(),
        db_path=db_path,
        http=http_client,
        collectors=[],
        now=now or (lambda: NOW),
        alert=alert,
        poster=poster or _FakePoster(),
        sleep=sleep or _no_sleep,
    )
    deps.alerts = alerts  # type: ignore[attr-defined]
    return deps


def _seed(db_path) -> None:
    with closing(connect(db_path)) as conn:
        repo.record_silent_codes(conn, [], now=lambda: NOW, mark_seeded=True)


def _row(db_path, code):
    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT status, pinged, message_id FROM alerted_codes WHERE code = ?", (code,)
        ).fetchone()


# --- silent seeding ---


async def test_unseeded_healthy_records_silently_and_sets_marker(db_path, http_client):
    deps = _deps(db_path, http_client)
    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    assert outcome.posted == 0
    assert outcome.silent == 1
    row = _row(db_path, CODE_A)
    assert row["status"] == "seeded"
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is True


async def test_unseeded_unhealthy_does_not_set_marker(db_path, http_client):
    deps = _deps(db_path, http_client)
    await process_items(deps, [_item(CODE_A)], seeding_ok=False)
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is False
    assert _row(db_path, CODE_A)["status"] == "seeded"  # still recorded, just no marker


# --- once seeded: posting, ping, once-per-code ---


async def test_seeded_new_code_posts_and_pings(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    assert outcome.posted == 1
    assert outcome.ping is True
    assert len(poster.sent) == 1
    assert poster.sent[0].content.startswith("@everyone ")
    row = _row(db_path, CODE_A)
    assert row["status"] == "posted"
    assert row["pinged"] == 1
    assert row["message_id"] == 1001


async def test_repeat_code_posts_nothing(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    assert outcome.posted == 0
    assert outcome.silent == 0
    assert len(poster.sent) == 1  # only the first call's post


# --- daily ping cap ---


async def test_fourth_batch_in_a_day_posts_without_ping_and_admin_alerts(db_path, http_client):
    _seed(db_path)
    alerts: list[str] = []
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster, alerts=alerts, cfg=_cfg(max_pings_per_day=3))

    for code in (CODE_A, CODE_B, CODE_C):
        outcome = await process_items(deps, [_item(code)], seeding_ok=True)
        assert outcome.ping is True

    outcome = await process_items(deps, [_item(CODE_D)], seeding_ok=True)
    assert outcome.posted == 1
    assert outcome.ping is False
    assert outcome.cap_reached is True
    assert any("cap" in a.lower() for a in alerts)
    assert _row(db_path, CODE_D)["pinged"] == 0
    assert _row(db_path, CODE_D)["status"] == "posted"


async def test_next_la_day_resets_the_ping_cap(db_path, http_client):
    _seed(db_path)
    cfg = _cfg(max_pings_per_day=1)
    deps = _deps(db_path, http_client, cfg=cfg)
    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    assert outcome.ping is True

    deps_tomorrow = _deps(db_path, http_client, cfg=cfg, now=lambda: NEXT_LA_DAY)
    outcome2 = await process_items(deps_tomorrow, [_item(CODE_B)], seeding_ok=True)
    assert outcome2.ping is True


# --- posting failures ---


async def test_poster_failing_every_attempt_marks_failed_no_retry_ping_stays_spent(
    db_path, http_client
):
    _seed(db_path)
    alerts: list[str] = []
    poster = _FakePoster(fail_times=999)  # always fails
    deps = _deps(db_path, http_client, poster=poster, alerts=alerts)
    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)

    assert outcome.posted == 0
    assert outcome.failed == 1
    assert poster.calls == 4  # 1 attempt + 3 retries (pipeline/run.py's own backoff length)
    assert _row(db_path, CODE_A)["status"] == "failed"
    with closing(connect(db_path)) as conn:
        # The ping was spent at claim time and stays spent -- a failed post
        # doesn't get the budget back.
        assert repo.get_alert_state(conn).ping_count == 1
    assert any("failed" in a.lower() for a in alerts)


async def test_non_publish_error_fails_immediately_without_retry(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster(fail_times=999, error_factory=lambda: RuntimeError("auth broke"))
    deps = _deps(db_path, http_client, poster=poster)
    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)

    assert outcome.failed == 1
    assert poster.calls == 1  # no retry for a non-PublishError
    assert _row(db_path, CODE_A)["status"] == "failed"


async def test_cancelled_error_leaves_code_pending_for_fail_pending_codes(db_path, http_client):
    _seed(db_path)

    class _CancellingPoster:
        async def post(self, alert):
            raise asyncio.CancelledError

    deps = _deps(db_path, http_client, poster=_CancellingPoster())
    with pytest.raises(asyncio.CancelledError):
        await process_items(deps, [_item(CODE_A)], seeding_ok=True)

    assert _row(db_path, CODE_A)["status"] == "pending"
    with closing(connect(db_path)) as conn:
        assert repo.fail_pending_codes(conn) == [CODE_A]


# --- run_code_sweep: the run lock ---


async def test_run_code_sweep_returns_none_and_writes_nothing_when_lock_held(db_path, http_client):
    deps = _deps(db_path, http_client)
    await _run_lock.acquire()
    try:
        outcome = await run_code_sweep(deps)
    finally:
        _run_lock.release()

    assert outcome is None
    with closing(connect(db_path)) as conn:
        state = repo.get_alert_state(conn)
        assert state.last_sweep_at is None
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


async def test_run_code_sweep_writes_no_digest_tables(db_path, http_client):
    _seed(db_path)
    deps = _deps(db_path, http_client, poster=PrintCodeAlertPoster())
    await run_code_sweep(deps)
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0] == 0


# --- the daily run's own code check ---


def _daily_deps(
    db_path, http_client, *, poster=None, mode_alerts=None, enabled=True, max_item_age_hours=48
):
    cfg = load_config(CONFIG_PATH)
    alerts_cfg = cfg.alerts.model_copy(
        update={"enabled": enabled, "max_item_age_hours": max_item_age_hours}
    )
    cfg = cfg.model_copy(update={"alerts": alerts_cfg})
    alerts = mode_alerts if mode_alerts is not None else []

    async def alert(text: str) -> None:
        alerts.append(text)

    deps = Deps(
        cfg=cfg,
        db_path=db_path,
        http=http_client,
        llm=StubLLM(INTEGRATION_FIXTURES / "llm.json"),
        collectors=build_fixture_collectors(INTEGRATION_FIXTURES),
        now=lambda: NOW,
        alert=alert,
        code_alert_poster=poster,
    )
    deps.alerts = alerts  # type: ignore[attr-defined]
    return deps


async def test_daily_post_run_checks_codes_and_alerts_once(db_path, http_client):
    # integration/borderlands4.json's first item embeds a code in full_text
    # (see the fixture file) -- silent-seeded on this first run, same as a
    # fresh sweep would.
    poster = _FakePoster()
    deps = _daily_deps(db_path, http_client, poster=poster)
    outcome = await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    assert outcome.status in ("ok", "partial")
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is True
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1


async def test_daily_preview_run_never_checks_codes(db_path, http_client):
    poster = _FakePoster()
    deps = _daily_deps(db_path, http_client, poster=poster)
    await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.PREVIEW, sleep=_no_sleep)
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


async def test_daily_hook_exception_does_not_change_digest_status(db_path, http_client):
    _seed(db_path)  # already seeded, so this run actually tries to post

    class _ExplodingPoster:
        async def post(self, alert):
            raise RuntimeError("poster is on fire")

    alerts: list[str] = []
    deps = _daily_deps(
        db_path, http_client, poster=_ExplodingPoster(), mode_alerts=alerts, max_item_age_hours=200
    )
    outcome = await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    # The digest itself still posted fine; only the code check blew up.
    assert outcome.status in ("ok", "partial")
    assert any("code alert" in a.lower() for a in alerts)


async def test_daily_hook_is_a_noop_when_alerts_disabled(db_path, http_client):
    poster = _FakePoster()
    deps = _daily_deps(db_path, http_client, poster=poster, enabled=False)
    await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


async def test_daily_hook_is_a_noop_when_no_poster_configured(db_path, http_client):
    deps = _daily_deps(db_path, http_client, poster=None)
    await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.POST, sleep=_no_sleep)
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


class _PrintDigestPublisher:
    async def publish(self, r) -> list[int]:
        return [1]
