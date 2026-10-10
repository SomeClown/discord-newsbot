"""QA round 2, area 2: attacking the release of a code a table silenced (batch d36d492).

A code recorded `too_old` only because of a fan site's data carries `table_silenced_at`, and an
official sighting within 72 hours of the bot first seeing it releases it after all. This file
leans on that rule from the sides the first round didn't: the boundary to the microsecond,
several official sightings at once, a stale official post that must not spend the flag, the
B+ follow-up and the daily cap meeting a revived code, and the ways a code can be silenced
that must never revive.

The decision pinned here for B+ ("one ping per code per server"): a revived code is an
official find, so it pings at once (cap permitting) and never also gets a "confirmed by a
second source" follow-up; a revived code the cap left unpinged stays unpinged for good.
"""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta

import pytest
from test_qa1_release_rules import (
    HOUR,
    official_seen_at,
    post_rows,
    silenced_at,
    table_with,
)
from test_shift_details_detection import GEARBOX, T0, X, Y, reddit_item
from test_shift_details_detection_adversarial import (
    BLUESKY,
    Ledger,
    d,
    details_of,
    first_seen,
    followup_rows,
    official,
    ping_count,
    read_named,
    statuses,
)
from test_shift_fanout import V3, add_guild, rows, seed

from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.shift import fanout
from newsbot.shift.fanout import deliver_queued_codes
from newsbot.store.db import connect, migrate

STEAM = "Borderlands 4 (Steam)"
MICROSECOND = timedelta(microseconds=1)
# The wrong "Added" date that makes a table's row look a month old.
TYPO = "Sept 1, 2026"


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


async def flag(h, code=X, *, guilds=((1, "everyone"),), at=None):
    """Silence `code` the way a table typo does, and check that it took."""
    for guild_id, ping in guilds:
        add_guild(h.db_path, guild_id, ping=ping)
    typo = table_with(code, added=TYPO)
    await h.run(await read_named(typo, at or T0), at=at or T0)
    assert statuses(h.db_path)[code] == "too_old"
    assert silenced_at(h.db_path, code) is not None
    return typo


def steam_post(text: str, *, at) -> RawItem:
    """A second official-trust source (its own name and URL)."""
    return RawItem(
        url="https://example.com/steam/post",
        title=f"Borderlands 4 SHiFT: {text}",
        excerpt="",
        source_name=STEAM,
        trust="official",
        published_at=at,
    )


# --- the 72 hour boundary, to the microsecond ---


@pytest.mark.parametrize(
    ("after", "released"),
    [
        (timedelta(hours=72) - MICROSECOND, True),
        (timedelta(hours=72), True),
        (timedelta(hours=72) + MICROSECOND, False),
    ],
)
async def test_the_72_hour_edge_is_exact_to_the_microsecond(h, after, released):
    await flag(h)

    later = T0 + after
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.original_codes(1) == ([X] if released else [])
    assert statuses(h.db_path) == {X: "posted" if released else "too_old"}


async def test_a_code_that_missed_the_72_hour_window_is_never_released_by_a_later_official_post(h):
    await flag(h)
    late = T0 + timedelta(hours=72) + MICROSECOND
    await h.run([official(f"new code {X}", at=late - HOUR)], at=late)
    await h.run([official(f"new code {X}", at=late + 5 * HOUR, tag="again")], at=late + 6 * HOUR)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


# --- a stale official post doesn't spend the flag ---


async def test_a_stale_official_post_neither_releases_nor_unflags_the_code(h):
    await flag(h)

    later = T0 + 5 * HOUR
    stale = official(f"new code {X}", at=later - timedelta(days=5), tag="old")
    await h.run([stale], at=later)
    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is not None

    # Gearbox says it for real an hour later, still inside the 72 hours.
    fresh_at = later + HOUR
    await h.run([official(f"new code {X}", at=fresh_at - HOUR, tag="new")], at=fresh_at)
    assert h.original_codes(1) == [X]
    assert statuses(h.db_path) == {X: "posted"}


async def test_a_stale_official_post_with_a_dead_official_expiry_keeps_the_code_silent(h):
    await flag(h)

    later = T0 + 5 * HOUR
    # Posted Oct 5, saying it was good through Oct 6: stale, and its expiry is official.
    stale = official(
        f"new code {X} valid until October 6, 2026", at=later - timedelta(days=4), tag="old"
    )
    await h.run([stale], at=later)
    assert details_of(h.db_path, X).expires_source == "official"

    again = later + HOUR
    await h.run([official(f"new code {X}", at=again - HOUR, tag="new")], at=again)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


# --- the official sighting meets other things in the same pass ---


async def test_an_official_post_beside_a_community_roundup_of_the_same_code_is_not_a_roundup(h):
    await flag(h)
    others = [f"R{n:04d}-ABCDE-FGHJK-LMNPQ-RSTVW" for n in range(1, 7)]
    roundup = RawItem(
        url="https://example.com/reddit/roundup",
        title="All the codes: " + " ".join([X, *others]),
        excerpt="",
        source_name="r/Borderlands4",
        trust="community",
        published_at=T0 + 4 * HOUR,
    )

    later = T0 + 5 * HOUR
    await h.run([roundup, official(f"new code {X}", at=later - HOUR)], at=later)

    assert statuses(h.db_path)[X] == "posted"
    assert rows(h.db_path, "SELECT from_roundup FROM alerted_codes WHERE code = ?", X) == [(0,)]
    assert [(r[1], r[3]) for r in post_rows(h.db_path) if r[1] == X] == [(X, 1)]


async def test_two_official_sources_naming_a_flagged_code_in_one_pass_release_it_once(h):
    await flag(h, guilds=((1, "everyone"), (2, "everyone")))

    later = T0 + 5 * HOUR
    await h.run(
        [official(f"new code {X}", at=later - HOUR), steam_post(f"new code {X}", at=later - HOUR)],
        at=later,
    )

    assert h.original_codes(1) == [X]
    assert h.original_codes(2) == [X]
    assert len(h.pings(1)) == 1 and len(h.pings(2)) == 1
    assert statuses(h.db_path) == {X: "posted"}
    assert post_rows(h.db_path) == [(1, X, "posted", 1), (2, X, "posted", 1)]
    assert first_seen(h.db_path, X) == T0
    assert official_seen_at(h.db_path, X) is not None


async def test_a_second_official_source_a_pass_later_changes_nothing(h):
    await flag(h)

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)
    await h.run(
        [steam_post(f"new code {X}", at=later), official(f"new code {X}", at=later - HOUR)],
        at=later + HOUR,
    )

    assert h.original_codes(1) == [X]
    assert len(h.pings(1)) == 1
    assert h.followups(1) == []


# --- crash and restart ---


async def test_a_restart_between_release_and_delivery_delivers_once_with_the_ping(db_path):
    first = Ledger(db_path, load_config(V3), T0)
    seed(db_path)
    await flag(first)

    later = T0 + 5 * HOUR
    first.clock = later
    items = [official(f"new code {X}", at=later - HOUR)]
    assert await fanout._release(first.deps(), items, seeding_ok=True) == 1
    # The process dies here. A new one starts on the same file and sees the same items again.
    restarted = Ledger(db_path, load_config(V3), later + 10 * timedelta(minutes=1))
    assert await fanout._release(restarted.deps(), items, seeding_ok=True) == 0
    await deliver_queued_codes(restarted.deps())
    await deliver_queued_codes(restarted.deps())
    restarted.harvest()

    assert restarted.original_codes(1) == [X]
    assert len(restarted.pings(1)) == 1
    assert post_rows(db_path) == [(1, X, "posted", 1)]


# --- B+: one ping per code per server ---


async def test_a_revived_code_gets_its_ping_and_no_confirmed_followup_in_the_same_pass(h):
    await flag(h)

    later = T0 + 5 * HOUR  # inside the 24 hours a follow-up would need
    await h.run(
        [
            official(f"new code {X}", at=later - HOUR),
            reddit_item(X, at=later),
            reddit_item(X, at=later, source=BLUESKY),
            *await read_named(table_with(X, added=TYPO), later),
        ],
        at=later,
    )

    assert h.original_codes(1) == [X]
    assert len(h.pings(1)) == 1
    assert h.followups(1) == []
    assert followup_rows(h.db_path) == []


async def test_a_revived_code_never_gets_a_followup_as_more_sources_keep_listing_it(h):
    await flag(h)
    typo = table_with(X, added=TYPO)

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)
    for hours in range(1, 30):
        at = later + hours * HOUR
        await h.run(
            [
                reddit_item(X, at=at),
                reddit_item(X, at=at, source=BLUESKY),
                official(f"new code {X}", at=later - HOUR),
                *await read_named(typo, at),
            ],
            at=at,
        )

    assert len(h.pings(1)) == 1
    assert h.followups(1) == []
    assert followup_rows(h.db_path) == []
    assert h.original_codes(1) == [X]


async def test_a_revived_code_at_the_daily_cap_posts_unpinged_and_is_never_pinged_later(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-09", ping_count=3)
    add_guild(h.db_path, 2, ping="everyone")
    await flag(h, guilds=())

    later = T0 + 5 * HOUR  # still Oct 9 in UTC, the day these servers keep
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.original_codes(1) == [X]
    assert h.pings(1) == []
    assert ping_count(h.db_path, 1) == 3
    assert [a.ping for a in h.originals(2)] == [True]
    assert any("cap reached" in text for g, text in h.notices if g == 1)

    # The window for a follow-up closed long ago, and a trusted find never earned one anyway.
    for hours in (6, 20, 30):
        at = T0 + hours * HOUR
        await h.run([reddit_item(X, at=at), reddit_item(X, at=at, source=BLUESKY)], at=at)
    assert h.pings(1) == []
    assert h.followups(1) == []
    # Not even a queued-then-skipped follow-up row: a trusted find that lost its ping to the
    # cap was never one a second source could follow up.
    assert followup_rows(h.db_path) == []
    assert post_rows(h.db_path) == [(1, X, "posted", 0), (2, X, "posted", 1)]


async def test_two_flagged_codes_revived_together_spend_the_cap_in_order(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-09", ping_count=2)
    await flag(h, X, guilds=())
    await h.run(await read_named(table_with(Y, added=TYPO), T0 + HOUR), at=T0 + HOUR)
    assert statuses(h.db_path)[Y] == "too_old"

    later = T0 + 5 * HOUR
    await h.run(
        [official(f"new codes {X} and {Y}", at=later - HOUR)],
        at=later,
    )

    # One ping left in the day's budget: one message holds both, and it pings.
    assert sorted(h.original_codes(1)) == sorted([X, Y])
    assert len(h.pings(1)) == 1
    assert ping_count(h.db_path, 1) == 3


# --- silenced in ways that must never revive ---


async def test_a_community_date_silence_followed_by_a_table_listing_never_revives(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0 - timedelta(days=3))], at=T0)
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is None

    # The table now lists it too, with its own stale date; the first word was the community's.
    await h.run(await read_named(table_with(X, added=TYPO), T0 + HOUR), at=T0 + HOUR)
    assert silenced_at(h.db_path, X) is None

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_table_silence_followed_by_a_stale_community_post_still_revives_for_gearbox(h):
    await flag(h)
    await h.run([reddit_item(X, at=T0 - timedelta(days=3))], at=T0 + HOUR)
    assert statuses(h.db_path) == {X: "too_old"}

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    # The community post arrived later and can't un-flag what the table flagged first.
    assert h.original_codes(1) == [X]


async def test_a_flagged_code_with_an_official_past_expiry_stored_by_another_route_stays_silent(h):
    await flag(h)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "UPDATE code_details SET expires_on = '2026-10-01', expires_source = 'official' "
            "WHERE code = ?",
            (X,),
        )

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_flag_survives_the_revival_it_caused_and_a_posted_code_is_never_revived_again(h):
    await flag(h)
    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)
    assert statuses(h.db_path) == {X: "posted"}
    assert silenced_at(h.db_path, X) is not None  # history, not a handle

    again = later + 10 * HOUR
    await h.run([official(f"new code {X}", at=again - HOUR, tag="more")], at=again)

    assert h.original_codes(1) == [X]
    assert post_rows(h.db_path) == [(1, X, "posted", 1)]


async def test_a_revived_code_whose_table_date_was_a_typo_still_lists_with_the_tables_details(h):
    await flag(h)
    later = T0 + 5 * HOUR
    await h.run(
        [
            official(f"new code {X}", at=later - HOUR),
            *await read_named(table_with(X, added=d(T0.date())), later),
        ],
        at=later,
    )

    assert h.original_codes(1) == [X]
    assert details_of(h.db_path, X) is not None
    assert rows(h.db_path, "SELECT source_name FROM alerted_codes WHERE code = ?", X) == [
        (GEARBOX,)
    ]
