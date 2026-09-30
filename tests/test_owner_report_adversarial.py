"""Breaking the owner's daily line, the failing-sources report and the status view.

The owner's report is read once a day by one person who wants to know whether
to panic. So it should be short, it should never ping anyone, it should never
contain a password, and its first line should survive whatever the rest of it
does. The strict xfails below are the places where that last list is a
slightly optimistic reading of the code; the rest pin what holds.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

import pytest

from newsbot.bot.format import (
    DigestOutcome,
    discord_len,
    render_digest_summary_line,
    render_guild_status,
    render_owner_report,
)
from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import Notice, SourceHealthRow

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _src(name, fails, error="timeout"):
    return SourceHealthRow(name, None, T0, error, fails)


# --- the summary line ---


def test_huge_counts_do_not_overflow_or_misformat():
    outcomes = [DigestOutcome(True)] * 100_000 + [DigestOutcome(False, "boom")] * 5_000
    assert render_digest_summary_line(outcomes) == (
        "digests posted to 100000 of 105000 servers; 5000 failed (boom)"
    )


def test_reason_ties_break_alphabetically_so_the_line_is_stable():
    outcomes = [DigestOutcome(False, "b"), DigestOutcome(False, "a"), DigestOutcome(False, "c")]
    assert render_digest_summary_line(outcomes).endswith("3 failed (1 a, 1 b, 1 c)")


def test_reasons_that_differ_only_by_whitespace_are_one_reason():
    outcomes = [DigestOutcome(False, "claude  error"), DigestOutcome(False, "claude\nerror")]
    assert render_digest_summary_line(outcomes).endswith("2 failed (claude error)")


def test_a_reason_on_a_posted_outcome_is_ignored():
    assert render_digest_summary_line([DigestOutcome(True, "leftover")]) == (
        "digests posted to 1 of 1 server"
    )


@pytest.mark.parametrize(
    "reason",
    [
        "@everyone",
        "@here",
        "<@123456789012345678>",
        "<@&123456789012345678>",
        "<#123456789012345678>",
    ],
)
def test_reasons_cannot_ping(reason):
    line = render_digest_summary_line([DigestOutcome(False, reason)])
    assert "@everyone" not in line and "@here" not in line
    assert "<@123456789012345678>" not in line and "<@&123456789012345678>" not in line
    assert "<#123456789012345678>" not in line


@pytest.mark.xfail(
    strict=True,
    reason=(
        "render_digest_summary_line lists every distinct reason with no cap; 200 distinct "
        "reasons make a line far past Discord's 2000-unit limit (it only survives because "
        "whoever sends it truncates). A one-liner should stay one line."
    ),
)
def test_summary_line_stays_short_however_many_reasons_there_are():
    outcomes = [DigestOutcome(False, f"reason number {i:04d} " + "x" * 30) for i in range(200)]
    assert discord_len(render_digest_summary_line(outcomes)) <= 2000


# --- the report ---


def test_the_first_line_survives_a_pathological_source_list():
    failing = [_src("n" * 500 + str(i), 10**9, "e" * 500) for i in range(1000)]
    report = render_owner_report([DigestOutcome(True)] * 37 + [DigestOutcome(False, "x")], failing)
    assert report.splitlines()[0] == (
        "newsbot daily: digests posted to 37 of 38 servers; 1 failed (x)"
    )
    assert discord_len(report) <= 2000
    assert "1000 failing sources:" in report


def test_astral_heavy_names_and_errors_still_fit_the_cap_in_utf16_units():
    failing = [_src("\U0001f600" * 80, 3, "\U0001f4a5" * 200) for _ in range(10)]
    report = render_owner_report([DigestOutcome(True)], failing)
    assert discord_len(report) <= 2000
    report.encode("utf-16-le")


def test_markdown_heavy_error_expands_but_the_cap_still_holds():
    report = render_owner_report([DigestOutcome(True)], [_src("a", 2, "_" * 500)] * 10)
    assert discord_len(report) <= 2000
    assert report.startswith("newsbot daily: digests posted to 1 of 1 server")


def test_exactly_ten_failing_sources_have_no_more_line_and_eleven_have_one():
    ten = render_owner_report([DigestOutcome(True)], [_src(f"s{i}", 2, None) for i in range(10)])
    eleven = render_owner_report([DigestOutcome(True)], [_src(f"s{i}", 2, None) for i in range(11)])
    assert "more" not in ten
    assert eleven.splitlines()[-1] == "+1 more"


def test_the_report_with_no_outcomes_and_no_failures_is_still_a_report():
    assert render_owner_report([], []).splitlines() == [
        "newsbot daily: no server digests were due today",
        "All sources are healthy.",
    ]


@pytest.mark.parametrize(
    "error",
    [
        "@everyone",
        "<@&123456789012345678> <#123456789012345678>",
        "[click](https://evil.example/)",
        "discord.gg/abc",
    ],
)
def test_source_errors_cannot_ping_or_link(error):
    report = render_owner_report([DigestOutcome(True)], [_src("s", 2, error)])
    assert "@everyone" not in report
    assert "<@&123456789012345678>" not in report and "<#123456789012345678>" not in report
    assert "](https://" not in report and "discord.gg/" not in report


@pytest.mark.xfail(
    strict=True,
    reason=(
        "render_owner_report escapes the stored last_error but never runs URLs in it through "
        "shown_url, so credentials and query-string tokens in a failing source's error "
        "(httpx puts the whole URL in its messages) land in the admin channel. The v2 "
        "run report has the same gap."
    ),
)
@pytest.mark.parametrize(
    "secret_error",
    [
        "Client error '403 Forbidden' for url 'https://user:hunter2@feeds.example/x'",
        "Server error '500' for url 'https://feeds.example/x?token=SECRET123'",
    ],
)
def test_secrets_in_error_urls_are_redacted_with_shown_url(secret_error):
    report = render_owner_report([DigestOutcome(True)], [_src("feed", 2, secret_error)])
    assert "hunter2" not in report and "SECRET123" not in report


# --- failing_sources ---


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "n.db")) as c:
        migrate(c)
        yield c


def _health(conn, name, fails, error="e", error_at=None):
    with conn:
        conn.execute(
            "INSERT INTO source_health (source_name, last_error_at, last_error, "
            "consecutive_failures) VALUES (?, ?, ?, ?)",
            (name, error_at, error, fails),
        )


def test_failing_sources_are_worst_first_with_name_breaking_ties(conn):
    for name, fails in [("b", 3), ("a", 3), ("c", 9), ("d", 1), ("healthy", 0)]:
        _health(conn, name, fails)
    assert [(r.source_name, r.consecutive_failures) for r in repo.failing_sources(conn)] == [
        ("c", 9),
        ("a", 3),
        ("b", 3),
        ("d", 1),
    ]


def test_tie_order_is_binary_so_capitals_sort_before_lowercase(conn):
    """Documented wart: the v2 status view sorts by casefold, this sorts by byte."""
    for name in ("alpha", "Zed"):
        _health(conn, name, 2)
    assert [r.source_name for r in repo.failing_sources(conn)] == ["Zed", "alpha"]


@pytest.mark.parametrize("floor", [0, -5, 1])
def test_a_floor_below_one_still_excludes_healthy_sources(conn, floor):
    _health(conn, "ok", 0)
    _health(conn, "bad", 1)
    assert [r.source_name for r in repo.failing_sources(conn, floor)] == ["bad"]


def test_min_failures_is_a_floor_not_an_exact_match(conn):
    for name, fails in [("two", 2), ("three", 3), ("twelve", 12)]:
        _health(conn, name, fails)
    assert [r.source_name for r in repo.failing_sources(conn, 3)] == ["twelve", "three"]


def test_a_source_with_no_error_or_timestamps_comes_back_whole(conn):
    _health(conn, "quiet", 4, error=None, error_at=None)
    (row,) = repo.failing_sources(conn)
    assert (row.last_error, row.last_error_at, row.last_success_at) == (None, None, None)


def test_no_health_rows_means_an_empty_list(conn):
    assert repo.failing_sources(conn) == []


def test_failing_sources_feed_the_report_end_to_end(conn):
    _health(conn, "ign", 12, error="timeout after 30s")
    _health(conn, "rss", 2, error="HTTP 503")
    report = render_owner_report([DigestOutcome(True)], repo.failing_sources(conn))
    assert report.splitlines()[2:] == [
        "- ign (12 in a row): timeout after 30s",
        "- rss (2 in a row): HTTP 503",
    ]


# --- the status view ---


def test_status_keeps_its_field_value_under_the_embed_limit_with_astral_text():
    notices = [Notice(i, 1, T0, "\U0001f600" * 400) for i in range(5)]
    text = render_guild_status(notices)
    assert discord_len(text) <= 1024
    text.encode("utf-16-le")


def test_status_flattens_newlines_so_one_notice_is_one_bullet():
    text = render_guild_status([Notice(1, 1, T0, "line one\nline two\n\n- fake bullet")])
    assert text.count("\n") == 0
    assert text.startswith("- 2026-09-30 12:00 UTC: line one line two - fake bullet")


def test_status_shows_only_the_newest_limit_notices():
    notices = [Notice(i, 1, T0, f"n{i}") for i in range(30)]
    assert render_guild_status(notices, limit=2).count("\n") == 1
    assert "n2" not in render_guild_status(notices, limit=2)
