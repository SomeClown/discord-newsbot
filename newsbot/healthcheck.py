"""`python -m newsbot.healthcheck`: the Docker `HEALTHCHECK` command.

There's no port to poll -- the bot only makes outgoing connections to
Discord's gateway -- so the healthcheck instead looks at a heartbeat file
(`bot/client.HEARTBEAT`) that the running process rewrites every 60
seconds, but only while it's actually connected and its scheduler is
running (see `NewsBot._heartbeat_job`). If that file goes stale, something
downstream of "the process is technically still running" has gone wrong:
a dead gateway connection, a crashed scheduler, a process wedged in a
deadlock. This script's whole job is turning "how stale is too stale"
into an exit code Docker understands.
"""

from __future__ import annotations

import time
from pathlib import Path

from newsbot.bot.client import HEARTBEAT

_STALE_AFTER_S = 180


def is_healthy(heartbeat_path: Path, *, now: float | None = None) -> bool:
    """True iff `heartbeat_path` exists and was written less than `_STALE_AFTER_S` ago."""
    try:
        mtime = heartbeat_path.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return (current - mtime) < _STALE_AFTER_S


def main() -> int:
    return 0 if is_healthy(HEARTBEAT) else 1


if __name__ == "__main__":
    raise SystemExit(main())
