"""The slash commands: `/news recent` and `/news search`.

Both are built by a factory function (`make_news_group`) rather than
declared as a module-level class, because the game choices on
`/news recent` come from `cfg.topics` -- they can't exist before the
config has loaded. `setup_hook` calls the factory once, after the config
is in hand, and hands the result to the command tree.

Every handler calls `interaction.response.defer()` inside the
Discord-mandated 3 seconds, then does the slow database read after.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.app_commands import Choice

from newsbot.bot.format import render_story_page
from newsbot.bot.views import PagerView
from newsbot.config import AppConfig, Topic
from newsbot.store.db import connect
from newsbot.store.models import StoryView
from newsbot.store.repo import query_stories, search_stories

_PAGE_SIZE = 6
_GAME_ALL = "all"
_LABEL_CHOICES = ("official", "reported", "rumor")


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


def _page_count(total: int) -> int:
    return max((total + _PAGE_SIZE - 1) // _PAGE_SIZE, 1)


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


__all__ = ["make_news_group", "resolve_query_args"]
