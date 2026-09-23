"""Match collected items to topics, and cap each topic's list to a sane size.

A topic isn't a search query; it's a name, some aliases, and a list of
entities that are related but not synonyms ("Blizzard" isn't "Diablo IV",
it's the company that makes it). A name or alias hit is a confident match.
An entity-only hit is `uncertain`, because "Take-Two" also shows up in
every GTA 6 and earnings story that has nothing to do with Borderlands.
The LLM downstream treats those differently; this module's job is just to
flag them correctly and not let them crowd out the real news.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from newsbot.collectors.base import RawItem
from newsbot.config import Topic

_TRUST_RANK = {"official": 0, "press": 1, "community": 2}
_MATCH_RANK = {"confident": 0, "uncertain": 1}


@dataclass(frozen=True)
class TopicItem:
    item: RawItem
    topic_key: str
    uncertain: bool


@dataclass(frozen=True)
class TopicMatcher:
    """Precompiled matchers for one topic: name/aliases, and entities separately."""

    confident_re: re.Pattern[str] | None
    entity_re: re.Pattern[str] | None

    def match(self, haystack: str) -> Literal["confident", "uncertain"] | None:
        if self.confident_re is not None and self.confident_re.search(haystack):
            return "confident"
        if self.entity_re is not None and self.entity_re.search(haystack):
            return "uncertain"
        return None


def _terms_pattern(terms: list[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    # (?<!\w) / (?!\w) instead of \b, so a term that starts or ends with a
    # non-word character (an apostrophe, say) still gets proper boundaries.
    alternation = "|".join(re.escape(term) for term in terms)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


def build_matchers(topics: list[Topic]) -> dict[str, TopicMatcher]:
    return {
        topic.key: TopicMatcher(
            confident_re=_terms_pattern([topic.name, *topic.aliases]),
            entity_re=_terms_pattern(topic.entities),
        )
        for topic in topics
    }


def _cap_key(topic_item: TopicItem) -> tuple[int, int, float]:
    published = topic_item.item.published_at
    # Undated items (SPEC-DEV 4 keeps them around) sort as if they were the
    # oldest thing in the batch -- recency can't rank what it doesn't know.
    recency = -published.timestamp() if published else float("inf")
    return (
        _MATCH_RANK["uncertain" if topic_item.uncertain else "confident"],
        _TRUST_RANK.get(topic_item.item.trust, len(_TRUST_RANK)),
        recency,
    )


def filter_items(
    items: list[RawItem], topics: list[Topic], max_per_topic: int
) -> dict[str, list[TopicItem]]:
    """Match every item to the topics it belongs to, then cap each topic's list.

    An item with `topics` set only considers those topics (and, per
    SPEC-DEV 3, is a confident match for a single named topic with no
    keyword check at all -- a dedicated Steam feed's patch notes don't
    have to mention the game by name). An item with `topics=None` is
    checked against every topic by keyword.

    The cap ranks confident matches above uncertain ones, then trust, then
    recency, so a busy press feed's `uncertain` entity noise can't crowd a
    quiet official source out of its own topic's story slots.
    """
    matchers = build_matchers(topics)
    known_keys = set(matchers)
    grouped: dict[str, list[TopicItem]] = defaultdict(list)

    for item in items:
        candidates = set(item.topics) & known_keys if item.topics else known_keys
        dedicated_key = item.topics[0] if item.topics and len(item.topics) == 1 else None

        for topic_key in candidates:
            if topic_key == dedicated_key:
                grouped[topic_key].append(
                    TopicItem(item=item, topic_key=topic_key, uncertain=False)
                )
                continue

            result = matchers[topic_key].match(f"{item.title} {item.excerpt}")
            if result is None:
                continue
            grouped[topic_key].append(
                TopicItem(item=item, topic_key=topic_key, uncertain=result == "uncertain")
            )

    return {
        topic_key: sorted(topic_items, key=_cap_key)[:max_per_topic]
        for topic_key, topic_items in grouped.items()
    }


__all__ = ["TopicItem", "TopicMatcher", "build_matchers", "filter_items"]
