"""The per-server run report's stories, Sources and Claude lines (v3.0.2).

v2.2's report had all three; v3.0.0 dropped Sources and Claude and called every count
"items". These pin the restored lines: first the renderer on its own, then a few runs
end to end against a real database, since the Sources line is only as good as the
health rows and summary rows it reads.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from test_guild_digest import DUE, G1, G2, World, in_window

from newsbot.bot.format import (
    ReportClaude,
    ReportSources,
    discord_len,
    render_guild_run_report,
    report_claude_from_rows,
)
from newsbot.config import GameCfg
from newsbot.pipeline.guild_digest import GameSummary, run_guild_digest
from newsbot.pipeline.run import RunKind
from newsbot.store import repo
from newsbot.store.models import StoryToSave, Usage

BL4 = GameCfg(key="borderlands4", name="Borderlands 4")
PAL = GameCfg(key="palworld", name="Palworld")
LA = "America/Los_Angeles"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# 08:30 PDT on 2026-09-30, half an hour before the digest was due.
PREPARED = datetime(2026, 9, 30, 15, 30, tzinfo=UTC)


def report(**overrides) -> str:
    args = dict(
        status="ok",
        run_date=date(2026, 9, 30),
        run_kind="scheduled",
        games=[BL4, PAL],
        counts={"borderlands4": 3, "palworld": 2},
        channels={"borderlands4": 11, "palworld": 12},
        posted_by_game={"borderlands4": 777, "palworld": 778},
        skipped={},
        duration=timedelta(seconds=3),
        notes=[],
        guild_id=42,
        timezone=LA,
    )
    args.update(overrides)
    return render_guild_run_report(**args)


def lines(text: str) -> list[str]:
    return text.splitlines()


# --- stories vs items ---


def test_a_free_server_counts_items():
    assert lines(report())[1].startswith("5 items: Borderlands 4 3 ")


def test_a_comped_digest_of_summaries_counts_stories():
    text = report(story_games={"borderlands4", "palworld"})
    assert lines(text)[1].startswith("5 stories: Borderlands 4 3 ")


def test_a_mixed_comped_digest_counts_each_kind_separately():
    text = report(story_games={"borderlands4"})
    assert lines(text)[1].startswith("3 stories, 2 headlines: ")


def test_a_comped_digest_that_fell_back_everywhere_counts_headlines():
    assert lines(report(story_games=set()))[1].startswith("5 headlines: ")


def test_one_of_a_kind_is_singular():
    text = report(counts={"borderlands4": 1, "palworld": 1}, story_games={"borderlands4"})
    assert lines(text)[1].startswith("1 story, 1 headline: ")
    assert lines(report(counts={"borderlands4": 1, "palworld": 0}))[1].startswith("1 item: ")


# --- the Sources line ---


def sources_line(**kw) -> str:
    [line] = [ln for ln in lines(report(**kw)) if ln.startswith("Sources:")]
    return line


def test_no_sources_object_means_no_sources_line():
    assert "Sources:" not in report()


def test_a_server_with_no_sources_has_no_sources_line():
    assert "Sources:" not in report(sources=ReportSources(0, 0, []))


def test_zero_failures_is_a_plain_count():
    assert sources_line(sources=ReportSources(28, 0, [])) == "Sources: 28 of 28 ok"


def test_one_failure_names_it_and_its_error():
    sources = ReportSources(28, 0, [("r/Palworld", "403 Blocked")])
    assert sources_line(sources=sources) == "Sources: 27 of 28 ok (r/Palworld: 403 Blocked)"


def test_many_failures_name_the_first_and_count_the_rest():
    failing = [("r/Palworld", "403 Blocked"), ("Feed B", "boom"), ("Feed C", "boom")]
    sources = ReportSources(28, 0, failing)
    assert sources_line(sources=sources) == (
        "Sources: 25 of 28 ok (r/Palworld: 403 Blocked; +2 more)"
    )


def test_unchecked_sources_are_not_failures_and_are_said_so():
    sources = ReportSources(28, 2, [("r/Palworld", "403 Blocked")])
    assert sources_line(sources=sources) == (
        "Sources: 25 of 28 ok, 2 not checked yet (r/Palworld: 403 Blocked)"
    )
    assert sources_line(sources=ReportSources(5, 5, [])) == "Sources: 5 sources not checked yet"
    assert sources_line(sources=ReportSources(1, 1, [])) == "Sources: 1 source not checked yet"


def test_a_failure_with_no_recorded_error_still_reads():
    sources = ReportSources(3, 0, [("Feed", None)])
    assert sources_line(sources=sources) == "Sources: 2 of 3 ok (Feed: unknown error)"


def test_an_error_keeps_its_url_but_has_no_markdown_link_and_no_live_scheme():
    error = (
        "Client error '429 Too Many Requests' for url "
        "'https://www.reddit.com/r/Palworld/top/.rss?t=day&token=hunter2' "
        "[click](https://evil.example/x) <@123456789012345678> @everyone"
    )
    line = sources_line(sources=ReportSources(5, 0, [("r/Palworld", error)]))
    assert "reddit.com/r/Palworld/top/.rss" in line
    assert "hunter2" not in line  # shown_url drops the query
    assert not re.search(r"\]\(", line)  # nothing left that Discord would read as a link
    assert "https://" not in line and "http://" not in line  # the scheme is defused
    assert "@everyone" not in line.replace("@​everyone", "")
    assert not re.search(r"<@[!&]?\d{17,20}>", line)


def test_a_multi_line_error_shows_its_first_line_only_and_is_capped():
    sources = ReportSources(2, 0, [("Feed", "first line\n" + "x" * 500)])
    assert sources_line(sources=sources) == "Sources: 1 of 2 ok (Feed: first line)"
    sources = ReportSources(2, 0, [("Feed", "y" * 500)])
    assert discord_len(sources_line(sources=sources)) < 300


def test_a_hostile_source_name_cannot_ping_or_format():
    sources = ReportSources(2, 0, [("**@everyone**", "bad")])
    line = sources_line(sources=sources)
    assert "@everyone" not in line.replace("@​everyone", "")
    assert "\\*\\*" in line


# --- the Claude line ---


def last_line(text: str) -> str:
    return lines(text)[-1]


def test_a_free_server_gets_only_took():
    assert last_line(report()) == "Took 3s"


def test_the_claude_line_shows_cost_prepare_time_and_took():
    claude = ReportClaude(0.04, [PREPARED], shared=False)
    assert last_line(report(claude=claude)) == (
        "Claude: ~$0.04 (summary prepared at 08:30) · took 3s"
    )


def test_several_summaries_say_summaries_and_a_spread_shows_a_range():
    same = ReportClaude(0.04, [PREPARED, PREPARED], shared=False)
    assert "(summaries prepared at 08:30)" in last_line(report(claude=same))
    spread = ReportClaude(0.04, [PREPARED, PREPARED + timedelta(minutes=12)], shared=False)
    assert "(summaries prepared at 08:30 to 08:42)" in last_line(report(claude=spread))


def test_prepare_time_is_in_the_servers_own_clock():
    claude = ReportClaude(0.04, [PREPARED], shared=False)
    assert "prepared at 17:30" in last_line(report(claude=claude, timezone="Europe/Berlin"))
    assert "prepared at 15:30 UTC" in last_line(report(claude=claude, timezone="Not/AZone"))


def test_a_shared_summary_says_shared_with_the_full_cost():
    claude = ReportClaude(0.04, [PREPARED], shared=True)
    assert last_line(report(claude=claude)) == (
        "Claude: ~$0.04, shared (summary prepared at 08:30) · took 3s"
    )


def test_missing_cost_drops_the_cost_and_keeps_the_rest():
    claude = ReportClaude(None, [PREPARED], shared=False)
    assert last_line(report(claude=claude)) == "Claude: summary prepared at 08:30 · took 3s"
    shared = ReportClaude(None, [PREPARED], shared=True)
    assert last_line(report(claude=shared)) == (
        "Claude: shared (summary prepared at 08:30) · took 3s"
    )


def test_a_claude_line_with_nothing_to_show_falls_back_to_took():
    assert last_line(report(claude=ReportClaude(None, [], shared=False))) == "Took 3s"


def test_a_fraction_of_a_cent_is_not_free():
    claude = ReportClaude(0.004, [PREPARED], shared=False)
    assert "Claude: <$0.01 (" in last_line(report(claude=claude))


def test_rows_fold_into_a_summed_cost_and_an_unknown_one_adds_nothing():
    done = report_claude_from_rows(
        [(20_000, 4_000, PREPARED), (0, 0, PREPARED), (10_000, 0, None)], shared=False
    )
    assert done is not None and done.cost_usd == pytest.approx(0.04 + 0.01)
    assert list(done.prepared_at) == [PREPARED, PREPARED]
    unknown = report_claude_from_rows([(0, 0, PREPARED)], shared=False)
    assert unknown is not None and unknown.cost_usd is None
    assert report_claude_from_rows([], shared=True) is None


# --- the whole report ---


def test_the_target_report_renders_as_the_owner_approved_it():
    text = report(
        run_date=date(2026, 10, 4),
        games=[BL4, PAL, GameCfg(key="diablo4", name="Diablo IV")],
        counts={"borderlands4": 4, "palworld": 2, "diablo4": 2},
        channels={"borderlands4": 11, "palworld": 12, "diablo4": 13},
        posted_by_game={"borderlands4": 1, "palworld": 2, "diablo4": 3},
        story_games={"borderlands4", "palworld", "diablo4"},
        sources=ReportSources(28, 1, [("r/Palworld", "403 Blocked")]),
        claude=ReportClaude(0.04, [PREPARED], shared=False),
    )
    out = lines(text)
    assert out[0] == "✅ **Digest posted** · Sun Oct 4 (scheduled)"
    assert out[1].startswith("8 stories: Borderlands 4 4 [jump](<https://discord.com/channels/")
    assert out[2] == "Sources: 26 of 28 ok, 1 not checked yet (r/Palworld: 403 Blocked)"
    assert out[3] == "Claude: ~$0.04 (summary prepared at 08:30) · took 3s"


# --- length ---

LONG_NOTES = ["n" * 200]


def flood(skipped_count: int, **overrides) -> str:
    games = [GameCfg(key=f"g{n}", name=f"Some Game Number {n}") for n in range(6)]
    args = dict(
        games=games,
        counts={g.key: n for n, g in enumerate(games)},
        channels={g.key: 100 + n for n, g in enumerate(games)},
        posted_by_game={g.key: 900 + n for n, g in enumerate(games)},
        skipped={f"k{n}" + "x" * 50: "r" * 80 for n in range(skipped_count)},
        status="partial",
        notes=LONG_NOTES,
        story_games={g.key for g in games},
        sources=ReportSources(60, 0, [("S" * 200, "e" * 600), ("b", "c")]),
        claude=ReportClaude(0.04, [PREPARED], shared=True),
    )
    args.update(overrides)
    return report(**args)


def stage(text: str) -> int:
    """How far shedding got: 0 nothing shed, then notes, detail, reasons, links, flat cut."""
    if text.endswith("\u2026"):
        return 5
    if "[jump]" not in text:
        return 4
    skipped = [ln for ln in lines(text) if ln.startswith("Skipped:")]
    if skipped and "(" not in skipped[0]:
        return 3
    sources = [ln for ln in lines(text) if ln.startswith("Sources:")]
    if sources and "(" not in sources[0]:
        return 2
    if "Notes:" not in text:
        return 1
    return 0


def test_shedding_goes_notes_then_source_detail_then_reasons_then_links_then_a_flat_cut():
    seen = []
    for count in range(0, 80):
        text = flood(count)
        assert discord_len(text) <= 2000
        seen.append(stage(text))
    assert seen == sorted(seen), seen  # a later stage never un-sheds an earlier one
    assert set(seen) == {0, 1, 2, 3, 4, 5}, sorted(set(seen))


def test_the_claude_and_header_lines_survive_every_stage_but_the_flat_cut():
    for count in range(0, 80):
        text = flood(count)
        if stage(text) == 5:
            continue
        assert lines(text)[0].startswith("\u26a0\ufe0f **Digest posted with gaps**")
        assert (
            lines(text)[-1] == "Claude: ~$0.04, shared (summary prepared at 08:30) \u00b7 took 3s"
        )


# --- end to end ---


def seed_summary(world: World, game: str, *, tokens=(20_000, 4_000), url=None):
    """A stored `ok` summary for `game` with one story, written at PREPARED."""
    url = url or f"https://example.com/{game}/story"
    world.add_item(game, f"{game} source item", in_window(3), url=url)
    with world.conn() as conn:
        return repo.save_game_summary(
            conn,
            game_key=game,
            run_date=date(2026, 9, 30),
            status="ok",
            window_start=DUE - timedelta(hours=24, minutes=30),
            window_end=DUE - timedelta(minutes=30),
            coverage_notes=[],
            note=None,
            usage=Usage(*tokens),
            stories=[StoryToSave(game, "A headline", "A summary.", "official", [url], None)],
            token="t",  # noqa: S106 (a summary claim token, not a credential)
            now=lambda: PREPARED,
            items_after=None,
            items_upto=None,
        )


def health(world: World, name: str, *errors: str | None):
    with world.conn() as conn:
        for error in errors:
            repo.record_source_result(conn, name, DUE, error)


async def the_report(world: World, guild_id=G1) -> str:
    # The fake summary lookup is for tests that stub summaries; these use the stored rows.
    await run_guild_digest(world.deps(summary_for=None), guild_id, kind=RunKind.SCHEDULED)
    [(_, _, text)] = [r for r in world.reports if r[0] == guild_id]
    return text


async def test_a_free_server_report_has_items_sources_and_took(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    # Palworld shares PC Gamer with Borderlands 4, so the two games have five distinct
    # sources between them, not six.
    names = {n for k in ("borderlands4", "palworld") for n in world.cfg.game_source_names(k)}
    assert len(names) == 5
    for name in names:
        health(world, name, None)
    text = await the_report(world)
    out = lines(text)
    assert re.fullmatch(r"1 item: Borderlands 4 1 \[jump\]\(<\S+>\) \u00b7 Palworld 0", out[1])
    assert out[2] == "Sources: 5 of 5 ok"
    assert out[-1] == "Took 0s" and "Claude:" not in text


async def test_shared_sources_are_counted_once_across_games(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    cfg = world.cfg
    per_game = [cfg.game_source_names(k) for k in ("borderlands4", "palworld")]
    distinct = len(set(per_game[0]) | set(per_game[1]))
    assert distinct < len(per_game[0]) + len(per_game[1])  # PC Gamer feeds both
    text = await the_report(world)
    assert f"Sources: {distinct} sources not checked yet" in text


async def test_a_failing_source_is_named_and_a_never_checked_one_is_not_a_failure(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    health(world, "Palworld Steam", "Client error '429' for url 'https://store.example/x?k=1'")
    health(world, "PC Gamer", None)
    # The rest have no health row at all: what a rotated-out or backed-off Reddit source
    # looks like, since those passes write nothing. Not failures.
    text = await the_report(world)
    [line] = [ln for ln in lines(text) if ln.startswith("Sources:")]
    total = len({n for k in ("borderlands4", "palworld") for n in world.cfg.game_source_names(k)})
    assert line.startswith(
        f"Sources: 1 of {total} ok, {total - 2} not checked yet (Palworld Steam: "
    )
    assert "store.example/x" in line and "k=1" not in line


async def test_a_source_that_recovered_is_not_failing(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    health(world, "Palworld Steam", "boom", None)
    text = await the_report(world)
    assert "Palworld Steam:" not in text


async def test_the_sources_line_only_counts_the_servers_own_games(world):
    world.add_guild(G2)  # follows borderlands4 and rust (rust has no sources)
    world.add_item("borderlands4", "b", in_window())
    health(world, "Palworld Steam", "boom")  # not G2's game
    text = await the_report(world, G2)
    assert "Palworld Steam" not in text


async def test_a_comped_report_has_stories_cost_prepare_time_and_took(world):
    world.add_guild(G1, tier="comped")
    seed_summary(world, "borderlands4")
    seed_summary(world, "palworld", tokens=(0, 0))  # from before tokens were recorded
    text = await the_report(world)
    out = lines(text)
    assert re.match(r"2 stories: Borderlands 4 1 \[jump\]", out[1])
    # $0.04 from the one priced row; the unpriced one adds nothing and drops nothing.
    assert out[-1] == "Claude: ~$0.04 (summaries prepared at 08:30) · took 0s"


async def test_a_summary_with_no_token_data_omits_the_cost_but_keeps_the_rest(world):
    world.add_guild(G1, tier="comped")
    seed_summary(world, "borderlands4", tokens=(0, 0))
    text = await the_report(world)
    assert lines(text)[-1] == "Claude: summary prepared at 08:30 · took 0s"


async def test_a_comped_digest_with_a_fallback_game_counts_both_kinds(world):
    world.add_guild(G1, tier="comped")
    seed_summary(world, "borderlands4")
    world.add_item("palworld", "palworld headline", in_window())  # no summary: headlines
    text = await the_report(world)
    assert lines(text)[1].startswith("1 story, 1 headline: ")
    # Only the summary that was shown is priced.
    assert "Claude: ~$0.04 (summary prepared at 08:30)" in lines(text)[-1]


async def test_a_comped_digest_with_no_summaries_at_all_has_no_claude_line(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "b", in_window())
    text = await the_report(world)
    assert lines(text)[1].startswith("1 headline: ")
    assert lines(text)[-1] == "Took 0s"


async def test_a_summary_another_comped_server_could_use_is_called_shared(world):
    world.add_guild(G1, tier="comped")
    world.add_guild(G2, tier="comped")  # also follows borderlands4, first digest like G1
    seed_summary(world, "borderlands4")
    text = await the_report(world)
    assert "Claude: ~$0.04, shared (summary prepared at 08:30)" in lines(text)[-1]


async def test_an_unshared_summary_says_nothing_about_sharing(world):
    world.add_guild(G1, tier="comped", games=["palworld"])
    world.add_guild(G2, tier="comped")  # follows borderlands4 and rust, not palworld
    seed_summary(world, "palworld")
    text = await the_report(world)
    assert "shared" not in text


async def test_a_server_whose_coverage_starts_elsewhere_does_not_share_the_row(world):
    world.add_guild(G1, tier="comped")
    world.add_guild(G2, tier="comped")
    yesterday = (DUE - timedelta(hours=48), DUE - timedelta(hours=24))
    with world.conn() as conn:
        earlier = repo.claim_guild_digest(
            conn, G2, date(2026, 9, 29), force=False, window=yesterday, now=lambda: yesterday[1]
        )
        repo.save_guild_digest(
            conn, earlier.digest_id, "ok", {}, None, yesterday, now=lambda: yesterday[1]
        )
    # G2 has been told the news up to a mark; the row starts at "no mark" (None), so it
    # isn't the row G2's next digest would pick.
    seed_summary(world, "borderlands4")
    text = await the_report(world)
    assert "shared" not in text


async def test_a_free_server_sharing_a_game_does_not_make_a_summary_shared(world):
    world.add_guild(G1, tier="comped")
    world.add_guild(G2, tier="free")
    seed_summary(world, "borderlands4")
    text = await the_report(world)
    assert "shared" not in text


def test_a_game_summary_dataclass_still_builds_without_the_new_fields():
    summary = GameSummary("ok", [], [])
    assert summary.summary_id is None and summary.input_tokens == 0


async def test_the_report_still_never_pings(world):
    world.add_guild(G1, tier="comped")
    seed_summary(world, "borderlands4")
    health(world, "Palworld Steam", "@everyone [x](https://evil.example)")
    text = await the_report(world)
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "](https://evil" not in text
