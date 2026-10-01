"""Shared fakes for the v3 command tests (plan task 9): interactions, channels, a bot.

Hand-written like the v2 handler tests' fakes, for the same reason: the handlers only
touch a handful of attributes, and a fake that shows exactly which ones is worth more
than a mock that accepts anything. `FakeInteraction.sent` is every message the handler
produced, in order, whether it went through the initial response or a followup.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import discord

from newsbot.config import LoungeCfg, _RawConfig, load_config
from newsbot.store import repo
from newsbot.store.db import connect

CONFIG_V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
ADMIN = discord.Permissions(manage_guild=True)
MEMBER = discord.Permissions(send_messages=True)
HOME = 200000000000000001
GUILD_A = 300000000000000001
GUILD_B = 300000000000000002
OWNER_ID = 42


def load_v3_config(monkeypatch, **overrides):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(CONFIG_V3)
    return cfg.model_copy(update=overrides) if overrides else cfg


class FakeResponse:
    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent
        self._done = False
        self.deferred_ephemeral: bool | None = None
        self.edits: list[dict] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.deferred_ephemeral = ephemeral

    async def send_message(self, content=None, **kwargs) -> None:
        self._done = True
        self._sent.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs) -> None:
        self.edits.append(kwargs)

    def is_done(self) -> bool:
        return self._done


class FakeFollowup:
    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent

    async def send(self, content=None, **kwargs) -> None:
        self._sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(
        self,
        *,
        guild_id: int | None = GUILD_A,
        user_id: int = 1,
        permissions: discord.Permissions = ADMIN,
    ) -> None:
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.permissions = permissions
        self.command = SimpleNamespace(qualified_name="fake")
        self.sent: list[dict] = []
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup(self.sent)
        self.original_edits: list[dict] = []

    async def edit_original_response(self, **kwargs) -> None:
        self.original_edits.append(kwargs)

    @property
    def texts(self) -> list[str]:
        return [m["content"] for m in self.sent if m.get("content")]

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


def channel(channel_id: int, guild_id: int | None = GUILD_A, kind=discord.ChannelType.text):
    """A resolved channel: the owning guild on `.guild`, like a cached `TextChannel`."""
    guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    return SimpleNamespace(id=channel_id, type=kind, guild=guild)


def thin_channel(channel_id: int, guild_id: int, kind=discord.ChannelType.text):
    """An uncached channel: `AppCommandChannel` has `.guild_id` and no `.guild`."""
    return SimpleNamespace(id=channel_id, type=kind, guild_id=guild_id)


def role(role_id: int, guild_id: int = GUILD_A, *, default: bool = False):
    return SimpleNamespace(
        id=role_id, guild=SimpleNamespace(id=guild_id), is_default=lambda: default
    )


def make_guild(db_path, guild_id=GUILD_A, *, set_up=True, tier="free", games=(), **kwargs):
    """Create a server row, following `games` as `(key, channel_id)` pairs."""
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, guild_id, set_up=set_up, tier=tier, **kwargs)
        for key, channel_id in games:
            repo.follow_game(conn, guild_id, key, channel_id)


def command(group, name):
    return next(c for c in group.commands if c.name == name)


def legacy_lounge(cfg) -> LoungeCfg:
    """The v2 `lounge:` block as the loader parsed it (switched off, the v2 default, if absent).

    The loaded `AppConfig` no longer carries the v2 keys; the one server's old lounge setup
    is `cfg.legacy.lounge`, which the import reads exactly once. The config tests that check
    how the block is parsed and validated read it back from there.
    """
    if cfg.legacy is not None and cfg.legacy.lounge is not None:
        return cfg.legacy.lounge
    return LoungeCfg()


def raw_v2(path):
    """The v2 keys as a file spells them (`topics`, `digest`, `sources`...), before any deriving.

    `AppConfig` has no v2 fields any more, but a few tests need the v2 view as their yardstick:
    "the prompt for the friend is byte-identical to what v2 built" can only be checked against
    what v2 built it from.
    """
    import yaml

    return _RawConfig.model_validate(yaml.safe_load(Path(path).read_text()))
