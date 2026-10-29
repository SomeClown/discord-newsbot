"""Count the links the bot posts, from a backup, so a URL shortener can be priced.

There are two numbers and they behave differently. *Unique links* are distinct article
URLs: a shortener makes one short link per URL and every server reuses it, so this grows
with the catalog and not with the number of servers. *Links posted* are every link in
every message the bot sent (each server's digests, SHiFT alerts, roundups), and this one
grows with servers. The report gives both by month, then today's shape (servers, games per
server, links per server by tier) and a projection at 10 to 200 set-up servers.

The database keeps what each digest covered (item id ranges) and which games it posted, but
not what the post said. So each posted digest is rebuilt from its inputs with the repo's
own readers, run through the real renderer (`render_guild_digest`), and the links are
counted in the embed that comes out. That is the closest a backup gets. It is not exact,
and the report ends with a list of what it guesses at. This script and the word "exact"
have never been introduced.

It only reads. The file is opened with SQLite's `mode=ro&immutable=1` (the second flag
because `.backup` copies keep the WAL flag, and a plain read-only open of one wants to
create `-shm` and `-wal` files next to it, which a read-only mount refuses and a writable
one would not appreciate). Immutable means it trusts the file not to change under it, so
point it at a backup, never the live database; a non-empty `-wal` next to the file is
refused for that reason. It prints numbers only: no server names, message text or URLs.

On the Droplet, through the published image (the image has `newsbot/` but not
`scripts/`, so the script is mounted in; uid 10001 owns the backups):

    docker run --rm -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/data/backups:/backups:ro" \\
        ghcr.io/someclown/discord-newsbot:<TAG> \\
        python scripts/link_counts.py /backups/newsbot-YYYY-MM-DD.db

From a checkout with the venv active:

    python scripts/link_counts.py data/backups/newsbot-YYYY-MM-DD.db [--days 7]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from newsbot.bot.format import _shown_link, render_guild_digest
from newsbot.guilds.schedule import item_floor
from newsbot.industry import INDUSTRY_KEY, INDUSTRY_MAX_STORIES
from newsbot.pipeline.guild_digest import summary_from_row
from newsbot.pipeline.summarize import _FALLBACK_NOTE
from newsbot.store import repo
from newsbot.store.models import Coverage

# The first migration with everything this reads (digests' item ranges, migration 007).
MIN_VERSION = 7
# A month, for scaling a window of days to "per month".
MONTH_DAYS = 30
# How many days the per-day table shows.
DAILY_ROWS = 14
# The server counts the projection table is asked for.
PROJECTION_SERVERS = (10, 25, 50, 75, 100, 200)

# What a rendered link looks like: `<url>`, bare (SHiFT alerts) or inside the parentheses of a
# masked `[domain](<url>)` (digests, since v3.4.0). Text from feeds goes through `esc()` first,
# which defuses a scheme, so a title can't pose as one.
_LINK_RE = re.compile(r"<(https?://[^>\s]+)>")

# Link categories. SHiFT has three; a digest has two (the industry post is its own thing).
GAMES, INDUSTRY = "games", "industry"
SHIFT_ALERT, SHIFT_ROUNDUP, SHIFT_FOLLOWUP = "shift_alert", "shift_roundup", "shift_followup"
SHIFT_CATS = (SHIFT_ALERT, SHIFT_ROUNDUP, SHIFT_FOLLOWUP)
TIERS = ("free", "comped")


@dataclass(frozen=True)
class _Game:
    """A game as the renderer wants it. The name only reaches the embed title, never a link."""

    key: str
    name: str


class Tally:
    """Everything counted so far: links by (month, tier, category), plus one window's worth."""

    def __init__(self, window_days: set[str]) -> None:
        self.window_days = window_days
        self.by_month: Counter[tuple[str, str, str]] = Counter()
        self.in_window: Counter[tuple[str, str]] = Counter()
        self.server_days: Counter[str] = Counter()
        self.first_posted: dict[str, str] = {}
        # Per month: game posts the digest rows record, and how many could be rebuilt.
        self.posts_recorded: Counter[str] = Counter()
        self.posts_rebuilt: Counter[str] = Counter()
        self.digests_without_window = 0
        self.comped_headline_posts = 0

    def add(self, day: str, tier: str, category: str, links: int, urls: list[str]) -> None:
        """Count `links` in `category` on `day` (an ISO date), and remember which URLs went out."""
        self.by_month[(day[:7], tier, category)] += links
        if day in self.window_days:
            self.in_window[(tier, category)] += links
        for url in urls:
            seen = self.first_posted.get(url)
            if seen is None or day < seen:
                self.first_posted[url] = day


# --- Opening the file ---


class RefusedPath(Exception):
    """The path can't be read the way this script promises to read it."""


def open_read_only(path: str | Path) -> sqlite3.Connection:
    """Open `path` read-only without creating, locking or writing anything.

    Refuses a path that isn't an existing file (SQLite would happily create it), and a file
    with a non-empty `-wal` beside it: immutable mode ignores that log, so the numbers
    would quietly leave out whatever is still in it.
    """
    file = Path(path).expanduser().resolve()
    if not file.is_file():
        raise RefusedPath("no such database file (this script never creates one)")
    wal = Path(f"{file}-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise RefusedPath(
            "a write-ahead log sits beside this file: use a backup, not a live database"
        )
    conn = sqlite3.connect(f"file:{quote(str(file))}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- Digests ---


def _keys(raw: str | None) -> set[str]:
    """The game keys in a JSON object column (`posted_by_game`, `game_items_upto`)."""
    return set(_mapping(raw))


def _mapping(raw: str | None) -> dict:
    try:
        loaded = json.loads(raw or "{}")
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _marks_before(
    conn: sqlite3.Connection, prev: sqlite3.Row | None, row: sqlite3.Row, games: set[str]
) -> dict[str, int | None]:
    """Where each game's news left off before `row`'s digest: the previous digest's mark per game.

    `repo.last_coverage` answers this for the newest digest only (it has no "before this
    date" and always looks at the last five), so this follows the same rules for one
    step back. A game the previous digest didn't follow has no mark (`None`: a fresh game,
    which takes the first-digest floor).
    """
    if prev is None:
        return dict.fromkeys(games)
    newest = repo.latest_item_id(conn)
    prev_end = datetime.fromisoformat(prev["window_end"])
    prev_upto = prev["items_upto"]
    if prev_upto is None or prev_upto > newest:
        prev_upto = repo.latest_item_id(conn, collected_by=prev_end)
    posted, stored = _keys(prev["posted_by_game"]), _mapping(prev["game_items_upto"])
    legacy = not posted and not stored
    marks: dict[str, int | None] = {}
    for key in games:
        mark = stored.get(key)
        if isinstance(mark, int) and not isinstance(mark, bool):
            marks[key] = mark if mark <= newest else prev_upto
        elif key in posted or legacy:
            marks[key] = prev_upto
        elif key in stored:
            marks[key] = row[
                "items_after"
            ]  # followed but not reached; the real rule looks further back
        else:
            marks[key] = None
    return marks


def _coverage(prev: sqlite3.Row | None, mark: int | None) -> Coverage | None:
    if prev is None or mark is None:
        return None
    return Coverage(datetime.fromisoformat(prev["window_end"]), mark)


def _rebuild_digest(
    conn: sqlite3.Connection,
    guild_id: int,
    tier: str,
    row: sqlite3.Row,
    prev: sqlite3.Row | None,
    tally: Tally,
) -> None:
    """Rebuild one posted digest from its inputs and count the links in what the renderer gives."""
    posted = _keys(row["posted_by_game"])
    run_day = row["run_date"]
    month = run_day[:7]
    tally.posts_recorded[month] += len(posted)
    window_end = datetime.fromisoformat(row["window_end"])
    upto = row["items_upto"]
    if upto is None:
        upto = repo.latest_item_id(conn, collected_by=window_end)
    after = row["items_after"]
    marks = _marks_before(conn, prev, row, posted)

    items_by_game = repo.items_for_window(
        conn,
        guild_id,
        item_floor(window_end, after is not None),
        window_end,
        after_id=after,
        upto_id=upto,
        after_by_game=marks,
        fresh_floor=item_floor(window_end, False),
    )
    stories_by_game: dict = {}
    notes_by_game: dict[str, str | None] = {}
    picks_by_game: dict = {}
    for key in sorted(posted):
        coverage = _coverage(prev, marks.get(key))
        if tier == "comped":
            summary = repo.get_game_summary(conn, key, window_end, coverage)
            if summary is not None and summary.status == "ok":
                stories_by_game[key] = summary_from_row(conn, summary).stories
            else:
                notes_by_game[key] = _FALLBACK_NOTE
                tally.comped_headline_posts += 1
        elif key == INDUSTRY_KEY:
            pick = repo.get_game_summary(conn, key, window_end, coverage)
            if pick is not None and pick.status == "ok":
                stories = summary_from_row(conn, pick).stories
                urls = list(dict.fromkeys(s.item_urls[0] for s in stories if s.item_urls))
                found = repo.headline_items_by_url(conn, urls)
                picks_by_game[key] = [found[u] for u in urls if u in found][:INDUSTRY_MAX_STORIES]

    rendered = render_guild_digest(
        date.fromisoformat(run_day),
        [_Game(key, key) for key in sorted(posted)],
        dict.fromkeys(posted, 0),
        stories_by_game=stories_by_game,
        items_by_game=items_by_game,
        notes_by_game=notes_by_game,
        coverage_notes=[],
        picks_by_game=picks_by_game,
        capped={INDUSTRY_KEY: INDUSTRY_MAX_STORIES} if INDUSTRY_KEY in posted else {},
    )
    for message in rendered.messages:
        urls = _LINK_RE.findall(message.embed.description or "")
        category = INDUSTRY if message.topic_key == INDUSTRY_KEY else GAMES
        tally.posts_rebuilt[month] += 1
        tally.add(run_day, tier, category, len(urls), urls)


def _count_digests(conn: sqlite3.Connection, tally: Tally) -> None:
    guilds = {r["guild_id"]: r["tier"] for r in conn.execute("SELECT guild_id, tier FROM guilds")}
    for guild_id, tier in guilds.items():
        prev: sqlite3.Row | None = None
        rows = conn.execute(
            "SELECT run_date, status, posted_by_game, window_end, items_after, items_upto, "
            "game_items_upto FROM digests WHERE guild_id = ? ORDER BY run_date",
            (guild_id,),
        ).fetchall()
        for row in rows:
            posted = _keys(row["posted_by_game"])
            if row["window_end"] is None:
                tally.digests_without_window += bool(posted)
                continue
            counted = row["status"] in ("ok", "partial") or bool(posted)
            if counted and row["run_date"] in tally.window_days:
                tally.server_days[tier] += 1
            if posted:
                _rebuild_digest(conn, guild_id, tier, row, prev, tally)
            if counted:
                prev = row


# --- SHiFT ---


def _count_shift(conn: sqlite3.Connection, tally: Tally) -> None:
    """SHiFT links from the delivery records, one rule per message shape.

    An alert shows a link under each code (its source's post); a roundup shows one in its
    header, shared by the group of codes that came from one post; a follow-up shows none.
    """
    tiers = {r["guild_id"]: r["tier"] for r in conn.execute("SELECT guild_id, tier FROM guilds")}
    roundups: set[tuple[int, str, str]] = set()
    for r in conn.execute(
        "SELECT p.guild_id, p.from_roundup, p.claimed_at, a.item_url "
        "FROM guild_code_posts p JOIN alerted_codes a ON a.code = p.code "
        "WHERE p.status = 'posted'"
    ):
        link = _shown_link(r["item_url"])
        day, tier = r["claimed_at"][:10], tiers[r["guild_id"]]
        if r["from_roundup"]:
            roundups.add((r["guild_id"], r["claimed_at"], r["item_url"]))
        else:
            tally.add(day, tier, SHIFT_ALERT, int(link is not None), [link] if link else [])
    for guild_id, claimed_at, item_url in roundups:
        link = _shown_link(item_url)
        tally.add(
            claimed_at[:10],
            tiers[guild_id],
            SHIFT_ROUNDUP,
            int(link is not None),
            [link] if link else [],
        )
    for r in conn.execute(
        "SELECT guild_id, COALESCE(claimed_at, queued_at) AS at FROM guild_code_followups "
        "WHERE status = 'posted'"
    ):
        # The follow-up message is a ping, a prompt line and fenced codes. No links.
        tally.add(r["at"][:10], tiers[r["guild_id"]], SHIFT_FOLLOWUP, 0, [])


# --- The report ---


def _full_day_anchor(conn: sqlite3.Connection) -> date | None:
    """The newest run date that looks complete.

    That is the newest, unless fewer digests ran on it than the day before (a backup taken
    before the morning's digests).
    """
    rows = conn.execute(
        "SELECT run_date, COUNT(*) AS n FROM digests WHERE window_end IS NOT NULL "
        "GROUP BY run_date ORDER BY run_date DESC LIMIT 2"
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 2 and rows[0]["n"] < rows[1]["n"]:
        return date.fromisoformat(rows[1]["run_date"])
    return date.fromisoformat(rows[0]["run_date"])


def _n(value: float) -> str:
    return f"{round(value):,}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    out = []
    for r in [header, *rows]:
        cells = [r[0].ljust(widths[0])] + [
            c.rjust(w) for c, w in zip(r[1:], widths[1:], strict=True)
        ]
        out.append("  " + "  ".join(cells).rstrip())
    return out


def _unique_section(
    conn: sqlite3.Connection, tally: Tally, newest: date | None
) -> tuple[list[str], tuple[float, float] | None]:
    """The unique-links section, and the per-month rates the projection uses.

    The rates are (stored items, URLs first posted), both from the seven full days before the
    newest one and scaled to 30; `None` if there are no items.
    """
    lines = [
        "1. UNIQUE LINKS (distinct article URLs; one short link each, shared by every server)",
        "",
    ]
    stored_months = {
        r["m"]: r["n"]
        for r in conn.execute(
            "SELECT substr(collected_at, 1, 7) AS m, COUNT(*) AS n FROM items GROUP BY 1"
        )
    }
    if not stored_months or newest is None:
        return [*lines, "  No items stored.", ""], None
    posted_months = Counter(day[:7] for day in tally.first_posted.values())
    lines += [
        "Stored items by month collected (what the owner's SQL counts), and the distinct URLs",
        "a server posted, in the month of the first post that carried each. 'Posted' includes",
        "the source pages behind SHiFT codes, which aren't always stored items:",
        "",
    ]
    lines += _table(
        ["month", "stored", "posted"],
        [
            [m, _n(stored_months.get(m, 0)), _n(posted_months.get(m, 0))]
            for m in sorted(stored_months.keys() | posted_months.keys())
        ],
    )
    lines += [
        "",
        "  The first month is partial (items age out after 90 days, and Reddit items were",
        "  purged); the last is partial too, because the backup was taken mid-month.",
        "",
    ]

    first = newest - timedelta(days=DAILY_ROWS - 1)
    per_day = {
        r["d"]: r["n"]
        for r in conn.execute(
            "SELECT substr(collected_at, 1, 10) AS d, COUNT(*) AS n FROM items "
            "WHERE collected_at >= ? GROUP BY 1",
            (first.isoformat(),),
        )
    }
    posted_days = Counter(d for d in tally.first_posted.values())
    lines += [f"Last {DAILY_ROWS} days (the final day may be partial):", ""]
    lines += _table(
        ["day", "stored", "first posted"],
        [
            [
                d.isoformat(),
                _n(per_day.get(d.isoformat(), 0)),
                _n(posted_days.get(d.isoformat(), 0)),
            ]
            for d in (first + timedelta(days=i) for i in range(DAILY_ROWS))
        ],
    )
    full = [first + timedelta(days=i) for i in range(DAILY_ROWS - 1)]
    last7 = sum(per_day.get(d.isoformat(), 0) for d in full[-7:])
    last14 = sum(per_day.get(d.isoformat(), 0) for d in full[: DAILY_ROWS - 1])
    first7 = sum(posted_days.get(d.isoformat(), 0) for d in full[-7:])
    rate = (last7 / 7 * MONTH_DAYS, first7 / 7 * MONTH_DAYS)
    lines += [
        "",
        "  Rate from the 7 full days before the last:",
        f"    stored: {_n(last7 / 7)} a day, {_n(rate[0])} a month",
        f"    first posted: {_n(first7 / 7)} a day, {_n(rate[1])} a month",
        f"  From the {DAILY_ROWS - 1} full days before the last: "
        f"{_n(last14 / (DAILY_ROWS - 1))} a day, "
        f"{_n(last14 / (DAILY_ROWS - 1) * MONTH_DAYS)} a month.",
        "  The catalog grew in October (five games on the 21st, thin-game sources on the 23rd,",
        "  the industry entry on the 24th), so older days understate the rate.",
        "",
    ]
    return lines, rate


def _posted_section(tally: Tally) -> list[str]:
    months = sorted({m for m, _, _ in tally.by_month} | set(tally.posts_recorded))
    lines = ["2. LINKS POSTED (every link in every message, all servers)", ""]
    if not months:
        return [*lines, "  Nothing posted.", ""]

    def cell(month: str, tiers: tuple[str, ...], cats: tuple[str, ...]) -> str:
        return _n(sum(tally.by_month[(month, t, c)] for t in tiers for c in cats))

    spec = [
        ("free game headlines", ("free",), (GAMES,)),
        ("comped game stories", ("comped",), (GAMES,)),
        ("industry, free", ("free",), (INDUSTRY,)),
        ("industry, comped", ("comped",), (INDUSTRY,)),
        ("SHiFT alerts", TIERS, (SHIFT_ALERT,)),
        ("SHiFT roundups", TIERS, (SHIFT_ROUNDUP,)),
        ("SHiFT follow-ups", TIERS, (SHIFT_FOLLOWUP,)),
        ("total", TIERS, (GAMES, INDUSTRY, *SHIFT_CATS)),
    ]
    rows = [[label, *(cell(m, tiers, cats) for m in months)] for label, tiers, cats in spec]
    rows.append(["digest posts recorded", *(_n(tally.posts_recorded[m]) for m in months)])
    rows.append(["digest posts rebuilt", *(_n(tally.posts_rebuilt[m]) for m in months)])
    lines += _table(["", *months], rows)
    lines += [
        "",
        "  Digest links come from rebuilding each digest (see the approximations below);",
        "  SHiFT links come from the delivery records. A SHiFT follow-up message has no links.",
        '  "Comped game stories" includes a comped post that fell back to headlines.',
        "",
    ]
    return lines


def _shape_section(
    conn: sqlite3.Connection, tally: Tally, days: int, window: list[date]
) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Today's servers and games, the per-server rates by tier, and the rates for the projection."""
    lines = ["3. TODAY'S SHAPE", ""]
    guilds = conn.execute(
        "SELECT g.guild_id, g.tier, g.set_up, COALESCE(s.enabled, 0) AS shift "
        "FROM guilds g LEFT JOIN guild_shift s ON s.guild_id = g.guild_id"
    ).fetchall()
    set_up = [g for g in guilds if g["set_up"]]
    counts = {t: sum(1 for g in set_up if g["tier"] == t) for t in TIERS}
    lines += [
        f"  servers: {len(guilds)} total, {len(set_up)} set up",
        f"  set up by tier: {counts['free']} free, {counts['comped']} comped",
        f"  set up with SHiFT alerts on: {sum(1 for g in set_up if g['shift'])}",
        "",
    ]
    followed = Counter()
    industry = 0
    for r in conn.execute("SELECT guild_id, game_key FROM guild_games"):
        if r["game_key"] == INDUSTRY_KEY:
            industry += 1
        else:
            followed[r["guild_id"]] += 1
    per_server = [followed.get(g["guild_id"], 0) for g in set_up]
    if per_server:
        average = sum(per_server) / len(per_server)
        lines.append(f"  games followed per set-up server (not industry): average {average:.1f}")
        for count, servers in sorted(Counter(per_server).items()):
            lines.append(
                f"    {count} game{'s' * (count != 1)}: {servers} server{'s' * (servers != 1)}"
            )
    lines += [f"  following the industry entry: {industry}", ""]

    rates: dict[str, dict[str, float]] = {}
    if not window:
        return [*lines, "  No digests recorded, so no per-server rates.", ""], rates
    lines.append(
        f"Links posted per set-up server per month, from {window[0].isoformat()} to "
        f"{window[-1].isoformat()} ({days} days) scaled to {MONTH_DAYS}:"
    )
    lines.append("")
    rows = []
    groups = [
        ("games (headlines or stories)", (GAMES,)),
        ("industry", (INDUSTRY,)),
        ("SHiFT", SHIFT_CATS),
    ]
    for tier in TIERS:
        sd = tally.server_days[tier]
        rates[tier] = {
            label: (sum(tally.in_window[(tier, c)] for c in cats) / sd * MONTH_DAYS if sd else 0.0)
            for label, cats in groups
        }
    for label, _ in groups:
        rows.append(
            [label, *(_n(rates[t][label]) if tally.server_days[t] else "n/a" for t in TIERS)]
        )
    rows.append(
        ["total", *(_n(sum(rates[t].values())) if tally.server_days[t] else "n/a" for t in TIERS)]
    )
    rows.append(["server-days in the window", *(_n(tally.server_days[t]) for t in TIERS)])
    lines += _table(["", *TIERS], rows)
    lines += [
        "",
        "  A server-day is one digest that posted or finished, so a server that joined",
        "  mid-window counts only its own days.",
        "",
    ]
    return lines, rates


def _projection_section(
    tally: Tally,
    rates: dict[str, dict[str, float]],
    counts_comped: int,
    unique_rate: tuple[float, float] | None,
) -> list[str]:
    lines = ["4. PROJECTION (per month)", ""]
    free = sum(rates.get("free", {}).values()) if tally.server_days["free"] else None
    comped = sum(rates.get("comped", {}).values()) if tally.server_days["comped"] else None
    if free is None:
        return [
            *lines,
            "  No free servers in the window, so there is no free-server rate to scale.",
            "",
        ]
    rows = []
    for n in PROJECTION_SERVERS:
        kept = min(counts_comped, n)
        mixed = kept * (comped or 0.0) + (n - kept) * free
        rows.append(
            [
                str(n),
                _n(n * free),
                _n(mixed),
                _n(unique_rate[0]) if unique_rate is not None else "n/a",
                _n(unique_rate[1]) if unique_rate is not None else "n/a",
            ]
        )
    lines += _table(
        [
            "set-up servers",
            "links posted",
            "keeping today's comped",
            "unique (stored)",
            "unique (posted)",
        ],
        rows,
    )
    lines += [
        "",
        "  Assumptions:",
        "  - Links posted: every new server looks like today's free ones (the free column of",
        "    section 3: same games per server, same SHiFT rate). The third column keeps the",
        f"    {counts_comped} comped server(s) at their own rate and makes the rest free-like.",
        "  - Unique links ignore server count: the catalog's rate from section 1 (stored items",
        "    a month, and the subset that got posted). Servers that follow games nobody follows",
        "    today would add to it; servers that follow the same games add nothing.",
        "  - Rates come from a short window, scaled to 30 days; quiet or busy weeks move them.",
        "",
    ]
    return lines


APPROXIMATIONS = [
    "Follows are today's. A server that followed other games on an earlier day is rebuilt "
    "with its current games: games it has since dropped contribute nothing, and games it has "
    "since added are rebuilt from items the earlier digest never held.",
    "Tier is today's. A server that was comped and isn't (or the reverse) is counted in its "
    "current tier for every month.",
    "Digests are rebuilt from the item ranges they recorded, but items and summaries age out "
    "after 90 days. A post whose items or summary are gone counts as fewer links (or none): "
    "the 'digest posts rebuilt' row shows how many recorded posts came back.",
    "A comped post's summary is the one the repo says that server's digest would reuse, looked "
    "up now. If none qualifies the post counts as headlines, and a later summary for the same "
    "window can stand in for the one that was used.",
    "Each game's starting point comes from the previous digest's recorded marks. The one rule "
    "not copied is the real reader's habit of looking further back past a game a digest "
    "never reached.",
    "Digests that predate item ranges (v2.x rows) have no window and are not rebuilt.",
    "Links are counted as angle-bracketed URLs in the rebuilt embed. Edits after posting are not "
    "seen, nor are messages someone else deleted.",
    "SHiFT: an alert is one link per posted code whose source URL is shown (a link shed to "
    "make a message fit is overcounted); a roundup is one link per source post per claim; a "
    "follow-up has none; alert edits add none.",
    "A SHiFT code counts once per server that posted it, in the month it was claimed. Codes "
    "from before the v3 import appear only if the import copied them.",
    "Months are the digest's local run date for digests and the UTC claim date for SHiFT, so "
    "a day can land on either side of midnight.",
    "Per-server rates use whole days (a partial last day is left out of the window), and "
    "server-days count only digests that finished or posted.",
    "Unique posted links are keyed by the canonical URL the renderer shows, and counted in "
    "the month of their earliest rebuilt post.",
]


def build_report(conn: sqlite3.Connection, days: int = 14) -> str:
    """The whole report for an open connection, as text."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < MIN_VERSION:
        raise RefusedPath(f"database is at migration {version}; this needs {MIN_VERSION} or later")

    anchor = _full_day_anchor(conn)
    window = [anchor - timedelta(days=i) for i in range(days - 1, -1, -1)] if anchor else []
    tally = Tally({d.isoformat() for d in window})
    _count_digests(conn, tally)
    _count_shift(conn, tally)

    newest_row = conn.execute("SELECT MAX(collected_at) AS newest FROM items").fetchone()
    newest = date.fromisoformat(newest_row["newest"][:10]) if newest_row["newest"] else None
    lines = ["newsbot link counts (read-only; numbers only)", ""]
    if newest:
        lines += [f"Newest item collected: {newest.isoformat()}", ""]

    unique_lines, unique_rate = _unique_section(conn, tally, newest)
    shape_lines, rates = _shape_section(conn, tally, days, window)
    comped_now = conn.execute(
        "SELECT COUNT(*) FROM guilds WHERE set_up = 1 AND tier = 'comped'"
    ).fetchone()[0]
    lines += unique_lines
    lines += _posted_section(tally)
    lines += shape_lines
    lines += _projection_section(tally, rates, comped_now, unique_rate)
    lines += ["APPROXIMATIONS", ""]
    notes = list(APPROXIMATIONS)
    if tally.digests_without_window:
        notes.append(
            f"This database: {tally.digests_without_window} digest(s) had no window, skipped."
        )
    if tally.comped_headline_posts:
        notes.append(
            f"This database: {tally.comped_headline_posts} comped game post(s) were rebuilt as "
            "headlines because no usable summary was found."
        )
    lines += [textwrap.fill(n, 96, initial_indent="  - ", subsequent_indent="    ") for n in notes]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Count the links the bot posts, from a backup.")
    parser.add_argument("database", help="a backup file, e.g. data/backups/newsbot-2026-10-29.db")
    parser.add_argument(
        "--days", type=int, choices=(7, 14), default=14, help="window for per-server rates"
    )
    args = parser.parse_args(argv)
    try:
        conn = open_read_only(args.database)
        try:
            report = build_report(conn, args.days)
        finally:
            conn.close()
    except RefusedPath as exc:
        print(f"link_counts: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"link_counts: could not read the database ({exc})", file=sys.stderr)
        return 1
    print(report, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
