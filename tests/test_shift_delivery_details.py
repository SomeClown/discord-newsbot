"""Delivery with details (plan task 7; design.md §16).

What the fan-out does with a code's details once it has them: the first alert carries
the details line, every alert reserves room for a bigger one later, each posted alert
leaves a row in `guild_alert_messages` so the edit pass (task 8) can find it, and the
things that are never edited (roundups, follow-ups, anything the CLI preview prints)
leave none. A follow-up fits by its real size, and whatever doesn't fit waits.

Real temp SQLite with the real migrations, fake posters, an injected clock. No network.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_shift_fanout import (
    CODE_A,
    CODE_B,
    CODE_C,
    NOW,
    ROLE,
    V3,
    Harness,
    add_guild,
    item,
    rows,
    seed,
)

from newsbot.bot.format import details_reserve, discord_len
from newsbot.collectors.base import ItemDetails, RawItem
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.details import DetailObservation
from newsbot.shift.fanout import FanoutDeps, detect_and_fan_out, reserve_for_config
from newsbot.shift.sweep import PrintCodeAlertPoster
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import DETAILS_STAMP_FLOOR

GEARBOX = "Gearbox Blog"
MENTALMARS = "mentalmars"
REDDIT = "r/Borderlands4"
BLUESKY = 'Bluesky "Borderlands 4" search'
# Big enough that a pair of codes fills a message, so three codes split in two.
BIG_RESERVE = 700


class DetailsHarness(Harness):
    """The shared harness, with the reservation turned on."""

    reserve = 0

    def deps(self):
        return replace(super().deps(), details_reserve=self.reserve)


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
    harness = DetailsHarness(db_path, cfg, NOW)
    seed(db_path)
    return harness


def with_details(raw: RawItem, code: str, **fields) -> RawItem:
    """`raw` carrying official details for `code`; the reward is the usual one unless given."""
    fields.setdefault("reward", "3 Golden Keys")
    return replace(raw, details=ItemDetails(code=code, rank="official", **fields))


def store_details(db_path, code, *, reward="", name=MENTALMARS, now=NOW):
    """Put a table row's details for `code` straight into `code_details`."""
    obs = DetailObservation(
        code, ItemDetails(code=code, rank="table", reward=reward, listed=True), name, None
    )
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(conn, [obs], now=now)


def message_rows(db_path):
    return rows(
        db_path,
        "SELECT guild_id, message_id, channel_id, header, golden_prefix, posted_at, rendered_at "
        "FROM guild_alert_messages ORDER BY guild_id, message_id",
    )


# --- the first alert ---


async def test_the_first_alert_carries_the_details_line(h):
    add_guild(h.db_path, 1, ping="everyone")
    tomorrow = date(2026, 10, 2)  # NOW is 05:00 on Oct 1 in Pacific

    await h.run([with_details(item(CODE_A), CODE_A, expires_on=tomorrow)])

    (alert,) = h.sent(1)
    # Oct 2 ends at Pacific midnight, Oct 3 00:00 PDT = 07:00 UTC; no "tomorrow" in the post.
    stamp = int(datetime(2026, 10, 3, 7, 0, tzinfo=UTC).timestamp())
    assert (
        f"```\n{CODE_A}\n```\n`reward` 3 Golden Keys · `expires` Oct 2, 2026 (<t:{stamp}:R>)"
        f" · `via` {GEARBOX}\n"
    ) in alert.content
    assert "tomorrow" not in alert.content


async def test_a_code_without_details_posts_the_old_way(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run([item(CODE_A)])

    (alert,) = h.sent(1)
    assert alert.content == (
        f"@everyone `> new SHiFT code ▌`\n\n```\n{CODE_A}\n```\n"
        f"`src` {GEARBOX} · <https://example.com/AAAA>"
    )


THREE_ITEMS = [
    item(CODE_A, url="https://example.com/a"),
    item(CODE_B, url="https://example.com/b"),
    item(CODE_C, url="https://example.com/c"),
]


async def test_without_a_reserve_three_codes_share_a_message(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run(THREE_ITEMS)

    assert [a.codes for a in h.sent(1)] == [[CODE_A, CODE_B, CODE_C]]


async def test_the_reserve_splits_a_batch_that_would_otherwise_fit_one_message(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE

    await h.run(THREE_ITEMS)

    sent = h.sent(1)
    assert [a.codes for a in sent] == [[CODE_A, CODE_B], [CODE_C]]
    assert all(discord_len(a.content) <= 2000 for a in sent)


def test_the_reserve_comes_from_the_sources_that_can_supply_details(cfg):
    names = [
        s.name
        for game in cfg.catalog
        for s in game.sources
        if s.type == "shift_table" or s.trust == "official"
    ]
    assert names, "the fixture config should have at least one official source"
    assert reserve_for_config(cfg) == details_reserve(names)
    assert reserve_for_config(cfg) > details_reserve([])


def test_a_long_source_name_widens_the_reserve(cfg):
    base = reserve_for_config(cfg)
    long_name = "L" * 100
    game = cfg.catalog[0]
    source = next(s for s in game.sources if s.trust == "official")
    wider = cfg.model_copy(
        update={
            "catalog": [
                game.model_copy(
                    update={
                        "sources": [
                            *(s for s in game.sources if s is not source),
                            source.model_copy(update={"name": long_name}),
                        ]
                    }
                ),
                *cfg.catalog[1:],
            ]
        }
    )
    assert reserve_for_config(wider) >= base
    assert reserve_for_config(wider) >= details_reserve([long_name])


# --- the record of each posted message ---


async def test_each_posted_alert_leaves_a_row_for_the_edit_pass(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    h.reserve = BIG_RESERVE
    golden = item(CODE_A, title="Borderlands 4 Golden Key SHiFT code: " + CODE_A, url="https://e/a")
    plain_b = item(CODE_B, url="https://example.com/b")
    plain_c = item(CODE_C, url="https://example.com/c")

    await h.run([golden, plain_b, plain_c])

    got = message_rows(h.db_path)
    # Two servers, two messages each (a pair, then the leftover), mixed so golden entries
    # wear their prefix. Each poster numbers its own messages from 9001.
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in got] == [
        (1, 9001, 100, "@everyone `> new SHiFT codes ▌`", 1),
        (1, 9002, 100, "`> new SHiFT codes (continued) ▌`", 1),
        (2, 9001, 200, f"<@&{ROLE}> `> new SHiFT codes ▌`", 1),
        (2, 9002, 200, "`> new SHiFT codes (continued) ▌`", 1),
    ]
    for _guild, _message, _channel, _header, _golden, posted_at, rendered_at in got:
        assert datetime.fromisoformat(posted_at) == NOW
        # No code had details, so the version stamp is the floor, not a clock reading.
        assert datetime.fromisoformat(rendered_at) == DETAILS_STAMP_FLOOR
        assert posted_at.endswith("+00:00") and rendered_at.endswith("+00:00")


async def test_the_codes_of_a_message_come_back_in_the_order_they_were_packed(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE

    await h.run(
        [
            item(CODE_A, url="https://example.com/a"),
            item(CODE_B, url="https://example.com/b"),
            item(CODE_C, url="https://example.com/c"),
        ]
    )

    by_message = rows(
        h.db_path,
        "SELECT message_id, code FROM guild_code_posts WHERE guild_id = 1 AND from_roundup = 0 "
        "ORDER BY rowid",
    )
    assert by_message == [(9001, CODE_A), (9001, CODE_B), (9002, CODE_C)]
    assert [a.codes for a in h.sent(1)] == [[CODE_A, CODE_B], [CODE_C]]


async def test_rendered_at_is_the_newest_stamp_among_the_messages_codes(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE  # two to a message: A and B together, then C alone
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=NOW - timedelta(hours=2))
    store_details(h.db_path, CODE_B, reward="1 Golden Key", now=NOW - timedelta(hours=1))

    await h.run(
        [
            item(CODE_A, url="https://example.com/a"),
            item(CODE_B, url="https://example.com/b"),
            item(CODE_C, url="https://example.com/c"),
        ]
    )

    stamps = [datetime.fromisoformat(r[6]) for r in message_rows(h.db_path)]
    assert stamps == [NOW - timedelta(hours=1), DETAILS_STAMP_FLOOR]


async def test_rendered_at_predates_the_read_and_does_not_follow_the_clock(h):
    add_guild(h.db_path, 1, ping="everyone")
    changed = NOW - timedelta(days=3)
    store_details(h.db_path, CODE_A, reward="2 Golden Keys", now=changed)

    class SlowPoster:
        """Posting takes ten minutes of the injected clock."""

        sent: list = []

        async def post(self, alert):
            h.clock = NOW + timedelta(minutes=10)
            return 9001

    h.factory = lambda guild_id, channel_id, ping, notify: SlowPoster()
    await h.run([item(CODE_A)])

    ((*_, posted_at, rendered_at),) = message_rows(h.db_path)
    assert datetime.fromisoformat(rendered_at) == changed  # older than the read, by design
    assert datetime.fromisoformat(posted_at) == NOW + timedelta(minutes=10)


async def test_a_stripped_ping_stores_the_header_that_actually_posted(h):
    add_guild(h.db_path, 1, ping=ROLE)
    h.fail_with[1] = [PublishError("timeout")]

    await h.run([item(CODE_A)])

    ((_g, _m, _c, header, *_),) = message_rows(h.db_path)
    assert header == "`> new SHiFT code ▌`"


async def test_a_ping_kept_on_a_rejection_is_stored_with_the_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [PublishError("slow down", rejected=True, retry_after=0.0)]

    await h.run([item(CODE_A)])

    ((_g, _m, _c, header, *_),) = message_rows(h.db_path)
    assert header == "@everyone `> new SHiFT code ▌`"


async def test_a_failed_message_leaves_no_row(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [PublishError("down")] * 20

    await h.run([item(CODE_A)])

    assert message_rows(h.db_path) == []
    assert rows(h.db_path, "SELECT status FROM guild_code_posts") == [("failed",)]


async def test_a_roundup_leaves_no_row(h):
    add_guild(h.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]

    await h.run([item(*many, url="https://example.com/megathread")])

    assert h.sent(1)
    assert message_rows(h.db_path) == []
    assert rows(h.db_path, "SELECT DISTINCT message_id FROM guild_code_posts") == [(9001,)]


async def test_a_roundup_code_with_details_still_shows_them(h):
    add_guild(h.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]
    store_details(h.db_path, many[0], reward="A skin")

    await h.run([item(*many, url="https://example.com/megathread")])

    assert any("A skin" in a.content for a in h.sent(1))
    assert message_rows(h.db_path) == []


def test_marking_a_message_posted_needs_a_message_id_and_a_live_server(db_path):
    record = repo.AlertMessageRecord(
        channel_id=100, header="**New SHiFT code**", golden_prefix=False, posted_at=NOW,
        rendered_at=NOW,
    )  # fmt: skip
    add_guild(db_path, 1)
    with closing(connect(db_path)) as conn:
        # Nothing pending: nothing flips, nothing is recorded.
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=5, message=record)
        # No message id (the CLI's poster): the codes may flip, but there's nothing to edit.
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=None, message=record)
    assert message_rows(db_path) == []


# --- follow-ups ---


def sighting(source, trust, code, at):
    return RawItem(
        url=f"https://example.com/{source[:3]}/{code[:5]}",
        title="Borderlands 4 SHiFT code: " + code,
        excerpt="",
        source_name=source,
        trust=trust,
        published_at=at - timedelta(hours=1),
    )


def numbered(n):
    return f"{n:05d}-AAAAA-BBBBB-CCCCC-DDDDD"


async def test_a_followup_shows_details_and_writes_no_row(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([sighting(REDDIT, "community", CODE_A, NOW)])
    store_details(h.db_path, CODE_A, reward="3 Golden Keys")
    assert message_rows(h.db_path) != []  # the original alert, which is recorded
    before = message_rows(h.db_path)

    h.clock = NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", CODE_A, h.clock)])

    (followup,) = h.sent(1)
    assert followup.content == (
        "@everyone `> confirmed by a second source ▌`\n\n"
        f"```\n{CODE_A}\n```\n"
        f"`reward` 3 Golden Keys · `via` {MENTALMARS}"
    )
    assert message_rows(h.db_path) == before


async def test_a_followup_fits_by_its_real_size_and_the_rest_wait(h):
    add_guild(h.db_path, 1, ping="everyone")
    codes = [numbered(n) for n in range(1, 9)]
    await h.run([sighting(REDDIT, "community", c, NOW) for c in codes])
    reward = "x" * 300
    for code in codes:
        store_details(h.db_path, code, reward=reward)

    h.clock = NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", c, h.clock) for c in codes])

    (followup,) = h.sent(1)
    assert discord_len(followup.content) <= 2000
    assert followup.codes == codes[:5]
    assert rows(h.db_path, "SELECT code, status FROM guild_code_followups ORDER BY code") == [
        (c, "posted") for c in codes[:5]
    ] + [(c, "queued") for c in codes[5:]]
    assert len(message_rows(h.db_path)) == 1  # still only the original alert


async def test_followups_without_details_are_just_the_prompt_and_the_fence(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([sighting(REDDIT, "community", CODE_A, NOW)])

    h.clock = NOW + timedelta(hours=20)
    await h.run([sighting(BLUESKY, "community", CODE_A, h.clock)])

    (followup,) = h.sent(1)
    assert followup.content == (
        f"@everyone `> confirmed by a second source ▌`\n\n```\n{CODE_A}\n```"
    )


# --- the CLI preview ---


async def test_the_cli_preview_prints_details_and_records_nothing(h, capsys):
    add_guild(h.db_path, 1, ping="everyone")
    deps = FanoutDeps(
        cfg=h.cfg,
        db_path=h.db_path,
        now=lambda: NOW,
        poster_for=lambda _g, _c, _p, _n: PrintCodeAlertPoster(),
        notify_guild=h.notify_guild,
        preview=True,
        details_reserve=reserve_for_config(h.cfg),
    )

    released = await detect_and_fan_out(
        deps, [with_details(item(CODE_A), CODE_A, reward="3 Golden Keys")], seeding_ok=True
    )

    assert released == 1
    printed = capsys.readouterr().out
    assert CODE_A in printed and "3 Golden Keys" in printed
    assert message_rows(h.db_path) == []
    assert rows(h.db_path, "SELECT status FROM guild_code_posts") == [("queued",)]


# --- a record the table would refuse never undoes the posted mark ---


def _pending(db_path):
    add_guild(db_path, 1, ping="everyone")
    with closing(connect(db_path)) as conn:
        repo.record_released_codes(
            conn, [(CODE_A, "s", "https://x.test", False)], now=lambda: NOW, queue_for_games=[]
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")


@pytest.mark.parametrize(
    ("message_id", "channel_id"),
    [(0, 100), (-5, 100), (5, 0), (5, -1), (5, 2**63)],
)
def test_ids_the_table_refuses_skip_the_record_and_keep_the_posted_mark(
    db_path, message_id, channel_id
):
    _pending(db_path)
    record = repo.AlertMessageRecord(channel_id, "**New SHiFT code**", False, NOW, NOW)
    with closing(connect(db_path)) as conn:
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=message_id, message=record)
    assert rows(db_path, "SELECT status FROM guild_code_posts") == [("posted",)]
    assert message_rows(db_path) == []


def test_a_refusal_the_guard_did_not_foresee_rolls_back_the_row_alone(db_path, monkeypatch):
    """The last resort: the CHECK says no after the guard said yes, and only the row goes."""
    _pending(db_path)
    monkeypatch.setattr(repo, "_alert_record_problem", lambda *_a: None)
    record = repo.AlertMessageRecord(100, "**New SHiFT code**", False, NOW, NOW)
    with closing(connect(db_path)) as conn:
        repo.mark_guild_codes_posted(conn, 1, [CODE_A], message_id=0, message=record)
        assert not conn.in_transaction
    assert rows(db_path, "SELECT status, message_id FROM guild_code_posts") == [("posted", 0)]
    assert message_rows(db_path) == []


# --- the reserve covers names still stored on rows ---


async def test_a_name_stored_on_a_row_widens_the_reserve_at_delivery(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = reserve_for_config(h.cfg)
    long_name = "*" * 100  # escapes to 200 units
    codes = [numbered(n) for n in range(1, 13)]
    for code in codes:
        store_details(h.db_path, code, reward="r", name=long_name)
    items = [item(c, url=f"https://example.com/{c[:5]}") for c in codes]

    await h.run(items)

    from newsbot.bot.format import render_code_alerts
    from newsbot.shift.decide import CodeCandidate

    with closing(connect(h.db_path)) as conn:
        details = repo.code_details_for(conn, codes)
    candidates = [
        CodeCandidate(c, golden=False, source_name=GEARBOX, item_url=f"https://example.com/{c[:5]}",
                      fresh=True, trusted=True)
        for c in codes
    ]  # fmt: skip

    def partition(reserve):
        alerts = render_code_alerts(
            candidates, ping=True, ping_mention="@everyone", details=details, now=NOW,
            reserve=reserve,
        )  # fmt: skip
        return [a.codes for a in alerts]

    widened = partition(max(reserve_for_config(h.cfg), details_reserve([long_name])))
    assert partition(h.reserve) != widened, "the stored name should change the packing"
    assert [a.codes for a in h.sent(1)] == widened


# --- a queued code whose expiry has passed is skipped at delivery ---


def store_expiry(db_path, code, expires_on, *, category=None, rank="official"):
    """Give `code` an expiry after it was released. Official unless said otherwise: a table's
    expiry can't silence a code an official source named (the same rule as at release), an
    official one can."""
    obs = DetailObservation(
        code,
        ItemDetails(
            code=code, rank=rank, reward="A reward", category=category,
            expires_on=expires_on, listed=rank == "table",
        ),
        GEARBOX if rank == "official" else MENTALMARS,
        None,
    )  # fmt: skip
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(conn, [obs], now=NOW)


async def release_then_deliver(h, items):
    """Release (so the codes queue) without delivering, then deliver on a later call."""
    from newsbot.shift import fanout

    await fanout._release(h.deps(), items, seeding_ok=True)


async def deliver(h):
    from newsbot.shift.fanout import deliver_queued_codes

    h.posters.clear()
    return await deliver_queued_codes(h.deps())


def statuses(db_path):
    return dict(rows(db_path, "SELECT code, status FROM guild_code_posts"))


YESTERDAY = date(2026, 9, 30)  # NOW is Oct 1, 05:00 in Pacific


async def test_an_expired_queued_code_is_skipped_and_the_live_ones_go_out(h):
    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(
        h, [item(CODE_A, url="https://e/a"), item(CODE_B, url="https://e/b")]
    )
    store_expiry(h.db_path, CODE_A, YESTERDAY)

    (outcome,) = await deliver(h)

    (alert,) = h.sent(1)
    assert alert.codes == [CODE_B]
    assert CODE_A not in alert.content
    assert statuses(h.db_path) == {CODE_A: "skipped", CODE_B: "posted"}
    assert outcome.skipped == 1 and outcome.posted == 1
    assert shift_ping_count(h.db_path) == 1  # B's ping, spent once


async def test_a_batch_that_is_all_expired_creates_no_poster_and_spends_no_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(
        h, [item(CODE_A, url="https://e/a"), item(CODE_B, url="https://e/b")]
    )
    store_expiry(h.db_path, CODE_A, YESTERDAY)
    store_expiry(h.db_path, CODE_B, YESTERDAY)

    (outcome,) = await deliver(h)

    assert h.posters == {}
    assert statuses(h.db_path) == {CODE_A: "skipped", CODE_B: "skipped"}
    assert shift_ping_count(h.db_path) == 0
    assert outcome.skipped == 2 and outcome.posted == 0
    assert message_rows(h.db_path) == []


async def test_an_expiry_that_has_not_passed_yet_still_posts(h):
    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(h, [item(CODE_A)])
    store_expiry(h.db_path, CODE_A, date(2026, 10, 1))  # today, Pacific: good until midnight

    await deliver(h)

    assert statuses(h.db_path) == {CODE_A: "posted"}


async def test_a_permanent_code_with_a_stray_past_expiry_still_posts(h):
    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(h, [item(CODE_A)])
    store_expiry(h.db_path, CODE_A, YESTERDAY, category="permanent")

    await deliver(h)

    assert statuses(h.db_path) == {CODE_A: "posted"}


async def test_an_expired_roundup_code_is_left_alone(h):
    add_guild(h.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]
    await release_then_deliver(h, [item(*many, url="https://example.com/megathread")])
    store_expiry(h.db_path, many[0], YESTERDAY)

    await deliver(h)

    assert set(statuses(h.db_path).values()) == {"posted"}


async def test_the_preview_neither_prints_nor_skips_an_expired_code(h, capsys):
    from newsbot.shift.fanout import deliver_queued_codes

    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(
        h, [item(CODE_A, url="https://e/a"), item(CODE_B, url="https://e/b")]
    )
    store_expiry(h.db_path, CODE_A, YESTERDAY)
    capsys.readouterr()
    preview = replace(h.deps(), preview=True, poster_for=lambda *_a: PrintCodeAlertPoster())

    await deliver_queued_codes(preview)

    printed = capsys.readouterr().out
    assert CODE_B in printed and CODE_A not in printed
    assert statuses(h.db_path) == {CODE_A: "queued", CODE_B: "queued"}


def shift_ping_count(db_path):
    return rows(db_path, "SELECT ping_count FROM guild_shift WHERE guild_id = 1")[0][0]


# --- the preview prints roundup codes beside normal ones, and changes nothing ---


async def test_the_preview_prints_normal_and_roundup_codes_and_changes_nothing(h, capsys):
    from newsbot.shift.fanout import deliver_queued_codes

    add_guild(h.db_path, 1, ping="everyone")
    many = [
        f"{c}1-AAAAA-AAAAA-AAAAA-AAAAA" for c in ("QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV")
    ]
    await release_then_deliver(h, [item(CODE_A), item(*many, url="https://example.com/megathread")])
    capsys.readouterr()
    before = rows(h.db_path, "SELECT * FROM guild_code_posts ORDER BY rowid")
    preview = replace(h.deps(), preview=True, poster_for=lambda *_a: PrintCodeAlertPoster())

    (outcome,) = await deliver_queued_codes(preview)

    printed = capsys.readouterr().out
    assert CODE_A in printed and all(code in printed for code in many)
    assert outcome.posted == 1 and outcome.roundup_posted == len(many)
    assert rows(h.db_path, "SELECT * FROM guild_code_posts ORDER BY rowid") == before
    assert message_rows(h.db_path) == []
    assert shift_ping_count(h.db_path) == 0


async def test_a_tables_expiry_cannot_silence_a_trusted_code_at_delivery_either(h):
    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(h, [item(CODE_A)])  # official, so trusted
    store_expiry(h.db_path, CODE_A, YESTERDAY, rank="table")

    await deliver(h)

    assert statuses(h.db_path) == {CODE_A: "posted"}


async def test_a_tables_expiry_does_skip_a_community_code_at_delivery(h):
    add_guild(h.db_path, 1, ping="everyone")
    await release_then_deliver(h, [item(CODE_A, trust="community")])
    store_expiry(h.db_path, CODE_A, YESTERDAY, rank="table")

    await deliver(h)

    assert h.posters == {}
    assert statuses(h.db_path) == {CODE_A: "skipped"}
