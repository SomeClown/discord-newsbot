"""Adversarial tests for `/newsbot` and `/lounge` in the public app (plan task 9).

`test_guild_admin_commands.py` proves the commands work when a polite admin runs
them in one server. This file is the other guests: a member who found the command
anyway, an admin in server B with a very good arm, a channel of every shape Discord
makes, and a reply that would like to be two thousand and one characters long.
Real command callbacks, real temp-file SQLite, the `tests/v3_fakes.py` interactions.

The claims, in rough order of how much I'd mind being wrong:
  * a command run in A never changes B's rows or shows B's ids, names or notices;
  * a refused option writes nothing, including the good options beside it;
  * only text and news channels of the invoking server are ever stored;
  * every reply is ephemeral, mention-proof and inside Discord's limits.

Where a command does something the owner might want different, the test pins what it
does today and a comment says so. Anything that looked like a real defect would be
an `xfail(strict=True)`; there isn't one here, which I'm choosing to read as good
work rather than insufficient imagination. (Both are available. See git log.)

No gateway, no network.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest
from discord.app_commands import Choice
from v3_fakes import (
    ADMIN,
    GUILD_A,
    GUILD_B,
    MEMBER,
    FakeInteraction,
    channel,
    command,
    fake_bot,
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
from newsbot.bot.format import discord_len
from newsbot.bot.permissions import ChannelProblem, GuildCheck
from newsbot.pipeline.guild_digest import GuildDigestDeps
from newsbot.store import repo
from newsbot.store.db import connect

CT = discord.ChannelType
EVERYONE = Choice(name="everyone", value="everyone")
ROLE_PING = Choice(name="role", value="role")

# Markers that must never show up in a reply to server A. They're all server B's.
B_CHANNEL = 777000000000000001
B_ADMIN = 777000000000000002
B_SHIFT = 777000000000000003
B_ROLE = 777000000000000004
B_NOTICE = "B-SECRET-NOTICE-zebracorn"
# A real role id is a 17 to 20 digit snowflake; the table's CHECK insists on it.
ROLE_ID = 400000000000000008


# --- plumbing ---


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


def _deps(cfg, db_path):
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
    deps = _deps(v3_cfg, v3_db)
    bot = fake_bot(v3_db)
    group = make_guild_admin_group(v3_cfg, bot, digest_deps=lambda: deps)

    def get(name):
        return command(group, name)

    get.group = group
    get.deps = deps
    get.callback = lambda name: command(group, name).callback
    return get


def dump(db_path, guild_id=None):
    """Every row of every table that has a `guild_id`, optionally just one server's."""
    out = {}
    with closing(connect(db_path)) as conn:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not r[0].startswith("sqlite_") and "_fts" not in r[0]
        ]
        for table in tables:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]  # noqa: S608
            if "guild_id" not in columns:
                continue
            sql = f'SELECT * FROM "{table}"'  # noqa: S608
            params: tuple = ()
            if guild_id is not None:
                sql += " WHERE guild_id = ?"
                params = (guild_id,)
            out[table] = [tuple(r) for r in conn.execute(sql + " ORDER BY 1, 2", params)]
    return out


def every_table(db_path):
    """Every row of every table (minus FTS shadow tables): "nothing at all was written"."""
    out = {}
    with closing(connect(db_path)) as conn:
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            if table.startswith("sqlite_") or "_fts" in table:
                continue
            out[table] = [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}"')]  # noqa: S608
    return out


def seed_b(db_path):
    """Server B with a row of everything, so an accidental write to it has somewhere to land."""
    make_guild(
        db_path,
        GUILD_B,
        set_up=True,
        tier="comped",
        admin_channel_id=B_ADMIN,
        games=[("rust", B_CHANNEL), ("borderlands4", B_CHANNEL)],
    )
    with closing(connect(db_path)) as conn:
        repo.set_shift(conn, GUILD_B, enabled=True, channel_id=B_SHIFT, ping=str(B_ROLE))
        repo.add_notice(conn, GUILD_B, B_NOTICE)


def blob(interaction):
    """Everything a reply said, embeds and fields included, as one string."""
    parts = []
    for message in interaction.sent:
        parts.append(message.get("content") or "")
        embed = message.get("embed")
        if embed is not None:
            parts.append(embed.title or "")
            parts.append(embed.description or "")
            for field in embed.fields:
                parts.append(f"{field.name}\n{field.value}")
    return "\n".join(parts)


# --- authorization ---

ALL_OPTIONS = {
    "follow": {"game": "palworld", "channel": channel(5)},
    "unfollow": {"game": "rust"},
    "games": {},
    "settings": {
        "time": "03:00",
        "timezone": "Asia/Tokyo",
        "admin_channel": channel(6),
    },
    "shift": {
        "channel": channel(7),
        "enabled": True,
        "ping": ROLE_PING,
        "role": role(ROLE_ID),
    },
    "status": {},
    "preview": {},
    "run-now": {},
}


@pytest.mark.parametrize("name", sorted(ALL_OPTIONS))
@pytest.mark.parametrize(
    "perms",
    [
        MEMBER,
        discord.Permissions.none(),
        discord.Permissions(manage_channels=True, manage_roles=True, kick_members=True),
        discord.Permissions(manage_guild=False, send_messages=True, mention_everyone=True),
    ],
    ids=["member", "none", "other-staff-powers", "mention-everyone-is-not-admin"],
)
async def test_a_non_admin_with_every_option_filled_in_changes_nothing_anywhere(
    admin, v3_db, name, perms
):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("rust", 4)], admin_channel_id=3)
    seed_b(v3_db)
    before = every_table(v3_db)
    interaction = FakeInteraction(permissions=perms)
    await admin.callback(name)(interaction, **ALL_OPTIONS[name])
    assert interaction.text == "You don't have permission to run this."
    assert all(m["ephemeral"] is True for m in interaction.sent)
    assert every_table(v3_db) == before


async def test_a_bare_administrator_bit_counts_as_admin(admin, v3_db, problems):
    # Discord resolves Administrator into every bit before we see it, but the check
    # doesn't rely on that; pin it, because locking the server owner out would be a
    # memorable bug.
    interaction = FakeInteraction(permissions=discord.Permissions(administrator=True))
    await admin.callback("games")(interaction)
    assert "Following (0 of 10)" in interaction.text


async def test_the_lounge_command_denies_non_admins_before_reading_the_database(v3_cfg, v3_db):
    calls = []

    async def run_quote(guild_id, force):
        calls.append(force)

    group = make_lounge_group(v3_cfg, fake_bot(v3_db, run_guild_quote=run_quote))
    before = every_table(v3_db)
    interaction = FakeInteraction(permissions=MEMBER, guild_id=GUILD_A)
    await command(group, "quote-now").callback(interaction)
    assert interaction.text == "You don't have permission to run this."
    assert calls == [] and every_table(v3_db) == before


# --- autocomplete runs without the default-permission gate ---


def autocomplete_of(admin, name, param):
    return command(admin.group, name)._params[param].autocomplete


async def test_autocompletes_answer_a_plain_member_but_only_about_their_own_server(admin, v3_db):
    # default_permissions only hides the command in the client; a server admin can
    # open it to everyone, and autocomplete never re-checks Manage Server. So the
    # claim worth pinning is what it can reveal: the invoking server's own follows
    # and the public catalog, nothing of B's.
    make_guild(v3_db, GUILD_A, games=[("palworld", 4)])
    seed_b(v3_db)
    member = FakeInteraction(permissions=MEMBER)
    followed = await autocomplete_of(admin, "unfollow", "game")(member, "")
    assert [c.value for c in followed] == ["palworld"]
    available = await autocomplete_of(admin, "follow", "game")(member, "")
    assert [c.value for c in available] == ["borderlands4", "rust"]
    # B follows rust and borderlands4; A sees them only because they're catalog entries.
    assert str(B_CHANNEL) not in repr(followed) + repr(available)


async def test_autocompletes_in_a_dm_answer_nothing_about_any_server(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 4)])
    dm = FakeInteraction(guild_id=None, permissions=MEMBER)
    assert await autocomplete_of(admin, "unfollow", "game")(dm, "") == []
    assert await autocomplete_of(admin, "follow", "game")(dm, "") == []


async def test_autocomplete_is_read_only_even_for_a_server_with_no_row(admin, v3_db):
    before = every_table(v3_db)
    member = FakeInteraction(permissions=MEMBER)
    await autocomplete_of(admin, "follow", "game")(member, "")
    await autocomplete_of(admin, "unfollow", "game")(member, "")
    await autocomplete_of(admin, "settings", "timezone")(member, "tokyo")
    assert every_table(v3_db) == before


async def test_the_follow_autocomplete_hides_a_followed_game_however_it_is_cased(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 4)])
    got = await autocomplete_of(admin, "follow", "game")(FakeInteraction(), "PALWORLD")
    assert got == []
    got = await autocomplete_of(admin, "follow", "game")(FakeInteraction(), "RUST")
    assert [c.value for c in got] == ["rust"]


@pytest.mark.parametrize("current", ["", "a", "zz", "%", "_", "\ud800", "x" * 10_000, "\x00"])
def test_zone_autocomplete_stays_inside_discords_choice_limits(current):
    got = zone_choices(current)
    assert len(got) <= 25
    for choice in got:
        assert 1 <= len(choice.name) <= 100 and 1 <= len(choice.value) <= 100


def test_zone_autocomplete_reveals_only_public_zone_names():
    # It needs no server at all (it runs fine in a DM); the only thing it can hand out
    # is tzdata's own list.
    assert {c.value for c in zone_choices("")} <= {
        resolve_timezone(c.value) for c in zone_choices("")
    }


# --- cross-guild isolation: every command, every option, invoked in A ---


@pytest.mark.parametrize("name", sorted(ALL_OPTIONS))
async def test_a_command_in_a_leaves_b_untouched_and_never_shows_b(
    admin, v3_db, problems, name, monkeypatch
):
    ran = []

    async def run(deps, guild_id, **_):
        ran.append(guild_id)
        return SimpleNamespace(status="ok", notes=[])

    async def today(deps, guild_id):
        return None

    monkeypatch.setattr(commands_module, "run_guild_digest", run)
    monkeypatch.setattr(commands_module, "todays_guild_digest", today)
    make_guild(v3_db, GUILD_A, set_up=True, games=[("palworld", 4)], admin_channel_id=3)
    seed_b(v3_db)
    before_b = dump(v3_db, GUILD_B)
    interaction = FakeInteraction()
    await admin.callback(name)(interaction, **ALL_OPTIONS[name])
    assert dump(v3_db, GUILD_B) == before_b
    text = blob(interaction)
    for secret in (B_CHANNEL, B_ADMIN, B_SHIFT, B_ROLE, GUILD_B, B_NOTICE):
        assert str(secret) not in text
    assert set(problems.asked) <= {GUILD_A} and set(ran) <= {GUILD_A}


async def test_the_shift_command_in_a_with_all_options_does_not_touch_bs_ping_budget(admin, v3_db):
    seed_b(v3_db)
    with closing(connect(v3_db)) as conn, conn:
        conn.execute(
            "UPDATE guild_shift SET ping_day = '2026-09-30', ping_count = 2 WHERE guild_id = ?",
            (GUILD_B,),
        )
    before = dump(v3_db, GUILD_B)
    make_guild(v3_db, GUILD_A)
    await admin.callback("shift")(FakeInteraction(), **ALL_OPTIONS["shift"])
    assert dump(v3_db, GUILD_B) == before


async def test_a_real_preview_in_a_shows_none_of_bs_games(admin, v3_db):
    from newsbot.store.models import StoredItem

    make_guild(v3_db, GUILD_A, set_up=True, games=[("palworld", 4)])
    seed_b(v3_db)
    with closing(connect(v3_db)) as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    "https://e.example/pal",
                    "Palworld zebracorn",
                    "x",
                    "Feed",
                    "official",
                    None,
                    {"palworld": False},
                ),
                StoredItem(
                    "https://e.example/rust",
                    "Rust quokka",
                    "x",
                    "Feed",
                    "official",
                    None,
                    {"rust": False},
                ),
            ],
            now=lambda: datetime(2026, 9, 30, 16, 0, tzinfo=UTC),
        )
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("preview")(interaction)
    text = blob(interaction)
    assert "quokka" not in text and str(B_CHANNEL) not in text
    assert every_table(v3_db) == before  # a preview writes nothing, anywhere


async def test_status_in_a_with_bs_notices_and_failing_sources_shows_only_health_counts(
    admin, v3_db
):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 4)])
    seed_b(v3_db)
    with closing(connect(v3_db)) as conn, conn:
        conn.execute(
            "INSERT INTO source_health (source_name, consecutive_failures, last_error) "
            "VALUES ('Borderlands 4 Steam', 5, 'https://secret.example/feed exploded')"
        )
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    text = blob(interaction)
    assert "of 4 sources ok" in text
    assert "secret.example" not in text and "exploded" not in text and B_NOTICE not in text


# --- a refused option writes nothing, including the good options beside it ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time": "03:00", "timezone": "Mars/Base"},
        {"timezone": "Asia/Tokyo", "time": "24:00"},
        {"time": "03:00", "timezone": "Asia/Tokyo", "admin_channel": channel(6, GUILD_B)},
        {"time": "03:00", "timezone": "Asia/Tokyo", "admin_channel": thin_channel(6, GUILD_B)},
        {"time": "03:00", "admin_channel": channel(6, GUILD_A, CT.voice)},
        {
            "time": "03:00",
            "timezone": "Asia/Tokyo",
            "admin_channel": channel(6),
            "clear_admin_channel": True,
        },
    ],
    ids=[
        "bad-zone",
        "bad-time",
        "foreign-channel",
        "foreign-thin",
        "voice-channel",
        "both-admin-options",
    ],
)
async def test_settings_with_one_refused_option_saves_none_of_them(admin, v3_db, problems, kwargs):
    make_guild(v3_db, GUILD_A, set_up=True, admin_channel_id=3)
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, **kwargs)
    assert every_table(v3_db) == before
    assert "Saved" not in interaction.text
    assert problems.asked == []  # nothing changed, so nothing to re-check


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channel": channel(7), "ping": ROLE_PING},  # no role
        {"channel": channel(7), "ping": EVERYONE, "role": role(ROLE_ID)},
        {"channel": channel(7), "ping": ROLE_PING, "role": role(ROLE_ID, GUILD_B)},
        {"channel": channel(7), "ping": ROLE_PING, "role": role(ROLE_ID, default=True)},
        {"channel": channel(7, GUILD_B), "enabled": True},
        {"enabled": True},  # on, with no channel anywhere
        {"role": role(ROLE_ID), "channel": thin_channel(7, GUILD_B)},
    ],
    ids=[
        "no-role",
        "role-with-everyone",
        "foreign-role",
        "everyone-role",
        "foreign-channel",
        "no-channel",
        "thin-foreign",
    ],
)
async def test_shift_with_one_refused_option_saves_none_of_them(admin, v3_db, problems, kwargs):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 4)])
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, **kwargs)
    assert every_table(v3_db) == before
    assert "Saved" not in interaction.text
    assert problems.asked == []


async def test_follow_with_a_bad_channel_does_not_create_the_missing_row_either(
    admin, v3_db, problems
):
    before = every_table(v3_db)
    await admin.callback("follow")(FakeInteraction(), game="palworld", channel=channel(5, GUILD_B))
    assert every_table(v3_db) == before  # no guilds row, no games row


async def test_settings_with_nothing_to_change_creates_no_row(admin, v3_db, problems):
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction)
    assert "Nothing to change" in interaction.text
    assert every_table(v3_db) == before


# --- channel validation ---

_ACCEPTED = {CT.text, CT.news}


@pytest.mark.parametrize("kind", list(CT), ids=lambda k: k.name)
@pytest.mark.parametrize(
    "shape", [channel, lambda i, g, k: thin_channel(i, g, k)], ids=["resolved", "partial"]
)
def test_only_text_and_news_channels_of_this_server_pass(kind, shape):
    got = channel_problem(shape(5, GUILD_A, kind), GUILD_A)
    assert (got is None) == (kind in _ACCEPTED), kind.name


@pytest.mark.parametrize("kind", list(CT), ids=lambda k: k.name)
def test_no_channel_type_is_accepted_from_another_server(kind):
    assert channel_problem(channel(5, GUILD_B, kind), GUILD_A) is not None
    assert channel_problem(thin_channel(5, GUILD_B, kind), GUILD_A) is not None


@pytest.mark.parametrize(
    "odd",
    [
        SimpleNamespace(id=5, type=CT.text),  # says nothing about where it lives
        SimpleNamespace(id=5, type=CT.text, guild=None),
        SimpleNamespace(id=5, type=CT.text, guild=SimpleNamespace(id=None)),
        SimpleNamespace(
            id=5, type=CT.text, guild=SimpleNamespace(id=str(GUILD_A))
        ),  # text, not int
        SimpleNamespace(id=5, type=CT.text, guild_id=str(GUILD_A)),
        SimpleNamespace(id=5, type=CT.text, guild_id=None),
        SimpleNamespace(id=5, type=0, guild_id=GUILD_A),  # the raw number, not the enum
        SimpleNamespace(id=5, type="text", guild_id=GUILD_A),
        SimpleNamespace(id=5, guild_id=GUILD_A),  # no type at all
        SimpleNamespace(id=0, type=CT.text, guild_id=GUILD_A),
        SimpleNamespace(id=-7, type=CT.text, guild_id=GUILD_A),
        SimpleNamespace(id=True, type=CT.text, guild_id=GUILD_A),
        SimpleNamespace(id="5", type=CT.text, guild_id=GUILD_A),
        SimpleNamespace(id=None, type=CT.text, guild_id=GUILD_A),
        SimpleNamespace(type=CT.text, guild_id=GUILD_A),  # no id
        object(),
        None,
    ],
)
def test_a_channel_that_cannot_be_tied_to_this_server_is_refused(odd):
    assert channel_problem(odd, GUILD_A) is not None


def test_a_resolved_channel_is_judged_by_its_guild_even_if_a_stale_guild_id_disagrees():
    # `.guild` wins when both are there. A channel cached under B but carrying A's
    # guild_id (nothing real does this) is still B's.
    odd = SimpleNamespace(id=5, type=CT.text, guild=SimpleNamespace(id=GUILD_B), guild_id=GUILD_A)
    assert channel_problem(odd, GUILD_A) is not None


@pytest.mark.parametrize("guild_id", [None, 0])
def test_no_server_means_no_channel_passes(guild_id):
    assert channel_problem(channel(5, GUILD_A), guild_id) is not None
    assert channel_problem(channel(5, None), guild_id) is not None


@pytest.mark.parametrize(
    "kind",
    [CT.news_thread, CT.private_thread, CT.public_thread, CT.stage_voice, CT.category, CT.forum],
)
@pytest.mark.parametrize("which", ["follow", "settings", "shift"])
async def test_no_command_stores_a_thread_stage_category_or_forum(
    admin, v3_db, problems, kind, which
):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 4)])
    before = every_table(v3_db)
    target = channel(9, GUILD_A, kind)
    calls = {
        "follow": {"game": "palworld", "channel": target},
        "settings": {"admin_channel": target},
        "shift": {"channel": target},
    }
    interaction = FakeInteraction()
    await admin.callback(which)(interaction, **calls[which])
    assert every_table(v3_db) == before


async def test_a_news_channel_is_accepted_everywhere_a_text_channel_is(admin, v3_db):
    make_guild(v3_db, GUILD_A, set_up=True)
    news = channel(9, GUILD_A, CT.news)
    await admin.callback("follow")(FakeInteraction(), game="palworld", channel=news)
    await admin.callback("settings")(FakeInteraction(), admin_channel=news)
    await admin.callback("shift")(FakeInteraction(), channel=news, enabled=True)
    dumped = dump(v3_db, GUILD_A)
    assert dumped["guild_games"][0][2] == 9
    assert dumped["guild_shift"][0][2] == 9
    with closing(connect(v3_db)) as conn:
        assert repo.get_guild(conn, GUILD_A).admin_channel_id == 9


async def test_a_refused_channel_reply_names_neither_server_nor_channel(admin, v3_db):
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await admin.callback("follow")(
        interaction, game="palworld", channel=channel(B_CHANNEL, GUILD_B)
    )
    assert str(B_CHANNEL) not in interaction.text and str(GUILD_B) not in interaction.text


# --- roles ---


def _fake_world(guild_id, channel_id, *, roles, perms):
    class Guild:
        id = guild_id
        me = "bot"

        def get_role(self, role_id):
            return roles.get(role_id)

    guild = Guild()

    class Chan(discord.TextChannel):
        def __init__(self):
            self.guild = guild

        def permissions_for(self, member):
            return perms

    chan = Chan()

    class Client:
        db_path = None

        def get_guild(self, gid):
            return guild if gid == guild_id else None

        def get_channel(self, cid):
            return chan if cid == channel_id else None

        async def fetch_channel(self, cid):
            raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Channel")

    return Client()


NO_MENTION = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
WITH_MENTION = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, mention_everyone=True
)


def _bot_for(v3_db, **world):
    client = _fake_world(GUILD_A, 7, **world)
    client.db_path = v3_db
    return client


async def _shift_role_reply(v3_cfg, v3_db, *, roles, perms, picked):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 7)])
    bot = _bot_for(v3_db, roles=roles, perms=perms)
    group = make_guild_admin_group(v3_cfg, bot)
    interaction = FakeInteraction()
    await command(group, "shift").callback(
        interaction, channel=channel(7), ping=ROLE_PING, role=picked
    )
    return interaction


async def test_pinging_a_role_the_bot_cannot_mention_warns_in_the_reply(v3_cfg, v3_db):
    picked = role(ROLE_ID)
    interaction = await _shift_role_reply(
        v3_cfg,
        v3_db,
        roles={ROLE_ID: SimpleNamespace(mentionable=False)},
        perms=NO_MENTION,
        picked=picked,
    )
    assert "Saved: SHiFT alerts are on." in interaction.text
    assert "Mention @everyone" in interaction.text  # the warning rides along with the save


async def test_a_mentionable_role_needs_no_warning(v3_cfg, v3_db):
    interaction = await _shift_role_reply(
        v3_cfg,
        v3_db,
        roles={ROLE_ID: SimpleNamespace(mentionable=True)},
        perms=NO_MENTION,
        picked=role(ROLE_ID),
    )
    assert interaction.text == "Saved: SHiFT alerts are on."


async def test_an_unmentionable_role_is_fine_when_the_bot_may_mention_everyone(v3_cfg, v3_db):
    interaction = await _shift_role_reply(
        v3_cfg,
        v3_db,
        roles={ROLE_ID: SimpleNamespace(mentionable=False)},
        perms=WITH_MENTION,
        picked=role(ROLE_ID),
    )
    assert interaction.text == "Saved: SHiFT alerts are on."


async def test_a_role_the_bot_cannot_see_is_saved_and_the_reply_says_it_is_gone(v3_cfg, v3_db):
    # The command only knows the role's id and guild; "the bot's own cache has never
    # heard of it" is the live check's job, and it reports it after the save.
    interaction = await _shift_role_reply(
        v3_cfg, v3_db, roles={}, perms=WITH_MENTION, picked=role(ROLE_ID)
    )
    assert "no longer exists" in interaction.text


async def test_managed_and_above_the_bots_top_role_are_accepted_without_a_hierarchy_warning(
    v3_cfg, v3_db
):
    # Mentioning doesn't depend on role position or on a role being bot-managed, only
    # on `mentionable` and the Mention @everyone permission, so neither gets a warning.
    # Pinned so a future "helpful" hierarchy check is a deliberate decision.
    picked = SimpleNamespace(
        id=ROLE_ID,
        guild=SimpleNamespace(id=GUILD_A),
        is_default=lambda: False,
        managed=True,
        position=999,
    )
    interaction = await _shift_role_reply(
        v3_cfg,
        v3_db,
        roles={ROLE_ID: SimpleNamespace(mentionable=True)},
        perms=NO_MENTION,
        picked=picked,
    )
    assert interaction.text == "Saved: SHiFT alerts are on."
    assert dump(v3_db, GUILD_A)["guild_shift"][0][3] == str(ROLE_ID)


@pytest.mark.parametrize(
    "odd",
    [
        SimpleNamespace(id=ROLE_ID, guild=None, is_default=lambda: False),
        SimpleNamespace(id=ROLE_ID, is_default=lambda: False),
        SimpleNamespace(
            id=ROLE_ID, guild=SimpleNamespace(id=str(GUILD_A)), is_default=lambda: False
        ),
    ],
)
async def test_a_role_that_cannot_be_tied_to_this_server_is_refused(admin, v3_db, odd):
    make_guild(v3_db, GUILD_A, set_up=True)
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("shift")(interaction, channel=channel(7), ping=ROLE_PING, role=odd)
    assert "isn't in this server" in interaction.text
    assert every_table(v3_db) == before


# --- shift: enabled_at, ping memory ---


async def test_enabling_stamps_enabled_at_once_and_resaving_does_not_move_it(admin, v3_db):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 4)])
    await admin.callback("shift")(FakeInteraction(), channel=channel(7), enabled=True)
    first = dump(v3_db, GUILD_A)["guild_shift"][0]
    await asyncio.sleep(0.01)
    await admin.callback("shift")(FakeInteraction(), ping=EVERYONE)
    second = dump(v3_db, GUILD_A)["guild_shift"][0]
    assert first[4] is not None and second[4] == first[4]
    await admin.callback("shift")(FakeInteraction(), enabled=False)
    await asyncio.sleep(0.01)
    await admin.callback("shift")(FakeInteraction(), enabled=True)
    third = dump(v3_db, GUILD_A)["guild_shift"][0]
    assert third[4] > first[4]  # off then on starts from now, not from the old backlog


async def test_switching_from_a_role_ping_to_everyone_replaces_the_stored_role(admin, v3_db):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 4)])
    await admin.callback("shift")(
        FakeInteraction(), channel=channel(7), ping=ROLE_PING, role=role(ROLE_ID)
    )
    await admin.callback("shift")(FakeInteraction(), ping=EVERYONE)
    assert dump(v3_db, GUILD_A)["guild_shift"][0][3] == "everyone"
    await admin.callback("shift")(FakeInteraction(), ping=Choice(name="none", value="none"))
    assert dump(v3_db, GUILD_A)["guild_shift"][0][3] == "none"


# --- settings: time and zone edge cases ---


@pytest.mark.parametrize("text", ["00:00", "09:00", "23:59", "12:34"])
def test_good_times_pass_untouched(text):
    assert parse_digest_time(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "24:00",
        "9:00",
        "09:60",
        "09:5",
        "0900",
        "09-00",
        "09:00:00",
        "９:００",
        "０９:００",  # fullwidth digits
        "09:00​",  # zero-width space survives strip()
        "​09:00",
        "٠٩:٠٠",  # Arabic-Indic digits
        "",
        "  ",
        "09:00\x00",
        "99:99",
        "-1:00",
        "+9:00",
        "a" * 1000,
    ],
)
async def test_bad_times_are_refused_and_nothing_is_saved(admin, v3_db, problems, text):
    make_guild(v3_db, GUILD_A, set_up=True)
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, time=text)
    assert "HH:MM" in interaction.text
    assert every_table(v3_db) == before


@pytest.mark.parametrize("text", [" 09:00", "09:00 ", "\t09:00\n"])
async def test_a_time_with_stray_whitespace_is_accepted_and_stored_clean(admin, v3_db, text):
    # `parse_digest_time` is strict, the command trims first. Kinder than the docstring
    # suggests; pinned so nobody "fixes" one side without noticing the other.
    make_guild(v3_db, GUILD_A, set_up=True)
    await admin.callback("settings")(FakeInteraction(), time=text)
    with closing(connect(v3_db)) as conn:
        assert repo.get_guild(conn, GUILD_A).digest_time == "09:00"


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("UTC", "UTC"),
        ("utc", "UTC"),
        (" America/Chicago ", "America/Chicago"),
        ("america/chicago", "America/Chicago"),
        ("Etc/GMT+5", "Etc/GMT+5"),
        ("America/Argentina/ComodRivadavia", "America/Argentina/ComodRivadavia"),
        ("US/Pacific", "US/Pacific"),
        ("Factory", "Factory"),  # tzdata's "this zone is a placeholder"; accepted today
    ],
)
async def test_real_zones_are_stored_under_their_canonical_spelling(admin, v3_db, given, stored):
    make_guild(v3_db, GUILD_A, set_up=True)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, timezone=given)
    with closing(connect(v3_db)) as conn:
        assert repo.get_guild(conn, GUILD_A).timezone == stored
    assert f"time zone {stored}" in interaction.text


@pytest.mark.parametrize(
    "text",
    [
        "Mars/Base",
        "",
        " ",
        "x" * 1000,
        "../../etc/passwd",
        "/etc/localtime",
        "UTC\x00",
        "America/Chicago/../UTC",
        "localtime",
        "posixrules",
        "America",  # a directory in tzdata, not a zone
        "GMT+5",
        "\ud800",
        "ＵＴＣ",
    ],
)
async def test_bad_zones_are_refused_and_nothing_is_saved(admin, v3_db, problems, text):
    make_guild(v3_db, GUILD_A, set_up=True)
    before = every_table(v3_db)
    interaction = FakeInteraction()
    await admin.callback("settings")(interaction, timezone=text)
    assert "isn't a time zone I know" in interaction.text
    assert every_table(v3_db) == before
    assert len(text.strip()) <= 7 or text.strip() not in interaction.text  # no echo


@pytest.mark.parametrize("name", sorted(__import__("zoneinfo").available_timezones()))
def test_every_zone_tzdata_lists_resolves_to_itself(name):
    # A zone the autocomplete offers must be one the command accepts. Ubuntu's tzdata
    # also lists `localtime`, which is the host's zone wearing a fake moustache; that
    # one is refused on purpose (see the bad-zones test above).
    if name.casefold() in {"localtime", "posixrules"}:
        assert resolve_timezone(name) is None
    else:
        assert resolve_timezone(name) == name


def test_not_really_zones_are_refused_even_when_tzdata_lists_them(monkeypatch):
    # macOS doesn't ship `localtime` in zoneinfo, so fake a Linux-shaped list to prove
    # the filter, not the platform, is what refuses it.
    import zoneinfo

    from newsbot.bot import commands

    monkeypatch.setattr(
        zoneinfo, "available_timezones", lambda: {"UTC", "Factory", "localtime", "posixrules"}
    )
    commands._zone_index.cache_clear()
    try:
        assert resolve_timezone("UTC") == "UTC"
        assert resolve_timezone("Factory") == "Factory"  # odd, but a real tzdata zone
        for name in ("localtime", "posixrules"):
            assert resolve_timezone(name) is None
        assert [c.value for c in commands.zone_choices("local")] == []
    finally:
        commands._zone_index.cache_clear()


# --- follow edge cases ---


@pytest.mark.parametrize("given", ["PALWORLD", "Palworld", "  palworld  ", "pAlWoRlD"])
async def test_follow_accepts_a_key_or_name_in_any_case_and_stores_the_catalog_key(
    admin, v3_db, given
):
    await admin.callback("follow")(FakeInteraction(), game=given, channel=channel(5))
    assert [row[1] for row in dump(v3_db, GUILD_A)["guild_games"]] == ["palworld"]


@pytest.mark.parametrize(
    "given", ["all", "ALL", "*", "%", "palworld,rust", "palworld\x00", "\ud800", "p" * 10_000]
)
async def test_follow_refuses_anything_that_is_not_one_catalog_game(admin, v3_db, given):
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game=given, channel=channel(5))
    assert "don't know that game" in interaction.text
    assert dump(v3_db, GUILD_A).get("guild_games", []) == []
    assert len(given) <= 7 or given not in interaction.text  # no echo


async def test_following_the_same_game_twice_leaves_one_row_and_the_second_channel(admin, v3_db):
    await admin.callback("follow")(FakeInteraction(), game="rust", channel=channel(5))
    second = FakeInteraction()
    await admin.callback("follow")(second, game="RUST", channel=channel(6))
    rows = dump(v3_db, GUILD_A)["guild_games"]
    assert [(r[1], r[2]) for r in rows] == [("rust", 6)]
    assert "Moved Rust" in second.text


async def test_the_eleventh_game_is_refused_and_a_twelfth_attempt_leaves_ten(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[(f"game{i}", 100 + i) for i in range(10)])
    before = dump(v3_db, GUILD_A)
    for game in ("palworld", "rust"):
        interaction = FakeInteraction()
        await admin.callback("follow")(interaction, game=game, channel=channel(5))
        assert "already follows 10 games" in interaction.text
    assert dump(v3_db, GUILD_A) == before


async def test_unfollow_is_case_blind_and_never_touches_another_servers_follow(admin, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 4)])
    make_guild(v3_db, GUILD_B, games=[("palworld", 9), ("rust", 9)])
    before_b = dump(v3_db, GUILD_B)
    await admin.callback("unfollow")(FakeInteraction(), game="PALWORLD")
    assert dump(v3_db, GUILD_A)["guild_games"] == []
    assert dump(v3_db, GUILD_B) == before_b
    interaction = FakeInteraction()
    await admin.callback("unfollow")(interaction, game="rust")  # B follows it; A doesn't
    assert "doesn't follow that game" in interaction.text
    assert dump(v3_db, GUILD_B) == before_b


# --- replies: limits, ephemerality, mentions ---


async def test_a_flood_of_permission_problems_is_cut_to_discords_limit(admin, v3_db, problems):
    problems.lines = ["\U0001f600" * 120 + " @everyone <@&1>" for _ in range(40)]
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(5))
    for message in interaction.sent:
        assert discord_len(message["content"]) <= 2000
        assert message["ephemeral"] is True
        assert message["allowed_mentions"].everyone is False
    assert "Now following Palworld" in interaction.text  # the head survives the cut


@pytest.mark.parametrize("name", ["follow", "settings", "shift"])
async def test_every_saving_command_reports_problems_inside_the_limit(admin, v3_db, problems, name):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("borderlands4", 4)])
    problems.lines = ["x" * 500 for _ in range(10)]
    interaction = FakeInteraction()
    await admin.callback(name)(
        interaction,
        **{
            "follow": {"game": "rust", "channel": channel(5)},
            "settings": {"time": "10:00"},
            "shift": {"channel": channel(5), "enabled": True},
        }[name],
    )
    assert all(discord_len(m["content"]) <= 2000 for m in interaction.sent)
    assert "But I can't do everything I'm set up to do" in interaction.text


async def test_no_problem_lines_means_a_plain_saved_reply(admin, v3_db, problems):
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="rust", channel=channel(5))
    assert "But I can't do everything" not in interaction.text


async def test_a_problem_line_is_posted_verbatim_under_the_saved_line(admin, v3_db, problems):
    problems.lines = ["palworld channel <#5>: missing Embed Links"]
    interaction = FakeInteraction()
    await admin.callback("follow")(interaction, game="palworld", channel=channel(5))
    assert interaction.text.endswith("- palworld channel <#5>: missing Embed Links")


async def test_status_stays_inside_the_embed_limits_with_hostile_stored_text(admin, v3_db):
    make_guild(
        v3_db,
        GUILD_A,
        set_up=True,
        tier="comped",
        games=[("borderlands4", 4), ("palworld", 5), ("rust", 6)],
    )
    with closing(connect(v3_db)) as conn:
        for i in range(8):
            repo.add_notice(conn, GUILD_A, f"@everyone <@&{i}> " + "\U0001f600" * 900)
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    embed = interaction.sent[-1]["embed"]
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert discord_len(field.value) <= 1024 and discord_len(field.name) <= 256
    assert interaction.sent[-1]["ephemeral"] is True
    assert interaction.sent[-1]["allowed_mentions"].everyone is False


async def test_status_never_contains_a_secret_or_a_dollar_sign(admin, v3_db, v3_cfg):
    make_guild(v3_db, GUILD_A, set_up=True, tier="comped", games=[("borderlands4", 4)])
    with closing(connect(v3_db)) as conn, conn:
        conn.execute(
            "INSERT INTO source_health (source_name, consecutive_failures, last_error) "
            "VALUES ('Borderlands 4 Steam', 9, 'key=test-key token abc')"
        )
    interaction = FakeInteraction()
    await admin.callback("status")(interaction)
    text = blob(interaction)
    assert "test-key" not in text and "$" not in text and "token" not in text.lower()


async def test_games_reply_fits_even_with_the_whole_catalog_listed(v3_cfg, v3_db, problems):
    game = v3_cfg.catalog[0]
    big = [
        game.model_copy(update={"key": f"g{i}", "name": f"Game {i} " + "n" * 60}) for i in range(25)
    ]
    cfg = v3_cfg.model_copy(update={"catalog": big})
    group = make_guild_admin_group(cfg, fake_bot(v3_db))
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction()
    await command(group, "games").callback(interaction)
    assert all(discord_len(m["content"]) <= 2000 for m in interaction.sent)
    assert all(m["ephemeral"] for m in interaction.sent)


# --- preview and run-now without wiring, and DMs ---


@pytest.mark.parametrize("name", ["preview", "run-now"])
async def test_digest_commands_without_deps_say_not_available_and_touch_nothing(
    v3_cfg, v3_db, problems, name
):
    make_guild(v3_db, GUILD_A, set_up=True, games=[("palworld", 4)])
    before = every_table(v3_db)
    group = make_guild_admin_group(v3_cfg, fake_bot(v3_db))
    interaction = FakeInteraction()
    await command(group, name).callback(interaction)
    assert "isn't available yet" in interaction.text
    assert interaction.sent[-1]["ephemeral"] is True
    assert every_table(v3_db) == before


async def test_the_wiring_hook_on_the_bot_is_used_when_no_deps_are_passed(v3_cfg, v3_db, problems):
    # `bot.guild_digest_deps` is what task 13 will add. Pin that the factory is called
    # fresh each time (the shared object lives behind it, not in the command).
    make_guild(v3_db, GUILD_A, set_up=True, games=[("palworld", 4)])
    calls = []
    deps = _deps(v3_cfg, v3_db)

    def factory():
        calls.append(1)
        return deps

    bot = fake_bot(v3_db, guild_digest_deps=factory)
    group = make_guild_admin_group(v3_cfg, bot)
    for _ in range(2):
        await command(group, "preview").callback(FakeInteraction())
    assert len(calls) == 2


@pytest.mark.parametrize("name", sorted(ALL_OPTIONS))
async def test_a_dm_is_refused_before_anything_else_happens(admin, v3_db, name):
    before = every_table(v3_db)
    interaction = FakeInteraction(guild_id=None, permissions=ADMIN)
    await admin.callback(name)(interaction, **ALL_OPTIONS[name])
    assert interaction.text == "This only works inside a server."
    assert interaction.sent[0]["ephemeral"] is True
    assert every_table(v3_db) == before


async def test_a_dm_even_with_no_permissions_gets_the_server_only_reply_not_a_denial(admin, v3_db):
    # Pin the order: "not in a server" is checked before "not an admin".
    interaction = FakeInteraction(guild_id=None, permissions=discord.Permissions.none())
    await admin.callback("settings")(interaction, time="10:00")
    assert interaction.text == "This only works inside a server."


# --- /lounge quote-now ---


def _lounge_row(db_path, guild_id, *, quote=True):
    from newsbot.store.models import LoungeSettings

    with closing(connect(db_path)) as conn:
        repo.upsert_lounge(conn, LoungeSettings(guild_id, 50, True, "hi", quote, "08:00", [], None))


@pytest.fixture
def lounge(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"guild_id": GUILD_A})
    calls = []

    async def run_quote(guild_id, force):
        calls.append((guild_id, force))
        return SimpleNamespace(status="posted", message_id=123)

    bot = fake_bot(v3_db, run_guild_quote=run_quote)
    group = make_lounge_group(cfg, bot)
    return SimpleNamespace(call=command(group, "quote-now").callback, calls=calls, cfg=cfg)


async def test_quote_now_runs_in_any_guild_with_a_quote_row_and_only_with_one(lounge, v3_db):
    # Since task 12 the run is keyed by the invoking guild, so B (not `cfg.guild_id`)
    # with a lounge row is served; A has no lounge row and gets nothing.
    make_guild(v3_db, GUILD_A)
    make_guild(v3_db, GUILD_B)
    _lounge_row(v3_db, GUILD_B)
    interaction = FakeInteraction(guild_id=GUILD_B)
    await lounge.call(interaction)
    assert lounge.calls == [(GUILD_B, False)]
    interaction = FakeInteraction(guild_id=GUILD_A)
    await lounge.call(interaction)
    assert lounge.calls == [(GUILD_B, False)]


async def test_quote_now_with_the_quote_turned_off_does_nothing(lounge, v3_db):
    make_guild(v3_db, GUILD_A)
    _lounge_row(v3_db, GUILD_A, quote=False)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await lounge.call(interaction)
    assert lounge.calls == []
    assert "doesn't have a lounge quote set up" in interaction.text


async def test_quote_now_with_a_broken_stored_zone_says_so_instead_of_crashing(lounge, v3_db):
    make_guild(v3_db, GUILD_A)
    _lounge_row(v3_db, GUILD_A)
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, timezone="Mars/Olympus")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await lounge.call(interaction)
    assert "time zone setting is invalid" in interaction.text
    assert lounge.calls == []


async def test_quote_now_replies_are_ephemeral_and_mention_proof(lounge, v3_db):
    make_guild(v3_db, GUILD_A)
    _lounge_row(v3_db, GUILD_A)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await lounge.call(interaction)
    assert lounge.calls == [(GUILD_A, False)]
    for message in interaction.sent:
        assert message["ephemeral"] is True
        assert message["allowed_mentions"].everyone is False


async def test_the_isolation_helpers_can_actually_see_rows(v3_db):
    seed_b(v3_db)
    tables = dump(v3_db, GUILD_B)
    assert {"guilds", "guild_games", "guild_shift", "guild_notices"} <= set(tables)
    assert all(tables[t] for t in ("guilds", "guild_games", "guild_shift", "guild_notices"))
