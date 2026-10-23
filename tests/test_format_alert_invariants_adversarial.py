"""Adversarial round on task 6's one promise: nothing here ever comes out over 2,000 units.

`test_format_alert_packing.py` pins the shapes. This file leans on the promise from the
outside, the way a hostile scraper and a renamed source would: every combination of ping
header, golden prefix and continuation header, source names whose escaped form is twice
their length (markdown), made of surrogate pairs (emoji) or of combining marks, rewards
sitting exactly on the 300-unit cap, and the longest expiry phrase. The central test
renders N codes with no details at all and then edits every message with the worst
details the reserve was sized for, for N from 1 to 40.

Invisible and bidi characters are written as `\\uXXXX` escapes on purpose; the file stays
pure ASCII.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from newsbot.bot.format import (
    FOLLOWUP_MAX_CODES,
    details_line,
    details_reserve,
    discord_len,
    esc,
    render_alert_edit,
    render_code_alerts,
    render_followup_alerts,
    render_roundup_alerts,
)
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeDetailsRow

NOW = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)  # 23:30 PDT on Oct 9
ROLE20 = "<@&" + "1" * 20 + ">"
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
BIDI = "\u202e\u2066"

# Source names as the config allows them: at most 100 characters, which is at most 200
# units once escaped (a character is worth at most two units shown).
NAMES = {
    "plain": "Gearbox Blog",
    "stars": "*" * 100,
    "emoji": "\U0001f600" * 100,
    "combining": "e\u0301" * 50,
    "combining_stack": "a" + "\u0301" * 99,
    "mentions": "@everyone" * 11,
    "backslashes": "\\" * 100,
    "stars_and_emoji": "*\U0001f600" * 50,
    "zwj_families": FAMILY * 20,
}

# Rewards that are exactly 300 shown units once escaped and stripped of bidi controls
# (a test below pins that, so the cap is hit on the nose and not merely approached).
REWARDS = {
    "plain300": "x" * 300,
    "stars150": "*" * 150,
    "emoji150": "\U0001f600" * 150,
    "mention300": "@everyone" * 30,  # 270 characters, 300 shown
    "combining150": "e\u0301" * 150,
    "bidi_padded": "x" * 300 + BIDI * 20,
    "bidi_in_mentions": "@\u202eeveryone" * 30,
    "backslash150": "\\" * 150,
    "family": FAMILY * 37 + "xxxx",
}

PINGS = {
    "everyone": dict(ping=True, ping_mention="@everyone"),
    "role20": dict(ping=True, ping_mention=ROLE20),
    "default_mention": dict(ping=True, ping_mention=None),
    "no_ping": dict(ping=False, ping_mention=None),
}

LONG_URL = "https://example.com/" + "a/" * 700


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _candidate(i: int, **kwargs) -> CodeCandidate:
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


def _worst(code: str, name_a: str, name_b: str, reward: str) -> CodeDetailsRow:
    return _row(
        code,
        reward=reward,
        reward_from=name_a,
        expires_on=date(5000, 12, 31),  # the widest date, with an 11-digit timestamp
        expires_from=name_b,
    )


def _twin(name: str) -> str:
    """A different name exactly as long once escaped (so `via` A and B is the worst)."""
    return name.replace("*", "_").replace("x", "y").replace("e", "i") or name


def _golden_pattern(n: int, i: int) -> bool:
    return (False, True, i % 3 == 0)[n % 3]


# --- the cap itself ---


@pytest.mark.parametrize("reward", REWARDS)
def test_every_hostile_reward_is_exactly_the_cap_and_not_cut(reward):
    line = details_line(_row(_code(0), reward=REWARDS[reward]), now=NOW)
    assert line is not None
    shown = line.removeprefix("`reward` ")
    assert discord_len(shown) == 300
    assert "\u2026" not in line


@pytest.mark.parametrize("reward", REWARDS)
def test_one_unit_past_the_cap_is_cut_to_fit_with_an_ellipsis(reward):
    line = details_line(_row(_code(0), reward=REWARDS[reward] + "q"), now=NOW)
    assert line.endswith("\u2026")
    assert 290 <= discord_len(line.removeprefix("`reward` ")) <= 300


def test_the_longest_expiry_value_is_what_the_reserve_assumed():
    # A date plus an 11-digit live timestamp: " (<t:", the digits, ":R>)" after the date.
    line = details_line(_row(_code(0), expires_on=date(5000, 12, 31)), now=NOW)
    assert line == "`expires` Dec 31, 5000 (<t:95649148800:R>)"
    assert discord_len(line.removeprefix("`expires` ")) == 32
    now_ish = details_line(_row(_code(0), expires_on=date(2026, 10, 14)), now=NOW)
    assert now_ish == "`expires` Oct 14, 2026 (<t:1792047600:R>)"
    # Today's timestamps are one digit short of the reserve.
    assert discord_len(now_ish.removeprefix("`expires` ")) == 31


# --- first render, with the worst details already present ---


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("ping", PINGS)
def test_first_render_with_worst_details_is_under_2000(name, ping):
    a, b = NAMES[name], _twin(NAMES[name])
    reserve = details_reserve([a, b])
    for n in range(1, 41):
        batch = [
            _candidate(
                i,
                golden=_golden_pattern(n, i),
                source_name=a if i % 2 else "Gearbox Blog",
                item_url=LONG_URL if i % 4 == 0 else f"https://e.com/a{i}",
            )
            for i in range(n)
        ]
        rewards = list(REWARDS.values())
        details = {
            c.code: _worst(c.code, a, b, rewards[i % len(rewards)]) for i, c in enumerate(batch)
        }
        alerts = render_code_alerts(
            batch, test=n % 5 == 0, reserve=reserve, details=details, now=NOW, **PINGS[ping]
        )
        assert [c for alert in alerts for c in alert.codes] == [c.code for c in batch]
        assert [alert.ping for alert in alerts] == [PINGS[ping]["ping"]] + [False] * (
            len(alerts) - 1
        )
        for alert in alerts:
            assert discord_len(alert.content) <= 2000, (name, ping, n)
            assert alert.content.startswith(alert.header)


# --- the edit-fits guarantee ---


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("ping", PINGS)
def test_render_bare_then_edit_with_worst_details_always_fits_for_1_to_40_codes(name, ping):
    a, b = NAMES[name], _twin(NAMES[name])
    reserve = details_reserve([a, b, "mentalmars"])
    rewards = list(REWARDS.values())
    for n in range(1, 41):
        batch = [
            _candidate(
                i,
                golden=_golden_pattern(n, i),
                source_name=a if i % 2 else "Gearbox Blog",
                item_url=LONG_URL if i % 4 == 0 else f"https://e.com/a{i}",
            )
            for i in range(n)
        ]
        by_code = {c.code: c for c in batch}
        alerts = render_code_alerts(batch, test=n % 7 == 0, reserve=reserve, **PINGS[ping])
        assert [c for alert in alerts for c in alert.codes] == [c.code for c in batch]
        for alert in alerts:
            assert discord_len(alert.content) <= 2000
            details = {
                code: _worst(code, a, b, rewards[(i + n) % len(rewards)])
                for i, code in enumerate(alert.codes)
            }
            edited = render_alert_edit(
                alert.header,
                [by_code[code] for code in alert.codes],
                golden_prefix=alert.golden_prefix,
                details=details,
                now=NOW,
            )
            assert discord_len(edited) <= 2000, (name, ping, n, alert.codes)
            assert edited.startswith(alert.header)
            assert all(code in edited for code in alert.codes)


@pytest.mark.parametrize("reward", REWARDS)
def test_edit_with_one_worst_reward_for_every_code_fits(reward):
    names = ["mentalmars", "Borderlands (Bluesky)"]
    reserve = details_reserve(names)
    batch = [_candidate(i, golden=i % 2 == 0) for i in range(40)]
    by_code = {c.code: c for c in batch}
    for alert in render_code_alerts(batch, ping=True, ping_mention=ROLE20, reserve=reserve):
        details = {c: _worst(c, *names, REWARDS[reward]) for c in alert.codes}
        edited = render_alert_edit(
            alert.header,
            [by_code[c] for c in alert.codes],
            golden_prefix=alert.golden_prefix,
            details=details,
            now=NOW,
        )
        assert discord_len(edited) <= 2000


def test_an_edit_whose_ping_was_stripped_is_still_under_the_original():
    reserve = details_reserve(["mentalmars"])
    batch = [_candidate(i) for i in range(12)]
    by_code = {c.code: c for c in batch}
    for alert in render_code_alerts(batch, ping=True, ping_mention=ROLE20, reserve=reserve):
        stripped = alert.header.removeprefix(alert.ping_prefix)
        details = {c: _worst(c, "mentalmars", "mentalmars", "x" * 300) for c in alert.codes}
        edited = render_alert_edit(
            stripped,
            [by_code[c] for c in alert.codes],
            golden_prefix=alert.golden_prefix,
            details=details,
            now=NOW,
        )
        assert discord_len(edited) <= 2000
        assert "<@&" not in edited


def test_continuation_messages_edit_under_their_own_header():
    reserve = details_reserve(["mentalmars"])
    batch = [_candidate(i, golden=i % 2 == 0) for i in range(30)]
    by_code = {c.code: c for c in batch}
    alerts = render_code_alerts(batch, ping=True, ping_mention=ROLE20, reserve=reserve)
    assert len(alerts) > 2
    for alert in alerts[1:]:
        assert alert.header == "`> new SHiFT codes (continued) ▌`"
        assert alert.golden_prefix is True  # the batch was mixed; a continuation keeps the flag
        details = {c: _worst(c, "mentalmars", "mentalmars", "x" * 300) for c in alert.codes}
        edited = render_alert_edit(
            alert.header,
            [by_code[c] for c in alert.codes],
            golden_prefix=alert.golden_prefix,
            details=details,
            now=NOW,
        )
        assert edited.startswith("`> new SHiFT codes (continued) ▌`\n\n")
        assert discord_len(edited) <= 2000


# --- a renamed source: the reserve was computed for shorter names ---


def test_via_names_longer_than_the_reserve_give_a_value_error_never_an_oversize_edit():
    reserve = details_reserve(["mentalmars"])
    long_a, long_b = "*" * 100, "_" * 100  # 200 shown units each; the reserve assumed 10
    batch = [_candidate(i) for i in range(40)]
    by_code = {c.code: c for c in batch}
    alerts = render_code_alerts(batch, ping=True, ping_mention=ROLE20, reserve=reserve)
    raised = 0
    for alert in alerts:
        details = {c: _worst(c, long_a, long_b, "x" * 300) for c in alert.codes}
        try:
            edited = render_alert_edit(
                alert.header,
                [by_code[c] for c in alert.codes],
                golden_prefix=alert.golden_prefix,
                details=details,
                now=NOW,
            )
        except ValueError:
            raised += 1
        else:
            assert discord_len(edited) <= 2000
    assert raised, "the messages were packed full, so a longer name has to overflow some"


def test_a_line_bigger_than_the_reserve_is_never_packed_into_an_oversize_first_render():
    reserve = details_reserve(["mentalmars"])
    batch = [_candidate(i, golden=i % 3 == 0) for i in range(40)]
    details = {c.code: _worst(c.code, "*" * 100, "_" * 100, "x" * 300) for c in batch}
    alerts = render_code_alerts(
        batch, ping=True, ping_mention=ROLE20, reserve=reserve, details=details, now=NOW
    )
    assert [c for a in alerts for c in a.codes] == [c.code for c in batch]
    assert all(discord_len(a.content) <= 2000 for a in alerts)


def test_a_reserve_for_a_longer_name_than_the_edit_uses_has_room_to_spare():
    reserve = details_reserve(["*" * 100])
    batch = [_candidate(i) for i in range(40)]
    by_code = {c.code: c for c in batch}
    for alert in render_code_alerts(batch, ping=True, ping_mention=ROLE20, reserve=reserve):
        details = {c: _worst(c, "m", "n", "x" * 300) for c in alert.codes}
        edited = render_alert_edit(
            alert.header,
            [by_code[c] for c in alert.codes],
            golden_prefix=alert.golden_prefix,
            details=details,
            now=NOW,
        )
        assert discord_len(edited) <= 2000
        assert f"<{by_code[alert.codes[0]].item_url}>" in edited


# --- follow-ups ---


@pytest.mark.parametrize("mention", ["@everyone", "@here", ROLE20])
@pytest.mark.parametrize("who_has_details", ["all", "every_third", "none"])
def test_followups_always_render_in_order_and_each_message_is_as_full_as_it_can_be(
    mention, who_has_details
):
    for n in range(1, 26):
        codes = [_code(i) for i in range(n)]
        details = {}
        for i, code in enumerate(codes):
            if who_has_details == "all" or (who_has_details == "every_third" and i % 3 == 0):
                details[code] = _worst(code, "m" * 100, "n" * 100, "x" * 300)
        alerts = render_followup_alerts(codes, ping_mention=mention, details=details, now=NOW)
        assert alerts, "even the worst single code fits behind a ping"
        assert [c for a in alerts for c in a.codes] == codes  # in order, none lost
        assert all(discord_len(a.content) <= 2000 for a in alerts)
        assert all(len(a.codes) <= FOLLOWUP_MAX_CODES for a in alerts)
        assert [a.ping for a in alerts] == [True] + [False] * (len(alerts) - 1)
        # A message closed early only because the next code did not fit (or it held twenty).
        for here, after in zip(alerts, alerts[1:], strict=False):
            entry = after.content.split("\n\n")[1]
            assert len(here.codes) == FOLLOWUP_MAX_CODES or (
                discord_len(here.content) + 2 + discord_len(entry) > 2000
            )


def test_a_followup_for_one_astral_heavy_code_fits():
    code = _code(0)
    details = {code: _worst(code, "\U0001f600" * 100, "\U0001f601" * 100, "\U0001f600" * 150)}
    [alert] = render_followup_alerts([code], ping_mention=ROLE20, details=details, now=NOW)
    assert discord_len(alert.content) <= 2000


def test_a_followup_with_no_details_for_any_code_fits_at_the_cap():
    codes = [_code(i) for i in range(FOLLOWUP_MAX_CODES)]
    [alert] = render_followup_alerts(codes, ping_mention="@everyone", details={}, now=NOW)
    assert alert.content.startswith("@everyone `> confirmed by a second source ▌`\n\n```\n")
    assert alert.codes == codes


# --- roundups ---


def test_roundups_with_worst_details_stay_under_2000_and_lose_no_code():
    names = ["m" * 100, "n" * 100]
    cands = [
        _candidate(
            i,
            source_name=f"Roundup {i % 3}",
            item_url=f"https://e.com/post{i % 3}",
            roundup=True,
        )
        for i in range(45)
    ]
    rewards = list(REWARDS.values())
    details = {
        c.code: _worst(c.code, *names, rewards[i % len(rewards)]) for i, c in enumerate(cands)
    }
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    assert sorted(c for a in alerts for c in a.codes) == sorted(c.code for c in cands)
    assert all(discord_len(a.content) <= 2000 for a in alerts)
    assert all(a.ping is False for a in alerts)


def test_a_roundup_header_is_the_first_paragraph_of_every_message():
    cands = [
        _candidate(i, source_name="Roundup", item_url="https://e.com/p", roundup=True)
        for i in range(20)
    ]
    details = {c.code: _worst(c.code, "mentalmars", "mentalmars", "x" * 300) for c in cands}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    assert len(alerts) > 1
    assert alerts[0].header.startswith("`> SHiFT roundup ▌` 20 codes from Roundup")
    assert [a.header for a in alerts[1:]] == ["`> SHiFT roundup (continued) ▌`"] * (len(alerts) - 1)
    for alert in alerts:
        assert alert.content.startswith(alert.header + "\n\n")


def test_a_long_post_url_does_not_sink_a_roundup_whose_later_code_has_details():
    url = "https://example.com/" + "a" * 1600
    cands = [
        _candidate(i, source_name="Roundup Blog", item_url=url, roundup=True) for i in range(2)
    ]
    details = {_code(1): _worst(_code(1), "mentalmars", "mentalmars", "x" * 300)}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    assert sorted(c for a in alerts for c in a.codes) == [_code(0), _code(1)]
    assert all(discord_len(a.content) <= 2000 for a in alerts)


def test_the_same_roundup_with_the_detailed_code_first_already_sheds_the_link():
    url = "https://example.com/" + "a" * 1600
    cands = [_candidate(i, source_name="Roundup Blog", item_url=url, roundup=True) for i in (1, 0)]
    details = {_code(1): _worst(_code(1), "mentalmars", "mentalmars", "x" * 300)}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    assert all(discord_len(a.content) <= 2000 for a in alerts)
    assert "<https://" not in alerts[0].content


# --- the escape is part of the arithmetic ---


@pytest.mark.parametrize("name", NAMES)
def test_reserve_counts_names_as_shown(name):
    shown = discord_len(esc(NAMES[name]))
    assert shown <= 200
    assert details_reserve([NAMES[name]]) == 369 + shown + shown
