"""Turn whatever markup a source hands us into plain, excerpt-sized text.

Every source has its own idea of "text": RSS feeds give us HTML (sometimes
escaped twice), Steam announcements use BBCode from what still looks like a
2004-era forum package, and nobody agrees on where a sentence ends. This
module is the one place that turns all of that into a plain string capped
at a sane length, so downstream code (the LLM prompt, the RSS title
fallback for feeds that skip the `<title>` tag entirely) never has to know
an HTML entity from a hole in the ground.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Tags whose start or end implies a line break in the rendered text. This
# list doesn't need to be exhaustive -- worst case a missed tag just joins
# two sentences with a space instead of a newline, which `clean_text`
# collapses away anyway.
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

# Steam's BBCode, e.g. [b]bold[/b] or [url=https://...]. This strips the
# bracket tokens only; whatever text sits between them (including a raw
# [img] URL) is left for `clean_text`'s whitespace collapsing to sort out.
_BBCODE_RE = re.compile(r"\[/?[a-zA-Z*]+(?:=[^\]]*)?\]")


class _TextExtractor(HTMLParser):
    """Collects the text content of an HTML fragment, one block tag = one newline."""

    def __init__(self, *, separate_inline: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._inline_sep = " " if separate_inline else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._parts.append("\n" if tag in _BLOCK_TAGS else self._inline_sep)

    def handle_endtag(self, tag: str) -> None:
        self._parts.append("\n" if tag in _BLOCK_TAGS else self._inline_sep)

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _strip_markup(html_or_bbcode: str, *, separate_inline: bool = False) -> str:
    """Strip HTML tags (keeping line breaks) and Steam BBCode tags.

    With `separate_inline`, every inline tag becomes a space instead of
    vanishing, so `[b]CODE[/b]Golden` doesn't weld the code onto the next
    word. Off by default, because it also turns `<b>BL</b>4` into "BL 4".
    """
    extractor = _TextExtractor(separate_inline=separate_inline)
    extractor.feed(html_or_bbcode)
    extractor.close()
    return _BBCODE_RE.sub(" " if separate_inline else "", extractor.text())


def _truncate(value: str, limit: int) -> str:
    """Truncate on a word boundary, adding an ellipsis if anything was cut."""
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(" ", 1)[0]
    # A single "word" longer than the limit (a bare URL, usually) has
    # nowhere to break, so just hard-cut it rather than return nothing.
    return (cut or value[:limit]).rstrip() + "…"


def _collapse(html_or_bbcode: str, *, separate_inline: bool = False) -> str:
    """Strip HTML/BBCode from `html_or_bbcode` and collapse whitespace to single spaces.

    Line breaks carry no meaning once the text is headed into a one-line
    excerpt, an LLM prompt, or a code matcher that only cares whether two
    tokens are separated by *some* whitespace -- so this collapses
    everything (tags, newlines, runs of spaces) down to single spaces.
    Shared by `clean_text` and `plain_text`, which differ only in how much
    of the result they keep.
    """
    stripped = _strip_markup(html_or_bbcode, separate_inline=separate_inline)
    return re.sub(r"\s+", " ", stripped).strip()


def clean_text(html_or_bbcode: str, limit: int = 500) -> str:
    """Strip HTML/BBCode from `html_or_bbcode`, collapse whitespace, and truncate.

    The 500-character default is excerpt-sized: this is what goes in
    front of the LLM and in `/news recent`, not what a code matcher scans.
    """
    return _truncate(_collapse(html_or_bbcode), limit)


def plain_text(html_or_bbcode: str, limit: int = 100_000) -> str:
    """Like `clean_text`, but capped for a whole article instead of a one-line excerpt.

    `RawItem.full_text` exists so the SHiFT code matcher (design.md §12)
    can scan a source's *entire* post instead of the 500-character excerpt
    stored for summaries -- a code five paragraphs into a patch-notes post
    would otherwise never be seen. 100,000 characters is a "this should
    never actually bind" ceiling, not a real limit; nothing we collect is
    that long, and untruncated text never gets persisted or sent to the
    LLM regardless (see `RawItem.full_text`'s docstring).

    Markup is a trap for the matcher in both directions. Strip tags with
    no separator and `[b]CODE[/b]Golden Keys` glues the code to "Golden";
    strip them with a space and `<b>ABCDE</b>-FGHIJ-...` falls apart at
    the hyphen. Rather than pick which half of the codes to miss, this
    returns both renderings, one per line, when they differ. Nobody reads
    `full_text` but the matcher, and the matcher dedupes, so the only cost
    is a little memory. (test-engineer found the gluing case; I had
    confidently assumed tags were always followed by whitespace.)
    """
    joined = _collapse(html_or_bbcode)
    spaced = _collapse(html_or_bbcode, separate_inline=True)
    if joined == spaced:
        return _truncate(joined, limit)
    return _truncate(joined, limit) + "\n" + _truncate(spaced, limit)


def first_line(html_or_text: str, limit: int = 120) -> str:
    """The first non-blank line of `html_or_text`, cleaned and truncated.

    Used when a source gives us a description and no title at all -- the
    official Bluesky accounts' RSS mirror does exactly this, since a post
    doesn't really have a headline.
    """
    stripped = _strip_markup(html_or_text)
    for line in stripped.split("\n"):
        candidate = re.sub(r"[ \t]+", " ", line).strip()
        if candidate:
            return _truncate(candidate, limit)
    return ""
