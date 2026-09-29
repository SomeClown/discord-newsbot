"""Adversarial tests for the lounge repo functions (`get_lounge_state`,
`quote_deck_state`, `claim_quote`), beyond test_repo_lounge.py's happy paths.

Angle (test-engineer brief, 2026-09-29): `claim_quote` is a tiny function
with a big job. It has to be the one thing standing between a restart near
09:00 and two quotes in the lounge, so these tests lean on it: threads
racing each other, a failure halfway through a reshuffle, a connection that
was already mid-transaction, dates that are wrong in creative ways, keys
and hashes that are weird but legal. Anything surprising is pinned as
written and labelled "documented", because a behavior nobody wrote down
just gets rediscovered later, at a worse hour. The two things that were
genuinely wrong (both latent, neither reachable with the production clock)
were strict xfails until they got fixed; now they're plain tests.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate

WILDE = "wikiquote:Oscar Wilde"
TWAIN = "wikiquote:Mark Twain"
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
DAY1, DAY2 = "2026-09-29", "2026-09-30"
T0 = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)


def _clock(minutes: int = 0):
    return lambda: T0 + timedelta(minutes=minutes)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "newsbot.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def conn(db_path):
    with closing(connect(db_path)) as c:
        yield c


def _claim(
    conn,
    source_key=WILDE,
    quote_hash=H1,
    day=DAY1,
    *,
    reshuffle=False,
    force=False,
    now=None,
    m=0,
):
    return repo.claim_quote(
        conn,
        source_key=source_key,
        quote_hash=quote_hash,
        local_day=day,
        reshuffle=reshuffle,
        force=force,
        now=now or _clock(m),
    )


def _rows(conn):
    return {
        (r["source_key"], r["quote_hash"]): r["used_at"]
        for r in conn.execute("SELECT source_key, quote_hash, used_at FROM lounge_quotes_used")
    }


def _race(db_path, jobs):
    """Run each `job(conn)` on its own thread and connection, all released at once."""
    barrier = threading.Barrier(len(jobs))
    results: list = [None] * len(jobs)
    errors: list[BaseException] = []

    def runner(i, job):
        try:
            with closing(connect(db_path)) as c:
                barrier.wait(timeout=5)
                results[i] = job(c)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert errors == []
    return results


# --- concurrency ---


def test_eight_connections_racing_one_day_produce_exactly_one_winner_and_one_row(db_path):
    hashes = [str(i) * 64 for i in range(8)]
    jobs = [(lambda c, h=h: _claim(c, quote_hash=h, reshuffle=False, force=False)) for h in hashes]
    results = _race(db_path, jobs)

    assert results.count(True) == 1
    winner = hashes[results.index(True)]
    with closing(connect(db_path)) as c:
        assert set(_rows(c)) == {(WILDE, winner)}
        assert repo.get_lounge_state(c).last_quote_date == DAY1


def test_a_forced_claim_racing_a_normal_one_leaves_consistent_state(db_path):
    results = _race(
        db_path,
        [
            lambda c: _claim(c, quote_hash=H1, force=False),
            lambda c: _claim(c, quote_hash=H2, force=True),
        ],
    )
    # Force always wins; the normal claim wins only if it happened to go first.
    assert results[1] is True
    with closing(connect(db_path)) as c:
        expected = {(WILDE, H2)} | ({(WILDE, H1)} if results[0] else set())
        assert set(_rows(c)) == expected
        assert repo.get_lounge_state(c).last_quote_date == DAY1


def test_racing_reshuffles_on_one_source_leave_exactly_the_last_writers_row(db_path):
    with closing(connect(db_path)) as c:
        for h in (H1, H2, H3):
            _claim(c, quote_hash=h, force=True)
    new = [str(i) * 64 for i in "456789"]
    jobs = [(lambda c, h=h: _claim(c, quote_hash=h, reshuffle=True, force=True)) for h in new]
    assert _race(db_path, jobs) == [True] * 6

    with closing(connect(db_path)) as c:
        rows = _rows(c)
        assert len(rows) == 1  # no partial decks, no leftovers from before
        assert next(iter(rows))[1] in new
        assert repo.quote_deck_state(c, WILDE).last_hash == next(iter(rows))[1]


def test_racing_reshuffles_on_different_sources_never_delete_each_others_rows(db_path):
    sources = [f"wikiquote:Person {i}" for i in range(6)]
    with closing(connect(db_path)) as c:
        for s in sources:
            _claim(c, s, H1, force=True)
            _claim(c, s, H2, force=True, m=1)
    jobs = [(lambda c, s=s: _claim(c, s, H3, reshuffle=True, force=True, m=2)) for s in sources]
    assert _race(db_path, jobs) == [True] * 6

    with closing(connect(db_path)) as c:
        assert set(_rows(c)) == {(s, H3) for s in sources}


# --- transaction hygiene ---


def _boom_trigger(conn):
    """Make the last write in `claim_quote` (the `lounge_state` upsert) blow up."""
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON lounge_state BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()


def test_failure_after_the_reshuffle_delete_and_insert_restores_deck_and_date(conn):
    _claim(conn, quote_hash=H1, day=DAY1, m=0)
    _claim(conn, quote_hash=H2, day=DAY1, force=True, m=1)
    before = _rows(conn)
    _boom_trigger(conn)

    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        _claim(conn, quote_hash=H3, day=DAY2, reshuffle=True, m=2)

    assert _rows(conn) == before
    assert repo.get_lounge_state(conn).last_quote_date == DAY1
    assert conn.in_transaction is False


def test_failure_leaves_isolation_level_and_autocommit_as_they_were(conn):
    _boom_trigger(conn)
    level, autocommit = conn.isolation_level, conn.autocommit
    with pytest.raises(sqlite3.DatabaseError):
        _claim(conn)
    assert conn.isolation_level == level
    assert conn.autocommit == autocommit


def test_success_also_restores_a_non_default_isolation_level(conn):
    conn.isolation_level = "EXCLUSIVE"
    assert _claim(conn) is True
    assert conn.isolation_level == "EXCLUSIVE"


def test_other_repo_calls_still_commit_on_the_same_connection_after_a_failed_claim(db_path, conn):
    _boom_trigger(conn)
    with pytest.raises(sqlite3.DatabaseError):
        _claim(conn)
    conn.execute("DROP TRIGGER boom")
    conn.commit()

    assert repo.claim_digest(conn, date(2026, 9, 29), force=False) is not None
    # claim_codes returns whether the ping was actually spent.
    assert repo.claim_codes(
        conn,
        [("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "Src", "https://e/a")],
        pinged=True,
        local_day=DAY1,
        now=_clock(),
    )
    # If the isolation level had been left at None, these writes would sit
    # uncommitted; a second connection is the honest witness.
    with closing(connect(db_path)) as other:
        assert other.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
        assert other.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1


def test_a_successful_claim_is_visible_to_a_second_connection_immediately(db_path, conn):
    _claim(conn)
    with closing(connect(db_path)) as other:
        assert repo.get_lounge_state(other).last_quote_date == DAY1


def test_a_closed_connection_raises_and_writes_nothing(db_path):
    c = connect(db_path)
    c.close()
    with pytest.raises(sqlite3.ProgrammingError):
        _claim(c)
    with closing(connect(db_path)) as other:
        assert _rows(other) == {}
        assert repo.get_lounge_state(other).last_quote_date is None


def test_claiming_inside_a_callers_open_transaction_commits_the_callers_work_too(conn):
    # Documented, and slightly alarming: `claim_quote` sets
    # `isolation_level = None`, which per the sqlite3 docs commits any
    # transaction that's already open. So a caller's pending write is
    # committed by the claim and a later rollback() can't take it back. No
    # caller does this today (every repo function opens and closes its own
    # unit of work); the tripwire is here for whoever first does.
    conn.execute("INSERT INTO lounge_state (key, value) VALUES ('callers_pending', 'x')")
    assert conn.in_transaction
    assert _claim(conn) is True
    conn.rollback()
    keys = {r["key"] for r in conn.execute("SELECT key FROM lounge_state")}
    assert keys == {"callers_pending", "last_quote_date"}


def test_a_refused_claim_also_restores_state_and_leaves_no_open_transaction(conn):
    _claim(conn)
    level = conn.isolation_level
    assert _claim(conn, quote_hash=H2) is False
    assert conn.in_transaction is False
    assert conn.isolation_level == level


# --- date guard edges ---


def test_days_that_differ_only_in_format_are_different_days(conn):
    # Documented: the guard is string equality. `local_day` is the caller's
    # `date.isoformat()`, and that's the contract; "2026-9-29" is caller error
    # and gets a second quote rather than an exception.
    assert _claim(conn, day="2026-09-29") is True
    assert _claim(conn, quote_hash=H2, day="2026-9-29", m=1) is True
    assert repo.get_lounge_state(conn).last_quote_date == "2026-9-29"


def test_force_on_the_same_day_twice_wins_both_times(conn):
    assert _claim(conn, quote_hash=H1, force=True) is True
    assert _claim(conn, quote_hash=H2, force=True, m=1) is True
    assert set(_rows(conn)) == {(WILDE, H1), (WILDE, H2)}
    assert repo.get_lounge_state(conn).last_quote_date == DAY1


def test_an_earlier_day_after_a_later_one_is_refused_and_writes_nothing(conn):
    # Changed from "wins and rewinds last_quote_date": the guard is now
    # "not older than", so a clock stepping backwards can't post extra quotes.
    assert _claim(conn, quote_hash=H1, day=DAY2) is True
    assert _claim(conn, quote_hash=H2, day=DAY1, m=1) is False
    assert repo.get_lounge_state(conn).last_quote_date == DAY2
    assert set(_rows(conn)) == {(WILDE, H1)}
    assert _claim(conn, quote_hash=H3, day=DAY2, m=2) is False


def test_a_forced_claim_for_an_earlier_day_still_wins_and_rewinds(conn):
    # Force bypasses the guard and still stores its own day, as before.
    assert _claim(conn, quote_hash=H1, day=DAY2) is True
    assert _claim(conn, quote_hash=H2, day=DAY1, force=True, m=1) is True
    assert repo.get_lounge_state(conn).last_quote_date == DAY1


def test_empty_day_is_accepted_once_then_refused_like_any_other_string(conn):
    # Documented: no validation of `local_day`. Empty is a legal string.
    assert _claim(conn, day="") is True
    assert repo.get_lounge_state(conn).last_quote_date == ""
    assert _claim(conn, quote_hash=H2, day="", m=1) is False


def test_none_day_raises_and_rolls_back_the_deck_write(conn):
    _claim(conn, quote_hash=H1, day=DAY1)
    with pytest.raises(sqlite3.IntegrityError):
        _claim(conn, quote_hash=H2, day=None, reshuffle=True, force=True, m=1)
    assert set(_rows(conn)) == {(WILDE, H1)}
    assert repo.get_lounge_state(conn).last_quote_date == DAY1
    assert conn.in_transaction is False


# --- last_hash determinism ---


def test_equal_timestamps_break_ties_by_insertion_order(conn):
    _claim(conn, quote_hash=H1, day=DAY1, m=0)
    _claim(conn, quote_hash=H2, day=DAY1, force=True, m=0)
    _claim(conn, quote_hash=H3, day=DAY1, force=True, m=0)
    assert repo.quote_deck_state(conn, WILDE).last_hash == H3


def test_reclaiming_an_old_hash_in_the_same_tick_makes_it_the_latest_again(conn):
    _claim(conn, quote_hash=H1, day=DAY1, m=0)
    _claim(conn, quote_hash=H2, day=DAY1, force=True, m=0)
    _claim(conn, quote_hash=H1, day=DAY1, force=True, m=0)
    assert repo.quote_deck_state(conn, WILDE).last_hash == H1


def test_a_non_utc_now_still_orders_by_instant(conn):
    pdt = timezone(timedelta(hours=-7))
    _claim(conn, quote_hash=H1, day=DAY1, now=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC))
    _claim(
        conn,
        quote_hash=H2,
        day=DAY1,
        force=True,
        now=lambda: datetime(2026, 9, 29, 6, tzinfo=pdt),  # 13:00 UTC, later
    )
    assert repo.quote_deck_state(conn, WILDE).last_hash == H2


def test_reclaiming_an_old_hash_at_a_later_time_makes_it_the_latest(conn):
    _claim(conn, quote_hash=H1, day=DAY1, m=0)
    _claim(conn, quote_hash=H2, day=DAY1, force=True, m=1)
    _claim(conn, quote_hash=H1, day=DAY1, force=True, m=2)
    deck = repo.quote_deck_state(conn, WILDE)
    assert deck.last_hash == H1
    assert deck.used == frozenset({H1, H2})


# --- keys and hashes ---


def test_unicode_keys_round_trip(conn):
    key = "wikiquote:Gabriel García Márquez \U0001f4da 東京"
    assert _claim(conn, key) is True
    assert repo.quote_deck_state(conn, key).used == frozenset({H1})


def test_keys_differing_only_in_case_or_trailing_space_are_distinct_decks(conn):
    # Documented: identity is exact text. Normalizing keys is the caller's job.
    for i, key in enumerate(["file:/q.txt", "FILE:/q.txt", "file:/q.txt ", " file:/q.txt"]):
        assert _claim(conn, key, H1, DAY1, force=True, m=i) is True
    assert len(_rows(conn)) == 4
    assert repo.quote_deck_state(conn, "file:/q.txt").used == frozenset({H1})
    assert repo.quote_deck_state(conn, "File:/q.txt").used == frozenset()


def test_a_reshuffle_on_one_spelling_leaves_the_other_spellings_alone(conn):
    _claim(conn, "file:/q.txt", H1, DAY1)
    _claim(conn, "file:/q.txt ", H2, DAY1, force=True, m=1)
    _claim(conn, "file:/q.txt", H3, DAY2, reshuffle=True, m=2)
    assert set(_rows(conn)) == {("file:/q.txt", H3), ("file:/q.txt ", H2)}


def test_key_at_the_4096_bound_is_accepted_and_one_past_it_is_rejected_cleanly(conn):
    ok = "k" * 4096
    assert _claim(conn, ok) is True
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _claim(conn, "k" * 4097, quote_hash=H2, day=DAY2, m=1)
    assert set(_rows(conn)) == {(ok, H1)}
    assert repo.get_lounge_state(conn).last_quote_date == DAY1


def test_the_4096_bound_counts_characters_not_bytes(conn):
    key = "\U0001f4da" * 4096  # 16 KiB of UTF-8, 4096 characters
    assert _claim(conn, key) is True


def test_a_key_with_an_embedded_nul_is_measured_up_to_the_nul(conn):
    # Documented SQLite quirk: length() of TEXT stops at the first NUL, so a
    # key that STARTS with NUL measures 0 and is rejected by the CHECK, while
    # "a\0b" measures 1 and is accepted (and round-trips as the whole
    # string). No real source key has a NUL; this is here so nobody assumes
    # the CHECK is a byte-exact bound.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _claim(conn, "\x00lead")
    assert _claim(conn, "a\x00b", day=DAY1) is True
    assert repo.quote_deck_state(conn, "a\x00b").used == frozenset({H1})


def test_like_wildcards_in_a_key_do_not_match_other_decks(conn):
    _claim(conn, "wikiquote:Alpha", H1, DAY1)
    _claim(conn, "%", H2, DAY1, force=True, m=1)
    assert repo.quote_deck_state(conn, "%").used == frozenset({H2})
    assert repo.quote_deck_state(conn, "wikiquote:%").used == frozenset()
    assert repo.quote_deck_state(conn, "_").used == frozenset()


def test_64_char_non_hex_hash_is_accepted_by_the_check(conn):
    # Documented: the schema only checks length, not "is hex".
    assert _claim(conn, quote_hash="z" * 64) is True
    assert repo.quote_deck_state(conn, WILDE).last_hash == "z" * 64


def test_uppercase_and_lowercase_spellings_of_one_digest_are_two_quotes(conn):
    # Documented: hashes are compared as text. hashlib's hexdigest() is
    # always lowercase, so this only bites a caller who upper-cases it.
    lower, upper = "abcdef0123456789" * 4, "ABCDEF0123456789" * 4
    _claim(conn, quote_hash=lower, day=DAY1)
    _claim(conn, quote_hash=upper, day=DAY1, force=True, m=1)
    assert repo.quote_deck_state(conn, WILDE).used == frozenset({lower, upper})


def test_non_string_hash_of_the_wrong_length_never_lands(conn):
    with pytest.raises((sqlite3.IntegrityError, sqlite3.ProgrammingError, sqlite3.InterfaceError)):
        _claim(conn, quote_hash="a" * 63)
    assert _rows(conn) == {}
    assert repo.get_lounge_state(conn).last_quote_date is None


# --- decks at scale ---


def test_a_deck_of_five_thousand_stays_correct(conn):
    hashes = [f"{i:064x}" for i in range(5000)]
    conn.executemany(
        "INSERT INTO lounge_quotes_used VALUES (?, ?, ?)",
        [(WILDE, h, (T0 + timedelta(seconds=i)).isoformat()) for i, h in enumerate(hashes)],
    )
    conn.execute("INSERT INTO lounge_quotes_used VALUES (?, ?, ?)", (TWAIN, H1, T0.isoformat()))
    conn.commit()

    deck = repo.quote_deck_state(conn, WILDE)
    assert len(deck.used) == 5000
    assert deck.last_hash == hashes[-1]

    assert _claim(conn, quote_hash=H2, day=DAY1, reshuffle=True, m=10_000) is True
    assert set(_rows(conn)) == {(WILDE, H2), (TWAIN, H1)}
