"""Every query the pipeline needs. No SQL anywhere else in the codebase.

Keeping the SQL in one file is a rule, not a preference: it means a
"what tables does `is_update_of` touch" question has exactly one file to
grep, and it means the parameterization discipline (nothing ever gets
string-formatted into a query) only has to be checked in one place.

The digest write path centers on a claim-then-publish-then-save guard
(SPEC-DEV 2): `claim_guild_digest` reserves a server's slot for the day with a
`pending` row *before* anything slow happens (summarizing, posting to Discord),
and the save after publishing records what actually posted. A digest that's
half-posted and half-recorded is the alternative (I've seen that movie, and it's
not a good one).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Literal, NamedTuple

from newsbot.guilds.schedule import MAX_ATTEMPTS, item_floor, lease_is_stale, retry_is_ready
from newsbot.store.db import StoreError
from newsbot.store.models import (
    AlertState,
    CodeView,
    CompedFollow,
    Coverage,
    DueCandidate,
    GameSummaryRow,
    GuildClaim,
    GuildDigestRow,
    GuildGame,
    GuildSettings,
    HeadlineItem,
    ItemRange,
    ItemView,
    LoungeSettings,
    LoungeState,
    Notice,
    PriorStory,
    QueuedCode,
    QuoteDeckState,
    ShiftSettings,
    SourceHealthRow,
    StoredItem,
    StoryToSave,
    StoryView,
    Tier,
    Usage,
)

# SQLite caps a single statement at 999 (or 32766 on newer builds, but we
# don't get to pick) bound parameters. Chunking here means callers never
# have to think about the limit or hit it in production with a big batch
# of collected URLs.
_SQLITE_VARIABLE_CHUNK = 900


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


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the body in one `BEGIN IMMEDIATE` transaction: take the write lock, then look.

    Read-modify-write on a digest row needs this. A plain `with conn:` starts
    its transaction at the first write, after the read, so two writers can both
    read the old value and the second one quietly undoes the first. (The
    claim has always done this; `record_posted_game` learned it from a test
    that lost 500 of 600 games.) Not reentrant: the connection must not
    already be in a transaction.
    """
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation


def _utc_iso(moment: datetime) -> str:
    """ISO string for `moment` in UTC, for comparing against stored timestamps.

    Every timestamp in the database is stored as UTC ISO text, and SQLite
    compares that text as text. That works exactly as long as both sides
    are UTC; hand it a Pacific-time boundary and "an hour later" can sort
    earlier. test-engineer proved it with a naive-datetime boundary in
    mind, so every time boundary goes through here first. (`/shift
    codes`'s own window is already a rolling UTC one: `now - days`, no
    timezone involved; it's only the *displayed* first-seen date that
    gets converted to the digest's timezone, in `format.py`, well after
    this function's job is done.) A naive datetime is taken to already be
    UTC, which is what the rest of this module assumes anyway.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def recent_headlines(conn: sqlite3.Connection, topic_key: str, since: datetime) -> list[PriorStory]:
    """Headlines for `topic_key` since `since`, newest first.

    Fed back to the summarizer so it can say "same as yesterday" instead of
    re-announcing a patch note for the third day running.
    """
    rows = conn.execute(
        "SELECT id, headline, created_at FROM stories "
        "WHERE topic_key = ? AND created_at >= ? ORDER BY created_at DESC",
        (topic_key, _utc_iso(since)),
    ).fetchall()
    return [
        PriorStory(
            id=row["id"],
            headline=row["headline"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


def _insert_items(
    conn: sqlite3.Connection, items: list[StoredItem], now_iso: str
) -> tuple[dict[str, int], int]:
    """Insert items and their game tags, returning url -> item id and the rows inserted.

    The count comes from each INSERT's own `rowcount` (1 if the row went in,
    0 if the url was already there), so it can't include anything another
    connection wrote in the meantime.

    No transaction of its own: `store_items` wraps it in one, and a nested
    commit would quietly break that atomicity.
    """
    url_to_id: dict[str, int] = {}
    inserted = 0
    for item in items:
        cursor = conn.execute(
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
        inserted += cursor.rowcount
        item_id = conn.execute("SELECT id FROM items WHERE url = ?", (item.url,)).fetchone()[0]
        url_to_id[item.url] = item_id
        for topic_key, uncertain in item.topics.items():
            conn.execute(
                "INSERT INTO item_topics (item_id, topic_key, uncertain) VALUES (?, ?, ?) "
                "ON CONFLICT(item_id, topic_key) DO NOTHING",
                (item_id, topic_key, int(uncertain)),
            )
    return url_to_id, inserted


def store_items(
    conn: sqlite3.Connection,
    items: list[StoredItem],
    now: Callable[[], datetime] | None = None,
) -> int:
    """Store collected items and their game tags, atomically. Returns how many were new.

    This is the hourly shared collection's write (design.md §15): items
    belong to everyone, and `item_topics` is the game tag. An item whose url
    is already stored keeps its row but can still gain a tag it didn't have.
    """
    now_iso = _resolve_now(now)
    with conn:
        _, inserted = _insert_items(conn, items, now_iso)
    return inserted


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
    cutoff_iso = _utc_iso(cutoff)
    with conn:
        items_deleted = conn.execute(
            "DELETE FROM items WHERE collected_at < ?", (cutoff_iso,)
        ).rowcount
        stories_deleted = conn.execute(
            "DELETE FROM stories WHERE created_at < ?", (cutoff_iso,)
        ).rowcount
    return items_deleted, stories_deleted


def purge_guild_data_older_than(conn: sqlite3.Connection, cutoff: datetime) -> tuple[int, int]:
    """Delete shared summaries and server notices older than `cutoff`.

    Returns (summaries_deleted, notices_deleted). The public app's half of
    retention, next to `purge_older_than`: the stories a summary owns are
    already gone by the same age (they're purged by `created_at` too), and any
    that aren't have their `summary_id` nulled by the foreign key, not orphaned.
    """
    cutoff_iso = _utc_iso(cutoff)
    with conn:
        summaries = conn.execute(
            "DELETE FROM game_summaries WHERE created_at < ?", (cutoff_iso,)
        ).rowcount
        notices = conn.execute(
            "DELETE FROM guild_notices WHERE created_at < ?", (cutoff_iso,)
        ).rowcount
    return summaries, notices


def recent_guild_digests(conn: sqlite3.Connection, since: datetime) -> list[tuple[str, str | None]]:
    """`(status, error notes)` for each server's newest digest row touched since `since`.

    One row per server, so a server isn't counted twice because two of its days
    straddle the window. Rows from v2.2 with no server (`guild_id` NULL) aren't
    anybody's and are left out. For the owner's daily report.
    """
    rows = conn.execute(
        "SELECT status, error_notes FROM digests WHERE id IN ("
        "SELECT MAX(id) FROM digests WHERE guild_id IS NOT NULL AND updated_at >= ? "
        "GROUP BY guild_id) ORDER BY guild_id",
        (_utc_iso(since),),
    ).fetchall()
    return [(row["status"], row["error_notes"]) for row in rows]


# --- SHiFT code alerts (design.md §12) ---
#
# `alerted_codes` is the once-per-code-ever guard and `alert_state` is a
# key/value scratchpad for the handful of cross-sweep facts that don't
# deserve their own columns (the seeded marker, the last sweep's summary,
# today's ping count). Retention (`purge_older_than` above) never touches
# either table: there's no lookback window on "have we ever alerted this
# code before".


def _set_alert_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO alert_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_alert_state(conn: sqlite3.Connection) -> AlertState:
    """Read `alert_state` into one dataclass. Missing keys read as `None`/`0`.

    `seeded` collapses the `seeded_at` timestamp into a bool: nothing
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
    rows: list[tuple[str, str, str, str, bool]],
    *,
    now: Callable[[], datetime] | None = None,
    mark_seeded: bool,
) -> None:
    """Record codes without posting them: `(code, source_name, item_url, status, from_roundup)`.

    `status` is `'seeded'` (unseeded sweep, A1), `'too_old'` (A11, a
    fresh-vs-stale call `shift/decide.py` already made), or `'roundup'`
    (QA item 7, owner decision 2026-09-25: a code whose every sighting
    came from an item naming more than `max_codes_per_item` distinct
    codes). `from_roundup` (migration 003) is `CodeCandidate.roundup`
    passed straight through: true exactly when `status == 'roundup'`
    today, but kept as its own column (not derived from `status`) because
    v2.0 (design.md §13) starts posting some roundup codes instead of
    silently recording them, at which point `status` alone can't carry
    the marker anymore. `ON CONFLICT DO NOTHING` because a code landing
    here twice across two sweeps should just stay however it was first
    recorded. `mark_seeded=True` sets the `seeded_at` marker, but only
    if it isn't already set, since the marker means "the first sweep
    after enabling has run", not "the most recent healthy sweep ran".
    """
    now_iso = _resolve_now(now)
    with conn:
        for code, source_name, item_url, status, from_roundup in rows:
            conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, status, from_roundup) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(code) DO NOTHING",
                (code, now_iso, source_name, item_url, status, int(from_roundup)),
            )
        if mark_seeded:
            conn.execute(
                "INSERT INTO alert_state (key, value) VALUES ('seeded_at', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (now_iso,),
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


def query_codes(
    conn: sqlite3.Connection, since: datetime, limit: int, offset: int
) -> tuple[list[CodeView], int]:
    """Known SHiFT codes for `/shift codes`, newest first.

    Excludes `'pending'` (still being claimed/posted, not confirmed yet)
    and `'failed'` (claimed but never actually landed in Discord): a
    member paging through known codes shouldn't see either half-state.
    Everything else (`'seeded'`, `'too_old'`, `'roundup'`, `'posted'`)
    is fair game; `format.render_code_page` is what turns `from_roundup`
    and `status` into the "from a roundup" / "old post" / "already
    around when alerts started" markers (D5). Ties in `first_seen_at`
    (plausible: a batch claimed together shares one timestamp) break on
    `rowid`, so paging never reorders rows between calls.
    """
    since_iso = _utc_iso(since)
    total = conn.execute(
        "SELECT COUNT(*) FROM alerted_codes "
        "WHERE status NOT IN ('pending', 'failed') AND first_seen_at >= ?",
        (since_iso,),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT code, first_seen_at, source_name, item_url, status, from_roundup "
        "FROM alerted_codes "
        "WHERE status NOT IN ('pending', 'failed') AND first_seen_at >= ? "
        "ORDER BY first_seen_at DESC, rowid ASC "
        "LIMIT ? OFFSET ?",
        (since_iso, limit, offset),
    ).fetchall()
    return [
        CodeView(
            code=row["code"],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            source_name=row["source_name"],
            item_url=row["item_url"],
            status=row["status"],
            from_roundup=bool(row["from_roundup"]),
        )
        for row in rows
    ], total


# --- Lounge (design.md §14) ---
#
# `lounge_quotes_used` is the per-source no-repeat deck and `lounge_state`
# holds the once-a-day guard. Like the SHiFT tables above, retention
# (`purge_older_than`) never touches either: a quote used a year ago is
# still used.


def get_lounge_state(conn: sqlite3.Connection) -> LoungeState:
    """Read `lounge_state`. A missing `last_quote_date` reads as `None`."""
    row = conn.execute("SELECT value FROM lounge_state WHERE key = 'last_quote_date'").fetchone()
    return LoungeState(last_quote_date=row["value"] if row else None)


def quote_deck_state(
    conn: sqlite3.Connection, source_key: str, guild_id: int | None = None
) -> QuoteDeckState:
    """Return the hashes `source_key` has used and the most recent one.

    "Most recent" is the greatest `used_at`, with `rowid` breaking a tie
    (two claims inside the same clock tick, which only a test with a frozen
    clock ever manages).

    With `guild_id`, only that server's rows count, plus rows no server has
    claimed yet (`guild_id IS NULL`: v2.2's history, until the import stamps
    it). Without one, every row counts, which is v2's single-lounge reading.
    """
    if guild_id is None:
        rows = conn.execute(
            "SELECT quote_hash FROM lounge_quotes_used WHERE source_key = ? "
            "ORDER BY used_at DESC, rowid DESC",
            (source_key,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT quote_hash FROM lounge_quotes_used WHERE source_key = ? "
            "AND (guild_id = ? OR guild_id IS NULL) ORDER BY used_at DESC, rowid DESC",
            (source_key, guild_id),
        ).fetchall()
    hashes = [row["quote_hash"] for row in rows]
    return QuoteDeckState(used=frozenset(hashes), last_hash=hashes[0] if hashes else None)


def _is_imported_guild(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True if `guild_id` is the guild the v2 import recorded (`imported_at` set)."""
    row = conn.execute("SELECT guild_id FROM guilds WHERE imported_at IS NOT NULL").fetchone()
    return row is not None and row["guild_id"] == guild_id


def claim_quote(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    quote_hash: str,
    local_day: str,
    reshuffle: bool,
    force: bool,
    now: Callable[[], datetime] | None = None,
    guild_id: int | None = None,
) -> bool:
    """Record today's quote as used, or say no. True means the caller won and should post.

    Record-then-post, same shape as `claim_guild_codes`: this runs before the
    Discord call, so a post that fails leaves the quote used and the day
    done (one admin alert, no retry).

    `local_day` is the date in `cfg.digest.timezone` as ISO text. Unless
    `force`, a `local_day` equal to or earlier than `last_quote_date` refuses
    and writes nothing. `force` is `/newsbot quote-now` after its
    confirmation; it skips the guard entirely, and (unchanged) it still
    stores its `local_day` as `last_quote_date`, even an earlier one.

    `reshuffle` deletes only `source_key`'s rows first, so the deck starts
    over without touching any other source's.

    With `guild_id` (design.md §15) the same rules run per server: the date
    guard is that server's `guild_lounge.last_quote_date`, the reshuffle
    deletes only that server's rows (and the unclaimed `guild_id IS NULL`
    ones), and the new row is stamped with the server. Usage is per server
    (migration 005): two servers on one source each keep their own rows, so
    one claiming a hash never touches the other's. `lounge_state` is written
    too, so a rollback to v2.2 finds a current date, but only when `guild_id`
    is the imported guild: v2.2 has exactly one lounge (the friend's), and
    its date is the one a rolled-back process must read. Every other server
    leaving that cell alone keeps it from jumping around. A server
    with no lounge row (it was removed while the quote was loading) gets
    False and nothing is written.

    Runs inside `BEGIN IMMEDIATE` for the reason `claim_guild_codes` does: the
    scheduled job and `quote-now` can land in the same second, and the
    loser has to wait for the winner's committed date instead of both
    reading the same stale one and both posting.
    """
    # Through _utc_iso, not the caller's raw isoformat(): used_at is sorted as
    # text, and a non-UTC offset sorts by wall clock instead of by instant.
    now_iso = _utc_iso((now or (lambda: datetime.now(UTC)))())
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        # ISO dates sort correctly as text, so `<=` covers "already done today"
        # and "the clock jumped backwards" in one comparison. Without the
        # second half, a backwards step let an earlier day through and the
        # day after that let today through again, which is two quotes for one
        # sunrise.
        if guild_id is None:
            last_day = get_lounge_state(conn).last_quote_date
        else:
            row = conn.execute(
                "SELECT last_quote_date FROM guild_lounge WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            last_day = row["last_quote_date"]
        if not force and last_day is not None and local_day <= last_day:
            conn.execute("ROLLBACK")
            return False
        if reshuffle and guild_id is None:
            conn.execute("DELETE FROM lounge_quotes_used WHERE source_key = ?", (source_key,))
        elif reshuffle:
            conn.execute(
                "DELETE FROM lounge_quotes_used WHERE source_key = ? "
                "AND (guild_id = ? OR guild_id IS NULL)",
                (source_key, guild_id),
            )
        # Delete-then-insert rather than an upsert: an upsert keeps the old
        # rowid, so a re-claimed hash inside one clock tick would still rank
        # as older than rows inserted after it. A fresh insert gets a fresh rowid.
        # With a guild, the delete is that guild's copy (and an unstamped one,
        # which the insert supersedes); another guild's row is none of our business.
        if guild_id is None:
            conn.execute(
                "DELETE FROM lounge_quotes_used WHERE source_key = ? AND quote_hash = ?",
                (source_key, quote_hash),
            )
        else:
            conn.execute(
                "DELETE FROM lounge_quotes_used WHERE source_key = ? AND quote_hash = ? "
                "AND (guild_id = ? OR guild_id IS NULL)",
                (source_key, quote_hash, guild_id),
            )
        conn.execute(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at, guild_id) "
            "VALUES (?, ?, ?, ?)",
            (source_key, quote_hash, now_iso, guild_id),
        )
        if guild_id is not None:
            conn.execute(
                "UPDATE guild_lounge SET last_quote_date = ? WHERE guild_id = ?",
                (local_day, guild_id),
            )
        if guild_id is None or _is_imported_guild(conn, guild_id):
            conn.execute(
                "INSERT INTO lounge_state (key, value) VALUES ('last_quote_date', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (local_day,),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation
    return True


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
    """Stories for `/news recent`, newest first. An empty `topic_keys` means "All".

    `limit` is clamped to at least 1 and `offset` to at least 0 (SQLite reads
    `LIMIT -1` as "no limit", which is not what anyone meant by -1).
    """
    limit, offset = max(limit, 1), max(offset, 0)
    where = ["created_at >= ?"]
    params: list[object] = [_utc_iso(since)]
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
    conn: sqlite3.Connection,
    query: str,
    since: datetime,
    limit: int,
    offset: int,
    *,
    topic_keys: list[str] | None = None,
) -> tuple[list[StoryView], int]:
    """Full-text search over story headlines and summaries, for `/news search`.

    Ranked by `bm25` (FTS5's relevance score, where lower is better) and
    then recency. An empty or all-whitespace query returns nothing rather
    than matching everything, since "search for nothing" isn't a query a
    member meant to run.

    `topic_keys` limits the search to those games (a public server only sees
    the games it follows). `None` means every game, as v2 had it; an empty list
    means *no* game and returns nothing. That's the opposite of
    `query_stories`, where an empty list means "All", and I did it on purpose:
    for a server that follows nothing, the safe misreading is an empty result.
    """
    escaped = fts_escape(query)
    if not escaped or topic_keys == []:
        return [], 0

    topic_sql, topic_params = "", []
    if topic_keys is not None:
        topic_sql = f" AND stories.topic_key IN ({','.join('?' for _ in topic_keys)})"
        topic_params = list(topic_keys)

    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM stories_fts "  # noqa: S608
            "JOIN stories ON stories.id = stories_fts.rowid "
            f"WHERE stories_fts MATCH ? AND stories.created_at >= ?{topic_sql}",
            (escaped, _utc_iso(since), *topic_params),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT stories.id, stories.topic_key, stories.headline, stories.summary, "  # noqa: S608
            "stories.label, stories.created_at, stories.is_update_of "
            "FROM stories_fts "
            "JOIN stories ON stories.id = stories_fts.rowid "
            f"WHERE stories_fts MATCH ? AND stories.created_at >= ?{topic_sql} "
            "ORDER BY bm25(stories_fts), stories.created_at DESC "
            "LIMIT ? OFFSET ?",
            (escaped, _utc_iso(since), *topic_params, limit, offset),
        ).fetchall()
    except sqlite3.OperationalError, UnicodeEncodeError:
        # fts_escape should make every query syntactically valid, but this
        # is cheap insurance against the one FTS5 quirk we didn't think of.
        # (The second one: a lone surrogate, which Python will happily hold
        # in a str and SQLite's UTF-8 binding will not. JSON escapes can
        # produce one, so a member can too.)
        return [], 0

    return _rows_to_story_views(conn, rows), total


# --- Guilds (design.md §15) ---
#
# Everything a server owns lives in the tables migration 005 added, and every
# function below takes a `guild_id` and only ever touches that guild's rows.
# That's the whole cross-guild-bleed defense at this layer: there is no query
# here that can be asked about "all guilds' games" by accident. (The two that
# do span guilds, `list_set_up_guilds` and `list_lounges`, say so in their names.)
#
# The old single-server functions above are untouched on purpose: v2.2's rollback
# compatibility and their guard tests lean on them, even though nothing in the
# running bot calls the digest ones any more.

MAX_GAMES_PER_GUILD = 10
_MAX_NOTICES_PER_GUILD = 20
_MAX_NOTICE_LENGTH = 2000

_UNSET: object = object()


class GameLimitError(StoreError):
    """Raised when a guild would end up following more than 10 games."""


class StaleSetupError(StoreError):
    """Raised when a server's settings changed after the setup wizard took its snapshot."""


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _guild_from_row(row: sqlite3.Row) -> GuildSettings:
    return GuildSettings(
        guild_id=row["guild_id"],
        digest_time=row["digest_time"],
        timezone=row["timezone"],
        admin_channel_id=row["admin_channel_id"],
        tier=row["tier"],
        set_up=bool(row["set_up"]),
        joined_at=datetime.fromisoformat(row["joined_at"]),
        imported_at=_parse_dt(row["imported_at"]),
        permission_problems=row["permission_problems"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


_GUILD_COLUMNS = (
    "guild_id, digest_time, timezone, admin_channel_id, tier, set_up, "
    "joined_at, imported_at, permission_problems, updated_at"
)


def create_guild(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    digest_time: str = "09:00",
    timezone: str = "UTC",
    admin_channel_id: int | None = None,
    tier: Tier = "free",
    set_up: bool = False,
    imported_at: datetime | None = None,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Insert a `guilds` row. True if it's new, False if the guild already existed.

    A duplicate join event (Discord does resend them) just returns False and
    leaves the existing row, and its settings, alone.
    """
    now_iso = _resolve_now(now)
    with conn:
        cur = conn.execute(
            "INSERT INTO guilds (guild_id, digest_time, timezone, admin_channel_id, tier, "
            "set_up, joined_at, imported_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO NOTHING",
            (
                guild_id,
                digest_time,
                timezone,
                admin_channel_id,
                tier,
                int(set_up),
                now_iso,
                _utc_iso(imported_at) if imported_at else None,
                now_iso,
            ),
        )
    return cur.rowcount == 1


def get_guild(conn: sqlite3.Connection, guild_id: int) -> GuildSettings | None:
    row = conn.execute(
        f"SELECT {_GUILD_COLUMNS} FROM guilds WHERE guild_id = ?",  # noqa: S608
        (guild_id,),
    ).fetchone()
    return _guild_from_row(row) if row else None


def guild_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts across every server, for the owner's `/owner servers`. Numbers only, no names.

    Keys: `servers`, `set_up`, `free`, `comped`, `shift_enabled` and `permission_problems`
    (servers whose last permission check left a note).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS servers, "
        "COALESCE(SUM(set_up = 1), 0) AS set_up, "
        "COALESCE(SUM(tier = 'free'), 0) AS free, "
        "COALESCE(SUM(tier = 'comped'), 0) AS comped, "
        "COALESCE(SUM(permission_problems IS NOT NULL AND permission_problems != ''), 0) "
        "AS permission_problems FROM guilds"
    ).fetchone()
    counts = {key: row[key] for key in row.keys()}  # noqa: SIM118 (sqlite3.Row isn't a mapping)
    counts["shift_enabled"] = conn.execute(
        "SELECT COUNT(*) FROM guild_shift WHERE enabled = 1"
    ).fetchone()[0]
    return counts


def list_guilds(conn: sqlite3.Connection) -> list[GuildSettings]:
    """Every guild row, in guild id order (startup reconciliation reads this)."""
    rows = conn.execute(
        f"SELECT {_GUILD_COLUMNS} FROM guilds ORDER BY guild_id"  # noqa: S608
    ).fetchall()
    return [_guild_from_row(row) for row in rows]


def list_set_up_guilds(conn: sqlite3.Connection) -> list[GuildSettings]:
    """Every guild that finished `/newsbot setup`, in guild id order."""
    rows = conn.execute(
        f"SELECT {_GUILD_COLUMNS} FROM guilds WHERE set_up = 1 ORDER BY guild_id"  # noqa: S608
    ).fetchall()
    return [_guild_from_row(row) for row in rows]


def update_guild_settings(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    digest_time: str | object = _UNSET,
    timezone: str | object = _UNSET,
    admin_channel_id: int | None | object = _UNSET,
    tier: Tier | object = _UNSET,
    set_up: bool | object = _UNSET,
    permission_problems: str | None | object = _UNSET,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Change the fields passed, leave the rest. True if the guild exists.

    Anything not passed stays as it was; passing `None` for `admin_channel_id`
    or `permission_problems` clears it (that's why "not passed" is a sentinel
    and not `None`). The column names below are a fixed list; only values
    are ever bound from the caller.
    """
    changes: list[tuple[str, object]] = []
    if digest_time is not _UNSET:
        changes.append(("digest_time", digest_time))
    if timezone is not _UNSET:
        changes.append(("timezone", timezone))
    if admin_channel_id is not _UNSET:
        changes.append(("admin_channel_id", admin_channel_id))
    if tier is not _UNSET:
        changes.append(("tier", tier))
    if set_up is not _UNSET:
        changes.append(("set_up", int(bool(set_up))))
    if permission_problems is not _UNSET:
        changes.append(("permission_problems", permission_problems))
    assignments = "".join(f"{column} = ?, " for column, _ in changes)
    params = [value for _, value in changes]
    with conn:
        cur = conn.execute(
            f"UPDATE guilds SET {assignments}updated_at = ? WHERE guild_id = ?",  # noqa: S608
            [*params, _resolve_now(now), guild_id],
        )
    return cur.rowcount == 1


# `app_state` keys that belong to one server, named here so the code that writes them and
# `delete_guild` (which clears them) can't drift apart.
STUCK_V22_KEY_PREFIX = "stuck_v22_digest:"


def guild_crash_key(guild_id: int) -> str:
    return f"guild_digest_crash:{guild_id}"


def delete_guild(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Delete a guild and, through ON DELETE CASCADE, everything it owns.

    Shared data stays: items, alerted_codes, and any story a deleted digest
    pointed at (its `digest_id` just goes NULL). Needs foreign keys on,
    which `connect()` always does.

    `app_state` has no foreign key to hang a cascade on, so the per-server
    bookkeeping keys (the digest crash backoff, the "told them about the stuck
    v2.2 digest" markers) are deleted here by name, in the same transaction.
    A server that leaves shouldn't leave its id behind in a table nobody
    reads. (The import report stays: it's a record of what the import did, not
    a setting of anyone's.)
    """
    with conn:
        cur = conn.execute("DELETE FROM guilds WHERE guild_id = ?", (guild_id,))
        stuck = f"{STUCK_V22_KEY_PREFIX}{guild_id}:"
        conn.execute(
            "DELETE FROM app_state WHERE key = ? OR substr(key, 1, ?) = ?",
            (guild_crash_key(guild_id), len(stuck), stuck),
        )
    return cur.rowcount == 1


def list_guild_games(conn: sqlite3.Connection, guild_id: int) -> list[GuildGame]:
    """The games `guild_id` follows, in the order they were added."""
    rows = conn.execute(
        "SELECT guild_id, game_key, channel_id FROM guild_games WHERE guild_id = ? ORDER BY rowid",
        (guild_id,),
    ).fetchall()
    return [GuildGame(r["guild_id"], r["game_key"], r["channel_id"]) for r in rows]


def set_guild_games(conn: sqlite3.Connection, guild_id: int, games: list[tuple[str, int]]) -> None:
    """Replace `guild_id`'s followed games with `games`, `(game_key, channel_id)` each.

    All or nothing: more than 10 raises `GameLimitError` and a repeated key
    raises `ValueError`, both before anything is written.
    """
    if len(games) > MAX_GAMES_PER_GUILD:
        raise GameLimitError(f"At most {MAX_GAMES_PER_GUILD} games per server.")
    keys = [key for key, _ in games]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate game key.")
    with conn:
        conn.execute("DELETE FROM guild_games WHERE guild_id = ?", (guild_id,))
        conn.executemany(
            "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)",
            [(guild_id, key, channel_id) for key, channel_id in games],
        )


def setup_fingerprint(guild: GuildSettings | None, games: Sequence[GuildGame]) -> str:
    """A stable digest of what `/newsbot setup` edits: the time, the zone and the followed games.

    The wizard takes one when it opens and `apply_guild_setup` takes another inside the
    write's transaction; if they differ, somebody else got there first. Games are sorted,
    so the order they were added in doesn't matter, but their channels do. A missing row
    has a fingerprint too (it just never matches a real one). `updated_at` would be
    shorter, but `follow` and `unfollow` don't touch it, which I found out by reading
    them rather than by trusting the column name.
    """
    snapshot = [
        None if guild is None else [guild.digest_time, guild.timezone],
        sorted([g.game_key, g.channel_id] for g in games),
    ]
    return hashlib.sha256(json.dumps(snapshot).encode()).hexdigest()


def apply_guild_setup(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    digest_time: str,
    timezone: str,
    games: list[tuple[str, int]],
    tier: Tier = "free",
    expected_fingerprint: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> None:
    """What `/newsbot setup` saves, in one transaction: the row, the schedule and the games.

    Creates the guild's row if a missed join left it without one (an existing row keeps
    its tier, so a comped server stays comped; passing `tier="comped"` upgrades a free
    row, and nothing ever downgrades), sets the time and zone and `set_up = 1`,
    drops every followed game not in `games`, and points the rest at their channels,
    following any that are new. Unlike `set_guild_games` this doesn't wipe and refill, so
    a game that stays keeps its place in the order it was added. Any failure rolls the
    lot back: more than 10 games raises `GameLimitError` and a repeated key raises
    `ValueError`, both before anything is written.

    With `expected_fingerprint` (from `setup_fingerprint`) the write is compare-and-set:
    the transaction takes the write lock first (`BEGIN IMMEDIATE`), then re-reads the
    server's settings, and raises `StaleSetupError` without writing if they no longer
    match. Nobody can change anything between the check and the write, because nobody
    else can hold the lock in between.
    """
    if len(games) > MAX_GAMES_PER_GUILD:
        raise GameLimitError(f"At most {MAX_GAMES_PER_GUILD} games per server.")
    keys = [key for key, _ in games]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate game key.")
    now_iso = _resolve_now(now)
    try:
        with conn:
            # Python would otherwise start the transaction at the first write, after the
            # fingerprint read. Starting it ourselves takes the lock before we look.
            conn.execute("BEGIN IMMEDIATE")
            if expected_fingerprint is not None:
                current = setup_fingerprint(
                    get_guild(conn, guild_id), list_guild_games(conn, guild_id)
                )
                if current != expected_fingerprint:
                    raise StaleSetupError("This server's settings changed after setup opened.")
            conn.execute(
                "INSERT INTO guilds (guild_id, digest_time, timezone, tier, set_up, joined_at, "
                "updated_at) VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT(guild_id) DO NOTHING",
                (guild_id, digest_time, timezone, tier, now_iso, now_iso),
            )
            conn.execute(
                "UPDATE guilds SET digest_time = ?, timezone = ?, set_up = 1, updated_at = ? "
                "WHERE guild_id = ?",
                (digest_time, timezone, now_iso, guild_id),
            )
            if tier == "comped":
                conn.execute("UPDATE guilds SET tier = 'comped' WHERE guild_id = ?", (guild_id,))
            marks = ", ".join("?" for _ in keys)
            conn.execute(
                f"DELETE FROM guild_games WHERE guild_id = ? AND game_key NOT IN ({marks})",  # noqa: S608
                (guild_id, *keys),
            )
            for key, channel_id in games:
                cur = conn.execute(
                    "UPDATE guild_games SET channel_id = ? WHERE guild_id = ? AND game_key = ?",
                    (channel_id, guild_id, key),
                )
                if not cur.rowcount:
                    conn.execute(
                        "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)",
                        (guild_id, key, channel_id),
                    )
    except sqlite3.IntegrityError as exc:
        if "at most 10 games" in str(exc):
            raise GameLimitError(f"At most {MAX_GAMES_PER_GUILD} games per server.") from exc
        raise


def follow_game(conn: sqlite3.Connection, guild_id: int, game_key: str, channel_id: int) -> bool:
    """Follow `game_key` in `channel_id`. True if newly followed, False if it just moved channels.

    Update-then-insert rather than an upsert on purpose: the 10-game trigger
    fires on every INSERT attempt, including one that would only have
    turned into an update, so an upsert would refuse to re-point the tenth
    game's channel. Raises `GameLimitError` on an eleventh.
    """
    try:
        with conn:
            cur = conn.execute(
                "UPDATE guild_games SET channel_id = ? WHERE guild_id = ? AND game_key = ?",
                (channel_id, guild_id, game_key),
            )
            if cur.rowcount:
                return False
            conn.execute(
                "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)",
                (guild_id, game_key, channel_id),
            )
            return True
    except sqlite3.IntegrityError as exc:
        if "at most 10 games" in str(exc):
            raise GameLimitError(f"At most {MAX_GAMES_PER_GUILD} games per server.") from exc
        raise


def unfollow_game(conn: sqlite3.Connection, guild_id: int, game_key: str) -> bool:
    """Stop following `game_key`. True if it was being followed."""
    with conn:
        cur = conn.execute(
            "DELETE FROM guild_games WHERE guild_id = ? AND game_key = ?", (guild_id, game_key)
        )
    return cur.rowcount == 1


def get_shift(conn: sqlite3.Connection, guild_id: int) -> ShiftSettings | None:
    row = conn.execute(
        "SELECT guild_id, enabled, channel_id, ping, enabled_at, ping_day, ping_count "
        "FROM guild_shift WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is None:
        return None
    return ShiftSettings(
        guild_id=row["guild_id"],
        enabled=bool(row["enabled"]),
        channel_id=row["channel_id"],
        ping=row["ping"],
        enabled_at=_parse_dt(row["enabled_at"]),
        ping_day=row["ping_day"],
        ping_count=row["ping_count"],
    )


def set_shift(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    enabled: bool,
    channel_id: int | None,
    ping: str,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Create or change a server's SHiFT settings, leaving its ping budget alone.

    `enabled_at` is stamped only on the off-to-on transition (or a first
    insert that's already on): "enabling starts from codes found after that
    moment", so re-saving the same settings must not move it. The table's
    CHECKs reject an enabled row with no channel and a malformed `ping`.
    """
    now_iso = _resolve_now(now)
    with conn:
        conn.execute(
            "INSERT INTO guild_shift (guild_id, enabled, channel_id, ping, enabled_at) "
            "VALUES (?, ?, ?, ?, CASE WHEN ? THEN ? END) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "enabled_at = CASE WHEN excluded.enabled = 1 AND guild_shift.enabled = 0 "
            "THEN excluded.enabled_at ELSE guild_shift.enabled_at END, "
            "enabled = excluded.enabled, channel_id = excluded.channel_id, ping = excluded.ping",
            (guild_id, int(enabled), channel_id, ping, int(enabled), now_iso),
        )


def _lounge_from_row(row: sqlite3.Row) -> LoungeSettings:
    return LoungeSettings(
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        welcome_enabled=bool(row["welcome_enabled"]),
        welcome_message=row["welcome_message"],
        quote_enabled=bool(row["quote_enabled"]),
        quote_time=row["quote_time"],
        quote_sources=json.loads(row["quote_sources"]),
        last_quote_date=row["last_quote_date"],
    )


_LOUNGE_COLUMNS = (
    "guild_id, channel_id, welcome_enabled, welcome_message, quote_enabled, "
    "quote_time, quote_sources, last_quote_date"
)


def get_lounge(conn: sqlite3.Connection, guild_id: int) -> LoungeSettings | None:
    row = conn.execute(
        f"SELECT {_LOUNGE_COLUMNS} FROM guild_lounge WHERE guild_id = ?",  # noqa: S608
        (guild_id,),
    ).fetchone()
    return _lounge_from_row(row) if row else None


def list_lounges(conn: sqlite3.Connection) -> list[LoungeSettings]:
    """Every guild's lounge row, in guild id order (loaded once at startup)."""
    rows = conn.execute(
        f"SELECT {_LOUNGE_COLUMNS} FROM guild_lounge ORDER BY guild_id"  # noqa: S608
    ).fetchall()
    return [_lounge_from_row(row) for row in rows]


def upsert_lounge(conn: sqlite3.Connection, lounge: LoungeSettings) -> None:
    """Create or replace a server's lounge settings.

    On an existing row, `last_quote_date` is deliberately not overwritten:
    re-syncing settings must not let today's quote go out twice. It's only
    taken from `lounge` when the row is new (the import carries it over).
    """
    with conn:
        conn.execute(
            "INSERT INTO guild_lounge (guild_id, channel_id, welcome_enabled, welcome_message, "
            "quote_enabled, quote_time, quote_sources, last_quote_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, "
            "welcome_enabled = excluded.welcome_enabled, "
            "welcome_message = excluded.welcome_message, "
            "quote_enabled = excluded.quote_enabled, quote_time = excluded.quote_time, "
            "quote_sources = excluded.quote_sources",
            (
                lounge.guild_id,
                lounge.channel_id,
                int(lounge.welcome_enabled),
                lounge.welcome_message,
                int(lounge.quote_enabled),
                lounge.quote_time,
                json.dumps(lounge.quote_sources),
                lounge.last_quote_date,
            ),
        )


def advance_lounge_quote_date(conn: sqlite3.Connection, guild_id: int, day: str | None) -> bool:
    """Move a lounge row's `last_quote_date` forward to `day`, never back. True if it moved.

    `upsert_lounge` leaves an existing row's date alone on purpose; this is the one
    deliberate way to push it later (the startup re-sync, when v2.2 has posted a quote the
    row hasn't heard about). ISO dates compare correctly as text.
    """
    if day is None:
        return False
    with conn:
        cur = conn.execute(
            "UPDATE guild_lounge SET last_quote_date = ? WHERE guild_id = ? "
            "AND (last_quote_date IS NULL OR last_quote_date < ?)",
            (day, guild_id, day),
        )
    return cur.rowcount == 1


def add_notice(
    conn: sqlite3.Connection,
    guild_id: int,
    text: str,
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Record a problem note for `guild_id`, keeping only its newest 20.

    Text over 2000 characters is cut to fit the column's limit (a permission
    error listing every channel shouldn't be the thing that fails). Empty
    text raises `ValueError`, and so does text that's only whitespace and NULs.
    NULs are stripped first: SQLite's `length()` stops counting at the first
    one, so the column's CHECK would otherwise call "\x00oops" empty.
    """
    text = text.replace("\x00", "")
    if not text.strip():
        raise ValueError("A notice needs some text.")
    text = text[:_MAX_NOTICE_LENGTH]
    with conn:
        conn.execute(
            "INSERT INTO guild_notices (guild_id, created_at, text) VALUES (?, ?, ?)",
            (guild_id, _resolve_now(now), text),
        )
        conn.execute(
            "DELETE FROM guild_notices WHERE guild_id = ? AND id NOT IN ("
            "SELECT id FROM guild_notices WHERE guild_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?)",
            (guild_id, guild_id, _MAX_NOTICES_PER_GUILD),
        )


def recent_notices(conn: sqlite3.Connection, guild_id: int, limit: int = 20) -> list[Notice]:
    """`guild_id`'s problem notes, newest first."""
    rows = conn.execute(
        "SELECT id, guild_id, created_at, text FROM guild_notices "
        "WHERE guild_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [
        Notice(r["id"], r["guild_id"], datetime.fromisoformat(r["created_at"]), r["text"])
        for r in rows
    ]


def failing_sources(conn: sqlite3.Connection, min_failures: int = 1) -> list[SourceHealthRow]:
    """Every source with at least `min_failures` failed collections in a row, worst first.

    Ties sort by casefolded name, like v2's status view. That's done here
    and not in SQL because SQLite's `LOWER` only knows ASCII.

    For the owner's daily report (D8): the alert fires once at 12, but the
    report lists everything that's currently failing, at any count.
    """
    rows = conn.execute(
        "SELECT source_name, last_success_at, last_error_at, last_error, consecutive_failures "
        "FROM source_health WHERE consecutive_failures >= ? "
        "ORDER BY consecutive_failures DESC, source_name",
        (max(1, min_failures),),
    ).fetchall()
    rows.sort(key=lambda r: (-r["consecutive_failures"], r["source_name"].casefold()))
    return [
        SourceHealthRow(
            source_name=r["source_name"],
            last_success_at=_parse_dt(r["last_success_at"]),
            last_error_at=_parse_dt(r["last_error_at"]),
            last_error=r["last_error"],
            consecutive_failures=r["consecutive_failures"],
        )
        for r in rows
    ]


def app_state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def app_state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def app_state_delete(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))


# v2.2 collected inside its own digest run, so a digest it posted has no window of its own:
# the items it used were stamped a few seconds after its due time, and a first v3 window of
# "due minus 24 hours" would hand every one of them to the model (and the server) a second time.
# What the data supports is that the digest was finished at `updated_at`, so that's its cutoff.
# Only `ok` and `partial` rows get one: a v2.2 `pending` row with no window is how the schedule
# knows nobody can say what it posted, and that has to stay true. (`julianday` is NULL for a
# timestamp that isn't one, which keeps a hand-edited row from breaking the window math later.)
_STAMP_V22_WINDOWS = (
    "UPDATE digests SET window_start = created_at, window_end = updated_at "
    "WHERE guild_id = ? AND window_end IS NULL AND status IN ('ok', 'partial') "
    "AND julianday(created_at) IS NOT NULL AND julianday(updated_at) IS NOT NULL"
)


def adopt_orphan_digests(conn: sqlite3.Connection, guild_id: int) -> int:
    """Give v2.2-written digest rows (NULL `guild_id`) to the imported guild.

    This is what closes the rollback-then-roll-forward double post: v2.2
    inserts today's digest with no guild, and without this v3 wouldn't see
    it as `guild_id`'s. Does nothing unless `guild_id` is the guild the
    import created (`imported_at` set). `UPDATE OR IGNORE` skips a row
    that would collide with a digest the guild already has for that day.
    Returns how many rows it adopted. The posted ones get a window ending where the
    v2.2 digest finished (`_STAMP_V22_WINDOWS`), so the next v3 window starts after it.
    """
    with conn:
        cur = conn.execute(
            "UPDATE OR IGNORE digests SET guild_id = ? WHERE guild_id IS NULL "
            "AND ? = (SELECT guild_id FROM guilds WHERE imported_at IS NOT NULL)",
            (guild_id, guild_id),
        )
        conn.execute(_STAMP_V22_WINDOWS, (guild_id,))
    return cur.rowcount


# The three backfills below belong to the one-time import (newsbot/guilds/
# importer.py). Unlike everything else in this file they do NOT open their own
# `with conn:` block: a `with conn:` commits, and the import's whole point is
# that nothing commits until every section has been written. The caller owns
# the transaction.


def adopt_orphan_digests_in_tx(conn: sqlite3.Connection, guild_id: int) -> int:
    """Hand every NULL-guild digest row to `guild_id`, inside the caller's transaction.

    Same idea as `adopt_orphan_digests`, minus the commit and minus the
    "is this the imported guild" subquery: the import runs this on an empty
    `guilds` table, where every digest row is an orphan by definition.
    Returns the number of rows adopted.
    """
    cur = conn.execute("UPDATE digests SET guild_id = ? WHERE guild_id IS NULL", (guild_id,))
    conn.execute(_STAMP_V22_WINDOWS, (guild_id,))
    return cur.rowcount


def backfill_guild_code_posts(conn: sqlite3.Connection, guild_id: int) -> int:
    """Copy every posted or failed `alerted_codes` row into `guild_code_posts` for `guild_id`.

    This is what keeps a code v2.2 already announced from being announced
    again: v3 asks `guild_code_posts`, not `alerted_codes`, whether this
    guild has seen a code. `seeded`, `too_old`, `roundup` and `pending` rows
    are left out on purpose: the first three were never posted anywhere, and
    a `pending` one is the crash-recovery path's business. `claimed_at`
    borrows `first_seen_at`, the closest thing v2.2 recorded. No commit.
    """
    cur = conn.execute(
        "INSERT INTO guild_code_posts "
        "(guild_id, code, status, message_id, pinged, from_roundup, claimed_at) "
        "SELECT ?, code, status, message_id, pinged, from_roundup, first_seen_at "
        "FROM alerted_codes WHERE status IN ('posted', 'failed')",
        (guild_id,),
    )
    return cur.rowcount


def backfill_lounge_quotes_guild(conn: sqlite3.Connection, guild_id: int) -> int:
    """Stamp `guild_id` on every `lounge_quotes_used` row that has no guild yet. No commit."""
    cur = conn.execute(
        "UPDATE lounge_quotes_used SET guild_id = ? WHERE guild_id IS NULL", (guild_id,)
    )
    return cur.rowcount


# --- SHiFT per guild (design.md §12 and §15) ---
#
# Detection is global (`alerted_codes` above: one row per code, ever); what a
# server has been told lives in `guild_code_posts`. The two are joined by the
# fan-out in `shift/fanout.py`, which is the only caller of everything here.
#
# A code's life in one server's queue (`guild_code_posts.status`):
#
#   release -> queued -> pending -> posted | failed
#                 \-> skipped (the server stopped wanting it before its turn)
#
# `queued` is written at release time, for every server eligible at that
# moment, in the release's own transaction. Delivery (a later step, possibly a
# later pass) claims queued -> pending under BEGIN IMMEDIATE, sends, then marks
# the outcome. `pending` rows are never re-sent: a crash between claim and send
# is recovered to `failed`, because a double ping is worse than a missed one.


def record_released_codes(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, bool]],
    *,
    now: Callable[[], datetime] | None = None,
    queue_for_games: list[str] | None = None,
    flags: Mapping[str, tuple[bool, bool]] | None = None,
) -> list[str]:
    """Record globally-new codes as released: `(code, source_name, item_url, from_roundup)`.

    `status='posted'` here means "released to post"; whether any given server
    actually got it is `guild_code_posts`' business. Writing the row keeps v2.2's
    once-per-code check and `/shift codes` honest. Returns the codes this call
    really inserted: a code some other writer recorded first is left alone and
    left out, so it never fans out twice.

    Passing `queue_for_games` (the `shift.games` list; empty means every game)
    also queues each inserted code for every server eligible at `now`, in the
    same transaction, so who hears about a code is decided at the moment it's
    released and not by whoever happens to be reachable before a timeout.
    `flags` maps a code to its `(golden, trusted)` for the later render.
    """
    moment = (now or (lambda: datetime.now(UTC)))()
    now_iso = moment.isoformat()
    inserted: list[str] = []
    with conn:
        for code, source_name, item_url, from_roundup in rows:
            cur = conn.execute(
                "INSERT INTO alerted_codes "
                "(code, first_seen_at, source_name, item_url, pinged, from_roundup, status) "
                "VALUES (?, ?, ?, ?, 0, ?, 'posted') ON CONFLICT(code) DO NOTHING",
                (code, now_iso, source_name, item_url, int(from_roundup)),
            )
            if cur.rowcount == 1:
                inserted.append(code)
        if queue_for_games is None or not inserted:
            return inserted
        by_code = {row[0]: row for row in rows}
        guild_ids = [g.guild_id for g, _ in eligible_shift_guilds(conn, queue_for_games, moment)]
        for guild_id in guild_ids:
            for code in inserted:
                golden, trusted = (flags or {}).get(code, (False, False))
                conn.execute(
                    "INSERT INTO guild_code_posts "
                    "(guild_id, code, status, pinged, from_roundup, golden, trusted, claimed_at) "
                    "VALUES (?, ?, 'queued', 0, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (guild_id, code, int(by_code[code][3]), int(golden), int(trusted), now_iso),
                )
    return inserted


def _shift_guild_state(
    conn: sqlite3.Connection, guild_id: int, games: list[str]
) -> tuple[GuildSettings, ShiftSettings] | None:
    """This server's settings if it currently wants SHiFT alerts, else None.

    Set up, alerts on with a channel, and (when `games` is non-empty)
    following at least one of them. Says nothing about `enabled_at`; callers
    differ on what that means.
    """
    guild = get_guild(conn, guild_id)
    shift = get_shift(conn, guild_id)
    if guild is None or shift is None or not guild.set_up:
        return None
    if not shift.enabled or shift.channel_id is None:
        return None
    if games:
        followed = {g.game_key for g in list_guild_games(conn, guild_id)}
        if not followed & set(games):
            return None
    return guild, shift


def eligible_shift_guilds(
    conn: sqlite3.Connection, games: list[str], now: datetime
) -> list[tuple[GuildSettings, ShiftSettings]]:
    """Servers that should hear about a code released at `now`, in guild id order.

    Set up, alerts on with a channel, `enabled_at` not in the future, and (when
    `games` is non-empty) following at least one of them. Empty `games` means
    every game, same as `shift.games` always has.
    """
    rows = conn.execute(
        "SELECT g.guild_id FROM guilds g JOIN guild_shift s ON s.guild_id = g.guild_id "
        "WHERE g.set_up = 1 AND s.enabled = 1 AND s.channel_id IS NOT NULL "
        "ORDER BY g.guild_id"
    ).fetchall()
    eligible: list[tuple[GuildSettings, ShiftSettings]] = []
    for row in rows:
        state = _shift_guild_state(conn, row["guild_id"], games)
        if state is None:
            continue
        _, shift = state
        if shift.enabled_at is None or shift.enabled_at > now:
            continue
        eligible.append(state)
    return eligible


def current_shift_delivery(
    conn: sqlite3.Connection, guild_id: int, games: list[str]
) -> tuple[GuildSettings, ShiftSettings] | None:
    """A queued code's delivery-time check: what this server wants *right now*.

    None means skip (alerts off, no channel, no followed SHiFT game, or the
    server is gone). The caller uses the returned settings, not whatever they
    were when the code was released.
    """
    return _shift_guild_state(conn, guild_id, games)


def queued_guild_ids(conn: sqlite3.Connection) -> list[int]:
    """Servers with at least one `queued` code, in guild id order."""
    return [
        row["guild_id"]
        for row in conn.execute(
            "SELECT DISTINCT guild_id FROM guild_code_posts WHERE status = 'queued' "
            "ORDER BY guild_id"
        )
    ]


def queued_guild_codes(conn: sqlite3.Connection, guild_id: int) -> list[QueuedCode]:
    """This server's `queued` codes in release order, with what the render needs."""
    rows = conn.execute(
        "SELECT p.code, a.source_name, a.item_url, p.from_roundup, p.golden, p.trusted, "
        "p.claimed_at FROM guild_code_posts p JOIN alerted_codes a ON a.code = p.code "
        "WHERE p.guild_id = ? AND p.status = 'queued' ORDER BY p.rowid",
        (guild_id,),
    ).fetchall()
    return [
        QueuedCode(
            code=row["code"],
            source_name=row["source_name"],
            item_url=row["item_url"],
            from_roundup=bool(row["from_roundup"]),
            golden=bool(row["golden"]),
            trusted=bool(row["trusted"]),
            queued_at=datetime.fromisoformat(row["claimed_at"]),
        )
        for row in rows
    ]


def skip_queued_guild_codes(conn: sqlite3.Connection, guild_id: int, codes: list[str]) -> None:
    """Flip this server's still-`queued` `codes` to `skipped`: it stopped wanting them."""
    with conn:
        for code in codes:
            conn.execute(
                "UPDATE guild_code_posts SET status = 'skipped' "
                "WHERE guild_id = ? AND code = ? AND status = 'queued'",
                (guild_id, code),
            )


class ClaimedCodes(NamedTuple):
    """What `claim_guild_codes` got: the codes now `pending`, and whether the ping was spent."""

    codes: list[str]
    pinged: bool


def claim_guild_codes(
    conn: sqlite3.Connection,
    guild_id: int,
    codes: list[str],
    *,
    pinged: bool,
    local_day: str,
    now: Callable[[], datetime] | None = None,
    max_pings: int | None = None,
    from_roundup: bool = False,
    ping_codes: Collection[str] | None = None,
    followup_ok: bool = False,
) -> ClaimedCodes:
    """Move one server's `queued` `codes` to `pending`, spending its ping.

    Record-then-post, under `BEGIN IMMEDIATE` so two claimers can't both spend the
    last ping. The budget is the server's own (`guild_shift.ping_day` and
    `ping_count`), and `local_day` is that server's local date.

    Takes whichever of `codes` are still `queued` under the write lock and
    returns those, in the order given. Another pass may have claimed (or the
    server skipped) some of them since the caller looked; the rest are still
    ours to send, so one lost code no longer strands its siblings. Claiming
    nothing spends nothing. The ping is spent only if some claimed code
    deserves one (`ping_codes`, when given, names the codes that do) and the
    cap, re-checked here under the lock, still has room.

    `followup_ok` (migration 006) stamps the claimed rows as eligible for a
    "confirmed by a second source" follow-up, but only when this claim really
    went out unpinged: a batch that carried the ping already said everything
    a follow-up would.
    """
    if not codes:
        return ClaimedCodes([], False)
    now_iso = _resolve_now(now)
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ping_day, ping_count FROM guild_shift WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"guild {guild_id} has no SHiFT settings to claim against")
        still_queued = _queued_among(conn, guild_id, codes)
        claimed = [code for code in codes if code in still_queued]
        if not claimed:
            conn.execute("COMMIT")
            return ClaimedCodes([], False)
        count = row["ping_count"] if row["ping_day"] == local_day else 0
        actual_pinged = pinged
        if ping_codes is not None and not any(code in ping_codes for code in claimed):
            actual_pinged = False
        if max_pings is not None and actual_pinged and count >= max_pings:
            actual_pinged = False
        if actual_pinged:
            count += 1
        conn.execute(
            "UPDATE guild_shift SET ping_day = ?, ping_count = ? WHERE guild_id = ?",
            (local_day, count, guild_id),
        )
        for code in claimed:
            conn.execute(
                "UPDATE guild_code_posts SET status = 'pending', pinged = ?, from_roundup = ?, "
                "followup_ok = ?, claimed_at = ? "
                "WHERE guild_id = ? AND code = ? AND status = 'queued'",
                (
                    int(actual_pinged),
                    int(from_roundup),
                    int(followup_ok and not actual_pinged and not from_roundup),
                    now_iso,
                    guild_id,
                    code,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation
    return ClaimedCodes(claimed, actual_pinged)


def _queued_among(conn: sqlite3.Connection, guild_id: int, codes: list[str]) -> set[str]:
    """The subset of `codes` this server still has `queued`."""
    found: set[str] = set()
    for i in range(0, len(codes), _SQLITE_VARIABLE_CHUNK):
        chunk = codes[i : i + _SQLITE_VARIABLE_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        # Literal "?"s sized to the chunk; the values are bound below.
        query = (
            "SELECT code FROM guild_code_posts "  # noqa: S608
            f"WHERE guild_id = ? AND status = 'queued' AND code IN ({placeholders})"
        )
        found.update(row["code"] for row in conn.execute(query, [guild_id, *chunk]))
    return found


def mark_guild_codes_posted(
    conn: sqlite3.Connection, guild_id: int, codes: list[str], *, message_id: int | None
) -> None:
    """Flip this guild's `pending` `codes` to `posted`, all sharing one message id.

    Only `pending` rows move: a startup recovery that already failed one must
    not have it flipped back by a send that finished late.
    """
    with conn:
        for code in codes:
            conn.execute(
                "UPDATE guild_code_posts SET status = 'posted', message_id = ? "
                "WHERE guild_id = ? AND code = ? AND status = 'pending'",
                (message_id, guild_id, code),
            )


def mark_guild_codes_failed(conn: sqlite3.Connection, guild_id: int, codes: list[str]) -> None:
    """Flip this guild's `pending` `codes` to `failed` after a send that never landed."""
    with conn:
        for code in codes:
            conn.execute(
                "UPDATE guild_code_posts SET status = 'failed' "
                "WHERE guild_id = ? AND code = ? AND status = 'pending'",
                (guild_id, code),
            )


def fail_queued_guild_codes(conn: sqlite3.Connection, guild_id: int) -> list[str]:
    """Flip this server's `queued` codes to `failed`; return them.

    For a turn that blew up before anything was claimed: retrying a bug every
    hour (and telling the server every hour) is worse than saying so once.
    """
    with conn:
        codes = [
            row["code"]
            for row in conn.execute(
                "SELECT code FROM guild_code_posts WHERE guild_id = ? AND status = 'queued' "
                "ORDER BY rowid",
                (guild_id,),
            )
        ]
        conn.execute(
            "UPDATE guild_code_posts SET status = 'failed' "
            "WHERE guild_id = ? AND status = 'queued'",
            (guild_id,),
        )
    return codes


def fail_pending_guild_codes(
    conn: sqlite3.Connection, *, claimed_before: datetime | None = None
) -> dict[int, list[str]]:
    """Flip `pending` guild codes to `failed`; return the codes per guild.

    The crash-recovery step, per server: a `pending` row means a process
    claimed a code (and a ping) for that server and died before the send was
    confirmed. The caller tells each affected server, and the owner only a count.
    At startup nothing can be in flight, so every `pending` row goes; a running
    pass passes `claimed_before` so a send still in flight elsewhere is left alone.
    """
    with conn:
        rows = conn.execute(
            "SELECT guild_id, code, claimed_at FROM guild_code_posts WHERE status = 'pending' "
            "ORDER BY guild_id, claimed_at, code"
        ).fetchall()
        by_guild: dict[int, list[str]] = {}
        for row in rows:
            if claimed_before is not None and datetime.fromisoformat(row["claimed_at"]) >= (
                claimed_before
            ):
                continue
            by_guild.setdefault(row["guild_id"], []).append(row["code"])
            conn.execute(
                "UPDATE guild_code_posts SET status = 'failed' "
                "WHERE guild_id = ? AND code = ? AND status = 'pending'",
                (row["guild_id"], row["code"]),
            )
    return by_guild


# --- SHiFT follow-ups: "confirmed by a second source" (migration 006, plan D14) ---
#
# A community-only code posts unpinged. If a second, independent source name
# (any trust) or any trusted source sees it within `FOLLOWUP_WINDOW` of the
# code's first sighting, every server whose original post went out unpinged
# *because the batch was untrusted* gets one follow-up that carries its ping.
# Cap-reached, roundup, ping-off and failed originals never qualify: the
# `followup_ok` stamp (`claim_guild_codes`) is how that gets decided once, at
# the original post, instead of reconstructed from circumstantial evidence.
# `guild_code_followups` has the same life cycle as `guild_code_posts`, and its
# primary key is the "at most one, ever" guarantee.

FOLLOWUP_WINDOW = timedelta(hours=24)


def record_code_sightings(
    conn: sqlite3.Connection,
    rows: Iterable[tuple[str, str, bool, bool]],
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Remember who has seen which code: `(code, source_name, trusted, roundup)`.

    One row per (code, source name); `seen_at` is the first time that source
    showed it to us and never moves, because the 24 hour window is measured
    from it. A later sighting from the same source can only make the row more
    trusted or less of a roundup, never the reverse (a source that once posted
    the code in a normal item has posted it in a normal item).
    """
    now_iso = _resolve_now(now)
    with conn:
        for code, source_name, trusted, roundup in rows:
            conn.execute(
                "INSERT INTO code_sightings (code, source_name, trusted, roundup, seen_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(code, source_name) DO UPDATE SET "
                "trusted = MAX(trusted, excluded.trusted), "
                "roundup = MIN(roundup, excluded.roundup)",
                (code, source_name, int(trusted), int(roundup), now_iso),
            )


def queue_confirmed_followups(
    conn: sqlite3.Connection,
    codes: Iterable[str],
    *,
    now: Callable[[], datetime] | None = None,
    window: timedelta = FOLLOWUP_WINDOW,
) -> int:
    """Queue a follow-up for every confirmed code in `codes`; return how many rows were queued.

    A code is confirmed when its non-roundup sightings, counting only those
    first seen within `window` of the code's own `first_seen_at`, include a
    trusted source or two different source names (compared without regard to
    case: "r/Borderlands4" spelled twice is one feed, not a second opinion). Each confirmed code is
    queued for every server whose original post of it is `posted` and stamped
    `followup_ok`. A (server, code) that already has a follow-up row, in any
    state, is left alone, so calling this every pass is safe (and is how a
    second pass that races the first sends nothing extra).
    """
    now_iso = _resolve_now(now)
    queued = 0
    with conn:
        for code in dict.fromkeys(codes):
            first = conn.execute(
                "SELECT first_seen_at FROM alerted_codes WHERE code = ?", (code,)
            ).fetchone()
            if first is None:
                continue
            deadline = datetime.fromisoformat(first["first_seen_at"]) + window
            sightings = [
                row
                for row in conn.execute(
                    "SELECT source_name, trusted, seen_at FROM code_sightings "
                    "WHERE code = ? AND roundup = 0",
                    (code,),
                )
                if datetime.fromisoformat(row["seen_at"]) <= deadline
            ]
            if not (
                any(row["trusted"] for row in sightings)
                or len({row["source_name"].casefold() for row in sightings}) >= 2
            ):
                continue
            cur = conn.execute(
                "INSERT INTO guild_code_followups (guild_id, code, status, pinged, queued_at) "
                "SELECT guild_id, code, 'queued', 0, ? FROM guild_code_posts "
                "WHERE code = ? AND status = 'posted' AND followup_ok = 1 "
                "ON CONFLICT DO NOTHING",
                (now_iso, code),
            )
            queued += cur.rowcount
    return queued


def queued_followup_guild_ids(conn: sqlite3.Connection) -> list[int]:
    """Servers with at least one `queued` follow-up, in guild id order."""
    return [
        row["guild_id"]
        for row in conn.execute(
            "SELECT DISTINCT guild_id FROM guild_code_followups WHERE status = 'queued' "
            "ORDER BY guild_id"
        )
    ]


def queued_guild_followups(
    conn: sqlite3.Connection, guild_id: int, limit: int | None = None
) -> list[str]:
    """This server's `queued` follow-up codes in queue order (at most `limit`)."""
    rows = conn.execute(
        "SELECT code FROM guild_code_followups WHERE guild_id = ? AND status = 'queued' "
        "ORDER BY rowid LIMIT ?",
        (guild_id, -1 if limit is None else limit),
    ).fetchall()
    return [row["code"] for row in rows]


def _set_followup_status(
    conn: sqlite3.Connection,
    guild_id: int,
    codes: Iterable[str],
    *,
    status: str,
    only_from: str,
    message_id: int | None = None,
) -> None:
    with conn:
        for code in codes:
            conn.execute(
                "UPDATE guild_code_followups SET status = ?, message_id = COALESCE(?, message_id) "
                "WHERE guild_id = ? AND code = ? AND status = ?",
                (status, message_id, guild_id, code, only_from),
            )


def skip_queued_guild_followups(conn: sqlite3.Connection, guild_id: int, codes: list[str]) -> None:
    """Flip this server's still-`queued` follow-ups to `skipped`: it stopped wanting them."""
    _set_followup_status(conn, guild_id, codes, status="skipped", only_from="queued")


def mark_guild_followups_posted(
    conn: sqlite3.Connection, guild_id: int, codes: list[str], *, message_id: int | None
) -> None:
    """Flip this server's `pending` follow-ups to `posted` (only `pending` ones move)."""
    _set_followup_status(
        conn, guild_id, codes, status="posted", only_from="pending", message_id=message_id
    )


def mark_guild_followups_failed(conn: sqlite3.Connection, guild_id: int, codes: list[str]) -> None:
    """Flip this server's `pending` follow-ups to `failed` after a send that never landed."""
    _set_followup_status(conn, guild_id, codes, status="failed", only_from="pending")


def fail_queued_guild_followups(conn: sqlite3.Connection, guild_id: int) -> None:
    """Flip this server's `queued` follow-ups to `failed` (a turn blew up before claiming)."""
    with conn:
        conn.execute(
            "UPDATE guild_code_followups SET status = 'failed' "
            "WHERE guild_id = ? AND status = 'queued'",
            (guild_id,),
        )


def fail_pending_guild_followups(
    conn: sqlite3.Connection, *, claimed_before: datetime | None = None
) -> int:
    """Flip `pending` follow-ups to `failed`, like `fail_pending_guild_codes`; return how many."""
    failed = 0
    with conn:
        for row in conn.execute(
            "SELECT guild_id, code, claimed_at FROM guild_code_followups WHERE status = 'pending'"
        ).fetchall():
            if (
                claimed_before is not None
                and row["claimed_at"] is not None
                and datetime.fromisoformat(row["claimed_at"]) >= claimed_before
            ):
                continue
            failed += conn.execute(
                "UPDATE guild_code_followups SET status = 'failed' "
                "WHERE guild_id = ? AND code = ? AND status = 'pending'",
                (row["guild_id"], row["code"]),
            ).rowcount
    return failed


def claim_guild_followups(
    conn: sqlite3.Connection,
    guild_id: int,
    codes: list[str],
    *,
    local_day: str,
    max_pings: int,
    now: Callable[[], datetime] | None = None,
) -> list[str]:
    """Claim this server's `queued` follow-ups for `codes`, spending one unit of its ping budget.

    The same `BEGIN IMMEDIATE` shape as `claim_guild_codes`: the budget is
    re-read under the write lock, and only codes still `queued` are taken, so
    two passes racing for the same follow-up produce one claim. One message
    carries every claimed code, so one unit is spent however many there are.
    If the day's budget is already gone, the follow-ups become `skipped` (never
    an unpinged repost: the whole point of one is the ping) and nothing is
    returned; the cap is left exactly as it was.
    """
    if not codes:
        return []
    now_iso = _resolve_now(now)
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ping_day, ping_count FROM guild_shift WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"guild {guild_id} has no SHiFT settings to claim against")
        placeholders = ",".join("?" for _ in codes)
        # Literal "?"s sized to the list; the values are bound below.
        query = (
            "SELECT code FROM guild_code_followups "  # noqa: S608
            f"WHERE guild_id = ? AND status = 'queued' AND code IN ({placeholders})"
        )
        still_queued = {r["code"] for r in conn.execute(query, [guild_id, *codes])}
        claimed = [code for code in codes if code in still_queued]
        if not claimed:
            conn.execute("COMMIT")
            return []
        count = row["ping_count"] if row["ping_day"] == local_day else 0
        if count >= max_pings:
            for code in claimed:
                conn.execute(
                    "UPDATE guild_code_followups SET status = 'skipped' "
                    "WHERE guild_id = ? AND code = ? AND status = 'queued'",
                    (guild_id, code),
                )
            conn.execute("COMMIT")
            return []
        conn.execute(
            "UPDATE guild_shift SET ping_day = ?, ping_count = ? WHERE guild_id = ?",
            (local_day, count + 1, guild_id),
        )
        for code in claimed:
            conn.execute(
                "UPDATE guild_code_followups SET status = 'pending', pinged = 1, claimed_at = ? "
                "WHERE guild_id = ? AND code = ? AND status = 'queued'",
                (now_iso, guild_id, code),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation
    return claimed


# --- Free servers' /news: item headlines (design.md §15, owner decision D2) ---
#
# Stories only exist for games a comped server follows, so a free server
# reading `/news` would see nothing for most of the catalog. These read the
# shared `items` instead, limited to the games `guild_id` follows.

_FOLLOWED = "SELECT game_key FROM guild_games WHERE guild_id = ?"


def _item_topic_filter(guild_id: int, topic_keys: list[str]) -> tuple[str, list[object]]:
    """SQL fragment and params: items tagged with a game this guild follows.

    A non-empty `topic_keys` narrows that to those games, still only if the
    guild follows them. Placeholders are literal "?"s; values are bound.
    """
    sql = f"items.id IN (SELECT item_id FROM item_topics WHERE topic_key IN ({_FOLLOWED})"  # noqa: S608
    params: list[object] = [guild_id]
    if topic_keys:
        sql += f" AND topic_key IN ({','.join('?' for _ in topic_keys)})"
        params.extend(topic_keys)
    return sql + ")", params


def _rows_to_item_views(
    conn: sqlite3.Connection, guild_id: int, rows: list[sqlite3.Row]
) -> list[ItemView]:
    topics_by_item: dict[int, list[str]] = {row["id"]: [] for row in rows}
    ids = list(topics_by_item)
    for i in range(0, len(ids), _SQLITE_VARIABLE_CHUNK):
        chunk = ids[i : i + _SQLITE_VARIABLE_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        query = (
            "SELECT item_id, topic_key FROM item_topics "  # noqa: S608
            f"WHERE item_id IN ({placeholders}) AND topic_key IN ({_FOLLOWED}) "
            "ORDER BY item_id, topic_key"
        )
        for topic_row in conn.execute(query, [*chunk, guild_id]):
            topics_by_item[topic_row["item_id"]].append(topic_row["topic_key"])
    return [
        ItemView(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            excerpt=row["excerpt"],
            source_name=row["source_name"],
            trust=row["trust"],
            published_at=_parse_dt(row["published_at"]),
            collected_at=datetime.fromisoformat(row["collected_at"]),
            topic_keys=topics_by_item[row["id"]],
        )
        for row in rows
    ]


_ITEM_COLUMNS = (
    "items.id, items.url, items.title, items.excerpt, items.source_name, "
    "items.trust, items.published_at, items.collected_at"
)


def query_items(
    conn: sqlite3.Connection,
    guild_id: int,
    topic_keys: list[str],
    since: datetime,
    limit: int,
    offset: int,
) -> tuple[list[ItemView], int]:
    """Items for a free server's `/news recent`, newest collected first.

    Limited to games `guild_id` follows; an empty `topic_keys` means all of them.
    `limit` is clamped to at least 1 and `offset` to at least 0, for the same
    reason as in `query_stories`.
    """
    limit, offset = max(limit, 1), max(offset, 0)
    topic_sql, topic_params = _item_topic_filter(guild_id, topic_keys)
    params = [*topic_params, _utc_iso(since)]
    where = f"{topic_sql} AND items.collected_at >= ?"
    total = conn.execute(
        f"SELECT COUNT(*) FROM items WHERE {where}",  # noqa: S608
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM items WHERE {where} "  # noqa: S608
        "ORDER BY items.collected_at DESC, items.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return _rows_to_item_views(conn, guild_id, rows), total


def search_items(
    conn: sqlite3.Connection, guild_id: int, query: str, since: datetime, limit: int, offset: int
) -> tuple[list[ItemView], int]:
    """Full-text search over item titles and excerpts, for a free server's `/news search`.

    Same shape as `search_stories`: `fts_escape`d, ranked by bm25 then
    recency, and an empty query returns nothing. Limited to games
    `guild_id` follows.
    """
    escaped = fts_escape(query)
    if not escaped:
        return [], 0
    topic_sql, topic_params = _item_topic_filter(guild_id, [])
    where = f"items_fts MATCH ? AND items.collected_at >= ? AND {topic_sql}"
    params = [escaped, _utc_iso(since), *topic_params]
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM items_fts JOIN items ON items.id = items_fts.rowid "  # noqa: S608
            f"WHERE {where}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items_fts "  # noqa: S608
            "JOIN items ON items.id = items_fts.rowid "
            f"WHERE {where} "
            "ORDER BY bm25(items_fts), items.collected_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    except sqlite3.OperationalError, UnicodeEncodeError:
        # Same two escape hatches as search_stories: an FTS5 quirk, or a
        # lone surrogate that can't be bound as UTF-8.
        return [], 0
    return _rows_to_item_views(conn, guild_id, rows), total


# --- Per-guild digests (design.md §13 and §15, plan task 6) ---
#
# The v2 guard, keyed on `(guild_id, run_date)` instead of `run_date` alone:
# claim a `pending` row before anything slow, post, write each game's message
# id through as it lands (D7), then save the final status. The v2 functions
# above stay as they are (rollback to v2.2 reads the same tables).

_GUILD_DIGEST_COLUMNS = (
    "id, guild_id, run_date, status, posted_message_ids, posted_by_game, "
    "window_start, window_end, attempts, error_notes"
)


def _guild_digest_from_row(row: sqlite3.Row) -> GuildDigestRow:
    return GuildDigestRow(
        id=row["id"],
        guild_id=row["guild_id"],
        run_date=date.fromisoformat(row["run_date"]),
        status=row["status"],
        posted_message_ids=json.loads(row["posted_message_ids"]),
        posted_by_game=json.loads(row["posted_by_game"]),
        window_start=_parse_dt(row["window_start"]),
        window_end=_parse_dt(row["window_end"]),
        attempts=row["attempts"],
        error_notes=row["error_notes"],
    )


def get_guild_digest(
    conn: sqlite3.Connection, guild_id: int, run_date: date
) -> GuildDigestRow | None:
    row = conn.execute(
        f"SELECT {_GUILD_DIGEST_COLUMNS} FROM digests "  # noqa: S608
        "WHERE guild_id = ? AND run_date = ?",
        (guild_id, run_date.isoformat()),
    ).fetchone()
    return _guild_digest_from_row(row) if row else None


def latest_guild_digest(conn: sqlite3.Connection, guild_id: int) -> GuildDigestRow | None:
    """`guild_id`'s newest digest row by local date, for `/newsbot status`."""
    row = conn.execute(
        f"SELECT {_GUILD_DIGEST_COLUMNS} FROM digests "  # noqa: S608
        "WHERE guild_id = ? ORDER BY run_date DESC LIMIT 1",
        (guild_id,),
    ).fetchone()
    return _guild_digest_from_row(row) if row else None


def _parse_dt_or_none(value: str | None) -> datetime | None:
    # One guild's unreadable timestamp must not take the whole tick down with it.
    try:
        return _parse_dt(value)
    except ValueError:
        return None


def due_candidates(conn: sqlite3.Connection) -> list[DueCandidate]:
    """Every set-up guild that follows a game, with its newest digest row.

    SQLite can't do IANA time zones, so this only fetches; `guilds.schedule.due_guilds`
    decides. A guild with no digest yet comes back with the row fields empty.
    """
    rows = conn.execute(
        "SELECT g.guild_id, g.digest_time, g.timezone, g.tier, "
        "d.run_date, d.status, d.attempts, d.updated_at, d.window_end, "
        "(d.posted_by_game != '{}' OR d.posted_message_ids != '[]') AS posted_any "
        "FROM guilds g "
        "LEFT JOIN digests d ON d.id = ("
        "SELECT id FROM digests WHERE guild_id = g.guild_id ORDER BY run_date DESC LIMIT 1) "
        "WHERE g.set_up = 1 AND EXISTS ("
        "SELECT 1 FROM guild_games WHERE guild_id = g.guild_id) "
        "ORDER BY g.guild_id"
    ).fetchall()
    return [
        DueCandidate(
            guild_id=row["guild_id"],
            digest_time=row["digest_time"],
            timezone=row["timezone"],
            tier=row["tier"],
            run_date=date.fromisoformat(row["run_date"]) if row["run_date"] else None,
            status=row["status"],
            posted_any=bool(row["posted_any"]),
            attempts=row["attempts"] or 0,
            updated_at=_parse_dt_or_none(row["updated_at"]),
            window_end=_parse_dt_or_none(row["window_end"]),
        )
        for row in rows
    ]


def last_window_end(
    conn: sqlite3.Connection, guild_id: int, *, exclude_run_date: date
) -> datetime | None:
    """Where the previous digest's window ended, for chaining the next one onto it.

    Counts only digests that posted something (`ok`, `partial`, or a failure that got
    some games out), and never `exclude_run_date`'s own row: a forced re-run of a
    day covers that day's window again instead of starting after itself.
    """
    rows = conn.execute(
        "SELECT window_end FROM digests WHERE guild_id = ? AND run_date != ? "
        "AND window_end IS NOT NULL AND (status IN ('ok', 'partial') OR posted_by_game != '{}') "
        "ORDER BY run_date DESC LIMIT 5",
        (guild_id, exclude_run_date.isoformat()),
    ).fetchall()
    ends = [datetime.fromisoformat(row["window_end"]) for row in rows]
    return max(ends) if ends else None


# --- Item watermarks (migration 007) ---
#
# A time window over `collected_at` loses items. A collection pass stamps its
# items with the instant it *started* and stores them only once every source has
# answered, so a pass that starts at 08:59:30 and commits at 09:01 stores items
# "from 08:59:30" after the 09:00 digest has read and closed its window. The next
# window starts at 09:00 and never looks back. `items.id` is assigned at commit,
# in commit order, so a digest that records the newest id it covered can't be
# overtaken: whatever commits afterwards has a bigger id and belongs to the next
# window, exactly once. `collected_at` stays as the window's label and as a floor
# (how far back a backlog may reach).
#
# I picked ids over "stamp `collected_at` at write time and make readers stop short
# of any pass in flight" because the second needs every reader to know about every
# writer (the hourly pass, the web search, a CLI run) and to agree on what "in
# flight" means. An id is just a number the database already hands out in the one
# order that matters.


def latest_item_id(conn: sqlite3.Connection, *, collected_by: datetime | None = None) -> int:
    """The newest item id, or the newest among items stamped at or before `collected_by`. 0 if none.

    With `collected_by` this is "the last item the window ending then can hold". Items
    stamped after it, even ones already stored, are left for the next window (a window
    reads as `(start, end]`, and a pass that started after the digest's time belongs to
    tomorrow's), unless an id below the cut-off drags them in; that's harmless, since a
    window reads *ids* up to this one and the next starts right after it, so nothing is
    missed or repeated either way. The `+` before the column stops SQLite choosing the
    `collected_at` index: walking ids downward from the newest stops at the first match,
    which is the first few rows, where the index route would sort every row older than
    `collected_by` (a few hundred digests at 09:00 would each do that).
    """
    if collected_by is None:
        row = conn.execute("SELECT MAX(id) AS newest FROM items").fetchone()
    else:
        row = conn.execute(
            "SELECT id AS newest FROM items WHERE +collected_at <= ? ORDER BY id DESC LIMIT 1",
            (_utc_iso(collected_by),),
        ).fetchone()
    return (row["newest"] or 0) if row else 0


def _mark_map(raw: str | None) -> dict[str, int | None]:
    """A digest's `game_items_upto` as `{game: id, or None for "intended, not covered"}`."""
    try:
        loaded = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        k: v
        for k, v in loaded.items()
        if v is None or (isinstance(v, int) and not isinstance(v, bool))
    }


def _posted_keys(raw: str | None) -> set[str]:
    try:
        loaded = json.loads(raw or "{}")
    except ValueError:
        return set()
    return set(loaded) if isinstance(loaded, dict) else set()


def last_coverage(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    exclude_run_date: date,
    games: Collection[str] | None = None,
) -> Coverage | None:
    """Where the server's previous digests left off, per game: marks, not one shared number.

    The row that sets `Coverage.end` and `item_id` is the one `last_window_end` picks (the
    posted digest with the latest window end, never `exclude_run_date`'s own). Each game's
    mark comes from the newest of the recent digests that *covered* that game:
    - a digest covered a game if it posted it, or finished (`ok`, `partial`) with a mark
      recorded for it, which includes a game that was quiet that day;
    - a digest that was following the game but didn't reach it (it failed first, or the
      channel refused it: `record_game_coverage` writes `null` for those) is skipped, and
      the next older digest decides, so the game picks up from where it last got through
      (a game no digest ever got through starts from id 0, which the 48 hour floor trims);
    - a digest that wasn't following the game at all ends the search: the game has no mark
      and starts fresh (a game followed today, or unfollowed and followed again, gets the
      first-digest floor for itself and not a stale mark from before the gap).
    `games` limits the lookup (default: every game the recent digests mention).

    A digest written by v3 carries its own ids. One v2.2 wrote, and one adopted at the
    import, has no per-game record at all, and only a window end: its ids are derived from
    it as "everything stored at or before that instant", which is what v2.2 had used, and
    every game counts as covered by it. The same derivation covers an id that's higher than
    anything stored. Migration 008 (AUTOINCREMENT) means ids can't go backwards by a purge
    anymore, so that's the belt to its braces, for a database restored without its sequence.
    """
    rows = conn.execute(
        "SELECT status, window_end, items_upto, game_items_upto, posted_by_game FROM digests "
        "WHERE guild_id = ? AND run_date != ? AND window_end IS NOT NULL "
        "AND (status IN ('ok', 'partial') OR posted_by_game != '{}') "
        "ORDER BY run_date DESC LIMIT 5",
        (guild_id, exclude_run_date.isoformat()),
    ).fetchall()
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: datetime.fromisoformat(row["window_end"]), reverse=True)
    newest = latest_item_id(conn)

    def row_end(row: sqlite3.Row) -> datetime:
        return datetime.fromisoformat(row["window_end"])

    def row_upto(row: sqlite3.Row) -> int:
        upto = row["items_upto"]
        if upto is None or upto > newest:
            return latest_item_id(conn, collected_by=row_end(row))
        return upto

    infos = []
    for row in rows:
        posted, marks = _posted_keys(row["posted_by_game"]), _mark_map(row["game_items_upto"])
        infos.append((row, posted, marks, not posted and not marks))

    def resolve(game_key: str) -> tuple[int, datetime] | None:
        wanted_since: datetime | None = None  # the oldest digest that followed it and missed it
        for row, posted, marks, legacy in infos:
            if legacy:
                return row_upto(row), row_end(row)
            mark = marks.get(game_key)
            finished = row["status"] in ("ok", "partial") and mark is not None
            if game_key in posted or finished:
                if mark is None or mark > newest:
                    mark = row_upto(row)
                return mark, row_end(row)
            if game_key in marks:
                wanted_since = row_end(row)  # it was following the game and never got to it
                continue
            break
        # Never covered, but a digest wanted to cover it: that digest's news is still owed, so
        # the game starts from the beginning of what the floor allows, not from its first mark.
        return (0, wanted_since) if wanted_since is not None else None

    wanted = set(games) if games is not None else {k for _, p, m, _ in infos for k in (*p, *m)}
    by_game: dict[str, int] = {}
    ends: dict[str, datetime] = {}
    for key in sorted(wanted):
        found = resolve(key)
        if found is not None:
            by_game[key], ends[key] = found
    best = infos[0]
    return Coverage(row_end(best[0]), row_upto(best[0]), by_game, ends, best[3])


def item_range(
    conn: sqlite3.Connection, guild_id: int, *, exclude_run_date: date, end: datetime
) -> ItemRange:
    """The items a digest ending at `end` covers: past the last digest's ids, up to `end`'s newest.

    `upto` never falls below `after` (a window that ends before the last one did, after a
    zone change or a clock step, is empty, not backwards).
    """
    previous = last_coverage(conn, guild_id, exclude_run_date=exclude_run_date)
    after = previous.item_id if previous else None
    upto = max(latest_item_id(conn, collected_by=end), after or 0)
    return ItemRange(after, upto, item_floor(end, previous is not None))


def claim_guild_digest(
    conn: sqlite3.Connection,
    guild_id: int,
    run_date: date,
    *,
    force: bool,
    window: tuple[datetime, datetime],
    resume: bool = False,
    now: Callable[[], datetime] | None = None,
) -> GuildClaim | None:
    """Reserve `(guild_id, run_date)` for a digest, or say no.

    Runs under `BEGIN IMMEDIATE`, so two claimers can't both see "no row".
    The meanings (they date from v2's one-server claim):
    - no row: insert `pending` with `window`;
    - `ok`, `partial`, or `pending` without `force`: refused (`None`), except that
      a `pending` row with a stored window *resumes* when `resume` is true (D7: the
      process that started it died). A resume keeps the row's window and whatever
      it already posted, so the caller posts only the missing games;
    - `failed`: reclaimable. If it had posted games and has a window it continues
      like a resume; a clean failure starts over with `window`;
    - `force`: replaces the row in place, keeping its id, with `window` and nothing
      posted. (A confirmed run-now; the admin was asked first.)
    Every successful claim increments `attempts`.

    `resume=True` is the scheduler's claim (scheduled and catch-up runs), and it
    carries the scheduler's rules, re-checked here under the lock because the due
    check ran on a snapshot that two ticks, or two processes, may both be holding:
    a `pending` row resumes only once its lease is stale (`schedule.lease_is_stale`:
    `updated_at` is refreshed as games land, so a fresh one is someone's live
    publish), and a `failed` row is reclaimed only by `schedule.retry_is_ready`
    (clean, under the attempt cap, past the ten-minute gap). A clean failure
    retried this way reuses the window it was claimed with, so the retry covers
    the same items.

    The claim also fixes which items the digest covers (`item_range`: ids past the
    previous digest's, up to the newest stored at the window's end), under the same lock,
    and stores them on the row. A retry or a resume keeps the row's own ids, so it reads
    what the first try read; and because they're ids rather than times, an item a slow
    collection pass commits after this claim lands in the next digest, never in neither.
    """
    now_dt = (now or (lambda: datetime.now(UTC)))()
    now_iso = now_dt.isoformat()
    new_start, new_end = (_utc_iso(moment) for moment in window)
    claim: GuildClaim | None
    with _immediate(conn):
        row = conn.execute(
            "SELECT id, status, posted_by_game, posted_message_ids, window_start, window_end, "
            "attempts, updated_at, items_after, items_upto "
            "FROM digests WHERE guild_id = ? AND run_date = ?",
            (guild_id, run_date.isoformat()),
        ).fetchone()

        def fresh_ids(end: datetime) -> tuple[int | None, int]:
            covered = item_range(conn, guild_id, exclude_run_date=run_date, end=end)
            return covered.after, covered.upto

        if row is None:
            after_id, upto_id = fresh_ids(window[1])
            cur = conn.execute(
                "INSERT INTO digests (guild_id, run_date, status, window_start, window_end, "
                "items_after, items_upto, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, 1, ?, ?)",
                (guild_id, run_date.isoformat(), new_start, new_end, after_id, upto_id)
                + (now_iso, now_iso),
            )
            claim = GuildClaim(cur.lastrowid, window[0], window[1], {}, False, 1, after_id, upto_id)
        else:
            status = row["status"]
            posted: dict[str, int] = json.loads(row["posted_by_game"])
            posted_any = bool(posted) or row["posted_message_ids"] != "[]"
            updated_at = _parse_dt_or_none(row["updated_at"])
            attempts = row["attempts"] + 1
            stored = (
                (
                    datetime.fromisoformat(row["window_start"]),
                    datetime.fromisoformat(row["window_end"]),
                )
                if row["window_start"] and row["window_end"]
                else None
            )
            # The ids a stored window goes with (a row written before migration 007 has
            # none, so they're worked out for its window now).
            kept = (
                (row["items_after"], row["items_upto"])
                if row["items_upto"] is not None
                else fresh_ids(stored[1])
                if stored is not None
                else (None, 0)
            )
            if force:
                claim = GuildClaim(row["id"], window[0], window[1], {}, False, attempts)
            elif status in ("ok", "partial") or (status == "pending" and not resume):
                claim = None
            elif status == "pending" and (stored is None or not lease_is_stale(updated_at, now_dt)):
                claim = None  # a v2.2 row nobody can read, or somebody's live publish
            elif status == "pending" and resume and row["attempts"] >= MAX_ATTEMPTS:
                claim = None  # out of attempts: the tick abandons it (`abandon_guild_digest`)
            elif (
                status == "failed"
                and resume
                and not retry_is_ready(
                    posted_any=posted_any,
                    attempts=row["attempts"],
                    updated_at=updated_at,
                    now=now_dt,
                )
            ):
                claim = None  # too soon, too many tries, or an admin's call
            elif (status == "pending" or posted) and stored is not None:
                claim = GuildClaim(row["id"], stored[0], stored[1], posted, True, attempts, *kept)
            elif resume and stored is not None:
                claim = GuildClaim(row["id"], stored[0], stored[1], {}, False, attempts, *kept)
            else:
                claim = GuildClaim(row["id"], window[0], window[1], {}, False, attempts)
            if claim is not None:
                if claim.items_upto is None:  # a new window: its ids are fixed now, under the lock
                    after_id, upto_id = fresh_ids(claim.window_end)
                    claim = replace(claim, items_after=after_id, items_upto=upto_id)
                start_iso, end_iso = _utc_iso(claim.window_start), _utc_iso(claim.window_end)
                conn.execute(
                    "UPDATE digests SET status = 'pending', window_start = ?, window_end = ?, "
                    "items_after = ?, items_upto = ?, "
                    "attempts = attempts + 1, updated_at = ?, "
                    "posted_by_game = CASE WHEN ? THEN '{}' ELSE posted_by_game END, "
                    "game_items_upto = CASE WHEN ? THEN NULL ELSE game_items_upto END "
                    "WHERE id = ?",
                    (
                        start_iso,
                        end_iso,
                        claim.items_after,
                        claim.items_upto,
                        now_iso,
                        int(force),
                        int(force),
                        row["id"],
                    ),
                )
    return claim


def record_posted_game(
    conn: sqlite3.Connection,
    digest_id: int,
    game_key: str,
    message_id: int,
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Write one posted game's message id through to its digest row (D7).

    Called as each game lands, so a crash later in the same digest leaves an
    honest record of what already went out. Keeps the flat `posted_message_ids`
    list (v2.2's shape) in step, and refreshes `updated_at`: for a `pending` row
    that's the lease that keeps another process from resuming it. The read and
    the write happen in one `BEGIN IMMEDIATE`, so two writers can't lose each
    other's games.
    """
    now_iso = _resolve_now(now)
    with _immediate(conn):
        row = conn.execute(
            "SELECT posted_by_game FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"digest {digest_id} doesn't exist")
        posted = json.loads(row["posted_by_game"])
        posted[game_key] = message_id
        conn.execute(
            "UPDATE digests SET posted_by_game = ?, posted_message_ids = ?, updated_at = ? "
            "WHERE id = ?",
            (json.dumps(posted), json.dumps(list(posted.values())), now_iso, digest_id),
        )


def touch_guild_digest(
    conn: sqlite3.Connection, digest_id: int, *, now: Callable[[], datetime] | None = None
) -> None:
    """Refresh a `pending` row's `updated_at`: the publisher's "still alive" heartbeat.

    Does nothing to a row that's no longer `pending`, so a late heartbeat can't
    touch a digest that already finished (or failed, whose retry gap runs from
    `updated_at`).
    """
    with conn:
        conn.execute(
            "UPDATE digests SET updated_at = ? WHERE id = ? AND status = 'pending'",
            (_resolve_now(now), digest_id),
        )


def save_guild_digest(
    conn: sqlite3.Connection,
    digest_id: int,
    status: str,
    posted_by_game: Mapping[str, int],
    notes: str | None,
    window: tuple[datetime, datetime],
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Record a finished digest: its status, what posted, the notes and the window."""
    with conn:
        conn.execute(
            "UPDATE digests SET status = ?, posted_by_game = ?, posted_message_ids = ?, "
            "error_notes = ?, window_start = ?, window_end = ?, updated_at = ? WHERE id = ?",
            (
                status,
                json.dumps(dict(posted_by_game)),
                json.dumps(list(posted_by_game.values())),
                notes,
                _utc_iso(window[0]),
                _utc_iso(window[1]),
                _resolve_now(now),
                digest_id,
            ),
        )


def abandon_guild_digest(
    conn: sqlite3.Connection,
    guild_id: int,
    run_date: date,
    *,
    max_attempts: int,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Mark a stale `pending` digest `failed` once it has used `max_attempts`. True if it did.

    Every claim, a resume included, counts as an attempt, so a digest that keeps timing
    out or dying mid-send runs out of them instead of being resumed forever. Re-checks
    under the lock (status, attempts, and that the lease really is stale), because the due
    check that asked ran on a snapshot. What already posted stays on the row.
    """
    now_dt = (now or (lambda: datetime.now(UTC)))()
    with _immediate(conn):
        row = conn.execute(
            "SELECT id, status, attempts, updated_at FROM digests "
            "WHERE guild_id = ? AND run_date = ?",
            (guild_id, run_date.isoformat()),
        ).fetchone()
        if (
            row is None
            or row["status"] != "pending"
            or row["attempts"] < max_attempts
            or not lease_is_stale(_parse_dt_or_none(row["updated_at"]), now_dt)
        ):
            return False
        conn.execute(
            "UPDATE digests SET status = 'failed', error_notes = ?, updated_at = ? WHERE id = ?",
            (
                f"gave up after {row['attempts']} attempts (each timed out or was interrupted)",
                now_dt.isoformat(),
                row["id"],
            ),
        )
    return True


def record_game_coverage(
    conn: sqlite3.Connection,
    digest_id: int,
    coverage: Mapping[str, int | None],
    *,
    keep: Collection[str] = (),
) -> None:
    """Note, per game, the newest item id this digest covers for it (`last_coverage` reads it).

    Every game the digest follows gets an entry, written before anything posts: the id
    its news will have reached (a shared summary ends before the digest was claimed, so for a
    comped game that's the summary's own `items_upto`; any other game reaches the digest's).
    An entry of `None` says "followed, but not reached" (the channel refused it), so the
    next digest picks the game up from its older mark instead of treating it as newly
    followed. `keep` names games already posted by an earlier try of this digest, whose
    recorded coverage stands (what a resume rebuilds may not be what it posted).
    """
    with _immediate(conn):
        row = conn.execute(
            "SELECT game_items_upto FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"digest {digest_id} doesn't exist")
        stored = _mark_map(row["game_items_upto"])
        merged = {k: v for k, v in coverage.items() if k not in keep or k not in stored}
        merged.update({k: v for k, v in stored.items() if k in keep})
        conn.execute(
            "UPDATE digests SET game_items_upto = ? WHERE id = ?",
            (json.dumps(merged, sort_keys=True), digest_id),
        )


def mark_guild_digest_failed(
    conn: sqlite3.Connection,
    digest_id: int,
    notes: str,
    posted_by_game: Mapping[str, int] | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Record that a digest's publish step failed, keeping everything that did post.

    `posted_by_game` is merged into what the row already has (write-through may
    already have recorded most of it); nothing is ever removed, for the reason
    `mark_guild_digest_failed` gives: forgetting a posted game is how it gets posted twice.
    """
    with _immediate(conn):
        row = conn.execute(
            "SELECT posted_by_game FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
        posted = json.loads(row["posted_by_game"]) if row else {}
        posted.update(posted_by_game or {})
        conn.execute(
            "UPDATE digests SET status = 'failed', error_notes = ?, posted_by_game = ?, "
            "posted_message_ids = ?, updated_at = ? WHERE id = ?",
            (
                notes,
                json.dumps(posted),
                json.dumps(list(posted.values())),
                _resolve_now(now),
                digest_id,
            ),
        )


def items_for_window(
    conn: sqlite3.Connection,
    guild_id: int,
    start: datetime,
    end: datetime,
    *,
    after_id: int | None = None,
    upto_id: int | None = None,
    after_by_game: Mapping[str, int | None] | None = None,
    fresh_floor: datetime | None = None,
) -> dict[str, list[HeadlineItem]]:
    """Stored items for a digest, per game `guild_id` follows, newest first.

    By default the window is time: items collected in `(start, end]`. With `upto_id` it's
    the digest's item range instead (`item_range`): ids in `(after_id, upto_id]`, and
    `start` is only the floor on `collected_at` (`end` is unused). `after_by_game` gives each
    game its own lower bound (`Coverage.mark`): an id, or `None` for a game the server
    hasn't been told yet, which takes everything newer than `fresh_floor` (the first-digest
    floor) and no id bound. A game missing from it uses `after_id`.

    Only games the guild follows come back, so another server's games can't
    leak into its digest. An item matching two followed games appears under both.
    """
    marks = after_by_game or {}
    if upto_id is None:
        window_sql = "AND items.collected_at > ? AND items.collected_at <= ? "
        params: tuple[object, ...] = (guild_id, _utc_iso(start), _utc_iso(end))
    else:
        window_sql = "AND items.collected_at > ? AND items.id > ? AND items.id <= ? "
        lowest = min([after_id or 0, *(mark or 0 for mark in marks.values())])
        params = (guild_id, _utc_iso(start), lowest, upto_id)
    rows = conn.execute(
        "SELECT item_topics.topic_key, item_topics.uncertain, items.id AS item_id, items.url, "  # noqa: S608
        "items.title, items.source_name, items.trust, items.published_at, items.collected_at "
        "FROM item_topics JOIN items ON items.id = item_topics.item_id "
        "WHERE item_topics.topic_key IN (SELECT game_key FROM guild_games WHERE guild_id = ?) "
        + window_sql
        + "ORDER BY items.collected_at DESC, items.id DESC",
        params,
    ).fetchall()
    fresh = _utc_iso(fresh_floor) if fresh_floor is not None else None
    by_game: dict[str, list[HeadlineItem]] = {}
    for row in rows:
        if upto_id is not None:
            key = row["topic_key"]
            mark = marks.get(key, after_id or 0)
            if mark is None:
                if fresh is not None and row["collected_at"] <= fresh:
                    continue
            elif row["item_id"] <= mark:
                continue
        by_game.setdefault(row["topic_key"], []).append(
            HeadlineItem(
                url=row["url"],
                title=row["title"],
                source_name=row["source_name"],
                trust=row["trust"],
                published_at=_parse_dt(row["published_at"]),
                collected_at=datetime.fromisoformat(row["collected_at"]),
                uncertain=bool(row["uncertain"]),
            )
        )
    return by_game


# The most a stored summary may trail a digest's due instant and still be reused
# (plan §3.6). The other half of the reuse rule is "newer than the server's last
# digest", which is per server and so arrives as `after`.
SUMMARY_MAX_AGE = timedelta(hours=6)


def get_game_summary(
    conn: sqlite3.Connection,
    game_key: str,
    due_at: datetime,
    after: Coverage | None = None,
) -> GameSummaryRow | None:
    """The stored summary a comped server's digest due at `due_at` may reuse, if any.

    `after` is where the server's coverage of this game left off (`Coverage.for_game`);
    `None` for a first digest, which has nothing to repeat and takes any fresh row. A
    row qualifies when:
    - it starts *exactly* where the server's coverage ended (`items_after` equals the
      server's item id), so the summary holds the server's whole window with no gap
      before it and nothing the server has already read. Two servers on one schedule
      share a row because they ended in the same place; two a half day apart don't,
      and each gets a summary of its own window. (The old rule chained summaries per
      game, so a 09:00 server and a 21:00 server each lost the news the other one's
      summary had consumed.) A row from before migration 007 has no ids and never
      matches a server that has a history;
    - it ends strictly after the server's last digest's window end (the same row
      can't be served to the same server twice), and no earlier than
      `due_at - SUMMARY_MAX_AGE` (nobody reads yesterday's news). Both are instants, so
      a 23-hour DST day or a moved digest time can't break them.
    Of the rows that qualify, an `ok` one wins over a `fallback`, then the newest.
    `None` means a new summary has to be made.
    """
    sql = "SELECT id FROM game_summaries WHERE game_key = ? AND window_end >= ?"
    params: list[object] = [game_key, _utc_iso(due_at - SUMMARY_MAX_AGE)]
    if after is not None:
        sql += " AND window_end > ? AND items_after = ?"
        params += [_utc_iso(after.end), after.item_id]
    sql += " ORDER BY (status = 'ok') DESC, window_end DESC, id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return game_summary_by_id(conn, row["id"]) if row else None


def summary_stories(conn: sqlite3.Connection, summary_id: int) -> list[StoryView]:
    """The stories saved against `summary_id`, in the order they were written."""
    rows = conn.execute(
        "SELECT id, topic_key, headline, summary, label, created_at, is_update_of "
        "FROM stories WHERE summary_id = ? ORDER BY id",
        (summary_id,),
    ).fetchall()
    return _rows_to_story_views(conn, rows)


# --- Comped summaries (plan task 7) ---

# How long a summary claim holds the game before somebody else may take it over.
# A real summary is three attempts at 60 s plus 7 s of backoff, so ten minutes
# is "the process that claimed this is dead", not "it's being thorough".
SUMMARY_CLAIM_LEASE = timedelta(minutes=10)


def _summary_claim_key(game_key: str) -> str:
    return f"summary_claim:{game_key}"


def comped_follows(conn: sqlite3.Connection) -> list[CompedFollow]:
    """Every (set-up comped server, game it follows) pair, for working out who needs a summary.

    Fetch only: `pipeline/summaries.py` does the time zone arithmetic, since
    SQLite can't do IANA zones.
    """
    rows = conn.execute(
        "SELECT gg.game_key, g.guild_id, g.digest_time, g.timezone "
        "FROM guilds g JOIN guild_games gg ON gg.guild_id = g.guild_id "
        "WHERE g.tier = 'comped' AND g.set_up = 1 ORDER BY g.guild_id, gg.game_key"
    ).fetchall()
    return [
        CompedFollow(r["game_key"], r["guild_id"], r["digest_time"], r["timezone"]) for r in rows
    ]


def game_summary_by_id(conn: sqlite3.Connection, summary_id: int) -> GameSummaryRow | None:
    row = conn.execute(
        "SELECT id, game_key, run_date, status, window_start, window_end, coverage_notes, "
        "note, input_tokens, output_tokens, items_after, items_upto "
        "FROM game_summaries WHERE id = ?",
        (summary_id,),
    ).fetchone()
    if row is None:
        return None
    return GameSummaryRow(
        id=row["id"],
        game_key=row["game_key"],
        run_date=date.fromisoformat(row["run_date"]),
        status=row["status"],
        window_start=datetime.fromisoformat(row["window_start"]),
        window_end=datetime.fromisoformat(row["window_end"]),
        coverage_notes=json.loads(row["coverage_notes"]),
        note=row["note"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        items_after=row["items_after"],
        items_upto=row["items_upto"],
    )


def summary_items(
    conn: sqlite3.Connection,
    game_key: str,
    start: datetime,
    end: datetime,
    *,
    after_id: int | None = None,
    upto_id: int | None = None,
) -> list[StoredItem]:
    """Stored items tagged `game_key`, oldest first: what a summary is built from.

    The same items v2's collect step handed the model, read back out of the store
    instead of straight off the wire. By default the window is time (collected in
    `(start, end]`); with `upto_id` it is ids in `(after_id, upto_id]` and `start` is
    just the floor on `collected_at` (see `item_range` for why ids).
    """
    if upto_id is None:
        window_sql = "AND items.collected_at > ? AND items.collected_at <= ? "
        params: tuple[object, ...] = (game_key, _utc_iso(start), _utc_iso(end))
    else:
        window_sql = "AND items.collected_at > ? AND items.id > ? AND items.id <= ? "
        params = (game_key, _utc_iso(start), after_id or 0, upto_id)
    rows = conn.execute(
        "SELECT items.url, items.title, items.excerpt, items.source_name, items.trust, "
        "items.published_at, item_topics.uncertain "
        "FROM item_topics JOIN items ON items.id = item_topics.item_id "
        "WHERE item_topics.topic_key = ? " + window_sql + "ORDER BY items.collected_at, items.id",
        params,
    ).fetchall()
    return [
        StoredItem(
            url=r["url"],
            title=r["title"],
            excerpt=r["excerpt"],
            source_name=r["source_name"],
            trust=r["trust"],
            published_at=_parse_dt(r["published_at"]),
            topics={game_key: bool(r["uncertain"])},
        )
        for r in rows
    ]


def _claim_is_live(conn: sqlite3.Connection, game_key: str, moment: datetime) -> bool:
    """True if somebody holds a claim on `game_key` that hasn't outlived its lease."""
    held = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (_summary_claim_key(game_key),)
    ).fetchone()
    if held is None:
        return False
    try:
        claimed_at = datetime.fromisoformat(json.loads(held["value"])["at"])
    except ValueError, KeyError, TypeError:
        return False  # an unreadable claim is a dead one
    return moment - claimed_at < SUMMARY_CLAIM_LEASE


def claim_game_summary(
    conn: sqlite3.Connection,
    game_key: str,
    due_at: datetime,
    *,
    token: str,
    now: Callable[[], datetime] | None = None,
    retry_fallback: bool = False,
    after: Coverage | None = None,
) -> tuple[Literal["claimed", "reusable", "busy"], GameSummaryRow | None]:
    """Decide, under `BEGIN IMMEDIATE`, who summarizes `game_key` for a digest due at `due_at`.

    `after` is where the asking server's coverage of the game left off (see
    `get_game_summary`, which decides what "reusable" means).

    - `reusable`: a summary this server may use already exists (the row comes
      back). A `fallback` row counts unless `retry_fallback` is set.
    - `busy`: somebody else holds a fresh claim; wait and look again. A live
      claim also beats a `fallback` row, because that claim may be a run-now
      retry about to replace it, and the waiter should get the retry's answer
      and not the stale one. An `ok` row is never waiting on a retry, so it is
      handed out at once even while somebody summarizes for another cycle.
    - `claimed`: the claim is now yours (`token` is written to `app_state`).
      The row is the fallback being retried, if that's what you're doing.

    The read and the write share one write lock, so two callers can't both
    see "nothing there" and both go ask Claude. (`game_summaries` itself
    can't hold a "pending" status: its CHECK only allows `ok` and
    `fallback`, and I'd rather not rebuild a table for a lock.)
    """
    moment = (now or (lambda: datetime.now(UTC)))()
    with _immediate(conn):
        existing = get_game_summary(conn, game_key, due_at, after)
        live = _claim_is_live(conn, game_key, moment)
        if existing is not None and existing.status == "ok":
            return "reusable", existing
        if live:
            return "busy", None
        if existing is not None and not retry_fallback:
            return "reusable", existing
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_summary_claim_key(game_key), json.dumps({"token": token, "at": moment.isoformat()})),
        )
        return "claimed", existing


def refresh_game_summary_claim(
    conn: sqlite3.Connection,
    game_key: str,
    token: str,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Push the claim's lease out to now, if it's still ours. False means it was lost."""
    moment = (now or (lambda: datetime.now(UTC)))()
    with _immediate(conn):
        if not _holds_claim(conn, game_key, token):
            return False
        conn.execute(
            "UPDATE app_state SET value = ? WHERE key = ?",
            (json.dumps({"token": token, "at": moment.isoformat()}), _summary_claim_key(game_key)),
        )
        return True


def _holds_claim(conn: sqlite3.Connection, game_key: str, token: str) -> bool:
    held = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (_summary_claim_key(game_key),)
    ).fetchone()
    if held is None:
        return False
    try:
        return json.loads(held["value"]).get("token") == token
    except ValueError, AttributeError:
        return False


def release_game_summary_claim(conn: sqlite3.Connection, game_key: str, token: str) -> None:
    """Drop the claim if it's still ours (a stale one somebody took over is theirs now)."""
    with _immediate(conn):
        _release_claim(conn, game_key, token)


def _release_claim(conn: sqlite3.Connection, game_key: str, token: str) -> None:
    key = _summary_claim_key(game_key)
    held = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if held is None:
        return
    try:
        mine = json.loads(held["value"]).get("token") == token
    except ValueError, AttributeError:
        mine = True
    if mine:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))


def save_game_summary(
    conn: sqlite3.Connection,
    *,
    game_key: str,
    run_date: date,
    status: Literal["ok", "fallback"],
    window_start: datetime,
    window_end: datetime,
    coverage_notes: list[str],
    note: str | None,
    usage: Usage,
    stories: list[StoryToSave],
    token: str,
    now: Callable[[], datetime] | None = None,
    replace_id: int | None = None,
    require_claim: bool = False,
    items_after: int | None = None,
    items_upto: int | None = None,
) -> int | None:
    """Save a summary and its stories in one transaction, release the claim, return the row id.

    Stories get `summary_id` set and no digest (items were stored by collection
    already, so there's nothing to wait for, and prior headlines exist the next
    day even if the post fails).

    A save is always a new row, except when `replace_id` names the row being
    retried: that one is rewritten in place, which is how a run-now retry turns
    a `fallback` into an `ok`. Its tokens add to the old row's (the failed
    attempt still cost money) and any stories it had are detached rather than
    deleted. `run_date` is only a label now; two rows with the same one are
    fine, and one can never replace the other (it used to, and an inline summary
    for a missed digest quietly ate a good prepared one).

    `items_after` and `items_upto` are the item ids the summary covers (`(after, upto]`),
    which is what lets a server tell whether it starts exactly where that server's own
    coverage ended (`get_game_summary`).

    `require_claim` makes the save conditional on `token` still holding the
    claim: if the claim was taken over (or is gone) nothing is written and the
    answer is `None`. That's what stops a slow owner and the caller who took
    its claim from both saving.
    """
    now_iso = _resolve_now(now)
    with _immediate(conn):
        if require_claim and not _holds_claim(conn, game_key, token):
            return None
        row = (
            conn.execute(
                "SELECT id FROM game_summaries WHERE id = ? AND game_key = ?",
                (replace_id, game_key),
            ).fetchone()
            if replace_id is not None
            else None
        )
        values = (
            status,
            _utc_iso(window_start),
            _utc_iso(window_end),
            json.dumps(coverage_notes),
            note,
            usage.input_tokens,
            usage.output_tokens,
            now_iso,
            items_after,
            items_upto,
        )
        if row is not None:
            summary_id = row["id"]
            conn.execute("UPDATE stories SET summary_id = NULL WHERE summary_id = ?", (summary_id,))
            conn.execute(
                "UPDATE game_summaries SET status = ?, window_start = ?, window_end = ?, "
                "coverage_notes = ?, note = ?, input_tokens = input_tokens + ?, "
                "output_tokens = output_tokens + ?, created_at = ?, items_after = ?, "
                "items_upto = ? WHERE id = ?",
                (*values, summary_id),
            )
        else:
            summary_id = conn.execute(
                "INSERT INTO game_summaries (game_key, run_date, status, window_start, "
                "window_end, coverage_notes, note, input_tokens, output_tokens, created_at, "
                "items_after, items_upto) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game_key, run_date.isoformat(), *values),
            ).lastrowid
        for story in stories:
            item_ids = [
                row["id"]
                for url in story.item_urls
                for row in conn.execute("SELECT id FROM items WHERE url = ?", (url,))
            ]
            if not item_ids:
                continue  # postprocess() already drops these; belt and braces
            cur = conn.execute(
                "INSERT INTO stories "
                "(topic_key, headline, summary, label, is_update_of, digest_id, summary_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    story.topic_key,
                    story.headline,
                    story.summary,
                    story.label,
                    story.update_of_story_id,
                    summary_id,
                    now_iso,
                ),
            )
            for item_id in item_ids:
                conn.execute(
                    "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)",
                    (cur.lastrowid, item_id),
                )
        _release_claim(conn, game_key, token)
    return summary_id


def game_summary_tokens(conn: sqlite3.Connection, since: datetime) -> Usage:
    """Tokens the summaries wrote down since `since`, for the owner's spend estimate."""
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o "
        "FROM game_summaries WHERE created_at >= ?",
        (_utc_iso(since),),
    ).fetchone()
    return Usage(input_tokens=row["i"], output_tokens=row["o"])
