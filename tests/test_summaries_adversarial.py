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

What this found, each kept as a strict xfail so the day somebody fixes it the
suite tells them to delete the marker:

- the reuse window is "24 hours before the due time", and a day that is only
  23 hours long (spring forward) or a digest time moved more than half an hour
  earlier makes tomorrow's digest reuse yesterday's summary and post it twice;
- one late summary can flip which server "goes first", and the flip is sticky:
  from then on the Berlin server reads the Pacific server's summary from the
  day before, about 15 hours old, for good;
- an inline summary for a missed digest shares a `run_date` with the prepared
  row it has no business touching, and replaces it;
- a stale claim takeover makes a second call while the first is alive (only
  reachable if a call outlives the ten-minute lease, which the 60 second
  client timeout prevents in real life);
- a model call that hangs stalls every later game in the same prepare run;
- two overlapping prepare jobs each run the web search.

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
from newsbot.store.models import PriorStory, StoredItem, StoryToSave, Usage

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

    everything = filter_items(raw, w.cfg.topics, 10**6)
    capped = filter_items(raw, w.cfg.topics, w.cfg.digest.max_items_per_topic)
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
    topic = next(t for t in w.cfg.topics if t.key == "borderlands4")
    expected = build_prompt(
        topic,
        capped["borderlands4"],
        in_range,
        all_topics=w.cfg.topics,
        subject=w.cfg.digest.subject,
    )

    w.clock.t = end
    await ensure_summary(w.deps(), "borderlands4", PACIFIC_DUE, run_date=DAY1)

    assert w.llm.calls == [expected]
    if case == "over_the_cap":
        assert len(titles(w.llm.calls[0][1])) == w.cfg.digest.max_items_per_topic
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


@pytest.mark.xfail(
    strict=True,
    reason=("overlapping prepare jobs each run the web search: only the Claude call is claimed"),
)
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


async def test_prepare_racing_a_digests_inline_lookup_and_another_zone_is_still_one_call(world):
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(3))
    world.clock.t = BERLIN_DUE
    world.llm = FakeLLM(yields=10)
    deps = world.deps()
    lookup = summary_lookup(deps)

    results = await asyncio.gather(
        prepare_summaries(deps),
        lookup("palworld", BERLIN_DUE),
        lookup("palworld", PACIFIC_DUE),
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "a run-now waiting behind another run-now's retry gets the "
        "old fallback, not the retry's result"
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason=("a stale-claim takeover while the first caller is alive makes a second Claude call"),
)
async def test_a_takeover_of_a_claim_whose_owner_is_alive_but_slow_is_not_a_second_call(world):
    # Only reachable if one call outlives the ten-minute lease; the real client gives up at 60 s
    # (three attempts, about three minutes), so this is belt and braces. The claim has a lease and
    # no heartbeat, and `save_game_summary` never checks it still holds the claim.
    world.guild(PAC, "comped", LA, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.clock.t = BERLIN_DUE
    world.llm = Gated()
    deps = world.deps()
    slow = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await world.llm.started.wait()

    world.clock.t += repo.SUMMARY_CLAIM_LEASE + timedelta(seconds=1)
    rival = asyncio.create_task(ensure_summary(deps, "palworld", BERLIN_DUE, run_date=DAY1))
    await settle()
    world.llm.gate.set()
    await asyncio.gather(slow, rival)

    assert len(world.llm.calls) == 1


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
    real catch-up does). Returns `{guild_id: [hours between the summary's window end
    and the digest's due time, or None]}`, in day order.
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
            due = local_due_instant(start_day + timedelta(days=d), "09:00", tz)
            for k in range(-7, 8):
                events.append((due + timedelta(minutes=5 * k), 0, gid, due))
            events.append((due + timedelta(seconds=10), 1, gid, due))
    if outage:
        moved = []
        for at, kind, gid, due in events:
            if outage[0] <= at < outage[1]:
                if kind == 1:
                    moved.append((outage[1] + timedelta(minutes=1), 1, gid, due))
                continue
            moved.append((at, kind, gid, due))
        events = moved
    events.sort(key=lambda e: (e[0], e[1]))
    lags: dict[int, list[float | None]] = {gid: [] for gid in zones}
    deps = world.deps()
    lookup = summary_lookup(deps)
    for at, kind, gid, due in events:
        world.clock.t = at
        if kind == 0:
            await prepare_summaries(deps)
            continue
        await lookup(game, due)
        with world.conn() as conn:
            row = repo.get_game_summary(conn, game, due)
        lags[gid].append(None if row is None else (due - row.window_end).total_seconds() / 3600)
    return lags


async def test_a_failing_api_costs_one_attempt_set_per_game_per_day_not_per_server_or_tick(world):
    lags_days = 4
    world.llm = FakeLLM(fail={"*": LLMError("overloaded", input_tokens=5, output_tokens=0)})
    await simulate(world, {BER: BERLIN, PAC: LA}, days=lags_days)
    # Three attempts inside one summarize_topic, one set a day, shared by both servers,
    # however many prepare ticks and digests looked at it.
    assert len(world.llm.calls) == 3 * lags_days
    assert len(world.alerts) == lags_days
    assert {r["status"] for r in world.summaries()} == {"fallback"}


async def test_a_fatal_api_error_costs_one_call_per_game_per_day(world):
    world.llm = FakeLLM(fail={"*": LLMFatalError("400")})
    await simulate(world, {BER: BERLIN, PAC: LA}, days=3)
    assert len(world.llm.calls) == 3


async def test_a_disk_that_wont_save_costs_one_attempt_set_per_tick_inside_the_window_only(
    world, monkeypatch
):
    # The worst case I could find: the row can't be written, so "a row exists" never becomes
    # true and every five-minute tick tries again, for as long as the job considers the digest
    # upcoming (30 minutes before it to 30 minutes after: 13 ticks). Outside that, nothing.
    world.guild(BER, "comped", BERLIN, ["palworld"])
    world.item("palworld", "a thing", ago(2))
    world.llm = FakeLLM(fail={"*": LLMError("overloaded")})

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(summaries, "_save_sync", boom)
    deps = world.deps()
    ticks = 0
    for minutes in range(-180, 181, 5):
        world.clock.t = BERLIN_DUE + timedelta(minutes=minutes)
        before = len(world.llm.calls)
        assert await prepare_summaries(deps) == []
        if -30 <= minutes <= 30:
            ticks += 1
            assert len(world.llm.calls) - before == 3
        else:
            assert len(world.llm.calls) == before
    assert ticks == 13 and len(world.llm.calls) == 13 * 3
    assert claims(world) == []


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


def save(conn, game, run_date, window_end, *, status="ok", stories=(), usage=None):
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
    )


def test_a_row_saved_exactly_on_the_reuse_boundaries(tmp_path):
    db = str(tmp_path / "t.db")
    with closing(connect(db)) as conn:
        migrate(conn)
        due = BERLIN_DUE
        save(conn, "g", date(2026, 9, 1), due - timedelta(hours=24))
        save(conn, "g", date(2026, 9, 2), due + timedelta(minutes=30))
        # Window end of due minus 24h exactly is out; due plus 30 min exactly is in.
        got = repo.get_game_summary(conn, "g", due)
        assert got is not None and got.window_end == due + timedelta(minutes=30)
        assert repo.get_game_summary(conn, "h", due) is None
        save(conn, "h", date(2026, 9, 1), due - timedelta(hours=24))
        assert repo.get_game_summary(conn, "h", due) is None
        save(conn, "h", date(2026, 9, 2), due - timedelta(hours=24) + timedelta(microseconds=1))
        assert repo.get_game_summary(conn, "h", due) is not None
        save(conn, "i", date(2026, 9, 1), due + timedelta(minutes=30, microseconds=1))
        assert repo.get_game_summary(conn, "i", due) is None


async def test_no_item_lands_in_two_consecutive_summaries_even_across_an_outage(world):
    await simulate(
        world,
        {BER: BERLIN, PAC: LA},
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
    await ensure_summary(world.deps(), "palworld", BERLIN_DUE, run_date=DAY1)
    later = boundary + timedelta(hours=25)
    world.clock.t = later
    await ensure_summary(world.deps(), "palworld", later, run_date=date(2026, 10, 2))
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
    await ensure_summary(
        world.deps(), "palworld", BERLIN_DUE + timedelta(hours=25), run_date=date(2026, 10, 2)
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
    await ensure_summary(world.deps(), "palworld", later, run_date=date(2026, 10, 6))
    assert titles(world.llm.calls[1][1]) == ["47h before"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "an inline summary for a missed digest shares a run_date "
        "with the prepared row and replaces it"
    ),
)
async def test_an_inline_summary_for_a_missed_digest_cannot_replace_a_good_prepared_one(world):
    # The bot is down overnight and comes back at 15:40 UTC on Oct 1. The Pacific digest's prepare
    # tick runs first (its row's run_date is the Pacific day, Oct 1). Then Berlin's catch-up digest,
    # due at 07:00 that day, finds nothing reusable (the row ends after 07:30) and makes an inline
    # one whose run_date is the UTC date of 07:00: Oct 1 again. Same (game, run_date): it
    # replaces.
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


async def test_with_no_downtime_berlin_reads_fresh_news_and_pacific_a_summary_9h30_old(world):
    lags = await simulate(world, {BER: BERLIN, PAC: LA}, days=6)
    # Berlin's lead triggers the day's only summary; Pacific reuses it nine and a half hours on.
    assert lags[BER] == [0.5] * 6
    assert lags[PAC] == [9.5] * 6
    assert len(world.llm.calls) == 6


async def test_one_outage_flips_who_goes_first_and_berlin_reads_15h_old_news(world):
    # Down from 06:00 to 15:10 UTC on Oct 2: Berlin's 07:00 digest is caught up at 15:11 with an
    # inline summary, ahead of the Pacific lead at 15:30, and that one serves Pacific too.
    lags = await simulate(
        world,
        {BER: BERLIN, PAC: LA},
        days=6,
        outage=(datetime(2026, 10, 2, 6, 0, tzinfo=UTC), datetime(2026, 10, 2, 15, 10, tzinfo=UTC)),
    )
    # From Oct 3 on, Berlin's 07:00 digest reuses the summary made for Pacific's 16:00 the day
    # before: about 15 hours old at post time, every day, until something else disturbs the order.
    assert max(h for h in lags[BER][2:] if h is not None) > 15
    assert max(h for h in lags[PAC][1:] if h is not None) < 1  # and Pacific is now the fresh one


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the stale-reuse flip is sticky: after one late summary the "
        "Berlin server reads 15h old news for good"
    ),
)
async def test_the_order_recovers_after_an_outage_so_berlin_is_fresh_again(world):
    lags = await simulate(
        world,
        {BER: BERLIN, PAC: LA},
        days=8,
        outage=(datetime(2026, 10, 2, 6, 0, tzinfo=UTC), datetime(2026, 10, 2, 15, 10, tzinfo=UTC)),
    )
    assert all(h is not None and h <= 1.0 for h in lags[BER][-3:])


async def test_the_worst_a_member_sees_is_bounded_by_the_reuse_window(world):
    # Two servers 23 hours apart (Auckland then Honolulu) is about as far as the rule reaches.
    lags = await simulate(world, {BER: "Pacific/Auckland", PAC: "Pacific/Honolulu"}, days=6)
    worst = max(h for per in lags.values() for h in per if h is not None)
    assert worst < 24
    # Honolulu reads what Auckland's lead made, 23 hours and 35 minutes before its own 09:00.
    assert worst == pytest.approx(23.58, abs=0.05)
    assert all(h is not None for per in lags.values() for h in per)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "spring forward is a 23 hour day: its digest reuses yesterday's summary and posts it twice"
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "moving a server's digest time more than 30 minutes earlier "
        "makes tomorrow's digest a repeat"
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "prepare runs games one after another with no timeout: a hung call stalls every later game"
    ),
)
async def test_a_game_whose_model_call_hangs_does_not_stop_the_next_games_summary(world):
    world.guild(PAC, "comped", LA, ["borderlands4", "palworld"])
    world.item("borderlands4", "a thing", ago(2))
    world.item("palworld", "a thing", ago(2))
    world.llm = HangsFor("borderlands4")  # catalog order: it goes first
    world.clock.t = PACIFIC_DUE - timedelta(minutes=20)
    job = asyncio.create_task(prepare_summaries(world.deps()))
    await asyncio.wait({job}, timeout=0.5)
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)
    assert [r["game_key"] for r in world.summaries()] == ["palworld"]


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
        first = save(conn, "palworld", DAY1, ago(1), stories=[story("Zebra raid")])
        second = save(conn, "palworld", DAY1, ago(0), stories=[story("Yak raid")])
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
