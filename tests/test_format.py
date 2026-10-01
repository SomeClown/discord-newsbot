"""Tests for newsbot.bot.format: escaping, story sorting, embed limits and plain-text rendering.

The story tests go through `render_guild_digest` with a stored summary per game, which is the
only way stories reach an embed since v3. (They used to run through v2's `render_digest`.)
"""

from __future__ import annotations

from datetime import date

from newsbot.bot.format import esc, render_guild_digest, to_text
from newsbot.config import Topic
from newsbot.pipeline.summarize import StoryDraft

RUN_DATE = date(2026, 9, 23)
BL4 = Topic(key="borderlands4", name="Borderlands 4", channel_id=1, aliases=[], entities=[])
PALWORLD = Topic(key="palworld", name="Palworld", channel_id=2, aliases=[], entities=[])
DIABLO4 = Topic(key="diablo4", name="Diablo IV", channel_id=3, aliases=[], entities=[])
TOPICS = [BL4, PALWORLD, DIABLO4]


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


# --- esc ---


def test_esc_escapes_markdown_and_everyone_mention():
    result = esc("**bold** @everyone")
    assert "**bold**" not in result
    assert "@everyone" not in result or "@​everyone" in result  # zero-width-joined, not live


def test_esc_defuses_a_url_scheme_that_slipped_through_layer_one():
    # postprocess() in summarize.py is layer one and should have already
    # stripped this: esc() is the belt-and-suspenders second layer for
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
    # link phishing shape: the scheme inside the parens is what needs
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
    rendered = _digest([PALWORLD], {"palworld": stories})
    description = _first_embed(rendered).description
    assert description.index("Official story") < description.index("Reported story")
    assert description.index("Reported story") < description.index("Rumor story")


def test_more_items_sort_before_fewer_within_same_label():
    stories = [
        _draft("One link", label="official", n_urls=1),
        _draft("Three links", label="official", n_urls=3),
    ]
    rendered = _digest([PALWORLD], {"palworld": stories})
    description = _first_embed(rendered).description
    assert description.index("Three links") < description.index("One link")


def test_update_gets_update_marker_and_sorts_within_its_label():
    stories = [
        _draft("Fresh official", label="official"),
        _draft("Updated official", label="official", update_of=5),
    ]
    rendered = _digest([PALWORLD], {"palworld": stories})
    description = _first_embed(rendered).description
    assert "🔁 UPDATE" in description
    assert "🟢 OFFICIAL" in description


# --- embed limits ---


def test_4096_boundary_cuts_least_important_and_adds_more_line():
    long_summary = "x" * 380
    stories = [_draft(f"Story {i}", summary=long_summary, label="official") for i in range(40)]
    rendered = _digest([PALWORLD], {"palworld": stories})
    embed = _first_embed(rendered)
    assert len(embed.description) <= 4096
    assert "more, use /news" in embed.description
    assert len(embed) <= 6000


def test_each_topic_gets_its_own_message_to_its_own_channel():
    stories = [_draft("A")]
    rendered = _digest(TOPICS, {t.key: stories for t in TOPICS})
    assert len(rendered.messages) == 3
    assert [m.channel_id for m in rendered.messages] == [1, 2, 3]
    assert [m.topic_key for m in rendered.messages] == [t.key for t in TOPICS]


# --- empty topic: posts nothing at all (design.md §13) ---


def test_empty_topic_produces_no_message():
    rendered = _digest([PALWORLD], {"palworld": []})
    assert rendered.messages == []


def test_topic_with_no_summary_at_all_also_produces_no_message():
    rendered = _digest([PALWORLD], {})
    assert rendered.messages == []


def test_topics_with_and_without_news_only_the_first_posts():
    stories = [_draft("A")]
    rendered = _digest(TOPICS, {"borderlands4": stories})
    assert [m.topic_key for m in rendered.messages] == ["borderlands4"]


def test_all_empty_topics_gives_no_messages_at_all():
    rendered = _digest(TOPICS, {t.key: [] for t in TOPICS})
    assert rendered.messages == []


def test_message_order_follows_the_topics_argument_not_alphabetical_or_key_order():
    # design.md §13: "Topics post in config order"; render_guild_digest must
    # never impose an order of its own. Handed topics reversed from
    # TOPICS's usual borderlands4/palworld/diablo4 order (which also isn't
    # alphabetical, so a stray sort() on name or key would show up here).
    reversed_topics = [DIABLO4, PALWORLD, BL4]
    stories = [_draft("A")]
    rendered = _digest(reversed_topics, {t.key: stories for t in reversed_topics})
    assert [m.topic_key for m in rendered.messages] == ["diablo4", "palworld", "borderlands4"]


def test_story_url_with_breakout_characters_never_produces_a_masked_link():
    # A URL shaped to break out of the <...> autolink and grow a fake
    # [text](url) markdown link right after it (see normalize.py's
    # canonicalize() docstring). Even if a URL like this somehow reached
    # the renderer without going through postprocess's canonicalize
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
    rendered = _digest([PALWORLD], {"palworld": stories})
    description = _first_embed(rendered).description
    assert "](https://evil.example" not in description
    assert "> **boom**" not in description


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
    rendered = _digest([PALWORLD], {"palworld": stories})
    description = _first_embed(rendered).description
    assert "**Big**" not in description
    assert "@everyone" not in description


# --- coverage notes (D1: footer of every posted embed) ---


def test_coverage_note_appears_in_every_posted_embeds_footer():
    stories = [_draft("A")]
    rendered = _digest(
        TOPICS, {t.key: stories for t in TOPICS}, ["Brave search skipped: quota exceeded"]
    )
    assert len(rendered.messages) == 3
    for message in rendered.messages:
        assert "Brave search skipped: quota exceeded" in message.embed.footer.text


def test_no_coverage_notes_leaves_footer_unset():
    rendered = _digest([PALWORLD], {"palworld": [_draft("A")]})
    assert _first_embed(rendered).footer.text is None


def test_empty_topic_with_coverage_notes_still_produces_no_message():
    # A topic that isn't posting shouldn't get a footer added to an embed
    # that never gets sent.
    rendered = _digest([PALWORLD], {"palworld": []}, ["some note"])
    assert rendered.messages == []


# --- to_text ---


def test_to_text_includes_channel_and_topic_titles():
    rendered = _digest([PALWORLD], {"palworld": [_draft("A")]})
    text = to_text(rendered)
    assert "Palworld" in text
    assert "A" in text
    assert "#2" in text  # PALWORLD's channel_id


def test_to_text_with_nothing_to_post_says_so():
    rendered = _digest([PALWORLD], {"palworld": []})
    assert to_text(rendered) == "Nothing would post today: no game has news."
