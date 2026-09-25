"""Brave News search collector, for the single `web_search` source.

Unlike the other collector types, one configured source here expands into
several requests: `queries_per_topic` templates, formatted per topic, so
three topics and two templates means six searches. Brave's free tier is
good for about one request a second, so those six run one after another
with a pause between them, not concurrently -- there's no `httpx` retry
for "the whole run got 429'd because we fired six requests at once".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from newsbot import text
from newsbot.collectors.base import QuotaExceeded, RawItem
from newsbot.config import Topic, WebSearchSource

_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
_QUERY_GAP_S = 1.1


def _parse_page_age(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class WebSearchCollector:
    source_type = "web_search"
    rate_limit_key = None

    def __init__(
        self,
        source: WebSearchSource,
        topics: list[Topic],
        api_key: str,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._source = source
        self._topics = topics
        self._api_key = api_key
        self._sleep = sleep or asyncio.sleep
        self.name = source.name

    async def collect(self, http: httpx.AsyncClient) -> list[RawItem]:
        items: list[RawItem] = []
        first = True
        for topic in self._topics:
            queries = self._queries_for(topic)
            for query in queries:
                if not first:
                    await self._sleep(_QUERY_GAP_S)
                first = False
                items.extend(await self._search(http, query, topic.key))
        return items

    def _queries_for(self, topic: Topic) -> list[str]:
        # A topic with its own search_queries (BL4's press coverage never
        # says "Borderlands 4 news" the way the default template guesses)
        # skips the templates entirely, still capped at queries_per_topic.
        # Everyone else falls back to the shared templates, formatted per
        # topic name, same as before this existed.
        if topic.search_queries:
            return topic.search_queries[: self._source.queries_per_topic]
        templates = self._source.query_templates[: self._source.queries_per_topic]
        return [template.format(name=topic.name) for template in templates]

    async def _search(self, http: httpx.AsyncClient, query: str, topic_key: str) -> list[RawItem]:
        response = await http.get(
            _NEWS_URL,
            params={"q": query, "freshness": "pd", "count": 20},
            headers={"X-Subscription-Token": self._api_key},
        )
        if response.status_code in (429, 402):
            raise QuotaExceeded("quota")
        response.raise_for_status()

        data = response.json()
        results = []
        for result in data.get("results", []):
            if not isinstance(result, dict):
                # Brave's shape is trusted about as far as any third-party
                # API's is: a malformed entry (not even an object) skips,
                # rather than taking the rest of this query's results with
                # it.
                continue
            url = result.get("url")
            title = result.get("title")
            if not (url and title):
                continue
            # `.get(..., "")` only covers a *missing* key; Brave has sent
            # an explicit `"description": null` in the wild, which sails
            # right past that default and into `clean_text(None)`, which
            # doesn't have a good day.
            description = result.get("description") or ""
            results.append(
                RawItem(
                    url=url,
                    title=title,
                    excerpt=text.clean_text(description),
                    source_name=self._source.name,
                    trust=self._source.trust,
                    published_at=_parse_page_age(result.get("page_age")),
                    topics=(topic_key,),
                    # Brave only ever gives us a snippet, never full-page
                    # text -- this is the same source `excerpt` is built
                    # from, just uncapped at 500 characters, not some
                    # additional fetch of the whole article.
                    full_text=text.plain_text(description) if description else None,
                )
            )
        return results
