"""`/shift codes` (current codes) and `/shift history` through the real handlers (design.md §16).

Real temp-file SQLite, the real command callbacks, hand-written interactions, and a clock the
test owns (`make_member_shift_group(..., now=...)`). `test_commands_shift_codes_handler.py`
has the long-standing `/shift history` window, DST and paging tests; this file is about what
`/shift codes` means now: the codes that should still work, grouped, filtered, paged by size.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from pydantic import SecretStr
from v3_fakes import GUILD_A, FakeInteraction, command
from v3_fakes import make_guild as _make_guild

from newsbot.bot.client import NewsBot
from newsbot.bot.commands import make_member_shift_group
from newsbot.bot.registration import setup_commands
from newsbot.config import Secrets, ShiftTableSource
from newsbot.shift.details import is_current
from newsbot.store import repo
from newsbot.store.db import connect
from newsbot.store.models import CodeDetailsRow

LA = ZoneInfo("America/Los_Angeles")
# 11:00 in Los Angeles on Friday, October 9, 2026 (PDT, UTC-7).
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
TODAY = date(2026, 10, 9)
CHANNEL = 777000000000000001


def _code(n: int) -> str:
    return f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA"


class Clock:
    """A settable `now`, because the boundary tests need to step it a microsecond at a time."""

    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _guild(db_path: str, *, alerts: bool = True) -> None:
    _make_guild(db_path, GUILD_A, games=[("borderlands4", 11)])
    if alerts:
        with closing(connect(db_path)) as conn:
            repo.set_shift(conn, GUILD_A, enabled=True, channel_id=CHANNEL, ping="none")


def _seen(
    db_path: str,
    n: int,
    *,
    first_seen_at: datetime = NOW - timedelta(days=1),
    status: str = "posted",
    from_roundup: bool = False,
) -> str:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO alerted_codes "
            "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
            "VALUES (?, ?, 'Feed', 'https://example.com/post', ?, ?, 0)",
            (_code(n), first_seen_at.astimezone(UTC).isoformat(), status, int(from_roundup)),
        )
    return _code(n)


def _details(db_path: str, n: int, **kwargs) -> None:
    row = CodeDetailsRow(
        code=_code(n),
        clock_start=kwargs.pop("clock_start", NOW - timedelta(days=1)),
        updated_at=NOW,
        **kwargs,
    )
    with closing(connect(db_path)) as conn, conn:
        repo._write_details(conn, row)


def _known(db_path: str, n: int, **kwargs) -> str:
    """A released code with a details row; `kwargs` are the row's fields."""
    seen = _seen(db_path, n)
    _details(db_path, n, **kwargs)
    return seen


def _codes(cfg, db_path, clock=None):
    return command(make_member_shift_group(cfg, db_path, now=clock or Clock()), "codes").callback


def _history(cfg, db_path, clock=None):
    return command(make_member_shift_group(cfg, db_path, now=clock or Clock()), "history").callback


def make_bot(cfg, db_path):
    """A `NewsBot` whose `tree.sync` records the scope it was asked for (None is global)."""
    secrets = Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("test-anthropic-key"),
        brave_api_key=None,
        bluesky_handle=None,
        bluesky_app_password=None,
    )
    bot = NewsBot(cfg, secrets, db_path)
    bot.synced = []

    async def sync(*, guild=None):
        bot.synced.append(guild.id if guild is not None else None)
        return []

    bot.tree.sync = sync
    return bot


def _embed(interaction):
    [call] = [m for m in interaction.sent if m.get("embed") is not None]
    return call["embed"]


def _shows(interaction, code: str) -> bool:
    return code in (_embed(interaction).description or "")


# --- groups, order, filter ---


async def test_groups_run_golden_cosmetic_permanent_other_with_newest_first(v3_cfg, v3_db):
    _guild(v3_db)
    far = TODAY + timedelta(days=30)
    _known(v3_db, 1, category="golden", expires_on=far, added_on=date(2026, 10, 1))
    _known(v3_db, 2, category="golden", expires_on=far, added_on=date(2026, 10, 5))
    _known(v3_db, 3, category="cosmetic", expires_on=far)
    _known(v3_db, 4, category="permanent", origin="Launch")
    _seen(v3_db, 5)  # no details at all: Other
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    text = _embed(interaction).description
    headings = [text.index(h) for h in ("`[golden keys]`", "`[cosmetics]`", "`[permanent]`")]
    assert headings == sorted(headings)
    assert text.index("`[other]`") > headings[-1]
    assert text.index(_code(2)) < text.index(_code(1))  # added Oct 5 beats added Oct 1
    assert text.index(_code(1)) < text.index("`[cosmetics]`")
    assert text.index(_code(3)) < text.index("`[permanent]`")
    assert text.index(_code(4)) < text.index("`[other]`") < text.index(_code(5))


async def test_the_type_option_keeps_one_group(v3_cfg, v3_db):
    _guild(v3_db)
    far = TODAY + timedelta(days=30)
    _known(v3_db, 1, category="golden", expires_on=far)
    _known(v3_db, 2, category="cosmetic", expires_on=far)
    _known(v3_db, 3, category="permanent")
    _seen(v3_db, 4)
    for kind, wanted in (("golden", 1), ("cosmetic", 2), ("permanent", 3)):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await _codes(v3_cfg, v3_db)(interaction, type=kind)
        shown = {n for n in (1, 2, 3, 4) if _shows(interaction, _code(n))}
        assert shown == {wanted}, kind
    everything = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(everything, type="all")
    assert all(_shows(everything, _code(n)) for n in (1, 2, 3, 4))


async def test_type_defaults_to_all_and_a_stray_value_means_all(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1)
    for args in ({}, {"type": "nonsense"}):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await _codes(v3_cfg, v3_db)(interaction, **args)
        assert _shows(interaction, _code(1))


async def test_an_entry_has_the_reward_and_the_expiry_phrase(v3_cfg, v3_db):
    _guild(v3_db)
    _known(
        v3_db, 1, category="golden", reward="3 Golden Keys", expires_on=TODAY + timedelta(days=5)
    )
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert (
        f"```\n{_code(1)}\n```\n`reward` 3 Golden Keys · `expires` Oct 14 (in 5 days)"
        in _embed(interaction).description
    )


# --- the footer and the empty page ---


async def test_the_footer_names_gearbox_and_the_table_hosts(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1)
    mentalmars = ShiftTableSource(
        type="shift_table",
        name="mentalmars",
        url="https://mentalmars.com/game-news/borderlands-4-shift-codes/",
        trust="community",
    )
    game = v3_cfg.catalog[0]
    cfg = v3_cfg.model_copy(
        update={
            "catalog": [game.model_copy(update={"sources": [*game.sources, mentalmars]})]
            + list(v3_cfg.catalog[1:])
        }
    )
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(cfg, v3_db)(interaction)

    assert _embed(interaction).footer.text == (
        "Codes from Gearbox and mentalmars.com · redeem at shift.gearboxsoftware.com · Page 1 of 1"
    )


async def test_without_a_table_source_the_footer_claims_only_gearbox(v3_cfg, v3_db):
    _guild(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert _embed(interaction).footer.text == (
        "Codes from Gearbox · redeem at shift.gearboxsoftware.com · Page 1 of 1"
    )


async def test_an_empty_list_points_at_the_alert_channel(v3_cfg, v3_db):
    _guild(v3_db)
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert _embed(interaction).description == (
        f"No current codes. I'll post new ones in <#{CHANNEL}> when they show up."
    )


async def test_an_empty_list_leaves_the_channel_out_when_alerts_are_off(v3_cfg, v3_db):
    _guild(v3_db, alerts=False)
    interaction = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(interaction)
    assert _embed(interaction).description == "No current codes."

    # Saved, then switched off: the channel is still in the row, and still not mentioned.
    with closing(connect(v3_db)) as conn:
        repo.set_shift(conn, GUILD_A, enabled=False, channel_id=CHANNEL, ping="none")
    again = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(again)
    assert _embed(again).description == "No current codes."


async def test_an_empty_filtered_list_says_which_kind_is_missing(v3_cfg, v3_db):
    _guild(v3_db)
    _known(v3_db, 1, category="cosmetic", expires_on=TODAY + timedelta(days=3))
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction, type="golden")

    assert _embed(interaction).description.startswith("No current golden codes.")


# --- what counts as current ---


async def test_the_expiry_day_is_current_until_pacific_midnight(v3_cfg, v3_db):
    _guild(v3_db)
    _known(v3_db, 1, category="golden", expires_on=TODAY)
    last_instant = datetime(2026, 10, 10, 7, 0, tzinfo=UTC) - timedelta(microseconds=1)
    for moment, expected in (
        (last_instant, True),
        (last_instant + timedelta(microseconds=1), False),
    ):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await _codes(v3_cfg, v3_db, Clock(moment))(interaction)
        assert _shows(interaction, _code(1)) is expected, moment


async def test_the_clock_ends_at_180_days_for_a_row_and_for_a_code_with_no_row(v3_cfg, v3_db):
    _guild(v3_db)
    start = NOW - timedelta(days=180)
    _seen(v3_db, 1, first_seen_at=start + timedelta(seconds=1))  # no details row
    _seen(v3_db, 2, first_seen_at=start)
    _seen(v3_db, 3, first_seen_at=NOW - timedelta(days=400))
    _details(v3_db, 3, clock_start=start + timedelta(seconds=1))  # the row's clock wins
    _seen(v3_db, 4, first_seen_at=NOW - timedelta(days=1))
    _details(v3_db, 4, clock_start=start)  # a row whose clock has just run out
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert {n for n in (1, 2, 3, 4) if _shows(interaction, _code(n))} == {1, 3}


async def test_a_permanent_code_never_goes_away(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1, first_seen_at=NOW - timedelta(days=1000))
    _details(
        v3_db,
        1,
        category="permanent",
        clock_start=NOW - timedelta(days=1000),
        expires_on=date(2020, 1, 1),  # a stray date on a permanent code means nothing
        origin="Launch day",
    )
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert (
        f"```\n{_code(1)}\n```\n`reward` unknown · `expires` never · Launch day"
        in _embed(interaction).description
    )


async def test_a_known_expiry_outlives_the_180_day_clock(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1, first_seen_at=NOW - timedelta(days=400))
    _details(v3_db, 1, clock_start=NOW - timedelta(days=400), expires_on=TODAY + timedelta(days=1))
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert _shows(interaction, _code(1))


async def test_pending_and_failed_codes_never_show(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1, status="pending")
    _seen(v3_db, 2, status="failed")
    for status, n in (("seeded", 3), ("too_old", 4), ("roundup", 5), ("posted", 6)):
        _seen(v3_db, n, status=status, from_roundup=status == "roundup")
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    # QA batch 1, fix 5: a code that was only ever recorded quietly (seeded, too_old, roundup)
    # and that nothing vouches for stays off the list; the posted one is listed as before.
    assert {n for n in range(1, 7) if _shows(interaction, _code(n))} == {6}


async def test_quiet_codes_show_once_something_vouches_for_them(v3_cfg, v3_db):
    _guild(v3_db)
    for status, n in (("seeded", 3), ("too_old", 4), ("roundup", 5)):
        _seen(v3_db, n, status=status, from_roundup=status == "roundup")
    _details(v3_db, 4, reward="Cosmetic", reward_source="table", reward_from="mentalmars")
    with closing(connect(v3_db)) as conn:
        repo.record_code_sightings(conn, [(_code(3), "Gearbox", True, False)])
        repo.record_code_sightings(
            conn, [(_code(5), "Reddit", False, False), (_code(5), "Bluesky", False, False)]
        )
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    assert {n for n in range(3, 6) if _shows(interaction, _code(n))} == {3, 4, 5}


def test_current_codes_agrees_with_is_current_at_every_boundary(v3_db):
    """The SQL and `shift.details.is_current` are two spellings of one rule; hold them together."""
    start = NOW - timedelta(days=180)
    cases: list[tuple[datetime, CodeDetailsRow | None]] = []
    for offset in (-1, 0, 1):
        delta = timedelta(microseconds=offset)
        cases.append((start + delta, None))  # no row: first_seen_at + 180 days
        cases.append((NOW, CodeDetailsRow("x", clock_start=start + delta, updated_at=NOW)))
        end = datetime.combine(TODAY + timedelta(days=1), datetime.min.time(), tzinfo=LA)
        cases.append((NOW, CodeDetailsRow("x", NOW, NOW, expires_on=TODAY)))
        cases.append((end.astimezone(UTC) + delta, None))
    cases.append(
        (NOW, CodeDetailsRow("x", NOW, NOW, category="permanent", expires_on=date(2020, 1, 1)))
    )

    boundary_nows = [
        NOW,
        datetime(2026, 10, 10, 7, 0, tzinfo=UTC) - timedelta(microseconds=1),
        datetime(2026, 10, 10, 7, 0, tzinfo=UTC),
    ]
    with closing(connect(v3_db)) as conn:
        for index, (seen, row) in enumerate(cases):
            code = _code(index)
            with conn:
                conn.execute(
                    "INSERT INTO alerted_codes "
                    "(code, first_seen_at, source_name, item_url, status, from_roundup, pinged) "
                    "VALUES (?, ?, 'Feed', 'https://example.com/p', 'posted', 0, 0)",
                    (code, seen.astimezone(UTC).isoformat()),
                )
                if row is not None:
                    repo._write_details(conn, CodeDetailsRow(**{**row.__dict__, "code": code}))
        for now in boundary_nows:
            wanted = set()
            for index, (seen, _row) in enumerate(cases):
                code = _code(index)
                stored = repo.code_details_for(conn, [code]).get(code)
                if is_current(stored, seen, now=now):
                    wanted.add(code)
            assert {c.code for c in repo.current_codes(conn, now=now)} == wanted, now


# --- gating, no cooldown, mentions ---


async def test_gating_is_unchanged(v3_cfg, v3_db):
    unset = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(unset)
    assert "hasn't set up newsbot yet" in unset.text

    _make_guild(v3_db, GUILD_A, games=[("palworld", 11)])
    elsewhere = FakeInteraction(guild_id=GUILD_A)
    await _codes(v3_cfg, v3_db)(elsewhere)
    assert elsewhere.text == "This server doesn't follow Borderlands 4."

    dm = FakeInteraction(guild_id=None)
    await _codes(v3_cfg, v3_db)(dm)
    assert dm.text == "This only works inside a server."


async def test_there_is_no_cooldown(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1)
    for _ in range(5):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await _codes(v3_cfg, v3_db)(interaction)
        assert _shows(interaction, _code(1))


async def test_public_posts_openly_and_every_send_has_mentions_off(v3_cfg, v3_db):
    _guild(v3_db)
    far = TODAY + timedelta(days=30)
    for n in range(40):
        _known(v3_db, n, category="golden", reward="A" * 300, expires_on=far)
    for public in (False, True):
        interaction = FakeInteraction(guild_id=GUILD_A)
        await _codes(v3_cfg, v3_db)(interaction, public=public)
        assert interaction.response.deferred_ephemeral is (not public)
        [sent] = interaction.sent
        assert sent["ephemeral"] is (not public)
        assert sent["allowed_mentions"].everyone is False
        assert sent["allowed_mentions"].users is False and sent["allowed_mentions"].roles is False

    view = sent["view"]
    assert view.total_pages > 1
    page = FakeInteraction(guild_id=GUILD_A)
    await view.next_button.callback(page)
    assert page.response.edits[-1]["allowed_mentions"].everyone is False


async def test_long_entries_page_by_size_and_never_split(v3_cfg, v3_db):
    _guild(v3_db)
    far = TODAY + timedelta(days=30)
    count = 30
    for n in range(count):
        _known(v3_db, n, category="cosmetic", reward="*" * 150, expires_on=far)  # 300 shown
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    [sent] = interaction.sent
    view = sent["view"]
    assert view.total_pages >= 3
    embeds = [sent["embed"]]
    for _ in range(view.total_pages - 1):
        page = FakeInteraction(guild_id=GUILD_A)
        await view.next_button.callback(page)
        embeds.append(page.response.edits[-1]["embed"])
    seen = []
    for embed in embeds:
        assert len(embed.description) <= 4096
        assert embed.description.count("`") % 2 == 0
        seen += [n for n in range(count) if _code(n) in embed.description]
    assert sorted(seen) == list(range(count))  # every code, once
    assert "(continued)]`" in embeds[1].description  # the heading comes back on the next page
    assert embeds[-1].footer.text.endswith(f"Page {len(embeds)} of {len(embeds)}")


async def test_hostile_reward_text_cannot_ping(v3_cfg, v3_db):
    _guild(v3_db)
    _known(
        v3_db,
        1,
        category="golden",
        reward="@everyone <@&123456789012345678> [x](http://evil.example)",
        expires_on=TODAY + timedelta(days=3),
    )
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _codes(v3_cfg, v3_db)(interaction)

    description = _embed(interaction).description
    assert "@everyone" not in description.replace("@\u200beveryone", "")
    assert "<@&" not in description


# --- /shift history with details ---


async def test_history_marks_expired_codes_and_wins_over_the_other_markers(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1, status="too_old")
    _details(v3_db, 1, reward="1 Golden Key", expires_on=TODAY - timedelta(days=1))
    _seen(v3_db, 2, status="too_old")
    _seen(v3_db, 3, from_roundup=True)
    _details(v3_db, 3, reward="Cosmetic", expires_on=TODAY - timedelta(days=2))
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history(v3_cfg, v3_db)(interaction)

    text = _embed(interaction).description
    blocks = {n: b for n in (1, 2, 3) for b in text.split("\n\n") if _code(n) in b}
    assert blocks[1].endswith("· expired") and "old post" not in blocks[1]
    assert blocks[2].endswith("· old post")
    assert blocks[3].endswith("· expired") and "from a roundup" not in blocks[3]
    # The details line sits right under the fence, and the marker has already said "expired",
    # so the line doesn't say it again (QA batch 1, fix 16).
    assert blocks[1].split("\n")[3] == "`reward` 1 Golden Key"
    assert "1 Golden Key ·" not in blocks[1]
    assert blocks[1].count("expired") == 1
    assert blocks[3].split("\n")[3] == "`reward` Cosmetic" and blocks[3].count("expired") == 1


async def test_history_is_the_old_list_with_its_own_title_and_gating(v3_cfg, v3_db):
    _guild(v3_db)
    _seen(v3_db, 1, first_seen_at=NOW - timedelta(days=20))
    _seen(v3_db, 2, first_seen_at=NOW - timedelta(days=2))
    interaction = FakeInteraction(guild_id=GUILD_A)

    await _history(v3_cfg, v3_db)(interaction, days=14)

    embed = _embed(interaction)
    assert embed.title == "> SHiFT code history ▌"
    assert embed.color.value == 0x3FE37F
    assert _code(2) in embed.description and _code(1) not in embed.description

    wrong = FakeInteraction(guild_id=GUILD_A)
    _make_guild(v3_db, 300000000000000009, games=[("palworld", 1)])
    wrong.guild_id = 300000000000000009
    await _history(v3_cfg, v3_db)(wrong)
    assert wrong.text == "This server doesn't follow Borderlands 4."


# --- registration: the hash changes, and the sync sends once ---


async def test_the_new_shift_tree_changes_the_hash_and_syncs_once(v3_cfg, v3_db, monkeypatch):
    import newsbot.bot.registration as registration

    def old_shift_group(cfg, db_path):
        group = app_commands.Group(
            name="shift",
            description="SHiFT codes the bot has seen.",
            guild_only=True,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        )

        @group.command(name="codes", description="List known SHiFT codes.")
        async def codes(
            interaction: discord.Interaction,
            days: app_commands.Range[int, 1, 90] = 14,
            public: bool = False,
        ) -> None: ...

        return group

    with monkeypatch.context() as patch:
        patch.setattr(registration, "make_member_shift_group", old_shift_group)
        before = make_bot(v3_cfg, v3_db)
        assert (await setup_commands(before))["global"] == "synced"

    after = make_bot(v3_cfg, v3_db)
    results = await setup_commands(after)
    assert results["global"] == "synced"
    assert after.synced.count(None) == 1  # one global sync, not one per command

    again = make_bot(v3_cfg, v3_db)
    assert set((await setup_commands(again)).values()) == {"unchanged"}
    assert again.synced == []


# --- the pager and `days` at their edges ---


async def test_a_stale_pager_click_stays_on_a_real_page_for_every_pager(v3_cfg, v3_db):
    """`PagerView` clamps, so /news, /shift codes and /shift history share the fix."""
    from newsbot.bot.views import PagerView

    pages = [discord.Embed(description=f"page {i}") for i in (1, 2, 3)]

    async def render_page(page: int) -> discord.Embed:
        return pages[page - 1]

    view = PagerView(1, render_page=render_page, total_pages=3)
    for button, edge in ((view.prev_button, 1), (view.next_button, 3)):
        for _ in range(4):  # more clicks than there are pages
            click = FakeInteraction(guild_id=GUILD_A)
            await button.callback(click)
            assert 1 <= view.page <= 3
            assert click.response.edits[-1]["embed"] is pages[view.page - 1]
        assert view.page == edge


def test_history_days_is_clamped_and_non_integers_fall_back_to_14():
    from newsbot.bot.commands import _history_days

    assert [_history_days(d) for d in (1, 14, 90, 0, -3, 91, 10**12)] == [1, 14, 90, 1, 1, 90, 90]
    assert [_history_days(d) for d in ("abc", "30", 7.0, None, True, [5])] == [14] * 6
