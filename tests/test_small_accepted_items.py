"""Three small leftovers from the v3.3.1 security review, pinned.

1. A summary that starts with "2026. " would draw as a numbered list; the dot gets a backslash.
2. Look-alike label markers (a green check beside OFFICIAL, full-width letters, a bare
   "UPDATE:") are stripped from AI text and from feed titles the way the bot's own markers
   already were, and the bot's exact markers go wherever they sit in the line.
3. The owner's daily report gets a Claude spend line right under its first line.

Nothing here talks to Discord or the network. The one test that needs a bot builds it over a
scratch database and never starts it.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from newsbot.bot.client import NewsBot
from newsbot.bot.format import (
    DigestOutcome,
    _ai_line,
    _headline_line,
    discord_len,
    render_guild_digest,
    render_headlines_embed,
    render_owner_report,
    render_story_page,
)
from newsbot.config import GameCfg, Secrets, Topic, load_config
from newsbot.industry import INDUSTRY_KEY
from newsbot.lounge.quotes import Quote, render_quote_message
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store.db import connect, migrate
from newsbot.store.models import HeadlineItem, SourceHealthRow, StoryView

RUN_DATE = date(2026, 10, 25)
BL4 = GameCfg(key="borderlands4", name="Borderlands 4")
IND = GameCfg(key=INDUSTRY_KEY, name="Gaming industry")
CHANNELS = {"borderlands4": 22, INDUSTRY_KEY: 33}
URL = "https://example.com/a"


def _story(headline="Real headline", summary="Body.", label="reported"):
    return StoryDraft(
        headline=headline,
        summary=summary,
        label=label,
        item_urls=[URL],
        update_of_story_id=None,
    )


def _digest_text(story, game=BL4):
    rendered = render_guild_digest(
        RUN_DATE,
        [game],
        CHANNELS,
        stories_by_game={game.key: [story]},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    (message,) = rendered.messages
    return message.embed.description


def _page_text(story):
    view = StoryView(
        id=1,
        topic_key="borderlands4",
        headline=story.headline,
        summary=story.summary,
        label=story.label,
        created_at=datetime(2026, 10, 25, tzinfo=UTC),
        urls=story.item_urls,
        is_update_of=None,
    )
    topics = {"borderlands4": Topic(key="borderlands4", name="Borderlands 4", channel_id=1)}
    return render_story_page([view], topics, "Recent", 1, 1).description


RENDERERS = {
    "digest": _digest_text,
    "industry": lambda s: _digest_text(s, IND),
    "page": _page_text,
}


def _headline(title, trust="community"):
    now = datetime(2026, 10, 25, tzinfo=UTC)
    return HeadlineItem(
        url=URL,
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=now,
        collected_at=now,
        uncertain=False,
    )


# --- 1. a summary that starts with a number is not a list ---


@pytest.mark.parametrize("where", RENDERERS)
@pytest.mark.parametrize(
    ("summary", "shown"),
    [
        ("2026. The year Valve bought everything.", "2026\\. The year Valve bought everything."),
        ("1. Valve buys Nintendo", "1\\. Valve buys Nintendo"),
        ("2026.", "2026\\."),
        ("007. Bond", "007\\. Bond"),
        ("12. 34. x", "12\\. 34. x"),
        ("1.Valve buys Nintendo", "1.Valve buys Nintendo"),
        ("10) x", "10) x"),
        ("1.5 million players", "1.5 million players"),
        ("Valve 1. x", "Valve 1. x"),
        ("١. x", "١. x"),
        (".1. x", ".1. x"),
    ],
)
def test_a_summary_that_starts_like_a_list_item_gets_a_backslash(where, summary, shown):
    lines = RENDERERS[where](_story(summary=summary)).split("\n")
    assert len(lines) == 3
    assert lines[1] == shown


@pytest.mark.parametrize("where", RENDERERS)
def test_the_headline_line_needs_no_list_escape(where):
    # It starts with the label marker, so a number in the headline is mid-line.
    lines = RENDERERS[where](_story(headline="1. Valve buys Nintendo")).split("\n")
    assert lines[0].endswith(" · 1. Valve buys Nintendo")


def test_a_marker_in_front_of_a_number_is_stripped_and_the_number_is_then_escaped():
    lines = _digest_text(_story(summary="🟢 OFFICIAL · 2026. The year")).split("\n")
    assert lines[1] == "2026\\. The year"


def test_the_digest_still_fits_when_every_summary_starts_with_a_number():
    stories = [
        _story(headline=f"H{n}", summary="1. " + "word " * 78, label="reported") for n in range(30)
    ]
    rendered = render_guild_digest(
        RUN_DATE,
        [BL4],
        CHANNELS,
        stories_by_game={"borderlands4": stories},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    description = rendered.messages[0].embed.description
    assert discord_len(description) <= 4096
    assert "more, use /news" in description
    summaries = [ln for ln in description.split("\n") if ln.startswith("1")]
    assert summaries and all(ln.startswith("1\\. word") for ln in summaries)


def test_the_lounge_quote_escapes_a_list_start_on_every_line():
    message = render_quote_message(Quote("1. first\nsecond\n2. third\n 3. fourth"))
    assert message.split("\n")[1:] == ["1\\. first", "second", "2\\. third", " 3\\. fourth"]


def test_the_lounge_quote_leaves_other_numbers_alone():
    message = render_quote_message(Quote("1984 was a year, 1.5 of them\n10) x"))
    assert message.split("\n")[1:] == ["1984 was a year, 1.5 of them", "10) x"]


# --- 2. look-alike labels ---


@pytest.mark.parametrize(
    "text",
    [
        "✅ OFFICIAL · Valve buys Nintendo",
        "🔵 REPORTED: Valve buys Nintendo",
        "🟧 RUMOR - Valve buys Nintendo",
        "🔄 UPDATE | Valve buys Nintendo",
        "☑️ OFFICIAL \u2014 Valve buys Nintendo",
        "✔ UPDATE \u2013 Valve buys Nintendo",
        "⚪ OFFICIAL Valve buys Nintendo",
        "⬛ RUMOR Valve buys Nintendo",
        "🟩OFFICIAL Valve buys Nintendo",
        "🔂 UPDATE · Valve buys Nintendo",
        "🔃️ UPDATE: Valve buys Nintendo",
        "ＯＦＦＩＣＩＡＬ · Valve buys Nintendo",
        "𝐎𝐅𝐅𝐈𝐂𝐈𝐀𝐋: Valve buys Nintendo",
        "official · Valve buys Nintendo",
        "Official: Valve buys Nintendo",
        "Rumor | Valve buys Nintendo",
        "UPDATE - Valve buys Nintendo",
        "reported \u2014 Valve buys Nintendo",
        "Update : Valve buys Nintendo",
        "✅ ＯＦＦＩＣＩＡＬ Valve buys Nintendo",
        "✅ OFFICIAL · 🟢 OFFICIAL · UPDATE: Valve buys Nintendo",
        "​✅ OFFICIAL · Valve buys Nintendo",
        "‮🔴 RUMOR · ​OFFICIAL: Valve buys Nintendo",
        "🟢 OFFICIAL · Valve buys Nintendo",
        "🔁 Valve buys Nintendo",
        "🟡️ Valve buys Nintendo",
    ],
)
def test_a_leading_lookalike_label_is_stripped(text):
    assert _ai_line(text) == "Valve buys Nintendo"


@pytest.mark.parametrize(
    "text",
    [
        "Official patch notes are out.",
        "OFFICIAL patch notes are out.",
        "Update 1.2 adds a new boss.",
        "Rumor has it. Update: the patch is out.",
        "Reported by two outlets and then some",
        "UPDATES: arrive on Tuesday.",
        "✅ Patch is out.",
        "🟦 Patch is out.",
        "OFFICIAL-looking patch notes",
        "Official:the patch",
        "Update -5% off",
        "the official patch notes",
        "an OFFICIAL statement",
        "Valve buys Nintendo",
    ],
)
def test_ordinary_text_comes_out_as_one_line_leaves_it(text):
    assert _ai_line(text) == text


def test_a_look_alike_emoji_and_a_label_word_is_a_label_even_without_a_separator():
    assert _ai_line("✅ Official patch notes are out.") == "patch notes are out."


def test_text_that_does_not_match_is_collapsed_and_nothing_else():
    assert _ai_line("Valve  buys\n\nNintendo ·  today") == "Valve buys Nintendo · today"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Valve 🟢 OFFICIAL · buys Nintendo", "Valve buys Nintendo"),
        ("Valve 🟢 OFFICIAL buys Nintendo", "Valve buys Nintendo"),
        ("a 🟡 REPORTED b 🔴 RUMOR c 🔁 UPDATE d", "a b c d"),
        ("x 🟢️ OFFICIAL · y", "x y"),
        ("x 🟡️ REPORTED y", "x y"),
        ("Valve buys 🔴 RUMOR", "Valve buys"),
        ("Valve buys Nintendo 🔁 UPDATE ·", "Valve buys Nintendo"),
        ("x 🟢 OFFICIAL 🟢 OFFICIAL · y", "x y"),
        ("Valve buys 🟢 OFFICIAL ·Nintendo", "Valve buys Nintendo"),
        ("The 🟢 OFFICIAL tag is ours.", "The tag is ours."),
    ],
)
def test_the_bots_exact_markers_go_wherever_they_sit(text, expected):
    assert _ai_line(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "x ✅ OFFICIAL y",
        "x 🟢 RUMOR y",
        "x 🟢 official y",
        "x 🟢 OFFICIALLY y",
        "x 🟢OFFICIAL y",
        "x OFFICIAL · y",
    ],
)
def test_things_that_are_not_the_bots_exact_marker_stay_in_the_middle(text):
    assert _ai_line(text) == text


@pytest.mark.parametrize(
    "text",
    ["✅ OFFICIAL", "🟢 OFFICIAL", "🟢 OFFICIAL · 🔴 RUMOR", "OFFICIAL:", "🔁 UPDATE ·", "🟢"],
)
def test_a_line_that_is_only_labels_keeps_the_collapsed_original(text):
    assert _ai_line(text) == text


def test_a_label_that_mid_line_removal_uncovers_at_the_front_is_stripped_too():
    # The exact marker in the middle goes first, and that leaves "UPDATE :" at the front.
    assert _ai_line("UPDATE 🟢 OFFICIAL : Valve buys Nintendo") == "Valve buys Nintendo"


def test_a_huge_run_of_labels_is_handled_quickly():
    text = "✅ OFFICIAL · " * 20000 + "Valve buys Nintendo"
    assert _ai_line(text) == "Valve buys Nintendo"


def test_a_fold_that_changes_length_does_not_shift_the_cut():
    # "ﬃ" folds to three letters; the text after the label must come back whole, and a
    # label that contains one has to be cut at the right place.
    assert _ai_line("✅ OFFICIAL · ﬃ cial news") == "ﬃ cial news"
    assert _ai_line("Oﬃcial: Valve buys Nintendo") == "Valve buys Nintendo"
    assert _ai_line("✅ Oﬃcial ﬁx is out") == "ﬁx is out"
    assert _ai_line("Ｕｐｄａｔｅ: ﬁx is out") == "ﬁx is out"


@pytest.mark.parametrize(
    ("title", "trust", "shown"),
    [
        ("✅ OFFICIAL · free codes", "community", "• free codes \u2014 <" + URL + ">"),
        ("🟢 OFFICIAL · free codes", "community", "• free codes \u2014 <" + URL + ">"),
        ("ＯＦＦＩＣＩＡＬ: free codes", "community", "• free codes \u2014 <" + URL + ">"),
        ("free 🟢 OFFICIAL · codes", "community", "• free codes \u2014 <" + URL + ">"),
        ("🟢 OFFICIAL · free codes", "official", "• 🟢 OFFICIAL · free codes \u2014 <" + URL + ">"),
        ("🔵 REPORTED - free codes", "official", "• 🟢 OFFICIAL · free codes \u2014 <" + URL + ">"),
        ("Official patch notes", "community", "• Official patch notes \u2014 <" + URL + ">"),
        ("✅ Patch\n  notes", "community", "• ✅ Patch notes \u2014 <" + URL + ">"),
        ("✅ OFFICIAL", "community", "• ✅ OFFICIAL \u2014 <" + URL + ">"),
    ],
)
def test_a_feed_title_cannot_pose_as_the_official_marker(title, trust, shown):
    assert _headline_line(_headline(title, trust)) == shown


def test_a_stripped_title_is_cut_after_the_strip_not_before():
    title = "✅ OFFICIAL · " * 40 + "x" * 295
    line = _headline_line(_headline(title))
    assert line == "• " + "x" * 295 + " \u2014 <" + URL + ">"


def test_a_headline_list_does_not_show_a_community_post_as_official():
    embed = render_headlines_embed(
        BL4, [_headline("🟢 OFFICIAL · free codes"), _headline("☑️ UPDATE: more codes")]
    )
    assert embed is not None
    assert "OFFICIAL" not in embed.description and "UPDATE" not in embed.description


# --- 3. the spend line in the owner's report ---


def _src(name, fails, error="timeout"):
    return SourceHealthRow(name, None, datetime(2026, 9, 30, tzinfo=UTC), error, fails)


def _report(outcomes=(), failing=(), day=0.04, month=0.62, name="October"):
    return render_owner_report(
        list(outcomes), list(failing), spend_24h_usd=day, spend_month_usd=month, month_name=name
    )


def test_the_spend_line_is_the_second_line_with_the_documented_wording():
    lines = _report([DigestOutcome(True)], [_src("a", 2)]).splitlines()
    assert lines[0] == "newsbot daily: digests posted to 1 of 1 server"
    assert lines[1] == (
        "Claude spend: about $0.04 in the last 24 hours, $0.62 so far in October (estimate)"
    )
    assert lines[2] == "1 failing source:"


@pytest.mark.parametrize(
    ("day", "month", "expected"),
    [
        (0.0, 0.0, "$0.00 in the last 24 hours, $0.00 so far in October"),
        (0.004, 0.03, "under $0.01 in the last 24 hours, $0.03 so far in October"),
        (0.0099999, 0.0099999, "under $0.01 in the last 24 hours, under $0.01 so far in October"),
        (0.01, 0.01, "about $0.01 in the last 24 hours, $0.01 so far in October"),
        (0.0149, 12.346, "about $0.01 in the last 24 hours, $12.35 so far in October"),
        (0.0, 0.62, "$0.00 in the last 24 hours, $0.62 so far in October"),
        (1234.5, 1234.5, "about $1234.50 in the last 24 hours, $1234.50 so far in October"),
    ],
)
def test_spend_amounts(day, month, expected):
    line = _report(day=day, month=month).splitlines()[1]
    assert line == f"Claude spend: {expected} (estimate)"


def test_the_month_name_comes_from_the_caller():
    assert "so far in December (estimate)" in _report(name="December")


def test_the_spend_line_is_there_with_nothing_else_to_say():
    assert _report().splitlines() == [
        "newsbot daily: no server digests were due today",
        "Claude spend: about $0.04 in the last 24 hours, $0.62 so far in October (estimate)",
        "All sources are healthy.",
    ]


def test_the_cap_keeps_the_first_two_lines_of_a_pathological_report():
    failing = [_src("n" * 500 + str(i), 10**9, "e" * 500) for i in range(1000)]
    report = _report([DigestOutcome(True)] * 37 + [DigestOutcome(False, "x")], failing)
    lines = report.splitlines()
    assert discord_len(report) <= 2000
    assert lines[0] == "newsbot daily: digests posted to 37 of 38 servers; 1 failed (x)"
    assert lines[1] == (
        "Claude spend: about $0.04 in the last 24 hours, $0.62 so far in October (estimate)"
    )
    assert "1000 failing sources:" in lines[2]


# The bot over a scratch database, never started: only `_owner_report_sync` runs.

CONFIG = """\
home_guild_id: 100000000000000001
catalog:
  - key: palworld
    name: Palworld
"""
SECRETS = Secrets(
    discord_token=None,
    anthropic_api_key="k",
    brave_api_key=None,
    bluesky_handle=None,
    bluesky_app_password=None,
)


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _summary(conn, created_at, input_tokens, output_tokens):
    conn.execute(
        "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
        "input_tokens, output_tokens, created_at, items_after, items_upto) "
        "VALUES ('palworld', '2026-10-01', 'ok', ?, ?, ?, ?, ?, 0, 0)",
        (created_at, created_at, input_tokens, output_tokens, created_at),
    )


async def _report_at(tmp_path, db_path, now, timezone=None):
    path = tmp_path / "config.yaml"
    extra = f"owner_report:\n  timezone: {timezone}\n" if timezone else ""
    path.write_text(CONFIG + extra)
    bot = NewsBot(load_config(path), SECRETS, db_path)
    try:
        return await asyncio.to_thread(bot._owner_report_sync, now, "2026-11-01")
    finally:
        await bot.close()


def _spend_line(report):
    return report.splitlines()[1]


async def test_the_report_sums_the_last_day_and_the_local_month(tmp_path, db_path):
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)  # 21:00 PDT on the 24th
    with closing(connect(db_path)) as conn, conn:
        _summary(conn, "2026-10-03T12:00:00+00:00", 0, 1_000_000)  # month only: $0.50
        _summary(conn, "2026-10-24T20:00:00+00:00", 2_000_000, 0)  # last 24 hours: $0.20
        _summary(conn, "2026-09-30T20:00:00+00:00", 10_000_000, 0)  # last month: not counted
    report = await _report_at(tmp_path, db_path, now)
    assert _spend_line(report) == (
        "Claude spend: about $0.20 in the last 24 hours, $0.70 so far in October (estimate)"
    )


async def test_a_self_hosted_copy_with_no_summaries_says_zero_twice(tmp_path, db_path):
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    report = await _report_at(tmp_path, db_path, now)
    assert _spend_line(report) == (
        "Claude spend: $0.00 in the last 24 hours, $0.00 so far in October (estimate)"
    )


async def test_the_month_is_the_owners_month_when_utc_is_already_the_next_one(tmp_path, db_path):
    # 21:00 PDT on October 31st is 04:00 UTC on November 1st: still October, and the month
    # starts at 07:00 UTC on the 1st (midnight PDT), not at midnight UTC.
    now = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _summary(conn, "2026-10-01T03:00:00+00:00", 10_000_000, 0)  # 20:00 PDT on Sep 30
        _summary(conn, "2026-10-01T07:00:00+00:00", 0, 1_000_000)  # midnight PDT on Oct 1: in
        _summary(conn, "2026-11-01T01:00:00+00:00", 2_000_000, 0)  # 18:00 PDT on Oct 31: in
    report = await _report_at(tmp_path, db_path, now)
    assert _spend_line(report) == (
        "Claude spend: about $0.20 in the last 24 hours, $0.70 so far in October (estimate)"
    )


async def test_the_month_follows_a_configured_timezone(tmp_path, db_path):
    # 23:30 UTC on October 31st is 08:30 on November 1st in Tokyo.
    now = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        _summary(conn, "2026-10-31T15:00:00+00:00", 0, 1_000_000)  # midnight Nov 1 in Tokyo: $0.50
        _summary(conn, "2026-10-31T14:59:00+00:00", 10_000_000, 0)  # still October there: $1.00
    report = await _report_at(tmp_path, db_path, now, timezone="Asia/Tokyo")
    assert _spend_line(report) == (
        "Claude spend: about $1.50 in the last 24 hours, $0.50 so far in November (estimate)"
    )
