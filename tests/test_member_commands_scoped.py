"""`/news recent`, `/news search` and `/shift codes` for the public app (plan task 9, D2).

The one rule under all of it: a server sees only the games it follows, whatever the
`game` option, the query or a forged interaction says. Real temp-file SQLite, real
command callbacks, hand-written interactions.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from discord.app_commands import Choice
from v3_fakes import GUILD_A, GUILD_B, FakeInteraction, command, make_guild

from newsbot.bot import commands as commands_module
from newsbot.bot.commands import (
    _clamp_page,
    _search_items_sync,
    game_choices,
    make_member_news_group,
    make_member_shift_group,
    resolve_game,
)
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import StoredItem

NOW = datetime.now(UTC)
LABEL_OFFICIAL = Choice(name="official", value="official")


def _items(db_path, *rows):
    """Store `(url, title, [topics])` items, collected a minute ago."""
    with closing(connect(db_path)) as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url,
                    title,
                    "zebracorn excerpt",
                    "Feed",
                    "official",
                    None,
                    dict.fromkeys(topics, False),
                )
                for url, title, topics in rows
            ],
            now=lambda: NOW - timedelta(minutes=1),
        )


def _story(db_path, topic_key, headline):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO digests (id, run_date, status, created_at, updated_at) "
            "VALUES (1, '2026-09-01', 'ok', 'n', 'n')"
        )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES (?, ?, 'STORYBODY zebracorn', 'official', 1, ?)",
            (topic_key, headline, NOW.isoformat()),
        )


@pytest.fixture
def world(v3_db):
    """A follows borderlands4 (free), B follows palworld (free); items for three games."""
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 11)])
    make_guild(v3_db, GUILD_B, games=[("palworld", 22)])
    _items(
        v3_db,
        ("https://e.example/bl4", "BL4 zebracorn headline", ["borderlands4"]),
        ("https://e.example/pal", "Palworld zebracorn headline", ["palworld"]),
        ("https://e.example/rust", "Rust zebracorn headline", ["rust"]),
    )
    return v3_db


def news(v3_cfg, db, name):
    return command(make_member_news_group(v3_cfg, db), name).callback


def embed_text(interaction):
    embeds = [m["embed"] for m in interaction.sent if m.get("embed") is not None]
    assert len(embeds) == 1, interaction.sent
    return f"{embeds[0].title}\n{embeds[0].description}"


# --- not set up, nothing followed ---


@pytest.mark.parametrize("name", ["recent", "search"])
async def test_news_before_setup_says_so(v3_cfg, v3_db, name):
    args = {"query": "x"} if name == "search" else {}
    interaction = FakeInteraction(guild_id=GUILD_A)  # no row at all
    await news(v3_cfg, v3_db, name)(interaction, **args)
    assert "hasn't set up newsbot yet" in interaction.text
    make_guild(v3_db, GUILD_A, set_up=False, games=[("borderlands4", 1)])
    interaction = FakeInteraction(guild_id=GUILD_A)  # a row, but setup isn't done
    await news(v3_cfg, v3_db, name)(interaction, **args)
    assert "hasn't set up newsbot yet" in interaction.text


async def test_news_for_a_server_that_follows_nothing(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    assert "doesn't follow any games" in interaction.text


async def test_news_outside_a_server(v3_cfg, v3_db):
    interaction = FakeInteraction(guild_id=None)
    await news(v3_cfg, v3_db, "recent")(interaction)
    assert "inside a server" in interaction.text


# --- D2: free servers see item headlines ---


async def test_free_recent_shows_only_followed_item_headlines(v3_cfg, world):
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, world, "recent")(interaction)
    text = embed_text(interaction)
    assert "BL4 zebracorn headline" in text
    assert "Palworld zebracorn" not in text and "Rust zebracorn" not in text
    assert "Borderlands 4" in text  # the game's name, not its key


async def test_each_server_reads_only_its_own_games(v3_cfg, world):
    a, b = FakeInteraction(guild_id=GUILD_A), FakeInteraction(guild_id=GUILD_B)
    await news(v3_cfg, world, "recent")(a)
    await news(v3_cfg, world, "recent")(b)
    assert "BL4" in embed_text(a) and "Palworld" not in embed_text(a)
    assert "Palworld" in embed_text(b) and "BL4" not in embed_text(b)


@pytest.mark.parametrize("game", ["palworld", "rust", "' OR 1=1 --", "", "PALWORLD", "%"])
async def test_a_crafted_game_value_never_reaches_an_unfollowed_game(v3_cfg, world, game):
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, world, "recent")(interaction, game=game)
    assert "doesn't follow that game" in interaction.text
    assert not [m for m in interaction.sent if m.get("embed")]


async def test_recent_accepts_a_followed_game_by_key_or_name(v3_cfg, world):
    for value in ("borderlands4", "Borderlands 4"):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await news(v3_cfg, world, "recent")(interaction, game=value)
        assert "BL4 zebracorn" in embed_text(interaction)


async def test_free_search_finds_followed_items_only(v3_cfg, world):
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, world, "search")(interaction, query="zebracorn")
    text = embed_text(interaction)
    assert "BL4 zebracorn" in text and "Palworld" not in text and "Rust" not in text


@pytest.mark.parametrize(
    "query",
    [
        '"',
        "zebracorn OR palworld",
        "NEAR(a b)",
        "zebracorn*",
        "' OR 1=1 --",
        "'; DROP TABLE items; --",
        "\ud800",  # a lone surrogate
        "@everyone <#1> [x](https://evil.example)",
        "a" * 100,
    ],
)
async def test_hostile_search_strings_are_harmless_and_defused(v3_cfg, world, query):
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, world, "search")(interaction, query=query)
    embed = [m for m in interaction.sent if m.get("embed")][0]["embed"]
    assert "Palworld zebracorn" not in (embed.description or "")
    if "@everyone" in query:
        assert "@everyone" not in embed.title  # defused with a zero-width space
        assert "<#1>" not in embed.title and "https://evil" not in embed.title
    with closing(connect(world)) as conn:  # the table is still there
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3


async def test_responses_carry_no_mentions_and_follow_the_public_flag(v3_cfg, world):
    private, public = FakeInteraction(guild_id=GUILD_A), FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, world, "recent")(private)
    await news(v3_cfg, world, "recent")(public, public=True)
    assert private.response.deferred_ephemeral is True
    assert public.response.deferred_ephemeral is False
    for interaction in (private, public):
        assert interaction.sent[-1]["allowed_mentions"].everyone is False
        assert interaction.sent[-1]["allowed_mentions"].roles is False


async def test_a_label_on_a_free_server_is_explained_not_silently_dropped(v3_cfg, world):
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, world, "recent")(interaction, label=LABEL_OFFICIAL)
    assert "premium" in interaction.text
    assert "BL4 zebracorn" in embed_text_all(interaction)


def embed_text_all(interaction):
    return "\n".join(
        (m["embed"].description or "") for m in interaction.sent if m.get("embed") is not None
    )


# --- D2: comped servers keep stories ---


async def test_comped_recent_and_search_show_stories_of_followed_games_only(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 11)])
    _story(v3_db, "borderlands4", "BL4 story")
    _story(v3_db, "palworld", "Palworld story")
    _items(v3_db, ("https://e.example/bl4", "BL4 item headline zebracorn", ["borderlands4"]))
    recent, search = FakeInteraction(guild_id=GUILD_A), FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(recent)
    await news(v3_cfg, v3_db, "search")(search, query="zebracorn")
    for interaction in (recent, search):
        text = embed_text(interaction)
        assert "BL4 story" in text and "STORYBODY" in text
        assert "Palworld story" not in text and "item headline" not in text


async def test_comped_crafted_game_value_is_refused(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 11)])
    _story(v3_db, "palworld", "Palworld story")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction, game="palworld")
    assert "doesn't follow that game" in interaction.text


async def test_comped_label_filter_still_works(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 11)])
    _story(v3_db, "borderlands4", "BL4 story")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction, label=Choice(name="rumor", value="rumor"))
    assert "No stories found" in embed_text(interaction)


# --- clamping ---


@pytest.mark.parametrize(
    ("limit", "offset", "want"),
    [(-1, -5, (1, 0)), (0, 0, (1, 0)), (6, 12, (6, 12)), (10**9, 3, (25, 3))],
)
def test_clamp_page(limit, offset, want):
    assert _clamp_page(limit, offset) == want


def test_a_negative_limit_cannot_unbound_the_item_search(world):
    # repo.search_items would hand SQLite LIMIT -1 ("no limit") as is; the command's
    # wrapper clamps first.
    with closing(connect(world)) as conn:
        for i in range(30):
            repo.store_items(
                conn,
                [
                    StoredItem(
                        f"https://e.example/{i}",
                        "zebracorn",
                        "x",
                        "F",
                        "press",
                        None,
                        {"borderlands4": False},
                    )
                ],
            )
    rows, total = _search_items_sync(world, GUILD_A, "zebracorn", NOW - timedelta(days=1), -1, 0)
    assert len(rows) == 1 and total >= 30


# --- autocomplete and lookup helpers ---


def test_game_choices_filter_by_keys_and_substring_and_cap_at_25(v3_cfg):
    catalog = v3_cfg.catalog
    everything = game_choices(catalog, [g.key for g in catalog], "")
    assert [c.value for c in everything] == ["borderlands4", "palworld", "rust"]
    assert [c.value for c in game_choices(catalog, ["palworld", "rust"], "PAL")] == ["palworld"]
    assert [c.value for c in game_choices(catalog, ["palworld"], "borderlands")] == []
    with_all = game_choices(catalog, ["rust"], "", include_all=True)
    assert [c.value for c in with_all] == ["all", "rust"]
    many = [type("G", (), {"key": f"g{i}", "name": f"Game {i}"})() for i in range(40)]
    assert len(game_choices(many, [g.key for g in many], "game")) == 25


def test_resolve_game_only_finds_allowed_keys(v3_cfg):
    catalog = v3_cfg.catalog
    assert resolve_game(catalog, ["palworld"], "palworld").key == "palworld"
    assert resolve_game(catalog, ["palworld"], " Palworld ").key == "palworld"
    assert resolve_game(catalog, ["palworld"], "rust") is None
    assert resolve_game(catalog, [], "palworld") is None


async def test_the_recent_game_autocomplete_lists_only_followed_games_and_all(v3_cfg, world):
    group = make_member_news_group(v3_cfg, world)
    autocomplete = command(group, "recent")._params["game"].autocomplete
    got = await autocomplete(FakeInteraction(guild_id=GUILD_A), "")
    assert [c.value for c in got] == ["all", "borderlands4"]
    got = await autocomplete(FakeInteraction(guild_id=GUILD_B), "")
    assert [c.value for c in got] == ["all", "palworld"]
    assert await autocomplete(FakeInteraction(guild_id=None), "") == []


# --- /shift codes ---


def _code(db_path, code="AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status, "
            "from_roundup) VALUES (?, ?, 'Feed', 'https://e.example/c', 'posted', 0)",
            (code, NOW.isoformat()),
        )


def shift_codes(v3_cfg, db):
    return command(make_member_shift_group(v3_cfg, db), "codes").callback


async def test_shift_codes_before_setup(v3_cfg, v3_db):
    interaction = FakeInteraction(guild_id=GUILD_A)
    await shift_codes(v3_cfg, v3_db)(interaction)
    assert "hasn't set up newsbot yet" in interaction.text


async def test_shift_codes_needs_a_followed_shift_game(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("palworld", 11)])
    _code(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await shift_codes(v3_cfg, v3_db)(interaction)
    assert interaction.text == "This server doesn't follow Borderlands 4."
    assert not [m for m in interaction.sent if m.get("embed")]


async def test_shift_codes_lists_the_shared_codes_for_a_bl4_server(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 11)], timezone="America/Chicago")
    _code(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await shift_codes(v3_cfg, v3_db)(interaction)
    assert "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1" in embed_text(interaction)
    assert interaction.sent[-1]["allowed_mentions"].everyone is False


async def test_shift_codes_survives_a_broken_stored_time_zone(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 11)])
    with closing(connect(v3_db)) as conn:
        repo.update_guild_settings(conn, GUILD_A, timezone="Mars/Olympus")
    _code(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await shift_codes(v3_cfg, v3_db)(interaction)
    assert "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1" in embed_text(interaction)


def test_the_member_groups_are_guild_only_and_guild_installed(v3_cfg, v3_db):
    for group in (make_member_news_group(v3_cfg, v3_db), make_member_shift_group(v3_cfg, v3_db)):
        assert group.guild_only is True
        assert group.allowed_installs.guild is True and group.allowed_installs.user is False
        assert group.default_permissions is None  # members, not admins


def test_the_v2_factories_are_gone_from_the_commands_module():
    # They were kept "for the running bot until the cutover" (this test used to say the
    # opposite). The cutover is done, and a stray import of one should fail loudly.
    for name in ("make_news_group", "make_admin_group", "make_shift_group"):
        assert not hasattr(commands_module, name)
