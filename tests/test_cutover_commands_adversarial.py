"""Slash commands at the cutover, and the v2 code that's supposed to be gone.

The friend's server has v2's `/newsbot`, `/news` and `/shift` registered as *guild* commands,
because v2 synced per guild. v3 registers the same three names *globally* and gives guilds
only what is theirs alone (`/owner` in the home guild, `/lounge` where a lounge has the
quote on). Until the old guild copy is gone the friend's slash menu shows every command
twice, which is the sort of thing that gets reported as "the bot is broken" by someone who is
being perfectly reasonable.

The plan says the copy goes away on its own: a guild-scope sync is a bulk overwrite, so
syncing the friend's guild with a command set that contains only `/owner` and `/lounge`
replaces the v2 set. That is true, and these tests check each link of it: the friend's guild
really is a sync scope, its payload really has none of the old names, and the hash gate
(`app_state` keys `commands:<scope>`) has nothing stored at cutover because v2.2 never wrote
an `app_state` table at all. The friend's guild is always a scope, even with the quote off and
the home guild somewhere else. The one link that stays conditional is pinned too: no re-sync
after a rollback and roll-forward, because the stored hash still matches and Discord's copy does
not (the plan's deploy notes tell the owner to clear the hashes first).

The second half is housekeeping with teeth: names that were deleted must stay deleted, and
the repo functions the cutover orphaned (`claim_codes`, `save_run` and friends) must not be
called from anywhere a bot could reach.
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime
from pathlib import Path

import discord
import pytest
from cutover_world import FRIEND, OWNER_GUILD, PRODLIKE

import newsbot.bot.client as client_module
import newsbot.bot.commands as commands_module
import newsbot.bot.permissions as permissions_module
import newsbot.pipeline.run as run_module
import newsbot.shift.sweep as sweep_module
from newsbot.config import load_config

T_NOON = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)
ROOT = Path(__file__).parent.parent / "newsbot"


def synced_scopes(world) -> list:
    return [None if guild is None else guild.id for guild in world.tree_syncs]


def names_in(world, guild_id: int | None) -> set[str]:
    guild = None if guild_id is None else discord.Object(id=guild_id)
    return {command.name for command in world.bot.tree.get_commands(guild=guild)}


def quote_off_config(tmp_path):
    config = tmp_path / "quote-off.yaml"
    config.write_text(
        PRODLIKE.read_text().replace(
            "daily_quote:\n    enabled: true", "daily_quote:\n    enabled: false"
        )
    )
    return load_config(config)


# --- the friend's old per-guild copy ---


async def test_the_first_v3_start_syncs_the_global_set_and_the_friends_guild(make_world, v22_db):
    # v2.2 never made an app_state table, so there is nothing stored to gate the first sync.
    world = await make_world(now=T_NOON)

    assert synced_scopes(world) == [None, FRIEND]
    keys = {k for (k,) in world.rows("SELECT key FROM app_state WHERE key LIKE 'commands:%'")}
    assert keys == {"commands:global", f"commands:{FRIEND}"}


async def test_the_friends_guild_payload_has_none_of_the_v2_names(make_world):
    # What the overwrite sends: with the home guild defaulting to the friend's, that's `/owner`
    # and `/lounge`. Everything v2 registered in that guild (`news`, `newsbot`, `shift`)
    # is absent from the payload, which is what makes the sync a replacement.
    world = await make_world(now=T_NOON)

    assert names_in(world, FRIEND) == {"owner", "lounge"}
    assert names_in(world, None) == {"news", "newsbot", "shift"}
    assert names_in(world, FRIEND).isdisjoint(names_in(world, None))


async def test_a_second_start_with_nothing_changed_syncs_nothing(make_world):
    first = await make_world(now=T_NOON)
    assert synced_scopes(first) == [None, FRIEND]

    second = await make_world(now=T_NOON, channels=first.channels)

    assert synced_scopes(second) == []


async def test_a_rollback_and_roll_forward_does_not_resync_the_guild_copy(make_world):
    # Pinned, and the one link in the chain I'd call a gap. v2.2 syncs per guild on every
    # start, so a rollback puts its `/newsbot` back in the friend's guild. v2.2 knows nothing
    # about `app_state`, so the stored hash from the first v3 start is still there, still
    # matches, and the next v3 start sends nothing. The duplicate commands come back and stay
    # until the owner clears the hashes (DELETE FROM app_state WHERE key LIKE 'commands:%').
    first = await make_world(now=T_NOON)
    assert synced_scopes(first) == [None, FRIEND]
    # ... the rollback: v2.2 runs for a while and re-syncs its own commands into the guild ...
    after_rollback = await make_world(now=T_NOON, channels=first.channels)

    assert synced_scopes(after_rollback) == []


async def test_clearing_the_stored_hashes_is_enough_to_resync_everything(make_world):
    first = await make_world(now=T_NOON)
    first.run("DELETE FROM app_state WHERE key LIKE 'commands:%'")

    again = await make_world(now=T_NOON, channels=first.channels)

    assert synced_scopes(again) == [None, FRIEND]


async def test_with_the_quote_off_and_a_separate_home_the_friends_guild_is_still_a_sync_scope(
    make_world, tmp_path
):
    # The imported server is always a sync scope, so the old v2 `/newsbot`, `/news` and
    # `/shift` guild copies are cleared there even with no lounge quote and a home guild
    # that's somewhere else. (It used to be left out, and the duplicates stayed.)
    world = await make_world(now=T_NOON, cfg=quote_off_config(tmp_path), owner_channel=True)

    assert synced_scopes(world) == [None, OWNER_GUILD, FRIEND]
    assert names_in(world, FRIEND) == set()  # an empty set: the overwrite that clears v2's copies


async def test_dev_mode_never_syncs_globally_and_only_the_listed_guilds(make_world):
    cfg = load_config(PRODLIKE).model_copy(update={"command_guild_ids": [FRIEND]})

    world = await make_world(now=T_NOON, cfg=cfg)

    assert synced_scopes(world) == [FRIEND]
    assert {"news", "newsbot", "shift"} <= names_in(world, FRIEND)


# --- allow_test_command: still loads, says it's ignored ---


def _config_with(tmp_path, source: Path, old: str, new: str):
    config = tmp_path / "with-test-command.yaml"
    config.write_text(source.read_text().replace(old, new))
    return config


def test_a_v22_file_with_the_test_command_on_loads_and_says_it_is_ignored(tmp_path, caplog):
    config = _config_with(
        tmp_path, PRODLIKE, "allow_test_command: false", "allow_test_command: true"
    )

    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(config)

    assert cfg.shift.allow_test_command is True  # it loads, so a rollback can keep the file
    assert "allow_test_command is ignored" in caplog.text


def test_the_v3_shift_key_gets_the_same_warning(tmp_path, caplog):
    config = _config_with(
        tmp_path,
        Path(__file__).parent / "fixtures" / "config_v3.yaml",
        "allow_test_command: false",
        "allow_test_command: true",
    )

    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        load_config(config)

    assert "allow_test_command is ignored" in caplog.text


# --- v2 code that stays deleted ---

REMOVED = {
    commands_module: [
        "make_news_group",
        "make_admin_group",
        "make_shift_group",
        "summarize_test_alert",
        "invalid_test_alert_code_message",
    ],
    permissions_module: ["required_channels", "check_channels", "render_permission_alert"],
    sweep_module: ["SweepDeps", "process_items", "run_code_sweep", "run_test_alert"],
    run_module: ["run_daily", "build_digest", "Deps", "PipelineOutcome", "RunMode", "_run_post"],
    client_module: ["should_catch_up", "build_intents", "schedule_daily_quote", "NullPublisher"],
}
REMOVED_METHODS = [
    "_daily_job",
    "_catch_up",
    "_sweep_job",
    "publisher_for_today",
    "run_quote",
    "_quote_job",
    "build_deps",
    "build_sweep_deps",
    "_check_permissions",
]


@pytest.mark.parametrize(
    ("module", "name"), [(m, n) for m, names in REMOVED.items() for n in names]
)
def test_a_deleted_v2_name_stays_deleted(module, name):
    assert not hasattr(module, name), f"{module.__name__}.{name} came back"


@pytest.mark.parametrize("name", REMOVED_METHODS)
def test_the_bot_has_none_of_the_v2_job_or_helper_methods(name):
    assert not hasattr(client_module.NewsBot, name)


def _references(name: str) -> list[tuple[str, int]]:
    """Every use of `name` in newsbot/: a call, an attribute, an import; not its own `def`."""
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Name) and node.id == name) or (
                isinstance(node, ast.Attribute) and node.attr == name
            ):
                hits.append((str(path.relative_to(ROOT)), node.lineno))
            elif isinstance(node, ast.alias) and node.name == name:
                hits.append((str(path.relative_to(ROOT)), 0))
    return hits


@pytest.mark.parametrize(
    "name",
    [
        "claim_codes",
        "mark_codes_posted",
        "mark_codes_failed",
        "save_run",
        "claim_digest",
        "mark_digest_failed",
        "get_digest",
        "alert_status",
        "status_snapshot",
        "guild_posted_codes",
    ],
)
def test_the_orphaned_v2_repo_functions_are_not_called_from_anywhere(name):
    assert _references(name) == []


def test_the_v2_command_groups_are_not_imported_by_anything_in_the_package():
    for name in ("make_news_group", "make_admin_group", "make_shift_group"):
        assert _references(name) == []


def test_no_shell_script_or_compose_file_still_calls_the_retired_cli_modes():
    repo = ROOT.parent
    stale = []
    for pattern in ("scripts/*", "docker-compose*.yml", "Dockerfile*", ".github/workflows/*"):
        for path in repo.glob(pattern):
            if path.is_file():
                text = path.read_text(errors="ignore")
                if "--sweep" in text or "run_daily" in text:
                    stale.append(str(path.relative_to(repo)))
    assert stale == []
