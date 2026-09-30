"""The slash commands: `/news recent`, `/news search`, and the `/newsbot` admin group.

Both groups are built by factory functions (`make_news_group`,
`make_admin_group`) rather than declared as module-level classes, because
neither one can exist before the config has loaded: the game choices on
`/news recent` come from `cfg.topics`, and the admin group's
`default_permissions` comes from `cfg.admin_permission`. `setup_hook`
calls both factories once, after the config is in hand, and hands the
result to the command tree.

v3 (the public app) builds the same three groups a second time, per server:
`make_member_news_group`, `make_member_shift_group` and `make_guild_admin_group`
(plus `make_lounge_group`), registered by `bot/registration.py`. The v2
factories stay until the cutover, because the running bot still uses them.
The v3 handlers only ever read and write the server that invoked them; the
server id comes from the interaction and from nowhere else.

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
import re
import zoneinfo
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.app_commands import Choice

from newsbot.bot.client import DiscordPublisher, NewsBot, NullPublisher
from newsbot.bot.format import (
    _report_jump_link,
    _truncate_utf16,
    esc,
    render_code_page,
    render_guild_overview,
    render_item_page,
    render_status,
    render_story_page,
)
from newsbot.bot.permissions import check_guild
from newsbot.bot.views import ConfirmView, PagerView
from newsbot.config import AppConfig, GameInfo, Topic, configured_source_names
from newsbot.guilds.schedule import local_due_instant
from newsbot.pipeline.guild_digest import (
    GuildDigestDeps,
    GuildTimeZoneError,
    guild_needs_confirmation,
    is_guild_busy,
    preview_guild_digest,
    run_guild_digest,
    todays_guild_digest,
)
from newsbot.pipeline.run import RunKind, RunMode, is_run_in_progress, local_run_date, run_daily
from newsbot.pipeline.summarize import estimate_spend_usd
from newsbot.shift.match import is_code
from newsbot.shift.sweep import CodeCheckOutcome, run_test_alert
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import (
    AlertStatus,
    CodeView,
    DigestRow,
    GuildGame,
    GuildSettings,
    ItemView,
    StatusSnapshot,
    StoryView,
    Tier,
)
from newsbot.store.repo import (
    alert_status,
    get_digest,
    get_lounge_state,
    query_codes,
    query_stories,
    search_stories,
    status_snapshot,
)
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

_PAGE_SIZE = 6
# design.md §13: /shift codes' own page size: a code's block is smaller
# than a story's (no headline, no summary, just a code and a source line),
# so a page holds a couple more of them before it'd risk the embed's
# 4096-unit cap.
_SHIFT_PAGE_SIZE = 8
_GAME_ALL = "all"
_LABEL_CHOICES = ("official", "reported", "rumor")
# run-now, preview, and test-alert all block on the same _run_lock (a
# sweep holds it too, briefly): one shared wording so "busy" reads the
# same regardless of which of the three tripped over it.
_BUSY_MESSAGE = "A run or code check is in progress."
_NOT_A_CODE_MESSAGE = (
    "That doesn't look like a SHiFT code (expected five groups of five "
    "letters or digits joined by hyphens: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX)."
)


# --- Pure decision functions: the part of this module worth unit testing ---


def resolve_query_args(
    topics: list[Topic], game: str, days: int, label: str | None, now: datetime
) -> tuple[list[str], datetime, str | None]:
    """Turn `/news recent`'s raw option values into `repo.query_stories` arguments.

    `game` is a topic key, or the literal "all" that the "All" choice
    carries as its value: which maps to an empty `topic_keys` list,
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
    does a `failed` row with a non-empty `posted_message_ids`: that
    combination means a publish attempt got the header (and maybe some
    embeds) into the channel before it died, so a plain re-run would post
    a second header on top of the one already there. A `failed` row with
    nothing posted is a clean failure (never reached Discord at all), so
    it doesn't need asking.

    `pending` also needs confirming (QA step 20, group 4): it means either
    something else is mid-run right now, or (far more likely, since
    `is_run_in_progress()` already caught the former before this function
    is even consulted) a previous run crashed between claiming the day
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


def _page_count(total: int, page_size: int) -> int:
    return max((total + page_size - 1) // page_size, 1)


def summarize_test_alert(outcome: CodeCheckOutcome) -> str:
    """Turn one `run_test_alert` outcome into `/newsbot test-alert`'s ephemeral reply.

    `run_test_alert` always treats its one synthetic code as seeded and
    fresh, so the only ways it can land here are: it posted (with or
    without a ping, the cap decided that), it failed to actually send,
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
    otherwise malformed code (the slash command option's
    `Range[str, 29, 29]` only bounds length, not shape, so plenty of
    29-character garbage still reaches here) can be exercised without a
    fake `discord.Interaction`.
    """
    if is_code(code):
        return None
    return _NOT_A_CODE_MESSAGE


def _quote_now_reply(cfg: AppConfig, status: str, message_id: int | None) -> str:
    """Map one `QuoteOutcome` to `/newsbot quote-now`'s ephemeral reply."""
    if status == "posted" and message_id is not None and cfg.lounge.channel_id is not None:
        link = _report_jump_link(cfg.guild_id, cfg.lounge.channel_id, message_id)
        return f"Posted: {link}"
    if status == "already_posted":
        return "Today's quote already posted."
    if status == "post_failed":
        return "Posting failed; the admin channel has the details."
    return "No quote posted; the admin channel has the reason."


def _lounge_quote_reply(guild_id: int, channel_id: int, status: str, message_id: int | None) -> str:
    """Map one `QuoteOutcome` to `/lounge quote-now`'s ephemeral reply (server-keyed)."""
    if status == "posted" and message_id is not None:
        return f"Posted: {_report_jump_link(guild_id, channel_id, message_id)}"
    if status == "no_lounge":
        return "This server doesn't have a lounge quote set up."
    return _quote_now_reply_plain(status)


def _quote_now_reply_plain(status: str) -> str:
    if status == "already_posted":
        return "Today's quote already posted."
    if status == "post_failed":
        return "Posting failed; the admin channel has the details."
    return "No quote posted; the admin channel has the reason."


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


def _quote_posted_today_sync(db_path: str, day: str, guild_id: int | None = None) -> bool:
    """Same rule as `claim_quote`: a stored date at or after `day` counts as posted.

    With `guild_id`, that server's own date (a missing lounge row reads as not posted).
    """
    with closing(connect(db_path)) as conn:
        if guild_id is None:
            last = get_lounge_state(conn).last_quote_date
        else:
            lounge = repo.get_lounge(conn, guild_id)
            last = lounge.last_quote_date if lounge else None
    return last is not None and last >= day


def _status_snapshot_sync(
    db_path: str, now: datetime, month_start: datetime, configured_names: set[str]
) -> StatusSnapshot:
    with closing(connect(db_path)) as conn:
        return status_snapshot(conn, now, month_start, configured_names)


def _query_codes_sync(
    db_path: str, since: datetime, limit: int, offset: int
) -> tuple[list[CodeView], int]:
    with closing(connect(db_path)) as conn:
        return query_codes(conn, since, limit, offset)


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


# --- Paging glue shared by /news recent, /news search, and /shift codes ---

_Fetch = Callable[[int, int], tuple[list, int]]
_Render = Callable[[list, int, int], discord.Embed]


async def _first_page_and_view(
    owner_id: int,
    page_size: int,
    fetch: _Fetch,
    render: _Render,
) -> tuple[discord.Embed, PagerView]:
    """Run `fetch` for page 1, then build the embed and a `PagerView` bound to it.

    `fetch(limit, offset)` is a plain sync callable (already bound to a
    db_path and the rest of a query's fixed arguments via
    `functools.partial`); every call, including this first one, goes
    through `asyncio.to_thread` so a slow query never blocks the gateway.
    `render(items, page, pages)` turns one page's worth of rows into an
    embed: generalized (design.md §13) so `/shift codes` can share this
    with `/news recent`/`/news search` instead of each command re-doing
    the same "query, embed page 1, wire up Prev/Next" dance with its own
    fixed call to `render_story_page`.
    """
    items, total = await asyncio.to_thread(fetch, page_size, 0)
    pages = _page_count(total, page_size)
    embed = render(items, 1, pages)

    async def render_page(page: int) -> discord.Embed:
        page_items, _total = await asyncio.to_thread(fetch, page_size, (page - 1) * page_size)
        return render(page_items, page, pages)

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
        title = f"/news recent — {game.name}"

        def render(stories: list[StoryView], page: int, pages: int) -> discord.Embed:
            return render_story_page(stories, topics_by_key, title, page, pages)

        embed, view = await _first_page_and_view(interaction.user.id, _PAGE_SIZE, fetch, render)
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
        title = f"/news search — {query}"

        def render(stories: list[StoryView], page: int, pages: int) -> discord.Embed:
            return render_story_page(stories, topics_by_key, title, page, pages)

        embed, view = await _first_page_and_view(interaction.user.id, _PAGE_SIZE, fetch, render)
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
        # field here: an admin reading /newsbot status on a bot where
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
            embed=render_status(snap, spend, alerts_status, timezone=cfg.digest.timezone),
            ephemeral=True,
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
            # case: it definitely posted, this would just post again.
            if existing is not None and existing.status in ("pending", "failed"):
                prompt = (
                    "A previous run may have crashed mid-post; check the game channels. "
                    "Post anyway?"
                )
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

        deps = bot.build_deps(RunKind.RUN_NOW)
        publisher = DiscordPublisher(bot)
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
        if not outcome.rendered.messages:
            await interaction.followup.send(
                "Nothing would post today: no game has news.", ephemeral=True
            )
            return
        for message in outcome.rendered.messages:
            await interaction.followup.send(
                content=f"Would post in <#{message.channel_id}>:",
                embed=message.embed,
                ephemeral=True,
            )

    if cfg.lounge.daily_quote.enabled:
        # Registered on the quote feature alone, welcomes or no welcomes:
        # the two lounge features share a channel, not a switch.
        @group.command(
            name="quote-now",
            description="Post today's lounge quote now (the scheduled one then skips today).",
        )
        async def quote_now(interaction: discord.Interaction) -> None:
            if not await _check_admin(interaction, cfg.admin_permission):
                return

            today = local_run_date(datetime.now(UTC), cfg.digest.timezone).isoformat()
            already = await asyncio.to_thread(_quote_posted_today_sync, bot.db_path, today)

            force = False
            if already:
                view = ConfirmView(interaction.user.id)
                await interaction.response.send_message(
                    "Today's quote already posted. Post another one?", view=view, ephemeral=True
                )
                await view.wait()
                if not view.value:
                    await interaction.edit_original_response(content="Cancelled.", view=None)
                    return
                force = True
                await interaction.edit_original_response(content="Posting...", view=None)
            else:
                await interaction.response.defer(ephemeral=True)

            outcome = await bot.run_quote(force)
            await interaction.followup.send(
                _quote_now_reply(cfg, outcome.status, outcome.message_id), ephemeral=True
            )

    if cfg.alerts.allow_test_command:
        # Registered only when the config opts in (config.py already logs
        # a startup warning when it does): a prod config should never
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
                # acquiring the run lock (step 7): something else (a
                # sweep, another test-alert) grabbed it first.
                await interaction.followup.send(_BUSY_MESSAGE, ephemeral=True)
                return
            await interaction.followup.send(summarize_test_alert(outcome), ephemeral=True)

    return group


# --- /shift (design.md §13) ---


def make_shift_group(cfg: AppConfig, db_path: str) -> app_commands.Group:
    """Build `/shift codes`: the member-facing list of known SHiFT codes.

    `setup_hook` only ever calls this when `cfg.alerts.enabled` (D4): a
    list of codes from a feature that's off would just be an empty list
    forever, and a third top-level command group for that isn't worth the
    clutter. This factory doesn't re-check `enabled` itself, same as
    `make_news_group`/`make_admin_group` trust their own callers'
    preconditions.
    """
    group = app_commands.Group(name="shift", description="SHiFT codes the bot has seen.")

    @group.command(name="codes", description="List known SHiFT codes.")
    @app_commands.describe(
        days="How many days back to look (1-90)",
        public="Show the result to the whole channel instead of just you",
    )
    async def codes(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 14,
        public: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not public)
        since = datetime.now(UTC) - timedelta(days=days)
        fetch = functools.partial(_query_codes_sync, db_path, since)
        title = "/shift codes"

        def render(codes: list[CodeView], page: int, pages: int) -> discord.Embed:
            return render_code_page(
                codes, title=title, page=page, pages=pages, timezone=cfg.digest.timezone
            )

        embed, view = await _first_page_and_view(
            interaction.user.id, _SHIFT_PAGE_SIZE, fetch, render
        )
        # Explicit (design.md §13): a member-facing list of codes should
        # never be the send that reintroduces a ping, even though nothing
        # here mentions anyone: belt and suspenders next to NewsBot's
        # own client-level default.
        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=not public,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    return group


# --- v3: the public app's commands (design.md §15, plan task 9) ---
#
# Everything from here down is scoped to the server that ran the command. The
# guild id comes from `interaction.guild_id` and nowhere else; no handler takes a
# server id as an option, so there is no option to tamper with. Replies are
# ephemeral (members' `public` switch aside), carry `AllowedMentions.none()`, and
# put scraped text through `esc()`.

_GUILD_INSTALL = app_commands.AppInstallationType(guild=True, user=False)
_NOT_SET_UP = "This server hasn't set up newsbot yet; an admin can run /newsbot setup."
_NOT_SET_UP_POSTS_NOTHING = (
    "\nThis server isn't set up yet, so nothing posts until an admin runs /newsbot setup."
)
_NO_GAMES = "This server doesn't follow any games yet; an admin can add one with /newsbot follow."
_ZONE_INVALID = "Your time zone setting is invalid; fix it with /newsbot settings."
_SERVER_ONLY = "This only works inside a server."
_DIGEST_BUSY = "A digest for this server is already running; try again in a bit."
_UNWIRED = "That command isn't available yet."
# A page is at most this many rows, whatever a caller (or a future caller) asks for.
_MAX_PAGE_LIMIT = 25
_CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.news)
_DIGEST_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_AUTOCOMPLETE_MAX = 25
_MAX_REPLY = 2000
_PING_CHOICES = ("none", "everyone", "role")


def _none_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


async def _say(interaction: discord.Interaction, content: str, **kwargs) -> None:
    """Send `content` ephemerally (unless told otherwise), mention-proof, cut to Discord's limit.

    Uses the initial response if nothing has answered yet and a followup if
    something has (a defer counts), so handlers don't track which one they're on.
    """
    kwargs.setdefault("ephemeral", True)
    text = _truncate_utf16(content, _MAX_REPLY, suffix="…")
    if interaction.response.is_done():
        await interaction.followup.send(text, allowed_mentions=_none_mentions(), **kwargs)
    else:
        await interaction.response.send_message(text, allowed_mentions=_none_mentions(), **kwargs)


def channel_problem(channel: object, guild_id: int | None) -> str | None:
    """Why `channel` can't be used in server `guild_id`, or None if it can.

    Discord's own option types keep a normal client from offering another server's
    channel or a voice channel, but a forged interaction isn't a normal client, and a
    channel the bot hasn't cached arrives as a thin `AppCommandChannel`. So this
    re-checks on the resolved object, reading the owning guild off either shape, and
    fails closed: a channel it can't tie to `guild_id` is refused. The reply never says
    anything about a foreign channel beyond "not in this server".
    """
    if channel is None or guild_id is None:
        return "Pick a text channel in this server."
    owner = getattr(getattr(channel, "guild", None), "id", None)
    if owner is None:
        owner = getattr(channel, "guild_id", None)
    if owner != guild_id:
        return "That channel isn't in this server."
    if getattr(channel, "type", None) not in _CHANNEL_TYPES:
        return (
            "Pick a regular text channel; threads, voice, forum and other types can't post digests."
        )
    channel_id = getattr(channel, "id", None)
    if not isinstance(channel_id, int) or isinstance(channel_id, bool) or channel_id <= 0:
        return "Pick a text channel in this server."
    return None


def parse_digest_time(text: str) -> str | None:
    """`text` as `HH:MM` (00-23, 00-59), or None. Strict: no `9:00`, no whitespace."""
    return text if _DIGEST_TIME_RE.fullmatch(text) else None


@functools.cache
def _zone_index() -> dict[str, str]:
    """Casefolded zone name to its canonical spelling, for everything tzdata knows."""
    return {name.casefold(): name for name in sorted(zoneinfo.available_timezones())}


def resolve_timezone(text: str) -> str | None:
    """The canonical IANA name for `text` (case-insensitive), or None if it isn't one.

    Checked against the list of real zone names rather than handed to `ZoneInfo`, which
    also accepts file-system-shaped strings it has no business trying; then opened once
    to prove it loads.
    """
    name = _zone_index().get(text.strip().casefold())
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return None
    return name


def _choice(name: str, value: str) -> Choice[str]:
    return Choice(name=name[:100], value=value[:100])


def game_choices(
    catalog: Sequence[GameInfo], keys: Iterable[str], current: str, *, include_all: bool = False
) -> list[Choice[str]]:
    """Autocomplete choices over the catalog games whose key is in `keys`, matching `current`.

    Substring match on name or key, case-insensitive, catalog order, 25 at most
    (Discord's cap). `include_all` adds an "All" entry first when it matches.
    """
    wanted = set(keys)
    needle = current.strip().casefold()
    choices: list[Choice[str]] = []
    if include_all and (not needle or needle in _GAME_ALL):
        choices.append(_choice("All", _GAME_ALL))
    for game in catalog:
        if game.key in wanted and (needle in game.name.casefold() or needle in game.key.casefold()):
            choices.append(_choice(game.name, game.key))
    return choices[:_AUTOCOMPLETE_MAX]


def zone_choices(current: str) -> list[Choice[str]]:
    """Autocomplete choices for `settings timezone:`: zones containing `current`, 25 at most."""
    needle = current.strip().casefold()
    names = [name for folded, name in _zone_index().items() if needle in folded]
    return [_choice(name, name) for name in names[:_AUTOCOMPLETE_MAX]]


def resolve_game(
    catalog: Sequence[GameInfo], allowed_keys: Iterable[str], text: str
) -> GameInfo | None:
    """The catalog game `text` names (by key, or by name ignoring case), if it's in `allowed_keys`.

    The option's value is whatever the client sent, autocomplete or not, so it's
    looked up here and never trusted. `None` for anything unknown or not allowed.
    """
    allowed = set(allowed_keys)
    folded = text.strip().casefold()
    for game in catalog:
        if game.key in allowed and folded in (game.key.casefold(), game.name.casefold()):
            return game
    return None


def _clamp_page(limit: int, offset: int) -> tuple[int, int]:
    return min(max(limit, 1), _MAX_PAGE_LIMIT), max(offset, 0)


# --- Synchronous DB calls for the v3 handlers (run through asyncio.to_thread) ---


def _guild_state_sync(
    db_path: str, guild_id: int, create_tier: Tier | None = None
) -> tuple[GuildSettings | None, list[GuildGame]]:
    """The server's row and followed games; with `create_tier`, a missing row is made first.

    A missing row means a join event was missed (the bot was down), so admin commands
    create it on the fly; member commands just report "not set up".
    """
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, guild_id)
        if guild is None and create_tier is not None:
            repo.create_guild(conn, guild_id, tier=create_tier)
            guild = repo.get_guild(conn, guild_id)
        return guild, (repo.list_guild_games(conn, guild_id) if guild else [])


def _query_items_sync(
    db_path: str, guild_id: int, topic_keys: list[str], since: datetime, limit: int, offset: int
) -> tuple[list[ItemView], int]:
    limit, offset = _clamp_page(limit, offset)
    with closing(connect(db_path)) as conn:
        return repo.query_items(conn, guild_id, topic_keys, since, limit, offset)


def _search_items_sync(
    db_path: str, guild_id: int, query: str, since: datetime, limit: int, offset: int
) -> tuple[list[ItemView], int]:
    # `search_items` doesn't clamp a negative limit (SQLite would read -1 as "no limit"),
    # so the clamp lives here.
    limit, offset = _clamp_page(limit, offset)
    with closing(connect(db_path)) as conn:
        return repo.search_items(conn, guild_id, query, since, limit, offset)


def _scoped_stories_sync(
    db_path: str, topic_keys: list[str], since: datetime, label: str | None, limit: int, offset: int
) -> tuple[list[StoryView], int]:
    limit, offset = _clamp_page(limit, offset)
    with closing(connect(db_path)) as conn:
        return query_stories(conn, topic_keys, since, label, limit, offset)


def _scoped_story_search_sync(
    db_path: str, topic_keys: list[str], query: str, since: datetime, limit: int, offset: int
) -> tuple[list[StoryView], int]:
    limit, offset = _clamp_page(limit, offset)
    with closing(connect(db_path)) as conn:
        return search_stories(conn, query, since, limit, offset, topic_keys=topic_keys)


# --- /news and /shift for members, per server ---


async def _member_context(
    interaction: discord.Interaction, db_path: str
) -> tuple[GuildSettings, list[GuildGame]] | None:
    """The invoking server's row and games, or None after telling the member why not.

    Call after `defer`. "Not set up" and "follows nothing" are separate replies on
    purpose: one is the admins' job, the other is a different admin command.
    """
    if interaction.guild_id is None:
        await _say(interaction, _SERVER_ONLY)
        return None
    guild, games = await asyncio.to_thread(_guild_state_sync, db_path, interaction.guild_id)
    if guild is None or not guild.set_up:
        await _say(interaction, _NOT_SET_UP)
        return None
    return guild, games


def make_member_news_group(cfg: AppConfig, db_path: str) -> app_commands.Group:
    """Build v3's `/news recent` and `/news search`, limited to the invoking server's games.

    Owner decision D2: a free server gets stored item headlines (`items`, and
    `items_fts` for search); a comped server gets summarized stories. Either way the
    query is handed the invoking server's followed games and nothing else, so a crafted
    `game` value can't reach a game the server doesn't follow. (The value is also
    validated against the followed list before it gets that far.)
    """
    names = {game.key: game.name for game in cfg.catalog}
    label_choices = [Choice(name=label, value=label) for label in _LABEL_CHOICES]
    group = app_commands.Group(
        name="news",
        description="Recent news and search for the games this server follows.",
        guild_only=True,
        allowed_installs=_GUILD_INSTALL,
    )

    async def followed_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[Choice[str]]:
        if interaction.guild_id is None:
            return []
        _guild, games = await asyncio.to_thread(_guild_state_sync, db_path, interaction.guild_id)
        return game_choices(cfg.catalog, [g.game_key for g in games], current, include_all=True)

    async def scope(
        interaction: discord.Interaction, game: str
    ) -> tuple[GuildSettings, list[str]] | None:
        """The server's row and the game keys to query, or None after replying."""
        context = await _member_context(interaction, db_path)
        if context is None:
            return None
        guild, games = context
        followed = [g.game_key for g in games]
        if not followed:
            await _say(interaction, _NO_GAMES)
            return None
        if game.strip().casefold() == _GAME_ALL:
            return guild, followed
        chosen = resolve_game(cfg.catalog, followed, game)
        if chosen is None:
            await _say(interaction, "This server doesn't follow that game.")
            return None
        return guild, [chosen.key]

    @group.command(name="recent", description="Recent news for the games this server follows.")
    @app_commands.describe(
        game="Which game (or All)",
        days="How many days back to look (1-30)",
        label="Premium servers only: only show stories with this label",
        public="Show the result to the whole channel instead of just you",
    )
    @app_commands.choices(label=label_choices)
    async def recent(
        interaction: discord.Interaction,
        game: str = _GAME_ALL,
        days: app_commands.Range[int, 1, 30] = 7,
        label: Choice[str] | None = None,
        public: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not public)
        chosen = await scope(interaction, game)
        if chosen is None:
            return
        guild, keys = chosen
        since = datetime.now(UTC) - timedelta(days=days)
        title = "/news recent"
        if len(keys) == 1:
            title += f": {names.get(keys[0], keys[0])}"
        if guild.tier == "comped":
            fetch = functools.partial(
                _scoped_stories_sync, db_path, keys, since, label.value if label else None
            )

            def render(stories: list[StoryView], page: int, pages: int) -> discord.Embed:
                return render_story_page(stories, _topics_by_key(cfg), title, page, pages)

        else:
            # A free server sees no stories (D2), so the label filter has nothing to bite on.
            fetch = functools.partial(_query_items_sync, db_path, interaction.guild_id, keys, since)

            def render(items: list[ItemView], page: int, pages: int) -> discord.Embed:
                return render_item_page(items, names, title, page, pages)

        embed, view = await _first_page_and_view(interaction.user.id, _PAGE_SIZE, fetch, render)
        if guild.tier != "comped" and label is not None:
            await _say(
                interaction, "Labels only apply to premium servers' stories; showing headlines."
            )
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=not public, allowed_mentions=_none_mentions()
        )

    @recent.autocomplete("game")
    async def _recent_game(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        return await followed_autocomplete(interaction, current)

    @group.command(name="search", description="Search the news for the games this server follows.")
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
        # A lone surrogate (JSON can carry one; a keyboard can't) can't be encoded, which
        # would sink the query and the title we echo it in. Swap it for "?" up front.
        query = query.encode("utf-8", errors="replace").decode("utf-8")
        chosen = await scope(interaction, _GAME_ALL)
        if chosen is None:
            return
        guild, keys = chosen
        since = datetime.now(UTC) - timedelta(days=days)
        title = f"/news search: {query}"
        if guild.tier == "comped":
            fetch = functools.partial(_scoped_story_search_sync, db_path, keys, query, since)

            def render(stories: list[StoryView], page: int, pages: int) -> discord.Embed:
                return render_story_page(stories, _topics_by_key(cfg), title, page, pages)

        else:
            fetch = functools.partial(
                _search_items_sync, db_path, interaction.guild_id, query, since
            )

            def render(items: list[ItemView], page: int, pages: int) -> discord.Embed:
                return render_item_page(items, names, title, page, pages)

        embed, view = await _first_page_and_view(interaction.user.id, _PAGE_SIZE, fetch, render)
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=not public, allowed_mentions=_none_mentions()
        )

    return group


def _topics_by_key(cfg: AppConfig) -> dict[str, Topic]:
    """The catalog shaped for `render_story_page`, which only ever reads `.name`."""
    return {game.key: game for game in cfg.catalog}


def make_member_shift_group(cfg: AppConfig, db_path: str) -> app_commands.Group:
    """Build v3's `/shift codes`: the shared list of known SHiFT codes.

    The list is global (a code is a code), but the command is only for servers that
    follow a game the SHiFT detection covers: `shift.games`, or any game if that's empty.
    """
    names = {game.key: game.name for game in cfg.catalog}
    eligible = set(cfg.shift.games) or set(names)
    group = app_commands.Group(
        name="shift",
        description="SHiFT codes the bot has seen.",
        guild_only=True,
        allowed_installs=_GUILD_INSTALL,
    )

    @group.command(name="codes", description="List known SHiFT codes.")
    @app_commands.describe(
        days="How many days back to look (1-90)",
        public="Show the result to the whole channel instead of just you",
    )
    async def codes(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 14,
        public: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not public)
        context = await _member_context(interaction, db_path)
        if context is None:
            return
        guild, games = context
        if not any(g.game_key in eligible for g in games):
            covered = ", ".join(esc(names.get(key, key)) for key in cfg.shift.games)
            await _say(
                interaction,
                f"This server doesn't follow {covered}."
                if covered
                else "This server doesn't follow any games yet.",
            )
            return
        timezone = resolve_timezone(guild.timezone) or "UTC"
        since = datetime.now(UTC) - timedelta(days=days)
        fetch = functools.partial(_query_codes_sync, db_path, since)
        title = "/shift codes"

        def render(codes: list[CodeView], page: int, pages: int) -> discord.Embed:
            return render_code_page(codes, title=title, page=page, pages=pages, timezone=timezone)

        embed, view = await _first_page_and_view(
            interaction.user.id, _SHIFT_PAGE_SIZE, fetch, render
        )
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=not public, allowed_mentions=_none_mentions()
        )

    return group


# --- /newsbot for admins, per server ---


def _game_source_names(cfg: AppConfig, game_key: str) -> list[str]:
    """Names of the sources that feed `game_key`: its own, plus shared ones that cover it."""
    game = next((g for g in cfg.catalog if g.key == game_key), None)
    names = [s.name for s in game.sources if s.name] if game else []
    for shared in cfg.shared_sources:
        covers = getattr(shared, "games", None)
        if shared.type != "web_search" and shared.name and (covers is None or game_key in covers):
            names.append(shared.name)
    return names


def _next_due_line(guild: GuildSettings, now: datetime) -> str:
    """ "09:00 America/Los_Angeles, next <t:...>", or the reason it can't say."""
    try:
        local = local_run_date(now, guild.timezone)
        due = local_due_instant(local, guild.digest_time, guild.timezone)
        if due <= now:
            due = local_due_instant(local + timedelta(days=1), guild.digest_time, guild.timezone)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return "Time zone or digest time is invalid; fix it with /newsbot settings."
    return (
        f"Digest at {guild.digest_time} {esc(guild.timezone)}; next: <t:{int(due.timestamp())}:f>"
    )


def _status_embed_sync(
    cfg: AppConfig, db_path: str, guild_id: int, now: datetime
) -> discord.Embed | None:
    """Build `/newsbot status` for one server, or None if it has no row. No spend, ever."""
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, guild_id)
        if guild is None:
            return None
        games = repo.list_guild_games(conn, guild_id)
        shift = repo.get_shift(conn, guild_id)
        last = repo.latest_guild_digest(conn, guild_id)
        notices = repo.recent_notices(conn, guild_id, 5)
        failing = {row.source_name for row in repo.failing_sources(conn)}
    names = {game.key: game.name for game in cfg.catalog}

    schedule = _next_due_line(guild, now)
    if not guild.set_up:
        schedule += "\nNot set up yet; no digest will post until an admin runs /newsbot setup."
    elif not games:
        schedule += "\nFollows no games, so no digest will post."

    digest_lines = ["No digest yet."]
    if last is not None:
        digest_lines = [f"{last.run_date.isoformat()}: {last.status}"]
        channels = {g.game_key: g.channel_id for g in games}
        for key, message_id in last.posted_by_game.items():
            if isinstance(message_id, int) and key in channels:
                link = _report_jump_link(guild_id, channels[key], message_id)
                digest_lines.append(f"{esc(names.get(key, key))}: {link}")

    game_lines = []
    for game in games:
        sources = _game_source_names(cfg, game.game_key)
        health = f": {len(sources) - len(failing & set(sources))} of {len(sources)} sources ok"
        game_lines.append(
            f"{esc(names.get(game.game_key, game.game_key))} in <#{game.channel_id}>"
            f"{health if sources else ''}"
        )

    if shift is None or not shift.enabled:
        shift_line = "Off. Turn it on with /newsbot shift."
    else:
        ping = {"none": "no ping", "everyone": "@everyone"}.get(shift.ping, f"<@&{shift.ping}>")
        try:
            today_key = local_run_date(now, guild.timezone).isoformat()
        except ZoneInfoNotFoundError, ValueError, OSError:
            today_key = None
        used = shift.ping_count if shift.ping_day == today_key else 0
        shift_line = (
            f"On in <#{shift.channel_id}>, ping: {ping}. "
            f"Pings today: {used} of {cfg.shift.max_pings_per_day}."
        )
    return render_guild_overview(
        tier=guild.tier,
        schedule_line=schedule,
        digest_lines=digest_lines,
        game_lines=game_lines,
        shift_line=shift_line,
        notices=notices,
    )


def _late_note_sync(db_path: str, guild_id: int, now: datetime) -> str | None:
    """A "posts within a minute" note if today's digest time passed with no digest yet."""
    with closing(connect(db_path)) as conn:
        guild = repo.get_guild(conn, guild_id)
        if guild is None or not guild.set_up or not repo.list_guild_games(conn, guild_id):
            return None
        try:
            local = local_run_date(now, guild.timezone)
            due = local_due_instant(local, guild.digest_time, guild.timezone)
        except ZoneInfoNotFoundError, ValueError, OSError:
            return None
        if due <= now and repo.get_guild_digest(conn, guild_id, local) is None:
            return (
                "That time has already passed today, so today's digest will post within a minute."
            )
    return None


def _save_settings_sync(
    db_path: str, guild_id: int, tier: Tier, changes: dict[str, object]
) -> GuildSettings | None:
    with closing(connect(db_path)) as conn:
        if repo.get_guild(conn, guild_id) is None:
            repo.create_guild(conn, guild_id, tier=tier)
        repo.update_guild_settings(conn, guild_id, **changes)
        return repo.get_guild(conn, guild_id)


def _save_shift_sync(
    db_path: str,
    guild_id: int,
    tier: Tier,
    *,
    enabled: bool | None,
    channel_id: int | None,
    ping: str | None,
) -> tuple[bool, int | None, str, list[GuildGame]]:
    """Merge the options over the stored SHiFT row and save. Returns what was saved + games.

    Raises `ValueError` (with a reply-ready message) when enabling would leave no channel.
    """
    with closing(connect(db_path)) as conn:
        if repo.get_guild(conn, guild_id) is None:
            repo.create_guild(conn, guild_id, tier=tier)
        current = repo.get_shift(conn, guild_id)
        new_channel = channel_id if channel_id is not None else (current and current.channel_id)
        new_ping = ping if ping is not None else (current.ping if current else "none")
        if enabled is None:
            new_enabled = current.enabled if current else True
        else:
            new_enabled = enabled
        if new_enabled and not new_channel:
            raise ValueError("Pick a channel for the SHiFT codes with channel:.")
        repo.set_shift(
            conn, guild_id, enabled=new_enabled, channel_id=new_channel or None, ping=new_ping
        )
        return new_enabled, new_channel or None, new_ping, repo.list_guild_games(conn, guild_id)


def _follow_sync(
    db_path: str, guild_id: int, tier: Tier, game_key: str, channel_id: int
) -> tuple[str, GuildSettings | None]:
    """Follow `game_key`. Returns ("new" | "moved" | "limit", the server's row)."""
    with closing(connect(db_path)) as conn:
        if repo.get_guild(conn, guild_id) is None:
            repo.create_guild(conn, guild_id, tier=tier)
        try:
            is_new = repo.follow_game(conn, guild_id, game_key, channel_id)
        except repo.GameLimitError:
            return "limit", repo.get_guild(conn, guild_id)
        return ("new" if is_new else "moved"), repo.get_guild(conn, guild_id)


def _unfollow_sync(db_path: str, guild_id: int, game_key: str) -> tuple[bool, int]:
    """Unfollow. Returns (it was followed, how many games are left)."""
    with closing(connect(db_path)) as conn:
        removed = repo.unfollow_game(conn, guild_id, game_key)
        return removed, len(repo.list_guild_games(conn, guild_id))


def _tier_for(cfg: AppConfig, guild_id: int) -> Tier:
    return "comped" if guild_id in cfg.comped_guild_ids else "free"


def make_guild_admin_group(
    cfg: AppConfig,
    bot: NewsBot,
    *,
    digest_deps: Callable[[], GuildDigestDeps] | None = None,
) -> app_commands.Group:
    """Build v3's `/newsbot`: per-server follow, settings, SHiFT, status, preview and run-now.

    Every handler: requires a server, re-checks Manage Server on the interaction
    (`_check_admin`; `default_permissions` is only what Discord's client shows),
    creates the server's row if a missed join left it without one, and replies
    ephemerally. Every channel option goes through `channel_problem` before anything is
    written, and after any change `check_guild` reports what the bot still can't do.

    `digest_deps` returns the one shared `GuildDigestDeps` (its per-server locks live in
    it, so it must be the same object every call). The default is
    `bot.guild_digest_deps`, which the cutover task adds; without either, `preview` and
    `run-now` say they're not available. `/newsbot setup` hands off to the wizard in `setup_views`.
    """
    db_path = bot.db_path
    names = {game.key: game.name for game in cfg.catalog}
    eligible_shift = set(cfg.shift.games) or set(names)
    group = app_commands.Group(
        name="newsbot",
        description="Admin: this server's games, settings, SHiFT alerts and digests.",
        default_permissions=discord.Permissions(**{cfg.admin_permission: True}),
        guild_only=True,
        allowed_installs=_GUILD_INSTALL,
    )

    def get_deps() -> GuildDigestDeps | None:
        factory = digest_deps or getattr(bot, "guild_digest_deps", None)
        return factory() if factory is not None else None

    async def gate(interaction: discord.Interaction) -> int | None:
        """The invoking server's id if this is a server and the caller is an admin."""
        if interaction.guild_id is None:
            await interaction.response.send_message(_SERVER_ONLY, ephemeral=True)
            return None
        if not await _check_admin(interaction, cfg.admin_permission):
            return None
        return interaction.guild_id

    async def permission_lines(guild_id: int) -> list[str]:
        check = await check_guild(bot, db_path, guild_id, game_names=names)
        return check.lines()

    async def reply_with_check(interaction: discord.Interaction, saved: str, guild_id: int) -> None:
        lines = await permission_lines(guild_id)
        if lines:
            saved += "\nBut I can't do everything I'm set up to do:\n" + "\n".join(
                f"- {line}" for line in lines
            )
        await _say(interaction, saved)

    async def followed_autocomplete(
        interaction: discord.Interaction, current: str, *, followed: bool
    ) -> list[Choice[str]]:
        if interaction.guild_id is None:
            return []
        _guild, games = await asyncio.to_thread(_guild_state_sync, db_path, interaction.guild_id)
        have = {g.game_key for g in games}
        keys = have if followed else {g.key for g in cfg.catalog} - have
        return game_choices(cfg.catalog, keys, current)

    @group.command(
        name="setup", description="Guided setup: time zone, digest time, games and a channel."
    )
    async def setup(interaction: discord.Interaction) -> None:
        # Imported here because setup_views borrows this module's helpers at import
        # time, and two modules can't both go first.
        from newsbot.bot.setup_views import SetupView  # noqa: PLC0415

        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        # Read-only on purpose: a missing row isn't created until Save, so opening the
        # wizard and walking away leaves no trace.
        guild, followed = await asyncio.to_thread(_guild_state_sync, db_path, guild_id)
        view = SetupView(
            cfg=cfg,
            bot=bot,
            db_path=db_path,
            owner_id=interaction.user.id,
            guild_id=guild_id,
            tier=_tier_for(cfg, guild_id),
            guild=guild,
            followed=followed,
            origin=interaction,
        )
        await interaction.followup.send(
            view.render(), view=view, ephemeral=True, allowed_mentions=_none_mentions()
        )

    @group.command(
        name="follow", description="Follow a game: its digest posts in the channel you pick."
    )
    @app_commands.describe(game="Which game to follow", channel="Where its daily digest posts")
    async def follow(
        interaction: discord.Interaction, game: str, channel: discord.TextChannel
    ) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        chosen = resolve_game(cfg.catalog, [g.key for g in cfg.catalog], game)
        if chosen is None:
            await _say(interaction, "I don't know that game; pick one from the list.")
            return
        problem = channel_problem(channel, guild_id)
        if problem is not None:
            await _say(interaction, problem)
            return
        outcome, guild = await asyncio.to_thread(
            _follow_sync, db_path, guild_id, _tier_for(cfg, guild_id), chosen.key, channel.id
        )
        if outcome == "limit":
            await _say(
                interaction,
                f"This server already follows {repo.MAX_GAMES_PER_GUILD} games; "
                "unfollow one first (/newsbot unfollow).",
            )
            return
        verb = "Now following" if outcome == "new" else "Moved"
        saved = f"{verb} {esc(chosen.name)}: its digest posts in <#{channel.id}>."
        if guild is not None and not guild.set_up:
            saved += _NOT_SET_UP_POSTS_NOTHING
        await reply_with_check(interaction, saved, guild_id)

    @follow.autocomplete("game")
    async def _follow_game(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        return await followed_autocomplete(interaction, current, followed=False)

    @group.command(name="unfollow", description="Stop following a game.")
    @app_commands.describe(game="Which game to stop following")
    async def unfollow(interaction: discord.Interaction, game: str) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        _guild, games = await asyncio.to_thread(_guild_state_sync, db_path, guild_id)
        chosen = resolve_game(cfg.catalog, [g.game_key for g in games], game)
        if chosen is None:
            await _say(interaction, "This server doesn't follow that game.")
            return
        _removed, left = await asyncio.to_thread(_unfollow_sync, db_path, guild_id, chosen.key)
        reply = f"Stopped following {esc(chosen.name)}."
        if left == 0:
            reply += (
                " This server follows no games now, so no digest will post until you follow one."
            )
        await _say(interaction, reply)

    @unfollow.autocomplete("game")
    async def _unfollow_game(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        return await followed_autocomplete(interaction, current, followed=True)

    @group.command(
        name="games", description="Games this server follows, and the rest of the catalog."
    )
    async def games(interaction: discord.Interaction) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        _guild, followed = await asyncio.to_thread(_guild_state_sync, db_path, guild_id)
        have = {g.game_key for g in followed}
        lines = [
            f"- {esc(names.get(g.game_key, g.game_key))} in <#{g.channel_id}>" for g in followed
        ] or ["(none yet)"]
        others = [esc(g.name) for g in cfg.catalog if g.key not in have]
        text = f"Following ({len(followed)} of {repo.MAX_GAMES_PER_GUILD}):\n" + "\n".join(lines)
        if others:
            text += "\n\nAvailable: " + ", ".join(others)
        await _say(interaction, text)

    @group.command(name="settings", description="Digest time, time zone and the admin channel.")
    @app_commands.describe(
        time="Digest time as HH:MM, 24-hour (for example 09:00)",
        timezone="IANA time zone (for example America/Chicago)",
        admin_channel="Where I post problems and run reports",
        clear_admin_channel="Stop posting problems and run reports to a channel",
    )
    async def settings(
        interaction: discord.Interaction,
        time: str | None = None,
        timezone: str | None = None,
        admin_channel: discord.TextChannel | None = None,
        clear_admin_channel: bool = False,
    ) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        changes: dict[str, object] = {}
        said: list[str] = []
        if time is not None:
            parsed = parse_digest_time(time.strip())
            if parsed is None:
                await _say(interaction, "The time must be HH:MM, 24-hour (00:00 to 23:59).")
                return
            changes["digest_time"] = parsed
            said.append(f"digest time {parsed}")
        if timezone is not None:
            zone = resolve_timezone(timezone)
            if zone is None:
                await _say(
                    interaction, "That isn't a time zone I know; try one like America/Chicago."
                )
                return
            changes["timezone"] = zone
            said.append(f"time zone {zone}")
        if admin_channel is not None and clear_admin_channel:
            await _say(interaction, "Use either admin_channel or clear_admin_channel, not both.")
            return
        if admin_channel is not None:
            problem = channel_problem(admin_channel, guild_id)
            if problem is not None:
                await _say(interaction, problem)
                return
            changes["admin_channel_id"] = admin_channel.id
            said.append(f"admin channel <#{admin_channel.id}>")
        elif clear_admin_channel:
            changes["admin_channel_id"] = None
            said.append("admin channel cleared")
        if not changes:
            await _say(interaction, "Nothing to change; give me a time, timezone or admin channel.")
            return
        await asyncio.to_thread(
            _save_settings_sync, db_path, guild_id, _tier_for(cfg, guild_id), changes
        )
        saved = "Saved: " + ", ".join(said) + "."
        if "digest_time" in changes or "timezone" in changes:
            late = await asyncio.to_thread(_late_note_sync, db_path, guild_id, datetime.now(UTC))
            if late:
                saved += "\n" + late
        await reply_with_check(interaction, saved, guild_id)

    @settings.autocomplete("timezone")
    async def _settings_zone(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        return zone_choices(current)

    @group.command(name="shift", description="SHiFT code alerts: channel, ping and on/off.")
    @app_commands.describe(
        channel="Where codes post",
        enabled="Turn the alerts on or off",
        ping="Who gets pinged: nobody, everyone, or a role",
        role="The role to ping (with ping: role)",
    )
    @app_commands.choices(ping=[Choice(name=p, value=p) for p in _PING_CHOICES])
    async def shift(
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        enabled: bool | None = None,
        ping: Choice[str] | None = None,
        role: discord.Role | None = None,
    ) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        if channel is not None:
            problem = channel_problem(channel, guild_id)
            if problem is not None:
                await _say(interaction, problem)
                return
        ping_value = ping.value if ping is not None else None
        if role is not None and ping_value is None:
            ping_value = "role"
        if ping_value == "role" and role is None:
            await _say(interaction, "ping: role needs a role; pick one with role:.")
            return
        if role is not None and ping_value != "role":
            await _say(interaction, "role: only goes with ping: role.")
            return
        stored_ping: str | None = ping_value
        if role is not None:
            role_guild = getattr(getattr(role, "guild", None), "id", None)
            if role_guild != guild_id:
                await _say(interaction, "That role isn't in this server.")
                return
            if role.is_default():
                await _say(interaction, "@everyone isn't a role to pick; use ping: everyone.")
                return
            stored_ping = str(role.id)
        try:
            is_on, _channel_id, _ping, followed = await asyncio.to_thread(
                functools.partial(
                    _save_shift_sync,
                    db_path,
                    guild_id,
                    _tier_for(cfg, guild_id),
                    enabled=enabled,
                    channel_id=channel.id if channel is not None else None,
                    ping=stored_ping,
                )
            )
        except ValueError as exc:
            await _say(interaction, str(exc))
            return
        saved = "Saved: SHiFT alerts are on." if is_on else "Saved: SHiFT alerts are off."
        if is_on and not any(g.game_key in eligible_shift for g in followed):
            covered = ", ".join(esc(names.get(key, key)) for key in cfg.shift.games) or "a game"
            saved = f"Saved, but nothing will post until this server follows {covered}."
        await reply_with_check(interaction, saved, guild_id)

    @group.command(
        name="status", description="This server's settings, last digest and recent problems."
    )
    async def status(interaction: discord.Interaction) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await asyncio.to_thread(
            _status_embed_sync, cfg, db_path, guild_id, datetime.now(UTC)
        )
        if embed is None:
            await _say(interaction, _NOT_SET_UP)
            return
        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=_none_mentions()
        )

    async def ready_to_run(interaction: discord.Interaction, guild_id: int):
        """The shared digest deps, if this server can have a digest right now (else replies).

        Nothing here is slow (local reads), so `run-now` can call it before deciding
        whether its initial response is a defer or the confirm dialog.
        """
        deps = get_deps()
        if deps is None:
            await _say(interaction, _UNWIRED)
            return None
        guild, games = await asyncio.to_thread(_guild_state_sync, db_path, guild_id)
        if guild is None or not guild.set_up:
            await _say(interaction, _NOT_SET_UP)
            return None
        if not games:
            await _say(interaction, _NO_GAMES)
            return None
        if is_guild_busy(deps, guild_id):
            await _say(interaction, _DIGEST_BUSY)
            return None
        return deps

    @group.command(name="preview", description="Show this server's next digest, only to you.")
    async def preview(interaction: discord.Interaction) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        deps = await ready_to_run(interaction, guild_id)
        if deps is None:
            return
        try:
            result = await preview_guild_digest(deps, guild_id)
        except GuildTimeZoneError:
            await _say(interaction, _ZONE_INVALID)
            return
        if result is None or not result.rendered.messages:
            await _say(interaction, "Nothing would post right now: no followed game has news.")
            return
        for message in result.rendered.messages:
            await interaction.followup.send(
                content=f"Would post in <#{message.channel_id}>:",
                embed=message.embed,
                ephemeral=True,
                allowed_mentions=_none_mentions(),
            )

    @group.command(name="run-now", description="Post this server's digest now.")
    async def run_now(interaction: discord.Interaction) -> None:
        guild_id = await gate(interaction)
        if guild_id is None:
            return
        deps = await ready_to_run(interaction, guild_id)
        if deps is None:
            return
        try:
            existing = await todays_guild_digest(deps, guild_id)
        except GuildTimeZoneError:
            await _say(interaction, _ZONE_INVALID)
            return

        force = False
        if guild_needs_confirmation(existing):
            view = ConfirmView(interaction.user.id)
            if existing is not None and existing.status in ("pending", "failed"):
                prompt = (
                    "A previous run may have crashed mid-post; check the game channels. "
                    "Post anyway?"
                )
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

        try:
            outcome = await run_guild_digest(deps, guild_id, kind=RunKind.RUN_NOW, force=force)
        except GuildTimeZoneError:
            await _say(interaction, _ZONE_INVALID)
            return
        reply = f"Run finished: {outcome.status}"
        notes = [esc(plain_line(note, 200)) for note in outcome.notes[:3]]
        if notes:
            reply += "\n" + "\n".join(f"- {note}" for note in notes)
        await _say(interaction, reply)

    return group


def make_lounge_group(cfg: AppConfig, bot: NewsBot) -> app_commands.Group:
    """Build `/lounge quote-now`, registered only in servers that have a lounge (D3).

    Any server with a lounge row and its quote on can use it. It runs through
    `bot.run_guild_quote`, so the sources, channel, date guard and deck are that
    server's own and nobody else's.
    """
    db_path = bot.db_path
    group = app_commands.Group(
        name="lounge",
        description="Admin: the lounge's daily quote.",
        default_permissions=discord.Permissions(**{cfg.admin_permission: True}),
        guild_only=True,
        allowed_installs=_GUILD_INSTALL,
    )

    @group.command(
        name="quote-now",
        description="Post today's lounge quote now (the scheduled one then skips today).",
    )
    async def quote_now(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(_SERVER_ONLY, ephemeral=True)
            return
        if not await _check_admin(interaction, cfg.admin_permission):
            return
        guild, _games = await asyncio.to_thread(_guild_state_sync, db_path, interaction.guild_id)
        lounge = await asyncio.to_thread(_get_lounge_sync, db_path, interaction.guild_id)
        if guild is None or lounge is None or not lounge.quote_enabled:
            await interaction.response.send_message(
                "This server doesn't have a lounge quote set up.", ephemeral=True
            )
            return
        zone = resolve_timezone(guild.timezone)
        if zone is None:
            await interaction.response.send_message(_ZONE_INVALID, ephemeral=True)
            return
        today = local_run_date(datetime.now(UTC), zone).isoformat()
        already = await asyncio.to_thread(
            _quote_posted_today_sync, db_path, today, interaction.guild_id
        )

        force = False
        if already:
            view = ConfirmView(interaction.user.id)
            await interaction.response.send_message(
                "Today's quote already posted. Post another one?", view=view, ephemeral=True
            )
            await view.wait()
            if not view.value:
                await interaction.edit_original_response(content="Cancelled.", view=None)
                return
            force = True
            await interaction.edit_original_response(content="Posting...", view=None)
        else:
            await interaction.response.defer(ephemeral=True)

        outcome = await bot.run_guild_quote(interaction.guild_id, force)
        await _say(
            interaction,
            _lounge_quote_reply(
                interaction.guild_id, lounge.channel_id, outcome.status, outcome.message_id
            ),
        )

    return group


def _get_lounge_sync(db_path: str, guild_id: int):
    with closing(connect(db_path)) as conn:
        return repo.get_lounge(conn, guild_id)


__all__ = [
    "estimate_spend_usd",
    "has_admin_permission",
    "channel_problem",
    "game_choices",
    "invalid_test_alert_code_message",
    "make_admin_group",
    "make_guild_admin_group",
    "make_lounge_group",
    "make_member_news_group",
    "make_member_shift_group",
    "make_news_group",
    "make_shift_group",
    "needs_confirmation",
    "parse_digest_time",
    "resolve_game",
    "resolve_query_args",
    "resolve_timezone",
    "summarize_test_alert",
    "zone_choices",
]
