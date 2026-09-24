"""`python -m newsbot`: load config, migrate the store, and connect to Discord.

This is the container's actual entrypoint. Everything it does before
`bot.run()` is deliberately the same order as the CLI pipeline runner
(`pipeline/run.py`'s `main`): configure logging first so nothing that
follows gets lost, load and validate the config and secrets before
touching a database or a socket, then migrate and check FTS5 so a broken
store fails at startup instead of at 9:04 a.m. when the first digest tries
to post. A bad config or a missing secret exits 2 with a readable message;
`docker compose`'s restart loop will just keep restarting into the same
clear error, which is annoying but at least legible from `docker compose
logs`.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

from newsbot.bot.client import NewsBot
from newsbot.config import ConfigError, load_config, load_secrets
from newsbot.logging_setup import configure_logging
from newsbot.store.db import assert_fts5, connect, migrate


def main() -> int:
    configure_logging()

    config_path = os.environ.get("NEWSBOT_CONFIG")
    if not config_path:
        print("newsbot: NEWSBOT_CONFIG is required", file=sys.stderr)
        return 2

    try:
        cfg = load_config(config_path)
        secrets = load_secrets()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    db_path = os.environ.get("NEWSBOT_DB", "./data/newsbot.db")
    db_parent = Path(db_path).parent
    if str(db_parent) not in ("", "."):
        db_parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        migrate(conn)
        assert_fts5(conn)

    bot = NewsBot(cfg, secrets, db_path)
    # log_handler=None: we've already pointed the root logger at stdout
    # with our own JSON formatter (configure_logging, above); letting
    # discord.py's run() install its own handler on top would mean every
    # gateway log line prints twice, in two different formats.
    bot.run(secrets.discord_token.get_secret_value(), log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
