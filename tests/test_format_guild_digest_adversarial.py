"""Adversarial tests for the free headline render and the per-guild run report (plan task 6).

A headline list is a hostile document by construction: every line is a string
somebody else's server wrote, rendered into a channel full of strangers, with
a little green "OFFICIAL" badge that means "this source is on the owner's
trusted list". So this file is mostly about two promises. One, nothing a feed
says can become live Discord syntax (a mention, a masked link, an invite, a
forged badge). Two, the arithmetic on the way out is exact: Discord counts
UTF-16 code units, "+N more" has to be the number actually cut, and a link
that can't be made safe is dropped rather than counted.

Pure functions; no database, no network. Strict xfails are real holes, each
says what it found.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from newsbot.bot.format import (
    _headline_sort_key,
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
LIMIT = 4096


def item(
    title,
    *,
    url=None,
    trust="press",
    uncertain=False,
    age_h=1.0,
    published=True,
    published_at=None,
) -> HeadlineItem:
    moment = T0 - timedelta(hours=age_h)
    slug = re.sub(r"\W+", "-", title)[:40] or "untitled"
    return HeadlineItem(
        url=url if url is not None else f"https://example.com/{slug}-{age_h}",
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=published_at or (moment if published else None),
        collected_at=moment,
        uncertain=uncertain,
    )


def body(embed) -> list[str]:
    return str(embed.description).split("\n")


def shown_count(embed) -> int:
    return sum(1 for line in body(embed) if line.startswith("• "))


def more_count(embed) -> int:
    found = re.search(r"\+(\d+) more, use /news", str(embed.description))
    return int(found.group(1)) if found else 0


# --- hostile titles ---

_HOSTILE_TITLES = [
    "@everyone free loot",
    "@here look",
    "<@123456789012345678> you won",
    "<@&123456789012345678> role call",
    "<#999> go here",
    "</newsbot:1234> run me",
    "[click me](https://evil.example/phish)",
    "[click me](<https://evil.example/phish>)",
    "**bold** __under__ ~~strike~~ ||spoiler|| `code` ```block```",
    "https://evil.example/bare",
    "<https://evil.example/autolink>",
    "discord.gg/abcdef and discord.com/invite/abcdef",
    "‮evil‬ bidi override",
    "a" * 30 + "​" * 30 + "@everyone",
    "@​everyone with a zero width space already in it",
]


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_a_hostile_title_renders_with_no_live_syntax_and_one_real_link(title):
    embed = render_headlines_embed(BL4, [item(title, url="https://example.com/real")])
    [line] = body(embed)
    text, _, link = line.rpartition(" — ")
    assert link == "<https://example.com/real>"
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "@here" not in text.replace("@​here", "")
    # The real assertions, stated plainly: after the trailing link, no scheme, mention or
    # masked-link opener is left in the title text in a form Discord would act on.
    assert not re.search(r"<@[!&]?\d{17,20}>|<#\d+>|</[\w-]+:\d+>", text)
    assert not re.search(r"https?://", text)
    assert not re.search(r"(?<!\\)\[[^\]]*\]\(", text)
    assert not re.search(r"(?<!\\)\|\|", text)
    assert not re.search(r"(?<![\\\w])(discord\.gg|discord\.com/invite)/", text)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "esc() leaves newlines alone, so a title with a line break can start its own line in "
        "the list, including a forged '🟢 OFFICIAL' one. Bluesky posts are multi-line, so "
        "real titles do this. The fix is to flatten whitespace (text.plain_line) before "
        "building the line."
    ),
)
def test_a_title_with_a_newline_cannot_forge_an_official_line():
    forged = "normal\n• 🟢 OFFICIAL · Free V-Bucks here"
    embed = render_headlines_embed(BL4, [item(forged, trust="community")])
    assert len(body(embed)) == 1
    assert "🟢 OFFICIAL" not in str(embed.description)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Same cause: a title can carry its own '+99 more, use /news' line and impersonate "
        "the renderer's count."
    ),
)
def test_a_title_cannot_forge_the_more_line():
    embed = render_headlines_embed(BL4, [item("x\n\n+99 more, use /news")])
    assert more_count(embed) == 0


@pytest.mark.parametrize("title", ["", " ", "\t\n", "**", "\\", "<", "@", "​"])
def test_empty_and_degenerate_titles_still_render_a_bulleted_link(title):
    embed = render_headlines_embed(BL4, [item(title, url="https://example.com/x")])
    assert embed is not None
    assert str(embed.description).endswith("<https://example.com/x>")


# --- hostile URLs ---

_BAD_URLS = [
    "javascript:alert(1)",
    "data:text/html,<script>",
    "ftp://example.com/file",
    "file:///etc/passwd",
    "//example.com/x",
    "https://user:pass@example.com/",
    "https://",
    "",
    "not a url",
    "https://example.com:notaport/",
]


@pytest.mark.parametrize("url", _BAD_URLS)
def test_an_unusable_url_drops_the_item_and_it_is_not_counted(url):
    good = [item(f"good {n}", age_h=2 + n) for n in range(3)]
    embed = render_headlines_embed(BL4, [item("bad", url=url, age_h=0.5), *good])
    assert shown_count(embed) == 3 and more_count(embed) == 0
    assert "bad" not in str(embed.description)


def test_only_unusable_urls_means_no_embed_at_all():
    assert render_headlines_embed(BL4, [item("a", url="javascript:1"), item("b", url="")]) is None


_TRICKY_URLS = [
    "https://example.com/a>[x](https://evil.example)",
    "https://example.com/a b c",
    "https://example.com/a\nb",
    "https://example.com/a`b`",
    "https://example.com/a\"b'c",
    "https://example.com/a]b[c",
    "https://example.com/‮evil",
    "https://example.com/?q=<script>&r=>",
    "https://example.com/" + "%3E" * 50,
]


@pytest.mark.parametrize("url", _TRICKY_URLS)
def test_a_tricky_url_is_either_dropped_or_safely_wrapped(url):
    embed = render_headlines_embed(BL4, [item("title", url=url)])
    if embed is None:
        return
    [line] = body(embed)
    link = line.rpartition(" — ")[2]
    assert link.startswith("<https://") and link.endswith(">")
    inner = link[1:-1]
    assert not re.search(r"[\s<>\[\]`\"]", inner)
    assert "‮" not in inner


# --- order and markers (D4) ---


def test_equal_sort_keys_keep_their_input_order():
    same = [item(f"same {n}", age_h=3.0, url=f"https://example.com/s{n}") for n in range(5)]
    lines = body(render_headlines_embed(BL4, same))
    assert [ln.split(" — ")[0] for ln in lines] == [f"• same {n}" for n in range(5)]


def test_the_order_is_a_total_preorder_over_every_trust_certainty_age_combination():
    combos = [
        item(f"{trust}-{int(unc)}-{age}", trust=trust, uncertain=unc, age_h=age)
        for trust in ("community", "press", "official")
        for unc in (True, False)
        for age in (5.0, 1.0, 3.0)
    ]
    shown = [
        ln.split(" — ")[0].replace("• 🟢 OFFICIAL · ", "• ")
        for ln in body(render_headlines_embed(BL4, combos))
    ]
    expected = [f"• {i.title}" for i in sorted(combos, key=_headline_sort_key)]
    assert shown == expected
    assert shown[0].startswith("• official-0-1.0") and shown[-1].startswith("• community-1-5.0")


def test_official_marker_only_on_official_items_and_never_twice():
    embed = render_headlines_embed(
        BL4, [item("a", trust="official"), item("b", trust="press"), item("c", trust="community")]
    )
    assert str(embed.description).count("🟢 OFFICIAL") == 1
    assert body(embed)[0].startswith("• 🟢 OFFICIAL · a")


def test_a_published_date_far_in_the_future_outranks_fresh_items_within_its_trust_tier():
    # Documents current behavior, not a verdict: a feed with a wrong (or hostile) date
    # pins its item to the top of its own trust tier until the date passes. It can't
    # outrank a more trusted tier.
    future = item("future", published_at=T0 + timedelta(days=3650))
    fresh = item("fresh", age_h=0.01)
    official = item("official", trust="official", age_h=30)
    titles = [
        ln.split(" — ")[0] for ln in body(render_headlines_embed(BL4, [fresh, future, official]))
    ]
    assert [t.replace("🟢 OFFICIAL · ", "") for t in titles] == [
        "• official",
        "• future",
        "• fresh",
    ]


@pytest.mark.parametrize(
    "when",
    [
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(9999, 12, 31, 23, 59, tzinfo=UTC),
        datetime(2026, 9, 30, 12, 0),  # naive
    ],
)
def test_extreme_or_naive_published_dates_do_not_crash_the_sort(when):
    embed = render_headlines_embed(BL4, [item("x", published_at=when), item("y")])
    assert shown_count(embed) == 2


# --- the limit, exactly ---


def _titled_with_total(total_units: int, *, astral: bool, n: int = 8):
    """`n` items whose description is exactly `total_units` UTF-16 units once rendered."""
    base = [item(f"headline {i:03d} " + "w" * 120, age_h=2.0 + i) for i in range(n - 1)]

    def make(pad: int):
        if astral:
            last = "😀" * (pad // 2) + "p" * (pad % 2)
        else:
            last = "p" * pad
        return [*base, item("last " + last, age_h=50.0, url="https://example.com/last")]

    def size(pad: int) -> int:
        return discord_len(str(render_headlines_embed(BL4, make(pad)).description))

    pad = total_units - size(0)
    assert pad >= 0, "fixture too big"
    items = make(pad)
    return items


@pytest.mark.parametrize("astral", [False, True])
def test_a_description_of_exactly_4096_units_is_not_cut_and_4097_cuts_exactly_one(astral):
    # Build at a size where nothing is shed, so `size` is the true unshed length.
    items = _titled_with_total(LIMIT, astral=astral, n=8)
    assert len(items) == 8
    fits = render_headlines_embed(BL4, items)
    assert discord_len(str(fits.description)) == LIMIT
    assert more_count(fits) == 0 and shown_count(fits) == 8

    over = _titled_with_total(LIMIT, astral=astral, n=8)
    over[-1] = item(over[-1].title + "p", age_h=50.0, url="https://example.com/last")
    cut = render_headlines_embed(BL4, over)
    assert discord_len(str(cut.description)) <= LIMIT
    assert more_count(cut) == 1 and shown_count(cut) == 7
    assert str(cut.description).endswith("\n\n+1 more, use /news")


def test_astral_text_is_measured_in_utf16_units_not_codepoints():
    embed = render_headlines_embed(BL4, [item("😀" * 1000), item("tail", age_h=9)])
    text = str(embed.description)
    assert discord_len(text) <= LIMIT
    assert len(text) < discord_len(text)  # the emoji really did count double
    text.encode("utf-8")  # and nothing was cut through a surrogate pair


def test_a_game_name_of_astral_characters_is_truncated_to_the_title_limit_in_units():
    game = GameCfg(key="x", name="😀" * 400)
    embed = render_headlines_embed(game, [item("a")])
    assert discord_len(str(embed.title)) <= 256
    str(embed.title).encode("utf-8")


def test_plus_n_more_is_exactly_the_number_of_usable_items_not_shown():
    usable = [item(f"usable {n:04d} " + "z" * 40, age_h=1.0 + n / 100) for n in range(300)]
    junk = [item(f"junk {n}", url="javascript:1", age_h=0.001 * (n + 1)) for n in range(37)]
    embed = render_headlines_embed(BL4, [*junk, *usable])
    assert shown_count(embed) + more_count(embed) == 300
    assert more_count(embed) > 0
    assert discord_len(str(embed.description)) <= LIMIT


def test_shedding_keeps_the_most_important_lines_and_cuts_from_the_bottom():
    items = [
        *[item(f"press {n:03d} " + "q" * 60, trust="press", age_h=1 + n) for n in range(60)],
        *[item(f"rumor {n:03d} " + "q" * 60, trust="community", age_h=1 + n) for n in range(60)],
        item("the official one " + "q" * 60, trust="official", age_h=500),
    ]
    embed = render_headlines_embed(BL4, items)
    lines = body(embed)
    assert lines[0].startswith("• 🟢 OFFICIAL · the official one")
    assert not any(line.startswith("• rumor") for line in lines)  # community went first
    ranked = [ln for ln in lines if ln.startswith("• press")]
    assert [ln.split(" — ")[0] for ln in ranked] == [
        f"• press {n:03d} " + "q" * 60 for n in range(len(ranked))
    ]


def test_a_giant_note_cannot_push_the_description_over_the_limit():
    items = [item(f"h{n}", age_h=1 + n) for n in range(50)]
    for note in ("n" * 5000, "😀" * 3000, "short note"):
        embed = render_headlines_embed(BL4, items, note)
        assert discord_len(str(embed.description)) <= LIMIT


def test_a_long_note_with_many_items_keeps_the_note_and_counts_more_against_items_only():
    items = [item(f"h{n:03d} " + "x" * 80, age_h=1 + n) for n in range(200)]
    embed = render_headlines_embed(BL4, items, "Summary unavailable; showing headlines.")
    assert body(embed)[0] == "Summary unavailable; showing headlines."
    assert shown_count(embed) + more_count(embed) == 200


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Shedding walks down from 'keep every line' and stops at the first prefix that fits. "
        "If the very first line is longer than the whole embed (a giant title or URL on the "
        "newest top-tier item) no prefix fits, and the fallback hard-truncates that one line "
        "and drops every other headline without a '+N more'. One oversized item swallows "
        "the game's whole digest."
    ),
)
@pytest.mark.parametrize("giant", ["title", "url"])
def test_one_oversized_item_does_not_swallow_the_rest_of_the_list(giant):
    if giant == "title":
        first = item("g" * 5000, age_h=0.1)
    else:
        first = item("giant", url="https://example.com/" + "x" * 5000, age_h=0.1)
    rest = [item(f"normal {n}", age_h=2 + n) for n in range(10)]
    embed = render_headlines_embed(BL4, [first, *rest])
    assert "normal 0" in str(embed.description)


def test_embed_total_stays_under_discords_6000_even_with_a_full_description_and_footer():
    items = [item(f"h{n:03d} " + "x" * 80, age_h=1 + n) for n in range(300)]
    games = [GameCfg(key="borderlands4", name="B" * 300)]
    digest = render_guild_digest(
        date(2026, 9, 30),
        games,
        {"borderlands4": 1},
        stories_by_game={},
        items_by_game={"borderlands4": items},
        notes_by_game={"borderlands4": "n" * 2000},
        coverage_notes=["c" * 2000],
    )
    [message] = digest.messages
    embed = message.embed
    assert len(embed) <= 6000
    assert discord_len(str(embed.title)) <= 256 and discord_len(str(embed.description)) <= LIMIT


# --- render_guild_digest ---


def story(headline="Big patch"):
    return StoryDraft(headline, "It fixes things.", "official", ["https://example.com/s"], None)


def render(games, channels=None, **kwargs):
    kwargs.setdefault("stories_by_game", {})
    kwargs.setdefault("items_by_game", {})
    kwargs.setdefault("notes_by_game", {})
    kwargs.setdefault("coverage_notes", [])
    return render_guild_digest(
        date(2026, 9, 30),
        games,
        {"borderlands4": 11, "palworld": 12} if channels is None else channels,
        **kwargs,
    )


def test_a_summary_for_a_game_beats_its_headlines_and_an_empty_one_beats_them_too():
    items = {"borderlands4": [item("free headline")], "palworld": [item("other headline")]}
    digest = render(
        [BL4, PAL],
        stories_by_game={"borderlands4": [story()], "palworld": []},
        items_by_game=items,
    )
    assert [m.topic_key for m in digest.messages] == ["borderlands4"]
    assert "free headline" not in str(digest.messages[0].embed.description)


def test_a_key_in_the_summaries_that_isnt_a_followed_game_is_ignored():
    digest = render([BL4], stories_by_game={"rust": [story()]}, items_by_game={"rust": [item("x")]})
    assert digest.messages == []


def test_no_games_renders_an_empty_digest_not_an_error():
    digest = render([])
    assert digest.messages == [] and digest.run_date == date(2026, 9, 30)


def test_a_footer_only_lands_on_embeds_that_actually_post_and_is_defused():
    digest = render(
        [BL4, PAL],
        items_by_game={"borderlands4": [item("x")]},
        coverage_notes=["@everyone <@1> [a](https://evil.example) https://evil.example"],
    )
    [message] = digest.messages
    footer = message.embed.footer.text
    assert footer.startswith("Reduced coverage today: ")
    assert "@everyone" not in footer.replace("@​everyone", "")
    assert "https://" not in footer


def test_the_same_game_twice_in_games_posts_twice_not_once_hidden():
    # A caller bug, not a feature: pin that the renderer doesn't dedupe behind its back.
    digest = render([BL4, BL4], items_by_game={"borderlands4": [item("x")]})
    assert [m.topic_key for m in digest.messages] == ["borderlands4", "borderlands4"]


def test_a_game_without_a_channel_entry_is_a_key_error_not_a_silent_wrong_channel():
    with pytest.raises(KeyError):
        render([BL4], channels={}, items_by_game={"borderlands4": [item("x")]})


# --- the per-guild run report ---


def report(**overrides) -> str:
    args = dict(
        status="ok",
        run_date=date(2026, 9, 30),
        run_kind="scheduled",
        games=[BL4, PAL],
        counts={"borderlands4": 3, "palworld": 2},
        channels={"borderlands4": 11, "palworld": 12},
        posted_by_game={"borderlands4": 777, "palworld": 778},
        skipped={},
        duration=timedelta(seconds=12),
        notes=[],
        guild_id=42,
    )
    args.update(overrides)
    return render_guild_run_report(**args)


def test_every_jump_link_points_at_this_guild_and_no_other_id_appears():
    text = report(guild_id=4242)
    assert set(re.findall(r"discord\.com/channels/(\d+)/", text)) == {"4242"}


@pytest.mark.parametrize("seconds", [-5, -1e6, 0, 0.4, 59.5, 3599, 10**7])
def test_the_duration_never_crashes_and_never_goes_negative(seconds):
    text = report(duration=timedelta(seconds=seconds))
    last = text.splitlines()[-1]
    assert re.fullmatch(r"Took (\d+s|\d+m\d\ds)", last)


def test_a_clock_that_stepped_backwards_reads_as_zero_seconds():
    assert report(duration=timedelta(seconds=-300)).endswith("Took 0s")


def test_a_hostile_skip_reason_and_note_cannot_ping_or_link():
    text = report(
        status="partial",
        skipped={"palworld": "@everyone <@123456789012345678> [x](https://evil.example)"},
        notes=["@here <#123456789012345678> https://evil.example"],
    )
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "@here" not in text.replace("@​here", "")
    assert not re.search(r"<@[!&]?\d{17,20}>|<#\d+>", text)
    assert "https://evil" not in text and "](https://evil" not in text


def test_a_partial_report_with_no_notes_has_no_notes_line_and_ok_never_has_one():
    assert "Notes:" not in report(status="partial", notes=[])
    assert "Notes:" not in report(status="ok", notes=["ignored on ok"])


def test_a_skipped_key_the_catalog_no_longer_has_still_renders():
    text = report(status="partial", skipped={"gone-game": "removed"})
    assert "Skipped: gone-game (removed)" in text


def test_ten_followed_games_with_long_names_fit_the_message_limit():
    games = [
        GameCfg(key=f"g{n}", name=f"Extremely Long Game Name Number {n} " * 3) for n in range(10)
    ]
    text = report(
        games=games,
        counts={g.key: n for n, g in enumerate(games)},
        channels={g.key: 100 + n for n, g in enumerate(games)},
        posted_by_game={g.key: 900 + n for n, g in enumerate(games)},
        skipped={g.key: "r" * 300 for g in games[:5]},
        status="partial",
        notes=["n" * 400],
    )
    assert discord_len(text) <= 2000
    assert text.startswith("⚠️ **Digest posted with gaps**")


def test_the_run_kind_label_is_shown_verbatim_for_all_three_kinds():
    for kind in ("scheduled", "catch-up", "run-now"):
        assert f"({kind})" in report(run_kind=kind)


def test_a_game_with_a_posted_id_but_no_count_reports_zero_not_a_crash():
    text = report(counts={})
    assert "0 items" in text
