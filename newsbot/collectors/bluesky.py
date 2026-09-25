"""Bluesky search collector, for the `bluesky_search` sources.

As of this writing, `public.api.bsky.app`'s unauthenticated `searchPosts`
returns a flat **403 with an HTML body from BunnyCDN**, not the JSON error
Bluesky's own docs would lead you to expect (see
docs/sources-research.md). That's why the auth check below happens before
anything tries to parse the response as JSON: a status-code check can't be
fooled by whatever a CDN's error page looks like today.

Without a `BlueskySession` (SPEC-DEV 7 has no Bluesky secret by default),
every search just tries the public endpoint and treats a 401/403 as
"skip me, note the coverage gap" rather than a source failure -- Bluesky's
own official accounts still get through via their RSS mirror
(`collectors/rss.py`), so this collector going quiet doesn't mean silence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from newsbot import text
from newsbot.collectors.base import QuotaExceeded, RawItem
from newsbot.config import BlueskySource

_UNAUTHED_BASE = "https://public.api.bsky.app"
_AUTHED_BASE = "https://bsky.social"
_SEARCH_LIMIT = 25


class BlueskySession:
    """One shared login, reused by every `bluesky_search` collector.

    Three configured queries (one per topic) with one collector each
    shouldn't mean three logins fighting over the rate limit; the first
    collector that needs a token creates the session under the lock, and
    the rest just wait for it and reuse the result.
    """

    def __init__(self, handle: str, app_password: str) -> None:
        self._handle = handle
        self._app_password = app_password
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def token(self, http: httpx.AsyncClient) -> str:
        async with self._lock:
            if self._token is None:
                response = await http.post(
                    f"{_AUTHED_BASE}/xrpc/com.atproto.server.createSession",
                    json={"identifier": self._handle, "password": self._app_password},
                )
                response.raise_for_status()
                self._token = response.json()["accessJwt"]
        return self._token


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class BlueskyCollector:
    source_type = "bluesky_search"
    rate_limit_key = None

    def __init__(self, source: BlueskySource, session: BlueskySession | None) -> None:
        self._source = source
        self._session = session
        self.name = source.name

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        headers = {}
        base = _UNAUTHED_BASE
        if self._session is not None:
            base = _AUTHED_BASE
            headers["Authorization"] = f"Bearer {await self._session.token(http)}"

        response = await http.get(
            f"{base}/xrpc/app.bsky.feed.searchPosts",
            params={"q": self._source.query, "sort": "latest", "limit": _SEARCH_LIMIT},
            headers=headers,
        )
        if response.status_code in (401, 403):
            # Checked before touching the body: an unauthenticated 403 here
            # has come back as an HTML CDN error page in testing, and
            # response.json() on that would raise before we ever got to
            # decide this isn't a real failure.
            raise QuotaExceeded("auth")
        response.raise_for_status()

        data = response.json()
        topics = tuple(self._source.topics) if self._source.topics else None
        items = []
        for post in data.get("posts", []):
            record = post.get("record", {})
            body = record.get("text", "")
            handle = post.get("author", {}).get("handle")
            uri = post.get("uri", "")
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            if not (handle and rkey and body):
                continue
            items.append(
                RawItem(
                    url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                    title=text.first_line(body, limit=120) or body[:120],
                    excerpt=text.clean_text(body),
                    source_name=self._source.name,
                    trust=self._source.trust,
                    published_at=_parse_created_at(record.get("createdAt")),
                    topics=topics,
                    full_text=text.plain_text(body),
                )
            )
        return items
