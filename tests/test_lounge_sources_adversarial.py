"""Adversarial tests for `load_source`, beyond `test_lounge_sources.py`.

The brief (test-engineer, 2026-09-29): `load_source` is the function the daily
quote leans on at 08:00 with nobody watching, and its one promise is that it
never raises. So most of this file is a small zoo of things going wrong (a
FIFO where a file should be, a server that drips bytes for a year, a cache
file that's technically JSON but a list) and the same three questions after
each: did we get a `LoadedSource` back, is `problem` one short line, and did
the copy on disk survive?

Everything is hermetic: `tmp_path` for files, `httpx.MockTransport` for the
net, synthetic quotes (fake teapots, fake Mabel Quince) apart from the
committed Wikiquote fixtures. Where a test pins a behavior the owner might
want different, the test's docstring says so, so changing it is a decision
and not a surprise. Where a test found an actual bug it's an `xfail(strict)`
with the reason spelled out, so the fix flips it to a loud XPASS.

One rule the whole file leans on: cancellation is not a failure.
`asyncio.CancelledError` is a `BaseException` and `load_source` catches only
`Exception`, so a cancelled load propagates, which is what shutdown wants.
"""

from __future__ import annotations

import asyncio
import errno
import gzip
import json
import logging
import os
import stat
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsbot.config import QuoteSourceCfg
from newsbot.lounge import sources
from newsbot.lounge.sources import LoadedSource, cache_dir_for, describe, load_source

FIXTURES = Path(__file__).parent / "fixtures" / "wikiquote"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
TEXT_HEADERS = {"content-type": "text/plain; charset=utf-8"}
FORTUNE = "The teapot is not a suspect.\n%\nMabel Quince knew where the biscuits were.\n"
MARKER = "BODY_MARKER-9f3a-DO-NOT-ECHO"
MIB = 1_048_576
# The cap on a problem, ellipsis included (plain_line counts it).
HARD_MAX_PROBLEM = 250

_running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0
needs_non_root = pytest.mark.skipif(_running_as_root, reason="root ignores file permissions")


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


def _file(path: Path | str) -> QuoteSourceCfg:
    return QuoteSourceCfg(kind="file", value=str(path))


def _url(url: str = "https://example.test/quotes.txt") -> QuoteSourceCfg:
    return QuoteSourceCfg(kind="url", value=url)


def _text_response(body: bytes = FORTUNE.encode(), headers=TEXT_HEADERS) -> httpx.Response:
    return httpx.Response(200, content=body, headers=headers)


def _wilde_ok(request: httpx.Request) -> httpx.Response:
    return _envelope("Oscar Wilde", _fixture_html())


async def _load(src, cache_dir, handler=_forbidden, now=NOW) -> LoadedSource:
    async with _client(handler) as http:
        return await load_source(src, http=http, cache_dir=cache_dir, now=now)


def _cache_files(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("*.json")) if cache_dir.exists() else []


def _cache_json(cache_dir: Path) -> dict:
    (path,) = _cache_files(cache_dir)
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_devices() -> list[Path]:
    return [Path(p) for p in ("/dev/null", "/dev/zero") if Path(p).exists()]


def _write_text(path: Path | str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def _unlink(path: Path | str) -> None:
    Path(path).unlink()


def _leftovers(root: Path) -> list[Path]:
    return sorted(root.rglob("*.tmp"))


def _assert_clean_failure(result: LoadedSource) -> None:
    """The shape every failed load must have, whatever went wrong."""
    assert isinstance(result, LoadedSource)
    assert result.quotes == []
    assert result.origin == "fresh"
    assert result.saved_on is None
    assert result.problem
    assert "\n" not in result.problem
    assert "\r" not in result.problem
    assert len(result.problem) <= HARD_MAX_PROBLEM


def _drop_cache_field(cache_dir: Path, *fields: str) -> None:
    (path,) = _cache_files(cache_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in fields:
        data.pop(field)
    path.write_text(json.dumps(data), encoding="utf-8")


def _edit_cache(cache_dir: Path, **changes) -> None:
    (path,) = _cache_files(cache_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


async def _seed_wikiquote(cache_dir: Path, when: datetime = NOW) -> LoadedSource:
    return await _load(_wikiquote(), cache_dir, _wilde_ok, now=when)


async def _seed_file(tmp_path: Path, body: str = FORTUNE) -> tuple[Path, Path]:
    path = tmp_path / "quotes.txt"
    path.write_text(body, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    result = await _load(_file(path), cache_dir)
    assert result.problem is None
    return path, cache_dir


# --- describe and cache_dir_for, the odd inputs ---


def test_cache_dir_for_takes_str_or_path_and_handles_odd_names():
    assert cache_dir_for("data/newsbot.db") == Path("data/newsbot-lounge-cache")
    assert cache_dir_for(Path("data/newsbot.db")) == cache_dir_for("data/newsbot.db")
    # No suffix at all: the cache dir must not collide with the database itself.
    assert cache_dir_for("data/newsbot") == Path("data/newsbot-lounge-cache")
    # Only the last suffix goes; the rest is part of the stem.
    assert cache_dir_for("data/news.bot.db") == Path("data/news.bot-lounge-cache")


def test_describe_strips_whitespace_and_canonicalizes_wikiquote_titles():
    assert describe(_wikiquote("  wilde_oscar  ")) == 'Wikiquote "Wilde oscar"'
    assert describe(QuoteSourceCfg(kind="url", value="  https://e.test/q.txt \n")) == (
        "url https://e.test/q.txt"
    )


# --- load_source never raises: file sources ---


async def test_file_directory_fifo_and_devices_fail_without_hanging(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    targets = [tmp_path, fifo]
    targets += _existing_devices()

    for target in targets:
        result = await asyncio.wait_for(_load(_file(target), tmp_path / "cache"), timeout=5)
        _assert_clean_failure(result)
        assert "regular file" in result.problem or "Is a directory" in result.problem


async def test_file_symlink_loop_fails_cleanly(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    _assert_clean_failure(await _load(_file(a), tmp_path / "cache"))


async def test_file_symlink_to_a_good_file_works(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text(FORTUNE, encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    result = await _load(_file(link), tmp_path / "cache")
    assert len(result.quotes) == 2


@needs_non_root
async def test_file_unreadable_fails_cleanly_and_names_the_path(tmp_path):
    path = tmp_path / "secret.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    path.chmod(0)
    try:
        result = await _load(_file(path), tmp_path / "cache")
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _assert_clean_failure(result)
    assert "secret.txt" in result.problem


@needs_non_root
async def test_file_in_an_unsearchable_directory_fails_cleanly(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "quotes.txt").write_text(FORTUNE, encoding="utf-8")
    locked.chmod(0)
    try:
        result = await _load(_file(locked / "quotes.txt"), tmp_path / "cache")
    finally:
        locked.chmod(stat.S_IRWXU)
    _assert_clean_failure(result)


@pytest.mark.parametrize(
    "value",
    [
        "/nonexistent/a\x00b.txt",
        "/nonexistent/a\ud800b.txt",
        "/nonexistent/line one\nline two.txt",
        "",
        "   ",
        "/" + "long/" * 400 + "quotes.txt",
    ],
    ids=["nul", "lone-surrogate", "newline", "empty", "blank", "very-long"],
)
async def test_file_pathological_paths_fail_cleanly(tmp_path, value):
    result = await _load(_file(value), tmp_path / "cache")
    _assert_clean_failure(result)


async def test_file_that_grows_past_the_cap_between_stat_and_read_is_rejected(
    tmp_path, monkeypatch
):
    path = tmp_path / "growing.txt"
    path.write_bytes(b"a\n%\n" + b"x" * MIB)  # really over the cap
    real_stat = Path.stat

    def small_lie(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self == path:
            fields = list(info)
            fields[6] = 10  # st_size
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "stat", small_lie)
    result = await _load(_file(path), tmp_path / "cache")
    _assert_clean_failure(result)
    assert "MiB" in result.problem
    assert _cache_files(tmp_path / "cache") == []


async def test_file_grown_past_the_cap_falls_back_to_the_saved_copy(tmp_path):
    path, cache_dir = await _seed_file(tmp_path)
    path.write_bytes(b"x" * (MIB + 1))
    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    assert result.origin == "fallback"
    assert len(result.quotes) == 2
    assert "MiB" in result.problem
    assert _cache_json(cache_dir)["body"] == FORTUNE


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xef\xbb\xbf",
        b"\xef\xbb\xbf\n\n",
        b"%\n%\n%\n",
        b"%\r\n%\r\n",
        b"  %  \n\t%\t\n",
        b"\n\n\n   \n",
        b"\xff\xfe\xfd",
        b"good quote\n%\n\xff",
        b"\x00\x00\x00",
    ],
    ids=[
        "empty",
        "bom-only",
        "bom-and-newlines",
        "only-percent-lines",
        "only-percent-crlf",
        "percent-with-spaces",
        "only-whitespace",
        "invalid-utf8",
        "invalid-utf8-after-a-good-quote",
        "nul-bytes",
    ],
)
async def test_file_degenerate_contents_are_a_clean_failure_or_quotes(tmp_path, content):
    """Anything with no usable quote is a failure; a NUL-only file is 'text' with one entry."""
    path = tmp_path / "quotes.txt"
    path.write_bytes(content)
    result = await _load(_file(path), tmp_path / "cache")
    if content == b"\x00\x00\x00":
        # Documenting: NULs are ordinary characters to split_fortune, so this
        # "loads" one three-NUL quote. Nothing here filters control characters.
        assert len(result.quotes) == 1
        return
    _assert_clean_failure(result)
    assert _cache_files(tmp_path / "cache") == []


async def test_file_invalid_utf8_problem_does_not_echo_the_quotes(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_bytes(b"Mabel Quince has opinions about teapots.\n%\n\xff\xfe")
    result = await _load(_file(path), tmp_path / "cache")
    _assert_clean_failure(result)
    assert "Mabel" not in result.problem
    assert "teapots" not in result.problem


_READ_FAILURES = [
    lambda: OSError(errno.EIO, "Input/output error"),
    lambda: OSError(errno.ENOSPC, "No space left on device"),
    lambda: OSError(),
    lambda: PermissionError(errno.EACCES, "Permission denied"),
    lambda: IsADirectoryError(errno.EISDIR, "Is a directory"),
    lambda: BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),
    lambda: TimeoutError(),
    lambda: MemoryError(),
    lambda: RecursionError("maximum recursion depth exceeded"),
    lambda: ValueError("first line\nsecond line\nthird line"),
    lambda: RuntimeError("x" * 10_000),
    lambda: Exception(),
    lambda: KeyError("missing"),
]


@pytest.mark.parametrize("make", _READ_FAILURES, ids=lambda m: repr(m()))
async def test_file_read_that_raises_anything_is_a_clean_failure(tmp_path, monkeypatch, make):
    def boom(path):
        raise make()

    monkeypatch.setattr(sources, "_read_file_sync", boom)
    _assert_clean_failure(await _load(_file(tmp_path / "q.txt"), tmp_path / "cache"))


async def test_a_bare_exception_still_says_something(tmp_path, monkeypatch):
    """An exception with an empty message gets its class name, not an empty problem."""

    def boom(path):
        raise OSError()

    monkeypatch.setattr(sources, "_read_file_sync", boom)
    result = await _load(_file(tmp_path / "q.txt"), tmp_path / "cache")
    assert result.problem == "OSError"


async def test_a_multiline_exception_message_is_cut_to_its_first_line(tmp_path, monkeypatch):
    def boom(path):
        raise ValueError("first line\nsecond line " + MARKER)

    monkeypatch.setattr(sources, "_read_file_sync", boom)
    result = await _load(_file(tmp_path / "q.txt"), tmp_path / "cache")
    assert result.problem == "first line"


@pytest.mark.parametrize("kind", ["file", "url"])
async def test_a_parser_exception_is_a_clean_failure(tmp_path, monkeypatch, kind):
    def boom(raw):
        raise RecursionError("parser fell over\nsecond line")

    monkeypatch.setattr(sources, "split_fortune", boom)
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    src = _file(path) if kind == "file" else _url()
    result = await _load(src, tmp_path / "cache", lambda r: _text_response())
    _assert_clean_failure(result)
    assert result.problem == "parser fell over"
    assert _cache_files(tmp_path / "cache") == []


async def test_a_parser_exception_while_falling_back_still_does_not_raise(tmp_path, monkeypatch):
    """Documenting: the fallback's own parse isn't guarded (the Wikiquote one is).

    The fresh load fails, then re-parsing the copy also raises, so the caller
    gets the *second* exception's text and the original reason is lost. It
    never raises, which is the contract; the message is just less helpful.
    """
    path, cache_dir = await _seed_file(tmp_path)
    path.unlink()

    def boom(raw):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(sources, "split_fortune", boom)
    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    _assert_clean_failure(result)
    assert result.problem == "parser exploded"


# --- load_source never raises: URL sources ---


_HTTPX_ERRORS = [
    lambda: httpx.ConnectError("no route"),
    lambda: httpx.ConnectTimeout("slow"),
    lambda: httpx.ReadTimeout("slow"),
    lambda: httpx.WriteTimeout("slow"),
    lambda: httpx.PoolTimeout("slow"),
    lambda: httpx.ReadError("reset"),
    lambda: httpx.WriteError("reset"),
    lambda: httpx.CloseError("reset"),
    lambda: httpx.NetworkError("net"),
    lambda: httpx.TransportError("transport"),
    lambda: httpx.RemoteProtocolError("garbage"),
    lambda: httpx.LocalProtocolError("garbage"),
    lambda: httpx.ProtocolError("garbage"),
    lambda: httpx.ProxyError("proxy"),
    lambda: httpx.UnsupportedProtocol("ftp"),
    lambda: httpx.TooManyRedirects("loop"),
    lambda: httpx.DecodingError("bad gzip"),
    lambda: httpx.HTTPError("plain"),
    lambda: httpx.InvalidURL("bad url"),
    lambda: httpx.StreamConsumed(),
    lambda: httpx.StreamClosed(),
    lambda: httpx.ResponseNotRead(),
    lambda: httpx.RequestNotRead(),
    lambda: httpx.CookieConflict("cookie"),
    lambda: TimeoutError(),
    lambda: TimeoutError("with a message"),
    lambda: ConnectionResetError(errno.ECONNRESET, "reset by peer"),
    lambda: OSError(errno.ENETUNREACH, "Network is unreachable"),
    lambda: ValueError("first\nsecond " + MARKER),
    lambda: Exception(),
]


@pytest.mark.parametrize("make", _HTTPX_ERRORS, ids=lambda m: type(m()).__name__)
async def test_url_every_transport_failure_is_a_clean_failure(tmp_path, make):
    def handler(request):
        raise make()

    result = await _load(_url(), tmp_path / "cache", handler)
    _assert_clean_failure(result)
    assert MARKER not in result.problem


@pytest.mark.parametrize("make", _HTTPX_ERRORS, ids=lambda m: type(m()).__name__)
async def test_url_every_transport_failure_falls_back_to_the_copy(tmp_path, make):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())

    def handler(request):
        raise make()

    result = await _load(_url(), cache_dir, handler, now=NOW + timedelta(days=1))
    assert result.origin == "fallback"
    assert len(result.quotes) == 2
    assert result.problem
    assert "\n" not in result.problem
    assert result.saved_on == NOW.date()


async def test_url_a_transport_raised_timeout_is_labeled_as_the_deadline(tmp_path):
    """Documenting: a bare TimeoutError from the transport reads as our own deadline.

    Nothing wrong with the outcome; the message just says "didn't answer within
    10 seconds" even if the transport gave up sooner. A real httpx timeout is
    an httpx.TimeoutException and gets its class name instead.
    """

    def handler(request):
        raise TimeoutError

    result = await _load(_url(), tmp_path / "cache", handler)
    assert "didn't answer" in result.problem


async def test_url_lone_surrogate_in_the_url_is_a_clean_failure(tmp_path):
    result = await _load(_url("https://example.test/a\ud800b"), tmp_path / "cache")
    _assert_clean_failure(result)


async def test_url_slow_drip_body_hits_the_overall_deadline(tmp_path, monkeypatch):
    """Each chunk arrives inside the per-read timeout; only the total deadline saves us."""
    monkeypatch.setattr(sources, "URL_TIMEOUT_S", 0.3)

    async def drip():
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"a\n%\n"

    started = asyncio.get_running_loop().time()
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(drip()))  # type: ignore[arg-type]
    elapsed = asyncio.get_running_loop().time() - started

    _assert_clean_failure(result)
    assert "didn't answer" in result.problem
    assert elapsed < 2.5


async def test_url_slow_drip_with_a_copy_falls_back(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())
    monkeypatch.setattr(sources, "URL_TIMEOUT_S", 0.2)

    async def drip():
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"new\n%\n"

    result = await _load(
        _url(),
        cache_dir,
        lambda r: _text_response(drip()),
        now=NOW + timedelta(days=1),  # type: ignore[arg-type]
    )
    assert result.origin == "fallback"
    assert [q.text for q in result.quotes][0] == "The teapot is not a suspect."


# --- cancellation is not a failure ---


async def test_cancelling_a_url_load_propagates_the_cancellation(tmp_path):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(30)
        return _text_response()

    async with _client(handler) as http:
        task = asyncio.create_task(
            load_source(_url(), http=http, cache_dir=tmp_path / "cache", now=NOW)
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled()
    assert _cache_files(tmp_path / "cache") == []


async def test_cancelling_a_wikiquote_load_propagates_the_cancellation(tmp_path):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(30)
        return _wilde_ok(request)

    async with _client(handler) as http:
        task = asyncio.create_task(
            load_source(_wikiquote(), http=http, cache_dir=tmp_path / "cache", now=NOW)
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled()


async def test_cancelling_a_url_load_leaves_the_saved_copy_alone(tmp_path):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())
    before = _cache_files(cache_dir)[0].read_bytes()
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(30)

    async with _client(handler) as http:
        task = asyncio.create_task(
            load_source(_url(), http=http, cache_dir=cache_dir, now=NOW + timedelta(days=1))
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _cache_files(cache_dir)[0].read_bytes() == before
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("kind", ["url", "wikiquote"])
async def test_a_cancelled_error_raised_inside_the_transport_is_not_swallowed(tmp_path, kind):
    """Documenting: even a CancelledError nobody asked for propagates.

    It's a BaseException, so `except Exception` lets it through. That's what
    we want when the task really is being cancelled, and it means a transport
    that raises one by mistake takes the caller down with it. I'd still
    rather that than a load that eats a shutdown.
    """

    def handler(request):
        raise asyncio.CancelledError

    src = _url() if kind == "url" else _wikiquote()
    with pytest.raises(asyncio.CancelledError):
        await _load(src, tmp_path / "cache", handler)


# --- URL sources: content types and charsets ---


@pytest.mark.parametrize(
    "content_type",
    [
        "TEXT/PLAIN",
        "text/plain;charset=UTF-8",
        "text/plain ; charset=utf-8",
        "Text/Plain; format=flowed; charset=utf-8",
        'text/plain; charset="utf-8"',
        "  text/plain",
        "text/plain; charset=UTF-8; boundary=odd",
    ],
)
async def test_url_text_plain_in_its_many_spellings_is_accepted(tmp_path, content_type):
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(headers={"content-type": content_type}),
    )
    assert len(result.quotes) == 2


@pytest.mark.parametrize(
    "content_type",
    ["text/plainx", "text/plain, text/html", "xtext/plain", "text/", "plain", "text/plain/x"],
)
async def test_url_lookalike_content_types_are_rejected(tmp_path, content_type):
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(headers={"content-type": content_type}),
    )
    _assert_clean_failure(result)
    assert "not text/plain" in result.problem


@pytest.mark.parametrize(
    "content_type", ["text/html", "TEXT/HTML", "text/html; charset=utf-8", " Text/Html "]
)
async def test_url_html_in_any_case_gets_the_raw_link_advice(tmp_path, content_type):
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(b"<html><body>" + MARKER.encode(), {"content-type": content_type}),
    )
    _assert_clean_failure(result)
    assert "raw link" in result.problem
    assert MARKER not in result.problem


async def test_url_missing_content_type_is_named_as_such(tmp_path):
    result = await _load(_url(), tmp_path / "cache", lambda r: httpx.Response(200, content=b"a"))
    _assert_clean_failure(result)
    assert "no content type" in result.problem


async def test_url_other_page_like_types_do_not_get_the_raw_link_advice(tmp_path):
    """Documenting: only exactly text/html earns the raw-link hint.

    An XHTML page (`application/xhtml+xml`) is just as much a web page, but
    gets the generic "not text/plain" message. Fine, I think; rare enough.
    """
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(b"<html/>", {"content-type": "application/xhtml+xml"}),
    )
    assert "not text/plain" in result.problem
    assert "raw link" not in result.problem


async def test_url_absurdly_long_content_type_stays_within_the_bound(tmp_path):
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(headers={"content-type": "application/" + "x" * 5000}),
    )
    _assert_clean_failure(result)


@pytest.mark.parametrize("charset", ["bogus", "hex", "rot13", "base64", "undefined"])
async def test_url_unusable_charsets_are_clean_failures(tmp_path, charset):
    """Includes Python's non-text codecs (`hex`, `rot13`) and the `undefined` codec.

    `undefined` raises UnicodeError, which the narrow except in the fetcher
    doesn't name, so it lands in the catch-all with a terse message. Same
    outcome as the others, which is all the contract asks.
    """
    headers = {"content-type": f"text/plain; charset={charset}"}
    result = await _load(
        _url(), tmp_path / "cache", lambda r: _text_response(b"ab\n%\ncd\n", headers)
    )
    _assert_clean_failure(result)


async def test_url_misdeclared_charset_that_happens_to_decode_is_accepted_as_mojibake(tmp_path):
    """Documenting: valid bytes in the wrong charset load as gibberish, not as a failure.

    Eight UTF-8 bytes declared as UTF-16 decode without complaint into a
    single nonsense "quote". Nothing here can tell; the owner will notice
    when it posts. An odd number of bytes, which UTF-16 can't decode, fails.
    """
    headers = {"content-type": "text/plain; charset=utf-16"}
    mojibake = await _load(
        _url(), tmp_path / "a", lambda r: _text_response(b"ab\n%\ncd\n", headers)
    )
    assert len(mojibake.quotes) == 1
    odd = await _load(_url(), tmp_path / "b", lambda r: _text_response(b"abc", headers))
    _assert_clean_failure(odd)


async def test_url_bogus_charset_names_the_charset_and_not_the_body(tmp_path):
    headers = {"content-type": "text/plain; charset=bogus"}
    result = await _load(
        _url(), tmp_path / "cache", lambda r: _text_response(MARKER.encode(), headers)
    )
    assert "bogus" in result.problem
    assert MARKER not in result.problem


async def test_url_long_charset_name_is_bounded(tmp_path):
    headers = {"content-type": "text/plain; charset=" + "z" * 4000}
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(b"a", headers))
    _assert_clean_failure(result)


@pytest.mark.parametrize("charset", ["latin-1", "LATIN-1", "iso-8859-1", "cp1252", "windows-1252"])
async def test_url_latin_charsets_decode(tmp_path, charset):
    headers = {"content-type": f"text/plain; charset={charset}"}
    body = "Le caf\xe9 de Mabel.\n".encode("latin-1")
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body, headers))
    assert result.quotes[0].text == "Le caf\xe9 de Mabel."


async def test_url_ascii_charset_with_high_bytes_fails(tmp_path):
    headers = {"content-type": "text/plain; charset=ascii"}
    body = b"Mabel " + MARKER.encode() + b" caf\xe9\n"
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body, headers))
    _assert_clean_failure(result)
    assert "ascii" in result.problem
    assert MARKER not in result.problem


async def test_url_utf8_bom_in_the_body_is_not_left_on_the_first_quote(tmp_path):
    body = b"\xef\xbb\xbf" + FORTUNE.encode()
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body))
    assert result.quotes[0].text == "The teapot is not a suspect."


async def test_url_empty_charset_parameter_falls_back_to_utf8(tmp_path):
    headers = {"content-type": "text/plain; charset="}
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(headers=headers))
    assert len(result.quotes) == 2


# --- URL sources: sizes, encodings, empties ---


def _padded(size: int) -> bytes:
    head = b"a short one\n%\n"
    return head + b" " * (size - len(head))


async def test_url_body_of_exactly_one_mib_is_accepted_and_one_more_is_not(tmp_path):
    ok = await _load(_url(), tmp_path / "a", lambda r: _text_response(_padded(MIB)))
    assert [q.text for q in ok.quotes] == ["a short one"]

    over = await _load(_url(), tmp_path / "b", lambda r: _text_response(_padded(MIB + 1)))
    _assert_clean_failure(over)
    assert "MiB" in over.problem


async def test_url_cap_counts_across_chunks(tmp_path):
    async def body(size):
        data = _padded(size)
        for i in range(0, len(data), 1000):
            yield data[i : i + 1000]

    ok = await _load(_url(), tmp_path / "a", lambda r: _text_response(body(MIB)))  # type: ignore[arg-type]
    assert len(ok.quotes) == 1
    over = await _load(_url(), tmp_path / "b", lambda r: _text_response(body(MIB + 1)))  # type: ignore[arg-type]
    assert "MiB" in over.problem


async def test_url_cap_applies_after_gzip_decompression(tmp_path):
    raw = b"a\n%\n" * 400_000  # 1.6 MB of text, a few KB on the wire
    packed = gzip.compress(raw)
    assert len(packed) < 100_000
    headers = {**TEXT_HEADERS, "content-encoding": "gzip"}
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(packed, headers))
    _assert_clean_failure(result)
    assert "MiB" in result.problem


async def test_url_gzip_bomb_is_rejected(tmp_path):
    """A 32 MiB run of zeros is about 32 KB gzipped. Rejected, and quickly.

    Documenting the limit: httpx inflates each network chunk whole before we
    see it, so peak memory is one chunk's worth of inflation, not the 1 MiB
    cap. That's the client library's behavior; the cap still bounds what we
    keep and parse.
    """
    packed = gzip.compress(b"\0" * (32 * MIB))
    headers = {**TEXT_HEADERS, "content-encoding": "gzip"}
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(packed, headers))
    _assert_clean_failure(result)
    assert "MiB" in result.problem


async def test_url_small_gzip_body_is_decoded(tmp_path):
    headers = {**TEXT_HEADERS, "content-encoding": "gzip"}
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(gzip.compress(FORTUNE.encode()), headers),
    )
    assert len(result.quotes) == 2


async def test_url_corrupt_gzip_is_a_clean_failure(tmp_path):
    headers = {**TEXT_HEADERS, "content-encoding": "gzip"}
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: _text_response(b"not gzip " + MARKER.encode(), headers),
    )
    _assert_clean_failure(result)
    assert MARKER not in result.problem


@pytest.mark.parametrize(
    "body",
    [b"", b"\n\n\n", b"%\n%\n%\n", b"%", b"\xef\xbb\xbf", b" \t\n%\n \n%\n", b"%\r\n%\r\n"],
    ids=[
        "empty",
        "newlines",
        "only-separators",
        "one-percent",
        "bom-only",
        "blank-entries",
        "crlf",
    ],
)
async def test_url_bodies_with_no_quotes_fail_and_write_nothing(tmp_path, body):
    result = await _load(_url(), tmp_path / "cache", lambda r: _text_response(body))
    _assert_clean_failure(result)
    assert "no usable quotes" in result.problem
    assert _cache_files(tmp_path / "cache") == []


async def test_url_non_200_statuses_are_named_and_never_echo_the_body(tmp_path):
    for status in (201, 204, 206, 301, 304, 400, 401, 403, 404, 410, 429, 500, 502, 503):
        result = await _load(
            _url(),
            tmp_path / f"c{status}",
            lambda r, s=status: httpx.Response(s, content=MARKER.encode(), headers=TEXT_HEADERS),
        )
        _assert_clean_failure(result)
        assert f"HTTP {status}" in result.problem
        assert MARKER not in result.problem


# --- URL sources: redirects ---


def _seen(handler_map) -> tuple[list[tuple[str, str]], Callable]:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.scheme, request.url.path))
        return handler_map(request)

    return seen, handler


async def test_url_https_to_https_redirect_makes_two_requests(tmp_path):
    def routes(request):
        if request.url.path == "/quotes.txt":
            return httpx.Response(301, headers={"location": "https://cdn.test/final.txt"})
        return _text_response()

    seen, handler = _seen(routes)
    result = await _load(_url(), tmp_path / "cache", handler)
    assert len(result.quotes) == 2
    assert seen == [("https", "/quotes.txt"), ("https", "/final.txt")]


async def test_url_http_hop_in_the_middle_is_never_requested(tmp_path):
    """An https, http, https chain stops at the http Location; the transport never sees it.

    Changed from "is requested and the chain is followed": redirects are now
    followed by hand and each Location is checked before the request, so no
    clear-text request leaves the machine (and the last https URL isn't
    requested either).
    """

    def routes(request):
        if request.url.path == "/quotes.txt":
            return httpx.Response(302, headers={"location": "http://example.test/mid.txt"})
        return _text_response()

    seen, handler = _seen(routes)
    result = await _load(_url(), tmp_path / "cache", handler)

    _assert_clean_failure(result)
    assert "non-https" in result.problem
    assert "http://example.test/mid.txt" in result.problem
    assert seen == [("https", "/quotes.txt")]
    assert _cache_files(tmp_path / "cache") == []


async def test_url_redirect_ending_on_http_is_rejected_after_one_request(tmp_path):
    """Changed from two requests: the http Location is refused before it's requested."""

    def routes(request):
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://example.test/end.txt"})
        return _text_response()

    seen, handler = _seen(routes)
    result = await _load(_url(), tmp_path / "cache", handler)
    _assert_clean_failure(result)
    assert "non-https" in result.problem
    assert seen == [("https", "/quotes.txt")]


async def test_url_plain_http_source_is_rejected_without_a_request(tmp_path):
    """Changed from "sends its one request": an `http://` source URL makes no request at all."""
    seen, handler = _seen(lambda r: _text_response())
    result = await _load(_url("http://example.test/q.txt"), tmp_path / "cache", handler)
    _assert_clean_failure(result)
    assert "non-https" in result.problem
    assert seen == []


async def test_url_http_hop_falls_back_to_the_saved_copy(tmp_path):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())

    def routes(request):
        if request.url.path == "/quotes.txt":
            return httpx.Response(302, headers={"location": "http://example.test/x.txt"})
        return _text_response(b"attacker quote\n")

    result = await _load(_url(), cache_dir, routes, now=NOW + timedelta(days=1))
    assert result.origin == "fallback"
    assert [q.text for q in result.quotes][0] == "The teapot is not a suspect."
    assert _cache_json(cache_dir)["body"] == FORTUNE


def _chain(length: int, scheme: str = "https") -> Callable[[httpx.Request], httpx.Response]:
    def routes(request):
        step = int(request.url.path.strip("/") or 0)
        if step < length:
            return httpx.Response(302, headers={"location": f"{scheme}://example.test/{step + 1}"})
        return _text_response()

    return routes


async def test_url_a_long_redirect_chain_is_cut_off_after_five_hops(tmp_path):
    """Changed from "cut off by httpx" (its limit of 20): we follow five hops ourselves."""
    seen, handler = _seen(_chain(50))
    result = await _load(_url("https://example.test/0"), tmp_path / "cache", handler)
    _assert_clean_failure(result)
    assert "redirected more than 5 times" in result.problem
    assert len(seen) == 6  # the first request plus five hops


async def test_url_a_chain_of_five_hops_still_loads_and_six_does_not(tmp_path):
    ok = await _load(_url("https://example.test/0"), tmp_path / "a", _chain(5))
    assert len(ok.quotes) == 2
    over = await _load(_url("https://example.test/0"), tmp_path / "b", _chain(6))
    _assert_clean_failure(over)
    assert "redirected more than 5 times" in over.problem


async def test_url_a_redirect_loop_terminates(tmp_path):
    seen, handler = _seen(
        lambda r: httpx.Response(302, headers={"location": "https://example.test/quotes.txt"})
    )
    result = await _load(_url(), tmp_path / "cache", handler)
    _assert_clean_failure(result)
    assert "redirected more than 5 times" in result.problem
    assert len(seen) == 6


async def test_url_relative_location_is_resolved_against_the_current_url(tmp_path):
    def routes(request):
        if request.url.path == "/a/quotes.txt":
            return httpx.Response(302, headers={"location": "../b/final.txt"})
        return _text_response()

    seen, handler = _seen(routes)
    result = await _load(_url("https://example.test/a/quotes.txt"), tmp_path / "cache", handler)
    assert len(result.quotes) == 2
    assert seen == [("https", "/a/quotes.txt"), ("https", "/b/final.txt")]


async def test_url_scheme_relative_location_stays_on_https(tmp_path):
    def routes(request):
        if request.url.path == "/quotes.txt":
            return httpx.Response(302, headers={"location": "//cdn.test/final.txt"})
        return _text_response()

    result = await _load(_url(), tmp_path / "cache", routes)
    assert len(result.quotes) == 2


async def test_url_redirect_problems_do_not_echo_credentials(tmp_path):
    url = "https://user:hunter2@example.test/quotes.txt?token=abc123"

    def routes(request):
        return httpx.Response(
            302, headers={"location": "http://user:hunter2@evil.test/x?token=abc123"}
        )

    result = await _load(_url(url), tmp_path / "cache", routes)
    _assert_clean_failure(result)
    assert "non-https" in result.problem
    assert "hunter2" not in result.problem
    assert "abc123" not in result.problem


async def test_url_redirect_to_an_invalid_location_fails(tmp_path):
    result = await _load(
        _url(),
        tmp_path / "cache",
        lambda r: httpx.Response(302, headers={"location": "https://exa mple.test:99999999/x"}),
    )
    _assert_clean_failure(result)


async def test_url_redirect_without_a_location_is_a_plain_status_failure(tmp_path):
    result = await _load(_url(), tmp_path / "cache", lambda r: httpx.Response(302))
    _assert_clean_failure(result)
    assert "HTTP 302" in result.problem


# --- the cache: hostile files ---


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        '"a string"',
        "42",
        "true",
        "{}",
        "{",
        "",
        "[" * 200_000,
        '{"version": 1}',
        b"\xff\xfe\x00\x01",
        b"\x00" * 2048,
        "﻿" + json.dumps({"version": 1}),
    ],
    ids=[
        "list",
        "null",
        "string",
        "int",
        "bool",
        "empty-object",
        "truncated",
        "empty-file",
        "deeply-nested",
        "version-only",
        "invalid-utf8",
        "nul-bytes",
        "bom-then-json",
    ],
)
async def test_garbage_cache_file_is_ignored_and_then_healed(tmp_path, raw):
    path, cache_dir = await _seed_file(tmp_path)
    (cache_file,) = _cache_files(cache_dir)
    cache_file.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))

    # The source is fine: the load succeeds and rewrites a healthy copy.
    healed = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    assert healed.origin == "fresh"
    assert _cache_json(cache_dir)["body"] == FORTUNE

    # Break it again and take the source away: garbage means no fallback.
    cache_file.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    path.unlink()
    gone = await _load(_file(path), cache_dir, now=NOW + timedelta(days=2))
    _assert_clean_failure(gone)


_FUTURE = "9999-12-31T23:59:59+00:00"
_ANCIENT = "0001-01-01T00:00:00+00:00"

_BAD_WIKIQUOTE_CACHES = {
    "version-2": {"version": 2},
    "version-string": {"version": "1"},
    "version-missing": ("drop", "version"),
    "other-key": {"key": "wikiquote:Somebody Else"},
    "key-missing": ("drop", "key"),
    "body-int": {"body": 12},
    "body-null": {"body": None},
    "body-list": {"body": ["<p>x</p>"]},
    "title-int": {"title": 5},
    "title-list": {"title": ["Oscar Wilde"]},
    "revid-string": {"revid": "7"},
    "revid-float": {"revid": 7.5},
    "revid-list": {"revid": [7]},
    "fetched-null": {"fetched_at": None},
    "fetched-int": {"fetched_at": 1_790_000_000},
    "fetched-naive": {"fetched_at": "2026-09-29T12:00:00"},
    "fetched-date-only": {"fetched_at": "2026-09-29"},
    "fetched-junk": {"fetched_at": "yesterday-ish"},
    "fetched-empty": {"fetched_at": ""},
    "attempted-naive": {"attempted_at": "2026-09-29T12:00:00"},
    "attempted-null": {"attempted_at": None},
    "attempted-dict": {"attempted_at": {"when": "now"}},
    "attempted-missing": ("drop", "attempted_at"),
}


@pytest.mark.parametrize("name", sorted(_BAD_WIKIQUOTE_CACHES))
async def test_wikiquote_cache_with_a_bad_field_is_treated_as_absent(tmp_path, name):
    """The copy is ignored (so a fetch happens, even inside its week) and then replaced."""
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    changes = _BAD_WIKIQUOTE_CACHES[name]
    if isinstance(changes, tuple):
        _drop_cache_field(cache_dir, changes[1])
    else:
        _edit_cache(cache_dir, **changes)
    calls: list[httpx.Request] = []

    def failing(request):
        calls.append(request)
        return httpx.Response(500)

    later = NOW + timedelta(hours=1)
    result = await _load(_wikiquote(), cache_dir, failing, now=later)
    _assert_clean_failure(result)
    assert len(calls) == 1

    # And a good fetch afterwards replaces the bad file with a good one.
    fresh = await _load(_wikiquote(), cache_dir, _wilde_ok, now=later)
    assert fresh.origin == "fresh"
    again = await _load(_wikiquote(), cache_dir, _forbidden, now=later + timedelta(hours=1))
    assert again.origin == "saved-weekly"


async def test_cache_path_replaced_by_a_directory_does_not_stop_the_load(tmp_path, caplog):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    cache_file.unlink()
    cache_file.mkdir()
    (cache_file / "occupant.txt").write_text("i live here now", encoding="utf-8")

    with caplog.at_level("WARNING"):
        result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))

    assert len(result.quotes) == 2
    assert result.problem is None
    assert cache_file.is_dir()
    assert (cache_file / "occupant.txt").exists()
    assert _leftovers(tmp_path) == []
    assert any("Couldn't save" in r.message for r in caplog.records)


async def test_cache_path_replaced_by_a_directory_counts_as_no_copy(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    cache_file.unlink()
    cache_file.mkdir()
    path.unlink()
    _assert_clean_failure(await _load(_file(path), cache_dir))


@needs_non_root
async def test_read_only_cache_directory_costs_nothing_but_a_warning(tmp_path, caplog):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with caplog.at_level("WARNING"):
            result = await _load(_file(path), cache_dir)
    finally:
        cache_dir.chmod(stat.S_IRWXU)
    assert len(result.quotes) == 2
    assert result.problem is None
    assert _leftovers(tmp_path) == []
    assert any("Couldn't save" in r.message for r in caplog.records)


@needs_non_root
async def test_unreadable_cache_file_is_treated_as_absent(tmp_path):
    path, cache_dir = await _seed_file(tmp_path)
    (cache_file,) = _cache_files(cache_dir)
    cache_file.chmod(0)
    path.unlink()
    try:
        result = await _load(_file(path), cache_dir)
    finally:
        cache_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _assert_clean_failure(result)


async def test_cache_whose_body_is_one_giant_entry_parses_to_nothing_and_is_absent(tmp_path):
    """A hand-edited 3 MiB copy (no fetch could have written it) can't be used.

    The copy skips the 1 MiB read cap, so this pins the backstop: one entry
    that big is "too long" for a Discord message and the copy parses to zero
    quotes, which makes it absent.
    """
    path, cache_dir = await _seed_file(tmp_path)
    _edit_cache(cache_dir, body="word " * 600_000)
    path.unlink()
    result = await _load(_file(path), cache_dir)
    _assert_clean_failure(result)


async def test_cache_with_thousands_of_entries_is_usable(tmp_path):
    path, cache_dir = await _seed_file(tmp_path)
    _edit_cache(cache_dir, body="\n%\n".join(f"synthetic teapot number {i}" for i in range(20_000)))
    path.unlink()
    result = await _load(_file(path), cache_dir)
    assert result.origin == "fallback"
    assert len(result.quotes) == 20_000


async def test_cache_from_another_source_under_this_sources_name_is_ignored(tmp_path):
    """Simulated hash collision: A's file sits at B's filename. B must not use it."""
    a_path = tmp_path / "a.txt"
    a_path.write_text("alpha teapot\n", encoding="utf-8")
    b_path = tmp_path / "b.txt"
    b_path.write_text("beta teapot\n", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(a_path), cache_dir)
    (a_cache,) = _cache_files(cache_dir)
    b_cache = cache_dir / sources._cache_name(_file(b_path).key)
    b_cache.write_bytes(a_cache.read_bytes())
    b_path.unlink()

    result = await _load(_file(b_path), cache_dir)
    _assert_clean_failure(result)

    b_path.write_text("beta teapot\n", encoding="utf-8")
    good = await _load(_file(b_path), cache_dir, now=NOW + timedelta(days=1))
    assert [q.text for q in good.quotes] == ["beta teapot"]
    assert json.loads(b_cache.read_text(encoding="utf-8"))["key"] == _file(b_path).key


async def test_wikiquote_cache_from_another_page_is_ignored_even_inside_its_week(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    other = _wikiquote("Somebody Else")
    other_cache = cache_dir / sources._cache_name(other.key)
    other_cache.write_bytes(cache_file.read_bytes())
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    result = await _load(other, cache_dir, handler, now=NOW + timedelta(days=1))
    _assert_clean_failure(result)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "value",
    ["../../etc/passwd", "/", "a/b/c", "..", ".", "x" * 5000, "wiki\x00quote", "café/über"],
)
async def test_weird_keys_never_write_outside_the_cache_directory(tmp_path, value):
    cache_dir = tmp_path / "sandbox" / "cache"
    src = _wikiquote(value)
    await _load(src, cache_dir, _wilde_ok)
    written = [p for p in (tmp_path / "sandbox").rglob("*") if p.is_file()]
    assert all(p.parent == cache_dir for p in written)
    assert all(p.suffix == ".json" for p in written)


async def test_lone_surrogate_page_title_is_a_clean_failure(tmp_path):
    src = _wikiquote("Oscar \ud800 Wilde")
    assert isinstance(describe(src), str)
    result = await _load(src, tmp_path / "cache", _wilde_ok)
    # Either the request can't be built (a failure) or it loads; it must not raise.
    assert isinstance(result, LoadedSource)
    if result.quotes == []:
        _assert_clean_failure(result)


# --- the cache: timestamps ---


async def test_wikiquote_timestamps_in_the_far_future_or_past_do_not_crash(tmp_path):
    for stamp in (_FUTURE, _ANCIENT, "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-23:59"):
        cache_dir = tmp_path / stamp[:4] / stamp[-6:].replace(":", "")
        await _seed_wikiquote(cache_dir)
        _edit_cache(cache_dir, attempted_at=stamp, fetched_at=stamp)
        result = await _load(
            _wikiquote(), cache_dir, lambda r: httpx.Response(500), now=NOW + timedelta(days=1)
        )
        # A copy that claims to be from year 9999 or 1 is still a usable copy.
        assert result.origin == "fallback", stamp
        assert len(result.quotes) > 10


async def test_wikiquote_attempt_in_the_future_triggers_a_fetch_and_is_reset(tmp_path):
    """Clock skew: a copy 'attempted' 30 days from now can't gate the fetch forever."""
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir, when=NOW + timedelta(days=30))
    calls: list[httpx.Request] = []

    def failing(request):
        calls.append(request)
        return httpx.Response(503)

    result = await _load(_wikiquote(), cache_dir, failing, now=NOW)

    assert len(calls) == 1
    assert result.origin == "fallback"
    assert _cache_json(cache_dir)["attempted_at"] == NOW.isoformat()
    # And the week now counts from the reset.
    again = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=1))
    assert again.origin == "saved-weekly"


async def test_wikiquote_old_fetch_with_a_recent_attempt_uses_the_attempt_for_the_rule(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    _edit_cache(cache_dir, fetched_at="2020-01-01T00:00:00+00:00")

    result = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=1))

    assert result.origin == "saved-weekly"
    assert result.saved_on == date(2020, 1, 1)


async def test_wikiquote_copy_with_a_non_utc_offset_reports_its_own_local_date(tmp_path):
    """Documenting: `saved_on` is the date in the stamp's own offset, not converted to UTC."""
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    _edit_cache(cache_dir, fetched_at="2026-09-29T23:30:00-08:00")
    result = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=1))
    assert result.saved_on == date(2026, 9, 29)


# --- the weekly rule, at the edges ---


@pytest.mark.parametrize(
    ("delta", "requests_expected"),
    [
        (timedelta(0), 0),
        (timedelta(seconds=1), 0),
        (timedelta(days=6, hours=23, minutes=59, seconds=59), 0),
        (timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999_999), 0),
        (timedelta(days=7), 1),
        (timedelta(days=7, microseconds=1), 1),
        (timedelta(days=365), 1),
        (timedelta(microseconds=-1), 1),
        (timedelta(seconds=-1), 1),
        (timedelta(days=-30), 1),
    ],
    ids=[
        "same-instant",
        "one-second",
        "6d-23:59:59",
        "6d-23:59:59.999999",
        "exactly-7d",
        "7d-plus-1us",
        "a-year",
        "1us-backwards",
        "1s-backwards",
        "30d-backwards",
    ],
)
async def test_wikiquote_weekly_rule_at_the_boundaries(tmp_path, delta, requests_expected):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    result = await _load(_wikiquote(), cache_dir, handler, now=NOW + delta)

    assert len(calls) == requests_expected
    assert result.quotes
    assert result.origin == ("saved-weekly" if requests_expected == 0 else "fallback")


async def test_wikiquote_saved_weekly_hit_does_not_touch_the_cache_file(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    before = cache_file.read_bytes()
    os.utime(cache_file, (1_000_000, 1_000_000))

    await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=3))

    assert cache_file.read_bytes() == before
    assert cache_file.stat().st_mtime == 1_000_000


async def test_wikiquote_failing_page_with_a_copy_is_fetched_at_most_once_a_week(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    fetch_days: list[int] = []
    origins: list[str] = []
    day = {"n": 0}

    def failing(request):
        fetch_days.append(day["n"])
        return httpx.Response(503)

    for n in range(1, 31):
        day["n"] = n
        result = await _load(_wikiquote(), cache_dir, failing, now=NOW + timedelta(days=n))
        assert result.quotes, f"day {n} came up empty"
        origins.append(result.origin)

    assert fetch_days == [7, 14, 21, 28]
    assert origins.count("fallback") == 4
    assert set(origins) == {"saved-weekly", "fallback"}
    # The copy itself never changed.
    assert _cache_json(cache_dir)["fetched_at"] == NOW.isoformat()


async def test_wikiquote_saved_weekly_days_between_failures_carry_no_problem(tmp_path):
    """Documenting: only the retry day reports the failure; the six days between look healthy.

    A page that has been failing for a month shows a problem on one morning
    in seven. The owner might want the last known problem to stick around.
    """
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    retry = await _load(
        _wikiquote(), cache_dir, lambda r: httpx.Response(503), now=NOW + timedelta(days=8)
    )
    quiet = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=9))
    assert retry.problem
    assert quiet.problem is None


async def test_wikiquote_failing_page_with_no_copy_is_attempted_every_time(tmp_path):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("no route")

    cache_dir = tmp_path / "cache"
    for n in range(10):
        result = await _load(_wikiquote(), cache_dir, handler, now=NOW + timedelta(days=n))
        _assert_clean_failure(result)
    assert len(calls) == 10
    assert _cache_files(cache_dir) == []


async def test_wikiquote_copy_that_no_longer_parses_gets_refetched_inside_its_week(tmp_path):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    _edit_cache(cache_dir, body="<h2>Quotes</h2><p>Nothing to see.</p>")
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return _wilde_ok(request)

    result = await _load(_wikiquote(), cache_dir, handler, now=NOW + timedelta(days=1))

    assert len(calls) == 1
    assert result.origin == "fresh"
    assert _cache_json(cache_dir)["body"] == _fixture_html()


async def test_wikiquote_copy_that_no_longer_parses_and_a_failing_fetch_is_a_failure(tmp_path):
    """An unparseable copy is absent, so there's nothing to fall back to."""
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    _edit_cache(cache_dir, body="<h2>Quotes</h2><p>Nothing to see.</p>")
    result = await _load(
        _wikiquote(), cache_dir, lambda r: httpx.Response(503), now=NOW + timedelta(days=8)
    )
    _assert_clean_failure(result)


async def test_wikiquote_parser_that_raises_on_the_copy_is_a_clean_failure(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)

    def boom(title, html):
        raise RecursionError("parser fell over\nsecond line")

    monkeypatch.setattr(sources, "parse_page", boom)
    result = await _load(_wikiquote(), cache_dir, _wilde_ok, now=NOW + timedelta(days=1))
    _assert_clean_failure(result)
    assert result.problem == "parser fell over"


async def test_wikiquote_parser_that_raises_on_a_fresh_page_falls_back_to_a_good_copy(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    real = sources.parse_page

    def picky(title, html):
        if html != _fixture_html():
            raise ValueError("new markup, new problems")
        return real(title, html)

    monkeypatch.setattr(sources, "parse_page", picky)
    result = await _load(
        _wikiquote(),
        cache_dir,
        lambda r: _envelope("Oscar Wilde", "<h2>Quotes</h2><ul><li>brand new page</li></ul>"),
        now=NOW + timedelta(days=8),
    )
    assert result.origin == "fallback"
    assert "new markup" in result.problem
    assert _cache_json(cache_dir)["body"] == _fixture_html()


@pytest.mark.parametrize("make", _READ_FAILURES, ids=lambda m: repr(m()))
async def test_wikiquote_fetch_that_raises_anything_is_a_clean_failure(tmp_path, monkeypatch, make):
    async def boom(http, title):
        raise make()

    monkeypatch.setattr(sources, "fetch_page", boom)
    _assert_clean_failure(await _load(_wikiquote(), tmp_path / "cache"))


@pytest.mark.parametrize("make", _HTTPX_ERRORS, ids=lambda m: type(m()).__name__)
async def test_wikiquote_every_transport_failure_is_a_clean_failure(tmp_path, make):
    def handler(request):
        raise make()

    _assert_clean_failure(await _load(_wikiquote(), tmp_path / "cache", handler))


async def test_wikiquote_page_with_a_lone_surrogate_survives_the_cache_round_trip(tmp_path):
    cache_dir = tmp_path / "cache"
    html = _fixture_html() + "<p>stray half of an emoji: \ud83d</p>"
    fresh = await _load(_wikiquote(), cache_dir, lambda r: _envelope("Oscar Wilde", html), now=NOW)
    assert len(fresh.quotes) > 10
    saved = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=1))
    assert saved.origin == "saved-weekly"
    assert len(saved.quotes) == len(fresh.quotes)


async def test_wikiquote_api_error_info_is_shown_but_kept_to_one_bounded_line(tmp_path):
    """The one deliberate echo: MediaWiki's own `info` string, folded onto one line."""

    def handler(request):
        body = {"error": {"code": "weird", "info": "first line\nsecond line " + "y" * 1000}}
        return httpx.Response(200, content=json.dumps(body).encode(), headers=JSON_HEADERS)

    result = await _load(_wikiquote(), tmp_path / "cache", handler)
    _assert_clean_failure(result)
    assert "first line" in result.problem
    assert "y" * 400 not in result.problem


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, content=MARKER.encode()),
        httpx.Response(200, content=MARKER.encode(), headers={"content-type": "text/html"}),
        httpx.Response(200, content=MARKER.encode(), headers=JSON_HEADERS),
        httpx.Response(200, content=b'{"weird": "' + MARKER.encode() + b'"}', headers=JSON_HEADERS),
        httpx.Response(200, content=b'["' + MARKER.encode() + b'"]', headers=JSON_HEADERS),
        httpx.Response(302, content=MARKER.encode(), headers={"location": "https://e.test/"}),
    ],
    ids=["500", "html", "not-json", "odd-shape", "json-list", "redirect"],
)
async def test_wikiquote_problem_never_echoes_the_response_body(tmp_path, response):
    result = await _load(_wikiquote(), tmp_path / "cache", lambda r: response)
    _assert_clean_failure(result)
    assert MARKER not in result.problem


# --- the cache: writes ---


async def test_interrupted_replace_leaves_the_old_copy_and_no_temp_files(tmp_path, monkeypatch):
    path, cache_dir = await _seed_file(tmp_path)
    (cache_file,) = _cache_files(cache_dir)
    before = cache_file.read_bytes()

    def boom(src, dst):
        raise PermissionError(errno.EACCES, "replace refused")

    monkeypatch.setattr(sources.os, "replace", boom)
    path.write_text("a brand new quote\n", encoding="utf-8")
    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    monkeypatch.undo()

    # Documenting: a save that fails is invisible in `problem`; it's a log line only.
    assert [q.text for q in result.quotes] == ["a brand new quote"]
    assert result.problem is None
    assert cache_file.read_bytes() == before
    assert _leftovers(tmp_path) == []
    assert [p.name for p in cache_dir.iterdir()] == [cache_file.name]


async def test_a_write_that_dies_halfway_leaves_the_old_copy_and_no_temp_files(
    tmp_path, monkeypatch
):
    path, cache_dir = await _seed_file(tmp_path)
    (cache_file,) = _cache_files(cache_dir)
    before = cache_file.read_bytes()

    def half_a_write(payload, handle, *args, **kwargs):
        handle.write('{"version": 1, "key": "tru')
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(sources.json, "dump", half_a_write)
    path.write_text("a brand new quote\n", encoding="utf-8")
    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    monkeypatch.undo()

    assert [q.text for q in result.quotes] == ["a brand new quote"]
    assert cache_file.read_bytes() == before
    assert _leftovers(tmp_path) == []


async def test_first_ever_save_failing_leaves_nothing_behind(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(sources.os, "replace", boom)
    cache_dir = tmp_path / "cache"
    result = await _load(_url(), cache_dir, lambda r: _text_response())
    monkeypatch.undo()

    assert len(result.quotes) == 2
    assert list(cache_dir.iterdir()) == []


async def test_wikiquote_fallback_still_serves_when_the_attempt_cannot_be_saved(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    await _seed_wikiquote(cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    before = cache_file.read_bytes()

    def boom(src, dst):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(sources.os, "replace", boom)
    result = await _load(
        _wikiquote(), cache_dir, lambda r: httpx.Response(503), now=NOW + timedelta(days=8)
    )
    monkeypatch.undo()

    assert result.origin == "fallback"
    assert len(result.quotes) > 10
    assert cache_file.read_bytes() == before
    assert _leftovers(tmp_path) == []


async def test_saved_cache_is_plain_json_that_round_trips_hostile_text(tmp_path):
    body = 'quote with "quotes", \\ backslashes,   line separators, 😀 and \x00 nul\n'
    path, cache_dir = await _seed_file(tmp_path, body)
    assert _cache_json(cache_dir)["body"] == body.replace("\r", "")
    path.unlink()
    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))
    assert result.origin == "fallback"
    assert len(result.quotes) == 1


# --- concurrency ---


async def test_concurrent_loads_of_one_file_source_leave_one_valid_cache(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text(FORTUNE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    async with _client(_forbidden) as http:
        results = await asyncio.gather(
            *[load_source(_file(path), http=http, cache_dir=cache_dir, now=NOW) for _ in range(12)]
        )
    assert all(len(r.quotes) == 2 and r.problem is None for r in results)
    assert _cache_json(cache_dir)["body"] == FORTUNE
    assert _leftovers(tmp_path) == []
    assert len(_cache_files(cache_dir)) == 1


async def test_concurrent_loads_of_one_url_with_different_bodies_leave_a_valid_cache(tmp_path):
    counter = {"n": 0}
    bodies = [f"body number {i}\n%\nsecond entry {i}\n".encode() for i in range(10)]

    def handler(request):
        counter["n"] += 1
        return _text_response(bodies[counter["n"] - 1])

    cache_dir = tmp_path / "cache"
    async with _client(handler) as http:
        results = await asyncio.gather(
            *[
                load_source(_url(), http=http, cache_dir=cache_dir, now=NOW + timedelta(seconds=i))
                for i in range(10)
            ]
        )
    assert all(len(r.quotes) == 2 for r in results)
    data = _cache_json(cache_dir)
    assert data["key"] == _url().key
    assert data["body"].encode() in bodies
    assert _leftovers(tmp_path) == []
    assert len(_cache_files(cache_dir)) == 1


async def test_concurrent_wikiquote_loads_agree_and_leave_a_valid_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    async with _client(_wilde_ok) as http:
        results = await asyncio.gather(
            *[load_source(_wikiquote(), http=http, cache_dir=cache_dir, now=NOW) for _ in range(6)]
        )
    assert len({len(r.quotes) for r in results}) == 1
    assert _cache_json(cache_dir)["body"] == _fixture_html()
    assert _leftovers(tmp_path) == []
    saved = await _load(_wikiquote(), cache_dir, _forbidden, now=NOW + timedelta(days=1))
    assert saved.origin == "saved-weekly"


async def test_readers_racing_a_writer_never_see_a_torn_copy(tmp_path):
    """Loads that fail (so they read the copy) run alongside loads that rewrite it."""
    counter = {"n": 0}

    def flip(request):
        counter["n"] += 1
        if counter["n"] % 2:
            return _text_response(f"changing quote {counter['n']}\n%\nanother one\n".encode())
        return httpx.Response(500)

    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())
    async with _client(flip) as http:
        results = await asyncio.gather(
            *[
                load_source(_url(), http=http, cache_dir=cache_dir, now=NOW + timedelta(seconds=i))
                for i in range(30)
            ]
        )
    assert all(r.quotes for r in results)
    assert all(r.origin in ("fresh", "fallback") for r in results)
    json.loads(_cache_files(cache_dir)[0].read_text(encoding="utf-8"))
    assert _leftovers(tmp_path) == []


# --- file and URL caches: what "saved on" means ---


async def test_file_saved_on_is_when_the_body_last_changed_not_the_last_good_read(tmp_path):
    """Documenting: an unchanged file never refreshes its copy, so `saved_on` runs old.

    A file read fine every day for a month, then deleted, reports a copy
    saved a month ago, because the copy is only rewritten when the text
    changes. Accurate for "how old is this text"; possibly surprising for
    "when did this last work". The owner might prefer to touch `fetched_at`.
    """
    path, cache_dir = await _seed_file(tmp_path)
    for day in (5, 20, 29):
        await _load(_file(path), cache_dir, now=NOW + timedelta(days=day))
    path.unlink()

    result = await _load(_file(path), cache_dir, now=NOW + timedelta(days=30))

    assert result.origin == "fallback"
    assert result.saved_on == NOW.date()


async def test_url_fallback_never_rewrites_the_copy_however_often_it_fails(tmp_path):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())
    (cache_file,) = _cache_files(cache_dir)
    before = cache_file.read_bytes()
    os.utime(cache_file, (1_000_000, 1_000_000))
    calls: list[httpx.Request] = []

    def failing(request):
        calls.append(request)
        return httpx.Response(503)

    for day in range(1, 11):
        result = await _load(_url(), cache_dir, failing, now=NOW + timedelta(days=day))
        assert result.origin == "fallback"
        assert result.saved_on == NOW.date()

    assert len(calls) == 10  # no weekly rule for URLs
    assert cache_file.read_bytes() == before
    assert cache_file.stat().st_mtime == 1_000_000


async def test_url_source_that_changes_body_replaces_the_copy_and_its_date(tmp_path):
    cache_dir = tmp_path / "cache"
    await _load(_url(), cache_dir, lambda r: _text_response())
    later = NOW + timedelta(days=9)
    await _load(_url(), cache_dir, lambda r: _text_response(b"only one now\n"), now=later)
    data = _cache_json(cache_dir)
    assert data["body"] == "only one now\n"
    assert data["fetched_at"] == later.isoformat()


async def test_file_fallback_uses_the_copy_even_when_the_file_is_now_a_fifo(tmp_path):
    path, cache_dir = await _seed_file(tmp_path)
    path.unlink()
    os.mkfifo(path)
    result = await asyncio.wait_for(
        _load(_file(path), cache_dir, now=NOW + timedelta(days=1)), timeout=5
    )
    assert result.origin == "fallback"
    assert "regular file" in result.problem


# --- messages ---


async def test_problem_stays_within_the_250_character_cap_inclusive_of_the_ellipsis(tmp_path):
    """A cut problem is at most 250 characters, the ellipsis included.

    This was an off-by-one (251) while problems went through `first_line`.
    """
    result = await _load(
        _url("https://example.test/" + "a" * 400), tmp_path / "c", lambda r: httpx.Response(404)
    )
    assert len(result.problem) <= 250
    assert result.problem.endswith("\u2026")


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/q&region=us/x.txt",
        "https://example.test/q&section=2/x.txt",
        "https://example.test/q&notify=1/x.txt",
        "https://example.test/<b>bold</b>/q.txt",
        "https://example.test/q&amp;b=2/x.txt",
    ],
    ids=["region-entity", "section-entity", "notify-entity", "tags", "literal-amp-entity"],
)
async def test_problem_shows_the_configured_url_unaltered(tmp_path, value):
    """Markup-looking characters in the path come back as typed, not "cleaned".

    These used to carry the entities in the query string; the query is now
    dropped from problems on purpose (see the credentials test), so the same
    characters moved into the path, which is still shown.
    """
    result = await _load(_url(value), tmp_path / "cache", lambda r: httpx.Response(404))
    assert value in result.problem


async def test_problem_shows_a_file_path_unaltered(tmp_path):
    path = tmp_path / "a&region<b>x</b>.txt"
    result = await _load(_file(path), tmp_path / "cache")
    assert str(path) in result.problem


async def test_url_credentials_and_query_never_show_up_in_the_problem_or_logs(tmp_path, caplog):
    """Changed from "they show up": a secret in a URL must not reach the admin channel.

    A raw gist link with a `user:pass@` or a `?token=` is a plausible thing to
    configure, so problems and logs show scheme, host and path only. The
    cache key still uses the full URL, so two tokens are still two sources.
    """
    url = "https://user:hunter2@example.test/q.txt?token=abc123#frag9"
    with caplog.at_level(logging.DEBUG):
        result = await _load(_url(url), tmp_path / "cache", lambda r: httpx.Response(403))
        seeded = await _load(_url(url), tmp_path / "cache2", lambda r: _text_response())
        failed = await _load(_url(url), tmp_path / "cache2", lambda r: httpx.Response(500), now=NOW)
    for text in (result.problem, failed.problem, describe(_url(url)), caplog.text):
        for secret in ("hunter2", "user:", "abc123", "frag9", "token="):
            assert secret not in text
    assert result.problem == "https://example.test/q.txt returned HTTP 403"
    assert seeded.problem is None
    assert describe(_url(url)) == "url https://example.test/q.txt"
    assert _url(url).key.endswith(url)


@pytest.mark.parametrize(
    "src_factory",
    [
        lambda tmp: _file(tmp / "missing.txt"),
        lambda tmp: _url(),
        lambda tmp: _wikiquote(),
    ],
    ids=["file", "url", "wikiquote"],
)
async def test_fallback_problem_is_one_bounded_line_and_never_holds_quote_text(
    tmp_path, src_factory
):
    src = src_factory(tmp_path)
    if src.kind == "file":
        _write_text(src.value, "Zebulon Q. Fumblewick's private teapot\n")
    seed = {
        "file": lambda r: _forbidden(r),
        "url": lambda r: _text_response(b"Zebulon Q. Fumblewick's private teapot\n"),
        "wikiquote": _wilde_ok,
    }[src.kind]
    cache_dir = tmp_path / "cache"
    seeded = await _load(src, cache_dir, seed)
    if src.kind == "file":
        _unlink(src.value)

    def failing(request):
        raise httpx.ReadError("the line dropped\nmid quote")

    result = await _load(src, cache_dir, failing, now=NOW + timedelta(days=8))

    assert result.origin == "fallback"
    assert result.problem and "\n" not in result.problem
    assert len(result.problem) <= HARD_MAX_PROBLEM
    for quote in seeded.quotes:
        assert quote.text[:20] not in result.problem


# --- privacy: what gets logged ---


def _info_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "newsbot.lounge.sources" and r.levelno == logging.INFO
    ]


async def test_one_info_line_per_successful_load_and_no_quote_text_in_any_log(tmp_path, caplog):
    quote_a = "Zebulon Q. Fumblewick has opinions"
    quote_b = "Ottoline Marsh kept the good teapot"
    path = tmp_path / "quotes.txt"
    path.write_text(f"{quote_a}\n%\n{quote_b}\n", encoding="utf-8")
    file_cache = tmp_path / "file-cache"
    url_cache = tmp_path / "url-cache"
    wiki_cache = tmp_path / "wiki-cache"
    url_body = f"{quote_a}\n%\n{quote_b}\n".encode()
    later = NOW + timedelta(days=8)
    wilde_first_quotes: list[str] = []

    with caplog.at_level(logging.INFO, logger="newsbot.lounge.sources"):
        caplog.clear()
        await _load(_file(path), file_cache)
        assert len(_info_records(caplog)) == 1
        caplog.clear()
        path.unlink()
        await _load(_file(path), file_cache, now=later)
        assert len(_info_records(caplog)) == 1
        caplog.clear()
        await _load(_url(), url_cache, lambda r: _text_response(url_body))
        assert len(_info_records(caplog)) == 1
        caplog.clear()
        await _load(_url(), url_cache, lambda r: httpx.Response(500), now=later)
        assert len(_info_records(caplog)) == 1
        caplog.clear()
        fresh = await _load(_wikiquote(), wiki_cache, _wilde_ok)
        wilde_first_quotes = [q.text for q in fresh.quotes]
        assert len(_info_records(caplog)) == 1
        caplog.clear()
        await _load(_wikiquote(), wiki_cache, _forbidden, now=NOW + timedelta(days=1))
        assert len(_info_records(caplog)) == 1
        caplog.clear()
        await _load(_wikiquote(), wiki_cache, lambda r: httpx.Response(503), now=later)
        assert len(_info_records(caplog)) == 1
        caplog.clear()

    # Everything captured across the whole run, at any level, with tracebacks.
    all_text = caplog.text
    for line in [quote_a, quote_b, *wilde_first_quotes[:15]]:
        assert line not in all_text
        assert line[:25] not in all_text


async def test_failure_logs_carry_no_quote_text_either(tmp_path, caplog, monkeypatch):
    teapot_line = "Ottoline Marsh kept the good teapot"
    path = tmp_path / "quotes.txt"
    path.write_text(teapot_line + "\n", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    await _load(_file(path), cache_dir)
    (cache_file,) = _cache_files(cache_dir)
    cache_file.write_text("{ not json " + teapot_line[:5], encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        await _load(_file(path), cache_dir, now=NOW + timedelta(days=1))

        def boom(raw):
            raise RuntimeError("the parser is having a day")

        monkeypatch.setattr(sources, "split_fortune", boom)
        await _load(_file(path), cache_dir, now=NOW + timedelta(days=2))

    assert teapot_line not in caplog.text
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
