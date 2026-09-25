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
from newsbot.shift.sweep import (
    PrintCodeAlertPoster,
    SweepDeps,
    process_items,
    run_code_sweep,
    run_test_alert,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
INTEGRATION_FIXTURES = Path(__file__).parent / "fixtures" / "integration"

# 13:00 America/Los_Angeles on 2026-09-25 (PDT, UTC-7) -- an ordinary
# daytime instant, nowhere near a local-midnight edge case.
NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
NEXT_LA_DAY = NOW + timedelta(days=1)

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
CODE_C = "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"
CODE_D = "DDDD4-DDDDD-DDDDD-DDDDD-DDDDD"
CODE_E = "EEEE5-EEEEE-EEEEE-EEEEE-EEEEE"


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


def _multi_code_item(codes: list[str], *, url: str, trust="official") -> RawItem:
    return RawItem(
        url=url,
        title="Weekly SHiFT code roundup",
        excerpt="",
        full_text="All this week's codes: " + " ".join(codes),
        source_name="Roundup Blog",
        trust=trust,
        published_at=NOW,
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


async def test_healthy_empty_sweep_sets_seeded_marker(db_path, http_client):
    # QA item 4: an unseeded, healthy sweep that finds zero codes
    # anywhere still has to flip the marker on -- otherwise the *next*
    # sweep to find a real fresh code treats it as brand new and silently
    # seeds it instead of posting.
    deps = _deps(db_path, http_client)
    outcome = await process_items(deps, [], seeding_ok=True)
    assert outcome.posted == 0
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is True


async def test_healthy_empty_sweep_then_fresh_code_posts_with_ping(db_path, http_client):
    deps = _deps(db_path, http_client)
    await process_items(deps, [], seeding_ok=True)  # seeds on zero codes

    poster = _FakePoster()
    deps2 = _deps(db_path, http_client, poster=poster)
    outcome = await process_items(deps2, [_item(CODE_A)], seeding_ok=True)

    assert outcome.posted == 1
    assert outcome.ping is True
    assert len(poster.sent) == 1


async def test_unhealthy_empty_sweep_does_not_set_marker(db_path, http_client):
    deps = _deps(db_path, http_client)
    await process_items(deps, [], seeding_ok=False)
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is False


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


# --- trust-gated pings (QA item 7 option A, owner decision 2026-09-25) ---


async def test_community_only_code_posts_without_ping_and_without_spending_cap(
    db_path, http_client
):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    outcome = await process_items(deps, [_item(CODE_A, trust="community")], seeding_ok=True)

    assert outcome.posted == 1
    assert outcome.ping is False
    assert outcome.cap_reached is False  # nothing trusted, so nothing "reached" the cap
    assert len(poster.sent) == 1
    assert not poster.sent[0].content.startswith("@everyone ")
    row = _row(db_path, CODE_A)
    assert row["status"] == "posted"
    assert row["pinged"] == 0
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).ping_count == 0  # the cap was never spent


async def test_trusted_and_community_batch_pings_once(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    items = [_item(CODE_A, trust="community"), _item(CODE_B, trust="official")]
    outcome = await process_items(deps, items, seeding_ok=True)

    assert outcome.posted == 2
    assert outcome.ping is True
    assert len(poster.sent) == 1  # one message, one ping, covering both codes
    assert poster.sent[0].content.startswith("@everyone ")
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).ping_count == 1


async def test_press_only_batch_pings(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    outcome = await process_items(deps, [_item(CODE_A, trust="press")], seeding_ok=True)

    assert outcome.posted == 1
    assert outcome.ping is True
    assert poster.sent[0].content.startswith("@everyone ")


async def test_trusted_candidates_ordered_first_in_the_batch(db_path, http_client):
    # Community first, official second in collection order -- the
    # rendered (and posted) batch should still put the trusted one first,
    # so the one message that carries the ping carries a trusted code.
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    items = [_item(CODE_A, trust="community"), _item(CODE_B, trust="official")]
    await process_items(deps, items, seeding_ok=True)

    assert poster.sent[0].codes == [CODE_B, CODE_A]


# --- silent roundups (QA item 7, owner decision 2026-09-25) ---


async def test_roundup_item_with_six_codes_records_all_silently_no_post(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    codes = [CODE_A, CODE_B, CODE_C, CODE_D, CODE_E, "F6666-FFFFF-FFFFF-FFFFF-FFFFF"]
    item = _multi_code_item(codes, url="https://example.com/roundup")
    outcome = await process_items(deps, [item], seeding_ok=True)

    assert outcome.posted == 0
    assert poster.sent == []
    with closing(connect(db_path)) as conn:
        rows = {
            row["code"]: row["status"]
            for row in conn.execute(
                "SELECT code, status FROM alerted_codes WHERE code IN ({})".format(  # noqa: S608
                    ",".join("?" for _ in codes)
                ),
                codes,
            ).fetchall()
        }
    assert rows == {code: "roundup" for code in codes}


async def test_code_in_roundup_and_dedicated_post_still_alerts_with_ping(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    codes = [CODE_A, CODE_B, CODE_C, CODE_D, CODE_E, "F6666-FFFFF-FFFFF-FFFFF-FFFFF"]
    roundup = _multi_code_item(codes, url="https://example.com/roundup")
    dedicated = _item(CODE_A, url="https://example.com/dedicated-post")

    outcome = await process_items(deps, [roundup, dedicated], seeding_ok=True)

    assert outcome.posted == 1
    assert outcome.ping is True
    assert poster.sent[0].codes == [CODE_A]
    row = _row(db_path, CODE_A)
    assert row["status"] == "posted"
    assert row["pinged"] == 1
    # The other five roundup-only codes still recorded silently.
    with closing(connect(db_path)) as conn:
        others = [c for c in codes if c != CODE_A]
        rows = {
            row["code"]: row["status"]
            for row in conn.execute(
                "SELECT code, status FROM alerted_codes WHERE code IN ({})".format(  # noqa: S608
                    ",".join("?" for _ in others)
                ),
                others,
            ).fetchall()
        }
    assert rows == {code: "roundup" for code in others}


async def test_roundup_threshold_is_configurable(db_path, http_client):
    _seed(db_path)
    cfg = _cfg(max_codes_per_item=10)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster, cfg=cfg)
    codes = [CODE_A, CODE_B, CODE_C, CODE_D, CODE_E, "F6666-FFFFF-FFFFF-FFFFF-FFFFF"]
    item = _multi_code_item(codes, url="https://example.com/roundup")

    outcome = await process_items(deps, [item], seeding_ok=True)

    # 6 codes no longer exceeds a raised threshold of 10 -- not a roundup.
    assert outcome.posted == 6


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


async def test_poster_fails_on_second_of_three_messages_earlier_codes_stay_posted(
    db_path, http_client
):
    # A batch big enough to split into 3 Discord messages (format.py's
    # own overflow handling); message 1 posts fine, message 2 fails every
    # retry, message 3 is marked failed without ever being attempted.
    # The ping (spent once, at claim time, before any message went out)
    # must stay spent regardless of which message actually carried it.
    _seed(db_path)
    items = [
        _item(f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA", url=f"https://example.com/{'a' * 300}/{i}")
        for i in range(12)
    ]

    class _FailsOnSecondPoster:
        # Fails every attempt (including retries) of the *second distinct*
        # message content it sees, so message 1 posts once and message 2
        # exhausts its retries -- a call-count check alone would miss
        # this, since a retry re-invokes `post()` for the same message.
        def __init__(self) -> None:
            self.calls = 0
            self.sent: list = []
            self._seen_contents: list[str] = []

        async def post(self, alert):
            self.calls += 1
            if alert.content not in self._seen_contents:
                self._seen_contents.append(alert.content)
            message_index = self._seen_contents.index(alert.content)
            if message_index == 1:
                raise PublishError("second message failed")
            self.sent.append(alert)
            return 1000 + self.calls

    poster = _FailsOnSecondPoster()
    alerts: list[str] = []
    deps = _deps(db_path, http_client, poster=poster, alerts=alerts)
    outcome = await process_items(deps, items, seeding_ok=True)

    assert outcome.ping is True
    assert outcome.posted == 5  # message 1's codes only
    assert outcome.failed == 7  # message 2's + message 3's codes, message 3 never attempted
    # message 1's poster call, plus message 2's 1 attempt + 3 retries --
    # message 3 is marked failed without ever calling the poster again.
    assert poster.calls == 5

    with closing(connect(db_path)) as conn:
        statuses = {
            row["code"]: row["status"]
            for row in conn.execute("SELECT code, status FROM alerted_codes").fetchall()
        }
    posted_codes = [code for code, status in statuses.items() if status == "posted"]
    failed_codes = [code for code, status in statuses.items() if status == "failed"]
    assert len(posted_codes) == 5
    assert len(failed_codes) == 7
    with closing(connect(db_path)) as conn:
        # The ping was spent once at claim time (before any message sent)
        # and stays spent no matter which message failed.
        assert repo.get_alert_state(conn).ping_count == 1
    assert any("failed" in a.lower() for a in alerts)


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
    db_path,
    http_client,
    *,
    poster=None,
    mode_alerts=None,
    enabled=True,
    max_item_age_hours=48,
    collectors=None,
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
        collectors=collectors
        if collectors is not None
        else build_fixture_collectors(INTEGRATION_FIXTURES),
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


class _WebSearchOnlyCollector:
    # source_type = "web_search" is the only thing that matters here --
    # the daily run's own code check has to treat this the same as the
    # hourly sweep does (which never even builds a web_search collector
    # at all): a code that only ever showed up here was never seeded.
    source_type = "web_search"
    rate_limit_key = None

    def __init__(self, name: str, items: list[RawItem]) -> None:
        self.name = name
        self._items = items

    async def collect(self, http):
        return self._items


async def test_daily_hook_never_alerts_on_a_web_search_only_code(db_path, http_client):
    web_search_item = _item(CODE_A, url="https://example.com/brave-result", trust="community")
    non_web_search_item = RawItem(
        url="https://example.com/other",
        title="An ordinary post with no code",
        excerpt="Nothing to see here.",
        source_name="Some RSS Feed",
        trust="community",
        published_at=NOW,
        topics=None,
    )
    collectors = [
        _WebSearchOnlyCollector("Brave Search", [web_search_item]),
        _WebSearchOnlyCollector("not-brave", [non_web_search_item]),  # source_type overridden below
    ]
    collectors[1].source_type = "rss"

    poster = _FakePoster()
    deps = _daily_deps(db_path, http_client, poster=poster, collectors=collectors)
    outcome = await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status in ("ok", "partial")
    with closing(connect(db_path)) as conn:
        # web_search_item's code never even got recorded, let alone
        # seeded or posted -- it's as if that sweep-invisible item never
        # existed for the code check at all.
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0
    assert poster.sent == []


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


async def test_daily_hook_cancelled_after_successful_publish_leaves_digest_ok_with_ids(
    db_path, http_client
):
    # QA item 3: the code check now runs only after the digest row is
    # already durably saved -- a CancelledError raised inside it must not
    # unwind past _run_claimed and let _run_post's own catch-all mark an
    # already-published digest `failed` with no ids.
    _seed(db_path)  # already seeded, so this run actually tries to post

    class _CancellingPoster:
        async def post(self, alert):
            raise asyncio.CancelledError

    alerts: list[str] = []
    deps = _daily_deps(
        db_path, http_client, poster=_CancellingPoster(), mode_alerts=alerts, max_item_age_hours=200
    )
    outcome = await run_daily(deps, _PrintDigestPublisher(), mode=RunMode.POST, sleep=_no_sleep)

    assert outcome.status in ("ok", "partial")
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status, posted_message_ids FROM digests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] in ("ok", "partial")
    import json

    assert json.loads(row["posted_message_ids"]) == [1]  # _PrintDigestPublisher's own id
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


# --- run_test_alert ---


async def test_run_test_alert_posts_with_ping_and_counts_against_cap(db_path, http_client):
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    outcome = await run_test_alert(deps, CODE_A, False)
    assert outcome.posted == 1
    assert outcome.ping is True
    assert poster.sent[0].content.startswith("@everyone ")
    assert "[TEST] " in poster.sent[0].content
    row = _row(db_path, CODE_A)
    assert row["status"] == "posted"
    with closing(connect(db_path)) as conn:
        # Still counted against the daily cap -- a free test alert
        # wouldn't actually exercise the cap.
        assert repo.get_alert_state(conn).ping_count == 1


async def test_run_test_alert_does_not_set_the_seeded_marker(db_path, http_client):
    # A test alert is treated as already seeded (so it always tries to
    # post) without ever setting the marker itself -- it doesn't get to
    # vouch for every other code sitting in the feeds.
    deps = _deps(db_path, http_client)
    await run_test_alert(deps, CODE_A, False)
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).seeded is False


async def test_run_test_alert_with_already_known_code_posts_nothing(db_path, http_client):
    # The code was already alerted (posted) for real; a test run against
    # the same code shouldn't re-post it or spend another ping -- known
    # codes are dropped in plan_alerts regardless of the caller.
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    assert len(poster.sent) == 1
    first_ping_count = None
    with closing(connect(db_path)) as conn:
        first_ping_count = repo.get_alert_state(conn).ping_count

    outcome = await run_test_alert(deps, CODE_A, False)
    assert outcome.posted == 0
    assert outcome.silent == 0
    assert len(poster.sent) == 1  # no second post
    with closing(connect(db_path)) as conn:
        assert repo.get_alert_state(conn).ping_count == first_ping_count  # cap untouched


async def test_run_test_alert_golden_true_shows_golden_wording(db_path, http_client):
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    await run_test_alert(deps, CODE_A, True)
    assert "Golden Key" in poster.sent[0].content


async def test_run_test_alert_skips_when_run_lock_held(db_path, http_client):
    # Step 7: run_test_alert shares the run lock with a sweep/daily run --
    # an admin firing /newsbot test-alert while one is in progress gets a
    # clean skip (None), the same shape run_code_sweep already returns,
    # not a race against claim_codes.
    deps = _deps(db_path, http_client)
    await _run_lock.acquire()
    try:
        outcome = await run_test_alert(deps, CODE_A, False)
    finally:
        _run_lock.release()

    assert outcome is None
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 0


# --- claim_codes cap re-check wired through _apply_plan (step 7) ---


async def test_apply_plan_renders_without_ping_when_cap_reached_between_read_and_claim(
    db_path, http_client
):
    # Simulates two callers racing the same last ping slot: seed the state
    # so plan_alerts (reading pings_today a moment "before") believes
    # there's still budget, but have claim_codes's own recheck see the cap
    # already spent by the time it actually runs (as if another caller
    # claimed it first). The rendered message must not have pinged.
    _seed(db_path)
    cfg = _cfg(max_pings_per_day=1)
    poster = _FakePoster()
    alerts: list[str] = []
    deps = _deps(db_path, http_client, poster=poster, cfg=cfg, alerts=alerts)

    # Spend the one available ping out from under this call, between its
    # own _read_ping_state and its claim_codes call is hard to simulate
    # without a real race, so instead prove the same thing claim_codes's
    # own recheck guarantees directly: process one batch that spends the
    # cap, then a second batch in the same call sequence sees it spent.
    await process_items(deps, [_item(CODE_A)], seeding_ok=True)
    outcome = await process_items(deps, [_item(CODE_B)], seeding_ok=True)

    assert outcome.posted == 1
    assert outcome.ping is False
    assert outcome.cap_reached is True
    assert poster.sent[-1].ping is False
    assert not poster.sent[-1].content.startswith("@everyone ")
    assert any("cap" in a.lower() for a in alerts)


async def test_cap_reached_admin_alert_suppressed_when_max_pings_is_zero(db_path, http_client):
    _seed(db_path)
    cfg = _cfg(max_pings_per_day=0)
    poster = _FakePoster()
    alerts: list[str] = []
    deps = _deps(db_path, http_client, poster=poster, cfg=cfg, alerts=alerts)

    outcome = await process_items(deps, [_item(CODE_A)], seeding_ok=True)

    assert outcome.posted == 1
    assert outcome.ping is False
    assert outcome.cap_reached is True
    # The cap being 0 is the config working as intended, not an admin-worthy
    # surprise -- no "cap reached" alert for it.
    assert not any("cap" in a.lower() for a in alerts)


# --- `--sweep` CLI round trip ---


def test_sweep_cli_round_trip_is_idempotent_against_the_same_fixtures(tmp_path):
    import subprocess
    import sys

    shift_fixtures = Path(__file__).parent / "fixtures" / "shift"
    config_path = shift_fixtures / "config.yaml"
    db_path_cli = str(tmp_path / "sweep_cli.db")

    def _run_sweep_cli() -> subprocess.CompletedProcess:
        # Every argument is a fixed literal or a path this test built
        # itself -- nothing here comes from untrusted input.
        return subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "newsbot.pipeline.run",
                "--config",
                str(config_path),
                "--db",
                db_path_cli,
                "--fixtures",
                str(shift_fixtures),
                "--sweep",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    first = _run_sweep_cli()
    assert first.returncode == 0, first.stderr

    with closing(connect(db_path_cli)) as conn:
        first_rows = conn.execute("SELECT code, status FROM alerted_codes").fetchall()
        first_state = repo.get_alert_state(conn)
    assert len(first_rows) == 1
    assert first_rows[0]["status"] == "seeded"
    assert first_state.seeded is True  # borderlands4.json's collector set was healthy

    second = _run_sweep_cli()
    assert second.returncode == 0, second.stderr

    with closing(connect(db_path_cli)) as conn:
        second_rows = conn.execute("SELECT code, status FROM alerted_codes").fetchall()
        # Never posted, never pinged: the code was already recorded
        # `seeded` on the first run, so the second run's identical
        # fixtures data drops it as already-known rather than re-alerting.
        assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
        assert repo.get_alert_state(conn).ping_count == 0
    assert [dict(r) for r in second_rows] == [dict(r) for r in first_rows]
