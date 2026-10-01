"""Breaking the per-server permission check and its startup sweep.

The check has one job: look at what a server told the bot to use and say, in
a sentence, what won't work. The sweep has another: do that for every server
at startup without telling anybody twice, without telling anybody the wrong
thing, and without one server's bad day taking the walk down. These tests
throw every channel type at the first, and five hundred servers (some of
which crash, some of which flip-flop) at the second. The places where the
code did something I'd call a bug used to be strict xfails; they're fixed,
and their markers are gone.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import closing
from datetime import UTC, datetime

import discord
import pytest

from newsbot.bot import permissions
from newsbot.bot.permissions import (
    ChannelRequirement,
    SweepResult,
    check_guild,
    check_guild_channels,
    render_guild_permission_notice,
    render_sweep_counts,
    required_channels_for_guild,
    sweep_guild_permissions,
)
from newsbot.guilds.notify import Router, client_sender
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import GuildGame, GuildSettings, LoungeSettings, ShiftSettings

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
ALL = discord.Permissions.all()
GUILD = 111
OTHER = 222
OWNER_CHANNEL = 9000


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


def _chan(kind, guild, perms=ALL):
    """A channel of discord.py class `kind` that records sends and can't be built for real."""

    class C(kind):
        def __init__(self):
            self.guild = guild
            self.sent = []

        def permissions_for(self, member):
            return perms

        async def send(self, text, **kwargs):
            self.sent.append((text, kwargs))

    return C()


class _Resp:
    status = 404
    reason = "not found"
    headers: dict = {}
    request_info = None


class FakeClient:
    def __init__(self, channels=None, guilds=None, raises=None, hang=()):
        self.channels = channels if channels is not None else {}
        self.guilds = guilds if guilds is not None else {}
        self.raises = raises or {}
        self.hang = set(hang)

    def get_channel(self, channel_id):
        if channel_id in self.raises:
            raise self.raises[channel_id]
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id in self.hang:
            await asyncio.sleep(3600)
        raise discord.NotFound(_Resp(), "Unknown Channel")

    def get_guild(self, guild_id):
        value = self.guilds.get(guild_id)
        if isinstance(value, Exception):
            raise value
        return value


def _req(channel_id, needed=("view_channel", "send_messages"), purpose="x", role=None):
    return ChannelRequirement(channel_id, purpose, frozenset(needed), role)


async def _kind(client, req, guild_id=GUILD):
    result = await check_guild_channels(client, guild_id, [req])
    return [p.kind for p in result.problems]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "n.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _add_guild(db_path, guild_id, channels=(), *, admin=None, set_up=True):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, guild_id, admin_channel_id=admin, set_up=set_up)
        for i, channel in enumerate(channels):
            repo.follow_game(conn, guild_id, f"game{i}", channel)


def _stored_problems(db_path, guild_id):
    with closing(connect(db_path)) as conn:
        return repo.get_guild(conn, guild_id).permission_problems


# --- every channel kind ---


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (discord.TextChannel, []),  # announcement channels are TextChannels too
        (discord.Thread, []),  # public or private, same class, permissions come from the parent
        (discord.VoiceChannel, ["not_text"]),
        (discord.StageChannel, ["not_text"]),
        (discord.ForumChannel, ["not_text"]),
        (discord.CategoryChannel, ["not_text"]),
    ],
    ids=["text", "thread", "voice", "stage", "forum", "category"],
)
async def test_every_channel_kind_gets_its_verdict(kind, expected):
    client = FakeClient({10: _chan(kind, FakeGuild(GUILD))})
    assert await _kind(client, _req(10)) == expected


async def test_a_channel_with_no_guild_attribute_is_wrong_guild():
    class DMish:
        pass

    assert await _kind(FakeClient({10: DMish()}), _req(10)) == ["wrong_guild"]


async def test_a_channel_in_another_guild_never_has_its_permissions_read():
    class Spyglass(discord.TextChannel):
        def __init__(self):
            self.guild = FakeGuild(OTHER)

        def permissions_for(self, member):
            raise AssertionError("read another guild's permissions")

    assert await _kind(FakeClient({10: Spyglass()}), _req(10)) == ["wrong_guild"]


async def test_a_channel_whose_guild_attribute_is_none_is_wrong_guild():
    assert await _kind(FakeClient({10: _chan(discord.TextChannel, None)}), _req(10)) == [
        "wrong_guild"
    ]


# --- permissions ---


async def test_a_role_overwrite_that_denies_send_messages_is_named():
    perms = discord.Permissions(view_channel=True, send_messages=False, embed_links=True)
    client = FakeClient({10: _chan(discord.TextChannel, FakeGuild(GUILD), perms)})
    result = await check_guild_channels(
        client, GUILD, [_req(10, ("view_channel", "send_messages", "embed_links"))]
    )
    (problem,) = result.problems
    assert (problem.kind, problem.missing) == ("missing_permissions", ("Send Messages",))
    assert problem.text == "x channel <#10>: missing Send Messages"


async def test_no_permissions_at_all_lists_every_one_in_the_fixed_order():
    client = FakeClient(
        {10: _chan(discord.TextChannel, FakeGuild(GUILD), discord.Permissions.none())}
    )
    req = _req(10, ("embed_links", "mention_everyone", "send_messages", "view_channel"))
    (problem,) = (await check_guild_channels(client, GUILD, [req])).problems
    assert problem.missing == ("View Channel", "Send Messages", "Embed Links", "Mention @everyone")


async def test_the_bot_missing_from_the_cache_is_unknown_self_not_a_crash():
    client = FakeClient({10: _chan(discord.TextChannel, FakeGuild(GUILD, me=None))})
    assert await _kind(client, _req(10)) == ["unknown_self"]


async def test_role_ping_with_a_deleted_role_is_no_role_and_skips_the_permission_read():
    guild = FakeGuild(GUILD, roles={})
    client = FakeClient({10: _chan(discord.TextChannel, guild, discord.Permissions.none())})
    (problem,) = (await check_guild_channels(client, GUILD, [_req(10, role=555)])).problems
    assert problem.kind == "no_role"
    assert "555" in problem.text


async def test_non_mentionable_role_needs_mention_everyone_and_reports_it():
    guild = FakeGuild(GUILD, roles={555: FakeRole(mentionable=False)})
    perms = discord.Permissions(view_channel=True, send_messages=True)
    client = FakeClient({10: _chan(discord.TextChannel, guild, perms)})
    (problem,) = (await check_guild_channels(client, GUILD, [_req(10, role=555)])).problems
    assert problem.missing == ("Mention @everyone",)


async def test_check_failed_text_is_defused_and_capped():
    nasty = "@everyone [click](https://evil.example/x) <#123456789012345678> " + "y" * 500
    client = FakeClient(raises={10: RuntimeError(nasty)})
    (problem,) = (await check_guild_channels(client, GUILD, [_req(10)])).problems
    assert problem.kind == "check_failed"
    assert "@everyone" not in problem.text
    assert "https://" not in problem.text
    assert "<#123456789012345678>" not in problem.text
    assert len(problem.text) < 300


@pytest.mark.parametrize(
    "exc",
    [
        discord.Forbidden(_Resp(), "no"),
        discord.NotFound(_Resp(), "gone"),
    ],
)
async def test_forbidden_and_not_found_from_fetch_are_both_not_found(exc):
    class C(FakeClient):
        async def fetch_channel(self, channel_id):
            raise exc

    assert await _kind(C(), _req(10)) == ["not_found"]


async def test_other_http_errors_from_fetch_are_check_failed_not_not_found():
    class C(FakeClient):
        async def fetch_channel(self, channel_id):
            raise discord.HTTPException(_Resp(), "server on fire")

    assert await _kind(C(), _req(10)) == ["check_failed"]


# --- requirements from settings ---


def _guild_row(admin=None):
    return GuildSettings(GUILD, "09:00", "UTC", admin, "free", True, T0, None, None, T0)


def test_nothing_configured_needs_nothing():
    assert required_channels_for_guild(_guild_row(), [], None, None) == []


def test_three_features_on_one_channel_merge_into_one_requirement():
    reqs = required_channels_for_guild(
        _guild_row(admin=10),
        [GuildGame(GUILD, "palworld", 10)],
        ShiftSettings(GUILD, True, 10, "777777777777777777", None, None, 0),
        LoungeSettings(GUILD, 10, True, "hi", True, "08:00", [], None),
        game_names={"palworld": "Palworld"},
    )
    (req,) = reqs
    assert req.needed == {"view_channel", "send_messages", "embed_links"}
    assert req.ping_role_id == 777777777777777777
    for purpose in ("Palworld", "SHiFT codes", "admin", "lounge"):
        assert purpose in req.purpose


def test_merged_purpose_names_every_feature_even_when_one_name_contains_another():
    reqs = required_channels_for_guild(
        _guild_row(),
        [GuildGame(GUILD, "poe2", 10), GuildGame(GUILD, "poe", 10)],
        None,
        None,
        game_names={"poe2": "Path of Exile 2", "poe": "Path of Exile"},
    )
    (req,) = reqs
    assert req.purpose.split(" / ") == ["Path of Exile 2", "Path of Exile"]


def test_a_switched_off_lounge_is_not_required():
    lounge = LoungeSettings(GUILD, 70, False, "hi", False, "08:00", [], None)
    assert required_channels_for_guild(_guild_row(), [], None, lounge) == []


def test_a_disabled_shift_with_a_channel_is_not_required():
    shift = ShiftSettings(GUILD, False, 50, "everyone", None, None, 0)
    assert required_channels_for_guild(_guild_row(), [], shift, None) == []


def test_an_enabled_shift_with_no_channel_is_not_required():
    shift = ShiftSettings(GUILD, True, None, "none", None, None, 0)
    assert required_channels_for_guild(_guild_row(), [], shift, None) == []


async def test_check_guild_for_a_guild_with_nothing_configured_is_clean(db_path):
    _add_guild(db_path, GUILD)
    result = await check_guild(FakeClient(), db_path, GUILD)
    assert result.ok and result.lines() == []


async def test_check_guild_for_an_unknown_guild_says_not_set_up_not_ok(db_path):
    """Changed from "clean": a server we have no row for isn't all clear, it's unknown."""
    result = await check_guild(FakeClient(), db_path, 404)
    assert not result.ok
    assert [p.kind for p in result.problems] == ["not_set_up"]
    assert "isn't set up" in result.lines()[0]


async def test_check_guild_problems_come_back_in_requirement_order(db_path):
    _add_guild(db_path, GUILD, [30, 10, 20], admin=40)
    result = await check_guild(FakeClient(), db_path, GUILD)
    assert [p.channel_id for p in result.problems] == [30, 10, 20, 40]


# --- duplicate channels across guilds ---


async def test_two_servers_pointing_at_one_channel_only_the_owning_one_is_clean():
    shared = _chan(discord.TextChannel, FakeGuild(GUILD))
    client = FakeClient({10: shared})
    assert (await check_guild_channels(client, GUILD, [_req(10)])).ok
    assert await _kind(client, _req(10), guild_id=OTHER) == ["wrong_guild"]


# --- the sweep ---


async def test_flapping_guild_is_told_once_per_change_and_never_missed(db_path):
    _add_guild(db_path, 1, [10])
    guild = FakeGuild(1)
    client = FakeClient(guilds={1: guild})
    no_send = discord.Permissions(view_channel=True, embed_links=True)
    states = {
        "gone": None,
        "noperm": _chan(discord.TextChannel, guild, no_send),
        "ok": _chan(discord.TextChannel, guild),
    }
    sequence = ["gone", "gone", "ok", "gone", "noperm", "noperm", "ok", "ok", "noperm"]
    told: list[str] = []

    async def notify(guild_id, text):
        told.append(text)

    for state in sequence:
        client.channels.clear()
        if states[state] is not None:
            client.channels[10] = states[state]
        await sweep_guild_permissions(client, db_path, notify)
    assert (
        len(told) == 4
    )  # gone, gone again after a clean spell, noperm, noperm after a clean spell
    assert ["not found" in t for t in told] == [True, True, False, False]
    assert ["missing Send Messages" in t for t in told] == [False, False, True, True]
    assert _stored_problems(db_path, 1) == told[-1]


async def test_a_problem_that_changes_shape_is_news_again(db_path):
    _add_guild(db_path, 1, [10, 11])
    guild = FakeGuild(1)
    client = FakeClient({10: _chan(discord.TextChannel, guild)}, guilds={1: guild})
    told = []

    async def notify(guild_id, text):
        told.append(text)

    await sweep_guild_permissions(client, db_path, notify)  # 11 is gone
    client.channels[11] = _chan(discord.VoiceChannel, guild)  # now it's a voice channel
    await sweep_guild_permissions(client, db_path, notify)
    await sweep_guild_permissions(client, db_path, notify)  # unchanged: quiet
    assert len(told) == 2


async def test_a_failed_notification_is_retried_by_the_next_sweep_not_lost(db_path):
    _add_guild(db_path, 1, [10])
    client = FakeClient(guilds={1: FakeGuild(1)})
    attempts = []

    async def notify(guild_id, text):
        attempts.append(guild_id)
        if len(attempts) == 1:
            raise RuntimeError("discord is having a day")

    first = await sweep_guild_permissions(client, db_path, notify)
    assert (first.failed, first.notified) == (1, 0)
    assert _stored_problems(db_path, 1) is None  # not marked told
    second = await sweep_guild_permissions(client, db_path, notify)
    assert (second.failed, second.notified) == (0, 1)
    assert attempts == [1, 1]
    await sweep_guild_permissions(client, db_path, notify)
    assert attempts == [1, 1]


async def test_sweep_of_500_servers_with_crashes_and_dead_notifiers_keeps_walking(db_path):
    with closing(connect(db_path)) as conn:
        for gid in range(1, 501):
            repo.create_guild(conn, gid, set_up=True)
            repo.follow_game(conn, gid, "game0", 100_000 + gid)
    client = FakeClient()
    told: list[int] = []
    for gid in range(1, 501):
        kind = gid % 5
        channel = 100_000 + gid
        if kind == 0:  # healthy
            client.guilds[gid] = FakeGuild(gid)
            client.channels[channel] = _chan(discord.TextChannel, client.guilds[gid])
        elif kind == 1:  # channel deleted
            client.guilds[gid] = FakeGuild(gid)
        elif kind == 2:  # the client blows up looking the channel up
            client.guilds[gid] = FakeGuild(gid)
            client.raises[channel] = RuntimeError("cache exploded")
        elif kind == 3:  # the guild lookup itself blows up
            client.guilds[gid] = RuntimeError("guild lookup exploded")
        else:  # channel deleted and the notifier dies for this one
            client.guilds[gid] = FakeGuild(gid)

    async def notify(guild_id, text):
        if guild_id % 5 == 4:
            raise RuntimeError("notifier died")
        told.append(guild_id)

    result = await sweep_guild_permissions(client, db_path, notify)
    assert result == SweepResult(
        checked=400, with_problems=300, notified=200, skipped=0, failed=200
    )
    assert sorted(told) == sorted(g for g in range(1, 501) if g % 5 in (1, 2))
    # Changed: the 200 failed guilds are in the line now, not silently dropped from it.
    assert render_sweep_counts(result) == (
        "newsbot: permission problems in 300 of 400 servers; "
        "permission check failed for 200 of 600 servers"
    )


async def test_sweep_skips_guilds_that_are_not_set_up_and_guilds_the_bot_cannot_see(db_path):
    _add_guild(db_path, 1, [10], set_up=False)
    _add_guild(db_path, 2, [20])  # not in client.guilds
    told = []

    async def notify(guild_id, text):
        told.append(guild_id)

    result = await sweep_guild_permissions(FakeClient(), db_path, notify)
    assert (result.checked, result.skipped, told) == (0, 1, [])
    assert _stored_problems(db_path, 1) is None and _stored_problems(db_path, 2) is None


async def test_a_hanging_lookup_does_not_stall_the_whole_sweep(db_path, monkeypatch):
    monkeypatch.setattr(permissions, "_GUILD_TIMEOUT_S", 0.1)  # the real one is a minute
    _add_guild(db_path, 1, [10])
    _add_guild(db_path, 2, [20])
    client = FakeClient(guilds={1: FakeGuild(1), 2: FakeGuild(2)}, hang=[10])
    told = []

    async def notify(guild_id, text):
        told.append(guild_id)

    result = await asyncio.wait_for(sweep_guild_permissions(client, db_path, notify), timeout=2)
    assert 2 in told
    assert result.failed == 1  # the hung guild is counted, not skipped


def test_owner_hears_when_the_sweep_itself_is_failing():
    assert render_sweep_counts(SweepResult(checked=0, failed=38)) != ""


def test_sweep_counts_wording_is_counts_only():
    assert render_sweep_counts(SweepResult(checked=38, with_problems=0)) == ""
    assert render_sweep_counts(SweepResult(checked=38, with_problems=3)) == (
        "newsbot: permission problems in 3 of 38 servers"
    )


def test_sweep_counts_wording_for_one_server_is_singular():
    """Changed from the old "1 of 1 servers" wart; the summary line was already singular."""
    line = render_sweep_counts(SweepResult(checked=1, with_problems=1))
    assert line == "newsbot: permission problems in 1 of 1 server"


def test_sweep_counts_include_failed_guilds_alongside_problems():
    assert render_sweep_counts(SweepResult(checked=0, failed=38)) == (
        "newsbot: permission check failed for 38 of 38 servers"
    )
    assert render_sweep_counts(SweepResult(checked=3, with_problems=1, failed=1)) == (
        "newsbot: permission problems in 1 of 3 servers; permission check failed for 1 of 4 servers"
    )
    assert render_sweep_counts(SweepResult(checked=0, failed=1)).endswith("1 of 1 server")


# --- sweep through the real router ---


def _text_with_sends(guild):
    return _chan(discord.TextChannel, guild)


async def test_sweep_through_the_real_router_keeps_each_guilds_problems_in_that_guild(db_path):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 1, admin_channel_id=1001, set_up=True)
        repo.follow_game(conn, 1, "game0", 10)  # deleted
        repo.create_guild(conn, 2, admin_channel_id=1002, set_up=True)
        repo.follow_game(conn, 2, "game0", 20)  # fine
        repo.create_guild(conn, 3, admin_channel_id=1003, set_up=True)
        repo.follow_game(conn, 3, "game0", 30)  # deleted
    g1, g2, g3, home = FakeGuild(1), FakeGuild(2), FakeGuild(3), FakeGuild(999)
    admin1, admin2, admin3 = (_text_with_sends(g) for g in (g1, g2, g3))
    owner = _text_with_sends(home)
    client = FakeClient(
        {1001: admin1, 1002: admin2, 1003: admin3, 20: _text_with_sends(g2), OWNER_CHANNEL: owner},
        guilds={1: g1, 2: g2, 3: g3},
    )
    router = Router(db_path, client_sender(client), OWNER_CHANNEL)
    result = await sweep_guild_permissions(client, db_path, router.notify_guild)
    assert (result.with_problems, result.notified) == (2, 2)
    assert (
        len(admin1.sent) == 1 and "<#10>" in admin1.sent[0][0] and "<#30>" not in admin1.sent[0][0]
    )
    assert (
        len(admin3.sent) == 1 and "<#30>" in admin3.sent[0][0] and "<#10>" not in admin3.sent[0][0]
    )
    assert admin2.sent == []
    assert owner.sent == []


async def test_sweep_does_not_announce_a_server_problem_inside_another_server(db_path):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 1, set_up=True)
        repo.create_guild(conn, 3, admin_channel_id=1001, set_up=True)  # guild 1's channel!
    g1, g3 = FakeGuild(1), FakeGuild(3)
    victim = _text_with_sends(g1)
    client = FakeClient({1001: victim}, guilds={1: g1, 3: g3})
    router = Router(db_path, client_sender(client), OWNER_CHANNEL)
    await sweep_guild_permissions(client, db_path, router.notify_guild)
    assert victim.sent == []


async def test_hostile_exception_text_reaches_the_admin_channel_defused(db_path):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 1, admin_channel_id=1001, set_up=True)
        repo.follow_game(conn, 1, "game0", 10)
    g1 = FakeGuild(1)
    admin = _text_with_sends(g1)
    nasty = "@everyone <@&123456789012345678> [login](https://evil.example/) discord.gg/abc"
    client = FakeClient({1001: admin}, guilds={1: g1}, raises={10: RuntimeError(nasty)})
    router = Router(db_path, client_sender(client), OWNER_CHANNEL)
    await sweep_guild_permissions(client, db_path, router.notify_guild)
    ((text, kwargs),) = admin.sent
    assert not re.search(r"(?<![\w​])@everyone", text)
    assert "<@&123456789012345678>" not in text
    assert "](https://" not in text
    assert "discord.gg/" not in text
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_the_notice_keeps_its_channel_links(db_path):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 1, admin_channel_id=1001, set_up=True)
        repo.follow_game(conn, 1, "game0", 123456789012345678)
    g1 = FakeGuild(1)
    admin = _text_with_sends(g1)
    client = FakeClient({1001: admin}, guilds={1: g1})
    router = Router(db_path, client_sender(client), OWNER_CHANNEL)
    await sweep_guild_permissions(client, db_path, router.notify_guild)
    assert "<#123456789012345678>" in admin.sent[0][0]


def test_notice_with_many_astral_problem_lines_stays_under_the_cap():
    from newsbot.bot.format import discord_len

    text = render_guild_permission_notice([f"\U0001f600 channel <#{i}>: gone" for i in range(400)])
    assert discord_len(text) <= 2000
    assert text.startswith("newsbot: I can't do everything")


# --- the lines v2 produced, and still do ---
#
# This section pinned v2's `required_channels` and `check_channels` against the fixture config so
# the per-server builder couldn't quietly change a word. v2 retired at the cutover; the same
# channels, purposes, merges and one-line texts are asserted here against a server's rows, which
# is the only thing that builds them now.

_GAMES = [
    GuildGame(GUILD, "borderlands4", 123456789012345690),
    GuildGame(GUILD, "palworld", 123456789012345691),
    GuildGame(GUILD, "diablo4", 123456789012345692),
]
_NAMES = {"borderlands4": "Borderlands 4", "palworld": "Palworld", "diablo4": "Diablo IV"}
_ADMIN = 123456789012345679
_SHIFT = 999999999999999999


def _fixture_reqs(*, shift_channel=_SHIFT):
    guild = GuildSettings(GUILD, "09:00", "UTC", _ADMIN, "free", True, T0, None, None, T0)
    shift = ShiftSettings(GUILD, True, shift_channel, "everyone", None, None, 0)
    return required_channels_for_guild(guild, _GAMES, shift, None, game_names=_NAMES)


def test_required_channels_are_exactly_what_they_were_for_the_fixture_setup():
    got = {r.channel_id: (r.purpose, sorted(r.needed), r.ping_role_id) for r in _fixture_reqs()}
    base = ["embed_links", "send_messages", "view_channel"]
    assert got[123456789012345690] == ("Borderlands 4", base, None)
    assert got[123456789012345691] == ("Palworld", base, None)
    assert got[123456789012345692] == ("Diablo IV", base, None)
    assert got[_SHIFT] == (
        "SHiFT codes",
        ["mention_everyone", "send_messages", "view_channel"],
        None,
    )
    assert got[_ADMIN] == ("admin", ["send_messages", "view_channel"], None)
    assert len(got) == 5


def test_shared_channel_purpose_join_is_unchanged():
    merged = {r.channel_id: r for r in _fixture_reqs(shift_channel=_ADMIN)}[_ADMIN]
    assert merged.purpose == "SHiFT codes / admin"
    assert merged.needed == {"view_channel", "send_messages", "mention_everyone"}


async def test_check_lines_are_unchanged():
    guild = FakeGuild(GUILD)
    no_embed = discord.Permissions(view_channel=True, send_messages=True)
    client = FakeClient(
        {
            123456789012345690: _chan(discord.TextChannel, guild, no_embed),
            123456789012345691: _chan(discord.VoiceChannel, guild),
            123456789012345692: _chan(discord.TextChannel, FakeGuild(OTHER)),
            _SHIFT: _chan(discord.TextChannel, guild),
        }
    )
    result = await check_guild_channels(client, GUILD, _fixture_reqs())
    assert result.lines() == [
        "Borderlands 4 channel <#123456789012345690>: missing Embed Links",
        "Palworld channel <#123456789012345691>: not a text channel",
        "Diablo IV channel <#123456789012345692>: not in the configured guild",
        f"admin channel <#{_ADMIN}>: not found or not visible to the bot",
    ]


async def test_check_crash_line_keeps_its_shape_for_plain_messages():
    client = FakeClient(raises={123456789012345690: RuntimeError("boom")})
    lines = (await check_guild_channels(client, GUILD, _fixture_reqs())).lines()
    assert lines[0] == "Borderlands 4 channel <#123456789012345690>: permission check failed (boom)"


async def test_check_crash_line_escapes_and_caps_the_exception():
    """The exception text is run through esc() and plain_line(100)."""
    client = FakeClient(raises={123456789012345690: RuntimeError("a_b " + "z" * 300)})
    line = (await check_guild_channels(client, GUILD, _fixture_reqs())).lines()[0]
    assert "a\\_b" in line
    assert line.endswith("…)")
    assert "z" * 150 not in line


def test_the_notice_text_is_the_header_and_one_bullet_per_problem():
    assert render_guild_permission_notice(["a", "b"]) == (
        "newsbot: I can't do everything I'm set up to do here:\n- a\n- b"
    )
    assert render_guild_permission_notice([]) == ""
