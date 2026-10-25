"""What counts as a Discord invite, in one place.

A hijacked or odd feed can hand us an entry whose own link is
`https://discord.gg/abc`, and the bot would print it as a live invite: in a
digest, in a headline list, or under a SHiFT code as the post it came from. The
collector drops such items (`pipeline/collect.py`) and the SHiFT renderers
leave the link off (`bot/format.py`). Two places needing to agree on what an
invite is, from two sides of the import graph, is how one of them ends up
knowing about `discordapp.com` and the other doesn't. So this is the one copy.

Gift links (`discord.gift/...`, `/gifts/...`) and the usual invite shorteners
(`dsc.gg`, `discord.me`) get the same treatment: a free-Nitro link in a news
item is a classic scam, and nobody here wants to explain it at 9:01 a.m.

The check judges the address the way a browser would read it, not the way it was
typed: `https://discord%2Egg/abc`, a full-width host, a soft hyphen in the middle
of the name, `/./invite/` and a backslash for a slash all end up at the same place
as the plain spelling, and `canonicalize` lets every one of them through.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import unquote, urlsplit

# These hosts are nothing but invites (or gifts, or shorteners for them). A subdomain
# (ptb., canary., www.) counts as its parent.
_ALWAYS_HOSTS = ("discord.gg", "dsc.gg", "discord.me", "discord.gift")
# These only count under the first path segment named here (discord.com/channels/...
# is an ordinary link, and so is the blog).
_PATH_HOSTS = ("discord.com", "discordapp.com")
_PATH_SEGMENTS = ("invite", "gifts")


def _on_host(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _browser_host(host: str) -> str:
    """The host after percent-decoding and the IDNA mapping a browser applies.

    NFKC folds full-width letters, the IDNA mapping drops soft hyphens and zero-width
    characters and lowercases, and the ideographic full stops count as dots. A name that
    won't encode (a stray empty label, say) is judged as it stands, lowercased.
    """
    host = unicodedata.normalize("NFKC", unquote(host))
    host = host.replace("。", ".").rstrip(".")
    try:
        return host.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return host.lower()


def _first_segment(path: str) -> str:
    """The first path segment once dot segments are collapsed, lowercased and decoded."""
    stack: list[str] = []
    for raw_segment in path.split("/"):
        segment = unquote(raw_segment).lower()
        if segment == "..":
            if stack:
                stack.pop()
        elif segment not in ("", "."):
            stack.append(segment)
    return stack[0] if stack else ""


def is_discord_invite(url: str) -> bool:
    """True if `url` itself is a Discord invite or gift (not an article that mentions one).

    Backslashes count as slashes before the query or fragment (as they do in a browser),
    then the host and the first path segment are judged in the normalized form above.
    Junk that doesn't parse is not an invite (`canonicalize` will have opinions about it
    later).
    """
    url = url.strip()
    # A backslash is a slash up to the query; after it, it's just a backslash.
    cut = min((i for i in (url.find("?"), url.find("#")) if i >= 0), default=len(url))
    url = url[:cut].replace("\\", "/") + url[cut:]
    try:
        parts = urlsplit(url)
        raw_host = parts.hostname or ""
    except ValueError:
        return False
    host = _browser_host(raw_host)
    if any(_on_host(host, domain) for domain in _ALWAYS_HOSTS):
        return True
    if any(_on_host(host, domain) for domain in _PATH_HOSTS):
        return _first_segment(parts.path) in _PATH_SEGMENTS
    return False
