"""End-to-end (gateway-free) tests for `/news recent` and `/news search`.

Plan step 6 generalized `_first_page_and_view` so `/shift codes` could
share it; this file pins that the shared helper left page counts and
first-page contents alone, with a hand-written `FakeInteraction` against a
real temp sqlite DB.

These were the v2 tests (one server, config-driven). They moved to
`make_member_news_group` when v2 retired, run as a comped server that follows
Borderlands 4 and Palworld (comped, because stories are what these assert on;
a free server's headlines are in `test_member_commands_scoped.py`).
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from newsbot.bot.commands import make_member_news_group
from newsbot.config import load_config
from newsbot.store import repo
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_v3.yaml"
GUILD = 300000000000000001


class FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.edits: list[dict] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
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
        self.guild_id = GUILD
        self.response = FakeResponse()
        self.followup = FakeFollowup()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.create_guild(conn, GUILD, set_up=True, tier="comped")
        repo.follow_game(conn, GUILD, "borderlands4", 11)
        repo.follow_game(conn, GUILD, "palworld", 12)
    return path


def _cfg():
    return load_config(CONFIG_PATH)


def _news_command(cfg, db_path, name):
    group = make_member_news_group(cfg, db_path)
    return next(c for c in group.commands if c.name == name)


def _insert_story(
    db_path: str,
    *,
    topic_key: str,
    headline: str,
    created_at: datetime,
    label: str = "official",
    summary: str = "A patch dropped.",
    digest_id: int = 1,
) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO digests (id, run_date, status, created_at, updated_at) "
            "VALUES (?, ?, 'ok', 'now', 'now')",
            (digest_id, f"2026-09-{digest_id:02d}"),
        )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                topic_key,
                headline,
                summary,
                label,
                digest_id,
                created_at.astimezone(UTC).isoformat(),
            ),
        )
        conn.commit()


def _headline(n: int) -> str:
    return f"Patch note {n}"


# --- /news recent: page counts and first page unchanged by the pager refactor ---


async def test_recent_seventeen_stories_page_size_six_gives_three_pages(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert_story(
            db_path,
            topic_key="borderlands4",
            headline=_headline(i),
            created_at=now - timedelta(minutes=i),
        )
    command = _news_command(cfg, db_path, "recent")
    interaction = FakeInteraction()

    await command.callback(interaction, game="all", days=7, label=None, public=False)

    assert len(interaction.followup.calls) == 1
    call = interaction.followup.calls[0]
    embed = call["embed"]
    assert "Page 1 of 3" in embed.footer.text
    assert _headline(0) in embed.description  # newest first
    assert _headline(5) in embed.description
    assert _headline(6) not in embed.description
    assert call["ephemeral"] is True


async def test_recent_no_stories_is_still_one_page(db_path):
    cfg = _cfg()
    command = _news_command(cfg, db_path, "recent")
    interaction = FakeInteraction()

    await command.callback(interaction, game="all", days=7, label=None, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert embed.description == "No stories found."
    view = interaction.followup.calls[0]["view"]
    assert view.total_pages == 1


async def test_recent_filters_to_the_chosen_game(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    _insert_story(db_path, topic_key="borderlands4", headline="BL4 news", created_at=now)
    _insert_story(db_path, topic_key="palworld", headline="Palworld news", created_at=now)
    command = _news_command(cfg, db_path, "recent")
    interaction = FakeInteraction()

    await command.callback(interaction, game="palworld", days=7, label=None, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert "Palworld news" in embed.description
    assert "BL4 news" not in embed.description


async def test_recent_public_true_sends_non_ephemeral(db_path):
    cfg = _cfg()
    _insert_story(
        db_path, topic_key="borderlands4", headline="BL4 news", created_at=datetime.now(UTC)
    )
    command = _news_command(cfg, db_path, "recent")
    interaction = FakeInteraction()

    await command.callback(interaction, game="all", days=7, label=None, public=True)

    assert interaction.followup.calls[0]["ephemeral"] is False


async def test_recent_paging_to_page_three_shows_the_oldest_story(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    for i in range(17):
        _insert_story(
            db_path,
            topic_key="borderlands4",
            headline=_headline(i),
            created_at=now - timedelta(minutes=i),
        )
    command = _news_command(cfg, db_path, "recent")
    interaction = FakeInteraction()
    await command.callback(interaction, game="all", days=7, label=None, public=False)
    view = interaction.followup.calls[0]["view"]
    assert view.total_pages == 3

    page_interaction = FakeInteraction(user_id=1)
    await view.next_button.callback(page_interaction)
    await view.next_button.callback(page_interaction)

    embed = page_interaction.response.edits[-1]["embed"]
    assert "Page 3 of 3" in embed.footer.text
    assert _headline(16) in embed.description  # oldest story, last page


# --- /news search: identical first-page behavior after the shared pager helper ---


async def test_search_matches_stories_by_headline(db_path):
    cfg = _cfg()
    now = datetime.now(UTC)
    _insert_story(db_path, topic_key="borderlands4", headline="Vault Hunters unite", created_at=now)
    _insert_story(db_path, topic_key="palworld", headline="Totally unrelated", created_at=now)
    command = _news_command(cfg, db_path, "search")
    interaction = FakeInteraction()

    await command.callback(interaction, query="Vault", days=30, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert "Vault Hunters unite" in embed.description
    assert "Totally unrelated" not in embed.description


async def test_search_no_matches_is_still_one_page(db_path):
    cfg = _cfg()
    command = _news_command(cfg, db_path, "search")
    interaction = FakeInteraction()

    await command.callback(interaction, query="nonexistent", days=30, public=False)

    embed = interaction.followup.calls[0]["embed"]
    assert embed.description == "No stories found."
    view = interaction.followup.calls[0]["view"]
    assert view.total_pages == 1


async def test_search_public_true_sends_non_ephemeral(db_path):
    cfg = _cfg()
    _insert_story(
        db_path, topic_key="borderlands4", headline="Vault news", created_at=datetime.now(UTC)
    )
    command = _news_command(cfg, db_path, "search")
    interaction = FakeInteraction()

    await command.callback(interaction, query="Vault", days=30, public=True)

    assert interaction.followup.calls[0]["ephemeral"] is False
