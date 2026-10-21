"""Adversarial round for the v3.1.1 owner alerts: hostile server names, a flaky alert path.

The first file (`test_lifecycle_owner_alerts.py`) pins the happy shapes. This one
leans on the seams: every Discord markup trick in a server name, names built to
blow the 2,000-character limit, joins and removals racing each other, an alert
that raises or hangs or has nowhere to go, and a log that must never learn a
server's name. Invisible characters are written as \\u escapes on purpose; a
test file full of characters you can't see is a test file nobody can review.

The bugs this round found (they were strict xfails until the v3.1.1 fixes) are
now plain tests. Three older ones were re-pinned when the alerts became
background tasks that start before the hello: each says so in its docstring.
"""

from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace

import discord
import pytest
from test_command_registration_v3 import make_bot
from test_lifecycle import G1, G2, G3, FakeChannel, _guild_ids, _seed, _tier
from test_lifecycle_owner_alerts import client_with, comped_life, named
from v3_fakes import load_v3_config

from newsbot.bot.client import GuildLifecycle
from newsbot.guilds import lifecycle
from newsbot.guilds.notify import Router
from newsbot.store import repo

SECRET = "Zebra Secret Club"  # noqa: S105
UID = "123456789012345678"


@pytest.fixture(autouse=True)
def _no_valve_pause(monkeypatch):
    monkeypatch.setattr(lifecycle, "VALVE_CHECK_PAUSE", 0)


@pytest.fixture
def cfg(monkeypatch):
    return load_v3_config(monkeypatch)


@pytest.fixture
def alerts():
    return []


@pytest.fixture
def life(v3_db, cfg, alerts):
    async def alert(text):
        alerts.append(text)

    return GuildLifecycle(v3_db, cfg, alert)


def u16(text: str) -> int:
    """Length the way Discord counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


# What Discord would turn into something live (a ping, a link, a timestamp, an
# embed) if it showed up in the owner's channel. Deliberately ASCII-only and
# deliberately blunt: the point is that nothing close to these survives.
LIVE = {
    "everyone/here": r"@(everyone|here)",
    "user/role mention": r"<@[!&]?\d+>",
    "channel mention": r"<#\d+>",
    "slash-command link": r"</[^>]+:\d+>",
    "custom emoji": r"<a?:\w+:\d+>",
    "timestamp": r"<t:-?\d+(?::[A-Za-z])?>",
    "guild navigation": r"<id:\w+>",
    "url scheme": r"(?i)(?:https?|steam|ftp|ftps|file|wss?)://",
    "invite": r"(?i)discord(?:app)?\.(?:gg|com/invite)/",
    "control or bidi": r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]",
}


def live_markup(text: str) -> list[str]:
    """Names of every live thing found in `text`. Empty means the text is inert."""
    found = [what for what, pattern in LIVE.items() if re.search(pattern, text)]
    # A markdown marker only formats when there's a second one to close it, so a
    # lone `*` is just a star. After the escapes are stripped, two of a kind is live.
    bare = re.sub(r"\\.", "", text, flags=re.S)
    found += [f"markdown {c!r}" for c in "*_~|`" if bare.count(c) >= 2]
    return found


HOSTILE = {
    "everyone": "@everyone",
    "here": "@here",
    "everyone in prose": "hey @everyone and @here, come look",
    "full-width at": "\uff20everyone \uff20here "
    "\uff20\uff45\uff56\uff45\uff52\uff59\uff4f\uff4e\uff45",
    "zero-width split": "@\u200beveryone @e\u200bveryone @\u2060here @\u200dhere",
    "NUL split": "@ever\x00yone @he\x00re @\x00everyone",
    "user mention": f"<@{UID}>",
    "nick mention": f"<@!{UID}>",
    "role mention": f"<@&{UID}>",
    "odd-length ids": "<@&12345678901234567> <@&1234567890123456789> <@&12345678901234567890>",
    "channel mention": f"<#{UID}>",
    "slash command": f"</shift codes:{UID}> </shift:{UID}> </a b c:{UID}>",
    "custom emoji": f"<:pepe:{UID}> <a:dance:{UID}>",
    "timestamps": "<t:1700000000> <t:1700000000:R> <t:1700000000:F> <t:-1:t>",
    "navigation": "<id:customize> <id:browse> <id:guide>",
    "masked link": "[Click here](https://evil.example/login)",
    "masked link on a url": "[https://discord.com](https://evil.example)",
    "bare urls": "https://evil.example/x HTTPS://EVIL.EXAMPLE steam://run/1 ftp://x.example",
    "suppressed embed": "<https://evil.example>",
    "invites": "discord.gg/abc DISCORD.GG/abc discord.com/invite/abc discordapp.com/invite/abc",
    "heading": "# Big heading",
    "heading three": "### h3\n## h2",
    "subtext": "-# tiny grey text",
    "list": "- item\n* item\n1. item",
    "quote": "> quote\n>>> block quote",
    "code fence": "```py\nprint(1)\n```",
    "inline code": "`code` ``a`b``",
    "spoiler": "||spoiler|| ||and another||",
    "emphasis": "***x*** __u__ ~~s~~ **b** *i* _i_",
    "backslash tricks": "\\*not bold\\* \\\\*real* \\\\\\*x\\*",
    "newlines": "line one\nline two\r\n\ttabbed\u2028sep\u2029para\x0bvt\x0cff",
    "bidi overrides": "\u202eevil\u202c \u202d\u202a\u202b \u2066\u2067\u2068\u2069 end",
    "control characters": "a\x00b\x07c\x1bd\x7fe\x85f\x9fg",
}
HOSTILE_PARAMS = [pytest.param(name, id=label) for label, name in HOSTILE.items()]


async def alert_text(path: str, name: str, v3_db: str, cfg) -> str:
    """The one owner alert produced when a server called `name` joins, leaves or is caught up on."""
    sent: list[str] = []

    async def alert(text):
        sent.append(text)

    life = GuildLifecycle(v3_db, cfg, alert)
    guild = named(G1, name)
    if path == "join":
        await life.on_guild_join(guild)
    elif path == "removal":
        _seed(v3_db, G1)
        await life.on_guild_remove(guild)
    else:
        await life.reconcile(client_with(guild))
    assert len(sent) == 1, sent
    return sent[0]


PATHS = ["join", "removal", "offline summary"]


# --- hostile names: nothing renders live ---


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name", HOSTILE_PARAMS)
async def test_a_hostile_name_renders_nothing_live(path, name, v3_db, cfg):
    text = await alert_text(path, name, v3_db, cfg)
    assert live_markup(text) == [], text


@pytest.mark.parametrize("name", HOSTILE_PARAMS)
async def test_a_hostile_name_is_still_inert_after_the_routers_own_cleanup(name, v3_db, cfg):
    """The real path is lifecycle then `Router.alert_owner`, which escapes and trims again."""
    sent: list[tuple[int, str]] = []

    async def send(channel_id, text):
        sent.append((channel_id, text))
        return True

    router = Router(v3_db, send, 555)
    await GuildLifecycle(v3_db, cfg, router.alert_owner).on_guild_join(named(G1, name))
    assert [c for c, _ in sent] == [555]
    assert live_markup(sent[0][1]) == [], sent[0][1]
    assert u16(sent[0][1]) <= 2000


@pytest.mark.parametrize("name", HOSTILE_PARAMS)
async def test_a_hostile_name_stays_on_one_line_and_in_its_place(name, v3_db, cfg):
    text = await alert_text("join", name, v3_db, cfg)
    assert text.startswith("Joined a new server: ")
    assert text.endswith(f" ({G1}), 12 members. Now in 1 server.")
    assert "\n" not in text


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(
            "_https://evil.example/login",
            id="underscore then https",
        ),
        pytest.param(
            "_http://evil.example",
            id="underscore then http",
        ),
        pytest.param(
            "_discord.gg/abc",
            id="underscore then invite",
        ),
        pytest.param(
            "_steam://run/1",
            id="underscore then steam",
        ),
    ],
)
def test_a_word_character_cannot_smuggle_a_live_scheme_past_the_label(tail):
    label = lifecycle.guild_label(named(G1, tail))
    assert live_markup(label) == [], label


@pytest.mark.parametrize("bidi", list("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))
def test_every_bidi_override_and_isolate_is_gone_from_the_label(bidi):
    label = lifecycle.guild_label(named(G1, f"before{bidi}after"))
    assert bidi not in label
    assert label == f"before after ({G1})"


# --- hostile names: nothing to show ---


@pytest.mark.parametrize(
    "blank",
    [
        " ",
        "\t\n\r",
        "\u00a0",
        "\u3000",
        "\u2028\u2029",
        "\x85",
        "\u1680",
        "\u202f",
        " \u00a0\u3000 ",
        "\x00",
        "\x1b\x7f",
        "\u202e",
        "\u2066\u2069",
        "\x00 \u202e \x85",
    ],
)
def test_a_name_of_only_whitespace_or_control_characters_shows_as_unnamed(blank):
    assert lifecycle.guild_label(named(G1, blank)) == f"(unnamed) ({G1})"


@pytest.mark.parametrize("blank", [None, "", 0])
def test_a_missing_or_empty_name_shows_as_unnamed(blank):
    guild = named(G1, blank)
    assert lifecycle.guild_label(guild) == f"(unnamed) ({G1})"


@pytest.mark.parametrize(
    "invisible",
    [
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
        "\u3164",
        "\u2800",
        "\u115f",
        "\uffa0",
        "\u180e",
        "\u200e",
        "\u200f",
        "\u061c",
        "\u00ad",
        "\u034f",
        "\u17b4",
        "\u2062",
        "\U000e0041\U000e0042",
        "\u200b \u200b\u2060",
    ],
)
def test_a_name_of_only_invisible_characters_shows_as_unnamed(invisible):
    assert lifecycle.guild_label(named(G1, invisible)) == f"(unnamed) ({G1})"


# --- length: the 100-character boundary, long names, the 2,000 limit ---


@pytest.mark.parametrize(
    ("n", "shown"),
    [
        (1, "x"),
        (99, "x" * 99),
        (100, "x" * 100),
        (101, "x" * 99 + "\u2026"),
        (2000, "x" * 99 + "\u2026"),
    ],
)
def test_the_name_cap_boundary(n, shown):
    assert lifecycle.guild_label(named(G1, "x" * n)) == f"{shown} ({G1})"


def test_padding_around_a_hundred_characters_does_not_cost_the_name_its_ending():
    assert lifecycle.guild_label(named(G1, "  " + "x" * 100 + "\n\t ")) == f"{'x' * 100} ({G1})"


def test_the_cut_does_not_leave_a_space_in_front_of_the_ellipsis():
    # 98 x, a space, then a long word: the cut lands right after the space.
    label = lifecycle.guild_label(named(G1, "x" * 98 + " " + "y" * 50))
    assert label == f"{'x' * 98}\u2026 ({G1})"


def test_a_hundred_astral_characters_are_kept_whole():
    name = "\U0001f600" * 100
    label = lifecycle.guild_label(named(G1, name))
    assert label == f"{name} ({G1})"
    label.encode("utf-8")  # no half of a pair left behind


def test_a_hundred_and_one_astral_characters_are_cut_on_a_character():
    label = lifecycle.guild_label(named(G1, "\U0001f600" * 101))
    assert label == f"{'\U0001f600' * 99}\u2026 ({G1})"


LONG_SHAPES = {
    "plain": "x",
    "stars": "*",
    "underscores": "_",
    "pipes": "|",
    "backticks": "`",
    "tildes": "~",
    "backslashes": "\\",
    "channel mentions": "<#",
    "mentions": f"<@&{UID}>",
    "everyone": "@everyone ",
    "emoji": "\U0001f600",
    "links": "https://evil.example/",
    "invites": "discord.gg/abc ",
}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("unit", list(LONG_SHAPES.values()), ids=list(LONG_SHAPES))
async def test_a_two_thousand_character_name_cannot_push_one_alert_past_discords_limit(
    path, unit, v3_db, cfg
):
    name = (unit * (2000 // len(unit) + 1))[:2000]
    assert len(name) == 2000
    text = await alert_text(path, name, v3_db, cfg)
    assert u16(text) <= 2000
    assert live_markup(text) == []


def _ten(name: str):
    ids = [10**18 + i for i in range(10)]  # nineteen digits, like the real ones
    return ids, [named(i, name) for i in ids]


@pytest.mark.parametrize(
    "unit",
    [
        pytest.param("x", id="plain"),
        pytest.param("@", id="at signs"),
        pytest.param("<#", id="channel openers"),
        pytest.param(" a", id="words"),
        pytest.param(
            "*",
            id="stars",
        ),
        pytest.param(
            "\U0001f600",
            id="emoji",
        ),
    ],
)
async def test_the_startup_summary_with_ten_full_length_names_fits_in_discords_limit(
    unit, v3_db, cfg, alerts
):
    """BUG: NAME_CAP trims to 100 characters, then `esc` runs and can double the name
    (every `*`, `_`, `|`, backtick, tilde and backslash gains a backslash) and emoji
    count twice in UTF-16. Ten such names plus ' (id, comped)' run to about 2,300
    units, past the 2,000 limit; `Router._safe_text` then cuts the message, and the
    'Now in N servers.' tail goes with it. A single join alert is fine (the previous
    test); only the ten-name summary can overflow. The name should be capped after
    escaping, or the list shortened until the whole message fits.
    """
    name = (unit * 101)[:100]
    ids, guilds = _ten(name)
    life = comped_life(v3_db, cfg, alerts, *ids)
    await life.reconcile(client_with(*guilds))
    assert len(alerts) == 1
    assert u16(alerts[0]) <= 2000, u16(alerts[0])
    assert alerts[0].endswith("Now in 10 servers.")


async def test_that_overflowing_summary_still_reaches_discord_inside_the_limit(v3_db, cfg):
    """Whatever the lifecycle sends, the router's trim keeps the real message legal."""
    sent: list[str] = []

    async def send(channel_id, text):
        sent.append(text)
        return True

    ids, guilds = _ten("*" * 100)
    router = Router(v3_db, send, 555)
    life = GuildLifecycle(
        v3_db, cfg.model_copy(update={"comped_guild_ids": ids}), router.alert_owner
    )
    await life.reconcile(client_with(*guilds))
    assert len(sent) == 1 and u16(sent[0]) <= 2000
    assert live_markup(sent[0]) == []


async def test_the_offline_removal_summary_lists_ten_ids_and_counts_the_rest(v3_db, life, alerts):
    rows = list(range(7000, 7400))
    _seed(v3_db, *rows)
    await life.reconcile(client_with(*(named(i) for i in rows[30:])))
    assert len(alerts) == 1
    listed = re.findall(r"\b7\d{3}\b", alerts[0].split(" Now in")[0])
    assert len(listed) == 10
    assert ", and 20 more. Now in 370 servers." in alerts[0]
    assert u16(alerts[0]) <= 2000


async def test_eleven_offline_joins_say_one_more(life, alerts):
    ids = range(5000, 5011)
    await life.reconcile(client_with(*(named(i, f"S{i}") for i in ids)))
    assert alerts[0].endswith(", and 1 more. Now in 11 servers.")


# --- surrogates ---


LONE_SURROGATES = ["ab\ud800cd", "\ude00", "\ud83d", "x\udbff\udfffy\udc00"]


@pytest.mark.parametrize("name", LONE_SURROGATES)
async def test_a_lone_surrogate_in_a_name_does_not_make_the_alert_unencodable(name, v3_db, cfg):
    text = await alert_text("join", name, v3_db, cfg)
    text.encode("utf-8")
    text.encode("utf-16-le")


async def test_a_lone_surrogate_in_a_name_still_reaches_the_owner(v3_db, cfg):
    sent: list[str] = []

    async def send(channel_id, text):
        sent.append(text)
        return True

    router = Router(v3_db, send, 555)
    await GuildLifecycle(v3_db, cfg, router.alert_owner).on_guild_join(named(G1, "ab\ud800cd"))
    assert len(sent) == 1


# --- shared fakes for the races and the failure drills ---


class GatedChannel(FakeChannel):
    """A hello that waits at a gate, and says so when it gets there."""

    def __init__(self, channel_id, gate, **kw):
        super().__init__(channel_id, **kw)
        self.gate = gate
        self.waiting = asyncio.Event()

    async def send(self, content=None, **kwargs):
        self.waiting.set()
        await self.gate.wait()
        await super().send(content, **kwargs)


class YieldingChannel(FakeChannel):
    """A hello that takes a few trips round the event loop, so joins interleave."""

    async def send(self, content=None, **kwargs):
        for _ in range(3):
            await asyncio.sleep(0)
        await super().send(content, **kwargs)


class HungAlert:
    """An owner alert that starts, says so, and then waits until it is let go."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.texts: list[str] = []

    async def __call__(self, text):
        self.texts.append(text)
        self.started.set()
        await self.release.wait()


def counts(alerts):
    return [int(m.group(1)) for a in alerts if (m := re.search(r"Now in (\d+) server", a))]


# --- counting ---


async def test_five_joins_at_once_each_get_one_alert_and_an_honest_count(life, alerts, v3_db):
    ids = [G1, G2, G3, 9004, 9005]
    guilds = [named(i, f"S{i}", channels=[YieldingChannel(1)]) for i in ids]
    results = await asyncio.gather(*(life.on_guild_join(g) for g in guilds))
    assert results == [True] * 5
    assert sorted(_guild_ids(v3_db)) == sorted(ids)
    assert len(alerts) == 5
    assert all(any(f"S{i} ({i})" in a for a in alerts) for i in ids)
    # Every count is a count of rows that existed when it was written: never zero,
    # never more than there are, and the last one standing sees them all.
    assert all(1 <= c <= 5 for c in counts(alerts))
    assert counts(alerts)[-1] == 5


async def test_a_join_that_finishes_while_another_waits_on_its_hello_counts_both(life, alerts):
    """Each alert is composed at its own insert, so A says one server and B says two.

    (Before v3.1.1 A's alert waited for its hello and said two. Taking the count
    at the insert is what keeps a removal alert from overtaking its join alert.)
    """
    gate = asyncio.Event()
    waiting = GatedChannel(1, gate)
    slow = asyncio.create_task(life.on_guild_join(named(G1, "A", channels=[waiting])))
    await asyncio.wait_for(waiting.waiting.wait(), 1)
    assert alerts == [f"Joined a new server: A ({G1}), 12 members. Now in 1 server."]

    assert await life.on_guild_join(named(G2, "B")) is True
    assert alerts[1] == f"Joined a new server: B ({G2}), 12 members. Now in 2 servers."

    gate.set()
    await slow
    assert len(alerts) == 2


async def test_a_join_during_reconcile_is_announced_alone_and_counted_in_the_summary(
    life, alerts, v3_db
):
    gate = asyncio.Event()
    waiting = GatedChannel(1, gate)
    client = client_with(named(G1, "A", channels=[waiting]), named(G2, "B"))
    recon = asyncio.create_task(life.reconcile(client))
    await asyncio.wait_for(waiting.waiting.wait(), 1)

    assert await life.on_guild_join(named(G3, "Late")) is True
    assert alerts == [f"Joined a new server: Late ({G3}), 12 members. Now in 2 servers."]

    gate.set()
    await recon
    assert len(alerts) == 2
    assert alerts[1] == f"Joined while I was offline: A ({G1}), B ({G2}). Now in 3 servers."
    assert "Late" not in alerts[1]
    assert sorted(_guild_ids(v3_db)) == [G1, G2, G3]


async def test_a_server_the_gateway_announces_mid_reconcile_is_not_announced_again(
    life, alerts, v3_db
):
    gate = asyncio.Event()
    waiting = GatedChannel(1, gate)
    client = client_with(named(G1, "A", channels=[waiting]), named(G2, "B"))
    recon = asyncio.create_task(life.reconcile(client))
    await asyncio.wait_for(waiting.waiting.wait(), 1)

    # G2 is in reconcile's plan but reconcile hasn't reached it; the gateway does.
    assert await life.on_guild_join(named(G2, "B")) is True
    gate.set()
    await recon

    assert [a.split(":")[0] for a in alerts] == [
        "Joined a new server",
        "Joined while I was offline",
    ]
    assert f"B ({G2})" not in alerts[1]
    assert f"A ({G1})" in alerts[1]


async def test_a_gateway_replay_of_a_server_reconcile_is_creating_adds_no_second_alert(
    life, alerts
):
    gate = asyncio.Event()
    waiting = GatedChannel(1, gate)
    guild = named(G1, "A", channels=[waiting])
    recon = asyncio.create_task(life.reconcile(client_with(guild)))
    await asyncio.wait_for(waiting.waiting.wait(), 1)

    assert await life.on_guild_join(guild) is False
    gate.set()
    await recon
    assert alerts == [f"Joined while I was offline: A ({G1}). Now in 1 server."]


async def test_comped_servers_listed_in_config_but_not_joined_are_not_counted(v3_db, cfg, alerts):
    life = comped_life(v3_db, cfg, alerts, G2, G3)
    await life.on_guild_join(named(G1))
    assert alerts == [f"Joined a new server: Tavern ({G1}), 12 members. Now in 1 server."]

    await life.reconcile(client_with(named(G1)))
    assert _guild_ids(v3_db) == [G1]  # comping never invents a row
    assert len(alerts) == 1

    await life.on_guild_join(named(G2, "Friend"))
    assert alerts[1] == f"Joined a new server: Friend ({G2}), 12 members. Now in 2 servers; comped."
    assert _tier(v3_db, G2) == "comped"


async def test_a_listed_server_that_joins_while_the_bot_is_down_is_marked_comped_in_the_summary(
    v3_db, cfg, alerts
):
    life = comped_life(v3_db, cfg, alerts, G2, 424242)
    await life.reconcile(client_with(named(G1, "Free"), named(G2, "Friend")))
    assert alerts == [
        f"Joined while I was offline: Free ({G1}), Friend ({G2}, comped). Now in 2 servers."
    ]


async def test_a_rejoin_after_a_removal_alerts_twice_with_counts_that_follow(life, alerts, v3_db):
    guild = named(G1, "Tavern")
    _seed(v3_db, G2)
    await life.on_guild_join(guild)
    await life.on_guild_remove(guild)
    await life.on_guild_join(guild)
    assert alerts == [
        f"Joined a new server: Tavern ({G1}), 12 members. Now in 2 servers.",
        f"Removed from Tavern ({G1}). Its settings were deleted. Now in 1 server.",
        f"Joined a new server: Tavern ({G1}), 12 members. Now in 2 servers.",
    ]


async def test_removing_the_home_server_deletes_only_its_row_and_still_tells_the_owner(
    v3_db, cfg, caplog
):
    """The owner channel lives in the home server, so the send can't land; the bot must shrug."""
    calls: list[tuple[int, int, str]] = []

    class Sender:
        async def __call__(self, channel_id, text):
            raise AssertionError("an owner alert must use the guild-checked door")

        async def in_guild(self, guild_id, channel_id, text, *, unverified_ok=False):
            calls.append((guild_id, channel_id, text))
            return False  # the bot is out of that server; Discord can't place the channel

    caplog.set_level(logging.DEBUG)
    home_cfg = cfg.model_copy(update={"home_guild_id": G1})
    router = Router(v3_db, Sender(), 555, home_guild_id=G1)
    _seed(v3_db, G1, G2)

    await GuildLifecycle(v3_db, home_cfg, router.alert_owner).on_guild_remove(named(G1, SECRET))

    assert _guild_ids(v3_db) == [G2]
    assert [(g, c) for g, c, _ in calls] == [(G1, 555)]
    assert (
        calls[0][2] == f"Removed from {SECRET} ({G1}). Its settings were deleted. Now in 1 server."
    )
    assert "Zebra" not in caplog.text


async def test_a_failed_count_drops_the_count_and_keeps_the_alert(life, alerts, monkeypatch, v3_db):
    def boom(conn):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "list_guilds", boom)
    await life.on_guild_join(named(G1))
    _seed(v3_db, G2)
    await life.on_guild_remove(named(G2))
    assert alerts == [
        f"Joined a new server: Tavern ({G1}), 12 members.",
        f"Removed from Tavern ({G2}). Its settings were deleted.",
    ]


async def test_a_join_alert_never_arrives_after_that_servers_removal_alert(life, alerts, v3_db):
    gate = asyncio.Event()
    waiting = GatedChannel(1, gate)
    guild = named(G1, "Fleeting", channels=[waiting])
    joining = asyncio.create_task(life.on_guild_join(guild))
    await asyncio.wait_for(waiting.waiting.wait(), 1)
    await life.on_guild_remove(guild)
    gate.set()
    await joining

    kinds = [a.split()[0] for a in alerts]
    assert kinds != ["Removed", "Joined"], alerts


# --- duplicates ---


async def test_three_replays_of_one_join_at_once_make_one_alert_and_one_hello(life, alerts):
    ch = YieldingChannel(1)
    guild = named(G1, channels=[ch])
    results = await asyncio.gather(*(life.on_guild_join(guild) for _ in range(3)))
    assert sorted(results) == [False, False, True]
    assert len(alerts) == 1
    assert len(ch.sent) == 1


async def test_a_replayed_join_on_the_real_client_alerts_once(v3_cfg, v3_db, monkeypatch):
    from newsbot.bot import registration

    async def no_resync(bot, guild_id):
        return "unchanged"

    monkeypatch.setattr(registration, "resync_guild_commands", no_resync)
    bot = make_bot(v3_cfg, v3_db)
    sent: list[str] = []

    async def alert(text):
        sent.append(text)

    bot.lifecycle._alert_owner = alert
    guild = named(G1, "Tavern")
    await bot.on_guild_join(guild)
    await bot.on_guild_join(guild)  # the gateway resumed and replayed it
    assert len(sent) == 1


async def test_a_replayed_join_for_a_listed_free_row_comps_it_without_an_alert(v3_db, cfg, alerts):
    _seed(v3_db, G1)  # a free row, as an older start would have left it
    life = comped_life(v3_db, cfg, alerts, G1)
    assert await life.on_guild_join(named(G1)) is False
    assert alerts == []
    assert _tier(v3_db, G1) == "comped"


async def test_reconcile_after_reconcile_has_nothing_to_say(life, alerts):
    client = client_with(named(G1), named(G2))
    await life.reconcile(client)
    assert len(alerts) == 1
    await life.reconcile(client)
    await life.reconcile(client)
    assert len(alerts) == 1


async def test_a_restart_where_everything_matches_sends_nothing_even_when_comping_changes_a_row(
    v3_db, cfg, alerts
):
    _seed(v3_db, G1, G2)
    life = comped_life(v3_db, cfg, alerts, G2)
    await life.reconcile(client_with(named(G1), named(G2)))
    assert alerts == []
    assert _tier(v3_db, G2) == "comped"


# --- failure isolation: the alert breaks, the join still happens ---


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Access")


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("channel gone"),
        _forbidden(),
        OSError("connection reset"),
        TimeoutError(),
        ValueError(),
        KeyError("owner"),
    ],
    ids=["runtime", "forbidden", "oserror", "timeout", "bare valueerror", "keyerror"],
)
async def test_whatever_the_alert_raises_the_join_still_happens(error, v3_db, cfg):
    async def alert(text):
        raise error

    ch = FakeChannel(1)
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), alert)
    assert await life.on_guild_join(named(G1, channels=[ch])) is True
    assert _guild_ids(v3_db) == [G1]
    assert _tier(v3_db, G1) == "comped"
    assert len(ch.sent) == 1


async def test_a_hung_alert_lets_the_join_return_after_the_timeout_and_nothing_else_is_lost(
    v3_db, cfg, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)
    hung = HungAlert()
    ch = FakeChannel(1)
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), hung)
    assert await asyncio.wait_for(life.on_guild_join(named(G1, channels=[ch])), 2) is True
    assert hung.started.is_set()
    assert len(ch.sent) == 1
    assert _tier(v3_db, G1) == "comped"


async def test_the_owner_alert_starts_before_the_hello_and_neither_waits_for_the_other(v3_db, cfg):
    """Re-pinned in v3.1.1: the alert is composed at the insert and started ahead of the hello."""
    order: list[str] = []

    class Ch(FakeChannel):
        async def send(self, content=None, **kwargs):
            order.append("hello")

    async def alert(text):
        order.append("alert")

    await GuildLifecycle(v3_db, cfg, alert).on_guild_join(named(G1, channels=[Ch(1)]))
    assert order == ["alert", "hello"]


async def test_a_hung_alert_does_not_hold_back_the_hello(v3_db, cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 30)
    hung = HungAlert()
    ch = FakeChannel(1)
    task = asyncio.create_task(
        GuildLifecycle(v3_db, cfg, hung).on_guild_join(named(G1, channels=[ch]))
    )
    try:
        await asyncio.wait_for(hung.started.wait(), 1)
        assert len(ch.sent) == 1
    finally:
        hung.release.set()
        await task


async def test_a_join_does_not_wait_for_its_alert_and_a_cancelled_alert_costs_nothing(
    v3_db, cfg, monkeypatch
):
    """Re-pinned in v3.1.1: the join returns at once; shutdown cancels the alert, not the join."""
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 30)
    hung = HungAlert()
    life = GuildLifecycle(v3_db, cfg, hung)
    assert await asyncio.wait_for(life.on_guild_join(named(G1)), 1) is True
    assert hung.started.is_set()
    (pending,) = life._alert_tasks
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert pending.cancelled()
    assert not life._alert_tasks
    assert _guild_ids(v3_db) == [G1]


@pytest.mark.parametrize("path", PATHS)
async def test_a_router_with_no_owner_channel_loses_the_alert_and_nothing_else(v3_db, cfg, path):
    sent: list = []

    async def send(channel_id, text):
        sent.append(text)
        return True

    router = Router(v3_db, send, None)
    life = GuildLifecycle(
        v3_db, cfg.model_copy(update={"comped_guild_ids": [G1]}), router.alert_owner
    )
    guild = named(G1, "Tavern", channels=[FakeChannel(1)])
    if path == "join":
        assert await life.on_guild_join(guild) is True
        assert len(guild.text_channels[0].sent) == 1
    elif path == "removal":
        _seed(v3_db, G1)
        await life.on_guild_remove(guild)
    else:
        await life.reconcile(client_with(guild))
    assert sent == []
    assert _guild_ids(v3_db) == ([] if path == "removal" else [G1])
    if path != "removal":
        assert _tier(v3_db, G1) == "comped"


async def test_reconcile_still_creates_and_comps_when_the_summary_raises(v3_db, cfg):
    async def alert(text):
        raise RuntimeError("nope")

    _seed(v3_db, G2)
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G1, G2]}), alert)
    plan = await life.reconcile(client_with(named(G1), named(G2)))
    assert plan.to_create == (G1,)
    assert sorted(_guild_ids(v3_db)) == [G1, G2]
    assert _tier(v3_db, G1) == _tier(v3_db, G2) == "comped"


async def test_comping_is_finished_before_the_startup_summary_goes_out(v3_db, cfg):
    seen: dict[str, str] = {}

    async def alert(text):
        seen["tier"] = _tier(v3_db, G2)

    _seed(v3_db, G2)
    life = GuildLifecycle(v3_db, cfg.model_copy(update={"comped_guild_ids": [G2]}), alert)
    await life.reconcile(client_with(named(G1), named(G2)))
    assert seen == {"tier": "comped"}


async def test_a_hung_summary_times_out_and_reconcile_still_returns_its_plan(
    v3_db, cfg, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)
    life = GuildLifecycle(v3_db, cfg, HungAlert())
    _seed(v3_db, G2, G3)  # G3 stays, so the valve (which has its own alert) stays shut
    plan = await asyncio.wait_for(life.reconcile(client_with(named(G1), named(G3))), 2)
    assert plan.to_create == (G1,) and plan.to_delete == (G2,)
    assert sorted(_guild_ids(v3_db)) == [G1, G3]


async def test_a_hung_startup_summary_does_not_hold_up_reconcile(v3_db, cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 30)
    hung = HungAlert()
    life = GuildLifecycle(v3_db, cfg, hung)
    task = asyncio.create_task(life.reconcile(client_with(named(G1))))
    try:
        await asyncio.wait_for(hung.started.wait(), 1)
        await asyncio.wait_for(asyncio.shield(task), 0.5)
    finally:
        hung.release.set()
        await task


class StrangeGuild:
    """A guild whose name blows up when read: not real, but guild_label promises not to care."""

    id = G1
    me = "bot"
    system_channel = None
    text_channels: list = []
    member_count = 3

    @property
    def name(self):
        raise RuntimeError("cache went away")


@pytest.mark.parametrize("path", ["join", "removal"])
async def test_a_guild_that_cannot_describe_itself_does_not_break_the_join_or_removal(
    path, life, v3_db
):
    if path == "join":
        assert await life.on_guild_join(StrangeGuild()) is True
    else:
        _seed(v3_db, G1)
        await life.on_guild_remove(StrangeGuild())
    assert _guild_ids(v3_db) == ([G1] if path == "join" else [])


# --- failure isolation on the real client: order of the work after the alert ---


@pytest.fixture
def registration_spy(monkeypatch):
    from newsbot.bot import registration

    spy = SimpleNamespace(resynced=asyncio.Event(), forgotten=asyncio.Event())

    async def resync(bot, guild_id):
        spy.resynced.set()
        return "synced"

    async def forget(bot, guild_id):
        spy.forgotten.set()

    monkeypatch.setattr(registration, "resync_guild_commands", resync)
    monkeypatch.setattr(registration, "forget_guild_commands", forget)
    return spy


@pytest.mark.parametrize(
    "failure",
    ["raises", "hangs"],
)
async def test_the_command_resync_and_comping_survive_a_broken_alert(
    failure, v3_cfg, v3_db, registration_spy, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)
    bot = make_bot(v3_cfg.model_copy(update={"comped_guild_ids": [G1]}), v3_db)

    async def boom(text):
        raise RuntimeError("channel gone")

    bot.lifecycle._alert_owner = boom if failure == "raises" else HungAlert()
    ch = FakeChannel(1)
    await asyncio.wait_for(bot.on_guild_join(named(G1, channels=[ch])), 2)
    assert registration_spy.resynced.is_set()
    assert len(ch.sent) == 1
    assert _tier(v3_db, G1) == "comped"


@pytest.mark.parametrize("failure", ["raises", "hangs"])
async def test_the_removal_cleanup_survives_a_broken_alert(
    failure, v3_cfg, v3_db, registration_spy, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)
    bot = make_bot(v3_cfg, v3_db)

    async def boom(text):
        raise RuntimeError("channel gone")

    bot.lifecycle._alert_owner = boom if failure == "raises" else HungAlert()
    _seed(v3_db, G1)
    await asyncio.wait_for(bot.on_guild_remove(named(G1)), 2)
    assert registration_spy.forgotten.is_set()
    assert _guild_ids(v3_db) == []


async def test_a_slow_owner_alert_does_not_delay_the_command_resync(
    v3_cfg, v3_db, registration_spy, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 30)
    bot = make_bot(v3_cfg, v3_db)
    hung = HungAlert()
    bot.lifecycle._alert_owner = hung
    task = asyncio.create_task(bot.on_guild_join(named(G1, channels=[FakeChannel(1)])))
    try:
        await asyncio.wait_for(registration_spy.resynced.wait(), 0.5)
    finally:
        hung.release.set()
        await task


async def test_a_slow_owner_alert_does_not_delay_the_removal_cleanup(
    v3_cfg, v3_db, registration_spy, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 30)
    bot = make_bot(v3_cfg, v3_db)
    removed_jobs: list[str] = []
    bot.scheduler = SimpleNamespace(
        get_job=lambda job_id: object(), remove_job=lambda job_id: removed_jobs.append(job_id)
    )
    hung = HungAlert()
    bot.lifecycle._alert_owner = hung
    _seed(v3_db, G1)
    task = asyncio.create_task(bot.on_guild_remove(named(G1)))
    try:
        await asyncio.wait_for(hung.started.wait(), 1)
        await asyncio.wait_for(registration_spy.forgotten.wait(), 0.5)
        assert removed_jobs == [f"daily-quote-{G1}"]
    finally:
        hung.release.set()
        await task


async def test_a_removal_with_a_working_alert_still_removes_the_quote_job(
    v3_cfg, v3_db, registration_spy
):
    bot = make_bot(v3_cfg, v3_db)
    removed_jobs: list[str] = []
    bot.scheduler = SimpleNamespace(
        get_job=lambda job_id: object(), remove_job=lambda job_id: removed_jobs.append(job_id)
    )
    sent: list[str] = []

    async def alert(text):
        sent.append(text)

    bot.lifecycle._alert_owner = alert
    _seed(v3_db, G1)
    await bot.on_guild_remove(named(G1))
    assert removed_jobs == [f"daily-quote-{G1}"]
    assert len(sent) == 1


# --- logs: a server's name belongs to the owner's alert and to nothing else ---


NEEDLES = ("Zebra", "Secret Club")


def leaked(caplog) -> list[str]:
    """Which fragments of the secret name appear anywhere in anything that was logged."""
    fmt = logging.Formatter()
    blobs = [caplog.text]
    for record in caplog.records:
        # The formatted line with its traceback, the message, and every attribute
        # (args, extras, the exception object's repr): leaks hide in all of them.
        blobs += [fmt.format(record), record.getMessage(), repr(vars(record))]
    return [n for n in NEEDLES if any(n.lower() in b.lower() for b in blobs)]


async def _every_path(life, v3_db, *, hello_fails=False):
    """Join, remove, then a restart that catches up on one join and one removal."""
    await life.on_guild_join(named(G1, SECRET, channels=[FakeChannel(1, fail=hello_fails)]))
    await life.on_guild_remove(named(G1, SECRET))
    _seed(v3_db, G3, 9004)
    await life.reconcile(
        client_with(named(G2, SECRET, channels=[FakeChannel(2, fail=hello_fails)]), named(9004))
    )


@pytest.mark.parametrize(
    "behaviour",
    ["ok", "runtime error", "forbidden", "oserror", "timeout error", "hangs", "hello fails"],
)
async def test_the_name_never_reaches_a_log_record_on_any_lifecycle_path(
    behaviour, v3_db, cfg, caplog, monkeypatch
):
    monkeypatch.setattr(lifecycle, "HELLO_TIMEOUT", 0.05)

    async def alert(text):
        if behaviour == "runtime error":
            raise RuntimeError("channel gone")
        if behaviour == "forbidden":
            raise _forbidden()
        if behaviour == "oserror":
            raise OSError("connection reset by peer")
        if behaviour == "timeout error":
            raise TimeoutError
        if behaviour == "hangs":
            await asyncio.sleep(30)

    caplog.set_level(logging.DEBUG)
    await _every_path(
        GuildLifecycle(v3_db, cfg, alert), v3_db, hello_fails=behaviour == "hello fails"
    )
    assert caplog.records, "the drill should have logged something, or this test proves nothing"
    assert leaked(caplog) == []


async def test_the_name_stays_out_of_the_log_when_the_count_fails_too(
    life, v3_db, caplog, monkeypatch
):
    def boom(conn):
        raise sqlite_error()

    monkeypatch.setattr(repo, "list_guilds", boom)
    caplog.set_level(logging.DEBUG)
    await life.on_guild_join(named(G1, SECRET))
    await life.on_guild_remove(named(G1, SECRET))
    assert "could not count" in caplog.text
    assert leaked(caplog) == []


def sqlite_error():
    import sqlite3

    return sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize("sender", ["raises", "returns false", "hangs", "wrong guild"])
async def test_the_name_never_reaches_a_log_record_on_the_router_paths(
    sender, v3_db, cfg, caplog, monkeypatch
):
    from newsbot.guilds import notify

    monkeypatch.setattr(notify, "_SEND_TIMEOUT_S", 0.05)

    class Send:
        async def __call__(self, channel_id, text):
            if sender == "raises":
                raise RuntimeError("discord is having a day")
            if sender == "hangs":
                await asyncio.sleep(30)
            return False

    class GuildCheckedSend(Send):
        async def in_guild(self, guild_id, channel_id, text, *, unverified_ok=False):
            return False

    router = Router(
        v3_db, GuildCheckedSend() if sender == "wrong guild" else Send(), 555, home_guild_id=G3
    )
    caplog.set_level(logging.DEBUG)
    await _every_path(GuildLifecycle(v3_db, cfg, router.alert_owner), v3_db)
    assert caplog.records
    assert leaked(caplog) == []


@pytest.mark.parametrize("path", PATHS)
async def test_a_router_with_no_owner_channel_does_not_log_the_server_name(
    path, v3_db, cfg, caplog
):
    async def send(channel_id, text):
        return True

    caplog.set_level(logging.DEBUG)
    router = Router(v3_db, send, None)
    life = GuildLifecycle(v3_db, cfg, router.alert_owner)
    guild = named(G1, SECRET)
    if path == "join":
        await life.on_guild_join(guild)
    elif path == "removal":
        _seed(v3_db, G1)
        await life.on_guild_remove(guild)
    else:
        await life.reconcile(client_with(guild))
    assert "no owner channel" in caplog.text
    assert leaked(caplog) == []


async def test_an_alert_error_that_quotes_the_text_does_not_put_the_name_in_the_log(
    v3_db, cfg, caplog
):
    async def alert(text):
        raise RuntimeError(f"could not send {text!r}")

    caplog.set_level(logging.DEBUG)
    await GuildLifecycle(v3_db, cfg, alert).on_guild_join(named(G1, SECRET))
    assert "owner alert failed" in caplog.text
    assert leaked(caplog) == []


async def test_a_sender_error_that_quotes_the_text_does_not_put_the_name_in_the_log(
    v3_db, cfg, caplog
):
    async def send(channel_id, text):
        raise RuntimeError(f"bad payload {text!r}")

    caplog.set_level(logging.DEBUG)
    router = Router(v3_db, send, 555)
    await GuildLifecycle(v3_db, cfg, router.alert_owner).on_guild_join(named(G1, SECRET))
    assert "couldn't send a routed message" in caplog.text
    assert leaked(caplog) == []
