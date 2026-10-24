"""Adversarial round for the digest build's industry block (plan task 7).

The builder's tests pin the cases in the brief. These go after the seams: a free
preview, run-now or timeout reaching the model (every lookup is a spy, and a few
tests wire the real lookups to a counting fake model); the model's words reaching a
free message (sentinel strings in every field a summary has); the coverage mark for
the industry entry after each kind of pick; picks that are short, long, purged or
duplicated; two days in a row; a free and a comped server sharing (or not sharing) a
pick; and what a server that couldn't post is told it covered.

The rig is the builder's `IndustryWorld` with a few additions here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import aiohttp
import pytest
from test_guild_digest import ADMIN, DUE, G1, G2, NOW, V3, FakeChannel
from test_guild_digest_industry import (
    BL4,
    CHANNELS,
    FALLBACK,
    NOW_RUN,
    PAL,
    SCHED,
    IndustryWorld,
    draft,
    titles,
)

from newsbot.config import load_config
from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline import guild_digest
from newsbot.pipeline.guild_digest import (
    GameSummary,
    preview_guild_digest,
    run_guild_digest,
)
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summaries import (
    SummaryDeps,
    dry_run_lookup,
    prepare_summaries,
    retry_lookup,
    summary_lookup,
)
from newsbot.pipeline.summarize import LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.models import StoryToSave as Save
from newsbot.store.models import Usage

SENTINEL = "ZQXSENTINELZQX"
G2_OFFSET = 100
DAY = timedelta(days=1)


class PickLLM:
    """A counting fake model: one story per item shown (at most five), the last item first.

    Each story is headlined with the item's own title, so a test can tell which items
    a comped server's ranked post is about.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        self.calls.append(user)
        items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        stories = [
            StoryOut(
                headline=item["title"],
                summary="What happened.",
                label="reported",
                item_urls=[item["url"]],
                relevant=True,
            )
            for item in reversed(items)
        ][:5]
        return LLMResult(StoriesOut(stories=stories), 10, 5)


class AdvWorld(IndustryWorld):
    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        for channel_id in CHANNELS.values():
            self.channels.setdefault(channel_id + G2_OFFSET, FakeChannel())
        self.llm = PickLLM()

    # --- guilds ---

    def follow(self, guild_id, tier="free", games=(BL4, PAL, INDUSTRY_KEY)):
        offset = G2_OFFSET if guild_id == G2 else 0
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
            repo.set_guild_games(conn, guild_id, [(k, CHANNELS[k] + offset) for k in games])

    # --- the real lookups, wired to the counting model ---

    def summary_deps(self) -> SummaryDeps:
        async def alert(text: str) -> None:
            return None

        async def sleep(seconds: float) -> None:
            await asyncio.sleep(0)

        return SummaryDeps(
            cfg=self.cfg,
            db_path=self.db_path,
            llm=self.llm,
            now=lambda: self.clock,
            alert=alert,
            sleep=sleep,
        )

    def real_deps(self, **overrides):
        sd = self.summary_deps()
        args = dict(
            summary_for=summary_lookup(sd),
            preview_summary_for=dry_run_lookup(sd),
            retry_summary=retry_lookup(sd),
        )
        args.update(overrides)
        return self.deps(**args)

    async def prepare(self, at):
        self.clock = at
        await prepare_summaries(self.summary_deps())

    # --- data ---

    def store_pick(  # type: ignore[override]
        self, urls, *, status="ok", upto=None, note=None, coverage_notes=(), headline="Model words"
    ):
        with self.conn() as conn:
            newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0]
            repo.save_game_summary(
                conn,
                game_key=INDUSTRY_KEY,
                run_date=DUE.date(),
                status=status,
                window_start=DUE - timedelta(hours=24),
                window_end=DUE - timedelta(minutes=30),
                coverage_notes=list(coverage_notes),
                note=note,
                usage=Usage(10, 5),
                stories=[
                    Save(
                        INDUSTRY_KEY, f"{headline} {n}", f"{headline} text.", "reported", [u], None
                    )
                    for n, u in enumerate(urls)
                ],
                token="t",  # noqa: S106
                now=lambda: DUE,
                items_after=None,
                items_upto=newest if upto is None else upto,
            )

    def newest_id(self) -> int:
        with self.conn() as conn:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0]

    def marks(self, guild_id=G1) -> dict:
        """The per-game coverage marks written on the guild's digest row."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT game_items_upto FROM digests WHERE guild_id = ?", (guild_id,)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def dump(self) -> list[str]:
        with self.conn() as conn:
            return list(conn.iterdump())

    def every_text(self) -> str:
        """Everything any channel was shown, embeds included, as one string."""
        parts = []
        for channel in self.channels.values():
            parts += [json.dumps(e.to_dict()) for e in channel.embeds]
            parts += [str(c) for c in channel.contents]
        return "\n".join(parts)

    def industry_titles(self, guild_id=G1) -> list[str]:
        offset = G2_OFFSET if guild_id == G2 else 0
        return titles(self.lines(CHANNELS[INDUSTRY_KEY] + offset))


@pytest.fixture
def world(tmp_path):
    return AdvWorld(tmp_path)


def story_urls(world, n, prefix="Story"):
    return [world.news(f"{prefix} {i}", hours_before_due=1 + i / 10) for i in range(n)]


# --- a free server never starts a model call unless it is a scheduled or catch-up digest ---


@pytest.mark.parametrize("how", ["preview", "run_now", "forced_run_now"])
async def test_a_free_preview_or_run_now_with_the_real_lookups_never_reaches_the_model(world, how):
    story_urls(world, 3)
    world.follow(G1)
    deps = world.real_deps()
    if how == "preview":
        await preview_guild_digest(deps, G1)
    else:
        await run_guild_digest(deps, G1, kind=NOW_RUN, force=how == "forced_run_now")
    assert world.llm.calls == []
    with world.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM game_summaries").fetchone()[0] == 0


async def test_the_same_wiring_does_reach_the_model_for_a_comped_preview_and_a_free_schedule(
    world,
):
    # The control for the test above: the spy would have seen a call if there had been one.
    story_urls(world, 3)
    world.follow(G1, "comped")
    await preview_guild_digest(world.real_deps(), G1)
    assert len(world.llm.calls) == 1

    world.llm.calls.clear()
    world.follow(G2, "free")
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    assert len(world.llm.calls) == 1


async def test_a_free_catch_up_digest_may_use_the_injected_lookup(world):
    world.news("Studio closes")
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("w", world.news("Other"))], [], items_upto=2)
    await run_guild_digest(world.deps(), G1, kind=RunKind.CATCH_UP)
    assert world.calls == [("summary_for", INDUSTRY_KEY)]


@pytest.mark.parametrize("how", ["raises", "hangs"])
@pytest.mark.parametrize("kind", [NOW_RUN, "preview"])
async def test_a_free_fresh_build_whose_stored_lookup_breaks_still_never_calls_a_model_lookup(
    world, monkeypatch, how, kind
):
    world.news("Studio closes")
    world.follow(G1)

    async def broken(db_path, game_key, due_at, after, fresh_upto=None):
        if how == "raises":
            raise RuntimeError("db went away")
        await asyncio.sleep(60)

    monkeypatch.setattr(guild_digest, "_stored_summary", broken)
    deps = world.deps(summary_timeout_s=0.05, summary_min_s=0.01)
    if kind == "preview":
        preview = await preview_guild_digest(deps, G1)
        [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
        assert titles(str(message.embed.description).split("\n")) == ["Studio closes"]
    else:
        outcome = await run_guild_digest(deps, G1, kind=kind)
        assert outcome.status == "ok" and world.industry_titles() == ["Studio closes"]
    assert world.calls == []


async def test_a_free_scheduled_lookup_that_raises_is_not_followed_by_a_retry_or_preview_lookup(
    world,
):
    world.news("Studio closes")
    world.follow(G1)

    async def boom(game_key, due_at, after=None):
        world.calls.append(("boom", game_key))
        raise RuntimeError("lookup broke")

    await run_guild_digest(world.deps(summary_for=boom), G1, kind=SCHED)
    assert world.calls == [("boom", INDUSTRY_KEY)]


async def test_a_free_scheduled_lookup_gets_the_min_deadline_when_the_budget_is_spent(world):
    # `max(min(timeout, budget), min_s)`: a spent budget still gives the floor, as for a game.
    url = world.news("Studio closes")
    world.follow(G1)

    async def slow(game_key, due_at, after=None):
        await asyncio.sleep(0.15)
        return GameSummary("ok", [draft("Model words", url)], [], items_upto=1)

    deps = world.deps(
        summary_for=slow, summary_timeout_s=0.05, summary_budget_s=0.001, summary_min_s=1.0
    )
    await run_guild_digest(deps, G1, kind=SCHED)
    assert world.industry_titles() == ["Studio closes"]


# --- the model's words never reach a free message ---


def sentinel_pick(world):
    url = world.news("Studio closes")
    return GameSummary(
        "ok",
        [
            type(draft("h", url))(
                f"{SENTINEL} headline", f"{SENTINEL} summary", "reported", [url], 12345
            )
        ],
        [f"{SENTINEL} coverage"],
        f"{SENTINEL} note",
        items_upto=1,
    )


@pytest.mark.parametrize("how", ["scheduled", "preview", "run_now", "catch_up"])
async def test_no_ai_text_reaches_a_free_message_report_or_notice(world, how):
    pick = sentinel_pick(world)
    world.follow(G1)
    world.pick = pick
    deps = world.deps()
    if how == "preview":
        preview = await preview_guild_digest(deps, G1)
        text = "\n".join(json.dumps(m.embed.to_dict()) for m in preview.rendered.messages)
        text += "\n".join(preview.notes)
    else:
        kind = {"scheduled": SCHED, "run_now": NOW_RUN, "catch_up": RunKind.CATCH_UP}[how]
        await run_guild_digest(deps, G1, kind=kind)
        text = world.every_text() + "\n".join(t for _, t in world.notices)
    assert SENTINEL not in text


async def test_no_ai_text_reaches_a_free_message_from_a_stored_pick_or_a_fallback_note(world):
    url = world.news("Studio closes")
    world.follow(G1)
    world.store_pick(
        [url], headline=SENTINEL, note=f"{SENTINEL} note", coverage_notes=[f"{SENTINEL} c"]
    )
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert SENTINEL not in world.every_text()
    assert world.industry_titles() == ["Studio closes"]


async def test_a_stored_fallback_with_a_note_shows_five_headlines_and_not_its_note(world):
    story_urls(world, 7)
    world.follow(G1)
    world.store_pick([], status="fallback", note=f"{SENTINEL} note")
    preview = await preview_guild_digest(world.deps(), G1)
    [message] = [m for m in preview.rendered.messages if m.topic_key == INDUSTRY_KEY]
    lines = str(message.embed.description).split("\n")
    assert len(lines) == 5 and SENTINEL not in json.dumps(message.embed.to_dict())
    assert preview.notes == []


async def test_a_free_pick_with_markup_in_a_picked_title_is_escaped_and_pings_nobody(world):
    url = world.news("@everyone **loud** [click](https://evil.example/x)")
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("Model words", url)], [], items_upto=1)
    await run_guild_digest(world.deps(), G1, kind=SCHED)  # FakeChannel asserts no mentions
    [line] = world.lines()
    label = line.split("](")[0]  # the link text; the target is a URL and may hold anything
    assert "@everyone" not in label.replace("@\u200beveryone", "")
    assert "\\*\\*loud\\*\\*" in label


# --- the stored coverage mark for industry ---


async def test_an_ok_pick_marks_industry_at_its_items_upto_not_the_digests_range_end(world):
    urls = story_urls(world, 5)
    world.follow(G1)
    world.store_pick(urls[:2], upto=3)
    assert world.newest_id() == 5
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    marks = world.marks()
    assert marks[INDUSTRY_KEY] == 3
    # The games keep the digest's own end, as they always did.
    assert marks[BL4] == marks[PAL] == 5


async def test_an_ok_pick_with_zero_stories_marks_industry_at_its_items_upto_and_posts_nothing(
    world,
):
    story_urls(world, 5)
    world.follow(G1)
    world.store_pick([], upto=3)
    outcome = await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert world.channels[13].embeds == [] and INDUSTRY_KEY not in outcome.posted_by_game
    assert world.marks()[INDUSTRY_KEY] == 3
    assert outcome.status == "ok"


async def test_an_ok_pick_whose_first_items_are_all_gone_still_marks_its_items_upto(world):
    story_urls(world, 5)
    world.follow(G1)
    world.store_pick(["https://example.com/pruned-1", "https://example.com/pruned-2"], upto=3)
    outcome = await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert world.channels[13].embeds == [] and outcome.status == "ok"
    assert world.marks()[INDUSTRY_KEY] == 3


async def test_an_ok_pick_with_no_items_upto_marks_industry_at_the_range_end(world):
    urls = story_urls(world, 4)
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("w", urls[0])], [], items_upto=None)
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.marks()[INDUSTRY_KEY] == world.newest_id() == 4


@pytest.mark.parametrize("how", ["fallback", "none", "raises", "hangs"])
async def test_a_pick_that_isnt_ok_marks_industry_at_the_digests_range_end(world, how):
    story_urls(world, 5)
    world.follow(G1)

    async def raising(game_key, due_at, after=None):
        raise RuntimeError("lookup broke")

    async def hanging(game_key, due_at, after=None):
        await asyncio.sleep(60)

    overrides = {}
    if how == "fallback":
        # Its own items_upto (1) must not be read: only an ok pick sets the mark.
        world.pick = GameSummary("fallback", [], [], "Custom note.", items_upto=1)
    elif how == "none":
        world.pick = None
    elif how == "raises":
        overrides["summary_for"] = raising
    else:
        overrides.update(summary_for=hanging, summary_timeout_s=0.05, summary_min_s=0.01)
    outcome = await run_guild_digest(world.deps(**overrides), G1, kind=SCHED)
    assert outcome.status == "ok"
    marks = world.marks()
    assert marks[INDUSTRY_KEY] == world.newest_id() == 5
    assert marks[INDUSTRY_KEY] == marks[BL4]


async def test_a_free_preview_writes_nothing_at_all(world):
    urls = story_urls(world, 4)
    world.follow(G1)
    world.store_pick(urls[:2])
    before = world.dump()
    await preview_guild_digest(world.deps(), G1)
    await preview_guild_digest(world.real_deps(), G1)
    assert world.dump() == before
    with world.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0


# --- picks of every size, purged items, odd stories ---


@pytest.mark.parametrize(("stories", "shown"), [(1, 1), (4, 4), (5, 5), (6, 5), (12, 5)])
async def test_a_pick_of_n_stories_shows_at_most_five_in_pick_order_and_counts_what_showed(
    world, stories, shown
):
    urls = story_urls(world, stories)
    world.follow(G1)
    world.pick = GameSummary("ok", [draft(f"S{n}", u) for n, u in enumerate(reversed(urls))], [])
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    expected = [f"Story {n}" for n in reversed(range(stories))][:shown]
    assert world.industry_titles() == expected
    [(_, _, report)] = world.reports
    assert f"Gaming industry {shown} " in report


async def test_a_story_whose_first_item_is_purged_is_skipped_even_if_a_later_url_survives(world):
    first, second = world.news("Purged soon"), world.news("Survivor")
    world.follow(G1)
    with world.conn() as conn:
        conn.execute("DELETE FROM items WHERE url = ?", (first,))
        conn.commit()
    world.pick = GameSummary(
        "ok", [draft("Two urls", first, second), draft("One url", second)], [], items_upto=2
    )
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.industry_titles() == ["Survivor"]


async def test_a_real_purged_first_item_leaves_the_other_stories_in_order(world):
    a, b, c = world.news("Aaa"), world.news("Bbb"), world.news("Ccc")
    world.follow(G1)
    world.store_pick([a, b, c])
    with world.conn() as conn:
        conn.execute("DELETE FROM items WHERE url = ?", (b,))
        conn.commit()
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert world.industry_titles() == ["Aaa", "Ccc"]


async def test_all_first_items_purged_posts_nothing_never_the_newest_five_and_counts_zero(world):
    story_urls(world, 7)
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("Gone", "https://example.com/gone")], [], items_upto=3)
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.channels[13].embeds == [] and outcome.status == "ok"
    [(_, _, report)] = world.reports
    assert "Gaming industry 0" in report


async def test_a_story_with_no_item_urls_is_skipped_and_does_not_shift_the_others(world):
    a, b = world.news("Aaa"), world.news("Bbb")
    world.follow(G1)
    world.pick = GameSummary(
        "ok", [draft("none"), draft("A", a), draft("none again"), draft("B", b)], [], items_upto=2
    )
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.industry_titles() == ["Aaa", "Bbb"]
    [(_, _, report)] = world.reports
    assert "Gaming industry 2 " in report


async def test_two_stories_sharing_a_first_url_list_the_headline_once(world):
    a, b = world.news("Aaa"), world.news("Bbb")
    world.follow(G1)
    world.pick = GameSummary(
        "ok", [draft("One", a), draft("Same item again", a, b), draft("Two", b)], [], items_upto=2
    )
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.industry_titles() == ["Aaa", "Bbb"]


async def test_a_duplicate_first_url_does_not_eat_one_of_the_five_slots(world):
    urls = story_urls(world, 6)
    world.follow(G1)
    stories = [draft("S0", urls[0]), draft("S0 again", urls[0])]
    stories += [draft(f"S{n}", u) for n, u in enumerate(urls[1:], start=1)]
    world.pick = GameSummary("ok", stories, [], items_upto=6)
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert world.industry_titles() == [f"Story {n}" for n in range(5)]


async def test_the_report_counts_a_duplicated_first_url_once(world):
    a = world.news("Aaa")
    world.follow(G1)
    world.pick = GameSummary("ok", [draft("One", a), draft("Again", a)], [], items_upto=1)
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    [(_, _, report)] = world.reports
    assert "Gaming industry 1 " in report


# --- the shape of the digest ---


@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_the_industry_post_lands_last_even_when_it_was_followed_first(world, tier):
    world.news("Bl4 patch", game=BL4)
    world.news("Pal patch", game=PAL)
    url = world.news("Studio closes")
    world.follow(G1, tier, games=(INDUSTRY_KEY, PAL, BL4))
    world.pick = GameSummary("ok", [draft("Studio closes", url)], [], items_upto=3)
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    by_post_order = sorted(outcome.posted_by_game, key=outcome.posted_by_game.get)
    assert by_post_order == [BL4, PAL, INDUSTRY_KEY]


def pick_ok(world):
    url = world.news("Studio closes")
    return GameSummary("ok", [draft("Model words", url)], [], items_upto=3)


def pick_fallback(world):
    story_urls(world, 7, prefix="Fill")
    return GameSummary("fallback", [], [], "Custom note.")


def pick_none(world):
    return None


def pick_gone(world):
    return GameSummary("ok", [draft("Gone", "https://example.com/gone")], [], items_upto=3)


def pick_with_notes(world):
    url = world.news("Studio closes")
    return GameSummary("ok", [draft("Model words", url)], [f"{SENTINEL} cov"], items_upto=3)


@pytest.mark.parametrize(
    "make_pick", [pick_ok, pick_fallback, pick_none, pick_gone, pick_with_notes]
)
@pytest.mark.parametrize("tier", ["free"])
async def test_a_free_servers_game_posts_and_marks_are_identical_with_and_without_industry(
    tmp_path, make_pick, tier
):
    async def run(name, games):
        (tmp_path / name).mkdir()
        w = AdvWorld(tmp_path / name)
        w.news("Bl4 patch", game=BL4)
        w.news("Pal patch", game=PAL, trust="official")
        w.news("Pal second", game=PAL)
        w.pick = make_pick(w)
        w.follow(G1, tier, games=games)
        outcome = await run_guild_digest(w.deps(), G1, kind=SCHED)
        embeds = {
            m.topic_key: m.embed.to_dict()
            for m in outcome.rendered.messages
            if m.topic_key != INDUSTRY_KEY
        }
        marks = {k: v for k, v in w.marks().items() if k != INDUSTRY_KEY}
        return outcome.status, embeds, marks

    with_industry = await run("a", (BL4, PAL, INDUSTRY_KEY))
    without = await run("b", (BL4, PAL))
    assert with_industry == without


async def test_a_free_servers_coverage_notes_from_the_pick_never_reach_a_footer_or_make_it_partial(
    world,
):
    world.news("Bl4 patch", game=BL4)
    world.pick = pick_with_notes(world)
    world.follow(G1)
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert outcome.status == "ok"
    assert all(e.footer.text is None for ch in world.channels.values() for e in ch.embeds)


async def test_an_industry_key_followed_in_a_catalog_without_it_is_skipped_not_a_crash(world):
    world.cfg = load_config(V3)
    world.news("Bl4 patch", game=BL4)
    world.follow(G1, games=(BL4, INDUSTRY_KEY))
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert outcome.status == "partial" and list(outcome.posted_by_game) == [BL4]
    assert world.calls == []


async def test_a_free_server_that_does_not_follow_industry_never_asks_for_the_pick(world):
    world.news("Bl4 patch", game=BL4)
    world.news("Studio closes")
    world.follow(G1, games=(BL4, PAL))
    world.pick = GameSummary("ok", [draft("w", "https://example.com/x")], [])
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    await preview_guild_digest(world.deps(), G1)
    assert world.calls == []


# --- comped servers ---


async def test_a_comped_fallback_with_its_own_note_shows_that_note_and_is_partial(world):
    story_urls(world, 7)
    world.follow(G1, "comped")
    world.pick = GameSummary("fallback", [], [], "Custom note.")
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    lines = world.lines()
    assert lines[0] == "Custom note." and len(lines) == 6
    assert outcome.status == "partial"


async def test_a_comped_fallback_with_no_industry_items_posts_nothing_and_is_not_partial(world):
    world.follow(G1, "comped", games=(INDUSTRY_KEY,))
    world.pick = GameSummary("fallback", [], [], None)
    outcome = await run_guild_digest(world.deps(), G1, kind=SCHED)
    assert INDUSTRY_KEY not in outcome.posted_by_game
    assert outcome.status == "ok"


async def test_a_comped_lookup_that_raises_falls_back_to_five_headlines_with_the_note(world):
    story_urls(world, 7)
    world.follow(G1, "comped")

    async def boom(game_key, due_at, after=None, **kw):
        raise RuntimeError("lookup broke")

    outcome = await run_guild_digest(world.deps(summary_for=boom), G1, kind=SCHED)
    lines = world.lines()
    assert lines[0] == FALLBACK and len(lines) == 6 and outcome.status == "partial"


async def test_a_comped_ok_pick_of_seven_stories_shows_five_ranked_and_marks_items_upto(world):
    urls = story_urls(world, 7)
    world.follow(G1, "comped")
    world.pick = GameSummary(
        "ok", [draft(f"Rank {n}", u) for n, u in enumerate(urls)], [], items_upto=4
    )
    await run_guild_digest(world.deps(), G1, kind=SCHED)
    text = "\n".join(world.lines())
    assert "Rank 4" in text and "Rank 5" not in text
    assert world.marks()[INDUSTRY_KEY] == 4
    [(_, _, report)] = world.reports
    assert "Gaming industry 7 " in report or "Gaming industry 5 " in report


# --- the run report ---


async def test_a_free_report_has_no_claude_line_even_for_a_pick_a_real_row_paid_for(world):
    story_urls(world, 3)
    world.follow(G1)
    world.follow(G2, "comped")
    await world.prepare(DUE - timedelta(minutes=20))
    world.clock = NOW
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    free_report = next(t for g, _, t in world.reports if g == G1)
    comped_report = next(t for g, _, t in world.reports if g == G2)
    assert "Claude" not in free_report and "Gaming industry 3 " in free_report
    assert "Claude" in comped_report


# --- two days, and servers of both tiers ---


async def test_two_days_in_a_row_show_each_industry_item_once_and_lose_none(world):
    world.news("Day one early")
    world.news("Day one also")
    world.follow(G1)
    world.follow(G2, "comped")
    await world.prepare(DUE - timedelta(minutes=20))
    # Arrives after the prepare job read the store and before the digest is due.
    world.add_item(INDUSTRY_KEY, "Late arrival", DUE - timedelta(minutes=10))
    world.clock = NOW
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    first_free, first_comped = world.industry_titles(G1), world.industry_titles(G2)
    assert sorted(first_free) == ["Day one also", "Day one early"]
    assert "Late arrival" not in first_free

    world.add_item(INDUSTRY_KEY, "Day two news", DUE + DAY - timedelta(hours=3))
    await world.prepare(DUE + DAY - timedelta(minutes=20))
    world.clock = NOW + DAY
    before = {
        g: len(world.channels[CHANNELS[INDUSTRY_KEY] + o].embeds)
        for g, o in ((G1, 0), (G2, G2_OFFSET))
    }
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    for guild, offset in ((G1, 0), (G2, G2_OFFSET)):
        embeds = world.channels[CHANNELS[INDUSTRY_KEY] + offset].embeds
        assert len(embeds) == before[guild] + 1
        second = str(embeds[-1].description)
        assert "Late arrival" in second and "Day two news" in second
        assert "Day one early" not in second and "Day one also" not in second
    assert first_comped  # the comped server posted on day one too


async def test_a_free_and_a_comped_server_on_one_schedule_share_one_industry_call(world):
    story_urls(world, 3)
    world.follow(G1)
    world.follow(G2, "comped")
    await world.prepare(DUE - timedelta(minutes=20))
    world.clock = NOW
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    assert len(world.llm.calls) == 1
    assert world.marks(G1)[INDUSTRY_KEY] == world.marks(G2)[INDUSTRY_KEY]

    # Day two: both start from the same mark, so they still share one call.
    world.add_item(INDUSTRY_KEY, "Day two news", DUE + DAY - timedelta(hours=3))
    await world.prepare(DUE + DAY - timedelta(minutes=20))
    world.clock = NOW + DAY
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    assert len(world.llm.calls) == 2
    assert world.industry_titles(G1) == ["Day two news"]


async def test_when_a_free_server_fell_back_and_a_comped_one_did_not_nothing_posts_twice_or_is_lost(
    world,
):
    # The parked Task 5 question. Day one: the comped server gets the prepared pick (mark 2),
    # the free one is handed no pick, so it shows the newest headlines and marks its range
    # end (3, which includes an item that arrived after the prepare job). Their marks differ
    # on day two, so they no longer share a pick; the point is that neither repeats a story
    # and the comped server doesn't lose the late item.
    world.news("Day one early")
    world.news("Day one also")
    world.follow(G1)
    world.follow(G2, "comped")
    await world.prepare(DUE - timedelta(minutes=20))
    world.add_item(INDUSTRY_KEY, "Late arrival", DUE - timedelta(minutes=10))
    world.clock = NOW

    async def no_pick(game_key, due_at, after=None, **kw):
        return None

    await run_guild_digest(world.deps(summary_for=no_pick), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    assert world.marks(G1)[INDUSTRY_KEY] == 3 and world.marks(G2)[INDUSTRY_KEY] == 2
    day_one_free = set(world.industry_titles(G1))
    assert "Late arrival" in day_one_free

    world.add_item(INDUSTRY_KEY, "Day two news", DUE + DAY - timedelta(hours=3))
    await world.prepare(DUE + DAY - timedelta(minutes=20))
    world.clock = NOW + DAY
    await run_guild_digest(world.real_deps(), G1, kind=SCHED)
    await run_guild_digest(world.real_deps(), G2, kind=SCHED)
    free_two = str(world.channels[13].embeds[-1].description)
    comped_two = str(world.channels[13 + G2_OFFSET].embeds[-1].description)
    assert "Day two news" in free_two and "Day two news" in comped_two
    for title in day_one_free:
        assert title not in free_two
    assert "Day one" not in comped_two and "Late arrival" in comped_two


# --- a server that couldn't post ---


async def test_an_industry_channel_that_refuses_the_post_does_not_advance_its_mark(world):
    world.news("Bl4 patch", game=BL4)
    world.news("Pal patch", game=PAL)
    urls = story_urls(world, 3)
    world.follow(G1)
    world.store_pick(urls)
    del world.channels[13]
    outcome = await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert outcome.status == "partial" and set(outcome.skipped_games) == {INDUSTRY_KEY}
    marks = world.marks()
    assert marks[INDUSTRY_KEY] is None and marks[BL4] is not None


async def test_an_industry_post_that_fails_after_the_games_posted_does_not_advance_its_mark(world):
    world.news("Bl4 patch", game=BL4)
    world.news("Pal patch", game=PAL)
    urls = story_urls(world, 3)
    world.follow(G1)
    world.store_pick(urls)
    world.channels[13].fail = [aiohttp.ClientError("reset")] * 4
    outcome = await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert outcome.status == "failed"
    with world.conn() as conn:
        coverage = repo.last_coverage(conn, G1, exclude_run_date=(NOW + DAY).date())
    assert coverage is not None
    # Not advanced: a game no digest got through starts again from id 0 (the floor trims it).
    assert coverage.mark(INDUSTRY_KEY) == 0
    assert coverage.mark(PAL) == 5


async def test_the_next_digest_picks_up_the_items_an_unposted_industry_digest_never_showed(world):
    first = world.news("Day one news")
    world.follow(G1)
    del world.channels[13]
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert first and world.channels.get(13) is None

    world.channels[13] = FakeChannel()
    world.clock = NOW + DAY
    await run_guild_digest(world.deps(summary_for=None), G1, kind=SCHED)
    assert "Day one news" in world.industry_titles()
