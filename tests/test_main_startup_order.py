"""`python -m newsbot`: what happens before the bot exists, and in what order (plan task 13).

The order is the whole point. Migrate first, then the one-time import, then adopt the digest rows
a rolled-back v2.2 left without a server, then re-sync the lounge block from config, then read
the lounge rows, then build the gateway intents from those rows, and only then construct the
bot. Intents are fixed when the client is constructed; building them from anything but the
final rows is how you get a welcome that never fires. `NewsBot` is a stub here and `run` does
nothing, so no gateway is involved.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

import newsbot.__main__ as entrypoint
from newsbot.config import Secrets, load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.store import repo
from newsbot.store.db import connect, migrate

PRODLIKE = Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml"
T_IMPORT = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)


def _secrets() -> Secrets:
    return Secrets(
        discord_token=SecretStr("not-a-real-token"),
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


class StubBot:
    built: list[tuple] = []

    def __init__(self, cfg, secrets, db_path, **kwargs) -> None:
        type(self).built.append((cfg, db_path, kwargs))

    def run(self, token: str, **kwargs) -> None:
        return None


@pytest.fixture
def order(monkeypatch, tmp_path):
    """Wire `main()` to the prod-like config and a scratch database; record the call order."""
    calls: list[str] = []
    StubBot.built = []
    monkeypatch.setenv("NEWSBOT_CONFIG", str(PRODLIKE))
    monkeypatch.setenv("NEWSBOT_DB", str(tmp_path / "newsbot.db"))
    monkeypatch.setattr(entrypoint, "load_secrets", _secrets)
    monkeypatch.setattr(entrypoint, "NewsBot", StubBot)

    def spy(name, real):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(entrypoint, "migrate", spy("migrate", entrypoint.migrate))
    monkeypatch.setattr(entrypoint, "ensure_imported", spy("import", entrypoint.ensure_imported))
    monkeypatch.setattr(
        entrypoint, "_adopt_orphan_digests", spy("adopt", entrypoint._adopt_orphan_digests)
    )
    monkeypatch.setattr(
        entrypoint,
        "resync_lounge_from_config",
        spy("resync", entrypoint.resync_lounge_from_config),
    )
    monkeypatch.setattr(entrypoint.repo, "list_lounges", spy("list_lounges", repo.list_lounges))
    monkeypatch.setattr(
        entrypoint,
        "build_intents_for_lounges",
        spy("intents", entrypoint.build_intents_for_lounges),
    )

    class RecordingStub(StubBot):
        def __init__(self, *args, **kwargs) -> None:
            calls.append("NewsBot")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(entrypoint, "NewsBot", RecordingStub)
    return calls


def test_startup_does_its_work_in_the_documented_order(order):
    assert entrypoint.main() == 0

    assert order == ["migrate", "import", "adopt", "resync", "list_lounges", "intents", "NewsBot"]


def test_the_bot_gets_the_rows_the_intents_were_built_from(order):
    entrypoint.main()

    ((cfg, db_path, kwargs),) = StubBot.built
    lounges = kwargs["lounges"]
    assert [row.guild_id for row in lounges] == [100000000000000001]  # the friend's lounge
    # The prod-like config turns welcomes on, so the Server Members intent is on, from those rows.
    assert kwargs["intents"].members is True
    # The first start did the import, and the report rides along for the first on_ready.
    assert kwargs["import_report"] is not None
    assert "Imported the v2 setup" in kwargs["import_report"].owner_notice()


def test_a_later_start_has_no_import_report(order, monkeypatch, tmp_path):
    entrypoint.main()
    StubBot.built.clear()

    entrypoint.main()

    ((_cfg, _db, kwargs),) = StubBot.built
    assert kwargs["import_report"] is None
    assert [row.guild_id for row in kwargs["lounges"]] == [100000000000000001]


def test_a_failed_import_stops_everything_after_it(order, monkeypatch, capsys, tmp_path):
    from newsbot.guilds.importer import ImportFailedError

    def boom(*args, **kwargs):
        order.append("import")
        raise ImportFailedError("the v2 import failed and was rolled back")

    monkeypatch.setattr(entrypoint, "ensure_imported", boom)

    assert entrypoint.main() == 2

    assert order == ["migrate", "import"]  # nothing adopted, nothing built
    assert "the v2 import failed" in capsys.readouterr().err
    assert StubBot.built == []


def test_a_welcome_turned_off_in_config_is_what_the_intents_see(order, monkeypatch, tmp_path):
    # The D5 re-sync runs before the rows are read, so an owner who switches the welcome off in
    # config.yaml gets intents without Server Members on the very next start.
    entrypoint.main()
    edited = tmp_path / "edited.yaml"
    edited.write_text(
        PRODLIKE.read_text().replace("enabled: true\n    message", "enabled: false\n    message")
    )
    monkeypatch.setenv("NEWSBOT_CONFIG", str(edited))
    StubBot.built.clear()

    entrypoint.main()

    ((_cfg, _db, kwargs),) = StubBot.built
    assert kwargs["lounges"][0].welcome_enabled is False
    assert kwargs["intents"].members is False


# --- the adopt step ---


@pytest.fixture
def imported_db(tmp_path):
    path = str(tmp_path / "adopt.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    assert ensure_imported(path, load_config(PRODLIKE), lambda: T_IMPORT) is not None
    return path


def _insert_orphan(path: str, run_date: str) -> None:
    with closing(connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
            "updated_at) VALUES (NULL, ?, 'ok', '[1]', 't', 't')",
            (run_date,),
        )


def _owner_of(path: str, run_date: str):
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT guild_id FROM digests WHERE run_date = ?", (run_date,)
        ).fetchone()
    return row[0] if row else "no row"


def test_rows_a_rolled_back_v22_wrote_are_adopted_by_the_imported_server(imported_db):
    # Roll back to v2.2, it posts today and records the digest with no server; roll forward.
    _insert_orphan(imported_db, "2026-10-02")

    entrypoint._adopt_orphan_digests(imported_db)

    assert _owner_of(imported_db, "2026-10-02") == 100000000000000001


def test_adopting_twice_changes_nothing_and_a_server_with_its_own_row_keeps_it(imported_db):
    with closing(connect(imported_db)) as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
            "updated_at) VALUES (100000000000000001, '2026-10-02', 'ok', '[]', 't', 't')"
        )
    _insert_orphan(imported_db, "2026-10-02")  # a v2.2 row for a day the server already has

    entrypoint._adopt_orphan_digests(imported_db)
    entrypoint._adopt_orphan_digests(imported_db)

    with closing(connect(imported_db)) as conn:
        rows = conn.execute(
            "SELECT guild_id FROM digests WHERE run_date = '2026-10-02' ORDER BY id"
        ).fetchall()
    # `UPDATE OR IGNORE`: the colliding orphan is left alone, never duplicated onto the server.
    assert [r[0] for r in rows] == [100000000000000001, None]


def test_adopting_with_no_imported_server_does_nothing(tmp_path):
    path = str(tmp_path / "plain.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    _insert_orphan(path, "2026-10-02")

    entrypoint._adopt_orphan_digests(path)

    assert _owner_of(path, "2026-10-02") is None


def test_an_adopt_or_resync_that_fails_is_logged_and_the_bot_still_starts(order, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(entrypoint.repo, "adopt_orphan_digests", boom)
    monkeypatch.setattr(entrypoint, "resync_lounge_from_config", boom)

    assert entrypoint.main() == 0

    assert len(StubBot.built) == 1
