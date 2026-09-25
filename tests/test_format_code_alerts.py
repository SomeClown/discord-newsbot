"""Tests for `bot.format.render_code_alerts` (plan step 6).

Every case here is rendering, not I/O -- `shift/sweep.py` is what decides
which candidates to post at all; this file just pins what the resulting
Discord message(s) look like, and that "@everyone" can never show up
uninvited.
"""

from __future__ import annotations

import pytest

from newsbot.bot.format import discord_len, render_code_alerts
from newsbot.shift.decide import CodeCandidate

CODE_A = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"
CODE_B = "BBBBB-BBBBB-BBBBB-BBBBB-BBBBB"


def _candidate(**kwargs) -> CodeCandidate:
    defaults = dict(
        code=CODE_A,
        golden=False,
        source_name="Gearbox Blog",
        item_url="https://e.com/a",
        fresh=True,
    )
    defaults.update(kwargs)
    return CodeCandidate(**defaults)


# --- wording ---


def test_single_plain_code_wording():
    rendered = render_code_alerts([_candidate()], ping=True)
    assert len(rendered) == 1
    assert "**New SHiFT code**" in rendered[0].content
    assert "New SHiFT codes" not in rendered[0].content


def test_multiple_plain_codes_wording_is_plural():
    candidates = [_candidate(code=CODE_A), _candidate(code=CODE_B)]
    rendered = render_code_alerts(candidates, ping=True)
    assert "**New SHiFT codes**" in rendered[0].content


def test_all_golden_wording():
    rendered = render_code_alerts([_candidate(golden=True)], ping=True)
    assert "**New Golden Key code**" in rendered[0].content


def test_all_golden_multiple_wording_is_plural():
    candidates = [_candidate(code=CODE_A, golden=True), _candidate(code=CODE_B, golden=True)]
    rendered = render_code_alerts(candidates, ping=True)
    assert "**New Golden Key codes**" in rendered[0].content


def test_mixed_batch_uses_plain_title_with_golden_key_prefix():
    candidates = [_candidate(code=CODE_A, golden=False), _candidate(code=CODE_B, golden=True)]
    rendered = render_code_alerts(candidates, ping=True)
    content = rendered[0].content
    assert "**New SHiFT codes**" in content
    assert "Golden Key: " in content
    # The non-golden entry shouldn't get the prefix.
    assert f"Golden Key: {'Gearbox Blog'}" not in content.split(CODE_A)[1].split(CODE_B)[0]


def test_non_mixed_batch_has_no_golden_key_prefix():
    rendered = render_code_alerts([_candidate(golden=True)], ping=True)
    assert "Golden Key: " not in rendered[0].content


# --- ping / mentions ---


def test_ping_true_includes_everyone_once():
    rendered = render_code_alerts([_candidate()], ping=True)
    assert rendered[0].content.startswith("@everyone ")
    assert rendered[0].ping is True


def test_ping_false_never_mentions_everyone():
    rendered = render_code_alerts([_candidate()], ping=False)
    assert "@everyone" not in rendered[0].content
    assert rendered[0].ping is False


def test_test_prefix_shown():
    rendered = render_code_alerts([_candidate()], ping=False, test=True)
    assert "[TEST] " in rendered[0].content


# --- escaping and safety ---


def test_hostile_source_name_is_escaped():
    hostile = "@everyone <@123456> __pwned__"
    rendered = render_code_alerts([_candidate(source_name=hostile)], ping=True)
    content = rendered[0].content
    # Only the header's own leading "@everyone " -- the source name's copy
    # must have been neutralized by esc().
    assert content.count("@everyone") == 1


def test_code_appears_in_a_fenced_block():
    rendered = render_code_alerts([_candidate()], ping=True)
    assert f"```\n{CODE_A}\n```" in rendered[0].content


def test_unsafe_url_omits_the_link_but_keeps_the_code():
    rendered = render_code_alerts([_candidate(item_url="javascript:alert(1)")], ping=True)
    content = rendered[0].content
    assert CODE_A in content
    assert "<javascript:" not in content


def test_url_metacharacters_are_re_encoded():
    hostile_url = "https://example.com/<script>"
    rendered = render_code_alerts([_candidate(item_url=hostile_url)], ping=True)
    assert "<script>" not in rendered[0].content


def test_not_a_code_raises():
    with pytest.raises(ValueError, match="not a SHiFT code"):
        render_code_alerts([_candidate(code="not-a-code")], ping=True)


def test_empty_candidates_returns_no_messages():
    assert render_code_alerts([], ping=True) == []


# --- overflow / splitting ---


def test_many_codes_with_long_urls_split_and_only_first_pings():
    candidates = [
        _candidate(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            item_url=f"https://example.com/{'a' * 300}/{i}",
        )
        for i in range(12)
    ]
    rendered = render_code_alerts(candidates, ping=True)

    assert len(rendered) > 1
    for i, message in enumerate(rendered):
        assert discord_len(message.content) <= 2000
        if i == 0:
            assert message.content.startswith("@everyone ")
            assert message.ping is True
        else:
            assert message.content.startswith("**(continued)**")
            assert "@everyone" not in message.content
            assert message.ping is False

    all_codes = [code for message in rendered for code in message.codes]
    assert len(all_codes) == 12
    assert len(set(all_codes)) == 12
    assert all_codes == [c.code for c in candidates]


def test_split_batch_when_unpinged_never_pings_any_message():
    candidates = [
        _candidate(
            code=f"{i:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
            item_url=f"https://example.com/{'a' * 300}/{i}",
        )
        for i in range(12)
    ]
    rendered = render_code_alerts(candidates, ping=False)
    assert len(rendered) > 1
    assert all(not m.ping for m in rendered)
    assert all("@everyone" not in m.content for m in rendered)
