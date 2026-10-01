"""Startup at the v3 cutover: the order is fixed, and nothing in it is allowed to be fatal.

`python -m newsbot` has two ways to ruin a morning. It can refuse to start (the import
failed, the config is wrong), which is loud, which is fine, and which a restart loop will
tell me about. Or it can start and quietly never turn the every-minute digest job on,
which is silent, and which nobody finds out about until about 9:04 a.m. This file is mostly
about the second kind: seven `on_ready` steps, any one of which may blow up, and the one
promise that matters is that the bot still ends up with the minute job running.

Also here, because they're the same worry in different clothes: a Discord reconnect fires
`on_ready` again and must not redo the startup work or switch the gate back off; the
scheduler is allowed to fire the minute job early (it does, if the first tick lands inside
startup) and the job has to shrug; and the jobs themselves have settings (`max_instances`,
`coalesce`, how late is too late) that are the difference between a slow digest and a
digest posted twice.

No gateway, no network: a world from `cutover_world.py`, fake channels, and a `StubBot`
for the parts of `__main__` that stop before the bot exists.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from cutover_world import DUE, FRIEND, FRIEND_ADMIN_CH, OWNER_CH, PRODLIKE, TODAY, v22_digest
from pydantic import SecretStr

import newsbot.__main__ as entrypoint
import newsbot.bot.client as client_module
from newsbot.config import Secrets
from newsbot.store import repo
from newsbot.store.db import connect

T_NOON = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)

STEPS = (
    ("_send_import_notice", "import notice"),
    # QA M1: startup delivery runs after reconcile, and the hourly collection is
    # scheduled by the last step instead of by setup_hook.
    ("_reconcile", "reconcile"),
    ("_recover_codes", "pending codes"),
    ("reload_lounges", "lounge reload"),
    ("chunk_lounge_guilds", "lounge chunking"),
    ("schedule_lounge_quotes", "lounge quotes"),
    ("_permission_sweep", "permission sweep"),
    ("_start_collection_job", "collection job"),
)


def spy_on_steps(bot, monkeypatch, *, raising=()):
    """Replace every startup step with a recorder; the names in `raising` raise as well."""
    ran: list[str] = []

    def make(name):
        async def step(*args, **kwargs):
            ran.append(name)
            if name in raising:
                raise RuntimeError(f"{name} has a bug")
            return []

        return step

    for attr, name in STEPS:
        monkeypatch.setattr(bot, attr, make(name))
    return ran


# --- `__main__`, up to the point where the bot exists ---


class StubBot:
    built: list[dict] = []

    def __init__(self, cfg, secrets, db_path, **kwargs) -> None:
        type(self).built.append({"db_path": db_path, **kwargs})

    def run(self, token: str, **kwargs) -> None:
        return None


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    StubBot.built = []
    monkeypatch.setenv("NEWSBOT_CONFIG", str(PRODLIKE))
    monkeypatch.setenv("NEWSBOT_DB", str(tmp_path / "data" / "newsbot.db"))
    monkeypatch.setattr(
        entrypoint,
        "load_secrets",
        lambda: Secrets(
            discord_token=SecretStr("not-a-real-token"),
            anthropic_api_key=SecretStr("k"),
            brave_api_key=None,
            bluesky_handle=None,
            bluesky_app_password=None,
        ),
    )
    monkeypatch.setattr(entrypoint, "NewsBot", StubBot)
    return tmp_path


def test_no_config_path_exits_2_before_touching_anything(main_env, monkeypatch, capsys):
    monkeypatch.delenv("NEWSBOT_CONFIG")

    assert entrypoint.main() == 2

    assert "NEWSBOT_CONFIG is required" in capsys.readouterr().err
    assert not (main_env / "data").exists() and StubBot.built == []


def test_a_config_that_will_not_load_exits_2_before_creating_a_database(
    main_env, monkeypatch, tmp_path, capsys
):
    broken = tmp_path / "broken.yaml"
    broken.write_text("guild_id: [not, an, int]\n")
    monkeypatch.setenv("NEWSBOT_CONFIG", str(broken))

    assert entrypoint.main() == 2

    assert capsys.readouterr().err.strip()
    assert not (main_env / "data" / "newsbot.db").exists() and StubBot.built == []


def test_an_import_that_fails_leaves_no_trace_in_the_database(
    main_env, monkeypatch, tmp_path, capsys
):
    games = "\n".join(f"  - {{key: g{i}, name: 'G{i}', channel_id: {i + 1}}}" for i in range(11))
    over_limit = tmp_path / "eleven-games.yaml"
    over_limit.write_text(
        f"guild_id: 1\ndigest: {{time: '09:00', timezone: UTC}}\ntopics:\n{games}\nsources: []\n"
    )
    monkeypatch.setenv("NEWSBOT_CONFIG", str(over_limit))

    assert entrypoint.main() == 2

    assert "can follow at most 10" in capsys.readouterr().err
    with closing(connect(main_env / "data" / "newsbot.db")) as conn:
        assert repo.list_guilds(conn) == []
        assert repo.app_state_get(conn, "import") is None  # a retry has to be able to try again
    assert StubBot.built == []


def test_a_failure_listing_the_guilds_to_adopt_for_still_starts_the_bot(main_env, monkeypatch):
    def boom(conn):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(entrypoint.repo, "list_guilds", boom)

    assert entrypoint.main() == 0

    ((built,),) = [StubBot.built]
    assert built["import_report"] is not None  # the notice for the owner still travels along
    assert [row.guild_id for row in built["lounges"]] == [FRIEND]  # and the lounge still loads


def test_a_failed_resync_still_reads_the_lounge_rows_the_import_wrote(main_env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("config and database disagree")

    monkeypatch.setattr(entrypoint, "resync_lounge_from_config", boom)

    assert entrypoint.main() == 0

    ((built,),) = [StubBot.built]
    assert built["intents"].members is True  # built from the imported row, resync or not


def test_deleting_the_lounge_block_later_leaves_the_imported_row_alone(
    main_env, monkeypatch, tmp_path
):
    # The owner is told to delete the old keys once the import has run. After that the row
    # in the database is the lounge, and a start with no `lounge:` block must not touch it.
    assert entrypoint.main() == 0
    trimmed = tmp_path / "no-lounge.yaml"
    text = PRODLIKE.read_text()
    trimmed.write_text(text[: text.index("lounge:")])
    monkeypatch.setenv("NEWSBOT_CONFIG", str(trimmed))
    StubBot.built.clear()

    assert entrypoint.main() == 0

    ((built,),) = [StubBot.built]
    assert [row.guild_id for row in built["lounges"]] == [FRIEND]
    assert built["lounges"][0].welcome_enabled is True
    assert built["intents"].members is True


def test_a_config_with_no_lounge_at_all_starts_without_the_members_intent(
    main_env, monkeypatch, tmp_path
):
    trimmed = tmp_path / "no-lounge.yaml"
    text = PRODLIKE.read_text()
    trimmed.write_text(text[: text.index("lounge:")])
    monkeypatch.setenv("NEWSBOT_CONFIG", str(trimmed))

    assert entrypoint.main() == 0

    ((built,),) = [StubBot.built]
    assert built["lounges"] == [] and built["intents"].members is False


# --- on_ready: any one step may fail, none may stop the rest ---


@pytest.mark.parametrize("failing", [name for _, name in STEPS])
async def test_whichever_step_breaks_the_rest_run_and_the_minute_job_comes_on(
    make_world, monkeypatch, failing
):
    world = await make_world(now=T_NOON)
    ran = spy_on_steps(world.bot, monkeypatch, raising=(failing,))

    await world.ready()

    assert ran == [name for _, name in STEPS]
    assert world.bot._digests_enabled is True
    told = [m.content for m in world.sent(FRIEND_ADMIN_CH)]
    assert [t for t in told if f"{failing!r}" in t] != []  # the owner hears which step it was


async def test_every_step_breaking_at_once_still_turns_the_minute_job_on(make_world, monkeypatch):
    world = await make_world(now=T_NOON)
    ran = spy_on_steps(world.bot, monkeypatch, raising={name for _, name in STEPS})

    await world.ready()

    assert len(ran) == len(STEPS) and world.bot._digests_enabled is True


async def test_an_owner_channel_that_cannot_be_posted_to_does_not_stop_startup(
    make_world, monkeypatch
):
    # The alert about a failed step is itself a send. If that send is what's broken (the
    # channel was deleted, the role revoked), it must not become the thing that skips
    # the remaining steps.
    import discord

    world = await make_world(now=T_NOON)
    world.channels[FRIEND_ADMIN_CH].fail_with = discord.Forbidden(
        type("R", (), {"status": 403, "reason": "no"})(), "Missing Access"
    )
    ran = spy_on_steps(world.bot, monkeypatch, raising=("reconcile", "lounge reload"))

    await world.ready()

    assert len(ran) == len(STEPS) and world.bot._digests_enabled is True


async def test_no_owner_channel_at_all_is_not_a_startup_problem(make_world):
    from newsbot.config import load_config

    cfg = load_config(PRODLIKE).model_copy(update={"admin_channel_id": None})
    world = await make_world(now=T_NOON, cfg=cfg)

    await world.ready()

    assert world.bot._digests_enabled is True
    assert (
        world.sent(FRIEND_ADMIN_CH) == []
    )  # the import notice had nowhere to go, and went nowhere


async def test_a_reconnect_redoes_nothing_and_does_not_close_the_gate(make_world, monkeypatch):
    world = await make_world(now=T_NOON)
    chunks: list[int] = []

    async def counting_chunk():
        chunks.append(1)

    monkeypatch.setattr(world.bot, "chunk_lounge_guilds", counting_chunk)
    await world.ready()
    jobs = {job.id for job in world.bot.scheduler.get_jobs()}
    told = len(world.sent(FRIEND_ADMIN_CH))

    await world.ready()
    await world.ready()

    assert chunks == [1]  # one chunking storm, not three
    assert {job.id for job in world.bot.scheduler.get_jobs()} == jobs
    assert len(world.sent(FRIEND_ADMIN_CH)) == told  # no second import notice, no repeat alerts
    assert world.bot._digests_enabled is True


async def test_a_reconnect_does_not_retry_a_step_that_failed_the_first_time(
    make_world, monkeypatch
):
    # Pinned, not praised: a reconcile that failed at boot waits for the next restart. That is
    # the price of "first connection only", and the owner was told which step it was.
    world = await make_world(now=T_NOON)
    ran = spy_on_steps(world.bot, monkeypatch, raising=("reconcile",))

    await world.ready()
    await world.ready()

    assert ran.count("reconcile") == 1


async def test_a_second_on_ready_while_the_first_is_mid_startup_neither_repeats_nor_enables(
    make_world, monkeypatch
):
    world = await make_world(now=T_NOON)
    gate = asyncio.Event()
    ran = spy_on_steps(world.bot, monkeypatch)

    async def slow_reconcile(*args, **kwargs):
        ran.append("reconcile")
        await gate.wait()

    monkeypatch.setattr(world.bot, "_reconcile", slow_reconcile)
    first = asyncio.create_task(world.bot.on_ready())
    await asyncio.sleep(0.05)

    await world.bot.on_ready()  # a reconnect while the first is still reconciling

    assert world.bot._digests_enabled is False  # the early return must not flip the gate
    assert ran.count("reconcile") == 1
    gate.set()
    await first
    assert world.bot._digests_enabled is True and ran.count("reconcile") == 1


async def test_the_gate_stays_shut_while_the_last_step_is_still_running(make_world, monkeypatch):
    world = await make_world(now=T_NOON)
    gate = asyncio.Event()
    spy_on_steps(world.bot, monkeypatch)

    async def slow_sweep(*args, **kwargs):
        await gate.wait()

    monkeypatch.setattr(world.bot, "_permission_sweep", slow_sweep)
    v22_digest(world.db_path, "2026-09-30", "ok")  # nothing about today, so a tick would post
    task = asyncio.create_task(world.bot.on_ready())
    await asyncio.sleep(0.05)

    await world.tick(DUE)

    assert [m for ch in world.channels.values() for m in ch.sent if m.embed] == []
    gate.set()
    await task


async def test_the_scheduler_firing_the_minute_job_early_does_nothing(make_world, monkeypatch):
    # The real scheduler, on the real loop: ask it to run the job now, before on_ready.
    world = await make_world(now=T_NOON)
    fired = asyncio.Event()

    async def spy(deps):
        fired.set()
        return []

    monkeypatch.setattr(client_module, "run_due_guilds", spy)
    job = world.bot.scheduler.get_job("guild-digests")

    job.modify(next_run_time=datetime.now(UTC))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fired.wait(), timeout=0.4)

    await world.ready()
    job.modify(next_run_time=datetime.now(UTC))
    await asyncio.wait_for(fired.wait(), timeout=3)  # and once ready, the same job does run


async def test_after_startup_the_jobs_are_the_v3_set_and_none_of_the_v2_ones(make_world):
    world = await make_world(now=T_NOON)

    await world.ready()

    ids = {job.id for job in world.bot.scheduler.get_jobs()}
    assert ids == {
        "collection",
        "guild-digests",
        "summaries",
        "retention",
        "owner-report",
        "heartbeat",
        f"daily-quote-{FRIEND}",
    }
    assert not ids & {"daily-digest", "daily-quote", "code-sweep"}  # v2's names


# --- the jobs' own settings ---


async def test_how_late_each_job_may_run_and_how_many_may_overlap(make_world):
    world = await make_world(now=T_NOON)
    await world.ready()  # the collection job is scheduled by on_ready
    jobs = {job.id: job for job in world.bot.scheduler.get_jobs()}

    settings = {
        job_id: (job.max_instances, job.coalesce, job.misfire_grace_time)
        for job_id, job in jobs.items()
    }

    assert settings["guild-digests"] == (1, True, 30)
    assert settings["summaries"] == (1, True, 120)
    assert settings["retention"] == (1, True, 3600)
    assert settings["owner-report"] == (1, True, 3600)
    assert settings["collection"][:2] == (1, True)
    # The heartbeat only has to exist; two of them overlapping would be harmless but silly.
    assert settings["heartbeat"][:2] == (1, True)


async def test_the_owner_report_follows_the_configured_time_and_zone(v3_cfg, v22_db, monkeypatch):
    from cutover_world import secrets

    from newsbot.bot.client import NewsBot
    from newsbot.config import OwnerReportCfg

    cfg = v3_cfg.model_copy(
        update={"owner_report": OwnerReportCfg(time="07:45", timezone="Europe/Berlin")}
    )
    bot = NewsBot(cfg, secrets(), str(v22_db))

    async def fake_sync(*args, **kwargs):
        return []

    bot.tree.sync = fake_sync  # type: ignore[method-assign]
    await bot.setup_hook()
    try:
        job = bot.scheduler.get_job("owner-report")
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("7", "45")
        assert str(job.trigger.timezone) == "Europe/Berlin"
        assert str(bot.scheduler.timezone) == "Europe/Berlin"
    finally:
        bot.scheduler.shutdown(wait=False)
        await bot.http_client.aclose()


async def test_the_heartbeat_waits_for_the_gateway_and_never_writes_when_not_ready(
    make_world, monkeypatch, tmp_path
):
    world = await make_world(now=T_NOON)
    target = tmp_path / "beat"
    monkeypatch.setattr(client_module, "HEARTBEAT", target)

    await world.bot._heartbeat_job()  # not ready: no gateway, so the healthcheck should fail

    assert not target.exists()


# --- retention ---


def _old(days: int, hours: int = 0) -> datetime:
    return datetime.now(UTC) - timedelta(days=days, hours=hours)


def _summary(world, key: str, created: datetime) -> int:
    with closing(connect(world.db_path)) as conn, conn:
        return conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "created_at) VALUES (?, '2026-07-01', 'ok', ?, ?, ?)",
            (key, created.isoformat(), created.isoformat(), created.isoformat()),
        ).lastrowid


async def test_retention_keeps_everything_inside_90_days_and_what_the_next_digest_chains_on(
    make_world,
):
    world = await make_world(now=T_NOON)
    _summary(world, "recent-a", _old(89, 23))
    _summary(world, "recent-b", _old(1))
    _summary(world, "ancient", _old(91))
    with closing(connect(world.db_path)) as conn, conn:
        repo.add_notice(conn, FRIEND, "fresh notice", now=lambda: _old(2))
        repo.add_notice(conn, FRIEND, "ancient notice", now=lambda: _old(120))

    await world.bot._retention_job()

    kept = [r[0] for r in world.rows("SELECT game_key FROM game_summaries ORDER BY id")]
    assert kept == ["recent-a", "recent-b"]
    assert [r[0] for r in world.rows("SELECT text FROM guild_notices")] == ["fresh notice"]


async def test_retention_does_not_touch_digests_or_codes_or_the_pending_work(make_world):
    world = await make_world(now=T_NOON)
    v22_digest(world.db_path, "2026-04-01", "pending")  # ancient, and still not retention's
    before = {
        table: world.rows(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        for table in ("digests", "alerted_codes", "guild_code_posts", "guilds", "guild_games")
    }

    await world.bot._retention_job()

    after = {table: world.rows(f"SELECT COUNT(*) FROM {table}") for table in before}  # noqa: S608
    assert after == before


async def test_a_summary_older_than_its_stories_is_purged_without_taking_the_stories(make_world):
    # Retention deletes by `created_at`, and a story can in principle be younger than the
    # summary row it hangs off. The foreign key must null the link, not cascade or crash.
    world = await make_world(now=T_NOON)
    summary_id = _summary(world, "palworld", _old(91))
    with closing(connect(world.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, is_update_of, digest_id, "
            "summary_id, created_at) VALUES ('palworld', 'h', 's', 'official', NULL, NULL, ?, ?)",
            (summary_id, _old(10).isoformat()),
        )

    await world.bot._retention_job()

    assert world.rows("SELECT summary_id FROM stories WHERE headline = 'h'") == [(None,)]
    assert world.rows("SELECT COUNT(*) FROM game_summaries") == [(0,)]


async def test_a_retention_crash_alerts_the_owner_and_does_not_raise(make_world, monkeypatch):
    world = await make_world(now=T_NOON, owner_channel=True)

    def boom(cutoff):
        raise RuntimeError("disk is full")

    monkeypatch.setattr(world.bot, "_purge_sync", boom)

    await world.bot._retention_job()

    (alert,) = [m.content for m in world.sent(OWNER_CH)]
    assert "retention job crashed" in alert and "disk is full" in alert


# --- the day's rows survive an early startup on the same day ---


async def test_starting_up_does_not_invent_a_digest_row_for_today(make_world):
    world = await make_world(now=DUE - timedelta(hours=3))

    await world.ready()
    await world.tick(DUE - timedelta(hours=3))

    assert world.rows("SELECT COUNT(*) FROM digests WHERE run_date = ?", TODAY) == [(0,)]
