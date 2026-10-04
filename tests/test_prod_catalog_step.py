"""Production's catalog step: a v2-shaped config.yaml plus the example's marked blocks.

Production's config.yaml is still v2-shaped (guild_id, digest, topics, sources,
alerts, lounge, plus home_guild_id, owner_channel_id and comped_guild_ids), and
the 16-game catalog goes in by appending one marker-delimited stretch of
config.example.yaml to it. That's an edit to a file I can't see, run by a person
on a server at an hour when nobody wants surprises, so this file does the
rehearsal ahead of time: the prod-like fixture, plus exactly what the runbook
appends (cut out with the runbook's own awk command), has to be the same bot
for the friend's server as before, only with more games on the shelf.

The sources are the one place the answer is "more, not identical": the catalog
brings the example's own sources for the friend's three games (official Bluesky
accounts, two more press feeds), so health continuity is a superset check, and
the one source that vanishes is named below so nobody finds out the hard way.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from v3_fakes import raw_v2

from newsbot.collectors.base import RawItem
from newsbot.config import load_config
from newsbot.guilds.importer import ensure_imported
from newsbot.pipeline.filter import filter_items
from newsbot.pipeline.prompts import build_prompt
from newsbot.pipeline.summaries import _all_topics
from newsbot.store.db import connect, migrate
from newsbot.store.models import PriorStory

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "config.example.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "config_v2_prodlike.yaml"

# The awk program in the owner's recipe: `awk 'PROGRAM' config.example.yaml`. Keep it identical to
# the one in the runbook.
AWK_PROGRAM = "/^# >>> catalog-step: start/{f=1;next} /^# <<< catalog-step: end/{f=0} f"

# The three keys the real prod file adds on top of the v2.2 shape. The values are the
# real, non-secret ids from CLAUDE.md; the friend's guild id is the fixture's fake one.
PROD_EXTRAS = """
home_guild_id: 1552824311608512532
owner_channel_id: 1555677520806944859
comped_guild_ids: [100000000000000001]
"""

FRIENDS_GAMES = ["borderlands4", "palworld", "diablo4"]
THEN = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)


def _recipe_blocks() -> str:
    # The same awk the runbook runs, run in the repo the way the runbook runs it on /opt/newsbot.
    if shutil.which("awk") is None:
        pytest.skip("no awk on this machine")
    # A fixed argv with no user input in it, hence the two noqa codes.
    out = subprocess.run(  # noqa: S603
        ["awk", AWK_PROGRAM, "config.example.yaml"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), "the catalog-step markers are missing from config.example.yaml"
    return out


@pytest.fixture(autouse=True)
def _brave(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")


@pytest.fixture
def before_path(tmp_path) -> Path:
    path = tmp_path / "config.pre-catalog.yaml"
    path.write_text(FIXTURE.read_text() + PROD_EXTRAS)
    return path


@pytest.fixture
def after_path(tmp_path, before_path) -> Path:
    # `cp config.yaml config.pre-catalog.yaml`, then `printf '\n' >> ...` and the awk appended.
    path = tmp_path / "config.yaml"
    path.write_text(before_path.read_text() + "\n" + _recipe_blocks())
    return path


@pytest.fixture
def before(before_path):
    return load_config(before_path)


@pytest.fixture
def after(after_path):
    return load_config(after_path)


# --- the recipe itself ---


def test_the_markers_appear_once_and_cut_out_no_top_level_key_prod_already_has():
    text = EXAMPLE.read_text()
    assert text.count("# >>> catalog-step: start") == 1
    assert text.count("# <<< catalog-step: end") == 1
    top_level = {
        line.split(":", 1)[0]
        for line in _recipe_blocks().splitlines()
        if line and not line[0].isspace() and not line.startswith("#")
    }
    # PyYAML takes the last of two equal keys without a word, so a clash here would quietly
    # replace prod's own setting. These are the v2 and prod keys the blocks must never carry.
    prod_keys = {
        "guild_id",
        "admin_channel_id",
        "admin_permission",
        "digest",
        "topics",
        "sources",
        "alerts",
        "lounge",
        "home_guild_id",
        "owner_channel_id",
        "comped_guild_ids",
        "command_guild_ids",
    }
    assert top_level == {
        "collection",
        "ai",
        "run_report",
        "web_search",
        "shift",
        "shared_sources",
        "catalog",
    }
    assert not top_level & prod_keys


# --- 1 and 2: it loads, friend's games first ---


def test_it_loads_with_sixteen_games_and_the_friends_three_first_in_v2_order(before, after):
    assert [g.key for g in before.catalog] == FRIENDS_GAMES
    keys = [g.key for g in after.catalog]
    assert len(keys) == 16
    assert keys[:3] == ["borderlands4", "palworld", "diablo4"]
    assert keys[-1] == "arcraiders"
    assert [g.name for g in after.catalog[:3]] == ["Borderlands 4", "Palworld", "Diablo IV"]


def test_the_friends_three_games_keep_their_matching_terms(before, after):
    for old, new in zip(before.catalog, after.catalog[:3], strict=True):
        assert (new.key, new.name, new.aliases, new.entities) == (
            old.key,
            old.name,
            old.aliases,
            old.entities,
        )
        assert new.search_queries == old.search_queries
        assert new.match_name == old.match_name


def test_the_prod_only_keys_survive(before, after):
    assert after.home_guild_id == before.home_guild_id == 1552824311608512532
    assert after.owner_channel_id == before.owner_channel_id == 1555677520806944859
    assert after.comped_guild_ids == before.comped_guild_ids == [100000000000000001]
    assert after.admin_channel_id == before.admin_channel_id == 100000000000000002
    assert after.effective_owner_channel_id == 1555677520806944859


# --- 3: the friend's AI prompt ---


def _items() -> list[RawItem]:
    published = THEN - timedelta(hours=3)
    rows = [
        ("Borderlands 4 patch notes", "official", ("borderlands4",)),
        ("Gearbox is hiring", "press", None),  # entity only: an uncertain match
        ("Palworld raid guide", "press", None),
        ("Diablo IV season starts", "official", ("diablo4",)),
        ("ARC Raiders update 1.48.0", "official", ("arcraiders",)),
        ("Nothing about any game", "press", None),
    ]
    return [
        RawItem(
            url=f"https://example.com/{n}",
            title=title,
            excerpt=f"Excerpt {n}. " * 20,
            source_name="Feed",
            trust=trust,
            published_at=published - timedelta(minutes=n),
            topics=topics,
        )
        for n, (title, trust, topics) in enumerate(rows)
    ]


@pytest.mark.parametrize("game_key", FRIENDS_GAMES)
def test_the_friends_prompt_is_byte_identical_to_the_pre_catalog_prompt(
    game_key, before_path, before, after
):
    items = _items()
    prior = [PriorStory(1, "Yesterday's headline", THEN - timedelta(days=1))]
    followed = set(FRIENDS_GAMES)  # a comped follow: the friend's three, the only comped server

    def prompt(cfg):
        game = next(g for g in cfg.catalog if g.key == game_key)
        grouped = filter_items(items, cfg.catalog, cfg.collection.max_items_per_game)
        return build_prompt(
            game,
            grouped.get(game_key, []),
            prior,
            all_topics=_all_topics(cfg, followed, game),
            subject=cfg.ai.subject,
        )

    # The yardstick is what v2.2 built it from: its topics and digest.subject, no catalog at all.
    v2 = raw_v2(before_path)
    v2_game = next(t for t in v2.topics if t.key == game_key)
    v2_grouped = filter_items(items, v2.topics, v2.digest.max_items_per_topic)
    yardstick = build_prompt(
        v2_game,
        v2_grouped.get(game_key, []),
        prior,
        all_topics=v2.topics,
        subject=v2.digest.subject,
    )

    assert prompt(before) == yardstick
    assert prompt(after) == yardstick
    assert "video games Borderlands 4, Palworld and Diablo IV." in yardstick[0]


# --- 4: SHiFT ---


def test_shift_settings_are_unchanged(before, after):
    assert after.shift == before.shift
    assert after.shift.games == ["borderlands4"]
    assert after.shift.ping_trust == ["official", "press"]
    assert after.shift.max_pings_per_day == 3
    assert after.shift.max_codes_per_item == 5  # the roundup threshold
    assert after.shift.max_item_age_hours == 48
    # Channel handling is the import's business and the import reads `legacy`.
    assert after.legacy.shift_enabled and after.legacy.shift_channel_id == 1553597251933438122
    assert after.legacy.shift_ping == "everyone"
    assert after.legacy.alerts_max_pings == 3


def test_every_block_the_recipe_adds_equals_what_v2_derived_for_this_server(before, after):
    # Production's digest and alerts settings sit at their defaults, so the example's explicit
    # collection, ai, run_report and shift blocks are the values the v2 keys were feeding.
    # (The runbook's validation command prints this comparison for the real file.)
    assert after.collection == before.collection
    assert after.ai == before.ai
    assert after.run_report is before.run_report
    assert after.web_search == before.web_search
    assert after.owner_report == before.owner_report


# --- 5: the import, and a restart ---


def test_the_import_sees_the_same_legacy_setup(before, after):
    assert after.legacy == before.legacy
    assert after.legacy.games == [
        ("borderlands4", 1452017235274240221),
        ("palworld", 1542581309845799013),
        ("diablo4", 1531681353681211524),
    ]


def test_a_restart_after_the_import_has_run_changes_nothing(tmp_path, before, after):
    db = tmp_path / "newsbot.db"
    with closing(connect(db)) as conn:
        migrate(conn)
    assert ensure_imported(db, before, lambda: THEN) is not None  # the import that already ran

    def snapshot():
        with closing(connect(db)) as conn:
            return [
                [tuple(r) for r in conn.execute(sql)]
                for sql in (
                    "SELECT * FROM guilds",
                    "SELECT * FROM guild_games ORDER BY game_key",
                )
            ]

    imported = snapshot()
    assert ensure_imported(db, after, lambda: THEN + timedelta(days=3)) is None
    assert snapshot() == imported


# --- 6: source health continuity ---


@pytest.mark.parametrize("game_key", FRIENDS_GAMES)
def test_the_friends_games_keep_every_source_name_but_the_fixtures_synthetic_wire(
    game_key, before, after
):
    old = set(before.game_source_names(game_key))
    new = set(after.game_source_names(game_key))
    # "Pocketpair and Gearbox Wire" is an example.com source the fixture invented to exercise
    # the two-topics case; production has no such feed. Anything else missing would reset that
    # source's health history (source_health is keyed by name), so it would be a real finding.
    assert old - new <= {"Pocketpair and Gearbox Wire"}
    assert {"2K Newsroom", "PC Gamer", "Eurogamer"} <= new
    # The new names are additions, never renames of the old ones.
    assert new - old, "expected the catalog to add sources for the friend's games"


def test_the_sources_the_catalog_adds_for_the_friends_games_are_named(before, after):
    added = {
        key: sorted(set(after.game_source_names(key)) - set(before.game_source_names(key)))
        for key in FRIENDS_GAMES
    }
    assert added == {
        "borderlands4": ["Borderlands Bluesky", "GamesRadar+", "PCGamesN"],
        "palworld": ["Bluesky: Palworld", "GamesRadar+", "PCGamesN", "Palworld Bluesky"],
        "diablo4": ['Bluesky: "Diablo 4"', "Diablo Bluesky", "GamesRadar+", "PCGamesN"],
    }


# --- 7: Reddit ---


def test_r_borderlands4_is_still_codes_only_and_a_shift_source(before, after):
    for cfg in (before, after):
        game = next(g for g in cfg.catalog if g.key == "borderlands4")
        reddit = [s for s in game.sources if s.name == "r/Borderlands4"]
        assert len(reddit) == 1
        assert "borderlands4" in cfg.shift.games
        assert "r/Borderlands4" in cfg.codes_only_source_names()
        assert cfg.codes_only_source_names() == {"r/Borderlands4"}


def test_no_other_reddit_feed_comes_back(after):
    names = [s.name for g in after.catalog for s in g.sources] + [
        s.name for s in after.shared_sources
    ]
    assert [n for n in names if n.startswith("r/")] == ["r/Borderlands4"]
