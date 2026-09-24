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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def canonicalize(url: str) -> str | None:
    """Return the canonical form of `url`, or None if it isn't http(s).

    Lowercases the scheme and host, drops the fragment and default port,
    strips tracking params (utm_* and the usual suspects) while leaving
    every other query param alone, and drops a trailing slash from a
    non-root path. `http` and `https` are deliberately not merged into
    one; the spec doesn't ask for it, and future me is welcome to it.
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

    host = parts.hostname.lower()
    # "www.pcgamesn.com" and "pcgamesn.com" are the same article wearing a
    # different hat; the first live run posted both, side by side, like a
    # typo with confidence.
    host = host.removeprefix("www.")
    try:
        # `.port` is validated lazily, so "example.com:99999" sails through
        # urlsplit() and only explodes here. Found by test-engineer, not by me,
        # which is roughly how it always goes.
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0]
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or "/"
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
