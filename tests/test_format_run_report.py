"""Tests for `render_run_report` (design.md §6): the admin-channel run report.

Same fixture-building style as test_format.py -- plain dataclasses, no
Discord connection needed, since `render_run_report` (like the rest of
`format.py`) only ever builds a string.
"""

from __future__ import annotations

from datetime import date, timedelta

from newsbot.bot.format import discord_len, render_run_report
from newsbot.collectors.base import CollectorResult
from newsbot.config import Topic
from newsbot.pipeline.summarize import StoryDraft, TopicSummary
from newsbot.store.models import Usage

RUN_DATE = date(2026, 9, 27)  # a Sunday
BL4 = Topic(key="borderlands4", name="Borderlands 4", aliases=[], entities=[])
PALWORLD = Topic(key="palworld", name="Palworld", aliases=[], entities=[])
DIABLO4 = Topic(key="diablo4", name="Diablo IV", aliases=[], entities=[])
TOPICS = [BL4, PALWORLD, DIABLO4]

_EMPTY_USAGE = Usage(input_tokens=0, output_tokens=0)


def _story(n=1):
    return [
        StoryDraft(
            headline=f"Headline {i}",
            summary="Summary.",
            label="official",
            item_urls=[f"https://example.com/{i}"],
            update_of_story_id=None,
        )
        for i in range(n)
    ]


def _summary(topic_key, n_stories=1, *, fallback=False, note=None):
    return TopicSummary(
        topic_key=topic_key,
        stories=_story(n_stories),
        fallback=fallback,
        note=note,
        usage=_EMPTY_USAGE,
    )


def _ok_result(name):
    return CollectorResult(name, "rss", [])


def _render(**overrides):
    kwargs = {
        "status": "ok",
        "run_date": RUN_DATE,
        "run_kind": "scheduled",
        "topics": TOPICS,
        "summaries": {
            "borderlands4": _summary("borderlands4", 2),
            "palworld": _summary("palworld", 1),
            "diablo4": _summary("diablo4", 4),
        },
        "fallback_items": {},
        "results": [_ok_result(f"src{i}") for i in range(3)],
        "usage": Usage(input_tokens=8000, output_tokens=1500),
        "duration": timedelta(minutes=1, seconds=52),
        "notes": [],
        "guild_id": 123456789012345678,
        "channel_id": 123456789012345680,
        "header_message_id": 555,
    }
    kwargs.update(overrides)
    return render_run_report(**kwargs)


# --- ok / partial happy path ---


def test_ok_report_matches_the_design_doc_example_shape():
    text = _render()
    lines = text.splitlines()
    assert lines[0] == "✅ **Digest posted** · Sun Sep 27 (scheduled)"
    assert lines[1] == "7 stories: Borderlands 4 2 · Palworld 1 · Diablo IV 4"
    assert lines[2] == "Sources: 3 of 3 ok"
    assert lines[3].startswith("Claude: ~$0.02 · took 1m52s")
    assert "[jump to digest](<https://discord.com/channels/" in lines[3]
    assert "/555>)" in lines[3]


def test_partial_report_uses_the_gaps_emoji_and_wording():
    text = _render(status="partial")
    assert text.startswith("⚠️ **Digest posted with gaps**")


def test_partial_report_includes_a_notes_line_from_outcome_notes():
    text = _render(status="partial", notes=["Blizzard News: quota exceeded"])
    assert "Notes: Blizzard News: quota exceeded" in text


def test_ok_report_has_no_notes_line_even_if_notes_given():
    # Design: the Notes line only shows up on a partial run.
    text = _render(status="ok", notes=["shouldn't show"])
    assert "Notes:" not in text


def test_partial_report_with_no_notes_omits_the_notes_line():
    text = _render(status="partial", notes=[])
    assert "Notes:" not in text


# --- run kind label ---


def test_run_kind_label_is_shown_verbatim():
    for kind in ("scheduled", "catch-up", "run-now"):
        text = _render(run_kind=kind)
        assert f"({kind})" in text


def test_run_now_report_never_mentions_a_user_id():
    # design.md: run-now's report never says who ran it.
    text = _render(run_kind="run-now")
    assert "user" not in text.lower()
    assert "id" not in text.lower()


# --- story counts ---


def test_story_counts_are_per_topic_in_config_order_including_zero():
    summaries = {"borderlands4": _summary("borderlands4", 3)}
    text = _render(summaries=summaries, fallback_items={})
    assert "3 stories: Borderlands 4 3 · Palworld 0 · Diablo IV 0" in text


def test_zero_story_day_reports_zero_for_every_topic():
    text = _render(summaries={}, fallback_items={})
    assert "0 stories: Borderlands 4 0 · Palworld 0 · Diablo IV 0" in text


def test_fallback_topic_counts_its_fallback_headlines():
    # design.md: a topic that fell back to a plain headline list counts
    # those headlines, not "0" and not the (nonexistent) story count --
    # matching the digest header's own _story_count behavior.
    summaries = {"borderlands4": _summary("borderlands4", 0, fallback=True, note="unavailable")}
    fallback_items = {"borderlands4": ["item1", "item2", "item3"]}
    text = _render(summaries=summaries, fallback_items=fallback_items, status="partial")
    assert "3 stories: Borderlands 4 3 · Palworld 0 · Diablo IV 0" in text


# --- sources line ---


def test_sources_line_counts_this_runs_results_not_source_health():
    results = [_ok_result("a"), _ok_result("b"), CollectorResult("c", "rss", [], error="boom")]
    text = _render(results=results, status="partial")
    assert "Sources: 2 of 3 ok (c: boom)" in text


def test_sources_line_shows_skipped_as_well_as_errored():
    results = [_ok_result("a"), CollectorResult("b", "rss", [], skipped="quota")]
    text = _render(results=results, status="partial")
    assert "Sources: 1 of 2 ok (b: quota)" in text


def test_many_failed_sources_shows_first_few_and_a_count_of_the_rest():
    results = [_ok_result("good")]
    results += [CollectorResult(f"bad{i}", "rss", [], error=f"error {i}") for i in range(6)]
    text = _render(results=results, status="partial")
    sources_line = next(line for line in text.splitlines() if line.startswith("Sources:"))
    assert sources_line.startswith("Sources: 1 of 7 ok (")
    assert "bad0: error 0" in sources_line
    assert "bad1: error 1" in sources_line
    assert "bad2: error 2" in sources_line
    assert "bad3" not in sources_line
    assert "+3 more" in sources_line


def test_source_error_first_line_only_is_shown_and_truncated():
    long_error = "first line of the error\nsecond line nobody should see"
    results = [CollectorResult("flaky", "rss", [], error=long_error)]
    text = _render(results=results, status="partial")
    sources_line = next(line for line in text.splitlines() if line.startswith("Sources:"))
    assert "second line" not in sources_line
    assert "first line of the error" in sources_line


# --- cost formatting ---


def test_cost_rounds_to_dollars_and_cents():
    text = _render(usage=Usage(input_tokens=8000, output_tokens=1500))
    assert "Claude: ~$0.02" in text


def test_tiny_cost_shows_under_a_cent_marker():
    text = _render(usage=Usage(input_tokens=10, output_tokens=1))
    assert "Claude: <$0.01" in text


def test_zero_usage_shows_under_a_cent_marker():
    text = _render(usage=_EMPTY_USAGE)
    assert "Claude: <$0.01" in text


# --- duration formatting ---


def test_duration_under_a_minute_shows_seconds_only():
    text = _render(duration=timedelta(seconds=45))
    assert "took 45s" in text


def test_duration_over_a_minute_shows_minutes_and_seconds():
    text = _render(duration=timedelta(minutes=1, seconds=52))
    assert "took 1m52s" in text


# --- jump link ---


def test_missing_header_message_id_omits_the_jump_link_entirely():
    text = _render(header_message_id=None)
    assert "jump to digest" not in text
    # the rest of the cost line should still be there
    assert "Claude:" in text and "took" in text


def test_jump_link_uses_guild_channel_and_message_id():
    text = _render(guild_id=111, channel_id=222, header_message_id=333)
    assert "https://discord.com/channels/111/222/333" in text


# --- escaping hostile input ---


def test_hostile_topic_name_is_escaped():
    hostile = Topic(key="x", name="@everyone **x**", aliases=[], entities=[])
    text = _render(topics=[hostile], summaries={}, fallback_items={})
    assert "@everyone" not in text
    assert "**x**" not in text


def test_hostile_source_name_and_error_are_escaped():
    results = [CollectorResult("@everyone <@123>", "rss", [], error="**boom** @everyone")]
    text = _render(results=results, status="partial")
    assert "@everyone" not in text
    assert "**boom**" not in text


def test_hostile_notes_are_escaped():
    text = _render(status="partial", notes=["@everyone **gotcha**"])
    assert "@everyone" not in text
    assert "**gotcha**" not in text


# --- the 2000 UTF-16 unit cap ---


def test_astral_emoji_and_long_names_stay_under_the_cap():
    hostile_topic = Topic(key="x", name="🤖" * 200, aliases=[], entities=[])
    results = [CollectorResult(f"src {'🤖' * 30}", "rss", [], error="💥" * 100) for _ in range(10)]
    text = _render(
        topics=[hostile_topic],
        summaries={},
        fallback_items={},
        results=results,
        status="partial",
    )
    assert discord_len(text) <= 2000


def test_many_failing_sources_and_long_notes_together_still_stay_under_the_cap():
    results = [CollectorResult(f"source-{i}", "rss", [], error="x" * 200) for i in range(50)]
    notes = ["a very long note about a coverage gap " * 30]
    text = _render(results=results, notes=notes, status="partial")
    assert discord_len(text) <= 2000


def test_masked_link_spoof_in_error_string_does_not_become_a_clickable_link():
    # A source's error text is scraped/attacker-adjacent in the general
    # case (some collectors echo remote response bodies into `.error`).
    # `[free loot](https://evil.example/phish)` is the classic markdown
    # masked-link spoof: if this rendered as real markdown, an admin
    # reading the report would see "free loot" as clickable text pointing
    # at a URL they never saw.
    hostile_error = "[free loot](https://evil.example/phish)"
    results = [CollectorResult("flaky", "rss", [], error=hostile_error)]
    text = _render(results=results, status="partial")
    # Never the live, clickable form: neither an intact masked link nor a
    # working autolink underneath it.
    assert "[free loot](https://evil.example/phish)" not in text
    assert "\\[free loot]" in text  # markdown-link syntax defused with a backslash
    assert "https://evil.example/phish" not in text  # scheme itself defused too


def test_bare_url_in_error_string_does_not_autolink():
    # `esc()` defuses any "scheme://" token wherever it appears -- an
    # error string is no exception -- so a URL an attacker slipped into a
    # collector's error text never becomes a live link at all, masked or
    # otherwise.
    hostile_error = "fetch failed: https://evil.example/free-vbucks timed out"
    results = [CollectorResult("flaky", "rss", [], error=hostile_error)]
    text = _render(results=results, status="partial")
    assert "](https://evil.example/free-vbucks)" not in text
    assert "https://evil.example/free-vbucks" not in text
    assert "evil.example/free-vbucks" in text  # still readable, just not live


def test_error_string_with_backticks_and_newlines_stays_on_one_escaped_line():
    hostile_error = "boom `rm -rf /`\nsecond line with a [link](https://evil.example)"
    results = [CollectorResult("flaky", "rss", [], error=hostile_error)]
    text = _render(results=results, status="partial")
    sources_line = next(line for line in text.splitlines() if line.startswith("Sources:"))
    assert "second line" not in sources_line
    assert "`rm -rf /`" not in sources_line  # backtick escaped, no live code span
    assert "\\`" in sources_line


def test_identical_topic_names_are_both_shown_with_their_own_counts():
    a = Topic(key="dupe_a", name="Same Name", aliases=[], entities=[])
    b = Topic(key="dupe_b", name="Same Name", aliases=[], entities=[])
    summaries = {"dupe_a": _summary("dupe_a", 2), "dupe_b": _summary("dupe_b", 5)}
    text = _render(topics=[a, b], summaries=summaries, fallback_items={})
    assert "Same Name 2 · Same Name 5" in text


def test_zero_topics_configured_still_renders_a_sane_zero_stories_line():
    # config.py has no minimum-topics check -- an owner could ship a
    # config with an empty topics list (everything filtered out by other
    # means), so render_run_report must not crash on it.
    text = _render(topics=[], summaries={}, fallback_items={})
    assert "0 stories: " in text


def test_all_sources_failed_shows_zero_of_n_ok():
    results = [CollectorResult(f"src{i}", "rss", [], error="down") for i in range(4)]
    text = _render(results=results, status="partial")
    assert "Sources: 0 of 4 ok" in text


def test_fifty_failed_sources_shows_first_three_and_a_count_of_the_rest():
    results = [CollectorResult(f"bad{i}", "rss", [], error="down") for i in range(50)]
    text = _render(results=results, status="partial")
    sources_line = next(line for line in text.splitlines() if line.startswith("Sources:"))
    assert sources_line.startswith("Sources: 0 of 50 ok (")
    assert "+47 more" in sources_line
    assert discord_len(text) <= 2000


def test_huge_token_counts_render_a_plausible_dollar_figure_without_crashing():
    text = _render(usage=Usage(input_tokens=5_000_000_000, output_tokens=1_000_000_000))
    assert "Claude: ~$" in text
    assert discord_len(text) <= 2000


def test_zero_duration_shows_zero_seconds():
    text = _render(duration=timedelta(seconds=0))
    assert "took 0s" in text


def test_duration_over_an_hour_shows_minutes_past_sixty():
    text = _render(duration=timedelta(hours=1, minutes=17, seconds=3))
    assert "took 77m03s" in text


def test_content_exactly_at_the_2000_unit_boundary_with_astral_chars_is_not_truncated():
    # 🤖 is astral (2 UTF-16 units); build a source name/error combo that
    # lands the whole message exactly on the 2000-unit cap and check
    # nothing gets clipped right at the edge.
    base = _render(results=[_ok_result("src")], status="ok")
    room = 2000 - discord_len(base)
    pad_units = max(room, 0)
    pad = "🤖" * (pad_units // 2)
    hostile_topic = Topic(key="x", name="Pad" + pad, aliases=[], entities=[])
    text = _render(topics=[hostile_topic], summaries={}, fallback_items={}, status="ok")
    assert discord_len(text) <= 2000


def test_content_one_astral_char_over_the_boundary_truncates_without_a_lone_surrogate():
    hostile_topic = Topic(key="x", name="🤖" * 1200, aliases=[], entities=[])
    text = _render(topics=[hostile_topic], summaries={}, fallback_items={}, status="ok")
    assert discord_len(text) <= 2000
    # A truncated lone surrogate encodes as a replacement char with
    # 'surrogatepass'-style errors; round-tripping through UTF-16 without
    # 'surrogatepass' is exactly the check that a half-clipped pair fails.
    text.encode("utf-16-le").decode("utf-16-le")


def test_format_module_uses_the_same_estimate_spend_usd_summarize_owns():
    # a45548d relocated estimate_spend_usd next to its price constants in
    # pipeline/summarize.py; format.py's run-report cost line must call
    # that exact function, not a copy that could quietly drift from what
    # /newsbot status shows.
    import newsbot.bot.format as format_module
    from newsbot.pipeline import summarize

    assert format_module.estimate_spend_usd is summarize.estimate_spend_usd


def test_notes_are_truncated_before_source_detail_is_dropped():
    # With a moderate number of bad sources (still fits) but an enormous
    # notes line, the notes line should shed detail (or vanish) before the
    # per-source failure detail does.
    results = [CollectorResult("only-bad-source", "rss", [], error="short error")]
    notes = ["x" * 3000]
    text = _render(results=results, notes=notes, status="partial")
    assert discord_len(text) <= 2000
    assert "only-bad-source: short error" in text
