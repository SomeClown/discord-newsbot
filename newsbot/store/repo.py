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
    AlertState,
    AlertStatus,
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
      - a `pending` row already exists and `force` wasn't passed
        (something else is running, or crashed mid-run; either way, we
        don't want to overlap it without being asked to)
      - an `ok`/`partial` row exists and `force` wasn't passed
    A `failed` row always allows a reclaim (that day never actually posted).
    `force=True` against `pending`/`ok`/`partial` updates the existing row
    in place, keeping its id, rather than inserting a second row for the
    same date.

    `force` overriding `pending` (added for QA step 20, group 4) relies on
    `pipeline/run.py`'s in-process `_run_lock`, held for the whole guard
    dance, to already rule out two runs racing to claim the same date --
    the only way this layer ever sees a `pending` row at all is a crash
    that happened before `save_run`/`mark_digest_failed` got to run, which
    means it's always safe to force past.
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
        if status in ("pending", "ok", "partial") and not force:
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


# --- SHiFT code alerts (design.md §12) ---
#
# `alerted_codes` is the once-per-code-ever guard and `alert_state` is a
# key/value scratchpad for the handful of cross-sweep facts that don't
# deserve their own columns (the seeded marker, the last sweep's summary,
# today's ping count). Retention (`purge_older_than` above) never touches
# either table -- there's no lookback window on "have we ever alerted this
# code before".


def _set_alert_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO alert_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_alert_state(conn: sqlite3.Connection) -> AlertState:
    """Read `alert_state` into one dataclass. Missing keys read as `None`/`0`.

    `seeded` collapses the `seeded_at` timestamp into a bool -- nothing
    downstream cares *when* the marker was set, only whether it's there
    (see A1 in the plan: losing the marker silently re-seeds, which is
    the safe direction to fail in).
    """
    rows = conn.execute("SELECT key, value FROM alert_state").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    last_sweep_at = values.get("last_sweep_at")
    return AlertState(
        seeded=values.get("seeded_at") is not None,
        last_sweep_at=datetime.fromisoformat(last_sweep_at) if last_sweep_at else None,
        last_sweep_summary=values.get("last_sweep_summary"),
        ping_day=values.get("ping_day"),
        ping_count=int(values.get("ping_count") or 0),
    )


def known_codes(conn: sqlite3.Connection, codes: Iterable[str]) -> set[str]:
    """Return the subset of `codes` already in `alerted_codes`, any status."""
    code_list = list(codes)
    found: set[str] = set()
    for i in range(0, len(code_list), _SQLITE_VARIABLE_CHUNK):
        chunk = code_list[i : i + _SQLITE_VARIABLE_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        # placeholders is a string of literal "?"s sized to the chunk, never
        # interpolated user data; the actual values are bound below.
        query = f"SELECT code FROM alerted_codes WHERE code IN ({placeholders})"  # noqa: S608
        rows = conn.execute(query, chunk)
        found.update(row["code"] for row in rows)
    return found


def record_silent_codes(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, str]],
    *,
    now: Callable[[], datetime] | None = None,
    mark_seeded: bool,
) -> None:
    """Record codes without posting them: `(code, source_name, item_url, status)`.

    `status` is `'seeded'` (unseeded sweep, A1) or `'too_old'` (A11, a
    fresh-vs-stale call `shift/decide.py` already made). `ON CONFLICT DO
    NOTHING` because a code landing here twice across two sweeps should
    just stay however it was first recorded. `mark_seeded=True` sets the
    `seeded_at` marker -- but only if it isn't already set, since the
    marker means "the first sweep after enabling has run", not "the most
    recent healthy sweep ran".
    """
    now_iso = _resolve_now(now)
    with conn:
        for code, source_name, item_url, status in rows:
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(code) DO NOTHING",
                (code, now_iso, source_name, item_url, status),
            )
        if mark_seeded:
            conn.execute(
                "INSERT INTO alert_state (key, value) VALUES ('seeded_at', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (now_iso,),
            )


def claim_codes(
    conn: sqlite3.Connection,
    codes: list[tuple[str, str, str]],
    *,
    pinged: bool,
    local_day: str,
    now: Callable[[], datetime] | None = None,
    max_pings: int | None = None,
) -> bool:
    """Claim `codes` as `pending` and spend today's ping budget, in one transaction.

    `codes` is `(code, source_name, item_url)`. This is a plain `INSERT`,
    not `ON CONFLICT DO NOTHING` -- record-then-post (plan §1) depends on
    a code that's somehow already claimed aborting the *whole* claim,
    ping spend included, rather than silently claiming its siblings and
    leaving the budget half-spent for a code that never got recorded.
    `local_day` resets `ping_count` to 0 first if it doesn't match the
    stored `ping_day` (a new day in `cfg.digest.timezone`, not UTC
    midnight -- see A8), then spends one more if `pinged`.

    Runs inside an explicit `BEGIN IMMEDIATE`, not sqlite3's default
    deferred transaction -- it grabs SQLite's write lock before reading
    `alert_state`, so a second caller doing the same thing at the same
    moment (a sweep and a `/newsbot test-alert` both landing in the same
    second, step 7) blocks on `busy_timeout` and sees this call's
    committed count, instead of both readers computing "count < max_pings"
    from the same stale row and over-spending the budget between them.

    `max_pings`, when given, re-checks the cap against that up-to-date
    count: if `pinged` was asked for but the cap was already reached by
    the time this claim actually got the write lock, the claim still
    goes through, just without a ping (`actual_pinged` in the code below,
    also this function's return value) -- a caller uses that to decide
    whether to still render the message as pinging. `max_pings=None` (the
    default) skips the re-check and spends exactly what `pinged` asked
    for, unchanged from how this function worked before the cap re-check
    existed; every caller from before that keeps its exact prior
    behavior.

    An empty `codes` is a no-op -- nothing to claim means nothing to spend
    a ping on either, and a caller that got this far with `pinged=True`
    but no codes (shouldn't happen, but "shouldn't" isn't "can't") would
    otherwise burn a slot of today's budget for an alert that never posts.
    """
    if not codes:
        return False
    now_iso = _resolve_now(now)
    # sqlite3's own "begin a transaction on first DML" behavior only ever
    # issues a deferred BEGIN; to get an immediate write lock instead, the
    # module's automatic handling has to be turned off (isolation_level =
    # None -- autocommit) so this can issue "BEGIN IMMEDIATE" itself.
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = get_alert_state(conn)
        count = state.ping_count if state.ping_day == local_day else 0
        actual_pinged = pinged
        if max_pings is not None and pinged and count >= max_pings:
            actual_pinged = False
        if actual_pinged:
            count += 1
        _set_alert_state(conn, "ping_day", local_day)
        _set_alert_state(conn, "ping_count", str(count))
        for code, source_name, item_url in codes:
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, pinged, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (code, now_iso, source_name, item_url, int(actual_pinged)),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation
    return actual_pinged


def mark_codes_posted(
    conn: sqlite3.Connection, codes: list[str], *, message_id: int | None
) -> None:
    """Flip `codes` (already `pending`) to `posted`, all sharing one `message_id`.

    One call per Discord message -- a batch that split across several
    messages (`format.py`'s overflow handling) calls this once per
    message with that message's own id and its own slice of codes.
    """
    with conn:
        for code in codes:
            conn.execute(
                "UPDATE alerted_codes SET status = 'posted', message_id = ? WHERE code = ?",
                (message_id, code),
            )


def mark_codes_failed(conn: sqlite3.Connection, codes: list[str]) -> None:
    """Flip `codes` (already `pending`) to `failed` after a send that never landed."""
    with conn:
        for code in codes:
            conn.execute(
                "UPDATE alerted_codes SET status = 'failed' WHERE code = ?",
                (code,),
            )


def fail_pending_codes(conn: sqlite3.Connection) -> list[str]:
    """Flip every still-`pending` code to `failed`; return which ones changed.

    Called once at startup (R4): a `pending` row means a previous process
    claimed a code and the ping budget, then died before confirming the
    Discord send actually landed. Flipping it to `failed` here, rather
    than leaving it `pending` forever, is what lets a future sweep treat
    the code as already handled instead of retrying a post that might
    already be sitting in the channel.
    """
    with conn:
        rows = conn.execute("SELECT code FROM alerted_codes WHERE status = 'pending'").fetchall()
        codes = [row["code"] for row in rows]
        if codes:
            conn.execute("UPDATE alerted_codes SET status = 'failed' WHERE status = 'pending'")
    return codes


def record_sweep(
    conn: sqlite3.Connection, now: Callable[[], datetime] | None, summary: str
) -> None:
    """Record that a sweep ran and what it found, e.g. "17/19 sources ok, 1 new code"."""
    now_iso = _resolve_now(now)
    with conn:
        _set_alert_state(conn, "last_sweep_at", now_iso)
        _set_alert_state(conn, "last_sweep_summary", summary)


def alert_status(
    conn: sqlite3.Connection,
    today: str,
    *,
    enabled: bool,
    max_pings: int,
    test_command_enabled: bool = False,
) -> AlertStatus:
    """Everything `/newsbot status`'s SHiFT alerts field shows, in one place.

    `today` is the caller's `local_run_date` string (`cfg.digest.timezone`,
    A8) -- `pings_today` only counts `ping_count` when it was spent on
    that same local day; a stale `ping_day` from yesterday reads as 0
    without needing its own reset write. `test_command_enabled` is just
    `cfg.alerts.allow_test_command` passed through -- it's config, not
    anything stored, but it lives on this dataclass because it's the one
    place `render_status` already reads the rest of this from.
    """
    state = get_alert_state(conn)
    codes_alerted = conn.execute(
        "SELECT COUNT(*) FROM alerted_codes WHERE status = 'posted'"
    ).fetchone()[0]
    pings_today = state.ping_count if state.ping_day == today else 0
    return AlertStatus(
        enabled=enabled,
        seeded=state.seeded,
        last_sweep_at=state.last_sweep_at,
        last_sweep_summary=state.last_sweep_summary,
        codes_alerted=codes_alerted,
        pings_today=pings_today,
        max_pings=max_pings,
        test_command_enabled=test_command_enabled,
    )


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
    conn: sqlite3.Connection, now: datetime, month_start: datetime, configured_names: Iterable[str]
) -> StatusSnapshot:
    """Everything `/newsbot status` shows, gathered in one place.

    `configured_names` (from `config.configured_source_names`) is the
    source_health filter: a source dropped from config.yaml still has a
    row in this table forever (this function only reads; see the module
    docstring on why pruning isn't its job), but nobody wants it showing
    up in `/newsbot status` claiming to be unhealthy years later. A
    configured source with no row yet (just added, never run) still gets
    a place in the list, marked `never_run`, rather than silently missing
    from the count.
    """
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

    names = list(configured_names)
    health_rows: list[sqlite3.Row] = []
    if names:
        placeholders = ",".join("?" for _ in names)
        # placeholders is a string of literal "?"s sized to the configured
        # source list, never interpolated user data; the actual names are
        # bound below.
        select = "SELECT source_name, last_success_at, last_error_at, last_error, "
        select += "consecutive_failures FROM source_health "
        where = f"WHERE source_name IN ({placeholders})"  # noqa: S608
        health_rows = conn.execute(select + where, names).fetchall()

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
    seen_names = {row["source_name"] for row in health_rows}
    source_health.extend(
        SourceHealthRow(
            source_name=name,
            last_success_at=None,
            last_error_at=None,
            last_error=None,
            consecutive_failures=0,
            never_run=True,
        )
        for name in names
        if name not in seen_names
    )
    source_health.sort(key=lambda s: s.source_name)

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
