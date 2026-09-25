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
    entity-only hit, not a name or alias; see `pipeline/filter.py`).
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


@dataclass(frozen=True)
class StoryView:
    """A story as shown to a member through `/news recent` or `/news search`."""

    id: int
    topic_key: str
    headline: str
    summary: str
    label: Label
    created_at: datetime
    urls: list[str]
    is_update_of: int | None


@dataclass(frozen=True)
class SourceHealthRow:
    source_name: str
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    # True for a configured source with no source_health row yet -- e.g. one
    # added to config.yaml since the last run. consecutive_failures stays 0
    # for it (there's nothing to be unhealthy about), so this is the only
    # way to tell "never run" apart from "ran fine."
    never_run: bool = False


@dataclass(frozen=True)
class AlertState:
    """The SHiFT alert sweep's cross-run scratchpad, read out of `alert_state`.

    `seeded` is the marker from A1: while False, every code the sweep
    finds gets recorded silently instead of posted, so turning the
    feature on against feeds full of months-old codes doesn't flood the
    channel on the first run. `ping_count` only means anything alongside
    `ping_day` -- a stale `ping_day` (not today, in `cfg.digest.timezone`)
    means the count has already effectively reset; see `pings_used_today`
    in `shift/decide.py`.
    """

    seeded: bool
    last_sweep_at: datetime | None
    last_sweep_summary: str | None
    ping_day: str | None
    ping_count: int


@dataclass(frozen=True)
class AlertStatus:
    """Everything `/newsbot status`'s SHiFT alerts field needs."""

    enabled: bool
    seeded: bool
    last_sweep_at: datetime | None
    last_sweep_summary: str | None
    codes_alerted: int
    pings_today: int
    max_pings: int
    test_command_enabled: bool = False


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything `/newsbot status` needs, gathered in one query pass."""

    last_digest: DigestRow | None
    source_health: list[SourceHealthRow]
    items_last_24h: int
    stories_last_24h: int
    month_input_tokens: int
    month_output_tokens: int
