"""One Claude call per topic: turn a pile of items into a short story list.

This is the module I trust least, not because the code is complicated (it
isn't) but because it's the one place where a third party gets to hand us
free-form text and we ask a language model to turn that into something we
post to fifty people without a human looking at it first. So almost
everything here is defense: the prompt says the items are untrusted data,
and then the code doesn't take the model's word for it either -- URLs get
re-canonicalized and checked against what we actually collected, the
`official` label gets re-derived instead of trusted, and three failed
attempts get a plain headline list instead of nothing at all. Trust, but
verify, and then verify again because I've been burned before.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field, ValidationError

from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.normalize import canonicalize
from newsbot.pipeline.prompts import build_prompt
from newsbot.store.models import Label, PriorStory, Usage

# Model id per the current SDK docs, not the dated snapshot in the original
# plan -- Anthropic's convention is to alias the undated name to whatever
# the current Haiku 4.5 build is, so this stays current without a code
# change every time they cut a new snapshot.
MODEL = "claude-haiku-4-5"

# $/million tokens, Haiku 4.5, as of this writing. Verify against the
# current pricing page before trusting the spend estimate in `/newsbot
# status` for anything more important than a rough sense of the bill.
PRICE_IN_PER_MTOK = 1.00
PRICE_OUT_PER_MTOK = 5.00

_MAX_TOKENS = 4096
_TEMPERATURE = 0.2
_BACKOFF_S = (1.0, 2.0, 4.0)
_FALLBACK_NOTE = "Summary unavailable; showing headlines."


class StoryOut(BaseModel):
    # No `max_length` here on purpose: the Anthropic SDK's structured-
    # output mode strips length constraints out of the schema it actually
    # sends the model (only `min_length` and the rest of the shape
    # survive), so a model response that ran long would fail pydantic
    # validation here with absolutely no way to ever pass -- the model
    # was never told the limit it just got rejected for. Length is
    # enforced downstream instead, by truncating in postprocess()'s
    # `_truncate` calls.
    headline: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    label: Literal["official", "reported", "rumor"]
    item_urls: list[str]
    relevant: bool
    update_of_headline: str | None = None


class StoriesOut(BaseModel):
    """The model's whole response. A bare top-level array isn't valid tool/structured-output
    input (it has to be an object), so this wraps the spec's array in one field.
    """

    stories: list[StoryOut]


class LLMError(Exception):
    """A summarize attempt failed in a way worth retrying.

    Carries whatever token usage the failed attempt still burned (the API
    happily charges for a response it then hands us `stop_reason:
    "max_tokens"` on), so a retried topic's usage total isn't a lie.
    """

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class LLMFatalError(LLMError):
    """A summarize attempt failed in a way retrying won't fix (a non-5xx API error).

    A 400 or a 401 is going to be a 400 or a 401 again a second from now;
    burning the rest of the retry budget (and the backoff sleeps that come
    with it) on that is just a slower way of reaching the same fallback.
    """


@dataclass
class LLMResult:
    stories: StoriesOut
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    async def emit_stories(self, system: str, user: str) -> LLMResult:
        """Call the model and return validated stories. Raise `LLMError` on failure."""
        ...


class AnthropicLLM:
    """The real Claude client, wired for structured output.

    `max_retries=0` on the SDK client is deliberate: `summarize_topic`
    below already retries three times with its own backoff, and the SDK's
    default retry behavior stacking on top of that would quietly turn
    3 attempts into 9 -- a fun way to find out Anthropic's rate limits the
    hard way.
    """

    def __init__(self, api_key: str, *, model: str = MODEL) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=60.0)
        self._model = model

    async def emit_stories(self, system: str, user: str) -> LLMResult:
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=StoriesOut,
                # `temperature` isn't a `messages.parse()` kwarg anymore (SDK
                # 1.x dropped sampling params from the method signatures
                # entirely, not just for the newer models that reject them
                # outright) -- `extra_body` is the escape hatch for a model,
                # like Haiku 4.5, that still honors it.
                extra_body={"temperature": _TEMPERATURE},
            )
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMError(f"server error {exc.status_code}: {exc}") from exc
            # A 4xx that isn't a rate limit (bad request, auth, quota) will
            # still be a 4xx on attempt two. Fail fast to the fallback.
            raise LLMFatalError(f"API error {exc.status_code}: {exc}") from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise LLMError(f"connection error: {exc}") from exc
        except ValidationError as exc:
            raise LLMError(f"invalid output: {exc}") from exc

        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        if response.stop_reason in ("max_tokens", "refusal"):
            raise LLMError(f"model returned stop_reason={response.stop_reason}", **usage)
        if response.parsed_output is None:
            raise LLMError("response had no parsed output", **usage)

        return LLMResult(stories=response.parsed_output, **usage)


@dataclass
class StoryDraft:
    headline: str
    summary: str
    label: Label
    item_urls: list[str]
    update_of_story_id: int | None


@dataclass
class TopicSummary:
    topic_key: str
    stories: list[StoryDraft]
    fallback: bool
    note: str | None
    usage: Usage


_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Anything shaped like `scheme://...` (http, https, steam, discord, or any
# other scheme a model could invent) or a bare `www.`-prefixed token. This
# runs on `headline`/`summary` only -- `item_urls` already goes through
# `canonicalize()` against what we actually collected, which is a much
# stricter check than "does this look like a URL". Shift codes (the thing
# this exists to *not* remove; see CLAUDE.md's content policy) don't match
# either pattern, so a code stays right where the model put it.
_URL_TOKEN_RE = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://\S+|\bwww\.\S+|\b(?:discord\.gg|discord(?:app)?\.com/invite)/\S+",
    re.IGNORECASE,
)
_LINK_REMOVED = "[link removed]"


_HEADLINE_MAX = 200
_SUMMARY_MAX = 400


def _truncate(text: str, limit: int) -> str:
    """Hard-truncate `text` to `limit` characters, replacing StoryOut's old
    `max_length=...` Field constraints now that those live here instead.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strip_urls(text: str) -> str:
    """Replace any URL-shaped token in `text` with `_LINK_REMOVED`.

    A model writing a headline or summary has no business handing back
    something a Discord client (or `format.py`'s own `<url>` markup) would
    turn into a clickable link -- that's how a phishing link riding along
    with a legitimate Shift code becomes one click instead of a copy-paste.
    Dropping the token outright (rather than, say, de-schemeing it) is the
    simpler of two reasonable choices and the one that reads cleanest in a
    sentence.
    """
    return _URL_TOKEN_RE.sub(_LINK_REMOVED, text)


def _normalize_headline(headline: str) -> str:
    """Casefold, NFKC-normalize, strip punctuation, collapse whitespace.

    Used only to compare `update_of_headline` against prior headlines
    (SPEC-DEV 8). Deliberately exact after normalization, not fuzzy: a
    near-miss match risks silently linking two unrelated stories, and the
    spec's fallback for "no match" (just leave it unlinked) is a fine
    outcome, whereas a wrong link is a worse one.
    """
    folded = unicodedata.normalize("NFKC", headline).casefold()
    stripped = _PUNCTUATION_RE.sub("", folded)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def _match_update_of(update_of_headline: str | None, prior: list[PriorStory]) -> int | None:
    if not update_of_headline:
        return None
    target = _normalize_headline(update_of_headline)
    if not target:
        return None
    # `prior` comes from repo.recent_headlines, newest first, so the first
    # normalized match is also the newest one -- which is the tie-break
    # the plan asks for, for free.
    for story in prior:
        if _normalize_headline(story.headline) == target:
            return story.id
    return None


def postprocess(
    out: StoriesOut, items: list[TopicItem], prior: list[PriorStory]
) -> list[StoryDraft]:
    """Clean up the model's stories before they're allowed anywhere near the store.

    In order: drop anything marked not relevant, keep only item_urls that
    exactly match a URL we actually collected (re-canonicalized, in case
    the model normalized one differently than we did), drop stories left
    with no URLs, downgrade `official` to `reported` unless a linked item
    is actually trust `official`, and match `update_of_headline` back to a
    prior story. The model is told all of these rules in the prompt; this
    function exists because "the model was told" and "the model complied"
    are not the same claim.
    """
    known_urls = {topic_item.item.url for topic_item in items}
    trust_by_url = {topic_item.item.url: topic_item.item.trust for topic_item in items}

    drafts: list[StoryDraft] = []
    for story in out.stories:
        if not story.relevant:
            continue

        urls: list[str] = []
        seen: set[str] = set()
        for raw_url in story.item_urls:
            canonical = canonicalize(raw_url)
            if canonical is None or canonical not in known_urls or canonical in seen:
                continue
            seen.add(canonical)
            urls.append(canonical)
        if not urls:
            continue

        label = story.label
        if label == "official" and not any(trust_by_url[u] == "official" for u in urls):
            label = "reported"

        drafts.append(
            StoryDraft(
                headline=_truncate(_strip_urls(story.headline), _HEADLINE_MAX),
                summary=_truncate(_strip_urls(story.summary), _SUMMARY_MAX),
                label=label,
                item_urls=urls,
                update_of_story_id=_match_update_of(story.update_of_headline, prior),
            )
        )
    return drafts


async def summarize_topic(
    llm: LLMClient,
    topic: Topic,
    items: list[TopicItem],
    prior: list[PriorStory],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    all_topics: list[Topic] | None = None,
) -> TopicSummary:
    """Summarize one topic's items, with retry and a fallback on repeated failure.

    Three attempts, backing off 1s/2s/4s between them, triggered by any
    `LLMError` (a transient API problem, an invalid response). An
    `LLMFatalError` skips straight to the fallback without burning the
    rest of the budget on a request that's going to fail the same way
    again. Token usage accumulates across every attempt, successful or
    not -- a response we can't use still cost money.

    `all_topics` just passes through to `build_prompt` (see its
    docstring); `build_digest` is the one real caller and always has
    `cfg.topics` on hand to pass.
    """
    system, user = build_prompt(topic, items, prior, all_topics=all_topics)
    input_tokens = output_tokens = 0

    for attempt in range(len(_BACKOFF_S)):
        try:
            result = await llm.emit_stories(system, user)
        except LLMFatalError as exc:
            input_tokens += exc.input_tokens
            output_tokens += exc.output_tokens
            break
        except LLMError as exc:
            input_tokens += exc.input_tokens
            output_tokens += exc.output_tokens
            if attempt < len(_BACKOFF_S) - 1:
                await sleep(_BACKOFF_S[attempt])
            continue
        else:
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            return TopicSummary(
                topic_key=topic.key,
                stories=postprocess(result.stories, items, prior),
                fallback=False,
                note=None,
                usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
            )

    return TopicSummary(
        topic_key=topic.key,
        stories=[],
        fallback=True,
        note=_FALLBACK_NOTE,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


__all__ = [
    "MODEL",
    "PRICE_IN_PER_MTOK",
    "PRICE_OUT_PER_MTOK",
    "AnthropicLLM",
    "LLMClient",
    "LLMError",
    "LLMFatalError",
    "LLMResult",
    "StoriesOut",
    "StoryDraft",
    "StoryOut",
    "TopicSummary",
    "postprocess",
    "summarize_topic",
]
