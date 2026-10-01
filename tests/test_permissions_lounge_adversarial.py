"""Adversarial tests for the lounge half of the permission check (plan task 9).

The happy path (lounge channel needs View and Send, merges with admin) lives
in `test_permissions.py`. This file is the awkward seating chart: a lounge
that's also a game channel, the SHiFT channel and the admin channel at the
same time; a lounge id that points at nothing, at another guild, or at a
channel that can't hold a chat; and a bot that has half of what it needs, in
each of the two possible halves. After every one the questions are the same:
is there exactly one requirement per channel id, does it carry the union of
what everyone sharing it needs, and does the one alert line name every
purpose and every missing permission?

Where the check does something the owner might want different, the test says
so in a comment, so changing it later is a decision and not a surprise.

These were the v2 tests (ported at the cutover), written against a config (`required_channels` and
`check_channels`). The small `Setup` class below stands in for that config: it
holds the same facts (games, the admin channel, SHiFT, the lounge) and hands
them to the per-server builder, so each test body asks the same question it
always did of the code that exists now. One v2 case, a lounge with no channel
id, is gone: the table won't hold such a row.

Same hand-written fakes as `test_permissions.py` (copied, because tests here
aren't a package and I'm not going to make them one for four small classes).
No gateway, no network.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from newsbot.bot.permissions import check_guild_channels, required_channels_for_guild
from newsbot.store.models import GuildGame, GuildSettings, LoungeSettings, ShiftSettings

LOUNGE_ID = 555000000000000001
SHIFT_ID = 777000000000000001
ADMIN_ID = 123456789012345679
ALL = discord.Permissions.all()
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _topics():
    return [
        SimpleNamespace(key="borderlands4", name="Borderlands 4", channel_id=123456789012345690),
        SimpleNamespace(key="palworld", name="Palworld", channel_id=123456789012345691),
        SimpleNamespace(key="diablo4", name="Diablo IV", channel_id=123456789012345692),
    ]


@dataclass(frozen=True)
class Setup:
    """One server's settings, shaped like the v2 config these tests used to load."""

    guild_id: int = 123456789012345678
    topics: list = field(default_factory=_topics)
    admin_channel_id: int = ADMIN_ID
    shift_channel_id: int | None = None
    lounge: LoungeSettings | None = None


def _cfg(*, welcome=False, quote=False, channel_id=LOUNGE_ID, shift=False):
    lounge = LoungeSettings(
        123456789012345678, channel_id, welcome, "Hi {member}", quote, "08:00", [], None
    )
    return Setup(shift_channel_id=SHIFT_ID if shift else None, lounge=lounge)


def required_channels(setup: Setup):
    guild = GuildSettings(
        setup.guild_id, "09:00", "UTC", setup.admin_channel_id, "free", True, _NOW, None, None, _NOW
    )
    shift = (
        ShiftSettings(setup.guild_id, True, setup.shift_channel_id, "everyone", None, None, 0)
        if setup.shift_channel_id is not None
        else None
    )
    games = [GuildGame(setup.guild_id, t.key, t.channel_id) for t in setup.topics]
    return required_channels_for_guild(
        guild, games, shift, setup.lounge, game_names={t.key: t.name for t in setup.topics}
    )


async def check_channels(client, setup: Setup) -> list[str]:
    result = await check_guild_channels(client, setup.guild_id, required_channels(setup))
    return result.lines()


def _by_id(cfg):
    return {r.channel_id: r for r in required_channels(cfg)}


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.me = "bot-member"


def _perms(*, view=True, send=True, embed=True, mention=True) -> discord.Permissions:
    perms = discord.Permissions.none()
    perms.view_channel = view
    perms.send_messages = send
    perms.embed_links = embed
    perms.mention_everyone = mention
    return perms


class FakeTextChannel(discord.TextChannel):
    def __init__(self, guild, perms) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member):
        return self._perms


class FakeThread(discord.Thread):
    def __init__(self, guild, perms) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member):
        return self._perms


class FakeVoice(discord.VoiceChannel):
    def __init__(self, guild, perms) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member):
        return self._perms


class FakeStage(discord.StageChannel):
    def __init__(self, guild, perms) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member):
        return self._perms


class FakeForum(discord.ForumChannel):
    def __init__(self, guild, perms) -> None:
        self.guild = guild
        self._perms = perms

    def permissions_for(self, member):
        return self._perms


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"
        self.headers = {}
        self.request_info = None


class FakeClient:
    def __init__(self, channels, *, errors=None) -> None:
        self.channels = channels
        self.errors = errors or {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id in self.errors:
            raise self.errors[channel_id]
        if channel_id not in self.channels:
            raise discord.NotFound(_Resp(404), "Unknown Channel")
        return self.channels[channel_id]


def _clean(cfg) -> dict[int, object]:
    guild = FakeGuild(cfg.guild_id)
    channels = {t.channel_id: FakeTextChannel(guild, ALL) for t in cfg.topics}
    channels[cfg.admin_channel_id] = FakeTextChannel(guild, ALL)
    if cfg.shift_channel_id is not None:
        channels[cfg.shift_channel_id] = FakeTextChannel(guild, ALL)
    channels[LOUNGE_ID] = FakeTextChannel(guild, ALL)
    return channels


# --- shared ids: one requirement per channel, union of needs, labels combined ---


def test_lounge_sharing_a_topic_channel_adds_no_second_requirement():
    base = _cfg(quote=True)
    topic = base.topics[0]
    cfg = _cfg(quote=True, channel_id=topic.channel_id)

    reqs = required_channels(cfg)

    assert len([r for r in reqs if r.channel_id == topic.channel_id]) == 1
    req = _by_id(cfg)[topic.channel_id]
    assert req.purpose == f"{topic.name} / lounge"
    assert req.needed == frozenset({"view_channel", "send_messages", "embed_links"})


def test_lounge_sharing_the_shift_channel_keeps_mention_everyone():
    cfg = _cfg(welcome=True, quote=True, shift=True, channel_id=SHIFT_ID)

    req = _by_id(cfg)[SHIFT_ID]

    assert req.purpose == "SHiFT codes / lounge"
    assert req.needed == frozenset({"view_channel", "send_messages", "mention_everyone"})


def test_lounge_sharing_the_admin_channel_labels_admin_first():
    cfg = _cfg(welcome=True, channel_id=ADMIN_ID)

    req = _by_id(cfg)[cfg.admin_channel_id]

    assert req.purpose == "admin / lounge"


def test_lounge_sharing_a_channel_with_everyone_merges_all_the_labels_once():
    # Admin and SHiFT and a game channel all on the lounge's id: one line, all
    # four names, the union of needs. (Config validation might refuse this
    # seating; the permission check shouldn't care.)
    topic = _topics()[0]
    cfg = dataclasses.replace(
        _cfg(quote=True, channel_id=topic.channel_id),
        shift_channel_id=topic.channel_id,
        admin_channel_id=topic.channel_id,
    )

    reqs = [r for r in required_channels(cfg) if r.channel_id == topic.channel_id]

    assert len(reqs) == 1
    for label in (topic.name, "SHiFT codes", "admin", "lounge"):
        assert reqs[0].purpose.count(label) == 1
    assert reqs[0].needed == frozenset(
        {"view_channel", "send_messages", "embed_links", "mention_everyone"}
    )


@pytest.mark.parametrize(("welcome", "quote"), [(True, False), (False, True), (True, True)])
def test_any_lounge_feature_yields_the_same_single_requirement(welcome, quote):
    cfg = _cfg(welcome=welcome, quote=quote)

    reqs = [r for r in required_channels(cfg) if r.channel_id == LOUNGE_ID]

    assert [(r.purpose, r.needed) for r in reqs] == [
        ("lounge", frozenset({"view_channel", "send_messages"}))
    ]


async def test_shared_topic_lounge_missing_embed_links_names_only_the_topic():
    base = _cfg(quote=True)
    topic = base.topics[0]
    cfg = _cfg(quote=True, channel_id=topic.channel_id)
    channels = _clean(cfg)
    channels[topic.channel_id] = FakeTextChannel(FakeGuild(cfg.guild_id), _perms(embed=False))

    problems = await check_channels(FakeClient(channels), cfg)

    assert len(problems) == 1
    # The lounge only needs View and Send, so it isn't blamed for the missing Embed Links.
    assert topic.name in problems[0] and "lounge" not in problems[0]
    assert problems[0].endswith("missing Embed Links")


# --- the channel itself is wrong ---


async def test_lounge_pointing_at_a_deleted_channel_is_reported_like_any_other():
    cfg = _cfg(welcome=True)
    channels = _clean(cfg)
    del channels[LOUNGE_ID]

    problems = await check_channels(FakeClient(channels), cfg)

    assert problems == [f"lounge channel <#{LOUNGE_ID}>: not found or not visible to the bot"]


async def test_lounge_forbidden_fetch_reads_as_not_visible():
    cfg = _cfg(quote=True)
    channels = _clean(cfg)
    del channels[LOUNGE_ID]
    errors = {LOUNGE_ID: discord.Forbidden(_Resp(403), "Missing Access")}

    problems = await check_channels(FakeClient(channels, errors=errors), cfg)

    assert problems == [f"lounge channel <#{LOUNGE_ID}>: not found or not visible to the bot"]


async def test_lounge_in_another_guild_is_flagged():
    cfg = _cfg(quote=True)
    channels = _clean(cfg)
    channels[LOUNGE_ID] = FakeTextChannel(FakeGuild(cfg.guild_id + 1), ALL)

    problems = await check_channels(FakeClient(channels), cfg)

    assert problems == [f"lounge channel <#{LOUNGE_ID}>: not in the configured guild"]


async def test_lounge_as_a_thread_passes():
    # Documented: threads are accepted like every other destination. A thread
    # can be archived, which the check doesn't look at; a send to one would
    # fail later, with an admin alert, not here.
    cfg = _cfg(welcome=True)
    channels = _clean(cfg)
    channels[LOUNGE_ID] = FakeThread(FakeGuild(cfg.guild_id), ALL)

    assert await check_channels(FakeClient(channels), cfg) == []


@pytest.mark.parametrize("fake", [FakeVoice, FakeStage, FakeForum])
async def test_lounge_as_voice_stage_or_forum_is_flagged_not_a_text_channel(fake):
    cfg = _cfg(quote=True)
    channels = _clean(cfg)
    channels[LOUNGE_ID] = fake(FakeGuild(cfg.guild_id), ALL)

    problems = await check_channels(FakeClient(channels), cfg)

    assert problems == [f"lounge channel <#{LOUNGE_ID}>: not a text channel"]


# --- half of what it needs ---


@pytest.mark.parametrize(
    ("perms", "named"),
    [
        (_perms(view=True, send=False), "missing Send Messages"),
        (_perms(view=False, send=True), "missing View Channel"),
        (_perms(view=False, send=False), "missing View Channel, Send Messages"),
    ],
)
async def test_lounge_missing_view_or_send_is_named_exactly(perms, named):
    cfg = _cfg(welcome=True, quote=True)
    channels = _clean(cfg)
    channels[LOUNGE_ID] = FakeTextChannel(FakeGuild(cfg.guild_id), perms)

    problems = await check_channels(FakeClient(channels), cfg)

    assert problems == [f"lounge in <#{LOUNGE_ID}>: {named}"]


async def test_lounge_needs_no_embed_links_or_mention_everyone():
    cfg = _cfg(welcome=True, quote=True)
    channels = _clean(cfg)
    channels[LOUNGE_ID] = FakeTextChannel(
        FakeGuild(cfg.guild_id), _perms(embed=False, mention=False)
    )

    assert await check_channels(FakeClient(channels), cfg) == []


async def test_disabled_lounge_is_never_looked_up():
    cfg = _cfg()
    channels = _clean(cfg)
    del channels[LOUNGE_ID]

    assert await check_channels(FakeClient(channels), cfg) == []
