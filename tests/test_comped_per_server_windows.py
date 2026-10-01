"""Comped summaries cover each server's own window, not one window per game (qa review, fix 2).

Summaries used to chain per game: each new one started where the game's newest one ended, whoever
it was made for. With one comped server that's the same thing. With two on different schedules
it isn't: a 09:00 server's summary consumes the news, a 21:00 server's summary then starts
at 09:00's end (it loses the morning), and the next 09:00 summary starts at 21:00's end (so that
server loses the day). Now a server's summary starts exactly where that server's own coverage
left off. A stored summary is shared only by servers whose coverage ended in the same place, which
is the friend alone, or comped servers on one schedule.

The rig is the summaries file's: a counting fake model (here one that writes a story per item, so
the posted embeds say which items they covered), a real database, a clock that only moves when
told, and a publisher that remembers what it was asked to post.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import pytest
from test_summaries import FakePublisher, World, _noop_notify
from test_summaries_adversarial import EveryItem

from newsbot.guilds.schedule import local_due_instant
from newsbot.pipeline.guild_digest import GuildDigestDeps, run_guild_digest
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import prepare_summaries, summary_lookup
from newsbot.store import repo
from newsbot.store.models import StoredItem

LA = "America/Los_Angeles"
A, B, A2 = 11, 12, 13
FIRST_DAY = date(2026, 10, 5)  # PDT all week


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.llm = EveryItem()
    return w


def store_hourly(world, start: datetime, hours: int) -> list[tuple[str, datetime]]:
    items = []
    with world.conn() as conn:
        for n in range(hours):
            at = start + timedelta(hours=n)
            title = f"news-{n}"
            repo.store_items(
                conn,
                [
                    StoredItem(
                        f"https://example.com/p/{n}",
                        title,
                        "",
                        "Feed",
                        "press",
                        at,
                        {"palworld": False},
                    )
                ],
                now=lambda at=at: at,
            )
            items.append((title, at))
    return items


class Servers:
    """Prepare ticks and digests for some comped servers, as the bot's jobs would run them."""

    def __init__(self, world, zones: dict[int, tuple[str, str]]) -> None:
        self.world = world
        self.zones = zones
        self.pubs = {gid: FakePublisher() for gid in zones}
        self.read: dict[int, list[set[str]]] = {gid: [] for gid in zones}
        for gid, (tz, at) in zones.items():
            world.guild(gid, "comped", tz, ["palworld"], time=at)
        self.deps = world.deps()
        self.digest_deps = GuildDigestDeps(
            cfg=world.cfg,
            db_path=world.db_path,
            now=world.clock,
            publisher_for=lambda guild, scope, already, on_posted: self.pubs[guild.guild_id],
            notify_guild=_noop_notify,
            summary_for=summary_lookup(self.deps),
            sleep=world.sleep,
            pace_s=0,
        )

    def due(self, gid: int, day: date) -> datetime:
        tz, at = self.zones[gid]
        return local_due_instant(day, at, tz)

    async def run_day(self, day: date) -> None:
        """Each server: a prepare tick at its lead, then its digest at its due time."""
        events = []
        for gid in self.zones:
            due = self.due(gid, day)
            events.append((due - timedelta(minutes=30), 0, gid))
            events.append((due + timedelta(seconds=10), 1, gid))
        for at, kind, gid in sorted(events):
            self.world.clock.t = at
            if kind == 0:
                await prepare_summaries(self.deps)
                continue
            before = len(self.pubs[gid].posted)
            await run_guild_digest(self.digest_deps, gid, kind=RunKind.SCHEDULED)
            posted = self.pubs[gid].posted[before:]
            self.read[gid].append(
                {
                    t
                    for m in posted
                    for t in re.findall(r"story about (news-\d+)", json.dumps(m.embed.to_dict()))
                }
            )


async def test_two_comped_servers_twelve_hours_apart_each_read_their_whole_window(world):
    # The qa reviewer's scenario: 09:00 and 21:00 in Los Angeles, both following Palworld.
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    items = store_hourly(world, epoch, 24 * 7)
    servers = Servers(world, {A: (LA, "09:00"), B: (LA, "21:00")})
    days = [FIRST_DAY + timedelta(days=d) for d in range(3)]
    for day in days:
        await servers.run_day(day)

    for gid in (A, B):
        digests = servers.read[gid]
        assert len(digests) == 3 and all(digests)
        # No repeats: no item is read in two of this server's digests.
        union = set().union(*digests)
        assert sum(len(d) for d in digests) == len(union)
        # No gaps: everything stamped between the first summary's floor and the last summary's
        # end is in there. A summary is made 30 minutes before its digest and reads what had
        # been stored by then.
        first_end = servers.due(gid, days[0]) - timedelta(minutes=30)
        last_end = servers.due(gid, days[-1]) - timedelta(minutes=30)
        expected = {t for t, at in items if first_end - timedelta(hours=24) < at <= last_end}
        assert union == expected, (
            f"server {gid}: missing {sorted(expected - union)}, extra {sorted(union - expected)}"
        )
    # Different windows, so the two servers don't share a summary: two calls a day.
    assert len(world.llm.calls) == 2 * len(days)
    # (And each server's later digests are about a day of news, not half of one.)
    assert all(len(d) in (23, 24, 25) for gid in (A, B) for d in servers.read[gid][1:])


async def test_comped_servers_on_one_schedule_share_one_summary_a_day(world):
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 7)
    servers = Servers(world, {A: (LA, "09:00"), A2: (LA, "09:05")})
    for d in range(3):
        await servers.run_day(FIRST_DAY + timedelta(days=d))
    assert len(world.llm.calls) == 3  # one a day for both
    assert servers.read[A] == servers.read[A2]
    assert all(servers.read[A])


async def test_a_servers_first_digest_takes_a_fresh_summary_instead_of_making_another(world):
    """A first digest has nothing to repeat or miss, so any fresh summary will do (plan 3.6)."""
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    store_hourly(world, epoch, 24 * 7)
    servers = Servers(world, {A: (LA, "09:00")})
    await servers.run_day(FIRST_DAY)
    await servers.run_day(FIRST_DAY + timedelta(days=1))
    calls_before = len(world.llm.calls)

    world.guild(B, "comped", LA, ["palworld"], time="09:00")
    servers.zones[B] = (LA, "09:00")
    servers.pubs[B] = FakePublisher()
    servers.read[B] = []
    await servers.run_day(FIRST_DAY + timedelta(days=2))
    assert len(world.llm.calls) == calls_before + 1  # A's summary, made once, read by both
    assert servers.read[A][-1] == servers.read[B][-1] and servers.read[B][-1]


async def test_a_server_whose_coverage_differs_gets_a_summary_of_its_own(world):
    """Same game, same morning, but B left off elsewhere: sharing would leave a gap or repeat."""
    epoch = local_due_instant(FIRST_DAY, "09:00", LA) - timedelta(days=2)
    items = store_hourly(world, epoch, 24 * 7)
    servers = Servers(world, {A: (LA, "09:00"), B: (LA, "21:00")})
    for d in range(2):
        await servers.run_day(FIRST_DAY + timedelta(days=d))
    # On day 3 B moves its digest to 09:05. Its history (it last read up to 20:30) is not A's
    # (up to 08:30), so A's fresh summary would hand B news it already has: B gets its own.
    with world.conn() as conn:
        repo.update_guild_settings(conn, B, digest_time="09:05")
    servers.zones[B] = (LA, "09:05")
    calls_before = len(world.llm.calls)
    await servers.run_day(FIRST_DAY + timedelta(days=2))
    assert len(world.llm.calls) == calls_before + 2
    end_a = servers.due(A, FIRST_DAY + timedelta(days=2)) - timedelta(minutes=30)
    end_b = servers.due(B, FIRST_DAY + timedelta(days=2)) - timedelta(minutes=30)
    day2_end_b = local_due_instant(FIRST_DAY + timedelta(days=1), "21:00", LA) - timedelta(
        minutes=30
    )
    assert servers.read[B][-1] == {t for t, at in items if day2_end_b < at <= end_b}
    # (B had already read the 21:00 day's news; a day-2 digest at 21:00 would have ended at 20:30.)
    assert end_a - timedelta(hours=24) < end_b


# --- the in-flight race, for summaries ---


async def test_an_item_a_slow_pass_commits_after_a_summary_read_is_in_the_next_summary_only(world):
    due = local_due_instant(FIRST_DAY, "09:00", LA)
    store_hourly(world, due - timedelta(days=1, hours=2), 24)  # news-0 .. news-23, all before 08:00
    servers = Servers(world, {A: (LA, "09:00")})
    # The summary reads at 08:30:00. A pass started at 08:29:30 and commits a minute later.
    world.clock.t = due - timedelta(minutes=30)
    await prepare_summaries(servers.deps)
    with world.conn() as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    "https://example.com/p/slow",
                    "slow-pass",
                    "",
                    "Feed",
                    "press",
                    None,
                    {"palworld": False},
                )
            ],
            now=lambda: due - timedelta(minutes=30, seconds=30),
        )
    world.clock.t = due + timedelta(seconds=10)
    await run_guild_digest(servers.digest_deps, A, kind=RunKind.SCHEDULED)

    world.clock.t = due + timedelta(days=1) - timedelta(minutes=30)
    await prepare_summaries(servers.deps)
    prompts = [user for _, user in world.llm.calls]
    assert len(prompts) == 2
    assert [p.count('"title": "slow-pass"') for p in prompts] == [0, 1]  # never in neither, once


# --- rollback and roll-forward ---


async def test_after_a_rollback_the_first_summary_does_not_refeed_what_v22_posted(world):
    """The newest summary is stale, but the server's last digest (v2.2's) is later. Start there."""
    day = FIRST_DAY
    due = local_due_instant(day, "09:00", LA)
    epoch = due - timedelta(days=2)
    store_hourly(world, epoch, 24 * 5)
    servers = Servers(world, {A: (LA, "09:00")})
    await servers.run_day(day)  # v3's day: a summary at 08:30, a digest at 09:00
    assert world.llm.calls

    # Rolled back to v2.2 for day 2: it posted at 09:00 with its own items, and was rolled
    # forward again. The import's adoption left that digest with only a window end.
    day2_due = local_due_instant(day + timedelta(days=1), "09:00", LA)
    v22_posted_at = day2_due + timedelta(seconds=7)
    with world.conn() as conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, window_start, "
            "window_end, created_at, updated_at) VALUES (?, ?, 'ok', '[1]', ?, ?, 't', 't')",
            (
                A,
                (day + timedelta(days=1)).isoformat(),
                (v22_posted_at - timedelta(hours=24)).isoformat(),
                v22_posted_at.isoformat(),
            ),
        )
        conn.commit()
    world.llm.calls.clear()

    # Day 3: the first v3 summary after the roll-forward must start after v2.2's digest, not
    # at the end of the (day 1) summary, or it hands the server day 2's news a second time.
    day3_due = local_due_instant(day + timedelta(days=2), "09:00", LA)
    world.clock.t = day3_due - timedelta(minutes=30)
    await prepare_summaries(world.deps())
    [(_, user)] = world.llm.calls
    shown = {int(n) for n in re.findall(r'"title": "news-(\d+)"', user)}
    start_hour = (v22_posted_at - epoch) / timedelta(hours=1)
    assert shown and min(shown) > start_hour - 1  # nothing v2.2 already posted
    assert max(shown) == int((day3_due - timedelta(minutes=30) - epoch) / timedelta(hours=1))
