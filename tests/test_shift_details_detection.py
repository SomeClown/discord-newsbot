"""Detection wiring for SHiFT code details (plan task 5; design.md §16).

The scenario that matters most is the boring one: a bot that has been alerting
for weeks meets a page of thirty-odd codes it has never read, and has to say
nothing. A repeat ping is the one mistake here that strangers notice, so the
rollout tests start from a database seeded the way prod looks (pinged codes,
unpinged codes whose follow-up window closed, message ids and all) and assert
silence all the way down. The rest pin the pieces that make it true: rows
that don't collapse into one item, the first-listing rule for undated rows,
codes that expire before they're released, B+ still working with a table as
the second source, and a crash between any two steps being redone safely.

Real temp SQLite with the real migrations, scripted fake posters, the
collector reading the hand-made fixture through `httpx.MockTransport`. No
network, and the clock only moves when a test moves it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_shift_fanout import V3, Harness, add_guild, rows, seed

from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.collectors.shift_table import ShiftTableCollector
from newsbot.config import ShiftTableSource, load_config
from newsbot.pipeline.normalize import canonicalize_items
from newsbot.shift import fanout
from newsbot.shift.decide import sightings_from_items
from newsbot.shift.fanout import _canonical_batch
from newsbot.store import repo
from newsbot.store.db import connect, migrate

FIXTURE = Path(__file__).parent / "fixtures" / "shift_table" / "real_shape.html"
PAGE_URL = "https://codes.example/borderlands-4-shift-codes/"
MENTALMARS = "mentalmars"
REDDIT = "r/Borderlands4"
GEARBOX = "Gearbox Blog"

# 2026-10-10 12:00 UTC: every dated row in real_shape.html is more than 48 hours old.
LATER = datetime(2026, 10, 10, 12, tzinfo=UTC)
# 2026-10-07 12:00 UTC: the fixture's "Oct 6, 2026" row (7KQ2M...) is about a day old.
FRESH_DAY = datetime(2026, 10, 7, 12, tzinfo=UTC)
T0 = datetime(2026, 10, 9, 12, tzinfo=UTC)

# real_shape.html, by what each code is there for (all invented).
K5 = "7KQ2M-4XNTB-8RWLD-3HVCP-ZJ6FS"  # Oct 6, expires Oct 20
K3 = "2WB9T-6LHXN-5QDRC-8MVKP-YJ3GF"
K1 = "9DR4N-3TXQK-7LWBH-2VPMC-FS8GZ"
K2 = "5XM8C-1QHTV-4RLNB-9KWDP-GJ2ZF"
SKIN = "3NH7V-8BKQD-2TXLR-6WMPC-ZF4GJ"
BOTH = "4TV8K-2RHQN-7XWLB-5DMPC-JF9ZG"
BANNER = "6PC3Z-9GJ5F-2KWBN-8LRHD-4XMQV"  # undated: cosmetic table
SUNRISE = "1ZV6D-7MQKX-4NBHL-9RTWC-2PGFJ"  # undated: permanent table
FOUNDERS = "A3M9K-5TNXQ-8DLWB-2HRVC-7PFZJ"  # undated: permanent table
REAL_SHAPE_CODES = 11

X = "XA111-BBBBB-CCCCC-DDDDD-EEEEE"
Y = "YA222-BBBBB-CCCCC-DDDDD-EEEEE"
Z = "ZA333-BBBBB-CCCCC-DDDDD-EEEEE"


async def _noop_sleep(_seconds: float) -> None:
    return None


def _source() -> ShiftTableSource:
    return ShiftTableSource(
        type="shift_table",
        name=MENTALMARS,
        url=PAGE_URL,
        trust="community",
        topics=["borderlands4"],
    )


async def read_page(html: bytes | str, now: datetime) -> list[RawItem]:
    """What the real collector makes of `html` on a pass at `now`."""
    body = html.encode() if isinstance(html, str) else html
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as http:
        collector = ShiftTableCollector(_source(), sleep=_noop_sleep, clock=lambda: now)
        return await collector.collect(http)


def main_table(*table_rows: tuple[str, str, str, str]) -> str:
    """Rows are (reward, added, code, expires)."""
    body = "".join(
        f"<tr><td>{reward}</td><td>{added}</td><td>{code}</td><td>{expires}</td></tr>"
        for reward, added, code, expires in table_rows
    )
    return (
        "<h2>Every Borderlands 4 SHiFT Code for Golden Keys</h2><table><tr><th>Reward</th>"
        f"<th>Added</th><th>Borderlands 4 SHiFT Code</th><th>Expire Date</th></tr>{body}</table>"
    )


def cosmetic_table(*table_rows: tuple[str, str, str, str]) -> str:
    """Rows are (reward, source, code, expires): no "Added" column, so no dates."""
    body = "".join(
        f"<tr><td>{reward}</td><td>{origin}</td><td>{code}</td><td>{expires}</td></tr>"
        for reward, origin, code, expires in table_rows
    )
    return (
        "<h2>Cosmetic Reward SHiFT Codes</h2><table><tr><th>Reward</th><th>Source</th>"
        f"<th>Borderlands 4 SHiFT Code</th><th>Expire Date</th></tr>{body}</table>"
    )


def permanent_table(*table_rows: tuple[str, str, str]) -> str:
    """Rows are (reward, source, code)."""
    body = "".join(
        f"<tr><td>{reward}</td><td>{origin}</td><td>{code}</td></tr>"
        for reward, origin, code in table_rows
    )
    return (
        "<h2>Permanent SHiFT Codes</h2><table><tr><th>Reward</th><th>Source</th>"
        f"<th>Borderlands 4 SHiFT Code</th></tr>{body}</table>"
    )


def page(*tables: str) -> str:
    return "<html><body>" + "".join(tables) + "</body></html>"


def code_n(n: int) -> str:
    return f"T{n:04d}-ABCDE-FGHJK-LMNPQ-RSTVW"


def big_page(dated: int, undated: int, *, added: str = "Sept 1, 2026") -> str:
    """A page in the real shape: `dated` main-table rows then `undated` cosmetic and permanent."""
    main = main_table(
        *[("1 Golden Key", added, code_n(i), "Unknown") for i in range(dated)],
    )
    half = undated // 2
    cosmetic = cosmetic_table(
        *[("Trinket: Gnome", "Event", code_n(1000 + i), "???") for i in range(half)]
    )
    permanent = permanent_table(
        *[("Skin: Sunrise", "Stream", code_n(2000 + i)) for i in range(undated - half)]
    )
    return page(main, cosmetic, permanent)


def reddit_item(code: str, *, at: datetime, source: str = REDDIT) -> RawItem:
    return RawItem(
        url=f"https://example.com/{source.replace('/', '-')}/{code[:5]}",
        title=f"Borderlands 4 SHiFT code {code}",
        excerpt="",
        source_name=source,
        trust="community",
        published_at=at - timedelta(hours=1),
    )


def gearbox_item(text: str, *, at: datetime | None) -> RawItem:
    return RawItem(
        url="https://example.com/gearbox/post",
        title=f"Borderlands 4 SHiFT: {text}",
        excerpt="",
        source_name=GEARBOX,
        trust="official",
        published_at=at,
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def h(db_path):
    harness = Harness(db_path, load_config(V3), T0)
    seed(db_path)
    return harness


def details_row(db_path: str, code: str):
    with closing(connect(db_path)) as conn:
        return repo.code_details_for(conn, [code]).get(code)


def statuses(db_path: str) -> dict[str, str]:
    return dict(rows(db_path, "SELECT code, status FROM alerted_codes"))


def sent_codes(h: Harness, guild_id: int) -> list[str]:
    return [code for alert in h.sent(guild_id) for code in alert.codes]


# --- the rows must not collapse ---


async def test_a_thirty_three_row_page_gives_thirty_three_sightings_not_one():
    items = await read_page(big_page(dated=11, undated=22), LATER)
    assert len(items) == 33
    # The bug this guards: one page, one URL, so the batch dedupe keeps a single row.
    assert len(canonicalize_items(items)) == 1

    batch = _canonical_batch(items)
    assert len(batch) == 33
    sightings = sightings_from_items(
        batch,
        topics=load_config(V3).catalog,
        alert_topics=["borderlands4"],
        now=LATER,
    )
    assert len(sightings) == 33
    assert all(s.details is not None and s.details.listed for s in sightings)


def test_detail_rows_are_canonicalized_one_by_one_and_plain_items_still_dedupe():
    def row(code: str, url: str) -> RawItem:
        return RawItem(
            url=url,
            title=code,
            excerpt=code,
            source_name=MENTALMARS,
            trust="community",
            published_at=None,
            details=ItemDetails(code=code, rank="table", listed=True),
        )

    plain = RawItem(
        url="https://example.com/a?utm_source=x",
        title="t",
        excerpt="e",
        source_name="Some Feed",
        trust="community",
        published_at=None,
    )
    plain_again = replace(plain, url="https://example.com/a")
    batch = _canonical_batch(
        [
            row(X, "https://codes.example/p/?utm_source=feed#golden"),
            plain,
            row(Y, "https://codes.example/p/#cosmetic"),
            plain_again,
            row(Z, "ftp://codes.example/p/"),
        ]
    )

    assert [i.url for i in batch if i.details is not None] == [
        "https://codes.example/p",
        "https://codes.example/p",
    ]
    assert [i.title for i in batch if i.details is not None] == [X, Y]
    assert len([i for i in batch if i.details is None]) == 1


async def test_a_page_of_fresh_dated_rows_is_released_row_for_row(h):
    add_guild(h.db_path, 1, ping="everyone")
    html = big_page(dated=33, undated=0, added="Oct 9, 2026")

    released = await h.run(await read_page(html, T0))

    # Thirty-three new rows from one table is a flood: it goes out as a roundup, every row
    # of it (nothing collapsed into one), unpinged.
    assert released == 33
    assert len(sent_codes(h, 1)) == 33
    assert all(alert.ping is False for alert in h.sent(1))
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1") == [(33,)]
    assert len(rows(h.db_path, "SELECT 1 FROM code_details")) == 33


# --- rollout: nothing known is announced again, nothing old is announced at all ---


def seed_prod_like(db_path: str, codes: list[str]) -> dict[str, str]:
    """Make the database look like prod before 3.1.0, using the first few of `codes`.

    codes[0]: an official code that pinged (message id, `followup_ok` 0).
    codes[1]: a community code that posted unpinged three days ago with `followup_ok` 1, so a
        follow-up was possible once and the 24 hours are long gone.
    codes[2]: seeded when alerts started. codes[3]: recorded `too_old`.
    The rest are strangers.
    """
    first_seen = LATER - timedelta(days=3)
    with closing(connect(db_path)) as conn, conn:
        # Explicit, so these tests don't depend on what the wall clock said when they ran.
        conn.execute(
            "UPDATE app_state SET value = ? WHERE key = 'shift_details_since'",
            ((LATER - timedelta(days=1)).isoformat(),),
        )
        for i, (code, status, pinged, ok) in enumerate(
            [
                (codes[0], "posted", 1, 0),
                (codes[1], "posted", 0, 1),
                (codes[2], "seeded", 0, 0),
                (codes[3], "too_old", 0, 0),
            ]
        ):
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, message_id, pinged, status) "
                "VALUES (?, ?, ?, 'https://example.com/old', ?, ?, ?)",
                (
                    code,
                    first_seen.isoformat(),
                    GEARBOX if i == 0 else REDDIT,
                    7000 + i if status == "posted" else None,
                    pinged,
                    status,
                ),
            )
            if status == "posted":
                conn.execute(
                    "INSERT INTO guild_code_posts "
                    "(guild_id, code, status, message_id, pinged, followup_ok, claimed_at) "
                    "SELECT guild_id, ?, 'posted', ?, ?, ?, ? FROM guilds",
                    (code, 7000 + i, pinged, ok, first_seen.isoformat()),
                )
                conn.execute(
                    "INSERT INTO code_sightings (code, source_name, trusted, roundup, seen_at) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (
                        code,
                        GEARBOX if i == 0 else REDDIT,
                        1 if i == 0 else 0,
                        first_seen.isoformat(),
                    ),
                )
    return statuses(db_path)


def snapshot(db_path: str) -> dict[str, list[tuple]]:
    return {
        "posts": rows(db_path, "SELECT * FROM guild_code_posts ORDER BY guild_id, code"),
        "followups": rows(db_path, "SELECT * FROM guild_code_followups"),
        "pings": rows(db_path, "SELECT guild_id, ping_day, ping_count FROM guild_shift"),
    }


@pytest.mark.parametrize("shape", ["real_shape", "thirty_three_rows"])
async def test_rollout_onto_a_prod_like_database_is_silent(h, shape):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-10", ping_count=1)
    add_guild(h.db_path, 2, ping="1234567890123456789")
    add_guild(h.db_path, 3, ping="none")
    if shape == "real_shape":
        html = FIXTURE.read_bytes()
        page_items = await read_page(html, LATER)
    else:
        # 33 rows: eleven dated (one of them within 48 hours, and known), the rest undated.
        html = big_page(dated=11, undated=22)
        page_items = await read_page(html, LATER)
        page_items = [
            i if i.title != code_n(1) else _with_added(i, LATER.date() - timedelta(days=1))
            for i in page_items
        ]
    page_codes = [i.title for i in page_items]
    assert len(page_codes) == (REAL_SHAPE_CODES if shape == "real_shape" else 33)

    before_statuses = seed_prod_like(h.db_path, page_codes)
    before = snapshot(h.db_path)
    h.clock = LATER

    released = await h.run(page_items)

    # Silence: no post, no ping, no follow-up, nothing for any server.
    assert released == 0
    assert all(h.sent(g) == [] for g in (1, 2, 3))
    assert snapshot(h.db_path) == before

    # Everything already known keeps the status it had; every stranger is recorded quietly.
    after = statuses(h.db_path)
    for code, status in before_statuses.items():
        assert after[code] == status
    strangers = [c for c in page_codes if c not in before_statuses]
    assert strangers
    assert {after[c] for c in strangers} == {"too_old"}

    # Every listed code has its details row, and the clock starts at this rollout.
    for code in page_codes:
        row = details_row(h.db_path, code)
        assert row is not None, code
        assert row.clock_start == LATER
        assert row.first_listed_at == LATER
    assert rows(
        h.db_path, "SELECT 1 FROM app_state WHERE key LIKE 'shift_table_kind_listed:%:mentalmars'"
    )

    # And the next hourly pass has nothing to say either.
    h.clock = LATER + timedelta(hours=1)
    released = await h.run(await read_page(html, h.clock))
    assert released == 0
    assert all(h.sent(g) == [] for g in (1, 2, 3))
    assert snapshot(h.db_path) == before
    assert details_row(h.db_path, page_codes[0]).clock_start == LATER


def _with_added(item: RawItem, day: date) -> RawItem:
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return replace(item, published_at=start, details=replace(item.details, added_on=day))


async def test_a_row_added_within_48_hours_and_new_to_the_bot_is_queued_unpinged(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.clock = FRESH_DAY

    released = await h.run(await read_page(FIXTURE.read_bytes(), FRESH_DAY))

    assert released == 1
    assert sent_codes(h, 1) == [K5]
    assert h.sent(1)[0].ping is False
    after = statuses(h.db_path)
    assert after[K5] == "posted"
    # Everything else on that first listing is history: dated-and-old or undated.
    assert {c: s for c, s in after.items() if c != K5} == {c: "too_old" for c in after if c != K5}
    assert len(after) == REAL_SHAPE_CODES

    h.clock = FRESH_DAY + timedelta(hours=1)
    assert await h.run(await read_page(FIXTURE.read_bytes(), h.clock)) == 0
    assert h.sent(1) == []


async def test_the_first_listing_is_per_source_and_remembered(h, db_path):
    add_guild(h.db_path, 1)
    html = page(cosmetic_table(("Trinket: Gnome", "Event", X, "Unknown")))

    await h.run(await read_page(html, T0))

    assert h.sent(1) == []
    assert statuses(db_path) == {X: "too_old"}
    with closing(connect(db_path)) as conn:
        assert {n for n, _ in repo.table_kinds_listed(conn, [MENTALMARS, "other"])} == {MENTALMARS}


async def test_an_undated_new_row_after_the_first_listing_posts_unpinged(h):
    add_guild(h.db_path, 1, ping="everyone")
    first = page(cosmetic_table(("Trinket: Gnome", "Event", X, "Unknown")))
    await h.run(await read_page(first, T0))
    assert h.sent(1) == []

    h.clock = T0 + timedelta(hours=1)
    second = page(
        cosmetic_table(
            ("Trinket: Gnome", "Event", X, "Unknown"),
            ("Banner: Vault", "Newsletter", Y, "Unknown"),
        )
    )
    released = await h.run(await read_page(second, h.clock))

    assert released == 1
    assert sent_codes(h, 1) == [Y]
    assert h.sent(1)[0].ping is False
    assert statuses(h.db_path) == {X: "too_old", Y: "posted"}


async def test_undated_rows_of_a_second_table_source_get_their_own_first_listing(h):
    add_guild(h.db_path, 1)
    await h.run(await read_page(page(cosmetic_table(("Gnome", "Event", X, "Unknown"))), T0))
    with closing(connect(h.db_path)) as conn:
        repo.mark_table_listed(conn, MENTALMARS, T0, ["cosmetic"])
        assert repo.table_kinds_listed(conn, ["another site"]) == set()

    other = [
        RawItem(
            url="https://other.example/codes",
            title=Y,
            excerpt=Y,
            source_name="another site",
            trust="community",
            published_at=None,
            topics=("borderlands4",),
            full_text=Y,
            details=ItemDetails(code=Y, rank="table", listed=True),
        )
    ]
    await h.run(other)

    assert h.sent(1) == []
    assert statuses(h.db_path)[Y] == "too_old"


# --- expired on arrival ---


async def test_a_code_that_expired_before_its_release_is_recorded_quietly(h):
    add_guild(h.db_path, 1, ping="everyone")
    # Seen on the first listing already, so this pass is an ordinary one.
    await h.run(await read_page(page(cosmetic_table(("Gnome", "Event", Z, "Unknown"))), T0))
    h.clock = T0 + timedelta(hours=1)
    html = page(
        main_table(
            ("3 Golden Keys", "Oct 9, 2026", X, "Oct 8, 2026"),
            ("3 Golden Keys", "Oct 9, 2026", Y, "Oct 9, 2026"),
        )
    )

    released = await h.run(await read_page(html, h.clock))

    # X ended yesterday (Pacific); Y's last day is today, so it's still worth saying.
    assert released == 1
    assert sent_codes(h, 1) == [Y]
    assert statuses(h.db_path)[X] == "too_old"
    assert details_row(h.db_path, X).expires_on == date(2026, 10, 8)

    # It stays quiet on every later pass and never gets queued.
    h.clock = T0 + timedelta(hours=2)
    assert await h.run(await read_page(html, h.clock)) == 0
    assert rows(h.db_path, "SELECT code FROM guild_code_posts WHERE code = ?", X) == []


async def test_expiry_is_judged_by_the_pacific_date_not_utc(h):
    add_guild(h.db_path, 1)
    await h.run(await read_page(page(cosmetic_table(("Gnome", "Event", Z, "Unknown"))), T0))
    # 2026-10-10 03:00 UTC is still the evening of Oct 9 in Los Angeles.
    h.clock = datetime(2026, 10, 10, 3, tzinfo=UTC)
    html = page(main_table(("1 Golden Key", "Oct 9, 2026", X, "Oct 9, 2026")))

    assert await h.run(await read_page(html, h.clock)) == 1
    assert sent_codes(h, 1) == [X]


async def test_expired_on_arrival_while_unseeded_stays_seeded(db_path):
    harness = Harness(db_path, load_config(V3), T0)  # deliberately not seeded
    add_guild(db_path, 1)
    html = page(main_table(("1 Golden Key", "Oct 9, 2026", X, "Oct 1, 2026")))

    assert await harness.run(await read_page(html, T0)) == 0

    assert statuses(db_path) == {X: "seeded"}


# --- B+ with a table as the second source ---


async def test_mentalmars_confirming_a_community_code_within_a_day_queues_the_one_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0)])
    assert sent_codes(h, 1) == [X] and h.sent(1)[0].ping is False

    h.clock = T0 + timedelta(hours=3)
    html = page(main_table(("3 Golden Keys", "Oct 9, 2026", X, "Oct 30, 2026")))
    await h.run(await read_page(html, h.clock))

    assert len(h.sent(1)) == 1
    assert h.sent(1)[0].ping is True
    # Task 7: a follow-up shows the details as they stand when it's sent.
    assert h.sent(1)[0].content == (
        f"@everyone `> confirmed by a second source ▌`\n\n```\n{X}\n```\n`reward` 3 Golden Keys"
        " · `expires` Oct 30, 2026 (<t:1793430000:R>) · `via` mentalmars"
    )
    assert rows(h.db_path, "SELECT guild_id, code, status, pinged FROM guild_code_followups") == [
        (1, X, "posted", 1)
    ]
    # The listing also filled in what the alert didn't know.
    row = details_row(h.db_path, X)
    assert (row.reward, row.expires_on, row.reward_from) == (
        "3 Golden Keys",
        date(2026, 10, 30),
        MENTALMARS,
    )

    # No repeat, ever, however many hours it stays on the page.
    for hours in (4, 5, 30):
        h.clock = T0 + timedelta(hours=hours)
        await h.run(await read_page(html, h.clock))
        assert h.sent(1) == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_followups") == [(1,)]


async def test_a_listing_after_the_window_closes_queues_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0)])

    h.clock = T0 + timedelta(hours=25)
    html = page(main_table(("3 Golden Keys", "Oct 9, 2026", X, "Oct 30, 2026")))
    await h.run(await read_page(html, h.clock))

    assert h.sent(1) == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_followups") == [(0,)]
    assert details_row(h.db_path, X) is not None


async def test_mentalmars_and_reddit_in_the_same_pass_ping_on_the_first_post(h):
    add_guild(h.db_path, 1, ping="everyone")
    html = page(main_table(("3 Golden Keys", "Oct 9, 2026", X, "Oct 30, 2026")))
    items = [reddit_item(X, at=T0), *await read_page(html, T0)]

    assert await h.run(items) == 1

    assert sent_codes(h, 1) == [X]
    assert h.sent(1)[0].ping is True
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_followups") == [(0,)]


async def test_a_lone_table_row_never_pings_and_five_new_rows_are_still_ordinary_news(h):
    add_guild(h.db_path, 1, ping="everyone")
    html = big_page(dated=5, undated=0, added="Oct 9, 2026")

    released = await h.run(await read_page(html, T0))

    # At the flood limit a table's new rows are ordinary community finds: not roundups.
    assert released == 5
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1") == [(0,)]
    assert all(alert.ping is False for alert in h.sent(1))


# --- official posts ---


async def test_an_official_single_code_post_overrides_the_tables_expiry(h):
    add_guild(h.db_path, 1)
    html = page(main_table(("3 Golden Keys", "Oct 9, 2026", X, "Oct 20, 2026")))
    await h.run(await read_page(html, T0))
    assert details_row(h.db_path, X).expires_on == date(2026, 10, 20)

    h.clock = T0 + timedelta(hours=1)
    await h.run([gearbox_item(f"{X} expires October 14", at=h.clock)])
    row = details_row(h.db_path, X)
    assert row.expires_on == date(2026, 10, 14)
    assert (row.expires_source, row.expires_from) == ("official", GEARBOX)
    assert row.details_changed_at == h.clock

    # The table going on to say Oct 20 again never wins it back.
    h.clock = T0 + timedelta(hours=2)
    await h.run(await read_page(html, h.clock))
    row = details_row(h.db_path, X)
    assert row.expires_on == date(2026, 10, 14)
    assert row.details_changed_at == T0 + timedelta(hours=1)


async def test_an_official_multi_code_post_releases_every_code_and_leaves_details_alone(h):
    add_guild(h.db_path, 1, ping="everyone")
    html = page(main_table(("3 Golden Keys", "Oct 9, 2026", X, "Oct 20, 2026")))
    await h.run(await read_page(html, T0))
    before = details_row(h.db_path, X)

    h.clock = T0 + timedelta(hours=1)
    released = await h.run([gearbox_item(f"{X} and {Y}, all expire October 1", at=h.clock)])

    assert released == 1  # X is known; Y is new
    # Y posts pinged (official). X, already out there unpinged, is confirmed by this second
    # source and gets its one follow-up, which is B+ doing its job and not a repeat.
    assert rows(h.db_path, "SELECT status, pinged FROM guild_code_posts WHERE code = ?", Y) == [
        ("posted", 1)
    ]
    assert rows(h.db_path, "SELECT code, status FROM guild_code_followups") == [(X, "posted")]
    assert details_row(h.db_path, X) == before
    assert details_row(h.db_path, Y) is None

    h.clock = T0 + timedelta(hours=2)
    two_new = f"{Z} and ZB444-BBBBB-CCCCC-DDDDD-EEEEE, both expire October 1"
    assert await h.run([gearbox_item(two_new, at=h.clock)]) == 2
    assert details_row(h.db_path, Z) is None


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        (datetime(2026, 10, 10, 9, tzinfo=UTC), date(2026, 10, 14)),
        (datetime(2026, 11, 1, 9, tzinfo=UTC), date(2027, 10, 14)),
    ],
)
async def test_the_year_of_an_undated_official_expiry_comes_from_the_hooks_clock(
    h, clock, expected
):
    add_guild(h.db_path, 1)
    h.clock = clock

    await h.run([gearbox_item(f"{X} expires October 14", at=None)])

    assert details_row(h.db_path, X).expires_on == expected


# --- a crash between any two steps is redone safely ---


def _fail_once(monkeypatch, name: str) -> None:
    real = getattr(fanout, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked (a test, not a prophecy)")
        return real(*args, **kwargs)

    monkeypatch.setattr(fanout, name, flaky)


async def test_a_crash_before_the_release_is_redone_by_the_next_pass(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    h.clock = FRESH_DAY
    _fail_once(monkeypatch, "record_released_codes")

    with pytest.raises(sqlite3.OperationalError):
        await h.run(await read_page(FIXTURE.read_bytes(), FRESH_DAY))
    # Sightings and details were written; the release and the first-listing mark were not.
    assert details_row(h.db_path, K5) is not None
    assert (
        rows(h.db_path, "SELECT 1 FROM app_state WHERE key LIKE 'shift_table_kind_listed:%'") == []
    )
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(0,)]

    h.clock = FRESH_DAY + timedelta(hours=1)
    assert await h.run(await read_page(FIXTURE.read_bytes(), h.clock)) == 1

    assert sent_codes(h, 1) == [K5]
    after = statuses(h.db_path)
    assert after[K5] == "posted"
    assert {s for c, s in after.items() if c != K5} == {"too_old"}
    assert rows(h.db_path, "SELECT 1 FROM app_state WHERE key LIKE 'shift_table_kind_listed:%'")


async def test_a_crash_before_the_first_listing_mark_posts_nothing_twice(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    h.clock = FRESH_DAY
    _fail_once(monkeypatch, "mark_table_listed")

    with pytest.raises(sqlite3.OperationalError):
        await h.run(await read_page(FIXTURE.read_bytes(), FRESH_DAY))
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts WHERE status = 'queued'") == [
        (1,)
    ]

    h.clock = FRESH_DAY + timedelta(hours=1)
    assert await h.run(await read_page(FIXTURE.read_bytes(), h.clock)) == 0

    # The queued code from the crashed pass goes out exactly once, on the redo.
    assert sent_codes(h, 1) == [K5]
    assert rows(h.db_path, "SELECT 1 FROM app_state WHERE key LIKE 'shift_table_kind_listed:%'")
    h.clock = FRESH_DAY + timedelta(hours=2)
    await h.run(await read_page(FIXTURE.read_bytes(), h.clock))
    assert h.sent(1) == []
    assert statuses(h.db_path)[K3] == "too_old"
