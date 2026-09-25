"""Tests for newsbot.bot.format: sorting, embed limits, escaping, fallback rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from newsbot.bot.format import discord_len, esc, render_digest, render_status, to_text
from newsbot.collectors.base import RawItem
from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.summarize import StoryDraft, TopicSummary
from newsbot.store.models import AlertStatus, DigestRow, SourceHealthRow, StatusSnapshot, Usage

RUN_DATE = date(2026, 9, 23)
BL4 = Topic(key="borderlands4", name="Borderlands 4", aliases=[], entities=[])
PALWORLD = Topic(key="palworld", name="Palworld", aliases=[], entities=[])
DIABLO4 = Topic(key="diablo4", name="Diablo IV", aliases=[], entities=[])
TOPICS = [BL4, PALWORLD, DIABLO4]

_EMPTY_USAGE = Usage(input_tokens=0, output_tokens=0)


def _draft(headline="Headline", summary="Summary.", label="official", n_urls=1, update_of=None):
    urls = [f"https://example.com/{headline}/{i}" for i in range(n_urls)]
    return StoryDraft(
        headline=headline,
        summary=summary,
        label=label,
        item_urls=urls,
        update_of_story_id=update_of,
    )


def _summary(topic_key, stories=None, *, fallback=False, note=None):
    return TopicSummary(
        topic_key=topic_key, stories=stories or [], fallback=fallback, note=note, usage=_EMPTY_USAGE
    )


def _topic_item(title="Item", url="https://example.com/item", source_name="Some Source"):
    item = RawItem(
        url=url,
        title=title,
        excerpt="",
        source_name=source_name,
        trust="official",
        published_at=None,
    )
    return TopicItem(item=item, topic_key="palworld", uncertain=False)


# --- esc ---


def test_esc_escapes_markdown_and_everyone_mention():
    result = esc("**bold** @everyone")
    assert "**bold**" not in result
    assert "@everyone" not in result or "@​everyone" in result  # zero-width-joined, not live


def test_esc_defuses_a_url_scheme_that_slipped_through_layer_one():
    # postprocess() in summarize.py is layer one and should have already
    # stripped this -- esc() is the belt-and-suspenders second layer for
    # any text that reaches format.py by some other path (or a defusal
    # regex that layer one didn't quite cover). A defused scheme should
    # not survive as a live "scheme://" token.
    result = esc("click https://evil.example/x for a prize")
    assert "https://" not in result
    assert "evil.example" in result  # the text isn't hidden, just not clickable


def test_esc_leaves_a_shift_code_alone():
    result = esc("Shift code: TRICK-4CLIK-3BAIT-URLS9-9WXYZ")
    assert "TRICK-4CLIK-3BAIT-URLS9-9WXYZ" in result


def test_esc_defuses_an_uppercase_scheme():
    # _URL_SCHEME_RE is re.IGNORECASE, but that's exactly the kind of
    # thing worth pinning: "HTTPS://" is just as clickable in most
    # clients as "https://".
    result = esc("go to HTTPS://evil.example/x now")
    assert "HTTPS://" not in result


def test_esc_defuses_a_scheme_inside_a_markdown_masked_link():
    # "[legit-looking text](https://evil.example)" is the classic masked-
    # link phishing shape -- the scheme inside the parens is what needs
    # defusing, same as bare prose.
    result = esc("[Official patch notes](https://evil.example/x)")
    assert "https://" not in result


def test_esc_defuses_a_scheme_inside_an_angle_bracket_autolink():
    # "<https://evil.example>" is Discord's own raw-autolink syntax --
    # _URL_SCHEME_RE doesn't special-case being inside "<...>", so this
    # should come out defused just like any other scheme token.
    result = esc("<https://evil.example/x>")
    assert "https://" not in result


def test_esc_defuses_a_bare_discord_invite_too():
    result = esc("join our discord.gg/abc123 server for the giveaway")
    assert "discord.gg/abc123" not in result


# --- sort order and update placement ---


def test_stories_sort_official_then_reported_then_rumor():
    stories = [
        _draft("Rumor story", label="rumor"),
        _draft("Official story", label="official"),
        _draft("Reported story", label="reported"),
    ]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert description.index("Official story") < description.index("Reported story")
    assert description.index("Reported story") < description.index("Rumor story")


def test_more_items_sort_before_fewer_within_same_label():
    stories = [
        _draft("One link", label="official", n_urls=1),
        _draft("Three links", label="official", n_urls=3),
    ]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert description.index("Three links") < description.index("One link")


def test_update_gets_update_marker_and_sorts_within_its_label():
    stories = [
        _draft("Fresh official", label="official"),
        _draft("Updated official", label="official", update_of=5),
    ]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert "🔁 UPDATE" in description
    assert "🟢 OFFICIAL" in description


# --- embed limits ---


def test_4096_boundary_cuts_least_important_and_adds_more_line():
    long_summary = "x" * 380
    stories = [_draft(f"Story {i}", summary=long_summary, label="official") for i in range(40)]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    embed = rendered.embed_messages[0][0]
    assert len(embed.description) <= 4096
    assert "more, use /news" in embed.description
    assert len(embed) <= 6000


def test_three_large_topics_split_into_more_than_one_message():
    long_summary = "y" * 380
    stories = [_draft(f"Story {i}", summary=long_summary, label="official") for i in range(30)]
    summaries = {t.key: _summary(t.key, stories) for t in TOPICS}
    rendered = render_digest(RUN_DATE, TOPICS, summaries, {}, [])
    assert len(rendered.embed_messages) >= 2
    for message in rendered.embed_messages:
        assert sum(len(e) for e in message) <= 6000
        assert len(message) <= 10


# --- empty topic ---


def test_empty_topic_shows_no_new_stories():
    rendered = render_digest(RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", [])}, {}, [])
    assert rendered.embed_messages[0][0].description == "No new stories today."


def test_topic_with_no_summary_at_all_also_shows_no_new_stories():
    rendered = render_digest(RUN_DATE, [PALWORLD], {}, {}, [])
    assert rendered.embed_messages[0][0].description == "No new stories today."


# --- fallback ---


def test_fallback_topic_renders_items_as_headline_and_link_list():
    items = [_topic_item(title="Patch notes posted", url="https://example.com/patch")]
    summaries = {
        "palworld": _summary(
            "palworld", fallback=True, note="Summary unavailable; showing headlines."
        )
    }
    rendered = render_digest(RUN_DATE, [PALWORLD], summaries, {"palworld": items}, [])
    description = rendered.embed_messages[0][0].description
    assert "Patch notes posted" in description
    assert "<https://example.com/patch>" in description
    assert "Summary unavailable" in description


def test_story_url_with_breakout_characters_never_produces_a_masked_link():
    # A URL shaped to break out of the <...> autolink and grow a fake
    # [text](url) markdown link right after it (see normalize.py's
    # canonicalize() docstring). Even if a URL like this somehow reached
    # render_digest without going through postprocess's canonicalize
    # check first, _safe_link()'s re-canonicalize at render time has to
    # catch it.
    malicious = "https://ok.example/a> **boom** [Official patch notes](https://evil.example/x)"
    stories = [
        StoryDraft(
            headline="Headline",
            summary="Summary.",
            label="official",
            item_urls=[malicious],
            update_of_story_id=None,
        )
    ]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert "](https://evil.example" not in description
    assert "> **boom**" not in description


def test_fallback_item_url_with_breakout_characters_never_produces_a_masked_link():
    malicious = "https://ok.example/a> **boom** [Official patch notes](https://evil.example/x)"
    item = _topic_item(title="Patch notes posted", url=malicious)
    summaries = {
        "palworld": _summary(
            "palworld", fallback=True, note="Summary unavailable; showing headlines."
        )
    }
    rendered = render_digest(RUN_DATE, [PALWORLD], summaries, {"palworld": [item]}, [])
    description = rendered.embed_messages[0][0].description
    assert "](https://evil.example" not in description
    assert "> **boom**" not in description


def test_fallback_topic_with_no_items_shows_no_new_stories():
    summaries = {
        "palworld": _summary(
            "palworld", fallback=True, note="Summary unavailable; showing headlines."
        )
    }
    rendered = render_digest(RUN_DATE, [PALWORLD], summaries, {"palworld": []}, [])
    assert rendered.embed_messages[0][0].description == "No new stories today."


# --- escaping in real output ---


def test_headline_with_everyone_and_bold_is_escaped_in_embed():
    stories = [
        StoryDraft(
            headline="**Big** @everyone announcement",
            summary="Summary.",
            label="official",
            item_urls=["https://example.com/x"],
            update_of_story_id=None,
        )
    ]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert "**Big**" not in description
    assert "@everyone" not in description


# --- coverage notes ---


def test_coverage_note_appears_in_header():
    rendered = render_digest(
        RUN_DATE,
        [PALWORLD],
        {"palworld": _summary("palworld", [])},
        {},
        ["Brave search skipped: quota exceeded"],
    )
    assert "Brave search skipped: quota exceeded" in rendered.header


def test_header_has_per_topic_story_counts():
    stories = [_draft("A"), _draft("B")]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    assert "Palworld: 2" in rendered.header


# --- to_text ---


def test_to_text_includes_header_and_topic_titles():
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", [_draft("A")])}, {}, []
    )
    text = to_text(rendered)
    assert "Palworld" in text
    assert "A" in text


# --- render_status ---


def test_render_status_flags_source_with_3_consecutive_failures():
    snap = StatusSnapshot(
        last_digest=DigestRow(
            id=1, run_date=RUN_DATE, status="ok", posted_message_ids=[1], error_notes=None
        ),
        source_health=[
            SourceHealthRow(
                source_name="Reddit",
                last_success_at=None,
                last_error_at=datetime(2026, 9, 23, tzinfo=UTC),
                last_error="403",
                consecutive_failures=3,
            )
        ],
        items_last_24h=10,
        stories_last_24h=3,
        month_input_tokens=1000,
        month_output_tokens=500,
    )
    embed = render_status(snap, spend_usd=1.23)
    assert "⚠️" in embed.description
    assert "Reddit" in embed.description


def _healthy_field(embed):
    return next(f for f in embed.fields if f.name == "Sources healthy")


def test_render_status_counts_healthy_of_total():
    snap = StatusSnapshot(
        last_digest=None,
        source_health=[
            SourceHealthRow(
                source_name="Blizzard News",
                last_success_at=datetime(2026, 9, 23, tzinfo=UTC),
                last_error_at=None,
                last_error=None,
                consecutive_failures=0,
            ),
            SourceHealthRow(
                source_name="Reddit",
                last_success_at=None,
                last_error_at=datetime(2026, 9, 23, tzinfo=UTC),
                last_error="403",
                consecutive_failures=1,
            ),
        ],
        items_last_24h=0,
        stories_last_24h=0,
        month_input_tokens=0,
        month_output_tokens=0,
    )
    embed = render_status(snap, spend_usd=0.0)
    assert _healthy_field(embed).value == "1 of 2"


def test_render_status_notes_never_run_source_without_counting_it_healthy():
    snap = StatusSnapshot(
        last_digest=None,
        source_health=[
            SourceHealthRow(
                source_name="Blizzard News",
                last_success_at=datetime(2026, 9, 23, tzinfo=UTC),
                last_error_at=None,
                last_error=None,
                consecutive_failures=0,
            ),
            SourceHealthRow(
                source_name="Palworld Steam",
                last_success_at=None,
                last_error_at=None,
                last_error=None,
                consecutive_failures=0,
                never_run=True,
            ),
        ],
        items_last_24h=0,
        stories_last_24h=0,
        month_input_tokens=0,
        month_output_tokens=0,
    )
    embed = render_status(snap, spend_usd=0.0)
    value = _healthy_field(embed).value
    assert value == "1 of 2 (1 not run yet)"
    # A never-run source isn't a failure either -- it shouldn't show up in
    # the "recent failures" list.
    assert "Palworld Steam" not in (embed.description or "")


def test_render_status_stays_under_discords_25_field_limit_with_many_sources():
    # The live test guild found this one: 24 sources used to mean 28 fields.
    health = [
        SourceHealthRow(
            source_name=f"Source {i}",
            last_success_at=None,
            last_error_at=None,
            last_error="timed out after 20.0s" if i % 5 == 0 else None,
            consecutive_failures=1 if i % 5 == 0 else 0,
        )
        for i in range(60)
    ]
    snap = StatusSnapshot(
        last_digest=None,
        source_health=health,
        items_last_24h=0,
        stories_last_24h=0,
        month_input_tokens=0,
        month_output_tokens=0,
    )
    embed = render_status(snap, spend_usd=0.0)
    assert len(embed.fields) <= 25
    assert len(embed.description) <= 4096
    assert len(embed) <= 6000


# --- render_status: SHiFT alerts field (plan step 10) ---

_EMPTY_SNAP = StatusSnapshot(
    last_digest=None,
    source_health=[],
    items_last_24h=0,
    stories_last_24h=0,
    month_input_tokens=0,
    month_output_tokens=0,
)


def _alerts_field(embed):
    return next(f for f in embed.fields if f.name == "SHiFT alerts")


def test_render_status_omits_alerts_field_when_not_given():
    # Every existing caller (and every test above this one) doesn't pass
    # `alerts` at all -- the field must not appear, not appear as "disabled".
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0)
    assert not any(f.name == "SHiFT alerts" for f in embed.fields)


def test_render_status_alerts_field_says_disabled_when_not_enabled():
    alerts = AlertStatus(
        enabled=False,
        seeded=False,
        last_sweep_at=None,
        last_sweep_summary=None,
        codes_alerted=0,
        pings_today=0,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    assert _alerts_field(embed).value == "disabled"


def test_render_status_alerts_field_notes_seeding_before_first_healthy_sweep():
    alerts = AlertStatus(
        enabled=True,
        seeded=False,
        last_sweep_at=datetime(2026, 9, 25, 20, 0, tzinfo=UTC),
        last_sweep_summary="17/19 sources ok, 0 new codes",
        codes_alerted=0,
        pings_today=0,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    value = _alerts_field(embed).value
    assert "(seeding)" in value
    assert "17/19 sources ok" in value


def test_render_status_alerts_field_shows_sweep_summary_and_ping_spend_once_seeded():
    alerts = AlertStatus(
        enabled=True,
        seeded=True,
        last_sweep_at=datetime(2026, 9, 25, 21, 0, tzinfo=UTC),
        last_sweep_summary="19/19 sources ok, 1 new code",
        codes_alerted=3,
        pings_today=1,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    value = _alerts_field(embed).value
    assert "(seeding)" not in value
    assert "3 codes alerted" in value
    assert "pings today 1 of 3" in value
    assert "19/19 sources ok, 1 new code" in value


def test_render_status_alerts_field_no_sweep_yet():
    alerts = AlertStatus(
        enabled=True,
        seeded=False,
        last_sweep_at=None,
        last_sweep_summary=None,
        codes_alerted=0,
        pings_today=0,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    value = _alerts_field(embed).value
    assert value.startswith("no sweep yet")
    assert "(seeding)" in value


def test_render_status_alerts_field_escapes_hostile_sweep_summary():
    alerts = AlertStatus(
        enabled=True,
        seeded=True,
        last_sweep_at=datetime(2026, 9, 25, 21, 0, tzinfo=UTC),
        last_sweep_summary="@everyone **pwned**",
        codes_alerted=0,
        pings_today=0,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    value = _alerts_field(embed).value
    assert "@everyone" not in value
    assert "**pwned**" not in value


def test_render_status_alerts_field_hostile_summary_stays_under_utf16_length():
    # Same case as the escape test above, checked for length instead of
    # content: escape_markdown/escape_mentions can only ever grow a
    # string (backslashes and zero-width spaces get inserted, nothing is
    # removed), so an already-long hostile summary could still overflow
    # some limit even once it's safely inert.
    alerts = AlertStatus(
        enabled=True,
        seeded=True,
        last_sweep_at=datetime(2026, 9, 25, 21, 0, tzinfo=UTC),
        last_sweep_summary="@everyone " * 50 + "**pwned**" * 50,
        codes_alerted=0,
        pings_today=0,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    value = _alerts_field(embed).value
    assert discord_len(value) < 6000  # at minimum, under Discord's whole-message cap


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (newsbot/bot/format.py _alerts_field_value / render_status): "
        "the 'SHiFT alerts' field value is never run through "
        "_truncate_utf16(..., _MAX_FIELD_VALUE) the way the 'Last digest' "
        "field a few lines above it is. In production last_sweep_summary "
        "only ever comes from shift/sweep.py's own bounded "
        "f'{ok}/{total} sources ok, {n} new codes' string, so this isn't "
        "reachable today -- but render_status has no way to know that, and "
        "a single overlong or heavily-escaped last_sweep_summary (a future "
        "caller, a corrupted alert_state row, a schema change) would "
        "produce a field value over Discord's 1024-unit field-value limit "
        "and make the whole /newsbot status embed fail to send. Severity: "
        "low today (no current caller can trigger it), but it's a "
        "real gap in a module whose entire job is 'never send an embed "
        "Discord will reject.'"
    ),
)
def test_render_status_alerts_field_value_is_truncated_to_the_field_limit():
    alerts = AlertStatus(
        enabled=True,
        seeded=True,
        last_sweep_at=datetime(2026, 9, 25, 21, 0, tzinfo=UTC),
        last_sweep_summary="x" * 2000,
        codes_alerted=0,
        pings_today=0,
        max_pings=3,
    )
    embed = render_status(_EMPTY_SNAP, spend_usd=0.0, alerts=alerts)
    value = _alerts_field(embed).value
    assert discord_len(value) <= 1024
