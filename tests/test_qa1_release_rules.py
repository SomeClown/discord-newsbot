"""QA fix batch 1, fixes 2 and 3: release and delivery agree; a table can't silence Gearbox.

Fix 2. Delivery used to skip a queued code on its own rule ("not trusted, or the expiry is
official") while release used another ("no official source named it, or the expiry is
official"). A code two community sources agreed on, with a table's expiry that lapsed between
release and delivery, posted at delivery and would have been silent at release. Release now
records whether an official-trust source named the code (`alerted_codes.official_seen_at`)
and delivery applies release's predicate to it.

Fix 3. A code recorded `too_old` only because of table-rank data (a table's expiry already
past, a table's "Added" date over 48 hours old) is flagged (`table_silenced_at`). If an
official-trust source names it within 72 hours of `first_seen_at` it is released after all:
an UPDATE to `posted` plus the usual queueing, exactly once, never twice.

Same scaffolding as the detection tests: real temp SQLite, the real collector over
`httpx.MockTransport`, scripted fake posters, an injected clock.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta

import pytest
from test_shift_details_detection import (
    GEARBOX,
    T0,
    X,
    Y,
    cosmetic_table,
    main_table,
    page,
    reddit_item,
)
from test_shift_details_detection_adversarial import (
    BLUESKY,
    Ledger,
    d,
    details_of,
    first_seen,
    official,
    read_named,
    statuses,
)
from test_shift_fanout import V3, add_guild, rows, seed

from newsbot.config import load_config
from newsbot.shift import fanout
from newsbot.shift.details import silenced_by_expiry
from newsbot.shift.fanout import _dead_on_arrival, deliver_queued_codes
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow

HOUR = timedelta(hours=1)


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


def silenced_at(db_path: str, code: str) -> str | None:
    [(stamp,)] = rows(db_path, "SELECT table_silenced_at FROM alerted_codes WHERE code = ?", code)
    return stamp


def official_seen_at(db_path: str, code: str) -> str | None:
    [(stamp,)] = rows(db_path, "SELECT official_seen_at FROM alerted_codes WHERE code = ?", code)
    return stamp


def post_rows(db_path: str):
    return rows(
        db_path, "SELECT guild_id, code, status, pinged FROM guild_code_posts ORDER BY rowid"
    )


def table_with(code: str, *, added: str, expires: str = "Unknown") -> str:
    return page(main_table(("1 Golden Key", added, code, expires)))


# =====================================================================================
# Fix 2: delivery's dead-code rule is release's
# =====================================================================================


async def _release_then_deliver_next_day(h, items):
    """Release at T0, deliver at T0 + 20 hours (Oct 10, 01:00 Pacific: Oct 9 is over)."""
    assert await fanout._release(h.deps(), items, seeding_ok=True) == 1
    h.clock = T0 + 20 * HOUR
    await deliver_queued_codes(h.deps())
    h.harvest()


async def test_two_community_sources_do_not_save_a_code_a_tables_expiry_has_since_killed(h):
    add_guild(h.db_path, 1, ping="everyone")
    table = table_with(X, added=d(T0.date()), expires="Oct 9, 2026")
    items = [
        reddit_item(X, at=T0),
        reddit_item(X, at=T0, source=BLUESKY),
        *await read_named(table, T0),
    ]

    await _release_then_deliver_next_day(h, items)

    # Trusted by two names, but no official source named it: release would have called it
    # dead had the expiry already passed, so delivery does too. (The old rule posted it.)
    assert h.alerts(1) == []
    assert post_rows(h.db_path) == [(1, X, "skipped", 0)]
    assert official_seen_at(h.db_path, X) is None


async def test_an_official_mention_keeps_a_code_alive_against_a_tables_lapsed_expiry(h):
    add_guild(h.db_path, 1, ping="everyone")
    table = table_with(X, added=d(T0.date()), expires="Oct 9, 2026")
    items = [official(f"new code {X}", at=T0 - HOUR), *await read_named(table, T0)]

    await _release_then_deliver_next_day(h, items)

    assert h.original_codes(1) == [X]
    assert post_rows(h.db_path) == [(1, X, "posted", 1)]
    assert official_seen_at(h.db_path, X) is not None


async def test_an_official_expiry_kills_a_code_even_when_an_official_source_named_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    items = [official(f"new code {X}, valid until October 9", at=T0 - HOUR)]

    await _release_then_deliver_next_day(h, items)

    assert details_of(h.db_path, X).expires_source == "official"
    assert h.alerts(1) == []
    assert post_rows(h.db_path) == [(1, X, "skipped", 0)]


async def test_release_records_who_saw_the_code_and_the_queue_reports_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    await fanout._release(
        h.deps(), [official(f"{X}", at=T0 - HOUR), reddit_item(Y, at=T0)], seeding_ok=True
    )

    with closing(connect(h.db_path)) as conn:
        queued = {q.code: q for q in repo.queued_guild_codes(conn, 1)}

    assert queued[X].official_seen is True
    assert queued[Y].official_seen is False
    assert official_seen_at(h.db_path, Y) is None


@pytest.mark.parametrize("official_seen", [False, True])
@pytest.mark.parametrize("source", ["", "table", "official"])
@pytest.mark.parametrize("expired", [False, True])
def test_release_and_delivery_ask_the_same_question(official_seen, source, expired):
    now = datetime(2026, 10, 10, 12, 0, tzinfo=T0.tzinfo)
    expires_on = date(2026, 10, 9) if expired else date(2026, 10, 10)
    row = CodeDetailsRow(
        code=X,
        clock_start=now,
        updated_at=now,
        expires_on=expires_on if source else None,
        expires_source=source,
    )
    release = _dead_on_arrival(row, X, now, {X} if official_seen else set())
    delivery = silenced_by_expiry(row, now, official_seen=official_seen)
    assert release == delivery
    # And the rule itself: dead needs an expiry that has passed, and then either nobody
    # official named the code or the expiry is itself official.
    assert delivery == (bool(source) and expired and (not official_seen or source == "official"))


def test_no_row_and_a_permanent_code_are_never_silenced():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=T0.tzinfo)
    assert silenced_by_expiry(None, now, official_seen=False) is False
    permanent = CodeDetailsRow(
        code=X,
        clock_start=now,
        updated_at=now,
        category="permanent",
        expires_on=date(2020, 1, 1),
        expires_source="official",
    )
    assert silenced_by_expiry(permanent, now, official_seen=False) is False


# =====================================================================================
# Fix 3: a table's typo can't silence Gearbox across passes
# =====================================================================================


async def test_a_wrong_added_date_is_flagged_and_gearbox_releases_the_code_once(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")  # really today's code; the table is a month off
    await h.run(await read_named(typo, T0), at=T0)
    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is not None

    later = T0 + 10 * HOUR
    post = official(f"new code {X}", at=later - HOUR)
    await h.run([post, *await read_named(typo, later)], at=later)

    assert h.original_codes(1) == [X]
    assert [a.ping for a in h.originals(1)] == [True]
    assert statuses(h.db_path) == {X: "posted"}
    assert rows(h.db_path, "SELECT source_name FROM alerted_codes") == [(GEARBOX,)]
    assert first_seen(h.db_path, X) == T0  # the clock on the 72 hours never moved
    assert official_seen_at(h.db_path, X) is not None
    assert post_rows(h.db_path) == [(1, X, "posted", 1)]

    # The next hour's pass sees the same post and the same table. Nothing happens again.
    await h.run([post, *await read_named(typo, later + HOUR)], at=later + HOUR)
    assert h.original_codes(1) == [X]
    assert len(h.pings(1)) == 1
    assert post_rows(h.db_path) == [(1, X, "posted", 1)]


async def test_an_expiry_typo_in_a_table_is_flagged_and_gearbox_releases_the_code(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added=d(T0.date()), expires="Oct 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is not None

    later = T0 + 2 * HOUR
    await h.run(
        [official(f"new code {X}", at=later - HOUR), *await read_named(typo, later)], at=later
    )

    assert h.original_codes(1) == [X]
    assert [a.ping for a in h.originals(1)] == [True]
    assert statuses(h.db_path) == {X: "posted"}
    assert details_of(h.db_path, X).expires_source == "table"  # the typo is still on file


@pytest.mark.parametrize(
    ("after", "released"),
    [
        (timedelta(hours=71), True),
        (timedelta(hours=72), True),
        (timedelta(hours=72, seconds=1), False),
        (timedelta(days=5), False),
    ],
)
async def test_the_release_window_is_72_hours_from_first_seen(h, after, released):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)
    assert statuses(h.db_path) == {X: "too_old"}

    later = T0 + after
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.original_codes(1) == ([X] if released else [])
    assert statuses(h.db_path) == {X: "posted" if released else "too_old"}


async def test_a_code_silenced_by_an_official_expiry_stays_silent(h):
    add_guild(h.db_path, 1, ping="everyone")
    # Gearbox itself says it ended on Oct 7 (the post is from the evening of Oct 7 Pacific).
    dead = official(f"{X} valid until 10/07/2026", at=T0 - 30 * HOUR)
    await h.run([dead], at=T0)
    assert statuses(h.db_path) == {X: "too_old"}
    assert details_of(h.db_path, X).expires_source == "official"
    assert silenced_at(h.db_path, X) is None  # not a table's doing, so never revivable

    later = T0 + 5 * HOUR
    again = official(f"new code {X}", at=later - HOUR, tag="again")
    await h.run([dead, again], at=later)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_flagged_code_whose_official_post_gives_a_dead_official_expiry_stays_silent(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)
    assert silenced_at(h.db_path, X) is not None

    # Posted at 23:00 Pacific on Oct 9, saying it's good through Oct 9. Read at 13:00 Pacific
    # on Oct 10 it is a fresh post (14 hours old) about a code that is already over.
    later = T0 + 32 * HOUR
    post = official(f"new code {X} valid until October 9", at=T0 + 18 * HOUR)
    await h.run([post], at=later)

    assert details_of(h.db_path, X).expires_source == "official"
    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_community_sighting_alone_never_releases_a_flagged_code(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)

    later = T0 + 5 * HOUR
    await h.run([reddit_item(X, at=later), reddit_item(X, at=later, source=BLUESKY)], at=later)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_code_made_stale_by_a_community_date_is_not_flagged(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0 - timedelta(days=3))], at=T0)
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is None

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.alerts(1) == []
    assert statuses(h.db_path) == {X: "too_old"}


async def test_a_quiet_first_listing_is_not_flagged(h):
    add_guild(h.db_path, 1, ping="everyone")
    first = page(cosmetic_table(("Gnome", "Event", X, "Unknown")))
    await h.run(await read_named(first, T0), at=T0)
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is None

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.alerts(1) == []


async def test_a_code_stale_in_both_a_table_and_a_community_post_is_not_flagged(h):
    add_guild(h.db_path, 1, ping="everyone")
    items = [
        reddit_item(X, at=T0 - timedelta(days=4)),
        *await read_named(table_with(X, added="Sept 1, 2026"), T0),
    ]
    await h.run(items, at=T0)
    assert statuses(h.db_path) == {X: "too_old"}
    assert silenced_at(h.db_path, X) is None


async def test_a_flagged_code_released_by_an_official_roundup_posts_unpinged_as_a_roundup(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)

    later = T0 + 5 * HOUR
    others = " ".join(f"R{n:04d}-ABCDE-FGHJK-LMNPQ-RSTVW" for n in range(1, 7))
    await h.run([official(f"all the codes: {X} {others}", at=later - HOUR)], at=later)

    # Seven codes in one post is a roundup: whatever the normal rules do with a new roundup
    # code is what the revived one gets. Posted, unpinged, once.
    assert statuses(h.db_path)[X] == "posted"
    assert rows(h.db_path, "SELECT from_roundup FROM alerted_codes WHERE code = ?", X) == [(1,)]
    assert h.pings(1) == []
    assert post_rows(h.db_path)[0] == (1, X, "posted", 0)


async def test_two_racing_releases_of_the_same_flagged_code_have_one_winner(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)
    row = (X, GEARBOX, "https://example.com/g", False)

    with closing(connect(h.db_path)) as one, closing(connect(h.db_path)) as two:
        first = repo.record_released_codes(
            one,
            [row],
            now=lambda: T0 + HOUR,
            queue_for_games=["borderlands4"],
            flags={X: (True, True)},
            official={X},
            revive={X},
        )
        second = repo.record_released_codes(
            two,
            [row],
            now=lambda: T0 + HOUR,
            queue_for_games=["borderlands4"],
            flags={X: (True, True)},
            official={X},
            revive={X},
        )

    assert first == [X]
    assert second == []
    assert rows(h.db_path, "SELECT code, status FROM alerted_codes") == [(X, "posted")]
    assert post_rows(h.db_path) == [(1, X, "queued", 0)]


async def test_revive_leaves_a_code_that_is_not_flagged_alone(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([reddit_item(X, at=T0 - timedelta(days=3))], at=T0)  # too_old, not flagged

    with closing(connect(h.db_path)) as conn:
        released = repo.record_released_codes(
            conn,
            [(X, GEARBOX, "https://example.com/g", False)],
            now=lambda: T0 + HOUR,
            queue_for_games=["borderlands4"],
            revive={X},
        )

    assert released == []
    assert statuses(h.db_path) == {X: "too_old"}
    assert post_rows(h.db_path) == []


async def test_a_crash_between_release_and_delivery_does_not_release_or_post_twice(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)

    later = T0 + 5 * HOUR
    h.clock = later
    items = [official(f"new code {X}", at=later - HOUR), *await read_named(typo, later)]

    # Pass one releases and queues, then "crashes" before delivering anything.
    assert await fanout._release(h.deps(), items, seeding_ok=True) == 1
    assert post_rows(h.db_path) == [(1, X, "queued", 0)]
    # Pass two sees the same items, and the code is already out.
    assert await fanout._release(h.deps(), items, seeding_ok=True) == 0
    assert post_rows(h.db_path) == [(1, X, "queued", 0)]

    await deliver_queued_codes(h.deps())
    await deliver_queued_codes(h.deps())
    h.harvest()

    assert h.original_codes(1) == [X]
    assert len(h.pings(1)) == 1
    assert post_rows(h.db_path) == [(1, X, "posted", 1)]


async def test_a_revived_code_is_delivered_to_every_server_eligible_at_the_moment(h):
    add_guild(h.db_path, 1, ping="everyone")
    typo = table_with(X, added="Sept 1, 2026")
    await h.run(await read_named(typo, T0), at=T0)
    # A server that turns alerts on after the code was silenced, before it is revived.
    add_guild(h.db_path, 2, ping="none", enabled_at=T0 + HOUR)

    later = T0 + 5 * HOUR
    await h.run([official(f"new code {X}", at=later - HOUR)], at=later)

    assert h.original_codes(1) == [X]
    assert h.original_codes(2) == [X]
    assert [a.ping for a in h.originals(2)] == [False]


async def test_the_flag_only_goes_on_codes_that_were_recorded_too_old(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([official(f"new code {Y}", at=T0 - HOUR)], at=T0)
    assert statuses(h.db_path) == {Y: "posted"}
    assert silenced_at(h.db_path, Y) is None

    fresh_table = table_with(X, added=d(T0.date()), expires="Unknown")
    await h.run(await read_named(fresh_table, T0 + HOUR), at=T0 + HOUR)
    assert silenced_at(h.db_path, X) is None
    assert statuses(h.db_path)[X] == "posted"
