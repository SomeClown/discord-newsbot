"""Migration 010 (the Gaming industry entry stops counting toward the 10-game limit).

The trigger from 005 counted every row; 010 replaces it with one that skips the
`industry` key on both sides (it neither counts nor is refused). It runs on production's
real database at deploy time, so the test that matters most is the first one: a version-9
database with a full ten games per server keeps every row.
"""

import sqlite3
from contextlib import closing

import pytest

from newsbot.store import db, repo
from newsbot.store.db import connect, migrate

ERROR = "at most 10 games per guild"


def _games(n, *, start=0):
    return [(f"game{i}", 100 + i) for i in range(start, start + n)]


@pytest.fixture
def v9_path(tmp_path, monkeypatch, migrations_up_to):
    path = tmp_path / "v9.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", migrations_up_to(tmp_path / "migrations_v9", 9))
        with closing(connect(path)) as conn:
            assert migrate(conn) == 9
            with conn:
                for guild_id, offset in ((7, 0), (8, 20)):
                    conn.execute(
                        "INSERT INTO guilds (guild_id, joined_at, updated_at) VALUES (?, 'n', 'n')",
                        (guild_id,),
                    )
                    conn.executemany(
                        "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)",
                        [(guild_id, key, channel) for key, channel in _games(10, start=offset)],
                    )
    return path


@pytest.fixture
def conn(v3_db):
    with closing(connect(v3_db)) as c:
        repo.create_guild(c, 7)
        yield c


def _rows(c):
    return [tuple(r) for r in c.execute("SELECT * FROM guild_games ORDER BY rowid")]


def _trigger_sql(c):
    return [
        r[0]
        for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'guild_games_limit'"
        )
    ]


def test_a_v9_database_with_ten_games_per_server_keeps_every_row(v9_path):
    with closing(connect(v9_path)) as c:
        before = _rows(c)
        guilds_before = [tuple(r) for r in c.execute("SELECT * FROM guilds ORDER BY guild_id")]
    assert len(before) == 20
    with closing(connect(v9_path)) as c:
        assert migrate(c) == 10
        assert c.execute("PRAGMA user_version").fetchone()[0] == 10
        assert _rows(c) == before
        assert [tuple(r) for r in c.execute("SELECT * FROM guilds ORDER BY guild_id")] == (
            guilds_before
        )
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert len(_trigger_sql(c)) == 1
        # The migrated database has the new rule, not just the new number.
        repo.follow_game(c, 7, "industry", 555)
        assert len(repo.list_guild_games(c, 7)) == 11
        with pytest.raises(repo.GameLimitError):
            repo.follow_game(c, 8, "game99", 556)


def test_migrating_twice_is_a_noop(v9_path):
    with closing(connect(v9_path)) as c:
        assert migrate(c) == 10
    with closing(connect(v9_path)) as c:
        assert migrate(c) == 10
        assert len(_trigger_sql(c)) == 1


def test_a_fresh_database_lands_on_version_10(v3_db):
    with closing(connect(v3_db)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 10


def test_a_guild_with_ten_games_can_follow_industry(conn):
    repo.set_guild_games(conn, 7, _games(10))
    assert repo.follow_game(conn, 7, "industry", 555) is True
    assert len(repo.list_guild_games(conn, 7)) == 11


def test_with_industry_followed_an_eleventh_counted_game_is_still_refused(conn):
    repo.set_guild_games(conn, 7, _games(10))
    repo.follow_game(conn, 7, "industry", 555)
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game10", 556)
    assert len(repo.list_guild_games(conn, 7)) == 11


def test_industry_does_not_use_a_slot(conn):
    repo.follow_game(conn, 7, "industry", 555)
    for key, channel in _games(10):
        repo.follow_game(conn, 7, key, channel)
    assert len(repo.list_guild_games(conn, 7)) == 11


def test_industry_can_be_followed_first_or_last_and_unfollowed(conn):
    repo.set_guild_games(conn, 7, _games(10))
    repo.follow_game(conn, 7, "industry", 555)
    assert repo.unfollow_game(conn, 7, "industry") is True
    assert len(repo.list_guild_games(conn, 7)) == 10
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(conn, 7, "game10", 556)


def test_re_pointing_industrys_channel_works(conn):
    repo.set_guild_games(conn, 7, _games(10))
    repo.follow_game(conn, 7, "industry", 555)
    assert repo.follow_game(conn, 7, "industry", 777) is False
    moved = {g.game_key: g.channel_id for g in repo.list_guild_games(conn, 7)}
    assert moved["industry"] == 777


def test_set_guild_games_takes_ten_plus_industry_and_refuses_eleven_plus_industry(conn):
    repo.set_guild_games(conn, 7, [*_games(10), ("industry", 555)])
    assert len(repo.list_guild_games(conn, 7)) == 11
    before = _rows(conn)
    with pytest.raises(repo.GameLimitError):
        repo.set_guild_games(conn, 7, [*_games(11), ("industry", 555)])
    assert _rows(conn) == before


def test_apply_guild_setup_takes_ten_plus_industry_and_refuses_eleven_plus_industry(conn):
    repo.apply_guild_setup(
        conn, 7, digest_time="09:00", timezone="UTC", games=[*_games(10), ("industry", 555)]
    )
    assert len(repo.list_guild_games(conn, 7)) == 11
    before = _rows(conn)
    with pytest.raises(repo.GameLimitError):
        repo.apply_guild_setup(
            conn, 7, digest_time="09:00", timezone="UTC", games=[*_games(11), ("industry", 555)]
        )
    assert _rows(conn) == before


def test_apply_guild_setup_adds_industry_to_a_full_server(conn):
    repo.set_guild_games(conn, 7, _games(10))
    repo.apply_guild_setup(
        conn, 7, digest_time="09:00", timezone="UTC", games=[*_games(10), ("industry", 555)]
    )
    assert [g.game_key for g in repo.list_guild_games(conn, 7)][-1] == "industry"


def test_a_repeated_industry_key_is_still_a_duplicate(conn):
    with pytest.raises(ValueError, match="Duplicate"):
        repo.set_guild_games(conn, 7, [("industry", 1), ("industry", 2)])
    with pytest.raises(ValueError, match="Duplicate"):
        repo.apply_guild_setup(
            conn, 7, digest_time="09:00", timezone="UTC", games=[("industry", 1), ("industry", 2)]
        )


def test_a_raw_insert_of_an_eleventh_other_row_aborts_with_the_old_message(conn):
    repo.set_guild_games(conn, 7, _games(10))
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (7, 'x', 1)")


def test_a_raw_insert_of_an_eleventh_other_row_aborts_even_with_industry_followed(conn):
    repo.set_guild_games(conn, 7, [*_games(10), ("industry", 555)])
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (7, 'x', 1)")


def test_a_raw_insert_of_industry_at_ten_games_goes_through(conn):
    repo.set_guild_games(conn, 7, _games(10))
    conn.execute(
        "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (7, 'industry', 1)"
    )
    assert conn.execute("SELECT COUNT(*) FROM guild_games WHERE guild_id = 7").fetchone()[0] == 11


def test_the_limit_is_per_guild(conn):
    repo.create_guild(conn, 8)
    repo.set_guild_games(conn, 7, [*_games(10), ("industry", 555)])
    repo.follow_game(conn, 8, "industry", 556)
    assert repo.follow_game(conn, 8, "game0", 557) is True


def test_a_failure_halfway_through_010_leaves_version_9_and_the_old_trigger(
    v9_path, tmp_path, monkeypatch
):
    # The real file with a statement that fails tacked on the end: the DROP and CREATE
    # before it have already run inside the transaction, and all of it has to go.
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    for path in db._MIGRATIONS_DIR.glob("0*.sql"):
        text = path.read_text()
        if path.name.startswith("010_"):
            text += "\nINSERT INTO no_such_table VALUES (1);\n"
        (broken_dir / path.name).write_text(text)
    with closing(connect(v9_path)) as c:
        before = _rows(c)
        old_trigger = _trigger_sql(c)
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", broken_dir)
    with closing(connect(v9_path)) as c, pytest.raises(sqlite3.OperationalError):
        migrate(c)
    with closing(connect(v9_path)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 9
        assert _rows(c) == before
        assert _trigger_sql(c) == old_trigger


def test_renaming_industry_into_a_game_is_refused_at_ten_and_fine_below(conn):
    repo.set_guild_games(conn, 7, [*_games(10), ("industry", 555)])
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("UPDATE guild_games SET game_key = 'x' WHERE game_key = 'industry'")
    repo.unfollow_game(conn, 7, "game0")
    conn.execute("UPDATE guild_games SET game_key = 'x' WHERE game_key = 'industry'")
    assert conn.execute("SELECT COUNT(*) FROM guild_games WHERE game_key = 'x'").fetchone()[0] == 1


def test_updates_that_do_not_raise_the_count_are_untouched_at_ten(conn):
    repo.set_guild_games(conn, 7, [*_games(10), ("industry", 555)])
    conn.execute("UPDATE guild_games SET channel_id = 9 WHERE game_key = 'game0'")
    conn.execute("UPDATE guild_games SET game_key = 'renamed' WHERE game_key = 'game1'")
    conn.execute("UPDATE guild_games SET channel_id = 9 WHERE game_key = 'industry'")
    assert conn.execute("SELECT COUNT(*) FROM guild_games WHERE guild_id = 7").fetchone()[0] == 11


def test_moving_a_row_into_a_full_guild_is_refused_unless_it_is_industry(conn):
    repo.create_guild(conn, 8)
    repo.set_guild_games(conn, 7, _games(10))
    repo.set_guild_games(conn, 8, [("a", 1), ("industry", 2)])
    with pytest.raises(sqlite3.IntegrityError, match=ERROR):
        conn.execute("UPDATE guild_games SET guild_id = 7 WHERE game_key = 'a'")
    conn.execute("UPDATE guild_games SET guild_id = 7 WHERE game_key = 'industry'")
    assert conn.execute("SELECT COUNT(*) FROM guild_games WHERE guild_id = 7").fetchone()[0] == 11
