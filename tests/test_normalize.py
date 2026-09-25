"""Tests for newsbot.pipeline.normalize: canonicalization, dedupe, lookback."""

from datetime import UTC, datetime, timedelta

from newsbot.collectors.base import RawItem
from newsbot.pipeline.normalize import canonicalize, canonicalize_items, normalize

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
        == "https://youtube.com/watch?v=abc123"
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


def test_canonicalize_strips_gclid_and_mc_params_together():
    assert (
        canonicalize("https://example.com/a?gclid=g&mc_cid=c&mc_eid=e&keep=1")
        == "https://example.com/a?keep=1"
    )


def test_canonicalize_tracking_param_match_is_case_insensitive():
    # A source that shouts UTM_SOURCE in all caps shouldn't slip past the filter.
    assert (
        canonicalize("https://example.com/a?UTM_SOURCE=feed&FBCLID=abc&y=1")
        == "https://example.com/a?y=1"
    )


def test_canonicalize_rejects_ftp_scheme():
    assert canonicalize("ftp://example.com/file.txt") is None


def test_canonicalize_rejects_empty_string():
    assert canonicalize("") is None


def test_canonicalize_rejects_whitespace_only():
    assert canonicalize("   ") is None


def test_canonicalize_rejects_scheme_relative_url():
    assert canonicalize("//example.com/a") is None


def test_canonicalize_rejects_empty_host():
    assert canonicalize("https:///path") is None


def test_canonicalize_rejects_userinfo_in_netloc():
    # Was previously preserved verbatim; QA flagged that credentials (or
    # any "user@host" shape) in a link have no legitimate reason to be in
    # a news URL and are a classic way to make the displayed host and the
    # actual host disagree. Rejecting outright is simpler than guessing
    # which part was meant to be the real host.
    assert canonicalize("https://user:pass@example.com/a") is None


def test_canonicalize_rejects_bare_at_in_netloc():
    assert canonicalize("https://evil@example.com/a") is None


# --- path/host metacharacter and homograph defenses ---


def test_canonicalize_percent_encodes_path_breakout_characters():
    # ">" and a space are exactly what would close format.py's `<url>`
    # autolink early; "[" and "]" are what would then let a
    # `[text](url)` markdown link grow right after it. None of those four
    # should survive as literal characters in the canonical path.
    result = canonicalize("https://example.com/a> **boom** [Official patch notes](x)")
    assert result is not None
    for char in "><[] ":
        assert char not in result


def test_canonicalize_path_quoting_is_idempotent():
    once = canonicalize("https://example.com/a b/c[d]")
    twice = canonicalize(once)
    assert once == twice


def test_canonicalize_percent_encodes_non_ascii_path():
    assert canonicalize("https://example.com/café") == "https://example.com/caf%C3%A9"


def test_canonicalize_percent_encodes_bidi_override_in_path():
    result = canonicalize("https://example.com/a‮b")
    assert result is not None
    assert "‮" not in result


def test_canonicalize_rejects_bidi_override_in_host():
    assert canonicalize("https://exa‮mple.com/a") is None


def test_canonicalize_rejects_host_with_overlong_label():
    assert canonicalize(f"https://{'a' * 70}.com/x") is None


def test_canonicalize_handles_extremely_long_url():
    long_path = "a" * 3000
    result = canonicalize(f"https://example.com/{long_path}?utm_source=feed&x=1")
    assert result == f"https://example.com/{long_path}?x=1"


def test_canonicalize_reddit_share_link_strips_share_tracking():
    result = canonicalize(
        "https://old.reddit.com/r/Palworld/comments/abc123/title/?utm_source=share&utm_name=iossmf"
    )
    assert result == "https://old.reddit.com/r/Palworld/comments/abc123/title"


def test_canonicalize_youtube_short_link_strips_si_param():
    assert canonicalize("https://youtu.be/abc123?si=xyz") == "https://youtu.be/abc123"


def test_canonicalize_steam_app_url_strips_snr_keeps_path():
    result = canonicalize(
        "https://store.steampowered.com/app/1623730/Palworld/?snr=1_ab&utm_source=x"
    )
    assert result == "https://store.steampowered.com/app/1623730/Palworld?snr=1_ab"


def test_canonicalize_idn_host_is_lowercased_as_given():
    # urlsplit hands back the Unicode host as-is; canonicalize lowercases it
    # but doesn't fold it to (or from) its punycode form. A feed that mixes
    # Unicode and punycode spellings of the same host will dedupe-miss --
    # not promised anywhere, just documenting the current shape.
    assert canonicalize("https://Exämple.com/a") == "https://exämple.com/a"


def test_canonicalize_punycode_host_is_lowercased():
    assert canonicalize("https://XN--EXMPLE-CUA.com/a") == "https://xn--exmple-cua.com/a"


def test_canonicalize_out_of_range_port_returns_none_instead_of_raising():
    assert canonicalize("http://example.com:99999/a") is None


# --- adversarial cases test-engineer went looking for (QA step 20, group 3) ---


def test_canonicalize_at_sign_survives_unescaped_in_path():
    # "@" is a legitimate path character (a handle in a URL shape like
    # "/users/@name") and is in _PATH_SAFE -- only netloc "@" (credentials)
    # is rejected. Pins the distinction the docstring draws between the two.
    assert canonicalize("https://example.com/users/@handle") == "https://example.com/users/@handle"


def test_canonicalize_already_percent_encoded_path_is_left_as_is_not_double_escaped():
    # "%" is in _PATH_SAFE specifically so re-canonicalizing an
    # already-canonical path is a no-op -- this is the idempotency
    # guarantee _safe_link()'s render-time re-check depends on.
    assert canonicalize("https://example.com/a%20b") == "https://example.com/a%20b"


def test_canonicalize_ipv6_host_keeps_its_brackets():
    result = canonicalize("https://[::1]/path")
    assert result == "https://[::1]/path"


def test_canonicalize_trailing_dot_on_host_is_treated_as_the_same_host():
    assert canonicalize("https://example.com./x") == canonicalize("https://example.com/x")


def test_canonicalize_percent_encoded_hex_case_does_not_affect_identity():
    assert canonicalize("https://example.com/a%3e") == canonicalize("https://example.com/a%3E")


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


def test_normalize_empty_item_list_returns_empty_list():
    result = normalize([], known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert result == []


def test_normalize_known_urls_not_called_with_empty_set_when_all_items_dropped():
    # Every candidate URL is non-http, so nothing survives to ask the store about.
    seen = {}

    def known_urls(urls):
        seen["urls"] = urls
        return set()

    items = [_item("javascript:alert(1)"), _item("data:text/html,x")]
    result = normalize(items, known_urls=known_urls, now=NOW, lookback=timedelta(hours=24))
    assert result == []
    assert seen["urls"] == set()


def test_normalize_in_batch_dedupe_official_beats_press_beats_community():
    items = [
        _item("https://example.com/a", trust="community", title="worst"),
        _item("https://example.com/a", trust="press", title="middle"),
        _item("https://example.com/a", trust="official", title="best"),
    ]
    result = normalize(items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24))
    assert len(result) == 1
    assert result[0].title == "best"


def test_canonicalize_strips_leading_www():
    assert canonicalize("https://www.pcgamesn.com/diablo-4/x") == canonicalize(
        "https://pcgamesn.com/diablo-4/x"
    )


# --- canonicalize_items: the shared canonicalize+dedupe phases, for the alert sweep ---


def test_canonicalize_items_canonicalizes_urls():
    items = [_item("https://example.com/a/?utm_source=feed")]
    result = canonicalize_items(items)
    assert [i.url for i in result] == ["https://example.com/a"]


def test_canonicalize_items_drops_non_http_urls():
    result = canonicalize_items([_item("javascript:alert(1)")])
    assert result == []


def test_canonicalize_items_in_batch_dedupe_first_seen_wins_on_tie():
    items = [
        _item("https://example.com/a", trust="press", title="first"),
        _item("https://example.com/a", trust="press", title="second"),
    ]
    result = canonicalize_items(items)
    assert len(result) == 1
    assert result[0].title == "first"


def test_canonicalize_items_in_batch_dedupe_prefers_higher_trust():
    items = [
        _item("https://example.com/a", trust="community", title="first"),
        _item("https://example.com/a", trust="official", title="second"),
    ]
    result = canonicalize_items(items)
    assert len(result) == 1
    assert result[0].trust == "official"


def test_canonicalize_items_does_not_touch_the_store_or_apply_a_lookback():
    # Unlike normalize(), canonicalize_items has no known_urls callback and
    # no lookback window -- a stale, previously-seen URL survives here.
    items = [
        _item(
            "https://example.com/old",
            published_at=NOW - timedelta(days=365),
        )
    ]
    result = canonicalize_items(items)
    assert [i.url for i in result] == ["https://example.com/old"]


def test_canonicalize_items_preserves_first_seen_order():
    items = [
        _item("https://example.com/b"),
        _item("https://example.com/a"),
    ]
    result = canonicalize_items(items)
    assert [i.url for i in result] == ["https://example.com/b", "https://example.com/a"]


def test_normalize_calls_canonicalize_items_under_the_hood():
    # Pins that normalize() didn't quietly diverge from canonicalize_items
    # when the shared phases were split out -- same canonicalize+dedupe
    # result either way, before normalize's own known_urls/lookback steps.
    items = [
        _item("https://example.com/a/?utm_source=feed", trust="community", title="first"),
        _item("https://example.com/a", trust="official", title="second"),
    ]
    via_canonicalize_items = canonicalize_items(items)
    via_normalize = normalize(
        items, known_urls=lambda urls: set(), now=NOW, lookback=timedelta(hours=24)
    )
    assert [i.url for i in via_normalize] == [i.url for i in via_canonicalize_items]
    assert [i.title for i in via_normalize] == [i.title for i in via_canonicalize_items]
