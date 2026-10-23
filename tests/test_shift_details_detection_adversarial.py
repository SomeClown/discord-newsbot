"""Adversarial tests for SHiFT details detection (plan task 5; design.md §16).

The owner's rules, in the order a stranger would notice them breaking:
1. No repeat pings, ever.
2. No flood at rollout: undated rows are quiet on a source's first listing.
3. A code that expired before we first said it is quiet.
4. B+: mentalmars posts unpinged, and confirming a community code inside 24 hours
   earns exactly one pinged follow-up.
5. Gearbox outranks on details, but only where it says something.
6. The daily ping cap holds, follow-ups included.

Each section attacks one of those from the side the happy-path file doesn't: the
rollout in odd shapes, the freshness boundaries to the second, Pacific versus UTC
at the edges of a day, the ping budget under B+, hostile batches, a crash at every
step of `_release` followed by a convergence check, readers and overlapping hooks,
and a seeded random walk over three days of hourly passes that checks the rules as
invariants. Real temp SQLite with the real migrations, scripted fake posters, the
real collector over `httpx.MockTransport`, an injected clock. No network.

The four `xfail(strict=True)` tests this round started with (per-table first listing,
permanent codes, follow-ups for dead codes, scope by item identity) are plain tests now:
the bugs they pinned are fixed.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from test_collect import make_deps
from test_shift_details_detection import (
    FIXTURE,
    GEARBOX,
    LATER,
    MENTALMARS,
    PAGE_URL,
    REDDIT,
    T0,
    X,
    Y,
    Z,
    code_n,
    cosmetic_table,
    main_table,
    page,
    permanent_table,
    reddit_item,
    seed_prod_like,
    statuses,
)
from test_shift_fanout import V3, Harness, add_guild, rows, seed

from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.collectors.shift_table import PageValidators, ShiftTableCollector
from newsbot.config import ShiftTableSource, load_config
from newsbot.pipeline.collect import run_collection
from newsbot.pipeline.lock import _run_lock
from newsbot.shift import fanout
from newsbot.shift.details import SHIFT_TZ, is_current
from newsbot.shift.fanout import _canonical_batch, make_shift_hook
from newsbot.store import repo
from newsbot.store.db import connect, migrate

BLUESKY = "Borderlands 4 (Bluesky search)"
OTHER_SITE = "another-codes-site"
FOLLOWUP_TEXT = "confirmed by a second source"


async def _noop_sleep(_seconds: float) -> None:
    return None


# --- scaffolding ---


class Ledger(Harness):
    """A `Harness` that remembers every message from every pass, and when it went out.

    The base harness keeps one poster per server (the last one made), and a pass that sends
    an alert and then a follow-up makes two. Here every poster is kept until `harvest()`.
    """

    def __init__(self, db_path, cfg, now):
        super().__init__(db_path, cfg, now)
        self.history: dict[int, list[tuple[datetime, object]]] = {}
        self.made: list = []
        self.last_pass: dict[int, list] = {}

    def factory(self, guild_id, channel_id, ping, notify):
        poster = super().factory(guild_id, channel_id, ping, notify)
        self.made.append(poster)
        return poster

    def harvest(self) -> None:
        """File what the posters sent since the last harvest under the current clock."""
        self.last_pass = {}
        for poster in self.made:
            self.last_pass.setdefault(poster.guild_id, []).extend(poster.sent)
            self.history.setdefault(poster.guild_id, []).extend(
                (self.clock, a) for a in poster.sent
            )
        self.made = []

    def sent(self, guild_id):
        return self.last_pass.get(guild_id, [])

    async def run(self, items, *, seeding_ok=True, at=None):
        if at is not None:
            self.clock = at
        self.made = []
        try:
            return await super().run(items, seeding_ok=seeding_ok)
        finally:
            self.harvest()

    def alerts(self, guild_id):
        return [a for _, a in self.history.get(guild_id, [])]

    def originals(self, guild_id):
        return [a for a in self.alerts(guild_id) if FOLLOWUP_TEXT not in a.content]

    def followups(self, guild_id):
        return [a for a in self.alerts(guild_id) if FOLLOWUP_TEXT in a.content]

    def original_codes(self, guild_id):
        return [c for a in self.originals(guild_id) for c in a.codes]

    def pings(self, guild_id):
        return [a for a in self.alerts(guild_id) if a.ping]


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def h(db_path):
    harness = Ledger(db_path, load_config(V3), T0)
    seed(db_path)
    return harness


def source(name: str = MENTALMARS, url: str = PAGE_URL) -> ShiftTableSource:
    return ShiftTableSource(
        type="shift_table", name=name, url=url, trust="community", topics=["borderlands4"]
    )


async def read_named(
    html: str | bytes, now: datetime, name: str = MENTALMARS, url: str = PAGE_URL
) -> list[RawItem]:
    body = html.encode() if isinstance(html, str) else html
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as http:
        collector = ShiftTableCollector(source(name, url), sleep=_noop_sleep, clock=lambda: now)
        return await collector.collect(http)


def d(day: date) -> str:
    """A date the way the page writes it."""
    return f"{day:%b} {day.day}, {day.year}"


def synthetic_rows(n: int, *, prefix: str, added: date | None) -> list[RawItem]:
    """`n` table rows as the collector would hand them over (it caps a page at 5,000 rows)."""
    published = (
        datetime.combine(added, datetime.min.time(), tzinfo=UTC) + timedelta(hours=7)
        if added
        else None
    )
    out = []
    for i in range(n):
        code = f"{prefix}{i:04d}-ABCDE-FGHJK-LMNPQ-RSTVW"
        out.append(
            RawItem(
                url=PAGE_URL,
                title=code,
                excerpt=code,
                source_name=MENTALMARS,
                trust="community",
                published_at=published,
                topics=("borderlands4",),
                full_text=code,
                details=ItemDetails(
                    code=code,
                    rank="table",
                    reward="1 Golden Key",
                    category="golden",
                    added_on=added,
                    listed=True,
                ),
            )
        )
    return out


def details_of(db_path: str, code: str):
    with closing(connect(db_path)) as conn:
        return repo.code_details_for(conn, [code]).get(code)


def first_seen(db_path: str, code: str) -> datetime:
    [(stamp,)] = rows(db_path, "SELECT first_seen_at FROM alerted_codes WHERE code = ?", code)
    return datetime.fromisoformat(stamp)


def followup_rows(db_path: str):
    return rows(
        db_path, "SELECT guild_id, code, status, pinged FROM guild_code_followups ORDER BY rowid"
    )


def listed_markers(db_path: str) -> list[str]:
    """The names of the table sources that have listed something, from their per-kind markers."""
    keys = rows(db_path, "SELECT key FROM app_state WHERE key LIKE 'shift_table_kind_listed:%'")
    return sorted({k.split(":", 2)[2] for (k,) in keys})


def official(text: str, *, at: datetime, tag: str = "post") -> RawItem:
    """A Gearbox post (its own URL, so two in one batch aren't deduped into one)."""
    return RawItem(
        url=f"https://example.com/gearbox/{tag}",
        title=f"Borderlands 4 SHiFT: {text}",
        excerpt="",
        source_name=GEARBOX,
        trust="official",
        published_at=at,
    )


def community(code: str, *, at: datetime, name: str) -> RawItem:
    return reddit_item(code, at=at, source=name)


def ping_count(db_path: str, guild_id: int) -> int:
    [(n,)] = rows(db_path, "SELECT ping_count FROM guild_shift WHERE guild_id = ?", guild_id)
    return n


# =====================================================================
# Rollout variants
# =====================================================================


async def test_first_listing_beside_a_fresh_reddit_sighting_posts_that_one_code_only(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-10", ping_count=1)
    items = await read_named(FIXTURE.read_bytes(), LATER)
    codes = [i.title for i in items]
    seed_prod_like(h.db_path, codes)
    strangers = [c for c in codes if c not in statuses(h.db_path)]
    chosen = strangers[0]
    h.clock = LATER

    released = await h.run([reddit_item(chosen, at=LATER), *items])

    # The Reddit post is dated and fresh, so that one code is news. The other
    # ten rows of the page are the bookshelf.
    assert released == 1
    assert [c for a in h.sent(1) for c in a.codes] == [chosen]
    after = statuses(h.db_path)
    assert after[chosen] == "posted"
    assert {c for c in strangers if c != chosen and after[c] == "too_old"} == set(strangers) - {
        chosen
    }
    # Two names saw it in one pass, which is the existing B+ shortcut: it pings once, and
    # spends exactly one unit of the day's budget.
    assert [a.ping for a in h.sent(1)] == [True]
    assert ping_count(h.db_path, 1) == 2
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_followups") == [(0,)]


async def test_first_listing_beside_a_stale_reddit_sighting_posts_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    items = await read_named(FIXTURE.read_bytes(), LATER)
    codes = [i.title for i in items]
    seed_prod_like(h.db_path, codes)
    stranger = next(c for c in codes if c not in statuses(h.db_path))
    old_reddit = replace(
        reddit_item(stranger, at=LATER), published_at=LATER - timedelta(days=3, hours=1)
    )
    h.clock = LATER

    assert await h.run([old_reddit, *items]) == 0

    assert h.sent(1) == []
    assert statuses(h.db_path)[stranger] == "too_old"


async def test_first_listing_on_an_unseeded_database_seeds_it_and_stays_silent(db_path):
    harness = Ledger(db_path, load_config(V3), LATER)  # deliberately not seeded
    add_guild(db_path, 1, ping="everyone")
    items = await read_named(FIXTURE.read_bytes(), LATER)

    assert await harness.run(items) == 0

    assert harness.alerts(1) == []
    assert {s for s in statuses(db_path).values()} == {"seeded"}
    assert len(statuses(db_path)) == 11
    assert rows(db_path, "SELECT 1 FROM alert_state WHERE key = 'seeded_at'")
    assert listed_markers(db_path) == [MENTALMARS]

    # Now it's seeded and the source has listed once: an undated newcomer is ordinary news.
    harness.clock = LATER + timedelta(hours=1)
    extra = page(cosmetic_table(("Banner: Brand New", "Newsletter", X, "Unknown")))
    assert await harness.run(await read_named(extra, harness.clock)) == 1
    assert harness.original_codes(1) == [X]
    assert harness.pings(1) == []


async def test_an_unhealthy_unseeded_first_listing_still_cannot_flood_the_next_pass(db_path):
    harness = Ledger(db_path, load_config(V3), LATER)
    add_guild(db_path, 1, ping="everyone")
    html = FIXTURE.read_bytes()

    # seeding_ok False: the sweep wasn't healthy enough to flip the seeded marker.
    await harness.run(await read_named(html, LATER), seeding_ok=False)
    assert not rows(db_path, "SELECT 1 FROM alert_state WHERE key = 'seeded_at'")

    harness.clock = LATER + timedelta(hours=1)
    assert await harness.run(await read_named(html, harness.clock)) == 0
    assert harness.alerts(1) == []
    assert {s for s in statuses(db_path).values()} == {"seeded"}


async def test_two_table_sources_listing_for_the_first_time_in_one_pass_are_both_quiet(h):
    add_guild(h.db_path, 1, ping="everyone")
    html_a = page(cosmetic_table(("Gnome", "Event", X, "Unknown")))
    html_b = page(
        permanent_table(("Skin: Sunrise", "Stream", Y)),
        cosmetic_table(("Gnome", "Event", X, "???")),
    )
    items = [
        *await read_named(html_a, T0, MENTALMARS, PAGE_URL),
        *await read_named(html_b, T0, OTHER_SITE, "https://other.example/bl4-codes/"),
    ]

    assert await h.run(items) == 0

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old", Y: "too_old"}
    assert listed_markers(h.db_path) == sorted([MENTALMARS, OTHER_SITE])
    # Each earns its own later passes: one undated newcomer from each posts, unpinged.
    h.clock = T0 + timedelta(hours=1)
    next_a = page(cosmetic_table(("Gnome", "Event", X, "Unknown"), ("Hat", "Event", Z, "Unknown")))
    next_b = page(permanent_table(("Skin", "Stream", Y), ("Skin 2", "Stream", code_n(7))))
    await h.run(
        [
            *await read_named(next_a, h.clock, MENTALMARS, PAGE_URL),
            *await read_named(next_b, h.clock, OTHER_SITE, "https://other.example/bl4-codes/"),
        ]
    )
    assert sorted(h.original_codes(1)) == sorted([Z, code_n(7)])
    assert h.pings(1) == []


async def test_a_second_new_table_source_does_not_make_the_first_one_forget(h):
    """Another source's first listing must not put mentalmars back in first-listing mode."""
    add_guild(h.db_path, 1)
    await h.run(await read_named(page(cosmetic_table(("Gnome", "Event", X, "Unknown"))), T0))
    h.clock = T0 + timedelta(hours=1)
    items = [
        *await read_named(
            page(cosmetic_table(("Gnome", "Event", X, "Unknown"), ("Hat", "Event", Z, "???"))),
            h.clock,
        ),
        *await read_named(
            page(permanent_table(("Skin", "Stream", Y))),
            h.clock,
            OTHER_SITE,
            "https://other.example/bl4-codes/",
        ),
    ]

    await h.run(items)

    # Z is mentalmars' undated newcomer (not a first listing for it): posts. Y is the other
    # site's first listing: quiet.
    assert h.original_codes(1) == [Z]
    assert statuses(h.db_path)[Y] == "too_old"


async def test_renamed_source_is_a_new_first_listing_and_does_not_flood(h):
    """Decision (design 16, E1): a new name has never listed here, so its undated rows are quiet.

    Every code on the page is already known from the old name, so the rename costs nothing
    but one undated newcomer being recorded quietly on that first pass.
    """
    add_guild(h.db_path, 1, ping="everyone")
    day = d(T0.date())
    old_page = page(
        main_table(("3 Golden Keys", day, X, "Unknown")),
        cosmetic_table(("Gnome", "Event", Y, "Unknown")),
        permanent_table(("Skin", "Stream", Z)),
    )
    await h.run(await read_named(old_page, T0, "mentalmars"))
    assert h.original_codes(1) == [X]  # the dated, fresh one; the rest were the bookshelf
    before = rows(h.db_path, "SELECT * FROM guild_code_posts ORDER BY code")

    h.clock = T0 + timedelta(hours=26)  # past the 24h follow-up window, so B+ stays out of it
    newcomer_undated = code_n(5)
    newcomer_dated = code_n(6)
    new_page = page(
        main_table(
            ("3 Golden Keys", day, X, "Unknown"),
            ("1 Golden Key", d(h.clock.date()), newcomer_dated, "Unknown"),
        ),
        cosmetic_table(("Gnome", "Event", Y, "Unknown"), ("Hat", "Event", newcomer_undated, "?")),
        permanent_table(("Skin", "Stream", Z)),
    )
    await h.run(await read_named(new_page, h.clock, "mentalmars.com"))

    assert h.original_codes(1) == [X, newcomer_dated]  # nothing known came back
    assert statuses(h.db_path)[newcomer_undated] == "too_old"
    assert [
        r
        for r in rows(h.db_path, "SELECT * FROM guild_code_posts ORDER BY code")
        if r[1] in (X, Y, Z)
    ] == [r for r in before if r[1] in (X, Y, Z)]
    assert listed_markers(h.db_path) == [
        "mentalmars",
        "mentalmars.com",
    ]
    assert h.pings(1) == []


async def test_rollout_inside_a_community_codes_window_confirms_it_once_and_nothing_else(h):
    """The one thing a rollout may say: a code Reddit posted three hours ago, now on the list."""
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-10", ping_count=1)
    add_guild(h.db_path, 2, ping="none")
    items = await read_named(FIXTURE.read_bytes(), LATER)
    codes = [i.title for i in items]
    seed_prod_like(h.db_path, codes)
    live = next(c for c in codes if c not in statuses(h.db_path))
    with closing(connect(h.db_path)) as conn, conn:
        stamp = (LATER - timedelta(hours=3)).isoformat()
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, message_id, "
            "pinged, status) VALUES (?, ?, ?, 'https://example.com/r', 7100, 0, 'posted')",
            (live, stamp, REDDIT),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, message_id, pinged, "
            "followup_ok, claimed_at) SELECT guild_id, ?, 'posted', 7100, 0, "
            "CASE WHEN guild_id = 1 THEN 1 ELSE 0 END, ? FROM guilds",
            (live, stamp),
        )
        conn.execute(
            "INSERT INTO code_sightings (code, source_name, trusted, roundup, seen_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (live, REDDIT, stamp),
        )
    h.clock = LATER

    assert await h.run(items) == 0

    assert [a.codes for a in h.followups(1)] == [[live]]
    assert h.originals(1) == [] and h.sent(2) == []
    assert ping_count(h.db_path, 1) == 2
    assert followup_rows(h.db_path) == [(1, live, "posted", 1)]
    h.clock = LATER + timedelta(hours=1)
    assert await h.run(await read_named(FIXTURE.read_bytes(), h.clock)) == 0
    assert h.sent(1) == []


async def test_a_collector_that_errors_leaves_the_first_listing_for_the_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    # Pass 1: the table fetch failed, so the hook only hears from the others.
    await h.run([reddit_item(code_n(3), at=T0)])
    assert h.original_codes(1) == [code_n(3)]
    assert listed_markers(h.db_path) == []

    # Pass 2: the page is back. It is still the first listing, so it is quiet.
    h.clock = T0 + timedelta(hours=1)
    assert await h.run(await read_named(FIXTURE.read_bytes(), h.clock)) == 0
    assert listed_markers(h.db_path) == [MENTALMARS]
    assert h.original_codes(1) == [code_n(3)]  # nothing from the page: every row is history
    assert len(statuses(h.db_path)) == 12


async def test_a_page_with_no_code_rows_is_a_failed_pass_and_never_counts_as_a_listing(h):
    add_guild(h.db_path, 1)
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await read_named(page(cosmetic_table()), T0)  # a header and no rows

    # The collection pass records that as a failed source; the hook gets nothing from it.
    assert await h.run([]) == 0
    assert listed_markers(h.db_path) == []

    # So the real first listing, when it comes, is still quiet.
    html = page(cosmetic_table(("Gnome", "Event", X, "Unknown")))
    assert await h.run(await read_named(html, T0 + timedelta(hours=1))) == 0
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_first_listing_that_only_got_part_of_the_page_does_not_flood_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    main = main_table(("1 Golden Key", d(date(2026, 8, 1)), code_n(1), "Unknown"))
    await h.run(await read_named(page(main), T0))
    assert h.alerts(1) == []

    h.clock = T0 + timedelta(hours=1)
    full = page(
        main,
        cosmetic_table(*[("Gnome", "Event", code_n(100 + i), "Unknown") for i in range(10)]),
        permanent_table(*[("Skin", "Stream", code_n(200 + i)) for i in range(10)]),
    )
    await h.run(await read_named(full, h.clock))

    assert h.original_codes(1) == [], "twenty long-standing undated codes were announced"


# =====================================================================
# Freshness edges
# =====================================================================


@pytest.mark.parametrize(
    ("offset_s", "expected"),
    [(-1, "posted"), (0, "posted"), (1, "too_old")],
    ids=["a second inside", "exactly 48 hours", "a second over"],
)
async def test_a_dated_row_is_fresh_through_exactly_48_hours(h, offset_s, expected):
    add_guild(h.db_path, 1)
    # "Oct 7" starts at 2026-10-07 00:00 Pacific = 07:00 UTC (PDT).
    start = datetime(2026, 10, 7, 7, tzinfo=UTC)
    h.clock = start + timedelta(hours=48, seconds=offset_s)
    html = page(main_table(("1 Golden Key", "Oct 7, 2026", X, "Unknown")))
    await h.run(await read_named(html, h.clock))
    # (the table was never listed before, but a dated row doesn't care)

    assert statuses(h.db_path) == {X: expected}
    assert (h.original_codes(1) == [X]) is (expected == "posted")


@pytest.mark.parametrize(
    ("clock", "added", "expected"),
    [
        # 20:00 Pacific on Oct 9 is already Oct 10 in UTC. "Oct 9" is Pacific today.
        (datetime(2026, 10, 10, 3, tzinfo=UTC), "Oct 9, 2026", "posted"),
        # A site a day ahead of Pacific writes tomorrow's date; the collector allows a day.
        (datetime(2026, 10, 10, 3, tzinfo=UTC), "Oct 10, 2026", "posted"),
        # 23:00 Pacific on Oct 8, which is 06:00 UTC on Oct 9. "Oct 7" is 47 hours old.
        (datetime(2026, 10, 9, 6, tzinfo=UTC), "Oct 7, 2026", "posted"),
        (datetime(2026, 10, 9, 6, tzinfo=UTC), "Oct 6, 2026", "too_old"),
    ],
)
async def test_added_dates_are_pacific_days_not_utc_days(h, clock, added, expected):
    add_guild(h.db_path, 1)
    h.clock = clock

    await h.run(await read_named(page(main_table(("1 Golden Key", added, X, "Unknown"))), clock))

    assert statuses(h.db_path) == {X: expected}


async def test_a_row_whose_added_date_moves_forward_is_never_announced_late(h):
    add_guild(h.db_path, 1, ping="everyone")
    old = page(main_table(("1 Golden Key", "Aug 1, 2026", X, "Unknown")))
    await h.run(await read_named(old, T0))
    assert statuses(h.db_path) == {X: "too_old"}

    # The site "refreshes" the row: it's now added today. The bot already knows the code.
    h.clock = T0 + timedelta(hours=1)
    fresh = page(main_table(("1 Golden Key", d(h.clock.date()), X, "Unknown")))
    assert await h.run(await read_named(fresh, h.clock)) == 0

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}
    assert details_of(h.db_path, X).added_on == h.clock.date()


async def test_a_row_whose_added_date_moves_back_is_never_announced_twice(h):
    add_guild(h.db_path, 1, ping="everyone")
    fresh = page(main_table(("1 Golden Key", d(T0.date()), X, "Unknown")))
    await h.run(await read_named(fresh, T0))
    assert h.original_codes(1) == [X]

    for hours, added in ((1, "Jan 5, 2026"), (2, d(T0.date())), (3, "Unknown")):
        h.clock = T0 + timedelta(hours=hours)
        html = page(main_table(("1 Golden Key", added, X, "Unknown")))
        await h.run(await read_named(html, h.clock))

    assert h.original_codes(1) == [X]
    assert h.pings(1) == []


async def test_a_code_moving_from_the_main_table_to_permanent_is_not_new(h):
    add_guild(h.db_path, 1, ping="everyone")
    main = page(main_table(("3 Golden Keys", d(T0.date()), X, "Oct 20, 2026")))
    await h.run(await read_named(main, T0))
    assert h.original_codes(1) == [X]
    assert details_of(h.db_path, X).category == "golden"

    h.clock = T0 + timedelta(hours=1)
    moved = page(permanent_table(("Skin: Sunrise", "Stream", X)))
    assert await h.run(await read_named(moved, h.clock)) == 0

    assert len(h.alerts(1)) == 1  # the original post, and nothing since
    row = details_of(h.db_path, X)
    # The newer table reading wins its fields; a missing expiry never erases a present one.
    assert row.category == "permanent"
    assert row.reward == "Skin: Sunrise"
    assert row.expires_on == date(2026, 10, 20)
    assert row.details_changed_at == h.clock  # an alert edit is due: the reward changed
    assert is_current(row, first_seen(h.db_path, X), now=h.clock + timedelta(days=400))


async def test_a_code_moving_to_permanent_and_back_does_not_post_or_follow_up(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run(await read_named(page(permanent_table(("Skin", "Stream", X))), T0))
    for hours, html in (
        (1, page(main_table(("3 Golden Keys", d(T0.date()), X, "Unknown")))),
        (2, page(permanent_table(("Skin", "Stream", X)))),
        (3, page(cosmetic_table(("Skin", "Event", X, "Oct 30, 2026")))),
    ):
        await h.run(
            await read_named(html, T0 + timedelta(hours=hours)), at=T0 + timedelta(hours=hours)
        )

    assert h.alerts(1) == []
    assert followup_rows(h.db_path) == []


# =====================================================================
# Expired on arrival
# =====================================================================


@pytest.mark.parametrize(
    ("clock", "expires", "expected"),
    [
        # 19:00 Pacific on Oct 8 (02:00 UTC Oct 9): Oct 8 is still today where the code works.
        (datetime(2026, 10, 9, 2, tzinfo=UTC), "Oct 8, 2026", "posted"),
        (datetime(2026, 10, 9, 2, tzinfo=UTC), "Oct 7, 2026", "too_old"),
        # 01:00 Pacific on Oct 9 (08:00 UTC): Oct 8 is over in both zones.
        (datetime(2026, 10, 9, 8, tzinfo=UTC), "Oct 8, 2026", "too_old"),
        (datetime(2026, 10, 9, 8, tzinfo=UTC), "Oct 9, 2026", "posted"),
        # The clocks go back on Nov 1 2026 (09:00 UTC); Nov 2 00:00 PST is 08:00 UTC.
        (datetime(2026, 11, 2, 7, 30, tzinfo=UTC), "Nov 1, 2026", "posted"),
        (datetime(2026, 11, 2, 8, 30, tzinfo=UTC), "Nov 1, 2026", "too_old"),
    ],
)
async def test_expiry_is_the_end_of_the_pacific_day(h, clock, expires, expected):
    add_guild(h.db_path, 1)
    h.clock = clock
    added = d(clock.astimezone(SHIFT_TZ).date())

    await h.run(await read_named(page(main_table(("1 Golden Key", added, X, expires))), clock))

    assert statuses(h.db_path) == {X: expected}


async def test_an_expiry_from_a_different_source_in_the_same_pass_keeps_it_quiet(h):
    add_guild(h.db_path, 1, ping="everyone")
    # Reddit found X (fresh, no details); mentalmars, same pass, says it died yesterday.
    gone = page(main_table(("1 Golden Key", "Aug 1, 2026", X, "Oct 8, 2026")))
    items = [reddit_item(X, at=T0), *await read_named(gone, T0)]

    assert await h.run(items) == 0

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(0,)]
    assert followup_rows(h.db_path) == []


async def test_a_tables_expiry_typo_cannot_silence_gearboxs_own_announcement(h):
    """Gearbox names the code; the table says it expired. Gearbox wins (owner, 2026-10-09)."""
    add_guild(h.db_path, 1, ping="everyone")
    gone = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 1, 2026")))
    items = [official(f"new code {X}", at=T0 - timedelta(hours=1)), *await read_named(gone, T0)]

    await h.run(items)

    assert h.original_codes(1) == [X]
    assert [a.ping for a in h.originals(1)] == [True]
    assert statuses(h.db_path) == {X: "posted"}


async def test_an_expiry_learned_after_release_does_not_unpost_and_hides_the_code(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([official(f"new code {X}", at=T0 - timedelta(hours=1))])
    assert h.original_codes(1) == [X] and h.pings(1)
    posts = rows(h.db_path, "SELECT * FROM guild_code_posts")

    h.clock = T0 + timedelta(hours=2)
    gone = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 8, 2026")))
    assert await h.run(await read_named(gone, h.clock)) == 0

    # Posted stays posted; nothing new went out; the details say it is over.
    assert statuses(h.db_path) == {X: "posted"}
    assert rows(h.db_path, "SELECT * FROM guild_code_posts") == posts
    assert [a for a in h.alerts(1)] == h.originals(1) and len(h.alerts(1)) == 1
    row = details_of(h.db_path, X)
    assert row.expires_on == date(2026, 10, 8)
    assert not is_current(row, first_seen(h.db_path, X), now=h.clock)
    assert is_current(row, first_seen(h.db_path, X), now=datetime(2026, 10, 8, 20, tzinfo=UTC))


async def test_a_permanent_code_is_never_expired_on_arrival(h):
    add_guild(h.db_path, 1)
    html = page(
        main_table(("1 Golden Key", d(T0.date()), X, "Oct 1, 2026")),
        permanent_table(("Skin: Sunrise", "Stream", X)),
    )
    items = await read_named(html, T0)
    assert items[0].details.category == "permanent" and items[0].details.expires_on is not None

    await h.run(items)

    assert statuses(h.db_path) == {X: "posted"}


async def test_a_code_the_table_says_is_already_dead_earns_no_pinged_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0)])  # someone reposts an old code; nobody knows yet
    assert h.original_codes(1) == [X]

    h.clock = T0 + timedelta(hours=2)
    dead = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 8, 2026")))
    await h.run(await read_named(dead, h.clock))

    assert h.pings(1) == [], "@everyone was pinged to confirm a code that expired yesterday"


async def test_a_code_recorded_quietly_for_its_expiry_stays_quiet_if_the_expiry_is_corrected(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run(await read_named(page(main_table(("Key", d(T0.date()), X, "Oct 1, 2026"))), T0))
    assert statuses(h.db_path) == {X: "too_old"}

    h.clock = T0 + timedelta(hours=1)
    corrected = page(main_table(("Key", d(T0.date()), X, "Oct 30, 2026")))
    assert await h.run(await read_named(corrected, h.clock)) == 0

    assert h.alerts(1) == []
    assert details_of(h.db_path, X).expires_on == date(2026, 10, 30)


async def test_an_official_post_can_pull_the_expiry_into_the_past_for_a_new_code(h):
    add_guild(h.db_path, 1, ping="everyone")
    table = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 30, 2026")))
    # Posted on the evening of Oct 7 (Pacific) saying it's good through that day: a deadline
    # that is already over by the time this pass reads it. (QA batch 1, fix 1: a post can no
    # longer announce a date before its own day, so "valid until 10/01/2026" on Oct 9 is a
    # misread now, not an expiry.)
    post = official(f"{X} valid until 10/07/2026", at=T0 - timedelta(hours=30))

    assert await h.run([post, *await read_named(table, T0)]) == 0

    # Gearbox outranks the table on the field it speaks to, and an expired code is quiet.
    row = details_of(h.db_path, X)
    assert (row.expires_on, row.expires_from) == (date(2026, 10, 7), GEARBOX)
    assert statuses(h.db_path) == {X: "too_old"}
    assert h.alerts(1) == []


async def test_an_official_post_that_names_a_code_but_no_expiry_cannot_erase_the_tables(h):
    add_guild(h.db_path, 1)
    table = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 30, 2026")))
    await h.run(await read_named(table, T0))
    before = details_of(h.db_path, X)

    h.clock = T0 + timedelta(hours=1)
    await h.run([official(f"{X} is live, go go go", at=h.clock)])

    after = details_of(h.db_path, X)
    assert (after.expires_on, after.expires_from) == (before.expires_on, before.expires_from)
    assert after.reward == before.reward


# =====================================================================
# B+ and the ping budget
# =====================================================================


def _confirm_page(at: datetime, code: str = X, expires: str = "Oct 30, 2026") -> str:
    return page(main_table(("3 Golden Keys", d(T0.date()), code, expires)))


@pytest.mark.parametrize(
    ("delay", "followup"),
    [
        (timedelta(hours=23, minutes=59), True),
        (timedelta(hours=24), True),
        (timedelta(hours=24, minutes=1), False),
    ],
    ids=["23h59m", "exactly 24h", "24h01m"],
)
async def test_mentalmars_earns_a_followup_only_inside_the_24_hour_window(h, delay, followup):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0)])
    assert h.pings(1) == []

    h.clock = T0 + delay
    await h.run(await read_named(_confirm_page(h.clock), h.clock))

    assert len(h.followups(1)) == int(followup)
    if followup:
        assert h.followups(1)[0].ping is True
        assert ping_count(h.db_path, 1) == 1
    else:
        assert followup_rows(h.db_path) == []
        assert ping_count(h.db_path, 1) == 0


async def test_the_followup_is_exactly_one_however_long_the_page_keeps_saying_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0)])
    html = _confirm_page(T0)
    for hours in range(1, 60):
        h.clock = T0 + timedelta(hours=hours)
        items = await read_named(html, h.clock)
        if hours < 30:
            items.append(reddit_item(X, at=h.clock))  # Reddit keeps repeating itself too
        await h.run(items)

    assert len(h.followups(1)) == 1
    assert len(h.originals(1)) == 1
    assert len(h.pings(1)) == 1
    assert h.pings(1)[0] is h.followups(1)[0]


async def test_mentalmars_and_reddit_together_with_the_cap_spent_post_unpinged_and_never_follow_up(
    h,
):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-09", ping_count=3)
    add_guild(h.db_path, 2, ping="everyone", ping_day="2026-10-09", ping_count=2)
    items = [reddit_item(X, at=T0), *await read_named(_confirm_page(T0), T0)]

    assert await h.run(items) == 1

    # Guild 1 had no ping left: the post goes out plain. Guild 2 spends its last one.
    assert [a.ping for a in h.sent(1)] == [False]
    assert [a.ping for a in h.sent(2)] == [True]
    assert ping_count(h.db_path, 1) == 3 and ping_count(h.db_path, 2) == 3
    assert any("cap reached" in text for g, text in h.notices if g == 1)

    # Keep the page up for a day and a half, across the cap's reset: still no second ping for X.
    for hours in range(1, 40):
        await h.run(
            await read_named(_confirm_page(T0), T0 + timedelta(hours=hours)),
            at=T0 + timedelta(hours=hours),
        )
    assert h.pings(1) == []
    assert len(h.pings(2)) == 1
    assert followup_rows(h.db_path) == []


async def test_a_followup_that_finds_the_cap_spent_is_skipped_for_good(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-09", ping_count=3)
    await h.run([reddit_item(X, at=T0)])
    assert h.sent(1)[0].ping is False

    h.clock = T0 + timedelta(hours=2)
    await h.run(await read_named(_confirm_page(h.clock), h.clock))

    assert h.followups(1) == []
    assert followup_rows(h.db_path) == [(1, X, "skipped", 0)]
    assert ping_count(h.db_path, 1) == 3

    # Tomorrow the budget is back. The skipped follow-up is not revived.
    for hours in (22, 23, 30):
        h.clock = T0 + timedelta(hours=hours)
        await h.run(await read_named(_confirm_page(h.clock), h.clock))
    assert h.pings(1) == []
    assert followup_rows(h.db_path) == [(1, X, "skipped", 0)]


async def test_a_code_found_by_mentalmars_and_confirmed_by_bluesky_gets_one_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run(await read_named(_confirm_page(T0), T0))
    assert h.original_codes(1) == [X] and h.pings(1) == []

    h.clock = T0 + timedelta(hours=3)
    await h.run([community(X, at=h.clock, name=BLUESKY)])
    assert len(h.followups(1)) == 1
    # Task 7: mentalmars' row gave the code details, and the follow-up shows them.
    assert h.followups(1)[0].content == (
        f"@everyone `> {FOLLOWUP_TEXT} ▌`\n\n```\n{X}\n```\n`reward` 3 Golden Keys"
        " · `expires` Oct 30, 2026 (<t:1793430000:R>) · `via` mentalmars"
    )

    # A third source, and a fourth, change nothing.
    for hours, name in ((4, REDDIT), (5, "Another Skeet Collector")):
        h.clock = T0 + timedelta(hours=hours)
        await h.run([community(X, at=h.clock, name=name)])
    assert len(h.followups(1)) == 1 and len(h.pings(1)) == 1


async def test_a_roundup_code_listed_by_mentalmars_earns_no_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    megathread = RawItem(
        url="https://example.com/megathread",
        title="Borderlands 4 SHiFT code megathread: " + " ".join(code_n(i) for i in range(1, 8)),
        excerpt="",
        source_name=REDDIT,
        trust="community",
        published_at=T0 - timedelta(hours=1),
    )
    await h.run([megathread])
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1") == [(7,)]

    h.clock = T0 + timedelta(hours=2)
    listed = page(main_table(("3 Golden Keys", d(T0.date()), code_n(1), "Oct 30, 2026")))
    await h.run(await read_named(listed, h.clock))
    # Even a normal-post mention from yet another source can't confirm a roundup post.
    h.clock = T0 + timedelta(hours=3)
    await h.run([community(code_n(1), at=h.clock, name=BLUESKY)])

    assert followup_rows(h.db_path) == []
    assert h.pings(1) == []
    assert details_of(h.db_path, code_n(1)).reward == "3 Golden Keys"  # details still land


async def test_a_case_variant_of_a_source_name_is_not_a_second_opinion(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0, source="r/Borderlands4")])

    h.clock = T0 + timedelta(hours=2)
    await h.run([reddit_item(X, at=h.clock, source="R/BORDERLANDS4")])

    assert followup_rows(h.db_path) == []


async def test_mentalmars_renamed_only_in_case_is_still_one_source_for_b_plus(h):
    """A pinned judgment: casing doesn't make a second opinion, but it does reset the first
    listing (the marker is per exact name), so an undated newcomer is quiet that one pass."""
    add_guild(h.db_path, 1, ping="everyone")
    await h.run(await read_named(_confirm_page(T0), T0, "mentalmars"))
    assert h.original_codes(1) == [X]

    h.clock = T0 + timedelta(hours=2)
    html = page(
        main_table(("3 Golden Keys", d(T0.date()), X, "Oct 30, 2026")),
        cosmetic_table(("Hat", "Event", Y, "Unknown")),
    )
    await h.run(await read_named(html, h.clock, "Mentalmars"))

    assert followup_rows(h.db_path) == []
    assert h.original_codes(1) == [X]
    assert statuses(h.db_path)[Y] == "too_old"


async def test_renaming_mentalmars_inside_a_codes_window_counts_as_a_second_source(h):
    """Pinned limitation, not a verdict: B+ can't tell a rename from a new site.

    A code mentalmars found at T0 and the same page under a new name at T0+2h looks like
    two independent sources, so it earns the pinged follow-up. One ping, never a repeat,
    and only if the owner renames within a day of a code. Reported, not fixed here.
    """
    add_guild(h.db_path, 1, ping="everyone")
    await h.run(await read_named(_confirm_page(T0), T0, "mentalmars"))

    h.clock = T0 + timedelta(hours=2)
    await h.run(await read_named(_confirm_page(h.clock), h.clock, "mentalmars.com"))

    assert len(h.followups(1)) == 1
    h.clock = T0 + timedelta(hours=3)
    await h.run(await read_named(_confirm_page(h.clock), h.clock, "mentalmars.com"))
    assert len(h.followups(1)) == 1


async def test_a_pinged_official_code_never_earns_a_followup_from_the_table(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([official(f"new code {X}", at=T0 - timedelta(hours=1))])
    assert [a.ping for a in h.sent(1)] == [True]

    h.clock = T0 + timedelta(hours=2)
    await h.run(await read_named(_confirm_page(h.clock), h.clock))

    assert h.followups(1) == []
    assert ping_count(h.db_path, 1) == 1


async def test_a_guild_with_pings_off_gets_no_followup_from_a_confirmation(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([reddit_item(X, at=T0)])
    h.clock = T0 + timedelta(hours=2)
    await h.run(await read_named(_confirm_page(h.clock), h.clock))

    assert h.pings(1) == [] and h.followups(1) == []
    assert followup_rows(h.db_path) == []


async def test_followups_spend_the_daily_ping_budget_and_the_fourth_is_skipped(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = [code_n(50 + i) for i in range(4)]
    for i, code in enumerate(codes):
        h.clock = T0 + timedelta(minutes=10 * i)
        await h.run([reddit_item(code, at=h.clock)])
    assert h.pings(1) == []

    # Four separate passes, four confirmations, one day: three pings and a polite silence.
    for i, code in enumerate(codes):
        h.clock = T0 + timedelta(hours=1, minutes=10 * i)
        await h.run(await read_named(_confirm_page(h.clock, code), h.clock))

    assert [a.codes for a in h.followups(1)] == [[c] for c in codes[:3]]
    assert ping_count(h.db_path, 1) == 3
    assert [r[2] for r in followup_rows(h.db_path)] == ["posted", "posted", "posted", "skipped"]


async def test_several_confirmations_in_one_pass_share_one_pinged_message_and_one_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = [code_n(60 + i) for i in range(4)]
    await h.run([reddit_item(c, at=T0) for c in codes])
    assert h.pings(1) == []

    h.clock = T0 + timedelta(hours=1)
    html = page(main_table(*[("Key", d(T0.date()), c, "Oct 30, 2026") for c in codes]))
    await h.run(await read_named(html, h.clock))

    assert [a.codes for a in h.followups(1)] == [codes]
    assert ping_count(h.db_path, 1) == 1


@pytest.mark.parametrize("official_first", [True, False])
async def test_gearbox_outranks_mentalmars_in_one_pass_whichever_spoke_last(h, official_first):
    add_guild(h.db_path, 1)
    # The official post is older; the table's "added" date is later. Neither order matters.
    post = official(f"{X} valid until 10/20/2026", at=T0 - timedelta(hours=5))
    table = await read_named(
        page(main_table(("3 Golden Keys", d(T0.date()), X, "Oct 25, 2026"))), T0
    )

    await h.run([post, *table] if official_first else [*table, post])

    row = details_of(h.db_path, X)
    assert (row.expires_on, row.expires_from) == (date(2026, 10, 20), GEARBOX)
    assert (row.reward, row.reward_from) == ("3 Golden Keys", MENTALMARS)


async def test_an_unchanged_page_never_flags_an_alert_for_an_edit(h):
    add_guild(h.db_path, 1)
    html = page(
        main_table(("3 Golden Keys", d(T0.date()), X, "Oct 30, 2026")),
        cosmetic_table(("Gnome", "Event", Y, "Unknown")),
    )
    await h.run(await read_named(html, T0))
    stamps = rows(h.db_path, "SELECT code, details_changed_at FROM code_details ORDER BY code")
    assert stamps

    for hours in range(1, 8):
        await h.run(
            await read_named(html, T0 + timedelta(hours=hours)), at=T0 + timedelta(hours=hours)
        )

    assert (
        rows(h.db_path, "SELECT code, details_changed_at FROM code_details ORDER BY code") == stamps
    )
    assert rows(h.db_path, "SELECT COUNT(*) FROM code_details") == [(2,)]


# =====================================================================
# The canonical batch
# =====================================================================


def _row(code: str, url: str, name: str = MENTALMARS) -> RawItem:
    return RawItem(
        url=url,
        title=code,
        excerpt=code,
        source_name=name,
        trust="community",
        published_at=None,
        topics=("borderlands4",),
        details=ItemDetails(code=code, rank="table", listed=True),
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "mailto:codes@example.com",
        "file:///etc/passwd",
        "data:text/html,<b>x</b>",
        "//codes.example/p",
        "codes.example/p",
        "",
        "   ",
        "ftp://codes.example/p",
    ],
)
async def test_a_table_row_with_a_url_that_is_not_http_is_dropped_and_leaves_no_trace(h, url):
    add_guild(h.db_path, 1, ping="everyone")
    assert _canonical_batch([_row(X, url)]) == []

    assert await h.run([_row(X, url)]) == 0

    assert statuses(h.db_path) == {}
    assert rows(h.db_path, "SELECT COUNT(*) FROM code_details") == [(0,)]
    assert listed_markers(h.db_path) == []
    assert h.alerts(1) == []


def test_http_rows_with_odd_cases_and_fragments_all_survive_one_by_one():
    batch = _canonical_batch(
        [
            _row(X, "HTTPS://Codes.Example/p/#a"),
            _row(Y, "https://codes.example/p/?utm_medium=x#b"),
            _row(Z, "http://codes.example/p"),
        ]
    )
    assert [i.title for i in batch] == [X, Y, Z]
    assert all(i.url.startswith("http") and "#" not in i.url for i in batch)


async def test_a_plain_item_sharing_the_tables_url_does_not_eat_or_get_eaten(h):
    add_guild(h.db_path, 1, ping="everyone")
    table = await read_named(page(main_table(("1 Golden Key", d(T0.date()), X, "Unknown"))), T0)
    plain = RawItem(
        url=PAGE_URL + "?utm_source=feed",
        title=f"Borderlands 4 SHiFT code {Y}",
        excerpt="",
        source_name="Some Press",
        trust="press",
        published_at=T0 - timedelta(hours=1),
    )

    assert await h.run([plain, *table]) == 2

    assert sorted(h.original_codes(1)) == sorted([X, Y])
    assert details_of(h.db_path, X) is not None
    assert details_of(h.db_path, Y) is None  # a plain item carries no details


async def test_an_off_topic_plain_item_cannot_borrow_the_tables_game_match(h):
    add_guild(h.db_path, 1, ping="everyone")
    table = await read_named(page(main_table(("1 Golden Key", d(T0.date()), X, "Unknown"))), T0)
    off_topic = RawItem(
        url=PAGE_URL,
        title=f"Fortnite promo code {Y}",
        excerpt="Redeem in the Fortnite item shop.",
        source_name="Some Press",
        trust="press",
        published_at=T0 - timedelta(hours=1),
    )

    await h.run([off_topic, *table])

    assert h.original_codes(1) == [X], "an off-topic item announced a code for Borderlands 4"


async def test_two_sources_listing_the_same_code_in_one_pass_agree_and_ping_once(h):
    add_guild(h.db_path, 1, ping="everyone")
    day = d(T0.date())
    a = await read_named(page(main_table(("3 Golden Keys", day, X, "Oct 30, 2026"))), T0)
    b = await read_named(
        page(main_table(("1 Golden Key", day, X, "Oct 31, 2026"))),
        T0,
        OTHER_SITE,
        "https://other.example/bl4-codes/",
    )

    assert await h.run([*a, *b]) == 1

    # Two names, one pass: trusted by the B+ shortcut, so the first post pings. Once.
    assert [a.ping for a in h.sent(1)] == [True]
    assert ping_count(h.db_path, 1) == 1
    row = details_of(h.db_path, X)
    # Same rank, so the later reading wins; both observations are undated by the page, so
    # input order decides. The assertion is that *a* table reading won, whole.
    assert (row.reward, row.expires_on) in {
        ("3 Golden Keys", date(2026, 10, 30)),
        ("1 Golden Key", date(2026, 10, 31)),
    }
    assert row.reward_from in {MENTALMARS, OTHER_SITE}
    assert followup_rows(h.db_path) == []

    h.clock = T0 + timedelta(hours=1)
    await h.run([*a, *b])
    assert len(h.alerts(1)) == 1


async def test_5000_rows_through_the_hook_fit_the_budget_when_quiet(h):
    add_guild(h.db_path, 1, ping="everyone")
    items = synthetic_rows(2500, prefix="M", added=date(2026, 8, 1)) + synthetic_rows(
        2500, prefix="C", added=None
    )
    assert len(items) == 5000
    h.clock = LATER

    started = time.perf_counter()
    released = await h.run(items)
    elapsed = time.perf_counter() - started

    assert released == 0 and h.alerts(1) == []
    assert len(statuses(h.db_path)) == 5000
    assert elapsed < 30, f"5,000 quiet rows took {elapsed:.1f}s of the hook's 120s"

    # And the hourly repeat, the common case, is cheaper still.
    h.clock = LATER + timedelta(hours=1)
    started = time.perf_counter()
    assert await h.run(items) == 0
    assert time.perf_counter() - started < 30


async def test_5000_fresh_rows_are_released_inside_the_budget_and_delivered(h):
    """A hostile or glitching page: 5,000 new, fresh, dated rows. Nothing caps table rows."""
    add_guild(h.db_path, 1, ping="everyone")
    items = synthetic_rows(5000, prefix="F", added=T0.date())

    started = time.perf_counter()
    released = await h.run(items)
    elapsed = time.perf_counter() - started

    # A flood of table rows goes out the roundup way: the cap of 50, the rest recorded quietly.
    assert released == 50
    assert elapsed < 60, f"releasing and delivering 5,000 rows took {elapsed:.1f}s"
    assert len(statuses(h.db_path)) == 5000
    assert sorted(set(statuses(h.db_path).values())) == ["posted", "roundup"]
    codes = h.original_codes(1)
    assert len(codes) == len(set(codes)) == 50
    assert h.pings(1) == []  # one community source: never a ping, however many rows


# =====================================================================
# Crash safety
# =====================================================================

STEPS = [
    "record_code_sightings",
    "upsert_code_details",
    "table_kinds_listed",
    "code_details_for",
    "plan_alerts",
    "record_silent_codes",
    "record_released_codes",
    "queue_confirmed_followups",
    "mark_table_listed",
]

RC = code_n(900)  # a community code posted unpinged two hours before the scenario
NEW_DATED = code_n(901)  # fresh dated row, new
NEW_EXPIRED = code_n(902)  # fresh dated row that already expired
UNDATED = [code_n(910), code_n(911), code_n(912)]  # first-listing bookshelf
REDDIT_NEW = code_n(903)


def _scenario_page(at: datetime) -> str:
    day = d(at.date())
    return page(
        main_table(
            ("3 Golden Keys", day, RC, "Oct 30, 2026"),
            ("1 Golden Key", day, NEW_DATED, "Oct 31, 2026"),
            ("1 Golden Key", day, NEW_EXPIRED, "Oct 1, 2026"),
            ("Old key", "Jul 4, 2026", code_n(904), "Unknown"),
        ),
        cosmetic_table(*[("Gnome", "Event", c, "Unknown") for c in UNDATED[:2]]),
        permanent_table(("Skin", "Stream", UNDATED[2])),
    )


async def _pass(h: Ledger, at: datetime) -> int:
    items = [
        reddit_item(REDDIT_NEW, at=T0 - timedelta(hours=1)),
        *await read_named(_scenario_page(T0), at),
    ]
    return await h.run(items, at=at)


def _projection(h: Ledger):
    db = h.db_path
    return {
        "alerted": rows(
            db, "SELECT code, status, pinged, from_roundup FROM alerted_codes ORDER BY code"
        ),
        "posts": rows(
            db,
            "SELECT guild_id, code, status, pinged, followup_ok "
            "FROM guild_code_posts ORDER BY guild_id, code",
        ),
        "followups": followup_rows(db),
        "pings": rows(db, "SELECT guild_id, ping_count FROM guild_shift ORDER BY guild_id"),
        "details": rows(
            db,
            "SELECT code, reward, reward_from, category, expires_on, expires_from, "
            "added_on, origin "
            "FROM code_details ORDER BY code",
        ),
        "markers": listed_markers(db),
        "sent": sorted(
            (g, tuple(a.codes), a.ping, FOLLOWUP_TEXT in a.content)
            for g, entries in h.history.items()
            for _, a in entries
        ),
    }


async def _prepared(db_path: str) -> Ledger:
    harness = Ledger(db_path, load_config(V3), T0 - timedelta(hours=2))
    seed(db_path)
    add_guild(db_path, 1, ping="everyone")
    add_guild(db_path, 2, ping="none")
    add_guild(db_path, 3, ping="1234567890123456789")
    # RC went out unpinged two hours ago, from Reddit, in a real earlier pass.
    await harness.run([reddit_item(RC, at=T0 - timedelta(hours=3))], at=T0 - timedelta(hours=2))
    return harness


async def test_a_clean_run_of_the_crash_scenario_does_what_the_scenario_claims(tmp_path):
    path = str(tmp_path / "clean.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    h = await _prepared(path)
    await _pass(h, T0)

    got = _projection(h)
    status = dict(got["alerted"] and [(r[0], r[1]) for r in got["alerted"]])
    assert status[RC] == "posted"
    assert status[NEW_DATED] == "posted" and status[REDDIT_NEW] == "posted"
    assert status[NEW_EXPIRED] == "too_old" and status[code_n(904)] == "too_old"
    assert all(status[c] == "too_old" for c in UNDATED)
    # RC's one follow-up went to the pinging servers (1 everyone, 3 role), not to "none".
    assert sorted((r[0], r[1]) for r in got["followups"]) == [(1, RC), (3, RC)]
    assert got["markers"] == [MENTALMARS]


@pytest.mark.parametrize("step", STEPS)
async def test_a_crash_at_every_step_converges_to_the_clean_runs_final_state(
    tmp_path, monkeypatch, step
):
    clean_path = str(tmp_path / "clean.db")
    crash_path = str(tmp_path / "crash.db")
    for path in (clean_path, crash_path):
        with closing(connect(path)) as conn:
            migrate(conn)

    clean = await _prepared(clean_path)
    for hours in (0, 1, 2):
        await _pass(clean, T0 + timedelta(hours=hours))

    crashed = await _prepared(crash_path)
    real = getattr(fanout, step)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"a test, not a prophecy ({step})")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, step, boom)
    with pytest.raises(RuntimeError, match="not a prophecy"):
        await _pass(crashed, T0)
    for hours in (1, 2):
        await _pass(crashed, T0 + timedelta(hours=hours))

    assert calls["n"] >= 2, f"{step} was never reached a second time"
    a, b = _projection(clean), _projection(crashed)
    for key in a:
        assert b[key] == a[key], f"{key} differs after a crash in {step}"
    # And, because the invariant is the owner's: no (guild, code) ever went out twice.
    for guild_id in (1, 2, 3):
        sent = crashed.original_codes(guild_id)
        assert len(sent) == len(set(sent)), (step, guild_id, sent)
        follow = [c for a_ in crashed.followups(guild_id) for c in a_.codes]
        assert len(follow) == len(set(follow))


@pytest.mark.parametrize(
    "step",
    ["record_code_sightings", "upsert_code_details", "record_silent_codes", "mark_table_listed"],
)
async def test_a_crash_during_the_unseeded_first_pass_still_seeds_everything_quietly(
    tmp_path, monkeypatch, step
):
    path = str(tmp_path / "unseeded.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    harness = Ledger(path, load_config(V3), LATER)  # not seeded
    add_guild(path, 1, ping="everyone")
    real = getattr(fanout, step)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a test, not a prophecy")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, step, boom)
    with pytest.raises(RuntimeError, match="not a prophecy"):
        await harness.run(await read_named(FIXTURE.read_bytes(), LATER))
    harness.clock = LATER + timedelta(hours=1)
    assert await harness.run(await read_named(FIXTURE.read_bytes(), harness.clock)) == 0

    assert harness.alerts(1) == []
    assert set(statuses(path).values()) == {"seeded"} and len(statuses(path)) == 11
    assert rows(path, "SELECT 1 FROM alert_state WHERE key = 'seeded_at'")
    assert listed_markers(path) == [MENTALMARS]


async def test_a_crash_after_release_loses_neither_the_queue_nor_the_ping_budget(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "q.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    h = await _prepared(path)
    real = fanout.mark_table_listed
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("late crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, "mark_table_listed", boom)
    with pytest.raises(RuntimeError):
        await _pass(h, T0)
    queued = rows(path, "SELECT guild_id, code FROM guild_code_posts WHERE status = 'queued'")
    assert {c for _, c in queued} == {NEW_DATED, REDDIT_NEW}
    assert ping_count(path, 1) == 0  # nothing was claimed, nothing spent

    await _pass(h, T0 + timedelta(hours=1))
    assert sorted(h.original_codes(1)) == sorted([RC, NEW_DATED, REDDIT_NEW])
    assert ping_count(path, 1) == 1  # only RC's follow-up


# =====================================================================
# Validators and the commit point
# =====================================================================

TABLE_PAGE_ETAG = '"v1"'


class _EtagPage:
    def __init__(self, html: str) -> None:
        self.html = html
        self.if_none_match: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("If-None-Match")
        self.if_none_match.append(sent)
        if sent == TABLE_PAGE_ETAG:
            return httpx.Response(304, headers={"ETag": TABLE_PAGE_ETAG})
        return httpx.Response(200, content=self.html.encode(), headers={"ETag": TABLE_PAGE_ETAG})


def _wired(h: Ledger, handler, validators: PageValidators, alerts: list[str]):
    """run_collection deps with the real hook, over one MockTransport table page."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = ShiftTableCollector(
        source(), validators=validators, sleep=_noop_sleep, clock=lambda: h.clock
    )
    deps = make_deps(
        h.cfg,
        h.db_path,
        client,
        [collector],
        alerts,
        page_validators=validators,
        shift_hook=make_shift_hook(h.deps()),
    )
    return client, deps


async def test_a_failing_details_upsert_leaves_validators_uncommitted_and_the_next_pass_recovers(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="everyone")
    h.clock = T0
    html = page(main_table(("3 Golden Keys", d(T0.date()), X, "Oct 30, 2026")))
    handler = _EtagPage(html)
    validators = PageValidators()
    alerts: list[str] = []
    client, deps = _wired(h, handler, validators, alerts)
    real = fanout.upsert_code_details
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the details store fell over")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, "upsert_code_details", boom)
    async with client:
        await run_collection(deps)
        h.harvest()
        assert validators == {}, "the validators were committed though nothing was saved"
        assert h.original_codes(1) == []
        assert any("SHiFT detection failed" in a for a in alerts)

        await run_collection(deps)  # not a 304: a full fetch, so the row gets read again
        h.harvest()
        assert handler.if_none_match[:2] == [None, None]
        assert validators.keys() == {PAGE_URL} and validators[PAGE_URL].etag == TABLE_PAGE_ETAG
        assert h.original_codes(1) == [X]

        await run_collection(deps)  # now it is conditional, and a 304 changes nothing
        h.harvest()
        assert handler.if_none_match[2] == TABLE_PAGE_ETAG or handler.if_none_match[2] is None
    assert h.original_codes(1) == [X]
    assert len(h.pings(1)) == 0


# =====================================================================
# Concurrency
# =====================================================================


async def test_a_reader_holding_a_snapshot_never_blocks_the_hook_or_sees_half_of_it(h):
    add_guild(h.db_path, 1)
    html = page(
        main_table(*[("1 Golden Key", d(T0.date()), code_n(i), "Oct 30, 2026") for i in range(60)])
    )
    reader = connect(h.db_path)
    try:
        reader.execute("BEGIN")
        before = reader.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0]
        details_before = reader.execute("SELECT COUNT(*) FROM code_details").fetchone()[0]

        released = await asyncio.wait_for(h.run(await read_named(html, T0)), timeout=30)

        assert released == 50  # sixty fresh rows from one table is a flood: the roundup cap
        assert reader.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == before
        assert reader.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == details_before
        reader.execute("COMMIT")
        assert reader.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 60
        assert reader.execute("SELECT COUNT(*) FROM code_details").fetchone()[0] == 60
    finally:
        reader.close()


async def test_readers_polling_during_the_hook_see_consistent_counts_and_no_errors(h):
    add_guild(h.db_path, 1)
    html = page(
        main_table(*[("1 Golden Key", d(T0.date()), code_n(i), "Unknown") for i in range(300)])
    )
    items = await read_named(html, T0)
    errors: list[BaseException] = []
    seen: list[int] = []
    stop = threading.Event()

    def poll() -> None:
        try:
            with closing(connect(h.db_path)) as conn:
                while not stop.is_set():
                    seen.append(len(repo.history_codes(conn, T0 - timedelta(days=90))))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        assert await h.run(items) == 50  # a flood goes out the roundup way, capped at 50
    finally:
        stop.set()
        thread.join(timeout=10)

    assert errors == []
    assert seen == sorted(seen), "the count went backwards under a reader"
    assert not seen or seen[-1] <= 300


async def test_a_second_pass_while_the_first_is_in_the_hook_is_skipped_and_nothing_doubles(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.clock = T0
    html = page(main_table(("3 Golden Keys", d(T0.date()), X, "Oct 30, 2026")))
    handler = _EtagPage(html)
    gate = asyncio.Event()
    entered = asyncio.Event()
    real_hook = make_shift_hook(h.deps())
    alerts: list[str] = []
    validators = PageValidators()
    client, deps = _wired(h, handler, validators, alerts)

    async def slow_hook(items, results):
        entered.set()
        await gate.wait()
        return await real_hook(items, results)

    deps.shift_hook = slow_hook
    async with client:
        first = asyncio.create_task(run_collection(deps))
        await asyncio.wait_for(entered.wait(), timeout=10)
        second = await run_collection(deps)
        assert second.skipped
        gate.set()
        await first
        h.harvest()

    assert _run_lock.locked() is False
    assert h.original_codes(1) == [X]
    assert len(handler.if_none_match) == 1  # the skipped pass never even fetched


async def test_two_hooks_that_somehow_overlap_still_release_and_post_each_code_once(tmp_path):
    path = str(tmp_path / "overlap.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    seed(path)
    add_guild(path, 1, ping="everyone")
    add_guild(path, 2, ping="everyone")
    first = Ledger(path, load_config(V3), T0)
    second = Ledger(path, load_config(V3), T0)
    day = d(T0.date())
    table = await read_named(
        page(main_table(*[("Key", day, code_n(i), "Unknown") for i in range(40)])), T0
    )
    items = [reddit_item(code_n(0), at=T0), reddit_item(code_n(1), at=T0), *table]

    await asyncio.gather(first.run(items), second.run(items))

    for guild_id in (1, 2):
        sent = first.original_codes(guild_id) + second.original_codes(guild_id)
        assert len(sent) == len(set(sent)) == 40, (guild_id, len(sent), len(set(sent)))
        pinged = first.pings(guild_id) + second.pings(guild_id)
        assert len(pinged) == 1, (guild_id, len(pinged))
    assert ping_count(path, 1) == 1
    assert len(rows(path, "SELECT 1 FROM alerted_codes")) == 40
    # Two sources saw code 0 and 1 together: trusted, so one ping per guild. No more.
    followups = first.followups(1) + second.followups(1)
    assert followups == []


# =====================================================================
# No repeat pings, ever: a seeded random walk
# =====================================================================


def _walk_page(rng: random.Random, clock: datetime, pool: list[str]) -> str:
    today = clock.astimezone(SHIFT_TZ).date()
    main_rows, cosmetic_rows, permanent_rows = [], [], []
    for code in pool:
        if rng.random() < 0.45:
            continue
        kind = rng.choice(["main", "main", "cosmetic", "permanent"])
        expires = rng.choice(
            [
                "Unknown",
                "???",
                d(today - timedelta(days=rng.randint(1, 3))),
                d(today + timedelta(days=rng.randint(0, 9))),
            ]
        )
        reward = rng.choice(["3 Golden Keys", "1 Golden Key", "Skin: Sunrise", "Trinket: Gnome"])
        if kind == "main":
            added = rng.choice(["Unknown", d(today - timedelta(days=rng.randint(0, 6)))])
            main_rows.append((reward, added, code, expires))
        elif kind == "cosmetic":
            cosmetic_rows.append((reward, "Event", code, expires))
        else:
            permanent_rows.append((reward, "Stream", code))
    tables = []
    if main_rows:
        tables.append(main_table(*main_rows))
    if cosmetic_rows:
        tables.append(cosmetic_table(*cosmetic_rows))
    if permanent_rows:
        tables.append(permanent_table(*permanent_rows))
    return page(*tables)


@pytest.mark.parametrize("seed_", [1, 2, 3, 4, 5, 6])
async def test_three_days_of_hourly_passes_never_repeat_a_post_a_ping_or_a_follow_up(
    tmp_path, seed_
):
    path = str(tmp_path / "walk.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    seed(path)
    rng = random.Random(seed_)  # noqa: S311
    start = datetime(2026, 10, 9, 0, tzinfo=UTC)
    h = Ledger(path, load_config(V3), start)
    add_guild(path, 1, ping="everyone")
    add_guild(path, 2, ping="1234567890123456789")
    add_guild(path, 3, ping="none")
    pool = [code_n(500 + i) for i in range(30)]
    known_status: dict[str, tuple[str, str]] = {}
    revived: set[str] = set()

    for hour in range(72):
        clock = start + timedelta(hours=hour)
        items: list[RawItem] = []
        if rng.random() > 0.12:  # sometimes the table fetch just fails
            items += await read_named(_walk_page(rng, clock, pool), clock)
        for _ in range(rng.choice([0, 0, 1, 2])):
            items.append(
                reddit_item(
                    rng.choice(pool),
                    at=clock,
                    source=rng.choice([REDDIT, BLUESKY, "r/borderlands4"]),
                )
            )
        named_by_gearbox: set[str] = set()
        if rng.random() < 0.12:
            code = rng.choice(pool)
            today = clock.astimezone(SHIFT_TZ).date()
            when = today + timedelta(days=rng.choice([-2, 3]))
            named_by_gearbox.add(code)
            items.append(
                official(f"{code} valid until {when:%m/%d/%Y}", at=clock, tag=f"{seed_}-{hour}")
            )
        await h.run(items, at=clock)

        # Rule: a code's record never changes under it, and never un-posts. The one move there
        # is (QA batch 1, fix 3): a code a table's data silenced is released after all when an
        # official source names it, `too_old` to `posted`, once, with its first sighting intact.
        for code, status, first in rows(
            path, "SELECT code, status, first_seen_at FROM alerted_codes"
        ):
            if code in known_status:
                was_status, was_first = known_status[code]
                assert was_first == first, (seed_, hour, code)
                if was_status != status:
                    assert (was_status, status) == ("too_old", "posted"), (seed_, hour, code)
                    assert code not in revived, (seed_, hour, code)
                    assert rows(
                        path, "SELECT table_silenced_at FROM alerted_codes WHERE code = ?", code
                    ) != [(None,)], (seed_, hour, code)
                    revived.add(code)
            known_status[code] = (status, first)
        # Rule: a code isn't announced when its expiry, as known right now, has passed. The one
        # exception is the standing one (a table's typo can't silence Gearbox): a post from
        # Gearbox naming the code this very pass outranks an expiry only a table gave. (A post
        # dated before its own day gives no expiry at all now, QA batch 1, fix 1, so the "-2
        # days" posts above are such mentions and not deadlines.)
        today = clock.astimezone(SHIFT_TZ).date()
        for guild_id in (1, 2, 3):
            this_pass = [
                c
                for when, a in h.history.get(guild_id, [])
                if when == clock and FOLLOWUP_TEXT not in a.content
                for c in a.codes
            ]
            with closing(connect(path)) as conn:
                found = repo.code_details_for(conn, this_pass)
            for code, row in found.items():
                tables_typo = row.expires_source == "table" and code in named_by_gearbox
                assert row.expires_on is None or row.expires_on >= today or tables_typo, (
                    seed_,
                    hour,
                    code,
                )

    for guild_id in (1, 2, 3):
        originals = h.original_codes(guild_id)
        assert len(originals) == len(set(originals)), (seed_, guild_id)
        follow = [c for a in h.followups(guild_id) for c in a.codes]
        assert len(follow) == len(set(follow)), (seed_, guild_id)
        # A follow-up only ever chases a post that went out unpinged.
        pinged_originals = {c for a in h.originals(guild_id) if a.ping for c in a.codes}
        assert not (set(follow) & pinged_originals), (seed_, guild_id)
        # The cap, per UTC day (these servers keep UTC).
        per_day: dict[date, int] = {}
        for when, a in h.history.get(guild_id, []):
            if a.ping:
                per_day[when.date()] = per_day.get(when.date(), 0) + 1
        assert all(n <= 3 for n in per_day.values()), (seed_, guild_id, per_day)
    assert h.pings(3) == []
