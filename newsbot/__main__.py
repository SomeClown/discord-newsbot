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

The order after that is not negotiable, and it's the part to read before
changing anything: migrate, import the v2 setup if this is the first v3 start,
adopt any digest rows a rolled-back v2.2 left without a server, re-sync the
lounge block from config, read the lounge rows, build the gateway intents from
those rows, and only then construct the bot. The intents are fixed when the
client is constructed, so a lounge row that changes after that point needs a
restart to change them, and building them from anything but the final rows is
how you get a welcome that never fires.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from contextlib import closing
from pathlib import Path

import discord

from newsbot.bot.client import NewsBot, build_intents_for_lounges
from newsbot.config import AppConfig, ConfigError, load_config, load_secrets
from newsbot.guilds.importer import ImportFailedError, ensure_imported, resync_lounge_from_config
from newsbot.logging_setup import configure_logging
from newsbot.store import repo
from newsbot.store.db import assert_fts5, connect, migrate

logger = logging.getLogger(__name__)

# How long to sit on a "the intent switch is off" failure before exiting.
# Discord resets a bot's token after about 1,000 logins in 24 hours, and
# `restart: unless-stopped` with Docker's backoff (roughly one start a minute
# once it settles) could plausibly get there in under a day. I haven't
# confirmed whether a login the gateway refuses with 4014 counts toward that,
# and I'd rather not find out by having the token reset at 3 a.m. So we wait
# before exiting, which turns the crash loop into a slow drip. Module level so
# a test can set it to zero instead of sleeping for ten real minutes.
INTENT_EXIT_DELAY_S = 600

# One line, because a restart loop's log is easier to read that way. Since the
# public app (design.md §15) the welcome switch lives in a `guild_lounge` row,
# which the `lounge:` block in config.yaml still feeds until someone deletes it.
PRIVILEGED_INTENTS_MESSAGE = (
    "newsbot: a lounge welcome is enabled (guild_lounge / lounge.welcome.enabled) "
    "and needs the Server Members intent. Turn it on in the Discord Developer "
    "Portal (Bot page, Privileged Gateway Intents), or turn the welcome off "
    "(lounge.welcome.enabled: false)."
)


def _wait_before_exit() -> None:
    """Sleep `INTENT_EXIT_DELAY_S`, but wake straight away on SIGTERM or SIGINT.

    A bare `time.sleep` isn't good enough. Once `bot.run` has returned,
    discord.py's handlers are gone, and the container's main process is PID 1,
    which the kernel never delivers a signal to unless the process installed
    a handler for it. So `docker stop` would get ten seconds of silence and
    then a SIGKILL. Handlers that set an event make the wait end when asked.
    """
    stop = threading.Event()

    def wake(signum: int, frame: object) -> None:
        stop.set()

    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.signal(sig, wake) for sig in signals}
    try:
        stop.wait(INTENT_EXIT_DELAY_S)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _adopt_orphan_digests(db_path: str) -> None:
    """Give digest rows a rolled-back v2.2 wrote (no server on them) to the imported server.

    This is what closes the rollback-then-roll-forward double post: v2.2 inserts
    today's row with no server, and without this v3 wouldn't see today's digest
    as already posted. It reads the imported server from the database, not from
    `config.yaml`, so it keeps working after the old keys are deleted. A failure
    is logged and the bot starts anyway; `UPDATE OR IGNORE` has already made the
    statement itself hard to hurt.
    """
    try:
        with closing(connect(db_path)) as conn:
            imported = [g.guild_id for g in repo.list_guilds(conn) if g.imported_at is not None]
            adopted = sum(repo.adopt_orphan_digests(conn, guild_id) for guild_id in imported)
        if adopted:
            logger.info("adopted %d digest row(s) written without a server", adopted)
    except Exception:
        logger.exception("couldn't adopt orphan digest rows; starting anyway")


def _resync_lounge(db_path: str, cfg: AppConfig) -> None:
    """D5: re-apply a `lounge:` block still in config.yaml to the imported server's row.

    Failure is logged, not fatal: the row that's already in the database is a
    working lounge, and a re-sync that couldn't run is not a reason to stay down.
    """
    try:
        resync_lounge_from_config(db_path, cfg)
    except Exception:
        logger.exception("couldn't re-sync the lounge from config; starting anyway")


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

    # A half import would be worse than none, so a failure here is fatal.
    try:
        import_report = ensure_imported(db_path, cfg)
    except ImportFailedError as exc:
        print(f"newsbot: {exc}", file=sys.stderr)
        return 2

    _adopt_orphan_digests(db_path)
    _resync_lounge(db_path, cfg)

    with closing(connect(db_path)) as conn:
        lounges = repo.list_lounges(conn)
    intents = build_intents_for_lounges(lounges)
    bot = NewsBot(
        cfg, secrets, db_path, lounges=lounges, intents=intents, import_report=import_report
    )
    # log_handler=None: we've already pointed the root logger at stdout
    # with our own JSON formatter (configure_logging, above); letting
    # discord.py's run() install its own handler on top would mean every
    # gateway log line prints twice, in two different formats.
    try:
        bot.run(secrets.discord_token.get_secret_value(), log_handler=None)
    except discord.PrivilegedIntentsRequired:
        # Welcomes ask for the Server Members intent; Discord closes the
        # connection if the portal switch is off. Say which switch, and which
        # config key to flip instead, rather than dumping a traceback.
        print(PRIVILEGED_INTENTS_MESSAGE, file=sys.stderr)
        logger.warning(
            "Waiting %d seconds before exiting, so a restart loop can't spend the bot's logins",
            INTENT_EXIT_DELAY_S,
        )
        _wait_before_exit()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
