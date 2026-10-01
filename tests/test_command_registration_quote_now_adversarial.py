"""Adversarial registration tests for `/lounge quote-now` (plan task 9, ported at the cutover).

The rule is small and easy to get subtly wrong: a server gets the command
exactly when it has a lounge row with the quote on, and nothing else in the row
gets a vote. Welcomes, the channel and the quote time all take a turn here as a
full grid rather than the four cases I'd have picked by hand. The other half is
Discord's own limits: a name of 32 characters and a description of 100, either
of which makes the whole sync fail instead of just the one command, which is a
lot of blast radius for one long sentence.

(This began as v2's "`/newsbot quote-now` is registered iff `daily_quote` is
enabled" grid, with config switches. The switches live in the `guild_lounge`
row now; the rule underneath is the same.)
"""

from __future__ import annotations

import itertools
from contextlib import closing

import pytest
from v3_fakes import GUILD_A, make_guild

from newsbot.bot.commands import make_lounge_group
from newsbot.bot.registration import lounge_guild_ids_sync, plan_scopes
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import LoungeSettings


class _FakeBot:
    db_path = ":memory:"


def _lounge(*, welcome: bool, quote: bool, quote_time: str = "08:00") -> LoungeSettings:
    return LoungeSettings(
        guild_id=GUILD_A,
        channel_id=555000000000000001,
        welcome_enabled=welcome,
        welcome_message="Hi {member}",
        quote_enabled=quote,
        quote_time=quote_time,
        quote_sources=[{"kind": "wikiquote", "value": "Oscar Wilde"}],
        last_quote_date=None,
    )


@pytest.mark.parametrize(
    ("welcome", "quote", "quote_time"),
    list(itertools.product([False, True], [False, True], ["00:00", "08:00", "23:59"])),
)
def test_quote_now_is_registered_iff_the_quote_is_on(v3_db, v3_cfg, welcome, quote, quote_time):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 11)])
    with closing(connect(v3_db)) as conn:
        repo.upsert_lounge(conn, _lounge(welcome=welcome, quote=quote, quote_time=quote_time))

    lounge_ids = lounge_guild_ids_sync(v3_db)

    assert (GUILD_A in lounge_ids) is quote
    assert (GUILD_A in plan_scopes(v3_cfg, lounge_ids).lounge_guilds) is quote


def test_a_server_with_no_lounge_row_never_gets_the_command(v3_db, v3_cfg):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 11)])

    assert lounge_guild_ids_sync(v3_db) == []
    assert plan_scopes(v3_cfg, []).lounge_guilds == ()


def test_every_lounge_command_fits_discords_name_and_description_limits(v3_cfg):
    commands = make_lounge_group(v3_cfg, _FakeBot()).commands

    assert "quote-now" in {c.name for c in commands}
    for command in commands:
        assert len(command.name) <= 32, command.name
        assert 1 <= len(command.description) <= 100, command.name


def test_quote_now_takes_no_options(v3_cfg):
    # An option would be a way for an admin to type a quote into the lounge.
    # The design has none; this keeps it that way.
    command = next(
        c for c in make_lounge_group(v3_cfg, _FakeBot()).commands if c.name == "quote-now"
    )

    assert command.parameters == []
