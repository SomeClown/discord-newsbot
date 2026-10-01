"""Adversarial tests for the one-time v2 import (plan task 3), beyond test_import_v2.py.

Angle (test-engineer brief, 2026-09-30): the import is a house move that
has to finish before the morning digest, and it only gets one try at being
right. test_import_v2.py walks the happy path in a tidy database. This file
goes and finds the ways a real one is untidy: the upgrade landing at every
hour of the digest's day, a v2.2 database with a crashed run or a garbage
`ping_count` in it, a config that puts every channel in one room, two
processes starting at once, a process killed between any two statements,
and a welcome message that must not show up in a log no matter how hard we
ask the log to talk.

Most of the file pins "yes, that's fine". A few tests pin behavior I'd call
safe but surprising; those say "documented" so the owner can overrule them.
Real bugs are strict xfails with the reason spelled out; the implement agent
owns the fix, and flipping one to a plain test is the proof.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import types
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from newsbot.bot.format import discord_len
from newsbot.config import AppConfig, ConfigError, load_config
from newsbot.guilds import importer
from newsbot.guilds.importer import ImportFailedError, ensure_imported, resync_lounge_from_config
from newsbot.lounge.default_sources import DEFAULT_WIKIQUOTE_PAGES
from newsbot.shift.decide import CodeCandidate, plan_alerts
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings, Usage

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT = FIXTURES / "v22_repo_snapshot.txt"
REPO_ROOT = Path(__file__).parent.parent
GUILD = 100000000000000001
TEST_SERVER = 1552824311608512532
NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
TODAY = date(2026, 9, 30)
SENTINEL = "ZXQ-SENTINEL-WELCOME-7731"
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def _clock():
    return NOW


@pytest.fixture(scope="module")
def v22() -> types.ModuleType:
    """The v2.2.0 repo module from the checked-in snapshot: v2.2's real SQL."""
    module = types.ModuleType("v22_repo_import")
    exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
    return module


# --- config and database builders ---


def _prodlike_text() -> str:
    return (FIXTURES / "config_v2_prodlike.yaml").read_text()


def _load(tmp_path: Path, text: str, name: str = "config.yaml") -> AppConfig:
    path = tmp_path / name
    path.write_text(text)
    return load_config(path)


def _with_lounge(block: str) -> str:
    """The prod-like config with its `lounge:` block (the last thing in the file) replaced."""
    return _prodlike_text().split("\nlounge:")[0] + "\n" + block


def _yaml_str(text: str) -> str:
    """A YAML double-quoted scalar; JSON strings are valid YAML ones (emoji kept literal)."""
    return json.dumps(text, ensure_ascii=False)


def _lounge_block(welcome: str | None = None, quote: bool = False, channel: bool = True) -> str:
    lines = ["lounge:"]
    if channel:
        lines.append("  channel_id: 1401806745898061826")
    if welcome is not None:
        lines += ["  welcome:", "    enabled: true", f"    message: {_yaml_str(welcome)}"]
    if quote:
        lines += ["  daily_quote:", "    enabled: true", '    time: "08:30"']
    return "\n".join(lines) + "\n" if len(lines) > 1 else "lounge: {}\n"


@pytest.fixture
def cfg() -> AppConfig:
    return load_config(FIXTURES / "config_v2_prodlike.yaml")


def _rows(db: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(connect(db)) as conn:
        return conn.execute(sql, params).fetchall()


def _sql(db: Path, sql: str, params: tuple = ()) -> None:
    with closing(connect(db)) as conn, conn:
        conn.execute(sql, params)


def _everything(db: Path) -> dict[str, list]:
    """Every table the import could touch, plus what it reads, as plain tuples."""
    tables = [
        "guilds",
        "guild_games",
        "guild_shift",
        "guild_lounge",
        "guild_code_posts",
        "app_state",
        "digests",
        "alerted_codes",
        "alert_state",
        "lounge_quotes_used",
        "lounge_state",
        "stories",
    ]
    return {t: [tuple(r) for r in _rows(db, f"SELECT * FROM {t}")] for t in tables}  # noqa: S608


def _healthy(db: Path) -> None:
    with closing(connect(db)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _copy_db(src: Path, dst: Path) -> Path:
    with closing(connect(src)) as a, closing(sqlite3.connect(dst)) as b:
        a.backup(b)
    return dst


@pytest.fixture
def fresh_db(tmp_path) -> Path:
    path = tmp_path / "fresh.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _lounge_row(db: Path) -> LoungeSettings | None:
    with closing(connect(db)) as conn:
        return repo.get_lounge(conn, GUILD)


# --- 1. the upgrade lands at every point of the digest's day ---


def _set_today(db: Path, state: str) -> None:
    """Put today's (2026-09-30) digest in the state v2.2 would have left it in."""
    if state == "before":
        _sql(db, "DELETE FROM digests WHERE id = 12")
    elif state == "pending":
        _sql(db, "UPDATE digests SET status = 'pending', posted_message_ids = '[]' WHERE id = 12")
    elif state == "failed":
        _sql(
            db,
            "UPDATE digests SET status = 'failed', posted_message_ids = '[]', "
            "error_notes = 'discord down' WHERE id = 12",
        )
    elif state == "partial":
        _sql(db, "UPDATE digests SET status = 'partial' WHERE id = 12")
    else:
        assert state == "ok"


@pytest.mark.parametrize(
    ("state", "claim_blocked"),
    [("before", False), ("pending", True), ("ok", True), ("partial", True), ("failed", False)],
)
def test_upgrade_at_every_point_of_the_day_leaves_exactly_one_digest_for_the_guild(
    v22, v22_db, cfg, state, claim_blocked
):
    """Whatever state v2.2 left today in, v3 sees one row, the guild's, and never a second."""
    _set_today(v22_db, state)
    ensure_imported(v22_db, cfg, _clock)

    with closing(connect(v22_db)) as conn:
        seen = v22.get_digest(conn, TODAY)
        if state == "before":
            assert seen is None
        else:
            assert (seen.id, seen.status) == (12, "pending" if state == "pending" else state)

        # The v2-era claim is what a same-day restart of the old code path would ask
        # (v2.2's own, from the snapshot; the live module no longer has it).
        claimed = v22.claim_digest(conn, TODAY, force=False, now=_clock)
        assert (claimed is None) is claim_blocked
        if state == "failed":
            assert claimed == 12  # reclaimed in place: that day never posted

        # Anything v2's claim_digest inserted is an orphan (v2's insert names no guild);
        # the startup adopt step makes it the guild's.
        repo.adopt_orphan_digests(conn, GUILD)
        today = conn.execute(
            "SELECT id, guild_id FROM digests WHERE run_date = '2026-09-30'"
        ).fetchall()
        assert [r["guild_id"] for r in today] == [GUILD]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
                "VALUES (?, '2026-09-30', 'pending', 't', 't')",
                (GUILD,),
            )
    _healthy(v22_db)


def test_upgrade_before_the_digest_lets_v3_claim_today_exactly_once(v22_db, cfg):
    _set_today(v22_db, "before")
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.digests_adopted == 3  # the three earlier days only
    with closing(connect(v22_db)) as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (?, '2026-09-30', 'pending', 't', 't')",
            (GUILD,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
                "VALUES (?, '2026-09-30', 'pending', 't', 't')",
                (GUILD,),
            )


def test_every_adopted_digest_keeps_its_status_ids_and_stories(v22_db, cfg):
    before = {
        r["id"]: (r["run_date"], r["status"], r["posted_message_ids"], r["error_notes"])
        for r in _rows(v22_db, "SELECT * FROM digests")
    }
    stories = [tuple(r) for r in _rows(v22_db, "SELECT id, digest_id FROM stories ORDER BY id")]
    ensure_imported(v22_db, cfg, _clock)
    after = {
        r["id"]: (r["run_date"], r["status"], r["posted_message_ids"], r["error_notes"])
        for r in _rows(v22_db, "SELECT * FROM digests")
    }
    assert after == before
    assert [tuple(r) for r in _rows(v22_db, "SELECT id, digest_id FROM stories ORDER BY id")] == (
        stories
    )
    _healthy(v22_db)


# --- 1b. codes: what v2.2 alerted must never be alerted for this guild again ---

ROUNDUP_POSTED = "GGGGG-GGGGG-GGGGG-GGGGG-GGGG7"
POSTED_NO_MESSAGE = "HHHHH-HHHHH-HHHHH-HHHHH-HHHH8"


@pytest.fixture
def v22_codes(v22, v22_db, codes) -> dict[str, str]:
    """The fixture's six codes plus a roundup v2.2 posted for real and a posted code with no id."""
    with closing(connect(v22_db)) as conn:
        v22.claim_codes(
            conn,
            [(ROUNDUP_POSTED, "Feed", "https://example.com/r")],
            pinged=False,
            local_day="2026-09-30",
            now=_clock,
            from_roundup=True,
        )
        v22.mark_codes_posted(conn, [ROUNDUP_POSTED], message_id=4242)
        v22.claim_codes(
            conn,
            [(POSTED_NO_MESSAGE, "Feed", "https://example.com/n")],
            pinged=False,
            local_day="2026-09-30",
            now=_clock,
        )
        v22.mark_codes_posted(conn, [POSTED_NO_MESSAGE], message_id=None)
    return {**codes, "roundup_posted": ROUNDUP_POSTED, "posted_no_message": POSTED_NO_MESSAGE}


def test_v22_records_a_posted_roundup_as_posted_with_the_roundup_flag(v22_db, v22_codes):
    """Why the importer's status filter is right: v2.2 never writes status 'roundup' for a post."""
    row = _rows(
        v22_db,
        "SELECT status, from_roundup, pinged, message_id FROM alerted_codes WHERE code = ?",
        (ROUNDUP_POSTED,),
    )[0]
    assert tuple(row) == ("posted", 1, 0, 4242)


def test_a_roundup_v22_posted_is_copied_and_a_silent_roundup_is_not(v22_db, cfg, v22_codes):
    ensure_imported(v22_db, cfg, _clock)
    copied = {r["code"]: r for r in _rows(v22_db, "SELECT * FROM guild_code_posts")}
    assert set(copied) == {
        v22_codes["posted"],
        v22_codes["failed"],
        ROUNDUP_POSTED,
        POSTED_NO_MESSAGE,
    }
    assert (copied[ROUNDUP_POSTED]["status"], copied[ROUNDUP_POSTED]["from_roundup"]) == (
        "posted",
        1,
    )
    assert copied[ROUNDUP_POSTED]["pinged"] == 0 and copied[ROUNDUP_POSTED]["message_id"] == 4242
    assert copied[POSTED_NO_MESSAGE]["message_id"] is None
    assert v22_codes["roundup"] not in copied  # silent overflow: v2.2 never posted it


def test_no_v2_code_can_be_released_or_claimed_again(v22, v22_db, cfg, v22_codes):
    """Every code v2.2 knew stays known: global detection is what stops a re-alert.

    The per-guild table only holds what v2.2 posted or failed; 'pending',
    'seeded', 'too_old' and the silent 'roundup' rows are absent from it on
    purpose, and this is the test that says that is safe: they're all still
    in `alerted_codes`, so `plan_alerts` drops them and `claim_codes` refuses.
    """
    ensure_imported(v22_db, cfg, _clock)
    all_codes = list(v22_codes.values())
    with closing(connect(v22_db)) as conn:
        known = repo.known_codes(conn, all_codes)
        assert known == set(all_codes)

        candidates = [
            CodeCandidate(c, golden=False, source_name="Feed", item_url="u", fresh=True)
            for c in all_codes
        ]
        plan = plan_alerts(
            candidates, known=known, seeded=True, seeding_ok=True, pings_today=0, max_pings=3
        )
        assert plan.to_post == [] and plan.roundup_to_post == [] and plan.silent == []

        spent = repo.get_alert_state(conn).ping_count
        for code in all_codes:
            with pytest.raises(sqlite3.IntegrityError):
                v22.claim_codes(
                    conn, [(code, "Feed", "u")], pinged=True, local_day="2026-09-30", now=_clock
                )
        assert repo.get_alert_state(conn).ping_count == spent  # no budget burned by the refusals


@pytest.mark.parametrize("which", ["posted", "failed", "roundup_posted", "posted_no_message"])
def test_a_code_the_guild_already_has_cannot_be_claimed_per_guild_again(
    v22_db, cfg, v22_codes, which
):
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, claimed_at) "
            "VALUES (?, ?, 'pending', 't')",
            (GUILD, v22_codes[which]),
        )


def test_documented_pending_codes_are_not_copied(v22_db, cfg, codes):
    """A v2.2 'pending' code is skipped, so the guild has no row for it.

    Safe today: the code stays in `alerted_codes` (never released again) and
    v2.2's own startup turns it 'failed'. The owner might want it copied as
    'failed' so a guild-level ledger is complete, which is a one-word change.
    """
    ensure_imported(v22_db, cfg, _clock)
    assert _rows(v22_db, "SELECT 1 FROM guild_code_posts WHERE code = ?", (codes["pending"],)) == []


def test_a_posted_code_with_no_message_id_and_a_huge_code_set_copy_faithfully(v22_db, cfg):
    with closing(connect(v22_db)) as conn, conn:
        conn.executemany(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, message_id, "
            "pinged, status, from_roundup) VALUES (?, ?, 'Feed', 'u', NULL, 0, ?, 0)",
            [
                (f"{i:05d}-00000-00000-00000-00000", "2026-08-01T00:00:00+00:00", s)
                for i, s in enumerate(["posted", "failed", "seeded", "too_old", "pending"] * 1200)
            ],
        )
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.code_posts == 2 + 2400
    assert (
        _rows(v22_db, "SELECT COUNT(*) FROM guild_code_posts WHERE message_id IS NULL")[0][0] > 2400
    )
    _healthy(v22_db)


# --- 2. messy v2.2 databases ---


def test_a_huge_history_imports_fast_and_completely(v22_db, cfg):
    base = date(2015, 1, 1).toordinal()
    with closing(connect(v22_db)) as conn, conn:
        conn.executemany(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES (?, 'ok', 't', 't')",
            [(date.fromordinal(base + i).isoformat(),) for i in range(4000)],
        )
        conn.executemany(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES (?, ?, ?)",
            [
                ("wikiquote:Oscar Wilde", hashlib.sha256(str(i).encode()).hexdigest(), "t")
                for i in range(4000)
            ],
        )
    started = time.monotonic()
    report = ensure_imported(v22_db, cfg, _clock)
    assert time.monotonic() - started < 10
    assert report.digests_adopted == 4004
    assert report.quotes_backfilled == 4002
    assert _rows(v22_db, "SELECT COUNT(*) FROM digests WHERE guild_id IS NULL")[0][0] == 0
    assert (
        _rows(v22_db, "SELECT COUNT(*) FROM lounge_quotes_used WHERE guild_id IS NULL")[0][0] == 0
    )
    _healthy(v22_db)


def test_a_crashed_pending_digest_and_odd_neighbours_all_get_the_guild(v22_db, cfg):
    with closing(connect(v22_db)) as conn, conn:
        conn.executemany(
            "INSERT INTO digests (run_date, status, posted_message_ids, error_notes, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 't', 't')",
            [
                ("2026-09-26", "pending", "[]", None),  # a crashed run nobody cleaned up
                ("2026-09-25", "partial", "[1, 2]", "one game failed"),
                ("2026-09-24", "failed", "[]", "x" * 5000),
                ("2026-10-01", "pending", "[]", None),  # a clock-skewed future row
            ],
        )
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.digests_adopted == 8
    statuses = {r["run_date"]: r["status"] for r in _rows(v22_db, "SELECT * FROM digests")}
    assert statuses["2026-09-26"] == "pending" and statuses["2026-10-01"] == "pending"
    assert _rows(v22_db, "SELECT COUNT(*) FROM digests WHERE guild_id != ?", (GUILD,))[0][0] == 0


def test_missing_alert_state_means_a_fresh_ping_budget(v22_db, cfg):
    _sql(v22_db, "DELETE FROM alert_state")
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        shift = repo.get_shift(conn, GUILD)
    assert (shift.ping_day, shift.ping_count) == (None, 0)


@pytest.mark.parametrize("junk", ["abc", "2.0", "0x10", "1e3", "99999999999999999999"])
def test_a_garbage_ping_count_refuses_to_import_and_writes_nothing(v22_db, cfg, junk):
    """Documented: the import won't guess. v2.2 itself crashes on the same value."""
    _sql(v22_db, "UPDATE alert_state SET value = ? WHERE key = 'ping_count'", (junk,))
    before = _everything(v22_db)
    with pytest.raises(ImportFailedError, match="rolled back"):
        ensure_imported(v22_db, cfg, _clock)
    assert _everything(v22_db) == before


@pytest.mark.parametrize("value", ["", "  3  ", "٣"])
def test_a_blank_or_padded_ping_count_is_read_the_way_int_reads_it(v22_db, cfg, value):
    _sql(v22_db, "UPDATE alert_state SET value = ? WHERE key = 'ping_count'", (value,))
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.get_shift(conn, GUILD).ping_count == int(value.strip() or 0)


@pytest.mark.parametrize("junk", ["yesterday-ish", "2026-9-3", "", "30/09/2026"])
def test_a_wrong_format_ping_day_is_copied_verbatim_and_harmlessly(v22_db, cfg, junk):
    """Documented: v3's claim compares it to the local day and resets on any mismatch."""
    _sql(v22_db, "UPDATE alert_state SET value = ? WHERE key = 'ping_day'", (junk,))
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.get_shift(conn, GUILD).ping_day == junk


def test_missing_or_garbage_lounge_state(v22_db, tmp_path, cfg):
    _sql(v22_db, "DELETE FROM lounge_state")
    ensure_imported(v22_db, cfg, _clock)
    assert _lounge_row(v22_db).last_quote_date is None

    garbage_db = _copy_db(v22_db, tmp_path / "garbage.db")
    with closing(connect(garbage_db)) as conn, conn:
        conn.execute("DELETE FROM guilds")
        conn.execute("DELETE FROM app_state")
        conn.execute("INSERT INTO lounge_state (key, value) VALUES ('last_quote_date', 'lunch')")
    ensure_imported(garbage_db, cfg, _clock)
    assert _lounge_row(garbage_db).last_quote_date == "lunch"  # documented: copied, not judged


def test_quote_history_for_sources_no_longer_in_config_still_gets_the_guild(v22_db, cfg):
    _sql(
        v22_db,
        "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) "
        "VALUES ('wikiquote:A Page Since Deleted', ?, 't')",
        ("c" * 64,),
    )
    report = ensure_imported(v22_db, cfg, _clock)
    assert report.quotes_backfilled == 3
    assert (
        _rows(v22_db, "SELECT COUNT(*) FROM lounge_quotes_used WHERE guild_id IS NULL")[0][0] == 0
    )


def test_a_database_with_no_v2_history_at_all_but_a_full_config_still_imports(fresh_db, cfg):
    with closing(connect(fresh_db)) as conn, conn:
        conn.execute("DELETE FROM alert_state")
    report = ensure_imported(fresh_db, cfg, _clock)
    assert report.guild_id == GUILD
    _healthy(fresh_db)


# --- 3. config edge cases ---


def test_one_channel_for_everything(v22_db, tmp_path):
    text = _prodlike_text()
    for channel in (
        "1452017235274240221",
        "1542581309845799013",
        "1531681353681211524",
        "1553597251933438122",
        "1401806745898061826",
        "100000000000000002",
    ):
        text = text.replace(channel, "777000000000000001")
    ensure_imported(v22_db, _load(tmp_path, text), _clock)
    with closing(connect(v22_db)) as conn:
        games = repo.list_guild_games(conn, GUILD)
        assert [g.channel_id for g in games] == [777000000000000001] * 3
        assert repo.get_shift(conn, GUILD).channel_id == 777000000000000001
        assert repo.get_lounge(conn, GUILD).channel_id == 777000000000000001
        assert repo.get_guild(conn, GUILD).admin_channel_id == 777000000000000001


def test_admin_channel_equal_to_a_game_channel(v22_db, tmp_path):
    text = _prodlike_text().replace("100000000000000002", "1452017235274240221")
    ensure_imported(v22_db, _load(tmp_path, text), _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild(conn, GUILD).admin_channel_id == 1452017235274240221
        assert [g.channel_id for g in repo.list_guild_games(conn, GUILD)][0] == 1452017235274240221


def test_no_admin_channel_at_all(v22_db, tmp_path):
    text = _prodlike_text().replace("admin_channel_id: 100000000000000002\n", "")
    report = ensure_imported(v22_db, _load(tmp_path, text), _clock)
    assert report.admin_channel_id is None
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild(conn, GUILD).admin_channel_id is None


def test_alerts_for_a_topic_that_is_not_followed(v22_db, tmp_path):
    """Whatever the loader decides, the import must not write a half answer."""
    text = _prodlike_text().replace(
        "topics: [borderlands4]\n  interval", "topics: [nosuchgame]\n  interval"
    )
    try:
        cfg = _load(tmp_path, text)
    except ConfigError:
        return  # the loader refuses it: nothing for the import to see
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        assert [g.game_key for g in repo.list_guild_games(conn, GUILD)] == [
            "borderlands4",
            "palworld",
            "diablo4",
        ]
        assert repo.get_shift(conn, GUILD).enabled is True  # alerts.topics is global, not per guild


@pytest.mark.parametrize(
    ("block", "welcome", "quote", "has_row"),
    [
        (_lounge_block(welcome="Hi {member}."), True, False, True),
        (_lounge_block(quote=True), False, True, True),
        (_lounge_block(), False, False, False),
        (_lounge_block(channel=False), False, False, False),
    ],
    ids=["welcome-only", "quote-only", "neither-with-channel", "neither-no-channel"],
)
def test_lounge_block_shapes(v22_db, tmp_path, block, welcome, quote, has_row):
    report = ensure_imported(v22_db, _load(tmp_path, _with_lounge(block)), _clock)
    row = _lounge_row(v22_db)
    if not has_row:
        assert row is None and report.lounge is None
        return
    assert (row.welcome_enabled, row.quote_enabled) == (welcome, quote)
    assert row.channel_id == 1401806745898061826
    # The default list is written out even for a welcome-only row.
    assert len(row.quote_sources) == len(DEFAULT_WIKIQUOTE_PAGES)
    assert report.lounge["source_count"] == len(DEFAULT_WIKIQUOTE_PAGES)


def test_a_lounge_with_no_sources_gets_the_default_list_written(v22_db, tmp_path):
    ensure_imported(v22_db, _load(tmp_path, _with_lounge(_lounge_block(quote=True))), _clock)
    assert [s["value"] for s in _lounge_row(v22_db).quote_sources] == list(DEFAULT_WIKIQUOTE_PAGES)


WELCOMES = [
    "Welcome to {server}, {member}. \U0001f37b\U0001f37b Pull up a stool.",
    "Bienvenue {member} à {server}; café ☕ 日本語 مرحبا",
    "Line one\nLine two with \"quotes\", 'apostrophes', and a # hash: colon",
    "\U0001f468‍\U0001f469‍\U0001f467 family {member}",
]


@pytest.mark.parametrize("message", WELCOMES)
def test_unicode_and_emoji_welcome_round_trips_through_import_and_resync(v22_db, tmp_path, message):
    cfg = _load(tmp_path, _with_lounge(_lounge_block(welcome=message)))
    ensure_imported(v22_db, cfg, _clock)
    assert _lounge_row(v22_db).welcome_message == message
    assert resync_lounge_from_config(v22_db, cfg) is False  # byte-for-byte equal: nothing to write


def _welcome_of_length(units: int) -> str:
    """A welcome whose worst-case rendered length is exactly `units` UTF-16 units."""
    pairs, single = divmod(units, 2)
    return "\U0001f37a" * pairs + "x" * single


def test_welcome_at_exactly_the_limit_imports_and_one_unit_over_is_refused_by_the_loader(
    v22_db, tmp_path
):
    at_limit = _welcome_of_length(2000)
    assert discord_len(at_limit) == 2000
    cfg = _load(tmp_path, _with_lounge(_lounge_block(welcome=at_limit)))
    ensure_imported(v22_db, cfg, _clock)
    assert _lounge_row(v22_db).welcome_message == at_limit

    with pytest.raises(ConfigError, match="2000"):
        _load(tmp_path, _with_lounge(_lounge_block(welcome=_welcome_of_length(2001))), "over.yaml")


def test_legacy_guild_that_is_also_the_home_guild(v22_db, cfg):
    """The dev and test server case: D9 makes it home and v2's guild_id makes it imported."""
    assert cfg.home_guild_id == cfg.legacy.guild_id == GUILD
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        guild = repo.get_guild(conn, GUILD)
        assert guild.tier == "comped" and guild.set_up and guild.imported_at == NOW
        assert len(repo.list_guild_games(conn, GUILD)) == 3
        assert repo.get_shift(conn, GUILD).enabled and repo.get_lounge(conn, GUILD)
    assert _rows(v22_db, "SELECT COUNT(*) FROM guilds")[0][0] == 1


def test_a_home_guild_that_differs_from_the_legacy_guild_gets_no_row(v22_db, tmp_path):
    text = f"home_guild_id: {TEST_SERVER}\n" + _prodlike_text()
    cfg = _load(tmp_path, text)
    assert cfg.home_guild_id == TEST_SERVER and cfg.legacy.guild_id == GUILD
    ensure_imported(v22_db, cfg, _clock)
    assert [r["guild_id"] for r in _rows(v22_db, "SELECT guild_id FROM guilds")] == [GUILD]
    # and the resync follows the imported guild, not the home one
    assert resync_lounge_from_config(v22_db, cfg) is False


def test_documented_a_config_with_no_games_imports_a_guild_with_no_games(v22_db, cfg):
    bare = cfg.model_copy(update={"legacy": cfg.legacy.model_copy(update={"games": []})})
    ensure_imported(v22_db, bare, _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild(conn, GUILD).set_up is True
        assert repo.list_guild_games(conn, GUILD) == []


def test_exactly_ten_games_is_fine_and_eleven_writes_no_import_state(v22_db, cfg):
    def with_games(n):
        games = [(f"game{i}", 2000 + i) for i in range(n)]
        return cfg.model_copy(update={"legacy": cfg.legacy.model_copy(update={"games": games})})

    with pytest.raises(ImportFailedError, match="nothing was imported"):
        ensure_imported(v22_db, with_games(11), _clock)
    assert _rows(v22_db, "SELECT COUNT(*) FROM app_state")[0][0] == 0
    assert ensure_imported(v22_db, with_games(10), _clock).games[-1] == ("game9", 2009)


def test_a_duplicate_game_key_rolls_everything_back(v22_db, cfg):
    dup = cfg.model_copy(
        update={"legacy": cfg.legacy.model_copy(update={"games": [("a", 1), ("a", 2)]})}
    )
    before = _everything(v22_db)
    with pytest.raises(ImportFailedError, match="rolled back"):
        ensure_imported(v22_db, dup, _clock)
    assert _everything(v22_db) == before


def test_deleting_the_only_guild_does_not_brick_the_next_start(v22_db, cfg):
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        assert repo.delete_guild(conn, GUILD)
    ensure_imported(v22_db, cfg, _clock)  # must not raise


# --- 4. concurrency and crash safety ---

_CHILD = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from newsbot.config import load_config
    from newsbot.guilds import importer
    from newsbot.store import repo

    db, cfg_path, go, crash_at = sys.argv[1:5]
    if crash_at:
        original = getattr(repo, crash_at)
        def die(*a, **k):
            os._exit(9)  # no rollback, no cleanup: the plug pulled out of the wall
        setattr(repo, crash_at, die)
    cfg = load_config(cfg_path)
    Path(f"{go}.ready.{os.getpid()}").touch()
    while not Path(go).exists():
        time.sleep(0.001)
    report = importer.ensure_imported(db, cfg)
    print("IMPORTED" if report else "NOOP")
    """
)


def _spawn(db: Path, cfg_path: Path, go: Path, crash_at: str = "") -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _CHILD, str(db), str(cfg_path), str(go), crash_at],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release(go: Path, procs: list[subprocess.Popen]) -> None:
    deadline = time.monotonic() + 30
    while len(list(go.parent.glob(f"{go.name}.ready.*"))) < len(procs):
        assert time.monotonic() < deadline, "children never got ready"
        time.sleep(0.01)
    go.touch()


def test_three_real_processes_importing_at_once_produce_exactly_one_import(v22_db, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_prodlike_text())
    go = tmp_path / "go"
    procs = [_spawn(v22_db, cfg_path, go) for _ in range(3)]
    _release(go, procs)
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    assert sorted(o[0].strip() for o in outs) == ["IMPORTED", "NOOP", "NOOP"]
    assert _rows(v22_db, "SELECT COUNT(*) FROM guilds")[0][0] == 1
    assert _rows(v22_db, "SELECT COUNT(*) FROM guild_games")[0][0] == 3
    assert _rows(v22_db, "SELECT COUNT(*) FROM guild_code_posts")[0][0] == 2
    _healthy(v22_db)


@pytest.mark.parametrize(
    "crash_at",
    ["adopt_orphan_digests_in_tx", "backfill_guild_code_posts", "backfill_lounge_quotes_guild"],
)
def test_a_process_killed_mid_import_leaves_nothing_and_the_retry_works(v22_db, tmp_path, crash_at):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_prodlike_text())
    before = _everything(v22_db)

    go = tmp_path / "go_crash"
    victim = _spawn(v22_db, cfg_path, go, crash_at)
    _release(go, [victim])
    victim.communicate(timeout=60)
    assert victim.returncode == 9  # it really did die mid-transaction
    assert _everything(v22_db) == before
    _healthy(v22_db)

    go2 = tmp_path / "go_retry"
    retry = _spawn(v22_db, cfg_path, go2)
    _release(go2, [retry])
    out, err = retry.communicate(timeout=60)
    assert retry.returncode == 0 and out.strip() == "IMPORTED", err
    assert _rows(v22_db, "SELECT COUNT(*) FROM guilds")[0][0] == 1
    _healthy(v22_db)


def _write_ops_wrapper(monkeypatch, deny_nth: int | None, deny_commit: bool = False) -> dict:
    """Make `importer.connect` hand back a connection whose Nth write statement is refused.

    A SQLite authorizer sees every statement as it's prepared, so refusing
    the Nth INSERT/UPDATE/DELETE is "the database failed exactly here",
    with no assumptions about which Python function issued it.
    """
    state = {"writes": 0}
    real = importer.connect

    def wrapper(path):
        conn = real(path)

        def authorizer(action, arg1, arg2, dbname, source):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                state["writes"] += 1
                if state["writes"] == deny_nth:
                    return sqlite3.SQLITE_DENY
            if deny_commit and action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(importer, "connect", wrapper)
    return state


def test_a_failure_at_every_single_write_statement_rolls_back_and_a_retry_succeeds(
    v22_db, tmp_path, cfg, monkeypatch
):
    scratch = _copy_db(v22_db, tmp_path / "dry.db")
    counter = _write_ops_wrapper(monkeypatch, deny_nth=None)
    ensure_imported(scratch, cfg, _clock)
    total = counter["writes"]
    assert total >= 8, "the dry run should have seen every section's writes"

    for n in range(1, total + 1):
        monkeypatch.undo()
        victim = _copy_db(v22_db, tmp_path / f"victim{n}.db")
        before = _everything(victim)
        _write_ops_wrapper(monkeypatch, deny_nth=n)
        with pytest.raises(ImportFailedError, match="rolled back"):
            ensure_imported(victim, cfg, _clock)
        assert _everything(victim) == before, f"write #{n} left residue"
        _healthy(victim)

        monkeypatch.undo()
        assert ensure_imported(victim, cfg, _clock) is not None, f"retry after #{n} failed"
        assert _rows(victim, "SELECT COUNT(*) FROM guilds")[0][0] == 1


def test_a_failure_at_the_commit_itself_rolls_back(v22_db, cfg, monkeypatch):
    before = _everything(v22_db)
    _write_ops_wrapper(monkeypatch, deny_nth=None, deny_commit=True)
    with pytest.raises(ImportFailedError):
        ensure_imported(v22_db, cfg, _clock)
    assert _everything(v22_db) == before
    monkeypatch.undo()
    assert ensure_imported(v22_db, cfg, _clock) is not None


def test_an_import_racing_a_v22_digest_save_never_leaves_two_digests_or_an_orphan(
    v22_db, v22, cfg, tmp_path
):
    """v2.2 claims tomorrow's digest while v3 imports: either order must end with one guild row."""
    tomorrow = date(2026, 10, 1)
    outcomes = set()
    for i in range(8):
        db = _copy_db(v22_db, tmp_path / f"race{i}.db")
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def do_import(db=db, barrier=barrier, errors=errors):
            try:
                barrier.wait()
                ensure_imported(db, cfg, _clock)
            except BaseException as exc:
                errors.append(exc)

        def do_v22_save(db=db, barrier=barrier, errors=errors):
            try:
                with closing(connect(db)) as conn:
                    barrier.wait()
                    digest_id = v22.claim_digest(conn, tomorrow, force=False, now=_clock)
                    v22.save_run(conn, digest_id, [], [], "ok", [9], None, Usage(0, 0), now=_clock)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_import), threading.Thread(target=do_v22_save)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

        with closing(connect(db)) as conn:
            orphans_before_adopt = conn.execute(
                "SELECT COUNT(*) FROM digests WHERE guild_id IS NULL"
            ).fetchone()[0]
            outcomes.add(orphans_before_adopt)
            repo.adopt_orphan_digests(conn, GUILD)
            rows = conn.execute(
                "SELECT guild_id FROM digests WHERE run_date = '2026-10-01'"
            ).fetchall()
            assert [r["guild_id"] for r in rows] == [GUILD]
            assert (
                conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id IS NULL").fetchone()[0]
                == 0
            )
        _healthy(db)
    assert outcomes <= {0, 1}


# --- 5. the D5 resync ---


def _lounge_cfg(tmp_path: Path, name: str, welcome=None, quote=False, channel=True) -> AppConfig:
    return _load(tmp_path, _with_lounge(_lounge_block(welcome, quote, channel)), name)


def test_alternating_resyncs_follow_the_config_and_never_touch_quote_state(v22_db, tmp_path, cfg):
    ensure_imported(v22_db, cfg, _clock)
    deck = [tuple(r) for r in _rows(v22_db, "SELECT * FROM lounge_quotes_used ORDER BY rowid")]
    state = [tuple(r) for r in _rows(v22_db, "SELECT * FROM lounge_state")]

    plan = [
        ("a", dict(welcome="First {member}.", quote=True)),
        ("b", dict(welcome=None, quote=True)),
        ("c", dict(welcome="Second {server}.", quote=False)),
        ("d", dict(welcome=None, quote=False)),
        ("e", dict(welcome="First {member}.", quote=True)),
        ("f", dict(welcome="First {member}.", quote=True)),  # repeat: nothing to do
    ]
    for step, (name, kwargs) in enumerate(plan):
        wanted = _lounge_cfg(tmp_path, f"{name}.yaml", **kwargs)
        changed = resync_lounge_from_config(v22_db, wanted)
        assert changed is (name != "f"), name
        assert resync_lounge_from_config(v22_db, wanted) is False  # always idempotent
        row = _lounge_row(v22_db)
        assert row.last_quote_date == "2026-09-30", step
        assert row.welcome_enabled is (kwargs["welcome"] is not None)
        assert row.quote_enabled is kwargs["quote"]
        assert row.channel_id == 1401806745898061826
        assert [
            tuple(r) for r in _rows(v22_db, "SELECT * FROM lounge_quotes_used ORDER BY rowid")
        ] == deck
        assert [tuple(r) for r in _rows(v22_db, "SELECT * FROM lounge_state")] == state
    _healthy(v22_db)


def test_documented_both_off_with_only_the_text_changed_does_not_write(v22_db, tmp_path, cfg):
    """Off-and-off counts as in sync even when the stored welcome text differs from the config's."""
    ensure_imported(v22_db, cfg, _clock)
    off_one = _load(tmp_path, _with_lounge("lounge:\n  channel_id: 1401806745898061826\n"))
    assert resync_lounge_from_config(v22_db, off_one) is True
    stored = _lounge_row(v22_db).welcome_message
    assert stored != ""  # the old text is kept when the config has none
    off_two = _load(
        tmp_path,
        _with_lounge(
            "lounge:\n  channel_id: 1401806745898061826\n  welcome:\n"
            '    enabled: false\n    message: "Edited while off."\n'
        ),
        "off2.yaml",
    )
    assert resync_lounge_from_config(v22_db, off_two) is False
    assert _lounge_row(v22_db).welcome_message == stored


def test_off_keeps_the_channel_and_turning_back_on_uses_the_configs_channel(v22_db, tmp_path, cfg):
    ensure_imported(v22_db, cfg, _clock)
    moved_off = _load(tmp_path, _with_lounge("lounge:\n  channel_id: 999000000000000001\n"))
    assert resync_lounge_from_config(v22_db, moved_off) is True
    assert (
        _lounge_row(v22_db).channel_id == 1401806745898061826
    )  # documented: off keeps the channel
    moved_on = _load(
        tmp_path,
        _with_lounge(
            _lounge_block("Hi {member}.").replace("1401806745898061826", "999000000000000001")
        ),
        "on.yaml",
    )
    assert resync_lounge_from_config(v22_db, moved_on) is True
    assert _lounge_row(v22_db).channel_id == 999000000000000001


def test_resync_after_guild_id_is_deleted_but_lounge_kept_does_nothing(v22_db, tmp_path, cfg):
    """Documented: the D5 re-sync hangs off `guild_id`; without it the row stands alone."""
    ensure_imported(v22_db, cfg, _clock)
    text = _prodlike_text().replace("guild_id: 100000000000000001\n", "")
    try:
        no_guild = _load(tmp_path, text)
    except ConfigError:
        return  # also a fair answer: the loader refuses the file before the re-sync could run
    assert no_guild.legacy is None
    before = _everything(v22_db)
    assert resync_lounge_from_config(v22_db, no_guild) is False
    assert _everything(v22_db) == before


def test_resync_targets_the_imported_guild_even_if_legacy_guild_id_changes(v22_db, cfg, tmp_path):
    ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        repo.create_guild(conn, 4242, now=_clock)
        repo.upsert_lounge(
            conn,
            LoungeSettings(4242, 55, True, "stranger's welcome", False, "07:00", [], None),
        )
    stranger_before = _rows(v22_db, "SELECT * FROM guild_lounge WHERE guild_id = 4242")
    edited = _lounge_cfg(tmp_path, "edit.yaml", welcome="Edited {member}.", quote=True)
    moved = edited.model_copy(
        update={"legacy": edited.legacy.model_copy(update={"guild_id": 4242})}
    )
    assert resync_lounge_from_config(v22_db, moved) is True
    assert _lounge_row(v22_db).welcome_message == "Edited {member}."
    assert _rows(v22_db, "SELECT * FROM guild_lounge WHERE guild_id = 4242") == stranger_before
    assert _rows(v22_db, "SELECT COUNT(*) FROM guilds")[0][0] == 2


def test_resync_creates_a_missing_row_when_the_config_turns_the_lounge_on(v22_db, tmp_path, cfg):
    off = _load(tmp_path, _with_lounge("lounge:\n  channel_id: 1401806745898061826\n"))
    ensure_imported(v22_db, off, _clock)
    assert _lounge_row(v22_db) is None
    on = _lounge_cfg(tmp_path, "on.yaml", welcome="Hi {member}.")
    assert resync_lounge_from_config(v22_db, on) is True
    assert _lounge_row(v22_db).welcome_enabled is True


def test_a_resync_created_row_inherits_v22s_last_quote_date(v22_db, tmp_path):
    off = _load(tmp_path, _with_lounge("lounge:\n  channel_id: 1401806745898061826\n"))
    ensure_imported(v22_db, off, _clock)
    on = _lounge_cfg(tmp_path, "on.yaml", welcome="Hi {member}.", quote=True)
    resync_lounge_from_config(v22_db, on)
    assert _lounge_row(v22_db).last_quote_date == "2026-09-30"


@pytest.mark.parametrize(
    "block",
    [
        "lounge:\n  welcome:\n    enabled: true\n    message: hi\n",
        _lounge_block("Hello {nobody}."),
        _lounge_block("Hi", quote=True).replace('time: "08:30"', 'time: "25:99"'),
        (
            "lounge:\n  channel_id: 1401806745898061826\n"
            "  daily_quote:\n    enabled: true\n    sources: []\n"
        ),
        _lounge_block(welcome="   "),
    ],
    ids=["no-channel", "bad-placeholder", "bad-time", "empty-sources", "blank-welcome"],
)
def test_invalid_lounge_settings_never_reach_the_resync_because_the_loader_refuses(tmp_path, block):
    with pytest.raises(ConfigError):
        _load(tmp_path, _with_lounge(block))


def test_documented_a_resync_handed_an_invalid_row_raises_and_changes_nothing(v22_db, cfg):
    """Can't happen after load_config; if it somehow did, the CHECK raises a raw sqlite error."""
    ensure_imported(v22_db, cfg, _clock)
    quote = cfg.legacy.lounge.daily_quote.model_copy(update={"time": "25:99"})
    bad = cfg.model_copy(
        update={
            "legacy": cfg.legacy.model_copy(
                update={"lounge": cfg.legacy.lounge.model_copy(update={"daily_quote": quote})}
            )
        }
    )
    before = _everything(v22_db)
    with pytest.raises(sqlite3.IntegrityError):
        resync_lounge_from_config(v22_db, bad)
    assert _everything(v22_db) == before


# --- logging: the welcome text must not appear, at any level, anywhere ---


def _sentinel_cfg(tmp_path: Path) -> AppConfig:
    return _load(
        tmp_path,
        _with_lounge(_lounge_block(f"{SENTINEL} hello {{member}} to {{server}}", quote=True)),
        "sentinel.yaml",
    )


def _everything_logged(caplog) -> str:
    chunks = []
    for r in caplog.records:
        chunks += [r.getMessage(), str(r.msg), repr(r.args), r.name]
        if r.exc_info:
            chunks.append(repr(r.exc_info[1]))
    return "\n".join(chunks)


def test_the_welcome_text_is_in_no_log_record_at_any_level_during_import_or_resync(
    v22_db, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)  # the root logger, every level, every library
    cfg = _sentinel_cfg(tmp_path)
    ensure_imported(v22_db, cfg, _clock)
    edited = _load(
        tmp_path,
        _with_lounge(_lounge_block(f"{SENTINEL} changed", quote=True)),
        "sentinel2.yaml",
    )
    assert resync_lounge_from_config(v22_db, edited) is True
    assert caplog.records, "the point is to have looked at real records"
    assert SENTINEL not in _everything_logged(caplog)


def test_a_failed_import_does_not_put_the_welcome_text_in_its_error_or_the_log(
    v22_db, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    cfg = _sentinel_cfg(tmp_path)
    bad = cfg.model_copy(
        update={"legacy": cfg.legacy.model_copy(update={"shift_channel_id": None})}
    )
    with pytest.raises(ImportFailedError) as exc_info:
        ensure_imported(v22_db, bad, _clock)
    assert SENTINEL not in str(exc_info.value)
    assert SENTINEL not in _everything_logged(caplog)


def test_log_lines_arrive_only_after_the_commit(v22_db, cfg, caplog, monkeypatch):
    seen: list[int] = []

    def peek(_report):
        seen.append(_rows(v22_db, "SELECT COUNT(*) FROM guilds")[0][0])

    monkeypatch.setattr(importer, "_log_report", peek)
    ensure_imported(v22_db, cfg, _clock)
    assert seen == [1]  # the guild was visible to a second connection when logging began


# --- 6. the report ---


def test_owner_notice_is_short_plain_and_unpingable_for_every_shape(v22_db, cfg):
    report = ensure_imported(v22_db, cfg, _clock)
    shapes = []
    for n_games in (0, 1, 10):
        for shift in (None, {"x": 1}):
            for lounge in (None, {"x": 1}):
                games = [(f"@everyone<@&{i}>https://x.example", i + 1) for i in range(n_games)]
                shapes.append(
                    importer.ImportReport(
                        **{**report.__dict__, "games": games, "shift": shift, "lounge": lounge}
                    )
                )
    for shaped in shapes:
        notice = shaped.owner_notice()
        assert discord_len(notice) <= 2000
        assert not any(bad in notice for bad in ("@", "<", ">", "http", "`", "[", "]", "\n"))
        assert notice.startswith("Imported the v2 setup for this server (")


def test_owner_notice_never_contains_welcome_text_ids_or_secrets(v22_db, tmp_path):
    cfg = _sentinel_cfg(tmp_path)
    report = ensure_imported(v22_db, cfg, _clock)
    notice = report.owner_notice()
    assert SENTINEL not in notice and SENTINEL not in repr(report)
    for needle in (str(GUILD), "1401806745898061826", "token", "key"):
        assert needle not in notice


def test_the_saved_import_json_is_well_formed_complete_and_welcome_free(v22_db, tmp_path):
    cfg = _sentinel_cfg(tmp_path)
    report = ensure_imported(v22_db, cfg, _clock)
    with closing(connect(v22_db)) as conn:
        raw = repo.app_state_get(conn, "import")
    saved = json.loads(raw)
    assert set(saved) == set(importer.ImportReport.__dataclass_fields__)
    assert saved == json.loads(json.dumps(importer.asdict(report)))
    assert saved["imported_at"] == NOW.isoformat()
    assert [tuple(g) for g in saved["games"]] == report.games
    assert isinstance(saved["guild_id"], int) and isinstance(saved["digests_adopted"], int)
    assert SENTINEL not in raw and "message" not in saved["lounge"]
    assert SENTINEL not in json.dumps([tuple(r) for r in _rows(v22_db, "SELECT * FROM app_state")])


def test_the_saved_json_for_a_minimal_import_has_nulls_not_holes(v22_db, tmp_path):
    text = (
        _prodlike_text()
        .split("\nlounge:")[0]
        .replace(
            "  enabled: true\n  channel_id: 1553597251933438122",
            "  enabled: false\n  channel_id: 1553597251933438122",
        )
    )
    text = text.replace("admin_channel_id: 100000000000000002\n", "")
    ensure_imported(v22_db, _load(tmp_path, text), _clock)
    saved = json.loads(_rows(v22_db, "SELECT value FROM app_state WHERE key = 'import'")[0][0])
    assert saved["shift"] is None and saved["lounge"] is None and saved["admin_channel_id"] is None


def test_the_environment_is_not_leaking_into_the_report(v22_db, cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    report = ensure_imported(v22_db, cfg, _clock)
    blob = repr(report) + report.owner_notice() + json.dumps(_everything(v22_db)["app_state"])
    assert "super-secret" not in blob and "sk-ant" not in blob
    assert os.environ["DISCORD_TOKEN"] not in blob
