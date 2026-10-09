"""The six rules the adversarial round added to detection (plan task 5; owner, 2026-10-09).

1. The first-listing rule is per table kind, not per source.
2. An item's game match is its own, even when it shares a URL with a table.
3. A permanent code is never "expired on arrival".
4. No pinged follow-up for a dead code, at queue time or at send time.
5. A table's expiry typo can't silence Gearbox's own announcement.
6. More than five new codes from one table in one pass go out as a roundup.

Same scaffolding as the other two detection files: real temp SQLite, the real collector
over `httpx.MockTransport`, scripted fake posters, an injected clock. No network.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_shift_details_detection import (
    FIXTURE,
    MENTALMARS,
    PAGE_URL,
    T0,
    X,
    Y,
    Z,
    code_n,
    cosmetic_table,
    main_table,
    page,
    permanent_table,
    read_page,
    reddit_item,
)
from test_shift_details_detection_adversarial import (
    Ledger,
    d,
    details_of,
    followup_rows,
    official,
    ping_count,
    read_named,
    statuses,
    synthetic_rows,
)
from test_shift_fanout import V3, add_guild, rows, seed

from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.shift import fanout
from newsbot.shift.decide import sightings_from_items
from newsbot.shift.details import end_of_day, expired_by_expiry
from newsbot.shift.fanout import TABLE_FLOOD_LIMIT, deliver_queued_codes
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow

OTHER_SITE = "another-codes-site"


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


def kind_markers(db_path: str) -> list[str]:
    return [
        k
        for (k,) in rows(
            db_path,
            "SELECT key FROM app_state WHERE key LIKE 'shift_table_kind_listed:%' ORDER BY key",
        )
    ]


def row_for(code: str, **fields) -> CodeDetailsRow:
    return CodeDetailsRow(code=code, clock_start=T0, updated_at=T0, **fields)


# --- 1. first listing, per table kind ---


async def test_the_collector_names_every_table_kind_a_code_was_listed_in():
    items = {i.title: i for i in await read_page(FIXTURE.read_bytes(), T0)}

    assert items["7KQ2M-4XNTB-8RWLD-3HVCP-ZJ6FS"].details.listed_in == ("main",)
    assert items["8JF2R-5CVNX-1LQTK-7WBDM-3HZPG"].details.listed_in == ("cosmetic",)
    assert items["2R8WC-6LNXT-3QHDB-9MVKP-4GZFJ"].details.listed_in == ("cosmetic", "permanent")
    assert items["4TV8K-2RHQN-7XWLB-5DMPC-JF9ZG"].details.listed_in == ("main", "cosmetic")


async def test_a_table_the_source_adds_later_gets_its_own_quiet_first_listing(h):
    add_guild(h.db_path, 1, ping="everyone")
    first = page(
        main_table(("1 Golden Key", "Aug 1, 2026", code_n(1), "Unknown")),
        cosmetic_table(("Gnome", "Event", X, "Unknown")),
    )
    await h.run(await read_named(first, T0), at=T0)
    assert h.alerts(1) == []
    assert kind_markers(h.db_path) == [
        f"shift_table_kind_listed:cosmetic:{MENTALMARS}",
        f"shift_table_kind_listed:main:{MENTALMARS}",
    ]

    # The site grows a permanent table. Its undated rows are old news to everyone but us.
    second = page(
        main_table(("1 Golden Key", "Aug 1, 2026", code_n(1), "Unknown")),
        cosmetic_table(("Gnome", "Event", X, "Unknown")),
        permanent_table(("Skin", "Stream", Y), ("Skin", "Stream", Z)),
    )
    await h.run(await read_named(second, T0 + timedelta(hours=1)), at=T0 + timedelta(hours=1))
    assert h.alerts(1) == []
    assert statuses(h.db_path)[Y] == statuses(h.db_path)[Z] == "too_old"
    assert f"shift_table_kind_listed:permanent:{MENTALMARS}" in kind_markers(h.db_path)

    # And from then on a new undated permanent code is news, like any community find.
    third = page(
        main_table(("1 Golden Key", "Aug 1, 2026", code_n(1), "Unknown")),
        cosmetic_table(("Gnome", "Event", X, "Unknown")),
        permanent_table(
            ("Skin", "Stream", Y), ("Skin", "Stream", Z), ("Skin", "Stream", code_n(5))
        ),
    )
    await h.run(await read_named(third, T0 + timedelta(hours=2)), at=T0 + timedelta(hours=2))
    assert h.original_codes(1) == [code_n(5)]
    assert h.pings(1) == []


async def test_a_code_listed_in_a_table_kind_read_before_is_not_a_first_listing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run(await read_named(page(cosmetic_table(("Gnome", "Event", X, "Unknown"))), T0))
    assert h.alerts(1) == []

    # Z turns up in the cosmetic table we have read, and in a permanent one we haven't.
    both = page(
        cosmetic_table(("Gnome", "Event", X, "Unknown"), ("Gnome", "Event", Z, "Unknown")),
        permanent_table(("Skin", "Stream", Z), ("Skin", "Stream", Y)),
    )
    await h.run(await read_named(both, T0 + timedelta(hours=1)), at=T0 + timedelta(hours=1))

    assert h.original_codes(1) == [Z]  # news; Y, only in the new table, is not
    assert statuses(h.db_path)[Y] == "too_old"


async def test_an_old_per_source_marker_alone_still_means_every_kind_was_read(h):
    add_guild(h.db_path, 1, ping="everyone")
    with closing(connect(h.db_path)) as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (f"{repo.TABLE_LISTED_PREFIX}{MENTALMARS}", T0.isoformat()),
        )
        conn.commit()

    await h.run(await read_named(page(permanent_table(("Skin", "Stream", X))), T0))

    assert h.original_codes(1) == [X]
    assert kind_markers(h.db_path) == [f"shift_table_kind_listed:permanent:{MENTALMARS}"]


def test_the_kind_markers_round_trip_and_keep_the_per_source_key(db_path):
    name = "odd: name:with colons"
    with closing(connect(db_path)) as conn:
        assert repo.table_kinds_listed(conn, [name, "other"]) == set()
        repo.mark_table_listed(conn, name, T0, ["main", "permanent"])
        repo.mark_table_listed(conn, name, T0 + timedelta(days=1), ["cosmetic"])
        assert repo.table_kinds_listed(conn, [name, "other"]) == {
            (name, "main"),
            (name, "permanent"),
            (name, "cosmetic"),
        }
        assert repo.table_listed_sources(conn, [name, "other"]) == {name}
        # The first stamp sticks.
        [(stamp,)] = conn.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (f"{repo.TABLE_KIND_LISTED_PREFIX}main:{name}",),
        ).fetchall()
        assert stamp == T0.isoformat()


# --- 2. scope by item, not by URL ---


def test_an_off_topic_item_sharing_a_tables_url_gets_no_sighting():
    cfg = load_config(V3)
    table_row = RawItem(
        url=PAGE_URL,
        title=X,
        excerpt=X,
        source_name=MENTALMARS,
        trust="community",
        published_at=None,
        topics=("borderlands4",),
        full_text=X,
    )
    off_topic = RawItem(
        url=PAGE_URL,
        title=f"Fortnite promo code {Y}",
        excerpt="Redeem in the item shop.",
        source_name="Some Press",
        trust="press",
        published_at=None,
    )

    sightings = sightings_from_items(
        [off_topic, table_row], topics=cfg.catalog, alert_topics=["borderlands4"]
    )

    assert [s.code for s in sightings] == [X]


# --- 3. permanent codes never expire on arrival ---


def test_a_permanent_code_never_expires_and_a_dated_one_ends_at_pacific_midnight():
    on = date(2026, 10, 8)
    ends = end_of_day(on)
    assert not expired_by_expiry(row_for(X, category="permanent", expires_on=on), ends)
    assert not expired_by_expiry(row_for(X, expires_on=on), ends - timedelta(seconds=1))
    assert expired_by_expiry(row_for(X, expires_on=on), ends)
    assert not expired_by_expiry(row_for(X), ends)
    assert not expired_by_expiry(None, ends)


# --- 4. no pinged follow-up for a dead code ---


async def _queue_a_followup_without_delivering(h, *codes):
    """Post `codes` unpinged from Reddit, then queue (but don't send) their follow-ups."""
    await h.run([reddit_item(code, at=T0) for code in codes], at=T0)
    h.clock = T0 + timedelta(hours=3)
    table = page(
        main_table(*[("1 Golden Key", d(T0.date()), code, "Oct 30, 2026") for code in codes])
    )
    await fanout._release(h.deps(), await read_named(table, h.clock), seeding_ok=True)
    assert [c for _, c, s, _ in followup_rows(h.db_path) if s == "queued"] == list(codes)


async def test_a_followup_for_a_code_that_died_before_the_send_is_skipped_and_costs_no_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    await _queue_a_followup_without_delivering(h, X)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE code_details SET expires_on = '2026-10-08' WHERE code = ?", (X,))

    await deliver_queued_codes(h.deps())
    h.harvest()

    assert followup_rows(h.db_path) == [(1, X, "skipped", 0)]
    assert h.pings(1) == []
    assert ping_count(h.db_path, 1) == 0


async def test_a_dead_code_is_dropped_from_a_followup_and_the_live_one_still_goes(h):
    add_guild(h.db_path, 1, ping="everyone")
    await _queue_a_followup_without_delivering(h, X, Y)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE code_details SET expires_on = '2026-10-08' WHERE code = ?", (X,))

    await deliver_queued_codes(h.deps())
    h.harvest()

    assert followup_rows(h.db_path) == [(1, X, "skipped", 0), (1, Y, "posted", 1)]
    assert [a.codes for a in h.followups(1)] == [[Y]]
    assert ping_count(h.db_path, 1) == 1


async def test_a_listing_that_says_a_posted_code_is_dead_queues_no_followup_at_all(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0)], at=T0)
    dead = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 8, 2026")))

    await h.run(await read_named(dead, T0 + timedelta(hours=2)), at=T0 + timedelta(hours=2))

    assert followup_rows(h.db_path) == []
    assert h.pings(1) == []


# --- 5. Gearbox can't be silenced by a table ---


async def test_a_tables_expiry_typo_does_not_silence_a_gearbox_post_in_the_same_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 1, 2026")))
    items = [official(f"new code {X}", at=T0 - timedelta(hours=1)), *await read_named(typo, T0)]

    await h.run(items, at=T0)

    assert h.original_codes(1) == [X]
    assert [a.ping for a in h.originals(1)] == [True]
    assert details_of(h.db_path, X).expires_on == date(2026, 10, 1)  # the typo is still stored


async def test_an_expiry_from_gearbox_itself_still_makes_a_new_code_quiet(h):
    add_guild(h.db_path, 1, ping="everyone")
    table = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 30, 2026")))
    post = official(f"{X} valid until 10/01/2026", at=T0 - timedelta(hours=1))

    await h.run([post, *await read_named(table, T0)], at=T0)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}
    assert details_of(h.db_path, X).expires_source == "official"


async def test_a_community_mention_does_not_rescue_a_code_a_table_says_is_dead(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = page(main_table(("1 Golden Key", d(T0.date()), X, "Oct 1, 2026")))
    items = [reddit_item(X, at=T0), *await read_named(typo, T0)]

    await h.run(items, at=T0)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


# --- 6. a flood of new rows goes out as a roundup ---


def fresh_rows(n: int, *, name: str = MENTALMARS, prefix: str = "F") -> list[RawItem]:
    rows_ = synthetic_rows(n, prefix=prefix, added=T0.date())
    return [r if name == MENTALMARS else _renamed(r, name) for r in rows_]


def _renamed(item: RawItem, name: str) -> RawItem:
    return replace(item, source_name=name)


def test_the_flood_limit_is_five():
    assert TABLE_FLOOD_LIMIT == 5


@pytest.mark.parametrize(
    ("count", "roundup", "released"),
    [(5, False, 5), (6, True, 6), (300, True, 50)],
    ids=["at the limit", "one over", "three hundred"],
)
async def test_fresh_new_rows_from_one_table_flood_only_past_five(h, count, roundup, released):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="1234567890123456789")

    assert await h.run(fresh_rows(count), at=T0) == released

    flagged = rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1")[0][0]
    assert flagged == (count if roundup else 0)  # the overflow past the cap counts too
    assert h.pings(1) == [] and h.pings(2) == []
    assert len(statuses(h.db_path)) == count
    assert sorted(set(statuses(h.db_path).values())) == (
        ["posted", "roundup"] if count > 50 else ["posted"]
    )
    assert len(set(h.original_codes(1))) == released

    # No follow-ups, ever, for a roundup: a second source a few hours later confirms nothing.
    # (An ordinary batch of five is different: that is B+, and it would earn them.)
    some = [r.title for r in fresh_rows(count)[:3]]
    h.clock = T0 + timedelta(hours=3)
    await h.run([reddit_item(code, at=h.clock, source=OTHER_SITE) for code in some], at=h.clock)
    assert (followup_rows(h.db_path) == []) is roundup
    assert (h.pings(1) == []) is roundup
    assert (ping_count(h.db_path, 1) == 0) is roundup


async def test_a_code_another_source_also_saw_is_not_counted_against_the_table(h):
    add_guild(h.db_path, 1, ping="everyone")
    rows_ = fresh_rows(6)
    # Two of the six were also reported by Reddit in the same pass: not the table's discovery.
    items = [*rows_, reddit_item(rows_[0].title, at=T0), reddit_item(rows_[1].title, at=T0)]

    assert await h.run(items, at=T0) == 6

    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1") == [(0,)]
    assert len(h.pings(1)) == 1  # the two double-sourced codes are trusted; the batch pings once


async def test_known_codes_and_a_quiet_first_listing_do_not_count_toward_a_flood(h):
    add_guild(h.db_path, 1, ping="everyone")
    # Eight codes already known, so only the other four are new this pass.
    with closing(connect(h.db_path)) as conn:
        repo.record_silent_codes(
            conn,
            [(r.title, "x", "https://example.com", "too_old", False) for r in fresh_rows(12)[:8]],
            now=lambda: T0 - timedelta(days=3),
            mark_seeded=False,
        )
    undated = synthetic_rows(30, prefix="U", added=None)

    assert await h.run([*fresh_rows(12), *undated], at=T0) == 4

    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1") == [(0,)]


async def test_each_table_source_is_counted_on_its_own(h):
    add_guild(h.db_path, 1, ping="everyone")
    items = [*fresh_rows(5), *fresh_rows(5, name=OTHER_SITE, prefix="G")]

    assert await h.run(items, at=T0) == 10

    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes WHERE from_roundup = 1") == [(0,)]


async def test_a_flood_is_released_with_the_usual_roundup_cap_note(h):
    add_guild(h.db_path, 1)
    notes: list[str] = []

    async def alert_owner(text: str) -> None:
        notes.append(text)

    deps = h.deps()
    deps.alert_owner = alert_owner
    await fanout.detect_and_fan_out(deps, fresh_rows(60), seeding_ok=True)

    assert len(notes) == 1 and "10" in notes[0]
