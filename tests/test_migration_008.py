"""Migration 008 (items.id becomes AUTOINCREMENT): a table rebuild that must not lose a thing.

The point is that an item id, once handed out, is never handed out again, because the
item watermarks (007) are ids. The cost is a rebuild of `items`, which three things hang
off: the child tables' foreign keys, the FTS index keyed on rowid, and its triggers.
(v2.2.0's own SQL running against the result is in test_migration_005_v22_compat.py.)
"""

from contextlib import closing

import pytest

from newsbot.store import db
from newsbot.store.db import connect, migrate


@pytest.fixture
def v7_path(tmp_path, monkeypatch, migrations_up_to):
    path = tmp_path / "v7.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "migrations_v7", 7))
        with closing(connect(path)) as conn:
            assert migrate(conn) == 7
            for n in (3, 5, 9):  # gaps on purpose: the ids must be copied, not renumbered
                conn.execute(
                    "INSERT INTO items (id, url, title, excerpt, source_name, trust, "
                    "published_at, collected_at) VALUES (?, ?, ?, 'ex', 'Feed', 'press', NULL, ?)",
                    (n, f"https://e.com/{n}", f"Zebracorn {n}", f"2026-09-30T0{n}:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO item_topics (item_id, topic_key, uncertain) VALUES (?, 'bl4', 0)",
                    (n,),
                )
            conn.execute(
                "INSERT INTO stories (id, topic_key, headline, summary, label, created_at) "
                "VALUES (1, 'bl4', 'h', 's', 'official', 'n')"
            )
            conn.execute("INSERT INTO story_items (story_id, item_id) VALUES (1, 5)")
            conn.commit()
    return path


def test_the_rebuild_keeps_every_row_its_id_and_its_children(v7_path):
    with closing(connect(v7_path)) as conn:
        assert migrate(conn) == 8
        assert [r["id"] for r in conn.execute("SELECT id FROM items ORDER BY id")] == [3, 5, 9]
        assert conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0] == 3
        assert conn.execute("SELECT item_id FROM story_items").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone()[0]
        assert "AUTOINCREMENT" in ddl


def test_the_fts_index_and_its_triggers_survive(v7_path):
    with closing(connect(v7_path)) as conn:
        migrate(conn)
        conn.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
        hit = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH 'zebracorn'")
        assert sorted(r[0] for r in hit) == [3, 5, 9]
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert {"items_ai", "items_ad", "items_au"} <= triggers
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'idx_items_collected_at'")
        conn.execute("UPDATE items SET title = 'Wyvern' WHERE id = 5")
        conn.execute("DELETE FROM items WHERE id = 3")
        conn.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
        found = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH 'wyvern'")
        assert [r[0] for r in found] == [5]
        assert (
            conn.execute("SELECT 1 FROM items_fts WHERE items_fts MATCH 'zebracorn 3'").fetchone()
            is None
        )


def test_ids_never_come_back_after_everything_is_deleted(v7_path):
    with closing(connect(v7_path)) as conn:
        migrate(conn)
        conn.execute("DELETE FROM items")
        cur = conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://e.com/new', 't', 'e', 'Feed', 'press', 'n')"
        )
        assert cur.lastrowid == 10


def test_the_sequence_starts_above_any_mark_a_digest_already_recorded(
    tmp_path, monkeypatch, migrations_up_to
):
    """A store purged empty before 008 ran has no items to read the high-water mark from."""
    path = tmp_path / "v7.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "migrations_v7", 7))
        with closing(connect(path)) as conn:
            migrate(conn)
            conn.execute(
                "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (7, 'n', 'n')"
            )
            conn.execute(
                "INSERT INTO digests (guild_id, run_date, status, items_upto, created_at, "
                "updated_at) VALUES (7, '2026-09-30', 'ok', 41, 'n', 'n')"
            )
            conn.commit()
    with closing(connect(path)) as conn:
        migrate(conn)
        cur = conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://e.com/new', 't', 'e', 'Feed', 'press', 'n')"
        )
        assert cur.lastrowid == 42


def test_a_fresh_database_numbers_from_one(tmp_path):
    with closing(connect(tmp_path / "fresh.db")) as conn:
        migrate(conn)
        cur = conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://e.com/a', 't', 'e', 'Feed', 'press', 'n')"
        )
        assert cur.lastrowid == 1
