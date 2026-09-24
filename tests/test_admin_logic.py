"""Tests for the pure logic behind /newsbot: permission check, confirm-flow decision, spend.

Per plan section 5: no gateway mocking. `has_admin_permission` is tested
against a real `discord.Permissions` object (plain data, no bot token
needed), `needs_confirmation` against `DigestRow`, and `estimate_spend_usd`
as arithmetic.
"""

from __future__ import annotations

from datetime import date

import discord

from newsbot.bot.commands import estimate_spend_usd, has_admin_permission, needs_confirmation
from newsbot.pipeline.summarize import PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK
from newsbot.store.models import DigestRow


def _digest_row(status: str) -> DigestRow:
    return DigestRow(
        id=1, run_date=date(2026, 9, 23), status=status, posted_message_ids=[], error_notes=None
    )


# --- has_admin_permission ---


def test_has_admin_permission_true_when_flag_set():
    perms = discord.Permissions(manage_guild=True)
    assert has_admin_permission(perms, "manage_guild") is True


def test_has_admin_permission_false_when_flag_unset():
    perms = discord.Permissions.none()
    assert has_admin_permission(perms, "manage_guild") is False


def test_has_admin_permission_false_for_unrelated_flag():
    # A user with, say, manage_messages but not manage_guild shouldn't
    # slip through a permission check configured for manage_guild.
    perms = discord.Permissions(manage_messages=True)
    assert has_admin_permission(perms, "manage_guild") is False


def test_has_admin_permission_unknown_flag_name_is_false_not_a_crash():
    # config.py already validates admin_permission at load time, but this
    # function shouldn't blow up if it somehow got a bad one anyway.
    perms = discord.Permissions(manage_guild=True)
    assert has_admin_permission(perms, "not_a_real_permission") is False


# --- needs_confirmation ---


def test_needs_confirmation_no_row_is_false():
    assert needs_confirmation(None) is False


def test_needs_confirmation_ok_row_is_true():
    assert needs_confirmation(_digest_row("ok")) is True


def test_needs_confirmation_partial_row_is_true():
    assert needs_confirmation(_digest_row("partial")) is True


def test_needs_confirmation_failed_row_is_false():
    assert needs_confirmation(_digest_row("failed")) is False


def test_needs_confirmation_pending_row_is_false():
    # run-now's caller handles a pending row before ever asking this
    # question (see should_catch_up's docstring for why it's ambiguous).
    assert needs_confirmation(_digest_row("pending")) is False


# --- estimate_spend_usd ---


def test_estimate_spend_usd_zero_tokens_is_zero():
    assert estimate_spend_usd(0, 0) == 0.0


def test_estimate_spend_usd_matches_price_constants():
    spend = estimate_spend_usd(1_000_000, 1_000_000)
    assert spend == PRICE_IN_PER_MTOK + PRICE_OUT_PER_MTOK


def test_estimate_spend_usd_scales_linearly():
    assert estimate_spend_usd(500_000, 0) == PRICE_IN_PER_MTOK / 2
    assert estimate_spend_usd(0, 250_000) == PRICE_OUT_PER_MTOK / 4
