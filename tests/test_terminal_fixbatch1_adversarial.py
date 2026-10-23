"""Adversarial round on fix batch 1 of the v3.2.0 qa pass: escaping inside URLs, one-line details.

The batch changed three things in `format.py`, and each one gets leaned on here.

1. `esc()` escapes markdown inside URL-shaped text now (`ignore_links=False`). Before, a reward
   of "see https://x.y/`" kept its raw backtick, which could close a label chip early (so the
   timestamp and the link landed inside a chip), and three of them opened a fence. The attack
   matrix below crosses backticks (1 to 4), `||`, `~~`, `*` and `_` with every URL-shaped
   spelling I could think of (`https://`, `<x:/...>`, `steam://`, a bare `x:/`, a URL inside
   parentheses, two URLs in one string), puts the result into every scraped field one at a
   time, and renders every message kind: alerts, terminal and classic edits, roundups,
   follow-ups, `/shift codes`, `/shift history`, and the daily digest's two embeds.
   The checks are made by a small parser of my own (the same idea as `chip_check`, with more
   teeth): chips are a subset of the allowed ones, every fence holds exactly a code, no
   timestamp or link sits in a chip, and once the bot's own chips, fences, ping, live links and
   timestamps are taken out of a message, nothing is left that Discord could read as a chip,
   a fence, a spoiler, a strikethrough or emphasis.
2. `_detail_text` collapses whitespace, newlines included, so a reward or origin can't start a
   list, a quote or a line of its own. Every message kind again, and the check is structural:
   every line outside a fence starts with a chip, so there is no line left to be a list.
3. Budgets. The reserve is still the worst line plus one with the new `esc`, `cap_shown` still
   promises what it promises, a message packed with the real reserve and then edited with the
   worst details still fits with every link, and `_EXPIRY_VALUE_MAX` is still 32 and still
   built from its parts rather than typed in.

A failing test in here is a bug report, not a test to loosen.
"""

from __future__ import annotations

import inspect
import random
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies

from newsbot.bot import format as fmt
from newsbot.bot import terminal
from newsbot.bot.format import (
    _EXPIRY_VALUE_MAX,
    _detail_text,
    cap_shown,
    details_line,
    details_reserve,
    discord_len,
    esc,
    render_alert_edit,
    render_code_alerts,
    render_current_pages,
    render_followup_alerts,
    render_guild_digest,
    render_history_pages,
    render_roundup_alerts,
)
from newsbot.pipeline.summarize import StoryDraft
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.match import is_code
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode, HeadlineItem

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
ROLE = "<@&123456789012345678>"
ROLE_20 = "<@&" + "9" * 20 + ">"
URL = "https://example.com/post"
CLASSIC_HEADER = "**New SHiFT codes**"


# --- builders ---


def _code(i: int = 0) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _cand(i: int = 0, **kwargs) -> CodeCandidate:
    defaults = dict(
        code=_code(i), golden=False, source_name="Gearbox Blog", item_url=URL, fresh=True
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


def _row(code: str, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code, clock_start=NOW, updated_at=NOW)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


# --- the parser: what could Discord read as markup, once the bot's own pieces are out ---

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_PING_PREFIX = re.compile(r"^(?:@everyone |@here |<@&\d+> )")
_LIVE_LINK = re.compile(r"<https?://[^\s<>]+>")
_STAMP = re.compile(r"\(<t:\d{1,11}:R>\)")
_STRAY_LINKY = re.compile(r"<(?:[#/@:]|a:|t:|id:)", re.IGNORECASE)
_OWN_CHIPS = sorted(ALLOWED_CHIPS, key=len, reverse=True)
_LINE_STARTS_A_BLOCK = re.compile(r"^\s*(?:>|[-*+]\s|#{1,3}\s|-#\s|\d+[.)]\s)")


def _unescaped(text: str, token: str) -> list[int]:
    """Where `token` appears with no backslash making it literal (a backslash eats the next)."""
    found, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text.startswith(token, i):
            found.append(i)
        i += 1
    return found


def _strip_own_chips(text: str) -> str:
    for chip in _OWN_CHIPS:
        text = text.replace(f"`{chip}`", "")
    return text


def _markup_problems(text: str, *, n_codes: int | None, allow_chips: bool = True) -> list[str]:
    """Everything wrong with `text` as a Discord message; an empty list is a clean bill.

    `n_codes` is how many fences it should hold (each one exactly a code), or None for text
    that has no fences at all (a digest). `allow_chips=False` is for the classic layout and
    the digest, which have no chips: any backtick outside a fence is then a finding.
    """
    problems: list[str] = []
    found_chips = chips(text)
    if allow_chips:
        if not set(found_chips) <= ALLOWED_CHIPS:
            problems.append(f"chips outside the allowed set: {set(found_chips) - ALLOWED_CHIPS}")
    elif found_chips:
        problems.append(f"a layout with no chips has one: {found_chips}")
    for chip in found_chips:
        if any(bad in chip for bad in ("<", "://", "@", "t:", "\n")):
            problems.append(f"a link, timestamp, mention or newline inside a chip: {chip!r}")

    bodies = fence_bodies(text)
    expected = 0 if n_codes is None else n_codes
    if len(bodies) != expected or text.count("```") != 2 * expected:
        problems.append(f"{len(bodies)} fences / {text.count('```')} fence marks, want {expected}")
    problems += [f"fence holds more than a code: {b!r}" for b in bodies if not is_code(b)]

    rest = _PING_PREFIX.sub("", text)
    rest = _FENCE.sub("", rest)
    if allow_chips:
        rest = _strip_own_chips(rest)
    # The bot's own stamps and links go; anything else of that shape is somebody's forgery.
    n_stamps = len(_STAMP.findall(rest))
    rest = _STAMP.sub("", rest)
    if rest.count("<t:") != 0:
        problems.append(f"a timestamp that isn't the bot's: {rest.count('<t:')} of them")
    if n_stamps > expected * 2:
        problems.append(f"{n_stamps} timestamps for {expected} codes")
    rest = _LIVE_LINK.sub("", rest)
    if "://" in rest:
        problems.append("a live scheme:// outside the bot's own link")
    if _STRAY_LINKY.search(rest):
        problems.append("a live mention, channel link, command link or emoji")
    if "@everyone" in rest or "@here" in rest or "<@" in rest:
        problems.append("a live mention outside the ping prefix")
    for token, what in (
        ("`", "a backtick (chip or fence) that isn't escaped"),
        ("||", "a spoiler"),
        ("~~", "a strikethrough"),
        ("*", "emphasis with *"),
        ("_", "emphasis with _"),
    ):
        if _unescaped(rest, token):
            problems.append(f"{what}: {rest!r}")
    return problems


def _every_line_opens_with_a_chip(text: str) -> list[str]:
    """Structural "can't start a list or quote": every line outside a fence opens with a chip."""
    rest = _FENCE.sub("", _PING_PREFIX.sub("", text))
    return [
        f"line does not open with a chip: {line!r}"
        for line in rest.split("\n")
        if line.strip() and not line.startswith("`")
    ]


# --- every message kind, with one hostile string in one scraped field ---


@dataclass
class Surface:
    where: str
    text: str
    n_codes: int | None
    layout: str  # "look", "classic" or "page"
    block_lines: tuple[int, ...] = (4, 5)  # fence, details (maybe), src


def _rows(f: dict) -> dict[str, CodeDetailsRow]:
    plain, golden, cosmetic = _code(0), _code(1), _code(2)
    return {
        plain: _row(
            plain,
            reward=f["reward"],
            reward_from=f["reward_from"],
            expires_on=date(2026, 10, 14),
            expires_from=f["expires_from"],
            origin=f["origin"],
        ),
        golden: _row(
            golden,
            reward=f["reward"],
            reward_from=f["reward_from"],
            category="permanent",
            category_from=f["category_from"],
            origin=f["origin"],
        ),
        cosmetic: _row(cosmetic, reward=f["reward"], origin=f["origin"]),
    }


def _surfaces(f: dict) -> list[Surface]:
    """Every alert, edit, roundup, follow-up and page, with the fields in `f` as their text."""
    src = (f["source"] or "s")[:100]
    plain, golden, cosmetic = _cand(0, source_name=src), _cand(1, golden=True), _cand(2)
    golden = _cand(1, golden=True, source_name=src)
    cosmetic = _cand(2, source_name=src)
    batch = [plain, golden, cosmetic]
    by_code = {c.code: c for c in batch}
    details = _rows(f)
    names = [(f[k] or "n")[:100] for k in ("reward_from", "expires_from", "category_from")]
    reserve = details_reserve([*names, src])
    out: list[Surface] = []

    for cands, ping, mention, test in (
        (batch, False, None, False),
        (batch, True, None, False),
        (batch, True, ROLE_20, True),
        ([golden], True, ROLE, False),
    ):
        kw = dict(ping=ping, ping_mention=mention, test=test, reserve=reserve)
        for n, a in enumerate(render_code_alerts(cands, details=details, now=NOW, **kw)):
            out.append(Surface(f"alert {n}", a.content, len(a.codes), "look"))
        for n, a in enumerate(render_code_alerts(cands, **kw)):
            members = [by_code[c] for c in a.codes]
            for header in (a.header, a.header.removeprefix(a.ping_prefix)):
                edit = render_alert_edit(
                    header,
                    members,
                    golden_prefix=a.golden_prefix,
                    details=details,
                    now=NOW,
                )
                out.append(Surface(f"terminal edit {n}", edit, len(members), "look"))
            classic = render_alert_edit(
                CLASSIC_HEADER, members, golden_prefix=True, details=details, now=NOW
            )
            out.append(Surface(f"classic edit {n}", classic, len(members), "classic"))

    roundup_batch = [replace_src(c, src) for c in batch]
    for n, a in enumerate(render_roundup_alerts(roundup_batch, details=details, now=NOW)):
        out.append(Surface(f"roundup {n}", a.content, len(a.codes), "look", (3, 4)))

    for mention in ("@everyone", ROLE):
        for n, fu in enumerate(
            render_followup_alerts(
                [c.code for c in batch], ping_mention=mention, details=details, now=NOW
            )
        ):
            out.append(Surface(f"followup {n}", fu.content, len(fu.codes), "look", (3, 4)))

    entries = [CurrentCode(code=c.code, first_seen_at=NOW, details=details[c.code]) for c in batch]
    for kind in ("all", "golden", "permanent", "cosmetic"):
        pages = render_current_pages(entries, kind=kind, now=NOW, footer="sources", empty="none")
        for n, embed in enumerate(pages):
            if embed.description == "none":  # the empty page, nothing of ours on it
                continue
            count = str(embed.description).count("```") // 2
            out.append(Surface(f"/shift codes {kind} {n}", str(embed.description), count, "page"))

    views = [
        CodeView(
            code=c.code,
            first_seen_at=NOW,
            source_name=src,
            item_url=URL,
            status="posted",
            from_roundup=i == 1,
            details=details[c.code],
        )
        for i, c in enumerate(batch)
    ]
    for n, embed in enumerate(render_history_pages(views, timezone="UTC", now=NOW)):
        count = str(embed.description).count("```") // 2
        out.append(Surface(f"/shift history {n}", str(embed.description), count, "page"))
    return out


def replace_src(candidate: CodeCandidate, source_name: str) -> CodeCandidate:
    return CodeCandidate(
        code=candidate.code,
        golden=candidate.golden,
        source_name=source_name,
        item_url=candidate.item_url,
        fresh=True,
    )


_FIELD_NAMES = ("reward", "reward_from", "expires_from", "category_from", "origin", "source")


def _placements(text: str):
    """`text` in one scraped field at a time, harmless text elsewhere; then in all of them."""
    for name in _FIELD_NAMES:
        yield name, {k: (text if k == name else "ok") for k in _FIELD_NAMES}
    yield "everywhere", dict.fromkeys(_FIELD_NAMES, text)


def _check(surface: Surface) -> list[str]:
    if surface.layout == "classic":
        # The stored v3.1 header is bold on purpose and the edit keeps it as it went out.
        body = surface.text.removeprefix(CLASSIC_HEADER)
        return _markup_problems(body, n_codes=surface.n_codes, allow_chips=False)
    problems = _markup_problems(surface.text, n_codes=surface.n_codes)
    problems += _every_line_opens_with_a_chip(surface.text)
    return problems


# --- 1. the attack matrix: markup characters inside URL-shaped text ---

MARKS = ["`", "``", "```", "````", "||", "~~", "*", "**", "_", "__", "`||`", "\\`", "~~`"]
URL_SHAPES = [
    "https://x.y/{m}",
    "see https://x.y/{m} now",
    "3 Golden Keys, see https://x.y/{m}",
    "<x:/{m}>",
    "<ftp://a.b/{m}>",
    "<https://x.y/{m}>",
    "steam://a{m}b",
    "x:/{m}",
    "{m}https://x.y/",
    "https://x.y/{m}{m}",
    "http://a.b/?q={m}&r={m}",
    "https://x.y/{m} and https://z.w/{m}",
    "(https://x.y/{m})",
    "_https://x.y/{m}",
]
PAYLOADS = [shape.format(m=mark) for shape in URL_SHAPES for mark in MARKS]


@pytest.mark.parametrize("payload", PAYLOADS, ids=[f"u{i}" for i in range(len(PAYLOADS))])
def test_markup_inside_url_shaped_text_is_inert_in_every_field_of_every_message_kind(payload):
    for field, args in _placements(payload):
        for surface in _surfaces(args):
            problems = _check(surface)
            assert not problems, (field, surface.where, payload, problems, surface.text)


def _digest_problems(payload: str) -> list[tuple[str, list[str], str]]:
    """The daily digest's two embed kinds with `payload` in every text field; findings, if any."""
    game = SimpleNamespace(key="g", name=payload[:80])
    story = StoryDraft(
        headline=payload,
        summary=payload,
        label="official",
        item_urls=["https://example.com/a"],
        update_of_story_id=None,
    )
    item = HeadlineItem(
        url="https://example.com/b",
        title=payload,
        source_name="s",
        trust="official",
        published_at=None,
        collected_at=NOW,
        uncertain=False,
    )
    found = []
    for stories_by_game, items_by_game, notes in (
        ({"g": [story]}, {}, {}),
        ({}, {"g": [item]}, {"g": payload}),
    ):
        digest = render_guild_digest(
            date(2026, 10, 9),
            [game],
            {"g": 1},
            stories_by_game=stories_by_game,
            items_by_game=items_by_game,
            notes_by_game=notes,
            coverage_notes=[],
        )
        [message] = digest.messages
        for part in (message.embed.title, message.embed.description):
            problems = _markup_problems(str(part), n_codes=None, allow_chips=False)
            if problems:
                found.append((payload, problems, str(part)))
    return found


@pytest.mark.parametrize("payload", PAYLOADS[::5], ids=[f"d{i}" for i in range(len(PAYLOADS[::5]))])
def test_markup_inside_url_shaped_text_is_inert_in_the_daily_digest(payload):
    assert not _digest_problems(payload)


def test_the_qa_finding_reproduction_is_closed_a_single_backtick_after_a_url():
    reward = "3 Golden Keys, see https://x.y/`"
    row = _row(_code(0), reward=reward, reward_from="mentalmars", expires_on=date(2026, 10, 29))
    line = details_line(row, now=NOW)
    assert chips(line) == [terminal.REWARD, terminal.EXPIRES, terminal.VIA]
    assert "\\`" in line
    assert "<t:" in line.split("`expires`")[1]  # the stamp is outside every chip


def test_the_qa_finding_reproduction_is_closed_three_backticks_after_a_url():
    candidates = [_cand(0), _cand(1)]
    reward = "see https://x.y/```"
    details = {
        c.code: _row(c.code, reward=reward, reward_from="mentalmars", expires_on=date(2026, 10, 29))
        for c in candidates
    }
    for alert in render_code_alerts(
        candidates, ping=True, details=details, now=NOW, reserve=details_reserve(["mentalmars"])
    ):
        assert len(fence_bodies(alert.content)) == len(alert.codes)
        assert alert.content.count("```") == 2 * len(alert.codes)
        assert all(is_code(b) for b in fence_bodies(alert.content))


@pytest.mark.parametrize(
    "text",
    [
        "https://x.y/`",
        "https://x.y/```",
        "<x:/a`b>",
        "steam://a||b",
        "http://a.b/~~c",
        "https://a.b/*c*",
        "https://a.b/_c_",
    ],
)
def test_esc_leaves_no_raw_markup_inside_a_url_and_loses_no_text(text):
    out = esc(text)
    for token in ("`", "||", "~~", "*", "_"):
        assert not _unescaped(out, token), (text, out)
    # Escaping adds backslashes and zero-width spaces and nothing else.
    assert out.replace("\\", "").replace("\u200b", "") == text


def test_esc_still_defuses_every_scheme_and_mention_it_defused_before():
    for text in ("https://evil.example", "_https://evil.example", "@everyone", "<#1>", "<t:1:R>"):
        out = esc(text)
        assert "://" not in out and "@everyone" not in out and "<#" not in out
        assert "<t:" not in out


# --- the checker has teeth: put the old behavior back and the matrix catches it ---


def test_the_parser_catches_the_old_escaping(monkeypatch):
    real = fmt.discord.utils.escape_markdown

    def old(text, *, as_needed=False, ignore_links=True):
        return real(text, as_needed=as_needed, ignore_links=True)

    monkeypatch.setattr(fmt.discord.utils, "escape_markdown", old)
    caught = 0
    for payload in ("3 Golden Keys, see https://x.y/`", "see https://x.y/```", "<x:/||>"):
        for _field, args in _placements(payload):
            caught += any(_check(s) for s in _surfaces(args))
    assert caught >= 3


def test_the_parser_catches_the_old_whitespace_handling(monkeypatch):
    monkeypatch.setattr(fmt, "_detail_text", lambda text: cap_shown(text.strip(), 300))
    row = _row(_code(0), reward="x\n  > q", reward_from="m")
    line = details_line(row, now=NOW)
    assert "\n" in line, "the old handling kept the newline"
    assert _every_line_opens_with_a_chip(line)


# --- the same attack, shaped like a markdown link (discord.py leaves the inside of one raw) ---

LINK_SHAPES = ["[a](b{m})", "[a{m}](b)", "[a](https://x.y/{m})", "x [a](b{m}) y", "[{m}]({m})"]
LINK_PAYLOADS = [s.format(m=m) for s in LINK_SHAPES for m in ("`", "```", "||", "~~", "*", "_")]


@pytest.mark.parametrize("payload", LINK_PAYLOADS, ids=[f"l{i}" for i in range(len(LINK_PAYLOADS))])
def test_markup_inside_a_markdown_link_shape_is_inert_too(payload):
    # `discord.utils.escape_markdown` escapes only the "[" of "[a](b`)" and passes the rest
    # through raw, so the backtick in it stays live. Same class as the batch's own finding;
    # `ignore_links=False` doesn't cover this regex.
    for field, args in _placements(payload):
        for surface in _surfaces(args):
            problems = _check(surface)
            assert not problems, (field, surface.where, payload, problems, surface.text)


def test_a_link_shaped_reward_with_one_backtick_cannot_close_a_chip():
    row = _row(
        _code(0), reward="3 Golden Keys [a](b`)", reward_from="m", expires_on=date(2026, 10, 29)
    )
    line = details_line(row, now=NOW)
    assert chips(line) == [terminal.REWARD, terminal.EXPIRES, terminal.VIA], line


def test_a_link_shaped_reward_with_three_backticks_cannot_open_a_fence():
    candidates = [_cand(0), _cand(1)]
    details = {c.code: _row(c.code, reward="[a](b```)", reward_from="m") for c in candidates}
    [alert] = render_code_alerts(
        candidates, ping=True, details=details, now=NOW, reserve=details_reserve(["m"])
    )
    assert alert.content.count("```") == 2 * len(candidates), alert.content


# --- 2. whitespace: one line, and never the start of a list or a quote ---

WS_REWARDS = [
    "a\nb",
    "a\r\nb",
    "a\rb",
    "a\tb",
    "a   b",
    "\n1. x",
    "\n  > q",
    "x\n1. y",
    "x\n  > q",
    "x\n> q",
    "x\n>>> q",
    "x\n- y",
    "x\n* y",
    "x\n# y",
    "x\n-# y",
    "x\n\n\ny",
    "\n\n",
    "\r\n- a\r\n- b\r\n",
    "x\x0b\x0cy",
    "x\x85y",
    "x\u2028y",
    "x\u2029y",
    "x\u00a0y",
    "x\u3000y",
    "x\n```\nfake\n```\ny",
    "x\n`reward` fake",
    "x\n@everyone",
    "line one\n\n" + "line two\n" * 50,
]


@pytest.mark.parametrize("reward", WS_REWARDS, ids=[f"w{i}" for i in range(len(WS_REWARDS))])
def test_whitespace_in_a_reward_or_origin_renders_on_one_line_in_every_message_kind(reward):
    # Reward and origin only: a source name with a newline in it can't get past config
    # validation ("has a control character"), which is why `_via_name` doesn't collapse.
    quiet = dict.fromkeys(_FIELD_NAMES, "ok")
    for field, args in (
        ("reward", quiet | {"reward": reward}),
        ("origin", quiet | {"origin": reward}),
        ("both", quiet | {"reward": reward, "origin": reward}),
    ):
        for surface in _surfaces(args):
            problems = _check(surface)
            assert not problems, (field, surface.where, reward, problems, surface.text)
            if surface.layout in ("look", "classic"):
                # An alert is a header, then one blank-line-separated block per code, each of
                # them fence (3), details (0 or 1) and src (1; roundups and follow-ups have none).
                blocks = surface.text.split("\n\n")
                assert len(blocks) == 1 + surface.n_codes, (field, surface.where, reward)
                for block in blocks[1:]:
                    lines = len(block.split("\n"))
                    assert lines in surface.block_lines, (field, surface.where, block)
            assert "\r" not in surface.text and "\t" not in surface.text
            assert "\u2028" not in surface.text and "\u2029" not in surface.text


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("a\nb", "a b"),
        ("a\r\nb", "a b"),
        ("a\tb", "a b"),
        ("  a   b  ", "a b"),
        ("\n1. x", "1. x"),
        ("\n  > q", "> q"),
        ("a\n> b", "a > b"),
        ("x\n\n\ny", "x y"),
        ("\n\n", ""),
    ],
)
def test_detail_text_is_one_line_with_single_spaces(raw, shown):
    assert _detail_text(raw) == shown


def test_the_reward_that_started_a_list_is_inline_text_now():
    row = _row(_code(0), reward="\n1. x", reward_from="m")
    assert details_line(row, now=NOW) == "`reward` 1. x · `via` m"
    row = _row(_code(0), reward="\n  > q", reward_from="m")
    # The ">" is a line's first character now, which is what esc() escapes, harmlessly.
    assert details_line(row, now=NOW) == "`reward` \\> q · `via` m"


def test_whitespace_collapse_applies_to_the_origin_on_shift_codes():
    code = _code(0)
    row = _row(code, category="permanent", reward="r", origin="Vault\n1. x\n  > q\t\tyes")
    [page] = render_current_pages(
        [CurrentCode(code=code, first_seen_at=NOW, details=row)],
        kind="all",
        now=NOW,
        footer="f",
        empty="e",
    )
    text = str(page.description)
    assert text.endswith("Vault 1. x > q yes")  # mid-line, so nothing to escape
    assert not _markup_problems(text, n_codes=1) and not _every_line_opens_with_a_chip(text)


def test_a_reward_of_only_whitespace_is_no_reward_at_all():
    for blank in ("", " ", "\n", "\r\n\t ", "\u3000"):
        row = _row(_code(0), reward=blank, reward_from="m")
        assert details_line(row, now=NOW) is None
        with_date = _row(_code(0), reward=blank, reward_from="m", expires_on=date(2026, 10, 14))
        assert "`reward`" not in details_line(with_date, now=NOW)


# --- 3. budgets ---


def _units(name: str) -> int:
    return discord_len(esc(name[:100]))


def _linky(piece: str, last: str) -> str:
    """A 100-character name made of URL-shaped pieces with markup in them, `last` as its tail."""
    body = (piece * 100)[:99]
    return body + last


def _widest(name: str) -> int:
    return _units(name)


NAME_PAIRS = [
    (_linky("https://x.y/`", "a"), _linky("https://x.y/`", "b")),
    (_linky("<x:/`|`>", "a"), _linky("<x:/`|`>", "b")),
    (_linky("steam://a_b*c", "a"), _linky("steam://a_b*c", "b")),
    (_linky("https://||~~", "a"), _linky("https://||~~", "b")),
    (_linky("<ftp://a/```>", "a"), _linky("<ftp://a/```>", "b")),
    ("https://" * 12 + "xxxx", "https://" * 12 + "yyyy"),
    ("m", "n"),
]


@pytest.mark.parametrize("pair", NAME_PAIRS, ids=[f"n{i}" for i in range(len(NAME_PAIRS))])
def test_the_reserve_is_still_exactly_the_worst_line_plus_one_with_url_shaped_names(pair):
    a, b = pair
    assert _units(a) == _units(b), "a pair has to be equal in shown units to be a worst case"
    row = _row(
        "x",
        reward="r" * 300,
        reward_from=a,
        expires_on=date(5000, 12, 31),
        expires_from=b,
    )
    line = details_line(row, now=NOW)
    assert "(<t:95649148800:R>)" in line
    assert discord_len(line) + 1 == details_reserve([a, b])
    assert details_reserve([a, b]) == 369 + 2 * _units(a)


def test_the_reserve_is_still_exactly_tight_with_a_url_shaped_reward_at_the_cap():
    # A reward whose shown form is as wide as 300 allows: the line meets the reserve exactly,
    # with whitespace collapse and the new escaping both in play.
    name = "m"
    for reward in (
        "https://x.y/*" * 80,
        "see https://x.y/`" * 40,
        "<x:/_>" * 100,
        "steam://a|b " * 50,
        "a\n\n" + "https://x.y/~~ " * 60,
    ):
        shown = _detail_text(reward)
        assert discord_len(esc(shown)) <= 300, reward
        row = _row(
            "x",
            reward=reward,
            reward_from=name,
            expires_on=date(5000, 12, 31),
            expires_from=name,
        )
        assert discord_len(details_line(row, now=NOW)) + 1 <= details_reserve([name])


@pytest.mark.parametrize("limit", [300, 100, 50, 10, 3, 1])
def test_cap_shown_still_promises_its_limit_on_the_escaped_form(limit):
    rng = random.Random(limit)  # noqa: S311
    bits = [
        "https://x.y/",
        "`",
        "||",
        "~~",
        "*",
        "_",
        "<x:/",
        ">",
        "a",
        " ",
        "\\",
        "😀",
        "steam://",
    ]
    for _ in range(300):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(0, 200)))
        got = cap_shown(text, limit)
        assert discord_len(esc(got)) <= limit, (text, got)
        if discord_len(esc(text)) <= limit:
            assert got == text


def test_cap_shown_cuts_a_url_shaped_string_where_the_escaped_form_stops_fitting():
    text = "https://x.y/`" * 60
    got = cap_shown(text, 300)
    assert got.endswith("…") and discord_len(esc(got)) <= 300
    # One more raw character would not have fit: the cut is as late as the limit allows.
    longer = text[: len(got) + 1].rstrip() + "…"
    assert discord_len(esc(longer)) > 300 or len(longer) <= len(got)


def test_detail_text_stays_within_300_shown_units_for_hostile_whitespace_and_urls():
    rng = random.Random(1022)  # noqa: S311
    bits = ["https://x.y/`", "\n", "  ", "\t", "||", "~~", "*", "_", "<x:/", ">", "a", "\\", "😀"]
    for _ in range(400):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(0, 400)))
        assert discord_len(esc(_detail_text(text))) <= 300, text


def test_collapsing_whitespace_only_ever_shrinks_the_shown_text():
    rng = random.Random(7)  # noqa: S311
    bits = ["a", "bb", " ", "\n", "\t", "\r\n", "  ", "https://x.y/`", "`"]
    for _ in range(300):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(0, 120)))
        collapsed = " ".join(text.split())
        assert discord_len(esc(collapsed)) <= discord_len(esc(text.strip())) + 2 * text.count("\n")
        assert len(collapsed) <= len(text)


_FUZZ = [
    "",
    "3 Golden Keys",
    "see https://x.y/`" * 10,
    "https://x.y/```",
    "<x:/`|`>" * 30,
    "steam://a_b*c " * 25,
    "https://||~~ " * 30,
    "a\n> b\n1. c\n" * 30,
    "\t\t \n\n" * 40,
    "<t:1:R> https://x.y/_ " * 15,
    "x" * 600,
    "😀`" * 100,
]


def test_a_seeded_fuzz_never_beats_the_reserve_with_the_new_escaping():
    rng = random.Random(20261022)  # noqa: S311
    for _ in range(600):
        rf, ef, cf = (rng.choice(_FUZZ)[:100] for _ in range(3))
        row = _row(
            "x",
            reward=rng.choice(_FUZZ),
            reward_from=rf,
            category="permanent" if rng.random() < 0.3 else None,
            category_from=cf,
            expires_on=rng.choice([None, date(2026, 10, 14), date(5000, 12, 31)]),
            expires_from=ef,
            origin=rng.choice(_FUZZ),
        )
        line = details_line(row, now=NOW)
        if line is not None:
            assert discord_len(line) + 1 <= details_reserve([rf, ef, cf]), (row, line)


ROLES = [(False, None), (True, None), (True, ROLE), (True, ROLE_20)]


@pytest.mark.parametrize("pair", NAME_PAIRS[:6], ids=[f"p{i}" for i in range(6)])
@pytest.mark.parametrize(("ping", "mention"), ROLES, ids=["quiet", "everyone", "role", "role20"])
@pytest.mark.parametrize("test", [False, True], ids=["real", "test"])
def test_pack_then_the_worst_edit_still_fits_with_every_link_under_the_new_escaping(
    pair, ping, mention, test
):
    names = list(pair)
    reserve = details_reserve(names)
    batch = [
        _cand(
            i,
            golden=i % 3 == 0,
            source_name=names[i % 2],
            item_url="https://example.com/" + "a" * 120,
        )
        for i in range(30)
    ]
    by_code = {c.code: c for c in batch}
    alerts = render_code_alerts(
        batch, ping=ping, ping_mention=mention, test=test, reserve=reserve, nonce_scope="1|2"
    )
    assert [c for a in alerts for c in a.codes] == [c.code for c in batch]
    reward = ("see https://x.y/`*_ " * 40) + "\n\n  \t" + ("https://a.b/||~~ " * 40)
    worst = {
        c.code: _row(
            c.code,
            reward=reward,
            reward_from=names[0],
            expires_on=date(5000, 12, 31),
            expires_from=names[1],
        )
        for c in batch
    }
    for a in alerts:
        assert discord_len(a.content) <= 2000
        members = [by_code[c] for c in a.codes]
        edited = render_alert_edit(
            a.header, members, golden_prefix=a.golden_prefix, details=worst, now=NOW
        )
        assert discord_len(edited) <= 2000
        assert edited.count(f" · <{URL.rsplit('/', 1)[0]}/") == len(members), "a link was shed"
        assert not _markup_problems(edited, n_codes=len(members))


def test_the_reserve_still_leaves_room_for_the_worst_edit_of_a_classic_header_too():
    # A pre-3.2.0 message is edited in the classic layout; the new escaping must not push it
    # over what the terminal-layout reserve set aside, because that is the bigger of the two.
    names = [_linky("https://x.y/`", "a"), _linky("https://x.y/`", "b")]
    reserve = details_reserve(names)
    batch = [_cand(i, source_name=names[0], item_url=URL + "/" + "p" * 100) for i in range(20)]
    alerts = render_code_alerts(batch, ping=True, reserve=reserve)
    by_code = {c.code: c for c in batch}
    worst = {
        c.code: _row(
            c.code,
            reward="https://x.y/`*_ " * 40,
            reward_from=names[0],
            expires_on=date(5000, 12, 31),
            expires_from=names[1],
        )
        for c in batch
    }
    for a in alerts:
        members = [by_code[c] for c in a.codes]
        for header in (CLASSIC_HEADER, "@everyone " + CLASSIC_HEADER):
            edited = render_alert_edit(header, members, golden_prefix=False, details=worst, now=NOW)
            assert discord_len(edited) <= 2000
            assert edited.count(" · <https://") == len(members)


def test_followup_packing_is_consistent_with_the_rendered_size():
    codes = [_code(i) for i in range(20)]
    details = {
        c: _row(
            c,
            reward="see https://x.y/`*_ " * 15,
            reward_from="https://x.y/`" * 7,
            expires_on=date(2026, 10, 14),
            expires_from="m",
        )
        for c in codes
    }
    alerts = render_followup_alerts(codes, ping_mention=ROLE_20, details=details, now=NOW)
    assert [c for a in alerts for c in a.codes] == codes
    assert all(0 < len(a.codes) <= 20 for a in alerts)
    for a in alerts:
        assert discord_len(a.content) <= 2000
        assert not _markup_problems(a.content, n_codes=len(a.codes))
    # Each message closed because the next code really would not have fit under it.
    for here, after in zip(alerts, alerts[1:], strict=False):
        entry = after.content.split("\n\n")[1]
        assert discord_len(here.content) + 2 + discord_len(entry) > 2000


def test_pages_stay_within_the_embed_limit_with_worst_case_details():
    reward = "see https://x.y/`*_ " * 30
    entries = []
    for i in range(40):
        code = _code(i)
        row = _row(
            code,
            reward=reward,
            reward_from="https://x.y/`" * 7,
            category="permanent",
            origin="https://x.y/_ " * 40,
        )
        entries.append(CurrentCode(code=code, first_seen_at=NOW, details=row))
    pages = render_current_pages(entries, kind="all", now=NOW, footer="f", empty="e")
    assert len(pages) > 1
    for embed in pages:
        assert discord_len(str(embed.description)) <= 4096
        text = str(embed.description)
        assert not _markup_problems(text, n_codes=text.count("```") // 2)
        assert not _every_line_opens_with_a_chip(text)


# --- the constant and its derivation ---


def test_the_expiry_value_max_is_still_32_and_is_the_sum_of_its_parts():
    assert _EXPIRY_VALUE_MAX == 32
    parts = (
        len("Sep 30, 2026") + len(fmt._STAMP_OPEN) + fmt._STAMP_DIGITS_MAX + len(fmt._STAMP_CLOSE)
    )
    assert _EXPIRY_VALUE_MAX == parts == len("Sep 30, 2026 (<t:") + 11 + len(":R>)")


def test_the_expiry_value_max_has_no_hand_typed_number_in_its_definition():
    source = inspect.getsource(fmt)
    [definition] = [ln for ln in source.splitlines() if ln.startswith("_EXPIRY_VALUE_MAX")]
    assert not re.search(r"=\s*\d+\s*$", definition), definition
    for part in ("_STAMP_OPEN", "_STAMP_DIGITS_MAX", "_STAMP_CLOSE"):
        assert part in definition, definition


def test_the_reserve_with_no_names_is_still_369_and_is_the_sum_of_its_parts():
    assert details_reserve([]) == 369
    parts = [
        1,
        len("`reward` "),
        300,
        len(" · "),
        len("`expires` "),
        _EXPIRY_VALUE_MAX,
        len(" · "),
        len("`via` "),
        len(" and "),
    ]
    assert sum(parts) == 369


# =====================================================================================
# Round 2: the round-1 fix to esc() (cut at "](", escape the pieces, rejoin with "\\](")
# =====================================================================================

LINK_EDGE = [
    "[[a](b)](c)",
    "[a](b)(c)",
    "[a](b)[c](d)",
    "[a](b) [c](d`)",
    "](",
    "a](",
    "](a",
    "](`",
    "`](",
    "x](y](z`",
    "][](",
    "[](]",
    "[a]()",
    "[](a)",
    "[a] (b`)",
    "[a]( b` )",
    "\\](",
    "\\](`",
    "[a\\](b`)",
    "\\[a](b`)",
    "[a\\\\](b`)",
    "[" * 40 + "](`" + ")" * 40,
    "](" * 150,
    "[a](b`)" * 50,
    "](`" * 100,
    "[" + "](`" * 100 + "]",
    "[a](https://x.y/`)](https://z.w/||)",
    "[a](<x:/`>)",
    "[@everyone](<#1>)",
    "[a](b)" + "\u200b" + "(c`)",
]


def _unescape(out: str) -> str:
    """What Discord draws for `out`: a backslash makes the next character literal and vanishes."""
    shown, i = [], 0
    while i < len(out):
        if out[i] == "\\" and i + 1 < len(out):
            i += 1
        shown.append(out[i])
        i += 1
    return "".join(shown)


@pytest.mark.parametrize("text", LINK_EDGE, ids=[f"e{i}" for i in range(len(LINK_EDGE))])
def test_esc_of_link_shapes_leaves_nothing_live_and_draws_the_same_words(text):
    out = esc(text)
    for token in ("`", "||", "~~", "*", "_", "]("):
        assert not _unescaped(out, token), (text, out, token)
    # One backslash per mark and no more: unescaping gives the input back (minus zero-width
    # spaces, which the mention and scheme defusing add and which draw as nothing).
    assert _unescape(out).replace("\u200b", "") == text.replace("\u200b", ""), (text, out)


def test_esc_rejoins_with_one_escaped_bracket_and_adds_no_backslash_runs():
    assert esc("[a](b`)") == "\\[a\\](b\\`)"
    assert esc("](") == "\\]("
    assert esc("a](") == "a\\]("
    assert esc("[a](b)(c)") == "\\[a\\](b)(c)"
    assert esc("[[a](b)](c)") == "\\[\\[a\\](b)\\](c)"
    # Already escaped input: the user's backslash is doubled and the bracket still gets its own.
    assert esc("\\](") == "\\\\\\]("
    assert esc("[a\\](b)") == "\\[a\\\\\\](b)"


def test_esc_of_a_link_shape_that_spans_lines_is_inert_too():
    out = esc("[a](b)\n[c](d`)\n> [e](f||)")
    for token in ("`", "||", "]("):
        assert not _unescaped(out, token), out
    assert _unescape(out) == "[a](b)\n[c](d`)\n> [e](f||)"


def test_esc_does_not_touch_brackets_that_are_not_a_link_shape():
    for text in ("[a] (b)", "[a]", "(b)", "[a]b(c)", "a [b] c", "]", "(", "[", "]["):
        assert esc(text) == text, text


def test_esc_link_shape_fuzz_keeps_the_invariants_and_the_round_trip():
    rng = random.Random(20261023)  # noqa: S311
    bits = ["[", "]", "(", ")", "](", "[a](b)", "`", "``", "\\", "||", "~~", "*", "_", "a", " "]
    bits += ["https://x.y/", "<x:/", ">", "😀", "\u200b", "\n"]
    for _ in range(3000):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(0, 40)))
        out = esc(text)
        for token in ("`", "||", "~~", "*", "_", "]("):
            assert not _unescaped(out, token), (text, out, token)
        assert "://" not in out
        assert _unescape(out).replace("\u200b", "") == text.replace("\u200b", ""), (text, out)


@pytest.mark.parametrize("payload", LINK_EDGE, ids=[f"m{i}" for i in range(len(LINK_EDGE))])
def test_link_shape_edges_are_inert_in_every_field_of_every_message_kind(payload):
    for field, args in _placements(payload):
        for surface in _surfaces(args):
            problems = _check(surface)
            assert not problems, (field, surface.where, payload, problems, surface.text)


@pytest.mark.parametrize("payload", LINK_EDGE, ids=[f"g{i}" for i in range(len(LINK_EDGE))])
def test_link_shape_edges_are_inert_in_the_daily_digest(payload):
    assert not _digest_problems(payload)


@pytest.mark.parametrize("payload", LINK_EDGE, ids=[f"q{i}" for i in range(len(LINK_EDGE))])
def test_link_shape_edges_are_inert_in_the_lounge_quote(payload):
    from newsbot.lounge.quotes import ATTRIBUTION_PREFIX, QUOTE_HEADER, Quote, render_quote_message

    text = " ".join(payload.split())  # the lounge keeps the quote's own lines; one line here
    for quote in (Quote(text), Quote("fine", attribution=payload), Quote(text, attribution=text)):
        message = render_quote_message(quote)
        body = message.removeprefix(QUOTE_HEADER + "\n").replace(ATTRIBUTION_PREFIX, "", 1)
        problems = _markup_problems(body, n_codes=None, allow_chips=False)
        assert not problems, (payload, problems, message)


def test_a_masked_link_in_a_reward_reads_as_text_with_one_backslash_per_bracket():
    row = _row(_code(0), reward="see [the post](https://evil.example/x)", reward_from="m")
    line = details_line(row, now=NOW)
    assert line == "`reward` see \\[the post\\](https:\u200b//evil.example/x) · `via` m"


# --- round 2 budgets: the "](" cut makes esc() less additive, so re-check what measures it ---


def _ladder(piece: str, tail: str) -> str:
    return (piece * 100)[:98] + tail


LINK_NAME_PAIRS = [
    ("](" * 49 + "]a", "](" * 49 + "]b"),
    (_ladder("[a](b`)", "xa"), _ladder("[a](b`)", "xb")),
    (_ladder("](`", "xa"), _ladder("](`", "xb")),
    (_ladder("[[](", "xa"), _ladder("[[](", "xb")),
    ("\\](" * 33 + "a", "\\](" * 33 + "b"),
]


@pytest.mark.parametrize(
    "pair", LINK_NAME_PAIRS, ids=[f"k{i}" for i in range(len(LINK_NAME_PAIRS))]
)
def test_the_reserve_is_still_exactly_tight_with_link_shaped_names(pair):
    a, b = pair
    assert len(a) <= 100 and len(b) <= 100
    assert _units(a) == _units(b)
    row = _row("x", reward="r" * 300, reward_from=a, expires_on=date(5000, 12, 31), expires_from=b)
    line = details_line(row, now=NOW)
    assert discord_len(line) + 1 == details_reserve([a, b]) == 369 + 2 * _units(a)


@pytest.mark.parametrize(
    "pair", LINK_NAME_PAIRS, ids=[f"kp{i}" for i in range(len(LINK_NAME_PAIRS))]
)
@pytest.mark.parametrize(("ping", "mention"), ROLES, ids=["quiet", "everyone", "role", "role20"])
def test_pack_then_the_worst_edit_still_fits_with_every_link_and_link_shaped_text(
    pair, ping, mention
):
    names = list(pair)
    reserve = details_reserve(names)
    batch = [
        _cand(i, golden=i % 3 == 0, source_name=names[i % 2], item_url=URL + "/" + "a" * 120)
        for i in range(30)
    ]
    by_code = {c.code: c for c in batch}
    alerts = render_code_alerts(batch, ping=ping, ping_mention=mention, reserve=reserve)
    reward = "[a](b`)](" * 60
    worst = {
        c.code: _row(
            c.code,
            reward=reward,
            reward_from=names[0],
            expires_on=date(5000, 12, 31),
            expires_from=names[1],
        )
        for c in batch
    }
    for a in alerts:
        members = [by_code[c] for c in a.codes]
        edited = render_alert_edit(
            a.header, members, golden_prefix=a.golden_prefix, details=worst, now=NOW
        )
        assert discord_len(edited) <= 2000
        assert edited.count(f" · <{URL}/") == len(members), "a link was shed"
        assert not _markup_problems(edited, n_codes=len(members))
        classic = render_alert_edit(
            CLASSIC_HEADER, members, golden_prefix=False, details=worst, now=NOW
        )
        assert discord_len(classic) <= 2000


@pytest.mark.parametrize("limit", [300, 100, 50, 10, 4, 1])
def test_cap_shown_still_promises_its_limit_with_link_shapes_in_the_text(limit):
    rng = random.Random(limit + 1000)  # noqa: S311
    bits = ["](", "[a](b`)", "[", "]", "(", ")", "`", "\\", "a", " ", "😀", "https://x.y/", "||"]
    for _ in range(400):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(0, 200)))
        got = cap_shown(text, limit)
        assert discord_len(esc(got)) <= limit, (text, got)
        if discord_len(esc(text)) <= limit:
            assert got == text


def test_cap_shown_does_not_cut_a_link_shape_into_a_live_one():
    # Cutting between "]" and "(" or after "[a](" leaves a different shape; esc() must cope
    # with every cut point, not just the whole string.
    text = "[a](b`)" * 80
    for limit in range(2, 120):
        got = cap_shown(text, limit)
        out = esc(got)
        assert discord_len(out) <= limit
        for token in ("`", "]("):
            assert not _unescaped(out, token), (limit, got, out)


def test_detail_text_stays_within_300_shown_units_for_link_shapes():
    rng = random.Random(1023)  # noqa: S311
    bits = ["](", "[a](b`)", "[", "]", "\n", "  ", "`", "\\", "a", "😀", "https://x.y/`"]
    for _ in range(400):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(0, 300)))
        assert discord_len(esc(_detail_text(text))) <= 300, text


def test_a_long_run_of_link_openers_is_linear_and_fits_the_cap():
    # "](" x 5000: esc() splits and joins, it must not go quadratic or blow the 300 cap.
    text = "](" * 5000
    shown = _detail_text(text)
    assert discord_len(esc(shown)) <= 300
    assert shown.endswith("…")


def test_pages_stay_within_the_embed_limit_with_link_shaped_worst_case_details():
    entries = []
    for i in range(40):
        code = _code(i)
        row = _row(
            code,
            reward="[a](b`)](" * 40,
            reward_from="](`" * 33,
            category="permanent",
            origin="[x](y_)" * 40,
        )
        entries.append(CurrentCode(code=code, first_seen_at=NOW, details=row))
    pages = render_current_pages(entries, kind="all", now=NOW, footer="f", empty="e")
    assert len(pages) > 1
    for embed in pages:
        text = str(embed.description)
        assert discord_len(text) <= 4096
        assert not _markup_problems(text, n_codes=text.count("```") // 2)
        assert not _every_line_opens_with_a_chip(text)
