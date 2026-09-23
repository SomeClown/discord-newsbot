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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _strip_markup(html_or_bbcode: str) -> str:
    """Strip HTML tags (keeping line breaks) and Steam BBCode tags."""
    extractor = _TextExtractor()
    extractor.feed(html_or_bbcode)
    extractor.close()
    return _BBCODE_RE.sub("", extractor.text())


def _truncate(value: str, limit: int) -> str:
    """Truncate on a word boundary, adding an ellipsis if anything was cut."""
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(" ", 1)[0]
    # A single "word" longer than the limit (a bare URL, usually) has
    # nowhere to break, so just hard-cut it rather than return nothing.
    return (cut or value[:limit]).rstrip() + "…"


def clean_text(html_or_bbcode: str, limit: int = 500) -> str:
    """Strip HTML/BBCode from `html_or_bbcode`, collapse whitespace, and truncate.

    Line breaks carry no meaning once the text is headed into a one-line
    excerpt or an LLM prompt, so this collapses everything -- tags,
    newlines, runs of spaces -- down to single spaces before truncating.
    """
    stripped = _strip_markup(html_or_bbcode)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return _truncate(collapsed, limit)


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
