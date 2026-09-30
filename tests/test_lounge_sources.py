"""Tests for `load_source`: tmp_path for the files and caches, httpx.MockTransport for the net.

Quote text is synthetic (fake names, fake teapots) except for the Wikiquote
cases, which use the committed fixture pages and only count what comes out.
"""

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from newsbot.config import QuoteSourceCfg
from newsbot.lounge import sources
from newsbot.lounge.sources import LoadedSource, cache_dir_for, describe, load_source

FIXTURES = Path(__file__).parent / "fixtures" / "wikiquote"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
TEXT_HEADERS = {"content-type": "text/plain; charset=utf-8"}
FORTUNE = "The teapot is not a suspect.\n%\nMabel Quince knew where the biscuits were.\n"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _forbidden(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


def _fixture_html(name: str = "oscar_wilde.html") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _envelope(title: str, html: str, revid: int = 7) -> httpx.Response:
    body = {"parse": {"title": title, "revid": revid, "text": html}}
    return httpx.Response(200, content=json.dumps(body).encode(), headers=JSON_HEADERS)


def _wikiquote(title: str = "Oscar Wilde") -> QuoteSourceCfg:
    return QuoteSourceCfg(kind="wikiquote", value=title)


def _file(path: Path) -> QuoteSourceCfg:
    return QuoteSourceCfg(kind="file", value=str(path))


def _url(url: str = "https://example.test/quotes.txt") -> QuoteSourceCfg:
    return QuoteSourceCfg(kind="url", value=url)


async def _load(src, cache_dir, handler=_forbidden, now=NOW) -> LoadedSource:
    async with _client(handler) as http:
        return await load_source(src, http=http, cache_dir=cache_dir, now=now)


def _cache_files(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("*.json")) if cache_dir.exists() else []


def _cache_json(cache_dir: Path) -> dict:
    (path,) = _cache_files(cache_dir)
    return json.loads(path.read_text(encoding="utf-8"))


# --- describe and the cache directory ---


def test_describe_names_each_kind():
    assert describe(_wikiquote("oscar_Wilde")) == 'Wikiquote "Oscar Wilde"'
    assert (
        describe(QuoteSourceCfg(kind="file", value="/data/quotes.txt")) == "file /data/quotes.txt"
    )
    assert describe(_url()) == "url https://example.test/quotes.txt"


def test_cache_dir_is_per_database(tmp_path):
    prod = cache_dir_for(tmp_path / "newsbot.db")
    dev = cache_dir_for(tmp_path / "dev.db")
    assert prod == tmp_path / "newsbot-lounge-cache"
    assert dev == tmp_path / "dev-lounge-cache"
    assert prod != dev


# --- file sources ---


async def test_file_good_load_writes_the_cache(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"

    result = await _load(_file(path), cache_dir)

    assert [q.text for q in result.quotes] == [
        "The teapot is not a suspect.",
        "Mabel Quince knew where the biscuits were.",
    ]
    assert (result.origin, result.problem, result.saved_on) == ("fresh", None, None)
    data = _cache_json(cache_dir)
    assert data["body"] == FORTUNE
    assert data["key"] == _file(path).key
    assert data["version"] == 1


async def test_file_missing_names_the_resolved_path(tmp_path):
    path = tmp_path / "nope.txt"
    result = await _load(_file(path), tmp_path / "cache")
    assert result.quotes == []
    assert str(path) in result.problem


async def test_file_over_one_mib_fails(tmp_path):
    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * (1_048_576 + 1))
    result = await _load(_file(path), tmp_path / "cache")
    assert result.quotes == []
    assert "MiB" in result.problem


async def test_file_exactly_one_mib_is_allowed(tmp_path):
    path = tmp_path / "edge.txt"
    path.write_bytes(b"a short one\n%\n" + b" " * (1_048_576 - 14))
    result = await _load(_file(path), tmp_path / "cache")
    assert [q.text for q in result.quotes] == ["a short one"]


async def test_file_not_utf8_fails(tmp_path):
    path = tmp_path / "latin.txt"
    path.write_bytes("caf\xe9\n".encode("latin-1"))
    result = await _load(_file(path), tmp_path / "cache")
    assert result.quotes == []
    assert "UTF-8" in result.problem


async def test_file_bom_is_stripped(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbf" + FORTUNE.encode())
    result = await _load(_file(path), tmp_path / "cache")
    assert result.quotes[0].text == "The teapot is not a suspect."


async def test_file_directory_fails_cleanly(tmp_path):
    result = await _load(_file(tmp_path), tmp_path / "cache")
    assert result.quotes == []
    assert result.problem


async def test_file_cache_is_not_rewritten_when_the_body_is_unchanged(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    os.utime(cache_file, (1_000_000, 1_000_000))

    await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    assert cache_file.stat().st_mtime == 1_000_000

    path.write_text(FORTUNE + "%\nA third line of nonsense.\n", encoding="utf-8")
    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=2))
    assert len(result.quotes) == 3
    assert cache_file.stat().st_mtime != 1_000_000
    assert "third line" in _cache_json(cache_dir)["body"]


async def test_file_missing_after_a_good_load_falls_back_to_the_cache(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    path.unlink()

    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=3))

    assert result.origin == "fallback"
    assert len(result.quotes) == 2
    assert str(path) in result.problem
    assert result.saved_on == NOW.date()


async def test_file_with_no_entries_fails_and_keeps_the_cache(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    path.write_text("%\n\n%\n", encoding="utf-8")

    result = await _load(_file(path), cache_dir)

    assert result.origin == "fallback"
    assert "no usable quotes" in result.problem
    assert _cache_json(cache_dir)["body"] == FORTUNE


# --- url sources ---


def _text_response(body: bytes = FORTUNE.encode(), headers=TEXT_HEADERS) -> httpx.Response:
    return httpx.Response(200, content=body, headers=headers)


async def test_url_good_text_plain(tmp_path):
    cache_dir = tmp_path / "cache"
    result = await _load(_url(), cache_dir, lambda r: _text_response())
    assert len(result.quotes) == 2
    assert result.origin == "fresh"
    assert _cache_json(cache_dir)["body"] == FORTUNE


async def test_url_is_fetched_every_time(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return _text_response()

    await _load(_url(), tmp_path / "cache", handler)
    await _load(_url(), tmp_path / "cache", handler, now=NOW + timedelta(minutes=1))
    assert len(calls) == 2


async def test_url_html_says_to_use_the_raw_link(tmp_path):
    handler = lambda r: _text_response(b"<html></html>", {"content-type": "text/html"})  # noqa: E731
    result = await _load(_url(), tmp_path / "cache", handler)
    assert result.quotes == []
    assert "raw link" in result.problem


async def test_url_other_content_type_fails(tmp_path):
    handler = lambda r: _text_response(b"{}", {"content-type": "application/json"})  # noqa: E731
    result = await _load(_url(), tmp_path / "cache", handler)
    assert "application/json" in result.problem


async def test_url_404_fails(tmp_path):
    result = await _load(_url(), tmp_path / "cache", lambda r: httpx.Response(404))
    assert result.quotes == []
    assert "404" in result.problem


async def test_url_timeout_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "URL_TIMEOUT_S", 0.05)

    async def handler(request):
        await asyncio.sleep(2)
        return _text_response()

    result = await _load(_url(), tmp_path / "cache", handler)
    assert result.quotes == []
    assert "didn't answer" in result.problem


async def test_url_connection_error_fails(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route")

    result = await _load(_url(), tmp_path / "cache", handler)
    assert result.quotes == []
    assert "ConnectError" in result.problem


async def test_url_over_one_mib_fails(tmp_path):
    body = b"a\n%\n" * 300_000
    assert len(body) > 1_048_576
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body))
    assert result.quotes == []
    assert "MiB" in result.problem


async def test_url_redirect_through_http_is_rejected(tmp_path):
    def handler(request):
        if request.url.path == "/quotes.txt":
            return httpx.Response(302, headers={"location": "http://example.test/mid.txt"})
        if request.url.path == "/mid.txt":
            return httpx.Response(302, headers={"location": "https://example.test/end.txt"})
        return _text_response()

    result = await _load(_url(), tmp_path / "cache", handler)
    assert result.quotes == []
    assert "non-https" in result.problem


async def test_url_redirect_within_https_is_fine(tmp_path):
    def handler(request):
        if request.url.path == "/quotes.txt":
            return httpx.Response(302, headers={"location": "https://other.test/end.txt"})
        return _text_response()

    result = await _load(_url(), tmp_path / "cache", handler)
    assert len(result.quotes) == 2


async def test_url_plain_http_address_is_rejected(tmp_path):
    result = await _load(
        _url("http://example.test/q.txt"), tmp_path / "cache", lambda r: _text_response()
    )
    assert result.quotes == []
    assert "non-https" in result.problem


async def test_url_empty_200_fails_and_keeps_the_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())

    result = await _load(
        _url(), cache_dir, lambda r: _text_response(b""), now=NOW + timedelta(days=1)
    )

    assert result.origin == "fallback"
    assert "no usable quotes" in result.problem
    assert _cache_json(cache_dir)["body"] == FORTUNE


async def test_url_empty_200_with_no_cache_is_a_failure(tmp_path):
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(b""))
    assert result.quotes == []
    assert result.problem


async def test_url_declared_charset_is_honored(tmp_path):
    body = "Le caf\xe9 de Mabel Quince.\n".encode("latin-1")
    headers = {"content-type": "text/plain; charset=latin-1"}
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body, headers))
    assert result.quotes[0].text == "Le caf\xe9 de Mabel Quince."


async def test_url_without_charset_defaults_to_utf8(tmp_path):
    body = "Le caf\xe9 de Mabel Quince.\n".encode()
    headers = {"content-type": "text/plain"}
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body, headers))
    assert result.quotes[0].text == "Le caf\xe9 de Mabel Quince."


async def test_url_bad_bytes_for_the_charset_fail(tmp_path):
    body = b"\xff\xfe\xfa\n"
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body))
    assert result.quotes == []
    assert "couldn't be read" in result.problem


async def test_url_cache_is_keyed_by_url(tmp_path):
    cache_dir = tmp_path / "cache"
    await _load(_url("https://example.test/a.txt"), cache_dir, lambda r: _text_response())

    result = await _load(
        _url("https://example.test/b.txt"), cache_dir, lambda r: httpx.Response(500)
    )

    assert result.quotes == []
    assert "500" in result.problem
    assert len(_cache_files(cache_dir)) == 1


# --- Wikiquote sources ---


async def _seed_wikiquote(cache_dir: Path, when: datetime = NOW) -> LoadedSource:
    return await _load(
        _wikiquote(), cache_dir, lambda r: _envelope("Oscar Wilde", _fixture_html()), now=when
    )


async def test_wikiquote_good_fetch_saves_raw_html(tmp_path):
    cache_dir = tmp_path / "cache"
    result = await _seed_wikiquote(cache_dir)

    assert result.origin == "fresh"
    assert result.title == "Oscar Wilde"
    assert len(result.quotes) > 10
    assert result.quotes[0].link == "https://en.wikiquote.org/wiki/Oscar_Wilde"
    data = _cache_json(cache_dir)
    assert data["body"] == _fixture_html()
    assert data["title"] == "Oscar Wilde"
    assert data["revid"] == 7
    assert data["fetched_at"] == data["attempted_at"] == NOW.isoformat()


async def test_wikiquote_six_day_old_copy_makes_no_request(tmp_path):
    cache_dir = tmp_path / "cache"
    first = await _seed_wikiquote(cache_dir)

    result = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=6))

    assert result.origin == "saved-weekly"
    assert result.problem is None
    assert result.saved_on == NOW.date()
    assert len(result.quotes) == len(first.quotes)
    assert result.title == "Oscar Wilde"


async def test_wikiquote_eight_day_old_copy_triggers_a_fetch(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    calls = []

    def handler(request):
        calls.append(request)
        return _envelope("Oscar Wilde", _fixture_html(), revid=8)

    later = NOW + timedelta(days=8)
    result = await _load(_wikiquote(), cache_dir, handler, now=later)

    assert len(calls) == 1
    assert result.origin == "fresh"
    data = _cache_json(cache_dir)
    assert data["revid"] == 8
    assert data["fetched_at"] == later.isoformat()


async def test_wikiquote_failure_with_a_copy_falls_back_and_counts_the_attempt(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    day8 = NOW + timedelta(days=8)
    result = await _load(_wikiquote(), cache_dir, handler, now=day8)

    assert len(calls) == 1
    assert result.origin == "fallback"
    assert result.saved_on == NOW.date()
    assert "503" in result.problem
    assert len(result.quotes) > 10
    data = _cache_json(cache_dir)
    assert data["fetched_at"] == NOW.isoformat()
    assert data["attempted_at"] == day8.isoformat()

    # The next day: no request, because the attempt restarted the week.
    again = await _load(_wikiquote(), cache_dir, _forbidden, now=day8 + timedelta(days=1))
    assert again.origin == "saved-weekly"
    assert again.saved_on == NOW.date()


async def test_wikiquote_failure_with_no_copy_gives_a_problem(tmp_path):
    cache_dir = tmp_path / "cache"
    result = await _load(_wikiquote(), cache_dir, lambda r: httpx.Response(500))
    assert result.quotes == []
    assert "500" in result.problem
    assert _cache_files(cache_dir) == []


async def test_wikiquote_failure_with_no_copy_retries_on_the_next_load(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    cache_dir = tmp_path / "cache"
    await _load(_wikiquote(), cache_dir, handler)
    await _load(_wikiquote(), cache_dir, handler, now=NOW + timedelta(hours=1))
    assert len(calls) == 2


async def test_wikiquote_missing_page_message_reaches_the_problem(tmp_path):
    def handler(request):
        body = {"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}}
        return httpx.Response(200, content=json.dumps(body).encode(), headers=JSON_HEADERS)

    result = await _load(_wikiquote("Mabel Quince"), tmp_path / "cache", handler)
    assert "no page called Mabel Quince" in result.problem


async def test_wikiquote_zero_quotes_is_a_failure_and_keeps_the_old_copy(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)

    empty = "<h2>Quotes</h2><p>Nothing to see.</p>"
    later = NOW + timedelta(days=9)
    result = await _load(
        _wikiquote(), cache_dir, lambda r: _envelope("Oscar Wilde", empty), now=later
    )

    assert result.origin == "fallback"
    assert "no usable quotes" in result.problem
    assert _cache_json(cache_dir)["body"] == _fixture_html()


async def test_wikiquote_zero_quotes_with_no_copy_writes_nothing(tmp_path):
    cache_dir = tmp_path / "cache"
    empty = "<h2>Quotes</h2><p>Nothing to see.</p>"
    result = await _load(_wikiquote(), cache_dir, lambda r: _envelope("Oscar Wilde", empty))
    assert result.quotes == []
    assert _cache_files(cache_dir) == []


async def test_wikiquote_redirect_title_is_saved_and_used_for_the_link(tmp_path):
    cache_dir = tmp_path / "cache"
    handler = lambda r: _envelope("Oscar Wilde", _fixture_html())  # noqa: E731
    result = await _load(_wikiquote("Wilde, Oscar"), cache_dir, handler)

    assert result.title == "Oscar Wilde"
    assert {q.link for q in result.quotes} == {"https://en.wikiquote.org/wiki/Oscar_Wilde"}
    assert _cache_json(cache_dir)["title"] == "Oscar Wilde"

    saved = await _load(
        _wikiquote("Wilde, Oscar"), cache_dir, _forbidden, now=NOW + timedelta(days=1)
    )
    assert saved.title == "Oscar Wilde"
    assert {q.link for q in saved.quotes} == {"https://en.wikiquote.org/wiki/Oscar_Wilde"}


async def test_wikiquote_cached_html_is_reparsed_on_load(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    seen = []
    real = sources.parse_page

    def spy(title, html):
        seen.append((title, len(html)))
        return real(title, html)

    monkeypatch.setattr(sources, "parse_page", spy)
    await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=1))
    assert seen == [("Oscar Wilde", len(_fixture_html()))]


# --- the cache files themselves ---


async def test_corrupt_cache_is_treated_as_absent(tmp_path, caplog):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    cache_file.write_text("{ this is not json", encoding="utf-8")
    path.unlink()

    with caplog.at_level("WARNING"):
        result = await _load(_file(path), cache_dir)

    assert result.quotes == []
    assert any("unusable" in r.message for r in caplog.records)


async def test_cache_with_the_wrong_shape_is_treated_as_absent(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["fetched_at"] = "yesterday-ish"
    cache_file.write_text(json.dumps(data), encoding="utf-8")

    result = await _load(
        _wikiquote(), cache_dir, lambda r: httpx.Response(500), now=NOW + timedelta(days=1)
    )
    assert result.quotes == []


async def test_unwritable_cache_directory_still_returns_quotes(tmp_path, caplog):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")

    with caplog.at_level("WARNING"):
        result = await _load(_file(path), blocker / "cache")

    assert len(result.quotes) == 2
    assert result.problem is None
    assert any("Couldn't save" in r.message for r in caplog.records)
    assert list(tmp_path.glob("**/*.tmp")) == []


async def test_no_temp_files_are_left_behind(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    assert [p.suffix for p in cache_dir.iterdir()] == [".json"]


async def test_filenames_differ_for_keys_that_differ_only_in_case(tmp_path):
    a = QuoteSourceCfg(kind="url", value="https://example.test/Quotes.txt")
    b = QuoteSourceCfg(kind="url", value="https://example.test/quotes.txt")
    cache_dir = tmp_path / "cache"
    await _load(a, cache_dir, lambda r: _text_response())
    await _load(b, cache_dir, lambda r: _text_response())
    assert len(_cache_files(cache_dir)) == 2
