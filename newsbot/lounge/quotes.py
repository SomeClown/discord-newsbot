"""Split, hash, deal and render quotes, with no idea where they came from.

Think of a deck of cards at a card table where nobody has ever written a name
on the back of a card. You can't say "the queen of spades is used up"; you
can only recognize her by her face. That's how the quote deck works: a quote
is identified by a hash of its normalized text, so a list can be edited
freely (quotes added, removed, retyped in a different case) and the deck
keeps track without a single line of bookkeeping. The price is that a real
rewording is a brand new card, which I've decided to live with.

This module also splits text in the `fortune` format (entries separated by a
line holding only `%`) and renders the message that goes to the lounge. It
knows nothing about files, URLs or Wikiquote; `sources.py` and `wikiquote.py`
turn those into `Quote` objects and hand them here. It imports `bot.format`
for `esc` and the UTF-16 length helpers, which is why nothing loaded at
config time may import it.
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from dataclasses import dataclass

from newsbot.bot.format import _ALERT_CONTENT_LIMIT, discord_len, esc
from newsbot.store.models import QuoteDeckState

# Placeholders until the owner picks the wording (plan checkpoint B).
QUOTE_HEADER = "**Quote of the day**"
ATTRIBUTION_PREFIX = "~ "
WIKIQUOTE_LINK_LABEL = "From Wikiquote:"

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Quote:
    """One quote. `link` is set only by the Wikiquote parser, from `urllib.parse.quote` output."""

    text: str
    attribution: str | None = None
    link: str | None = None


@dataclass(frozen=True)
class QuotePick:
    """What `choose_quote` chose, its hash, and whether the deck was reshuffled to get it."""

    quote: Quote
    hash: str
    reshuffle: bool


def render_quote_message(quote: Quote) -> str:
    """Build the lounge message: header, text, optional attribution, optional Wikiquote link.

    Text and attribution go through `esc()`. The link line does not: it's
    wrapped in `<...>` (which also stops the link preview) and must only
    ever come from `urllib.parse.quote` output, which can't contain `>`.
    """
    lines = [QUOTE_HEADER, esc(quote.text)]
    if quote.attribution:
        lines.append(f"{ATTRIBUTION_PREFIX}{esc(quote.attribution)}")
    if quote.link:
        lines.append(f"{WIKIQUOTE_LINK_LABEL} <{quote.link}>")
    return "\n".join(lines)


def fits(quote: Quote) -> bool:
    """True if the rendered message is at most 2,000 UTF-16 units (measured after escaping)."""
    return discord_len(render_quote_message(quote)) <= _ALERT_CONTENT_LIMIT


def split_fortune(raw: str) -> tuple[list[str], int]:
    """Split `fortune`-format text into entries; return them and how many were too long.

    A separator is a line that is just `%` once stripped, so `%%` and
    `% text` are ordinary text. Entries are stripped, empty ones vanish,
    and attribution lines stay inside the entry exactly as written. An
    entry is "too long" if its rendered message won't fit in a Discord
    message, which is not the same as the raw text being short: escaping
    can double a run of asterisks.
    """
    raw = raw.removeprefix("﻿")
    entries: list[str] = []
    current: list[str] = []
    for line in [*raw.splitlines(), "%"]:
        if line.strip() == "%":
            entry = "\n".join(current).strip()
            if entry:
                entries.append(entry)
            current = []
        else:
            current.append(line.rstrip())
    kept = [e for e in entries if fits(Quote(e))]
    return kept, len(entries) - len(kept)


def quote_hash(text: str) -> str:
    """Return the sha256 hex of `text` after NFKC, casefold, whitespace collapse and strip.

    The attribution isn't hashed, so a Wikiquote editor tidying a citation
    doesn't put a used quote back in the deck.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def choose_quote(quotes: list[Quote], deck: QuoteDeckState, rng: random.Random) -> QuotePick | None:
    """Pick the next quote from one source's deck; None only if `quotes` is empty.

    Duplicates (by hash) collapse to their first appearance. Candidates are
    the hashes not yet used. With none left it's a reshuffle: everything
    except `deck.last_hash`, so yesterday's quote can't come straight back.
    A one-quote source has nothing else to offer and repeats itself.
    """
    by_hash: dict[str, Quote] = {}
    for q in quotes:
        by_hash.setdefault(quote_hash(q.text), q)
    if not by_hash:
        return None

    candidates = set(by_hash) - deck.used
    reshuffle = not candidates
    if reshuffle:
        candidates = set(by_hash) - {deck.last_hash}
        if not candidates:
            candidates = set(by_hash)
    # sorted() because set order isn't stable, and a seeded RNG should be.
    chosen = rng.choice(sorted(candidates))
    return QuotePick(by_hash[chosen], chosen, reshuffle)
