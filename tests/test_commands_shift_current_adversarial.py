"""Adversarial round on `/shift codes` and `/shift history`, through the handlers (design.md §16).

`test_shift_current_sql_adversarial.py` attacks the two queries. This file attacks everything
around them: who may run the commands and what they hear when they can't, what the pages do with
hostile text, how big things get (5,000 current codes, a 90-day history with 10,000), the pager
buttons, the registration payload Discord will validate, and a reader running against the hook.

The defects this round found (an unclamped pager, a stale `days`, a torn read, unreadable dates,
an over-long via name) are fixed and pinned as ordinary tests. Invisible and non-ASCII characters
are written as escapes.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
import tracemalloc
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from pydantic import SecretStr
from v3_fakes import GUILD_A, FakeInteraction, command, make_guild

from newsbot.bot import commands as commands_module
from newsbot.bot.client import NewsBot
from newsbot.bot.commands import _codes_empty_text, _codes_footer, make_member_shift_group
from newsbot.bot.format import discord_len
from newsbot.bot.registration import commands_hash, setup_commands
from newsbot.config import Secrets, ShiftTableSource
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import CodeDetailsRow, ShiftSettings

NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
TODAY = date(2026, 10, 9)
FAR = TODAY + timedelta(days=30)
CHANNEL = 777000000000000001
SNOWFLAKE = "123456789012345678"


def _code(n: int) -> str:
    return f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA"


class Clock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _guild(db_path: str, *, alerts: bool = True) -> None:
    make_guild(db_path, GUILD_A, games=[("borderlands4", 11)])
    if alerts:
        with closing(connect(db_path)) as conn:
            repo.set_shift(conn, GUILD_A, enabled=True, channel_id=CHANNEL, ping="none")


def _put(db_path: str, n: int, *, seen: datetime = NOW - timedelta(days=1), **details) -> str:
    """A released code; any keyword that isn't a column of the alert row becomes its details."""
    source = details.pop("source_name", "Feed")
    url = details.pop("item_url", "https://example.com/post")
    status = details.pop("status", "posted")
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
            "VALUES (?, ?, ?, ?, ?, 0, 0)",
            (_code(n), seen.astimezone(UTC).isoformat(), source, url, status),
        )
        if details:
            clock_start = details.pop("clock_start", NOW - timedelta(days=1))
            repo._write_details(
                conn,
                CodeDetailsRow(code=_code(n), clock_start=clock_start, updated_at=NOW, **details),
            )
    return _code(n)


def _bulk(db_path: str, rows: list[tuple]) -> None:
    """Many released codes with details at once: `(n, seen, reward, category, expires_on)`."""
    with closing(connect(db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
            "VALUES (?, ?, 'Feed', 'https://example.com/post', 'posted', 0, 0)",
            [(_code(n), seen.isoformat()) for n, seen, *_ in rows],
        )
        conn.executemany(
            "INSERT INTO code_details (code, reward, category, expires_on, clock_start, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (_code(n), reward, category, expires, NOW.isoformat(), NOW.isoformat())
                for n, _seen, reward, category, expires in rows
            ],
        )


def _codes(cfg, db_path, clock=None):
    return command(make_member_shift_group(cfg, db_path, now=clock or Clock()), "codes").callback


def _history(cfg, db_path, clock=None):
    return command(make_member_shift_group(cfg, db_path, now=clock or Clock()), "history").callback


def _sent(interaction) -> dict:
    [call] = [m for m in interaction.sent if m.get("embed") is not None]
    return call


def _embed(interaction) -> discord.Embed:
    return _sent(interaction)["embed"]


async def _all_pages(interaction) -> list[discord.Embed]:
    """Page 1 as sent, then every other page by pressing Next, the way a member would."""
    call = _sent(interaction)
    view = call["view"]
    embeds = [call["embed"]]
    for _ in range(view.total_pages - 1):
        click = FakeInteraction(guild_id=GUILD_A)
        await view.next_button.callback(click)
        embeds.append(click.response.edits[-1]["embed"])
    return embeds


def _within_discord_limits(embed: discord.Embed) -> None:
    assert discord_len(embed.title or "") <= 256
    assert discord_len(embed.description or "") <= 4096
    assert discord_len(embed.footer.text or "") <= 2048
    assert len(embed) <= 6000  # discord.py's own sum of title, description and footer


# --- registration: what Discord will validate ---


def _bot(cfg, db_path) -> NewsBot:
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    bot = NewsBot(cfg, secrets, db_path)
    bot.synced = []

    async def sync(*, guild=None):
        bot.synced.append(guild.id if guild is not None else None)
        return []

    bot.tree.sync = sync
    return bot


def _payload(cfg, db_path) -> dict:
    bot = _bot(cfg, db_path)
    bot.tree.add_command(make_member_shift_group(cfg, db_path))
    [shift] = [c for c in bot.tree.get_commands() if c.name == "shift"]
    return shift.to_dict(bot.tree)


def _walk(node):
    """Every command or option dict under `node`, `node` included."""
    yield node
    for child in node.get("options", []):
        yield from _walk(child)


_SUBCOMMAND, _STRING, _INTEGER, _BOOLEAN = 1, 3, 4, 5
_NAME = re.compile(r"^[-_\w]{1,32}$")


def test_the_shift_payload_is_valid_for_discord(v3_cfg, v3_db):
    payload = _payload(v3_cfg, v3_db)

    assert payload["name"] == "shift"
    assert {o["name"] for o in payload["options"]} == {"codes", "history"}
    assert len(json.dumps(payload)) < 8000  # Discord's cap on one command's whole payload
    for node in _walk(payload):
        assert _NAME.match(node["name"]) and node["name"] == node["name"].lower(), node["name"]
        assert 1 <= len(node["description"]) <= 100, node["description"]
        assert len(node.get("options", [])) <= 25
        for choice in node.get("choices", []):
            assert 1 <= len(choice["name"]) <= 100
            assert len(str(choice["value"])) <= 100
        assert len(node.get("choices", [])) <= 25


def test_every_description_is_under_100_characters(v3_cfg, v3_db):
    descriptions = [n["description"] for n in _walk(_payload(v3_cfg, v3_db))]
    assert descriptions and all(len(text) < 100 for text in descriptions), descriptions


def test_required_options_come_first_and_the_types_are_right(v3_cfg, v3_db):
    payload = _payload(v3_cfg, v3_db)
    by_name = {o["name"]: o for o in payload["options"]}
    for sub in by_name.values():
        required = [bool(o.get("required")) for o in sub["options"]]
        assert required == sorted(required, reverse=True)  # no optional before a required one
    codes = {o["name"]: o for o in by_name["codes"]["options"]}
    history = {o["name"]: o for o in by_name["history"]["options"]}
    assert set(codes) == {"type", "public"} and set(history) == {"days", "public"}
    assert codes["type"]["type"] == _STRING
    assert [c["value"] for c in codes["type"]["choices"]] == [
        "all",
        "golden",
        "cosmetic",
        "permanent",
    ]
    assert [c["name"] for c in codes["type"]["choices"]] == [
        "all",
        "golden",
        "cosmetic",
        "permanent",
    ]
    assert history["days"]["type"] == _INTEGER
    assert (history["days"]["min_value"], history["days"]["max_value"]) == (1, 90)
    assert codes["public"]["type"] == history["public"]["type"] == _BOOLEAN
    assert not any(o.get("required") for o in (*codes.values(), *history.values()))


def test_the_commands_are_guild_only_and_need_no_permission(v3_cfg, v3_db):
    payload = _payload(v3_cfg, v3_db)
    assert payload["dm_permission"] is False  # a server, never a DM
    assert payload["integration_types"] == [0]  # installed to servers, not users
    assert payload.get("default_member_permissions") is None  # everyone may run it


def test_the_hash_is_stable_whatever_the_database_or_the_clock(v3_cfg, tmp_path, v3_db):
    def hash_of(db_path, **kwargs):
        bot = _bot(v3_cfg, db_path)
        bot.tree.add_command(make_member_shift_group(v3_cfg, db_path, **kwargs))
        return commands_hash(bot.tree, None)

    first = hash_of(v3_db)
    assert hash_of(v3_db) == first
    assert hash_of(str(tmp_path / "elsewhere.db")) == first
    assert hash_of(v3_db, now=Clock(datetime(2030, 1, 1, tzinfo=UTC))) == first


async def test_booting_twice_after_the_change_syncs_the_new_tree_exactly_once(v3_cfg, v3_db):
    boots = [_bot(v3_cfg, v3_db) for _ in range(4)]
    results = [await setup_commands(bot) for bot in boots]
    assert [r["global"] for r in results] == ["synced", "unchanged", "unchanged", "unchanged"]
    assert boots[0].synced.count(None) == 1  # one global sync (plus the home server's copy)
    assert len(boots[0].synced) == len(set(boots[0].synced))
    assert [bot.synced for bot in boots[1:]] == [[], [], []]


async def test_each_guild_scope_syncs_once_when_the_tree_changes(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"command_guild_ids": [GUILD_A, GUILD_A + 1]})
    first, second = _bot(cfg, v3_db), _bot(cfg, v3_db)
    await setup_commands(first)
    await setup_commands(second)
    assert sorted(first.synced) == sorted({GUILD_A, GUILD_A + 1} | set(first.synced))
    assert first.synced and second.synced == []


# --- gating ---


async def test_a_server_that_has_not_finished_setup_is_told_so_by_both_commands(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, set_up=False, games=[("borderlands4", 11)])
    for handler in (_codes, _history):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await handler(v3_cfg, v3_db)(interaction)
        assert "hasn't set up newsbot yet" in interaction.text
        assert not [m for m in interaction.sent if m.get("embed") is not None]


async def test_a_server_the_bot_has_no_row_for_is_told_the_same(v3_cfg, v3_db):
    for handler in (_codes, _history):
        interaction = FakeInteraction(guild_id=424242424242424242)
        await handler(v3_cfg, v3_db)(interaction)
        assert "hasn't set up newsbot yet" in interaction.text


async def test_a_server_following_other_games_gets_the_borderlands_reply_from_both(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 11), ("rust", 12)])
    _put(v3_db, 1, category="permanent")
    for handler in (_codes, _history):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await handler(v3_cfg, v3_db)(interaction)
        assert interaction.text == "This server doesn't follow Borderlands 4."
        assert not [m for m in interaction.sent if m.get("embed") is not None]


async def test_a_server_following_nothing_is_told_so(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[])
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(interaction)
    assert interaction.text.startswith("This server doesn't follow")


async def test_a_dm_gets_the_server_only_reply_without_touching_the_database(v3_cfg):
    for handler in (_codes, _history):
        interaction = FakeInteraction(guild_id=None)
        await handler(v3_cfg, "/nonexistent/dir/newsbot.db")(interaction)
        assert interaction.text == "This only works inside a server."


async def test_a_member_with_no_permissions_at_all_may_use_both(v3_cfg, v3_db):
    _guild(v3_db)
    _put(v3_db, 1)
    for handler in (_codes, _history):
        interaction = FakeInteraction(guild_id=GUILD_A, permissions=discord.Permissions.none())
        await handler(v3_cfg, v3_db)(interaction)
        assert _code(1) in _embed(interaction).description


async def test_a_server_removed_between_the_gate_and_the_query_still_gets_an_answer(
    v3_cfg, v3_db, monkeypatch
):
    """The bot is kicked while the command is in flight: `delete_guild` runs after the gate.

    The shared code list doesn't belong to any server, so the reply still goes out; the only
    per-server fact it wanted (the alerts channel) is gone, so that part of the empty text is
    left off. Nothing raises.
    """
    _guild(v3_db)
    real = commands_module._current_codes_sync

    def after_removal(db_path, guild_id, now):
        with closing(connect(db_path)) as conn:
            assert repo.delete_guild(conn, guild_id)
        return real(db_path, guild_id, now)

    monkeypatch.setattr(commands_module, "_current_codes_sync", after_removal)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert _embed(interaction).description == "No current codes."


async def test_a_server_removed_before_the_command_runs_is_not_set_up(v3_cfg, v3_db):
    _guild(v3_db)
    with closing(connect(v3_db)) as conn:
        repo.delete_guild(conn, GUILD_A)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _history(v3_cfg, v3_db)(interaction)
    assert "hasn't set up newsbot yet" in interaction.text


async def test_a_failed_send_after_removal_reaches_the_clients_error_handler(v3_cfg, v3_db):
    """Discord answers 404 "Unknown Webhook" once the bot is gone; the handler lets it through.

    `NewsBot._on_command_error` is what logs it and tries (and tolerates failing) to tell the
    member, so the one thing to pin here is that the handler doesn't swallow the error or
    turn it into something else.
    """
    _guild(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    async def gone(*args, **kwargs):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Webhook")

    interaction.followup.send = gone
    with pytest.raises(discord.NotFound):
        await _codes(v3_cfg, v3_db)(interaction)


# --- the type option, as a raw interaction payload ---


async def _raw(cmd, interaction, **options):
    """Invoke through discord.py's own path with options as the gateway would hand them over.

    Nothing is validated against the command's declared choices or range by the library for
    a string or an integer (`Range` is Discord-side only), so this is exactly what a handler
    receives from a payload that bypassed the client.
    """
    interaction._state = None
    interaction.guild = None
    interaction.channel = None
    types = {str: _STRING, bool: _BOOLEAN, int: _INTEGER}
    payload = [{"name": k, "type": types[type(v)], "value": v} for k, v in options.items()]
    namespace = app_commands.Namespace(interaction, {}, payload)
    await cmd._invoke_with_namespace(interaction, namespace)


@pytest.mark.parametrize("value", ["bogus", "", "all\u200b", "go lden", "x" * 10_000])
async def test_a_type_value_outside_the_choices_gets_the_whole_list_and_no_echo(
    v3_cfg, v3_db, value
):
    """Pinned behavior: an unknown `type` falls back to `all` (decided, not spec'd).

    Discord only ever sends one of the four choice values, so this is a defensive default; it
    shows more rather than less, never raises, and never repeats the stray text back.
    """
    _guild(v3_db)
    _put(v3_db, 1, category="golden", expires_on=FAR)
    _put(v3_db, 2, category="cosmetic", expires_on=FAR)
    cmd = command(make_member_shift_group(v3_cfg, v3_db, now=Clock()), "codes")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _raw(cmd, interaction, type=value)

    text = _embed(interaction).description
    assert _code(1) in text and _code(2) in text
    assert _embed(interaction).title == "Current SHiFT codes"
    if value.strip():
        assert value not in json.dumps(_embed(interaction).to_dict())


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("GOLDEN", {1}),
        ("Golden", {1}),
        (" golden", {1}),
        ("golden ", {1}),
        ("golden\n", {1}),
        ("\tCOSMETIC  ", {2}),
        ("Permanent", {3}),
        ("ALL", {1, 2, 3}),
        (" all ", {1, 2, 3}),
    ],
)
async def test_a_type_value_is_matched_without_regard_to_case_or_edge_whitespace(
    v3_cfg, v3_db, value, shown
):
    """Decided 2026-10-09: `GOLDEN` and ` golden` mean golden; only unknown values mean all."""
    _guild(v3_db)
    _put(v3_db, 1, category="golden", expires_on=FAR)
    _put(v3_db, 2, category="cosmetic", expires_on=FAR)
    _put(v3_db, 3, category="permanent")
    cmd = command(make_member_shift_group(v3_cfg, v3_db, now=Clock()), "codes")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _raw(cmd, interaction, type=value)

    assert {n for n in (1, 2, 3) if _code(n) in _embed(interaction).description} == shown


@pytest.mark.parametrize("kind", ["all", "golden", "cosmetic", "permanent"])
async def test_each_real_choice_value_survives_the_raw_path(v3_cfg, v3_db, kind):
    _guild(v3_db)
    _put(v3_db, 1, category="golden", expires_on=FAR)
    _put(v3_db, 2, category="cosmetic", expires_on=FAR)
    _put(v3_db, 3, category="permanent")
    cmd = command(make_member_shift_group(v3_cfg, v3_db, now=Clock()), "codes")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _raw(cmd, interaction, type=kind, public=True)

    shown = {n for n in (1, 2, 3) if _code(n) in _embed(interaction).description}
    assert shown == {"all": {1, 2, 3}, "golden": {1}, "cosmetic": {2}, "permanent": {3}}[kind]
    assert interaction.response.deferred_ephemeral is False


@pytest.mark.parametrize(
    "days",
    [
        pytest.param(0, id="zero"),
        pytest.param(-5, id="negative"),
        pytest.param(91, id="ninety-one"),
        pytest.param(500, id="five-hundred"),
        pytest.param(10**6, id="a-million"),
        pytest.param(10**12, id="a-trillion"),
        pytest.param("abc", id="a-string"),
    ],
)
async def test_a_days_value_outside_one_to_ninety_never_breaks_or_widens_the_window(
    v3_cfg, v3_db, days
):
    _guild(v3_db)
    _put(v3_db, 1, seen=NOW - timedelta(days=100))
    cmd = command(make_member_shift_group(v3_cfg, v3_db, now=Clock()), "history")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _raw(cmd, interaction, days=days)

    assert _code(1) not in _embed(interaction).description


# --- the empty pages ---


@pytest.mark.parametrize(
    ("kind", "phrase"),
    [
        ("all", "No current codes."),
        ("golden", "No current golden codes."),
        ("cosmetic", "No current cosmetic codes."),
        ("permanent", "No current permanent codes."),
    ],
)
@pytest.mark.parametrize("alerts", ["on", "saved_but_off", "never_saved"])
async def test_every_empty_text_with_alerts_on_and_off(v3_cfg, v3_db, kind, phrase, alerts):
    _guild(v3_db, alerts=False)
    with closing(connect(v3_db)) as conn:
        if alerts == "on":
            repo.set_shift(conn, GUILD_A, enabled=True, channel_id=CHANNEL, ping="none")
        elif alerts == "saved_but_off":
            repo.set_shift(conn, GUILD_A, enabled=False, channel_id=CHANNEL, ping="none")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction, type=kind)

    expected = phrase + (
        f" I'll post new ones in <#{CHANNEL}> when they show up." if alerts == "on" else ""
    )
    embed = _embed(interaction)
    assert embed.description == expected
    assert embed.footer.text.endswith("Page 1 of 1")
    view = _sent(interaction)["view"]
    assert view.total_pages == 1 and view.prev_button.disabled and view.next_button.disabled


def test_the_empty_text_is_built_from_the_saved_integer_alone():
    shift = ShiftSettings(
        guild_id=1,
        enabled=True,
        channel_id=CHANNEL,
        ping="everyone",
        enabled_at=None,
        ping_day=None,
        ping_count=0,
    )
    text = _codes_empty_text("all", shift)
    assert text == f"No current codes. I'll post new ones in <#{CHANNEL}> when they show up."
    assert "@" not in text  # the ping setting never leaks into the page
    assert _codes_empty_text("all", None) == "No current codes."
    assert _codes_empty_text("surprise", None) == "No current codes."  # unknown kind: no adjective


async def test_a_deleted_alerts_channel_is_left_out_of_the_empty_page(v3_cfg, v3_db):
    """Decided 2026-10-09: the setting stays (`on_guild_channel_delete` only adds an admin
    notice), but the empty page doesn't promise a channel the server's cache says is gone.
    """
    _guild(v3_db, alerts=False)
    gone = 999000000000000009
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_A, enabled=True, channel_id=gone, ping="everyone")
    interaction = FakeInteraction(guild_id=GUILD_A)
    interaction.guild = SimpleNamespace(get_channel=lambda channel_id: None)

    await _codes(v3_cfg, v3_db)(interaction)

    assert _embed(interaction).description == "No current codes."
    mentions = _sent(interaction)["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False


async def test_a_channel_the_server_still_has_is_mentioned(v3_cfg, v3_db):
    _guild(v3_db)
    asked = []
    interaction = FakeInteraction(guild_id=GUILD_A)
    interaction.guild = SimpleNamespace(
        get_channel=lambda channel_id: asked.append(channel_id) or object()
    )

    await _codes(v3_cfg, v3_db)(interaction)

    assert asked == [CHANNEL]
    assert f"<#{CHANNEL}>" in _embed(interaction).description


async def test_with_no_guild_to_ask_the_saved_channel_is_trusted(v3_cfg, v3_db):
    _guild(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)  # .guild is None: not cached
    await _codes(v3_cfg, v3_db)(interaction)
    assert f"<#{CHANNEL}>" in _embed(interaction).description


# --- the footer ---


def _cfg_with_tables(cfg, *urls: str):
    sources = [
        ShiftTableSource(type="shift_table", name=f"table{i}", url=url, trust="community")
        for i, url in enumerate(urls)
    ]
    game = cfg.catalog[0]
    kept = [s for s in game.sources if not isinstance(s, ShiftTableSource)]
    return cfg.model_copy(
        update={
            "catalog": [game.model_copy(update={"sources": [*kept, *sources]}), *cfg.catalog[1:]]
        }
    )


_REDEEM = "redeem at shift.gearboxsoftware.com"


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        ((), f"Codes from Gearbox \u00b7 {_REDEEM}"),
        (
            ("https://mentalmars.com/a/",),
            f"Codes from Gearbox and mentalmars.com \u00b7 {_REDEEM}",
        ),
        (
            ("https://mentalmars.com/a/", "https://example.org/b"),
            f"Codes from Gearbox, mentalmars.com and example.org \u00b7 {_REDEEM}",
        ),
        (  # the same host twice, once with www and a port: listed once, port gone
            ("https://www.mentalmars.com/a/", "https://MentalMars.com:8443/b"),
            f"Codes from Gearbox and mentalmars.com \u00b7 {_REDEEM}",
        ),
        (
            ("https://a.example/1", "https://b.example/2", "https://c.example/3"),
            f"Codes from Gearbox, a.example, b.example and c.example \u00b7 {_REDEEM}",
        ),
    ],
)
def test_the_footer_for_zero_one_two_and_more_sources(v3_cfg, urls, expected):
    assert _codes_footer(_cfg_with_tables(v3_cfg, *urls)) == expected


def test_the_footer_lists_three_hosts_at_most(v3_cfg):
    urls = [f"https://h{i}.example/p" for i in range(6)]
    footer = _codes_footer(_cfg_with_tables(v3_cfg, *urls))
    assert footer == f"Codes from Gearbox, h0.example, h1.example and h2.example \u00b7 {_REDEEM}"


def test_the_footer_never_passes_300_characters_and_drops_hosts_whole(v3_cfg):
    first = ".".join(["a" * 60, "b" * 60, "c" * 60]) + ".example"  # 190 characters
    second = ".".join(["d" * 60, "e" * 60]) + ".example"  # 128 characters
    footer = _codes_footer(_cfg_with_tables(v3_cfg, f"https://{first}/", f"https://{second}/"))
    assert len(footer) <= 300 and footer.endswith(_REDEEM)
    assert first in footer and second not in footer  # the second didn't fit, so it's not there

    huge = ".".join(["w" * 63, "x" * 63, "y" * 63, "z" * 61])  # 253 characters
    alone = _codes_footer(_cfg_with_tables(v3_cfg, f"https://{huge}/", "https://f.example/"))
    assert len(alone) <= 300 and alone.endswith(_REDEEM)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@mentalmars.com:8443/a",
        "https://m\u00fcnchen.example/codes",
        "https://xn--mnchen-3ya.example/codes",
        "https://[2001:db8::1]:8443/codes",
        "https://192.0.2.7:9000/codes",
        "https://mentalmars.com./codes",
    ],
)
def test_odd_hosts_never_leak_ports_or_credentials_into_the_footer(v3_cfg, url):
    footer = _codes_footer(_cfg_with_tables(v3_cfg, url))
    assert footer.startswith("Codes from Gearbox and ") and footer.endswith(_REDEEM)
    assert "pass" not in footer and "user" not in footer and "8443" not in footer
    assert "9000" not in footer and "None" not in footer and "@" not in footer
    assert "/" not in footer.removesuffix(_REDEEM).replace("Codes from", "")


async def test_the_footer_reaches_the_page_with_the_page_count(v3_cfg, v3_db):
    _guild(v3_db)
    cfg = _cfg_with_tables(v3_cfg, "https://mentalmars.com/a/")
    await_me = _codes(cfg, v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await await_me(interaction)
    assert _embed(interaction).footer.text == (
        f"Codes from Gearbox and mentalmars.com \u00b7 {_REDEEM} \u00b7 Page 1 of 1"
    )


# --- hostile text, in every field ---

HOSTILE = [
    "@everyone",
    "@here",
    f"<@{SNOWFLAKE}>",
    f"<@!{SNOWFLAKE}>",
    f"<@&{SNOWFLAKE}>",
    f"<#{SNOWFLAKE}>",
    f"</shift:{SNOWFLAKE}>",
    f"<:evil:{SNOWFLAKE}>",
    f"<a:evil:{SNOWFLAKE}>",
    "[click](http://evil.example/x)",
    "https://evil.example/phish",
    "discord.gg/abcdef",
    "```\n@everyone\n```",
    "`` ` ``",
    "||spoiler||",
    "# heading\n> quote\n- list",
    "@\u202eeveryone",
    "@\u200beveryone",
    "\u202e\u2066reversed\u2069",
    "@" * 150,
    "*" * 150,
    "\U0001f600" * 140,
]


def _assert_inert(text: str, *, links_allowed: bool = False) -> None:
    assert "@everyone" not in text and "@here" not in text, text
    assert not re.search(r"<@!?&?\d", text), text
    assert not re.search(r"<#\d", text.replace(f"<#{CHANNEL}>", "")), text
    assert not re.search(r"</\w+:\d", text), text
    assert not re.search(r"<a?:\w+:\d", text), text
    assert "discord.gg/" not in text, text
    if not links_allowed:
        assert "://" not in text, text


@pytest.mark.parametrize("hostile", HOSTILE)
async def test_hostile_reward_and_origin_text_is_inert_on_every_page(v3_cfg, v3_db, hostile):
    _guild(v3_db)
    _put(v3_db, 1, category="golden", reward=hostile[:300], expires_on=FAR)
    _put(v3_db, 2, category="permanent", reward=hostile[:300], origin=hostile[:300])
    _put(v3_db, 3, category="cosmetic", reward=hostile[:300], expires_on=FAR)
    _put(v3_db, 4, reward=hostile[:300])
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction, public=True)

    for embed in await _all_pages(interaction):
        _within_discord_limits(embed)
        _assert_inert(embed.description)
        _assert_inert(embed.title)
        _assert_inert(embed.footer.text)
    assert not re.search(
        "[\u202a-\u202e\u2066-\u2069]",
        "".join(e.description for e in await _all_pages(interaction)),
    )
    mentions = _sent(interaction)["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False


@pytest.mark.parametrize("hostile", HOSTILE)
async def test_hostile_source_names_and_via_names_are_inert_in_history(v3_cfg, v3_db, hostile):
    _guild(v3_db)
    _put(
        v3_db,
        1,
        source_name=hostile[:100],
        reward=hostile[:300],
        reward_from=hostile[:100],
        expires_from=hostile[:100],
        category_from=hostile[:100],
        category="golden",
        expires_on=FAR,
        reward_source="table",
        expires_source="table",
    )
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history(v3_cfg, v3_db)(interaction, public=True)

    for embed in await _all_pages(interaction):
        _within_discord_limits(embed)
        _assert_inert(embed.description, links_allowed=True)
    mentions = _sent(interaction)["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/a>) @everyone [x](http://evil.example",
        "https://evil.example/a b`c<d>e",
        "javascript:alert(1)",
        "https://evil.example/" + "a" * 5000,
        "https://evil.example/\u202efdp.exe",
        "",
    ],
)
async def test_a_hostile_item_url_cannot_close_the_link_or_ping(v3_cfg, v3_db, url):
    _guild(v3_db)
    _put(v3_db, 1, item_url=url)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history(v3_cfg, v3_db)(interaction)

    text = _embed(interaction).description
    _within_discord_limits(_embed(interaction))
    # The link is one angle-bracketed autolink with nothing in it that could close it early;
    # whatever is left once it is cut out must be inert.
    links = re.findall(r"<https?://[^>\s]*>", text)
    assert len(links) <= 1
    outside = re.sub(r"<https?://[^>\s]*>", "", text)
    assert "@everyone" not in outside and "[x](" not in outside
    assert "javascript:" not in text
    assert _code(1) in text


@pytest.mark.parametrize("seed", range(20))
async def test_random_hostile_rewards_never_overflow_a_page_or_split_an_entry(v3_cfg, v3_db, seed):
    rng = random.Random(seed)  # noqa: S311 (a reproducible sequence, not a secret)
    alphabet = list("*_~`|>\\@<#&:[]()") + [
        "\U0001f600",
        "\u202e",
        "\u200b",
        "a",
        " ",
        "\n",
        "&amp;",
    ]
    _guild(v3_db)
    count = 60
    for n in range(count):
        length = rng.choice([1, 20, 150, 300])
        text = "".join(rng.choice(alphabet) for _ in range(length))[:300]
        _put(
            v3_db,
            n,
            category=rng.choice(["golden", "cosmetic", "permanent", "other", None]),
            reward=text,
            origin="".join(rng.choice(alphabet) for _ in range(rng.choice([0, 300])))[:300],
            expires_on=rng.choice([None, FAR, TODAY]),
            reward_from="".join(rng.choice(alphabet) for _ in range(100))[:100],
            reward_source="table",
        )
    for handler in (_codes, _history):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await handler(v3_cfg, v3_db)(interaction)
        embeds = await _all_pages(interaction)
        joined = "\n".join(e.description for e in embeds)
        for embed in embeds:
            _within_discord_limits(embed)
            _assert_inert(embed.description, links_allowed=True)
        assert all(joined.count(_code(n)) == 1 for n in range(count)), handler


async def test_an_absurdly_long_stored_source_name_is_cut_not_fatal(v3_cfg, v3_db):
    _guild(v3_db)
    _put(v3_db, 1, source_name="S" * 50_000)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _history(v3_cfg, v3_db)(interaction)
    _within_discord_limits(_embed(interaction))
    assert _code(1) in _embed(interaction).description


async def test_an_absurdly_long_stored_via_name_does_not_take_history_down(v3_cfg, v3_db):
    _guild(v3_db)
    _put(v3_db, 1)
    _put(v3_db, 2, reward="1 Golden Key", reward_from="N" * 6000, reward_source="table")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _history(v3_cfg, v3_db)(interaction)
    assert _code(1) in _embed(interaction).description


# --- size ---


async def test_five_thousand_current_codes_page_cleanly_and_quickly(v3_cfg, v3_db):
    _guild(v3_db)
    total = 5_000
    _bulk(
        v3_db,
        [
            (n, NOW - timedelta(minutes=n), f"{n % 7 + 1} Golden Keys", "golden", FAR.isoformat())
            for n in range(total)
        ],
    )
    interaction = FakeInteraction(guild_id=GUILD_A)

    started = time.perf_counter()
    await _codes(v3_cfg, v3_db)(interaction, public=True)
    elapsed = time.perf_counter() - started

    assert elapsed < 10, f"the handler took {elapsed:.1f}s for {total} codes"
    view = _sent(interaction)["view"]
    embeds = await _all_pages(interaction)
    joined = "\n\n".join(e.description for e in embeds)
    for index, embed in enumerate(embeds, start=1):
        _within_discord_limits(embed)
        assert embed.description.count("`") % 2 == 0  # no entry split mid code span
        assert embed.footer.text.endswith(f"Page {index} of {len(embeds)}")
    assert view.total_pages == len(embeds)
    assert all(joined.count(_code(n)) == 1 for n in range(total))
    # Greedy packing wastes little: no more than a tenth over the least possible.
    least = -(-sum(discord_len(e.description) for e in embeds) // 4096)
    assert len(embeds) <= least * 1.1 + 2
    assert joined.count("**Golden Keys**") == 1  # the heading once, then "(continued)" per page
    assert joined.count("(continued)") == len(embeds) - 1


async def test_five_thousand_current_codes_hold_a_modest_amount_of_memory(v3_cfg, v3_db):
    _guild(v3_db)
    _bulk(
        v3_db,
        [
            (n, NOW - timedelta(minutes=n), "3 Golden Keys", "golden", FAR.isoformat())
            for n in range(5_000)
        ],
    )
    interaction = FakeInteraction(guild_id=GUILD_A)
    tracemalloc.start()
    try:
        await _codes(v3_cfg, v3_db)(interaction)
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The view keeps every page for its ten minutes; that is the whole cost of "no re-query".
    assert retained < 25 * 2**20, f"the open pager holds {retained / 2**20:.1f} MiB"
    assert peak < 100 * 2**20, f"peak {peak / 2**20:.1f} MiB"
    assert _sent(interaction)["view"].total_pages > 1


async def test_a_ninety_day_history_of_ten_thousand_codes_pages_and_loses_none(v3_cfg, v3_db):
    _guild(v3_db)
    total = 10_000
    _bulk(
        v3_db,
        [(n, NOW - timedelta(seconds=n * 777), f"reward {n}", None, None) for n in range(total)],
    )
    # 10,000 codes spread over 90 days: 777 s apart is 90 days exactly, give or take a code.
    interaction = FakeInteraction(guild_id=GUILD_A)

    started = time.perf_counter()
    await _history(v3_cfg, v3_db)(interaction, days=90, public=True)
    elapsed = time.perf_counter() - started

    assert elapsed < 15, f"the handler took {elapsed:.1f}s"
    embeds = await _all_pages(interaction)
    joined = "\n".join(e.description for e in embeds)
    inside = sum(
        1 for n in range(total) if NOW - timedelta(seconds=n * 777) >= NOW - timedelta(days=90)
    )
    assert inside >= 9_990
    assert sum(joined.count(_code(n)) for n in range(total)) == inside  # none lost, none doubled
    for index, embed in enumerate(embeds, start=1):
        _within_discord_limits(embed)
        assert embed.description.count("```") % 2 == 0  # no fenced block cut in half
        assert embed.footer.text.endswith(f"Page {index} of {len(embeds)}")
    sent = _sent(interaction)
    assert sent["allowed_mentions"].everyone is False
    assert sent["ephemeral"] is False  # public: one open message, with the whole window behind it


async def test_the_history_window_is_the_only_cap(v3_cfg, v3_db):
    """No row limit applies below the window: 90 days of 3,000 codes lists all 3,000.

    Pinned so that a LIMIT added later is a decision. The practical cap is what gets collected
    in 90 days (a busy week is a few dozen codes), and the pager is 4 KiB a page.
    """
    _guild(v3_db)
    _bulk(v3_db, [(n, NOW - timedelta(minutes=n), "", None, None) for n in range(3_000)])
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _history(v3_cfg, v3_db)(interaction, days=90)
    joined = "\n".join(e.description for e in await _all_pages(interaction))
    assert all(joined.count(_code(n)) == 1 for n in range(3_000))


async def test_worst_case_entries_still_fit_and_never_shorten_a_reward(v3_cfg, v3_db):
    """300 shown units of reward and of origin on every entry; every one must appear whole."""
    _guild(v3_db)
    count = 80
    for n in range(count):
        _put(
            v3_db,
            n,
            category="permanent" if n % 2 else "golden",
            reward=f"R{n:03d}" + "*" * 148,
            origin=f"O{n:03d}" + "_" * 148,
            expires_on=None if n % 2 else FAR,
        )
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(interaction)
    embeds = await _all_pages(interaction)
    text = "\n".join(e.description for e in embeds)
    for embed in embeds:
        _within_discord_limits(embed)
    for n in range(count):
        assert f"R{n:03d}" in text
    assert "\u2026" not in text  # nothing was cut at 300 shown: the limit is the cap, not a trim


# --- the pager ---


async def _pager(v3_cfg, v3_db, *, pages_wanted=3):
    _guild(v3_db)
    n = 0
    while True:
        _put(v3_db, n, category="cosmetic", reward="*" * 150, expires_on=FAR)
        n += 1
        if n % 10 == 0:
            interaction = FakeInteraction(guild_id=GUILD_A, user_id=77)
            await _codes(v3_cfg, v3_db)(interaction, public=True)
            if _sent(interaction)["view"].total_pages >= pages_wanted:
                return interaction, _sent(interaction)["view"]


async def test_the_pager_times_out_after_ten_minutes_and_is_not_persistent(v3_cfg, v3_db):
    _interaction, view = await _pager(v3_cfg, v3_db)
    assert view.timeout == 600
    assert view.is_persistent() is False
    assert view.owner_id == 77


async def test_only_the_person_who_ran_the_command_may_turn_pages_even_when_public(v3_cfg, v3_db):
    _interaction, view = await _pager(v3_cfg, v3_db)
    stranger = FakeInteraction(guild_id=GUILD_A, user_id=78)
    owner = FakeInteraction(guild_id=GUILD_A, user_id=77)

    assert await view.interaction_check(stranger) is False
    assert stranger.sent[-1]["content"] == "Only the requester can do this."
    assert stranger.sent[-1]["ephemeral"] is True
    assert view.page == 1 and not stranger.response.edits
    assert await view.interaction_check(owner) is True
    assert not owner.sent


async def test_paging_forward_and_back_shows_the_same_embeds_and_keeps_mentions_off(v3_cfg, v3_db):
    first, view = await _pager(v3_cfg, v3_db)
    forward = []
    for _ in range(view.total_pages - 1):
        click = FakeInteraction(guild_id=GUILD_A, user_id=77)
        await view.next_button.callback(click)
        forward.append(click.response.edits[-1])
    assert view.next_button.disabled and not view.prev_button.disabled
    back = []
    for _ in range(view.total_pages - 1):
        click = FakeInteraction(guild_id=GUILD_A, user_id=77)
        await view.prev_button.callback(click)
        back.append(click.response.edits[-1])
    assert view.page == 1 and view.prev_button.disabled
    assert [e["embed"].description for e in reversed(back[:-1])] == [
        e["embed"].description for e in forward[:-1]
    ]
    assert back[-1]["embed"].description == _sent(first)["embed"].description
    assert all(e["allowed_mentions"].everyone is False for e in [*forward, *back])


@pytest.mark.parametrize("direction", ["prev", "next"])
async def test_a_stale_click_past_either_end_leaves_the_pager_on_a_real_page(
    v3_cfg, v3_db, direction
):
    first, view = await _pager(v3_cfg, v3_db)
    pages = [_sent(first)["embed"].description]
    for _ in range(view.total_pages - 1):
        click = FakeInteraction(guild_id=GUILD_A, user_id=77)
        await view.next_button.callback(click)
        pages.append(click.response.edits[-1]["embed"].description)
    # Park on the edge, then deliver one more click the disabled button would have refused.
    if direction == "prev":
        while view.page > 1:
            await view.prev_button.callback(FakeInteraction(guild_id=GUILD_A, user_id=77))
        edge = 1
    else:
        edge = view.total_pages
    click = FakeInteraction(guild_id=GUILD_A, user_id=77)
    await (view.prev_button if direction == "prev" else view.next_button).callback(click)

    assert 1 <= view.page <= view.total_pages
    shown = click.response.edits[-1]["embed"].description if click.response.edits else None
    assert shown == pages[edge - 1]  # still the page it was on


# --- a reader against the hook ---


async def test_concurrent_commands_while_the_hook_commits_batches_see_whole_batches(v3_cfg, v3_db):
    _guild(v3_db)
    batch, rounds = 10, 25
    done = threading.Event()

    def hook() -> None:
        with closing(connect(v3_db)) as conn:
            for r in range(rounds):
                rows = [
                    (r * batch + i, NOW, f"batch {r}", "golden", FAR.isoformat())
                    for i in range(batch)
                ]
                with conn:
                    conn.executemany(
                        "INSERT INTO alerted_codes (code, first_seen_at, source_name, "
                        "item_url, status, from_roundup, pinged) "
                        "VALUES (?, ?, 'Feed', 'https://example.com/p', 'posted', 0, 0)",
                        [(_code(n), seen.isoformat()) for n, seen, *_ in rows],
                    )
                    conn.executemany(
                        "INSERT INTO code_details (code, reward, category, expires_on, "
                        "clock_start, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (_code(n), reward, cat, exp, NOW.isoformat(), NOW.isoformat())
                            for n, _s, reward, cat, exp in rows
                        ],
                    )
        done.set()

    writer = threading.Thread(target=hook)
    writer.start()
    counts = []
    try:
        while not done.is_set():
            batch_of = [FakeInteraction(guild_id=GUILD_A) for _ in range(4)]
            await asyncio.gather(*(_codes(v3_cfg, v3_db)(i) for i in batch_of))
            for interaction in batch_of:
                text = "\n".join(e.description for e in await _all_pages(interaction))
                counts.append(len(re.findall(r"\d{5}-AAAAA", text)))
                for n in range(rounds * batch):
                    if _code(n) in text:
                        assert f"batch {n // batch}" in text
    finally:
        writer.join(timeout=60)
    assert counts and all(c % batch == 0 for c in counts), counts
    final = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(final)
    joined = "\n".join(e.description for e in await _all_pages(final))
    assert len(re.findall(r"\d{5}-AAAAA", joined)) == batch * rounds
