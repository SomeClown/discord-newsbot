"""Adversarial round on edits in place (plan task 8; design.md §16).

The happy paths live in `test_shift_edits.py` and `test_code_alert_editor.py`. This file is the
other half: it leans on the edit pass until something gives. Four themes:

- **No ping, ever.** An edit of an `@everyone` alert must not re-notify anyone, whichever
  road the edit took, and text scraped from a table must stay defused in the edit.
- **Correctness under pressure.** Same codes in the same order, the right server's copy, the
  stamp written is the stamp rendered even when the details move mid-edit, two passes at once.
- **The budget.** The pass shares the delivery walk's hard stop; 500 due messages across 50
  servers drain in a bounded number of passes; a poisoned head of the queue is the one thing
  that doesn't (the old ones were strict xfails; they are plain tests now that the code is fixed).
- **Everything that can end an edit**: gone, forbidden, blocked-then-saved, removed-then-rejoined.

Real temp SQLite with the real migrations, an injected clock (wall and monotonic), fake
Discord objects where the real `DiscordCodeAlertEditor` is under test. No network. The tests
that used to be `xfail(strict=True)` (real defects in the application code, found by this round)
are plain tests now that the code is fixed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import time
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
import pytest
from test_code_alert_editor import _error
from test_shift_delivery_details import (
    BIG_RESERVE,
    GEARBOX,
    MENTALMARS,
    store_details,
    with_details,
)
from test_shift_edits import EditHarness, blocked_at, learn, message_state, table_row
from test_shift_fanout import CODE_A, CODE_B, CODE_C, NOW, ROLE, V3, add_guild, item, rows, seed

from newsbot.bot import client as client_module
from newsbot.bot.client import DiscordCodeAlertEditor
from newsbot.bot.format import render_alert_edit
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import edits as edits_module
from newsbot.shift import fanout
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.details import DetailObservation
from newsbot.shift.edits import edit_changed_alerts
from newsbot.shift.fanout import deliver_queued_codes, detect_and_fan_out
from newsbot.shift.sweep import EditForbiddenError, MessageGoneError
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import DETAILS_STAMP_FLOOR

HOUR = timedelta(hours=1)
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def cfg():
    return load_config(V3)


@pytest.fixture
def h(db_path, cfg):
    harness = EditHarness(db_path, cfg, NOW)
    seed(db_path)
    return harness


# --- fakes that look like Discord, for the tests that need the real editor ---


class _WorldChannel:
    """A channel that records edits and fails scripted message ids, one scripted error per try."""

    def __init__(self, guild_id: int) -> None:
        self.guild = type("G", (), {"id": guild_id})()
        self.edits: list[tuple[int, dict]] = []
        self.scripts: dict[int, list[Exception]] = {}

    def get_partial_message(self, message_id: int):
        channel = self

        class _Partial:
            async def edit(self, **kwargs) -> None:
                script = channel.scripts.get(message_id)
                if script:
                    raise script.pop(0)
                channel.edits.append((message_id, kwargs))

        return _Partial()


class World:
    """A tiny Discord: channels by id. A channel that isn't here was deleted (10003 on fetch)."""

    def __init__(self) -> None:
        self.channels: dict[int, _WorldChannel] = {}
        self.fetches = 0

    def channel(self, channel_id: int, guild_id: int) -> _WorldChannel:
        self.channels[channel_id] = _WorldChannel(guild_id)
        return self.channels[channel_id]

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        self.fetches += 1
        raise _error(discord.NotFound, 404, 10003, "Unknown Channel")

    def editors(self):
        return lambda guild_id, channel_id: DiscordCodeAlertEditor(
            self, channel_id, guild_id=guild_id
        )


def all_mentions_off(kwargs: dict) -> bool:
    mentions = kwargs["allowed_mentions"]
    return not (mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)


# --- bulk setup, for the budget tests (hundreds of messages are slow to deliver for real) ---


def _code(n: int) -> str:
    return f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA"  # 29 characters, like a real one


def bulk_world(db_path, per_guild: dict[int, int], *, reward: str = "3 Golden Keys") -> dict:
    """Servers with `per_guild[g]` live alert messages each, every one due an edit.

    One code per message, so the message count is the code count. Message ids are
    `guild * 100_000 + i` (oldest first), the channel is the usual `guild * 100`, and the
    details were learned an hour after NOW, so every message's stamp (the epoch) is behind.
    Returns {guild_id: [message ids]}.
    """
    ids: dict[int, list[int]] = {}
    observations = []
    n = 0
    for guild_id in per_guild:
        add_guild(db_path, guild_id, ping="none")
    with closing(connect(db_path)) as conn, conn:
        for guild_id, count in per_guild.items():
            ids[guild_id] = []
            for i in range(count):
                n += 1
                code, message_id = _code(n), guild_id * 100_000 + i + 1
                posted = (NOW - timedelta(days=1) + timedelta(seconds=n)).isoformat()
                conn.execute(
                    "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
                    "message_id, pinged, status) VALUES (?, ?, ?, ?, ?, 0, 'posted')",
                    (code, posted, GEARBOX, "https://example.com/x", message_id),
                )
                conn.execute(
                    "INSERT INTO guild_code_posts (guild_id, code, status, message_id, pinged, "
                    "from_roundup, golden, trusted, claimed_at) "
                    "VALUES (?, ?, 'posted', ?, 0, 0, 0, 1, ?)",
                    (guild_id, code, message_id, posted),
                )
                # The stored header is v3.1's, so edits of these go through the classic layout.
                conn.execute(
                    "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
                    "golden_prefix, posted_at, rendered_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (
                        guild_id,
                        message_id,
                        guild_id * 100,
                        "**New SHiFT code**",
                        posted,
                        DETAILS_STAMP_FLOOR.isoformat(),
                    ),
                )
                ids[guild_id].append(message_id)
                observations.append(
                    DetailObservation(
                        code,
                        ItemDetails(code=code, rank="table", reward=reward, listed=True),
                        MENTALMARS,
                        None,
                    )
                )
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(conn, observations, now=NOW + HOUR)
    return ids


async def one_pass(h, deps, *, budget: float = 100.0):
    """One edit pass on a fresh monotonic clock, like the next hourly hook."""
    h.mono = 0.0
    return await edit_changed_alerts(deps, deadline=budget)


def edited_per_guild(h) -> dict[int, int]:
    counts: dict[int, int] = {}
    for call in h.editors.calls:
        counts[call.guild_id] = counts.get(call.guild_id, 0) + 1
    return counts


# =====================================================================================
# No ping, ever
# =====================================================================================


async def test_every_edit_path_repeats_the_mention_the_message_had_and_never_adds_one(h):
    world = World()
    for guild_id, ping in ((1, "everyone"), (2, ROLE), (3, "none")):
        add_guild(h.db_path, guild_id, ping=ping)
        world.channel(guild_id * 100, guild_id)
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    # Server 1's first try is a 429, so its edit takes the retry road as well as the plain one.
    world.channels[100].scripts[9001] = [
        _error(discord.HTTPException, 429, 0, "slow down", retry_after=1.0)
    ]
    h.editors = world.editors()

    h.clock = NOW + 2 * HOUR
    await h.run([])

    sent = [(cid, mid, kw) for cid, ch in sorted(world.channels.items()) for mid, kw in ch.edits]
    assert [cid for cid, _mid, _kw in sent] == [100, 200, 300]
    # QA batch 1: Discord rebuilds a message's mentions from the edit, so an edit sends the
    # choice the message went out with (the highlight stays); a message that had none gets none.
    by_channel = {cid: kw for cid, _mid, kw in sent}
    assert by_channel[100]["allowed_mentions"] is client_module._PING_EVERYONE
    role_mentions = by_channel[200]["allowed_mentions"]
    assert not role_mentions.everyone and not role_mentions.users
    assert [r.id for r in role_mentions.roles] == [int(ROLE)]
    assert all_mentions_off(by_channel[300])
    contents = {cid: kw["content"] for cid, _mid, kw in sent}
    assert contents[100].startswith("@everyone `> new SHiFT code ▌`")
    assert contents[200].startswith(f"<@&{ROLE}> `> new SHiFT code ▌`")
    assert contents[300].startswith("`> new SHiFT code ▌`")


def _calls_named_edit(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "edit"
    ]


def test_the_editor_source_has_exactly_one_edit_call_and_its_mentions_come_from_mentions_for():
    from newsbot.bot import client

    tree = ast.parse(inspect.getsource(client.DiscordCodeAlertEditor))
    # The call is `get_partial(message_id).edit(...)`, and its own `def edit` is not a call.
    calls = _calls_named_edit(tree)
    assert len(calls) == 1
    (call,) = calls
    mentions = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    # The one place a ping choice becomes an `AllowedMentions` (and the only `everyone=True`,
    # held by tests/test_mentions_tripwire.py), not a mention built on the spot.
    assert mentions["allowed_mentions"] == "mentions_for(ping)"
    # And nothing in the class (or the edit pass) builds an AllowedMentions of its own.
    # (Docstrings may talk about `everyone=True`; the code may not do it, so read the syntax.)
    for source in (
        inspect.getsource(client.DiscordCodeAlertEditor),
        inspect.getsource(edits_module),
    ):
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                assert not ast.unparse(node.func).endswith("AllowedMentions")
            if isinstance(node, ast.keyword) and node.arg == "everyone":
                assert ast.unparse(node.value) != "True"


@pytest.mark.parametrize("ping", ["everyone", ROLE])
async def test_an_alert_whose_ping_was_stripped_is_edited_without_the_ping(h, ping):
    add_guild(h.db_path, 1, ping=ping)
    # A timeout is ambiguous (the message may have landed and pinged), so the retry goes out
    # with the ping stripped; that retry is the message that actually posted.
    h.fail_with[1] = [PublishError("timed out")]
    await h.run([item(CODE_A)])
    (alert,) = h.sent(1)
    assert alert.ping is False and not alert.content.startswith(("@everyone", "<@&"))
    stored = message_state(h.db_path)[0]
    assert stored[1] == 9001
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert call.content.startswith("`> new SHiFT code ▌`")
    assert "@everyone" not in call.content and "<@&" not in call.content


async def test_a_ping_that_was_refused_and_retried_stays_in_the_header_as_text(h):
    add_guild(h.db_path, 1, ping="everyone")
    # A 429 is a refusal: nothing landed, nobody was pinged, the retry keeps the ping.
    h.fail_with[1] = [PublishError("slow down", rejected=True, retry_after=1.0)]
    await h.run([item(CODE_A)])
    assert h.sent(1)[0].content.startswith("@everyone ")
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert h.editors.calls[0].content.startswith("@everyone `> new SHiFT code ▌`")


HOSTILE_REWARD = (
    "@everyone @here <@123456789012345678> <@&1234567890123456789> <#99> </shift:1> "
    "https://evil.example/x discord.gg/abc [click](http://x) `tick` **bold**"
)


@pytest.mark.parametrize(
    ("reward", "name"),
    [
        (HOSTILE_REWARD, MENTALMARS),
        ("1 Golden Key", "@everyone <@&1234567890123456789> mentalmars"),
    ],
)
async def test_text_scraped_from_a_table_stays_defused_in_the_edit(h, reward, name):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    store_details(h.db_path, CODE_A, reward=reward, name=name, now=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert "@everyone" not in call.content and "@here" not in call.content
    # No live mention, channel link, command link or scheme survives; each got a zero-width break.
    shown = call.content.replace("<https://example.com/AAAA>", "")
    for live in (r"<@[&!]?\d", r"<#\d", r"</[\w -]+:\d", r"://", r"discord\.gg/"):
        assert re.search(live, shown) is None, live
    assert "\u200b" in call.content  # the breaks are there; the text still reads the same


# =====================================================================================
# Correctness
# =====================================================================================


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0), (1, 0, 2)])
@pytest.mark.parametrize("ping", ["everyone", "none"])
async def test_an_edit_is_the_original_message_plus_details_lines_whatever_the_release_order(
    h, order, ping
):
    add_guild(h.db_path, 1, ping=ping)
    h.reserve = BIG_RESERVE  # two to a message
    codes = [CODE_A, CODE_B, CODE_C]
    items = [
        item(
            c,
            url=f"https://e/{c[:1]}",
            title="Borderlands 4 Golden Key SHiFT code: " + c if c == CODE_B else None,
        )
        for c in codes
    ]
    await h.run([items[i] for i in order])
    originals = list(h.sent(1))
    assert len(originals) == 2
    for code in codes:
        learn(h, code, f"reward-{code[:1]}", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert len(h.editors.calls) == len(originals)
    for number, original in enumerate(originals):
        edited = h.editors.contents(9001 + number)[0]
        for code in original.codes:
            edited = edited.replace(f"`reward` reward-{code[:1]} · `via` {MENTALMARS}\n", "")
        assert edited == original.content


async def test_an_alert_is_edited_in_the_channel_and_header_it_posted_with_not_the_servers_new_ones(
    h,
):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn:  # the admin moves the alerts and drops the ping
        repo.set_shift(conn, 1, enabled=True, channel_id=555, ping="none")
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert (call.channel_id, call.message_id) == (100, 9001)
    assert call.content.startswith("@everyone `> new SHiFT code ▌`")


async def test_a_code_can_hold_only_one_message_per_server(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, pytest.raises(Exception, match="UNIQUE"), conn:
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, message_id, claimed_at) "
            "VALUES (1, ?, 'posted', 9999, ?)",
            (CODE_A, NOW.isoformat()),
        )


async def test_a_second_delivery_cannot_move_a_posted_code_to_a_second_message(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    # Only a 'pending' row moves, so a late "posted" for another message id changes nothing.
    with closing(connect(h.db_path)) as conn:
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=7777)
    assert rows(h.db_path, "SELECT message_id FROM guild_code_posts") == [(9001,)]


async def test_details_that_change_mid_edit_are_stamped_as_rendered_and_edited_again(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    def a_newer_reading_lands_mid_edit(_call):
        store_details(h.db_path, CODE_A, reward="9 Golden Keys", now=NOW + 3 * HOUR)

    h.editors.on_edit = a_newer_reading_lands_mid_edit
    h.clock = NOW + 2 * HOUR
    await h.run([])

    # The message shows 3 keys, so its stamp is the one that rendered 3 keys, never the newer.
    assert "3 Golden Keys" in h.editors.calls[0].content
    assert datetime.fromisoformat(message_state(h.db_path)[0][3]) == NOW + HOUR

    h.editors.on_edit = None
    h.clock = NOW + 4 * HOUR
    await h.run([])
    assert "9 Golden Keys" in h.editors.calls[1].content
    assert datetime.fromisoformat(message_state(h.db_path)[0][3]) == NOW + 3 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 2


async def test_a_change_before_a_later_message_is_read_shows_in_that_edit_with_its_own_stamp(h):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    changed = []

    def only_the_first_edit_moves_the_details(call):
        if not changed:
            changed.append(call)
            store_details(h.db_path, CODE_A, reward="9 Golden Keys", now=NOW + 3 * HOUR)

    h.editors.on_edit = only_the_first_edit_moves_the_details
    h.clock = NOW + 4 * HOUR
    await h.run([])

    first, second = h.editors.calls[:2]
    assert (first.guild_id, second.guild_id) == (1, 2)
    assert "3 Golden Keys" in first.content and "9 Golden Keys" in second.content
    stamps = {g: datetime.fromisoformat(r) for g, _m, _s, r in message_state(h.db_path)}
    assert stamps == {1: NOW + HOUR, 2: NOW + 3 * HOUR}
    # Server 1 is behind and gets its second edit next pass; server 2 is current and doesn't.
    h.editors.on_edit = None
    await h.run([])
    assert [c.guild_id for c in h.editors.calls[2:]] == [1]


async def test_a_change_made_while_the_clock_stands_behind_the_last_stamp_still_gets_its_edit(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 1

    # NTP steps the droplet's clock back ten minutes, and the table changes its mind.
    learn(h, CODE_A, "9 Golden Keys", at=NOW + HOUR - timedelta(minutes=10))
    h.clock = NOW + 3 * HOUR
    await h.run([])

    assert len(h.editors.calls) == 2
    assert "9 Golden Keys" in h.editors.calls[1].content


class _Yielding:
    """Wraps an editor factory so every edit yields to the loop a few times first."""

    def __init__(self, factory) -> None:
        self._factory = factory

    def __call__(self, guild_id: int, channel_id: int):
        inner = self._factory(guild_id, channel_id)

        class _Slow:
            async def edit(self, message_id: int, content: str, *, ping: str = "none") -> None:
                for _ in range(5):
                    await asyncio.sleep(0)
                await inner.edit(message_id, content, ping=ping)

        return _Slow()


async def test_two_overlapping_edit_passes_edit_each_message_once(h):
    for guild_id in (1, 2, 3):
        add_guild(h.db_path, guild_id, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    deps = replace(h.deps(), editor_for=_Yielding(h.editors))
    h.clock = NOW + 2 * HOUR

    first, second = await asyncio.gather(
        edit_changed_alerts(deps, deadline=100.0), edit_changed_alerts(deps, deadline=100.0)
    )

    assert sorted(c.guild_id for c in h.editors.attempts) == [1, 2, 3]
    assert (first.edited, second.edited) in {(3, 0), (0, 3)}


async def test_overlapping_hooks_edit_each_message_once_and_post_each_alert_once(h):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    deps = replace(h.deps(), editor_for=_Yielding(h.editors))
    h.clock = NOW + 2 * HOUR
    new_alert = [item(CODE_B, url="https://example.com/b")]

    await asyncio.gather(
        detect_and_fan_out(deps, new_alert, seeding_ok=True),
        detect_and_fan_out(deps, new_alert, seeding_ok=True),
    )

    assert sorted((c.guild_id, c.message_id) for c in h.editors.attempts) == [(1, 9001), (2, 9001)]
    posted = rows(
        h.db_path,
        "SELECT guild_id, count(*) FROM guild_code_posts WHERE code = ? "
        "AND status = 'posted' GROUP BY guild_id",
        CODE_B,
    )
    assert posted == [(1, 1), (2, 1)]


async def test_an_edit_pass_waits_for_a_delivery_walk_in_progress(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    deps = h.deps()
    h.clock = NOW + 2 * HOUR

    async with deps.delivery_lock:  # a delivery (or startup's recovery) holds the walk
        task = asyncio.create_task(edit_changed_alerts(deps, deadline=100.0))
        for _ in range(20):
            await asyncio.sleep(0)
        assert h.editors.attempts == [] and not task.done()
    outcome = await task

    assert outcome.edited == 1


async def test_a_delivery_to_a_server_is_never_edited_before_it_is_recorded(h):
    """A code released this pass is delivered first; its alert is edited only on a later pass."""
    add_guild(h.db_path, 1, ping="none")
    # The details exist before the alert does (a table listed it in the same pass).
    await h.run([item(CODE_A), table_row(CODE_A, "3 Golden Keys")])

    assert h.editors.attempts == []  # the first render already had the line
    assert "`reward` 3 Golden Keys · `via`" in h.sent(1)[0].content
    assert datetime.fromisoformat(message_state(h.db_path)[0][3]) == NOW


# =====================================================================================
# The 14-day window
# =====================================================================================


def _post_at(h, posted_at: datetime) -> None:
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET posted_at = ?", (posted_at.isoformat(),))


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(0), True),
        (timedelta(microseconds=-1), True),
        (timedelta(microseconds=1), False),
        (timedelta(days=1), False),
        (timedelta(days=-1), True),
    ],
)
async def test_the_window_is_exactly_fourteen_days_and_includes_its_edge(h, delta, expected):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW - HOUR)
    # Posted exactly 14 days before `now`, then nudged by `delta` (more = older).
    _post_at(h, NOW - timedelta(days=14) - delta)

    with closing(connect(h.db_path)) as conn:
        found = repo.alert_messages_to_edit(conn, now=NOW)

    assert bool(found) is expected


async def test_the_edge_holds_through_the_full_pass_too(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW - HOUR)
    _post_at(h, NOW - timedelta(days=14))

    await h.run([])

    assert len(h.editors.calls) == 1


@pytest.mark.parametrize(
    "zone",
    ["UTC", "America/Los_Angeles", "Pacific/Kiritimati", "Pacific/Pago_Pago", "Asia/Kolkata"],
)
async def test_the_window_means_the_same_instant_whatever_zone_the_clock_speaks(h, zone):
    tz = ZoneInfo(zone)
    h.clock = NOW.astimezone(tz)
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    # Written as UTC text whatever zone the clock was in, or the text compare would lie.
    posted = message_state(h.db_path) and rows(
        h.db_path, "SELECT posted_at FROM guild_alert_messages"
    )
    assert posted == [(NOW.isoformat(),)]
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    with closing(connect(h.db_path)) as conn:
        edge = NOW + timedelta(days=14)
        assert repo.alert_messages_to_edit(conn, now=edge.astimezone(tz)) != []
        assert (
            repo.alert_messages_to_edit(conn, now=(edge + timedelta(microseconds=1)).astimezone(tz))
            == []
        )


async def test_a_naive_now_is_read_as_utc(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    with closing(connect(h.db_path)) as conn:
        edge = (NOW + timedelta(days=14)).replace(tzinfo=None)
        assert repo.alert_messages_to_edit(conn, now=edge) != []
        assert repo.alert_messages_to_edit(conn, now=edge + timedelta(microseconds=1)) == []


@pytest.mark.parametrize(
    "posted",
    [
        datetime(2026, 10, 20, 15, 0, tzinfo=UTC),  # 08:00 PDT; fall-back (Nov 1) in between
        datetime(2027, 2, 28, 18, 0, tzinfo=UTC),  # 10:00 PST; spring-forward (Mar 14) in between
    ],
)
async def test_fourteen_days_is_336_elapsed_hours_even_across_a_daylight_saving_change(h, posted):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW - HOUR)
    _post_at(h, posted)
    edge = posted + timedelta(hours=336)

    with closing(connect(h.db_path)) as conn:
        assert repo.alert_messages_to_edit(conn, now=edge) != []
        assert repo.alert_messages_to_edit(conn, now=edge + timedelta(microseconds=1)) == []


async def test_the_cutoff_is_elapsed_time_when_the_clock_speaks_a_dst_zone(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW - HOUR)
    posted = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    _post_at(h, posted)
    now_in_la = (posted + timedelta(days=14, minutes=30)).astimezone(LA)  # 07:30 PST, Nov 3

    with closing(connect(h.db_path)) as conn:
        assert repo.alert_messages_to_edit(conn, now=now_in_la) == []


# =====================================================================================
# Blocking
# =====================================================================================


async def test_two_blocked_servers_get_one_notice_each_and_a_third_is_untouched(h):
    for guild_id in (1, 2, 3):
        add_guild(h.db_path, guild_id, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    original_edit = h.editors.__class__.__call__

    def forbid_servers_one_and_two(guild_id, channel_id):
        editor = original_edit(h.editors, guild_id, channel_id)
        real_edit = editor.edit

        async def edit(message_id, content, *, ping="none"):
            if guild_id in (1, 2):
                raise EditForbiddenError("50013")
            await real_edit(message_id, content, ping=ping)

        editor.edit = edit
        return editor

    deps = replace(h.deps(), editor_for=forbid_servers_one_and_two)
    h.clock = NOW + 2 * HOUR
    for _ in range(3):
        await one_pass(h, deps)

    assert sorted(g for g, _t in h.notices) == [1, 2]
    assert [c.guild_id for c in h.editors.calls] == [3]
    assert (blocked_at(h.db_path, 1) is None, blocked_at(h.db_path, 3) is None) == (False, True)


async def test_a_notice_that_fails_is_not_retried_on_later_passes(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [EditForbiddenError("50013")]
    attempts = []

    async def broken(guild_id, text):
        attempts.append(guild_id)
        raise RuntimeError("the admin channel is gone too")

    h.notify_guild = broken
    h.clock = NOW + 2 * HOUR
    for _ in range(3):
        await h.run([])

    assert attempts == [1]  # blocked once, told once (the telling failed), never retold
    assert len(h.editors.attempts) == 1
    assert blocked_at(h.db_path, 1) is not None


@pytest.mark.parametrize(
    "save",
    [
        dict(enabled=True, channel_id=100, ping="none"),  # identical to what's stored
        dict(enabled=True, channel_id=555, ping="none"),  # the channel moved
        dict(enabled=True, channel_id=100, ping="everyone"),  # the ping changed
    ],
)
async def test_any_save_of_the_shift_settings_unblocks_even_one_that_changes_nothing(h, save):
    add_guild(h.db_path, 1, ping="none")
    with closing(connect(h.db_path)) as conn:
        assert repo.block_guild_edits(conn, 1, NOW) is True
        repo.set_shift(conn, 1, **save)

    assert blocked_at(h.db_path, 1) is None


async def test_the_slash_command_save_with_no_options_unblocks_too(h):
    """`/newsbot shift` with nothing changed is still a save, so it clears the block.

    That is the reading of "until its settings change" this build chose (any save): the thing
    an admin does after fixing the permission is run the command, and it may well change nothing.
    The price is that an admin who runs it for an unrelated reason re-arms edits that still fail,
    and hears one more notice. Pinned here so a change of mind is deliberate.
    """
    from newsbot.bot.commands import _save_shift_sync

    add_guild(h.db_path, 1, ping="none")
    with closing(connect(h.db_path)) as conn:
        repo.block_guild_edits(conn, 1, NOW)

    _save_shift_sync(h.db_path, 1, "free", enabled=None, channel_id=None, ping=None)

    assert blocked_at(h.db_path, 1) is None


async def test_disabling_the_alerts_clears_the_block_and_pauses_edits_until_they_are_back_on(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    with closing(connect(h.db_path)) as conn:
        repo.block_guild_edits(conn, 1, NOW)
        repo.set_shift(conn, 1, enabled=False, channel_id=100, ping="none")

    h.clock = NOW + 2 * HOUR
    await h.run([])

    # QA batch 1, fix 7 reverses this test's old half: a server that switched its SHiFT alerts
    # off isn't edited ("alerts off is a delivery setting" was how it used to read). Saving the
    # settings did clear the block, though, and switching back on gets the edit.
    assert h.editors.attempts == []
    assert blocked_at(h.db_path, 1) is None
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="none")
    h.clock = NOW + 3 * HOUR
    await h.run([])
    assert [c.message_id for c in h.editors.calls] == [9001]


async def test_a_server_removed_while_blocked_and_added_back_starts_clean(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    with closing(connect(h.db_path)) as conn:
        repo.block_guild_edits(conn, 1, NOW)
        repo.delete_guild(conn, 1)
    assert rows(h.db_path, "SELECT count(*) FROM guild_alert_messages") == [(0,)]

    add_guild(h.db_path, 1, ping="none")  # the bot is invited back

    assert blocked_at(h.db_path, 1) is None
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert h.editors.attempts == []  # nothing of the old server's messages survived to edit
    with closing(connect(h.db_path)) as conn:
        assert repo.block_guild_edits(conn, 1, NOW) is True  # and it can be blocked afresh


async def test_a_server_removed_in_the_middle_of_its_forbidden_edit_gets_no_notice(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    def kicked_while_editing(_call):
        with closing(connect(h.db_path)) as conn:
            repo.delete_guild(conn, 1)

    h.editors.on_edit = kicked_while_editing
    h.editors.fail[9001] = [EditForbiddenError("50001")]
    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert h.notices == []  # block_guild_edits found no server to block, so nobody speaks


async def test_a_blocked_servers_new_alerts_still_post_and_are_edited_once_it_is_unblocked(h):
    add_guild(h.db_path, 1, ping="none")
    with closing(connect(h.db_path)) as conn:
        repo.block_guild_edits(conn, 1, NOW)

    await h.run([item(CODE_A)])
    assert [a.codes for a in h.sent(1)] == [[CODE_A]]  # delivery isn't edits
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert h.editors.attempts == []

    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="none")
    h.clock = NOW + 3 * HOUR
    await h.run([])
    assert [c.message_id for c in h.editors.calls] == [9001]


# =====================================================================================
# Gone
# =====================================================================================


async def test_a_deleted_message_does_not_take_its_neighbours_with_it(h):
    add_guild(h.db_path, 1, ping="none")
    h.reserve = BIG_RESERVE
    await h.run(
        [
            item(CODE_A, url="https://e/a"),
            item(CODE_B, url="https://e/b"),
            item(CODE_C, url="https://e/c"),
        ]
    )
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    learn(h, CODE_C, "A skin", at=NOW + HOUR)
    h.editors.fail[9001] = [MessageGoneError("10008")]

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert [(m, s) for _g, m, s, _r in message_state(h.db_path)] == [(9001, "gone"), (9002, "live")]
    assert [c.message_id for c in h.editors.calls] == [9002]
    assert h.notices == []
    assert blocked_at(h.db_path, 1) is None  # a missing message is not a missing permission


async def test_a_deleted_channel_marks_every_message_in_it_gone_in_the_same_pass(h):
    """10003: one pass finishes them all, and the cost is one lookup for the channel."""
    world = World()  # no channel 100: it was deleted
    h.editors = world.editors()
    ids = bulk_world(h.db_path, {1: 5, 2: 1})
    world.channel(200, 2)

    await one_pass(h, h.deps())

    states = rows(
        h.db_path,
        "SELECT guild_id, state, count(*) FROM guild_alert_messages "
        "GROUP BY guild_id, state ORDER BY guild_id",
    )
    assert states == [(1, "gone", 5), (2, "live", 1)]
    assert world.fetches == 1  # one lookup for the dead channel, not one per message
    assert ids[1] and all(mid not in {m for m, _kw in world.channels[200].edits} for mid in ids[1])
    assert [mid for mid, _kw in world.channels[200].edits] == ids[2]
    assert h.notices == []
    # And nothing is left to retry: a second pass has no work.
    outcome = await one_pass(h, h.deps())
    assert outcome.edited == outcome.gone == 0


async def test_a_channel_that_belongs_to_another_server_is_gone_and_never_touched(h):
    world = World()
    foreign = world.channel(100, guild_id=999)  # the id now resolves to somebody else's channel
    h.editors = world.editors()
    bulk_world(h.db_path, {1: 3})

    await one_pass(h, h.deps())

    assert foreign.edits == []
    assert rows(h.db_path, "SELECT state, count(*) FROM guild_alert_messages GROUP BY state") == [
        ("gone", 3)
    ]
    assert h.notices == []


# =====================================================================================
# The budget
# =====================================================================================


class _Hangs:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    def __call__(self, _guild_id, _channel_id):
        return self

    async def edit(self, _message_id, _content, *, ping="none") -> None:
        self.started.set()
        await asyncio.Event().wait()


def _tiny_walk(monkeypatch) -> None:
    monkeypatch.setattr(fanout, "_WALK_STOP_S", 0.2)
    monkeypatch.setattr(fanout, "_WALK_HARD_S", 0.3)
    monkeypatch.setattr(fanout, "_WALK_FLOOR_S", 0.05)


async def test_an_edit_that_never_returns_is_cut_at_the_hard_stop_after_the_deliveries(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    _tiny_walk(monkeypatch)
    hung = _Hangs()
    deps = replace(h.deps(), editor_for=hung, clock=time.monotonic)
    h.clock = NOW + 2 * HOUR

    began = time.monotonic()
    released = await asyncio.wait_for(
        detect_and_fan_out(deps, [item(CODE_B, url="https://example.com/b")], seeding_ok=True), 5
    )

    assert time.monotonic() - began < 2  # the real thing is 115 s; this is its 0.3 s cousin
    assert released == 1
    assert hung.started.is_set()
    assert [a.codes for a in h.sent(1)] == [[CODE_B]]  # the delivery had already gone out
    assert not deps.delivery_lock.locked()  # the walk's lock is not left behind
    # The cut-off edit is still due: a pass with a working editor does it.
    deps = replace(deps, editor_for=h.editors)
    await detect_and_fan_out(deps, [], seeding_ok=True)
    assert [c.message_id for c in h.editors.calls] == [9001]


async def test_a_notice_that_never_returns_costs_this_pass_but_not_the_next(h, monkeypatch):
    for guild_id in (1, 2):
        add_guild(h.db_path, guild_id, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.editors.fail[9001] = [EditForbiddenError("50013")]
    _tiny_walk(monkeypatch)

    async def hangs(_guild_id, _text):
        await asyncio.Event().wait()

    deps = replace(h.deps(), notify_guild=hangs, clock=time.monotonic)
    h.clock = NOW + 2 * HOUR
    await asyncio.wait_for(detect_and_fan_out(deps, [], seeding_ok=True), 5)
    assert blocked_at(h.db_path, 1) is not None  # the block was written before the notice
    assert not deps.delivery_lock.locked()

    await h.run([])  # the next hourly pass, with a notice path that works
    assert [c.guild_id for c in h.editors.calls] == [2]


@pytest.mark.parametrize(
    ("per_guild", "passes"),
    [
        # 500 spread evenly: four rounds of the 50 servers fill a pass, so every server gets
        # four edits in each of the first two passes and the last two in the third.
        ({g: 10 for g in range(1, 51)}, [200, 200, 100]),
        # One server hogs: it is capped at 20 a pass, so the 49 small ones (100 messages) are
        # done in pass one beside its first 20, and it takes 19 more hours to drain.
        ({1: 400, **{g: 2 for g in range(2, 50)}, 50: 4}, [120] + [20] * 19),
    ],
    ids=["spread-evenly", "one-server-with-400"],
)
async def test_500_due_messages_across_50_servers_drain_without_starving_anyone(
    h, per_guild, passes
):
    assert sum(per_guild.values()) == 500 and len(per_guild) == 50
    bulk_world(h.db_path, per_guild)
    deps = h.deps()
    seen: set[int] = set()

    sizes = []
    for _ in range(len(passes) + 2):
        before = len(h.editors.calls)
        outcome = await one_pass(h, deps)
        sizes.append(len(h.editors.calls) - before)
        assert outcome.failed == 0
        if sizes[-1] == 0:
            break
        seen |= {c.guild_id for c in h.editors.calls}
        if len(sizes) == 1:
            assert seen == set(per_guild)  # everyone, in the very first pass

    assert sizes[: len(passes)] == passes and sizes[len(passes)] == 0  # progress, then done
    assert edited_per_guild(h) == per_guild  # and nobody was edited twice
    assert rows(
        h.db_path,
        "SELECT count(*) FROM guild_alert_messages WHERE rendered_at < ?",
        (NOW + HOUR).isoformat(),
    ) == [(0,)]


async def test_a_big_server_does_not_make_the_small_ones_wait(h):
    """Round-robin with a cap of 20 a server: the small servers are all edited in pass one.

    Server 1 holds 400 of the 500 due messages and has the lowest id. It used to fill the first
    two passes (the batch size) and leave the 49 small servers for pass three; now each server
    takes its turn, and server 1 gets 20 of its messages a pass.
    """
    bulk_world(h.db_path, {1: 400, **{g: 2 for g in range(2, 50)}, 50: 4})
    deps = h.deps()

    await one_pass(h, deps)
    assert set(edited_per_guild(h)) == set(range(1, 51))
    assert edited_per_guild(h)[1] == 20
    assert all(n == 2 for g, n in edited_per_guild(h).items() if 1 < g < 50)
    assert edited_per_guild(h)[50] == 4


async def test_no_server_gets_more_than_20_edits_in_one_pass(h):
    bulk_world(h.db_path, {1: 60, 2: 21, 3: 5})

    await one_pass(h, h.deps())

    assert edited_per_guild(h) == {1: 20, 2: 20, 3: 5}


async def test_the_servers_take_turns_in_id_order_oldest_message_first(h):
    ids = bulk_world(h.db_path, {1: 3, 2: 2, 3: 1})

    await one_pass(h, h.deps())

    assert [(c.guild_id, c.message_id) for c in h.editors.calls] == [
        (1, ids[1][0]),
        (2, ids[2][0]),
        (3, ids[3][0]),
        (1, ids[1][1]),
        (2, ids[2][1]),
        (1, ids[1][2]),
    ]


@pytest.mark.parametrize(("due", "passes"), [(199, [199]), (200, [200]), (201, [200, 1])])
async def test_the_batch_limit_is_200_messages_a_pass(h, due, passes):
    bulk_world(h.db_path, {g: 1 for g in range(1, due + 1)})  # one each: the cap isn't in play
    deps = h.deps()

    sizes = []
    for _ in passes:
        before = len(h.editors.calls)
        await one_pass(h, deps)
        sizes.append(len(h.editors.calls) - before)

    assert sizes == passes
    assert (await one_pass(h, deps)).edited == 0


@pytest.mark.parametrize(("cost", "per_pass"), [(30.0, 4), (45.0, 3), (150.0, 1)])
async def test_a_slow_discord_still_makes_progress_every_pass_and_finishes(h, cost, per_pass):
    bulk_world(h.db_path, {g: 1 for g in range(1, 11)})  # ten servers, one message each
    h.editors.on_edit = lambda _call: setattr(h, "mono", h.mono + cost)
    deps = h.deps()

    sizes = []
    while len(h.editors.calls) < 10 and len(sizes) < 12:
        before = len(h.editors.calls)
        await one_pass(h, deps)
        sizes.append(len(h.editors.calls) - before)

    assert all(size >= 1 for size in sizes)
    assert max(sizes) == per_pass and sum(sizes) == 10
    assert len(sizes) == -(-10 // per_pass)  # ceil: the bound the budget implies


async def test_an_edit_may_not_start_once_the_deadline_has_passed_even_with_the_pace_sleep(h):
    bulk_world(h.db_path, {g: 1 for g in range(1, 6)})
    deps = replace(h.deps(), pace_s=30.0)  # the pace itself eats the budget

    outcome = await one_pass(h, deps)

    # Edits start at 0, 30, 60, 90; the one that would start at 120 is past 100.
    assert outcome.edited == 4 and outcome.waiting == 1
    assert max(h.sleeps) == 30.0


async def test_a_forbidden_server_with_a_full_batch_does_not_cost_the_others_anything(h):
    """A blocked server wastes its slots in the pass it's found out, and nobody else's."""
    bulk_world(h.db_path, {1: 201, 2: 3})
    h.editors.fail.update({mid: [EditForbiddenError("50013")] for mid in range(100_001, 100_202)})
    deps = h.deps()

    first = await one_pass(h, deps)
    # Server 1's first message is refused; its other 19 slots are skipped without asking.
    assert (first.edited, first.blocked_guilds, first.waiting) == (3, 1, 19)
    assert edited_per_guild(h) == {2: 3}
    assert len(h.editors.attempts) == 4

    second = await one_pass(h, deps)
    assert second.edited == 0 and len(h.editors.attempts) == 4  # blocked: not even listed
    assert len(h.notices) == 1


async def test_a_server_whose_edits_keep_failing_cannot_starve_the_servers_after_it(h):
    ids = bulk_world(h.db_path, {1: 201, 2: 1})
    h.editors.fail.update({mid: [PublishError("503")] * 10 for mid in ids[1]})
    deps = h.deps()

    for _ in range(5):
        await one_pass(h, deps)

    assert edited_per_guild(h).get(2) == 1


async def test_an_unexpected_editor_error_leaves_the_message_due_and_the_pass_going(h, caplog):
    ids = bulk_world(h.db_path, {1: 2, 2: 1})
    h.editors.fail[ids[1][0]] = [ValueError("a discord.py surprise")]
    deps = h.deps()

    outcome = await one_pass(h, deps)

    # Server 1 stalls for the rest of the pass (its second message isn't even tried); server 2
    # is somebody else's channel and carries on.
    assert (outcome.edited, outcome.failed, outcome.waiting) == (1, 1, 1)
    assert [c.message_id for c in h.editors.calls] == [ids[2][0]]
    # Next pass the failed one has been tried and goes behind its untried neighbour; both land.
    assert (await one_pass(h, deps)).edited == 2
    assert [c.message_id for c in h.editors.calls[1:]] == [ids[1][1], ids[1][0]]


# =====================================================================================
# The edit storm
# =====================================================================================


async def test_a_flapping_reward_across_many_servers_costs_one_edit_per_message_per_pass(h):
    servers = list(range(1, 21))
    for guild_id in servers:
        add_guild(h.db_path, guild_id, ping="none")
    await h.run([item(CODE_A)])

    for n in range(6):
        h.clock = NOW + (n + 1) * HOUR
        before = len(h.editors.calls)
        await h.run([table_row(CODE_A, "1 Golden Key" if n % 2 == 0 else "2 Golden Keys")])
        assert len(h.editors.calls) - before == len(servers)  # one each, never two

    assert len(h.editors.calls) == 6 * len(servers)


def _row_from(name: str, code: str, reward: str):
    return replace(table_row(code, reward), source_name=name)


async def test_a_flapping_via_name_is_one_edit_per_message_per_pass_and_a_steady_one_is_none(h):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    await h.run([item(CODE_A)])
    x, y = "mentalmars", "othersite"

    # Two table sources disagree and take turns being the only one listed: value and name flip.
    for n in range(4):
        h.clock = NOW + (n + 1) * HOUR
        before = len(h.editors.calls)
        await h.run(
            [_row_from(x if n % 2 == 0 else y, CODE_A, "1 Key" if n % 2 == 0 else "2 Keys")]
        )
        assert len(h.editors.calls) - before == 2
    assert f"`via` {y}" in h.editors.calls[-1].content

    # Both listed, in the same order every pass: it settles, and settled means silent.
    both = [_row_from(x, CODE_A, "1 Key"), _row_from(y, CODE_A, "2 Keys")]
    h.clock = NOW + 5 * HOUR
    await h.run(both)
    settled = len(h.editors.calls)
    for n in (6, 7, 8):
        h.clock = NOW + n * HOUR
        await h.run(both)
    assert len(h.editors.calls) == settled


async def test_the_same_agreeing_sources_listed_in_either_order_do_not_flap(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    x, y = "mentalmars", "othersite"
    h.clock = NOW + HOUR
    await h.run([_row_from(x, CODE_A, "3 Golden Keys"), _row_from(y, CODE_A, "3 Golden Keys")])
    settled = len(h.editors.calls)

    h.clock = NOW + 2 * HOUR
    await h.run([_row_from(y, CODE_A, "3 Golden Keys"), _row_from(x, CODE_A, "3 Golden Keys")])

    assert len(h.editors.calls) == settled  # agreeing same-rank sources don't swap the name


# =====================================================================================
# A render that fails
# =====================================================================================


async def test_a_message_that_cannot_be_rendered_does_not_hold_up_the_others_in_the_pass(h):
    add_guild(h.db_path, 1, ping="none")
    add_guild(h.db_path, 2, ping="none")
    codes = [
        f"{n}1-AAAAA-AAAAA-AAAAA-AAAAA"
        for n in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV", "WWWW", "XXXX")
    ]
    await h.run([item(c, url=f"https://example.com/{c[:4]}") for c in codes])  # all eight in one
    add_guild(h.db_path, 3, ping="none", enabled_at=NOW + HOUR)
    for i, code in enumerate(codes):
        learn(h, code, "r" * 300, at=NOW + HOUR + timedelta(seconds=i))

    h.clock = NOW + 2 * HOUR
    outcome = await h.run([])

    assert h.editors.attempts == []  # both servers' fat messages are unrenderable, politely
    assert rows(h.db_path, "SELECT count(*) FROM guild_alert_messages WHERE state = 'live'") == [
        (2,)
    ]
    assert outcome == 0


async def test_a_later_real_change_that_makes_a_failed_message_fit_is_edited(h, caplog):
    add_guild(h.db_path, 1, ping="none")
    codes = [
        f"{n}1-AAAAA-AAAAA-AAAAA-AAAAA"
        for n in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV", "WWWW", "XXXX")
    ]
    await h.run([item(c, url=f"https://example.com/{c[:4]}") for c in codes])
    for i, code in enumerate(codes):
        learn(h, code, "r" * 300, at=NOW + HOUR + timedelta(seconds=i))
    h.clock = NOW + 2 * HOUR
    await h.run([])
    await h.run([])
    assert h.editors.attempts == []  # the advance of the stamp means it is not retried

    for i, code in enumerate(codes):  # the table trims every reward: a real, later change
        learn(h, code, "short", at=NOW + 3 * HOUR + timedelta(seconds=i))
    h.clock = NOW + 4 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert call.content.count("`reward` short · `via`") == 8
    await h.run([])
    assert len(h.editors.calls) == 1


async def test_an_unexpected_error_rendering_one_message_does_not_end_the_pass(h, monkeypatch):
    ids = bulk_world(h.db_path, {1: 1, 2: 1})
    real = edits_module.render_alert_edit

    def breaks_for_server_one(header, candidates, **kwargs):
        if candidates[0].code == _code(1):
            raise TypeError("a bug nobody planned for")
        return real(header, candidates, **kwargs)

    monkeypatch.setattr(edits_module, "render_alert_edit", breaks_for_server_one)
    deps = h.deps()

    for _ in range(3):
        try:
            await one_pass(h, deps)
        except TypeError:
            pass  # today it escapes; the hook's wrapper would log it

    assert [c.message_id for c in h.editors.calls] == ids[2]


# =====================================================================================
# Startup, preview, the CLI
# =====================================================================================


@pytest.mark.parametrize("startup", [True, False])
async def test_delivering_the_queue_on_its_own_never_edits(h, startup):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    await deliver_queued_codes(h.deps(), startup=startup)

    assert h.editors.attempts == []


async def test_preview_with_a_fresh_code_and_a_due_edit_does_neither_edit_nor_stamp(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR

    await detect_and_fan_out(
        replace(h.deps(), preview=True),
        [item(CODE_B, url="https://example.com/b")],
        seeding_ok=True,
    )

    assert h.editors.attempts == []
    assert [s[3] for s in message_state(h.db_path)] == [DETAILS_STAMP_FLOOR.isoformat()]


async def test_the_cli_mode_with_no_editor_runs_a_whole_hook_and_edits_nothing(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    deps = replace(h.deps(), editor_for=None)

    released = await detect_and_fan_out(
        deps, [item(CODE_B, url="https://example.com/b")], seeding_ok=True
    )

    assert released == 1 and h.editors.attempts == []
    assert [s[3] for s in message_state(h.db_path)][0] == DETAILS_STAMP_FLOOR.isoformat()


# =====================================================================================
# Relative phrases, baked in
# =====================================================================================


@dataclass(frozen=True)
class _Shown:
    when: datetime
    text: str


def _line_at(h, when: datetime) -> str:
    with closing(connect(h.db_path)) as conn:
        row = repo.code_details_for(conn, [CODE_A])[CODE_A]
    text = render_alert_edit(
        "`> new SHiFT code ▌`",
        [
            CodeCandidate(
                CODE_A, golden=False, source_name=GEARBOX, item_url="", fresh=True, trusted=True
            )
        ],
        golden_prefix=False,
        details={CODE_A: row},
        now=when,
    )
    return next(line for line in text.splitlines() if "expire" in line)


async def test_a_posted_expiry_cannot_go_stale_because_discord_keeps_its_timestamp_current(h):
    """Pins the 3.1.0 wording: a date and a live timestamp, so no edit is ever due for the clock.

    NOW is 05:00 Pacific. The code expires at the end of that same Pacific day. The alert
    used to say "expires in 19 hours" and would have said so for the life of the message, since
    nothing edits for the clock alone. It now says the date and a `<t:...:R>` that every
    reader's Discord counts down by itself, so the text is the same at 05:00, at 19:00 and 13
    days later, and there is nothing to go stale. A real change later re-renders the same
    expiry text next to the new reward.
    """
    add_guild(h.db_path, 1, ping="none")
    await h.run([with_details(item(CODE_A), CODE_A, reward="", expires_on=date(2026, 10, 1))])
    stamp = int(datetime(2026, 10, 2, 7, 0, tzinfo=UTC).timestamp())  # Oct 1 ends: PDT midnight
    expiry = f"`expires` Oct 1, 2026 (<t:{stamp}:R>)"
    assert expiry in h.sent(1)[0].content
    assert "`expires` in" not in h.sent(1)[0].content

    for hours in (5, 14, 20, 24 * 3, 24 * 13):
        h.clock = NOW + hours * HOUR
        await h.run([])
    assert h.editors.attempts == []  # 13 days on, nothing is due and nothing is wrong

    # What the same row says whenever anyone asks: the same thing.
    assert _line_at(h, NOW + 14 * HOUR).startswith(expiry)
    assert _line_at(h, NOW + 14 * HOUR) == _line_at(h, NOW + 20 * HOUR)

    # A real change re-renders the line with the same expiry beside the new reward.
    learn(h, CODE_A, "3 Golden Keys", at=NOW + 24 * 13 * HOUR + HOUR)
    h.clock = NOW + 24 * 13 * HOUR + 2 * HOUR
    await h.run([])
    assert expiry in h.editors.calls[0].content
    assert "3 Golden Keys" in h.editors.calls[0].content
    assert "expired" not in h.editors.calls[0].content


# =====================================================================================
# After the round: the fixes, pinned on their own
# =====================================================================================


def _tried_at(h, message_id: int):
    return rows(
        h.db_path,
        "SELECT edit_tried_at FROM guild_alert_messages WHERE message_id = ?",
        message_id,
    )[0][0]


def test_a_real_change_always_gets_a_stamp_strictly_after_the_last_one():
    """No clock, however confused, hands a change a stamp the message already shows."""
    from newsbot.shift.details import merge_details

    first = merge_details(
        None,
        DetailObservation(
            CODE_A, ItemDetails(code=CODE_A, rank="table", reward="1 Key"), MENTALMARS, None
        ),
        now=NOW,
        first_seen_at=None,
        details_since=NOW,
    )
    assert first.details_changed_at == NOW

    for now in (NOW, NOW - HOUR):  # the clock stood still, then stepped back
        again = merge_details(
            first,
            DetailObservation(
                CODE_A, ItemDetails(code=CODE_A, rank="table", reward="2 Keys"), MENTALMARS, None
            ),
            now=now,
            first_seen_at=None,
            details_since=NOW,
        )
        assert again.details_changed_at == NOW + timedelta(microseconds=1)
        first = replace(first, reward="1 Key")  # forget the change, ask again

    later = merge_details(
        first,
        DetailObservation(
            CODE_A, ItemDetails(code=CODE_A, rank="table", reward="3 Keys"), MENTALMARS, None
        ),
        now=NOW + HOUR,
        first_seen_at=None,
        details_since=NOW,
    )
    assert later.details_changed_at == NOW + HOUR  # a clock that moves forward is believed


async def test_a_message_that_keeps_failing_goes_to_the_back_of_its_own_servers_line(h):
    ids = bulk_world(h.db_path, {1: 3})
    h.editors.fail[ids[1][0]] = [PublishError("503")] * 3
    deps = h.deps()

    await one_pass(h, deps)  # the first message fails, and the server stalls
    assert h.editors.calls == [] and _tried_at(h, ids[1][0]) is not None
    assert _tried_at(h, ids[1][1]) is None

    h.editors.fail[ids[1][1]] = [PublishError("503")] * 3
    await one_pass(h, deps)  # the untried second goes first, fails, and stalls the server
    assert [c.message_id for c in h.editors.attempts][-1] == ids[1][1]

    h.editors.fail.clear()
    await one_pass(h, deps)
    # Never-tried first, then least recently tried.
    assert [c.message_id for c in h.editors.calls] == [ids[1][2], ids[1][0], ids[1][1]]


async def test_a_429_past_the_budget_stalls_that_server_for_the_pass_and_counts_the_rest_as_waiting(
    h,
):
    ids = bulk_world(h.db_path, {1: 3, 2: 1})
    h.editors.fail[ids[1][0]] = [PublishError("slow", rejected=True, retry_after=600.0)]

    outcome = await one_pass(h, h.deps())

    assert [c.message_id for c in h.editors.attempts] == [ids[1][0], ids[2][0]]
    assert (outcome.edited, outcome.waiting) == (1, 3)  # message one, plus its two neighbours
    assert _tried_at(h, ids[1][0]) is not None


async def test_a_deleted_channel_is_one_lookup_and_leaves_the_servers_other_channels_alone(h):
    world = World()
    h.editors = world.editors()
    ids = bulk_world(h.db_path, {1: 4})
    with closing(connect(h.db_path)) as conn, conn:  # the last two messages live in channel 150
        conn.execute(
            "UPDATE guild_alert_messages SET channel_id = 150 WHERE message_id IN (?, ?)",
            (ids[1][2], ids[1][3]),
        )
    world.channel(150, 1)

    outcome = await one_pass(h, h.deps())

    assert world.fetches == 1
    assert outcome.gone == 2 and outcome.edited == 2
    assert [mid for mid, _kw in world.channels[150].edits] == [ids[1][2], ids[1][3]]
    assert rows(
        h.db_path, "SELECT state, count(*) FROM guild_alert_messages GROUP BY state ORDER BY 1"
    ) == [("gone", 2), ("live", 2)]


async def test_a_read_that_blows_up_costs_that_message_its_turn_and_nothing_more(
    h, monkeypatch, caplog
):
    ids = bulk_world(h.db_path, {1: 1, 2: 1})
    real = edits_module.alert_message_codes

    def breaks_for_server_one(conn, guild_id, message_id):
        if guild_id == 1:
            raise RuntimeError("a row I did not expect")
        return real(conn, guild_id, message_id)

    monkeypatch.setattr(edits_module, "alert_message_codes", breaks_for_server_one)

    with caplog.at_level("ERROR"):
        outcome = await one_pass(h, h.deps())

    assert [c.message_id for c in h.editors.calls] == ids[2]
    assert (outcome.edited, outcome.failed) == (1, 1)
    assert _tried_at(h, ids[1][0]) is not None
    assert any(r.exc_info for r in caplog.records)  # logged with the traceback


async def test_a_render_that_blows_up_leaves_the_stamp_alone_and_remembers_the_try(h, monkeypatch):
    ids = bulk_world(h.db_path, {1: 1})

    def breaks(*_args, **_kwargs):
        raise TypeError("a bug nobody planned for")

    monkeypatch.setattr(edits_module, "render_alert_edit", breaks)

    outcome = await one_pass(h, h.deps())

    assert outcome.failed == 1 and h.editors.attempts == []
    assert _tried_at(h, ids[1][0]) is not None
    assert message_state(h.db_path)[0][3] == DETAILS_STAMP_FLOOR.isoformat()  # still due


@pytest.mark.parametrize(
    ("column", "junk", "warned"),
    [
        ("rendered_at", "", True),
        # Letters sort after digits, so this one is never "older" than a change: never due,
        # and nothing to warn about. It is skipped all the same.
        ("rendered_at", "yesterday-ish", False),
        ("posted_at", "garbage", True),
    ],
)
async def test_a_malformed_timestamp_skips_that_row_and_not_the_pass(
    h, caplog, column, junk, warned
):
    ids = bulk_world(h.db_path, {1: 1, 2: 1})
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            f"UPDATE guild_alert_messages SET {column} = ? WHERE guild_id = 1",  # noqa: S608
            (junk,),
        )

    with caplog.at_level("WARNING"):
        outcome = await one_pass(h, h.deps())

    assert [c.message_id for c in h.editors.calls] == ids[2]
    assert outcome.edited == 1
    assert any("timestamp" in r.getMessage() for r in caplog.records) is warned


def test_the_cutoff_ignores_how_the_callers_clock_is_spelled(h):
    """The same instant, in UTC and in Pacific, picks the same messages."""
    add_guild(h.db_path, 1, ping="none")
    posted = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
            "golden_prefix, posted_at, rendered_at) VALUES (1, 9, 100, 'h', 0, ?, ?)",
            (posted.isoformat(), DETAILS_STAMP_FLOOR.isoformat()),
        )
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES (?, ?, 'x', 'https://x', 'posted')",
            (CODE_A, posted.isoformat()),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, message_id, claimed_at) "
            "VALUES (1, ?, 'posted', 9, ?)",
            (CODE_A, posted.isoformat()),
        )
    learn(h, CODE_A, "3 Golden Keys", at=NOW)
    edge = posted + timedelta(days=14)
    with closing(connect(h.db_path)) as conn:
        for now in (edge, edge.astimezone(LA), edge.astimezone(ZoneInfo("Asia/Tokyo"))):
            assert len(repo.alert_messages_to_edit(conn, now=now)) == 1
        for now in (edge + timedelta(minutes=1), (edge + timedelta(minutes=1)).astimezone(LA)):
            assert repo.alert_messages_to_edit(conn, now=now) == []
