"""One Claude summary per game per cycle, made once and shared by every comped server.

In v2 the daily run collected, summarized and posted in one breath, for one
server, and the summary was a private matter between me and Haiku. With more
than one comped server that's a poor arrangement: two servers following
Palworld would pay for the same paragraph twice and, worse, might get two
slightly different ones. So the summary moved out of the digest. It's made
ahead of time by a job (`prepare_summaries`), stored in `game_summaries` with
its stories, and every comped digest just reads it. Think of it as the
kitchen cooking one big pot before anyone sits down, instead of a pot per
table.

Whether a pot is still good is decided per server, not per clock: a stored
summary is reusable for a server's digest if it starts exactly where that
server's last coverage of the game ended (so nobody misses news or reads the
same paragraph twice) and is no more than six hours older than the digest's due
time (so nobody reads yesterday's news). "Where it ended" is an item id, not a
time, because a collection pass stamps its items when it starts and stores them
minutes later (`repo.item_range` has the whole sad story). My first rule was
"anything from the last 24 hours", which a 23-hour spring forward day, a moved
digest time, and one late outage each found a way to break; my second chained the
summaries per game, which shared one summary between a 09:00 server and a 21:00
one and handed each of them half the other's news. Servers on one schedule still
share one call; servers a half day apart each get their own window and their own
call, so the honest bound is one call per distinct comped cycle per game, not one
per game.

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
reads the winner's row. The winner runs the web search and the model call
inside the claim, refreshes it while it works, gives the model call a
deadline, and saves only if the claim is still its own (lose it too many times
running and the caller gives up and falls back). A game whose saves keep failing
is left alone for a while (30 minutes, doubling, kept in memory) instead of being
paid for every five minutes.

`NewsBot` runs `prepare_summaries` every five minutes and hands the lookups to the
digest job.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from newsbot.collectors.base import RawItem
from newsbot.config import AppConfig, GameCfg
from newsbot.guilds.schedule import digest_window, item_floor, local_due_instant
from newsbot.pipeline.collect import CollectionDeps, collect_web_search
from newsbot.pipeline.filter import TopicItem, _cap_key
from newsbot.pipeline.guild_digest import (
    GameSummary,
    SummaryLookup,
    _stored_summary,
    summary_from_row,
)
from newsbot.pipeline.run import _PRIOR_HEADLINE_WINDOW, local_run_date
from newsbot.pipeline.summarize import _FALLBACK_NOTE, LLMClient, TopicSummary, summarize_topic
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import CompedFollow, Coverage, GameSummaryRow, StoryToSave, Usage

logger = logging.getLogger(__name__)

# The prepare job starts this long before a comped digest, so the digest never
# waits on Claude.
LEAD = timedelta(minutes=30)
# A digest whose due time passed this recently may still be about to need its summary.
_DUE_GRACE = timedelta(minutes=30)
# How long one model call for one game may take. A real one is three attempts at
# 60 s plus 7 s of backoff (about three minutes), so this means "it's hung", not
# "it's being thorough"; it's also well inside the claim's ten-minute lease.
SUMMARIZE_TIMEOUT_S = 300.0
_TIMEOUT_NOTE = "Summary unavailable (the model took too long); showing headlines."
# After a failed save the game is left alone for this long, doubling per failure up
# to the cap. In memory (on `SummaryDeps`), not in `app_state`: the likeliest reason
# a save fails is that the database is unwell, and a backoff that needs the database
# to be written down is not much of a backoff. A restart forgets it; that's fine.
SAVE_BACKOFF = timedelta(minutes=30)
SAVE_BACKOFF_MAX = timedelta(hours=8)
# How often the claim's lease is pushed out while somebody is still working.
_HEARTBEAT_S = repo.SUMMARY_CLAIM_LEASE.total_seconds() / 5
# How long a caller that lost the claim waits between looks, and how many looks
# it gets (together: the claim's lease, after which the claim is fair game).
_POLL_S = 2.0
_MAX_POLLS = int(repo.SUMMARY_CLAIM_LEASE.total_seconds() / _POLL_S)
_WEB_SEARCH_SKIPPED = "web search skipped"
# How long a preview's in-memory summary is handed to the next preview of the same game and
# coverage. Previews save nothing, so without this every `/newsbot preview` is a model call.
PREVIEW_SUMMARY_TTL = timedelta(minutes=30)
# How many times `ensure_summary` may find its claim taken out from under it and start
# over before it stops and lets the digest fall back to headlines. The takeover only
# happens if a heartbeat dies, so one retry is generous and three is a leash.
_MAX_LOST_CLAIMS = 3

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
    summarize_timeout_s: float = SUMMARIZE_TIMEOUT_S
    heartbeat_s: float = _HEARTBEAT_S
    # game key -> (consecutive failed saves, not before). See `SAVE_BACKOFF`.
    save_failures: dict[str, tuple[int, datetime]] = field(default_factory=dict)


# --- Who needs a summary, and when ---


def upcoming_dues(
    follows: Sequence[CompedFollow], now: datetime
) -> list[tuple[CompedFollow, datetime, date]]:
    """Each comped server's next digest instant per game it follows, with its local date.

    "Next" includes a due time that passed less than a grace period ago (30
    minutes): that digest may still be about to need its summary. Once a
    server's time today is further back than that, its next one is tomorrow's.
    A server with an unusable zone or time is skipped; its own digest will say so.
    """
    out: list[tuple[CompedFollow, datetime, date]] = []
    for follow in follows:
        try:
            day = local_run_date(now, follow.timezone)
            due = local_due_instant(day, follow.digest_time, follow.timezone)
            if now > due + _DUE_GRACE:
                day += timedelta(days=1)
                due = local_due_instant(day, follow.digest_time, follow.timezone)
        except ZoneInfoNotFoundError, ValueError, OSError:
            continue
        out.append((follow, due, day))
    return out


def _all_topics(cfg: AppConfig, followed: set[str], game: GameCfg) -> list[GameCfg]:
    """Every game a comped server follows, in catalog order: what the prompt says we track."""
    return [g for g in cfg.catalog if g.key in followed or g.key == game.key]


# --- Making one summary ---


def _claim_sync(
    db_path: str,
    game_key: str,
    due_at: datetime,
    after: Coverage | None,
    token: str,
    now,
    retry_fallback: bool,
):
    with closing(connect(db_path)) as conn:
        state, row = repo.claim_game_summary(
            conn,
            game_key,
            due_at,
            token=token,
            now=now,
            retry_fallback=retry_fallback,
            after=after,
        )
        summary = summary_from_row(conn, row) if row is not None and state == "reusable" else None
        return state, row, summary


def _release_sync(db_path: str, game_key: str, token: str) -> None:
    with closing(connect(db_path)) as conn:
        repo.release_game_summary_claim(conn, game_key, token)


def _refresh_sync(db_path: str, game_key: str, token: str, now) -> bool:
    with closing(connect(db_path)) as conn:
        return repo.refresh_game_summary_claim(conn, game_key, token, now)


@dataclass(frozen=True)
class _Made:
    """One summarize run: the window it labels, the item ids it really covers, the result."""

    start: datetime
    end: datetime
    items_after: int | None
    items_upto: int
    summary: TopicSummary


def _inputs_sync(
    db_path: str,
    cfg: AppConfig,
    game: GameCfg,
    end: datetime,
    after: Coverage | None,
    retrying: GameSummaryRow | None,
):
    """The window, item ids, capped items and prior headlines for one game, read together.

    The items are the ids past `after` (the asking server's coverage of this game, so the
    summary starts exactly where the server left off) up to the newest stored by `end`;
    a retried row keeps the lower bound it was made with. The time window is only the
    label and a floor.
    """
    with closing(connect(db_path)) as conn:
        if retrying is not None:
            start, items_after = retrying.window_start, retrying.items_after
        else:
            # With no coverage at all (a server's first digest) there's no lower id, and the
            # floor keeps it to a day. A server adopted from v2.2 has coverage derived from its
            # last digest, so v2.2's items aren't fed in again.
            start = digest_window(end, after.end if after else None)[0]
            items_after = after.item_id if after else None
        upto = max(repo.latest_item_id(conn, collected_by=end), items_after or 0)
        stored = repo.summary_items(
            conn,
            game.key,
            item_floor(end, items_after is not None),
            end,
            after_id=items_after,
            upto_id=upto,
        )
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
    return start, items_after, upto, topic_items, prior, followed


async def _summarize(
    deps: SummaryDeps, game: GameCfg, retrying: GameSummaryRow | None, after: Coverage | None = None
) -> _Made:
    """Run the model for one game."""
    end = deps.now()
    start, items_after, upto, items, prior, followed = await asyncio.to_thread(
        _inputs_sync, deps.db_path, deps.cfg, game, end, after, retrying
    )
    if not items:
        # Like v2: no items, no call, no story. Saved as `ok` so the next digest
        # doesn't go looking for a summary that will never exist.
        return _Made(
            start, end, items_after, upto, TopicSummary(game.key, [], False, None, Usage(0, 0))
        )
    try:
        async with asyncio.timeout(deps.summarize_timeout_s):
            summary = await summarize_topic(
                deps.llm,
                game,
                items,
                prior,
                all_topics=_all_topics(deps.cfg, followed, game),
                subject=deps.cfg.ai.subject,
                sleep=deps.sleep,
            )
    except TimeoutError:
        # A hung call would otherwise hold this game's claim, and every game
        # after it in the prepare run, until somebody restarted the bot.
        logger.warning("summarizing %s timed out after %s s", game.key, deps.summarize_timeout_s)
        summary = TopicSummary(game.key, [], True, _TIMEOUT_NOTE, Usage(0, 0))
    except Exception:
        # `summarize_topic` turns API trouble into a fallback itself; this is for
        # the bug I haven't met yet, which gets the same treatment.
        logger.exception("summarizing %s failed unexpectedly", game.key)
        summary = TopicSummary(game.key, [], True, _FALLBACK_NOTE, Usage(0, 0))
    return _Made(start, end, items_after, upto, summary)


def _save_sync(
    db_path: str,
    game_key: str,
    run_date: date,
    made: _Made,
    coverage: list[str],
    token: str,
    now,
    replace_id: int | None,
) -> GameSummary | None:
    """Save it, or `None` if the claim was lost while the model was thinking."""
    summary = made.summary
    with closing(connect(db_path)) as conn:
        summary_id = repo.save_game_summary(
            conn,
            game_key=game_key,
            run_date=run_date,
            status="fallback" if summary.fallback else "ok",
            window_start=made.start,
            window_end=made.end,
            items_after=made.items_after,
            items_upto=made.items_upto,
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
            replace_id=replace_id,
            require_claim=True,
        )
        if summary_id is None:
            return None
        row = repo.game_summary_by_id(conn, summary_id)
        if row is None:
            raise RuntimeError(f"summary {summary_id} vanished right after it was saved")
        return summary_from_row(conn, row)


def _backed_off(deps: SummaryDeps, game_key: str) -> bool:
    failed = deps.save_failures.get(game_key)
    return failed is not None and deps.now() < failed[1]


def _note_save_failure(deps: SummaryDeps, game_key: str) -> None:
    count = deps.save_failures.get(game_key, (0, deps.now()))[0] + 1
    wait = min(SAVE_BACKOFF * 2 ** (count - 1), SAVE_BACKOFF_MAX)
    deps.save_failures[game_key] = (count, deps.now() + wait)
    logger.warning("%s: save failed %d time(s), resting it for %s", game_key, count, wait)


async def _keep_claim(deps: SummaryDeps, game_key: str, token: str) -> None:
    """Push the claim's lease out while the owner is still working, so nobody takes it over."""
    while True:
        await asyncio.sleep(deps.heartbeat_s)
        try:
            ours = await asyncio.to_thread(_refresh_sync, deps.db_path, game_key, token, deps.now)
        except Exception:
            logger.exception("couldn't refresh the summary claim for %s", game_key)
            continue
        if not ours:
            return


async def _make(
    deps: SummaryDeps,
    game: GameCfg,
    run_date: date,
    coverage: Sequence[str],
    search: bool,
    retrying: GameSummaryRow | None,
    token: str,
    after: Coverage | None,
) -> GameSummary | None:
    """Do the work behind a won claim: search, summarize, save. `None` if the claim was lost."""
    beat = asyncio.create_task(_keep_claim(deps, game.key, token))
    try:
        if search:
            # Inside the claim, so two overlapping jobs search once, not twice.
            coverage = await _web_search(deps, [game.key])
        made = await _summarize(deps, game, retrying, after)
        try:
            saved = await asyncio.to_thread(
                _save_sync,
                deps.db_path,
                game.key,
                run_date,
                made,
                list(coverage),
                token,
                deps.now,
                retrying.id if retrying is not None else None,
            )
        except Exception:
            _note_save_failure(deps, game.key)
            raise
        deps.save_failures.pop(game.key, None)
        return saved
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        # Normally `save_game_summary` already dropped the claim; this covers a
        # cancel or a save that blew up, so nobody waits out the whole lease.
        try:
            await asyncio.to_thread(_release_sync, deps.db_path, game.key, token)
        except Exception:
            logger.exception("couldn't release the summary claim for %s", game.key)


async def ensure_summary(
    deps: SummaryDeps,
    game_key: str,
    due_at: datetime,
    *,
    run_date: date,
    after: Coverage | None = None,
    coverage: Sequence[str] = (),
    search: bool = False,
    retry_fallback: bool = False,
) -> GameSummary | None:
    """The summary a digest due at `due_at` should use for `game_key`, making it if nobody has.

    `after` is where the asking server's coverage of this game left off (`None`
    for a first digest); with `due_at` it decides what counts as reusable, and
    where a new summary starts, see `repo.get_game_summary`. If nothing is, this
    claims the game (one caller wins, the rest wait and then reuse the result),
    optionally searches the web
    (`search`, which replaces `coverage` with what the search reports),
    summarizes, saves, and releases. `None` means there was nothing to be had:
    an unknown game, a claim that never resolved inside its lease, or a game
    whose saves have been failing and is resting.

    `retry_fallback` (a confirmed run-now) remakes a `fallback` summary once for
    this call. A caller that had to wait for somebody else's claim doesn't
    retry on top of it: they just got an answer.

    If the claim is taken over while the model is thinking (only possible if the
    heartbeat dies), the slow owner's result is thrown away, not saved beside
    the new owner's; it goes back to looking for the answer that won, at most
    `_MAX_LOST_CLAIMS` times, and then returns `None`.
    """
    game = next((g for g in deps.cfg.catalog if g.key == game_key), None)
    if game is None:
        return None
    token = uuid.uuid4().hex
    retry = retry_fallback
    polls = 0
    lost = 0
    while True:
        state, row, stored = await asyncio.to_thread(
            _claim_sync, deps.db_path, game_key, due_at, after, token, deps.now, retry
        )
        if state == "reusable":
            return stored
        if state == "busy":
            retry = False
            if polls >= _MAX_POLLS:
                logger.warning("gave up waiting for %s's summary claim", game_key)
                return None
            polls += 1
            await deps.sleep(_POLL_S)
            continue
        if _backed_off(deps, game_key):
            try:
                await asyncio.to_thread(_release_sync, deps.db_path, game_key, token)
            except Exception:
                logger.exception("couldn't release the summary claim for %s", game_key)
            return None
        saved = await _make(deps, game, run_date, coverage, search, row, token, after)
        if saved is None:
            retry = False
            lost += 1
            if lost >= _MAX_LOST_CLAIMS:
                # Somebody keeps taking the claim over (or the heartbeat keeps dying), and each
                # lap costs a model call. Stop paying; the digest posts headlines instead.
                logger.warning("lost the summary claim for %s %d times; giving up", game_key, lost)
                return None
            continue
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

    async def lookup(
        game_key: str, due_at: datetime, after: Coverage | None = None
    ) -> GameSummary | None:
        return await ensure_summary(
            deps,
            game_key,
            due_at,
            run_date=due_at.astimezone(UTC).date(),
            after=after,
            coverage=[_WEB_SEARCH_SKIPPED] if deps.cfg.web_search is not None else [],
        )

    return lookup


def retry_lookup(deps: SummaryDeps) -> SummaryLookup:
    """The run-now lookup: like `summary_lookup`, but a `fallback` gets one more try."""

    async def lookup(
        game_key: str, due_at: datetime, after: Coverage | None = None
    ) -> GameSummary | None:
        return await ensure_summary(
            deps,
            game_key,
            due_at,
            run_date=due_at.astimezone(UTC).date(),
            after=after,
            coverage=[_WEB_SEARCH_SKIPPED] if deps.cfg.web_search is not None else [],
            retry_fallback=True,
        )

    return lookup


async def _newest_item_id(db_path: str) -> int:
    """The newest stored item id (0 if none): what a cached preview summary has to still match."""

    def _sync() -> int:
        with closing(connect(db_path)) as conn:
            return repo.latest_item_id(conn)

    return await asyncio.to_thread(_sync)


def dry_run_lookup(
    deps: SummaryDeps, *, cache_ttl: timedelta = PREVIEW_SUMMARY_TTL
) -> SummaryLookup:
    """The CLI's `--dry-run` lookup: a reusable stored summary, else one made in memory.

    Never saves anything. A dry run is a rehearsal, and a rehearsal that
    quietly writes a summary row for the real digest to reuse is a rehearsal
    with side effects, which is the one thing it's not allowed to have. If
    nothing stored qualifies this does call the model (the stub, offline), and
    the result lives as long as the preview does. The one exception is this lookup's own memory:
    a summary it made is kept for `cache_ttl` (in this process only, keyed by game, coverage and
    the newest stored item id) so a second preview inside the window costs nothing, as long as
    nothing new has arrived; an item stored since is a different summary and a fresh call. A
    `fallback` isn't kept; a model that just failed deserves a fresh try next time.
    """
    made_lately: dict[tuple[str, Coverage | None, int], tuple[datetime, GameSummary]] = {}

    async def lookup(
        game_key: str, due_at: datetime, after: Coverage | None = None
    ) -> GameSummary | None:
        stored = await _stored_summary(deps.db_path, game_key, due_at, after)
        if stored is not None:
            return stored
        now = deps.now()
        key = (game_key, after, await _newest_item_id(deps.db_path))
        kept = made_lately.get(key)
        if kept is not None and now - kept[0] < cache_ttl:
            return kept[1]
        for stale in [k for k, (at, _) in made_lately.items() if now - at >= cache_ttl]:
            del made_lately[stale]
        game = next((g for g in deps.cfg.catalog if g.key == game_key), None)
        if game is None:
            return None
        made = await _summarize(deps, game, None, after)
        summary = made.summary
        result = GameSummary(
            "fallback" if summary.fallback else "ok",
            list(summary.stories),
            [],
            summary.note,
            made.items_upto,
        )
        if not summary.fallback:
            made_lately[key] = (now, result)
        return result

    return lookup


# --- The prepare job ---


def _needs_summary_sync(
    db_path: str, guild_id: int, game_key: str, due_at: datetime, day: date
) -> tuple[bool, Coverage | None]:
    """Whether this server's digest has no summary it may reuse, and where its coverage left off."""
    with closing(connect(db_path)) as conn:
        coverage = repo.last_coverage(conn, guild_id, exclude_run_date=day, games=[game_key])
        after = coverage.for_game(game_key) if coverage else None
        return repo.get_game_summary(conn, game_key, due_at, after) is None, after


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

    Meant to run every five minutes. A game needs one when some comped server
    that follows it has a digest due within `LEAD` and no stored summary that
    server may reuse (it starts where that server's coverage ended, and is at
    most six hours older than this digest; a fallback counts, it isn't retried
    today). The game is then searched and summarized for that server's window,
    the search inside the same claim as the summary so overlapping jobs search
    once. The new summary also serves every other server whose coverage ended
    in the same place, which is what keeps this to about one call per distinct
    comped cycle per game. A server whose window starts elsewhere gets its own
    summary on a later pass (one per game per run, so one pass never makes two).
    """
    now = now or deps.now()
    follows = await asyncio.to_thread(_follows_sync, deps.db_path)
    dues = upcoming_dues(follows, now)
    todo: list[tuple[GameCfg, datetime, date, Coverage | None]] = []
    for game in deps.cfg.catalog:
        mine = sorted((d for d in dues if d[0].game_key == game.key), key=lambda d: d[1])
        for follow, due, run_date in mine:
            if now < due - LEAD:
                continue
            needs, after = await asyncio.to_thread(
                _needs_summary_sync, deps.db_path, follow.guild_id, game.key, due, run_date
            )
            if needs:
                todo.append((game, due, run_date, after))
                break
    done: list[str] = []
    for game, due, run_date, after in todo:
        try:
            got = await ensure_summary(
                deps, game.key, due, run_date=run_date, after=after, search=True
            )
            if got is not None:
                done.append(game.key)
        except Exception:
            # One game's bad day doesn't cancel the others'.
            logger.exception("preparing the summary for %s failed", game.key)
    return done


__all__ = [
    "LEAD",
    "SAVE_BACKOFF",
    "SAVE_BACKOFF_MAX",
    "SUMMARIZE_TIMEOUT_S",
    "SummaryDeps",
    "dry_run_lookup",
    "ensure_summary",
    "prepare_summaries",
    "retry_lookup",
    "summary_lookup",
    "upcoming_dues",
]
