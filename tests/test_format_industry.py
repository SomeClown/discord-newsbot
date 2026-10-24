"""Rendering the industry post: ranked, capped, and never a "+N more" (plan task 6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from newsbot.bot.format import (
    discord_len,
    render_capped_headlines_embed,
    render_guild_digest,
    render_picked_headlines_embed,
)
from newsbot.config import GameCfg
from newsbot.industry import INDUSTRY_KEY, INDUSTRY_MAX_STORIES
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store.models import HeadlineItem

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
RUN_DATE = date(2026, 10, 23)
IND = GameCfg(key=INDUSTRY_KEY, name="Gaming industry")
BL4 = GameCfg(key="borderlands4", name="Borderlands 4")
PAL = GameCfg(key="palworld", name="Palworld")
CHANNELS = {INDUSTRY_KEY: 11, "borderlands4": 22, "palworld": 33}


def story(n, label="reported", *, update=None):
    return StoryDraft(
        headline=f"Headline sentinel {n}",
        summary=f"Summary sentinel {n}",
        label=label,
        item_urls=[f"https://example.com/story-{n}"],
        update_of_story_id=update,
    )


def item(title, trust="press", *, age_h=1.0, uncertain=False, url=None):
    moment = T0 - timedelta(hours=age_h)
    return HeadlineItem(
        url=url or f"https://example.com/{title.replace(' ', '-').lower()}",
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=moment,
        collected_at=moment,
        uncertain=uncertain,
    )


def digest(games=(IND,), **kwargs):
    base = dict(stories_by_game={}, items_by_game={}, notes_by_game={}, coverage_notes=[])
    base.update(kwargs)
    return render_guild_digest(RUN_DATE, list(games), CHANNELS, **base)


def only_embed(rendered):
    assert len(rendered.messages) == 1
    return rendered.messages[0].embed


def test_eight_mixed_stories_render_as_the_first_five_in_the_given_order():
    labels = ["rumor", "official", "reported", "official", "rumor", "reported", "official", "rumor"]
    stories = [story(i, label) for i, label in enumerate(labels)]
    embed = only_embed(
        digest(
            stories_by_game={INDUSTRY_KEY: stories},
            capped={INDUSTRY_KEY: INDUSTRY_MAX_STORIES},
        )
    )
    text = str(embed.description)
    positions = [text.index(f"Headline sentinel {i}") for i in range(5)]
    assert positions == sorted(positions)
    for i in range(5, 8):
        assert f"Headline sentinel {i}" not in text
    assert text.startswith("🔴 RUMOR · Headline sentinel 0")
    assert "more" not in text
    assert embed.title == "Gaming industry"


def test_an_update_story_keeps_its_update_marker():
    stories = [story(0, "reported", update=7), story(1, "official")]
    embed = only_embed(digest(stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: 5}))
    text = str(embed.description)
    assert text.startswith("🔁 UPDATE · Headline sentinel 0")
    assert "🟢 OFFICIAL · Headline sentinel 1" in text


def test_an_empty_ranked_list_posts_nothing():
    assert digest(stories_by_game={INDUSTRY_KEY: []}, capped={INDUSTRY_KEY: 5}).messages == []


def test_oversize_ranked_stories_shed_from_the_bottom_with_no_more_line():
    big = [
        StoryDraft(
            headline=f"Big {i}",
            summary="x" * 400,
            label="reported",
            item_urls=[f"https://example.com/big-{i}-" + "p" * 500],
            update_of_story_id=None,
        )
        for i in range(5)
    ]
    embed = only_embed(digest(stories_by_game={INDUSTRY_KEY: big}, capped={INDUSTRY_KEY: 5}))
    text = str(embed.description)
    assert discord_len(text) <= 4096
    assert "Big 0" in text and "Big 4" not in text
    assert "more" not in text.replace("x", "")


def test_picks_show_source_titles_and_urls_only():
    picks = [item("Studio closes", "official"), item("Layoffs hit publisher", "community")]
    embed = only_embed(
        digest(
            stories_by_game={},
            items_by_game={INDUSTRY_KEY: [item("ignored")]},
            picks_by_game={INDUSTRY_KEY: picks},
            capped={INDUSTRY_KEY: 5},
        )
    )
    text = str(embed.description)
    assert text.split("\n")[0].startswith("• 🟢 OFFICIAL · Studio closes")
    assert "<https://example.com/layoffs-hit-publisher>" in text
    assert "ignored" not in text
    assert "sentinel" not in text
    upper = text.upper()
    assert "RUMOR" not in upper and "REPORTED" not in upper and "UPDATE" not in upper


def test_picks_keep_the_order_given_not_the_trust_sort():
    picks = [item("community first", "community"), item("official second", "official")]
    embed = render_picked_headlines_embed(IND, picks)
    lines = str(embed.description).split("\n")
    assert "community first" in lines[0] and "official second" in lines[1]


def test_picks_are_cut_to_the_cap():
    picks = [item(f"pick {i}") for i in range(8)]
    embed = only_embed(
        digest(picks_by_game={INDUSTRY_KEY: picks}, capped={INDUSTRY_KEY: INDUSTRY_MAX_STORIES})
    )
    assert str(embed.description).count("\n") + 1 == INDUSTRY_MAX_STORIES
    assert "pick 5" not in str(embed.description)


def test_an_empty_pick_list_posts_nothing_even_with_items_by_game():
    rendered = digest(
        items_by_game={INDUSTRY_KEY: [item("would be shown")]},
        picks_by_game={INDUSTRY_KEY: []},
        capped={INDUSTRY_KEY: 5},
    )
    assert rendered.messages == []


def test_a_capped_fallback_of_twelve_shows_the_newest_five_by_the_headline_sort():
    items = [item(f"press {i}", "press", age_h=i + 1) for i in range(12)]
    embed = render_capped_headlines_embed(IND, items, None, 5)
    lines = str(embed.description).split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        assert f"press {i}" in line and f"press {i}0" not in line
    assert "more" not in str(embed.description)


def test_a_capped_fallback_puts_the_note_on_top():
    items = [item(f"press {i}", age_h=i + 1) for i in range(12)]
    embed = only_embed(
        digest(
            items_by_game={INDUSTRY_KEY: items},
            notes_by_game={INDUSTRY_KEY: "Summary unavailable today."},
            capped={INDUSTRY_KEY: 5},
        )
    )
    lines = str(embed.description).split("\n")
    assert lines[0] == "Summary unavailable today."
    assert len(lines) == 6
    assert "more" not in str(embed.description)


def test_oversize_headline_lines_shed_from_the_bottom_with_no_more_line():
    items = [
        item(f"{i:02d} " + "t" * 290, age_h=i + 1, url=f"https://example.com/{i:02d}/" + "p" * 200)
        for i in range(20)
    ]
    embed = render_capped_headlines_embed(IND, items, None, 20)
    text = str(embed.description)
    lines = text.split("\n")
    assert discord_len(text) <= 4096
    assert 0 < len(lines) < 20
    assert [ln.split(" ")[1] for ln in lines] == [f"{i:02d}" for i in range(len(lines))]
    assert "more" not in text


def test_nothing_to_show_returns_none():
    assert render_capped_headlines_embed(IND, [], "a note", 5) is None
    assert render_picked_headlines_embed(IND, []) is None


def test_other_games_render_identically_with_or_without_the_new_arguments():
    stories = {"borderlands4": [story(1, "official"), story(2, "rumor")]}
    items = {"palworld": [item("pal patch", "official"), item("pal chatter", "community")]}
    notes = {"palworld": "Summary unavailable today."}
    plain = digest(
        games=(BL4, PAL),
        stories_by_game=stories,
        items_by_game=items,
        notes_by_game=notes,
    )
    with_args = digest(
        games=(BL4, PAL),
        stories_by_game=stories,
        items_by_game=items,
        notes_by_game=notes,
        picks_by_game={INDUSTRY_KEY: [item("industry pick")]},
        capped={INDUSTRY_KEY: 5},
    )
    with_industry = digest(
        games=(BL4, PAL, IND),
        stories_by_game={**stories, INDUSTRY_KEY: [story(9)]},
        items_by_game=items,
        notes_by_game=notes,
        picks_by_game={INDUSTRY_KEY: [item("industry pick")]},
        capped={INDUSTRY_KEY: 5},
    )
    expected = [m.embed.to_dict() for m in plain.messages]
    assert len(expected) == 2
    assert [m.embed.to_dict() for m in with_args.messages] == expected
    others = [m.embed.to_dict() for m in with_industry.messages if m.topic_key != INDUSTRY_KEY]
    assert others == expected
    assert [m.topic_key for m in with_industry.messages][-1] == INDUSTRY_KEY


def test_picks_with_an_unusable_first_url_still_fill_the_cap():
    bad = item("bad", url="https://example.com/" + "p" * 1100)
    picks = [bad, *(item(f"pick {i}") for i in range(6))]
    embed = only_embed(digest(picks_by_game={INDUSTRY_KEY: picks}, capped={INDUSTRY_KEY: 5}))
    lines = str(embed.description).split("\n")
    assert [ln.split(" — ")[0] for ln in lines] == [f"• pick {i}" for i in range(5)]


def test_a_cap_below_one_posts_nothing():
    for cap in (0, -1):
        capped = {INDUSTRY_KEY: cap}
        assert (
            digest(picks_by_game={INDUSTRY_KEY: [item("a"), item("b")]}, capped=capped).messages
            == []
        )
        assert (
            digest(stories_by_game={INDUSTRY_KEY: [story(1), story(2)]}, capped=capped).messages
            == []
        )
        assert (
            digest(items_by_game={INDUSTRY_KEY: [item("a"), item("b")]}, capped=capped).messages
            == []
        )


def test_an_oversize_story_loses_links_not_text_and_later_stories_still_render():
    long_urls = [f"https://example.com/{i}/" + "p" * 1500 for i in range(3)]
    fat = StoryDraft(
        headline="Fat headline",
        summary="Fat summary",
        label="reported",
        item_urls=long_urls,
        update_of_story_id=None,
    )
    embed = only_embed(
        digest(stories_by_game={INDUSTRY_KEY: [fat, story(2)]}, capped={INDUSTRY_KEY: 5})
    )
    text = str(embed.description)
    assert discord_len(text) <= 4096
    assert text.count("<https") == text.count(">")
    assert "Fat headline" in text and "Fat summary" in text
    assert "Headline sentinel 2" in text
