"""What's left of the v2 SHiFT sweep: the poster protocol and the retry-with-no-second-ping rule.

This module used to be the whole v2 alert path: collect, plan, claim, post, one
server, one ping budget. The hourly shared collection and the per-server
fan-out (`pipeline/collect.py` and `shift/fanout.py`) took all of that over,
and the sweep, the test-alert command and their plumbing went with `run_daily`
at the cutover. Two pieces survived because the fan-out still needs them
and rewriting them would only have been an opportunity to break something that
works: the `CodeAlertPoster` protocol (plus a print-to-the-terminal poster for
the CLI), and `post_alert_with_retry`, which carries the one rule I'd never
trade away. A retry never risks a second live ping.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Protocol

from newsbot.bot.format import RenderedAlert
from newsbot.pipeline.publisher import PublishError

# Same backoff the digest publisher retries against (pipeline/run.py's
# _PUBLISH_BACKOFF_S): a code alert message is posted the same way a
# digest embed batch is, so it gets the same "give Discord a few seconds
# to recover from a blip" policy.
_POST_BACKOFF_S = (2.0, 4.0, 8.0)


class CodeAlertPoster(Protocol):
    async def post(self, alert: RenderedAlert) -> int | None:
        """Post one alert message and return its Discord message id (if any).

        Raises `PublishError` for a failure worth retrying (a 5xx, a
        timeout); anything else raised is treated as final: see
        `post_alert_with_retry`.
        """
        ...


class PrintCodeAlertPoster:
    """Prints an alert to stdout instead of posting it: the CLI's `--collect` poster."""

    async def post(self, alert: RenderedAlert) -> int | None:
        print(alert.content)
        return None


_PING_PREFIX = "@everyone "


def _strip_ping(alert: RenderedAlert) -> RenderedAlert:
    """A copy of `alert` with the ping turned off: same nonce, same codes.

    Used by `post_alert_with_retry` when a ping-bearing send raised
    `PublishError`: our client gave up waiting for a response, but that's
    not proof Discord never got the message (the deterministic `nonce`
    handles that half). What it *doesn't* rule out is that Discord got it
    and the ping already went out, so a retry must never risk a second
    live `@everyone` for the same batch. The content's "@everyone " prefix
    is stripped the same deterministic way `render_code_alerts` added it,
    rather than re-rendering from the candidates (which
    `post_alert_with_retry` doesn't have; only the already-rendered `RenderedAlert` does).
    """
    content = alert.content
    prefix = alert.ping_prefix or _PING_PREFIX
    if content.startswith(prefix):
        content = content[len(prefix) :]
    return dataclasses.replace(alert, content=content, ping=False, ping_prefix="")


async def post_alert_with_retry(
    poster: CodeAlertPoster, sleep: Callable[[float], Awaitable[None]], alert: RenderedAlert
) -> tuple[int | None, Exception | None]:
    """Post one message, retrying only on `PublishError`, up to `_POST_BACKOFF_S`'s length.

    Any other exception is treated as final without a retry: a poster
    bug or an auth failure isn't going to fix itself by waiting eight
    seconds. `asyncio.CancelledError` (a `BaseException`, not an
    `Exception`) is deliberately not caught here at all: a delivery cancelled
    mid-post should propagate, leaving its already-claimed codes `pending`
    for the next startup's recovery to find, not get silently
    marked `failed` by a handler that was never meant to catch it.

    Every attempt after the first reuses `alert.nonce` (set once by
    `render_code_alerts` and never changed here) so Discord can dedupe a
    retry against a first attempt that actually landed but whose response
    we never saw. That covers a retried post landing twice in the
    channel; it says nothing about a retried *ping*, which Discord's
    nonce dedup has no opinion on: a duplicate message with the ping
    stripped is still a duplicate message, but a duplicate `@everyone` is
    the one failure mode worth refusing to risk even once. So a
    ping-bearing alert that fails once retries with the ping already
    turned off (`_strip_ping`): the first attempt is the only one that
    ever could have pinged, whether or not it actually landed.
    """
    current = alert
    last_error: Exception | None = None
    for attempt in range(len(_POST_BACKOFF_S) + 1):
        try:
            message_id = await poster.post(current)
            return message_id, None
        except PublishError as exc:
            last_error = exc
            if current.ping:
                current = _strip_ping(current)
            if attempt < len(_POST_BACKOFF_S):
                await sleep(_POST_BACKOFF_S[attempt])
        except Exception as exc:  # non-PublishError: final immediately, no retry
            return None, exc
    return None, last_error


_ROUNDUP_CAP_ALERT = (
    "newsbot: SHiFT roundup code cap reached ({count} per check); "
    "{overflow} code(s) recorded silently, not posted this check."
)


__all__ = [
    "CodeAlertPoster",
    "PrintCodeAlertPoster",
    "post_alert_with_retry",
]
