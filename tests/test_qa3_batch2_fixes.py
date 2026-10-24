"""QA fix batch 2: the round-2 fixes that the adversary's xfails did not already pin.

The adversarial round (`test_qa2_*.py`) found the bugs; these are the neighbours: the window
snapping to clause boundaries, the separators that end a paragraph, foreign digits after a
year, headings of every level and captions, the stamp floor with an odd offset, and the
index the stale-009 check now wants. Invisible and non-ASCII characters are built with
`chr()` and `\\N{...}`.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from test_qa2_stamps_migration import A, B, C, T, overwrite_stamp, put, stamp_of
from test_shift_details_detection import X, Y
from test_shift_details_detection_adversarial import read_named

from newsbot.shift.details import after
from newsbot.shift.official import _clause_edges, _windows, read_official_details
from newsbot.store.db import StoreError, connect, migrate

CODE = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA"
PUBLISHED = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 9, 12, tzinfo=UTC)


def _expiry(text, published_at=PUBLISHED):
    found = read_official_details(text, CODE, published_at)
    return None if found is None else found.expires_on


# =====================================================================================
# A1: the window snaps to clause boundaries instead of cutting through a clause
# =====================================================================================


def test_a_date_across_the_old_edge_is_read_whole_when_the_sentence_ends_soon_after():
    # The old window ended after "December 3". The sentence ends a few characters later, so
    # the window now does too, and the date is the date it says.
    text = f"{CODE} " + "x" * 180 + " expires December 31, 2026. Enjoy the loot."
    assert _expiry(text) == date(2026, 12, 31)


def test_a_front_clause_across_the_old_edge_is_read_whole_when_the_sentence_starts_soon_before():
    text = "Sadly, this is not valid until October 14 " + "x" * 170 + f" {CODE}"
    assert _expiry(text) is None
    text = "Heads up. Valid until October 14 " + "x" * 170 + f" {CODE}"
    assert _expiry(text) == date(2026, 10, 14)


def test_a_clause_with_no_boundary_in_reach_is_dropped_not_cut():
    # One breathless sentence: the keyword is inside the plain window, the rest of the date is
    # past it, and there is no boundary within another 200 characters. Nothing is read.
    text = f"{CODE} " + "x" * 190 + " expires December 31, 2026 " + "y" * 400
    assert _expiry(text) is None


def test_the_clause_the_code_is_in_is_always_inside_the_window():
    text = "intro. " + "x" * 600 + f" {CODE} " + "y" * 600 + ". outro"
    (window,) = _windows(text, CODE)
    assert CODE in window
    assert "intro" not in window and "outro" not in window


@pytest.mark.parametrize("pad", range(0, 260, 7))
def test_a_dotted_abbreviation_never_opens_a_hole_in_a_range(pad):
    # "Oct." looks like a sentence end and isn't one: a period before a digit is not a boundary,
    # so the window can't stop between the two halves of a range.
    assert _expiry(f"{CODE} " + "x" * pad + " expires Oct. 14 - Oct. 21") is None


@pytest.mark.parametrize("pad", range(0, 260, 7))
def test_the_answer_is_right_or_nothing_wherever_the_edge_falls(pad):
    got = _expiry(f"{CODE} " + "x" * pad + " expires Oct. 14, 2027. Thanks.")
    assert got in (None, date(2027, 10, 14))


def test_clause_edges_know_a_decimal_and_an_abbreviation_from_a_sentence():
    text = "Version 1.5 is out. Expires Oct. 14! Really? Yes.\nNext line"
    starts, ends = _clause_edges(text, 0, len(text))
    assert text.index("is out.") + len("is out.") in ends
    assert text.index("14!") + len("14!") in ends
    assert text.index("Really?") + len("Really?") in ends
    assert text.index("\nNext") in ends and text.index("\nNext") + 1 in starts
    assert text.index("1.5") + len("1.5") not in ends  # a decimal
    assert text.index("Oct.") + len("Oct.") not in ends  # a period before a digit


# =====================================================================================
# A2: a digit from another script is a digit
# =====================================================================================


@pytest.mark.parametrize("digit", [chr(0x0664), chr(0xFF14), chr(0x0969)])
def test_a_foreign_digit_after_a_year_or_a_numeric_date_is_not_a_date(digit):
    assert _expiry(f"{CODE} valid until October 14, 2027{digit}") is None
    assert _expiry(f"{CODE} valid until 10/14/2027{digit}") is None
    assert _expiry(f"{CODE} valid until October 14, 2027") == date(2027, 10, 14)


# =====================================================================================
# A6: line and paragraph separators end a paragraph
# =====================================================================================


@pytest.mark.parametrize("separator", [chr(0x2028), chr(0x2029), chr(0x0085)])
def test_unicode_separators_split_paragraphs_in_both_directions(separator):
    assert _expiry(f"Code {CODE}{separator}expires October 14") is None
    assert _expiry(f"expires October 14{separator}Code {CODE}") is None
    (window,) = _windows(f"before{separator}the code {CODE} here{separator}after", CODE)
    assert "before" not in window and "after" not in window


def test_a_plain_carriage_return_is_still_only_a_line_break():
    assert _expiry(f"Code {CODE}\rexpires October 14") == date(2026, 10, 14)


# =====================================================================================
# A3: headings of every level, captions, and headings nobody closed
# =====================================================================================


def _rows(*codes: str) -> str:
    body = "".join(f"<tr><td>3 Golden Keys</td><td>{c}</td></tr>" for c in codes)
    return f"<tr><th>Reward</th><th>Code</th></tr>{body}"


async def _titles(html: str) -> list[str]:
    return sorted(i.title for i in await read_named(f"<html><body>{html}</body></html>", T0))


async def _category_of(html: str, code: str) -> str | None:
    items = await read_named(f"<html><body>{html}</body></html>", T0)
    return next(i.details.category for i in items if i.title == code)


LIVE = f"<h2>Codes</h2><table>{_rows(X)}</table>"


@pytest.mark.parametrize("level", ["h1", "h2", "h3", "h4", "h5", "h6"])
async def test_every_heading_level_can_say_expired(level):
    html = f"<{level}>Expired codes</{level}><table>{_rows(Y)}</table>" + LIVE
    assert await _titles(html) == [X]


async def test_a_caption_that_says_expired_skips_its_table():
    html = f"<table><caption>Expired codes</caption>{_rows(Y)}</table>" + LIVE
    assert await _titles(html) == [X]


async def test_a_caption_and_a_heading_both_count_toward_expired():
    html = f"<h2>Old stuff</h2><table><caption>Long expired</caption>{_rows(Y)}</table>" + LIVE
    assert await _titles(html) == [X]
    html = f"<h3>Expired</h3><table><caption>Codes</caption>{_rows(Y)}</table>" + LIVE
    assert await _titles(html) == [X]


async def test_an_unclosed_caption_ends_where_the_rows_begin():
    html = f"<table><caption>Expired codes{_rows(Y)}</table>" + LIVE
    assert await _titles(html) == [X]


async def test_a_caption_with_no_such_word_changes_nothing():
    html = f"<table><caption>Tonight's codes</caption>{_rows(Y)}</table>" + LIVE
    assert await _titles(html) == sorted([X, Y])


@pytest.mark.parametrize("level", ["h1", "h5", "h6"])
async def test_permanent_and_cosmetic_headings_work_at_every_level(level):
    html = (
        f"<{level}>Permanent SHiFT codes</{level}><table>{_rows(X)}</table>"
        f"<{level}>Cosmetic rewards</{level}><table>{_rows(Y)}</table>"
    )
    assert await _category_of(html, X) == "permanent"
    assert await _category_of(html, Y) == "cosmetic"


async def test_a_caption_decides_the_kind_over_a_heading_that_has_no_opinion():
    html = f"<h2>Codes</h2><table><caption>Permanent codes</caption>{_rows(X)}</table>"
    assert await _category_of(html, X) == "permanent"
    html = f"<h2>Codes</h2><table><caption>Cosmetic drops</caption>{_rows(X)}</table>"
    assert await _category_of(html, X) == "cosmetic"


@pytest.mark.parametrize("closing_tag", ["", "<br>", "<p>"])
async def test_an_unclosed_heading_that_runs_into_the_table_is_still_the_heading(closing_tag):
    skipped = f"<h5>Expired codes{closing_tag}<table>{_rows(Y)}</table>" + LIVE
    assert await _titles(skipped) == [X]
    permanent = f"<h3>Permanent codes{closing_tag}<table>{_rows(X)}</table>"
    assert await _category_of(permanent, X) == "permanent"


async def test_an_unclosed_heading_followed_by_another_heading_keeps_the_later_one():
    html = f"<h2>Expired codes<h3>Active codes</h3><table>{_rows(X)}</table>"
    assert await _titles(html) == [X]


# =====================================================================================
# A4 and A5: the stamp floor
# =====================================================================================


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def test_an_odd_offset_stamp_is_compared_as_the_instant_it_is(db_path):
    put(db_path, A, "1 key", now=T + timedelta(hours=1))  # 13:00Z
    put(db_path, B, "2 keys", now=T + timedelta(hours=1))
    # 14:00 in Tokyo is 05:00Z: later as text than A's stamp, earlier as time. Not the floor.
    overwrite_stamp(db_path, B, "2026-10-09T14:00:00+09:00")
    put(db_path, C, "3 keys", now=T)
    assert stamp_of(db_path, C) == stamp_of(db_path, A) + timedelta(microseconds=1)


def test_an_odd_offset_stamp_that_really_is_the_latest_is_the_floor(db_path):
    put(db_path, A, "1 key", now=T + timedelta(hours=1))
    put(db_path, B, "2 keys", now=T + timedelta(hours=1))
    # 14:00 on the 10th in Tokyo is 05:00Z on the 10th, a day after everything else.
    overwrite_stamp(db_path, B, "2026-10-10T14:00:00+09:00")
    put(db_path, C, "3 keys", now=T)
    assert stamp_of(db_path, C) == datetime(2026, 10, 10, 5, 0, 0, 1, tzinfo=UTC)


def test_the_largest_stamp_that_parses_wins_over_garbled_ones_that_sort_higher(db_path):
    put(db_path, A, "1 key", now=T + timedelta(hours=1))
    put(db_path, B, "2 keys", now=T + timedelta(hours=1))
    put(db_path, C, "3 keys", now=T + timedelta(hours=1))
    overwrite_stamp(db_path, B, "2026-13-45T00:00:00+00:00")  # looks UTC, isn't a date
    overwrite_stamp(db_path, C, "2099-01-01T00:00:00+00:00x")
    other = "DDDD4-DDDDD-DDDDD-DDDDD-DDDDD"
    put(db_path, other, "4 keys", now=T)
    assert stamp_of(db_path, other) == stamp_of(db_path, A) + timedelta(microseconds=1)


def test_after_adds_a_microsecond_and_stops_at_the_end_of_time():
    moment = datetime(2026, 10, 9, tzinfo=UTC)
    assert after(moment) == moment + timedelta(microseconds=1)
    last = datetime.max.replace(tzinfo=UTC)
    assert after(last) == last


def test_a_change_to_the_row_that_holds_the_last_instant_does_not_raise(db_path):
    put(db_path, A, "1 key", now=T)
    overwrite_stamp(db_path, A, "9999-12-31T23:59:59.999999+00:00")
    put(db_path, A, "2 keys", now=T + timedelta(hours=1))  # a displayed change to that very row
    assert stamp_of(db_path, A) == datetime.max.replace(tzinfo=UTC)


# =====================================================================================
# A7: the stale-009 check wants the index too
# =====================================================================================


def test_a_database_without_the_follow_up_index_is_refused(db_path):
    with closing(connect(db_path)) as conn:
        conn.execute("DROP INDEX idx_guild_code_posts_code")
        conn.commit()
        with pytest.raises(StoreError) as caught:
            migrate(conn)
    assert "idx_guild_code_posts_code" in str(caught.value)
    assert "start the database fresh" in str(caught.value)


def test_a_database_with_the_index_still_passes(db_path):
    with closing(connect(db_path)) as conn:
        assert migrate(conn) == 10


def test_both_a_missing_column_and_a_missing_index_are_named(db_path):
    with closing(connect(db_path)) as conn:
        conn.execute("DROP INDEX idx_guild_code_posts_code")
        conn.execute("ALTER TABLE alerted_codes RENAME COLUMN official_seen_at TO draft")
        conn.commit()
        with pytest.raises(StoreError) as caught:
            migrate(conn)
    message = str(caught.value)
    assert "alerted_codes.official_seen_at" in message and "idx_guild_code_posts_code" in message


def _many_stamps(db_path: str, count: int) -> datetime:
    """`count` rows with distinct UTC stamps, written the way the store writes them."""
    from newsbot.store import repo
    from newsbot.store.models import CodeDetailsRow

    base = datetime(2026, 10, 1, tzinfo=UTC)
    with closing(connect(db_path)) as conn, conn:
        for n in range(count):
            stamp = base + timedelta(minutes=n)
            repo._write_details(
                conn,
                CodeDetailsRow(
                    code=f"{n:05d}-AAAAA-AAAAA-AAAAA-AAAAA",
                    clock_start=base,
                    updated_at=base,
                    details_changed_at=stamp,
                ),
            )
    return base + timedelta(minutes=count - 1)


def test_the_floor_finds_the_newest_stamp_in_a_table_far_bigger_than_the_scan(db_path):
    from newsbot.store import repo

    newest = _many_stamps(db_path, 600)
    with closing(connect(db_path)) as conn:
        assert repo._stamp_floor(conn) == newest


def test_an_odd_offset_row_is_still_found_in_a_big_table(db_path):
    from newsbot.store import repo

    _many_stamps(db_path, 600)
    overwrite_stamp(db_path, "00007-AAAAA-AAAAA-AAAAA-AAAAA", "2027-01-01T09:00:00+09:00")
    with closing(connect(db_path)) as conn:
        assert repo._stamp_floor(conn) == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)
