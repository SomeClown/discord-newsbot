"""JSON-lines logging.

The bot runs in a container, and the container's stdout goes wherever
`docker compose logs` (or the Droplet's log driver) sends it. Grepping
plain-text log lines by hand is fine at 2 a.m. when something's on fire;
it's less fine when you're trying to graph source failures over a month.
So every line here is one JSON object, and whatever the caller passes
through ``extra=`` rides along as top-level keys, structured logging on
the cheap.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# The set of attributes a bare LogRecord already has. Anything else found
# on a record was added by the caller's `extra=` kwarg, and that's the
# stuff we want to surface as first-class JSON keys instead of burying it
# inside the message string.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__.keys()
)


class JsonFormatter(logging.Formatter):
    """Format a LogRecord as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Point the root logger at stdout, one JSON object per line.

    Safe to call more than once (tests do); it replaces the root logger's
    handlers rather than stacking new ones on top, so log lines don't
    start showing up twice.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # discord.py and httpx log request details at INFO/DEBUG, tokens
    # included in some cases. WARNING is plenty for a bot this small, and
    # it keeps auth headers out of the logs by construction.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
