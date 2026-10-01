"""Day one for the friend: what the v3 cutover does to a server that never asked for it.

Everything else in the v3 pile can be wrong in an interesting way and nobody but me will
notice. This one has a witness: about fifty people who got a digest at 09:00 yesterday and
have opinions about getting it twice, or not at all, or with the @everyone ping spent a
second time. So each test here starts from the populated v2.2 database in `conftest.py` plus
the prod-like config, runs the same startup steps `__main__` runs, and then asks the
question the friend would ask if they could see the plumbing: did anything change?

The shape is "pick a moment v2.2 could have been replaced at, then watch the channels":
upgrade after the digest already went out (before and after 09:00 Pacific), at 08:58 so the
first v3 tick lands on the due minute, over a v2.2 row that failed cleanly, failed halfway or
died pending, with SHiFT codes v2.2 already announced, with the ping budget half spent or
fully spent, and with the quote already posted. The fake channels record every send, so
"nothing changed" is an assertion about silence, which is the hardest kind to get wrong by
accident.

A few of these pin behavior I'd rather not have found (the strict xfails); each says why.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cutover_world import (
    BL4_CH,
    D4_CH,
    DUE,
    FRIEND,
    FRIEND_ADMIN_CH,
    LOUNGE_CH,
    PAL_CH,
    SHIFT_CH,
    TODAY,
    add_item,
    code_item,
    collect_these,
    fixture_collectors,
    v22_digest,
    write_fixture_items,
)

import newsbot.bot.client as client_module
from newsbot.store.db import connect

DIGEST_CHANNELS = (BL4_CH, PAL_CH, D4_CH)
WELCOME = "Welcome to {server}, {member}. Pull up a stool; the lighting is questionable."


def digest_posts(world) -> int:
    return sum(len(world.sent(ch)) for ch in DIGEST_CHANNELS)


def sql(db_path: str, statement: str, *params) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(statement, params)


# --- the digest ---


@pytest.mark.parametrize("status", ["ok", "partial"])
@pytest.mark.parametrize(
    "cutover",
    [
        datetime(2026, 10, 1, 16, 5, tzinfo=UTC),  # 09:05, right after v2.2 posted
        datetime(2026, 10, 1, 17, 30, tzinfo=UTC),  # 10:30, the "after 09:00" case
        datetime(2026, 10, 2, 6, 59, tzinfo=UTC),  # 23:59, still the same Pacific day
    ],
)
async def test_a_day_v22_already_posted_gets_no_second_digest(make_world, v22_db, status, cutover):
    v22_digest(v22_db, TODAY, status, posted_ids="[301, 302]")
    world = await make_world(now=cutover)
    add_item(world.db_path, "borderlands4", "fresh", cutover - timedelta(minutes=20))

    await world.ready()
    for minute in range(4):
        await world.tick(cutover + timedelta(minutes=minute))

    assert digest_posts(world) == 0
    rows = world.rows("SELECT guild_id FROM digests WHERE run_date = ?", TODAY)
    assert rows == [(FRIEND,)]  # the v2.2 row was adopted, and nothing was added beside it


async def test_the_first_v3_digest_is_tomorrows_and_it_posts_once(make_world, v22_db):
    v22_digest(v22_db, TODAY, "ok", posted_ids="[301, 302]")
    world = await make_world(now=datetime(2026, 10, 1, 17, 30, tzinfo=UTC))
    tomorrow_due = DUE + timedelta(days=1)
    for game, slug in (("borderlands4", "b"), ("palworld", "p")):
        add_item(world.db_path, game, slug, tomorrow_due - timedelta(hours=1))
    await world.ready()

    await world.tick(tomorrow_due - timedelta(minutes=1))
    assert digest_posts(world) == 0
    await world.tick(tomorrow_due)
    await world.tick(tomorrow_due + timedelta(minutes=1))
    await world.tick(tomorrow_due + timedelta(minutes=2))

    assert [len(world.sent(ch)) for ch in DIGEST_CHANNELS] == [1, 1, 0]
    rows = world.rows("SELECT status FROM digests WHERE run_date = '2026-10-02'")
    assert len(rows) == 1 and rows[0][0] in ("ok", "partial")


async def test_an_upgrade_at_0858_posts_exactly_once_at_0900(make_world):
    world = await make_world(now=DUE - timedelta(minutes=2))
    for game, slug in (("borderlands4", "b"), ("palworld", "p")):
        add_item(world.db_path, game, slug, DUE - timedelta(hours=2))
    await world.ready()

    await world.tick(DUE - timedelta(minutes=2))
    await world.tick(DUE - timedelta(seconds=1))
    assert digest_posts(world) == 0
    await world.tick(DUE)
    await world.tick(DUE + timedelta(minutes=1))
    await world.tick(DUE + timedelta(minutes=30))

    assert [len(world.sent(ch)) for ch in DIGEST_CHANNELS] == [1, 1, 0]
    assert len(world.sent(FRIEND_ADMIN_CH)) >= 1  # the run report goes to the admin channel
    rows = world.rows("SELECT guild_id FROM digests WHERE run_date = ?", TODAY)
    assert rows == [(FRIEND,)]


async def test_a_restart_after_the_digest_does_not_post_it_again(make_world):
    # The deploy window says never 09:00 to 09:15, but a crash loop doesn't read the docs.
    world = await make_world(now=DUE - timedelta(minutes=2))
    add_item(world.db_path, "palworld", "p", DUE - timedelta(hours=2))
    await world.ready()
    await world.tick(DUE)
    assert len(world.sent(PAL_CH)) == 1

    # A new process over the same database and the same Discord: first ready, first ticks.
    again = await make_world(now=DUE + timedelta(minutes=1), channels=world.channels)
    await again.ready()
    await again.tick(DUE + timedelta(minutes=2))
    await again.tick(DUE + timedelta(minutes=3))

    assert len(again.sent(PAL_CH)) == 1


@pytest.mark.parametrize(
    ("when", "retried"),
    [
        (datetime(2026, 10, 1, 16, 5, tzinfo=UTC), False),  # 5 minutes after the failure
        (datetime(2026, 10, 1, 17, 30, tzinfo=UTC), True),  # long enough ago
    ],
)
async def test_a_clean_v22_failure_is_retried_as_v22_would_have_retried_it(
    make_world, v22_db, when, retried
):
    # v2.2's `should_catch_up` ran a `failed` row with nothing posted again. v3's schedule does
    # the same, ten minutes after the failure, so a late cutover is a retry rather than a skip.
    v22_digest(v22_db, TODAY, "failed", notes="summarize failed")
    world = await make_world(now=when)
    add_item(world.db_path, "palworld", "p", datetime(2026, 10, 1, 16, 0, 30, tzinfo=UTC))
    await world.ready()

    await world.tick(when)
    await world.tick(when + timedelta(minutes=1))

    assert len(world.sent(PAL_CH)) == (1 if retried else 0)


async def test_a_v22_failure_that_got_something_out_is_not_retried_by_itself(make_world, v22_db):
    # v2.2 alerted and waited for an admin here, because a retry would double-post the part
    # that landed. v3 must never auto-post over it either.
    v22_digest(v22_db, TODAY, "failed", posted_ids="[301]", notes="discord send failed")
    world = await make_world(now=datetime(2026, 10, 1, 17, 30, tzinfo=UTC))
    add_item(world.db_path, "palworld", "p", datetime(2026, 10, 1, 16, 0, 30, tzinfo=UTC))
    await world.ready()

    await world.tick(datetime(2026, 10, 1, 17, 30, tzinfo=UTC))
    await world.tick(datetime(2026, 10, 1, 18, 30, tzinfo=UTC))

    assert digest_posts(world) == 0


async def test_a_v22_pending_row_is_never_posted_over(make_world, v22_db):
    v22_digest(v22_db, TODAY, "pending")
    world = await make_world(now=datetime(2026, 10, 1, 17, 30, tzinfo=UTC))
    add_item(world.db_path, "palworld", "p", datetime(2026, 10, 1, 16, 0, 30, tzinfo=UTC))
    await world.ready()

    for minutes in (0, 1, 15, 90):
        await world.tick(datetime(2026, 10, 1, 17, 30, tzinfo=UTC) + timedelta(minutes=minutes))

    assert digest_posts(world) == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "v2.2 told the admin channel when it found a digest left pending or half-posted at "
        "startup. v3 skips such a row, correctly, but says nothing to anyone, so the friend's "
        "digest just doesn't arrive until somebody notices"
    ),
)
@pytest.mark.parametrize(("status", "posted_ids"), [("pending", "[]"), ("failed", "[301]")])
async def test_a_stuck_v22_row_is_reported_to_somebody(make_world, v22_db, status, posted_ids):
    v22_digest(v22_db, TODAY, status, posted_ids=posted_ids, notes="v2.2 stopped here")
    world = await make_world(now=datetime(2026, 10, 1, 17, 30, tzinfo=UTC), owner_channel=True)
    await world.ready()
    await world.tick(datetime(2026, 10, 1, 17, 31, tzinfo=UTC))

    told = [m.content for m in world.everything_sent() if m.content]
    notices = [row[0] for row in world.rows("SELECT text FROM guild_notices")]
    # The import notice and the fixture's pending SHiFT code also land here; the question is
    # whether anything is about the digest.
    about_it = [t for t in told + notices if "digest" in t.casefold()]
    assert about_it, (told, notices)


async def test_a_collection_pass_collects_and_never_publishes(make_world, tmp_path, monkeypatch):
    # The hourly pass that starts two minutes after boot must not be a second way to post.
    world = await make_world(now=datetime(2026, 10, 1, 17, 30, tzinfo=UTC))
    day = write_fixture_items(
        tmp_path / "pass",
        "palworld",
        [
            {
                "url": "https://example.com/palworld/pass",
                "title": "Palworld v0.6.3 Patch Notes",
                "excerpt": "Fixes a few crashes.",
                "source_name": "Palworld Steam",
                "trust": "official",
                "published_at": "2026-10-01T17:00:00+00:00",
                "topics": ["palworld"],
            }
        ],
    )
    monkeypatch.setattr(client_module, "build_collection_collectors", fixture_collectors(day))

    await world.bot._collection_job()

    assert digest_posts(world) == 0
    assert world.rows("SELECT COUNT(*) FROM items WHERE url LIKE '%palworld/pass'") == [(1,)]


async def test_the_friends_home_guild_and_owner_channel_are_the_friends_own_server(make_world):
    # With the prod-like config (guild_id and admin_channel_id, no home_guild_id), the "owner's
    # channel" is the friend's admin channel and `/owner` lives in the friend's server. That's
    # what v2.2 did with its health messages, but it's worth pinning, because the new daily
    # owner report and the import notice will now show up in front of the friend too.
    world = await make_world(now=DUE)

    assert world.cfg.home_guild_id == FRIEND
    assert world.cfg.admin_channel_id == FRIEND_ADMIN_CH
    await world.ready()
    owner_text = [m.content for m in world.sent(FRIEND_ADMIN_CH) if m.content]
    assert any("Imported the v2 setup" in text for text in owner_text)


# --- the lounge ---


async def test_the_quote_v22_already_posted_today_is_not_posted_again(make_world, v22_db):
    # `lounge_state.last_quote_date` is the one cell v2.2 and the import share: the import
    # copies it into the friend's lounge row, and the 08:00 job reads it from there.
    sql(v22_db, "UPDATE lounge_state SET value = ? WHERE key = 'last_quote_date'", TODAY)
    world = await make_world(now=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))  # 08:00 PDT

    assert world.rows("SELECT last_quote_date FROM guild_lounge") == [(TODAY,)]
    outcome = await world.bot.run_guild_quote(FRIEND, False)

    assert outcome.status == "already_posted"
    assert world.sent(LOUNGE_CH) == []


async def test_a_restart_after_the_quote_does_not_repeat_it(make_world, v22_db):
    sql(v22_db, "UPDATE lounge_state SET value = ? WHERE key = 'last_quote_date'", TODAY)
    world = await make_world(now=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    await world.ready()
    again = await make_world(now=datetime(2026, 10, 1, 15, 3, tzinfo=UTC), channels=world.channels)
    await again.ready()

    outcome = await again.bot.run_guild_quote(FRIEND, False)

    assert outcome.status == "already_posted"
    assert again.sent(LOUNGE_CH) == []


async def test_the_quote_job_is_scheduled_on_the_friends_own_clock(make_world):
    world = await make_world(now=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))

    await world.ready()

    job = world.bot.scheduler.get_job(f"daily-quote-{FRIEND}")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("8", "0")
    assert str(job.trigger.timezone) == "America/Los_Angeles"
    assert (job.misfire_grace_time, job.coalesce, job.max_instances) == (300, True, 1)


def _member(*, guild_id: int = FRIEND, user_id: int = 777, pending: bool = False):
    guild = SimpleNamespace(id=guild_id, name="The Friend's Server")
    return SimpleNamespace(
        guild=guild,
        id=user_id,
        bot=False,
        pending=pending,
        mention=f"<@{user_id}>",
        flags=SimpleNamespace(value=0),
    )


async def test_the_welcome_comes_from_the_imported_row_and_pings_only_the_new_member(make_world):
    world = await make_world(now=DUE)
    assert world.bot.intents.members is True  # welcome on in the imported row
    member = _member()

    await world.bot.on_member_join(member)

    ((sent,),) = [world.sent(LOUNGE_CH)]
    assert sent.content == WELCOME.format(server="The Friend's Server", member="<@777>")
    mentions = sent.allowed_mentions
    assert mentions.everyone is False and mentions.roles is False
    assert mentions.users == [member]


async def test_a_welcome_needs_the_servers_own_lounge_row(make_world):
    world = await make_world(now=DUE)

    await world.bot.on_member_join(_member(guild_id=424242))  # some other server

    assert world.sent(LOUNGE_CH) == []


async def test_the_welcome_switched_off_in_config_is_off_after_the_resync(make_world, tmp_path):
    from cutover_world import PRODLIKE

    from newsbot.config import load_config

    edited = tmp_path / "welcome-off.yaml"
    edited.write_text(
        PRODLIKE.read_text().replace("enabled: true\n    message", "enabled: false\n    message")
    )
    world = await make_world(now=DUE, cfg=load_config(edited))

    assert world.bot.intents.members is False
    await world.bot.on_member_join(_member())
    assert world.sent(LOUNGE_CH) == []


# --- SHiFT ---

NEW_CODE = "NEWAA-NEWAA-NEWAA-NEWAA-NEWA1"
SECOND_CODE = "NEWBB-NEWBB-NEWBB-NEWBB-NEWB2"


T_SHIFT = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)  # 08:00 Pacific


async def test_codes_v22_already_handled_are_never_alerted_again(
    make_world, codes, monkeypatch, tmp_path
):
    # One item per code v2.2 knew about, in every status it could have left one in, all fresh,
    # all from an official source. `pending` becomes `failed` at setup; `seeded`, `too_old` and
    # `roundup` were never posted anywhere; `posted` and `failed` were. None may post now.
    world = await make_world(now=T_SHIFT)
    items = [code_item(status, code) for status, code in codes.items()]
    collect_these(monkeypatch, tmp_path, items)

    await world.bot._collection_job()

    assert world.sent(SHIFT_CH) == []
    statuses = dict(world.rows("SELECT code, status FROM alerted_codes"))
    assert set(statuses) == set(codes.values())  # and no second row for any of them


async def test_a_new_code_is_alerted_once_even_across_two_passes(
    make_world, codes, monkeypatch, tmp_path
):
    world = await make_world(now=T_SHIFT)
    collect_these(monkeypatch, tmp_path, [code_item("new", NEW_CODE)])

    await world.bot._collection_job()
    await world.bot._collection_job()

    (alert,) = world.sent(SHIFT_CH)
    assert NEW_CODE in alert.content


@pytest.mark.parametrize(
    ("ping_day", "ping_count", "pings"),
    [
        (TODAY, 3, False),  # the budget v2.2 spent this morning stays spent
        (TODAY, 2, True),  # one left
        ("2026-09-30", 3, True),  # yesterday's budget is yesterday's
    ],
)
async def test_the_ping_budget_v22_spent_carries_over(
    make_world, v22_db, monkeypatch, tmp_path, ping_day, ping_count, pings
):
    sql(v22_db, "UPDATE alert_state SET value = ? WHERE key = 'ping_day'", ping_day)
    sql(v22_db, "UPDATE alert_state SET value = ? WHERE key = 'ping_count'", str(ping_count))
    world = await make_world(now=T_SHIFT)
    collect_these(monkeypatch, tmp_path, [code_item("new", NEW_CODE)])

    await world.bot._collection_job()

    (alert,) = world.sent(SHIFT_CH)
    assert alert.allowed_mentions.everyone is pings
    assert alert.content.startswith("@everyone") is pings


async def test_the_last_ping_of_the_day_is_spent_once_and_the_next_code_is_quiet(
    make_world, v22_db, monkeypatch, tmp_path
):
    sql(v22_db, "UPDATE alert_state SET value = ? WHERE key = 'ping_day'", TODAY)
    sql(v22_db, "UPDATE alert_state SET value = '2' WHERE key = 'ping_count'")
    world = await make_world(now=T_SHIFT)
    collect_these(monkeypatch, tmp_path, [code_item("one", NEW_CODE)], name="first")
    await world.bot._collection_job()
    collect_these(monkeypatch, tmp_path, [code_item("two", SECOND_CODE)], name="second")

    world.set_now(T_SHIFT + timedelta(hours=1))
    await world.bot._collection_job()

    first, second = world.sent(SHIFT_CH)
    assert first.allowed_mentions.everyone is True
    assert second.allowed_mentions.everyone is False and not second.content.startswith("@everyone")
    assert world.rows("SELECT ping_count FROM guild_shift") == [(3,)]


async def test_a_code_alert_never_comes_back_after_a_restart(make_world, monkeypatch, tmp_path):
    world = await make_world(now=T_SHIFT)
    collect_these(monkeypatch, tmp_path, [code_item("new", NEW_CODE)])
    await world.ready()
    await world.bot._collection_job()
    assert len(world.sent(SHIFT_CH)) == 1

    again = await make_world(now=T_SHIFT + timedelta(minutes=5), channels=world.channels)
    await again.ready()  # startup delivery of anything still queued
    await again.bot._collection_job()

    assert len(again.sent(SHIFT_CH)) == 1


# --- what the first v3 digest is made of ---


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A first v3 window is (due - 24h, due], and v2.2 collected the items of its last "
        "digest a few seconds after that digest's due time, so every one of them falls "
        "inside it. The first v3 summary hands the model, and so the friend, yesterday's "
        "news again (v2.2 itself only ever summarized items it had not stored before)"
    ),
)
async def test_the_first_v3_summary_does_not_refeed_items_v22_already_posted(make_world, v22_db):
    # v2.2 collects inside its own run, so the items of its 09:00 digest are stamped a few
    # seconds after 09:00. A story from that digest cites one.
    v22_digest(v22_db, TODAY, "ok", posted_ids="[301]")
    posted = add_item(v22_db, "palworld", "already-posted", DUE + timedelta(seconds=25))
    with closing(connect(v22_db)) as conn, conn:
        story = conn.execute(
            "INSERT INTO stories (topic_key, headline, summary, label, is_update_of, digest_id, "
            "created_at) VALUES ('palworld', 'Posted by v2.2', 'x', 'official', NULL, "
            "(SELECT id FROM digests WHERE run_date = ?), ?)",
            (TODAY, DUE.isoformat()),
        ).lastrowid
        conn.execute(
            "INSERT INTO story_items (story_id, item_id) SELECT ?, id FROM items WHERE url = ?",
            (story, posted),
        )
    world = await make_world(now=datetime(2026, 10, 1, 17, 30, tzinfo=UTC))
    tomorrow_due = DUE + timedelta(days=1)
    fresh = add_item(world.db_path, "palworld", "fresh", tomorrow_due - timedelta(hours=1))
    await world.ready()

    await world.tick(tomorrow_due)

    assert fresh in world.llm.urls_seen()
    assert posted not in world.llm.urls_seen()


async def test_an_upgrade_in_the_last_minutes_before_nine_posts_a_reduced_coverage_footer(
    make_world,
):
    # Pinned, and the one thing the friend can see change on day one. The prepare job (which
    # searches the web, half an hour ahead) only gets its first turn five minutes after
    # startup, so a start at 08:58 has no summary ready at 09:00 and the digest makes one
    # inline, which never searches. v2.2 searched inline. The post is right, and says so.
    world = await make_world(now=DUE - timedelta(minutes=2), brave_key="a-brave-key")
    for game in ("borderlands4", "palworld"):
        add_item(world.db_path, game, "b", DUE - timedelta(hours=2))
    await world.ready()

    await world.tick(DUE)

    footer = world.sent(PAL_CH)[0].embed.footer.text
    assert "Reduced coverage today: web search skipped" in footer
    assert world.rows("SELECT status FROM digests WHERE run_date = ?", TODAY) == [("partial",)]


async def test_an_upgrade_a_few_minutes_earlier_gets_the_prepare_job_and_a_clean_digest(
    make_world, monkeypatch
):
    # The same morning at 08:50: the prepare job's first turn (08:55) comes before 09:00.
    from types import SimpleNamespace

    import newsbot.pipeline.summaries as summaries_module

    async def fake_search(deps, game_keys, key, *, sleep):
        return SimpleNamespace(sources_total=1)

    monkeypatch.setattr(summaries_module, "collect_web_search", fake_search)
    world = await make_world(now=DUE - timedelta(minutes=10), brave_key="a-brave-key")
    for game in ("borderlands4", "palworld"):
        add_item(world.db_path, game, "b", DUE - timedelta(hours=2))
    await world.ready()

    world.set_now(DUE - timedelta(minutes=5))  # the prepare job's first interval tick
    await world.bot._summaries_job()
    await world.tick(DUE)

    assert not world.sent(PAL_CH)[0].embed.footer.text
    assert world.rows("SELECT status FROM digests WHERE run_date = ?", TODAY) == [("ok",)]
