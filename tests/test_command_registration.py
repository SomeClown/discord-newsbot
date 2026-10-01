"""Tests that the command tree is built the way SPEC-DEV says it should be.

Per plan section 5, this stays at the "cheap object-level checks" level:
building `app_commands.Group` objects and reading their metadata (option
names, choices, bounds, `default_permissions`) is plain object
construction that discord.py supports with no gateway connection, no
event loop, and no bot token. It's not the same thing as *syncing* those
commands to Discord, which is exactly the part this suite doesn't try to
cover (`test_command_registration_v3.py` covers what gets synced where).

These began as the v2 tree's checks. At the cutover they moved to the per-server
groups, keeping every bound, default and permission assertion; the ones about
`/newsbot test-alert` became the opposite assertion (it's gone, whatever the
config says).
"""

from __future__ import annotations

import discord
import pytest
from pydantic import SecretStr

from newsbot.bot.client import NewsBot
from newsbot.bot.commands import (
    game_choices,
    make_guild_admin_group,
    make_lounge_group,
    make_member_news_group,
    make_member_shift_group,
)
from newsbot.config import Secrets


class _FakeBot:
    db_path = ":memory:"


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def _command(group: discord.app_commands.Group, name: str) -> discord.app_commands.Command:
    return next(c for c in group.commands if c.name == name)


def _param(command: discord.app_commands.Command, name: str) -> discord.app_commands.Parameter:
    return next(p for p in command.parameters if p.name == name)


# --- /news recent ---


def test_recent_game_choices_include_every_followed_game_and_all(v3_cfg):
    # v2 listed every topic as a fixed choice; v3 autocompletes over the server's own games
    # (and "All"), so the same assertion is about what the autocomplete offers.
    keys = [g.key for g in v3_cfg.catalog]
    choices = game_choices(v3_cfg.catalog, keys, "", include_all=True)
    assert [c.value for c in choices] == ["all", *keys]
    assert choices[0].name == "All"


def test_recent_choices_stay_within_discords_25_choice_limit(v3_cfg):
    # Discord shows 25 autocomplete choices at most, however big the catalog gets.
    keys = [g.key for g in v3_cfg.catalog] * 20
    assert len(game_choices(v3_cfg.catalog, keys, "", include_all=True)) <= 25


def test_recent_label_choices_match_the_three_labels(v3_cfg):
    group = make_member_news_group(v3_cfg, ":memory:")
    label = _param(_command(group, "recent"), "label")
    assert [c.value for c in label.choices] == ["official", "reported", "rumor"]


def test_recent_days_option_bounds_are_one_to_thirty(v3_cfg):
    group = make_member_news_group(v3_cfg, ":memory:")
    days = _param(_command(group, "recent"), "days")
    assert days.min_value == 1
    assert days.max_value == 30


# --- /news search ---


def test_search_query_option_length_bounds_are_one_to_a_hundred(v3_cfg):
    group = make_member_news_group(v3_cfg, ":memory:")
    query = _param(_command(group, "search"), "query")
    assert query.min_value == 1
    assert query.max_value == 100


def test_search_days_option_bounds_are_one_to_thirty(v3_cfg):
    group = make_member_news_group(v3_cfg, ":memory:")
    days = _param(_command(group, "search"), "days")
    assert days.min_value == 1
    assert days.max_value == 30


# --- /newsbot admin group ---


def test_admin_group_has_default_permissions_set(v3_cfg):
    group = make_guild_admin_group(v3_cfg, _FakeBot())
    assert group.default_permissions is not None
    assert group.default_permissions.manage_guild is True


def test_admin_group_default_permissions_follow_configured_admin_permission(v3_cfg):
    cfg = v3_cfg.model_copy(update={"admin_permission": "kick_members"})
    group = make_guild_admin_group(cfg, _FakeBot())
    assert group.default_permissions.kick_members is True
    assert group.default_permissions.manage_guild is False


def test_admin_group_commands(v3_cfg):
    group = make_guild_admin_group(v3_cfg, _FakeBot())
    assert {c.name for c in group.commands} == {
        "setup",
        "follow",
        "unfollow",
        "games",
        "settings",
        "shift",
        "status",
        "preview",
        "run-now",
    }


# --- NewsBot construction ---


def test_bot_constructs_with_allowed_mentions_none(v3_cfg):
    # AllowedMentions doesn't define __eq__, so this compares the fields
    # that actually matter: nothing the digest posts should ever be able
    # to ping @everyone, a role, or an arbitrary user.
    bot = NewsBot(v3_cfg, _secrets(), ":memory:")
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False
    assert bot.allowed_mentions.replied_user is False


def test_bot_constructs_with_default_non_privileged_intents(v3_cfg):
    # The three privileged intents (members, presences, message_content)
    # all require an approved application in the Discord dev portal;
    # this bot doesn't use any of them (members only when a lounge turns
    # welcomes on, which these tests don't), and shouldn't accidentally
    # start asking for one.
    bot = NewsBot(v3_cfg, _secrets(), ":memory:")
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.message_content is False
    assert bot.intents == discord.Intents.default()


# --- /newsbot test-alert is gone (owner decision, 2026-10-01) ---


@pytest.mark.parametrize("allow_test_command", [False, True])
def test_test_alert_is_never_registered_whatever_the_config_says(v3_cfg, allow_test_command):
    # v2 registered it only when `allow_test_command` was true, so a prod config could never
    # carry it by accident. There's no v3 version at all, so the flag has nothing left to switch.
    shift = v3_cfg.shift.model_copy(update={"allow_test_command": allow_test_command})
    cfg = v3_cfg.model_copy(update={"shift": shift})
    group = make_guild_admin_group(cfg, _FakeBot())
    assert "test-alert" not in {c.name for c in group.commands}


# --- /shift codes (design.md §13, plan step 6) ---
#
# This file stays at the same "cheap object-level checks" level as everything
# else here: option bounds and defaults, given a group the factory always builds.


def test_shift_group_has_one_codes_command(v3_cfg):
    group = make_member_shift_group(v3_cfg, ":memory:")
    assert {c.name for c in group.commands} == {"codes"}


def test_shift_codes_days_option_bounds_are_one_to_ninety(v3_cfg):
    group = make_member_shift_group(v3_cfg, ":memory:")
    days = _param(_command(group, "codes"), "days")
    assert days.min_value == 1
    assert days.max_value == 90
    assert days.default == 14


def test_shift_codes_public_option_defaults_false(v3_cfg):
    group = make_member_shift_group(v3_cfg, ":memory:")
    public = _param(_command(group, "codes"), "public")
    assert public.default is False


# --- /lounge quote-now (design.md §14) ---


def test_quote_now_description(v3_cfg):
    group = make_lounge_group(v3_cfg, _FakeBot())
    assert _command(group, "quote-now").description == (
        "Post today's lounge quote now (the scheduled one then skips today)."
    )
