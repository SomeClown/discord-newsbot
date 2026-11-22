"""Adversarial round on v3.5.0 Task 2: what a SHiFT turn says it did, and what it must not touch.

The builder's file (`test_shift_outcome_bookkeeping.py`) pins the happy shapes. This one leans
on the three places the change could have moved something it promised to leave alone:

1. A differential against the pre-change `fanout.py`, read out of git at a pinned commit.
   Random turns (several servers, every ping setting, capped days, official and community
   codes, roundups, follow-up bursts, scripted post failures, lost marks, preview mode) run
   through both modules on twin databases. What was sent (text, ping, nonce, codes, every
   attempt), every table in the database afterwards, and every `GuildOutcome` field must
   match. The two things allowed to differ are the ones the task changed: `pinged` (only
   upward, only for a turn that sent a follow-up) and the counts on an errored turn (which
   used to be none at all).
2. Honesty of the partial counts. A mark is made to fail at every message position (normal
   batch, roundup batch, follow-ups, and after a post Discord rejected), and the outcome is
   checked against what the database says was recorded, never against what was sent.
3. The follow-up ping, as a truth table, including the one the builder's report calls out of
   scope for alerts but the task text names for follow-ups: a ping Discord's permissions
   dropped.

If git or the pinned commit isn't around (a shallow checkout, a tarball), the differential
tests skip with a reason; everything else runs everywhere. Real temp databases, scripted
fakes, no network.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import sqlite3
import subprocess
import sys
import types
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_shift_fanout_adversarial import (
    ROLE,
    FakeChannel,
    FakeRole,
    Harness,
    _code,
    add_guild,
    seed,
)
from test_shift_followups import (
    BLUESKY,
    FOLLOWUP_PROMPT,
    REDDIT,
    T0,
    burst,
    script_posts,
    sighting_item,
)

from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import fanout
from newsbot.shift.fanout import GuildOutcome, MarkLostError
from newsbot.store.db import connect, migrate

REPO = Path(__file__).resolve().parent.parent
# The commit the staged change sits on top of, pinned so the differential means the same thing
# after the change lands.
BASE_COMMIT = "ede23c34ff15585844d19bd3a2fc9d48c52a2d06"
V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
ERROR = "database busy while recording"
CONFIRMED_DAY = "2026-10-01"  # the local day T0 + 20 hours falls on (UTC servers)


# --- the old fanout.py, read out of git as a throwaway module ---


@pytest.fixture(scope="module")
def old_fanout():
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{BASE_COMMIT}:newsbot/shift/fanout.py"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {BASE_COMMIT} is not available here")
    name = "_newsbot_fanout_base_t2"
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module  # dataclasses look their module up by name
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


# --- helpers ---


def make_db(tmp_path, name):
    path = str(tmp_path / f"{name}.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    seed(path)
    return path


def make_harness(tmp_path, name):
    return Harness(make_db(tmp_path, name), load_config(V3), T0)


@contextmanager
def held_delivery():
    """Queue without sending: the delivery step does nothing while this is open."""

    async def nothing(deps, guild_id):
        return GuildOutcome(guild_id)

    with (
        patch.object(fanout, "_deliver_one", nothing),
        patch.object(fanout, "_deliver_followups", nothing),
    ):
        yield


async def queue(h, items):
    with held_delivery():
        await h.run(items)


@contextmanager
def lose_marks(module, lose, tripped):
    """Every write that records a message naming a (guild, code) in `lose` stays busy forever."""

    def wrap(real):
        def inner(db_path, guild_id, codes, *rest):
            if any((guild_id, c) in lose for c in codes):
                tripped.add((guild_id, tuple(codes)))
                raise sqlite3.OperationalError("database is locked")
            return real(db_path, guild_id, codes, *rest)

        return inner

    names = ("_mark_posted_sync", "_mark_failed_sync", "_followup_mark_sync")
    patches = [patch.object(module, n, wrap(getattr(module, n))) for n in names]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


async def deliver(h, module=fanout, *, script=(), lose=(), preview=False):
    made = script_posts(h, *script)
    deps = h.deps()
    deps.preview = preview
    tripped: set = set()
    with lose_marks(module, set(lose), tripped):
        outcomes = await module.deliver_queued_codes(deps)
    return outcomes, made, tripped


def dump(db_path):
    """Every table, every row, so "nothing else moved" means nothing."""
    with closing(connect(db_path)) as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = {
            n: sorted(repr(tuple(r)) for r in conn.execute(f"SELECT * FROM {n}"))  # noqa: S608
            for n in names
        }
    # The one wall-clock stamp in the schema (set the first time a database is opened).
    tables["app_state"] = [r for r in tables["app_state"] if "shift_details_since" not in r]
    return tables


def state(db_path):
    """(posts, followups): each code's status (and whether it came from a roundup)."""
    with closing(connect(db_path)) as conn:
        posts = {
            (r[0], r[1]): (r[2], r[3])
            for r in conn.execute(
                "SELECT guild_id, code, status, from_roundup FROM guild_code_posts"
            )
        }
        follows = {
            (r[0], r[1]): r[2]
            for r in conn.execute("SELECT guild_id, code, status FROM guild_code_followups")
        }
    return posts, follows


def recorded_delta(before, after, guild_id):
    """What the database says changed for one server: what the partial counts must equal."""
    bp, bf = before
    ap, af = after
    posted = roundup = failed = followups = 0
    for key, (status, from_roundup) in ap.items():
        if key[0] != guild_id:
            continue
        was = bp.get(key, (None, None))[0]
        if status == "posted" and was != "posted":
            if from_roundup:
                roundup += 1
            else:
                posted += 1
        if status == "failed" and was != "failed":
            failed += 1
    for key, status in af.items():
        if key[0] == guild_id and status == "posted" and bf.get(key) != "posted":
            followups += 1
    return {
        "posted": posted,
        "roundup_posted": roundup,
        "failed": failed,
        "followups_posted": followups,
    }


def is_followup(alert):
    return (
        FOLLOWUP_PROMPT in alert.content.split("\n", 1)[0]
        or "(continued)" in (alert.content.split("\n", 1)[0])
    )


def sent_view(made):
    return [
        (
            p.guild_id,
            [(a.content, a.ping, a.ping_prefix, a.nonce, tuple(a.codes)) for a in p.calls],
            [(a.content, a.ping) for a in p.sent],
        )
        for p in made
    ]


def fields(outcome):
    return dataclasses.asdict(outcome)


# --- 1. the differential ---


def make_spec(seed_no):
    r = random.Random(seed_no)  # noqa: S311 (a fixed seed, not a secret)
    guilds = [
        (g, r.choice(["none", "everyone", "everyone", ROLE])) for g in range(1, r.randint(1, 3) + 1)
    ]
    n1 = r.choice([0, 0, 3, 20, 45])
    stage1 = [_code(i) for i in range(1, n1 + 1)]
    return {
        "guilds": guilds,
        "capped": {g for g, _ in guilds if r.random() < 0.2},
        "stage1": stage1,
        "official": [_code(i) for i in range(100, 100 + r.choice([0, 1, 3, 25, 45]))],
        "community": [_code(i) for i in range(200, 200 + r.choice([0, 0, 1, 4]))],
        "roundup": [_code(i) for i in range(300, 300 + r.choice([0, 0, 8, 25, 40]))],
        "confirm": [c for c in stage1 if r.random() < 0.8],
        "script": [
            r.choice(
                [None] * 6
                + [
                    PublishError("timeout"),
                    RuntimeError("gone"),
                    PublishError("429", rejected=True),
                ]
            )
            for _ in range(r.randint(0, 7))
        ],
        "preview": r.random() < 0.1,
        "lose": r.random() < 0.45,
        "lose_pick": r.random(),
        "lose_guild": r.randint(0, 2),
    }


async def build_stage_two(h, spec):
    for g, ping in spec["guilds"]:
        add_guild(h.db_path, g, ping=ping)
    if spec["stage1"]:
        await h.run([sighting_item(REDDIT, "community", c, at=T0) for c in spec["stage1"]])
    h.clock = T0 + timedelta(hours=20)
    for g in spec["capped"]:
        with closing(connect(h.db_path)) as conn, conn:
            conn.execute(
                "UPDATE guild_shift SET ping_day = ?, ping_count = 3 WHERE guild_id = ?",
                (CONFIRMED_DAY, g),
            )
    items = [sighting_item("Gearbox", "official", c, at=h.clock) for c in spec["official"]]
    items += [sighting_item(REDDIT, "community", c, at=h.clock) for c in spec["community"]]
    if spec["roundup"]:
        items.append(sighting_item(REDDIT, "community", *spec["roundup"], at=h.clock))
    items += [sighting_item(BLUESKY, "community", c, at=h.clock) for c in spec["confirm"]]
    await queue(h, items)


def pick_loss(spec):
    pool = spec["official"] + spec["community"] + spec["roundup"] + spec["confirm"]
    if not spec["lose"] or not pool:
        return ()
    guilds = [g for g, _ in spec["guilds"]]
    guild = guilds[spec["lose_guild"] % len(guilds)]
    return ((guild, pool[int(spec["lose_pick"] * len(pool))]),)


@pytest.mark.parametrize("seed_no", range(90))
async def test_the_change_moves_nothing_but_pinged_and_the_errored_counts(
    old_fanout, tmp_path, seed_no
):
    spec = make_spec(seed_no)
    lose = pick_loss(spec)
    worlds = []
    for name, module in (("old", old_fanout), ("new", fanout)):
        h = make_harness(tmp_path, name)
        await build_stage_two(h, spec)
        before = state(h.db_path)
        outcomes, made, tripped = await deliver(
            h, module, script=spec["script"], lose=lose, preview=spec["preview"]
        )
        worlds.append((outcomes, made, tripped, dump(h.db_path), before, state(h.db_path)))
    (o_out, o_made, o_trip, o_dump, _, _), (n_out, n_made, n_trip, n_dump, n_before, n_after) = (
        worlds
    )

    assert sent_view(n_made) == sent_view(o_made)  # what was sent, in order, every attempt
    assert n_trip == o_trip  # the same marks were the ones that were lost
    assert n_dump == o_dump  # every table, byte for byte
    assert [o.guild_id for o in n_out] == [o.guild_id for o in o_out]  # same turns, same order
    for old, new in zip(o_out, n_out, strict=True):
        a, b = fields(old), fields(new)
        if old.error is not None:
            assert new.error == old.error
            continue
        pa, pb = a.pop("pinged"), b.pop("pinged")
        assert a == b
        assert pb == pa or (pb and not pa and new.followups_posted > 0)
    # The other half of the contract: an errored turn's counts are the database's, not the wire's.
    if not spec["preview"]:
        for new in n_out:
            if new.error == ERROR:
                want = recorded_delta(n_before, n_after, new.guild_id)
                got = {k: getattr(new, k) for k in want}
                assert got == want
                recorded_pings = [
                    a
                    for p in n_made
                    if p.guild_id == new.guild_id
                    for a in p.sent
                    if a.ping and _recorded(a, n_after, new.guild_id)
                ]
                if recorded_pings:
                    assert new.pinged is True


def _recorded(alert, after, guild_id):
    posts, follows = after
    table = follows if is_followup(alert) else {k: v[0] for k, v in posts.items()}
    return all(table.get((guild_id, c)) == "posted" for c in alert.codes)


# --- 2. partial counts at every position ---


async def official_world(tmp_path, name, *, official=45, roundup=0, ping="everyone"):
    h = make_harness(tmp_path, name)
    add_guild(h.db_path, 1, ping=ping)
    items = [sighting_item("Gearbox", "official", _code(i), at=T0) for i in range(1, official + 1)]
    if roundup:
        items.append(
            sighting_item(
                REDDIT, "community", *[_code(i) for i in range(300, 300 + roundup)], at=T0
            )
        )
    await queue(h, items)
    return h


async def message_plan(tmp_path, **kw):
    """The messages a clean turn sends: [(codes, is_roundup)] in order."""
    h = await official_world(tmp_path, "plan", **kw)
    before = state(h.db_path)
    _, made, _ = await deliver(h)
    posts = before[0]
    return [(tuple(a.codes), posts[(1, a.codes[0])][1] == 1) for a in made[0].sent] or [
        (tuple(a.codes), False) for a in made[0].sent
    ]


async def plan_with_roundups(tmp_path):
    h = await official_world(tmp_path, "plan2", official=45, roundup=50)
    _, made, _ = await deliver(h)
    with closing(connect(h.db_path)) as conn:
        roundup_codes = {
            r[0] for r in conn.execute("SELECT code FROM guild_code_posts WHERE from_roundup = 1")
        }
    return [(tuple(a.codes), a.codes[0] in roundup_codes) for a in made[0].sent]


async def test_a_lost_mark_at_every_position_of_a_normal_batch_counts_only_what_was_recorded(
    tmp_path,
):
    plan = await message_plan(tmp_path)
    assert len(plan) >= 3, "the batch has to span messages for this test to mean anything"
    for i, (codes, _) in enumerate(plan):
        h = await official_world(tmp_path, f"n{i}")
        before = state(h.db_path)
        [outcome], made, _ = await deliver(h, lose=[(1, codes[0])])

        recorded = sum(len(c) for c, _ in plan[:i])
        assert outcome.error == ERROR, i
        assert outcome.posted == recorded, i
        assert outcome.failed == 0 and outcome.roundup_posted == 0 and outcome.followups_posted == 0
        assert outcome.pinged is (i > 0), i  # the pinging message is the first one
        assert {k: getattr(outcome, k) for k in ("posted", "failed")} == {
            k: v
            for k, v in recorded_delta(before, state(h.db_path), 1).items()
            if k in ("posted", "failed")
        }
        assert [tuple(a.codes) for a in made[0].sent] == [c for c, _ in plan[: i + 1]]
        statuses = state(h.db_path)[0]
        assert all(statuses[(1, c)][0] == "pending" for c in codes)  # the lost one stays pending


async def test_a_lost_mark_in_the_roundup_batch_keeps_the_whole_normal_batch(tmp_path):
    plan = await plan_with_roundups(tmp_path)
    normal = [c for c, r in plan if not r]
    roundup = [c for c, r in plan if r]
    assert len(roundup) >= 2 and normal
    for j, codes in enumerate(roundup):
        h = await official_world(tmp_path, f"r{j}", official=45, roundup=50)
        before = state(h.db_path)
        [outcome], _, _ = await deliver(h, lose=[(1, codes[0])])

        assert outcome.error == ERROR, j
        assert outcome.posted == sum(len(c) for c in normal)
        assert outcome.roundup_posted == sum(len(c) for c in roundup[:j]), j
        assert outcome.pinged is True  # the normal batch's ping landed and was recorded
        want = recorded_delta(before, state(h.db_path), 1)
        assert (outcome.posted, outcome.roundup_posted, outcome.failed) == (
            want["posted"],
            want["roundup_posted"],
            want["failed"],
        )


async def test_a_lost_mark_on_the_only_message_reports_nothing_and_no_ping(tmp_path):
    h = await official_world(tmp_path, "one", official=1)
    [outcome], made, _ = await deliver(h, lose=[(1, _code(1))])

    assert len(made[0].sent) == 1  # it did go out
    assert outcome == GuildOutcome(1, error=ERROR, reasons=outcome.reasons)
    assert outcome.pinged is False and outcome.posted == 0


@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_a_post_discord_rejected_then_a_lost_mark_counts_only_recorded_failures(
    tmp_path, fail_at
):
    plan = await message_plan(tmp_path)
    script = [None] * fail_at + [RuntimeError("channel gone")]
    for j in range(fail_at, len(plan)):
        h = await official_world(tmp_path, f"f{fail_at}_{j}")
        before = state(h.db_path)
        [outcome], made, _ = await deliver(h, script=script, lose=[(1, plan[j][0][0])])

        assert outcome.error == ERROR, (fail_at, j)
        assert outcome.posted == sum(len(c) for c, _ in plan[:fail_at]), (fail_at, j)
        assert outcome.failed == sum(len(c) for c, _ in plan[fail_at:j]), (fail_at, j)
        want = recorded_delta(before, state(h.db_path), 1)
        assert (outcome.posted, outcome.failed) == (want["posted"], want["failed"])
        assert len(made[0].sent) == fail_at  # nothing after the rejection was ever sent
        # Pinged only if the pinging first message landed and was recorded.
        assert outcome.pinged is (fail_at > 0 and j > 0), (fail_at, j)


async def test_the_error_log_stays_and_the_info_line_is_only_for_the_healthy_server(
    tmp_path, caplog
):
    h = make_harness(tmp_path, "log")
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await queue(h, [sighting_item("Gearbox", "official", _code(1), at=T0)])
    caplog.set_level(logging.INFO, logger=fanout.logger.name)

    outcomes, _, _ = await deliver(h, lose=[(1, _code(1))])

    assert [(o.guild_id, o.error) for o in outcomes] == [(1, ERROR), (2, None)]
    assert outcomes[1].posted == 1 and outcomes[1].pinged is True  # the next server had its turn
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.guild_id for r in errors if "stayed busy" in r.getMessage()] == [1]
    lines = [r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"]
    assert [r.guild_id for r in lines] == [2]


async def test_a_lost_mark_with_a_capped_day_keeps_cap_reached_and_never_pings(tmp_path):
    plan = await message_plan(tmp_path)
    h = await official_world(tmp_path, "cap")
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "UPDATE guild_shift SET ping_day = ?, ping_count = 3 WHERE guild_id = 1",
            ("2026-09-30",),
        )
    [outcome], _, _ = await deliver(h, lose=[(1, plan[1][0][0])])

    assert outcome.error == ERROR
    assert outcome.cap_reached is True and "cap_reached" in outcome.reasons
    assert outcome.posted == len(plan[0][0])
    assert outcome.pinged is False


# --- 3. follow-ups ---


async def followup_world(tmp_path, name, *, burst_size=1, ping="everyone"):
    h = make_harness(tmp_path, name)
    add_guild(h.db_path, 1, ping=ping)
    codes = await burst(h, burst_size)
    await queue(h, [sighting_item(BLUESKY, "community", c, at=h.clock) for c in codes])
    return h, codes


async def test_a_followup_burst_over_three_messages_pings_once_only_through_the_first(tmp_path):
    h, codes = await followup_world(tmp_path, "b3", burst_size=50)
    [outcome], made, _ = await deliver(h)

    sent = made[0].sent
    assert [a.ping for a in sent] == [True, False, False]
    assert outcome.followups_posted == 50 and outcome.pinged is True


async def test_a_burst_whose_first_message_lost_its_ping_never_reports_one_later(tmp_path):
    h, _ = await followup_world(tmp_path, "b3s", burst_size=50)
    [outcome], made, _ = await deliver(h, script=[PublishError("timeout")])

    assert [a.ping for a in made[0].sent] == [False, False, False]
    assert outcome.followups_posted == 50
    assert outcome.pinged is False


async def test_a_burst_whose_later_message_dies_still_reports_the_first_messages_ping(tmp_path):
    h, _ = await followup_world(tmp_path, "b3d", burst_size=50)
    [outcome], _, _ = await deliver(h, script=[None, RuntimeError("gone")])

    assert outcome.followups_posted == 20 and outcome.pinged is True


async def test_a_burst_whose_first_message_dies_for_good_reports_no_ping(tmp_path):
    h, _ = await followup_world(tmp_path, "b3x", burst_size=50)
    [outcome], made, _ = await deliver(h, script=[RuntimeError("gone")])

    assert made[0].sent == [] and outcome.followups_posted == 0 and outcome.pinged is False


async def test_a_429_retry_keeps_the_ping_and_reports_it(tmp_path):
    h, _ = await followup_world(tmp_path, "s429")
    [outcome], made, _ = await deliver(
        h, script=[PublishError("429", rejected=True), PublishError("429", rejected=True)]
    )

    assert made[0].sent[0].ping is True
    assert outcome.pinged is True


async def test_a_429_then_an_ambiguous_failure_strips_and_reports_no_ping(tmp_path):
    h, _ = await followup_world(tmp_path, "s429t")
    [outcome], made, _ = await deliver(
        h, script=[PublishError("429", rejected=True), PublishError("t")]
    )

    assert made[0].sent[0].ping is False
    assert outcome.followups_posted == 1 and outcome.pinged is False


async def test_a_role_server_followup_reports_its_role_ping(tmp_path):
    h, _ = await followup_world(tmp_path, "role", ping=ROLE)
    [outcome], made, _ = await deliver(h)

    assert made[0].sent[0].content.startswith(f"<@&{ROLE}> ")
    assert outcome.pinged is True


async def test_a_capped_server_gets_no_followup_and_reports_no_ping(tmp_path):
    h, _ = await followup_world(tmp_path, "capf")
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "UPDATE guild_shift SET ping_day = ?, ping_count = 3 WHERE guild_id = 1",
            (CONFIRMED_DAY,),
        )
    [outcome], made, _ = await deliver(h)

    assert all(p.sent == [] for p in made)
    assert outcome.followups_posted == 0 and outcome.pinged is False


async def test_a_server_that_turned_pings_off_gets_no_followup_and_reports_no_ping(tmp_path):
    h, _ = await followup_world(tmp_path, "off")
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_shift SET ping = 'none' WHERE guild_id = 1")
    outcomes, _, _ = await deliver(h)

    assert all(o.pinged is False and o.followups_posted == 0 for o in outcomes)


async def test_a_followup_whose_codes_all_expired_before_sending_reports_nothing(tmp_path):
    h, codes = await followup_world(tmp_path, "dead")
    # Every queued code is dead by the time the turn reads it.
    with patch.object(fanout, "expired_by_expiry", lambda details, now: True):
        outcomes, made, _ = await deliver(h)

    assert all(o.followups_posted == 0 and o.pinged is False and o.error is None for o in outcomes)
    assert all(p.sent == [] for p in made)


async def test_a_followup_whose_ping_discord_permissions_dropped_does_not_report_pinged(tmp_path):
    """The task text: a follow-up stripped by Discord's permissions must not set `pinged`."""
    h, _ = await followup_world(tmp_path, "perm")
    channel = FakeChannel(can_mention_everyone=False)
    h.channels = {1: channel}

    [outcome] = await fanout.deliver_queued_codes(h.deps())

    assert len(channel.sent) == 1  # it posted, with the mention Discord then dropped
    assert outcome.followups_posted == 1
    assert outcome.pinged is False


async def test_a_role_followup_the_bot_cannot_mention_does_not_report_pinged(tmp_path):
    h, _ = await followup_world(tmp_path, "permrole", ping=ROLE)
    channel = FakeChannel(
        roles={int(ROLE): FakeRole(mentionable=False)}, can_mention_everyone=False
    )
    h.channels = {1: channel}

    [outcome] = await fanout.deliver_queued_codes(h.deps())

    assert outcome.followups_posted == 1
    assert outcome.pinged is False


async def test_a_followup_with_permission_on_the_real_poster_reports_pinged(tmp_path):
    h, _ = await followup_world(tmp_path, "permok")
    h.channels = {1: FakeChannel(can_mention_everyone=True)}

    [outcome] = await fanout.deliver_queued_codes(h.deps())

    assert outcome.followups_posted == 1 and outcome.pinged is True


# --- follow-up losses, and how the loop merges ---


async def test_a_lost_followup_mark_at_every_message_keeps_only_the_recorded_ones(tmp_path):
    for m, recorded in enumerate([0, 20, 40]):
        h, codes = await followup_world(tmp_path, f"fl{m}", burst_size=50)
        before = state(h.db_path)
        [outcome], made, _ = await deliver(h, lose=[(1, codes[m * 20])])

        assert outcome.error == ERROR, m
        assert outcome.followups_posted == recorded, m
        assert outcome.pinged is (m > 0), m
        assert recorded_delta(before, state(h.db_path), 1)["followups_posted"] == recorded
        assert len(made[0].sent) == m + 1  # the lost one went out; nothing after it did


async def test_a_lost_mark_on_a_rejected_followup_counts_nothing_and_no_ping(tmp_path):
    h, codes = await followup_world(tmp_path, "flx", burst_size=50)
    [outcome], _, _ = await deliver(h, script=[RuntimeError("gone")], lose=[(1, codes[20])])

    assert outcome.error == ERROR
    assert outcome.followups_posted == 0 and outcome.pinged is False


async def test_a_lost_followup_mark_keeps_the_finished_alert_turns_every_field(tmp_path):
    h = make_harness(tmp_path, "merge")
    add_guild(h.db_path, 1, ping="everyone")
    old = await burst(h, 25)
    official = [_code(i) for i in range(100, 103)]
    mega = [_code(i) for i in range(300, 310)]
    await queue(
        h,
        [sighting_item("Gearbox", "official", c, at=h.clock) for c in official]
        + [sighting_item(REDDIT, "community", *mega, at=h.clock)]
        + [sighting_item(BLUESKY, "community", c, at=h.clock) for c in old],
    )
    [outcome], _, _ = await deliver(h, lose=[(1, old[20])])  # the second follow-up message

    assert outcome.error == ERROR
    assert outcome.posted == 3 and outcome.roundup_posted == 10
    assert outcome.followups_posted == 20  # the first message was recorded, the second wasn't
    assert outcome.pinged is True


async def test_an_unpinged_alert_turn_then_a_lost_first_followup_reports_no_ping(tmp_path):
    h = make_harness(tmp_path, "merge2")
    add_guild(h.db_path, 1, ping="everyone")
    old = await burst(h, 1)
    await queue(
        h,
        [sighting_item(REDDIT, "community", _code(100), at=h.clock)]
        + [sighting_item(BLUESKY, "community", c, at=h.clock) for c in old],
    )
    [outcome], made, _ = await deliver(h, lose=[(1, old[0])])

    assert [a.ping for p in made for a in p.sent] == [False, True]
    assert outcome.error == ERROR and outcome.posted == 1
    assert outcome.followups_posted == 0 and outcome.pinged is False


async def test_a_lost_followup_mark_keeps_the_next_server_turn(tmp_path):
    h = make_harness(tmp_path, "next")
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    codes = await burst(h, 1)
    await queue(h, [sighting_item(BLUESKY, "community", c, at=h.clock) for c in codes])

    outcomes, _, _ = await deliver(h, lose=[(1, codes[0])])

    assert [(o.guild_id, o.error) for o in outcomes] == [(1, ERROR), (2, None)]
    assert outcomes[1].followups_posted == 1 and outcomes[1].pinged is True


# --- None-safety and the loop's other exits ---


def test_a_fresh_mark_lost_error_has_no_partial_and_instances_do_not_share_one():
    a, b = MarkLostError("x"), MarkLostError("y")
    assert a.partial is None and b.partial is None
    a.partial = GuildOutcome(1, posted=3)
    assert b.partial is None and MarkLostError.partial is None


@pytest.mark.parametrize("which", ["one", "followups", "batch_first", "batch_second"])
async def test_a_mark_lost_error_with_no_partial_still_yields_a_plain_errored_outcome(
    tmp_path, which
):
    h = make_harness(tmp_path, f"bare_{which}")
    add_guild(h.db_path, 1, ping="everyone")
    mega = [_code(i) for i in range(300, 310)]
    await queue(
        h,
        [sighting_item("Gearbox", "official", _code(1), at=T0)]
        + [sighting_item(REDDIT, "community", *mega, at=T0)],
    )
    real_batch = fanout._post_batch
    calls = {"n": 0}

    async def bare_batch(*args, **kw):
        calls["n"] += 1
        if calls["n"] == (1 if which == "batch_first" else 2):
            raise MarkLostError("busy")
        return await real_batch(*args, **kw)

    async def bare(*args, **kw):
        raise MarkLostError("busy")

    target = {
        "one": ("_deliver_one", bare),
        "followups": ("_deliver_followups", bare),
        "batch_first": ("_post_batch", bare_batch),
        "batch_second": ("_post_batch", bare_batch),
    }[which]
    script_posts(h)
    with patch.object(fanout, *target):
        [outcome] = await fanout.deliver_queued_codes(h.deps())

    assert outcome.error == ERROR and outcome.guild_id == 1
    assert outcome.pinged is False or which in ("followups", "batch_second")


async def test_a_generic_error_after_a_finished_alert_turn_stays_a_plain_error(tmp_path):
    h = make_harness(tmp_path, "generic")
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await queue(h, [sighting_item("Gearbox", "official", _code(1), at=T0)])
    real = fanout._deliver_followups

    async def boom(deps, guild_id):
        if guild_id == 1:
            raise RuntimeError("followup bug")
        return await real(deps, guild_id)

    script_posts(h)
    with patch.object(fanout, "_deliver_followups", boom):
        outcomes = await fanout.deliver_queued_codes(h.deps())

    assert outcomes[0] == GuildOutcome(1, error="followup bug")
    assert outcomes[1].guild_id == 2 and outcomes[1].error is None and outcomes[1].posted == 1


async def test_preview_never_runs_followups_and_reports_the_alert_turns_ping_alone(tmp_path):
    h = make_harness(tmp_path, "prev")
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 1)
    await queue(
        h,
        [sighting_item(BLUESKY, "community", c, at=h.clock) for c in codes]
        + [sighting_item(REDDIT, "community", _code(100), at=h.clock)],
    )
    outcomes, _, _ = await deliver(h, preview=True)

    assert all(o.followups_posted == 0 for o in outcomes)
    assert all(o.pinged is False for o in outcomes)  # only an unpinged community code was queued
