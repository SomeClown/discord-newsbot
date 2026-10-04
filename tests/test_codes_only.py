"""v3.0.3: Reddit is codes only, and a codes-only source never reaches the news.

Reddit denied the bot's Data API request (2026-10-04) and its policy bars sharing
Reddit content, so `r/Borderlands4` is read to spot SHiFT codes and then forgotten.
Three things are pinned here. The flag and its Reddit default (config). The pass
(a codes-only item reaches SHiFT detection and the B+ sightings, and is never
stored). And every reader of the store (digest, summary input, `/news recent`,
`/news search`, the AI prompt), against a world where a Reddit post really was
collected, because "nothing reads it" is only true if nothing is there to read.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_shift_fanout_adversarial import Harness, rows, seed
from test_summaries import FakeLLM

from newsbot.collectors.base import RawItem, build_catalog_collectors
from newsbot.config import (
    ConfigError,
    Secrets,
    effective_codes_only,
    is_reddit_source,
    load_config,
)
from newsbot.guilds.importer import ensure_imported
from newsbot.pipeline import prompts
from newsbot.pipeline.collect import CollectionDeps, run_collection, storable_items
from newsbot.pipeline.run import CheckSourcesReport, run_check_sources
from newsbot.pipeline.summaries import SummaryDeps, ensure_summary
from newsbot.reddit import is_reddit_host, is_reddit_url
from newsbot.shift.fanout import make_shift_hook
from newsbot.store import repo
from newsbot.store.db import connect, migrate

FIXTURES = Path(__file__).parent / "fixtures"
PRODLIKE = FIXTURES / "config_v2_prodlike.yaml"
EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"

# 09:00 PDT on 2026-10-04 is 16:00 UTC; the pass runs a couple of hours before it.
DUE = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)
PASS_AT = DUE - timedelta(hours=2)
CODE = "ABCDE-FGHJK-LMNPQ-RSTUV-WXYZ2"
REDDIT_URL = "https://www.reddit.com/r/Borderlands4/comments/abc123/shift_code_megathread/"
# What the store and the alerts call that address once it's been canonicalized.
REDDIT_CANON = "https://reddit.com/r/Borderlands4/comments/abc123/shift_code_megathread"
REDDIT_TITLE = "Megathread: a fresh SHiFT code for Borderlands 4"
PRESS_URL = "https://pcgamer.com/borderlands-4-patch-notes"
PRESS_TITLE = "Borderlands 4 patch notes land"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def _v3(body: str, *, shift: str = "") -> str:
    """A v3 file; `body` starts inside the last game (borderlands4), so it opens with `sources:`."""
    return (
        "home_guild_id: 100000000000000001\n"
        + shift
        + "catalog:\n"
        + "  - key: palworld\n    name: Palworld\n"
        + "  - key: borderlands4\n    name: Borderlands 4\n    aliases: [BL4]\n"
        + body
    )


# --- what counts as Reddit ---


@pytest.mark.parametrize(
    "host",
    [
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "np.reddit.com",
        "WWW.Reddit.COM",
        "reddit.com.",
    ],
)
def test_reddit_hosts(host):
    assert is_reddit_host(host)


@pytest.mark.parametrize(
    "host", [None, "", "notreddit.com", "reddit.com.example.org", "redd.it", "example.com"]
)
def test_not_reddit_hosts(host):
    assert not is_reddit_host(host)


def test_reddit_urls():
    assert is_reddit_url(REDDIT_URL)
    assert not is_reddit_url(PRESS_URL)
    assert not is_reddit_url("https://example.com/?next=https://reddit.com/")
    assert not is_reddit_url("http://[broken")


# --- the flag and its Reddit default ---

BL4_FEED = "https://www.reddit.com/r/Borderlands4/top/.rss?t=day"


def _src(name, url, trust="community", extra="", *, indent="      "):
    return f'{indent}- {{type: rss, name: "{name}", url: "{url}", trust: {trust}{extra}}}\n'


def _reddit_bl4(extra=""):
    return _src("r/Borderlands4", BL4_FEED, extra=extra)


def _bl4_with(extra: str) -> str:
    """The prod-like file with `extra` added to its r/Borderlands4 source."""
    old = f'url: "{BL4_FEED}"\n    topics: [borderlands4]\n    trust: community'
    text = PRODLIKE.read_text()
    assert old in text
    return text.replace(old, old + extra)


def test_the_flag_defaults_to_false_and_is_accepted_on_every_collectable_type(tmp_path):
    flag = ", codes_only: true"
    cfg = load_config(
        _write(
            tmp_path,
            _v3(
                "    sources:\n"
                f'      - {{type: steam_news, name: "S", app_id: 1, trust: official{flag}}}\n'
                f'      - {{type: bluesky_search, name: "B", query: "x", trust: community{flag}}}\n'
                + _src("R", "https://example.com/r", "press", flag)
                + _src("Plain", "https://example.com/p", "press")
                + "shared_sources:\n"
                + _src("Shared", "https://example.com/s", "press", flag, indent="  ")
            ),
        )
    )
    assert cfg.codes_only_source_names() == {"S", "R", "B", "Shared"}
    plain = next(s for s in cfg.catalog[1].sources if s.name == "Plain")
    assert plain.codes_only is None and not effective_codes_only(plain)


def test_a_reddit_source_is_codes_only_with_no_flag(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            _v3(
                "    sources:\n"
                + _reddit_bl4()
                + _src("old", "https://old.reddit.com/r/Borderlands4/.rss")
                + _src("Not Reddit", "https://example.com/reddit.com")
            ),
        )
    )
    assert cfg.codes_only_source_names() == {"r/Borderlands4", "old"}
    assert [s.name for s in cfg.catalog[1].sources] == ["r/Borderlands4", "old", "Not Reddit"]


@pytest.mark.parametrize(
    "body",
    [
        "    sources:\n" + _reddit_bl4(", codes_only: false"),  # under a game
        "shared_sources:\n"
        + _src("r/Borderlands4", BL4_FEED, extra=", codes_only: false", indent="  "),
    ],
)
def test_codes_only_false_on_a_reddit_source_is_an_error_that_says_why(tmp_path, body):
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, _v3(body)))
    message = str(exc.value)
    assert (
        "r/Borderlands4" in message and "codes_only can't be false for a Reddit source" in message
    )
    assert "denied" in message and "forbids sharing" in message


def test_codes_only_false_on_a_reddit_source_is_an_error_in_the_v2_shape_too(tmp_path):
    text = _bl4_with("\n    codes_only: false")
    with pytest.raises(ConfigError, match="codes_only can't be false for a Reddit source"):
        load_config(_write(tmp_path, text))


def test_codes_only_true_on_a_reddit_source_is_fine_and_not_a_nag(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(_write(tmp_path, _bl4_with("\n    codes_only: true")))
    assert "r/Borderlands4" in cfg.codes_only_source_names()
    assert not [r for r in caplog.records if "r/Borderlands4" in r.getMessage()]


@pytest.mark.parametrize("bad", ["maybe", "'yes please'", "[true]"])
def test_codes_only_must_be_a_boolean(tmp_path, bad):
    body = "    sources:\n" + _src("R", "https://example.com/r", "press", f", codes_only: {bad}")
    with pytest.raises(ConfigError, match="codes_only"):
        load_config(_write(tmp_path, _v3(body)))


def test_codes_only_on_web_search_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    old = "  - type: web_search\n    queries_per_topic: 2\n    trust: press"
    text = PRODLIKE.read_text()
    assert old in text
    with pytest.raises(ConfigError, match="codes_only does nothing on web_search"):
        load_config(_write(tmp_path, text.replace(old, old + "\n    codes_only: true")))


# --- Reddit for a game with no SHiFT codes is skipped, with a warning ---


def test_a_reddit_source_for_a_non_shift_game_is_warned_about_and_skipped(tmp_path, caplog):
    body = (
        "    sources:\n"
        + _reddit_bl4()
        + '      - {type: steam_news, name: "BL4 Steam", app_id: 1, trust: official}\n'
        + "  - key: rust\n    name: Rust\n    aliases: [Facepunch]\n    sources:\n"
        + _src("r/playrust", "https://www.reddit.com/r/playrust/.rss")
        + '      - {type: steam_news, name: "Rust Steam", app_id: 2, trust: official}\n'
        + "shared_sources:\n"
        + _src(
            "r/Palworld",
            "https://www.reddit.com/r/Palworld/.rss",
            extra=", games: [palworld]",
            indent="  ",
        )
        + _src("r/gaming", "https://www.reddit.com/r/gaming/.rss", indent="  ")
    )
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(_write(tmp_path, _v3(body, shift="shift:\n  games: [borderlands4]\n")))
    kept = [s.name for g in cfg.catalog for s in g.sources] + [s.name for s in cfg.shared_sources]
    # Non-Reddit sources of the same game stay, and so does a shared one with no game list
    # (the keyword matcher may find a SHiFT game in it).
    assert sorted(kept) == ["BL4 Steam", "Rust Steam", "r/Borderlands4", "r/gaming"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("r/playrust" in m and "it will be skipped" in m for m in warned)
    assert any("r/Palworld" in m and "it will be skipped" in m for m in warned)
    assert not any("r/Borderlands4" in m for m in warned)


def test_the_skipped_source_is_never_built_into_a_collector(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    cfg = load_config(PRODLIKE)
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="k",
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    built = {c.name for c in build_catalog_collectors(cfg, secrets)}
    assert "r/Borderlands4" in built
    assert not {"r/Palworld", "r/diablo4"} & built


def test_an_empty_shift_games_in_the_v2_shape_means_every_game_so_nothing_is_skipped(tmp_path):
    old = "  topics: [borderlands4]\n  interval_minutes"
    text = PRODLIKE.read_text()
    assert old in text
    cfg = load_config(_write(tmp_path, text.replace(old, "  topics: []\n  interval_minutes")))
    assert {"r/Borderlands4", "r/Palworld", "r/diablo4"} <= cfg.codes_only_source_names()


def test_the_prodlike_v2_config_has_one_codes_only_subreddit_and_two_skipped(monkeypatch, caplog):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = load_config(PRODLIKE)
    # The derive path applies the same rule: no flag in the file, and it's codes-only anyway.
    assert cfg.codes_only_source_names() == {"r/Borderlands4"}
    names = [s.name for g in cfg.catalog for s in g.sources] + [s.name for s in cfg.shared_sources]
    assert "r/Borderlands4" in names and "r/Palworld" not in names and "r/diablo4" not in names
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for skipped in ("r/Palworld", "r/diablo4"):
        assert any(skipped in m and "it will be skipped" in m for m in warned)
    bl4 = next(s for s in cfg.catalog[0].sources if s.name == "r/Borderlands4")
    assert is_reddit_source(bl4) and bl4.codes_only is None


def test_the_example_config_keeps_only_r_borderlands4_and_marks_it_codes_only():
    cfg = load_config(EXAMPLE)
    sources = [s for g in cfg.catalog for s in g.sources] + list(cfg.shared_sources)
    reddit = [s for s in sources if is_reddit_source(s)]
    assert [s.name for s in reddit] == ["r/Borderlands4"]
    assert reddit[0].codes_only is True  # spelled out in the file, not just the default
    assert cfg.codes_only_source_names() == {"r/Borderlands4"}
    # And the file itself has no other Reddit source for the loader to be quietly skipping.
    assert EXAMPLE.read_text().count("reddit.com/r/") == 1


# --- the pass: detected, sighted, never stored ---


class FakeFeed:
    source_type = "rss"

    def __init__(self, name, items, *, key=None):
        self.name = name
        self.rate_limit_key = key
        self._items = items

    async def collect(self, http):
        return list(self._items)


def raw(url, title, source, trust, *, excerpt="", topics=("borderlands4",), at=PASS_AT):
    return RawItem(
        url=url,
        title=title,
        excerpt=excerpt,
        source_name=source,
        trust=trust,
        published_at=at - timedelta(hours=1),
        topics=topics,
    )


def reddit_post(*, at=PASS_AT):
    return raw(
        REDDIT_URL,
        REDDIT_TITLE,
        "r/Borderlands4",
        "community",
        excerpt=f"Redeem {CODE} in the SHiFT menu.",
        at=at,
    )


def press_post(*, at=PASS_AT):
    return raw(PRESS_URL, PRESS_TITLE, "PC Gamer", "press", topics=None, at=at)


class World:
    """The prod-like config, imported (the friend's server), with SHiFT detection wired in."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        self.db_path = str(tmp_path / "t.db")
        with closing(connect(self.db_path)) as conn:
            migrate(conn)
        self.cfg = load_config(PRODLIKE)
        report = ensure_imported(self.db_path, self.cfg, lambda: PASS_AT - timedelta(days=3))
        assert report is not None
        self.friend = report.guild_id
        self.shift = Harness(self.db_path, self.cfg, PASS_AT)
        seed(self.db_path)
        self.alerts: list[str] = []

    async def collect(self, feeds, *, at=PASS_AT):
        self.shift.clock = at

        async def alert(text):
            self.alerts.append(text)

        async with httpx.AsyncClient() as http:
            deps = CollectionDeps(
                cfg=self.cfg,
                db_path=self.db_path,
                http=http,
                collectors=feeds,
                now=lambda: at,
                alert=alert,
                shift_hook=make_shift_hook(self.shift.deps()),
            )
            return await run_collection(deps)

    def sql(self, query, *args):
        return rows(self.db_path, query, *args)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


async def test_a_codes_only_item_reaches_detection_and_is_never_stored(world):
    outcome = await world.collect(
        [
            FakeFeed("r/Borderlands4", [reddit_post()], key="reddit"),
            FakeFeed("PC Gamer", [press_post()]),
        ]
    )

    # Stored: the press item, and only that.
    assert world.sql("SELECT url FROM items") == [(PRESS_URL,)]
    assert outcome.new_items == 1
    assert world.sql("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'megathread'") == [(0,)]
    # Detected: the code, attributed to the subreddit, with the post's own address.
    assert outcome.new_codes == 1
    assert world.sql("SELECT code, source_name, item_url, status FROM alerted_codes") == [
        (CODE, "r/Borderlands4", REDDIT_CANON, "posted")
    ]
    # And the alert a server got links back to the post, as it always has.
    (alert,) = world.shift.sent(world.friend)
    assert REDDIT_CANON in alert.content
    # Health still hears about it, and it counts as a source that ran.
    ((failures, ok_at),) = world.sql(
        "SELECT consecutive_failures, last_success_at FROM source_health "
        "WHERE source_name = 'r/Borderlands4'"
    )
    assert failures == 0 and ok_at is not None
    assert (outcome.sources_ok, outcome.sources_total) == (2, 2)


async def test_a_codes_only_sighting_counts_as_a_distinct_source_for_b_plus(world):
    await world.collect([FakeFeed("r/Borderlands4", [reddit_post()], key="reddit")])
    assert world.sql("SELECT source_name FROM code_sightings") == [("r/Borderlands4",)]
    assert world.sql("SELECT COUNT(*) FROM guild_code_followups") == [(0,)]

    # Eight hours later a second, differently named source sees the same code.
    later = PASS_AT + timedelta(hours=8)
    bluesky = raw(
        "https://bsky.app/profile/someone/post/1",
        "Borderlands 4 SHiFT code " + CODE,
        'Bluesky "Borderlands 4" search',
        "community",
        topics=None,
        at=later,
    )
    await world.collect([FakeFeed(bluesky.source_name, [bluesky])], at=later)

    assert world.sql("SELECT source_name FROM code_sightings ORDER BY source_name") == [
        ('Bluesky "Borderlands 4" search',),
        ("r/Borderlands4",),
    ]
    assert world.sql("SELECT guild_id, code, status FROM guild_code_followups") == [
        (world.friend, CODE, "posted")
    ]


async def test_a_reddit_link_under_another_sources_name_is_not_stored_either(world):
    # Web search can return a reddit.com thread under Brave's name; the host check catches it.
    sneaky = raw(REDDIT_URL, REDDIT_TITLE, "PC Gamer", "press", topics=None)
    await world.collect([FakeFeed("PC Gamer", [sneaky, press_post()])])
    assert world.sql("SELECT url FROM items") == [(PRESS_URL,)]


async def test_any_source_can_be_codes_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    old = 'name: "PC Gamer"\n    url: "https://www.pcgamer.com/rss/"\n    trust: press'
    text = PRODLIKE.read_text()
    assert old in text
    text = text.replace(old, old + "\n    codes_only: true")
    w = World(tmp_path, monkeypatch)
    w.cfg = load_config(_write(tmp_path, text))
    assert "PC Gamer" in w.cfg.codes_only_source_names()
    outcome = await w.collect([FakeFeed("PC Gamer", [press_post()])])
    assert w.sql("SELECT COUNT(*) FROM items") == [(0,)] and outcome.new_items == 0
    assert w.sql("SELECT source_name FROM source_health") == [("PC Gamer",)]


def test_storable_items_keeps_everything_but_codes_only_and_reddit_links():
    cfg = load_config(PRODLIKE)
    items = [
        reddit_post(),
        press_post(),
        raw(REDDIT_URL + "2", "x", "Eurogamer", "press"),
    ]
    assert [i.url for i in storable_items(items, cfg)] == [PRESS_URL]


# --- nothing that reads the store can see it ---


@pytest.fixture
async def collected(world):
    """A world where Reddit's post and a press post were both collected, a server following BL4."""
    await world.collect(
        [
            FakeFeed("r/Borderlands4", [reddit_post()], key="reddit"),
            FakeFeed("PC Gamer", [press_post()]),
        ]
    )
    with closing(connect(world.db_path)) as conn:
        repo.create_guild(
            conn,
            777,
            digest_time="09:00",
            timezone="America/Los_Angeles",
            set_up=True,
            now=lambda: PASS_AT - timedelta(days=3),
        )
        repo.follow_game(conn, 777, "borderlands4", 10)
    return world


async def test_news_recent_and_search_never_show_it(collected):
    since = PASS_AT - timedelta(days=1)
    with closing(connect(collected.db_path)) as conn:
        recent, total = repo.query_items(conn, 777, [], since, 50, 0)
        assert [i.title for i in recent] == [PRESS_TITLE] and total == 1
        for query in ("megathread", "SHiFT", "Redeem", "reddit", "code"):
            found, count = repo.search_items(conn, 777, query, since, 50, 0)
            assert REDDIT_TITLE not in [i.title for i in found], query
            assert all(not is_reddit_url(i.url) for i in found)
        assert repo.search_items(conn, 777, "megathread", since, 50, 0) == ([], 0)


async def test_digest_and_summary_inputs_never_include_it(collected):
    start, end = PASS_AT - timedelta(days=1), DUE
    with closing(connect(collected.db_path)) as conn:
        by_game = repo.items_for_window(conn, 777, start, end)
        headlines = [h for items in by_game.values() for h in items]
        assert [h.title for h in headlines] == [PRESS_TITLE]
        feed = repo.summary_items(conn, "borderlands4", start, end)
        assert [i.url for i in feed] == [PRESS_URL]
        # Everything stored, by any route: not a Reddit address in the whole items table.
        urls = [r["url"] for r in conn.execute("SELECT url FROM items")]
        assert urls == [PRESS_URL]
        assert conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0] == 1


async def test_the_friends_ai_prompt_never_includes_it(collected):
    llm = FakeLLM()

    async def alert(_text):
        return None

    async def sleep(_s):
        return None

    deps = SummaryDeps(
        cfg=collected.cfg,
        db_path=collected.db_path,
        llm=llm,
        now=lambda: DUE - timedelta(minutes=20),
        alert=alert,
        collection=None,
        brave_api_key=None,
        sleep=sleep,
    )
    await ensure_summary(deps, "borderlands4", DUE, run_date=date(2026, 10, 4))

    ((system, user),) = llm.calls
    items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
    assert [i["url"] for i in items] == [PRESS_URL]
    for text in (system, user):
        assert "reddit" not in text.lower() and REDDIT_TITLE not in text and CODE not in text
    # The friend's three games are still the games in the prompt, in v2's order.
    assert "video games Borderlands 4, Palworld and Diablo IV." in system


async def test_check_sources_counts_exclude_codes_only_items(world, monkeypatch):
    monkeypatch.setattr(
        "newsbot.pipeline.run.build_catalog_collectors",
        lambda *a, **k: [
            FakeFeed("r/Borderlands4", [reddit_post(at=datetime.now(UTC))], key="reddit"),
            FakeFeed("PC Gamer", [press_post(at=datetime.now(UTC))]),
        ],
    )
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key="k",
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    async with httpx.AsyncClient() as http:
        report = await run_check_sources(world.cfg, secrets, http)
    assert isinstance(report, CheckSourcesReport)
    assert {r.source_name: len(r.items) for r in report.results} == {
        "r/Borderlands4": 1,
        "PC Gamer": 1,
    }
    assert report.topic_counts["borderlands4"] == 1  # the press post, not Reddit's


# --- Reddit rotation with one source ---


async def test_one_reddit_source_is_a_priority_source_and_goes_every_pass(world):
    feed = FakeFeed("r/Borderlands4", [], key="reddit")
    for hour in range(3):
        at = PASS_AT + timedelta(hours=hour)
        outcome = await world.collect([feed], at=at)
        assert (outcome.sources_ok, outcome.sources_total) == (1, 1)


# --- the prompt itself did not change ---


def test_the_prompt_template_text_is_unchanged_from_v3_0_2():
    # v3.0.3 changes which items go into the prompt (Reddit's leave), never the words around
    # them. If this fails, the prompt changed, and that needs an owner-reviewed /newsbot preview.
    pinned = "b4205462daccbbb1918868b765b7dc168c664200f12fba96e7f2dad9746724ef"
    template = "\x00".join([prompts.SYSTEM_PROMPT, prompts._ITEMS_HEADER, prompts._PRIOR_HEADER])
    assert hashlib.sha256(template.encode()).hexdigest() == pinned
