"""The lounge wiring in `newsbot.bot.client` (design.md §14, plan task 8), now per server.

Everything with real logic lives in `newsbot/lounge/` and has its own tests.
This file checks the glue: which intents get requested, what the member
handlers do with a join, the exact `AllowedMentions` on each send, the
scheduler job, and the lock the job and `/lounge quote-now` share. The fakes
are the same tiny hand-written kind as `test_code_alert_poster.py`; the
scheduler test uses a real `AsyncIOScheduler` because "missed means skipped" is
a claim about APScheduler, and a fake would only agree with me.

These began as the v2 tests (one lounge, read from `config.yaml`). At the
cutover they moved to the per-server lounge: the same welcome, mention, dedupe,
logging, schedule and lock assertions, with the lounge now a `guild_lounge`
row, the scheduler job named per server, and a lounge problem told to that
server's notifier instead of the owner's channel.

No gateway, no network. `tree.sync` is stubbed the same way as in
`test_command_registration_v3.py`.
"""

from __future__ import annotations

import asyncio
import logging
import random
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import SecretStr

import newsbot.bot.client as client_module
from newsbot.bot.client import NewsBot, build_intents_for_lounges, schedule_guild_quote
from newsbot.config import QuoteSourceCfg, Secrets, load_config
from newsbot.lounge.daily import QuoteOutcome
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_v3.yaml"
GUILD = 300000000000000001
ZONE = "America/Los_Angeles"
LOUNGE_ID = 555000000000000001
USER_ID = 424242424242424242
USER_NAME = "Distinctive_Guest_Name"
SOURCE = QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


@dataclass(frozen=True)
class Setup:
    """One server and its lounge row, standing in for the v2 config these tests used to load."""

    guild_id: int
    lounge: LoungeSettings
    cfg: object


def _cfg(
    *,
    welcome: bool = False,
    quote: bool = False,
    message: str = "Hi {member}, {server}",
    time: str = "08:00",
    sources: list[QuoteSourceCfg] | None = None,
) -> Setup:
    lounge = LoungeSettings(
        guild_id=GUILD,
        channel_id=LOUNGE_ID,
        welcome_enabled=welcome,
        welcome_message=message,
        quote_enabled=quote,
        quote_time=time,
        quote_sources=[{"kind": s.kind, "value": s.value} for s in (sources or [SOURCE])],
        last_quote_date=None,
    )
    return Setup(GUILD, lounge, load_config(CONFIG_PATH))


def _seed(db_path, setup: Setup) -> None:
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, setup.guild_id, set_up=True, timezone=ZONE)
        repo.upsert_lounge(conn, setup.lounge)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


class FakeFlags:
    value = 7


class FakeGuild:
    def __init__(self, guild_id: int, name: str = "The Speakeasy") -> None:
        self.id = guild_id
        self.name = name


class FakeMember:
    def __init__(
        self,
        guild: FakeGuild,
        *,
        user_id: int = USER_ID,
        bot: bool = False,
        pending: bool = False,
    ) -> None:
        self.guild = guild
        self.id = user_id
        self.bot = bot
        self.pending = pending
        self.flags = FakeFlags()
        self.name = USER_NAME
        self.display_name = USER_NAME
        self.mention = f"<@{user_id}>"

    def __repr__(self) -> str:
        return f"FakeMember({self.name}, {self.id})"


class FakeSent:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, discord.AllowedMentions]] = []
        self.next_error: Exception | None = None

    async def send(self, content: str, *, allowed_mentions: discord.AllowedMentions):
        if self.next_error is not None:
            raise self.next_error
        self.sent.append((content, allowed_mentions))
        return FakeSent(900 + len(self.sent))


def _bot(setup: Setup, db_path, *, channel: FakeChannel | None = None, via_fetch: bool = False):
    """A NewsBot with this lounge row, whose channel lookups and server notices are local fakes."""
    _seed(db_path, setup)
    bot = NewsBot(setup.cfg, _secrets(), db_path, lounges=[setup.lounge])
    channel = channel if channel is not None else FakeChannel()
    bot.fake_channel = channel  # type: ignore[attr-defined]
    bot.alerts = []  # type: ignore[attr-defined]
    fetched: list[int] = []
    bot.fetched = fetched  # type: ignore[attr-defined]

    def get_channel(channel_id: int):
        return None if via_fetch else channel

    async def fetch_channel(channel_id: int):
        fetched.append(channel_id)
        return channel

    async def notify(guild_id: int, text: str) -> None:
        assert guild_id == setup.guild_id  # a lounge problem is told to that server and nobody else
        bot.alerts.append(text)  # type: ignore[attr-defined]

    bot.get_channel = get_channel  # type: ignore[method-assign]
    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]
    bot.guild_notifier = notify
    return bot


def _guild(setup: Setup) -> FakeGuild:
    return FakeGuild(setup.guild_id)


def _assert_only_this_member_mentionable(mentions: discord.AllowedMentions, member) -> None:
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    assert mentions.users == [member]


# --- intents ---


def test_intents_with_welcomes_on():
    intents = build_intents_for_lounges([_cfg(welcome=True).lounge])
    assert intents.members is True
    assert intents.presences is False
    assert intents.message_content is False


def test_intents_with_welcomes_off_even_if_quotes_are_on():
    intents = build_intents_for_lounges([_cfg(welcome=False, quote=True).lounge])
    assert intents.members is False
    assert intents.presences is False
    assert intents.message_content is False


def test_intents_with_no_lounge_row_at_all():
    assert build_intents_for_lounges([]).members is False


def test_the_bot_uses_the_rows_for_its_intents(db_path):
    on, off = _cfg(welcome=True), _cfg(welcome=False)
    assert NewsBot(on.cfg, _secrets(), db_path, lounges=[on.lounge]).intents.members is True
    assert NewsBot(off.cfg, _secrets(), db_path, lounges=[off.lounge]).intents.members is False
    assert NewsBot(off.cfg, _secrets(), db_path).intents.members is False


# --- welcomes ---


async def test_join_posts_one_welcome_with_exact_mentions(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)
    member = FakeMember(_guild(cfg))

    await bot.on_member_join(member)

    ((text, mentions),) = bot.fake_channel.sent
    assert text == f"Hi <@{USER_ID}>, The Speakeasy"
    _assert_only_this_member_mentionable(mentions, member)
    assert bot.alerts == []


async def test_owner_text_with_everyone_and_role_is_inert(db_path):
    cfg = _cfg(welcome=True, message="Hello {member} @everyone <@&123456789012345678>")
    bot = _bot(cfg, db_path)
    member = FakeMember(_guild(cfg))

    await bot.on_member_join(member)

    ((text, mentions),) = bot.fake_channel.sent
    assert "@everyone" in text and "<@&123456789012345678>" in text
    _assert_only_this_member_mentionable(mentions, member)


async def test_channel_falls_back_to_fetch(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path, via_fetch=True)

    await bot.on_member_join(FakeMember(_guild(cfg)))

    assert bot.fetched == [LOUNGE_ID]
    assert len(bot.fake_channel.sent) == 1


async def test_pending_member_waits_then_is_welcomed_on_acceptance(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)
    guild = _guild(cfg)
    pending = FakeMember(guild, pending=True)
    accepted = FakeMember(guild, pending=False)

    await bot.on_member_join(pending)
    assert bot.fake_channel.sent == []

    await bot.on_member_update(pending, accepted)
    assert len(bot.fake_channel.sent) == 1


async def test_updates_that_are_not_the_pending_transition_are_ignored(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)
    guild = _guild(cfg)

    # A nickname change: pending stays false.
    await bot.on_member_update(FakeMember(guild), FakeMember(guild))
    # Still pending after the update.
    await bot.on_member_update(FakeMember(guild, pending=True), FakeMember(guild, pending=True))
    # The reverse transition.
    await bot.on_member_update(FakeMember(guild), FakeMember(guild, pending=True))

    assert bot.fake_channel.sent == []


async def test_bots_are_never_welcomed(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)
    guild = _guild(cfg)

    await bot.on_member_join(FakeMember(guild, bot=True))
    await bot.on_member_update(
        FakeMember(guild, bot=True, pending=True), FakeMember(guild, bot=True)
    )

    assert bot.fake_channel.sent == []


async def test_other_guilds_are_ignored(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)

    await bot.on_member_join(FakeMember(FakeGuild(cfg.guild_id + 1)))

    assert bot.fake_channel.sent == []


async def test_same_member_is_welcomed_once_in_24_hours(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)
    member = FakeMember(_guild(cfg))

    await bot.on_member_join(member)
    await bot.on_member_join(member)
    await bot.on_member_update(FakeMember(member.guild, pending=True), member)

    assert len(bot.fake_channel.sent) == 1


async def test_two_simultaneous_events_for_one_member_post_once(db_path):
    cfg = _cfg(welcome=True)
    bot = _bot(cfg, db_path)
    member = FakeMember(_guild(cfg))

    await asyncio.gather(bot.on_member_join(member), bot.on_member_join(member))

    assert len(bot.fake_channel.sent) == 1


async def test_send_failure_gives_one_alert_that_names_no_member(db_path):
    cfg = _cfg(welcome=True)
    channel = FakeChannel()
    channel.next_error = RuntimeError(f"boom for {USER_NAME} {USER_ID}")
    bot = _bot(cfg, db_path, channel=channel)

    await bot.on_member_join(FakeMember(_guild(cfg)))

    (alert,) = bot.alerts
    assert "RuntimeError" in alert
    assert USER_NAME not in alert
    assert str(USER_ID) not in alert


async def test_handlers_are_inert_when_welcomes_are_disabled(db_path):
    cfg = _cfg(welcome=False, quote=True)
    bot = _bot(cfg, db_path)
    guild = _guild(cfg)

    await bot.on_member_join(FakeMember(guild))
    await bot.on_member_update(FakeMember(guild, pending=True), FakeMember(guild))

    assert bot.fake_channel.sent == []
    assert bot.alerts == []


async def test_member_event_logs_never_carry_the_user_id_or_name(db_path, caplog):
    cfg = _cfg(welcome=True)
    channel = FakeChannel()
    bot = _bot(cfg, db_path, channel=channel)
    guild = _guild(cfg)
    caplog.set_level(logging.DEBUG)

    member = FakeMember(guild)
    await bot.on_member_join(member)  # welcomed
    await bot.on_member_join(member)  # recent
    await bot.on_member_join(FakeMember(guild, user_id=USER_ID + 1, pending=True))  # wait
    await bot.on_member_join(FakeMember(guild, user_id=USER_ID + 2, bot=True))  # ignore
    channel.next_error = RuntimeError("nope")
    await bot.on_member_join(FakeMember(guild, user_id=USER_ID + 3))  # failure path

    member_records = [r for r in caplog.records if r.name == "newsbot.bot.client"]
    assert member_records
    for record in member_records:
        blob = record.getMessage() + repr(record.__dict__)
        assert USER_NAME not in blob
        for uid in range(USER_ID, USER_ID + 4):
            assert str(uid) not in blob
    decisions = [
        r.decision
        for r in member_records
        if hasattr(r, "decision")  # type: ignore[attr-defined]
    ]
    assert decisions == ["welcome", "recent", "wait", "ignore", "welcome"]
    logged = next(r for r in member_records if hasattr(r, "flags"))
    assert logged.flags == 7  # type: ignore[attr-defined]


# --- the schedule ---


def test_schedule_guild_quote_fields():
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("UTC"))

    async def callback() -> None:
        pass

    job = schedule_guild_quote(scheduler, GUILD, "07:45", ZONE, callback)

    assert job.id == f"daily-quote-{GUILD}"
    assert job.misfire_grace_time == 300
    assert job.coalesce is True
    assert job.max_instances == 1
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "7"
    assert fields["minute"] == "45"
    assert str(job.trigger.timezone) == ZONE


def test_schedule_guild_quote_stays_at_local_time_across_dst():
    tz = ZoneInfo(ZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    async def callback() -> None:
        pass

    trigger = schedule_guild_quote(scheduler, GUILD, "08:00", ZONE, callback).trigger

    # A day either side of a US spring-forward (2026-03-08) and fall-back (2026-11-01).
    for start in (datetime(2026, 3, 7, 12, tzinfo=tz), datetime(2026, 10, 31, 12, tzinfo=tz)):
        firings = []
        cursor = start
        for _ in range(3):
            nxt = trigger.get_next_fire_time(None, cursor)
            firings.append(nxt)
            cursor = nxt + timedelta(seconds=1)
        assert all((f.hour, f.minute) == (8, 0) for f in firings)


async def test_the_quote_job_exists_only_when_the_quote_is_enabled(db_path):
    # v2 asked setup_hook to add the one job; the per-server jobs come from the rows, once
    # the scheduler exists (`schedule_lounge_quotes`, which `on_ready` calls).
    for enabled in (True, False):
        setup = _cfg(quote=enabled)
        _seed(db_path, setup)
        bot = NewsBot(setup.cfg, _secrets(), db_path, lounges=[setup.lounge])

        async def _fake_sync(*args, **kwargs):
            return []

        bot.tree.sync = _fake_sync  # type: ignore[method-assign]
        try:
            await bot.setup_hook()
            assert bot.scheduler.get_job(f"daily-quote-{GUILD}") is None  # not before on_ready
            await bot.schedule_lounge_quotes()
            job = bot.scheduler.get_job(f"daily-quote-{GUILD}")
            assert (job is not None) is enabled
        finally:
            bot.scheduler.shutdown(wait=False)
            await bot.http_client.aclose()


async def test_a_missed_quote_is_skipped_not_run_late():
    ran: list[int] = []

    async def callback() -> None:
        ran.append(1)

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(ZONE))
    scheduler.start(paused=True)
    try:
        job = schedule_guild_quote(scheduler, GUILD, "08:00", ZONE, callback)
        now = datetime.now(UTC)
        scheduler.modify_job(job.id, next_run_time=now - timedelta(minutes=10))
        scheduler.resume()
        await asyncio.sleep(0.5)

        assert ran == []
        upcoming = scheduler.get_job(job.id).next_run_time
        assert upcoming > now
        assert upcoming - now <= timedelta(hours=24, minutes=1)
        assert (upcoming.hour, upcoming.minute) == (8, 0)
    finally:
        scheduler.shutdown(wait=False)


async def test_on_ready_never_runs_the_quote_job(db_path, monkeypatch):
    bot = _bot(_cfg(quote=True), db_path)
    bot.http_client = httpx.AsyncClient()
    bot.llm = object()  # type: ignore[assignment]
    bot._wire()
    calls: list[str] = []

    async def boom(*args, **kwargs):
        calls.append("quote")
        raise AssertionError("on_ready must not run the quote")

    monkeypatch.setattr(client_module, "run_daily_quote", boom)
    bot.run_guild_quote = boom  # type: ignore[method-assign]
    bot._guild_quote_job = boom  # type: ignore[method-assign]
    try:
        await bot.on_ready()
    finally:
        await bot.http_client.aclose()

    assert "quote" not in calls


# --- the deps and the lock ---


async def test_build_guild_quote_deps_wires_sources_cache_clock_and_post(db_path):
    setup = _cfg(quote=True)
    bot = _bot(setup, db_path)
    bot.http_client = httpx.AsyncClient()
    try:
        deps = bot.build_guild_quote_deps(setup.lounge, ZONE)

        assert deps.sources == [SOURCE]
        assert deps.db_path == db_path
        assert deps.http is bot.http_client
        assert deps.guild_id == GUILD
        assert deps.cache_dir == Path(db_path).with_name("newsbot-lounge-cache")
        assert isinstance(deps.rng, random.SystemRandom)
        assert type(deps.local_day) is date
        assert deps.now().tzinfo is not None

        await deps.alert("a source note")
        assert bot.alerts == ["a source note"]  # that server's notifier, not the owner's channel

        message_id = await deps.post("@everyone <@&123456789012345678> hello")
        ((_, mentions),) = bot.fake_channel.sent
        assert message_id == 901
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert mentions.replied_user is False
    finally:
        await bot.http_client.aclose()


async def test_build_guild_quote_deps_before_setup_hook_is_a_clear_error(db_path):
    setup = _cfg(quote=True)
    bot = NewsBot(setup.cfg, _secrets(), db_path, lounges=[setup.lounge])
    with pytest.raises(RuntimeError, match="setup_hook"):
        bot.build_guild_quote_deps(setup.lounge, ZONE)


async def test_default_source_list_reaches_the_deps(db_path):
    resolved = [SOURCE, QuoteSourceCfg(kind="wikiquote", value="Mark Twain")]
    setup = _cfg(quote=True, sources=resolved)
    bot = NewsBot(setup.cfg, _secrets(), db_path, lounges=[setup.lounge])
    bot.http_client = httpx.AsyncClient()
    try:
        assert bot.build_guild_quote_deps(setup.lounge, ZONE).sources == resolved
    finally:
        await bot.http_client.aclose()


async def test_job_and_forced_run_serialize_through_the_lock(db_path, monkeypatch):
    bot = _bot(_cfg(quote=True), db_path)
    bot.http_client = httpx.AsyncClient()
    events: list[str] = []
    running = 0
    overlapped = False

    async def fake_run(deps, *, force: bool = False) -> QuoteOutcome:
        nonlocal running, overlapped
        running += 1
        overlapped = overlapped or running > 1
        events.append(f"start force={force}")
        await asyncio.sleep(0.05)
        events.append(f"end force={force}")
        running -= 1
        return QuoteOutcome("posted", 1)

    monkeypatch.setattr(client_module, "run_daily_quote", fake_run)
    try:
        job = asyncio.create_task(bot._guild_quote_job(GUILD))
        await asyncio.sleep(0)  # let the job take the lock first
        forced = asyncio.create_task(bot.run_guild_quote(GUILD, True))
        outcome = await forced
        await job
    finally:
        await bot.http_client.aclose()

    assert outcome.status == "posted"
    assert overlapped is False
    assert events == ["start force=False", "end force=False", "start force=True", "end force=True"]


async def test_quote_job_crash_alerts_once_and_does_not_raise(db_path):
    bot = _bot(_cfg(quote=True), db_path)  # no http client, so the quote run raises

    await bot._guild_quote_job(GUILD)

    (alert,) = bot.alerts
    assert alert.startswith("newsbot: daily quote job crashed")


async def test_quote_job_lets_cancellation_through(db_path, monkeypatch):
    bot = _bot(_cfg(quote=True), db_path)

    async def cancelled(guild_id: int, force: bool):
        raise asyncio.CancelledError

    bot.run_guild_quote = cancelled  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await bot._guild_quote_job(GUILD)
    assert bot.alerts == []
