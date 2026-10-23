"""Adversarial round for the follow-up overflow fix: one claim, one ping, many messages.

Two halves. The first leans on `render_followup_alerts` alone: the packer is swept across
every size boundary a details line can reach (a message at exactly 2,000 units and one over,
20 codes against 2,000 units, a ping mention that eats most of the first message) and
checked against rules written down independently of the packer: every code once and in
order, nothing over the limit, nothing that could have gone in the previous message left
for the next one, and the ping in the first message only. The second half drives the real
delivery path with a real temp database and scripted posters: the ping cap, failures and
lost marks at every position in a burst, two passes racing, codes dying mid-flight, the
walk's deadline, and the real poster's allowed mentions.

Nothing here touches the network; the fakes are the delivery tests' own.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import random
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_shift_fanout_adversarial import (
    V3,
    FakeChannel,
    Harness,
    _code,
    add_guild,
    rows,
    seed,
    shift_row,
)
from test_shift_followups import (
    BLUESKY,
    FOLLOWUP_CONTINUED,
    FOLLOWUP_PROMPT,
    REDDIT,
    T0,
    burst,
    confirm,
    followup_alerts,
    script_posts,
    sighting_item,
    status_of,
)

from newsbot.bot import terminal
from newsbot.bot.format import (
    FOLLOWUP_MAX_CODES,
    RenderedAlert,
    discord_len,
    render_code_alerts,
    render_followup_alerts,
)
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import fanout
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.details import DetailObservation
from newsbot.shift.fanout import GuildOutcome, deliver_queued_codes
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import CodeDetailsRow

LIMIT = 2000
NOW = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)
EVERYONE = "@everyone"
ROLE = "<@&123456789012345678>"
HEADER_FIRST = f"{EVERYONE} {terminal.prompt(terminal.FOLLOWUP_HEADER)}"
HEADER_NEXT = terminal.prompt(terminal.FOLLOWUP_CONTINUATION)
# A mention Discord would act on; `esc()` leaves a zero-width space inside the ones it defuses.
LIVE_MENTION = re.compile(r"@everyone|@here|<@[!&]?\d")
EMOJI = "\U0001f600"  # two UTF-16 units, one code point


# --- the packer, against rules that don't share its code ---


def fcode(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def drow(code: str, reward: str) -> CodeDetailsRow:
    return CodeDetailsRow(
        code=code, clock_start=NOW, updated_at=NOW, reward=reward, reward_from="mentalmars"
    )


def details_for(codes: list[str], rewards: list[str]) -> dict[str, CodeDetailsRow]:
    return {c: drow(c, r) for c, r in zip(codes, rewards, strict=True) if r}


def entry_of(code: str, details) -> str:
    """One code's entry, got by rendering it alone and cutting off the header."""
    [alone] = render_followup_alerts([code], ping_mention=EVERYONE, details=details, now=NOW)
    return alone.content[len(HEADER_FIRST) + 2 :]


def check_packing(codes, details, mention=EVERYONE) -> list[RenderedAlert]:
    """Render and hold the result to the rules: the whole contract, restated from outside."""
    sent = render_followup_alerts(codes, ping_mention=mention, details=details, now=NOW)
    first_header = f"{mention} {terminal.prompt(terminal.FOLLOWUP_HEADER)}"
    assert [c for a in sent for c in a.codes] == codes, "every code once, in order"
    entries = {c: entry_of(c, details) for c in codes}
    for i, alert in enumerate(sent):
        header = first_header if i == 0 else HEADER_NEXT
        assert alert.content == "\n\n".join([header, *(entries[c] for c in alert.codes)])
        assert discord_len(alert.content) <= LIMIT
        assert 1 <= len(alert.codes) <= FOLLOWUP_MAX_CODES
        assert alert.ping is (i == 0)
        assert alert.ping_prefix == (f"{mention} " if i == 0 else "")
        assert alert.header == ""
        if i + 1 < len(sent):
            nxt = entries[sent[i + 1].codes[0]]
            full = len(alert.codes) == FOLLOWUP_MAX_CODES
            assert full or discord_len(alert.content) + 2 + discord_len(nxt) > LIMIT, (
                f"message {i + 1} left room for the next message's first code"
            )
    assert len({a.nonce for a in sent}) == len(sent)
    return sent


@pytest.mark.parametrize(
    ("count", "shape"),
    [
        (1, [1]),
        (19, [19]),
        (20, [20]),
        (21, [20, 1]),
        (39, [20, 19]),
        (40, [20, 20]),
        (41, [20, 20, 1]),
        (100, [20] * 5),
    ],
)
def test_plain_bursts_split_on_the_twenty_code_boundary(count, shape):
    codes = [fcode(i) for i in range(count)]
    sent = check_packing(codes, {})
    assert [len(a.codes) for a in sent] == shape


def test_rendering_leaves_the_callers_list_alone_and_hands_back_its_own_copies():
    codes = [fcode(i) for i in range(25)]
    before = list(codes)
    sent = render_followup_alerts(codes, ping_mention=EVERYONE)
    assert codes == before
    codes.clear()
    assert flatten(sent) == before


def test_nothing_to_say_is_no_messages():
    assert render_followup_alerts([], ping_mention=EVERYONE) == []


@pytest.mark.parametrize("lead", [0, 20])
def test_a_message_of_exactly_2000_units_stays_whole_and_one_unit_more_spills(lead):
    # lead=0: the boundary is in the pinged first message. lead=20: twenty plain codes fill
    # message 1 on the count, so the boundary is in a "(continued)" message, which has a
    # shorter header. The last code's reward is swept one unit at a time through the limit.
    plain = [fcode(i) for i in range(lead)]
    big = [fcode(100 + i) for i in range(5)]
    tuned = fcode(200)
    exact = over = None
    for k in range(1, 301):
        codes = [*plain, *big, tuned]
        details = details_for(codes, ["" for _ in plain] + ["y" * 300] * 5 + ["x" * k])
        sent = check_packing(codes, details)
        last = sent[-1]
        if tuned in last.codes and len(last.codes) == 6 and discord_len(last.content) == LIMIT:
            exact = k
        if exact is not None and k == exact + 1:
            over = sent
            break
    assert exact is not None, "the sweep never hit a message of exactly 2000 units"
    assert over is not None and over[-1].codes == [tuned], "one unit over must push the code out"
    assert len(over) == len(sent) and discord_len(over[-2].content) <= LIMIT


def test_a_long_ping_mention_leaves_the_first_message_shorter_than_the_rest():
    # 1,900 units of mention: the pinged message holds exactly one plain code, then the
    # continuation (whose header has no mention) takes the next twenty.
    mention = "@" + "r" * 1899
    codes = [fcode(i) for i in range(30)]
    sent = check_packing(codes, {}, mention=mention)
    assert [len(a.codes) for a in sent] == [1, 20, 9]
    assert all(mention not in a.content for a in sent[1:])


def test_a_mention_too_long_for_even_one_code_renders_nothing():
    mention = "@" + "r" * 1960
    assert render_followup_alerts([fcode(1)], ping_mention=mention) == []
    # Right at the edge it still holds one: 2000 units total.
    first_len = discord_len(f"{EVERYONE} {terminal.prompt(terminal.FOLLOWUP_HEADER)}")
    entry = discord_len(f"```\n{fcode(1)}\n```")
    spare = LIMIT - (first_len - len(EVERYONE)) - 2 - entry
    edge = "@" + "r" * (spare - 1)
    [only] = render_followup_alerts([fcode(1)], ping_mention=edge)
    assert discord_len(only.content) == LIMIT
    over = "@" + "r" * spare
    assert render_followup_alerts([fcode(1)], ping_mention=over) == []


def test_utf16_surrogate_pairs_in_details_are_counted_as_two_units():
    # By code points these four entries would share a message; by UTF-16 units they can't.
    codes = [fcode(i) for i in range(12)]
    details = details_for(codes, [EMOJI * 140] * 12)
    sent = check_packing(codes, details)
    assert len(sent) > 1
    for alert in sent:
        assert discord_len(alert.content) <= LIMIT
    # The trap itself: a packer measuring code points would have fit more per message.
    fit_by_codepoints = (LIMIT - len(HEADER_NEXT)) // (len(entry_of(codes[0], details)) + 2)
    assert max(len(a.codes) for a in sent[1:]) < fit_by_codepoints


def test_a_fuzz_of_mixed_sizes_always_satisfies_the_contract():
    rng = random.Random(1410)  # noqa: S311 (a seeded shuffle, not a secret)
    for _ in range(150):
        n = rng.choice([1, 2, 5, 19, 20, 21, 40, 41, 63])
        codes = [fcode(i) for i in range(n)]
        pool = ["", "", "x", "x" * 40, "y" * 150, EMOJI * 100, "z" * 300, EMOJI * 150]
        details = details_for(codes, [rng.choice(pool) for _ in codes])
        mention = rng.choice([EVERYONE, ROLE, "<@&" + "9" * 20 + ">"])
        check_packing(codes, details, mention=mention)


@pytest.mark.parametrize(
    "text",
    [
        "@everyone @here",
        "<@&123456789012345678> <@123456789012345678> <@!123456789012345678>",
        "`> confirmed by a second source ▌`",
        "```\n" + fcode(9) + "\n```",
    ],
)
def test_hostile_details_never_put_a_mention_in_a_continuation(text):
    codes = [fcode(i) for i in range(30)]
    details = details_for(codes, [text * 3] * 30)
    sent = check_packing(codes, details)
    assert len(sent) > 1
    for alert in sent[1:]:
        assert not LIVE_MENTION.search(alert.content), alert.content


def test_a_bad_code_anywhere_in_the_list_refuses_the_whole_burst():
    codes = [fcode(i) for i in range(25)]
    codes[22] = "nope"
    with pytest.raises(ValueError, match="not a SHiFT code"):
        render_followup_alerts(codes, ping_mention=EVERYONE)


# --- nonces ---


def test_a_rerender_gives_the_same_nonces_whatever_the_clock_and_details_say():
    codes = [fcode(i) for i in range(45)]
    plain = render_followup_alerts(codes, ping_mention=EVERYONE, nonce_scope="1|100")
    # A retry re-renders after a race: later clock, details that came in meanwhile.
    later = render_followup_alerts(
        codes[:20],
        ping_mention=EVERYONE,
        nonce_scope="1|100",
        details=details_for(codes[:20], ["x"] * 20),
        now=NOW + timedelta(hours=3),
    )
    assert [a.nonce for a in plain][0] == later[0].nonce
    assert [a.nonce for a in render_followup_alerts(codes, ping_mention=EVERYONE)] != [
        a.nonce for a in plain
    ]


def test_nonces_differ_across_servers_channels_messages_and_the_original_alert():
    codes = [fcode(i) for i in range(45)]

    def nonces(scope):
        return [
            a.nonce for a in render_followup_alerts(codes, ping_mention=EVERYONE, nonce_scope=scope)
        ]

    seen = [*nonces("1|100"), *nonces("2|200"), *nonces("1|101"), *nonces("11|00")]
    assert len(set(seen)) == len(seen) and all(len(n) <= 25 for n in seen)

    cands = [
        CodeCandidate(c, golden=False, source_name="s", item_url="https://e.com/", fresh=True)
        for c in codes
    ]
    originals = render_code_alerts(cands, ping=True, ping_mention=EVERYONE, nonce_scope="1|100")
    originals += render_code_alerts(cands[:3], ping=False, nonce_scope="1|100")
    assert not {a.nonce for a in originals} & set(nonces("1|100"))


def test_the_nonce_formula_pins_the_codes_and_the_index():
    codes = [fcode(i) for i in range(22)]
    first, second = render_followup_alerts(codes, ping_mention=EVERYONE, nonce_scope="7|70")
    want_1 = hashlib.sha256(f"7|70|followup|{'|'.join(codes[:20])}|0".encode()).hexdigest()[:25]
    want_2 = hashlib.sha256(f"7|70|followup|{'|'.join(codes[20:])}|1".encode()).hexdigest()[:25]
    assert (first.nonce, second.nonce) == (want_1, want_2)


# --- delivery ---


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
    harness = Harness(db_path, cfg, T0)
    seed(db_path)
    return harness


def queue_only(monkeypatch):
    """Make the next detection pass queue follow-ups without delivering them."""

    async def nothing(deps, guild_id):
        return GuildOutcome(guild_id)

    real = fanout._deliver_followups
    monkeypatch.setattr(fanout, "_deliver_followups", nothing)
    return lambda: monkeypatch.setattr(fanout, "_deliver_followups", real)


def flatten(alerts):
    return [c for a in alerts for c in a.codes]


@pytest.mark.parametrize("count", [100, 241])
async def test_a_huge_burst_is_many_messages_and_exactly_one_ping_unit(h, count):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, count)
    await confirm(h, codes)

    sent = followup_alerts(h, 1)
    assert flatten(sent) == codes and len(sent) == -(-count // FOLLOWUP_MAX_CODES)
    assert [m.ping for m in sent] == [True] + [False] * (len(sent) - 1)
    assert shift_row(h.db_path, 1).ping_count == 1
    assert set(status_of(h.db_path).values()) == {"posted"}


@pytest.mark.parametrize("spent", [0, 2])
async def test_the_last_unit_of_the_cap_is_enough_for_a_burst_of_any_size(h, spent):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 61)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_shift SET ping_day = '2026-10-01', ping_count = ?", (spent,))
    await confirm(h, codes)  # 2026-10-01 15:44 UTC, the same local day

    sent = followup_alerts(h, 1)
    assert flatten(sent) == codes and [a.ping for a in sent][0] is True
    assert shift_row(h.db_path, 1).ping_count == spent + 1
    assert set(status_of(h.db_path).values()) == {"posted"}


async def test_a_burst_that_took_the_last_unit_leaves_the_next_claim_capped_not_the_burst(h):
    add_guild(h.db_path, 1, ping="everyone", ping_day="2026-10-01", ping_count=2)
    codes = await burst(h, 45)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_shift SET ping_day = '2026-10-01', ping_count = 2")
    await confirm(h, codes)
    assert shift_row(h.db_path, 1).ping_count == 3 and flatten(followup_alerts(h, 1)) == codes

    # A second burst the same day finds the cap spent and is skipped whole (never partly sent).
    more = [_code(1000 + i) for i in range(30)]
    h.clock = T0 + timedelta(hours=21)
    await h.run([sighting_item(REDDIT, "community", c, at=h.clock) for c in more])
    h.clock += timedelta(hours=1)
    h.posters.clear()
    await h.run([sighting_item(BLUESKY, "community", c, at=h.clock) for c in more])
    assert followup_alerts(h, 1) == []
    skipped = [c for c, s in status_of(h.db_path).items() if s == "skipped"]
    assert sorted(skipped) == sorted(more)
    assert shift_row(h.db_path, 1).ping_count == 3


async def test_the_real_poster_pings_in_message_one_and_allows_no_mention_after_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    channel = FakeChannel(can_mention_everyone=True)
    h.channels = {1: channel}
    codes = await burst(h, 45, reward="@everyone @here <@&123456789012345678>")
    channel.sent.clear()
    await confirm(h, codes)

    assert len(channel.sent) >= 3
    (first, first_wire), *rest = channel.sent
    assert first.startswith(f"@everyone {FOLLOWUP_PROMPT}") and first_wire["parse"] == ["everyone"]
    for content, wire in rest:
        assert content.startswith(FOLLOWUP_CONTINUED)
        assert wire.get("parse", []) == [] and not wire.get("roles") and not wire.get("users")
        assert not LIVE_MENTION.search(content), content
    assert h.notices == []


async def test_a_bot_that_cannot_ping_says_so_once_however_many_messages_follow(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.channels = {1: FakeChannel(can_mention_everyone=False)}
    codes = await burst(h, 45)
    await confirm(h, codes)
    assert len(h.notices) == 1


async def test_the_first_message_failing_for_good_fails_every_message_unsent(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 50)
    made = script_posts(h, RuntimeError("the channel is gone"))
    await confirm(h, codes)

    [poster] = made
    assert len(poster.calls) == 1 and poster.sent == []
    assert set(status_of(h.db_path).values()) == {"failed"}
    assert h.notices == []
    assert shift_row(h.db_path, 1).ping_count == 1  # claimed before the send; failure keeps it


async def test_a_rate_limited_first_message_that_never_lands_never_starts_the_second(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 40)
    refused = [PublishError("429", rejected=True)] * 10
    made = script_posts(h, *refused)
    await confirm(h, codes)

    [poster] = made
    assert poster.sent == []
    assert {a.nonce for a in poster.calls} == {poster.calls[0].nonce}  # the same nonce each try
    assert all(a.codes == poster.calls[0].codes for a in poster.calls)
    assert all(a.ping for a in poster.calls)  # a refusal never costs the ping
    assert set(status_of(h.db_path).values()) == {"failed"}


async def test_the_last_message_failing_leaves_the_earlier_ones_posted(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 50)  # 20, 20, 10
    made = script_posts(h, None, None, RuntimeError("boom"))
    await confirm(h, codes)

    [poster] = made
    assert len(poster.calls) == 3 and len(poster.sent) == 2
    statuses = status_of(h.db_path)
    assert [statuses[c] for c in codes] == ["posted"] * 40 + ["failed"] * 10
    ids = rows(h.db_path, "SELECT code, message_id FROM guild_code_followups ORDER BY rowid")
    assert [i for _, i in ids] == [9001] * 20 + [9002] * 20 + [None] * 10


async def test_an_ambiguous_failure_on_a_continuation_retries_it_whole_and_touches_no_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    made = script_posts(h, None, PublishError("timeout"))
    await confirm(h, codes)

    [poster] = made
    first, second_try, second_retry, third = poster.calls
    assert first.ping is True and first.content.startswith(f"@everyone {FOLLOWUP_PROMPT}")
    assert second_try.ping is False and second_retry == second_try
    assert second_retry.content.startswith(FOLLOWUP_CONTINUED) and second_retry.ping_prefix == ""
    assert flatten(poster.sent) == codes
    assert set(status_of(h.db_path).values()) == {"posted"}


async def test_an_ambiguous_failure_on_message_one_strips_that_ping_and_not_the_next_messages(h):
    add_guild(h.db_path, 1, ping=ROLE.strip("<@&>"))
    codes = await burst(h, 45)
    made = script_posts(h, PublishError("502"), None, None)
    await confirm(h, codes)

    [poster] = made
    tried, retried, second, third = poster.calls
    assert tried.ping is True and retried.ping is False
    assert retried.content == tried.content[len(tried.ping_prefix) :]
    assert retried.nonce == tried.nonce and retried.codes == tried.codes
    assert second.content.startswith(FOLLOWUP_CONTINUED) and third.content.startswith(
        FOLLOWUP_CONTINUED
    )
    assert flatten(poster.sent) == codes


def fail_marks_for(monkeypatch, *, from_nth_call: int, only_posted: bool | None = None):
    """From the Nth mark on, every follow-up mark hits a busy database (through all retries)."""
    real = fanout._followup_mark_sync
    calls = {"n": 0}

    def flaky(db_path, guild_id, codes, message_id, posted):
        calls["n"] += 1
        wrong_kind = only_posted is not None and posted is not only_posted
        if calls["n"] >= from_nth_call and not wrong_kind:
            raise sqlite3.OperationalError("database is locked")
        real(db_path, guild_id, codes, message_id, posted)

    monkeypatch.setattr(fanout, "_followup_mark_sync", flaky)
    return calls


async def test_a_lost_mark_on_a_later_message_stops_the_burst_there_and_nothing_is_resent(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 50)  # 20, 20, 10
    fail_marks_for(monkeypatch, from_nth_call=2)
    await confirm(h, codes)

    # Message 1 posted and recorded, message 2 posted and could not be, message 3 never sent.
    assert [len(a.codes) for a in followup_alerts(h, 1)] == [20, 20]
    statuses = status_of(h.db_path)
    assert [statuses[c] for c in codes] == ["posted"] * 20 + ["pending"] * 30
    monkeypatch.undo()

    # No later pass re-sends it, and once the pending rows go stale they are failed, quietly.
    await h.deliver()
    assert followup_alerts(h, 1) == []
    h.clock += timedelta(minutes=11)
    await h.deliver()
    assert followup_alerts(h, 1) == [] and h.notices == []
    assert [status_of(h.db_path)[c] for c in codes] == ["posted"] * 20 + ["failed"] * 30


async def test_a_lost_mark_in_one_server_does_not_stop_the_next_servers_burst(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    codes = await burst(h, 25)
    real = fanout._followup_mark_sync

    def flaky(db_path, guild_id, codes, message_id, posted):
        if guild_id == 1:
            raise sqlite3.OperationalError("database is locked")
        real(db_path, guild_id, codes, message_id, posted)

    monkeypatch.setattr(fanout, "_followup_mark_sync", flaky)
    await confirm(h, codes)
    assert len(h.sent(1)) == 1  # the pinged message went out; the rest of the burst did not
    assert flatten(h.sent(2)) == codes and len(h.sent(2)) == 2
    assert {
        s
        for c, s in rows(
            h.db_path, "SELECT code, status FROM guild_code_followups WHERE guild_id = 2"
        )
    } == {"posted"}
    assert {
        s for (s,) in rows(h.db_path, "SELECT status FROM guild_code_followups WHERE guild_id = 1")
    } == {"pending"}


async def test_a_lost_failed_mark_also_stops_the_burst_and_leaves_rows_pending(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    script_posts(h, None, RuntimeError("boom"))
    fail_marks_for(monkeypatch, from_nth_call=1, only_posted=False)
    await confirm(h, codes)

    statuses = status_of(h.db_path)
    assert [statuses[c] for c in codes] == ["posted"] * 20 + ["pending"] * 25


async def test_the_delivery_walk_reports_a_lost_mark_as_that_servers_error(
    db_path, cfg, monkeypatch
):
    h = Harness(db_path, cfg, T0)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone")
    codes = await burst(h, 25)
    release = queue_only(monkeypatch)
    await confirm(h, codes)
    release()
    fail_marks_for(monkeypatch, from_nth_call=1)

    outcomes = await deliver_queued_codes(h.deps())
    [outcome] = outcomes
    assert outcome.guild_id == 1 and outcome.error is not None


async def test_two_passes_racing_for_one_burst_send_every_code_exactly_once(
    h, db_path, cfg, monkeypatch
):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 60)
    release = queue_only(monkeypatch)
    await confirm(h, codes)
    release()
    assert set(status_of(db_path).values()) == {"queued"}

    a = Harness(db_path, cfg, h.clock)
    b = Harness(db_path, cfg, h.clock)
    await asyncio.gather(
        deliver_queued_codes(a.deps()),
        deliver_queued_codes(b.deps()),
    )
    sent = [*a.sent(1), *b.sent(1)]
    assert sorted(flatten(sent)) == sorted(codes)  # none twice, none lost
    assert shift_row(db_path, 1).ping_count == 1
    assert sum(1 for m in sent if m.ping) == 1
    assert set(status_of(db_path).values()) == {"posted"}


async def test_a_pass_that_loses_part_of_the_race_repacks_without_a_hole(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    real = fanout.claim_guild_followups

    def racing(conn, guild_id, wanted, **kwargs):
        with conn:  # another pass took the first message's worth
            for code in wanted[:20]:
                conn.execute(
                    "UPDATE guild_code_followups SET status = 'posted' WHERE code = ?", (code,)
                )
        return real(conn, guild_id, wanted, **kwargs)

    monkeypatch.setattr(fanout, "claim_guild_followups", racing)
    await confirm(h, codes)

    sent = followup_alerts(h, 1)
    assert flatten(sent) == codes[20:] and sent[0].ping and not any(m.ping for m in sent[1:])
    assert all(m.ping_prefix == "" for m in sent[1:])
    assert sent[0].content.startswith(f"@everyone {FOLLOWUP_PROMPT}")
    assert shift_row(h.db_path, 1).ping_count == 1


async def test_a_race_that_leaves_one_code_makes_one_pinged_message(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 25)
    real = fanout.claim_guild_followups

    def racing(conn, guild_id, wanted, **kwargs):
        with conn:
            for code in wanted[:-1]:
                conn.execute(
                    "UPDATE guild_code_followups SET status = 'posted' WHERE code = ?", (code,)
                )
        return real(conn, guild_id, wanted, **kwargs)

    monkeypatch.setattr(fanout, "claim_guild_followups", racing)
    await confirm(h, codes)
    [only] = followup_alerts(h, 1)
    assert only.codes == codes[-1:] and only.ping is True


def expire(db_path, codes, on):
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(
            conn,
            [
                DetailObservation(
                    c,
                    ItemDetails(code=c, rank="table", expires_on=on, listed=True),
                    "mentalmars",
                    None,
                )
                for c in codes
            ],
            now=T0,
        )


async def test_codes_that_expired_while_queued_are_skipped_and_the_rest_repack(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    release = queue_only(monkeypatch)
    await confirm(h, codes)  # all 45 queued, none sent
    release()
    assert set(status_of(h.db_path).values()) == {"queued"}
    dead = codes[::3]  # every third code, spread over what would have been all three messages
    expire(h.db_path, dead, date(2026, 9, 20))

    await h.deliver()
    live = [c for c in codes if c not in dead]
    sent = followup_alerts(h, 1)
    assert flatten(sent) == live and [m.ping for m in sent][0] is True
    assert [len(m.codes) for m in sent] == [20, 10]
    statuses = status_of(h.db_path)
    assert {statuses[c] for c in dead} == {"skipped"} and {statuses[c] for c in live} == {"posted"}
    assert shift_row(h.db_path, 1).ping_count == 1


async def test_a_burst_that_died_while_queued_sends_nothing_and_spends_no_ping(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 25)
    release = queue_only(monkeypatch)
    await confirm(h, codes)
    release()
    expire(h.db_path, codes, date(2026, 9, 20))

    await h.deliver()
    assert h.sent(1) == [] and shift_row(h.db_path, 1).ping_count == 0
    assert set(status_of(h.db_path).values()) == {"skipped"}


async def test_the_server_turning_alerts_off_skips_a_whole_burst_without_a_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=False, channel_id=100, ping="everyone", now=lambda: T0)
    await confirm(h, codes)
    assert h.sent(1) == [] and shift_row(h.db_path, 1).ping_count == 0
    assert set(status_of(h.db_path).values()) == {"skipped"}


async def test_a_walk_past_its_deadline_finishes_the_server_it_is_in_and_leaves_the_next_queued(
    h, monkeypatch
):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    codes = await burst(h, 45)
    release = queue_only(monkeypatch)
    await confirm(h, codes)
    release()

    clock = {"t": 0.0}
    deps = replace(h.deps(), clock=lambda: clock["t"])
    base = deps.poster_for

    def factory(guild_id, channel_id, ping, notify):
        poster = base(guild_id, channel_id, ping, notify)
        real_post = poster.post

        async def post(alert):
            clock["t"] += 100.0  # the first send takes the whole budget
            return await real_post(alert)

        poster.post = post
        return poster

    deps = replace(deps, poster_for=factory)
    await deliver_queued_codes(deps, deadline=50.0)

    assert flatten(h.sent(1)) == codes and len(h.sent(1)) == 3
    assert h.sent(2) == []
    statuses = rows(h.db_path, "SELECT status FROM guild_code_followups WHERE guild_id = 2")
    assert {s for (s,) in statuses} == {"queued"}  # waiting for the next pass, not skipped
    await deliver_queued_codes(replace(deps, clock=lambda: 0.0), deadline=50.0)
    assert flatten(h.sent(2)) == codes
    assert shift_row(h.db_path, 2).ping_count == 1


async def test_the_outcome_and_the_log_count_the_codes_that_landed(h, caplog):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 50)
    script_posts(h, None, RuntimeError("boom"))
    caplog.clear()  # the burst's own original-alert line may have been captured too
    with caplog.at_level(logging.INFO, logger="newsbot.shift.fanout"):
        await confirm(h, codes)
    logged = [r for r in caplog.records if r.getMessage() == "SHiFT delivery for a guild"]
    assert [r.codes for r in logged] == [20] and logged[0].guild_id == 1


async def test_the_outcome_counts_every_code_of_a_full_burst(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 50)
    release = queue_only(monkeypatch)
    await confirm(h, codes)
    release()
    [outcome] = await h.deliver()
    assert outcome.followups_posted == 50 and outcome.error is None


async def test_two_servers_keep_separate_ping_budgets_and_separate_nonces(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    codes = await burst(h, 45)
    await confirm(h, codes)
    one, two = h.sent(1)[-3:], h.sent(2)[-3:]
    assert flatten(one) == flatten(two) == codes
    assert not {m.nonce for m in one} & {m.nonce for m in two}
    assert shift_row(h.db_path, 1).ping_count == 1 and shift_row(h.db_path, 2).ping_count == 1


async def test_a_second_burst_later_is_its_own_pinged_message_not_a_continuation(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 25)
    await confirm(h, codes)
    first_round = len(followup_alerts(h, 1))

    more = [_code(2000 + i) for i in range(3)]
    h.clock += timedelta(hours=1)
    await h.run([sighting_item(REDDIT, "community", c, at=h.clock) for c in more])
    h.clock += timedelta(hours=1)
    h.posters.clear()
    await h.run([sighting_item(BLUESKY, "community", c, at=h.clock) for c in more])

    [second] = followup_alerts(h, 1)
    assert first_round == 2 and second.ping is True and second.codes == more
    assert second.content.startswith(f"@everyone {FOLLOWUP_PROMPT}")
    assert shift_row(h.db_path, 1).ping_count == 2


async def test_messages_go_out_in_queue_order_even_when_confirmation_order_differs(h, monkeypatch):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    seen: list[list[str]] = []
    real = fanout.claim_guild_followups

    def spy(conn, guild_id, wanted, **kwargs):
        seen.append(list(repo.queued_guild_followups(conn, guild_id)))
        return real(conn, guild_id, wanted, **kwargs)

    monkeypatch.setattr(fanout, "claim_guild_followups", spy)
    shuffled = codes[:]
    random.Random(7).shuffle(shuffled)  # noqa: S311 (reproducible, not a secret)
    await confirm(h, shuffled)
    assert seen and flatten(followup_alerts(h, 1)) == seen[0]
    assert sorted(seen[0]) == sorted(codes)


async def test_a_details_heavy_burst_stays_inside_the_limit_through_the_real_pipeline(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 30, reward=EMOJI * 150)
    await confirm(h, codes)
    sent = followup_alerts(h, 1)
    assert flatten(sent) == codes and len(sent) > 3
    assert all(discord_len(m.content) <= LIMIT for m in sent)
    assert shift_row(h.db_path, 1).ping_count == 1


# --- the preview script ---


def _preview_script():
    path = Path(__file__).parent.parent / "scripts" / "preview_shift_look.py"
    spec = importlib.util.spec_from_file_location("preview_shift_look_overflow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_preview_script_still_builds_every_case_and_shows_a_continued_followup(tmp_path):
    script = _preview_script()
    cases = script.build_cases(script.NOW)
    titles = [title for title, _ in cases]
    assert [t.split(".")[0] for t in titles] == [str(n) for n in range(1, len(cases) + 1)]
    [title] = [t for t in titles if "follow-up that continues" in t]
    [(_, messages)] = [c for c in cases if c[0] == title]
    assert len(messages) >= 2
    assert messages[0].startswith(f"@everyone {FOLLOWUP_PROMPT}")
    assert all(m.startswith(FOLLOWUP_CONTINUED) and "@everyone" not in m for m in messages[1:])
    assert all(discord_len(m) <= LIMIT for m in messages)

    assert script.main(["--out", str(tmp_path)]) == 0
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "(continued)" in page and "follow-up that continues" in page


# --- qa round (2026-10-23) ---


async def test_a_race_rerender_that_raises_fails_the_claimed_codes_quietly(h, monkeypatch):
    # Unreachable with real data, which is why it's faked: the second render (after a
    # race shrank the claim) raises. The codes are claimed by then, so they must end up
    # `failed`, not stuck `pending`, and the server hears nothing, as for any follow-up.
    add_guild(h.db_path, 1, ping="everyone")
    codes = await burst(h, 45)
    real_claim = fanout.claim_guild_followups
    real_render = fanout.render_followup_alerts
    renders = 0

    def racing(conn, guild_id, wanted, **kwargs):
        with conn:
            for code in wanted[:20]:
                conn.execute(
                    "UPDATE guild_code_followups SET status = 'posted' WHERE code = ?", (code,)
                )
        return real_claim(conn, guild_id, wanted, **kwargs)

    def flaky_render(*args, **kwargs):
        nonlocal renders
        renders += 1
        if renders > 1:
            raise ValueError("an entry that fits nowhere")
        return real_render(*args, **kwargs)

    monkeypatch.setattr(fanout, "claim_guild_followups", racing)
    monkeypatch.setattr(fanout, "render_followup_alerts", flaky_render)
    await confirm(h, codes)

    assert followup_alerts(h, 1) == []
    statuses = status_of(h.db_path)
    assert [statuses[c] for c in codes[20:]] == ["failed"] * 25
    assert h.notices == []


async def test_a_burst_cancelled_mid_way_keeps_what_landed_and_recovery_fails_the_rest(
    db_path, cfg, monkeypatch
):
    # The collection hook's hard stop cancels a slow turn. Message 1 landed; message 2 was
    # in flight. Nothing may be re-sent, and startup recovery fails the rest without a word.
    h = Harness(db_path, cfg, T0)
    seed(db_path)
    add_guild(db_path, 1, ping="everyone")
    codes = await burst(h, 45)  # 20, 20, 5
    release = queue_only(monkeypatch)
    await confirm(h, codes)
    release()
    made = script_posts(h, None, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await deliver_queued_codes(h.deps())

    [poster] = made
    assert flatten(poster.sent) == codes[:20]
    statuses = status_of(db_path)
    assert [statuses[c] for c in codes] == ["posted"] * 20 + ["pending"] * 25

    h.posters.clear()
    await deliver_queued_codes(h.deps(), startup=True)
    statuses = status_of(db_path)
    assert [statuses[c] for c in codes] == ["posted"] * 20 + ["failed"] * 25
    assert followup_alerts(h, 1) == []
    assert h.notices == []
