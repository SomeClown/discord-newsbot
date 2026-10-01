"""Gateway-free tests for the `/lounge quote-now` handler.

A hand-written fake interaction drives the handler against a real temp
database. The bot is a spy: `run_guild_quote` records the server and the
`force` it was called with and returns whatever outcome the test set up, so
nothing here posts, sleeps or touches a network.

These were the v2 `/newsbot quote-now` tests (one lounge, config-driven). They
moved to the per-server `/lounge quote-now` when v2 retired: the same
confirm, cancel, timeout and reply assertions, with the "already posted today"
state now read from the server's own `guild_lounge` row.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import newsbot.bot.commands as commands_module
from newsbot.bot.commands import make_lounge_group
from newsbot.config import load_config
from newsbot.lounge.daily import QuoteOutcome
from newsbot.pipeline.run import local_run_date
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_v3.yaml"
GUILD = 300000000000000001
LOUNGE_ID = 555000000000000001
DENIAL = "You don't have permission to run this."


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.deferred: list[bool] = []

    def is_done(self) -> bool:
        return bool(self.messages or self.deferred)

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred.append(ephemeral)

    async def send_message(
        self, content: str = "", *, ephemeral: bool = False, view=None, **kwargs
    ) -> None:
        self.messages.append((content, ephemeral))


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str = "", *, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, *, permissions: discord.Permissions) -> None:
        self.permissions = permissions
        self.guild_id = GUILD
        self.user = SimpleNamespace(id=1)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = SimpleNamespace(qualified_name="lounge quote-now")
        self.edits: list[str | None] = []

    async def edit_original_response(self, *, content=None, view=None) -> None:
        self.edits.append(content)


class SpyBot:
    def __init__(self, db_path: str, outcome: QuoteOutcome, *, in_server: bool = True) -> None:
        self.db_path = db_path
        self.outcome = outcome
        self.in_server = in_server
        self.calls: list[tuple[int, bool]] = []

    def get_guild(self, guild_id: int):
        return object() if self.in_server else None

    async def run_guild_quote(self, guild_id: int, force: bool) -> QuoteOutcome:
        self.calls.append((guild_id, force))
        return self.outcome


class FakeConfirmView:
    """Stands in for `ConfirmView`: answers immediately with a preset value."""

    answer: bool | None = True

    def __init__(self, owner_id: int) -> None:
        self.value = type(self).answer

    async def wait(self) -> None:
        return None


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.create_guild(conn, GUILD, set_up=True, timezone="America/Los_Angeles")
        repo.upsert_lounge(
            conn,
            LoungeSettings(
                guild_id=GUILD,
                channel_id=LOUNGE_ID,
                welcome_enabled=False,
                welcome_message="Hi {member}",
                quote_enabled=True,
                quote_time="08:00",
                quote_sources=[{"kind": "wikiquote", "value": "Oscar Wilde"}],
                last_quote_date=None,
            ),
        )
    return path


@pytest.fixture
def cfg():
    return load_config(CONFIG_PATH)


def _mark_posted_today(db_path: str) -> None:
    today = local_run_date(datetime.now(UTC), "America/Los_Angeles").isoformat()
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE guild_lounge SET last_quote_date = ? WHERE guild_id = ?", (today, GUILD)
        )


def _run(cfg, bot):
    group = make_lounge_group(cfg, bot)
    return next(c for c in group.commands if c.name == "quote-now")


def _admin(cfg) -> discord.Permissions:
    return discord.Permissions(**{cfg.admin_permission: True})


async def test_non_admin_is_denied_and_nothing_runs(cfg, db_path):
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=1))
    interaction = FakeInteraction(permissions=discord.Permissions.none())

    await _run(cfg, bot).callback(interaction)

    assert interaction.response.messages == [(DENIAL, True)]
    assert interaction.followup.messages == []
    assert bot.calls == []


async def test_without_the_bot_in_the_server_the_command_is_refused_and_nothing_runs(cfg, db_path):
    from newsbot.bot.commands import _NOT_IN_SERVER

    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=1), in_server=False)
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert interaction.response.messages == [(_NOT_IN_SERVER, True)]
    assert interaction.response.deferred == [] and bot.calls == []


async def test_a_non_admin_gets_the_permission_denial_not_the_bot_gate(cfg, db_path):
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=1), in_server=False)
    interaction = FakeInteraction(permissions=discord.Permissions.none())

    await _run(cfg, bot).callback(interaction)

    assert interaction.response.messages == [(DENIAL, True)]


async def test_fresh_day_defers_ephemerally_and_runs_unforced(cfg, db_path):
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=42))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert interaction.response.deferred == [True]
    assert interaction.response.messages == []
    assert bot.calls == [(GUILD, False)]


async def test_posted_reply_carries_the_jump_link(cfg, db_path):
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=987654321))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    link = f"https://discord.com/channels/{GUILD}/{LOUNGE_ID}/987654321"
    assert interaction.followup.messages == [(f"Posted: {link}", True)]


@pytest.mark.parametrize(
    ("outcome", "reply"),
    [
        (QuoteOutcome("already_posted"), "Today's quote already posted."),
        (QuoteOutcome("skipped"), "No quote posted; the admin channel has the reason."),
        (QuoteOutcome("post_failed"), "Posting failed; the admin channel has the details."),
    ],
)
async def test_each_outcome_maps_to_its_reply(cfg, db_path, outcome, reply):
    bot = SpyBot(db_path, outcome)
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert interaction.followup.messages == [(reply, True)]


async def test_already_posted_asks_first_and_cancel_posts_nothing(cfg, db_path, monkeypatch):
    _mark_posted_today(db_path)
    FakeConfirmView.answer = False
    monkeypatch.setattr(commands_module, "ConfirmView", FakeConfirmView)
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=1))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert interaction.response.messages == [
        ("Today's quote already posted. Post another one?", True)
    ]
    assert interaction.edits == ["Cancelled."]
    assert bot.calls == []
    assert interaction.followup.messages == []


async def test_timeout_counts_as_cancel(cfg, db_path, monkeypatch):
    _mark_posted_today(db_path)
    FakeConfirmView.answer = None
    monkeypatch.setattr(commands_module, "ConfirmView", FakeConfirmView)
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=1))

    await _run(cfg, bot).callback(FakeInteraction(permissions=_admin(cfg)))

    assert bot.calls == []


async def test_confirmed_run_forces_the_post(cfg, db_path, monkeypatch):
    _mark_posted_today(db_path)
    FakeConfirmView.answer = True
    monkeypatch.setattr(commands_module, "ConfirmView", FakeConfirmView)
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=7))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert bot.calls == [(GUILD, True)]
    assert interaction.followup.messages[0][0].startswith("Posted: https://discord.com/channels/")
