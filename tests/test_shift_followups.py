"""D14 (B+): a community code that a second, independent source confirms gets one pinged follow-up.

The headline test replays the real `39FJ3` timeline from prod (first seen on
r/Borderlands4, seen again by the Bluesky search about 20 hours later). The
rest pin the edges: the 24 hour window, "independent" meaning a different
source name, roundups never counting, one follow-up per server ever, the cap,
and the CLI preview staying out of it. Real temp databases, scripted fakes,
no network.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_shift_fanout_adversarial import (
    CODE_A,
    CODE_B,
    EARLIER,
    V3,
    FakeChannel,
    Harness,
    _code,
    add_guild,
    rows,
    seed,
    shift_row,
)

from newsbot.bot.format import FOLLOWUP_MAX_CODES, render_code_alerts, render_followup_alert
from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.decide import CodeCandidate, CodeSighting, aggregate
from newsbot.shift.fanout import deliver_queued_codes, detect_and_fan_out
from newsbot.store import repo
from newsbot.store.db import connect, migrate

# The real one's first sighting (r/Borderlands4, 2026-09-30 19:44 UTC); the code itself is made up.
T0 = datetime(2026, 9, 30, 19, 44, tzinfo=UTC)
CODE = "39FJ3-AAAAA-BBBBB-CCCCC-DDDDD"
REDDIT = "r/Borderlands4"
BLUESKY = 'Bluesky "Borderlands 4" search'


def sighting_item(source, trust, *codes, at):
    """An item from `source` published an hour before `at`, in scope for borderlands4."""
    return RawItem(
        url=f"https://example.com/{source.replace(' ', '-').replace('/', '-')}/{codes[0][:5]}",
        title="Borderlands 4 SHiFT code: " + " ".join(codes),
        excerpt="",
        source_name=source,
        trust=trust,
        published_at=at - timedelta(hours=1),
    )


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
    seed(db_path)
    return Harness(db_path, cfg, T0)


def at(h, delta):
    """Move the harness clock to T0 plus `delta` and hand the harness back."""
    h.clock = T0 + delta
    return h


async def first_sighting(h, source=REDDIT, trust="community"):
    await at(h, timedelta(0)).run([sighting_item(source, trust, CODE, at=h.clock)])


async def later(h, delta, source=BLUESKY, trust="community"):
    at(h, delta)
    await h.run([sighting_item(source, trust, CODE, at=h.clock)])


def followup_rows(db_path):
    return rows(
        db_path, "SELECT guild_id, code, status, pinged FROM guild_code_followups ORDER BY 1, 2"
    )


def followup_alerts(h, guild_id):
    return [a for a in h.sent(guild_id) if a.content.split(" ", 1)[-1].startswith("Confirmed")]


# --- the real timeline ---


async def test_the_39fj3_timeline_posts_unpinged_then_one_pinged_followup(h):
    add_guild(h.db_path, 1, ping="everyone")

    await first_sighting(h)
    original = h.sent(1)
    assert len(original) == 1 and original[0].ping is False and CODE in original[0].content
    assert shift_row(h.db_path, 1).ping_count == 0

    await later(h, timedelta(hours=20))
    sent = h.sent(1)
    assert len(sent) == 1 and sent[0].ping is True
    assert sent[0].content == f"@everyone Confirmed by a second source: `{CODE}`"
    assert sent[0].ping_prefix == "@everyone "
    assert shift_row(h.db_path, 1).ping_count == 1
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]

    # The next hourly passes keep seeing the code (both sources are still in
    # their feeds) and say nothing more.
    for hours in (21, 22):
        await later(h, timedelta(hours=hours))
        assert h.sent(1) == []
    await later(h, timedelta(hours=23), source=REDDIT)
    assert h.sent(1) == []
    assert shift_row(h.db_path, 1).ping_count == 1


async def test_the_followup_goes_through_the_real_poster_with_the_servers_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = FakeChannel(can_mention_everyone=True)
    h.channels = {1: channel}

    await first_sighting(h)
    await later(h, timedelta(hours=20))

    assert len(channel.sent) == 2
    (orig_content, orig_wire), (content, wire) = channel.sent
    assert wire["parse"] == ["everyone"] and orig_wire.get("parse", []) == []
    assert content == f"@everyone Confirmed by a second source: `{CODE}`"


async def test_a_role_server_gets_its_role_pinged_and_nothing_else(h):
    role = "1234567890123456789"
    add_guild(h.db_path, 1, ping=role)
    channel = FakeChannel(roles={int(role): type("R", (), {"mentionable": True})()})
    h.channels = {1: channel}

    await first_sighting(h)
    await later(h, timedelta(hours=20))

    content, wire = channel.sent[1]
    assert content == f"<@&{role}> Confirmed by a second source: `{CODE}`"
    assert "everyone" not in wire.get("parse", []) and wire["roles"] == [int(role)]


# --- the window and "independent" ---


async def test_a_second_source_at_exactly_24_hours_still_counts(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=24))
    assert len(h.sent(1)) == 1 and h.sent(1)[0].ping is True


async def test_a_second_source_at_25_hours_produces_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=25))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []
    assert shift_row(h.db_path, 1).ping_count == 0
    # ...and a late third source doesn't revive it: it's outside the window too.
    await later(h, timedelta(hours=26), source="Some Other Source")
    assert followup_rows(h.db_path) == []


async def test_the_same_source_seen_twice_produces_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=5), source=REDDIT)
    await later(h, timedelta(hours=19), source=REDDIT)
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


async def test_a_different_post_from_the_same_source_name_is_not_independent(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    other_post = RawItem(
        url="https://example.com/another-reddit-thread",
        title="Borderlands 4 SHiFT code again: " + CODE,
        excerpt="",
        source_name=REDDIT,
        trust="community",
        published_at=T0 + timedelta(hours=2),
    )
    await at(h, timedelta(hours=3)).run([other_post])
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


async def test_a_roundup_as_the_second_source_produces_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    megathread = sighting_item(
        BLUESKY, "community", CODE, *[_code(i) for i in range(6)], at=T0 + timedelta(hours=20)
    )
    await at(h, timedelta(hours=20)).run([megathread])
    assert followup_alerts(h, 1) == [] and followup_rows(h.db_path) == []
    assert all(a.ping is False for a in h.sent(1))
    assert shift_row(h.db_path, 1).ping_count == 0


async def test_an_official_second_sighting_produces_a_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=3), source="Gearbox Blog", trust="official")
    assert len(followup_alerts(h, 1)) == 1 and h.sent(1)[0].ping is True


async def test_a_press_second_sighting_produces_a_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=3), source="Some Press Site", trust="press")
    assert len(followup_alerts(h, 1)) == 1


async def test_an_officially_first_sighted_code_never_gets_a_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h, source="Gearbox Blog", trust="official")
    assert h.sent(1)[0].ping is True
    await later(h, timedelta(hours=5))
    await later(h, timedelta(hours=6), source="Another Source", trust="community")
    assert h.sent(1) == [] and followup_rows(h.db_path) == []
    assert shift_row(h.db_path, 1).ping_count == 1


async def test_two_sources_in_the_same_first_pass_ping_right_away(h):
    add_guild(h.db_path, 1, ping="everyone")
    await at(h, timedelta(0)).run(
        [
            sighting_item(REDDIT, "community", CODE, at=T0),
            sighting_item(BLUESKY, "community", CODE, at=T0),
        ]
    )
    assert len(h.sent(1)) == 1 and h.sent(1)[0].ping is True
    await later(h, timedelta(hours=2), source="A Third One")
    assert followup_rows(h.db_path) == []


# --- which originals are eligible ---


async def test_a_server_with_ping_off_gets_nothing(h):
    add_guild(h.db_path, 1, ping="none")
    await first_sighting(h)
    await later(h, timedelta(hours=20))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


async def test_a_server_that_turns_ping_on_after_the_original_is_not_followed_up(h):
    add_guild(h.db_path, 1, ping="none")
    await first_sighting(h)
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(
            conn,
            1,
            enabled=True,
            channel_id=100,
            ping="everyone",
            now=lambda: T0 + timedelta(hours=1),
        )
    await later(h, timedelta(hours=20))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


async def test_a_server_that_turns_ping_off_before_the_followup_skips_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(
            conn, 1, enabled=True, channel_id=100, ping="none", now=lambda: T0 + timedelta(hours=1)
        )
    await later(h, timedelta(hours=20))
    assert h.sent(1) == []
    assert followup_rows(h.db_path) == [(1, CODE, "skipped", 0)]


async def test_a_cap_spent_server_gets_nothing_and_its_cap_is_unchanged(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_shift SET ping_day = '2026-10-01', ping_count = 3")
    await later(h, timedelta(hours=20))  # 2026-10-01 15:44 UTC
    assert h.sent(1) == []
    assert shift_row(h.db_path, 1).ping_count == 3
    assert followup_rows(h.db_path) == [(1, CODE, "skipped", 0)]
    # Skipped is final: no unpinged repost later, even with the cap fresh again.
    await later(h, timedelta(hours=23))
    assert h.sent(1) == []


async def test_a_cap_reached_original_is_not_followed_up(h):
    # The original was a *trusted* code that lost its ping to the cap; that's not
    # the case B+ is for (the decision: no follow-up, to keep it simple).
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-09-30", ping_count=3)
    await first_sighting(h, source="Gearbox Blog", trust="official")
    assert h.sent(1)[0].ping is False
    await later(h, timedelta(hours=2))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


async def test_an_untrusted_code_in_a_pinged_mixed_batch_is_not_followed_up(h):
    add_guild(h.db_path, 1, ping="everyone")
    await at(h, timedelta(0)).run(
        [
            sighting_item("Gearbox Blog", "official", CODE_A, at=T0),
            sighting_item(REDDIT, "community", CODE, at=T0),
        ]
    )
    assert h.sent(1)[0].ping is True
    await later(h, timedelta(hours=2))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


async def test_a_roundup_original_is_not_followed_up(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = [CODE, *[_code(i) for i in range(6)]]
    await at(h, timedelta(0)).run([sighting_item(REDDIT, "community", *codes, at=T0)])
    assert all(a.ping is False for a in h.sent(1))
    await later(h, timedelta(hours=2))
    await later(h, timedelta(hours=3), source="Gearbox Blog", trust="official")
    assert followup_alerts(h, 1) == [] and followup_rows(h.db_path) == []


async def test_a_failed_original_is_not_followed_up(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with = {1: [PublishError("503", retryable=True)] * 4}
    await first_sighting(h)
    assert rows(h.db_path, "SELECT status FROM guild_code_posts") == [("failed",)]
    await later(h, timedelta(hours=2))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


# --- once per server, ever ---


async def test_a_restart_between_the_sightings_still_works(h, db_path, cfg):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    # A brand new harness (fresh deps, fresh locks, fresh posters) over the same file.
    fresh = Harness(db_path, cfg, T0 + timedelta(hours=20))
    await fresh.run([sighting_item(BLUESKY, "community", CODE, at=fresh.clock)])
    assert len(fresh.sent(1)) == 1 and fresh.sent(1)[0].ping is True
    # And a startup walk afterwards has nothing left to send.
    assert await deliver_queued_codes(fresh.deps(), startup=True) is not None
    assert followup_rows(db_path) == [(1, CODE, "posted", 1)]


async def test_a_followup_queued_before_a_crash_is_sent_by_the_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    at(h, timedelta(hours=20))
    # Release and queue without delivering: the process "died" right after.
    from newsbot.shift.fanout import _release

    await _release(
        h.deps(), [sighting_item(BLUESKY, "community", CODE, at=h.clock)], seeding_ok=True
    )
    assert followup_rows(h.db_path) == [(1, CODE, "queued", 0)]
    await h.deliver(startup=True)
    assert len(h.sent(1)) == 1 and followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_followup_pending_at_a_crash_fails_and_is_never_resent(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=20))
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_code_followups SET status = 'pending'")
    await h.deliver(startup=True)
    assert h.sent(1) == []
    assert followup_rows(h.db_path) == [(1, CODE, "failed", 1)]
    await later(h, timedelta(hours=21))
    assert h.sent(1) == []


async def test_overlapping_passes_send_one_followup(h, db_path, cfg):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    a = Harness(db_path, cfg, T0 + timedelta(hours=20))
    b = Harness(db_path, cfg, T0 + timedelta(hours=20))
    new_item = [sighting_item(BLUESKY, "community", CODE, at=a.clock)]
    await asyncio.gather(
        detect_and_fan_out(a.deps(), new_item, seeding_ok=True),
        detect_and_fan_out(b.deps(), new_item, seeding_ok=True),
    )
    assert len(a.sent(1)) + len(b.sent(1)) == 1
    assert shift_row(db_path, 1).ping_count == 1
    assert followup_rows(db_path) == [(1, CODE, "posted", 1)]


async def test_two_servers_each_get_their_own_followup(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    add_guild(h.db_path, 3, ping="none")
    await first_sighting(h)
    await later(h, timedelta(hours=20))
    one, two = followup_alerts(h, 1), followup_alerts(h, 2)
    assert len(one) == 1 and len(two) == 1 and h.sent(3) == []
    assert one[0].nonce != two[0].nonce  # per-server scope
    assert {g for g, *_ in followup_rows(h.db_path)} == {1, 2}
    assert shift_row(h.db_path, 1).ping_count == 1 and shift_row(h.db_path, 2).ping_count == 1


async def test_a_server_that_joined_after_the_original_is_not_followed_up(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    add_guild(h.db_path, 2, ping="everyone", enabled_at=T0 + timedelta(hours=1))
    await later(h, timedelta(hours=20))
    assert len(followup_alerts(h, 1)) == 1 and h.sent(2) == []


async def test_several_codes_confirmed_at_once_share_one_message_and_one_ping_unit(h):
    add_guild(h.db_path, 1, ping="everyone")
    await at(h, timedelta(0)).run([sighting_item(REDDIT, "community", CODE, CODE_A, at=T0)])
    at(h, timedelta(hours=20))
    await h.run([sighting_item(BLUESKY, "community", CODE, CODE_A, at=h.clock)])
    sent = h.sent(1)
    assert len(sent) == 1
    assert sent[0].content == f"@everyone Confirmed by a second source: `{CODE}`, `{CODE_A}`"
    assert shift_row(h.db_path, 1).ping_count == 1


# --- delivery reuses the alert machinery ---


async def test_a_429_retry_keeps_the_followups_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    h.fail_with = {1: [PublishError("429", rejected=True), PublishError("429", rejected=True)]}
    await later(h, timedelta(hours=20))
    sent = h.sent(1)
    assert len(sent) == 1 and sent[0].ping is True and sent[0].content.startswith("@everyone ")
    assert shift_row(h.db_path, 1).ping_count == 1


async def test_an_ambiguous_failure_strips_the_ping_from_the_retry(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    h.fail_with = {1: [PublishError("timeout")]}
    await later(h, timedelta(hours=20))
    sent = h.sent(1)
    assert len(sent) == 1 and sent[0].ping is False
    assert sent[0].content == f"Confirmed by a second source: `{CODE}`"
    assert shift_row(h.db_path, 1).ping_count == 1  # claimed once, retries never re-spend


async def test_a_followup_that_never_lands_is_failed_quietly_and_not_retried(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    h.fail_with = {1: [PublishError("503")] * 8}
    await later(h, timedelta(hours=20))
    assert followup_rows(h.db_path) == [(1, CODE, "failed", 1)]
    assert h.notices == []
    h.fail_with = {}
    await later(h, timedelta(hours=21))
    assert h.sent(1) == []


async def test_a_followup_nonce_differs_from_the_originals(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    original = h.sent(1)[0]
    await later(h, timedelta(hours=20))
    assert h.sent(1)[0].nonce != original.nonce


# --- the CLI preview ---


async def test_preview_sends_no_followups_and_spends_nothing(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    printed = []

    class Printer:
        async def post(self, alert):
            printed.append(alert)

    deps = h.deps(now=lambda: T0 + timedelta(hours=20))
    deps.preview = True
    deps.poster_for = lambda *_args: Printer()
    await detect_and_fan_out(
        deps,
        [sighting_item(BLUESKY, "community", CODE, at=T0 + timedelta(hours=20))],
        seeding_ok=True,
    )
    assert printed == []
    assert shift_row(h.db_path, 1).ping_count == 0
    assert followup_rows(h.db_path) == [(1, CODE, "queued", 0)]  # waiting for a real pass


# --- detection and storage ---


def test_sightings_keep_their_first_seen_time_and_only_ever_get_more_trusted(db_path):
    with closing(connect(db_path)) as conn:
        repo.record_code_sightings(conn, [(CODE, "S", False, True)], now=lambda: T0)
        repo.record_code_sightings(
            conn, [(CODE, "S", True, False)], now=lambda: T0 + timedelta(hours=9)
        )
        repo.record_code_sightings(
            conn, [(CODE, "S", False, True)], now=lambda: T0 + timedelta(hours=10)
        )
        row = conn.execute("SELECT trusted, roundup, seen_at FROM code_sightings").fetchone()
    assert (row["trusted"], row["roundup"], row["seen_at"]) == (1, 0, T0.isoformat())


def test_aggregate_treats_two_source_names_as_trusted_and_one_as_not():
    def s(source, trust="community", roundup=False):
        return CodeSighting(CODE, False, source, "https://e.com", trust, None, roundup)

    now = T0
    kwargs = {"now": now, "max_age": timedelta(hours=48)}
    assert aggregate([s("A")], **kwargs)[0].trusted is False
    assert aggregate([s("A"), s("A")], **kwargs)[0].trusted is False
    assert aggregate([s("A"), s("B")], **kwargs)[0].trusted is True
    assert aggregate([s("A"), s("B", roundup=True)], **kwargs)[0].trusted is False
    assert aggregate([s("A"), s("B", trust="press")], **kwargs)[0].trusted is True


def test_the_followup_text_is_short_and_uses_backticks():
    alert = render_followup_alert([CODE], ping_mention="@everyone", nonce_scope="1|100")
    assert alert.content == f"@everyone Confirmed by a second source: `{CODE}`"
    assert alert.ping is True and alert.codes == [CODE]
    other = render_code_alerts(
        [CodeCandidate(CODE, golden=False, source_name="s", item_url="https://e.com", fresh=True)],
        ping=True,
        ping_mention="@everyone",
        nonce_scope="1|100",
    )[0]
    assert alert.nonce != other.nonce and len(alert.nonce) <= 25


@pytest.mark.parametrize(
    "bad", [[], ["not a code"], [_code(i) for i in range(FOLLOWUP_MAX_CODES + 1)]]
)
def test_the_followup_renderer_refuses_bad_input(bad):
    with pytest.raises(ValueError):
        render_followup_alert(bad, ping_mention="@everyone")


async def test_a_code_confirmed_with_nothing_to_follow_up_is_a_quiet_noop(h):
    # No servers at all: detection still records sightings and confirms without error.
    await first_sighting(h)
    await later(h, timedelta(hours=20))
    assert followup_rows(h.db_path) == []
    assert rows(h.db_path, "SELECT COUNT(*) FROM code_sightings") == [(2,)]
    assert EARLIER < T0 and CODE_B  # (the shared fixtures' constants stay importable)
