"""Adversarial round for plan task 3: the 10-game limit leaves the industry entry out.

Migration 010 swaps the `guild_games_limit` trigger on production's real database, and
four Python paths (repo, importer, wizard, `/newsbot games`) now count "games" a second
way. These tests go after the seams: every write path with an eleventh counted game, rows
that are legal at version 9 but odd, the order things are inserted in, key spellings the
SQL and Python could disagree about, a process killed at every statement of the migration,
a draft-009 database that must be refused untouched, and the wizard's select arithmetic.

A test marked `xfail(strict=True)` is a real bug; its reason says where.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from v3_fakes import (
    GUILD_A,
    FakeInteraction,
    channel,
    command,
    fake_bot,
    load_v3_industry_config,
    make_guild,
    thin_channel,
)

from newsbot.bot import commands as commands_module
from newsbot.bot import setup_views
from newsbot.bot.commands import make_guild_admin_group
from newsbot.bot.permissions import GuildCheck
from newsbot.bot.setup_views import SetupView
from newsbot.config import load_config
from newsbot.guilds.importer import ImportFailedError, ensure_imported
from newsbot.industry import INDUSTRY_KEY, counts_toward_limit
from newsbot.store import db, repo
from newsbot.store.db import StoreError, connect, migrate

ERROR = "at most 10 games per guild"
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
INSERT = "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)"


def games(n, *, start=0):
    return [(f"game{i}", 100 + i) for i in range(start, start + n)]


def rows(c, guild_id=None):
    sql = "SELECT * FROM guild_games"
    params: tuple = ()
    if guild_id is not None:
        sql += " WHERE guild_id = ?"
        params = (guild_id,)
    return [tuple(r) for r in c.execute(sql + " ORDER BY rowid", params)]


def trigger_sql(c):
    return [
        r[0]
        for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'guild_games_limit'"
        )
    ]


def schema(c):
    return [tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")]


@pytest.fixture
def conn(v3_db):
    with closing(connect(v3_db)) as c:
        repo.create_guild(c, 7)
        repo.create_guild(c, 8)
        yield c


def make_v9(path, tmp_path, monkeypatch, migrations_up_to, name="m9", *, populate=None):
    """A version-9 file (the schema prod has today), optionally filled by `populate(conn)`."""
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / name, 9))
        with closing(connect(path)) as c:
            assert migrate(c) == 9
            if populate is not None:
                with c:
                    populate(c)
    return path


def add_guild(c, guild_id, keys):
    c.execute(
        "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (?, 'n', 'n')", (guild_id,)
    )
    for i, key in enumerate(keys):
        c.execute(INSERT, (guild_id, key, 1000 + i))


# --- every write path, with the eleventh counted game ---


def test_every_write_path_refuses_the_eleventh_counted_game_with_industry_present(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    before = rows(conn)

    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game10", 1)
    with pytest.raises(repo.GameLimitError):
        repo.set_guild_games(conn, 7, [*games(11), (INDUSTRY_KEY, 555)])
    with pytest.raises(repo.GameLimitError):
        repo.apply_guild_setup(
            conn, 7, digest_time="09:00", timezone="UTC", games=[*games(11), (INDUSTRY_KEY, 555)]
        )
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute(INSERT, (7, "game10", 1))
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("INSERT OR REPLACE INTO guild_games VALUES (7, 'game10', 1)")
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("INSERT OR IGNORE INTO guild_games VALUES (7, 'game10', 1)")
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("INSERT INTO guild_games SELECT 7, 'game10', 1")
    assert rows(conn) == before


def test_one_multi_row_insert_of_ten_games_plus_industry_goes_through_in_any_order(conn):
    values = ", ".join(f"(7, 'game{i}', {100 + i})" for i in range(10))
    conn.execute(f"INSERT INTO guild_games VALUES (7, 'industry', 1), {values}")  # noqa: S608
    assert len(rows(conn, 7)) == 11
    conn.execute(
        f"INSERT INTO guild_games VALUES {values.replace('(7,', '(8,')}, (8, 'industry', 1)"  # noqa: S608
    )
    assert len(rows(conn, 8)) == 11


def test_one_multi_row_insert_of_eleven_counted_games_leaves_nothing_behind(conn):
    values = ", ".join(f"(7, 'game{i}', {100 + i})" for i in range(11))
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute(f"INSERT INTO guild_games VALUES {values}, (7, 'industry', 1)")  # noqa: S608
    assert rows(conn, 7) == []


def test_insert_or_replace_of_industry_when_it_is_already_there_keeps_one_row(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    conn.execute("INSERT OR REPLACE INTO guild_games VALUES (7, 'industry', 999)")
    conn.execute("INSERT OR IGNORE INTO guild_games VALUES (7, 'industry', 1000)")
    got = [r for r in rows(conn, 7) if r[1] == "industry"]
    assert got == [(7, "industry", 999)]
    assert len(rows(conn, 7)) == 11


def test_a_plain_second_insert_of_industry_is_a_primary_key_error_not_the_limit(conn):
    repo.follow_game(conn, 7, "industry", 555)
    with pytest.raises(sqlite3.IntegrityError) as err:
        conn.execute(INSERT, (7, "industry", 556))
    assert ERROR not in str(err.value)
    assert repo.follow_game(conn, 7, "industry", 556) is False


def test_the_importer_takes_ten_games_plus_industry_wherever_industry_sits(tmp_path, v3_db):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    base = [(f"game{i}", 1000 + i) for i in range(10)]
    for position in (0, 5, 10):
        path = tmp_path / f"imp{position}.db"
        with closing(connect(path)) as c:
            migrate(c)
        mixed = [*base[:position], (INDUSTRY_KEY, 2000), *base[position:]]
        legacy = cfg.legacy.model_copy(update={"games": mixed})
        ensure_imported(path, cfg.model_copy(update={"legacy": legacy}), lambda: NOW)
        with closing(connect(path)) as c:
            assert [r[1] for r in rows(c)] == [k for k, _ in mixed]


def test_the_importer_refuses_eleven_counted_games_wherever_industry_sits(tmp_path):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    base = [(f"game{i}", 1000 + i) for i in range(11)]
    for position in (0, 6, 11):
        path = tmp_path / f"imp{position}.db"
        with closing(connect(path)) as c:
            migrate(c)
        mixed = [*base[:position], (INDUSTRY_KEY, 2000), *base[position:]]
        legacy = cfg.legacy.model_copy(update={"games": mixed})
        with pytest.raises(ImportFailedError, match="at most 10"):
            ensure_imported(path, cfg.model_copy(update={"legacy": legacy}), lambda: NOW)
        with closing(connect(path)) as c:
            assert rows(c) == []
            assert c.execute("SELECT COUNT(*) FROM guilds").fetchone()[0] == 0


# --- UPDATE: the trigger is BEFORE INSERT only ---


def test_updating_a_game_into_industry_is_harmless(conn):
    repo.set_guild_games(conn, 7, games(10))
    conn.execute("UPDATE guild_games SET game_key = 'industry' WHERE game_key = 'game3'")
    assert sum(counts_toward_limit(r[1]) for r in rows(conn, 7)) == 9
    assert repo.follow_game(conn, 7, "game99", 1) is True


def test_updating_industry_into_a_game_cannot_make_an_eleventh_counted_game(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("UPDATE guild_games SET game_key = 'game10' WHERE game_key = 'industry'")


# --- order of operations at the limit ---


def test_ten_plus_industry_then_drop_a_game_and_add_another(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    assert repo.unfollow_game(conn, 7, "game3") is True
    assert repo.follow_game(conn, 7, "game10", 1) is True
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game11", 2)
    assert len(repo.list_guild_games(conn, 7)) == 11


def test_dropping_industry_does_not_make_room_for_an_eleventh_counted_game(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    repo.unfollow_game(conn, 7, INDUSTRY_KEY)
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game10", 1)


def test_apply_guild_setup_can_swap_every_game_on_a_full_server_and_keeps_industry(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    repo.apply_guild_setup(
        conn,
        7,
        digest_time="09:00",
        timezone="UTC",
        games=[*games(10, start=50), (INDUSTRY_KEY, 556)],
    )
    got = {g.game_key: g.channel_id for g in repo.list_guild_games(conn, 7)}
    assert len(got) == 11 and got[INDUSTRY_KEY] == 556 and "game50" in got and "game0" not in got


def test_apply_guild_setup_can_drop_industry_from_a_full_server(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    repo.apply_guild_setup(conn, 7, digest_time="09:00", timezone="UTC", games=games(10))
    assert [g.game_key for g in repo.list_guild_games(conn, 7)] == [k for k, _ in games(10)]


def test_apply_guild_setup_with_industry_alone_and_with_nothing(conn):
    repo.apply_guild_setup(conn, 7, digest_time="09:00", timezone="UTC", games=[(INDUSTRY_KEY, 5)])
    assert [g.game_key for g in repo.list_guild_games(conn, 7)] == [INDUSTRY_KEY]
    repo.apply_guild_setup(conn, 7, digest_time="09:00", timezone="UTC", games=[])
    assert repo.list_guild_games(conn, 7) == []


def test_a_refused_apply_guild_setup_rolls_back_the_deletes_and_the_row_changes(conn):
    repo.set_guild_games(conn, 7, [*games(10), (INDUSTRY_KEY, 555)])
    before = rows(conn)
    with pytest.raises(repo.GameLimitError):
        repo.apply_guild_setup(
            conn, 7, digest_time="03:00", timezone="UTC", games=[*games(11, start=40)]
        )
    assert rows(conn) == before
    assert repo.get_guild(conn, 7).digest_time != "03:00"


def test_two_servers_at_the_limit_do_not_count_each_others_industry(conn):
    repo.set_guild_games(conn, 7, games(10))
    repo.set_guild_games(conn, 8, [(INDUSTRY_KEY, 5), *games(10, start=20)])
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game10", 1)
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 8, "game30", 1)
    assert repo.follow_game(conn, 7, INDUSTRY_KEY, 9) is True


# --- key spellings: SQL and Python must agree ---


@pytest.mark.parametrize(
    "spelling", ["Industry", "INDUSTRY", " industry", "industry ", "industry\n"]
)
def test_other_spellings_of_industry_are_ordinary_games_in_sql_and_in_python(conn, spelling):
    assert counts_toward_limit(spelling) is True
    repo.set_guild_games(conn, 7, games(10))
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute(INSERT, (7, spelling, 1))
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, spelling, 1)
    with pytest.raises(repo.GameLimitError):
        repo.set_guild_games(conn, 7, [*games(10), (spelling, 1)])
    with pytest.raises(repo.GameLimitError):
        repo.apply_guild_setup(
            conn, 7, digest_time="09:00", timezone="UTC", games=[*games(10), (spelling, 1)]
        )


def test_a_cased_industry_row_counts_toward_the_ten_in_sql_and_python_alike(conn):
    repo.set_guild_games(conn, 7, [*games(9), ("Industry", 5)])
    # Nine games plus "Industry" is ten counted rows, so the next game is the eleventh.
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute(INSERT, (7, "game9", 1))
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game9", 1)
    assert repo.follow_game(conn, 7, INDUSTRY_KEY, 1) is True


def test_a_null_game_key_is_still_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT, (7, None, 1))


# --- migrating a version-9 database with odd but valid data ---


def test_v9_database_with_every_odd_shape_migrates_and_keeps_every_row(
    tmp_path, monkeypatch, migrations_up_to
):
    def populate(c):
        add_guild(c, 1, [])  # no games
        add_guild(c, 2, [f"g{i}" for i in range(10)])  # exactly full
        add_guild(c, 3, [f"g{i}" for i in range(9)] + ["industry"])  # legal at v9: ten rows
        add_guild(c, 4, ["industry"])  # only the entry
        add_guild(c, 5, ["Industry", "INDUSTRY", " industry"])  # lookalikes
        for gid in range(100, 140):  # many servers
            add_guild(c, gid, [f"g{i}" for i in range(gid % 11)])

    path = make_v9(tmp_path / "odd.db", tmp_path, monkeypatch, migrations_up_to, populate=populate)
    with closing(connect(path)) as c:
        before = rows(c)
        guilds_before = [tuple(r) for r in c.execute("SELECT * FROM guilds ORDER BY guild_id")]
        old_trigger = trigger_sql(c)
    assert len(before) > 100

    with closing(connect(path)) as c:
        assert migrate(c) == 10
        assert rows(c) == before
        assert [tuple(r) for r in c.execute("SELECT * FROM guilds ORDER BY guild_id")] == (
            guilds_before
        )
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert len(trigger_sql(c)) == 1 and trigger_sql(c) != old_trigger

        # Server 3 had nine games and the entry: ten rows at v9, room for one more game now.
        assert repo.follow_game(c, 3, "g9", 1) is True
        with pytest.raises(repo.GameLimitError):
            repo.follow_game(c, 3, "g10", 1)
        # Server 2 was full of games; the entry still fits.
        assert repo.follow_game(c, 2, INDUSTRY_KEY, 1) is True
        # Server 4 had only the entry; ten games fit beside it, an eleventh doesn't.
        for key, ch in games(10):
            repo.follow_game(c, 4, key, ch)
        with pytest.raises(repo.GameLimitError):
            repo.follow_game(c, 4, "game10", 1)
        # Server 5's lookalikes are three counted games.
        for key, ch in games(7):
            repo.follow_game(c, 5, key, ch)
        with pytest.raises(repo.GameLimitError):
            repo.follow_game(c, 5, "game7", 1)


def test_an_empty_v9_database_migrates_and_a_second_run_changes_nothing(
    tmp_path, monkeypatch, migrations_up_to
):
    path = make_v9(tmp_path / "empty.db", tmp_path, monkeypatch, migrations_up_to)
    with closing(connect(path)) as c:
        assert migrate(c) == 10
        first = schema(c)
    for _ in range(2):
        with closing(connect(path)) as c:
            assert migrate(c) == 10
            assert schema(c) == first
            assert c.execute("PRAGMA user_version").fetchone()[0] == 10


def test_a_v8_database_goes_through_009_and_010_in_one_start(
    tmp_path, monkeypatch, migrations_up_to
):
    path = tmp_path / "v8.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "m8", 8))
        with closing(connect(path)) as c:
            assert migrate(c) == 8
            with c:
                add_guild(c, 1, [f"g{i}" for i in range(10)])
    with closing(connect(path)) as c:
        assert migrate(c) == 10
        assert len(rows(c)) == 10
        assert repo.follow_game(c, 1, INDUSTRY_KEY, 1) is True


def test_a_fresh_database_has_one_insert_and_one_update_limit_trigger_with_the_old_message(
    v3_db,
):
    with closing(connect(v3_db)) as c:
        assert len(trigger_sql(c)) == 1
        assert f"'{ERROR}'" in trigger_sql(c)[0]
        names = [
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'guild_games'")
        ]
        assert sorted(n for n in names if n.startswith("guild_games_")) == [
            "guild_games_limit",
            "guild_games_limit_update",
        ]
        update_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'guild_games_limit_update'"
        ).fetchone()[0]
        assert f"'{ERROR}'" in update_sql


def test_the_migrations_sql_literal_is_the_python_industry_key():
    # Nothing else ties the trigger's literal to INDUSTRY_KEY: rename the key and every
    # other test would still pass while the trigger and the Python counting disagreed.
    sql = (Path(db.__file__).parent / "migrations" / "010_industry_limit.sql").read_text()
    assert f"'{INDUSTRY_KEY}'" in sql


_KILL_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from newsbot.store import db
    from newsbot.store.db import connect, migrate
    db._MIGRATIONS_DIR = Path(sys.argv[3])
    kill_at = int(sys.argv[2])
    seen = [0]
    conn = connect(sys.argv[1])
    def trace(sql):
        seen[0] += 1
        if seen[0] == kill_at:
            os._exit(7)
    conn.set_trace_callback(trace)
    migrate(conn)
    os._exit(0)
    """
)


def test_a_process_killed_at_any_statement_of_010_leaves_v9_intact_or_a_finished_v10(
    tmp_path, monkeypatch, migrations_up_to
):
    def populate(c):
        add_guild(c, 1, [f"g{i}" for i in range(10)])
        add_guild(c, 2, [f"g{i}" for i in range(9)] + ["industry"])

    src = make_v9(tmp_path / "v9.db", tmp_path, monkeypatch, migrations_up_to, populate=populate)
    full_dir = migrations_up_to(tmp_path / "m10", 10)
    probe = tmp_path / "probe.db"
    shutil.copy(src, probe)
    count = [0]
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", full_dir)
    with closing(connect(probe)) as c:
        c.set_trace_callback(lambda sql: count.__setitem__(0, count[0] + 1))
        migrate(c)
    total = count[0]
    assert total >= 6

    with closing(connect(src)) as c:
        before_rows = rows(c)
        old_trigger = trigger_sql(c)
        old_schema = schema(c)

    saw_v9 = saw_v10 = False
    for point in range(1, total + 1):
        work = tmp_path / f"kill{point}.db"
        shutil.copy(src, work)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _KILL_SCRIPT, str(work), str(point), str(full_dir)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 7, (point, result.stderr)
        with closing(connect(work)) as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            version = c.execute("PRAGMA user_version").fetchone()[0]
            assert rows(c) == before_rows, f"kill at {point} changed rows"
            if version == 9:
                saw_v9 = True
                assert trigger_sql(c) == old_trigger, f"kill at {point}: trigger changed"
                assert schema(c) == old_schema
            else:
                saw_v10 = True
                assert version == 10 and len(trigger_sql(c)) == 1
            assert migrate(c) == 10
            assert len(trigger_sql(c)) == 1
            assert repo.follow_game(c, 1, INDUSTRY_KEY, 5) is True
            with pytest.raises(repo.GameLimitError):
                repo.follow_game(c, 1, "g10", 5)
    assert saw_v9 and saw_v10


# --- draft-009 databases: refused, and nothing applied ---


@pytest.fixture
def draft_path(tmp_path, monkeypatch, migrations_up_to):
    """A version-9 file from an earlier draft of 009: it lacks an index the final 009 has."""
    path = make_v9(
        tmp_path / "draft.db", tmp_path, monkeypatch, migrations_up_to, populate=lambda c: None
    )
    with closing(connect(path)) as c:
        with c:
            add_guild(c, 1, [f"g{i}" for i in range(10)])
        c.execute("DROP INDEX idx_guild_code_posts_code")
        c.commit()
    return path


def test_a_draft_009_database_is_refused_and_010_is_not_applied(draft_path):
    with closing(connect(draft_path)) as c:
        before_schema, before_rows = schema(c), rows(c)
    for _ in range(2):  # and again: refusing must not wear anything down
        with closing(connect(draft_path)) as c, pytest.raises(StoreError, match="earlier draft"):
            migrate(c)
        with closing(connect(draft_path)) as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 9
            assert schema(c) == before_schema
            assert rows(c) == before_rows


def test_a_version_10_database_missing_a_009_index_is_still_refused(draft_path):
    with closing(connect(draft_path)) as c:
        c.execute("PRAGMA user_version = 10")
        c.commit()
    with closing(connect(draft_path)) as c, pytest.raises(StoreError, match="idx_guild_code"):
        migrate(c)


def test_a_draft_009_database_missing_a_column_is_refused_too(draft_path):
    with closing(connect(draft_path)) as c:
        c.execute("CREATE INDEX idx_guild_code_posts_code ON guild_code_posts (code)")
        c.execute("ALTER TABLE alerted_codes DROP COLUMN table_silenced_at")
        c.commit()
        before = schema(c)
    with closing(connect(draft_path)) as c, pytest.raises(StoreError, match="table_silenced_at"):
        migrate(c)
    with closing(connect(draft_path)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 9
        assert schema(c) == before


def test_a_healthy_database_passes_the_check_before_and_after_applying(
    tmp_path, monkeypatch, migrations_up_to
):
    path = make_v9(tmp_path / "ok.db", tmp_path, monkeypatch, migrations_up_to)
    with closing(connect(path)) as c:
        assert migrate(c) == 10
        assert migrate(c) == 10


# --- the wizard's select arithmetic ---


@pytest.fixture
def industry_cfg(monkeypatch):
    return load_v3_industry_config(monkeypatch)


def catalog_of(cfg, n, *, industry=True):
    base = cfg.catalog[0]
    entries = [base.model_copy(update={"key": f"game{i}", "name": f"Game {i}"}) for i in range(n)]
    if industry:
        entries.append(next(g for g in cfg.catalog if g.key == INDUSTRY_KEY))
    return cfg.model_copy(update={"catalog": entries})


def make_view(cfg, db_path):
    with closing(connect(db_path)) as c:
        guild = repo.get_guild(c, GUILD_A)
        followed = repo.list_guild_games(c, GUILD_A)
    return SetupView(
        cfg=cfg,
        bot=SimpleNamespace(get_guild=lambda guild_id: object()),
        db_path=db_path,
        owner_id=1,
        guild_id=GUILD_A,
        tier="free",
        guild=guild,
        followed=followed,
        origin=FakeInteraction(guild_id=GUILD_A, user_id=1),
    )


def click():
    return FakeInteraction(guild_id=GUILD_A, user_id=1)


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    ("n_games", "has_industry", "max_values", "max_games"),
    [
        (0, True, 1, 1),
        (1, True, 2, 2),
        (9, True, 10, 10),
        (10, True, 11, 10),
        (11, True, 11, 10),
        (14, True, 11, 10),
        (3, False, 3, 3),
        (10, False, 10, 10),
        (14, False, 10, 10),
    ],
)
def test_the_select_never_offers_more_values_than_options_or_than_ten_plus_industry(
    industry_cfg, v3_db, n_games, has_industry, max_values, max_games
):
    view = make_view(catalog_of(industry_cfg, n_games, industry=has_industry), v3_db)
    select = view.games_select
    assert (select.min_values, select.max_values) == (1, max_values)
    assert select.max_values <= len(select.options)
    assert view.max_games == max_games


def test_without_industry_in_the_catalog_the_placeholder_has_no_plus_clause(industry_cfg, v3_db):
    view = make_view(catalog_of(industry_cfg, 14, industry=False), v3_db)
    assert view.games_select.placeholder == "Games to follow (up to 10)"


def test_the_placeholder_text_with_the_entry_is_exact(industry_cfg, v3_db):
    view = make_view(catalog_of(industry_cfg, 14), v3_db)
    assert view.games_select.placeholder == "Games to follow (up to 10, plus Gaming industry)"


@pytest.mark.parametrize("name", ["\U0001f3ae" * 120, "x" * 400, "<@everyone> " * 30, "**" * 100])
def test_the_placeholder_stays_within_discords_150_units_for_any_entry_name(
    industry_cfg, v3_db, name
):
    cfg = catalog_of(industry_cfg, 12)
    entry = cfg.catalog[-1].model_copy(update={"name": name})
    cfg = cfg.model_copy(update={"catalog": [*cfg.catalog[:-1], entry]})
    placeholder = make_view(cfg, v3_db).games_select.placeholder
    assert 0 < utf16_len(placeholder) <= 150
    assert placeholder.startswith("Games to follow (up to 10, plus ")
    assert "<@everyone>" not in placeholder.replace("\\<\\@everyone\\>", "")


async def test_picking_every_option_of_a_ten_game_catalog_plus_industry_is_ten_of_ten(
    industry_cfg, v3_db
):
    view = make_view(catalog_of(industry_cfg, 10), v3_db)
    keys = [g.key for g in view.catalog]
    assert len(keys) == 11 and view.games_select.max_values == 11
    await view.on_games(click(), keys)
    assert view.game_keys == keys and view.note == ""
    assert "(10 of 10)" in view.render()


async def test_picking_every_option_of_an_eleven_game_catalog_plus_industry_is_refused(
    industry_cfg, v3_db
):
    view = make_view(catalog_of(industry_cfg, 11), v3_db)
    await view.on_games(click(), ["game0"])
    await view.on_games(click(), [g.key for g in view.catalog])
    assert view.game_keys == ["game0"]
    assert "at most 10 games" in view.note and "you picked 11" in view.note


async def test_a_click_order_with_industry_first_still_lands_in_catalog_order(industry_cfg, v3_db):
    view = make_view(catalog_of(industry_cfg, 12), v3_db)
    await view.on_games(click(), [INDUSTRY_KEY, "game5", "game1", "game5"])
    assert view.game_keys == ["game1", "game5", INDUSTRY_KEY]
    assert "(2 of 10)" in view.render()


async def test_a_server_already_at_ten_plus_industry_opens_the_wizard_and_saves_unchanged(
    industry_cfg, v3_db, monkeypatch
):
    async def fake_check(client, db_path, guild_id, *, game_names=None):
        return GuildCheck(guild_id, ())

    monkeypatch.setattr(setup_views, "check_guild", fake_check)
    cfg = catalog_of(industry_cfg, 12)
    keys = [f"game{i}" for i in range(10)] + [INDUSTRY_KEY]
    make_guild(v3_db, GUILD_A, games=[(k, 100 + i) for i, k in enumerate(keys)])
    view = make_view(cfg, v3_db)
    assert view.game_keys == keys
    assert "(10 of 10)" in view.render()
    await view.on_channel(click(), [thin_channel(111111111111111111, GUILD_A)])
    await view.on_save(click())
    with closing(connect(v3_db)) as c:
        assert [g.game_key for g in repo.list_guild_games(c, GUILD_A)] == keys


# --- /newsbot follow, /newsbot unfollow and /newsbot games ---


@pytest.fixture
def industry_admin(industry_cfg, v3_db, monkeypatch):
    async def fake_check(client, db_path, guild_id, *, game_names=None):
        return GuildCheck(guild_id, ())

    monkeypatch.setattr(commands_module, "check_guild", fake_check)
    group = make_guild_admin_group(industry_cfg, fake_bot(v3_db))
    return lambda name: command(group, name).callback


async def test_follow_industry_works_on_a_full_server_and_the_eleventh_game_message_is_unchanged(
    industry_admin, v3_db
):
    make_guild(v3_db, GUILD_A, games=games(10))
    said = FakeInteraction()
    await industry_admin("follow")(said, game="industry", channel=channel(5))
    assert "Now following Gaming industry" in said.text

    refused = FakeInteraction()
    await industry_admin("follow")(refused, game="palworld", channel=channel(6))
    assert "This server already follows 10 games; unfollow one first" in refused.text
    with closing(connect(v3_db)) as c:
        assert len(repo.list_guild_games(c, GUILD_A)) == 11


async def test_games_says_ten_of_ten_for_ten_games_plus_industry_and_lists_industry(
    industry_admin, v3_db
):
    make_guild(v3_db, GUILD_A, games=[*games(10), (INDUSTRY_KEY, 77)])
    said = FakeInteraction()
    await industry_admin("games")(said)
    assert "Following (10 of 10):" in said.text
    assert "Gaming industry in <#77>" in said.text
    assert "Available:" in said.text and "Gaming industry" not in said.text.split("Available:")[1]


async def test_games_says_zero_of_ten_for_industry_alone(industry_admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[(INDUSTRY_KEY, 77)])
    said = FakeInteraction()
    await industry_admin("games")(said)
    assert "Following (0 of 10):" in said.text
    assert "(none yet)" not in said.text


async def test_unfollowing_the_last_game_with_industry_left_does_not_say_no_digest_will_post(
    industry_admin, v3_db
):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5), (INDUSTRY_KEY, 77)])
    said = FakeInteraction()
    await industry_admin("unfollow")(said, game="palworld")
    assert "Stopped following Palworld" in said.text
    assert "no digest will post" not in said.text
