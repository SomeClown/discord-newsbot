"""Canonicalize URLs and dedupe collected items against the batch and the store.

Every source has its own ideas about what a URL is. Some append tracking
parameters the way toddlers append jam to furniture; some flip between http
and https depending on the phase of the moon. This module sands all of that
down to one canonical form so the database's UNIQUE constraint can play
bouncer: if you've already been inside tonight, you're not getting back in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from newsbot.collectors.base import RawItem

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Tracking params to strip. Everything else is kept on purpose: YouTube's
# `?v=` and Steam's `?appid=` are query params too, and stripping those
# would break the URL rather than clean it.
_TRACKING_PREFIX_RE = re.compile(r"^utm_", re.IGNORECASE)
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "igshid",
    "si",
    "feature",
}

_TRUST_RANK = {"official": 0, "press": 1, "community": 2}

# What's allowed to survive path re-encoding unescaped, on top of the
# characters `quote()` already treats as always-safe (letters, digits,
# `_.-~`). This is RFC 3986's path-segment sub-delims plus `/` (so we're
# not re-splitting the path into segments) and `%` (so re-encoding an
# already-canonical path is a no-op instead of double-escaping it).
# Deliberately *not* in this set: `<>[]"` \``, space, and anything
# non-ASCII -- those are exactly the characters that let a story's URL
# break out of format.py's `<url>` autolink and grow a markdown
# `[text](url)` link next to it that points somewhere else entirely.
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"


def canonicalize(url: str) -> str | None:
    """Return the canonical form of `url`, or None if it isn't http(s).

    Lowercases the scheme and host, drops the fragment and default port,
    strips tracking params (utm_* and the usual suspects) while leaving
    every other query param alone, and drops a trailing slash from a
    non-root path. `http` and `https` are deliberately not merged into
    one; the spec doesn't ask for it, and future me is welcome to it.

    Rejects anything with userinfo in the netloc (`user:pass@host`, or
    just `user@host`) outright rather than passing it through -- there's
    no legitimate reason a news URL needs credentials in it, and it's a
    classic way to make a link's *displayed* host and its *actual* host
    disagree. Also rejects a host that doesn't survive IDNA encoding: a
    bidirectional override character (U+202E and friends) in a hostname
    is a textbook homograph trick, and IDNA's nameprep step already
    refuses those, so this just declines to catch the exception and
    ship the URL anyway.

    The path gets percent-re-encoded (`urllib.parse.quote`, idempotent)
    rather than passed through -- angle brackets, square brackets, quotes,
    backticks, raw spaces, control characters and non-ASCII (including
    bidi overrides) in a path are all things a member's client would
    happily treat as the end of a URL, which is exactly how a poisoned
    path could close `format.py`'s `<url>` autolink early and grow a fake
    markdown link right after it.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        # Rejects javascript:, data: and anything else that isn't a page
        # a member can click; these should never reach a Discord embed.
        return None
    if not parts.hostname:
        return None
    if "@" in parts.netloc:
        # Credentials in a URL, or just a stray "@" that pushes everything
        # before it into what a browser would treat as a discarded
        # username. Neither belongs in a news link; reject rather than
        # try to guess what was meant.
        return None

    host = parts.hostname.lower()
    # "www.pcgamesn.com" and "pcgamesn.com" are the same article wearing a
    # different hat; the first live run posted both, side by side, like a
    # typo with confidence.
    host = host.removeprefix("www.")
    try:
        host.encode("idna")
    except UnicodeError:
        return None
    try:
        # `.port` is validated lazily, so "example.com:99999" sails through
        # urlsplit() and only explodes here. Found by test-engineer, not by me,
        # which is roughly how it always goes.
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"

    path = quote(parts.path, safe=_PATH_SAFE) or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PREFIX_RE.match(key) and key.lower() not in _TRACKING_PARAMS
        ]
    )

    return urlunsplit((scheme, netloc, path, query, ""))


def normalize(
    items: list[RawItem],
    known_urls: Callable[[set[str]], set[str]],
    now: datetime,
    lookback: timedelta,
) -> list[RawItem]:
    """Canonicalize, dedupe, and drop what's already known or too old.

    In that order: canonicalize every URL (dropping anything that isn't
    http(s)); dedupe within this batch, keeping the first-seen item unless
    a later duplicate has higher trust; ask the store which of the
    survivors it already has and drop those; then drop anything published
    before `now - lookback`. An item with no `published_at` (SPEC-DEV 4:
    Brave doesn't always give us one) is kept -- URL dedupe against the
    store already stops it from showing up twice.
    """
    canonical_items = []
    for item in items:
        canonical = canonicalize(item.url)
        if canonical is None:
            continue
        canonical_items.append(replace(item, url=canonical))

    deduped: dict[str, RawItem] = {}
    for item in canonical_items:
        existing = deduped.get(item.url)
        if existing is None or _TRUST_RANK[item.trust] < _TRUST_RANK[existing.trust]:
            deduped[item.url] = item

    known = known_urls(set(deduped))
    cutoff = now - lookback
    return [
        item
        for item in deduped.values()
        if item.url not in known and (item.published_at is None or item.published_at >= cutoff)
    ]
