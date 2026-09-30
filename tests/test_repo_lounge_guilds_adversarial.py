"""Adversarial tests for the per-guild lounge deck and guard (plan task 12).

`test_repo_lounge_guilds.py` checks the happy path: two guilds, two sources,
everybody minding their own business. This file asks what happens when they
don't have separate sources, because `lounge_quotes_used` kept its
`(source_key, quote_hash)` primary key and only grew a `guild_id` column. Two
servers that follow the same Wikiquote page are two tenants in one row. Only
the friend has a lounge today, so none of this can bite yet; the xfails are
here so the day a second lounge exists, somebody reads the reason first.

Also pinned: what a second guild sees of the `guild_id IS NULL` rows (v2.2's
history, and whatever a rolled-back v2.2 writes afterwards), and what the
`lounge_state` rollback mirror holds when two guilds write to it. Where the
code does something the owner might want to change, the test says what it does
today and the comment says so. Strict xfails are real bugs; the fix belongs to
the implement agent, not to me.
"""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.config import load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.lounge.quotes import quote_hash
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings

G1, G2, G3 = 100000000000000001, 100000000000000002, 100000000000000003
WILDE = "wikiquote:Oscar Wilde"
TWAIN = "wikiquote:Mark Twain"
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
DAY1, DAY2 = "2026-09-29", "2026-09-30"
T0 = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


def _clock(minutes: int = 0):
    return lambda: T0 + timedelta(minutes=minutes)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "newsbot.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        for gid in (G1, G2, G3):
            repo.create_guild(conn, gid)
            repo.upsert_lounge(conn, LoungeSettings(gid, 50, True, "hi", True, "08:00", [], None))
    return path


@pytest.fixture
def conn(db_path):
    with closing(connect(db_path)) as c:
        yield c


def _claim(conn, gid, key=WILDE, h=H1, day=DAY1, *, reshuffle=False, force=False, m=0):
    return repo.claim_quote(
        conn,
        source_key=key,
        quote_hash=h,
        local_day=day,
        reshuffle=reshuffle,
        force=force,
        now=_clock(m),
        guild_id=gid,
    )


def _v2_claim(conn, key=WILDE, h=H1, day=DAY1, m=0):
    """v2.2's call, exactly as it was before task 12: no guild_id anywhere."""
    return repo.claim_quote(
        conn,
        source_key=key,
        quote_hash=h,
        local_day=day,
        reshuffle=False,
        force=False,
        now=_clock(m),
    )


def _used(conn, gid, key=WILDE):
    return repo.quote_deck_state(conn, key, gid).used


# --- the shared primary key (plan 3.2) ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "lounge_quotes_used keeps PRIMARY KEY (source_key, quote_hash), and claim_quote deletes "
        "by that pair before inserting. Guild B claiming a quote guild A already used re-stamps "
        "A's row with B's guild_id, so A's deck forgets it and A can post that quote again "
        "before its deck is exhausted"
    ),
)
def test_two_guilds_sharing_a_source_do_not_make_each_other_repeat(conn):
    assert _claim(conn, G1, h=H1, day=DAY1) is True
    assert _claim(conn, G2, h=H1, day=DAY1) is True  # B happens to draw the same quote
    assert H1 in _used(conn, G1)  # A posted it; it must still count as used for A


@pytest.mark.xfail(
    strict=True,
    reason=(
        "same shared primary key: after B re-stamps A's row, A's last_hash (the quote the "
        "no-back-to-back rule protects) is gone too, so A can draw the same quote two days running"
    ),
)
def test_a_second_guild_claiming_the_same_quote_does_not_erase_the_first_guilds_last_hash(conn):
    _claim(conn, G1, h=H2, day=DAY1, m=0)
    _claim(conn, G1, h=H1, day=DAY2, m=10)  # A's most recent quote is H1
    _claim(conn, G2, h=H1, day=DAY1, m=20)
    assert repo.quote_deck_state(conn, WILDE, G1).last_hash == H1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "same shared primary key: a quote both guilds posted is stored once, under whoever "
        "claimed it last, so when that guild leaves (ON DELETE CASCADE) the other guild's "
        "history of it is deleted with it"
    ),
)
def test_the_other_guild_leaving_does_not_delete_a_quote_this_guild_used(conn):
    _claim(conn, G1, h=H1)
    _claim(conn, G2, h=H1)
    repo.delete_guild(conn, G2)
    assert H1 in _used(conn, G1)


def test_a_second_guild_claiming_a_different_quote_leaves_the_first_guilds_deck_alone(conn):
    _claim(conn, G1, h=H1)
    _claim(conn, G2, h=H2)
    assert _used(conn, G1) == {H1}
    assert _used(conn, G2) == {H2}


def test_a_guild_does_not_see_the_quotes_another_guild_used_from_the_same_source(conn):
    # Not a bug, a consequence: two servers on one source may post the same quote the same week.
    _claim(conn, G1, h=H1)
    assert _used(conn, G2) == frozenset()


def test_both_guilds_dates_are_written_even_when_they_claim_the_same_quote(conn):
    assert _claim(conn, G1, h=H1, day=DAY1) is True
    assert _claim(conn, G2, h=H1, day=DAY2) is True
    assert repo.get_lounge(conn, G1).last_quote_date == DAY1
    assert repo.get_lounge(conn, G2).last_quote_date == DAY2


def test_one_guilds_reshuffle_leaves_the_other_guilds_rows_on_a_shared_source(conn):
    _claim(conn, G1, h=H1, day=DAY1)
    _claim(conn, G2, h=H2, day=DAY1)
    assert _claim(conn, G2, h=H3, day=DAY2, reshuffle=True) is True
    assert _used(conn, G2) == {H3}
    assert _used(conn, G1) == {H1}


def test_three_guilds_on_one_source_each_own_their_own_row_when_hashes_differ(conn):
    for gid, h in ((G1, H1), (G2, H2), (G3, H3)):
        assert _claim(conn, gid, h=h) is True
    assert [_used(conn, g) for g in (G1, G2, G3)] == [{H1}, {H2}, {H3}]
    owners = conn.execute("SELECT guild_id FROM lounge_quotes_used ORDER BY guild_id").fetchall()
    assert [r["guild_id"] for r in owners] == [G1, G2, G3]


# --- the NULL rows (v2.2's history, or a rolled-back v2.2's new writes) ---


def test_a_null_row_belongs_to_every_guilds_deck(conn):
    """Pins the `OR guild_id IS NULL` half of the filter: nobody claimed it, so everybody has it."""
    assert _v2_claim(conn, h=H1) is True
    assert conn.execute("SELECT guild_id FROM lounge_quotes_used").fetchone()["guild_id"] is None
    # A second lounge guild that never posted H1 still treats it as used. Fine while the
    # import backfills; surprising if v2.2 ran again after a rollback. The owner may want this
    # to stop once a second lounge exists.
    assert _used(conn, G1) == {H1}
    assert _used(conn, G2) == {H1}


def test_a_second_guilds_reshuffle_deletes_the_unclaimed_rows_the_first_guild_relies_on(conn):
    _v2_claim(conn, h=H1)
    assert _claim(conn, G2, h=H2, day=DAY1, reshuffle=True) is True
    # G1 never posted H1 either, so losing it is harmless; the point pinned here is that a
    # reshuffle by any guild erases NULL rows for everyone.
    assert _used(conn, G1) == frozenset()
    assert _used(conn, G2) == {H2}


def test_a_null_row_is_not_restamped_by_a_guilds_reshuffle_of_another_source(conn):
    _v2_claim(conn, key=TWAIN, h=H1)
    _claim(conn, G1, key=WILDE, h=H2, reshuffle=True)
    assert _used(conn, G2, TWAIN) == {H1}


def test_the_import_stamps_every_v22_row_so_a_new_lounge_guild_starts_with_an_empty_deck(v22_db):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    with closing(connect(v22_db)) as c, c:
        c.execute("DELETE FROM lounge_quotes_used")
        for n, key in enumerate((WILDE, TWAIN)):
            c.execute(
                "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES (?, ?, ?)",
                (key, quote_hash(f"q{n}"), "2026-09-20T15:00:00+00:00"),
            )
    report = ensure_imported(v22_db, cfg, lambda: T0)
    friend = report.guild_id
    with closing(connect(v22_db)) as c:
        assert (
            c.execute("SELECT COUNT(*) FROM lounge_quotes_used WHERE guild_id IS NULL").fetchone()[
                0
            ]
            == 0
        )
        repo.create_guild(c, G2)
        repo.upsert_lounge(c, LoungeSettings(G2, 50, True, "hi", True, "08:00", [], None))
        for key in (WILDE, TWAIN):
            assert _used(c, G2, key) == frozenset()  # a newcomer does not inherit the friend's deck
        assert len(_used(c, friend, WILDE)) == 1 and len(_used(c, friend, TWAIN)) == 1


def test_a_rolled_back_v22_writing_null_rows_after_the_import_leaks_into_every_guilds_deck(v22_db):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    report = ensure_imported(v22_db, cfg, lambda: T0)
    with closing(connect(v22_db)) as c:
        repo.create_guild(c, G2)
        repo.upsert_lounge(c, LoungeSettings(G2, 50, True, "hi", True, "08:00", [], None))
        # v2.2 running again after a rollback: no guild_id, so the row is born NULL.
        assert _v2_claim(c, key=WILDE, h=H3, day="2099-01-01") is True
        # Pinned: the unstamped row counts for the friend (right) and for the stranger (wrong
        # in spirit; the stranger never posted it). The import's backfill only runs once.
        assert H3 in _used(c, report.guild_id, WILDE)
        assert H3 in _used(c, G2, WILDE)


# --- the lounge_state rollback mirror ---


def test_the_rollback_mirror_holds_whichever_guild_claimed_last_not_the_latest_date(conn):
    # Two lounge guilds in different zones: G1 is a day ahead of G2. The mirror is a single
    # cell and the upsert takes the last writer, so it can move backwards. A rollback to
    # v2.2 in that window would read G2's older date and, for the friend, allow a second
    # quote that day. Low risk (the mirror exists for a rollback before going public), but
    # the owner may want MAX() or "imported guild only".
    _claim(conn, G1, key=WILDE, h=H1, day=DAY2, m=0)
    _claim(conn, G2, key=TWAIN, h=H2, day=DAY1, m=1)
    assert repo.get_lounge_state(conn).last_quote_date == DAY1


def test_a_refused_guild_claim_leaves_the_mirror_alone(conn):
    _claim(conn, G1, h=H1, day=DAY2)
    assert _claim(conn, G1, h=H2, day=DAY2) is False
    assert _claim(conn, G1, h=H2, day=DAY1) is False  # the clock stepped backwards
    assert repo.get_lounge_state(conn).last_quote_date == DAY2


def test_a_clock_that_steps_backwards_is_refused_for_a_guild_and_writes_no_row(conn):
    _claim(conn, G1, h=H1, day=DAY2)
    assert _claim(conn, G1, h=H2, day=DAY1) is False
    assert _used(conn, G1) == {H1}
    assert repo.get_lounge(conn, G1).last_quote_date == DAY2


# --- v2's claim is unchanged ---


def test_v2_claim_without_a_guild_id_leaves_every_guild_date_untouched(conn):
    assert _v2_claim(conn, h=H1, day=DAY1) is True
    assert repo.get_lounge_state(conn).last_quote_date == DAY1
    for gid in (G1, G2, G3):
        assert repo.get_lounge(conn, gid).last_quote_date is None
    assert _v2_claim(conn, h=H2, day=DAY1) is False  # v2's own guard, still global


def test_a_guild_claim_does_not_trip_v2s_guard_for_another_source_on_its_own_date(conn):
    assert _claim(conn, G1, h=H1, day=DAY1) is True
    # v2's reading of the mirror says today is taken, exactly as it would have after v2's own claim.
    assert _v2_claim(conn, key=TWAIN, h=H2, day=DAY1) is False
    assert _used(conn, G2, TWAIN) == frozenset()
