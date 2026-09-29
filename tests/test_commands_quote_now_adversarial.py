"""Adversarial tests for the `/newsbot quote-now` handler (plan task 9).

`test_commands_quote_now_handler.py` proves the handler works when one polite
admin runs it once, against a spy bot. This file is the rest of the evening:
two admins hitting the command in the same second, a stranger leaning on
somebody else's confirm button, a clock that steps over midnight (or back
over it) between the question and the answer, and a `run_quote` that throws
its hands up. Where it can, it uses the real thing: a real `NewsBot` (no
gateway), a real temp database, the real `run_daily_quote` and the real
`ConfirmView` gate. The spy only turns up where I'd otherwise need a
stopwatch.

Where the handler does something the owner might want different, the test
pins what happens today and says so in a comment. Nothing here found a real
bug, which I'm choosing to treat as the handler being good and not me being
bad at this.

No gateway, no network; quotes are teapots.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord
import httpx
import pytest
from pydantic import SecretStr

import newsbot.bot.client as client_module
import newsbot.bot.commands as commands_module
from newsbot.bot.client import NewsBot
from newsbot.bot.commands import make_admin_group
from newsbot.bot.views import ConfirmView
from newsbot.config import (
    DailyQuoteCfg,
    LoungeCfg,
    QuoteSourceCfg,
    Secrets,
    WelcomeCfg,
    load_config,
)
from newsbot.lounge.daily import QuoteOutcome
from newsbot.store.db import connect, migrate
from newsbot.store.repo import get_lounge_state

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
LOUNGE_ID = 555000000000000001
DENIAL = "You don't have permission to run this."
OWNER_ONLY = "Only the requester can do this."


class FakeResponse:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.messages: list[tuple[str, bool]] = []
        self.deferred: list[bool] = []

    def is_done(self) -> bool:
        return bool(self.messages or self.deferred)

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.log.append("defer")
        self.deferred.append(ephemeral)

    async def send_message(self, content: str = "", *, ephemeral: bool = False, view=None) -> None:
        self.log.append("send_message")
        self.messages.append((content, ephemeral))


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.kwargs: list[dict] = []

    async def send(self, content: str = "", *, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))
        self.kwargs.append(kwargs)


class FakeInteraction:
    def __init__(self, permissions: discord.Permissions, *, user_id: int = 1, log=None) -> None:
        self.log: list[str] = log if log is not None else []
        self.permissions = permissions
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup()
        self.command = SimpleNamespace(qualified_name="newsbot quote-now")
        self.data: dict = {}
        self.edits: list[str | None] = []

    async def edit_original_response(self, *, content=None, view=None) -> None:
        self.edits.append(content)


def _lounge_cfg(*, channel_id: int | None = LOUNGE_ID, sources=None):
    base = load_config(CONFIG_PATH)
    lounge = LoungeCfg(
        channel_id=channel_id,
        welcome=WelcomeCfg(enabled=False, message="Hi {member}"),
        daily_quote=DailyQuoteCfg(
            enabled=True,
            sources=sources or [QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")],
        ),
    )
    return base.model_copy(update={"lounge": lounge})


def _admin(cfg) -> discord.Permissions:
    return discord.Permissions(**{cfg.admin_permission: True})


def _handler(cfg, bot):
    group = make_admin_group(cfg, bot)
    return next(c for c in group.commands if c.name == "quote-now")


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _set_last_quote_date(db_path: str, day: str) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO lounge_state (key, value) VALUES ('last_quote_date', ?)",
            (day,),
        )


def _last_day(db_path: str) -> str | None:
    with closing(connect(db_path)) as conn:
        return get_lounge_state(conn).last_quote_date


def _used_rows(db_path: str) -> int:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0]


class SpyBot:
    def __init__(self, db_path: str, outcome: QuoteOutcome | None = None, *, action=None) -> None:
        self.db_path = db_path
        self.outcome = outcome or QuoteOutcome("posted", message_id=1)
        self.action = action
        self.calls: list[bool] = []

    async def run_quote(self, force: bool) -> QuoteOutcome:
        self.calls.append(force)
        if self.action is not None:
            await self.action()
        return self.outcome


# --- the real bot, the real DB, the real run_daily_quote ---


class FakeSent:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, discord.AllowedMentions]] = []

    async def send(self, content: str, *, allowed_mentions: discord.AllowedMentions):
        await asyncio.sleep(0)
        self.sent.append((content, allowed_mentions))
        return FakeSent(900 + len(self.sent))


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _quote_file(tmp_path: Path, quotes=None) -> QuoteSourceCfg:
    path = tmp_path / "quotes.txt"
    lines = quotes or [f"Teapot number {n}, in full." for n in range(6)]
    path.write_text("\n%\n".join(lines), encoding="utf-8")
    return QuoteSourceCfg(kind="file", value=str(path))


def _real_bot(cfg, db_path):
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.lounge_channel = FakeChannel()  # type: ignore[attr-defined]
    admin_channel = FakeChannel()
    bot.admin_channel = admin_channel  # type: ignore[attr-defined]
    channels = {LOUNGE_ID: bot.lounge_channel, cfg.admin_channel_id: admin_channel}  # type: ignore[attr-defined]
    bot.get_channel = lambda channel_id: channels.get(channel_id)  # type: ignore[method-assign]
    bot.http_client = httpx.AsyncClient()
    return bot


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.current = start


def _freeze(monkeypatch, start: datetime) -> _Clock:
    clock = _Clock(start)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current if tz is None else clock.current.astimezone(tz)

    monkeypatch.setattr(client_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(commands_module, "datetime", FrozenDatetime)
    return clock


NOON_UTC = datetime(2026, 9, 29, 19, 0, tzinfo=UTC)  # noon in Los Angeles, 2026-09-29


# --- who is allowed to do what ---


async def test_non_admin_never_reaches_the_confirm_dialog_or_run_quote(db_path, monkeypatch):
    # Even with "already posted today" on file, which is the state that would
    # build a ConfirmView, a non-admin gets the denial and nothing else: there
    # is no button to press because no view was ever created.
    cfg = _lounge_cfg()
    _freeze(monkeypatch, NOON_UTC)
    _set_last_quote_date(db_path, "2026-09-29")

    def boom(*args, **kwargs):
        raise AssertionError("ConfirmView was built for a non-admin")

    monkeypatch.setattr(commands_module, "ConfirmView", boom)
    bot = SpyBot(db_path)
    interaction = FakeInteraction(discord.Permissions.none())

    await _handler(cfg, bot).callback(interaction)

    assert interaction.response.messages == [(DENIAL, True)]
    assert bot.calls == []
    assert interaction.followup.messages == []


async def test_a_stranger_cannot_press_the_owners_confirm_button(db_path, monkeypatch):
    # Real ConfirmView, real interaction_check, driven through discord.py's own
    # `_scheduled_task` (private, but it's the exact path a click takes). The
    # stranger is told off and the dialog stays open; only then does the
    # invoker's click let the forced post through.
    cfg = _lounge_cfg()
    _freeze(monkeypatch, NOON_UTC)
    _set_last_quote_date(db_path, "2026-09-29")
    views: list[ConfirmView] = []

    class Capturing(ConfirmView):
        def __init__(self, owner_id: int) -> None:
            super().__init__(owner_id)
            views.append(self)

    monkeypatch.setattr(commands_module, "ConfirmView", Capturing)
    bot = SpyBot(db_path)
    invoker = FakeInteraction(_admin(cfg), user_id=111)
    task = asyncio.create_task(_handler(cfg, bot).callback(invoker))
    for _ in range(100):
        if views and invoker.response.messages:
            break
        await asyncio.sleep(0)
    (view,) = views
    confirm = next(c for c in view.children if c.label == "Post again")

    stranger = FakeInteraction(_admin(cfg), user_id=222)
    await view._scheduled_task(confirm, stranger)

    assert stranger.response.messages == [(OWNER_ONLY, True)]
    assert view.value is None
    assert not task.done()
    assert bot.calls == []

    owner_click = FakeInteraction(_admin(cfg), user_id=111)
    await view._scheduled_task(confirm, owner_click)
    await asyncio.wait_for(task, 1)

    assert view.value is True
    assert bot.calls == [True]


async def test_a_stranger_cannot_cancel_the_owners_dialog_either(db_path, monkeypatch):
    cfg = _lounge_cfg()
    _freeze(monkeypatch, NOON_UTC)
    _set_last_quote_date(db_path, "2026-09-29")
    view = ConfirmView(111)
    cancel = next(c for c in view.children if c.label == "Cancel")
    stranger = FakeInteraction(_admin(cfg), user_id=222)

    await view._scheduled_task(cancel, stranger)

    assert view.value is None
    assert stranger.response.messages == [(OWNER_ONLY, True)]


# --- two admins, one fresh day ---


async def test_two_admins_at_once_post_exactly_one_quote(db_path, tmp_path, monkeypatch):
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg(sources=[_quote_file(tmp_path)])
    bot = _real_bot(cfg, db_path)
    first = FakeInteraction(_admin(cfg), user_id=1)
    second = FakeInteraction(_admin(cfg), user_id=2)
    try:
        await asyncio.gather(
            _handler(cfg, bot).callback(first), _handler(cfg, bot).callback(second)
        )
    finally:
        await bot.http_client.aclose()

    replies = sorted(i.followup.messages[0][0] for i in (first, second))
    assert len(bot.lounge_channel.sent) == 1
    assert replies[0] == "Posted: https://discord.com/channels/" + (
        f"{cfg.guild_id}/{LOUNGE_ID}/901"
    )
    assert replies[1] == "Today's quote already posted."
    assert _used_rows(db_path) == 1
    assert _last_day(db_path) == "2026-09-29"
    assert first.response.deferred == [True] and second.response.deferred == [True]


async def test_the_scheduled_job_racing_quote_now_still_posts_once(db_path, tmp_path, monkeypatch):
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg(sources=[_quote_file(tmp_path)])
    bot = _real_bot(cfg, db_path)
    admin = FakeInteraction(_admin(cfg))
    try:
        await asyncio.gather(_handler(cfg, bot).callback(admin), bot._quote_job())
    finally:
        await bot.http_client.aclose()

    assert len(bot.lounge_channel.sent) == 1
    assert _used_rows(db_path) == 1


# --- the clock ---


async def test_midnight_between_the_state_read_and_run_quote_posts_the_new_day(
    db_path, tmp_path, monkeypatch
):
    # 23:59:50 in Los Angeles: nothing posted for the 29th, so no prompt. By
    # the time run_quote works out its own day it's the 30th. Pinned: the
    # quote is recorded against the 30th (what run_quote saw), so the 30th's
    # scheduled quote skips. Nobody gets two quotes; someone gets an early one.
    clock = _freeze(monkeypatch, datetime(2026, 9, 30, 6, 59, 50, tzinfo=UTC))
    cfg = _lounge_cfg(sources=[_quote_file(tmp_path)])
    bot = _real_bot(cfg, db_path)
    real_read = commands_module._quote_posted_today_sync

    def read_then_tick(path: str, day: str) -> bool:
        result = real_read(path, day)
        clock.current = datetime(2026, 9, 30, 7, 0, 5, tzinfo=UTC)
        return result

    monkeypatch.setattr(commands_module, "_quote_posted_today_sync", read_then_tick)
    interaction = FakeInteraction(_admin(cfg))
    try:
        await _handler(cfg, bot).callback(interaction)
    finally:
        await bot.http_client.aclose()

    assert interaction.response.deferred == [True]
    assert interaction.followup.messages[0][0].startswith("Posted: ")
    assert len(bot.lounge_channel.sent) == 1
    assert _last_day(db_path) == "2026-09-30"


async def _click_through_the_prompt(cfg, bot, monkeypatch, label: str) -> FakeInteraction:
    """Run the handler and press `label` on the confirm dialog as the invoker."""
    views: list[ConfirmView] = []

    class Capturing(ConfirmView):
        def __init__(self, owner_id: int) -> None:
            super().__init__(owner_id)
            views.append(self)

    monkeypatch.setattr(commands_module, "ConfirmView", Capturing)
    interaction = FakeInteraction(_admin(cfg), user_id=111)
    task = asyncio.create_task(_handler(cfg, bot).callback(interaction))
    for _ in range(100):
        if views and interaction.response.messages:
            break
        await asyncio.sleep(0)
    (view,) = views
    button = next(c for c in view.children if c.label == label)
    await view._scheduled_task(button, FakeInteraction(_admin(cfg), user_id=111))
    await asyncio.wait_for(task, 1)
    return interaction


async def test_last_quote_date_in_the_future_gets_the_confirm_prompt(db_path, monkeypatch):
    # Changed from "skips the prompt and is refused". The clock went backwards
    # (or the box was briefly in 2027). The pre-check used to compare for
    # equality, so "tomorrow is done" didn't look like "today is done" and the
    # admin got a bare "already posted" with no way forward. It now uses
    # claim_quote's rule (a stored date at or after today counts as posted),
    # so the admin is offered the same "Post another one?" prompt.
    _freeze(monkeypatch, NOON_UTC)
    _set_last_quote_date(db_path, "2026-10-01")
    cfg = _lounge_cfg()
    bot = SpyBot(db_path)

    interaction = await _click_through_the_prompt(cfg, bot, monkeypatch, "Cancel")

    assert interaction.response.messages == [
        ("Today's quote already posted. Post another one?", True)
    ]
    assert bot.calls == []


async def test_confirming_the_prompt_for_a_future_last_quote_date_forces_the_run(
    db_path, monkeypatch
):
    _freeze(monkeypatch, NOON_UTC)
    _set_last_quote_date(db_path, "2026-10-01")
    cfg = _lounge_cfg()
    bot = SpyBot(db_path)

    await _click_through_the_prompt(cfg, bot, monkeypatch, "Post again")

    assert bot.calls == [True]


# --- run_quote misbehaving ---


async def test_defer_happens_before_run_quote_starts(db_path, monkeypatch):
    # Discord gives three seconds to acknowledge; the quote can take longer
    # (a Wikiquote fetch). The ack has to be first, and the reply after.
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg()
    log: list[str] = []
    interaction = FakeInteraction(_admin(cfg), log=log)

    async def slow() -> None:
        log.append("run_quote:start")
        await asyncio.sleep(0.05)
        log.append("run_quote:end")

    bot = SpyBot(db_path, action=slow)

    await _handler(cfg, bot).callback(interaction)

    assert log == ["defer", "run_quote:start", "run_quote:end"]
    assert interaction.followup.messages != []


async def test_run_quote_raising_propagates_to_the_tree_error_handler(db_path, monkeypatch):
    # The handler has no try/except of its own; it relies on
    # `NewsBot._on_command_error` (set as `tree.on_error`) to un-stick the
    # "thinking..." state. Pinned end to end: the exception escapes the
    # handler, and the real error handler answers the deferred interaction
    # with an ephemeral followup that leaks nothing.
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg()
    real = _real_bot(cfg, db_path)

    async def boom() -> None:
        raise RuntimeError("Wikiquote said something rude about teapots")

    bot = SpyBot(db_path, action=boom)
    interaction = FakeInteraction(_admin(cfg))
    try:
        with pytest.raises(RuntimeError):
            await _handler(cfg, bot).callback(interaction)
        assert interaction.followup.messages == []

        await real._on_command_error(interaction, discord.app_commands.AppCommandError())
    finally:
        await real.http_client.aclose()

    ((text, ephemeral),) = interaction.followup.messages
    assert ephemeral is True
    assert text.startswith("Something went wrong")
    assert "teapots" not in text


async def test_a_crashed_run_releases_the_lock_for_the_next_admin(db_path, tmp_path, monkeypatch):
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg(sources=[_quote_file(tmp_path)])
    bot = _real_bot(cfg, db_path)
    real_run = client_module.run_daily_quote
    calls = 0

    async def flaky(deps, *, force=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first run explodes")
        return await real_run(deps, force=force)

    monkeypatch.setattr(client_module, "run_daily_quote", flaky)
    first = FakeInteraction(_admin(cfg))
    second = FakeInteraction(_admin(cfg))
    try:
        with pytest.raises(RuntimeError):
            await _handler(cfg, bot).callback(first)
        await asyncio.wait_for(_handler(cfg, bot).callback(second), 5)
    finally:
        await bot.http_client.aclose()

    assert second.followup.messages[0][0].startswith("Posted: ")


# --- odd outcomes ---


async def test_posted_without_a_message_id_gets_the_generic_no_quote_reply(db_path, monkeypatch):
    # Documented, and arguably misleading: status "posted" with no id (which
    # `run_daily_quote` never produces; `post` returns an int) falls through
    # to "No quote posted; the admin channel has the reason.", which would be
    # false. Harmless while the impossible stays impossible.
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg()
    interaction = FakeInteraction(_admin(cfg))

    await _handler(cfg, SpyBot(db_path, QuoteOutcome("posted", message_id=None))).callback(
        interaction
    )

    assert interaction.followup.messages == [
        ("No quote posted; the admin channel has the reason.", True)
    ]


async def test_posted_without_a_lounge_channel_id_gets_the_generic_reply(db_path, monkeypatch):
    # Same shape: config validation should make a None channel_id impossible
    # with the quote on (model_copy skips validation, so I can arrange it).
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg(channel_id=None)
    interaction = FakeInteraction(_admin(cfg))

    await _handler(cfg, SpyBot(db_path, QuoteOutcome("posted", message_id=5))).callback(interaction)

    assert interaction.followup.messages == [
        ("No quote posted; the admin channel has the reason.", True)
    ]


# --- what the replies say, and who they can ping ---


async def test_replies_never_carry_quote_text_or_notes_and_send_no_mention_override(
    db_path, tmp_path, monkeypatch
):
    # A quote and a source note both full of ping syntax. The lounge post is
    # the one place the quote goes; the admin's reply is a fixed sentence plus
    # a jump link built from integers. The followup passes no allowed_mentions
    # of its own, so it inherits the client default (asserted below to be
    # AllowedMentions.none()), which is what the implementer relied on.
    _freeze(monkeypatch, NOON_UTC)
    hostile = "@everyone <@&123456789012345678> tea is a lifestyle"
    cfg = _lounge_cfg(sources=[_quote_file(tmp_path, [hostile])])
    bot = _real_bot(cfg, db_path)
    interaction = FakeInteraction(_admin(cfg))
    try:
        await _handler(cfg, bot).callback(interaction)
    finally:
        await bot.http_client.aclose()

    ((reply, ephemeral),) = interaction.followup.messages
    assert ephemeral is True
    assert reply == f"Posted: https://discord.com/channels/{cfg.guild_id}/{LOUNGE_ID}/901"
    assert "@" not in reply and "tea" not in reply
    assert "allowed_mentions" not in interaction.followup.kwargs[0]
    mentions = bot._connection.allowed_mentions
    assert (mentions.everyone, mentions.roles, mentions.users, mentions.replied_user) == (
        False,
        False,
        False,
        False,
    )
    ((sent_text, sent_mentions),) = bot.lounge_channel.sent
    assert "@everyone" not in sent_text  # defused with a zero-width space
    assert "tea is a lifestyle" in sent_text
    assert sent_mentions.everyone is False and sent_mentions.roles is False
    assert sent_mentions.users is False


@pytest.mark.parametrize(
    "outcome",
    [
        QuoteOutcome("skipped", notes=("https://evil.example said @everyone",)),
        QuoteOutcome("post_failed", source_key="k", notes=("<@&1> is a role",)),
        QuoteOutcome("already_posted", source_key="k", notes=("@here",)),
        QuoteOutcome("posted", message_id=3, notes=("@everyone",)),
    ],
)
async def test_notes_never_reach_the_admins_reply(db_path, monkeypatch, outcome):
    _freeze(monkeypatch, NOON_UTC)
    cfg = _lounge_cfg()
    interaction = FakeInteraction(_admin(cfg))

    await _handler(cfg, SpyBot(db_path, outcome)).callback(interaction)

    ((reply, _),) = interaction.followup.messages
    assert "evil.example" not in reply and "@" not in reply and "<@" not in reply
