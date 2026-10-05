"""The v3 bot wiring: jobs, startup order, shared router and deps, and one whole day (task 13).

`NewsBot` is glue, and glue is where "it works in every unit test and does
nothing in production" lives, so this file checks the glue directly: which
jobs exist and on what triggers, that the every-minute digest job stays inert
until `on_ready` is done, the order `on_ready` does its work in, that there is
exactly one router and one `GuildDigestDeps` and that everything shares them,
and then one end-to-end day for the friend's imported server: a v2.2 database
plus the prod-like config lead to the import, a collection pass from fixtures,
the minute tick posting the digest into fake channels at 09:00 Pacific, and no
second post on the next tick.

It also holds what used to be the v2 scheduling-wiring tests
(`test_client_scheduling_adversarial.py`, retired with the daily job): the
collection job's interval and first run, fresh collectors and the shared Reddit
gap, the startup report of interrupted codes, and the crash-alerts-once rule,
now asked of the collection job instead of the sweep job.

No gateway, no network: `tree.sync` is a stub and the channels are fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from pydantic import SecretStr

import newsbot.bot.client as client_module
from newsbot.bot.client import DiscordCodeAlertPoster, DiscordPublisher, NewsBot
from newsbot.collectors.base import RateLimitState
from newsbot.config import Secrets, load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.guilds.notify import ClientSender, Router
from newsbot.pipeline.collect import CollectionOutcome
from newsbot.pipeline.run import StubLLM, build_fixture_collectors
from newsbot.store import repo
from newsbot.store.db import connect, migrate

PRODLIKE = Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml"
INTEGRATION_LLM = Path(__file__).parent / "fixtures" / "integration" / "llm.json"
HOME = 200000000000000001
OWNER_CHANNEL = 200000000000000002
LA = ZoneInfo("America/Los_Angeles")


def _secrets(*, brave: str | None = None) -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=SecretStr(brave) if brave else None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


async def _setup(bot: NewsBot) -> None:
    """Run `setup_hook` with the one REST call it makes (`tree.sync`) stubbed out."""

    async def _fake_sync(*args, **kwargs):
        return []

    bot.tree.sync = _fake_sync  # type: ignore[method-assign]
    await bot.setup_hook()


async def _stopped(bot: NewsBot) -> None:
    if bot.scheduler is not None:
        bot.scheduler.shutdown(wait=False)
    if bot.http_client is not None:
        await bot.http_client.aclose()


@pytest.fixture
async def bot(v3_cfg, db_path):
    b = NewsBot(v3_cfg, _secrets(), db_path)
    await _setup(b)
    yield b
    await _stopped(b)


def _alerts(bot: NewsBot, monkeypatch) -> list[str]:
    """Record the owner alerts instead of sending them."""
    sent: list[str] = []

    async def fake_alert(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(bot, "alert", fake_alert)
    return sent


# --- the job list and its triggers ---


async def test_setup_hook_adds_exactly_these_jobs(bot):
    # The per-server quote jobs (daily-quote-<guild>) and the hourly collection come from
    # `on_ready` (QA M1: a pass before the guild cache is warm saw no bot member); nothing
    # else is a job.
    assert {job.id for job in bot.scheduler.get_jobs()} == {
        "guild-digests",
        "summaries",
        "retention",
        "owner-report",
        "heartbeat",
    }


async def test_collection_job_follows_the_configured_interval(v3_cfg, db_path):
    cfg = v3_cfg.model_copy(
        update={"collection": v3_cfg.collection.model_copy(update={"interval_minutes": 15})}
    )
    b = NewsBot(cfg, _secrets(), db_path)
    try:
        await _setup(b)
        assert b.scheduler.get_job("collection") is None  # not before `on_ready`
        await b._start_collection_job()
        job = b.scheduler.get_job("collection")
        assert job.trigger.interval == timedelta(minutes=15)
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 15 * 60 // 2  # `collection_job_options`' own number
    finally:
        await _stopped(b)


async def test_collection_job_first_run_is_a_breath_after_on_ready(v3_cfg, db_path):
    tz = ZoneInfo(v3_cfg.owner_report.timezone)
    before = datetime.now(tz)
    b = NewsBot(v3_cfg, _secrets(), db_path)
    try:
        await _setup(b)
        await b._start_collection_job()
        delta = (b.scheduler.get_job("collection").next_run_time - before).total_seconds()
        # Generous bounds around the 30 second settle, to absorb however long setup took.
        assert 25 <= delta <= 45
    finally:
        await _stopped(b)


async def test_the_minute_the_summaries_and_the_heartbeat_triggers(bot):
    minute = bot.scheduler.get_job("guild-digests")
    assert minute.trigger.interval == timedelta(minutes=1)
    assert (minute.coalesce, minute.max_instances, minute.misfire_grace_time) == (True, 1, 30)
    summaries = bot.scheduler.get_job("summaries")
    assert summaries.trigger.interval == timedelta(minutes=5)
    assert (summaries.coalesce, summaries.max_instances) == (True, 1)
    assert bot.scheduler.get_job("heartbeat").trigger.interval == timedelta(seconds=60)


async def test_retention_and_the_owner_report_are_crons_in_the_owners_zone(bot, v3_cfg):
    for job_id, hour, minute in (("retention", "3", "30"), ("owner-report", "21", "0")):
        job = bot.scheduler.get_job(job_id)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == (hour, minute)
        assert str(job.trigger.timezone) == v3_cfg.owner_report.timezone
        assert job.coalesce is True and job.max_instances == 1


async def test_the_v3_command_set_is_registered_globally_even_with_alerts_off(bot):
    # v2 registered `/shift` only when alerts were on (D4). It's global now, whatever any
    # one server does with SHiFT.
    assert {c.name for c in bot.tree.get_commands()} == {"news", "newsbot", "shift"}
    assert {c.name for c in bot.tree.get_command("shift").commands} == {"codes"}


# --- the minute job is inert until on_ready is done ---


def _spy_ticks(monkeypatch) -> list[object]:
    seen: list[object] = []

    async def fake_run_due_guilds(deps):
        seen.append(deps)
        return []

    monkeypatch.setattr(client_module, "run_due_guilds", fake_run_due_guilds)
    return seen


async def test_the_minute_job_does_nothing_before_on_ready(bot, monkeypatch):
    seen = _spy_ticks(monkeypatch)

    await bot._digest_job()

    assert seen == []


async def test_the_minute_job_runs_once_on_ready_has_finished(bot, monkeypatch):
    # On ready, but held: a fresh start has no recent pass, so the first one goes first
    # (tests/test_digest_first_pass_hold.py has the rest of that story).
    seen = _spy_ticks(monkeypatch)

    await bot.on_ready()
    await bot._digest_job()
    assert seen == []

    bot._release_digest_hold("the first pass is over")
    await bot._digest_job()

    assert seen == [bot.guild_digest_deps()]


async def test_the_minute_job_is_not_enabled_by_a_reconnect_alone(bot, monkeypatch):
    # `on_ready` fires on every reconnect; only the first does the startup work.
    seen = _spy_ticks(monkeypatch)
    steps: list[str] = []

    async def counting_reload():
        steps.append("reload")
        return []

    monkeypatch.setattr(bot, "reload_lounges", counting_reload)

    await bot.on_ready()
    await bot.on_ready()

    assert steps == ["reload"]
    bot._release_digest_hold("the first pass is over")
    await bot._digest_job()
    assert len(seen) == 1


# --- the on_ready order ---


def _record_steps(bot: NewsBot, monkeypatch, *, boom: str | None = None) -> list[tuple[str, bool]]:
    """Replace every startup step with a recorder of (name, were the digests on yet)."""
    log: list[tuple[str, bool]] = []

    def step(name: str):
        async def run(*args, **kwargs):
            log.append((name, bot._digests_enabled))
            if name == boom:
                raise RuntimeError("this step has a bug")
            return []

        return run

    for attr, name in (
        ("_send_import_notice", "import notice"),
        ("_reconcile", "reconcile"),
        ("_check_owner_channel", "owner channel check"),
        ("_recover_codes", "pending codes"),
        ("_start_collection_job", "collection job"),
        ("reload_lounges", "lounge reload"),
        ("chunk_lounge_guilds", "lounge chunking"),
        ("schedule_lounge_quotes", "lounge quotes"),
        ("_permission_sweep", "permission sweep"),
    ):
        monkeypatch.setattr(bot, attr, step(name))
    return log


async def test_on_ready_does_its_work_in_the_documented_order(bot, monkeypatch):
    log = _record_steps(bot, monkeypatch)

    await bot.on_ready()

    assert [name for name, _ in log] == [
        "import notice",
        "reconcile",  # QA M1: startup delivery waits for it (the guild cache is warm by then)
        "owner channel check",  # after reconcile (it counts servers), before the first owner alert
        "pending codes",
        "lounge reload",
        "lounge chunking",
        "lounge quotes",
        "permission sweep",
        "collection job",  # last of the steps: after startup delivery, so they can't overlap
    ]
    # The digests wait for every step but the collection job, which comes after they're on.
    assert all(enabled is False for name, enabled in log if name != "collection job")
    assert bot._digests_enabled is True


# --- the owner channel check ---


def _with_servers(bot: NewsBot, count: int) -> None:
    with closing(connect(bot.db_path)) as conn:
        for gid in range(31, 31 + count):
            repo.create_guild(conn, gid, set_up=False)


def _channel_in(guild_id: int | None):
    return SimpleNamespace(id=OWNER_CHANNEL, guild=SimpleNamespace(id=guild_id))


async def _check(bot: NewsBot, monkeypatch, *, home, channel_id, channel, servers, caplog, capsys):
    bot.cfg = bot.cfg.model_copy(update={"home_guild_id": home, "admin_channel_id": channel_id})
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel)
    _with_servers(bot, servers)
    caplog.set_level(logging.ERROR)
    await bot._check_owner_channel()
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    return errors, capsys.readouterr().err


async def test_a_good_owner_channel_says_nothing(bot, monkeypatch, caplog, capsys):
    errors, err = await _check(
        bot,
        monkeypatch,
        home=HOME,
        channel_id=OWNER_CHANNEL,
        channel=_channel_in(HOME),
        servers=3,
        caplog=caplog,
        capsys=capsys,
    )
    assert errors == [] and err == ""


async def test_an_owner_channel_in_another_server_is_an_error_on_both_logs(
    bot, monkeypatch, caplog, capsys
):
    errors, err = await _check(
        bot,
        monkeypatch,
        home=HOME,
        channel_id=OWNER_CHANNEL,
        channel=_channel_in(HOME + 1),
        servers=3,
        caplog=caplog,
        capsys=capsys,
    )
    assert len(errors) == 1 and "isn't in home_guild_id" in errors[0] and "Fix:" in errors[0]
    assert err.strip() == errors[0]


async def test_an_owner_channel_the_bot_cannot_see_is_an_error(bot, monkeypatch, caplog, capsys):
    errors, err = await _check(
        bot,
        monkeypatch,
        home=HOME,
        channel_id=OWNER_CHANNEL,
        channel=None,
        servers=1,
        caplog=caplog,
        capsys=capsys,
    )
    assert len(errors) == 1 and "isn't a channel I can see" in errors[0]
    assert err.strip() == errors[0]


async def test_no_home_server_with_several_servers_is_an_error(bot, monkeypatch, caplog, capsys):
    errors, err = await _check(
        bot,
        monkeypatch,
        home=None,
        channel_id=OWNER_CHANNEL,
        channel=_channel_in(HOME),
        servers=2,
        caplog=caplog,
        capsys=capsys,
    )
    assert len(errors) == 1 and "home_guild_id isn't set" in errors[0]
    assert err.strip() == errors[0]


async def test_no_admin_channel_with_several_servers_is_an_error(bot, monkeypatch, caplog, capsys):
    errors, _err = await _check(
        bot,
        monkeypatch,
        home=HOME,
        channel_id=None,
        channel=None,
        servers=2,
        caplog=caplog,
        capsys=capsys,
    )
    assert len(errors) == 1 and "owner_channel_id isn't set" in errors[0]


async def test_a_self_hosted_one_server_bot_without_either_setting_is_left_alone(
    bot, monkeypatch, caplog, capsys
):
    errors, err = await _check(
        bot,
        monkeypatch,
        home=None,
        channel_id=None,
        channel=None,
        servers=1,
        caplog=caplog,
        capsys=capsys,
    )
    assert errors == [] and err == ""


async def test_a_misconfigured_owner_channel_does_not_stop_startup(bot, monkeypatch):
    bot.cfg = bot.cfg.model_copy(update={"home_guild_id": HOME, "admin_channel_id": OWNER_CHANNEL})
    monkeypatch.setattr(bot, "get_channel", lambda cid: _channel_in(HOME + 1))

    await bot.on_ready()  # the real steps; the check complains and the rest carries on

    assert bot._digests_enabled is True


async def test_a_step_that_blows_up_is_reported_and_does_not_stop_the_rest(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    log = _record_steps(bot, monkeypatch, boom="reconcile")

    await bot.on_ready()

    assert [name for name, _ in log][-1] == "collection job"
    assert bot._digests_enabled is True
    assert len(alerts) == 1 and "'reconcile'" in alerts[0] and "RuntimeError" not in alerts[0]


async def test_the_import_notice_goes_to_the_owner_once(db_path, monkeypatch):
    cfg = load_config(PRODLIKE)
    report = ensure_imported(db_path, cfg, lambda: datetime(2026, 10, 1, 7, 0, tzinfo=UTC))
    assert report is not None
    b = NewsBot(cfg, _secrets(), db_path, import_report=report)
    try:
        await _setup(b)
        alerts = _alerts(b, monkeypatch)
        await b.on_ready()
        assert alerts[0] == report.owner_notice()
        assert alerts.count(report.owner_notice()) == 1
    finally:
        await _stopped(b)


async def test_the_permission_sweep_sends_the_owner_counts_only(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    # Two set-up servers the bot can't see: skipped, not flagged, so nothing to report.
    with closing(connect(bot.db_path)) as conn:
        for gid in (31, 32):
            repo.create_guild(conn, gid, set_up=True)
            repo.follow_game(conn, gid, "palworld", 5)

    await bot.on_ready()

    assert alerts == []


async def test_a_permission_sweep_with_problems_tells_the_owner_how_many_and_nothing_else(
    bot, monkeypatch
):
    alerts = _alerts(bot, monkeypatch)
    told: list[tuple[int, str]] = []

    async def notify(guild_id: int, text: str) -> None:
        told.append((guild_id, text))

    bot.router.notify_guild = notify  # type: ignore[method-assign]
    with closing(connect(bot.db_path)) as conn:
        repo.create_guild(conn, 41, set_up=True)
        repo.follow_game(conn, 41, "palworld", 5)
    guild = SimpleNamespace(id=41, me="bot-member")
    monkeypatch.setattr(bot, "get_guild", lambda gid: guild if gid == 41 else None)

    async def gone(channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Channel")

    monkeypatch.setattr(bot, "get_channel", lambda channel_id: None)
    monkeypatch.setattr(bot, "fetch_channel", gone)

    await bot._permission_sweep()

    assert [g for g, _ in told] == [41]
    assert alerts == ["newsbot: permission problems in 1 of 1 server"]


# --- one router, one set of deps ---


async def test_one_router_is_shared_by_every_consumer(bot):
    router = bot.router
    assert isinstance(router, Router)
    assert isinstance(router._send, ClientSender)  # the guild-ownership check is on...
    assert router._home_guild_id == bot.cfg.home_guild_id  # ...and knows the home guild
    assert router._owner_channel_id == bot.cfg.effective_owner_channel_id
    assert bot.guild_notifier == router.notify_guild
    assert bot.lifecycle._alert_owner == router.alert_owner
    fan = bot._fanout_deps
    assert fan.notify_guild == router.notify_guild and fan.alert_owner == router.alert_owner
    assert bot._collection_deps.alert == router.alert_owner
    assert bot._summary_deps.alert == router.alert_owner
    deps = bot.guild_digest_deps()
    assert deps.notify_guild == router.notify_guild
    assert deps.send_report == router.send_report


async def test_the_owner_alert_is_the_routers(bot, monkeypatch):
    sent: list[str] = []

    async def alert_owner(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(bot.router, "alert_owner", alert_owner)
    await bot.alert("hello owner")
    assert sent == ["hello owner"]


async def test_guild_digest_deps_is_one_shared_object(bot):
    first = bot.guild_digest_deps()
    assert bot.guild_digest_deps() is first
    assert first is bot._digest_deps


def test_guild_digest_deps_before_setup_hook_is_a_clear_error(v3_cfg, db_path):
    with pytest.raises(RuntimeError, match="setup_hook"):
        NewsBot(v3_cfg, _secrets(), db_path).guild_digest_deps()


async def test_the_commands_and_the_minute_job_share_those_deps(bot, v3_cfg):
    from newsbot.bot.commands import make_guild_admin_group

    group = make_guild_admin_group(v3_cfg, bot)
    assert group is not None
    # The factory's default is `bot.guild_digest_deps`, so preview and run-now hit the same
    # per-server locks the schedule does.
    factory = getattr(bot, "guild_digest_deps", None)
    assert factory() is bot.guild_digest_deps()


async def test_the_digest_summaries_come_from_the_shared_summary_lookup(bot):
    deps = bot.guild_digest_deps()
    assert deps.summary_for is not None and deps.retry_summary is not None
    assert (
        deps.guild_timeout_s
        == client_module.GuildDigestDeps.__dataclass_fields__["guild_timeout_s"].default
    )
    assert (
        deps.heartbeat_s
        == client_module.GuildDigestDeps.__dataclass_fields__["heartbeat_s"].default
    )


async def test_the_publisher_factory_is_resumable_and_skips_dead_channels(bot):
    published: list[tuple[str, int]] = []

    async def on_posted(key: str, message_id: int) -> None:
        published.append((key, message_id))

    publisher = bot.guild_digest_deps().publisher_for(
        SimpleNamespace(guild_id=1), "1|2026-10-01", {"palworld": 77}, on_posted
    )

    assert isinstance(publisher, DiscordPublisher)
    assert publisher.posted_by_game == {"palworld": 77}  # a resume's preload
    assert publisher._skip_permanent is True
    assert publisher._nonce_salt == "1|2026-10-01"  # stable across a restart
    assert publisher._on_posted is on_posted


async def test_the_code_poster_factory_carries_the_servers_own_choice(bot):
    notified: list[str] = []

    async def notify(text: str) -> None:
        notified.append(text)

    poster = bot._fanout_deps.poster_for(5, 6, "everyone", notify)

    assert isinstance(poster, DiscordCodeAlertPoster)
    assert (poster._channel_id, poster._ping_choice, poster._notify) == (6, "everyone", notify)


class _AdminChannel:
    def __init__(self, guild_id: int) -> None:
        self.guild = SimpleNamespace(id=guild_id)
        self.sent: list[tuple[str, discord.AllowedMentions]] = []

    async def send(self, content, *, allowed_mentions):
        self.sent.append((content, allowed_mentions))
        return SimpleNamespace(id=1)


async def test_a_run_report_never_pings_anyone(bot, monkeypatch):
    channel = _AdminChannel(guild_id=77)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)

    await bot.guild_digest_deps().send_report(77, 123, "report with @everyone and <@&5>")

    ((text, mentions),) = channel.sent
    assert "@everyone" not in text  # defused by the router as well
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (
        False,
        False,
        False,
        False,
    )


async def test_a_run_report_for_a_channel_in_another_server_goes_nowhere(bot, monkeypatch):
    channel = _AdminChannel(guild_id=999)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)

    await bot.guild_digest_deps().send_report(77, 123, "report")

    assert channel.sent == []  # the ownership check is switched on, which is the point


# --- the collection job (was the v2 sweep job's wiring) ---


async def test_collection_collectors_never_include_web_search(v3_cfg, db_path):
    b = NewsBot(v3_cfg, _secrets(brave="k"), db_path)
    try:
        await _setup(b)
        from newsbot.pipeline.collect import build_collection_collectors

        collectors = build_collection_collectors(v3_cfg, _secrets(brave="k"))
        assert collectors
        assert all(getattr(c, "source_type", None) != "web_search" for c in collectors)
    finally:
        await _stopped(b)


async def test_collection_job_builds_fresh_collectors_each_pass(bot, monkeypatch):
    seen: list[list[object]] = []

    async def fake_run_collection(deps):
        seen.append(list(deps.collectors))
        return CollectionOutcome()

    monkeypatch.setattr(client_module, "run_collection", fake_run_collection)

    await bot._collection_job()
    await bot._collection_job()

    first, second = seen
    assert first and len(first) == len(second)
    assert all(a is not b for a, b in zip(first, second, strict=True))


async def test_collection_shares_one_rate_limit_state_for_the_life_of_the_process(bot):
    assert isinstance(bot._rate_limit_state, RateLimitState)
    assert bot._collection_deps.rate_limit_state is bot._rate_limit_state


def test_collection_job_before_setup_hook_is_a_clear_error(v3_cfg, db_path):
    b = NewsBot(v3_cfg, _secrets(), db_path)
    with pytest.raises(RuntimeError):
        asyncio.run(b._collection_job())


async def _run_collection_with(bot, monkeypatch, outcomes):
    calls = iter(outcomes)

    async def fake_run_collection(deps):
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client_module, "run_collection", fake_run_collection)


async def test_collection_job_repeated_crashes_alert_once(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    await _run_collection_with(bot, monkeypatch, [RuntimeError("boom")] * 3)

    for _ in range(3):
        await bot._collection_job()

    assert len(alerts) == 1 and alerts[0].startswith("newsbot: collection pass crashed")
    assert "collection" in bot._crash_alerted


async def test_collection_job_success_clears_the_crash_flag(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    bot._crash_alerted.add("collection")
    await _run_collection_with(bot, monkeypatch, [CollectionOutcome(sources_ok=1, sources_total=1)])

    await bot._collection_job()

    assert "collection" not in bot._crash_alerted
    assert alerts == []


async def test_collection_job_crash_success_crash_alerts_twice(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    await _run_collection_with(
        bot, monkeypatch, [RuntimeError("boom"), CollectionOutcome(), RuntimeError("boom")]
    )

    for _ in range(3):
        await bot._collection_job()

    assert len(alerts) == 2


async def test_collection_job_lock_busy_neither_alerts_nor_clears_the_flag(bot, monkeypatch):
    # `run_collection` reports a skipped pass (the run lock was held). That is not a crash
    # and not a success: a crash that is still unresolved stays unresolved.
    alerts = _alerts(bot, monkeypatch)
    bot._crash_alerted.add("collection")
    await _run_collection_with(bot, monkeypatch, [CollectionOutcome(skipped=True)])

    await bot._collection_job()

    assert alerts == []
    assert "collection" in bot._crash_alerted


async def test_the_digest_tick_crash_alerts_once_until_it_runs_clean(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    bot._digests_enabled = True
    results = iter([RuntimeError("locked"), RuntimeError("locked"), [], RuntimeError("locked")])

    async def fake_run_due_guilds(deps):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client_module, "run_due_guilds", fake_run_due_guilds)

    for _ in range(4):
        await bot._digest_job()

    assert len(alerts) == 2  # the first crash, then the first crash after a clean tick
    assert all(a.startswith("newsbot: digest tick crashed") for a in alerts)


async def test_the_summaries_job_crash_alerts_once(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)

    async def boom(deps):
        raise RuntimeError("model down")

    monkeypatch.setattr(client_module, "prepare_summaries", boom)

    await bot._summaries_job()
    await bot._summaries_job()

    assert len(alerts) == 1 and alerts[0].startswith("newsbot: summaries job crashed")


# --- startup: interrupted codes ---

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"


async def test_legacy_interrupted_codes_are_reported_on_the_first_on_ready_only(
    v3_cfg, db_path, monkeypatch
):
    # A v2.2 process claimed a code and died before confirming the send. `fail_pending_codes`
    # (called from setup_hook, before any job exists) flips it and remembers it for on_ready.
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
            "pinged, status) VALUES (?, '2026-09-25T00:00:00+00:00', 'Gearbox Blog', "
            "'https://example.com/a', 0, 'pending')",
            (CODE,),
        )
    b = NewsBot(v3_cfg, _secrets(), db_path)
    try:
        await _setup(b)
        assert b._interrupted_codes == [CODE]
        with closing(connect(db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM alerted_codes WHERE code = ?", (CODE,)
            ).fetchone()[0]
        assert status == "failed"
        alerts = _alerts(b, monkeypatch)

        await b.on_ready()
        assert len(alerts) == 1 and CODE in alerts[0]

        await b.on_ready()  # a reconnect: not repeated
        assert len(alerts) == 1
    finally:
        await _stopped(b)


async def test_no_interrupted_codes_means_no_startup_alert(bot, monkeypatch):
    assert bot._interrupted_codes == []
    alerts = _alerts(bot, monkeypatch)

    await bot.on_ready()

    assert alerts == []


async def test_a_servers_pending_code_is_failed_at_startup_and_the_owner_hears_a_count_only(
    bot, monkeypatch
):
    alerts = _alerts(bot, monkeypatch)
    told: list[tuple[int, str]] = []

    async def notify(guild_id: int, text: str) -> None:
        told.append((guild_id, text))

    monkeypatch.setattr(bot.router, "notify_guild", notify)
    with closing(connect(bot.db_path)) as conn, conn:
        repo.create_guild(conn, 51, set_up=True)
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status) "
            "VALUES (?, '2026-10-01T00:00:00+00:00', 's', 'https://e.example', 'posted')",
            (CODE,),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, pinged, from_roundup, "
            "claimed_at) VALUES (51, ?, 'pending', 1, 0, '2026-10-01T00:00:00+00:00')",
            (CODE,),
        )

    await bot._recover_codes()

    assert [g for g, _ in told] == [51]
    assert CODE in told[0][1]
    assert len(alerts) == 1 and "1 SHiFT code alert" in alerts[0]
    assert CODE not in alerts[0] and "51" not in alerts[0]  # a count, never whose or which


# --- the lounge and the guild events ---


async def test_removing_a_guild_removes_its_quote_job_and_its_lounge(bot):
    gid = 61
    with closing(connect(bot.db_path)) as conn:
        repo.create_guild(conn, gid, set_up=True)
        from newsbot.store.models import LoungeSettings

        repo.upsert_lounge(
            conn,
            LoungeSettings(
                gid, 5, False, "hi", True, "08:00", [{"kind": "wikiquote", "value": "x"}], None
            ),
        )
    await bot.schedule_lounge_quotes()
    assert bot.scheduler.get_job(f"daily-quote-{gid}") is not None

    await bot.on_guild_remove(SimpleNamespace(id=gid))

    assert bot.scheduler.get_job(f"daily-quote-{gid}") is None
    assert gid not in bot._lounges
    with closing(connect(bot.db_path)) as conn:
        assert repo.get_guild(conn, gid) is None


async def test_the_lifecycle_events_are_forwarded(bot, monkeypatch):
    calls: list[tuple[str, object]] = []

    async def record(name, arg):
        calls.append((name, arg))

    monkeypatch.setattr(bot.lifecycle, "on_guild_join", lambda g: record("join", g))
    monkeypatch.setattr(bot.lifecycle, "on_guild_channel_delete", lambda c: record("delete", c))
    guild, channel = SimpleNamespace(id=71), SimpleNamespace(id=72)

    await bot.on_guild_join(guild)
    await bot.on_guild_channel_delete(channel)

    assert calls == [("join", guild), ("delete", channel)]


# --- retention and the owner report ---


async def test_retention_purges_summaries_and_notices_past_90_days_and_keeps_the_rest(bot):
    old = datetime.now(UTC) - timedelta(days=91)
    new = datetime.now(UTC) - timedelta(days=89)
    with closing(connect(bot.db_path)) as conn, conn:
        repo.create_guild(conn, 81, set_up=True)
        for when, key in ((old, "old"), (new, "new")):
            conn.execute(
                "INSERT INTO game_summaries (game_key, run_date, status, window_start, "
                "window_end, created_at) VALUES (?, ?, 'ok', ?, ?, ?)",
                (key, "2026-07-01", when.isoformat(), when.isoformat(), when.isoformat()),
            )
            repo.add_notice(conn, 81, f"{key} notice", now=lambda w=when: w)

    await bot._retention_job()

    with closing(connect(bot.db_path)) as conn:
        keys = [r[0] for r in conn.execute("SELECT game_key FROM game_summaries")]
        notices = [n.text for n in repo.recent_notices(conn, 81)]
    assert keys == ["new"]
    assert notices == ["new notice"]


def _digest_row(conn, guild_id, status, notes, when):
    conn.execute(
        "INSERT INTO digests (guild_id, run_date, status, error_notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, when.date().isoformat(), status, notes, when.isoformat(), when.isoformat()),
    )


async def test_the_owner_report_goes_out_once_a_day(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    now = datetime.now(UTC)
    with closing(connect(bot.db_path)) as conn, conn:
        for gid, status, notes in (
            (91, "ok", None),
            (92, "partial", "palworld: skipped (404)"),
            (93, "failed", "publish failed: discord send failed: 403 Forbidden"),
            (94, "pending", None),
        ):
            repo.create_guild(conn, gid, set_up=True)
            _digest_row(conn, gid, status, notes, now - timedelta(hours=2))

    await bot._owner_report_job()
    await bot._owner_report_job()  # the same day: guarded

    assert len(alerts) == 1
    assert alerts[0].startswith("newsbot daily: digests posted to 2 of 4 servers; 2 failed")
    assert "missing permissions" in alerts[0] and "interrupted" in alerts[0]
    with closing(connect(bot.db_path)) as conn:
        today = now.astimezone(LA).date().isoformat()
        assert repo.app_state_get(conn, "owner_report_date") == today


async def test_the_owner_report_goes_out_again_the_next_day(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    with closing(connect(bot.db_path)) as conn:
        repo.app_state_set(conn, "owner_report_date", "2000-01-01")

    await bot._owner_report_job()

    assert len(alerts) == 1 and "no server digests were due today" in alerts[0]


async def test_the_owner_report_lists_failing_sources(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)
    now = datetime.now(UTC)
    # The report only lists sources still in the config, so use a real one.
    name = sorted(bot.cfg.configured_source_names())[0]
    with closing(connect(bot.db_path)) as conn:
        for _ in range(3):
            repo.record_source_result(conn, name, now, "503 from host")

    await bot._owner_report_job()

    assert "1 failing source:" in alerts[0] and f"{name} (3 in a row)" in alerts[0]


async def test_a_crashing_owner_report_alerts_and_does_not_raise(bot, monkeypatch):
    alerts = _alerts(bot, monkeypatch)

    def boom(now, today):
        raise RuntimeError("db gone")

    monkeypatch.setattr(bot, "_owner_report_sync", boom)

    await bot._owner_report_job()

    assert alerts == ["newsbot: daily report crashed: db gone"]


# --- one whole day for the friend's server ---

FRIEND = 100000000000000001
BL4_CH, PAL_CH, D4_CH = 1452017235274240221, 1542581309845799013, 1531681353681211524
SHIFT_CH, ADMIN_CH = 1553597251933438122, 100000000000000002
T_IMPORT = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
T_COLLECT = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)  # 08:00 Pacific
T_DUE = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)  # 09:00 Pacific (PDT)
E2E_CODE = "ZZZZ1-AAAAA-BBBBB-CCCCC-DDDDD"


class _Message:
    _next = 5000

    def __init__(self) -> None:
        type(self)._next += 1
        self.id = type(self)._next


class _E2EChannel:
    def __init__(self, guild_id: int = FRIEND) -> None:
        self.guild = SimpleNamespace(id=guild_id, me="the-bot")
        self.sent: list[SimpleNamespace] = []

    def permissions_for(self, member) -> discord.Permissions:
        return discord.Permissions.all()  # including Mention @everyone: nothing to complain about

    async def send(self, content=None, *, embed=None, allowed_mentions=None, nonce=None):
        self.sent.append(
            SimpleNamespace(
                content=content, embed=embed, allowed_mentions=allowed_mentions, nonce=nonce
            )
        )
        return _Message()


def _write_day_fixtures(directory: Path) -> Path:
    """Two games' worth of fresh items (one with a SHiFT code in it), dated for T_COLLECT."""
    directory.mkdir()
    fresh = (T_COLLECT - timedelta(hours=3)).isoformat()
    (directory / "borderlands4.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://example.com/bl4/patch-1",
                    "title": "Borderlands 4 Patch 1.2 Notes",
                    "excerpt": "Fixes several crashes and rebalances loot drop rates.",
                    "source_name": "Gearbox Blog",
                    "trust": "official",
                    "published_at": fresh,
                    "topics": ["borderlands4"],
                    "full_text": f"Fixes crashes. Redeem code {E2E_CODE} this week only.",
                }
            ]
        )
    )
    (directory / "palworld.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://example.com/palworld/patch",
                    "title": "Palworld v0.6.2 Patch Notes",
                    "excerpt": "Fixes several crash issues reported after the last update.",
                    "source_name": "Palworld Steam",
                    "trust": "official",
                    "published_at": fresh,
                    "topics": ["palworld"],
                }
            ]
        )
    )
    return directory


async def test_a_whole_day_for_the_friends_server(v22_db, tmp_path, monkeypatch):
    cfg = load_config(PRODLIKE)
    # The fixture is the v2.2 config, which has no such key. Prod gets the friend's own id in
    # `comped_guild_ids` (D12); the home or test guild must never be in it.
    assert not set(cfg.comped_guild_ids)
    clock = {"now": T_IMPORT}
    monkeypatch.setattr(client_module, "_utcnow", lambda: clock["now"])
    day = _write_day_fixtures(tmp_path / "day")
    monkeypatch.setattr(
        client_module, "build_collection_collectors", lambda c, s: build_fixture_collectors(day)
    )

    # The import: the v2.2 database plus the prod-like config, once.
    with closing(connect(v22_db)) as conn:
        assert migrate(conn) == 8
    report = ensure_imported(v22_db, cfg, lambda: T_IMPORT)
    assert report is not None and len(report.games) == 3
    with closing(connect(v22_db)) as conn:
        friend = repo.get_guild(conn, FRIEND)
        lounges = repo.list_lounges(conn)
    assert friend.tier == "comped" and friend.imported_at is not None
    assert [row.guild_id for row in lounges] == [FRIEND]

    # The bot, with its channels faked and its model a stub.
    bot = NewsBot(cfg, _secrets(), str(v22_db), lounges=lounges, import_report=report)
    channels = {cid: _E2EChannel() for cid in (BL4_CH, PAL_CH, D4_CH, SHIFT_CH, ADMIN_CH)}
    monkeypatch.setattr(bot, "get_channel", lambda cid: channels.get(cid))
    await _setup(bot)  # the real setup_hook: commands, scheduler, jobs
    await bot.http_client.aclose()
    bot.http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("net")))
    bot.llm = StubLLM(INTEGRATION_LLM)
    bot._wire()  # the same deps, rebuilt around the stub model
    monkeypatch.setattr(bot.router, "alert_owner", lambda text: asyncio.sleep(0))
    assert not bot._digests_enabled
    try:
        # 08:00 Pacific: an hourly collection pass, from fixtures. It finds the code and the
        # fan-out posts it to the friend's SHiFT channel with the friend's own @everyone.
        clock["now"] = T_COLLECT
        await bot._collection_job()
        (code_post,) = channels[SHIFT_CH].sent
        assert E2E_CODE in code_post.content and code_post.content.startswith("@everyone")
        assert code_post.allowed_mentions.everyone is True

        # 09:00 Pacific, but the bot hasn't finished starting: the minute job is inert.
        clock["now"] = T_DUE
        await bot._digest_job()
        assert channels[BL4_CH].sent == [] and channels[PAL_CH].sent == []

        # on_ready finishes (nothing to reconcile or sweep here), then the tick posts the digest.
        await bot.on_ready()
        await bot._digest_job()
        assert len(channels[BL4_CH].sent) == 1 and len(channels[PAL_CH].sent) == 1
        assert channels[D4_CH].sent == []  # no news for Diablo IV, so nothing posted
        bl4 = channels[BL4_CH].sent[0]
        assert "Patch 1.2 fixes crashes" in bl4.embed.description  # the stubbed summary
        assert bl4.allowed_mentions.everyone is False
        assert len(channels[ADMIN_CH].sent) == 1  # the run report, to the friend's admin channel
        with closing(connect(v22_db)) as conn:
            row = repo.get_guild_digest(conn, FRIEND, datetime(2026, 10, 1).date())
        assert row.status in ("ok", "partial")
        assert set(row.posted_by_game) == {"borderlands4", "palworld"}

        # One minute later: nothing more.
        clock["now"] = T_DUE + timedelta(minutes=1)
        await bot._digest_job()
        assert len(channels[BL4_CH].sent) == 1 and len(channels[PAL_CH].sent) == 1
    finally:
        await _stopped(bot)
