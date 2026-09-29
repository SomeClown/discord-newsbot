"""Tests for `run_daily_quote`: a real temp database, fake post and alert, seeded rng.

Quote text is synthetic (teapots and biscuits) except in the integration test,
which serves the committed Wikiquote fixture through a mock transport. Nothing
here touches the network; every client has a transport that can't.
"""

import asyncio
import json
import random
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.config import QuoteSourceCfg
from newsbot.lounge import daily
from newsbot.lounge.daily import QuoteDeps, run_daily_quote
from newsbot.lounge.quotes import Quote, quote_hash, render_quote_message
from newsbot.lounge.sources import load_source
from newsbot.store.db import connect, migrate

FIXTURES = Path(__file__).parent / "fixtures" / "wikiquote"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DAY = date(2026, 9, 29)
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


def _forbidden(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


class Spy:
    """Collects what `post` and `alert` were asked to send."""

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
    return path


def _file(tmp_path: Path, name: str, quotes: list[str]) -> QuoteSourceCfg:
    path = tmp_path / name
    path.write_text("\n%\n".join(quotes) + "\n", encoding="utf-8")
    return QuoteSourceCfg(kind="file", value=str(path))


def _missing(tmp_path: Path, name: str = "gone.txt") -> QuoteSourceCfg:
    return QuoteSourceCfg(kind="file", value=str(tmp_path / name))


def _deps(db_path, sources, spy, *, seed=1, day=DAY, http=None, now=NOW) -> QuoteDeps:
    return QuoteDeps(
        sources=sources,
        db_path=db_path,
        http=http or httpx.AsyncClient(transport=httpx.MockTransport(_forbidden)),
        local_day=day,
        now=lambda: now,
        post=spy.post,
        alert=spy.alert,
        rng=_rng(seed),
    )


def _rows(db_path: str, key: str | None = None) -> list[sqlite3.Row]:
    with closing(connect(db_path)) as conn:
        if key is None:
            return conn.execute("SELECT * FROM lounge_quotes_used").fetchall()
        return conn.execute(
            "SELECT * FROM lounge_quotes_used WHERE source_key = ?", (key,)
        ).fetchall()


def _last_day(db_path: str) -> str | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM lounge_state WHERE key='last_quote_date'").fetchone()
    return row["value"] if row else None


async def test_posts_a_quote_and_records_it(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["The teapot is not a suspect."])
    out = await run_daily_quote(_deps(db_path, [src], spy))
    assert out.status == "posted"
    assert out.message_id == 1001
    assert out.source_key == src.key
    assert spy.posts == [render_quote_message(Quote("The teapot is not a suspect."))]
    assert spy.alerts == []
    assert [r["quote_hash"] for r in _rows(db_path)] == [quote_hash("The teapot is not a suspect.")]
    assert _last_day(db_path) == "2026-09-29"


async def test_already_posted_today_loads_nothing(tmp_path, db_path, monkeypatch):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["One.", "Two."])
    assert (await run_daily_quote(_deps(db_path, [src], spy))).status == "posted"

    async def boom(*args, **kwargs):
        raise AssertionError("load_source must not be called")

    monkeypatch.setattr(daily, "load_source", boom)
    out = await run_daily_quote(_deps(db_path, [src], spy, seed=2))
    assert out.status == "already_posted"
    assert len(spy.posts) == 1
    assert spy.alerts == []


async def test_a_new_day_posts_again(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["One.", "Two.", "Three."])
    for offset in range(3):
        deps = _deps(db_path, [src], spy, day=DAY + timedelta(days=offset), seed=offset)
        assert (await run_daily_quote(deps)).status == "posted"
    assert len(set(spy.posts)) == 3


async def test_lost_race_is_already_posted_without_a_post(tmp_path, db_path, monkeypatch):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["One.", "Two."])
    assert (await run_daily_quote(_deps(db_path, [src], spy))).status == "posted"
    # Pretend the pre-check ran just before the other trigger committed.
    monkeypatch.setattr(daily, "_already_posted", lambda db, day: False)
    out = await run_daily_quote(_deps(db_path, [src], spy, seed=5))
    assert out.status == "already_posted"
    assert len(spy.posts) == 1
    assert spy.alerts == []
    assert len(_rows(db_path)) == 1


async def test_mixing_is_uniform_across_sources(tmp_path, db_path):
    spy = Spy()
    big = _file(tmp_path, "big.txt", [f"Big quote number {i}." for i in range(1000)])
    small = _file(tmp_path, "small.txt", ["Tiny one.", "Tiny two."])
    rng = _rng(42)
    first: dict[str, int] = {big.key: 0, small.key: 0}
    runs = 300
    for i in range(runs):
        deps = _deps(db_path, [big, small], spy, day=DAY + timedelta(days=i))
        deps.rng = rng
        out = await run_daily_quote(deps, force=True)
        assert out.status == "posted"
        first[out.source_key] += 1
    assert 0.35 * runs < first[small.key] < 0.65 * runs, first


async def test_failover_posts_from_the_second_source_with_one_combined_alert(tmp_path, db_path):
    spy = Spy()
    bad = _missing(tmp_path)
    good = _file(tmp_path, "good.txt", ["The biscuits are safe."])
    out = await run_daily_quote(_deps(db_path, [bad, good], spy))
    assert out.status == "posted"
    assert out.source_key == good.key
    assert len(spy.alerts) == 1
    assert "gone.txt" in spy.alerts[0]
    assert "no saved copy" in spy.alerts[0]
    assert "used another source" in spy.alerts[0]
    assert "biscuits" not in spy.alerts[0]


async def test_every_source_failing_skips_the_day_with_one_alert(tmp_path, db_path):
    spy = Spy()
    a, b = _missing(tmp_path, "a-gone.txt"), _missing(tmp_path, "b-gone.txt")
    out = await run_daily_quote(_deps(db_path, [a, b], spy))
    assert out.status == "skipped"
    assert spy.posts == []
    assert len(spy.alerts) == 1
    assert spy.alerts[0].startswith("newsbot: no lounge quote today. ")
    assert "a-gone.txt" in spy.alerts[0] and "b-gone.txt" in spy.alerts[0]
    # A skipped day isn't a done day: nothing recorded.
    assert _last_day(db_path) is None
    assert _rows(db_path) == []


async def test_each_source_is_tried_at_most_once_per_run(tmp_path, db_path, monkeypatch):
    spy = Spy()
    calls: list[str] = []
    real = load_source

    async def counting(src, **kwargs):
        calls.append(src.key)
        return await real(src, **kwargs)

    monkeypatch.setattr(daily, "load_source", counting)
    srcs = [_missing(tmp_path, f"gone{i}.txt") for i in range(4)]
    await run_daily_quote(_deps(db_path, srcs, spy))
    assert sorted(calls) == sorted(s.key for s in srcs)


async def test_no_sources_at_all_still_says_something(tmp_path, db_path):
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, [], spy))
    assert out.status == "skipped"
    assert spy.alerts == ["newsbot: no lounge quote today. no sources configured"]


async def test_a_fallback_source_posts_with_one_alert(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["The teapot is not a suspect."])
    assert (await run_daily_quote(_deps(db_path, [src], spy))).status == "posted"
    await asyncio.to_thread(Path(src.value).unlink)
    out = await run_daily_quote(
        _deps(db_path, [src], spy, day=DAY + timedelta(days=1)), force=False
    )
    assert out.status == "posted"
    assert len(spy.posts) == 2
    assert len(spy.alerts) == 1
    assert "used its saved copy from 2026-09-29" in spy.alerts[0]
    assert "teapot" not in spy.alerts[0]


async def test_url_credentials_and_query_never_reach_the_admin(tmp_path, db_path):
    spy = Spy()
    url = "https://hunter2:s3cret@example.test/raw/quotes.txt?token=abc123"
    src = QuoteSourceCfg(kind="url", value=url)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    out = await run_daily_quote(_deps(db_path, [src], spy, http=http))
    assert out.status == "skipped"
    text = spy.alerts[0]
    assert "example.test/raw/quotes.txt" in text
    for secret in ("hunter2", "s3cret", "abc123", "token"):
        assert secret not in text


async def test_per_source_decks_are_independent_and_reshuffle_skips_own_last(tmp_path, db_path):
    spy = Spy()
    a = _file(tmp_path, "a.txt", ["A one.", "A two."])
    b = _file(tmp_path, "b.txt", ["B one.", "B two."])
    days = iter(DAY + timedelta(days=i) for i in range(10))

    async def run(src):
        return await run_daily_quote(_deps(db_path, [src], spy, day=next(days)))

    await run(a)
    await run(a)
    await run(b)
    b_rows = [r["quote_hash"] for r in _rows(db_path, b.key)]
    assert len(_rows(db_path, a.key)) == 2
    a_last = spy.posts[1]

    await run(a)  # A's deck is spent: a reshuffle, with B's post in between.
    assert spy.posts[3] != a_last
    assert len(_rows(db_path, a.key)) == 1
    assert [r["quote_hash"] for r in _rows(db_path, b.key)] == b_rows


async def test_force_posts_a_second_quote_and_records_it(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["One.", "Two.", "Three."])
    await run_daily_quote(_deps(db_path, [src], spy))
    out = await run_daily_quote(_deps(db_path, [src], spy, seed=9), force=True)
    assert out.status == "posted"
    assert len(spy.posts) == 2
    assert spy.posts[0] != spy.posts[1]
    assert len(_rows(db_path, src.key)) == 2
    assert _last_day(db_path) == "2026-09-29"


async def test_post_failure_alerts_once_and_keeps_the_quote_used(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["The teapot is not a suspect."])

    async def failing_post(text: str) -> int:
        raise RuntimeError("403 Forbidden (error code: 50013): Missing Permissions\nmore detail")

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    out = await run_daily_quote(deps)
    assert out.status == "post_failed"
    assert spy.alerts == [
        "newsbot: couldn't post today's quote in the lounge: "
        "403 Forbidden (error code: 50013): Missing Permissions"
    ]
    assert _last_day(db_path) == "2026-09-29"
    assert len(_rows(db_path)) == 1
    # No retry the same day.
    again = await run_daily_quote(_deps(db_path, [src], spy))
    assert again.status == "already_posted"


async def test_post_failure_that_echoes_the_quote_names_only_the_error_class(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["The teapot is not a suspect."])

    async def failing_post(text: str) -> int:
        raise ValueError(f"bad content: {text.splitlines()[1]}")

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    out = await run_daily_quote(deps)
    assert out.status == "post_failed"
    assert "teapot" not in spy.alerts[0]
    assert spy.alerts[0].endswith("ValueError")


async def test_post_failure_folds_source_notes_into_the_same_alert(tmp_path, db_path):
    spy = Spy()
    bad = _missing(tmp_path)
    good = _file(tmp_path, "good.txt", ["One."])

    async def failing_post(text: str) -> int:
        raise RuntimeError("nope")

    deps = _deps(db_path, [bad, good], spy)
    deps.post = failing_post
    out = await run_daily_quote(deps)
    assert out.status == "post_failed"
    assert len(spy.alerts) == 1
    assert "gone.txt" in spy.alerts[0]


async def test_a_broken_alert_channel_does_not_undo_the_post(tmp_path, db_path):
    spy = Spy()
    bad = _missing(tmp_path)
    good = _file(tmp_path, "good.txt", ["One."])

    async def failing_alert(text: str) -> None:
        raise RuntimeError("admin channel is on fire")

    deps = _deps(db_path, [bad, good], spy)
    deps.alert = failing_alert
    out = await run_daily_quote(deps)
    assert out.status == "posted"
    assert len(spy.posts) == 1


async def test_cancellation_propagates_from_post(tmp_path, db_path):
    spy = Spy()
    src = _file(tmp_path, "a.txt", ["One."])

    async def cancelled_post(text: str) -> int:
        raise asyncio.CancelledError

    deps = _deps(db_path, [src], spy)
    deps.post = cancelled_post
    with pytest.raises(asyncio.CancelledError):
        await run_daily_quote(deps)
    assert spy.alerts == []


async def test_cancellation_propagates_from_load(tmp_path, db_path, monkeypatch):
    spy = Spy()

    async def cancelled_load(src, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(daily, "load_source", cancelled_load)
    with pytest.raises(asyncio.CancelledError):
        await run_daily_quote(_deps(db_path, [_missing(tmp_path)], spy))
    assert spy.alerts == []


async def test_alert_text_is_one_capped_message(tmp_path, db_path):
    spy = Spy()
    srcs = [_missing(tmp_path, f"{'x' * 200}{i}.txt") for i in range(30)]
    await run_daily_quote(_deps(db_path, srcs, spy))
    assert len(spy.alerts) == 1
    assert len(spy.alerts[0]) <= 1900


async def test_wikiquote_post_includes_the_page_link(tmp_path, db_path):
    spy = Spy()
    html = (FIXTURES / "oscar_wilde.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"parse": {"title": "Oscar Wilde", "revid": 7, "text": html}}
        return httpx.Response(200, content=json.dumps(body).encode(), headers=JSON_HEADERS)

    src = QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await run_daily_quote(_deps(db_path, [src], spy, http=http))
    assert out.status == "posted"
    assert "From Wikiquote: <https://en.wikiquote.org/wiki/Oscar_Wilde>" in spy.posts[0]
    assert spy.alerts == []


async def test_integration_several_days_no_repeats_and_once_a_day(tmp_path, db_path):
    """A Wikiquote source (fixture through a mock transport) and a file, over 12 days."""
    html = (FIXTURES / "oscar_wilde.html").read_text(encoding="utf-8")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        body = {"parse": {"title": "Oscar Wilde", "revid": 7, "text": html}}
        return httpx.Response(200, content=json.dumps(body).encode(), headers=JSON_HEADERS)

    wiki = QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")
    mine = _file(tmp_path, "mine.txt", [f"Home-grown quote {i}." for i in range(12)])
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spy = Spy()
    rng = _rng(7)
    by_source: dict[str, list[str]] = {wiki.key: [], mine.key: []}

    for i in range(12):
        day = DAY + timedelta(days=i)
        now = NOW + timedelta(days=i)
        deps = _deps(db_path, [wiki, mine], spy, day=day, http=http, now=now)
        deps.rng = rng
        out = await run_daily_quote(deps)
        assert out.status == "posted"
        by_source[out.source_key].append(spy.posts[-1])
        # Same day again: the guard holds and nothing more goes out.
        deps2 = _deps(db_path, [wiki, mine], spy, day=day, http=http, now=now)
        deps2.rng = rng
        assert (await run_daily_quote(deps2)).status == "already_posted"

    assert len(spy.posts) == 12
    assert spy.alerts == []
    for key, posts in by_source.items():
        assert len(posts) == len(set(posts)), key
    assert len(_rows(db_path)) == 12
    # Days 0 to 6 sit inside Wikiquote's week; day 7 onward may refetch.
    assert len(requests) <= 2
