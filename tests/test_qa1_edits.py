"""QA fix batch 1, fixes 4, 7, 13, 14 and the edit half of 15: how an edit is sent, and to whom.

Fix 4. Discord rebuilds a message's mentions from each edit's `allowed_mentions`, so an edit
sent with `AllowedMentions.none()` strips the highlight from an alert that pinged. The ping
choice is stored with the message (`guild_alert_messages.ping`) and goes back with the edit,
but only while the stored header still starts with that ping's text.

Fix 7. A server that switched its SHiFT alerts off isn't edited.

Fix 13. Every new `details_changed_at` is later than every other row's, so a message's
version stamp (the largest among its codes) is safe against any code's.

Fix 14. `displayed()` is what the details line shows, so a category-only change is no change.

Fix 15 (edits). A database that lacks a column or table is an error with its text in the log,
not a "busy" shrug.

Real temp SQLite, the real migrations, the fake editors of the other edit tests, plus the real
`DiscordCodeAlertEditor` over a tiny fake channel where `allowed_mentions` is the question.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import discord
import pytest
from test_code_alert_editor import CHANNEL, GUILD, _Channel, _editor
from test_shift_delivery_details import BIG_RESERVE
from test_shift_edits import EditHarness, learn, message_state
from test_shift_fanout import CODE_A, CODE_B, CODE_C, NOW, ROLE, V3, add_guild, item, rows, seed

from newsbot.bot import client as client_module
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift import edits as edits_module
from newsbot.shift.details import DetailObservation, displayed, merge_details
from newsbot.shift.edits import edit_changed_alerts
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import AlertMessageRecord, CodeDetailsRow

HOUR = timedelta(hours=1)
MENTALMARS = "mentalmars"


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
    harness = EditHarness(db_path, cfg, NOW)
    seed(db_path)
    return harness


def stored_pings(db_path) -> list[tuple[int, int, str, str]]:
    return rows(
        db_path,
        "SELECT guild_id, message_id, ping, header FROM guild_alert_messages "
        "ORDER BY guild_id, message_id",
    )


# =====================================================================================
# Fix 4: the real editor sends the ping choice as the edit's mentions
# =====================================================================================


async def test_an_everyone_edit_carries_the_one_ping_everyone_object():
    channel = _Channel()

    await _editor(channel).edit(9001, "@everyone **New SHiFT code**", ping="everyone")

    ((_, kwargs),) = channel.edits
    assert kwargs["allowed_mentions"] is client_module._PING_EVERYONE


async def test_a_role_edit_mentions_that_role_and_nobody_else():
    channel = _Channel()

    await _editor(channel).edit(9001, f"<@&{ROLE}> **New SHiFT code**", ping=ROLE)

    ((_, kwargs),) = channel.edits
    mentions = kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.replied_user
    assert [r.id for r in mentions.roles] == [int(ROLE)]


@pytest.mark.parametrize("choice", ["none", "", "everyone ", "EVERYONE", "<@&1234>", "12", None, 5])
async def test_anything_else_edits_with_every_mention_off(choice):
    channel = _Channel()

    await _editor(channel).edit(9001, "x", ping=choice)

    ((_, kwargs),) = channel.edits
    mentions = kwargs["allowed_mentions"]
    assert not (mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)


async def test_the_default_is_no_mentions():
    channel = _Channel()
    await _editor(channel).edit(9001, "x")
    ((_, kwargs),) = channel.edits
    assert not kwargs["allowed_mentions"].everyone


def test_the_editor_defaults_to_the_safe_choice():
    import inspect

    default = inspect.signature(client_module.DiscordCodeAlertEditor.edit).parameters["ping"]
    assert default.default == "none"
    assert GUILD and CHANNEL  # (the fake channel's ids, used by `_editor`)


# =====================================================================================
# Fix 4: the choice is stored when the alert posts
# =====================================================================================


async def test_the_ping_choice_is_stored_with_each_posted_message(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    add_guild(h.db_path, 3, ping="none")

    await h.run([item(CODE_A)])

    assert stored_pings(h.db_path) == [
        (1, 9001, "everyone", "@everyone **New SHiFT code**"),
        (2, 9001, ROLE, f"<@&{ROLE}> **New SHiFT code**"),
        (3, 9001, "none", "**New SHiFT code**"),
    ]


async def test_a_continuation_message_has_no_ping_and_stores_none(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE  # two to a message: A and B, then C
    await h.run(
        [
            item(CODE_A, url="https://e/a"),
            item(CODE_B, url="https://e/b"),
            item(CODE_C, url="https://e/c"),
        ]
    )

    stored = stored_pings(h.db_path)
    assert [(s[2], s[3].startswith("@everyone")) for s in stored] == [
        ("everyone", True),
        ("none", False),
    ]


async def test_a_community_only_batch_posts_unpinged_and_stores_none(h):
    add_guild(h.db_path, 1, ping="everyone")

    await h.run([item(CODE_A, trust="community")])

    assert [s[2] for s in stored_pings(h.db_path)] == ["none"]


async def test_a_ping_the_retry_stripped_stores_none_and_a_header_without_it(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [PublishError("timed out")]  # ambiguous: the retry goes out unpinged

    await h.run([item(CODE_A)])

    assert stored_pings(h.db_path) == [(1, 9001, "none", "**New SHiFT code**")]


def test_a_ping_the_table_would_refuse_is_stored_as_none(db_path):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 1, timezone="UTC", set_up=True, now=lambda: NOW)
        repo.set_shift(conn, 1, enabled=True, channel_id=100, ping="none", now=lambda: NOW)
        repo.record_released_codes(
            conn,
            [(CODE_A, "g", "https://e/a", False)],
            now=lambda: NOW,
            queue_for_games=[],
        )
        repo.claim_guild_codes(conn, 1, [CODE_A], pinged=False, local_day="2026-10-01")
        repo.mark_guild_codes_posted(
            conn,
            1,
            [CODE_A],
            message_id=9001,
            message=AlertMessageRecord(
                channel_id=100,
                header="**New SHiFT code**",
                golden_prefix=False,
                posted_at=NOW,
                rendered_at=NOW,
                ping="@everyone and then some",
            ),
        )

    assert stored_pings(db_path) == [(1, 9001, "none", "**New SHiFT code**")]


def test_the_table_refuses_a_ping_that_is_not_none_everyone_or_a_role_id(db_path):
    with closing(connect(db_path)) as conn:
        repo.create_guild(conn, 1, timezone="UTC", set_up=True, now=lambda: NOW)
        for bad in ("here", "12345", "123456789012345678901", "1234567890123456a", ""):
            with pytest.raises(sqlite3.IntegrityError), conn:
                conn.execute(
                    "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
                    "posted_at, rendered_at, ping) VALUES (1, 1, 1, 'h', 'x', 'x', ?)",
                    (bad,),
                )
        with conn:
            conn.execute(
                "INSERT INTO guild_alert_messages (guild_id, message_id, channel_id, header, "
                "posted_at, rendered_at) VALUES (1, 2, 1, 'h', 'x', 'x')"
            )
    assert stored_pings(db_path)[0][2] == "none"  # the default


# =====================================================================================
# Fix 4: the edit pass hands it back, when the header still wears it
# =====================================================================================


async def test_an_edit_of_a_pinged_alert_sends_the_stored_choice_back(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping=ROLE)
    add_guild(h.db_path, 3, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert [(c.guild_id, c.ping) for c in h.editors.calls] == [
        (1, "everyone"),
        (2, ROLE),
        (3, "none"),
    ]
    # And the text, as before: the header is as it went out.
    assert h.editors.calls[0].content.startswith("@everyone **New SHiFT code**")


async def test_an_edit_of_an_alert_whose_ping_was_stripped_sends_none(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.fail_with[1] = [PublishError("timed out")]
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert call.ping == "none"
    assert "@everyone" not in call.content


@pytest.mark.parametrize(
    ("ping", "header"),
    [
        ("everyone", "**New SHiFT code**"),  # the header lost its prefix (a hand edit)
        (ROLE, "@everyone **New SHiFT code**"),  # the wrong prefix for the stored choice
        ("everyone", " @everyone **New SHiFT code**"),
        ("everyone", "@everyone**New SHiFT code**"),  # no space: not the ping's own text
    ],
)
async def test_a_header_that_does_not_wear_its_ping_edits_with_none(h, ping, header):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET ping = ?, header = ?", (ping, header))
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert call.ping == "none"


async def test_a_record_from_before_this_column_edits_with_none(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET ping = 'none'")  # the column's default
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    assert [c.ping for c in h.editors.calls] == ["none"]


async def test_hostile_reward_text_adds_no_second_live_mention_to_a_pinged_edit(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "@everyone @here <@&1234567890123456789> free keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert call.ping == "everyone"
    # The header's is the only "@everyone" an unescaped reader would see; the reward's is defused.
    assert call.content.count("@everyone") == 1
    assert "@here" not in call.content
    assert "<@&1234567890123456789>" not in call.content


def test_exactly_one_everyone_true_still_exists():
    from test_mentions_tripwire import _everyone_true_calls

    assert len(_everyone_true_calls()) == 1


# =====================================================================================
# Fix 7: servers with SHiFT alerts off aren't edited
# =====================================================================================


async def test_a_server_that_switched_alerts_off_is_not_edited_until_it_switches_them_on(h):
    add_guild(h.db_path, 1, ping="everyone")
    add_guild(h.db_path, 2, ping="everyone")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn:
        repo.set_shift(conn, 2, enabled=False, channel_id=200, ping="everyone", now=lambda: NOW)
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert [c.guild_id for c in h.editors.calls] == [1]
    assert [state for _g, _m, state, _r in message_state(h.db_path, 2)] == ["live"]

    with closing(connect(h.db_path)) as conn:
        assert repo.alert_messages_to_edit(conn, now=h.clock) == []
        repo.set_shift(conn, 2, enabled=True, channel_id=200, ping="everyone", now=lambda: NOW)
        assert [t.guild_id for t in repo.alert_messages_to_edit(conn, now=h.clock)] == [2]
    h.clock = NOW + 3 * HOUR
    await h.run([])
    assert [c.guild_id for c in h.editors.calls] == [1, 2]


# =====================================================================================
# Fix 13: stamps are later than every other row's
# =====================================================================================


def _obs(code: str, reward: str, name: str = MENTALMARS) -> DetailObservation:
    details = ItemDetails(code=code, rank="table", reward=reward, listed=True)
    return DetailObservation(code, details, name, None)


def _stamp(db_path: str, code: str) -> datetime:
    with closing(connect(db_path)) as conn:
        return repo.code_details_for(conn, [code])[code].details_changed_at


def test_two_changes_in_one_pass_get_two_strictly_increasing_stamps(db_path):
    with closing(connect(db_path)) as conn:
        changed = repo.upsert_code_details(conn, [_obs(CODE_A, "a"), _obs(CODE_B, "b")], now=NOW)
    assert changed == [CODE_A, CODE_B]
    a, b = _stamp(db_path, CODE_A), _stamp(db_path, CODE_B)
    assert a == NOW
    assert b == NOW + timedelta(microseconds=1)


def test_a_clock_that_stepped_back_still_stamps_above_every_other_row(db_path):
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(conn, [_obs(CODE_A, "a")], now=NOW + 2 * HOUR)
        repo.upsert_code_details(conn, [_obs(CODE_B, "b")], now=NOW)  # an hour behind
    assert _stamp(db_path, CODE_B) == NOW + 2 * HOUR + timedelta(microseconds=1)


def test_a_change_that_is_not_shown_does_not_move_any_stamp_or_the_floor(db_path):
    with closing(connect(db_path)) as conn:
        repo.upsert_code_details(conn, [_obs(CODE_A, "a")], now=NOW)
        # Same reward, same name: nothing an alert shows changed.
        assert repo.upsert_code_details(conn, [_obs(CODE_A, "a")], now=NOW + HOUR) == []
    assert _stamp(db_path, CODE_A) == NOW


def test_the_floor_ignores_a_stamp_that_is_not_a_timestamp(db_path, caplog):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO code_details (code, clock_start, updated_at, details_changed_at) "
            "VALUES (?, 'x', 'x', 'zzz-not-a-time')",
            (CODE_B,),
        )
    with closing(connect(db_path)) as conn, caplog.at_level(logging.WARNING):
        repo.upsert_code_details(conn, [_obs(CODE_A, "a")], now=NOW)
    assert _stamp(db_path, CODE_A) == NOW
    assert "isn't a timestamp" in caplog.text


async def test_a_change_to_a_sibling_code_with_an_older_clock_still_edits_the_message(h):
    add_guild(h.db_path, 1, ping="none")
    h.reserve = 0
    await h.run([item(CODE_A, url="https://e/a"), item(CODE_B, url="https://e/b")])
    assert {row[0] for row in rows(h.db_path, "SELECT message_id FROM guild_code_posts")} == {
        9001
    }  # one message holds both

    learn(h, CODE_A, "3 Golden Keys", at=NOW + 3 * HOUR)
    h.clock = NOW + 4 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 1

    # B's table row is read by a pass whose clock is behind A's change (a restart with a skewed
    # clock, a second process). Without the floor, B's stamp would sit below the message's.
    learn(h, CODE_B, "A skin", at=NOW + 2 * HOUR)
    h.clock = NOW + 5 * HOUR
    await h.run([])

    assert len(h.editors.calls) == 2
    assert "A skin" in h.editors.calls[1].content
    assert "3 Golden Keys" in h.editors.calls[1].content


# =====================================================================================
# Fix 14: displayed() is what the line shows
# =====================================================================================

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def _row(**fields) -> CodeDetailsRow:
    return CodeDetailsRow(code=CODE_A, clock_start=NOW, updated_at=NOW, **fields)


def test_a_category_that_never_reaches_the_line_is_not_displayed():
    golden = _row(reward="3 Golden Keys", reward_from="m", category="golden", category_from="m")
    other = replace(golden, category="other", category_source="table", category_from="elsewhere")
    cosmetic = replace(golden, category="cosmetic", category_from="third")
    assert displayed(golden) == displayed(other) == displayed(cosmetic)


def test_permanent_is_displayed_with_whoever_set_it():
    plain = _row(reward="A skin", reward_from="m")
    permanent = replace(plain, category="permanent", category_from="m")
    renamed = replace(permanent, category_from="n")
    assert displayed(plain) != displayed(permanent)
    assert displayed(permanent) != displayed(renamed)


def test_a_stray_expiry_on_a_permanent_code_is_not_displayed():
    permanent = _row(reward="A skin", reward_from="m", category="permanent", category_from="m")
    assert displayed(permanent) == displayed(replace(permanent, expires_on=NOW.date()))


def test_who_supplied_a_value_only_matters_when_the_value_is_there():
    assert displayed(_row(reward_from="m")) == displayed(_row(reward_from="n"))
    assert displayed(_row(expires_from="m")) == displayed(_row(expires_from="n"))
    assert displayed(_row(reward="x", reward_from="m")) != displayed(
        _row(reward="x", reward_from="n")
    )
    day = NOW.date()
    assert displayed(_row(expires_on=day, expires_from="m")) != displayed(
        _row(expires_on=day, expires_from="n")
    )


def test_no_row_and_an_empty_row_show_the_same_nothing():
    assert displayed(None) == displayed(_row())


def test_a_category_only_observation_is_not_a_change_and_the_first_reward_is():
    category_only = DetailObservation(
        CODE_A, ItemDetails(code=CODE_A, rank="table", category="golden", listed=True), "m", None
    )
    row = merge_details(None, category_only, now=NOW, first_seen_at=None, details_since=SINCE)
    assert row.category == "golden"
    assert row.details_changed_at is None

    reward = DetailObservation(
        CODE_A, ItemDetails(code=CODE_A, rank="table", reward="3 Golden Keys"), "m", None
    )
    row = merge_details(row, reward, now=NOW + HOUR, first_seen_at=None, details_since=SINCE)
    assert row.details_changed_at == NOW + HOUR


def test_upsert_reports_a_category_flip_as_no_change(db_path):
    first = DetailObservation(
        CODE_A,
        ItemDetails(code=CODE_A, rank="table", reward="x", category="golden", listed=True),
        "m",
        None,
    )
    flipped = DetailObservation(
        CODE_A,
        ItemDetails(code=CODE_A, rank="table", reward="x", category="other", listed=True),
        "m",
        None,
    )
    with closing(connect(db_path)) as conn:
        assert repo.upsert_code_details(conn, [first], now=NOW) == [CODE_A]
        assert repo.upsert_code_details(conn, [flipped], now=NOW + HOUR) == []
        stored = repo.code_details_for(conn, [CODE_A])[CODE_A]
    assert stored.category == "other"  # stored, just not worth an edit
    assert stored.details_changed_at == NOW


async def test_a_category_only_change_does_not_edit_a_posted_alert(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    assert len(h.editors.calls) == 1

    obs = DetailObservation(
        CODE_A,
        ItemDetails(code=CODE_A, rank="table", reward="3 Golden Keys", category="cosmetic"),
        MENTALMARS,
        None,
    )
    with closing(connect(h.db_path)) as conn:
        assert repo.upsert_code_details(conn, [obs], now=NOW + 3 * HOUR) == []
    h.clock = NOW + 4 * HOUR
    await h.run([])

    assert len(h.editors.calls) == 1


# =====================================================================================
# Fix 15 (edits): a missing column is an error, a busy database is a shrug
# =====================================================================================


def _raises(message: str):
    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError(message)

    return boom


async def test_a_missing_column_while_finding_edits_is_an_error_with_its_text(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="none")
    monkeypatch.setattr(edits_module, "alert_messages_to_edit", _raises("no such column: m.ping"))

    with (
        caplog.at_level(logging.WARNING, logger="newsbot.shift.edits"),
        pytest.raises(sqlite3.OperationalError, match="no such column"),
    ):
        await edit_changed_alerts(h.deps(), deadline=100.0)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "no such column: m.ping" in errors[0].getMessage()


async def test_a_missing_table_is_an_error_too(h, monkeypatch, caplog):
    add_guild(h.db_path, 1, ping="none")
    monkeypatch.setattr(
        edits_module, "alert_messages_to_edit", _raises("no such table: guild_alert_messages")
    )

    with (
        caplog.at_level(logging.WARNING, logger="newsbot.shift.edits"),
        pytest.raises(sqlite3.OperationalError),
    ):
        await edit_changed_alerts(h.deps(), deadline=100.0)

    assert any(
        r.levelno >= logging.ERROR and "no such table" in r.getMessage() for r in caplog.records
    )


async def test_a_busy_database_is_a_warning_that_says_why_and_the_pass_moves_on(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="none")
    monkeypatch.setattr(edits_module, "alert_messages_to_edit", _raises("database is locked"))

    with caplog.at_level(logging.WARNING, logger="newsbot.shift.edits"):
        outcome = await edit_changed_alerts(h.deps(), deadline=100.0)

    assert outcome == edits_module.EditOutcome()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "database is locked" in caplog.records[0].getMessage()


async def test_a_missing_column_while_recording_an_edit_is_an_error_not_busy(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    monkeypatch.setattr(
        edits_module, "mark_alert_message_rendered", _raises("no such column: edit_tried_at")
    )
    h.clock = NOW + 2 * HOUR

    with (
        caplog.at_level(logging.WARNING, logger="newsbot.shift.edits"),
        pytest.raises(sqlite3.OperationalError, match="edit_tried_at"),
    ):
        await edit_changed_alerts(h.deps(), deadline=100.0)

    assert any(
        r.levelno >= logging.ERROR and "edit_tried_at" in r.getMessage() for r in caplog.records
    )


async def test_a_locked_database_while_recording_an_edit_logs_its_text_and_carries_on(
    h, monkeypatch, caplog
):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    learn(h, CODE_A, "3 Golden Keys", at=NOW + HOUR)
    monkeypatch.setattr(edits_module, "mark_alert_message_rendered", _raises("database is locked"))
    h.clock = NOW + 2 * HOUR

    with caplog.at_level(logging.WARNING, logger="newsbot.shift.edits"):
        outcome = await edit_changed_alerts(h.deps(), deadline=100.0)

    assert outcome.edited == 1
    assert any("database is locked" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_the_walk_survives_a_schema_error_from_the_edit_pass(h, monkeypatch, caplog):
    """`_deliver_then_edit` already logs and swallows what the pass raises; deliveries are done."""
    add_guild(h.db_path, 1, ping="none")
    monkeypatch.setattr(edits_module, "alert_messages_to_edit", _raises("no such column: ping"))
    from newsbot.shift import fanout

    monkeypatch.setattr(fanout, "edit_changed_alerts", edits_module.edit_changed_alerts)

    with caplog.at_level(logging.ERROR):
        await h.run([item(CODE_A)])

    assert h.sent(1)  # the alert still went out
    assert any("no such column" in r.getMessage() for r in caplog.records)
    assert discord  # (the module is imported for the mention types above)
