"""Owner alerts for joins and removals (v3.1.1): the bot tells the owner when the guest list moves.

Same hand-written fakes as `test_lifecycle.py`, same real temp database. The
alert path is a plain list the lifecycle appends to, so what's checked here is
the text and the rules (who gets told, who doesn't, what a rude server name
can do), not Discord's delivery. A server's name is the one untrusted thing
in every message, so most of the suspicion is aimed at it.
"""

from __future__ import annotations

import asyncio
import logging
import re

import pytest
from test_lifecycle import (
    G1,
    G2,
    G3,
    FakeChannel,
    FakeClient,
    _guild_ids,
    _seed,
    make_guild,
)
from v3_fakes import load_v3_config

from newsbot.bot.client import GuildLifecycle
from newsbot.guilds import lifecycle
from newsbot.store import repo


@pytest.fixture(autouse=True)
def _no_valve_pause(monkeypatch):
    monkeypatch.setattr(lifecycle, "VALVE_CHECK_PAUSE", 0)


@pytest.fixture
def cfg(monkeypatch):
    return load_v3_config(monkeypatch)


@pytest.fixture
def alerts():
    return []


@pytest.fixture
def life(v3_db, cfg, alerts):
    async def alert(text):
        alerts.append(text)

    return GuildLifecycle(v3_db, cfg, alert)


def named(guild_id=G1, name="Tavern", members=12, **kw):
    guild = make_guild(guild_id, **kw)
    guild.name = name
    guild.member_count = members
    return guild


def comped_life(v3_db, cfg, alerts, *ids):
    async def alert(text):
        alerts.append(text)

    return GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": list(ids)}), alert)


def client_with(*guilds):
    client = FakeClient()
    client.guilds = list(guilds)
    return client


# --- join ---


async def test_a_new_join_alerts_once_with_the_count(life, alerts):
    await life.on_guild_join(named(G1, "Tavern", 12))
    assert alerts == [f"Joined a new server: Tavern ({G1}), 12 members. Now in 1 server."]


async def test_the_count_is_the_rows_after_the_insert(life, alerts, v3_db):
    _seed(v3_db, G2, G3)
    await life.on_guild_join(named(G1))
    assert alerts == [f"Joined a new server: Tavern ({G1}), 12 members. Now in 3 servers."]


async def test_a_comped_guild_says_so(v3_db, cfg, alerts):
    life = comped_life(v3_db, cfg, alerts, G1)
    await life.on_guild_join(named(G1))
    assert alerts == [f"Joined a new server: Tavern ({G1}), 12 members. Now in 1 server; comped."]


async def test_a_not_comped_guild_does_not(v3_db, cfg, alerts):
    life = comped_life(v3_db, cfg, alerts, G2)
    await life.on_guild_join(named(G1))
    assert "comped" not in alerts[0]


async def test_no_member_count_leaves_the_members_clause_out(life, alerts):
    await life.on_guild_join(named(G1, members=None))
    assert alerts == [f"Joined a new server: Tavern ({G1}). Now in 1 server."]


async def test_one_member_is_singular(life, alerts):
    await life.on_guild_join(named(G1, members=1))
    assert alerts == [f"Joined a new server: Tavern ({G1}), 1 member. Now in 1 server."]


async def test_a_guild_with_no_name_still_gets_an_alert(life, alerts):
    await life.on_guild_join(make_guild(G1))
    assert alerts == [f"Joined a new server: (unnamed) ({G1}). Now in 1 server."]


async def test_a_duplicate_join_is_silent(life, alerts):
    guild = named()
    await life.on_guild_join(guild)
    await life.on_guild_join(guild)
    assert len(alerts) == 1


async def test_a_failed_row_write_is_silent(life, alerts, monkeypatch):
    def boom(guild_id):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(life, "_create_row", boom)
    assert await life.on_guild_join(named()) is False
    assert alerts == []


async def test_a_failing_alert_keeps_the_row_and_the_hello(v3_db, cfg, caplog):
    async def alert(text):
        raise RuntimeError("channel gone")

    ch = FakeChannel(1)
    life = GuildLifecycle(v3_db, cfg, alert)
    assert await life.on_guild_join(named(channels=[ch])) is True
    assert _guild_ids(v3_db) == [G1]
    assert len(ch.sent) == 1
    assert "owner alert failed" in caplog.text


async def test_a_hung_alert_times_out_without_breaking_the_join(v3_db, cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)

    async def alert(text):
        await asyncio.sleep(30)

    ch = FakeChannel(1)
    life = GuildLifecycle(v3_db, cfg, alert)
    assert await life.on_guild_join(named(channels=[ch])) is True
    assert _guild_ids(v3_db) == [G1]
    assert len(ch.sent) == 1


async def test_a_hung_hello_does_not_cost_the_alert(life, alerts, monkeypatch):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)

    class HungChannel(FakeChannel):
        async def send(self, content=None, **kwargs):
            await asyncio.sleep(30)

    await life.on_guild_join(named(channels=[HungChannel(1)]))
    assert len(alerts) == 1


# --- a hostile name ---


HOSTILE = (
    "@everyone @here <@&1> <@&123456789012345678> <@123456789012345678> <#456> </shift:789> "
    "https://evil.example/x "
    "discord.gg/abc **bold** __under__ `code` [click](https://evil.example)\n"
    "line two\r\n\ttabbed \u202e" + "x" * 300
)


def assert_defused(text: str) -> None:
    assert "@everyone" not in text and "@here" not in text
    # "<@&1>" is too short to be a real mention and Discord shows it as text; the
    # eighteen-digit ones are the live kind, and they must not survive intact.
    assert not re.search(r"<@[!&]?\d{17,20}>", text)
    assert "<#" not in text and "</" not in text
    assert "://" not in text
    assert "discord.gg/" not in text
    assert "\n" not in text and "\r" not in text and "\t" not in text
    assert "\u202e" not in text
    assert "**bold**" not in text and "__under__" not in text and "`code`" not in text


async def test_a_hostile_name_is_defused_and_capped_on_join(life, alerts):
    await life.on_guild_join(named(G1, HOSTILE))
    text = alerts[0]
    assert_defused(text)
    assert "x" * 200 not in text
    assert text.endswith(f"({G1}), 12 members. Now in 1 server.")


async def test_a_hostile_name_is_defused_on_removal(life, alerts, v3_db):
    _seed(v3_db, G1)
    await life.on_guild_remove(named(G1, HOSTILE))
    assert_defused(alerts[0])
    assert "x" * 200 not in alerts[0]


def test_the_label_caps_the_name_at_a_hundred_characters():
    label = lifecycle.guild_label(named(G1, "y" * 300))
    assert label == "y" * 99 + "…" + f" ({G1})"


def test_the_label_collapses_whitespace():
    assert lifecycle.guild_label(named(G1, "  a \n\n b\t\tc  ")) == f"a b c ({G1})"


def test_the_label_marks_comped():
    assert lifecycle.guild_label(named(G1, "Tavern"), comped=True) == f"Tavern ({G1}, comped)"


# --- removal ---


async def test_a_removal_alerts_with_the_remaining_count(life, alerts, v3_db):
    _seed(v3_db, G1, G2)
    await life.on_guild_remove(named(G1, "Tavern"))
    assert alerts == [f"Removed from Tavern ({G1}). Its settings were deleted. Now in 1 server."]


async def test_a_removal_leaving_nothing_says_zero_servers(life, alerts, v3_db):
    _seed(v3_db, G1)
    await life.on_guild_remove(named(G1, "Tavern"))
    assert alerts[0].endswith("Now in 0 servers.")


async def test_a_failed_removal_is_silent(life, alerts, v3_db, monkeypatch):
    _seed(v3_db, G1)

    def boom(conn, guild_id):
        raise RuntimeError("locked")

    monkeypatch.setattr(repo, "delete_guild", boom)
    await life.on_guild_remove(named(G1))
    assert alerts == []
    assert _guild_ids(v3_db) == [G1]


async def test_removing_a_guild_with_no_row_is_silent(life, alerts):
    await life.on_guild_remove(named(G1))
    assert alerts == []


async def test_a_failing_alert_does_not_undo_the_removal(v3_db, cfg):
    async def alert(text):
        raise RuntimeError("nope")

    _seed(v3_db, G1)
    await GuildLifecycle(v3_db, cfg, alert).on_guild_remove(named(G1))
    assert _guild_ids(v3_db) == []


# --- startup reconciliation ---


async def test_reconcile_sends_one_summary_for_the_offline_joins(v3_db, cfg, alerts):
    life = comped_life(v3_db, cfg, alerts, G2)
    plan = await life.reconcile(client_with(named(G1, "One"), named(G2, "Two")))
    assert plan.to_create == (G1, G2)
    assert alerts == [
        f"Joined while I was offline: One ({G1}), Two ({G2}, comped). Now in 2 servers."
    ]


async def test_reconcile_with_one_offline_join_is_singular(life, alerts):
    await life.reconcile(client_with(named(G1, "One")))
    assert alerts == [f"Joined while I was offline: One ({G1}). Now in 1 server."]


async def test_reconcile_caps_the_list_at_ten_names(life, alerts):
    ids = list(range(5000, 5013))
    await life.reconcile(client_with(*(named(i, f"S{i}") for i in ids)))
    assert len(alerts) == 1
    text = alerts[0]
    assert all(f"S{i} ({i})" in text for i in ids[:10])
    assert not any(f"S{i} ({i})" in text for i in ids[10:])
    assert text.endswith(", and 3 more. Now in 13 servers.")


async def test_reconcile_ten_names_exactly_has_no_more_clause(life, alerts):
    ids = list(range(5000, 5010))
    await life.reconcile(client_with(*(named(i, f"S{i}") for i in ids)))
    assert "more" not in alerts[0]


async def test_reconcile_joins_make_no_per_server_alert(life, alerts):
    await life.reconcile(client_with(named(G1), named(G2), named(G3)))
    assert len(alerts) == 1
    assert alerts[0].startswith("Joined while I was offline")


async def test_reconcile_sends_one_summary_for_the_offline_removals(life, alerts, v3_db):
    _seed(v3_db, G1, G2, G3)
    await life.reconcile(client_with(named(G3)))
    assert alerts == [f"Removed while I was offline: {G1}, {G2}. Now in 1 server."]
    assert _guild_ids(v3_db) == [G3]


async def test_reconcile_with_one_removal_and_one_join_sends_two_alerts(life, alerts, v3_db):
    _seed(v3_db, G1, G2)
    await life.reconcile(client_with(named(G2), named(G3, "New")))
    assert alerts == [
        f"Joined while I was offline: New ({G3}). Now in 2 servers.",
        f"Removed while I was offline: {G1}. Now in 2 servers.",
    ]


async def test_reconcile_with_nothing_to_do_is_silent(life, alerts, v3_db):
    _seed(v3_db, G1)
    await life.reconcile(client_with(named(G1)))
    assert alerts == []


async def test_a_tripped_valve_keeps_its_own_message_and_no_removal_summary(v3_db, cfg, alerts):
    # Empty cache with rows in the database: the valve trips, nothing is
    # deleted, and the one alert is the valve's, not a second "removed" one.
    _seed(v3_db, G1, G2)
    life = comped_life(v3_db, cfg, alerts)
    plan = await life.reconcile(FakeClient([]))
    assert plan.valve_tripped
    assert len(alerts) == 1 and alerts[0].startswith("Startup cleanup paused")


async def test_self_healed_deletions_are_left_to_the_valve_message(v3_db, cfg, alerts):
    ids = list(range(1000, 1030))
    _seed(v3_db, *ids)
    life = comped_life(v3_db, cfg, alerts)
    client = FakeClient(ids[:20], gone=ids[20:])
    await life.reconcile(client)
    assert len(alerts) == 1 and alerts[0].startswith("Startup cleanup paused")
    assert "10 confirmed gone" in alerts[0]


async def test_a_failing_summary_does_not_fail_reconcile(v3_db, cfg):
    async def alert(text):
        raise RuntimeError("nope")

    ch = FakeChannel(1)
    plan = await GuildLifecycle(v3_db, cfg, alert).reconcile(client_with(named(G1, channels=[ch])))
    assert plan.to_create == (G1,)
    assert _guild_ids(v3_db) == [G1]
    assert len(ch.sent) == 1


async def test_hostile_names_are_defused_in_the_summary(life, alerts):
    await life.reconcile(client_with(named(G1, HOSTILE)))
    assert_defused(alerts[0])


# --- logs ---


async def test_the_name_never_reaches_a_log_record(life, v3_db, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Zebra Secret Club"  # noqa: S105
    await life.on_guild_join(named(G1, secret))
    await life.on_guild_remove(named(G1, secret))
    await life.reconcile(client_with(named(G2, secret)))
    assert secret not in caplog.text
    assert all(secret not in r.getMessage() for r in caplog.records)


async def test_the_name_stays_out_of_the_log_when_the_alert_fails(v3_db, cfg, caplog):
    secret = "Zebra Secret Club"  # noqa: S105

    async def alert(text):
        raise RuntimeError("channel gone")

    caplog.set_level(logging.DEBUG)
    life = GuildLifecycle(v3_db, cfg, alert)
    await life.on_guild_join(named(G1, secret))
    assert secret not in caplog.text


def test_a_visible_name_keeps_its_joiners_and_selectors():
    # Only an all-invisible name is "(unnamed)"; a family emoji keeps its glue.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467 Crew \u2764\ufe0f"
    assert lifecycle.guild_label(named(G1, family)) == f"{family} ({G1})"


def test_a_summary_never_cuts_its_tail():
    labels = [f"{'w' * 60} ({i})" for i in range(10)]
    text = lifecycle.summary_text(
        "Joined while I was offline: ", labels, " Now in 10 servers.", limit=300
    )
    assert text.endswith(", and 7 more. Now in 10 servers.")
    assert lifecycle.discord_len(text) <= 300
