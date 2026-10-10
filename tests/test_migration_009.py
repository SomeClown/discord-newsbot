"""Migration 009 (SHiFT code details and alert-message records): additive, and nothing lost.

Built on a real v8 database with the kind of rows prod has (posted, too_old and seeded
codes, a server's posts with message ids, its SHiFT settings), then migrated by the real
runner. The compatibility tests (v3.0.5 and v2.2.0 running against the result) are in
test_migration_009_v305_compat.py and test_migration_005_v22_compat.py.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsbot.store import db
from newsbot.store.db import connect, migrate

MIGRATION = Path(db.__file__).parent / "migrations" / "009_shift_details.sql"
SEEDED = "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"
TOO_OLD = "BBBBB-BBBBB-BBBBB-BBBBB-BBBB2"
POSTED = "DDDDD-DDDDD-DDDDD-DDDDD-DDDD4"
POSTED_2 = "EEEEE-EEEEE-EEEEE-EEEEE-EEEE5"
NEW_TABLES = {"code_details", "guild_alert_messages"}
# Tables whose rows the migration must carry over untouched, with a stable ordering.
KEPT = {
    "alerted_codes": "code",
    "guilds": "guild_id",
    "guild_code_posts": "guild_id, code",
    "code_sightings": "code, source_name",
    "guild_code_followups": "guild_id, code",
    "alert_state": "key",
    "app_state": "key",
}


@pytest.fixture
def v8_path(tmp_path, monkeypatch, migrations_up_to):
    path = tmp_path / "v8.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "migrations_v8", 8))
        with closing(connect(path)) as conn:
            assert migrate(conn) == 8
            with conn:
                for code, status, message_id in (
                    (SEEDED, "seeded", None),
                    (TOO_OLD, "too_old", None),
                    (POSTED, "posted", 999),
                    (POSTED_2, "posted", 1000),
                ):
                    conn.execute(
                        "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
                        "message_id, pinged, status) VALUES (?, '2026-10-01T10:00:00+00:00', "
                        "'Feed', 'https://example.com/p', ?, ?, ?)",
                        (code, message_id, int(status == "posted"), status),
                    )
                conn.execute(
                    "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (7, 'n', 'n')"
                )
                conn.execute(
                    "INSERT INTO guild_shift (guild_id, enabled, channel_id, ping, enabled_at, "
                    "ping_day, ping_count) VALUES (7, 1, 555, 'everyone', 'n', '2026-10-01', 2)"
                )
                for code, message_id in ((POSTED, 999), (POSTED_2, 999), (SEEDED, None)):
                    conn.execute(
                        "INSERT INTO guild_code_posts (guild_id, code, status, message_id, "
                        "pinged, claimed_at) VALUES (7, ?, ?, ?, 1, 'n')",
                        (code, "posted" if message_id else "skipped", message_id),
                    )
                conn.execute(
                    "INSERT INTO code_sightings (code, source_name, seen_at) "
                    "VALUES (?, 'r/Borderlands4', 'n')",
                    (POSTED,),
                )
                conn.execute(
                    "INSERT INTO guild_code_followups (guild_id, code, status, queued_at) "
                    "VALUES (7, ?, 'queued', 'n')",
                    (POSTED,),
                )
                conn.execute("INSERT INTO alert_state (key, value) VALUES ('seeded_at', 'n')")
    return path


def _dump(conn):
    return {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]  # noqa: S608
        for table, order in KEPT.items()
    }


def _schema(conn):
    return {
        (r["type"], r["name"]): r["sql"]
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def test_a_v8_database_migrates_to_9_with_all_data_intact(v8_path):
    with closing(connect(v8_path)) as conn:
        before = _dump(conn)
        guild_shift_before = [tuple(r) for r in conn.execute("SELECT * FROM guild_shift")]
    with closing(connect(v8_path)) as conn:
        assert migrate(conn) == 9
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        after = _dump(conn)
        # The one new app_state row is the marker; everything else is exactly as it was.
        marker = [row for row in after["app_state"] if row[0] == "shift_details_since"]
        after["app_state"] = [row for row in after["app_state"] if row[0] != "shift_details_since"]
        assert len(marker) == 1
        # alerted_codes gained two trailing NULL columns (QA batch 1, fixes 2 and 3) and nothing
        # else about it moved.
        codes_after = after.pop("alerted_codes")
        assert [row[-2:] for row in codes_after] == [(None, None)] * len(codes_after)
        assert [row[:-2] for row in codes_after] == before.pop("alerted_codes")
        assert after == before
        # guild_shift gained a trailing NULL column and nothing else changed.
        guild_shift_after = [tuple(r) for r in conn.execute("SELECT * FROM guild_shift")]
        assert [row[:-1] for row in guild_shift_after] == guild_shift_before
        assert [row[-1] for row in guild_shift_after] == [None]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_migration_only_adds_things(v8_path):
    with closing(connect(v8_path)) as conn:
        old = _schema(conn)
        migrate(conn)
        new = _schema(conn)
    for key, sql in old.items():
        if key in (("table", "guild_shift"), ("table", "alerted_codes")):
            continue  # each gained nullable columns, checked below
        assert new[key] == sql, f"{key} was changed by 009"
    assert {name for (kind, name) in new.keys() - old.keys() if kind == "table"} == NEW_TABLES
    assert {name for (kind, name) in new.keys() - old.keys() if kind == "index"} == {
        "idx_guild_alert_messages_posted",
        "idx_guild_code_posts_message",
        "idx_guild_code_posts_code",
    }
    assert [k for k in old.keys() - new.keys()] == []


def test_guild_shift_gains_one_nullable_column_with_no_default(v8_path):
    with closing(connect(v8_path)) as conn:
        before = [r["name"] for r in conn.execute("PRAGMA table_info(guild_shift)")]
        migrate(conn)
        info = list(conn.execute("PRAGMA table_info(guild_shift)"))
    assert [r["name"] for r in info][:-1] == before
    last = info[-1]
    assert (last["name"], last["notnull"], last["dflt_value"]) == ("edits_blocked_at", 0, None)


def test_alerted_codes_gains_two_nullable_timestamps_and_keeps_its_status_check(v8_path):
    with closing(connect(v8_path)) as conn:
        before = [r["name"] for r in conn.execute("PRAGMA table_info(alerted_codes)")]
        old_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='alerted_codes'"
        ).fetchone()
        migrate(conn)
        info = list(conn.execute("PRAGMA table_info(alerted_codes)"))
        new_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='alerted_codes'"
        ).fetchone()
        # The release of a silenced code is an UPDATE to a status the CHECK already allows.
        statuses = conn.execute("SELECT status FROM alerted_codes").fetchall()
    assert statuses is not None
    assert [r["name"] for r in info][:-2] == before
    assert [(r["name"], r["notnull"], r["dflt_value"]) for r in info[-2:]] == [
        ("official_seen_at", 0, None),
        ("table_silenced_at", 0, None),
    ]
    status_check = "status IN ('seeded', 'too_old', 'pending', 'posted', 'failed', 'roundup')"
    assert status_check in " ".join(old_sql[0].split())
    assert status_check in " ".join(new_sql[0].split())


def test_the_file_has_no_destructive_statements_and_needs_no_fk_header():
    text = MIGRATION.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    starts = [part.split(None, 2)[:2] for part in code.split(";") if part.strip()]
    assert {" ".join(s).upper() for s in starts} <= {
        "CREATE TABLE",
        "CREATE INDEX",
        "ALTER TABLE",
        "INSERT INTO",
    }
    assert "RENAME" not in code.upper() and "DROP" not in code.upper()
    assert not text.startswith("-- newsbot: foreign-keys-off")


def test_the_rollout_marker_is_present_and_sorts_against_isoformat_timestamps(v8_path):
    before = datetime.now(UTC)
    with closing(connect(v8_path)) as conn:
        migrate(conn)
        value = conn.execute(
            "SELECT value FROM app_state WHERE key = 'shift_details_since'"
        ).fetchone()[0]
    marker = datetime.fromisoformat(value)
    assert marker.tzinfo is not None
    assert before.replace(microsecond=0) <= marker <= datetime.now(UTC)
    # Text comparison is how SQL sees it; it has to agree with the datetimes.
    assert value <= datetime.now(UTC).isoformat()


def test_migrating_twice_is_idempotent_and_the_marker_never_moves(v8_path):
    with closing(connect(v8_path)) as conn:
        migrate(conn)
        first = conn.execute(
            "SELECT value FROM app_state WHERE key = 'shift_details_since'"
        ).fetchone()[0]
        assert migrate(conn) == 9
    with closing(connect(v8_path)) as conn:
        assert migrate(conn) == 9
        assert (
            conn.execute(
                "SELECT value FROM app_state WHERE key = 'shift_details_since'"
            ).fetchone()[0]
            == first
        )


def test_an_existing_marker_survives_the_insert(v8_path):
    # ON CONFLICT DO NOTHING: a hand-set (or earlier) marker is never overwritten.
    with closing(connect(v8_path)) as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('shift_details_since', 'hand set')"
        )
        conn.commit()
        migrate(conn)
        assert (
            conn.execute(
                "SELECT value FROM app_state WHERE key = 'shift_details_since'"
            ).fetchone()[0]
            == "hand set"
        )


def test_the_lookup_by_message_uses_its_index(v8_path):
    with closing(connect(v8_path)) as conn:
        migrate(conn)
        plan = " ".join(
            r["detail"]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT code FROM guild_code_posts "
                "WHERE guild_id = 7 AND message_id = 999 AND from_roundup = 0 ORDER BY rowid"
            )
        )
    assert "idx_guild_code_posts_message" in plan


# --- the CHECKs ---


@pytest.fixture
def conn(v8_path):
    with closing(connect(v8_path)) as c:
        migrate(c)
        yield c


def _details(conn, **overrides):
    values = {"code": SEEDED, "clock_start": "2026-10-09T16:00:00+00:00", "updated_at": "n"}
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO code_details ({columns}) VALUES ({marks})", list(values.values()))  # noqa: S608


def test_a_minimal_row_gets_the_defaults(conn):
    _details(conn)
    row = conn.execute("SELECT * FROM code_details").fetchone()
    assert (row["reward"], row["reward_source"], row["reward_from"]) == ("", "", "")
    assert (row["category"], row["expires_on"], row["added_on"], row["origin"]) == (
        None,
        None,
        None,
        "",
    )
    assert (row["first_listed_at"], row["details_changed_at"]) == (None, None)


def test_a_fully_filled_honest_row_is_accepted(conn):
    _details(
        conn,
        reward="x" * 300,
        reward_source="table",
        category="permanent",
        category_source="official",
        expires_on="2026-10-14",
        expires_source="table",
        added_on="2026-01-31",
        origin="y" * 300,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "legendary"},
        {"category": ""},
        {"expires_on": "tomorrow"},
        {"expires_on": "2026-1-5"},
        {"expires_on": "2026-10-14T00:00:00"},
        {"expires_on": "26-10-14"},
        {"added_on": "Oct 14, 2026"},
        {"reward": "x" * 301},
        {"origin": "y" * 301},
        {"reward_source": "press"},
        {"category_source": "community"},
        {"expires_source": "Official"},
        {"code": "TOO-SHORT"},
        {"clock_start": None},
        {"updated_at": None},
        {"reward": None},
    ],
)
def test_the_checks_refuse_a_bad_row(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        _details(conn, **bad)


def test_the_code_is_the_primary_key(conn):
    _details(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _details(conn)


def _message(conn, **overrides):
    values = {
        "guild_id": 7,
        "message_id": 999,
        "channel_id": 555,
        "header": "New SHiFT code!",
        "posted_at": "2026-10-09T16:00:00+00:00",
        "rendered_at": "2026-10-09T16:00:00+00:00",
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO guild_alert_messages ({columns}) VALUES ({marks})",  # noqa: S608
        list(values.values()),
    )


def test_an_alert_message_row_gets_its_defaults(conn):
    _message(conn)
    row = conn.execute("SELECT * FROM guild_alert_messages").fetchone()
    assert (row["golden_prefix"], row["state"]) == (0, "live")


@pytest.mark.parametrize(
    "bad",
    [
        {"message_id": 0},
        {"channel_id": 0},
        {"channel_id": -5},
        {"header": ""},
        {"header": "h" * 201},
        {"golden_prefix": 2},
        {"state": "deleted"},
        {"guild_id": 8},  # no such guild
        {"posted_at": None},
        {"rendered_at": None},
    ],
)
def test_the_alert_message_checks_refuse_a_bad_row(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        _message(conn, **bad)


def test_a_200_character_header_and_a_gone_state_are_fine(conn):
    _message(conn, header="h" * 200, state="gone", golden_prefix=1)


def test_the_same_message_cannot_be_recorded_twice(conn):
    _message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _message(conn)
    _message(conn, message_id=1000)  # a different message in the same server is fine


def test_deleting_a_guild_cascades_its_alert_messages_and_only_its_own(conn):
    conn.execute("INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (8, 'n', 'n')")
    _message(conn)
    _message(conn, guild_id=8)
    conn.commit()
    conn.execute("DELETE FROM guilds WHERE guild_id = 7")
    conn.commit()
    assert [r["guild_id"] for r in conn.execute("SELECT guild_id FROM guild_alert_messages")] == [8]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_code_details_has_no_foreign_key_so_a_table_can_list_an_unreleased_code(conn):
    _details(conn, code="ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZ9")
    assert conn.execute("PRAGMA foreign_key_list(code_details)").fetchall() == []
