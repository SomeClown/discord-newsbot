"""The industry pick's prior headlines are the ones its own cycle was shown.

A game has one story history, so "prior headlines" is every story of the last three days.
The industry pick is made once per distinct digest time, over nearly the same items, so
that rule is wrong for it: the 07:30 pick for an 08:00 server would be handed to the
08:30 pick for a 09:00 server as "already covered", and the 09:00 server would never hear
about the studio that closed. A cycle's prior stories are the ones on its own chain: the
picks that ended at the cycle's coverage mark, and the picks before those, three days back.

The rig is the summaries file's, pointed at the catalog with the industry entry in it. The
model here is a small fake with one rule from the real prompt: it drops (marks not
relevant) any story whose headline is already among its prior headlines.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from test_summaries import BERLIN_DUE, World

from newsbot.industry import INDUSTRY_KEY
from newsbot.pipeline.summaries import ensure_summary
from newsbot.pipeline.summarize import LLMResult, StoriesOut, StoryOut
from newsbot.store import repo
from newsbot.store.models import Coverage, StoryToSave, Usage

DAY = timedelta(days=1)
_CLAIM = "claim-for-the-test"  # a claim token, not a password


class DropsRepeats:
    """One story per item, headline = the item's title, dropped if a prior headline says so."""

    def __init__(self) -> None:
        self.priors: list[list[str]] = []

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        prior: list[str] = []
        if "<prior_stories>" in user:
            prior = json.loads(
                user.split("<prior_stories>\n", 1)[1].split("\n</prior_stories>", 1)[0]
            )
        self.priors.append(prior)
        items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        stories = [
            StoryOut(
                headline=item["title"],
                summary="What happened.",
                label="reported",
                item_urls=[item["url"]],
                relevant=item["title"] not in prior,
            )
            for item in items
        ]
        return LLMResult(StoriesOut(stories=stories), 10, 5)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, cfg_name="config_v3_industry.yaml")
    w.llm = DropsRepeats()
    return w


def _item(world, title, hours_ago):
    world.item(
        INDUSTRY_KEY,
        title,
        BERLIN_DUE - timedelta(hours=hours_ago),
        url=f"https://example.com/industry/{title.replace(' ', '-')}",
    )


def _newest_id(world):
    with world.conn() as conn:
        return repo.latest_item_id(conn, collected_by=world.clock.t)


async def _pick(world, mark, *, at):
    """One industry pick for a cycle whose servers stand at item id `mark` (None: first digest)."""
    world.clock.t = at
    after = None if mark is None else Coverage(at - timedelta(hours=12), mark)
    got = await ensure_summary(world.deps(), INDUSTRY_KEY, at, run_date=at.date(), after=after)
    assert got is not None
    return got


def _headlines(got):
    return [s.headline for s in got.stories]


# --- through the summaries job ---


async def test_a_later_cycle_still_gets_a_story_only_an_earlier_cycle_was_shown(world):
    _item(world, "old a", 40)
    a0 = await _pick(world, None, at=BERLIN_DUE - timedelta(hours=38))  # server A's first
    _item(world, "old b", 20)
    b0 = await _pick(world, None, at=BERLIN_DUE - timedelta(hours=18))  # server B's first
    mark_a, mark_b = a0.items_upto, b0.items_upto
    assert mark_a < mark_b

    _item(world, "BioWare shut down", 3)
    a1 = await _pick(world, mark_a, at=BERLIN_DUE - timedelta(hours=2))
    assert "BioWare shut down" in _headlines(a1)

    # Server B was never shown it, and its pick is an hour or so after A's.
    b1 = await _pick(world, mark_b, at=BERLIN_DUE - timedelta(hours=1))

    assert _headlines(b1) == ["BioWare shut down"]
    assert "BioWare shut down" not in world.llm.priors[-1]
    assert sorted(world.llm.priors[-1]) == ["old a", "old b"]


async def test_a_later_pick_on_the_same_chain_is_told_what_the_chain_was_shown(world):
    _item(world, "old a", 10)
    a0 = await _pick(world, None, at=BERLIN_DUE - timedelta(hours=9))
    _item(world, "BioWare shut down", 6)
    a1 = await _pick(world, a0.items_upto, at=BERLIN_DUE - timedelta(hours=5))
    world.item(
        INDUSTRY_KEY,
        "BioWare shut down",
        BERLIN_DUE - timedelta(hours=2),
        url="https://example.com/industry/other-outlet",
    )

    a2 = await _pick(world, a1.items_upto, at=BERLIN_DUE - timedelta(hours=1))

    assert sorted(world.llm.priors[-1]) == ["BioWare shut down", "old a"]
    assert _headlines(a2) == []


async def test_a_first_digest_has_no_prior_stories(world):
    _item(world, "old a", 10)
    await _pick(world, None, at=BERLIN_DUE - timedelta(hours=9))
    _item(world, "newer", 3)

    await _pick(world, None, at=BERLIN_DUE - timedelta(hours=2))

    assert world.llm.priors[-1] == []


async def test_a_games_prior_headlines_are_still_every_story_of_three_days(world):
    # The rule change is the industry entry's alone: a game's picks, whichever cycle they were
    # made for, are all history to the next one.
    for n in range(2):
        _save(
            world,
            after=n * 10,
            upto=n * 10 + 10,
            headlines=[f"patch {n}"],
            at=BERLIN_DUE - timedelta(hours=10 - n),
            game="palworld",
        )
    with world.conn() as conn:
        got = repo.recent_headlines(conn, "palworld", BERLIN_DUE - 3 * DAY)
    assert [p.headline for p in got] == ["patch 1", "patch 0"]


# --- the chain rule itself ---


def _save(world, *, after, upto, headlines, at, status="ok", game=INDUSTRY_KEY):
    """Store a pick directly. A story needs an item behind it, so each headline gets one."""
    stories = []
    for headline in headlines:
        url = f"https://example.com/{game}/{upto}/{headline.replace(' ', '-')}"
        world.item(game, headline, at, url=url)
        stories.append(StoryToSave(game, headline, "x", "reported", [url], None))
    with world.conn() as conn:
        return repo.save_game_summary(
            conn,
            game_key=game,
            run_date=at.date(),
            status=status,
            window_start=at - DAY,
            window_end=at,
            coverage_notes=[],
            note=None,
            usage=Usage(1, 1),
            stories=stories,
            token=_CLAIM,
            now=lambda: at,
            items_after=after,
            items_upto=upto,
        )


def _chain(world, mark, *, since=None):
    with world.conn() as conn:
        got = repo.chain_headlines(conn, INDUSTRY_KEY, since or BERLIN_DUE - 3 * DAY, mark)
    return [p.headline for p in got]


def test_the_chain_walks_back_from_the_mark_newest_first(world):
    _save(world, after=None, upto=10, headlines=["one"], at=BERLIN_DUE - timedelta(hours=30))
    _save(world, after=10, upto=20, headlines=["two"], at=BERLIN_DUE - timedelta(hours=20))
    _save(world, after=20, upto=30, headlines=["three"], at=BERLIN_DUE - timedelta(hours=10))

    assert _chain(world, 30) == ["three", "two", "one"]
    assert _chain(world, 20) == ["two", "one"]
    assert _chain(world, 10) == ["one"]


def test_no_mark_or_a_mark_before_every_pick_gives_nothing(world):
    _save(world, after=5, upto=10, headlines=["one"], at=BERLIN_DUE - timedelta(hours=30))

    assert _chain(world, None) == []
    assert _chain(world, 9) == []


def test_a_mark_newer_than_the_pick_behind_it_still_finds_that_pick(world):
    # A fallback day or a run-now records the digest's own range end, which an item stored
    # after the pick makes newer than the pick's. The server was shown the pick before it.
    _save(world, after=None, upto=10, headlines=["one"], at=BERLIN_DUE - timedelta(hours=30))
    _save(
        world,
        after=10,
        upto=20,
        headlines=[],
        at=BERLIN_DUE - timedelta(hours=20),
        status="fallback",
    )

    assert _chain(world, 21) == ["one"]
    assert _chain(world, 25) == ["one"]


def test_an_old_pick_outside_the_window_is_not_swapped_for_another_cycles(world):
    _save(world, after=None, upto=10, headlines=["old"], at=BERLIN_DUE - 5 * DAY)
    _save(world, after=None, upto=5, headlines=["other cycle"], at=BERLIN_DUE - timedelta(hours=5))

    assert _chain(world, 12) == []


def test_another_cycles_picks_are_not_on_the_chain(world):
    _save(world, after=None, upto=10, headlines=["a first"], at=BERLIN_DUE - timedelta(hours=30))
    _save(world, after=None, upto=12, headlines=["b first"], at=BERLIN_DUE - timedelta(hours=29))
    _save(world, after=10, upto=20, headlines=["a second"], at=BERLIN_DUE - timedelta(hours=6))

    assert _chain(world, 12) == ["b first"]
    assert _chain(world, 20) == ["a second", "a first"]


def test_a_chain_older_than_three_days_is_cut_where_the_window_ends(world):
    for day in range(5):
        _save(
            world,
            after=None if day == 0 else day * 10,
            upto=(day + 1) * 10,
            headlines=[f"day {day}"],
            at=BERLIN_DUE - (5 - day) * DAY + timedelta(hours=1),
        )

    got = _chain(world, 50)

    # Days 2, 3 and 4 are inside three days of BERLIN_DUE; 0 and 1 are not.
    assert got == ["day 4", "day 3", "day 2"]


def test_a_fallback_pick_has_no_stories_but_the_chain_goes_through_it(world):
    _save(world, after=None, upto=10, headlines=["one"], at=BERLIN_DUE - timedelta(hours=30))
    _save(
        world,
        after=10,
        upto=20,
        headlines=[],
        at=BERLIN_DUE - timedelta(hours=20),
        status="fallback",
    )
    _save(world, after=20, upto=30, headlines=["three"], at=BERLIN_DUE - timedelta(hours=10))

    assert _chain(world, 20) == ["one"]  # on the fallback's mark: nothing new was shown
    assert _chain(world, 30) == ["three", "one"]  # rejoined


def test_a_pick_that_found_nothing_new_does_not_loop_the_walk(world):
    _save(world, after=None, upto=10, headlines=["one"], at=BERLIN_DUE - timedelta(hours=30))
    _save(world, after=10, upto=10, headlines=[], at=BERLIN_DUE - timedelta(hours=20))
    _save(world, after=10, upto=10, headlines=[], at=BERLIN_DUE - timedelta(hours=10))

    assert _chain(world, 10) == ["one"]


def test_two_chains_that_land_on_one_mark_are_both_possibly_shown(world):
    # Two cycles whose picks both ended at the newest item. A server standing on that mark was
    # shown one of them; the rule says "could have been shown", so it is told both.
    _save(world, after=5, upto=20, headlines=["via a"], at=BERLIN_DUE - timedelta(hours=10))
    _save(world, after=8, upto=20, headlines=["via b"], at=BERLIN_DUE - timedelta(hours=9))

    assert _chain(world, 20) == ["via b", "via a"]


def test_the_chain_only_reads_the_asked_for_game(world):
    _save(world, after=None, upto=10, headlines=["industry"], at=BERLIN_DUE - timedelta(hours=5))
    _save(
        world,
        after=None,
        upto=10,
        headlines=["palworld"],
        at=BERLIN_DUE - timedelta(hours=5),
        game="palworld",
    )

    assert _chain(world, 10) == ["industry"]
