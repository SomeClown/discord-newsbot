"""Who can be pinged after the cutover, and the short list of who is allowed to ping.

v2.2 had one place that could wake a server up (the SHiFT poster) and one server to wake. v3
has the same poster, run once per server with that server's own choice of ping, in a bot that
other people's servers now invite on their own. So the question for the cutover isn't "does
the poster still work" (it does, see `test_shift_fanout*.py`) but "has anything else grown the
ability to ping while nobody was watching". Everything the bot says unprompted (the first
contact message, the import notice, the owner report, each server's run report and notices)
has to be inert: no `@everyone`, no roles, no users, no replies, whatever a hostile server
name, source name or scraped error message tries to smuggle into the text.

The checks come in three flavors. Audits that run the whole startup and a digest and then
look at every single send. Hostile text pushed through the paths that carry scraped or
user-chosen strings. And the SHiFT matrix: three servers (everyone, a role, nobody) and the
friend, one code, each getting its own mention policy, its own trust rule and its own
daily cap, with none of it leaking sideways. A last scan of the source tree holds the line on
roles and users the way `test_mentions_tripwire.py` already does for `@everyone`.
"""

from __future__ import annotations

import ast
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
import pytest
from cutover_world import (
    BL4_CH,
    DUE,
    FRIEND,
    FRIEND_ADMIN_CH,
    OWNER_CH,
    OWNER_GUILD,
    SHIFT_CH,
    Chan,
    add_item,
    code_item,
    collect_these,
    fake_gateway,
    guild_stub,
)
from test_shift_fanout import add_guild

from newsbot.bot.client import mentions_for
from newsbot.store import repo
from newsbot.store.db import connect

T_SHIFT = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
HOSTILE = "@everyone @here <@&123456789012345678> <@987654321098765432>"
ROLE = "123456789012345678"
CODE = "HOSTL-HOSTL-HOSTL-HOSTL-HOST1"
CODE_2 = "HOSTL-HOSTL-HOSTL-HOSTL-HOST2"


def assert_inert(message) -> None:
    mentions = message.allowed_mentions
    assert mentions is not None, "a send with no allowed_mentions at all falls back to the default"
    assert (mentions.everyone, mentions.roles, mentions.users, mentions.replied_user) == (
        False,
        False,
        False,
        False,
    )


def assert_text_is_defused(text: str) -> None:
    for token in ("@everyone", "@here", "<@&", "<@9"):
        assert token not in text, f"{token!r} survived in {text!r}"


def everything(world):
    return [m for m in world.everything_sent()]


# --- the audit: run the whole cutover morning, then look at every send ---


async def test_nothing_the_cutover_says_unprompted_can_ping(make_world, monkeypatch):
    world = await make_world(now=DUE - timedelta(minutes=1), owner_channel=True)
    stranger = guild_stub(888, name=HOSTILE, channels=[Chan(8881, 888)])
    owner = guild_stub(OWNER_GUILD, name=HOSTILE, channels=[Chan(8882, OWNER_GUILD)])
    fake_gateway(world.bot, monkeypatch, [stranger, owner])
    for game in ("borderlands4", "palworld"):
        add_item(world.db_path, game, "n", DUE - timedelta(hours=1), title=HOSTILE)

    await world.ready()  # the import notice, the sweep, reconciliation and its two hellos
    await world.tick(DUE)  # the friend's digest and its run report
    await world.bot._owner_report_job()  # the owner report

    sent = everything(world) + [
        m for g in (stranger, owner) for c in g.text_channels for m in c.sent
    ]
    assert len(sent) >= 5  # the audit really did see the notice, two hellos, a report and posts
    for message in sent:
        assert_inert(message)
        if message.content:
            assert_text_is_defused(message.content)


async def test_the_import_notice_is_plain_text_with_no_mentions(make_world):
    world = await make_world(now=DUE, owner_channel=True)

    await world.ready()

    (notice,) = [m for m in world.sent(OWNER_CH) if "Imported the v2 setup" in m.content]
    assert_inert(notice)


# --- hostile text through the paths that carry scraped or chosen strings ---


async def test_a_channel_error_that_says_everyone_is_defused_in_the_report_and_the_notice(
    make_world,
):
    world = await make_world(now=DUE - timedelta(minutes=1))
    world.channels[BL4_CH].fail_with = discord.Forbidden(
        type("R", (), {"status": 403, "reason": "Forbidden"})(), HOSTILE
    )
    for game in ("borderlands4", "palworld"):
        add_item(world.db_path, game, "n", DUE - timedelta(hours=1))
    await world.ready()

    await world.tick(DUE)

    admin = world.sent(FRIEND_ADMIN_CH)
    texts = [m.content for m in admin]
    assert any("borderlands4" in t or "Borderlands 4" in t for t in texts)  # something was said
    for message in admin:
        assert_inert(message)
        assert_text_is_defused(message.content)
    notices = [row[0] for row in world.rows("SELECT text FROM guild_notices")]
    assert notices and all("@everyone" not in text and "<@&" not in text for text in notices)


async def test_failing_sources_with_hostile_names_and_errors_are_defused_in_the_owner_report(
    make_world,
):
    world = await make_world(now=DUE, owner_channel=True)
    now = datetime.now(UTC)
    with closing(connect(world.db_path)) as conn:
        for name in (HOSTILE, "plain feed"):
            for _ in range(3):
                repo.record_source_result(
                    conn, name, now, f"boom {HOSTILE} http://x.test/?t=SECRET"
                )

    await world.bot._owner_report_job()

    (report,) = world.sent(OWNER_CH)
    assert_inert(report)
    assert_text_is_defused(report.content)
    assert "SECRET" not in report.content  # and the query string is cut, as before


@pytest.mark.parametrize("name", [HOSTILE, "@everyone", "‮@everyone", "`@everyone`"])
async def test_the_welcome_in_a_server_named_like_a_ping_pings_only_the_member(make_world, name):
    from types import SimpleNamespace

    world = await make_world(now=DUE)
    member = SimpleNamespace(
        guild=SimpleNamespace(id=FRIEND, name=name),
        id=42,
        bot=False,
        pending=False,
        mention="<@42>",
        flags=SimpleNamespace(value=0),
    )

    await world.bot.on_member_join(member)

    (message,) = [m for m in world.everything_sent() if m.content]
    assert message.allowed_mentions.everyone is False
    assert message.allowed_mentions.roles is False
    assert message.allowed_mentions.users == [member]


# --- the SHiFT matrix: the only place a ping is allowed to exist ---

G_ROLE, G_NONE, G_CAPPED = 2002, 2003, 2004


async def shift_world(make_world, *, ping_everyone_perm: bool = False):
    """The friend (everyone) plus a role server, a no-ping server and a capped server."""
    world = await make_world(now=T_SHIFT)
    mention_perm = (
        discord.Permissions.all()
        if ping_everyone_perm
        else discord.Permissions(view_channel=True, send_messages=True)
    )
    for gid in (G_ROLE, G_NONE, G_CAPPED):
        world.add_channel(
            gid * 100, gid, permissions=mention_perm if gid == G_ROLE else discord.Permissions.all()
        )
    add_guild(world.db_path, G_ROLE, ping=ROLE)
    add_guild(world.db_path, G_NONE, ping="none")
    add_guild(world.db_path, G_CAPPED, ping="everyone", ping_day="2026-10-01", ping_count=3)
    return world


def only(world, channel_id):
    (message,) = world.sent(channel_id)
    return message


async def test_each_server_gets_its_own_policy_and_nobody_elses(make_world, monkeypatch, tmp_path):
    world = await shift_world(make_world)
    collect_these(monkeypatch, tmp_path, [code_item("c1", CODE)])

    await world.bot._collection_job()

    friend, role, none, capped = (
        only(world, SHIFT_CH),
        only(world, G_ROLE * 100),
        only(world, G_NONE * 100),
        only(world, G_CAPPED * 100),
    )
    assert friend.allowed_mentions.everyone is True and friend.content.startswith("@everyone ")
    assert role.allowed_mentions.everyone is False
    assert [r.id for r in role.allowed_mentions.roles] == [int(ROLE)]
    assert role.content.startswith(f"<@&{ROLE}> ")
    for message in (none, capped):  # no choice made, and a choice whose budget is spent
        assert_inert(message)
        assert "@everyone" not in message.content and "<@&" not in message.content


async def test_one_servers_spent_cap_does_not_spend_or_save_anyone_elses(
    make_world, monkeypatch, tmp_path
):
    world = await shift_world(make_world)
    collect_these(monkeypatch, tmp_path, [code_item("c1", CODE)])

    await world.bot._collection_job()

    counts = dict(world.rows("SELECT guild_id, ping_count FROM guild_shift"))
    assert counts[FRIEND] == 1 and counts[G_CAPPED] == 3  # the capped server stays at its cap
    assert counts[G_NONE] == 0 and counts[G_ROLE] == 1


async def test_a_community_only_code_never_pings_even_where_everyone_is_chosen(
    make_world, monkeypatch, tmp_path
):
    world = await shift_world(make_world)
    collect_these(monkeypatch, tmp_path, [code_item("c1", CODE, trust="community")])

    await world.bot._collection_job()

    for channel in (SHIFT_CH, G_ROLE * 100, G_NONE * 100, G_CAPPED * 100):
        assert_inert(only(world, channel))


async def test_a_roundup_never_pings_and_never_spends_a_budget(make_world, monkeypatch, tmp_path):
    world = await shift_world(make_world)
    many = " ".join(f"RNDUP-RNDUP-RNDUP-RNDUP-RND{i:02d}" for i in range(7))  # past the 5 cap
    item = code_item("round", "X")
    item["full_text"] = f"Every code ever: {many}"
    collect_these(monkeypatch, tmp_path, [item])

    await world.bot._collection_job()

    for channel in (SHIFT_CH, G_ROLE * 100, G_NONE * 100):
        for message in world.sent(channel):
            assert_inert(message)
    assert dict(world.rows("SELECT guild_id, ping_count FROM guild_shift"))[FRIEND] == 0


async def test_a_server_that_never_chose_shift_hears_nothing(make_world, monkeypatch, tmp_path):
    world = await shift_world(make_world)
    add_guild(world.db_path, 2005, ping="everyone", enabled=False)
    add_guild(world.db_path, 2006, ping="everyone", games=("palworld",))  # follows no BL4
    world.add_channel(200500, 2005)
    world.add_channel(200600, 2006)
    collect_these(monkeypatch, tmp_path, [code_item("c1", CODE)])

    await world.bot._collection_job()

    assert world.sent(200500) == [] and world.sent(200600) == []


async def test_hostile_source_names_and_titles_cannot_add_a_second_ping(
    make_world, monkeypatch, tmp_path
):
    world = await shift_world(make_world)
    collect_these(
        monkeypatch,
        tmp_path,
        [code_item("c1", CODE, source=HOSTILE, title=f"{HOSTILE} new SHiFT code")],
    )

    await world.bot._collection_job()

    friend = only(world, SHIFT_CH)
    assert friend.content.count("@everyone") == 1  # the header's, which this server chose
    assert "@here" not in friend.content and "<@&" not in friend.content
    role = only(world, G_ROLE * 100)
    assert "@everyone" not in role.content and "@here" not in role.content
    assert role.content.count("<@&") == 1
    for channel in (G_NONE * 100, G_CAPPED * 100):
        assert_text_is_defused(only(world, channel).content)


async def test_a_missing_ping_permission_is_told_to_that_server_and_not_to_the_owner(
    make_world, monkeypatch, tmp_path
):
    world = await shift_world(make_world)
    # The role isn't mentionable (the stub guild knows no roles) and the role server's channel
    # lacks Mention @everyone: the alert posts anyway and says so to its own admins.
    collect_these(monkeypatch, tmp_path, [code_item("c1", CODE)])

    await world.bot._collection_job()

    notices = world.rows("SELECT guild_id, text FROM guild_notices")
    about_permission = [g for g, text in notices if "mentionable" in text]
    assert about_permission == [G_ROLE]
    assert [g for g, text in notices if "cap reached" in text] == [G_CAPPED]  # its own, too
    assert not any("mentionable" in (m.content or "") for m in world.sent(FRIEND_ADMIN_CH))


# --- the source tree, once more, for roles and users ---


def _allowed_mentions_sites() -> dict[tuple[str, str], list[str]]:
    """(file, enclosing function) of every `AllowedMentions(...)` that isn't all-False."""
    root = Path(__file__).parent.parent / "newsbot"
    sites: dict[tuple[str, str], list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "AllowedMentions":
                    continue
                for kw in node.keywords:
                    if kw.arg in ("roles", "users") and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False
                    ):
                        site = (str(path.relative_to(root)), func.name)
                        sites.setdefault(site, []).append(kw.arg)
    return sites


def test_only_the_shift_poster_and_the_welcome_can_name_a_role_or_a_user():
    assert set(_allowed_mentions_sites()) == {
        ("bot/client.py", "mentions_for"),  # the SHiFT poster's role choice
        ("bot/client.py", "_welcome_member"),  # the new member, and only them
    }


@pytest.mark.parametrize("bad", [None, 0, 12345678901234567, "", "everyone ", "EVERYONE", "role"])
def test_a_garbage_ping_value_in_the_database_pings_nobody(bad):
    mentions = mentions_for(bad)
    assert (mentions.everyone, mentions.roles, mentions.users) == (False, False, False)
