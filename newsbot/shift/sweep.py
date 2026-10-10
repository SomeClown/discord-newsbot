"""What's left of the v2 SHiFT sweep: the poster protocol and the retry-with-no-second-ping rule.

This module used to be the whole v2 alert path: collect, plan, claim, post, one
server, one ping budget. The hourly shared collection and the per-server
fan-out (`pipeline/collect.py` and `shift/fanout.py`) took all of that over,
and the sweep, the test-alert command and their plumbing went with `run_daily`
at the cutover. Two pieces survived because the fan-out still needs them
and rewriting them would only have been an opportunity to break something that
works: the `CodeAlertPoster` protocol (plus a print-to-the-terminal poster for
the CLI), and `post_alert_with_retry`, which carries the one rule I'd never
trade away. A retry never risks a second live ping that could have landed.
"""

from __future__ import annotations

import dataclasses
import logging
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


class MessageGoneError(Exception):
    """The alert message (or the channel it was in) no longer exists, or never can be edited.

    Raised by a `CodeAlertEditor`. It's the one "done, quietly" outcome of an edit: a
    deleted message is the server's business, and nobody needs telling.
    """


class ChannelGoneError(MessageGoneError):
    """The whole channel is gone, or isn't one this server's alerts can be edited in.

    A kind of `MessageGoneError` that the edit pass answers for every message in the
    channel at once, so a deleted channel costs one lookup and not one per message.
    """


class EditForbiddenError(Exception):
    """The bot isn't allowed to see or edit the alert's channel (a 403, 50001 or 50013).

    Raised by a `CodeAlertEditor`. The edit pass tells the server's admins once and stops
    editing there until the server's SHiFT settings are saved again.
    """


class CodeAlertEditor(Protocol):
    async def edit(self, message_id: int, content: str) -> None:
        """Replace one alert message's text, with every mention switched off.

        Raises `MessageGoneError` or `EditForbiddenError` for those two outcomes, and
        `PublishError` for a failure worth another try later (a 5xx, a timeout, or a 429,
        whose `retry_after` says how long Discord wants us to wait). Anything else is a bug
        or an error nobody planned for, and the edit pass logs it and moves on.
        """
        ...


class PrintCodeAlertPoster:
    """Prints an alert to stdout instead of posting it: the CLI's `--collect` poster."""

    async def post(self, alert: RenderedAlert) -> int | None:
        print(alert.content)
        return None


logger = logging.getLogger(__name__)

_PING_PREFIX = "@everyone "


def _strip_ping(alert: RenderedAlert) -> RenderedAlert:
    """A copy of `alert` with the ping turned off: same nonce, same codes.

    Used by `post_alert_with_retry` once a ping-bearing send has failed
    ambiguously (a timeout, a 5xx, a dropped connection): our client gave up
    waiting for a response, but that's not proof Discord never got the message
    (the deterministic `nonce` handles that half). What it *doesn't* rule out is that Discord got it
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
    poster: CodeAlertPoster,
    sleep: Callable[[float], Awaitable[None]],
    alert: RenderedAlert,
    on_ping_stripped: Callable[[], None] | None = None,
) -> tuple[int | None, Exception | None]:
    """Post one message, retrying only on `PublishError`, up to `_POST_BACKOFF_S`'s length.

    A 429 arrives as a `PublishError` too. If Discord said how long to wait, we
    wait that long instead when it's longer than the step's backoff, but never
    past the budget's last step (`_POST_BACKOFF_S[-1]`): a server told to wait a
    minute isn't worth a minute of the whole fan-out's time.

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
    the one failure mode worth refusing to risk even once.

    So the ping follows what we know about the failures so far (owner
    decision D13). A 429 (`PublishError.rejected`) means Discord refused the
    message: nothing landed, nobody was pinged, and the ping is still owed, so
    the retry keeps it. Any ambiguous failure (a timeout, a 5xx, a connection
    error, anything not marked `rejected`) might have landed and pinged, so
    from then on every attempt goes out with the ping stripped (`_strip_ping`),
    even if a later failure is a 429. The invariant: at most one ping-bearing
    send that could have landed. Several ping-bearing *attempts* can happen
    (a run of 429s), but each was refused, so only the last one can have
    delivered a ping. The ping budget is claimed once, before any of this, and
    isn't touched by retries.

    `on_ping_stripped` is called when a ping gets stripped (the fan-out uses it
    to say why a server's alert went out unpinged).
    """
    current = alert
    last_error: Exception | None = None
    ambiguous = False
    for attempt in range(len(_POST_BACKOFF_S) + 1):
        try:
            message_id = await poster.post(current)
            return message_id, None
        except PublishError as exc:
            last_error = exc
            if not exc.rejected:
                ambiguous = True
            if ambiguous and current.ping:
                current = _strip_ping(current)
                # The one place a server's ping quietly turns into no ping; say so.
                logger.warning(
                    "SHiFT ping stripped after an ambiguous failure",
                    extra={"codes": len(alert.codes)},
                )
                if on_ping_stripped is not None:
                    on_ping_stripped()
            if attempt < len(_POST_BACKOFF_S):
                wait = _POST_BACKOFF_S[attempt]
                if exc.retry_after is not None:
                    wait = max(wait, min(exc.retry_after, _POST_BACKOFF_S[-1]))
                await sleep(wait)
        except Exception as exc:  # non-PublishError: final immediately, no retry
            return None, exc
    return None, last_error


_ROUNDUP_CAP_ALERT = (
    "newsbot: SHiFT roundup code cap reached ({count} per check); "
    "{overflow} code(s) recorded silently, not posted this check."
)


__all__ = [
    "CodeAlertEditor",
    "ChannelGoneError",
    "CodeAlertPoster",
    "EditForbiddenError",
    "MessageGoneError",
    "PrintCodeAlertPoster",
    "post_alert_with_retry",
]
