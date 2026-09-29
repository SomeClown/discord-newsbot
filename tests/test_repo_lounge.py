"""Tests for the lounge repo functions (plan task 2, design.md §14)."""

import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

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


def _claim(conn, source_key=WILDE, quote_hash=H1, day=DAY1, *, reshuffle=False, force=False, m=0):
    return repo.claim_quote(
        conn,
        source_key=source_key,
        quote_hash=quote_hash,
        local_day=day,
        reshuffle=reshuffle,
        force=force,
        now=_clock(m),
    )


def _rows(conn):
    return {
        (r["source_key"], r["quote_hash"]): r["used_at"]
        for r in conn.execute("SELECT source_key, quote_hash, used_at FROM lounge_quotes_used")
    }


def test_empty_database_reads_as_no_state_and_an_empty_deck(conn):
    assert repo.get_lounge_state(conn).last_quote_date is None
    deck = repo.quote_deck_state(conn, WILDE)
    assert deck.used == frozenset()
    assert deck.last_hash is None


def test_claim_round_trips_state_and_deck(conn):
    assert _claim(conn) is True
    assert repo.get_lounge_state(conn).last_quote_date == DAY1
    deck = repo.quote_deck_state(conn, WILDE)
    assert deck.used == frozenset({H1})
    assert deck.last_hash == H1


def test_same_day_is_refused_and_writes_nothing(conn):
    assert _claim(conn, quote_hash=H1) is True
    assert _claim(conn, quote_hash=H2, m=1) is False
    assert set(_rows(conn)) == {(WILDE, H1)}


def test_next_day_is_allowed(conn):
    assert _claim(conn, quote_hash=H1, day=DAY1) is True
    assert _claim(conn, quote_hash=H2, day=DAY2, m=1) is True
    assert repo.get_lounge_state(conn).last_quote_date == DAY2


def test_force_bypasses_the_date_guard(conn):
    assert _claim(conn, quote_hash=H1) is True
    assert _claim(conn, quote_hash=H2, force=True, m=1) is True
    assert repo.quote_deck_state(conn, WILDE).used == frozenset({H1, H2})


def test_refusal_leaves_the_connection_usable(conn):
    assert _claim(conn) is True
    assert _claim(conn, quote_hash=H2) is False
    assert conn.in_transaction is False
    assert conn.isolation_level == ""
    assert _claim(conn, quote_hash=H2, day=DAY2, m=1) is True


def test_last_hash_is_the_most_recently_used(conn):
    _claim(conn, quote_hash=H1, day=DAY1, m=0)
    _claim(conn, quote_hash=H2, day=DAY2, m=5)
    assert repo.quote_deck_state(conn, WILDE).last_hash == H2


def test_reclaiming_a_hash_updates_used_at_and_last_hash(conn):
    _claim(conn, quote_hash=H1, day=DAY1, m=0)
    _claim(conn, quote_hash=H2, day=DAY2, m=5)
    _claim(conn, quote_hash=H1, force=True, m=10)
    rows = _rows(conn)
    assert len(rows) == 2
    assert rows[(WILDE, H1)] == _clock(10)().isoformat()
    assert repo.quote_deck_state(conn, WILDE).last_hash == H1


def test_reshuffle_clears_only_that_sources_rows(conn):
    _claim(conn, WILDE, H1, DAY1)
    _claim(conn, WILDE, H2, DAY2, m=1)
    _claim(conn, TWAIN, H3, DAY2, force=True, m=2)

    assert _claim(conn, WILDE, H3, "2026-10-01", reshuffle=True, m=3) is True

    assert set(_rows(conn)) == {(WILDE, H3), (TWAIN, H3)}
    assert repo.quote_deck_state(conn, TWAIN).used == frozenset({H3})
    assert repo.quote_deck_state(conn, WILDE).used == frozenset({H3})


def test_refused_reshuffle_deletes_nothing(conn):
    _claim(conn, WILDE, H1, DAY1)
    assert _claim(conn, WILDE, H2, DAY1, reshuffle=True, m=1) is False
    assert set(_rows(conn)) == {(WILDE, H1)}


def test_same_hash_under_two_keys_is_two_rows(conn):
    _claim(conn, WILDE, H1, DAY1)
    _claim(conn, TWAIN, H1, DAY1, force=True, m=1)
    assert set(_rows(conn)) == {(WILDE, H1), (TWAIN, H1)}


def test_last_hash_is_per_source(conn):
    _claim(conn, WILDE, H1, DAY1, m=0)
    _claim(conn, TWAIN, H2, DAY1, force=True, m=5)
    assert repo.quote_deck_state(conn, WILDE).last_hash == H1
    assert repo.quote_deck_state(conn, TWAIN).last_hash == H2


def test_bad_hash_raises_and_rolls_back_the_whole_claim(conn):
    with pytest.raises(Exception, match="CHECK"):
        _claim(conn, quote_hash="short", reshuffle=True)
    assert repo.get_lounge_state(conn).last_quote_date is None
    assert conn.in_transaction is False


def test_purge_older_than_leaves_the_lounge_tables_alone(conn):
    _claim(conn)
    repo.purge_older_than(conn, datetime(2100, 1, 1, tzinfo=UTC))
    assert set(_rows(conn)) == {(WILDE, H1)}
    assert repo.get_lounge_state(conn).last_quote_date == DAY1


def test_sql_metacharacters_in_the_key_are_just_text(conn):
    nasty = "file:/tmp/x'; DROP TABLE lounge_state; --"
    assert _claim(conn, nasty) is True
    assert repo.quote_deck_state(conn, nasty).used == frozenset({H1})
    assert repo.get_lounge_state(conn).last_quote_date == DAY1


def test_two_connections_racing_the_same_day_exactly_one_wins(db_path):
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def racer(quote_hash: str) -> None:
        try:
            with closing(connect(db_path)) as c:
                barrier.wait(timeout=5)
                won = repo.claim_quote(
                    c,
                    source_key=WILDE,
                    quote_hash=quote_hash,
                    local_day=DAY1,
                    reshuffle=False,
                    force=False,
                )
                results.append(won)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=racer, args=(h,)) for h in (H1, H2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert sorted(results) == [False, True]
    with closing(connect(db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 1
