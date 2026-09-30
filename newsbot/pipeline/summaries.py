"""One Claude summary per game per day, made once and shared by every comped server.

In v2 the daily run collected, summarized and posted in one breath, for one
server, and the summary was a private matter between me and Haiku. With more
than one comped server that's a poor arrangement: two servers following
Palworld would pay for the same paragraph twice and, worse, might get two
slightly different ones. So the summary moved out of the digest. It's made
ahead of time by a job (`prepare_summaries`), stored in `game_summaries` with
its stories, and every comped digest just reads it. Think of it as the
kitchen cooking one big pot before anyone sits down, instead of a pot per
table.

The prompt is deliberately untouched. `summarize_topic` still builds the exact
same two strings v2 did, from the same kind of inputs (the stored items in a
window, capped and ordered the way `filter_items` capped and ordered them, and
the prior headlines); all that changed is where the items come from and where
the result goes. The one moving part is the `{games}` list in the system
prompt, which is now "every game a comped server follows" instead of "every
game in the config". With only the friend's server comped that's the same
three games in the same order, so the text is byte-for-byte what v2 sent (a
test pins it). Add a comped server that follows something else and the list,
and so the prompt, changes: that needs an owner `/newsbot preview` first.

Failure is boring on purpose. A summary that can't be made is saved as
`fallback` (the digest then posts headlines with "Summary unavailable"), the
owner hears about it once, and nothing retries it that day: `summarize_topic`
already spent its own three attempts. The one exception is a confirmed
run-now, which is an admin asking out loud. Game A failing never stops game B.

Two servers that want the same game's summary at the same moment must cost one
Claude call, not two. The claim is a row in `app_state`, written under
`BEGIN IMMEDIATE` in the same transaction as the "is there one already?"
check; whoever loses waits (politely, on a timer, not in a loop) and then
reads the winner's row.

The bot's scheduler doesn't run any of this yet (plan task 13).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from newsbot.collectors.base import RawItem
from newsbot.config import AppConfig, GameCfg
from newsbot.guilds.schedule import digest_window, local_due_instant
from newsbot.pipeline.collect import CollectionDeps, collect_web_search
from newsbot.pipeline.filter import TopicItem, _cap_key
from newsbot.pipeline.guild_digest import GameSummary, SummaryLookup, summary_from_row
from newsbot.pipeline.run import _PRIOR_HEADLINE_WINDOW, local_run_date
from newsbot.pipeline.summarize import _FALLBACK_NOTE, LLMClient, TopicSummary, summarize_topic
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import CompedFollow, GameSummaryRow, StoryToSave, Usage

logger = logging.getLogger(__name__)

# The prepare job starts this long before the earliest comped digest, so the
# digest never waits on Claude.
LEAD = timedelta(minutes=30)
# How long a caller that lost the claim waits between looks, and how many looks
# it gets (together: the claim's lease, after which the claim is fair game).
_POLL_S = 2.0
_MAX_POLLS = int(repo.SUMMARY_CLAIM_LEASE.total_seconds() / _POLL_S)
_WEB_SEARCH_SKIPPED = "web search skipped"

Sleep = Callable[[float], Awaitable[None]]
Alert = Callable[[str], Awaitable[None]]


@dataclass
class SummaryDeps:
    cfg: AppConfig
    db_path: str
    llm: LLMClient
    now: Callable[[], datetime]
    # The owner's alert channel, never a server's.
    alert: Alert
    # What `collect_web_search` needs, and the Brave key. Without both, web
    # search is skipped with a coverage note rather than failing anything.
    collection: CollectionDeps | None = None
    brave_api_key: str | None = None
    # Used for the model's retry backoff and for waiting on somebody else's claim.
    sleep: Sleep = asyncio.sleep


# --- Who needs a summary, and when ---


def earliest_dues(
    follows: Sequence[CompedFollow], now: datetime
) -> dict[str, tuple[datetime, date]]:
    """Per game, the earliest upcoming comped digest instant and its local date.

    "Upcoming" includes a due time that passed less than a reuse tolerance ago
    (30 minutes): that digest may still be about to need its summary. Once a
    server's time today is further back than that, its next one is tomorrow's.
    A server with an unusable zone or time is skipped; its own digest will say so.
    """
    best: dict[str, tuple[datetime, date]] = {}
    for follow in follows:
        try:
            day = local_run_date(now, follow.timezone)
            due = local_due_instant(day, follow.digest_time, follow.timezone)
            if now > due + repo.SUMMARY_REUSE_AFTER:
                day += timedelta(days=1)
                due = local_due_instant(day, follow.digest_time, follow.timezone)
        except ZoneInfoNotFoundError, ValueError, OSError:
            continue
        if follow.game_key not in best or due < best[follow.game_key][0]:
            best[follow.game_key] = (due, day)
    return best


def _all_topics(cfg: AppConfig, followed: set[str], game: GameCfg) -> list[GameCfg]:
    """Every game a comped server follows, in catalog order: what the prompt says we track."""
    return [g for g in cfg.catalog if g.key in followed or g.key == game.key]


# --- Making one summary ---


def _claim_sync(
    db_path: str, game_key: str, due_at: datetime, token: str, now, retry_fallback: bool
):
    with closing(connect(db_path)) as conn:
        state, row = repo.claim_game_summary(
            conn, game_key, due_at, token=token, now=now, retry_fallback=retry_fallback
        )
        summary = summary_from_row(conn, row) if row is not None and state == "reusable" else None
        return state, row, summary


def _release_sync(db_path: str, game_key: str, token: str) -> None:
    with closing(connect(db_path)) as conn:
        repo.release_game_summary_claim(conn, game_key, token)


def _inputs_sync(
    db_path: str,
    cfg: AppConfig,
    game: GameCfg,
    end: datetime,
    retrying: GameSummaryRow | None,
):
    """The window, the capped items and the prior headlines for one game, read together."""
    with closing(connect(db_path)) as conn:
        if retrying is not None:
            start = retrying.window_start
        else:
            latest = repo.latest_game_summary(conn, game.key)
            start, _ = digest_window(end, latest.window_end if latest else None)
        stored = repo.summary_items(conn, game.key, start, end)
        prior = repo.recent_headlines(conn, game.key, end - _PRIOR_HEADLINE_WINDOW)
        followed = {f.game_key for f in repo.comped_follows(conn)}
    # The same cap `filter_items` applied in v2: confident before uncertain, then
    # trust, then recency, and only the top N make it to the model.
    topic_items = sorted(
        (
            TopicItem(
                item=RawItem(
                    url=i.url,
                    title=i.title,
                    excerpt=i.excerpt,
                    source_name=i.source_name,
                    trust=i.trust,
                    published_at=i.published_at,
                    topics=(game.key,),
                ),
                topic_key=game.key,
                uncertain=i.topics[game.key],
            )
            for i in stored
        ),
        key=_cap_key,
    )[: cfg.collection.max_items_per_game]
    return start, topic_items, prior, followed


async def _summarize(deps: SummaryDeps, game: GameCfg, retrying: GameSummaryRow | None):
    """Run the model for one game. Returns (window start, end, summary, items' stories)."""
    end = deps.now()
    start, items, prior, followed = await asyncio.to_thread(
        _inputs_sync, deps.db_path, deps.cfg, game, end, retrying
    )
    if not items:
        # Like v2: no items, no call, no story. Saved as `ok` so the next digest
        # doesn't go looking for a summary that will never exist.
        return start, end, TopicSummary(game.key, [], False, None, Usage(0, 0))
    try:
        summary = await summarize_topic(
            deps.llm,
            game,
            items,
            prior,
            all_topics=_all_topics(deps.cfg, followed, game),
            subject=deps.cfg.ai.subject,
            sleep=deps.sleep,
        )
    except Exception:
        # `summarize_topic` turns API trouble into a fallback itself; this is for
        # the bug I haven't met yet, which gets the same treatment.
        logger.exception("summarizing %s failed unexpectedly", game.key)
        summary = TopicSummary(game.key, [], True, _FALLBACK_NOTE, Usage(0, 0))
    return start, end, summary


def _save_sync(
    db_path: str,
    game_key: str,
    run_date: date,
    start: datetime,
    end: datetime,
    summary: TopicSummary,
    coverage: list[str],
    token: str,
    now,
) -> GameSummary:
    with closing(connect(db_path)) as conn:
        summary_id = repo.save_game_summary(
            conn,
            game_key=game_key,
            run_date=run_date,
            status="fallback" if summary.fallback else "ok",
            window_start=start,
            window_end=end,
            coverage_notes=coverage,
            note=summary.note,
            usage=summary.usage,
            stories=[
                StoryToSave(
                    topic_key=game_key,
                    headline=d.headline,
                    summary=d.summary,
                    label=d.label,
                    item_urls=d.item_urls,
                    update_of_story_id=d.update_of_story_id,
                )
                for d in summary.stories
            ],
            token=token,
            now=now,
        )
        row = repo.game_summary_by_id(conn, summary_id)
        if row is None:
            raise RuntimeError(f"summary {summary_id} vanished right after it was saved")
        return summary_from_row(conn, row)


async def ensure_summary(
    deps: SummaryDeps,
    game_key: str,
    due_at: datetime,
    *,
    run_date: date,
    coverage: Sequence[str] = (),
    retry_fallback: bool = False,
) -> GameSummary | None:
    """The summary a digest due at `due_at` should use for `game_key`, making it if nobody has.

    Reuses a stored one inside the reuse window. Otherwise claims the game
    (one caller wins, the rest wait and then reuse the result), summarizes,
    saves, and releases. `None` means there was nothing to be had: an unknown
    game, or a claim that never resolved inside its lease.

    `retry_fallback` (a confirmed run-now) remakes a `fallback` summary once for
    this call. A caller that had to wait for somebody else's claim doesn't
    retry on top of it: they just got an answer.
    """
    game = next((g for g in deps.cfg.catalog if g.key == game_key), None)
    if game is None:
        return None
    token = uuid.uuid4().hex
    retry = retry_fallback
    retrying: GameSummaryRow | None = None
    polls = 0
    while True:
        state, row, stored = await asyncio.to_thread(
            _claim_sync, deps.db_path, game_key, due_at, token, deps.now, retry
        )
        if state == "reusable":
            return stored
        if state == "claimed":
            retrying = row
            break
        retry = False
        if polls >= _MAX_POLLS:
            logger.warning("gave up waiting for %s's summary claim", game_key)
            return None
        polls += 1
        await deps.sleep(_POLL_S)

    try:
        start, end, summary = await _summarize(deps, game, retrying)
        saved = await asyncio.to_thread(
            _save_sync,
            deps.db_path,
            game_key,
            run_date,
            start,
            end,
            summary,
            list(coverage),
            token,
            deps.now,
        )
    finally:
        # Normally `save_game_summary` already dropped the claim; this covers a
        # cancel or a save that blew up, so nobody waits out the whole lease.
        try:
            await asyncio.to_thread(_release_sync, deps.db_path, game_key, token)
        except Exception:
            logger.exception("couldn't release the summary claim for %s", game_key)
    if saved.status == "fallback":
        try:
            await deps.alert(f"newsbot: summary for {game.name} fell back to headlines")
        except Exception:
            logger.exception("couldn't send the summary fallback alert for %s", game_key)
    return saved


# --- The lookups a digest uses ---


def summary_lookup(deps: SummaryDeps) -> SummaryLookup:
    """The comped digest's lookup: the stored summary, or one made right now.

    That's the "prepare job didn't run" path (downtime, or a server that was
    set up late): one inline attempt, no web search, and a coverage note saying
    so. If that fails too the row is `fallback` like any other.
    """

    async def lookup(game_key: str, due_at: datetime) -> GameSummary | None:
        return await ensure_summary(
            deps,
            game_key,
            due_at,
            run_date=due_at.astimezone(UTC).date(),
            coverage=[_WEB_SEARCH_SKIPPED] if deps.cfg.web_search is not None else [],
        )

    return lookup


def retry_lookup(deps: SummaryDeps) -> SummaryLookup:
    """The run-now lookup: like `summary_lookup`, but a `fallback` gets one more try."""

    async def lookup(game_key: str, due_at: datetime) -> GameSummary | None:
        return await ensure_summary(
            deps,
            game_key,
            due_at,
            run_date=due_at.astimezone(UTC).date(),
            coverage=[_WEB_SEARCH_SKIPPED] if deps.cfg.web_search is not None else [],
            retry_fallback=True,
        )

    return lookup


# --- The prepare job ---


def _has_summary_sync(db_path: str, game_key: str, due_at: datetime) -> bool:
    with closing(connect(db_path)) as conn:
        return repo.get_game_summary(conn, game_key, due_at) is not None


def _follows_sync(db_path: str) -> list[CompedFollow]:
    with closing(connect(db_path)) as conn:
        return repo.comped_follows(conn)


async def _web_search(deps: SummaryDeps, game_keys: list[str]) -> list[str]:
    """Search the web for these games; the coverage notes to store if it couldn't."""
    if deps.cfg.web_search is None:
        return []
    if deps.collection is None or not deps.brave_api_key:
        return [_WEB_SEARCH_SKIPPED]
    try:
        outcome = await collect_web_search(
            deps.collection, game_keys, deps.brave_api_key, sleep=deps.sleep
        )
    except Exception:
        logger.exception("web search for the summaries failed")
        return [_WEB_SEARCH_SKIPPED]
    # Nothing ran: the quota said no (a skip, not a failure).
    return [_WEB_SEARCH_SKIPPED] if outcome.sources_total == 0 else []


async def prepare_summaries(deps: SummaryDeps, now: datetime | None = None) -> list[str]:
    """Make the summaries the next comped digests will need. Returns the games it worked on.

    Meant to run every five minutes. For each game a comped server follows it
    finds the earliest digest due; once that's within `LEAD`, and no summary
    (of any status: a fallback isn't retried today) is reusable for it, the
    game is searched and summarized. Web search runs first, once for all the
    games that need it, so the summary can see what it found.
    """
    now = now or deps.now()
    follows = await asyncio.to_thread(_follows_sync, deps.db_path)
    dues = earliest_dues(follows, now)
    todo: list[tuple[GameCfg, datetime, date]] = []
    for game in deps.cfg.catalog:
        if game.key not in dues:
            continue
        due, run_date = dues[game.key]
        if now < due - LEAD:
            continue
        if await asyncio.to_thread(_has_summary_sync, deps.db_path, game.key, due):
            continue
        todo.append((game, due, run_date))
    if not todo:
        return []

    notes = await _web_search(deps, [g.key for g, _, _ in todo])
    done: list[str] = []
    for game, due, run_date in todo:
        try:
            await ensure_summary(deps, game.key, due, run_date=run_date, coverage=notes)
            done.append(game.key)
        except Exception:
            # One game's bad day doesn't cancel the others'.
            logger.exception("preparing the summary for %s failed", game.key)
    return done


__all__ = [
    "LEAD",
    "SummaryDeps",
    "earliest_dues",
    "ensure_summary",
    "prepare_summaries",
    "retry_lookup",
    "summary_lookup",
]
