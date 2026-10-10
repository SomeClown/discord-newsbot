"""What the bot knows about a SHiFT code besides its characters: who said it, and who wins.

A code can be described by three sources at once: Gearbox's own post, a fan site's
table, and (eventually) somebody's free text. They disagree about as often as you'd
expect. This module is the referee, and it is deliberately pure: give `merge_details`
the row as it stands plus one new observation and it hands back the new row, touching
no database and no clock but the one you pass in. SQL only stores the answer. Twelve
lines of `CASE` per column would have been harder to test and easier to get wrong, and
I'd have found out which at an inconvenient hour.

The rules (design.md §16), field by field for reward, category and expiry:
- an empty value never replaces a present one;
- otherwise a value replaces the stored one if its rank is at least as high, where
  official beats table beats nobody, and "at least" means the same rank's newer reading
  wins, so a fan site can correct itself;
- a source that merely agrees with what's stored doesn't get its name on it, unless it
  outranks the one that's there (two fan sites repeating each other shouldn't take turns
  being "via" every hour);
- `details_changed_at` moves only when something an alert shows changes (a value, or
  who supplied it), and only on the net difference, because that timestamp is what sends
  an edit to a posted message and nobody wants an edit for a change that undid itself;
  it also only moves forward, by at least a microsecond, so a change always looks newer
  than the message that doesn't show it yet. ("Shows" is `displayed`: a category change
  that doesn't change the line, golden to other, is not a change.) The store goes one
  better and keeps every new stamp above every other row's (`repo.upsert_code_details`),
  because a message's version stamp is the largest among its codes and has to be safe to
  compare with any of them.

The clock is the other half. A code is "current" until its expiry date ends in Pacific
time, or forever if it's permanent, or (no expiry known) 180 days after its clock
started. The clock starts once, when the row is created, and never moves again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from newsbot.collectors.base import ItemDetails
from newsbot.store.models import CodeDetailsRow, DetailSource

# Expiry dates on the sites we read, and on Gearbox's posts, are Pacific calendar days.
SHIFT_TZ = ZoneInfo("America/Los_Angeles")

# How long a code with no known expiry stays "current" after its clock starts.
CLOCK_DAYS = 180

# Higher beats lower. The empty rank is "nobody has said", and anything beats it.
RANK: dict[str, int] = {"": 0, "table": 1, "official": 2}

# The same caps the table's CHECKs enforce (migration 009); the collector and the official
# reader promise to stay under them, and a promise is a fine thing to double-check.
_CODE_LENGTH = 29
_TEXT_MAX = 300
# Config holds source names to 100 characters (they're printed in alerts as "via ..."), so a
# longer one is damage, and the store won't take it.
_NAME_MAX = 100
_CATEGORIES = frozenset({"golden", "cosmetic", "permanent", "other"})
# Dates outside these years are somebody's typo or somebody's joke. They're read as "unknown",
# which keeps `end_of_day` away from the edges of what a `datetime` can hold.
_YEAR_MIN, _YEAR_MAX = 2000, 2100

_EARLIEST = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DetailObservation:
    """One source's reading of one code: the details, who they came from, and when it was said.

    `published_at` only orders observations within a pass (later wins inside a rank); it
    is the item's publish time, or None when the source gave none.
    """

    code: str
    details: ItemDetails
    source_name: str
    published_at: datetime | None = None


def after(moment: datetime) -> datetime:
    """`moment` plus one microsecond, or `moment` itself at the very end of time.

    A stamp has to be strictly later than the one before it; a stamp that is already the
    last representable instant (a hand-edited row, nothing we'd write) has nowhere to go,
    and an `OverflowError` out of the middle of a release is a worse answer than a tie.
    """
    try:
        return moment + timedelta(microseconds=1)
    except OverflowError:
        return moment


def _utc(moment: datetime) -> datetime:
    """An aware UTC datetime; a naive one is taken to already be UTC, like the rest of the store."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def sort_observations(observations: Iterable[DetailObservation]) -> list[DetailObservation]:
    """Order a pass's observations oldest first: `(published_at or earliest, input order)`.

    Rank decides across ranks whatever the order, but inside a rank the later reading
    wins, so the later one has to be folded in last. `sorted` is stable, which is the
    "input order" half of the key for free.
    """
    return sorted(
        observations,
        key=lambda o: _utc(o.published_at) if o.published_at is not None else _EARLIEST,
    )


def _clean_text(label: str, text: str) -> None:
    """Refuse text SQLite can't hold faithfully: unencodable (a lone surrogate) or holding a NUL.

    SQLite's `length()` stops counting at a NUL, so a CHECK on it can be talked past; keeping
    NULs out here is cheaper than finding out what else they confuse.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} can't be encoded as UTF-8") from None
    if "\0" in text:
        raise ValueError(f"{label} contains a NUL")


def _shown_length(text: str) -> int:
    # Imported here because format.py imports this module (for the Pacific day helpers), so
    # a top-level import would be a circle, and I have met enough of those.
    from newsbot.bot.format import discord_len, esc

    return discord_len(esc(text))


def _year_ok(d: date | None) -> date | None:
    return d if d is not None and _YEAR_MIN <= d.year <= _YEAR_MAX else None


def clean_observation(obs: DetailObservation) -> DetailObservation:
    """Return `obs` with anything the store would refuse removed, or raise ValueError.

    Whole-observation refusals (the caller skips these): a code that isn't 29 plain characters,
    a category or rank outside the allowed sets, text that can't be stored, and reward or origin
    longer than 300 *as shown* (`discord_len(esc(text))`, the plan's real cap; a raw-length check
    would wave through 300 asterisks that are 600 units on screen). Dates outside 2000 to 2100 are
    not worth refusing the rest over, so they're dropped as unknown.
    """
    d = obs.details
    if obs.code != d.code:
        raise ValueError(f"observation for {obs.code!r} carries details for {d.code!r}")
    if len(obs.code) != _CODE_LENGTH:
        raise ValueError(f"{obs.code!r} isn't {_CODE_LENGTH} characters")
    if d.rank not in ("official", "table"):
        raise ValueError(f"unknown detail rank {d.rank!r}")
    if d.category is not None and d.category not in _CATEGORIES:
        raise ValueError(f"unknown category {d.category!r}")
    _clean_text("code", obs.code)
    _clean_text("source name", obs.source_name)
    if len(obs.source_name) > _NAME_MAX:
        raise ValueError(f"source name over {_NAME_MAX} characters for {obs.code!r}")
    for label, text in (("reward", d.reward), ("origin", d.origin)):
        _clean_text(label, text)
        if len(text) > _TEXT_MAX or _shown_length(text) > _TEXT_MAX:
            raise ValueError(f"{label} over {_TEXT_MAX} characters as shown for {obs.code!r}")
    if _year_ok(d.expires_on) is d.expires_on and _year_ok(d.added_on) is d.added_on:
        return obs
    return replace(
        obs,
        details=replace(d, expires_on=_year_ok(d.expires_on), added_on=_year_ok(d.added_on)),
    )


def _take[T](
    old: T,
    old_source: DetailSource,
    old_from: str,
    new: T | None,
    rank: DetailSource,
    name: str,
) -> tuple[T, DetailSource, str]:
    """One field's precedence: an empty `new` keeps the old triple, else rank decides.

    A different value wins at an equal or higher rank (`>=`: equal rank means the newer
    reading wins). The same value only changes hands upward: a higher rank takes the name,
    an equal or lower one leaves it alone, so agreeing sources don't take turns.
    """
    if new is None or new == "":
        return old, old_source, old_from
    if new == old:
        if RANK[rank] > RANK[old_source]:
            return new, rank, name
        return old, old_source, old_from
    if RANK[rank] >= RANK[old_source]:
        return new, rank, name
    return old, old_source, old_from


def displayed(row: CodeDetailsRow | None) -> tuple:
    """What an alert's details line shows for `row`, as a value that compares equal when it does.

    Mirrors `format.details_line`, and nothing more: the reward and, if there is one, who
    supplied it; for a permanent code the word "permanent" and who set the category; for any
    other code the expiry and who supplied it. A category that never reaches the line
    (golden, cosmetic, other) isn't in here, so changing it doesn't send an edit to every
    message that holds the code. None, like a row with nothing to show, is the all-empty
    tuple, which is what makes "the first details" a change.
    """
    if row is None:
        return ("", "", False, "", None, "")
    reward = row.reward.strip()
    if row.category == "permanent":
        return (reward, row.reward_from if reward else "", True, row.category_from, None, "")
    return (
        reward,
        row.reward_from if reward else "",
        False,
        "",
        row.expires_on,
        row.expires_from if row.expires_on is not None else "",
    )


def silenced_by_expiry(row: CodeDetailsRow | None, now: datetime, *, official_seen: bool) -> bool:
    """Whether a code's expiry has passed and the expiry is believable enough to silence it.

    The one rule for "dead on arrival", shared by release (`fanout._dead_on_arrival`) and by
    delivery's last look at a queued code, so the two can't drift apart. A table's expiry is
    a stranger's typing: if an official-trust source named the code, a typo in somebody
    else's table doesn't get to silence Gearbox. Only an expiry that came from an official
    source can, and then an official mention doesn't help.
    """
    if not expired_by_expiry(row, now):
        return False
    return not official_seen or (row is not None and row.expires_source == "official")


def _clock_start(
    first_seen_at: datetime | None, details_since: datetime, now: datetime
) -> datetime:
    """When a brand-new row's 180-day clock starts (design.md §16; plan §1 item 6).

    A code first seen at or after the 009 marker keeps that first sighting (but never from the
    future: a clock that stepped backwards doesn't get to start tomorrow). Anything else
    (a code the bot has never heard of, or one that was around before this release, which
    is the owner's rollout rule) starts now. That includes an older code re-mentioned by a
    single-code official post: Gearbox repeating it is a fair sign it's current.
    """
    if first_seen_at is not None and _utc(first_seen_at) >= _utc(details_since):
        return min(_utc(first_seen_at), now)
    return now


def merge_details(
    old: CodeDetailsRow | None,
    obs: DetailObservation,
    *,
    now: datetime,
    first_seen_at: datetime | None,
    details_since: datetime,
) -> CodeDetailsRow:
    """Fold one observation into a code's stored details; return the new row.

    `old` is None when the code has no row yet. `first_seen_at` is the code's
    `alerted_codes.first_seen_at` (None if the bot hasn't released it) and
    `details_since` is migration 009's rollout marker; the two only matter for a new row's
    clock. Raises ValueError for an observation the store would refuse (see `clean_observation`),
    so the caller can skip it without losing a batch.
    """
    obs = clean_observation(obs)
    now = _utc(now)
    d = obs.details
    base = old or CodeDetailsRow(
        code=obs.code,
        clock_start=_clock_start(first_seen_at, details_since, now),
        updated_at=now,
    )

    reward, reward_source, reward_from = _take(
        base.reward,
        base.reward_source,
        base.reward_from,
        d.reward if d.reward.strip() else None,
        d.rank,
        obs.source_name,
    )
    category, category_source, category_from = _take(
        base.category,
        base.category_source,
        base.category_from,
        d.category,
        d.rank,
        obs.source_name,
    )
    expires_on, expires_source, expires_from = _take(
        base.expires_on,
        base.expires_source,
        base.expires_from,
        d.expires_on,
        d.rank,
        obs.source_name,
    )

    # "Added" and "where it came from" are a table's business; nobody else outranks or
    # contradicts them, so a present value replaces and an absent one never erases.
    added_on, origin = base.added_on, base.origin
    if d.rank == "table":
        added_on = d.added_on or base.added_on
        origin = d.origin if d.origin.strip() else base.origin

    merged = replace(
        base,
        reward=reward,
        reward_source=reward_source,
        reward_from=reward_from,
        category=category,
        category_source=category_source,
        category_from=category_from,
        expires_on=expires_on,
        expires_source=expires_source,
        expires_from=expires_from,
        added_on=added_on,
        origin=origin,
        first_listed_at=base.first_listed_at or (now if d.listed else None),
        last_listed_at=now if d.listed else base.last_listed_at,
        updated_at=max(_utc(base.updated_at), now),
    )
    changed = displayed(merged) != displayed(old)
    if not changed:
        return merged
    # Strictly forward, by at least a microsecond. An edit is due when a code's stamp is
    # newer than the one its message was rendered with, and "newer" means greater, not equal:
    # a clock that stepped back, or one that stood still between two real changes, would
    # otherwise hand the change a stamp the message already shows and the edit would never
    # come. The microsecond is the price of that, and it's cheap.
    if base.details_changed_at:
        stamp = max(after(_utc(base.details_changed_at)), now)
    else:
        stamp = now
    return replace(merged, details_changed_at=stamp)


def merge_observations(
    old: CodeDetailsRow | None,
    observations: Iterable[DetailObservation],
    *,
    now: datetime,
    first_seen_at: datetime | None,
    details_since: datetime,
) -> CodeDetailsRow:
    """Fold one code's observations (oldest first, see `sort_observations`) into its row.

    Same as calling `merge_details` in a loop, except `details_changed_at` answers for the
    whole batch: if the final row shows what the stored one showed, the stamp goes back to
    what it was, so two sources that disagree can't flip a value and flip it back and call
    that news. Needs at least one observation.
    """
    row = old
    for obs in observations:
        row = merge_details(
            row, obs, now=now, first_seen_at=first_seen_at, details_since=details_since
        )
    if row is None:
        raise ValueError("merge_observations needs at least one observation")
    if old is not None and displayed(row) == displayed(old):
        row = replace(row, details_changed_at=old.details_changed_at)
    elif row.details_changed_at is not None:
        # The chain bumped the stamp once per observation; a net change is one change, so it
        # gets one stamp: strictly after the old one, and `now` when the clock is behaving.
        previous = old.details_changed_at if old is not None else None
        stamp = max(after(_utc(previous)), now) if previous else now
        row = replace(row, details_changed_at=stamp)
    return row


def end_of_day(d: date) -> datetime:
    """The instant `d` ends in Pacific time, as UTC: midnight at the start of the next day.

    Built from the next day's midnight and not "23:59:59 plus a bit", so the 23-hour and
    25-hour days at the two DST changes come out right on their own. A code is current
    while `now < end_of_day(expires_on)`. The last days a `datetime` can hold have no next
    midnight to build from; they end at `datetime.max`, which is as close to "never" as
    this gets (and stops a stored `9999-12-31` from crashing the clock).
    """
    try:
        return datetime.combine(d + timedelta(days=1), time.min, tzinfo=SHIFT_TZ).astimezone(UTC)
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


def current_until(row: CodeDetailsRow | None, first_seen_at: datetime | None) -> datetime | None:
    """When a code stops being current; None means never (a permanent code).

    In order: permanent codes never end; a known expiry ends at the end of that Pacific
    day; otherwise the clock runs 180 days from `clock_start`, or from the code's first
    sighting when it has no details row at all. Raises ValueError if there's no row and no
    first sighting, because then there's nothing to count from.
    """
    if row is not None:
        if row.category == "permanent":
            return None
        if row.expires_on is not None:
            return end_of_day(row.expires_on)
        return _utc(row.clock_start) + timedelta(days=CLOCK_DAYS)
    if first_seen_at is None:
        raise ValueError("a code with no details row needs its first sighting")
    return _utc(first_seen_at) + timedelta(days=CLOCK_DAYS)


def expired_by_expiry(row: CodeDetailsRow | None, now: datetime) -> bool:
    """Whether `row` carries an expiry that has already ended at `now` (Pacific end of day).

    Only a *known* expiry counts. A permanent code never expires, whatever date a stray
    table row left on it, which is how `is_current` already sees it. A code with no
    expiry, or no row, isn't "expired" here; it just hasn't said (the 180-day clock is
    `current_until`'s business, and it is not a reason to call anything dead on arrival).
    """
    if row is None or row.category == "permanent" or row.expires_on is None:
        return False
    return _utc(now) >= end_of_day(row.expires_on)


def is_current(
    row: CodeDetailsRow | None, first_seen_at: datetime | None, *, now: datetime
) -> bool:
    """Whether the code should still work at `now` (see `current_until`)."""
    until = current_until(row, first_seen_at)
    return until is None or _utc(now) < until
