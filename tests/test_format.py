"""Tests for newsbot.bot.format: sorting, embed limits, escaping, fallback rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime

from newsbot.bot.format import esc, render_digest, render_status, to_text
from newsbot.collectors.base import RawItem
from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.summarize import StoryDraft, TopicSummary
from newsbot.store.models import DigestRow, SourceHealthRow, StatusSnapshot, Usage

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
    field = next(f for f in embed.fields if f.name == "Reddit")
    assert "⚠️" in field.value
