"""The per-guild digest, end to end against a real database and fake channels (plan task 6).

Publishing is the real `DiscordPublisher` talking to hand-written fake channels,
so the write-through, the resume and the skip-and-continue are exercised for
real; nothing here needs a gateway. The clock only moves when a test moves it,
and so does the pause between servers.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiohttp
import discord
import pytest

from newsbot.bot.client import DiscordPublisher
from newsbot.bot.format import discord_len
from newsbot.config import load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.guilds.schedule import due_guilds
from newsbot.pipeline.guild_digest import (
    GameSummary,
    GuildDigestDeps,
    guild_needs_confirmation,
    is_guild_busy,
    preview_guild_digest,
    run_due_guilds,
    run_guild_digest,
    todays_guild_digest,
)
from newsbot.pipeline.run import RunKind
from newsbot.pipeline.summarize import StoryDraft
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem

V3 = Path(__file__).parent / "fixtures" / "config_v3.yaml"
LA = "America/Los_Angeles"
# 09:00 PDT on 2026-09-30, and thirty seconds past it: the minute job's usual lateness.
DUE = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
NOW = DUE + timedelta(seconds=30)
DAY = date(2026, 9, 30)

# Guild 1 follows borderlands4 and palworld; guild 2 follows borderlands4 and rust.
G1, G2 = 1001, 1002
CH = {
    (G1, "borderlands4"): 11,
    (G1, "palworld"): 12,
    (G2, "borderlands4"): 21,
    (G2, "rust"): 23,
}
ADMIN = {G1: 901, G2: 902}


class _Response:
    def __init__(self, status: int) -> None:
        self.status, self.reason, self.headers, self.request_info = status, "x", {}, None


class FakeChannel:
    def __init__(self) -> None:
        self.embeds: list[discord.Embed] = []
        self.contents: list[str] = []
        self.nonces: list[str | None] = []
        self.fail: list[Exception] = []

    async def send(self, content=None, *, embed=None, allowed_mentions=None, nonce=None):
        if self.fail:
            raise self.fail.pop(0)
        assert allowed_mentions is not None and allowed_mentions.everyone is False
        assert allowed_mentions.users is False and allowed_mentions.roles is False
        if embed is None:
            self.contents.append(content)
            return type("Msg", (), {"id": World.next_message_id()})()
        self.embeds.append(embed)
        self.nonces.append(nonce)
        return type("Msg", (), {"id": World.next_message_id()})()


class FakeClient:
    def __init__(self, channels: dict[int, FakeChannel]) -> None:
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(_Response(404), "unknown channel")


class World:
    """A database, fake channels, and everything the digest told anyone."""

    _ids = 5000

    @classmethod
    def next_message_id(cls) -> int:
        cls._ids += 1
        return cls._ids

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = str(tmp_path / "t.db")
        with closing(connect(self.db_path)) as conn:
            migrate(conn)
        self.cfg = load_config(V3)
        self.clock = NOW
        self.channels = {ch: FakeChannel() for ch in [*CH.values(), *ADMIN.values()]}
        self.notices: list[tuple[int, str]] = []
        self.reports: list[tuple[int, int, str]] = []
        self.sleeps: list[float] = []
        self.publishers: list[DiscordPublisher] = []
        self.summaries: dict[str, GameSummary | None] = {}
        self.factory_boom: dict[int, Exception] = {}

    # --- deps ---

    def deps(self, **overrides) -> GuildDigestDeps:
        async def notify(guild_id: int, text: str) -> None:
            self.notices.append((guild_id, text))

        async def send_report(guild_id: int, channel_id: int, text: str) -> None:
            self.reports.append((guild_id, channel_id, text))
            await self.channels[channel_id].send(
                text, allowed_mentions=discord.AllowedMentions.none()
            )

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        async def summary_for(game_key: str, due_at: datetime, after: datetime | None = None):
            return self.summaries.get(game_key)

        def publisher_for(guild, scope, already, on_posted):
            if guild.guild_id in self.factory_boom:
                raise self.factory_boom[guild.guild_id]
            publisher = DiscordPublisher(
                FakeClient(self.channels),
                nonce_scope=scope,
                on_posted=on_posted,
                already_posted=already,
                skip_permanent=True,
            )
            self.publishers.append(publisher)
            return publisher

        args = dict(
            cfg=self.cfg,
            db_path=self.db_path,
            now=lambda: self.clock,
            publisher_for=publisher_for,
            notify_guild=notify,
            send_report=send_report,
            summary_for=summary_for,
            sleep=sleep,
        )
        args.update(overrides)
        return GuildDigestDeps(**args)

    # --- data ---

    def conn(self):
        return closing(connect(self.db_path))

    def add_guild(self, guild_id, *, tier="free", games=None, time="09:00", tz=LA, admin=True):
        with self.conn() as conn:
            repo.create_guild(
                conn,
                guild_id,
                digest_time=time,
                timezone=tz,
                admin_channel_id=ADMIN[guild_id] if admin else None,
                tier=tier,
                set_up=True,
                now=lambda: NOW,
            )
            wanted = games or [k for (g, k) in CH if g == guild_id]
            repo.set_guild_games(conn, guild_id, [(k, CH[(guild_id, k)]) for k in wanted])

    def add_item(self, game, title, collected, *, trust="press", uncertain=False, url=None):
        with self.conn() as conn:
            repo.store_items(
                conn,
                [
                    StoredItem(
                        url=url or f"https://example.com/{game}/{title.replace(' ', '-')}",
                        title=title,
                        excerpt="",
                        source_name="Feed",
                        trust=trust,
                        published_at=None,
                        topics={game: uncertain},
                    )
                ],
                now=lambda: collected,
            )

    def digest(self, guild_id, run_date=DAY):
        with self.conn() as conn:
            return repo.get_guild_digest(conn, guild_id, run_date)

    def posted_titles(self, channel_id) -> list[str]:
        return [str(e.title) for e in self.channels[channel_id].embeds]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def in_window(hours_before_due: float = 2) -> datetime:
    return DUE - timedelta(hours=hours_before_due)


# --- the free render, from shared items ---


async def test_a_free_digest_is_built_from_its_own_games_items_in_d4_order(world):
    world.add_guild(G1)
    world.add_guild(G2)
    for title, trust, unc, hrs in [
        ("community thread", "community", False, 1),
        ("press confident", "press", False, 3),
        ("press uncertain", "press", True, 2),
        ("official patch", "official", False, 4),
    ]:
        world.add_item("borderlands4", title, in_window(hrs), trust=trust, uncertain=unc)
    world.add_item("rust", "rust only", in_window())  # guild 2's game, not guild 1's
    world.add_item("borderlands4", "too old", DUE - timedelta(hours=24))  # start is exclusive
    world.add_item("borderlands4", "too new", DUE + timedelta(seconds=1))

    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)

    assert outcome.status == "ok" and outcome.run_date == DAY
    [embed] = world.channels[11].embeds
    body = str(embed.description).split("\n")
    assert [line.split(" — ")[0] for line in body] == [
        "• 🟢 OFFICIAL · official patch",
        "• press confident",
        "• press uncertain",
        "• community thread",
    ]
    assert str(embed.title) == "Borderlands 4"
    assert world.channels[12].embeds == []  # palworld: nothing that day, nothing posted
    assert all(not world.channels[c].embeds for c in (21, 23))  # the other guild's channels
    assert "rust only" not in str(embed.description)


async def test_games_post_in_catalog_order_not_follow_order(world):
    world.add_guild(G1, games=["palworld", "borderlands4"])
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert list(outcome.posted_by_game) == ["borderlands4", "palworld"]


async def test_over_the_limit_the_free_digest_sheds_lines_and_says_how_many(world):
    world.add_guild(G1)
    for n in range(120):
        world.add_item("borderlands4", f"headline {n:03d} " + "z" * 50, in_window(1 + n / 100))
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    text = str(world.channels[11].embeds[0].description)
    assert discord_len(text) <= 4096 and "more, use /news" in text
    assert text.endswith("more, use /news")


async def test_a_game_with_zero_items_posts_nothing_but_the_others_do(world):
    world.add_guild(G1)
    world.add_item("palworld", "only palworld has news", in_window())
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok" and list(outcome.posted_by_game) == ["palworld"]
    assert world.channels[11].embeds == [] and len(world.channels[12].embeds) == 1


async def test_a_day_with_no_news_at_all_saves_ok_with_nothing_posted(world):
    world.add_guild(G1)
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok" and outcome.posted_by_game == {}
    row = world.digest(G1)
    assert (row.status, row.posted_by_game, row.posted_message_ids) == ("ok", {}, [])
    assert all(not ch.embeds for ch in world.channels.values())


async def test_titles_are_escaped_and_nobody_is_pinged(world):
    world.add_guild(G1)
    world.add_item(
        "borderlands4", "@everyone free loot **now**", in_window(), url="https://example.com/loot"
    )
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    text = str(world.channels[11].embeds[0].description)
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "\\*\\*now\\*\\*" in text  # FakeChannel.send also asserts AllowedMentions.none()


# --- comped guilds ---


def story(headline, label="official", urls=("https://example.com/borderlands4/s",)):
    return StoryDraft(headline, "Summary text.", label, list(urls), None)


async def test_a_comped_guild_with_a_stored_summary_posts_the_stories(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "a headline", in_window())
    world.summaries["borderlands4"] = GameSummary("ok", [story("Vault patch")], [])
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    text = str(world.channels[11].embeds[0].description)
    assert "🟢 OFFICIAL · Vault patch" in text and "Summary text." in text
    assert "a headline" not in text
    assert outcome.status == "ok"
    # palworld had no summary and no items: nothing posted, and that isn't a failure.
    assert world.channels[12].embeds == []


async def test_a_comped_guild_without_a_summary_falls_back_to_headlines_partial(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "a headline", in_window())
    # No summary for borderlands4 (None), and nothing is stored either.
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    lines = str(world.channels[11].embeds[0].description).split("\n")
    assert lines[0] == "Summary unavailable; showing headlines."
    assert lines[1].startswith("• a headline")
    assert outcome.status == "partial"
    assert world.digest(G1).status == "partial"


async def test_a_fallback_summary_behaves_like_a_missing_one(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "a headline", in_window())
    world.summaries["borderlands4"] = GameSummary("fallback", [], [], "Custom note.")
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert str(world.channels[11].embeds[0].description).startswith("Custom note.")
    assert outcome.status == "partial"


async def test_coverage_notes_from_the_summary_go_in_the_footer_and_make_it_partial(world):
    world.add_guild(G1, tier="comped")
    world.summaries["borderlands4"] = GameSummary("ok", [story("x")], ["web search skipped"])
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert world.channels[11].embeds[0].footer.text.endswith("web search skipped")
    assert outcome.status == "partial"


async def test_a_summary_lookup_that_raises_costs_that_game_its_summary_only(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "a headline", in_window())

    async def boom(game_key, due_at, after):
        raise RuntimeError("lookup broke")

    outcome = await run_guild_digest(world.deps(summary_for=boom), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "partial" and len(world.channels[11].embeds) == 1


async def test_the_default_lookup_reads_the_stored_summary_for_that_due_instant(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "a headline", in_window(3))
    with world.conn() as conn:
        cur = conn.execute(
            "INSERT INTO game_summaries (game_key, run_date, status, window_start, window_end, "
            "coverage_notes, created_at) VALUES ('borderlands4', '2026-09-30', 'ok', ?, ?, "
            "'[\"one note\"]', 't')",
            (
                (DUE - timedelta(hours=24, minutes=30)).isoformat(),
                (DUE - timedelta(minutes=30)).isoformat(),
            ),
        )
        sid = cur.lastrowid
        item_id = conn.execute("SELECT id FROM items").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, summary_id, created_at) "
            "VALUES ('borderlands4', 'Stored story', 'Stored text.', 'reported', ?, ?)",
            (sid, DUE.isoformat()),
        )
        conn.execute(
            "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)", (cur.lastrowid, item_id)
        )
        conn.commit()
    deps = world.deps(summary_for=None)  # None means: read what's stored
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    text = str(world.channels[11].embeds[0].description)
    assert "🟡 REPORTED · Stored story" in text and "Stored text." in text
    assert world.channels[11].embeds[0].footer.text.endswith("one note")


async def test_a_comped_lookup_is_told_when_the_servers_last_digest_ended(world):
    world.add_guild(G1, tier="comped")
    world.add_item("borderlands4", "a headline", in_window(3))
    seen = []

    async def spy(game_key, due_at, after):
        seen.append((game_key, due_at, after))

    deps = world.deps(summary_for=spy)
    world.clock = DUE + timedelta(seconds=20)
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    # A first digest has nothing to repeat; the next day's is told where this one ended.
    tomorrow = DUE + timedelta(days=1)
    world.clock = tomorrow + timedelta(seconds=20)
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    assert {(due, after) for _, due, after in seen if due == DUE} == {(DUE, None)}
    assert {(due, after) for _, due, after in seen if due == tomorrow} == {(tomorrow, DUE)}


async def test_a_free_guild_never_asks_for_summaries(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "a headline", in_window())
    calls = []

    async def spy(game_key, due_at, after):
        calls.append(game_key)

    await run_guild_digest(world.deps(summary_for=spy), G1, kind=RunKind.SCHEDULED)
    assert calls == []


# --- the guard ---


async def test_a_second_run_the_same_day_is_refused(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "x", in_window())
    deps = world.deps()
    first = await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    second = await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    assert (first.status, second.status) == ("ok", "skipped")
    assert len(world.channels[11].embeds) == 1
    third = await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW)
    assert third.status == "skipped"  # run-now without the admin's yes


async def test_force_replaces_the_row_in_place_and_reposts_with_a_fresh_nonce(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "x", in_window())
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    row_id = world.digest(G1).id
    world.clock = NOW + timedelta(hours=2)

    outcome = await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True)

    assert outcome.status == "ok"
    assert len(world.channels[11].embeds) == 2
    assert world.channels[11].nonces[0] != world.channels[11].nonces[1]
    row = world.digest(G1)
    assert row.id == row_id and row.attempts == 2
    with world.conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id = ?", (G1,)).fetchone()[0]
            == 1
        )


async def test_guilds_do_not_bleed_into_each_other(world):
    world.add_guild(G1)
    world.add_guild(G2)
    world.add_item("borderlands4", "shared", in_window())
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    assert world.digest(G2) is None
    await run_guild_digest(deps, G2, kind=RunKind.SCHEDULED)
    assert len(world.channels[11].embeds) == 1 and len(world.channels[21].embeds) == 1


async def test_an_unset_up_or_game_less_guild_is_skipped_without_a_claim(world):
    with world.conn() as conn:
        repo.create_guild(conn, G1, set_up=False, now=lambda: NOW)
        repo.create_guild(conn, G2, set_up=True, now=lambda: NOW)
    deps = world.deps()
    assert (await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW)).status == "skipped"
    assert (await run_guild_digest(deps, G2, kind=RunKind.RUN_NOW)).status == "skipped"
    assert (await run_guild_digest(deps, 999, kind=RunKind.RUN_NOW)).status == "skipped"
    assert world.digest(G1) is None and world.digest(G2) is None


async def test_the_window_is_saved_and_a_scheduled_window_ends_at_the_due_instant(world):
    world.add_guild(G1)
    # A guild that already has a digest behind it (a server's very first digest ends
    # at "now" instead; see the first-digest test in the adversarial file).
    with world.conn() as conn:
        earlier = repo.claim_guild_digest(
            conn,
            G1,
            DAY - timedelta(days=1),
            force=False,
            window=(DUE - timedelta(hours=48), DUE - timedelta(hours=24)),
            now=lambda: NOW - timedelta(days=1),
        )
        repo.save_guild_digest(
            conn,
            earlier.digest_id,
            "ok",
            {},
            None,
            (DUE - timedelta(hours=48), DUE - timedelta(hours=24)),
            now=lambda: NOW - timedelta(days=1),
        )
    world.clock = DUE + timedelta(hours=3)  # the bot was down; this is catch-up
    [due] = due_guilds(_candidates(world), world.clock)
    assert due.catch_up is True
    world.add_item("borderlands4", "before the due instant", in_window())
    world.add_item("borderlands4", "during the outage", DUE + timedelta(hours=1))
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.CATCH_UP, due=due)
    row = world.digest(G1)
    assert row.window_end == DUE and row.window_start == DUE - timedelta(hours=24)
    text = str(world.channels[11].embeds[0].description)
    assert "before the due instant" in text and "during the outage" not in text
    assert outcome.status == "ok"


async def test_windows_chain_a_run_now_then_tomorrows_digest(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "early", DUE - timedelta(hours=4))
    deps = world.deps()
    world.clock = DUE - timedelta(hours=1)  # run-now at 08:00 PDT, before the digest time
    await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW)
    assert world.digest(G1).window_end == world.clock
    world.add_item("borderlands4", "later", DUE - timedelta(minutes=30))  # after the run-now
    # Tomorrow's scheduled digest starts where the run-now ended.
    tomorrow = DUE + timedelta(days=1)
    world.clock = tomorrow + timedelta(seconds=20)
    await run_due_guilds(deps)
    row = world.digest(G1, date(2026, 10, 1))
    assert row.window_start == DUE - timedelta(hours=1) and row.window_end == tomorrow
    embeds = world.channels[11].embeds
    assert len(embeds) == 2
    assert "early" in str(embeds[0].description) and "later" not in str(embeds[0].description)
    assert "later" in str(embeds[1].description) and "early" not in str(embeds[1].description)


def _candidates(world):
    with world.conn() as conn:
        return repo.due_candidates(conn)


# --- write-through, resume, failures ---


async def test_posted_games_are_written_through_and_a_crash_leaves_them_on_the_row(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    # Palworld's channel raises something nobody planned for (not a PublishError).
    world.channels[12].fail = [RuntimeError("the gateway ate it")]
    deps = world.deps()
    with pytest.raises(RuntimeError):
        await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    row = world.digest(G1)
    assert row.status == "failed" and "unhandled error" in row.error_notes
    assert list(row.posted_by_game) == ["borderlands4"]  # written as it landed
    assert row.posted_message_ids == list(row.posted_by_game.values())
    assert not is_guild_busy(deps, G1) and G1 not in deps.running


async def test_resume_after_a_crash_mid_publish_posts_only_the_missing_games(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    # What a process that died mid-publish leaves behind: a pending row, with
    # borderlands4's message already written through.
    window = (DUE - timedelta(hours=24), DUE)
    with world.conn() as conn:
        claim = repo.claim_guild_digest(
            conn, G1, DAY, force=False, window=window, now=lambda: DUE + timedelta(seconds=5)
        )
        repo.record_posted_game(
            conn, claim.digest_id, "borderlands4", 424242, now=lambda: DUE + timedelta(seconds=5)
        )

    # The new process starts once the dead one's lease (ten minutes of quiet) has gone stale.
    world.clock = DUE + timedelta(minutes=16)
    outcomes = await run_due_guilds(world.deps())

    assert [o.status for o in outcomes] == ["ok"]
    assert world.channels[11].embeds == []  # not posted again
    assert len(world.channels[12].embeds) == 1
    row = world.digest(G1)
    assert row.status == "ok" and row.attempts == 2
    assert row.posted_by_game["borderlands4"] == 424242 and "palworld" in row.posted_by_game
    assert (row.window_start, row.window_end) == window  # resumed with the same window
    assert outcomes[0].posted_by_game == row.posted_by_game


async def test_transient_send_failures_retry_with_backoff_and_post_each_game_once(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    world.channels[12].fail = [aiohttp.ClientError("reset"), aiohttp.ClientError("reset")]
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok"
    assert len(world.channels[11].embeds) == 1 and len(world.channels[12].embeds) == 1
    assert world.sleeps == [2.0, 4.0]


async def test_retries_that_never_recover_fail_the_digest_but_keep_what_posted(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    world.channels[12].fail = [aiohttp.ClientError("reset")] * 4
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "failed"
    assert world.sleeps == [2.0, 4.0, 8.0]
    row = world.digest(G1)
    assert row.status == "failed" and list(row.posted_by_game) == ["borderlands4"]
    [(guild_id, text)] = world.notices
    assert guild_id == G1
    assert "Borderlands 4" in text and "Palworld" in text and "run-now" in text
    assert world.reports == []  # a failure gets a notice, not a run report
    # It posted something, so the due check leaves it for an admin's run-now.
    world.clock = NOW + timedelta(hours=1)
    assert due_guilds(_candidates(world), world.clock) == []
    assert guild_needs_confirmation(row) is True


async def test_a_clean_failure_retries_on_a_later_tick_up_to_three_attempts(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    deps = world.deps()
    world.channels[11].fail = [aiohttp.ClientError("reset")] * 4
    first = await run_due_guilds(deps)
    assert [o.status for o in first] == ["failed"]
    assert world.digest(G1).posted_by_game == {}

    world.clock = NOW + timedelta(minutes=5)
    assert await run_due_guilds(deps) == []  # too soon to retry
    world.clock = NOW + timedelta(minutes=30)
    second = await run_due_guilds(deps)  # the channel recovers
    assert [o.status for o in second] == ["ok"] and len(world.channels[11].embeds) == 1
    assert world.digest(G1).attempts == 2


async def test_a_deleted_channel_skips_that_game_and_tells_the_guild(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    del world.channels[11]  # borderlands4's channel is gone (fetch_channel says 404)
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "partial"
    assert list(outcome.posted_by_game) == ["palworld"]
    assert set(outcome.skipped_games) == {"borderlands4"}
    row = world.digest(G1)
    assert row.status == "partial" and "borderlands4: skipped" in row.error_notes
    [(guild_id, text)] = world.notices
    assert guild_id == G1 and "Borderlands 4" in text and "/newsbot follow" in text
    assert len(world.channels[12].embeds) == 1


async def test_forbidden_on_one_game_is_the_same_skip(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    world.channels[11].fail = [discord.Forbidden(_Response(403), "missing access")]
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "partial" and list(outcome.posted_by_game) == ["palworld"]


async def test_a_game_no_longer_in_the_catalog_is_skipped_with_a_note(world):
    world.add_guild(G1)
    with world.conn() as conn:
        repo.set_guild_games(conn, G1, [("borderlands4", 11), ("retired_game", 12)])
    world.add_item("borderlands4", "b", in_window())
    outcome = await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "partial" and list(outcome.posted_by_game) == ["borderlands4"]
    assert "retired_game: not in the catalog" in world.digest(G1).error_notes


async def test_a_notifier_that_raises_does_not_break_the_digest(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    del world.channels[11]

    async def broken(guild_id, text):
        raise RuntimeError("no admin channel either")

    outcome = await run_guild_digest(world.deps(notify_guild=broken), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "partial"


async def test_cancellation_mid_publish_leaves_it_pending_with_what_posted_and_propagates(world):
    # Changed from "marks failed": a graceful cancel (a deploy) is a pause, not a failure.
    # `pending` with the window and the posted games is what lets the lease-expiry resume
    # finish it; `failed` with posts only ever waited for an admin's run-now.
    import asyncio

    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    world.channels[12].fail = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    row = world.digest(G1)
    assert row.status == "pending" and list(row.posted_by_game) == ["borderlands4"]
    assert row.window_end == DUE

    world.channels[12].fail = []
    world.clock = NOW + timedelta(minutes=11)  # the lease has gone stale
    [outcome] = await run_due_guilds(world.deps())
    assert outcome.status == "ok" and world.digest(G1).attempts == 2
    assert len(world.channels[11].embeds) == 1 and len(world.channels[12].embeds) == 1


# --- the run report ---


async def test_the_run_report_goes_only_to_that_guilds_admin_channel(world):
    world.add_guild(G1)
    world.add_guild(G2)
    world.add_item("borderlands4", "b", in_window())
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)
    [(guild_id, channel_id, text)] = world.reports
    assert (guild_id, channel_id) == (G1, ADMIN[G1])
    assert "Digest posted" in text and "(scheduled)" in text
    assert "https://discord.com/channels/1001/11/" in text
    assert len(world.channels[ADMIN[G1]].contents) == 1 and world.channels[ADMIN[G2]].contents == []
    assert all(not world.channels[c].embeds for c in (21, 23))


async def test_no_admin_channel_means_no_report_and_the_switch_turns_it_off(world):
    world.add_guild(G1, admin=False)
    world.add_item("borderlands4", "b", in_window())
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    assert world.reports == []

    world.add_guild(G2)
    world.add_item("borderlands4", "b2", in_window())
    world.cfg = world.cfg.model_copy(update={"run_report": False})
    await run_guild_digest(world.deps(), G2, kind=RunKind.SCHEDULED)
    assert world.reports == []


async def test_a_report_that_fails_to_send_does_not_undo_the_digest(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())

    async def broken(guild_id, channel_id, text):
        raise RuntimeError("admin channel deleted")

    outcome = await run_guild_digest(world.deps(send_report=broken), G1, kind=RunKind.SCHEDULED)
    assert outcome.status == "ok" and world.digest(G1).status == "ok"


async def test_a_partial_digest_reports_the_skipped_game(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "b", in_window())
    world.add_item("palworld", "p", in_window())
    del world.channels[11]
    await run_guild_digest(world.deps(), G1, kind=RunKind.SCHEDULED)
    [(_, _, text)] = world.reports
    assert "Digest posted with gaps" in text and "Skipped: Borderlands 4" in text


# --- the tick: pacing, isolation, time zones ---


async def test_a_tick_runs_guilds_one_after_another_with_a_pause_between(world):
    for guild_id in (G1, G2):
        world.add_guild(guild_id)
        world.add_item("borderlands4", f"for {guild_id}", in_window())
    outcomes = await run_due_guilds(world.deps())
    assert [(o.guild_id, o.status) for o in outcomes] == [(G1, "ok"), (G2, "ok")]
    assert world.sleeps == [1.0]  # between the two, not before the first or after the last
    assert len(world.channels[11].embeds) == 1 and len(world.channels[21].embeds) == 1


async def test_one_guilds_crash_is_reported_to_it_and_the_next_guild_still_posts(world):
    for guild_id in (G1, G2):
        world.add_guild(guild_id)
        world.add_item("borderlands4", "x", in_window())
    world.factory_boom[G1] = RuntimeError("factory exploded")
    outcomes = await run_due_guilds(world.deps())
    assert [(o.guild_id, o.status) for o in outcomes] == [(G1, "failed"), (G2, "ok")]
    assert [g for g, _ in world.notices] == [G1]  # only the guild that had the problem
    assert world.digest(G1).status == "failed" and world.digest(G2).status == "ok"
    assert world.sleeps == []  # G1 sent nothing before it crashed, so G2 gets no pause


async def test_a_skipped_guild_costs_no_pause(world):
    world.add_guild(G1)
    world.add_guild(G2)
    world.add_item("borderlands4", "x", in_window())
    deps = world.deps()
    await run_guild_digest(deps, G1, kind=RunKind.SCHEDULED)  # G1 is already done today
    outcomes = await run_due_guilds(deps)
    assert [(o.guild_id, o.status) for o in outcomes] == [(G2, "ok")]
    assert world.sleeps == []


async def test_two_guilds_in_different_zones_post_at_different_moments(world):
    world.add_guild(G1)  # Los Angeles, 09:00 PDT = 16:00Z
    world.add_guild(G2, tz="America/New_York")  # 09:00 EDT = 13:00Z
    world.add_item("borderlands4", "x", DUE - timedelta(hours=10))
    deps = world.deps()
    world.clock = datetime(2026, 9, 30, 13, 0, 20, tzinfo=UTC)
    assert [o.guild_id for o in await run_due_guilds(deps)] == [G2]
    assert world.channels[11].embeds == [] and len(world.channels[21].embeds) == 1
    world.clock = NOW
    assert [o.guild_id for o in await run_due_guilds(deps)] == [G1]
    assert world.digest(G2).window_end == datetime(2026, 9, 30, 13, 0, tzinfo=UTC)


async def test_a_dst_spring_forward_digest_runs_once_at_the_shifted_moment(world):
    world.add_guild(G1, time="02:30")
    world.add_item("borderlands4", "x", datetime(2026, 3, 7, 20, 0, tzinfo=UTC))
    deps = world.deps()
    world.clock = datetime(2026, 3, 8, 10, 29, tzinfo=UTC)
    assert await run_due_guilds(deps) == []  # 03:29 PDT: not yet
    world.clock = datetime(2026, 3, 8, 10, 30, 30, tzinfo=UTC)
    assert [o.status for o in await run_due_guilds(deps)] == ["ok"]
    world.clock = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
    assert await run_due_guilds(deps) == []
    assert len(world.channels[11].embeds) == 1


async def test_a_dst_fall_back_digest_runs_once_across_the_repeated_hour(world):
    world.add_guild(G1, time="01:30")
    world.add_item("borderlands4", "x", datetime(2026, 10, 31, 20, 0, tzinfo=UTC))
    deps = world.deps()
    world.clock = datetime(2026, 11, 1, 8, 30, 30, tzinfo=UTC)  # first 01:30 (PDT)
    assert [o.status for o in await run_due_guilds(deps)] == ["ok"]
    for minute in (30, 31, 45):  # the second 01:30 (PST), and after
        world.clock = datetime(2026, 11, 1, 9, minute, tzinfo=UTC)
        assert await run_due_guilds(deps) == []
    assert len(world.channels[11].embeds) == 1


async def test_a_southern_hemisphere_guild_posts_on_its_own_local_day(world):
    world.add_guild(G1, tz="Australia/Sydney")  # 09:00 AEDT = 22:00Z the day before
    world.add_item("borderlands4", "x", datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
    world.clock = datetime(2026, 10, 10, 22, 0, 20, tzinfo=UTC)
    [outcome] = await run_due_guilds(world.deps())
    assert outcome.status == "ok" and outcome.run_date == date(2026, 10, 11)


async def test_a_three_hour_outage_is_made_up_on_the_first_tick_back(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "x", in_window())
    world.clock = DUE + timedelta(hours=3)
    [outcome] = await run_due_guilds(world.deps())
    assert outcome.status == "ok" and len(world.channels[11].embeds) == 1
    world.clock += timedelta(minutes=1)
    assert await run_due_guilds(world.deps()) == []  # and only once


async def test_a_running_guild_is_not_picked_up_again_by_a_tick(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "x", in_window())
    deps = world.deps()
    claim_window = (DUE - timedelta(hours=24), DUE)
    with world.conn() as conn:
        repo.claim_guild_digest(conn, G1, DAY, force=False, window=claim_window, now=lambda: NOW)
    deps.running.add(G1)  # this process is still publishing it
    world.clock = NOW + timedelta(minutes=20)
    assert await run_due_guilds(deps) == []
    deps.running.discard(G1)
    assert [o.status for o in await run_due_guilds(deps)] == ["ok"]


async def test_a_v22_pending_row_is_not_resumed_automatically(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "x", in_window())
    with world.conn() as conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, posted_message_ids, created_at, "
            "updated_at) VALUES (?, ?, 'pending', '[131]', 't', 't')",
            (G1, DAY.isoformat()),
        )
        conn.commit()
    deps = world.deps()
    assert await run_due_guilds(deps) == []
    assert (await run_guild_digest(deps, G1, kind=RunKind.CATCH_UP)).status == "skipped"
    row = await todays_guild_digest(deps, G1)
    assert guild_needs_confirmation(row) is True
    # An admin's confirmed run-now does post.
    assert (await run_guild_digest(deps, G1, kind=RunKind.RUN_NOW, force=True)).status == "ok"


# --- command-layer helpers ---


def test_guild_needs_confirmation_follows_v2s_rule():
    def row(status, posted=None, ids=None):
        from newsbot.store.models import GuildDigestRow

        return GuildDigestRow(1, 1, DAY, status, ids or [], posted or {}, None, None, 1, None)

    assert guild_needs_confirmation(None) is False
    assert guild_needs_confirmation(row("ok")) is True
    assert guild_needs_confirmation(row("partial")) is True
    assert guild_needs_confirmation(row("pending")) is True
    assert guild_needs_confirmation(row("failed")) is False
    assert guild_needs_confirmation(row("failed", {"a": 1})) is True
    assert guild_needs_confirmation(row("failed", ids=[5])) is True  # v2.2's flat list


async def test_todays_guild_digest_uses_the_guilds_own_local_date(world):
    world.add_guild(G1, tz="Pacific/Auckland")
    deps = world.deps()
    assert await todays_guild_digest(deps, G1) is None
    assert await todays_guild_digest(deps, 999) is None
    world.clock = datetime(2026, 9, 30, 20, 30, tzinfo=UTC)  # 09:30 NZDT on the 1st
    world.add_item("borderlands4", "x", world.clock - timedelta(hours=3))
    await run_due_guilds(deps)
    row = await todays_guild_digest(deps, G1)
    assert row.run_date == date(2026, 10, 1)


async def test_preview_builds_the_digest_and_writes_nothing(world):
    world.add_guild(G1)
    world.add_item("borderlands4", "x", in_window())
    deps = world.deps()
    preview = await preview_guild_digest(deps, G1)
    assert [m.topic_key for m in preview.rendered.messages] == ["borderlands4"]
    assert world.digest(G1) is None
    assert all(not ch.embeds for ch in world.channels.values())
    assert await preview_guild_digest(deps, 999) is None


async def test_is_guild_busy_while_a_run_holds_the_lock(world):
    world.add_guild(G1)
    deps = world.deps()
    assert is_guild_busy(deps, G1) is False
    async with deps.lock_for(G1):
        assert is_guild_busy(deps, G1) is True
        assert is_guild_busy(deps, G2) is False


async def test_an_idle_guild_lock_is_pruned_and_a_busy_one_is_kept(world):
    deps = world.deps()
    async with deps.guild_lock(G1):
        assert G1 in deps._locks
        deps.forget_guild(G1)  # held, so it stays (the guild-removed hook can land mid-run)
        assert G1 in deps._locks
    assert deps._locks == {} and deps._users == {}
    assert is_guild_busy(deps, G2) is False
    assert deps._locks == {}  # asking whether a server is busy doesn't make it a lock


async def test_a_lock_pruned_as_a_new_run_starts_still_serializes(world):
    """The holder's exit prunes while a second task is already queued behind it."""
    deps = world.deps()
    order: list[str] = []
    seen: list[asyncio.Lock] = []
    release = asyncio.Event()

    async def first():
        async with deps.guild_lock(G1):
            seen.append(deps._locks[G1])
            order.append("first in")
            await release.wait()
            order.append("first out")

    async def second():
        async with deps.guild_lock(G1):
            seen.append(deps._locks[G1])
            order.append("second in")
            await asyncio.sleep(0)
            order.append("second out")

    async def third():
        # Arrives in the same wakeup the first one releases in.
        await release.wait()
        async with deps.guild_lock(G1):
            seen.append(deps._locks[G1])
            order.append("third in")
            await asyncio.sleep(0)
            order.append("third out")

    tasks = [asyncio.create_task(c()) for c in (first, second, third)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)

    assert order.index("first out") < order.index("second in")
    for name in ("second", "third"):
        assert order.index(f"{name} out") == order.index(f"{name} in") + 1  # never interleaved
    assert len({id(lock) for lock in seen}) == 1  # one server, one lock, start to finish
    assert deps._locks == {} and deps._users == {}


async def test_a_cancelled_waiter_doesnt_leave_a_lock_behind(world):
    deps = world.deps()
    async with deps.guild_lock(G1):
        waiter = asyncio.create_task(deps.guild_lock(G1).__aenter__())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert deps._users == {G1: 1}
    assert deps._locks == {} and deps._users == {}


# --- import day (closes the task 3 note) ---


def _v22_world(v22_db):
    """A v2.2 database with the friend's server imported, as the prod config would."""
    cfg = load_config(Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml")
    imported_at = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)  # 14:00 PDT, after v2.2's digest
    report = ensure_imported(v22_db, cfg, lambda: imported_at)
    return cfg, report


async def test_upgrade_day_the_imported_guild_is_not_due_and_is_due_tomorrow_at_nine(v22_db):
    cfg, report = _v22_world(v22_db)
    friend = 100000000000000001
    # v2.2's row for 2026-09-30 (status ok) now belongs to the guild.
    with closing(connect(v22_db)) as conn:
        [candidate] = repo.due_candidates(conn)
    assert candidate.guild_id == friend and candidate.tier == "comped"
    assert (candidate.run_date, candidate.status) == (DAY, "ok")

    import_time = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
    for minutes in (0, 60, 600):  # through the rest of the local day (to 07:00 the next morning)
        assert due_guilds([candidate], import_time + timedelta(minutes=minutes)) == []
    # Tomorrow at 09:00 PDT (16:00Z) it is, and only then.
    assert due_guilds([candidate], datetime(2026, 10, 1, 15, 59, tzinfo=UTC)) == []
    [due] = due_guilds([candidate], datetime(2026, 10, 1, 16, 0, tzinfo=UTC))
    assert (due.guild_id, due.run_date, due.reason) == (friend, date(2026, 10, 1), "first")

    # And the tick really does nothing today, then runs tomorrow and leaves today's row alone.
    clock = {"now": import_time + timedelta(hours=1)}
    posted: list[int] = []

    def publisher_for(guild, scope, already, on_posted):
        posted.append(guild.guild_id)
        return DiscordPublisher(
            FakeClient({}),
            nonce_scope=scope,
            on_posted=on_posted,
            already_posted=already,
            skip_permanent=True,
        )

    async def notify(guild_id, text):
        return None

    async def no_sleep(seconds):
        return None

    deps = GuildDigestDeps(
        cfg=cfg,
        db_path=str(v22_db),
        now=lambda: clock["now"],
        publisher_for=publisher_for,
        notify_guild=notify,
        summary_for=lambda key, at, after: _none(),
        sleep=no_sleep,
    )
    assert await run_due_guilds(deps) == []
    assert posted == []
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild_digest(conn, friend, DAY).posted_message_ids == [131, 132]

    clock["now"] = datetime(2026, 10, 1, 16, 0, 30, tzinfo=UTC)
    [outcome] = await run_due_guilds(deps)
    assert outcome.guild_id == friend and outcome.run_date == date(2026, 10, 1)
    assert posted == [friend]
    with closing(connect(v22_db)) as conn:
        assert repo.get_guild_digest(conn, friend, DAY).status == "ok"  # untouched


async def _none():
    return None
