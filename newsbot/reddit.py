"""What counts as Reddit, in one place.

Reddit denied the bot's Data API request (2026-10-04, under its Responsible
Builder Policy), and that policy bars sharing Reddit content without written
approval. So Reddit is a SHiFT-code detector now and nothing else: its posts
are read in memory, searched for codes, and dropped. Several places need to
agree on what "a Reddit address" is (the collector's rate limit, the config
loader's default, the store's last line of defense), and three copies of a
host list is how one of them ends up knowing about `old.reddit.com` and the
others don't. So this is the one copy.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def is_reddit_host(host: str | None) -> bool:
    """True for reddit.com and any subdomain of it (www, old, np, ...)."""
    if not host:
        return False
    host = host.lower().rstrip(".")
    return host == "reddit.com" or host.endswith(".reddit.com")


def is_reddit_url(url: str) -> bool:
    """True if `url` points at Reddit. Junk that doesn't parse is not Reddit."""
    try:
        return is_reddit_host(urlsplit(url).hostname)
    except ValueError:
        return False
