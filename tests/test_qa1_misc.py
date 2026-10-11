"""QA fix batch 1: the smaller fixes (6, 8, 9, 10, 11, 12, 15, 16, 17).

Each section is named for the fix it pins. Real temp SQLite where there is a database,
`httpx.MockTransport` where there is a fetch, and no network anywhere. Invisible and
non-ASCII characters are built with `chr()` and `\\N{...}`, never typed.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_config_shift_table_adversarial import _error, _item, _load, _rss, _table, _v3
from test_shift_details_detection import X, Y, code_n, cosmetic_table, main_table, page
from test_shift_details_detection_adversarial import read_named
from test_shift_fanout import V3, rows

from newsbot.bot.format import render_history_pages
from newsbot.collectors.base import run_collectors
from newsbot.collectors.rss import _fetch, _fetch_body
from newsbot.collectors.shift_table import (
    _MAX_CODES,
    PageValidators,
    ShiftTableCollector,
)
from newsbot.config import AppConfig, ShiftTableSource, _name_problem, load_config
from newsbot.shift.fanout import reserve_for_config
from newsbot.shift.match import CODE_RE, defuse_codes, find_codes
from newsbot.shift.official import read_official_details
from newsbot.store import db, repo
from newsbot.store.db import StoreError, connect, migrate
from newsbot.store.models import CodeDetailsRow, CodeView

T0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
PAGE_URL = "https://codes.example/borderlands-4-shift-codes/"


async def _no_sleep(_seconds: float) -> None:
    return None


# =====================================================================================
# Fix 6: the edit reserve counts only the sources that can reach an alert
# =====================================================================================

LONG = "S" * 100


def _official(name: str = LONG, **extra):
    from newsbot.config import RssSource, SharedRssSource

    kind = SharedRssSource if "games" in extra else RssSource
    return kind(type="rss", name=name, url="https://example.com/feed", trust="official", **extra)


def _with_game_source(cfg: AppConfig, key: str, source) -> AppConfig:
    games = [
        g.model_copy(update={"sources": [*g.sources, source]}) if g.key == key else g
        for g in cfg.catalog
    ]
    return cfg.model_copy(update={"catalog": games})


def _with_shared(cfg: AppConfig, source) -> AppConfig:
    return cfg.model_copy(update={"shared_sources": [*cfg.shared_sources, source]})


def _with_shift_games(cfg: AppConfig, games: list[str]) -> AppConfig:
    return cfg.model_copy(update={"shift": cfg.shift.model_copy(update={"games": games})})


def test_a_long_name_under_a_game_with_no_shift_codes_costs_nothing():
    cfg = load_config(V3)
    assert cfg.shift.games == ["borderlands4"]
    base = reserve_for_config(cfg)

    assert reserve_for_config(_with_game_source(cfg, "palworld", _official())) == base


def test_the_same_name_under_a_shift_game_widens_the_reserve():
    cfg = load_config(V3)
    wider = reserve_for_config(_with_game_source(cfg, "borderlands4", _official()))
    assert wider > reserve_for_config(cfg)


def test_a_shared_source_limited_to_other_games_costs_nothing_and_an_open_one_does():
    cfg = load_config(V3)
    base = reserve_for_config(cfg)
    assert reserve_for_config(_with_shared(cfg, _official(games=["palworld"]))) == base
    assert reserve_for_config(_with_shared(cfg, _official(games=["borderlands4"]))) > base
    assert reserve_for_config(_with_shared(cfg, _official())) > base  # no games: every game


def test_an_empty_shift_games_list_means_every_game_like_v2():
    cfg = _with_shift_games(load_config(V3), [])
    base = reserve_for_config(cfg)
    assert reserve_for_config(_with_game_source(cfg, "palworld", _official())) > base


def test_a_source_that_cannot_supply_details_never_counts():
    cfg = load_config(V3)
    press = _official().model_copy(update={"trust": "press"})
    assert reserve_for_config(_with_game_source(cfg, "borderlands4", press)) == (
        reserve_for_config(cfg)
    )


def test_a_shift_table_source_under_a_shift_game_counts():
    cfg = load_config(V3)
    table = ShiftTableSource(type="shift_table", name=LONG, url=PAGE_URL, trust="community")
    assert reserve_for_config(_with_game_source(cfg, "borderlands4", table)) > (
        reserve_for_config(cfg)
    )


# =====================================================================================
# Fix 8: tables that say "expired", and a cap on how many codes one source can contribute
# =====================================================================================


async def test_a_table_under_an_expired_heading_is_skipped():
    html = page(
        main_table(("1 Golden Key", "Oct 9, 2026", X, "Unknown")),
        "<h2>Expired SHiFT Codes</h2><table><tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>old</td><td>{Y}</td></tr></table>",
    )

    items = await read_named(html, T0)

    assert [i.title for i in items] == [X]


@pytest.mark.parametrize(
    "heading",
    ["EXPIRED", "Recently expired (do not use)", "Codes that have Expired", "Not expired"],
)
async def test_any_heading_that_mentions_expired_skips_its_table(heading):
    html = (
        f"<h2>{heading}</h2><table><tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>old</td><td>{Y}</td></tr></table>"
    ) + main_table(("1 Golden Key", "Oct 9, 2026", X, "Unknown"))

    items = await read_named(f"<html><body>{html}</body></html>", T0)

    assert [i.title for i in items] == [X]


async def test_an_invisible_character_inside_the_heading_does_not_hide_the_word():
    heading = "Exp" + chr(0x200B) + "ired codes"
    html = (
        f"<h2>{heading}</h2><table><tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>old</td><td>{Y}</td></tr></table>"
    ) + main_table(("1 Golden Key", "Oct 9, 2026", X, "Unknown"))

    assert [i.title for i in await read_named(html, T0)] == [X]


async def test_a_table_with_no_heading_or_a_different_one_is_read():
    html = page(
        main_table(("1 Golden Key", "Oct 9, 2026", X, "Unknown")),
        cosmetic_table(("Gnome", "Event", Y, "Unknown")),
    )
    assert sorted(i.title for i in await read_named(html, T0)) == sorted([X, Y])


async def test_a_page_with_only_an_expired_table_is_a_failed_pass():
    html = (
        "<h2>Expired</h2><table><tr><th>Reward</th><th>Code</th></tr>"
        f"<tr><td>old</td><td>{Y}</td></tr></table>"
    )
    with pytest.raises(ValueError, match="no SHiFT code tables found"):
        await read_named(html, T0)


def _codes_page(n: int) -> str:
    return page(main_table(*[("1 Golden Key", "Unknown", code_n(i), "Unknown") for i in range(n)]))


async def test_five_hundred_codes_is_the_most_one_source_can_contribute():
    assert _MAX_CODES == 500
    assert len(await read_named(_codes_page(500), T0)) == 500


async def test_five_hundred_and_one_codes_fail_the_pass_and_name_the_source(caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.collectors.shift_table"):
        with pytest.raises(ValueError, match="too many rows"):
            await read_named(_codes_page(501), T0, name="mentalmars")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "mentalmars" in warnings[0].getMessage()


async def test_the_cap_counts_distinct_codes_not_rows():
    page_ = page(
        main_table(*[("1 Golden Key", "Unknown", code_n(i % 100), "Unknown") for i in range(900)])
    )
    assert len(await read_named(page_, T0)) == 100


async def test_a_pass_over_the_cap_shows_up_as_a_failed_source_in_the_collection_results():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_codes_page(501).encode())
    )
    source = ShiftTableSource(
        type="shift_table", name="mentalmars", url=PAGE_URL, trust="community"
    )
    validators = PageValidators()
    collector = ShiftTableCollector(source, validators=validators, sleep=_no_sleep)
    async with httpx.AsyncClient(transport=transport) as http:
        (result,) = await run_collectors([collector], http)

    assert result.items == []
    assert result.error is not None and "too many rows" in result.error
    assert validators.pending == {}  # nothing staged for a pass that failed


# =====================================================================================
# Fix 9: code-shaped text in rewards and origins
# =====================================================================================

A_CODE = "ABCDE-12345-FGHIJ-67890-KLMNO"


def test_defuse_codes_replaces_every_code_shaped_substring():
    text = f"Use {A_CODE} or {A_CODE.lower()}, ({A_CODE}) {find_codes(A_CODE)[0]}."
    out = defuse_codes(text)
    assert out == "Use [code] or [code], ([code]) [code]."
    assert not CODE_RE.search(out)


def test_defuse_codes_leaves_ordinary_text_alone_and_takes_placeholders_too():
    assert defuse_codes("3 Golden Keys, expires Oct 14") == "3 Golden Keys, expires Oct 14"
    placeholder = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"  # no digit, so not a code to find_codes
    assert find_codes(placeholder) == []
    assert defuse_codes(f"like {placeholder}") == "like [code]"
    # The boundary rules of CODE_RE hold: a slug glued to a path separator is not a code.
    assert defuse_codes("/aaaaa-bbbbb-ccccc-ddddd-eeeee/") == "/aaaaa-bbbbb-ccccc-ddddd-eeeee/"


async def test_the_collector_defuses_codes_in_rewards_and_origins():
    html = page(
        main_table((f"Redeem {A_CODE} for 3 Golden Keys", "Oct 9, 2026", X, "Unknown")),
        cosmetic_table(("Gnome", f"Stream, code {A_CODE.lower()}", Y, "Unknown")),
    )

    items = {i.title: i for i in await read_named(html, T0)}

    assert items[X].details.reward == "Redeem [code] for 3 Golden Keys"
    assert items[Y].details.origin == "Stream, code [code]"
    for item in items.values():
        assert find_codes(item.details.reward) == [] and find_codes(item.details.origin) == []
        # And the item's own text is still exactly its one code.
        assert find_codes(f"{item.title}\n{item.full_text}") == [item.title]


async def test_a_reward_that_is_only_a_code_becomes_a_placeholder_not_a_second_code():
    html = page(main_table((A_CODE, "Oct 9, 2026", X, "Unknown")))
    (item,) = await read_named(html, T0)
    assert item.details.reward == "[code]"


def test_the_official_readers_reward_is_built_from_a_number_so_there_is_nothing_to_defuse():
    details = read_official_details(f"3 Golden Keys {X}", X, T0)
    assert details.reward == "3 Golden Keys"
    assert "[code]" not in details.reward


# =====================================================================================
# Fix 10: a 304 is an answer only to a conditional request
# =====================================================================================


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "headers",
    [
        {"If-None-Match": '"abc"'},
        {"If-Modified-Since": "Wed, 01 Oct 2026 00:00:00 GMT"},
        {"if-none-match": '"abc"'},
        {"IF-MODIFIED-SINCE": "Wed, 01 Oct 2026 00:00:00 GMT"},
        {"If-None-Match": '"abc"', "If-Modified-Since": "x"},
    ],
)
async def test_a_304_to_a_conditional_request_is_returned_not_raised(headers):
    async with _client(lambda request: httpx.Response(304)) as http:
        fetched = await _fetch(http, PAGE_URL, sleep=_no_sleep, headers=headers)
    assert fetched.status == 304 and fetched.body == b""


@pytest.mark.parametrize("headers", [None, {}, {"X-Anything": "1"}, {"Accept": "text/html"}])
async def test_a_304_to_any_other_request_is_an_error(headers):
    async with _client(lambda request: httpx.Response(304)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch(http, PAGE_URL, sleep=_no_sleep, headers=headers)


async def test_the_feed_fetch_raises_on_a_304_as_it_did_before_this_branch():
    async with _client(lambda request: httpx.Response(304)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_body(http, "https://example.com/feed", sleep=_no_sleep)


async def test_a_200_is_unaffected_either_way():
    async with _client(lambda request: httpx.Response(200, content=b"ok")) as http:
        assert (await _fetch(http, PAGE_URL, sleep=_no_sleep)).body == b"ok"
        got = await _fetch(http, PAGE_URL, sleep=_no_sleep, headers={"If-None-Match": '"a"'})
        assert got.body == b"ok"


# =====================================================================================
# Fix 11: source names with bidi controls or line and paragraph separators
# =====================================================================================

BIDI = [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]
SEPARATORS = [0x2028, 0x2029]


@pytest.mark.parametrize("cp", [*BIDI, *SEPARATORS], ids=lambda cp: f"U+{cp:04X}")
def test_a_name_with_one_of_them_anywhere_is_a_problem(cp):
    for name in (f"a{chr(cp)}b", f"{chr(cp)}name", f"name{chr(cp)}"):
        problem = _name_problem(name)
        assert problem is not None
        assert "separator" in problem or "direction" in problem
        assert chr(cp) not in problem  # the message doesn't echo it raw


@pytest.mark.parametrize(
    "name",
    [
        "mentalmars",
        "Borderlands 4 (Bluesky search)",
        "caf" + chr(0xE9),
        "Family " + chr(0x1F468) + chr(0x200D) + chr(0x1F469) + " news",
        "left" + chr(0x200E) + "mark",  # LRM and RLM aren't on the list
        "arabic " + chr(0x0627) + chr(0x0644),
    ],
)
def test_ordinary_names_and_the_marks_that_are_not_on_the_list_are_fine(name):
    assert _name_problem(name) is None


def _yaml_escape(cp: int) -> str:
    return "\\" + f"u{cp:04x}"


@pytest.mark.parametrize("cp", [0x202E, 0x2066, 0x2028, 0x2029])
@pytest.mark.parametrize("make", [_rss, lambda n: _table(name=n)], ids=["rss", "shift_table"])
def test_config_rejects_such_a_name_for_every_source_type(tmp_path, make, cp):
    message = _error(tmp_path, _v3(_item(make(f'"evil{_yaml_escape(cp)}name"'))))
    assert "it's shown in SHiFT alerts" in message
    assert all(ch.isprintable() or ch == "\n" for ch in message)


def test_config_still_loads_a_name_with_a_left_to_right_mark(tmp_path):
    _load(tmp_path, _v3(_item(_rss(f'"fine{_yaml_escape(0x200E)}name"'))))


# =====================================================================================
# Fix 12: the follow-up check skips codes whose window has closed, and has an index
# =====================================================================================


def _seeded_followup_world(db_path: str, *, first_seen: datetime) -> None:
    with closing(connect(db_path)) as conn, conn:
        repo.create_guild(conn, 1, timezone="UTC", set_up=True, now=lambda: first_seen)
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, pinged, "
            "status) VALUES (?, ?, 'r/Borderlands4', 'https://e/x', 0, 'posted')",
            (X, first_seen.isoformat()),
        )
        conn.execute(
            "INSERT INTO guild_code_posts (guild_id, code, status, message_id, pinged, "
            "followup_ok, claimed_at) VALUES (1, ?, 'posted', 5, 0, 1, ?)",
            (X, first_seen.isoformat()),
        )
        for name in ("r/Borderlands4", "mentalmars"):
            conn.execute(
                "INSERT INTO code_sightings (code, source_name, trusted, roundup, seen_at) "
                "VALUES (?, ?, 0, 0, ?)",
                (X, name, first_seen.isoformat()),
            )


@pytest.fixture
def fresh_db(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def test_a_code_inside_its_window_is_still_confirmed(fresh_db):
    _seeded_followup_world(fresh_db, first_seen=T0)
    with closing(connect(fresh_db)) as conn:
        queued = repo.queue_confirmed_followups(conn, [X], now=lambda: T0 + timedelta(hours=23))
    assert queued == 1


def test_the_window_includes_its_last_instant_and_excludes_the_next(fresh_db):
    _seeded_followup_world(fresh_db, first_seen=T0)
    with closing(connect(fresh_db)) as conn:
        late = repo.queue_confirmed_followups(
            conn, [X], now=lambda: T0 + timedelta(hours=24, microseconds=1)
        )
        assert late == 0
        edge = repo.queue_confirmed_followups(conn, [X], now=lambda: T0 + timedelta(hours=24))
        assert edge == 1


def test_a_code_whose_window_has_closed_costs_one_lookup_and_nothing_else(fresh_db):
    _seeded_followup_world(fresh_db, first_seen=T0)
    statements: list[str] = []
    with closing(connect(fresh_db)) as conn:
        conn.set_trace_callback(statements.append)
        queued = repo.queue_confirmed_followups(conn, [X], now=lambda: T0 + timedelta(days=3))
    assert queued == 0
    reads = [s for s in statements if "code_sightings" in s or "code_details" in s]
    assert reads == []
    assert any("alerted_codes" in s for s in statements)


def test_an_unknown_code_is_skipped_quietly(fresh_db):
    with closing(connect(fresh_db)) as conn:
        assert repo.queue_confirmed_followups(conn, [Y], now=lambda: T0) == 0


def test_guild_code_posts_has_an_index_on_code_and_the_followup_insert_uses_it(fresh_db):
    with closing(connect(fresh_db)) as conn:
        indexes = {
            r["name"]: [c["name"] for c in conn.execute(f"PRAGMA index_info({r['name']})")]
            for r in conn.execute("PRAGMA index_list(guild_code_posts)")
        }
        assert indexes["idx_guild_code_posts_code"] == ["code"]
        plan = " ".join(
            r["detail"]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT guild_id, code FROM guild_code_posts "
                "WHERE code = ? AND status = 'posted' AND followup_ok = 1",
                (X,),
            )
        )
    assert "idx_guild_code_posts_code" in plan


# =====================================================================================
# Fix 15: a stale 009 fails loudly at startup
# =====================================================================================


def _stale(path: str, table: str, column: str) -> None:
    """Make the database look as if it ran a draft of 009 that lacked `column`."""
    with closing(connect(path)) as conn:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {column} TO {column}_draft")  # noqa: S608
        conn.commit()


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("guild_alert_messages", "edit_tried_at"),
        ("guild_alert_messages", "ping"),
        ("alerted_codes", "official_seen_at"),
        ("alerted_codes", "table_silenced_at"),
    ],
)
def test_a_version_9_database_missing_a_build_column_is_refused(fresh_db, table, column):
    _stale(fresh_db, table, column)

    with closing(connect(fresh_db)) as conn:
        with pytest.raises(StoreError) as caught:
            migrate(conn)

    message = str(caught.value)
    assert f"{table}.{column}" in message
    assert "start the database fresh" in message


def test_every_missing_column_is_named_at_once(fresh_db):
    _stale(fresh_db, "guild_alert_messages", "ping")
    _stale(fresh_db, "alerted_codes", "official_seen_at")
    with closing(connect(fresh_db)) as conn:
        with pytest.raises(StoreError) as caught:
            migrate(conn)
    assert "guild_alert_messages.ping" in str(caught.value)
    assert "alerted_codes.official_seen_at" in str(caught.value)


def test_a_healthy_database_migrates_and_remigrates_without_complaint(fresh_db):
    with closing(connect(fresh_db)) as conn:
        assert migrate(conn) == 9
        assert migrate(conn) == 9


def test_the_check_only_looks_at_databases_that_claim_to_have_009(fresh_db):
    # One that doesn't is a migration's business, and the migration builds the columns.
    _stale(fresh_db, "guild_alert_messages", "ping")
    with closing(connect(fresh_db)) as conn:
        db._check_build_columns(conn, 8)  # does nothing and raises nothing
        with pytest.raises(StoreError):
            db._check_build_columns(conn, 9)


def test_the_columns_the_check_wants_are_the_columns_009_creates(tmp_path):
    path = str(tmp_path / "x.db")
    with closing(connect(path)) as conn:
        migrate(conn)
        for table, column in db._BUILD_COLUMNS:
            names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
            assert column in names, (table, column)


# =====================================================================================
# Fix 16: /shift history says "expired" once
# =====================================================================================


def _view(n: int, **details) -> CodeView:
    row = CodeDetailsRow(code=X, clock_start=T0, updated_at=T0, **details)
    return CodeView(
        code=X,
        first_seen_at=T0 - timedelta(days=n),
        source_name="Feed",
        item_url="https://example.com/p",
        status="posted",
        from_roundup=False,
        details=row,
    )


def _history_text(view: CodeView) -> str:
    (embed,) = render_history_pages([view], timezone="UTC", now=T0, title="/shift history")
    return embed.description


def test_an_expired_code_with_a_reward_says_expired_once_in_its_marker():
    text = _history_text(
        _view(
            2,
            reward="3 Golden Keys",
            reward_from="mentalmars",
            expires_on=(T0 - timedelta(days=1)).date(),
            expires_from="Gearbox",
        )
    )
    assert text.count("expired") == 1
    assert "(expired)" in text
    assert text.endswith(
        "\n3 Golden Keys · details via mentalmars"
    )  # the expiry's credit went with it


def test_an_expired_code_with_no_reward_has_no_details_line_at_all():
    text = _history_text(_view(2, expires_on=(T0 - timedelta(days=1)).date(), expires_from="G"))
    assert text.count("expired") == 1
    assert "via" not in text


def test_a_live_code_still_shows_its_expiry_in_the_line():
    text = _history_text(
        _view(
            1,
            reward="3 Golden Keys",
            reward_from="mentalmars",
            expires_on=(T0 + timedelta(days=5)).date(),
            expires_from="mentalmars",
        )
    )
    assert "(expired)" not in text
    assert "3 Golden Keys · expires Oct 14, 2026 · details via mentalmars" in text


def test_a_permanent_code_is_never_marked_expired_and_keeps_its_line():
    text = _history_text(
        _view(
            1,
            reward="Skin",
            reward_from="m",
            category="permanent",
            category_from="m",
            expires_on=(T0 - timedelta(days=400)).date(),
        )
    )
    assert "expired" not in text
    assert "Skin · permanent · details via m" in text


# =====================================================================================
# A last look at the database layer the other fixes lean on
# =====================================================================================


def test_alerted_codes_columns_default_to_null_for_codes_written_the_old_way(fresh_db):
    with closing(connect(fresh_db)) as conn:
        repo.record_silent_codes(
            conn,
            [(X, "Feed", "https://e/x", "too_old", False)],
            now=lambda: T0,
            mark_seeded=False,
        )
        repo.record_released_codes(
            conn, [(Y, "Feed", "https://e/y", False)], now=lambda: T0, queue_for_games=[]
        )
    assert rows(
        fresh_db, "SELECT code, official_seen_at, table_silenced_at FROM alerted_codes ORDER BY 1"
    ) == [(X, None, None), (Y, None, None)]


def test_the_flag_goes_only_on_too_old_rows_and_never_overwrites(fresh_db):
    with closing(connect(fresh_db)) as conn:
        repo.record_silent_codes(
            conn,
            [(X, "Feed", "u", "too_old", False), (Y, "Feed", "u", "seeded", False)],
            now=lambda: T0,
            mark_seeded=False,
            table_silenced=[X, Y],
        )
        repo.record_silent_codes(
            conn,
            [(X, "Feed", "u", "too_old", False)],
            now=lambda: T0 + timedelta(hours=1),
            mark_seeded=False,
            table_silenced=[],
        )
    stamps = dict(rows(fresh_db, "SELECT code, table_silenced_at FROM alerted_codes"))
    assert stamps[X] == T0.isoformat()
    assert stamps[Y] is None  # a seeded code is not revivable
