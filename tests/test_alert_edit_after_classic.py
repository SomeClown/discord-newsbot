"""Pins what `render_alert_edit` does with any stored header, now that the v3.1 layout is gone.

The v3.1 layout was removed in v3.5.0. No pre-3.2.0 alert can still be edited, but if a
v3.1-style header ever turned up in a stored record anyway, it gets edited in today's layout
like any other: the header goes back out untouched and everything after it is what a terminal
header would have produced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from chip_check import fence_bodies

from newsbot.bot.format import details_reserve, discord_len, render_alert_edit, render_code_alerts
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeDetailsRow

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
URL = "https://example.com/post"
NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
TERMINAL_HEADER = "`> new SHiFT code ▌`"


def _cands():
    return [
        CodeCandidate(code=c, golden=g, source_name="r/Borderlands4", item_url=URL, fresh=True)
        for c, g in ((CODE, True), (CODE_B, False))
    ]


def _edit(header, details):
    return render_alert_edit(header, _cands(), golden_prefix=True, details=details, now=NOW)


@pytest.mark.parametrize("header", ["**New SHiFT codes**", "@everyone **New SHiFT code**"])
@pytest.mark.parametrize("with_details", [False, True])
def test_a_v3_1_header_is_edited_in_the_current_layout(header, with_details):
    details = (
        {CODE: CodeDetailsRow(code=CODE, reward="3 Golden Keys", clock_start=NOW, updated_at=NOW)}
        if with_details
        else {}
    )
    old_head, _, old_rest = _edit(header, details).partition("\n\n")
    new_head, _, new_rest = _edit(TERMINAL_HEADER, details).partition("\n\n")
    assert old_head == header
    assert new_head == TERMINAL_HEADER
    assert old_rest == new_rest
    assert "details via" not in old_rest


# --- the adversarial round: any header, same body ---


OLD_PING = "<@&18446744073709551615> "
HEADERS = [
    TERMINAL_HEADER,
    "@everyone " + TERMINAL_HEADER,
    OLD_PING + "`> [test] new Golden Key codes (continued) ▌`",
    "**New SHiFT code**",
    "@everyone **New SHiFT codes**",
    OLD_PING + "**[TEST] New Golden Key codes**",
    "**(continued)**",
    "**SHiFT codes from a roundup** · r/Borderlands4 · <https://example.com/x>",
    "",
    " ",
    "@everyone",
    "@everyone ",
    "```",
    "```\nAAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n```",
    "`",
    "``",
    "a ` b ``` c",
    "`> x ▌",
    "▌`",
    "x",
    "garbage with a\nnewline",
    "two\n\nparagraphs",
    "‮ bidi",
    "<t:1:R> https://evil.example [a](https://evil.example)",
]


def _body(text, header):
    assert text.startswith(header + "\n\n"), (header, text[:80])
    return text[len(header) + 2 :]


def _details():
    return {
        CODE: CodeDetailsRow(
            code=CODE,
            reward="3 Golden Keys",
            reward_from="mentalmars",
            expires_on=date(2026, 10, 14),
            expires_from="Gearbox",
            clock_start=NOW,
            updated_at=NOW,
        ),
        CODE_B: CodeDetailsRow(
            code=CODE_B, reward="a skin", category="permanent", clock_start=NOW, updated_at=NOW
        ),
    }


@pytest.mark.parametrize("header", HEADERS, ids=[f"h{i}" for i in range(len(HEADERS))])
@pytest.mark.parametrize("golden_prefix", [False, True])
@pytest.mark.parametrize("with_details", [False, True])
def test_any_header_gets_the_same_body_the_terminal_header_gets(
    header, golden_prefix, with_details
):
    details = _details() if with_details else None
    kw = dict(golden_prefix=golden_prefix, details=details, now=NOW)
    got = render_alert_edit(header, _cands(), **kw)
    want = render_alert_edit(TERMINAL_HEADER, _cands(), **kw)
    assert _body(got, header) == _body(want, TERMINAL_HEADER)
    assert fence_bodies(_body(got, header)) == [CODE, CODE_B]
    assert discord_len(got) <= 2000


@pytest.mark.parametrize("header", HEADERS, ids=[f"h{i}" for i in range(len(HEADERS))])
def test_a_header_is_never_changed_trimmed_or_given_a_ping(header):
    got = render_alert_edit(header, _cands(), golden_prefix=False, details=None, now=NOW)
    assert got.startswith(header + "\n\n")
    if "@everyone" not in header and "<@&" not in header:
        assert "@everyone" not in got and "<@&" not in got


@pytest.mark.parametrize("header", HEADERS, ids=[f"h{i}" for i in range(len(HEADERS))])
def test_no_header_gets_a_details_line_in_the_old_wording(header):
    got = render_alert_edit(header, _cands(), golden_prefix=False, details=_details(), now=NOW)
    body = _body(got, header)
    assert "details via" not in body
    assert "`reward` 3 Golden Keys" in body
    assert "`src` r/Borderlands4" in body


def test_no_candidates_is_just_the_header_whatever_it_is():
    for header in HEADERS:
        assert render_alert_edit(header, [], golden_prefix=False, details={}, now=NOW) == header


# --- links go before anything fails ---


def _long_cands():
    url = "https://example.com/" + "p" * 200
    return [
        CodeCandidate(code=c, golden=False, source_name="r/Borderlands4", item_url=url, fresh=True)
        for c in (CODE, CODE_B)
    ]


def _size_with_links(header):
    text = render_alert_edit(header, _long_cands(), golden_prefix=False, details=None, now=NOW)
    return discord_len(text), text


def test_links_are_kept_when_the_message_lands_exactly_on_the_limit():
    base, _ = _size_with_links("")
    header = "h" * (2000 - base)
    size, text = _size_with_links(header)
    assert size == 2000
    assert text.count(" · <https://") == 2


def test_links_are_dropped_when_one_unit_too_many_and_everything_else_stays():
    base, _ = _size_with_links("")
    header = "h" * (2000 - base + 1)
    size, text = _size_with_links(header)
    assert size <= 2000
    assert " · <" not in text
    assert text.count("`src` r/Borderlands4") == 2
    assert fence_bodies(_body(text, header)) == [CODE, CODE_B]


def test_a_header_too_long_even_without_links_raises_value_error_with_the_documented_text():
    with pytest.raises(ValueError, match=r"exceeds 2000 UTF-16 units .* even without links"):
        render_alert_edit("h" * 1990, _long_cands(), golden_prefix=False, details=None, now=NOW)


def test_an_astral_header_counts_in_utf16_units_not_characters():
    # 1,000 emoji are 1,000 characters and 2,000 units; the message can't fit.
    with pytest.raises(ValueError):
        render_alert_edit("\U0001f600" * 1000, _cands(), golden_prefix=False, details=None, now=NOW)


# --- the worst message the packer makes, edited with the worst details ---

_NAMES = ["n" * 100, "*" * 100, "_" * 100, "@everyone" * 11, "<t:1:R>" * 14, "\\" * 100]


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize(
    ("ping", "mention", "test"),
    [(False, None, False), (True, None, False), (True, "<@&18446744073709551615>", True)],
)
@pytest.mark.parametrize("old_header", [False, True])
def test_a_packed_message_edited_with_the_longest_details_fits_or_raises_value_error(
    name, ping, mention, test, old_header
):
    other = "m" * 100
    url = "https://example.com/" + "p" * 200
    cands = [
        CodeCandidate(
            code=f"AAAA{i % 10}-AAAAA-AAAAA-AAAAA-A{i:04d}",
            golden=i % 2 == 0,
            source_name=name,
            item_url=url,
            fresh=True,
        )
        for i in range(14)
    ]
    reserve = details_reserve([name, other])
    packed = render_code_alerts(
        cands, ping=ping, ping_mention=mention, test=test, nonce_scope="1:2", reserve=reserve
    )
    assert len(packed) >= 2
    by_code = {c.code: c for c in cands}
    for alert in packed:
        members = [by_code[c] for c in alert.codes]
        worst = {
            c.code: CodeDetailsRow(
                code=c.code,
                reward="*" * 400,
                reward_from=name,
                expires_on=date(5000, 12, 31),
                expires_from=other,
                clock_start=NOW,
                updated_at=NOW,
            )
            for c in members
        }
        headers = {alert.header, alert.header.removeprefix(alert.ping_prefix)}
        if old_header:
            headers = {alert.ping_prefix + "**New SHiFT codes**", "**New SHiFT codes**"}
        for header in headers:
            try:
                got = render_alert_edit(
                    header, members, golden_prefix=alert.golden_prefix, details=worst, now=NOW
                )
            except ValueError as exc:
                assert "even without links" in str(exc)  # documented, not a crash
                pytest.fail(f"the packer's own message could not be edited: {header!r}: {exc}")
            assert discord_len(got) <= 2000
            assert got.count(f"<{url}>") == len(members), "a link shed that the reserve covers"
