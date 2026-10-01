"""The `Publisher` protocol: the one seam between the pipeline and wherever the digest goes.

`run.py` builds a `RenderedDigest` and hands it to whichever publisher it
was given. The CLI's publisher prints it; the bot's (`bot/client.py`,
`DiscordPublisher`) posts each topic's embed to that topic's own channel
(design.md §13). Every other piece of this pipeline (the double-post
guard, the save transaction, the publish retry with backoff) gets
written and tested once, against a publisher that just prints to a
terminal, instead of twice against a terminal and a live gateway
connection. That trade cost one small interface; it's been worth it every
time I've had to debug a save path without also needing a bot token.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from newsbot.bot.format import RenderedDigest, to_text


class PublishError(Exception):
    """A publish attempt failed. The digest runner retries a few times before giving up on it.

    `posted_by_topic` carries whatever topic -> message id mapping the
    failed attempt already got back from Discord before it died: a
    `DiscordPublisher` posts one message per topic, to that topic's own
    channel, and any one of those sends can be the one that fails. Without
    this, a publish that got two topics' embeds out before a third one
    timed out would report "nothing posted" right alongside two channels
    that very much have an embed sitting in them.

    `posted_ids` is `list(posted_by_topic.values())`, the same ids,
    flattened, since that's the shape `digests.posted_message_ids` and
    everything upstream of `DiscordPublisher` (the double-post guard, the
    run-now confirmation) has always dealt in, and still does (design.md
    §13's plan: "`digests.posted_message_ids` stays a flat JSON list").

    `retryable` is `False` for a permanent per-channel error (a 4xx, a
    channel that's gone or a permission that's missing, design.md §13's
    D2): `_publish_with_retry` shouldn't burn its backoff budget retrying
    something no amount of waiting will fix; whatever's in
    `posted_by_topic` at that point is what gets recorded on the `failed`
    row.

    `retry_after` is how many seconds Discord said to wait, when the failure
    was a 429 that said so. Only the SHiFT code alert retry reads it.
    """

    def __init__(
        self,
        message: str,
        posted_ids: list[int] | None = None,
        *,
        posted_by_topic: Mapping[str, int] | None = None,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.posted_by_topic: dict[str, int] = dict(posted_by_topic or {})
        self.posted_ids: list[int] = (
            list(posted_ids) if posted_ids is not None else list(self.posted_by_topic.values())
        )
        self.retryable = retryable
        self.retry_after = retry_after


class Publisher(Protocol):
    async def publish(self, r: RenderedDigest) -> dict[str, int]:
        """Publish a rendered digest and return topic key -> message id for whatever posted.

        Raises `PublishError` on failure: never posts half a digest and
        calls it a success.
        """
        ...


class PrintPublisher:
    """Writes the digest to stdout instead of posting it anywhere.

    Message ids don't mean anything outside a real chat, so this always
    returns an empty mapping; `save_run` is happy to store `[]` for
    `posted_message_ids` just as it would for any other run.
    """

    async def publish(self, r: RenderedDigest) -> dict[str, int]:
        print(to_text(r))
        return {}


__all__ = ["PrintPublisher", "PublishError", "Publisher"]
