"""Does the bot actually have what a server's settings say it needs?

Settings validation can tell that a channel id is a positive int and that the
pieces fit together; it has no way to ask Discord whether the bot's role can
post there, because that answer doesn't exist until the gateway connects and
the guild's permission overwrites are in hand. This module is that second,
later check. It walks the channels one server's database rows point at and asks
Discord directly. Anything wrong (a missing permission, a channel that's gone,
a channel that belongs to some other server entirely) becomes one line. Commands
run it right after a setting changes, and the startup sweep runs it for every
server. The bot starts either way; a permission problem on day one is annoying,
but it's a lot less annoying than a bot that refuses to come up at all because
`#palworld` forgot Embed Links.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import discord

from newsbot.bot.format import _truncate_utf16, esc
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import GuildGame, GuildSettings, LoungeSettings, ShiftSettings
from newsbot.text import plain_line

logger = logging.getLogger(__name__)

# Discord's plain-message content cap: same constant `format.py` uses for
# a code alert, reused here rather than imported since this is the only
# other place in the codebase that needs it and a second admin-alert
# constant module felt like overkill for one int.
_ALERT_LIMIT = 2000

# How long the startup sweep spends on one server (its channel lookups, the
# notice, the bookkeeping) before calling it failed and moving on. Generous:
# a healthy server takes well under a second.
_GUILD_TIMEOUT_S = 60.0

# Every permission flag this module ever asks about, in a fixed order so
# "missing X, Y" reads the same way every time instead of shuffling with
# whatever order a frozenset happens to iterate in this process.
_PERMISSION_ORDER = ("view_channel", "send_messages", "embed_links", "mention_everyone")
_PERMISSION_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "mention_everyone": "Mention @everyone",
}


@dataclass(frozen=True)
class ChannelRequirement:
    """What one channel needs to do its job, and why."""

    channel_id: int
    purpose: str
    needed: frozenset[str]
    # A SHiFT channel whose ping is a role id. Whether that role needs
    # Mention @everyone depends on `role.mentionable`, which only the live
    # guild can answer, so the pure requirement just carries the id.
    ping_role_id: int | None = None


def _add_requirement(
    by_channel: dict[int, ChannelRequirement],
    channel_id: int,
    purpose: str,
    needed: frozenset[str],
    ping_role_id: int | None = None,
) -> None:
    """Add a requirement, merging with any earlier one for the same channel.

    Two features pointed at one channel end up as one requirement with the
    union of what either needs, not two alerts about the same channel.
    """
    existing = by_channel.get(channel_id)
    if existing is None:
        by_channel[channel_id] = ChannelRequirement(channel_id, purpose, needed, ping_role_id)
        return
    # Exact match on the split parts, not `in`: "Path of Exile" is a substring
    # of "Path of Exile 2", and the second feature shouldn't vanish over it.
    parts = existing.purpose.split(" / ")
    merged_purpose = existing.purpose if purpose in parts else f"{existing.purpose} / {purpose}"
    by_channel[channel_id] = ChannelRequirement(
        channel_id,
        merged_purpose,
        existing.needed | needed,
        existing.ping_role_id if existing.ping_role_id is not None else ping_role_id,
    )


def missing(perms: discord.Permissions, needed: frozenset[str]) -> list[str]:
    """Which of `needed`'s flags `perms` doesn't grant, in `_PERMISSION_ORDER`."""
    return [flag for flag in _PERMISSION_ORDER if flag in needed and not getattr(perms, flag)]


async def _resolve_channel(client: discord.Client, channel_id: int) -> object:
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    return await client.fetch_channel(channel_id)


# Voice and stage channels are, technically, `Messageable` in discord.py
# (Discord grew text chat in voice channels a while back), but they're
# not what an owner means by "post the digest here", so they stay flagged
# even though they'd otherwise pass the check below.
_EXCLUDED_CHANNEL_TYPES = (discord.VoiceChannel, discord.StageChannel)


def _is_sendable_guild_channel(channel: object) -> bool:
    """True for anything `_inspect` should treat as a normal text destination.

    A thread isn't a `discord.abc.GuildChannel` (it's its own class), but
    it's exactly as sendable as the channel it lives in, and there's no
    design.md reason a game or SHiFT channel couldn't be one: excluding
    it just because it fails an `isinstance(..., TextChannel)` check was
    the actual bug here. Forum and category channels fail this on their
    own (neither is `Messageable`); voice and stage channels are excluded
    explicitly, above.
    """
    if isinstance(channel, _EXCLUDED_CHANNEL_TYPES):
        return False
    if isinstance(channel, discord.Thread):
        return True
    return isinstance(channel, discord.abc.Messageable) and isinstance(
        channel, discord.abc.GuildChannel
    )


def sendability_problem(channel: object) -> str | None:
    """Why this bot can't post in `channel` (one it can already see), or None if it can.

    A real channel has to be a text destination (a category, a forum or a voice channel is
    nowhere to send an alert) and the bot's own member needs View Channel and Send Messages
    in it. Something that can't be asked at all (no `permissions_for`, or no member of the
    bot's to ask about) passes: not being able to tell isn't a finding.
    """
    if not hasattr(channel, "permissions_for"):
        return None
    if not _is_sendable_guild_channel(channel):
        return "isn't a text channel"
    me = getattr(getattr(channel, "guild", None), "me", None)
    if me is None:
        return None
    lacking = missing(channel.permissions_for(me), frozenset({"view_channel", "send_messages"}))
    if lacking:
        return "is a channel where I'm missing " + " and ".join(
            _PERMISSION_LABELS[flag] for flag in lacking
        )
    return None


@dataclass(frozen=True)
class ChannelProblem:
    """One broken channel, in a shape a command reply can use.

    `kind` is one of `not_found`, `wrong_guild`, `not_text`, `unknown_self`,
    `missing_permissions`, `no_role` or `check_failed`; `missing` holds the
    permission labels for the `missing_permissions` kind. `text` is the
    one-line human version (one line, ready to put in a reply).
    """

    channel_id: int
    purpose: str
    kind: str
    missing: tuple[str, ...]
    text: str


async def _inspect(
    client: discord.Client, guild_id: int, req: ChannelRequirement
) -> ChannelProblem | None:
    """Return what's wrong with `req`'s channel, or None if it checks out clean."""
    cid, purpose = req.channel_id, req.purpose

    def problem(kind: str, text: str, names: tuple[str, ...] = ()) -> ChannelProblem:
        return ChannelProblem(cid, purpose, kind, names, f"{purpose} channel <#{cid}>: {text}")

    try:
        channel = await _resolve_channel(client, cid)
    except discord.NotFound, discord.Forbidden:
        return problem("not_found", "not found or not visible to the bot")

    guild = getattr(channel, "guild", None)
    if guild is None or guild.id != guild_id:
        return problem("wrong_guild", "not in the configured guild")
    if not _is_sendable_guild_channel(channel):
        return problem("not_text", "not a text channel")

    me = guild.me
    if me is None:
        return problem("unknown_self", "can't tell this bot's own permissions there")

    perms = channel.permissions_for(me)
    needed = req.needed
    if req.ping_role_id is not None:
        role = guild.get_role(req.ping_role_id)
        if role is None:
            return problem("no_role", f"the role to ping (<@&{req.ping_role_id}>) no longer exists")
        if not role.mentionable:
            needed = needed | {"mention_everyone"}
    missing_flags = missing(perms, needed)
    if not missing_flags:
        return None
    names = tuple(_PERMISSION_LABELS[flag] for flag in missing_flags)
    return problem("missing_permissions", f"missing {', '.join(names)}", names)


# --- Per guild (design.md §15, plan task 8) ---
#
# The list of channels comes from one server's database rows, and the answer
# is a structure rather than a flat list of strings, so a command can reply
# "I can't post in <#id>: missing Send Messages" the moment an admin changes
# a setting. (v2 checked the one configured server from config.yaml; that went
# away at the cutover.)


def required_channels_for_guild(
    guild: GuildSettings,
    games: list[GuildGame],
    shift: ShiftSettings | None,
    lounge: LoungeSettings | None,
    *,
    game_names: Mapping[str, str] | None = None,
) -> list[ChannelRequirement]:
    """Every channel one server's settings point at, and what each needs. Pure.

    Game channels need View Channel, Send Messages and Embed Links. The
    SHiFT channel (only when enabled) needs View and Send, plus Mention
    @everyone when the ping is `everyone`; a role ping carries the role id
    so the live check can add Mention @everyone if that role isn't
    mentionable. The admin channel (if set) and the lounge channel (if a
    lounge row exists) need View and Send. Channels shared between features
    merge into one requirement.
    """
    names = game_names or {}
    by_channel: dict[int, ChannelRequirement] = {}
    for game in games:
        _add_requirement(
            by_channel,
            game.channel_id,
            names.get(game.game_key, game.game_key),
            frozenset({"view_channel", "send_messages", "embed_links"}),
        )
    if shift is not None and shift.enabled and shift.channel_id is not None:
        needed = {"view_channel", "send_messages"}
        role_id: int | None = None
        if shift.ping == "everyone":
            needed.add("mention_everyone")
        elif shift.ping.isdigit():
            role_id = int(shift.ping)
        _add_requirement(by_channel, shift.channel_id, "SHiFT codes", frozenset(needed), role_id)
    if guild.admin_channel_id is not None:
        _add_requirement(
            by_channel,
            guild.admin_channel_id,
            "admin",
            frozenset({"view_channel", "send_messages"}),
        )
    # A lounge with both features off needs no channel.
    if lounge is not None and (lounge.welcome_enabled or lounge.quote_enabled):
        _add_requirement(
            by_channel, lounge.channel_id, "lounge", frozenset({"view_channel", "send_messages"})
        )
    return list(by_channel.values())


def requirements_from_db(
    conn: sqlite3.Connection, guild_id: int, *, game_names: Mapping[str, str] | None = None
) -> list[ChannelRequirement]:
    """`required_channels_for_guild` for `guild_id`'s stored settings; empty if no such guild."""
    guild = repo.get_guild(conn, guild_id)
    if guild is None:
        return []
    return required_channels_for_guild(
        guild,
        repo.list_guild_games(conn, guild_id),
        repo.get_shift(conn, guild_id),
        repo.get_lounge(conn, guild_id),
        game_names=game_names,
    )


@dataclass(frozen=True)
class GuildCheck:
    """The result of checking one server: empty `problems` means all clear."""

    guild_id: int
    problems: tuple[ChannelProblem, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems

    def lines(self) -> list[str]:
        """One human-readable line per problem, in requirement order."""
        return [p.text for p in self.problems]


async def check_guild_channels(
    client: discord.Client, guild_id: int, requirements: list[ChannelRequirement]
) -> GuildCheck:
    """Check `requirements` against Discord for `guild_id`. Never raises.

    One channel blowing up becomes its own problem instead of hiding the rest
    (a crash resolving one channel, a network blip mid-check, an unexpected
    discord.py exception: the walk carries on). A channel that belongs to another
    server is reported as a problem and its permissions are never read, so
    one server can't learn anything about another's.
    """
    found: list[ChannelProblem] = []
    for req in requirements:
        try:
            problem = await _inspect(client, guild_id, req)
        except Exception as exc:  # noqa: BLE001 (one bad channel must not stop the rest)
            logger.exception("permission check failed for channel %d", req.channel_id)
            text = (
                f"{req.purpose} channel <#{req.channel_id}>: "
                f"permission check failed ({esc(plain_line(str(exc), 100))})"
            )
            problem = ChannelProblem(req.channel_id, req.purpose, "check_failed", (), text)
        if problem is not None:
            found.append(problem)
    return GuildCheck(guild_id, tuple(found))


async def check_guild(
    client: discord.Client,
    db_path: str | Path,
    guild_id: int,
    *,
    game_names: Mapping[str, str] | None = None,
) -> GuildCheck:
    """Read `guild_id`'s settings from the database and check them: what commands call.

    A server with no row at all isn't "all clear", it's a server we've never
    heard of, so that comes back as one `not_set_up` problem (channel id 0,
    since there's no channel to point at).
    """

    def load() -> list[ChannelRequirement] | None:
        with closing(connect(db_path)) as conn:
            if repo.get_guild(conn, guild_id) is None:
                return None
            return requirements_from_db(conn, guild_id, game_names=game_names)

    requirements = await asyncio.to_thread(load)
    if requirements is None:
        text = "this server isn't set up yet (run /newsbot setup)"
        return GuildCheck(guild_id, (ChannelProblem(0, "server", "not_set_up", (), text),))
    return await check_guild_channels(client, guild_id, requirements)


def render_guild_permission_notice(problems: list[str]) -> str:
    """The text a server's admin channel and `/newsbot status` get. Empty if no problems."""
    if not problems:
        return ""
    lines = ["newsbot: I can't do everything I'm set up to do here:"]
    lines.extend(f"- {p}" for p in problems)
    return _truncate_utf16("\n".join(lines), _ALERT_LIMIT, suffix="…")


@dataclass(frozen=True)
class SweepResult:
    """What a startup sweep found, in counts only (the owner never sees channel names)."""

    checked: int = 0
    with_problems: int = 0
    notified: int = 0
    skipped: int = 0
    failed: int = 0


def render_sweep_counts(result: SweepResult) -> str:
    """The owner's one line, or an empty string when every checked server was clean.

    Servers whose check crashed or timed out (`failed`) count too: a sweep
    that's itself broken is exactly what the owner needs to hear about, and
    "0 problems" from a sweep that checked nothing is a lie of omission.
    """
    parts = []
    if result.with_problems:
        noun = "server" if result.checked == 1 else "servers"
        parts.append(f"permission problems in {result.with_problems} of {result.checked} {noun}")
    if result.failed:
        total = result.checked + result.failed
        noun = "server" if total == 1 else "servers"
        parts.append(f"permission check failed for {result.failed} of {total} {noun}")
    if not parts:
        return ""
    return "newsbot: " + "; ".join(parts)


async def sweep_guild_permissions(
    client: discord.Client,
    db_path: str | Path,
    notify_guild: Callable[[int, str], Awaitable[None]],
    *,
    game_names: Mapping[str, str] | None = None,
) -> SweepResult:
    """Check every set-up server; tell each one (only on change) and count for the owner.

    A server is told when its problem text differs from the one stored in
    `guilds.permission_problems`, so a redeploy doesn't re-post the same
    complaint. Coming back clean clears the stored text, so a recurrence is
    news again. A server the bot can't currently see (an outage, or it was
    kicked and the leave event hasn't been processed) is skipped, not
    flagged. One server's check crashing never stops the walk.
    """

    def load() -> list[GuildSettings]:
        with closing(connect(db_path)) as conn:
            return repo.list_set_up_guilds(conn)

    def store(guild_id: int, text: str | None) -> None:
        with closing(connect(db_path)) as conn:
            repo.update_guild_settings(conn, guild_id, permission_problems=text)

    checked = with_problems = notified = skipped = failed = 0
    for guild in await asyncio.to_thread(load):
        try:
            # One server's hung lookup shouldn't hold up the other 37.
            async with asyncio.timeout(_GUILD_TIMEOUT_S):
                if client.get_guild(guild.guild_id) is None:
                    skipped += 1
                    continue

                def reqs(gid: int = guild.guild_id) -> list[ChannelRequirement]:
                    with closing(connect(db_path)) as conn:
                        return requirements_from_db(conn, gid, game_names=game_names)

                result = await check_guild_channels(
                    client, guild.guild_id, await asyncio.to_thread(reqs)
                )
                checked += 1
                text = render_guild_permission_notice(result.lines()) or None
                if text is not None:
                    with_problems += 1
                if text != guild.permission_problems:
                    if text is not None:
                        await notify_guild(guild.guild_id, text)
                        notified += 1
                    await asyncio.to_thread(store, guild.guild_id, text)
        except Exception:  # includes TimeoutError from the guard above
            logger.exception(
                "permission sweep failed for a guild", extra={"guild_id": guild.guild_id}
            )
            failed += 1
    return SweepResult(checked, with_problems, notified, skipped, failed)


__all__ = [
    "ChannelProblem",
    "ChannelRequirement",
    "GuildCheck",
    "SweepResult",
    "check_guild",
    "check_guild_channels",
    "missing",
    "render_guild_permission_notice",
    "render_sweep_counts",
    "required_channels_for_guild",
    "requirements_from_db",
    "sendability_problem",
    "sweep_guild_permissions",
]
