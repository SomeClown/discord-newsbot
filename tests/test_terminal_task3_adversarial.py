"""Adversarial round on v3.2.0 Task 3: roundups and follow-ups in the terminal look.

The builder's own file (`test_format_terminal_roundups_followups.py`) pins the happy numbers.
This one leans on the seams around them, in the order a stranger would find them:

1. The literal chip strings, spelled out here and not read back from `terminal`, and a
   chip allow-list of this file's own, so a rename in `terminal.py` (or in `chip_check`,
   which now builds `ALLOWED_CHIPS` from those constants) can't quietly move the goalposts.
2. Roundup counts at the edges: 1, 10, 50, a claim that shrinks 7 to 3, two groups each
   spanning several messages.
3. Header shedding to the exact unit: the link survives at 2,000 and goes at 2,001, the name
   is cut only after the link is gone, and no cut ever leaves a lone surrogate.
4. Follow-ups at the boundary: the ping mention length where 20 codes stop fitting, an
   astral-character mention, 21 codes, no codes, a "code" that isn't one.
5. The queue: 20 codes with worst-case details drain over passes in order, none twice, none
   lost, under the server's current ping rules (budget, mention, ping turned off).
6. Hostile text through both renderers, fixed and random, judged by `chip_check`'s parser
   against the allow-list below.
7. Differentials against the v3.1.4 `format.py` (commit 1fcd04b, read out of git like
   `test_terminal_task1_adversarial.py` does; skipped with a reason if git or the commit
   isn't here): nonces, pings and code lists for the same inputs are the ones they always were.

No network, no sleeping, temp SQLite with the real migrations for the queue tests.
"""

from __future__ import annotations

import hashlib
import random
import re
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from chip_check import chips, fence_bodies
from test_shift_delivery_details import BLUESKY, REDDIT, numbered, sighting
from test_shift_delivery_details_adversarial import Rig, claim_elsewhere
from test_shift_fanout import NOW as FANOUT_NOW
from test_shift_fanout import V3, add_guild, item, rows, seed
from test_terminal_task1_adversarial import old  # noqa: F401  (a fixture, used by name)

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
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.details import DetailObservation
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow

NOW = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)
POST = "https://e.com/roundup"
ROLE20_ID = "1" * 20
ROLE20 = f"<@&{ROLE20_ID}>"

# Spelled out on purpose: if `terminal.py` changes a word, these fail, and that's the point.
ROUNDUP_CHIP = "`> SHiFT roundup ▌`"
CONTINUATION_CHIP = "`> SHiFT roundup (continued) ▌`"
FOLLOWUP_CHIP = "`> confirmed by a second source ▌`"
LITERAL_ALLOWED = frozenset(
    {
        "> SHiFT roundup ▌",
        "> SHiFT roundup (continued) ▌",
        "> confirmed by a second source ▌",
        "reward",
        "expires",
        "expired",
        "via",
        "src",
        "first seen",
    }
)
LIVE_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here")
CODE_SHAPE = re.compile(r"[0-9A-Z]{5}(-[0-9A-Z]{5}){4}")

HOSTILE = [
    "`> SHiFT roundup ▌`",
    "`> confirmed by a second source ▌` @everyone",
    "```\nEVIL-AAAAA-AAAAA-AAAAA-AAAAA\n```",
    "`reward` `via` `src`",
    "\\",
    "\\\\",
    "\\`",
    "\\`\\`",
    "x\\",
    "`" * 100,
    "｀fullwidth｀ ‵prime‵ ´acute´",
    "`​`",
    "@everyone @here <@123456789012345678> <@!123456789012345678> <@&123456789012345678>",
    "@‮everyone @​everyone",
    "<#123456789012345678> </cmd:123456789012345678>",
    "[x](http://evil.example) https://evil.example/x http://a.b discord.gg/abc",
    "_https://evil.example",
    "\U0001f600" * 60 + "`" + "\U0001f600" * 60,
    "*" * 100,
    "||spoiler|| ~~strike~~ __under__ *it* **bold** > quote",
]


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _cand(i: int, **kwargs) -> CodeCandidate:
    base = dict(
        code=_code(i),
        golden=False,
        source_name="Some Site",
        item_url=POST,
        fresh=True,
        roundup=True,
    )
    base.update(kwargs)
    return CodeCandidate(**base)


def _row(code: str, **kwargs) -> CodeDetailsRow:
    base = dict(code=code, clock_start=NOW, updated_at=NOW)
    base.update(kwargs)
    return CodeDetailsRow(**base)


def _worst(code: str, name_a: str, name_b: str, reward: str) -> CodeDetailsRow:
    return _row(
        code,
        reward=reward,
        reward_from=name_a,
        expires_on=date(5000, 12, 31),
        expires_from=name_b,
    )


def _url_of_header_len(target: int, count: int = 1, name: str = "Some Site") -> str | None:
    """A URL whose full roundup header is exactly `target` UTF-16 units, or None if none is."""
    for k in range(1, 3000):
        url = "https://example.com/" + "a" * k
        if discord_len(_roundup_header(name, url, count)) == target:
            return url
    return None


def _assert_clean(content: str) -> None:
    """The two rules, plus no live mention, judged by this file's own allow-list."""
    assert set(chips(content)) <= LITERAL_ALLOWED, content
    assert all(CODE_SHAPE.fullmatch(b) for b in fence_bodies(content)), content
    assert discord_len(content) <= 2000


# --- 1. the literal strings ---


def test_the_terminal_constants_are_the_words_in_the_contract():
    assert terminal.ROUNDUP_HEADER == "SHiFT roundup"
    assert terminal.ROUNDUP_CONTINUATION == "SHiFT roundup (continued)"
    assert terminal.FOLLOWUP_HEADER == "confirmed by a second source"
    assert terminal.prompt(terminal.ROUNDUP_HEADER) == ROUNDUP_CHIP
    assert terminal.prompt(terminal.ROUNDUP_CONTINUATION) == CONTINUATION_CHIP
    assert terminal.prompt(terminal.FOLLOWUP_HEADER) == FOLLOWUP_CHIP


def test_the_exact_bytes_of_a_roundup_a_continuation_and_a_followup():
    [one] = render_roundup_alerts([_cand(1)])
    assert one.content == (
        f"`> SHiFT roundup ▌` 1 code from Some Site · <{POST}>\n\n```\n{_code(1)}\n```"
    )
    big = render_roundup_alerts([_cand(i) for i in range(80)])
    assert big[1].content.startswith("`> SHiFT roundup (continued) ▌`\n\n```\n")
    f = render_followup_alert([_code(1)], ping_mention="@everyone")
    assert f.content == f"@everyone `> confirmed by a second source ▌`\n\n```\n{_code(1)}\n```"


def test_the_old_wording_is_gone_from_every_message():
    msgs = render_roundup_alerts([_cand(i) for i in range(80)])
    msgs.append(render_followup_alert([_code(1)], ping_mention="@everyone"))
    for m in msgs:
        for dead in ("from a roundup", "**", "Confirmed by", "(continued)**"):
            assert dead not in m.content
        assert m.content.count("`> ") == 1


# --- 2. roundup counts ---


@pytest.mark.parametrize(
    ("n", "words"),
    [(1, "1 code"), (2, "2 codes"), (10, "10 codes"), (11, "11 codes"), (50, "50 codes")],
)
def test_the_count_text_at_the_edges(n, words):
    first = render_roundup_alerts([_cand(i) for i in range(n)])[0]
    assert first.header.startswith(f"{ROUNDUP_CHIP} {words} from Some Site")
    assert not re.search(r"\b1 codes\b", first.header)


def test_the_header_function_says_zero_codes_in_the_plural():
    # Not reachable through the renderer (it returns nothing for no candidates), but the
    # function is the one place the plural rule lives.
    assert _roundup_header("S", POST, 0).startswith(f"{ROUNDUP_CHIP} 0 codes from S")


def test_two_groups_that_each_span_messages_count_and_continue_separately():
    a = [_cand(i, source_name="Site A", item_url="https://e.com/a") for i in range(40)]
    b = [_cand(i + 100, source_name="Site B", item_url="https://e.com/b") for i in range(33)]
    alerts = render_roundup_alerts(a + b, nonce_scope="1|100")
    heads = [x for x in alerts if x.header != CONTINUATION_CHIP]
    assert [h.header.split(" from ")[0] for h in heads] == [
        f"{ROUNDUP_CHIP} 40 codes",
        f"{ROUNDUP_CHIP} 33 codes",
    ]
    # Each group's messages are contiguous, and nothing from B sits under A's header.
    seen_b = False
    for x in alerts:
        is_b = x.codes[0] in {c.code for c in b}
        seen_b = seen_b or is_b
        assert not (seen_b and not is_b)
        assert all((c in {k.code for k in b}) == is_b for c in x.codes)
        assert x.ping is False and x.ping_prefix == ""
    assert [c for x in alerts for c in x.codes] == [c.code for c in a + b]
    assert len({x.nonce for x in alerts}) == len(alerts)


async def test_a_claim_that_shrinks_seven_codes_to_three_says_3_codes():
    # The rig is the only honest way to get a claim smaller than the ask.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "newsbot.db")
        with closing(connect(path)) as conn:
            migrate(conn)
        h = Rig(path, load_config(V3), FANOUT_NOW)
        seed(path)
        add_guild(path, 1, ping="everyone")
        codes = [numbered(n) for n in range(1, 8)]

        def another_pass_takes_four(guild_id):
            for k, code in enumerate(codes[:4]):
                claim_elsewhere(h, guild_id, code, message_id=7100 + k)

        h.on_begin = another_pass_takes_four
        await h.run([item(*codes, url="https://example.com/megathread")])
        [alert] = h.sent(1)
        assert alert.codes == codes[4:]
        assert alert.header.startswith(f"{ROUNDUP_CHIP} 3 codes from Gearbox Blog")
        assert alert.ping is False and alert.ping_prefix == ""


# --- 3. shedding, to the unit ---


def test_the_link_survives_at_exactly_the_room_there_is_and_goes_one_unit_over():
    # One code, no details: the block is 37 units, so the header gets 2000 - 37 - 2 = 1961.
    kept = _url_of_header_len(1961)
    assert kept is not None
    [alert] = render_roundup_alerts([_cand(1, item_url=kept)])
    assert f"<{kept}>" in alert.content and discord_len(alert.content) == 2000

    shed = _url_of_header_len(1962)
    assert shed is not None
    [alert] = render_roundup_alerts([_cand(1, item_url=shed)])
    assert "<http" not in alert.content
    assert alert.header == f"{ROUNDUP_CHIP} 1 code from Some Site"
    assert discord_len(alert.content) <= 2000


def test_the_room_is_set_by_the_largest_block_not_the_first():
    # Only the last code has a details line; the header must leave room for it.
    url = "https://example.com/" + "a" * 1800
    cands = [_cand(i, item_url=url) for i in range(3)]
    details = {cands[2].code: _worst(cands[2].code, "*" * 100, "_" * 100, "x" * 300)}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    for a in alerts:
        assert discord_len(a.content) <= 2000
        _assert_clean(a.content)
    assert [c for a in alerts for c in a.codes] == [c.code for c in cands]
    assert "<http" not in alerts[0].header  # a 1,800-unit link and a 600-unit block don't both fit


@pytest.mark.parametrize("hostile", range(len(HOSTILE)))
def test_a_2000_unit_url_and_a_100_char_hostile_name_shed_link_first(hostile):
    name = HOSTILE[hostile][:100]
    url = "https://example.com/" + "a" * 1990
    cands = [_cand(i, source_name=name, item_url=url) for i in range(4)]
    details = {c.code: _worst(c.code, name, name, "x" * 300) for c in cands}
    alerts = render_roundup_alerts(cands, details=details, now=NOW)
    assert alerts[0].header == f"{ROUNDUP_CHIP} 4 codes from {esc(name)}"
    for a in alerts:
        _assert_clean(a.content)
        assert a.ping is False
    assert [b for a in alerts for b in fence_bodies(a.content)] == [c.code for c in cands]


@pytest.mark.parametrize("hostile", range(len(HOSTILE)))
def test_name_truncation_only_starts_after_the_link_is_gone_and_never_breaks_a_character(hostile):
    name = HOSTILE[hostile][:100]
    url = "https://example.com/" + "a" * 300
    full = _roundup_header(name, url, 12)
    no_link = _roundup_header(name, url, 12, max_len=discord_len(full) - 1)
    assert "<http" not in no_link and discord_len(no_link) <= discord_len(full) - 1
    lead = f"{ROUNDUP_CHIP} 12 codes from "
    escaped = esc(name)
    previous = None
    for room in range(discord_len(lead), discord_len(no_link) + 1):
        h = _roundup_header(name, url, 12, max_len=room)
        h.encode("utf-8")  # a lone surrogate would raise here
        assert h.startswith(lead)
        assert discord_len(h) <= max(room, discord_len(lead) + 1)
        assert "<http" not in h
        shown = h[len(lead) :]
        if room == discord_len(no_link):
            assert h == no_link  # the whole name, exactly when it fits whole
        else:
            assert shown.endswith("…")
            assert escaped.startswith(shown.removesuffix("…"))
        if previous is not None:
            assert discord_len(h) >= discord_len(previous)  # more room never means less header
        previous = h


def test_a_header_given_no_room_at_all_still_keeps_the_chip_and_the_count():
    h = _roundup_header("Some Site", POST, 5, max_len=0)
    assert h == f"{ROUNDUP_CHIP} 5 codes from …"


def test_a_name_far_over_the_config_cap_is_cut_by_the_renderer_and_the_message_fits():
    name = "\U0001f600" * 1500  # 3,000 units escaped; the config would never allow it
    cands = [
        _cand(i, source_name=name, item_url="https://example.com/" + "a" * 2000) for i in range(2)
    ]
    alerts = render_roundup_alerts(cands)
    for a in alerts:
        assert discord_len(a.content) <= 2000
    first = alerts[0].header
    assert first.startswith(f"{ROUNDUP_CHIP} 2 codes from ") and first.endswith("…")
    assert "<http" not in first
    first.encode("utf-8")


# --- 3b. continuations ---


def test_a_continuation_never_pings_names_nothing_and_wears_only_its_own_chip():
    name = "@everyone <@&123456789012345678>"
    cands = [_cand(i, source_name=name) for i in range(150)]
    alerts = render_roundup_alerts(cands, nonce_scope="9|9")
    assert len(alerts) >= 3
    for a in alerts[1:]:
        assert a.header == CONTINUATION_CHIP
        assert a.content.startswith(CONTINUATION_CHIP + "\n\n```\n")
        assert a.ping is False and a.ping_prefix == ""
        assert not LIVE_MENTION.search(a.content)
        assert "everyone" not in a.content and "Some" not in a.content and " from " not in a.content
        assert chips(a.content) == ["> SHiFT roundup (continued) ▌"]
    for a in alerts:
        assert not LIVE_MENTION.search(a.content)


# --- 4. follow-ups at the boundary ---


def _mention_of_len(n: int) -> str:
    return "<@&" + "1" * (n - 4) + ">"


def test_twenty_codes_fit_up_to_one_unit_of_ping_and_nineteen_after():
    codes = [_code(i) for i in range(20)]
    # Find the longest mention that still fits 20 detail-less codes by rendering.
    lo = 24
    assert fit_followup_codes(codes, ping_mention=_mention_of_len(lo), details={}, now=NOW) == codes
    n = lo
    while (
        len(fit_followup_codes(codes, ping_mention=_mention_of_len(n + 1), details=None, now=None))
        == 20
    ):
        n += 1
    fits = render_followup_alert(codes, ping_mention=_mention_of_len(n))
    assert discord_len(fits.content) == 2000
    assert (
        fit_followup_codes(codes, ping_mention=_mention_of_len(n + 1), details=None, now=None)
        == codes[:19]
    )
    with pytest.raises(ValueError, match="exceeds 2000"):
        render_followup_alert(codes, ping_mention=_mention_of_len(n + 1))


def test_the_fit_counts_utf16_units_not_characters_in_the_mention():
    codes = [_code(i) for i in range(20)]
    plain = _mention_of_len(24)
    base = len(render_followup_alert(codes, ping_mention=plain).content)
    room = 2000 - base
    # An astral character is one codepoint and two units: pad with enough of them that the
    # codepoint count says "fits" and the unit count says "doesn't".
    k = room // 2 + 5
    mention = "<@&" + "\U0001f600" * (k + 10) + ">"
    assert len(render_followup_alert(codes[:1], ping_mention=mention).content) < 2000
    fit = fit_followup_codes(codes, ping_mention=mention, details={}, now=None)
    assert 0 < len(fit) < 20
    assert discord_len(render_followup_alert(fit, ping_mention=mention).content) <= 2000


def test_a_mention_so_long_that_not_one_code_fits_gives_an_empty_fit_and_a_raise():
    mention = "<@&" + "1" * 1990 + ">"
    assert fit_followup_codes([_code(1)], ping_mention=mention, details=None, now=None) == []
    with pytest.raises(ValueError, match="exceeds 2000"):
        render_followup_alert([_code(1)], ping_mention=mention)


def test_one_code_too_many_and_no_codes_are_refused():
    codes = [_code(i) for i in range(FOLLOWUP_MAX_CODES + 1)]
    assert fit_followup_codes(codes, ping_mention="@everyone", details=None, now=None) == codes[:20]
    with pytest.raises(ValueError, match="1 to 20"):
        render_followup_alert(codes, ping_mention="@everyone")
    assert fit_followup_codes([], ping_mention="@everyone", details=None, now=None) == []
    with pytest.raises(ValueError, match="1 to 20"):
        render_followup_alert([], ping_mention="@everyone")


@pytest.mark.parametrize(
    "bad",
    [
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA\n",
        "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA```\n@everyone",
        "```\n@everyone\n```",
        " AAAA1-AAAAA-AAAAA-AAAAA-AAAAA",
        "",
        "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
    ],
)
def test_nothing_that_is_not_a_code_ever_gets_into_a_fence(bad):
    with pytest.raises(ValueError, match="not a SHiFT code"):
        render_followup_alert([_code(1), bad], ping_mention="@everyone")
    with pytest.raises(ValueError, match="not a SHiFT code"):
        render_roundup_alerts([_cand(1), _cand(2, code=bad)])


def test_details_without_a_clock_is_refused_and_empty_details_need_none():
    codes = [_code(1)]
    assert "```" in render_followup_alert(codes, ping_mention="@everyone", details={}).content
    with pytest.raises(ValueError, match="clock"):
        render_followup_alert(
            codes, ping_mention="@everyone", details={codes[0]: _row(codes[0], reward="x")}
        )


def test_mixed_details_join_with_one_blank_line_and_no_stray_whitespace():
    codes = [_code(i) for i in range(3)]
    details = {
        codes[0]: _row(codes[0], reward="A", reward_from="mentalmars"),
        codes[2]: _row(codes[2], reward="C", reward_from="mentalmars"),
    }
    content = render_followup_alert(codes, ping_mention=ROLE20, details=details, now=NOW).content
    header, *entries = content.split("\n\n")
    assert header == f"{ROLE20} {FOLLOWUP_CHIP}"
    assert [e.count("\n") for e in entries] == [3, 2, 3]  # fence (3 newlines inside) + a line
    assert "\n\n\n" not in content and content == content.rstrip() and " \n" not in content
    assert entries[1] == f"```\n{codes[1]}\n```"


def test_a_followup_has_no_src_line_no_link_and_one_fence_per_code():
    codes = [_code(i) for i in range(5)]
    details = {c: _worst(c, "mentalmars", "Gearbox Blog", "3 Golden Keys") for c in codes}
    content = render_followup_alert(
        codes, ping_mention="@everyone", details=details, now=NOW
    ).content
    assert "src" not in chips(content)
    assert "<http" not in content and "://" not in content
    assert fence_bodies(content) == codes
    assert content.count("```") == 2 * len(codes)
    assert all(not line.startswith("`src`") for line in content.splitlines())


# --- 5. the queue, through the real delivery path ---


def _store_worst(db_path, code, name_a, name_b, now):
    """A reward from one source and an expiry from another, so the line carries both credits."""
    table = ItemDetails(code=code, rank="table", reward="x" * 300, listed=True)
    official = ItemDetails(code=code, rank="official", expires_on=date(2100, 12, 31))
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(
            conn,
            [DetailObservation(code, table, name_a), DetailObservation(code, official, name_b)],
            now=now,
        )


def _followups(rig):
    return [a for a in rig.sent(1) if FOLLOWUP_CHIP in a.content.split("\n", 1)[0]]


def _rig_with_budget(tmp_path, max_pings):
    cfg = load_config(V3)
    cfg = cfg.model_copy(
        update={"shift": cfg.shift.model_copy(update={"max_pings_per_day": max_pings})}
    )
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    rig = Rig(path, cfg, FANOUT_NOW)
    seed(path)
    return rig


async def _queue_twenty_worst(h, ping):
    add_guild(h.db_path, 1, ping=ping)
    codes = [numbered(n) for n in range(1, 21)]
    await h.run([sighting(REDDIT, "community", c, FANOUT_NOW) for c in codes])
    for code in codes:
        _store_worst(h.db_path, code, "*" * 100, "_" * 100, FANOUT_NOW)
    h.clock = FANOUT_NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])
    return codes


async def test_twenty_worst_case_codes_drain_in_order_under_the_role_ping(tmp_path):
    h = _rig_with_budget(tmp_path, 50)
    codes = await _queue_twenty_worst(h, ROLE20_ID)

    for _ in range(25):
        if len([c for a in _followups(h) for c in a.codes]) == 20:
            break
        h.clock += timedelta(minutes=10)
        await h.run([])
    sent = _followups(h)

    assert [c for a in sent for c in a.codes] == codes  # in order, none twice, none lost
    assert 3 <= len(sent) <= 10
    assert all(len(a.codes) < 20 for a in sent)  # worst-case lines can't all fit
    for a in sent:
        assert a.content.startswith(f"{ROLE20} {FOLLOWUP_CHIP}\n\n")
        assert a.ping is True and a.ping_prefix == f"{ROLE20} "
        assert a.header == ""
        _assert_clean(a.content)
        assert fence_bodies(a.content) == a.codes
        assert "src" not in chips(a.content) and "<http" not in a.content
        assert a.content.count(ROLE20) == 1
    assert len({a.nonce for a in sent}) == len(sent)
    statuses = dict(rows(h.db_path, "SELECT code, status FROM guild_code_followups"))
    assert set(statuses.values()) == {"posted"}


async def test_once_the_days_pings_are_spent_the_leftovers_are_skipped_and_never_reposted_unpinged(
    tmp_path,
):
    h = _rig_with_budget(tmp_path, 3)
    codes = await _queue_twenty_worst(h, "everyone")
    for _ in range(12):
        h.clock += timedelta(minutes=10)
        await h.run([])

    sent = _followups(h)
    assert len(sent) == 3  # the budget is three pings a day
    assert all(a.content.startswith(f"@everyone {FOLLOWUP_CHIP}\n\n") for a in sent)
    posted = [c for a in sent for c in a.codes]
    assert posted == codes[: len(posted)] and len(posted) < 20
    statuses = dict(rows(h.db_path, "SELECT code, status FROM guild_code_followups"))
    assert [c for c in codes if statuses[c] == "posted"] == posted
    assert [c for c in codes if statuses[c] == "skipped"] == codes[len(posted) :]
    # Nothing at all went out for them without the ping.
    everything = [c for a in h.sent(1) for c in a.codes if _unpinged_confirmation(a)]
    assert everything == []


def _unpinged_confirmation(alert):
    """A message that repeats a skipped code without being a pinged follow-up (should be none)."""
    return alert.ping is False and FOLLOWUP_CHIP in alert.content


async def test_the_leftovers_follow_the_servers_ping_choice_of_the_next_pass(tmp_path):
    h = _rig_with_budget(tmp_path, 50)
    codes = await _queue_twenty_worst(h, ROLE20_ID)
    [first] = _followups(h)
    assert first.content.startswith(f"{ROLE20} ")

    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="everyone")
    h.clock += timedelta(minutes=10)
    await h.run([])
    second = _followups(h)[1]
    assert second.content.startswith(f"@everyone {FOLLOWUP_CHIP}\n\n")
    assert ROLE20_ID not in second.content
    assert second.codes == codes[len(first.codes) : len(first.codes) + len(second.codes)]

    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="none")
    before = len(h.sent(1))
    h.clock += timedelta(minutes=10)
    await h.run([])
    assert len(h.sent(1)) == before  # ping off: no unpinged "confirmed" repost
    statuses = dict(rows(h.db_path, "SELECT code, status FROM guild_code_followups"))
    left = codes[len(first.codes) + len(second.codes) :]
    assert left and all(statuses[c] == "skipped" for c in left)


async def test_twenty_codes_without_details_go_out_as_one_message(tmp_path):
    h = _rig_with_budget(tmp_path, 3)
    add_guild(h.db_path, 1, ping=ROLE20_ID)
    codes = [numbered(n) for n in range(1, 21)]
    await h.run([sighting(REDDIT, "community", c, FANOUT_NOW) for c in codes])
    h.clock = FANOUT_NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])
    [only] = _followups(h)
    assert only.codes == codes
    assert discord_len(only.content) <= 2000
    assert fence_bodies(only.content) == codes
    assert "\n".join(only.content.splitlines()[:1]) == f"{ROLE20} {FOLLOWUP_CHIP}"


# --- 6. hostile text, fixed and random ---


@pytest.mark.parametrize("hostile", range(len(HOSTILE)))
def test_hostile_rewards_and_names_stay_out_of_every_chip_in_both_renderers(hostile):
    text = HOSTILE[hostile]
    cands = [_cand(i, source_name=text[:100]) for i in range(6)]
    details = {c.code: _worst(c.code, text[:100], text[:100], text) for c in cands}
    for a in render_roundup_alerts(cands, details=details, now=NOW):
        _assert_clean(a.content)
        assert not LIVE_MENTION.search(a.content)
        assert "://" not in a.content.replace(f"<{POST}>", "")  # no live scheme but the link
    codes = [c.code for c in cands]
    fit = fit_followup_codes(codes, ping_mention="@everyone", details=details, now=NOW)
    f = render_followup_alert(fit, ping_mention="@everyone", details=details, now=NOW)
    _assert_clean(f.content)
    assert fence_bodies(f.content) == fit
    assert len(LIVE_MENTION.findall(f.content)) == 1  # the ping, and only the ping
    assert f.content.startswith("@everyone ")


def test_a_reward_with_newlines_cannot_open_a_fence_or_a_chip():
    nasty = "line one\n```\nEVIL-AAAAA-AAAAA-AAAAA-AAAAA\n```\n`reward` @everyone\n> quote"
    codes = [_code(1), _code(2)]
    details = {c: _row(c, reward=nasty, reward_from="mentalmars") for c in codes}
    f = render_followup_alert(codes, ping_mention="@everyone", details=details, now=NOW).content
    assert fence_bodies(f) == codes
    assert set(chips(f)) <= LITERAL_ALLOWED
    assert len(LIVE_MENTION.findall(f)) == 1


def test_random_roundups_keep_every_invariant():
    rng = random.Random(3032)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(150):
        n = rng.randint(1, 60)
        name = rng.choice(HOSTILE)[: rng.randint(1, 100)] or "x"
        url = "https://example.com/" + "a" * rng.choice([0, 10, 500, 1900, 1990, 2100])
        cands = [_cand(i, source_name=name, item_url=url) for i in range(n)]
        details = {}
        for c in cands:
            if rng.random() < 0.4:
                text = rng.choice(HOSTILE)
                details[c.code] = _worst(c.code, text[:100], rng.choice(HOSTILE)[:100], text)
        alerts = render_roundup_alerts(cands, details=details, now=NOW, nonce_scope="1|2")
        assert [b for a in alerts for b in fence_bodies(a.content)] == [c.code for c in cands]
        assert alerts[0].header.startswith(f"{ROUNDUP_CHIP} {n} code{'' if n == 1 else 's'} from ")
        for i, a in enumerate(alerts):
            _assert_clean(a.content)
            assert not LIVE_MENTION.search(a.content)
            assert a.ping is False and a.ping_prefix == ""
            assert (a.header == CONTINUATION_CHIP) == (i > 0)
            want = hashlib.sha256(f"1|2|roundup|{'|'.join(a.codes)}|{i}".encode()).hexdigest()[:25]
            assert a.nonce == want


def test_random_followups_drain_completely_and_keep_every_invariant():
    rng = random.Random(3033)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(150):
        n = rng.randint(1, 40)
        codes = [_code(i) for i in range(n)]
        details = {}
        for c in codes:
            if rng.random() < 0.7:
                text = rng.choice(HOSTILE)
                details[c] = _worst(c, text[:100], rng.choice(HOSTILE)[:100], text)
        mention = rng.choice(["@everyone", ROLE20, "<@&" + "9" * rng.randint(17, 20) + ">"])
        left = list(codes)
        sent = []
        while left:
            fit = fit_followup_codes(left, ping_mention=mention, details=details, now=NOW)
            assert 1 <= len(fit) <= FOLLOWUP_MAX_CODES
            assert fit == left[: len(fit)]
            f = render_followup_alert(fit, ping_mention=mention, details=details, now=NOW)
            _assert_clean(f.content)
            assert fence_bodies(f.content) == fit
            assert f.content.startswith(f"{mention} {FOLLOWUP_CHIP}\n\n")
            sent += fit
            left = left[len(fit) :]
        assert sent == codes


# --- 7. differentials against v3.1.4 ---


def test_roundup_nonces_and_pings_match_v314_for_the_same_inputs(old):  # noqa: F811
    rng = random.Random(314)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(40):
        n = rng.randint(1, 4)  # small enough that both versions fit one message
        scope = rng.choice(["", "1|100", "424242|9"])
        name = rng.choice(["Some Site", "Gearbox Blog", "r/Borderlands4"])
        cands = [_cand(k, source_name=name) for k in rng.sample(range(10000), n)]
        was = old.render_roundup_alerts(cands, nonce_scope=scope)
        now = render_roundup_alerts(cands, nonce_scope=scope)
        assert len(was) == len(now) == 1
        assert (now[0].nonce, now[0].codes, now[0].ping, now[0].ping_prefix) == (
            was[0].nonce,
            was[0].codes,
            was[0].ping,
            was[0].ping_prefix,
        )


def test_roundup_nonces_follow_the_v314_formula_even_when_the_split_moves(old):  # noqa: F811
    cands = [_cand(i) for i in range(80)]
    now = render_roundup_alerts(cands, nonce_scope="7|8")
    for i, a in enumerate(now):
        want = hashlib.sha256(f"7|8|roundup|{'|'.join(a.codes)}|{i}".encode()).hexdigest()[:25]
        assert a.nonce == want
    # And the v3.1.4 renderer, fed the same codes, used the same formula, so the claim above
    # is about the real before and not about this file's memory of it.
    was = old.render_roundup_alerts(cands, nonce_scope="7|8")
    for i, a in enumerate(was):
        want = hashlib.sha256(f"7|8|roundup|{'|'.join(a.codes)}|{i}".encode()).hexdigest()[:25]
        assert a.nonce == want
    assert [c for a in was for c in a.codes] == [c for a in now for c in a.codes]


def test_followup_nonces_pings_and_codes_match_v314(old):  # noqa: F811
    rng = random.Random(1314)  # noqa: S311 (a reproducible sequence, not a secret)
    for _ in range(60):
        n = rng.randint(1, 20)
        codes = [_code(k) for k in rng.sample(range(10000), n)]
        scope = rng.choice(["", "1|100", "424242|9"])
        mention = rng.choice(["@everyone", ROLE20])
        was = old.render_followup_alert(codes, ping_mention=mention, nonce_scope=scope)
        now = render_followup_alert(codes, ping_mention=mention, nonce_scope=scope)
        assert (now.nonce, now.codes, now.ping, now.ping_prefix, now.header) == (
            was.nonce,
            was.codes,
            was.ping,
            was.ping_prefix,
            was.header,
        )


def test_v314_really_did_have_the_old_wording_so_the_differential_is_not_vacuous(old):  # noqa: F811
    was = old.render_followup_alert([_code(1)], ping_mention="@everyone")
    assert "Confirmed by a second source" in was.content
    assert (
        "Confirmed by a second source"
        not in render_followup_alert([_code(1)], ping_mention="@everyone").content
    )
