"""Seed a database with the rows v2's `save_run` used to write, without v2's `save_run`.

The cutover deleted `claim_digest` and `save_run` from `newsbot/store/repo.py` (v3 has its own
writers, and v2.2's live behaviour is pinned by `fixtures/v22_repo_snapshot.txt`). A number of
read-path tests only ever used them to put a story or two in the database, so this is the
smallest honest replacement: items through the v3 `store_items`, then a digest row and stories
by plain SQL. It writes what the old function wrote and checks nothing the tests don't.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from sqlite3 import Connection

from newsbot.store import repo
from newsbot.store.models import StoredItem, StoryToSave

V22_SNAPSHOT = Path(__file__).parent / "fixtures" / "v22_repo_snapshot.txt"


def load_v22_repo() -> types.ModuleType:
    """v2.2.0's `repo.py`, exactly as the checked-in snapshot has it, as a throwaway module.

    For tests that want what the old code does (claim a day, claim a code) rather than a
    bare row in a table: v2.2's live behaviour against a current database is the whole
    point of the rollback tests.
    """
    module = types.ModuleType("v22_repo_loaded")
    exec(compile(V22_SNAPSHOT.read_text(), str(V22_SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
    return module


def seed_run(
    conn: Connection,
    run_date: date,
    items: Sequence[StoredItem] = (),
    stories: Sequence[StoryToSave] = (),
    *,
    status: str = "ok",
    message_ids: Sequence[int] = (),
    notes: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Store `items`, then one guild-less digest row for `run_date` holding `stories`.

    Returns the digest id. A story is skipped if none of its urls were stored, like the old
    writer did. `message_ids` are written as the row's `posted_message_ids`.
    """
    when = (now or (lambda: datetime.now(UTC)))().isoformat()
    repo.store_items(conn, list(items), now=now)
    with conn:
        digest_id = conn.execute(
            "INSERT INTO digests (run_date, status, posted_message_ids, error_notes, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_date.isoformat(), status, json.dumps(list(message_ids)), notes, when, when),
        ).lastrowid
        for story in stories:
            item_ids = [
                row[0]
                for url in story.item_urls
                for row in conn.execute("SELECT id FROM items WHERE url = ?", (url,))
            ]
            if not item_ids:
                continue
            story_id = conn.execute(
                "INSERT INTO stories (topic_key, headline, summary, label, is_update_of, "
                "digest_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    story.topic_key,
                    story.headline,
                    story.summary,
                    story.label,
                    story.update_of_story_id,
                    digest_id,
                    when,
                ),
            ).lastrowid
            for item_id in item_ids:
                conn.execute(
                    "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)", (story_id, item_id)
                )
    return digest_id
