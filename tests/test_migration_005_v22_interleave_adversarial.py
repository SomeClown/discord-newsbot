"""v2.2.0 and v3 taking turns on one database (the rollback path, harder).

test_migration_005_v22_compat.py proves v2.2's code runs against a migrated
file. This one is about the awkward part: rolling back is a TAG change, so
there will be days when v3 ran, then v2.2 ran, then v3 comes back and has to
make sense of what v2.2 left. Those days are built here out of the real
v2.2 module (the checked-in snapshot) and the real v3 repo functions, taking
turns on the same file.

Several of these pin behavior I'd call "safe but surprising", and they're
labelled "documented" because the owner may want a different answer. The big
one: once a stranger's server has a digest row for a day, a rolled-back v2.2
can't tell it isn't the friend's. It either refuses to run (polite) or, if
forced, borrows the stranger's row (less polite). Rolling back after going
public is a real decision, and the runbook should say so.
"""

from __future__ import annotations

import threading
import types
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import StoredItem, StoryToSave, Usage

SNAPSHOT = Path(__file__).parent / "fixtures" / "v22_repo_snapshot.txt"
NOW = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
FRIEND = 42
STRANGER = 77


@pytest.fixture(scope="module")
def v22() -> types.ModuleType:
    module = types.ModuleType("v22_repo_interleave")
    exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
    return module


@pytest.fixture
def conn(v22_db):
    with closing(connect(v22_db)) as c:
        yield c


def _clock(moment=NOW):
    return lambda: moment


def _healthy(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for name in ("stories_fts", "items_fts"):
        conn.execute(f"INSERT INTO {name} ({name}) VALUES ('integrity-check')")  # noqa: S608


def _v3_digest(conn, guild_id, run_date, status="ok", message_ids="[1]"):
    with conn:
        cur = conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 'v3', 'v3')",
            (guild_id, run_date, status, message_ids),
        )
    return cur.lastrowid


def _v22_run(v22, conn, day, *, force=False, url="https://example.com/v22-day", title="Quartz"):
    """One v2.2 day: claim, then save a run with one item and one story."""
    digest_id = v22.claim_digest(conn, day, force=force, now=_clock())
    if digest_id is None:
        return None
    v22.save_run(
        conn,
        digest_id,
        [
            StoredItem(
                url=url,
                title=f"{title} announced for Palworld",
                excerpt="v2.2 wrote this",
                source_name="Feed",
                trust="press",
                published_at=NOW,
                topics={"palworld": False},
            )
        ],
        [StoryToSave("palworld", f"{title} story", "Body.", "reported", [url], None)],
        "ok",
        [555],
        None,
        Usage(10, 5),
        now=_clock(),
    )
    return digest_id


# --- v3, then v2.2, then v3 again ---


def test_v3_day_then_a_v22_day_then_v3_adopts_the_orphan_and_nothing_else_moves(v22, conn):
    repo.create_guild(conn, FRIEND, tier="comped", set_up=True, imported_at=NOW)
    assert repo.adopt_orphan_digests(conn, FRIEND) == 4  # the import adopts v2.2's history
    v3_id = _v3_digest(conn, FRIEND, "2026-10-01")
    with conn:
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES ('borderlands4', 'v3 story', 'v3 body', 'official', ?, 'v3-time')",
            (v3_id,),
        )

    # v3 is rolled back; v2.2 runs the next day. Its own day guard works: a day v3 already
    # posted is refused, a fresh day is claimed.
    assert v22.claim_digest(conn, date(2026, 10, 1), force=False, now=_clock()) is None
    v22_id = _v22_run(v22, conn, date(2026, 10, 2))
    assert v22_id is not None
    assert (
        conn.execute("SELECT guild_id FROM digests WHERE id = ?", (v22_id,)).fetchone()[0] is None
    )

    # v3 comes back.
    assert repo.adopt_orphan_digests(conn, FRIEND) == 1
    assert repo.adopt_orphan_digests(conn, FRIEND) == 0  # idempotent
    rows = conn.execute("SELECT id, guild_id, run_date FROM digests ORDER BY run_date").fetchall()
    assert [r["guild_id"] for r in rows] == [FRIEND] * 6
    assert len({r["run_date"] for r in rows}) == 6
    # The v2.2 story kept its digest link through the adoption; the v3 story is untouched.
    assert (
        conn.execute("SELECT digest_id FROM stories WHERE headline = 'Quartz story'").fetchone()[0]
        == v22_id
    )
    assert (
        conn.execute("SELECT digest_id FROM stories WHERE headline = 'v3 story'").fetchone()[0]
        == v3_id
    )
    # And v3 can't now insert a second digest for a day v2.2 covered.
    with pytest.raises(Exception, match="UNIQUE"):
        _v3_digest(conn, FRIEND, "2026-10-02")
    _healthy(conn)


def test_a_v22_day_survives_while_v3_keeps_a_stranger_running_on_other_days(v22, conn):
    repo.create_guild(conn, STRANGER)
    _v3_digest(conn, STRANGER, "2026-09-30")  # same date as the friend's old v2.2 digest
    assert (
        conn.execute("SELECT COUNT(*) FROM digests WHERE run_date = '2026-09-30'").fetchone()[0]
        == 2
    )
    # v2.2's own same-day guard still sees 'ok' for that date and refuses.
    assert v22.claim_digest(conn, date(2026, 9, 30), force=False, now=_clock()) is None
    _healthy(conn)


def test_a_pending_orphan_from_a_crashed_v22_run_is_adopted_as_pending(v22, conn):
    # Documented: adoption copies the row as it stands, status included. A v2.2 run that
    # died mid-day leaves 'pending', and that's what v3's claim logic will meet.
    digest_id = v22.claim_digest(conn, date(2026, 10, 2), force=False, now=_clock())
    repo.create_guild(conn, FRIEND, tier="comped", imported_at=NOW)
    repo.adopt_orphan_digests(conn, FRIEND)
    row = conn.execute("SELECT guild_id, status FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert (row["guild_id"], row["status"]) == (FRIEND, "pending")


def test_v22_forcing_a_day_v3_already_has_reuses_the_guild_row_in_place(v22, conn):
    # Documented: `--force` on v2.2 doesn't know about guilds. It updates the one row it finds
    # for that date, guild_id and all, so the day stays the friend's and adopt has nothing to do.
    repo.create_guild(conn, FRIEND, tier="comped", imported_at=NOW)
    v3_id = _v3_digest(conn, FRIEND, "2026-10-02", status="failed")
    assert _v22_run(v22, conn, date(2026, 10, 2)) == v3_id  # 'failed' also allows a plain reclaim
    row = conn.execute(
        "SELECT guild_id, status, posted_message_ids FROM digests WHERE id = ?", (v3_id,)
    ).fetchone()
    assert (row["guild_id"], row["status"], row["posted_message_ids"]) == (FRIEND, "ok", "[555]")
    assert (
        conn.execute("SELECT COUNT(*) FROM digests WHERE run_date = '2026-10-02'").fetchone()[0]
        == 1
    )
    _healthy(conn)


# --- a stranger's digest and the friend's rolled-back v2.2 day ---


def test_a_strangers_ok_digest_blocks_the_friends_v22_day(v22, conn):
    # Documented: rolling back after other servers have digests means v2.2, which only
    # asks "is there a row for this date", skips the friend's day entirely. Safe (no double
    # post), but the friend gets no digest until someone forces it or the date rolls over.
    repo.create_guild(conn, STRANGER)
    _v3_digest(conn, STRANGER, "2026-10-02")
    assert v22.claim_digest(conn, date(2026, 10, 2), force=False, now=_clock()) is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM digests WHERE guild_id IS NULL AND run_date = '2026-10-02'"
        ).fetchone()[0]
        == 0
    )
    assert v22.get_digest(conn, date(2026, 10, 2)).status == "ok"  # it "sees" the stranger's row


def test_a_strangers_failed_digest_is_reclaimed_by_v22_as_if_it_were_the_friends(v22, conn):
    # Documented: same blindness in the other direction. A failed stranger row gets taken over.
    repo.create_guild(conn, STRANGER)
    stranger_id = _v3_digest(conn, STRANGER, "2026-10-02", status="failed")
    assert _v22_run(v22, conn, date(2026, 10, 2)) == stranger_id
    row = conn.execute(
        "SELECT guild_id, posted_message_ids FROM digests WHERE id = ?", (stranger_id,)
    ).fetchone()
    assert (row["guild_id"], row["posted_message_ids"]) == (STRANGER, "[555]")


def test_forcing_v22_over_a_strangers_digest_overwrites_it_and_leaves_the_friend_unrecorded(
    v22, conn
):
    # Documented, and the one worth a runbook line: `--force` on a rolled-back v2.2 rewrites a
    # stranger's 'ok' row with the friend's message ids. The friend's own (guild) digest for
    # that day still doesn't exist, so a later v3 would post it again.
    repo.create_guild(conn, FRIEND, tier="comped", imported_at=NOW)
    repo.create_guild(conn, STRANGER)
    stranger_id = _v3_digest(conn, STRANGER, "2026-10-02", message_ids="[1, 2]")
    assert _v22_run(v22, conn, date(2026, 10, 2), force=True) == stranger_id
    row = conn.execute(
        "SELECT guild_id, posted_message_ids FROM digests WHERE id = ?", (stranger_id,)
    ).fetchone()
    assert (row["guild_id"], row["posted_message_ids"]) == (STRANGER, "[555]")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM digests WHERE guild_id = ? AND run_date = '2026-10-02'", (FRIEND,)
        ).fetchone()[0]
        == 0
    )
    assert repo.adopt_orphan_digests(conn, FRIEND) == 4  # only the four old days, not this one
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM digests WHERE guild_id = ? AND run_date = '2026-10-02'", (FRIEND,)
        ).fetchone()[0]
        == 0
    )


def test_v22_status_reports_whichever_guilds_digest_is_newest_and_sums_everyones_tokens(v22, conn):
    # Documented: `/newsbot status` on a rolled-back v2.2 is about "the digests table",
    # which now has strangers in it. Cosmetic, but it will look odd.
    repo.create_guild(conn, STRANGER)
    with conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, input_tokens, output_tokens, "
            "created_at, updated_at) VALUES (?, '2026-10-09', 'partial', 1000, 1000, ?, ?)",
            (STRANGER, NOW.isoformat(), NOW.isoformat()),
        )
    snapshot = v22.status_snapshot(conn, NOW, NOW - timedelta(days=30), [])
    assert snapshot.last_digest.run_date == date(2026, 10, 9)
    assert snapshot.last_digest.status == "partial"


# --- v2.2 write paths on rows v3 created ---


def _v3_summaries_and_stories(conn):
    """game_summaries plus stories pointing at them, written the way comped v3 will."""
    with conn:
        conn.executemany(
            "INSERT INTO game_summaries (id, game_key, run_date, status, window_start, "
            "window_end, created_at) "
            "VALUES (?, ?, ?, 'ok', 'a', 'b', 'c')",
            [
                (1, "palworld", "2026-01-10"),
                (2, "palworld", "2026-09-29"),
                (3, "borderlands4", "2026-09-29"),
            ],
        )
        conn.executemany(
            "INSERT INTO stories (id, topic_key, headline, summary, label, is_update_of, "
            "digest_id, created_at, summary_id) VALUES (?, ?, ?, ?, 'reported', ?, NULL, ?, ?)",
            [
                (
                    200,
                    "palworld",
                    "Old palworld scoop",
                    "ancient history",
                    None,
                    "2026-01-10T10:00:00+00:00",
                    1,
                ),
                (
                    201,
                    "palworld",
                    "Fresh palworld scoop",
                    "recent gossip",
                    200,
                    "2026-09-29T10:00:00+00:00",
                    2,
                ),
                (
                    202,
                    "borderlands4",
                    "Fresh bl4 scoop",
                    "more gossip",
                    None,
                    "2026-09-29T10:00:00+00:00",
                    3,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)",
            [(200, 20), (201, 20), (202, 10)],
        )


def test_v22_purge_keeps_summary_links_consistent_and_never_touches_game_summaries(v22, conn):
    _v3_summaries_and_stories(conn)
    items_gone, stories_gone = v22.purge_older_than(conn, datetime(2026, 9, 1, tzinfo=UTC))
    assert stories_gone == 1 and items_gone == 0
    assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 3
    links = {
        r["id"]: r["summary_id"]
        for r in conn.execute("SELECT id, summary_id FROM stories WHERE id >= 200")
    }
    assert links == {201: 2, 202: 3}  # survivors keep their summary
    # The story that was an "update of" the purged one loses only that pointer.
    assert conn.execute("SELECT is_update_of FROM stories WHERE id = 201").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM story_items WHERE story_id = 200").fetchone()[0] == 0
    # v2.2's FTS search: the purged story is gone, v3-written survivors are searchable.
    assert v22.search_stories(conn, "ancient", EPOCH, 10, 0)[1] == 0
    assert v22.search_stories(conn, "gossip", EPOCH, 10, 0)[1] == 2
    _healthy(conn)


def test_deleting_a_summary_later_nulls_the_link_and_keeps_the_fts_index_in_step(v22, conn):
    # v3's retention will delete summaries; the SET NULL action updates stories, which fires
    # the update trigger. Both the old and the new FTS state have to stay coherent.
    _v3_summaries_and_stories(conn)
    with conn:
        conn.execute("DELETE FROM game_summaries WHERE id = 2")
    assert conn.execute("SELECT summary_id FROM stories WHERE id = 201").fetchone()[0] is None
    assert v22.search_stories(conn, "recent gossip", EPOCH, 10, 0)[1] == 1
    assert v22.search_stories(conn, "gossip", EPOCH, 10, 0)[1] == 2
    _healthy(conn)


def test_v22_save_run_between_v3_writes_does_not_disturb_summary_links(v22, conn):
    _v3_summaries_and_stories(conn)
    day = date(2026, 10, 2)
    digest_id = _v22_run(v22, conn, day)
    row = conn.execute(
        "SELECT summary_id, digest_id FROM stories WHERE headline = 'Quartz story'"
    ).fetchone()
    assert (row["summary_id"], row["digest_id"]) == (None, digest_id)
    links = {
        r["id"]: r["summary_id"]
        for r in conn.execute("SELECT id, summary_id FROM stories WHERE id >= 200")
    }
    assert links == {200: 1, 201: 2, 202: 3, 203: None}
    _healthy(conn)


def test_v22_search_and_recent_see_stories_v3_wrote_with_no_digest(v22, conn):
    _v3_summaries_and_stories(conn)
    found, total = v22.search_stories(conn, "gossip", EPOCH, 10, 0)
    assert total == 2 and {s.headline for s in found} == {"Fresh palworld scoop", "Fresh bl4 scoop"}
    recent, total = v22.query_stories(conn, ["borderlands4"], EPOCH, None, 10, 0)
    assert total == 4  # three stories from the fixture plus the v3 one
    assert (
        v22.recent_headlines(conn, "palworld", datetime(2026, 9, 28, tzinfo=UTC))[0].headline
        == "Fresh palworld scoop"
    )


# --- the orphan uniqueness guarantee under a race ---


def test_two_v22_processes_racing_to_claim_one_day_never_make_two_orphan_rows(v22, v22_db):
    outcomes: list[object] = []
    barrier = threading.Barrier(4)

    def claimer():
        with closing(connect(v22_db)) as c:
            barrier.wait(timeout=10)
            try:
                outcomes.append(v22.claim_digest(c, date(2026, 10, 3), force=False, now=_clock()))
            except Exception as exc:  # a lost race can surface as a lock or a unique error
                outcomes.append(exc)

    threads = [threading.Thread(target=claimer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    winners = [o for o in outcomes if isinstance(o, int)]
    assert len(winners) == 1, outcomes
    with closing(connect(v22_db)) as c:
        assert (
            c.execute("SELECT COUNT(*) FROM digests WHERE run_date = '2026-10-03'").fetchone()[0]
            == 1
        )
