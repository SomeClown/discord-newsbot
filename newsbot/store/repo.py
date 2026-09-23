"""Every query the pipeline needs. No SQL anywhere else in the codebase.

Keeping the SQL in one file is a rule, not a preference: it means a
"what tables does `is_update_of` touch" question has exactly one file to
grep, and it means the parameterization discipline (nothing ever gets
string-formatted into a query) only has to be checked in one place.

The write path centers on a claim-then-publish-then-save guard
(SPEC-DEV 2): `claim_digest` reserves today's slot with a `pending` row
*before* anything slow happens (summarizing, posting to Discord), and
`save_run` writes items, stories and the final status together in one
transaction, after publishing succeeds. If posting fails, nothing gets
saved; a retry just recollects, which beats the alternative of a digest
that's half-posted and half-recorded (I've seen that movie, and it's not
a good one).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta

from newsbot.store.models import (
    DigestRow,
    PriorStory,
    SourceHealthRow,
    StatusSnapshot,
    StoredItem,
    StoryToSave,
    StoryView,
    Usage,
)

# SQLite caps a single statement at 999 (or 32766 on newer builds, but we
# don't get to pick) bound parameters. Chunking here means callers never
# have to think about the limit or hit it in production with a big batch
# of collected URLs.
_SQLITE_VARIABLE_CHUNK = 900

_ONE_DAY = timedelta(hours=24)


def _resolve_now(now: Callable[[], datetime] | None) -> str:
    """Turn an optional injected clock into an ISO timestamp string.

    `now` defaults to the real UTC wall clock; tests pass a fixed one so
    "what time did this claim happen" doesn't depend on how fast CI is
    today.
    """
    return (now or (lambda: datetime.now(UTC)))().isoformat()


def existing_urls(conn: sqlite3.Connection, urls: Iterable[str]) -> set[str]:
    """Return the subset of `urls` already present in `items`, for dedupe."""
    url_list = list(urls)
    found: set[str] = set()
    for i in range(0, len(url_list), _SQLITE_VARIABLE_CHUNK):
        chunk = url_list[i : i + _SQLITE_VARIABLE_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        # placeholders is a string of literal "?"s sized to the chunk, never
        # interpolated user data; the actual values are bound below.
        query = f"SELECT url FROM items WHERE url IN ({placeholders})"  # noqa: S608
        rows = conn.execute(query, chunk)
        found.update(row["url"] for row in rows)
    return found


def recent_headlines(conn: sqlite3.Connection, topic_key: str, since: datetime) -> list[PriorStory]:
    """Headlines for `topic_key` since `since`, newest first.

    Fed back to the summarizer so it can say "same as yesterday" instead of
    re-announcing a patch note for the third day running.
    """
    rows = conn.execute(
        "SELECT id, headline, created_at FROM stories "
        "WHERE topic_key = ? AND created_at >= ? ORDER BY created_at DESC",
        (topic_key, since.isoformat()),
    ).fetchall()
    return [
        PriorStory(
            id=row["id"],
            headline=row["headline"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


def get_digest(conn: sqlite3.Connection, run_date: date) -> DigestRow | None:
    row = conn.execute(
        "SELECT id, run_date, status, posted_message_ids, error_notes "
        "FROM digests WHERE run_date = ?",
        (run_date.isoformat(),),
    ).fetchone()
    if row is None:
        return None
    return DigestRow(
        id=row["id"],
        run_date=date.fromisoformat(row["run_date"]),
        status=row["status"],
        posted_message_ids=json.loads(row["posted_message_ids"]),
        error_notes=row["error_notes"],
    )


def claim_digest(
    conn: sqlite3.Connection,
    run_date: date,
    *,
    force: bool,
    now: Callable[[], datetime] | None = None,
) -> int | None:
    """Reserve `run_date` for a run, or say no.

    Returns the digest id (status set to `pending`) if the claim succeeds,
    or `None` if it's blocked:
      - a `pending` row already exists (something else is running, or
        crashed mid-run; either way, we don't want to overlap it)
      - an `ok`/`partial` row exists and `force` wasn't passed
    A `failed` row always allows a reclaim (that day never actually posted).
    `force=True` against `ok`/`partial` updates the existing row in place,
    keeping its id, rather than inserting a second row for the same date.
    """
    now_iso = _resolve_now(now)
    with conn:
        row = conn.execute(
            "SELECT id, status FROM digests WHERE run_date = ?", (run_date.isoformat(),)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO digests (run_date, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (run_date.isoformat(), "pending", now_iso, now_iso),
            )
            return cur.lastrowid

        digest_id, status = row["id"], row["status"]
        if status == "pending":
            return None
        if status in ("ok", "partial") and not force:
            return None
        conn.execute(
            "UPDATE digests SET status = 'pending', updated_at = ? WHERE id = ?",
            (now_iso, digest_id),
        )
        return digest_id


def save_run(
    conn: sqlite3.Connection,
    digest_id: int,
    items: list[StoredItem],
    stories: list[StoryToSave],
    status: str,
    message_ids: list[int],
    notes: str | None,
    usage: Usage,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Save one run's items, stories and final digest status, atomically.

    Everything happens inside a single transaction. If any statement fails
    (a bad status value, a constraint violation, whatever), the whole thing
    rolls back, so a half-saved run never sits in the database looking like
    a real one.
    """
    now_iso = _resolve_now(now)
    with conn:
        url_to_id: dict[str, int] = {}
        for item in items:
            conn.execute(
                "INSERT INTO items "
                "(url, title, excerpt, source_name, trust, published_at, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO NOTHING",
                (
                    item.url,
                    item.title,
                    item.excerpt,
                    item.source_name,
                    item.trust,
                    item.published_at.isoformat() if item.published_at else None,
                    now_iso,
                ),
            )
            item_id = conn.execute("SELECT id FROM items WHERE url = ?", (item.url,)).fetchone()[0]
            url_to_id[item.url] = item_id
            for topic_key, uncertain in item.topics.items():
                conn.execute(
                    "INSERT INTO item_topics (item_id, topic_key, uncertain) VALUES (?, ?, ?) "
                    "ON CONFLICT(item_id, topic_key) DO NOTHING",
                    (item_id, topic_key, int(uncertain)),
                )

        for story in stories:
            item_ids = [url_to_id[u] for u in story.item_urls if u in url_to_id]
            if not item_ids:
                # Shouldn't happen: postprocess() in pipeline/summarize.py
                # already drops stories with no valid URLs. If it does
                # happen, better to skip the story than write an orphan.
                continue
            cur = conn.execute(
                "INSERT INTO stories "
                "(topic_key, headline, summary, label, is_update_of, digest_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    story.topic_key,
                    story.headline,
                    story.summary,
                    story.label,
                    story.update_of_story_id,
                    digest_id,
                    now_iso,
                ),
            )
            story_id = cur.lastrowid
            for item_id in item_ids:
                conn.execute(
                    "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)", (story_id, item_id)
                )

        conn.execute(
            "UPDATE digests SET status = ?, posted_message_ids = ?, error_notes = ?, "
            "input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, updated_at = ? "
            "WHERE id = ?",
            (
                status,
                json.dumps(message_ids),
                notes,
                usage.input_tokens,
                usage.output_tokens,
                now_iso,
                digest_id,
            ),
        )


def mark_digest_failed(
    conn: sqlite3.Connection,
    digest_id: int,
    notes: str,
    message_ids: list[int],
    now: Callable[[], datetime] | None = None,
) -> None:
    """Record that a run's publish step failed.

    Items and stories aren't touched here: `save_run` never ran for this
    attempt, so there's nothing to undo. A retry just recollects.
    """
    now_iso = _resolve_now(now)
    with conn:
        conn.execute(
            "UPDATE digests SET status = 'failed', error_notes = ?, posted_message_ids = ?, "
            "updated_at = ? WHERE id = ?",
            (notes, json.dumps(message_ids), now_iso, digest_id),
        )


def record_source_result(
    conn: sqlite3.Connection, source_name: str, now: datetime, error: str | None
) -> int:
    """Update a source's health row and return its new consecutive-failure count."""
    now_iso = now.isoformat()
    with conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM source_health WHERE source_name = ?", (source_name,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO source_health (source_name, consecutive_failures) VALUES (?, 0)",
                (source_name,),
            )
            consecutive_failures = 0
        else:
            consecutive_failures = row["consecutive_failures"]

        if error is None:
            consecutive_failures = 0
            conn.execute(
                "UPDATE source_health SET last_success_at = ?, consecutive_failures = 0 "
                "WHERE source_name = ?",
                (now_iso, source_name),
            )
        else:
            consecutive_failures += 1
            conn.execute(
                "UPDATE source_health "
                "SET last_error_at = ?, last_error = ?, consecutive_failures = ? "
                "WHERE source_name = ?",
                (now_iso, error, consecutive_failures, source_name),
            )
    return consecutive_failures


def purge_older_than(conn: sqlite3.Connection, cutoff: datetime) -> tuple[int, int]:
    """Delete items and stories older than `cutoff`. Returns (items_deleted, stories_deleted).

    Foreign keys handle the rest: `story_items` and `item_topics` rows
    cascade-delete with their parent, `stories_fts` follows along through
    the AFTER DELETE trigger, and any story pointing at a deleted one via
    `is_update_of` gets nulled rather than orphaned.
    """
    cutoff_iso = cutoff.isoformat()
    with conn:
        items_deleted = conn.execute(
            "DELETE FROM items WHERE collected_at < ?", (cutoff_iso,)
        ).rowcount
        stories_deleted = conn.execute(
            "DELETE FROM stories WHERE created_at < ?", (cutoff_iso,)
        ).rowcount
    return items_deleted, stories_deleted


# --- Read path: /news commands and /newsbot status ---
#
# Everything below only reads. It's split out here more for the reader's
# sake than the database's: the write path above has to reason about
# atomicity and the claim guard, and none of that applies here.

_FTS_TOKEN_RE = re.compile(r"\S+")


def _story_urls(conn: sqlite3.Connection, story_ids: list[int]) -> dict[int, list[str]]:
    """Map story id to its item URLs, in the order they were linked."""
    urls_by_story: dict[int, list[str]] = {story_id: [] for story_id in story_ids}
    for i in range(0, len(story_ids), _SQLITE_VARIABLE_CHUNK):
        chunk = story_ids[i : i + _SQLITE_VARIABLE_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        # placeholders is a run of literal "?"s sized to the chunk, not
        # interpolated user data; the actual values are bound below.
        select = "SELECT story_items.story_id, items.url FROM story_items "
        join = "JOIN items ON items.id = story_items.item_id "
        where = f"WHERE story_items.story_id IN ({placeholders}) "  # noqa: S608
        order = "ORDER BY story_items.story_id, items.id"
        for row in conn.execute(select + join + where + order, chunk):
            urls_by_story[row["story_id"]].append(row["url"])
    return urls_by_story


def _rows_to_story_views(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[StoryView]:
    urls_by_story = _story_urls(conn, [row["id"] for row in rows])
    return [
        StoryView(
            id=row["id"],
            topic_key=row["topic_key"],
            headline=row["headline"],
            summary=row["summary"],
            label=row["label"],
            created_at=datetime.fromisoformat(row["created_at"]),
            urls=urls_by_story.get(row["id"], []),
            is_update_of=row["is_update_of"],
        )
        for row in rows
    ]


def query_stories(
    conn: sqlite3.Connection,
    topic_keys: list[str],
    since: datetime,
    label: str | None,
    limit: int,
    offset: int,
) -> tuple[list[StoryView], int]:
    """Stories for `/news recent`, newest first. An empty `topic_keys` means "All"."""
    where = ["created_at >= ?"]
    params: list[object] = [since.isoformat()]
    if topic_keys:
        placeholders = ",".join("?" for _ in topic_keys)
        where.append(f"topic_key IN ({placeholders})")  # noqa: S608
        params.extend(topic_keys)
    if label:
        where.append("label = ?")
        params.append(label)
    where_sql = " AND ".join(where)

    # where_sql is built only from fixed clause fragments above (never from
    # topic_keys' or label's *values*), so this is parameterized in spirit
    # even though the WHERE clause itself is assembled with an f-string.
    count_sql = f"SELECT COUNT(*) FROM stories WHERE {where_sql}"  # noqa: S608
    total = conn.execute(count_sql, params).fetchone()[0]

    select_cols = "SELECT id, topic_key, headline, summary, label, created_at, is_update_of "
    select_tail = f"FROM stories WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"  # noqa: S608
    rows = conn.execute(select_cols + select_tail, [*params, limit, offset]).fetchall()

    return _rows_to_story_views(conn, rows), total


def fts_escape(user_query: str) -> str:
    """Turn free-text user input into a query FTS5 will always accept.

    FTS5's query syntax has its own operators (`AND`, `NEAR`, `*`, `"`...),
    and a member typing `patch OR notes` doesn't mean "run an OR query",
    they mean those three words. We split on whitespace and wrap every
    token in double quotes (escaping any literal quote by doubling it,
    the same way SQL string literals do), so every token becomes a plain
    phrase match and none of FTS5's operators can sneak in.
    """
    tokens = _FTS_TOKEN_RE.findall(user_query)
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def search_stories(
    conn: sqlite3.Connection, query: str, since: datetime, limit: int, offset: int
) -> tuple[list[StoryView], int]:
    """Full-text search over story headlines and summaries, for `/news search`.

    Ranked by `bm25` (FTS5's relevance score, where lower is better) and
    then recency. An empty or all-whitespace query returns nothing rather
    than matching everything, since "search for nothing" isn't a query a
    member meant to run.
    """
    escaped = fts_escape(query)
    if not escaped:
        return [], 0

    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM stories_fts "
            "JOIN stories ON stories.id = stories_fts.rowid "
            "WHERE stories_fts MATCH ? AND stories.created_at >= ?",
            (escaped, since.isoformat()),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT stories.id, stories.topic_key, stories.headline, stories.summary, "
            "stories.label, stories.created_at, stories.is_update_of "
            "FROM stories_fts "
            "JOIN stories ON stories.id = stories_fts.rowid "
            "WHERE stories_fts MATCH ? AND stories.created_at >= ? "
            "ORDER BY bm25(stories_fts), stories.created_at DESC "
            "LIMIT ? OFFSET ?",
            (escaped, since.isoformat(), limit, offset),
        ).fetchall()
    except sqlite3.OperationalError:
        # fts_escape should make every query syntactically valid, but this
        # is cheap insurance against the one FTS5 quirk we didn't think of.
        return [], 0

    return _rows_to_story_views(conn, rows), total


def status_snapshot(
    conn: sqlite3.Connection, now: datetime, month_start: datetime
) -> StatusSnapshot:
    """Everything `/newsbot status` shows, gathered in one place."""
    digest_row = conn.execute(
        "SELECT id, run_date, status, posted_message_ids, error_notes "
        "FROM digests ORDER BY run_date DESC LIMIT 1"
    ).fetchone()
    last_digest = None
    if digest_row is not None:
        last_digest = DigestRow(
            id=digest_row["id"],
            run_date=date.fromisoformat(digest_row["run_date"]),
            status=digest_row["status"],
            posted_message_ids=json.loads(digest_row["posted_message_ids"]),
            error_notes=digest_row["error_notes"],
        )

    health_rows = conn.execute(
        "SELECT source_name, last_success_at, last_error_at, last_error, consecutive_failures "
        "FROM source_health ORDER BY source_name"
    ).fetchall()
    source_health = [
        SourceHealthRow(
            source_name=row["source_name"],
            last_success_at=(
                datetime.fromisoformat(row["last_success_at"]) if row["last_success_at"] else None
            ),
            last_error_at=(
                datetime.fromisoformat(row["last_error_at"]) if row["last_error_at"] else None
            ),
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"],
        )
        for row in health_rows
    ]

    since_24h = (now - _ONE_DAY).isoformat()
    items_last_24h = conn.execute(
        "SELECT COUNT(*) FROM items WHERE collected_at >= ?", (since_24h,)
    ).fetchone()[0]
    stories_last_24h = conn.execute(
        "SELECT COUNT(*) FROM stories WHERE created_at >= ?", (since_24h,)
    ).fetchone()[0]

    month_row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
        "FROM digests WHERE created_at >= ? AND created_at <= ?",
        (month_start.isoformat(), now.isoformat()),
    ).fetchone()

    return StatusSnapshot(
        last_digest=last_digest,
        source_health=source_health,
        items_last_24h=items_last_24h,
        stories_last_24h=stories_last_24h,
        month_input_tokens=month_row[0],
        month_output_tokens=month_row[1],
    )
