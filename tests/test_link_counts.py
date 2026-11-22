"""`scripts/link_counts.py`: the read-only report of how many links the bot posts.

The database here is a small one built with the real migrations. Each test pins one rule the
report relies on: the digest rebuild counts what the renderer would show (an invite and an
over-long URL dropped from a headline list, a story's third link the last one shown), SHiFT is
counted from its delivery records, the file is never touched, and the projection is plain
multiplication. Nothing here prints a URL, and a test checks that too.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from newsbot.store.db import connect, migrate

CODES = [
    f"{letter * 5}-{letter * 5}-{letter * 5}-{letter * 5}-{letter * 4}{n}"
    for n, letter in enumerate("ABCDEF")
]
OK_URLS = [f"https://example.com/bl4/{n}" for n in range(3)]
INVITE = "https://discord.gg/abc123"
TOO_LONG = "https://example.com/" + "x" * 1100


def _load_script():
    path = Path(__file__).parent.parent / "scripts" / "link_counts.py"
    spec = importlib.util.spec_from_file_location("link_counts", path)
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its own module up by name, so the script has to be findable.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _item(conn, item_id, url, collected_at, topics, source="Feed", trust="press"):
    conn.execute(
        "INSERT INTO items (id, url, title, excerpt, source_name, trust, published_at, "
        "collected_at) VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
        (item_id, url, f"Title {item_id}", source, trust, collected_at),
    )
    for topic in topics:
        conn.execute("INSERT INTO item_topics (item_id, topic_key) VALUES (?, ?)", (item_id, topic))


def _guild(conn, guild_id, tier, games, *, shift=False):
    conn.execute(
        "INSERT INTO guilds (guild_id, tier, set_up, joined_at, updated_at) "
        "VALUES (?, ?, 1, '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
        (guild_id, tier),
    )
    for game in games:
        conn.execute(
            "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, 5)",
            (guild_id, game),
        )
    if shift:
        conn.execute(
            "INSERT INTO guild_shift (guild_id, enabled, channel_id) VALUES (?, 1, 7)",
            (guild_id,),
        )


def _digest(conn, guild_id, posted, upto):
    conn.execute(
        "INSERT INTO digests (guild_id, run_date, status, posted_by_game, window_start, "
        "window_end, items_after, items_upto, created_at, updated_at) "
        "VALUES (?, '2026-10-20', 'ok', ?, '2026-10-19T16:00:00+00:00', "
        "'2026-10-20T16:00:00+00:00', NULL, ?, '2026-10-20T16:00:00+00:00', "
        "'2026-10-20T16:00:00+00:00')",
        (guild_id, json.dumps(dict.fromkeys(posted, 1)), upto),
    )


def build_world(path: Path) -> Path:
    """Two free servers, one comped, and a little of everything that gets posted."""
    with closing(connect(path)) as conn:
        migrate(conn)
        with conn:
            # Two September items, so the monthly table has two rows.
            _item(conn, 1, "https://example.com/old/1", "2026-09-30T10:00:00+00:00", ["bl4"])
            _item(conn, 2, "https://example.com/old/2", "2026-09-30T11:00:00+00:00", ["bl4"])
            # The free digest's inputs: three usable links, an invite and an over-long URL.
            for n, url in enumerate([*OK_URLS, INVITE, TOO_LONG], start=10):
                _item(conn, n, url, "2026-10-20T10:00:00+00:00", ["bl4"])
            # The comped story's four links.
            for n in range(20, 24):
                _item(conn, n, f"https://example.com/pal/{n}", "2026-10-20T11:00:00+00:00", ["pal"])
            # Three industry items for the free server's fallback list.
            for n in range(30, 33):
                _item(
                    conn,
                    n,
                    f"https://example.com/ind/{n}",
                    "2026-10-20T12:00:00+00:00",
                    ["industry"],
                )

            _guild(conn, 1, "free", ["bl4", "industry"], shift=True)
            _guild(conn, 2, "free", ["bl4"])
            _guild(conn, 3, "comped", ["pal"])
            _digest(conn, 1, ["bl4", "industry"], 40)
            _digest(conn, 3, ["pal"], 40)

            conn.execute(
                "INSERT INTO game_summaries (id, game_key, run_date, status, window_start, "
                "window_end, items_after, items_upto, created_at) VALUES (1, 'pal', "
                "'2026-10-20', 'ok', '2026-10-19T16:00:00+00:00', '2026-10-20T15:30:00+00:00', "
                "NULL, 40, '2026-10-20T15:30:00+00:00')"
            )
            conn.execute(
                "INSERT INTO stories (id, topic_key, headline, summary, label, summary_id, "
                "created_at) VALUES (1, 'pal', 'A story', 'Things happened.', 'reported', 1, "
                "'2026-10-20T15:30:00+00:00')"
            )
            for n in range(20, 24):
                conn.execute("INSERT INTO story_items (story_id, item_id) VALUES (1, ?)", (n,))

            # SHiFT: two alerts (one sourced from an invite), a three-code roundup sharing
            # one source post, a failed code, and one follow-up.
            sources = [
                (CODES[0], "https://example.com/shift/a"),
                (CODES[1], INVITE),
                (CODES[2], "https://example.com/shift/roundup"),
                (CODES[3], "https://example.com/shift/roundup"),
                (CODES[4], "https://example.com/shift/roundup"),
                (CODES[5], "https://example.com/shift/failed"),
            ]
            for code, url in sources:
                conn.execute(
                    "INSERT INTO alerted_codes "
                    "(code, first_seen_at, source_name, item_url, status) "
                    "VALUES (?, '2026-10-20T08:00:00+00:00', 'Src', ?, 'posted')",
                    (code, url),
                )
            posts = [
                (CODES[0], "posted", 0),
                (CODES[1], "posted", 0),
                (CODES[2], "posted", 1),
                (CODES[3], "posted", 1),
                (CODES[4], "posted", 1),
                (CODES[5], "failed", 0),
            ]
            for code, status, roundup in posts:
                conn.execute(
                    "INSERT INTO guild_code_posts (guild_id, code, status, from_roundup, "
                    "claimed_at) VALUES (1, ?, ?, ?, '2026-10-20T08:05:00+00:00')",
                    (code, status, roundup),
                )
            conn.execute(
                "INSERT INTO guild_code_followups (guild_id, code, status, queued_at, "
                "claimed_at) VALUES (1, ?, 'posted', '2026-10-20T09:00:00+00:00', "
                "'2026-10-20T09:01:00+00:00')",
                (CODES[0],),
            )
    return path


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_world(tmp_path_factory.mktemp("links") / "newsbot.db")


@pytest.fixture(scope="module")
def tally(script, world):
    conn = script.open_read_only(world)
    try:
        counted = script.Tally({"2026-10-20"})
        script._count_digests(conn, counted)
        script._count_shift(conn, counted)
    finally:
        conn.close()
    return counted


def test_unique_links_by_month(script, world):
    conn = script.open_read_only(world)
    try:
        report = script.build_report(conn)
    finally:
        conn.close()
    rows = dict(re.findall(r"^\s+(2026-\d\d)\s+(\d+)\s+\d+$", report, flags=re.MULTILINE))
    assert rows == {"2026-09": "2", "2026-10": "12"}


def test_free_headlines_drop_the_invite_and_the_overlong_url(tally):
    # Five bl4 items were stored; the invite and the 1,100-character URL never reach the list.
    assert tally.by_month[("2026-10", "free", "games")] == 3


def test_comped_story_shows_three_of_four_links(tally):
    assert tally.by_month[("2026-10", "comped", "games")] == 3


def test_free_industry_fallback_lists_each_headline(tally):
    assert tally.by_month[("2026-10", "free", "industry")] == 3


def test_shift_counts_follow_the_message_shapes(tally):
    # Two alerts, one of them sourced from an invite (no link shown): one link.
    assert tally.by_month[("2026-10", "free", "shift_alert")] == 1
    # Three roundup codes from one source post: one header link.
    assert tally.by_month[("2026-10", "free", "shift_roundup")] == 1
    # A follow-up has a ping, a prompt line and fenced codes.
    assert tally.by_month[("2026-10", "free", "shift_followup")] == 0
    # The failed code posted nothing, so no link of its own.
    assert tally.posts_recorded["2026-10"] == 3
    assert tally.posts_rebuilt["2026-10"] == 3


def test_unique_posted_links_are_remembered_by_first_post(tally):
    assert "https://example.com/bl4/0" in tally.first_posted
    assert INVITE not in tally.first_posted
    assert tally.first_posted["https://example.com/bl4/0"] == "2026-10-20"


def test_report_prints_numbers_only(script, world):
    conn = script.open_read_only(world)
    try:
        report = script.build_report(conn)
    finally:
        conn.close()
    assert "http" not in report
    assert "example.com" not in report
    assert "PROJECTION" in report


def test_per_server_rates_scale_a_window_to_thirty_days(script, world, tally):
    conn = script.open_read_only(world)
    try:
        _, rates = script._shape_section(conn, tally, 1, [date(2026, 10, 20)])
    finally:
        conn.close()
    # One free server-day: 3 headlines, 3 industry links, 1 alert link and 1 roundup link.
    assert rates["free"]["games (headlines or stories)"] == 3 * 30
    assert rates["free"]["industry"] == 3 * 30
    assert rates["free"]["SHiFT"] == 2 * 30
    assert rates["comped"]["games (headlines or stories)"] == 3 * 30


def test_projection_is_plain_multiplication(script):
    counted = script.Tally(set())
    counted.server_days["free"] = 1
    counted.server_days["comped"] = 1
    rates = {
        "free": {"games": 60.0, "industry": 30.0, "SHiFT": 10.0},
        "comped": {"games": 400.0, "industry": 80.0, "SHiFT": 20.0},
    }
    lines = script._projection_section(counted, rates, 2, (4500.0, 3000.0))
    row = re.compile(r"\s+(\d+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)$")
    table = {
        int(m[1]): [int(v.replace(",", "")) for v in m.groups()[1:]]
        for m in (row.match(line) for line in lines)
        if m
    }
    assert sorted(table) == [10, 25, 50, 75, 100, 200]
    # 100 links a month per free server. Two comped servers at 500 are kept in the third column.
    assert table[10] == [1000, 2 * 500 + 8 * 100, 4500, 3000]
    assert table[200] == [20000, 2 * 500 + 198 * 100, 4500, 3000]


def test_projection_without_free_servers_says_so(script):
    counted = script.Tally(set())
    lines = script._projection_section(counted, {}, 0, (100.0, 50.0))
    assert any("No free servers" in line for line in lines)


# --- Read-only, provably ---


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(directory.iterdir())}


def test_the_file_is_never_touched(script, tmp_path, capsys):
    path = build_world(tmp_path / "backup.db")
    before = _snapshot(tmp_path)
    assert script.main([str(path)]) == 0
    assert "LINKS POSTED" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before  # same bytes, same mtime, no -wal or -shm beside it


def test_a_read_only_directory_is_fine(script, tmp_path, capsys):
    # A `.backup` copy keeps the WAL flag, and a plain read-only open of one wants to create
    # -shm in the directory. The Droplet's mount is read-only, so it has to work there.
    live = build_world(tmp_path / "live.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    with (
        closing(sqlite3.connect(live)) as src,
        closing(sqlite3.connect(backup_dir / "b.db")) as dst,
    ):
        src.backup(dst)
    before = _snapshot(backup_dir)
    backup_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert script.main([str(backup_dir / "b.db")]) == 0
    finally:
        backup_dir.chmod(stat.S_IRWXU)
    assert "PROJECTION" in capsys.readouterr().out
    assert _snapshot(backup_dir) == before


def test_a_missing_path_is_refused_and_not_created(script, tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert script.main([str(missing)]) == 2
    assert not missing.exists()
    assert os.listdir(tmp_path) == []
    assert "no such database file" in capsys.readouterr().err


def test_a_directory_is_refused(script, tmp_path, capsys):
    assert script.main([str(tmp_path)]) == 2
    assert "no such database file" in capsys.readouterr().err


def test_a_live_looking_database_is_refused(script, tmp_path, capsys):
    path = build_world(tmp_path / "live.db")
    Path(f"{path}-wal").write_bytes(b"not empty")
    assert script.main([str(path)]) == 2
    assert "write-ahead log" in capsys.readouterr().err


def test_a_file_that_is_not_a_database_is_an_error_not_a_crash(script, tmp_path, capsys):
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is not sqlite" * 100)
    assert script.main([str(path)]) == 1
    assert "could not read" in capsys.readouterr().err


def test_an_old_database_is_refused(script, tmp_path, capsys):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 3")
    assert script.main([str(path)]) == 2
    assert "migration 3" in capsys.readouterr().err


def test_an_empty_current_database_reports_zeros(script, tmp_path, capsys):
    path = tmp_path / "empty.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    assert script.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "No items stored." in out
    assert "No free servers" in out
