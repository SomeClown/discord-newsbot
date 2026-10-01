"""Comped summaries: one Claude call per game per day, shared (plan task 7).

The LLM is a hand-written fake that counts its calls, Brave is an
`httpx.MockTransport`, the clock only moves when a test moves it, and
the "sleep" between looks at somebody else's claim just yields to the loop.
No network, no real Anthropic, nothing waits for real.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.pipeline import summaries
from newsbot.pipeline.collect import CollectionDeps
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.guild_digest import GuildDigestDeps, run_guild_digest
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import (
    SummaryDeps,
    ensure_summary,
    prepare_summaries,
    retry_lookup,
    summary_lookup,
    upcoming_dues,
)
from newsbot.pipeline.summarize import LLMError, LLMFatalError, LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CompedFollow, StoredItem

FIXTURES = Path(__file__).parent / "fixtures"
LA = "America/Los_Angeles"
BERLIN = "Europe/Berlin"
# 2026-10-01: Berlin is on summer time (09:00 is 07:00 UTC) and Pacific too (09:00 is 16:00 UTC).
BERLIN_DUE = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
PACIFIC_DUE = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
PAC, BER, FREE = 1, 2, 3
_A, _B, _OTHER = "claim-a", "claim-b", "someone-else"  # claim tokens, not passwords


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class FakeLLM:
    """Counts calls, records the prompts, and writes one story about the first item it's shown."""

    def __init__(self, *, fail=None, yields=0, usage=(100, 20)) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = fail or {}
        self.yields = yields
        self.usage = usage

    def games(self) -> list[str]:
        return [re.search(r"\(key: (\w+)\)", user).group(1) for _, user in self.calls]

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        for _ in range(self.yields):
            await asyncio.sleep(0)
        game = re.search(r"\(key: (\w+)\)", user).group(1)
        err = self.fail.get(game) or self.fail.get("*")
        if err is not None:
            raise err
        items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        stories = (
            [
                StoryOut(
                    headline=f"{game} story",
                    summary="What happened.",
                    label="reported",
                    item_urls=[items[0]["url"]],
                    relevant=True,
                )
            ]
            if items
            else []
        )
        return LLMResult(StoriesOut(stories=stories), *self.usage)


class FakePublisher:
    def __init__(self) -> None:
        self.posted = []

    async def publish(self, r):
        self.posted.extend(r.messages)
        return {m.topic_key: 9000 + i for i, m in enumerate(r.messages)}


class World:
    def __init__(self, tmp_path, monkeypatch, cfg_name="config_v3.yaml", now=None) -> None:
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        self.db_path = str(tmp_path / "t.db")
        with closing(connect(self.db_path)) as conn:
            migrate(conn)
        self.cfg = load_config(FIXTURES / cfg_name)
        self.clock = Clock(now or BERLIN_DUE - timedelta(minutes=20))
        self.llm = FakeLLM()
        self.alerts: list[str] = []
        self.sleeps: list[float] = []
        self.brave: list[str] = []
        self.brave_status = 200
        self.http: httpx.AsyncClient | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.brave.append(request.url.params["q"])
        if self.brave_status != 200:
            return httpx.Response(self.brave_status)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": f"https://news.example/{len(self.brave)}",
                        "title": "A web headline",
                        "description": "About " + request.url.params["q"],
                        "page_age": (self.clock.t - timedelta(hours=1)).isoformat(),
                    }
                ]
            },
        )

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    async def alert(self, text: str) -> None:
        self.alerts.append(text)

    def deps(self, *, web: bool = False, **over) -> SummaryDeps:
        collection = None
        if web:
            self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
            collection = CollectionDeps(
                cfg=self.cfg,
                db_path=self.db_path,
                http=self.http,
                collectors=[],
                now=self.clock,
                alert=self.alert,
                sleep=self.sleep,
            )
        args = dict(
            cfg=self.cfg,
            db_path=self.db_path,
            llm=self.llm,
            now=self.clock,
            alert=self.alert,
            collection=collection,
            brave_api_key="k" if web else None,
            sleep=self.sleep,
        )
        args.update(over)
        return SummaryDeps(**args)

    def conn(self):
        return closing(connect(self.db_path))

    def guild(self, guild_id, tier, tz, games, time="09:00"):
        with self.conn() as conn:
            repo.create_guild(
                conn,
                guild_id,
                digest_time=time,
                timezone=tz,
                admin_channel_id=None,
                tier=tier,
                set_up=True,
                now=lambda: BERLIN_DUE - timedelta(days=3),
            )
            repo.set_guild_games(conn, guild_id, [(g, 100 + i) for i, g in enumerate(games)])

    def item(self, game, title, collected, *, trust="press", uncertain=False, excerpt="", url=None):
        with self.conn() as conn:
            repo.store_items(
                conn,
                [
                    StoredItem(
                        url=url or f"https://example.com/{game}/{title.replace(' ', '-')}",
                        title=title,
                        excerpt=excerpt,
                        source_name="Feed",
                        trust=trust,
                        published_at=collected,
                        topics={game: uncertain},
                    )
                ],
                now=lambda: collected,
            )

    def rows(self, sql, params=()):
        with self.conn() as conn:
            return conn.execute(sql, params).fetchall()

    def summaries(self, game=None):
        return self.rows(
            "SELECT * FROM game_summaries WHERE (? IS NULL OR game_key = ?) ORDER BY id",
            (game, game),
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def ago(hours: float) -> datetime:
    return BERLIN_DUE - timedelta(hours=hours)


# --- one call, shared ---


async def test_two_comped_guilds_racing_for_one_game_make_one_call(world):
    # Was Berlin and Pacific (nine hours apart). Under the per-server reuse rule those are two
    # cycles and two summaries on purpose; two servers with digests minutes apart share one.
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(FREE + 1, "comped", BERLIN, ["palworld"], time="09:05")
    world.item("palworld", "raid boss patch", ago(3))
    world.clock.t = BERLIN_DUE
    # The winner's model call is held until the loser has found the claim busy and
    # gone to sleep on it. This used to be `FakeLLM(yields=5)`, a guess at "long
    # enough", and on a loaded machine the claims run in threads: the winner could
    # claim, summarize and save before the loser's claim thread ever ran, so the
    # loser just reused the row without waiting. Still one call (the code was
    # fine), but `world.sleeps` came back empty and the test flaked on the clock.
    loser_waiting = asyncio.Event()

    class HeldLLM(FakeLLM):
        async def emit_stories(self, system: str, user: str) -> LLMResult:
            await loser_waiting.wait()
            return await super().emit_stories(system, user)

    async def sleep(seconds: float) -> None:
        loser_waiting.set()
        await world.sleep(seconds)

    world.llm = HeldLLM()
    deps = world.deps(sleep=sleep)
    lookup = summary_lookup(deps)

    berlin, pacific = await asyncio.wait_for(
        asyncio.gather(
            lookup("palworld", BERLIN_DUE), lookup("palworld", BERLIN_DUE + timedelta(minutes=5))
        ),
        timeout=10,
    )

    assert world.llm.games() == ["palworld"]
    assert berlin is not None and pacific is not None
    assert berlin.status == pacific.status == "ok"
    assert [s.headline for s in berlin.stories] == [s.headline for s in pacific.stories]
    assert len(world.summaries("palworld")) == 1
    assert world.sleeps  # the loser waited on a timer instead of asking Claude too
    assert world.rows("SELECT value FROM app_state WHERE key LIKE 'summary_claim:%'") == []


def test_the_claim_is_won_by_exactly_one_of_many_threads(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        with closing(connect(db)) as conn:
            barrier.wait()
            state, _ = repo.claim_game_summary(conn, "palworld", PACIFIC_DUE, token=f"t{n}")
            results.append(state)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("claimed") == 1 and results.count("busy") == 7


def test_a_stale_claim_can_be_taken_over_and_a_fresh_one_cannot(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        t0 = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
        assert repo.claim_game_summary(conn, "g", t0, token=_A, now=lambda: t0)[0] == "claimed"
        soon = t0 + timedelta(minutes=9)
        assert repo.claim_game_summary(conn, "g", t0, token=_B, now=lambda: soon)[0] == "busy"
        late = t0 + repo.SUMMARY_CLAIM_LEASE + timedelta(seconds=1)
        assert repo.claim_game_summary(conn, "g", t0, token=_B, now=lambda: late)[0] == "claimed"
        # The old holder releasing doesn't drop the new holder's claim.
        repo.release_game_summary_claim(conn, "g", _A)
        assert repo.app_state_get(conn, "summary_claim:g") is not None
        repo.release_game_summary_claim(conn, "g", _B)
        assert repo.app_state_get(conn, "summary_claim:g") is None


async def test_a_caller_that_never_gets_the_claim_gives_up_without_calling_claude(
    world, monkeypatch
):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    with world.conn() as conn:
        repo.claim_game_summary(conn, "palworld", BERLIN_DUE, token=_OTHER, now=world.clock)
    monkeypatch.setattr(summaries, "_MAX_POLLS", 3)

    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))

    assert got is None
    assert world.llm.calls == [] and world.sleeps == [summaries._POLL_S] * 3


async def test_a_cancelled_summary_releases_its_claim(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    started = asyncio.Event()

    class Hangs:
        async def emit_stories(self, system, user):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        ensure_summary(world.deps(llm=Hangs()), "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.rows("SELECT * FROM app_state WHERE key LIKE 'summary_claim:%'") == []
    assert world.summaries() == []


# --- when the prepare job runs ---


async def test_nothing_happens_before_the_lead_and_the_first_guilds_lead_triggers_it(world):
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))

    world.clock.t = BERLIN_DUE - timedelta(minutes=30, seconds=1)
    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == []

    world.clock.t = BERLIN_DUE - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert world.llm.games() == ["palworld"]
    [row] = world.summaries("palworld")
    assert row["status"] == "ok" and row["run_date"] == "2026-10-01"
    assert row["window_end"] == world.clock.t.isoformat()


async def test_a_guild_minutes_later_reuses_the_summary_and_one_hours_later_gets_its_own(world):
    # Was "the later guild's lead reuses the earlier one's summary" for Berlin and Pacific. A
    # summary older than six hours at digest time is stale now, so those are two cycles; only a
    # server due within hours of the first shares its call.
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(FREE + 1, "comped", BERLIN, ["palworld"], time="09:05")
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "raid boss patch", ago(3))
    world.clock.t = BERLIN_DUE - timedelta(minutes=30)
    await prepare_summaries(world.deps())

    # The 09:05 server's lead arrives; it reads the same summary. Still one call.
    world.clock.t = BERLIN_DUE - timedelta(minutes=25)
    assert await prepare_summaries(world.deps()) == []
    assert len(world.llm.calls) == 1

    # Pacific's digest is nine hours on: that summary would be 9h30 old, so it gets a new one.
    world.item("palworld", "a third thing", BERLIN_DUE + timedelta(hours=2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert len(world.llm.calls) == 2

    # Next morning is a new day and a new summary, chained onto the last window.
    world.item("palworld", "a second thing", BERLIN_DUE + timedelta(hours=10))
    world.clock.t = BERLIN_DUE + timedelta(days=1) - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert len(world.llm.calls) == 3
    third = world.summaries("palworld")[2]
    assert third["window_start"] == (PACIFIC_DUE - timedelta(minutes=30)).isoformat()
    # Yesterday's story is sent back as a prior headline (so it can say "same as yesterday").
    assert "palworld story" in world.llm.calls[2][1]
    assert "<prior_stories>" in world.llm.calls[2][1]


async def test_a_free_guild_following_the_same_game_never_triggers_a_summary(world):
    world.guild(FREE, "free", BERLIN, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE

    assert await prepare_summaries(world.deps()) == []
    assert world.llm.calls == [] and world.summaries() == []

    # Nor does its own digest: the free render reads headlines, not summaries.
    digest_deps = GuildDigestDeps(
        cfg=world.cfg,
        db_path=world.db_path,
        now=world.clock,
        publisher_for=lambda *a: FakePublisher(),
        notify_guild=_noop_notify,
        summary_for=summary_lookup(world.deps()),
        sleep=world.sleep,
        pace_s=0,
    )
    await run_guild_digest(digest_deps, FREE, kind=RunKind.SCHEDULED)
    assert world.llm.calls == []


async def _noop_notify(guild_id, text):
    return None


def test_upcoming_dues_skips_unusable_zones_and_rolls_to_tomorrow_after_the_grace():
    # Was `earliest_dues` (one entry per game); the reuse rule is per server now, so
    # the prepare job needs every server's due time, not just the earliest.
    follows = [
        CompedFollow("palworld", 1, "09:00", "Not/AZone"),
        CompedFollow("palworld", 2, "09:00", BERLIN),
        CompedFollow("rust", 3, "25:99", LA),
    ]
    at = BERLIN_DUE + timedelta(minutes=29)
    assert upcoming_dues(follows, at) == [(follows[1], BERLIN_DUE, date(2026, 10, 1))]
    later = BERLIN_DUE + timedelta(minutes=31)
    assert upcoming_dues(follows, later) == [
        (follows[1], BERLIN_DUE + timedelta(days=1), date(2026, 10, 2))
    ]


# --- what the model is told ---


async def test_all_topics_is_the_comped_followed_games_in_catalog_order(world):
    world.guild(PAC, "comped", LA, ["palworld", "borderlands4"])  # follow order isn't catalog order
    world.guild(FREE, "free", LA, ["rust"])  # a free server's games don't count
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)

    await prepare_summaries(world.deps())
    system = world.llm.calls[0][0]
    assert "about the video games Borderlands 4 and Palworld." in system

    # A second comped server following something else changes the list, and so the prompt
    # (plan risk 8: that is the day an owner /newsbot preview is needed).
    world.guild(BER, "comped", LA, ["rust"])
    world.item("palworld", "another thing", PACIFIC_DUE - timedelta(minutes=10))
    world.clock.t = PACIFIC_DUE + timedelta(days=1) - timedelta(minutes=20)
    await prepare_summaries(world.deps())
    assert "video games Borderlands 4, Palworld and Rust." in world.llm.calls[-1][0]


async def test_the_model_input_for_the_friends_server_is_byte_identical_to_v22s(
    tmp_path, monkeypatch
):
    w = World(tmp_path, monkeypatch, "config_v2_prodlike.yaml", now=PACIFIC_DUE)
    assert ensure_imported(w.db_path, w.cfg, lambda: PACIFIC_DUE - timedelta(days=3)) is not None
    raw = []
    for n, (title, trust, published_h, topics) in enumerate(
        [
            ("Borderlands 4 patch notes", "official", 2, ("borderlands4",)),
            ("Gearbox is hiring", "press", 3, None),  # entity only: an uncertain match
            ("Borderlands 4 fan theory", "community", 5, ("borderlands4",)),
            ("Palworld raid guide", "press", 4, ("palworld",)),
        ]
    ):
        published = PACIFIC_DUE - timedelta(hours=published_h, minutes=n)
        raw.append(
            RawItem(
                url=f"https://example.com/{n}",
                title=title,
                excerpt=("Excerpt text. " * 60) if n == 0 else f"Excerpt {n}",
                source_name="Feed",
                trust=trust,
                published_at=published,
                topics=topics,
            )
        )
    grouped = filter_items(raw, w.cfg.topics, w.cfg.digest.max_items_per_topic)
    with w.conn() as conn:
        stored = {}
        for key, topic_items in grouped.items():
            for ti in topic_items:
                stored.setdefault(ti.item.url, (ti.item, {}))[1][key] = ti.uncertain
        for item, tags in stored.values():
            repo.store_items(
                conn,
                [
                    StoredItem(
                        item.url,
                        item.title,
                        item.excerpt,
                        item.source_name,
                        item.trust,
                        item.published_at,
                        tags,
                    )
                ],
                now=lambda: PACIFIC_DUE - timedelta(hours=1),
            )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, created_at) "
            "VALUES ('borderlands4', 'Yesterday''s headline', 's', 'reported', ?)",
            ((PACIFIC_DUE - timedelta(days=1)).isoformat(),),
        )
        conn.commit()
        prior = repo.recent_headlines(conn, "borderlands4", PACIFIC_DUE - timedelta(days=3))
    assert prior and grouped["borderlands4"]
    topic = next(t for t in w.cfg.topics if t.key == "borderlands4")
    expected = build_prompt(
        topic,
        grouped["borderlands4"],
        prior,
        all_topics=w.cfg.topics,
        subject=w.cfg.digest.subject,
    )

    w.clock.t = PACIFIC_DUE - timedelta(minutes=10)
    await ensure_summary(w.deps(), "borderlands4", PACIFIC_DUE, run_date=date(2026, 10, 1))

    assert w.llm.calls[0][0] == expected[0]
    assert w.llm.calls == [expected]
    # ...and the three games really are the friend's three, in v2's order.
    assert "video games Borderlands 4, Palworld and Diablo IV." in expected[0]


async def test_the_friends_guild_after_the_import_gets_summaries_for_its_three_games(
    tmp_path, monkeypatch
):
    w = World(tmp_path, monkeypatch, "config_v2_prodlike.yaml", now=PACIFIC_DUE)
    report = ensure_imported(w.db_path, w.cfg, lambda: PACIFIC_DUE - timedelta(days=2))
    assert report is not None
    for game in ("borderlands4", "palworld", "diablo4"):
        w.item(game, f"{game} news", PACIFIC_DUE - timedelta(hours=4))
    w.clock.t = PACIFIC_DUE - timedelta(minutes=29)

    deps = w.deps(web=True)
    try:
        done = await prepare_summaries(deps)
    finally:
        await w.http.aclose()

    assert done == ["borderlands4", "palworld", "diablo4"]
    assert w.llm.games() == done
    assert [r["status"] for r in w.summaries()] == ["ok"] * 3

    # And the digest, when it's due, is built from them without asking Claude again.
    w.clock.t = PACIFIC_DUE + timedelta(seconds=30)
    publisher = FakePublisher()
    digest_deps = GuildDigestDeps(
        cfg=w.cfg,
        db_path=w.db_path,
        now=w.clock,
        publisher_for=lambda *a: publisher,
        notify_guild=_noop_notify,
        summary_for=summary_lookup(w.deps()),
        sleep=w.sleep,
        pace_s=0,
    )
    outcome = await run_guild_digest(digest_deps, report.guild_id, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok"
    assert [m.topic_key for m in publisher.posted] == ["borderlands4", "palworld", "diablo4"]
    assert len(w.llm.calls) == 3


# --- web search ---


async def test_web_search_runs_once_per_game_per_day_and_only_for_comped_games(world):
    world.guild(PAC, "comped", LA, ["palworld", "borderlands4"])
    world.guild(FREE, "free", LA, ["rust"])
    world.item("palworld", "a thing", ago(2))
    deps = world.deps(web=True)
    world.clock.t = PACIFIC_DUE - timedelta(minutes=30)

    try:
        assert await prepare_summaries(deps) == ["borderlands4", "palworld"]
        # Borderlands 4 has its own search_queries (one); Palworld uses the two templates.
        assert sorted(world.brave) == [
            "Borderlands 4 news",
            "Palworld news",
            "Palworld update OR patch OR leak",
        ]
        assert not any("Rust" in q for q in world.brave)
        searched = len(world.brave)

        world.clock.t += timedelta(minutes=10)
        assert await prepare_summaries(deps) == []
        assert len(world.brave) == searched
    finally:
        await world.http.aclose()
    # What it found went through the same store, so the summary could see it.
    assert any("A web headline" in user for _, user in world.llm.calls)


@pytest.mark.parametrize("why", ["quota", "no key"])
async def test_a_web_search_that_cant_run_is_a_coverage_note_not_a_failure(world, why):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=30)
    if why == "quota":
        world.brave_status = 429
        deps = world.deps(web=True)
    else:
        deps = world.deps(web=True, brave_api_key=None)

    try:
        await prepare_summaries(deps)
    finally:
        await world.http.aclose()

    [row] = world.summaries("palworld")
    assert row["status"] == "ok"
    assert json.loads(row["coverage_notes"]) == ["web search skipped"]


async def test_the_prepare_job_never_ran_so_the_digest_makes_one_inline_without_web_search(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "palworld headline", PACIFIC_DUE - timedelta(hours=3))
    world.clock.t = PACIFIC_DUE + timedelta(seconds=30)
    publisher = FakePublisher()
    deps = GuildDigestDeps(
        cfg=world.cfg,
        db_path=world.db_path,
        now=world.clock,
        publisher_for=lambda *a: publisher,
        notify_guild=_noop_notify,
        summary_for=summary_lookup(world.deps(web=True)),
        sleep=world.sleep,
        pace_s=0,
    )
    try:
        outcome = await run_guild_digest(deps, PAC, kind=RunKind.SCHEDULED)
    finally:
        await world.http.aclose()

    assert world.brave == []
    assert world.llm.games() == ["palworld"]
    [message] = publisher.posted
    assert message.embed.footer.text.endswith("web search skipped")
    assert outcome.status == "partial"  # coverage gaps make a digest partial, as in v2


# --- failure ---


@pytest.mark.parametrize(
    ("error", "attempts"),
    [(LLMError("boom", input_tokens=7, output_tokens=1), 3), (LLMFatalError("400"), 1)],
)
async def test_a_claude_failure_is_a_fallback_a_headline_digest_and_one_alert(
    world, error, attempts
):
    world.guild(PAC, "comped", LA, ["palworld", "borderlands4"])
    world.item("palworld", "palworld headline", PACIFIC_DUE - timedelta(hours=3))
    world.item("borderlands4", "bl4 headline", PACIFIC_DUE - timedelta(hours=3))
    world.llm = FakeLLM(fail={"palworld": error})  # borderlands4 is fine
    world.clock.t = PACIFIC_DUE - timedelta(minutes=30)

    assert await prepare_summaries(world.deps()) == ["borderlands4", "palworld"]
    assert world.llm.games().count("palworld") == attempts
    statuses = {r["game_key"]: r["status"] for r in world.summaries()}
    assert statuses == {"borderlands4": "ok", "palworld": "fallback"}
    assert world.alerts == ["newsbot: summary for Palworld fell back to headlines"]

    # No retry that day: not from the next prepare tick, not from the digest itself.
    calls = len(world.llm.calls)
    world.clock.t = PACIFIC_DUE + timedelta(seconds=30)
    assert await prepare_summaries(world.deps()) == []
    publisher = FakePublisher()
    digest_deps = GuildDigestDeps(
        cfg=world.cfg,
        db_path=world.db_path,
        now=world.clock,
        publisher_for=lambda *a: publisher,
        notify_guild=_noop_notify,
        summary_for=summary_lookup(world.deps()),
        sleep=world.sleep,
        pace_s=0,
    )
    outcome = await run_guild_digest(digest_deps, PAC, kind=RunKind.SCHEDULED)
    assert len(world.llm.calls) == calls
    assert len(world.alerts) == 1
    by_game = {m.topic_key: str(m.embed.description) for m in publisher.posted}
    assert by_game["palworld"].startswith("Summary unavailable; showing headlines.")
    assert "palworld headline" in by_game["palworld"]
    assert "borderlands4 story" in by_game["borderlands4"]
    assert outcome.status == "partial"


async def test_failed_attempts_tokens_are_recorded_on_the_row(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMError("boom", input_tokens=7, output_tokens=2)})
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))
    [row] = world.summaries()
    assert (row["input_tokens"], row["output_tokens"]) == (21, 6)
    with world.conn() as conn:
        usage = repo.game_summary_tokens(conn, BERLIN_DUE - timedelta(days=1))
    assert (usage.input_tokens, usage.output_tokens) == (21, 6)


async def test_a_game_with_no_items_costs_no_call_and_is_saved_ok_and_empty(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))
    assert got is not None and got.status == "ok" and got.stories == []
    assert world.llm.calls == []


async def test_run_now_force_retries_a_fallback_once(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "palworld headline", PACIFIC_DUE - timedelta(hours=3))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    world.clock.t = PACIFIC_DUE + timedelta(seconds=30)
    publisher = FakePublisher()

    def deps_for():
        sd = world.deps()
        return GuildDigestDeps(
            cfg=world.cfg,
            db_path=world.db_path,
            now=world.clock,
            publisher_for=lambda *a: publisher,
            notify_guild=_noop_notify,
            summary_for=summary_lookup(sd),
            retry_summary=retry_lookup(sd),
            sleep=world.sleep,
            pace_s=0,
        )

    await run_guild_digest(deps_for(), PAC, kind=RunKind.SCHEDULED)
    assert world.summaries()[0]["status"] == "fallback" and len(world.llm.calls) == 1

    # The model recovers; the admin confirms run-now. One retry, then it's a real summary.
    world.llm = FakeLLM()
    world.clock.t += timedelta(minutes=10)
    await run_guild_digest(deps_for(), PAC, kind=RunKind.RUN_NOW, force=True)
    assert world.llm.games() == ["palworld"]
    [row] = world.summaries()
    assert row["status"] == "ok"
    assert "palworld story" in str(publisher.posted[-1].embed.description)

    # Nothing left to retry: another run-now doesn't call Claude.
    await run_guild_digest(deps_for(), PAC, kind=RunKind.RUN_NOW, force=True)
    assert len(world.llm.calls) == 1


async def test_run_now_retry_that_fails_again_costs_one_attempt_set_not_a_loop(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "palworld headline", PACIFIC_DUE - timedelta(hours=3))
    world.llm = FakeLLM(fail={"*": LLMError("boom")})
    world.clock.t = PACIFIC_DUE + timedelta(seconds=30)
    sd = world.deps()
    await ensure_summary(sd, "palworld", PACIFIC_DUE, run_date=date(2026, 10, 1))
    assert len(world.llm.calls) == 3

    got = await retry_lookup(sd)("palworld", PACIFIC_DUE)
    assert got is not None and got.status == "fallback"
    assert len(world.llm.calls) == 6
    [row] = world.summaries()
    assert row["status"] == "fallback"


# --- storage ---


async def test_stories_carry_the_summary_id_and_no_digest(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))
    [summary] = world.summaries()
    [story] = world.rows("SELECT * FROM stories")
    assert story["summary_id"] == summary["id"] and story["digest_id"] is None
    assert story["topic_key"] == "palworld"
    assert world.rows("SELECT COUNT(*) AS n FROM story_items")[0]["n"] == 1
    assert (summary["input_tokens"], summary["output_tokens"]) == (100, 20)


async def test_reuse_rule_edges(world):
    # The rule changed from "ended in (due - 24h, due + 30min]" to "ended after the server's
    # last digest, and no more than six hours before this one's due time".
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    sd = world.deps()
    await ensure_summary(sd, "palworld", BERLIN_DUE, run_date=date(2026, 10, 1))
    window_end = BERLIN_DUE
    assert len(world.llm.calls) == 1

    async def calls_after(due_at, after, day):
        before = len(world.llm.calls)
        await ensure_summary(sd, "palworld", due_at, after=after, run_date=day)
        return len(world.llm.calls) - before

    # Reused: a first digest (nothing to repeat), one due exactly six hours later, one due
    # long before (no upper bound any more), and one whose last digest ended a moment earlier.
    assert await calls_after(window_end + repo.SUMMARY_MAX_AGE, None, date(2026, 10, 2)) == 0
    assert await calls_after(window_end - timedelta(days=30), None, date(2026, 10, 3)) == 0
    assert await calls_after(window_end, window_end - timedelta(seconds=1), date(2026, 10, 4)) == 0
    # A server whose last digest ended at that very instant would read it twice: new summary.
    world.item("palworld", "newer", window_end + timedelta(hours=1))
    world.clock.t = window_end + timedelta(hours=2)
    assert await calls_after(window_end + timedelta(hours=2), window_end, date(2026, 10, 5)) == 1
    # And one due more than six hours after the newest summary ended can't use it either.
    newest_end = window_end + timedelta(hours=2)
    world.item("palworld", "newest", newest_end + timedelta(hours=1))
    world.clock.t = newest_end + repo.SUMMARY_MAX_AGE + timedelta(seconds=1)
    due = world.clock.t
    assert await calls_after(due, None, date(2026, 10, 6)) == 1
