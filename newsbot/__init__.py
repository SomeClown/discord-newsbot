"""discord-newsbot: reads the internet every morning so the server doesn't have to.

The package is laid out in layers that mostly don't know about each other:
``collectors`` fetch raw items from RSS, Steam, Bluesky and web search;
``pipeline`` normalizes, filters, summarizes and orchestrates a daily run;
``store`` is the only place that speaks SQL; ``bot`` is the discord.py
adapter on top of all of it. The pipeline can run headlessly through
``python -m newsbot.pipeline.run --dry-run`` with no Discord connection at
all, which is also how most of this gets tested.
"""
