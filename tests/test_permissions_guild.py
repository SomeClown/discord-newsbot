"""Per-guild permission checks (plan task 8, design.md §15).

The pure requirement builder gets plain tests; the live check and the
startup sweep get the same hand-written fakes the v2 permission tests use.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

import discord
import pytest

from newsbot.bot.permissions import (
    check_guild,
    check_guild_channels,
    render_guild_permission_notice,
    render_sweep_counts,
    required_channels_for_guild,
    requirements_from_db,
    sweep_guild_permissions,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import GuildGame, LoungeSettings, ShiftSettings

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
ALL = discord.Permissions.all()
GUILD = 111
OTHER = 222


def _guild(admin=None):
    return repo.GuildSettings(GUILD, "09:00", "UTC", admin, "free", True, T0, None, None, T0)


def _shift(ping="none", enabled=True, channel=50):
    return ShiftSettings(GUILD, enabled, channel, ping, None, None, 0)


def _lounge(channel=70):
    return LoungeSettings(GUILD, channel, True, "hi", True, "08:00", [], None)


def _by_id(reqs):
    return {r.channel_id: r for r in reqs}


# --- the pure requirement builder ---


def test_game_channels_need_view_send_embed():
    reqs = required_channels_for_guild(_guild(), [GuildGame(GUILD, "palworld", 10)], None, None)
    assert _by_id(reqs)[10].needed == {"view_channel", "send_messages", "embed_links"}


def test_game_names_replace_keys_in_the_purpose():
    reqs = required_channels_for_guild(
        _guild(),
        [GuildGame(GUILD, "palworld", 10)],
        None,
        None,
        game_names={"palworld": "Palworld"},
    )
    assert reqs[0].purpose == "Palworld"


def test_shift_none_ping_needs_only_view_and_send():
    reqs = required_channels_for_guild(_guild(), [], _shift("none"), None)
    req = _by_id(reqs)[50]
    assert req.needed == {"view_channel", "send_messages"}
    assert req.ping_role_id is None


def test_shift_everyone_ping_adds_mention_everyone():
    reqs = required_channels_for_guild(_guild(), [], _shift("everyone"), None)
    assert "mention_everyone" in _by_id(reqs)[50].needed


def test_shift_role_ping_carries_the_role_id_and_not_mention_everyone_yet():
    reqs = required_channels_for_guild(_guild(), [], _shift("987654321"), None)
    req = _by_id(reqs)[50]
    assert req.ping_role_id == 987654321
    assert "mention_everyone" not in req.needed


def test_disabled_shift_is_not_required():
    assert required_channels_for_guild(_guild(), [], _shift(enabled=False), None) == []


def test_admin_and_lounge_channels_need_view_and_send():
    reqs = required_channels_for_guild(_guild(admin=60), [], None, _lounge(70))
    by = _by_id(reqs)
    assert by[60].needed == {"view_channel", "send_messages"}
    assert by[70].needed == {"view_channel", "send_messages"}


def test_no_admin_no_lounge_no_extra_requirements():
    assert required_channels_for_guild(_guild(), [], None, None) == []


def test_shared_channel_merges_and_unions_needs():
    reqs = required_channels_for_guild(
        _guild(admin=10), [GuildGame(GUILD, "palworld", 10)], _shift("everyone", channel=10), None
    )
    assert len(reqs) == 1
    assert reqs[0].needed == {"view_channel", "send_messages", "embed_links", "mention_everyone"}


def test_requirements_from_db_reads_every_source(tmp_path):
    with closing(connect(tmp_path / "n.db")) as conn:
        migrate(conn)
        repo.create_guild(conn, GUILD, admin_channel_id=60, set_up=True)
        repo.follow_game(conn, GUILD, "palworld", 10)
        repo.upsert_lounge(conn, _lounge(70))
        assert {r.channel_id for r in requirements_from_db(conn, GUILD)} == {10, 60, 70}
        assert requirements_from_db(conn, 999) == []


# --- fakes ---


class FakeRole:
    def __init__(self, mentionable):
        self.mentionable = mentionable


class FakeGuild:
    def __init__(self, guild_id, roles=None, me="bot"):
        self.id = guild_id
        self.me = me
        self._roles = roles or {}

    def get_role(self, role_id):
        return self._roles.get(role_id)


def _text(guild, perms=ALL):
    class T(discord.TextChannel):
        def __init__(self):
            self.guild = guild

        def permissions_for(self, member):
            return perms

    return T()


def _thread(guild, perms=ALL):
    class T(discord.Thread):
        def __init__(self):
            self.guild = guild

        def permissions_for(self, member):
            return perms

    return T()


def _voice(guild):
    class V(discord.VoiceChannel):
        def __init__(self):
            self.guild = guild

        def permissions_for(self, member):
            return ALL

    return V()


def _forum(guild):
    class F(discord.ForumChannel):
        def __init__(self):
            self.guild = guild

        def permissions_for(self, member):
            return ALL

    return F()


class FakeClient:
    def __init__(self, channels, guilds=None, errors=None):
        self._channels = channels
        self._guilds = guilds if guilds is not None else {}
        self._errors = errors or {}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id in self._errors:
            raise self._errors[channel_id]
        raise discord.NotFound(_Resp(), "Unknown Channel")

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)


class _Resp:
    status = 404
    reason = "not found"
    headers: dict = {}
    request_info = None


def _req_for(channel_id, needed=("view_channel", "send_messages"), purpose="x", role=None):
    from newsbot.bot.permissions import ChannelRequirement

    return ChannelRequirement(channel_id, purpose, frozenset(needed), role)


# --- channel types ---


async def test_text_and_thread_channels_pass():
    g = FakeGuild(GUILD)
    client = FakeClient({1: _text(g), 2: _thread(g)})
    result = await check_guild_channels(client, GUILD, [_req_for(1), _req_for(2)])
    assert result.ok
    assert result.lines() == []


@pytest.mark.parametrize("maker", [_voice, _forum])
async def test_voice_and_forum_channels_are_not_text_channels(maker):
    client = FakeClient({1: maker(FakeGuild(GUILD))})
    result = await check_guild_channels(client, GUILD, [_req_for(1, purpose="Palworld")])
    assert [p.kind for p in result.problems] == ["not_text"]
    assert "Palworld channel <#1>: not a text channel" == result.lines()[0]


async def test_deleted_channel_is_not_found():
    result = await check_guild_channels(FakeClient({}), GUILD, [_req_for(1)])
    assert result.problems[0].kind == "not_found"


async def test_forbidden_fetch_is_not_found_too():
    err = discord.Forbidden(_Resp(), "Missing Access")
    result = await check_guild_channels(FakeClient({}, errors={1: err}), GUILD, [_req_for(1)])
    assert result.problems[0].kind == "not_found"


async def test_channel_in_another_guild_is_refused_without_reading_its_permissions():
    class Boom(discord.TextChannel):
        def __init__(self):
            self.guild = FakeGuild(OTHER)

        def permissions_for(self, member):
            raise AssertionError("must not read another guild's permissions")

    result = await check_guild_channels(FakeClient({1: Boom()}), GUILD, [_req_for(1)])
    assert result.problems[0].kind == "wrong_guild"


async def test_missing_permission_is_named_for_a_command_reply():
    perms = discord.Permissions.all()
    perms.send_messages = False
    client = FakeClient({1: _text(FakeGuild(GUILD), perms)})
    result = await check_guild_channels(client, GUILD, [_req_for(1, purpose="palworld-news")])
    (problem,) = result.problems
    assert problem.kind == "missing_permissions"
    assert problem.missing == ("Send Messages",)
    assert problem.text == "palworld-news in <#1>: missing Send Messages"


async def test_unknown_self_is_a_problem():
    client = FakeClient({1: _text(FakeGuild(GUILD, me=None))})
    result = await check_guild_channels(client, GUILD, [_req_for(1)])
    assert result.problems[0].kind == "unknown_self"


async def test_one_crashing_channel_does_not_hide_the_rest():
    class Crashy(discord.TextChannel):
        def __init__(self):
            self.guild = FakeGuild(GUILD)

        def permissions_for(self, member):
            raise RuntimeError("boom @everyone\nsecond line")

    g = FakeGuild(GUILD)
    perms = discord.Permissions.none()
    client = FakeClient({1: Crashy(), 2: _text(g, perms)})
    result = await check_guild_channels(client, GUILD, [_req_for(1), _req_for(2)])
    assert [p.kind for p in result.problems] == ["check_failed", "missing_permissions"]
    assert "@everyone" not in result.problems[0].text.replace("@​everyone", "")
    assert "\n" not in result.problems[0].text


# --- role pings ---


async def test_non_mentionable_role_needs_mention_everyone():
    perms = discord.Permissions.all()
    perms.mention_everyone = False
    g = FakeGuild(GUILD, roles={9: FakeRole(False)})
    result = await check_guild_channels(
        FakeClient({1: _text(g, perms)}), GUILD, [_req_for(1, role=9)]
    )
    assert result.problems[0].missing == ("Mention @everyone",)


async def test_mentionable_role_does_not_need_mention_everyone():
    perms = discord.Permissions.all()
    perms.mention_everyone = False
    g = FakeGuild(GUILD, roles={9: FakeRole(True)})
    result = await check_guild_channels(
        FakeClient({1: _text(g, perms)}), GUILD, [_req_for(1, role=9)]
    )
    assert result.ok


async def test_deleted_role_is_a_problem():
    g = FakeGuild(GUILD)
    result = await check_guild_channels(FakeClient({1: _text(g)}), GUILD, [_req_for(1, role=9)])
    assert result.problems[0].kind == "no_role"


# --- check_guild and the sweep ---


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "n.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _add_guild(db_path, guild_id, channels, *, admin=None, set_up=True):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, guild_id, admin_channel_id=admin, set_up=set_up)
        for i, channel in enumerate(channels):
            repo.follow_game(conn, guild_id, f"game{i}", channel)


async def test_check_guild_reads_settings_from_the_database(db_path):
    _add_guild(db_path, GUILD, [10, 11], admin=60)
    g = FakeGuild(GUILD)
    client = FakeClient({10: _text(g), 60: _text(g)})  # 11 is gone
    result = await check_guild(client, db_path, GUILD)
    assert [p.channel_id for p in result.problems] == [11]


async def test_sweep_routes_each_guild_its_own_problems_and_owner_gets_counts(db_path):
    _add_guild(db_path, 1, [10])  # fine
    _add_guild(db_path, 2, [20])  # channel deleted
    _add_guild(db_path, 3, [30], admin=31)  # admin channel in another guild
    _add_guild(db_path, 4, [40], set_up=False)  # not set up: not checked
    _add_guild(db_path, 5, [50])  # bot can't see the guild: skipped
    g1, g2, g3 = FakeGuild(1), FakeGuild(2), FakeGuild(3)
    client = FakeClient(
        {10: _text(g1), 30: _text(g3), 31: _text(FakeGuild(OTHER))},
        guilds={1: g1, 2: g2, 3: g3},
    )
    told: list[tuple[int, str]] = []

    async def notify(guild_id, text):
        told.append((guild_id, text))

    result = await sweep_guild_permissions(client, db_path, notify)
    assert (result.checked, result.with_problems, result.notified) == (3, 2, 2)
    assert result.skipped == 1
    assert {g for g, _ in told} == {2, 3}
    assert "<#20>" in dict(told)[2]
    assert "<#20>" not in dict(told)[3]
    assert render_sweep_counts(result) == "newsbot: permission problems in 2 of 3 servers"
    assert "<#" not in render_sweep_counts(result)


async def test_sweep_notifies_only_on_change_and_clears_when_fixed(db_path):
    _add_guild(db_path, 2, [20])
    g2 = FakeGuild(2)
    channels = {}
    client = FakeClient(channels, guilds={2: g2})
    told = []

    async def notify(guild_id, text):
        told.append(guild_id)

    await sweep_guild_permissions(client, db_path, notify)
    second = await sweep_guild_permissions(client, db_path, notify)
    assert told == [2]  # the repeat was quiet
    assert second.with_problems == 1 and second.notified == 0  # but still counted

    channels[20] = _text(g2)  # fixed
    third = await sweep_guild_permissions(client, db_path, notify)
    assert third.with_problems == 0 and render_sweep_counts(third) == ""
    with closing(connect(db_path)) as conn:
        assert repo.get_guild(conn, 2).permission_problems is None

    del channels[20]  # breaks again: news again
    await sweep_guild_permissions(client, db_path, notify)
    assert told == [2, 2]


async def test_sweep_survives_a_failing_notifier(db_path):
    _add_guild(db_path, 1, [10])
    _add_guild(db_path, 2, [20])
    client = FakeClient({}, guilds={1: FakeGuild(1), 2: FakeGuild(2)})
    calls = []

    async def notify(guild_id, text):
        calls.append(guild_id)
        if guild_id == 1:
            raise RuntimeError("nope")

    result = await sweep_guild_permissions(client, db_path, notify)
    assert calls == [1, 2]
    assert result.failed == 1 and result.notified == 1


async def test_sweep_over_no_guilds_is_quiet(db_path):
    result = await sweep_guild_permissions(FakeClient({}), db_path, lambda g, t: None)
    assert result.checked == 0 and render_sweep_counts(result) == ""


def test_notice_text_has_no_mentions_and_is_capped():
    text = render_guild_permission_notice(
        [f"channel <#{i}>: missing Send Messages" for i in range(300)]
    )
    assert len(text) <= 2000
    assert "@everyone" not in text
    assert render_guild_permission_notice([]) == ""


# --- who gets blamed in a shared channel ---


async def test_shared_channel_names_only_the_feature_that_needs_the_missing_permission():
    class Role:
        mentionable = False

    perms = discord.Permissions.all()
    perms.mention_everyone = False
    guild = FakeGuild(GUILD, roles={5: Role()})
    games = [GuildGame(GUILD, "rust", 10), GuildGame(GUILD, "fortnite", 10)]
    reqs = required_channels_for_guild(
        _guild(),
        games,
        _shift(ping="5", channel=10),
        None,
        game_names={"rust": "Rust", "fortnite": "Fortnite"},
    )
    result = await check_guild_channels(FakeClient({10: _text(guild, perms)}), GUILD, reqs)
    (problem,) = result.problems
    assert problem.text == "SHiFT codes in <#10>: missing Mention @everyone"
    assert problem.missing == ("Mention @everyone",)


async def test_shared_channel_blames_every_game_but_not_shift_for_missing_embed_links():
    perms = discord.Permissions.all()
    perms.embed_links = False
    games = [GuildGame(GUILD, "rust", 10), GuildGame(GUILD, "fortnite", 10)]
    reqs = required_channels_for_guild(
        _guild(),
        games,
        _shift(channel=10),
        None,
        game_names={"rust": "Rust", "fortnite": "Fortnite"},
    )
    result = await check_guild_channels(
        FakeClient({10: _text(FakeGuild(GUILD), perms)}), GUILD, reqs
    )
    (problem,) = result.problems
    assert problem.text == "Rust / Fortnite in <#10>: missing Embed Links"


async def test_shared_channel_with_two_different_shortfalls_gets_one_clause_each():
    perms = discord.Permissions.all()
    perms.embed_links = False
    perms.mention_everyone = False
    reqs = required_channels_for_guild(
        _guild(),
        [GuildGame(GUILD, "rust", 10)],
        _shift(ping="everyone", channel=10),
        None,
        game_names={"rust": "Rust"},
    )
    result = await check_guild_channels(
        FakeClient({10: _text(FakeGuild(GUILD), perms)}), GUILD, reqs
    )
    (problem,) = result.problems
    assert problem.text == (
        "Rust in <#10>: missing Embed Links; SHiFT codes in <#10>: missing Mention @everyone"
    )
    assert problem.missing == ("Embed Links", "Mention @everyone")
