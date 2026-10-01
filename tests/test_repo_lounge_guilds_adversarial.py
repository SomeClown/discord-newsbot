"""Adversarial tests for the per-guild lounge deck and guard (plan task 12).

`test_repo_lounge_guilds.py` checks the happy path: two guilds, two sources,
everybody minding their own business. This file asks what happens when they
don't have separate sources: two servers that follow the same Wikiquote page
must each keep their own history (migration 005 rebuilt the table without the
shared `(source_key, quote_hash)` key, which used to make them tenants in one
row and let one server's claim or departure rewrite the other's).

Also pinned: what a second guild sees of the `guild_id IS NULL` rows (v2.2's
history, and whatever a rolled-back v2.2 writes afterwards), v2.2's literal
quote SQL running against the v3 table, and what the `lounge_state` rollback
mirror holds when two guilds write to it (the imported guild's date only).
"""

import sqlite3
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


def test_two_guilds_sharing_a_source_do_not_make_each_other_repeat(conn):
    assert _claim(conn, G1, h=H1, day=DAY1) is True
    assert _claim(conn, G2, h=H1, day=DAY1) is True  # B happens to draw the same quote
    assert H1 in _used(conn, G1)  # A posted it; it must still count as used for A


def test_a_second_guild_claiming_the_same_quote_does_not_erase_the_first_guilds_last_hash(conn):
    _claim(conn, G1, h=H2, day=DAY1, m=0)
    _claim(conn, G1, h=H1, day=DAY2, m=10)  # A's most recent quote is H1
    _claim(conn, G2, h=H1, day=DAY1, m=20)
    assert repo.quote_deck_state(conn, WILDE, G1).last_hash == H1


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


def test_the_rollback_mirror_is_written_only_for_the_imported_guild(conn):
    # G1 is the friend's guild (the one the import recorded); G2 is a stranger's lounge in
    # another zone. v2.2 has one lounge, the friend's, so the mirror is G1's date and nobody
    # else's: G2 claiming later, on an earlier date, must not drag it backwards.
    conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (T0.isoformat(), G1))
    conn.commit()
    _claim(conn, G1, key=WILDE, h=H1, day=DAY2, m=0)
    _claim(conn, G2, key=TWAIN, h=H2, day=DAY1, m=1)
    assert repo.get_lounge_state(conn).last_quote_date == DAY2


def test_a_claim_by_a_non_imported_guild_leaves_the_mirror_alone(conn):
    conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (T0.isoformat(), G1))
    conn.commit()
    _claim(conn, G2, h=H1, day=DAY1)
    assert repo.get_lounge_state(conn).last_quote_date is None


def test_with_no_imported_guild_nothing_writes_the_mirror(conn):
    _claim(conn, G1, h=H1, day=DAY1)
    assert repo.get_lounge_state(conn).last_quote_date is None


def test_a_refused_guild_claim_leaves_the_mirror_alone(conn):
    conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (T0.isoformat(), G1))
    conn.commit()
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
    conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (T0.isoformat(), G1))
    conn.commit()
    assert _claim(conn, G1, h=H1, day=DAY1) is True
    # v2's reading of the mirror says today is taken, exactly as it would have after v2's own claim.
    assert _v2_claim(conn, key=TWAIN, h=H2, day=DAY1) is False
    assert _used(conn, G2, TWAIN) == frozenset()


# --- v2.2's own statements against the v3 table (the rollback promise) ---

# Copied from `git show v2.2.0:newsbot/store/repo.py`, claim_quote and quote_deck_state.
V22_RESHUFFLE = "DELETE FROM lounge_quotes_used WHERE source_key = ?"
V22_DELETE = "DELETE FROM lounge_quotes_used WHERE source_key = ? AND quote_hash = ?"
V22_INSERT = "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES (?, ?, ?)"
V22_DECK = (
    "SELECT quote_hash FROM lounge_quotes_used WHERE source_key = ? "
    "ORDER BY used_at DESC, rowid DESC"
)


def _v22_claim(conn, h, used_at="2026-10-01T15:00:00+00:00", *, reshuffle=False):
    if reshuffle:
        conn.execute(V22_RESHUFFLE, (WILDE,))
    conn.execute(V22_DELETE, (WILDE, h))
    conn.execute(V22_INSERT, (WILDE, h, used_at))
    conn.commit()


def test_v22_sql_runs_against_the_v3_table_and_repeat_claims_do_not_collide(conn):
    _claim(conn, G1, h=H1)
    _v22_claim(conn, H2)
    _v22_claim(conn, H2)  # the same quote again: delete-then-insert, never a collision
    deck = [r["quote_hash"] for r in conn.execute(V22_DECK, (WILDE,))]
    assert deck[0] == H2 and set(deck) == {H1, H2} and len(deck) == 2


def test_v22_delete_then_insert_takes_over_a_guild_stamped_row_of_the_same_quote(conn):
    _claim(conn, G1, h=H1)
    _claim(conn, G2, h=H1)
    _v22_claim(conn, H1)  # a single-lounge v2.2 sees one deck: its delete clears both copies
    rows = conn.execute("SELECT guild_id FROM lounge_quotes_used").fetchall()
    assert [r["guild_id"] for r in rows] == [None]


def test_v22_reshuffle_clears_the_source_and_leaves_other_sources_alone(conn):
    _claim(conn, G1, key=TWAIN, h=H3)
    _claim(conn, G1, h=H1)
    _v22_claim(conn, H2, reshuffle=True)
    assert [r["quote_hash"] for r in conn.execute(V22_DECK, (WILDE,))] == [H2]
    assert _used(conn, G1, TWAIN) == {H3}


def test_what_v22_wrote_reads_back_in_v3_for_every_guild(conn):
    _v22_claim(conn, H1)
    assert H1 in _used(conn, G1) and H1 in _used(conn, G2)
    assert _claim(conn, G1, h=H2) is True  # the stamped claim sits beside the unclaimed row
    assert _used(conn, G1) == {H1, H2}
    assert _used(conn, G2) == {H1}


def test_the_stamped_key_still_refuses_a_literal_duplicate_for_one_guild(conn):
    _claim(conn, G1, h=H1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at, guild_id) "
            "VALUES (?, ?, ?, ?)",
            (WILDE, H1, "2026-10-01T15:00:00+00:00", G1),
        )
