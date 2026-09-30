"""`/newsbot` for the public app, and `/lounge quote-now` (plan task 9, design.md §15).

Every command is driven through its real callback with a hand-written interaction
against a real temp-file SQLite. The claim under test, over and over: a command run in
server A reads and writes server A and nothing else, and refuses anyone without Manage
Server, anything but a plain text channel of its own server, and bad input.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from types import SimpleNamespace

import discord
import pytest
from discord.app_commands import Choice
from v3_fakes import (
    GUILD_A,
    GUILD_B,
    MEMBER,
    FakeInteraction,
    channel,
    command,
    make_guild,
    role,
    thin_channel,
)

from newsbot.bot import commands as commands_module
from newsbot.bot.commands import (
    channel_problem,
    make_guild_admin_group,
    make_lounge_group,
    parse_digest_time,
    resolve_timezone,
    zone_choices,
)
from newsbot.bot.permissions import ChannelProblem, GuildCheck
from newsbot.pipeline.guild_digest import GuildDigestDeps
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import GuildDigestRow

CT = discord.ChannelType


@pytest.fixture
def problems(monkeypatch):
    """The problems `check_guild` will report, and every guild id it was asked about."""
    state = SimpleNamespace(lines=[], asked=[])

    async def fake_check(client, db_path, guild_id, *, game_names=None):
        state.asked.append(guild_id)
        found = tuple(
            ChannelProblem(1, "x", "missing_permissions", (), line) for line in state.lines
        )
        return GuildCheck(guild_id, found)

    monkeypatch.setattr(commands_module, "check_guild", fake_check)
    return state


def deps_for(cfg, db_path):
    async def nobody(*_):
        return None

    return GuildDigestDeps(
        cfg=cfg,
        db_path=db_path,
        now=lambda: datetime(2026, 9, 30, 17, 0, tzinfo=UTC),
        publisher_for=None,
        notify_guild=nobody,
    )


@pytest.fixture
def admin(v3_cfg, v3_db, problems):
    """`admin("follow")` is that command's callback; `admin.deps` is the shared digest deps."""
    deps = deps_for(v3_cfg, v3_db)
    bot = SimpleNamespace(db_path=v3_db)
    group = make_guild_admin_group(v3_cfg, bot, digest_deps=lambda: deps)

    def get(name):
        return command(group, name)

    get.group = group
    get.deps = deps
    get.callback = lambda name: command(group, name).callback
    return get


def snapshot(db_path, guild_id):
    """Everything stored for one server, for before/after comparisons."""
    with closing(connect(db_path)) as conn:
        return (
            repo.get_guild(conn, guild_id),
            repo.list_guild_games(conn, guild_id),
            repo.get_shift(conn, guild_id),
            repo.recent_notices(conn, guild_id),
        )


# --- registration shape ---


def test_the_group_shape(admin, v3_cfg):
    group = admin.group
    assert group.name == "newsbot"
    assert group.guild_only is True
    assert group.default_permissions.manage_guild is True
    assert group.allowed_installs.guild is True and group.allowed_installs.user is False
    assert {c.name for c in group.commands} == {
        "setup",
        "follow",
        "unfollow",
        "games",
        "settings",
        "shift",
        "status",
        "preview",
        "run-now",
    }


def test_default_permissions_follow_the_configured_admin_permission(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"admin_permission": "kick_members"})
    group = make_guild_admin_group(cfg, SimpleNamespace(db_path=v3_db))
    assert group.default_permissions.kick_members is True


def test_names_and_descriptions_fit_discords_limits(admin):
    def walk(node):
        assert 1 <= len(node.name) <= 32
        assert 1 <= len(node.description) <= 100, node.name
        for param in getattr(node, "parameters", []):
            assert 1 <= len(param.name) <= 32
            assert 1 <= len(param.description) <= 100, (node.name, param.name)
        for child in getattr(node, "commands", []):
            walk(child)

    walk(admin.group)
    walk(
        make_lounge_group(
            SimpleNamespace(admin_permission="manage_guild"), SimpleNamespace(db_path="x")
        )
    )


# --- run-time denial ---

CALLS = {
    "follow": {"game": "palworld", "channel": channel(5)},
    "unfollow": {"game": "palworld"},
    "games": {},
    "settings": {"time": "10:00"},
    "shift": {"channel": channel(5)},
    "status": {},
    "preview": {},
    "run-now": {},
}


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_non_admins_are_refused_and_nothing_is_written(admin, v3_db, name):
    interaction = FakeInteraction(permissions=MEMBER)
    await admin.callback(name)(interaction, **CALLS[name])
    assert interaction.text == "You don't have permission to run this."
    assert interaction.sent[0]["ephemeral"] is True
    assert snapshot(v3_db, GUILD_A) == (None, [], None, [])  # not even a row was created


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_outside_a_server_is_refused(admin, name):
    interaction = FakeInteraction(guild_id=None)
    await admin.callback(name)(interaction, **CALLS[name])
    assert "inside a server" in interaction.text


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_every_reply_is_ephemeral_and_mention_proof(
    admin, v3_db, problems, digest_calls, name
):
    make_guild(v3_db, GUILD_A, games=[("palworld", 7)])
    problems.lines = ["@everyone <@&1> trouble"]
    interaction = FakeInteraction()
    await admin.callback(name)(interaction, **CALLS[name])
    assert interaction.sent
    for message in interaction.sent:
        assert message["ephemeral"] is True
        if "allowed_mentions" in message:
            assert message["allowed_mentions"].everyone is False
            assert message["allowed_mentions"].roles is False
            assert message["allowed_mentions"].users is False


# --- channel_problem ---


@pytest.mark.parametrize("kind", [CT.text, CT.news])
def test_text_and_news_channels_of_this_guild_pass(kind):
    assert channel_problem(channel(5, GUILD_A, kind), GUILD_A) is None
    assert channel_problem(thin_channel(5, GUILD_A, kind), GUILD_A) is None


@pytest.mark.parametrize(
    "bad",
    [
        channel(5, GUILD_B),
        thin_channel(5, GUILD_B),
        channel(5, None, CT.private),
        channel(5, GUILD_A, CT.public_thread),
        channel(5, GUILD_A, CT.private_thread),
        channel(5, GUILD_A, CT.news_thread),
        channel(5, GUILD_A, CT.voice),
        channel(5, GUILD_A, CT.stage_voice),
        channel(5, GUILD_A, CT.forum),
        channel(5, GUILD_A, CT.media),
        channel(5, GUILD_A, CT.category),
        channel(0, GUILD_A),
        channel(-3, GUILD_A),
        SimpleNamespace(id=5, type=CT.text),  # can't be tied to any guild: refused
        SimpleNamespace(),
        None,
    ],
)
def test_everything_else_is_refused(bad):
    assert channel_problem(bad, GUILD_A)


def test_a_foreign_channel_reply_reveals_nothing_about_it():
    foreign = channel(987654321, GUILD_B, CT.voice)  # wrong guild AND wrong type
    message = channel_problem(foreign, GUILD_A)
    assert message == "That channel isn't in this server."


def test_channel_problem_with_no_guild_id():
    assert channel_problem(channel(5), None)


# --- follow / unfollow / games ---


async def test_follow_saves_for_this_server_only_and_checks_permissions(admin, v3_db, problems):
    make_guild(v3_db, GUILD_B, games=[("rust", 99)])
    before_b = snapshot(v3_db, GUILD_B)
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(5))
    with closing(connect(v3_db)) as conn:
        assert [(g.game_key, g.channel_id) for g in repo.list_guild_games(conn, GUILD_A)] == [
            ("palworld", 5)
        ]
    assert "Now following Palworld" in interaction.text and "<#5>" in interaction.text
    assert problems.asked == [GUILD_A]
    assert snapshot(v3_db, GUILD_B) == before_b


async def test_follow_reports_permission_problems_in_the_reply(admin, problems):
    problems.lines = ["palworld channel <#5>: missing Send Messages"]
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(5))
    assert "Now following Palworld" in interaction.text
    assert "I can't do everything" in interaction.text
    assert "missing Send Messages" in interaction.text


async def test_follow_creates_a_missing_row_with_the_configured_tier(v3_cfg, v3_db, problems):
    cfg = v3_cfg.model_copy(update={"comped_guild_ids": [GUILD_A]})
    group = make_guild_admin_group(cfg, SimpleNamespace(db_path=v3_db))
    interaction = FakeInteraction()
    await command(group, "follow").callback(interaction, game="rust", channel=channel(5))
    guild = snapshot(v3_db, GUILD_A)[0]
    assert guild.tier == "comped" and guild.set_up is False
    assert "isn't set up yet" in interaction.text


async def test_follow_the_same_game_again_moves_it(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(6))
    assert "Moved Palworld" in interaction.text
    assert snapshot(v3_db, GUILD_A)[1][0].channel_id == 6


async def test_follow_at_ten_games_is_refused_and_explained(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[(f"game{i}", 100 + i) for i in range(10)])
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(5))
    assert "already follows 10 games; unfollow one first" in interaction.text
    assert len(snapshot(v3_db, GUILD_A)[1]) == 10


async def test_moving_a_game_still_works_at_ten(admin, v3_db):
    games = [(f"game{i}", 100 + i) for i in range(9)] + [("palworld", 5)]
    make_guild(v3_db, GUILD_A, games=games)
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(6))
    assert "Moved Palworld" in interaction.text


@pytest.mark.parametrize("game", ["nope", "", "' OR 1=1 --", "borderlands4; DROP TABLE guilds"])
async def test_follow_validates_the_game_against_the_catalog(admin, v3_db, game):
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game=game, channel=channel(5))
    assert "don't know that game" in interaction.text
    assert snapshot(v3_db, GUILD_A)[1] == []


@pytest.mark.parametrize(
    "bad",
    [
        channel(5, GUILD_B),
        thin_channel(5, GUILD_B),
        channel(5, None, CT.private),
        channel(5, GUILD_A, CT.public_thread),
        channel(5, GUILD_A, CT.voice),
        channel(5, GUILD_A, CT.forum),
    ],
)
async def test_follow_refuses_a_bad_channel_and_writes_nothing(admin, v3_db, problems, bad):
    make_guild(v3_db, GUILD_A)
    before = snapshot(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=bad)
    assert snapshot(v3_db, GUILD_A) == before
    assert problems.asked == []


async def test_unfollow(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5), ("rust", 6)])
    make_guild(v3_db, GUILD_B, games=[("palworld", 7)])
    interaction = FakeInteraction()
    await admin.callback("unfollow")(interaction, game="palworld")
    assert "Stopped following Palworld." == interaction.text
    assert [g.game_key for g in snapshot(v3_db, GUILD_A)[1]] == ["rust"]
    assert [g.game_key for g in snapshot(v3_db, GUILD_B)[1]] == ["palworld"]


async def test_unfollowing_the_last_game_says_the_digest_pauses(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    interaction = FakeInteraction()
    await admin.callback("unfollow")(interaction, game="palworld")
    assert "no digest will post until you follow one" in interaction.text
    assert snapshot(v3_db, GUILD_A)[0].set_up is True


async def test_unfollow_a_game_the_server_does_not_follow(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("rust", 5)])
    make_guild(v3_db, GUILD_B, games=[("palworld", 7)])
    interaction = FakeInteraction()
    await admin.callback("unfollow")(interaction, game="palworld")  # B follows it, A doesn't
    assert "doesn't follow that game" in interaction.text
    assert [g.game_key for g in snapshot(v3_db, GUILD_B)[1]] == ["palworld"]


async def test_games_lists_this_servers_then_the_rest(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    make_guild(v3_db, GUILD_B, games=[("rust", 7)])
    interaction = FakeInteraction()
    await admin.callback("games")(interaction)
    assert "Following (1 of 10)" in interaction.text
    assert "Palworld in <#5>" in interaction.text
    assert "Available: Borderlands 4, Rust" in interaction.text
    assert "<#7>" not in interaction.text


async def test_follow_and_unfollow_autocomplete(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    follow = command(admin.group, "follow")._params["game"].autocomplete
    unfollow = command(admin.group, "unfollow")._params["game"].autocomplete
    assert [c.value for c in await follow(FakeInteraction(), "")] == ["borderlands4", "rust"]
    assert [c.value for c in await follow(FakeInteraction(), "RUS")] == ["rust"]
    assert [c.value for c in await unfollow(FakeInteraction(), "")] == ["palworld"]
    assert await unfollow(FakeInteraction(), "rust") == []
    assert await follow(FakeInteraction(guild_id=None), "") == []


# --- settings ---


@pytest.mark.parametrize("good", ["00:00", "09:30", "23:59", "12:05"])
def test_good_digest_times(good):
    assert parse_digest_time(good) == good


@pytest.mark.parametrize(
    "bad",
    ["24:00", "9:00", "09:60", "9am", "", " 09:00", "09:00 ", "09-00", "٠٩:٠٠", "09:00\n", "١٢:٣٠"],
)
def test_bad_digest_times(bad):
    assert parse_digest_time(bad) is None


@pytest.mark.parametrize(
    "bad", ["Mars/Olympus", "", "../etc/passwd", "America", "UTC\x00", "  ", "é" * 50]
)
def test_bad_zones(bad):
    assert resolve_timezone(bad) is None


def test_zones_resolve_case_insensitively_to_the_canonical_name():
    assert resolve_timezone("america/chicago") == "America/Chicago"
    assert resolve_timezone(" UTC ") == "UTC"


def test_zone_autocomplete_is_substring_and_capped():
    assert "America/Chicago" in [c.value for c in zone_choices("chicago")]
    assert len(zone_choices("")) == 25
    assert len(zone_choices("a")) == 25
    assert zone_choices("zzzzzz-not-a-zone") == []
    assert all(len(c.name) <= 100 for c in zone_choices(""))


async def test_settings_saves_time_and_zone_for_this_server_only(admin, v3_db):
    make_guild(v3_db, GUILD_A)
    make_guild(v3_db, GUILD_B, digest_time="07:00", timezone="Europe/Paris")
    before_b = snapshot(v3_db, GUILD_B)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, time="10:30", timezone="america/chicago")
    guild = snapshot(v3_db, GUILD_A)[0]
    assert (guild.digest_time, guild.timezone) == ("10:30", "America/Chicago")
    assert snapshot(v3_db, GUILD_B) == before_b
    assert (
        "digest time 10:30" in interaction.text and "time zone America/Chicago" in interaction.text
    )


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"time": "25:00"}, "HH:MM"),
        ({"time": "9:00"}, "HH:MM"),
        ({"timezone": "Mars/Olympus"}, "time zone I know"),
        ({"time": "10:00", "timezone": "Nope"}, "time zone I know"),
        ({}, "Nothing to change"),
        ({"admin_channel": channel(5), "clear_admin_channel": True}, "not both"),
    ],
)
async def test_settings_validation_writes_nothing(admin, v3_db, kwargs, fragment):
    make_guild(v3_db, GUILD_A)
    before = snapshot(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, **kwargs)
    assert fragment in interaction.text
    assert snapshot(v3_db, GUILD_A)[0] == before[0]


async def test_settings_admin_channel_set_and_clear(admin, v3_db):
    make_guild(v3_db, GUILD_A)
    await admin.callback("settings")(FakeInteraction(), admin_channel=channel(77))
    assert snapshot(v3_db, GUILD_A)[0].admin_channel_id == 77
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, clear_admin_channel=True)
    assert snapshot(v3_db, GUILD_A)[0].admin_channel_id is None
    assert "admin channel cleared" in interaction.text


@pytest.mark.parametrize(
    "bad",
    [
        channel(77, GUILD_B),
        thin_channel(77, GUILD_B),
        channel(77, None, CT.private),
        channel(77, GUILD_A, CT.public_thread),
        channel(77, GUILD_A, CT.voice),
        channel(77, GUILD_A, CT.forum),
    ],
)
async def test_settings_admin_channel_refuses_a_bad_channel(admin, v3_db, problems, bad):
    make_guild(v3_db, GUILD_A, admin_channel_id=5)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, time="10:00", admin_channel=bad)
    guild = snapshot(v3_db, GUILD_A)[0]
    assert guild.admin_channel_id == 5  # untouched
    assert guild.digest_time == "09:00"  # and the rest of the request wasn't half-applied
    assert problems.asked == []


async def test_settings_reports_problems_after_saving(admin, v3_db, problems):
    make_guild(v3_db, GUILD_A)
    problems.lines = ["admin channel <#77>: missing Send Messages"]
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, admin_channel=channel(77))
    assert snapshot(v3_db, GUILD_A)[0].admin_channel_id == 77
    assert "Saved: admin channel <#77>." in interaction.text
    assert "missing Send Messages" in interaction.text


async def test_settings_says_a_passed_time_posts_within_a_minute(admin, v3_db, monkeypatch):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    monkeypatch.setattr(commands_module, "datetime", _FrozenDatetime)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, time="01:00", timezone="UTC")
    assert "already passed today" in interaction.text and "within a minute" in interaction.text
    later = FakeInteraction()
    await admin.callback("settings")(later, time="23:00")
    assert "within a minute" not in later.text


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 30, 12, 0, tzinfo=tz or UTC)


async def test_settings_late_note_needs_a_set_up_server_that_follows_a_game(
    admin, v3_db, monkeypatch
):
    monkeypatch.setattr(commands_module, "datetime", _FrozenDatetime)
    make_guild(v3_db, GUILD_A, set_up=False, games=[("palworld", 5)])
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, time="01:00", timezone="UTC")
    assert "within a minute" not in interaction.text


# --- shift ---


async def test_shift_enable_with_channel_and_ping_everyone(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    interaction = FakeInteraction()
    await admin.callback("shift")(
        interaction, channel=channel(9), ping=Choice(name="everyone", value="everyone")
    )
    shift = snapshot(v3_db, GUILD_A)[2]
    assert (shift.enabled, shift.channel_id, shift.ping) == (True, 9, "everyone")
    assert shift.enabled_at is not None
    assert interaction.text == "Saved: SHiFT alerts are on."


async def test_shift_ping_role_stores_the_role_id(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    rid = 400000000000000001
    await admin.callback("shift")(
        FakeInteraction(),
        channel=channel(9),
        ping=Choice(name="role", value="role"),
        role=role(rid),
    )
    assert snapshot(v3_db, GUILD_A)[2].ping == str(rid)


async def test_shift_role_alone_means_ping_role(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    await admin.callback("shift")(
        FakeInteraction(), channel=channel(9), role=role(400000000000000002)
    )
    assert snapshot(v3_db, GUILD_A)[2].ping == "400000000000000002"


async def test_shift_ping_role_without_a_role_is_an_error(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    interaction = FakeInteraction()
    await admin.callback("shift")(
        interaction, channel=channel(9), ping=Choice(name="role", value="role")
    )
    assert "needs a role" in interaction.text
    assert snapshot(v3_db, GUILD_A)[2] is None


async def test_shift_role_with_another_ping_is_an_error(admin, v3_db):
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await admin.callback("shift")(
        interaction,
        channel=channel(9),
        ping=Choice(name="everyone", value="everyone"),
        role=role(400000000000000001),
    )
    assert "only goes with ping: role" in interaction.text
    assert snapshot(v3_db, GUILD_A)[2] is None


@pytest.mark.parametrize(
    "bad_role", [role(400000000000000001, GUILD_B), role(GUILD_A, default=True)]
)
async def test_shift_refuses_a_foreign_or_default_role(admin, v3_db, bad_role):
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, channel=channel(9), role=bad_role)
    assert snapshot(v3_db, GUILD_A)[2] is None
    assert "role" in interaction.text or "@everyone" in interaction.text


@pytest.mark.parametrize(
    "bad",
    [
        channel(9, GUILD_B),
        thin_channel(9, GUILD_B),
        channel(9, None, CT.private),
        channel(9, GUILD_A, CT.public_thread),
        channel(9, GUILD_A, CT.voice),
        channel(9, GUILD_A, CT.forum),
    ],
)
async def test_shift_channel_refuses_a_bad_channel(admin, v3_db, problems, bad):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, channel=bad)
    assert snapshot(v3_db, GUILD_A)[2] is None
    assert problems.asked == []


async def test_shift_explains_itself_when_the_server_does_not_follow_borderlands(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, channel=channel(9))
    assert interaction.text == (
        "Saved, but nothing will post until this server follows Borderlands 4."
    )
    assert snapshot(v3_db, GUILD_A)[2].enabled is True


async def test_shift_enabling_needs_a_channel(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, enabled=True)
    assert "Pick a channel" in interaction.text
    assert snapshot(v3_db, GUILD_A)[2] is None


async def test_shift_turn_off_keeps_the_channel_and_ping(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    await admin.callback("shift")(
        FakeInteraction(), channel=channel(9), ping=Choice(name="everyone", value="everyone")
    )
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, enabled=False)
    shift = snapshot(v3_db, GUILD_A)[2]
    assert (shift.enabled, shift.channel_id, shift.ping) == (False, 9, "everyone")
    assert interaction.text == "Saved: SHiFT alerts are off."


async def test_shift_is_per_server_and_reports_permission_problems(admin, v3_db, problems):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 5)])
    make_guild(v3_db, GUILD_B, games=[("borderlands4", 6)])
    before_b = snapshot(v3_db, GUILD_B)
    problems.lines = ["SHiFT codes channel <#9>: missing Mention @everyone"]
    interaction = FakeInteraction()
    await admin.callback("shift")(
        interaction, channel=channel(9), ping=Choice(name="everyone", value="everyone")
    )
    assert "missing Mention @everyone" in interaction.text
    assert snapshot(v3_db, GUILD_B) == before_b
    assert problems.asked == [GUILD_A]


# --- status ---


async def test_status_shows_this_server_and_never_spend(admin, v3_db):
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 5), ("palworld", 6)])
    make_guild(v3_db, GUILD_B, games=[("rust", 7)])
    with closing(connect(v3_db)) as conn:
        repo.add_notice(conn, GUILD_A, "A's problem")
        repo.add_notice(conn, GUILD_B, "B's secret problem")
        repo.set_shift(conn, GUILD_A, enabled=True, channel_id=9, ping="everyone")
        conn.execute(
            "INSERT INTO source_health (source_name, consecutive_failures) "
            "VALUES ('Borderlands 4 Steam', 3)"
        )
        conn.commit()
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    embed = interaction.sent[-1]["embed"]
    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    assert "Tier: comped" in text
    assert "Borderlands 4 in <#5>: 3 of 4 sources ok" in text  # 2 own + 2 shared, 1 failing
    assert "Palworld in <#6>" in text
    assert "A's problem" in text and "B's secret" not in text and "Rust" not in text
    assert "On in <#9>, ping: @everyone. Pings today: 0 of 3." in text
    assert "spend" not in text.lower() and "$" not in text
    assert interaction.sent[-1]["ephemeral"] is True


async def test_status_before_setup_and_with_a_broken_zone(admin, v3_db):
    make_guild(v3_db, GUILD_A, set_up=False)
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, timezone="Mars/Olympus")
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    text = "\n".join(f.value for f in interaction.sent[-1]["embed"].fields)
    assert "Not set up yet" in text and "invalid" in text


async def test_status_creates_no_row_and_says_not_set_up(admin, v3_db):
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    assert "hasn't set up newsbot yet" in interaction.text
    assert snapshot(v3_db, GUILD_A)[0] is None


async def test_status_lists_last_digest_with_jump_links(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    with closing(connect(v3_db)) as conn, conn:
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at, guild_id, "
            "posted_by_game) VALUES ('2026-09-29', 'ok', 'n', 'n', ?, '{\"palworld\": 123}')",
            (GUILD_A,),
        )
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    text = "\n".join(f.value for f in interaction.sent[-1]["embed"].fields)
    assert f"https://discord.com/channels/{GUILD_A}/5/123" in text


# --- preview and run-now ---


def _row(status, posted=None):
    return GuildDigestRow(
        1, GUILD_A, date(2026, 9, 30), status, [], posted or {}, None, None, 1, None
    )


@pytest.fixture
def digest_calls(monkeypatch):
    calls = SimpleNamespace(runs=[], today=None, preview=None, prompts=[], answer=True)

    async def today(deps, guild_id):
        return calls.today

    async def run(deps, guild_id, *, kind, force=False, due=None):
        calls.runs.append((guild_id, kind, force))
        return SimpleNamespace(status="ok", notes=["a note with @everyone"])

    async def preview(deps, guild_id):
        return calls.preview

    class FakeConfirm:
        def __init__(self, owner_id, **_):
            self.value = None

        async def wait(self):
            self.value = calls.answer

    monkeypatch.setattr(commands_module, "todays_guild_digest", today)
    monkeypatch.setattr(commands_module, "run_guild_digest", run)
    monkeypatch.setattr(commands_module, "preview_guild_digest", preview)
    monkeypatch.setattr(commands_module, "ConfirmView", FakeConfirm)
    return calls


async def test_run_now_posts_for_this_server_without_asking_when_nothing_ran(
    admin, v3_db, digest_calls
):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    interaction = FakeInteraction()
    await admin.callback("run-now")(interaction)
    assert [(g, k.name, f) for g, k, f in digest_calls.runs] == [(GUILD_A, "RUN_NOW", False)]
    assert interaction.text.startswith("Run finished: ok")
    assert "@everyone" not in interaction.text  # the note is defused


@pytest.mark.parametrize(
    ("status", "asked"),
    [
        ("ok", "already posted"),
        ("partial", "already posted"),
        ("pending", "crashed"),
        ("failed", "crashed"),
    ],
)
async def test_run_now_confirms_before_reposting(admin, v3_db, digest_calls, status, asked):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.today = _row(status, posted={"palworld": 1})
    interaction = FakeInteraction()
    await admin.callback("run-now")(interaction)
    assert asked in interaction.sent[0]["content"]
    assert digest_calls.runs[0][2] is True  # forced once confirmed


async def test_run_now_cancelled_runs_nothing(admin, v3_db, digest_calls):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.today = _row("ok")
    digest_calls.answer = False
    interaction = FakeInteraction()
    await admin.callback("run-now")(interaction)
    assert digest_calls.runs == []
    assert interaction.original_edits[-1]["content"] == "Cancelled."


async def test_run_now_a_clean_failure_needs_no_confirmation(admin, v3_db, digest_calls):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.today = _row("failed")
    interaction = FakeInteraction()
    await admin.callback("run-now")(interaction)
    assert digest_calls.runs and digest_calls.runs[0][2] is False


@pytest.mark.parametrize("name", ["run-now", "preview"])
async def test_digest_commands_need_setup_and_games(admin, v3_db, digest_calls, name):
    interaction = FakeInteraction()
    await admin.callback(name)(interaction)
    assert "hasn't set up newsbot yet" in interaction.text
    make_guild(v3_db, GUILD_A, set_up=False, games=[("palworld", 5)])
    interaction = FakeInteraction()
    await admin.callback(name)(interaction)
    assert "hasn't set up newsbot yet" in interaction.text
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, set_up=True)
        repo.unfollow_game(conn, GUILD_A, "palworld")
    interaction = FakeInteraction()
    await admin.callback(name)(interaction)
    assert "doesn't follow any games" in interaction.text
    assert digest_calls.runs == []


@pytest.mark.parametrize("name", ["run-now", "preview"])
async def test_digest_commands_refuse_while_this_server_is_busy(admin, v3_db, digest_calls, name):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    await admin.deps.lock_for(GUILD_A).acquire()
    interaction = FakeInteraction()
    await admin.callback(name)(interaction)
    assert "already running" in interaction.text
    assert digest_calls.runs == []


async def test_another_servers_busy_lock_does_not_block_this_one(admin, v3_db, digest_calls):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    await admin.deps.lock_for(GUILD_B).acquire()
    interaction = FakeInteraction()
    await admin.callback("run-now")(interaction)
    assert digest_calls.runs


async def test_digest_commands_without_wired_deps(v3_cfg, v3_db, problems):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    group = make_guild_admin_group(v3_cfg, SimpleNamespace(db_path=v3_db))
    for name in ("run-now", "preview"):
        interaction = FakeInteraction()
        await command(group, name).callback(interaction)
        assert "isn't available yet" in interaction.text


async def test_preview_shows_each_message_privately(admin, v3_db, digest_calls):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    embed = discord.Embed(title="Palworld")
    digest_calls.preview = SimpleNamespace(
        rendered=SimpleNamespace(messages=[SimpleNamespace(channel_id=5, embed=embed)]), notes=[]
    )
    interaction = FakeInteraction()
    await admin.callback("preview")(interaction)
    assert interaction.sent[-1]["content"] == "Would post in <#5>:"
    assert interaction.sent[-1]["embed"] is embed
    assert interaction.sent[-1]["ephemeral"] is True


@pytest.mark.parametrize(
    "empty", [None, SimpleNamespace(rendered=SimpleNamespace(messages=[]), notes=[])]
)
async def test_preview_with_nothing_to_show(admin, v3_db, digest_calls, empty):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.preview = empty
    interaction = FakeInteraction()
    await admin.callback("preview")(interaction)
    assert "Nothing would post" in interaction.text


@pytest.mark.parametrize("name", ["run-now", "preview"])
async def test_a_broken_time_zone_gets_the_fix_it_message(admin, v3_db, name):
    # The real digest functions, not stubs: GuildTimeZoneError is theirs to raise.
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, timezone="Mars/Olympus")
    interaction = FakeInteraction()
    await admin.callback(name)(interaction)
    assert interaction.text == "Your time zone setting is invalid; fix it with /newsbot settings."


async def test_a_broken_time_zone_during_the_run_itself(admin, v3_db, monkeypatch, digest_calls):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])

    async def boom(deps, guild_id, **_):
        raise commands_module.GuildTimeZoneError("nope")

    monkeypatch.setattr(commands_module, "run_guild_digest", boom)
    interaction = FakeInteraction()
    await admin.callback("run-now")(interaction)
    assert "time zone setting is invalid" in interaction.text


# --- cross-guild isolation ---


async def test_a_full_session_in_b_leaves_a_untouched(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("rust", 5)], admin_channel_id=3)
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_A, enabled=True, channel_id=4, ping="none")
        repo.add_notice(conn, GUILD_A, "hello")
    before = snapshot(v3_db, GUILD_A)
    b = {"guild_id": GUILD_B}
    everyone = Choice(name="everyone", value="everyone")
    await admin.callback("follow")(
        FakeInteraction(**b), game="palworld", channel=channel(8, GUILD_B)
    )
    await admin.callback("follow")(FakeInteraction(**b), game="rust", channel=channel(9, GUILD_B))
    await admin.callback("unfollow")(FakeInteraction(**b), game="rust")
    await admin.callback("settings")(
        FakeInteraction(**b),
        time="03:00",
        timezone="Asia/Tokyo",
        admin_channel=channel(10, GUILD_B),
    )
    await admin.callback("shift")(FakeInteraction(**b), channel=channel(11, GUILD_B), ping=everyone)
    await admin.callback("settings")(FakeInteraction(**b), clear_admin_channel=True)
    await admin.callback("unfollow")(FakeInteraction(**b), game="palworld")
    assert snapshot(v3_db, GUILD_A) == before


async def test_b_cannot_point_its_settings_at_a_channel_of_a(admin, v3_db):
    make_guild(v3_db, GUILD_B)
    a_channel = channel(5, GUILD_A)  # a real channel, in the wrong server
    b = {"guild_id": GUILD_B}
    await admin.callback("follow")(FakeInteraction(**b), game="rust", channel=a_channel)
    await admin.callback("settings")(FakeInteraction(**b), admin_channel=a_channel)
    await admin.callback("shift")(FakeInteraction(**b), channel=a_channel)
    guild, games, shift, _ = snapshot(v3_db, GUILD_B)
    assert games == [] and shift is None and guild.admin_channel_id is None


# --- /lounge quote-now ---


@pytest.fixture
def lounge(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"guild_id": GUILD_A})
    calls = []

    async def run_guild_quote(guild_id, force):
        calls.append((guild_id, force))
        return SimpleNamespace(status="already_posted", message_id=None)

    bot = SimpleNamespace(db_path=v3_db, run_guild_quote=run_guild_quote)
    group = make_lounge_group(cfg, bot)
    return SimpleNamespace(call=command(group, "quote-now").callback, calls=calls, group=group)


def _lounge_row(db_path, guild_id, *, quote=True):
    from newsbot.store.models import LoungeSettings

    with closing(connect(db_path)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(guild_id, 50, True, "hi", quote, "08:00", [], None))


def test_the_lounge_group_shape(lounge):
    assert lounge.group.name == "lounge" and lounge.group.guild_only is True
    assert lounge.group.default_permissions.manage_guild is True
    assert [c.name for c in lounge.group.commands] == ["quote-now"]


async def test_lounge_quote_now_denies_non_admins(lounge, v3_db):
    make_guild(v3_db, GUILD_A)
    _lounge_row(v3_db, GUILD_A)
    interaction = FakeInteraction(permissions=MEMBER)
    await lounge.call(interaction)
    assert interaction.text == "You don't have permission to run this."
    assert lounge.calls == []


async def test_lounge_quote_now_needs_a_lounge_row_with_the_quote_on(lounge, v3_db):
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await lounge.call(interaction)
    assert "doesn't have a lounge quote set up" in interaction.text
    _lounge_row(v3_db, GUILD_A, quote=False)
    interaction = FakeInteraction()
    await lounge.call(interaction)
    assert "doesn't have a lounge quote set up" in interaction.text
    assert lounge.calls == []


async def test_lounge_quote_now_runs_the_invoking_guilds_own_quote(lounge, v3_db):
    make_guild(v3_db, GUILD_A)
    _lounge_row(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await lounge.call(interaction)
    assert lounge.calls == [(GUILD_A, False)]
    assert interaction.text == "Today's quote already posted."


async def test_lounge_quote_now_works_in_any_guild_with_a_lounge_row(lounge, v3_db):
    # Task 12 dropped task 9's "only the v2 lounge guild" guard: the run is keyed by guild.
    make_guild(v3_db, GUILD_B)
    _lounge_row(v3_db, GUILD_B)
    interaction = FakeInteraction(guild_id=GUILD_B)
    await lounge.call(interaction)
    assert lounge.calls == [(GUILD_B, False)]
    assert interaction.text == "Today's quote already posted."


async def test_lounge_quote_now_outside_a_server(lounge):
    interaction = FakeInteraction(guild_id=None)
    await lounge.call(interaction)
    assert "inside a server" in interaction.text
