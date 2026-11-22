"""Teach a throwaway copy of an old `prompts.py` the v3.4.1 prompt changes, and nothing else.

Several tests load `newsbot/pipeline/prompts.py` from v3.3.0 and check that ordinary input still
builds the same prompt. v3.4.1 changed it on purpose: one more rule in the system prompt, and a
`published` date on every item. So "the same" now means "the same, plus those two". This adds
exactly those to the old copy and leaves the rest of it alone, so everything else in the prompt
(the header prose, the fencing, the escaping, the title cap) is still compared byte for byte.
"""

from __future__ import annotations

import types

from newsbot.pipeline.prompts import _published_date

_DATES_RULE = (
    "- Each item has a published date (null when its source gave none), and today's date "
    "is given above the items. Sources sometimes get their dates wrong, so also read the "
    "item's own text: if it shows the item is old news (it calls something upcoming or new "
    "whose date is weeks or months before today, or it carries a dateline "
    "weeks or months before today), "
    "set relevant to false for a story built only on such items. This is only about news "
    "that is no longer news: a guide, a deal that's still running, or a code that hasn't "
    "expired is welcome whatever its date.\n"
)
_AFTER = "one out if it doesn't actually belong.\n"


def date_prompts_like_v341(old: types.ModuleType) -> types.ModuleType:
    """Return `old` with the dates rule in `SYSTEM_PROMPT` and `published` in `_item_payload`."""
    assert _AFTER in old.SYSTEM_PROMPT
    old.SYSTEM_PROMPT = old.SYSTEM_PROMPT.replace(_AFTER, _AFTER + _DATES_RULE, 1)
    payload = old._item_payload

    def dated_payload(n, topic_item):
        out = {}
        for key, value in payload(n, topic_item).items():
            if key == "uncertain":
                # Production's own conversion, so a naive stamp is read as UTC here too and
                # not as whatever zone this machine happens to be in.
                out["published"] = _published_date(topic_item.item.published_at)
            out[key] = value
        return out

    old._item_payload = dated_payload
    return old
