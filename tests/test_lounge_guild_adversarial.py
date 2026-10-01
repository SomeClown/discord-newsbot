"""Adversarial tests for the per-guild lounge wiring in `NewsBot` (plan task 12).

`test_lounge_guild_client.py` proves the glue works when one or two guilds
behave. This file is the crowded bar: three lounges with different zones,
channels and (sometimes) the very same source; one guest walking into two of
them; a server that leaves between the cron firing and the quote loading; a
`/lounge quote-now` and the scheduled job arriving in the same breath; and a
member-cache chunk that never comes back. Where the code does something the
owner might want to reconsider, the test pins what it does today and the
comment says so, so changing it later is a decision and not a surprise. The
xfails are real bugs, strict, with the reason attached; fixing them belongs to
the implement agent.

Helpers come from `test_lounge_guild_client.py`: the same hand-written fakes,
no gateway, no network, the quote sources are files in `tmp_path`.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import types
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from test_lounge_guild_client import (
    CH1,
    CH2,
    G1,
    G2,
    G3,
    USER_ID,
    USER_NAME,
    FakeChannel,
    FakeGuild,
    FakeMember,
    _bot,
    _files,
    _row,
    _secrets,
    _seed,
    _with_http,
    build_intents_for_lounges,
)
from v3_fakes import FakeInteraction, command

import newsbot.bot.client as client_module
from newsbot.bot.client import NewsBot
from newsbot.bot.commands import make_lounge_group
from newsbot.config import load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.lounge import daily
from newsbot.lounge.daily import QuoteOutcome
from newsbot.lounge.quotes import Quote
from newsbot.lounge.sources import LoadedSource
from newsbot.lounge.welcome import RecentWelcomes
from newsbot.pipeline.run import local_run_date
from newsbot.store import repo
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
FIXTURES = Path(__file__).parent / "fixtures"
CH3 = 555000000000000003


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


def _channel_in(guild_id):
    """A fake channel that really lives in `guild_id` (a lounge send checks that)."""
    channel = FakeChannel()
    channel.guild = types.SimpleNamespace(id=guild_id)
    return channel


def _three_channels():
    return {CH1: _channel_in(G1), CH2: _channel_in(G2), CH3: _channel_in(G3)}


def _date_of(db_path, gid):
    with closing(connect(db_path)) as conn:
        return repo.get_lounge(conn, gid).last_quote_date


# --- isolation between lounge guilds ---


async def test_three_guilds_on_the_same_source_each_post_only_in_their_own_channel(
    tmp_path, db_path
):
    shared = [_files(tmp_path, "shared.txt", ["the one and only quote"])]
    _seed(
        db_path,
        _row(G1, CH1, src=shared),
        _row(G2, CH2, src=shared),
        _row(G3, CH3, src=shared),
        zones={G1: "America/Los_Angeles", G2: "Asia/Tokyo", G3: "Europe/Berlin"},
    )
    chans = _three_channels()
    bot = _bot(db_path, [], channels=chans)
    await _with_http(bot)
    try:
        for gid in (G1, G2, G3):
            assert (await bot.run_guild_quote(gid, False)).status == "posted"
        for ch in (CH1, CH2, CH3):
            assert len(chans[ch].sent) == 1 and "the one and only quote" in chans[ch].sent[0][0]
        assert bot.notices == [] and bot.owner_alerts == []
    finally:
        await bot.http_client.aclose()


async def test_each_guilds_guard_uses_its_own_zone_so_dates_can_differ(tmp_path, db_path):
    src = [_files(tmp_path, "a.txt", ["alpha", "beta"])]
    _seed(
        db_path,
        _row(G1, CH1, src=src),
        _row(G2, CH2, src=src),
        zones={G1: "Pacific/Kiritimati", G2: "Pacific/Pago_Pago"},
    )
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        await bot.run_guild_quote(G1, False)
        await bot.run_guild_quote(G2, False)
        now = datetime.now(UTC)
        assert _date_of(db_path, G1) == local_run_date(now, "Pacific/Kiritimati").isoformat()
        assert _date_of(db_path, G2) == local_run_date(now, "Pacific/Pago_Pago").isoformat()
        assert _date_of(db_path, G1) > _date_of(db_path, G2)  # a real day apart
    finally:
        await bot.http_client.aclose()


async def test_a_broken_source_in_one_guild_tells_only_that_guild_and_spares_the_rest(
    tmp_path, db_path
):
    broken = {"kind": "file", "value": str(tmp_path / "missing.txt")}
    _seed(
        db_path,
        _row(G1, CH1, src=[broken]),
        _row(G2, CH2, src=[_files(tmp_path, "b.txt", ["beta"])]),
    )
    bot = _bot(db_path, [])
    await _with_http(bot)
    try:
        assert (await bot.run_guild_quote(G1, False)).status == "skipped"
        assert (await bot.run_guild_quote(G2, False)).status == "posted"
        assert [gid for gid, _ in bot.notices] == [G1]
        assert bot.chans[CH1].sent == [] and len(bot.chans[CH2].sent) == 1
    finally:
        await bot.http_client.aclose()


async def test_a_quote_never_lands_in_another_guilds_channel_even_if_channels_are_swapped(
    tmp_path, db_path
):
    """Two rows pointing at each other's channel ids: each row's own channel is what counts."""
    _seed(
        db_path,
        _row(G1, CH2, src=[_files(tmp_path, "a.txt", ["alpha"])]),
        _row(G2, CH1, src=[_files(tmp_path, "b.txt", ["beta"])]),
    )
    # Each channel really lives in the guild whose row points at it.
    bot = _bot(db_path, [], channels={CH2: _channel_in(G1), CH1: _channel_in(G2)})
    await _with_http(bot)
    try:
        await bot.run_guild_quote(G1, False)
        assert "alpha" in bot.chans[CH2].sent[0][0] and bot.chans[CH1].sent == []
    finally:
        await bot.http_client.aclose()


async def test_a_lounge_row_naming_another_servers_channel_posts_nothing(tmp_path, db_path):
    """QA: the quote and welcome sends take the same foreign-channel check as the digest."""
    _seed(db_path, _row(G1, CH2, src=[_files(tmp_path, "a.txt", ["alpha"])]))
    bot = _bot(db_path, [])  # CH2 lives in G2, but G1's row points at it
    await _with_http(bot)
    try:
        assert (await bot.run_guild_quote(G1, False)).status == "post_failed"
        assert bot.chans[CH2].sent == []
        await bot.handle_member_join(FakeMember(FakeGuild(G1)))
        assert bot.chans[CH2].sent == []
    finally:
        await bot.http_client.aclose()


async def test_the_same_guest_joining_two_lounges_is_welcomed_in_both_and_once_each(db_path):
    bot = _bot(
        db_path, [_row(G1, CH1, message="one {member}"), _row(G2, CH2, message="two {member}")]
    )
    a, b = FakeGuild(G1), FakeGuild(G2)
    await bot.handle_member_join(FakeMember(a))
    await bot.handle_member_join(FakeMember(b))
    await bot.handle_member_join(FakeMember(a))  # rejoin inside 24 hours
    await bot.handle_member_join(FakeMember(b))
    assert [t for t, _ in bot.chans[CH1].sent] == [f"one <@{USER_ID}>"]
    assert [t for t, _ in bot.chans[CH2].sent] == [f"two <@{USER_ID}>"]


async def test_a_second_guilds_problem_notice_never_reaches_the_first(db_path):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    bot.chans[CH2].next_error = discord.Forbidden(_Resp(403), "Missing Access")
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    await bot.handle_member_join(FakeMember(FakeGuild(G2), user_id=USER_ID + 1))
    assert [gid for gid, _ in bot.notices] == [G2]
    assert len(bot.chans[CH1].sent) == 1


# --- the friend's continuity after the import ---


@pytest.fixture
def imported(v22_db):
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    report = ensure_imported(v22_db, cfg, lambda: datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    return str(v22_db), report


async def test_upgrade_day_the_friend_gets_no_second_quote_but_tomorrow_gets_one(
    imported, monkeypatch
):
    path, report = imported
    source = Path(path).parent / "friend.txt"
    source.write_text("one\n%\ntwo\n", encoding="utf-8")
    with closing(connect(path)) as conn:
        row = repo.get_lounge(conn, report.guild_id)
        assert row.last_quote_date is not None  # v2.2 had already posted today
        posted_day = date.fromisoformat(row.last_quote_date)
        repo.upsert_lounge(
            conn,
            replace(
                row, quote_enabled=True, quote_sources=[{"kind": "file", "value": str(source)}]
            ),
        )
    chan = _channel_in(report.guild_id)
    bot = _bot(path, [], channels={row.channel_id: chan})
    await _with_http(bot)
    try:
        monkeypatch.setattr(client_module, "local_run_date", lambda now, tz: posted_day)
        assert (await bot.run_guild_quote(report.guild_id, False)).status == "already_posted"
        assert chan.sent == []
        tomorrow = posted_day + timedelta(days=1)
        monkeypatch.setattr(client_module, "local_run_date", lambda now, tz: tomorrow)
        assert (await bot.run_guild_quote(report.guild_id, False)).status == "posted"
        assert len(chan.sent) == 1
    finally:
        await bot.http_client.aclose()


async def test_the_friends_job_is_8am_local_on_both_sides_of_both_dst_changes(imported):
    path, report = imported
    bot = _bot(path, [])
    bot.scheduler = AsyncIOScheduler()
    assert report.guild_id in await bot.schedule_lounge_quotes()
    job = bot.scheduler.get_job(f"daily-quote-{report.guild_id}")
    zone = ZoneInfo(report.timezone)
    offsets = set()
    # Day before and day after the 2027 US changes (Mar 14 and Nov 7), and a plain winter day.
    for start in (
        datetime(2027, 3, 13, 18, 0, tzinfo=UTC),
        datetime(2027, 3, 14, 18, 0, tzinfo=UTC),
        datetime(2027, 11, 6, 18, 0, tzinfo=UTC),
        datetime(2027, 11, 7, 18, 0, tzinfo=UTC),
        datetime(2027, 1, 10, 18, 0, tzinfo=UTC),
    ):
        fire = job.trigger.get_next_fire_time(None, start).astimezone(zone)
        assert (fire.hour, fire.minute) == (8, 0)
        offsets.add(fire.utcoffset())
    assert len(offsets) == 2  # PST and PDT both showed up; the wall-clock time never moved


async def test_reload_lounges_moves_a_job_whose_quote_time_changed(db_path):
    _seed(db_path, _row(G1, CH1, t="08:00"))
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    bot.scheduler.start(paused=True)
    await bot.schedule_lounge_quotes()
    with closing(connect(db_path)) as conn:
        repo.upsert_lounge(conn, _row(G1, CH1, t="21:15"))

    def fields():
        trigger = bot.scheduler.get_job(f"daily-quote-{G1}").trigger
        return {f.name: str(f) for f in trigger.fields}

    assert (fields()["hour"], fields()["minute"]) == ("8", "0")  # nothing has re-read the row yet
    await bot.reload_lounges()
    assert (fields()["hour"], fields()["minute"]) == ("21", "15")
    assert len(bot.scheduler.get_jobs()) == 1
    bot.scheduler.shutdown(wait=False)


async def test_reload_lounges_moves_a_job_whose_zone_changed(db_path):
    _seed(db_path, _row(G1, CH1), zones={G1: "UTC"})
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    bot.scheduler.start(paused=True)
    try:
        await bot.schedule_lounge_quotes()
        with closing(connect(db_path)) as conn:
            conn.execute("UPDATE guilds SET timezone = 'Asia/Tokyo' WHERE guild_id = ?", (G1,))
            conn.commit()
        await bot.reload_lounges()
        assert bot.scheduler.get_job(f"daily-quote-{G1}").trigger.timezone == ZoneInfo("Asia/Tokyo")
    finally:
        bot.scheduler.shutdown(wait=False)


async def test_reload_lounges_drops_jobs_for_removed_and_disabled_guilds_only(db_path):
    _seed(db_path, _row(G1, CH1), _row(G2, CH2), _row(G3, CH1))
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    bot.scheduler.start(paused=True)
    try:
        await bot.schedule_lounge_quotes()
        bot.scheduler.add_job(lambda: None, "interval", hours=1, id="daily-quote")  # v2.2's own
        with closing(connect(db_path)) as conn:
            repo.upsert_lounge(conn, _row(G1, CH1, quote=False))
            repo.delete_guild(conn, G2)
        await bot.reload_lounges()
        await bot.reload_lounges()
        assert sorted(j.id for j in bot.scheduler.get_jobs()) == sorted(
            ["daily-quote", f"daily-quote-{G3}"]
        )
    finally:
        bot.scheduler.shutdown(wait=False)


async def test_reload_lounges_with_no_scheduler_just_loads_the_rows(db_path):
    _seed(db_path, _row(G1, CH1))
    bot = _bot(db_path, [])
    bot.scheduler = None
    assert [row.guild_id for row in await bot.reload_lounges()] == [G1]


@pytest.mark.parametrize("started_first", [True, False])
async def test_scheduling_twice_keeps_one_job_per_guild_and_raises_nothing(db_path, started_first):
    _seed(db_path, _row(G1, CH1), _row(G2, CH2))
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    if started_first:
        bot.scheduler.start(paused=True)
    assert await bot.schedule_lounge_quotes() == [G1, G2]
    assert await bot.schedule_lounge_quotes() == [G1, G2]
    if not started_first:
        # Before start() the jobs sit in a pending list, which `get_jobs` shows unmerged;
        # starting applies replace_existing, and that is when the duplicates must vanish.
        bot.scheduler.start(paused=True)
    try:
        assert sorted(j.id for j in bot.scheduler.get_jobs()) == [
            f"daily-quote-{G1}",
            f"daily-quote-{G2}",
        ]
    finally:
        bot.scheduler.shutdown(wait=False)


def test_the_table_refuses_a_quote_time_that_is_not_a_time(db_path):
    """So schedule_guild_quote's ValueError path is a second wall, not the first."""
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, G1)
        for bad in ("29:00", "24:00", "8:00", "12:60", ""):
            with pytest.raises(sqlite3.IntegrityError):
                repo.upsert_lounge(conn, _row(G1, CH1, t=bad))
        assert repo.get_lounge(conn, G1) is None


async def test_a_job_whose_quote_was_turned_off_removes_itself_and_stays_gone_until_reloaded(
    db_path,
):
    _seed(db_path, _row(G1, CH1))
    bot = _bot(db_path, [])
    await _with_http(bot)
    bot.scheduler = AsyncIOScheduler()
    bot.scheduler.start(paused=True)
    try:
        await bot.schedule_lounge_quotes()
        with closing(connect(db_path)) as conn:
            repo.upsert_lounge(conn, _row(G1, CH1, quote=False))
        await bot._guild_quote_job(G1)
        assert bot.scheduler.get_job(f"daily-quote-{G1}") is None
        with closing(connect(db_path)) as conn:
            repo.upsert_lounge(conn, _row(G1, CH1, quote=True))
        # Turning the quote back on doesn't bring the job back by itself; the reload does.
        assert bot.scheduler.get_job(f"daily-quote-{G1}") is None
        await bot.reload_lounges()
        assert bot.scheduler.get_job(f"daily-quote-{G1}") is not None
    finally:
        bot.scheduler.shutdown(wait=False)
        await bot.http_client.aclose()


async def test_a_job_with_no_scheduler_attached_does_not_crash_on_a_removed_guild(db_path):
    bot = _bot(db_path, [])
    bot.scheduler = None
    await bot._guild_quote_job(G1)  # no row at all; must not raise
    assert bot.notices == [] and bot.owner_alerts == []


async def test_a_guild_removed_while_its_source_loads_posts_nothing_and_drops_its_job(
    db_path, monkeypatch
):
    _seed(db_path, _row(G1, CH1, src=[{"kind": "wikiquote", "value": "Oscar Wilde"}]))
    bot = _bot(db_path, [])
    await _with_http(bot)
    bot.scheduler = AsyncIOScheduler()
    bot.scheduler.start(paused=True)
    await bot.schedule_lounge_quotes()

    async def load_then_leave(source, **kwargs):
        with closing(connect(db_path)) as conn:
            repo.delete_guild(conn, G1)
        return LoadedSource([Quote("too late")], "fresh")

    monkeypatch.setattr(daily, "load_source", load_then_leave)
    try:
        await bot._guild_quote_job(G1)
        assert bot.chans[CH1].sent == []
        assert bot.notices == [] and bot.owner_alerts == []
        assert bot.scheduler.get_job(f"daily-quote-{G1}") is None
    finally:
        bot.scheduler.shutdown(wait=False)
        await bot.http_client.aclose()


async def test_the_job_and_quote_now_racing_for_one_guild_post_exactly_once(db_path, monkeypatch):
    _seed(db_path, _row(G1, CH1, src=[{"kind": "wikiquote", "value": "Oscar Wilde"}]))
    bot = _bot(db_path, [])
    await _with_http(bot)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_load(source, **kwargs):
        started.set()
        await release.wait()
        return LoadedSource([Quote("only one of me")], "fresh")

    monkeypatch.setattr(daily, "load_source", slow_load)
    group = make_lounge_group(load_config(CONFIG_PATH), bot)
    interaction = FakeInteraction(guild_id=G1)
    try:
        job = asyncio.create_task(bot.run_guild_quote(G1, False))
        await started.wait()
        # quote-now checks "already posted today?" before the job has claimed anything, so it
        # asks for no force; the lock makes it wait, and then the guard turns it away.
        now_cmd = asyncio.create_task(command(group, "quote-now").callback(interaction))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(asyncio.gather(job, now_cmd), timeout=5)
        assert job.result().status == "posted"
        assert len(bot.chans[CH1].sent) == 1
        assert "already posted" in interaction.text
    finally:
        await bot.http_client.aclose()


async def test_the_quote_lock_is_released_when_a_run_crashes(db_path, monkeypatch):
    _seed(db_path, _row(G1, CH1))
    bot = _bot(db_path, [])
    await _with_http(bot)
    calls = []

    async def flaky(deps, *, force=False):
        calls.append(force)
        if len(calls) == 1:
            raise RuntimeError("first run falls over")
        return QuoteOutcome("already_posted")

    monkeypatch.setattr(client_module, "run_daily_quote", flaky)
    try:
        with pytest.raises(RuntimeError):
            await bot.run_guild_quote(G1, False)
        out = await asyncio.wait_for(bot.run_guild_quote(G1, False), timeout=2)
        assert out.status == "already_posted"
    finally:
        await bot.http_client.aclose()


# --- welcome ---


async def test_events_in_a_non_lounge_guild_log_nothing_at_all(db_path, caplog):
    bot = _bot(db_path, [_row(G1, CH1)])
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    stranger = FakeGuild(G3, "Somebody Else's Bar")
    await bot.handle_member_join(FakeMember(stranger))
    await bot.handle_member_update(FakeMember(stranger, pending=True), FakeMember(stranger))
    assert caplog.records == []
    assert USER_NAME not in caplog.text and str(USER_ID) not in caplog.text


async def test_a_guild_removed_from_the_rows_stops_getting_welcomes_after_a_reload(db_path):
    _seed(db_path, _row(G1, CH1))
    bot = _bot(db_path, [])
    await bot.reload_lounges()
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    assert len(bot.chans[CH1].sent) == 1
    with closing(connect(db_path)) as conn:
        repo.delete_guild(conn, G1)
    await bot.reload_lounges()
    await bot.handle_member_join(FakeMember(FakeGuild(G1), user_id=USER_ID + 1))
    assert len(bot.chans[CH1].sent) == 1


async def test_pending_then_accepted_is_tracked_per_guild(db_path):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    g1, g2 = FakeGuild(G1), FakeGuild(G2)
    await bot.handle_member_join(FakeMember(g1))  # welcomed in G1 right away
    await bot.handle_member_join(FakeMember(g2, pending=True))  # waits in G2
    assert len(bot.chans[CH1].sent) == 1 and bot.chans[CH2].sent == []
    await bot.handle_member_update(FakeMember(g2, pending=True), FakeMember(g2))
    assert len(bot.chans[CH2].sent) == 1  # G1's welcome did not use up G2's
    await bot.handle_member_update(FakeMember(g2, pending=True), FakeMember(g2))
    assert len(bot.chans[CH2].sent) == 1  # but G2's own repeat is still caught


async def test_a_join_and_an_acceptance_in_the_same_tick_welcome_once(db_path):
    bot = _bot(db_path, [_row(G1, CH1)])
    guild = FakeGuild(G1)
    await asyncio.gather(
        bot.handle_member_join(FakeMember(guild)),
        bot.handle_member_update(FakeMember(guild, pending=True), FakeMember(guild)),
    )
    assert len(bot.chans[CH1].sent) == 1


async def test_bots_are_ignored_in_every_lounge_guild_and_leave_no_record(db_path):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    for gid in (G1, G2):
        await bot.handle_member_join(FakeMember(FakeGuild(gid), bot=True))
    assert bot.chans[CH1].sent == [] and bot.chans[CH2].sent == []
    assert bot._guild_welcomes._seen == {}


async def test_a_hostile_display_name_never_reaches_the_text_the_notice_or_the_logs(
    db_path, caplog
):
    bot = _bot(db_path, [_row(G1, CH1, message="Hi {member} {name} {member.display_name}")])
    caplog.set_level(logging.DEBUG)
    hostile = "**@everyone** [click](http://evil.example) <@&123456789012345678>"
    member = FakeMember(FakeGuild(G1))
    member.name = member.display_name = hostile
    await bot.handle_member_join(member)
    ((text, mentions),) = bot.chans[CH1].sent
    assert hostile not in text and "evil.example" not in text
    assert mentions.everyone is False and mentions.roles is False and mentions.users == [member]
    bot.chans[CH1].next_error = RuntimeError(hostile)
    other = FakeMember(FakeGuild(G1), user_id=USER_ID + 5)
    other.name = other.display_name = hostile
    await bot.handle_member_join(other)
    assert "evil.example" not in bot.notices[0][1]
    assert "evil.example" not in caplog.text


async def test_a_hostile_server_name_is_posted_but_cannot_ping_anyone(db_path):
    # `{server}` goes in unescaped on purpose (whoever renames the server already has Manage
    # Server); what stops a ping is the allowed_mentions on the send.
    bot = _bot(db_path, [_row(G1, CH1, message="Welcome to {server}, {member}")])
    member = FakeMember(FakeGuild(G1, "@everyone <@&123456789012345678>"))
    await bot.handle_member_join(member)
    ((text, mentions),) = bot.chans[CH1].sent
    assert "@everyone" in text
    assert mentions.everyone is False and mentions.roles is False
    assert mentions.users == [member]


async def test_a_missing_welcome_channel_notifies_only_that_guild_and_names_nobody(db_path, caplog):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, 555000000000000099)])
    caplog.set_level(logging.DEBUG)

    async def fetch_channel(channel_id):
        raise discord.NotFound(_Resp(404), f"Unknown Channel for {USER_NAME}")

    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]
    await bot.handle_member_join(FakeMember(FakeGuild(G2)))
    assert [gid for gid, _ in bot.notices] == [G2]
    assert "NotFound" in bot.notices[0][1]
    assert USER_NAME not in bot.notices[0][1] + caplog.text
    assert str(USER_ID) not in bot.notices[0][1] + caplog.text


async def test_a_lounge_with_welcomes_off_still_schedules_its_quote(tmp_path, db_path):
    _seed(db_path, _row(G1, CH1, welcome=False, src=[_files(tmp_path, "a.txt", ["alpha"])]))
    bot = _bot(db_path, [])
    bot.scheduler = AsyncIOScheduler()
    assert await bot.schedule_lounge_quotes() == [G1]
    await bot.handle_member_join(FakeMember(FakeGuild(G1)))
    assert bot.chans[CH1].sent == []  # the welcome is off, the quote is not


def test_the_no_rewelcome_map_stays_bounded_with_guild_user_keys():
    recent = RecentWelcomes()
    t0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    for user in range(2000):
        for gid in (G1, G2):
            assert recent.check_and_record((gid, user), t0) is True
    assert len(recent._seen) == 4000
    later = t0 + timedelta(hours=24, seconds=1)
    assert recent.check_and_record((G1, 1), later) is True
    assert len(recent._seen) == 1  # a day on, the whole crowd has been forgotten


async def test_a_chunk_that_never_returns_does_not_stall_the_rest(db_path, monkeypatch):
    monkeypatch.setattr(client_module, "_LOUNGE_CHUNK_TIMEOUT_S", 0.05)
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    guilds = {G1: FakeGuild(G1), G2: FakeGuild(G2)}

    async def hang() -> None:
        await asyncio.Event().wait()

    guilds[G1].chunk = hang  # type: ignore[method-assign]
    bot.get_guild = guilds.get  # type: ignore[method-assign]
    await asyncio.wait_for(bot.chunk_lounge_guilds(), timeout=0.5)
    assert guilds[G2].chunks == 1


async def test_chunking_skips_a_lounge_guild_the_bot_is_not_in(db_path):
    bot = _bot(db_path, [_row(G1, CH1), _row(G2, CH2)])
    guilds = {G2: FakeGuild(G2)}
    bot.get_guild = guilds.get  # type: ignore[method-assign]
    await bot.chunk_lounge_guilds()
    assert guilds[G2].chunks == 1


# --- intents ---


@pytest.mark.parametrize(
    ("welcomes", "expected"),
    [
        ([], False),
        ([False], False),
        ([False, False, False], False),
        ([True], True),
        ([False, True], True),
        ([True, True, True], True),
    ],
)
def test_the_members_intent_follows_any_welcome_across_row_combinations(welcomes, expected):
    rows = [_row(G1 + n, CH1, welcome=w, quote=not w) for n, w in enumerate(welcomes)]
    assert build_intents_for_lounges(rows).members is expected
    assert build_intents_for_lounges(iter(rows)).members is expected  # a one-shot iterator too


async def test_intents_are_fixed_at_construction_and_a_later_reload_does_not_change_them(db_path):
    """Pins today's behavior: `lounges=[]` means no Server Members intent, for good.

    Constructing the bot before the rows are read (or from an empty list, as most tests do)
    and reloading afterwards leaves `members` off, so welcomes the rows ask for would never
    arrive. Task 13 has to build the intents from the rows it just imported or re-synced.
    """
    _seed(db_path, _row(G1, CH1, welcome=True))
    bot = _bot(db_path, [])
    await bot.reload_lounges()
    assert bot._lounges[G1].welcome_enabled is True
    assert bot.intents.members is False


async def test_lounge_mode_turns_off_startup_chunking_even_with_welcomes_on(db_path):
    bot = NewsBot(load_config(CONFIG_PATH), _secrets(), db_path, lounges=[_row(G1, CH1)])
    assert bot._connection._chunk_guilds is False
    assert bot.intents.members is True
