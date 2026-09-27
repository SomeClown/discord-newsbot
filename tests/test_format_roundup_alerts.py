"""Tests for `bot.format.render_roundup_alerts` (design.md §13, plan step 5).

v1 never rendered a roundup message at all -- every roundup code was
recorded silently, forever. v2.0 posts the fresh ones, unpinged, headed
"SHiFT codes from a roundup"; this file pins what those messages look
like and, same as `test_format_code_alerts.py`, that "@everyone" can
never show up in one no matter what a source name or a batch size throws
at it.
"""

from __future__ import annotations

import pytest

from newsbot.bot.format import discord_len, render_roundup_alerts
from newsbot.shift.decide import CodeCandidate

CODE_A = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"


def _candidate(**kwargs) -> CodeCandidate:
    defaults = dict(
        code=CODE_A,
        golden=False,
        source_name="Reddit Megathread",
        item_url="https://e.com/roundup",
        fresh=True,
        roundup=True,
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


# --- wording / shape ---


def test_single_code_header_and_block():
    rendered = render_roundup_alerts([_candidate()])
    assert len(rendered) == 1
    content = rendered[0].content
    assert content.startswith("**SHiFT codes from a roundup** · Reddit Megathread")
    assert f"```\n{CODE_A}\n```" in content


def test_never_pings():
    rendered = render_roundup_alerts([_candidate()])
    assert rendered[0].ping is False
    assert "@everyone" not in rendered[0].content


def test_empty_candidates_returns_no_messages():
    assert render_roundup_alerts([]) == []


def test_link_shown_for_a_safe_url():
    rendered = render_roundup_alerts([_candidate(item_url="https://example.com/roundup-thread")])
    assert "<https://example.com/roundup-thread>" in rendered[0].content


def test_unsafe_url_omits_the_link_but_keeps_the_header_and_code():
    rendered = render_roundup_alerts([_candidate(item_url="javascript:alert(1)")])
    content = rendered[0].content
    assert "SHiFT codes from a roundup" in content
    assert CODE_A in content
    assert "<javascript:" not in content


# --- two items -> two headers ---


def test_two_roundup_items_produce_two_headed_messages():
    item_a = _candidate(code=CODE_A, source_name="Item A", item_url="https://e.com/a")
    item_b = _candidate(code=CODE_B, source_name="Item B", item_url="https://e.com/b")
    rendered = render_roundup_alerts([item_a, item_b])
    assert len(rendered) == 2
    assert "Item A" in rendered[0].content
    assert "Item B" in rendered[1].content
    assert rendered[0].codes == [CODE_A]
    assert rendered[1].codes == [CODE_B]


def test_multiple_codes_same_item_share_one_header():
    same_item = [
        _candidate(code=CODE_A, source_name="Same", item_url="https://e.com/same"),
        _candidate(code=CODE_B, source_name="Same", item_url="https://e.com/same"),
    ]
    rendered = render_roundup_alerts(same_item)
    assert len(rendered) == 1
    assert rendered[0].content.count("SHiFT codes from a roundup") == 1
    assert rendered[0].codes == [CODE_A, CODE_B]


# --- escaping ---


def test_hostile_source_name_is_escaped():
    hostile = "@everyone <@123456> __pwned__"
    rendered = render_roundup_alerts([_candidate(source_name=hostile)])
    assert "@everyone" not in rendered[0].content


def test_not_a_code_raises():
    with pytest.raises(ValueError, match="not a SHiFT code"):
        render_roundup_alerts([_candidate(code="not-a-code")])


# --- overflow / splitting within one roundup item ---


def test_many_codes_one_item_split_into_continuation_messages():
    codes = [
        _candidate(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            source_name="Big Roundup" * 20,
            item_url=f"https://example.com/{'a' * 300}",
        )
        for i in range(80)
    ]
    rendered = render_roundup_alerts(codes)
    assert len(rendered) > 1
    for i, message in enumerate(rendered):
        assert discord_len(message.content) <= 2000
        assert message.ping is False
        assert "@everyone" not in message.content
        if i == 0:
            assert message.content.startswith("**SHiFT codes from a roundup**")
        else:
            assert message.content.startswith("**(continued)**")

    all_codes = [code for message in rendered for code in message.codes]
    assert len(all_codes) == 80
    assert len(set(all_codes)) == 80


def test_two_items_each_needing_a_split_never_mix_codes_across_items():
    item_a = [
        _candidate(
            code=f"A{i:04d}-AAAAA-AAAAA-AAAAA-AAAAA",
            source_name="Item A" * 40,
            item_url=f"https://example.com/{'a' * 300}",
        )
        for i in range(40)
    ]
    item_b = [
        _candidate(
            code=f"B{i:04d}-BBBBB-BBBBB-BBBBB-BBBBB",
            source_name="Item B" * 40,
            item_url=f"https://example.com/{'b' * 300}",
        )
        for i in range(40)
    ]
    rendered = render_roundup_alerts(item_a + item_b)
    for message in rendered:
        codes = set(message.codes)
        assert codes <= {c.code for c in item_a} or codes <= {c.code for c in item_b}


# --- header shrinking (QA follow-up: a huge url/source must not blow the budget) ---


def test_huge_url_and_source_name_do_not_raise_and_still_post_the_code():
    huge_url = "https://example.com/" + "a" * 2010
    huge_source = "Reddit Megathread " * 40
    rendered = render_roundup_alerts([_candidate(item_url=huge_url, source_name=huge_source)])
    assert len(rendered) == 1
    content = rendered[0].content
    assert discord_len(content) <= 2000
    assert f"```\n{CODE_A}\n```" in content
    assert rendered[0].codes == [CODE_A]


def test_huge_url_is_dropped_before_source_name_is_truncated():
    # A source name that comfortably fits without the link should survive
    # intact once the link is dropped -- shedding order matters.
    huge_url = "https://example.com/" + "a" * 2010
    rendered = render_roundup_alerts([_candidate(item_url=huge_url, source_name="Short Name")])
    content = rendered[0].content
    assert "Short Name" in content
    assert huge_url not in content


def test_huge_url_and_huge_source_name_together_across_many_codes():
    huge_url = "https://example.com/" + "a" * 2010
    huge_source = "Reddit Megathread " * 40
    candidates = [
        _candidate(
            code=f"A{i:04d}-AAAAA-AAAAA-AAAAA-AAAAA", item_url=huge_url, source_name=huge_source
        )
        for i in range(10)
    ]
    rendered = render_roundup_alerts(candidates)
    for message in rendered:
        assert discord_len(message.content) <= 2000
    all_codes = [code for message in rendered for code in message.codes]
    assert len(all_codes) == 10


# --- nonces ---


def test_nonce_is_deterministic_and_distinct_from_the_normal_alert_scheme():
    rendered_once = render_roundup_alerts([_candidate()])
    rendered_twice = render_roundup_alerts([_candidate()])
    assert rendered_once[0].nonce == rendered_twice[0].nonce
    assert rendered_once[0].nonce != ""
