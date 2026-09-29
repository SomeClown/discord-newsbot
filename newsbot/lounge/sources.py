"""Load quotes from one configured source, and keep a last good copy of it.

Sources fail. Files get renamed, a URL starts answering with a login page,
and Wikiquote has the occasional bad afternoon. The lounge shouldn't go
quiet because of any of that, so every source keeps a "last good copy" in a
small JSON file next to the database. When a load fails and a copy exists,
we use the copy and say so. When it fails and there's no copy, the source
just fails, and `daily.py` moves on to the next one.

A "good load" means the fetch or read passed every check *and* parsing found
at least one usable quote. Only a good load overwrites the copy. A page that
fetches fine but parses to nothing is a failure, because the alternative is
replacing a working copy with an empty one, which I'd rather learn about from
a log line than from the lounge.

Wikiquote also has a weekly rule: a copy younger than seven days is used with
no network at all, since a page of quotes doesn't change that often and
Wikimedia has better things to do than answer us daily. The clock for that
rule counts attempts, not successes. When a fetch fails and we fall back to
the copy, `attempted_at` moves to now, so a page that's failing gets retried
weekly and not every single morning. (Files and URLs have no such rule; they
are read or fetched every time the source is picked.)

The copy is the raw text (or, for Wikiquote, the raw rendered HTML), never the
parsed quotes, so a parser fix in a later release applies to cached pages
right away. Parsing runs in a thread because a big author page is a couple of
megabytes of HTML and the gateway heartbeat shouldn't wait on it.

Nothing here raises. `load_source` turns every failure into a `problem`
string, which is what ends up in the admin channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx

from newsbot.config import QuoteSourceCfg
from newsbot.lounge.quotes import Quote, split_fortune
from newsbot.lounge.wikiquote import fetch_page, parse_page
from newsbot.text import plain_line, shown_url

logger = logging.getLogger(__name__)

# Files and URLs both. A fortune file bigger than a megabyte is a mistake.
MAX_SOURCE_BYTES = 1_048_576
# Overall deadline for a URL source. Module level so a test can shorten it.
URL_TIMEOUT_S = 10.0
WIKIQUOTE_REFRESH = timedelta(days=7)
_CACHE_VERSION = 1
_PROBLEM_CHARS = 250
# Redirect hops we'll follow by hand. Five is plenty for a raw-file link.
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

Origin = Literal["fresh", "saved-weekly", "fallback"]


class SourceError(Exception):
    """A load that failed. The message is meant to be shown to the admin."""


@dataclass(frozen=True)
class LoadedSource:
    """What one load produced. `quotes` is empty exactly when the source failed.

    `origin` says where a non-empty `quotes` came from: read or fetched just
    now ("fresh"), the Wikiquote copy that was still inside its week
    ("saved-weekly"), or the last good copy after something went wrong
    ("fallback"). A fallback carries the `problem` and the day the copy was
    saved (`saved_on`). A failed load has `problem` and nothing else; its
    `origin` is "fresh" only because the field has to hold something.
    `title` is the resolved Wikiquote page title, for the page link.
    """

    quotes: list[Quote]
    origin: Origin
    problem: str | None = None
    saved_on: date | None = None
    title: str | None = None


@dataclass(frozen=True)
class _Cache:
    fetched_at: datetime
    attempted_at: datetime
    body: str
    title: str | None
    revid: int | None


def cache_dir_for(db_path: str | Path) -> Path:
    """The lounge cache directory for this database, e.g. `newsbot.db` gives `newsbot-lounge-cache`.

    Per database on purpose: the staging container shares `./data` with prod
    under a different database name, and I'd rather they didn't share weekly
    fetch clocks and quietly steer each other.
    """
    db = Path(db_path)
    return db.with_name(db.stem + "-lounge-cache")


def describe(src: QuoteSourceCfg) -> str:
    """A short name for admin lines: `Wikiquote "Oscar Wilde"`, `file /data/quotes.txt`, `url https://...`."""
    if src.kind == "wikiquote":
        return f'Wikiquote "{_wikiquote_title(src)}"'
    if src.kind == "url":
        return f"url {shown_url(src.value)}"
    return f"{src.kind} {src.value.strip()}"


def _log_name(src: QuoteSourceCfg) -> str:
    """`src.key`, except a URL source's key holds the whole URL, secrets and all."""
    return f"url:{shown_url(src.value)}" if src.kind == "url" else src.key


def _wikiquote_title(src: QuoteSourceCfg) -> str:
    return src.key.removeprefix("wikiquote:")


async def load_source(
    src: QuoteSourceCfg, *, http: httpx.AsyncClient, cache_dir: Path, now: datetime
) -> LoadedSource:
    """Load `src`, using and maintaining its saved copy. Never raises.

    `now` is passed in (timezone-aware) so tests can move the clock.
    """
    try:
        cache_file = cache_dir / _cache_name(src.key)
        if src.kind == "wikiquote":
            return await _load_wikiquote(src, http, cache_file, now)
        return await _load_plain(src, http, cache_file, now)
    except Exception as exc:
        logger.warning("Loading %s failed unexpectedly", _log_name(src), exc_info=True)
        return _failed(exc)


def _failed(exc: BaseException) -> LoadedSource:
    # str() of a timeout is empty; the class name is better than nothing.
    # First non-blank line only: a multi-line message is somebody's traceback
    # or somebody's response body, and neither belongs in the admin channel.
    lines = [line for line in str(exc).splitlines() if line.strip()]
    problem = (plain_line(lines[0], _PROBLEM_CHARS) if lines else "") or type(exc).__name__
    return LoadedSource([], "fresh", problem)


# --- File and URL sources ---


async def _load_plain(
    src: QuoteSourceCfg, http: httpx.AsyncClient, cache_file: Path, now: datetime
) -> LoadedSource:
    """The file and URL branch: read or fetch every time, fall back to the copy."""
    cache = await asyncio.to_thread(_read_cache, cache_file, src.key)
    try:
        body = await (_read_file(src.value) if src.kind == "file" else _fetch_url(http, src.value))
        quotes, too_long = await asyncio.to_thread(_parse_fortune, body)
        if not quotes:
            raise SourceError(f"{describe(src)} has no usable quotes in it")
    except Exception as exc:
        problem = _failed(exc).problem
        if cache is not None:
            quotes, _ = await asyncio.to_thread(_parse_fortune, cache.body)
            if quotes:
                logger.info(
                    "Source %s: origin=fallback quotes=%d saved=%s",
                    _log_name(src),
                    len(quotes),
                    cache.fetched_at.date(),
                )
                return LoadedSource(quotes, "fallback", problem, cache.fetched_at.date())
        return LoadedSource([], "fresh", problem)

    if cache is None or cache.body != body:
        await asyncio.to_thread(
            _write_cache, cache_file, src.key, _Cache(now, now, body, None, None)
        )
    logger.info(
        "Source %s: origin=fresh quotes=%d dropped_long=%d", _log_name(src), len(quotes), too_long
    )
    return LoadedSource(quotes, "fresh")


def _parse_fortune(body: str) -> tuple[list[Quote], int]:
    entries, too_long = split_fortune(body)
    return [Quote(e) for e in entries], too_long


async def _read_file(path: str) -> str:
    return await asyncio.to_thread(_read_file_sync, Path(path))


def _read_file_sync(path: Path) -> str:
    try:
        info = path.stat()
    except FileNotFoundError:
        raise SourceError(f"file not found: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise SourceError(f"{path} isn't a regular file")
    if info.st_size > MAX_SOURCE_BYTES:
        raise SourceError(f"{path} is over {MAX_SOURCE_BYTES // (1024 * 1024)} MiB")
    # Read one byte past the cap, in case the file grew after the stat.
    with path.open("rb") as handle:
        data = handle.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise SourceError(f"{path} is over {MAX_SOURCE_BYTES // (1024 * 1024)} MiB")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceError(f"{path} isn't UTF-8 text") from None


async def _fetch_url(http: httpx.AsyncClient, url: str) -> str:
    shown = shown_url(url)
    try:
        async with asyncio.timeout(URL_TIMEOUT_S):
            return await _get_text(http, url, shown)
    except TimeoutError:
        raise SourceError(f"{shown} didn't answer within {URL_TIMEOUT_S:g} seconds") from None
    except httpx.HTTPError as exc:
        raise SourceError(f"couldn't reach {shown} ({type(exc).__name__})") from exc


async def _get_text(http: httpx.AsyncClient, url: str, shown: str) -> str:
    """Fetch `url`, following redirects by hand so no hop is ever requested over plain http.

    I let httpx follow redirects at first and checked the hops afterwards,
    which is the bouncer inspecting your ID after you've already walked the
    dodgy alley. Now the `Location` is checked before the request, so a
    downgrade is refused instead of merely noticed.
    """
    current = httpx.URL(url)
    # Config load already refuses http://, but this is the function that
    # sends the request, so it checks too.
    if current.scheme != "https":
        raise SourceError(f"{shown} is a non-https address")
    for hop in range(MAX_REDIRECTS + 1):
        # httpx's own timeout is per operation, so the caller's asyncio.timeout
        # is what makes ten seconds mean ten seconds.
        async with http.stream(
            "GET", current, timeout=URL_TIMEOUT_S, follow_redirects=False
        ) as response:
            if response.status_code not in _REDIRECT_STATUSES:
                return await _read_final(response, shown)
            target = response.headers.get("location")
            status = response.status_code
        if not target:
            raise SourceError(f"{shown} answered HTTP {status} with no Location to follow")
        if hop == MAX_REDIRECTS:
            raise SourceError(f"{shown} redirected more than {MAX_REDIRECTS} times")
        try:
            current = current.join(target)
        except httpx.InvalidURL:
            raise SourceError(f"{shown} redirected to an address that isn't a valid URL") from None
        if current.scheme != "https":
            raise SourceError(
                f"{shown} redirects to a non-https address ({shown_url(str(current))})"
            )
    raise AssertionError("unreachable: the loop returns or raises")


async def _read_final(response: httpx.Response, shown: str) -> str:
    """Check the last response's status and type, then read its body under the size cap."""
    if response.status_code != 200:
        raise SourceError(f"{shown} returned HTTP {response.status_code}")
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type == "text/html":
        raise SourceError(
            f"{shown} is a web page, not plain text; use the raw link (a raw file link, "
            "not the page that displays it)"
        )
    if media_type != "text/plain":
        raise SourceError(
            f"{shown} answered with {media_type or 'no content type'}, not text/plain"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise SourceError(f"{shown} is over {MAX_SOURCE_BYTES // (1024 * 1024)} MiB")
        chunks.append(chunk)
    encoding = response.charset_encoding or "utf-8"
    try:
        return b"".join(chunks).decode(encoding)
    except UnicodeDecodeError, LookupError:
        raise SourceError(f"{shown} couldn't be read as {encoding} text") from None


# --- Wikiquote ---


async def _load_wikiquote(
    src: QuoteSourceCfg, http: httpx.AsyncClient, cache_file: Path, now: datetime
) -> LoadedSource:
    """The Wikiquote branch: the weekly rule, then a fetch, then the copy as a last resort."""
    title = _wikiquote_title(src)
    cache = await asyncio.to_thread(_read_cache, cache_file, src.key)

    if cache is not None and timedelta(0) <= now - cache.attempted_at < WIKIQUOTE_REFRESH:
        parsed = await _parse_cached(cache, title)
        if parsed is not None:
            logger.info("Source %s: origin=saved-weekly quotes=%d", src.key, len(parsed.quotes))
            return LoadedSource(
                parsed.quotes, "saved-weekly", saved_on=cache.fetched_at.date(), title=cache.title
            )

    try:
        page = await fetch_page(http, title)
        parsed = await asyncio.to_thread(parse_page, page.title, page.html)
        if not parsed.quotes:
            raise SourceError(f"Wikiquote's {page.title} page has no usable quotes on it")
    except Exception as exc:
        problem = _failed(exc).problem
        if cache is not None:
            saved = await _parse_cached(cache, title)
            if saved is not None:
                # Counting the attempt is what keeps a failing page to one
                # retry a week instead of one a day.
                await asyncio.to_thread(
                    _write_cache, cache_file, src.key, replace(cache, attempted_at=now)
                )
                logger.info(
                    "Source %s: origin=fallback quotes=%d saved=%s",
                    src.key,
                    len(saved.quotes),
                    cache.fetched_at.date(),
                )
                return LoadedSource(
                    saved.quotes, "fallback", problem, cache.fetched_at.date(), cache.title
                )
        return LoadedSource([], "fresh", problem)

    await asyncio.to_thread(
        _write_cache, cache_file, src.key, _Cache(now, now, page.html, page.title, page.revid)
    )
    logger.info(
        "Source %s: origin=fresh kind=%s quotes=%d dropped_long=%d dropped_unattributed=%d",
        src.key,
        parsed.kind,
        len(parsed.quotes),
        parsed.dropped_long,
        parsed.dropped_unattributed,
    )
    return LoadedSource(parsed.quotes, "fresh", title=page.title)


async def _parse_cached(cache: _Cache, title: str):
    """Re-parse a saved page; None if that fails or finds nothing, so callers treat it as absent."""
    try:
        parsed = await asyncio.to_thread(parse_page, cache.title or title, cache.body)
    except Exception:
        logger.warning("Re-parsing the saved copy of %s failed", title, exc_info=True)
        return None
    return parsed if parsed.quotes else None


# --- The cache files ---


def _cache_name(key: str) -> str:
    # surrogatepass: a hand-escaped config value can hold a lone surrogate,
    # and a filename is no place to find that out.
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:16] + ".json"


def _read_cache(path: Path, key: str) -> _Cache | None:
    """The saved copy, or None if there isn't one or it can't be trusted (corrupt means absent)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError, UnicodeError:
        logger.warning("Couldn't read the lounge cache file %s; ignoring it", path, exc_info=True)
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            raise ValueError("unknown layout")
        if data.get("key") != key:
            raise ValueError("belongs to another source")
        body, title, revid = data.get("body"), data.get("title"), data.get("revid")
        if not isinstance(body, str):
            raise ValueError("no body")
        if title is not None and not isinstance(title, str):
            raise ValueError("bad title")
        if revid is not None and not isinstance(revid, int):
            raise ValueError("bad revision")
        fetched_at = _parse_time(data.get("fetched_at"))
        attempted_at = _parse_time(data.get("attempted_at"))
    except (ValueError, RecursionError) as exc:
        logger.warning("The lounge cache file %s is unusable (%s); ignoring it", path, exc)
        return None
    return _Cache(fetched_at, attempted_at, body, title, revid)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("bad timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp without a timezone")
    return parsed


def _write_cache(path: Path, key: str, cache: _Cache) -> None:
    """Save `cache` atomically (temp file, then `os.replace`); a failure only logs a warning."""
    payload = {
        "version": _CACHE_VERSION,
        "key": key,
        "fetched_at": cache.fetched_at.isoformat(),
        "attempted_at": cache.attempted_at.isoformat(),
        "body": cache.body,
        "title": cache.title,
        "revid": cache.revid,
    }
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the target, so os.replace is a rename and not a copy.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle)
        os.replace(temp_name, path)
        temp_name = None
    except OSError, UnicodeError:
        logger.warning("Couldn't save the lounge cache file %s", path, exc_info=True)
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
