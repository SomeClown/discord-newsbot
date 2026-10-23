"""Tests for `_fetch` and the conditional requests the shift_table collector makes with it.

The old `_fetch_body` callers still have their own tests (test_collectors_rss_steam.py),
untouched; this file is for what's new: validators coming back, a 304 being an answer
and not an error, and the shift_table collector remembering (and forgetting) them.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from newsbot.collectors.rss import _MAX_RESPONSE_BYTES, _RETRY_BACKOFFS_S, _fetch, _fetch_body
from newsbot.collectors.shift_table import PageValidators, ShiftTableCollector, Validators
from newsbot.config import ShiftTableSource
from newsbot.useragent import user_agent_headers

PAGE = (Path(__file__).parent / "fixtures" / "shift_table" / "page.html").read_bytes()
BAD_PAGE = (Path(__file__).parent / "fixtures" / "shift_table" / "no_tables.html").read_bytes()
URL = "https://codes.example/borderlands-4-shift-codes/"


async def _noop_sleep(_seconds: float) -> None:
    return None


def _collector(validators: PageValidators | None = None, *, sleep=_noop_sleep):
    source = ShiftTableSource(type="shift_table", name="mentalmars", url=URL, trust="community")
    return ShiftTableCollector(source, validators=validators, sleep=sleep)


# --- _fetch ---


async def test_fetch_returns_status_body_and_validators():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"hello",
            headers={"ETag": '"abc"', "Last-Modified": "Wed, 07 Oct 2026 12:00:00 GMT"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep)
    assert fetched.status == 200
    assert fetched.body == b"hello"
    assert fetched.etag == '"abc"'
    assert fetched.last_modified == "Wed, 07 Oct 2026 12:00:00 GMT"


async def test_fetch_without_validators_says_none():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"))
    async with httpx.AsyncClient(transport=transport) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep)
    assert (fetched.etag, fetched.last_modified) == (None, None)


async def test_fetch_sends_the_user_agent_and_any_extra_headers():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, content=b"x")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _fetch(http, URL, sleep=_noop_sleep, headers={"If-None-Match": '"abc"'})
        await _fetch(http, URL, sleep=_noop_sleep)
    expected_ua = user_agent_headers()["User-Agent"]
    assert seen[0]["user-agent"] == expected_ua
    assert seen[0]["if-none-match"] == '"abc"'
    assert seen[1]["user-agent"] == expected_ua
    assert "if-none-match" not in seen[1]


async def test_fetch_returns_a_304_instead_of_raising():
    transport = httpx.MockTransport(lambda request: httpx.Response(304, headers={"ETag": '"abc"'}))
    async with httpx.AsyncClient(transport=transport) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep, headers={"If-None-Match": '"abc"'})
    assert fetched.status == 304
    assert fetched.body == b""
    assert fetched.etag == '"abc"'


async def test_fetch_still_raises_for_other_errors():
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch(http, URL, sleep=_noop_sleep)


async def test_fetch_body_is_the_body_and_nothing_else():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"just this", headers={"ETag": '"x"'})
    )
    async with httpx.AsyncClient(transport=transport) as http:
        assert await _fetch_body(http, URL, sleep=_noop_sleep) == b"just this"


async def test_fetch_caps_the_body_at_5_mb():
    oversized = b"a" * (6 * 1024 * 1024)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=oversized))
    async with httpx.AsyncClient(transport=transport) as http:
        fetched = await _fetch(http, URL, sleep=_noop_sleep)
    assert len(fetched.body) == _MAX_RESPONSE_BYTES


# --- the collector's side of it ---


async def test_first_request_is_unconditional_and_the_second_carries_the_validators():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(
            200,
            content=PAGE,
            headers={"ETag": '"v1"', "Last-Modified": "Wed, 07 Oct 2026 12:00:00 GMT"},
        )

    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _collector(validators).collect(http)
        validators.commit_all()  # what the pipeline does once the sightings are saved
        await _collector(validators).collect(http)  # rebuilt, like every pass
    assert "if-none-match" not in seen[0]
    assert "if-modified-since" not in seen[0]
    assert seen[1]["if-none-match"] == '"v1"'
    assert seen[1]["if-modified-since"] == "Wed, 07 Oct 2026 12:00:00 GMT"
    validators.commit_all()
    assert validators == {URL: Validators('"v1"', "Wed, 07 Oct 2026 12:00:00 GMT")}


async def test_a_304_is_a_healthy_pass_with_no_items():
    validators = PageValidators({URL: Validators('"v1"', None)})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        items = await _collector(validators).collect(http)
    assert items == []
    assert seen[0]["if-none-match"] == '"v1"'
    assert "if-modified-since" not in seen[0]
    assert validators == {URL: Validators('"v1"', None)}  # untouched


async def test_validators_are_stored_only_after_a_parse_that_found_rows():
    validators = PageValidators()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=BAD_PAGE, headers={"ETag": '"bad"'})
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="no SHiFT code tables found"):
            await _collector(validators).collect(http)
    assert validators == {}
    assert validators.pending == {}


async def test_a_failed_parse_keeps_the_last_good_validators():
    good = Validators('"good"', None)
    validators = PageValidators({URL: good})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=BAD_PAGE, headers={"ETag": '"bad"'})
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError):
            await _collector(validators).collect(http)
    assert validators == {URL: good}


async def test_a_page_that_stops_sending_validators_clears_the_old_ones():
    validators = PageValidators({URL: Validators('"old"', None)})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=PAGE))
    async with httpx.AsyncClient(transport=transport) as http:
        await _collector(validators).collect(http)
    assert validators == {URL: Validators('"old"', None)}  # staged, not yet committed
    validators.commit_all()
    assert validators == {}


async def test_without_a_shared_dict_nothing_is_remembered_between_collectors():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, content=PAGE, headers={"ETag": '"v1"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _collector().collect(http)
        await _collector().collect(http)
    assert all("if-none-match" not in h for h in seen)


# --- the guarded fetch's other guards still apply ---


async def test_a_429_is_retried_and_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, content=PAGE)

    sleeps = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        items = await _collector(sleep=recording_sleep).collect(http)
    assert len(calls) == 3
    assert sleeps == list(_RETRY_BACKOFFS_S)
    assert len(items) == 12


async def test_a_429_that_never_clears_raises():
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await _collector().collect(http)


async def test_a_redirect_to_a_private_address_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "codes.example":
            return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest/"})
        raise AssertionError(f"the private host was requested: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="non-public"):
            await _collector().collect(http)


async def test_a_redirect_to_a_hostname_that_resolves_privately_is_refused(monkeypatch):
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "internal.attacker.example":
            return [(2, 1, 6, "", ("10.13.14.15", 0))]
        raise OSError(f"unexpected resolution attempt for {host!r}")

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "codes.example":
            return httpx.Response(302, headers={"Location": "https://internal.attacker.example/x"})
        return httpx.Response(200, content=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="non-public"):
            await _collector().collect(http)


async def test_an_oversized_page_is_cut_at_5_mb_and_then_judged_on_what_is_left():
    # 6 MB of padding in front of the tables: the cap lands inside the padding, so what
    # parses is nothing, which is a failed pass and not a memory problem.
    body = b"<!-- " + b"a" * (6 * 1024 * 1024) + b" -->" + PAGE
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValueError, match="no SHiFT code tables found"):
            await _collector().collect(http)


# --- two phases: staged by collect(), current once committed ---


async def test_collect_only_stages_the_validators_and_the_next_request_ignores_them():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, content=PAGE, headers={"ETag": '"v1"'})

    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _collector(validators).collect(http)
        assert validators == {}
        assert validators.pending == {URL: Validators('"v1"', None)}
        await _collector(validators).collect(http)  # nobody committed: unconditional again
    assert seen == [None, None]


async def test_a_pass_that_was_never_committed_cannot_hide_its_codes_behind_a_304():
    # The failure this design exists for: the page parsed, something later broke, and
    # an eager cache would now answer 304 and keep the codes from ever being saved.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=PAGE, headers={"ETag": '"v1"'})

    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = await _collector(validators).collect(http)
        # ...the sightings never got saved, so there's no commit...
        second = await _collector(validators).collect(http)
    assert len(first) == len(second) == 12


async def test_commit_makes_one_urls_staged_validators_current_and_leaves_the_rest():
    other = "https://other.example/codes/"
    validators = PageValidators()
    validators.stage(URL, Validators('"a"', None))
    validators.stage(other, Validators('"b"', None))
    validators.commit(URL)
    assert validators == {URL: Validators('"a"', None)}
    assert validators.pending == {other: Validators('"b"', None)}
    validators.commit_all()
    assert validators == {URL: Validators('"a"', None), other: Validators('"b"', None)}
    assert validators.pending == {}


def test_committing_a_url_with_nothing_staged_changes_nothing():
    validators = PageValidators({URL: Validators('"a"', None)})
    validators.commit(URL)
    validators.commit("https://nowhere.example/")
    validators.commit_all()
    assert validators == {URL: Validators('"a"', None)}


def test_staging_none_forgets_the_old_validators_on_commit():
    validators = PageValidators({URL: Validators('"a"', None)})
    validators.stage(URL, None)
    assert validators == {URL: Validators('"a"', None)}
    validators.commit(URL)
    assert validators == {}


def test_a_newer_stage_replaces_an_older_uncommitted_one_and_discard_drops_it():
    validators = PageValidators()
    validators.stage(URL, Validators('"a"', None))
    validators.stage(URL, Validators('"b"', None))
    assert validators.pending == {URL: Validators('"b"', None)}
    validators.discard(URL)
    validators.discard(URL)  # and again, harmlessly
    validators.commit_all()
    assert validators == {}


async def test_the_next_collect_forgets_what_the_last_one_staged_if_it_fails():
    validators = PageValidators()
    good = httpx.Response(200, content=PAGE, headers={"ETag": '"v1"'})
    bad = httpx.Response(200, content=BAD_PAGE, headers={"ETag": '"v2"'})
    responses = [good, bad]
    transport = httpx.MockTransport(lambda request: responses.pop(0))
    async with httpx.AsyncClient(transport=transport) as http:
        await _collector(validators).collect(http)
        with pytest.raises(ValueError):
            await _collector(validators).collect(http)
    validators.commit_all()
    assert validators == {}


def test_the_pending_view_is_a_copy():
    validators = PageValidators()
    validators.stage(URL, Validators('"a"', None))
    validators.pending.clear()
    assert validators.pending == {URL: Validators('"a"', None)}


# --- validators that can't be header values ---


async def test_only_the_unusable_validator_is_dropped_and_the_other_survives():
    modified = b"Wed, 07 Oct 2026 12:00:00 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        headers = [(b"etag", b'"caf\xe9"'), (b"last-modified", modified)]
        return httpx.Response(200, content=PAGE, headers=headers)

    validators = PageValidators()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _collector(validators).collect(http)
    assert validators.pending == {URL: Validators(None, modified.decode())}


async def test_a_bad_committed_validator_cannot_stop_a_request_going_out():
    # Whatever's in the owner's dict, the request goes out; a bad value is just not sent.
    validators = PageValidators({URL: Validators('"caf\xe9"', "Wed, 07 Oct 2026 \n GMT")})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, content=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert len(await _collector(validators).collect(http)) == 12
    assert "if-none-match" not in seen[0]
    assert "if-modified-since" not in seen[0]
