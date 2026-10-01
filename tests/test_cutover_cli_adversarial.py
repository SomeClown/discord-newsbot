"""`python -m newsbot.pipeline.run` against a database that matters.

The CLI is how I check a cutover before doing it, which makes it the thing most likely to be
pointed at the production database "just to see", and so the thing whose side effects need to
be known rather than discovered. `--dry-run` is documented as a rehearsal that writes
nothing, and for a server that's already been imported that's true to the byte (these tests
compare a full dump of the database before and after). Against a database that is still
v2.2's, it isn't: every mode except `--check-sources` runs the once-ever import first, and a
dry run is a mode. That has consequences, worked out below, one of which is a strict xfail:
an early dry run freezes the SHiFT ping budget at the moment of the import, and v2.2 keeps
spending it until the real cutover.

The rest is the flag matrix: how many servers, with and without `--guild`, `--post-to-stdout`
and `--force` against rows v2.2 left behind, `--check-sources` never touching a database, the
`--sweep` alias, `--now` stamping everything it touches, and the placeholder admin channel
that has to mean "none".
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cutover_world import (
    FRIEND,
    PRODLIKE,
    SHIFT_CH,
    TODAY,
    add_item,
    code_item,
    collect_these,
    v22_digest,
    write_fixture_items,
)

from newsbot.config import load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.pipeline.run import main
from newsbot.store import repo
from newsbot.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
LLM = FIXTURES / "integration" / "llm.json"
V3 = FIXTURES / "config_v3.yaml"
NOW = "2026-10-01T17:00:00Z"  # 10:00 Pacific: the friend's digest is due
NOW_DT = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "BRAVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def offline(monkeypatch):
    real = httpx.AsyncClient

    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected HTTP request to {request.url}")

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(refuse)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


def dump(db_path) -> list[str]:
    with closing(connect(db_path)) as conn:
        return list(conn.iterdump())


def table(db_path, name: str) -> list[tuple]:
    with closing(connect(db_path)) as conn:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {name}")]  # noqa: S608


def args(db, *extra, config=PRODLIKE):
    return ["--config", str(config), "--db", str(db), "--stub-llm", str(LLM), "--now", NOW, *extra]


@pytest.fixture
def imported(v22_db):
    """The v2.2 database after the one-time import, with a patch note waiting for the stub."""
    assert ensure_imported(str(v22_db), load_config(PRODLIKE), lambda: NOW_DT) is not None
    # The url the stub model's story cites, so a preview has a real story to print.
    add_item(
        str(v22_db),
        "borderlands4",
        "patch-1",
        NOW_DT - timedelta(hours=2),
        url="https://example.com/bl4/patch-1",
    )
    return v22_db


# --- --dry-run writes nothing (once the server has been imported) ---


@pytest.mark.parametrize("extra", [[], ["--guild", str(FRIEND)], ["--force"]])
def test_a_dry_run_leaves_every_table_exactly_as_it_found_it(imported, capsys, offline, extra):
    before = dump(imported)

    assert main(args(imported, "--dry-run", *extra)) == 0

    out = capsys.readouterr().out
    assert "Patch 1.2 fixes crashes and rebalances loot" in out  # a real preview, not a no-op
    assert dump(imported) == before  # app_state, game_summaries, digests, items: all of it


def test_a_dry_run_that_has_to_summarize_does_not_leave_the_summary_behind(
    imported, capsys, offline
):
    stories = len(table(imported, "stories"))

    main(args(imported, "--dry-run"))

    assert table(imported, "game_summaries") == []
    assert len(table(imported, "stories")) == stories


def test_a_dry_run_does_not_claim_the_day_so_the_real_digest_still_posts(imported, offline):
    main(args(imported, "--dry-run"))
    main(args(imported, "--dry-run"))

    with closing(connect(imported)) as conn:
        assert repo.get_guild_digest(conn, FRIEND, datetime(2026, 10, 1).date()) is None


# --- --dry-run against a database that is still v2.2's ---


def test_a_dry_run_against_a_v22_database_performs_the_once_ever_import(v22_db, capsys, offline):
    # Pinned: the module docstring says so, and it's the thing to know before pointing this at
    # the production file. The import is silent to the bot afterwards (no notice for the owner).
    assert table(v22_db, "guilds") == []

    assert main(args(v22_db, "--dry-run")) == 0

    err = capsys.readouterr().err
    assert "[import] Imported the v2 setup" in err
    with closing(connect(v22_db)) as conn:
        assert [g.guild_id for g in repo.list_guilds(conn)] == [FRIEND]
        assert repo.app_state_get(conn, "import") is not None
    assert ensure_imported(str(v22_db), load_config(PRODLIKE)) is None  # so the bot has no report


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the import copies the SHiFT ping budget once, and a dry run can be the one that runs "
        "it, any time before the cutover. v2.2 keeps spending the budget in alert_state "
        "meanwhile, and nothing carries the later count over, so a server whose three pings "
        "were spent after an early dry run gets three more on cutover day"
    ),
)
async def test_the_ping_budget_v22_spends_after_an_early_dry_run_stays_spent(
    v22_db, make_world, offline
):
    # (`main` runs its own event loop, so it gets a thread of its own inside an async test.)
    await asyncio.to_thread(main, args(v22_db, "--dry-run"))  # imports; ping_count 2 on 09-30
    with closing(connect(v22_db)) as conn, conn:
        conn.execute("UPDATE alert_state SET value = ? WHERE key = 'ping_day'", (TODAY,))
        conn.execute("UPDATE alert_state SET value = '3' WHERE key = 'ping_count'")

    world = await make_world(now=NOW_DT)

    assert world.rows("SELECT ping_day, ping_count FROM guild_shift") == [(TODAY, 3)]


async def test_what_v22_does_after_an_early_dry_run_import_is_still_handled(
    v22_db, make_world, offline, monkeypatch, tmp_path
):
    # The other half: the digest row v2.2 writes later is adopted at startup, and a code it
    # alerts later is known globally, so neither is repeated by the first v3 start.
    await asyncio.to_thread(main, args(v22_db, "--dry-run"))
    v22_digest(str(v22_db), TODAY, "ok", posted_ids="[301]")
    later = "LATER-LATER-LATER-LATER-LATE1"
    with closing(connect(v22_db)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, pinged, "
            "status) VALUES (?, '2026-10-01T15:00:00+00:00', 'Feed', 'https://e.example', 1, "
            "'posted')",
            (later,),
        )
    world = await make_world(now=NOW_DT)
    collect_these(monkeypatch, tmp_path, [code_item("later", later)])

    await world.tick(NOW_DT)
    await world.bot._collection_job()

    assert [m for ch in world.channels.values() for m in ch.sent if m.embed] == []
    assert world.sent(SHIFT_CH) == []
    assert world.rows("SELECT guild_id FROM digests WHERE run_date = ?", TODAY) == [(FRIEND,)]


# --- --fixtures and --collect: what "writes nothing" doesn't cover ---


def test_fixtures_imply_a_collection_pass_so_that_dry_run_does_write_items(imported, offline):
    before = len(table(imported, "items"))

    # (The fixtures' own dates are 2026-09-23, so the clock has to say so.)
    main(
        args(
            imported,
            "--dry-run",
            "--fixtures",
            str(FIXTURES / "integration"),
            "--now",
            "2026-09-23T17:00:00Z",
        )
    )

    assert len(table(imported, "items")) > before  # documented: --fixtures is a collection pass


def test_a_cli_collection_pass_marks_codes_posted_that_it_only_printed(
    imported, capsys, offline, tmp_path
):
    # Pinned, and the same as v2's `--sweep`: the CLI's poster prints, but the fan-out records
    # the code as posted for every SHiFT server. Run against the production file, it eats
    # codes the real bot would have announced.
    new = "EATEN-EATEN-EATEN-EATEN-EATE1"
    directory = write_fixture_items(tmp_path / "pass", "borderlands4", [code_item("c", new)])

    assert main(args(imported, "--collect", "--fixtures", str(directory))) == 0

    assert new in capsys.readouterr().out
    with closing(connect(imported)) as conn:
        status = conn.execute("SELECT status FROM alerted_codes WHERE code = ?", (new,)).fetchone()
        posted = conn.execute(
            "SELECT status FROM guild_code_posts WHERE code = ? AND guild_id = ?", (new, FRIEND)
        ).fetchone()
    assert status[0] == "posted" and posted[0] == "posted"


# --- --post-to-stdout and --force against what v2.2 left behind ---


def test_post_to_stdout_refuses_a_day_v22_already_posted_and_changes_nothing(
    imported, capsys, offline
):
    v22_digest(str(imported), TODAY, "ok", posted_ids="[301]")
    with closing(connect(imported)) as conn, conn:
        conn.execute("UPDATE digests SET guild_id = ? WHERE guild_id IS NULL", (FRIEND,))
    before = dump(imported)

    assert main(args(imported, "--post-to-stdout")) == 0

    assert "isn't due a digest" in capsys.readouterr().err
    assert dump(imported) == before


def test_force_reposts_over_a_v22_day_when_asked_to_and_says_so_only_there(
    imported, capsys, offline
):
    v22_digest(str(imported), TODAY, "ok", posted_ids="[301]")
    with closing(connect(imported)) as conn, conn:
        conn.execute("UPDATE digests SET guild_id = ? WHERE guild_id IS NULL", (FRIEND,))

    assert main(args(imported, "--post-to-stdout", "--force")) == 0

    assert "Patch 1.2 fixes crashes" in capsys.readouterr().out
    with closing(connect(imported)) as conn:
        row = repo.get_guild_digest(conn, FRIEND, datetime(2026, 10, 1).date())
    assert row.status in ("ok", "partial")


def test_the_digest_row_a_cli_run_writes_is_stamped_with_now_not_the_wall_clock(imported, offline):
    main(args(imported, "--post-to-stdout"))

    with closing(connect(imported)) as conn:
        created, updated = conn.execute(
            "SELECT created_at, updated_at FROM digests WHERE guild_id = ? AND run_date = ?",
            (FRIEND, TODAY),
        ).fetchone()
    assert created.startswith("2026-10-01T17:00") and updated.startswith("2026-10-01T17:00")


def test_the_import_is_stamped_with_now_too(v22_db, offline):
    main(args(v22_db, "--dry-run"))

    with closing(connect(v22_db)) as conn:
        (guild,) = repo.list_guilds(conn)
    assert guild.imported_at == NOW_DT


# --- choosing the server ---


def test_a_guild_that_is_not_a_number_is_an_argparse_error(imported):
    with pytest.raises(SystemExit) as exit_info:
        main(args(imported, "--dry-run", "--guild", "abc"))

    assert exit_info.value.code == 2


def test_dry_run_and_post_to_stdout_cannot_be_combined(imported):
    with pytest.raises(SystemExit) as exit_info:
        main(args(imported, "--dry-run", "--post-to-stdout"))

    assert exit_info.value.code == 2


def test_a_set_up_server_that_follows_nothing_says_so_and_exits_1(imported, capsys, offline):
    with closing(connect(imported)) as conn, conn:
        conn.execute("DELETE FROM guild_games WHERE guild_id = ?", (FRIEND,))

    code = main(args(imported, "--dry-run", "--guild", str(FRIEND)))

    assert code == 1
    assert "isn't set up or follows no games" in capsys.readouterr().err


def test_a_row_that_exists_but_is_not_set_up_is_not_a_server_to_the_cli(imported, capsys, offline):
    with closing(connect(imported)) as conn:
        repo.create_guild(conn, 555, set_up=False)

    assert main(args(imported, "--dry-run", "--guild", "555")) == 2

    assert "server 555 isn't set up" in capsys.readouterr().err


def test_no_config_at_all_exits_2(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("NEWSBOT_CONFIG", raising=False)

    assert main(["--db", str(tmp_path / "x.db"), "--dry-run"]) == 2

    assert "--config" in capsys.readouterr().err


# --- --check-sources ---


@pytest.fixture
def feeds(monkeypatch):
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss><channel></channel></rss>")

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


def test_check_sources_never_imports_and_never_touches_an_existing_database(v22_db, feeds, capsys):
    before = dump(v22_db)

    code = main(["--config", str(PRODLIKE), "--db", str(v22_db), "--check-sources"])

    assert code == 0
    assert "[import]" not in capsys.readouterr().err
    assert dump(v22_db) == before  # no guild rows, no import marker, nothing


def test_check_sources_with_a_sweep_flag_is_still_just_check_sources(v22_db, feeds, capsys):
    code = main(["--config", str(PRODLIKE), "--db", str(v22_db), "--check-sources", "--sweep"])

    assert code == 0 and "--sweep is now" not in capsys.readouterr().err


# --- --sweep, the alias ---


def test_sweep_with_dry_run_collects_then_previews_and_says_it_is_an_alias(
    imported, capsys, offline
):
    code = main(args(imported, "--sweep", "--dry-run", "--fixtures", str(FIXTURES / "integration")))

    captured = capsys.readouterr()
    assert code == 0
    assert "--sweep is now --collect" in captured.err
    assert "collection:" in captured.out and "Patch 1.2" in captured.out


def test_sweep_alone_does_not_preview_anything(imported, capsys, offline):
    code = main(args(imported, "--sweep", "--fixtures", str(FIXTURES / "integration")))

    out = capsys.readouterr().out
    assert code == 0 and "collection:" in out and "--- #" not in out


# --- the placeholder admin channel ---


def placeholder_config(tmp_path, written: str) -> Path:
    config = tmp_path / "placeholder.yaml"
    config.write_text(
        PRODLIKE.read_text().replace(
            "admin_channel_id: 100000000000000002", f"admin_channel_id: {written}"
        )
    )
    return config


@pytest.mark.parametrize("written", ["0", "000000000000000000", "-5"])
def test_a_placeholder_admin_channel_means_no_admin_channel_for_the_imported_server(
    tmp_path, written, v3_db
):
    cfg = load_config(placeholder_config(tmp_path, written))

    assert cfg.legacy.admin_channel_id is None
    assert ensure_imported(v3_db, cfg, lambda: NOW_DT) is not None
    with closing(connect(v3_db)) as conn:
        (guild,) = repo.list_guilds(conn)
    assert guild.admin_channel_id is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "only the import's view of admin_channel_id treats a placeholder as none. "
        "AppConfig.admin_channel_id, which is the owner's channel and what the Router is built "
        "with, stays 0 (or -5), so with config.example.yaml's placeholder every owner alert "
        "tries to fetch channel 0 from Discord, fails, and logs an exception instead of "
        "quietly having nowhere to go"
    ),
)
@pytest.mark.parametrize("written", ["0", "-5"])
def test_a_placeholder_admin_channel_is_also_no_owner_channel(tmp_path, written):
    assert load_config(placeholder_config(tmp_path, written)).admin_channel_id is None


def test_a_server_with_no_admin_channel_still_gets_its_digest_run_without_a_report(
    tmp_path, capsys, offline
):
    config = placeholder_config(tmp_path, "0")
    db = tmp_path / "t.db"

    code = main(
        [
            *args(db, "--post-to-stdout", config=config),
            "--fixtures",
            str(FIXTURES / "integration"),
            "--now",
            "2026-09-23T17:00:00Z",
        ]
    )

    assert code == 0
    assert "Patch 1.2 fixes crashes" in capsys.readouterr().out


async def test_with_the_placeholder_an_owner_alert_asks_discord_for_channel_zero(
    make_world, tmp_path, monkeypatch
):
    # Pinned consequence of the xfail above: the alert has "somewhere to go", so it goes and
    # looks for it. Harmless (the lookup fails and is logged), but it's a REST call per alert.
    from types import SimpleNamespace

    import discord

    world = await make_world(now=NOW_DT, cfg=load_config(placeholder_config(tmp_path, "0")))
    asked: list[int] = []

    async def fetch(channel_id):
        asked.append(channel_id)
        raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Channel")

    monkeypatch.setattr(world.bot, "fetch_channel", fetch)

    await world.ready()  # the import notice is an owner alert

    assert 0 in asked
