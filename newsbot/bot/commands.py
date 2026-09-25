"""The slash commands: `/news recent`, `/news search`, and the `/newsbot` admin group.

Both groups are built by factory functions (`make_news_group`,
`make_admin_group`) rather than declared as module-level classes, because
neither one can exist before the config has loaded: the game choices on
`/news recent` come from `cfg.topics`, and the admin group's
`default_permissions` comes from `cfg.admin_permission`. `setup_hook`
calls both factories once, after the config is in hand, and hands the
result to the command tree.

Every handler that touches the database or the pipeline calls
`interaction.response.defer()` (or sends its first response) inside the
Discord-mandated 3 seconds, then does the slow part after. And every admin
handler re-checks the caller's permissions itself (SPEC section 9): Discord
hides `/newsbot` from non-admins in its UI via `default_permissions`, but
UI hiding isn't authorization, and a stale client or a forged interaction
doesn't get to find that out the hard way.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.app_commands import Choice

from newsbot.bot.client import DiscordPublisher, NewsBot, NullPublisher
from newsbot.bot.format import render_status, render_story_page
from newsbot.bot.views import ConfirmView, PagerView
from newsbot.config import AppConfig, Topic, configured_source_names
from newsbot.pipeline.run import RunMode, is_run_in_progress, local_run_date, run_daily
from newsbot.pipeline.summarize import PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK
from newsbot.shift.match import is_code
from newsbot.shift.sweep import CodeCheckOutcome, run_test_alert
from newsbot.store.db import connect
from newsbot.store.models import AlertStatus, DigestRow, StatusSnapshot, StoryView
from newsbot.store.repo import (
    alert_status,
    get_digest,
    query_stories,
    search_stories,
    status_snapshot,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 6
_GAME_ALL = "all"
_LABEL_CHOICES = ("official", "reported", "rumor")
# run-now, preview, and test-alert all block on the same _run_lock (a
# sweep holds it too, briefly) -- one shared wording so "busy" reads the
# same regardless of which of the three tripped over it.
_BUSY_MESSAGE = "A run or code check is in progress."
_NOT_A_CODE_MESSAGE = (
    "That doesn't look like a SHiFT code -- expected five groups of five "
    "letters or digits joined by hyphens (XXXXX-XXXXX-XXXXX-XXXXX-XXXXX)."
)


# --- Pure decision functions: the part of this module worth unit testing ---


def resolve_query_args(
    topics: list[Topic], game: str, days: int, label: str | None, now: datetime
) -> tuple[list[str], datetime, str | None]:
    """Turn `/news recent`'s raw option values into `repo.query_stories` arguments.

    `game` is a topic key, or the literal "all" that the "All" choice
    carries as its value -- which maps to an empty `topic_keys` list,
    since `query_stories` already treats "no topics given" as "every
    topic". `topics` isn't actually consulted here; it's in the signature
    so a caller can't hand this a game value that skipped validation
    without at least having the topic list in front of them to check
    against, which every actual caller (the choice-constrained slash
    command option) already guarantees anyway.
    """
    topic_keys = [] if game == _GAME_ALL else [game]
    since = now - timedelta(days=days)
    return topic_keys, since, label


def needs_confirmation(existing: DigestRow | None) -> bool:
    """True if `/newsbot run-now` should ask before running again.

    `ok`/`partial` (today already posted) always needs confirming, and so
    does a `failed` row with a non-empty `posted_message_ids` -- that
    combination means a publish attempt got the header (and maybe some
    embeds) into the channel before it died, so a plain re-run would post
    a second header on top of the one already there. A `failed` row with
    nothing posted is a clean failure (never reached Discord at all), so
    it doesn't need asking.

    `pending` also needs confirming (QA step 20, group 4): it means either
    something else is mid-run right now, or -- far more likely, since
    `is_run_in_progress()` already caught the former before this function
    is even consulted -- a previous run crashed between claiming the day
    and saving or failing it, and nobody knows whether it posted. `run-now`
    passes `force=True` through to `claim_digest` after the admin confirms,
    which (as of the same change) is what actually lets the reclaim
    succeed instead of silently no-op'ing into "skipped". No row at all
    obviously doesn't need asking either.
    """
    if existing is None:
        return False
    if existing.status in ("ok", "partial", "pending"):
        return True
    return existing.status == "failed" and bool(existing.posted_message_ids)


def has_admin_permission(permissions: discord.Permissions, admin_permission: str) -> bool:
    """The server-side half of the admin check.

    Discord's `default_permissions` on the command group only controls
    what the client *shows*; it isn't enforced against every possible way
    an interaction can reach the bot. This is what actually gates the
    handler, checked against the permissions Discord attaches to the
    interaction itself rather than anything we could accidentally trust
    from the client.
    """
    # Discord usually resolves Administrator into every bit before the
    # interaction reaches us, but "usually" is carrying a lot of weight in
    # that sentence. Locking the server owner out of their own bot's admin
    # commands would be a memorable bug; checking one extra flag is cheap.
    return bool(
        getattr(permissions, "administrator", False)
        or getattr(permissions, admin_permission, False)
    )


def estimate_spend_usd(input_tokens: int, output_tokens: int) -> float:
    """Rough running Claude spend from token counts, at Haiku 4.5 list pricing.

    "Rough" is doing some work in that sentence: this is list price times
    tokens, not an invoice. Good enough to notice "why is this $40" long
    before the actual bill would tell you.
    """
    return (
        input_tokens * PRICE_IN_PER_MTOK / 1_000_000
        + output_tokens * PRICE_OUT_PER_MTOK / 1_000_000
    )


def _page_count(total: int) -> int:
    return max((total + _PAGE_SIZE - 1) // _PAGE_SIZE, 1)


def summarize_test_alert(outcome: CodeCheckOutcome) -> str:
    """Turn one `run_test_alert` outcome into `/newsbot test-alert`'s ephemeral reply.

    `run_test_alert` always treats its one synthetic code as seeded and
    fresh, so the only ways it can land here are: it posted (with or
    without a ping -- the cap decided that), it failed to actually send,
    or the code was already in `alerted_codes` from an earlier test (or a
    real alert) and `plan_alerts` dropped it before it ever reached
    `to_post`. `outcome.failed` is checked first: a code that both failed
    to send *and* somehow looks otherwise silent shouldn't read as
    "already alerted" just because nothing posted.
    """
    if outcome.failed:
        return "post failed; check the bot's log"
    if outcome.posted and outcome.ping:
        return "posted with ping"
    if outcome.posted and not outcome.ping:
        return "posted without ping (cap)"
    return "already alerted, nothing posted"


def invalid_test_alert_code_message(code: str) -> str | None:
    """None if `code` is a real SHiFT code shape; otherwise what to tell the caller.

    A plain function, not inlined into the handler, so a fullwidth or
    otherwise malformed code -- the slash command option's
    `Range[str, 29, 29]` only bounds length, not shape, so plenty of
    29-character garbage still reaches here -- can be exercised without a
    fake `discord.Interaction`.
    """
    if is_code(code):
        return None
    return _NOT_A_CODE_MESSAGE


# --- Synchronous DB calls, run through asyncio.to_thread by the handlers below ---


def _query_stories_sync(
    db_path: str,
    topic_keys: list[str],
    since: datetime,
    label: str | None,
    limit: int,
    offset: int,
) -> tuple[list[StoryView], int]:
    with closing(connect(db_path)) as conn:
        return query_stories(conn, topic_keys, since, label, limit, offset)


def _search_stories_sync(
    db_path: str, query: str, since: datetime, limit: int, offset: int
) -> tuple[list[StoryView], int]:
    with closing(connect(db_path)) as conn:
        return search_stories(conn, query, since, limit, offset)


def _get_digest_sync(db_path: str, run_date) -> DigestRow | None:
    with closing(connect(db_path)) as conn:
        return get_digest(conn, run_date)


def _status_snapshot_sync(
    db_path: str, now: datetime, month_start: datetime, configured_names: set[str]
) -> StatusSnapshot:
    with closing(connect(db_path)) as conn:
        return status_snapshot(conn, now, month_start, configured_names)


def _alert_status_sync(
    db_path: str, today: str, *, enabled: bool, max_pings: int, test_command_enabled: bool
) -> AlertStatus:
    with closing(connect(db_path)) as conn:
        return alert_status(
            conn,
            today,
            enabled=enabled,
            max_pings=max_pings,
            test_command_enabled=test_command_enabled,
        )


# --- Paging glue shared by /news recent and /news search ---

_Fetch = Callable[[int, int], tuple[list[StoryView], int]]


async def _first_page_and_view(
    owner_id: int,
    topics_by_key: dict[str, Topic],
    title: str,
    fetch: _Fetch,
) -> tuple[discord.Embed, PagerView]:
    """Run `fetch` for page 1, then build the embed and a `PagerView` bound to it.

    `fetch(limit, offset)` is a plain sync callable (already bound to a
    db_path and the rest of a query's fixed arguments via
    `functools.partial`); every call, including this first one, goes
    through `asyncio.to_thread` so a slow query never blocks the gateway.
    """
    stories, total = await asyncio.to_thread(fetch, _PAGE_SIZE, 0)
    pages = _page_count(total)
    embed = render_story_page(stories, topics_by_key, title, 1, pages)

    async def render_page(page: int) -> discord.Embed:
        page_stories, _total = await asyncio.to_thread(fetch, _PAGE_SIZE, (page - 1) * _PAGE_SIZE)
        return render_story_page(page_stories, topics_by_key, title, page, pages)

    view = PagerView(owner_id, render_page=render_page, total_pages=pages)
    return embed, view


# --- /news ---


def make_news_group(cfg: AppConfig, db_path: str) -> app_commands.Group:
    """Build the `/news recent` and `/news search` commands.

    A parent command can't be both runnable on its own and have
    subcommands (SPEC-DEV 10), so `/news` itself does nothing; `recent`
    and `search` are the two real commands underneath it.
    """
    topics_by_key = {t.key: t for t in cfg.topics}
    game_choices = [Choice(name=t.name, value=t.key) for t in cfg.topics]
    game_choices.append(Choice(name="All", value=_GAME_ALL))
    label_choices = [Choice(name=label, value=label) for label in _LABEL_CHOICES]

    group = app_commands.Group(
        name="news", description="Recent news and search for the tracked games."
    )

    @group.command(name="recent", description="Recent stories for a game.")
    @app_commands.describe(
        game="Which game (or All)",
        days="How many days back to look (1-30)",
        label="Only show stories with this label",
        public="Show the result to the whole channel instead of just you",
    )
    @app_commands.choices(game=game_choices, label=label_choices)
    async def recent(
        interaction: discord.Interaction,
        game: Choice[str],
        days: app_commands.Range[int, 1, 30] = 7,
        label: Choice[str] | None = None,
        public: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not public)
        topic_keys, since, label_value = resolve_query_args(
            cfg.topics, game.value, days, label.value if label else None, datetime.now(UTC)
        )
        fetch = functools.partial(_query_stories_sync, db_path, topic_keys, since, label_value)
        embed, view = await _first_page_and_view(
            interaction.user.id, topics_by_key, f"/news recent — {game.name}", fetch
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=not public)

    @group.command(name="search", description="Search story headlines and summaries.")
    @app_commands.describe(
        query="What to search for",
        days="How many days back to search (1-30)",
        public="Show the result to the whole channel instead of just you",
    )
    async def search(
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 100],
        days: app_commands.Range[int, 1, 30] = 30,
        public: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not public)
        since = datetime.now(UTC) - timedelta(days=days)
        fetch = functools.partial(_search_stories_sync, db_path, query, since)
        embed, view = await _first_page_and_view(
            interaction.user.id, topics_by_key, f"/news search — {query}", fetch
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=not public)

    return group


# --- /newsbot ---


def _admin_denial_message() -> str:
    return "You don't have permission to run this."


async def _check_admin(interaction: discord.Interaction, admin_permission: str) -> bool:
    """Deny and log if `interaction.user` lacks `admin_permission`; return whether to proceed."""
    if has_admin_permission(interaction.permissions, admin_permission):
        return True
    logger.warning(
        "admin command denied",
        extra={
            "user_id": interaction.user.id,
            "command": interaction.command.qualified_name if interaction.command else None,
        },
    )
    await interaction.response.send_message(_admin_denial_message(), ephemeral=True)
    return False


def make_admin_group(cfg: AppConfig, bot: NewsBot) -> app_commands.Group:
    """Build the `/newsbot status | run-now | preview` group.

    `default_permissions` only drives what Discord's own client shows;
    every handler below re-checks with `_check_admin` regardless (SPEC
    section 9). `bot` gives the handlers `build_deps()` for a fresh
    `Deps`, and is itself the `discord.Client` `DiscordPublisher` sends
    through.
    """
    permissions = discord.Permissions(**{cfg.admin_permission: True})
    group = app_commands.Group(
        name="newsbot",
        description="Admin: status, manual runs and previews.",
        default_permissions=permissions,
    )

    @group.command(name="status", description="Last digest, source health, and estimated spend.")
    async def status(interaction: discord.Interaction) -> None:
        if not await _check_admin(interaction, cfg.admin_permission):
            return
        await interaction.response.defer(ephemeral=True)
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        snap = await asyncio.to_thread(
            _status_snapshot_sync, bot.db_path, now, month_start, configured_source_names(cfg)
        )
        spend = estimate_spend_usd(snap.month_input_tokens, snap.month_output_tokens)
        # Computed regardless of cfg.alerts.enabled, same as every other
        # field here -- an admin reading /newsbot status on a bot where
        # alerts are off should see "disabled", not have the whole field
        # vanish and wonder whether the feature exists at all.
        today = local_run_date(now, cfg.digest.timezone).isoformat()
        alerts_status = await asyncio.to_thread(
            _alert_status_sync,
            bot.db_path,
            today,
            enabled=cfg.alerts.enabled,
            max_pings=cfg.alerts.max_pings_per_day,
            test_command_enabled=cfg.alerts.allow_test_command,
        )
        await interaction.followup.send(
            embed=render_status(snap, spend, alerts_status), ephemeral=True
        )

    @group.command(name="run-now", description="Run the pipeline and post the digest now.")
    async def run_now(interaction: discord.Interaction) -> None:
        if not await _check_admin(interaction, cfg.admin_permission):
            return
        if is_run_in_progress():
            await interaction.response.send_message(_BUSY_MESSAGE, ephemeral=True)
            return

        run_date = local_run_date(datetime.now(UTC), cfg.digest.timezone)
        existing = await asyncio.to_thread(_get_digest_sync, bot.db_path, run_date)

        force = False
        if needs_confirmation(existing):
            view = ConfirmView(interaction.user.id)
            # "pending" and "failed-with-something-posted" are both the
            # same underlying situation from an admin's point of view: a
            # run died partway through and nobody knows for sure what
            # made it to the channel. "ok"/"partial" is the unambiguous
            # case -- it definitely posted, this would just post again.
            if existing is not None and existing.status in ("pending", "failed"):
                prompt = "A previous run may have crashed mid-post; check the channel. Post anyway?"
            else:
                prompt = "Today's digest already posted. Post again?"
            await interaction.response.send_message(prompt, view=view, ephemeral=True)
            await view.wait()
            if not view.value:
                await interaction.edit_original_response(content="Cancelled.", view=None)
                return
            force = True
            await interaction.edit_original_response(content="Running...", view=None)
        else:
            await interaction.response.defer(ephemeral=True)

        deps = bot.build_deps()
        publisher = DiscordPublisher(bot, cfg.digest.channel_id, run_date)
        outcome = await run_daily(deps, publisher, mode=RunMode.POST, force=force)
        await interaction.followup.send(f"Run finished: {outcome.status}", ephemeral=True)

    @group.command(name="preview", description="Run the pipeline; show the digest only to you.")
    async def preview(interaction: discord.Interaction) -> None:
        if not await _check_admin(interaction, cfg.admin_permission):
            return
        if is_run_in_progress():
            # Same reasoning as run-now: run_daily would just block on
            # _run_lock until whatever's running finishes, which reads to
            # the admin as a hung command for however long that takes.
            # A quick "try again in a bit" beats a silent multi-minute wait.
            await interaction.response.send_message(_BUSY_MESSAGE, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deps = bot.build_deps()
        outcome = await run_daily(deps, NullPublisher(), mode=RunMode.PREVIEW)
        if outcome.rendered is None:
            await interaction.followup.send(
                f"Preview failed: {'; '.join(outcome.notes)}", ephemeral=True
            )
            return
        await interaction.followup.send(outcome.rendered.header, ephemeral=True)
        for message in outcome.rendered.embed_messages:
            await interaction.followup.send(embeds=message, ephemeral=True)

    if cfg.alerts.allow_test_command:
        # Registered only when the config opts in (config.py already logs
        # a startup warning when it does) -- a prod config should never
        # carry a command that lets any admin post a fake @everyone-ping
        # on demand, and the plan's owner checklist relies on this command
        # disappearing the moment allow_test_command flips back to false.
        @group.command(
            name="test-alert",
            description="Post a fake SHiFT code alert, to test the pipeline end to end (dev only).",
        )
        @app_commands.describe(
            code="A fake code, shaped like XXXXX-XXXXX-XXXXX-XXXXX-XXXXX",
            golden="Treat it as a Golden Key code",
        )
        async def test_alert(
            interaction: discord.Interaction,
            code: app_commands.Range[str, 29, 29],
            golden: bool = False,
        ) -> None:
            if not await _check_admin(interaction, cfg.admin_permission):
                return
            rejection = invalid_test_alert_code_message(code)
            if rejection is not None:
                await interaction.response.send_message(rejection, ephemeral=True)
                return
            if is_run_in_progress():
                await interaction.response.send_message(_BUSY_MESSAGE, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            outcome = await run_test_alert(bot.build_sweep_deps(), code, golden)
            if outcome is None:
                # Lost the race between the pre-check above and actually
                # acquiring the run lock (step 7) -- something else (a
                # sweep, another test-alert) grabbed it first.
                await interaction.followup.send(_BUSY_MESSAGE, ephemeral=True)
                return
            await interaction.followup.send(summarize_test_alert(outcome), ephemeral=True)

    return group


__all__ = [
    "estimate_spend_usd",
    "has_admin_permission",
    "invalid_test_alert_code_message",
    "make_admin_group",
    "make_news_group",
    "needs_confirmation",
    "resolve_query_args",
    "summarize_test_alert",
]
