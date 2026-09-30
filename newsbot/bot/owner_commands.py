"""`/owner servers`: the one command that looks at every server at once.

It exists so the owner can answer "how's the public bot doing?" without opening
the database, and it's deliberately all numbers: counts of servers, tiers and
digest outcomes, plus the month's summary spend. No server names, no channel
names, nothing a stranger's settings could put in front of the owner. (The
owner has enough to read. So does everyone.)

Two locks keep it from being anyone else's command. It's registered only in the
home guild (see `registration.py`), which stops it showing up elsewhere. And the
handler checks, every time, that it's running in the home guild *and* that the
caller owns the application, because registration is about what Discord shows,
not about who gets to run it. A non-owner gets the same "no permission" reply an
admin command gives, home guild or not.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

from newsbot.bot.client import NewsBot
from newsbot.config import AppConfig
from newsbot.guilds.schedule import local_due_instant
from newsbot.pipeline.summarize import estimate_spend_usd
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import DueCandidate

logger = logging.getLogger(__name__)

_DENIAL = "You don't have permission to run this."
# Buckets for "today's digests", in the order the reply lists them.
DIGEST_BUCKETS = ("ok", "partial", "failed", "pending", "not yet due", "due, none yet", "bad zone")


def summarize_digests(candidates: Sequence[DueCandidate], now: datetime) -> Counter[str]:
    """Bucket each set-up server that follows a game by how its local "today" is going.

    A candidate's newest digest row counts as today's only if its `run_date` is the
    server's local date right now; otherwise the server has had no digest today, and
    it's either `not yet due` or `due, none yet` (the minute job will get to it), by
    the same due-instant arithmetic the scheduler uses. A zone or time that can't be
    used is `bad zone`, which the scheduler skips too.
    """
    counts: Counter[str] = Counter()
    for candidate in candidates:
        try:
            today = now.astimezone(ZoneInfo(candidate.timezone)).date()
            due = local_due_instant(today, candidate.digest_time, candidate.timezone)
        except ZoneInfoNotFoundError, ValueError, OSError:
            counts["bad zone"] += 1
            continue
        if candidate.run_date == today and candidate.status in (
            "ok",
            "partial",
            "failed",
            "pending",
        ):
            counts[candidate.status] += 1
        else:
            counts["not yet due" if now < due else "due, none yet"] += 1
    return counts


def render_owner_servers(
    counts: dict[str, int], digests: Counter[str], spend_usd: float, month: str
) -> str:
    """The reply: plain numbers, one fact per line."""
    lines = [
        f"Servers: {counts['servers']} ({counts['set_up']} set up)",
        f"Tiers: {counts['free']} free, {counts['comped']} comped",
        f"SHiFT alerts on: {counts['shift_enabled']}",
        f"Servers with permission problems: {counts['permission_problems']}",
        "Today's digests: " + ", ".join(f"{digests[b]} {b}" for b in DIGEST_BUCKETS),
        f"Summary spend, {month}: about ${spend_usd:.2f}",
    ]
    return "\n".join(lines)


def _owner_numbers_sync(db_path: str, now: datetime) -> tuple[dict[str, int], Counter[str], float]:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with closing(connect(db_path)) as conn:
        counts = repo.guild_counts(conn)
        digests = summarize_digests(repo.due_candidates(conn), now)
        usage = repo.game_summary_tokens(conn, month_start)
    return counts, digests, estimate_spend_usd(usage.input_tokens, usage.output_tokens)


def make_owner_group(cfg: AppConfig, bot: NewsBot) -> app_commands.Group:
    """Build `/owner servers`. Register it in the home guild only (D3)."""
    group = app_commands.Group(
        name="owner",
        description="Owner only: how the public bot is doing.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )

    @group.command(name="servers", description="Server counts, today's digests and month spend.")
    async def servers(interaction: discord.Interaction) -> None:
        in_home = cfg.home_guild_id is not None and interaction.guild_id == cfg.home_guild_id
        # Short-circuit: outside the home guild nobody is even asked who they are.
        if not in_home or not await bot.is_owner(interaction.user):
            logger.warning(
                "owner command denied",
                extra={"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            )
            await interaction.response.send_message(_DENIAL, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        now = datetime.now(UTC)
        counts, digests, spend = await asyncio.to_thread(_owner_numbers_sync, bot.db_path, now)
        await interaction.followup.send(
            render_owner_servers(counts, digests, spend, now.strftime("%Y-%m")),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    return group


__all__ = ["make_owner_group", "render_owner_servers", "summarize_digests"]
