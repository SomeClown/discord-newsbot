"""Decide what to do about the SHiFT codes a sweep just found. No I/O in here.

`shift/match.py` finds codes in text; this module decides what happens to
them, which is the part with all the actual judgment calls: is this code
new, is it too old to bother anyone about, has today already spent its
`@everyone` budget. Every one of those is a pure function of its inputs, on
purpose -- `shift/sweep.py` is where a plan produced here actually turns
into a database write or a Discord message, and keeping the deciding and
the doing in separate modules is what let this file's tests run in
milliseconds against a list of dataclasses instead of a temp SQLite file.

Game scoping (A6, plan step 5b) lives here too: an item only counts toward
a sighting if `pipeline.filter.filter_items` would have matched it to one
of `cfg.alerts.topics` -- the same confident/dedicated-source rules the
digest itself uses, not a separate keyword check invented for this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from newsbot.collectors.base import CollectorResult, RawItem, Trust
from newsbot.config import Topic
from newsbot.pipeline.filter import filter_items
from newsbot.shift.match import find_codes, mentions_golden_key
from newsbot.store.models import AlertState

_TRUST_RANK = {"official": 0, "press": 1, "community": 2}


@dataclass(frozen=True)
class CodeSighting:
    """One code, as seen in one item. A code mentioned in three items makes three of these."""

    code: str
    golden: bool
    source_name: str
    item_url: str
    trust: Trust
    published_at: datetime | None


@dataclass(frozen=True)
class CodeCandidate:
    """One code, aggregated across every sighting of it in this batch (`aggregate`)."""

    code: str
    golden: bool
    source_name: str
    item_url: str
    fresh: bool


@dataclass(frozen=True)
class AlertPlan:
    """What `shift/sweep.py` should do with one batch's worth of candidates.

    `silent` pairs a candidate with why it's being recorded without a post
    (`"seeded"` while the marker is unset, A1; `"too_old"` once seeded but
    stale, A11). `to_post` is already in the order an alert message should
    announce them. `mark_seeded` tells the caller whether this batch is the
    one that gets to flip the marker on.
    """

    silent: list[tuple[CodeCandidate, str]]
    to_post: list[CodeCandidate]
    ping: bool
    cap_reached: bool
    mark_seeded: bool


def sightings_from_items(
    items: list[RawItem], *, topics: list[Topic], alert_topics: list[str]
) -> list[CodeSighting]:
    """Every code found in `items`, restricted to `alert_topics` (A6).

    An empty `alert_topics` scopes to nothing -- every item counts, same as
    the digest's own "no `topics` configured" default. Otherwise an item
    only contributes sightings if `filter_items` would have *confidently*
    matched it to one of `alert_topics` -- dedicated sources included, but
    an `uncertain` (entity-only) match doesn't count. "Gearbox" showing up
    in an unrelated press item is exactly the kind of match that's good
    enough for the digest's lower bar (a human reads the headline right
    next to it) and not good enough to scope a code alert by -- a
    Borderlands-shaped SHiFT code sitting in an item that only mentioned
    "Gearbox" in passing has no business alerting a server scoped to a
    different Gearbox game.
    """
    scoped_items = items
    if alert_topics:
        # max_per_topic is generous rather than exact: this call only cares
        # which items matched at all, never the cap that trims what the
        # digest shows -- a code from item #61 in a busy topic still counts.
        grouped = filter_items(items, topics, max(len(items), 1))
        matched_urls = {
            topic_item.item.url
            for topic_key in alert_topics
            for topic_item in grouped.get(topic_key, [])
            if not topic_item.uncertain
        }
        scoped_items = [item for item in items if item.url in matched_urls]

    sightings: list[CodeSighting] = []
    for item in scoped_items:
        text = f"{item.title}\n{item.full_text or item.excerpt}"
        codes = find_codes(text)
        if not codes:
            continue
        golden = mentions_golden_key(text)
        for code in codes:
            sightings.append(
                CodeSighting(
                    code=code,
                    golden=golden,
                    source_name=item.source_name,
                    item_url=item.url,
                    trust=item.trust,
                    published_at=item.published_at,
                )
            )
    return sightings


def _sighting_key(sighting: CodeSighting) -> tuple[int, float]:
    # Best trust first, then earliest published (undated last -- there's no
    # date to prefer it on), used to pick which sighting's source_name/
    # item_url represents the code in an alert.
    published = sighting.published_at
    recency = published.timestamp() if published else float("inf")
    return (_TRUST_RANK.get(sighting.trust, len(_TRUST_RANK)), recency)


def _is_fresh(sighting: CodeSighting, now: datetime, max_age: timedelta) -> bool:
    # Undated items are treated as fresh (SPEC-DEV 4's rule, carried over
    # here per A11) -- there's no date to judge them stale by.
    if sighting.published_at is None:
        return True
    return sighting.published_at >= now - max_age


def aggregate(
    sightings: list[CodeSighting], *, now: datetime, max_age: timedelta
) -> list[CodeCandidate]:
    """Collapse every sighting of the same code into one `CodeCandidate`.

    A code is `fresh` if *any* sighting of it is fresh (mixed ages -> fresh:
    one fresh mention is enough reason to alert). `golden` is likewise "any
    sighting mentions it". The shown source is the best-trust, then
    earliest-dated, then first-seen sighting -- ties keep first-seen order,
    which is what makes this deterministic across runs of the same input.
    Candidates come back in first-seen order (by code), matching A3's
    "announce them in the order they turned up" rule.
    """
    order: list[str] = []
    by_code: dict[str, list[CodeSighting]] = {}
    for sighting in sightings:
        if sighting.code not in by_code:
            order.append(sighting.code)
            by_code[sighting.code] = []
        by_code[sighting.code].append(sighting)

    candidates = []
    for code in order:
        group = by_code[code]
        fresh = any(_is_fresh(s, now, max_age) for s in group)
        golden = any(s.golden for s in group)
        best = min(group, key=_sighting_key)
        candidates.append(
            CodeCandidate(
                code=code,
                golden=golden,
                source_name=best.source_name,
                item_url=best.item_url,
                fresh=fresh,
            )
        )
    return candidates


def seeding_healthy(results: list[CollectorResult]) -> bool:
    """Whether this sweep is healthy enough to set the seeded marker (A1).

    "Healthy" means at least one non-skipped collector succeeded, and at
    least half of the non-skipped collectors succeeded -- a sweep where
    every real source timed out shouldn't get to declare "every code we
    saw (which is none) is the historical baseline" just because nothing
    technically errored on a source that was skipped for a missing API key.
    """
    non_skipped = [r for r in results if r.skipped is None]
    successes = sum(1 for r in non_skipped if r.error is None)
    return successes >= 1 and successes * 2 >= len(non_skipped)


def pings_used_today(state: AlertState, today: str) -> int:
    """How many pings the day named `today` (in `cfg.digest.timezone`) has already spent.

    `state.ping_count` only means anything alongside a matching
    `state.ping_day` -- a `ping_day` from a prior local day has already
    effectively reset to zero, it just hasn't been written back yet
    (`claim_codes` does that lazily, the next time it's asked to spend).
    """
    return state.ping_count if state.ping_day == today else 0


def plan_alerts(
    candidates: list[CodeCandidate],
    *,
    known: set[str],
    seeded: bool,
    seeding_ok: bool,
    pings_today: int,
    max_pings: int,
) -> AlertPlan:
    """Turn this batch's candidates into what to record and what to post.

    Codes already in `known` (any status, ever) are dropped outright --
    they're not this function's business anymore. What's left splits on
    `seeded`:

    - **Unseeded** (A1): every new code is recorded silently as `"seeded"`,
      nothing posts, and `mark_seeded` becomes `seeding_ok` -- the caller
      only gets to flip the marker on if this batch was healthy.
    - **Seeded**: stale codes (A11) are recorded silently as `"too_old"`
      and never get another chance; fresh codes are queued to post, in
      first-seen order. One ping covers the whole batch (A4): `ping` is
      true only if there's something to post and the day's budget isn't
      spent; `cap_reached` says the budget is what stopped the ping, not
      "nothing to post" -- a caller uses that to decide whether an admin
      alert about the cap is warranted.
    """
    new_candidates = [c for c in candidates if c.code not in known]

    if not seeded:
        return AlertPlan(
            silent=[(c, "seeded") for c in new_candidates],
            to_post=[],
            ping=False,
            cap_reached=False,
            mark_seeded=seeding_ok,
        )

    stale = [c for c in new_candidates if not c.fresh]
    fresh = [c for c in new_candidates if c.fresh]
    ping = bool(fresh) and pings_today < max_pings
    cap_reached = bool(fresh) and not ping
    return AlertPlan(
        silent=[(c, "too_old") for c in stale],
        to_post=fresh,
        ping=ping,
        cap_reached=cap_reached,
        mark_seeded=False,
    )


__all__ = [
    "AlertPlan",
    "CodeCandidate",
    "CodeSighting",
    "aggregate",
    "pings_used_today",
    "plan_alerts",
    "seeding_healthy",
    "sightings_from_items",
]
