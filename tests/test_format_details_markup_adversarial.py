"""Adversarial round on what scraped reward and origin text can do once it is on screen.

Three attacks live here. A reward or origin that tries to ping (`@everyone`, `<@&id>`,
`</cmd:id>`), to link (bare, masked, autolinked, invites), to format (a fence break,
spoilers, headings, `-# `, quotes) or to play with the clock (timestamps). Bidirectional
controls mixed with the joiner that holds emoji together, with the 300-unit cap, and
with the mention escaper. And the 48-hour wording, which has to be right on both sides
of Pacific midnight and of both daylight-saving changes.

Every surface a reward can reach is covered: the details line, a first alert, an edit, a
follow-up, a roundup, `/shift codes` and `/shift history`. "Defused" means no live
mention, link or formatting survives once the legitimate markup of that surface (its
fences, its own links, its bold headings) is set aside.

`<t:...>`, custom emoji and `<id:...>` navigation tokens are defused like the other
angle-bracket links (`esc()` puts a zero-width space after the `<`).

Invisible and bidi characters are `\\uXXXX` escapes on purpose; this file stays ASCII.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot.bot.format import (
    details_line,
    details_reserve,
    discord_len,
    fit_followup_codes,
    render_alert_edit,
    render_code_alerts,
    render_current_pages,
    render_followup_alert,
    render_history_pages,
    render_roundup_alerts,
)
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # noon PDT, Oct 9
URL = "https://e.com/a0"
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
BIDI_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
ID = "123456789012345678"
ID20 = "12345678901234567890"


def _code(i: int = 0) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _row(code: str | None = None, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code or _code(), clock_start=NOW, updated_at=NOW)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _line(now=NOW, **kwargs) -> str | None:
    return details_line(_row(**kwargs), now=now)


def _rel(now=NOW, **kwargs) -> str | None:
    """The `/shift history` flavor of the line: relative wording, fresh on every call."""
    return details_line(_row(**kwargs), now=now, relative=True)


# --- what counts as live ---

_LIVE = {
    "everyone/here": re.compile(r"@(?:everyone|here)"),
    "user/role mention": re.compile(r"<@[!&]?\d"),
    "channel or command link": re.compile(r"<[#/]"),
    "masked link opener": re.compile(r"(?<!\\)\["),
    "backtick": re.compile(r"(?<!\\)`"),
    "spoiler": re.compile(r"(?<!\\)\|\|"),
    "emphasis": re.compile(r"(?<!\\)[*_~]"),
    "heading, subtext or quote": re.compile(r"(?m)^[ \t]*(?:#{1,3}[ \t]|-#[ \t]|>)"),
    "url scheme": re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://"),
    "invite": re.compile(r"(?i)discord(?:app)?\.(?:gg|com/invite)/"),
}


def _live(text: str) -> list[str]:
    return [name for name, pattern in _LIVE.items() if pattern.search(text)]


def _set_aside(text: str, *, codes=(), extra=()) -> str:
    """Remove a surface's own markup, so only what the scraped text could add is left."""
    for code in codes:
        text = text.replace(f"```\n{code}\n```", "").replace(f"`{code}`", "")
    for piece in extra:
        text = text.replace(piece, "")
    text = re.sub(r"<https://e\.com/[^>\s]*>", "", text)
    return re.sub(r"\*\*[^*\n]+\*\*", "", text)


HOSTILE = [
    "@everyone",
    "@here",
    "@everyone @here @everyone",
    f"<@&{ID}>",
    f"<@&{ID20}>",
    f"<@{ID}>",
    f"<@!{ID}>",
    f"<#{ID}>",
    f"</shift codes:{ID}>",
    "</cmd:1>",
    "[x](http://evil.example)",
    "[x](<http://evil.example>)",
    "![img](http://evil.example/a.png)",
    "http://evil.example",
    "<http://evil.example>",
    "https://evil.example/x?y=1",
    "discord.gg/abcdef",
    "discord.com/invite/abcdef",
    "```",
    "``` break out ``` @everyone",
    "x```\n@everyone\n```y",
    "`one` `two`",
    "||spoiler||",
    "# heading",
    "## heading",
    "### heading",
    "-# subtext",
    "> quote",
    ">>> block quote",
    "ok\n# heading after a newline",
    "ok\n-# subtext after a newline",
    "ok\n> quote after a newline",
    "ok\n>>> block quote after a newline",
    "**bold** __under__ ~~strike~~ *it* _it_",
    "\\",
    "\\*\\*",
    "@\u202eeveryone",
    "@\u2066\u2067here",
    f"<\u202e@&{ID}>",
    f"<@\u202e&{ID}>",
    f"<\u2066#{ID}>",
    "</\u202ecmd:1>",
    "[\u202ex](\u2066http://evil.example)",
    "\u202e# heading",
    "\u2066> quote",
    "ht\u202etps://evil.example",
    "discord\u2069.gg/abcdef",
    " # heading with a leading space",
    "\t> quote after a tab",
    "x" * 280 + " @everyone",
    "@everyone" * 33,
    FAMILY + "@everyone" + FAMILY,
]


def _surfaces(text: str) -> dict[str, str]:
    """Every place `text` can show up as a reward or origin, minus each surface's own markup."""
    plain = _row(
        _code(0),
        reward=text,
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    perm = _row(
        _code(1),
        reward=text,
        reward_from="mentalmars",
        category="permanent",
        category_from="mentalmars",
        origin=text,
    )
    cand = CodeCandidate(
        code=_code(0), golden=False, source_name="Gearbox Blog", item_url=URL, fresh=True
    )
    codes = (_code(0), _code(1))
    out = {"line": _set_aside(details_line(plain, now=NOW) or "")}
    details = {_code(0): plain}
    [alert] = render_code_alerts(
        [cand], ping=False, details=details, now=NOW, reserve=details_reserve(["mentalmars"])
    )
    out["alert"] = _set_aside(alert.content, codes=codes, extra=("Gearbox Blog",))
    edit = render_alert_edit(alert.header, [cand], golden_prefix=False, details=details, now=NOW)
    out["edit"] = _set_aside(edit, codes=codes, extra=("Gearbox Blog",))
    follow = render_followup_alert(
        [_code(0)], ping_mention="@everyone", details=details, now=NOW
    ).content
    out["followup"] = _set_aside(
        follow.removeprefix("@everyone "), codes=codes, extra=("Confirmed by a second source:",)
    )
    rcand = CodeCandidate(
        code=_code(0),
        golden=False,
        source_name="Roundup Blog",
        item_url="https://e.com/post",
        fresh=True,
        roundup=True,
    )
    [roundup] = render_roundup_alerts([rcand], details=details, now=NOW)
    out["roundup"] = _set_aside(roundup.content, codes=codes, extra=("Roundup Blog",))
    pages = render_current_pages(
        [
            CurrentCode(code=_code(0), first_seen_at=NOW, details=plain),
            CurrentCode(code=_code(1), first_seen_at=NOW, details=perm),
        ],
        kind="all",
        now=NOW,
        footer="f",
        empty="e",
    )
    out["current"] = _set_aside("\n".join(p.description for p in pages), codes=codes)
    view = CodeView(
        code=_code(0),
        first_seen_at=NOW,
        source_name="Gearbox Blog",
        item_url=URL,
        status="posted",
        from_roundup=False,
        details=plain,
    )
    history = render_history_pages([view], timezone="America/Los_Angeles", now=NOW, title="t")
    out["history"] = _set_aside(
        "\n".join(p.description for p in history),
        codes=codes,
        extra=("Gearbox Blog", "First seen 2026-10-09"),
    )
    return out


@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_hostile_text_is_defused_on_every_surface(text):
    for surface, shown in _surfaces(text).items():
        assert _live(shown) == [], (surface, shown)


@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_hostile_text_never_leaves_a_bidi_control_on_any_surface(text):
    for surface, shown in _surfaces(text).items():
        assert not any(ch in shown for ch in BIDI_CONTROLS), surface


def test_a_mention_survives_as_text_not_as_nothing():
    # Defused is not deleted: a reader still sees what the reward said.
    shown = _surfaces("@everyone")["line"]
    assert "@\u200beveryone" in shown
    shown = _surfaces(f"<@&{ID}>")["line"]
    assert ID in shown


def test_known_defusers_still_work_on_the_checker_itself():
    # If this stopped failing, the table above would be passing for the wrong reason.
    assert "everyone/here" in _live("@everyone")
    assert "user/role mention" in _live(f"<@&{ID}>")
    assert "channel or command link" in _live(f"<#{ID}>")
    assert "masked link opener" in _live("[x](http://e)")
    assert "backtick" in _live("```")
    assert "spoiler" in _live("||x||")
    assert "heading, subtext or quote" in _live("ok\n-# x")
    assert "url scheme" in _live("https://e.com")
    assert "invite" in _live("discord.gg/x")


@pytest.mark.parametrize(
    "token",
    [
        "<t:1789000000:R>",
        "<t:1789000000>",
        "<t:1:f>",
        f"<:wave:{ID}>",
        f"<a:wave:{ID}>",
        "<id:customize>",
        "<id:browse>",
        "<id:guide>",
        "<\u202et:1789000000:R>",
    ],
)
def test_angle_bracket_tokens_other_than_mentions_do_not_render_live(token):
    line = _line(reward=f"2 Golden Keys {token}")
    assert not re.search(r"<(?:t:|a?:\w+:\d+>|id:)", line), line
    # With an expiry the bot adds its own timestamp, and that one is the only live token.
    dated = _line(reward=f"2 Golden Keys {token}", expires_on=date(2026, 10, 14))
    assert re.findall(r"<(?:t:|a?:\w+:\d+>|id:)[^>]*>", dated) == ["<t:1792047600:R>"], dated


# --- bidirectional controls ---


def test_an_override_between_zwj_and_the_next_emoji_keeps_the_sequence_joined():
    line = _line(reward="\U0001f468\u200d\u202e\U0001f469")
    assert line == "\U0001f468\u200d\U0001f469"


def test_nested_isolates_and_overrides_all_go():
    reward = "\u2066\u2067\u2068\u202e\u202bx\u202c\u202c\u2069\u2069\u2069"
    assert _line(reward=reward) == "x"


def test_an_unclosed_override_in_front_of_a_family_leaves_the_family_whole():
    line = _line(reward="\u202e" + FAMILY + " keys", reward_from="mentalmars")
    assert line == FAMILY + " keys \u00b7 details via mentalmars"


def test_stripping_can_bring_a_reward_back_under_the_cap_without_an_ellipsis():
    reward = "x" * 250 + "\u202e" * 100  # 350 units raw, 250 shown
    assert _line(reward=reward) == "x" * 250


def test_an_override_exactly_at_the_cap_does_not_push_the_reward_over():
    reward = "x" * 300 + "\u202e"
    assert _line(reward=reward) == "x" * 300


def test_an_override_in_the_middle_of_a_reward_that_is_one_unit_too_long_is_cut_after_stripping():
    reward = "x" * 299 + "\u202e" + "yy"  # 301 units once stripped
    assert _line(reward=reward) == "x" * 299 + "\u2026"


def test_a_cut_never_lands_inside_a_surrogate_pair():
    reward = "x" * 299 + "\U0001f600"  # the emoji would straddle unit 300
    line = _line(reward=reward)
    assert discord_len(line) <= 300
    line.encode("utf-16-le")  # a lone surrogate would raise here
    assert "\U0001f600" not in line


def test_a_mention_split_by_an_override_is_still_defused_and_the_count_still_holds():
    reward = "@\u202eeveryone" * 33  # 330 characters raw
    line = _line(reward=reward)
    assert "@everyone" not in line
    assert discord_len(line) <= 300


def test_bidi_heavy_rewards_never_outgrow_the_cap():
    rng = random.Random(20261009)  # noqa: S311 (a seeded shuffle, not a secret)
    pieces = ["@", "everyone", "<", "#", "/", "*", "\\", "https", ":", "//", "x", FAMILY, "\u200d"]
    pieces += list(BIDI_CONTROLS)
    for _ in range(600):
        reward = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 700)))
        line = _line(reward=reward)
        if line is not None:
            assert discord_len(line) <= 300, reward
            assert not any(ch in line for ch in BIDI_CONTROLS)


def test_a_reward_of_nothing_but_controls_is_no_reward_on_the_page_either():
    entry = CurrentCode(
        code=_code(), first_seen_at=NOW, details=_row(reward=BIDI_CONTROLS, expires_on=None)
    )
    [embed] = render_current_pages([entry], kind="all", now=NOW, footer="f", empty="e")
    assert "reward unknown" in embed.description


def test_an_origin_at_the_cap_with_controls_is_untouched_and_a_dangling_override_is_dropped():
    perm = dict(category="permanent", category_from="mentalmars")
    entries = [
        CurrentCode(
            code=_code(0),
            first_seen_at=NOW,
            details=_row(_code(0), origin="o" * 300 + "\u202e" * 40, **perm),
        ),
        CurrentCode(
            code=_code(1),
            first_seen_at=NOW - timedelta(days=1),
            details=_row(_code(1), origin="\u202eabc", **perm),
        ),
    ]
    [embed] = render_current_pages(entries, kind="permanent", now=NOW, footer="f", empty="e")
    assert "o" * 300 in embed.description
    assert "\u2026" not in embed.description
    assert "reward unknown \u00b7 abc" in embed.description
    assert not any(ch in embed.description for ch in BIDI_CONTROLS)


# --- the 48-hour wording ---


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d + timedelta(days=1), time.min, tzinfo=PT).astimezone(UTC)


def _oracle(d: date, now: datetime) -> str:
    """The wording, worked out from exact timedeltas instead of the implementation's shortcuts."""
    end = _end_of_day(d)
    today = now.astimezone(PT).date()
    if now >= end:
        return "expired"
    if today == d:
        remaining = end - now
        if remaining < timedelta(hours=1):
            return "expires in under an hour"
        hours = remaining // timedelta(hours=1)
        return f"expires in {hours} hour{'' if hours == 1 else 's'}"
    if today + timedelta(days=1) == d:
        return "expires tomorrow"
    return f"expires {d.strftime('%b')} {d.day}, {d.year}"


@pytest.mark.parametrize(
    ("start", "expires"),
    [
        (datetime(2026, 3, 6, 12, tzinfo=PT), date(2026, 3, 8)),  # spring forward, Mar 8
        (datetime(2026, 3, 7, 12, tzinfo=PT), date(2026, 3, 9)),
        (datetime(2026, 10, 30, 12, tzinfo=PT), date(2026, 11, 1)),  # fall back, Nov 1
        (datetime(2026, 10, 31, 12, tzinfo=PT), date(2026, 11, 2)),
        (datetime(2026, 10, 7, 12, tzinfo=PT), date(2026, 10, 9)),  # an ordinary week
    ],
)
def test_the_wording_matches_an_independent_oracle_every_five_minutes(start, expires):
    t = start.astimezone(UTC)
    seen = []
    for k in range(0, 4 * 24 * 12):
        now = t + timedelta(minutes=5 * k)
        phrase = _rel(now=now, expires_on=expires)
        assert phrase == _oracle(expires, now), now.astimezone(PT).isoformat()
        if not seen or seen[-1] != phrase:
            seen.append(phrase)

    # It only ever walks forward: a date, tomorrow, N hours (counting down), under an hour, expired.
    def stage(phrase):
        if phrase == "expires tomorrow":
            return 1
        if phrase == "expires in under an hour":
            return 3
        if phrase == "expired":
            return 4
        return 2 if re.fullmatch(r"expires in \d+ hours?", phrase) else 0

    stages = [stage(p) for p in seen]
    assert stages == sorted(stages)
    assert stages[0] == 0 and stages[-1] == 4
    hours = [int(p.split()[2]) for p in seen if stage(p) == 2]
    assert hours == sorted(hours, reverse=True)


@pytest.mark.parametrize(
    ("now", "expires", "expected"),
    [
        # Exact second boundaries around the end of the day.
        (datetime(2026, 10, 10, 6, 0, 0, tzinfo=UTC), date(2026, 10, 9), "expires in 1 hour"),
        (
            datetime(2026, 10, 10, 6, 0, 1, tzinfo=UTC),
            date(2026, 10, 9),
            "expires in under an hour",
        ),
        (
            datetime(2026, 10, 10, 6, 59, 59, 999999, tzinfo=UTC),
            date(2026, 10, 9),
            "expires in under an hour",
        ),
        (datetime(2026, 10, 10, 7, 0, 0, tzinfo=UTC), date(2026, 10, 9), "expired"),
        (datetime(2026, 10, 10, 5, 0, 0, tzinfo=UTC), date(2026, 10, 9), "expires in 2 hours"),
        (datetime(2026, 10, 10, 5, 0, 1, tzinfo=UTC), date(2026, 10, 9), "expires in 1 hour"),
        # Pacific midnight turns the day, not UTC's.
        (datetime(2026, 10, 10, 6, 59, 59, tzinfo=UTC), date(2026, 10, 10), "expires tomorrow"),
        (datetime(2026, 10, 10, 7, 0, 0, tzinfo=UTC), date(2026, 10, 10), "expires in 24 hours"),
        (datetime(2026, 10, 10, 7, 0, 0, tzinfo=UTC), date(2026, 10, 11), "expires tomorrow"),
        (datetime(2026, 10, 10, 6, 59, 59, tzinfo=UTC), date(2026, 10, 11), "expires Oct 11, 2026"),
        # UTC already tomorrow, Pacific still today.
        (datetime(2026, 10, 10, 3, 0, 0, tzinfo=UTC), date(2026, 10, 9), "expires in 4 hours"),
        # Spring forward: Mar 8 is 23 hours long.
        (datetime(2026, 3, 8, 8, 0, 0, tzinfo=UTC), date(2026, 3, 8), "expires in 23 hours"),
        (datetime(2026, 3, 8, 10, 0, 0, tzinfo=UTC), date(2026, 3, 8), "expires in 21 hours"),
        (datetime(2026, 3, 8, 7, 59, 59, tzinfo=UTC), date(2026, 3, 8), "expires tomorrow"),
        (datetime(2026, 3, 8, 7, 59, 59, tzinfo=UTC), date(2026, 3, 7), "expires in under an hour"),
        # Fall back: Nov 1 is 25 hours long, and 01:30 happens twice.
        (datetime(2026, 11, 1, 7, 0, 0, tzinfo=UTC), date(2026, 11, 1), "expires in 25 hours"),
        (datetime(2026, 11, 1, 8, 30, 0, tzinfo=UTC), date(2026, 11, 1), "expires in 23 hours"),
        (datetime(2026, 11, 1, 9, 30, 0, tzinfo=UTC), date(2026, 11, 1), "expires in 22 hours"),
        (datetime(2026, 11, 1, 6, 59, 59, tzinfo=UTC), date(2026, 11, 1), "expires tomorrow"),
        (
            datetime(2026, 11, 2, 7, 59, 59, tzinfo=UTC),
            date(2026, 11, 1),
            "expires in under an hour",
        ),
        (datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC), date(2026, 11, 1), "expired"),
        # A clock in another zone is the same instant.
        (
            datetime(2026, 10, 10, 20, 30, tzinfo=ZoneInfo("Pacific/Kiritimati")),
            date(2026, 10, 9),
            "expires in under an hour",
        ),
    ],
)
def test_wording_at_the_boundaries(now, expires, expected):
    assert _rel(now=now, expires_on=expires) == expected


def test_a_passed_expiry_says_expired_with_everything_else_intact():
    line = _rel(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 1),
        expires_from="Gearbox Blog",
    )
    assert line == "3 Golden Keys \u00b7 expired \u00b7 details via mentalmars and Gearbox Blog"
    # Persisted messages never say "expired": the date, and Discord's timestamp, do that job.
    posted = _line(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 1),
        expires_from="Gearbox Blog",
    )
    assert posted == (
        "3 Golden Keys \u00b7 expires Oct 1, 2026 (<t:1790924400:R>)"
        " \u00b7 details via mentalmars and Gearbox Blog"
    )


def test_a_passed_expiry_alone_is_still_a_line():
    assert _rel(expires_on=date(2020, 1, 1), expires_from="mentalmars") == (
        "expired \u00b7 details via mentalmars"
    )
    assert _line(expires_on=date(2020, 1, 1), expires_from="mentalmars") == (
        "expires Jan 1, 2020 (<t:1577952000:R>) \u00b7 details via mentalmars"
    )


@pytest.mark.parametrize(
    "expires", [date(2020, 1, 1), date(2026, 10, 9), date(2026, 10, 10), date(2099, 1, 1), None]
)
def test_a_permanent_code_with_any_expiry_says_permanent_and_never_expired(expires):
    line = _line(
        reward="Head",
        reward_from="mentalmars",
        category="permanent",
        category_from="mentalmars",
        expires_on=expires,
        expires_from="Gearbox Blog",
    )
    assert line == "Head \u00b7 permanent \u00b7 details via mentalmars"
    assert line == _rel(
        reward="Head",
        reward_from="mentalmars",
        category="permanent",
        category_from="mentalmars",
        expires_on=expires,
        expires_from="Gearbox Blog",
    )


@pytest.mark.parametrize(
    ("expires", "expected"),
    [
        # Each timestamp is the Pacific midnight that ends the day, worked out by hand.
        (date(2028, 2, 29), "expires Feb 29, 2028 (<t:1835510400:R>)"),  # Mar 1 08:00 UTC
        (date(2026, 12, 31), "expires Dec 31, 2026 (<t:1798790400:R>)"),  # Jan 1 08:00 UTC
        (date(2027, 1, 1), "expires Jan 1, 2027 (<t:1798876800:R>)"),
        (date(2099, 9, 30), "expires Sep 30, 2099 (<t:4094521200:R>)"),  # Oct 1 07:00 UTC
        (date(9999, 12, 31), "expires Dec 31, 9999"),  # too far out for a token
    ],
)
def test_far_dates_read_as_dates(expires, expected):
    assert _line(expires_on=expires) == expected


def test_the_current_page_counts_days_from_pacific_midnight():
    entry = CurrentCode(
        code=_code(), first_seen_at=NOW, details=_row(reward="r", expires_on=date(2026, 10, 14))
    )

    def phrase(now):
        [embed] = render_current_pages([entry], kind="all", now=now, footer="f", empty="e")
        return embed.description.splitlines()[-1]

    before = datetime(2026, 10, 10, 6, 59, 59, tzinfo=UTC)  # Oct 9, 23:59:59 PDT
    assert phrase(before) == "r \u00b7 expires Oct 14 (in 5 days)"
    assert phrase(before + timedelta(seconds=1)) == "r \u00b7 expires Oct 14 (in 4 days)"
    assert phrase(datetime(2026, 10, 13, 7, 0, tzinfo=UTC)) == "r \u00b7 expires tomorrow"


def test_the_current_page_shows_the_year_only_when_it_is_another_year():
    entries = [
        CurrentCode(
            code=_code(0), first_seen_at=NOW, details=_row(_code(0), expires_on=date(2026, 12, 31))
        ),
        CurrentCode(
            code=_code(1),
            first_seen_at=NOW - timedelta(days=1),
            details=_row(_code(1), expires_on=date(2027, 1, 2)),
        ),
    ]
    [embed] = render_current_pages(entries, kind="all", now=NOW, footer="f", empty="e")
    assert "expires Dec 31 (in 83 days)" in embed.description
    assert "expires Jan 2, 2027 (in 85 days)" in embed.description


def test_followup_lines_use_the_same_wording_as_alerts():
    code = _code()
    details = {code: _row(code, reward="r", expires_on=date(2026, 10, 10))}
    fit = fit_followup_codes([code], ping_mention="@everyone", details=details, now=NOW)
    alert = render_followup_alert(fit, ping_mention="@everyone", details=details, now=NOW)
    assert alert.content == (
        "@everyone Confirmed by a second source:\n"
        f"`{code}` \u00b7 r \u00b7 expires Oct 10, 2026 (<t:1791702000:R>)"
    )


@pytest.mark.parametrize("mention", ["@everyone", f"<@&{ID20}>"])
@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_a_pinged_alert_and_its_edit_carry_exactly_the_one_ping_in_the_header(mention, text):
    cand = CodeCandidate(
        code=_code(0), golden=False, source_name="Gearbox Blog", item_url=URL, fresh=True
    )
    details = {_code(0): _row(_code(0), reward=text, reward_from="mentalmars")}
    [alert] = render_code_alerts(
        [cand], ping=True, ping_mention=mention, details=details, now=NOW, reserve=500
    )
    edit = render_alert_edit(alert.header, [cand], golden_prefix=False, details=details, now=NOW)
    live = re.compile(r"@(?:everyone|here)|<@&?\d")
    for content in (alert.content, edit):
        assert content.startswith(f"{mention} ")
        assert len(live.findall(content)) == 1, content
