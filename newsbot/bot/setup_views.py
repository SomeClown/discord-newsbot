"""The `/newsbot setup` wizard: four picks and a Save button, all on one ephemeral message.

The wizard is one `discord.ui.View` holding its own draft of the settings (a time
zone, a digest time, up to ten games, a channel). Every select just updates the draft
and redraws the message; nothing touches the database until Save, and Save writes
the lot in one transaction (`repo.apply_guild_setup`). Cancel, a timeout and a
database error all leave the server exactly as it was, which is the whole point of
keeping a draft: a half-finished wizard should be as harmless as a half-finished
grocery list.

Save also notices when it's out of date. The wizard fingerprints the server's settings
when it opens; if the time, zone or followed games have moved by the time Save lands
(another admin, a `/newsbot follow`), it writes nothing and says so, rather than
cheerfully undoing someone else's afternoon.

Running it again starts from the server's current settings instead of a blank
form, so the wizard doubles as the "edit everything" screen. Games that already
post somewhere keep posting there unless the channel select is touched, in which
case every selected game moves to the new channel. (I went back and forth on
"touched" versus "changed to a different value". Picking the same channel on
purpose still means "put everything here", so touched wins.)

Everything a select sends is checked again on our side. A client is free to send a
time zone we never offered, eleven games, or a channel from a different server; the
options in the menu are a courtesy, not a guarantee.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import Counter
from collections.abc import Collection, Sequence
from contextlib import closing
from datetime import UTC, datetime

import discord

from newsbot.bot.commands import (
    _SERVER_ONLY,
    _next_due_line,
    channel_problem,
    has_admin_permission,
    parse_digest_time,
    resolve_timezone,
)
from newsbot.bot.format import _truncate_utf16, esc
from newsbot.bot.permissions import check_guild
from newsbot.bot.views import is_command_owner
from newsbot.config import AppConfig, GameInfo
from newsbot.store import repo
from newsbot.store.db import StoreError, connect
from newsbot.store.models import GuildGame, GuildSettings, Tier

logger = logging.getLogger(__name__)

_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
_MAX_CONTENT = 2000

# Offered in the zone menu, in this order: the same 24 the plan lists, which between
# them cover most of where a Discord server's members actually live. One more slot
# (Discord's cap is 25) goes to "Other".
CURATED_ZONES: tuple[str, ...] = (
    "UTC",
    "Pacific/Honolulu",
    "America/Anchorage",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Denver",
    "America/Chicago",
    "America/Mexico_City",
    "America/New_York",
    "America/Halifax",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Helsinki",
    "Europe/Moscow",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
)
OTHER_ZONE = "__other__"
HOUR_TIMES: tuple[str, ...] = tuple(f"{hour:02d}:00" for hour in range(24))

MSG_NOT_YOURS = "Only the admin who ran setup can use this."
MSG_FINISHED = "This setup is finished. Run /newsbot setup again to start a new one."
MSG_CANCELLED = "Setup cancelled. Nothing was saved."
MSG_TIMED_OUT = (
    "Setup timed out, so nothing was saved. Run /newsbot setup when you're ready to try again."
)
MSG_SAVE_FAILED = (
    "I couldn't save that, and nothing was changed. Press Save to try again, or Cancel."
)
MSG_TIMED_OUT_SAVE_LOST = (
    "Setup timed out and that last save didn't go through, so nothing was saved. "
    "Run /newsbot setup again."
)
MSG_BOT_GONE = "I'm not in this server anymore, so nothing was saved."
MSG_STALE = (
    "Someone changed this server's settings while setup was open, so I didn't save. "
    "Run /newsbot setup again to start from the current settings."
)
MSG_TOO_MANY_GAMES = (
    "That's more games than a server can follow, so nothing was saved. Run /newsbot setup again."
)
MSG_REFUSED = (
    "The server wouldn't accept those picks, so nothing was saved. Run /newsbot setup again."
)
MSG_BROKEN = "Something went wrong and nothing was saved. Run /newsbot setup to start over."

_FOLLOW_HINT = "Use /newsbot follow to give a game its own channel."


def _none_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


def most_used_channel(
    followed: Sequence[GuildGame], known: Collection[str] | None = None
) -> int | None:
    """The channel most followed games post to (the first one added wins a tie), or None.

    With `known` (the catalog's keys), games that have left the catalog don't get a vote:
    the wizard drops them on Save, so their channel shouldn't pick the default.
    """
    if known is not None:
        followed = [g for g in followed if g.game_key in known]
    if not followed:
        return None
    counts = Counter(g.channel_id for g in followed)
    best = max(counts.values())
    return next(g.channel_id for g in followed if counts[g.channel_id] == best)


def _save_setup_sync(
    db_path: str,
    guild_id: int,
    tier: Tier,
    *,
    digest_time: str,
    timezone: str,
    games: list[tuple[str, int]],
    expected_fingerprint: str,
) -> None:
    with closing(connect(db_path)) as conn:
        repo.apply_guild_setup(
            conn,
            guild_id,
            digest_time=digest_time,
            timezone=timezone,
            games=games,
            tier=tier,
            expected_fingerprint=expected_fingerprint,
        )


class SetupView(discord.ui.View):
    """The wizard. Build one per `/newsbot setup`, send it, and let it run itself.

    `guild` is the server's current row (None if it has none yet) and `followed` its
    games; both only seed the draft. `origin` is the slash-command interaction, kept
    so a timeout can edit the message after the last click.
    """

    def __init__(
        self,
        *,
        cfg: AppConfig,
        bot: discord.Client,
        db_path: str,
        owner_id: int,
        guild_id: int,
        tier: Tier,
        guild: GuildSettings | None,
        followed: Sequence[GuildGame],
        origin: discord.Interaction,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cfg = cfg
        self.bot = bot
        self.db_path = db_path
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.tier: Tier = tier
        self.origin = origin
        self.catalog: list[GameInfo] = list(cfg.catalog)
        self.names = {game.key: game.name for game in self.catalog}
        self.max_games = min(repo.MAX_GAMES_PER_GUILD, len(self.catalog))

        # The draft. Seeded from what the server has now, or from the defaults.
        self.previous_zone = guild.timezone if guild else "UTC"
        self.zone = self.previous_zone
        self.zone_is_other = False
        self.digest_time = guild.digest_time if guild else "09:00"
        self.prior_channels = {
            g.game_key: g.channel_id for g in followed if g.game_key in self.names
        }
        self.game_keys = [g.game_key for g in followed if g.game_key in self.names]
        self.channel_id: int | None = most_used_channel(followed, self.names)
        # What the server looked like when we opened; Save refuses if that has moved.
        self.fingerprint = repo.setup_fingerprint(guild, followed)
        self.channel_touched = False

        self.note = ""
        self.finished = False
        self.timed_out = False
        self._busy = False

        self.zone_select = self._build_zone_select()
        self.time_select = self._build_time_select()
        self.games_select = self._build_games_select()
        self.channel_select = self._build_channel_select()
        self.save_button = discord.ui.Button(label="Save", style=discord.ButtonStyle.success, row=4)
        self.save_button.callback = self._save_callback
        self.cancel_button = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary, row=4
        )
        self.cancel_button.callback = self._cancel_callback
        for item in (
            self.zone_select,
            self.time_select,
            self.games_select,
            self.channel_select,
            self.save_button,
            self.cancel_button,
        ):
            self.add_item(item)

    # --- building the menus ---

    def _build_zone_select(self) -> discord.ui.Select:
        select = discord.ui.Select(
            placeholder="Time zone", min_values=1, max_values=1, row=0, options=self._zone_options()
        )
        select.callback = self._zone_callback
        return select

    def _zone_options(self) -> list[discord.SelectOption]:
        on_list = self.zone in CURATED_ZONES and not self.zone_is_other
        options = [
            discord.SelectOption(label=zone, value=zone, default=on_list and zone == self.zone)
            for zone in CURATED_ZONES
        ]
        options.append(
            discord.SelectOption(
                label="Other (keeps your current zone)",
                value=OTHER_ZONE,
                description="Then use /newsbot settings timezone: to type one",
                default=not on_list,
            )
        )
        return options

    def _build_time_select(self) -> discord.ui.Select:
        select = discord.ui.Select(
            placeholder=_truncate_utf16(f"Digest time (now {self.digest_time})", 150),
            min_values=1,
            max_values=1,
            row=1,
            options=self._time_options(),
        )
        select.callback = self._time_callback
        return select

    def _time_options(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(label=t, value=t, default=t == self.digest_time)
            for t in HOUR_TIMES
        ]

    def _build_games_select(self) -> discord.ui.Select:
        select = discord.ui.Select(
            placeholder=f"Games to follow (up to {self.max_games})",
            min_values=1,
            max_values=self.max_games,
            row=2,
            options=self._game_options(),
        )
        select.callback = self._games_callback
        return select

    def _game_options(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(
                label=_truncate_utf16(game.name, 100, suffix="…"),
                value=game.key,
                default=game.key in self.game_keys,
            )
            for game in self.catalog
        ]

    def _channel_defaults(self) -> list[discord.SelectDefaultValue]:
        if self.channel_id is None:
            return []
        return [
            discord.SelectDefaultValue(
                id=self.channel_id, type=discord.SelectDefaultValueType.channel
            )
        ]

    def _build_channel_select(self) -> discord.ui.ChannelSelect:
        select = discord.ui.ChannelSelect(
            placeholder="Channel for the digests",
            channel_types=_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            row=3,
            default_values=self._channel_defaults(),
        )
        select.callback = self._channel_callback
        return select

    def _sync_components(self) -> None:
        """Push the draft back into the menus so the redrawn message shows what we hold."""
        self.zone_select.options = self._zone_options()
        self.time_select.options = self._time_options()
        self.games_select.options = self._game_options()
        self.channel_select.default_values = self._channel_defaults()

    # --- drawing the message ---

    def render(self) -> str:
        """The wizard's text: the current draft, then any note about the last click."""
        zone = esc(self.zone)
        if self.zone_is_other:
            zone += " (kept; see below)"
        if self.game_keys:
            picked = ", ".join(esc(self.names[k]) for k in self.game_keys)
            games = f"{picked} ({len(self.game_keys)} of {self.max_games})"
        else:
            games = "none picked yet"
        channel = f"<#{self.channel_id}>" if self.channel_id is not None else "none picked yet"
        lines = [
            "Setup: four picks and a Save. Nothing is saved until you press Save.",
            "",
            f"Time zone: {zone}",
            f"Digest time: {esc(self.digest_time)}",
            f"Games: {games}",
            f"Channel: {channel}",
        ]
        if self.note:
            lines += ["", self.note]
        return _truncate_utf16("\n".join(lines), _MAX_CONTENT, suffix="…")

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._sync_components()
        await interaction.response.edit_message(
            content=self.render(), view=self, allowed_mentions=_none_mentions()
        )

    # --- guards ---

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(_SERVER_ONLY, ephemeral=True)
            return False
        if not is_command_owner(interaction.user.id, self.owner_id):
            await interaction.response.send_message(MSG_NOT_YOURS, ephemeral=True)
            return False
        # The person who opened it could have lost Manage Server since.
        if not has_admin_permission(interaction.permissions, self.cfg.admin_permission):
            await interaction.response.send_message(MSG_NOT_YOURS, ephemeral=True)
            return False
        if self.finished or self._busy:
            await interaction.response.send_message(MSG_FINISHED, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self._busy:
            # discord.py has already stopped the view, so a failed save can't offer a
            # retry; on_save reads this flag and says so instead.
            self.timed_out = True
            return
        if self.finished:
            return
        self.finished = True
        try:
            await self.origin.edit_original_response(
                content=MSG_TIMED_OUT, view=None, allowed_mentions=_none_mentions()
            )
        except discord.HTTPException:
            # The message is gone, or the interaction token expired. Nothing was
            # saved either way, so there's nothing left to tell anyone.
            logger.info("setup timed out and the message couldn't be edited", exc_info=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        logger.exception("setup wizard error", exc_info=error)
        self._busy = False
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    MSG_BROKEN, ephemeral=True, allowed_mentions=_none_mentions()
                )
            else:
                await interaction.response.send_message(
                    MSG_BROKEN, ephemeral=True, allowed_mentions=_none_mentions()
                )
        except discord.HTTPException:
            logger.info("couldn't tell the admin the wizard broke", exc_info=True)

    # --- the handlers (testable without a select: they take the values) ---

    async def on_zone(self, interaction: discord.Interaction, values: Sequence[str]) -> None:
        value = values[0] if len(values) == 1 else None
        if value == OTHER_ZONE:
            self.zone = self.previous_zone
            self.zone_is_other = True
            self.note = (
                f"I'll keep the time zone this server has now ({esc(self.previous_zone)}). "
                "To type a different one, run /newsbot settings timezone: after you save."
            )
        elif value in CURATED_ZONES and resolve_timezone(value) == value:
            self.zone = value
            self.zone_is_other = False
            self.note = ""
        else:
            self.note = "I don't know that time zone. Pick one from the list."
        await self._redraw(interaction)

    async def on_time(self, interaction: discord.Interaction, values: Sequence[str]) -> None:
        value = values[0] if len(values) == 1 else None
        if value in HOUR_TIMES and parse_digest_time(value) is not None:
            self.digest_time = value
            self.note = ""
        else:
            self.note = "That isn't a time I offered. Pick one from the list."
        await self._redraw(interaction)

    async def on_games(self, interaction: discord.Interaction, values: Sequence[str]) -> None:
        chosen = list(dict.fromkeys(values))
        unknown = [key for key in chosen if key not in self.names]
        if unknown:
            self.note = "I don't know one of those games. Pick from the list."
        elif not chosen:
            self.note = "Pick at least one game."
        elif len(chosen) > self.max_games:
            self.note = (
                f"A server can follow at most {self.max_games} games here; "
                f"you picked {len(chosen)}. Your last valid pick is still in place."
            )
        else:
            # Catalog order, not click order, so the summary reads the same every time.
            self.game_keys = [g.key for g in self.catalog if g.key in chosen]
            self.note = ""
        await self._redraw(interaction)

    async def on_channel(self, interaction: discord.Interaction, values: Sequence[object]) -> None:
        if len(values) != 1:
            self.note = "Pick exactly one channel."
        else:
            problem = channel_problem(values[0], self.guild_id)
            if problem is not None:
                self.note = problem
            else:
                self.channel_id = values[0].id  # type: ignore[attr-defined]  # channel_problem vetted it
                self.channel_touched = True
                self.note = ""
        await self._redraw(interaction)

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content=MSG_CANCELLED, view=None, allowed_mentions=_none_mentions()
        )

    def final_games(self) -> list[tuple[str, int]]:
        """`(game, channel)` for every selected game.

        A game that already posts somewhere keeps that channel unless the channel menu
        was touched; new games (and every game, once it's touched) go to the pick.
        """
        assert self.channel_id is not None  # noqa: S101 (on_save checks before it gets here)
        games = []
        for key in self.game_keys:
            prior = self.prior_channels.get(key)
            keep = prior is not None and not self.channel_touched
            games.append((key, prior if keep else self.channel_id))  # type: ignore[arg-type]
        return games

    def incomplete(self) -> str | None:
        """What's still missing before Save can work, or None."""
        if not self.game_keys:
            return "Pick at least one game before you save."
        if self.channel_id is None:
            return "Pick a channel before you save."
        return None

    async def on_save(self, interaction: discord.Interaction) -> None:
        missing = self.incomplete()
        if missing is not None:
            self.note = missing
            await self._redraw(interaction)
            return
        self._busy = True
        # The permission check below can take a moment; acknowledge now, edit after.
        await interaction.response.defer()
        games = self.final_games()
        if self.bot.get_guild(self.guild_id) is None:
            # Removed since the wizard opened. The write would happily re-create the row.
            await self._end(interaction, MSG_BOT_GONE)
            return
        try:
            await asyncio.to_thread(
                _save_setup_sync,
                self.db_path,
                self.guild_id,
                self.tier,
                digest_time=self.digest_time,
                timezone=self.zone,
                games=games,
                expected_fingerprint=self.fingerprint,
            )
        except repo.StaleSetupError:
            await self._end(interaction, MSG_STALE)
            return
        except repo.GameLimitError:
            logger.exception("setup save hit the game limit", extra={"guild_id": self.guild_id})
            await self._end(interaction, MSG_TOO_MANY_GAMES)
            return
        except ValueError, sqlite3.IntegrityError:
            # Repeating the same picks can't work, so don't offer to.
            logger.exception("setup save was refused", extra={"guild_id": self.guild_id})
            await self._end(interaction, MSG_REFUSED)
            return
        except sqlite3.Error, StoreError:
            # The write is one transaction, so a failure means nothing changed.
            logger.exception("setup save failed", extra={"guild_id": self.guild_id})
            if self.timed_out:
                await self._end(interaction, MSG_TIMED_OUT_SAVE_LOST)
                return
            self._busy = False
            self.note = MSG_SAVE_FAILED
            await interaction.edit_original_response(
                content=self.render(), view=self, allowed_mentions=_none_mentions()
            )
            return
        self.finished = True
        self._busy = False
        self.stop()
        summary = await self._summary(games)
        try:
            await interaction.edit_original_response(
                content=summary, view=None, allowed_mentions=_none_mentions()
            )
        except discord.HTTPException:
            logger.warning("setup saved but the summary couldn't be shown", exc_info=True)

    async def _end(self, interaction: discord.Interaction, message: str) -> None:
        """Finish the wizard with `message`: nothing was saved and nothing more can be."""
        self.finished = True
        self._busy = False
        self.stop()
        try:
            await interaction.edit_original_response(
                content=message, view=None, allowed_mentions=_none_mentions()
            )
        except discord.HTTPException:
            logger.warning("setup ended but the message couldn't be shown", exc_info=True)

    async def _summary(self, games: list[tuple[str, int]]) -> str:
        """What got saved, what the bot still can't do, and where to go next."""
        head = ["Saved. This server is set up:", ""]
        head.append(f"- Time zone: {esc(self.zone)}")
        head.append(f"- Digest time: {esc(self.digest_time)}")
        for key, channel_id in games:
            head.append(f"- {esc(self.names[key])}: <#{channel_id}>")
        if self.zone_is_other:
            head.append(
                f"I kept the time zone ({esc(self.zone)}). "
                "To use another, run /newsbot settings timezone:."
            )
        head.append(self._next_line())

        tail: list[str] = []
        shift_games = [k for k, _ in games if k in set(self.cfg.shift.games)]
        if shift_games:
            named = ", ".join(esc(self.names[k]) for k in shift_games)
            tail.append(f"{named} has SHiFT codes; /newsbot shift turns on code alerts.")
        tail.append(_FOLLOW_HINT)

        try:
            check = await check_guild(self.bot, self.db_path, self.guild_id, game_names=self.names)
            problems = check.lines()
        except Exception:  # noqa: BLE001 (the save worked; a failed check must not hide that)
            logger.exception("setup permission check failed", extra={"guild_id": self.guild_id})
            problems = ["I couldn't check my permissions just now; /newsbot status will show them."]
        middle = ["", "All my permissions check out."] if not problems else []
        head_text = "\n".join(head)
        tail_text = "\n".join(tail)
        if problems:
            room = _MAX_CONTENT - len(head_text) - len(tail_text) - 8
            block = "But I can't do everything I'm set up to do:\n" + "\n".join(
                f"- {line}" for line in problems
            )
            middle = ["", _truncate_utf16(block, max(room, 0), suffix="…")]
        return _truncate_utf16(
            "\n".join([head_text, *middle, "", tail_text]), _MAX_CONTENT, suffix="…"
        )

    def _next_line(self) -> str:
        fake_row = GuildSettings(
            guild_id=self.guild_id,
            digest_time=self.digest_time,
            timezone=self.zone,
            admin_channel_id=None,
            tier=self.tier,
            set_up=True,
            joined_at=datetime.now(UTC),
            imported_at=None,
            permission_problems=None,
            updated_at=datetime.now(UTC),
        )
        return _next_due_line(fake_row, datetime.now(UTC))

    # --- wiring: each component's callback reads its own values and hands them over ---

    async def _zone_callback(self, interaction: discord.Interaction) -> None:
        await self.on_zone(interaction, self.zone_select.values)

    async def _time_callback(self, interaction: discord.Interaction) -> None:
        await self.on_time(interaction, self.time_select.values)

    async def _games_callback(self, interaction: discord.Interaction) -> None:
        await self.on_games(interaction, self.games_select.values)

    async def _channel_callback(self, interaction: discord.Interaction) -> None:
        await self.on_channel(interaction, self.channel_select.values)

    async def _save_callback(self, interaction: discord.Interaction) -> None:
        await self.on_save(interaction)

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        await self.on_cancel(interaction)


__all__ = [
    "CURATED_ZONES",
    "HOUR_TIMES",
    "OTHER_ZONE",
    "SetupView",
    "most_used_channel",
]
