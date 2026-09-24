"""Tests for the pure logic behind /newsbot: permission check, confirm-flow decision, spend.

Per plan section 5: no gateway mocking. `has_admin_permission` is tested
against a real `discord.Permissions` object (plain data, no bot token
needed), `needs_confirmation` against `DigestRow`, and `estimate_spend_usd`
as arithmetic.
"""

from __future__ import annotations

from datetime import date

import discord
import pytest

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


def test_has_admin_permission_none_permissions_is_false_not_a_crash():
    # `discord.Interaction.permissions` is documented to return an empty
    # Permissions object outside a guild context (DMs), never None -- but
    # `getattr(..., default=False)` means this function degrades cleanly
    # to "deny" even if a caller somehow hands it None anyway, instead of
    # raising and taking the whole command handler down with it.
    assert has_admin_permission(None, "manage_guild") is False


def test_has_admin_permission_empty_permissions_in_dm_context_is_false():
    # discord.py hands back Permissions.none() for an interaction that
    # didn't happen in a guild (a DM'd command, if one were ever
    # registered) -- there's no guild to be an admin of, so this should
    # deny cleanly rather than error.
    assert has_admin_permission(discord.Permissions.none(), "manage_guild") is False


def test_has_admin_permission_administrator_flag_alone_does_not_imply_other_flags():
    # discord.Permissions.administrator is just one more bit in the
    # bitmask here, not a bypass -- Permissions doesn't compute "implies
    # every other permission" the way Discord's own client-side display
    # does. In production, interaction.permissions is the fully resolved
    # value Discord's API already sends (which does reflect an
    # administrator's effective permissions), so this documents this
    # function's behavior against the raw object, not a claim about what
    # real admins see. Worth a second look if `/newsbot` ever denies an
    # actual server administrator (see CLAUDE.md open items).
    perms = discord.Permissions(administrator=True)
    assert has_admin_permission(perms, "manage_guild") is False


def test_has_admin_permission_configured_permission_name_variants():
    # admin_permission is a free-form string naming any real
    # discord.Permissions flag (config.py only checks it's a valid one,
    # not that it's manage_guild specifically) -- a few other plausible
    # choices should all work the same way.
    assert has_admin_permission(discord.Permissions(kick_members=True), "kick_members") is True
    assert has_admin_permission(discord.Permissions(ban_members=True), "kick_members") is False
    assert (
        has_admin_permission(discord.Permissions(manage_channels=True), "manage_channels") is True
    )


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


def test_estimate_spend_usd_large_token_counts():
    # A month's worth of real usage, not a single run -- nothing here
    # should overflow or behave differently just because the numbers got
    # bigger.
    spend = estimate_spend_usd(50_000_000, 10_000_000)
    expected = (
        50_000_000 * PRICE_IN_PER_MTOK / 1_000_000 + 10_000_000 * PRICE_OUT_PER_MTOK / 1_000_000
    )
    assert spend == pytest.approx(expected)


def test_estimate_spend_usd_small_counts_round_via_plain_float_arithmetic():
    # Token counts that don't divide evenly leave this as ordinary float
    # arithmetic (no decimal.Decimal rounding) -- pinning the actual
    # value here so a future switch to Decimal, if it ever happens,
    # doesn't happen by accident.
    assert estimate_spend_usd(1, 1) == pytest.approx(0.000_006, abs=1e-12)
