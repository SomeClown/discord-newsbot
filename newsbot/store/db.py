"""Connection setup and the migration runner.

Nothing fancy: SQLite in WAL mode, numbered migration files, and a version
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
        current = _apply(conn, path, version, current)

    return current


_FK_OFF_HEADER = "-- newsbot: foreign-keys-off"


def _apply(conn: sqlite3.Connection, path: Path, version: int, current: int) -> int:
    """Run one migration under the write lock; return the version afterwards.

    `user_version` gets re-read after `BEGIN IMMEDIATE`, not trusted from
    before it. I originally checked once up front, which is fine right up
    until several processes open the same old file at once: they all see
    the old version, all run the migration, and the losers die with
    "already exists". Under the lock, a loser sees the winner's version and
    skips. `autocommit` is on for the duration because `executescript`
    would otherwise commit first and quietly hand the lock back.

    A file whose first line is `-- newsbot: foreign-keys-off` is a table
    rebuild, and SQLite's documented recipe for those (the "12 steps" in
    its ALTER TABLE docs) needs foreign keys off while tables are dropped
    and renamed, or the drop cascades into child rows. `PRAGMA
    foreign_keys` is a silent no-op inside a transaction, so it gets
    flipped outside `BEGIN IMMEDIATE` and restored in a `finally`. Before
    the commit, `PRAGMA foreign_key_check` has to come back empty; any row
    rolls the whole migration back and names the offending table.
    """
    text = path.read_text()
    fk_off = text.startswith(_FK_OFF_HEADER)
    old_autocommit = conn.autocommit
    conn.commit()
    conn.autocommit = True
    old_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    try:
        if fk_off:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = conn.execute("PRAGMA user_version").fetchone()[0]
            if latest >= version:
                conn.execute("ROLLBACK")
                return latest
            conn.executescript(text)
            if fk_off:
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    tables = sorted({row[0] for row in violations})
                    raise StoreError(
                        f"Migration {path.name} left foreign key violations in: "
                        f"{', '.join(tables)}. Rolled back."
                    )
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        if fk_off:
            conn.execute(f"PRAGMA foreign_keys = {'ON' if old_fk else 'OFF'}")
        conn.autocommit = old_autocommit
    return version


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
