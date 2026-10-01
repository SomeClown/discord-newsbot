"""Tests for migration 005: the public-app tables, and the two rebuilds (plan task 2).

The interesting part isn't the new tables, it's that `digests` and `stories`
get dropped and recreated while holding real history, with an FTS index and
child rows hanging off them. So most of this runs against the populated v4
database from conftest.py and checks that nothing got lost in the move.
Whether a v2.2.0 process can still use the result is its own file:
test_migration_005_v22_compat.py.
"""

# The f-string SQL below only ever interpolates table and column names from
# fixed lists in this file, never data.
# ruff: noqa: S608

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from newsbot.store import db, repo
from newsbot.store.db import StoreError, connect, migrate

MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"

NOW = "2026-09-30T12:00:00+00:00"


def _dump(conn, sql):
    return [tuple(row) for row in conn.execute(sql)]


def _fts_tables(conn):
    return [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%USING fts5%'"
        )
    ]


def _add_guild(conn, guild_id, **extra):
    repo.create_guild(conn, guild_id, **extra)


# --- what the migration does to a populated v4 database ---

OLD_DIGEST_COLUMNS = (
    "id, run_date, status, posted_message_ids, error_notes, "
    "input_tokens, output_tokens, created_at, updated_at"
)
OLD_STORY_COLUMNS = "id, topic_key, headline, summary, label, is_update_of, digest_id, created_at"


def test_fresh_db_reaches_user_version_five(tmp_path):
    with closing(connect(tmp_path / "n.db")) as conn:
        assert migrate(conn) == 6
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_digests_and_stories_keep_their_ids_and_data(v4_db):
    with closing(connect(v4_db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        digests_before = _dump(conn, f"SELECT {OLD_DIGEST_COLUMNS} FROM digests ORDER BY id")
        stories_before = _dump(conn, f"SELECT {OLD_STORY_COLUMNS} FROM stories ORDER BY id")
        links_before = _dump(conn, "SELECT * FROM story_items ORDER BY 1, 2")
        items_before = _dump(conn, "SELECT * FROM items ORDER BY id")
        topics_before = _dump(conn, "SELECT * FROM item_topics ORDER BY 1, 2")
        codes_before = _dump(conn, "SELECT * FROM alerted_codes ORDER BY code")
        lounge_before = _dump(conn, "SELECT * FROM lounge_quotes_used ORDER BY 1, 2")
        assert len(digests_before) == 4 and len(stories_before) == 5

        assert migrate(conn) == 6

        assert (
            _dump(conn, f"SELECT {OLD_DIGEST_COLUMNS} FROM digests ORDER BY id") == digests_before
        )
        assert _dump(conn, f"SELECT {OLD_STORY_COLUMNS} FROM stories ORDER BY id") == stories_before
        assert _dump(conn, "SELECT * FROM story_items ORDER BY 1, 2") == links_before
        assert _dump(conn, "SELECT * FROM items ORDER BY id") == items_before
        assert _dump(conn, "SELECT * FROM item_topics ORDER BY 1, 2") == topics_before
        assert _dump(conn, "SELECT * FROM alerted_codes ORDER BY code") == codes_before
        assert (
            _dump(
                conn, "SELECT source_key, quote_hash, used_at FROM lounge_quotes_used ORDER BY 1, 2"
            )
            == lounge_before
        )


def test_new_columns_start_empty_or_defaulted(v22_db):
    with closing(connect(v22_db)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id IS NOT NULL").fetchone()[0]
            == 0
        )
        row = conn.execute(
            "SELECT posted_by_game, window_start, window_end, attempts FROM digests WHERE id = 7"
        ).fetchone()
        assert tuple(row) == ("{}", None, None, 0)
        assert (
            conn.execute("SELECT COUNT(*) FROM stories WHERE summary_id IS NOT NULL").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM lounge_quotes_used WHERE guild_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_new_tables_exist_and_start_empty(v22_db):
    with closing(connect(v22_db)) as conn:
        for table in (
            "guilds",
            "guild_games",
            "guild_shift",
            "guild_code_posts",
            "guild_lounge",
            "guild_notices",
            "game_summaries",
            "app_state",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        indexes = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {
            "idx_item_topics_topic",
            "idx_guild_notices",
            "idx_stories_topic_created",
            "idx_stories_created_at",
        } <= indexes


def test_foreign_key_check_is_empty_and_foreign_keys_are_back_on(v22_db):
    with closing(connect(v22_db)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migrate_restores_foreign_keys_on_the_connection_that_ran_it(v4_db):
    with closing(connect(v4_db)) as conn:
        migrate(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # And a caller that had them off keeps them off (the runner restores, it doesn't force).
    with closing(sqlite3.connect(v4_db)) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        migrate(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0


# --- FTS survives the rebuild ---


def test_every_fts_table_passes_its_integrity_check(v22_db):
    with closing(connect(v22_db)) as conn:
        tables = _fts_tables(conn)
        assert set(tables) == {"stories_fts", "items_fts"}
        for name in tables:
            conn.execute(f"INSERT INTO {name} ({name}) VALUES ('integrity-check')")
            conn.execute(f"INSERT INTO {name} ({name}, rank) VALUES ('integrity-check', 1)")


def test_stories_triggers_are_recreated_and_point_at_the_new_table(v22_db):
    with closing(connect(v22_db)) as conn:
        triggers = {
            r["name"]: r["tbl_name"]
            for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'")
        }
    assert triggers == {
        "stories_ai": "stories",
        "stories_ad": "stories",
        "stories_au": "stories",
        "items_ai": "items",
        "items_ad": "items",
        "items_au": "items",
        "guild_games_limit": "guild_games",
    }


def test_old_stories_are_still_searchable(v22_db):
    from datetime import UTC, datetime

    with closing(connect(v22_db)) as conn:
        found, total = repo.search_stories(conn, "vault", datetime(2026, 1, 1, tzinfo=UTC), 10, 0)
    assert total == 2
    assert {s.id for s in found} == {100, 102}


def test_stories_fts_triggers_still_work_after_the_rebuild(v22_db):
    from datetime import UTC, datetime

    since = datetime(2026, 1, 1, tzinfo=UTC)
    with closing(connect(v22_db)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
                "VALUES ('palworld', 'Zebracorn arrives', 'Stripes.', 'official', 12, ?)",
                (NOW,),
            )
        assert repo.search_stories(conn, "zebracorn", since, 10, 0)[1] == 1
        with conn:
            conn.execute(
                "UPDATE stories SET headline = 'Narwhal arrives' WHERE headline LIKE 'Zebra%'"
            )
        assert repo.search_stories(conn, "zebracorn", since, 10, 0)[1] == 0
        assert repo.search_stories(conn, "narwhal", since, 10, 0)[1] == 1
        with conn:
            conn.execute("DELETE FROM stories WHERE headline LIKE 'Narwhal%'")
        assert repo.search_stories(conn, "narwhal", since, 10, 0)[1] == 0
        conn.execute("INSERT INTO stories_fts (stories_fts) VALUES ('integrity-check')")


def test_items_fts_is_backfilled_and_its_triggers_work(v22_db):
    with closing(connect(v22_db)) as conn:
        hits = _dump(
            conn,
            "SELECT rowid FROM items_fts WHERE items_fts MATCH 'borderlands' ORDER BY rowid",
        )
        assert hits == [(10,), (40,)]
        with conn:
            conn.execute(
                "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
                "VALUES ('https://example.com/x', 'Zebracorn', 'stripes', 'Feed', 'press', ?)",
                (NOW,),
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'zebracorn'"
            ).fetchone()[0]
            == 1
        )
        with conn:
            conn.execute("UPDATE items SET title = 'Narwhal' WHERE url = 'https://example.com/x'")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'zebracorn'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'narwhal'"
            ).fetchone()[0]
            == 1
        )
        with conn:
            conn.execute("DELETE FROM items WHERE url = 'https://example.com/x'")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'narwhal'"
            ).fetchone()[0]
            == 0
        )
        conn.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")


# --- idempotency, concurrency, failure ---


def test_migrate_is_idempotent_and_changes_nothing_the_second_time(v4_db):
    with closing(connect(v4_db)) as conn:
        assert migrate(conn) == 6
        snapshot = _dump(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
        rows = _dump(conn, "SELECT * FROM digests ORDER BY id")
        assert migrate(conn) == migrate(conn) == 6
        assert _dump(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2") == snapshot
        assert _dump(conn, "SELECT * FROM digests ORDER BY id") == rows


def test_several_processes_opening_a_v4_file_at_once_all_end_up_on_v5(v4_db):
    versions: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def opener():
        try:
            with closing(connect(v4_db)) as conn:
                barrier.wait(timeout=10)
                versions.append(migrate(conn))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert versions == [6] * 6
    with closing(connect(v4_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _broken_005_dir(tmp_path, monkeypatch, migrations_up_to, marker):
    """Migrations 001 to 004 plus a copy of 005 that blows up right after `marker`."""
    directory = migrations_up_to(tmp_path / "broken", 4)
    text = (MIGRATIONS_DIR / "005_public_app.sql").read_text()
    assert marker in text
    (directory / "005_public_app.sql").write_text(
        text.replace(marker, marker + "\nSELECT * FROM this_table_does_not_exist;", 1)
    )
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", directory)


@pytest.mark.parametrize(
    "marker",
    [
        "DROP TABLE digests;",
        "ALTER TABLE digests_new RENAME TO digests;",
        "DROP TABLE stories;",
        "ALTER TABLE stories_new RENAME TO stories;",
    ],
)
def test_a_failure_mid_rebuild_leaves_the_database_at_v4_and_intact(
    v4_db, tmp_path, monkeypatch, migrations_up_to, marker
):
    with closing(connect(v4_db)) as conn:
        schema_before = _dump(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
        tables = ["digests", "stories", "story_items", "items", "item_topics", "alerted_codes"]
        before = {t: _dump(conn, f"SELECT * FROM {t} ORDER BY 1, 2") for t in tables}

    _broken_005_dir(tmp_path, monkeypatch, migrations_up_to, marker)
    with closing(connect(v4_db)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="this_table_does_not_exist"):
            migrate(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.in_transaction

    with closing(connect(v4_db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            _dump(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2") == schema_before
        )
        assert {t: _dump(conn, f"SELECT * FROM {t} ORDER BY 1, 2") for t in tables} == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.execute("INSERT INTO stories_fts (stories_fts) VALUES ('integrity-check')")

    # The real migration then applies cleanly on top of the survivor.
    monkeypatch.undo()
    with closing(connect(v4_db)) as conn:
        assert migrate(conn) == 6
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- the runner's foreign-keys-off header, with a synthetic migration ---


def _synthetic_dir(tmp_path, monkeypatch, second):
    directory = tmp_path / "synthetic"
    directory.mkdir()
    (directory / "001_base.sql").write_text(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY,"
        " pid INTEGER REFERENCES parent (id) ON DELETE CASCADE);\n"
        "INSERT INTO parent (id) VALUES (1);\n"
        "INSERT INTO child (id, pid) VALUES (1, 1);\n"
    )
    (directory / "002_change.sql").write_text(second)
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", directory)


def test_a_foreign_key_violation_rolls_the_migration_back_and_names_the_table(
    tmp_path, monkeypatch
):
    _synthetic_dir(
        tmp_path,
        monkeypatch,
        "-- newsbot: foreign-keys-off\nCREATE TABLE extra (x INTEGER);\n"
        "INSERT INTO child (id, pid) VALUES (2, 99);\n",
    )
    with closing(connect(tmp_path / "s.db")) as conn:
        with pytest.raises(StoreError, match="child"):
            migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 1
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "extra" not in names
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_header_turns_foreign_keys_off_so_a_drop_does_not_cascade(tmp_path, monkeypatch):
    _synthetic_dir(
        tmp_path,
        monkeypatch,
        "-- newsbot: foreign-keys-off\n"
        "CREATE TABLE parent_new (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO parent_new SELECT id FROM parent;\n"
        "DROP TABLE parent;\nALTER TABLE parent_new RENAME TO parent;\n",
    )
    with closing(connect(tmp_path / "s.db")) as conn:
        assert migrate(conn) == 2
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_without_the_header_foreign_keys_stay_on_and_a_drop_cascades(tmp_path, monkeypatch):
    _synthetic_dir(
        tmp_path, monkeypatch, "DROP TABLE parent;\nCREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
    )
    with closing(connect(tmp_path / "s.db")) as conn:
        assert migrate(conn) == 2
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0


# --- constraints on the new tables ---


def _raises_integrity(conn, sql, params=()):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(sql, params)


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "n.db")) as c:
        migrate(c)
        yield c


@pytest.mark.parametrize("guild_id", [0, -5])
def test_guild_id_must_be_positive(conn, guild_id):
    _raises_integrity(
        conn,
        "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (?, 'n', 'n')",
        (guild_id,),
    )


@pytest.mark.parametrize(
    "bad_time", ["9:00", "09:60", "0900", "", "aa:bb", "3a:00", "24:00", "29:59"]
)
def test_digest_time_shape_is_checked(conn, bad_time):
    _raises_integrity(
        conn,
        "INSERT INTO guilds (guild_id, digest_time, joined_at, updated_at) VALUES (1, ?, 'n', 'n')",
        (bad_time,),
    )


def test_tier_must_be_free_or_comped(conn):
    _raises_integrity(
        conn,
        "INSERT INTO guilds (guild_id, tier, joined_at, updated_at) VALUES (1, 'gold', 'n', 'n')",
    )


def test_eleventh_game_is_refused_by_the_trigger(conn):
    _add_guild(conn, 1)
    _add_guild(conn, 2)
    with conn:
        for i in range(10):
            conn.execute("INSERT INTO guild_games VALUES (1, ?, 5)", (f"g{i}",))
            conn.execute("INSERT INTO guild_games VALUES (2, ?, 5)", (f"g{i}",))
    with pytest.raises(sqlite3.IntegrityError, match="at most 10 games"):
        with conn:
            conn.execute("INSERT INTO guild_games VALUES (1, 'g10', 5)")
    assert conn.execute("SELECT COUNT(*) FROM guild_games WHERE guild_id = 1").fetchone()[0] == 10


def test_game_channel_must_be_positive(conn):
    _add_guild(conn, 1)
    _raises_integrity(conn, "INSERT INTO guild_games VALUES (1, 'g', 0)")


@pytest.mark.parametrize("ping", ["none", "everyone", "12345678901234567", "123456789012345678"])
def test_shift_accepts_good_ping_values(conn, ping):
    _add_guild(conn, 1)
    with conn:
        conn.execute(
            "INSERT INTO guild_shift (guild_id, enabled, channel_id, ping) VALUES (1, 1, 9, ?)",
            (ping,),
        )


@pytest.mark.parametrize(
    "ping", ["", "Everyone", "here", "0", "1", "0123", "12a", "-5", "1 2", "12\n", "9" * 21]
)
def test_shift_rejects_malformed_ping_values(conn, ping):
    _add_guild(conn, 1)
    _raises_integrity(
        conn,
        "INSERT INTO guild_shift (guild_id, enabled, channel_id, ping) VALUES (1, 1, 9, ?)",
        (ping,),
    )


@pytest.mark.parametrize("channel", [0, -1])
def test_shift_channel_must_be_positive_when_set(conn, channel):
    _add_guild(conn, 1)
    _raises_integrity(
        conn,
        "INSERT INTO guild_shift (guild_id, enabled, channel_id) VALUES (1, 1, ?)",
        (channel,),
    )


def test_shift_enabled_needs_a_channel_but_disabled_does_not(conn):
    _add_guild(conn, 1)
    _add_guild(conn, 2)
    _raises_integrity(conn, "INSERT INTO guild_shift (guild_id, enabled) VALUES (1, 1)")
    with conn:
        conn.execute("INSERT INTO guild_shift (guild_id, enabled) VALUES (2, 0)")


def test_guild_code_posts_needs_a_known_code_and_a_real_status(conn):
    _add_guild(conn, 1)
    _raises_integrity(
        conn,
        "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
        "VALUES (1, 'X', 'posted', 'n')",
    )
    with conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES (?, 'n', 's', 'u', 'posted')",
            ("A" * 29,),
        )
    _raises_integrity(
        conn,
        "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
        "VALUES (1, ?, 'sent', 'n')",
        ("A" * 29,),
    )
    with conn:
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
            "VALUES (1, ?, 'posted', 'n')",
            ("A" * 29,),
        )
    _raises_integrity(
        conn,
        "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
        "VALUES (1, ?, 'failed', 'n')",
        ("A" * 29,),
    )


def test_guild_code_posts_accepts_every_queue_status(conn):
    _add_guild(conn, 1)
    statuses = ["queued", "pending", "posted", "failed", "skipped"]
    with conn:
        for i, status in enumerate(statuses):
            code = f"{i}" * 29
            conn.execute(
                "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
                "VALUES (?, 'n', 's', 'u', 'posted')",
                (code,),
            )
            conn.execute(
                "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
                "VALUES (1, ?, ?, 'n')",
                (code, status),
            )
    got = [r[0] for r in conn.execute("SELECT status FROM guild_code_posts ORDER BY rowid")]
    assert got == statuses


def test_notice_text_length_is_bounded(conn):
    _add_guild(conn, 1)
    _raises_integrity(
        conn, "INSERT INTO guild_notices (guild_id, created_at, text) VALUES (1, 'n', '')"
    )
    _raises_integrity(
        conn,
        "INSERT INTO guild_notices (guild_id, created_at, text) VALUES (1, 'n', ?)",
        ("x" * 2001,),
    )
    with conn:
        conn.execute(
            "INSERT INTO guild_notices (guild_id, created_at, text) VALUES (1, 'n', ?)",
            ("x" * 2000,),
        )


def test_game_summaries_may_share_a_run_date_but_not_take_a_bad_status(conn):
    # Was "unique per game per day". run_date is only a label now: a summary made inline for a
    # missed digest can land on the same date as a prepared one and must not replace it.
    sql = (
        "INSERT INTO game_summaries "
        "(game_key, run_date, status, window_start, window_end, created_at) "
        "VALUES ('bl4', '2026-09-30', ?, 'a', 'b', 'n')"
    )
    with conn:
        conn.execute(sql, ("ok",))
        conn.execute(sql, ("fallback",))
    assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 2
    _raises_integrity(conn, sql.replace("'bl4'", "'bl5'"), ("weird",))


# --- digests: two guilds, one day ---


def _digest(conn, guild_id, run_date):
    with conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, ?, 'ok', 'n', 'n')",
            (guild_id, run_date),
        )


def test_two_guilds_can_each_have_a_digest_for_the_same_day(conn):
    _add_guild(conn, 1)
    _add_guild(conn, 2)
    _digest(conn, 1, "2026-09-30")
    _digest(conn, 2, "2026-09-30")
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 2


def test_the_same_guild_cannot_have_two_digests_for_one_day(conn):
    _add_guild(conn, 1)
    _digest(conn, 1, "2026-09-30")
    with pytest.raises(sqlite3.IntegrityError):
        _digest(conn, 1, "2026-09-30")


def test_orphan_digests_stay_unique_per_day_like_v22_had_them(conn):
    _add_guild(conn, 1)
    _digest(conn, None, "2026-09-30")
    with pytest.raises(sqlite3.IntegrityError):
        _digest(conn, None, "2026-09-30")
    _digest(conn, 1, "2026-09-30")  # a guild's row for that day is a different thing


def test_digest_guild_id_must_reference_a_guild(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _digest(conn, 404, "2026-09-30")


# --- cascades ---


def _populate_guild(conn, guild_id, digest_id):
    with conn:
        conn.execute("INSERT INTO guild_games VALUES (?, 'bl4', 5)", (guild_id,))
        conn.execute(
            "INSERT INTO guild_shift (guild_id, enabled, channel_id) VALUES (?, 1, 5)", (guild_id,)
        )
        conn.execute(
            "INSERT OR IGNORE INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status) "
            "VALUES (?, 'n', 's', 'u', 'posted')",
            ("A" * 29,),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
            "VALUES (?, ?, 'posted', 'n')",
            (guild_id, "A" * 29),
        )
        conn.execute("INSERT INTO guild_lounge (guild_id, channel_id) VALUES (?, 5)", (guild_id,))
        conn.execute(
            "INSERT INTO guild_notices (guild_id, created_at, text) VALUES (?, 'n', 'hi')",
            (guild_id,),
        )
        conn.execute(
            "INSERT INTO digests (id, guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, ?, '2026-09-30', 'ok', 'n', 'n')",
            (digest_id, guild_id),
        )
        conn.execute(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at, guild_id) "
            "VALUES (?, ?, 'n', ?)",
            (f"s{guild_id}", "c" * 64, guild_id),
        )


PER_GUILD_TABLES = [
    "guild_games",
    "guild_shift",
    "guild_code_posts",
    "guild_lounge",
    "guild_notices",
    "digests",
    "lounge_quotes_used",
]


def test_deleting_a_guild_cascades_to_every_per_guild_table_and_only_that_guild(conn):
    for guild_id, digest_id in ((1, 501), (2, 502)):
        _add_guild(conn, guild_id)
        _populate_guild(conn, guild_id, digest_id)
    with conn:
        conn.execute(
            "INSERT INTO stories (id, topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES (900, 'bl4', 'h', 's', 'official', 501, 'n')"
        )
        conn.execute(
            "INSERT INTO items (id, url, title, excerpt, source_name, trust, collected_at) "
            "VALUES (1, 'u', 't', 'e', 's', 'press', 'n')"
        )

    with conn:
        conn.execute("DELETE FROM guilds WHERE guild_id = 1")

    for table in PER_GUILD_TABLES:
        assert (
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id = 1").fetchone()[0] == 0
        ), table
        assert (
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id = 2").fetchone()[0] == 1
        ), table
    # Shared data survives: the story just loses its digest, codes and items stay.
    assert tuple(conn.execute("SELECT digest_id FROM stories WHERE id = 900").fetchone()) == (None,)
    assert conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_a_digest_nulls_the_stories_that_pointed_at_it(v22_db):
    with closing(connect(v22_db)) as conn:
        with conn:
            conn.execute("DELETE FROM digests WHERE id = 12")
        rows = _dump(conn, "SELECT id, digest_id FROM stories WHERE id IN (103, 104) ORDER BY id")
        assert rows == [(103, None), (104, None)]
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 5
        # Their FTS entries are untouched, the stories are still there.
        conn.execute("INSERT INTO stories_fts (stories_fts) VALUES ('integrity-check')")


def test_deleting_a_game_summary_nulls_summary_id_on_its_stories(v22_db):
    with closing(connect(v22_db)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO game_summaries (id, game_key, run_date, status, window_start, "
                "window_end, created_at) VALUES (1, 'bl4', '2026-09-30', 'ok', 'a', 'b', 'n')"
            )
            conn.execute("UPDATE stories SET summary_id = 1 WHERE id = 100")
        with conn:
            conn.execute("DELETE FROM game_summaries WHERE id = 1")
        assert tuple(conn.execute("SELECT summary_id FROM stories WHERE id = 100").fetchone()) == (
            None,
        )


def test_story_summary_id_must_reference_a_summary(v22_db):
    with closing(connect(v22_db)) as conn:
        _raises_integrity(conn, "UPDATE stories SET summary_id = 999 WHERE id = 100")


def test_story_can_now_exist_without_a_digest(v22_db):
    with closing(connect(v22_db)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO stories (topic_key, headline, summary, label, created_at) "
                "VALUES ('bl4', 'h', 's', 'official', 'n')"
            )


def test_005_carries_the_runner_header_on_its_very_first_line():
    text = (MIGRATIONS_DIR / "005_public_app.sql").read_text()
    assert text.startswith("-- newsbot: foreign-keys-off\n")
