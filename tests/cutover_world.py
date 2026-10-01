"""A v2.2 production day, rebuilt in a temp directory, for the cutover adversarial tests.

The v3 cutover is the one deploy where the friend's server flips from a bot they trust to a
bot that looks the same and runs on completely different plumbing. The unit tests for each
piece are green; the question that matters is whether the pieces, wired the way
`python -m newsbot` wires them, leave that one server's morning exactly as it was. So this
module builds that morning: the populated v2.2 database from `conftest.py`, the prod-like
config, the same startup steps `__main__` runs (import, adopt, re-sync, read the lounge rows),
a real `NewsBot` with a real `setup_hook`, and fake channels standing in for Discord. The only
things that aren't real are the network (any request fails the test) and the model (a
scripted fake that cites the first item it's shown).

It lives next to the tests rather than in `conftest.py` because only the cutover files want
it, and a shared fixture file is the wrong place to keep a small private universe.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
import httpx
import pytest
from pydantic import SecretStr

import newsbot.__main__ as entrypoint
import newsbot.bot.client as client_module
from newsbot.bot.client import NewsBot, build_intents_for_lounges
from newsbot.config import Secrets, load_config
from newsbot.guilds.importer import ImportReport, ensure_imported
from newsbot.pipeline.run import build_fixture_collectors
from newsbot.pipeline.summarize import LLMFatalError, LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import StoredItem

FIXTURES = Path(__file__).parent / "fixtures"
PRODLIKE = FIXTURES / "config_v2_prodlike.yaml"

FRIEND = 100000000000000001
OWNER_GUILD = 200000000000000001
BL4_CH = 1452017235274240221
PAL_CH = 1542581309845799013
D4_CH = 1531681353681211524
SHIFT_CH = 1553597251933438122
FRIEND_ADMIN_CH = 100000000000000002
LOUNGE_CH = 1401806745898061826
OWNER_CH = 200000000000000002

# 2026-10-01 is a Thursday in PDT (UTC-7): 09:00 Pacific is 16:00 UTC.
DUE = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
TODAY = "2026-10-01"

_next_message_id = [9000]


def secrets(*, brave: str | None = None) -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=SecretStr(brave) if brave else None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


class Chan:
    """A channel that remembers everything sent to it, mentions policy included."""

    def __init__(
        self, channel_id: int, guild_id: int, *, permissions: discord.Permissions | None = None
    ) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(
            id=guild_id, me="the-bot", name="a server", get_role=lambda role_id: None
        )
        self.sent: list[SimpleNamespace] = []
        # Set to an exception and every send raises it (a deleted channel, a revoked role).
        self.fail_with: Exception | None = None
        self._permissions = permissions if permissions is not None else discord.Permissions.all()

    def permissions_for(self, member) -> discord.Permissions:
        return self._permissions

    async def send(self, content=None, *, embed=None, allowed_mentions=None, nonce=None):
        if self.fail_with is not None:
            raise self.fail_with
        _next_message_id[0] += 1
        self.sent.append(
            SimpleNamespace(
                content=content,
                embed=embed,
                allowed_mentions=allowed_mentions,
                nonce=nonce,
                channel_id=self.id,
                id=_next_message_id[0],
            )
        )
        return SimpleNamespace(id=_next_message_id[0])


class ScriptedLLM:
    """Cites the first item it's shown, once per game; or fails, when told to.

    `calls` is every user prompt, so a test can count Claude calls and read which items each
    one was handed.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.systems: list[str] = []
        self.fail = False

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        self.calls.append(user)
        self.systems.append(system)
        if self.fail:
            raise LLMFatalError("the model is having a day")
        key = re.search(r"\(key: (\w+)\)", user).group(1)
        body = user.split("<items>\n", 1)[1].split("\n</items>", 1)[0]
        items = json.loads(body)
        stories = []
        if items:
            stories.append(
                StoryOut(
                    headline=f"{key} headline for {items[0]['n']}",
                    summary="A summary the scripted model wrote.",
                    label="official",
                    item_urls=[items[0]["url"]],
                    relevant=True,
                )
            )
        return LLMResult(stories=StoriesOut(stories=stories), input_tokens=10, output_tokens=5)

    def urls_seen(self) -> set[str]:
        seen: set[str] = set()
        for user in self.calls:
            body = user.split("<items>\n", 1)[1].split("\n</items>", 1)[0]
            seen.update(item["url"] for item in json.loads(body))
        return seen


@dataclass
class World:
    """One bot, one database, one clock and a handful of fake channels."""

    bot: NewsBot
    db_path: str
    cfg: object
    clock: dict
    channels: dict[int, Chan]
    llm: ScriptedLLM
    report: ImportReport | None
    tree_syncs: list = field(default_factory=list)

    def set_now(self, when: datetime) -> None:
        self.clock["now"] = when

    def sent(self, channel_id: int) -> list[SimpleNamespace]:
        return self.channels[channel_id].sent

    def everything_sent(self) -> list[SimpleNamespace]:
        return [m for chan in self.channels.values() for m in chan.sent]

    def add_channel(self, channel_id: int, guild_id: int, **kwargs) -> Chan:
        chan = Chan(channel_id, guild_id, **kwargs)
        self.channels[channel_id] = chan
        return chan

    def rows(self, sql: str, *params):
        with closing(connect(self.db_path)) as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]

    def run(self, sql: str, *params) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(sql, params)

    async def tick(self, when: datetime | None = None) -> None:
        if when is not None:
            self.set_now(when)
        await self.bot._digest_job()

    async def ready(self) -> None:
        await self.bot.on_ready()

    async def close(self) -> None:
        if self.bot.scheduler is not None:
            self.bot.scheduler.shutdown(wait=False)
        if self.bot.http_client is not None:
            await self.bot.http_client.aclose()


def fake_gateway(bot: NewsBot, monkeypatch, guilds: list) -> None:
    """Make `bot` look connected to `guilds` (SimpleNamespaces with an `id`)."""
    by_id = {g.id: g for g in guilds}
    monkeypatch.setattr(NewsBot, "guilds", property(lambda self: list(guilds)))
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_guild", lambda gid: by_id.get(gid))


def guild_stub(guild_id: int, *, name: str = "a server", channels=()) -> SimpleNamespace:
    """A guild the bot can talk in: one writable text channel unless told otherwise."""
    me = "the-bot"
    chans = list(channels)
    if not chans:
        chans = [Chan(guild_id * 10 + 1, guild_id)]
    for position, chan in enumerate(chans):
        chan.position = position
    return SimpleNamespace(
        id=guild_id,
        name=name,
        me=me,
        system_channel=None,
        text_channels=chans,
        chunked=True,
        unavailable=False,
    )


def add_item(
    db_path: str,
    game: str,
    slug: str,
    collected_at: datetime,
    *,
    trust: str = "official",
    title: str | None = None,
    url: str | None = None,
) -> str:
    """Store one item for `game` as if a collection pass found it at `collected_at`."""
    url = url or f"https://example.com/{game}/{slug}"
    with closing(connect(db_path)) as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url=url,
                    title=title or f"{game} {slug}",
                    excerpt="An excerpt about it.",
                    source_name="Feed",
                    trust=trust,
                    published_at=collected_at - timedelta(minutes=5),
                    topics={game: False},
                )
            ],
            now=lambda: collected_at,
        )
    return url


def v22_digest(
    db_path: str,
    run_date: str,
    status: str,
    *,
    posted_ids: str = "[]",
    notes: str | None = None,
    updated_at: str | None = None,
) -> None:
    """A digest row exactly as v2.2 writes it: no server, no window, an id list."""
    stamp = updated_at or f"{run_date}T16:00:40+00:00"
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, error_notes, "
            "input_tokens, output_tokens, created_at, updated_at) "
            "VALUES (NULL, ?, ?, ?, ?, 0, 0, ?, ?)",
            (run_date, status, posted_ids, notes, stamp, stamp),
        )


def write_fixture_items(directory: Path, name: str, items: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(items))
    return directory


@pytest.fixture
async def make_world(v22_db, tmp_path, monkeypatch):
    """Factory: `world = await make_world(now=...)` builds the cutover morning at `now`.

    Pass `channels=old_world.channels` to "restart the process" over the same database and the
    same fake Discord (the import is a no-op the second time, as it is in real life).
    """
    built: list[World] = []

    async def build(
        *,
        now: datetime,
        config_path: Path = PRODLIKE,
        cfg=None,
        owner_channel: bool = False,
        lounge_resync: bool = True,
        channels: dict[int, Chan] | None = None,
        brave_key: str | None = None,
    ) -> World:
        cfg = cfg if cfg is not None else load_config(config_path)
        if owner_channel:
            # The friend keeps their own `admin_channel_id` (the import has already copied it
            # into their row); the owner's alerts get their own key, in the home server.
            cfg = cfg.model_copy(
                update={"owner_channel_id": OWNER_CH, "home_guild_id": OWNER_GUILD}
            )
        db_path = str(v22_db)
        clock = {"now": now}
        monkeypatch.setattr(client_module, "_utcnow", lambda: clock["now"])

        # Exactly what `__main__.main` does before it constructs the bot.
        report = ensure_imported(db_path, cfg, lambda: clock["now"])
        entrypoint._adopt_orphan_digests(db_path)
        if lounge_resync:
            entrypoint._resync_lounge(db_path, cfg)
        with closing(connect(db_path)) as conn:
            lounges = repo.list_lounges(conn)
        bot = NewsBot(
            cfg,
            secrets(brave=brave_key),
            db_path,
            lounges=lounges,
            intents=build_intents_for_lounges(lounges),
            import_report=report,
        )
        if channels is None:
            channels = {
                BL4_CH: Chan(BL4_CH, FRIEND),
                PAL_CH: Chan(PAL_CH, FRIEND),
                D4_CH: Chan(D4_CH, FRIEND),
                SHIFT_CH: Chan(SHIFT_CH, FRIEND),
                FRIEND_ADMIN_CH: Chan(FRIEND_ADMIN_CH, FRIEND),
                LOUNGE_CH: Chan(LOUNGE_CH, FRIEND),
            }
            if owner_channel:
                channels[OWNER_CH] = Chan(OWNER_CH, OWNER_GUILD)
        monkeypatch.setattr(bot, "get_channel", lambda cid: channels.get(cid))

        async def no_such_channel(channel_id):
            raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Channel")

        monkeypatch.setattr(bot, "fetch_channel", no_such_channel)

        world = World(bot, db_path, cfg, clock, channels, ScriptedLLM(), report)

        async def fake_sync(*args, **kwargs):
            world.tree_syncs.append(kwargs.get("guild"))
            return []

        bot.tree.sync = fake_sync  # type: ignore[method-assign]
        await bot.setup_hook()
        await bot.http_client.aclose()

        def refuse(request: httpx.Request) -> httpx.Response:
            pytest.fail(f"unexpected HTTP request to {request.url}")

        bot.http_client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        bot.llm = world.llm
        bot._wire()
        built.append(world)
        return world

    yield build

    for world in built:
        await world.close()


def fixture_collectors(directory: Path):
    """What a collection pass should use instead of the network."""
    return lambda cfg, secrets_: build_fixture_collectors(directory)


def code_item(
    slug: str,
    code: str,
    *,
    trust: str = "official",
    source: str = "Gearbox Blog",
    title: str | None = None,
):
    return {
        "url": f"https://example.com/bl4/{slug}",
        "title": title or f"Borderlands 4 SHiFT code {slug}",
        "excerpt": "A code for you.",
        "source_name": source,
        "trust": trust,
        "published_at": "2026-10-01T14:00:00+00:00",
        "topics": ["borderlands4"],
        "full_text": f"Redeem {code} before it expires.",
    }


def collect_these(monkeypatch, tmp_path, items, name="pass"):
    """Make the bot's next collection pass read `items` (a list of fixture dicts)."""
    directory = write_fixture_items(tmp_path / name, "borderlands4", items)
    monkeypatch.setattr(client_module, "build_collection_collectors", fixture_collectors(directory))
