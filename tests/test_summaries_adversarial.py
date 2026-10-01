"""Adversarial tests for comped summaries (plan task 7): one call, one prompt, one good row.

`test_summaries.py` is the well-behaved afternoon. This file is the one where
the prepare job and three digests all want the same paragraph at the same
instant and somebody's clock is wrong. In order of how much they'd cost if I
had them wrong:

1. Does the friend's model input stay byte-for-byte v2.2's, however the items,
   prior headlines and web results happen to be arranged? And can a free
   server, or a comped one that isn't set up yet, ever change the prompt?
2. Is it really one Claude call per game per day, under overlapping jobs,
   races between prepare and inline lookups, run-now retries, and a stale
   claim takeover? Does every exit path let go of the claim?
3. What's the most a broken API (or a broken disk) can cost in a day?
4. Do the windows chain with no item in two summaries, and does the reuse
   rule hand the right summary to the right server, every day of the year?
5. Does one game's bad day stay its own?

What this found, each first kept as a strict xfail and now fixed (the tests
below are the regression tests):

- the reuse window was "24 hours before the due time", and a day that is only
  23 hours long (spring forward) or a digest time moved more than half an hour
  earlier made tomorrow's digest reuse yesterday's summary and post it twice,
  and one late summary flipped which server "goes first" for good. The rule is
  per server now: newer than that server's last digest, at most six hours old;
- an inline summary for a missed digest shared a `run_date` with the prepared
  row it had no business touching, and replaced it (`run_date` is a label now);
- a run-now waiting behind another run-now's retry took the old fallback;
- a stale claim takeover made a second call, and a second save, while the
  first caller was alive (the claim is refreshed now, and a save that lost its
  claim is thrown away);
- a model call that hung stalled every later game in the same prepare run
  (there's a per-game timeout now);
- two overlapping prepare jobs each ran the web search (it's inside the claim).

Cost under the per-server rule: a game costs at most one summary (one attempt
set) per distinct comped cycle per day, not strictly one per game; see the
cost tests for the pinned numbers.

Same rig as the happy-path file: a hand-written LLM that counts, a clock that
only moves when a test moves it, `httpx.MockTransport` for Brave, and sleeps
that just yield. No network, nothing waits for real.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from test_summaries import (
    _A,
    _B,
    BER,
    BERLIN,
    BERLIN_DUE,
    FREE,
    LA,
    PAC,
    PACIFIC_DUE,
    FakeLLM,
    FakePublisher,
    World,
    _noop_notify,
    ago,
)

from newsbot.collectors.base import RawItem
from newsbot.guilds.importer import ensure_imported
from newsbot.guilds.schedule import local_due_instant
from newsbot.pipeline import summaries
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.guild_digest import GuildDigestDeps, run_guild_digest
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import (
    ensure_summary,
    prepare_summaries,
    retry_lookup,
    summary_lookup,
)
from newsbot.pipeline.summarize import LLMError, LLMFatalError, LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import Coverage, PriorStory, StoredItem, StoryToSave, Usage

DAY1 = date(2026, 10, 1)
_T = "claim-t"  # a claim token, not a password


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# --- rigs ---


class Gated(FakeLLM):
    """Holds every call open until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def emit_stories(self, system, user):
        self.started.set()
        await self.gate.wait()
        return await super().emit_stories(system, user)


class EveryItem(FakeLLM):
    """Writes one story per item it's shown (the stock fake only writes the first)."""

    async def emit_stories(self, system, user):
        self.calls.append((system, user))
        items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        stories = [
            StoryOut(
                headline=f"story about {i['title']}",
                summary="What happened.",
                label="reported",
                item_urls=[i["url"]],
                relevant=True,
            )
            for i in items
        ]
        return LLMResult(StoriesOut(stories=stories), 10, 2)


class HangsFor(FakeLLM):
    def __init__(self, game: str) -> None:
        super().__init__()
        self.hang = game

    async def emit_stories(self, system, user):
        if f"(key: {self.hang})" in user:
            self.calls.append((system, user))
            await asyncio.Event().wait()
        return await super().emit_stories(system, user)


class Stop(BaseException):
    """Not an Exception: what `except Exception` is famous for missing."""


def titles(user: str) -> list[str]:
    payload = user.split("<items>\n", 1)[1].split("\n</items>", 1)[0]
    return [i["title"] for i in json.loads(payload)]


def urls(user: str) -> list[str]:
    payload = user.split("<items>\n", 1)[1].split("\n</items>", 1)[0]
    return [i["url"] for i in json.loads(payload)]


def digest_deps(world, sd, publisher=None, **over):
    args = dict(
        cfg=world.cfg,
        db_path=world.db_path,
        now=world.clock,
        publisher_for=lambda *a: publisher or FakePublisher(),
        notify_guild=_noop_notify,
        summary_for=summary_lookup(sd),
        retry_summary=retry_lookup(sd),
        sleep=world.sleep,
        pace_s=0,
    )
    args.update(over)
    return GuildDigestDeps(**args)


def claims(world):
    return world.rows("SELECT value FROM app_state WHERE key LIKE 'summary_claim:%'")


async def settle(n: int = 30) -> None:
    """Let threads and tasks make progress (to_thread needs real scheduler turns)."""
    for _ in range(n):
        await asyncio.sleep(0.005)


# --- 1. prompt integrity ---

_FRIEND_CASES = [
    "forward",
    "reversed_store_order",
    "web_item",
    "undated_item",
    "over_the_cap",
    "prior_across_day_boundaries",
]


@pytest.mark.parametrize("case", _FRIEND_CASES)
async def test_the_friends_model_input_stays_v22s_however_the_inputs_are_arranged(
    tmp_path, monkeypatch, case
):
    w = World(tmp_path, monkeypatch, "config_v2_prodlike.yaml", now=PACIFIC_DUE)
    assert ensure_imported(w.db_path, w.cfg, lambda: PACIFIC_DUE - timedelta(days=3)) is not None
    end = PACIFIC_DUE - timedelta(minutes=10)

    def raw_item(n, title, trust, hours, topics, *, source="Feed", excerpt=None, undated=False):
        return RawItem(
            url=f"https://example.com/{n}",
            title=title,
            excerpt=excerpt if excerpt is not None else f"Excerpt {n}",
            source_name=source,
            trust=trust,
            published_at=None if undated else end - timedelta(hours=hours, minutes=n),
            topics=topics,
        )

    raw = [
        raw_item(0, "Borderlands 4 patch notes", "official", 2, ("borderlands4",)),
        raw_item(1, "Gearbox is hiring", "press", 3, None),
        raw_item(2, "Borderlands 4 fan theory", "community", 5, ("borderlands4",)),
        raw_item(3, "Palworld raid guide", "press", 4, ("palworld",)),
        raw_item(4, "Borderlands 4 loot guide", "press", 4, ("borderlands4",)),
    ]
    if case == "web_item":
        raw.append(raw_item(5, "Borderlands 4 leak", "press", 1, ("borderlands4",), source="Brave"))
    if case == "undated_item":
        raw.append(raw_item(5, "Borderlands 4 rumor", "press", 1, ("borderlands4",), undated=True))
    if case == "over_the_cap":
        raw.extend(
            raw_item(100 + n, f"Borderlands 4 filler {n}", "press", 6 + n // 4, ("borderlands4",))
            for n in range(65)
        )
    prior_specs: list[tuple[timedelta, str]] = []
    if case == "prior_across_day_boundaries":
        prior_specs = [
            (timedelta(days=3) - timedelta(seconds=1), "Almost three days ago"),
            (timedelta(days=3), "Exactly three days ago"),
            (timedelta(days=3, seconds=1), "Just out of range"),
            (timedelta(days=1, hours=8), "Yesterday morning"),
            (timedelta(minutes=30), "This morning"),
        ]

    everything = filter_items(raw, w.v2.topics, 10**6)
    capped = filter_items(raw, w.v2.topics, w.v2.digest.max_items_per_topic)
    tags: dict[str, tuple[RawItem, dict[str, bool]]] = {}
    for key, topic_items in everything.items():
        for ti in topic_items:
            tags.setdefault(ti.item.url, (ti.item, {}))[1][key] = ti.uncertain
    order = list(tags.values())
    if case == "reversed_store_order":
        order.reverse()
    with w.conn() as conn:
        for item, topics in order:
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
                        topics,
                    )
                ],
                now=lambda: end - timedelta(hours=1),
            )
        for n, (age, headline) in enumerate(prior_specs):
            conn.execute(
                "INSERT INTO stories (id, topic_key, headline, summary, label, created_at) "
                "VALUES (?, 'borderlands4', ?, 's', 'reported', ?)",
                (900 + n, headline, (end - age).isoformat()),
            )
        conn.commit()
    in_range = [
        PriorStory(900 + n, headline, end - age)
        for n, (age, headline) in enumerate(prior_specs)
        if age <= timedelta(days=3)
    ]
    in_range.sort(key=lambda p: p.created_at, reverse=True)
    topic = next(t for t in w.v2.topics if t.key == "borderlands4")
    expected = build_prompt(
        topic,
        capped["borderlands4"],
        in_range,
        all_topics=w.v2.topics,
        subject=w.v2.digest.subject,
    )

    w.clock.t = end
    await ensure_summary(w.deps(), "borderlands4", PACIFIC_DUE, run_date=DAY1)

    assert w.llm.calls == [expected]
    if case == "over_the_cap":
        assert len(titles(w.llm.calls[0][1])) == w.v2.digest.max_items_per_topic
    if case == "prior_across_day_boundaries":
        assert "Just out of range" not in w.llm.calls[0][1]
        assert "Exactly three days ago" in w.llm.calls[0][1]


async def test_a_free_servers_games_never_reach_the_prompt_however_many_it_follows(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.guild(FREE, "free", LA, ["borderlands4", "palworld", "rust"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)

    await prepare_summaries(world.deps())

    system = world.llm.calls[0][0]
    assert "about the video games Palworld." in system
    assert "Rust" not in system and "Borderlands" not in system


async def test_a_comped_server_that_is_not_set_up_yet_cannot_change_the_prompt(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    with world.conn() as conn:
        repo.create_guild(conn, 7, timezone=LA, tier="comped", set_up=False, now=world.clock)
        repo.set_guild_games(conn, 7, [("rust", 1), ("borderlands4", 2)])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)

    assert await prepare_summaries(world.deps()) == ["palworld"]

    assert "about the video games Palworld." in world.llm.calls[0][0]


async def test_a_comped_server_following_nothing_changes_nothing(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    with world.conn() as conn:
        repo.create_guild(conn, 7, timezone=LA, tier="comped", set_up=True, now=world.clock)
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)

    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert "about the video games Palworld." in world.llm.calls[0][0]


async def test_a_server_that_leaves_or_is_downgraded_drops_out_of_the_prompt(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.guild(BER, "comped", LA, ["rust"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    await prepare_summaries(world.deps())
    assert "video games Palworld and Rust." in world.llm.calls[-1][0]

    with world.conn() as conn:
        conn.execute("UPDATE guilds SET tier = 'free' WHERE guild_id = ?", (BER,))
        conn.commit()
    world.item("palworld", "another", PACIFIC_DUE - timedelta(minutes=5))
    world.clock.t = PACIFIC_DUE + timedelta(days=1) - timedelta(minutes=20)
    await prepare_summaries(world.deps())
    assert "about the video games Palworld." in world.llm.calls[-1][0]

    with world.conn() as conn:
        repo.delete_guild(conn, PAC)
    follows = summaries._follows_sync(world.db_path)
    assert follows == []


async def test_asking_for_a_game_nobody_comped_follows_names_only_that_game(world):
    # Not reachable from a digest (it only asks about games its own server follows), but
    # pinned: the requested game is always in the list, and nothing else sneaks in.
    world.guild(FREE, "free", LA, ["rust"])
    world.item("rust", "a thing", ago(2))
    await ensure_summary(world.deps(), "rust", BERLIN_DUE, run_date=DAY1)
    assert "about the video games Rust." in world.llm.calls[0][0]


async def test_guides_deals_and_codes_go_to_the_model_and_come_back_as_stories(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    want = [
        "Palworld full walkthrough and guide",
        "Palworld 40% off this weekend",
        "Palworld SHiFT code AAAAA-BBBBB-CCCCC-DDDDD-EEEEE",
    ]
    for n, title in enumerate(want):
        world.item("palworld", title, ago(3 - n * 0.5), excerpt="A guide, a deal, a code.")
    world.llm = EveryItem()

    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)

    assert sorted(titles(world.llm.calls[0][1])) == sorted(want)
    assert got is not None
    assert sorted(s.headline for s in got.stories) == sorted(f"story about {t}" for t in want)
    # The system prompt is the one build_prompt makes, with nothing bolted on to drop these.
    topic = next(g for g in world.cfg.catalog if g.key == "palworld")
    assert world.llm.calls[0][0] == build_prompt(topic, [], [], subject="video games")[0]


# --- 2. exactly one Claude call ---


async def test_two_prepare_jobs_overlapping_make_one_call(world):
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.item("palworld", "a thing", ago(3))
    world.clock.t = BERLIN_DUE - timedelta(minutes=10)
    world.llm = FakeLLM(yields=10)
    deps = world.deps()

    first, second = await asyncio.gather(prepare_summaries(deps), prepare_summaries(deps))

    assert world.llm.games() == ["palworld"]
    # Whichever job got there second may find the row already saved and have nothing to do.
    assert "palworld" in first + second
    assert len(world.summaries()) == 1 and claims(world) == []


async def test_two_prepare_jobs_overlapping_search_the_web_once(world):
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.item("palworld", "a thing", ago(3))
    world.clock.t = BERLIN_DUE - timedelta(minutes=10)
    world.llm = FakeLLM(yields=10)
    deps = world.deps(web=True)
    try:
        await asyncio.gather(prepare_summaries(deps), prepare_summaries(deps))
    finally:
        await world.http.aclose()
    assert len(world.brave) == 2  # palworld's two queries, once


async def test_prepare_racing_a_digests_inline_lookup_and_a_close_second_server_is_one_call(world):
    # Was Berlin and Pacific; nine hours apart those are two cycles now (see the cost tests).
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(PAC, "comped", BERLIN, ["palworld"], time="09:05")
    world.item("palworld", "a thing", ago(3))
    world.clock.t = BERLIN_DUE
    world.llm = FakeLLM(yields=10)
    deps = world.deps()
    lookup = summary_lookup(deps)

    results = await asyncio.gather(
        prepare_summaries(deps),
        lookup("palworld", BERLIN_DUE),
        lookup("palworld", BERLIN_DUE + timedelta(minutes=5)),
        retry_lookup(deps)("palworld", BERLIN_DUE),
    )

    assert world.llm.games() == ["palworld"]
    assert all(r for r in results[1:] if r is not None)
    assert len(world.summaries()) == 1 and claims(world) == []


async def test_two_admins_running_now_against_one_fallback_cost_one_attempt_set(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    deps = world.deps()
    await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1)
    assert len(world.llm.calls) == 1

    world.llm = FakeLLM(yields=10)  # the model recovers
    deps = world.deps()
    a, b = await asyncio.gather(
        retry_lookup(deps)("palworld", BERLIN_DUE), retry_lookup(deps)("palworld", BERLIN_DUE)
    )

    assert len(world.llm.calls) == 1
    assert a is not None and b is not None
    assert "ok" in {a.status, b.status}
    [row] = world.summaries()
    assert row["status"] == "ok" and claims(world) == []


async def test_a_run_now_waiting_behind_another_retry_gets_the_retrys_result(world):
    # `claim_game_summary` checks "is there a reusable row?" before "is somebody working on it?".
    # The waiter's second look (no longer asking for a retry) finds the old fallback row and
    # takes it, while the winner is still mid-call. Cost-safe (still one call); just stale.
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)

    world.llm = Gated()
    deps = world.deps()
    winner = asyncio.create_task(retry_lookup(deps)("palworld", BERLIN_DUE))
    await world.llm.started.wait()
    waiter = asyncio.create_task(retry_lookup(deps)("palworld", BERLIN_DUE))
    await settle()
    world.llm.gate.set()
    won, waited = await asyncio.gather(winner, waiter)

    assert len(world.llm.calls) == 1  # the winner's retry, and nobody else's
    assert won.status == "ok"
    assert waited.status == "ok"


async def test_a_run_now_retry_racing_a_prepare_tick_does_not_double_up(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    deps = world.deps()
    await prepare_summaries(deps)
    assert len(world.llm.calls) == 1

    world.llm = FakeLLM(yields=10)
    deps = world.deps()
    await asyncio.gather(prepare_summaries(deps), retry_lookup(deps)("palworld", PACIFIC_DUE))

    assert len(world.llm.calls) == 1  # the retry's; prepare sees a row and stands down
    assert [r["status"] for r in world.summaries()] == ["ok"]


async def test_a_retry_that_fails_again_leaves_the_waiter_the_fallback_not_a_second_retry(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    deps = world.deps()
    await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1)
    before = len(world.llm.calls)

    world.llm = Gated()
    world.llm.fail = {"*": LLMFatalError("400")}
    deps = world.deps()
    winner = asyncio.create_task(retry_lookup(deps)("palworld", BERLIN_DUE))
    await world.llm.started.wait()
    waiter = asyncio.create_task(retry_lookup(deps)("palworld", BERLIN_DUE))
    await settle()
    world.llm.gate.set()
    a, b = await asyncio.gather(winner, waiter)

    assert len(world.llm.calls) == 1 and before == 1
    assert a.status == b.status == "fallback"


async def test_a_takeover_of_a_claim_whose_owner_is_alive_but_slow_is_not_a_second_call(world):
    # The owner refreshes its claim while it works, so a call that outlives the ten-minute lease
    # (the heartbeat is made quick here, and the clock jumps past the lease) keeps its claim and
    # nobody starts a second call. Real timeouts make this unreachable anyway; belt and braces.
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    world.llm = Gated()
    deps = world.deps(heartbeat_s=0.01)
    slow = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await world.llm.started.wait()

    world.clock.t += repo.SUMMARY_CLAIM_LEASE + timedelta(seconds=1)
    await settle()  # a heartbeat or two goes by
    rival = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await settle()
    world.llm.gate.set()
    got = await asyncio.gather(slow, rival)

    assert len(world.llm.calls) == 1
    assert all(g is not None and g.status == "ok" for g in got)
    assert len(world.summaries()) == 1 and claims(world) == []


async def test_a_takeover_with_a_dead_heartbeat_is_a_second_call_but_never_a_second_save(world):
    # What is guaranteed when the heartbeat can't save the day (here: it never fires): the
    # rival may make a second call, but the slow owner's save notices the claim is no longer its
    # own and throws its result away. One row, one set of stories, and both callers get it.
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    world.llm = Gated()
    deps = world.deps(heartbeat_s=3600.0)
    slow = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await world.llm.started.wait()

    world.clock.t += repo.SUMMARY_CLAIM_LEASE + timedelta(seconds=1)
    rival = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await settle()
    world.llm.gate.set()
    got = await asyncio.gather(slow, rival)

    assert len(world.llm.calls) == 2
    assert all(g is not None and g.status == "ok" for g in got)
    assert len(world.summaries()) == 1
    assert world.rows("SELECT COUNT(*) AS n FROM stories")[0]["n"] == 1
    assert claims(world) == []


def test_a_save_that_lost_its_claim_writes_nothing(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        t0 = BERLIN_DUE
        assert repo.claim_game_summary(conn, "g", t0, token=_A, now=lambda: t0)[0] == "claimed"
        late = t0 + repo.SUMMARY_CLAIM_LEASE + timedelta(seconds=1)
        assert repo.claim_game_summary(conn, "g", t0, token=_B, now=lambda: late)[0] == "claimed"

        def save_as(token, **kw):
            return repo.save_game_summary(
                conn,
                game_key="g",
                run_date=DAY1,
                status="ok",
                window_start=t0 - timedelta(hours=1),
                window_end=t0,
                coverage_notes=[],
                note=None,
                usage=Usage(1, 1),
                stories=[],
                token=token,
                now=lambda: t0,
                **kw,
            )

        assert save_as(_A, require_claim=True) is None  # its claim was taken over
        assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 0
        assert repo.app_state_get(conn, "summary_claim:g") is not None  # and B's claim is intact
        assert save_as(_B, require_claim=True) is not None
        assert repo.app_state_get(conn, "summary_claim:g") is None
        assert save_as(_A, require_claim=True) is None  # the claim is gone: still not A's
        assert save_as(_A) is not None  # callers that never claimed (imports, tests) still save
        assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 2


def test_the_claim_heartbeat_extends_only_its_own_lease(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        t0 = BERLIN_DUE
        assert repo.claim_game_summary(conn, "g", t0, token=_A, now=lambda: t0)[0] == "claimed"
        near_end = t0 + repo.SUMMARY_CLAIM_LEASE - timedelta(seconds=1)
        assert repo.refresh_game_summary_claim(conn, "g", _A, lambda: near_end)
        still = near_end + repo.SUMMARY_CLAIM_LEASE - timedelta(seconds=1)
        assert repo.claim_game_summary(conn, "g", t0, token=_B, now=lambda: still)[0] == "busy"
        assert not repo.refresh_game_summary_claim(conn, "g", _B, lambda: still)
        assert not repo.refresh_game_summary_claim(conn, "nothing", _A, lambda: still)


def test_a_live_claim_beats_a_fallback_row_but_not_an_ok_one(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        t0 = BERLIN_DUE

        def now():
            return t0

        save(conn, "g", DAY1, t0 - timedelta(hours=1), status="fallback")
        # No claim: the fallback is reusable, and a retry may claim over it.
        assert repo.claim_game_summary(conn, "g", t0, token=_A, now=now)[0] == "reusable"
        assert repo.claim_game_summary(conn, "h", t0, token=_A, now=now)[0] == "claimed"
        state, row = repo.claim_game_summary(conn, "g", t0, token=_A, now=now, retry_fallback=True)
        assert state == "claimed" and row is not None and row.status == "fallback"
        # A live claim (say, that retry) makes everyone else wait for its result.
        assert repo.claim_game_summary(conn, "g", t0, token=_B, now=now)[0] == "busy"
        # An ok row is never waiting on a retry: it's handed out even while somebody works.
        save(conn, "g", date(2026, 10, 2), t0 - timedelta(minutes=30))
        state, row = repo.claim_game_summary(conn, "g", t0, token=_B, now=now)
        assert state == "reusable" and row is not None and row.status == "ok"


@pytest.mark.parametrize("where", ["model_base_exception", "save", "inputs"])
async def test_the_claim_is_released_on_every_exit_path(world, monkeypatch, where):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    expected = RuntimeError if where != "model_base_exception" else Stop

    if where == "model_base_exception":
        world.llm = FakeLLM(fail={"*": Stop()})
    else:

        def boom(*a, **k):
            raise RuntimeError(where)

        monkeypatch.setattr(summaries, "_save_sync" if where == "save" else "_inputs_sync", boom)

    with pytest.raises(expected):
        await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)

    assert claims(world) == []
    assert world.summaries() == []
    assert world.alerts == []
    monkeypatch.undo()

    # Nobody has to wait out the lease: the next caller claims at once and gets a real summary.
    world.llm = FakeLLM()
    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    assert got is not None and got.status == "ok" and world.sleeps == []


async def test_a_release_that_blows_up_does_not_lose_the_summary(world, monkeypatch):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(summaries, "_release_sync", boom)
    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    assert got is not None and got.status == "ok"


async def test_cancelling_a_waiter_leaves_the_winners_claim_alone(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    world.llm = Gated()
    deps = world.deps()
    winner = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await world.llm.started.wait()
    loser = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await settle()
    loser.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loser

    assert len(claims(world)) == 1
    world.llm.gate.set()
    got = await winner
    assert got is not None and got.status == "ok" and claims(world) == []


async def test_an_unreadable_claim_is_a_dead_claim(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    with world.conn() as conn:
        repo.app_state_set(conn, "summary_claim:palworld", "{not json")
    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    assert got is not None and world.sleeps == []


# --- 3. cost ---


async def simulate(world, zones, *, days=6, outage=None, game="palworld"):
    """Five-minute prepare ticks and one digest lookup per server per day, for a few days.

    `zones` is `{guild_id: tz}`, every server at 09:00 and comped. `outage` is
    `(start, end)`: ticks inside it don't happen and a digest that fell inside it
    is caught up a minute after it ends (keeping its scheduled due instant, as the
    real catch-up does). Each digest leaves a `digests` row, so the reuse rule's "newer than
    this server's last digest" has something to look at. Returns `{guild_id: [hours between
    the summary's window end and the digest's due time, or None]}`, in day order.
    """
    start_day = DAY1
    for gid, tz in zones.items():
        world.guild(gid, "comped", tz, [game])
    stamps = [BERLIN_DUE - timedelta(days=3) + timedelta(hours=h) for h in range(24 * (days + 5))]
    with world.conn() as conn:
        for n, at in enumerate(stamps):
            repo.store_items(
                conn,
                [
                    StoredItem(
                        f"https://example.com/{game}/sim-{n}",
                        f"{game} news {n}",
                        "",
                        "Feed",
                        "press",
                        at,
                        {game: False},
                    )
                ],
                now=lambda at=at: at,
            )
    events = []
    for d in range(days):
        for gid, tz in zones.items():
            day = start_day + timedelta(days=d)
            due = local_due_instant(day, "09:00", tz)
            for k in range(-7, 8):
                events.append((due + timedelta(minutes=5 * k), 0, gid, due, day))
            events.append((due + timedelta(seconds=10), 1, gid, due, day))
    if outage:
        moved = []
        for at, kind, gid, due, day in events:
            if outage[0] <= at < outage[1]:
                if kind == 1:
                    moved.append((outage[1] + timedelta(minutes=1), 1, gid, due, day))
                continue
            moved.append((at, kind, gid, due, day))
        events = moved
    events.sort(key=lambda e: (e[0], e[1]))
    lags: dict[int, list[float | None]] = {gid: [] for gid in zones}
    deps = world.deps()
    lookup = summary_lookup(deps)
    for at, kind, gid, due, day in events:
        world.clock.t = at
        if kind == 0:
            await prepare_summaries(deps)
            continue
        with world.conn() as conn:
            coverage = repo.last_coverage(conn, gid, exclude_run_date=day)
            after = coverage.for_game(game) if coverage else None
        await lookup(game, due, after)
        with world.conn() as conn:
            row = repo.get_game_summary(conn, game, due, after)
            # The digest posted: it ends at its due instant, and covers the items stored by
            # then, except that a game taken from a summary ends where the summary did. Those
            # are what the next one chains on.
            upto = max(
                repo.latest_item_id(conn, collected_by=due), coverage.item_id if coverage else 0
            )
            taken = (
                {game: row.items_upto}
                if row is not None and row.status == "ok" and row.items_upto is not None
                else {}
            )
            conn.execute(
                "INSERT INTO digests (guild_id, run_date, status, window_start, window_end, "
                "items_upto, game_items_upto, created_at, updated_at) "
                "VALUES (?, ?, 'ok', ?, ?, ?, ?, 't', 't')",
                (
                    gid,
                    day.isoformat(),
                    (due - timedelta(hours=24)).isoformat(),
                    due.isoformat(),
                    upto,
                    json.dumps(taken),
                ),
            )
            conn.commit()
        lags[gid].append(None if row is None else (due - row.window_end).total_seconds() / 3600)
    return lags


async def test_a_failing_api_costs_one_attempt_set_per_cycle_not_per_server_or_tick(world):
    lags_days = 4
    world.llm = FakeLLM(fail={"*": LLMError("overloaded", input_tokens=5, output_tokens=0)})
    await simulate(world, {BER: BERLIN, PAC: LA}, days=lags_days)
    # Three attempts inside one summarize_topic per attempt set. Berlin and Pacific are nine
    # hours apart, which is two cycles a day under the per-server reuse rule (it was one a day
    # under the old 24 hour one); however many prepare ticks and digests look at each, no more.
    assert len(world.llm.calls) == 3 * 2 * lags_days
    assert len(world.alerts) == 2 * lags_days
    assert {r["status"] for r in world.summaries()} == {"fallback"}


async def test_a_fatal_api_error_costs_one_call_per_cycle(world):
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    await simulate(world, {BER: BERLIN, PAC: LA}, days=3)
    assert len(world.llm.calls) == 2 * 3


async def test_four_servers_six_hours_apart_are_the_worst_case_four_summaries_a_day(world):
    # The recomputed bound. A summary made at the first digest's lead serves every digest due
    # up to 5h30 later (six hours, less the half hour lead), so the next cycle starts more than
    # 5h30 after the last one began: at most four per game per day, however many servers follow.
    # Four servers six hours apart is that worst case; servers a few hours apart share one.
    days = 5
    await simulate(
        world,
        {1: "Etc/GMT-8", 2: "Etc/GMT-2", 3: "Etc/GMT+4", 4: "Etc/GMT+10"},  # 01, 07, 13, 19 UTC
        days=days,
    )
    assert len(world.llm.calls) == 4 * days == len(world.summaries())


async def test_a_disk_that_wont_save_costs_two_attempt_sets_a_cycle_not_thirteen(
    world, monkeypatch
):
    # The worst case I could find: the row can't be written, so "a row exists" never becomes
    # true and every five-minute tick tried again, for as long as the job considers the digest
    # upcoming (30 minutes before it to 30 minutes after: 13 ticks, 39 calls). Now a game whose
    # save failed rests for 30 minutes, then 60, and so on: two attempts in that window.
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMError("overloaded")})

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(summaries, "_save_sync", boom)
    deps = world.deps()
    attempts = []
    for minutes in range(-180, 181, 5):
        world.clock.t = BERLIN_DUE + timedelta(minutes=minutes)
        before = len(world.llm.calls)
        assert await prepare_summaries(deps) == []
        if len(world.llm.calls) > before:
            assert len(world.llm.calls) - before == 3
            attempts.append(minutes)
    assert attempts == [-30, 0]  # the second one is the backoff's 30 minutes, to the tick
    assert len(world.llm.calls) == 6
    assert claims(world) == []


def test_the_save_backoff_doubles_and_stops_at_its_cap(world):
    deps = world.deps()
    waits = []
    for _ in range(8):
        summaries._note_save_failure(deps, "palworld")
        waits.append(deps.save_failures["palworld"][1] - world.clock.t)
    hour = timedelta(hours=1)
    assert waits == [hour / 2, hour, 2 * hour, 4 * hour, 8 * hour, 8 * hour, 8 * hour, 8 * hour]
    assert waits[-1] == summaries.SAVE_BACKOFF_MAX
    assert summaries.SAVE_BACKOFF == waits[0]


async def test_a_game_that_failed_to_save_rests_then_recovers_and_one_good_save_clears_it(
    world, monkeypatch
):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    real = summaries._save_sync
    broken = [True]

    def flaky(*a, **k):
        if broken[0]:
            raise RuntimeError("disk full")
        return real(*a, **k)

    monkeypatch.setattr(summaries, "_save_sync", flaky)
    deps = world.deps()
    with pytest.raises(RuntimeError):
        await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1)
    assert len(world.llm.calls) == 1

    # Resting: a digest's inline lookup gets nothing, costs nothing, and leaves no claim behind.
    world.clock.t += summaries.SAVE_BACKOFF - timedelta(seconds=1)
    assert await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1) is None
    assert len(world.llm.calls) == 1 and claims(world) == []

    # Rested, and the disk is back: a real summary, and the game's record is wiped.
    broken[0] = False
    world.clock.t += timedelta(seconds=1)
    got = await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1)
    assert got is not None and got.status == "ok" and len(world.llm.calls) == 2
    assert deps.save_failures == {}


async def test_a_failing_api_with_repeated_run_now_costs_an_attempt_set_each_time_and_no_loop(
    world,
):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMError("overloaded")})
    deps = world.deps()
    await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1)
    for n in range(1, 4):
        await retry_lookup(deps)("palworld", BERLIN_DUE)
        assert len(world.llm.calls) == 3 * (n + 1)
    assert len(world.summaries()) == 1


async def test_tokens_are_recorded_for_a_fatal_fallback_and_lost_for_an_unexpected_crash(world):
    world.guild(PAC, "comped", LA, ["palworld", "borderlands4"])
    world.item("palworld", "a thing", ago(2))
    world.item("borderlands4", "a thing", ago(2))
    world.llm = FakeLLM(
        fail={
            "palworld": LLMFatalError("400", input_tokens=40, output_tokens=3),
            "borderlands4": RuntimeError("a bug I haven't met"),
        }
    )
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    await ensure_summary(world.deps(), "borderlands4", BERLIN_DUE, run_date=DAY1)
    by_game = {
        r["game_key"]: (r["status"], r["input_tokens"], r["output_tokens"])
        for r in world.summaries()
    }
    assert by_game["palworld"] == ("fallback", 40, 3)
    # No usage number exists for a crash, so the row says zero; documented, not a defect.
    assert by_game["borderlands4"] == ("fallback", 0, 0)


async def test_replaced_rows_add_their_tokens_and_the_spend_sum_counts_each_once(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400", input_tokens=21, output_tokens=6)})
    deps = world.deps()
    await ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1)
    world.llm = FakeLLM(usage=(100, 20))
    deps = world.deps()
    await retry_lookup(deps)("palworld", BERLIN_DUE)
    [row] = world.summaries()
    assert (row["input_tokens"], row["output_tokens"]) == (121, 26)
    with world.conn() as conn:
        usage = repo.game_summary_tokens(conn, BERLIN_DUE - timedelta(days=1))
        assert (usage.input_tokens, usage.output_tokens) == (121, 26)
        # Another game's row is a separate row and adds on top.
        repo.save_game_summary(
            conn,
            game_key="rust",
            run_date=DAY1,
            status="ok",
            window_start=ago(24),
            window_end=ago(0),
            coverage_notes=[],
            note=None,
            usage=Usage(1, 2),
            stories=[],
            token=_T,
            now=lambda: BERLIN_DUE,
        )
        usage = repo.game_summary_tokens(conn, BERLIN_DUE - timedelta(days=1))
        assert (usage.input_tokens, usage.output_tokens) == (122, 28)
        # The since bound is inclusive and uses the row's latest save time.
        assert repo.game_summary_tokens(conn, BERLIN_DUE + timedelta(seconds=1)) == Usage(0, 0)


# --- 4. windows and reuse ---


def save(
    conn,
    game,
    run_date,
    window_end,
    *,
    status="ok",
    stories=(),
    usage=None,
    replace_id=None,
    items_after=None,
    items_upto=None,
):
    return repo.save_game_summary(
        conn,
        game_key=game,
        run_date=run_date,
        status=status,
        window_start=window_end - timedelta(hours=24),
        window_end=window_end,
        coverage_notes=[],
        note=None,
        usage=usage or Usage(0, 0),
        stories=list(stories),
        token=_T,
        now=lambda: window_end,
        replace_id=replace_id,
        items_after=items_after,
        items_upto=items_upto,
    )


def test_a_row_saved_exactly_on_the_reuse_boundaries(tmp_path):
    # Was the (due - 24h, due + 30min] edges. Now: at or after due - 6h, strictly after the
    # server's last digest's window end, and starting exactly where the server's coverage ended.
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        due = BERLIN_DUE
        limit = due - repo.SUMMARY_MAX_AGE
        save(conn, "g", date(2026, 9, 1), limit - timedelta(microseconds=1), items_after=7)
        assert repo.get_game_summary(conn, "g", due) is None
        exact = save(conn, "g", date(2026, 9, 2), limit, items_after=7)
        assert repo.get_game_summary(conn, "g", due).id == exact
        assert repo.get_game_summary(conn, "h", due) is None
        at_limit = Coverage(limit, 7)
        assert repo.get_game_summary(conn, "g", due, after=at_limit) is None  # strictly after
        before = Coverage(limit - timedelta(microseconds=1), 7)
        got = repo.get_game_summary(conn, "g", due, after=before)
        assert got is not None and got.id == exact
        # It has to start where the server's coverage ended: not before it (the server would
        # read news twice) and not after it (the server would never see the gap).
        assert repo.get_game_summary(conn, "g", due, after=Coverage(before.end, 6)) is None
        assert repo.get_game_summary(conn, "g", due, after=Coverage(before.end, 8)) is None


def test_a_summary_with_the_same_run_date_is_a_new_row_and_only_replace_id_replaces(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        url = "https://example.com/g/1"
        conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, published_at, "
            "collected_at) VALUES (?, 't', '', 's', 'press', NULL, 't')",
            (url,),
        )
        story = StoryToSave("g", "Zebra raid", "body", "reported", [url], None)
        good = save(conn, "g", DAY1, BERLIN_DUE, stories=[story])
        other = save(conn, "g", DAY1, BERLIN_DUE + timedelta(hours=9))  # same label, other cycle
        assert other != good
        assert [s.headline for s in repo.summary_stories(conn, good)] == ["Zebra raid"]
        # The explicit retry of a row still rewrites that row.
        again = save(conn, "g", DAY1, BERLIN_DUE + timedelta(hours=1), replace_id=good)
        assert again == good
        assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 2
        # A replace_id that names another game's row is ignored, not obeyed.
        stray = save(conn, "h", DAY1, BERLIN_DUE, replace_id=good)
        assert stray not in (good, other)


async def test_no_item_lands_in_two_consecutive_summaries_even_across_an_outage(world):
    # One server's consecutive summaries. (With two servers on different schedules the
    # summaries overlap on purpose: each server's own window is whole, so what one has read
    # another hasn't. tests/test_item_watermarks.py pins "no gaps, no repeats" per server.)
    await simulate(
        world,
        {BER: BERLIN},
        days=5,
        outage=(datetime(2026, 10, 2, 6, 0, tzinfo=UTC), datetime(2026, 10, 2, 15, 10, tzinfo=UTC)),
    )
    seen: set[str] = set()
    assert len(world.llm.calls) >= 4
    for _, user in world.llm.calls:
        batch = set(urls(user))
        assert batch and not (batch & seen)
        seen |= batch


async def test_an_item_collected_exactly_at_a_window_end_belongs_to_that_window_only(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    boundary = BERLIN_DUE
    world.item("palworld", "just before", boundary - timedelta(seconds=1))
    world.item("palworld", "on the dot", boundary)
    world.item("palworld", "just after", boundary + timedelta(seconds=1))
    world.clock.t = boundary
    got = await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    later = boundary + timedelta(hours=25)
    world.clock.t = later
    # The server's digest took that summary, so its coverage ends where the summary did.
    await ensure_summary(
        world.deps(),
        "palworld",
        later,
        run_date=date(2026, 10, 2),
        after=Coverage(boundary, got.items_upto),
    )
    first, second = (titles(u) for _, u in world.llm.calls)
    assert sorted(first) == ["just before", "on the dot"]
    assert second == ["just after"]


async def test_a_run_now_retry_keeps_its_window_start_and_the_next_summary_does_not_repeat(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "early", ago(3))
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    world.clock.t = BERLIN_DUE
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)

    world.item("palworld", "after the failure", BERLIN_DUE + timedelta(minutes=5))
    world.llm = FakeLLM()
    world.clock.t = BERLIN_DUE + timedelta(minutes=10)
    await retry_lookup(world.deps())("palworld", BERLIN_DUE)
    assert sorted(titles(world.llm.calls[0][1])) == ["after the failure", "early"]

    world.item("palworld", "next day", BERLIN_DUE + timedelta(hours=20))
    world.clock.t = BERLIN_DUE + timedelta(hours=25)
    with world.conn() as conn:
        retried = repo.get_game_summary(conn, "palworld", BERLIN_DUE)
    await ensure_summary(
        world.deps(),
        "palworld",
        BERLIN_DUE + timedelta(hours=25),
        run_date=date(2026, 10, 2),
        after=Coverage(retried.window_end, retried.items_upto),
    )
    assert titles(world.llm.calls[1][1]) == ["next day"]


async def test_the_window_floor_after_a_long_outage_and_the_first_ever_window(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    now = BERLIN_DUE
    # First ever: 24 hours back, strictly after.
    world.item("palworld", "25h old", now - timedelta(hours=25))
    world.item("palworld", "exactly 24h old", now - timedelta(hours=24))
    world.item("palworld", "23h old", now - timedelta(hours=23))
    world.clock.t = now
    await ensure_summary(world.deps(), "palworld", now, run_date=DAY1)
    assert titles(world.llm.calls[0][1]) == ["23h old"]

    # Five days of silence, then one summary: 48 hours back and no further.
    later = now + timedelta(days=5)
    world.item("palworld", "49h before", later - timedelta(hours=49))
    world.item("palworld", "exactly 48h before", later - timedelta(hours=48))
    world.item("palworld", "47h before", later - timedelta(hours=47))
    world.clock.t = later
    with world.conn() as conn:
        last = repo.get_game_summary(conn, "palworld", now)
    await ensure_summary(
        world.deps(),
        "palworld",
        later,
        run_date=date(2026, 10, 6),
        after=Coverage(last.window_end, last.items_upto),
    )
    assert titles(world.llm.calls[1][1]) == ["47h before"]


async def test_an_inline_summary_for_a_missed_digest_cannot_replace_a_good_prepared_one(world):
    # The bot is down overnight and comes back at 15:40 UTC on Oct 1. The Pacific digest's prepare
    # tick runs first (its row's run_date is the Pacific day, Oct 1). Then Berlin's catch-up digest,
    # due at 07:00 that day, finds nothing reusable (the row ends after 07:30) and makes an inline
    # one whose run_date is the UTC date of 07:00: Oct 1 again. Same (game, run_date): it
    # replaces.
    # Under the per-server reuse rule Berlin's catch-up digest now just reuses Pacific's fresh
    # row, so this scenario no longer reaches the collision; the repo-level test further down
    # (`..._same_run_date_is_a_new_row_...`) pins the storage half directly.
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "the real news", PACIFIC_DUE - timedelta(hours=3))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    assert await prepare_summaries(world.deps()) == ["palworld"]
    with world.conn() as conn:
        good = repo.get_game_summary(conn, "palworld", PACIFIC_DUE)
        assert good is not None and good.status == "ok"
        good_stories = len(repo.summary_stories(conn, good.id))
    assert good_stories == 1

    world.clock.t += timedelta(minutes=1)
    await summary_lookup(world.deps())("palworld", BERLIN_DUE)

    with world.conn() as conn:
        now_row = repo.get_game_summary(conn, "palworld", PACIFIC_DUE)
        assert now_row is not None
        assert len(repo.summary_stories(conn, now_row.id)) == good_stories
        detached = conn.execute(
            "SELECT COUNT(*) AS n FROM stories WHERE summary_id IS NULL"
        ).fetchone()["n"]
    assert detached == 0


# --- stale reuse: what a member actually sees ---


async def test_with_no_downtime_berlin_and_pacific_each_read_news_half_an_hour_old(world):
    # Was: Berlin's lead made the day's only summary and Pacific read it 9h30 on. Nine and a
    # half hours is over the six hour limit now, so Pacific's own lead makes one (two a day).
    lags = await simulate(world, {BER: BERLIN, PAC: LA}, days=6)
    assert lags[BER] == [0.5] * 6
    assert lags[PAC] == [0.5] * 6
    assert len(world.llm.calls) == 12


async def test_one_outage_no_longer_leaves_berlin_reading_stale_news(world):
    # Down from 06:00 to 15:10 UTC on Oct 2: Berlin's 07:00 digest is caught up at 15:11 with an
    # inline summary, ahead of the Pacific lead at 15:30, and that one serves Pacific too.
    lags = await simulate(
        world,
        {BER: BERLIN, PAC: LA},
        days=6,
        outage=(datetime(2026, 10, 2, 6, 0, tzinfo=UTC), datetime(2026, 10, 2, 15, 10, tzinfo=UTC)),
    )
    # The catch-up digest itself reads a summary made after its due time (a negative lag: the
    # news is fresher than the digest, which is what a late digest should get).
    assert min(h for h in lags[BER] if h is not None) < -8
    # And from Oct 3 on nothing is stale: it used to be 15 hours old, every day, for good.
    assert max(h for h in lags[BER][2:] if h is not None) <= 1
    assert max(h for per in lags.values() for h in per if h is not None) <= 6


async def test_the_order_recovers_after_an_outage_so_berlin_is_fresh_again(world):
    lags = await simulate(
        world,
        {BER: BERLIN, PAC: LA},
        days=8,
        outage=(datetime(2026, 10, 2, 6, 0, tzinfo=UTC), datetime(2026, 10, 2, 15, 10, tzinfo=UTC)),
    )
    assert all(h is not None and h <= 1.0 for h in lags[BER][-3:])


async def test_the_worst_a_member_sees_is_bounded_by_the_reuse_window(world):
    # Was "two servers 23 hours apart read news 23h35 old". The limit is six hours now; Berlin
    # (07:00 UTC) and Sao Paulo (12:00 UTC, no daylight saving) are five hours apart, close enough
    # to share one summary, and it's the oldest thing anyone reads: 5h30 before Sao Paulo's 09:00.
    lags = await simulate(world, {BER: BERLIN, PAC: "America/Sao_Paulo"}, days=6)
    worst = max(h for per in lags.values() for h in per if h is not None)
    assert worst <= repo.SUMMARY_MAX_AGE.total_seconds() / 3600
    assert worst == pytest.approx(5.5, abs=0.01)
    assert all(h is not None for per in lags.values() for h in per)
    assert len(world.llm.calls) == 6  # one summary a day for both


async def test_the_digest_on_the_spring_forward_day_gets_a_new_summary(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    day1, day2 = date(2027, 3, 13), date(2027, 3, 14)
    due1 = local_due_instant(day1, "09:00", LA)
    due2 = local_due_instant(day2, "09:00", LA)
    assert due2 - due1 == timedelta(hours=23)
    world.item("palworld", "saturday news", due1 - timedelta(hours=5))
    world.clock.t = due1 - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]

    world.item("palworld", "sunday news", due1 + timedelta(hours=5))
    world.clock.t = due2 - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert titles(world.llm.calls[-1][1]) == ["sunday news"]


async def test_moving_the_digest_time_two_hours_earlier_does_not_repeat_yesterdays_digest(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    due1 = local_due_instant(DAY1, "09:00", LA)
    world.item("palworld", "day one news", due1 - timedelta(hours=5))
    world.clock.t = due1 - timedelta(minutes=30)
    await prepare_summaries(world.deps())
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET digest_time = '07:00' WHERE guild_id = ?", (PAC,))
        conn.commit()
    due2 = local_due_instant(date(2026, 10, 2), "07:00", LA)
    world.item("palworld", "day two news", due1 + timedelta(hours=5))
    world.clock.t = due2 - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]


async def test_moving_the_digest_time_earlier_by_under_half_an_hour_is_fine(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    due1 = local_due_instant(DAY1, "09:00", LA)
    world.item("palworld", "day one news", due1 - timedelta(hours=5))
    world.clock.t = due1 - timedelta(minutes=30)
    await prepare_summaries(world.deps())
    with world.conn() as conn:
        conn.execute("UPDATE guilds SET digest_time = '08:45' WHERE guild_id = ?", (PAC,))
        conn.commit()
    due2 = local_due_instant(date(2026, 10, 2), "08:45", LA)
    world.item("palworld", "day two news", due1 + timedelta(hours=5))
    world.clock.t = due2 - timedelta(minutes=30)
    assert await prepare_summaries(world.deps()) == ["palworld"]


# --- 5. isolation ---


async def test_a_game_whose_model_call_hangs_does_not_stop_the_next_games_summary(world):
    world.guild(PAC, "comped", LA, ["borderlands4", "palworld"])
    world.item("borderlands4", "a thing", ago(2))
    world.item("palworld", "a thing", ago(2))
    world.llm = HangsFor("borderlands4")  # catalog order: it goes first
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    # The real deadline is five minutes (`SUMMARIZE_TIMEOUT_S`); a fraction of a second here.
    deps = world.deps(summarize_timeout_s=0.05)
    done = await asyncio.wait_for(prepare_summaries(deps), timeout=5)

    assert done == ["borderlands4", "palworld"]
    by_game = {r["game_key"]: r for r in world.summaries()}
    assert by_game["borderlands4"]["status"] == "fallback"
    assert "took too long" in by_game["borderlands4"]["note"]
    assert by_game["palworld"]["status"] == "ok"
    assert claims(world) == []
    assert world.alerts == ["newsbot: summary for Borderlands 4 fell back to headlines"]


async def test_the_deadline_is_well_inside_the_claims_lease():
    assert summaries.SUMMARIZE_TIMEOUT_S * 2 <= repo.SUMMARY_CLAIM_LEASE.total_seconds()


async def test_a_hung_game_does_not_block_a_digest_reading_an_existing_summary(world):
    world.guild(PAC, "comped", LA, ["borderlands4", "palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    with world.conn() as conn:
        save(
            conn,
            "palworld",
            DAY1,
            PACIFIC_DUE - timedelta(minutes=30),
            stories=[],
        )
        save(conn, "borderlands4", DAY1, PACIFIC_DUE - timedelta(minutes=30))
    world.llm = HangsFor("borderlands4")
    world.clock.t = PACIFIC_DUE
    publisher = FakePublisher()
    outcome = await asyncio.wait_for(
        run_guild_digest(digest_deps(world, world.deps(), publisher), PAC, kind=RunKind.SCHEDULED),
        timeout=5,
    )
    assert outcome.status in {"ok", "partial"} and world.llm.calls == []


async def test_a_crash_in_one_games_model_call_leaves_the_other_games_summaries_alone(world):
    world.guild(PAC, "comped", LA, ["borderlands4", "palworld"])
    world.item("borderlands4", "a thing", ago(2))
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"borderlands4": RuntimeError("a bug")})
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    assert await prepare_summaries(world.deps()) == ["borderlands4", "palworld"]
    assert {r["game_key"]: r["status"] for r in world.summaries()} == {
        "borderlands4": "fallback",
        "palworld": "ok",
    }
    assert claims(world) == []


async def test_a_save_that_blows_up_for_one_game_costs_the_others_nothing(world, monkeypatch):
    world.guild(PAC, "comped", LA, ["borderlands4", "palworld"])
    world.item("borderlands4", "a thing", ago(2))
    world.item("palworld", "a thing", ago(2))
    real = summaries._save_sync

    def picky(db_path, game_key, *a, **k):
        if game_key == "borderlands4":
            raise RuntimeError("disk said no")
        return real(db_path, game_key, *a, **k)

    monkeypatch.setattr(summaries, "_save_sync", picky)
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    assert await prepare_summaries(world.deps()) == ["palworld"]
    assert [r["game_key"] for r in world.summaries()] == ["palworld"]
    assert claims(world) == []

    # And the digest itself survives that lookup failing: one game missing, the rest posted.
    publisher = FakePublisher()
    world.clock.t = PACIFIC_DUE
    outcome = await run_guild_digest(
        digest_deps(world, world.deps(), publisher), PAC, kind=RunKind.SCHEDULED
    )
    assert "palworld" in [m.topic_key for m in publisher.posted]
    assert outcome.status in {"ok", "partial"}


async def test_free_servers_never_trigger_summaries_search_or_claims(world):
    world.guild(FREE, "free", BERLIN, ["palworld", "rust"])
    world.guild(FREE + 1, "free", LA, ["borderlands4"])
    for game in ("palworld", "rust", "borderlands4"):
        world.item(game, "a thing", ago(2))
    deps = world.deps(web=True)
    try:
        for minutes in (-40, -30, 0, 20):
            world.clock.t = BERLIN_DUE + timedelta(minutes=minutes)
            assert await prepare_summaries(deps) == []
        for gid in (FREE, FREE + 1):
            await run_guild_digest(
                digest_deps(world, deps, FakePublisher()), gid, kind=RunKind.SCHEDULED
            )
            await run_guild_digest(
                digest_deps(world, deps, FakePublisher()),
                gid,
                kind=RunKind.RUN_NOW,
                force=True,
            )
    finally:
        await world.http.aclose()
    assert world.llm.calls == [] and world.brave == [] and world.sleeps == []
    assert claims(world) == [] and world.summaries() == []
    assert world.rows("SELECT * FROM app_state") == []


# --- storage ---


async def test_summary_stories_survive_their_servers_deletion(world):
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    with world.conn() as conn:
        repo.delete_guild(conn, PAC)
    [story] = world.rows("SELECT * FROM stories")
    assert story["summary_id"] is not None and story["digest_id"] is None
    assert len(world.summaries()) == 1
    assert world.rows("SELECT COUNT(*) AS n FROM story_items")[0]["n"] == 1
    # And with nobody following, the prepare job has nothing to do and nothing to reap.
    world.clock.t = PACIFIC_DUE
    assert await prepare_summaries(world.deps()) == []


async def test_stories_fts_stays_consistent_when_a_summary_is_replaced_in_place(world):
    world.item("palworld", "a thing", ago(2))
    url = "https://example.com/palworld/a-thing"

    def story(headline):
        return StoryToSave("palworld", headline, f"{headline} body", "reported", [url], None)

    with world.conn() as conn:
        # Replacing in place is for the run-now retry only, and it has to name its row now: a
        # second save with the same run_date used to replace the first on its own.
        first = save(conn, "palworld", DAY1, ago(1), stories=[story("Zebra raid")])
        second = save(conn, "palworld", DAY1, ago(0), stories=[story("Yak raid")], replace_id=first)
        assert first == second
        rows = conn.execute("SELECT id, headline, summary_id FROM stories ORDER BY id").fetchall()
        assert [(r["headline"], r["summary_id"]) for r in rows] == [
            ("Zebra raid", None),
            ("Yak raid", second),
        ]
        for word, headline in (("Zebra", "Zebra raid"), ("Yak", "Yak raid")):
            hits = conn.execute(
                "SELECT rowid FROM stories_fts WHERE stories_fts MATCH ?", (word,)
            ).fetchall()
            assert [h["rowid"] for h in hits] == [
                r["id"] for r in rows if r["headline"] == headline
            ]
        conn.execute("INSERT INTO stories_fts (stories_fts) VALUES ('integrity-check')")
        assert [s.headline for s in repo.summary_stories(conn, second)] == ["Yak raid"]
