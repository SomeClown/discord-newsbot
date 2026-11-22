"""Teach a throwaway copy of an old `format.py` to write masked links, and nothing else.

Several tests load `newsbot/bot/format.py` from an earlier commit and check that ordinary
content still renders the same. Since v3.4.0 a story's links and a headline's link are
`[domain](<url>)` and not a bare `<url>`, so "the same" has to mean "the same, with the new
link text". This wraps the old copy's two link writers to turn each bare `<url>` into the
masked form (using the real label rules), and gives it the cut that never stops inside one.
Everything else in the old copy, the invite filter included (or not included, for v3.3.0),
runs as it did.
"""

from __future__ import annotations

import re
import types

from newsbot.bot import format as fmt

_BARE = re.compile(r"<(https?://[^>\s]+)>")
_TRAILING_BARE = re.compile(r" — <(https?://[^>\s]+)>$")


def mask_links_like_v340(old: types.ModuleType) -> types.ModuleType:
    """Return `old` with `_link_line`, `_headline_line` and `_truncate_description` updated."""
    link_line = old._link_line
    headline_line = old._headline_line

    def masked_link_line(urls: list[str]) -> str:
        seen: dict[str, int] = {}

        def mask(match: re.Match[str]) -> str:
            url = match.group(1)
            label = fmt._link_label(url)
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label} ({seen[label]})"
            return fmt._masked_link(label, url)

        return _BARE.sub(mask, link_line(urls))

    def masked_headline_line(item):
        line = headline_line(item)
        if line is None:
            return None
        return _TRAILING_BARE.sub(
            lambda m: f" — {fmt._masked_link(fmt._link_label(m.group(1)), m.group(1))}", line
        )

    old._link_line = masked_link_line
    old._headline_line = masked_headline_line
    old._truncate_description = fmt._truncate_description
    return old
