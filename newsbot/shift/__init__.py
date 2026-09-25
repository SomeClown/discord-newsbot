"""SHiFT code alerts (design.md §12): the hourly sweep, its matcher, and its guards.

Kept separate from `newsbot/pipeline/` on purpose -- this is a second,
much smaller pipeline (no Claude, no `items`/`stories` writes) that happens
to share a run lock and a database with the daily digest, not an extension
of it. "shift" is Gearbox's own name for these redeem codes; it has nothing
to do with a work shift, which confused exactly one docstring draft before
this one.
"""

from __future__ import annotations
