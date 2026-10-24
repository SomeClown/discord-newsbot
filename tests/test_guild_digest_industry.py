"""A server's digest with the "Gaming industry" entry in it (design §18, plan task 7).

A comped server gets the industry pick as ranked stories (or five headlines under the
usual note), like a game. A free server gets the pick's *source headlines* and never
the model's text, and it never pays for the pick: a preview or a run-now reads what's
stored and otherwise shows the newest five headlines. The spies below record every
lookup they're asked for and whether it could have made a model call, because "a free
preview must not start a model call" is the rule the owner cared about (2A) and a
spy is the cheapest way to hold the code to it.

The rig is `test_guild_digest.World` pointed at the catalog with the industry entry in
it, plus a channel for that entry.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_guild_digest import ADMIN, DUE, G1, NOW, World, in_window

from newsbot.config import load_config
from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline.guild_digest import (
    GameSummary,
    preview_guild_digest,
    run_guild_digest,
)
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store import repo
from newsbot.store.models import StoryToSave as Save
from newsbot.store.models import Usage

IND_CFG = Path(__file__).parent / "fixtures" / "config_v3_industry.yaml"
BL4, PAL = "borderlands4", "palworld"
CHANNELS = {BL4: 11, PAL: 12, INDUSTRY_KEY: 13}
SCHED, NOW_RUN = RunKind.SCHEDULED, RunKind.RUN_NOW
FALLBACK = "Summary unavailable; showing headlines."


class IndustryWorld(World):
    """`World` with the industry catalog, a channel for the industry entry and call spies."""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self.cfg = load_config(IND_CFG)
        for channel_id in CHANNELS.values():
            self.channels.setdefault(channel_id, type(self.channels[11])())
        self.calls: list[tuple[str, str]] = []
        # What the lookups answer for the industry entry; every game answers `None`.
        self.pick: GameSummary | None = None
        self.retry_result: GameSummary | None = None

    def deps(self, **overrides):
        def spy(name, answer):
            async def lookup(game_key, due_at, after=None, /, *, fresh_upto=None):
                self.calls.append((name, game_key))
                return answer() if game_key == INDUSTRY_KEY else None

            return lookup

        args = dict(
            summary_for=spy("summary_for", lambda: self.pick),
            preview_summary_for=spy("preview", lambda: self.pick),
            retry_summary=spy("retry", lambda: self.retry_result),
        )
        args.update(overrides)
        return super().deps(**args)

    def follow(self, guild_id, tier="free", games=(BL4, PAL, INDUSTRY_KEY)):
        with self.conn() as conn:
            repo.create_guild(
                conn,
                guild_id,
                digest_time="09:00",
                timezone="America/Los_Angeles",
                admin_channel_id=ADMIN[guild_id],
                tier=tier,
                set_up=True,
                now=lambda: NOW,
            )
            repo.set_guild_games(conn, guild_id, [(k, CHANNELS[k]) for k in games])

    def set_games(self, guild_id, games):
        with self.conn() as conn:
            repo.set_guild_games(conn, guild_id, [(k, CHANNELS[k]) for k in games])

    def news(self, title, hours_before_due=2, game=INDUSTRY_KEY, trust="press"):
        url = f"https://example.com/{game}/{title.replace(' ', '-')}"
        self.add_item(game, title, in_window(hours_before_due), trust=trust, url=url)
        return url

    def store_pick(self, urls, *, status="ok", upto=None):
        """A stored industry summary whose stories are headlined from `urls`, in order."""
        with self.conn() as conn:
            newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0]
            repo.save_game_summary(
                conn,
                game_key=INDUSTRY_KEY,
                run_date=DUE.date(),
                status=status,
                window_start=DUE - timedelta(hours=24),
                window_end=DUE - timedelta(minutes=30),
                coverage_notes=[],
                note=None,
                usage=Usage(10, 5),
                stories=[
                    Save(INDUSTRY_KEY, f"Model words {n}", "Model text.", "reported", [url], None)
                    for n, url in enumerate(urls)
                ],
                token="t",  # noqa: S106
                now=lambda: DUE,
                items_after=None,
                items_upto=newest if upto is None else upto,
            )

    def lines(self, channel_id=13):
        """The industry post's lines, or [] if nothing was posted."""
        embeds = self.channels[channel_id].embeds
        return str(embeds[-1].description).split("\n") if embeds else []


@pytest.fixture
def world(tmp_path):
    return IndustryWorld(tmp_path)


def draft(headline, *urls):
    return StoryDraft(headline, "Model text.", "reported", list(urls), None)


def titles(lines):
    return [line.split(" — ")[0].split("](")[0].lstrip("• ").lstrip("[") for line in lines]


# --- a free server, on schedule ---


async def test_a_free_scheduled_digest_shows_the_stored_pick_as_source_headlines_in_pick_order(
    world,
):
    first, second, third = world.news("Studio closes"), world.news("Deal done"), world.news("Cuts")
    world.news("Not picked")
    world.follow(G1)
    world.store_pick([third, first, second])
    outcome = await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    text = "\n".join(world.lines())
    assert titles(world.lines()) == ["Cuts", "Studio closes", "Deal done"]
    assert "Model" not in text and "Not picked" not in text
    assert outcome.status == "ok"
    with world.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 1


async def test_a_free_scheduled_digest_records_the_picks_items_upto_as_coverage(world):
    url = world.news("Studio closes")
    world.news("Later item", hours_before_due=1)
    world.follow(G1)
    world.store_pick([url], upto=1)
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    with world.conn() as conn:
        raw = conn.execute("SELECT game_items_upto FROM digests").fetchone()[0]
    assert json.loads(raw)[INDUSTRY_KEY] == 1


async def test_a_free_scheduled_digest_with_nothing_stored_asks_summary_for_once(world):
    url = world.news("Studio closes")
    world.news("Other")
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("Model words", url)], [], items_upto=2)
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.calls == [("summary_for", INDUSTRY_KEY)]
    assert titles(world.lines()) == ["Studio closes"]


async def test_a_free_scheduled_fallback_is_five_headlines_with_no_note_and_not_partial(world):
    for n in range(7):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1)
    world.pick = GameSummary("fallback", [], [], "Custom note.")
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    lines = world.lines()
    assert len(lines) == 5 and FALLBACK not in "\n".join(lines) and "Custom" not in "\n".join(lines)
    assert outcome.status == "ok"


# --- a free server, preview and run-now: no model call, ever ---


def assert_no_model_call(world):
    assert world.calls == []


async def test_a_free_preview_never_calls_any_lookup_and_shows_a_stored_pick_despite_newer_items(
    world,
):
    url = world.news("Studio closes")
    world.follow(G1)
    world.store_pick([url])
    world.news("Newer item", hours_before_due=0.5)
    preview = await preview_guild_digest(world.deps(), G1)
    [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
    assert titles(str(message.embed.description).split("\n")) == ["Studio closes"]
    assert_no_model_call(world)


async def test_a_free_preview_with_no_pick_shows_the_newest_five_headlines_and_no_note(world):
    for n in range(7):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1)
    preview = await preview_guild_digest(world.deps(), G1)
    [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
    lines = str(message.embed.description).split("\n")
    assert len(lines) == 5 and FALLBACK not in "\n".join(lines)
    assert not preview.notes
    assert_no_model_call(world)


async def test_a_free_run_now_makes_no_model_call_and_shows_the_stored_pick(world):
    url = world.news("Studio closes")
    world.follow(G1)
    world.store_pick([url])
    world.news("Newer item", hours_before_due=0.5)
    await run_guild_digest(world.deps(), G1, kind=NOW_RUN)
    assert titles(world.lines()) == ["Studio closes"]
    assert_no_model_call(world)


async def test_a_free_forced_run_now_does_not_retry_a_fallback_pick(world):
    world.news("Studio closes")
    world.follow(G1)
    world.store_pick([], status="fallback")
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=SCHED)
    world.calls.clear()
    await run_guild_digest(deps, G1, kind=NOW_RUN, force=True)
    assert_no_model_call(world)


# --- a comped server ---


async def test_a_comped_preview_uses_the_previews_lookup_for_industry(world):
    url = world.news("Studio closes")
    world.follow(G1, "comped")
    world.pick = GameSummary("ok", [draft("Studio closes", url)], [])
    preview = await preview_guild_digest(world.deps(), G1)
    assert ("preview", INDUSTRY_KEY) in world.calls
    assert ("summary_for", INDUSTRY_KEY) not in world.calls
    [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
    assert "Model text." in str(message.embed.description)


async def test_a_comped_confirmed_run_now_retries_a_fallback_pick(world):
    url = world.news("Studio closes")
    world.follow(G1, "comped")
    world.pick = GameSummary("fallback", [], [], None)
    world.retry_result = GameSummary("ok", [draft("Retried words", url)], [])
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=SCHED)
    world.calls.clear()
    await run_guild_digest(deps, G1, kind=NOW_RUN, force=True)
    assert ("retry", INDUSTRY_KEY) in world.calls
    assert "Retried words" in "\n".join(world.lines())


async def test_a_comped_fallback_shows_the_note_over_five_headlines_and_the_digest_is_partial(
    world,
):
    for n in range(7):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1, "comped")
    world.pick = GameSummary("fallback", [], [], None)
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    lines = world.lines()
    assert lines[0] == FALLBACK and len(lines) == 6
    assert outcome.status == "partial"


async def test_a_comped_ok_pick_shows_ranked_stories_in_the_models_order(world):
    first, second = world.news("Aaa closes"), world.news("Zzz sold")
    world.follow(G1, "comped")
    world.pick = GameSummary("ok", [draft("Zzz sold", second), draft("Aaa closes", first)], [])
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    text = "\n".join(world.lines())
    assert text.index("Zzz sold") < text.index("Aaa closes") and "Model text." in text
    assert outcome.status == "ok"


# --- an ok pick with nothing in it ---


@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_an_ok_pick_with_zero_stories_posts_nothing(world, tier):
    world.news("Studio closes")
    world.follow(G1, tier)
    world.pick = GameSummary("ok", [], [])
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.channels[13].embeds == []
    assert INDUSTRY_KEY not in outcome.posted_by_game


async def test_a_free_pick_whose_items_are_all_gone_posts_nothing_and_not_the_fallback(world):
    world.news("Studio closes")
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("Gone", "https://example.com/pruned")], [])
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.channels[13].embeds == []


async def test_a_free_pick_skips_a_story_whose_first_item_is_gone_and_caps_at_five(world):
    urls = [world.news(f"Story {n}") for n in range(7)]
    world.follow(G1)
    stories = [draft("Gone", "https://example.com/pruned")] + [
        draft(f"S{n}", u) for n, u in enumerate(urls)
    ]
    world.pick = GameSummary("ok", stories, [])
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert titles(world.lines()) == [f"Story {n}" for n in range(5)]


# --- the shape of the digest ---


@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_the_industry_message_comes_last_and_game_messages_match_a_digest_without_it(
    world, tier
):
    world.news("Bl4 patch", game=BL4)
    world.news("Pal patch", game=PAL)
    url = world.news("Studio closes")
    world.follow(G1, tier)
    world.pick = GameSummary("ok", [draft("Studio closes", url)], [])
    with_industry = await preview_guild_digest(world.deps(), G1)
    world.set_games(G1, [BL4, PAL])
    without = await preview_guild_digest(world.deps(), G1)
    assert [m.topic_key for m in with_industry.rendered.messages][-1] == INDUSTRY_KEY
    games_with = [m for m in with_industry.rendered.messages if m.topic_key != INDUSTRY_KEY]
    assert [m.topic_key for m in games_with] == [BL4, PAL]
    assert [(m.topic_key, m.channel_id, m.embed.to_dict()) for m in games_with] == [
        (m.topic_key, m.channel_id, m.embed.to_dict()) for m in without.rendered.messages
    ]


# --- the run report ---


async def test_a_free_run_report_counts_the_picks_headlines_and_has_no_claude_line(world):
    first, second = world.news("One"), world.news("Two")
    world.news("Three")
    world.follow(G1)
    world.store_pick([first, second])
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    [(_, _, report)] = world.reports
    assert "Gaming industry 2 " in report and "Claude" not in report


async def test_a_comped_run_report_counts_a_picks_stories(world):
    first, second = world.news("One"), world.news("Two")
    world.news("Three")
    world.follow(G1, "comped")
    world.pick = GameSummary("ok", [draft("One", first), draft("Two", second)], [])
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    [(_, _, report)] = world.reports
    assert "Gaming industry 2 " in report


@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_a_run_report_counts_at_most_five_fallback_headlines(world, tier):
    for n in range(7):
        world.news(f"Story {n}", hours_before_due=1 + n / 10)
    world.follow(G1, tier)
    world.pick = GameSummary("fallback", [], [], None)
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    [(_, _, report)] = world.reports
    assert "Gaming industry 5 " in report


# --- a lookup that breaks or hangs ---


async def test_a_free_scheduled_lookup_that_raises_or_hangs_falls_back_to_headlines(world):
    world.news("Studio closes")
    world.follow(G1)

    async def boom(game_key, due_at, after=None):
        raise RuntimeError("lookup broke")

    outcome = await run_guild_digest(world.deps(summary_for=boom), G1, kind=SCHED)
    assert outcome.status == "ok" and titles(world.lines()) == ["Studio closes"]


async def test_a_free_scheduled_lookup_that_hangs_is_cut_off_at_the_deadline(world):
    world.news("Studio closes")
    world.follow(G1)

    async def hang(game_key, due_at, after=None):
        await asyncio.sleep(60)

    deps = world.deps(summary_for=hang, summary_timeout_s=0.05, summary_min_s=0.01)
    outcome = await run_guild_digest(deps, G1, kind=SCHED)
    assert outcome.status == "ok" and titles(world.lines()) == ["Studio closes"]
