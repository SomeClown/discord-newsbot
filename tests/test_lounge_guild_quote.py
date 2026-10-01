"""`run_daily_quote` with a `guild_id` (design.md §15, plan task 12).

The quote run itself is covered by tests/test_lounge_daily.py. What's new
here is whose deck and whose date it reads, what happens when the server
leaves mid-run, and the friend's server after the import: same eight
sources, decks carried over, no quote posted twice.
"""

from __future__ import annotations

import random
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.config import QuoteSourceCfg, load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.lounge import daily
from newsbot.lounge.daily import QuoteDeps, run_daily_quote
from newsbot.lounge.quotes import Quote, quote_hash, render_quote_message
from newsbot.lounge.sources import LoadedSource
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import LoungeSettings

G1, G2 = 100000000000000001, 100000000000000002
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DAY = date(2026, 10, 1)
FIXTURES = Path(__file__).parent / "fixtures"


class Spy:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.alerts: list[str] = []

    async def post(self, text: str) -> int:
        self.posts.append(text)
        return 1000 + len(self.posts)

    async def alert(self, text: str) -> None:
        self.alerts.append(text)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
        for gid in (G1, G2):
            repo.create_guild(conn, gid)
            repo.upsert_lounge(conn, LoungeSettings(gid, 50, True, "hi", True, "08:00", [], None))
    return path


def _file(tmp_path: Path, name: str, quotes: list[str]) -> QuoteSourceCfg:
    path = tmp_path / name
    path.write_text("\n%\n".join(quotes) + "\n", encoding="utf-8")
    return QuoteSourceCfg(kind="file", value=str(path))


def _deps(db_path, sources, spy, gid, *, seed=1, day=DAY) -> QuoteDeps:
    return QuoteDeps(
        sources=sources,
        db_path=db_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail(str(r.url)))),
        local_day=day,
        now=lambda: NOW,
        post=spy.post,
        alert=spy.alert,
        rng=random.Random(seed),  # noqa: S311
        guild_id=gid,
    )


def _date_of(db_path, gid):
    with closing(connect(db_path)) as conn:
        return repo.get_lounge(conn, gid).last_quote_date


async def test_a_guild_run_posts_and_writes_that_guilds_date_and_deck(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["The teapot is not a suspect."])
    out = await run_daily_quote(_deps(db_path, [src], spy, G1))
    assert out.status == "posted"
    assert spy.posts == [render_quote_message(Quote("The teapot is not a suspect."))]
    assert _date_of(db_path, G1) == "2026-10-01"
    assert _date_of(db_path, G2) is None
    with closing(connect(db_path)) as conn:
        assert repo.quote_deck_state(conn, src.key, G1).used == {
            quote_hash("The teapot is not a suspect.")
        }
        assert repo.quote_deck_state(conn, src.key, G2).used == frozenset()


async def test_the_once_a_day_guard_is_per_guild(tmp_path, db_path):
    src1 = _file(tmp_path, "a.txt", ["one", "two"])
    src2 = _file(tmp_path, "b.txt", ["uno", "dos"])
    spy1, spy2 = Spy(), Spy()
    assert (await run_daily_quote(_deps(db_path, [src1], spy1, G1))).status == "posted"
    assert (await run_daily_quote(_deps(db_path, [src1], spy1, G1))).status == "already_posted"
    assert (await run_daily_quote(_deps(db_path, [src2], spy2, G2))).status == "posted"
    assert len(spy1.posts) == 1 and len(spy2.posts) == 1


async def test_force_posts_a_second_quote_for_that_guild_only(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["one", "two", "three"])
    spy = Spy()
    await run_daily_quote(_deps(db_path, [src], spy, G1))
    out = await run_daily_quote(_deps(db_path, [src], spy, G1, seed=2), force=True)
    assert out.status == "posted" and len(spy.posts) == 2
    assert spy.posts[0] != spy.posts[1]


async def test_two_guilds_with_different_sources_keep_independent_decks(tmp_path, db_path):
    src1 = _file(tmp_path, "a.txt", ["a1", "a2", "a3"])
    src2 = _file(tmp_path, "b.txt", ["b1", "b2", "b3"])
    spy1, spy2 = Spy(), Spy()
    for day in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)):
        await run_daily_quote(_deps(db_path, [src1], spy1, G1, day=day, seed=day.day))
        await run_daily_quote(_deps(db_path, [src2], spy2, G2, day=day, seed=day.day + 9))
    # Three days over a three-quote deck: every quote exactly once, nobody's deck touched theirs.
    assert sorted(spy1.posts) == sorted(render_quote_message(Quote(t)) for t in ("a1", "a2", "a3"))
    assert sorted(spy2.posts) == sorted(render_quote_message(Quote(t)) for t in ("b1", "b2", "b3"))


async def test_a_guild_with_no_lounge_row_posts_nothing_and_says_nothing(tmp_path, db_path):
    with closing(connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM guild_lounge WHERE guild_id = ?", (G1,))
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["one"])
    out = await run_daily_quote(_deps(db_path, [src], spy, G1))
    assert out.status == "no_lounge"
    assert spy.posts == [] and spy.alerts == []


async def test_a_guild_removed_mid_quote_posts_nothing_and_raises_nothing(
    tmp_path, db_path, monkeypatch
):
    """The server leaves between loading the source and claiming the quote."""
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["one", "two"])

    async def load_then_leave(source, **kwargs):
        with closing(connect(db_path)) as conn:
            repo.delete_guild(conn, G1)
        return LoadedSource([Quote("one")], "fresh")

    monkeypatch.setattr(daily, "load_source", load_then_leave)
    out = await run_daily_quote(_deps(db_path, [src], spy, G1))
    assert out.status == "no_lounge"
    assert spy.posts == [] and spy.alerts == []
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lounge_quotes_used").fetchone()[0] == 0
        assert repo.get_lounge(conn, G2) is not None  # the other server is untouched


async def test_a_guild_deleted_after_the_claim_fails_the_post_quietly(tmp_path, db_path):
    """The post itself can fail for a gone server; that's the usual post_failed path."""
    src = _file(tmp_path, "a.txt", ["one"])
    spy = Spy()

    async def leave_then_fail(text: str) -> int:
        with closing(connect(db_path)) as conn:
            repo.delete_guild(conn, G1)
        raise RuntimeError("Unknown Channel")

    deps = _deps(db_path, [src], spy, G1)
    deps.post = leave_then_fail
    out = await run_daily_quote(deps)
    assert out.status == "post_failed"
    assert len(spy.alerts) == 1


async def test_the_friends_imported_deck_continues_with_no_repeats(tmp_path, v22_db):
    """v2.2 had used two of three quotes; after the import the third is all that's left."""
    cfg = load_config(FIXTURES / "config_v2_prodlike.yaml")
    wilde = "wikiquote:Oscar Wilde"
    q1, q2, q3 = Quote("First wit."), Quote("Second wit."), Quote("Third wit.")
    with closing(connect(v22_db)) as conn, conn:
        conn.execute("DELETE FROM lounge_quotes_used")
        for n, q in enumerate((q1, q2)):
            conn.execute(
                "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES (?, ?, ?)",
                (wilde, quote_hash(q.text), f"2026-09-2{n}T15:00:00+00:00"),
            )
        conn.execute(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES (?, ?, ?)",
            ("wikiquote:Mark Twain", "b" * 64, "2026-09-30T15:00:00+00:00"),
        )
    report = ensure_imported(v22_db, cfg, lambda: NOW)
    gid = report.guild_id

    with closing(connect(v22_db)) as conn:
        lounge = repo.get_lounge(conn, gid)
        assert len(lounge.quote_sources) == 8
        assert lounge.quote_time == "08:00" and lounge.last_quote_date == "2026-09-30"
        assert repo.quote_deck_state(conn, wilde, gid).used == {
            quote_hash(q1.text),
            quote_hash(q2.text),
        }
        assert repo.quote_deck_state(conn, "wikiquote:Mark Twain", gid).used == {"b" * 64}

    async def fake_load(source, **kwargs):
        return LoadedSource([q1, q2, q3], "fresh")

    spy = Spy()
    only_wilde = QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(daily, "load_source", fake_load)
        out = await run_daily_quote(_deps(str(v22_db), [only_wilde], spy, gid))
    assert out.status == "posted"
    assert spy.posts == [render_quote_message(q3)]  # the only unused one; no repeat
