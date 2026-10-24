"""QA round 2, areas 7 and 8: the stamp floor and the stale-009 check (batch d36d492).

Area 7. Every new `details_changed_at` is lifted above the largest stamp in the table, so a
message's version stamp (the largest among its codes) can be compared with any code's. The
attacks: a garbled maximum (text order is not time order once a row is hand-edited), a floor
that sits far in the future (clock skew), and what an edit pass makes of stamps that have run
ahead of the wall clock. The answer to the last one, pinned below, is "nothing odd": stamps are
only ever compared with other stamps, never with a clock.

Area 8. Migration 009 was amended in place during the build, so a development database that ran
an earlier draft says `user_version = 9` and has no columns the code needs. Startup must refuse
it with a message that names what's missing. The two earlier drafts that were ever committed
(`bd6cf46`, `fb3dc40`) are kept as SQL fixtures here, with their comments stripped, so this
doesn't depend on git history being around.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_shift_delivery_details import BIG_RESERVE
from test_shift_edits import EditHarness, learn
from test_shift_fanout import CODE_A, CODE_B, NOW, V3, add_guild, item, seed

from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.shift.details import DetailObservation
from newsbot.store import db, repo
from newsbot.store.db import StoreError, connect, migrate

HOUR = timedelta(hours=1)
T = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
C = "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"
DRAFTS = Path(__file__).parent / "fixtures" / "migration_009_drafts"


def obs(code: str, reward: str) -> DetailObservation:
    return DetailObservation(
        code, ItemDetails(code=code, rank="table", reward=reward, listed=True), "mentalmars", None
    )


def stamps(db_path) -> dict[str, str]:
    with closing(connect(db_path)) as conn:
        return {
            r[0]: r[1] for r in conn.execute("SELECT code, details_changed_at FROM code_details")
        }


def stamp_of(db_path, code) -> datetime:
    return datetime.fromisoformat(stamps(db_path)[code])


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def put(db_path, code, reward, *, now):
    with closing(connect(db_path)) as conn:
        return repo.upsert_code_details(conn, [obs(code, reward)], now=now)


def overwrite_stamp(db_path, code, value):
    with closing(connect(db_path)) as conn, conn:
        conn.execute("UPDATE code_details SET details_changed_at = ? WHERE code = ?", (value, code))


# =====================================================================================
# Area 7: the stamp floor
# =====================================================================================


@pytest.mark.parametrize("garbage", ["zzzz", "NULL", "2026-13-45T00:00:00+00:00", "2027-01-01x"])
def test_a_garbled_maximum_does_not_stop_a_change_being_recorded(db_path, garbage, caplog):
    put(db_path, A, "1 key", now=T + HOUR)
    put(db_path, B, "2 keys", now=T + HOUR)
    overwrite_stamp(db_path, B, garbage)

    with caplog.at_level(logging.WARNING, logger="newsbot.store.repo"):
        changed = put(db_path, C, "3 keys", now=T)

    assert changed == [C]
    assert any("isn't a timestamp" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "garbage",
    ["zzzz", "NULL", "2026-13-45T00:00:00+00:00", "2027-01-01x", "2026-10-09T14:00:00+09:00"],
)
def test_a_garbled_or_oddly_spelled_stamp_does_not_disable_the_floor(db_path, garbage):
    put(db_path, A, "1 key", now=T + HOUR)
    put(db_path, B, "2 keys", now=T + HOUR)
    overwrite_stamp(db_path, B, garbage)

    put(db_path, C, "3 keys", now=T)  # the clock stepped back an hour

    assert stamp_of(db_path, C) > stamp_of(db_path, A)


@pytest.mark.parametrize("quiet_garbage", ["", "0000-00-00", "1999-01-01T00:00:00+00:00"])
def test_a_garbled_stamp_that_sorts_below_the_real_ones_leaves_the_floor_working(
    db_path, quiet_garbage
):
    put(db_path, A, "1 key", now=T + HOUR)
    put(db_path, B, "2 keys", now=T + HOUR)
    overwrite_stamp(db_path, B, quiet_garbage)

    put(db_path, C, "3 keys", now=T)

    assert stamp_of(db_path, C) > stamp_of(db_path, A)


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-10-09 14:00:00",
        "2026-10-09T14:00:00",
        "2026-10-09T14:00:00Z",
        "2026-10-09T07:00:00-07:00",
    ],
)
def test_other_spellings_of_a_real_instant_are_still_a_floor(db_path, stamp):
    put(db_path, A, "1 key", now=T)
    overwrite_stamp(db_path, A, stamp)

    put(db_path, C, "3 keys", now=T)

    assert stamp_of(db_path, C) > datetime(2026, 10, 9, 14, 0, tzinfo=UTC)


def test_a_floor_a_year_ahead_lifts_every_later_stamp_by_one_microsecond_each(db_path):
    far = T + timedelta(days=365)
    put(db_path, A, "1 key", now=T)
    overwrite_stamp(db_path, A, far.isoformat())

    for n, code in enumerate((B, C), start=1):
        put(db_path, code, f"{n} keys", now=T + n * HOUR)
        assert stamp_of(db_path, code) == far + n * timedelta(microseconds=1)


def test_a_far_future_floor_makes_one_pass_of_changes_strictly_increasing(db_path):
    put(db_path, A, "1 key", now=T)
    overwrite_stamp(db_path, A, (T + timedelta(days=3650)).isoformat())

    with closing(connect(db_path)) as conn:
        changed = repo.upsert_code_details(conn, [obs(B, "2 keys"), obs(C, "3 keys")], now=T)

    assert sorted(changed) == [B, C]
    assert len({stamp_of(db_path, c) for c in (A, B, C)}) == 3
    assert stamp_of(db_path, B) < stamp_of(db_path, C)
    assert stamp_of(db_path, A) < stamp_of(db_path, B)


def test_a_stamp_at_the_end_of_time_does_not_crash_the_upsert(db_path):
    put(db_path, A, "1 key", now=T)
    overwrite_stamp(db_path, A, "9999-12-31T23:59:59.999999+00:00")

    put(db_path, B, "2 keys", now=T)  # must not raise


@pytest.fixture
def h(db_path):
    harness = EditHarness(db_path, load_config(V3), NOW)
    seed(db_path)
    return harness


async def test_stamps_that_ran_ahead_of_the_wall_clock_still_send_every_change_to_its_message(h):
    """The rendered_at question: stamps are compared with stamps, never with a clock."""
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE  # A and B share one message
    await h.run([item(CODE_A), item(CODE_B, url="https://e/b")])
    far = NOW + timedelta(days=3650)

    learn(h, CODE_A, "1 Golden Key", at=NOW + HOUR)
    overwrite_stamp(h.db_path, CODE_A, far.isoformat())  # a clock that once ran ten years ahead
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 1  # A's stamp (far future) is newer than the message's epoch

    # Now the wall clock is far behind every stamp. B's change must still show up.
    learn(h, CODE_B, "2 Golden Keys", at=NOW + 3 * HOUR)
    assert stamp_of(h.db_path, CODE_B) > far
    h.clock = NOW + 4 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 2
    assert "2 Golden Keys" in h.editors.calls[-1].content

    # And with nothing new, nothing more is sent however the stamps compare with the clock.
    h.clock = NOW + 5 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 2


async def test_the_message_records_the_largest_stamp_it_showed_even_when_that_is_in_the_future(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    far = NOW + timedelta(days=3650)
    learn(h, CODE_A, "1 Golden Key", at=NOW + HOUR)
    overwrite_stamp(h.db_path, CODE_A, far.isoformat())
    h.clock = NOW + 2 * HOUR
    await h.run([])

    with closing(connect(h.db_path)) as conn:
        ((rendered_at,),) = conn.execute("SELECT rendered_at FROM guild_alert_messages")
    assert datetime.fromisoformat(rendered_at) == far


# =====================================================================================
# Area 8: a database from an earlier draft of 009
# =====================================================================================

# What each committed draft lacks, against the columns the code now needs.
DRAFT_MISSING = {
    "bd6cf46": [
        "guild_alert_messages.edit_tried_at",
        "guild_alert_messages.ping",
        "alerted_codes.official_seen_at",
        "alerted_codes.table_silenced_at",
    ],
    "fb3dc40": [
        "guild_alert_messages.ping",
        "alerted_codes.official_seen_at",
        "alerted_codes.table_silenced_at",
    ],
}


@pytest.fixture
def stale_db(tmp_path, monkeypatch, migrations_up_to):
    """A factory: migrations 1 to 8 plus the named draft of 009, at version 9."""

    def build(draft: str) -> str:
        path = str(tmp_path / f"stale-{draft}.db")
        with monkeypatch.context() as m:
            m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / f"m8-{draft}", 8))
            with closing(connect(path)) as conn:
                assert migrate(conn) == 8
        with closing(connect(path)) as conn:
            conn.executescript((DRAFTS / f"{draft}.sql").read_text())
            conn.execute("PRAGMA user_version = 9")
        return path

    return build


def schema_of(path) -> list[tuple]:
    with closing(connect(path)) as conn:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


@pytest.mark.parametrize("draft", sorted(DRAFT_MISSING))
def test_a_database_from_each_earlier_draft_is_refused_with_exactly_what_it_lacks(stale_db, draft):
    path = stale_db(draft)

    with closing(connect(path)) as conn:
        with pytest.raises(StoreError) as caught:
            migrate(conn)

    message = str(caught.value)
    named = {entry for entry in DRAFT_MISSING[draft] if entry in message}
    assert named == set(DRAFT_MISSING[draft])
    for other in {e for v in DRAFT_MISSING.values() for e in v} - set(DRAFT_MISSING[draft]):
        assert other not in message
    assert "start the database fresh" in message


@pytest.mark.parametrize("draft", sorted(DRAFT_MISSING))
def test_refusing_a_draft_database_changes_nothing_in_it(stale_db, draft):
    path = stale_db(draft)
    before = schema_of(path)
    with closing(connect(path)) as conn:
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]

    with closing(connect(path)) as conn, pytest.raises(StoreError):
        migrate(conn)

    assert schema_of(path) == before
    with closing(connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version_before == 9


@pytest.mark.parametrize("draft", sorted(DRAFT_MISSING))
def test_a_draft_database_is_refused_every_time_not_just_the_first(stale_db, draft):
    path = stale_db(draft)
    for _ in range(3):
        with closing(connect(path)) as conn, pytest.raises(StoreError):
            migrate(conn)


@pytest.mark.parametrize("draft", sorted(DRAFT_MISSING))
def test_a_draft_database_with_rows_in_it_is_still_refused_and_keeps_them(stale_db, draft):
    path = stale_db(draft)
    with closing(connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO code_details (code, clock_start, updated_at, reward, reward_source) "
            "VALUES (?, 'n', 'n', '1 key', 'table')",
            (A,),
        )

    with closing(connect(path)) as conn, pytest.raises(StoreError):
        migrate(conn)

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT reward FROM code_details").fetchall()[0][0] == "1 key"


def test_the_drafts_are_what_the_fixtures_say_and_the_final_migration_fixes_what_they_lacked(
    stale_db, tmp_path
):
    """Guard the fixtures themselves: they must lack exactly the listed columns, and the
    final migration must have every one of them (or this test file proves nothing)."""
    for draft, missing in DRAFT_MISSING.items():
        path = stale_db(draft)
        with closing(connect(path)) as conn:
            for entry in missing:
                table, column = entry.split(".")
                names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
                assert column not in names, (draft, entry)
    fresh = str(tmp_path / "fresh.db")
    with closing(connect(fresh)) as conn:
        assert migrate(conn) == 10
        for entry in DRAFT_MISSING["bd6cf46"]:
            table, column = entry.split(".")
            assert column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608


def test_a_fresh_database_passes_and_so_does_one_migrated_from_version_8(
    tmp_path, migrations_up_to, monkeypatch
):
    fresh = str(tmp_path / "fresh.db")
    with closing(connect(fresh)) as conn:
        assert migrate(conn) == 10

    upgraded = str(tmp_path / "upgraded.db")
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "m8", 8))
        with closing(connect(upgraded)) as conn:
            assert migrate(conn) == 8
    with closing(connect(upgraded)) as conn:
        assert migrate(conn) == 10
        assert migrate(conn) == 10


def test_a_database_newer_than_009_is_checked_too(stale_db):
    path = stale_db("fb3dc40")
    with closing(connect(path)) as conn:
        conn.execute("PRAGMA user_version = 12")
    with closing(connect(path)) as conn, pytest.raises(StoreError):
        migrate(conn)


def test_a_missing_table_is_reported_as_missing_columns_not_a_crash(
    tmp_path, migrations_up_to, monkeypatch
):
    path = str(tmp_path / "odd.db")
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "m8", 8))
        with closing(connect(path)) as conn:
            migrate(conn)
    with closing(connect(path)) as conn:
        conn.execute("PRAGMA user_version = 9")  # claims 009 without having run it
    with closing(connect(path)) as conn:
        with pytest.raises(StoreError) as caught:
            migrate(conn)
    assert "guild_alert_messages.ping" in str(caught.value)


def test_the_refusal_is_a_plain_store_error_that_says_which_migration_it_is_about(stale_db):
    path = stale_db("fb3dc40")
    with closing(connect(path)) as conn, pytest.raises(StoreError, match="migration 009"):
        migrate(conn)
