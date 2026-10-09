"""SHiFT code tables: read a community page's tables for codes and what they unlock.

A few fan sites keep a tidy list of every SHiFT code with its reward and expiry,
which is more than most posts bother to say. This collector turns one such page
(mentalmars' Borderlands 4 list is the one that started it, design.md §16) into
one `RawItem` per code, each carrying an `ItemDetails`. The page is parsed with
the standard library's `html.parser`, which has opinions about malformed HTML
that I respect and don't share, and the columns are found by their heading
words ("code", "reward", "added", "expir", "source") instead of by position, so
a site that shuffles its columns, or a second site shaped like the first, keeps
working. If the layout changes enough that no code rows turn up, the pass fails
loudly, which is how it ought to be: a source that quietly reads nothing is
worse than one that complains.

Everything on the page is a stranger's text. It's size-capped as it's read
(cells, rows, nesting), the reward and origin are cut so their escaped form fits
an alert, and nothing here escapes anything for Discord; that's the renderer's
job, and it does it at the last moment. A reward of "@everyone" is just a reward
until someone tries to print it unescaped.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Literal, NamedTuple
from zoneinfo import ZoneInfo

import httpx

from newsbot.bot.format import cap_shown
from newsbot.collectors.base import Category, ItemDetails, RawItem
from newsbot.collectors.rss import _fetch
from newsbot.config import ShiftTableSource
from newsbot.shift.match import GOLDEN_RE, find_codes

logger = logging.getLogger(__name__)

# A page that gets past these many rows, cells that get past this many characters
# or tables that nest deeper than this are somebody's idea of a joke. The real
# page has about thirty rows and I'd like to keep it that way. The row limit is
# per table, with a bigger one for the whole page, so a thousand junk rows in
# some unrelated table can't use up the budget the code tables were counting on.
_MAX_ROWS = 1000
_MAX_PAGE_ROWS = 5000
_MAX_CELL_CHARS = 4000
_MAX_TABLE_DEPTH = 8
# Spanning cells are expanded into a grid so column numbers line up. A page that
# asks for a colspan of a million is asking for a memory problem, so the spans
# and the width of the grid are bounded too.
_MAX_COLSPAN = 20
_MAX_ROWSPAN = 100
_MAX_COLUMNS = 64
# The header is the first row in this many that names a code column and another
# role; a title row ("Latest codes!") above the real header shouldn't hide the table.
_HEADER_SEARCH_ROWS = 5

# design.md §16: reward and origin are cut to this many UTF-16 units *as shown*,
# so no page, honest or not, can make an alert entry too big for a message.
_SHOWN_LIMIT = 300

# Dates on the page are Pacific calendar days. (details.SHIFT_TZ will own this
# once that module exists; until then it lives here, and I'll fold them together.)
_PACIFIC = ZoneInfo("America/Los_Angeles")

_SKIPPED_TAGS = frozenset({"script", "style"})
_HEADING_TAGS = frozenset({"h2", "h3", "h4"})
# Tags that end a line of text; a cell with "a<br>b" shouldn't read "ab".
_BREAK_TAGS = frozenset({"br", "p", "div", "li"})

TableKind = Literal["main", "cosmetic", "permanent"]
# Higher is more specific: a code listed in two tables takes the more specific one's category.
_KIND_RANK: dict[TableKind, int] = {"main": 0, "cosmetic": 1, "permanent": 2}

_COSMETIC_REWARD_RE = re.compile(r"\b(cosmetic|skin|emote|trinket|outfit|banner|head)\b", re.I)

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_DATE_RE = re.compile(
    r"^([A-Za-z]{3,9})\.?\s+([0-9]{1,2})(?:st|nd|rd|th)?,?\s+([0-9]{4})$", re.ASCII | re.IGNORECASE
)
# A year outside this range isn't a SHiFT expiry, it's a typo or an attack on
# whatever does date arithmetic later. "Unknown" is a better answer than 9999.
_YEAR_RANGE = range(2000, 2101)


class Validators(NamedTuple):
    """A page's `ETag` and `Last-Modified`, as the server last sent them."""

    etag: str | None
    last_modified: str | None


class PageValidators(dict[str, Validators]):
    """Source URL -> the validators of its last *committed* parse, in two phases.

    The collectors are rebuilt every pass, so the collector itself can't hold
    these; whoever builds them owns one of these and hands it in. It lives in
    memory only: a restart refetches the whole page once, which the site can
    afford.

    Two phases, because "the page parsed" and "the bot did something with what
    it read" are different events. If a later step fails (the sightings don't
    get saved, say) and the next request were conditional anyway, the server
    would answer 304 and the codes we never recorded would stay invisible until
    the page happened to change. So `collect()` only *stages* the new
    validators; the dict itself (what the next request will send) changes when
    the owner calls `commit(url)` or `commit_all()`, after the sightings are
    safely saved. Staged values nobody commits are simply forgotten by the next
    `collect()` for that URL. (Task 5 does the committing; until then nothing
    does, which is safe, just chatty: every pass is a full fetch.)
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # URL -> the validators to commit, or None meaning "the page sent none,
        # so forget the old ones".
        self._pending: dict[str, Validators | None] = {}

    @property
    def pending(self) -> dict[str, Validators | None]:
        """A copy of what's staged and not yet committed."""
        return dict(self._pending)

    def stage(self, url: str, validators: Validators | None) -> None:
        """Remember `validators` for `url` until they're committed (None means "clear")."""
        self._pending[url] = validators

    def discard(self, url: str) -> None:
        """Forget anything staged for `url`; the committed values stay."""
        self._pending.pop(url, None)

    def commit(self, url: str) -> None:
        """Make what's staged for `url` current. Nothing staged means nothing changes."""
        if url not in self._pending:
            return
        staged = self._pending.pop(url)
        if staged is None:
            self.pop(url, None)
        else:
            self[url] = staged

    def commit_all(self) -> None:
        """`commit` for every URL with something staged."""
        for url in list(self._pending):
            self.commit(url)


@dataclass(frozen=True, slots=True)
class ParsedTable:
    """One table, as text: its kind (from the heading above it) and its rows of cells."""

    kind: TableKind
    rows: list[list[str]]
    order: int = 0


@dataclass(slots=True)
class _OpenTable:
    kind: TableKind
    order: int
    rows: list[list[str]] = field(default_factory=list)
    # The row being built: (text, colspan, rowspan) per cell, expanded into
    # grid columns when the row closes.
    row: list[tuple[str, int, int]] | None = None
    cell: list[str] | None = None
    cell_chars: int = 0
    cell_span: tuple[int, int] = (1, 1)
    # Column -> (text, rows still to cover) for cells reaching down from above.
    carry: dict[int, tuple[str, int]] = field(default_factory=dict)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _visible(text: str) -> str:
    """`text` without invisible format characters (zero-width spaces, joiners, bidi marks...).

    A zero-width space beside "Sept 30, 2026" is invisible to the person reading
    the page and fatal to the date regex, so dates and headers get read through
    this. Code cells deliberately don't: a code with a zero-width character
    inside is not the code it looks like, and `find_codes` should keep saying so.
    """
    return _collapse("".join(c for c in text if unicodedata.category(c) != "Cf"))


def _span(attrs: list[tuple[str, str | None]], name: str, cap: int) -> int:
    """A colspan or rowspan attribute as a small positive integer (junk and 0 become 1)."""
    for key, value in attrs:
        if key == name and value:
            digits = value.strip()
            if digits.isascii() and digits.isdigit():
                return max(1, min(int(digits), cap))
    return 1


def _kind_from_heading(heading: str) -> TableKind:
    lowered = heading.lower()
    if "permanent" in lowered:
        return "permanent"
    if "cosmetic" in lowered:
        return "cosmetic"
    return "main"


class _PageParser(HTMLParser):
    """Collects every table's text and remembers the latest h2/h3/h4 above it.

    Script and style contents are skipped (a code in a `<script>` is a
    tracking id's cousin, not a code anyone can redeem). A table inside a cell is
    its own table and contributes nothing to the cell around it. Everything
    past the row, cell and depth limits is dropped, with `truncated` set so the
    caller can say so.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[ParsedTable] = []
        self.truncated = False
        self._heading = ""
        self._heading_buf: list[str] | None = None
        self._stack: list[_OpenTable] = []
        self._skip = 0
        self._deep = 0  # tables past _MAX_TABLE_DEPTH, ignored along with their insides
        self._rows_kept = 0
        self._opened = 0

    # -- HTMLParser hooks --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            if self._deep or len(self._stack) >= _MAX_TABLE_DEPTH:
                self._deep += 1
                self.truncated = True
                return
            self._opened += 1
            self._stack.append(_OpenTable(_kind_from_heading(self._heading), self._opened))
            return
        if self._deep:
            return
        if tag in _HEADING_TAGS:
            self._heading_buf = []
            return
        table = self._stack[-1] if self._stack else None
        if tag == "tr":
            if table is not None:
                self._end_row(table)
                table.row = []
        elif tag in ("td", "th"):
            if table is not None:
                self._end_cell(table)
                if table.row is None:
                    table.row = []
                table.cell = []
                table.cell_chars = 0
                table.cell_span = (
                    _span(attrs, "colspan", _MAX_COLSPAN),
                    _span(attrs, "rowspan", _MAX_ROWSPAN),
                )
        elif tag in _BREAK_TAGS:
            self._add_text(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "table":
            if self._deep:
                self._deep -= 1
            elif self._stack:
                self._close_table()
            return
        if self._deep:
            return
        if tag in _HEADING_TAGS:
            if self._heading_buf is not None:
                self._heading = _collapse("".join(self._heading_buf))
                self._heading_buf = None
            return
        table = self._stack[-1] if self._stack else None
        if tag == "tr":
            if table is not None:
                self._end_row(table)
        elif tag in ("td", "th"):
            if table is not None:
                self._end_cell(table)
        elif tag in _BREAK_TAGS:
            self._add_text(" ")

    def handle_data(self, data: str) -> None:
        if self._skip or self._deep:
            return
        self._add_text(data)

    # -- internals --

    def _add_text(self, data: str) -> None:
        table = self._stack[-1] if self._stack else None
        if table is not None and table.cell is not None:
            room = _MAX_CELL_CHARS - table.cell_chars
            if room <= 0:
                self.truncated = True
                return
            if len(data) > room:
                data = data[:room]
                self.truncated = True
            table.cell.append(data)
            table.cell_chars += len(data)
        elif self._heading_buf is not None:
            self._heading_buf.append(data)

    def _end_cell(self, table: _OpenTable) -> None:
        if table.cell is None:
            return
        if table.row is not None:
            colspan, rowspan = table.cell_span
            table.row.append((_collapse("".join(table.cell)), colspan, rowspan))
        table.cell = None

    def _end_row(self, table: _OpenTable) -> None:
        self._end_cell(table)
        cells, table.row = table.row, None
        if not cells:
            return
        if len(table.rows) >= _MAX_ROWS or self._rows_kept >= _MAX_PAGE_ROWS:
            self.truncated = True
            return
        table.rows.append(self._grid_row(table, cells))
        self._rows_kept += 1

    @staticmethod
    def _grid_row(table: _OpenTable, cells: list[tuple[str, int, int]]) -> list[str]:
        """Lay a row's cells onto the table's column grid, honoring spans from above.

        A cell with `rowspan` keeps its text over the rows beneath it (one reward
        over three codes means all three get it); a cell with `colspan` has its
        text in its first column and blanks in the rest. Without this, one
        spanning cell shifts everything after it one column and a valid code
        lands in the wrong place, which is a very quiet way to lose a code.
        """
        out: list[str] = []
        carry = table.carry

        def fill_carried() -> None:
            while len(out) in carry and len(out) < _MAX_COLUMNS:
                col = len(out)
                text, left = carry[col]
                out.append(text)
                if left <= 1:
                    del carry[col]
                else:
                    carry[col] = (text, left - 1)

        for text, colspan, rowspan in cells:
            fill_carried()
            for i in range(colspan):
                if len(out) >= _MAX_COLUMNS:
                    break
                col = len(out)
                shown = text if i == 0 else ""
                out.append(shown)
                if rowspan > 1:
                    carry[col] = (shown, rowspan - 1)
            fill_carried()
        # Spans reaching past the last cell of this row still belong to its grid.
        while any(col >= len(out) for col in carry) and len(out) < _MAX_COLUMNS:
            if len(out) in carry:
                fill_carried()
            else:
                out.append("")
        return out

    def _close_table(self) -> None:
        table = self._stack.pop()
        self._end_row(table)
        if table.rows:
            self.tables.append(ParsedTable(table.kind, table.rows, table.order))

    def finish(self) -> None:
        """Flush whatever a page left open (an unclosed table is still a table)."""
        self.close()
        while self._stack:
            self._close_table()
        self.tables.sort(key=lambda t: t.order)


def parse_tables(html: str) -> tuple[list[ParsedTable], bool]:
    """Every table in `html` as rows of collapsed cell text, in page order, and a truncated flag."""
    parser = _PageParser()
    parser.feed(html)
    parser.finish()
    return parser.tables, parser.truncated


def parse_shift_date(text: str) -> date | None:
    """A date in the page's style ("Oct 6, 2026", "Sept 30, 2026", "Oct. 14th, 2026"), or None.

    The month is any prefix of a month's name from three letters up, which is
    how "Sept" gets in. A blank cell, "Unknown" and "???" are also None; ask
    `_is_no_date` if you need to tell those apart from text that just didn't
    parse.
    """
    match = _DATE_RE.match(_visible(text))
    if match is None:
        return None
    word = match.group(1).lower()
    month = next((i for i, name in enumerate(_MONTHS, 1) if name.startswith(word)), None)
    year = int(match.group(3))
    if month is None or year not in _YEAR_RANGE:
        return None
    try:
        return date(year, month, int(match.group(2)))
    except ValueError:
        return None


def _is_no_date(text: str) -> bool:
    """The cells that honestly say "no date": empty, "Unknown", or a row of question marks."""
    stripped = _visible(text)
    return not stripped or stripped.lower() == "unknown" or set(stripped) == {"?"}


def _start_of_day(d: date) -> datetime:
    """Midnight at the start of `d` in Pacific time, as UTC."""
    return datetime.combine(d, time.min, tzinfo=_PACIFIC).astimezone(UTC)


def _column_map(header: list[str]) -> dict[str, int] | None:
    """Match header cells to roles by their words, or None if no cell mentions "code"."""
    words = [_visible(h).lower() for h in header]
    code_col = next((i for i, h in enumerate(words) if "code" in h), None)
    if code_col is None:
        return None
    cols = {"code": code_col}
    used = {code_col}
    # "expir" so that Expires, Expiry and Expiration all count.
    for role, stem in (
        ("reward", "reward"),
        ("added", "added"),
        ("expires", "expir"),
        ("source", "source"),
    ):
        for i, h in enumerate(words):
            if i not in used and stem in h:
                cols[role] = i
                used.add(i)
                break
    return cols


@dataclass(frozen=True, slots=True)
class _Row:
    """One code row, read from one table, before same-code rows get merged."""

    kind: TableKind
    category: Category
    reward: str
    expires: date | None
    added: date | None
    origin: str


@dataclass(slots=True)
class _Stats:
    rows_read: int = 0
    skipped: int = 0
    bad_dates: int = 0
    future_dates: int = 0


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """The header row's index and column map, looking at the first few rows.

    A row that names a code column *and* another role (reward, added, expiry,
    source) wins; a lone "Code" cell is the fallback, so a bare one-column
    table still reads. This is what keeps a title row ("Latest SHiFT codes" in
    one wide cell) from being mistaken for the header and eating the table.
    """
    fallback = None
    for i, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        cols = _column_map(row)
        if cols is None:
            continue
        if len(cols) > 1:
            return i, cols
        if fallback is None:
            fallback = (i, cols)
    return fallback


def _cell(row: list[str], cols: dict[str, int], role: str) -> str:
    i = cols.get(role)
    return row[i] if i is not None and i < len(row) else ""


def _category(kind: TableKind, reward: str) -> Category:
    if kind == "permanent":
        return "permanent"
    if kind == "cosmetic" or _COSMETIC_REWARD_RE.search(reward):
        return "cosmetic"
    if GOLDEN_RE.search(reward):
        return "golden"
    return "other"


def _read_rows(tables: list[ParsedTable], stats: _Stats, today: date) -> dict[str, list[_Row]]:
    """Code -> its rows, in the order the codes first appear on the page.

    `today` is the fetch day in Pacific time. An "Added" date after tomorrow is
    treated as no date at all (and counted): the alternative is a code that
    claims to have been added in 2099 and so looks forever fresh.
    """
    by_code: dict[str, list[_Row]] = {}
    for table in tables:
        header = _find_header(table.rows)
        if header is None:
            continue
        header_index, cols = header

        for row in table.rows[header_index + 1 :]:
            codes = find_codes(_cell(row, cols, "code"))
            if len(codes) != 1:
                stats.skipped += 1
                continue
            dates: dict[str, date | None] = {}
            for role in ("added", "expires"):
                text = _cell(row, cols, role)
                dates[role] = parse_shift_date(text)
                if dates[role] is None and not _is_no_date(text):
                    stats.bad_dates += 1
            added = dates["added"]
            # A day of slack: a site writing from a zone ahead of Pacific is
            # already on tomorrow's date, and that's honest, not far-future.
            if added is not None and added > today + timedelta(days=1):
                dates["added"] = None
                stats.future_dates += 1
            reward_raw = _cell(row, cols, "reward")
            origin_raw = _cell(row, cols, "source") if table.kind != "main" else ""
            stats.rows_read += 1
            by_code.setdefault(codes[0], []).append(
                _Row(
                    kind=table.kind,
                    category=_category(table.kind, reward_raw),
                    reward=cap_shown(reward_raw, _SHOWN_LIMIT),
                    expires=dates["expires"],
                    added=dates["added"],
                    origin=cap_shown(origin_raw, _SHOWN_LIMIT),
                )
            )
    return by_code


def _merge_rows(code: str, rows: list[_Row]) -> ItemDetails:
    """One code listed in several tables becomes one set of details.

    Category comes from the most specific table; reward and origin from the
    most specific table that has one; the dates from whichever row has them
    (again most specific first). Nothing is made up, and an empty cell never
    beats a full one.
    """
    ranked = sorted(rows, key=lambda r: _KIND_RANK[r.kind], reverse=True)
    return ItemDetails(
        code=code,
        rank="table",
        reward=next((r.reward for r in ranked if r.reward), ""),
        category=ranked[0].category,
        expires_on=next((r.expires for r in ranked if r.expires), None),
        added_on=next((r.added for r in ranked if r.added), None),
        origin=next((r.origin for r in ranked if r.origin), ""),
        listed=True,
    )


def _header_safe(value: str | None) -> str | None:
    """`value` if it can be sent back as a request header, else None.

    A validator is whatever the server said, and a server can say a byte httpx
    refuses to put in a header. Kept as-is, that would raise before the request
    went out, on every pass, until a restart: the source wedged by its own
    cache hint. Printable ASCII only; anything else is dropped, and the next
    request goes out unconditional.
    """
    if value and value.isascii() and value.isprintable():
        return value
    return None


class ShiftTableCollector:
    """Reads a SHiFT code page's tables: one `RawItem` per code, details attached.

    The item's text fields are all just the code, so `find_codes` sees exactly
    that code and never a stray one from a reward's prose. Its link is the
    page's own URL; the section anchors the page offers don't survive
    canonicalization anyway (design.md §16, E2).
    """

    source_type = "shift_table"
    rate_limit_key = None

    def __init__(
        self,
        source: ShiftTableSource,
        *,
        validators: PageValidators | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self.name = source.name
        self._validators = validators if validators is not None else PageValidators()
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or (lambda: datetime.now(UTC))

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        url = str(self._source.url)
        # This pass supersedes anything an earlier one staged and nobody committed.
        self._validators.discard(url)
        headers: dict[str, str] = {}
        known = self._validators.get(url)
        if known is not None:
            # Sanitized again on the way out: the dict is the owner's, and
            # whatever's in it, a bad header value must not stop a request.
            etag, last_modified = _header_safe(known.etag), _header_safe(known.last_modified)
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
        fetched = await _fetch(
            http,
            url,
            sleep=self._sleep,
            source_name=self.name,
            headers=headers or None,
        )
        if fetched.status == 304:
            logger.info("shift_table %s: not modified", self.name)
            return []
        # Parsing is plain synchronous CPU work and the page can be 5 MB of
        # whatever the site felt like sending, so it doesn't get to run on the loop.
        items = await asyncio.to_thread(self._parse_items, fetched.body)
        # Only a page that parsed gets its validators staged (see PageValidators
        # for who commits them); a 200 that sends none stages "forget the old
        # ones", since they no longer describe the page.
        etag, last_modified = _header_safe(fetched.etag), _header_safe(fetched.last_modified)
        self._validators.stage(
            url, Validators(etag, last_modified) if etag or last_modified else None
        )
        return items

    def _parse_items(self, body: bytes) -> list[RawItem]:
        tables, truncated = parse_tables(body.decode("utf-8", errors="replace"))
        stats = _Stats()
        today = self._clock().astimezone(_PACIFIC).date()
        by_code = _read_rows(tables, stats, today)
        if not by_code:
            raise ValueError("no SHiFT code tables found")
        logger.info(
            "shift_table %s: %d rows read, %d skipped, %d unparseable dates%s%s",
            self.name,
            stats.rows_read,
            stats.skipped,
            stats.bad_dates,
            f", {stats.future_dates} added in the future" if stats.future_dates else "",
            " (page hit a size limit)" if truncated else "",
        )
        url = str(self._source.url)
        topics = tuple(self._source.topics) if self._source.topics else None
        items = []
        for code, rows in by_code.items():
            details = _merge_rows(code, rows)
            items.append(
                RawItem(
                    url=url,
                    title=code,
                    excerpt=code,
                    source_name=self.name,
                    trust=self._source.trust,
                    published_at=_start_of_day(details.added_on) if details.added_on else None,
                    topics=topics,
                    full_text=code,
                    details=details,
                )
            )
        return items
