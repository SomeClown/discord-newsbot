"""Migration 006 (SHiFT follow-up tables, D14): additive, and safe after 005 already ran."""

from contextlib import closing

from newsbot.store import db
from newsbot.store.db import connect, migrate


def test_006_upgrades_a_v5_database_and_keeps_its_guild_code_posts(
    tmp_path, monkeypatch, migrations_up_to
):
    path = tmp_path / "v5.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "migrations_v5", 5))
        with closing(connect(path)) as conn:
            assert migrate(conn) == 5
            conn.execute(
                "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
                "VALUES ('AAAAA-AAAAA-AAAAA-AAAAA-AAAA1', 'n', 'S', 'u', 'posted')"
            )
            conn.execute(
                "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (7, 'n', 'n')"
            )
            conn.execute(
                "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
                "VALUES (7, 'AAAAA-AAAAA-AAAAA-AAAAA-AAAA1', 'posted', 'n')"
            )
            conn.commit()

    with closing(connect(path)) as conn:
        assert migrate(conn) == 8
        row = conn.execute("SELECT status, followup_ok FROM guild_code_posts").fetchone()
        assert (row["status"], row["followup_ok"]) == ("posted", 0)  # no retroactive follow-ups
        assert conn.execute("SELECT COUNT(*) FROM code_sightings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM guild_code_followups").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrate(conn) == 8  # idempotent
