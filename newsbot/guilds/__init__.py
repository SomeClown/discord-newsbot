"""Per-server logic for the public app (design.md §15).

Deliberately imports nothing, for the same reason `newsbot.lounge` doesn't:
these modules reach into `config` and `store`, and an eager import here
would be one more way to build a circle by accident.
"""
