"""The one User-Agent string every outbound HTTP request in this codebase sends.

Used to be three separate hardcoded copies (`pipeline/run.py`,
`bot/client.py`, `collectors/rss.py`), which is exactly the kind of thing
that drifts: one of them said "contact: owner" and another said
"+contact", neither of which is a contact anyone could actually reach.
Reddit in particular is unforgiving about a generic or missing one (see
docs/sources-research.md), and now that this bot is meant to run on
someone else's server, "the owner" isn't even a meaningful phrase;
`NEWSBOT_CONTACT` (a URL or an email, an owner's own choice) is what
fills in the "how do I reach you" part.

Unset, this falls back to the repo URL plus a plain admission that
nobody's said who to contact. `warn_if_contact_unset` is the startup-time
half of that: both the bot (`setup_hook`) and the CLI (`main`) call it
once, so a fork running with no contact info still works, it just isn't
lying about it, and it hears about that exactly once, not once per
request.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

logger = logging.getLogger(__name__)

# Fallback for a source checkout that was never `pip install -e .`'d, or
# any other reason importlib.metadata can't find the distribution. Kept in
# sync with pyproject.toml's [project].version by hand. For two releases
# this comment claimed nothing could catch it drifting, which was true only
# because nobody had written the three-line test that does; see
# test_fallback_version_matches_pyproject.
_FALLBACK_VERSION = "3.0.2"

_REPO_URL = "https://github.com/someclown/discord-newsbot"
_UNSET_CONTACT = f"{_REPO_URL}, contact unset"


def _package_version() -> str:
    try:
        return version("newsbot")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def build_user_agent(env: Mapping[str, str] = os.environ) -> str:
    """Build this process's User-Agent string.

    `discord-newsbot/<version> (+<contact>)`, where `<contact>` is
    `NEWSBOT_CONTACT` if set, or the repo URL plus "contact unset"
    otherwise. Pure: doesn't log anything, so it's safe to call as often
    as a caller likes; `warn_if_contact_unset` is the one place that
    complains about a missing contact, and only at startup.
    """
    contact = _usable_contact(env) or _UNSET_CONTACT
    return f"discord-newsbot/{_package_version()} (+{contact})"


def _usable_contact(env: Mapping[str, str]) -> str | None:
    """`NEWSBOT_CONTACT`, trimmed, if it can safely go in an HTTP header; else None.

    Header values have to be plain printable ASCII. A stray newline from a
    shell-quoting slip, or an accented email domain, doesn't let anyone
    inject a header (the HTTP library refuses first), but it does make
    every single outbound request fail, which is a spectacularly quiet way
    to lose all of your sources at once. So anything outside printable
    ASCII is treated as unset, and `warn_if_contact_unset` says why.
    """
    raw = (env.get("NEWSBOT_CONTACT") or "").strip()
    if raw and all(" " <= ch <= "~" for ch in raw):
        return raw
    return None


def warn_if_contact_unset(env: Mapping[str, str] = os.environ) -> None:
    """Log one startup warning if `NEWSBOT_CONTACT` isn't set.

    Called once from the bot's `setup_hook` and once from the CLI's
    `main`, so both front doors say the same thing about a missing
    contact instead of one warning and one silent fallback.
    """
    if (env.get("NEWSBOT_CONTACT") or "").strip() and _usable_contact(env) is None:
        logger.warning(
            "NEWSBOT_CONTACT contains characters an HTTP header can't carry (a line "
            "break, a control character, or non-ASCII text); ignoring it. Use a plain "
            "URL or email address."
        )
    elif _usable_contact(env) is None:
        logger.warning(
            "NEWSBOT_CONTACT is not set; outbound requests will identify this bot "
            "with no way to reach its operator. Set it to a URL or an email address."
        )


# The literal header name lives here too, not just the value: every
# httpx client in this codebase builds its headers through
# `user_agent_headers` instead of spelling "User-Agent" out itself, which
# is what tests/test_useragent_tripwire.py actually enforces (a hardcoded
# *value* was the original bug, but a second hardcoded copy of the header
# name is just the same drift one step removed).
_HEADER_NAME = "User-Agent"


def user_agent_headers(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The one-entry headers dict every outbound `httpx.AsyncClient` in this codebase uses."""
    return {_HEADER_NAME: build_user_agent(env)}


__all__ = ["build_user_agent", "user_agent_headers", "warn_if_contact_unset"]
