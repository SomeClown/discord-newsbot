"""Gateway-free tests for the `/newsbot quote-now` handler.

Same hand-written fake-interaction approach as
`test_commands_test_alert_handler.py`. The bot is a spy: `run_quote` records
the `force` it was called with and returns whatever outcome the test set up,
so nothing here posts, sleeps or touches a network.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import newsbot.bot.commands as commands_module
from newsbot.bot.commands import make_admin_group
from newsbot.config import DailyQuoteCfg, LoungeCfg, QuoteSourceCfg, WelcomeCfg, load_config
from newsbot.lounge.daily import QuoteOutcome
from newsbot.pipeline.run import local_run_date
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
LOUNGE_ID = 555000000000000001
DENIAL = "You don't have permission to run this."


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.deferred: list[bool] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred.append(ephemeral)

    async def send_message(self, content: str = "", *, ephemeral: bool = False, view=None) -> None:
        self.messages.append((content, ephemeral))


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str = "", *, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, *, permissions: discord.Permissions) -> None:
        self.permissions = permissions
        self.user = SimpleNamespace(id=1)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = SimpleNamespace(qualified_name="newsbot quote-now")
        self.edits: list[str | None] = []

    async def edit_original_response(self, *, content=None, view=None) -> None:
        self.edits.append(content)


class SpyBot:
    def __init__(self, db_path: str, outcome: QuoteOutcome) -> None:
        self.db_path = db_path
        self.outcome = outcome
        self.calls: list[bool] = []

    async def run_quote(self, force: bool) -> QuoteOutcome:
        self.calls.append(force)
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
    return path


@pytest.fixture
def cfg():
    base = load_config(CONFIG_PATH)
    lounge = LoungeCfg(
        channel_id=LOUNGE_ID,
        welcome=WelcomeCfg(enabled=False, message="Hi {member}"),
        daily_quote=DailyQuoteCfg(
            enabled=True, sources=[QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")]
        ),
    )
    return base.model_copy(update={"lounge": lounge})


def _mark_posted_today(db_path: str, cfg) -> None:
    today = local_run_date(datetime.now(UTC), cfg.digest.timezone).isoformat()
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO lounge_state (key, value) VALUES ('last_quote_date', ?)",
            (today,),
        )


def _run(cfg, bot):
    group = make_admin_group(cfg, bot)
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


async def test_fresh_day_defers_ephemerally_and_runs_unforced(cfg, db_path):
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=42))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert interaction.response.deferred == [True]
    assert interaction.response.messages == []
    assert bot.calls == [False]


async def test_posted_reply_carries_the_jump_link(cfg, db_path):
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=987654321))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    link = f"https://discord.com/channels/{cfg.guild_id}/{LOUNGE_ID}/987654321"
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
    _mark_posted_today(db_path, cfg)
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
    _mark_posted_today(db_path, cfg)
    FakeConfirmView.answer = None
    monkeypatch.setattr(commands_module, "ConfirmView", FakeConfirmView)
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=1))

    await _run(cfg, bot).callback(FakeInteraction(permissions=_admin(cfg)))

    assert bot.calls == []


async def test_confirmed_run_forces_the_post(cfg, db_path, monkeypatch):
    _mark_posted_today(db_path, cfg)
    FakeConfirmView.answer = True
    monkeypatch.setattr(commands_module, "ConfirmView", FakeConfirmView)
    bot = SpyBot(db_path, QuoteOutcome("posted", message_id=7))
    interaction = FakeInteraction(permissions=_admin(cfg))

    await _run(cfg, bot).callback(interaction)

    assert bot.calls == [True]
    assert interaction.followup.messages[0][0].startswith("Posted: https://discord.com/channels/")
