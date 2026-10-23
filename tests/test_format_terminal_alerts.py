"""New SHiFT alerts and their edits in the terminal look (design.md §17, plan task 2).

What this pins: every header shape (new, golden, test, plural, continuation), the entry
layout (fence, details line, `src` line), a permanent code reading `expires` never, the
reserve formula and its tightness, and the one rule the whole look hangs on: chips hold
only the fixed words, never anything a stranger typed. That last one is checked with
`tests/chip_check.py`, a parser of its own, against rewards and names made of every
character that could close a chip or open a fence.

The last test is the deploy story: a database full of messages that v3.1 posted, a bot
that's now v3.2.0, and an edit pass that must find nothing newly due (and that edits a
v3.1 message in the layout it was packed for when something real does change).
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies
from test_shift_edits import EditHarness, learn, message_state
from test_shift_fanout import CODE_A, V3, add_guild, item, seed
from test_shift_fanout import NOW as FANOUT_NOW

from newsbot.bot import terminal
from newsbot.bot.format import (
    _EXPIRY_VALUE_MAX,
    details_line,
    details_reserve,
    discord_len,
    esc,
    render_alert_edit,
    render_code_alerts,
)
from newsbot.config import load_config
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.match import is_code
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # noon PDT, Oct 9
ROLE = "<@&123456789012345678>"


def _code(i: int = 0) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _candidate(i: int = 0, **kwargs) -> CodeCandidate:
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


# --- the chip checker, checked ---


def test_chips_reads_inline_code_and_skips_fences():
    assert chips("`a` b `c d`") == ["a", "c d"]
    assert chips("```\nCODE\n```\n`src` x") == ["src"]
    assert chips("```\n`not a chip`\n```") == []


def test_chips_treats_an_escaped_backtick_as_literal_and_a_doubled_backslash_as_not():
    assert chips("\\`not a chip\\`") == []
    assert chips("`ok` \\`no\\` `fine`") == ["ok", "fine"]
    assert chips("\\\\`this opens`") == ["this opens"]


def test_chips_reports_a_chip_that_never_closes():
    assert chips("`reward` then `oops") == ["reward", "oops"]


def test_fence_bodies_return_what_is_inside_each_fence():
    assert fence_bodies(f"head\n\n```\n{_code(1)}\n```\nx\n\n```\n{_code(2)}\n```") == [
        _code(1),
        _code(2),
    ]


# --- headers ---


@pytest.mark.parametrize(
    ("golden", "test", "expected"),
    [
        ([False], False, "`> new SHiFT code ▌`"),
        ([False, False], False, "`> new SHiFT codes ▌`"),
        ([True], False, "`> new Golden Key code ▌`"),
        ([True, True], False, "`> new Golden Key codes ▌`"),
        ([True, False], False, "`> new SHiFT codes ▌`"),
        ([False], True, "`> [test] new SHiFT code ▌`"),
        ([True], True, "`> [test] new Golden Key code ▌`"),
        ([True, True, False], True, "`> [test] new SHiFT codes ▌`"),
    ],
)
@pytest.mark.parametrize(
    ("ping", "mention", "prefix"),
    [(False, None, ""), (True, None, "@everyone "), (True, ROLE, f"{ROLE} ")],
)
def test_the_first_header_follows_the_batch(golden, test, expected, ping, mention, prefix):
    batch = [_candidate(i, golden=g) for i, g in enumerate(golden)]
    [alert] = render_code_alerts(batch, ping=ping, ping_mention=mention, test=test)
    assert alert.header == prefix + expected
    assert alert.ping_prefix == prefix
    assert alert.content.startswith(prefix + expected + "\n\n```\n")


@pytest.mark.parametrize("test", [False, True])
@pytest.mark.parametrize("all_golden", [False, True])
def test_a_continuation_says_codes_continued_and_never_pings(test, all_golden):
    batch = [_candidate(i, golden=all_golden or i % 2 == 0) for i in range(12)]
    alerts = render_code_alerts(
        batch, ping=True, test=test, reserve=details_reserve(["Gearbox Blog", "mentalmars"])
    )
    assert len(alerts) > 1
    cont = f"`> {'[test] ' if test else ''}new SHiFT codes (continued) ▌`"
    assert all(a.header == cont for a in alerts[1:])
    assert all(a.ping_prefix == "" and not a.ping for a in alerts[1:])
    assert alerts[0].ping and alerts[0].header.startswith("@everyone ")
    assert all(a.content.startswith(cont + "\n\n") for a in alerts[1:])


def test_the_header_is_one_chip_with_the_cursor_inside():
    [alert] = render_code_alerts([_candidate()], ping=True)
    header_chips = chips(alert.header)
    assert header_chips == [f"> new SHiFT code {terminal.CURSOR}"]
    assert terminal.is_terminal_header(alert.header)


# --- the entry ---


def test_an_entry_is_the_fence_the_details_line_and_the_src_line():
    c = _candidate()
    row = _row(
        c.code,
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    [alert] = render_code_alerts([c], ping=False, details={c.code: row}, now=NOW)
    assert alert.content == (
        "`> new SHiFT code ▌`\n\n"
        f"```\n{c.code}\n```\n"
        "`reward` 3 Golden Keys · `expires` Oct 14, 2026 (<t:1792047600:R>) · `via` mentalmars\n"
        "`src` Gearbox Blog · <https://e.com/a0>"
    )


def test_a_code_with_no_details_is_the_fence_and_the_src_line():
    [alert] = render_code_alerts([_candidate()], ping=False)
    assert alert.content == (
        f"`> new SHiFT code ▌`\n\n```\n{_code()}\n```\n`src` Gearbox Blog · <https://e.com/a0>"
    )


def test_the_golden_key_prefix_comes_after_the_src_label_and_only_in_a_mixed_batch():
    batch = [_candidate(0, golden=True), _candidate(1)]
    [alert] = render_code_alerts(batch, ping=False)
    assert "\n`src` Golden Key: Gearbox Blog · <https://e.com/a0>" in alert.content
    assert "\n`src` Gearbox Blog · <https://e.com/a1>" in alert.content
    [plain] = render_code_alerts([_candidate(0, golden=True)], ping=False)
    assert "Golden Key:" not in plain.content


def test_an_oversize_entry_sheds_its_link_and_keeps_its_src_line():
    c = _candidate(item_url="https://example.com/" + "a" * 1900)
    [alert] = render_code_alerts([c], ping=False)
    assert "<https://" not in alert.content
    assert alert.content.endswith("\n`src` Gearbox Blog")


def test_the_src_line_escapes_the_source_name():
    c = _candidate(source_name="*bold* `tick` @everyone")
    [alert] = render_code_alerts([c], ping=False)
    assert f"\n`src` {esc(c.source_name)} · <" in alert.content
    assert set(chips(alert.content)) <= ALLOWED_CHIPS


def test_a_permanent_code_reads_expires_never():
    row = _row("x", reward="Cosmetic head", category="permanent", category_from="mentalmars")
    line = details_line(row, now=NOW)
    assert line == "`reward` Cosmetic head · `expires` never · `via` mentalmars"
    assert "permanent" not in line
    stray = _row("x", category="permanent", category_from="m", expires_on=date(2026, 10, 14))
    assert details_line(stray, now=NOW) == "`expires` never · `via` m"
    assert "<t:" not in details_line(stray, now=NOW)


def test_the_relative_line_swaps_the_label_once_the_date_has_passed():
    row = _row("x", reward="r", expires_on=date(2026, 10, 1))
    assert details_line(row, now=NOW, relative=True) == "`reward` r · `expired` Oct 1, 2026"
    assert details_line(row, now=NOW, relative=True, show_expiry=False) == "`reward` r"


# --- the reserve ---

NAME_A, NAME_B = "A" * 100, "B" * 100


def test_the_reserve_formula_is_369_plus_two_names():
    assert details_reserve([]) == 369
    assert details_reserve(["mentalmars", "Gearbox"]) == 369 + 2 * 10
    assert details_reserve([NAME_A, "m"]) == 369 + 2 * 100


def test_the_reserve_is_built_from_the_labels_not_typed_in():
    assert _EXPIRY_VALUE_MAX == len("Sep 30, 2026 (<t:") + 11 + len(":R>)")
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


def test_the_reserve_equals_the_real_worst_case_line_plus_one():
    # A 300-unit reward, the widest date (an 11-digit stamp), and two 100-character names.
    row = _row(
        "x",
        reward="x" * 300,
        reward_from=NAME_A,
        expires_on=date(5000, 12, 31),
        expires_from=NAME_B,
    )
    line = details_line(row, now=NOW)
    assert "(<t:95649148800:R>)" in line
    assert discord_len(line) + 1 == details_reserve([NAME_A, NAME_B])


def test_the_reserve_still_holds_when_the_names_are_all_markdown():
    names = ["*" * 100, "_" * 100]
    row = _row(
        "x",
        reward="*" * 150,
        reward_from=names[0],
        expires_on=date(5000, 12, 31),
        expires_from=names[1],
    )
    assert discord_len(details_line(row, now=NOW)) + 1 <= details_reserve(names)


# --- the chips rule, against hostile text ---

BIDI = "‮⁦⁩"
HOSTILE = [
    "`",
    "``",
    "```",
    "``` @everyone ```",
    "`reward`",
    "`> new SHiFT code ▌`",
    "▌",
    "> quote at the start",
    "\n> quote after a newline",
    "\\",
    "\\`",
    "\\\\`",
    "@everyone",
    "@here",
    "<t:1:R>",
    "<t:1792047600:R> `expires`",
    "<@&1>",
    f"{BIDI}`{BIDI}",
    "\U0001f600`\U0001f600",
    "x" * 280 + "`" * 30,
    "[x](http://evil.example) `src` https://evil.example",
]


def _surfaces(text: str) -> list[tuple[str, str]]:
    """Every rendered alert and edit with `text` standing in for each scraped field."""
    c = _candidate(0, source_name=text[:100] or "s")
    golden = _candidate(1, golden=True, source_name=text[:100] or "s")
    details = {
        c.code: _row(
            c.code,
            reward=text,
            reward_from=text,
            expires_on=date(2026, 10, 14),
            expires_from=text,
        ),
        golden.code: _row(
            golden.code,
            reward=text,
            reward_from=text,
            category="permanent",
            category_from=text,
        ),
    }
    reserve = details_reserve([text[:100], "mentalmars"])
    out: list[tuple[str, str]] = []
    for ping, test in ((False, False), (True, False), (False, True)):
        first = render_code_alerts(
            [c, golden], ping=ping, test=test, details=details, now=NOW, reserve=reserve
        )
        bare = render_code_alerts([c, golden], ping=ping, test=test, reserve=reserve)
        for n, alert in enumerate(first):
            out.append((f"alert {n}", alert.content))
        for n, alert in enumerate(bare):
            by_code = {x.code: x for x in (c, golden)}
            edited = render_alert_edit(
                alert.header,
                [by_code[code] for code in alert.codes],
                golden_prefix=alert.golden_prefix,
                details=details,
                now=NOW,
            )
            out.append((f"edit {n}", edited))
    relative = details_line(details[c.code], now=NOW, relative=True)
    out.append(("relative line", relative or ""))
    return out


@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_chips_stay_inside_the_fixed_words_on_every_alert_and_edit(text):
    for where, content in _surfaces(text):
        got = set(chips(content))
        assert got <= ALLOWED_CHIPS, (where, got - ALLOWED_CHIPS, content)


@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_every_fence_holds_only_a_code_and_no_chip_holds_a_timestamp(text):
    for where, content in _surfaces(text):
        for body in fence_bodies(content):
            assert is_code(body), (where, body)
        assert all("<t:" not in chip for chip in chips(content)), where


def test_the_bots_own_timestamp_is_outside_every_chip_and_scraped_ones_are_defused():
    for where, content in _surfaces("<t:1:R> and <t:1792047600:R>"):
        if where == "relative line":
            continue
        # The only live token is the bot's own (Oct 14); the scraped ones lost their "<".
        assert set(re.findall(r"<t:(\d+)(?::R>)?", content)) <= {"1792047600"}, where


# --- packing and the worst edit ---


@pytest.mark.parametrize(("ping", "mention"), [(False, None), (True, "@everyone"), (True, ROLE)])
@pytest.mark.parametrize("test", [False, True])
def test_a_message_packed_with_the_reserve_survives_the_worst_edit_with_its_links(
    ping, mention, test
):
    star = "*" * 100  # 200 units once escaped
    names = [star, "_" * 100]
    reserve = details_reserve(names)
    batch = [
        _candidate(
            i,
            golden=i % 3 == 0,
            source_name=star if i % 2 else "Gearbox Blog",
            item_url="https://example.com/" + "a" * 80,
        )
        for i in range(40)
    ]
    by_code = {c.code: c for c in batch}
    alerts = render_code_alerts(batch, ping=ping, ping_mention=mention, test=test, reserve=reserve)
    assert [code for a in alerts for code in a.codes] == [c.code for c in batch]
    for alert in alerts:
        assert discord_len(alert.content) <= 2000
        worst = {
            code: _row(
                code,
                reward="*" * 150,
                reward_from=names[0],
                expires_on=date(5000, 12, 31),
                expires_from=names[1],
            )
            for code in alert.codes
        }
        edited = render_alert_edit(
            alert.header,
            [by_code[code] for code in alert.codes],
            golden_prefix=alert.golden_prefix,
            details=worst,
            now=NOW,
        )
        assert discord_len(edited) <= 2000
        for code in alert.codes:
            assert f"<{by_code[code].item_url}>" in edited, "the worst case still had room"
        assert set(chips(edited)) <= ALLOWED_CHIPS


def test_a_one_code_message_with_a_300_unit_reward_and_a_2000_unit_url_still_fits():
    c = _candidate(item_url="https://example.com/" + "a" * 1960)
    reserve = details_reserve(["mentalmars", "Gearbox Blog"])
    [alert] = render_code_alerts([c], ping=True, reserve=reserve)
    row = _row(
        c.code,
        reward="x" * 300,
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="Gearbox Blog",
    )
    edited = render_alert_edit(
        alert.header, [c], golden_prefix=False, details={c.code: row}, now=NOW
    )
    assert discord_len(edited) <= 2000
    assert alert.content.count("<https://") == edited.count("<https://")


# --- the deploy: v3.1 records, v3.2.0 bot ---


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def cfg():
    return load_config(V3)


@pytest.fixture
def h(db_path, cfg):
    harness = EditHarness(db_path, cfg, FANOUT_NOW)
    seed(db_path)
    return harness


V31_HEADER = "@everyone **New SHiFT code**"


async def test_v31_records_are_not_newly_due_and_are_edited_in_their_own_layout(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=FANOUT_NOW + timedelta(hours=1))
    h.clock = FANOUT_NOW + timedelta(hours=2)
    await h.run([])
    assert len(h.editors.calls) == 1  # the message is now at the latest version of its details
    # Make the record what v3.1 would have left behind: same stamp, the old header.
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET header = ?", (V31_HEADER,))
    stamps = message_state(h.db_path)

    # Deploying v3.2.0 changed no stored details, so nothing is due, pass after pass.
    for hours in (3, 30, 24 * 9):
        h.clock = FANOUT_NOW + timedelta(hours=hours)
        await h.run([])
    assert len(h.editors.calls) == 1
    assert message_state(h.db_path) == stamps

    # A real change later is edited in the layout the message was packed for.
    learn(h, CODE_A, "5 Golden Keys", at=h.clock + timedelta(hours=1))
    h.clock += timedelta(hours=2)
    await h.run([])
    assert len(h.editors.calls) == 2
    content = h.editors.calls[-1].content
    assert content.startswith("@everyone **New SHiFT code**\n\n")
    assert "5 Golden Keys · details via" in content
    assert "`src`" not in content and "`reward`" not in content
