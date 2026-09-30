"""The per-guild lounge wiring in `NewsBot` (design.md §15, plan task 12).

v2.2's lounge tests live in tests/test_lounge_client.py and keep passing; this
file covers the path that reads `guild_lounge` rows: which events get a welcome,
how the state is keyed, one cron job per server in that server's zone, and where
problems are sent. No gateway, no network: the channels are local fakes and the
quote sources are files in `tmp_path`.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import SecretStr
from v3_fakes import FakeInteraction, command

import newsbot.bot.client as client_module
from newsbot.bot.client import NewsBot, build_intents_for_lounges
from newsbot.bot.commands import make_lounge_group
from newsbot.config import Secrets, load_config
from newsbot.lounge.daily import QuoteOutcome
from newsbot.pipeline.run import local_run_date
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
G1, G2, G3 = 100000000000000001, 100000000000000002, 100000000000000003
CH1, CH2 = 555000000000000001, 555000000000000002
USER_ID = 424242424242424242
USER_NAME = "Distinctive_Guest_Name"
SEP = "\n%\n"


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _row(
    gid, channel, *, welcome=True, quote=True, message="Hi {member}, {server}", t="08:00", src=()
):
    return LoungeSettings(gid, channel, welcome, message, quote, t, list(src), None)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _seed(db_path, *rows, zones=None):
    zones = zones or {}
    with closing(connect(db_path)) as conn:
        for row in rows:
            repo.create_guild(conn, row.guild_id, timezone=zones.get(row.guild_id, "UTC"))
            repo.upsert_lounge(conn, row)


class FakeFlags:
    value = 7


class FakeGuild:
    def __init__(self, guild_id: int, name: str = "The Speakeasy") -> None:
        self.id = guild_id
        self.name = name
        self.chunked = False
        self.chunks = 0

    async def chunk(self) -> None:
        self.chunks += 1
        self.chunked = True


class FakeMember:
    def __init__(self, guild, *, user_id=USER_ID, bot=False, pending=False) -> None:
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


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, discord.AllowedMentions]] = []
        self.next_error: Exception | None = None

    async def send(self, content: str, *, allowed_mentions: discord.AllowedMentions):
        if self.next_error is not None:
            raise self.next_error
        self.sent.append((content, allowed_mentions))

        class Sent:
            id = 900 + len(self.sent)

        return Sent()


def _bot(db_path, lounges, channels=None):
    """A lounge-mode NewsBot with fake channels and a recording notifier."""
    cfg = load_config(CONFIG_PATH)
    bot = NewsBot(cfg, _secrets(), db_path, lounges=lounges)
    chans = channels if channels is not None else {CH1: FakeChannel(), CH2: FakeChannel()}
    bot.chans = chans  # type: ignore[attr-defined]
    bot.notices = []  # type: ignore[attr-defined]
    bot.owner_alerts = []  # type: ignore[attr-defined]
    bot.get_channel = lambda cid: chans.get(cid)  # type: ignore[method-assign]

    async def notify(gid: int, text: str) -> None:
        bot.notices.append((gid, text))  # type: ignore[attr-defined]

    async def owner_alert(text: str) -> None:
        bot.owner_alerts.append(text)  # type: ignore[attr-defined]

    bot.guild_notifier = notify
    bot.alert = owner_alert  # type: ignore[method-assign]
    return bot


# --- intents ---


def test_members_intent_is_on_only_when_some_row_has_welcomes_on():
    assert build_intents_for_lounges([_row(G1, CH1, welcome=True)]).members is True
    assert build_intents_for_lounges([_row(G1, CH1, welcome=False), _row(G2, CH2)]).members is True
    assert build_intents_for_lounges([_row(G1, CH1, welcome=False)]).members is False
    assert build_intents_for_lounges([]).members is False


def test_the_other_intents_stay_at_the_defaults():
    intents = build_intents_for_lounges([_row(G1, CH1)])
    default = discord.Intents.default()
    assert intents.presences is False and intents.message_content is False
    assert intents.value == default.value | discord.Intents(members=True).value


async def test_the_client_follows_the_rows_and_does_not_chunk_at_startup(db_path):
    bot = _bot(db_path, [_row(G1, CH1)])
    assert bot.intents.members is True
    assert bot._connection._chunk_guilds is False
    quiet = _bot(db_path, [_row(G1, CH1, welcome=False)])
    assert quiet.intents.members is False


async def test_without_lounges_the_bot_is_still_v22s(db_path):
    bot = NewsBot(load_config(CONFIG_PATH), _secrets(), db_path)
    assert bot._lounges == {}
    assert bot.intents.members is False  # config_valid.yaml has no lounge block


async def test_chunking_touches_only_lounge_guilds_with_welcomes_on(db_path):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2, welcome=False)])
    guilds = {g: FakeGuild(g) for g in (G1, G2, G3)}
    bot.get_guild = guilds.get  # type: ignore[method-assign]
    await bot.chunk_lounge_guilds()
    assert [guilds[g].chunks for g in (G1, G2, G3)] == [1, 0, 0]
    await bot.chunk_lounge_guilds()  # already chunked: not asked again
    assert guilds[G1].chunks == 1


async def test_a_failed_chunk_is_logged_without_stopping_the_rest(db_path, caplog):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    guilds = {G1: FakeGuild(G1), G2: FakeGuild(G2)}

    async def boom() -> None:
        raise RuntimeError("nope")

    guilds[G1].chunk = boom  # type: ignore[method-assign]
    bot.get_guild = guilds.get  # type: ignore[method-assign]
    await bot.chunk_lounge_guilds()
    assert guilds[G2].chunks == 1


# --- welcomes ---


async def test_a_join_in_a_lounge_guild_posts_that_guilds_welcome_to_its_channel(db_path):
    rows = [
        _row(G1, CH1, message="Hi {member}, welcome to {server}."),
        _row(G2, CH2, message="Pull up a stool, {member}."),
    ]
    bot = _bot(db_path, rows)
    member = FakeMember(FakeGuild(G2, "Other Bar"))
    await bot.handle_member_join(member)
    assert bot.chans[CH1].sent == []
    ((text, mentions),) = bot.chans[CH2].sent
    assert text == f"Pull up a stool, <@{USER_ID}>."
    assert mentions.everyone is False and mentions.roles is False
    assert mentions.replied_user is False and mentions.users == [member]


async def test_the_server_name_is_that_guilds(db_path):
    bot = _bot(db_path, [_row(G1, CH1)])
    await bot.handle_member_join(FakeMember(FakeGuild(G1, "Room 101")))
    assert bot.chans[CH1].sent[0][0] == f"Hi <@{USER_ID}>, Room 101"


@pytest.mark.parametrize("event", ["join", "update"])
async def test_events_in_a_non_lounge_guild_are_ignored(db_path, event):
    bot = _bot(db_path, [_row(G1, CH1)])
    stranger = FakeMember(FakeGuild(G3))
    if event == "join":
        await bot.handle_member_join(stranger)
    else:
        await bot.handle_member_update(FakeMember(FakeGuild(G3), pending=True), stranger)
    assert bot.chans[CH1].sent == [] and bot.chans[CH2].sent == []
    assert bot.notices == []


async def test_a_lounge_with_welcomes_off_and_a_guild_with_no_row_stay_silent(db_path):
    bot = _bot(db_path, [_row(G1, CH1, welcome=False)])
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    await bot.handle_member_update(
        FakeMember(FakeGuild(G1), pending=True), FakeMember(FakeGuild(G1))
    )
    assert bot.chans[CH1].sent == []


async def test_the_no_rewelcome_map_is_keyed_by_guild_and_user(db_path):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))  # same guild, same user: once
    await bot.handle_member_join(FakeMember(FakeGuild(G2)))  # same user, other guild: welcomed
    assert len(bot.chans[CH1].sent) == 1
    assert len(bot.chans[CH2].sent) == 1


async def test_a_pending_member_waits_and_is_welcomed_on_acceptance(db_path):
    bot = _bot(db_path, [_row(G1, CH1)])
    guild = FakeGuild(G1)
    await bot.handle_member_join(FakeMember(guild, pending=True))
    assert bot.chans[CH1].sent == []
    await bot.handle_member_update(FakeMember(guild, pending=True), FakeMember(guild))
    assert len(bot.chans[CH1].sent) == 1
    # Not the pending flip (a nickname change, say): nothing.
    await bot.handle_member_update(FakeMember(guild), FakeMember(guild, user_id=USER_ID + 1))
    assert len(bot.chans[CH1].sent) == 1


async def test_bots_are_never_welcomed(db_path):
    bot = _bot(db_path, [_row(G1, CH1)])
    await bot.handle_member_join(FakeMember(FakeGuild(G1), bot=True))
    assert bot.chans[CH1].sent == []


async def test_mentions_in_owner_text_stay_inert(db_path):
    bot = _bot(db_path, [_row(G1, CH1, message="@everyone <@&123456789012345678> {member}")])
    member = FakeMember(FakeGuild(G1))
    await bot.handle_member_join(member)
    ((_, mentions),) = bot.chans[CH1].sent
    assert mentions.everyone is False and mentions.roles is False and mentions.users == [member]


async def test_a_failed_welcome_notifies_that_guild_only_and_names_nobody(db_path, caplog):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    bot.chans[CH1].next_error = RuntimeError(f"cannot send to {USER_NAME} {USER_ID}")
    caplog.set_level(logging.DEBUG)
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    assert [gid for gid, _ in bot.notices] == [G1]
    assert bot.owner_alerts == []
    notice = bot.notices[0][1]
    assert "RuntimeError" in notice and USER_NAME not in notice and str(USER_ID) not in notice
    assert USER_NAME not in caplog.text and str(USER_ID) not in caplog.text


async def test_member_events_never_put_a_name_or_id_in_the_logs(db_path, caplog):
    bot = _bot(db_path, [_row(G1, CH1)])
    caplog.set_level(logging.DEBUG)
    guild = FakeGuild(G1)
    await bot.handle_member_join(FakeMember(guild, pending=True))
    await bot.handle_member_update(FakeMember(guild, pending=True), FakeMember(guild))
    await bot.handle_member_join(FakeMember(guild))  # recent
    assert caplog.records
    for record in caplog.records:
        assert USER_NAME not in record.getMessage() and str(USER_ID) not in record.getMessage()
        assert USER_NAME not in repr(record.__dict__) and str(USER_ID) not in repr(record.__dict__)


async def test_a_failed_welcome_with_no_notifier_wired_does_not_raise(db_path):
    bot = _bot(db_path, [_row(G1, CH1)])
    bot.guild_notifier = None
    bot.chans[CH1].next_error = RuntimeError("x")
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    assert bot.owner_alerts == []  # never falls back to the owner


async def test_reload_lounges_picks_up_a_changed_row(db_path):
    _seed(db_path, _row(G1, CH1, message="old {member}"))
    bot = _bot(db_path, [])
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    assert bot.chans[CH1].sent == []  # not loaded yet
    await bot.reload_lounges()
    with closing(connect(db_path)) as conn:
        repo.upsert_lounge(conn, _row(G1, CH1, message="new {member}"))
    await bot.reload_lounges()
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    assert bot.chans[CH1].sent[0][0] == f"new <@{USER_ID}>"


# --- the quote job and run ---


def _files(tmp_path, name, quotes):
    path = tmp_path / name
    path.write_text(SEP.join(quotes) + "\n", encoding="utf-8")
    return {"kind": "file", "value": str(path)}


async def _with_http(bot):
    bot.http_client = httpx.AsyncClient()


async def test_the_job_time_is_in_the_guilds_own_zone(db_path):
    _seed(
        db_path,
        _row(G1, CH1, t="08:00"),
        _row(G2, CH2, t="21:30"),
        zones={G1: "America/Los_Angeles", G2: "Asia/Tokyo"},
    )
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    assert await bot.schedule_lounge_quotes() == [G1, G2]
    jobs = {j.id: j for j in bot.scheduler.get_jobs()}
    assert set(jobs) == {f"daily-quote-{G1}", f"daily-quote-{G2}"}
    for gid, zone, hour, minute in ((G1, "America/Los_Angeles", 8, 0), (G2, "Asia/Tokyo", 21, 30)):
        trigger = jobs[f"daily-quote-{gid}"].trigger
        assert trigger.timezone == ZoneInfo(zone)
        fields = {f.name: str(f) for f in trigger.fields}
        assert (fields["hour"], fields["minute"]) == (str(hour), str(minute))
        assert jobs[f"daily-quote-{gid}"].misfire_grace_time == 300
        assert jobs[f"daily-quote-{gid}"].max_instances == 1
        assert jobs[f"daily-quote-{gid}"].coalesce is True


async def test_a_missed_guild_quote_is_skipped_not_run_late(db_path):
    _seed(db_path, _row(G1, CH1, t="08:00"), zones={G1: "UTC"})
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    await bot.schedule_lounge_quotes()
    (job,) = bot.scheduler.get_jobs()
    # An hour late is outside the five-minute grace, so APScheduler drops it.
    assert job.misfire_grace_time == 300
    assert bot.scheduler.get_job(f"daily-quote-{G1}") is job


async def test_only_lounges_with_the_quote_on_get_a_job(db_path):
    _seed(db_path, _row(G1, CH1, quote=False), _row(G2, CH2, welcome=False))
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    assert await bot.schedule_lounge_quotes() == [G2]


async def test_a_bad_zone_leaves_that_guild_out_and_tells_it(db_path):
    _seed(db_path, _row(G1, CH1), _row(G2, CH2), zones={G1: "Mars/Olympus", G2: "UTC"})
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    assert await bot.schedule_lounge_quotes() == [G2]
    assert [gid for gid, _ in bot.notices] == [G1]
    assert bot.owner_alerts == []


async def test_deps_use_the_guilds_zone_row_channel_and_notifier(db_path):
    row = _row(G1, CH1, src=[{"kind": "wikiquote", "value": "Oscar Wilde"}])
    _seed(db_path, row)
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        ahead = bot.build_guild_quote_deps(row, "Pacific/Kiritimati")
        behind = bot.build_guild_quote_deps(row, "Pacific/Pago_Pago")
        assert ahead.local_day > behind.local_day
        assert ahead.local_day == local_run_date(datetime.now(UTC), "Pacific/Kiritimati")
        assert ahead.guild_id == G1
        assert [s.key for s in ahead.sources] == ["wikiquote:Oscar Wilde"]
        assert await ahead.post("@everyone hi") == 901
        (_, mentions) = bot.chans[CH1].sent[0]
        assert mentions.everyone is False and mentions.users is False
        await ahead.alert("trouble")
        assert bot.notices == [(G1, "trouble")] and bot.owner_alerts == []
    finally:
        await bot.http_client.aclose()


async def test_two_guilds_run_their_own_quotes_with_their_own_guards(tmp_path, db_path):
    _seed(
        db_path,
        _row(G1, CH1, src=[_files(tmp_path, "a.txt", ["alpha"])]),
        _row(G2, CH2, src=[_files(tmp_path, "b.txt", ["beta"])]),
        zones={G1: "America/Los_Angeles", G2: "Asia/Tokyo"},
    )
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        assert (await bot.run_guild_quote(G1, False)).status == "posted"
        assert (await bot.run_guild_quote(G1, False)).status == "already_posted"
        assert (await bot.run_guild_quote(G2, False)).status == "posted"
        assert "alpha" in bot.chans[CH1].sent[0][0] and len(bot.chans[CH1].sent) == 1
        assert "beta" in bot.chans[CH2].sent[0][0] and len(bot.chans[CH2].sent) == 1
        assert bot.notices == [] and bot.owner_alerts == []
    finally:
        await bot.http_client.aclose()


async def test_the_run_reads_the_row_fresh_each_time(tmp_path, db_path):
    """A re-sync after startup (D5) changes what the next run does, with no reload."""
    _seed(db_path, _row(G1, CH1, src=[_files(tmp_path, "a.txt", ["old source"])]))
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        with closing(connect(db_path)) as conn:
            repo.upsert_lounge(conn, _row(G1, CH1, src=[_files(tmp_path, "b.txt", ["new source"])]))
        await bot.run_guild_quote(G1, False)
        assert "new source" in bot.chans[CH1].sent[0][0]
    finally:
        await bot.http_client.aclose()


async def test_source_trouble_goes_to_that_guilds_admins_not_the_owner(tmp_path, db_path):
    missing = {"kind": "file", "value": str(tmp_path / "gone.txt")}
    _seed(db_path, _row(G1, CH1, src=[missing]))
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        out = await bot.run_guild_quote(G1, False)
        assert out.status == "skipped"
        assert [gid for gid, _ in bot.notices] == [G1]
        assert bot.owner_alerts == [] and bot.chans[CH1].sent == []
    finally:
        await bot.http_client.aclose()


async def test_a_guild_with_no_row_or_the_quote_off_does_nothing(db_path):
    _seed(db_path, _row(G1, CH1, quote=False))
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        assert (await bot.run_guild_quote(G1, False)).status == "no_lounge"
        assert (await bot.run_guild_quote(G3, False)).status == "no_lounge"
        assert bot.chans[CH1].sent == [] and bot.notices == []
    finally:
        await bot.http_client.aclose()


async def test_a_broken_zone_skips_the_run_and_tells_the_guild(tmp_path, db_path):
    _seed(
        db_path, _row(G1, CH1, src=[_files(tmp_path, "a.txt", ["alpha"])]), zones={G1: "Nope/Zone"}
    )
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        assert (await bot.run_guild_quote(G1, False)).status == "skipped"
        assert len(bot.notices) == 1 and bot.chans[CH1].sent == []
    finally:
        await bot.http_client.aclose()


async def test_the_lock_is_per_guild(db_path, monkeypatch):
    _seed(db_path, _row(G1, CH1), _row(G2, CH2))
    bot = _bot(db_path, [])
    await _with_http(bot)
    events: list[str] = []
    release = asyncio.Event()

    async def fake_run(deps, *, force):
        events.append(f"start {deps.guild_id}")
        if deps.guild_id == G1:
            await release.wait()
        events.append(f"end {deps.guild_id}")
        return QuoteOutcome("posted")

    monkeypatch.setattr(client_module, "run_daily_quote", fake_run)
    try:
        first = asyncio.create_task(bot.run_guild_quote(G1, False))
        again = asyncio.create_task(bot.run_guild_quote(G1, True))
        other = asyncio.create_task(bot.run_guild_quote(G2, False))
        await asyncio.sleep(0.05)
        # G2 ran to the end while G1 was held; G1's second run is waiting its turn.
        assert sorted(events) == sorted([f"start {G1}", f"start {G2}", f"end {G2}"])
        release.set()
        await asyncio.gather(first, again, other)
        assert events[3:] == [f"end {G1}", f"start {G1}", f"end {G1}"]  # the second G1 run waited
    finally:
        await bot.http_client.aclose()


async def test_the_job_crash_boundary_notifies_the_guild_once(db_path, monkeypatch):
    bot = _bot(db_path, [])

    async def boom(guild_id, force):
        raise RuntimeError("@everyone kaboom")

    monkeypatch.setattr(bot, "run_guild_quote", boom)
    await bot._guild_quote_job(G1)
    assert len(bot.notices) == 1 and bot.notices[0][0] == G1
    assert "@everyone" not in bot.notices[0][1].replace("@​everyone", "")
    assert bot.owner_alerts == []


async def test_the_job_lets_cancellation_through(db_path, monkeypatch):
    bot = _bot(db_path, [])

    async def cancelled(guild_id, force):
        raise asyncio.CancelledError

    monkeypatch.setattr(bot, "run_guild_quote", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await bot._guild_quote_job(G1)


async def test_a_job_for_a_removed_guild_removes_itself(db_path):
    _seed(db_path, _row(G1, CH1))
    bot = _bot(db_path, [])
    await _with_http(bot)
    bot.scheduler = AsyncIOScheduler()
    try:
        await bot.schedule_lounge_quotes()
        assert bot.scheduler.get_job(f"daily-quote-{G1}") is not None
        with closing(connect(db_path)) as conn:
            repo.delete_guild(conn, G1)
        await bot._guild_quote_job(G1)
        assert bot.scheduler.get_job(f"daily-quote-{G1}") is None
        assert bot.notices == []
    finally:
        await bot.http_client.aclose()


# --- /lounge quote-now, per guild ---


async def test_lounge_quote_now_runs_each_guilds_own_quote(tmp_path, db_path):
    _seed(
        db_path,
        _row(G1, CH1, src=[_files(tmp_path, "a.txt", ["alpha"])]),
        _row(G2, CH2, src=[_files(tmp_path, "b.txt", ["beta"])]),
    )
    bot = _bot(db_path, [])
    await _with_http(bot)
    cfg = load_config(CONFIG_PATH)
    group = make_lounge_group(cfg, bot)
    try:
        second = FakeInteraction(guild_id=G2)
        await command(group, "quote-now").callback(second)
        assert bot.chans[CH1].sent == [] and "beta" in bot.chans[CH2].sent[0][0]
        assert second.text == f"Posted: https://discord.com/channels/{G2}/{CH2}/901"
        first = FakeInteraction(guild_id=G1)
        await command(group, "quote-now").callback(first)
        assert "alpha" in bot.chans[CH1].sent[0][0]
        assert first.text == f"Posted: https://discord.com/channels/{G1}/{CH1}/901"
    finally:
        await bot.http_client.aclose()


async def test_lounge_quote_now_in_a_guild_without_a_lounge_is_refused(db_path):
    _seed(db_path, _row(G1, CH1))
    bot = _bot(db_path, [])
    group = make_lounge_group(load_config(CONFIG_PATH), bot)
    interaction = FakeInteraction(guild_id=G3)
    await command(group, "quote-now").callback(interaction)
    assert "doesn't have a lounge quote set up" in interaction.text
    assert bot.chans[CH1].sent == []
