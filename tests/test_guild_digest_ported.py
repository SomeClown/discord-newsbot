"""The v2 daily run's guard, report and notice assertions, asked of the per-server digest.

`run_daily` (one server, one claim, one report) retired with the cutover, and so did its
tests (`test_run_daily_state_machine.py`, `test_run_daily_report.py` and
`test_pipeline_integration.py`). Most of what they pinned was already asked of the per-server
digest in `test_guild_digest.py` and `test_guild_digest_adversarial.py`; the report-side
rules and a handful of guard cases were not, and they live here, in the same world
(a real temp database, the real `DiscordPublisher`, fake channels):

- the run report goes out once per posted digest, only after the row is saved, never for a
  failed, refused or previewed run, and a broken or cancelled report can't touch the outcome;
- a forced re-run reports again, and the report says why the run happened;
- a v2.2 `pending` row waits for an admin, and a preview doesn't care that it's there;
- the failure notice names what posted and what didn't, including "none" when nothing landed.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from test_guild_digest import DAY, G1, World, in_window

from newsbot.pipeline import guild_digest
from newsbot.pipeline.guild_digest import (
    preview_guild_digest,
    run_due_guilds,
    run_guild_digest,
)
from newsbot.pipeline.run import RunKind


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def one_digest_day(world) -> None:
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())


# --- the run report ---


async def test_the_report_names_why_the_run_happened(world):
    one_digest_day(world)
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    await run_guild_digest(world.deps(), G1, kind=RunKind.RUN_NOW, force=True)

    scheduled, run_now = (text for _g, _c, text in world.reports)
    assert "(scheduled)" in scheduled
    assert "(run-now)" in run_now


async def test_a_forced_rerun_sends_a_second_report(world):
    one_digest_day(world)
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)

    await run_guild_digest(world.deps(), G1, kind=RunKind.RUN_NOW, force=True)

    assert len(world.reports) == 2


async def test_a_refused_run_sends_no_report(world):
    one_digest_day(world)
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    world.reports.clear()

    refused = await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)  # today is already done

    assert refused.status == "skipped"
    assert world.reports == []


async def test_a_preview_sends_no_report_and_writes_no_row(world):
    one_digest_day(world)

    preview = await preview_guild_digest(world.deps(), G1)

    assert preview is not None and preview.rendered.messages
    assert world.reports == [] and world.digest(G1) is None
    assert all(not world.channels[c].embeds for c in (11, 12))


async def test_the_report_goes_out_only_after_the_digest_row_is_saved(world):
    one_digest_day(world)
    seen: list[str | None] = []

    async def send_report(guild_id: int, channel_id: int, text: str) -> None:
        row = world.digest(guild_id)
        seen.append(row.status if row else None)

    await run_guild_digest(world.deps(send_report=send_report), G1, kind=RunKind.SCHEDULED)

    assert seen == ["ok"]  # never "pending", and never before there was a row


async def test_a_report_that_cant_be_rendered_does_not_change_the_outcome(world, monkeypatch):
    one_digest_day(world)

    def boom(**kwargs):
        raise ValueError("the renderer has a bug")

    monkeypatch.setattr(guild_digest, "render_guild_run_report", boom)

    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)

    assert outcome.status == "ok" and world.digest(G1).status == "ok"
    assert world.reports == []


async def test_a_cancel_during_the_report_does_not_flip_a_saved_digest_to_failed(world):
    one_digest_day(world)

    async def send_report(guild_id: int, channel_id: int, text: str) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_guild_digest(world.deps(send_report=send_report), G1, kind=RunKind.SCHEDULED)

    row = world.digest(G1)
    assert row.status == "ok"  # it posted; a deploy landing in the report doesn't unpost it
    assert set(row.posted_by_game) == {"borderlands4", "palworld"}


# --- the guard (v2: test_run_daily_state_machine.py) ---


async def test_a_preview_ignores_a_pending_row_left_by_v22(world):
    one_digest_day(world)
    with world.conn() as conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
            "updated_at) VALUES (?, ?, 'pending', '[]', 't', 't')",
            (G1, DAY.isoformat()),
        )
        conn.commit()

    preview = await preview_guild_digest(world.deps(), G1)

    assert preview is not None and len(preview.rendered.messages) == 2
    assert world.digest(G1).status == "pending"  # untouched


async def test_a_plain_run_is_blocked_by_a_pending_row_and_a_forced_run_reclaims_it(world):
    one_digest_day(world)
    with world.conn() as conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
            "updated_at) VALUES (?, ?, 'pending', '[]', 't', 't')",
            (G1, DAY.isoformat()),
        )
        conn.commit()
    deps = world.deps()

    plain = await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW)
    assert plain.status == "skipped"
    assert world.channels[11].embeds == [] and world.channels[12].embeds == []

    forced = await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True)
    assert forced.status == "ok"
    assert len(world.channels[11].embeds) == 1 and len(world.channels[12].embeds) == 1
    assert world.digest(G1).status == "ok"


# --- the failure notice (v2: the publish-failed alert in test_publish_with_retry.py) ---


async def test_the_failure_notice_says_none_when_nothing_posted(world):
    one_digest_day(world)
    world.channels[11].fail = [aiohttp.ClientError("reset")] * 4  # the first game never lands

    outcome = await run_due_guilds(world.deps())

    assert [o.status for o in outcome] == ["failed"]
    [(guild_id, text)] = world.notices
    assert guild_id == G1
    assert "Posted: none" in text
    assert "didn't post: Borderlands 4, Palworld" in text


async def test_the_failure_notice_names_what_posted_and_what_did_not(world):
    one_digest_day(world)
    world.channels[12].fail = [aiohttp.ClientError("reset")] * 4  # palworld never lands

    await run_due_guilds(world.deps())

    [(_, text)] = world.notices
    assert "Posted: Borderlands 4" in text and "didn't post: Palworld" in text
    assert world.reports == []  # a failure gets a notice, not a run report
