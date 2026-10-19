"""Adversarial round on the shift_table wiring through a collection pass (plan Task 2).

The first round pinned the happy commit point: the hook returns, the validators
become real. This one tries to make that wrong. A hook that hangs and a next
pass that works; two passes at once; `--check-sources` running while the bot is
mid-pass; one table failing while another succeeds; two sources reading one
url; the real hook failing halfway; and the quiet question of what the default
no-op hook does to a validator. Then the other half of the contract: a table's
items reach the SHiFT hook and nothing else, and a table that breaks shows up
where a broken feed does (source health, the 12-in-a-row alert, the owner's
daily report).

Every request is an `httpx.MockTransport`; the database is a temp file.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest

import newsbot.pipeline.collect as collect_mod
import newsbot.shift.fanout as fanout_mod
from newsbot.bot.client import NewsBot
from newsbot.collectors.base import RawItem
from newsbot.collectors.shift_table import PageValidators, ShiftTableCollector, Validators
from newsbot.config import Secrets, ShiftTableSource, load_config
from newsbot.pipeline.collect import (
    SOURCE_FAILURE_ALERT_THRESHOLD,
    CollectionDeps,
    run_collection,
    storable_items,
)
from newsbot.pipeline.lock import _run_lock
from newsbot.pipeline.run import render_check_sources_report, run_check_sources
from newsbot.shift.fanout import FanoutDeps, make_shift_hook
from newsbot.shift.sweep import PrintCodeAlertPoster
from newsbot.store import repo
from newsbot.store.db import connect, migrate

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
URL_A = "https://codes.example/borderlands-4-shift-codes/"
URL_B = "https://other.example/bl4-codes/"
CODE = "K4M7P-2QXZR-9TLWB-6NHVC-J3FDS"
PAGE = (
    "<html><body><h2>Codes</h2><table><tr><th>SHiFT Code</th><th>Reward</th></tr>"
    f"<tr><td>{CODE}</td><td>3 Golden Keys</td></tr></table></body></html>"
)
NO_TABLES = "<html><body><p>We redesigned the site. Sorry.</p></body></html>"

SECRETS = Secrets(
    discord_token=None,
    anthropic_api_key="k",
    brave_api_key=None,
    bluesky_handle=None,
    bluesky_app_password=None,
)

CONFIG = f"""\
home_guild_id: 100000000000000001
catalog:
  - key: palworld
    name: Palworld
  - key: borderlands4
    name: Borderlands 4
    aliases: [BL4]
    sources:
      - {{type: shift_table, name: "mentalmars", url: "{URL_A}", trust: community}}
"""


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    return load_config(path)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def alerts():
    return []


class Site:
    """The pages, by url. A page answers 304 to its own ETag; `seen` logs the requests."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, str, str | None]] = {URL_A: (200, PAGE, '"v1"')}
        self.seen: list[tuple[str, str | None]] = []

    def set(self, url: str, status: int, body: str, etag: str | None) -> None:
        self.pages[url] = (status, body, etag)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        sent = request.headers.get("If-None-Match")
        self.seen.append((url, sent))
        status, body, etag = self.pages[url]
        headers = {"ETag": etag} if etag else {}
        if status == 200 and etag and sent == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(status, content=body.encode(), headers=headers)

    def sent(self, url: str = URL_A) -> list[str | None]:
        return [s for u, s in self.seen if u == url]


async def _no_sleep(_seconds: float) -> None:
    return None


def table(name: str = "mentalmars", url: str = URL_A, validators=None) -> ShiftTableCollector:
    # topics, as build_catalog_collectors sets them: the item is only its code, so the
    # dedicated-source rule is the one thing that ties it to Borderlands 4.
    source = ShiftTableSource(
        type="shift_table", name=name, url=url, trust="community", topics=["borderlands4"]
    )
    return ShiftTableCollector(source, validators=validators, sleep=_no_sleep)


def deps_for(cfg, db_path, client, alerts, collectors, **kw) -> CollectionDeps:
    async def alert(message: str) -> None:
        alerts.append(message)

    return CollectionDeps(
        cfg=cfg,
        db_path=db_path,
        http=client,
        collectors=collectors,
        now=lambda: NOW,
        alert=alert,
        sleep=_no_sleep,
        **kw,
    )


async def ok_hook(items, results):
    return 0


class FakeFeed:
    source_type = "rss"
    rate_limit_key = None

    def __init__(self, name: str, items: list[RawItem]) -> None:
        self.name = name
        self._items = items

    async def collect(self, http):
        return list(self._items)


def news(url: str, title: str, source: str = "Feed") -> RawItem:
    return RawItem(
        url=url,
        title=title,
        excerpt="",
        source_name=source,
        trust="press",
        published_at=NOW,
        topics=("borderlands4",),
    )


# --- the commit point, under stress ---


async def test_a_hook_that_times_out_then_a_good_pass_commits_only_on_the_good_one(
    cfg, db_path, alerts, monkeypatch
):
    monkeypatch.setattr(collect_mod, "_SHIFT_HOOK_TIMEOUT_S", 0.05)
    site = Site()
    validators = PageValidators()
    state = {"hang": True}

    async def hook(items, results):
        if state["hang"]:
            await asyncio.sleep(3600)
        return 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:

        async def one_pass():
            collectors = [table(validators=validators)]
            deps = deps_for(cfg, db_path, client, alerts, collectors, page_validators=validators)
            deps.shift_hook = hook
            await run_collection(deps)

        await one_pass()
        assert validators == {} and URL_A in validators.pending  # staged, never committed
        state["hang"] = False
        await one_pass()  # full fetch again, because nothing was committed
        assert validators == {URL_A: Validators('"v1"', None)} and validators.pending == {}
        await one_pass()  # and only now conditional
    assert site.sent() == [None, None, '"v1"']


async def test_an_overlapping_pass_is_skipped_and_leaves_the_first_ones_staging_alone(
    cfg, db_path, alerts
):
    site = Site()
    validators = PageValidators()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_hook(items, results):
        entered.set()
        await release.wait()
        return 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        first = deps_for(
            cfg, db_path, client, alerts, [table(validators=validators)], page_validators=validators
        )
        first.shift_hook = slow_hook
        second = deps_for(
            cfg, db_path, client, alerts, [table(validators=validators)], page_validators=validators
        )
        second.shift_hook = ok_hook

        task = asyncio.create_task(run_collection(first))
        await entered.wait()
        skipped = await run_collection(second)  # run-now while the hourly pass is mid-hook

        assert skipped.skipped
        assert validators == {}  # the skipped pass committed nothing for the first one
        assert URL_A in validators.pending  # and didn't discard what the first one staged
        assert len(site.seen) == 1  # and never touched the site

        release.set()
        await task
    assert validators == {URL_A: Validators('"v1"', None)}


async def test_check_sources_mid_pass_neither_commits_nor_clobbers_the_bots_validators(
    cfg, db_path, alerts
):
    site = Site()
    validators = PageValidators()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_hook(items, results):
        entered.set()
        await release.wait()
        return 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(
            cfg, db_path, client, alerts, [table(validators=validators)], page_validators=validators
        )
        deps.shift_hook = slow_hook
        task = asyncio.create_task(run_collection(deps))
        await entered.wait()

        site.set(URL_A, 200, PAGE, '"v2"')  # the page moves on while the bot is mid-pass
        report = await run_check_sources(cfg, SECRETS, client)

        (result,) = report.results
        assert result.error is None and len(result.items) == 1
        assert validators == {}  # --check-sources committed nothing...
        assert validators.pending == {URL_A: Validators('"v1"', None)}  # ...and staged nothing

        release.set()
        await task
    assert validators == {URL_A: Validators('"v1"', None)}  # the bot's own, not the check's


async def test_check_sources_always_reads_the_whole_page_even_when_the_bot_knows_it(cfg):
    # A conditional request would answer 304, the table would say "0 items", and the owner
    # would read a healthy page as an empty one. The check builds its own collectors, with
    # no validators, so every run is a full fetch.
    site = Site()
    known = PageValidators({URL_A: Validators('"v1"', None)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        first = await run_check_sources(cfg, SECRETS, client)
        second = await run_check_sources(cfg, SECRETS, client)
    assert site.sent() == [None, None]
    assert [len(r.items) for r in first.results + second.results] == [1, 1]
    assert known == {URL_A: Validators('"v1"', None)}  # nothing global to leak into


async def test_one_table_failing_does_not_block_the_commit_of_another(cfg, db_path, alerts):
    site = Site()
    site.set(URL_B, 200, PAGE, '"b1"')
    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:

        async def one_pass():
            collectors = [
                table("mentalmars", URL_A, validators),
                table("other table", URL_B, validators),
            ]
            deps = deps_for(cfg, db_path, client, alerts, collectors, page_validators=validators)
            deps.shift_hook = ok_hook
            return await run_collection(deps)

        await one_pass()
        assert validators == {URL_A: Validators('"v1"', None), URL_B: Validators('"b1"', None)}

        site.set(URL_A, 503, "down", None)  # A breaks; B's page changes
        site.set(URL_B, 200, PAGE, '"b2"')
        outcome = await one_pass()

    assert outcome.failing_sources == ("mentalmars",)
    # A keeps the validators of its last parse (those codes were saved); B moves on.
    assert validators == {URL_A: Validators('"v1"', None), URL_B: Validators('"b2"', None)}


async def test_a_200_with_no_tables_commits_nothing_and_forgets_nothing(cfg, db_path, alerts):
    site = Site()
    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:

        async def one_pass():
            deps = deps_for(
                cfg,
                db_path,
                client,
                alerts,
                [table(validators=validators)],
                page_validators=validators,
            )
            deps.shift_hook = ok_hook
            return await run_collection(deps)

        await one_pass()
        site.set(URL_A, 200, NO_TABLES, '"redesign"')  # a new ETag on a page with nothing in it
        broken = await one_pass()
        assert broken.failing_sources == ("mentalmars",)
        assert validators == {URL_A: Validators('"v1"', None)}
        assert validators.pending == {}
        site.set(URL_A, 200, PAGE, '"v3"')
        fixed = await one_pass()
    assert fixed.failing_sources == ()
    assert site.sent() == [None, '"v1"', '"v1"']
    assert validators == {URL_A: Validators('"v3"', None)}


async def test_two_sources_reading_one_url_both_go_conditional_and_both_commit_once(
    cfg, db_path, alerts
):
    site = Site()
    seen_by_hook: list[list[RawItem]] = []
    validators = PageValidators()

    async def hook(items, results):
        seen_by_hook.append(list(items))
        return 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:

        async def one_pass():
            collectors = [
                table("mentalmars", URL_A, validators),
                table("mentalmars mirror", URL_A, validators),
            ]
            deps = deps_for(cfg, db_path, client, alerts, collectors, page_validators=validators)
            deps.shift_hook = hook
            return await run_collection(deps)

        first = await one_pass()
        second = await one_pass()

    assert first.sources_ok == 2 and second.sources_ok == 2
    assert site.sent() == [None, None, '"v1"', '"v1"']
    assert sorted(i.source_name for i in seen_by_hook[0]) == ["mentalmars", "mentalmars mirror"]
    assert seen_by_hook[1] == []  # two 304s: healthy, nothing new
    assert validators == {URL_A: Validators('"v1"', None)}


async def test_the_default_no_op_hook_never_commits_validators(cfg, db_path, alerts):
    # The default hook (`_no_shift`) saves nothing, so committing after it would break
    # §16's rule ("only worth keeping if what we read got saved"): anyone who builds
    # CollectionDeps(page_validators=...) and forgets `shift_hook` would get a 304 for codes
    # that were never recorded. So it commits nothing, and the next pass is a full fetch.
    site = Site()
    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(
            cfg, db_path, client, alerts, [table(validators=validators)], page_validators=validators
        )
        assert deps.shift_hook is collect_mod._no_shift
        await run_collection(deps)
        assert validators == {} and validators.pending == {URL_A: Validators('"v1"', None)}
        await run_collection(deps)
    assert site.sent() == [None, None]
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM code_sightings").fetchone()[0] == 0


async def test_the_bot_never_pairs_validators_with_the_default_hook(cfg, db_path):
    # Both production wirings are safe, whatever the default hook does.
    bot = NewsBot(cfg, SECRETS, db_path)

    async def no_sync(*args, **kwargs):
        return []

    bot.tree.sync = no_sync  # type: ignore[method-assign]
    try:
        await bot.setup_hook()
        deps = bot._collection_deps
        assert deps is not None
        assert deps.page_validators is bot._page_validators
        assert deps.shift_hook is not collect_mod._no_shift
    finally:
        if bot.scheduler is not None:
            bot.scheduler.shutdown(wait=False)
        if bot.http_client is not None:
            await bot.http_client.aclose()
        await bot.close()


def test_the_collect_cli_builds_its_deps_without_validators():
    import ast
    import inspect
    import textwrap

    from newsbot.pipeline import run

    # Read the syntax, not the text: a comment or a docstring may say "page_validators" all it
    # likes, and a substring search would call that a failure. What matters is that no name,
    # attribute or keyword argument in the function is one.
    tree = ast.parse(textwrap.dedent(inspect.getsource(run._run_collect_cli)))
    used = {
        *(n.id for n in ast.walk(tree) if isinstance(n, ast.Name)),
        *(n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)),
        *(k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords),
    }
    assert "page_validators" not in used  # one pass, then the process exits; nothing to keep


# --- the real hook, failing for real ---


def _preview_hook(cfg, db_path):
    async def notify(_guild_id, _text):
        return None

    return make_shift_hook(
        FanoutDeps(
            cfg=cfg,
            db_path=db_path,
            now=lambda: NOW,
            poster_for=lambda _g, _c, _p, _n: PrintCodeAlertPoster(),
            notify_guild=notify,
            sleep=_no_sleep,
            preview=True,
        )
    )


def _sightings(db_path) -> list[tuple[str, str]]:
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT code, source_name FROM code_sightings ORDER BY 1, 2")
        return [(r["code"], r["source_name"]) for r in rows]


async def test_the_real_hook_saves_the_sightings_before_the_validators_become_real(
    cfg, db_path, alerts
):
    site = Site()
    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(
            cfg, db_path, client, alerts, [table(validators=validators)], page_validators=validators
        )
        deps.shift_hook = _preview_hook(cfg, db_path)
        await run_collection(deps)
    assert _sightings(db_path) == [(CODE, "mentalmars")]
    assert validators == {URL_A: Validators('"v1"', None)}


async def test_a_real_hook_that_dies_saving_costs_the_validators_and_the_next_pass_recovers(
    cfg, db_path, alerts, monkeypatch
):
    site = Site()
    validators = PageValidators()
    real = fanout_mod.record_code_sightings

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked, or on fire")

    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:

        async def one_pass():
            deps = deps_for(
                cfg,
                db_path,
                client,
                alerts,
                [table(validators=validators)],
                page_validators=validators,
            )
            deps.shift_hook = _preview_hook(cfg, db_path)
            await run_collection(deps)

        monkeypatch.setattr(fanout_mod, "record_code_sightings", broken)
        await one_pass()
        assert _sightings(db_path) == [] and validators == {}
        assert len(alerts) == 1 and "SHiFT detection failed" in alerts[0]

        monkeypatch.setattr(fanout_mod, "record_code_sightings", real)
        await one_pass()  # not a 304: the page is read in full and the code is saved this time
    assert site.sent() == [None, None]
    assert _sightings(db_path) == [(CODE, "mentalmars")]
    assert validators == {URL_A: Validators('"v1"', None)}


# --- what a table's items may touch ---


async def test_a_tables_items_reach_the_shift_hook_with_details_and_never_the_database(
    cfg, db_path, alerts
):
    site = Site()
    got: list[RawItem] = []

    async def hook(items, results):
        got.extend(items)
        return 0

    feed = FakeFeed("Feed", [news("https://press.example/bl4-patch", "Borderlands 4 patch notes")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(cfg, db_path, client, alerts, [table(), feed])
        deps.shift_hook = hook
        outcome = await run_collection(deps)

    by_source = {i.source_name: i for i in got}
    assert set(by_source) == {"mentalmars", "Feed"}
    assert by_source["mentalmars"].title == CODE
    assert by_source["mentalmars"].details is not None
    assert by_source["mentalmars"].details.rank == "table"
    assert outcome.new_items == 1  # the press story only
    with closing(connect(db_path)) as conn:
        urls = [r["url"] for r in conn.execute("SELECT url FROM items")]
        titles = [r["title"] for r in conn.execute("SELECT title FROM items")]
    assert urls == ["https://press.example/bl4-patch"]
    assert CODE not in " ".join(titles)


def test_storable_items_drops_a_tables_rows_and_keeps_everything_else(cfg):
    rows = [
        news(URL_A, CODE, "mentalmars"),
        news("https://press.example/a", "Borderlands 4 news", "Feed"),
        news("https://press.example/b", "Borderlands 4 more", "mentalmars mirror"),
    ]
    kept = storable_items(rows, cfg)
    assert [i.source_name for i in kept] == ["Feed", "mentalmars mirror"]
    # ...the second being the point: codes-only is by exact source name, so a source
    # that isn't configured as a table is not treated as one, whatever it's called.


async def test_another_source_returning_the_tables_own_page_is_not_stored(cfg, db_path, alerts):
    site = Site()
    search_hit = FakeFeed(
        "Brave Search", [news(URL_A, "Borderlands 4 SHiFT codes", "Brave Search")]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(cfg, db_path, client, alerts, [search_hit])
        deps.shift_hook = ok_hook
        await run_collection(deps)
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_storable_items_drops_the_tables_page_however_the_address_is_dressed(cfg):
    # Matched on the canonical address, so a fragment, tracking parameters, a trailing slash
    # or a shouty host don't get a search hit past the net. A different page on the same
    # site is just news.
    dressed = [
        URL_A + "#all-codes",
        URL_A.rstrip("/"),
        URL_A + "?utm_source=search",
        URL_A.replace("codes.example", "CODES.example"),
    ]
    rows = [news(u, "Borderlands 4 SHiFT codes", "Brave Search") for u in dressed]
    rows.append(news(URL_A + "other-page/", "Borderlands 4 guide", "Brave Search"))
    rows.append(news("not a url", "Borderlands 4 oddity", "Brave Search"))
    kept = storable_items(rows, cfg)
    assert [i.url for i in kept] == [URL_A + "other-page/", "not a url"]


# --- health, alerts, the owner's report ---


async def test_a_failing_table_is_counted_in_the_outcome_and_the_source_health(
    cfg, db_path, alerts
):
    site = Site()
    site.set(URL_A, 503, "nope", None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(cfg, db_path, client, alerts, [table()])
        deps.shift_hook = ok_hook
        outcome = await run_collection(deps)
    assert (outcome.sources_ok, outcome.sources_total) == (0, 1)
    assert outcome.failing_sources == ("mentalmars",)
    with closing(connect(db_path)) as conn:
        (row,) = repo.failing_sources(conn)
    assert (row.source_name, row.consecutive_failures) == ("mentalmars", 1)
    assert "503" in (row.last_error or "")


async def test_a_page_with_no_tables_is_a_failed_source_with_the_collectors_own_words(
    cfg, db_path, alerts
):
    site = Site()
    site.set(URL_A, 200, NO_TABLES, None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        deps = deps_for(cfg, db_path, client, alerts, [table()])
        deps.shift_hook = ok_hook
        await run_collection(deps)
    with closing(connect(db_path)) as conn:
        (row,) = repo.failing_sources(conn)
    assert row.last_error and "no SHiFT code tables found" in row.last_error


async def test_a_table_that_fails_twelve_passes_running_alerts_the_owner_once(cfg, db_path, alerts):
    site = Site()
    site.set(URL_A, 503, "nope", None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        for _ in range(SOURCE_FAILURE_ALERT_THRESHOLD + 3):
            deps = deps_for(cfg, db_path, client, alerts, [table()])
            deps.shift_hook = ok_hook
            await run_collection(deps)
    flagged = [a for a in alerts if "mentalmars" in a]
    assert len(flagged) == 1
    assert f"{SOURCE_FAILURE_ALERT_THRESHOLD} collections in a row" in flagged[0]


def _bot(cfg, db_path) -> NewsBot:
    return NewsBot(cfg, SECRETS, db_path)


async def test_the_owner_report_lists_a_failing_table_until_it_leaves_the_config(
    cfg, db_path, tmp_path
):
    with closing(connect(db_path)) as conn:
        for _ in range(3):
            repo.record_source_result(conn, "mentalmars", NOW, "HTTP 503 from the table page")
    bot = _bot(cfg, db_path)
    try:
        text = await asyncio.to_thread(bot._owner_report_sync, NOW, "2026-10-01")
    finally:
        await bot.close()
    assert text is not None
    assert "mentalmars (3 in a row)" in text and "503" in text

    without = tmp_path / "without.yaml"
    without.write_text(CONFIG.split("    sources:")[0])  # same file, minus the borderlands4 sources
    cfg2 = load_config(without)
    assert "mentalmars" not in cfg2.configured_source_names()
    bot2 = _bot(cfg2, db_path)
    try:
        text2 = await asyncio.to_thread(bot2._owner_report_sync, NOW, "2026-10-02")
    finally:
        await bot2.close()
    assert text2 is not None and "mentalmars" not in text2


# --- --check-sources: games and the rendered table ---


async def test_check_sources_for_another_game_leaves_the_table_out_and_for_its_game_keeps_it(cfg):
    site = Site()
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        palworld = await run_check_sources(cfg, SECRETS, client, games=["palworld"])
        bl4 = await run_check_sources(cfg, SECRETS, client, games=["borderlands4"])
    assert [r.source_name for r in palworld.results] == []
    assert [r.source_name for r in bl4.results] == ["mentalmars"]
    assert site.sent() == [None]  # palworld never asked


async def test_the_check_sources_table_shows_the_tables_items_but_counts_no_story(cfg):
    site = Site()
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        report = await run_check_sources(cfg, SECRETS, client)
    text = render_check_sources_report(cfg, report, web_search_note=None)
    line = next(
        line for line in text.splitlines() if "mentalmars" in line and "shift_table" in line
    )
    assert line.split()[2] == "1"  # read: one row
    assert "borderlands4 (Borderlands 4): 0 item(s)" in text  # counted: none
    assert "1 source(s) ok, 0 failed" in text


async def test_check_sources_reports_a_dead_table_without_failing_the_run(cfg):
    site = Site()
    site.set(URL_A, 200, NO_TABLES, None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        report = await run_check_sources(cfg, SECRETS, client)
    (result,) = report.results
    assert result.error and "no SHiFT code tables found" in result.error
    assert result.items == []


# --- the lock the other tests rely on ---


def test_the_run_lock_is_free_between_tests():
    # Several tests above hold a pass open on purpose; a leak would starve the next test.
    assert not _run_lock.locked()
