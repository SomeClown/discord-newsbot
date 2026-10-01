"""Adversarial tests for the "confirmed by a second source" follow-ups (D14, B+).

A follow-up is the one message the bot sends because somebody else agreed with it, and it
carries an `@everyone`, so every edge here is a way of annoying strangers or of staying
quiet when we promised not to. The existing file replays the real `39FJ3` timeline and pins
the happy edges; this one goes looking for the sad ones:

- what counts as "the same code" (case, spacing) and "a second source" (clock skew, order);
- confirmations that arrive before the thing they confirm is finished (the original still
  `queued`, or a crash between the sighting and the release);
- two kinds of message to one server in one turn, and the ping budget they fight over;
- a server that disappears, a database that rolls back and forward under it, and a
  migration that dies halfway.

Real temp databases, the fan-out's scripted fakes, no network. A few of these pin behavior
that's easy to read as a bug (a follow-up that waits for the code's next sighting); they're
marked as pins, and they're in the report so the owner can say "no, I wanted it the other way".
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from test_shift_fanout_adversarial import (
    V3,
    Harness,
    _with_shift,
    add_guild,
    rows,
    seed,
    shift_row,
)
from test_shift_followups import (
    BLUESKY,
    CODE,
    REDDIT,
    T0,
    at,
    first_sighting,
    followup_rows,
    later,
    sighting_item,
)

from newsbot.bot.format import render_code_alerts, render_followup_alert
from newsbot.config import load_config
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.fanout import _release, deliver_queued_codes
from newsbot.store import repo
from newsbot.store.db import connect, migrate

OFFICIAL = "Gearbox Blog"
CODE_X = "XXXX1-AAAAA-BBBBB-CCCCC-DDDDD"
CODE_Y = "YYYY2-AAAAA-BBBBB-CCCCC-DDDDD"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def cfg():
    return load_config(V3)


class Rig(Harness):
    """The fan-out harness, but remembering every poster a pass made.

    A turn that sends an original and a follow-up builds two posters for one server, and the
    stock harness keeps only the last, which would make the first message look like it never
    went out.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.made: list[tuple[int, object]] = []

    def factory(self, guild_id, channel_id, ping, notify):
        poster = super().factory(guild_id, channel_id, ping, notify)
        self.made.append((guild_id, poster))
        return poster

    async def deliver(self, *, startup=False):
        self.made.clear()
        return await super().deliver(startup=startup)

    async def run(self, items, *, seeding_ok=True):
        self.made.clear()
        return await super().run(items, seeding_ok=seeding_ok)

    def every(self, guild_id):
        """All the messages this server got in the last pass, in the order they went out."""
        return [a for g, poster in self.made if g == guild_id for a in poster.sent]


@pytest.fixture
def h(db_path, cfg):
    seed(db_path)
    return Rig(db_path, cfg, T0)


def sightings(db_path):
    return rows(
        db_path,
        "SELECT code, source_name, trusted, roundup FROM code_sightings ORDER BY source_name",
    )


# --- what counts as the same code, and as a second source ---


async def test_a_lowercase_second_sighting_is_the_same_code_and_confirms_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=3), source=BLUESKY, trust="community")  # uppercase already
    assert len(h.sent(1)) == 1
    # Now the lowercase spelling of the same code from a third source: still one row per source,
    # keyed by the uppercase code, and no second follow-up.
    h.clock = T0 + timedelta(hours=4)
    shouting = CODE.lower()
    await h.run([sighting_item("Third Source", "community", shouting, at=h.clock)])
    assert {row[0] for row in sightings(h.db_path)} == {CODE}
    assert h.sent(1) == [] and followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_lowercase_first_sighting_and_an_uppercase_second_still_confirm(h):
    add_guild(h.db_path, 1, ping="everyone")
    await at(h, timedelta(0)).run([sighting_item(REDDIT, "community", CODE.lower(), at=T0)])
    assert h.sent(1)[0].content.count(CODE) == 1  # posted in the one canonical spelling
    await later(h, timedelta(hours=5))
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_spaced_out_spelling_is_not_a_code_so_it_is_not_a_confirmation(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    spaced = " ".join(CODE.split("-"))  # the groups space-separated instead of hyphenated
    await at(h, timedelta(hours=5)).run([sighting_item(BLUESKY, "community", spaced, at=h.clock)])
    assert h.sent(1) == [] and followup_rows(h.db_path) == []
    assert [row[1] for row in sightings(h.db_path)] == [REDDIT]


async def test_a_sighting_whose_clock_ran_behind_still_counts_inside_the_window(h):
    """A pass that ran with the clock stepped back (so `seen_at` is before the first sighting)
    is still a sighting inside 'within 24 hours of the first'."""
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, -timedelta(hours=1))  # an hour before the code was first seen
    assert len(h.sent(1)) == 1 and h.sent(1)[0].ping is True
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_sighting_whose_clock_ran_far_ahead_is_outside_the_window(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    await later(h, timedelta(hours=24, seconds=1))
    assert followup_rows(h.db_path) == [] and h.sent(1) == []


async def test_source_names_that_differ_only_in_case_are_one_source(h):
    """Independence is by name without regard to case: 'r/borderlands4' and 'r/Borderlands4'
    are the same feed spelled twice, so they are not a second opinion."""
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h, source="r/Borderlands4")
    await later(h, timedelta(hours=2), source="r/borderlands4")
    assert followup_rows(h.db_path) == [] and h.sent(1) == []
    await later(h, timedelta(hours=3), source=BLUESKY)  # a genuinely different source confirms
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_stale_second_item_still_confirms_when_it_names_a_fresh_code(h):
    """Pinned: the second source's item may be old (a three-day-old article); we only ask that
    we *see* it inside the 24 hours. The first sighting is what had to be fresh."""
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    old = sighting_item(BLUESKY, "community", CODE, at=T0 + timedelta(hours=3))
    old = old.__class__(
        url=old.url,
        title=old.title,
        excerpt="",
        source_name=BLUESKY,
        trust="community",
        published_at=T0 - timedelta(days=3),
    )
    await at(h, timedelta(hours=3)).run([old])
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


# --- confirmations that beat the thing they confirm ---


async def test_a_sighting_recorded_before_its_codes_own_row_confirms_on_the_next_sighting(h):
    """The crash window: a pass wrote its sightings and died before the release. The code is
    released by the next pass; the earlier sighting (a different source) counts toward the
    window, but nothing re-asks until the code is seen again. (Pinned: the follow-up is late,
    not lost.)"""
    add_guild(h.db_path, 1, ping="everyone")
    with closing(connect(h.db_path)) as conn:
        repo.record_code_sightings(
            conn, [(CODE, BLUESKY, False, False)], now=lambda: T0 - timedelta(hours=1)
        )
    await first_sighting(h)  # r/Borderlands4, next pass: released, posted unpinged
    assert h.sent(1)[0].ping is False and followup_rows(h.db_path) == []
    await later(h, timedelta(hours=1), source=REDDIT)  # the feeds still show it an hour on
    assert [a.ping for a in h.sent(1)] == [True]
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_confirmation_while_the_original_is_still_queued_gets_no_followup_that_pass(h):
    """Pinned (the implementer's stated rule): the original is `queued`, not `posted`, so nothing
    is queued for it; the original then posts unpinged. The next pass that sees the code again
    sends the follow-up."""
    add_guild(h.db_path, 1, ping="everyone")
    await _release(
        h.deps(), [sighting_item(REDDIT, "community", CODE, at=T0)], seeding_ok=True
    )  # queued, delivery never ran
    h.clock = T0 + timedelta(hours=2)
    await h.run([sighting_item(BLUESKY, "community", CODE, at=h.clock)])
    assert [a.ping for a in h.sent(1)] == [False]  # the original, now, without a ping
    assert followup_rows(h.db_path) == []
    await later(h, timedelta(hours=3), source=BLUESKY)
    assert [a.ping for a in h.sent(1)] == [True]
    assert followup_rows(h.db_path) == [(1, CODE, "posted", 1)]


async def test_a_confirmation_while_the_original_is_pending_is_not_sent_in_between(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_code_posts SET status = 'pending'")  # a send in flight
    await later(h, timedelta(hours=2))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []


# --- one server, two kinds of message, one ping budget ---


async def test_an_original_and_a_followup_in_one_turn_are_two_messages_two_units(h):
    add_guild(h.db_path, 1, ping="everyone")
    await at(h, timedelta(0)).run([sighting_item(REDDIT, "community", CODE_Y, at=T0)])
    assert [a.ping for a in h.sent(1)] == [False]
    at(h, timedelta(hours=5))
    await h.run(
        [
            sighting_item(OFFICIAL, "official", CODE_X, at=h.clock),  # a brand new trusted code
            sighting_item(BLUESKY, "community", CODE_Y, at=h.clock),  # confirms Y
        ]
    )
    sent = h.every(1)
    assert [a.ping for a in sent] == [True, True]
    assert CODE_X in sent[0].content and "Confirmed" not in sent[0].content
    assert sent[1].content == f"@everyone Confirmed by a second source: `{CODE_Y}`"
    assert sent[0].nonce != sent[1].nonce
    assert shift_row(h.db_path, 1).ping_count == 2


async def test_with_one_unit_left_the_original_gets_it_and_the_followup_is_skipped(db_path, cfg):
    seed(db_path)
    harness = Rig(db_path, _with_shift(cfg, max_pings_per_day=1), T0)
    add_guild(db_path, 1, ping="everyone")
    await at(harness, timedelta(0)).run([sighting_item(REDDIT, "community", CODE_Y, at=T0)])
    at(harness, timedelta(hours=5))
    await harness.run(
        [
            sighting_item(OFFICIAL, "official", CODE_X, at=harness.clock),
            sighting_item(BLUESKY, "community", CODE_Y, at=harness.clock),
        ]
    )
    assert [a.ping for a in harness.every(1)] == [True]  # the new trusted code pinged
    assert followup_rows(db_path) == [(1, CODE_Y, "skipped", 0)]
    assert shift_row(db_path, 1).ping_count == 1


async def test_a_followup_that_takes_the_last_unit_leaves_a_later_trusted_code_unpinged(
    db_path, cfg
):
    seed(db_path)
    harness = Rig(db_path, _with_shift(cfg, max_pings_per_day=1), T0)
    add_guild(db_path, 1, ping="everyone")
    await first_sighting(harness)
    await later(harness, timedelta(hours=2))  # the follow-up spends the day's only unit
    assert shift_row(db_path, 1).ping_count == 1
    at(harness, timedelta(hours=3))
    await harness.run([sighting_item(OFFICIAL, "official", CODE_X, at=harness.clock)])
    [alert] = harness.every(1)
    assert alert.ping is False and CODE_X in alert.content
    assert any("cap reached" in text for _, text in harness.notices)


async def test_a_role_server_gets_one_role_mention_in_each_of_the_two_messages(h):
    role = "1234567890123456789"
    add_guild(h.db_path, 1, ping=role)
    await at(h, timedelta(0)).run([sighting_item(REDDIT, "community", CODE_Y, at=T0)])
    at(h, timedelta(hours=5))
    await h.run(
        [
            sighting_item(OFFICIAL, "official", CODE_X, at=h.clock),
            sighting_item(BLUESKY, "community", CODE_Y, at=h.clock),
        ]
    )
    sent = h.every(1)
    assert [a.ping_prefix for a in sent] == [f"<@&{role}> ", f"<@&{role}> "]
    assert all("@everyone" not in a.content for a in sent)


# --- a server that leaves, a database that rolls back ---


async def test_a_server_removed_between_the_queue_and_the_send_is_skipped_without_a_fuss(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await first_sighting(h)
    h.clock = T0 + timedelta(hours=5)
    await _release(
        h.deps(), [sighting_item(BLUESKY, "community", CODE, at=h.clock)], seeding_ok=True
    )
    assert {g for g, *_ in followup_rows(h.db_path)} == {1, 2}
    with closing(connect(h.db_path)) as conn:
        assert repo.delete_guild(conn, 1)
    outcomes = await h.deliver()
    assert [o.error for o in outcomes] == [None]
    assert h.sent(1) == [] and len(h.sent(2)) == 1
    assert followup_rows(h.db_path) == [(2, CODE, "posted", 1)]
    assert h.notices == []


async def test_a_server_that_switches_alerts_off_before_the_followup_gets_nothing_and_no_unit(h):
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    h.clock = T0 + timedelta(hours=5)
    await _release(
        h.deps(), [sighting_item(BLUESKY, "community", CODE, at=h.clock)], seeding_ok=True
    )
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 1, enabled=False, channel_id=100, ping="everyone", now=lambda: h.clock)
    await h.deliver()
    assert h.sent(1) == [] and shift_row(h.db_path, 1).ping_count == 0
    assert followup_rows(h.db_path) == [(1, CODE, "skipped", 0)]


async def test_codes_a_rolled_back_v22_announced_get_no_followups_and_no_crash(h):
    """v2.2 posted CODE while v3 was rolled back: an `alerted_codes` row, no per-server post,
    no sightings. After roll-forward two sources see it; nobody is owed anything."""
    add_guild(h.db_path, 1, ping="everyone")
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, message_id, "
            "pinged, status, from_roundup) VALUES (?, ?, ?, 'https://example.com/x', 9, 0, "
            "'posted', 0)",
            (CODE, (T0 - timedelta(hours=2)).isoformat(), REDDIT),
        )
    await first_sighting(h)
    await later(h, timedelta(hours=1))
    assert h.sent(1) == [] and followup_rows(h.db_path) == []
    assert {row[1] for row in sightings(h.db_path)} == {REDDIT, BLUESKY}  # remembered anyway


async def test_a_second_source_that_only_v22_saw_is_invisible_to_v3(h):
    """Sightings are recorded by v3 alone. A source that v2.2 saw during a rollback left no
    row, so a code first seen by v3 with one source stays unconfirmed until v3 sees another."""
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    assert sightings(h.db_path) == [(CODE, REDDIT, 0, 0)]
    await later(h, timedelta(hours=1), source=REDDIT)  # the same source again
    assert followup_rows(h.db_path) == []


# --- migrations that die halfway ---


def _dir_up_to(tmp_path: Path, version: int, sabotage: int | None = None) -> Path:
    """A migrations directory with 001..version, and `sabotage`'s file ending in bad SQL."""
    source = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"
    target = tmp_path / f"migrations_{version}_{sabotage}"
    target.mkdir()
    for path in sorted(source.glob("[0-9][0-9][0-9]_*.sql")):
        number = int(path.name.split("_", 1)[0])
        if number > version:
            continue
        if number == sabotage:
            (target / path.name).write_text(path.read_text() + "\nSELECT * FROM no_such_table;\n")
        else:
            shutil.copy(path, target / path.name)
    return target


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608


@pytest.mark.parametrize(
    ("number", "table", "column"),
    [
        (6, "guild_code_posts", "followup_ok"),
        (7, "digests", "items_upto"),
    ],
)
def test_a_migration_that_dies_halfway_rolls_back_whole_and_runs_cleanly_next_time(
    tmp_path, monkeypatch, number, table, column
):
    from newsbot.store import db

    path = tmp_path / "t.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", _dir_up_to(tmp_path, number - 1))
        with closing(connect(path)) as conn:
            assert migrate(conn) == number - 1
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", _dir_up_to(tmp_path, number, sabotage=number))
        with closing(connect(path)) as conn, pytest.raises(sqlite3.OperationalError):
            migrate(conn)
    with closing(connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == number - 1
        assert column not in _columns(conn, table)  # the ALTERs before the bad line are undone
        if number == 6:
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('code_sightings', 'guild_code_followups')"
                ).fetchall()
                == []
            )
        assert migrate(conn) == 8  # the real files, from where it stopped
        assert column in _columns(conn, table)


async def test_the_walk_still_delivers_after_a_database_that_lost_its_followup_rows(h):
    """A restored backup from before 006 has no follow-up rows at all. Delivery must not care."""
    add_guild(h.db_path, 1, ping="everyone")
    await first_sighting(h)
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("DELETE FROM guild_code_followups")
        conn.execute("DELETE FROM code_sightings")
    outcomes = await deliver_queued_codes(h.deps(), startup=True)
    assert outcomes == [] and followup_rows(h.db_path) == []
    await later(h, timedelta(hours=2))  # a fresh sighting rebuilds the picture from scratch
    assert [row[0] for row in sightings(h.db_path)] == [CODE]


def test_nonces_are_unique_across_servers_and_channels_and_never_shared_with_a_followup():
    candidate = CodeCandidate(
        CODE, golden=False, source_name="s", item_url="https://e.com", fresh=True
    )
    seen: dict[str, str] = {}
    for guild in range(1, 31):
        for channel in (guild * 100, guild * 100 + 1, 7):
            scope = f"{guild}|{channel}"
            original = render_code_alerts([candidate], ping=False, nonce_scope=scope)[0].nonce
            follow = render_followup_alert(
                [CODE], ping_mention="@everyone", nonce_scope=scope
            ).nonce
            for label, nonce in ((f"{scope} original", original), (f"{scope} followup", follow)):
                assert len(nonce) == 25 and nonce not in seen, (label, seen.get(nonce))
                seen[nonce] = label
