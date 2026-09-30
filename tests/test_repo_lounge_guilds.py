"""The per-guild lounge deck and date guard (design.md §15, plan task 12).

`claim_quote` and `quote_deck_state` grew a `guild_id`. Without one they are
v2.2's single lounge, unchanged (tests/test_repo_lounge.py). With one, the
date guard is the guild's own `guild_lounge.last_quote_date`, the deck only
counts that guild's rows plus the unclaimed ones, and `lounge_state` is still
written so a rollback to v2.2 finds a current date.
"""

import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings

G1, G2 = 100000000000000001, 100000000000000002
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
        for gid in (G1, G2):
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


def _guild_date(conn, gid):
    return repo.get_lounge(conn, gid).last_quote_date


def test_a_claim_writes_the_guilds_date_stamps_the_row_and_mirrors_lounge_state(conn):
    conn.execute("UPDATE guilds SET imported_at = ? WHERE guild_id = ?", (T0.isoformat(), G1))
    conn.commit()
    assert _claim(conn, G1) is True
    assert _guild_date(conn, G1) == DAY1
    assert _guild_date(conn, G2) is None
    assert repo.get_lounge_state(conn).last_quote_date == DAY1  # the rollback mirror
    row = conn.execute("SELECT guild_id FROM lounge_quotes_used").fetchone()
    assert row["guild_id"] == G1


def test_the_date_guard_is_per_guild(conn):
    assert _claim(conn, G1) is True
    assert _claim(conn, G1, h=H2) is False  # same guild, same day
    assert _claim(conn, G2, key=TWAIN) is True  # another guild's day is its own
    assert _claim(conn, G1, h=H2, day=DAY2) is True


def test_a_refused_claim_writes_nothing(conn):
    _claim(conn, G1)
    before = [tuple(r) for r in conn.execute("SELECT * FROM lounge_quotes_used")]
    assert _claim(conn, G1, h=H2) is False
    assert [tuple(r) for r in conn.execute("SELECT * FROM lounge_quotes_used")] == before
    assert conn.in_transaction is False


def test_force_skips_the_guild_guard_and_still_records(conn):
    _claim(conn, G1)
    assert _claim(conn, G1, h=H2, force=True) is True
    deck = repo.quote_deck_state(conn, WILDE, G1)
    assert deck.used == {H1, H2} and deck.last_hash == H2


def test_the_deck_is_scoped_by_guild(conn):
    _claim(conn, G1, h=H1)
    _claim(conn, G2, key=WILDE, h=H2)
    assert repo.quote_deck_state(conn, WILDE, G1).used >= {H1}
    # Same source key under two guilds shares the table's (source_key, quote_hash) key,
    # but each deck only reads its own rows.
    assert H2 not in repo.quote_deck_state(conn, WILDE, G1).used
    assert H1 not in repo.quote_deck_state(conn, WILDE, G2).used
    assert repo.quote_deck_state(conn, WILDE).used == {H1, H2}  # v2 reading: everything


def test_another_guilds_source_is_an_empty_deck(conn):
    _claim(conn, G1, key=WILDE)
    deck = repo.quote_deck_state(conn, WILDE, G2)
    assert deck.used == frozenset() and deck.last_hash is None


def test_unclaimed_rows_belong_to_every_deck_until_the_import_stamps_them(conn):
    conn.execute(
        "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at, guild_id) "
        "VALUES (?, ?, ?, NULL)",
        (WILDE, H3, "2026-09-28T15:00:00+00:00"),
    )
    conn.commit()
    assert repo.quote_deck_state(conn, WILDE, G1).used == {H3}
    assert repo.quote_deck_state(conn, WILDE, G1).last_hash == H3


def test_reshuffle_deletes_only_that_guilds_rows_and_the_unclaimed_ones(conn):
    conn.execute(
        "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at, guild_id) "
        "VALUES (?, ?, ?, NULL)",
        (WILDE, H3, "2026-09-28T15:00:00+00:00"),
    )
    conn.commit()
    _claim(conn, G2, key=WILDE, h=H2)  # G2's row for the same source
    _claim(conn, G1, key=WILDE, h=H1, reshuffle=True)
    rows = {
        r["quote_hash"]: r["guild_id"] for r in conn.execute("SELECT * FROM lounge_quotes_used")
    }
    assert rows == {H2: G2, H1: G1}  # H3 (unclaimed) went with the reshuffle; G2's stayed


def test_a_guild_with_no_lounge_row_is_refused_and_nothing_is_written(conn):
    with conn:
        conn.execute("DELETE FROM guild_lounge WHERE guild_id = ?", (G1,))
    assert _claim(conn, G1) is False
    assert conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 0
    assert repo.get_lounge_state(conn).last_quote_date is None
    assert conn.in_transaction is False


def test_a_guild_with_no_row_at_all_is_refused_not_a_foreign_key_error(conn):
    assert _claim(conn, 999) is False


def test_the_v2_reading_is_untouched_by_the_guild_arguments(conn):
    assert repo.claim_quote(
        conn, source_key=WILDE, quote_hash=H1, local_day=DAY1, reshuffle=False, force=False
    )
    assert repo.get_lounge_state(conn).last_quote_date == DAY1
    assert _guild_date(conn, G1) is None  # v2's path never touches a guild row
    assert conn.execute("SELECT guild_id FROM lounge_quotes_used").fetchone()["guild_id"] is None


def test_two_connections_racing_one_guilds_day_exactly_one_wins(db_path):
    results = []
    barrier = threading.Barrier(2)

    def worker(h):
        with closing(connect(db_path)) as c:
            barrier.wait()
            results.append(_claim(c, G1, h=h))

    threads = [threading.Thread(target=worker, args=(h,)) for h in (H1, H2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]


def test_deleting_a_guild_takes_its_deck_with_it(conn):
    _claim(conn, G1)
    repo.delete_guild(conn, G1)
    assert conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 0
