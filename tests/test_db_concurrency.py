"""Two real connections to the same file, the way `store/db.connect` is
actually used: every call into the store goes through `asyncio.to_thread`,
which means the daily job and a `/news` command can genuinely be talking to
SQLite at the same moment, from different connections, on different threads.

These tests don't use `asyncio.to_thread` directly (that's more machinery
than the behavior needs); plain `threading.Thread` against two connections
opened by `connect()` exercises the same WAL-mode-plus-busy-timeout contract
`to_thread` would hit in production. Two things are asserted: a second
writer waits out the first one's lock instead of raising `database is
locked` immediately, and a reader isn't blocked by an in-progress writer at
all (that second part is the entire reason WAL mode is turned on).
"""

import threading
import time
from contextlib import closing

import pytest

from newsbot.store.db import connect, migrate

HOLD_SECONDS = 0.3


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "newsbot.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _hold_write_lock(path, ready: threading.Event, release: threading.Event) -> None:
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES ('2026-09-01', 'ok', 'now', 'now')"
        )
        ready.set()
        release.wait(timeout=5)
        conn.commit()


def test_second_writer_waits_out_busy_timeout_instead_of_failing_immediately(db_path):
    ready = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db_path, ready, release))
    holder.start()
    ready.wait(timeout=5)

    def _release_after_hold():
        time.sleep(HOLD_SECONDS)
        release.set()

    threading.Thread(target=_release_after_hold).start()

    start = time.monotonic()
    with closing(connect(db_path)) as conn:
        # Racing the holder's transaction. Without `PRAGMA busy_timeout`,
        # this raises `sqlite3.OperationalError: database is locked`
        # immediately; with it, sqlite retries internally until the lock
        # frees or the timeout (5s, well over HOLD_SECONDS) elapses.
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES ('2026-09-02', 'ok', 'now', 'now')"
        )
        conn.commit()
    elapsed = time.monotonic() - start

    holder.join(timeout=5)
    # It actually waited for the lock rather than sneaking in before the
    # holder started its transaction (which would make this test pass for
    # the wrong reason).
    assert elapsed >= HOLD_SECONDS * 0.5

    with closing(connect(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    assert count == 2


def test_reader_is_not_blocked_by_an_open_writer_transaction(db_path):
    """The whole point of WAL mode: a reader sees a consistent snapshot
    without waiting on the writer."""
    ready = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db_path, ready, release))
    holder.start()
    ready.wait(timeout=5)

    try:
        start = time.monotonic()
        with closing(connect(db_path)) as reader:
            # The writer hasn't committed yet, so this shouldn't see its row,
            # and -- the actual point of this test -- shouldn't have to wait
            # for it either.
            count = reader.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
        elapsed = time.monotonic() - start
    finally:
        release.set()
        holder.join(timeout=5)

    assert count == 0
    assert elapsed < HOLD_SECONDS  # didn't wait for the writer to finish
