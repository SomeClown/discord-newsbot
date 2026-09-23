"""Tests for newsbot.pipeline.filter: keyword matching, dedicated sources, the per-topic cap."""

from datetime import UTC, datetime

from newsbot.collectors.base import RawItem
from newsbot.config import Topic
from newsbot.pipeline.filter import build_matchers, filter_items

BL4 = Topic(key="borderlands4", name="Borderlands 4", aliases=["BL4"], entities=["Gearbox"])
PALWORLD = Topic(key="palworld", name="Palworld", aliases=[], entities=["Pocketpair"])
DIABLO4 = Topic(key="diablo4", name="Diablo IV", aliases=["Diablo 4", "D4"], entities=["Blizzard"])
TOPICS = [BL4, PALWORLD, DIABLO4]


def _item(
    title="",
    excerpt="",
    *,
    trust="community",
    topics=None,
    published_at=None,
    url="https://example.com/x",
):
    return RawItem(
        url=url,
        title=title,
        excerpt=excerpt,
        source_name="Some Source",
        trust=trust,
        published_at=published_at,
        topics=topics,
    )


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 23, hour, tzinfo=UTC)


# --- matching ---


def test_name_match_is_confident():
    result = filter_items([_item(title="Diablo IV update announced")], TOPICS, max_per_topic=10)
    assert len(result["diablo4"]) == 1
    assert result["diablo4"][0].uncertain is False


def test_alias_match_is_confident():
    result = filter_items(
        [_item(title="Diablo 4 season 12 launches today")], TOPICS, max_per_topic=10
    )
    assert len(result["diablo4"]) == 1
    assert result["diablo4"][0].uncertain is False


def test_d4_does_not_match_d40():
    result = filter_items([_item(title="The D40 lens is on sale")], TOPICS, max_per_topic=10)
    assert result.get("diablo4", []) == []


def test_2k_style_entity_matches_2k_games_not_substring_word():
    topics = [Topic(key="borderlands4", name="Borderlands 4", aliases=[], entities=["2K"])]
    result = filter_items([_item(title="2K Games announces new slate")], topics, max_per_topic=10)
    assert len(result["borderlands4"]) == 1
    assert result["borderlands4"][0].uncertain is True


def test_entity_only_match_is_uncertain():
    result = filter_items([_item(title="Blizzard announces layoffs")], TOPICS, max_per_topic=10)
    assert len(result["diablo4"]) == 1
    assert result["diablo4"][0].uncertain is True


def test_no_match_drops_item():
    result = filter_items([_item(title="Unrelated gaming news")], TOPICS, max_per_topic=10)
    assert all(items == [] for items in result.values())


def test_item_matching_two_topics_appears_under_both():
    result = filter_items(
        [_item(title="Crossover event: Borderlands 4 and Diablo IV team up")],
        TOPICS,
        max_per_topic=10,
    )
    assert len(result["borderlands4"]) == 1
    assert len(result["diablo4"]) == 1


# --- SPEC-DEV 3: dedicated sources ---


def test_dedicated_source_item_with_no_keyword_match_is_kept_confident():
    item = _item(
        title="Patch v0.6.2 Notes", excerpt="Fixes several crash issues.", topics=("palworld",)
    )
    result = filter_items([item], TOPICS, max_per_topic=10)
    assert len(result["palworld"]) == 1
    assert result["palworld"][0].uncertain is False


def test_multi_topic_source_still_needs_keyword_match():
    item = _item(title="Unrelated corporate news", topics=("borderlands4", "diablo4"))
    result = filter_items([item], TOPICS, max_per_topic=10)
    assert result.get("borderlands4", []) == []
    assert result.get("diablo4", []) == []


def test_topics_field_restricts_matching_to_named_topics_only():
    # Would keyword-match diablo4 too, but topics= restricts it to palworld.
    item = _item(title="Diablo IV and Palworld both had big weeks", topics=("palworld",))
    result = filter_items([item], TOPICS, max_per_topic=10)
    assert len(result["palworld"]) == 1
    assert result.get("diablo4", []) == []


# --- cap ordering ---


def test_cap_keeps_official_over_newer_community():
    items = [
        _item(title="Palworld community fan art thread", trust="community", published_at=_at(10)),
        _item(title="Palworld patch notes", trust="official", published_at=_at(1)),
    ]
    result = filter_items(items, TOPICS, max_per_topic=1)
    assert len(result["palworld"]) == 1
    assert result["palworld"][0].item.trust == "official"


def test_cap_ranks_confident_above_uncertain_regardless_of_trust():
    items = [
        # Entity-only ("Blizzard"), official trust, but still just uncertain noise.
        _item(title="Blizzard quarterly earnings", trust="official", published_at=_at(10)),
        # Confident name match, community trust.
        _item(title="Diablo IV players report a new bug", trust="community", published_at=_at(1)),
    ]
    result = filter_items(items, TOPICS, max_per_topic=1)
    assert len(result["diablo4"]) == 1
    assert result["diablo4"][0].uncertain is False


def test_cap_orders_by_recency_within_same_match_and_trust():
    items = [
        _item(title="Palworld news A", trust="press", published_at=_at(1)),
        _item(title="Palworld news B", trust="press", published_at=_at(20)),
    ]
    result = filter_items(items, TOPICS, max_per_topic=1)
    assert result["palworld"][0].item.title == "Palworld news B"


def test_cap_handles_undated_items_without_raising():
    items = [
        _item(title="Palworld news undated", trust="press", published_at=None),
        _item(title="Palworld news dated", trust="press", published_at=_at(5)),
    ]
    result = filter_items(items, TOPICS, max_per_topic=1)
    assert result["palworld"][0].item.title == "Palworld news dated"


def test_max_per_topic_caps_the_list_length():
    items = [_item(title=f"Palworld update {i}", published_at=_at(i % 23)) for i in range(5)]
    result = filter_items(items, TOPICS, max_per_topic=2)
    assert len(result["palworld"]) == 2


# --- build_matchers ---


def test_build_matchers_returns_one_entry_per_topic():
    matchers = build_matchers(TOPICS)
    assert set(matchers) == {"borderlands4", "palworld", "diablo4"}
