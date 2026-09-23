"""Tests for newsbot.pipeline.normalize: canonicalization, dedupe, lookback."""

from datetime import UTC, datetime, timedelta

from newsbot.collectors.base import RawItem
from newsbot.pipeline.normalize import canonicalize, normalize

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _item(url: str, *, trust="community", published_at=None, title="Title", excerpt="Excerpt"):
    return RawItem(
        url=url,
        title=title,
        excerpt=excerpt,
        source_name="Some Source",
        trust=trust,
        published_at=published_at,
    )


# --- canonicalize ---


def test_canonicalize_strips_utm_params():
    assert (
        canonicalize("https://example.com/a?utm_source=feed&utm_medium=rss&x=1")
        == "https://example.com/a?x=1"
    )


def test_canonicalize_strips_known_tracking_params():
    assert (
        canonicalize("https://example.com/a?fbclid=abc&ref=xyz&y=2") == "https://example.com/a?y=2"
    )


def test_canonicalize_preserves_youtube_v_param():
    assert (
        canonicalize("https://www.youtube.com/watch?v=abc123")
        == "https://www.youtube.com/watch?v=abc123"
    )


def test_canonicalize_preserves_steam_appid_param():
    assert (
        canonicalize("https://store.steampowered.com/app/1623730?appid=1623730")
        == "https://store.steampowered.com/app/1623730?appid=1623730"
    )


def test_canonicalize_lowercases_scheme_and_host():
    assert canonicalize("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_canonicalize_strips_trailing_slash_on_non_root_path():
    assert canonicalize("https://example.com/a/") == "https://example.com/a"


def test_canonicalize_keeps_root_slash():
    assert canonicalize("https://example.com/") == "https://example.com/"


def test_canonicalize_drops_fragment():
    assert canonicalize("https://example.com/a#section") == "https://example.com/a"


def test_canonicalize_drops_default_port():
    assert canonicalize("https://example.com:443/a") == "https://example.com/a"
    assert canonicalize("http://example.com:80/a") == "http://example.com/a"


def test_canonicalize_keeps_non_default_port():
    assert canonicalize("https://example.com:8443/a") == "https://example.com:8443/a"


def test_canonicalize_rejects_javascript_scheme():
    assert canonicalize("javascript:alert(1)") is None


def test_canonicalize_rejects_data_scheme():
    assert canonicalize("data:text/html,<script>alert(1)</script>") is None


def test_canonicalize_does_not_merge_http_and_https():
    assert canonicalize("http://example.com/a") == "http://example.com/a"
    assert canonicalize("https://example.com/a") == "https://example.com/a"


def test_canonicalize_is_idempotent():
    once = canonicalize("HTTPS://Example.com/a/?utm_source=x&y=2#frag")
    twice = canonicalize(once)
    assert once == twice


# --- normalize ---


def test_normalize_drops_non_http_urls():
    items = [_item("javascript:alert(1)")]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert result == []


def test_normalize_in_batch_dedupe_first_seen_wins_on_tie():
    items = [
        _item("https://example.com/a", trust="press", title="first"),
        _item("https://example.com/a", trust="press", title="second"),
    ]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert len(result) == 1
    assert result[0].title == "first"


def test_normalize_in_batch_dedupe_prefers_higher_trust():
    items = [
        _item("https://example.com/a", trust="community", title="first"),
        _item("https://example.com/a", trust="official", title="second"),
    ]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert len(result) == 1
    assert result[0].title == "second"
    assert result[0].trust == "official"


def test_normalize_drops_known_urls():
    items = [_item("https://example.com/a"), _item("https://example.com/b")]

    def known_urls(urls):
        return {"https://example.com/a"}

    result = normalize(items, known_urls=known_urls, now=NOW, lookback=timedelta(hours=24))
    assert [i.url for i in result] == ["https://example.com/b"]


def test_normalize_drops_items_older_than_lookback():
    items = [
        _item("https://example.com/old", published_at=NOW - timedelta(hours=25)),
        _item("https://example.com/new", published_at=NOW - timedelta(hours=1)),
    ]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert [i.url for i in result] == ["https://example.com/new"]


def test_normalize_lookback_boundary_is_inclusive():
    items = [_item("https://example.com/exact", published_at=NOW - timedelta(hours=24))]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert len(result) == 1


def test_normalize_keeps_items_with_no_published_at():
    items = [_item("https://example.com/undated", published_at=None)]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert len(result) == 1


def test_normalize_canonicalizes_before_dedupe():
    items = [
        _item("https://example.com/a?utm_source=feed", trust="press", title="first"),
        _item("https://example.com/a", trust="press", title="second"),
    ]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert len(result) == 1


def test_normalize_known_urls_receives_canonical_candidates():
    seen = {}

    def known_urls(urls):
        seen["urls"] = urls
        return set()

    items = [_item("https://example.com/a/?utm_source=feed")]
    normalize(items, known_urls=known_urls, now=NOW, lookback=timedelta(hours=24))
    assert seen["urls"] == {"https://example.com/a"}
