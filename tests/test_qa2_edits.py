"""QA round 2, area 3: attacking edit pings (batch d36d492).

An edit now sends the ping choice the message went out with, because Discord rebuilds a
message's mentions from each edit and `none()` would strip the highlight. That makes the edit
the second place in the codebase that can carry a live mention, so the questions are all of
the form "can an edit ever carry a mention the message didn't already have?": a role deleted
since, settings changed since, a header stripped, a continuation message, text a source
controls, and the count of places that can ping at all.
"""

from __future__ import annotations

import ast
import re
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from test_code_alert_editor import _Channel, _editor
from test_qa1_edits import stored_pings
from test_shift_delivery_details import BIG_RESERVE, store_details
from test_shift_edits import EditHarness, learn
from test_shift_fanout import CODE_A, CODE_B, CODE_C, NOW, ROLE, V3, add_guild, item, seed

from newsbot.bot import client as client_module
from newsbot.collectors.base import ItemDetails
from newsbot.config import load_config
from newsbot.pipeline.publisher import PublishError
from newsbot.shift.details import DetailObservation
from newsbot.store import repo
from newsbot.store.db import connect, migrate

HOUR = timedelta(hours=1)
OTHER_ROLE = "9876543210987654321"
# What would notify somebody if it were live, outside a code block.
LIVE = re.compile(r"@everyone|@here|<@[&!]?\d+>")
SRC = Path(__file__).parent.parent / "newsbot"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def h(db_path):
    harness = EditHarness(db_path, load_config(V3), NOW)
    seed(db_path)
    return harness


def set_server_ping(db_path, guild_id, ping):
    with closing(connect(db_path)) as conn, conn:
        conn.execute("UPDATE guild_shift SET ping = ? WHERE guild_id = ?", (ping, guild_id))


def live_mentions(content: str) -> list[str]:
    """Mention-shaped text outside code blocks (the codes themselves are never mention-shaped)."""
    return LIVE.findall(re.sub(r"```.*?```", "", content, flags=re.S))


async def edit_after_learning(h, reward="3 Golden Keys", code=CODE_A):
    learn(h, code, reward, at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    return h.editors.calls


# --- the server's settings change after the message went out ---


@pytest.mark.parametrize("posted_with", ["everyone", ROLE, "none"])
@pytest.mark.parametrize("now_set_to", ["everyone", ROLE, OTHER_ROLE, "none"])
async def test_an_edit_repeats_the_ping_the_message_went_out_with_not_the_servers_current_one(
    h, posted_with, now_set_to
):
    add_guild(h.db_path, 1, ping=posted_with)
    await h.run([item(CODE_A)])
    set_server_ping(h.db_path, 1, now_set_to)

    (call,) = await edit_after_learning(h)

    assert call.ping == posted_with
    # And the text still starts the way it did, so the ping and the text agree.
    expected_live = {"everyone": ["@everyone"], ROLE: [f"<@&{ROLE}>"], "none": []}[posted_with]
    assert live_mentions(call.content) == expected_live
    assert stored_pings(h.db_path)[0][2] == posted_with


async def test_a_server_that_later_starts_pinging_does_not_turn_an_old_unpinged_alert_into_one(h):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    set_server_ping(h.db_path, 1, "everyone")

    (call,) = await edit_after_learning(h)

    assert call.ping == "none"
    assert live_mentions(call.content) == []


async def test_a_deleted_role_still_edits_with_the_roles_id_and_nobody_else(h):
    add_guild(h.db_path, 1, ping=ROLE)
    await h.run([item(CODE_A)])
    # The role is deleted and the server falls back to no ping; the old header still says
    # "<@&ROLE>" (Discord shows it as a deleted role).
    set_server_ping(h.db_path, 1, "none")
    (call,) = await edit_after_learning(h)
    assert call.ping == ROLE

    channel = _Channel()
    await _editor(channel).edit(9001, call.content, ping=call.ping)

    ((_, kwargs),) = channel.edits
    mentions = kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.replied_user
    assert [r.id for r in mentions.roles] == [int(ROLE)]  # one role id, which may not exist anymore


async def test_a_header_edited_to_a_different_role_in_the_database_edits_with_none(h):
    add_guild(h.db_path, 1, ping=ROLE)
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET ping = ?", (OTHER_ROLE,))

    (call,) = await edit_after_learning(h)

    assert call.ping == "none"  # the header wears ROLE, not OTHER_ROLE: no mention is added


@pytest.mark.parametrize(
    "header", [" ", "@everyone", "@everyone\n**New SHiFT code**", "@EVERYONE **x**"]
)
async def test_a_stored_everyone_with_a_stripped_or_odd_header_never_edits_with_everyone(h, header):
    add_guild(h.db_path, 1, ping="none")
    await h.run([item(CODE_A)])
    with closing(connect(h.db_path)) as conn, conn:
        conn.execute("UPDATE guild_alert_messages SET ping = 'everyone', header = ?", (header,))

    calls = await edit_after_learning(h)

    # Either the edit is skipped as unrenderable or it goes out with every mention off.
    assert all(c.ping == "none" for c in calls)


@pytest.mark.parametrize("ping", ["everyone", ROLE])
async def test_an_alert_that_lost_its_ping_to_the_daily_cap_edits_with_none(h, ping):
    add_guild(h.db_path, 1, ping=ping, ping_day="2026-10-01", ping_count=3)
    await h.run([item(CODE_A)])
    ((_, _, stored, header),) = stored_pings(h.db_path)
    assert stored == "none"
    assert live_mentions(header) == []

    (call,) = await edit_after_learning(h)

    assert call.ping == "none"
    assert live_mentions(call.content) == []


async def test_a_role_ping_stripped_by_an_ambiguous_retry_edits_with_none(h):
    add_guild(h.db_path, 1, ping=ROLE)
    h.fail_with[1] = [PublishError("timed out")]
    await h.run([item(CODE_A)])

    (call,) = await edit_after_learning(h)

    assert call.ping == "none"
    assert live_mentions(call.content) == []


# --- golden alerts and continuation messages ---


async def test_a_golden_alert_edit_keeps_its_ping_text_and_its_ping(h):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A, title="Borderlands 4 SHiFT: Golden Keys! " + CODE_A)])
    ((_, _, ping, header),) = stored_pings(h.db_path)
    assert ping == "everyone"
    assert "Golden" in header  # the golden variant of the header

    (call,) = await edit_after_learning(h)

    assert call.ping == "everyone"
    assert call.content.startswith(header)
    assert live_mentions(call.content) == ["@everyone"]


async def test_a_continuation_messages_edit_has_no_ping_and_the_first_keeps_its_own(h):
    add_guild(h.db_path, 1, ping="everyone")
    h.reserve = BIG_RESERVE  # two to a message: A and B, then C
    await h.run([item(CODE_A), item(CODE_B, url="https://e/b"), item(CODE_C, url="https://e/c")])
    assert [row[2] for row in stored_pings(h.db_path)] == ["everyone", "none"]

    learn(h, CODE_C, "Cosmetic: Gnome", at=NOW + HOUR)
    h.clock = NOW + 2 * HOUR
    await h.run([])
    (second,) = h.editors.calls
    assert second.ping == "none"
    assert live_mentions(second.content) == []

    learn(h, CODE_A, "3 Golden Keys", at=NOW + 3 * HOUR)
    h.clock = NOW + 4 * HOUR
    await h.run([])
    first = h.editors.calls[-1]
    assert first.ping == "everyone"
    assert live_mentions(first.content) == ["@everyone"]
    assert len(h.editors.calls) == 2  # each message edited once, for its own code


# --- text a source controls ---

HOSTILE = [
    "@everyone",
    "@here",
    "@everyone @here",
    f"<@&{ROLE}>",
    f"<@&{OTHER_ROLE}>",
    "<@123456789012345678>",
    "<@!123456789012345678>",
    "@\u200beveryone",
    "\uff20everyone",
    "@every\u200bone",
]


@pytest.mark.parametrize("reward", HOSTILE)
@pytest.mark.parametrize("ping", ["everyone", ROLE, "none"])
async def test_a_hostile_reward_adds_no_live_mention_beyond_the_headers_own(h, reward, ping):
    add_guild(h.db_path, 1, ping=ping)
    await h.run([item(CODE_A)])
    expected = {"everyone": ["@everyone"], ROLE: [f"<@&{ROLE}>"], "none": []}[ping]

    (call,) = await edit_after_learning(h, reward)

    assert live_mentions(call.content) == expected
    assert call.ping == ping


@pytest.mark.parametrize("name", ["@everyone", "@here", f"<@&{ROLE}>", "<@123456789012345678>"])
async def test_a_hostile_source_name_in_the_via_clause_adds_no_live_mention(h, name):
    add_guild(h.db_path, 1, ping="everyone")
    await h.run([item(CODE_A)])
    store_details(h.db_path, CODE_A, reward="3 Golden Keys", name=name, now=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert "via" in call.content
    assert live_mentions(call.content) == ["@everyone"]


@pytest.mark.parametrize("origin", ["@everyone", f"<@&{ROLE}>"])
async def test_a_hostile_origin_on_a_permanent_code_adds_no_live_mention(h, origin):
    add_guild(h.db_path, 1, ping=ROLE)
    await h.run([item(CODE_A)])
    obs = DetailObservation(
        CODE_A,
        ItemDetails(
            code=CODE_A,
            rank="table",
            reward="Skin: Sunrise",
            category="permanent",
            origin=origin,
            listed=True,
        ),
        "mentalmars",
        None,
    )
    with closing(connect(h.db_path)) as conn:
        repo.upsert_code_details(conn, [obs], now=NOW + HOUR)

    h.clock = NOW + 2 * HOUR
    await h.run([])

    (call,) = h.editors.calls
    assert live_mentions(call.content) == [f"<@&{ROLE}>"]


async def test_the_headers_ping_text_comes_first_and_only_once_even_when_the_reward_repeats_it(h):
    add_guild(h.db_path, 1, ping=ROLE)
    await h.run([item(CODE_A)])

    (call,) = await edit_after_learning(h, f"<@&{ROLE}> <@&{ROLE}>")

    assert call.content.startswith(f"<@&{ROLE}> ")
    assert call.content.count(f"<@&{ROLE}>") == 1


# --- how many places can ping at all ---


def _calls_named(tree, name):
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == name
    ]


def test_the_one_everyone_true_lives_in_the_client_module_and_nowhere_else():
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls_named(tree, "AllowedMentions"):
            for kw in call.keywords:
                if kw.arg == "everyone" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    hits.append(str(path.relative_to(SRC)))
    assert hits == ["bot/client.py"]


def test_every_message_edit_in_the_codebase_names_its_allowed_mentions():
    bare = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls_named(tree, "edit"):
            keys = {kw.arg for kw in call.keywords}
            if "content" in keys and "allowed_mentions" not in keys:
                bare.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert bare == []


def test_only_the_literal_everyone_choice_hands_back_the_everyone_object():
    for choice in [
        "everyone ",
        "Everyone",
        "everyone\n",
        "\uff45veryone",
        None,
        True,
        1,
        b"everyone",
    ]:
        assert client_module.mentions_for(choice) is not client_module._PING_EVERYONE
        assert not client_module.mentions_for(choice).everyone
    assert client_module.mentions_for("everyone") is client_module._PING_EVERYONE
