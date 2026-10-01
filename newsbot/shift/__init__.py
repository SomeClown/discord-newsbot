"""SHiFT code alerts (design.md §12 and §15): matching, deciding, and the per-server fan-out.

Kept separate from `newsbot/pipeline/` on purpose: this is a second,
much smaller pipeline (no Claude, no `items`/`stories` writes) that happens
to share the hourly collection pass and a database with the digests, not an
extension of them. "shift" is Gearbox's own name for these redeem codes; it has nothing
to do with a work shift, which confused exactly one docstring draft before
this one.
"""

from __future__ import annotations
