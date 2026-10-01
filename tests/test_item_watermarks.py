"""Item watermarks: an item lands in exactly one digest, even when its collection pass is slow.

A time window over `collected_at` loses items. A pass stamps its items with the instant it
*started* and stores them once every source has answered, so a pass that starts at 08:59:30 and
commits at 09:01 stores "08:59:30" items a minute after the 09:00 digest read its window and
closed it. The next window starts at 09:00, so nothing would ever pick them up. Digests (and
summaries) now cover item ids, which are handed out at commit: `(previous id, newest id stored by
the window's end]`. These tests store items the way a slow pass does (a stamp from before the
digest, written after it) and count how many digests show each one: it has to be one.

The digests here are real ones against a real database and fake channels, from the adversarial
digest file's rig. Items are named `item-N` so the rig can read them back out of the embeds.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import discord
import pytest
from test_guild_digest_adversarial import BL4, DAY, DUE, G1, G2, NOW, World, _Response, ch

from newsbot.pipeline.guild_digest import preview_guild_digest, run_due_guilds, run_guild_digest
from newsbot.pipeline.run import RunKind, _read_only_copy
from newsbot.store import repo
from newsbot.store.db import connect


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def shown(world, guild_id=G1, game_index=1) -> list[str]:
    return world.titles(ch(guild_id, game_index))


def times_shown(world, title, guild_id=G1) -> int:
    return shown(world, guild_id).count(title)


# --- the exact race ---


async def test_a_pass_that_starts_at_0859_and_commits_at_0901_is_in_the_next_digest_only(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=3))
    pass_started = DUE - timedelta(seconds=30)  # 08:59:30 PDT

    world.clock = DUE + timedelta(seconds=20)  # the 09:00 digest reads at 09:00:20
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-1"]

    # The pass finishes at 09:01 and stores its items, stamped with when it started.
    world.clock = DUE + timedelta(minutes=1)
    world.add_item(BL4, "item-2", pass_started)
    world.add_item(BL4, "item-3", pass_started)
    assert [times_shown(world, f"item-{n}") for n in (1, 2, 3)] == [1, 0, 0]

    # A restart in between changes nothing: the watermark is in the database.
    world.clock = DUE + timedelta(days=1, seconds=20)
    await run_due_guilds(world.deps())
    assert [times_shown(world, f"item-{n}") for n in (1, 2, 3)] == [1, 1, 1]  # never in neither

    world.clock = DUE + timedelta(days=2, seconds=20)
    await run_due_guilds(world.deps())
    assert [times_shown(world, f"item-{n}") for n in (1, 2, 3)] == [1, 1, 1]  # and never twice


async def test_the_same_race_through_a_run_now(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=5))
    world.clock = DUE - timedelta(hours=1)  # an admin runs it at 08:00
    await run_guild_digest(world.deps(), G1, kind=RunKind.RUN_NOW)
    world.add_item(BL4, "item-2", world.clock - timedelta(seconds=30))  # a slow pass lands late

    world.clock = DUE + timedelta(days=1, seconds=20)
    await run_due_guilds(world.deps())
    assert [times_shown(world, f"item-{n}") for n in (1, 2)] == [1, 1]


async def test_an_item_stored_after_a_claim_waits_for_the_next_digest_even_on_a_resume(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    window = (DUE - timedelta(hours=24), DUE)
    with world.conn() as conn:
        claim = repo.claim_guild_digest(conn, G1, DAY, force=False, window=window, now=lambda: NOW)
    assert (claim.items_after, claim.items_upto) == (None, 1)
    # The process dies after claiming. A slow pass lands an item stamped before the window end.
    world.add_item(BL4, "item-2", DUE - timedelta(seconds=30), url="https://example.com/late")

    world.clock = NOW + timedelta(minutes=11)  # the lease has gone stale: the next tick resumes
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-1"]  # it reads what the claim fixed, so a resume is repeatable
    with world.conn() as conn:
        resumed = repo.get_guild_digest(conn, G1, DAY)
    assert resumed.status == "ok"

    world.clock = DUE + timedelta(days=1, seconds=20)
    await run_due_guilds(world.deps())
    assert [times_shown(world, f"item-{n}") for n in (1, 2)] == [1, 1]


async def test_a_clean_failure_retries_with_the_ids_it_was_claimed_with(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    world.channels[ch(G1, 1)].always_fail = discord_error()
    world.clock = DUE + timedelta(seconds=20)
    await run_due_guilds(world.deps())
    assert world.digest(G1).status == "failed"
    world.add_item(BL4, "item-2", DUE - timedelta(seconds=30))  # lands between the attempts
    world.channels[ch(G1, 1)].always_fail = None

    world.clock += timedelta(minutes=11)
    await run_due_guilds(world.deps())  # the retry
    assert shown(world) == ["item-1"]
    world.clock = DUE + timedelta(days=1, seconds=20)
    await run_due_guilds(world.deps())
    assert [times_shown(world, f"item-{n}") for n in (1, 2)] == [1, 1]


def discord_error():
    return discord.HTTPException(_Response(500), "boom")


# --- the seed: the first v3 digest after the import ---


async def test_the_first_digest_after_the_import_starts_after_the_adopted_v22_digest(world):
    """An adopted v2.2 digest has only a time window; the ids are worked out from its end."""
    world.add_guild(G1, games=(BL4,))
    v22_posted_at = DUE - timedelta(days=1) + timedelta(seconds=7)
    world.add_item(BL4, "item-1", v22_posted_at - timedelta(hours=3))  # v2.2 posted it
    world.add_item(BL4, "item-2", v22_posted_at - timedelta(seconds=5))  # so did this
    world.add_item(BL4, "item-3", v22_posted_at + timedelta(hours=1))  # v3's, after
    with world.conn() as conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, window_start, "
            "window_end, created_at, updated_at) VALUES (?, ?, 'ok', '[131]', ?, ?, 't', 't')",
            (
                G1,
                (DAY - timedelta(days=1)).isoformat(),
                (v22_posted_at - timedelta(hours=24)).isoformat(),
                v22_posted_at.isoformat(),
            ),
        )
        conn.commit()
    world.clock = DUE + timedelta(seconds=20)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-3"]
    with world.conn() as conn:
        row = conn.execute(
            "SELECT items_after, items_upto FROM digests WHERE run_date = ?", (DAY.isoformat(),)
        ).fetchone()
    assert tuple(row) == (2, 3)  # chained on the derived mark, and written down for next time


# --- DST and long runs ---


async def test_across_the_fall_back_day_every_item_lands_in_exactly_one_digest(world):
    """Hourly items through a 25-hour day, a slow pass at every digest: each once, none lost."""
    world.add_guild(G1, games=(BL4,))
    dues = [
        datetime(2026, 10, 30, 16, 0, tzinfo=UTC),  # 09:00 PDT
        datetime(2026, 10, 31, 16, 0, tzinfo=UTC),  # the 25-hour day starts: the next one is...
        datetime(2026, 11, 1, 17, 0, tzinfo=UTC),  # ...09:00 PST, 25 hours later
        datetime(2026, 11, 2, 17, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 17, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 17, 0, tzinfo=UTC),  # one more, to pick up the last slow pass
    ]
    n = 0
    at = dues[0] - timedelta(hours=23)
    for due in dues:
        while at <= due and due < dues[-1]:  # the hourly feed, stored as it arrives
            n += 1
            world.add_item(BL4, f"item-{n}", at)
            at += timedelta(hours=1)
        world.clock = due + timedelta(seconds=20)
        await run_due_guilds(world.deps())
        n += 1
        world.add_item(BL4, f"item-{n}", due - timedelta(seconds=30))  # stored after the digest

    everything = shown(world)
    assert sorted(everything, key=lambda t: int(t.split("-")[1])) == [
        f"item-{k}"
        for k in range(1, n)  # every item but the one stored after the last digest
    ]
    with world.conn() as conn:
        rows = conn.execute("SELECT run_date FROM digests WHERE guild_id = ?", (G1,)).fetchall()
    assert len(rows) == len(dues)  # one digest a local day, the fall-back day included


# --- storage details ---


def test_the_derived_mark_survives_an_id_restart_after_everything_was_purged(world):
    """If SQLite starts numbering again (every item purged), an old mark must not hide new items."""
    world.add_guild(G1, games=(BL4,))
    for n in range(1, 6):
        world.add_item(BL4, f"item-{n}", DUE - timedelta(hours=10 - n))
    with world.conn() as conn:
        claim = repo.claim_guild_digest(
            conn, G1, DAY, force=False, window=(DUE - timedelta(hours=24), DUE), now=lambda: NOW
        )
        repo.save_guild_digest(conn, claim.digest_id, "ok", {}, None, (claim.window_start, DUE))
        assert claim.items_upto == 5
        conn.execute("DELETE FROM items")  # retention took everything; ids start over at 1
        conn.commit()
    world.add_item(BL4, "item-9", DUE + timedelta(hours=2))  # id 1 again
    with world.conn() as conn:
        covered = repo.item_range(
            conn, G1, exclude_run_date=DAY + timedelta(days=1), end=DUE + timedelta(days=1)
        )
    # The stale mark (5) is higher than anything stored, so it's rederived from the window end.
    assert (covered.after, covered.upto) == (0, 1)


def test_one_servers_coverage_never_comes_from_anothers_digest(world):
    world.add_guild(G1, games=(BL4,))
    world.add_guild(G2, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    with world.conn() as conn:
        claim = repo.claim_guild_digest(
            conn, G1, DAY, force=False, window=(DUE - timedelta(hours=24), DUE), now=lambda: NOW
        )
        repo.save_guild_digest(conn, claim.digest_id, "ok", {}, None, (claim.window_start, DUE))
        later = DAY + timedelta(days=1)
        assert repo.last_coverage(conn, G1, exclude_run_date=later).item_id == 1
        assert repo.last_coverage(conn, G2, exclude_run_date=later) is None


async def test_the_items_floor_still_trims_a_long_backlog_but_ids_decide_the_rest(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=60))  # older than the 48 hour floor
    world.add_item(BL4, "item-2", DUE - timedelta(hours=40))
    with world.conn() as conn:
        claim = repo.claim_guild_digest(
            conn,
            G1,
            DAY - timedelta(days=3),
            force=False,
            window=(DUE - timedelta(days=4), DUE - timedelta(days=3)),
            now=lambda: NOW,
        )
        repo.save_guild_digest(
            conn, claim.digest_id, "ok", {}, None, (claim.window_start, claim.window_end)
        )
    world.clock = DUE + timedelta(seconds=20)
    await run_due_guilds(world.deps())
    assert shown(world) == ["item-2"]


# --- the dry-run copy ---


async def test_a_dry_run_copy_keeps_the_ids_so_a_preview_matches_the_real_digest(world, tmp_path):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE - timedelta(hours=2))
    world.clock = DUE + timedelta(seconds=20)
    await run_due_guilds(world.deps())
    world.add_item(BL4, "item-2", DUE - timedelta(seconds=30))  # the slow pass, after the digest
    world.clock = DUE + timedelta(days=1, seconds=20)

    with _read_only_copy(world.db_path) as copy:
        (tmp_path / "copy").mkdir()
        copied = World(tmp_path / "copy")
        copied.db_path = copy
        copied.clock = world.clock
        preview = await preview_guild_digest(copied.deps(), G1)
    [message] = preview.rendered.messages
    assert "item-2" in str(message.embed.description)
    assert "item-1" not in str(message.embed.description)


def test_the_database_file_is_the_same_before_and_after_a_copy(world):
    world.add_guild(G1, games=(BL4,))
    world.add_item(BL4, "item-1", DUE)
    with closing(sqlite3.connect(world.db_path)) as c:
        before = c.execute("SELECT id, url FROM items").fetchall()
    with _read_only_copy(world.db_path) as copy, closing(connect(copy)) as c:
        assert [tuple(r) for r in c.execute("SELECT id, url FROM items")] == before
