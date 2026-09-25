"""Adversarial tests for the SHiFT-alert wiring in `newsbot.bot.client.NewsBot`.

Companion to `test_client_scheduling.py` (which stays at the pure-function
level per plan section 5) and `test_code_alert_poster.py` (which covers
`DiscordCodeAlertPoster` itself). This file exercises `NewsBot` object
construction directly -- no gateway connection, no real Discord HTTP calls
(`tree.sync` is monkeypatched to a no-network stub, the one REST call
`setup_hook` makes that isn't otherwise avoidable) -- for the things the
plan calls out as scheduler wiring: whether the code-sweep job gets
registered at all, its interval and first-run delay, `build_sweep_deps`'s
web_search exclusion and fresh-collectors-per-call behavior, the shared
`RateLimitState`, the startup `fail_pending_codes` + once-only admin alert,
and `_sweep_job`'s crash/success/lock-busy alerting.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

import newsbot.bot.client as client_module
from newsbot.bot.client import NewsBot
from newsbot.config import Secrets, load_config
from newsbot.shift.sweep import CodeCheckOutcome
from newsbot.store.db import connect, migrate

CONFIG_PATH = Path(__file__).parent / "fixtures" / "config_valid.yaml"


def _secrets(*, brave_api_key: str | None = None) -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=SecretStr(brave_api_key) if brave_api_key else None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _cfg(*, alerts_enabled: bool, interval_minutes: int = 60):
    cfg = load_config(CONFIG_PATH)
    alerts = cfg.alerts.model_copy(
        update={"enabled": alerts_enabled, "interval_minutes": interval_minutes}
    )
    return cfg.model_copy(update={"alerts": alerts})


async def _run_setup_hook(bot: NewsBot) -> None:
    # The only network call setup_hook makes that this test suite can't
    # just avoid by construction is tree.sync() (a REST PUT to Discord).
    # Stubbing it out is the one exception to "no gateway mocking" here --
    # it's a single async no-op, not a fake gateway or fake Client.
    async def _fake_sync(*args, **kwargs):
        return []

    bot.tree.sync = _fake_sync
    try:
        await bot.setup_hook()
    finally:
        if bot.scheduler is not None:
            bot.scheduler.shutdown(wait=False)


# --- code-sweep job registration ---


async def test_code_sweep_job_absent_when_alerts_disabled(db_path):
    bot = NewsBot(_cfg(alerts_enabled=False), _secrets(), db_path)
    await _run_setup_hook(bot)
    assert bot.scheduler.get_job("code-sweep") is None


async def test_code_sweep_job_present_when_alerts_enabled(db_path):
    bot = NewsBot(_cfg(alerts_enabled=True), _secrets(), db_path)
    await _run_setup_hook(bot)
    assert bot.scheduler.get_job("code-sweep") is not None


async def test_code_sweep_job_uses_configured_interval(db_path):
    bot = NewsBot(_cfg(alerts_enabled=True, interval_minutes=15), _secrets(), db_path)
    await _run_setup_hook(bot)
    job = bot.scheduler.get_job("code-sweep")
    assert job.trigger.interval.total_seconds() == 15 * 60


async def test_code_sweep_job_first_run_is_about_two_minutes_out(db_path):
    cfg = _cfg(alerts_enabled=True)
    tz = ZoneInfo(cfg.digest.timezone)
    before = datetime.now(tz)
    bot = NewsBot(cfg, _secrets(), db_path)
    await _run_setup_hook(bot)
    after = datetime.now(tz)
    job = bot.scheduler.get_job("code-sweep")
    delta = (job.next_run_time - before).total_seconds()
    # Generous bounds around "2 minutes after start" (plan step 9) to
    # absorb however long setup_hook itself took to run in this test.
    assert 110 <= delta <= 130
    assert job.next_run_time >= before
    assert job.next_run_time <= after + timedelta(minutes=3)


async def test_code_sweep_job_disabled_by_default_config(db_path):
    # config_valid.yaml carries no alerts: block -- confirms the "off"
    # side of the wiring test isn't just an artifact of explicitly setting
    # enabled=False above.
    cfg = load_config(CONFIG_PATH)
    assert cfg.alerts.enabled is False
    bot = NewsBot(cfg, _secrets(), db_path)
    await _run_setup_hook(bot)
    assert bot.scheduler.get_job("code-sweep") is None
    assert bot.code_alert_poster is None


# --- build_sweep_deps / build_deps ---


def _prep_bot_for_deps(cfg, db_path, *, secrets=None) -> NewsBot:
    import httpx

    from newsbot.bot.client import DiscordCodeAlertPoster

    bot = NewsBot(cfg, secrets or _secrets(), db_path)
    bot.http_client = httpx.AsyncClient()
    bot.llm = object()  # build_deps only checks it's not None
    bot.code_alert_poster = DiscordCodeAlertPoster(bot, cfg.digest.channel_id)
    return bot


async def test_build_sweep_deps_excludes_web_search(db_path):
    cfg = _cfg(alerts_enabled=True)
    bot = _prep_bot_for_deps(cfg, db_path)
    try:
        deps = bot.build_sweep_deps()
        assert all(getattr(c, "source_type", None) != "web_search" for c in deps.collectors)
    finally:
        await bot.http_client.aclose()


async def test_build_deps_includes_web_search_when_configured(db_path, monkeypatch):
    # config_valid.yaml's sources include a web_search entry -- build_deps
    # (the daily job's path) should still build it; only build_sweep_deps
    # excludes it. BRAVE_API_KEY has to be set *before* load_config runs
    # (config.py drops an unconfigured web_search source at load time),
    # so _cfg() must come after monkeypatch.setenv, not before.
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    cfg = _cfg(alerts_enabled=True)
    bot = _prep_bot_for_deps(cfg, db_path, secrets=_secrets(brave_api_key="test-key"))
    try:
        deps = bot.build_deps()
        assert any(getattr(c, "source_type", None) == "web_search" for c in deps.collectors)
    finally:
        await bot.http_client.aclose()


async def test_build_sweep_deps_builds_fresh_collectors_each_call(db_path):
    cfg = _cfg(alerts_enabled=True)
    bot = _prep_bot_for_deps(cfg, db_path)
    try:
        deps1 = bot.build_sweep_deps()
        deps2 = bot.build_sweep_deps()
        assert deps1.collectors is not deps2.collectors
        for c1, c2 in zip(deps1.collectors, deps2.collectors, strict=False):
            assert c1 is not c2
    finally:
        await bot.http_client.aclose()


async def test_build_deps_and_build_sweep_deps_share_the_same_rate_limit_state(db_path):
    cfg = _cfg(alerts_enabled=True)
    bot = _prep_bot_for_deps(cfg, db_path)
    try:
        deps = bot.build_deps()
        sweep_deps = bot.build_sweep_deps()
        assert deps.rate_limit_state is sweep_deps.rate_limit_state
        assert deps.rate_limit_state is bot._rate_limit_state
    finally:
        await bot.http_client.aclose()


def test_build_sweep_deps_raises_before_alerts_set_up(db_path):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    with pytest.raises(RuntimeError):
        bot.build_sweep_deps()


# --- startup: interrupted (pending -> failed) codes, reported once ---


async def test_interrupted_codes_reported_on_first_on_ready_only(db_path, monkeypatch):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)

    # Seed a 'pending' row directly -- as if a prior process claimed a
    # code and died before confirming the send. fail_pending_codes()
    # (called from setup_hook, before scheduler.start()) should flip it
    # to 'failed' and remember it for the first on_ready.
    with closing(connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
            "pinged, status) VALUES (?, ?, ?, ?, 0, 'pending')",
            (
                "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
                "2026-09-25T00:00:00+00:00",
                "Gearbox Blog",
                "https://example.com/a",
            ),
        )
        conn.commit()

    await _run_setup_hook(bot)
    assert bot._interrupted_codes == ["AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"]

    with closing(connect(db_path)) as conn:
        status = conn.execute(
            "SELECT status FROM alerted_codes WHERE code = ?",
            ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",),
        ).fetchone()[0]
    assert status == "failed"

    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", fake_alert)

    # _catch_up would otherwise try a real pipeline run; short-circuit it
    # since only the interrupted-codes reporting is under test here.
    async def fake_catch_up() -> None:
        return None

    monkeypatch.setattr(bot, "_catch_up", fake_catch_up)

    await bot.on_ready()
    assert len(alerts) == 1
    assert "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA" in alerts[0]

    # A reconnect fires on_ready again -- the interrupted-codes report is
    # a one-time thing, not repeated on every reconnect.
    await bot.on_ready()
    assert len(alerts) == 1


async def test_no_interrupted_codes_means_no_startup_alert(db_path, monkeypatch):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    await _run_setup_hook(bot)
    assert bot._interrupted_codes == []

    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    async def fake_catch_up() -> None:
        return None

    monkeypatch.setattr(bot, "alert", fake_alert)
    monkeypatch.setattr(bot, "_catch_up", fake_catch_up)
    await bot.on_ready()
    assert alerts == []


# --- _sweep_job crash/success/lock-busy alerting ---


def _outcome(**overrides) -> CodeCheckOutcome:
    defaults = dict(new_candidates=0, posted=0, silent=0, failed=0, ping=False, cap_reached=False)
    defaults.update(overrides)
    return CodeCheckOutcome(**defaults)


async def test_sweep_job_repeated_crashes_alert_once(db_path, monkeypatch):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.code_alert_poster = object()  # build_sweep_deps() just needs this not-None
    bot.http_client = object()

    async def crashing_sweep(_deps):
        raise RuntimeError("boom")

    monkeypatch.setattr(client_module, "run_code_sweep", crashing_sweep)

    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", fake_alert)

    await bot._sweep_job()
    await bot._sweep_job()
    await bot._sweep_job()

    assert len(alerts) == 1
    assert bot._sweep_crash_alerted is True


async def test_sweep_job_success_clears_crash_flag(db_path, monkeypatch):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.code_alert_poster = object()
    bot.http_client = object()
    bot._sweep_crash_alerted = True

    async def healthy_sweep(_deps):
        return _outcome(posted=1)

    monkeypatch.setattr(client_module, "run_code_sweep", healthy_sweep)

    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", fake_alert)

    await bot._sweep_job()

    assert bot._sweep_crash_alerted is False
    assert alerts == []


async def test_sweep_job_after_a_crash_next_success_clears_then_next_crash_alerts_again(
    db_path, monkeypatch
):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.code_alert_poster = object()
    bot.http_client = object()

    calls = {"n": 0}

    async def flaky_sweep(_deps):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise RuntimeError("boom")
        return _outcome()

    monkeypatch.setattr(client_module, "run_code_sweep", flaky_sweep)

    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", fake_alert)

    await bot._sweep_job()  # 1: crash -> alerts
    await bot._sweep_job()  # 2: success -> clears
    await bot._sweep_job()  # 3: crash again -> alerts again

    assert len(alerts) == 2


async def test_sweep_job_lock_busy_outcome_none_neither_alerts_nor_clears_incorrectly(
    db_path, monkeypatch
):
    # run_code_sweep() returns None when the run lock was held (A10) --
    # this isn't a crash and isn't a success either; _sweep_job should
    # leave the crash-alerted flag exactly as it found it and never alert.
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.code_alert_poster = object()
    bot.http_client = object()
    bot._sweep_crash_alerted = True  # pretend a prior crash was still unresolved

    async def busy_sweep(_deps):
        return None

    monkeypatch.setattr(client_module, "run_code_sweep", busy_sweep)

    alerts: list[str] = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(bot, "alert", fake_alert)

    await bot._sweep_job()

    assert alerts == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (newsbot/bot/client.py _sweep_job): a lock-busy sweep (outcome "
        "is None, not a crash) unconditionally clears _sweep_crash_alerted "
        "on the success path (`self._sweep_crash_alerted = False` runs "
        "before the `if outcome is None` check), same as a real successful "
        "sweep. So a genuinely still-broken sweep that happens to lose the "
        "lock race to a daily run goes silent about its own ongoing crash "
        "until the *next* failure, instead of staying flagged. Severity: "
        "low (this needs another job to be holding the lock at the same "
        "moment a broken sweep fires, and the next crash re-alerts within "
        "one more interval) but it is a real behavior gap from what the "
        "plan's 'once per ongoing failure' design implies."
    ),
)
async def test_sweep_job_lock_busy_does_not_silently_clear_a_real_crash_flag(db_path, monkeypatch):
    cfg = _cfg(alerts_enabled=True)
    bot = NewsBot(cfg, _secrets(), db_path)
    bot.code_alert_poster = object()
    bot.http_client = object()
    bot._sweep_crash_alerted = True

    async def busy_sweep(_deps):
        return None

    monkeypatch.setattr(client_module, "run_code_sweep", busy_sweep)
    monkeypatch.setattr(bot, "alert", lambda text: None)

    await bot._sweep_job()

    # A lock-busy skip tells us nothing about whether the underlying crash
    # is fixed -- the flag should still read True until an actual success.
    assert bot._sweep_crash_alerted is True
