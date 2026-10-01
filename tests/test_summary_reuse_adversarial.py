"""Adversarial tests for per-server summary reuse and the preview summary cache.

A comped server's summary may only be reused if it starts exactly where that server's own
coverage ended. That rule keeps two servers on different schedules from eating each other's
news, and it has a price: two servers whose marks differ by one stray item stop sharing, and
every day they don't share is a model call somebody pays for. So the questions are about money
and about gaps, in that order:

1. Over a week, does a crowd on one schedule cost one call a day? Do servers that were pushed
   out of step (a run-now, a failed model call) fall back into step, and how fast?
2. Does the friend's model input stay byte for byte what v2.2 would have sent on day two, when
   the items are picked by id and not by time?
3. The preview cache: keyed by game and coverage, kept half an hour, never written to the
   database. What does a member see when items arrive inside that half hour? (Pinned.)

The rig is the comped-windows file's: a fake model that writes a story per item, a real
database, and a clock that moves when told.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import timedelta

import pytest
from test_comped_per_server_windows import A2, FIRST_DAY, LA, A, B, Servers, store_hourly
from test_summaries import PACIFIC_DUE, FakePublisher, World, _noop_notify
from test_summaries_adversarial import EveryItem

from newsbot.collectors.base import RawItem
from newsbot.guilds.importer import ensure_imported
from newsbot.guilds.schedule import local_due_instant
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.guild_digest import (
    GuildDigestDeps,
    preview_guild_digest,
    run_due_guilds,
    run_guild_digest,
)
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import (
    _PRIOR_HEADLINE_WINDOW,
    PREVIEW_SUMMARY_TTL,
    dry_run_lookup,
    prepare_summaries,
    summary_lookup,
)
from newsbot.pipeline.summarize import LLMFatalError
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import Coverage, StoredItem

C = 14


class MaybeDown(EveryItem):
    """The every-item model, with a switch for a bad day (a fatal error, so no retries)."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def emit_stories(self, system, user):
        if self.down:
            self.calls.append((system, user))
            raise LLMFatalError("the model is having a day")
        return await super().emit_stories(system, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.llm = MaybeDown()
    return w


def titles_in(messages) -> set[str]:
    return {t for m in messages for t in re.findall(r"news-\d+", json.dumps(m.embed.to_dict()))}


# --- 1. what a crowd costs ---


async def test_three_servers_on_one_schedule_cost_one_call_a_day_for_a_week(world):
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 12)
    servers = Servers(world, {A: (LA, "09:00"), A2: (LA, "09:00"), B: (LA, "09:00")})
    for d in range(7):
        await servers.run_day(FIRST_DAY + timedelta(days=d))
    assert len(world.llm.calls) == 7
    assert servers.read[A] == servers.read[A2] == servers.read[B]
    assert all(servers.read[A])


async def test_servers_due_in_the_same_minute_share_through_one_digest_tick(world):
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 6)
    servers = Servers(world, {A: (LA, "09:00"), A2: (LA, "09:00")})
    for d in range(3):
        due = servers.due(A, FIRST_DAY + timedelta(days=d))
        world.clock.t = due - timedelta(minutes=30)
        await prepare_summaries(servers.deps)
        world.clock.t = due + timedelta(seconds=5)
        outcomes = await run_due_guilds(servers.digest_deps)
        assert [o.status for o in outcomes] == ["partial", "partial"]  # the web-search note
    assert len(world.llm.calls) == 3
    a, a2 = servers.pubs[A].posted, servers.pubs[A2].posted
    assert titles_in(a) == titles_in(a2) and a


async def test_a_run_now_pushes_one_server_out_of_step_for_one_cycle_not_forever(world):
    """B runs its day early. The two stop sharing (their marks differ), then share again."""
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 10)
    servers = Servers(world, {A: (LA, "09:00"), B: (LA, "09:00")})
    await servers.run_day(FIRST_DAY)
    calls_by_day = []
    for d in range(1, 6):
        day = FIRST_DAY + timedelta(days=d)
        if d == 1:  # an admin runs B's day at 05:00, from a clean start
            world.clock.t = servers.due(B, day) - timedelta(hours=4)
            await run_guild_digest(servers.digest_deps, B, kind=RunKind.RUN_NOW)
        before = len(world.llm.calls)
        await servers.run_day(day)
        calls_by_day.append(len(world.llm.calls) - before)
    # Day 1: B's run-now made its own summary, A's 08:30 one is A's. Day 2: B's mark and A's
    # still differ, so two calls. From day 3 on they are back in step.
    assert calls_by_day[-3:] == [1, 1, 1], calls_by_day
    assert calls_by_day[0] <= 2 and calls_by_day[1] <= 2


async def test_after_a_model_outage_the_servers_share_the_next_cycles_summary(world):
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 8)
    servers = Servers(world, {A: (LA, "09:00"), A2: (LA, "09:00")})
    world.llm.down = True
    await servers.run_day(FIRST_DAY)
    assert servers.read[A] == [set()] and servers.read[A2] == [set()]  # headlines, no stories
    first_day_headlines = titles_in(servers.pubs[A].posted)
    assert first_day_headlines and first_day_headlines == titles_in(servers.pubs[A2].posted)

    world.llm.down = False
    before = len(world.llm.calls)
    await servers.run_day(FIRST_DAY + timedelta(days=1))
    assert len(world.llm.calls) - before == 1  # one call, shared again at once
    assert servers.read[A][1] == servers.read[A2][1] and servers.read[A][1]
    # And the story day starts where the headline day ended: nothing is told twice.
    assert servers.read[A][1].isdisjoint(first_day_headlines)


async def test_a_summary_the_model_could_not_make_is_not_remade_for_each_server_that_day(world):
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 4)
    servers = Servers(world, {A: (LA, "09:00"), A2: (LA, "09:00"), B: (LA, "09:00")})
    world.llm.down = True
    await servers.run_day(FIRST_DAY)
    assert len(world.llm.calls) == 1  # one fallback row, served to all three


# --- 2. the friend's prompt, on the second day ---


async def test_the_friends_model_input_on_day_two_is_still_byte_identical_to_v22s(
    tmp_path, monkeypatch
):
    """Day two's items are picked by id past day one's mark; the prompt must be what v2.2 built
    from the same items, so an owner-reviewed preview of v2.2 stays an honest preview."""
    w = World(tmp_path, monkeypatch, "config_v2_prodlike.yaml", now=PACIFIC_DUE)
    friend = ensure_imported(w.db_path, w.cfg, lambda: PACIFIC_DUE - timedelta(days=3)).guild_id

    def raw(n, base, hours_before):
        return RawItem(
            url=f"https://example.com/{n}",
            title=f"Borderlands 4 story {n}",
            excerpt=("Excerpt text. " * 30) if n % 2 else f"Excerpt {n}",
            source_name="Feed",
            trust=("official", "press", "community")[n % 3],
            published_at=base - timedelta(hours=hours_before),
            topics=("borderlands4",),
        )

    def store(items, stored_at):
        grouped = filter_items(items, w.v2.topics, w.v2.digest.max_items_per_topic)
        with w.conn() as conn:
            for ti in grouped.get("borderlands4", []):
                repo.store_items(
                    conn,
                    [
                        StoredItem(
                            ti.item.url,
                            ti.item.title,
                            ti.item.excerpt,
                            ti.item.source_name,
                            ti.item.trust,
                            ti.item.published_at,
                            {"borderlands4": ti.uncertain},
                        )
                    ],
                    now=lambda: stored_at,
                )
        return grouped

    day1 = [raw(n, PACIFIC_DUE, 2 + n) for n in range(1, 5)]
    store(day1, PACIFIC_DUE - timedelta(hours=1))
    digest_deps = GuildDigestDeps(
        cfg=w.cfg,
        db_path=w.db_path,
        now=w.clock,
        publisher_for=lambda *a: FakePublisher(),
        notify_guild=_noop_notify,
        summary_for=summary_lookup(w.deps()),
        sleep=w.sleep,
        pace_s=0,
    )
    w.clock.t = PACIFIC_DUE - timedelta(minutes=30)
    await prepare_summaries(w.deps())
    w.clock.t = PACIFIC_DUE + timedelta(seconds=30)
    await run_guild_digest(digest_deps, friend, kind=RunKind.SCHEDULED)

    next_due = PACIFIC_DUE + timedelta(days=1)
    day2 = [raw(n, next_due, 2 + n) for n in range(10, 14)]
    grouped = store(day2, next_due - timedelta(hours=1))
    w.clock.t = next_due - timedelta(minutes=30)
    with closing(connect(w.db_path)) as conn:
        prior = repo.recent_headlines(conn, "borderlands4", w.clock.t - _PRIOR_HEADLINE_WINDOW)
    topic = next(t for t in w.v2.topics if t.key == "borderlands4")
    expected = build_prompt(
        topic,
        grouped["borderlands4"],
        prior,
        all_topics=w.v2.topics,
        subject=w.v2.digest.subject,
    )
    before = len(w.llm.calls)
    await prepare_summaries(w.deps())
    assert w.llm.calls[before:][0] == expected
    assert 'Borderlands 4 story 1"' not in expected[1]  # day one's items were not fed in again


# --- 3. the preview cache ---


def a_preview_world(world, *, tier="comped"):
    """A comped server with a stored coverage mark, a clock at noon, and a preview lookup."""
    due = local_due_instant(FIRST_DAY, "09:00", LA)
    store_hourly(world, due - timedelta(days=2), 24 * 4)
    servers = Servers(world, {A: (LA, "09:00")})
    return due, servers


def table_counts(world) -> dict[str, int]:
    with world.conn() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # noqa: S608
            for name in ("game_summaries", "stories", "digests", "app_state", "items")
        }


async def test_a_preview_never_writes_to_the_database_however_many_times_it_runs(world):
    due, servers = a_preview_world(world)
    await servers.run_day(FIRST_DAY)  # a coverage mark and a stored summary to leave alone
    world.clock.t = due + timedelta(days=1) - timedelta(hours=6)
    before = table_counts(world)
    snapshot = [tuple(r) for r in world.rows("SELECT * FROM digests ORDER BY id")]
    deps = servers.digest_deps
    deps.preview_summary_for = dry_run_lookup(world.deps())
    for _ in range(3):
        assert await preview_guild_digest(deps, A) is not None
    assert table_counts(world) == before
    assert [tuple(r) for r in world.rows("SELECT * FROM digests ORDER BY id")] == snapshot


async def test_a_second_preview_inside_the_ttl_costs_no_model_call_and_reads_the_same(world):
    due, servers = a_preview_world(world)
    await servers.run_day(FIRST_DAY)
    world.clock.t = due + timedelta(days=1) - timedelta(hours=6)
    servers.digest_deps.preview_summary_for = dry_run_lookup(world.deps())
    before = len(world.llm.calls)
    first = await preview_guild_digest(servers.digest_deps, A)
    assert len(world.llm.calls) == before + 1
    world.clock.t += PREVIEW_SUMMARY_TTL - timedelta(seconds=1)
    second = await preview_guild_digest(servers.digest_deps, A)
    assert len(world.llm.calls) == before + 1
    assert [m.embed.to_dict() for m in first.rendered.messages] == [
        m.embed.to_dict() for m in second.rendered.messages
    ]


async def test_a_preview_at_exactly_the_ttl_makes_a_fresh_summary(world):
    due, servers = a_preview_world(world)
    await servers.run_day(FIRST_DAY)
    world.clock.t = due + timedelta(days=1) - timedelta(hours=6)
    servers.digest_deps.preview_summary_for = dry_run_lookup(world.deps())
    before = len(world.llm.calls)
    await preview_guild_digest(servers.digest_deps, A)
    world.clock.t += PREVIEW_SUMMARY_TTL
    await preview_guild_digest(servers.digest_deps, A)
    assert len(world.llm.calls) == before + 2


async def test_a_preview_summary_is_stale_for_items_that_arrive_inside_the_ttl(world):
    """Pinned: the cache key is game and coverage, not what's stored, so a preview a few minutes
    later doesn't show an item that arrived in between (the owner may want the cache keyed on the
    newest stored id too: it would still hit when nothing arrived, and miss when something did)."""
    due, servers = a_preview_world(world)
    await servers.run_day(FIRST_DAY)
    world.clock.t = due + timedelta(days=1) - timedelta(hours=6)
    servers.digest_deps.preview_summary_for = dry_run_lookup(world.deps())
    first = await preview_guild_digest(servers.digest_deps, A)
    with world.conn() as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    "https://example.com/brand-new",
                    "news-brand-new",
                    "",
                    "Feed",
                    "press",
                    None,
                    {"palworld": False},
                )
            ],
            now=lambda: world.clock.t - timedelta(minutes=1),
        )
    world.clock.t += timedelta(minutes=5)
    second = await preview_guild_digest(servers.digest_deps, A)
    assert "brand-new" not in json.dumps([m.embed.to_dict() for m in first.rendered.messages])
    assert "brand-new" not in json.dumps([m.embed.to_dict() for m in second.rendered.messages])


async def test_a_new_digest_changes_the_coverage_and_so_misses_the_preview_cache(world):
    due, servers = a_preview_world(world)
    await servers.run_day(FIRST_DAY)
    world.clock.t = due + timedelta(days=1) - timedelta(hours=6)
    lookup = dry_run_lookup(world.deps())
    before = len(world.llm.calls)
    stale = Coverage(due, 1)
    await lookup("palworld", due + timedelta(days=1), stale)
    await lookup("palworld", due + timedelta(days=1), stale)
    assert len(world.llm.calls) == before + 1
    await lookup("palworld", due + timedelta(days=1), Coverage(due, 2))
    assert len(world.llm.calls) == before + 2


async def test_a_failed_preview_summary_is_not_cached_and_the_next_preview_tries_again(world):
    due, servers = a_preview_world(world)
    await servers.run_day(FIRST_DAY)
    world.clock.t = due + timedelta(days=1) - timedelta(hours=6)
    lookup = dry_run_lookup(world.deps())
    world.llm.down = True
    first = await lookup("palworld", due + timedelta(days=1), Coverage(due, 1))
    assert first.status == "fallback"
    world.llm.down = False
    second = await lookup("palworld", due + timedelta(days=1), Coverage(due, 1))
    assert second.status == "ok"


async def test_the_preview_cache_forgets_old_entries_so_it_does_not_grow_with_every_coverage(world):
    due, _ = a_preview_world(world)
    world.clock.t = due - timedelta(hours=6)
    lookup = dry_run_lookup(world.deps())
    for n in range(1, 40):
        await lookup("palworld", due, Coverage(due, n))
        world.clock.t += PREVIEW_SUMMARY_TTL  # every entry is stale by the next lookup
    cells = [c for c in lookup.__closure__ if isinstance(c.cell_contents, dict)]
    assert [len(c.cell_contents) for c in cells] == [1]


async def test_a_free_servers_preview_never_asks_the_model(world):
    due, servers = a_preview_world(world)
    world.guild(C, "free", LA, ["palworld"])
    servers.pubs[C] = FakePublisher()
    world.clock.t = due - timedelta(hours=6)
    servers.digest_deps.preview_summary_for = dry_run_lookup(world.deps())
    before = len(world.llm.calls)
    preview = await preview_guild_digest(servers.digest_deps, C)
    assert preview is not None and len(world.llm.calls) == before
