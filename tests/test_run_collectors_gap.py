"""Tests for `run_collectors`' cross-call rate-limit gap and `build_collectors`'
`include_web_search` flag (plan step 7).

The SHiFT alert sweep (design.md §12) shares both of these with the daily
job: the Reddit gap has to survive across separate `run_collectors` calls
an hour apart, and the sweep never wants `web_search` collectors at all.
Everything here uses a fake clock and a fake sleep, never a real one --
these tests would otherwise take as long as the gaps they're testing.
"""

from __future__ import annotations

import httpx
import pytest

from newsbot.collectors.base import RateLimitState, RawItem, build_collectors, run_collectors
from newsbot.config import (
    AppConfig,
    DigestCfg,
    RssSource,
    Secrets,
    SteamSource,
    Topic,
    WebSearchSource,
)


class _FakeCollector:
    def __init__(self, name: str, rate_limit_key: str | None = None) -> None:
        self.name = name
        self.source_type = "rss"
        self.rate_limit_key = rate_limit_key
        self.calls: list[float] = []

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        return []


@pytest.fixture
def http_client():
    return httpx.AsyncClient()


class _FakeClock:
    """A monotonic clock that only moves when told to -- `sleep()` advances it."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# --- cross-call gap ---


async def test_no_state_never_waits_across_calls(http_client):
    clock = _FakeClock()
    collector = _FakeCollector("reddit-a", rate_limit_key="reddit")
    await run_collectors(
        [collector], http_client, rate_limit_gap_s=35.0, sleep=clock.sleep, clock=clock
    )
    await run_collectors(
        [collector], http_client, rate_limit_gap_s=35.0, sleep=clock.sleep, clock=clock
    )
    # Without a shared RateLimitState, each call starts its group fresh --
    # today's (unchanged) behavior.
    assert clock.slept == []


async def test_state_honors_gap_within_a_group_first_member_included(http_client):
    clock = _FakeClock()
    state = RateLimitState()
    a = _FakeCollector("reddit-a", rate_limit_key="reddit")
    b = _FakeCollector("reddit-b", rate_limit_key="reddit")
    await run_collectors(
        [a, b],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    # a: no prior fetch, no wait. b: waits the full gap behind a.
    assert clock.slept == [35.0]


async def test_state_honors_gap_across_separate_calls(http_client):
    clock = _FakeClock()
    state = RateLimitState()
    first = _FakeCollector("reddit-a", rate_limit_key="reddit")
    await run_collectors(
        [first],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    assert clock.slept == []  # first call, nothing to wait behind

    clock.now += 10.0  # only 10s elapsed since the last fetch, gap is 35s
    second = _FakeCollector("reddit-b", rate_limit_key="reddit")
    await run_collectors(
        [second],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    assert clock.slept == [25.0]  # 35 - 10 already elapsed


async def test_state_no_wait_if_gap_already_elapsed(http_client):
    clock = _FakeClock()
    state = RateLimitState()
    first = _FakeCollector("reddit-a", rate_limit_key="reddit")
    await run_collectors(
        [first],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    clock.now += 60.0  # well past the gap
    second = _FakeCollector("reddit-b", rate_limit_key="reddit")
    await run_collectors(
        [second],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    assert clock.slept == []


async def test_non_keyed_collectors_never_wait_even_with_state(http_client):
    clock = _FakeClock()
    state = RateLimitState()
    a = _FakeCollector("rss-a")
    b = _FakeCollector("rss-b")
    await run_collectors(
        [a, b],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    assert clock.slept == []


async def test_keyed_and_unkeyed_run_concurrently_state_only_affects_keyed(http_client):
    clock = _FakeClock()
    state = RateLimitState()
    keyed = _FakeCollector("reddit-a", rate_limit_key="reddit")
    unkeyed = _FakeCollector("rss-a")
    results = await run_collectors(
        [keyed, unkeyed],
        http_client,
        rate_limit_gap_s=35.0,
        sleep=clock.sleep,
        rate_limit_state=state,
        clock=clock,
    )
    assert {r.source_name for r in results} == {"reddit-a", "rss-a"}
    assert clock.slept == []


# --- build_collectors(include_web_search=...) ---


def _cfg(sources) -> AppConfig:
    return AppConfig(
        guild_id=1,
        digest=DigestCfg(channel_id=1, time="09:00", timezone="UTC"),
        topics=[Topic(key="borderlands4", name="Borderlands 4")],
        sources=sources,
    )


def _secrets(**kwargs) -> Secrets:
    defaults = dict(
        discord_token=None,
        anthropic_api_key="x",
        brave_api_key="brave-key",
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    defaults.update(kwargs)
    return Secrets(**defaults)


def test_build_collectors_includes_web_search_by_default():
    cfg = _cfg(
        [
            RssSource(type="rss", name="Feed", url="https://e.com/rss", trust="press"),
            WebSearchSource(type="web_search", trust="press"),
        ]
    )
    collectors = build_collectors(cfg, _secrets())
    assert {c.source_type for c in collectors} == {"rss", "web_search"}


def test_build_collectors_excludes_web_search_when_asked():
    cfg = _cfg(
        [
            RssSource(type="rss", name="Feed", url="https://e.com/rss", trust="press"),
            WebSearchSource(type="web_search", trust="press"),
        ]
    )
    collectors = build_collectors(cfg, _secrets(), include_web_search=False)
    assert {c.source_type for c in collectors} == {"rss"}


def test_build_collectors_exclude_web_search_still_builds_everything_else():
    cfg = _cfg(
        [
            RssSource(type="rss", name="Feed", url="https://e.com/rss", trust="press"),
            SteamSource(type="steam_news", name="Steam", app_id=1, trust="official"),
            WebSearchSource(type="web_search", trust="press"),
        ]
    )
    collectors = build_collectors(cfg, _secrets(), include_web_search=False)
    assert {c.source_type for c in collectors} == {"rss", "steam_news"}
