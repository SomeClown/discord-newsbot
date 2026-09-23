"""Connection setup and the migration runner.

Nothing fancy: SQLite in WAL mode, one migration file so far, and a version
tracked in `PRAGMA user_version` because SQLite already gives us that for
free (a bespoke `schema_migrations` table would just be reinventing it,
worse). Every connection is short-lived: open, do the unit of work, close,
called from async code through `asyncio.to_thread`, which sidesteps
`sqlite3`'s single-thread-per-connection rule without needing a connection
pool for a database this small.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class StoreError(Exception):
    """Raised when the store can't do something it needs to, like find FTS5."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection configured the way every part of this app expects.

    WAL mode so readers (slash commands) don't block on the writer (the
    daily job), foreign keys on because SQLite defaults them off for
    backwards compatibility reasons that don't apply to us, and a five
    second busy timeout so two connections racing for the write lock get a
    retry instead of an immediate `database is locked`.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any `NNN_*.sql` files newer than `PRAGMA user_version`.

    Each file runs in its own transaction and bumps `user_version` to its
    number on success, so a crash mid-migration doesn't leave the database
    thinking it's further along than it is. Returns the resulting version.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    migrations = sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    for path in migrations:
        version = int(path.name.split("_", 1)[0])
        if version <= current:
            continue
        sql = path.read_text()
        with conn:
            conn.executescript(sql)
            # executescript issues an implicit COMMIT before running, so
            # user_version has to be set inside the same `with` block to
            # stay covered by its own transaction, not the script's.
            conn.execute(f"PRAGMA user_version = {version}")
        current = version

    return current


def assert_fts5(conn: sqlite3.Connection) -> None:
    """Confirm the SQLite build backing `conn` actually has FTS5 compiled in.

    Debian's libsqlite3 and Homebrew's both ship it, but "usually available"
    isn't the same as "guaranteed", and finding out at query time (after the
    daily job has already collected and summarized everything) would be a
    spectacularly annoying way to learn otherwise. We'd rather fail at
    startup with a clear message.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.newsbot_fts5_check USING fts5(a)")
        conn.execute("DROP TABLE temp.newsbot_fts5_check")
    except sqlite3.OperationalError as exc:
        raise StoreError(
            "This SQLite build doesn't have FTS5 compiled in; /news search can't work."
        ) from exc
