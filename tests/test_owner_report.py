"""The owner's daily summary line and report (plan task 8, D8)."""

from __future__ import annotations

from datetime import UTC, datetime

from newsbot.bot.format import (
    DigestOutcome,
    discord_len,
    render_digest_summary_line,
    render_guild_status,
    render_owner_report,
)
from newsbot.store.models import Notice, SourceHealthRow

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _ok(n):
    return [DigestOutcome(True)] * n


def _bad(n, reason="missing permissions"):
    return [DigestOutcome(False, reason)] * n


def _src(name, fails, error="timeout"):
    return SourceHealthRow(name, None, T0, error, fails)


def test_the_documented_example():
    line = render_digest_summary_line(_ok(37) + _bad(1))
    assert line == "digests posted to 37 of 38 servers; 1 failed (missing permissions)"


def test_all_ok():
    assert render_digest_summary_line(_ok(38)) == "digests posted to 38 of 38 servers"


def test_zero_servers():
    assert render_digest_summary_line([]) == "no server digests were due today"


def test_one_of_one():
    assert render_digest_summary_line(_ok(1)) == "digests posted to 1 of 1 server"


def test_one_failed_of_one():
    line = render_digest_summary_line(_bad(1))
    assert line == "digests posted to 0 of 1 server; 1 failed (missing permissions)"


def test_all_failed():
    line = render_digest_summary_line(_bad(38))
    assert line == "digests posted to 0 of 38 servers; 38 failed (missing permissions)"


def test_mixed_reasons_are_counted_most_common_first():
    outcomes = _ok(5) + _bad(1, "Claude error") + _bad(2, "missing permissions")
    assert render_digest_summary_line(outcomes) == (
        "digests posted to 5 of 8 servers; 3 failed (2 missing permissions, 1 Claude error)"
    )


def test_blank_reason_is_unknown():
    assert render_digest_summary_line(_bad(1, "  ")).endswith("1 failed (unknown reason)")


def test_reason_text_is_flattened_capped_and_defused():
    line = render_digest_summary_line(_bad(1, "@everyone\nboom " + "x" * 200))
    assert "\n" not in line and "@everyone" not in line
    assert discord_len(line) < 200


def test_report_leads_with_the_summary_then_lists_failing_sources():
    report = render_owner_report(_ok(2), [_src("ign", 12), _src("rss: pal", 3, "503 from host")])
    lines = report.splitlines()
    assert lines[0] == "newsbot daily: digests posted to 2 of 2 servers"
    assert lines[1] == "2 failing sources:"
    assert "ign (12 in a row): timeout" in lines[2]
    assert "rss: pal (3 in a row): 503 from host" in lines[3]


def test_report_with_no_failing_sources():
    assert "All sources are healthy." in render_owner_report(_ok(1), [])


def test_single_failing_source_is_singular():
    assert "1 failing source:" in render_owner_report(_ok(1), [_src("a", 1)])


def test_failing_source_list_is_cut_with_a_more_line_and_the_cap_holds():
    report = render_owner_report(_ok(1), [_src(f"s{i}", 2, "e" * 400) for i in range(40)])
    assert "+30 more" in report
    assert discord_len(report) <= 2000
    assert report.startswith("newsbot daily:")


def test_source_error_and_name_are_defused_and_one_line():
    report = render_owner_report(
        _ok(1), [_src("@everyone feed", 3, "bad\n<@123456789012345678> https://x.test")]
    )
    assert "@everyone" not in report and "<@123456789012345678>" not in report
    assert len(report.splitlines()) == 3


def test_source_with_no_error_text():
    report = render_owner_report(_ok(1), [_src("a", 2, None)])
    assert report.splitlines()[2] == "- a (2 in a row)"


def test_guild_status_lists_newest_first_and_caps():
    notices = [Notice(i, 1, T0, f"problem {i}") for i in range(8)]
    text = render_guild_status(notices)
    assert text.count("\n") == 4 and "problem 0" in text
    assert render_guild_status([]) == "No problems recorded lately."
