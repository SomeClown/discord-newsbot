"""Tests for the hourly shared collection (plan task 4).

Collectors are fakes or real collectors over `httpx.MockTransport`; the
database is a temp file; the clock is a fake that only moves when somebody
sleeps. Nothing here touches the network or waits for real.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import CollectorResult, QuotaExceeded, RateLimitState, RawItem
from newsbot.config import GameCfg, Secrets, load_config
from newsbot.pipeline.collect import (
    SOURCE_FAILURE_ALERT_THRESHOLD,
    CollectionDeps,
    build_collection_collectors,
    collect_web_search,
    collection_job_options,
    estimate_pass_seconds,
    run_collection,
)
from newsbot.pipeline.lock import _run_lock
from newsbot.store import repo
from newsbot.store.db import connect, migrate

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)


class FakeClock:
    """A monotonic clock that only moves when `sleep` is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)  # let a competing pass run while we "wait"


class FakeCollector:
    def __init__(self, name, items=(), *, error=None, quota=False, key=None, clock=None):
        self.name = name
        self.source_type = "rss"
        self.rate_limit_key = key
        self._items = list(items)
        self._error = error
        self._quota = quota
        self.calls = 0
        self.fetched_at: list[float] = []
        self._clock = clock

    async def collect(self, http):
        self.calls += 1
        if self._clock is not None:
            self.fetched_at.append(self._clock())
        if self._quota:
            raise QuotaExceeded("quota")
        if self._error:
            raise RuntimeError(self._error)
        return list(self._items)


def item(url, title, *, topics=None, source="Src", trust="press", published=RECENT, excerpt=""):
    return RawItem(
        url=url,
        title=title,
        excerpt=excerpt,
        source_name=source,
        trust=trust,
        published_at=published,
        topics=topics,
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    return load_config(V3)


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def alerts():
    return []


def make_deps(cfg, db_path, http, collectors, alerts, **kw):
    async def alert(message: str) -> None:
        alerts.append(message)

    return CollectionDeps(
        cfg=cfg,
        db_path=db_path,
        http=http,
        collectors=collectors,
        now=lambda: NOW,
        alert=alert,
        **kw,
    )


def tags(db_path):
    """url -> {game: uncertain} for everything stored."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT i.url, t.topic_key, t.uncertain FROM items i "
            "LEFT JOIN item_topics t ON t.item_id = i.id"
        ).fetchall()
    out: dict[str, dict[str, bool]] = {}
    for row in rows:
        out.setdefault(row["url"], {})
        if row["topic_key"]:
            out[row["url"]][row["topic_key"]] = bool(row["uncertain"])
    return out


def health(db_path, name):
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM source_health WHERE source_name = ?", (name,)).fetchone()


# --- storing and tagging ---


async def test_items_are_stored_once_with_the_right_game_tags(cfg, db_path, http, alerts):
    steam = FakeCollector(
        "Borderlands 4 Steam",
        [item("https://ex.com/bl4-patch", "Patch notes", topics=("borderlands4",))],
    )
    press = FakeCollector(
        "PC Gamer",
        [
            item("https://ex.com/both", "Borderlands 4 and Palworld crossover"),
            item("https://ex.com/none", "Nothing relevant"),
        ],
    )
    outcome = await run_collection(make_deps(cfg, db_path, http, [steam, press], alerts))
    assert outcome.new_items == 2
    assert tags(db_path) == {
        "https://ex.com/bl4-patch": {"borderlands4": False},
        "https://ex.com/both": {"borderlands4": False, "palworld": False},
    }


async def test_each_source_is_fetched_once_per_pass(cfg, db_path, http, alerts):
    a = FakeCollector("A", [item("https://ex.com/1", "Palworld news")])
    await run_collection(make_deps(cfg, db_path, http, [a], alerts))
    assert a.calls == 1


async def test_shared_items_match_by_keyword_and_honor_match_name(cfg, db_path, http, alerts):
    shared = FakeCollector(
        "PC Gamer",
        [
            item("https://ex.com/rusty", "A rust-colored gate is in the news"),
            item("https://ex.com/facepunch", "Facepunch ships a patch"),
            item("https://ex.com/gearbox", "Gearbox is hiring"),
        ],
    )
    await run_collection(make_deps(cfg, db_path, http, [shared], alerts))
    stored = tags(db_path)
    assert "https://ex.com/rusty" not in stored  # name matching is off for rust
    assert stored["https://ex.com/facepunch"] == {"rust": False}  # alias
    assert stored["https://ex.com/gearbox"] == {"borderlands4": True}  # entity: uncertain


async def test_a_shared_source_games_restriction_is_respected(cfg, db_path, http, alerts):
    shared = FakeCollector(
        "2K Newsroom", [item("https://ex.com/pal", "Palworld", topics=("borderlands4",))]
    )
    await run_collection(make_deps(cfg, db_path, http, [shared], alerts))
    assert tags(db_path) == {"https://ex.com/pal": {"borderlands4": False}}


async def test_dedupe_across_passes(cfg, db_path, http, alerts):
    c = FakeCollector("A", [item("https://ex.com/1?utm_source=x", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [c], alerts)
    first = await run_collection(deps)
    second = await run_collection(deps)
    assert (first.new_items, second.new_items) == (1, 0)
    assert list(tags(db_path)) == ["https://ex.com/1"]


async def test_lookback_drops_old_items(cfg, db_path, http, alerts):
    old = item("https://ex.com/old", "Palworld old", published=NOW - timedelta(hours=30))
    undated = item("https://ex.com/undated", "Palworld undated", published=None)
    c = FakeCollector("A", [old, undated])
    await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert list(tags(db_path)) == ["https://ex.com/undated"]


async def test_the_per_game_cap_applies(cfg, db_path, http, alerts):
    small = cfg.model_copy(
        update={"collection": cfg.collection.model_copy(update={"max_items_per_game": 2})}
    )
    c = FakeCollector("A", [item(f"https://ex.com/{i}", "Palworld news") for i in range(5)])
    outcome = await run_collection(make_deps(small, db_path, http, [c], alerts))
    assert outcome.new_items == 2


# --- source health and the D8 alert ---


async def test_source_health_is_written_every_pass(cfg, db_path, http, alerts):
    good = FakeCollector("Good", [])
    bad = FakeCollector("Bad", error="boom")
    deps = make_deps(cfg, db_path, http, [good, bad], alerts)
    await run_collection(deps)
    await run_collection(deps)
    assert health(db_path, "Good")["consecutive_failures"] == 0
    assert health(db_path, "Good")["last_success_at"] is not None
    assert health(db_path, "Bad")["consecutive_failures"] == 2
    assert health(db_path, "Bad")["last_error"] == "boom"


async def test_the_owner_is_alerted_once_at_exactly_twelve_and_it_rearms(
    cfg, db_path, http, alerts
):
    bad = FakeCollector("Bad", error="boom")
    deps = make_deps(cfg, db_path, http, [bad], alerts)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD - 1):
        await run_collection(deps)
    assert alerts == []
    outcome = await run_collection(deps)
    assert outcome.alerted_sources == ("Bad",)
    assert len(alerts) == 1 and "'Bad'" in alerts[0] and "12" in alerts[0]
    for _ in range(5):
        await run_collection(deps)
    assert len(alerts) == 1  # once, not every hour after

    bad._error = None
    await run_collection(deps)
    assert health(db_path, "Bad")["consecutive_failures"] == 0
    bad._error = "boom again"
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD):
        await run_collection(deps)
    assert len(alerts) == 2


async def test_skipped_results_are_not_failures(cfg, db_path, http, alerts):
    quota = FakeCollector("Quota", quota=True)
    deps = make_deps(cfg, db_path, http, [quota], alerts)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD + 2):
        outcome = await run_collection(deps)
    assert alerts == []
    assert health(db_path, "Quota") is None
    assert outcome.sources_total == 0


async def test_every_source_failing_stores_nothing_and_raises_nothing(cfg, db_path, http, alerts):
    cs = [FakeCollector(f"Bad {i}", error="down") for i in range(4)]
    outcome = await run_collection(make_deps(cfg, db_path, http, cs, alerts))
    assert outcome.new_items == 0
    assert (outcome.sources_ok, outcome.sources_total) == (0, 4)
    assert len(outcome.failing_sources) == 4
    assert tags(db_path) == {}


async def test_a_failing_alert_does_not_take_the_pass_down(cfg, db_path, http):
    async def broken_alert(_message: str) -> None:
        raise RuntimeError("discord is down")

    bad = FakeCollector("Bad", error="boom")
    deps = make_deps(cfg, db_path, http, [bad], [])
    deps.alert = broken_alert
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD):
        await run_collection(deps)
    assert health(db_path, "Bad")["consecutive_failures"] == SOURCE_FAILURE_ALERT_THRESHOLD


# --- SHiFT hook and the sweep record ---


async def test_the_shift_hook_sees_every_collected_item_before_dedupe(cfg, db_path, http, alerts):
    seen: list[list[str]] = []

    async def hook(items, results):
        seen.append(sorted(i.url for i in items))
        return 1

    c = FakeCollector("A", [item("https://ex.com/1", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [c], alerts, shift_hook=hook)
    first = await run_collection(deps)
    await run_collection(deps)  # already stored: the hook still gets it
    assert seen == [["https://ex.com/1"], ["https://ex.com/1"]]
    assert first.new_codes == 1 and first.summary == "1/1 sources ok, 1 new items, 1 new code"


async def test_a_shift_hook_bug_does_not_cost_the_stored_items(cfg, db_path, http, alerts):
    async def hook(items, results):
        raise RuntimeError("regex on fire")

    c = FakeCollector("A", [item("https://ex.com/1", "Palworld news")])
    outcome = await run_collection(make_deps(cfg, db_path, http, [c], alerts, shift_hook=hook))
    assert outcome.new_items == 1
    assert len(alerts) == 1 and "SHiFT" in alerts[0]


async def test_the_pass_is_recorded(cfg, db_path, http, alerts):
    c = FakeCollector("A", [item("https://ex.com/1", "Palworld news")])
    await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    with closing(connect(db_path)) as conn:
        state = conn.execute(
            "SELECT value FROM alert_state WHERE key = 'last_sweep_summary'"
        ).fetchone()
    assert state["value"] == "1/1 sources ok, 1 new items, 0 new codes"


# --- the run lock ---


async def test_a_held_run_lock_skips_the_pass_and_writes_nothing(cfg, db_path, http, alerts):
    c = FakeCollector("A", [item("https://ex.com/1", "Palworld news")])
    async with _run_lock:
        outcome = await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert outcome.skipped
    assert c.calls == 0 and tags(db_path) == {} and health(db_path, "A") is None


# --- web search ---


def brave_transport(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://news.example/" + str(len(seen)),
                        "title": "A headline",
                        "description": "About " + request.url.params["q"],
                        "page_age": RECENT.isoformat(),
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


async def test_web_search_runs_only_for_the_given_games(cfg, db_path, alerts):
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave_transport(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts)

        async def no_sleep(_s: float) -> None:
            return None

        outcome = await collect_web_search(deps, ["palworld", "not-a-game"], "k", sleep=no_sleep)
    assert seen == ["Palworld news", "Palworld update OR patch OR leak"]
    assert outcome.new_items == 2
    assert set().union(*tags(db_path).values()) == {"palworld"}
    assert health(db_path, "Brave Search")["consecutive_failures"] == 0


async def test_web_search_with_no_games_or_no_key_does_nothing(cfg, db_path, alerts):
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave_transport(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts)
        assert (await collect_web_search(deps, [], "k")).sources_total == 0
        assert (await collect_web_search(deps, ["palworld"], None)).sources_total == 0
    assert seen == []


async def test_web_search_quota_is_a_skip_not_a_failure(cfg, db_path, alerts):
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await collect_web_search(
            make_deps(cfg, db_path, client, [], alerts), ["rust"], "k"
        )
    assert outcome.sources_total == 0 and health(db_path, "Brave Search") is None


async def test_run_collection_never_searches_the_web(cfg, db_path, alerts):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        return httpx.Response(200, text="<rss version='2.0'><channel></channel></rss>")

    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="x",
        brave_api_key="k",
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    collectors = build_collection_collectors(cfg, secrets)
    assert collectors and all(c.source_type != "web_search" for c in collectors)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_collection(make_deps(cfg, db_path, client, collectors, alerts))
    assert requested and "api.search.brave.com" not in requested


# --- pacing and overlap, with a 15-subreddit catalog and a fake clock ---


def fifteen_subreddits(cfg, clock):
    games = [GameCfg(key=f"game{i}", name=f"Game Number {i}") for i in range(15)]
    # Rotation off (every subreddit, every pass): this test is about pacing.
    collection = cfg.collection.model_copy(update={"reddit_rotation_hours": 1})
    cfg = cfg.model_copy(
        update={
            "catalog": games,
            "shared_sources": [],
            "shift": cfg.shift,
            "collection": collection,
        }
    )
    collectors = [
        FakeCollector(
            f"r/game{i}",
            [item(f"https://example.com/r/game{i}/1", "post", topics=(f"game{i}",))],
            key="reddit",
            clock=clock,
        )
        for i in range(15)
    ]
    return cfg, collectors


def test_the_estimate_for_fifteen_feeds():
    cs = [FakeCollector(f"r/{i}", key="reddit") for i in range(15)] + [FakeCollector("rss")]
    estimate = estimate_pass_seconds(cs)
    assert estimate.throttled_feeds == 15
    assert estimate.typical_s == 14 * 35 == 490
    assert estimate.worst_s == 35 + 15 * 20 + 14 * 35 == 825
    assert estimate_pass_seconds([FakeCollector("rss")]).throttled_feeds == 0


async def test_fifteen_subreddits_fit_comfortably_in_the_interval(cfg, db_path, http, alerts):
    clock = FakeClock()
    cfg15, collectors = fifteen_subreddits(cfg, clock)
    deps = make_deps(
        cfg15,
        db_path,
        http,
        collectors,
        alerts,
        sleep=clock.sleep,
        clock=clock,
        rate_limit_state=RateLimitState(),
    )
    outcome = await run_collection(deps)
    assert outcome.sources_ok == 15 and outcome.new_items == 15
    # 14 gaps of 35 seconds: a little over 8 minutes, and the clock agrees.
    assert clock.slept == [35.0] * 14
    assert outcome.duration_s == 490.0
    assert outcome.duration_s < cfg15.collection.interval_minutes * 60 * 0.5
    starts = sorted(t for c in collectors for t in c.fetched_at)
    assert all(b - a >= 35.0 for a, b in zip(starts, starts[1:], strict=False))

    # The gap carries into the next pass: its first feed waits out the rest.
    before = clock.now
    await run_collection(deps)
    assert clock.now - before == 490.0 + 35.0


async def test_a_pass_still_running_when_the_next_is_due_is_skipped_not_stacked(
    cfg, db_path, http, alerts
):
    clock = FakeClock()
    cfg15, collectors = fifteen_subreddits(cfg, clock)
    deps = make_deps(
        cfg15,
        db_path,
        http,
        collectors,
        alerts,
        sleep=clock.sleep,
        clock=clock,
        rate_limit_state=RateLimitState(),
    )
    first = asyncio.create_task(run_collection(deps))
    await asyncio.sleep(0)  # the first pass is now mid-way through its gaps
    assert not first.done()
    second = await run_collection(deps)  # the "next interval" arrives
    assert second.skipped
    done = await first
    assert not done.skipped
    assert all(c.calls == 1 for c in collectors)  # nothing was fetched twice


async def test_a_slow_pass_logs_a_warning(cfg, db_path, http, alerts, caplog):
    clock = FakeClock()

    class Slow:
        name = "slow feed"
        source_type = "rss"
        rate_limit_key = None

        async def collect(self, http):
            clock.now += 490.0  # what a pass of fifteen paced subreddits used to cost
            return []

    short = cfg.model_copy(
        update={"collection": cfg.collection.model_copy(update={"interval_minutes": 15})}
    )
    deps = make_deps(short, db_path, http, [Slow()], alerts, sleep=clock.sleep, clock=clock)
    with caplog.at_level("INFO", logger="newsbot.pipeline.collect"):
        await run_collection(deps)
    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert ("WARNING", "collection pass finished") in messages  # 490 s > half of 900 s


def test_job_options_say_never_stack(cfg):
    options = collection_job_options(cfg)
    assert options["max_instances"] == 1 and options["coalesce"] is True
    assert options["trigger"].interval == timedelta(minutes=60)


async def test_collector_results_type_is_what_the_hook_gets(cfg, db_path, http, alerts):
    got = []

    async def hook(items, results):
        got.extend(results)
        return 0

    c = FakeCollector("A", [])
    await run_collection(make_deps(cfg, db_path, http, [c], alerts, shift_hook=hook))
    assert isinstance(got[0], CollectorResult) and got[0].source_name == "A"


def test_store_items_reports_new_rows_and_keeps_save_run_atomic(db_path):
    from newsbot.store.models import StoredItem

    it = StoredItem("https://ex.com/a", "t", "e", "s", "press", NOW, {"palworld": False})
    again = StoredItem("https://ex.com/a", "t", "e", "s", "press", NOW, {"rust": True})
    with closing(connect(db_path)) as conn:
        assert repo.store_items(conn, [it], lambda: NOW) == 1
        assert repo.store_items(conn, [again], lambda: NOW) == 0
    assert tags(db_path) == {"https://ex.com/a": {"palworld": False, "rust": True}}
