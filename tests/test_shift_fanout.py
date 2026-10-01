"""Tests for newsbot.shift.fanout: one detection, then a turn for every server (plan task 5).

Posting is a fake poster per server, recording what it was asked to send; the
database is a real temp file with the real migrations. Nothing here talks to
Discord, and the clock only moves when a test moves it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.bot.format import RenderedAlert
from newsbot.collectors.base import CollectorResult, RawItem
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.fanout import (
    FanoutDeps,
    detect_and_fan_out,
    make_shift_hook,
    recover_pending_guild_codes,
)
from newsbot.shift.sweep import _strip_ping
from newsbot.store import repo
from newsbot.store.db import connect, migrate

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
CODE_C = "CCCC3-CCCCC-CCCCC-CCCCC-CCCCC"
CODE_D = "DDDD4-DDDDD-DDDDD-DDDDD-DDDDD"
ROLE = "1234567890123456789"


async def _no_sleep(_seconds: float) -> None:
    return None


class FakePoster:
    def __init__(self, guild_id, channel_id, ping, *, fail=None):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.ping = ping
        self.fail = list(fail or [])
        self.sent: list[RenderedAlert] = []
        self.begin_calls = 0

    def begin_batch(self):
        self.begin_calls += 1

    async def post(self, alert):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(alert)
        return 9000 + len(self.sent)


class Harness:
    """Deps plus everything the fakes saw."""

    def __init__(self, db_path, cfg, now):
        self.db_path = db_path
        self.cfg = cfg
        self.clock = now
        self.posters: dict[int, FakePoster] = {}
        self.fail_with: dict[int, list[Exception]] = {}
        self.notices: list[tuple[int, str]] = []

    def factory(self, guild_id, channel_id, ping, notify):
        poster = FakePoster(guild_id, channel_id, ping, fail=self.fail_with.get(guild_id))
        self.posters.setdefault(guild_id, poster)
        self.posters[guild_id] = poster
        return poster

    async def notify_guild(self, guild_id, text):
        self.notices.append((guild_id, text))

    def deps(self):
        return FanoutDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            now=lambda: self.clock,
            poster_for=self.factory,
            notify_guild=self.notify_guild,
            sleep=_no_sleep,
        )

    async def run(self, items, *, seeding_ok=True):
        self.posters.clear()
        return await detect_and_fan_out(self.deps(), items, seeding_ok=seeding_ok)

    def sent(self, guild_id) -> list[RenderedAlert]:
        poster = self.posters.get(guild_id)
        return poster.sent if poster else []


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
    harness = Harness(db_path, cfg, NOW)
    seed(db_path)
    return harness


def seed(db_path):
    with closing(connect(db_path)) as conn:
        repo.record_silent_codes(conn, [], now=lambda: EARLIER, mark_seeded=True)


def add_guild(
    db_path,
    guild_id,
    *,
    ping="none",
    tz="UTC",
    games=("borderlands4",),
    enabled=True,
    enabled_at=EARLIER,
    set_up=True,
    ping_day=None,
    ping_count=0,
):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, guild_id, timezone=tz, set_up=set_up, now=lambda: EARLIER)
        for game in games:
            repo.follow_game(conn, guild_id, game, 10)
        repo.set_shift(
            conn,
            guild_id,
            enabled=enabled,
            channel_id=guild_id * 100,
            ping=ping,
            now=lambda: enabled_at,
        )
        with conn:
            conn.execute(
                "UPDATE guild_shift SET ping_day = ?, ping_count = ? WHERE guild_id = ?",
                (ping_day, ping_count, guild_id),
            )


def item(*codes, trust="official", published=None, url=None, title=None):
    return RawItem(
        url=url or f"https://example.com/{'-'.join(c[:4] for c in codes)}",
        title=title or "Borderlands 4 new SHiFT code: " + " ".join(codes),
        excerpt="",
        source_name="Gearbox Blog",
        trust=trust,
        published_at=published or NOW - timedelta(hours=1),
    )


def rows(db_path, sql, *args):
    with closing(connect(db_path)) as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def shift_row(db_path, guild_id):
    with closing(connect(db_path)) as conn:
        return repo.get_shift(conn, guild_id)


def post_status(db_path, guild_id):
    return dict(
        rows(db_path, "SELECT code, status FROM guild_code_posts WHERE guild_id = ?", guild_id)
    )


# --- detection, once ---


async def test_code_is_recorded_globally_once_whatever_the_guild_count(h):
    for gid in (1, 2, 3):
        add_guild(h.db_path, gid, ping="everyone")

    released = await h.run([item(CODE_A)])

    assert released == 1
    assert rows(h.db_path, "SELECT code, status, pinged, from_roundup FROM alerted_codes") == [
        (CODE_A, "posted", 0, 0)
    ]
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(3,)]


async def test_same_code_next_pass_is_known_and_posts_nothing(h):
    add_guild(h.db_path, 1)
    await h.run([item(CODE_A)])
    assert len(h.sent(1)) == 1

    released = await h.run([item(CODE_A)])

    assert released == 0
    assert h.sent(1) == []


async def test_first_sweep_seeds_silently_and_later_codes_post(db_path, cfg):
    harness = Harness(db_path, cfg, NOW)  # deliberately not seeded
    add_guild(db_path, 1, ping="everyone")

    assert await harness.run([item(CODE_A)]) == 0
    assert harness.sent(1) == []
    assert rows(db_path, "SELECT code, status FROM alerted_codes") == [(CODE_A, "seeded")]
    assert rows(db_path, "SELECT value FROM alert_state WHERE key = 'seeded_at'") != []

    assert await harness.run([item(CODE_B)]) == 1
    assert [a.codes for a in harness.sent(1)] == [[CODE_B]]


async def test_unhealthy_first_sweep_does_not_set_the_seeded_marker(db_path, cfg):
    harness = Harness(db_path, cfg, NOW)
    add_guild(db_path, 1)

    await harness.run([item(CODE_A)], seeding_ok=False)

    assert rows(db_path, "SELECT value FROM alert_state WHERE key = 'seeded_at'") == []


async def test_healthy_first_sweep_with_no_codes_still_sets_the_marker(db_path, cfg):
    harness = Harness(db_path, cfg, NOW)

    await harness.run([])

    assert rows(db_path, "SELECT value FROM alert_state WHERE key = 'seeded_at'") != []


async def test_too_old_code_is_recorded_silently(h):
    add_guild(h.db_path, 1)

    await h.run([item(CODE_A, published=NOW - timedelta(days=30))])

    assert h.sent(1) == []
    assert rows(h.db_path, "SELECT status FROM alerted_codes") == [("too_old",)]


async def test_a_code_outside_shift_games_is_ignored(h):
    add_guild(h.db_path, 1)

    await h.run([item(CODE_A, title="Palworld patch notes " + CODE_A)])

    assert h.sent(1) == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM alerted_codes") == [(0,)]


# --- pings follow each guild's choice ---


async def test_three_guilds_three_pings_and_one_failing_channel(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    add_guild(h.db_path, 3, ping="none")
    add_guild(h.db_path, 4, ping="everyone")
    h.fail_with[4] = [RuntimeError("Missing Access")]

    released = await h.run([item(CODE_A, CODE_B)])

    assert released == 2
    (a1,) = h.sent(1)
    assert a1.content.startswith("@everyone ") and a1.ping and a1.ping_prefix == "@everyone "
    (a2,) = h.sent(2)
    assert a2.content.startswith(f"<@&{ROLE}> ") and a2.ping and a2.ping_prefix == f"<@&{ROLE}> "
    (a3,) = h.sent(3)
    assert not a3.ping and "@" not in a3.content and a3.ping_prefix == ""
    assert h.sent(4) == []
    for gid in (1, 2, 3):
        assert post_status(h.db_path, gid) == {CODE_A: "posted", CODE_B: "posted"}
    assert post_status(h.db_path, 4) == {CODE_A: "failed", CODE_B: "failed"}
    # Only guild 4 hears about guild 4; the owner's channel isn't in this path at all.
    assert [g for g, _ in h.notices] == [4]
    assert CODE_A in h.notices[0][1]
    assert [shift_row(h.db_path, g).ping_count for g in (1, 2, 3, 4)] == [1, 1, 0, 1]


async def test_a_guild_that_raises_outside_the_poster_does_not_block_the_next(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    real = h.factory

    def boom(guild_id, channel_id, ping, notify):
        if guild_id == 1:
            raise RuntimeError("factory broke")
        return real(guild_id, channel_id, ping, notify)

    h.factory = boom

    await h.run([item(CODE_A)])

    assert len(h.sent(2)) == 1
    assert [g for g, _ in h.notices] == [1]


async def test_untrusted_only_batch_never_spends_a_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)

    await h.run([item(CODE_A, trust="community")])

    for gid in (1, 2):
        (alert,) = h.sent(gid)
        assert not alert.ping and not alert.content.startswith(("@everyone", "<@&"))
        assert shift_row(h.db_path, gid).ping_count == 0
    assert h.notices == []


async def test_ping_only_on_first_message_of_a_batch(h):
    add_guild(h.db_path, 1, ping=ROLE)
    # 25 codes from distinct official items overflow one 2000-unit message.
    codes = [f"{chr(65 + i)}{i:03d}1-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(25)]
    assert all(len(c) == 29 for c in codes)

    await h.run([item(c, url=f"https://example.com/{i}") for i, c in enumerate(codes)])

    alerts = h.sent(1)
    assert len(alerts) > 1
    assert [a.ping for a in alerts] == [True] + [False] * (len(alerts) - 1)
    assert shift_row(h.db_path, 1).ping_count == 1


# --- the cap, per guild and per local day ---


async def test_cap_is_per_guild_a_capped_guild_posts_unpinged_while_another_still_pings(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=3)
    add_guild(h.db_path, 2, ping="everyone")

    await h.run([item(CODE_A)])

    (a1,) = h.sent(1)
    (a2,) = h.sent(2)
    assert not a1.ping and a1.content.startswith("**")
    assert a2.ping and a2.content.startswith("@everyone ")
    assert shift_row(h.db_path, 1).ping_count == 3
    assert shift_row(h.db_path, 2).ping_count == 1
    # The capped guild is told, once, and only that guild.
    assert [(g, "cap reached" in t) for g, t in h.notices] == [(1, True)]


async def test_cap_spends_up_to_three_then_stops_within_one_day(h):
    add_guild(h.db_path, 1, ping="everyone")
    pings = []
    for code in (CODE_A, CODE_B, CODE_C, CODE_D):
        await h.run([item(code)])
        pings.append(h.sent(1)[0].ping)

    assert pings == [True, True, True, False]


async def test_cap_resets_on_each_guilds_own_local_day(h):
    # 2026-10-01 20:00 UTC is still Oct 1 in Los Angeles (13:00) but already
    # Oct 2 in Tokyo (05:00). Both carry a spent Oct 1 budget.
    h.clock = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    add_guild(
        h.db_path, 1, ping="everyone", tz="America/Los_Angeles", ping_day="2026-10-01", ping_count=3
    )
    add_guild(h.db_path, 2, ping="everyone", tz="Asia/Tokyo", ping_day="2026-10-01", ping_count=3)

    await h.run([item(CODE_A, published=h.clock - timedelta(hours=1))])

    assert not h.sent(1)[0].ping  # Los Angeles: same local day, budget spent
    assert h.sent(2)[0].ping  # Tokyo: a new local day, fresh budget
    tokyo = shift_row(h.db_path, 2)
    assert (tokyo.ping_day, tokyo.ping_count) == ("2026-10-02", 1)
    la = shift_row(h.db_path, 1)
    assert (la.ping_day, la.ping_count) == ("2026-10-01", 3)

    # And Los Angeles gets its own reset when its own midnight passes.
    h.clock = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    await h.run([item(CODE_B, published=h.clock - timedelta(hours=1))])
    assert h.sent(1)[0].ping
    assert (shift_row(h.db_path, 1).ping_day, shift_row(h.db_path, 1).ping_count) == (
        "2026-10-02",
        1,
    )


async def test_cap_of_zero_posts_without_ping_and_without_a_note(db_path, cfg):
    cfg0 = cfg.model_copy(update={"shift": cfg.shift.model_copy(update={"max_pings_per_day": 0})})
    harness = Harness(db_path, cfg0, NOW)
    seed(db_path)
    add_guild(db_path, 1, ping=ROLE)

    await harness.run([item(CODE_A)])

    assert not harness.sent(1)[0].ping
    assert harness.notices == []


# --- roundups ---


async def test_roundups_are_never_pinged_and_never_spend_the_cap(h):
    add_guild(h.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]

    released = await h.run([item(*many, url="https://example.com/megathread")])

    assert released == 6
    alerts = h.sent(1)
    assert alerts and all(not a.ping and a.ping_prefix == "" for a in alerts)
    assert all("@everyone" not in a.content for a in alerts)
    assert all("from a roundup" in a.content for a in alerts[:1])
    assert shift_row(h.db_path, 1).ping_count == 0
    assert set(post_status(h.db_path, 1).values()) == {"posted"}
    assert rows(h.db_path, "SELECT DISTINCT from_roundup FROM guild_code_posts") == [(1,)]
    assert rows(h.db_path, "SELECT DISTINCT from_roundup FROM alerted_codes") == [(1,)]


# --- eligibility and no backlog ---


async def test_enabling_alerts_gives_no_backlog(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])  # released while guild 2 didn't exist

    h.clock = NOW + timedelta(hours=1)
    add_guild(h.db_path, 2, ping="everyone", enabled_at=h.clock)
    # The old code turning up again posts nothing anywhere, and a new one
    # reaches guild 2 only from its enabled_at on.
    await h.run([item(CODE_A, published=h.clock), item(CODE_B, published=h.clock)])

    assert [a.codes for a in h.sent(2)] == [[CODE_B]]
    assert post_status(h.db_path, 2) == {CODE_B: "posted"}


async def test_a_guild_enabled_after_the_pass_time_is_not_eligible_yet(h):
    add_guild(h.db_path, 1, enabled_at=NOW + timedelta(minutes=1))

    await h.run([item(CODE_A)])

    assert h.sent(1) == []
    assert post_status(h.db_path, 1) == {}


async def test_disabling_and_re_enabling_moves_no_backlog_in(h):
    add_guild(h.db_path, 1)
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=False, channel_id=100, ping="none", now=lambda: NOW)
    await h.run([item(CODE_A)])
    assert h.sent(1) == []

    h.clock = NOW + timedelta(hours=2)
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="none", now=lambda: h.clock)
    await h.run([item(CODE_A, published=h.clock)])

    assert h.sent(1) == []  # CODE_A was released while it was off; it's history now


async def test_guild_not_following_a_shift_game_gets_nothing(h):
    add_guild(h.db_path, 1, games=("palworld",))
    add_guild(h.db_path, 2, games=("borderlands4", "palworld"))

    await h.run([item(CODE_A)])

    assert h.sent(1) == []
    assert len(h.sent(2)) == 1


async def test_unset_up_and_disabled_guilds_get_nothing(h):
    add_guild(h.db_path, 1, set_up=False)
    add_guild(h.db_path, 2, enabled=False)

    await h.run([item(CODE_A)])

    assert h.sent(1) == [] and h.sent(2) == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM guild_code_posts") == [(0,)]


async def test_empty_shift_games_means_every_guild_with_alerts_on(db_path, cfg):
    cfg_all = cfg.model_copy(update={"shift": cfg.shift.model_copy(update={"games": []})})
    harness = Harness(db_path, cfg_all, NOW)
    seed(db_path)
    add_guild(db_path, 1, games=("palworld",))

    await harness.run([item(CODE_A)])

    assert len(harness.sent(1)) == 1


# --- the friend's server after the import ---


async def test_imported_codes_are_never_reposted_and_the_friend_keeps_everyone(h):
    # v2.2 already announced CODE_A; the import copies that into guild_code_posts
    # and carries today's spent budget (2 of 3) over.
    with closing(connect(h.db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
                "pinged, status) VALUES (?, ?, 'Gearbox Blog', 'https://x.test', 1, 'posted')",
                (CODE_A, EARLIER.isoformat()),
            )
    add_guild(h.db_path, 77, ping="everyone", ping_day="2026-10-01", ping_count=2)
    with closing(connect(h.db_path)) as conn:
        with conn:
            repo.backfill_guild_code_posts(conn, 77)

    await h.run([item(CODE_A), item(CODE_B)])

    (alert,) = h.sent(77)
    assert alert.codes == [CODE_B]
    assert alert.content.startswith("@everyone ") and CODE_A not in alert.content
    assert shift_row(h.db_path, 77).ping_count == 3
    # The budget is now spent: the next code posts, unpinged, as it would have under v2.2.
    await h.run([item(CODE_C)])
    assert not h.sent(77)[0].ping
    assert post_status(h.db_path, 77) == {CODE_A: "posted", CODE_B: "posted", CODE_C: "posted"}


# --- retry, recovery, hook ---


async def test_a_retry_strips_the_role_prefix_and_the_ping(h):
    add_guild(h.db_path, 1, ping=ROLE)
    h.fail_with[1] = [PublishError("timeout")]

    await h.run([item(CODE_A)])

    (alert,) = h.sent(1)
    assert not alert.ping
    assert f"<@&{ROLE}>" not in alert.content
    assert post_status(h.db_path, 1) == {CODE_A: "posted"}


def test_strip_ping_removes_exactly_the_stored_prefix():
    alert = RenderedAlert(
        content=f"<@&{ROLE}> **New SHiFT code**",
        codes=[CODE_A],
        ping=True,
        ping_prefix=f"<@&{ROLE}> ",
    )
    stripped = _strip_ping(alert)
    assert stripped.content == "**New SHiFT code**"
    assert not stripped.ping and stripped.ping_prefix == ""


def test_strip_ping_still_strips_a_v2_alert_with_no_stored_prefix():
    alert = RenderedAlert(content="@everyone **New SHiFT code**", codes=[CODE_A], ping=True)
    assert _strip_ping(alert).content == "**New SHiFT code**"


async def test_failure_after_first_message_fails_the_rest_and_keeps_the_first(h):
    add_guild(h.db_path, 1, ping="none")
    codes = [f"{chr(65 + i)}{i:03d}1-AAAAA-AAAAA-AAAAA-AAAAA" for i in range(40)]
    h.fail_with[1] = []

    class Flaky(FakePoster):
        async def post(self, alert):
            if self.sent:
                raise RuntimeError("second message dies")
            return await super().post(alert)

    h.factory = lambda g, c, p, n: Flaky(g, c, p)
    h.posters.clear()
    await detect_and_fan_out(
        h.deps(),
        [item(c, url=f"https://example.com/{i}") for i, c in enumerate(codes)],
        seeding_ok=True,
    )

    statuses = set(post_status(h.db_path, 1).values())
    assert statuses == {"posted", "failed"}
    assert [g for g, _ in h.notices] == [1]


async def test_recover_pending_notifies_each_guild_and_returns_a_count(h):
    add_guild(h.db_path, 1)
    add_guild(h.db_path, 2)
    with closing(connect(h.db_path)) as conn:
        repo.record_released_codes(
            conn,
            [(CODE_A, "s", "https://x.test", False), (CODE_B, "s", "https://x.test", False)],
            now=lambda: NOW,
            # Changed with the delivery queue: a claim now moves a `queued` row to
            # `pending`, so the release has to queue for the guilds first.
            queue_for_games=[],
        )
        repo.claim_guild_codes(
            conn, 1, [CODE_A, CODE_B], pinged=False, local_day="2026-10-01", now=lambda: NOW
        )
        repo.claim_guild_codes(
            conn, 2, [CODE_A], pinged=False, local_day="2026-10-01", now=lambda: NOW
        )

    total = await recover_pending_guild_codes(h.db_path, h.notify_guild)

    assert total == 3
    by_guild = {g: t for g, t in h.notices}
    assert set(by_guild) == {1, 2}
    assert CODE_A in by_guild[1] and CODE_B in by_guild[1]
    assert CODE_B not in by_guild[2]
    # Changed with the queue: guild 2's CODE_B was queued at release and never claimed, so
    # recovery leaves it `queued` for delivery instead of it not existing.
    assert set(rows(h.db_path, "SELECT DISTINCT status FROM guild_code_posts")) == {
        ("failed",),
        ("queued",),
    }
    assert await recover_pending_guild_codes(h.db_path, h.notify_guild) == 0


async def test_the_hook_returns_the_released_count_and_judges_seeding_from_results(db_path, cfg):
    harness = Harness(db_path, cfg, NOW)
    add_guild(db_path, 1)
    hook = make_shift_hook(harness.deps())
    ok = CollectorResult(source_name="s", source_type="rss", items=[], error=None, skipped=None)
    bad = CollectorResult(source_name="t", source_type="rss", items=[], error="boom", skipped=None)

    assert (
        await hook([item(CODE_A)], [bad]) == 0
    )  # unhealthy: not seeded, nothing recorded as seeded
    assert rows(db_path, "SELECT value FROM alert_state WHERE key = 'seeded_at'") == []
    assert await hook([item(CODE_A)], [ok]) == 0  # healthy first pass seeds
    assert await hook([item(CODE_B)], [ok]) == 1


# --- the repo pieces the fan-out leans on ---


def test_claim_guild_codes_takes_whatever_is_still_queued_and_spends_the_ping_once(db_path):
    # Changed with QA M1: the claim used to be all-or-nothing (ClaimLostError), so a
    # pass that lost one code to a concurrent pass stranded every sibling. Now it
    # takes what's still queued, says what it took, and spends the ping for those.
    add_guild(db_path, 1, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn,
            [(CODE_A, "s", "https://x.test", False), (CODE_B, "s", "https://x.test", False)],
            now=lambda: NOW,
            queue_for_games=[],
        )
        repo.claim_guild_codes(
            conn, 1, [CODE_A], pinged=False, local_day="2026-10-01", now=lambda: NOW
        )
        got = repo.claim_guild_codes(
            conn, 1, [CODE_B, CODE_A], pinged=True, local_day="2026-10-01", now=lambda: NOW
        )
        assert got.codes == [CODE_B] and got.pinged is True
        assert repo.get_shift(conn, 1).ping_count == 1
        assert repo.queued_guild_codes(conn, 1) == []


def test_claim_guild_codes_with_nothing_queued_claims_nothing_and_spends_nothing(db_path):
    add_guild(db_path, 1, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")
        got = repo.claim_guild_codes(conn, 1, [CODE_A], pinged=True, local_day="2026-10-01")
        assert got == ([], False)
        assert repo.get_shift(conn, 1).ping_count == 0


def test_claim_guild_codes_ping_codes_decides_whether_the_claimed_ones_deserve_a_ping(db_path):
    add_guild(db_path, 1, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn,
            [(CODE_A, "s", "https://x.test", False), (CODE_B, "s", "https://x.test", False)],
            now=lambda: NOW,
            queue_for_games=[],
        )
        got = repo.claim_guild_codes(
            conn, 1, [CODE_B], pinged=True, local_day="2026-10-01", ping_codes={CODE_A}
        )
        assert got == ([CODE_B], False)  # the one trusted code isn't among the claimed
        assert repo.get_shift(conn, 1).ping_count == 0


def test_a_late_mark_cannot_flip_a_startup_failed_row_back_to_posted(db_path):
    add_guild(db_path, 1, ping="none")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")
        repo.fail_pending_guild_codes(conn)
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=5)
        repo.mark_guild_codes_failed(conn, 1, [CODE_A])
        assert rows(db_path, "SELECT status, message_id FROM guild_code_posts") == [
            ("failed", None)
        ]


def test_a_locked_database_surfaces_as_operational_error_not_a_rollback_error(db_path):
    # QA M3: BEGIN IMMEDIATE itself failing leaves no transaction to roll back, and the
    # unguarded ROLLBACK used to replace the real "database is locked" with its own error.
    add_guild(db_path, 1, ping="everyone")
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        victim = sqlite3.connect(db_path, timeout=0.05)
        victim.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            repo.claim_guild_codes(victim, 1, [CODE_A], pinged=False, local_day="2026-10-01")
        victim.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_record_released_codes_reports_only_what_it_inserted(db_path):
    with closing(connect(db_path)) as conn:
        first = repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW
        )
        second = repo.record_released_codes(
            conn,
            [(CODE_A, "s", "https://x.test", False), (CODE_B, "s", "https://x.test", True)],
            now=lambda: NOW,
        )
    assert (first, second) == ([CODE_A], [CODE_B])
