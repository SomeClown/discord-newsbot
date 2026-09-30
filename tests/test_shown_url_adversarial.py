"""Adversarial tests for `shown_url` and the config errors that echo a URL.

The brief (test-engineer, 2026-09-29): a private gist's raw link carries its
secret in the userinfo or the query string, and a config error is exactly
the sort of thing that gets pasted into a chat with the words "why won't this
work". `shown_url` promises scheme, host and path, nothing else. This file
tries to make that promise fail with the URLs a person actually mistypes
(an `@` in a password, an IPv6 host, a fragment where a path should be) and
a few nobody would type on purpose.

The one invariant everything leans on: no message the config loader or
`describe` builds may contain the password or the query of the URL it was
handed. Where the current code lets a shape through, the test is an
`xfail(strict)` with the reason spelled out. Where the behavior is merely
odd, it's `test_documented_...`. No network is involved; these are pure
functions and a temp YAML file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from newsbot.config import ConfigError, QuoteSourceCfg, _source_problem, load_config
from newsbot.lounge.sources import describe
from newsbot.text import shown_url

HIDDEN = "hunter2-SECRET"
QUERY = "token=QUERY-SECRET"
ROOT_FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"

# Shapes where the password and the query sit where urlsplit puts them.
WELL_FORMED = [
    f"https://user:{HIDDEN}@host.example/gist/raw?{QUERY}",
    f"https://user:{HIDDEN.replace('-', '@')}@host.example/x?{QUERY}",
    "https://user:hunter2%40SECRET@host.example/x?" + QUERY,
    f"https://user:{HIDDEN}@[2001:db8::1]:8443/x?{QUERY}",
    f"http://user:{HIDDEN}@host.example/x?{QUERY}",
    f"ftp://user:{HIDDEN}@host.example/x?{QUERY}",
    f"HTTPS://user:{HIDDEN}@HOST.example/x?{QUERY}#frag",
    f"  https://user:{HIDDEN}@host.example/x?{QUERY}  ",
    f"//user:{HIDDEN}@host.example/x?{QUERY}",
    f"https://user:{HIDDEN}@host.example:99999/x?{QUERY}",
    f"https://user:{HIDDEN}@host.example:notaport/x?{QUERY}",
    f"https://user:{HIDDEN}@host.example/x\n?{QUERY}",
    f"https://user:{HIDDEN}@/x?{QUERY}",
    f"https://[bad?{QUERY}&pw={HIDDEN}",
    f"https://host.example/x?{QUERY}#{HIDDEN}",
    f"https://host.example#{HIDDEN}",
    f"https://host.example?{QUERY}",
]


def _secrets_in(text: str) -> list[str]:
    return [s for s in (HIDDEN, "QUERY-SECRET", "hunter2@SECRET", "hunter2%40SECRET") if s in text]


def _write(tmp_path: Path, url: str) -> Path:
    text = ROOT_FIXTURE.read_text()
    doc = yaml.safe_load(text)
    doc["lounge"] = {
        "channel_id": 5,
        "daily_quote": {"enabled": True, "sources": [{"kind": "url", "value": url}]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.mark.parametrize("url", WELL_FORMED)
def test_shown_url_never_contains_the_password_or_the_query(url):
    assert _secrets_in(shown_url(url)) == []


@pytest.mark.parametrize("url", WELL_FORMED)
def test_source_problem_never_contains_the_password_or_the_query(url):
    problem = _source_problem(0, QuoteSourceCfg(kind="url", value=url))
    assert problem is None or _secrets_in(problem) == []


@pytest.mark.parametrize("url", WELL_FORMED)
def test_describe_never_contains_the_password_or_the_query(url):
    assert _secrets_in(describe(QuoteSourceCfg(kind="url", value=url))) == []


@pytest.mark.parametrize(
    "url", [u for u in WELL_FORMED if u.strip().lower().startswith(("http:", "ftp:", "//"))]
)
def test_a_config_that_fails_on_a_url_source_never_prints_its_secrets(tmp_path, url):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, url))
    assert _secrets_in(str(exc_info.value)) == []


@pytest.mark.parametrize(
    "url",
    [
        f"https:user:{HIDDEN}@host.example/x?{QUERY}",
        f"https:/user:{HIDDEN}@host.example/x?{QUERY}",
        f"https:\\\\user:{HIDDEN}@host.example\\x?{QUERY}",
        f"mailto:user:{HIDDEN}@host.example?{QUERY}",
    ],
)
def test_a_url_missing_its_slashes_does_not_leak_its_password(url):
    problem = _source_problem(0, QuoteSourceCfg(kind="url", value=url))
    assert problem is not None
    assert _secrets_in(problem) == []


# --- What shown_url keeps ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:p%40ss@host.example/x", "https://host.example/x"),
        ("https://user:p@ss@host.example/x", "https://host.example/x"),
        ("https://[::1]:8443/x?t=s", "https://[::1]:8443/x"),
        ("https://u:pw@[2001:db8::1]/x", "https://[2001:db8::1]/x"),
        ("https://host.example#frag", "https://host.example"),
        ("https://host.example?a=1", "https://host.example"),
        ("HTTPS://Host.Example/X", "https://Host.Example/X"),
        ("  https://host.example/x  ", "https://host.example/x"),
        ("https://host.example/x?a#b?c", "https://host.example/x"),
    ],
)
def test_shown_url_keeps_scheme_host_port_and_path(url, expected):
    assert shown_url(url) == expected


@pytest.mark.parametrize("url", ["?token=abc", "#frag", "", "   "])
def test_a_query_or_fragment_alone_shows_as_no_address(url):
    # Changed from showing '': nothing is left after the strip, and
    # "url '' isn't an https:// address" told the admin nothing. It still
    # leaks nothing.
    assert shown_url(url) == "(no address)"
    problem = _source_problem(3, QuoteSourceCfg(kind="url", value=url))
    assert problem == "lounge.daily_quote.sources[3].url '(no address)' isn't an https:// address"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("ftp://u:pw@h.example/x?q=1", "ftp://h.example/x"),
        ("file:///etc/passwd", "file:///etc/passwd"),
        ("javascript:alert(1)", "javascript:alert(1)"),
        # An @ in a netloc-less path now hides the path: it's how a password
        # in "https:user:pw@host" would have leaked. A mailto address is
        # collateral, and harmless to hide.
        ("mailto:a@b.example?subject=x", "mailto:(address hidden)"),
    ],
)
def test_non_http_schemes_are_shown_and_refused(url, shown):
    assert shown_url(url) == shown
    problem = _source_problem(0, QuoteSourceCfg(kind="url", value=url))
    assert problem == f"lounge.daily_quote.sources[0].url {shown!r} isn't an https:// address"


def test_a_plain_http_url_gets_the_plain_http_message_without_its_secrets():
    problem = _source_problem(
        0, QuoteSourceCfg(kind="url", value=f"http://u:{HIDDEN}@h.example/x?{QUERY}")
    )
    assert problem == (
        "lounge.daily_quote.sources[0].url 'http://h.example/x' uses plain http; "
        "only https:// addresses are allowed"
    )


def test_an_unreadable_address_is_named_as_such_and_echoes_nothing():
    url = f"https://[bad?{QUERY}"
    assert shown_url(url) == "(unreadable address)"
    problem = _source_problem(0, QuoteSourceCfg(kind="url", value=url))
    assert (
        problem
        == "lounge.daily_quote.sources[0].url '(unreadable address)' isn't an https:// address"
    )


def test_documented_a_path_parameter_after_a_semicolon_is_kept():
    # `;params` belongs to the last path segment as far as urlsplit is
    # concerned, and urlunsplit puts it back. It's a rare place for a secret,
    # but "scheme, host and path" is a promise about path, and this is path.
    assert shown_url("https://h.example/a;key=abc?q=1") == "https://h.example/a;key=abc"


def test_documented_a_percent_encoded_question_mark_in_the_path_is_kept_as_written():
    assert shown_url("https://h.example/p%3Ftoken=abc") == "https://h.example/p%3Ftoken=abc"


def test_documented_a_very_long_path_is_shown_in_full_and_not_truncated():
    # shown_url cuts nothing. A long path is the admin's own typing, and a
    # config error is one line in a log, not a Discord message. If a URL
    # source ever got echoed into Discord, `plain_line` is the tool for that.
    url = "https://h.example/" + "a" * 5000
    assert shown_url(url) == url
    problem = _source_problem(0, QuoteSourceCfg(kind="url", value="http://h.example/" + "a" * 5000))
    assert problem is not None and len(problem) > 5000


def test_shown_url_is_idempotent():
    once = shown_url(f"https://u:{HIDDEN}@h.example/x?{QUERY}")
    assert shown_url(once) == once
