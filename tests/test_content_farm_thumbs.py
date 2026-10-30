"""Brave results whose thumbnail is hotlinked from Reddit or Bing's thumbnail search are dropped.

The pages in question are language-model rewrites of Reddit threads on hijacked or expired
domains (`/article/<slug>`, titles ending "(2026)"). They name the game, so the name check
rightly keeps them. What gives them away is the picture: they hotlink it from Reddit's image
hosts or Bing's thumbnail service, and real press hosts its own. Brave only sends its own proxy
URL, with the original base64url-encoded after `/g:ce/` (a `/` every 16 characters). That format
is undocumented, so every case where it doesn't decode cleanly has to keep the result.
"""

from __future__ import annotations

import base64
import logging

import httpx
import pytest

from newsbot.collectors.web_search import WebSearchCollector
from newsbot.config import Topic, WebSearchSource

PROXY = (
    "https://imgs.search.brave.com/PV5ntV4-Jknq8fLdrGGtgnVKoHFR3pRDlxf7Dw_ndmg/rs:fit:200:200:1:0"
)

REDDIT_PREVIEW = "https://preview.redd.it/abc123.jpeg?width=640&auto=webp&s=deadbeef"
BING_THUMB = "https://ts2.mm.bing.net/th?q=Wardogs+Reddit+Thread+%282026%29&w=200"
PRESS_IMAGE = "https://static0.gamerantimages.com/wordpress/wp-content/uploads/2026/10/a.jpg"


def _proxy(origin: str) -> str:
    """Encode `origin` the way Brave's image proxy does."""
    b64 = base64.urlsafe_b64encode(origin.encode()).decode().rstrip("=")
    chunks = [b64[i : i + 16] for i in range(0, len(b64), 16)]
    return f"{PROXY}/g:ce/{'/'.join(chunks)}"


def _result(slug: str, thumbnail=None, *, omit_thumbnail=False, title="Wardogs patch notes"):
    result = {
        "url": f"https://news.test/{slug}",
        "title": title,
        "description": "Wardogs gets a patch.",
    }
    if not omit_thumbnail:
        result["thumbnail"] = thumbnail
    return result


def _with_src(slug: str, src):
    return _result(slug, {"src": src})


async def _kept(results, topics=None):
    """URLs the collector keeps from a fake Brave answering with `results`."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": results}))
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press")

    async def no_sleep(_seconds: float) -> None:
        return None

    topics = topics or [Topic(key="wardogs", name="Wardogs", channel_id=1)]
    async with httpx.AsyncClient(transport=transport) as http:
        items = await WebSearchCollector(source, topics, "k", sleep=no_sleep).collect(http)
    return [i.url.removeprefix("https://news.test/") for i in items]


# --- end to end: the case that happened ---


async def test_farm_thumbnails_are_dropped_and_real_press_is_kept():
    results = [
        _with_src("reddit", _proxy(REDDIT_PREVIEW)),
        _with_src("bing", _proxy(BING_THUMB)),
        _with_src("press", _proxy(PRESS_IMAGE)),
    ]
    assert await _kept(results) == ["press"]


async def test_the_drop_is_logged_once_per_search_with_a_count(caplog):
    results = [
        _with_src("a", _proxy(REDDIT_PREVIEW)),
        _with_src("b", _proxy(BING_THUMB)),
        _with_src("press", _proxy(PRESS_IMAGE)),
    ]
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.web_search"):
        await _kept(results)
    drops = [r for r in caplog.records if "content-farm" in r.getMessage()]
    assert len(drops) == 1
    assert drops[0].levelno == logging.INFO
    assert "2" in drops[0].getMessage()
    assert drops[0].source_name == "Brave Search"
    assert drops[0].topic == "wardogs"
    assert drops[0].farm_dropped == 2


async def test_nothing_is_logged_when_nothing_was_dropped(caplog):
    with caplog.at_level(logging.INFO, logger="newsbot.collectors.web_search"):
        await _kept([_with_src("press", _proxy(PRESS_IMAGE))])
    assert not [r for r in caplog.records if "content-farm" in r.getMessage()]


@pytest.mark.parametrize("key,name", [("borderlands4", "Borderlands 4"), ("palworld", "Palworld")])
async def test_the_drop_applies_to_every_game(key, name):
    topic = Topic(key=key, name=name, channel_id=1)
    results = [
        _with_src("farm", _proxy(REDDIT_PREVIEW)),
        _with_src("press", _proxy(PRESS_IMAGE)),
    ]
    assert await _kept(results, [topic]) == ["press"]


# --- the hosts that mean "farm" ---


@pytest.mark.parametrize(
    "origin",
    [
        "https://preview.redd.it/a.jpeg?s=1",
        "https://external-preview.redd.it/a.jpeg?s=1",
        "https://i.redd.it/a.png",
        "http://i.redd.it/a.png",
        "https://cf.preview.redd.it/imo-there-should-be-a-thing-abc123.png?width=640&s=1",
        "https://v.redd.it/abc123/DASH_720.mp4",
        "https://old.redd.it/a.png",
        "https://a.b.c.redd.it/a.png",
        "https://xpreview.redd.it/a.jpeg",
        "https://redd.it/a.jpeg",
        "https://REDD.IT./a.jpeg",
        "https://CF.Preview.Redd.It./a.jpeg",
        "https://PREVIEW.Redd.It/a.jpeg",
        "https://preview.redd.it./a.jpeg",
        "https://external-preview.redd.it.:443/a.jpeg",
        "https://ts1.mm.bing.net/th?q=x",
        "https://ts2.mm.bing.net/th?q=x",
        "https://tse1.mm.bing.net/th?q=x",
        "https://TSE1.MM.Bing.Net./th?q=x",
        "https://ts3.mm.bing.net/th/id/OIP.abc?pid=Api",
    ],
)
async def test_farm_hosts_are_dropped(origin):
    assert await _kept([_with_src("farm", _proxy(origin))]) == []


# --- everything else keeps the result ---


@pytest.mark.parametrize(
    "origin",
    [
        PRESS_IMAGE,
        "https://images.gamewatcher.com/a.jpg",
        "https://reddit.com/a.png",
        "https://www.reddit.com/a.png",
        "https://styles.redditmedia.com/a.png",
        "https://preview.redd.it.evil.example/a.jpeg",
        "https://.preview.redd.it/a.jpeg",
        "https://notredd.it/a.jpeg",
        "https://xredd.it/a.jpeg",
        "https://redd.it.evil.example/a.jpeg",
        "https://cf.preview.redd.it.evil.example/a.jpeg",
        "https://redd.it-evil.example/a.jpeg",
        "https://notbing.net/th?q=x",
        "https://ts2.notmm.bing.net/th?q=x",
        "https://xmm.bing.net/th?q=x",
        "https://mm.bing.net.example/th?q=x",
        "https://ts2.mm.bing.net.example/th?q=x",
        "https://ts2.mm.bing.net/other?q=x",
        "https://ts2.mm.bing.net/",
        "https://mm.bing.net/th?q=x",
        "https://www.bing.com/th?q=x",
        "https://example.com/preview.redd.it/a.jpeg",
        "https://example.com/a.jpeg?u=https://preview.redd.it/a",
    ],
)
async def test_other_hosts_keep_the_result(origin):
    assert await _kept([_with_src("keep", _proxy(origin))]) == ["keep"]


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "thumbnail",
    [
        pytest.param(None, id="null"),
        pytest.param("https://preview.redd.it/a.jpeg", id="string"),
        pytest.param(["https://preview.redd.it/a.jpeg"], id="list"),
        pytest.param(42, id="number"),
        pytest.param({}, id="no-src"),
        pytest.param({"src": None}, id="src-null"),
        pytest.param({"src": 12345}, id="src-number"),
        pytest.param({"src": ["x"]}, id="src-list"),
        pytest.param({"src": ""}, id="src-empty"),
        pytest.param({"src": f"{PROXY}/{_b64(REDDIT_PREVIEW)}"}, id="no-g:ce-segment"),
        pytest.param({"src": f"{PROXY}/g:ce/"}, id="nothing-after-g:ce"),
        pytest.param({"src": f"{PROXY}/g:ce/!!!!/????"}, id="bad-base64"),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64(REDDIT_PREVIEW)[:-1]}.jpg"}, id="trailing-junk"),
        pytest.param({"src": f"{PROXY}/g:ce/aHR0c"}, id="impossible-length"),
        pytest.param(
            {"src": f"{PROXY}/g:ce/" + base64.urlsafe_b64encode(b"\xff\xfe\xfa").decode()},
            id="not-utf8",
        ),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64('preview.redd.it/a.jpeg')}"}, id="no-scheme"),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64('ftp://preview.redd.it/a.jpeg')}"}, id="ftp"),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64('https:///a.jpeg')}"}, id="no-host"),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64('https://preview .redd.it/a')}"}, id="space"),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64('https://[preview.redd.it/a')}"}, id="bad-ip"),
        pytest.param({"src": f"{PROXY}/g:ce/{_b64('not a url at all')}"}, id="not-a-url"),
    ],
)
async def test_anything_that_does_not_decode_cleanly_keeps_the_result(thumbnail):
    assert await _kept([_result("keep", thumbnail)]) == ["keep"]


async def test_a_result_with_no_thumbnail_key_is_kept():
    assert await _kept([_result("keep", omit_thumbnail=True)]) == ["keep"]


async def test_a_padded_encoding_still_decodes():
    """Brave strips the padding; a proxy that kept it would still be read."""
    padded = base64.urlsafe_b64encode(b"https://i.redd.it/a.png").decode()
    assert padded.endswith("=")
    assert await _kept([_with_src("farm", f"{PROXY}/g:ce/{padded}")]) == []
