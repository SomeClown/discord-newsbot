"""Breaking the industry pick's per-cycle prior stories (`repo.chain_headlines`).

The rule under attack: the stories handed to an industry pick as "already covered" are the
ones the asking cycle's servers were shown, found by walking the picks that ended at their
coverage mark back through `items_after`. The first half of this file drives the real
summaries job and the real digest build with a small model that does what the industry
prompt asks of the real one: it drops any story whose headline is among the prior headlines
it was handed, and marks a story as an update of a prior one it recognizes. A reference
model written separately (it knows nothing about picks, chains or marks; only "what has this
server been shown, and what arrived since its last digest") says what each server should
have been shown, digest by digest, and the two have to agree. The second half goes at the
repo helper directly: the three day edge, loops, purged stories, a lot of chains, a long
chain, and the game priors that must not move.

The two tests that found bugs (a fallback day and a free run-now leave a mark no pick ended at)
pass since `chain_headlines` learned to start from the newest pick before such a mark.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_summaries import FakePublisher, World, _noop_notify

from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline import summaries
from newsbot.pipeline.guild_digest import GuildDigestDeps, run_guild_digest
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import prepare_summaries, summary_lookup
from newsbot.pipeline.summarize import LLMError, LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.models import Coverage

LA = "America/Los_Angeles"
# 00:00 on 2026-10-01 in Los Angeles (summer time until Nov 1, so no clock change in these runs).
MIDNIGHT = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
LEAD = timedelta(minutes=20)  # the prepare job runs this long before a digest is due
WINDOW = timedelta(days=3)  # the prior headline window
CAP = 5  # the industry cap on stories per pick and per digest


def pt(day: int, hour: int, minute: int = 0) -> datetime:
    return MIDNIGHT + timedelta(days=day, hours=hour, minutes=minute)


class TitleModel:
    """One story per item, headline = the item's title.

    A story is dropped (relevant false) when its headline is already a prior headline. A title
    of the form "<prior>: update" is kept and marked as an update of that prior headline,
    when the prior list has it. `fail` makes every call raise, which the real code turns into
    a fallback after its retries.
    """

    def __init__(self) -> None:
        self.priors: list[list[str]] = []
        self.fail = False

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        prior: list[str] = []
        if "<prior_stories>" in user:
            prior = json.loads(
                user.split("<prior_stories>\n", 1)[1].split("\n</prior_stories>", 1)[0]
            )
        self.priors.append(prior)
        if self.fail:
            raise LLMError("model down")
        items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        stories = []
        for item in items:
            title = item["title"]
            base = title.removesuffix(": update")
            stories.append(
                StoryOut(
                    headline=title,
                    summary="What happened.",
                    label="reported",
                    item_urls=[item["url"]],
                    relevant=title not in prior,
                    update_of_headline=base if base != title and base in prior else None,
                )
            )
        return LLMResult(StoriesOut(stories=stories), 10, 5)


class Reference:
    """What each server should have been shown, from first principles.

    Knows items (id, title, time) and, per server, its mark (the newest item id it has been
    given) and the titles it was shown, with the time of the pick that showed them. A pick for
    a server at time P covers the items past its mark up to the newest collected by P, within
    the floor (24 h for a first digest, 48 h after), and drops any title the server was shown
    in the 3 days before P. Same-cycle servers have the same mark, so they get the same list.
    """

    def __init__(self) -> None:
        self.items: list[tuple[int, str, datetime]] = []
        self.mark: dict[int, int | None] = {}
        self.shown: dict[int, list[tuple[str, datetime]]] = {}

    def join(self, gid: int) -> None:
        self.mark[gid] = None
        self.shown[gid] = []

    def digest(self, gid: int, pick_at: datetime) -> list[str]:
        mark = self.mark[gid]
        upto = max([i for i, _, at in self.items if at <= pick_at] + [mark or 0])
        floor = pick_at - (2 * DAY if mark is not None else DAY)
        window = [(i, t) for i, t, at in self.items if (mark or 0) < i <= upto and at > floor]
        seen = {t for t, at in self.shown[gid] if at >= pick_at - WINDOW}
        stories = [t for _, t in window if t not in seen]
        assert len(stories) <= CAP, "the schedule overflows the cap; the reference can't order it"
        self.shown[gid] += [(t, pick_at) for t in stories]
        self.mark[gid] = upto
        return sorted(stories)


class Sim:
    """The real prepare job and the real digest, a clock, and the reference beside them."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.world = World(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")
        self.model = TitleModel()
        self.world.llm = self.model
        self.ref = Reference()
        self.tiers: dict[int, str] = {}
        self.titles: set[str] = set()
        self.shown: dict[tuple[int, int], list[str]] = {}
        self.expected: dict[tuple[int, int], list[str]] = {}

    def join(self, gid: int, tier: str, time: str) -> None:
        self.world.guild(gid, tier, LA, [INDUSTRY_KEY], time=time)
        self.tiers[gid] = tier
        self.ref.join(gid)

    def drop(self, title: str, at: datetime, outlet: str = "a") -> None:
        slug = title.replace(" ", "-").replace(":", "")
        url = f"https://example.com/industry/{outlet}/{slug}"
        self.world.item(INDUSTRY_KEY, title, at, url=url)
        with self.world.conn() as conn:
            item_id = conn.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()["id"]
        self.ref.items.append((item_id, title, at))
        self.titles.add(title)

    async def prepare(self, at: datetime) -> None:
        """Run the prepare job until it has nothing left (it makes one pick per game per pass)."""
        self.world.clock.t = at
        for _ in range(8):
            if not await prepare_summaries(self.world.deps()):
                return
        raise AssertionError("the prepare job never ran out of work")

    async def post(self, gid: int, due: datetime, kind=RunKind.SCHEDULED, publisher=None):
        """Run one server's digest at `due`; the titles it posted, sorted."""
        self.world.clock.t = due
        publisher = publisher or FakePublisher()
        deps = GuildDigestDeps(
            cfg=self.world.cfg,
            db_path=self.world.db_path,
            now=self.world.clock,
            publisher_for=lambda *a: publisher,
            notify_guild=_noop_notify,
            summary_for=summary_lookup(self.world.deps()),
            sleep=self.world.sleep,
            pace_s=0,
        )
        await run_guild_digest(deps, gid, kind=kind)
        text = " ".join(
            re.sub(r"<https?://[^>]*>", " ", f"{m.embed.description or ''}")
            for m in publisher.posted
        )
        return sorted(
            t for t in self.titles for _ in re.findall(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text)
        )

    async def cycle(self, day: int, hour: int, servers: list[int]) -> None:
        """One digest time: the prepare job 20 minutes ahead, then each server's digest."""
        due = pt(day, hour)
        await self.prepare(due - LEAD)
        for gid in servers:
            expected = self.ref.digest(gid, due - LEAD)
            self.expected[gid, day] = expected
            self.shown[gid, day] = await self.post(gid, due)


# --- the three-cycle week ---

A, A2, B, B2, C = 11, 12, 21, 22, 31  # A/A2 at 08:00 (comped/free), B/B2 at 09:00 (free/comped)


def _day_items(sim: Sim, day: int) -> list[tuple[datetime, str, str]]:
    """The news of one day. Times sit between the picks (07:40, 08:40, 11:40) on purpose."""
    items = [
        (pt(day, 6), f"S1 day{day}", "a"),  # before every pick
        (pt(day, 8, 10), f"S2 day{day}", "a"),  # after the 08:00 pick, before the 09:00 one
        (pt(day, 10), f"S3 day{day}", "a"),  # between the 09:00 pick and the 12:00 one
        (pt(day, 13, 30), f"S2 day{day}", "b"),  # S2 again from a second outlet, after all picks
    ]
    if day == 3:
        # Shown to B at 08:40 on day 1 and to C at 11:40 on day 1, and the pick that meets it is
        # exactly three days later on both: the window's edge. A was not there on day 1.
        items.append((pt(day, 13, 50), "S1 day1", "b"))
    if day == 4:
        items.append((pt(day, 13, 45), "S1 day2", "b"))
    return items


async def _run_week(
    sim: Sim,
    days: int = 7,
    *,
    moves: dict[int, dict[int, int]] | None = None,
    extra: list[tuple[datetime, str, str]] = (),
) -> None:
    """C joins on day 0, B and B2 on day 1, A and A2 on day 2: each first digest finds no fresh
    pick (the six hour reuse rule), so each cycle starts its own chain.

    `moves` is {day: {server: new digest hour}}, applied at the start of that day. `extra` is
    more news, dropped on its own day in time order (ids have to rise with time).
    """
    hours = {A: 8, A2: 8, B: 9, B2: 9, C: 12}
    joins = {0: [(C, "comped")], 1: [(B, "free"), (B2, "comped")], 2: [(A, "comped"), (A2, "free")]}
    for day in range(days):
        todays = [*_day_items(sim, day), *(e for e in extra if (e[0] - MIDNIGHT).days == day)]
        for at, title, outlet in sorted(todays, key=lambda e: e[0]):
            sim.drop(title, at, outlet)
        for gid, tier in joins.get(day, []):
            sim.join(gid, tier, f"{hours[gid]:02d}:00")
        for gid, hour in (moves or {}).get(day, {}).items():
            hours[gid] = hour
            with sim.world.conn() as conn, conn:
                conn.execute(
                    "UPDATE guilds SET digest_time = ? WHERE guild_id = ?", (f"{hour:02d}:00", gid)
                )
        for hour in sorted({h for g, h in hours.items() if g in sim.tiers}):
            gids = [g for g in (A, A2, B, B2, C) if g in sim.tiers and hours[g] == hour]
            await sim.cycle(day, hour, gids)


async def test_three_cycles_over_a_week_each_server_gets_what_it_has_not_been_shown(
    tmp_path, monkeypatch
):
    sim = Sim(tmp_path, monkeypatch)

    await _run_week(sim)

    assert sim.shown == sim.expected


async def test_the_08_00_server_still_gets_the_story_the_09_00_pick_already_stored(
    tmp_path, monkeypatch
):
    # The bug this batch fixed, through the whole pipeline and a week of it: S2 arrives at 08:10,
    # after A's pick and before B's. B's pick stores it. A's next pick (a day later) has the
    # item in its own window and must not be told it's "already covered".
    sim = Sim(tmp_path, monkeypatch)

    await _run_week(sim)

    for day in range(3, 7):
        assert f"S2 day{day - 1}" in sim.shown[A, day], f"A lost S2 day{day - 1} on day {day}"
        assert sim.shown[A, day] == sim.shown[A2, day]  # comped and free on one cycle agree


async def test_a_story_is_never_shown_twice_to_the_same_server_inside_three_days(
    tmp_path, monkeypatch
):
    sim = Sim(tmp_path, monkeypatch)

    await _run_week(sim)

    for gid in (A, B, C):
        seen: list[tuple[str, int]] = []
        for (g, day), titles in sorted(sim.shown.items(), key=lambda kv: kv[0][1]):
            if g != gid:
                continue
            for title in titles:
                clash = [d for t, d in seen if t == title and day - d < 3]
                assert not clash or titles.count(title) > 1, (gid, day, title, clash)
            seen += [(t, day) for t in titles]


async def test_a_server_that_moves_its_digest_time_mid_week_keeps_its_own_history(
    tmp_path, monkeypatch
):
    # C goes from 12:00 to 13:00 on day 2 (a cycle of its own, with its chain just carried on:
    # its mark is the same, only the pick's moment moves), then onto B's 09:00 cycle on day 5
    # (its mark and B's differ at first, and both picks end at the same item).
    sim = Sim(tmp_path, monkeypatch)

    await _run_week(sim, moves={2: {C: 13}, 5: {C: 9}})

    assert sim.shown == sim.expected


async def test_an_update_marker_only_points_at_a_story_the_servers_own_chain_was_shown(
    tmp_path, monkeypatch
):
    # "S2 day5: update" turns up after every cycle's day 5 pick. B and C were shown "S2 day5" on
    # day 5; A was not (its pick was made before S2 arrived) and meets both in one pick on day 6.
    # So B's and C's pick on day 6 may call the item an update of the story they were shown, and
    # A's must not call it an update of a story A never saw.
    sim = Sim(tmp_path, monkeypatch)
    update = "S2 day5: update"

    await _run_week(sim, extra=[(pt(5, 13, 40), update, "c")])

    with sim.world.conn() as conn:
        picks = conn.execute(
            "SELECT s.id, s.items_after, s.items_upto, st.id AS story_id, st.is_update_of "
            "FROM game_summaries s JOIN stories st ON st.summary_id = s.id "
            "WHERE st.headline = ? ORDER BY s.id",
            (update,),
        ).fetchall()
        assert len(picks) == 3, "A, B and C each get their own pick of the update"
        for pick in picks:
            shown_before = {
                row["id"]
                for row in conn.execute(
                    "SELECT st.id FROM stories st JOIN game_summaries s ON s.id = st.summary_id "
                    "WHERE st.headline = 'S2 day5' AND s.items_upto = ?",
                    (pick["items_after"],),
                )
            }
            if shown_before:
                assert pick["is_update_of"] in shown_before
            else:
                assert pick["is_update_of"] is None, "an UPDATE marker on a story never shown"
        assert sorted(p["is_update_of"] is None for p in picks) == [False, False, True]


async def test_a_server_that_fell_back_and_rejoined_is_still_told_what_it_was_shown_before(
    tmp_path, monkeypatch
):
    # Day 0: the server is shown "Studio closes". Day 1: the model is down, the pick falls back,
    # and the server gets headlines (an item lands between the 08:40 pick and the 09:00 digest,
    # as the hourly collection does). Day 2: "Studio closes" is reported again from another
    # outlet. The server was shown it two days ago; it must not be shown it as news.
    sim = Sim(tmp_path, monkeypatch)
    server = 41
    sim.join(server, "comped", "09:00")
    sim.drop("Studio closes", pt(0, 6))
    await sim.cycle(0, 9, [server])
    assert sim.shown[server, 0] == ["Studio closes"]

    sim.drop("Layoffs", pt(1, 7))
    sim.model.fail = True
    await sim.prepare(pt(1, 9) - LEAD)
    sim.drop("Late arrival", pt(1, 8, 50))
    await sim.post(server, pt(1, 9))
    sim.model.fail = False
    assert [r["status"] for r in sim.world.summaries(INDUSTRY_KEY)][-1] == "fallback"

    sim.drop("Studio closes", pt(1, 12), "b")
    sim.drop("Fresh news", pt(1, 13))
    await sim.prepare(pt(2, 9) - LEAD)
    day_two = await sim.post(server, pt(2, 9))

    assert "Fresh news" in day_two
    assert "Studio closes" not in day_two


async def test_a_comped_server_that_ran_run_now_keeps_its_chain(tmp_path, monkeypatch):
    # The comped run-now makes its own pick up to the newest item, and that item is the digest's
    # mark, so the next scheduled pick still finds the chain.
    sim = Sim(tmp_path, monkeypatch)
    server = 42
    sim.join(server, "comped", "09:00")
    sim.drop("Studio closes", pt(0, 6))
    await sim.cycle(0, 9, [server])
    sim.drop("Layoffs", pt(1, 5))
    await sim.post(server, pt(1, 6), kind=RunKind.RUN_NOW)
    sim.drop("Late news", pt(1, 7))  # after the run-now, before the prepare job
    sim.drop("Studio closes", pt(1, 12), "b")
    sim.drop("Fresh news", pt(1, 13))
    await sim.prepare(pt(1, 9) - LEAD)  # the prepare job doesn't know a run-now happened

    await sim.prepare(pt(2, 9) - LEAD)
    day_two = await sim.post(server, pt(2, 9))

    assert "Fresh news" in day_two
    assert "Studio closes" not in day_two


async def test_a_free_server_that_ran_run_now_keeps_its_chain(tmp_path, monkeypatch):
    # A free run-now never makes the pick (owner decision 2A): if none is stored for the
    # server's coverage it gets the newest headlines, and its mark is the digest's own range
    # end, which no pick ended at.
    sim = Sim(tmp_path, monkeypatch)
    server = 43
    sim.join(server, "free", "09:00")
    sim.drop("Studio closes", pt(0, 6))
    await sim.cycle(0, 9, [server])
    sim.drop("Layoffs", pt(1, 5))
    await sim.post(server, pt(1, 6), kind=RunKind.RUN_NOW)
    sim.drop("Late news", pt(1, 7))  # after the run-now, before the prepare job
    sim.drop("Studio closes", pt(1, 12), "b")
    sim.drop("Fresh news", pt(1, 13))
    await sim.prepare(pt(1, 9) - LEAD)  # the prepare job doesn't know a run-now happened

    await sim.prepare(pt(2, 9) - LEAD)
    day_two = await sim.post(server, pt(2, 9))

    assert "Fresh news" in day_two
    assert "Studio closes" not in day_two


async def test_a_refused_channel_is_not_told_the_stories_nobody_saw(tmp_path, monkeypatch):
    # Day 1's pick stores "Layoffs", but the channel refuses the post, so nobody reads it. The
    # server's mark stays at day 0's, day 2's pick starts there, and must not treat day 1's
    # pick as shown: "Layoffs" is still news on day 2. Day 0's story is still prior.
    sim = Sim(tmp_path, monkeypatch)
    server = 44
    sim.join(server, "comped", "09:00")
    sim.drop("Studio closes", pt(0, 6))
    await sim.cycle(0, 9, [server])

    class Refuses(FakePublisher):
        skipped = {INDUSTRY_KEY: "no permission"}  # noqa: RUF012

        async def publish(self, r):
            return {}

    sim.drop("Layoffs", pt(1, 6))
    await sim.prepare(pt(1, 9) - LEAD)
    assert await sim.post(server, pt(1, 9), publisher=Refuses()) == []
    sim.drop("Studio closes", pt(1, 12), "b")
    sim.drop("Fresh news", pt(1, 13))
    await sim.prepare(pt(2, 9) - LEAD)
    day_two = await sim.post(server, pt(2, 9))

    assert day_two == ["Fresh news", "Layoffs"]


async def test_days_with_no_news_do_not_break_the_chain(tmp_path, monkeypatch):
    # Two quiet days make two picks that start and end at the same item (nothing to summarize,
    # no model call). The story from before them is still prior on the third day, and on the
    # fourth it has left the window.
    sim = Sim(tmp_path, monkeypatch)
    server = 45
    sim.join(server, "comped", "09:00")
    sim.drop("Studio closes", pt(0, 6))
    await sim.cycle(0, 9, [server])
    calls = len(sim.model.priors)
    await sim.cycle(1, 9, [server])
    await sim.cycle(2, 9, [server])
    assert len(sim.model.priors) == calls, "a quiet day should not call the model"
    with sim.world.conn() as conn:
        quiet = conn.execute(
            "SELECT COUNT(*) AS n FROM game_summaries WHERE items_after = items_upto"
        ).fetchone()["n"]
    assert quiet == 2

    sim.drop("Studio closes", pt(2, 12), "b")
    await sim.cycle(3, 9, [server])
    assert sim.shown[server, 3] == []  # shown on day 0 08:40, three days before day 3 08:40

    sim.drop("Studio closes", pt(3, 12), "c")
    await sim.cycle(4, 9, [server])
    # Day 3's pick stored nothing, and day 0's story is four days old: it is news again.
    assert sim.shown[server, 4] == ["Studio closes"]


# --- the repo helper, straight at the edges ---


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _put(
    conn: sqlite3.Connection,
    *,
    after: int | None,
    upto: int,
    at: datetime,
    headlines: tuple[str, ...] = (),
    status: str = "ok",
    game: str = INDUSTRY_KEY,
) -> int:
    """One stored pick and its stories, written straight to the tables (thousands are cheap)."""
    summary_id = conn.execute(
        "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
        "created_at, items_after, items_upto) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (game, at.date().isoformat(), status, _iso(at - DAY), _iso(at), _iso(at), after, upto),
    ).lastrowid
    for headline in headlines:
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, summary_id, created_at) "
            "VALUES (?, ?, 'x', 'reported', ?, ?)",
            (game, headline, summary_id, _iso(at)),
        )
    return summary_id


@pytest.fixture
def db(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")
    return world


def _chain(world, mark, since, game=INDUSTRY_KEY) -> list[str]:
    with world.conn() as conn:
        return [p.headline for p in repo.chain_headlines(conn, game, since, mark)]


SINCE = pt(5, 8, 40)  # a pick's `end - 3 days`


def test_a_story_created_exactly_at_the_window_start_is_prior_and_a_microsecond_earlier_is_not(db):
    with db.conn() as conn, conn:
        _put(conn, after=None, upto=10, at=SINCE - timedelta(microseconds=1), headlines=("early",))
        _put(conn, after=10, upto=20, at=SINCE, headlines=("on the edge",))
        _put(conn, after=20, upto=30, at=SINCE + timedelta(microseconds=1), headlines=("late",))
        game_stories = ("on the edge",)
        _put(conn, after=None, upto=10, at=SINCE, headlines=game_stories, game="palworld")

    got = _chain(db, 30, SINCE)

    assert got == ["late", "on the edge"]
    # The same instant is the same side of the line for a game's `recent_headlines`.
    with db.conn() as conn:
        game = [p.headline for p in repo.recent_headlines(conn, "palworld", SINCE)]
    assert game == ["on the edge"]


def test_the_window_start_may_be_given_in_any_zone_or_with_none(db):
    with db.conn() as conn, conn:
        _put(conn, after=None, upto=10, at=SINCE, headlines=("on the edge",))

    in_la = _chain(db, 10, SINCE.astimezone(ZoneInfo(LA)))
    naive = _chain(db, 10, SINCE.replace(tzinfo=None))  # taken to be UTC

    assert in_la == naive == ["on the edge"]


def test_a_pick_whose_stories_were_purged_keeps_the_chain_linked(db):
    with db.conn() as conn, conn:
        _put(conn, after=None, upto=10, at=pt(5, 9), headlines=("one",))
        middle = _put(conn, after=10, upto=20, at=pt(5, 12), headlines=("two",))
        _put(conn, after=20, upto=30, at=pt(5, 15), headlines=("three",))
        conn.execute("DELETE FROM stories WHERE summary_id = ?", (middle,))

    assert _chain(db, 30, SINCE) == ["three", "one"]


def test_a_chain_never_reads_a_story_that_lost_its_pick(db):
    # `summary_id` goes NULL when a summary row is purged; those stories belong to no chain.
    with db.conn() as conn, conn:
        gone = _put(conn, after=None, upto=10, at=pt(5, 9), headlines=("orphan",))
        _put(conn, after=10, upto=20, at=pt(5, 12), headlines=("kept",))
        conn.execute("DELETE FROM game_summaries WHERE id = ?", (gone,))

    assert _chain(db, 20, SINCE) == ["kept"]
    assert _chain(db, 10, SINCE) == []


def test_picks_that_point_at_each_other_in_a_circle_end_the_walk(db):
    # Not something the code writes (a pick's end is never below its start), but a restored or
    # hand-edited database could hold it, and the walk must still come home.
    with db.conn() as conn, conn:
        _put(conn, after=20, upto=10, at=pt(5, 9), headlines=("one",))
        _put(conn, after=10, upto=20, at=pt(5, 12), headlines=("two",))

    assert sorted(_chain(db, 20, SINCE)) == ["one", "two"]
    assert sorted(_chain(db, 10, SINCE)) == ["one", "two"]


def test_two_chains_that_end_at_one_item_are_both_prior_for_the_shared_pick_after_it(db):
    # Cycle X and cycle Y both ended at item 20 (a quiet night). The next pick for either is the
    # same pick (they now share a mark), so it can only have one prior list: the union. This
    # pins that: the narrowest list the records support, since nothing says who read which.
    with db.conn() as conn, conn:
        _put(conn, after=None, upto=8, at=pt(5, 9), headlines=("x early",))
        _put(conn, after=None, upto=12, at=pt(5, 10), headlines=("y early",))
        _put(conn, after=8, upto=20, at=pt(5, 13), headlines=("x late",))
        _put(conn, after=12, upto=20, at=pt(5, 14), headlines=("y late",))

    assert _chain(db, 20, SINCE) == ["y late", "x late", "y early", "x early"]
    # A server that is still behind sees only its own side.
    assert _chain(db, 8, SINCE) == ["x early"]
    assert _chain(db, 12, SINCE) == ["y early"]


def _statements(world, call):
    seen: list[str] = []
    with world.conn() as conn:
        conn.set_trace_callback(seen.append)
        started = time.perf_counter()
        result = call(conn)
        elapsed = time.perf_counter() - started
        conn.set_trace_callback(None)
    return result, [q for q in seen if q.lstrip().upper().startswith("SELECT")], elapsed


def test_a_long_chain_costs_one_query_per_pick_and_a_story_query_per_900(db):
    # Three days of a pick every three minutes, one story each: 1,440 picks on one chain.
    n = 1440
    start = SINCE + timedelta(minutes=1)
    with db.conn() as conn, conn:
        for i in range(n):
            _put(
                conn,
                after=None if i == 0 else i * 10,
                upto=(i + 1) * 10,
                at=start + timedelta(seconds=150 * i),
                headlines=(f"story {i}",),
            )
    since = start

    got, selects, elapsed = _statements(
        db, lambda conn: repo.chain_headlines(conn, INDUSTRY_KEY, since, n * 10)
    )

    assert len(got) == n
    assert got[0].headline == f"story {n - 1}" and got[-1].headline == "story 0"
    assert len(selects) <= n + 3, "one walk query per pick, plus two story chunks"
    assert elapsed < 10


def test_many_unrelated_chains_cost_nothing_to_the_one_asking(db):
    # 400 other cycles, three picks each, over the same three days. The asking chain has three.
    with db.conn() as conn, conn:
        for chain in range(401):
            base = chain * 1000
            for step in range(3):
                _put(
                    conn,
                    after=None if step == 0 else base + step * 10,
                    upto=base + (step + 1) * 10,
                    at=pt(5, 9) + timedelta(hours=step, minutes=chain % 60),
                    headlines=(f"chain {chain} story {step}",),
                )

    got, selects, _ = _statements(
        db, lambda conn: repo.chain_headlines(conn, INDUSTRY_KEY, SINCE, 400 * 1000 + 30)
    )

    assert sorted(p.headline for p in got) == [f"chain 400 story {s}" for s in range(3)]
    assert (
        len(selects) <= 5
    )  # three marks walked (the last finds nothing before) and one stories query


def test_a_lot_of_old_history_in_the_table_does_not_change_the_walk(db):
    with db.conn() as conn, conn:
        for i in range(3000):
            _put(conn, after=None, upto=i + 1, at=SINCE - timedelta(days=1, minutes=i))
        _put(conn, after=None, upto=5000, at=pt(5, 9), headlines=("recent",))

    got, selects, elapsed = _statements(
        db, lambda conn: repo.chain_headlines(conn, INDUSTRY_KEY, SINCE, 5000)
    )

    assert [p.headline for p in got] == ["recent"]
    assert len(selects) <= 3
    assert elapsed < 2


def test_a_quiet_weekend_where_every_cycle_ends_at_one_item_is_a_linear_walk(db):
    # 800 picks (a day of cycles for three days, say) all ending at item 1000, each starting
    # somewhere different. Every one is "possibly shown" to a server on that mark, and the walk
    # follows each one's start once. Bounded by the picks in the window, not by anything else.
    n = 800
    with db.conn() as conn, conn:
        for i in range(n):
            _put(
                conn,
                after=None if i == 0 else i,
                upto=1000,
                at=SINCE + timedelta(minutes=1 + 5 * i),
                headlines=(f"story {i}",),
            )

    got, selects, elapsed = _statements(
        db, lambda conn: repo.chain_headlines(conn, INDUSTRY_KEY, SINCE, 1000)
    )

    assert len(got) == n
    assert len(selects) <= n + 3
    assert elapsed < 10


# --- the game priors must not move ---


def test_a_games_prior_headlines_are_the_same_rows_in_the_same_order_with_industry_rows_about(db):
    games = [
        ("patch 1", pt(5, 9)),
        ("patch 2", pt(5, 12)),
        ("patch 3", pt(5, 15)),
    ]
    with db.conn() as conn, conn:
        for i, (headline, at) in enumerate(games):
            _put(
                conn, after=i * 10, upto=(i + 1) * 10, at=at, headlines=(headline,), game="palworld"
            )
        # Industry picks interleaved in time, on a chain that ends at the game's marks.
        for i in range(3):
            _put(
                conn,
                after=i * 10,
                upto=(i + 1) * 10,
                at=pt(5, 10 + 3 * i),
                headlines=(f"industry {i}",),
            )
    with db.conn() as conn:
        before = repo.recent_headlines(conn, "palworld", SINCE)

    game = next(g for g in db.cfg.catalog if g.key == "palworld")
    end = SINCE + timedelta(days=3)
    for mark in (None, 0, 10, 20, 30, 99):  # a mark on the game's own chain, or on none
        after = None if mark is None else Coverage(end - DAY, mark)
        prior = summaries._inputs_sync(db.db_path, db.cfg, game, end, after, None)[4]
        assert [(p.id, p.headline, p.created_at) for p in prior] == [
            (p.id, p.headline, p.created_at) for p in before
        ]
    assert [p.headline for p in before] == ["patch 3", "patch 2", "patch 1"]


def test_the_industry_entry_reads_its_chain_through_the_same_inputs_call(db):
    with db.conn() as conn, conn:
        _put(conn, after=None, upto=10, at=pt(5, 9), headlines=("shown",))
        _put(conn, after=None, upto=12, at=pt(5, 10), headlines=("someone elses",))
    game = next(g for g in db.cfg.catalog if g.key == INDUSTRY_KEY)
    end = SINCE + timedelta(days=3)

    on_ten = summaries._inputs_sync(db.db_path, db.cfg, game, end, Coverage(end - DAY, 10), None)[4]
    first = summaries._inputs_sync(db.db_path, db.cfg, game, end, None, None)[4]

    assert [p.headline for p in on_ten] == ["shown"]
    assert first == []
