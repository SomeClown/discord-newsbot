"""Edge cases for newsbot.bot.format, beyond the implementer's tests.

test_format.py already covers the happy paths: sort order, the 4096-char
trim-and-"+N more" boundary, empty topics (no message at all), and a basic
@everyone + bold escape. This file goes after the
sharper corners: the 256-char title limit, the coverage-note footer cap,
markdown spoofing beyond a bare `**bold**` (masked links, backticks,
spoilers), mention types `esc()` does and doesn't catch, and very long
single headlines/URLs and Unicode/emoji content.
"""

from __future__ import annotations

from datetime import date

from newsbot.bot.format import discord_len, esc, render_guild_digest
from newsbot.config import Topic
from newsbot.pipeline.summarize import StoryDraft

RUN_DATE = date(2026, 9, 23)
PALWORLD = Topic(key="palworld", name="Palworld", channel_id=1, aliases=[], entities=[])


def _draft(headline="Headline", summary="Summary.", label="official", n_urls=1, update_of=None):
    urls = [f"https://example.com/{headline}/{i}" for i in range(n_urls)]
    return StoryDraft(
        headline=headline,
        summary=summary,
        label=label,
        item_urls=urls,
        update_of_story_id=update_of,
    )


def _digest(topics, stories_by_game, coverage_notes=()):
    """`render_guild_digest` with a stored summary (a list of stories) per game key."""
    return render_guild_digest(
        RUN_DATE,
        topics,
        {t.key: t.channel_id for t in topics},
        stories_by_game=stories_by_game,
        items_by_game={},
        notes_by_game={},
        coverage_notes=list(coverage_notes),
    )


def _first_embed(rendered):
    return rendered.messages[0].embed


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
    # pointing somewhere other than what it says: the classic phishing
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


def test_esc_neutralizes_channel_mentions():
    result = esc("check <#123456789012345678> for details")
    assert "<#123456789012345678>" not in result


# --- 256-char title limit ---


def test_topic_title_is_truncated_at_256_chars():
    long_name = "Diablo IV: " + "A Very Long Subtitle " * 20
    topic = Topic(key="diablo4", name=long_name, channel_id=1, aliases=[], entities=[])
    rendered = _digest([topic], {"diablo4": [_draft("A")]}, [])
    embed = _first_embed(rendered)
    assert len(embed.title) <= 256


# --- coverage-note footer cap (D1) ---


def test_coverage_footer_is_truncated_well_under_the_embed_footer_limit():
    long_notes = [f"a very long coverage note number {i} about a skipped source" for i in range(50)]
    rendered = _digest([PALWORLD], {"palworld": [_draft("A")]}, long_notes)
    footer_text = _first_embed(rendered).footer.text
    assert discord_len(footer_text) <= 512


def test_coverage_footer_defuses_mentions_but_does_not_markdown_escape():
    # Embed footers never render markdown, so escaping it would just leave
    # literal backslashes sitting in the text (QA follow-up); mentions
    # still get defused (the one thing a footer *can* do something with),
    # but "**gotcha**" stays as plain, unescaped, still-inert text.
    rendered = _digest([PALWORLD], {"palworld": [_draft("A")]}, ["@everyone **gotcha**"])
    footer_text = _first_embed(rendered).footer.text
    assert "@everyone" not in footer_text
    assert "\\" not in footer_text
    assert "**gotcha**" in footer_text


# --- very long single headline / URL ---


def test_a_single_pathologically_long_headline_is_hard_truncated_not_dropped():
    stories = [_draft(headline="H" * 5000, summary="short")]
    rendered = _digest([PALWORLD], {"palworld": stories}, [])
    description = _first_embed(rendered).description
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
    rendered = _digest([PALWORLD], {"palworld": [draft]}, [])
    description = _first_embed(rendered).description
    assert len(description) <= 4096


# --- unicode / emoji ---


def test_multi_codepoint_emoji_headline_does_not_crash_and_respects_the_limit():
    # Family emoji is a ZWJ sequence of several codepoints per glyph, each
    # one an astral character: two UTF-16 units apiece, per discord_len().
    # QA step 20 group 5: the limit here is enforced in UTF-16 units, not
    # Python's codepoint-counting len(), so this asserts against
    # discord_len() (the real Discord-facing measure), not just len().
    emoji_headline = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466 " * 200
    stories = [_draft(headline=emoji_headline[:190], summary="Family news.")]
    rendered = _digest([PALWORLD], {"palworld": stories}, [])
    description = _first_embed(rendered).description
    assert discord_len(description) <= 4096
    assert "\U0001f468" in description


# --- UTF-16 accounting (QA step 20, group 5) ---


def test_discord_len_counts_astral_emoji_as_two_units():
    assert discord_len("🤖") == 2
    assert discord_len("ab") == 2
    assert len("🤖") == 1  # the gap discord_len exists to close


def test_description_truncation_never_splits_a_surrogate_pair_near_the_boundary():
    # 2100 astral emoji is 4200 UTF-16 units: comfortably past the 4096
    # description limit, and landing mid-emoji if truncation were done in
    # raw UTF-16 units instead of by codepoint.
    stories = [_draft(headline="H", summary="🤖" * 2100)]
    rendered = _digest([PALWORLD], {"palworld": stories}, [])
    description = _first_embed(rendered).description
    assert discord_len(description) <= 4096
    # A split surrogate pair can't exist in a Python str at all (the type
    # doesn't allow an unpaired surrogate from ordinary text operations),
    # so the real assertion is indirect: every character that made it in
    # is a complete codepoint, which round-tripping through utf-16-le and
    # back (strict, no surrogatepass) already proves.
    description.encode("utf-16-le").decode("utf-16-le")


def test_topic_title_truncation_respects_utf16_units_not_codepoints():
    # 200 astral emoji is 400 UTF-16 units, well past the 256-unit title
    # limit, but only 200 Python characters: a codepoint-counting
    # len()-based [:256] slice would let all 200 through untouched.
    long_name = "🤖" * 200
    topic = Topic(key="diablo4", name=long_name, channel_id=1, aliases=[], entities=[])
    rendered = _digest([topic], {"diablo4": [_draft("A")]}, [])
    embed = _first_embed(rendered)
    assert discord_len(embed.title) <= 256


def test_unicode_headline_with_combining_marks_and_rtl_text_round_trips():
    headline = "Ω مرحبا Zürich café é test"
    stories = [_draft(headline=headline, summary="Summary.")]
    rendered = _digest([PALWORLD], {"palworld": stories}, [])
    description = _first_embed(rendered).description
    assert headline in description
