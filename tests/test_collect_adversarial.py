"""Adversarial tests for the hourly shared collection (plan task 4).

The implementer's tests prove the happy path and the headline numbers. These
go looking for the seams: feeds that all hang, passes that die or get
cancelled halfway, the same story arriving twice in different outfits, a
health counter that gets poked in an awkward order, and the two copies of
the stored-item builder that are supposed to stay identical until task 13
retires one of them. Where the code does something the owner might not
expect, the test pins it and says so, so changing it later is a decision and
not an accident. Real bugs are `xfail(strict=True)`, which is the test suite
raising its hand politely.

No network, no real sleeping: fakes, `MockTransport`, and a clock that only
moves when somebody asks it to.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import QuotaExceeded, RateLimitState, RawItem
from newsbot.config import GameCfg, load_config
from newsbot.pipeline import collect as collect_mod
from newsbot.pipeline.collect import (
    SOURCE_FAILURE_ALERT_THRESHOLD,
    CollectionDeps,
    _to_stored_items,
    collect_web_search,
    collection_job_options,
    estimate_pass_seconds,
    run_collection,
)
from newsbot.pipeline.filter import TopicItem, filter_items
from newsbot.pipeline.lock import _run_lock
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)


class FakeClock:
    """A monotonic clock that only moves when `sleep` is called (or a collector says so)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Scripted:
    """A collector whose behavior can be changed between passes."""

    def __init__(self, name, items=(), *, key=None, cost=0.0, clock=None):
        self.name = name
        self.source_type = "rss"
        self.rate_limit_key = key
        self.items = list(items)
        self.error: str | None = None
        self.quota = False
        self.hang = False
        self.cost = cost
        self.clock = clock
        self.calls = 0

    async def collect(self, http):
        self.calls += 1
        if self.clock is not None:
            self.clock.now += self.cost
        if self.hang:
            await asyncio.sleep(3600)
        if self.quota:
            raise QuotaExceeded("quota")
        if self.error:
            raise RuntimeError(self.error)
        return list(self.items)


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


@pytest.fixture
def no_rotation(monkeypatch):
    """Every Reddit source every pass: these tests are about pacing and overlap, not rotation."""
    monkeypatch.setattr(
        collect_mod,
        "plan_reddit",
        lambda collectors, *_a: collect_mod.RedditPlan(list(collectors), 0),
    )


def with_collection(cfg, **changes):
    return cfg.model_copy(update={"collection": cfg.collection.model_copy(update=changes)})


def tags(db_path):
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


def sweep_summary(db_path):
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM alert_state WHERE key = 'last_sweep_summary'"
        ).fetchone()
    return row["value"] if row else None


def reddit_feeds(clock, n, *, cost=0.0):
    return [
        Scripted(
            f"r/game{i}",
            [item(f"https://example.com/r/game{i}/1", "post", topics=("palworld",))],
            key="reddit",
            cost=cost,
            clock=clock,
        )
        for i in range(n)
    ]


def clocked_deps(cfg, db_path, http, collectors, alerts, clock, **kw):
    return make_deps(
        cfg,
        db_path,
        http,
        collectors,
        alerts,
        sleep=clock.sleep,
        clock=clock,
        rate_limit_state=RateLimitState(),
        **kw,
    )


# --- pacing and overlap under stress ---


async def test_fifteen_reddit_feeds_that_all_time_out_still_finish_and_get_counted(
    no_rotation, cfg, db_path, http, alerts, monkeypatch
):
    monkeypatch.setattr(collect_mod, "_COLLECT_TIMEOUT_S", 0.01)
    clock = FakeClock()
    feeds = reddit_feeds(clock, 15)
    for feed in feeds:
        feed.hang = True
    deps = clocked_deps(cfg, db_path, http, feeds, alerts, clock)

    outcome = await run_collection(deps)

    assert (outcome.sources_ok, outcome.sources_total, outcome.new_items) == (0, 15, 0)
    assert clock.slept == [35.0] * 14  # the first feed in a fresh process doesn't wait
    assert all(health(db_path, f.name)["consecutive_failures"] == 1 for f in feeds)
    assert "timed out" in health(db_path, "r/game0")["last_error"]
    assert sweep_summary(db_path) == "0/15 sources ok, 0 new items, 0 new codes"
    assert alerts == []
    assert not _run_lock.locked()

    # The gap is owed across passes even when every fetch timed out.
    await run_collection(deps)
    assert clock.slept[14] == 35.0


async def test_three_passes_due_at_once_run_exactly_one_and_fetch_nothing_twice(
    no_rotation, cfg, db_path, http, alerts
):
    clock = FakeClock()
    feeds = reddit_feeds(clock, 15)
    deps = clocked_deps(cfg, db_path, http, feeds, alerts, clock)

    outcomes = await asyncio.gather(*(run_collection(deps) for _ in range(3)))

    assert sorted(o.skipped for o in outcomes) == [False, True, True]
    assert all(f.calls == 1 for f in feeds)
    assert not _run_lock.locked()


async def test_a_pass_that_crosses_the_next_interval_boundary_skips_that_tick_and_warns(
    no_rotation, cfg, db_path, http, alerts, caplog
):
    short = with_collection(cfg, interval_minutes=15)
    clock = FakeClock()
    # 20 feeds: 19 gaps of 35 s plus 100 s of "fetching" each is well over 15 minutes.
    feeds = reddit_feeds(clock, 20, cost=100.0)
    deps = clocked_deps(short, db_path, http, feeds, alerts, clock)

    with caplog.at_level("INFO", logger="newsbot.pipeline.collect"):
        first = asyncio.create_task(run_collection(deps))
        await asyncio.sleep(0)
        assert not first.done()
        tick = await run_collection(deps)  # the next interval arrives mid-pass
        done = await first
        after = await run_collection(deps)  # and the one after that is fine

    assert tick.skipped and not done.skipped and not after.skipped
    assert done.duration_s > 15 * 60
    assert all(f.calls == 2 for f in feeds)  # first + after, never the skipped tick
    levels = {(r.levelname, r.getMessage()) for r in caplog.records}
    assert ("WARNING", "collection pass finished") in levels
    assert ("WARNING", "worst-case pass length exceeds the interval") in levels


async def test_fifteen_feeds_at_the_minimum_interval_do_not_trip_the_worst_case_warning(
    cfg, db_path, http, alerts, caplog
):
    minimum = with_collection(cfg, interval_minutes=15)
    clock = FakeClock()
    deps = clocked_deps(minimum, db_path, http, reddit_feeds(clock, 15), alerts, clock)
    with caplog.at_level("INFO", logger="newsbot.pipeline.collect"):
        await run_collection(deps)
    messages = [r.getMessage() for r in caplog.records]
    assert "worst-case pass length exceeds the interval" not in messages  # 825 s < 900 s


async def test_a_crashed_pass_does_not_hold_the_lock_forever(
    cfg, db_path, http, alerts, monkeypatch
):
    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [feed], alerts)

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(collect_mod, "store_items", boom)
    with pytest.raises(RuntimeError, match="disk on fire"):
        await run_collection(deps)
    assert not _run_lock.locked()
    assert tags(db_path) == {}

    monkeypatch.undo()
    outcome = await run_collection(deps)
    assert not outcome.skipped and outcome.new_items == 1  # the lost items are simply refetched


async def test_a_pass_cancelled_mid_fetch_releases_the_lock_and_leaves_the_db_clean(
    no_rotation, cfg, db_path, http, alerts
):
    clock = FakeClock()
    feeds = reddit_feeds(clock, 15)
    deps = clocked_deps(cfg, db_path, http, feeds, alerts, clock)

    task = asyncio.create_task(run_collection(deps))
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _run_lock.locked()
    assert tags(db_path) == {}
    assert health(db_path, "r/game0") is None
    assert sweep_summary(db_path) is None

    outcome = await run_collection(deps)
    assert not outcome.skipped and outcome.new_items == 15


async def test_a_pass_cancelled_inside_the_hook_keeps_what_it_stored_and_frees_the_lock(
    cfg, db_path, http, alerts
):
    started = asyncio.Event()

    async def hook(items, results):
        started.set()
        await asyncio.Event().wait()
        return 0

    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    task = asyncio.create_task(
        run_collection(make_deps(cfg, db_path, http, [feed], alerts, shift_hook=hook))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _run_lock.locked()
    assert list(tags(db_path)) == ["https://ex.com/1"]  # the store committed before the hook
    assert sweep_summary(db_path) is None  # but the pass never got to say it finished
    assert alerts == []  # cancellation isn't a "SHiFT detection failed"


async def test_a_slow_hook_holds_the_lock_so_the_next_pass_skips(cfg, db_path, http, alerts):
    release = asyncio.Event()
    started = asyncio.Event()

    async def hook(items, results):
        started.set()
        await release.wait()
        return 2

    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [feed], alerts, shift_hook=hook)
    first = asyncio.create_task(run_collection(deps))
    await started.wait()
    assert (await run_collection(deps)).skipped
    release.set()
    outcome = await first
    assert outcome.new_codes == 2 and feed.calls == 1
    assert sweep_summary(db_path) == "1/1 sources ok, 1 new items, 2 new codes"


def test_estimate_edge_cases():
    none = estimate_pass_seconds([])
    assert (none.throttled_feeds, none.typical_s, none.worst_s) == (0, 0.0, 20.0)

    one = estimate_pass_seconds([Scripted("r/a", key="reddit")])
    assert (one.throttled_feeds, one.typical_s, one.worst_s) == (1, 0.0, 35.0 + 20.0)

    # Only the biggest group sets the pace; a None key isn't a group at all.
    mixed = [Scripted(f"r/{i}", key="reddit") for i in range(3)]
    mixed += [Scripted(f"x/{i}", key="other") for i in range(5)]
    mixed += [Scripted(f"rss{i}") for i in range(50)]
    assert estimate_pass_seconds(mixed).throttled_feeds == 5

    custom = estimate_pass_seconds(
        [Scripted("a", key="k"), Scripted("b", key="k")], gap_s=10, timeout_s=5
    )
    assert (custom.typical_s, custom.worst_s) == (10.0, 10 + 2 * 5 + 10)


def test_job_options_at_the_interval_extremes(cfg):
    for minutes in (15, 1440):
        options = collection_job_options(with_collection(cfg, interval_minutes=minutes))
        assert options["trigger"].interval == timedelta(minutes=minutes)
        assert options["misfire_grace_time"] == minutes * 30  # half the interval, in seconds
        assert options["max_instances"] == 1 and options["coalesce"] is True


# --- dedupe and tagging ---


async def test_the_same_story_from_two_sources_in_one_pass_is_stored_once_official_wins(
    cfg, db_path, http, alerts
):
    press = Scripted(
        "Press", [item("https://www.ex.com/x/?utm_source=a", "Palworld patch", source="Press")]
    )
    official = Scripted(
        "Official",
        [item("https://ex.com/x#top", "Palworld patch", source="Official", trust="official")],
    )
    outcome = await run_collection(make_deps(cfg, db_path, http, [press, official], alerts))
    assert outcome.new_items == 1
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT url, source_name, trust FROM items").fetchall()
    assert [tuple(r) for r in rows] == [("https://ex.com/x", "Official", "official")]


async def test_a_duplicate_that_loses_the_trust_contest_still_contributes_its_game_tags(
    cfg, db_path, http, alerts
):
    # Changed from a pin (the losing copy's palworld tag used to vanish with
    # it): a community feed dedicated to palworld and an official shared feed
    # that only mentions Borderlands 4 both carry one URL. The official copy's
    # content wins, and the stored item carries both tags.
    community = Scripted(
        "Reddit", [item("https://ex.com/x", "Crossover", topics=("palworld",), trust="community")]
    )
    official = Scripted(
        "Official",
        [
            item(
                "https://ex.com/x?ref=feed",
                "Borderlands 4 crossover",
                source="Official",
                trust="official",
            )
        ],
    )
    await run_collection(make_deps(cfg, db_path, http, [community, official], alerts))
    assert tags(db_path) == {"https://ex.com/x": {"borderlands4": False, "palworld": False}}
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT source_name, trust FROM items").fetchall()
    assert [tuple(r) for r in rows] == [("Official", "official")]


async def test_a_losing_copy_that_matches_a_game_the_winner_does_not_still_stores_the_item(
    cfg, db_path, http, alerts
):
    # The winner (official, shared, no game named) matches nothing on its own;
    # the dedicated community copy is the only thing that tags it.
    community = Scripted(
        "Reddit", [item("https://ex.com/y", "Crossover", topics=("palworld",), trust="community")]
    )
    official = Scripted(
        "Official", [item("https://ex.com/y", "Something", source="Official", trust="official")]
    )
    outcome = await run_collection(make_deps(cfg, db_path, http, [community, official], alerts))
    assert outcome.new_items == 1
    assert tags(db_path) == {"https://ex.com/y": {"palworld": False}}
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT source_name FROM items").fetchone()[0] == "Official"


async def test_a_confident_losing_copy_upgrades_an_uncertain_winning_tag(
    cfg, db_path, http, alerts
):
    official = Scripted(
        "Official", [item("https://ex.com/z", "Take-Two earnings", trust="official")]
    )
    community = Scripted(
        "Reddit", [item("https://ex.com/z", "Borderlands 4 thread", trust="community")]
    )
    await run_collection(make_deps(cfg, db_path, http, [official, community], alerts))
    assert tags(db_path)["https://ex.com/z"].get("borderlands4") is False


@pytest.mark.parametrize(
    "variant",
    [
        "https://ex.com/story?fbclid=abc",
        "https://www.ex.com/story/",
        "https://EX.com:443/story#comments",
        "https://ex.com/story?utm_medium=x&utm_campaign=y",
    ],
)
async def test_tracking_and_cosmetic_variants_are_the_same_story_across_passes(
    cfg, db_path, http, alerts, variant
):
    c = Scripted("A", [item("https://ex.com/story", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [c], alerts)
    await run_collection(deps)
    c.items = [item(variant, "Palworld news")]
    second = await run_collection(deps)
    assert second.new_items == 0
    assert list(tags(db_path)) == ["https://ex.com/story"]


async def test_pin_an_already_stored_story_never_gains_a_tag_from_a_later_pass(
    cfg, db_path, http, alerts
):
    # Behavior the owner may want changed: `store_items` can add a tag to a
    # stored item, but normalize() throws already-stored URLs away before
    # that point, so the late tag never arrives.
    c = Scripted("Shared", [item("https://ex.com/x", "Borderlands 4 patch")])
    deps = make_deps(cfg, db_path, http, [c], alerts)
    await run_collection(deps)
    c.items = [item("https://ex.com/x", "Borderlands 4 patch", topics=("palworld",))]
    await run_collection(deps)
    assert tags(db_path) == {"https://ex.com/x": {"borderlands4": False}}


async def test_a_game_restriction_with_two_games_still_needs_a_keyword_hit(
    cfg, db_path, http, alerts
):
    c = Scripted(
        "2K", [item("https://ex.com/a", "Palworld only", topics=("borderlands4", "palworld"))]
    )
    await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert tags(db_path) == {"https://ex.com/a": {"palworld": False}}


async def test_an_item_restricted_to_an_unknown_game_is_dropped(cfg, db_path, http, alerts):
    c = Scripted("Odd", [item("https://ex.com/a", "Palworld", topics=("ghost",))])
    outcome = await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert outcome.new_items == 0 and tags(db_path) == {}


async def test_an_item_matching_several_games_is_one_row_with_every_tag(cfg, db_path, http, alerts):
    c = Scripted("PC Gamer", [item("https://ex.com/a", "Borderlands 4, Palworld, Facepunch")])
    outcome = await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert outcome.new_items == 1
    assert tags(db_path) == {
        "https://ex.com/a": {"borderlands4": False, "palworld": False, "rust": False}
    }
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


async def test_match_name_false_ignores_prose_but_takes_aliases_and_dedicated_feeds(
    cfg, db_path, http, alerts
):
    c = Scripted(
        "Mixed",
        [
            item("https://ex.com/prose", "RUST and dust everywhere"),
            item("https://ex.com/alias", "new RUST GAME update"),
            item("https://ex.com/dedicated", "Quiet patch", topics=("rust",)),
        ],
    )
    await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert tags(db_path) == {
        "https://ex.com/alias": {"rust": False},
        "https://ex.com/dedicated": {"rust": False},
    }


async def test_the_cap_is_per_game_and_a_capped_out_tag_leaves_the_others_alone(
    cfg, db_path, http, alerts
):
    tiny = with_collection(cfg, max_items_per_game=1)
    c = Scripted(
        "Mixed",
        [
            item("https://ex.com/both", "Borderlands 4 and Palworld", trust="press"),
            item("https://ex.com/pal", "Palworld alone", trust="official"),
            item("https://ex.com/pal2", "Palworld again", trust="community"),
        ],
    )
    outcome = await run_collection(make_deps(tiny, db_path, http, [c], alerts))
    # Palworld keeps only its official item; "both" survives for borderlands4 alone.
    assert tags(db_path) == {
        "https://ex.com/both": {"borderlands4": False},
        "https://ex.com/pal": {"palworld": False},
    }
    assert outcome.new_items == 2


async def test_an_item_capped_out_of_every_game_is_not_stored_at_all(cfg, db_path, http, alerts):
    tiny = with_collection(cfg, max_items_per_game=1)
    c = Scripted(
        "A",
        [
            item("https://ex.com/1", "Palworld one", trust="official"),
            item("https://ex.com/2", "Palworld two", trust="community"),
        ],
    )
    await run_collection(make_deps(tiny, db_path, http, [c], alerts))
    assert list(tags(db_path)) == ["https://ex.com/1"]


async def test_the_lookback_boundary_is_inclusive_and_the_future_is_fine(
    cfg, db_path, http, alerts
):
    c = Scripted(
        "A",
        [
            item("https://ex.com/edge", "Palworld edge", published=NOW - timedelta(hours=24)),
            item(
                "https://ex.com/late",
                "Palworld late",
                published=NOW - timedelta(hours=24, seconds=1),
            ),
            item("https://ex.com/future", "Palworld future", published=NOW + timedelta(days=2)),
        ],
    )
    await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert sorted(tags(db_path)) == ["https://ex.com/edge", "https://ex.com/future"]


async def test_a_non_http_url_is_dropped_without_hurting_its_neighbors(cfg, db_path, http, alerts):
    c = Scripted(
        "A",
        [
            item("javascript:alert(1)", "Palworld bad"),
            item("https://user:pw@ex.com/x", "Palworld creds"),
            item("https://ex.com/ok", "Palworld fine"),
        ],
    )
    outcome = await run_collection(make_deps(cfg, db_path, http, [c], alerts))
    assert outcome.new_items == 1 and list(tags(db_path)) == ["https://ex.com/ok"]


# --- equivalence of the two stored-item builders ---


def odd_items():
    return [
        item("https://ex.com/empty", "", source="", published=None, topics=("palworld",)),
        item(
            "https://ex.com/uni",
            "Palworld Ünïcödé ✓ \U0001f3ae ‮ rtl",
            excerpt="中文 " * 50,
            source="Söurce",
            trust="official",
        ),
        item("https://ex.com/huge", "Borderlands 4", excerpt="x" * 200_000, trust="community"),
        item("https://ex.com/multi", "Borderlands 4 Palworld Gearbox Facepunch", published=None),
        item("https://ex.com/ded2", "Palworld", topics=("palworld", "rust")),
        item("https://ex.com/ent", "Gearbox news", source="Ent"),
    ]


def _expected_rows(grouped):
    """What `_to_stored_items` should make of `grouped`, worked out the long way round.

    One row per URL (the last item seen for it wins its content, as v2's own builder had it),
    carrying every game that matched it and how confident each match was.
    """
    last: dict[str, RawItem] = {}
    topics: dict[str, dict[str, bool]] = {}
    for key, topic_items in grouped.items():
        for ti in topic_items:
            last[ti.item.url] = ti.item
            topics.setdefault(ti.item.url, {})[key] = ti.uncertain
    return [
        StoredItem(
            url=url,
            title=it.title,
            excerpt=it.excerpt,
            source_name=it.source_name,
            trust=it.trust,
            published_at=it.published_at,
            topics=topics[url],
        )
        for url, it in last.items()
    ]


# These three began as "`_to_stored_items` matches run.py's builder" (v2's `_build_stored_items`
# was the same logic in the daily run). That builder retired with `run_daily`; the expected
# rows are computed independently above instead, which is a stricter test than "two copies agree".


def test_to_stored_items_on_filtered_odd_items(cfg):
    grouped = filter_items(odd_items(), cfg.catalog, 60)
    assert grouped  # the battery matched something
    assert _to_stored_items(grouped) == _expected_rows(grouped)


def test_to_stored_items_on_handbuilt_groupings():
    a = item("https://ex.com/a", "t1", source="one")
    a_again = item("https://ex.com/a", "t2", source="two")  # same url, different object
    b = item("https://ex.com/b", "t3")
    grouped = {
        "g1": [TopicItem(a, "g1", False), TopicItem(b, "g1", True)],
        "g2": [TopicItem(a_again, "g2", True)],
        "empty": [],
    }
    rows = _to_stored_items(grouped)
    assert rows == _expected_rows(grouped)
    assert [(r.url, r.title, r.topics) for r in rows] == [
        ("https://ex.com/a", "t2", {"g1": False, "g2": True}),  # two games, the later copy's text
        ("https://ex.com/b", "t3", {"g1": True}),
    ]
    assert _to_stored_items({}) == []


def dump(db_path):
    with closing(connect(db_path)) as conn:
        items = conn.execute(
            "SELECT url, title, excerpt, source_name, trust, published_at, collected_at "
            "FROM items ORDER BY url"
        ).fetchall()
        topics = conn.execute(
            "SELECT i.url, t.topic_key, t.uncertain FROM item_topics t "
            "JOIN items i ON i.id = t.item_id ORDER BY i.url, t.topic_key"
        ).fetchall()
    return [tuple(r) for r in items], [tuple(r) for r in topics]


def test_the_stored_rows_are_exactly_the_items_the_filter_kept(cfg, tmp_path):
    grouped = filter_items(odd_items(), cfg.catalog, 60)
    path = str(tmp_path / "a.db")
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.store_items(conn, _to_stored_items(grouped), lambda: NOW)
    items, topics = dump(path)
    expected = _expected_rows(grouped)
    assert items == sorted(
        (
            r.url,
            r.title,
            r.excerpt,
            r.source_name,
            r.trust,
            r.published_at.isoformat() if r.published_at else None,
            NOW.isoformat(),
        )
        for r in expected
    )
    assert topics == sorted(
        (r.url, key, int(unsure)) for r in expected for key, unsure in r.topics.items()
    )
    assert items  # and it wasn't two empty databases agreeing


# --- store_items ---


def test_store_items_is_atomic_when_one_item_is_bad(db_path):
    good = StoredItem("https://ex.com/good", "t", "e", "s", "press", NOW, {"palworld": False})
    bad = StoredItem("https://ex.com/bad", "t", "e", "s", "bogus", NOW, {"palworld": False})
    with closing(connect(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            repo.store_items(conn, [good, bad], lambda: NOW)
    assert tags(db_path) == {}


def test_store_items_counts_a_url_repeated_in_one_call_once(db_path):
    a = StoredItem("https://ex.com/a", "t", "e", "s", "press", NOW, {"palworld": False})
    b = StoredItem("https://ex.com/a", "t", "e", "s", "press", NOW, {"rust": False})
    with closing(connect(db_path)) as conn:
        assert repo.store_items(conn, [a, b], lambda: NOW) == 1
        assert repo.store_items(conn, [], lambda: NOW) == 0
    assert tags(db_path) == {"https://ex.com/a": {"palworld": False, "rust": False}}


def test_store_items_count_ignores_a_concurrent_writers_rows(db_path):
    mine = StoredItem("https://ex.com/mine", "t", "e", "s", "press", NOW, {"palworld": False})

    with closing(connect(db_path)) as conn, closing(connect(db_path)) as rival:
        real_execute = conn.execute
        state = {"armed": True}

        class Proxy:
            def __enter__(self):
                return conn.__enter__()

            def __exit__(self, *exc):
                return conn.__exit__(*exc)

            def execute(self, sql, *args):
                # The rival writes just before our first insert, which is after
                # any "how many rows are there now" read the old COUNT-based
                # code did, and before any read-after it did. (Changed with the
                # fix: the old trigger keyed on SELECT COUNT(*), which the code
                # no longer runs, so it would have passed without testing anything.)
                if state["armed"] and sql.startswith("INSERT INTO items"):
                    state["armed"] = False
                    with rival:
                        rival.execute(
                            "INSERT INTO items (url, title, excerpt, source_name, trust, "
                            "published_at, collected_at) VALUES "
                            "('https://ex.com/theirs', 't', 'e', 's', 'press', NULL, 'x')"
                        )
                return real_execute(sql, *args)

        assert repo.store_items(Proxy(), [mine], lambda: NOW) == 1  # type: ignore[arg-type]


# --- source health and the alert ---


@pytest.mark.parametrize(
    ("pattern", "expected_alerts"),
    [
        ("F" * 11, 0),
        ("F" * 11 + "S" + "F" * 11, 0),
        ("F" * 11 + "S" + "F" * 12, 1),
        ("F" * 12, 1),
        ("F" * 13, 1),
        ("F" * 24, 1),
        ("F" * 36, 1),
        ("F" * 12 + "S" + "F" * 12, 2),
    ],
)
async def test_the_alert_fires_only_when_the_streak_passes_through_twelve(
    cfg, db_path, http, alerts, pattern, expected_alerts
):
    bad = Scripted("Bad")
    deps = make_deps(cfg, db_path, http, [bad], alerts)
    for step in pattern:
        bad.error = "boom" if step == "F" else None
        await run_collection(deps)
    assert len(alerts) == expected_alerts


async def test_quota_skips_neither_reset_nor_extend_a_failure_streak(cfg, db_path, http, alerts):
    bad = Scripted("Bad")
    deps = make_deps(cfg, db_path, http, [bad], alerts)
    bad.error = "boom"
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD - 1):
        await run_collection(deps)
    bad.quota = True
    for _ in range(5):
        await run_collection(deps)
    assert health(db_path, "Bad")["consecutive_failures"] == SOURCE_FAILURE_ALERT_THRESHOLD - 1
    assert alerts == []
    bad.quota = False
    await run_collection(deps)
    assert len(alerts) == 1


async def test_two_sources_crossing_the_threshold_together_each_get_one_alert(
    cfg, db_path, http, alerts
):
    a, b = Scripted("A", []), Scripted("B", [])
    a.error = b.error = "down"
    deps = make_deps(cfg, db_path, http, [a, b], alerts)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD - 1):
        await run_collection(deps)
    outcome = await run_collection(deps)
    assert sorted(outcome.alerted_sources) == ["A", "B"]
    assert len(alerts) == 2


async def test_a_health_write_failure_does_not_abort_the_pass(
    cfg, db_path, http, alerts, monkeypatch
):
    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(collect_mod, "record_source_result", locked)
    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    outcome = await run_collection(make_deps(cfg, db_path, http, [feed], alerts))
    assert outcome.new_items == 1


async def test_a_raising_alert_still_leaves_the_items_and_the_outcome_intact(cfg, db_path, http):
    async def broken(_message: str) -> None:
        raise RuntimeError("discord is down")

    bad = Scripted("Bad")
    bad.error = "boom"
    good = Scripted("Good", [item("https://ex.com/1", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [bad, good], [])
    deps.alert = broken
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD - 1):
        await run_collection(deps)
    outcome = await run_collection(deps)
    assert outcome.alerted_sources == ("Bad",)
    assert list(tags(db_path)) == ["https://ex.com/1"]
    assert sweep_summary(db_path) is not None


async def test_the_alert_is_one_line_and_carries_no_error_text_or_url(cfg, db_path, http, alerts):
    bad = Scripted("Bad\nFeed")
    bad.error = "https://h.example/feed?token=SECRET\n<html>raw response body</html>"
    deps = make_deps(cfg, db_path, http, [bad], alerts)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD):
        await run_collection(deps)
    (message,) = alerts
    assert "\n" not in message
    assert "SECRET" not in message and "html" not in message and "h.example" not in message
    assert "SECRET" not in sweep_summary(db_path)


async def test_the_alert_is_capped_for_an_absurdly_long_source_name(cfg, db_path, http, alerts):
    bad = Scripted("N" * 5000)
    bad.error = "boom"
    deps = make_deps(cfg, db_path, http, [bad], alerts)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD):
        await run_collection(deps)
    assert len(alerts[0]) <= 2000


def test_source_names_are_health_keys_so_config_refuses_a_duplicate(tmp_path, monkeypatch):
    # Two sources sharing a name would share one source_health row (one
    # failing, one fine, the streak would never reach 12), so load_config is
    # the guard. Pin that it stays one.
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    text = V3.read_text().replace('name: "2K Newsroom"', 'name: "PC Gamer"')
    path = tmp_path / "dup.yaml"
    path.write_text(text)
    with pytest.raises(Exception, match="duplicate source name"):
        load_config(path)


async def test_same_named_collectors_share_one_health_row_and_mask_each_other(
    cfg, db_path, http, alerts
):
    # What config validation protects us from, shown directly: a healthy and a
    # failing source under one name never build a streak, so no alert ever fires.
    bad = Scripted("Twin")
    bad.error = "down"
    fine = Scripted("Twin", [])
    deps = make_deps(cfg, db_path, http, [bad, fine], alerts)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD * 2):
        await run_collection(deps)
    assert alerts == []
    assert health(db_path, "Twin")["consecutive_failures"] == 0


# --- the SHiFT hook ---


async def test_the_hook_sees_raw_items_duplicates_and_all_results_but_not_a_skipped_pass(
    cfg, db_path, http, alerts
):
    seen: list[tuple[list[str], list[str]]] = []

    async def hook(items, results):
        seen.append(([i.url for i in items], [r.source_name for r in results]))
        return 0

    a = Scripted("A", [item("https://ex.com/1?utm_source=x", "Palworld news")])
    b = Scripted("B", [item("https://ex.com/1", "Palworld news")])
    old = Scripted(
        "Old", [item("https://ex.com/old", "Palworld", published=NOW - timedelta(days=9))]
    )
    nothing = Scripted("Nothing", [item("https://ex.com/none", "No game here")])
    down = Scripted("Down")
    down.error = "boom"
    quota = Scripted("Quota")
    quota.quota = True
    deps = make_deps(cfg, db_path, http, [a, b, old, nothing, down, quota], alerts, shift_hook=hook)

    await run_collection(deps)
    async with _run_lock:
        await run_collection(deps)  # skipped: the hook must not run

    assert len(seen) == 1
    urls, sources = seen[0]
    # Raw: tracking params intact, the duplicate twice, stale and unmatched included.
    assert sorted(urls) == sorted(
        [
            "https://ex.com/1?utm_source=x",
            "https://ex.com/1",
            "https://ex.com/old",
            "https://ex.com/none",
        ]
    )
    assert sorted(sources) == ["A", "B", "Down", "Nothing", "Old", "Quota"]


async def test_the_hook_runs_after_health_and_store_and_before_the_sweep_is_recorded(
    cfg, db_path, http, alerts
):
    snapshot = {}

    async def hook(items, results):
        snapshot["tags"] = tags(db_path)
        snapshot["health"] = health(db_path, "A") is not None
        snapshot["sweep"] = sweep_summary(db_path)
        return 3

    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    outcome = await run_collection(make_deps(cfg, db_path, http, [feed], alerts, shift_hook=hook))
    assert snapshot == {
        "tags": {"https://ex.com/1": {"palworld": False}},
        "health": True,
        "sweep": None,
    }
    assert outcome.new_codes == 3
    assert sweep_summary(db_path) == "1/1 sources ok, 1 new items, 3 new codes"


async def test_the_hook_is_called_even_when_every_source_failed(cfg, db_path, http, alerts):
    calls = []

    async def hook(items, results):
        calls.append((len(items), len(results)))
        return 0

    down = Scripted("Down")
    down.error = "boom"
    await run_collection(make_deps(cfg, db_path, http, [down], alerts, shift_hook=hook))
    assert calls == [(0, 1)]


async def test_a_hook_that_trims_its_input_cannot_change_what_was_stored(
    cfg, db_path, http, alerts
):
    async def hook(items, results):
        items.clear()
        return 0

    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    await run_collection(make_deps(cfg, db_path, http, [feed], alerts, shift_hook=hook))
    assert list(tags(db_path)) == ["https://ex.com/1"]


async def test_a_persistently_broken_hook_alerts_once_and_re_arms_after_a_good_pass(
    cfg, db_path, http, alerts
):
    # Changed from a pin (it used to alert every pass, 24 pings a day): like
    # source health, the owner hears once, and a success re-arms it.
    broken = {"on": True}

    async def hook(items, results):
        if broken["on"]:
            raise ValueError("regex on fire")
        return 0

    feed = Scripted("A", [])
    deps = make_deps(cfg, db_path, http, [feed], alerts, shift_hook=hook)
    for _ in range(3):
        outcome = await run_collection(deps)
        assert outcome.new_codes == 0
    assert len(alerts) == 1 and sweep_summary(db_path) is not None
    broken["on"] = False
    await run_collection(deps)
    broken["on"] = True
    await run_collection(deps)
    assert len(alerts) == 2


async def test_a_hung_hook_times_out_and_frees_the_lock_but_keeps_the_items(
    cfg, db_path, http, alerts, monkeypatch
):
    monkeypatch.setattr(collect_mod, "_SHIFT_HOOK_TIMEOUT_S", 0.05)

    async def hook(items, results):
        await asyncio.sleep(3600)
        return 1

    feed = Scripted("A", [item("https://ex.com/1", "Palworld news")])
    deps = make_deps(cfg, db_path, http, [feed], alerts, shift_hook=hook)
    outcome = await run_collection(deps)
    assert outcome.new_codes == 0 and outcome.new_items == 1
    assert len(alerts) == 1 and sweep_summary(db_path) is not None
    assert list(tags(db_path)) == ["https://ex.com/1"]
    assert not (await run_collection(deps)).skipped  # the lock is free again
    assert len(alerts) == 1  # a second timeout stays quiet


# --- web search ---


def brave(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": f"https://news.example/{len(seen)}",
                        "title": "A headline",
                        "description": "About " + request.url.params["q"],
                        "page_age": RECENT.isoformat(),
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


async def no_sleep(_s: float) -> None:
    return None


async def test_web_search_without_a_block_a_key_or_known_games_does_nothing(cfg, db_path, alerts):
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts)
        blockless = make_deps(
            cfg.model_copy(update={"web_search": None}), db_path, client, [], alerts
        )
        results = [
            await collect_web_search(blockless, ["palworld"], "k", sleep=no_sleep),
            await collect_web_search(deps, ["palworld"], "", sleep=no_sleep),
            await collect_web_search(deps, ["ghost", "nope"], "k", sleep=no_sleep),
            await collect_web_search(deps, set(), "k", sleep=no_sleep),
        ]
    assert seen == []
    assert all(r.sources_total == 0 and r.new_items == 0 for r in results)
    assert health(db_path, "Brave Search") is None


async def test_web_search_searches_a_repeated_game_key_once(cfg, db_path, alerts):
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts)
        await collect_web_search(deps, ["palworld", "palworld"], "k", sleep=no_sleep)
    assert len(seen) == 2  # queries_per_game, once


async def test_web_search_tags_each_result_to_the_game_it_was_searched_for(cfg, db_path, alerts):
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts)
        outcome = await collect_web_search(deps, ("rust", "palworld"), "k", sleep=no_sleep)
    assert outcome.new_items == 4
    by_game: dict[str, int] = {}
    for found in tags(db_path).values():
        assert len(found) == 1
        (game,) = found
        by_game[game] = by_game.get(game, 0) + 1
    assert by_game == {"palworld": 2, "rust": 2}


async def test_web_search_timeout_grows_with_the_game_list(cfg, db_path, alerts, monkeypatch):
    many = [GameCfg(key=f"game{i}", name=f"Game {i}") for i in range(15)]
    big = cfg.model_copy(update={"catalog": many, "shared_sources": []})
    timeouts: list[float] = []
    real = collect_mod.run_collectors

    async def spy(collectors, http, timeout_s=20.0, **kw):
        timeouts.append(timeout_s)
        return await real(collectors, http, timeout_s=timeout_s, **kw)

    monkeypatch.setattr(collect_mod, "run_collectors", spy)
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave(seen)) as client:
        deps = make_deps(big, db_path, client, [], alerts)
        await collect_web_search(deps, ["game0"], "k", sleep=no_sleep)
        await collect_web_search(deps, [f"game{i}" for i in range(15)], "k", sleep=no_sleep)
    assert timeouts == [20 + 1 * 2 * 1.5, 20 + 15 * 2 * 1.5]
    assert len(seen) == 2 + 30  # every game got its queries


async def test_web_search_uses_the_deps_sleep_between_queries_when_none_is_given(
    cfg, db_path, alerts
):
    clock = FakeClock()
    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts, sleep=clock.sleep, clock=clock)
        await collect_web_search(deps, ["palworld", "rust"], "k")
    assert len(clock.slept) == 3  # four queries, three pauses, none of them real


async def test_web_search_does_not_need_or_take_the_run_lock_and_skips_the_hook(
    cfg, db_path, alerts
):
    hook_calls = []

    async def hook(items, results):
        hook_calls.append(1)
        return 0

    seen: list[str] = []
    async with httpx.AsyncClient(transport=brave(seen)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts, shift_hook=hook)
        async with _run_lock:
            outcome = await collect_web_search(deps, ["palworld"], "k", sleep=no_sleep)
            assert _run_lock.locked()
    assert not outcome.skipped and outcome.new_items == 2
    assert hook_calls == []  # a code in a Brave snippet is never seen by the SHiFT hook
    assert sweep_summary(db_path) is None  # and it isn't a "sweep"


async def test_web_search_failures_alert_at_twelve_like_any_other_source(cfg, db_path, alerts):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        deps = make_deps(cfg, db_path, client, [], alerts)
        for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD - 1):
            await collect_web_search(deps, ["palworld"], "k", sleep=no_sleep)
        assert alerts == []
        outcome = await collect_web_search(deps, ["palworld"], "k", sleep=no_sleep)
        await collect_web_search(deps, ["palworld"], "k", sleep=no_sleep)
    assert outcome.alerted_sources == ("Brave Search",)
    assert len(alerts) == 1 and "'Brave Search'" in alerts[0]
