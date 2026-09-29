"""Adversarial tests for the "already posted today" pre-checks (the `>=` rule).

The brief (test-engineer, 2026-09-29): `run_daily_quote` and `/newsbot
quote-now` both peek at `last_quote_date` before doing anything expensive,
and both used to ask "is it equal to today?". A clock that stepped backwards
(or a box that spent an afternoon in 2027) made tomorrow's date look like
not-today, so the daily job went off to fetch sources over the network only to
have `claim_quote` say no at the end. The pre-checks now use `claim_quote`'s
own rule: at or after today counts as posted.

The point of these tests is the word "nothing". With a future date on file
the daily job must load no source and make no request; the loader and the
transport are both wired to fail the test if touched. The rule is also pinned
at its edges (month and year rollovers, a garbage stored value), because ISO
dates sort as text and text will sort anything you give it.
"""

from __future__ import annotations

import random
from contextlib import closing
from datetime import UTC, date, datetime

import httpx
import pytest

from newsbot.bot import commands as commands_module
from newsbot.config import QuoteSourceCfg
from newsbot.lounge import daily
from newsbot.lounge.daily import QuoteDeps, _already_posted, run_daily_quote
from newsbot.store.db import connect, migrate

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DAY = date(2026, 9, 29)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _set_last_quote_date(db_path: str, value: str) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO lounge_state (key, value) VALUES ('last_quote_date', ?)",
            (value,),
        )


def _last_quote_date(db_path: str) -> str | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM lounge_state WHERE key='last_quote_date'").fetchone()
    return row["value"] if row else None


class Spy:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.alerts: list[str] = []

    async def post(self, text: str) -> int:
        self.posts.append(text)
        return 1

    async def alert(self, text: str) -> None:
        self.alerts.append(text)


def _deps(db_path: str, spy: Spy, requests: list[str], day: date = DAY) -> QuoteDeps:
    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise AssertionError(f"unexpected request to {request.url}")

    return QuoteDeps(
        sources=[
            QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde"),
            QuoteSourceCfg(kind="url", value="https://quotes.example/fortunes.txt"),
        ],
        db_path=db_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        local_day=day,
        now=lambda: NOW,
        post=spy.post,
        alert=spy.alert,
        rng=random.Random(1),  # noqa: S311
    )


@pytest.fixture
def no_loads(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def boom(src, **kwargs):
        calls.append(src.key)
        raise AssertionError("load_source must not be called")

    monkeypatch.setattr(daily, "load_source", boom)
    return calls


@pytest.mark.parametrize("stored", ["2026-09-30", "2026-10-01", "2027-01-01", "2099-12-31"])
async def test_a_future_last_quote_date_loads_nothing_fetches_nothing_and_posts_nothing(
    db_path, no_loads, stored
):
    _set_last_quote_date(db_path, stored)
    spy, requests = Spy(), []

    out = await run_daily_quote(_deps(db_path, spy, requests))

    assert out.status == "already_posted"
    assert (no_loads, requests, spy.posts, spy.alerts) == ([], [], [], [])
    assert _last_quote_date(db_path) == stored


async def test_a_future_last_quote_date_with_the_real_loader_still_touches_no_network(db_path):
    # Same as above without the loader patched out: if the pre-check let the
    # run through, the real `load_source` would try the transport, which
    # records the request and fails.
    _set_last_quote_date(db_path, "2027-01-01")
    spy, requests = Spy(), []

    out = await run_daily_quote(_deps(db_path, spy, requests))

    assert out.status == "already_posted"
    assert (requests, spy.posts) == ([], [])


async def test_the_year_boundary_counts_as_later(db_path, no_loads):
    _set_last_quote_date(db_path, "2027-01-01")
    spy, requests = Spy(), []
    out = await run_daily_quote(_deps(db_path, spy, requests, day=date(2026, 12, 31)))
    assert out.status == "already_posted"
    assert (no_loads, requests) == ([], [])


@pytest.mark.parametrize(
    ("stored", "day", "expected"),
    [
        (None, "2026-09-29", False),
        ("2026-09-28", "2026-09-29", False),
        ("2026-09-29", "2026-09-29", True),
        ("2026-09-30", "2026-09-29", True),
        ("2025-12-31", "2026-01-01", False),
        ("2026-01-01", "2025-12-31", True),
        ("2026-09-09", "2026-09-10", False),
        ("2026-10-01", "2026-09-30", True),
    ],
)
def test_already_posted_is_a_date_comparison_in_both_places(db_path, stored, day, expected):
    if stored is not None:
        _set_last_quote_date(db_path, stored)
    assert _already_posted(db_path, day) is expected
    assert commands_module._quote_posted_today_sync(db_path, day) is expected


@pytest.mark.parametrize("stored", ["", "garbage", "9", "tomorrow"])
def test_documented_a_stored_value_that_is_not_a_date_sorts_as_text_and_can_count_as_posted(
    db_path, stored
):
    # The stored date is only ever written by `claim_quote` from an ISO date,
    # so this can't happen through the app. If someone edits the row by hand,
    # the comparison is plain string ordering: "garbage" and "tomorrow" sort
    # after "2026-..." and read as "already posted", while "" sorts before
    # everything and never does. Nothing crashes either way.
    _set_last_quote_date(db_path, stored)
    expected = stored >= "2026-09-29"
    assert _already_posted(db_path, "2026-09-29") is expected
    assert commands_module._quote_posted_today_sync(db_path, "2026-09-29") is expected


async def test_force_skips_the_precheck_even_with_a_future_date_on_file(db_path, monkeypatch):
    # `quote-now` after its "Post another one?" confirmation. The pre-check is
    # bypassed on purpose; only `claim_quote` decides, and its `force` branch
    # accepts an earlier day than the stored one.
    _set_last_quote_date(db_path, "2027-01-01")
    loaded: list[str] = []

    async def counting(src, **kwargs):
        loaded.append(src.key)
        raise RuntimeError("stop here")

    monkeypatch.setattr(daily, "load_source", counting)
    spy, requests = Spy(), []

    await run_daily_quote(_deps(db_path, spy, requests), force=True)

    assert loaded != []
