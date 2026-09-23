"""Edge cases for newsbot.bot.format, beyond the implementer's tests.

test_format.py already covers the happy paths: sort order, the 4096-char
trim-and-"+N more" boundary, empty/fallback topics, and a basic @everyone +
bold escape. This file goes after the sharper corners: the exact 10-embed
and message-total boundaries, the 2000-char header limit, the 256-char
title limit, markdown spoofing beyond a bare `**bold**` (masked links,
backticks, spoilers), mention types `esc()` does and doesn't catch, and
very long single headlines/URLs and Unicode/emoji content.
"""

from __future__ import annotations

from datetime import date

import pytest

from newsbot.bot.format import esc, render_digest
from newsbot.config import Topic
from newsbot.pipeline.summarize import StoryDraft, TopicSummary
from newsbot.store.models import Usage

RUN_DATE = date(2026, 9, 23)
PALWORLD = Topic(key="palworld", name="Palworld", aliases=[], entities=[])
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


# --- esc(): mentions ---


def test_esc_neutralizes_here_mention():
    assert "@here" not in esc("@here everyone check this out")


def test_esc_neutralizes_user_mention():
    assert "<@123456789012345678>" not in esc("hey <@123456789012345678>")


def test_esc_neutralizes_nickname_mention():
    assert "<@!123456789012345678>" not in esc("hey <@!123456789012345678>")


def test_esc_neutralizes_role_mention():
    assert "<@&123456789012345678>" not in esc("attention <@&123456789012345678>")


# --- esc(): markdown spoofing ---


def test_esc_neutralizes_masked_link_spoofing():
    # A masked link is how a scraped title could render as clickable text
    # pointing somewhere other than what it says -- the classic phishing
    # trick, just wearing a headline's clothes.
    spoofed = "[Free V-Bucks, click here](https://totally-legit.example.com)"
    result = esc(spoofed)
    assert result != spoofed
    assert result.startswith("\\[")


def test_esc_neutralizes_backticks_and_code_fences():
    result = esc("```js\nalert(1)\n``` and `inline`")
    assert "```" not in result
    assert "`inline`" not in result


def test_esc_neutralizes_spoiler_tags():
    result = esc("||surprise ending||")
    assert "||surprise ending||" not in result


def test_esc_neutralizes_heading_and_blockquote_markers():
    result = esc("# Breaking News\n> some quote")
    assert not result.startswith("# ")
    assert "\\>" in result


# --- known gap: channel mentions are not escaped ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "esc() (newsbot/bot/format.py) relies solely on "
        "discord.utils.escape_mentions, which its own docstring says "
        "explicitly does not cover channel mentions. A scraped title "
        "containing a literal <#channel_id> survives esc() untouched and "
        "renders as a live, clickable channel link in the posted embed."
    ),
)
def test_esc_neutralizes_channel_mentions():
    result = esc("check <#123456789012345678> for details")
    assert "<#123456789012345678>" not in result


# --- 10-embed / message-total boundaries ---


def test_exactly_ten_small_embeds_fit_in_one_message():
    stories = {f"t{i}": _summary(f"t{i}", [_draft(f"S{i}")]) for i in range(10)}
    topics = [Topic(key=f"t{i}", name=f"Topic {i}", aliases=[], entities=[]) for i in range(10)]
    rendered = render_digest(RUN_DATE, topics, stories, {}, [])
    assert len(rendered.embed_messages) == 1
    assert len(rendered.embed_messages[0]) == 10


def test_eleventh_small_embed_spills_into_a_second_message():
    stories = {f"t{i}": _summary(f"t{i}", [_draft(f"S{i}")]) for i in range(11)}
    topics = [Topic(key=f"t{i}", name=f"Topic {i}", aliases=[], entities=[]) for i in range(11)]
    rendered = render_digest(RUN_DATE, topics, stories, {}, [])
    assert len(rendered.embed_messages) == 2
    assert len(rendered.embed_messages[0]) == 10
    assert len(rendered.embed_messages[1]) == 1


def test_every_message_respects_the_6000_char_total_even_at_the_boundary():
    # Big topics near the per-embed cap, several of them -- forces the
    # packer to split on total-length, not just the embed-count cap.
    long_summary = "z" * 380
    stories = [_draft(f"Story {i}", summary=long_summary) for i in range(11)]
    topics = [Topic(key=f"t{i}", name=f"Topic {i}", aliases=[], entities=[]) for i in range(4)]
    summaries = {t.key: _summary(t.key, stories) for t in topics}
    rendered = render_digest(RUN_DATE, topics, summaries, {}, [])
    for message in rendered.embed_messages:
        assert sum(len(e) for e in message) <= 6000
        assert len(message) <= 10


# --- 2000-char header limit ---


def test_header_is_truncated_at_2000_chars():
    topics = [
        Topic(key=f"t{i}", name=f"Topic {i} " + "x" * 40, aliases=[], entities=[])
        for i in range(30)
    ]
    summaries = {t.key: _summary(t.key, [_draft("A")]) for t in topics}
    notes = [f"coverage note number {i} about a source that got skipped" for i in range(30)]
    rendered = render_digest(RUN_DATE, topics, summaries, {}, notes)
    assert len(rendered.header) <= 2000


# --- 256-char title limit ---


def test_topic_title_is_truncated_at_256_chars():
    long_name = "Diablo IV: " + "A Very Long Subtitle " * 20
    topic = Topic(key="diablo4", name=long_name, aliases=[], entities=[])
    rendered = render_digest(
        RUN_DATE, [topic], {"diablo4": _summary("diablo4", [_draft("A")])}, {}, []
    )
    embed = rendered.embed_messages[0][0]
    assert len(embed.title) <= 256


# --- very long single headline / URL ---


def test_a_single_pathologically_long_headline_is_hard_truncated_not_dropped():
    stories = [_draft(headline="H" * 5000, summary="short")]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert len(description) <= 4096
    assert description.endswith("…")


def test_a_very_long_single_url_does_not_blow_the_description_limit():
    draft = StoryDraft(
        headline="Story with a monstrous URL",
        summary="Summary.",
        label="official",
        item_urls=["https://example.com/" + "a" * 3000],
        update_of_story_id=None,
    )
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", [draft])}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert len(description) <= 4096


# --- unicode / emoji ---


def test_multi_codepoint_emoji_headline_does_not_crash_and_respects_the_limit():
    # Family emoji is a ZWJ sequence of several codepoints per glyph;
    # Python len() counts codepoints, not rendered glyphs or UTF-16 code
    # units (which is what Discord's own limit is reportedly measured in).
    # This test pins our len()-based accounting; see the report for the
    # note on why that could, in principle, undercount against Discord's
    # real enforcement.
    emoji_headline = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466 " * 200
    stories = [_draft(headline=emoji_headline[:190], summary="Family news.")]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert len(description) <= 4096
    assert "\U0001f468" in description


def test_unicode_headline_with_combining_marks_and_rtl_text_round_trips():
    headline = "Ω مرحبا Zürich café é test"
    stories = [_draft(headline=headline, summary="Summary.")]
    rendered = render_digest(
        RUN_DATE, [PALWORLD], {"palworld": _summary("palworld", stories)}, {}, []
    )
    description = rendered.embed_messages[0][0].description
    assert headline in description
