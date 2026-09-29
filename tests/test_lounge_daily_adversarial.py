"""Adversarial tests for `run_daily_quote`, beyond `test_lounge_daily.py`.

The brief (test-engineer, 2026-09-29): `run_daily_quote` is the one function
that touches the database, a pile of sources, Discord and the admin channel in
a single breath, at 08:00, with nobody watching. So this file is mostly a
stopwatch and a crowd. The crowd is two runs (or eight) arriving in the same
second; the stopwatch is a cancel landing between any two awaits. After every
one of those the questions are the same: was the quote recorded as used, is
the day marked done, did anything get posted, and is the deck still a deck
and not a half-finished sentence?

The lock that keeps the scheduled job and `quote-now` from stepping on each
other lives in task 8. Everything here tests what `run_daily_quote` promises
on its own, which is to say through `claim_quote`'s `BEGIN IMMEDIATE`. That
turns out to be plenty, and also turns out to have some sharp corners; where
a test pins a behavior the owner might want different, its docstring says so.
Where a test found an actual bug it's an `xfail(strict)` with the reason
spelled out, so the fix flips it to a loud XPASS.

Everything is hermetic: a temp database, `httpx.MockTransport` for the net,
synthetic quotes (teapots and biscuits), and `monkeypatch` for the sources
when a real file would be slower than the point. The rng is always seeded.
"""

from __future__ import annotations

import asyncio
import contextvars
import random
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.bot.format import discord_len
from newsbot.config import QuoteSourceCfg
from newsbot.lounge import daily
from newsbot.lounge.daily import QuoteDeps, run_daily_quote
from newsbot.lounge.quotes import Quote, quote_hash, render_quote_message
from newsbot.lounge.sources import LoadedSource, load_source
from newsbot.store.db import connect, migrate
from newsbot.store.repo import claim_quote

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DAY = date(2026, 9, 29)


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


class _Ordered(random.Random):
    """An rng whose `sample` keeps the list order, so failover order is not a coin flip."""

    def sample(self, population, k, *, counts=None):
        return list(population)[:k]


def _forbidden(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


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
    return path


def _file(tmp_path: Path, name: str, quotes: list[str]) -> QuoteSourceCfg:
    path = tmp_path / name
    path.write_text("\n%\n".join(quotes) + "\n", encoding="utf-8")
    return QuoteSourceCfg(kind="file", value=str(path))


def _nowhere(name: str) -> QuoteSourceCfg:
    """A file source that doesn't exist, under a directory that doesn't either."""
    return QuoteSourceCfg(kind="file", value=f"/nowhere/{name}")


def _deps(db_path, sources, spy, *, seed=1, day=DAY, http=None, now=NOW, rng=None) -> QuoteDeps:
    return QuoteDeps(
        sources=sources,
        db_path=db_path,
        http=http or httpx.AsyncClient(transport=httpx.MockTransport(_forbidden)),
        local_day=day,
        now=lambda: now,
        post=spy.post,
        alert=spy.alert,
        rng=rng or _rng(seed),
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


def _assert_no_half_written_deck(db_path: str) -> None:
    """The claim is one transaction: either a row and a day, or neither."""
    rows, day = _rows(db_path), _last_day(db_path)
    assert (len(rows) > 0) == (day is not None), (len(rows), day)
    assert len({(r["source_key"], r["quote_hash"]) for r in rows}) == len(rows)


def _fake_loads(monkeypatch, table: dict[str, LoadedSource]) -> list[str]:
    """Replace `load_source` with a lookup by source key; returns the call log."""
    calls: list[str] = []

    async def fake(src, **kwargs):
        calls.append(src.key)
        return table[src.key]

    monkeypatch.setattr(daily, "load_source", fake)
    return calls


def _good(n: int, tag: str = "q") -> LoadedSource:
    return LoadedSource([Quote(f"{tag} teapot number {i}.") for i in range(n)], "fresh")


def _bad(why: str = "it is on fire") -> LoadedSource:
    return LoadedSource([], "fresh", why)


async def _until(predicate, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    async def poll() -> None:
        while not predicate():  # noqa: ASYNC110 (a threading.Event can't be awaited)
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


class _ThreadGate:
    """Holds a function that runs in a worker thread until the test lets it go."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()

    def wrap(self, real):
        def gated(*args, **kwargs):
            self.entered.set()
            assert self.release.wait(5), "the test never released the gate"
            try:
                return real(*args, **kwargs)
            finally:
                self.done.set()

        return gated


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- Concurrency -------------------------------------------------------------


async def test_eight_unforced_runs_racing_post_exactly_once(tmp_path, db_path, monkeypatch):
    """All eight pass the pre-check, all eight reach the claim; one wins."""
    src = _file(tmp_path, "a.txt", [f"Quote {i}." for i in range(10)])
    spy = Spy()
    barrier = asyncio.Barrier(8)
    real = load_source

    async def meet_first(s, **kwargs):
        await barrier.wait()  # nobody claims until everybody has passed the pre-check
        return await real(s, **kwargs)

    monkeypatch.setattr(daily, "load_source", meet_first)
    outcomes = await asyncio.gather(
        *(run_daily_quote(_deps(db_path, [src], spy, seed=i)) for i in range(8))
    )
    statuses = sorted(o.status for o in outcomes)
    assert statuses == ["already_posted"] * 7 + ["posted"]
    assert len(spy.posts) == 1
    assert spy.alerts == []
    assert len(_rows(db_path)) == 1
    assert _last_day(db_path) == "2026-09-29"
    _assert_no_half_written_deck(db_path)


async def test_racing_loser_is_silent_even_when_the_winner_fails_to_post(
    tmp_path, db_path, monkeypatch
):
    """One post attempt, one alert, and the loser doesn't add a second."""
    src = _file(tmp_path, "a.txt", ["One.", "Two.", "Three."])
    spy = Spy()
    barrier = asyncio.Barrier(2)
    real = load_source

    async def meet_first(s, **kwargs):
        await barrier.wait()
        return await real(s, **kwargs)

    attempts: list[str] = []

    async def failing_post(text: str) -> int:
        attempts.append(text)
        raise RuntimeError("Missing Permissions")

    monkeypatch.setattr(daily, "load_source", meet_first)
    deps = [_deps(db_path, [src], spy, seed=i) for i in (1, 2)]
    for d in deps:
        d.post = failing_post
    outcomes = await asyncio.gather(*(run_daily_quote(d) for d in deps))
    assert sorted(o.status for o in outcomes) == ["already_posted", "post_failed"]
    assert len(attempts) == 1
    assert len(spy.alerts) == 1
    assert len(_rows(db_path)) == 1


async def _forced_vs_scheduled(tmp_path, db_path, monkeypatch, forced_goes_first: bool):
    """Run one unforced and one forced run, with the claim order chosen by the test."""
    src = _file(tmp_path, "a.txt", ["One.", "Two.", "Three."])
    spy = Spy()
    who: contextvars.ContextVar[str] = contextvars.ContextVar("who")
    finished = {"scheduled": asyncio.Event(), "forced": asyncio.Event()}
    real = load_source

    async def ordered(s, **kwargs):
        waits_for = "scheduled" if who.get() == "forced" else "forced"
        if (who.get() == "forced") != forced_goes_first:
            await finished[waits_for].wait()
        return await real(s, **kwargs)

    monkeypatch.setattr(daily, "load_source", ordered)

    async def go(name: str, force: bool, seed: int):
        who.set(name)
        try:
            return await run_daily_quote(_deps(db_path, [src], spy, seed=seed), force=force)
        finally:
            finished[name].set()

    scheduled, forced = await asyncio.gather(go("scheduled", False, 1), go("forced", True, 2))
    return scheduled, forced, spy


async def test_forced_run_claiming_first_makes_the_scheduled_run_stand_down(
    tmp_path, db_path, monkeypatch
):
    scheduled, forced, spy = await _forced_vs_scheduled(tmp_path, db_path, monkeypatch, True)
    assert forced.status == "posted"
    assert scheduled.status == "already_posted"
    assert len(spy.posts) == 1
    assert len(_rows(db_path)) == 1
    _assert_no_half_written_deck(db_path)


async def test_scheduled_run_claiming_first_lets_the_forced_run_post_a_different_quote(
    tmp_path, db_path, monkeypatch
):
    """`force` bypasses only the date guard, so a forced run after a scheduled one posts too.

    That is the design (quote-now after a confirmation prompt), and the deck
    read happens after the claim, so the second post is not the first quote again.
    """
    scheduled, forced, spy = await _forced_vs_scheduled(tmp_path, db_path, monkeypatch, False)
    assert scheduled.status == "posted"
    assert forced.status == "posted"
    assert len(spy.posts) == 2
    assert spy.posts[0] != spy.posts[1]
    assert len(_rows(db_path)) == 2
    _assert_no_half_written_deck(db_path)


async def test_the_racing_loser_drops_its_source_notes_on_the_floor(tmp_path, db_path, monkeypatch):
    """A run that loses the claim says nothing, even about a source that failed for it.

    Documented: the failing source is only in the log then. The winner's own
    run reports whatever the winner saw.
    """
    bad, good = _nowhere("gone.txt"), _file(tmp_path, "good.txt", ["One.", "Two."])
    _fake_loads(monkeypatch, {bad.key: _bad(), good.key: _good(3)})
    spy = Spy()
    first = await run_daily_quote(_deps(db_path, [bad, good], spy, rng=_Ordered()))
    assert first.status == "posted"
    spy.alerts.clear()
    monkeypatch.setattr(daily, "_already_posted", lambda db, day: False)
    second = await run_daily_quote(_deps(db_path, [bad, good], spy, rng=_Ordered()))
    assert second.status == "already_posted"
    assert second.notes == ()
    assert spy.alerts == []


# --- Cancellation: what state does each await point leave? -------------------


async def test_cancel_during_the_pre_check_leaves_nothing(tmp_path, db_path, monkeypatch):
    """The pre-check thread still finishes, but it only reads."""
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()
    gate = _ThreadGate()
    monkeypatch.setattr(daily, "_already_posted", gate.wrap(daily._already_posted))
    task = asyncio.create_task(run_daily_quote(_deps(db_path, [src], spy)))
    await _until(gate.entered.is_set)
    await _cancel(task)
    gate.release.set()
    await asyncio.to_thread(gate.done.wait, 5)
    assert _rows(db_path) == []
    assert _last_day(db_path) is None
    assert spy.posts == [] and spy.alerts == []


async def test_cancel_during_a_source_load_leaves_nothing_and_says_nothing(
    tmp_path, db_path, monkeypatch
):
    """Even after an earlier source failed: the notes for it die with the run."""
    bad, slow = _nowhere("gone.txt"), _nowhere("slow.txt")
    entered = asyncio.Event()

    async def fake(src, **kwargs):
        if src.key == bad.key:
            return _bad()
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(daily, "load_source", fake)
    spy = Spy()
    task = asyncio.create_task(run_daily_quote(_deps(db_path, [bad, slow], spy, rng=_Ordered())))
    await asyncio.wait_for(entered.wait(), 5)
    await _cancel(task)
    assert _rows(db_path) == []
    assert _last_day(db_path) is None
    assert spy.posts == [] and spy.alerts == []


async def test_cancel_during_the_claim_thread_still_records_the_quote_but_never_posts(
    tmp_path, db_path, monkeypatch
):
    """The sharpest corner: a thread can't be cancelled, only abandoned.

    The task gets its CancelledError immediately, but the worker thread keeps
    going and commits the claim. Result: the quote is recorded as used and
    today is marked done, and nothing was ever posted. Record-then-post makes
    a shutdown at exactly this instant cost the lounge a day; the alternative
    (post-then-record) makes it cost a double post. The owner may want this
    documented in the runbook, since a restart at 08:00:00 could do it.
    """
    src = _file(tmp_path, "a.txt", ["One.", "Two."])
    spy = Spy()
    gate = _ThreadGate()
    monkeypatch.setattr(daily, "claim_quote", gate.wrap(daily.claim_quote))
    task = asyncio.create_task(run_daily_quote(_deps(db_path, [src], spy)))
    await _until(gate.entered.is_set)
    await _cancel(task)
    gate.release.set()
    await asyncio.to_thread(gate.done.wait, 5)

    assert len(_rows(db_path)) == 1
    assert _last_day(db_path) == "2026-09-29"
    assert spy.posts == [] and spy.alerts == []
    _assert_no_half_written_deck(db_path)
    # ...and a restart later that day finds the day done and posts nothing.
    again = await run_daily_quote(_deps(db_path, [src], spy, seed=3))
    assert again.status == "already_posted"
    assert spy.posts == []


async def test_cancel_during_post_leaves_the_quote_used_and_the_day_done(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()
    entered = asyncio.Event()

    async def slow_post(text: str) -> int:
        entered.set()
        await asyncio.sleep(3600)
        return 1

    deps = _deps(db_path, [src], spy)
    deps.post = slow_post
    task = asyncio.create_task(run_daily_quote(deps))
    await asyncio.wait_for(entered.wait(), 5)
    await _cancel(task)
    assert len(_rows(db_path)) == 1
    assert _last_day(db_path) == "2026-09-29"
    assert spy.alerts == []  # a cancelled post is not a failed post
    _assert_no_half_written_deck(db_path)


@pytest.mark.parametrize("route", ["skipped", "post_failed", "notes"])
async def test_cancel_during_the_admin_alert_leaves_the_db_as_the_route_had_it(
    tmp_path, db_path, route
):
    """`_tell_admin` swallows Exception only; a cancel goes straight through."""
    good = _file(tmp_path, "good.txt", ["One."])
    sources = [_nowhere("gone.txt")] if route == "skipped" else [_nowhere("gone.txt"), good]
    spy = Spy()
    entered = asyncio.Event()

    async def slow_alert(text: str) -> None:
        entered.set()
        await asyncio.sleep(3600)

    async def failing_post(text: str) -> int:
        raise RuntimeError("nope")

    deps = _deps(db_path, sources, spy, rng=_Ordered())
    deps.alert = slow_alert
    if route == "post_failed":
        deps.post = failing_post
    task = asyncio.create_task(run_daily_quote(deps))
    await asyncio.wait_for(entered.wait(), 5)
    await _cancel(task)
    if route == "skipped":
        assert _rows(db_path) == [] and _last_day(db_path) is None
    else:
        assert len(_rows(db_path)) == 1 and _last_day(db_path) == "2026-09-29"
    if route == "notes":
        assert len(spy.posts) == 1  # the post landed; only the notes were lost
    _assert_no_half_written_deck(db_path)


async def test_claim_that_raises_means_no_post_and_no_alert(tmp_path, db_path, monkeypatch):
    """A locked or broken database escapes `run_daily_quote` raw, before any post.

    Documented: the caller (task 8) is the only thing standing between this
    and a silent morning; there's no admin alert from in here.
    """
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daily, "claim_quote", locked)
    with pytest.raises(sqlite3.OperationalError):
        await run_daily_quote(_deps(db_path, [src], spy))
    assert spy.posts == [] and spy.alerts == []


async def test_unreadable_database_fails_the_pre_check_before_any_source_is_touched(
    tmp_path, monkeypatch
):
    calls = _fake_loads(monkeypatch, {})
    spy = Spy()
    deps = _deps(str(tmp_path / "no-such-dir" / "newsbot.db"), [_nowhere("a.txt")], spy)
    with pytest.raises(sqlite3.OperationalError):
        await run_daily_quote(deps)
    assert calls == []
    assert spy.posts == [] and spy.alerts == []


# --- Failover edges ----------------------------------------------------------


async def test_one_source_failing_is_one_alert_with_its_reason(tmp_path, db_path):
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, [_nowhere("only.txt")], spy))
    assert out.status == "skipped"
    assert spy.alerts == [
        "newsbot: no lounge quote today. file /nowhere/only.txt: file not found: /nowhere/only.txt"
    ]
    assert out.notes == ("file /nowhere/only.txt: file not found: /nowhere/only.txt",)


async def test_twenty_short_sources_failing_is_one_alert_naming_every_one(db_path, monkeypatch):
    srcs = [_nowhere(f"s{i:02d}.txt") for i in range(20)]
    _fake_loads(monkeypatch, {s.key: _bad("down") for s in srcs})
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, srcs, spy))
    assert out.status == "skipped"
    assert len(spy.alerts) == 1
    assert len(spy.alerts[0]) <= 1900
    for s in srcs:
        assert s.value in spy.alerts[0]
    assert len(out.notes) == 20


async def test_twenty_long_sources_hit_the_cap_and_the_tail_is_cut_with_an_ellipsis(
    db_path, monkeypatch
):
    """Past the cap the later sources just vanish from the message.

    Documented: `notes` on the outcome still lists all twenty, but the admin
    message is one `; `-joined line cut at 1,900, so with long names the last
    few sources are never named. The owner might prefer "and 7 more".
    """
    srcs = [_nowhere(f"{'x' * 150}{i:02d}.txt") for i in range(20)]
    _fake_loads(monkeypatch, {s.key: _bad("down") for s in srcs})
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, srcs, spy, rng=_Ordered()))
    (text,) = spy.alerts
    assert len(text) <= 1900
    assert text.endswith("…")
    assert text.startswith("newsbot: no lounge quote today. ")
    assert f"{'x' * 150}00.txt" in text
    assert f"{'x' * 150}19.txt" not in text
    assert len(out.notes) == 20


async def test_a_post_failure_keeps_its_headline_when_the_notes_overflow_the_cap(
    db_path, monkeypatch
):
    bad = [_nowhere(f"{'y' * 300}{i}.txt") for i in range(12)]
    good = _nowhere("good.txt")
    table = {s.key: _bad("down") for s in bad} | {good.key: _good(2)}
    _fake_loads(monkeypatch, table)
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise RuntimeError("Missing Permissions")

    deps = _deps(db_path, [*bad, good], spy, rng=_Ordered())
    deps.post = failing_post
    out = await run_daily_quote(deps)
    assert out.status == "post_failed"
    (text,) = spy.alerts
    assert len(text) <= 1900
    assert text.startswith("newsbot: couldn't post today's quote in the lounge: RuntimeError")
    assert all(len(line) <= 600 for line in text.splitlines()[1:])


async def test_a_load_source_that_raises_counts_as_a_failed_source(tmp_path, db_path, monkeypatch):
    """`load_source` promises never to raise; if it does, that source just failed.

    Changed from "the exception escapes the run": one broken loader shouldn't
    cost the day, so the run notes it (by exception class name only) and
    moves on to the healthy neighbor.
    """
    boom, good = _nowhere("boom.txt"), _file(tmp_path, "good.txt", ["One."])

    async def fake(src, **kwargs):
        if src.key == boom.key:
            raise RuntimeError("the never-raises contract has left the building")
        return _good(2)

    monkeypatch.setattr(daily, "load_source", fake)
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, [boom, good], spy, rng=_Ordered()))
    assert out.status == "posted"
    assert len(spy.posts) == 1
    assert "RuntimeError" in spy.alerts[0]
    assert "contract" not in spy.alerts[0]


async def test_a_load_source_that_raises_a_base_exception_still_propagates(db_path, monkeypatch):
    async def fake(src, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(daily, "load_source", fake)
    spy = Spy()
    with pytest.raises(asyncio.CancelledError):
        await run_daily_quote(_deps(db_path, [_nowhere("x.txt")], spy))
    assert spy.posts == [] and spy.alerts == []


async def test_duplicate_sources_are_each_tried_and_each_reported(db_path, monkeypatch):
    """The same source listed twice gets two turns and two lines when it's down.

    Documented: config doesn't dedupe, so a copy-paste in `sources:` doubles
    the load on a dead source and the noise in the alert.
    """
    a = _nowhere("dup.txt")
    calls = _fake_loads(monkeypatch, {a.key: _bad("down")})
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, [a, a], spy))
    assert out.status == "skipped"
    assert calls == [a.key, a.key]
    assert spy.alerts[0].count("file /nowhere/dup.txt") == 2


async def test_duplicate_sources_that_work_share_one_deck(tmp_path, db_path):
    a = _file(tmp_path, "a.txt", ["One.", "Two.", "Three."])
    spy = Spy()
    for i in range(3):
        deps = _deps(db_path, [a, a], spy, day=DAY + timedelta(days=i), seed=i)
        assert (await run_daily_quote(deps)).status == "posted"
    assert len(set(spy.posts)) == 3
    assert len(_rows(db_path, a.key)) == 3


async def test_a_source_whose_quotes_all_hash_alike_is_one_card_deck(tmp_path, db_path):
    """Three spellings of one quote are one card: it repeats daily, and nothing breaks."""
    src = _file(
        tmp_path, "a.txt", ["The teapot is calm.", "the TEAPOT is calm.", " The teapot  is calm. "]
    )
    spy = Spy()
    for i in range(4):
        out = await run_daily_quote(_deps(db_path, [src], spy, day=DAY + timedelta(days=i), seed=i))
        assert out.status == "posted"
    assert len(spy.posts) == 4
    assert [r["quote_hash"] for r in _rows(db_path)] == [quote_hash("The teapot is calm.")]
    assert spy.alerts == []


async def test_a_one_quote_source_repeats_itself_because_last_hash_is_all_it_has(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["Only card."])
    spy = Spy()
    for i in range(3):
        deps = _deps(db_path, [src], spy, day=DAY + timedelta(days=i))
        assert (await run_daily_quote(deps)).status == "posted"
    assert len(set(spy.posts)) == 1
    assert len(_rows(db_path)) == 1
    _assert_no_half_written_deck(db_path)


async def test_spent_deck_with_stale_hashes_and_the_last_quote_as_the_only_one_left(
    tmp_path, db_path
):
    """The list was edited down to the one quote that's also yesterday's: it posts it."""
    src = _file(tmp_path, "a.txt", ["Survivor."])
    with closing(connect(db_path)) as conn:
        for h, when in (
            (quote_hash("stale one"), 1),
            (quote_hash("stale two"), 2),
            (quote_hash("Survivor."), 3),
        ):
            claim_quote(
                conn,
                source_key=src.key,
                quote_hash=h,
                local_day=f"2026-09-0{when}",
                reshuffle=False,
                force=False,
                now=lambda when=when: NOW + timedelta(minutes=when),
            )
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, [src], spy))
    assert out.status == "posted"
    assert spy.posts == [render_quote_message(Quote("Survivor."))]
    assert [r["quote_hash"] for r in _rows(db_path, src.key)] == [quote_hash("Survivor.")]


async def test_a_spent_two_card_deck_reshuffles_to_the_other_card(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["Alpha.", "Beta."])
    with closing(connect(db_path)) as conn:
        for i, text in enumerate(["Beta.", "Alpha."]):  # Alpha is the last one used
            claim_quote(
                conn,
                source_key=src.key,
                quote_hash=quote_hash(text),
                local_day=f"2026-09-0{i + 1}",
                reshuffle=False,
                force=False,
                now=lambda i=i: NOW + timedelta(minutes=i),
            )
    spy = Spy()
    await run_daily_quote(_deps(db_path, [src], spy))
    assert spy.posts == [render_quote_message(Quote("Beta."))]
    assert [r["quote_hash"] for r in _rows(db_path, src.key)] == [quote_hash("Beta.")]


# --- The rng -----------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 7, 20])
async def test_the_order_is_a_permutation_for_any_seed(db_path, monkeypatch, n):
    srcs = [_nowhere(f"s{i}.txt") for i in range(n)]
    calls = _fake_loads(monkeypatch, {s.key: _bad("down") for s in srcs})
    original = list(srcs)
    orders = set()
    for seed in range(25):
        calls.clear()
        await run_daily_quote(_deps(db_path, srcs, Spy(), seed=seed), force=True)
        assert sorted(calls) == sorted(s.key for s in srcs)
        orders.add(tuple(calls))
    assert srcs == original  # sampling must not shuffle the caller's list
    if n >= 3:
        assert len(orders) > 1


async def test_sources_after_the_first_success_are_never_loaded(db_path, monkeypatch):
    a, b, c = (_nowhere(f"{x}.txt") for x in "abc")
    calls = _fake_loads(monkeypatch, {a.key: _bad(), b.key: _good(2), c.key: _good(2)})
    await run_daily_quote(_deps(db_path, [a, b, c], Spy(), rng=_Ordered()))
    assert calls == [a.key, b.key]


@pytest.mark.parametrize(
    "sizes",
    [
        pytest.param([1000, 2], id="two"),
        pytest.param([1000, 5, 1], id="three"),
        pytest.param([500, 300, 100, 50, 10, 2, 1], id="seven"),
    ],
)
async def test_mixing_is_fair_however_uneven_the_sources(db_path, monkeypatch, sizes):
    """Chi-square on which source posts: uniform by source, not by quote count.

    Fixed seed, so it's deterministic; the bound is generous (df at most 6,
    where 22.5 is the 0.1% point) so it flags a real skew, not bad luck.
    """
    srcs = [_nowhere(f"s{i}.txt") for i in range(len(sizes))]
    _fake_loads(
        monkeypatch,
        {s.key: _good(n, f"s{i}") for i, (s, n) in enumerate(zip(srcs, sizes, strict=True))},
    )
    rng = _rng(20260929)
    runs = 150 * len(sizes)
    seen = {s.key: 0 for s in srcs}
    spy = Spy()
    for _ in range(runs):
        deps = _deps(db_path, srcs, spy)
        deps.rng = rng
        seen[(await run_daily_quote(deps, force=True)).source_key] += 1
    expected = runs / len(sizes)
    chi = sum((count - expected) ** 2 / expected for count in seen.values())
    assert chi < 22.5, (seen, chi)


async def test_a_permanently_failing_source_hands_its_share_out_evenly(db_path, monkeypatch):
    dead, big, small = (_nowhere(f"{x}.txt") for x in ("dead", "big", "small"))
    _fake_loads(monkeypatch, {dead.key: _bad(), big.key: _good(900), small.key: _good(2)})
    rng, seen, spy = _rng(11), {big.key: 0, small.key: 0}, Spy()
    for _ in range(600):
        deps = _deps(db_path, [dead, big, small], spy)
        deps.rng = rng
        seen[(await run_daily_quote(deps, force=True)).source_key] += 1
    assert 240 < seen[small.key] < 360, seen


# --- Admin text hygiene ------------------------------------------------------


async def test_admin_text_never_carries_quote_or_attribution_text(db_path, monkeypatch):
    bad, good = _nowhere("gone.txt"), _nowhere("good.txt")
    quote = Quote("The kettle whistles at midnight.", attribution="Mabel Quince, Tea Leaves (1901)")
    _fake_loads(monkeypatch, {bad.key: _bad(), good.key: LoadedSource([quote], "fresh")})
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise RuntimeError("Missing Permissions")

    deps = _deps(db_path, [bad, good], spy, rng=_Ordered())
    deps.post = failing_post
    await run_daily_quote(deps)
    for text in spy.alerts:
        assert "kettle" not in text and "Mabel" not in text and "Tea Leaves" not in text


async def test_url_secrets_fragments_and_hops_never_reach_any_admin_line(db_path):
    """Credentials, query, fragment and a redirect target's secrets, on every alert route."""
    url = "https://hunter2:s3cret@example.test/raw/q.txt?token=abc123#frag-xyz"
    hop = "https://other.test/next?sig=zzz999"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.test":
            return httpx.Response(302, headers={"location": "http://plain.test/x?k=v"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spy = Spy()
    srcs = [QuoteSourceCfg(kind="url", value=url), QuoteSourceCfg(kind="url", value=hop)]
    out = await run_daily_quote(_deps(db_path, srcs, spy, http=http))
    assert out.status == "skipped"
    blob = spy.alerts[0] + "\n".join(out.notes)
    for secret in ("hunter2", "s3cret", "abc123", "token", "frag-xyz", "zzz999", "sig=", "k=v"):
        assert secret not in blob, secret
    assert "example.test/raw/q.txt" in blob


async def test_url_failures_named_in_notes_and_post_alerts_are_redacted_too(tmp_path, db_path):
    url = "https://user:pw@example.test/q.txt?key=k3y"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spy = Spy()
    bad = QuoteSourceCfg(kind="url", value=url)
    good = _file(tmp_path, "good.txt", ["One."])

    async def failing_post(text: str) -> int:
        raise RuntimeError("Missing Permissions")

    deps = _deps(db_path, [bad, good], spy, http=http, rng=_Ordered())
    deps.post = failing_post
    await run_daily_quote(deps)
    assert "example.test/q.txt" in spy.alerts[0]
    for secret in ("user:pw", "pw@", "key=", "k3y"):
        assert secret not in spy.alerts[0]


async def test_a_hostile_path_in_a_note_stays_on_its_own_line(tmp_path, db_path):
    """Notes go through `plain_line`, so a newline in a path can't fake a second note."""
    bad = QuoteSourceCfg(kind="file", value="/nowhere/a\nnewsbot: everything is fine\nb.txt")
    good = _file(tmp_path, "good.txt", ["One."])
    spy = Spy()
    await run_daily_quote(_deps(db_path, [bad, good], spy, rng=_Ordered()))
    (text,) = spy.alerts
    assert text.count("\n") == 1  # header, then exactly one note
    assert text.splitlines()[1].startswith("file /nowhere/a newsbot: everything is fine b.txt")


async def test_a_hostile_path_cannot_add_lines_to_the_skipped_alert(db_path):
    bad = QuoteSourceCfg(kind="file", value="/nowhere/a\n@here everyone panic\nb.txt")
    spy = Spy()
    await run_daily_quote(_deps(db_path, [bad], spy))
    assert "\n" not in spy.alerts[0]


@pytest.mark.parametrize("route", ["skipped", "noted"])
@pytest.mark.parametrize("hostile", ["@everyone", "<@123456>", "[click](https://evil.example)"])
async def test_source_names_arrive_inert_in_admin_text(tmp_path, db_path, route, hostile):
    bad = _nowhere(f"{hostile}.txt")
    sources = [bad] if route == "skipped" else [bad, _file(tmp_path, "good.txt", ["One."])]
    spy = Spy()
    await run_daily_quote(_deps(db_path, sources, spy, rng=_Ordered()))
    assert hostile not in spy.alerts[0]


async def test_a_post_error_mentioning_everyone_is_defused(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise RuntimeError("cannot ping @everyone here")

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    await run_daily_quote(deps)
    assert "@everyone" not in spy.alerts[0]


async def test_a_post_error_message_never_reaches_the_alert_only_its_class(tmp_path, db_path):
    """Changed from "first line, at most 200 characters": the message is left out entirely."""
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise RuntimeError("\n\n   first real line " + "z" * 500 + "\nsecond line\n")

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    await run_daily_quote(deps)
    assert spy.alerts == ["newsbot: couldn't post today's quote in the lounge: RuntimeError"]


@pytest.mark.parametrize("message", ["", "   ", "\n\n", "\n \n"])
async def test_a_blank_post_error_names_the_exception_class(tmp_path, db_path, message):
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise TimeoutError(message)

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    await run_daily_quote(deps)
    assert spy.alerts[0].endswith(": TimeoutError")


async def test_a_one_character_quote_makes_every_post_error_collapse_to_its_class(
    tmp_path, db_path
):
    """Documented: any error text that contains the quote's letters is treated as an echo.

    With a quote like "a" that is every error containing an "a", so the admin
    loses the useful part ("Cannot send messages" becomes "RuntimeError").
    Harmless while quotes are sentences; worth knowing if the owner's file
    ever holds a one-letter entry.
    """
    src = _file(tmp_path, "a.txt", ["a"])
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise RuntimeError("Cannot send messages here")

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    await run_daily_quote(deps)
    assert spy.alerts[0].endswith(": RuntimeError")


@pytest.mark.parametrize(
    "quote, echo",
    [
        pytest.param(
            Quote("**Bold** teapot"),
            lambda q: r"bad content: \*\*Bold\*\* teapot",
            id="escaped-markdown-echo",
        ),
        pytest.param(
            Quote("Line one is calm.\nLine two is a biscuit."),
            lambda q: "bad line: Line two is a biscuit.",
            id="one-line-of-a-multiline-quote",
        ),
        pytest.param(
            Quote("The kettle sings.", attribution="Mabel Quince, Tea Leaves (1901)"),
            lambda q: "bad footer: Mabel Quince, Tea Leaves (1901)",
            id="attribution-echo",
        ),
    ],
)
async def test_post_error_echoes_of_the_quote_never_reach_the_admin(
    db_path, monkeypatch, quote, echo
):
    src = _nowhere("q.txt")
    _fake_loads(monkeypatch, {src.key: LoadedSource([quote], "fresh")})
    spy = Spy()

    async def failing_post(text: str) -> int:
        raise ValueError(echo(quote))

    deps = _deps(db_path, [src], spy)
    deps.post = failing_post
    await run_daily_quote(deps)
    (text,) = spy.alerts
    for fragment in ("teapot", "biscuit", "Mabel", "Tea Leaves"):
        assert fragment not in text


async def test_combining_characters_keep_the_alert_within_limits(db_path, monkeypatch):
    srcs = [_nowhere("é".replace("é", "é") * 120 + f"{i}.txt") for i in range(30)]
    _fake_loads(monkeypatch, {s.key: _bad("é" * 300) for s in srcs})
    spy = Spy()
    await run_daily_quote(_deps(db_path, srcs, spy))
    (text,) = spy.alerts
    assert len(text) <= 1900
    assert discord_len(text) <= 2000


async def test_astral_characters_keep_the_alert_within_discords_limit(db_path):
    url = "https://example.test/" + "\U0001f36a" * 1500
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    spy = Spy()
    await run_daily_quote(_deps(db_path, [QuoteSourceCfg(kind="url", value=url)], spy, http=http))
    (text,) = spy.alerts
    assert len(text) <= 1900  # the number the code checks
    assert discord_len(text) <= 2000  # the number Discord checks


async def test_each_note_line_is_capped_at_600_characters(tmp_path, db_path):
    bad = _nowhere("w" * 1500 + ".txt")
    good = _file(tmp_path, "good.txt", ["One."])
    spy = Spy()
    out = await run_daily_quote(_deps(db_path, [bad, good], spy, rng=_Ordered()))
    assert out.status == "posted"
    note = spy.alerts[0].splitlines()[1]
    assert len(note) == 600 and note.endswith("…")
    assert len(out.notes[0]) == 600


# --- The post contract -------------------------------------------------------


@pytest.mark.parametrize("weird", [None, 0, "1234", 1.5])
async def test_post_returning_something_odd_is_still_posted_and_passed_through(
    tmp_path, db_path, weird
):
    """Documented: `message_id` is whatever `post` returned; nothing checks the type."""
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    async def odd_post(text: str):
        spy.posts.append(text)
        return weird

    deps = _deps(db_path, [src], spy)
    deps.post = odd_post
    out = await run_daily_quote(deps)
    assert out.status == "posted"
    assert out.message_id == weird
    assert len(_rows(db_path)) == 1


class _Fatal(BaseException):
    pass


async def test_a_base_exception_from_post_is_not_swallowed(tmp_path, db_path):
    """Only `Exception` is caught: the quote stays recorded, no alert, the error escapes."""
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    async def fatal_post(text: str) -> int:
        raise _Fatal

    deps = _deps(db_path, [src], spy)
    deps.post = fatal_post
    with pytest.raises(_Fatal):
        await run_daily_quote(deps)
    assert spy.alerts == []
    assert len(_rows(db_path)) == 1 and _last_day(db_path) == "2026-09-29"


def test_keyboard_interrupt_from_post_is_not_swallowed(tmp_path, db_path):
    """Run on a private loop in a thread: asyncio re-raises KeyboardInterrupt out of the loop."""
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()
    result: dict[str, object] = {}

    async def interrupted_post(text: str) -> int:
        raise KeyboardInterrupt

    async def main() -> None:
        deps = _deps(db_path, [src], spy)
        deps.post = interrupted_post
        await run_daily_quote(deps)

    def runner() -> None:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            result["interrupted"] = True

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(20)
    assert result == {"interrupted": True}
    assert spy.alerts == []
    assert len(_rows(db_path)) == 1


async def test_a_dead_alert_channel_does_not_change_the_post_failed_or_skipped_outcome(
    tmp_path, db_path
):
    src = _file(tmp_path, "a.txt", ["One."])
    spy = Spy()

    async def dead_alert(text: str) -> None:
        raise RuntimeError("no channel")

    async def failing_post(text: str) -> int:
        raise RuntimeError("Missing Permissions")

    deps = _deps(db_path, [src], spy)
    deps.alert, deps.post = dead_alert, failing_post
    assert (await run_daily_quote(deps)).status == "post_failed"
    skipped = _deps(db_path, [_nowhere("x.txt")], spy, day=DAY + timedelta(days=1))
    skipped.alert = dead_alert
    assert (await run_daily_quote(skipped)).status == "skipped"


async def test_a_base_exception_from_the_alert_is_not_swallowed(db_path):
    spy = Spy()

    async def fatal_alert(text: str) -> None:
        raise _Fatal

    deps = _deps(db_path, [_nowhere("x.txt")], spy)
    deps.alert = fatal_alert
    with pytest.raises(_Fatal):
        await run_daily_quote(deps)


async def test_the_post_argument_is_exactly_the_rendered_quote_and_hostile_text_is_escaped(
    tmp_path, db_path
):
    text = "@everyone **bold** <#123456> [x](https://evil.example) `code`"
    src = _file(tmp_path, "a.txt", [text])
    spy = Spy()
    await run_daily_quote(_deps(db_path, [src], spy))
    assert spy.posts == [render_quote_message(Quote(text))]
    assert "@everyone" not in spy.posts[0].replace("@​everyone", "")
    assert "\\[x](" in spy.posts[0]  # the bracket is escaped, so no masked link


async def test_every_post_fits_in_2000_utf16_units_even_at_the_astral_boundary(tmp_path, db_path):
    from newsbot.lounge.quotes import fits

    kmax = max(k for k in range(1, 2000) if fits(Quote("\U0001f36a" * k)))
    ks = [kmax - 2, kmax - 1, kmax, kmax + 1, kmax + 2]
    src = _file(tmp_path, "a.txt", ["\U0001f36a" * k for k in ks])
    spy = Spy()
    for i in range(5):
        deps = _deps(db_path, [src], spy, day=DAY + timedelta(days=i), seed=i)
        out = await run_daily_quote(deps)
        assert out.status == "posted"
    assert all(discord_len(p) <= 2000 for p in spy.posts)
    assert set(spy.posts) == {render_quote_message(Quote("\U0001f36a" * k)) for k in ks[:3]}


# --- Day handling ------------------------------------------------------------


@pytest.mark.parametrize(
    "first, second",
    [
        (date(2026, 12, 31), date(2027, 1, 1)),
        (date(2027, 2, 28), date(2028, 2, 29)),
        (date(2028, 2, 29), date(2028, 3, 1)),
        (date(1999, 12, 31), date(2000, 1, 1)),
    ],
)
async def test_days_across_year_and_leap_boundaries_each_post_once(
    tmp_path, db_path, first, second
):
    src = _file(tmp_path, "a.txt", ["One.", "Two.", "Three.", "Four."])
    spy = Spy()
    for i, day in enumerate((first, second)):
        assert (
            await run_daily_quote(_deps(db_path, [src], spy, day=day, seed=i))
        ).status == "posted"
        assert _last_day(db_path) == day.isoformat()
        assert (await run_daily_quote(_deps(db_path, [src], spy, day=day, seed=9))).status == (
            "already_posted"
        )
    assert len(spy.posts) == 2


async def test_forced_run_on_a_day_never_posted_marks_it_done(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["One.", "Two."])
    spy = Spy()
    assert (await run_daily_quote(_deps(db_path, [src], spy), force=True)).status == "posted"
    assert (await run_daily_quote(_deps(db_path, [src], spy, seed=4))).status == "already_posted"
    assert len(spy.posts) == 1


async def test_a_normal_run_the_day_after_only_forced_runs_posts(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", [f"Q{i}." for i in range(6)])
    spy = Spy()
    for i in range(3):
        await run_daily_quote(_deps(db_path, [src], spy, seed=i), force=True)
    tomorrow = _deps(db_path, [src], spy, day=DAY + timedelta(days=1), seed=8)
    assert (await run_daily_quote(tomorrow)).status == "posted"
    assert len(spy.posts) == 4
    assert len(set(spy.posts)) == 4
    assert _last_day(db_path) == "2026-09-30"


async def test_the_date_guard_refuses_an_earlier_day_too(tmp_path, db_path):
    """A run for an *earlier* day than the recorded one is refused, and the date stays put.

    Changed from "posts and rewinds": `claim_quote`'s guard is now `<=`, so a
    clock that steps back (or a timezone edit mid-week) can't post extras.
    """
    src = _file(tmp_path, "a.txt", [f"Q{i}." for i in range(6)])
    spy = Spy()
    order = [DAY, DAY - timedelta(days=1), DAY]
    statuses = [
        (await run_daily_quote(_deps(db_path, [src], spy, day=day, seed=i))).status
        for i, day in enumerate(order)
    ]
    assert statuses == ["posted", "already_posted", "already_posted"]
    assert len(spy.posts) == 1
    assert _last_day(db_path) == DAY.isoformat()


async def test_the_stored_day_is_plain_iso_the_same_text_the_guard_compares(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["One."])
    await run_daily_quote(_deps(db_path, [src], Spy(), day=date(2026, 1, 5)))
    assert _last_day(db_path) == "2026-01-05"


# --- cache_dir ---------------------------------------------------------------


async def test_cache_dir_defaults_to_the_directory_beside_the_database(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["One."])
    await run_daily_quote(_deps(db_path, [src], Spy()))
    default = Path(db_path).with_name("newsbot-lounge-cache")
    assert [p.suffix for p in default.iterdir()] == [".json"]


async def test_an_explicit_cache_dir_is_used_instead_of_the_default(tmp_path, db_path):
    src = _file(tmp_path, "a.txt", ["One."])
    elsewhere = tmp_path / "elsewhere"
    deps = _deps(db_path, [src], Spy())
    deps.cache_dir = elsewhere
    await run_daily_quote(deps)
    assert [p.suffix for p in elsewhere.iterdir()] == [".json"]
    assert not Path(db_path).with_name("newsbot-lounge-cache").exists()


async def test_every_source_failing_writes_no_cache_and_no_rows(tmp_path, db_path):
    await run_daily_quote(_deps(db_path, [_nowhere("a.txt"), _nowhere("b.txt")], Spy()))
    assert not Path(db_path).with_name("newsbot-lounge-cache").exists()
    assert _rows(db_path) == []
