"""Reddit rotation and backoff in the hourly pass (owner decision, 2026-10-01).

The live dev test fetched 15 subreddits in 553 seconds and got five 429s.
These tests pin the fix: priority subreddits (SHiFT games, comped servers'
games) go every pass, the rest rotate stalest first with the state kept in
`app_state`, and a 429 ends Reddit for the pass without counting against
anybody's health. Collectors are fakes or real ones over `httpx.MockTransport`;
the clock moves only when somebody sleeps.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from v3_fakes import make_guild

from newsbot.collectors.base import MAX_BACKOFF_S, RateLimited, RateLimitState, run_collectors
from newsbot.collectors.rss import RssCollector
from newsbot.config import GameCfg, RssSource, Secrets, load_config
from newsbot.pipeline.collect import (
    SOURCE_FAILURE_ALERT_THRESHOLD,
    CollectionDeps,
    build_collection_collectors,
    plan_reddit,
    priority_games,
    run_collection,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "config_v3.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class Sub:
    """A fake subreddit; `behave` is called with the call count and may raise."""

    source_type = "rss"
    rate_limit_key = "reddit"

    def __init__(self, name, behave=None):
        self.name = name
        self.calls = 0
        self._behave = behave

    async def collect(self, http):
        self.calls += 1
        if self._behave is not None:
            self._behave(self.calls)
        return []


class Blog:
    source_type = "rss"
    rate_limit_key = None

    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def collect(self, http):
        self.calls += 1
        return []


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def alerts():
    return []


def world_cfg(n, *, hours=3, interval=60):
    """n games g0..g(n-1), each with one subreddit named r/g<i>; SHiFT watches g0."""
    cfg = load_config(FIXTURE)
    games = [
        GameCfg(
            key=f"g{i}",
            name=f"Game Number {i}",
            sources=[
                {
                    "type": "rss",
                    "name": f"r/g{i}",
                    "url": f"https://www.reddit.com/r/g{i}/.rss",
                    "trust": "community",
                }
            ],
        )
        for i in range(n)
    ]
    collection = cfg.collection.model_copy(
        update={"reddit_rotation_hours": hours, "interval_minutes": interval}
    )
    shift = cfg.shift.model_copy(update={"games": ["g0"]})
    return cfg.model_copy(
        update={"catalog": games, "shared_sources": [], "collection": collection, "shift": shift}
    )


def make_deps(cfg, db_path, collectors, alerts, clock, state=None, http=None):
    async def alert(message: str) -> None:
        alerts.append(message)

    return CollectionDeps(
        cfg=cfg,
        db_path=db_path,
        http=http,  # the fakes never touch it
        collectors=collectors,
        now=lambda: NOW,
        alert=alert,
        sleep=clock.sleep,
        clock=clock,
        rate_limit_state=state if state is not None else RateLimitState(),
    )


def limited(retry_after=None):
    def behave(_n):
        raise RateLimited(retry_after)

    return behave


def calls(subs):
    return [s.calls for s in subs]


def health_rows(db_path):
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM source_health").fetchall()


# --- priority versus rotating ---


async def test_shift_and_comped_games_go_every_pass_and_the_rest_rotate(db_path, alerts):
    cfg = world_cfg(9)
    make_guild(db_path, 1001, tier="comped", games=[("g1", 11), ("g2", 12)])
    make_guild(db_path, 1002, tier="free", games=[("g3", 13)])  # free: rotates like the rest
    subs = [Sub(f"r/g{i}") for i in range(9)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    assert priority_games(cfg, db_path) == {"g0", "g1", "g2"}
    for _ in range(3):
        await run_collection(deps)
    assert calls(subs[:3]) == [3, 3, 3]
    # 6 rotating at 2 a pass: three passes reach each exactly once.
    assert calls(subs[3:]) == [1] * 6


async def test_the_priority_set_follows_the_comped_servers_game_lists(db_path, alerts):
    cfg = world_cfg(6)
    subs = [Sub(f"r/g{i}") for i in range(6)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    assert priority_games(cfg, db_path) == {"g0"}
    make_guild(db_path, 1001, tier="comped", games=[("g4", 14)])
    assert priority_games(cfg, db_path) == {"g0", "g4"}
    await run_collection(deps)
    await run_collection(deps)
    assert calls([subs[0], subs[4]]) == [2, 2]
    # A comped server that isn't set up yet doesn't count.
    make_guild(db_path, 1003, set_up=False, tier="comped", games=[("g5", 15)])
    assert "g5" not in priority_games(cfg, db_path)
    # And a comped server that stops following a game takes it out of the set.
    with closing(connect(db_path)) as conn:
        conn.execute("DELETE FROM guild_games WHERE guild_id = 1001")
        conn.commit()
    assert priority_games(cfg, db_path) == {"g0"}


async def test_non_reddit_sources_run_every_pass(db_path, alerts):
    cfg = world_cfg(6)
    blog = Blog("A blog")
    deps = make_deps(cfg, db_path, [blog, *[Sub(f"r/g{i}") for i in range(6)]], alerts, Clock())
    for _ in range(4):
        await run_collection(deps)
    assert blog.calls == 4


# --- rotation fairness and persistence ---


async def test_every_rotating_subreddit_is_fetched_within_the_period(db_path, alerts):
    cfg = world_cfg(13)  # g0 priority, 12 rotating: 4 a pass
    subs = [Sub(f"r/g{i}") for i in range(13)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())

    async def one_pass() -> set[str]:
        before = calls(subs)
        await run_collection(deps)
        return {s.name for s, b in zip(subs[1:], before[1:], strict=True) if s.calls > b}

    seen = [await one_pass() for _ in range(3)]
    assert [len(s) for s in seen] == [4, 4, 4]
    assert set().union(*seen) == {s.name for s in subs[1:]}
    assert await one_pass() == seen[0]  # and the cycle repeats in the same order


async def test_rotation_survives_a_restart(db_path, alerts):
    cfg = world_cfg(7)  # 6 rotating: 2 a pass
    subs = [Sub(f"r/g{i}") for i in range(7)]
    # Each pass is a "new process": fresh deps and rate-limit state, same database.
    await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    first = {s.name for s in subs[1:] if s.calls}
    await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    second = {s.name for s in subs[1:] if s.calls} - first
    assert len(first) == 2 and len(second) == 2
    await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    assert calls(subs[1:]) == [1] * 6


async def test_never_fetched_go_first_but_the_cap_still_holds(db_path, alerts):
    cfg = world_cfg(13)
    subs = [Sub(f"r/g{i}") for i in range(13)]
    # Everything but r/g5 has a recent fetch on record; r/g5 has none at all.
    with closing(connect(db_path)) as conn:
        for s in subs[1:]:
            if s.name != "r/g5":
                repo.app_state_set(conn, f"reddit_fetched:{s.name}", NOW.isoformat())
    await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    assert subs[5].calls == 1
    assert sum(calls(subs[1:])) == 4


async def test_a_feed_that_ran_and_failed_still_moves_to_the_back(db_path, alerts):
    cfg = world_cfg(7)

    def boom(_n):
        raise RuntimeError("503")

    subs = [Sub("r/g0"), Sub("r/g1", boom)] + [Sub(f"r/g{i}") for i in range(2, 7)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    for _ in range(3):
        await run_collection(deps)
    assert subs[1].calls == 1


async def test_a_shorter_interval_makes_each_pass_a_smaller_slice(db_path, alerts):
    cfg = world_cfg(13, hours=3, interval=15)  # 12 passes per rotation: 1 each
    subs = [Sub(f"r/g{i}") for i in range(13)]
    await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    assert sum(calls(subs[1:])) == 1


# --- backoff ---


async def test_a_429_stops_reddit_for_the_pass_and_the_leftovers_go_first_next_time(
    db_path, alerts
):
    cfg = world_cfg(7, hours=1)  # rotation off: all seven want a turn
    flaky = {"on": True}

    def behave(_n):
        if flaky["on"]:
            raise RateLimited(None)

    subs = [Sub(f"r/g{i}", behave if i == 2 else None) for i in range(7)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    outcome = await run_collection(deps)
    # g0 and g1 ran, g2 got the 429, g3 to g6 were never asked.
    assert calls(subs) == [1, 1, 1, 0, 0, 0, 0]
    assert outcome.sources_total == 2 and outcome.failing_sources == ()
    flaky["on"] = False
    await run_collection(deps)  # no Retry-After, so Reddit is open again
    assert calls(subs) == [2, 2, 2, 1, 1, 1, 1]


async def test_the_leftovers_are_next_in_line_when_rotation_is_on(db_path, alerts):
    cfg = world_cfg(13)  # 12 rotating: 4 a pass
    flaky = {"on": True}

    def behave(_n):
        if flaky["on"]:
            raise RateLimited(None)

    subs = [Sub("r/g0")] + [Sub(f"r/g{i}", behave if i == 2 else None) for i in range(1, 13)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    await run_collection(deps)
    # A cold start ranks in config order: g1, g2 (429), then g3 and g4 never asked.
    assert calls(subs[1:5]) == [1, 1, 0, 0]
    flaky["on"] = False
    await run_collection(deps)
    # g2, g3, g4 weren't stamped, so they lead; g5 fills the fourth slot.
    assert calls(subs[1:6]) == [1, 2, 1, 1, 1]


async def test_retry_after_keeps_reddit_closed_for_the_next_pass(db_path, alerts):
    cfg = world_cfg(5, hours=1)
    first = {"on": True}

    def behave(_n):
        if first["on"]:
            raise RateLimited(7200.0)

    subs = [Sub("r/g0", behave)] + [Sub(f"r/g{i}") for i in range(1, 5)]
    clock = Clock()
    deps = make_deps(cfg, db_path, subs, alerts, clock)
    await run_collection(deps)
    first["on"] = False
    clock.now += 3600  # an hour on, still inside the two
    await run_collection(deps)
    assert calls(subs) == [1, 0, 0, 0, 0]
    clock.now += 3601
    await run_collection(deps)
    assert calls(subs) == [2, 1, 1, 1, 1]


async def test_retry_after_is_capped():
    state = RateLimitState()
    clock = Clock()
    subs = [Sub("r/a", limited(10 * 24 * 3600.0)), Sub("r/b")]
    await run_collectors(subs, None, rate_limit_state=state, sleep=clock.sleep, clock=clock)
    assert state.backoff_until["reddit"] == MAX_BACKOFF_S


async def test_without_a_retry_after_only_the_call_is_affected():
    state = RateLimitState()
    clock = Clock()
    subs = [Sub("r/a", limited()), Sub("r/b")]
    results = await run_collectors(
        subs, None, rate_limit_state=state, sleep=clock.sleep, clock=clock
    )
    assert [r.skipped for r in results] == ["rate limited", "backed off"]
    assert "reddit" not in state.backoff_until
    assert subs[1].calls == 0


async def test_backoff_and_rotation_never_count_as_failures_or_alert(db_path, alerts):
    cfg = world_cfg(13)
    subs = [Sub("r/g0")] + [Sub(f"r/g{i}", limited(60.0) if i == 1 else None) for i in range(1, 13)]
    clock = Clock()
    deps = make_deps(cfg, db_path, subs, alerts, clock)
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD + 3):
        clock.now += 3600
        await run_collection(deps)
    assert alerts == []
    assert all(row["consecutive_failures"] == 0 for row in health_rows(db_path))
    # The 429'd one never gets a health row, so /newsbot status can't count it as failing.
    assert "r/g1" not in {row["source_name"] for row in health_rows(db_path)}
    with closing(connect(db_path)) as conn:
        assert repo.failing_sources(conn) == []


async def test_real_failures_still_count_on_the_fetches_that_happen(db_path, alerts):
    cfg = world_cfg(2, hours=1)

    def boom(_n):
        raise RuntimeError("dead")

    subs = [Sub("r/g0", boom), Sub("r/g1")]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD):
        await run_collection(deps)
    assert len(alerts) == 1 and "r/g0" in alerts[0]


# --- the summary line ---


async def test_one_info_line_per_pass_with_the_three_counts(db_path, alerts, caplog):
    cfg = world_cfg(7)  # g0 priority; 6 rotating, 2 picked: g1 runs, g2 429s
    subs = [Sub("r/g0")] + [Sub(f"r/g{i}", limited() if i == 2 else None) for i in range(1, 7)]
    deps = make_deps(cfg, db_path, subs, alerts, Clock())
    with caplog.at_level(logging.INFO, logger="newsbot.pipeline.collect"):
        await run_collection(deps)
    lines = [r for r in caplog.records if r.getMessage().startswith("Reddit fetched")]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    # g0 and g1 ran; g2 got the 429; the other four weren't picked.
    assert lines[0].getMessage() == "Reddit fetched 2, rotated out 4, backed off 1"


async def test_priority_subreddits_take_turns_when_reddit_lets_one_request_through(db_path, alerts):
    """The 2026-10-02 production pattern: one request a pass, the next one 429s."""
    cfg = world_cfg(3, hours=1)
    cfg = cfg.model_copy(
        update={"shift": cfg.shift.model_copy(update={"games": ["g0", "g1", "g2"]})}
    )
    asked_this_pass: list[str] = []
    fetched: list[str] = []

    def make(i):
        def behave(_n):
            asked_this_pass.append(f"r/g{i}")
            if len(asked_this_pass) > 1:
                raise RateLimited(None)
            fetched.append(f"r/g{i}")

        return behave

    subs = [Sub(f"r/g{i}", make(i)) for i in range(3)]
    clock = Clock()
    pass_no = {"n": 0}

    async def alert(message: str) -> None:
        alerts.append(message)

    deps = CollectionDeps(
        cfg=cfg,
        db_path=db_path,
        http=None,
        collectors=subs,
        now=lambda: NOW + timedelta(hours=pass_no["n"]),
        alert=alert,
        sleep=clock.sleep,
        clock=clock,
        rate_limit_state=RateLimitState(),
    )
    for n in range(6):
        pass_no["n"] = n
        asked_this_pass.clear()
        clock.now += 3600
        await run_collection(deps)
    # Pass 1: g0 gets through, g1 is 429'd, g2 is never asked. The unstamped two
    # lead from then on, so the line moves and each of them gets through twice.
    assert fetched == ["r/g0", "r/g1", "r/g2", "r/g0", "r/g1", "r/g2"]
    assert all(fetched.count(s.name) >= 2 for s in subs)


async def test_the_429d_and_the_never_asked_stay_unstamped(db_path, alerts):
    cfg = world_cfg(3, hours=1)
    subs = [Sub("r/g0"), Sub("r/g1", limited()), Sub("r/g2")]
    await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    with closing(connect(db_path)) as conn:
        stamps = {s.name: repo.app_state_get(conn, f"reddit_fetched:{s.name}") for s in subs}
    assert stamps["r/g0"] and stamps["r/g1"] is None and stamps["r/g2"] is None


async def test_a_429_says_what_reddit_said_in_the_summary(db_path, alerts, caplog):
    cfg = world_cfg(3, hours=1)
    subs = [Sub("r/g0"), Sub("r/g1", limited(1800.0)), Sub("r/g2")]
    with caplog.at_level(logging.INFO, logger="newsbot.pipeline.collect"):
        await run_collection(make_deps(cfg, db_path, subs, alerts, Clock()))
    (line,) = [r for r in caplog.records if r.getMessage().startswith("Reddit fetched")]
    assert (
        line.getMessage()
        == "Reddit fetched 1, rotated out 0, backed off 2, reddit_retry_after_s 1800"
    )
    assert line.reddit_retry_after_s == 1800.0


@pytest.mark.parametrize(
    ("headers", "said"),
    [({"Retry-After": "120"}, "120s"), ({"Retry-After": "soon"}, "soon"), ({}, "none")],
)
async def test_a_reddit_429_logs_the_subreddit_and_the_retry_after(headers, said, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    source = RssSource(
        type="rss",
        name="r/Palworld",
        url="https://www.reddit.com/r/Palworld/.rss",
        trust="community",
    )
    collector = RssCollector(source)
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.rss"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(RateLimited):
                await collector.collect(http)
    (line,) = [r for r in caplog.records if "429" in r.getMessage()]
    assert line.levelno == logging.INFO
    assert "r/Palworld" in line.getMessage() and line.getMessage().endswith(f"Retry-After: {said}")


# --- the real example catalog ---

RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
    b"<item><title>x</title><link>https://www.reddit.com/r/x/1</link></item></channel></rss>"
)


def example_world(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    cfg = load_config(EXAMPLE)
    reddit = [
        c
        for c in build_collection_collectors(
            cfg,
            Secrets(
                discord_token=None,
                anthropic_api_key="k",
                brave_api_key=None,
                bluesky_handle=None,
                bluesky_app_password=None,
            ),
        )
        if getattr(c, "rate_limit_key", None) == "reddit"
    ]
    return cfg, reddit


async def test_the_example_catalog_is_one_reddit_request_a_pass(monkeypatch, db_path, alerts):
    cfg, reddit = example_world(monkeypatch)
    # v3.0.3: Reddit is r/Borderlands4 and nothing else, read only for SHiFT codes.
    assert [c.name for c in reddit] == ["r/Borderlands4"]
    assert cfg.codes_only_source_names() == {"r/Borderlands4"}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path.split("/")[2].lower())
        return httpx.Response(200, content=RSS)

    clock = Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        deps = make_deps(cfg, db_path, reddit, alerts, clock, http=http)
        counts = []
        for _ in range(3):
            requested.clear()
            await run_collection(deps)
            counts.append(len(requested))
            assert requested == ["borderlands4"]
    # One subreddit, so nothing rotates: it goes every pass.
    assert counts == [1, 1, 1]


async def test_the_one_codes_only_subreddit_is_priority_with_no_comped_server(monkeypatch, db_path):
    cfg, reddit = example_world(monkeypatch)
    # Borderlands 4 is a SHiFT game, so its subreddit is hourly whoever follows what.
    assert "borderlands4" in cfg.shift.games
    plan = plan_reddit(reddit, cfg, priority_games(cfg, db_path), {})
    assert [c.name for c in plan.selected] == ["r/Borderlands4"]
    assert plan.rotated_out == 0
    # Even asked to rotate only once a day, and fetched a minute ago, it still goes.
    slow = cfg.model_copy(
        update={"collection": cfg.collection.model_copy(update={"reddit_rotation_hours": 24})}
    )
    just_now = {"r/Borderlands4": NOW}
    plan = plan_reddit(reddit, slow, priority_games(slow, db_path), just_now)
    assert [c.name for c in plan.selected] == ["r/Borderlands4"]


def test_the_rotation_knob_is_validated():
    cfg = load_config(EXAMPLE)
    assert cfg.collection.reddit_rotation_hours == 3
    for bad in (0, 25):
        with pytest.raises(ValueError, match="reddit_rotation_hours"):
            type(cfg.collection).model_validate({"reddit_rotation_hours": bad})
