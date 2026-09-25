"""The `Publisher` protocol: the one seam between the pipeline and wherever the digest goes.

`run.py` builds a `RenderedDigest` and hands it to whichever publisher it
was given. The CLI's publisher prints it; the bot's (`bot/client.py`,
`DiscordPublisher`, a later step) posts it to a channel and a thread. Every
other piece of this pipeline -- the double-post guard, the save
transaction, the publish retry with backoff -- gets written and tested
once, against a publisher that just prints to a terminal, instead of twice
against a terminal and a live gateway connection. That trade cost one
small interface; it's been worth it every time I've had to debug a save
path without also needing a bot token.
"""

from __future__ import annotations

from typing import Protocol

from newsbot.bot.format import RenderedDigest, to_text


class PublishError(Exception):
    """A publish attempt failed. `run_daily` retries a few times before giving up on it.

    `posted_ids` carries whatever message ids the failed attempt already
    got back from Discord before it died -- a `DiscordPublisher` posts the
    header, then a thread, then one message per embed batch, and any of
    those can be the one that fails. Without this, a publish that got the
    header and two embeds out before a third one timed out would report
    "nothing posted" right alongside a channel that very much has a
    header and two embeds sitting in it.
    """

    def __init__(self, message: str, posted_ids: list[int] | None = None) -> None:
        super().__init__(message)
        self.posted_ids: list[int] = posted_ids or []


class Publisher(Protocol):
    async def publish(self, r: RenderedDigest) -> list[int]:
        """Publish a rendered digest and return the ids of whatever got posted.

        Raises `PublishError` on failure -- never posts half a digest and
        calls it a success.
        """
        ...


class PrintPublisher:
    """Writes the digest to stdout instead of posting it anywhere.

    Message ids don't mean anything outside a real chat, so this always
    returns an empty list; `save_run` is happy to store `[]` for
    `posted_message_ids` just as it would for any other run.
    """

    async def publish(self, r: RenderedDigest) -> list[int]:
        print(to_text(r))
        return []


__all__ = ["PrintPublisher", "PublishError", "Publisher"]
