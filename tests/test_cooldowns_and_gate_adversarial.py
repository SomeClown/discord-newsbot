"""Adversarial tests for the preview and run-now cooldowns and the bot-not-in-server gate.

A cooldown is a promise to the people who aren't asking ("stop spending our model budget"),
and a promise to the person who is ("you'll get your turn"). The ways to break the first
are the owner's bypass leaking into the clock, and memory that grows with the number of
servers. The ways to break the second are a clock that starts for a run that never ran.
The gate has one job: a server that installed only the commands gets a polite no and not a
single row in the database.

Every command is driven through its real callback with the v3 fakes, like its siblings.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from v3_fakes import GUILD_A, GUILD_B, FakeInteraction, channel, command, fake_bot, make_guild

from newsbot.bot import commands as commands_module
from newsbot.bot.commands import make_guild_admin_group
from newsbot.pipeline.guild_digest import GuildDigestDeps, GuildTimeZoneError
from newsbot.store.models import GuildDigestRow

NOON = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
WINDOW = commands_module._PREVIEW_COOLDOWN


@pytest.fixture
def clock():
    return SimpleNamespace(t=NOON)


@pytest.fixture
def digest_calls(monkeypatch):
    calls = SimpleNamespace(
        runs=[],
        previews=[],
        today=None,
        preview=None,
        answer=True,
        run_raises=None,
        preview_raises=None,
    )

    async def today(deps, guild_id):
        return calls.today

    async def run(deps, guild_id, *, kind, force=False, due=None):
        calls.runs.append((guild_id, force))
        if calls.run_raises is not None:
            raise calls.run_raises
        return SimpleNamespace(status="ok", notes=[])

    async def preview(deps, guild_id):
        calls.previews.append(guild_id)
        if calls.preview_raises is not None:
            raise calls.preview_raises
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
    calls.preview = SimpleNamespace(
        rendered=SimpleNamespace(
            messages=[SimpleNamespace(channel_id=5, embed=discord.Embed(title="Palworld"))]
        ),
        notes=[],
    )
    return calls


@pytest.fixture
def admin(v3_cfg, v3_db, clock, digest_calls):
    async def nobody(*_):
        return None

    deps = GuildDigestDeps(
        cfg=v3_cfg,
        db_path=v3_db,
        now=lambda: clock.t,
        publisher_for=None,
        notify_guild=nobody,
    )
    bot = fake_bot(v3_db)
    group = make_guild_admin_group(v3_cfg, bot, digest_deps=lambda: deps)

    def get(name):
        return command(group, name).callback

    get.bot = bot
    get.group = group
    return get


def an_ok_row() -> GuildDigestRow:
    return GuildDigestRow(
        id=1,
        guild_id=GUILD_A,
        run_date=NOON.date(),
        status="ok",
        posted_by_game={"palworld": 1},
        posted_message_ids=[1],
        error_notes=None,
        window_start=None,
        window_end=None,
        attempts=1,
    )


def two_servers(v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    make_guild(v3_db, GUILD_B, games=[("palworld", 6)])


# --- the owner and the clock ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "`_cooldown_message` says the owner 'doesn't start a server's clock either', but it only "
        "returns early when a clock is already running. On a server with no clock the owner falls "
        "through to `last[guild_id] = now`, so an owner preview locks the server's own admins "
        "out for ten minutes."
    ),
)
async def test_an_owner_preview_does_not_start_the_servers_clock(admin, v3_db, clock):
    two_servers(v3_db)
    admin.bot.owner = True
    await admin("preview")(FakeInteraction())
    admin.bot.owner = False
    clock.t = NOON + timedelta(minutes=1)
    admins = FakeInteraction()
    await admin("preview")(admins)
    assert "try again" not in admins.text


@pytest.mark.xfail(
    strict=True,
    reason="The same leak on the forced run-now clock: the owner's first forced run starts it.",
)
async def test_an_owner_forced_run_now_does_not_start_the_servers_clock(
    admin, v3_db, clock, digest_calls
):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.today = an_ok_row()
    admin.bot.owner = True
    await admin("run-now")(FakeInteraction())
    admin.bot.owner = False
    clock.t = NOON + timedelta(minutes=1)
    second = FakeInteraction()
    await admin("run-now")(second)
    assert len(digest_calls.runs) == 2


async def test_the_owner_is_never_turned_away_and_never_extends_a_running_clock(
    admin, v3_db, clock
):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    await admin("preview")(FakeInteraction())  # an admin starts the clock at noon
    admin.bot.owner = True
    for minutes in (1, 5, 9):
        clock.t = NOON + timedelta(minutes=minutes)
        owner = FakeInteraction()
        await admin("preview")(owner)
        assert "try again" not in owner.text  # never held back
    admin.bot.owner = False
    clock.t = (
        NOON + WINDOW
    )  # the clock still ends ten minutes after the admin's go, not the owner's
    again = FakeInteraction()
    await admin("preview")(again)
    assert "try again" not in again.text


# --- a failed run and the clock ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The clock starts when the cooldown check passes, before the preview runs. A preview "
        "that raises (a broken time zone setting) has still used the server's go: the admin who "
        "fixes the zone and tries again is told to wait ten minutes for something that never ran."
    ),
)
async def test_a_preview_that_fails_does_not_start_the_cooldown(admin, v3_db, clock, digest_calls):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.preview_raises = GuildTimeZoneError("bad zone")
    broken = FakeInteraction()
    await admin("preview")(broken)
    assert "time zone" in broken.text
    digest_calls.preview_raises = None
    clock.t = NOON + timedelta(minutes=1)
    retry = FakeInteraction()
    await admin("preview")(retry)
    assert "try again" not in retry.text


@pytest.mark.xfail(
    strict=True,
    reason="Same as the preview: a confirmed run-now that raises has used the server's go.",
)
async def test_a_forced_run_that_fails_does_not_start_the_cooldown(
    admin, v3_db, clock, digest_calls
):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.today = an_ok_row()
    digest_calls.run_raises = GuildTimeZoneError("bad zone")
    await admin("run-now")(FakeInteraction())
    digest_calls.run_raises = None
    clock.t = NOON + timedelta(minutes=1)
    retry = FakeInteraction()
    await admin("run-now")(retry)
    assert len(digest_calls.runs) == 2  # the second one really ran


async def test_a_preview_with_nothing_to_show_still_counts_as_a_go(
    admin, v3_db, clock, digest_calls
):
    """Pinned: the clock is about the server's budget of looking, not about there being news."""
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    digest_calls.preview = None
    first = FakeInteraction()
    await admin("preview")(first)
    assert "Nothing would post" in first.text
    clock.t = NOON + timedelta(minutes=1)
    second = FakeInteraction()
    await admin("preview")(second)
    assert "try again" in second.text and digest_calls.previews == [GUILD_A]


async def test_a_refused_preview_does_not_push_the_clock_out_further(admin, v3_db, clock):
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    await admin("preview")(FakeInteraction())
    for minutes in (2, 4, 6, 8):
        clock.t = NOON + timedelta(minutes=minutes)
        refused = FakeInteraction()
        await admin("preview")(refused)
        assert "try again" in refused.text
    clock.t = NOON + WINDOW
    allowed = FakeInteraction()
    await admin("preview")(allowed)
    assert allowed.sent[-1]["content"] == "Would post in <#5>:"


async def test_the_preview_and_run_now_clocks_are_separate_and_each_server_has_its_own(
    admin, v3_db, clock, digest_calls
):
    two_servers(v3_db)
    digest_calls.today = an_ok_row()
    await admin("preview")(FakeInteraction())
    await admin("run-now")(FakeInteraction())  # a preview doesn't spend the forced-run go
    clock.t = NOON + timedelta(minutes=1)
    other = FakeInteraction(guild_id=GUILD_B)
    await admin("preview")(other)
    await admin("run-now")(FakeInteraction(guild_id=GUILD_B))
    assert "try again" not in other.text
    assert digest_calls.runs == [(GUILD_A, True), (GUILD_B, True)]


# --- memory ---


async def test_the_cooldown_memory_forgets_everyone_once_their_window_has_passed(
    admin, v3_db, clock
):
    for offset in range(1, 61):
        make_guild(v3_db, GUILD_A + 1000 + offset, games=[("palworld", 5)])
    last_preview = inspect.getclosurevars(admin("preview")).nonlocals["last_preview"]
    for offset in range(1, 61):
        await admin("preview")(FakeInteraction(guild_id=GUILD_A + 1000 + offset))
    assert len(last_preview) == 60  # bounded by how many servers asked inside one window
    clock.t = NOON + WINDOW + timedelta(seconds=1)
    make_guild(v3_db, GUILD_A, games=[("palworld", 5)])
    await admin("preview")(FakeInteraction())
    assert list(last_preview) == [GUILD_A]  # one allowed call swept the other sixty


# --- the bot-not-in-server gate ---

EVERY_COMMAND = {
    "setup": {},
    "follow": {"game": "palworld", "channel": channel(5)},
    "unfollow": {"game": "palworld"},
    "games": {},
    "settings": {"time": "10:00"},
    "shift": {"channel": channel(5)},
    "status": {},
    "preview": {},
    "run-now": {},
}


def test_the_gate_table_covers_every_command_in_the_group(admin):
    assert {c.name for c in admin.group.commands} == set(EVERY_COMMAND)


@pytest.mark.parametrize("name", sorted(EVERY_COMMAND))
async def test_without_the_bot_user_every_command_is_refused_and_no_row_is_written(
    v3_cfg, v3_db, digest_calls, name
):
    from contextlib import closing

    from newsbot.store.db import connect

    group = make_guild_admin_group(v3_cfg, fake_bot(v3_db, in_servers=set()))
    interaction = FakeInteraction()
    await command(group, name).callback(interaction, **EVERY_COMMAND[name])
    assert "not in it" in interaction.text
    with closing(connect(v3_db)) as conn:
        for table in ("guilds", "guild_games", "guild_shift", "guild_notices", "digests"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    assert digest_calls.runs == [] and digest_calls.previews == []


@pytest.mark.parametrize("name", sorted(EVERY_COMMAND))
async def test_a_guildless_interaction_gets_the_server_only_message_not_the_gate_one(
    v3_cfg, v3_db, name
):
    group = make_guild_admin_group(v3_cfg, fake_bot(v3_db, in_servers=set()))
    interaction = FakeInteraction(guild_id=None)
    await command(group, name).callback(interaction, **EVERY_COMMAND[name])
    assert "inside a server" in interaction.text and "not in it" not in interaction.text


async def test_the_bot_gate_asks_the_cache_for_this_server_and_no_other(v3_cfg, v3_db):
    asked = []
    bot = fake_bot(v3_db)
    real = bot.get_guild

    def spying(guild_id):
        asked.append(guild_id)
        return real(guild_id)

    bot.get_guild = spying
    group = make_guild_admin_group(v3_cfg, bot)
    await command(group, "status").callback(FakeInteraction(guild_id=GUILD_B))
    assert asked == [GUILD_B]


async def test_a_member_in_a_server_without_the_bot_is_not_told_the_bot_is_missing(v3_cfg, v3_db):
    from v3_fakes import MEMBER

    group = make_guild_admin_group(v3_cfg, fake_bot(v3_db, in_servers=set()))
    interaction = FakeInteraction(permissions=MEMBER)
    await command(group, "preview").callback(interaction)
    assert interaction.text == "You don't have permission to run this."
