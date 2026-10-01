"""Tests for the permission check (plan step 7, design.md §13), ported to the per-server version.

`required_channels_for_guild` and `missing` are plain functions, tested the
ordinary way. `check_guild_channels` is the one seam that talks to discord.py,
so it gets the same treatment `test_discord_publisher.py` gives
`DiscordPublisher`: tiny hand-written fakes standing in for a guild, a channel
and the client, rather than mocking discord.py's internals.

This file began as the v2 startup check's tests (`required_channels` and
`check_channels`, one server, read from config.yaml). At the cutover they moved
to the per-server builder, which reads the same facts from a server's rows:
every requirement, channel type, merge and failure-isolation assertion is
still here, asked of the same game, admin, SHiFT and lounge channels. The v2
"alerts.max_pings_per_day is 0" rule is now the server's ping choice being
`none`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from newsbot.bot.format import discord_len
from newsbot.bot.permissions import (
    ChannelRequirement,
    check_guild_channels,
    missing,
    render_guild_permission_notice,
    required_channels_for_guild,
)
from newsbot.store.models import GuildGame, GuildSettings, LoungeSettings, ShiftSettings

_GUILD_ID = 123456789012345678
_ALL = discord.Permissions.all()
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

_NAMES = {"borderlands4": "Borderlands 4", "palworld": "Palworld", "diablo4": "Diablo IV"}
_GAME_CHANNELS = (
    ("borderlands4", 123456789012345679),
    ("palworld", 123456789012345680),
    ("diablo4", 123456789012345681),
)
_ADMIN_ID = 123456789012345682
_SHIFT_ID = 999999999999999999
_LOUNGE_ID = 555000000000000001


def _reqs(
    *,
    shift_ping: str | None = None,
    shift_channel: int = _SHIFT_ID,
    lounge: LoungeSettings | None = None,
    admin: int | None = _ADMIN_ID,
    games=_GAME_CHANNELS,
) -> list[ChannelRequirement]:
    """What a server with three games and an admin channel (and maybe SHiFT, a lounge) needs."""
    guild = GuildSettings(_GUILD_ID, "09:00", "UTC", admin, "free", True, _NOW, None, None, _NOW)
    shift = (
        ShiftSettings(_GUILD_ID, True, shift_channel, shift_ping, None, None, 0)
        if shift_ping is not None
        else None
    )
    return required_channels_for_guild(
        guild,
        [GuildGame(_GUILD_ID, key, channel) for key, channel in games],
        shift,
        lounge,
        game_names=_NAMES,
    )


def _by_id(reqs):
    return {req.channel_id: req for req in reqs}


def _topic_channel_id() -> int:
    return _GAME_CHANNELS[0][1]


# --- required_channels_for_guild ---


def test_required_channels_covers_every_game():
    seen_ids = {req.channel_id for req in _reqs()}
    assert {channel for _key, channel in _GAME_CHANNELS} <= seen_ids


def test_game_requirement_needs_view_send_embed():
    reqs = _by_id(_reqs())
    assert reqs[_topic_channel_id()].needed == frozenset(
        {"view_channel", "send_messages", "embed_links"}
    )


def test_admin_channel_needs_view_and_send_only():
    reqs = _by_id(_reqs())
    assert reqs[_ADMIN_ID].needed == frozenset({"view_channel", "send_messages"})


def test_shift_channel_included_when_enabled():
    reqs = _by_id(_reqs(shift_ping="everyone"))
    assert reqs[_SHIFT_ID].needed == frozenset(
        {"view_channel", "send_messages", "mention_everyone"}
    )


def test_shift_channel_absent_when_disabled():
    reqs = _reqs()
    channel_ids = {req.channel_id for req in reqs}
    # No stray SHiFT requirement sneaks in when the server hasn't turned it on.
    assert all(req.needed != frozenset({"mention_everyone"}) for req in reqs)
    assert len(channel_ids) == len(_GAME_CHANNELS) + 1  # + admin channel, no SHiFT channel


def test_shift_channel_does_not_need_mention_everyone_when_pings_are_off():
    # v2: max_pings_per_day == 0 meant pinging was off on purpose, so there was no point
    # flagging a permission that would never get used. The v3 import turns that 0 into the
    # ping choice `none`, and the rule is the same.
    reqs = _by_id(_reqs(shift_ping="none"))
    assert reqs[_SHIFT_ID].needed == frozenset({"view_channel", "send_messages"})


def test_shift_channel_needs_mention_everyone_when_pings_are_on():
    reqs = _by_id(_reqs(shift_ping="everyone"))
    assert "mention_everyone" in reqs[_SHIFT_ID].needed


def test_shared_channel_ids_merge_needed_sets():
    shared_id = _topic_channel_id()
    merged = _by_id(_reqs(shift_ping="everyone", shift_channel=shared_id))[shared_id]
    assert merged.needed == frozenset(
        {"view_channel", "send_messages", "embed_links", "mention_everyone"}
    )


def test_no_duplicate_channel_requirements_when_channels_overlap():
    reqs = _reqs(shift_ping="everyone", shift_channel=_topic_channel_id())
    ids = [req.channel_id for req in reqs]
    assert len(ids) == len(set(ids))


# --- missing ---


def test_missing_returns_only_lacking_permissions_in_a_stable_order():
    perms = discord.Permissions(view_channel=True, send_messages=False, embed_links=False)
    needed = frozenset({"view_channel", "send_messages", "embed_links"})
    assert missing(perms, needed) == ["send_messages", "embed_links"]


def test_missing_returns_empty_when_everything_is_granted():
    needed = frozenset({"view_channel", "send_messages", "embed_links"})
    assert missing(_ALL, needed) == []


# --- check_guild_channels: fakes for the discord.py seam ---


class FakeGuild:
    def __init__(self, guild_id: int, me: object | None = "bot-member") -> None:
        self.id = guild_id
        self.me = me


class FakeTextChannel(discord.TextChannel):
    """A stand-in that satisfies `isinstance(channel, discord.TextChannel)`.

    `discord.TextChannel` can't be built without discord.py's full state
    machinery, so this skips `__init__` entirely and only sets the handful
    of attributes the check actually reads.
    """

    def __init__(self, guild: FakeGuild | None, perms: discord.Permissions) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member: object) -> discord.Permissions:
        return self._perms


class FakeNonTextChannel:
    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild


class FakeThread(discord.Thread):
    """A stand-in satisfying `isinstance(channel, discord.Thread)`."""

    def __init__(self, guild: FakeGuild | None, perms: discord.Permissions) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member: object) -> discord.Permissions:
        return self._perms


class FakeVoiceChannel(discord.VoiceChannel):
    """A stand-in satisfying `isinstance(channel, discord.VoiceChannel)`."""

    def __init__(self, guild: FakeGuild | None, perms: discord.Permissions) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member: object) -> discord.Permissions:
        return self._perms


class FakeClient:
    def __init__(
        self,
        channels: dict[int, object],
        *,
        fetch_errors: dict[int, Exception] | None = None,
    ) -> None:
        self._channels = channels
        self._fetch_errors = fetch_errors or {}

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        if channel_id in self._fetch_errors:
            raise self._fetch_errors[channel_id]
        if channel_id not in self._channels:
            raise discord.NotFound(_fake_response(404), "Unknown Channel")
        return self._channels[channel_id]


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers = {}
        self.request_info = None


def _fake_response(status: int):
    return _FakeResponse(status)


def _clean_channels(cfg) -> dict[int, object]:
    channels = {t.channel_id: FakeTextChannel(FakeGuild(_GUILD_ID), _ALL) for t in cfg.topics}
    channels[cfg.admin_channel_id] = FakeTextChannel(FakeGuild(_GUILD_ID), _ALL)
    return channels


def _clean_channels(reqs=None) -> dict[int, object]:
    return {
        req.channel_id: FakeTextChannel(FakeGuild(_GUILD_ID), _ALL)
        for req in (reqs if reqs is not None else _reqs())
    }


async def _problems(client, reqs=None) -> list[str]:
    result = await check_guild_channels(client, _GUILD_ID, reqs if reqs is not None else _reqs())
    return result.lines()


async def test_check_clean_server_has_no_problems():
    client = FakeClient(_clean_channels())

    assert await _problems(client) == []


async def test_check_missing_permission_is_named_human_readable():
    channels = _clean_channels()
    missing_embed_perms = discord.Permissions.all()
    missing_embed_perms.embed_links = False
    channels[_topic_channel_id()] = FakeTextChannel(FakeGuild(_GUILD_ID), missing_embed_perms)
    client = FakeClient(channels)

    problems = await _problems(client)
    assert len(problems) == 1
    assert "Embed Links" in problems[0]
    assert str(_topic_channel_id()) in problems[0]


async def test_check_channel_not_found():
    channels = _clean_channels()
    del channels[_topic_channel_id()]
    client = FakeClient(channels)

    problems = await _problems(client)
    assert len(problems) == 1
    assert "not found or not visible to the bot" in problems[0]


async def test_check_wrong_guild():
    channels = _clean_channels()
    channels[_topic_channel_id()] = FakeTextChannel(FakeGuild(999), _ALL)
    client = FakeClient(channels)

    problems = await _problems(client)
    assert len(problems) == 1
    # v2 said "not in the configured guild"; the wording survives because the line is the
    # same one `check_channels` produced.
    assert "not in the configured guild" in problems[0]


async def test_check_not_a_text_channel():
    channels = _clean_channels()
    channels[_topic_channel_id()] = FakeNonTextChannel(FakeGuild(_GUILD_ID))
    client = FakeClient(channels)

    problems = await _problems(client)
    assert len(problems) == 1
    assert "not a text channel" in problems[0]


async def test_check_accepts_a_thread():
    channels = _clean_channels()
    channels[_topic_channel_id()] = FakeThread(FakeGuild(_GUILD_ID), _ALL)
    client = FakeClient(channels)

    assert await _problems(client) == []


async def test_check_still_flags_a_voice_channel():
    # VoiceChannel is technically `Messageable` in discord.py these days,
    # but it's not what an owner means by "post the digest here": it
    # stays flagged even though it'd otherwise pass the sendable check.
    channels = _clean_channels()
    channels[_topic_channel_id()] = FakeVoiceChannel(FakeGuild(_GUILD_ID), _ALL)
    client = FakeClient(channels)

    problems = await _problems(client)
    assert len(problems) == 1
    assert "not a text channel" in problems[0]


async def test_check_uses_cache_before_fetching():
    channels = _clean_channels()
    client = FakeClient(channels)
    # fetch_channel would raise for everything: get_channel should be
    # tried first and succeed, so fetch_channel is never called.
    client._fetch_errors = {cid: RuntimeError("should not be called") for cid in channels}

    assert await _problems(client) == []


async def test_check_one_exception_does_not_stop_the_rest():
    channels = _clean_channels()
    broken = _topic_channel_id()
    del channels[broken]
    client = FakeClient(channels, fetch_errors={broken: RuntimeError("boom")})

    problems = await _problems(client)
    # The broken channel produced a problem, but everything else still
    # came back clean rather than the whole check blowing up.
    assert len(problems) == 1


# --- render_guild_permission_notice (v2's render_permission_alert) ---


def test_render_permission_notice_stays_under_the_2000_unit_cap():
    problems = [
        f"channel <#{i}>: missing View Channel, Send Messages, Embed Links" for i in range(200)
    ]
    text = render_guild_permission_notice(problems)
    assert discord_len(text) <= 2000


def test_render_permission_notice_lists_every_problem_when_short():
    problems = ["topic channel <#1>: missing Embed Links"]
    text = render_guild_permission_notice(problems)
    assert "missing Embed Links" in text


def test_render_permission_notice_empty_is_empty_string():
    assert render_guild_permission_notice([]) == ""


def test_channel_requirement_is_a_plain_dataclass_shape():
    req = ChannelRequirement(
        channel_id=1, purpose="Borderlands 4", needed=frozenset({"view_channel"})
    )
    assert req.channel_id == 1
    assert req.purpose == "Borderlands 4"
    assert req.needed == frozenset({"view_channel"})


# --- lounge channel (design.md §14) ---


def _lounge(*, welcome: bool = False, quote: bool = False, channel_id: int = _LOUNGE_ID):
    return LoungeSettings(_GUILD_ID, channel_id, welcome, "Hi {member}", quote, "08:00", [], None)


def _lounge_reqs(lounge) -> list[ChannelRequirement]:
    return [r for r in _reqs(lounge=lounge) if r.channel_id == _LOUNGE_ID]


def test_lounge_requirement_present_when_welcome_on():
    (req,) = _lounge_reqs(_lounge(welcome=True))
    assert req.purpose == "lounge"
    assert req.needed == frozenset({"view_channel", "send_messages"})


def test_lounge_requirement_present_when_quote_on():
    (req,) = _lounge_reqs(_lounge(quote=True))
    assert req.needed == frozenset({"view_channel", "send_messages"})


def test_lounge_requirement_absent_when_both_off():
    assert _lounge_reqs(_lounge()) == []


# (v2 also pinned "no lounge requirement without a channel id". A lounge row can't exist without
# one now (the table says NOT NULL), so that case has no v3 meaning.)


def test_lounge_requirement_merges_with_admin_channel_on_shared_id():
    reqs = _reqs(lounge=_lounge(quote=True, channel_id=_ADMIN_ID))
    matches = [r for r in reqs if r.channel_id == _ADMIN_ID]
    assert len(matches) == 1
    assert matches[0].purpose == "admin / lounge"
    assert matches[0].needed == frozenset({"view_channel", "send_messages"})


async def test_permission_notice_names_the_lounge_when_send_messages_is_missing():
    reqs = _reqs(lounge=_lounge(welcome=True))
    channels = _clean_channels(reqs)
    no_send = discord.Permissions.all()
    no_send.send_messages = False
    channels[_LOUNGE_ID] = FakeTextChannel(FakeGuild(_GUILD_ID), no_send)

    problems = await _problems(FakeClient(channels), reqs)

    assert len(problems) == 1
    assert "lounge" in problems[0]
    assert "Send Messages" in problems[0]
    assert "lounge" in render_guild_permission_notice(problems)
