"""Task 7 must not change a byte of what delivery sends when there are no details (plan task 7).

`fixtures/fanout_pre_delivery_snapshot.txt` is `newsbot/shift/fanout.py` exactly as
`git show HEAD:newsbot/shift/fanout.py` printed it before task 7 (the task 6 commit,
82e1144), checked in so the comparison survives the task 7 commit and doesn't need git at
test time. It is loaded as a throwaway module, the same trick the format snapshot and the
v2.2.0 and v3.0.5 repo snapshots play, and it runs against the *current* repo and format
modules (both of which are separately held to their own snapshots), so what this file
isolates is the fan-out's own change.

Every scenario below runs twice on identical, fresh databases and clocks: once through the
old module and once through the new one with `details_reserve=0` and no details rows. What
the posters were handed (content, codes, ping, nonce, ping prefix, in order), what the
servers were told, and every table in the database must come out the same. The one table
allowed to differ is `guild_alert_messages`, which only the new code writes, and a test
says so out loud.

One edit to the snapshot itself (QA batch 2): it imported `repo.table_listed_sources`, the
legacy per-source first-listing key's reader, which no longer exists, so that import is gone and
its one use reads `set()`. Nothing ever wrote the key, so the comparison is unchanged.

Invisible characters are `\\uXXXX` escapes on purpose; this file stays visible.
"""

from __future__ import annotations

import sys
import types
from contextlib import closing
from dataclasses import astuple, dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_shift_delivery_details import BLUESKY, REDDIT, numbered, sighting
from test_shift_fanout import (
    CODE_A,
    CODE_B,
    CODE_C,
    CODE_D,
    NOW,
    ROLE,
    V3,
    add_guild,
    item,
    seed,
)

from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import fanout as new_fanout
from newsbot.store import repo
from newsbot.store.db import connect, migrate

SNAPSHOT = Path(__file__).parent / "fixtures" / "fanout_pre_delivery_snapshot.txt"
STAR_NAME = "*" * 100


@pytest.fixture(scope="module")
def old_fanout():
    """`shift/fanout.py` as it was before task 7."""
    module = types.ModuleType("newsbot_shift_fanout_pre_delivery")
    sys.modules[module.__name__] = module  # dataclasses looks its own module up here
    try:
        exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(module.__name__, None)


def test_the_snapshot_really_is_the_old_module(old_fanout):
    assert not hasattr(old_fanout, "reserve_for_config")
    assert "details_reserve" not in old_fanout.FanoutDeps.__dataclass_fields__
    assert "details_reserve" in new_fanout.FanoutDeps.__dataclass_fields__


@dataclass
class Seen:
    """What a poster was handed, in order."""

    guild_id: int
    channel_id: int
    ping_setting: str
    alert: tuple


class World:
    """One fresh database, one module's fan-out, and a log of everything it did."""

    def __init__(self, module, tmp_path, name, *, cfg=None, **deps_extra):
        self.module = module
        self.deps_extra = deps_extra
        self.db_path = str(tmp_path / f"{name}.db")
        with closing(connect(self.db_path)) as conn:
            migrate(conn)
        seed(self.db_path)
        self.cfg = cfg or load_config(V3)
        self.clock = NOW
        self.next_id = 100
        self.script: dict[int, list] = {}
        self.on_begin = None
        self.seen: list[Seen] = []
        self.notices: list[tuple[int, str]] = []
        self.owner: list[str] = []
        self.sleeps: list[float] = []

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)

    async def _notify(self, guild_id, text):
        self.notices.append((guild_id, text))

    async def _owner(self, text):
        self.owner.append(text)

    def deps(self):
        world = self

        class Poster:
            def __init__(self, guild_id, channel_id, ping):
                self.guild_id, self.channel_id, self.ping = guild_id, channel_id, ping

            def begin_batch(self):
                if world.on_begin is not None:
                    world.on_begin(self.guild_id)

            async def post(self, alert):
                script = world.script.get(self.guild_id)
                outcome = script.pop(0) if script else None
                if outcome is not None:
                    raise outcome
                world.next_id += 1
                world.seen.append(
                    Seen(
                        self.guild_id,
                        self.channel_id,
                        self.ping,
                        (
                            alert.content,
                            tuple(alert.codes),
                            alert.ping,
                            alert.nonce,
                            alert.ping_prefix,
                        ),
                    )
                )
                return world.next_id

        return self.module.FanoutDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            now=lambda: self.clock,
            poster_for=lambda g, c, p, n: Poster(g, c, p),
            notify_guild=self._notify,
            sleep=self._sleep,
            alert_owner=self._owner,
            clock=lambda: 0.0,
            **self.deps_extra,
        )

    async def run(self, items):
        return await self.module.detect_and_fan_out(self.deps(), items, seeding_ok=True)

    def dump(self, *, skip=("guild_alert_messages",)) -> dict[str, list[tuple]]:
        out = {}
        with closing(connect(self.db_path)) as conn:
            tables = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
                )
            ]
            for table in sorted(tables):
                if table in skip:
                    continue
                columns = "*"
                if table == "alerted_codes":
                    # The two flags 009 grew for QA batch 1 (official_seen_at, table_silenced_at)
                    # are facts only the new fan-out writes, like guild_alert_messages, so they
                    # sit out the comparison; every column the old one wrote must still match.
                    names = [r["name"] for r in conn.execute("PRAGMA table_info(alerted_codes)")]
                    columns = ", ".join(
                        n for n in names if n not in ("official_seen_at", "table_silenced_at")
                    )
                got = [tuple(r) for r in conn.execute(f"SELECT {columns} FROM {table}")]  # noqa: S608
                if table == "app_state":
                    # Migration 009 stamps this with the wall clock when the database is built.
                    got = [r for r in got if r[0] != "shift_details_since"]
                out[table] = sorted(got, key=repr)
        return out

    def outcome(self):
        return {
            "seen": [astuple(s) for s in self.seen],
            "notices": self.notices,
            "owner": self.owner,
            "sleeps": self.sleeps,
            "db": self.dump(),
        }


def refused():
    return PublishError("slow down", rejected=True, retry_after=0.0)


def distinct(*codes, trust="official", **kw):
    return [item(c, trust=trust, url=f"https://example.com/{c[:5]}", **kw) for c in codes]


def claim_elsewhere(world, guild_id, code):
    with closing(connect(world.db_path)) as conn:
        repo.claim_guild_codes(
            conn, guild_id, [code], pinged=False, local_day="2026-10-01", now=lambda: NOW
        )
        repo.mark_guild_codes_posted(conn, guild_id, [code], message_id=7777)


GOLDEN_TITLE = "Borderlands 4 Golden Key SHiFT code: "


async def one_everyone(w):
    add_guild(w.db_path, 1, ping="everyone")
    await w.run([item(CODE_A)])


async def a_big_role_batch(w):
    add_guild(w.db_path, 1, ping=ROLE)
    add_guild(w.db_path, 2, ping="none")
    await w.run(distinct(*[numbered(i) for i in range(1, 26)]))


async def mixed_golden_and_plain(w):
    add_guild(w.db_path, 1, ping="everyone")
    add_guild(w.db_path, 2, ping=ROLE)
    golden = item(CODE_A, title=GOLDEN_TITLE + CODE_A, url="https://e/a")
    await w.run([golden, *distinct(CODE_B, CODE_C)])


async def community_then_confirmed(w):
    add_guild(w.db_path, 1, ping="everyone")
    codes = [numbered(i) for i in range(1, 9)]
    await w.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    w.clock = NOW + timedelta(hours=20)
    await w.run([sighting(BLUESKY, "community", c, w.clock) for c in codes])
    w.clock += timedelta(hours=1)
    await w.run([])


async def roundup_and_normal(w):
    add_guild(w.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]
    await w.run([item(CODE_A), item(*many, url="https://example.com/megathread")])


async def ambiguous_failure_strips_the_ping(w):
    add_guild(w.db_path, 1, ping=ROLE)
    add_guild(w.db_path, 2, ping="everyone")
    w.script[1] = [PublishError("timeout")]
    w.script[2] = [refused(), refused()]
    await w.run(distinct(CODE_A, CODE_B, CODE_C))


async def the_cap_is_reached(w):
    add_guild(w.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=3)
    add_guild(w.db_path, 2, ping="everyone", ping_day="2026-10-01", ping_count=2)
    await w.run(distinct(CODE_A, CODE_B))
    await w.run(distinct(CODE_C))


async def a_batch_that_fails_for_good(w):
    add_guild(w.db_path, 1, ping="everyone")
    add_guild(w.db_path, 2, ping="everyone")
    w.script[1] = [PublishError("down")] * 20
    await w.run(distinct(CODE_A, CODE_B))


async def a_partial_claim(w):
    add_guild(w.db_path, 1, ping="everyone")
    w.on_begin = lambda guild_id: claim_elsewhere(w, guild_id, CODE_B)
    golden = item(CODE_A, title=GOLDEN_TITLE + CODE_A, url="https://e/a")
    await w.run([golden, *distinct(CODE_B, CODE_C)])


async def hostile_names_and_links(w):
    add_guild(w.db_path, 1, ping="everyone")
    nasty = [
        replace(it, source_name=STAR_NAME, url="https://example.com/" + "a/" * 900)
        for it in distinct(CODE_A, CODE_B)
    ]
    emoji = replace(item(CODE_C, url="https://example.com/e"), source_name="\U0001f600" * 100)
    mention = replace(item(CODE_D, url="https://example.com/m"), source_name="@everyone <@&123>")
    await w.run([*nasty, emoji, mention])


async def two_passes_and_a_late_guild(w):
    add_guild(w.db_path, 1, ping="everyone")
    await w.run(distinct(CODE_A))
    w.clock += timedelta(hours=1)
    add_guild(w.db_path, 2, ping=ROLE, enabled_at=w.clock)
    await w.run(distinct(CODE_B, CODE_C))


SCENARIOS = [
    one_everyone,
    a_big_role_batch,
    mixed_golden_and_plain,
    community_then_confirmed,
    roundup_and_normal,
    ambiguous_failure_strips_the_ping,
    the_cap_is_reached,
    a_batch_that_fails_for_good,
    a_partial_claim,
    hostile_names_and_links,
    two_passes_and_a_late_guild,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
async def test_without_details_delivery_is_byte_identical_to_before_task_7(
    old_fanout, tmp_path, scenario
):
    old = World(old_fanout, tmp_path, "old")
    new = World(new_fanout, tmp_path, "new")

    await scenario(old)
    await scenario(new)

    assert old.seen, "the scenario should have posted something"
    assert new.outcome() == old.outcome()


async def test_the_only_new_write_is_the_alert_message_record(old_fanout, tmp_path):
    old = World(old_fanout, tmp_path, "old")
    new = World(new_fanout, tmp_path, "new")

    await a_big_role_batch(old)
    await a_big_role_batch(new)

    assert new.dump(skip=()).keys() == old.dump(skip=()).keys()
    with closing(connect(old.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM guild_alert_messages").fetchone()[0] == 0
    with closing(connect(new.db_path)) as conn:
        # One record per posted normal message: here, guild 1's and guild 2's first (and only)
        # batches, however many messages each took.
        count = conn.execute("SELECT COUNT(*) FROM guild_alert_messages").fetchone()[0]
        posted_messages = conn.execute(
            "SELECT COUNT(DISTINCT guild_id || ':' || message_id) FROM guild_code_posts "
            "WHERE status = 'posted' AND from_roundup = 0"
        ).fetchone()[0]
    assert count == posted_messages > 0


async def test_a_zero_reserve_with_details_present_packs_by_the_real_line(tmp_path):
    """The one place `details_reserve=0` isn't the old behavior: a code that has a line."""
    from test_shift_delivery_details import store_details

    w = World(new_fanout, tmp_path, "new")
    add_guild(w.db_path, 1, ping="everyone")
    store_details(w.db_path, CODE_A, reward="3 Golden Keys")

    await w.run([item(CODE_A)])

    (seen,) = w.seen
    assert "3 Golden Keys" in seen.alert[0]


async def test_the_comparison_would_notice_a_real_difference(old_fanout, tmp_path):
    """A guard on the guard: turn the reserve on and the same scenario must stop matching."""
    old = World(old_fanout, tmp_path, "old")
    new = World(new_fanout, tmp_path, "new", details_reserve=700)

    await a_big_role_batch(old)
    await a_big_role_batch(new)

    assert new.outcome() != old.outcome()
    assert len(new.seen) > len(old.seen)  # more, smaller messages
