"""`python -m newsbot.pipeline.run` in the multi-server world (plan task 13, design §3.1).

The CLI is the one place a person can run the pipeline's pieces without Discord, so these
tests drive `main()` the way a person would: real argument lists, a real temp database,
fixture collectors and the stub model. Nothing touches a network (the `offline` fixture makes
any request fail the test) and nothing needs a secret in the environment.

Several of these are the v2 CLI and pipeline tests, ported: the documented dry-run command
(`test_pipeline_integration.py`), the fixture collector's `full_text`, the no-`full_text`
column guarantee, a throwing collector not sinking the run, and the idempotent `--sweep`
round trip (`test_shift_sweep.py`).
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbot.config import load_config
from newsbot.pipeline import run as run_module
from newsbot.pipeline.collect import CollectionDeps, run_collection
from newsbot.pipeline.run import build_fixture_collectors, main
from newsbot.store import repo
from newsbot.store.db import connect, migrate

FIXTURES = Path(__file__).parent / "fixtures"
INTEGRATION = FIXTURES / "integration"
LLM = INTEGRATION / "llm.json"
PRODLIKE = FIXTURES / "config_v2_prodlike.yaml"
V3 = FIXTURES / "config_v3.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
SHIFT = FIXTURES / "shift"
NOW_ISO = "2026-09-23T17:00:00Z"  # 10:00 Pacific: the friend's 09:00 digest is due
BL4_CHANNEL = 1452017235274240221
PAL_CHANNEL = 1542581309845799013
D4_CHANNEL = 1531681353681211524


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No credentials in the environment: the offline modes must not need any."""
    for name in ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "BRAVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def offline(monkeypatch):
    """Any HTTP request made through `httpx.AsyncClient` fails the test."""
    real = httpx.AsyncClient

    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected HTTP request to {request.url}")

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(refuse)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "t.db")


def count(db_path: str, table: str) -> int:
    with closing(connect(db_path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def prodlike(db, *extra, config=PRODLIKE):
    return ["--config", str(config), "--db", db, *extra]


def day_args(db, *extra):
    return prodlike(
        db, "--fixtures", str(INTEGRATION), "--stub-llm", str(LLM), "--now", NOW_ISO, *extra
    )


# --- the verify command from the plan ---


def test_the_plans_verify_command_prints_the_friends_per_channel_digest(db, capsys, offline):
    code = main(day_args(db, "--dry-run"))

    assert code == 0
    out = capsys.readouterr().out
    assert f"--- #{BL4_CHANNEL} · Borderlands 4 ---" in out
    assert "Patch 1.2 fixes crashes and rebalances loot" in out
    assert f"--- #{PAL_CHANNEL} · Palworld ---" in out
    assert "Palworld 0.6.2 fixes crash bugs" in out
    assert f"#{D4_CHANNEL}" not in out  # nothing for Diablo IV, so no embed for it
    assert "collection: 2/2 sources ok, 3 new items" in out


def test_the_documented_dry_run_command_still_produces_both_stories(db, capsys, offline):
    # Exactly the command docs/self-host.md and the README tell a reader to run, short of a
    # throwaway --db path: --now pins the clock to the fixture data's own frozen date, which
    # keeps this deterministic whatever day it actually is. The example config's admin channel
    # is the placeholder zero; the import must treat that as "none", not die on it.
    code = main(
        [
            "--dry-run",
            "--config",
            str(EXAMPLE),
            "--db",
            db,
            "--fixtures",
            str(INTEGRATION),
            "--stub-llm",
            str(LLM),
            "--now",
            "2026-09-23T09:00:00+00:00",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "Patch 1.2 fixes crashes and rebalances loot" in out
    assert "Palworld 0.6.2 fixes crash bugs" in out


def test_the_documented_command_suppresses_the_brave_key_warning(db, capsys, caplog):
    # config.example.yaml has a web search block and this environment (like a stranger's clean
    # clone) has no BRAVE_API_KEY. --fixtures throws every real collector away, so the "no key"
    # warning load_config would log has nothing true left to say.
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        code = main(
            ["--dry-run", "--config", str(EXAMPLE), "--db", db, "--fixtures", str(INTEGRATION)]
            + ["--stub-llm", str(LLM), "--now", "2026-09-23T09:00:00+00:00"]
        )
    assert code == 0
    assert "BRAVE_API_KEY is not set" not in caplog.text


def test_an_offline_run_needs_no_secrets_and_makes_no_requests(db, offline, capsys):
    # The environment is clean (autouse) and every request fails the test (offline): this run
    # collects from fixtures, summarizes with the stub, and prints.
    assert main(day_args(db, "--dry-run")) == 0
    assert "Patch 1.2" in capsys.readouterr().out


# --- the import runs first, and says so ---


def test_the_first_run_imports_the_v2_setup_and_later_runs_do_not(db, capsys, offline):
    main(day_args(db, "--dry-run"))
    first = capsys.readouterr().err
    assert "[import] Imported the v2 setup for this server (3 games, SHiFT, lounge)" in first

    main(day_args(db, "--dry-run"))
    assert "[import]" not in capsys.readouterr().err
    with closing(connect(db)) as conn:
        assert [g.guild_id for g in repo.list_guilds(conn)] == [100000000000000001]


def test_a_failed_import_exits_2_and_writes_nothing(tmp_path, db, capsys):
    games = "\n".join(
        f"  - {{key: g{i}, name: 'G{i}', channel_id: {i + 1}}}" for i in range(11)
    )  # one over the ten-games-per-server limit
    config = tmp_path / "c.yaml"
    config.write_text(
        f"guild_id: 1\ndigest: {{time: '09:00', timezone: UTC}}\ntopics:\n{games}\nsources: []\n"
    )

    # --post-to-stdout, because a dry run works on a copy and would prove nothing about the file.
    argv = ["--config", str(config), "--db", db, "--stub-llm", str(LLM), "--post-to-stdout"]
    assert main(argv) == 2

    assert "can follow at most 10" in capsys.readouterr().err
    assert count(db, "guilds") == 0


# --- choosing the server ---


def make_server(db, guild_id, *, tier="free", time="09:00", tz="UTC", games=None):
    with closing(connect(db)) as conn:
        migrate(conn)
        repo.create_guild(conn, guild_id, digest_time=time, timezone=tz, tier=tier, set_up=True)
        repo.set_guild_games(conn, guild_id, games or [("palworld", 7)])


def store_item(db, *, when, game="palworld", title="Palworld patch notes", url=None):
    from newsbot.store.models import StoredItem

    with closing(connect(db)) as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url or f"https://example.com/{game}/{title}",
                    title,
                    "",
                    "Feed",
                    "official",
                    None,
                    {game: False},
                )
            ],
            now=lambda: when,
        )


def v3_args(db, *extra, now=NOW_ISO):
    return ["--config", str(V3), "--db", db, "--stub-llm", str(LLM), "--now", now, *extra]


def test_dry_run_with_no_server_set_up_says_so_and_exits_2(db, capsys):
    assert main(v3_args(db, "--dry-run")) == 2
    assert "no server is set up" in capsys.readouterr().err


def test_dry_run_with_exactly_one_server_needs_no_guild_flag(db, capsys):
    make_server(db, 11)
    store_item(db, when=datetime(2026, 9, 23, 12, tzinfo=UTC))

    assert main(v3_args(db, "--dry-run")) == 0

    assert "Palworld patch notes" in capsys.readouterr().out


def test_dry_run_with_two_servers_lists_them_and_asks_for_guild(db, capsys):
    make_server(db, 11)
    make_server(db, 12)

    assert main(v3_args(db, "--dry-run")) == 2

    err = capsys.readouterr().err
    assert "pick one with --guild (11, 12)" in err


def test_guild_picks_one_of_two_and_a_stranger_is_an_error(db, capsys):
    make_server(db, 11)
    make_server(db, 12, games=[("borderlands4", 8)])
    store_item(
        db, when=datetime(2026, 9, 23, 12, tzinfo=UTC), game="borderlands4", title="BL4 news"
    )

    assert main(v3_args(db, "--dry-run", "--guild", "12")) == 0
    assert "BL4 news" in capsys.readouterr().out

    assert main(v3_args(db, "--dry-run", "--guild", "99")) == 2
    assert "server 99 isn't set up in this database (set up: 11, 12)" in capsys.readouterr().err


def test_guild_without_check_sources_is_not_a_thing_for_game(db, capsys):
    assert main(["--config", str(V3), "--db", db, "--game", "palworld"]) == 2
    assert "--game only works together with --check-sources" in capsys.readouterr().err


# --- --dry-run writes nothing ---


def tables(db) -> dict[str, int]:
    names = ("items", "stories", "digests", "game_summaries", "guild_notices", "source_health")
    return {name: count(db, name) for name in names}


def test_dry_run_for_a_comped_server_writes_nothing_not_even_the_summary(db, capsys):
    make_server(db, 11, tier="comped")
    # The stub model's story cites this url, so the item has to be the one it was written for.
    store_item(
        db,
        when=datetime(2026, 9, 23, 12, tzinfo=UTC),
        title="Palworld v0.6.2 Patch Notes",
        url="https://example.com/palworld/patch",
    )
    before = tables(db)

    assert main(v3_args(db, "--dry-run")) == 0

    out = capsys.readouterr().out
    assert "Palworld 0.6.2 fixes crash bugs" in out  # the stub's story, computed in memory
    assert tables(db) == before
    assert before["game_summaries"] == 0 and before["digests"] == 0


# --- --post-to-stdout: the per-server guard ---


def test_post_to_stdout_claims_prints_saves_and_refuses_the_same_day_twice(db, capsys):
    make_server(db, 11, time="09:00", tz="UTC")
    make_server(db, 12, time="09:00", tz="UTC", games=[("borderlands4", 8)])
    # Collected before the 09:00 UTC due instant, so it falls in that day's window.
    store_item(db, when=datetime(2026, 9, 22, 12, tzinfo=UTC), title="Raid boss patch")
    args = v3_args(db, "--post-to-stdout", "--guild", "11", now="2026-09-23T09:30:00Z")

    assert main(args) == 0
    first = capsys.readouterr()
    assert "Raid boss patch" in first.out
    with closing(connect(db)) as conn:
        row = repo.get_guild_digest(conn, 11, datetime(2026, 9, 23).date())
        assert row is not None and row.status == "ok"
        assert repo.get_guild_digest(conn, 12, datetime(2026, 9, 23).date()) is None  # untouched

    assert main(args) == 0  # the guard: today is already claimed
    second = capsys.readouterr()
    assert "Raid boss patch" not in second.out
    assert "isn't due a digest" in second.err
    assert count(db, "digests") == 1

    assert main([*args, "--force"]) == 0  # an explicit rerun does print again
    assert "Raid boss patch" in capsys.readouterr().out


def test_now_decides_whether_the_server_is_due(db, capsys):
    make_server(db, 11, time="09:00", tz="UTC")
    store_item(db, when=datetime(2026, 9, 23, 7, tzinfo=UTC), title="Early news")

    assert main(v3_args(db, "--post-to-stdout", now="2026-09-23T08:59:00Z")) == 0
    assert "isn't due a digest" in capsys.readouterr().err
    assert count(db, "digests") == 0

    assert main(v3_args(db, "--post-to-stdout", now="2026-09-23T09:00:00Z")) == 0
    assert "Early news" in capsys.readouterr().out
    assert count(db, "digests") == 1


# --- --collect and --sweep ---


def test_collect_runs_one_pass_into_the_database_and_nothing_else(db, capsys, offline):
    code = main(prodlike(db, "--collect", "--fixtures", str(INTEGRATION), "--now", NOW_ISO))

    assert code == 0
    out = capsys.readouterr().out
    assert "collection: 2/2 sources ok, 3 new items" in out
    assert count(db, "items") == 3
    assert count(db, "digests") == 0 and count(db, "stories") == 0  # no digest from --collect


def test_sweep_is_an_alias_for_collect_with_a_note(db, capsys, offline):
    code = main(prodlike(db, "--sweep", "--fixtures", str(INTEGRATION), "--now", NOW_ISO))

    assert code == 0
    captured = capsys.readouterr()
    assert "--sweep is now --collect" in captured.err
    assert "collection: 2/2 sources ok" in captured.out
    assert count(db, "items") == 3


def test_collect_twice_against_the_same_fixtures_is_idempotent(tmp_path, offline):
    # The v2 `--sweep` round trip: the fixture's code is recorded on the first pass (silently,
    # while the feed's own history is being learned) and the second pass finds nothing new.
    db = str(tmp_path / "sweep.db")
    args = ["--config", str(SHIFT / "config.yaml"), "--db", db, "--fixtures", str(SHIFT)]
    args += ["--collect", "--now", "2026-09-23T09:00:00Z"]

    assert main(args) == 0
    with closing(connect(db)) as conn:
        first = [tuple(r) for r in conn.execute("SELECT code, status FROM alerted_codes")]
        state = repo.get_alert_state(conn)
    assert len(first) == 1 and first[0][1] == "seeded"
    assert state.seeded is True  # a healthy first pass flips the marker

    assert main(args) == 0
    with closing(connect(db)) as conn:
        second = [tuple(r) for r in conn.execute("SELECT code, status FROM alerted_codes")]
        assert repo.get_alert_state(conn).ping_count == 0
    assert second == first  # never posted, never pinged, not recorded twice


def test_fixtures_text_is_read_but_never_stored(db, offline):
    # An item with `full_text` makes it all the way through a pass without error, and the items
    # table has nowhere to put it (the privacy promise: article text is read, then forgotten).
    assert main(prodlike(db, "--collect", "--fixtures", str(INTEGRATION), "--now", NOW_ISO)) == 0

    with closing(connect(db)) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM items")])
    assert "full_text" not in columns
    assert "this week only" not in dump  # the sentence only the full text carried


# --- the fixture collector (v2: test_pipeline_integration.py) ---


async def test_the_fixture_collector_reads_optional_full_text():
    borderlands = next(c for c in build_fixture_collectors(INTEGRATION) if c.name == "borderlands4")
    async with httpx.AsyncClient() as http:
        items = await borderlands.collect(http)
    patch_item = next(i for i in items if i.url == "https://example.com/bl4/patch-1")
    assert patch_item.full_text == (
        "Fixes several crashes and rebalances loot drop rates based on community "
        "feedback. Redeem code AAAA1-BBBBB-CCCCC-DDDDD-EEEEE this week only."
    )


async def test_the_fixture_collector_full_text_defaults_to_none():
    borderlands = next(c for c in build_fixture_collectors(INTEGRATION) if c.name == "borderlands4")
    async with httpx.AsyncClient() as http:
        items = await borderlands.collect(http)
    thread = next(i for i in items if i.url == "https://example.com/bl4/community-thread")
    assert thread.full_text is None


def test_llm_json_is_not_mistaken_for_a_collector():
    assert "llm" not in {c.name for c in build_fixture_collectors(INTEGRATION)}


# --- a throwing collector doesn't sink the pass (v2: test_run_daily_state_machine.py) ---


class _ThrowingCollector:
    name = "cursed_source"
    source_type = "rss"
    rate_limit_key = None

    async def collect(self, http):
        raise RuntimeError("this parser has never worked and today is no exception")


async def test_a_throwing_collector_costs_the_pass_nothing_and_is_recorded_in_health(db):
    with closing(connect(db)) as conn:
        migrate(conn)
    cfg = load_config(PRODLIKE)
    alerts: list[str] = []

    async def alert(text: str) -> None:
        alerts.append(text)

    async with httpx.AsyncClient() as http:
        deps = CollectionDeps(
            cfg=cfg,
            db_path=db,
            http=http,
            collectors=[*build_fixture_collectors(INTEGRATION), _ThrowingCollector()],
            now=lambda: datetime(2026, 9, 23, 17, tzinfo=UTC),
            alert=alert,
        )
        outcome = await run_collection(deps)

    assert outcome.new_items == 3  # the good sources' items still made it in
    assert outcome.failing_sources == ("cursed_source",)
    with closing(connect(db)) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, last_error FROM source_health WHERE source_name = ?",
            ("cursed_source",),
        ).fetchone()
    assert row["consecutive_failures"] == 1
    assert "this parser has never worked" in row["last_error"]
    assert alerts == []  # one failure is not worth the owner's attention


# --- --check-sources ---


@pytest.fixture
def feeds(monkeypatch):
    """Every request returns an empty, valid feed and is recorded; returns the URL list."""
    seen: list[str] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).split("?")[0])
        return httpx.Response(200, content=b"<rss><channel></channel></rss>")

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return seen


def test_check_sources_game_checks_only_that_games_sources_and_the_shared_ones(feeds, capsys):
    code = main(["--config", str(V3), "--check-sources", "--game", "palworld"])

    assert code == 0
    out = capsys.readouterr().out
    cfg = load_config(V3)
    palworld = next(g for g in cfg.catalog if g.key == "palworld")
    borderlands = next(g for g in cfg.catalog if g.key == "borderlands4")
    assert "Palworld" in out and "palworld (Palworld)" in out
    assert "borderlands4 (Borderlands 4)" not in out  # not even in the match counts
    for source in borderlands.sources:
        assert source.name not in out, source.name
    for source in palworld.sources:
        assert source.name in out, source.name
    # Shared sources restricted to other games are left out; unrestricted ones stay.
    assert "PC Gamer" in out and "2K Newsroom" not in out
    assert feeds  # and it really did make requests, which the table was built from


def test_check_sources_without_game_covers_every_game_grouped_by_game(feeds, capsys):
    assert main(["--config", str(V3), "--check-sources"]) == 0

    out = capsys.readouterr().out
    assert "Borderlands 4\n" in out and "Palworld\n" in out  # a heading per game
    assert "Games matched:" in out
    for key in ("borderlands4", "palworld", "rust"):
        assert f"  {key} (" in out


def test_check_sources_game_may_be_repeated_and_unknown_games_are_refused(feeds, capsys):
    assert (
        main(["--config", str(V3), "--check-sources", "--game", "rust", "--game", "palworld"]) == 0
    )
    out = capsys.readouterr().out
    assert "rust (" in out and "palworld (" in out and "borderlands4 (" not in out

    assert main(["--config", str(V3), "--check-sources", "--game", "nope"]) == 2
    assert "isn't in the catalog: nope" in capsys.readouterr().err


def test_check_sources_needs_no_database_and_no_credentials(feeds, tmp_path, capsys):
    db = tmp_path / "must-not-exist.db"

    assert main(["--config", str(V3), "--db", str(db), "--check-sources"]) == 0

    assert not db.exists()


# --- the clock reaches every step ---


def test_now_must_be_an_iso_timestamp(db, capsys):
    assert main(prodlike(db, "--dry-run", "--now", "yesterday-ish")) == 2
    assert "not a valid ISO 8601 timestamp" in capsys.readouterr().err


def test_a_bare_timestamp_is_read_as_utc(db, capsys, offline):
    # 17:00 with no zone is 17:00 UTC (10:00 Pacific): the same digest as the explicit Z.
    args = prodlike(db, "--fixtures", str(INTEGRATION), "--stub-llm", str(LLM), "--dry-run")
    assert main([*args, "--now", "2026-09-23T17:00:00"]) == 0
    assert "Patch 1.2" in capsys.readouterr().out


def test_the_collection_time_is_the_clock_not_the_wall(db, offline):
    main(day_args(db, "--dry-run"))

    with closing(connect(db)) as conn:
        stamps = {r[0] for r in conn.execute("SELECT collected_at FROM items")}
    assert stamps == {datetime(2026, 9, 23, 17, tzinfo=UTC).isoformat()}


def test_the_retired_v2_runner_is_gone_from_the_module():
    assert run_module.main is main
    assert not hasattr(run_module, "run_daily")
    assert not hasattr(run_module, "build_digest")
