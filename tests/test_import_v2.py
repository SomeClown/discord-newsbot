"""The one-time v2 import (design.md §15, plan task 3).

The scenario that matters most is upgrade day: v2.2 has already posted
today's digest, v3 starts, and nobody in the friend's server should see a
second one. `test_upgrade_after_todays_digest_no_second_post` is that test;
the rest pin every row the import writes, and that it writes all of them or
none.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsbot.config import AppConfig, load_config
from newsbot.guilds import importer
from newsbot.guilds.importer import ImportFailedError, ensure_imported, resync_lounge_from_config
from newsbot.store import repo
from newsbot.store.db import connect, migrate

FIXTURES = Path(__file__).parent / "fixtures"
GUILD = 100000000000000001
NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
WIKIQUOTE_PAGES = [
    "Divine Comedy",
    "Fight Club (film)",
    "Hunter S. Thompson",
    "H. L. Mencken",
    "F. Scott Fitzgerald",
    "The Curious Case of Benjamin Button (film)",
    "Oscar Wilde",
    "Mark Twain",
]


def _clock():
    return NOW


@pytest.fixture
def cfg() -> AppConfig:
    return load_config(FIXTURES / "config_v2_prodlike.yaml")


def _legacy(cfg: AppConfig, **changes) -> AppConfig:
    return cfg.model_copy(update={"legacy": cfg.legacy.model_copy(update=changes)})


def _rows(db: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(connect(db)) as conn:
        return conn.execute(sql, params).fetchall()


def _count(db: Path, table: str) -> int:
    return _rows(db, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]  # noqa: S608


def _everything(db: Path) -> dict[str, list]:
    tables = [
        "guilds",
        "guild_games",
        "guild_shift",
        "guild_lounge",
        "guild_code_posts",
        "app_state",
    ]
    out = {t: [tuple(r) for r in _rows(db, f"SELECT * FROM {t}")] for t in tables}  # noqa: S608
    out["digests"] = [tuple(r) for r in _rows(db, "SELECT * FROM digests")]
    out["quotes"] = [tuple(r) for r in _rows(db, "SELECT * FROM lounge_quotes_used")]
    return out


@pytest.fixture
def fresh_db(tmp_path) -> Path:
    path = tmp_path / "fresh.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def test_writes_every_row_field_by_field(v22_db, cfg, codes):
    report = ensure_imported(v22_db, cfg, _clock)
    assert report is not None and report.guild_id == GUILD

    with closing(connect(v22_db)) as conn:
        guild = repo.get_guild(conn, GUILD)
        assert guild.tier == "comped" and guild.set_up is True
        assert (guild.digest_time, guild.timezone) == ("09:00", "America/Los_Angeles")
        assert guild.admin_channel_id == 100000000000000002
        assert guild.joined_at == NOW and guild.imported_at == NOW and guild.updated_at == NOW
        assert guild.permission_problems is None

        assert [(g.game_key, g.channel_id) for g in repo.list_guild_games(conn, GUILD)] == [
            ("borderlands4", 1452017235274240221),
            ("palworld", 1542581309845799013),
            ("diablo4", 1531681353681211524),
        ]

        shift = repo.get_shift(conn, GUILD)
        assert shift.enabled is True and shift.channel_id == 1553597251933438122
        assert shift.ping == "everyone" and shift.enabled_at == NOW

        lounge = repo.get_lounge(conn, GUILD)
        assert lounge.channel_id == 1401806745898061826
        assert lounge.welcome_enabled is True
        assert lounge.welcome_message == (
            "Welcome to {server}, {member}. Pull up a stool; the lighting is questionable."
        )
        assert lounge.quote_enabled is True and lounge.quote_time == "08:00"
        assert lounge.quote_sources == [{"kind": "wikiquote", "value": p} for p in WIKIQUOTE_PAGES]
        assert len(lounge.quote_sources) == 8

        saved = json.loads(repo.app_state_get(conn, "import"))
        assert saved["guild_id"] == GUILD and saved["imported_at"] == NOW.isoformat()
        assert "stool" not in json.dumps(saved)
    assert _count(v22_db, "guilds") == 1


def test_upgrade_after_todays_digest_no_second_post(v22_db, cfg):
    """v2.2 posted today's digest (row 12, 2026-09-30); after the import it is the guild's."""
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.digests_adopted == 4

    today = _rows(v22_db, "SELECT id, guild_id, status FROM digests WHERE run_date = '2026-09-30'")
    assert [(r["id"], r["guild_id"], r["status"]) for r in today] == [(12, GUILD, "ok")]
    assert _rows(v22_db, "SELECT COUNT(*) AS n FROM digests WHERE guild_id IS NULL")[0]["n"] == 0

    # What a per-guild claim for today would hit: the row exists and the
    # (guild, day) uniqueness refuses a second one.
    with closing(connect(v22_db)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, '2026-09-30', 'pending', 't', 't')",
            (GUILD,),
        )


def test_upgrade_with_a_pending_digest_today_adopts_it_too(v4_db, cfg):
    """A pending v2.2 row for today must also belong to the guild, not vanish into a NULL."""
    with closing(connect(v4_db)) as conn:
        conn.execute("DELETE FROM stories WHERE digest_id = 12")
        conn.execute(
            "UPDATE digests SET status = 'pending', posted_message_ids = '[]' WHERE id = 12"
        )
        conn.commit()
        assert migrate(conn) == 5
    ensure_imported(v4_db, cfg, _clock)
    row = _rows(v4_db, "SELECT guild_id, status FROM digests WHERE run_date = '2026-09-30'")[0]
    assert (row["guild_id"], row["status"]) == (GUILD, "pending")


def test_ping_count_and_last_quote_date_carry_over(v22_db, cfg):
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        shift = repo.get_shift(conn, GUILD)
        assert (shift.ping_day, shift.ping_count) == ("2026-09-30", 2)
        assert repo.get_lounge(conn, GUILD).last_quote_date == "2026-09-30"


def test_code_posts_are_exactly_the_posted_and_failed_codes(v22_db, cfg, codes):
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.code_posts == 2
    rows = _rows(
        v22_db,
        "SELECT guild_id, code, status, message_id, pinged, from_roundup, claimed_at "
        "FROM guild_code_posts ORDER BY code",
    )
    assert [tuple(r) for r in rows] == [
        (GUILD, codes["posted"], "posted", 999, 1, 0, "2026-09-29T10:00:00+00:00"),
        (GUILD, codes["failed"], "failed", None, 0, 0, "2026-09-29T10:00:00+00:00"),
    ]


def test_quote_deck_rows_get_the_guild(v22_db, cfg):
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.quotes_backfilled == 2
    guild_ids = {r["guild_id"] for r in _rows(v22_db, "SELECT guild_id FROM lounge_quotes_used")}
    assert guild_ids == {GUILD}


def test_old_history_is_untouched(v22_db, cfg):
    before = _rows(v22_db, "SELECT COUNT(*) AS n FROM stories")[0]["n"]
    ensure_imported(v22_db, cfg, _clock)
    assert _rows(v22_db, "SELECT COUNT(*) AS n FROM stories")[0]["n"] == before == 5
    assert _count(v22_db, "alerted_codes") == 6
    assert _count(v22_db, "digests") == 4


def test_second_call_and_restarts_import_nothing_more(v22_db, cfg):
    assert ensure_imported(v22_db, cfg, _clock) is not None
    snapshot = _everything(v22_db)
    later = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    for _ in range(3):
        assert ensure_imported(v22_db, cfg, lambda: later) is None
    assert _everything(v22_db) == snapshot


def test_never_runs_again_after_the_old_keys_are_deleted(v22_db, cfg):
    ensure_imported(v22_db, cfg, _clock)
    assert ensure_imported(v22_db, cfg.model_copy(update={"legacy": None}), _clock) is None
    assert _count(v22_db, "guilds") == 1


def test_any_existing_guild_row_means_it_never_runs(v22_db, cfg):
    with closing(connect(v22_db)) as conn:
        repo.create_guild(conn, 555, now=_clock)
    assert ensure_imported(v22_db, cfg, _clock) is None
    assert [r["guild_id"] for r in _rows(v22_db, "SELECT guild_id FROM guilds")] == [555]
    assert _count(v22_db, "guild_games") == 0
    assert _rows(v22_db, "SELECT COUNT(*) AS n FROM digests WHERE guild_id IS NULL")[0]["n"] == 4


def test_concurrent_calls_produce_exactly_one_import(v22_db, cfg):
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def go():
        try:
            barrier.wait()
            results.append(ensure_imported(v22_db, cfg, _clock))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(r is not None for r in results) == 1
    assert _count(v22_db, "guilds") == 1 and _count(v22_db, "guild_games") == 3


def test_pure_v3_self_host_imports_nothing(v22_db):
    cfg = load_config(FIXTURES / "config_v3.yaml")
    assert cfg.legacy is None
    before = _everything(v22_db)
    assert ensure_imported(v22_db, cfg, _clock) is None
    assert _everything(v22_db) == before
    assert _count(v22_db, "guilds") == 0


def test_a_legacy_guild_that_is_also_in_comped_guild_ids(v22_db, cfg):
    cfg = cfg.model_copy(update={"comped_guild_ids": [GUILD, 777]})
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild(conn, GUILD).tier == "comped"
        assert repo.get_guild(conn, 777) is None
    assert _count(v22_db, "guilds") == 1


def test_fresh_database_imports_cleanly(fresh_db, cfg):
    report = ensure_imported(fresh_db, cfg, _clock)
    assert (report.digests_adopted, report.code_posts, report.quotes_backfilled) == (0, 0, 0)
    with closing(connect(fresh_db)) as conn:
        assert repo.get_shift(conn, GUILD).ping_count == 0
        assert repo.get_lounge(conn, GUILD).last_quote_date is None


def test_alerts_off_means_no_shift_row(v22_db, cfg):
    ensure_imported(v22_db, _legacy(cfg, shift_enabled=False), _clock)
    assert _count(v22_db, "guild_shift") == 0


def test_zero_max_pings_means_ping_none(v22_db, cfg):
    ensure_imported(v22_db, _legacy(cfg, shift_ping="none", alerts_max_pings=0), _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.get_shift(conn, GUILD).ping == "none"


def test_lounge_off_or_absent_means_no_lounge_row(v22_db, tmp_path, cfg):
    off = cfg.legacy.lounge.model_copy(
        update={
            "welcome": cfg.legacy.lounge.welcome.model_copy(update={"enabled": False}),
            "daily_quote": cfg.legacy.lounge.daily_quote.model_copy(update={"enabled": False}),
        }
    )
    ensure_imported(v22_db, _legacy(cfg, lounge=off), _clock)
    assert _count(v22_db, "guild_lounge") == 0
    # The deck rows are still the guild's; the backfill doesn't depend on the lounge row.
    assert (
        _rows(v22_db, "SELECT COUNT(*) AS n FROM lounge_quotes_used WHERE guild_id IS NULL")[0]["n"]
        == 0
    )

    absent_db = tmp_path / "absent.db"
    with closing(connect(absent_db)) as conn:
        migrate(conn)
    ensure_imported(absent_db, _legacy(cfg, lounge=None), _clock)
    assert _count(absent_db, "guild_lounge") == 0


def test_default_quote_sources_are_written_out(fresh_db, cfg):
    quote = cfg.legacy.lounge.daily_quote.model_copy(update={"sources": None})
    lounge = cfg.legacy.lounge.model_copy(update={"daily_quote": quote})
    ensure_imported(fresh_db, _legacy(cfg, lounge=lounge), _clock)
    with closing(connect(fresh_db)) as conn:
        sources = repo.get_lounge(conn, GUILD).quote_sources
    assert sources and all(s["kind"] == "wikiquote" for s in sources)


def test_eleven_topics_fails_with_nothing_written(v22_db, cfg):
    games = [(f"game{i}", 1000 + i) for i in range(11)]
    before = _everything(v22_db)
    with pytest.raises(ImportFailedError, match="at most 10"):
        ensure_imported(v22_db, _legacy(cfg, games=games), _clock)
    assert _everything(v22_db) == before
    assert _count(v22_db, "guilds") == 0


@pytest.mark.parametrize(
    "failing", ["_write_shift", "_write_lounge", "backfill_lounge_quotes_guild"]
)
def test_partial_failure_rolls_everything_back(v22_db, cfg, monkeypatch, failing):
    """Blow up after the guild, games and more are already written; none of it may stay."""
    before = _everything(v22_db)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    target = repo if failing.startswith("backfill") else importer
    monkeypatch.setattr(target, failing, boom)
    with pytest.raises(ImportFailedError, match="rolled back.*disk on fire"):
        ensure_imported(v22_db, cfg, _clock)
    assert _everything(v22_db) == before
    assert _count(v22_db, "guilds") == 0

    # And a healthy retry afterwards still works: no half-import blocked it.
    monkeypatch.undo()
    assert ensure_imported(v22_db, cfg, _clock) is not None


def test_a_database_constraint_failure_rolls_back_too(v22_db, cfg):
    """A SHiFT row with no channel trips a CHECK after the guild row is in."""
    before = _everything(v22_db)
    with pytest.raises(ImportFailedError, match="rolled back"):
        ensure_imported(v22_db, _legacy(cfg, shift_channel_id=None), _clock)
    assert _everything(v22_db) == before


def test_logs_the_sections_and_deletable_keys_and_no_welcome_text(v22_db, cfg, caplog):
    caplog.set_level(logging.INFO, logger="newsbot.guilds.importer")
    ensure_imported(v22_db, cfg, _clock)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "borderlands4 -> 1452017235274240221" in text
    assert "09:00 America/Los_Angeles" in text
    assert "ping everyone" in text and "8 quote sources" in text
    assert "adopted 4 digest rows, 2 already-posted SHiFT codes, 2 lounge quote rows" in text
    assert (
        "these keys are now only read by the import and can be deleted from config.yaml: "
        "guild_id, digest.time, digest.timezone, topics[].channel_id, alerts.enabled, "
        "alerts.channel_id, lounge (plus topics and sources once catalog: is in place)"
    ) in text
    assert "lighting is questionable" not in text and "Pull up a stool" not in text


def test_a_second_start_logs_only_that_the_import_already_happened(v22_db, cfg, caplog):
    ensure_imported(v22_db, cfg, _clock)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="newsbot.guilds.importer")
    assert ensure_imported(v22_db, cfg, _clock) is None
    assert [r.getMessage() for r in caplog.records] == [
        "import: already happened on 2026-09-30T17:00:00+00:00; these keys can be deleted "
        "from config.yaml: " + importer._DELETABLE_KEYS
    ]


def test_owner_notice_names_what_was_imported(v22_db, cfg):
    assert ensure_imported(v22_db, cfg, _clock).owner_notice() == (
        "Imported the v2 setup for this server (3 games, SHiFT, lounge); "
        "the details are in the log."
    )


# --- D5: the lounge: block re-syncs at startup while it is present ---


def _edited_lounge(cfg: AppConfig, **welcome_changes):
    lounge = cfg.legacy.lounge
    return lounge.model_copy(update={"welcome": lounge.welcome.model_copy(update=welcome_changes)})


def test_resync_updates_the_row_without_touching_last_quote_date(v22_db, cfg, caplog):
    ensure_imported(v22_db, cfg, _clock)
    edited = _legacy(
        cfg, lounge=_edited_lounge(cfg, message="Hello {member}, welcome to {server}.")
    )
    caplog.set_level(logging.INFO, logger="newsbot.guilds.importer")

    assert resync_lounge_from_config(v22_db, edited) is True
    with closing(connect(v22_db)) as conn:
        lounge = repo.get_lounge(conn, GUILD)
    assert lounge.welcome_message == "Hello {member}, welcome to {server}."
    assert lounge.last_quote_date == "2026-09-30"
    assert any("re-synced guild" in r.getMessage() for r in caplog.records)


def test_resync_clobber_check_with_a_changed_row_date(v22_db, cfg):
    """The row's own date wins even if lounge_state (v2.2's copy) says otherwise."""
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        conn.execute("UPDATE guild_lounge SET last_quote_date = '2026-10-02'")
        conn.commit()
    edited = _legacy(cfg, lounge=_edited_lounge(cfg, message="Hi {member}."))
    assert resync_lounge_from_config(v22_db, edited) is True
    assert _rows(v22_db, "SELECT last_quote_date FROM guild_lounge")[0][0] == "2026-10-02"


def test_a_noop_resync_writes_and_logs_nothing(v22_db, cfg, caplog):
    ensure_imported(v22_db, cfg, _clock)
    before = _everything(v22_db)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="newsbot.guilds.importer")
    assert resync_lounge_from_config(v22_db, cfg) is False
    assert _everything(v22_db) == before
    assert caplog.records == []


def test_resync_without_a_lounge_block_or_before_the_import_does_nothing(v22_db, cfg):
    assert resync_lounge_from_config(v22_db, cfg) is False  # not imported yet
    ensure_imported(v22_db, cfg, _clock)
    before = _everything(v22_db)
    assert resync_lounge_from_config(v22_db, _legacy(cfg, lounge=None)) is False
    assert resync_lounge_from_config(v22_db, cfg.model_copy(update={"legacy": None})) is False
    assert _everything(v22_db) == before


def test_resync_can_switch_the_features_off(v22_db, cfg):
    ensure_imported(v22_db, cfg, _clock)
    lounge = cfg.legacy.lounge
    off = lounge.model_copy(
        update={
            "welcome": lounge.welcome.model_copy(update={"enabled": False}),
            "daily_quote": lounge.daily_quote.model_copy(update={"enabled": False}),
        }
    )
    assert resync_lounge_from_config(v22_db, _legacy(cfg, lounge=off)) is True
    with closing(connect(v22_db)) as conn:
        row = repo.get_lounge(conn, GUILD)
    assert (row.welcome_enabled, row.quote_enabled) == (False, False)
    assert row.last_quote_date == "2026-09-30"
    assert resync_lounge_from_config(v22_db, _legacy(cfg, lounge=off)) is False
