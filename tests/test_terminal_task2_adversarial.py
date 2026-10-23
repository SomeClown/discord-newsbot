"""Adversarial round on v3.2.0 Task 2: SHiFT alerts, edits and details lines in the terminal look.

The builder's own file (`test_format_terminal_alerts.py`) pins the happy shapes and runs a
couple dozen hostile strings through every surface. This one leans harder on the places the
look can actually break:

1. Chips. Rewards, source names and credit names built from every character that can close a
   chip, open a fence, forge a ping or forge a timestamp, each placed in a different field
   (`reward`, `reward_from`, `expires_from`, `category_from`, the `src` name), so one lucky
   field can't hide a leaky one. The check is `chip_check.chips()` (a parser of its own)
   against `ALLOWED_CHIPS`, plus the structural rules a chip can't see: no unescaped
   `@everyone`, no live `<t:` except the bot's own, no line that opens a quote or a heading.
2. The reserve. `369 + 2L` against the real worst line for several name sets (markdown,
   backslashes, astral emoji, names over 100 characters), with a seeded fuzz underneath.
3. Pack then edit. Messages packed with the real reserve, then edited with the worst details,
   with and without a ping (a role mention at its longest), `[test]`, golden prefixes, and a
   header the ping was stripped from. Never over 2,000 units, links kept while the reserve
   was honored.
4. Batches of 1, 2, 5, 6 and 50 codes: header kind and plural, continuations, ping on the first
   message only, nonces that match v3.1.4's (read out of git, like the Task 1 round does).
5. What must not move: `displayed()` (a v3.1-era record must not become due), `/shift codes`
   entries (the same facts as v3.1.4, laid out by Task 4), and the nonce formula.

If git or the base commit isn't around (a shallow clone, a tarball), the differential tests skip
with a reason; the literal pins and the formula checks still run everywhere.
"""

from __future__ import annotations

import hashlib
import random
import re
import subprocess
import sys
import types
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import product
from pathlib import Path

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies
from test_shift_edits import EditHarness, message_state
from test_shift_fanout import CODE_A, CODE_B, V3, add_guild, item, seed
from test_shift_fanout import NOW as FANOUT_NOW

from newsbot.bot import terminal
from newsbot.bot.format import (
    _current_entry,
    details_line,
    details_reserve,
    discord_len,
    esc,
    render_alert_edit,
    render_code_alerts,
    render_current_pages,
)
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.details import displayed
from newsbot.shift.match import is_code
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow, CurrentCode

REPO = Path(__file__).resolve().parent.parent
BASE_COMMIT = "1fcd04b"  # v3.1.4's tree, the base of this branch
NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # noon PDT, Oct 9
ROLE_20 = "<@&" + "9" * 20 + ">"  # a snowflake at its longest
ROLE_18 = "<@&123456789012345678>"
BIDI = "‮⁦⁩‪"


# --- the v3.1.4 modules, read out of git as throwaway modules ---


def _load_old(path: str, name: str):
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{BASE_COMMIT}:{path}"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {BASE_COMMIT} is not available here")
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module  # dataclasses look their module up by name
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def old_format():
    yield from _load_old("newsbot/bot/format.py", "_newsbot_format_v314_t2")


@pytest.fixture(scope="module")
def old_details():
    yield from _load_old("newsbot/shift/details.py", "_newsbot_details_v314_t2")


# --- builders ---


def _code(i: int = 0) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _cand(i: int = 0, **kwargs) -> CodeCandidate:
    defaults = dict(
        code=_code(i),
        golden=False,
        source_name="Gearbox Blog",
        item_url=f"https://e.com/a{i}",
        fresh=True,
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


def _row(code: str, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code, clock_start=NOW, updated_at=NOW)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _every_message(alerts):
    return [(f"message {n}", a.content) for n, a in enumerate(alerts)]


# --- 1. chips and structure, against hostile text in each field ---

POISON = [
    "`",
    "``",
    "```",
    "\\`",
    "\\\\`",
    "\\`\\`\\`",
    "``​`",
    "`\n`",
    "`reward`",
    "`src` evil",
    "`> new SHiFT code ▌`",
    "▌",
    "▌`",
    "> at the start",
    ">>> triple",
    "line one\n> quote after a newline",
    "line one\n# heading after a newline",
    "line one\n-# subtext after a newline",
    "line one\n- bullet after a newline",
    "line one\n```\n" + _code(7) + "\n```\nfake fence with a real-looking code",
    "\\",
    "ends in a backslash \\",
    "@everyone",
    "@here",
    "@​everyone",
    "<@&1>",
    "<@1>",
    "<@&123456789012345678>",
    "<@!123456789012345678>",
    "<@12345678901234567890>",
    "@123456789012345678",
    "<#1>",
    "</cmd:1>",
    "<t:1:R>",
    "<T:1:R>",
    "<t:1792047600:R> `expires`",
    "<a:x:1>",
    "<id:browse>",
    "https://evil.example/x",
    "_https://evil.example/x",
    "discord.gg/abc",
    "[click](http://evil.example)",
    f"{BIDI}`{BIDI}",
    f"{BIDI}@everyone",
    "\U0001f600`\U0001f600",
    "\U0001f469‍\U0001f4bb`",
    "x" * 290 + "`" * 20,
    "`" * 400,
    "*" * 400,
    "\\" * 400,
    "\U0001f600" * 200,
]


def _renderings(reward: str, rf: str, ef: str, cf: str, src: str) -> list[tuple[str, str]]:
    """Every alert and edit shape with a different piece of hostile text in each scraped field."""
    src = (src or "s")[:100]
    plain = _cand(0, source_name=src)
    golden = _cand(1, golden=True, source_name=src)
    details = {
        plain.code: _row(
            plain.code,
            reward=reward,
            reward_from=(rf or "r")[:100],
            expires_on=date(2026, 10, 14),
            expires_from=(ef or "e")[:100],
        ),
        golden.code: _row(
            golden.code,
            reward=reward,
            reward_from=(rf or "r")[:100],
            category="permanent",
            category_from=(cf or "c")[:100],
        ),
    }
    names = [(x or "n")[:100] for x in (rf, ef, cf, src)]
    reserve = details_reserve(names)
    out: list[tuple[str, str]] = []
    by_code = {plain.code: plain, golden.code: golden}
    for batch, ping, mention, test in (
        ([plain, golden], False, None, False),
        ([plain, golden], True, None, False),
        ([plain, golden], True, ROLE_20, True),
        ([golden], False, None, False),
        ([plain], True, ROLE_18, True),
    ):
        kw = dict(ping=ping, ping_mention=mention, test=test, reserve=reserve)
        shown = render_code_alerts(batch, details=details, now=NOW, **kw)
        bare = render_code_alerts(batch, **kw)
        out += [(f"alert {n}", a.content) for n, a in enumerate(shown)]
        for n, a in enumerate(bare):
            for header in (a.header, a.header.removeprefix(a.ping_prefix)):
                out.append(
                    (
                        f"edit {n}",
                        render_alert_edit(
                            header,
                            [by_code[c] for c in a.codes],
                            golden_prefix=a.golden_prefix,
                            details=details,
                            now=NOW,
                        ),
                    )
                )
    return out


def _fields(text: str):
    """`text` in one scraped field at a time, harmless text everywhere else."""
    harmless = "ok"
    yield "reward", (text, harmless, harmless, harmless, harmless)
    yield "reward_from", (harmless, text, harmless, harmless, harmless)
    yield "expires_from", (harmless, harmless, text, harmless, harmless)
    yield "category_from", (harmless, harmless, harmless, text, harmless)
    yield "source", (harmless, harmless, harmless, harmless, text)
    yield "everywhere", (text,) * 5


@pytest.mark.parametrize("text", POISON, ids=[f"p{i}" for i in range(len(POISON))])
def test_no_field_can_put_anything_in_a_chip_or_a_fence(text):
    for field, args in _fields(text):
        for where, content in _renderings(*args):
            got = set(chips(content))
            assert got <= ALLOWED_CHIPS, (field, where, got - ALLOWED_CHIPS)
            for body in fence_bodies(content):
                assert is_code(body), (field, where, body)
            assert all("<t:" not in chip for chip in chips(content)), (field, where)


@pytest.mark.parametrize("text", POISON, ids=[f"p{i}" for i in range(len(POISON))])
def test_every_chip_a_message_has_is_one_the_look_puts_there_and_in_the_right_place(text):
    # Beyond "in the allowed set": a detail line's chips are labels, in label order, and the
    # src chip appears once per code. A stray extra label would still be in ALLOWED_CHIPS.
    label_order = [terminal.REWARD, terminal.EXPIRES, terminal.VIA]
    for field, args in _fields(text):
        for where, content in _renderings(*args):
            found = chips(content)
            header_chips = [c for c in found if c.startswith("> ")]
            assert len(header_chips) == 1, (field, where, found)
            assert found[0] == header_chips[0], (field, where, found)
            codes = len(fence_bodies(content))
            assert found.count(terminal.SRC) == codes, (field, where, found)
            entry: list[str] = []
            for c in found[1:]:
                if c == terminal.SRC:
                    positions = [label_order.index(x) for x in entry]
                    assert positions == sorted(set(positions)), (field, where, found)
                    entry = []
                else:
                    entry.append(c)
            assert entry == [], (field, where, found)
            assert found.count(terminal.REWARD) <= codes, (field, where, found)
            assert found.count(terminal.EXPIRES) <= codes, (field, where, found)
            assert found.count(terminal.VIA) <= codes, (field, where, found)


@pytest.mark.parametrize("text", POISON, ids=[f"p{i}" for i in range(len(POISON))])
def test_scraped_text_never_arrives_as_a_live_ping_or_a_live_timestamp(text):
    for field, args in _fields(text):
        for where, content in _renderings(*args):
            header, _, body = content.partition("\n\n")
            # The header's own ping is the only mention; everything under it is plain text.
            assert not re.search(r"@(everyone|here)", body), (field, where)
            assert not re.search(r"<@[!&]?\d{17,20}", body), (field, where)
            assert "<#" not in body and "</" not in body, (field, where)
            # The bot's own stamp is the one live timestamp, and only for the Oct 14 expiry.
            stamps = re.findall(r"<t:([^>]*)>", body)
            assert set(stamps) <= {"1792047600:R"}, (field, where, stamps)
            assert not re.search(r"https?://[^\s<>]*evil", body), (
                field,
                where,
            )
            assert "<t:" not in header


@pytest.mark.parametrize("text", POISON, ids=[f"p{i}" for i in range(len(POISON))])
def test_no_line_of_a_rendered_message_opens_a_quote_a_heading_or_a_list(text):
    for field, args in _fields(text):
        for where, content in _renderings(*args):
            outside_fences = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
            for line in outside_fences.split("\n"):
                assert not re.match(r">|#|-#|- |\* |\+ ", line), (field, where, line)


@pytest.mark.parametrize("text", POISON, ids=[f"p{i}" for i in range(len(POISON))])
def test_a_reward_leaves_no_bidi_control_on_the_details_line(text):
    row = _row("x", reward=text, reward_from="m", expires_on=date(2026, 10, 14), expires_from="m")
    line = details_line(row, now=NOW) or ""
    assert not re.search("[‪-‮⁦-⁩]", line)


def test_a_reward_made_only_of_bidi_controls_has_no_line_and_no_empty_reward_chip():
    row = _row("x", reward=BIDI, reward_from="m")
    assert details_line(row, now=NOW) is None
    with_date = _row("x", reward=BIDI, reward_from="m", expires_on=date(2026, 10, 14))
    line = details_line(with_date, now=NOW)
    assert line is not None and "`reward`" not in line


def test_a_name_that_hits_the_via_cap_is_cut_with_an_ellipsis_and_stays_a_value():
    row = _row("x", reward="r", reward_from="`" * 150)
    line = details_line(row, now=NOW)
    assert chips(line) == [terminal.REWARD, terminal.VIA]
    assert line.endswith("…")


def test_the_details_line_has_exactly_the_labels_it_has_parts_for():
    both = _row("x", reward="r", reward_from="m", expires_on=date(2026, 10, 14), expires_from="m")
    assert chips(details_line(both, now=NOW)) == ["reward", "expires", "via"]
    reward_only = _row("x", reward="r", reward_from="m")
    assert chips(details_line(reward_only, now=NOW)) == ["reward", "via"]
    date_only = _row("x", expires_on=date(2026, 10, 14), expires_from="m")
    assert chips(details_line(date_only, now=NOW)) == ["expires", "via"]
    nameless = _row("x", reward="r")
    assert chips(details_line(nameless, now=NOW)) == ["reward"]
    assert details_line(_row("x"), now=NOW) is None
    assert details_line(None, now=NOW) is None


def test_the_same_name_for_reward_and_expiry_is_credited_once_and_two_names_in_order():
    same = _row("x", reward="r", reward_from="m", expires_on=date(2026, 10, 14), expires_from="m")
    assert details_line(same, now=NOW).endswith("`via` m")
    two = _row("x", reward="r", reward_from="b", expires_on=date(2026, 10, 14), expires_from="a")
    assert details_line(two, now=NOW).endswith("`via` b and a")


def test_a_permanent_code_never_shows_a_timestamp_even_with_a_stray_date():
    stray = _row(
        "x", reward="r", category="permanent", category_from="m", expires_on=date(2026, 10, 14)
    )
    for relative in (False, True):
        line = details_line(stray, now=NOW, relative=relative)
        assert "`expires` never" in line and "<t:" not in line and "Oct" not in line


# --- 2. the reserve ---

STAR, UNDER = "*" * 100, "_" * 100
EMOJI = "\U0001f600" * 100
BACKSLASHES = "\\" * 100
AT_X = ("@everyone" * 11) + "x"
AT_Y = ("@everyone" * 11) + "y"
LT_X = ("<t:1:R>" * 14) + "xx"
LT_Y = ("<t:1:R>" * 14) + "yy"
PLAIN_A, PLAIN_B = "a" * 100, "b" * 100
LINKY_X = ("https://" * 12) + "xxxx"
LINKY_Y = ("https://" * 12) + "yyyy"
ONE, TWO = "m", "n"

NAME_PAIRS = [
    (STAR, UNDER),
    (STAR, EMOJI),
    (EMOJI, BACKSLASHES),
    (BACKSLASHES, STAR),
    (AT_X, AT_Y),
    (LT_X, LT_Y),
    (PLAIN_A, PLAIN_B),
    (LINKY_X, LINKY_Y),
    (ONE, TWO),
    ("", ""),
]


def _units(name: str) -> int:
    return discord_len(esc(name[:100]))


@pytest.mark.parametrize("pair", NAME_PAIRS, ids=[f"pair{i}" for i in range(len(NAME_PAIRS))])
def test_the_reserve_is_exactly_the_worst_line_plus_one_for_each_name_set(pair):
    a, b = pair
    if a and b:
        assert _units(a) == _units(b), "the pair has to be equal in shown units to be a worst case"
    # The widest date there is: month and day at full width, an 11-digit stamp.
    row = _row(
        "x",
        reward="r" * 300,
        reward_from=a,
        expires_on=date(5000, 12, 31),
        expires_from=b,
    )
    line = details_line(row, now=NOW)
    if not (a and b):
        # No names means no `via` part at all; the reserve is a ceiling, not a promise.
        assert discord_len(line) + 1 <= details_reserve([a, b])
        return
    assert "(<t:95649148800:R>)" in line
    assert discord_len(line) + 1 == details_reserve([a, b])


@pytest.mark.parametrize("month", range(1, 13))
def test_no_month_makes_the_expiry_value_wider_than_the_reserve_counts(month):
    from newsbot.bot.format import _EXPIRY_VALUE_MAX, _live_expiry_value

    for day in (28, 30 if month != 2 else 28):
        row = _row("x", expires_on=date(5000, month, day))
        value = _live_expiry_value(row)
        assert discord_len(value) <= _EXPIRY_VALUE_MAX


@pytest.mark.parametrize(
    "year",
    [1, 1999, 2026, 2100, 2286, 2287, 3000, 5000, 5138, 5139, 9999],
)
def test_no_year_makes_the_expiry_value_wider_than_the_reserve_counts(year):
    from newsbot.bot.format import _EXPIRY_VALUE_MAX, _live_expiry_value

    row = _row("x", expires_on=date(year, 12, 31))
    try:
        value = _live_expiry_value(row)
    except OverflowError, ValueError:
        pytest.fail(f"a stored expiry in {year} blew up the details line")
    assert discord_len(value) <= _EXPIRY_VALUE_MAX


def test_the_reserve_never_counts_a_name_past_100_characters():
    assert details_reserve(["x" * 5000, "y"]) == details_reserve(["x" * 100, "y"])
    assert details_reserve(["*" * 5000]) == 369 + 2 * 200


def test_the_reserve_with_no_names_is_the_bare_369():
    assert details_reserve([]) == 369
    assert details_reserve(iter([])) == 369


def test_a_name_over_100_characters_still_fits_the_reserve_for_its_first_100():
    for name in ("*" * 150, "\U0001f600" * 150, ("<t:1:R>" * 30), "a" * 101, ("a" * 98) + "<:x"):
        row = _row(
            "x",
            reward="r" * 300,
            reward_from=name,
            expires_on=date(5000, 12, 31),
            expires_from="z",
        )
        assert discord_len(details_line(row, now=NOW)) + 1 <= details_reserve([name, "z"])


_FUZZ_TEXT = [
    "",
    "3 Golden Keys",
    "*" * 40,
    "_" * 41,
    "\\" * 50,
    "`" * 30,
    "\U0001f600" * 60,
    "@everyone " * 20,
    "<t:1:R> " * 30,
    "<#1>" * 40,
    "https://x.example/ " * 15,
    "discord.gg/abc " * 20,
    BIDI * 30,
    "x" * 300,
    "x" * 600,
    "\n> q\n" * 40,
    "a‍b" * 100,
]


def test_a_seeded_fuzz_never_beats_the_reserve():
    rng = random.Random(20261022)  # noqa: S311
    for _ in range(600):
        rf, ef, cf = (rng.choice(_FUZZ_TEXT)[:100] for _ in range(3))
        permanent = rng.random() < 0.3
        row = _row(
            "x",
            reward=rng.choice(_FUZZ_TEXT),
            reward_from=rf,
            category="permanent" if permanent else rng.choice([None, "golden", "cosmetic"]),
            category_from=cf,
            expires_on=rng.choice([None, date(2026, 10, 14), date(5000, 12, 31), date(2100, 1, 1)]),
            expires_from=ef,
        )
        names = [rf, ef, cf]
        line = details_line(row, now=NOW)
        if line is not None:
            assert discord_len(line) + 1 <= details_reserve(names), (row, line)


# --- 3. pack, then the worst edit ---


def _worst(code: str, names, reward="*") -> CodeDetailsRow:
    return _row(
        code,
        reward=reward * 150,
        reward_from=names[0],
        expires_on=date(5000, 12, 31),
        expires_from=names[1],
    )


@pytest.mark.parametrize("pair", NAME_PAIRS[:8], ids=[f"pair{i}" for i in range(8)])
@pytest.mark.parametrize(
    ("ping", "mention"),
    [(False, None), (True, None), (True, ROLE_18), (True, ROLE_20)],
    ids=["quiet", "everyone", "role18", "role20"],
)
@pytest.mark.parametrize("test", [False, True], ids=["real", "test"])
def test_pack_with_the_real_reserve_then_the_worst_edit_fits_with_every_link(
    pair, ping, mention, test
):
    names = list(pair)
    reserve = details_reserve(names)
    # Source names in the entries are as wide as the config allows (100 chars, markdown-heavy).
    batch = [
        _cand(
            i,
            golden=i % 3 == 0,
            source_name=(names[i % 2])[:100] or "s",
            item_url="https://example.com/" + "a" * 120,
        )
        for i in range(30)
    ]
    by_code = {c.code: c for c in batch}
    alerts = render_code_alerts(
        batch, ping=ping, ping_mention=mention, test=test, reserve=reserve, nonce_scope="1|2"
    )
    assert [c for a in alerts for c in a.codes] == [c.code for c in batch]
    for a in alerts:
        assert discord_len(a.content) <= 2000
        worst = {code: _worst(code, names) for code in a.codes}
        for header in {a.header, a.header.removeprefix(a.ping_prefix)}:
            edited = render_alert_edit(
                header,
                [by_code[c] for c in a.codes],
                golden_prefix=a.golden_prefix,
                details=worst,
                now=NOW,
            )
            assert discord_len(edited) <= 2000
            for c in a.codes:
                assert f"<{by_code[c].item_url}>" in edited
            assert set(chips(edited)) <= ALLOWED_CHIPS


def test_the_widest_possible_message_header_and_entry_still_take_the_worst_edit():
    # 1 code: the longest header (role ping, [test], a golden code), a 100-char starry source
    # name and a URL that only just fits without making the pack shed its link.
    names = [STAR, UNDER]
    reserve = details_reserve(names)
    c = _cand(0, golden=True, source_name=STAR, item_url="https://example.com/" + "a" * 600)
    [a] = render_code_alerts([c], ping=True, ping_mention=ROLE_20, test=True, reserve=reserve)
    assert f"<{c.item_url}>" in a.content
    edited = render_alert_edit(
        a.header, [c], golden_prefix=False, details={c.code: _worst(c.code, names)}, now=NOW
    )
    assert discord_len(edited) <= 2000
    assert f"<{c.item_url}>" in edited


def test_a_url_too_long_to_pack_sheds_at_pack_time_and_the_edit_keeps_it_shed_or_fits():
    names = [STAR, UNDER]
    reserve = details_reserve(names)
    c = _cand(0, source_name=STAR, item_url="https://example.com/" + "a" * 1500)
    [a] = render_code_alerts([c], ping=True, reserve=reserve)
    assert "<https://" not in a.content
    edited = render_alert_edit(
        a.header, [c], golden_prefix=False, details={c.code: _worst(c.code, names)}, now=NOW
    )
    assert discord_len(edited) <= 2000


def test_an_edit_that_outgrows_its_reserve_sheds_links_or_raises_but_never_overflows():
    # Packed when the only name anywhere was "m" (L=1); the config later gained two 100-char
    # starry names. The reserve is stale on purpose.
    small = details_reserve(["m"])
    batch = [_cand(i, item_url="https://example.com/" + "a" * 60) for i in range(40)]
    alerts = render_code_alerts(batch, ping=True, reserve=small)
    by_code = {c.code: c for c in batch}
    shed = raised = 0
    for a in alerts:
        worst = {code: _worst(code, [STAR, UNDER]) for code in a.codes}
        try:
            edited = render_alert_edit(
                a.header,
                [by_code[c] for c in a.codes],
                golden_prefix=False,
                details=worst,
                now=NOW,
            )
        except ValueError:
            raised += 1
            continue
        assert discord_len(edited) <= 2000
        shed += "<https://" not in edited
    assert shed + raised > 0, "the stale reserve was supposed to be too small for this"


def test_an_edit_with_no_details_at_all_is_the_original_message_byte_for_byte():
    batch = [_cand(i, golden=i % 2 == 0) for i in range(9)]
    by_code = {c.code: c for c in batch}
    for mention in (None, ROLE_20):
        for a in render_code_alerts(
            batch, ping=True, ping_mention=mention, reserve=details_reserve(["m"])
        ):
            same = render_alert_edit(
                a.header,
                [by_code[c] for c in a.codes],
                golden_prefix=a.golden_prefix,
                details={},
                now=NOW,
            )
            assert same == a.content


@pytest.mark.parametrize("text", POISON[:20], ids=[f"p{i}" for i in range(20)])
def test_an_edit_with_the_details_it_was_posted_with_is_the_posted_message(text):
    plain, golden = _cand(0, source_name=text[:100] or "s"), _cand(1, golden=True)
    details = {
        plain.code: _row(
            plain.code, reward=text, reward_from=text[:100], expires_on=date(2026, 10, 14)
        ),
        golden.code: _row(golden.code, reward=text, category="permanent", category_from=text[:100]),
    }
    by_code = {c.code: c for c in (plain, golden)}
    reserve = details_reserve([text[:100]])
    for ping, test in ((True, False), (False, True)):
        for a in render_code_alerts(
            [plain, golden], ping=ping, test=test, details=details, now=NOW, reserve=reserve
        ):
            again = render_alert_edit(
                a.header,
                [by_code[c] for c in a.codes],
                golden_prefix=a.golden_prefix,
                details=details,
                now=NOW,
            )
            assert again == a.content


def test_a_ping_stripped_terminal_header_edits_in_the_terminal_layout():
    c = _cand(0)
    row = _row(c.code, reward="3 Golden Keys", reward_from="mentalmars")
    [a] = render_code_alerts([c], ping=True, ping_mention=ROLE_20)
    stripped = a.header.removeprefix(a.ping_prefix)
    assert terminal.is_terminal_header(stripped) and stripped != a.header
    edited = render_alert_edit(stripped, [c], golden_prefix=False, details={c.code: row}, now=NOW)
    assert edited.startswith("`> new SHiFT code ▌`\n\n")
    assert "`reward` 3 Golden Keys · `via` mentalmars" in edited
    assert "`src` Gearbox Blog" in edited


@pytest.mark.parametrize(
    "header",
    [
        "`> hand made ▌`",
        "text `▌`",
        "▌`",
        "@everyone ▌`",
        f"`> a ▌` {ROLE_18}",
        f"{ROLE_18} @everyone `> a ▌`",
    ],
)
def test_a_hand_made_terminal_looking_header_gets_the_terminal_layout_and_fits(header):
    c = _cand(0)
    row = _row(c.code, reward="r", reward_from="m", expires_on=date(2026, 10, 14), expires_from="m")
    edited = render_alert_edit(header, [c], golden_prefix=False, details={c.code: row}, now=NOW)
    assert edited.startswith(header + "\n\n")
    assert "`src` Gearbox Blog" in edited and "`expires`" in edited
    assert "details via" not in edited


def test_a_terminal_header_too_big_for_any_entry_raises_value_error_not_overflow():
    huge = "`> " + "x" * 1990 + " ▌`"
    with pytest.raises(ValueError, match="exceeds"):
        render_alert_edit(huge, [_cand(0)], golden_prefix=False, details={}, now=NOW)


# --- 4. batches ---

SIZES = [1, 2, 5, 6, 50]
MIXES = ["plain", "mixed", "golden"]
SCOPES = ["", "42|7"]


def _batch(n: int, mix: str) -> list[CodeCandidate]:
    golden = {"plain": lambda i: False, "mixed": lambda i: i % 2 == 0, "golden": lambda i: True}
    if mix == "mixed" and n == 1:
        return [_cand(0, golden=True)]  # a batch of one can't be mixed
    return [_cand(i, golden=golden[mix](i)) for i in range(n)]


def _expect_kind(batch):
    return "Golden Key" if all(c.golden for c in batch) else "SHiFT"


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("mix", MIXES)
@pytest.mark.parametrize(("ping", "mention"), [(False, None), (True, None), (True, ROLE_20)])
@pytest.mark.parametrize("test", [False, True], ids=["real", "test"])
@pytest.mark.parametrize("with_reserve", [False, True], ids=["noreserve", "reserve"])
def test_a_batch_has_one_first_header_continuations_after_it_and_a_ping_only_up_front(
    n, mix, ping, mention, test, with_reserve
):
    batch = _batch(n, mix)
    reserve = details_reserve(["Gearbox Blog", "mentalmars"]) if with_reserve else 0
    alerts = render_code_alerts(
        batch, ping=ping, ping_mention=mention, test=test, reserve=reserve, nonce_scope="1|2"
    )

    assert [c for a in alerts for c in a.codes] == [c.code for c in batch]
    tag = "[test] " if test else ""
    prefix = (f"{mention or '@everyone'} ") if ping else ""
    plural = "s" if n > 1 else ""
    first = f"{prefix}`> {tag}new {_expect_kind(batch)} code{plural} ▌`"
    cont = f"`> {tag}new SHiFT codes (continued) ▌`"
    assert alerts[0].header == first
    assert alerts[0].ping is ping and alerts[0].ping_prefix == prefix
    for a in alerts[1:]:
        assert a.header == cont and a.ping is False and a.ping_prefix == ""
    for a in alerts:
        assert a.content.startswith(a.header + "\n\n```\n")
        assert discord_len(a.content) <= 2000
        assert fence_bodies(a.content) == a.codes
        assert a.content.count("@everyone") == (1 if ping and a is alerts[0] and not mention else 0)
        assert a.golden_prefix is (mix == "mixed" and n > 1)
        assert terminal.is_terminal_header(a.header)
    expect_messages = {
        (1, False): 1,
        (2, False): 1,
        (5, False): 1,
        (6, False): 1,
    }.get((n, with_reserve))
    if expect_messages:
        assert len(alerts) == expect_messages
    if n == 50:
        assert len(alerts) > 1


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("mix", MIXES)
@pytest.mark.parametrize("scope", SCOPES)
def test_nonces_follow_the_v314_formula_and_never_depend_on_look_ping_or_test(n, mix, scope):
    batch = _batch(n, mix)
    reserve = details_reserve(["Gearbox Blog", "mentalmars"])
    seen = None
    for ping, mention, test in ((False, None, False), (True, None, False), (True, ROLE_20, True)):
        alerts = render_code_alerts(
            batch, ping=ping, ping_mention=mention, test=test, nonce_scope=scope, reserve=reserve
        )
        salt = f"{scope}|" if scope else ""
        for i, a in enumerate(alerts):
            want = hashlib.sha256(f"{salt}{'|'.join(a.codes)}|{i}".encode()).hexdigest()[:25]
            assert a.nonce == want
        nonces = [a.nonce for a in alerts]
        assert len(set(nonces)) == len(nonces)
        seen = seen or nonces
        assert nonces == seen, "the nonce moved with the ping or the test flag"


@pytest.mark.parametrize("n", [1, 2, 5])
@pytest.mark.parametrize("mix", MIXES)
@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("ping", [False, True])
@pytest.mark.parametrize("test", [False, True], ids=["real", "test"])
def test_nonces_and_codes_match_what_v314_made_for_the_same_input(
    old_format, n, mix, scope, ping, test
):
    batch = _batch(n, mix)
    old_batch = [
        old_format.CodeCandidate(**c.__dict__) if hasattr(old_format, "CodeCandidate") else c
        for c in batch
    ]
    want = old_format.render_code_alerts(old_batch, ping=ping, test=test, nonce_scope=scope)
    got = render_code_alerts(batch, ping=ping, test=test, nonce_scope=scope)
    assert [(a.codes, a.nonce, a.ping, a.ping_prefix) for a in got] == [
        (a.codes, a.nonce, a.ping, a.ping_prefix) for a in want
    ]


@pytest.mark.parametrize("n", [6, 12, 50])
@pytest.mark.parametrize("scope", SCOPES)
def test_when_v314_and_today_split_a_batch_the_same_way_their_nonces_agree(old_format, n, scope):
    batch = _batch(n, "plain")
    for reserve_old, reserve_new in ((0, 0), (402, 407)):
        want = old_format.render_code_alerts(
            batch, ping=True, nonce_scope=scope, reserve=reserve_old
        )
        got = render_code_alerts(batch, ping=True, nonce_scope=scope, reserve=reserve_new)
        if [a.codes for a in want] == [a.codes for a in got]:
            assert [a.nonce for a in want] == [a.nonce for a in got]


def test_a_batch_of_fifty_with_the_prod_reserve_fills_every_message_and_loses_no_code():
    batch = _batch(50, "mixed")
    reserve = details_reserve(["mentalmars", "Gearbox Blog"])
    alerts = render_code_alerts(batch, ping=True, test=True, reserve=reserve)
    flat = [c for a in alerts for c in a.codes]
    assert flat == [c.code for c in batch] and len(set(flat)) == 50
    assert all(discord_len(a.content) <= 2000 for a in alerts)
    # Each message but the last is full enough that the next entry's slot wouldn't have fit.
    slot = max(discord_len(a.content) for a in alerts) // 4  # a loose floor, only to catch waste
    assert all(discord_len(a.content) > slot for a in alerts[:-1])


def test_an_all_golden_batch_says_golden_key_up_front_and_shift_codes_in_the_continuations():
    batch = _batch(50, "golden")
    alerts = render_code_alerts(
        batch, ping=False, reserve=details_reserve(["Gearbox Blog", "mentalmars"])
    )
    assert alerts[0].header == "`> new Golden Key codes ▌`"
    assert {a.header for a in alerts[1:]} == {"`> new SHiFT codes (continued) ▌`"}
    assert all("Golden Key:" not in a.content for a in alerts)


def test_a_mixed_batch_prefixes_only_the_golden_entries_in_every_message():
    batch = _batch(50, "mixed")
    alerts = render_code_alerts(
        batch, ping=False, reserve=details_reserve(["Gearbox Blog", "mentalmars"])
    )
    golden = {c.code for c in batch if c.golden}
    for a in alerts:
        for code in a.codes:
            line = re.search(rf"{code}\n```\n(?:[^\n]*\n)?(`src`[^\n]*)", a.content).group(1)
            assert line.startswith("`src` Golden Key: ") is (code in golden)


def test_no_candidates_means_no_messages():
    assert render_code_alerts([], ping=True, test=True) == []


def test_an_entry_that_cannot_fit_alone_raises_with_the_terminal_budget():
    # A 2,000-unit source name can't happen (the config caps it); this is the loud failure.
    c = _cand(0, source_name="x" * 2000)
    with pytest.raises(ValueError, match="can't fit in one message"):
        render_code_alerts([c], ping=True)


def test_a_lone_entry_is_budgeted_against_the_longer_continuation_header():
    # The plan's ruling: 2000 minus the longer of the two headers minus 2. With no ping, the
    # continuation header (33 units) is the longer, so a lone entry tops out at 1,965 + header.
    pad = "x" * 100
    base = _cand(0, source_name=pad, item_url="https://e.com/a0")
    block_units = discord_len(f"```\n{base.code}\n```\n`src` {pad}")
    cont = discord_len("`> new SHiFT codes (continued) ▌`")
    room = 2000 - cont - 2
    just = replace(base, item_url="https://e.com/" + "u" * (room - block_units - len(" · <>") - 14))
    [a] = render_code_alerts([just], ping=False)
    assert discord_len(a.content) <= 2000
    assert f"<{just.item_url}>" in a.content
    over = replace(just, item_url=just.item_url + "uu")
    [b] = render_code_alerts([over], ping=False)
    assert "<https://" not in b.content, "one entry over the budget sheds its link"


# --- 5. what must not move ---


def _displayed_grid():
    rewards = ["", "  ", "3 Golden Keys", " padded ", BIDI]
    froms = ["", "mentalmars"]
    cats = [None, "golden", "cosmetic", "permanent", "other"]
    dates = [None, date(2026, 10, 14)]
    for reward, rfrom, cat, cfrom, exp, efrom in product(rewards, froms, cats, froms, dates, froms):
        yield _row(
            "x",
            reward=reward,
            reward_from=rfrom,
            category=cat,
            category_from=cfrom,
            expires_on=exp,
            expires_from=efrom,
        )


def test_displayed_is_the_same_function_of_a_row_as_v314s(old_details):
    checked = 0
    for row in _displayed_grid():
        assert displayed(row) == old_details.displayed(row), row
        checked += 1
    assert displayed(None) == old_details.displayed(None)
    assert checked >= 400


def test_displayed_still_holds_the_values_and_not_the_wording():
    permanent = _row("x", reward="r", reward_from="m", category="permanent", category_from="c")
    assert displayed(permanent) == ("r", "m", True, "c", None, "")
    dated = _row("x", reward="r", reward_from="m", expires_on=date(2026, 10, 14), expires_from="e")
    assert displayed(dated) == ("r", "m", False, "", date(2026, 10, 14), "e")
    assert displayed(None) == ("", "", False, "", None, "")
    assert displayed(_row("x")) == ("", "", False, "", None, "")


@pytest.mark.parametrize("kind", ["permanent", "dated", "reward"])
async def test_v31_era_records_stay_not_due_for_each_kind_of_details(tmp_path, kind):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    harness = EditHarness(path, load_config(V3), FANOUT_NOW)
    seed(path)
    add_guild(path, 1, ping="everyone")
    add_guild(path, 2, ping=ROLE_18.strip("<@&>"))
    details = {
        "permanent": dict(reward="Skin", category="permanent"),
        "dated": dict(reward="3 Golden Keys", expires_on=date(2026, 10, 14)),
        "reward": dict(reward="3 Golden Keys"),
    }[kind]
    posted = item(CODE_A, CODE_B)
    posted = replace(posted, details=ItemDetails(code=CODE_A, rank="official", **details))
    await harness.run([posted])
    # What v3.1 would have stored: its own headers, same details stamps.
    with closing(connect(path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET header = '**New SHiFT codes**'")
    stamps = message_state(path)
    assert stamps, "the first pass should have posted something"

    for hours in (1, 3, 30, 24 * 9, 24 * 13):
        harness.clock = FANOUT_NOW + timedelta(hours=hours)
        await harness.run([])
    assert harness.editors.calls == []
    assert message_state(path) == stamps


async def test_v31_records_that_do_change_are_edited_in_the_classic_layout_with_its_wording(
    tmp_path,
):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    harness = EditHarness(path, load_config(V3), FANOUT_NOW)
    seed(path)
    add_guild(path, 1, ping="everyone")
    posted = replace(
        item(CODE_A),
        details=ItemDetails(code=CODE_A, rank="official", category="permanent"),
    )
    await harness.run([posted])
    with closing(connect(path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET header = '@everyone **New SHiFT code**'")
    from test_shift_edits import learn

    learn(harness, CODE_A, "A better skin", at=FANOUT_NOW + timedelta(hours=1))
    harness.clock = FANOUT_NOW + timedelta(hours=2)
    await harness.run([])
    [call] = harness.editors.calls
    assert call.content.startswith("@everyone **New SHiFT code**\n\n")
    assert "`" not in call.content.replace("```", ""), "no chip leaks into a v3.1 message"
    assert "details via" in call.content


def _current(code="A" * 29, **kwargs) -> CurrentCode:
    return CurrentCode(code=code, first_seen_at=NOW, details=_row(code, **kwargs))


def _entries_grid():
    rewards = ["", "3 Golden Keys", "*" * 50, "`x`", "a\n> b", BIDI + "z"]
    origins = ["", "Shift Code Hunters", "`origin`", "@everyone", "<t:1:R>"]
    cats = [None, "golden", "permanent"]
    dates = [
        None,
        date(2026, 10, 1),  # past
        date(2026, 10, 9),  # today (noon PDT)
        date(2026, 10, 10),  # tomorrow
        date(2026, 10, 14),  # this year
        date(2027, 3, 3),  # another year
        date(5000, 1, 1),
    ]
    for reward, origin, cat, exp in product(rewards, origins, cats, dates):
        yield dict(reward=reward, origin=origin, category=cat, expires_on=exp)


def _v314_entry_in_the_new_layout(old: str, fields: dict) -> str:
    """What v3.1.4 wrote for a `/shift codes` entry, re-laid-out the way Task 4 draws it.

    Task 4 changed the layout and none of the facts: the fence replaces the inline code, the
    two labels become chips, "unknown" loses its word, and a permanent code reads `expires`
    never with its origin after it. The reward text, the expiry phrase and the origin are
    taken from the old entry as they were.
    """
    code_line, rest = old.split("\n", 1)
    reward, tail = rest.rsplit(" \u00b7 ", 1)
    reward = "unknown" if reward == "reward unknown" else reward
    # Fix batch 1: detail text collapses whitespace, newlines included, to single spaces, so a
    # reward of "a\n> b" is one line now ("a > b": the ">" is no longer at a line start, so
    # it isn't escaped either). Everything else about the reward is what v3.1.4 said.
    reward = " ".join(reward.replace("\n\\>", " >").split())
    if tail == "permanent":
        expires = "`expires` never"
    elif fields.get("category") == "permanent":
        expires = f"`expires` never \u00b7 {tail}"
    elif tail == "expiry unknown":
        expires = "`expires` unknown"
    elif tail == "expired":
        d = fields["expires_on"]
        expires = f"`expired` {d.strftime('%b')} {d.day}, {d.year}"
    else:
        expires = "`expires` " + tail.removeprefix("expires ")
    return f"```\n{code_line.strip('`')}\n```\n`reward` {reward} \u00b7 {expires}"


def test_a_reward_with_a_newline_and_a_quote_marker_is_one_plain_line_now():
    cur = _current(reward="a\n> b", origin="Shift Code Hunters")
    entry = _current_entry(cur, NOW)
    assert "`reward` a > b \u00b7 " in entry
    assert "\n> " not in entry
    assert "\\>" not in entry


def test_shift_codes_entries_say_what_v314_said_in_the_new_layout(old_format):
    checked = 0
    for fields in _entries_grid():
        row = _row("A" * 29, **fields)
        cur = CurrentCode(code="A" * 29, first_seen_at=NOW, details=row)
        old_cur = (
            old_format.CurrentCode(**cur.__dict__) if hasattr(old_format, "CurrentCode") else cur
        )
        want = _v314_entry_in_the_new_layout(old_format._current_entry(old_cur, NOW), fields)
        assert _current_entry(cur, NOW) == want, fields
        checked += 1
    assert checked > 500
    bare = CurrentCode(code="A" * 29, first_seen_at=NOW, details=None)
    assert _current_entry(bare, NOW) == _v314_entry_in_the_new_layout(
        old_format._current_entry(bare, NOW), {}
    )


def test_shift_codes_pages_list_v314s_codes_in_v314s_order_under_the_new_look(old_format):
    entries = [
        _current(code=f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW", **f)
        for i, f in enumerate(list(_entries_grid())[::7])
    ]

    def codes(pages):
        text = "\n".join(e.description for e in pages)
        return re.findall(r"\d{4}A-BCDEF-GHJKL-MNPQR-STUVW", text)

    for kind in ("all", "golden", "permanent", "cosmetic"):
        kw = dict(kind=kind, now=NOW, footer="sources", empty="nothing")
        got = render_current_pages(entries, **kw)
        want = old_format.render_current_pages(entries, **kw)
        assert codes(got) == codes(want)
        assert all(e.footer.text.startswith("sources \u00b7 Page ") for e in got)
        assert {e.color.value for e in got} == {terminal.SHIFT_GREEN}


def test_the_only_alert_text_that_changed_is_what_the_plan_names(old_format):
    # A guard against drive-by edits: strip the new look from today's message and it is v3.1.4's,
    # modulo the header, the label chips, "details via" and the permanent wording.
    batch = [_cand(i, golden=i == 0) for i in range(3)]
    row = _row(
        batch[1].code,
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    details = {batch[1].code: row}
    got = render_code_alerts(batch, ping=True, details=details, now=NOW, reserve=407)[0].content
    want = old_format.render_code_alerts(batch, ping=True, details=details, now=NOW, reserve=402)[
        0
    ].content
    squashed = (
        got.replace("`reward` ", "")
        .replace("`expires` ", "expires ")
        .replace(" · `via` ", " · details via ")
        .replace("`src` ", "")
        .replace("`> new SHiFT codes ▌`", "**New SHiFT codes**")
    )
    assert squashed == want


def test_the_helper_agrees_the_header_chip_is_the_only_chip_in_a_bare_alert():
    [a] = render_code_alerts([_cand(0)], ping=True, ping_mention=ROLE_20, test=True)
    assert chips(a.content) == ["> [test] new SHiFT code ▌", "src"]
