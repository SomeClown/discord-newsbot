"""End-to-end (but gateway-free) tests for the `/newsbot test-alert` handler.

`test_commands_logic.py` and `test_command_registration.py` already cover
this command's pure decision functions (`invalid_test_alert_code_message`,
`summarize_test_alert`) and its registration. What's missing is the handler
closure itself -- `newsbot.bot.commands.make_admin_group`'s `test_alert`
callback -- which needs *something* interaction-shaped to call. Per the
test-engineer brief, this uses a small hand-written `FakeInteraction`
(same spirit as `FakeChannel`/`FakeClient` in `test_code_alert_poster.py`:
just enough surface to drive the handler, not a fake gateway) rather than
mocking discord.py's real `Interaction`.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from newsbot.bot.commands import make_admin_group
from newsbot.config import load_config
from newsbot.pipeline.lock import _run_lock
from newsbot.shift.sweep import PrintCodeAlertPoster, SweepDeps
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
VALID_CODE = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self._done = False

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True

    async def send_message(self, content: str = "", *, ephemeral: bool = False, view=None) -> None:
        self.messages.append((content, ephemeral))
        self._done = True

    def is_done(self) -> bool:
        return self._done


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str = "", *, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, *, permissions: discord.Permissions, user_id: int = 1) -> None:
        self.permissions = permissions
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = SimpleNamespace(qualified_name="newsbot test-alert")


class _FakeBot:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def build_sweep_deps(self) -> SweepDeps:
        return SweepDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            http=None,
            collectors=[],
            now=lambda: datetime(2026, 9, 25, 20, 0, tzinfo=UTC),
            alert=self._alert,
            poster=PrintCodeAlertPoster(),
        )

    async def _alert(self, text: str) -> None:
        pass


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _cfg(*, allow_test_command: bool = True):
    cfg = load_config(CONFIG_PATH)
    alerts = cfg.alerts.model_copy(
        update={"enabled": True, "allow_test_command": allow_test_command}
    )
    return cfg.model_copy(update={"alerts": alerts})


def _test_alert_command(cfg, bot):
    group = make_admin_group(cfg, bot)
    return next(c for c in group.commands if c.name == "test-alert")


def _admin_permissions(cfg) -> discord.Permissions:
    return discord.Permissions(**{cfg.admin_permission: True})


def _non_admin_permissions() -> discord.Permissions:
    return discord.Permissions.none()


# --- non-admins rejected server-side ---


async def test_non_admin_rejected_before_anything_else_runs(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_non_admin_permissions())

    await command.callback(interaction, code=VALID_CODE, golden=False)

    assert interaction.response.messages == [("You don't have permission to run this.", True)]
    assert interaction.followup.messages == []
    # Never got far enough to touch the DB at all.
    with closing(connect(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM alerted_codes").fetchone()[0]
    assert count == 0


async def test_non_admin_rejected_even_with_an_invalid_code():
    # Admin check runs before code validation -- a non-admin shouldn't get
    # to learn anything about code shape validation from this command.
    cfg = _cfg()
    bot = _FakeBot(":memory:")
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_non_admin_permissions())

    await command.callback(interaction, code="not-a-real-shift-code!!!!!!!", golden=False)

    assert interaction.response.messages == [("You don't have permission to run this.", True)]


async def test_administrator_flag_alone_is_sufficient(db_path):
    # has_admin_permission() also honors the blanket Administrator bit,
    # independent of the configured admin_permission -- pinned here
    # through the real handler, not just the pure function.
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=discord.Permissions(administrator=True))

    await command.callback(interaction, code=VALID_CODE, golden=False)

    assert interaction.response.messages == []  # never hit the denial path
    assert interaction.followup.messages  # got as far as a real reply


# --- bad codes rejected ---


async def test_admin_with_invalid_code_gets_the_not_a_code_message(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code="A" * 29, golden=False)  # 29 chars, wrong shape

    assert len(interaction.response.messages) == 1
    assert "doesn't look like a SHiFT code" in interaction.response.messages[0][0]
    assert interaction.followup.messages == []


async def test_admin_with_whitespace_padded_code_gets_the_not_a_code_message(db_path):
    # Range[str, 29, 29] only bounds length -- a 29-character string that's
    # mostly whitespace still reaches the handler and must still be
    # rejected by is_code's shape check.
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    padded = " " + VALID_CODE[:28]  # 29 chars, leading space
    assert len(padded) == 29
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=padded, golden=False)

    assert len(interaction.response.messages) == 1
    assert "doesn't look like a SHiFT code" in interaction.response.messages[0][0]


async def test_admin_with_trailing_whitespace_code_gets_the_not_a_code_message(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    padded = VALID_CODE[:28] + " "  # 29 chars, trailing space
    assert len(padded) == 29
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=padded, golden=False)

    assert len(interaction.response.messages) == 1
    assert "doesn't look like a SHiFT code" in interaction.response.messages[0][0]


async def test_admin_with_fullwidth_lookalike_code_gets_the_not_a_code_message(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    fullwidth = "Ａ" * 5 + "-AAAAA-AAAAA-AAAAA-AAAAA"  # fullwidth 'A' x5 in the first group
    assert len(fullwidth) == 29
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=fullwidth, golden=False)

    assert len(interaction.response.messages) == 1
    assert "doesn't look like a SHiFT code" in interaction.response.messages[0][0]


async def test_admin_with_lowercase_code_is_accepted_as_a_real_code(db_path):
    # is_code() has no re.IGNORECASE flag missing on purpose (both cases
    # are spelled out in the character class), so a lowercase code is a
    # perfectly valid SHiFT code shape and must reach run_test_alert, not
    # the rejection message.
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=VALID_CODE.lower(), golden=False)

    assert interaction.response.messages == []  # no rejection
    assert len(interaction.followup.messages) == 1
    assert "doesn't look like" not in interaction.followup.messages[0][0]


# --- busy ---


async def test_busy_run_in_progress_gets_the_busy_message(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await _run_lock.acquire()
    try:
        await command.callback(interaction, code=VALID_CODE, golden=False)
    finally:
        _run_lock.release()

    assert interaction.response.messages == [("A run or code check is in progress.", True)]
    assert interaction.followup.messages == []


async def test_not_busy_after_lock_released_reaches_the_pipeline(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await _run_lock.acquire()
    _run_lock.release()  # busy, then free again -- confirms the check isn't stuck "on"
    await command.callback(interaction, code=VALID_CODE, golden=False)

    assert interaction.response.messages == []
    assert len(interaction.followup.messages) == 1


# --- successful posts: summarize_test_alert branches, exercised end to end ---


async def test_first_call_posts_with_ping(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=VALID_CODE, golden=False)

    assert interaction.followup.messages == [("posted with ping", True)]


async def test_repeat_code_reports_already_alerted(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)

    first = FakeInteraction(permissions=_admin_permissions(cfg))
    await command.callback(first, code=VALID_CODE, golden=False)

    second = FakeInteraction(permissions=_admin_permissions(cfg))
    await command.callback(second, code=VALID_CODE, golden=False)

    assert second.followup.messages == [("already alerted, nothing posted", True)]


async def test_golden_true_is_accepted_and_posts(db_path):
    cfg = _cfg()
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=VALID_CODE, golden=True)

    assert interaction.followup.messages == [("posted with ping", True)]


async def test_cap_exhausted_posts_without_ping(db_path):
    cfg = _cfg()
    alerts = cfg.alerts.model_copy(update={"max_pings_per_day": 0})
    cfg = cfg.model_copy(update={"alerts": alerts})
    bot = _FakeBot(db_path)
    bot.cfg = cfg
    command = _test_alert_command(cfg, bot)
    interaction = FakeInteraction(permissions=_admin_permissions(cfg))

    await command.callback(interaction, code=VALID_CODE, golden=False)

    assert interaction.followup.messages == [("posted without ping (cap)", True)]
