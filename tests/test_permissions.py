"""Tests for the startup permission check (plan step 7, design.md §13).

`required_channels` and `missing` are plain functions, tested the ordinary
way. `check_channels` is the one seam that talks to discord.py, so it gets
the same treatment `test_discord_publisher.py` gives `DiscordPublisher`:
tiny hand-written fakes standing in for a guild, a channel, and the client,
rather than mocking discord.py's internals.
"""

from __future__ import annotations

from pathlib import Path

import discord

from newsbot.bot.format import discord_len
from newsbot.bot.permissions import (
    ChannelRequirement,
    check_channels,
    missing,
    render_permission_alert,
    required_channels,
)
from newsbot.config import load_config

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"

_GUILD_ID = 123456789012345678
_ALL = discord.Permissions.all()


def _cfg(
    *,
    alerts_enabled: bool = False,
    alerts_channel_id: int | None = None,
    max_pings_per_day: int | None = None,
):
    cfg = load_config(CONFIG_PATH)
    updates = {"enabled": alerts_enabled, "channel_id": alerts_channel_id}
    if max_pings_per_day is not None:
        updates["max_pings_per_day"] = max_pings_per_day
    alerts = cfg.alerts.model_copy(update=updates)
    return cfg.model_copy(update={"alerts": alerts})


def _topic_channel_id() -> int:
    return load_config(CONFIG_PATH).topics[0].channel_id


# --- required_channels ---


def test_required_channels_covers_every_topic():
    cfg = _cfg()
    reqs = required_channels(cfg)
    topic_ids = {topic.channel_id for topic in cfg.topics}
    seen_ids = {req.channel_id for req in reqs}
    assert topic_ids <= seen_ids


def test_topic_requirement_needs_view_send_embed():
    cfg = _cfg()
    reqs = {req.channel_id: req for req in required_channels(cfg)}
    topic = cfg.topics[0]
    assert reqs[topic.channel_id].needed == frozenset(
        {"view_channel", "send_messages", "embed_links"}
    )


def test_admin_channel_needs_view_and_send_only():
    cfg = _cfg()
    reqs = {req.channel_id: req for req in required_channels(cfg)}
    assert reqs[cfg.admin_channel_id].needed == frozenset({"view_channel", "send_messages"})


def test_alerts_channel_included_when_enabled():
    cfg = _cfg(alerts_enabled=True, alerts_channel_id=999999999999999999)
    reqs = {req.channel_id: req for req in required_channels(cfg)}
    assert reqs[999999999999999999].needed == frozenset(
        {"view_channel", "send_messages", "mention_everyone"}
    )


def test_alerts_channel_absent_when_disabled():
    cfg = _cfg(alerts_enabled=False)
    reqs = required_channels(cfg)
    channel_ids = {req.channel_id for req in reqs}
    # No stray alerts requirement sneaks in when there's no alerts channel
    # configured at all: alerts.channel_id is None here.
    assert all(req.needed != frozenset({"mention_everyone"}) for req in reqs)
    assert len(channel_ids) == len(cfg.topics) + 1  # + admin channel, no alerts channel


def test_alerts_channel_does_not_need_mention_everyone_when_pings_are_off():
    # max_pings_per_day == 0 means pinging is off on purpose (same rule
    # sweep.py uses to suppress the "cap reached" alert): there's no
    # point flagging a permission that will never actually get used.
    cfg = _cfg(alerts_enabled=True, alerts_channel_id=999999999999999999, max_pings_per_day=0)
    reqs = {req.channel_id: req for req in required_channels(cfg)}
    assert reqs[999999999999999999].needed == frozenset({"view_channel", "send_messages"})


def test_alerts_channel_needs_mention_everyone_when_pings_are_on():
    cfg = _cfg(alerts_enabled=True, alerts_channel_id=999999999999999999, max_pings_per_day=1)
    reqs = {req.channel_id: req for req in required_channels(cfg)}
    assert "mention_everyone" in reqs[999999999999999999].needed


def test_shared_channel_ids_merge_needed_sets():
    shared_id = _topic_channel_id()
    cfg = _cfg(alerts_enabled=True, alerts_channel_id=shared_id)
    reqs = {req.channel_id: req for req in required_channels(cfg)}
    merged = reqs[shared_id]
    assert merged.needed == frozenset(
        {"view_channel", "send_messages", "embed_links", "mention_everyone"}
    )


def test_no_duplicate_channel_requirements_when_channels_overlap():
    cfg = _cfg(alerts_enabled=True, alerts_channel_id=_topic_channel_id())
    reqs = required_channels(cfg)
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


# --- check_channels: fakes for the discord.py seam ---


class FakeGuild:
    def __init__(self, guild_id: int, me: object | None = "bot-member") -> None:
        self.id = guild_id
        self.me = me


class FakeTextChannel(discord.TextChannel):
    """A stand-in that satisfies `isinstance(channel, discord.TextChannel)`.

    `discord.TextChannel` can't be built without discord.py's full state
    machinery, so this skips `__init__` entirely and only sets the handful
    of attributes `check_channels` actually reads.
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


async def test_check_channels_clean_config_has_no_problems():
    cfg = _cfg()
    client = FakeClient(_clean_channels(cfg))

    problems = await check_channels(client, cfg)
    assert problems == []


async def test_check_channels_missing_permission_is_named_human_readable():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    topic = cfg.topics[0]
    missing_embed_perms = discord.Permissions.all()
    missing_embed_perms.embed_links = False
    channels[topic.channel_id] = FakeTextChannel(FakeGuild(_GUILD_ID), missing_embed_perms)
    client = FakeClient(channels)

    problems = await check_channels(client, cfg)
    assert len(problems) == 1
    assert "Embed Links" in problems[0]
    assert str(topic.channel_id) in problems[0]


async def test_check_channels_channel_not_found():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    missing_topic = cfg.topics[0]
    del channels[missing_topic.channel_id]
    client = FakeClient(channels)

    problems = await check_channels(client, cfg)
    assert len(problems) == 1
    assert "not found or not visible to the bot" in problems[0]


async def test_check_channels_wrong_guild():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    wrong_topic = cfg.topics[0]
    channels[wrong_topic.channel_id] = FakeTextChannel(FakeGuild(999), _ALL)
    client = FakeClient(channels)

    problems = await check_channels(client, cfg)
    assert len(problems) == 1
    assert "not in the configured guild" in problems[0]


async def test_check_channels_not_a_text_channel():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    voice_topic = cfg.topics[0]
    channels[voice_topic.channel_id] = FakeNonTextChannel(FakeGuild(_GUILD_ID))
    client = FakeClient(channels)

    problems = await check_channels(client, cfg)
    assert len(problems) == 1
    assert "not a text channel" in problems[0]


async def test_check_channels_accepts_a_thread():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    thread_topic = cfg.topics[0]
    channels[thread_topic.channel_id] = FakeThread(FakeGuild(_GUILD_ID), _ALL)
    client = FakeClient(channels)

    problems = await check_channels(client, cfg)
    assert problems == []


async def test_check_channels_still_flags_a_voice_channel():
    # VoiceChannel is technically `Messageable` in discord.py these days,
    # but it's not what an owner means by "post the digest here": it
    # stays flagged even though it'd otherwise pass the sendable check.
    cfg = _cfg()
    channels = _clean_channels(cfg)
    voice_topic = cfg.topics[0]
    channels[voice_topic.channel_id] = FakeVoiceChannel(FakeGuild(_GUILD_ID), _ALL)
    client = FakeClient(channels)

    problems = await check_channels(client, cfg)
    assert len(problems) == 1
    assert "not a text channel" in problems[0]


async def test_check_channels_uses_cache_before_fetching():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    client = FakeClient(channels)
    # fetch_channel would raise for everything: get_channel should be
    # tried first and succeed, so fetch_channel is never called.
    client._fetch_errors = {cid: RuntimeError("should not be called") for cid in channels}

    problems = await check_channels(client, cfg)
    assert problems == []


async def test_check_channels_one_exception_does_not_stop_the_rest():
    cfg = _cfg()
    channels = _clean_channels(cfg)
    broken_topic = cfg.topics[0]
    del channels[broken_topic.channel_id]
    client = FakeClient(channels, fetch_errors={broken_topic.channel_id: RuntimeError("boom")})

    problems = await check_channels(client, cfg)
    # The broken channel produced a problem, but everything else still
    # came back clean rather than the whole check blowing up.
    assert len(problems) == 1


# --- render_permission_alert ---


def test_render_permission_alert_stays_under_the_2000_unit_cap():
    problems = [
        f"channel <#{i}>: missing View Channel, Send Messages, Embed Links" for i in range(200)
    ]
    text = render_permission_alert(problems)
    assert discord_len(text) <= 2000


def test_render_permission_alert_lists_every_problem_when_short():
    problems = ["topic channel <#1>: missing Embed Links"]
    text = render_permission_alert(problems)
    assert "missing Embed Links" in text


def test_render_permission_alert_empty_is_empty_string():
    assert render_permission_alert([]) == ""


def test_channel_requirement_is_a_plain_dataclass_shape():
    req = ChannelRequirement(
        channel_id=1, purpose="Borderlands 4", needed=frozenset({"view_channel"})
    )
    assert req.channel_id == 1
    assert req.purpose == "Borderlands 4"
    assert req.needed == frozenset({"view_channel"})


# --- lounge channel (design.md §14) ---

_LOUNGE_ID = 555000000000000001


def _lounge_cfg(*, welcome: bool = False, quote: bool = False, channel_id: int | None = _LOUNGE_ID):
    from newsbot.config import DailyQuoteCfg, LoungeCfg, QuoteSourceCfg, WelcomeCfg

    cfg = _cfg()
    lounge = LoungeCfg(
        channel_id=channel_id,
        welcome=WelcomeCfg(enabled=welcome, message="Hi {member}"),
        daily_quote=DailyQuoteCfg(
            enabled=quote, sources=[QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")]
        ),
    )
    return cfg.model_copy(update={"lounge": lounge})


def _lounge_reqs(cfg) -> list[ChannelRequirement]:
    return [r for r in required_channels(cfg) if r.channel_id == _LOUNGE_ID]


def test_lounge_requirement_present_when_welcome_on():
    (req,) = _lounge_reqs(_lounge_cfg(welcome=True))
    assert req.purpose == "lounge"
    assert req.needed == frozenset({"view_channel", "send_messages"})


def test_lounge_requirement_present_when_quote_on():
    (req,) = _lounge_reqs(_lounge_cfg(quote=True))
    assert req.needed == frozenset({"view_channel", "send_messages"})


def test_lounge_requirement_absent_when_both_off():
    assert _lounge_reqs(_lounge_cfg()) == []


def test_lounge_requirement_absent_without_a_channel_id():
    cfg = _lounge_cfg(welcome=True, channel_id=None)
    assert {r.purpose for r in required_channels(cfg)}.isdisjoint({"lounge"})


def test_lounge_requirement_merges_with_admin_channel_on_shared_id():
    cfg = _lounge_cfg(quote=True, channel_id=_cfg().admin_channel_id)
    matches = [r for r in required_channels(cfg) if r.channel_id == cfg.admin_channel_id]
    assert len(matches) == 1
    assert matches[0].purpose == "admin / lounge"
    assert matches[0].needed == frozenset({"view_channel", "send_messages"})


async def test_permission_alert_names_the_lounge_when_send_messages_is_missing():
    cfg = _lounge_cfg(welcome=True)
    channels = _clean_channels(cfg)
    no_send = discord.Permissions.all()
    no_send.send_messages = False
    channels[_LOUNGE_ID] = FakeTextChannel(FakeGuild(_GUILD_ID), no_send)

    problems = await check_channels(FakeClient(channels), cfg)

    assert len(problems) == 1
    assert "lounge" in problems[0]
    assert "Send Messages" in problems[0]
    assert "lounge" in render_permission_alert(problems)
