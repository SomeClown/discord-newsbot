"""Adversarial tests for `/news recent`, `/news search` and `/shift codes` (plan task 9, D2).

`test_member_commands_scoped.py` shows that a server sees the games it follows. This
file is about the ways a server could see more: an item tagged with both its game and
somebody else's, a search string built to cause trouble, an empty follow list that
SQL would happily read as "everything", a page number that's a little too enthusiastic.
Every test runs a real command callback against a real temp-file SQLite.

Two things in here are documented rather than fixed, because they're the owner's call:
the collector's web-search items land in the shared `items` table, so a free server
following the same game sees them in `/news`; and a comped server's pager keeps the
game list it had when the command ran, so unfollowing mid-browse doesn't take effect
until the next command. Both are commented where they're pinned.

No gateway, no network.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from discord.app_commands import Choice
from v3_fakes import GUILD_A, GUILD_B, FakeInteraction, command, make_guild

from newsbot.bot.commands import (
    make_member_news_group,
    make_member_shift_group,
)
from newsbot.bot.format import discord_len
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import StoredItem

NOW = datetime.now(UTC)
RECENT = NOW - timedelta(minutes=1)


def put_items(db_path, *rows, source="Feed", trust="official"):
    """Store `(url, title, [topics])` items, collected a minute ago."""
    with closing(connect(db_path)) as conn:
        repo.store_items(
            conn,
            [
                StoredItem(
                    url, title, "zebracorn excerpt", source, trust, None, dict.fromkeys(t, False)
                )
                for url, title, t in rows
            ],
            now=lambda: RECENT,
        )


def put_story(db_path, topic_key, headline, summary="STORYBODY zebracorn", label="official"):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO digests (id, run_date, status, created_at, updated_at) "
            "VALUES (1, '2026-09-01', 'ok', 'n', 'n')"
        )
        conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, digest_id, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (topic_key, headline, summary, label, NOW.isoformat()),
        )


def news(v3_cfg, db, name):
    return command(make_member_news_group(v3_cfg, db), name).callback


def embeds(interaction):
    return [m["embed"] for m in interaction.sent if m.get("embed") is not None]


def shown(interaction):
    """Every embed's title and description, joined."""
    return "\n".join(f"{e.title}\n{e.description}" for e in embeds(interaction))


# --- D2 and what a free server can see ---


async def test_an_item_tagged_with_a_followed_and_an_unfollowed_game_names_only_the_followed_one(
    v3_cfg, v3_db
):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    make_guild(v3_db, GUILD_B, games=[("rust", 2)])
    put_items(v3_db, ("https://e.example/both", "Crossover news", ["borderlands4", "rust"]))
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    text = shown(interaction)
    assert "Crossover news" in text and "Borderlands 4" in text
    assert "Rust" not in text  # the item is A's to see; the tag that isn't A's is not


async def test_searching_for_another_servers_game_by_name_finds_nothing(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    put_items(v3_db, ("https://e.example/rust", "Rust quokka update", ["rust"]))
    for tier in ("free", "comped"):
        with closing(connect(v3_db)) as conn:
            repo.update_guild_settings(conn, GUILD_A, tier=tier)
        put_story(v3_db, "rust", "Rust quokka story")
        interaction = FakeInteraction(guild_id=GUILD_A)
        await news(v3_cfg, v3_db, "search")(interaction, query="quokka")
        assert "quokka" not in shown(interaction).replace("/news search: quokka", "")


async def test_web_search_items_reach_free_servers_through_the_shared_table(v3_cfg, v3_db):
    # Documented, not judged: D1 makes Brave a comped-only *cost*, but what it collects
    # lands in the shared `items` table like everything else, and `/news` has no notion
    # of where an item came from. A free server that follows the game reads them.
    make_guild(v3_db, GUILD_A, tier="free", games=[("borderlands4", 1)])
    put_items(
        v3_db,
        ("https://e.example/brave", "Found by Brave zebracorn", ["borderlands4"]),
        source="Brave Search",
        trust="press",
    )
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    assert "Found by Brave" in shown(interaction)


async def test_a_free_server_sees_no_stories_even_when_stories_exist_for_its_game(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, tier="free", games=[("borderlands4", 1)])
    put_story(v3_db, "borderlands4", "Premium-only story")
    for name, args in (("recent", {}), ("search", {"query": "zebracorn"})):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await news(v3_cfg, v3_db, name)(interaction, **args)
        assert "Premium-only" not in shown(interaction) and "STORYBODY" not in shown(interaction)


# --- the empty follow list: SQL would read it as "all" ---


@pytest.mark.parametrize("tier", ["free", "comped"])
@pytest.mark.parametrize("game", ["all", "ALL", "borderlands4", ""])
async def test_a_server_that_follows_nothing_never_reaches_the_query(v3_cfg, v3_db, tier, game):
    make_guild(v3_db, GUILD_A, tier=tier)
    make_guild(v3_db, GUILD_B, games=[("borderlands4", 2)])
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    put_story(v3_db, "borderlands4", "BL4 story")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction, game=game)
    assert "doesn't follow any games" in interaction.text
    assert not embeds(interaction)


@pytest.mark.parametrize("tier", ["free", "comped"])
async def test_search_from_a_server_that_follows_nothing_finds_nothing(v3_cfg, v3_db, tier):
    make_guild(v3_db, GUILD_A, tier=tier)
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    put_story(v3_db, "borderlands4", "BL4 story")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "search")(interaction, query="zebracorn")
    assert not embeds(interaction)


async def test_unfollowing_everything_takes_the_news_away_at_once(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    with closing(connect(v3_db)) as conn:
        repo.unfollow_game(conn, GUILD_A, "borderlands4")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    assert not embeds(interaction)


def test_the_repo_reads_an_empty_key_list_differently_for_stories_and_search(v3_db):
    # The trap the handler's guard exists for, pinned at the repo so nobody "tidies" it:
    # `query_stories([])` means All, `search_stories(topic_keys=[])` means none.
    put_story(v3_db, "rust", "Rust story")
    since = NOW - timedelta(days=1)
    with closing(connect(v3_db)) as conn:
        assert repo.query_stories(conn, [], since, None, 10, 0)[1] == 1
        assert repo.search_stories(conn, "zebracorn", since, 10, 0, topic_keys=[]) == ([], 0)
        assert repo.search_stories(conn, "zebracorn", since, 10, 0, topic_keys=None)[1] == 1
        assert repo.search_stories(conn, "zebracorn", since, 10, 0, topic_keys=["palworld"])[1] == 0


# --- search strings ---

HOSTILE = [
    "",
    " ",
    "\t\n",
    '"',
    '""',
    "zebracorn OR rust",
    "zebracorn AND NOT rust",
    "NEAR(zebracorn rust)",
    "zebracorn*",
    "^zebracorn",
    "title:zebracorn",
    "{title}:zebracorn",
    "zebracorn -rust",
    "(",
    ")",
    "*",
    "'; DROP TABLE items; --",
    "\ud800",
    "zebracorn\ud800",
    "\x00",
    "zebracorn\x00rust",
    "a" * 100,
    "a" * 10_000,
    "\U0001f600" * 5000,
    "@everyone <@&1> <#2> [x](https://evil.example)",
    "‮evil",
]


@pytest.mark.parametrize("tier", ["free", "comped"])
@pytest.mark.parametrize("query", HOSTILE, ids=[repr(q[:12]) for q in HOSTILE])
async def test_hostile_search_strings_never_crash_leak_or_overflow(v3_cfg, v3_db, tier, query):
    make_guild(v3_db, GUILD_A, tier=tier, games=[("borderlands4", 1)])
    make_guild(v3_db, GUILD_B, games=[("rust", 2)])
    put_items(
        v3_db,
        ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]),
        ("https://e.example/rust", "Rust zebracorn", ["rust"]),
    )
    put_story(v3_db, "borderlands4", "BL4 story")
    put_story(v3_db, "rust", "Rust story")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "search")(interaction, query=query)
    sent = embeds(interaction)
    assert len(sent) == 1
    embed = sent[0]
    assert discord_len(embed.title) <= 256
    assert discord_len(embed.description or "") <= 4096
    assert "Rust" not in (embed.description or "")  # B's game, whatever the query
    assert interaction.sent[-1]["allowed_mentions"].everyone is False
    assert interaction.sent[-1]["ephemeral"] is True
    if "@everyone" in query:
        assert "@everyone" not in embed.title
    with closing(connect(v3_db)) as conn:  # nothing was dropped
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2


@pytest.mark.parametrize("query", ["", " ", "\t", '""', "*", "()"])
async def test_a_query_with_nothing_searchable_returns_no_results_not_everything(
    v3_cfg, v3_db, query
):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "search")(interaction, query=query)
    assert "BL4 zebracorn" not in shown(interaction)


async def test_an_fts_operator_is_searched_as_a_word_not_obeyed(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "search")(interaction, query="zebracorn OR nonexistent")
    assert "BL4 zebracorn" not in shown(
        interaction
    )  # "OR" was a word, so nothing matched all three


# --- rendering hostile stored text ---


async def test_six_enormous_hostile_headlines_still_fit_one_embed_page(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    nasty = "@everyone <@&1> [click](https://evil.example) **bold** " + "\U0001f600" * 2000
    put_items(
        v3_db,
        *[(f"https://e.example/{i}", f"{nasty} {i}", ["borderlands4"]) for i in range(12)],
    )
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    (embed,) = embeds(interaction)
    assert discord_len(embed.description) <= 4096
    assert "@everyone" not in embed.description
    assert "https://evil.example" not in embed.description
    assert interaction.sent[-1]["allowed_mentions"].roles is False


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "https://e.example/" + "a" * 3000,
        "https://e.example/>injected",
        "https://e.example/ spaced",
    ],
)
async def test_an_unusable_stored_url_is_dropped_rather_than_rendered(v3_cfg, v3_db, url):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    with closing(connect(v3_db)) as conn, conn:
        # Bypass store_items' own URL checks: this is "what if the table already has it".
        conn.execute(
            "INSERT INTO items (url, title, excerpt, source_name, trust, collected_at) "
            "VALUES (?, 'Sneaky headline', 'x', 'Feed', 'official', ?)",
            (url, RECENT.isoformat()),
        )
        conn.execute(
            "INSERT INTO item_topics (item_id, topic_key, uncertain) "
            "VALUES (last_insert_rowid(), 'borderlands4', 0)"
        )
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    text = shown(interaction)
    assert "javascript:" not in text and "file:" not in text and "data:" not in text
    assert ">injected" not in text


async def test_comped_stories_with_hostile_text_stay_defused_and_inside_the_limit(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 1)])
    nasty = "@everyone <@&1> [click](https://evil.example) " + "\U0001f600" * 3000
    for i in range(10):
        put_story(v3_db, "borderlands4", f"{nasty} {i}", summary=nasty)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    (embed,) = embeds(interaction)
    assert discord_len(embed.description) <= 4096 and discord_len(embed.title) <= 256
    assert "@everyone" not in embed.description
    assert "https://evil.example" not in embed.description


async def test_a_nonsense_label_on_a_comped_server_finds_nothing_and_breaks_nothing(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 1)])
    put_story(v3_db, "borderlands4", "BL4 story")
    for value in ("nonsense", "' OR 1=1 --", "official\x00"):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await news(v3_cfg, v3_db, "recent")(interaction, label=Choice(name="x", value=value))
        assert "BL4 story" not in shown(interaction)


# --- paging ---


def _view_of(interaction):
    return next(m["view"] for m in interaction.sent if m.get("view") is not None)


def _fill(db, n, *, title="BL4 headline"):
    put_items(db, *[(f"https://e.example/{i}", f"{title} {i}", ["borderlands4"]) for i in range(n)])


async def test_the_pager_walks_every_page_and_shows_each_item_once(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    _fill(v3_db, 14)  # page size 6: three pages
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    view = _view_of(interaction)
    assert view.total_pages == 3
    seen = set(shown(interaction).split("BL4 headline ")[1:])
    for _ in range(2):
        click = FakeInteraction(guild_id=GUILD_A)
        await view.next_button.callback(click)
        seen |= {
            t.split()[0]
            for t in click.response.edits[-1]["embed"].description.split("BL4 headline ")[1:]
        }
    assert len({s.split()[0] for s in seen}) == 14


@pytest.mark.parametrize("page", [0, -1, -10_000, 10**9, 10**18])
async def test_a_forged_page_number_renders_an_empty_or_first_page_and_never_crashes(
    v3_cfg, v3_db, page
):
    # Discord disables Prev on page 1 and Next on the last page, but a forged component
    # interaction doesn't have to care. `render_page` must survive any page number.
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    make_guild(v3_db, GUILD_B, games=[("rust", 2)])
    _fill(v3_db, 8)
    put_items(v3_db, ("https://e.example/rust", "Rust secret", ["rust"]))
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    view = _view_of(interaction)
    view.page = page - 1
    click = FakeInteraction(guild_id=GUILD_A)
    await view.next_button.callback(click)
    edit = click.response.edits[-1]
    assert "Rust secret" not in (edit["embed"].description or "")
    assert edit["allowed_mentions"].everyone is False


async def test_a_stranger_cannot_turn_someone_elses_pages(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    _fill(v3_db, 14)
    interaction = FakeInteraction(guild_id=GUILD_A, user_id=1)
    await news(v3_cfg, v3_db, "recent")(interaction)
    view = _view_of(interaction)
    stranger = FakeInteraction(guild_id=GUILD_A, user_id=2)
    assert await view.interaction_check(stranger) is False
    assert "Only the requester" in stranger.text
    assert view.page == 1


async def test_a_comped_pager_keeps_the_game_list_it_started_with(v3_cfg, v3_db):
    # Documented: the pager's query was bound to the followed games at command time, so
    # unfollowing mid-browse doesn't hide page 2 of a game the server has since dropped.
    # It's the requester's own ephemeral message and lasts ten minutes, so I think it's
    # fine; it is the one place a follow change isn't immediate.
    make_guild(v3_db, GUILD_A, tier="comped", games=[("borderlands4", 1), ("palworld", 2)])
    for i in range(8):
        put_story(v3_db, "palworld", f"Palworld story {i}")
    interaction = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(interaction)
    view = _view_of(interaction)
    with closing(connect(v3_db)) as conn:
        repo.unfollow_game(conn, GUILD_A, "palworld")
    click = FakeInteraction(guild_id=GUILD_A)
    await view.next_button.callback(click)
    assert "Palworld story" in click.response.edits[-1]["embed"].description
    fresh = FakeInteraction(guild_id=GUILD_A)
    await news(v3_cfg, v3_db, "recent")(fresh)
    assert "No stories found" in shown(fresh)


# --- DMs and the not-set-up reply, for every member command ---


@pytest.mark.parametrize(
    ("name", "args"),
    [("recent", {}), ("recent", {"game": "borderlands4"}), ("search", {"query": "zebracorn"})],
)
async def test_news_in_a_dm_is_refused_and_reads_nothing(v3_cfg, v3_db, name, args):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    interaction = FakeInteraction(guild_id=None)
    await news(v3_cfg, v3_db, name)(interaction, **args)
    assert interaction.text == "This only works inside a server."
    assert not embeds(interaction)


async def test_shift_codes_in_a_dm_is_refused(v3_cfg, v3_db):
    interaction = FakeInteraction(guild_id=None)
    await command(make_member_shift_group(v3_cfg, v3_db), "codes").callback(interaction)
    assert interaction.text == "This only works inside a server."


async def test_a_server_with_no_setup_sees_none_of_bs_news(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, set_up=False, games=[("borderlands4", 1)])
    make_guild(v3_db, GUILD_B, games=[("borderlands4", 2)])
    put_items(v3_db, ("https://e.example/bl4", "BL4 zebracorn", ["borderlands4"]))
    for name, args in (("recent", {}), ("search", {"query": "zebracorn"})):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await news(v3_cfg, v3_db, name)(interaction, **args)
        assert "hasn't set up newsbot yet" in interaction.text and not embeds(interaction)


# --- /shift codes ---


def _code(db_path, code="AAAAA-AAAAA-AAAAA-AAAAA-AAAA1"):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, status, "
            "from_roundup) VALUES (?, ?, 'Feed', 'https://e.example/c', 'posted', 0)",
            (code, NOW.isoformat()),
        )


def codes(v3_cfg, db):
    return command(make_member_shift_group(v3_cfg, db), "codes").callback


async def test_with_no_shift_games_configured_any_followed_game_unlocks_codes(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"shift": v3_cfg.shift.model_copy(update={"games": []})})
    make_guild(v3_db, GUILD_A, games=[("palworld", 1)])
    _code(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await codes(cfg, v3_db)(interaction)
    assert "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1" in shown(interaction)


async def test_with_no_shift_games_and_no_followed_games_the_reply_says_so(v3_cfg, v3_db):
    cfg = v3_cfg.model_copy(update={"shift": v3_cfg.shift.model_copy(update={"games": []})})
    make_guild(v3_db, GUILD_A)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await codes(cfg, v3_db)(interaction)
    assert interaction.text == "This server doesn't follow any games yet."


async def test_shift_codes_for_a_server_that_follows_nothing_gets_a_clean_refusal(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A)
    _code(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await codes(v3_cfg, v3_db)(interaction)
    assert interaction.text == "This server doesn't follow Borderlands 4."
    assert not embeds(interaction)


async def test_shift_codes_show_no_server_specific_data_only_the_shared_list(v3_cfg, v3_db):
    # A code is a code: the list is global by design. What must not ride along is anything
    # about which server was pinged, where, or how often.
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    make_guild(v3_db, GUILD_B, games=[("borderlands4", 2)])
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_B, enabled=True, channel_id=777000000000000003, ping="everyone")
    _code(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await codes(v3_cfg, v3_db)(interaction)
    text = shown(interaction)
    assert str(GUILD_B) not in text and "777000000000000003" not in text
    assert interaction.sent[-1]["ephemeral"] is True
    assert interaction.sent[-1]["allowed_mentions"].everyone is False


def test_the_member_group_autocomplete_value_is_short_enough_for_discord(v3_cfg, v3_db):
    # Choice values cap at 100 characters; every catalog key and name must fit.
    for game in v3_cfg.catalog:
        assert 1 <= len(game.key) <= 100 and 1 <= len(game.name) <= 100


async def test_the_recent_game_autocomplete_survives_hostile_input(v3_cfg, v3_db):
    make_guild(v3_db, GUILD_A, games=[("borderlands4", 1)])
    make_guild(v3_db, GUILD_B, games=[("rust", 2)])
    autocomplete = (
        command(make_member_news_group(v3_cfg, v3_db), "recent")._params["game"].autocomplete
    )
    for current in ("", "%", "\ud800", "x" * 10_000, "RUST", "rust", "\x00", "ALL"):
        got = await autocomplete(FakeInteraction(guild_id=GUILD_A), current)
        assert len(got) <= 25
        assert all(c.value in ("all", "borderlands4") for c in got)
