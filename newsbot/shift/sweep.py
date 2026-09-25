"""Turn a batch of collected items into recorded and (maybe) posted SHiFT code alerts.

`shift/decide.py` is the pure planner; this module is where a plan
actually touches a database or sends a Discord message. Two entry points
call into the same core (`process_items`): the hourly sweep
(`run_code_sweep`, which also runs its own collectors under the shared
run lock) and the daily digest run's own post-publish check
(`newsbot.pipeline.run._maybe_check_codes`, which already has its items
and doesn't need the lock -- it's already holding it).

Record-then-post (plan §1) is the ordering this whole module protects:
`claim_codes` reserves every code in a batch as `pending`, ping budget
spent, in one transaction *before* a single message goes out. A crash
between claiming and a message actually landing can lose an alert (the
codes sit `pending` until `fail_pending_codes` cleans them up at the next
startup) but can never double-spend a ping or post the same batch twice.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from newsbot.bot.format import RenderedAlert, render_code_alerts
from newsbot.collectors.base import (
    Collector,
    CollectorResult,
    RateLimitState,
    RawItem,
    run_collectors,
)
from newsbot.config import AppConfig
from newsbot.pipeline.normalize import canonicalize_items
from newsbot.pipeline.publisher import PublishError
from newsbot.pipeline.run import local_run_date
from newsbot.shift.decide import (
    CodeCandidate,
    CodeSighting,
    aggregate,
    pings_used_today,
    plan_alerts,
    seeding_healthy,
    sightings_from_items,
)
from newsbot.store.db import connect
from newsbot.store.models import AlertState
from newsbot.store.repo import (
    claim_codes,
    get_alert_state,
    known_codes,
    mark_codes_failed,
    mark_codes_posted,
    record_silent_codes,
    record_sweep,
)

logger = logging.getLogger(__name__)

_SWEEP_COLLECT_TIMEOUT_S = 20.0
# Same backoff the digest publisher retries against (pipeline/run.py's
# _PUBLISH_BACKOFF_S) -- a code alert message is posted the same way a
# digest embed batch is, so it gets the same "give Discord a few seconds
# to recover from a blip" policy.
_POST_BACKOFF_S = (2.0, 4.0, 8.0)

_TEST_SOURCE_NAME = "test-alert"
_TEST_ITEM_URL = "https://shift.gearboxsoftware.com/rewards"


class CodeAlertPoster(Protocol):
    async def post(self, alert: RenderedAlert) -> int | None:
        """Post one alert message and return its Discord message id (if any).

        Raises `PublishError` for a failure worth retrying (a 5xx, a
        timeout); anything else raised is treated as final -- see
        `_post_with_retry`.
        """
        ...


class PrintCodeAlertPoster:
    """Prints an alert to stdout instead of posting it -- the CLI's `--sweep` poster."""

    async def post(self, alert: RenderedAlert) -> int | None:
        print(alert.content)
        return None


@dataclass
class SweepDeps:
    cfg: AppConfig
    db_path: str
    http: httpx.AsyncClient
    collectors: list[Collector]
    now: Callable[[], datetime]
    alert: Callable[[str], Awaitable[None]]
    poster: CodeAlertPoster
    rate_limit_state: RateLimitState | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class CodeCheckOutcome:
    """What one sweep or one daily code check actually did, for status and logging."""

    new_candidates: int
    posted: int
    silent: int
    failed: int
    ping: bool
    cap_reached: bool


_EMPTY_OUTCOME = CodeCheckOutcome(
    new_candidates=0, posted=0, silent=0, failed=0, ping=False, cap_reached=False
)


async def _read_ping_state(deps: SweepDeps) -> tuple[AlertState, str]:
    def _sync() -> AlertState:
        with closing(connect(deps.db_path)) as conn:
            return get_alert_state(conn)

    state = await asyncio.to_thread(_sync)
    today = local_run_date(deps.now(), deps.cfg.digest.timezone).isoformat()
    return state, today


async def _known_codes_for(deps: SweepDeps, candidates: list[CodeCandidate]) -> set[str]:
    codes = [c.code for c in candidates]

    def _sync() -> set[str]:
        with closing(connect(deps.db_path)) as conn:
            return known_codes(conn, codes)

    return await asyncio.to_thread(_sync)


_PING_PREFIX = "@everyone "


def _strip_ping(alert: RenderedAlert) -> RenderedAlert:
    """A copy of `alert` with the ping turned off -- same nonce, same codes.

    Used by `_post_with_retry` when a ping-bearing send raised
    `PublishError`: our client gave up waiting for a response, but that's
    not proof Discord never got the message (the deterministic `nonce`
    handles that half). What it *doesn't* rule out is that Discord got it
    and the ping already went out -- so a retry must never risk a second
    live `@everyone` for the same batch. The content's "@everyone " prefix
    is stripped the same deterministic way `render_code_alerts` added it,
    rather than re-rendering from the candidates (which `_post_with_retry`
    doesn't have -- only the already-rendered `RenderedAlert` does).
    """
    content = alert.content
    if content.startswith(_PING_PREFIX):
        content = content[len(_PING_PREFIX) :]
    return dataclasses.replace(alert, content=content, ping=False)


async def _post_with_retry(
    deps: SweepDeps, alert: RenderedAlert
) -> tuple[int | None, Exception | None]:
    """Post one message, retrying only on `PublishError`, up to `_POST_BACKOFF_S`'s length.

    Any other exception is treated as final without a retry -- a poster
    bug or an auth failure isn't going to fix itself by waiting eight
    seconds. `asyncio.CancelledError` (a `BaseException`, not an
    `Exception`) is deliberately not caught here at all: a sweep cancelled
    mid-post should propagate, leaving its already-claimed codes `pending`
    for `fail_pending_codes` to find at the next startup, not get silently
    marked `failed` by a handler that was never meant to catch it.

    Every attempt after the first reuses `alert.nonce` (set once by
    `render_code_alerts` and never changed here) so Discord can dedupe a
    retry against a first attempt that actually landed but whose response
    we never saw. That covers a retried post landing twice in the
    channel; it says nothing about a retried *ping*, which Discord's
    nonce dedup has no opinion on -- a duplicate message with the ping
    stripped is still a duplicate message, but a duplicate `@everyone` is
    the one failure mode worth refusing to risk even once. So a
    ping-bearing alert that fails once retries with the ping already
    turned off (`_strip_ping`): the first attempt is the only one that
    ever could have pinged, whether or not it actually landed.
    """
    current = alert
    last_error: Exception | None = None
    for attempt in range(len(_POST_BACKOFF_S) + 1):
        try:
            message_id = await deps.poster.post(current)
            return message_id, None
        except PublishError as exc:
            last_error = exc
            if current.ping:
                current = _strip_ping(current)
            if attempt < len(_POST_BACKOFF_S):
                await deps.sleep(_POST_BACKOFF_S[attempt])
        except Exception as exc:  # non-PublishError: final immediately, no retry
            return None, exc
    return None, last_error


async def _apply_plan(
    deps: SweepDeps,
    candidates: list[CodeCandidate],
    *,
    seeded: bool,
    seeding_ok: bool,
    today: str,
    test: bool,
) -> CodeCheckOutcome:
    known = await _known_codes_for(deps, candidates)
    state, _ = await _read_ping_state(deps)
    pings_today = pings_used_today(state, today)

    plan = plan_alerts(
        candidates,
        known=known,
        seeded=seeded,
        seeding_ok=seeding_ok,
        pings_today=pings_today,
        max_pings=deps.cfg.alerts.max_pings_per_day,
    )

    def _record_silent_sync() -> None:
        rows = [(c.code, c.source_name, c.item_url, status) for c, status in plan.silent]
        with closing(connect(deps.db_path)) as conn:
            record_silent_codes(conn, rows, now=deps.now, mark_seeded=plan.mark_seeded)

    if plan.silent or plan.mark_seeded:
        await asyncio.to_thread(_record_silent_sync)

    posted = 0
    failed = 0
    cap_reached = plan.cap_reached
    final_ping = plan.ping
    if plan.to_post:
        # claim_codes re-checks the ping cap itself, inside its own
        # BEGIN IMMEDIATE transaction, against whatever `pings_today`
        # looks like *right now* -- not the copy `plan_alerts` computed
        # a moment ago from a plain read (plan step 7). A sweep and a
        # concurrent `/newsbot test-alert` can both reach this point
        # having each seen "budget available"; only one of them actually
        # gets to spend it, and `actual_ping` is that outcome, which is
        # what actually gets rendered and posted -- not `plan.ping`.
        def _claim_sync() -> bool:
            rows = [(c.code, c.source_name, c.item_url) for c in plan.to_post]
            with closing(connect(deps.db_path)) as conn:
                return claim_codes(
                    conn,
                    rows,
                    pinged=plan.ping,
                    local_day=today,
                    now=deps.now,
                    max_pings=deps.cfg.alerts.max_pings_per_day,
                )

        final_ping = await asyncio.to_thread(_claim_sync)
        if plan.ping and not final_ping:
            cap_reached = True
        rendered = render_code_alerts(plan.to_post, ping=final_ping, test=test)

        begin_batch = getattr(deps.poster, "begin_batch", None)
        if begin_batch is not None:
            begin_batch()

        failed_from_here = False
        failed_codes: list[str] = []
        for alert in rendered:
            if failed_from_here:
                failed += len(alert.codes)
                failed_codes.extend(alert.codes)
                await asyncio.to_thread(_mark_failed_sync, deps.db_path, list(alert.codes))
                continue

            message_id, error = await _post_with_retry(deps, alert)
            if error is not None:
                failed_from_here = True
                failed += len(alert.codes)
                failed_codes.extend(alert.codes)
                await asyncio.to_thread(_mark_failed_sync, deps.db_path, list(alert.codes))
                logger.error(
                    "code alert post failed permanently",
                    extra={"codes": alert.codes, "error": str(error)},
                )
                continue

            posted += len(alert.codes)
            await asyncio.to_thread(_mark_posted_sync, deps.db_path, list(alert.codes), message_id)

        if failed_codes:
            await deps.alert(
                "newsbot: SHiFT code alert post failed; codes never posted: "
                + ", ".join(failed_codes)
            )

    if cap_reached and deps.cfg.alerts.max_pings_per_day > 0:
        # Suppressed at max_pings_per_day == 0 (step 8): with the cap set
        # to zero, *every* batch with something fresh to post trivially
        # "reaches" it -- that's the config working as intended (pinging
        # is turned off on purpose), not an admin-worthy event, and
        # alerting on it every single sweep would just be noise trained
        # to be ignored.
        await deps.alert("newsbot: SHiFT code alert daily ping cap reached; posted without a ping")

    return CodeCheckOutcome(
        new_candidates=len(candidates),
        posted=posted,
        silent=len(plan.silent),
        failed=failed,
        ping=final_ping,
        cap_reached=cap_reached,
    )


def _mark_failed_sync(db_path: str, codes: list[str]) -> None:
    with closing(connect(db_path)) as conn:
        mark_codes_failed(conn, codes)


def _mark_posted_sync(db_path: str, codes: list[str], message_id: int | None) -> None:
    with closing(connect(db_path)) as conn:
        mark_codes_posted(conn, codes, message_id=message_id)


async def process_items(
    deps: SweepDeps, items: list[RawItem], *, seeding_ok: bool
) -> CodeCheckOutcome:
    """The lock-free core: canonicalize -> sightings -> aggregate -> plan -> record/post.

    Neither the hourly sweep's own lock handling nor the daily run's
    already-held lock live here -- this function only needs a batch of
    items and somewhere to record what it decides. `seeding_ok` is the
    caller's call on whether *this* batch is healthy enough to flip the
    seeded marker on if it isn't already (A1); both the hourly sweep and
    the daily run's own code check compute this the same way
    (`decide.seeding_healthy(results)`), rather than the daily run always
    assuming it's healthy.
    """
    canonical_items = canonicalize_items(items)
    sightings = sightings_from_items(
        canonical_items,
        topics=deps.cfg.topics,
        alert_topics=deps.cfg.alerts.topics,
        max_codes_per_item=deps.cfg.alerts.max_codes_per_item,
    )
    if not sightings:
        if seeding_ok:
            # A healthy sweep that happened to find zero codes anywhere
            # is still the first healthy sweep -- it has to get to flip
            # the seeded marker on (A1) the same as one that found
            # plenty, or the *next* sweep to find a real code treats an
            # already-seeded feed as brand new and silently seeds it
            # instead of posting. `record_silent_codes([], mark_seeded=True)`
            # is exactly "set the marker if it isn't already set", with
            # nothing to record alongside it.
            def _mark_seeded_sync() -> None:
                with closing(connect(deps.db_path)) as conn:
                    record_silent_codes(conn, [], now=deps.now, mark_seeded=True)

            await asyncio.to_thread(_mark_seeded_sync)
        return _EMPTY_OUTCOME

    max_age = timedelta(hours=deps.cfg.alerts.max_item_age_hours)
    candidates = aggregate(
        sightings, now=deps.now(), max_age=max_age, ping_trust=tuple(deps.cfg.alerts.ping_trust)
    )

    state, today = await _read_ping_state(deps)
    return await _apply_plan(
        deps, candidates, seeded=state.seeded, seeding_ok=seeding_ok, today=today, test=False
    )


def _sweep_summary(results: list[CollectorResult], outcome: CodeCheckOutcome) -> str:
    non_skipped = [r for r in results if r.skipped is None]
    ok = sum(1 for r in non_skipped if r.error is None)
    total = len(non_skipped)
    word = "code" if outcome.posted == 1 else "codes"
    return f"{ok}/{total} sources ok, {outcome.posted} new {word}"


async def run_code_sweep(deps: SweepDeps) -> CodeCheckOutcome | None:
    """Run one sweep: collect (minus web_search, already excluded by `deps.collectors`),
    check for codes, and record that it happened. Skips entirely (returns `None`,
    no writes at all) if the run lock is already held (A10) -- the daily
    job doesn't wait on a sweep and a sweep doesn't wait on it either;
    it just tries again next interval.
    """
    from newsbot.pipeline.lock import run_lock_or_skip

    async with run_lock_or_skip() as acquired:
        if not acquired:
            return None

        results = await run_collectors(
            deps.collectors,
            deps.http,
            timeout_s=_SWEEP_COLLECT_TIMEOUT_S,
            rate_limit_state=deps.rate_limit_state,
        )
        for result in results:
            logger.info(
                "sweep collector finished",
                extra={
                    "source_name": result.source_name,
                    "source_type": result.source_type,
                    "item_count": len(result.items),
                    "error": result.error,
                    "skipped": result.skipped,
                },
            )
        items = [item for result in results for item in result.items]

        outcome = await process_items(deps, items, seeding_ok=seeding_healthy(results))

        summary = _sweep_summary(results, outcome)

        def _record_sweep_sync() -> None:
            with closing(connect(deps.db_path)) as conn:
                record_sweep(conn, deps.now, summary)

        await asyncio.to_thread(_record_sweep_sync)
        return outcome


async def run_test_alert(deps: SweepDeps, code: str, golden: bool) -> CodeCheckOutcome | None:
    """Post (or explain why not) one synthetic code, for `/newsbot test-alert`.

    Treated as already seeded (so it always tries to post rather than
    silently seed) without ever setting the marker itself -- a test alert
    proves the pipeline works, it doesn't get to vouch for every other
    code already sitting in the feeds. Still recorded and still counted
    against the daily ping cap: a test that pinged for free wouldn't
    actually test the cap.

    Wrapped in the same `run_lock_or_skip` an hourly sweep uses (step 7):
    without it, an admin firing `/newsbot test-alert` at the same moment
    an hourly sweep is mid-`claim_codes` could still race it (the command
    handler's own `is_run_in_progress()` pre-check has a window between
    checking and calling this), landing two writers on `alerted_codes` at
    once. `claim_codes`'s own `BEGIN IMMEDIATE` (step 7) would still keep
    that from corrupting anything, but skipping the whole attempt outright
    is simpler than making an admin wait out someone else's sweep. Returns
    `None` (never anything else) when skipped, same as `run_code_sweep`.
    """
    from newsbot.pipeline.lock import run_lock_or_skip

    async with run_lock_or_skip() as acquired:
        if not acquired:
            return None

        sighting = CodeSighting(
            code=code.upper(),
            golden=golden,
            source_name=_TEST_SOURCE_NAME,
            item_url=_TEST_ITEM_URL,
            trust="official",
            published_at=deps.now(),
        )
        max_age = timedelta(hours=deps.cfg.alerts.max_item_age_hours)
        candidates = aggregate(
            [sighting],
            now=deps.now(),
            max_age=max_age,
            ping_trust=tuple(deps.cfg.alerts.ping_trust),
        )
        _, today = await _read_ping_state(deps)
        return await _apply_plan(
            deps, candidates, seeded=True, seeding_ok=False, today=today, test=True
        )


__all__ = [
    "CodeAlertPoster",
    "CodeCheckOutcome",
    "PrintCodeAlertPoster",
    "SweepDeps",
    "process_items",
    "run_code_sweep",
    "run_test_alert",
]
