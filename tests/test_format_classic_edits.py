"""Pins the v3.1 ("classic") alert layout as `render_alert_edit` rewrites it.

Alerts posted before v3.2.0 stay in the channel and stay editable for 14 days, so an
edit of one has to come back out looking exactly like it did the first time: same
header, same `details via` wording, same plain source line. Every expected string below
is a literal on purpose. These tests were written against the v3.1.4 code before the
v3.2.0 layout existed and are not supposed to change afterwards; if one of them has to be
edited, a pre-3.2.0 alert somewhere is about to change shape under somebody.

The last group is the promise the classic layout lives on: a message packed under v3.1.4's
details reserve (hard-coded here, because it's history and not a live constant) still
fits its worst-case edit, link included.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from newsbot.bot.format import discord_len, esc, render_alert_edit
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeDetailsRow

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
URL = "https://example.com/post"
NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
# Pacific midnight at the end of Oct 14, 2026 (PDT, UTC-7), worked out by hand.
OCT_14_ENDS = 1792047600
FENCE = "```\n{}\n```"


def _cand(
    code: str = CODE, *, golden: bool = False, source: str = "r/Borderlands4", url: str = URL
):
    return CodeCandidate(code=code, golden=golden, source_name=source, item_url=url, fresh=True)


def _row(code: str = CODE, **kwargs) -> CodeDetailsRow:
    defaults = dict(code=code, clock_start=NOW, updated_at=NOW)
    defaults.update(kwargs)
    return CodeDetailsRow(**defaults)


def _edit(header, candidates, *, details=None, golden_prefix=False):
    return render_alert_edit(
        header, candidates, golden_prefix=golden_prefix, details=details, now=NOW
    )


# --- headers ---


@pytest.mark.parametrize(
    "header",
    [
        "@everyone **New SHiFT code**",
        "<@&123> **New SHiFT codes**",
        "**New Golden Key code**",
        "**[TEST] New SHiFT code**",
        "**(continued)**",
    ],
)
def test_the_stored_header_comes_back_untouched_above_the_classic_block(header):
    got = _edit(header, [_cand()])
    assert got == f"{header}\n\n```\n{CODE}\n```\nr/Borderlands4 · <{URL}>"


# --- details ---


def test_no_details_is_the_bare_block():
    assert _edit("**New SHiFT code**", [_cand()], details={}) == (
        f"**New SHiFT code**\n\n```\n{CODE}\n```\nr/Borderlands4 · <{URL}>"
    )


def test_a_row_with_nothing_to_show_is_the_bare_block():
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: _row()})
    assert got == f"**New SHiFT code**\n\n```\n{CODE}\n```\nr/Borderlands4 · <{URL}>"


def test_reward_only():
    row = _row(reward="3 Golden Keys", reward_from="mentalmars")
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: row})
    assert got == (
        "**New SHiFT code**\n\n"
        f"```\n{CODE}\n```\n"
        "3 Golden Keys · details via mentalmars\n"
        f"r/Borderlands4 · <{URL}>"
    )


def test_expiry_only():
    row = _row(expires_on=date(2026, 10, 14), expires_from="Gearbox")
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: row})
    assert got == (
        "**New SHiFT code**\n\n"
        f"```\n{CODE}\n```\n"
        f"expires Oct 14, 2026 (<t:{OCT_14_ENDS}:R>) · details via Gearbox\n"
        f"r/Borderlands4 · <{URL}>"
    )


def test_permanent_says_permanent_and_credits_whoever_set_the_category():
    row = _row(category="permanent", category_from="mentalmars", expires_on=date(2026, 10, 14))
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: row})
    assert got == (
        "**New SHiFT code**\n\n"
        f"```\n{CODE}\n```\n"
        "permanent · details via mentalmars\n"
        f"r/Borderlands4 · <{URL}>"
    )


def test_two_different_via_names_are_joined_with_and():
    row = _row(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="Gearbox",
    )
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: row})
    assert got == (
        "**New SHiFT code**\n\n"
        f"```\n{CODE}\n```\n"
        f"3 Golden Keys · expires Oct 14, 2026 (<t:{OCT_14_ENDS}:R>) "
        "· details via mentalmars and Gearbox\n"
        f"r/Borderlands4 · <{URL}>"
    )


def test_the_same_via_name_twice_is_said_once():
    row = _row(
        reward="3 Golden Keys",
        reward_from="mentalmars",
        expires_on=date(2026, 10, 14),
        expires_from="mentalmars",
    )
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: row})
    assert got.endswith(f"(<t:{OCT_14_ENDS}:R>) · details via mentalmars\nr/Borderlands4 · <{URL}>")


def test_reward_text_is_escaped():
    row = _row(reward="@everyone **keys**", reward_from="mentalmars")
    got = _edit("**New SHiFT code**", [_cand()], details={CODE: row})
    assert "@everyone **keys**" not in got
    assert esc("@everyone **keys**") + " · details via mentalmars" in got


# --- a mixed batch ---


def test_a_mixed_batch_prefixes_only_the_golden_entry():
    got = _edit(
        "@everyone **New SHiFT codes**",
        [_cand(CODE, golden=True), _cand(CODE_B)],
        details={CODE: _row(reward="3 Golden Keys", reward_from="mentalmars")},
        golden_prefix=True,
    )
    assert got == (
        "@everyone **New SHiFT codes**\n\n"
        f"```\n{CODE}\n```\n"
        "3 Golden Keys · details via mentalmars\n"
        f"Golden Key: r/Borderlands4 · <{URL}>\n\n"
        f"```\n{CODE_B}\n```\n"
        f"r/Borderlands4 · <{URL}>"
    )


# --- the link is the one thing that sheds ---


def _wide_candidates(n: int, url: str):
    return [
        _cand(f"AAAA{i % 10}-AAAAA-AAAAA-AAAAA-A{i:04d}", source="s" * 100, url=url)
        for i in range(n)
    ]


def test_over_two_thousand_units_every_link_goes_and_nothing_else():
    header = "**New SHiFT codes**"
    url = "https://example.com/" + "p" * 100
    cands = _wide_candidates(13, url)
    with_links = "\n\n".join(
        [header, *(f"```\n{c.code}\n```\n{'s' * 100} · <{url}>" for c in cands)]
    )
    without = "\n\n".join([header, *(f"```\n{c.code}\n```\n{'s' * 100}" for c in cands)])
    assert discord_len(with_links) > 2000 >= discord_len(without)
    assert _edit(header, cands) == without


def test_a_message_that_fits_with_its_links_keeps_them():
    header = "**New SHiFT codes**"
    cands = _wide_candidates(3, URL)
    got = _edit(header, cands)
    assert got.count(f"<{URL}>") == 3


def test_still_too_big_without_links_raises():
    header = "**New SHiFT codes**"
    cands = _wide_candidates(20, URL)
    with pytest.raises(ValueError, match="even without links"):
        _edit(header, cands)


# --- v3.1.4's reserve still holds a classic edit ---


@pytest.mark.parametrize(
    ("name_char", "other_char", "longest", "min_codes"),
    [
        # Plain names: L is 100 and two codes share a message.
        ("n", "m", 100, 2),
        # Names that are all markdown escape to 200 units, as bad as a name gets; one code
        # then fills a message by itself.
        ("*", "_", 200, 1),
    ],
)
def test_a_message_packed_under_the_v314_reserve_fits_its_worst_case_edit(
    name_char, other_char, longest, min_codes
):
    # v3.1.4's per-code reserve was 364 + 2L, L being the longest escaped source name.
    # Hard-coded: this is history, and it must not follow the live constant around.
    long_name = name_char * 100
    other_name = other_char * 100
    assert discord_len(esc(long_name)) == longest
    reserve = 364 + 2 * longest
    header = "@everyone **New SHiFT codes**"
    url = "https://example.com/" + "p" * 150

    def candidate(i: int):
        return _cand(f"AAAA{i % 10}-AAAAA-AAAAA-AAAAA-A{i:04d}", source=long_name, url=url)

    # Size a block the way the old packer did: the entry without its line, plus the reserve.
    bare = _edit(header, [candidate(0)])
    block_len = discord_len(bare) - discord_len(header) - 2
    slot = block_len + reserve + 2
    count = (2000 - discord_len(header)) // slot
    assert count >= min_codes
    cands = [candidate(i) for i in range(count)]

    worst = {
        c.code: _row(
            c.code,
            reward="*" * 400,  # shown capped at 300 units
            reward_from=long_name,
            expires_on=date(2026, 9, 30),  # the longest date there is
            expires_from=other_name,
        )
        for c in cands
    }
    got = _edit(header, cands, details=worst)
    assert discord_len(got) <= 2000
    assert got.count(f"<{url}>") == count  # every link survived
