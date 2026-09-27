"""Adversarial tests for the roundup posting path (design.md §13, step 5).

`test_shift_sweep.py` already covers the basic roundup-posts-unpinged
shape and the 50-code cap with a 60-code batch. This file goes after the
gaps a test-engineer brief specifically calls out: the same code showing
up in two different roundup items, a roundup batch that fails to post,
the ping budget staying untouched when a roundup batch rides alongside a
capped normal batch, and the exact 50/51 boundary.
"""

from __future__ import annotations

import dataclasses
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.decide import CodeCandidate, plan_alerts
from newsbot.shift.sweep import SweepDeps, process_items
from newsbot.store import repo
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"


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


def _item(code: str, *, url: str | None = None, trust="official") -> RawItem:
    return RawItem(
        url=url or f"https://example.com/{code}",
        title=f"New code: {code}",
        excerpt="",
        source_name="Gearbox Blog",
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


def _deps(db_path, http_client, *, cfg=None, poster=None, alerts=None, sleep=None):
    alerts = alerts if alerts is not None else []

    async def alert(text: str) -> None:
        alerts.append(text)

    deps = SweepDeps(
        cfg=cfg or _cfg(),
        db_path=db_path,
        http=http_client,
        collectors=[],
        now=lambda: NOW,
        alert=alert,
        poster=poster or _FakePoster(),
        sleep=sleep or _no_sleep,
    )
    deps.alerts = alerts  # type: ignore[attr-defined]
    return deps


def _seed(db_path) -> None:
    with closing(connect(db_path)) as conn:
        repo.record_silent_codes(conn, [], now=lambda: NOW, mark_seeded=True)


def _rows_for(conn, codes: list[str]) -> dict[str, tuple[str, int, int]]:
    placeholders = ",".join("?" for _ in codes)
    select = "SELECT code, status, pinged, from_roundup FROM alerted_codes "
    where = f"WHERE code IN ({placeholders})"  # noqa: S608
    return {
        row["code"]: (row["status"], row["pinged"], row["from_roundup"])
        for row in conn.execute(select + where, codes).fetchall()
    }


def _codes(n: int, prefix: str = "Z") -> list[str]:
    return [f"{prefix}{i:04d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(n)]


# --- same code, two different roundup items ---


async def test_same_code_in_two_different_roundup_items_posts_once(db_path, http_client):
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    filler_a = [f"A{i:04d}-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(6)]
    filler_b = [f"B{i:04d}-BBBBB-BBBBB-BBBBB-BBBBB" for i in range(6)]
    shared_code = "SHAR3-AAAAA-AAAAA-AAAAA-AAAAA"
    item_a = _multi_code_item([*filler_a, shared_code], url="https://example.com/roundup-a")
    item_b = _multi_code_item([*filler_b, shared_code], url="https://example.com/roundup-b")

    outcome = await process_items(deps, [item_a, item_b], seeding_ok=True)

    assert outcome.roundup_posted == 6 + 6 + 1  # each filler set plus the shared code, once
    matches = [c for message in poster.sent for c in message.codes if c == shared_code]
    assert matches == [shared_code]  # exactly one message carries it
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerted_codes WHERE code = ?", (shared_code,)
        ).fetchone()[0]
    assert row == 1  # one row, not one per item


# --- roundup posting failures ---


async def test_roundup_batch_permanent_failure_marks_failed_and_never_pings(db_path, http_client):
    _seed(db_path)
    alerts: list[str] = []
    poster = _FakePoster(fail_times=999)
    deps = _deps(db_path, http_client, poster=poster, alerts=alerts)
    codes = _codes(6)
    item = _multi_code_item(codes, url="https://example.com/roundup")

    outcome = await process_items(deps, [item], seeding_ok=True)

    assert outcome.roundup_posted == 0
    assert outcome.failed == 6
    assert poster.sent == []  # never actually landed
    with closing(connect(db_path)) as conn:
        rows = _rows_for(conn, codes)
    assert all(status == "failed" for status, _pinged, _roundup in rows.values())
    assert all(pinged == 0 for _status, pinged, _roundup in rows.values())
    assert any("failed" in a.lower() for a in alerts)
    assert not any("@everyone" in a for a in alerts)


async def test_roundup_batch_transient_then_success_posts_unpinged_after_retry(
    db_path, http_client
):
    _seed(db_path)
    poster = _FakePoster(fail_times=1)  # first attempt fails, retry succeeds
    deps = _deps(db_path, http_client, poster=poster)
    codes = _codes(6)
    item = _multi_code_item(codes, url="https://example.com/roundup")

    outcome = await process_items(deps, [item], seeding_ok=True)

    assert outcome.roundup_posted == 6
    assert outcome.failed == 0
    assert len(poster.sent) == 1
    assert poster.sent[0].ping is False
    with closing(connect(db_path)) as conn:
        rows = _rows_for(conn, codes)
    assert all(status == "posted" for status, _pinged, _roundup in rows.values())
    assert all(pinged == 0 for _status, pinged, _roundup in rows.values())


# --- roundup never spends the ping budget, even alongside a capped normal batch ---


async def test_roundup_alongside_a_capped_normal_batch_never_touches_ping_count(
    db_path, http_client
):
    _seed(db_path)
    cfg = _cfg(max_pings_per_day=0)  # cap already exhausted from the first code onward
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster, cfg=cfg)
    normal_code = "N0RML-AAAAA-AAAAA-AAAAA-AAAAA"
    roundup_codes = _codes(6, prefix="R")
    normal_item = _item(normal_code, url="https://example.com/normal")
    roundup_item = _multi_code_item(roundup_codes, url="https://example.com/roundup")

    outcome = await process_items(deps, [normal_item, roundup_item], seeding_ok=True)

    assert outcome.posted == 1  # normal code still posts, just without a ping
    assert outcome.ping is False
    assert outcome.roundup_posted == 6
    with closing(connect(db_path)) as conn:
        state = repo.get_alert_state(conn)
    assert state.ping_count == 0  # neither the capped normal batch nor the roundup spent it


# --- exact 50 / 51 boundary (design.md §13, D3: MAX_ROUNDUP_CODES = 50) ---


def _roundup_candidate(code: str) -> CodeCandidate:
    return CodeCandidate(
        code=code,
        golden=False,
        source_name="Reddit Megathread",
        item_url="https://example.com/roundup",
        fresh=True,
        trusted=True,
        roundup=True,
    )


def test_exactly_fifty_fresh_roundup_codes_all_post_no_overflow():
    codes = _codes(50)
    candidates = [_roundup_candidate(c) for c in codes]
    plan = plan_alerts(
        candidates, known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )

    assert len(plan.roundup_to_post) == 50
    assert plan.silent == []


def test_exactly_fifty_one_fresh_roundup_codes_caps_at_fifty_with_one_silent():
    codes = _codes(51)
    candidates = [_roundup_candidate(c) for c in codes]
    plan = plan_alerts(
        candidates, known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )

    assert len(plan.roundup_to_post) == 50
    assert plan.silent == [(candidates[50], "roundup")]


async def test_exactly_fifty_one_fresh_roundup_codes_through_the_sweep_fires_one_cap_alert(
    db_path, http_client
):
    _seed(db_path)
    alerts: list[str] = []
    cfg = _cfg(max_codes_per_item=1)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster, cfg=cfg, alerts=alerts)
    codes = _codes(51)
    item = _multi_code_item(codes, url="https://example.com/big-roundup")

    outcome = await process_items(deps, [item], seeding_ok=True)

    assert outcome.roundup_posted == 50
    assert outcome.silent == 1
    cap_alerts = [a for a in alerts if "roundup" in a.lower() and "cap" in a.lower()]
    assert len(cap_alerts) == 1
    assert "1" in cap_alerts[0]


# --- roundup items older than max_item_age: too_old + from_roundup, never alerted ---


async def test_stale_roundup_only_code_is_recorded_too_old_with_from_roundup_set(
    db_path, http_client
):
    _seed(db_path)
    cfg = _cfg(max_item_age_hours=48)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster, cfg=cfg)
    codes = _codes(6)
    stale_item = _multi_code_item(codes, url="https://example.com/stale-roundup")
    stale_item = dataclasses.replace(stale_item, published_at=NOW - timedelta(hours=49))

    outcome = await process_items(deps, [stale_item], seeding_ok=True)

    assert outcome.roundup_posted == 0
    assert outcome.posted == 0
    assert poster.sent == []
    with closing(connect(db_path)) as conn:
        rows = _rows_for(conn, codes)
    assert all(status == "too_old" for status, _pinged, _roundup in rows.values())
    assert all(from_roundup == 1 for _status, _pinged, from_roundup in rows.values())


async def test_stale_roundup_code_never_alerts_even_on_a_later_fresh_sighting(db_path, http_client):
    # Once a code is recorded 'too_old' it's in `known`, so a later batch
    # that sees the *same* code again (fresh this time) must never post
    # it -- A11's "never gets another chance" rule.
    _seed(db_path)
    poster = _FakePoster()
    deps = _deps(db_path, http_client, poster=poster)
    codes = _codes(6)
    stale_item = _multi_code_item(codes, url="https://example.com/stale-roundup")
    stale_item = dataclasses.replace(stale_item, published_at=NOW - timedelta(hours=49))
    await process_items(deps, [stale_item], seeding_ok=True)

    fresh_item = _multi_code_item(codes, url="https://example.com/fresh-roundup-again")
    outcome = await process_items(deps, [fresh_item], seeding_ok=True)

    assert outcome.roundup_posted == 0
    assert outcome.posted == 0
    assert poster.sent == []
