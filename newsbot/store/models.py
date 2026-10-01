"""Row-shaped dataclasses passed between the pipeline and the store.

These exist so the pipeline never has to know a `sqlite3.Row` from a hole in
the ground. They're deliberately dumb: no behavior, just the fields a query
returns or a write needs. If you find yourself adding a method to one of
these, it probably belongs in `repo.py` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Trust = Literal["official", "press", "community"]
Label = Literal["official", "reported", "rumor"]
Tier = Literal["free", "comped"]


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
    # True for a configured source with no source_health row yet: e.g. one
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
    `ping_day`: a stale `ping_day` (not today, in `cfg.digest.timezone`)
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
class CodeView:
    """A code as shown to a member through `/shift codes`.

    `from_roundup` (migration 003) is the marker `/shift codes` needs to
    show "from a roundup" that the `status` column alone can't give it
    once a roundup post's status is just `posted` like everything else
    (design.md §13): see `repo.query_codes`.
    """

    code: str
    first_seen_at: datetime
    source_name: str
    item_url: str
    status: str
    from_roundup: bool


@dataclass(frozen=True)
class LoungeState:
    """The daily quote's once-a-day guard, read out of `lounge_state`."""

    last_quote_date: str | None


@dataclass(frozen=True)
class QuoteDeckState:
    """One source's deck: the hashes already used, and the most recent of them.

    `last_hash` is what keeps a reshuffle from serving yesterday's quote
    again; it's `None` for a source that has never posted.
    """

    used: frozenset[str]
    last_hash: str | None


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything `/newsbot status` needs, gathered in one query pass."""

    last_digest: DigestRow | None
    source_health: list[SourceHealthRow]
    items_last_24h: int
    stories_last_24h: int
    month_input_tokens: int
    month_output_tokens: int


# --- Guilds (design.md §15) ---
#
# One frozen dataclass per table the public app added in migration 005. Same
# rule as everything above: no behavior, just the columns. Timestamps come
# back as datetimes; the 0/1 flag columns come back as bools.


@dataclass(frozen=True)
class GuildSettings:
    """One row of `guilds`: a server and its digest schedule, tier and admin channel."""

    guild_id: int
    digest_time: str
    timezone: str
    admin_channel_id: int | None
    tier: Tier
    set_up: bool
    joined_at: datetime
    imported_at: datetime | None
    permission_problems: str | None
    updated_at: datetime


@dataclass(frozen=True)
class GuildGame:
    """A game a server follows and the channel its digest post goes to."""

    guild_id: int
    game_key: str
    channel_id: int


@dataclass(frozen=True)
class ShiftSettings:
    """A server's SHiFT alert settings.

    `ping` is `none`, `everyone`, or a role id as digits. `ping_day` and
    `ping_count` are that server's own daily ping budget; like the global
    one in `alert_state`, the count only means something alongside its day.
    """

    guild_id: int
    enabled: bool
    channel_id: int | None
    ping: str
    enabled_at: datetime | None
    ping_day: str | None
    ping_count: int


@dataclass(frozen=True)
class LoungeSettings:
    """A server's lounge (welcome message and daily quote) settings.

    `quote_sources` is the already-resolved JSON list of `{"kind", "value"}`
    dicts, parsed. `last_quote_date` is the once-a-day guard.
    """

    guild_id: int
    channel_id: int
    welcome_enabled: bool
    welcome_message: str
    quote_enabled: bool
    quote_time: str
    quote_sources: list[dict[str, str]]
    last_quote_date: str | None


@dataclass(frozen=True)
class GuildDigestRow:
    """A `digests` row with the per-guild columns migration 005 added."""

    id: int
    guild_id: int | None
    run_date: date
    status: str
    posted_message_ids: list[int]
    posted_by_game: dict[str, Any]
    window_start: datetime | None
    window_end: datetime | None
    attempts: int
    error_notes: str | None


@dataclass(frozen=True)
class GameSummaryRow:
    """One Claude summary for one game on one local day, shared by every comped guild."""

    id: int
    game_key: str
    run_date: date
    status: Literal["ok", "fallback"]
    window_start: datetime
    window_end: datetime
    coverage_notes: list[str]
    note: str | None
    input_tokens: int
    output_tokens: int
    # The item ids the summary covers, `(items_after, items_upto]`; `None` on a row
    # saved before migration 007 and for `items_after` when nothing came before.
    items_after: int | None = None
    items_upto: int | None = None


@dataclass(frozen=True)
class Coverage:
    """How far a server has already been told the news: where its last digest left off.

    `end` is that digest's window end (a time, for display and the freshness rule) and
    `item_id` the newest stored item it covered. `by_game` overrides `item_id` for games a
    comped digest took from a shared summary, which ends where the summary did.
    """

    end: datetime
    item_id: int
    by_game: dict[str, int] = field(default_factory=dict, hash=False)

    def for_game(self, game_key: str) -> Coverage:
        """This coverage as one game's: `item_id` is where that game's news left off."""
        return Coverage(self.end, self.by_game.get(game_key, self.item_id))


@dataclass(frozen=True)
class ItemRange:
    """Which stored items a digest or summary covers: ids in `(after, upto]`, newer than `floor`.

    `after` is `None` for a server's first digest (the floor alone limits it). The floor is a
    time (`collected_at`) and only ever trims an old backlog; the ids decide what's in or out.
    """

    after: int | None
    upto: int
    floor: datetime


@dataclass(frozen=True)
class CompedFollow:
    """One comped, set-up server following one game, with when its digest is due."""

    game_key: str
    guild_id: int
    digest_time: str
    timezone: str


@dataclass(frozen=True)
class Notice:
    """A problem note kept for a server's `/newsbot status`."""

    id: int
    guild_id: int
    created_at: datetime
    text: str


@dataclass(frozen=True)
class ItemView:
    """A stored item as a free server sees it through `/news recent` or `/news search`.

    `topic_keys` is only the games this server follows; an item can match
    others, and those aren't this server's business.
    """

    id: int
    url: str
    title: str
    excerpt: str
    source_name: str
    trust: Trust
    published_at: datetime | None
    collected_at: datetime
    topic_keys: list[str]


@dataclass(frozen=True)
class QueuedCode:
    """A released code waiting in one server's delivery queue (`guild_code_posts`, `queued`)."""

    code: str
    source_name: str
    item_url: str
    from_roundup: bool
    golden: bool
    trusted: bool
    queued_at: datetime


# --- Per-guild digests (design.md §15, plan task 6) ---


@dataclass(frozen=True)
class HeadlineItem:
    """A stored item as a free server's digest lists it, for one game.

    `uncertain` is per game (an entity-only match; see `pipeline/filter.py`):
    the same item can be confident for one game and uncertain for another.
    """

    url: str
    title: str
    source_name: str
    trust: Trust
    published_at: datetime | None
    collected_at: datetime
    uncertain: bool


@dataclass(frozen=True)
class DueCandidate:
    """One set-up guild and its latest digest row, as the due check wants them.

    The `run_date` and everything after it describe the guild's newest `digests`
    row and are `None` (or empty) when it has never had one. `posted_any` is
    true when that row records any posted message at all, in either the per-game
    map or v2.2's flat id list (an adopted v2.2 row only has the list).
    """

    guild_id: int
    digest_time: str
    timezone: str
    tier: Tier
    run_date: date | None = None
    status: str | None = None
    posted_any: bool = False
    attempts: int = 0
    updated_at: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True)
class GuildClaim:
    """What a successful claim hands back: the row's id, its window, and what already posted.

    `posted_by_game` is non-empty only on a resume (or a retry of a failed row that
    had posted something): those games are done and must not be posted again.
    """

    digest_id: int
    window_start: datetime
    window_end: datetime
    posted_by_game: dict[str, int]
    resumed: bool
    # The row's attempt count after this claim, so the publisher knows whether a
    # failure is the last one the schedule will retry.
    attempts: int = 1
    # The item ids this digest covers, `(items_after, items_upto]`, fixed when the
    # row was first claimed so a resume or a retry reads the same items.
    items_after: int | None = None
    items_upto: int | None = None
