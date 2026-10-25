"""`/owner servers` (plan task 9, D3): home guild only, owner only, numbers only."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from v3_fakes import GUILD_A, GUILD_B, HOME, OWNER_ID, FakeInteraction, command, make_guild

from newsbot.bot import owner_commands as owner_commands_module
from newsbot.bot.format import local_month
from newsbot.bot.owner_commands import make_owner_group, render_owner_servers, summarize_digests
from newsbot.config import OwnerReportCfg
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import DueCandidate

DENIED = "You don't have permission to run this."


@pytest.fixture
def owner(v3_cfg, v3_db):
    asked = []

    async def is_owner(user):
        asked.append(user.id)
        return user.id == OWNER_ID

    bot = SimpleNamespace(db_path=v3_db, is_owner=is_owner)
    group = make_owner_group(v3_cfg, bot)
    return SimpleNamespace(call=command(group, "servers").callback, asked=asked, group=group)


def test_the_group_shape(owner):
    assert owner.group.name == "owner"
    assert owner.group.guild_only is True
    assert owner.group.default_permissions.administrator is True
    assert [c.name for c in owner.group.commands] == ["servers"]


async def test_the_owner_in_the_home_guild_gets_the_numbers(owner, v3_db):
    make_guild(v3_db, GUILD_A, games=[("rust", 1)])
    make_guild(v3_db, GUILD_B, tier="comped", set_up=False)
    with closing(connect(v3_db)) as conn, conn:
        conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "input_tokens, output_tokens, created_at) VALUES "
            "('rust', '2026-09-30', 'ok', 'a', 'b', 1000000, 0, ?)",
            (datetime.now(UTC).isoformat(),),
        )
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await owner.call(interaction)
    text = interaction.text
    assert "Servers: 2 (1 set up)" in text
    assert "Tiers: 1 free, 1 comped" in text
    assert "Today's digests:" in text
    assert "Summary spend, " in text and "about $" in text and "about $0.00" not in text
    assert interaction.sent[-1]["ephemeral"] is True
    assert interaction.sent[-1]["allowed_mentions"].everyone is False


async def test_a_non_owner_in_the_home_guild_is_refused(owner):
    interaction = FakeInteraction(guild_id=HOME, user_id=7)
    await owner.call(interaction)
    assert interaction.text == DENIED
    assert owner.asked == [7]


async def test_even_the_owner_is_refused_outside_the_home_guild(owner):
    interaction = FakeInteraction(guild_id=GUILD_A, user_id=OWNER_ID)
    await owner.call(interaction)
    assert interaction.text == DENIED
    assert owner.asked == []  # never even asked


async def test_no_guild_or_no_home_guild_configured(v3_cfg, v3_db):
    async def is_owner(user):
        return True

    cfg = v3_cfg.model_copy(update={"home_guild_id": None})
    group = make_owner_group(cfg, SimpleNamespace(db_path=v3_db, is_owner=is_owner))
    for guild_id in (None, HOME):
        interaction = FakeInteraction(guild_id=guild_id, user_id=OWNER_ID)
        await command(group, "servers").callback(interaction)
        assert interaction.text == DENIED


async def test_an_admin_is_not_the_owner(owner):
    interaction = FakeInteraction(guild_id=HOME, user_id=7)  # has Manage Server, isn't the owner
    await owner.call(interaction)
    assert interaction.text == DENIED


async def test_the_reply_names_no_server_or_channel(owner, v3_db):
    make_guild(v3_db, GUILD_A, games=[("rust", 4242)], admin_channel_id=9191)
    with closing(connect(v3_db)) as conn:
        repo.add_notice(conn, GUILD_A, "secret notice text")
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await owner.call(interaction)
    for needle in (str(GUILD_A), "4242", "9191", "secret", "Rust"):
        assert needle not in interaction.text


# --- the month is the owner's, same as the daily report's ---


def _freeze_now(monkeypatch, moment: datetime) -> None:
    """Pin `owner_commands.py`'s `datetime.now(UTC)`: the month it counts follows the clock."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(owner_commands_module, "datetime", _FrozenDatetime)


def _summary_row(db_path, created_at: datetime, input_tokens: int = 0, output_tokens: int = 0):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "input_tokens, output_tokens, created_at) VALUES "
            "('rust', '2026-10-31', 'ok', 'a', 'b', ?, ?, ?)",
            (input_tokens, output_tokens, created_at.isoformat()),
        )


async def _spend_line_at(v3_cfg, v3_db, monkeypatch, now: datetime, timezone: str) -> str:
    cfg = v3_cfg.model_copy(update={"owner_report": OwnerReportCfg(timezone=timezone)})

    async def is_owner(user):
        return True

    _freeze_now(monkeypatch, now)
    group = make_owner_group(cfg, SimpleNamespace(db_path=v3_db, is_owner=is_owner))
    interaction = FakeInteraction(guild_id=HOME, user_id=OWNER_ID)
    await command(group, "servers").callback(interaction)
    return interaction.text.split("\n")[-1]


async def test_the_month_is_the_reports_not_utcs_in_a_zone_behind_utc(v3_cfg, v3_db, monkeypatch):
    # 21:00 PDT on October 31st: UTC has already turned over to November.
    _summary_row(v3_db, datetime(2026, 10, 31, 20, 0, tzinfo=UTC), 0, 1_000_000)  # $0.50
    line = await _spend_line_at(
        v3_cfg, v3_db, monkeypatch, datetime(2026, 11, 1, 4, 0, tzinfo=UTC), "America/Los_Angeles"
    )
    assert line == "Summary spend, 2026-10: about $0.50"


async def test_the_month_is_the_reports_not_utcs_in_a_zone_ahead_of_utc(v3_cfg, v3_db, monkeypatch):
    # 01:00 JST on November 1st: UTC is still in October.
    _summary_row(v3_db, datetime(2026, 10, 31, 14, 59, tzinfo=UTC), 0, 2_000_000)  # October there
    _summary_row(v3_db, datetime(2026, 10, 31, 15, 0, tzinfo=UTC), 0, 1_000_000)  # $0.50
    line = await _spend_line_at(
        v3_cfg, v3_db, monkeypatch, datetime(2026, 10, 31, 16, 0, tzinfo=UTC), "Asia/Tokyo"
    )
    assert line == "Summary spend, 2026-11: about $0.50"


async def test_a_row_at_the_local_month_start_counts_and_one_just_before_does_not(
    v3_cfg, v3_db, monkeypatch
):
    # Pacific midnight on November 1st is 07:00 UTC (PDT ends that night, at 02:00).
    start = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    _summary_row(v3_db, start - timedelta(seconds=1), 0, 10_000_000)  # October there
    _summary_row(v3_db, start, 0, 1_000_000)  # $0.50
    line = await _spend_line_at(
        v3_cfg, v3_db, monkeypatch, datetime(2026, 11, 1, 12, 0, tzinfo=UTC), "America/Los_Angeles"
    )
    assert line == "Summary spend, 2026-11: about $0.50"


# --- the shared helper ---


@pytest.mark.parametrize("hour", [4, 5, 6], ids=["first pass", "repeated hour", "after it"])
def test_local_month_starts_at_the_first_midnight_when_a_zone_repeats_it(hour):
    # Havana ends daylight time at 01:00 on 2026-11-01, so local midnight on the 1st
    # happens twice (04:00 UTC, then 05:00 UTC). The first one is the month's start, even
    # when "now" falls in the repeated hour (fold=1).
    month = local_month(datetime(2026, 11, 1, hour, 30, tzinfo=UTC), "America/Havana")
    assert month.start_utc == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert month.start_utc.tzinfo is UTC
    assert (month.label, month.name) == ("2026-11", "November")


@pytest.mark.parametrize(
    ("zone", "now", "start", "label", "name"),
    [
        (
            "UTC",
            datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
            (2026, 10, 1, 0),
            "2026-10",
            "October",
        ),
        ("UTC", datetime(2026, 11, 1, 0, 0, tzinfo=UTC), (2026, 11, 1, 0), "2026-11", "November"),
        (
            "America/Los_Angeles",
            datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
            (2026, 10, 1, 7),
            "2026-10",
            "October",
        ),
        (
            "Asia/Tokyo",
            datetime(2026, 10, 31, 16, 0, tzinfo=UTC),
            (2026, 10, 31, 15),
            "2026-11",
            "November",
        ),
        (
            "Asia/Tokyo",
            datetime(2026, 10, 31, 14, 59, tzinfo=UTC),
            (2026, 9, 30, 15),
            "2026-10",
            "October",
        ),
    ],
)
def test_local_month_is_the_zones_month_as_a_utc_instant(zone, now, start, label, name):
    month = local_month(now, zone)
    assert month.start_utc == datetime(*start, tzinfo=UTC)
    assert (month.label, month.name) == (label, name)


# --- pure pieces ---

NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)  # 10:00 in Los Angeles


def cand(guild_id, *, run_date=None, status=None, tz="America/Los_Angeles", at="09:00"):
    return DueCandidate(guild_id, at, tz, "free", run_date, status)


def test_summarize_digests_buckets():
    today = date(2026, 9, 30)
    counts = summarize_digests(
        [
            cand(1, run_date=today, status="ok"),
            cand(2, run_date=today, status="ok"),
            cand(3, run_date=today, status="partial"),
            cand(4, run_date=today, status="failed"),
            cand(5, run_date=today, status="pending"),
            cand(6, run_date=date(2026, 9, 29), status="ok"),  # yesterday's: nothing today yet
            cand(7),  # never had one, 09:00 already passed
            cand(8, at="23:00"),  # not yet due
            cand(9, tz="Mars/Olympus"),
            cand(10, at="99:99"),
        ],
        NOW,
    )
    assert dict(counts) == {
        "ok": 2,
        "partial": 1,
        "failed": 1,
        "pending": 1,
        "due, none yet": 2,
        "not yet due": 1,
        "bad zone": 2,
    }


def test_summarize_digests_uses_each_servers_own_local_day():
    # 17:00 UTC is already tomorrow in Auckland: yesterday's row isn't today's there.
    row = date(2026, 9, 30)
    counts = summarize_digests([cand(1, run_date=row, status="ok", tz="Pacific/Auckland")], NOW)
    assert "ok" not in counts


def test_render_owner_servers_is_plain_lines():
    counts = {
        "servers": 3,
        "set_up": 2,
        "free": 2,
        "comped": 1,
        "shift_enabled": 1,
        "permission_problems": 0,
    }
    from collections import Counter

    text = render_owner_servers(counts, Counter(ok=2), 1.5, "2026-09")
    assert text.splitlines()[0] == "Servers: 3 (2 set up)"
    assert "2 ok, 0 partial" in text and "about $1.50" in text


# --- NewsBot.is_owner ---


def _bot(v3_cfg, v3_db, app_info):
    from pydantic import SecretStr

    from newsbot.bot.client import NewsBot
    from newsbot.config import Secrets

    secrets = Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("k"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    bot = NewsBot(v3_cfg, secrets, v3_db)
    calls = []

    async def application_info():
        calls.append(1)
        if isinstance(app_info, Exception):
            raise app_info
        return app_info

    bot.application_info = application_info
    return bot, calls


async def test_is_owner_uses_the_application_owner_and_caches_it(v3_cfg, v3_db):
    app = SimpleNamespace(team=None, owner=SimpleNamespace(id=OWNER_ID))
    bot, calls = _bot(v3_cfg, v3_db, app)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is True
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID + 1)) is False
    assert len(calls) == 1


async def test_is_owner_for_a_team_app_means_the_teams_owner(v3_cfg, v3_db):
    app = SimpleNamespace(team=SimpleNamespace(owner_id=OWNER_ID), owner=SimpleNamespace(id=1))
    bot, _ = _bot(v3_cfg, v3_db, app)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is True
    assert await bot.is_owner(SimpleNamespace(id=1)) is False


async def test_is_owner_says_no_when_discord_cant_be_asked(v3_cfg, v3_db):
    import discord

    error = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    bot, calls = _bot(v3_cfg, v3_db, error)
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is False
    assert await bot.is_owner(SimpleNamespace(id=OWNER_ID)) is False
    assert len(calls) == 2  # a failed lookup isn't cached
