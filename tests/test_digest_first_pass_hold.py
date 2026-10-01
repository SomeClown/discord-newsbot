"""After a start, no digest posts before the first collection pass is over.

The dev bot once started at 13:52 with yesterday's digest still owed, posted it at 13:53, and
only then finished its first pass (149 new items), so every game read "0 items". The minute
job now waits for that pass (however it ends), unless the database already shows a recent one,
and never for more than 15 minutes. These tests use the cutover world: the real `on_ready`,
the real minute job, fake channels and a clock the tests move.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cutover_world import PAL_CH, TODAY, collect_these

import newsbot.bot.client as client_module
from newsbot.pipeline.collect import CollectionOutcome

# 13:52 Pacific: the digest due at 09:00 is owed, nobody has collected anything yet.
STARTED = datetime(2026, 10, 1, 20, 52, tzinfo=UTC)

PASS_ITEM = {
    "url": "https://example.com/palworld/first-pass",
    "title": "Palworld v0.6.3 Patch Notes",
    "excerpt": "Fixes a few crashes.",
    "source_name": "Palworld Steam",
    "trust": "official",
    "published_at": "2026-10-01T17:00:00+00:00",
    "topics": ["palworld"],
}


def _digests(world) -> list[tuple]:
    return world.rows("SELECT guild_id FROM digests WHERE run_date = ?", TODAY)


async def test_an_owed_digest_waits_for_the_first_pass_and_includes_its_items(
    make_world, monkeypatch, tmp_path, caplog
):
    caplog.set_level("INFO", logger="newsbot.bot.client")
    world = await make_world(now=STARTED)
    collect_these(monkeypatch, tmp_path, [PASS_ITEM])
    await world.ready(hold=True)

    await world.tick(STARTED + timedelta(minutes=1))
    assert world.sent(PAL_CH) == [] and _digests(world) == []

    await world.tick(STARTED + timedelta(minutes=2))  # still waiting, and still quiet
    assert world.sent(PAL_CH) == []

    world.set_now(STARTED + timedelta(seconds=30 + 49))
    await world.bot._collection_job()
    await world.tick(STARTED + timedelta(minutes=3))

    (post,) = world.sent(PAL_CH)
    assert "palworld/first-pass" in post.embed.description  # the pass's item, not a gap
    assert len(_digests(world)) == 1
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("holding digests until the first collection pass finishes") == 1
    assert messages.count("releasing held digests") == 1


async def test_a_recent_pass_in_the_database_means_no_wait(make_world):
    world = await make_world(now=STARTED)
    world.run(
        "INSERT INTO alert_state (key, value) VALUES ('last_sweep_at', ?)",
        (STARTED - timedelta(minutes=40)).isoformat(),
    )

    await world.ready(hold=True)

    assert world.bot._digests_held is False


@pytest.mark.parametrize("age", [timedelta(minutes=61), timedelta(days=2)])
async def test_an_old_pass_in_the_database_still_waits(make_world, age):
    world = await make_world(now=STARTED)
    world.run(
        "INSERT INTO alert_state (key, value) VALUES ('last_sweep_at', ?)",
        (STARTED - age).isoformat(),
    )

    await world.ready(hold=True)

    assert world.bot._digests_held is True


async def test_a_failed_first_pass_releases_the_hold(make_world, monkeypatch):
    world = await make_world(now=STARTED)
    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    async def boom(deps):
        raise RuntimeError("the feed fell over")

    monkeypatch.setattr(world.bot, "alert", fake_alert)
    monkeypatch.setattr(client_module, "run_collection", boom)
    await world.ready(hold=True)
    assert world.bot._digests_held is True

    await world.bot._collection_job()

    assert world.bot._digests_held is False
    assert any("collection pass crashed" in a for a in alerts)


async def test_a_skipped_first_pass_does_not_release_the_hold(make_world, monkeypatch):
    # Skipped means the run lock was busy and nothing was collected; the cap covers it.
    world = await make_world(now=STARTED)

    async def skipped(deps):
        return CollectionOutcome(skipped=True)

    monkeypatch.setattr(client_module, "run_collection", skipped)
    await world.ready(hold=True)

    await world.bot._collection_job()

    assert world.bot._digests_held is True


async def test_the_hold_gives_up_after_fifteen_minutes(make_world, caplog):
    caplog.set_level("INFO", logger="newsbot.bot.client")
    world = await make_world(now=STARTED)
    await world.ready(hold=True)  # and no collection pass ever arrives

    await world.tick(STARTED + timedelta(minutes=14, seconds=59))
    assert world.bot._digests_held is True and _digests(world) == []

    await world.tick(STARTED + timedelta(minutes=15))

    assert world.bot._digests_held is False
    assert len(_digests(world)) == 1  # posted anyway, gaps and all
    released = [r for r in caplog.records if r.getMessage() == "releasing held digests"]
    assert len(released) == 1


async def test_a_reconnect_does_not_hold_again(make_world):
    world = await make_world(now=STARTED)
    await world.ready()  # the hold is lifted, as the first pass would have done

    await world.bot.on_ready()  # a gateway reconnect

    assert world.bot._digests_held is False
