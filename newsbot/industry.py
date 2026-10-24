"""The "Gaming industry" catalog entry is marked by its key, not by a flag.

Studio closures, layoffs and acquisitions aren't about any one game, so the catalog
gets a pseudo-game for them and servers follow it like any other. The obvious way
to say "this one is special" is a flag on the catalog entry. I didn't, because the
SQL trigger that caps a server at 10 games has to name a key anyway, and a flag
would only give the two of them a second way to disagree. This module is a leaf
(it imports nothing from the bot) so config, filter, repo, summaries and format can
all use it without anyone's layering getting upset.
"""

from __future__ import annotations

INDUSTRY_KEY = "industry"

# The most stories one industry post shows. The pick is the model's, in its own
# order of significance; this is only the ceiling.
INDUSTRY_MAX_STORIES = 5


def counts_toward_limit(game_key: str) -> bool:
    """Whether following `game_key` uses up one of a server's game slots.

    Only the industry entry is free (it's one post a day, last, and not a game).
    """
    return game_key != INDUSTRY_KEY
