"""The free headline render, the per-guild digest render, and the guild run report (plan task 6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from newsbot.bot.format import (
    discord_len,
    render_guild_digest,
    render_guild_run_report,
    render_headlines_embed,
)
from newsbot.config import GameCfg
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store.models import HeadlineItem

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
BL4 = GameCfg(key="borderlands4", name="Borderlands 4")
PAL = GameCfg(key="palworld", name="Palworld")


def item(title, trust="press", *, uncertain=False, age_h=1.0, url=None, published=True):
    moment = T0 - timedelta(hours=age_h)
    return HeadlineItem(
        url=url or f"https://example.com/{title.replace(' ', '-').lower()}",
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=moment if published else None,
        collected_at=moment,
        uncertain=uncertain,
    )


def lines(embed) -> list[str]:
    return str(embed.description).split("\n")


def test_order_is_trust_then_confident_then_newest():
    items = [
        item("community new", "community", age_h=1),
        item("press uncertain", "press", uncertain=True, age_h=1),
        item("press old", "press", age_h=9),
        item("official old", "official", age_h=8),
        item("press new", "press", age_h=2),
        item("official new", "official", age_h=3),
        item("community old", "community", age_h=20),
    ]
    embed = render_headlines_embed(BL4, items)
    titles = [ln.split("OFFICIAL · ")[-1].split(" — ")[0].lstrip("• ") for ln in lines(embed)]
    assert titles == [
        "official new",
        "official old",
        "press new",
        "press old",
        "press uncertain",
        "community new",
        "community old",
    ]


def test_only_official_items_get_the_marker_and_nothing_says_rumor():
    embed = render_headlines_embed(
        BL4, [item("a patch", "official"), item("a post", "press"), item("a thread", "community")]
    )
    text = str(embed.description)
    assert text.count("🟢 OFFICIAL") == 1
    assert lines(embed)[0].startswith("• 🟢 OFFICIAL · a patch")
    assert lines(embed)[1].startswith("• a post") and lines(embed)[2].startswith("• a thread")
    assert "RUMOR" not in text.upper() and "REPORTED" not in text.upper()
    assert embed.title == "Borderlands 4"


def test_an_undated_item_sorts_by_when_it_was_collected():
    embed = render_headlines_embed(
        BL4, [item("dated old", age_h=10), item("undated fresh", age_h=1, published=False)]
    )
    assert lines(embed)[0].startswith("• undated fresh")


def test_titles_are_escaped_and_links_are_suppressed_embeds():
    evil = item("@everyone **bold** <#123> https://evil.example", url="https://example.com/a b")
    text = str(render_headlines_embed(BL4, [evil]).description)
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "\\*\\*bold\\*\\*" in text
    assert "<​#123>" in text
    assert "https:​//evil.example" in text
    # The link is re-canonicalized (the space is percent-encoded) and wrapped in <>.
    assert "— <https://example.com/a%20b>" in text


def test_an_item_with_an_unusable_url_is_dropped_and_not_counted():
    embed = render_headlines_embed(BL4, [item("bad", url="not a url"), item("good")])
    assert lines(embed) == ["• good — <https://example.com/good>"]
    assert render_headlines_embed(BL4, [item("bad", url="not a url")]) is None


def test_nothing_to_show_means_no_embed():
    assert render_headlines_embed(BL4, []) is None
    assert render_headlines_embed(BL4, [], note="Summary unavailable") is None


def test_the_note_goes_on_top_and_is_not_counted_as_more():
    embed = render_headlines_embed(BL4, [item("a"), item("b")], note="Summary unavailable; x.")
    assert lines(embed)[0] == "Summary unavailable; x."
    assert "more, use /news" not in str(embed.description)


def test_over_the_limit_sheds_whole_lines_from_the_bottom_with_a_more_line():
    items = [item(f"headline number {n:03d} " + "x" * 60, age_h=n / 10) for n in range(1, 120)]
    embed = render_headlines_embed(BL4, items, note="Summary unavailable; showing headlines.")
    text = str(embed.description)
    assert discord_len(text) <= 4096
    body = text.split("\n\n")[0].split("\n")
    assert body[0].startswith("Summary unavailable")
    shown = len(body) - 1
    assert 0 < shown < 119
    assert text.endswith(f"+{119 - shown} more, use /news")
    # The newest survive; every shown line is whole (ends with its link).
    assert "headline number 001" in body[1]
    assert all(line.endswith(">") for line in body[1:])


def test_the_limit_counts_utf16_units_not_characters():
    # Each emoji title is 2 UTF-16 units per character; 60 of them per line.
    items = [item("😀" * 60 + f" {n}", age_h=n) for n in range(1, 60)]
    embed = render_headlines_embed(BL4, items)
    assert discord_len(str(embed.description)) <= 4096
    assert "more, use /news" in str(embed.description)


def test_one_enormous_title_is_hard_truncated_not_dropped():
    embed = render_headlines_embed(BL4, [item("y" * 6000)])
    assert embed is not None and discord_len(str(embed.description)) <= 4096


# --- render_guild_digest ---


def story(headline="Big patch", label="official", urls=("https://example.com/s",)):
    return StoryDraft(headline, "It fixes things.", label, list(urls), None)


def render(games, **kwargs):
    kwargs.setdefault("stories_by_game", {})
    kwargs.setdefault("items_by_game", {})
    kwargs.setdefault("notes_by_game", {})
    kwargs.setdefault("coverage_notes", [])
    return render_guild_digest(
        date(2026, 9, 30), games, {"borderlands4": 11, "palworld": 12}, **kwargs
    )


def test_guild_digest_posts_games_in_the_order_given_each_to_its_channel():
    rendered = render(
        [BL4, PAL], items_by_game={"palworld": [item("p")], "borderlands4": [item("b")]}
    )
    assert [(m.topic_key, m.channel_id) for m in rendered.messages] == [
        ("borderlands4", 11),
        ("palworld", 12),
    ]


def test_a_game_with_no_items_posts_nothing():
    rendered = render([BL4, PAL], items_by_game={"palworld": [item("p")]})
    assert [m.topic_key for m in rendered.messages] == ["palworld"]
    assert render([BL4, PAL]).messages == []


def test_a_stored_summary_renders_as_stories_and_beats_headlines():
    rendered = render(
        [BL4],
        stories_by_game={"borderlands4": [story()]},
        items_by_game={"borderlands4": [item("z")]},
    )
    [message] = rendered.messages
    assert "🟢 OFFICIAL · Big patch" in str(message.embed.description)
    assert "• z" not in str(message.embed.description)


def test_a_summary_with_no_stories_posts_nothing_even_if_items_exist():
    rendered = render(
        [BL4], stories_by_game={"borderlands4": []}, items_by_game={"borderlands4": [item("z")]}
    )
    assert rendered.messages == []


def test_coverage_notes_ride_in_every_footer_and_the_note_heads_a_fallback():
    rendered = render(
        [BL4, PAL],
        stories_by_game={"borderlands4": [story()]},
        items_by_game={"palworld": [item("p")]},
        notes_by_game={"palworld": "Summary unavailable; showing headlines."},
        coverage_notes=["web search skipped"],
    )
    for message in rendered.messages:
        assert message.embed.footer.text == "Reduced coverage today: web search skipped"
    assert str(rendered.messages[1].embed.description).startswith("Summary unavailable")


# --- render_guild_run_report ---


def report(**overrides):
    args = dict(
        status="ok",
        run_date=date(2026, 9, 30),
        run_kind="scheduled",
        games=[BL4, PAL],
        counts={"borderlands4": 3, "palworld": 0},
        channels={"borderlands4": 11, "palworld": 12},
        posted_by_game={"borderlands4": 777},
        skipped={},
        duration=timedelta(seconds=12),
        notes=[],
        guild_id=42,
    )
    args.update(overrides)
    return render_guild_run_report(**args)


def test_run_report_has_counts_jump_links_and_duration_but_no_spend_or_sources():
    text = report()
    assert text.startswith("✅ **Digest posted** · Wed Sep 30 (scheduled)")
    assert (
        "3 items: Borderlands 4 3 [jump](<https://discord.com/channels/42/11/777>) · Palworld 0"
        in text
    )
    assert text.endswith("Took 12s")
    assert "Claude" not in text and "Sources" not in text and "$" not in text


def test_run_report_names_skipped_games_and_partial_notes():
    text = report(
        status="partial",
        run_kind="run-now",
        skipped={"palworld": "channel deleted"},
        notes=["palworld: skipped (channel deleted)"],
    )
    assert "⚠️ **Digest posted with gaps**" in text and "(run-now)" in text
    assert "Skipped: Palworld (channel deleted)" in text
    assert "Notes: palworld: skipped (channel deleted)" in text


def test_run_report_sheds_detail_to_stay_under_the_message_limit():
    many = [GameCfg(key=f"g{n}", name=f"Game number {n} " + "w" * 40) for n in range(40)]
    text = report(
        games=many,
        counts={g.key: 1 for g in many},
        channels={g.key: 1000 + n for n, g in enumerate(many)},
        posted_by_game={g.key: 5000 + n for n, g in enumerate(many)},
        skipped={"g1": "r" * 500},
        status="partial",
        notes=["n" * 500],
    )
    assert discord_len(text) <= 2000
    assert text.startswith("⚠️")
