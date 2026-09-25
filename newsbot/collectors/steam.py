"""Steam announcements collector, for `GetNewsForApp`.

Without `feeds=steam_community_announcements`, Steam happily mixes in
third-party press ("PCGamesN", a Russian gaming site, SteamDB's "top
sellers" list) alongside the developer's own posts, none of which deserve
the `trust: official` label a Steam source is configured with. The filter
keeps this collector honest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from newsbot import text
from newsbot.collectors.base import RawItem
from newsbot.config import SteamSource

_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


class SteamCollector:
    source_type = "steam_news"
    rate_limit_key = None

    def __init__(self, source: SteamSource) -> None:
        self._source = source
        self.name = source.name

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        response = await http.get(
            _NEWS_URL,
            params={
                "appid": self._source.app_id,
                "count": 20,
                "maxlength": 0,
                "feeds": "steam_community_announcements",
            },
        )
        response.raise_for_status()
        data = response.json()

        topics = tuple(self._source.topics) if self._source.topics else None
        newsitems = data.get("appnews", {}).get("newsitems", [])

        items = []
        for item in newsitems:
            url = item.get("url")
            title = item.get("title")
            date = item.get("date")
            # url/title/date are all load-bearing (a RawItem without a
            # real url or date is worse than useless downstream), so one
            # item missing any of them skips rather than raising a
            # KeyError that would have taken every other item in this
            # response down with it.
            if not (url and title and date):
                continue
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    # Same story as the missing-key case above:
                    # `.get("contents", "")` doesn't catch an explicit
                    # `"contents": null`, which is exactly the shape that
                    # used to crash `clean_text`.
                    excerpt=text.clean_text(item.get("contents") or ""),
                    source_name=self._source.name,
                    trust=self._source.trust,
                    published_at=datetime.fromtimestamp(date, tz=UTC),
                    topics=topics,
                )
            )
        return items
