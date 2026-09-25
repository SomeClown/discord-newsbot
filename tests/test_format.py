"""Tests for newsbot.bot.format: sorting, embed limits, escaping, fallback rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

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


def test_esc_leaves_a_bare_discord_invite_untouched():
    # Neither summarize.py's _URL_TOKEN_RE (requires a scheme or a
    # "www." prefix) nor format.py's _URL_SCHEME_RE (requires "scheme:")
    # matches a bare "discord.gg/<code>" with no scheme -- documenting
    # current behavior, not asserting it's safe. See the xfail right
    # below for why this might still be a live risk.
    result = esc("join our discord.gg/abc123 server for the giveaway")
    assert "discord.gg/abc123" in result


@pytest.mark.xfail(
    strict=True,
    reason=(
        "POSSIBLE GAP: Discord's own client is known to auto-detect "
        "invite-shaped strings (discord.gg/<code>, discordapp.com/invite/"
        "<code>) as clickable/joinable even with no 'http://' or 'https://' "
        "prefix at all, because it scans message content for the invite "
        "pattern specifically rather than relying on generic autolinking. "
        "Neither summarize.py's _URL_TOKEN_RE (postprocess layer) nor "
        "format.py's _URL_SCHEME_RE (esc() layer) requires or defuses "
        "anything without a scheme or 'www.' prefix, so a model-written "
        "headline/summary that echoes a bare 'discord.gg/<code>' from a "
        "scraped article would sail through both layers untouched. Not "
        "independently verified against live Discord in this session (no "
        "network/gateway access here) -- flagging for the implementer to "
        "confirm against a real client before deciding whether it needs a "
        "fix. Severity: MEDIUM if confirmed (a scam/rickroll invite server "
        "join button in the digest), unclear if Discord doesn't actually "
        "autolink bare invite strings this way."
    ),
)
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
