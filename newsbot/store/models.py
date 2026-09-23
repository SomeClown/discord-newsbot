"""Row-shaped dataclasses passed between the pipeline and the store.

These exist so the pipeline never has to know a `sqlite3.Row` from a hole in
the ground. They're deliberately dumb: no behavior, just the fields a query
returns or a write needs. If you find yourself adding a method to one of
these, it probably belongs in `repo.py` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Trust = Literal["official", "press", "community"]
Label = Literal["official", "reported", "rumor"]


@dataclass(frozen=True)
class PriorStory:
    """A story from a previous run, sent back to the model so it knows what it already told."""

    id: int
    headline: str
    created_at: datetime


@dataclass(frozen=True)
class DigestRow:
    id: int
    run_date: date
    status: str
    posted_message_ids: list[int]
    error_notes: str | None


@dataclass(frozen=True)
class StoredItem:
    """An item on its way into (or already in) the `items` table.

    `topics` maps a topic key to whether the match was `uncertain` (an
    entity-only hit, not a name or alias) -- see `pipeline/filter.py`.
    """

    url: str
    title: str
    excerpt: str
    source_name: str
    trust: Trust
    published_at: datetime | None
    topics: dict[str, bool]


@dataclass(frozen=True)
class StoryToSave:
    """A story the summarizer produced, ready for `repo.save_run`."""

    topic_key: str
    headline: str
    summary: str
    label: Label
    item_urls: list[str]
    update_of_story_id: int | None


@dataclass(frozen=True)
class Usage:
    """Token counts from an LLM call, tracked so the owner can see the monthly bill coming."""

    input_tokens: int
    output_tokens: int
