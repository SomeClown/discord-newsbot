"""Breaking the router: who hears what, and what happens when everything goes wrong.

The router has three doors and one promise: a server's problem stays in that
server, and the owner's problem stays out of every server. These tests try
every door with the other doors' channel ids lying around, feed them text
that would love to ping somebody, and hand them a Discord that throws every
exception it knows. A couple are strict xfails because the router trusts the
channel id it finds in the database and never asks Discord whether that
channel lives in the server it's talking about. I've read the code three
times and I can't find the check. I'd be delighted to be wrong; see git log
for how often that goes.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import discord
import pytest

from newsbot.alerts import send_alert, send_to_channel
from newsbot.bot.format import _truncate_utf16, discord_len
from newsbot.guilds.notify import Router, client_sender
from newsbot.store import repo
from newsbot.store.db import connect, migrate

OWNER = 9000
HOME = 1
A1, A2 = 1001, 1002


class Spy:
    """A `Send` that records, and optionally misbehaves."""

    def __init__(self, exc=None, hang=False):
        self.sent: list[tuple[int, str]] = []
        self.exc = exc
        self.hang = hang

    async def __call__(self, channel_id, text):
        if self.hang:
            await asyncio.sleep(3600)
        if self.exc is not None:
            raise self.exc
        self.sent.append((channel_id, text))
        return True

    def to(self, channel_id):
        return [t for c, t in self.sent if c == channel_id]


class Chan:
    """A channel that remembers what it was sent and which server it lives in."""

    def __init__(self, guild_id):
        self.guild = SimpleNamespace(id=guild_id)
        self.sent: list[tuple[str, dict]] = []

    async def send(self, text, **kwargs):
        self.sent.append((text, kwargs))


class Client:
    def __init__(self, channels):
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise RuntimeError("no fetching in tests")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "n.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        repo.create_guild(conn, 1, admin_channel_id=A1)
        repo.create_guild(conn, 2, admin_channel_id=A2)
        repo.create_guild(conn, 3)  # no admin channel
    return path


def _notices(db_path, guild_id):
    with closing(connect(db_path)) as conn:
        return repo.recent_notices(conn, guild_id)


def _live_mentions(text):
    """Anything in `text` that Discord could still turn into a ping."""
    found = re.findall(r"(?<![\w​])@(?:everyone|here)", text)
    found += re.findall(r"<@[!&]?\d{17,20}>", text)
    return found


# --- routing isolation ---


async def test_every_door_opens_only_onto_its_own_room(db_path):
    spy = Spy()
    router = Router(db_path, spy, OWNER)
    await router.alert_owner("owner-news")
    await router.notify_guild(1, "g1-notice")
    await router.notify_guild(2, "g2-notice")
    await router.notify_guild(3, "g3-notice")
    await router.send_report(1, A1, "g1-report")
    await router.send_report(2, A2, "g2-report")
    assert spy.to(OWNER) == ["owner-news"]
    assert spy.to(A1) == ["g1-notice", "g1-report"]
    assert spy.to(A2) == ["g2-notice", "g2-report"]
    assert {c for c, _ in spy.sent} == {OWNER, A1, A2}
    assert [n.text for n in _notices(db_path, 3)] == ["g3-notice"]  # recorded, never posted
    assert [n.text for n in _notices(db_path, 1)] == ["g1-notice"]  # reports aren't notices


async def test_owner_alert_never_falls_back_to_a_guild_channel(db_path):
    spy = Spy()
    await Router(db_path, spy, None).alert_owner("nobody home")
    assert spy.sent == []


async def test_owner_alert_does_not_depend_on_any_guild_row(tmp_path):
    spy = Spy()
    await Router(tmp_path / "never-created.db", spy, OWNER).alert_owner("db? what db?")
    assert spy.sent == [(OWNER, "db? what db?")]


async def test_a_guild_with_its_admin_channel_cleared_posts_nothing_but_still_records(db_path):
    with closing(connect(db_path)) as conn:
        repo.update_guild_settings(conn, 1, admin_channel_id=None)
    spy = Spy()
    await Router(db_path, spy, OWNER).notify_guild(1, "now where?")
    assert spy.sent == []
    assert [n.text for n in _notices(db_path, 1)] == ["now where?"]


async def test_the_notice_is_written_before_the_post_goes_out(db_path):
    seen_during_send: list[list[str]] = []

    async def send(channel_id, text):
        seen_during_send.append([n.text for n in _notices(db_path, 1)])
        return True

    await Router(db_path, send, OWNER).notify_guild(1, "write first")
    assert seen_during_send == [["write first"]]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Router.notify_guild posts to whatever admin_channel_id is stored, through "
        "client_sender -> send_to_channel, and nothing checks that the channel belongs to "
        "the guild. A server whose admin_channel_id holds another server's channel id "
        "delivers its notices there."
    ),
)
async def test_notice_is_not_posted_into_a_channel_of_another_guild(db_path):
    foreign = Chan(guild_id=2)  # guild 2's channel...
    with closing(connect(db_path)) as conn:
        repo.update_guild_settings(conn, 3, admin_channel_id=77)  # ...named by guild 3
    client = Client({77: foreign})
    await Router(db_path, client_sender(client), OWNER).notify_guild(3, "guild 3's problem")
    assert foreign.sent == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Same root cause: a non-home guild whose admin_channel_id is the owner's channel "
        "id gets its notices posted into the owner's admin channel (home guild)."
    ),
)
async def test_non_home_guild_cannot_aim_its_notices_at_the_owner_channel(db_path):
    owner_chan = Chan(guild_id=HOME)
    with closing(connect(db_path)) as conn:
        repo.update_guild_settings(conn, 2, admin_channel_id=OWNER)
    client = Client({OWNER: owner_chan})
    await Router(db_path, client_sender(client), OWNER).notify_guild(2, "stranger's problem")
    assert owner_chan.sent == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "send_report ignores its guild_id argument entirely and posts to whatever channel "
        "it's handed, including one in a different server."
    ),
)
async def test_report_is_not_posted_into_a_channel_of_another_guild(db_path):
    foreign = Chan(guild_id=2)
    client = Client({A2: foreign})
    await Router(db_path, client_sender(client), OWNER).send_report(1, A2, "guild 1's report")
    assert foreign.sent == []


async def test_home_guild_admin_channel_equal_to_the_owner_channel_is_the_intended_overlap(
    db_path,
):
    """Documented choice: the owner's own server is allowed to be its own admin room."""
    with closing(connect(db_path)) as conn:
        repo.update_guild_settings(conn, HOME, admin_channel_id=OWNER)
    spy = Spy()
    router = Router(db_path, spy, OWNER)
    await router.notify_guild(HOME, "home notice")
    await router.alert_owner("owner news")
    assert spy.to(OWNER) == ["home notice", "owner news"]


# --- mention safety ---

HOSTILE = [
    "@everyone",
    "@here",
    "hi @everyone and @here",
    "<@123456789012345678>",
    "<@!123456789012345678>",
    "<@&123456789012345678>",
    "@\x00everyone",
    "@ev\x00eryone",
    "<@\x00123456789012345678>",
    "@" * 50 + "everyone",
    "@everyone" * 400,
]


@pytest.mark.parametrize("text", HOSTILE)
async def test_hostile_text_posts_nothing_live_through_every_door(db_path, text):
    a1, a2, owner = Chan(1), Chan(2), Chan(HOME)
    client = Client({A1: a1, A2: a2, OWNER: owner})
    router = Router(db_path, client_sender(client), OWNER)
    await router.alert_owner(text)
    await router.notify_guild(1, text)
    await router.send_report(2, A2, text)
    posted = [*owner.sent, *a1.sent, *a2.sent]
    assert len(posted) == 3
    for sent_text, kwargs in posted:
        assert _live_mentions(sent_text) == []
        assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert discord_len(sent_text) <= 2000
    assert all(_live_mentions(n.text) == [] for n in _notices(db_path, 1))


def test_allowed_mentions_none_really_means_no_everyone_roles_or_users():
    """The suspenders: make sure 'none' still means none in this discord.py."""
    allowed = discord.AllowedMentions.none()
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


async def test_the_router_leaves_markdown_and_links_to_the_caller(db_path):
    """Documented contract: the router defuses pings, not formatting. Callers run `esc()`."""
    spy = Spy()
    nasty = "[re-auth here](https://evil.example/login) discord.gg/abc <#123456789012345678>"
    await Router(db_path, spy, OWNER).alert_owner(nasty)
    assert spy.sent == [(OWNER, nasty)]


async def test_mention_defusing_happens_before_the_cap_so_expansion_cannot_overflow(db_path):
    spy = Spy()
    await Router(db_path, spy, OWNER).alert_owner("@everyone" * 300)  # 2700 chars, all cut
    ((_, text),) = spy.sent
    assert discord_len(text) <= 2000
    assert _live_mentions(text) == []


# --- length limits ---


@pytest.mark.parametrize(
    ("text", "unchanged"),
    [
        ("a" * 2000, True),
        ("a" * 2001, False),
        ("\U0001f600" * 1000, True),  # 2000 UTF-16 units exactly
        ("\U0001f600" * 1001, False),
        ("a" * 1999 + "\U0001f600", False),  # 2001 units: the emoji must go whole
        ("a" * 1998 + "\U0001f600", True),
    ],
)
async def test_the_cap_is_exactly_2000_utf16_units(db_path, text, unchanged):
    spy = Spy()
    await Router(db_path, spy, OWNER).alert_owner(text)
    ((_, sent),) = spy.sent
    assert discord_len(sent) <= 2000
    sent.encode("utf-16-le")  # no lone surrogates
    assert (sent == text) is unchanged
    if not unchanged:
        assert sent.endswith("…")


async def test_notice_is_stored_at_the_same_length_it_is_posted(db_path):
    spy = Spy()
    await Router(db_path, spy, OWNER).notify_guild(1, "\U0001f600" * 1500)
    ((_, sent),) = spy.sent
    assert _notices(db_path, 1)[0].text == sent


# --- blank text ---


@pytest.mark.parametrize("text", ["", " ", "\n\n", "\x00", "\x00\x00 \x00", "\t"])
async def test_blank_notice_posts_nothing_and_records_nothing(db_path, text):
    spy = Spy()
    await Router(db_path, spy, OWNER).notify_guild(1, text)
    assert spy.sent == []
    assert _notices(db_path, 1) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "alert_owner and send_report hand blank text straight to Discord, which rejects an "
        "empty message with a 400; notify_guild drops blanks but these two don't."
    ),
)
@pytest.mark.parametrize("door", ["owner", "report"])
@pytest.mark.parametrize("text", ["", "\x00", "   "])
async def test_blank_text_is_not_sent_to_discord(db_path, door, text):
    spy = Spy()
    router = Router(db_path, spy, OWNER)
    if door == "owner":
        await router.alert_owner(text)
    else:
        await router.send_report(1, A1, text)
    assert spy.sent == []


# --- robustness ---


def _resp(status):
    return SimpleNamespace(status=status, reason="nope", headers={}, request_info=None)


EXCEPTIONS = [
    lambda: discord.Forbidden(_resp(403), "Missing Access"),
    lambda: discord.NotFound(_resp(404), "Unknown Channel"),
    lambda: discord.HTTPException(_resp(400), "Cannot send an empty message"),
    lambda: discord.DiscordServerError(_resp(503), "upstream"),
    lambda: discord.RateLimited(1.5),
    lambda: discord.LoginFailure("bad token"),
    lambda: discord.ClientException("closed"),
    lambda: discord.InvalidData("weird"),
    lambda: discord.GatewayNotFound(),
    lambda: TimeoutError(),
    lambda: OSError("connection reset"),
    lambda: RuntimeError("anything"),
    lambda: ValueError("x"),
    lambda: KeyError("k"),
    lambda: AttributeError("no send on a category"),
]


@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_send_to_channel_survives_every_exception_type_from_send(make):
    class Bad:
        async def send(self, *a, **k):
            raise make()

    class C:
        def get_channel(self, cid):
            return Bad()

    assert await send_to_channel(C(), 1, "hi") is False
    await send_alert(C(), 1, "hi")


@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_send_to_channel_survives_every_exception_type_from_fetch(make):
    class C:
        def get_channel(self, cid):
            return None

        async def fetch_channel(self, cid):
            raise make()

    assert await send_to_channel(C(), 1, "hi") is False


@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_router_never_raises_whatever_the_sender_throws(db_path, make):
    spy = Spy(exc=make())
    router = Router(db_path, spy, OWNER)
    await router.alert_owner("x")
    await router.notify_guild(1, "x")
    await router.send_report(1, A1, "x")
    assert [n.text for n in _notices(db_path, 1)] == ["x"]  # the record survives a dead Discord


async def test_a_cancelled_send_is_not_swallowed(db_path):
    """Shutdown must still be able to cancel the router: CancelledError is not an Exception."""

    async def send(channel_id, text):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await Router(db_path, send, OWNER).alert_owner("x")


async def test_channel_without_a_send_method_is_a_false_not_a_crash():
    class Category:
        pass

    class C:
        def get_channel(self, cid):
            return Category()

    assert await send_to_channel(C(), 1, "hi") is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Router._deliver and send_to_channel have no timeout: a Discord send that hangs "
        "blocks alert_owner/notify_guild (and so a digest run or a sweep) forever."
    ),
)
@pytest.mark.parametrize("door", ["owner", "guild", "report"])
async def test_a_hanging_send_does_not_hang_the_caller(db_path, door):
    router = Router(db_path, Spy(hang=True), OWNER)
    call = {
        "owner": lambda: router.alert_owner("x"),
        "guild": lambda: router.notify_guild(1, "x"),
        "report": lambda: router.send_report(1, A1, "x"),
    }[door]()
    await asyncio.wait_for(call, timeout=0.3)


async def test_a_locked_database_is_waited_out_then_the_notice_is_recorded_and_posted(db_path):
    ready, release = threading.Event(), threading.Event()

    def hold():
        with closing(connect(db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO digests (run_date, status, created_at, updated_at) "
                "VALUES ('2026-09-01', 'ok', 'now', 'now')"
            )
            ready.set()
            release.wait(5)
            conn.rollback()

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(5)
    spy = Spy()
    task = asyncio.create_task(Router(db_path, spy, OWNER).notify_guild(1, "patience"))
    await asyncio.sleep(0.2)
    assert not task.done()  # blocked on the lock, not failed
    release.set()
    await asyncio.wait_for(task, 5)
    thread.join()
    assert spy.sent == [(A1, "patience")]
    assert [n.text for n in _notices(db_path, 1)] == ["patience"]


async def test_a_failed_notice_write_posts_nothing_and_does_not_raise(db_path, monkeypatch):
    """Documented choice: no record, no post (the record is the source of truth)."""

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "add_notice", locked)
    spy = Spy()
    await Router(db_path, spy, OWNER).notify_guild(1, "lost")
    assert spy.sent == []


async def test_concurrent_writers_leave_exactly_twenty_and_post_every_one(db_path):
    spy = Spy()
    router = Router(db_path, spy, OWNER)
    await asyncio.gather(*(router.notify_guild(1, f"n{i}") for i in range(60)))
    assert len(spy.to(A1)) == 60
    notices = _notices(db_path, 1)
    assert len(notices) == 20
    assert len({n.text for n in notices}) == 20


async def test_pruning_one_guild_leaves_the_others_notices_alone(db_path):
    spy = Spy()
    router = Router(db_path, spy, OWNER)
    await router.notify_guild(2, "keep me")
    for i in range(30):
        await router.notify_guild(1, f"n{i}")
    assert [n.text for n in _notices(db_path, 2)] == ["keep me"]
    assert len(_notices(db_path, 1)) == 20


# --- v2's send_alert, unchanged ---


class _Recorder:
    def __init__(self):
        self.calls = []

    async def send(self, text, **kwargs):
        self.calls.append((text, kwargs))


class _RecClient:
    def __init__(self, chan, cached=True):
        self.chan, self.cached, self.fetched = chan, cached, False

    def get_channel(self, cid):
        return self.chan if self.cached else None

    async def fetch_channel(self, cid):
        self.fetched = True
        return self.chan


@pytest.mark.parametrize(
    "text",
    ["short", "x" * 2000, "x" * 2001, "\U0001f600" * 1500, "\u00e9" * 5000],
    ids=["short", "exact", "over", "astral", "accents"],
)
async def test_send_alert_sends_exactly_the_v2_capped_text_with_no_mentions(text):
    chan = _Recorder()
    await send_alert(_RecClient(chan), 5, text)
    ((sent, kwargs),) = chan.calls
    assert sent == _truncate_utf16(text, 2000, suffix="…")
    assert set(kwargs) == {"allowed_mentions"}
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_send_alert_with_no_channel_never_touches_the_client():
    class Boom:
        def get_channel(self, cid):
            raise AssertionError("must not be called")

    await send_alert(Boom(), None, "x")


async def test_send_alert_falls_back_to_fetch_when_the_cache_misses():
    chan = _Recorder()
    client = _RecClient(chan, cached=False)
    await send_alert(client, 5, "hi")
    assert client.fetched and [c[0] for c in chan.calls] == ["hi"]
