"""The built-in quote list, for servers that haven't picked one.

With no `lounge.daily_quote.sources` in `config.yaml`, the bot draws from
these Wikiquote pages, so the daily quote works the day you switch it on
without anyone having to write a single quote. Every entry is an author whose
works are in the U.S. public domain (published 1930 or earlier, as of 2026).
This file imports nothing on purpose; `config.py` imports it, and I'd like it
to stay the kind of module that can't cause a cycle.
"""

from __future__ import annotations

# Public-domain authors only. Two of these come with fine print, and I'd
# rather write it down than have it surprise someone later: a Wikiquote page
# can quote things published long after the author died (Mark Twain's
# autobiography volumes came out from 2010 on), and it can quote modern
# translations of an old original (Marcus Aurelius), which are copyrighted
# even when the original isn't. The list is the owner's call to trim.
DEFAULT_WIKIQUOTE_PAGES: tuple[str, ...] = (
    "Oscar Wilde",
    "Mark Twain",
    "Benjamin Franklin",
    "William Shakespeare",
    "Jane Austen",
    "Edgar Allan Poe",
    "Marcus Aurelius",
)

# Deliberately NOT in the list above, and please don't uncomment these to be
# helpful. Wikiquote hosts limited excerpts of copyrighted works (films,
# recent books, that sort of thing) under its own fair-use policy. That
# policy covers Wikiquote. A bot reposting one of them every morning is a
# different use, and it carries real copyright risk. So switching these on is
# a decision for the owner, made with eyes open, and never a default. Theme
# pages like "Friendship" mix public-domain and modern quotes the same way, so
# they get the same caution. These are examples of the shape, and I haven't
# checked that each title exists.
#
#     "Fight Club (film)",
#     "Hunter S. Thompson",
#     "Friendship",
