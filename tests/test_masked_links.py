"""Masked links on the story and headline surfaces (v3.4.0).

A story's source links and a headline's link used to be bare `<url>`; they are now
`[domain](<url>)`, so a phone shows the site's name and not forty characters of path.
This file pins the label rules (host only, `www.` gone, punycode for look-alikes, a
plain `link` for anything the bot can't vouch for), the new form on every renderer
that draws one, the length bookkeeping, and the promise that nothing else changed:
SHiFT alerts, follow-ups, roundups, the run report and the lounge quote are compared
against what the code printed before this change, character for character.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.bot import format as fmt
from newsbot.bot.format import (
    _link_label,
    _link_line,
    _truncate_description,
    discord_len,
    render_capped_headlines_embed,
    render_code_alerts,
    render_followup_alerts,
    render_guild_digest,
    render_guild_run_report,
    render_headlines_embed,
    render_item_page,
    render_picked_headlines_embed,
    render_roundup_alerts,
    render_story_page,
)
from newsbot.config import GameCfg
from newsbot.lounge.quotes import Quote, render_quote_message
from newsbot.pipeline.summarize import StoryDraft
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import HeadlineItem, ItemView, StoryView

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
RUN_DATE = date(2026, 10, 29)
PAL = GameCfg(key="palworld", name="Palworld")
IND = GameCfg(key="industry", name="Gaming industry")

# A complete masked link as the bot writes one, and the two ways a cut leaves half of one.
_WHOLE_LINK_RE = re.compile(r"\[[a-z0-9.\-]+(?: \(\d+\))?\]\(<[^>\s]+>\)")


def assert_no_half_link(text: str) -> None:
    rest = _WHOLE_LINK_RE.sub("", text)
    assert "](<" not in rest, text[-120:]
    assert "<http" not in rest, text[-120:]
    assert not re.search(r"\[[a-z0-9.\- ()]*$", rest), text[-120:]


def item(title="A headline", *, url="https://www.pcgamer.com/games/", trust="press"):
    return HeadlineItem(
        url=url,
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=T0,
        collected_at=T0,
        uncertain=False,
    )


def draft(urls, headline="Headline", summary="Summary"):
    return StoryDraft(
        headline=headline,
        summary=summary,
        label="reported",
        item_urls=list(urls),
        update_of_story_id=None,
    )


def story_view(urls):
    return StoryView(
        id=1,
        topic_key="palworld",
        headline="Headline",
        summary="Summary",
        label="reported",
        created_at=T0,
        urls=list(urls),
        is_update_of=None,
    )


def item_view(url, title="A headline"):
    return ItemView(
        id=1,
        url=url,
        title=title,
        excerpt="",
        source_name="Feed",
        trust="press",
        published_at=T0,
        collected_at=T0,
        topic_keys=["palworld"],
    )


# --- the label ---


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://www.pcgamer.com/games/", "pcgamer.com"),
        ("https://PCGamer.com/games/", "pcgamer.com"),
        ("https://store.steampowered.com/app/1", "store.steampowered.com"),
        ("https://example.com:8443/x", "example.com"),
        ("https://user:secret@example.com/x", "example.com"),
        ("https://www.example.com.:8080/x", "example.com"),
        # A look-alike host shows as its xn-- form, never as the letters it imitates.
        ("https://bücher.de/x", "xn--bcher-kva.de"),
        ("https://аpple.com/x", "xn--pple-43d.com"),
        ("http://203.0.113.9/x", "203.0.113.9"),
    ],
)
def test_label_is_the_plain_host(url, label):
    assert _link_label(url) == label


@pytest.mark.parametrize(
    "url",
    [
        "https://[2001:db8::1]/x",
        "https://foo_bar.example.com/x",
        "https://" + "a" * 50 + ".example.com/x",
        "https:///x",
        "https://",
        "not a url",
        "https://bad..host.com/x",
    ],
)
def test_anything_the_bot_cant_vouch_for_is_labelled_link(url):
    assert _link_label(url) == "link"


@pytest.mark.parametrize("host", ["-a.com", "a-.com", "a.com-", ".a.com", "-", "a.-.com"])
def test_a_label_must_start_and_end_with_a_letter_or_digit(host):
    assert _link_label(f"https://{host}/x") == "link"


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://faß.de/x", "xn--fa-hia.de"),
        ("https://ß.de/x", "xn--zca.de"),
        ("https://ς.com/x", "xn--3xa.com"),
        ("https://a\u200db.com/x", "link"),
        ("https://a\u200cb.com/x", "link"),
    ],
)
def test_the_label_is_the_host_the_connection_uses_not_its_idna_2003_cousin(url, label):
    assert _link_label(url) == label


@pytest.mark.parametrize("host", ["exa mple.com", "a>b.com", "a >b.com"])
def test_a_url_with_whitespace_or_a_closing_bracket_is_not_shown_on_these_lines(host):
    odd = f"https://{host}/x"
    assert _link_line([odd]) == ""
    # It takes neither a slot nor a number, and it isn't counted in "+N more".
    good = [f"https://site{i}.example.com/p" for i in range(4)]
    line = _link_line([odd, *good])
    assert line.count("](<") == 3 and line.endswith(" · +1 more")
    assert fmt._headline_line(item(url=odd)) is None
    story = draft([odd, *good[:3]])
    assert fmt._fitting_story_block(story) == fmt._story_block(replace(story, item_urls=good[:3]))


def test_a_cut_just_before_a_link_or_inside_a_separator_leaves_no_separator():
    text = "Head\nSumm\n[a.com](<https://a.com/x>) · [b.com](<https://b.com/yyyyyyy>) · +2 more"
    for limit in range(1, discord_len(text)):
        assert not re.search(r"[\u00b7\u2014]\s*…$", _truncate_description(text, limit)), limit
    headline = "• A title \u2014 [a.com](<https://a.com/x>)"
    for limit in range(1, discord_len(headline)):
        assert not re.search(r"[\u00b7\u2014]\s*…$", _truncate_description(headline, limit)), limit


def test_the_cut_sees_any_masked_looking_link_up_to_its_closing_bracket():
    text = "Head\n[link](<https://exa mple.com/x>) tail"
    start = text.index("[link]")
    for limit in range(1, discord_len(text)):
        body = _truncate_description(text, limit)[:-1]
        assert not (start < len(body) < text.index(") tail") + 1), limit


def test_a_label_of_exactly_forty_characters_is_kept():
    host = "a" * 31 + ".example.com"
    assert len(host) == 43
    host = "a" * 28 + ".example.com"
    assert len(host) == 40
    assert _link_label(f"https://{host}/") == host
    assert _link_label(f"https://a{host}/") == "link"


# --- story link lines ---


def test_link_line_uses_domain_labels_and_keeps_the_separator():
    line = _link_line(
        [
            "https://www.pcgamer.com/games/",
            "https://store.steampowered.com/app/1",
        ]
    )
    assert line == (
        "[pcgamer.com](<https://pcgamer.com/games>) · "
        "[store.steampowered.com](<https://store.steampowered.com/app/1>)"
    )


def test_link_line_url_is_the_one_shown_today():
    url = "https://www.pcgamer.com/games/?utm_source=x#frag"
    assert _link_line([url]) == "[pcgamer.com](<https://pcgamer.com/games>)"


def test_repeated_labels_are_numbered_on_one_line():
    line = _link_line(
        [
            "https://pcgamer.com/a",
            "https://pcgamer.com/b",
            "https://pcgamer.com/c",
        ]
    )
    assert line == (
        "[pcgamer.com](<https://pcgamer.com/a>) · "
        "[pcgamer.com (2)](<https://pcgamer.com/b>) · "
        "[pcgamer.com (3)](<https://pcgamer.com/c>)"
    )


def test_the_fallback_label_is_numbered_too():
    line = _link_line(["https://[2001:db8::1]/a", "https://[2001:db8::2]/b"])
    assert line.startswith("[link](<https://[2001:db8::1]/a>) · [link (2)](<")


def test_numbering_does_not_leak_between_lines():
    first = _link_line(["https://pcgamer.com/a", "https://pcgamer.com/b"])
    second = _link_line(["https://pcgamer.com/c"])
    assert "(2)" in first
    assert second == "[pcgamer.com](<https://pcgamer.com/c>)"


def test_at_most_three_links_then_plus_n_more():
    urls = [f"https://site{i}.example.com/p" for i in range(5)]
    line = _link_line(urls)
    assert line == (
        "[site0.example.com](<https://site0.example.com/p>) · "
        "[site1.example.com](<https://site1.example.com/p>) · "
        "[site2.example.com](<https://site2.example.com/p>) · +2 more"
    )


def test_an_invite_url_is_still_dropped_and_not_counted():
    line = _link_line(
        [
            "https://discord.gg/abcdef",
            "https://pcgamer.com/a",
            "https://discord.com/invite/xyz",
        ]
    )
    assert line == "[pcgamer.com](<https://pcgamer.com/a>)"


def test_a_story_with_no_usable_link_has_an_empty_line():
    assert _link_line(["https://discord.gg/abcdef", "javascript:alert(1)"]) == ""


def test_comped_digest_story_block_uses_masked_links():
    story = draft(["https://www.pcgamer.com/games/", "https://pcgamer.com/more"])
    rendered = render_guild_digest(
        RUN_DATE,
        [PAL],
        {"palworld": 5},
        stories_by_game={"palworld": [story]},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    text = str(rendered.messages[0].embed.description)
    assert text == (
        "🟡 REPORTED · Headline\nSummary\n"
        "[pcgamer.com](<https://pcgamer.com/games>) · [pcgamer.com (2)](<https://pcgamer.com/more>)"
    )


def test_comped_industry_story_uses_masked_links():
    rendered = render_guild_digest(
        RUN_DATE,
        [IND],
        {"industry": 5},
        stories_by_game={"industry": [draft(["https://www.gamesindustry.biz/a"])]},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
        capped={"industry": 5},
    )
    assert str(rendered.messages[0].embed.description).endswith(
        "[gamesindustry.biz](<https://gamesindustry.biz/a>)"
    )


def test_story_page_uses_masked_links():
    embed = render_story_page(
        [story_view(["https://www.pcgamer.com/games/", "https://store.steampowered.com/app/1"])],
        {"palworld": PAL},
        "/news recent",
        1,
        1,
    )
    assert str(embed.description).endswith(
        "[pcgamer.com](<https://pcgamer.com/games>) · "
        "[store.steampowered.com](<https://store.steampowered.com/app/1>)"
    )


# --- headline lines ---


def test_headline_line_title_is_plain_and_link_is_masked():
    line = fmt._headline_line(item("Palworld update 2"))
    assert line == "• Palworld update 2 — [pcgamer.com](<https://pcgamer.com/games>)"


def test_official_marker_stays_outside_the_link():
    line = fmt._headline_line(item("Patch notes", trust="official"))
    assert line == "• 🟢 OFFICIAL · Patch notes — [pcgamer.com](<https://pcgamer.com/games>)"


def test_headline_title_with_brackets_cannot_reach_the_label():
    line = fmt._headline_line(item("[hi](<https://evil.example/>)"))
    assert line is not None
    assert line.endswith(" — [pcgamer.com](<https://pcgamer.com/games>)")
    assert line.startswith("• \\[hi\\]")


def test_headline_with_an_invite_url_is_dropped():
    assert fmt._headline_line(item(url="https://discord.gg/abcdef")) is None


def test_headline_with_an_overlong_url_is_still_dropped():
    long_url = "https://pcgamer.com/" + "a" * 1000
    assert fmt._headline_line(item(url=long_url)) is None
    assert fmt._headline_line(item(url="https://pcgamer.com/" + "a" * 900)) is not None


def test_free_digest_headlines_embed_is_masked():
    embed = render_headlines_embed(PAL, [item("One"), item("Two", url="https://ign.com/x")])
    assert embed is not None
    assert str(embed.description).split("\n") == [
        "• One — [pcgamer.com](<https://pcgamer.com/games>)",
        "• Two — [ign.com](<https://ign.com/x>)",
    ]


def test_free_headline_embed_with_a_note_on_top():
    embed = render_headlines_embed(PAL, [item("One")], note="Summary unavailable")
    assert embed is not None
    assert str(embed.description).split("\n")[0] == "Summary unavailable"
    assert "[pcgamer.com](<https://pcgamer.com/games>)" in str(embed.description)


def test_industry_picked_and_capped_headlines_are_masked():
    picked = render_picked_headlines_embed(IND, [item("One")])
    capped = render_capped_headlines_embed(IND, [item("One")], None, 5)
    for embed in (picked, capped):
        assert embed is not None
        assert str(embed.description) == "• One — [pcgamer.com](<https://pcgamer.com/games>)"


def test_item_page_is_masked():
    embed = render_item_page(
        [item_view("https://www.pcgamer.com/games/")], {"palworld": "Palworld"}, "/news", 1, 1
    )
    assert str(embed.description) == (
        "**Palworld**\n• A headline — [pcgamer.com](<https://pcgamer.com/games>)"
    )


# --- length ---


def _many_items(n):
    return [
        item(f"Headline number {i:04d} " + "x" * 80, url=f"https://s{i}.example.com/p/{i}")
        for i in range(n)
    ]


def test_a_long_free_list_sheds_whole_lines_with_the_right_count():
    items = _many_items(120)
    embed = render_headlines_embed(PAL, items)
    assert embed is not None
    text = str(embed.description)
    assert discord_len(text) <= 4096
    shown_lines = [ln for ln in text.split("\n") if ln.startswith("• ")]
    cut = 120 - len(shown_lines)
    assert cut > 0
    assert text.endswith(f"\n\n+{cut} more, use /news")
    for ln in shown_lines:
        assert_no_half_link(ln)
        assert ln.endswith(")")
    # The kept lines are the first ones, in order.
    assert [ln.split(" ")[3] for ln in shown_lines] == [f"{i:04d}" for i in range(len(shown_lines))]


def test_a_long_comped_digest_sheds_whole_stories_with_the_right_count():
    stories = [
        draft(
            [f"https://site{i}-{j}.example.com/{'p' * 60}" for j in range(3)],
            headline=f"Headline {i}",
            summary="s" * 380,
        )
        for i in range(14)
    ]
    rendered = render_guild_digest(
        RUN_DATE,
        [PAL],
        {"palworld": 5},
        stories_by_game={"palworld": stories},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    text = str(rendered.messages[0].embed.description)
    assert discord_len(text) <= 4096
    kept = text.count("REPORTED ·")
    assert 0 < kept < 14
    assert text.endswith(f"+{14 - kept} more, use /news")
    assert_no_half_link(text)


def test_fitting_story_block_sheds_whole_links_from_the_end():
    urls = [f"https://site{j}.example.com/{'p' * 900}" for j in range(3)]
    story = draft(urls, summary="s" * 400)
    block = fmt._fitting_story_block(story)
    assert block is not None
    assert discord_len(block) <= 4096
    assert_no_half_link(block)
    # Three links of this size do fit; push them until one has to go.
    big = draft([f"https://site{j}.example.com/{'p' * 1900}" for j in range(3)], summary="s" * 400)
    block = fmt._fitting_story_block(big)
    assert block is not None
    assert discord_len(block) <= 4096
    assert block.count("](<") < 3
    assert_no_half_link(block)


def test_the_label_counts_toward_the_block_length():
    story = draft(["https://pcgamer.com/a"])
    block = fmt._story_block(story)
    assert discord_len(block) == discord_len("🟡 REPORTED · Headline\nSummary\n") + len(
        "[pcgamer.com](<https://pcgamer.com/a>)"
    )


def test_industry_post_sheds_whole_stories_and_never_a_half_link():
    stories = [
        draft(
            [f"https://site{i}-{j}.example.com/{'p' * 1200}" for j in range(3)], summary="s" * 380
        )
        for i in range(5)
    ]
    rendered = render_guild_digest(
        RUN_DATE,
        [IND],
        {"industry": 5},
        stories_by_game={"industry": stories},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
        capped={"industry": 5},
    )
    text = str(rendered.messages[0].embed.description)
    assert discord_len(text) <= 4096
    assert_no_half_link(text)


# --- no cut leaves half a link ---


def _linky_text():
    blocks = []
    for i in range(6):
        blocks.append(
            fmt._story_block(
                draft(
                    [
                        f"https://site{i}.example.com/{'p' * 40}",
                        "https://pcgamer.com/a",
                        "https://pcgamer.com/b",
                    ],
                    headline=f"Headline {i}",
                    summary="Summary text " * 5,
                )
            )
        )
    return "\n\n".join(blocks)


def test_truncate_description_never_leaves_half_a_link_at_any_limit():
    text = _linky_text()
    for limit in range(1, discord_len(text) + 1):
        cut = _truncate_description(text, limit)
        assert discord_len(cut) <= limit
        assert_no_half_link(cut)


def test_truncate_description_leaves_text_that_fits_alone():
    text = _linky_text()
    assert _truncate_description(text, discord_len(text)) == text


def test_truncate_description_backs_up_to_before_the_bracket():
    text = "Headline\nSummary\n[pcgamer.com](<https://pcgamer.com/aaaaaaaaaa>)"
    cut = _truncate_description(text, discord_len(text) - 5)
    assert cut == "Headline\nSummary…"


def test_truncate_description_drops_a_dangling_separator():
    text = "Headline\nSummary\n[a.com](<https://a.com/x>) · [b.com](<https://b.com/yyyyyyyy>)"
    cut = _truncate_description(text, discord_len(text) - 5)
    assert cut == "Headline\nSummary\n[a.com](<https://a.com/x>)…"


def test_truncate_description_ignores_look_alike_text_in_a_headline():
    # esc() puts a backslash in front of the "[", so this is text and not a link.
    text = "\\[x\\](<https://e.com/>) " + "word " * 30
    cut = _truncate_description(text, 60)
    assert cut.startswith("\\[x\\](<https:")


def test_story_page_with_many_long_stories_cuts_cleanly():
    stories = [
        story_view([f"https://site{i}-{j}.example.com/{'p' * 150}" for j in range(3)])
        for i in range(12)
    ]
    embed = render_story_page(stories, {"palworld": PAL}, "/news recent", 1, 1)
    text = str(embed.description)
    assert discord_len(text) <= 4096
    assert_no_half_link(text)


def test_item_page_with_many_long_lines_cuts_cleanly():
    items = [
        item_view(f"https://site{i}.example.com/{'p' * 800}", title="t" * 280) for i in range(10)
    ]
    embed = render_item_page(items, {"palworld": "Palworld"}, "/news", 1, 1)
    text = str(embed.description)
    assert discord_len(text) <= 4096
    assert_no_half_link(text)


def test_one_enormous_story_in_a_comped_digest_cuts_cleanly():
    story = draft(
        [f"https://site{j}.example.com/{'p' * 3000}" for j in range(3)], summary="s" * 400
    )
    embed = fmt._topic_embed(PAL, [story])
    text = str(embed.description)
    assert discord_len(text) <= 4096
    assert_no_half_link(text)


def test_worst_case_headline_line_stays_under_the_documented_bound():
    host = "a" * 28 + ".example.com"
    url = f"https://{host}/" + "a" * (1000 - len(f"https://{host}/"))
    line = fmt._headline_line(item("*" * 300, url=url, trust="official"))
    assert line is not None
    assert f"[{host}](<{url}>)" in line
    assert 1600 < discord_len(line) < 1700


# --- scripts/link_counts.py still finds the URL inside a masked link ---


def test_link_counts_pattern_still_counts_masked_links():
    path = Path(__file__).parent.parent / "scripts" / "link_counts.py"
    spec = importlib.util.spec_from_file_location("link_counts", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    link_re = module._LINK_RE

    embed = render_headlines_embed(PAL, [item("One"), item("Two", url="https://ign.com/x")])
    assert embed is not None
    assert link_re.findall(str(embed.description)) == [
        "https://pcgamer.com/games",
        "https://ign.com/x",
    ]


# --- everything else is exactly as it was ---

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"


def _candidate(code=CODE_A, **kwargs):
    defaults = dict(
        code=code,
        golden=False,
        source_name="Gearbox",
        item_url="https://www.example.com/post?id=1",
        fresh=True,
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


def test_terminal_alert_is_unchanged():
    out = render_code_alerts(
        [_candidate(), _candidate(CODE_B, golden=True, source_name="Reddit")], ping=True
    )
    assert [m.content for m in out] == [
        "@everyone `> new SHiFT codes ▌`\n\n```\nAAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n```\n"
        "`src` Gearbox · <https://example.com/post?id=1>\n\n```\n"
        "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB\n```\n"
        "`src` Golden Key: Reddit · <https://example.com/post?id=1>"
    ]
    out = render_code_alerts([_candidate()], ping=False)
    assert [m.content for m in out] == [
        "`> new SHiFT code ▌`\n\n```\nAAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n```\n"
        "`src` Gearbox · <https://example.com/post?id=1>"
    ]


def test_classic_alert_block_is_unchanged():
    assert fmt._classic_alert_block(_candidate(), golden_prefix=True) == (
        "```\nAAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n```\nGearbox · <https://example.com/post?id=1>"
    )


def test_followup_is_unchanged():
    out = render_followup_alerts([CODE_A, CODE_B], ping_mention="@everyone")
    assert [m.content for m in out] == [
        "@everyone `> confirmed by a second source ▌`\n\n```\nAAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n```\n"
        "\n```\nBBBB2-BBBBB-BBBBB-BBBBB-BBBBB\n```"
    ]


def test_roundup_is_unchanged():
    out = render_roundup_alerts(
        [_candidate(roundup=True), _candidate(CODE_B, roundup=True)],
    )
    assert [m.content for m in out] == [
        "`> SHiFT roundup ▌` 2 codes from Gearbox · <https://example.com/post?id=1>\n\n"
        "```\nAAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n```\n\n```\nBBBB2-BBBBB-BBBBB-BBBBB-BBBBB\n```"
    ]


def test_lounge_wikiquote_line_is_unchanged():
    quote = Quote(text="Hello", attribution="Me", link="https://en.wikiquote.org/wiki/Oscar_Wilde")
    assert render_quote_message(quote) == (
        "🥃 **Today's pour**\nHello\n~ Me\n"
        "From Wikiquote: <https://en.wikiquote.org/wiki/Oscar_Wilde>"
    )


def test_run_report_jump_link_is_unchanged():
    report = render_guild_run_report(
        status="ok",
        run_date=RUN_DATE,
        run_kind="scheduled",
        games=[PAL],
        counts={"palworld": 3},
        channels={"palworld": 55},
        posted_by_game={"palworld": 77},
        skipped={},
        duration=timedelta(seconds=5),
        notes=[],
        guild_id=9,
    )
    assert report == (
        "✅ **Digest posted** · Thu Oct 29 (scheduled)\n"
        "3 items: Palworld 3 [jump](<https://discord.com/channels/9/55/77>)\nTook 5s"
    )
