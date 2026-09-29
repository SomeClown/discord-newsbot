"""Adversarial tests for the lounge wiring in `newsbot.bot.client` (plan task 8).

Companion to `test_lounge_client.py`, which checks that the glue works when
everyone behaves. This file is the other party: members whose names are
markdown with a grudge, guilds named after mention syntax, sends that fail
in every flavor discord.py offers, five events for one member arriving in
the same tick, and a scheduler asked to fire at a time that either doesn't
exist or happens twice. Where the code does something the owner might want
to reconsider, the test pins what it does today and says so in a comment,
so changing it later is a decision and not a surprise.

The fakes are the same hand-written kind as `test_lounge_client.py`; the
member is a plain object, not a `discord.Member`, so "the attribute is
missing" is something I can actually arrange. Time is a subclass of
`datetime` swapped into `client_module`, which is how the 24-hour window and
the local-day maths get tested without waiting a day. No gateway, no network.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.executors.base import run_coroutine_job
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import SecretStr

import newsbot.__main__ as entrypoint
import newsbot.bot.client as client_module
from newsbot.bot.client import NewsBot, build_intents, schedule_daily_quote
from newsbot.config import (
    DailyQuoteCfg,
    LoungeCfg,
    QuoteSourceCfg,
    Secrets,
    WelcomeCfg,
    load_config,
)
from newsbot.store.db import connect, migrate
from newsbot.store.repo import get_lounge_state

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
LOUNGE_ID = 555000000000000001
USER_ID = 424242424242424242
USER_NAME = "Distinctive_Guest_Name"
USER_NICK = "Distinctive_Nick_Name"
LA = ZoneInfo("America/Los_Angeles")
SOURCE = QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _cfg(
    *,
    welcome: bool = True,
    quote: bool = False,
    message: str = "Hi {member}, welcome to {server}",
    time: str = "08:00",
    sources: list[QuoteSourceCfg] | None = None,
):
    cfg = load_config(CONFIG_PATH)
    lounge = LoungeCfg(
        channel_id=LOUNGE_ID,
        welcome=WelcomeCfg(enabled=welcome, message=message),
        daily_quote=DailyQuoteCfg(enabled=quote, time=time, sources=sources or [SOURCE]),
    )
    return cfg.model_copy(update={"lounge": lounge})


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


class _Resp:
    """Just enough of an aiohttp response for discord.HTTPException to stringify."""

    def __init__(self, status: int, reason: str = "nope") -> None:
        self.status = status
        self.reason = reason


class FakeFlags:
    value = 7


class FakeGuild:
    def __init__(self, guild_id: int, name: str = "The Speakeasy") -> None:
        self.id = guild_id
        self.name = name


class FakeMember:
    def __init__(
        self,
        guild,
        *,
        user_id: int = USER_ID,
        bot: bool = False,
        pending=False,
        name: str = USER_NAME,
        nick: str = USER_NICK,
    ) -> None:
        self.guild = guild
        self.id = user_id
        self.bot = bot
        self.pending = pending
        self.flags = FakeFlags()
        self.name = name
        self.nick = nick
        self.display_name = nick
        self.global_name = name
        self.mention = f"<@{user_id}>"

    def __repr__(self) -> str:
        return f"FakeMember({self.name}, {self.nick}, {self.id})"


class FakeSent:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeChannel:
    """Records sends. `limit` mimics Discord's 2,000 UTF-16 unit cap; `slow` yields mid-send."""

    def __init__(self, *, limit: int | None = None, slow: bool = False) -> None:
        self.sent: list[tuple[str, discord.AllowedMentions]] = []
        self.next_error: BaseException | None = None
        self.limit = limit
        self.slow = slow

    async def send(self, content: str, *, allowed_mentions: discord.AllowedMentions):
        if self.slow:
            await asyncio.sleep(0)
        if self.next_error is not None:
            raise self.next_error
        if self.limit is not None and len(content.encode("utf-16-le")) // 2 > self.limit:
            raise discord.HTTPException(_Resp(400, "Bad Request"), "Must be 2000 or fewer")
        self.sent.append((content, allowed_mentions))
        return FakeSent(900 + len(self.sent))


def _bot(cfg, db_path, *, lounge: FakeChannel | None = None, admin: FakeChannel | None = None):
    """A NewsBot with the real `alert`, routed to fake lounge and admin channels.

    Unlike `test_lounge_client._bot`, `alert` is not stubbed: the alert text,
    its `AllowedMentions` and its "never raises" promise are part of what's
    under test. `get_channel` returning None for an unknown id sends
    `_lounge_channel` down its `fetch_channel` path.
    """
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.lounge_channel = lounge if lounge is not None else FakeChannel()  # type: ignore[attr-defined]
    bot.admin_channel = admin if admin is not None else FakeChannel()  # type: ignore[attr-defined]
    channels = {LOUNGE_ID: bot.lounge_channel, cfg.admin_channel_id: bot.admin_channel}  # type: ignore[attr-defined]
    bot.get_channel = lambda channel_id: channels.get(channel_id)  # type: ignore[method-assign]

    async def fetch_channel(channel_id: int):
        return channels[channel_id]

    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]
    return bot


class _Clock:
    """A settable `datetime.now`, installed into `client_module` by `_freeze`."""

    def __init__(self, start: datetime) -> None:
        self.current = start


def _freeze(monkeypatch, start: datetime) -> _Clock:
    clock = _Clock(start)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current if tz is None else clock.current.astimezone(tz)

    monkeypatch.setattr(client_module, "datetime", FrozenDatetime)
    return clock


def _mentions_are_only(mentions: discord.AllowedMentions, member) -> None:
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    assert mentions.users == [member]


def _mentions_are_none(mentions: discord.AllowedMentions) -> None:
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


# --- welcomes: hostile and odd members ---

HOSTILE = "@everyone **bold** [click](https://evil.example) <@&123456789012345678> \n# heading"


async def test_hostile_name_and_nick_never_reach_the_message(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    member = FakeMember(FakeGuild(cfg.guild_id), name=HOSTILE, nick=HOSTILE)

    await bot.on_member_join(member)

    ((text, mentions),) = bot.lounge_channel.sent
    assert text == f"Hi <@{USER_ID}>, welcome to The Speakeasy"
    assert "@everyone" not in text and "evil.example" not in text and "**" not in text
    _mentions_are_only(mentions, member)


async def test_guild_name_is_inserted_as_written_but_cannot_ping(db_path):
    # {server} is unescaped by design (welcome.py: whoever renames the server
    # has Manage Server). This pins what's sent and that the mention filter,
    # not the text, is what keeps the ping from firing.
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    guild_name = "@everyone <@&123456789012345678> [x](https://evil.example) **b**"
    member = FakeMember(FakeGuild(cfg.guild_id, name=guild_name))

    await bot.on_member_join(member)

    ((text, mentions),) = bot.lounge_channel.sent
    assert text == f"Hi <@{USER_ID}>, welcome to {guild_name}"
    _mentions_are_only(mentions, member)


async def test_pending_none_is_treated_as_not_pending_and_welcomed(db_path):
    # Documented: `if pending` is falsy for None, so a member whose `pending`
    # is None (discord.py never does this; the attribute is a bool) is welcomed.
    cfg = _cfg()
    bot = _bot(cfg, db_path)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id), pending=None))

    assert len(bot.lounge_channel.sent) == 1


async def test_a_member_with_no_pending_attribute_alerts_and_does_not_raise(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    member = FakeMember(FakeGuild(cfg.guild_id))
    del member.pending

    await bot.on_member_join(member)

    assert bot.lounge_channel.sent == []
    (alert,) = (text for text, _ in bot.admin_channel.sent)
    assert "AttributeError" in alert


async def test_missing_flags_alerts_and_the_member_is_not_retried(db_path):
    # Documented: the 24-hour record is written before `flags.value` is read
    # for the log line, so a member whose logging blows up is consumed and
    # never welcomed. discord.py's Member always has `flags`; this is a
    # what-if, not a bug I expect to meet.
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    member = FakeMember(FakeGuild(cfg.guild_id))
    del member.flags

    await bot.on_member_join(member)
    member.flags = FakeFlags()
    await bot.on_member_join(member)

    assert len(bot.admin_channel.sent) == 1
    assert bot.lounge_channel.sent == []


async def test_a_member_with_no_guild_alerts_and_does_not_raise(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)

    await bot.on_member_join(FakeMember(None))

    assert bot.lounge_channel.sent == []
    assert len(bot.admin_channel.sent) == 1


async def test_update_in_another_guild_is_ignored_and_does_not_use_up_the_member(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    elsewhere = FakeGuild(cfg.guild_id + 1)

    await bot.on_member_update(
        FakeMember(elsewhere, pending=True), FakeMember(elsewhere, pending=False)
    )
    assert bot.lounge_channel.sent == []

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))
    assert len(bot.lounge_channel.sent) == 1


async def test_a_bot_leaving_pending_is_never_welcomed_or_recorded(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    guild = FakeGuild(cfg.guild_id)

    await bot.on_member_update(
        FakeMember(guild, bot=True, pending=True), FakeMember(guild, bot=True)
    )
    assert bot.lounge_channel.sent == []
    assert bot.admin_channel.sent == []


async def test_reverse_transition_false_to_true_does_not_welcome_or_record(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    guild = FakeGuild(cfg.guild_id)

    await bot.on_member_update(FakeMember(guild, pending=False), FakeMember(guild, pending=True))
    assert bot.lounge_channel.sent == []

    # And the member wasn't used up: the real acceptance still welcomes.
    await bot.on_member_update(FakeMember(guild, pending=True), FakeMember(guild, pending=False))
    assert len(bot.lounge_channel.sent) == 1


async def test_update_with_none_pending_values(db_path):
    # Documented: None counts as false on both sides. before=None means "not
    # pending before", so nothing to welcome; before=True, after=None welcomes.
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    guild = FakeGuild(cfg.guild_id)

    await bot.on_member_update(FakeMember(guild, pending=None), FakeMember(guild, pending=False))
    assert bot.lounge_channel.sent == []

    await bot.on_member_update(FakeMember(guild, pending=True), FakeMember(guild, pending=None))
    assert len(bot.lounge_channel.sent) == 1


async def test_a_burst_of_join_and_update_events_sends_exactly_once(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path, lounge=FakeChannel(slow=True))
    guild = FakeGuild(cfg.guild_id)
    joined = FakeMember(guild)
    pending = FakeMember(guild, pending=True)

    await asyncio.gather(
        bot.on_member_join(joined),
        bot.on_member_update(pending, joined),
        bot.on_member_join(joined),
        bot.on_member_update(pending, joined),
        bot.on_member_join(joined),
    )

    assert len(bot.lounge_channel.sent) == 1
    assert bot.admin_channel.sent == []


async def test_rejoin_inside_and_after_24_hours(db_path, monkeypatch):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    guild = FakeGuild(cfg.guild_id)
    start = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    clock = _freeze(monkeypatch, start)

    await bot.on_member_join(FakeMember(guild))  # joins
    clock.current = start + timedelta(hours=1)
    await bot.on_member_join(FakeMember(guild))  # left and came back
    clock.current = start + timedelta(hours=23, minutes=59, seconds=59)
    await bot.on_member_join(FakeMember(guild))
    assert len(bot.lounge_channel.sent) == 1

    # Inclusive boundary: exactly 24 hours after the welcome is a new welcome.
    # The suppressed attempts at +1h and +23:59:59 didn't push the window out.
    clock.current = start + timedelta(hours=24)
    await bot.on_member_join(FakeMember(guild))
    assert len(bot.lounge_channel.sent) == 2


async def test_a_different_member_is_not_held_back_by_the_first(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    guild = FakeGuild(cfg.guild_id)

    await bot.on_member_join(FakeMember(guild, user_id=USER_ID))
    await bot.on_member_join(FakeMember(guild, user_id=USER_ID + 1))

    assert len(bot.lounge_channel.sent) == 2


# --- welcomes: failure paths ---


def _post_errors():
    return [
        discord.Forbidden(_Resp(403), f"Missing Access for {USER_NAME} {USER_ID} <@{USER_ID}>"),
        discord.NotFound(_Resp(404), f"Unknown Channel {USER_NICK}"),
        discord.HTTPException(_Resp(500), f"boom {USER_NAME}"),
        RuntimeError(f"generic {USER_NAME} {USER_ID}"),
    ]


@pytest.mark.parametrize("error", _post_errors(), ids=lambda e: type(e).__name__)
async def test_send_failures_give_one_alert_naming_no_member(db_path, error):
    cfg = _cfg()
    channel = FakeChannel()
    channel.next_error = error
    bot = _bot(cfg, db_path, lounge=channel)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    ((alert, mentions),) = bot.admin_channel.sent
    assert type(error).__name__ in alert
    for secret in (USER_NAME, USER_NICK, str(USER_ID), f"<@{USER_ID}>"):
        assert secret not in alert
    _mentions_are_none(mentions)


@pytest.mark.parametrize("error", _post_errors(), ids=lambda e: type(e).__name__)
async def test_fetch_failures_give_one_alert_naming_no_member(db_path, error):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    admin = bot.admin_channel

    async def fetch_channel(channel_id: int):
        if channel_id == LOUNGE_ID:
            raise error
        return admin

    bot.get_channel = lambda channel_id: None if channel_id == LOUNGE_ID else admin  # type: ignore[method-assign]
    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    ((alert, _),) = admin.sent
    assert type(error).__name__ in alert
    assert USER_NAME not in alert and str(USER_ID) not in alert


async def test_a_failed_welcome_is_not_retried_for_the_same_member(db_path):
    # Design: record before send, "a missing welcome beats a doubled one".
    cfg = _cfg()
    channel = FakeChannel()
    channel.next_error = discord.Forbidden(_Resp(403), "Missing Access")
    bot = _bot(cfg, db_path, lounge=channel)
    guild = FakeGuild(cfg.guild_id)

    await bot.on_member_join(FakeMember(guild))
    channel.next_error = None
    await bot.on_member_join(FakeMember(guild))

    assert channel.sent == []
    assert len(bot.admin_channel.sent) == 1


async def test_a_missing_lounge_channel_id_is_an_alert_not_a_crash(db_path):
    cfg = _cfg()
    cfg = cfg.model_copy(update={"lounge": cfg.lounge.model_copy(update={"channel_id": None})})
    bot = _bot(cfg, db_path)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    ((alert, _),) = bot.admin_channel.sent
    assert "RuntimeError" in alert


async def test_cancelled_send_propagates_without_an_alert(db_path):
    cfg = _cfg()
    channel = FakeChannel()
    channel.next_error = asyncio.CancelledError()
    bot = _bot(cfg, db_path, lounge=channel)

    with pytest.raises(asyncio.CancelledError):
        await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    assert bot.admin_channel.sent == []


async def test_cancelled_fetch_propagates_without_an_alert(db_path):
    cfg = _cfg()
    bot = _bot(cfg, db_path)
    bot.get_channel = lambda channel_id: None  # type: ignore[method-assign]

    async def fetch_channel(channel_id: int):
        raise asyncio.CancelledError

    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))
    assert bot.admin_channel.sent == []


async def test_a_broken_admin_channel_does_not_raise_out_of_the_handler(db_path):
    # The real `alert` is `send_alert`, which swallows its own failures, so a
    # dead admin channel can't take the handler down.
    cfg = _cfg()
    lounge, admin = FakeChannel(), FakeChannel()
    lounge.next_error = RuntimeError("lounge broke")
    admin.next_error = discord.Forbidden(_Resp(403), "Missing Access")
    bot = _bot(cfg, db_path, lounge=lounge, admin=admin)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    assert admin.sent == []


async def test_an_alert_that_itself_raises_propagates_out_of_the_handler(db_path):
    # Documented, not endorsed: `_maybe_welcome` awaits `self.alert` inside its
    # own except block with no second net. The production `alert` never
    # raises (see the test above), and discord.py's event dispatcher catches
    # whatever a handler raises and routes it to `on_error`, so this is
    # belt-and-braces territory. Pinned so a change is deliberate.
    cfg = _cfg()
    channel = FakeChannel()
    channel.next_error = RuntimeError("send failed")
    bot = _bot(cfg, db_path, lounge=channel)

    async def broken_alert(text: str) -> None:
        raise ValueError("the pager is on fire")

    bot.alert = broken_alert  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="pager"):
        await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))


async def test_logs_at_every_level_carry_no_name_id_nick_or_mention(db_path, caplog):
    cfg = _cfg()
    channel = FakeChannel()
    bot = _bot(cfg, db_path, lounge=channel)
    guild = FakeGuild(cfg.guild_id)
    caplog.set_level(logging.DEBUG)

    await bot.on_member_join(FakeMember(guild))
    await bot.on_member_join(FakeMember(guild))
    await bot.on_member_join(FakeMember(guild, user_id=USER_ID + 1, pending=True))
    await bot.on_member_update(
        FakeMember(guild, user_id=USER_ID + 1, pending=True),
        FakeMember(guild, user_id=USER_ID + 1),
    )
    channel.next_error = discord.Forbidden(_Resp(403), "Missing Access")
    await bot.on_member_join(FakeMember(guild, user_id=USER_ID + 2))

    assert caplog.records
    for record in caplog.records:
        # The formatted message, its extras and the raw args; the traceback
        # text is covered by the next test because it can legitimately differ.
        blob = record.getMessage() + repr(record.__dict__.get("args")) + repr(record.__dict__)
        for uid in (USER_ID, USER_ID + 1, USER_ID + 2):
            assert str(uid) not in blob
            assert f"<@{uid}>" not in blob
        assert USER_NAME not in blob and USER_NICK not in blob


async def test_failure_log_has_no_traceback_and_no_exception_message(db_path, caplog):
    # The failure log used to be `logger.exception`, whose traceback ends with
    # the exception's message: a member's name in an exception would have
    # walked into the log through that door. Now it's the class name only
    # (plus status and code for Discord errors), so the promise is a hard one.
    cfg = _cfg()
    channel = FakeChannel()
    channel.next_error = RuntimeError(f"failed for {USER_NAME} {USER_ID}")
    bot = _bot(cfg, db_path, lounge=channel)
    caplog.set_level(logging.DEBUG)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    (failure,) = [r for r in caplog.records if r.getMessage().startswith("welcome failed")]
    assert failure.getMessage() == "welcome failed: RuntimeError"
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is None
    for record in caplog.records:
        formatted = logging.Formatter("%(message)s").format(record)
        assert "Traceback" not in formatted
        assert USER_NAME not in formatted and str(USER_ID) not in formatted


async def test_failure_log_for_a_discord_error_carries_status_and_code_only(db_path, caplog):
    cfg = _cfg()
    channel = FakeChannel()
    channel.next_error = discord.Forbidden(_Resp(403), f"Missing Access for {USER_NAME}")
    bot = _bot(cfg, db_path, lounge=channel)
    caplog.set_level(logging.DEBUG)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id)))

    (failure,) = [r for r in caplog.records if r.getMessage().startswith("welcome failed")]
    assert failure.getMessage().startswith("welcome failed: Forbidden (status=403, code=")
    assert USER_NAME not in logging.Formatter("%(message)s").format(failure)


# --- the quote job ---


async def test_quote_job_crash_alerts_once_and_does_not_propagate(db_path):
    bot = _bot(_cfg(welcome=False, quote=True), db_path)

    async def boom(force: bool):
        raise RuntimeError("kaboom")

    bot.run_quote = boom  # type: ignore[method-assign]

    await bot._quote_job()

    ((alert, mentions),) = bot.admin_channel.sent
    assert alert == "newsbot: daily quote job crashed: kaboom"
    _mentions_are_none(mentions)


def _hostile_quote_error(size: int = 0) -> RuntimeError:
    return RuntimeError("@everyone [click](https://evil.example)\nsecond line " + "x" * size)


async def _crash_with(db_path, error: BaseException) -> FakeChannel:
    admin = FakeChannel(limit=2000)
    bot = _bot(_cfg(welcome=False, quote=True), db_path, admin=admin)

    async def boom(force: bool):
        raise error

    bot.run_quote = boom  # type: ignore[method-assign]
    await bot._quote_job()
    return admin


async def test_hostile_exception_text_in_the_crash_alert_cannot_ping(db_path):
    admin = await _crash_with(db_path, _hostile_quote_error())

    ((_, mentions),) = admin.sent
    _mentions_are_none(mentions)


async def test_crash_alert_shows_the_exception_text_as_one_escaped_line(db_path):
    # This used to pin the text going in as written (clickable masked link,
    # second line). Now the job boundary runs it through `plain_line` and
    # `esc`, so it's one inert line; the daily, sweep and retention jobs
    # share the helper.
    admin = await _crash_with(db_path, _hostile_quote_error())

    ((text, _),) = admin.sent
    assert "[click](https://evil.example)" not in text
    assert "https://evil.example" not in text  # esc() puts a zero-width space in the scheme
    assert "@everyone" not in text
    assert "\n" not in text
    assert "second line" in text


async def test_a_huge_exception_message_still_produces_a_deliverable_alert(db_path):
    admin = await _crash_with(db_path, _hostile_quote_error(5000))

    assert len(admin.sent) == 1
    assert len(admin.sent[0][0].encode("utf-16-le")) // 2 <= 2000


def _no_network_client() -> httpx.AsyncClient:
    """A client whose transport fails the test if anything tries to use it.

    These tests use file sources or never load one, so a request means the
    code went somewhere it shouldn't. A bare `AsyncClient()` would have gone
    to the real network to find out.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected HTTP request to {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse))


async def test_the_no_network_client_really_refuses_a_request():
    client = _no_network_client()
    try:
        with pytest.raises(pytest.fail.Exception, match="unexpected HTTP request"):
            await client.get("https://example.invalid/")
    finally:
        await client.aclose()


async def test_cancelled_run_releases_the_quote_lock(db_path, monkeypatch):
    bot = _bot(_cfg(welcome=False, quote=True), db_path)
    bot.http_client = _no_network_client()

    async def cancelled(deps, *, force: bool = False):
        raise asyncio.CancelledError

    monkeypatch.setattr(client_module, "run_daily_quote", cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await bot._quote_job()
        assert bot._quote_lock.locked() is False
        assert bot.admin_channel.sent == []
    finally:
        await bot.http_client.aclose()


def _quote_file(tmp_path: Path) -> QuoteSourceCfg:
    path = tmp_path / "quotes.txt"
    path.write_text("\n%\n".join(f"Quote number {n}, in full." for n in range(6)), encoding="utf-8")
    return QuoteSourceCfg(kind="file", value=str(path))


def _used_rows(db_path: str) -> int:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0]


def _last_day(db_path: str) -> str | None:
    with closing(connect(db_path)) as conn:
        return get_lounge_state(conn).last_quote_date


@pytest.mark.parametrize(
    ("order", "posts", "statuses"),
    [
        ((False, True), 2, ["posted", "posted"]),
        ((True, False), 1, ["posted", "already_posted"]),
    ],
)
async def test_racing_job_and_forced_run_agree_with_the_database(
    db_path, tmp_path, monkeypatch, order, posts, statuses
):
    _freeze(monkeypatch, datetime(2026, 9, 29, 16, 0, tzinfo=UTC))
    bot = _bot(_cfg(welcome=False, quote=True, sources=[_quote_file(tmp_path)]), db_path)
    bot.http_client = _no_network_client()
    try:
        outcomes = await asyncio.gather(*(bot.run_quote(force) for force in order))
    finally:
        await bot.http_client.aclose()

    assert [o.status for o in outcomes] == statuses
    assert len(bot.lounge_channel.sent) == posts
    assert _used_rows(db_path) == posts
    assert _last_day(db_path) == "2026-09-29"
    assert bot.admin_channel.sent == []


# --- build_quote_deps and the local day ---


async def test_explicit_source_list_reaches_the_deps_in_order(db_path):
    sources = [
        QuoteSourceCfg(kind="wikiquote", value="Mark Twain"),
        QuoteSourceCfg(kind="url", value="https://example.com/q.txt"),
        SOURCE,
    ]
    bot = _bot(_cfg(quote=True, sources=sources), db_path)
    bot.http_client = _no_network_client()
    try:
        assert bot.build_quote_deps().sources == sources
    finally:
        await bot.http_client.aclose()


async def test_an_unresolved_source_list_reads_as_not_ready_yet(db_path):
    # Documented: `sources=None` (which `load_config` always replaces with the
    # default list) is reported with the same "before setup_hook" message as a
    # missing HTTP client. Slightly misleading, but the state is unreachable
    # through `load_config`, so it's a footnote and not a bug.
    cfg = load_config(CONFIG_PATH)
    lounge = LoungeCfg(channel_id=LOUNGE_ID, daily_quote=DailyQuoteCfg(enabled=True, sources=None))
    bot = NewsBot(cfg.model_copy(update={"lounge": lounge}), _secrets(), db_path)
    bot.http_client = _no_network_client()
    try:
        with pytest.raises(RuntimeError, match="setup_hook"):
            bot.build_quote_deps()
    finally:
        await bot.http_client.aclose()


@pytest.mark.parametrize(
    ("utc_moment", "expected"),
    [
        # 23:59:59 and 00:00:00 in Los Angeles (PDT, UTC-7).
        (datetime(2026, 9, 30, 6, 59, 59, tzinfo=UTC), date(2026, 9, 29)),
        (datetime(2026, 9, 30, 7, 0, 0, tzinfo=UTC), date(2026, 9, 30)),
        # The same edge in winter (PST, UTC-8), and just after the UTC date rolled.
        (datetime(2026, 12, 1, 7, 59, 59, tzinfo=UTC), date(2026, 11, 30)),
        (datetime(2026, 12, 1, 8, 0, 0, tzinfo=UTC), date(2026, 12, 1)),
        (datetime(2026, 9, 30, 0, 0, 1, tzinfo=UTC), date(2026, 9, 29)),
    ],
)
async def test_local_day_follows_digest_timezone_not_utc(
    db_path, monkeypatch, utc_moment, expected
):
    _freeze(monkeypatch, utc_moment)
    bot = _bot(_cfg(quote=True), db_path)
    bot.http_client = _no_network_client()
    try:
        deps = bot.build_quote_deps()
    finally:
        await bot.http_client.aclose()

    assert deps.local_day == expected
    assert deps.now() == utc_moment


async def test_local_day_is_fixed_when_the_deps_are_built(db_path, monkeypatch):
    # Documented: the day is computed in `build_quote_deps`, not per call, so a
    # run that starts at 23:59:59 and finishes after midnight is still "yesterday's".
    clock = _freeze(monkeypatch, datetime(2026, 9, 30, 6, 59, 59, tzinfo=UTC))
    bot = _bot(_cfg(quote=True), db_path)
    bot.http_client = _no_network_client()
    try:
        deps = bot.build_quote_deps()
        clock.current += timedelta(seconds=5)
    finally:
        await bot.http_client.aclose()

    assert deps.local_day == date(2026, 9, 29)
    assert deps.now().date() == date(2026, 9, 30)


async def test_double_fire_on_the_fall_back_day_still_posts_one_quote(
    db_path, tmp_path, monkeypatch
):
    # APScheduler fires a 01:30 job twice on 2026-11-01 (see the schedule
    # tests below). The once-a-day guard is what keeps the lounge to one quote.
    clock = _freeze(monkeypatch, datetime(2026, 11, 1, 8, 30, tzinfo=UTC))  # 01:30 PDT
    bot = _bot(_cfg(welcome=False, quote=True, sources=[_quote_file(tmp_path)]), db_path)
    bot.http_client = _no_network_client()
    try:
        await bot._quote_job()
        clock.current = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 PST
        await bot._quote_job()
    finally:
        await bot.http_client.aclose()

    assert len(bot.lounge_channel.sent) == 1
    assert _last_day(db_path) == "2026-11-01"


# --- the schedule ---


def _fire_times(time: str, start: datetime, count: int) -> list[datetime]:
    """Successive fire times the way the scheduler walks them: previous fire time plus a clock."""
    scheduler = AsyncIOScheduler(timezone=LA)

    async def callback() -> None:
        pass

    trigger = schedule_daily_quote(scheduler, _cfg(welcome=False, quote=True, time=time), callback)
    trigger = trigger.trigger
    assert isinstance(trigger, CronTrigger)
    previous, now, fires = None, start, []
    for _ in range(count):
        fire = trigger.get_next_fire_time(previous, now)
        fires.append(fire)
        previous = fire
        now = (fire.astimezone(UTC) + timedelta(milliseconds=50)).astimezone(LA)
    return fires


def _local_days(fires: list[datetime]) -> list[date]:
    return [f.astimezone(LA).date() for f in fires]


SPRING = datetime(2026, 3, 6, 12, tzinfo=LA)  # gap: 2026-03-08 02:00 to 03:00
FALL = datetime(2026, 10, 30, 12, tzinfo=LA)  # overlap: 2026-11-01 01:00 to 02:00


@pytest.mark.parametrize("start", [SPRING, FALL], ids=["spring", "fall"])
@pytest.mark.parametrize("time", ["01:00", "02:30", "03:00", "08:00", "12:00", "23:59"])
def test_no_local_day_is_skipped_across_dst(start, time):
    days = sorted(set(_local_days(_fire_times(time, start, 7))))

    assert days == [days[0] + timedelta(days=i) for i in range(len(days))]


@pytest.mark.parametrize(
    "time",
    [
        pytest.param(
            "00:30",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "APScheduler 3.11.3 CronTrigger skips 2026-03-09 for a 00:00-00:59 job "
                    "after firing on the spring-forward day, so a quote time in that hour "
                    "loses a day. Default 08:00 is unaffected; digest jobs share the code."
                ),
            ),
        ),
        pytest.param(
            "00:00",
            marks=pytest.mark.xfail(strict=True, reason="same APScheduler skip as 00:30"),
        ),
    ],
)
def test_midnight_hour_quote_fires_every_day_across_spring_forward(time):
    days = _local_days(_fire_times(time, SPRING, 6))

    assert days == [days[0] + timedelta(days=i) for i in range(6)]


def test_time_inside_the_skipped_hour_fires_once_at_the_shifted_wall_time():
    # 02:30 does not exist on 2026-03-08. It fires once, at 03:30 PDT
    # (10:30 UTC), not skipped and not twice.
    fires = _fire_times("02:30", SPRING, 4)

    on_the_day = [f for f in fires if f.astimezone(LA).date() == date(2026, 3, 8)]
    assert [f.astimezone(UTC) for f in on_the_day] == [datetime(2026, 3, 8, 10, 30, tzinfo=UTC)]


def test_time_inside_the_repeated_hour_fires_twice_on_the_fall_back_day():
    # Documented: a 01:30 job fires at 01:30 PDT and again at 01:30 PST on
    # 2026-11-01. The once-a-day guard turns the second into "already_posted"
    # (see the double-fire test above), so members see one quote. 02:30 on the
    # same day is unambiguous and fires once.
    fires = _fire_times("01:30", FALL, 5)
    utc = [f.astimezone(UTC) for f in fires if f.astimezone(LA).date() == date(2026, 11, 1)]
    assert utc == [
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
    ]

    later = _fire_times("02:30", FALL, 5)
    assert len([f for f in later if f.astimezone(LA).date() == date(2026, 11, 1)]) == 1


async def test_the_job_id_is_unique_and_a_second_registration_is_a_conflict():
    scheduler = AsyncIOScheduler(timezone=LA)
    scheduler.start(paused=True)

    async def callback() -> None:
        pass

    try:
        schedule_daily_quote(scheduler, _cfg(quote=True), callback)
        with pytest.raises(ConflictingIdError):
            schedule_daily_quote(scheduler, _cfg(quote=True), callback)
        assert [job.id for job in scheduler.get_jobs()] == ["daily-quote"]
    finally:
        scheduler.shutdown(wait=False)


async def test_a_second_registration_before_start_fails_when_the_scheduler_starts():
    # Documented: before `start()`, add_job just queues, so the duplicate is
    # only noticed at start. setup_hook registers once, so this is a footnote.
    scheduler = AsyncIOScheduler(timezone=LA)

    async def callback() -> None:
        pass

    schedule_daily_quote(scheduler, _cfg(quote=True), callback)
    schedule_daily_quote(scheduler, _cfg(quote=True), callback)

    with pytest.raises(ConflictingIdError):
        scheduler.start()


@pytest.mark.parametrize(
    ("late_by", "event"),
    [
        (timedelta(seconds=299), EVENT_JOB_EXECUTED),
        (timedelta(seconds=300), EVENT_JOB_EXECUTED),
        (timedelta(seconds=300, microseconds=1), EVENT_JOB_MISSED),
        (timedelta(seconds=301), EVENT_JOB_MISSED),
    ],
)
async def test_misfire_grace_boundary_is_300_seconds_inclusive(monkeypatch, late_by, event):
    import apscheduler.executors.base as executors_base

    scheduled = datetime(2026, 9, 29, 8, 0, tzinfo=LA)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (scheduled + late_by).astimezone(tz) if tz else scheduled + late_by

    monkeypatch.setattr(executors_base, "datetime", FrozenDatetime)
    ran: list[int] = []

    async def callback() -> None:
        ran.append(1)

    scheduler = AsyncIOScheduler(timezone=LA)
    job = schedule_daily_quote(scheduler, _cfg(quote=True), callback)

    events = await run_coroutine_job(job, "default", [scheduled], "test")

    assert [e.code for e in events] == [event]
    assert bool(ran) is (event == EVENT_JOB_EXECUTED)


async def _setup_and_list_jobs(cfg, db_path) -> tuple[set[str], bool]:
    bot = NewsBot(cfg, _secrets(), db_path)

    async def _fake_sync(*args, **kwargs):
        return []

    bot.tree.sync = _fake_sync  # type: ignore[method-assign]
    try:
        await bot.setup_hook()
        return {job.id for job in bot.scheduler.get_jobs()}, bot.intents.members
    finally:
        bot.scheduler.shutdown(wait=False)
        await bot.http_client.aclose()


@pytest.mark.parametrize(
    ("welcome", "quote"), [(True, False), (False, True), (True, True), (False, False)]
)
async def test_setup_hook_wires_each_feature_independently(db_path, welcome, quote):
    jobs, members = await _setup_and_list_jobs(_cfg(welcome=welcome, quote=quote), db_path)

    assert ("daily-quote" in jobs) is quote
    assert members is welcome


# --- intents ---


@pytest.mark.parametrize("welcome", [True, False])
@pytest.mark.parametrize("quote", [True, False])
def test_intents_members_iff_welcome_and_nothing_else_privileged(welcome, quote):
    intents = build_intents(_cfg(welcome=welcome, quote=quote))

    assert intents.members is welcome
    assert intents.presences is False
    assert intents.message_content is False
    baseline = discord.Intents.default()
    baseline.members = welcome
    assert intents.value == baseline.value


# --- __main__ ---


def _wire_main(monkeypatch, tmp_path, run) -> None:
    class StubBot:
        def __init__(self, cfg, secrets, db_path) -> None:
            pass

        def run(self, token: str, **kwargs) -> None:
            run()

    monkeypatch.setenv("NEWSBOT_CONFIG", str(CONFIG_PATH))
    monkeypatch.setenv("NEWSBOT_DB", str(tmp_path / "newsbot.db"))
    monkeypatch.setattr(
        entrypoint,
        "load_secrets",
        lambda: Secrets(
            discord_token=SecretStr("super-secret-token-value"),
            anthropic_api_key=SecretStr("test-anthropic-key"),
            brave_api_key=None,
            bluesky_handle=None,
            bluesky_app_password=None,
        ),
    )
    monkeypatch.setattr(entrypoint, "NewsBot", StubBot)
    # The refused-intent path now waits ten minutes before exiting.
    monkeypatch.setattr(entrypoint, "INTENT_EXIT_DELAY_S", 0)


@pytest.mark.parametrize("shard_id", [None, 3])
def test_privileged_intents_prints_exactly_one_stderr_line_and_returns_2(
    monkeypatch, tmp_path, capsys, shard_id
):
    def run() -> None:
        raise discord.PrivilegedIntentsRequired(shard_id)

    _wire_main(monkeypatch, tmp_path, run)

    assert entrypoint.main() == 2

    captured = capsys.readouterr()
    # stdout may carry JSON log lines (config warnings); the message is stderr's alone.
    assert "Server Members" not in captured.out
    assert captured.err.endswith("\n")
    assert len(captured.err.splitlines()) == 1
    assert "super-secret-token-value" not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("something else broke"),
        ValueError("nope"),
        discord.LoginFailure("Improper token has been passed."),
        discord.ClientException("the parent class must not be swallowed"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_other_exceptions_still_propagate(monkeypatch, tmp_path, capsys, error):
    def run() -> None:
        raise error

    _wire_main(monkeypatch, tmp_path, run)

    with pytest.raises(type(error)):
        entrypoint.main()
    assert capsys.readouterr().err == ""
