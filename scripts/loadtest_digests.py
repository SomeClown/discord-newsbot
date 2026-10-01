"""Run a few hundred servers' digests against a pretend Discord, in real time, and time it.

`tests/test_load_many_guilds.py` does this on a virtual clock in a few seconds.
This is the same experiment with the clock left alone: real sleeps, the real
one-second pace between servers, the real retry backoff after a 429. It's the
slow, honest version, and the one to run when you want a number you could say
out loud. Three hundred servers is a bit over five minutes at the default pace;
`--pace-s` shrinks that if you'd like your coffee to still be warm.

Only Discord is fake (`tests/load_world.py`: about 50 sends a second overall, 5
per 5 seconds per channel, plus a few random 429s so the retry gets some
exercise). The database is real SQLite and `run_due_guilds` is the real tick.

It never touches anything you own. The database is a scratch file in a fresh
temp directory the script creates itself and deletes on the way out. `--db`
exists for people who want to look at the file afterwards, and it refuses a
path inside `data/` or any file that's already there: it only ever writes a
file it created. The point of a load test is to be wrong somewhere
that doesn't matter.

    python scripts/loadtest_digests.py --guilds 300 --games-max 10 --seed 1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import shutil
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from load_world import (  # noqa: E402 (needs the path line above)
    DigestWorld,
    FakeDiscord,
    TickDriver,
    build_digest_world,
    load_load_config,
)

from newsbot.store.db import connect  # noqa: E402

ZONES = ("America/Los_Angeles", "America/Vancouver", "America/Phoenix", "America/Whitehorse")


def check_db_path(path: Path) -> Path:
    """Return `path` if it's safe to create there; otherwise exit with a message.

    Safe means: not inside the repo's `data/`, and not a file (or anything) that already
    exists. A script that can only create what it's about to write can't clobber what
    you meant to keep.
    """
    resolved = path.expanduser().resolve()
    data_dir = (ROOT / "data").resolve()
    if resolved == data_dir or data_dir in resolved.parents:
        raise SystemExit(f"refusing to use {resolved}: that's inside data/")
    if resolved.exists() or Path(f"{resolved}-wal").exists() or Path(f"{resolved}-shm").exists():
        raise SystemExit(f"refusing to use {resolved}: it already exists and I didn't create it")
    return resolved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--guilds", type=int, default=300, help="servers due at once (300)")
    parser.add_argument("--games-max", type=int, default=10, help="most games per server (10)")
    parser.add_argument("--seed", type=int, default=1, help="seed for game picks and 429s (1)")
    parser.add_argument("--pace-s", type=float, default=1.0, help="pause between servers (1.0)")
    parser.add_argument("--latency-s", type=float, default=0.05, help="fake send latency (0.05)")
    parser.add_argument("--p429", type=float, default=0.03, help="chance a send gets a 429 (0.03)")
    parser.add_argument("--db", type=Path, default=None, help="keep the scratch database here")
    args = parser.parse_args(argv)
    if not 1 <= args.games_max <= 10:
        parser.error("--games-max must be 1 to 10 (the per-server limit)")
    if args.guilds < 1:
        parser.error("--guilds must be at least 1")
    return args


def done_count(db_path: str) -> int:
    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM digests WHERE status IN ('ok', 'partial')"
        ).fetchone()[0]


async def run(world: DigestWorld, pace_s: float) -> TickDriver:
    deps = world.deps(pace_s=pace_s)
    driver = TickDriver(deps, overlap=False, clock=time.monotonic)
    expected = len(world.plan)
    await driver.run(lambda: done_count(world.db_path) >= expected, every_s=60.0, max_ticks=30)
    return driver


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.db is not None:
        db_path = check_db_path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        scratch = None
    else:
        scratch = Path(tempfile.mkdtemp(prefix="newsbot-loadtest-"))
        db_path = scratch / "loadtest.db"
    os.environ.setdefault("BRAVE_API_KEY", "loadtest")

    try:
        now = datetime.now(UTC)
        # Everyone's digest time is local midnight, which has always already passed: all of
        # them are due on the first tick, the way 09:00 is for a real crowd.
        plan = build_digest_world(
            str(db_path),
            guilds=args.guilds,
            games_max=args.games_max,
            seed=args.seed,
            zones=ZONES,
            due=now,
            digest_time="00:00",
            now=now - timedelta(days=2),
        )
        discord_ = FakeDiscord(
            clock=time.monotonic,
            sleep=asyncio.sleep,
            rng=random.Random(args.seed),  # noqa: S311 (429 dice, not a secret)
            latency_s=args.latency_s,
            p429=args.p429,
        )
        world = DigestWorld(
            cfg=load_load_config(),
            db_path=str(db_path),
            plan=plan,
            discord=discord_,
            now=lambda: datetime.now(UTC),
            sleep=asyncio.sleep,
        )
        started = time.monotonic()
        driver = asyncio.run(run(world, args.pace_s))
        wall = time.monotonic() - started
        finished = done_count(str(db_path))
        sends = sum(len(v) for v in plan.values())
        busy = [
            end - begin
            for (begin, end), tick in zip(driver.spans, driver.ticks, strict=True)
            if tick
        ]
        print(f"guilds: {args.guilds} (games up to {args.games_max}, seed {args.seed})")
        print(f"digests ok or partial: {finished} of {args.guilds}")
        print(f"messages landed: {discord_.total_landed} of {sends} expected")
        print(f"send attempts: {discord_.attempts}")
        print(f"429s: {discord_.injected_429s} injected, {discord_.natural_429s} from the limits")
        print(f"pace between servers: {args.pace_s}s; fake send latency: {args.latency_s}s")
        print(f"wall time: {wall:.1f}s ({wall / 60:.1f} min)")
        if busy:
            print(f"busiest tick: {max(busy):.1f}s; per server: {max(busy) / args.guilds:.2f}s")
        print(f"notices to servers: {len(world.notices)}")
        return 0 if finished == args.guilds and discord_.total_landed == sends else 1
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
