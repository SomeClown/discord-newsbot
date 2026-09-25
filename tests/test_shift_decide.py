"""Tests for newsbot.shift.decide: the pure planner behind the SHiFT alert sweep.

No database, no clock, no network -- every case here is a list of
dataclasses in, a list of dataclasses out. `test_shift_sweep.py` covers the
I/O side (what actually gets written and posted); this file is the
contract for the judgment calls: fresh vs. stale, seeded vs. not, who gets
to spend today's ping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from newsbot.collectors.base import CollectorResult, RawItem
from newsbot.config import Topic
from newsbot.shift.decide import (
    CodeCandidate,
    CodeSighting,
    aggregate,
    pings_used_today,
    plan_alerts,
    seeding_healthy,
    sightings_from_items,
)
from newsbot.store.models import AlertState

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=48)


def _sighting(**kwargs) -> CodeSighting:
    defaults = dict(
        code=CODE_A,
        golden=False,
        source_name="Src",
        item_url="https://example.com/a",
        trust="community",
        published_at=NOW,
    )
    defaults.update(kwargs)
    return CodeSighting(**defaults)


def _item(**kwargs) -> RawItem:
    defaults = dict(
        url="https://example.com/a",
        title="A post",
        excerpt="",
        source_name="Src",
        trust="community",
        published_at=NOW,
        topics=None,
    )
    defaults.update(kwargs)
    return RawItem(**defaults)


# --- sightings_from_items: finding codes, and A6 game scoping ---


def test_sightings_from_items_finds_code_in_title():
    items = [_item(title=f"Code drop: {CODE_A}")]
    sightings = sightings_from_items(items, topics=[], alert_topics=[])
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_prefers_full_text_over_excerpt():
    items = [_item(excerpt="nothing here", full_text=f"deep in the post: {CODE_A}")]
    sightings = sightings_from_items(items, topics=[], alert_topics=[])
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_no_code_yields_nothing():
    items = [_item(title="no codes today")]
    assert sightings_from_items(items, topics=[], alert_topics=[]) == []


def test_sightings_from_items_sets_golden_flag():
    items = [_item(title=f"Golden Key code {CODE_A}")]
    sightings = sightings_from_items(items, topics=[], alert_topics=[])
    assert sightings[0].golden is True


def test_sightings_from_items_empty_alert_topics_scopes_to_everything():
    topics = [Topic(key="borderlands4", name="Borderlands 4")]
    items = [_item(title=f"unrelated post {CODE_A}", topics=("palworld",))]
    sightings = sightings_from_items(items, topics=topics, alert_topics=[])
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_scopes_to_configured_topics_by_keyword():
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"]),
        Topic(key="palworld", name="Palworld"),
    ]
    matching = _item(url="https://e/1", title=f"BL4 code: {CODE_A}")
    other = _item(url="https://e/2", title=f"Palworld code: {CODE_B}")
    sightings = sightings_from_items(
        [matching, other], topics=topics, alert_topics=["borderlands4"]
    )
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_scopes_dedicated_source_with_no_keyword_hit():
    topics = [Topic(key="borderlands4", name="Borderlands 4")]
    # A dedicated single-topic source counts even with no keyword hit at
    # all (SPEC-DEV 3 / A6) -- a Steam patch note that never says the
    # game's name by name.
    item = _item(title=f"v1.2 patch notes {CODE_A}", topics=("borderlands4",))
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_drops_items_outside_scoped_topics():
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"]),
        Topic(key="palworld", name="Palworld"),
    ]
    item = _item(title=f"Palworld code: {CODE_A}", topics=("palworld",))
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert sightings == []


def test_sightings_from_items_press_item_not_mentioning_bl4_never_alerts():
    # A press item with no `topics` pin gets checked against every topic
    # by keyword; one that never names Borderlands 4 (or an alias) at all
    # shouldn't count toward the scoped alert topic just because it's a
    # press item covering games in general.
    topics = [Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"])]
    item = _item(
        title=f"This week in gaming news: a code appeared, {CODE_A}",
        trust="press",
        topics=None,
    )
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert sightings == []


def test_sightings_from_items_diablo_item_never_alerts_when_scoped_to_bl4():
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"]),
        Topic(key="diablo4", name="Diablo IV"),
    ]
    item = _item(title=f"Diablo IV season code: {CODE_A}", topics=("diablo4",))
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert sightings == []


def test_sightings_from_items_item_matched_to_both_bl4_and_another_topic_still_counts():
    # An item explicitly pinned to more than one topic isn't a "dedicated"
    # single-topic source (SPEC-DEV 3's no-keyword-check rule only applies
    # when `len(item.topics) == 1`), so it goes through the normal
    # keyword match for each of its candidate topics. As long as it
    # confidently matches borderlands4 too, it should still count even
    # though it's also tagged for another topic.
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"]),
        Topic(key="palworld", name="Palworld"),
    ]
    item = _item(
        title=f"Crossover event: BL4 and Palworld code {CODE_A}",
        topics=("borderlands4", "palworld"),
    )
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_entity_only_match_never_alerts():
    # QA item 2: "Gearbox" is an entity, not a Borderlands 4 name/alias --
    # a press item naming only the studio is an uncertain (entity-only)
    # match, the same low-confidence signal the digest itself is happy to
    # show next to a human-readable headline but that this module's job
    # is to reject outright.
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"], entities=["Gearbox"])
    ]
    item = _item(
        title=f"Gearbox drops new Tiny Tina's Wonderlands SHiFT code {CODE_A}",
        trust="press",
        topics=None,
    )
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert sightings == []


def test_sightings_from_items_dedicated_source_still_counts_alongside_entity_only_item():
    # The flip side of the entity-only test above: a dedicated (single-
    # topic) BL4 source with no keyword hit at all is a confident match
    # (SPEC-DEV 3) and must still alert, in the same batch that also
    # contains an entity-only item that must not.
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"], entities=["Gearbox"])
    ]
    dedicated = _item(
        url="https://e/dedicated",
        title="v1.3 patch notes",
        excerpt=f"redeem {CODE_A}",
        topics=("borderlands4",),
    )
    entity_only = _item(
        url="https://e/entity-only",
        title=f"Gearbox drops new Tiny Tina's Wonderlands SHiFT code {CODE_B}",
        trust="press",
        topics=None,
    )
    sightings = sightings_from_items(
        [dedicated, entity_only], topics=topics, alert_topics=["borderlands4"]
    )
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_press_item_confidently_naming_the_topic_still_counts():
    # A press item that actually names "Borderlands 4" (a confident match,
    # not an entity-only one) still counts -- item 2 only tightens the
    # entity-only case, it must not accidentally exclude confident press
    # matches too.
    topics = [
        Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"], entities=["Gearbox"])
    ]
    item = _item(
        title=f"Borderlands 4 SHiFT code just dropped: {CODE_A}", trust="press", topics=None
    )
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert [s.code for s in sightings] == [CODE_A]


def test_sightings_from_items_dedicated_bl4_source_always_counts():
    # A dedicated (single-topic) source counts as a confident match with
    # no keyword check at all -- the flip side of the press-item test
    # above: this is what "dedicated BL4 sources do alert" means.
    topics = [Topic(key="borderlands4", name="Borderlands 4")]
    item = _item(title="v1.3 patch notes", excerpt=f"redeem {CODE_A}", topics=("borderlands4",))
    sightings = sightings_from_items([item], topics=topics, alert_topics=["borderlands4"])
    assert [s.code for s in sightings] == [CODE_A]


# --- aggregate ---


def test_aggregate_fresh_undated_is_fresh():
    sightings = [_sighting(published_at=None)]
    candidates = aggregate(sightings, now=NOW, max_age=MAX_AGE)
    assert candidates[0].fresh is True


def test_aggregate_exactly_48h_old_is_fresh():
    published = NOW - timedelta(hours=48)
    candidates = aggregate([_sighting(published_at=published)], now=NOW, max_age=MAX_AGE)
    assert candidates[0].fresh is True


def test_aggregate_47h59m_old_is_fresh():
    published = NOW - timedelta(hours=47, minutes=59)
    candidates = aggregate([_sighting(published_at=published)], now=NOW, max_age=MAX_AGE)
    assert candidates[0].fresh is True


def test_aggregate_48h01m_old_is_stale():
    published = NOW - timedelta(hours=48, minutes=1)
    candidates = aggregate([_sighting(published_at=published)], now=NOW, max_age=MAX_AGE)
    assert candidates[0].fresh is False


def test_aggregate_mixed_ages_for_same_code_is_fresh():
    stale = _sighting(source_name="Old", published_at=NOW - timedelta(hours=72))
    fresh = _sighting(source_name="New", published_at=NOW - timedelta(hours=1))
    candidates = aggregate([stale, fresh], now=NOW, max_age=MAX_AGE)
    assert len(candidates) == 1
    assert candidates[0].fresh is True


def test_aggregate_golden_true_if_any_sighting_mentions_it():
    plain = _sighting(golden=False)
    golden = _sighting(golden=True)
    candidates = aggregate([plain, golden], now=NOW, max_age=MAX_AGE)
    assert candidates[0].golden is True


def test_aggregate_prefers_official_trust_for_shown_source():
    community = _sighting(source_name="Community", trust="community", item_url="https://e/c")
    official = _sighting(source_name="Official", trust="official", item_url="https://e/o")
    candidates = aggregate([community, official], now=NOW, max_age=MAX_AGE)
    assert candidates[0].source_name == "Official"
    assert candidates[0].item_url == "https://e/o"


def test_aggregate_same_trust_prefers_earliest_dated():
    later = _sighting(source_name="Later", published_at=NOW - timedelta(hours=1))
    earlier = _sighting(source_name="Earlier", published_at=NOW - timedelta(hours=5))
    candidates = aggregate([later, earlier], now=NOW, max_age=MAX_AGE)
    assert candidates[0].source_name == "Earlier"


def test_aggregate_ties_break_on_first_seen():
    first = _sighting(source_name="First", published_at=NOW)
    second = _sighting(source_name="Second", published_at=NOW)
    candidates = aggregate([first, second], now=NOW, max_age=MAX_AGE)
    assert candidates[0].source_name == "First"


def test_aggregate_stale_dated_plus_undated_sighting_is_fresh():
    # One stale, dated sighting and one undated sighting of the same
    # code: the undated sighting is treated as fresh (no date to judge it
    # stale by), so the aggregate is fresh even though the only dated
    # sighting is old.
    stale_dated = _sighting(source_name="Old", published_at=NOW - timedelta(hours=200))
    undated = _sighting(source_name="Mystery", published_at=None)
    candidates = aggregate([stale_dated, undated], now=NOW, max_age=MAX_AGE)
    assert len(candidates) == 1
    assert candidates[0].fresh is True


def test_aggregate_only_stale_dated_sightings_is_stale():
    a = _sighting(source_name="Old A", published_at=NOW - timedelta(hours=72))
    b = _sighting(source_name="Old B", published_at=NOW - timedelta(hours=100))
    candidates = aggregate([a, b], now=NOW, max_age=MAX_AGE)
    assert candidates[0].fresh is False


def test_aggregate_golden_true_even_if_only_stale_sighting_mentions_it():
    # `golden` is "any sighting mentions it", independent of `fresh` --
    # a code that ends up recorded `too_old` (never posted) still carries
    # the right golden flag in case that judgment ever matters again.
    stale_golden = _sighting(
        golden=True, source_name="Old", published_at=NOW - timedelta(hours=200)
    )
    candidates = aggregate([stale_golden], now=NOW, max_age=MAX_AGE)
    assert candidates[0].golden is True
    assert candidates[0].fresh is False


def test_aggregate_deterministic_first_seen_order():
    sightings = [
        _sighting(code=CODE_B),
        _sighting(code=CODE_A),
        _sighting(code=CODE_B),
    ]
    candidates = aggregate(sightings, now=NOW, max_age=MAX_AGE)
    assert [c.code for c in candidates] == [CODE_B, CODE_A]


# --- seeding_healthy ---


def _result(name, *, error=None, skipped=None):
    return CollectorResult(
        source_name=name, source_type="rss", items=[], error=error, skipped=skipped
    )


def test_seeding_healthy_all_succeed():
    results = [_result("a"), _result("b")]
    assert seeding_healthy(results) is True


def test_seeding_healthy_half_succeed_is_healthy():
    results = [_result("a"), _result("b", error="boom")]
    assert seeding_healthy(results) is True


def test_seeding_healthy_less_than_half_succeed_is_unhealthy():
    results = [_result("a"), _result("b", error="x"), _result("c", error="y")]
    assert seeding_healthy(results) is False


def test_seeding_healthy_no_successes_is_unhealthy():
    results = [_result("a", error="x")]
    assert seeding_healthy(results) is False


def test_seeding_healthy_empty_results_is_unhealthy():
    assert seeding_healthy([]) is False


def test_seeding_healthy_ignores_skipped_collectors():
    results = [_result("a"), _result("b", skipped="quota"), _result("c", skipped="auth")]
    assert seeding_healthy(results) is True


def test_seeding_healthy_all_skipped_is_unhealthy():
    assert seeding_healthy([_result("a", skipped="quota")]) is False


# --- pings_used_today ---


def test_pings_used_today_matches_stored_day():
    state = AlertState(
        seeded=True,
        last_sweep_at=None,
        last_sweep_summary=None,
        ping_day="2026-09-25",
        ping_count=2,
    )
    assert pings_used_today(state, "2026-09-25") == 2


def test_pings_used_today_stale_day_reads_as_zero():
    state = AlertState(
        seeded=True,
        last_sweep_at=None,
        last_sweep_summary=None,
        ping_day="2026-09-24",
        ping_count=3,
    )
    assert pings_used_today(state, "2026-09-25") == 0


def test_pings_used_today_no_prior_day_is_zero():
    state = AlertState(
        seeded=True, last_sweep_at=None, last_sweep_summary=None, ping_day=None, ping_count=0
    )
    assert pings_used_today(state, "2026-09-25") == 0


def _la_day(utc_dt: datetime) -> str:
    return utc_dt.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


def test_pings_used_today_resets_at_la_midnight_not_utc_midnight():
    # 2026-09-25 07:30 UTC is still 2026-09-25 00:30 America/Los_Angeles
    # (UTC-7 in September) -- UTC's date has already rolled to the 25th
    # hours before LA's does, and pings_used_today has to agree with LA's
    # calendar, not UTC's.
    still_previous_utc_day_la_today = datetime(2026, 9, 25, 7, 30, tzinfo=UTC)
    today = _la_day(still_previous_utc_day_la_today)
    assert today == "2026-09-25"
    state = AlertState(
        seeded=True,
        last_sweep_at=None,
        last_sweep_summary=None,
        ping_day="2026-09-24",
        ping_count=1,
    )
    assert pings_used_today(state, today) == 0


def test_pings_used_today_across_spring_forward():
    # 2026-03-08 is the US spring-forward day; LA jumps from PST (UTC-8) to
    # PDT (UTC-7) at 02:00 local. The day string itself shouldn't care --
    # it's still one calendar day on either side of the jump.
    before = datetime(2026, 3, 8, 9, 0, tzinfo=UTC)  # 01:00 PST
    after = datetime(2026, 3, 8, 11, 0, tzinfo=UTC)  # 04:00 PDT
    assert _la_day(before) == _la_day(after) == "2026-03-08"
    state = AlertState(
        seeded=True,
        last_sweep_at=None,
        last_sweep_summary=None,
        ping_day="2026-03-08",
        ping_count=1,
    )
    assert pings_used_today(state, _la_day(after)) == 1


def test_pings_used_today_across_fall_back():
    # 2026-11-01 is the US fall-back day; LA repeats 01:00-02:00 local.
    # Still one calendar day, same story.
    before = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)  # 01:00 PDT (pre-fallback)
    after = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)  # 05:00 PST (post-fallback)
    assert _la_day(before) == _la_day(after) == "2026-11-01"
    state = AlertState(
        seeded=True,
        last_sweep_at=None,
        last_sweep_summary=None,
        ping_day="2026-11-01",
        ping_count=2,
    )
    assert pings_used_today(state, _la_day(before)) == 2


# --- plan_alerts ---


def _candidate(**kwargs) -> CodeCandidate:
    defaults = dict(
        code=CODE_A, golden=False, source_name="Src", item_url="https://e/a", fresh=True
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


def test_plan_alerts_drops_known_codes_entirely():
    plan = plan_alerts(
        [_candidate()], known={CODE_A}, seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert plan.silent == []
    assert plan.to_post == []
    assert plan.ping is False


def test_plan_alerts_unseeded_records_everything_silently():
    plan = plan_alerts(
        [_candidate()], known=set(), seeded=False, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert plan.silent == [(_candidate(), "seeded")]
    assert plan.to_post == []
    assert plan.ping is False
    assert plan.mark_seeded is True


def test_plan_alerts_unseeded_unhealthy_does_not_mark_seeded():
    plan = plan_alerts(
        [_candidate()], known=set(), seeded=False, seeding_ok=False, pings_today=0, max_pings=3
    )
    assert plan.mark_seeded is False


def test_plan_alerts_seeded_stale_recorded_too_old_never_posts():
    stale = _candidate(fresh=False)
    plan = plan_alerts(
        [stale], known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert plan.silent == [(stale, "too_old")]
    assert plan.to_post == []
    assert plan.ping is False


def test_plan_alerts_seeded_fresh_posts_with_ping_under_cap():
    fresh = _candidate(fresh=True)
    plan = plan_alerts(
        [fresh], known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert plan.to_post == [fresh]
    assert plan.ping is True
    assert plan.cap_reached is False


def test_plan_alerts_cap_2_of_3_still_pings():
    fresh = _candidate(fresh=True)
    plan = plan_alerts(
        [fresh], known=set(), seeded=True, seeding_ok=True, pings_today=2, max_pings=3
    )
    assert plan.ping is True
    assert plan.cap_reached is False


def test_plan_alerts_cap_3_of_3_withholds_ping():
    fresh = _candidate(fresh=True)
    plan = plan_alerts(
        [fresh], known=set(), seeded=True, seeding_ok=True, pings_today=3, max_pings=3
    )
    assert plan.to_post == [fresh]
    assert plan.ping is False
    assert plan.cap_reached is True


def test_plan_alerts_max_pings_zero_never_pings_but_still_posts():
    fresh = _candidate(fresh=True)
    plan = plan_alerts(
        [fresh], known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=0
    )
    assert plan.to_post == [fresh]
    assert plan.ping is False
    assert plan.cap_reached is True


def test_plan_alerts_no_fresh_candidates_cap_not_reached():
    stale = _candidate(fresh=False)
    plan = plan_alerts(
        [stale], known=set(), seeded=True, seeding_ok=True, pings_today=3, max_pings=3
    )
    assert plan.cap_reached is False


def test_plan_alerts_many_codes_one_ping_decision():
    candidates = [_candidate(code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA") for i in range(10)]
    plan = plan_alerts(
        candidates, known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert len(plan.to_post) == 10
    assert plan.ping is True


def test_plan_alerts_seeded_preserves_first_seen_order():
    a = _candidate(code=CODE_A)
    b = _candidate(code=CODE_B)
    plan = plan_alerts(
        [a, b], known=set(), seeded=True, seeding_ok=True, pings_today=0, max_pings=3
    )
    assert plan.to_post == [a, b]
