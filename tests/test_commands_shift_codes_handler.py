"""End-to-end (but gateway-free) tests for the `/shift codes` handler.

Same spirit as `test_commands_test_alert_handler.py`: a hand-written
`FakeInteraction`/`FakeFollowup` drives `newsbot.bot.commands.make_shift_group`'s
`codes` callback against a real temp-file sqlite DB, rather than mocking
discord.py's `Interaction`. `test_command_registration.py` already covers
registration and option bounds; this file is what's missing -- the handler
actually run against rows in `alerted_codes`.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import newsbot.bot.commands as commands_module
from newsbot.bot.commands import make_shift_group
from newsbot.config import load_config
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"
LA = ZoneInfo("America/Los_Angeles")


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.edits: list[dict] = []
        self._done = False

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True

    async def send_message(self, content: str = "", *, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))
        self._done = True

    async def edit_message(self, **kwargs) -> None:
        self.edits.append(kwargs)

    def is_done(self) -> bool:
        return self._done


class FakeFollowup:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *, embed=None, view=None, ephemeral: bool = False, **kwargs) -> None:
        self.calls.append({"embed": embed, "view": view, "ephemeral": ephemeral, **kwargs})


class FakeInteraction:
    def __init__(self, *, user_id: int = 1) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _cfg():
    cfg = load_config(CONFIG_PATH)
    alerts = cfg.alerts.model_copy(update={"enabled": True, "channel_id": 1})
    return cfg.model_copy(update={"alerts": alerts})


def _codes_command(cfg, db_path):
    group = make_shift_group(cfg, db_path)
    return next(c for c in group.commands if c.name == "codes")


def _insert(
    db_path: str,
    code: str,
    *,
    first_seen_at: datetime,
    status: str = "posted",
    from_roundup: bool = False,
    source_name: str = "Reddit",
    item_url: str = "https://example.com/item",
    pinged: bool = False,
) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                first_seen_at.astimezone(UTC).isoformat(),
                source_name,
                item_url,
                status,
                int(from_roundup),
                int(pinged),
            ),
        )
        conn.commit()


def _code(n: int) -> str:
    return f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA"


def _freeze_now(monkeypatch, moment: datetime) -> None:
    """Pin `commands.py`'s `datetime.now(UTC)` -- the handler computes `since`
    from real wall time, so a DST/window-boundary test needs this fixed."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(commands_module, "datetime", _FrozenDatetime)


def _allowed_mentions_none(mentions: discord.AllowedMentions) -> bool:
    # discord.AllowedMentions has no __eq__, so two "equivalent" instances
    # compare unequal by identity -- compare the fields that matter instead.
    return mentions.everyone is False and mentions.users is False and mentions.roles is False


# --- mixed statuses: pending/failed excluded, everything else shown with the right marker ---


async def test_mixed_statuses_excludes_pending_and_failed(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    _insert(db_path, _code(1), first_seen_at=now, status="posted")
    _insert(db_path, _code(2), first_seen_at=now, status="seeded")
    _insert(db_path, _code(3), first_seen_at=now, status="too_old")
    _insert(db_path, _code(4), first_seen_at=now, status="roundup", from_roundup=True)
    _insert(db_path, _code(5), first_seen_at=now, status="pending")
    _insert(db_path, _code(6), first_seen_at=now, status="failed")
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    assert len(interaction.followup.calls) == 1
    call = interaction.followup.calls[0]
    description = call["embed"].description
    assert _code(1) in description
    assert _code(2) in description
    assert _code(3) in description
    assert _code(4) in description
    assert _code(5) not in description
    assert _code(6) not in description
    assert "already around when alerts started" in description  # seeded marker
    assert "old post" in description  # too_old marker
    assert "from a roundup" in description  # roundup marker


async def test_roundup_status_shows_from_a_roundup_marker_not_a_fourth_state(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    _insert(db_path, _code(1), first_seen_at=now, status="roundup", from_roundup=True)
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    description = interaction.followup.calls[0]["embed"].description
    assert "from a roundup" in description


# --- empty window ---


async def test_empty_window_shows_the_no_codes_message(db_path):
    cfg = _cfg()
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert embed.description == "No codes seen in that window."


async def test_codes_outside_the_days_window_are_excluded(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    _insert(db_path, _code(1), first_seen_at=now - timedelta(days=20))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert embed.description == "No codes seen in that window."


# --- days window around midnight / DST in America/Los_Angeles ---


async def test_code_just_inside_the_days_window_around_la_midnight_is_included(
    db_path, monkeypatch
):
    # 14 days back from "now" is a fixed instant regardless of timezone
    # (resolve happens in UTC in the handler, via datetime.now(UTC) -
    # timedelta(days=days)) -- this pins that a code sitting right at the
    # LA midnight boundary of that window still shows up.
    cfg = _cfg()
    now = datetime(2026, 3, 15, 7, 59, tzinfo=UTC)  # just after LA midnight (PDT, UTC-7)
    _freeze_now(monkeypatch, now)
    boundary = now - timedelta(days=14) + timedelta(minutes=1)
    _insert(db_path, _code(1), first_seen_at=boundary)
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert _code(1) in embed.description


async def test_code_just_outside_the_days_window_around_la_midnight_is_excluded(
    db_path, monkeypatch
):
    cfg = _cfg()
    now = datetime(2026, 3, 15, 7, 59, tzinfo=UTC)
    _freeze_now(monkeypatch, now)
    just_outside = now - timedelta(days=14) - timedelta(minutes=1)
    _insert(db_path, _code(1), first_seen_at=just_outside)
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert embed.description == "No codes seen in that window."


async def test_first_seen_date_reflects_dst_spring_forward_boundary(db_path, monkeypatch):
    # 2026-03-08 02:00 America/Los_Angeles is the spring-forward instant
    # (clocks jump to 03:00 PDT); a code first seen right after it should
    # render its date in PDT (UTC-7), not PST (UTC-8).
    cfg = _cfg()
    first_seen = datetime(2026, 3, 8, 10, 30, tzinfo=UTC)  # 03:30 PDT
    _freeze_now(monkeypatch, first_seen + timedelta(hours=1))
    _insert(db_path, _code(1), first_seen_at=first_seen)
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=30, public=False)

    embed = interaction.followup.calls[0]["embed"]
    expected_date = first_seen.astimezone(LA).date().isoformat()
    assert expected_date in embed.description
    assert expected_date == "2026-03-08"


async def test_first_seen_date_reflects_dst_fall_back_boundary(db_path, monkeypatch):
    # 2026-11-01 02:00 America/Los_Angeles is the fall-back instant
    # (clocks repeat 01:00-02:00, first in PDT then PST).
    cfg = _cfg()
    first_seen = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 PST (after the repeat)
    _freeze_now(monkeypatch, first_seen + timedelta(hours=1))
    _insert(db_path, _code(1), first_seen_at=first_seen)
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=30, public=False)

    embed = interaction.followup.calls[0]["embed"]
    expected_date = first_seen.astimezone(LA).date().isoformat()
    assert expected_date in embed.description


# --- paging: 17 codes, page size 8 ---


async def test_seventeen_codes_page_one_shows_eight_newest_first(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert(db_path, _code(i), first_seen_at=now - timedelta(minutes=i))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=90, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert "Page 1 of 3" in embed.footer.text
    # Newest first: code 0 (minutes=0 back) is the newest.
    assert _code(0) in embed.description
    assert _code(7) in embed.description
    assert _code(8) not in embed.description


async def test_seventeen_codes_paging_to_page_three_shows_the_last_code(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert(db_path, _code(i), first_seen_at=now - timedelta(minutes=i))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=90, public=False)
    view = interaction.followup.calls[0]["view"]
    assert view.total_pages == 3

    page_interaction = FakeInteraction(user_id=1)
    await view.next_button.callback(page_interaction)
    await view.next_button.callback(page_interaction)

    embed = page_interaction.response.edits[-1]["embed"]
    assert "Page 3 of 3" in embed.footer.text
    assert _code(16) in embed.description  # oldest code, last page


# --- public vs ephemeral ---


async def test_public_false_sends_ephemeral(db_path):
    cfg = _cfg()
    _insert(db_path, _code(1), first_seen_at=datetime.now(UTC))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    assert interaction.followup.calls[0]["ephemeral"] is True


async def test_public_true_sends_non_ephemeral(db_path):
    cfg = _cfg()
    _insert(db_path, _code(1), first_seen_at=datetime.now(UTC))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=True)

    assert interaction.followup.calls[0]["ephemeral"] is False


# --- requester-only pager ---


async def test_a_different_user_cannot_page_the_requesters_result(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert(db_path, _code(i), first_seen_at=now - timedelta(minutes=i))
    command = _codes_command(cfg, db_path)
    requester = FakeInteraction(user_id=1)

    await command.callback(requester, days=90, public=False)
    view = requester.followup.calls[0]["view"]

    other_user = FakeInteraction(user_id=2)
    allowed = await view.interaction_check(other_user)

    assert allowed is False
    assert other_user.response.messages == [("Only the requester can do this.", True)]


async def test_the_requester_can_page_their_own_result(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert(db_path, _code(i), first_seen_at=now - timedelta(minutes=i))
    command = _codes_command(cfg, db_path)
    requester = FakeInteraction(user_id=1)

    await command.callback(requester, days=90, public=False)
    view = requester.followup.calls[0]["view"]

    same_user = FakeInteraction(user_id=1)
    allowed = await view.interaction_check(same_user)

    assert allowed is True


# --- AllowedMentions.none() on every send path ---


async def test_first_page_send_uses_allowed_mentions_none(db_path):
    cfg = _cfg()
    _insert(db_path, _code(1), first_seen_at=datetime.now(UTC))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    call = interaction.followup.calls[0]
    assert _allowed_mentions_none(call["allowed_mentions"])


async def test_paged_edit_also_uses_allowed_mentions_none(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert(db_path, _code(i), first_seen_at=now - timedelta(minutes=i))
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=90, public=False)
    view = interaction.followup.calls[0]["view"]

    page_interaction = FakeInteraction(user_id=1)
    await view.next_button.callback(page_interaction)

    assert _allowed_mentions_none(page_interaction.response.edits[-1]["allowed_mentions"])


async def test_hostile_source_name_never_produces_a_live_mention_in_the_embed(db_path):
    cfg = _cfg()
    _insert(
        db_path,
        _code(1),
        first_seen_at=datetime.now(UTC),
        source_name="@everyone <@123456789012345678> __pwned__",
    )
    command = _codes_command(cfg, db_path)
    interaction = FakeInteraction()

    await command.callback(interaction, days=14, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert "@everyone" not in embed.description
