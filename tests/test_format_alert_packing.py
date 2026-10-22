"""Tests for worst-case packing, edits and follow-ups with details (design.md §16, plan task 6).

The invariant under test: an alert is packed as if every code's details line were as
big as it can ever get, so re-rendering the same message later, with whatever the
details turned out to be, can never come out over 2,000 UTF-16 units. Roundups are
the exception on purpose (nothing edits them, so they get no reservation), and the
"shorten the source name" step is gone, so the failure for a name that cannot fit is a
loud ValueError and not a quiet "…".
"""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime

import pytest

from newsbot.bot import terminal
from newsbot.bot.format import (
    FOLLOWUP_MAX_CODES,
    _followup_content,
    details_line,
    details_reserve,
    discord_len,
    esc,
    fit_followup_codes,
    render_alert_edit,
    render_code_alerts,
    render_followup_alert,
    render_roundup_alerts,
)
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeDetailsRow

NOW = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)  # 23:30 PDT on Oct 9: "under an hour" for Oct 9
ROLE = "<@&123456789012345678>"
POST = "https://e.com/roundup-post"
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"


def _code(i: int) -> str:
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


def _worst_row(code: str, name_a: str, name_b: str, *, reward: str = "x" * 300) -> CodeDetailsRow:
    return _row(
        code,
        reward=reward,
        reward_from=name_a,
        expires_on=date(
            2026, 10, 9
        ),  # a real date and timestamp; the reserve is sized for 11 digits
        expires_from=name_b,
    )


# --- the details line in an alert ---


def test_details_line_sits_between_the_fence_and_the_source_line():
    c = _candidate()
    row = _row(c.code, reward="3 Golden Keys", reward_from="mentalmars")
    [alert] = render_code_alerts([c], ping=False, details={c.code: row}, now=NOW)
    assert (
        f"```\n{c.code}\n```\n`reward` 3 Golden Keys · `via` mentalmars\n`src` Gearbox Blog"
        in alert.content
    )


def test_a_code_with_no_details_gets_no_extra_line():
    c = _candidate()
    [with_map] = render_code_alerts([c], ping=False, details={"other": _row("other")}, now=NOW)
    [without] = render_code_alerts([c], ping=False)
    assert with_map.content == without.content


def test_a_row_with_nothing_to_show_gets_no_extra_line():
    c = _candidate()
    [alert] = render_code_alerts([c], ping=False, details={c.code: _row(c.code)}, now=NOW)
    assert alert.content.count("\n") == render_code_alerts([c], ping=False)[0].content.count("\n")


def test_details_without_a_clock_is_an_error():
    c = _candidate()
    with pytest.raises(ValueError, match="clock"):
        render_code_alerts([c], ping=False, details={c.code: _row(c.code, reward="x")})


def test_rendered_alert_remembers_its_header_and_golden_prefix():
    batch = [_candidate(1, golden=True), _candidate(2)]
    [alert] = render_code_alerts(batch, ping=True, ping_mention=ROLE)
    assert alert.header == f"{ROLE} `> new SHiFT codes ▌`"
    assert alert.content.startswith(alert.header + "\n\n")
    assert alert.golden_prefix is True
    [plain] = render_code_alerts([_candidate(1, golden=True)], ping=False)
    assert plain.golden_prefix is False
    assert plain.header == "`> new Golden Key code ▌`"


def test_continuation_messages_keep_their_own_header():
    batch = [_candidate(i, golden=i % 2 == 0) for i in range(8)]
    alerts = render_code_alerts(batch, ping=True, reserve=details_reserve(["Gearbox Blog"]))
    assert len(alerts) > 1
    assert alerts[0].header.endswith("`> new SHiFT codes ▌`")
    assert all(a.header == "`> new SHiFT codes (continued) ▌`" for a in alerts[1:])
    assert all(a.golden_prefix for a in alerts)
    assert all(a.content.startswith(a.header) for a in alerts)


def test_nonces_do_not_depend_on_details():
    batch = [_candidate(i) for i in range(3)]
    details = {c.code: _row(c.code, reward="x", reward_from="m") for c in batch}
    plain = render_code_alerts(batch, ping=False, nonce_scope="1|2")
    with_details = render_code_alerts(
        batch, ping=False, nonce_scope="1|2", details=details, now=NOW
    )
    assert [a.nonce for a in plain] == [a.nonce for a in with_details]


# --- worst-case reservation ---


def test_the_reserve_changes_how_many_codes_fit_in_a_message():
    batch = [_candidate(i) for i in range(10)]
    assert len(render_code_alerts(batch, ping=False)) == 1
    reserve = details_reserve(["Gearbox Blog", "mentalmars"])
    alerts = render_code_alerts(batch, ping=False, reserve=reserve)
    assert len(alerts) > 1
    assert [c for a in alerts for c in a.codes] == [c.code for c in batch]  # order kept


def test_packing_does_not_depend_on_which_codes_have_details_yet():
    names = ["Gearbox Blog", "mentalmars"]
    reserve = details_reserve(names)
    batch = [_candidate(i) for i in range(12)]
    details = {c.code: _worst_row(c.code, *names) for c in batch}
    bare = render_code_alerts(batch, ping=True, ping_mention=ROLE, reserve=reserve)
    full = render_code_alerts(
        batch, ping=True, ping_mention=ROLE, reserve=reserve, details=details, now=NOW
    )
    assert [a.codes for a in bare] == [a.codes for a in full]
    assert all(discord_len(a.content) <= 2000 for a in full)


def test_every_alert_rerendered_with_worst_case_details_still_fits():
    name = "*" * 100  # 200 units once escaped
    reserve = details_reserve([name, "mentalmars"])
    batch = [
        _candidate(
            i,
            golden=i % 3 == 0,
            source_name=name if i % 2 else "Gearbox Blog",
            item_url="https://example.com/" + "a/" * 200,
        )
        for i in range(40)
    ]
    by_code = {c.code: c for c in batch}
    for ping, mention in ((True, ROLE), (True, "@everyone"), (False, None)):
        alerts = render_code_alerts(
            batch, ping=ping, ping_mention=mention, test=True, reserve=reserve
        )
        assert [c for a in alerts for c in a.codes] == [c.code for c in batch]
        for alert in alerts:
            details = {
                code: _worst_row(code, name, name, reward=FAMILY * 40 + "*" * 20)
                for code in alert.codes
            }
            edited = render_alert_edit(
                alert.header,
                [by_code[code] for code in alert.codes],
                golden_prefix=alert.golden_prefix,
                details=details,
                now=NOW,
            )
            assert discord_len(edited) <= 2000
            assert "<https://example.com/" in edited  # the worst case still had room for its links


@pytest.mark.parametrize("reward", ["x" * 300, "\U0001f600" * 150, "*" * 150, "@everyone" * 30])
def test_the_real_line_never_exceeds_its_slot(reward):
    names = ["mentalmars", "Borderlands (Bluesky)"]
    reserve = details_reserve(names)
    batch = [_candidate(i) for i in range(30)]
    details = {c.code: _worst_row(c.code, *names, reward=reward) for c in batch}
    alerts = render_code_alerts(
        batch, ping=True, ping_mention=ROLE, reserve=reserve, details=details, now=NOW
    )
    for alert in alerts:
        assert discord_len(alert.content) <= 2000
        line = details_line(details[alert.codes[0]], now=NOW)
        assert discord_len(line) + 1 <= reserve


def test_a_line_bigger_than_the_reserve_is_packed_at_its_real_size():
    batch = [_candidate(i) for i in range(6)]
    details = {c.code: _worst_row(c.code, "a", "b") for c in batch}
    alerts = render_code_alerts(batch, ping=False, reserve=0, details=details, now=NOW)
    assert len(alerts) > 1
    assert all(discord_len(a.content) <= 2000 for a in alerts)


def test_property_any_edit_with_worst_case_details_fits_the_message_it_was_packed_for():
    # Random names (markdown-heavy included), rewards, dates (an 11-digit timestamp among
    # them) and links: whatever the packing decided, the edit has to come out under 2,000.
    rng = random.Random(20261011)  # noqa: S311 (a seeded shuffle, not a secret)
    pieces = ["*", "_", "x", "\U0001f600", "@everyone", "<t:1:R>", "[", "](", " ", "Gearbox"]
    due_dates = [date(2026, 10, 9), date(2026, 12, 31), date(2100, 12, 31), date(5000, 12, 31)]
    for _ in range(40):
        names = ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 100)))[:100]] * 2
        names[1] = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 100)))[:100]
        reserve = details_reserve(names)
        batch = [
            _candidate(
                i,
                golden=rng.random() < 0.5,
                source_name=rng.choice(names),
                item_url="https://example.com/" + "a" * rng.randint(0, 300),
            )
            for i in range(rng.randint(1, 25))
        ]
        by_code = {c.code: c for c in batch}
        ping = rng.random() < 0.5
        alerts = render_code_alerts(batch, ping=ping, ping_mention=ROLE, reserve=reserve)
        for alert in alerts:
            details = {
                code: _row(
                    code,
                    reward="".join(rng.choice(pieces) for _ in range(rng.randint(0, 400))),
                    reward_from=names[0],
                    expires_on=rng.choice(due_dates),
                    expires_from=names[1],
                )
                for code in alert.codes
            }
            edited = render_alert_edit(
                alert.header,
                [by_code[code] for code in alert.codes],
                golden_prefix=alert.golden_prefix,
                details=details,
                now=NOW,
            )
            assert discord_len(edited) <= 2000


# --- no relative words in anything that stays in a channel ---

TOKEN_RE = re.compile(r"<t:(\d+):R>")
RELATIVE_WORDS_RE = re.compile(r"expires` in|tomorrow|`expired`|\bhours?\b|\bago\b", re.IGNORECASE)


def _dated_rows(codes, due):
    return {
        c: _row(c, reward="3 Golden Keys", reward_from="mentalmars", expires_on=due) for c in codes
    }


def test_alerts_edits_followups_and_roundups_carry_a_date_and_a_live_timestamp_only():
    # Oct 9 is the last day at NOW: the relative wording would be "in under an hour".
    batch = [_candidate(i) for i in range(2)]
    codes = [c.code for c in batch]
    details = _dated_rows(codes, date(2026, 10, 9))
    stamp = int(datetime(2026, 10, 10, 7, 0, tzinfo=UTC).timestamp())  # Pacific midnight
    expected = f"`reward` 3 Golden Keys · `expires` Oct 9, 2026 (<t:{stamp}:R>) · `via` mentalmars"

    [alert] = render_code_alerts(batch, ping=False, details=details, now=NOW)
    edited = render_alert_edit(alert.header, batch, golden_prefix=False, details=details, now=NOW)
    followup = render_followup_alert(codes, ping_mention="@everyone", details=details, now=NOW)
    [roundup] = render_roundup_alerts(
        [_candidate(i, roundup=True, item_url=POST) for i in range(2)], details=details, now=NOW
    )
    for content in (alert.content, edited, followup.content, roundup.content):
        assert content.count(expected) == 2
        assert not RELATIVE_WORDS_RE.search(content), content
        assert TOKEN_RE.findall(content) == [str(stamp)] * 2
    assert edited == alert.content


def test_the_edit_pass_text_does_not_move_with_the_clock():
    # Nothing in a posted message depends on `now`, so there is no reason to edit it later.
    c = _candidate()
    details = _dated_rows([c.code], date(2026, 10, 9))
    later = datetime(2026, 10, 20, tzinfo=UTC)
    [soon] = render_code_alerts([c], ping=False, details=details, now=NOW)
    [past] = render_code_alerts([c], ping=False, details=details, now=later)
    assert soon.content == past.content


def test_a_forged_timestamp_in_a_reward_stays_defused_beside_the_bots_own():
    c = _candidate()
    forged = "<t:1:R>"
    row = _row(
        c.code,
        reward=forged,
        reward_from=forged,
        expires_on=date(2026, 10, 14),
        expires_from=forged,
    )
    [alert] = render_code_alerts([c], ping=False, details={c.code: row}, now=NOW)
    assert TOKEN_RE.findall(alert.content) == ["1792047600"]
    assert "<\u200bt:1:R>" in alert.content


# --- shedding ---


def test_the_link_is_the_first_thing_shed():
    long_url = "https://example.com/" + "a" * 1700
    c = _candidate(item_url=long_url)
    [alert] = render_code_alerts([c], ping=True, reserve=700)
    assert "<https://" not in alert.content
    assert "Gearbox Blog" in alert.content
    assert discord_len(alert.content) <= 2000


def test_a_short_link_is_kept_when_it_fits_next_to_the_reserve():
    [alert] = render_code_alerts([_candidate()], ping=True, reserve=700)
    assert "<https://e.com/a0>" in alert.content


def test_a_name_that_cannot_fit_raises_instead_of_being_cut():
    c = _candidate(source_name="n" * 1990)
    with pytest.raises(ValueError, match="can't fit"):
        render_code_alerts([c], ping=True)


def test_a_reserve_too_big_for_any_message_raises():
    with pytest.raises(ValueError, match="can't fit"):
        render_code_alerts([_candidate()], ping=True, reserve=1990)


def test_a_hundred_character_name_is_never_shortened():
    name = "*" * 100
    reserve = details_reserve([name])
    [alert] = render_code_alerts([_candidate(source_name=name)], ping=True, reserve=reserve)
    assert esc(name) in alert.content
    assert "…" not in alert.content


# --- edits ---


def test_an_edit_with_unchanged_details_reproduces_the_original_content():
    batch = [_candidate(i, golden=i == 0) for i in range(4)]
    details = {
        c.code: _row(c.code, reward="3 Golden Keys", reward_from="mentalmars") for c in batch
    }
    [original] = render_code_alerts(batch, ping=True, ping_mention=ROLE, details=details, now=NOW)
    edited = render_alert_edit(
        original.header, batch, golden_prefix=original.golden_prefix, details=details, now=NOW
    )
    assert edited == original.content


def test_an_edit_keeps_the_stored_header_even_without_its_ping():
    c = _candidate()
    [original] = render_code_alerts([c], ping=True, ping_mention=ROLE)
    stripped = original.header.removeprefix(original.ping_prefix)
    edited = render_alert_edit(
        stripped,
        [c],
        golden_prefix=False,
        details={c.code: _row(c.code, reward="x", reward_from="m")},
        now=NOW,
    )
    assert edited.startswith(stripped + "\n\n")
    assert ROLE not in edited


def test_an_edit_adds_the_details_that_arrived_after_the_post():
    c = _candidate()
    [original] = render_code_alerts([c], ping=False, reserve=details_reserve(["mentalmars"]))
    row = _row(c.code, reward="3 Golden Keys", reward_from="mentalmars")
    edited = render_alert_edit(
        original.header, [c], golden_prefix=False, details={c.code: row}, now=NOW
    )
    assert "`reward` 3 Golden Keys · `via` mentalmars" in edited
    assert "3 Golden Keys" not in original.content


def test_an_edit_that_has_outgrown_its_message_drops_the_links():
    # The config gained a very long name after this was posted.
    batch = [_candidate(i, item_url="https://example.com/" + "a" * 120) for i in range(4)]
    row = {c.code: _worst_row(c.code, "n" * 100, "m" * 100, reward="x" * 100) for c in batch}
    header = terminal.prompt("new SHiFT codes")
    # ... with links it would be over the cap (the setup really does overflow):
    kept = [
        f"```\n{c.code}\n```\n{details_line(row[c.code], now=NOW)}\n"
        f"`src` Gearbox Blog · <{c.item_url}>"
        for c in batch
    ]
    assert discord_len("\n\n".join([header, *kept])) > 2000
    edited = render_alert_edit(header, batch, golden_prefix=False, details=row, now=NOW)
    assert discord_len(edited) <= 2000
    assert "<https://" not in edited


def test_an_edit_that_cannot_fit_even_without_links_raises():
    batch = [_candidate(i) for i in range(5)]
    row = {c.code: _worst_row(c.code, "n" * 100, "m" * 100) for c in batch}
    with pytest.raises(ValueError, match="exceeds"):
        render_alert_edit(
            terminal.prompt("new SHiFT codes"), batch, golden_prefix=False, details=row, now=NOW
        )


# --- roundups: real size, no reservation ---


def test_roundups_show_details_at_their_real_size_without_a_reserve():
    batch = [_candidate(i, roundup=True, item_url=POST) for i in range(3)]
    details = {batch[0].code: _row(batch[0].code, reward="3 Golden Keys", reward_from="mentalmars")}
    [alert] = render_roundup_alerts(batch, details=details, now=NOW)
    assert f"```\n{batch[0].code}\n```\n`reward` 3 Golden Keys · `via` mentalmars" in alert.content
    assert alert.content.count("`via`") == 1
    assert alert.ping is False
    assert alert.header.startswith("**SHiFT codes from a roundup**")


def test_a_big_roundup_is_packed_without_a_reserve():
    batch = [_candidate(i, roundup=True, item_url=POST) for i in range(50)]
    alerts = render_roundup_alerts(batch)
    assert len(alerts) == 2  # no reservation: nothing will ever edit these
    assert all(discord_len(a.content) <= 2000 for a in alerts)


def test_roundup_details_that_are_long_still_fit():
    batch = [_candidate(i, roundup=True, item_url=POST) for i in range(12)]
    details = {c.code: _worst_row(c.code, "a" * 100, "b" * 100) for c in batch}
    alerts = render_roundup_alerts(batch, details=details, now=NOW)
    assert [c for a in alerts for c in a.codes] == [c.code for c in batch]
    assert all(discord_len(a.content) <= 2000 for a in alerts)


# --- follow-ups ---


def test_followup_without_details_is_the_old_one_liner():
    codes = [_code(1), _code(2)]
    alert = render_followup_alert(codes, ping_mention="@everyone")
    assert alert.content == f"@everyone Confirmed by a second source: `{codes[0]}`, `{codes[1]}`"
    with_empty = render_followup_alert(
        codes, ping_mention="@everyone", details={codes[0]: _row(codes[0])}, now=NOW
    )
    assert with_empty.content == alert.content


def test_followup_with_details_is_one_line_per_code():
    codes = [_code(1), _code(2)]
    details = {codes[0]: _row(codes[0], reward="3 Golden Keys", reward_from="mentalmars")}
    alert = render_followup_alert(codes, ping_mention=ROLE, details=details, now=NOW)
    assert alert.content == (
        f"{ROLE} Confirmed by a second source:\n"
        f"`{codes[0]}` · `reward` 3 Golden Keys · `via` mentalmars\n"
        f"`{codes[1]}`"
    )
    assert alert.ping is True
    assert alert.ping_prefix == f"{ROLE} "


def test_followup_too_big_for_a_message_is_an_error():
    codes = [_code(i) for i in range(10)]
    details = {c: _worst_row(c, "a" * 100, "b" * 100) for c in codes}
    with pytest.raises(ValueError, match="fit_followup_codes"):
        render_followup_alert(codes, ping_mention="@everyone", details=details, now=NOW)


def test_fit_followup_codes_takes_every_code_when_there_are_no_details():
    codes = [_code(i) for i in range(30)]
    fit = fit_followup_codes(codes, ping_mention="@everyone", details=None, now=None)
    assert fit == codes[:FOLLOWUP_MAX_CODES]


def test_fit_followup_codes_stops_when_the_details_fill_the_message():
    codes = [_code(i) for i in range(FOLLOWUP_MAX_CODES)]
    details = {c: _worst_row(c, "a" * 100, "b" * 100) for c in codes}
    fit = fit_followup_codes(codes, ping_mention=ROLE, details=details, now=NOW)
    assert 0 < len(fit) < len(codes)
    assert fit == codes[: len(fit)]  # a prefix, in queue order
    assert (
        discord_len(render_followup_alert(fit, ping_mention=ROLE, details=details, now=NOW).content)
        <= 2000
    )
    # ... and it was the longest one: one more code would not have fit.
    more = codes[: len(fit) + 1]
    assert discord_len(_followup_content(more, f"{ROLE} ", details, NOW)) > 2000


def test_fit_followup_codes_counts_real_sizes_not_worst_cases():
    codes = [_code(i) for i in range(FOLLOWUP_MAX_CODES)]
    details = {c: _row(c, reward="3 Golden Keys", reward_from="mentalmars") for c in codes}
    fit = fit_followup_codes(codes, ping_mention="@everyone", details=details, now=NOW)
    assert fit == codes  # twenty short lines fit easily


def test_fit_followup_codes_with_nothing_to_fit_is_empty():
    assert fit_followup_codes([], ping_mention="@everyone", details=None, now=None) == []
