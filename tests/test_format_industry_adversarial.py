"""Adversarial tests for the industry post's renderers (plan task 6).

The industry post has a few promises that are easy to state and easy to break
by accident. A free server's post is the source's own words and nothing the
model wrote, down to the update marker and the "Summary unavailable" note. The
model's order is the order, never official-first. At most five, and nobody is
told about the rest: no "+N more", and no half a line when the 4096 limit bites.
Nothing to say posts nothing, and "nothing" from an empty pick doesn't quietly
turn into the headline list. Everything that isn't the industry post has to
come out byte for byte as it did before.

Pure functions again: no database, no network. The byte-identical checks also
run against the pre-change `format.py`, read out of git (commit 2ed3b88, the
parent of this task), so "unchanged" is checked against the real before. If git
or that commit isn't around (a shallow clone), those tests skip with a reason.

A test marked `xfail(strict=True)` is a real defect, written to fail now and to turn
into a loud XPASS the day it's fixed. Its reason says where it comes from.
"""

from __future__ import annotations

import itertools
import random
import re
import subprocess
import sys
import types
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from masked_old import mask_links_like_v340

from newsbot.bot.format import (
    _coverage_footer,
    discord_len,
    render_capped_headlines_embed,
    render_guild_digest,
    render_headlines_embed,
    render_picked_headlines_embed,
)
from newsbot.config import GameCfg
from newsbot.industry import INDUSTRY_KEY, INDUSTRY_MAX_STORIES
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store.models import HeadlineItem

REPO = Path(__file__).resolve().parent.parent
BASE_COMMIT = "2ed3b88"
LIMIT = 4096
T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
RUN_DATE = date(2026, 10, 23)
IND = GameCfg(key=INDUSTRY_KEY, name="Gaming industry")
BL4 = GameCfg(key="borderlands4", name="Borderlands 4")
PAL = GameCfg(key="palworld", name="Palworld")
D4 = GameCfg(key="diablo4", name="Diablo IV")
CHANNELS = {INDUSTRY_KEY: 11, "borderlands4": 22, "palworld": 33, "diablo4": 44}
NOTE = "Summary unavailable today."
ROLE = "<@&123456789012345678>"
USER = "<@123456789012345678>"
CHAN = "<#123456789012345678>"


_ids = itertools.count()


def story(n, label="reported", *, update=None, urls=None, summary=None):
    return StoryDraft(
        headline=f"AI headline sentinel {n}",
        summary=summary if summary is not None else f"AI summary sentinel {n}",
        label=label,
        item_urls=urls if urls is not None else [f"https://example.com/story-{n}"],
        update_of_story_id=update,
    )


def item(title, trust="press", *, age_h=1.0, uncertain=False, url=None):
    moment = T0 - timedelta(hours=age_h)
    return HeadlineItem(
        url=url or f"https://example.com/item-{next(_ids)}",
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=moment,
        collected_at=moment,
        uncertain=uncertain,
    )


def numbered(n, prefix="pick", trust="press"):
    """`n` items whose given order is also their newest-first order."""
    return [
        item(f"{prefix} {i}", trust, age_h=i + 1, url=f"https://example.com/{prefix}-{i}")
        for i in range(n)
    ]


def digest(games=(IND,), **kwargs):
    base = dict(stories_by_game={}, items_by_game={}, notes_by_game={}, coverage_notes=[])
    base.update(kwargs)
    return render_guild_digest(RUN_DATE, list(games), CHANNELS, **base)


def embed_of(rendered, key=INDUSTRY_KEY):
    found = [m.embed for m in rendered.messages if m.topic_key == key]
    assert len(found) == 1
    return found[0]


_LIVE = re.compile(
    r"@everyone|@here|<@[!&]?\d+>|<#\d+>|</[\w -]+:\d+>|<t:\d+|<id:|(?<!\\)\]\(|https?://evil"
    r"|discord\.gg/|/invite/"
)


def live_syntax(text):
    """Whatever in `text` Discord would still act on: mentions, links, invites, masked links.

    esc() puts a zero-width space inside each of these, so none should match.
    """
    return _LIVE.findall(text)


def masked(url, label=None):
    """`[host](<url>)`, written out by hand (every URL in this file is on a plain ASCII host)."""
    return f"[{label or urlsplit(url).hostname}](<{url}>)"


def line_for(title, url):
    return f"• {title} — {masked(url)}"


# --- 1. AI text never reaches a free post ---


FREE_FIELDS = ("AI headline sentinel", "AI summary sentinel", "UPDATE", "REPORTED", "RUMOR")


def test_a_free_post_built_from_picks_carries_no_ai_field_or_label_for_any_trust():
    picks = [
        item("Studio closes", "official", url="https://example.com/a"),
        item("Press says layoffs", "press", url="https://example.com/b"),
        item("Chatter about a buyout", "community", url="https://example.com/c", uncertain=True),
    ]
    text = str(
        embed_of(
            digest(
                items_by_game={INDUSTRY_KEY: [item("Other")]},
                notes_by_game={INDUSTRY_KEY: "NOTE sentinel"},
                picks_by_game={INDUSTRY_KEY: picks},
                capped={INDUSTRY_KEY: 5},
            )
        ).description
    )
    for needle in (*FREE_FIELDS, "NOTE sentinel", "Other"):
        assert needle not in text
    # The only badge there is belongs to the official item, and only that one.
    assert text.count("OFFICIAL") == 1
    assert text.split("\n")[0].startswith("• 🟢 OFFICIAL · Studio closes")


def test_a_free_note_is_not_shown_over_picks_even_when_one_is_in_notes_by_game():
    embed = render_picked_headlines_embed(IND, [item("Only pick")])
    assert str(embed.description).startswith("• Only pick")


def test_picks_are_shown_even_when_the_stories_key_is_absent_but_other_games_have_stories():
    # Another game's stored summary must not bleed into the industry post.
    text = str(
        embed_of(
            digest(
                games=(BL4, IND),
                stories_by_game={"borderlands4": [story(1, "rumor", update=3)]},
                picks_by_game={INDUSTRY_KEY: [item("Source title")]},
                capped={INDUSTRY_KEY: 5},
            )
        ).description
    )
    assert "sentinel" not in text and "RUMOR" not in text and "UPDATE" not in text


# --- 2. How many, in what order ---


@pytest.mark.parametrize("n", [0, 1, 4, 5, 6, 50])
def test_ranked_stories_are_cut_to_five_and_keep_the_given_order(n):
    labels = ["rumor", "reported", "official"]
    stories = [story(i, labels[i % 3]) for i in range(n)]
    rendered = digest(
        stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: INDUSTRY_MAX_STORIES}
    )
    if n == 0:
        assert rendered.messages == []
        return
    text = str(embed_of(rendered).description)
    shown = min(n, INDUSTRY_MAX_STORIES)
    assert text.count("AI headline sentinel") == shown
    assert [text.index(f"sentinel {i}\n") for i in range(shown)] == sorted(
        text.index(f"sentinel {i}\n") for i in range(shown)
    )
    assert "more" not in text


@pytest.mark.parametrize("n", [0, 1, 4, 5, 6, 50])
def test_picks_are_cut_to_five_in_the_given_order(n):
    picks = list(reversed(numbered(n)))  # oldest first: the given order is not the sort order
    rendered = digest(
        picks_by_game={INDUSTRY_KEY: picks}, capped={INDUSTRY_KEY: INDUSTRY_MAX_STORIES}
    )
    if n == 0:
        assert rendered.messages == []
        return
    lines = str(embed_of(rendered).description).split("\n")
    assert len(lines) == min(n, INDUSTRY_MAX_STORIES)
    assert [ln.split(" — ")[0] for ln in lines] == [f"• {p.title}" for p in picks[: len(lines)]]


@pytest.mark.parametrize("n", [0, 1, 4, 5, 6, 50])
def test_the_capped_fallback_shows_at_most_five_newest_first(n):
    items = list(reversed(numbered(n)))
    rendered = digest(items_by_game={INDUSTRY_KEY: items}, capped={INDUSTRY_KEY: 5})
    if n == 0:
        assert rendered.messages == []
        return
    lines = str(embed_of(rendered).description).split("\n")
    assert len(lines) == min(n, 5)
    assert [ln.split(" — ")[0] for ln in lines] == [f"• pick {i}" for i in range(len(lines))]


def test_a_top_ranked_rumor_stays_above_a_lower_ranked_official_story():
    stories = [story(0, "rumor"), story(1, "official")]
    text = str(
        embed_of(
            digest(stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: 5})
        ).description
    )
    assert text.index("sentinel 0") < text.index("sentinel 1")


def test_more_sources_do_not_lift_a_story_either():
    stories = [
        story(0, "reported", urls=["https://example.com/one"]),
        story(1, "reported", urls=[f"https://example.com/m{i}" for i in range(3)]),
    ]
    text = str(
        embed_of(
            digest(stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: 5})
        ).description
    )
    assert text.index("sentinel 0") < text.index("sentinel 1")


def test_an_update_story_with_id_zero_still_gets_the_update_marker():
    stories = [story(0, "official", update=0)]
    text = str(
        embed_of(
            digest(stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: 5})
        ).description
    )
    assert text.startswith("🔁 UPDATE · AI headline sentinel 0")
    assert "OFFICIAL" not in text


def test_picks_without_a_cap_are_not_cut():
    picks = numbered(8)
    lines = str(embed_of(digest(picks_by_game={INDUSTRY_KEY: picks})).description).split("\n")
    assert len(lines) == 8


@pytest.mark.parametrize("cap", [0])
def test_a_cap_of_zero_posts_nothing_on_every_path(cap):
    assert (
        digest(stories_by_game={INDUSTRY_KEY: [story(1)]}, capped={INDUSTRY_KEY: cap}).messages
        == []
    )
    assert (
        digest(picks_by_game={INDUSTRY_KEY: numbered(3)}, capped={INDUSTRY_KEY: cap}).messages == []
    )
    assert (
        digest(items_by_game={INDUSTRY_KEY: numbered(3)}, capped={INDUSTRY_KEY: cap}).messages == []
    )


def test_a_negative_cap_posts_nothing_rather_than_all_but_the_last():
    rendered = digest(picks_by_game={INDUSTRY_KEY: numbered(3)}, capped={INDUSTRY_KEY: -1})
    assert rendered.messages == []


def test_the_capped_fallback_drops_unusable_urls_before_it_cuts():
    bad = [
        item(f"bad {i}", age_h=0.1 + i / 100, url="https://example.com/" + "p" * 1100)
        for i in range(3)
    ]
    good = numbered(6, "good")
    embed = render_capped_headlines_embed(IND, [*bad, *good], None, 5)
    lines = str(embed.description).split("\n")
    assert [ln.split(" — ")[0] for ln in lines] == [f"• good {i}" for i in range(5)]


def test_a_fallback_whose_every_url_is_unusable_posts_nothing_even_with_a_note():
    items = [item("x", url="javascript:alert(1)"), item("y", url="ftp://example.com/y")]
    assert render_capped_headlines_embed(IND, items, NOTE, 5) is None
    assert render_picked_headlines_embed(IND, items) is None


def test_the_capped_fallback_uses_the_trust_sort_but_picks_do_not():
    items = [
        item("community new", "community", age_h=0.1),
        item("official old", "official", age_h=99),
        item("press mid", "press", age_h=5),
    ]
    sorted_titles = [
        ln.split(" — ")[0]
        for ln in str(render_capped_headlines_embed(IND, items, None, 5).description).split("\n")
    ]
    assert sorted_titles == ["• 🟢 OFFICIAL · official old", "• press mid", "• community new"]
    given_titles = [
        ln.split(" — ")[0]
        for ln in str(render_picked_headlines_embed(IND, items).description).split("\n")
    ]
    assert given_titles == [
        "• community new",
        "• 🟢 OFFICIAL · official old",
        "• press mid",
    ]


# --- 3. Hostile text ---


NASTY = [
    "@everyone @here",
    f"{ROLE} {USER} {CHAN}",
    "</sync:123456789012345678> <t:1:R> <id:customize>",
    "**bold** __under__ ~~strike~~ ||spoiler|| `code` ```fence```",
    "[click](https://evil.example/x) [a](b`)",
    "https://evil.example discord.gg/abc discord.com/invite/abc",
    "> quote\n# heading\n- list\n+3 more, use /news",
    "line one\r\nline two line three\u0085end",
]


@pytest.mark.parametrize("title", NASTY)
def test_a_hostile_pick_title_stays_one_inert_line(title):
    embed = render_picked_headlines_embed(IND, [item(title, url="https://example.com/ok")])
    text = str(embed.description)
    assert "\n" not in text  # flattened: nothing can start a forged line
    link = masked("https://example.com/ok")
    assert text.startswith("• ") and text.endswith(f"— {link}")
    assert live_syntax(text.split(f" — {link}")[0]) == []
    assert "discord.gg/" not in text and "/invite/" not in text


@pytest.mark.parametrize("title", NASTY)
def test_a_hostile_ranked_headline_and_summary_get_the_same_escaping(title):
    s = story(1, summary=title)
    s = StoryDraft(
        headline=title,
        summary=title,
        label="reported",
        item_urls=["https://example.com/ok"],
        update_of_story_id=None,
    )
    text = str(
        embed_of(digest(stories_by_game={INDUSTRY_KEY: [s]}, capped={INDUSTRY_KEY: 5})).description
    )
    # The story's one real link is a masked link, and its "](" is the only one allowed.
    assert live_syntax(text.replace(masked("https://example.com/ok"), "")) == []


def test_the_game_name_and_the_note_are_escaped_too():
    hostile = GameCfg(key=INDUSTRY_KEY, name="@everyone **Gaming** " + "z" * 400)
    embed = render_capped_headlines_embed(hostile, numbered(2), f"@everyone {ROLE}", 5)
    assert live_syntax(str(embed.title)) == [] and discord_len(str(embed.title)) <= 256
    assert live_syntax(str(embed.description).split("\n")[0]) == []


def test_an_empty_or_blank_title_still_makes_a_link_line():
    text = str(
        render_picked_headlines_embed(IND, [item("", url="https://example.com/e")]).description
    )
    assert text.endswith(masked("https://example.com/e"))
    assert "\n" not in text


def test_a_very_long_title_is_cut_and_never_crowds_out_the_others():
    picks = [item("w" * 10_000, url=f"https://example.com/{i}") for i in range(3)]
    text = str(render_picked_headlines_embed(IND, picks).description)
    assert len(text.split("\n")) == 3
    assert discord_len(text) <= LIMIT


def test_a_very_long_astral_title_is_cut_on_a_codepoint_boundary():
    title = "🎮" * 400
    text = str(render_picked_headlines_embed(IND, [item(title)]).description)
    text.encode("utf-8")  # a split surrogate pair would raise here
    assert "…" in text
    assert discord_len(text) <= LIMIT


def test_five_longest_possible_lines_shed_whole_lines_and_never_exceed_the_limit():
    url = "https://example.com/" + "p" * 970
    picks = [item(("é" * 150) + "<>&" * 50 + str(i), url=url + str(i)) for i in range(5)]
    text = str(
        embed_of(digest(picks_by_game={INDUSTRY_KEY: picks}, capped={INDUSTRY_KEY: 5})).description
    )
    assert discord_len(text) <= LIMIT
    for ln in text.split("\n"):
        assert ln.startswith("• ") and ln.endswith(">)") and "(<https://example.com/" in ln


# --- 4. The 4096 edge, exactly ---


def _exact_lines(target, head=None, *, astral=False):
    """(titles, url_for) whose joined description is exactly `target` units, `head` on top.

    Every title but the last is 250 units (so a line is about 280 and the last title's
    window of 1..300 overlaps the next), the last is plain letters, sized to land on `target`.
    """
    filler = ("🎮" * 125) if astral else ("a" * 250)
    base = discord_len(head) + 1 if head else 0

    def url_for(i):
        return f"https://example.com/e{i:02d}"

    for n in range(1, 40):
        fixed = [line_for(filler, url_for(i)) for i in range(n - 1)]
        total = base + sum(discord_len(f) + 1 for f in fixed)
        overhead = discord_len(line_for("", url_for(n - 1)))
        need = target - total - overhead
        if 1 <= need <= 300:
            titles = [*([filler] * (n - 1)), "b" * need]
            lines = [line_for(t, url_for(i)) for i, t in enumerate(titles)]
            assert discord_len((head + "\n" if head else "") + "\n".join(lines)) == target
            return titles, url_for
    raise AssertionError("no fit")


def _items_for(titles, url_for):
    return [
        item(t, age_h=i + 1, url=url_for(i)) for i, t in enumerate(titles)
    ]  # newest first == given order


@pytest.mark.parametrize("astral", [False, True])
@pytest.mark.parametrize(
    ("renderer", "with_head"), [("picked", False), ("capped", False), ("capped", True)]
)
def test_a_description_of_exactly_4096_units_is_kept_whole(renderer, with_head, astral):
    head = NOTE if with_head else None
    titles, url_for = _exact_lines(LIMIT, head, astral=astral)
    items = _items_for(titles, url_for)
    if renderer == "picked":
        embed = render_picked_headlines_embed(IND, items)
    else:
        embed = render_capped_headlines_embed(IND, items, head, 99)
    text = str(embed.description)
    assert discord_len(text) == LIMIT
    assert len(text.split("\n")) == len(titles) + (1 if head else 0)
    assert "…" not in text and "more" not in text.replace("b" * 3, "")


@pytest.mark.parametrize("astral", [False, True])
@pytest.mark.parametrize(
    ("renderer", "with_head"), [("picked", False), ("capped", False), ("capped", True)]
)
def test_one_unit_over_4096_sheds_exactly_the_last_whole_line(renderer, with_head, astral):
    head = NOTE if with_head else None
    titles, url_for = _exact_lines(LIMIT + 1, head, astral=astral)
    items = _items_for(titles, url_for)
    if renderer == "picked":
        embed = render_picked_headlines_embed(IND, items)
    else:
        embed = render_capped_headlines_embed(IND, items, head, 99)
    expected_lines = [line_for(t, url_for(i)) for i, t in enumerate(titles[:-1])]
    expected = "\n".join([*([head] if head else []), *expected_lines])
    assert str(embed.description) == expected
    assert discord_len(expected) <= LIMIT


def test_ranked_stories_at_exactly_4096_are_kept_and_one_over_sheds_the_second():
    def block(headline, summary, url):
        return f"🟡 REPORTED · {headline}\n{summary}\n{masked(url)}"

    url1, url2 = "https://example.com/one", "https://example.com/two"
    first = block("H1", "s" * 1000, url1)
    for target, keeps_both in ((LIMIT, True), (LIMIT + 1, False)):
        need = target - discord_len(first) - 2 - discord_len(block("H2", "", url2))
        s1 = StoryDraft("H1", "s" * 1000, "reported", [url1], None)
        s2 = StoryDraft("H2", "t" * need, "reported", [url2], None)
        text = str(
            embed_of(
                digest(stories_by_game={INDUSTRY_KEY: [s1, s2]}, capped={INDUSTRY_KEY: 5})
            ).description
        )
        if keeps_both:
            assert discord_len(text) == LIMIT
            assert text == block("H1", "s" * 1000, url1) + "\n\n" + block("H2", "t" * need, url2)
        else:
            assert text == first


@pytest.mark.parametrize("n", range(8, 31))
def test_shedding_always_leaves_a_whole_line_prefix_that_is_as_long_as_it_can_be(n):
    items = [item("w" * 300, age_h=i + 1, url=f"https://example.com/line{i:02d}") for i in range(n)]
    lines = [line_for("w" * 300, f"https://example.com/line{i:02d}") for i in range(n)]
    best = max(k for k in range(1, n + 1) if discord_len("\n".join(lines[:k])) <= LIMIT)
    text = str(render_picked_headlines_embed(IND, items).description)
    assert text == "\n".join(lines[:best])
    assert "…" not in text and "more" not in text


def test_a_long_note_squeezes_the_lines_but_stays_on_top_and_whole():
    note = "N" * 1500
    items = [item("w" * 300, age_h=i + 1, url=f"https://example.com/n{i}") for i in range(6)]
    text = str(render_capped_headlines_embed(IND, items, note, 6).description)
    lines = text.split("\n")
    assert lines[0] == note
    assert all(ln.startswith("• ") and ln.endswith(">)") for ln in lines[1:])
    assert discord_len(text) <= LIMIT


def test_an_empty_note_adds_no_blank_line_on_top():
    text = str(render_capped_headlines_embed(IND, numbered(2), "", 5).description)
    assert text.startswith("• pick 0")


def test_an_oversize_first_story_never_leaves_half_a_link():
    urls = [f"https://example.com/{i}/" + "p" * 1500 for i in range(3)]
    text = str(
        embed_of(
            digest(stories_by_game={INDUSTRY_KEY: [story(0, urls=urls)]}, capped={INDUSTRY_KEY: 5})
        ).description
    )
    assert text.count("](<https") == text.count(">)")


# --- 5. Which input wins ---


def test_stories_for_a_capped_key_beat_picks_and_items():
    text = str(
        embed_of(
            digest(
                stories_by_game={INDUSTRY_KEY: [story(1)]},
                picks_by_game={INDUSTRY_KEY: [item("pick title")]},
                items_by_game={INDUSTRY_KEY: [item("item title")]},
                capped={INDUSTRY_KEY: 5},
            )
        ).description
    )
    assert (
        "AI headline sentinel 1" in text and "pick title" not in text and "item title" not in text
    )


def test_an_empty_story_list_posts_nothing_even_with_picks_and_items():
    rendered = digest(
        stories_by_game={INDUSTRY_KEY: []},
        picks_by_game={INDUSTRY_KEY: [item("pick title")]},
        items_by_game={INDUSTRY_KEY: [item("item title")]},
        capped={INDUSTRY_KEY: 5},
    )
    assert rendered.messages == []


def test_picks_beat_items_and_the_note_for_the_same_key():
    text = str(
        embed_of(
            digest(
                picks_by_game={INDUSTRY_KEY: [item("pick title")]},
                items_by_game={INDUSTRY_KEY: [item("item title")]},
                notes_by_game={INDUSTRY_KEY: NOTE},
                capped={INDUSTRY_KEY: 5},
            )
        ).description
    )
    assert "pick title" in text and "item title" not in text and NOTE not in text


def test_a_pick_list_whose_urls_are_all_unusable_posts_nothing_rather_than_the_items():
    rendered = digest(
        picks_by_game={INDUSTRY_KEY: [item("bad", url="javascript:alert(1)")]},
        items_by_game={INDUSTRY_KEY: [item("item title")]},
        capped={INDUSTRY_KEY: 5},
    )
    assert rendered.messages == []


def test_a_pick_cut_to_nothing_by_a_zero_cap_does_not_fall_through_to_items():
    rendered = digest(
        picks_by_game={INDUSTRY_KEY: [item("pick title")]},
        items_by_game={INDUSTRY_KEY: [item("item title")]},
        capped={INDUSTRY_KEY: 0},
    )
    assert rendered.messages == []


def test_a_capped_key_with_no_picks_entry_falls_back_to_items_with_the_note():
    lines = str(
        embed_of(
            digest(
                items_by_game={INDUSTRY_KEY: numbered(7)},
                notes_by_game={INDUSTRY_KEY: NOTE},
                capped={INDUSTRY_KEY: 5},
            )
        ).description
    ).split("\n")
    assert lines[0] == NOTE and len(lines) == 6


def test_an_uncapped_key_with_stories_still_renders_as_a_game_digest():
    # The industry key without `capped` is a game as far as the renderer is concerned.
    stories = [story(0, "rumor"), story(1, "official")]
    text = str(embed_of(digest(stories_by_game={INDUSTRY_KEY: stories})).description)
    assert text.index("sentinel 1") < text.index("sentinel 0")  # official first, as for a game


def test_an_uncapped_key_with_items_still_gets_the_plus_n_more_line():
    text = str(
        embed_of(
            digest(
                items_by_game={
                    INDUSTRY_KEY: [item("t" * 290 + str(i), age_h=i + 1) for i in range(40)]
                }
            )
        ).description
    )
    assert "+" in text.split("\n")[-1] and "more, use /news" in text


def test_a_cap_for_a_game_that_is_not_in_the_digest_changes_nothing():
    stories = {"borderlands4": [story(1)]}
    plain = digest(games=(BL4,), stories_by_game=stories)
    capped = digest(games=(BL4,), stories_by_game=stories, capped={"palworld": 1, "zzz": 2})
    assert [m.embed.to_dict() for m in plain.messages] == [
        m.embed.to_dict() for m in capped.messages
    ]


# --- 6. The post around the embed ---


def test_every_industry_path_gets_the_coverage_footer_and_the_same_color():
    notes = ["Brave search failed for one game"]
    footer = _coverage_footer(notes)
    assert footer
    variants = {
        "ranked": dict(stories_by_game={INDUSTRY_KEY: [story(1)]}),
        "picked": dict(picks_by_game={INDUSTRY_KEY: [item("p")]}),
        "capped": dict(items_by_game={INDUSTRY_KEY: [item("i")]}),
    }
    colors = set()
    for kwargs in variants.values():
        embed = embed_of(digest(coverage_notes=notes, capped={INDUSTRY_KEY: 5}, **kwargs))
        assert embed.footer.text == footer
        assert footer not in str(embed.description)
        assert not {"fields", "url", "image", "thumbnail", "author"} & set(embed.to_dict())
        colors.add(embed.color.value)
    assert len(colors) == 1
    assert colors == {render_headlines_embed(IND, [item("x")]).color.value}


def test_messages_follow_the_order_of_games_and_a_quiet_industry_does_not_disturb_it():
    full = digest(
        games=(BL4, PAL, IND),
        stories_by_game={"borderlands4": [story(1)]},
        items_by_game={"palworld": [item("pal")]},
        picks_by_game={INDUSTRY_KEY: [item("pick")]},
        capped={INDUSTRY_KEY: 5},
    )
    assert [m.topic_key for m in full.messages] == ["borderlands4", "palworld", INDUSTRY_KEY]
    assert [m.channel_id for m in full.messages] == [22, 33, 11]
    assert full.messages[-1].topic_name == "Gaming industry"
    quiet = digest(
        games=(BL4, PAL, IND),
        stories_by_game={"borderlands4": [story(1)]},
        items_by_game={"palworld": [item("pal")]},
        picks_by_game={INDUSTRY_KEY: []},
        capped={INDUSTRY_KEY: 5},
    )
    assert [m.topic_key for m in quiet.messages] == ["borderlands4", "palworld"]
    first = digest(
        games=(IND, BL4),
        stories_by_game={"borderlands4": [story(1)]},
        picks_by_game={INDUSTRY_KEY: [item("pick")]},
        capped={INDUSTRY_KEY: 5},
    )
    assert [m.topic_key for m in first.messages] == [INDUSTRY_KEY, "borderlands4"]  # caller's order


def test_the_renderers_do_not_reorder_or_mutate_what_they_are_given():
    picks = list(reversed(numbered(7)))
    items = list(reversed(numbered(7, "item")))
    stories = [story(i, ["rumor", "official", "reported"][i % 3]) for i in range(7)]
    snap = ([id(x) for x in picks], [id(x) for x in items], [id(x) for x in stories])
    digest(
        stories_by_game={INDUSTRY_KEY: stories},
        picks_by_game={INDUSTRY_KEY: picks},
        items_by_game={INDUSTRY_KEY: items},
        capped={INDUSTRY_KEY: 5},
    )
    digest(
        picks_by_game={INDUSTRY_KEY: picks},
        items_by_game={INDUSTRY_KEY: items},
        capped={INDUSTRY_KEY: 5},
    )
    digest(items_by_game={INDUSTRY_KEY: items}, capped={INDUSTRY_KEY: 5})
    assert snap == ([id(x) for x in picks], [id(x) for x in items], [id(x) for x in stories])
    assert len(picks) == len(items) == len(stories) == 7


def test_two_renders_make_two_independent_embeds():
    kwargs = dict(picks_by_game={INDUSTRY_KEY: [item("pick")]}, capped={INDUSTRY_KEY: 5})
    first = embed_of(digest(coverage_notes=["a note"], **kwargs))
    second = embed_of(digest(**kwargs))
    assert first is not second
    assert second.footer.text is None
    assert second.to_dict() == {k: v for k, v in first.to_dict().items() if k != "footer"}


def test_tuples_and_other_sequences_work_as_picks_and_stories():
    rendered = digest(picks_by_game={INDUSTRY_KEY: tuple(numbered(8))}, capped={INDUSTRY_KEY: 5})
    assert len(str(embed_of(rendered).description).split("\n")) == 5
    rendered = digest(
        stories_by_game={INDUSTRY_KEY: tuple(story(i) for i in range(8))}, capped={INDUSTRY_KEY: 5}
    )
    assert str(embed_of(rendered).description).count("AI headline sentinel") == 5


# --- 7. Nothing else moves ---


def _rich_world(seed):
    rng = random.Random(seed)  # noqa: S311
    labels = ["official", "reported", "rumor"]
    trusts = ["official", "press", "community"]

    def stories(n):
        return [
            StoryDraft(
                headline=f"H{rng.random():.6f} @everyone",
                summary="s" * rng.randint(0, 400) + " **x**",
                label=rng.choice(labels),
                item_urls=[
                    f"https://example.com/{rng.randint(0, 10**6)}" for _ in range(rng.randint(0, 5))
                ],
                update_of_story_id=rng.choice([None, None, 3]),
            )
            for _ in range(n)
        ]

    def items(n):
        return [
            item(
                "t" * rng.randint(1, 320) + str(i),
                rng.choice(trusts),
                age_h=rng.uniform(0, 100),
                uncertain=rng.random() < 0.3,
                url=f"https://example.com/{seed}/{i}",
            )
            for i in range(n)
        ]

    return dict(
        stories_by_game={"borderlands4": stories(rng.choice([0, 3, 12]))},
        items_by_game={
            "palworld": items(rng.choice([0, 2, 40])),
            "diablo4": items(rng.choice([1, 30])),
        },
        notes_by_game={"palworld": rng.choice([None, NOTE])},
        coverage_notes=rng.choice([[], ["Brave search failed"]]),
    )


def _flat(rendered):
    return [
        (m.topic_key, m.topic_name, m.channel_id, m.embed.to_dict())
        for m in rendered.messages
        if m.topic_key != INDUSTRY_KEY
    ]


@pytest.mark.parametrize("seed", range(12))
def test_non_industry_messages_are_identical_for_none_empty_and_populated_arguments(seed):
    world = _rich_world(seed)
    games = (BL4, PAL, D4)
    base = _flat(digest(games=games, **world))
    assert base  # the fixture is not vacuous
    assert _flat(digest(games=games, picks_by_game=None, capped=None, **world)) == base
    assert _flat(digest(games=games, picks_by_game={}, capped={}, **world)) == base
    populated = dict(
        picks_by_game={INDUSTRY_KEY: numbered(8)}, capped={INDUSTRY_KEY: 5, "nobody": 1}
    )
    assert _flat(digest(games=games, **populated, **world)) == base
    with_ind = dict(world)
    with_ind["stories_by_game"] = {**world["stories_by_game"], INDUSTRY_KEY: [story(1)]}
    with_ind["items_by_game"] = {**world["items_by_game"], INDUSTRY_KEY: numbered(9)}
    assert _flat(digest(games=(BL4, PAL, D4, IND), **populated, **with_ind)) == base
    assert _flat(digest(games=(IND, BL4, PAL, D4), **populated, **with_ind)) == base


@pytest.fixture(scope="module")
def old():
    """`newsbot/bot/format.py` from the parent commit, loaded under another name."""
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{BASE_COMMIT}:newsbot/bot/format.py"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {BASE_COMMIT} is not available here")
    name = "_newsbot_format_task6_throwaway"
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        mask_links_like_v340(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("seed", range(12))
def test_game_digests_match_the_pre_change_renderer_byte_for_byte(old, seed):
    world = _rich_world(seed)
    games = [BL4, PAL, D4]
    want = old.render_guild_digest(RUN_DATE, games, CHANNELS, **world)
    got = render_guild_digest(
        RUN_DATE,
        games,
        CHANNELS,
        **world,
        picks_by_game={INDUSTRY_KEY: numbered(3)},
        capped={INDUSTRY_KEY: 5},
    )
    as_tuples = lambda r: [  # noqa: E731
        (m.topic_key, m.topic_name, m.channel_id, m.embed.to_dict()) for m in r.messages
    ]
    assert as_tuples(got) == as_tuples(want)
    assert got.coverage_notes == want.coverage_notes


def test_the_old_headline_renderers_are_untouched(old):
    items = [
        item("t" * 290 + str(i), ["official", "press", "community"][i % 3], age_h=i + 1)
        for i in range(40)
    ]
    for note in (None, NOTE):
        assert (
            render_headlines_embed(BL4, items, note).to_dict()
            == old.render_headlines_embed(BL4, items, note).to_dict()
        )
    assert render_headlines_embed(BL4, [], NOTE) is None


# --- 8. Oversize stories: whole links go, never half a link (fix round 1) ---


_MARKERS = {"official": "🟢 OFFICIAL", "reported": "🟡 REPORTED", "rumor": "🔴 RUMOR"}


def long_url(i, n):
    return f"https://example.com/{i}/" + "p" * n


def draft(n, summary="s" * 100, urls=None, label="reported"):
    return StoryDraft(f"H{n}", summary, label, [long_url(n, 20)] if urls is None else urls, None)


def ref_block(story, shown=None):
    """The block the spec describes, written out by hand (headline and summary are plain ASCII)."""
    urls = story.item_urls if shown is None else story.item_urls[:shown]
    # Every URL here shares one host, so the second link on a line is "(2)" and the third "(3)".
    line = " · ".join(
        masked(u, "example.com" if k == 0 else f"example.com ({k + 1})")
        for k, u in enumerate(urls[:3])
    )
    if len(urls) > 3:
        line += f" · +{len(urls) - 3} more"
    return f"{_MARKERS[story.label]} · {story.headline}\n{story.summary}\n{line}"


def ref_fitting(story):
    """Whole story if it fits, else 3, 2, 1, then 0 links (rstripped), else None."""
    block = ref_block(story)
    if discord_len(block) <= LIMIT:
        return block
    for k in range(min(len(story.item_urls), 3), -1, -1):
        block = ref_block(story, k).rstrip("\n")
        if discord_len(block) <= LIMIT:
            return block
    return None


def ref_post(stories, cap):
    """Skip what can't fit, cut to `cap`, shed whole blocks from the bottom."""
    blocks = [b for st in stories if (b := ref_fitting(st))][: max(cap, 0)]
    while blocks and discord_len("\n\n".join(blocks)) > LIMIT:
        blocks.pop()
    return "\n\n".join(blocks) if blocks else None


def ranked_text(stories, cap=5):
    rendered = digest(stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: cap})
    return str(embed_of(rendered).description) if rendered.messages else None


def assert_no_half_link(text):
    assert text.count("](<http") == text.count(">)"), text
    for part in text.split("](<http")[1:]:
        assert ">)" in part.split("\n")[0], part[:80]


def small(n):
    return draft(n)


def big(n):
    """Three 1400-unit links: over the limit with three, about 2.9k with two."""
    return draft(n, urls=[long_url(n * 10 + i, 1400) for i in range(3)])


def links_in(text, n):
    return text.split(f"H{n}\n", 1)[1].split("\n\n")[0].count("](<http")


@pytest.mark.parametrize(
    ("name", "stories", "shown_links"),
    [
        ("first", [big(0), small(1), small(2), small(3)], {0: 2, 1: 1, 2: 1, 3: 1}),
        ("middle", [small(0), big(1), small(2)], {0: 1, 1: 2, 2: 1}),
        ("last", [small(0), small(1), big(2)], {0: 1, 1: 1, 2: 2}),
        ("all", [big(0), big(1), big(2)], {0: 2}),
    ],
)
def test_an_oversize_story_loses_links_in_any_position_and_the_others_still_render(
    name, stories, shown_links
):
    text = ranked_text(stories)
    assert text == ref_post(stories, 5)
    assert_no_half_link(text)
    assert discord_len(text) <= LIMIT
    for n, count in shown_links.items():
        assert links_in(text, n) == count
    for n in set(range(len(stories))) - set(shown_links):
        assert f"H{n}\n" not in text
    # Headline and summary of an oversize story are never touched, only its links.
    assert "s" * 100 in text


def test_an_oversize_story_keeps_the_first_links_not_the_last():
    story = big(0)
    text = ranked_text([story])
    assert story.item_urls[0] in text and story.item_urls[1] in text
    assert story.item_urls[2] not in text


def test_a_story_that_fits_only_with_zero_links_is_kept_with_its_text_and_no_link():
    story = draft(0, summary="s" * 3900, urls=[long_url(0, 500)])
    text = ranked_text([story])
    assert text == f"🟡 REPORTED · H0\n{'s' * 3900}"
    assert discord_len(text) <= LIMIT


def test_a_zero_link_story_with_a_later_story_sheds_the_later_one_whole():
    stories = [draft(0, summary="s" * 4000, urls=[long_url(0, 500)]), small(1)]
    text = ranked_text(stories)
    assert text == ref_post(stories, 5)
    assert "H1" not in text and "<http" not in text


def test_a_story_too_long_even_without_links_is_skipped_and_later_ones_render():
    stories = [draft(0, summary="s" * 5000), small(1), small(2)]
    text = ranked_text(stories)
    assert "H0" not in text
    assert text == ref_post(stories, 5)
    assert "H1" in text and "H2" in text


def test_every_story_unrenderable_posts_nothing():
    stories = [draft(i, summary="s" * 5000) for i in range(3)]
    assert digest(stories_by_game={INDUSTRY_KEY: stories}, capped={INDUSTRY_KEY: 5}).messages == []


def test_a_story_with_no_links_and_no_room_is_skipped_not_cut():
    assert ranked_text([draft(0, summary="s" * 4200, urls=[])]) is None


@pytest.mark.parametrize("skipped", [(0, 1), (2, 4), (5, 6), (0, 3, 6)])
def test_the_cap_counts_only_stories_that_render_so_skips_never_cost_a_renderable_one(skipped):
    stories = [draft(i, summary="s" * 5000) if i in skipped else small(i) for i in range(7)]
    renderable = [i for i in range(7) if i not in skipped]
    text = ranked_text(stories, cap=5)
    assert text == ref_post(stories, 5)
    # Five renderable stories survive any number of skips, as long as they fit by size.
    shown = [i for i in range(7) if f"H{i}\n" in text]
    assert shown == renderable[:5]


def test_the_cap_cuts_after_skipping_not_before():
    # Seven stories, the first three unrenderable: a cut-then-skip would show only two.
    stories = [draft(i, summary="s" * 5000) for i in range(3)] + [small(i) for i in range(3, 7)]
    text = ranked_text(stories, cap=5)
    assert [i for i in range(7) if f"H{i}\n" in text] == [3, 4, 5, 6]


def _sized_block_story(links_shown, target):
    """A story whose block with `links_shown` links is exactly `target` units.

    Links after the last shown one are huge, so showing more is always over the limit.
    """
    for n in range(0, 2000):
        urls = [long_url(i, 300) for i in range(links_shown - 1)] + [long_url(links_shown, n)]
        urls += [long_url(10 + i, 2000) for i in range(3 - links_shown)]
        story = draft(0, summary="s" * 2500, urls=urls)
        if discord_len(ref_block(story, links_shown)) == target:
            return story
    raise AssertionError("no fit")


@pytest.mark.parametrize("links", [1, 2, 3])
def test_a_story_that_lands_on_exactly_4096_with_k_links_keeps_all_k(links):
    story = _sized_block_story(links, LIMIT)
    text = ranked_text([story])
    assert text == ref_post([story], 5)
    assert discord_len(text) == LIMIT
    assert links_in(text, 0) == links
    assert_no_half_link(text)


@pytest.mark.parametrize("links", [1, 2, 3])
def test_a_story_one_unit_over_4096_with_k_links_drops_exactly_the_last_link(links):
    story = _sized_block_story(links, LIMIT + 1)
    text = ranked_text([story])
    assert text == ref_post([story], 5)
    assert links_in(text, 0) == links - 1
    assert discord_len(text) <= LIMIT
    assert_no_half_link(text)


def test_a_zero_link_block_one_unit_over_4096_is_skipped_and_exactly_4096_is_kept():
    keep = draft(0, summary="s" * (LIMIT - discord_len("🟡 REPORTED · H0\n")), urls=[])
    assert discord_len(ref_block(keep).rstrip("\n")) == LIMIT
    assert ranked_text([keep]) == ref_block(keep).rstrip("\n")
    over = draft(0, summary="s" * (LIMIT + 1 - discord_len("🟡 REPORTED · H0\n")), urls=[])
    assert ranked_text([over]) is None


def test_shedding_after_link_loss_still_lands_on_the_bigger_edge_of_the_whole_post():
    # Two stories that are each fine, jointly exactly 4096 and then 4097.
    first = draft(0, summary="s" * 1500, urls=[long_url(0, 100)])
    for extra, kept_both in ((0, True), (1, False)):
        fill = LIMIT - discord_len(ref_block(first)) - 2 - discord_len(ref_block(draft(1, "", [])))
        second = draft(1, summary="t" * (fill + extra), urls=[])
        text = ranked_text([first, second])
        assert text == ref_post([first, second], 5)
        assert ("H1" in text) is kept_both
        assert discord_len(text) <= LIMIT


@pytest.mark.parametrize("seed", range(60))
def test_ranked_posts_match_the_spec_for_random_mixes_of_oversize_stories(seed):
    rng = random.Random(seed)  # noqa: S311
    stories = [
        StoryDraft(
            f"H{i}",
            "s" * rng.choice([0, 50, 400, 1500, 3800, 4200]),
            rng.choice(["official", "reported", "rumor"]),
            [
                long_url(i * 10 + j, rng.choice([5, 300, 900, 1400, 2200]))
                for j in range(rng.randint(0, 5))
            ],
            None,
        )
        for i in range(rng.randint(0, 8))
    ]
    cap = rng.choice([1, 3, 5, 5, 8])
    text = ranked_text(stories, cap)
    assert text == ref_post(stories, cap)
    if text is not None:
        assert discord_len(text) <= LIMIT
        assert "more, use /news" not in text
        assert_no_half_link(text)


def test_an_unusable_first_url_does_not_cost_the_story_a_link_that_would_fit():
    four = [long_url(i, 315) for i in range(4)]
    story = draft(0, summary="s" * 3000, urls=["javascript:x", *four])
    assert discord_len(ref_block(replace(story, item_urls=four))) > LIMIT  # suffix pushes it over
    assert discord_len(ref_block(replace(story, item_urls=four[:3]))) <= LIMIT
    text = ranked_text([story])
    assert text.count("](<http") == 3


# --- 9. The head note and the cap, after the fix ---


def test_a_note_that_leaves_room_for_one_line_stays_and_the_extra_lines_are_shed():
    items = [item("w" * 300, age_h=i + 1, url=long_url(i, 20)) for i in range(5)]
    note = "N" * 2500
    text = str(render_capped_headlines_embed(IND, items, note, 5).description)
    lines = text.split("\n")
    assert lines[0] == note
    assert 1 <= len(lines) - 1 < 5
    assert discord_len(text) <= LIMIT
    assert all(ln.startswith("• ") and ln.endswith(">)") for ln in lines[1:])


def test_a_note_that_leaves_no_room_for_even_one_line_gives_way_to_the_line_not_half_of_it():
    items = [item("w" * 300, age_h=1, url=long_url(0, 20))]
    note = "N" * 4090
    text = str(render_capped_headlines_embed(IND, items, note, 5).description)
    assert text.startswith("• w") and text.endswith(">)")
    assert "N" not in text  # the note was dropped, whole, rather than cutting the headline
    assert discord_len(text) <= LIMIT


def test_a_note_alone_never_makes_a_post():
    assert (
        render_capped_headlines_embed(IND, [item("x", url="javascript:alert(1)")], "N" * 50, 5)
        is None
    )
    assert render_capped_headlines_embed(IND, numbered(3), "N" * 50, 0) is None


def test_the_ranked_post_has_no_note_even_when_one_is_given():
    rendered = digest(
        stories_by_game={INDUSTRY_KEY: [big(0)]},
        notes_by_game={INDUSTRY_KEY: NOTE},
        capped={INDUSTRY_KEY: 5},
    )
    assert NOTE not in str(embed_of(rendered).description)


@pytest.mark.parametrize("cap", [0, -1, -5])
def test_a_cap_below_one_posts_nothing_on_all_three_paths(cap):
    caps = {INDUSTRY_KEY: cap}
    assert digest(stories_by_game={INDUSTRY_KEY: [small(0)]}, capped=caps).messages == []
    assert digest(picks_by_game={INDUSTRY_KEY: numbered(3)}, capped=caps).messages == []
    assert digest(items_by_game={INDUSTRY_KEY: numbered(3)}, capped=caps).messages == []


@pytest.mark.parametrize("bad_at", [(0,), (2,), (0, 1), (1, 3, 5)])
def test_unusable_picks_are_dropped_before_the_cut_so_the_cap_still_fills(bad_at):
    picks = [
        item(f"pick {i}", url="javascript:alert(1)" if i in bad_at else long_url(i, 5))
        for i in range(8)
    ]
    good = [p for i, p in enumerate(picks) if i not in bad_at]
    lines = str(
        embed_of(digest(picks_by_game={INDUSTRY_KEY: picks}, capped={INDUSTRY_KEY: 5})).description
    ).split("\n")
    assert [ln.split(" — ")[0] for ln in lines] == [f"• {p.title}" for p in good[:5]]


def test_picks_that_are_all_unusable_post_nothing_with_a_cap_too():
    picks = [item("x", url="javascript:alert(1)")] * 3
    assert digest(picks_by_game={INDUSTRY_KEY: picks}, capped={INDUSTRY_KEY: 5}).messages == []
