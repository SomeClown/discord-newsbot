"""Shared fakes for the v3 command tests (plan task 9): interactions, channels, a bot.

Hand-written like the v2 handler tests' fakes, for the same reason: the handlers only
touch a handful of attributes, and a fake that shows exactly which ones is worth more
than a mock that accepts anything. `FakeInteraction.sent` is every message the handler
produced, in order, whether it went through the initial response or a followup.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import discord

from newsbot.config import LoungeCfg, _RawConfig, load_config
from newsbot.store import repo
from newsbot.store.db import connect

CONFIG_V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
CONFIG_V3_INDUSTRY = Path(__file__).parent / "fixtures" / "config_v3_industry.yaml"
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


def load_v3_industry_config(monkeypatch, **overrides):
    """`load_v3_config`, with the Gaming industry entry added last."""
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = load_config(CONFIG_V3_INDUSTRY)
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
        self.guild = None  # the resolved server; a test that cares sets one with get_channel
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


def fake_bot(db_path, *, owner: bool = False, **extra):
    """A bot the admin commands can run against: in every server, with `.owner` saying who asks.

    `in_servers` (a set of ids) narrows which servers it is in; by default it's in all of them.
    Flip `bot.owner` mid-test to make the next caller the bot owner.
    """
    in_servers = extra.pop("in_servers", None)

    async def is_owner(user) -> bool:
        return bot.owner

    def get_guild(guild_id):
        return object() if in_servers is None or guild_id in in_servers else None

    bot = SimpleNamespace(
        db_path=db_path, get_guild=get_guild, is_owner=is_owner, owner=owner, **extra
    )
    return bot


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


@dataclass(frozen=True)
class EditCall:
    """One `edit` the fake editor was asked for, with where it was asked."""

    guild_id: int
    channel_id: int
    message_id: int
    content: str
    # The ping choice the edit carried (what becomes its `allowed_mentions`).
    ping: str = "none"


class FakeEditors:
    """The `FanoutDeps.editor_for` of the edit-pass tests: an editor per channel, one shared log.

    Call it as the factory (`fake(guild_id, channel_id)`); every editor it hands out records into
    `.calls`. `.fail` scripts errors per message id (a list, raised one per call, in order), and
    `.on_edit` runs before each call, which is where a test moves its injected clock. A call
    records the ping choice it was handed (`EditCall.ping`); what that turns into as
    `allowed_mentions` is the real editor's business, and its own tests check it.
    `.calls` holds the edits that succeeded; `.attempts` every try, failed ones included.
    """

    def __init__(self) -> None:
        self.calls: list[EditCall] = []
        self.attempts: list[EditCall] = []
        self.fail: dict[int, list[Exception]] = {}
        self.on_edit: Callable[[EditCall], None] | None = None

    def __call__(self, guild_id: int, channel_id: int) -> _FakeEditor:
        return _FakeEditor(self, guild_id, channel_id)

    def contents(self, message_id: int, guild_id: int | None = None) -> list[str]:
        return [
            c.content
            for c in self.calls
            if c.message_id == message_id and guild_id in (None, c.guild_id)
        ]


class _FakeEditor:
    def __init__(self, owner: FakeEditors, guild_id: int, channel_id: int) -> None:
        self._owner = owner
        self._guild_id = guild_id
        self._channel_id = channel_id

    async def edit(self, message_id: int, content: str, *, ping: str = "none") -> None:
        call = EditCall(self._guild_id, self._channel_id, message_id, content, ping)
        self._owner.attempts.append(call)
        if self._owner.on_edit is not None:
            self._owner.on_edit(call)
        script = self._owner.fail.get(message_id)
        if script:
            raise script.pop(0)
        self._owner.calls.append(call)
