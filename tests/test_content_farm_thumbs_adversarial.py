"""Adversarial tests for the content-farm thumbnail drop in `web_search`.

The builder's tests prove the happy paths: a Reddit or Bing thumbnail goes, a press image stays,
garbage keeps the result. These go looking for the seams around that:

- the `/g:ce/` marker showing up twice, in the wrong place, in the wrong case, or on a host
  that isn't Brave;
- payloads in the wrong alphabet, padded in the wrong places, or large enough to be rude;
- origins that look like a farm host and aren't (userinfo, punycode, lookalikes, ports, dots)
  and origins that are a farm host in a costume (full-width dots, `%74h`, `/./th`);
- what the drop does to everything around it: `exclude_domains`, the name check, dedupe,
  the log line's count across games, and the source's health.

The contract these pin (owner-approved):

- the drop applies when the decoded origin's host is exactly `preview.redd.it`,
  `external-preview.redd.it` or `i.redd.it` (case-insensitive, trailing dot ignored), or ends in
  `.mm.bing.net` with a path starting `/th`;
- anything that doesn't decode to an http(s) URL with a host keeps the result, and so does any
  other host (fail open);
- one INFO line per search when anything was dropped, a count and nothing else;
- a drop never crashes a search, never costs another result, and never reads as a failing source.

A few tests below record what the code does where the contract is silent. They say so in their
names, so that a later change to the behavior is a decision and not an accident.

No network, no real sleeping: `MockTransport` and a temp database.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from newsbot.collectors.web_search import WebSearchCollector
from newsbot.config import Topic, WebSearchSource, load_config
from newsbot.pipeline.collect import CollectionDeps, collect_web_search
from newsbot.store.db import connect, migrate

LOGGER = "newsbot.collectors.web_search"
PROXY = (
    "https://imgs.search.brave.com/PV5ntV4-Jknq8fLdrGGtgnVKoHFR3pRDlxf7Dw_ndmg/rs:fit:200:200:1:0"
)

REDDIT = "https://preview.redd.it/abc123.jpeg?width=640&s=deadbeef"
BING = "https://ts2.mm.bing.net/th?q=Wardogs+Reddit+Thread&w=200"
PRESS = "https://static0.gamerantimages.com/wordpress/wp-content/uploads/2026/10/a.jpg"

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _chunked(b64: str) -> str:
    return "/".join(b64[i : i + 16] for i in range(0, len(b64), 16))


def _proxy(origin: str) -> str:
    """Encode `origin` the way Brave's image proxy does."""
    return f"{PROXY}/g:ce/{_chunked(_b64(origin))}"


def _result(slug: str, src=None, *, title="Wardogs patch notes", url=None, **extra):
    result = {
        "url": url or f"https://news.test/{slug}",
        "title": title,
        "description": "Wardogs gets a patch.",
        "page_age": RECENT.isoformat(),
        **extra,
    }
    if src is not None:
        result["thumbnail"] = {"src": src}
    return result


WARDOGS = Topic(key="wardogs", name="Wardogs", channel_id=1)
PALWORLD = Topic(key="palworld", name="Palworld", channel_id=1)


async def _collect(results_for, topics=(WARDOGS,), *, exclude_domains=None):
    """Run the collector against a fake Brave and return its items.

    `results_for` is a list (every query gets it) or a callable taking the query string.
    """
    answer = results_for if callable(results_for) else (lambda _q: results_for)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": answer(request.url.params["q"])})
    )
    kwargs = {} if exclude_domains is None else {"exclude_domains": exclude_domains}
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press", **kwargs)

    async def no_sleep(_seconds: float) -> None:
        return None

    async with httpx.AsyncClient(transport=transport) as http:
        return await WebSearchCollector(source, list(topics), "k", sleep=no_sleep).collect(http)


async def _kept(results, **kw):
    return [i.url.removeprefix("https://news.test/") for i in await _collect(results, **kw)]


def _drops(caplog):
    return [r for r in caplog.records if r.name == LOGGER and "content-farm" in r.getMessage()]


# --- the /g:ce/ marker: where it is, how it's spelled, what follows it ---


@pytest.mark.parametrize(
    "src",
    [
        pytest.param(f"{PROXY}/G:CE/{_chunked(_b64(REDDIT))}", id="uppercase-marker"),
        pytest.param(f"{PROXY}/g%3Ace/{_chunked(_b64(REDDIT))}", id="percent-encoded-colon"),
        pytest.param(f"{PROXY}/g%3ACE/{_chunked(_b64(REDDIT))}", id="percent-encoded-upper"),
        pytest.param(f"{PROXY}/g:ce{_chunked(_b64(REDDIT))}", id="no-slash-after-marker"),
        pytest.param(f"{PROXY}/sm:1/g:ce/sm:1/{_chunked(_b64(REDDIT))}", id="options-around"),
        pytest.param(f"{PROXY}/g:ce/sm:1/{_chunked(_b64(REDDIT))}", id="option-with-colon-after"),
        pytest.param(f"{PROXY}/g:ce/plain/{_chunked(_b64(REDDIT))}", id="option-without-colon"),
        pytest.param(f"{PROXY}/g:ce/{_chunked(_b64(REDDIT))}?x=1", id="query-string-after"),
        pytest.param(f"{PROXY}/g:ce/{_chunked(_b64(REDDIT))}#frag", id="fragment-after"),
        pytest.param(f"{PROXY}/g:ce/{_chunked(_b64(REDDIT))}.jpg", id="extension-after"),
        pytest.param(f"{PROXY}/g:ce/{_chunked(_b64(REDDIT))} ", id="trailing-space"),
        pytest.param(f"{PROXY}/g:ce/{_chunked(_b64(REDDIT))}\n", id="trailing-newline"),
    ],
)
async def test_a_marker_or_tail_the_parser_does_not_expect_keeps_the_result(src):
    """Each of these is a farm thumbnail dressed wrong; fail open means it stays. That's a miss."""
    assert await _kept([_result("keep", src)]) == ["keep"]


async def test_a_second_marker_after_a_real_image_keeps_the_result():
    src = f"{PROXY}/g:ce/{_chunked(_b64(PRESS))}/g:ce/{_chunked(_b64(REDDIT))}"
    assert await _kept([_result("keep", src)]) == ["keep"]


async def test_a_farm_image_followed_by_a_second_marker_keeps_the_result():
    src = f"{PROXY}/g:ce/{_chunked(_b64(REDDIT))}/g:ce/{_chunked(_b64(PRESS))}"
    assert await _kept([_result("keep", src)]) == ["keep"]


async def test_a_marker_earlier_in_the_url_than_the_real_one_keeps_the_result():
    """The first `/g:ce/` wins, and what follows it has colons in it, so nothing decodes."""
    src = f"https://imgs.search.brave.com/g:ce/x/rs:fit/g:ce/{_chunked(_b64(REDDIT))}"
    assert await _kept([_result("keep", src)]) == ["keep"]


async def test_the_marker_in_a_query_string_before_the_payload_keeps_the_result():
    src = f"{PROXY}?next=/g:ce/{_b64(PRESS)}&x=/g:ce/{_chunked(_b64(REDDIT))}"
    assert await _kept([_result("keep", src)]) == ["keep"]


async def test_the_proxy_host_is_not_checked_when_the_marker_and_payload_are_there():
    """Records what it does: the rule reads the marker, not who sent it.

    A `src` on some other host that borrows Brave's `/g:ce/<base64>` shape is judged by its
    payload alone. That seems right to me: the payload is what says where the picture really
    lives, and a non-Brave host mimicking the format is not something I've seen.
    """
    src = f"https://cdn.example/x/g:ce/{_chunked(_b64(REDDIT))}"
    assert await _kept([_result("farm", src)]) == []


async def test_a_direct_farm_url_with_no_marker_is_not_judged():
    """Records what it does: no `/g:ce/` means no payload to read, so no verdict."""
    assert await _kept([_result("keep", REDDIT)]) == ["keep"]


async def test_only_src_is_read_not_other_thumbnail_fields():
    """Records what it does: Brave sends `src` and the contract names `src` only."""
    result = _result("keep", _proxy(PRESS))
    result["thumbnail"]["original"] = REDDIT
    assert await _kept([result]) == ["keep"]


# --- the payload: alphabet, padding, size ---


async def test_the_standard_alphabet_with_plus_and_slash_keeps_the_result():
    """A `+` isn't urlsafe, and a `/` is read as one of Brave's chunk breaks. Fail open."""
    origin = "https://i.redd.it/a?x=>>>???"
    standard = base64.b64encode(origin.encode()).decode()
    assert "+" in standard and "/" in standard  # the point of the input
    assert await _kept([_result("keep", f"{PROXY}/g:ce/{standard}")]) == ["keep"]


async def test_the_same_origin_in_the_urlsafe_alphabet_is_dropped():
    """The control for the test above: the origin itself is a farm image."""
    origin = "https://i.redd.it/a?x=>>>???"
    assert "_" in _b64(origin) or "-" in _b64(origin)
    assert await _kept([_result("farm", _proxy(origin))]) == []


async def test_standard_alphabet_that_happens_to_need_no_plus_or_slash_still_decodes():
    """When the two alphabets agree there's nothing to tell apart, and the farm still goes."""
    origin = "https://i.redd.it/a.png"
    assert base64.b64encode(origin.encode()).decode().rstrip("=") == _b64(origin)
    assert await _kept([_result("farm", _proxy(origin))]) == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(lambda r: f"{_b64(r)}==/{_b64(r)}", id="padding-then-more-payload"),
        pytest.param(lambda r: f"{_b64(r)}===", id="too-much-padding"),
        pytest.param(lambda r: f"{_b64(r)}====", id="four-padding-chars"),
        pytest.param(lambda r: f"{_b64(r)}=/=", id="padding-split-by-a-slash"),
        pytest.param(lambda r: f"={_b64(r)}", id="padding-first"),
        pytest.param(lambda r: f"{_b64(r)}=x", id="padding-then-letter"),
        pytest.param(lambda r: f"{_chunked(_b64(r))}%3D", id="percent-encoded-padding"),
        pytest.param(lambda r: f"{_b64(r)}  ", id="trailing-spaces"),
        pytest.param(lambda r: f" {_b64(r)}", id="leading-space"),
        pytest.param(lambda r: f"{_b64(r)}\t", id="trailing-tab"),
    ],
)
async def test_padding_in_the_wrong_place_keeps_the_result(payload):
    # An origin whose unpadded encoding is a whole number of groups, so `=` has no business here.
    origin = "https://i.redd.it/aaaaa.png"
    assert len(origin) % 3 == 0
    assert await _kept([_result("keep", f"{PROXY}/g:ce/{payload(origin)}")]) == ["keep"]


async def test_padding_at_the_end_where_it_belongs_is_read():
    origin = "https://i.redd.it/a.png"
    padded = base64.urlsafe_b64encode(origin.encode()).decode()
    assert padded.endswith("=")
    assert await _kept([_result("farm", f"{PROXY}/g:ce/{padded}")]) == []


async def test_slashes_anywhere_in_the_payload_are_just_dropped_before_decoding():
    """Brave breaks every 16 characters; the parser doesn't care where the breaks are."""
    b64 = _b64(REDDIT)
    ragged = "/".join(b64[i : i + 5] for i in range(0, len(b64), 5))
    assert await _kept([_result("farm", f"{PROXY}/g:ce/{ragged}")]) == []
    assert await _kept([_result("farm", f"{PROXY}/g:ce/{b64}")]) == []
    assert await _kept([_result("farm", f"{PROXY}/g:ce//{b64[:7]}///{b64[7:]}/")]) == []


async def test_a_huge_payload_is_read_and_kept_without_a_scene():
    """5 MB of origin URL: nothing to flag, and not a performance problem finding that out."""
    huge = _proxy("https://example.com/" + "a" * 5_000_000)
    started = time.perf_counter()
    assert await _kept([_result("keep", huge)]) == ["keep"]
    # The budget is there to catch a pathological slowdown, not to time ordinary runs.
    assert time.perf_counter() - started < 30.0


async def test_a_big_payload_that_is_a_farm_is_still_dropped():
    big = _proxy("https://preview.redd.it/" + "a" * 60_000)
    assert await _kept([_result("farm", big)]) == []


async def test_a_farm_url_past_httpxs_length_cap_keeps_the_result():
    """Records what it does: httpx refuses URLs over 65,536 characters, so it's undecodable.

    No real thumbnail is anywhere near this, so it's a miss nobody will notice.
    """
    over = _proxy("https://preview.redd.it/" + "a" * 70_000)
    assert await _kept([_result("keep", over)]) == ["keep"]


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param("/" * 2_000_000, id="slashes-only"),
        pytest.param("A" * 2_000_000, id="one-long-run-that-is-not-a-url"),
        pytest.param("!" * 2_000_000, id="invalid-characters"),
        pytest.param("/g:ce/" * 200_000, id="marker-repeated"),
        pytest.param("=" * 2_000_000, id="padding-only"),
    ],
)
async def test_hostile_payload_shapes_finish_quickly_and_keep_the_result(tail):
    started = time.perf_counter()
    assert await _kept([_result("keep", f"{PROXY}/g:ce/{tail}")]) == ["keep"]
    # The budget is there to catch a pathological slowdown, not to time ordinary runs.
    assert time.perf_counter() - started < 30.0


async def test_a_page_of_large_payloads_does_not_lose_the_real_result_among_them():
    results = [
        _result(f"farm{i}", _proxy("https://preview.redd.it/" + "a" * 60_000 + str(i)))
        for i in range(19)
    ]
    results.append(_result("press", _proxy(PRESS)))
    assert await _kept(results) == ["press"]


# --- the origin URL: lookalikes that must stay, costumes that must go ---


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://preview.redd.it@evil.example/a.jpg", id="farm-name-as-userinfo"),
        pytest.param("https://preview.redd.it:pw@evil.example/a.jpg", id="farm-name-as-user"),
        pytest.param("https://ts1.mm.bing.net@evil.example/th?q=x", id="bing-name-as-userinfo"),
        pytest.param("https://preview.redd.it\\@evil.example/a.jpg", id="backslash-then-at"),
        pytest.param("https://xn--preview-redd-it-xxxx.example/a.jpg", id="punycode-lookalike"),
        pytest.param("https://preview.redd.іt/a.jpg", id="cyrillic-i-lookalike"),
        pytest.param("https://preview.redd.it../a.jpg", id="two-trailing-dots"),
        pytest.param("https://.preview.redd.it/a.jpg", id="leading-dot-is-an-empty-label"),
        pytest.param("https://preview-redd.it/a.jpg", id="hyphen-for-dot"),
        pytest.param("https://b.thumbs.redditmedia.com/a.jpg", id="reddit-media-thumbs"),
        pytest.param("https://[::1]/a.jpg", id="ipv6-loopback"),
        pytest.param("https://[2606:4700::6810:84e5]:443/th", id="ipv6-with-port-and-th"),
        pytest.param("https://127.0.0.1/th", id="ipv4"),
        pytest.param("https://2130706433/th", id="ipv4-as-an-integer"),
        pytest.param("https://preview.redd.it%20/a.jpg", id="encoded-space-in-host"),
        pytest.param("https://preview%2Eredd.it/a.jpg", id="encoded-dot-in-host"),
        pytest.param("https://%70review.redd.it/a.jpg", id="encoded-letter-in-host"),
        pytest.param("https://preview.redd.it\\a.jpg", id="backslash-ends-the-host-in-a-browser"),
        pytest.param("https://ｐreview.redd.it/a.jpg", id="fullwidth-letter"),
    ],
)
async def test_lookalike_origins_keep_the_result(origin):
    assert await _kept([_result("keep", _proxy(origin))]) == ["keep"]


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://evil.example@preview.redd.it/a.jpg", id="userinfo-then-farm-host"),
        pytest.param("https://:@preview.redd.it/a.jpg", id="empty-userinfo"),
        pytest.param("https://v.redd.it/a.mp4", id="video-host"),
        pytest.param("https://old.redd.it/a.jpg", id="another-redd-it-host"),
        pytest.param("https://www.redd.it/a.jpg", id="www-redd-it"),
        pytest.param("https://redd.it/a.jpg", id="the-bare-domain"),
        pytest.param("https://a.preview.redd.it/a.jpg", id="subdomain-of-a-named-farm-host"),
        pytest.param("https://cf.preview.redd.it/a.png", id="the-live-miss"),
        pytest.param("https://a.b.c.d.redd.it/a.jpg", id="deep-subdomain"),
        pytest.param("https://pr\u0435view.redd.it/a.jpg", id="cyrillic-e-is-an-xn-label"),
        pytest.param("https://\u00fcber.redd.it/a.jpg", id="non-ascii-label-under-redd-it"),
        pytest.param("https://xn--bcher-kva.redd.it/a.jpg", id="punycode-label-under-redd-it"),
        pytest.param("https://" + "a" * 63 + ".redd.it/a.jpg", id="longest-legal-label"),
        pytest.param("https://preview.redd.it:8443/a.jpg", id="port"),
        pytest.param("https://preview.redd.it:/a.jpg", id="empty-port"),
        pytest.param("HTTPS://preview.redd.it/a.jpg", id="uppercase-scheme"),
        pytest.param("https://preview。redd。it/a.jpg", id="ideographic-full-stops"),
        pytest.param("https://preview.redd.it。/a.jpg", id="ideographic-trailing-stop"),
        pytest.param("https://preview．redd．it/a.jpg", id="fullwidth-full-stops"),
        pytest.param("https://preview.redd.it?x=1", id="no-path-just-query"),
        pytest.param("https://preview.redd.it#frag", id="no-path-just-fragment"),
        pytest.param("https://preview.redd.it", id="bare-host"),
        pytest.param("https://i.redd.it/%00", id="encoded-nul-in-path"),
        pytest.param("https://i.redd.it/" + "a/" * 500, id="deep-path"),
    ],
)
async def test_farm_hosts_in_costume_are_still_dropped(origin):
    """The connection goes to the farm host in each of these, so the verdict does too."""
    assert await _kept([_result("farm", _proxy(origin))]) == []


# Reddit's rule is "redd.it or any subdomain of it" (owner, after cf.preview.redd.it got through).
# The tests below lean on the edges of that: what merely ends in the same letters, and what httpx
# turns into something that ends in `.redd.it` without anyone having meant a subdomain.


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://xredd.it/a.jpg", id="letter-glued-on"),
        pytest.param("https://a.xredd.it/a.jpg", id="letter-glued-on-with-subdomain"),
        pytest.param("https://notredd.it/a.jpg", id="word-glued-on"),
        pytest.param("https://reddXit/a.jpg", id="x-for-the-dot"),
        pytest.param("https://a.reddXit/a.jpg", id="x-for-the-dot-with-subdomain"),
        pytest.param("https://redd.it.evil.example/a.jpg", id="redd-it-as-a-prefix"),
        pytest.param("https://a.redd.it.evil.example/a.jpg", id="subdomain-then-evil"),
        pytest.param("https://redd.it-evil.example/a.jpg", id="hyphen-after"),
        pytest.param("https://redd.it_/a.jpg", id="underscore-after"),
        pytest.param("https://a_redd.it/a.jpg", id="underscore-before"),
        pytest.param("https://redd.itt/a.jpg", id="extra-letter-after"),
        pytest.param("https://redd.it.com/a.jpg", id="redd-it-dot-com"),
        pytest.param("https://redd.io/a.jpg", id="other-tld"),
        pytest.param("https://reddit.com/a.jpg", id="reddit-com"),
        pytest.param("https://r.edd.it/a.jpg", id="dot-in-the-wrong-place"),
        pytest.param("https://it/a.jpg", id="just-the-tld"),
        pytest.param("https://a.redd.\u0456t/a.jpg", id="cyrillic-i-in-the-tld"),
        pytest.param("https://xn--redd-it.example/a.jpg", id="punycode-lookalike-of-the-domain"),
        pytest.param("https://a.redd.xn--t-evb/a.jpg", id="punycode-tld"),
        pytest.param("https://\uff52\uff45\uff44\uff44.it/a.jpg", id="fullwidth-redd"),
        pytest.param("https://a.redd.it\u200b/a.jpg", id="zero-width-space-after"),
        pytest.param("https://a\u200b.redd.it/a.jpg", id="zero-width-space-before-the-dot"),
        pytest.param("https://a.\u00adredd.it/a.jpg", id="soft-hyphen"),
    ],
)
async def test_hosts_that_only_end_in_the_same_letters_as_redd_it_keep_the_result(origin):
    assert await _kept([_result("keep", _proxy(origin))]) == ["keep"]


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://a redd.it/a.jpg", id="space-for-the-dot"),
        pytest.param("https://a .redd.it/a.jpg", id="space-before-the-dot"),
        pytest.param("https://preview%20.redd.it/a.jpg", id="encoded-space"),
        pytest.param("https://[preview.redd.it/a.jpg", id="open-bracket"),
        pytest.param("https://%5Bpreview.redd.it/a.jpg", id="encoded-bracket"),
        pytest.param("https://a%2eredd.it/a.jpg", id="encoded-dot"),
        pytest.param("https://a%2Eredd.it/a.jpg", id="encoded-dot-uppercase"),
        pytest.param("https://a%252eredd.it/a.jpg", id="double-encoded-dot"),
        pytest.param("https://a<b.redd.it/a.jpg", id="angle-bracket-httpx-escapes"),
        pytest.param("https://a^b.redd.it/a.jpg", id="caret-httpx-escapes"),
        pytest.param("https://.redd.it/a.jpg", id="leading-dot"),
        pytest.param("https://..redd.it/a.jpg", id="two-leading-dots"),
        pytest.param("https://a..redd.it/a.jpg", id="consecutive-dots"),
        pytest.param("https://a.b..redd.it/a.jpg", id="consecutive-dots-deeper"),
        pytest.param("https://redd.it../a.jpg", id="bare-domain-two-trailing-dots"),
        pytest.param("https://a.redd.it../a.jpg", id="subdomain-two-trailing-dots"),
        pytest.param("https://a.redd.it\x00/a.jpg", id="nul-after"),
        pytest.param("https://a\x00.redd.it/a.jpg", id="nul-before-the-dot"),
        pytest.param("https://a?b.redd.it/a.jpg", id="question-mark-ends-the-host"),
        pytest.param("https://a#b.redd.it/a.jpg", id="hash-ends-the-host"),
        pytest.param("https://a.redd.it:80:80/a.jpg", id="two-ports"),
        pytest.param("https://redd.it:abc/a.jpg", id="port-not-a-number"),
        pytest.param("https://[::1].redd.it/a.jpg", id="ipv6-then-a-domain"),
        pytest.param("https://[::ffff:redd.it]/a.jpg", id="ipv6-with-letters-in-it"),
        pytest.param("https://[::1]/a.jpg", id="ipv6"),
        pytest.param("https://\t.redd.it/a.jpg", id="tab-label"),
        pytest.param("https://a.redd.it\n/a.jpg", id="newline-after"),
    ],
)
async def test_hosts_httpx_escapes_or_mangles_keep_the_result(origin):
    """Ending in `.redd.it` after httpx's escaping is not the same as being under it.

    The guard in the app code keeps anything with a `%` or an empty label; the rest of these
    never leave httpx as a host at all (so there's nothing to judge) or come out as something
    else.
    """
    assert await _kept([_result("keep", _proxy(origin))]) == ["keep"]


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://i.redd.it/a.png", id="i"),
        pytest.param("https://v.redd.it/abc/DASH_720.mp4", id="v"),
        pytest.param("https://preview.redd.it/a.jpeg", id="preview"),
        pytest.param("https://external-preview.redd.it/a.jpeg", id="external-preview"),
        pytest.param("https://cf.preview.redd.it/a.png", id="cf-preview"),
        pytest.param("https://emoji.redd.it/a.png", id="emoji"),
        pytest.param("https://redd.it/a.png", id="bare-domain"),
        pytest.param("https://REDD.IT/a.png", id="bare-domain-uppercase"),
        pytest.param("https://I.Redd.It./a.png", id="mixed-case-trailing-dot"),
        pytest.param("https://i.redd.it:443/a.png", id="port"),
        pytest.param("https://us%20er:p%40ss@i.redd.it/a.png", id="encoded-userinfo"),
        pytest.param("https://i.redd.it/a%20b.png?x=%41&y=%", id="percent-in-path-and-query"),
        pytest.param("https://i.redd.it/a b.png", id="space-in-path"),
        pytest.param("https://i.redd.it/\u00fc.png", id="non-ascii-path"),
        pytest.param("https://1.2.3.4.redd.it/a.png", id="digits-are-still-labels"),
        pytest.param("https://0x7f.redd.it/a.png", id="hex-looking-label"),
        pytest.param("https://a-b.redd.it/a.png", id="hyphen-label"),
        pytest.param("https://-a.redd.it/a.png", id="leading-hyphen-label"),
        pytest.param("https://xn--bcher-kva.redd.it/a.png", id="punycode-label"),
        pytest.param("https://a.redd.it\u3002/a.png", id="ideographic-trailing-stop"),
        pytest.param("https://a\u3002redd\u3002it/a.png", id="ideographic-stops-throughout"),
    ],
)
async def test_the_guard_never_makes_a_real_redd_it_host_keep_its_result(origin):
    assert await _kept([_result("farm", _proxy(origin))]) == []


@pytest.mark.parametrize(
    "label",
    [
        pytest.param("a" * 63, id="63-chars"),
        pytest.param("a" * 64, id="64-chars"),
        pytest.param("a" * 300, id="300-chars"),
    ],
)
async def test_a_very_long_label_under_redd_it_is_dropped_without_a_crash(label):
    """Records what it does: httpx doesn't enforce DNS's 63-character label cap.

    A name that can't exist is still a name under redd.it, and nobody sends one anyway.
    """
    assert await _kept([_result("farm", _proxy(f"https://{label}.redd.it/a.png"))]) == []


async def test_a_host_of_two_hundred_labels_deep_under_redd_it_is_dropped_quickly():
    started = time.perf_counter()
    assert await _kept([_result("farm", _proxy("https://" + "a." * 200 + "redd.it/a"))]) == []
    # The budget is there to catch a pathological slowdown, not to time ordinary runs.
    assert time.perf_counter() - started < 30.0


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("javascript:alert(1)", id="javascript"),
        pytest.param("javascript://preview.redd.it/%0aalert(1)", id="javascript-with-host"),
        pytest.param("data:image/png;base64,AAAA", id="data"),
        pytest.param("file://preview.redd.it/a.jpg", id="file"),
        pytest.param("ws://preview.redd.it/a.jpg", id="websocket"),
        pytest.param("blob:https://preview.redd.it/1234", id="blob"),
        pytest.param("//preview.redd.it/a.jpg", id="protocol-relative"),
        pytest.param("https:preview.redd.it/a.jpg", id="no-slashes"),
        pytest.param("https:/preview.redd.it/a.jpg", id="one-slash"),
        pytest.param("https:///preview.redd.it/a.jpg", id="three-slashes"),
        pytest.param("  https://preview.redd.it/a.jpg", id="leading-spaces"),
        pytest.param("https://preview.redd.it/a.jpg\n", id="trailing-newline"),
        pytest.param("\nhttps://preview.redd.it/a.jpg", id="leading-newline"),
        pytest.param("https://preview.redd.it/a.jpg\r\n", id="trailing-crlf"),
        pytest.param("https://preview.\tredd.it/a.jpg", id="tab-in-host"),
        pytest.param("https://preview.redd.it\x00.evil/a.jpg", id="nul-in-host"),
        pytest.param("https://preview.redd.it:abc/a.jpg", id="port-not-a-number"),
        pytest.param("https://[preview.redd.it/a.jpg", id="unclosed-bracket"),
        pytest.param("", id="empty"),
    ],
)
async def test_origins_that_are_not_clean_http_urls_keep_the_result(origin):
    """Mostly these are a miss on a real farm; that's the price of failing open."""
    assert await _kept([_result("keep", _proxy(origin) if origin else f"{PROXY}/g:ce/")]) == [
        "keep"
    ]


async def test_an_origin_that_is_itself_a_brave_proxy_url_is_not_unwrapped():
    """Records what it does: one layer is peeled, and the host found (Brave's own) is fine."""
    inner = _proxy(REDDIT)
    assert await _kept([_result("keep", _proxy(inner))]) == ["keep"]


async def test_non_utf8_bytes_in_the_origin_keep_the_result():
    src = f"{PROXY}/g:ce/" + base64.urlsafe_b64encode(b"https://i.redd.it/\xff\xfe").decode()
    assert await _kept([_result("keep", src)]) == ["keep"]


async def test_a_lone_surrogate_in_src_does_not_crash_the_search():
    """JSON allows the escape `\\ud800` even though Python can't encode the string it makes."""
    body = (
        b'{"results": [{"url": "https://news.test/keep", "title": "Wardogs",'
        b' "thumbnail": {"src": "https://x.test/g:ce/\\ud800aGk"}}]}'
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")

    async def no_sleep(_seconds: float) -> None:
        return None

    async with httpx.AsyncClient(transport=transport) as http:
        items = await WebSearchCollector(source, [WARDOGS], "k", sleep=no_sleep).collect(http)
    assert [i.url for i in items] == ["https://news.test/keep"]


# --- the Bing path rule ---


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://ts1.mm.bing.net/th", id="th"),
        pytest.param("https://ts1.mm.bing.net/th?q=x", id="th-query"),
        pytest.param("https://ts1.mm.bing.net/th/id/OIP.abc?pid=Api", id="th-id"),
        pytest.param("https://ts1.mm.bing.net/thumbnail/a.jpg", id="thumbnail"),
        pytest.param("https://ts1.mm.bing.net/thx", id="th-prefix-of-anything"),
        pytest.param("https://ts1.mm.bing.net/th%3Fq", id="encoded-question-mark"),
        pytest.param("https://ts1.mm.bing.net/%74h?q=x", id="encoded-t"),
        pytest.param("https://ts1.mm.bing.net/./th?q=x", id="dot-segment"),
        pytest.param("https://ts1.mm.bing.net/a/../th?q=x", id="dot-dot-segment"),
        pytest.param("https://a.b.mm.bing.net/th?q=x", id="two-labels-deep"),
        pytest.param("https://ts1.mm.bing.net:443/th?q=x", id="port"),
        pytest.param("https://ts1.mm.bing.net。/th?q=x", id="ideographic-stop"),
        pytest.param("https://TS1.MM.BING.NET/th?q=x", id="uppercase-host"),
    ],
)
async def test_bing_hosts_with_a_path_starting_th_are_dropped(origin):
    """`/thumbnail` and `/thx` go too: the contract says "starts with /th", so I read it so."""
    assert await _kept([_result("farm", _proxy(origin))]) == []


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://ts1.mm.bing.net/TH?q=x", id="uppercase-path"),
        pytest.param("https://ts1.mm.bing.net/Th?q=x", id="mixed-case-path"),
        pytest.param("https://ts1.mm.bing.net//th?q=x", id="double-slash"),
        pytest.param("https://ts1.mm.bing.net/t?q=x", id="just-t"),
        pytest.param("https://ts1.mm.bing.net/ht/th", id="th-later-in-the-path"),
        pytest.param("https://ts1.mm.bing.net/?u=/th", id="th-in-the-query"),
        pytest.param("https://ts1.mm.bing.net/#/th", id="th-in-the-fragment"),
        pytest.param("https://ts1.mm.bing.net?q=1", id="no-path"),
        pytest.param("https://ts1.mm.bing.net/", id="root-only"),
        pytest.param("https://ts1.mm.bing.net/%2Fth", id="encoded-leading-slash-twice"),
        pytest.param("https://mm.bing.net./th?q=x", id="bare-mm-bing-net-with-dot"),
        pytest.param("https://ts1.explicit.bing.net/th?q=x", id="explicit-bing-net"),
        pytest.param("https://tse1.bing.net/th?q=x", id="bing-net-without-mm"),
        pytest.param("https://th.bing.com/th?id=x", id="bing-com"),
        pytest.param("https://ts1.mm.bing.net.evil.example/th", id="suffix-of-another-name"),
        pytest.param("https://evilmm.bing.net/th", id="mm-glued-on-without-a-dot"),
    ],
)
async def test_bing_lookalikes_and_paths_not_starting_th_keep_the_result(origin):
    """`explicit.bing.net` and `bing.com` are outside the contract; a real miss, if they exist."""
    assert await _kept([_result("keep", _proxy(origin))]) == ["keep"]


# --- through the collector: what the drop does to everything around it ---


async def test_a_page_of_twenty_farm_results_comes_back_empty_without_an_error():
    results = [_result(f"farm{i}", _proxy(REDDIT)) for i in range(20)]
    assert await _kept(results) == []


async def test_order_and_fields_of_kept_results_do_not_change():
    """The same press results with and without farms between them come out identical."""
    press = [_result("p1", _proxy(PRESS)), _result("p2"), _result("p3", _proxy(PRESS))]
    mixed = [
        _result("f1", _proxy(REDDIT)),
        press[0],
        _result("f2", _proxy(BING)),
        press[1],
        press[2],
        _result("f3", _proxy(REDDIT)),
    ]
    assert await _collect(mixed) == await _collect(press)


async def test_results_without_thumbnails_come_through_exactly_as_before():
    plain = [_result("a"), _result("b"), _result("c")]
    with_press_images = [_result(r["url"].rsplit("/", 1)[1], _proxy(PRESS)) for r in plain]
    assert await _collect(plain) == await _collect(with_press_images)


async def test_a_farm_result_beside_a_malformed_one_does_not_stop_the_rest():
    results = [
        "not an object",
        _result("farm", _proxy(REDDIT)),
        {"url": "https://news.test/no-title", "thumbnail": {"src": _proxy(REDDIT)}},
        _result("bad-title", _proxy(PRESS), title=2026),
        _result("press", _proxy(PRESS)),
    ]
    assert await _kept(results) == ["press"]


async def test_the_search_params_do_not_change():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")

    async def no_sleep(_s: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await WebSearchCollector(source, [WARDOGS], "k", sleep=no_sleep).collect(http)
    assert seen == [{"q": "Wardogs news", "freshness": "pd", "count": "20"}]


# --- exclude_domains and the farm rule, in either order ---


async def test_an_excluded_domain_wins_whatever_its_thumbnail_says():
    results = [
        _result("a", _proxy(PRESS), url="https://spam.test/a"),
        _result("b", _proxy(REDDIT), url="https://spam.test/b"),
        _result("c", _proxy(PRESS)),
    ]
    assert await _kept(results, exclude_domains=["spam.test"]) == ["c"]


async def test_an_excluded_farm_result_is_not_counted_as_a_farm_drop(caplog):
    results = [_result("b", _proxy(REDDIT), url="https://spam.test/b")]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await _kept(results, exclude_domains=["spam.test"]) == []
    assert _drops(caplog) == []


async def test_with_exclusions_off_a_farm_is_still_dropped():
    results = [_result("a", _proxy(REDDIT)), _result("b", _proxy(PRESS))]
    assert await _kept(results, exclude_domains=[]) == ["b"]


async def test_a_farm_drop_does_not_depend_on_the_results_own_domain():
    results = [
        _result("a", _proxy(REDDIT), url="https://www.reddit.com/r/wardogs/comments/1/x"),
        _result("b", _proxy(REDDIT), url="https://i.redd.it/page"),
        _result("c", _proxy(REDDIT), url="https://old-wine-shop.example/article/wardogs-2026"),
    ]
    assert await _kept(results) == []


# --- the log line ---


def _counts(caplog):
    counts = []
    for record in _drops(caplog):
        match = re.search(
            r"\b(\d+)\b[^\d]*content-farm|content-farm[^\d]*\b(\d+)\b", record.getMessage()
        )
        counts.append(int(match.group(1) or match.group(2)))
    return counts


async def test_two_games_in_one_pass_log_one_line_each_with_their_own_counts(caplog):
    def answer(query):
        if "Wardogs" in query:
            return [_result("w1", _proxy(REDDIT)), _result("w2", _proxy(BING)), _result("wp")]
        return [_result("p1", _proxy(REDDIT), title="Palworld news"), _result("pp")]

    with caplog.at_level(logging.INFO, logger=LOGGER):
        kept = await _kept(answer, topics=[WARDOGS, PALWORLD])
    assert kept == ["wp", "pp"]
    assert _counts(caplog) == [2, 1]


async def test_a_game_with_no_farms_next_to_one_with_farms_logs_only_the_second(caplog):
    def answer(query):
        if "Wardogs" in query:
            return [_result("wp", _proxy(PRESS))]
        return [_result("p1", _proxy(REDDIT), title="Palworld news")]

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _kept(answer, topics=[WARDOGS, PALWORLD])
    messages = [r.getMessage() for r in _drops(caplog)]
    assert len(messages) == 1
    assert "palworld" in messages[0]
    assert "wardogs" not in messages[0]


async def test_the_log_line_is_a_count_and_not_the_things_counted(caplog):
    results = [
        _result("secret-slug", _proxy(REDDIT), title="Super Secret Title"),
        _result("other-slug", _proxy(BING)),
    ]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _kept(results)
    (record,) = _drops(caplog)
    shown = record.getMessage() + repr(record.__dict__.get("args")) + repr(record.exc_info)
    for leak in ("secret-slug", "other-slug", "Super Secret", "redd.it", "bing.net", "imgs.search"):
        assert leak not in shown
    assert record.levelno == logging.INFO


async def test_a_farm_with_an_off_topic_title_still_counts_as_a_farm_drop(caplog):
    """Records what it does: the thumbnail rule runs before the name check, so it's counted.

    The count says how many results had a farm's picture, which is true whether or not the name
    check would also have shown them the door.
    """
    results = [_result("off", _proxy(REDDIT), title="Modern Warfare 4 patch notes")]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await _kept(results) == []
    assert _counts(caplog) == [1]


async def test_nothing_above_info_is_logged_for_a_drop(caplog):
    results = [_result("farm", _proxy(REDDIT)), _result("junk", f"{PROXY}/g:ce/!!!!")]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _kept(results)
    assert [r for r in caplog.records if r.levelno > logging.INFO] == []


async def test_results_that_are_kept_because_nothing_decoded_log_nothing(caplog):
    results = [_result("a", f"{PROXY}/g:ce/!!!!"), _result("b", "nonsense"), _result("c")]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await _kept(results) == ["a", "b", "c"]
    assert _drops(caplog) == []


# --- through the store: the name check, dedupe and source health ---

CONFIG = """\
home_guild_id: 100000000000000001
web_search:
  queries_per_game: 1
catalog:
  - key: wardogs
    name: Wardogs
    search_queries: ["wd query"]
  - key: palworld
    name: Palworld
    entities: [Pocketpair]
    search_queries: ["pal query"]
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    return load_config(path)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _stored(db_path) -> dict[str, list[str]]:
    """url -> the games it's tagged to, for every stored item."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT i.url, t.topic_key FROM items i LEFT JOIN item_topics t ON t.item_id = i.id"
        ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["url"], [])
        if row["topic_key"]:
            out[row["url"]].append(row["topic_key"])
    return out


async def _search(cfg, db_path, answer, games, alerts=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": answer(request.url.params["q"])})

    async def no_sleep(_s: float) -> None:
        return None

    async def alert(message: str) -> None:
        if alerts is not None:
            alerts.append(message)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        deps = CollectionDeps(
            cfg=cfg, db_path=db_path, http=http, collectors=[], now=lambda: NOW, alert=alert
        )
        return await collect_web_search(deps, games, "k", sleep=no_sleep)


async def test_a_farm_that_names_the_game_is_not_stored(cfg, db_path):
    """The case that happened: the name check keeps it, the thumbnail check doesn't."""
    answer = lambda _q: [  # noqa: E731
        _result("farm", _proxy(REDDIT), title="Wardogs Reddit Thread (2026)"),
        _result("press", _proxy(PRESS), title="Wardogs gets a patch"),
    ]
    await _search(cfg, db_path, answer, ["wardogs"])
    assert _stored(db_path) == {"https://news.test/press": ["wardogs"]}


async def test_the_drop_applies_to_every_game_in_the_store(cfg, db_path):
    def answer(query):
        game = query.split()[0]
        return [
            _result(f"farm-{game}", _proxy(REDDIT), title="Wardogs and Palworld"),
            _result(f"press-{game}", _proxy(PRESS), title="Wardogs and Palworld"),
        ]

    await _search(cfg, db_path, answer, ["wardogs", "palworld"])
    assert sorted(_stored(db_path)) == [
        "https://news.test/press-pal",
        "https://news.test/press-wd",
    ]


@pytest.mark.parametrize("farm_first", [True, False], ids=["farm-first", "real-first"])
async def test_a_farm_copy_of_a_real_url_leaves_the_real_one_stored(cfg, db_path, farm_first):
    url = "https://news.test/same"
    farm = _result("x", _proxy(REDDIT), title="Wardogs patch", url=url)
    real = _result("x", _proxy(PRESS), title="Wardogs patch", url=url)
    results = [farm, real] if farm_first else [real, farm]
    outcome = await _search(cfg, db_path, lambda _q: results, ["wardogs"])
    assert _stored(db_path) == {url: ["wardogs"]}
    assert outcome.new_items == 1


async def test_a_farm_copy_that_differs_only_by_tracking_junk_leaves_the_real_one_stored(
    cfg, db_path
):
    real = _result("x", _proxy(PRESS), title="Wardogs patch", url="https://news.test/same")
    farm = _result(
        "x", _proxy(REDDIT), title="Wardogs patch", url="https://news.test/same?utm_source=x"
    )
    await _search(cfg, db_path, lambda _q: [farm, real], ["wardogs"])
    assert _stored(db_path) == {"https://news.test/same": ["wardogs"]}


async def test_a_farm_in_one_games_search_does_not_hide_the_real_copy_in_anothers(cfg, db_path):
    url = "https://news.test/both"

    def answer(query):
        src = _proxy(REDDIT) if query.startswith("wd") else _proxy(PRESS)
        return [_result("x", src, title="Wardogs meets Palworld", url=url)]

    await _search(cfg, db_path, answer, ["wardogs", "palworld"])
    assert _stored(db_path) == {url: ["palworld"]}


async def test_a_page_of_only_farms_is_a_good_pass_not_a_failing_source(cfg, db_path):
    alerts: list[str] = []
    answer = lambda _q: [_result(f"farm{i}", _proxy(REDDIT)) for i in range(20)]  # noqa: E731
    outcome = await _search(cfg, db_path, answer, ["wardogs"], alerts)
    assert outcome.sources_total == 1
    assert outcome.sources_ok == 1
    assert outcome.failing_sources == ()
    assert outcome.new_items == 0
    assert alerts == []


async def test_many_all_farm_passes_in_a_row_never_alert_the_owner(cfg, db_path):
    """The failure alert fires at 12 in a row; an all-farm answer is a success each time."""
    alerts: list[str] = []
    answer = lambda _q: [_result("farm", _proxy(REDDIT))]  # noqa: E731
    for _ in range(15):
        outcome = await _search(cfg, db_path, answer, ["wardogs"], alerts)
        assert outcome.failing_sources == ()
        assert outcome.alerted_sources == ()
    assert alerts == []
