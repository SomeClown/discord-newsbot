"""Tests for the guild functions in newsbot.store.repo (design.md §15, plan task 2).

The rule these are here to enforce is the boring, important one: every
function takes a `guild_id` and only ever touches that guild. So most tests
set up two guilds and check the other one didn't notice.
"""

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from newsbot.store import repo
from newsbot.store.db import StoreError, connect, migrate
from newsbot.store.models import LoungeSettings, StoredItem

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def _clock(moment=T0):
    return lambda: moment


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "n.db")) as c:
        migrate(c)
        yield c


@pytest.fixture
def two_guilds(conn):
    repo.create_guild(conn, 1, now=_clock())
    repo.create_guild(conn, 2, now=_clock())
    return conn


# --- guilds ---


def test_create_guild_defaults_and_get_round_trip(conn):
    assert repo.create_guild(conn, 111, now=_clock()) is True
    guild = repo.get_guild(conn, 111)
    assert guild.guild_id == 111
    assert (guild.digest_time, guild.timezone, guild.tier) == ("09:00", "UTC", "free")
    assert (guild.set_up, guild.admin_channel_id, guild.imported_at) == (False, None, None)
    assert guild.joined_at == T0 and guild.updated_at == T0
    assert guild.permission_problems is None


def test_create_guild_twice_keeps_the_first_row(conn):
    repo.create_guild(conn, 111, timezone="Europe/London", now=_clock())
    assert repo.create_guild(conn, 111, timezone="Asia/Tokyo", now=_clock()) is False
    assert repo.get_guild(conn, 111).timezone == "Europe/London"


def test_create_guild_carries_import_fields(conn):
    repo.create_guild(
        conn,
        111,
        digest_time="08:30",
        timezone="America/Los_Angeles",
        admin_channel_id=5,
        tier="comped",
        set_up=True,
        imported_at=T0,
        now=_clock(),
    )
    guild = repo.get_guild(conn, 111)
    assert (guild.digest_time, guild.tier, guild.set_up, guild.admin_channel_id) == (
        "08:30",
        "comped",
        True,
        5,
    )
    assert guild.imported_at == T0


def test_get_guild_missing_is_none(conn):
    assert repo.get_guild(conn, 404) is None


def test_list_set_up_guilds_only_returns_finished_ones_in_id_order(conn):
    repo.create_guild(conn, 30, set_up=True)
    repo.create_guild(conn, 10, set_up=True)
    repo.create_guild(conn, 20)
    assert [g.guild_id for g in repo.list_set_up_guilds(conn)] == [10, 30]


def test_update_guild_settings_changes_only_what_was_passed(two_guilds):
    later = _clock(T0 + timedelta(hours=1))
    assert repo.update_guild_settings(
        two_guilds, 1, digest_time="07:15", timezone="Europe/Paris", set_up=True, now=later
    )
    guild = repo.get_guild(two_guilds, 1)
    assert (guild.digest_time, guild.timezone, guild.set_up) == ("07:15", "Europe/Paris", True)
    assert guild.tier == "free" and guild.updated_at == T0 + timedelta(hours=1)
    other = repo.get_guild(two_guilds, 2)
    assert (other.digest_time, other.timezone, other.set_up) == ("09:00", "UTC", False)


def test_update_guild_settings_can_clear_nullable_fields(two_guilds):
    repo.update_guild_settings(two_guilds, 1, admin_channel_id=77, permission_problems="no send")
    assert repo.get_guild(two_guilds, 1).admin_channel_id == 77
    repo.update_guild_settings(two_guilds, 1, admin_channel_id=None, permission_problems=None)
    guild = repo.get_guild(two_guilds, 1)
    assert (guild.admin_channel_id, guild.permission_problems) == (None, None)


def test_update_guild_settings_with_nothing_to_change_still_touches_updated_at(two_guilds):
    later = _clock(T0 + timedelta(days=1))
    assert repo.update_guild_settings(two_guilds, 1, now=later) is True
    assert repo.get_guild(two_guilds, 1).updated_at == T0 + timedelta(days=1)


def test_update_guild_settings_unknown_guild_is_false(conn):
    assert repo.update_guild_settings(conn, 404, tier="comped") is False


def test_update_guild_settings_surfaces_check_violations(two_guilds):
    with pytest.raises(sqlite3.IntegrityError):
        repo.update_guild_settings(two_guilds, 1, tier="platinum")
    with pytest.raises(sqlite3.IntegrityError):
        repo.update_guild_settings(two_guilds, 1, digest_time="99:99x")
    assert repo.get_guild(two_guilds, 1).tier == "free"


def test_hostile_values_are_bound_not_interpolated(two_guilds):
    nasty = "x'; DROP TABLE guilds; #"
    repo.update_guild_settings(two_guilds, 1, timezone=nasty, permission_problems=nasty)
    assert repo.get_guild(two_guilds, 1).timezone == nasty
    assert repo.follow_game(two_guilds, 1, nasty, 5) is True
    assert repo.list_guild_games(two_guilds, 1)[0].game_key == nasty
    assert repo.get_guild(two_guilds, 2) is not None


def test_delete_guild_removes_everything_it_owns_and_nothing_else(two_guilds):
    for guild_id in (1, 2):
        repo.set_guild_games(two_guilds, guild_id, [("bl4", 5)])
        repo.set_shift(two_guilds, guild_id, enabled=True, channel_id=6, ping="everyone")
        repo.upsert_lounge(two_guilds, _lounge(guild_id))
        repo.add_notice(two_guilds, guild_id, "hi")
    assert repo.delete_guild(two_guilds, 1) is True
    assert repo.get_guild(two_guilds, 1) is None
    assert repo.list_guild_games(two_guilds, 1) == []
    assert repo.get_shift(two_guilds, 1) is None
    assert repo.get_lounge(two_guilds, 1) is None
    assert repo.recent_notices(two_guilds, 1) == []
    assert len(repo.list_guild_games(two_guilds, 2)) == 1
    assert repo.get_shift(two_guilds, 2) is not None
    assert repo.get_lounge(two_guilds, 2) is not None
    assert len(repo.recent_notices(two_guilds, 2)) == 1
    assert repo.delete_guild(two_guilds, 1) is False


# --- games ---


def test_set_guild_games_replaces_and_keeps_order(two_guilds):
    repo.set_guild_games(two_guilds, 1, [("c", 3), ("a", 1), ("b", 2)])
    assert [(g.game_key, g.channel_id) for g in repo.list_guild_games(two_guilds, 1)] == [
        ("c", 3),
        ("a", 1),
        ("b", 2),
    ]
    repo.set_guild_games(two_guilds, 1, [("z", 9)])
    assert [g.game_key for g in repo.list_guild_games(two_guilds, 1)] == ["z"]
    assert repo.list_guild_games(two_guilds, 2) == []


def test_set_guild_games_over_the_limit_writes_nothing(two_guilds):
    repo.set_guild_games(two_guilds, 1, [("keep", 1)])
    with pytest.raises(repo.GameLimitError):
        repo.set_guild_games(two_guilds, 1, [(f"g{i}", 1) for i in range(11)])
    assert [g.game_key for g in repo.list_guild_games(two_guilds, 1)] == ["keep"]


def test_set_guild_games_accepts_exactly_ten_and_rejects_duplicates(two_guilds):
    repo.set_guild_games(two_guilds, 1, [(f"g{i}", 1) for i in range(10)])
    assert len(repo.list_guild_games(two_guilds, 1)) == 10
    with pytest.raises(ValueError):
        repo.set_guild_games(two_guilds, 1, [("a", 1), ("a", 2)])
    assert len(repo.list_guild_games(two_guilds, 1)) == 10


def test_set_guild_games_for_an_unknown_guild_fails_and_rolls_back(conn):
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_guild_games(conn, 404, [("a", 1)])


def test_game_limit_error_is_a_store_error():
    assert issubclass(repo.GameLimitError, StoreError)


def test_follow_game_adds_then_repoints(two_guilds):
    assert repo.follow_game(two_guilds, 1, "bl4", 5) is True
    assert repo.follow_game(two_guilds, 1, "bl4", 6) is False
    assert [(g.game_key, g.channel_id) for g in repo.list_guild_games(two_guilds, 1)] == [
        ("bl4", 6)
    ]


def test_follow_game_eleventh_is_refused_but_repointing_the_tenth_is_fine(two_guilds):
    for i in range(10):
        repo.follow_game(two_guilds, 1, f"g{i}", 5)
    with pytest.raises(repo.GameLimitError):
        repo.follow_game(two_guilds, 1, "g10", 5)
    assert repo.follow_game(two_guilds, 1, "g9", 8) is False
    assert len(repo.list_guild_games(two_guilds, 1)) == 10
    # The other guild has its own ten.
    assert repo.follow_game(two_guilds, 2, "g10", 5) is True


def test_follow_game_other_integrity_errors_are_not_disguised(conn):
    with pytest.raises(sqlite3.IntegrityError) as info:
        repo.follow_game(conn, 404, "bl4", 5)
    assert not isinstance(info.value, repo.GameLimitError)
    repo.create_guild(conn, 1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.follow_game(conn, 1, "bl4", 0)


def test_unfollow_game_is_scoped_to_the_guild(two_guilds):
    repo.follow_game(two_guilds, 1, "bl4", 5)
    repo.follow_game(two_guilds, 2, "bl4", 5)
    assert repo.unfollow_game(two_guilds, 1, "bl4") is True
    assert repo.unfollow_game(two_guilds, 1, "bl4") is False
    assert [g.game_key for g in repo.list_guild_games(two_guilds, 2)] == ["bl4"]


def test_concurrent_follows_never_exceed_ten(tmp_path):
    path = tmp_path / "c.db"
    with closing(connect(path)) as setup:
        migrate(setup)
        repo.create_guild(setup, 1)
        repo.set_guild_games(setup, 1, [(f"g{i}", 5) for i in range(5)])
    outcomes: list[object] = []
    barrier = threading.Barrier(8)

    def follow(key):
        with closing(connect(path)) as c:
            barrier.wait(timeout=10)
            try:
                outcomes.append(repo.follow_game(c, 1, key, 5))
            except repo.GameLimitError:
                outcomes.append("limit")

    threads = [threading.Thread(target=follow, args=(f"n{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert outcomes.count(True) == 5 and outcomes.count("limit") == 3
    with closing(connect(path)) as c:
        assert len(repo.list_guild_games(c, 1)) == 10


# --- SHiFT ---


def test_get_shift_missing_is_none(two_guilds):
    assert repo.get_shift(two_guilds, 1) is None


def test_set_shift_first_enable_stamps_enabled_at(two_guilds):
    repo.set_shift(two_guilds, 1, enabled=True, channel_id=9, ping="everyone", now=_clock())
    shift = repo.get_shift(two_guilds, 1)
    assert (shift.enabled, shift.channel_id, shift.ping) == (True, 9, "everyone")
    assert shift.enabled_at == T0
    assert (shift.ping_day, shift.ping_count) == (None, 0)
    assert repo.get_shift(two_guilds, 2) is None


def test_set_shift_resaving_does_not_move_enabled_at_and_keeps_the_ping_budget(two_guilds):
    repo.set_shift(two_guilds, 1, enabled=True, channel_id=9, ping="none", now=_clock())
    with two_guilds:
        two_guilds.execute(
            "UPDATE guild_shift SET ping_day = '2026-09-30', ping_count = 2 WHERE guild_id = 1"
        )
    later = _clock(T0 + timedelta(days=2))
    repo.set_shift(
        two_guilds, 1, enabled=True, channel_id=10, ping="1234567890123456789", now=later
    )
    shift = repo.get_shift(two_guilds, 1)
    assert (shift.channel_id, shift.ping, shift.enabled_at) == (10, "1234567890123456789", T0)
    assert (shift.ping_day, shift.ping_count) == ("2026-09-30", 2)


def test_set_shift_re_enabling_after_a_disable_restamps_enabled_at(two_guilds):
    repo.set_shift(two_guilds, 1, enabled=True, channel_id=9, ping="none", now=_clock())
    repo.set_shift(
        two_guilds, 1, enabled=False, channel_id=9, ping="none", now=_clock(T0 + timedelta(days=1))
    )
    assert repo.get_shift(two_guilds, 1).enabled is False
    assert repo.get_shift(two_guilds, 1).enabled_at == T0
    repo.set_shift(
        two_guilds, 1, enabled=True, channel_id=9, ping="none", now=_clock(T0 + timedelta(days=2))
    )
    assert repo.get_shift(two_guilds, 1).enabled_at == T0 + timedelta(days=2)


def test_set_shift_disabled_first_insert_has_no_enabled_at(two_guilds):
    repo.set_shift(two_guilds, 1, enabled=False, channel_id=None, ping="none", now=_clock())
    shift = repo.get_shift(two_guilds, 1)
    assert (shift.enabled, shift.enabled_at, shift.channel_id) == (False, None, None)


def test_set_shift_rejects_enabled_without_channel_and_bad_ping(two_guilds):
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_shift(two_guilds, 1, enabled=True, channel_id=None, ping="none")
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_shift(two_guilds, 1, enabled=True, channel_id=5, ping="@everyone")
    assert repo.get_shift(two_guilds, 1) is None


# --- lounge ---


def _lounge(guild_id, **overrides):
    fields = {
        "guild_id": guild_id,
        "channel_id": 4242,
        "welcome_enabled": True,
        "welcome_message": "Welcome, {name}.",
        "quote_enabled": True,
        "quote_time": "08:00",
        "quote_sources": [{"kind": "wikiquote", "value": "Oscar Wilde"}],
        "last_quote_date": "2026-09-30",
    }
    fields.update(overrides)
    return LoungeSettings(**fields)


def test_lounge_round_trip(two_guilds):
    repo.upsert_lounge(two_guilds, _lounge(1))
    assert repo.get_lounge(two_guilds, 1) == _lounge(1)
    assert repo.get_lounge(two_guilds, 2) is None


def test_upsert_lounge_updates_settings_but_never_last_quote_date(two_guilds):
    repo.upsert_lounge(two_guilds, _lounge(1))
    repo.upsert_lounge(
        two_guilds,
        _lounge(
            1,
            welcome_enabled=False,
            quote_time="07:30",
            last_quote_date="1999-01-01",
            quote_sources=[{"kind": "file", "value": "q.txt"}],
        ),
    )
    lounge = repo.get_lounge(two_guilds, 1)
    assert (lounge.welcome_enabled, lounge.quote_time) == (False, "07:30")
    assert lounge.quote_sources == [{"kind": "file", "value": "q.txt"}]
    assert lounge.last_quote_date == "2026-09-30"


def test_list_lounges_returns_every_guild_in_order(two_guilds):
    repo.upsert_lounge(two_guilds, _lounge(2))
    repo.upsert_lounge(two_guilds, _lounge(1))
    assert [row.guild_id for row in repo.list_lounges(two_guilds)] == [1, 2]


def test_lounge_needs_a_guild_and_a_positive_channel(two_guilds):
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_lounge(two_guilds, _lounge(404))
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_lounge(two_guilds, _lounge(1, channel_id=0))


# --- notices ---


def test_notices_are_newest_first_and_per_guild(two_guilds):
    for i in range(3):
        repo.add_notice(two_guilds, 1, f"n{i}", now=_clock(T0 + timedelta(minutes=i)))
    repo.add_notice(two_guilds, 2, "other", now=_clock())
    assert [n.text for n in repo.recent_notices(two_guilds, 1)] == ["n2", "n1", "n0"]
    assert [n.text for n in repo.recent_notices(two_guilds, 1, limit=2)] == ["n2", "n1"]
    assert [n.text for n in repo.recent_notices(two_guilds, 2)] == ["other"]
    assert repo.recent_notices(two_guilds, 1)[0].created_at == T0 + timedelta(minutes=2)


def test_add_notice_prunes_to_the_newest_twenty_per_guild(two_guilds):
    repo.add_notice(two_guilds, 2, "keep me", now=_clock())
    for i in range(25):
        repo.add_notice(two_guilds, 1, f"n{i}", now=_clock(T0 + timedelta(minutes=i)))
    notices = repo.recent_notices(two_guilds, 1, limit=100)
    assert len(notices) == 20
    assert notices[0].text == "n24" and notices[-1].text == "n5"
    assert len(repo.recent_notices(two_guilds, 2)) == 1


def test_add_notice_same_timestamp_prunes_by_id(two_guilds):
    for i in range(22):
        repo.add_notice(two_guilds, 1, f"n{i}", now=_clock())
    texts = [n.text for n in repo.recent_notices(two_guilds, 1, limit=100)]
    assert len(texts) == 20 and texts[0] == "n21" and "n0" not in texts


def test_add_notice_truncates_long_text_and_refuses_empty(two_guilds):
    repo.add_notice(two_guilds, 1, "x" * 5000)
    assert len(repo.recent_notices(two_guilds, 1)[0].text) == 2000
    with pytest.raises(ValueError):
        repo.add_notice(two_guilds, 1, "")


def test_add_notice_for_an_unknown_guild_fails(conn):
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_notice(conn, 404, "hi")


# --- app_state ---


def test_app_state_get_set(conn):
    assert repo.app_state_get(conn, "k") is None
    repo.app_state_set(conn, "k", "v1")
    repo.app_state_set(conn, "k", "v2")
    repo.app_state_set(conn, "other", "x")
    assert repo.app_state_get(conn, "k") == "v2"
    assert repo.app_state_get(conn, "other") == "x"


# --- adopt_orphan_digests ---


def _orphan(conn, run_date):
    with conn:
        conn.execute(
            "INSERT INTO digests (run_date, status, created_at, updated_at) "
            "VALUES (?, 'ok', 'n', 'n')",
            (run_date,),
        )


def test_adopt_orphan_digests_only_for_the_imported_guild(two_guilds):
    _orphan(two_guilds, "2026-09-30")
    assert repo.adopt_orphan_digests(two_guilds, 1) == 0  # guild 1 wasn't imported
    repo.update_guild_settings(two_guilds, 1, set_up=True)
    with two_guilds:
        two_guilds.execute("UPDATE guilds SET imported_at = 'n' WHERE guild_id = 1")
    assert repo.adopt_orphan_digests(two_guilds, 2) == 0  # guild 2 isn't the imported one
    assert repo.adopt_orphan_digests(two_guilds, 1) == 1
    assert two_guilds.execute("SELECT guild_id FROM digests").fetchone()[0] == 1
    assert repo.adopt_orphan_digests(two_guilds, 1) == 0


def test_adopt_orphan_digests_skips_a_day_the_guild_already_has(conn):
    repo.create_guild(conn, 1, imported_at=T0)
    with conn:
        conn.execute(
            "INSERT INTO digests (guild_id, run_date, status, created_at, updated_at) "
            "VALUES (1, '2026-09-30', 'ok', 'n', 'n')"
        )
    _orphan(conn, "2026-09-30")
    _orphan(conn, "2026-09-29")
    assert repo.adopt_orphan_digests(conn, 1) == 1
    assert conn.execute("SELECT COUNT(*) FROM digests WHERE guild_id IS NULL").fetchone()[0] == 1


# --- items for free servers (D2) ---


def _store(conn, url, title, topics, collected_at, excerpt="An excerpt."):
    repo.store_items(
        conn,
        [
            StoredItem(
                url=url,
                title=title,
                excerpt=excerpt,
                source_name="Feed",
                trust="official",
                published_at=None,
                topics=topics,
            )
        ],
        now=_clock(collected_at),
    )


@pytest.fixture
def items(two_guilds):
    conn = two_guilds
    _store(conn, "https://e.com/1", "Borderlands 4 patch", {"bl4": False}, T0 - timedelta(hours=3))
    _store(
        conn, "https://e.com/2", "Palworld raid patch", {"palworld": False}, T0 - timedelta(hours=2)
    )
    _store(conn, "https://e.com/3", "Fortnite patch", {"fortnite": False}, T0 - timedelta(hours=1))
    _store(conn, "https://e.com/4", "Crossover patch", {"bl4": False, "fortnite": True}, T0)
    repo.follow_game(conn, 1, "bl4", 5)
    repo.follow_game(conn, 1, "palworld", 5)
    repo.follow_game(conn, 2, "fortnite", 5)
    return conn


def test_query_items_is_limited_to_followed_games_and_newest_first(items):
    found, total = repo.query_items(items, 1, [], SINCE, 10, 0)
    assert total == 3
    assert [i.title for i in found] == [
        "Crossover patch",
        "Palworld raid patch",
        "Borderlands 4 patch",
    ]
    found2, total2 = repo.query_items(items, 2, [], SINCE, 10, 0)
    assert total2 == 2
    assert [i.title for i in found2] == ["Crossover patch", "Fortnite patch"]


def test_query_items_shows_only_the_topics_the_guild_follows(items):
    crossover = repo.query_items(items, 1, [], SINCE, 10, 0)[0][0]
    assert crossover.topic_keys == [
        "bl4"
    ]  # it also matched fortnite; that isn't guild 1's business
    assert repo.query_items(items, 2, [], SINCE, 10, 0)[0][0].topic_keys == ["fortnite"]


def test_query_items_topic_filter_cannot_reach_unfollowed_games(items):
    found, total = repo.query_items(items, 1, ["palworld"], SINCE, 10, 0)
    assert (total, [i.title for i in found]) == (1, ["Palworld raid patch"])
    assert repo.query_items(items, 1, ["fortnite"], SINCE, 10, 0) == ([], 0)


def test_query_items_since_limit_offset_and_unfollowed_guild(items):
    assert repo.query_items(items, 1, [], T0 - timedelta(hours=2, minutes=30), 10, 0)[1] == 2
    page, total = repo.query_items(items, 1, [], SINCE, 1, 1)
    assert total == 3 and [i.title for i in page] == ["Palworld raid patch"]
    repo.create_guild(items, 3)
    assert repo.query_items(items, 3, [], SINCE, 10, 0) == ([], 0)


def test_query_items_returns_full_item_fields(items):
    item = repo.query_items(items, 1, ["bl4"], SINCE, 10, 1)[0][0]
    assert (item.url, item.trust, item.source_name, item.excerpt) == (
        "https://e.com/1",
        "official",
        "Feed",
        "An excerpt.",
    )
    assert item.published_at is None and item.collected_at == T0 - timedelta(hours=3)


def test_search_items_finds_titles_and_excerpts_within_followed_games(items):
    found, total = repo.search_items(items, 1, "patch", SINCE, 10, 0)
    assert total == 3 and {i.title for i in found} == {
        "Crossover patch",
        "Palworld raid patch",
        "Borderlands 4 patch",
    }
    assert {i.title for i in repo.search_items(items, 2, "patch", SINCE, 10, 0)[0]} == {
        "Crossover patch",
        "Fortnite patch",
    }
    assert repo.search_items(items, 2, "palworld", SINCE, 10, 0) == ([], 0)
    _store(items, "https://e.com/5", "Zebracorn", {"bl4": False}, T0, excerpt="stripes galore")
    assert repo.search_items(items, 1, "stripes", SINCE, 10, 0)[1] == 1


def test_search_items_empty_and_hostile_queries(items):
    assert repo.search_items(items, 1, "   ", SINCE, 10, 0) == ([], 0)
    for hostile in [
        '"',
        "patch OR",
        "NEAR(",
        "*",
        "a AND b",
        "'; DROP TABLE items; #",
        "title:patch",
    ]:
        repo.search_items(items, 1, hostile, SINCE, 10, 0)  # must not raise
    assert items.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4


def test_search_items_follows_deletes_through_the_fts_trigger(items):
    assert repo.search_items(items, 1, "palworld", SINCE, 10, 0)[1] == 1
    repo.purge_older_than(items, T0 - timedelta(hours=2, minutes=30))
    assert repo.search_items(items, 1, "borderlands", SINCE, 10, 0)[1] == 0
    assert repo.search_items(items, 1, "palworld", SINCE, 10, 0)[1] == 1
