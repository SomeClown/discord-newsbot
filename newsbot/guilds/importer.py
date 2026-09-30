"""The one-time import: the friend's v2.2 server becomes the first v3 guild.

Until v3, the whole bot was one server's settings spread across `config.yaml`
(games, channels, time, SHiFT, the lounge) plus a handful of single-server
tables. v3 keeps per-server settings in the database, so on the first start
something has to carry the old setup across exactly once, and do it so
carefully that nobody in that server can tell. Think of it as moving house
overnight without waking the neighbors: the important part isn't the boxes,
it's that the lights are on in the new place by morning and nobody orders a
second pizza.

The pizza, here, is today's digest. If v2.2 already posted it, the import
hands that digest row to the new guild inside the same transaction that
creates the guild, so v3 can never see the guild without also seeing the
digest. No second post on upgrade day; that is the one property I'd rather
not learn about from the friend.

Everything is written in a single `BEGIN IMMEDIATE` transaction. Any error
rolls the lot back and raises `ImportFailedError`: a half import would be
worse than none, because the next start would see a `guilds` row and never
try again.

This module also holds `resync_lounge_from_config` (owner decision D5): while
a `lounge:` block stays in `config.yaml`, it is re-applied to the imported
guild's `guild_lounge` row at every start, so the owner can still edit the
welcome and the quote sources until a command exists for it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from newsbot.config import AppConfig, LegacySetup, LoungeCfg
from newsbot.lounge.default_sources import DEFAULT_WIKIQUOTE_PAGES
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import LoungeSettings

log = logging.getLogger(__name__)

IMPORT_STATE_KEY = "import"

# Said verbatim in the log once the import has run. The last entry is a
# reminder, not a key: topics and sources only become deletable once the
# config has a `catalog:` of its own.
_DELETABLE_KEYS = (
    "guild_id, digest.time, digest.timezone, topics[].channel_id, alerts.enabled, "
    "alerts.channel_id, lounge (plus topics and sources once catalog: is in place)"
)


class ImportFailedError(Exception):
    """The import hit a problem and rolled back; nothing was written."""


@dataclass(frozen=True)
class ImportReport:
    """What the import wrote. Saved as JSON in `app_state`, and logged.

    Holds no welcome text and no secrets: it goes into the log.
    """

    guild_id: int
    digest_time: str
    timezone: str
    admin_channel_id: int | None
    games: list[tuple[str, int]]
    shift: dict[str, Any] | None
    lounge: dict[str, Any] | None
    digests_adopted: int
    code_posts: int
    quotes_backfilled: int
    imported_at: str

    def owner_notice(self) -> str:
        """The one short line the first `on_ready` sends the owner channel."""
        parts = [f"{len(self.games)} games"]
        if self.shift is not None:
            parts.append("SHiFT")
        if self.lounge is not None:
            parts.append("lounge")
        return (
            f"Imported the v2 setup for this server ({', '.join(parts)}); "
            "the details are in the log."
        )


def _now_iso(now: Callable[[], datetime] | None) -> str:
    return (now or (lambda: datetime.now(UTC)))().isoformat()


def _quote_sources(lounge: LoungeCfg) -> list[dict[str, str]]:
    """The quote sources as stored: resolved, with the built-in list written out.

    Materializing the default list means a later change to the built-in
    list can't silently change the friend's quotes.
    """
    sources = lounge.daily_quote.sources
    if sources is None:
        return [{"kind": "wikiquote", "value": page} for page in DEFAULT_WIKIQUOTE_PAGES]
    return [{"kind": s.kind, "value": s.value} for s in sources]


def _lounge_settings(
    guild_id: int, lounge: LoungeCfg, last_quote_date: str | None
) -> LoungeSettings | None:
    """The `guild_lounge` row a `lounge:` block describes, or None if it describes nothing.

    "Nothing" means both features are off, or there's no channel to post in
    (which the loader only allows when both are off).
    """
    welcome, quote = lounge.welcome, lounge.daily_quote
    if not (welcome.enabled or quote.enabled) or lounge.channel_id is None:
        return None
    return LoungeSettings(
        guild_id=guild_id,
        channel_id=lounge.channel_id,
        welcome_enabled=welcome.enabled,
        welcome_message=welcome.message,
        quote_enabled=quote.enabled,
        quote_time=quote.time,
        quote_sources=_quote_sources(lounge),
        last_quote_date=last_quote_date,
    )


def _alert_state(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM alert_state")}


# The write steps are separate functions, in the order they run, so the
# failure test can make one of them blow up and prove the rest rolled back.
# Every one of them writes through `conn` without committing.


def _write_guild(conn: sqlite3.Connection, legacy: LegacySetup, now_iso: str) -> None:
    conn.execute(
        "INSERT INTO guilds (guild_id, digest_time, timezone, admin_channel_id, tier, "
        "set_up, joined_at, imported_at, updated_at) VALUES (?, ?, ?, ?, 'comped', 1, ?, ?, ?)",
        (
            legacy.guild_id,
            legacy.digest_time,
            legacy.timezone,
            legacy.admin_channel_id,
            now_iso,
            now_iso,
            now_iso,
        ),
    )


def _write_games(conn: sqlite3.Connection, legacy: LegacySetup) -> None:
    conn.executemany(
        "INSERT INTO guild_games (guild_id, game_key, channel_id) VALUES (?, ?, ?)",
        [(legacy.guild_id, key, channel_id) for key, channel_id in legacy.games],
    )


def _write_shift(
    conn: sqlite3.Connection, legacy: LegacySetup, state: dict[str, str], now_iso: str
) -> dict[str, Any]:
    """Carry today's spent ping budget over too, so the friend doesn't get a fresh three."""
    ping_day = state.get("ping_day")
    ping_count = int(state.get("ping_count") or 0)
    conn.execute(
        "INSERT INTO guild_shift (guild_id, enabled, channel_id, ping, enabled_at, "
        "ping_day, ping_count) VALUES (?, 1, ?, ?, ?, ?, ?)",
        (
            legacy.guild_id,
            legacy.shift_channel_id,
            legacy.shift_ping,
            now_iso,
            ping_day,
            ping_count,
        ),
    )
    return {
        "channel_id": legacy.shift_channel_id,
        "ping": legacy.shift_ping,
        "ping_day": ping_day,
        "ping_count": ping_count,
    }


def _write_lounge(conn: sqlite3.Connection, lounge: LoungeSettings) -> None:
    conn.execute(
        "INSERT INTO guild_lounge (guild_id, channel_id, welcome_enabled, welcome_message, "
        "quote_enabled, quote_time, quote_sources, last_quote_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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


def _do_import(conn: sqlite3.Connection, legacy: LegacySetup, now_iso: str) -> ImportReport:
    """Every write, in order, inside the caller's open transaction."""
    if len(legacy.games) > repo.MAX_GAMES_PER_GUILD:
        raise ImportFailedError(
            f"the v2 config follows {len(legacy.games)} games but a server can follow at most "
            f"{repo.MAX_GAMES_PER_GUILD}; trim topics: in config.yaml and start again "
            "(nothing was imported)"
        )
    state = _alert_state(conn)

    _write_guild(conn, legacy, now_iso)
    _write_games(conn, legacy)
    shift = _write_shift(conn, legacy, state, now_iso) if legacy.shift_enabled else None

    lounge_row = None
    if legacy.lounge is not None:
        row = conn.execute("SELECT value FROM lounge_state WHERE key = 'last_quote_date'")
        found = row.fetchone()
        lounge_row = _lounge_settings(
            legacy.guild_id, legacy.lounge, found["value"] if found else None
        )
    if lounge_row is not None:
        _write_lounge(conn, lounge_row)

    digests = repo.adopt_orphan_digests_in_tx(conn, legacy.guild_id)
    code_posts = repo.backfill_guild_code_posts(conn, legacy.guild_id)
    quotes = repo.backfill_lounge_quotes_guild(conn, legacy.guild_id)

    report = ImportReport(
        guild_id=legacy.guild_id,
        digest_time=legacy.digest_time,
        timezone=legacy.timezone,
        admin_channel_id=legacy.admin_channel_id,
        games=list(legacy.games),
        shift=shift,
        lounge=(
            {
                "channel_id": lounge_row.channel_id,
                "welcome_enabled": lounge_row.welcome_enabled,
                "quote_enabled": lounge_row.quote_enabled,
                "quote_time": lounge_row.quote_time,
                "source_count": len(lounge_row.quote_sources),
                "last_quote_date": lounge_row.last_quote_date,
            }
            if lounge_row is not None
            else None
        ),
        digests_adopted=digests,
        code_posts=code_posts,
        quotes_backfilled=quotes,
        imported_at=now_iso,
    )
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (IMPORT_STATE_KEY, json.dumps(asdict(report))),
    )
    return report


def _log_report(report: ImportReport) -> None:
    log.info(
        "import: guild %s, comped, set up; digest at %s %s; admin channel %s",
        report.guild_id,
        report.digest_time,
        report.timezone,
        report.admin_channel_id,
    )
    log.info(
        "import: %d games: %s",
        len(report.games),
        ", ".join(f"{key} -> {channel}" for key, channel in report.games),
    )
    if report.shift is not None:
        log.info(
            "import: SHiFT on in channel %s, ping %s, today's pings spent %d (day %s)",
            report.shift["channel_id"],
            report.shift["ping"],
            report.shift["ping_count"],
            report.shift["ping_day"],
        )
    else:
        log.info("import: SHiFT alerts were off; no guild_shift row")
    if report.lounge is not None:
        log.info(
            "import: lounge in channel %s, welcome %s, quote %s at %s, %d quote sources, "
            "last quote %s",
            report.lounge["channel_id"],
            "on" if report.lounge["welcome_enabled"] else "off",
            "on" if report.lounge["quote_enabled"] else "off",
            report.lounge["quote_time"],
            report.lounge["source_count"],
            report.lounge["last_quote_date"],
        )
    else:
        log.info("import: lounge was off or absent; no guild_lounge row")
    log.info(
        "import: adopted %d digest rows, %d already-posted SHiFT codes, %d lounge quote rows",
        report.digests_adopted,
        report.code_posts,
        report.quotes_backfilled,
    )
    log.info(
        "these keys are now only read by the import and can be deleted from config.yaml: %s",
        _DELETABLE_KEYS,
    )


def _log_already_imported(value: str) -> None:
    try:
        when = json.loads(value)["imported_at"]
    except ValueError, KeyError, TypeError:
        when = "an unknown date"
    log.info(
        "import: already happened on %s; these keys can be deleted from config.yaml: %s",
        when,
        _DELETABLE_KEYS,
    )


def ensure_imported(
    db_path: str | Path,
    cfg: AppConfig,
    now: Callable[[], datetime] | None = None,
) -> ImportReport | None:
    """Import the v2 setup if this is the first v3 start; otherwise do nothing.

    Runs only when the config has a legacy setup (a `guild_id`), the
    `app_state` import marker is absent, and the `guilds` table is empty.
    Both are checked again under the write lock so two processes starting
    together produce exactly one import. The import happens once, ever: the
    marker outlives the guild row. Returns the report, or None when there
    was nothing to do.

    Raises `ImportFailedError` if anything goes wrong; the database is then
    exactly as it was.
    """
    legacy = cfg.legacy
    if legacy is None:
        return None
    now_iso = _now_iso(now)
    with closing(connect(db_path)) as conn:
        # Autocommit, so the BEGIN below is the only transaction there is and
        # sqlite3 doesn't helpfully start (or end) one of its own.
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            # The import happens once, ever. If the bot gets removed from the
            # friend's server the guild rows cascade away, but the marker
            # stays, and I'd much rather do nothing than resurrect a server
            # we may have been kicked out of (or crash-loop trying). The
            # guilds check is a second belt for the belt.
            done = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (IMPORT_STATE_KEY,)
            ).fetchone()
            if done is not None:
                conn.execute("ROLLBACK")
                _log_already_imported(done["value"])
                return None
            if conn.execute("SELECT COUNT(*) FROM guilds").fetchone()[0]:
                conn.execute("ROLLBACK")
                return None
            report = _do_import(conn, legacy, now_iso)
            conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, ImportFailedError):
                raise
            raise ImportFailedError(
                f"the v2 import failed and was rolled back, nothing was written: {exc}"
            ) from exc
    _log_report(report)
    return report


def resync_lounge_from_config(db_path: str | Path, cfg: AppConfig) -> bool:
    """D5: re-apply a `lounge:` block from config to the imported guild's lounge row.

    Runs at every start while the block (and the `guild_id` it hangs off)
    is still in `config.yaml`. The row's `last_quote_date` is never touched,
    so a restart can't earn anyone a second quote. Writes nothing if the row
    already matches, and logs only when it changed something. Returns True
    if it wrote. Delete the block and the database row stands alone.
    """
    legacy = cfg.legacy
    if legacy is None or legacy.lounge is None:
        return False
    with closing(connect(db_path)) as conn:
        imported = conn.execute("SELECT guild_id FROM guilds WHERE imported_at IS NOT NULL")
        found = imported.fetchone()
        if found is None:
            return False
        guild_id = found["guild_id"]
        existing = repo.get_lounge(conn, guild_id)
        # A row we're about to create starts from v2.2's date, like the import
        # would have; an existing row keeps its own.
        last_quote_date = (
            existing.last_quote_date if existing else repo.get_lounge_state(conn).last_quote_date
        )
        wanted = _lounge_settings(guild_id, legacy.lounge, last_quote_date)
        if wanted is None:
            # Both features off: switch an existing row off, but there's
            # nothing to create and nothing to say if there's no row.
            if existing is None or not (existing.welcome_enabled or existing.quote_enabled):
                return False
            wanted = LoungeSettings(
                guild_id=guild_id,
                channel_id=existing.channel_id,
                welcome_enabled=False,
                welcome_message=legacy.lounge.welcome.message or existing.welcome_message,
                quote_enabled=False,
                quote_time=legacy.lounge.daily_quote.time,
                quote_sources=_quote_sources(legacy.lounge),
                last_quote_date=existing.last_quote_date,
            )
        if wanted == existing:
            return False
        repo.upsert_lounge(conn, wanted)
    log.info(
        "lounge: re-synced guild %s from the lounge: block in config.yaml "
        "(channel %s, welcome %s, quote %s at %s, %d quote sources)",
        guild_id,
        wanted.channel_id,
        "on" if wanted.welcome_enabled else "off",
        "on" if wanted.quote_enabled else "off",
        wanted.quote_time,
        len(wanted.quote_sources),
    )
    return True
