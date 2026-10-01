"""The dry-run's throwaway copy works on a read-only data directory too (qa review, fix 8).

`--dry-run` copies the database through a read-only connection so a rehearsal can't touch the
real file. On a database in WAL mode that fails when the directory is read-only (a mounted
volume, a root-owned backup): SQLite wants to create a `-shm` file even to read. The copy then
falls back to copying the file and its `-wal` into the scratch directory and backing up from
there. These tests make a really read-only directory, which works on macOS and Linux when
the tests aren't run as root.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from newsbot.pipeline import run
from newsbot.pipeline.run import _read_only_copy
from newsbot.store.db import connect, migrate

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")


@contextmanager
def read_only(directory: Path):
    os.chmod(directory, 0o555)  # noqa: S103 (the point: a directory nothing can be created in)
    try:
        yield
    finally:
        os.chmod(directory, 0o755)  # noqa: S103 (so pytest can clean up)


def rows(path: str) -> list[int]:
    with closing(sqlite3.connect(path)) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM items ORDER BY id")]


def make_db(directory: Path, *, wal_left_behind: bool) -> Path:
    path = directory / "newsbot.db"
    conn = connect(path)
    migrate(conn)
    for n in range(1, 4):
        conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES (?, 't', '', 's', 'press', 'x')",
            (f"https://example.com/{n}",),
        )
    conn.commit()
    if wal_left_behind:
        # A crash (or a copy taken mid-run) leaves committed rows only in the -wal file.
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES ('https://example.com/4', 't', '', 's', 'press', 'x')"
        )
        conn.commit()
        snapshot = directory.parent / (directory.name + "-snapshot")
        snapshot.mkdir()
        shutil.copyfile(path, snapshot / "newsbot.db")
        shutil.copyfile(f"{path}-wal", snapshot / "newsbot.db-wal")
        conn.close()
        return snapshot / "newsbot.db"
    conn.close()
    return path


def test_a_read_only_directory_still_yields_a_complete_copy(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = make_db(data, wal_left_behind=False)
    with read_only(data):
        # The plain read-only open really does fail here, which is the point of the fallback.
        with pytest.raises(sqlite3.OperationalError):
            run._backup_read_only(db, tmp_path / "nope.db")
        with _read_only_copy(str(db)) as copy:
            assert rows(copy) == [1, 2, 3]
            with closing(connect(copy)) as conn:  # and it's a copy anyone can write to
                conn.execute("DELETE FROM items")
                conn.commit()
    assert rows(str(db)) == [1, 2, 3]  # the original is untouched
    assert sorted(p.name for p in data.iterdir()) == ["newsbot.db"]  # and nothing was left behind


def test_a_read_only_directory_with_a_wal_left_behind_keeps_the_wal_rows(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = make_db(data, wal_left_behind=True)
    assert Path(f"{db}-wal").exists()
    with read_only(db.parent), _read_only_copy(str(db)) as copy:
        assert rows(copy) == [1, 2, 3, 4]  # the row that only the -wal knew about is there


def test_the_fallback_runs_when_the_read_only_open_fails_for_any_reason(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = make_db(data, wal_left_behind=False)

    def refuse(source, copy):
        copy.write_bytes(b"half a database")  # a failed attempt may leave debris behind
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(run, "_backup_read_only", refuse)
    with _read_only_copy(str(db)) as copy:
        assert rows(copy) == [1, 2, 3]


def test_a_readable_database_still_goes_through_the_read_only_backup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = make_db(data, wal_left_behind=False)
    monkeypatch.setattr(
        run, "_backup_from_file_copy", lambda *a: pytest.fail("fallback used needlessly")
    )
    with _read_only_copy(str(db)) as copy:
        assert rows(copy) == [1, 2, 3]


def test_a_missing_database_still_starts_empty_and_creates_nothing(tmp_path):
    missing = tmp_path / "nope" / "newsbot.db"
    with _read_only_copy(str(missing)) as copy:
        assert not Path(copy).exists()
    assert not missing.parent.exists()
