"""Retry and token-accounting edge cases for newsbot.pipeline.summarize.

test_summarize.py already covers the happy path (success first try, two
transient failures then success, three failures giving a fallback, a fatal
error skipping straight to fallback). This file goes after the edges the
implementer's tests didn't: does `AnthropicLLM.emit_stories` map every SDK
failure mode to the right `LLMError`/`LLMFatalError` flavor, and does
`summarize_topic` keep summing tokens correctly when the failures are mixed
(a 429 here, a 500 there, one bad parse)? Nothing here makes a network call:
`AnthropicLLM` is exercised by monkeypatching `_client.messages.parse`
directly, never by hitting `messages.parse` for real.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from newsbot.collectors.base import RawItem
from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.summarize import (
    AnthropicLLM,
    LLMError,
    LLMFatalError,
    LLMResult,
    StoriesOut,
    StoryOut,
    summarize_topic,
)
from newsbot.store.models import Usage

DIABLO4 = Topic(key="diablo4", name="Diablo IV", aliases=["Diablo 4", "D4"], entities=["Blizzard"])


def _topic_item(url="https://example.com/a") -> TopicItem:
    item = RawItem(
        url=url,
        title="Diablo IV update",
        excerpt="Some patch notes.",
        source_name="Blizzard News",
        trust="official",
        published_at=None,
    )
    return TopicItem(item=item, topic_key="diablo4", uncertain=False)


class _StubLLM:
    """Returns (or raises) whatever was queued, in order. One call per queue entry."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _stories_result(*stories: StoryOut, input_tokens=100, output_tokens=50) -> LLMResult:
    return LLMResult(
        stories=StoriesOut(stories=list(stories)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def _llm() -> AnthropicLLM:
    return AnthropicLLM("sk-test-not-a-real-key")


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_request())


def _parsed_message(
    *, stop_reason="end_turn", parsed_output=None, input_tokens=11, output_tokens=22
):
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed_output,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# --- AnthropicLLM: mapping SDK failures to LLMError/LLMFatalError ---


async def test_rate_limit_error_is_retryable():
    llm = _llm()

    async def _raise(**_kwargs):
        raise anthropic.RateLimitError("slow down", response=_response(429), body=None)

    llm._client.messages.parse = _raise
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)


async def test_server_5xx_is_retryable():
    llm = _llm()

    async def _raise(**_kwargs):
        raise anthropic.APIStatusError("oops", response=_response(503), body=None)

    llm._client.messages.parse = _raise
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)


async def test_client_4xx_is_fatal_not_retryable():
    llm = _llm()

    async def _raise(**_kwargs):
        raise anthropic.APIStatusError("bad request", response=_response(400), body=None)

    llm._client.messages.parse = _raise
    with pytest.raises(LLMFatalError):
        await llm.emit_stories("sys", "user")


async def test_connection_error_is_retryable():
    llm = _llm()

    async def _raise(**_kwargs):
        raise anthropic.APIConnectionError(message="dns went sideways", request=_request())

    llm._client.messages.parse = _raise
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)


async def test_timeout_error_is_retryable():
    llm = _llm()

    async def _raise(**_kwargs):
        raise anthropic.APITimeoutError(request=_request())

    llm._client.messages.parse = _raise
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)


async def test_stop_reason_max_tokens_is_retryable_and_carries_usage():
    llm = _llm()

    async def _parse(**_kwargs):
        return _parsed_message(stop_reason="max_tokens", input_tokens=100, output_tokens=4096)

    llm._client.messages.parse = _parse
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)
    assert exc_info.value.input_tokens == 100
    assert exc_info.value.output_tokens == 4096


async def test_stop_reason_refusal_is_retryable():
    llm = _llm()

    async def _parse(**_kwargs):
        return _parsed_message(stop_reason="refusal", input_tokens=5, output_tokens=1)

    llm._client.messages.parse = _parse
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)
    assert (exc_info.value.input_tokens, exc_info.value.output_tokens) == (5, 1)


async def test_parsed_output_none_is_retryable_and_carries_usage():
    llm = _llm()

    async def _parse(**_kwargs):
        return _parsed_message(
            stop_reason="end_turn", parsed_output=None, input_tokens=7, output_tokens=3
        )

    llm._client.messages.parse = _parse
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)
    assert (exc_info.value.input_tokens, exc_info.value.output_tokens) == (7, 3)


async def test_validation_error_from_parse_is_retryable():
    llm = _llm()

    async def _raise(**_kwargs):
        try:
            StoryOut(headline="", summary="x", label="official", item_urls=[], relevant=True)
        except ValidationError as exc:
            raise exc
        raise AssertionError("expected ValidationError")  # pragma: no cover

    llm._client.messages.parse = _raise
    with pytest.raises(LLMError) as exc_info:
        await llm.emit_stories("sys", "user")
    assert not isinstance(exc_info.value, LLMFatalError)


async def test_successful_parse_returns_usage_and_stories():
    llm = _llm()
    stories = StoriesOut(
        stories=[
            StoryOut(
                headline="H",
                summary="S",
                label="official",
                item_urls=["https://example.com/a"],
                relevant=True,
            )
        ]
    )

    async def _parse(**_kwargs):
        return _parsed_message(
            stop_reason="end_turn", parsed_output=stories, input_tokens=42, output_tokens=17
        )

    llm._client.messages.parse = _parse
    result = await llm.emit_stories("sys", "user")
    assert result.stories is stories
    assert result.input_tokens == 42
    assert result.output_tokens == 17


# --- summarize_topic: mixed failure sequences, token accounting ---


async def test_tokens_sum_across_mixed_failure_types_before_fallback():
    items = [_topic_item()]
    llm = _StubLLM(
        [
            LLMError("rate limited", input_tokens=10, output_tokens=1),
            LLMError("server error", input_tokens=20, output_tokens=2),
            LLMError("max_tokens", input_tokens=30, output_tokens=4096),
        ]
    )
    summary = await summarize_topic(llm, DIABLO4, items, [], sleep=_no_sleep)
    assert summary.fallback is True
    assert summary.usage == Usage(input_tokens=60, output_tokens=4099)


async def test_fatal_error_after_a_transient_retry_still_sums_tokens():
    items = [_topic_item()]
    llm = _StubLLM(
        [
            LLMError("rate limited", input_tokens=10, output_tokens=1),
            LLMFatalError("bad request", input_tokens=5, output_tokens=0),
        ]
    )
    summary = await summarize_topic(llm, DIABLO4, items, [], sleep=_no_sleep)
    assert summary.fallback is True
    assert summary.usage == Usage(input_tokens=15, output_tokens=1)
    # Fatal on attempt two: no third attempt should have been made.
    assert len(llm.calls) == 2


async def test_exactly_three_attempts_before_giving_up():
    items = [_topic_item()]
    story = StoryOut(
        headline="Should never be reached",
        summary="S",
        label="official",
        item_urls=[items[0].item.url],
        relevant=True,
    )
    # A 4th queued result that would succeed proves the loop stops at 3.
    llm = _StubLLM([LLMError("1"), LLMError("2"), LLMError("3"), _stories_result(story)])
    summary = await summarize_topic(llm, DIABLO4, items, [], sleep=_no_sleep)
    assert summary.fallback is True
    assert len(llm.calls) == 3
