"""Adversarial registration tests for `/newsbot quote-now` (plan task 9).

The rule is small and easy to get subtly wrong: the command exists exactly
when `lounge.daily_quote.enabled` is true, and nothing else in the config gets
a vote. Welcomes, alerts, the dev-only test command and a missing channel id
all take a turn here, as a full grid rather than the four cases I'd have
picked by hand. The other half is Discord's own limits: a name of 32 characters
and a description of 100, either of which makes the whole sync fail instead of
just the one command, which is a lot of blast radius for one long sentence.
"""

from __future__ import annotations

import itertools

import pytest

from newsbot.bot.commands import make_admin_group
from newsbot.config import DailyQuoteCfg, LoungeCfg, QuoteSourceCfg, WelcomeCfg, load_config

FIXTURE = "tests/fixtures/config_valid.yaml"


class _FakeBot:
    db_path = ":memory:"


def _cfg(*, welcome, quote, alerts, test_command, channel_id=555000000000000001):
    cfg = load_config(FIXTURE)
    alerts_cfg = cfg.alerts.model_copy(
        update={
            "enabled": alerts,
            "channel_id": 777000000000000001 if alerts else None,
            "allow_test_command": test_command,
        }
    )
    lounge = LoungeCfg(
        channel_id=channel_id,
        welcome=WelcomeCfg(enabled=welcome, message="Hi {member}"),
        daily_quote=DailyQuoteCfg(
            enabled=quote, sources=[QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")]
        ),
    )
    return cfg.model_copy(update={"alerts": alerts_cfg, "lounge": lounge})


@pytest.mark.parametrize(
    ("welcome", "quote", "alerts", "test_command"),
    list(itertools.product([False, True], repeat=4)),
)
def test_quote_now_is_registered_iff_daily_quote_is_enabled(welcome, quote, alerts, test_command):
    cfg = _cfg(welcome=welcome, quote=quote, alerts=alerts, test_command=test_command)

    names = {c.name for c in make_admin_group(cfg, _FakeBot()).commands}

    assert ("quote-now" in names) is quote


def test_quote_now_registration_does_not_need_a_channel_id():
    # Documented: the registration rule looks at daily_quote.enabled only. If
    # channel_id is None (config validation should stop that; model_copy skips
    # validation) the command still exists and its "Posted" reply degrades to
    # the generic one. See the handler tests.
    cfg = _cfg(welcome=False, quote=True, alerts=False, test_command=False, channel_id=None)

    names = {c.name for c in make_admin_group(cfg, _FakeBot()).commands}

    assert "quote-now" in names


def test_every_admin_command_fits_discords_name_and_description_limits():
    cfg = _cfg(welcome=True, quote=True, alerts=True, test_command=True)

    commands = make_admin_group(cfg, _FakeBot()).commands

    assert "quote-now" in {c.name for c in commands}
    for command in commands:
        assert len(command.name) <= 32, command.name
        assert 1 <= len(command.description) <= 100, command.name


def test_quote_now_takes_no_options():
    # An option would be a way for an admin to type a quote into the lounge.
    # The design has none; this keeps it that way.
    cfg = _cfg(welcome=False, quote=True, alerts=False, test_command=False)
    command = next(c for c in make_admin_group(cfg, _FakeBot()).commands if c.name == "quote-now")

    assert command.parameters == []
