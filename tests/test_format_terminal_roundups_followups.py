"""Roundups and follow-ups in the terminal look (v3.2.0, task 3).

A roundup reads `> SHiFT roundup ▌` 7 codes from Some Site · <link>, and a follow-up is the
ping, `> confirmed by a second source ▌`, and then every code alone in its fence with its
details line under it. This file pins the numbers (the count, the shedding order, how many
follow-up codes fit) and then leans on the two rules from outside with its own parser
(`chip_check`): chips hold only the fixed words, and a fence holds a code and nothing else.

The partial-claim and `_deliver_followups` tests borrow the rig from the delivery tests,
because a claim that comes back smaller than asked for is hard to fake any other way.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from chip_check import ALLOWED_CHIPS, chips, fence_bodies
from test_shift_delivery_details import BLUESKY, REDDIT, numbered, sighting, store_details
from test_shift_delivery_details_adversarial import MANY, Rig, claim_elsewhere
from test_shift_fanout import NOW as FANOUT_NOW
from test_shift_fanout import V3, add_guild, item, rows, seed

from newsbot.bot import terminal
from newsbot.bot.format import (
    FOLLOWUP_MAX_CODES,
    _roundup_header,
    discord_len,
    esc,
    fit_followup_codes,
    render_followup_alert,
    render_roundup_alerts,
)
from newsbot.config import load_config
from newsbot.shift.decide import CodeCandidate
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow

NOW = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)
ROLE20 = "<@&" + "1" * 20 + ">"
POST = "https://e.com/roundup"
ROUNDUP_CHIP = "`> SHiFT roundup ▌`"
CONTINUATION = "`> SHiFT roundup (continued) ▌`"
FOLLOWUP_CHIP = "`> confirmed by a second source ▌`"
CODE_SHAPE = re.compile(r"[0-9A-Z]{5}(-[0-9A-Z]{5}){4}")

# Text that tries to be a chip, a fence, a ping or a link. Every one of these goes in as a
# reward and a source name, and none of them may come out as anything but plain text.
HOSTILE = [
    "`> new SHiFT code ▌`",
    "```\nEVIL-AAAAA-AAAAA-AAAAA-AAAAA\n```",
    "`reward` @everyone",
    "\\`",
    "x`y`z",
    "@everyone @here <@&123456789012345678>",
    "[x](http://evil.example) https://evil.example/x",
    "`" * 100,
]


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _candidate(i: int, **kwargs) -> CodeCandidate:
    defaults = dict(
        code=_code(i),
        golden=False,
        source_name="Some Site",
        item_url=POST,
        fresh=True,
        roundup=True,
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
        expires_on=date(5000, 12, 31),
        expires_from=name_b,
    )


# --- the roundup header and its count ---


def test_a_roundup_of_one_says_code_and_of_several_says_codes():
    [one] = render_roundup_alerts([_candidate(1)])
    assert one.header == f"{ROUNDUP_CHIP} 1 code from Some Site · <{POST}>"
    [two] = render_roundup_alerts([_candidate(1), _candidate(2)])
    assert two.header == f"{ROUNDUP_CHIP} 2 codes from Some Site · <{POST}>"
    assert two.content.startswith(two.header + "\n\n")


def test_the_count_is_the_groups_size_and_not_the_first_messages():
    # 80 codes need several messages; the header names all 80, the continuations name none.
    alerts = render_roundup_alerts([_candidate(i) for i in range(80)])
    assert len(alerts) > 1
    assert alerts[0].header.startswith(f"{ROUNDUP_CHIP} 80 codes from Some Site")
    assert {a.header for a in alerts[1:]} == {CONTINUATION}
    assert sum(len(a.codes) for a in alerts) == 80


def test_each_group_counts_its_own_codes():
    a = [_candidate(i, source_name="Site A", item_url="https://e.com/a") for i in range(3)]
    b = [_candidate(i + 10, source_name="Site B", item_url="https://e.com/b") for i in range(1)]
    first, second = render_roundup_alerts(a + b)
    assert first.header.startswith(f"{ROUNDUP_CHIP} 3 codes from Site A")
    assert second.header.startswith(f"{ROUNDUP_CHIP} 1 code from Site B")


# The rig and its fixtures come from the delivery tests; `seed` and `add_guild` are the same
# ones the fan-out tests use. These three are the only fixtures this file needs.


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
    rig = Rig(db_path, cfg, FANOUT_NOW)
    seed(db_path)
    return rig


async def test_a_shrunken_claim_counts_the_codes_that_were_claimed(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.on_begin = lambda guild_id: claim_elsewhere(h, guild_id, MANY[0])

    await h.run([item(*MANY, url="https://example.com/megathread")])

    [alert] = h.sent(1)
    assert alert.codes == MANY[1:]
    assert alert.header.startswith(f"{ROUNDUP_CHIP} 5 codes from Gearbox Blog")


async def test_a_claim_down_to_one_code_says_code_in_the_singular(h):
    add_guild(h.db_path, 1, ping="everyone")

    def another_pass_takes_all_but_the_last(guild_id):
        for code in MANY[:-1]:
            claim_elsewhere(h, guild_id, code, message_id=7000 + MANY.index(code))

    h.on_begin = another_pass_takes_all_but_the_last

    await h.run([item(*MANY, url="https://example.com/megathread")])

    [alert] = h.sent(1)
    assert alert.codes == [MANY[-1]]
    assert alert.header.startswith(f"{ROUNDUP_CHIP} 1 code from Gearbox Blog")


# --- shedding ---


def test_the_link_goes_before_the_name_and_the_chip_and_count_never_go():
    name, url = "A Site Name", "https://example.com/" + "a" * 100
    whole = _roundup_header(name, url, 7)
    assert whole == f"{ROUNDUP_CHIP} 7 codes from {name} · <{url}>"

    without_link = f"{ROUNDUP_CHIP} 7 codes from {name}"
    assert _roundup_header(name, url, 7, max_len=discord_len(whole) - 1) == without_link
    assert _roundup_header(name, url, 7, max_len=discord_len(without_link)) == without_link

    squeezed = _roundup_header(name, url, 7, max_len=discord_len(without_link) - 1)
    assert squeezed.startswith(f"{ROUNDUP_CHIP} 7 codes from ")
    assert squeezed.endswith("…")
    assert discord_len(squeezed) <= discord_len(without_link) - 1


def test_a_header_with_no_room_for_the_name_keeps_the_chip_and_the_count():
    header = _roundup_header("A Site Name", POST, 12, max_len=10)
    assert header.startswith(f"{ROUNDUP_CHIP} 12 codes from ")


@pytest.mark.parametrize(
    "name",
    ["Short Name", "*" * 100, "\U0001f600" * 100, "@everyone" * 11, "`" * 100],
    ids=["short", "stars", "emoji", "mentions", "backticks"],
)
def test_a_2000_unit_url_sheds_first_and_the_name_stays_whole(name):
    url = "https://example.com/" + "a" * 1980
    assert discord_len(f"<{url}>") >= 2000
    cands = [_candidate(i, source_name=name, item_url=url) for i in range(3)]
    details = {c.code: _worst(c.code, "m" * 100, "n" * 100, "x" * 300) for c in cands}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    for alert in alerts:
        assert discord_len(alert.content) <= 2000
    first = alerts[0].header
    assert url not in first
    assert first == f"{ROUNDUP_CHIP} 3 codes from {esc(name)}"
    assert [c for a in alerts for c in a.codes] == [c.code for c in cands]


def test_a_name_that_still_does_not_fit_is_cut_with_an_ellipsis_and_the_message_fits():
    # The config caps a name at 100 characters, so only the header function can be made to
    # squeeze one; render_roundup_alerts gets the same squeeze from a long enough block.
    name = "\U0001f600" * 100
    header = _roundup_header(name, "https://example.com/" + "a" * 2000, 3, max_len=120)
    assert discord_len(header) <= 120
    assert header.startswith(f"{ROUNDUP_CHIP} 3 codes from ")
    assert header.endswith("…")
    assert "https://" not in header


# --- no ping, same nonces ---


def test_a_roundup_never_pings_and_its_continuations_say_so():
    alerts = render_roundup_alerts([_candidate(i) for i in range(80)], nonce_scope="1|100")
    assert len(alerts) > 1
    for alert in alerts:
        assert alert.ping is False
        assert alert.ping_prefix == ""
        assert "@everyone" not in alert.content and "<@" not in alert.content
    for alert in alerts[1:]:
        assert alert.content.startswith(CONTINUATION + "\n\n")
        assert alert.header == CONTINUATION


def test_the_roundup_and_followup_nonces_are_the_ones_they_always_were():
    cands = [_candidate(1), _candidate(2)]
    [roundup] = render_roundup_alerts(cands, nonce_scope="1|100")
    codes = [c.code for c in cands]
    want = hashlib.sha256(f"1|100|roundup|{'|'.join(codes)}|0".encode()).hexdigest()[:25]
    assert roundup.nonce == want

    follow = render_followup_alert(codes, ping_mention="@everyone", nonce_scope="1|100")
    want = hashlib.sha256(f"1|100|followup|{'|'.join(codes)}".encode()).hexdigest()[:25]
    assert follow.nonce == want
    assert follow.ping is True and follow.ping_prefix == "@everyone "


# --- follow-up shape and room ---


def test_a_followup_is_the_ping_the_prompt_and_a_fence_per_code():
    codes = [_code(1), _code(2)]
    alert = render_followup_alert(codes, ping_mention=ROLE20)
    assert alert.content == (
        f"{ROLE20} {FOLLOWUP_CHIP}\n\n```\n{codes[0]}\n```\n\n```\n{codes[1]}\n```"
    )
    assert alert.header == ""  # a follow-up is never edited, so it keeps no layout marker


@pytest.mark.parametrize("mention", ["@everyone", ROLE20])
def test_twenty_followup_codes_without_details_fit_in_one_message(mention):
    codes = [_code(i) for i in range(FOLLOWUP_MAX_CODES)]
    assert FOLLOWUP_MAX_CODES == 20
    assert fit_followup_codes(codes, ping_mention=mention, details={}, now=NOW) == codes
    alert = render_followup_alert(codes, ping_mention=mention)
    assert discord_len(alert.content) <= 2000
    assert alert.codes == codes


def test_worst_case_details_make_the_fit_shorter_and_the_message_still_fits():
    codes = [_code(i) for i in range(FOLLOWUP_MAX_CODES)]
    details = {c: _worst(c, "m" * 100, "n" * 100, "x" * 300) for c in codes}
    fit = fit_followup_codes(codes, ping_mention=ROLE20, details=details, now=NOW)
    assert 0 < len(fit) < len(codes)
    assert fit == codes[: len(fit)]
    alert = render_followup_alert(fit, ping_mention=ROLE20, details=details, now=NOW)
    assert discord_len(alert.content) <= 2000
    # One more code would have been over; that's what "fits" means.
    with pytest.raises(ValueError, match="exceeds 2000"):
        render_followup_alert(codes[: len(fit) + 1], ping_mention=ROLE20, details=details, now=NOW)


def followups_sent(h):
    """What the rig sent to server 1 that was a follow-up, not the originals before it."""
    return [a for a in h.sent(1) if FOLLOWUP_CHIP in a.content.split("\n", 1)[0]]


async def test_the_codes_that_do_not_fit_stay_queued_and_go_out_next_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = [numbered(n) for n in range(1, 9)]
    await h.run([sighting(REDDIT, "community", c, FANOUT_NOW) for c in codes])
    for code in codes:
        store_details(h.db_path, code, reward="x" * 300, now=FANOUT_NOW)
    h.clock = FANOUT_NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])

    with closing(connect(h.db_path)) as conn:
        details = repo.code_details_for(conn, codes)
    expected = fit_followup_codes(codes, ping_mention="@everyone", details=details, now=h.clock)
    (first,) = followups_sent(h)
    assert first.codes == expected
    assert 0 < len(expected) < len(codes)
    assert discord_len(first.content) <= 2000
    statuses = dict(rows(h.db_path, "SELECT code, status FROM guild_code_followups"))
    assert [c for c in codes if statuses[c] == "queued"] == codes[len(expected) :]
    assert [c for c in codes if statuses[c] == "posted"] == expected

    h.clock += timedelta(hours=1)
    await h.run([])

    sent = followups_sent(h)
    assert [c for a in sent for c in a.codes] == codes  # in order, none twice, none lost
    assert all(discord_len(a.content) <= 2000 for a in sent)
    assert all(a.content.startswith(f"@everyone {FOLLOWUP_CHIP}\n\n") for a in sent)


# --- the two rules, checked from the outside ---


@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_hostile_text_never_lands_in_a_chip_or_a_fence_in_a_roundup(text):
    cands = [_candidate(i, source_name=text) for i in range(4)]
    details = {c.code: _worst(c.code, text[:100], text[:100], text) for c in cands}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    sent_codes = [c.code for c in cands]
    for alert in alerts:
        assert set(chips(alert.content)) <= ALLOWED_CHIPS, alert.content
        assert all(CODE_SHAPE.fullmatch(body) for body in fence_bodies(alert.content))
        assert "@everyone" not in alert.content and "@here" not in alert.content
    assert [b for a in alerts for b in fence_bodies(a.content)] == sent_codes


@pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_hostile_text_never_lands_in_a_chip_or_a_fence_in_a_followup(text):
    codes = [_code(i) for i in range(3)]
    details = {c: _worst(c, text[:100], text[:100], text) for c in codes}
    fit = fit_followup_codes(codes, ping_mention="@everyone", details=details, now=NOW)
    alert = render_followup_alert(fit, ping_mention="@everyone", details=details, now=NOW)
    assert set(chips(alert.content)) <= ALLOWED_CHIPS, alert.content
    assert fence_bodies(alert.content) == fit
    # The one ping is the one the server asked for.
    assert alert.content.count("@everyone") == 1 and "@here" not in alert.content
    assert alert.content.startswith(f"@everyone {FOLLOWUP_CHIP}")


def test_the_new_chips_are_in_the_allowed_set():
    assert ROUNDUP_CHIP.strip("`") in ALLOWED_CHIPS
    assert CONTINUATION.strip("`") in ALLOWED_CHIPS
    assert FOLLOWUP_CHIP.strip("`") in ALLOWED_CHIPS
    assert terminal.is_terminal_header(CONTINUATION)
    assert terminal.is_terminal_header(f"{ROUNDUP_CHIP} 1 code from Some Site")
