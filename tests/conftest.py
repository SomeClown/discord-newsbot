"""Shared fixtures: a populated v4 (v2.2.0) database, and the same one migrated to v5.

Migration 005 rebuilds two tables that hold the friend's real history, so
"it works on an empty database" proves very little. These fixtures build a
database the way v2.2.0 would have: the real migrations 001 to 004 (copied
into a scratch directory so the real runner applies exactly those and stops),
then rows in every table a v2.2 process writes. `v22_db` is that database
after migration 005.
"""

from __future__ import annotations

import asyncio
import ipaddress
import shutil
import socket
from contextlib import closing
from pathlib import Path

import pytest

from newsbot.store import db
from newsbot.store.db import connect, migrate

# The cutover tests' fixtures (a whole v2.2 morning, rebuilt in a temp directory) live in
# their own module; this is how pytest finds them without every file importing a fixture.
pytest_plugins = ("cutover_world",)

# What every hostname resolves to during a test (a public address, so the redirect guard sees a
# perfectly ordinary host). Tests that care about a particular answer patch their own.
PUBLIC_TEST_ADDRESS = "93.184.215.14"


class NetworkBlockedError(RuntimeError):
    """A test tried to open a real connection. It should have used a fake."""


def _fake_addrinfo(host, port, family=0, type=0, proto=0, flags=0):
    kind = type or socket.SOCK_STREAM
    return [
        (socket.AF_INET, kind, proto or socket.IPPROTO_TCP, "", (PUBLIC_TEST_ADDRESS, port or 0))
    ]


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """No real DNS and no real outbound connections from a test (`real_network` marker opts out).

    The suite used to make about 41 real DNS lookups (the RSS redirect check resolves hosts even
    under `MockTransport`) and at least one real connection to Steam, and an occasional stalled
    resolver looked like a hung test run. Now every lookup answers with a public test address,
    and connecting to anything but loopback or a unix socket raises on the spot.
    """
    if request.node.get_closest_marker("real_network"):
        return

    async def fake_loop_getaddrinfo(self, host, port, **kwargs):
        return _fake_addrinfo(host, port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_loop_getaddrinfo)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(address) -> None:
        if isinstance(address, tuple):
            host = str(address[0])
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                raise NetworkBlockedError(
                    f"test tried to connect to {host}; fake the transport instead"
                )

    def guarded_connect(self, address):
        check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


MIGRATIONS_DIR = Path(__file__).parent.parent / "newsbot" / "store" / "migrations"

# Real-looking SHiFT codes (29 characters: five groups of five and four dashes).
CODES = {
    "seeded": "AAAAA-AAAAA-AAAAA-AAAAA-AAAA1",
    "too_old": "BBBBB-BBBBB-BBBBB-BBBBB-BBBB2",
    "pending": "CCCCC-CCCCC-CCCCC-CCCCC-CCCC3",
    "posted": "DDDDD-DDDDD-DDDDD-DDDDD-DDDD4",
    "failed": "EEEEE-EEEEE-EEEEE-EEEEE-EEEE5",
    "roundup": "FFFFF-FFFFF-FFFFF-FFFFF-FFFF6",
}


def _migrations_up_to(target: Path, version: int) -> Path:
    """Copy migrations `001..version` into `target` and return it."""
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if int(path.name.split("_", 1)[0]) <= version:
            shutil.copy(path, target / path.name)
    return target


@pytest.fixture
def migrations_up_to():
    """The copy helper above, for tests that build their own migration directories."""
    return _migrations_up_to


def populate_v4(conn) -> None:
    """Insert one realistic slice of a v2.2.0 database (three days of digests and so on)."""
    with conn:
        for i, (url, title, trust) in enumerate(
            [
                ("https://example.com/bl4-patch", "Borderlands 4 patch notes 1.2", "official"),
                ("https://example.com/palworld-raid", "Palworld raid boss guide", "press"),
                ("https://example.com/d4-season", "Diablo IV season starts", "official"),
                ("https://example.com/bl4-rumor", "Borderlands 4 DLC leak", "community"),
            ],
            start=1,
        ):
            conn.execute(
                "INSERT INTO items (id, url, title, excerpt, source_name, trust, "
                "published_at, collected_at) VALUES (?, ?, ?, ?, 'Feed', ?, ?, ?)",
                (
                    i * 10,
                    url,
                    title,
                    f"Excerpt about {title.lower()}",
                    trust,
                    "2026-09-27T12:00:00+00:00",
                    f"2026-09-2{6 + i % 3}T16:00:00+00:00",
                ),
            )
        conn.executemany(
            "INSERT INTO item_topics (item_id, topic_key, uncertain) VALUES (?, ?, ?)",
            [
                (10, "borderlands4", 0),
                (20, "palworld", 0),
                (30, "diablo4", 0),
                (40, "borderlands4", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO digests (id, run_date, status, posted_message_ids, error_notes, "
            "input_tokens, output_tokens, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (7, "2026-09-27", "ok", "[111, 112, 113]", None, 5000, 900, "t27", "t27"),
                (8, "2026-09-28", "partial", "[121]", "one game failed", 4000, 700, "t28", "t28"),
                (9, "2026-09-29", "failed", "[]", "discord down", 0, 0, "t29", "t29"),
                (12, "2026-09-30", "ok", "[131, 132]", None, 5200, 950, "t30", "t30"),
            ],
        )
        conn.executemany(
            "INSERT INTO stories (id, topic_key, headline, summary, label, is_update_of, "
            "digest_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    100,
                    "borderlands4",
                    "Patch 1.2 lands",
                    "Fixes the vault crash.",
                    "official",
                    None,
                    7,
                    "2026-09-27T16:00:00+00:00",
                ),
                (
                    101,
                    "palworld",
                    "Raid bosses get harder",
                    "Pals will hurt more.",
                    "reported",
                    None,
                    7,
                    "2026-09-27T16:00:00+00:00",
                ),
                (
                    102,
                    "borderlands4",
                    "Patch 1.2 hotfix",
                    "A hotfix for the vault fix.",
                    "official",
                    100,
                    8,
                    "2026-09-28T16:00:00+00:00",
                ),
                (
                    103,
                    "diablo4",
                    "Season begins",
                    "New season, new grind.",
                    "official",
                    None,
                    12,
                    "2026-09-30T16:00:00+00:00",
                ),
                (
                    104,
                    "borderlands4",
                    "DLC leak claims a lot",
                    "Unverified leak.",
                    "rumor",
                    None,
                    12,
                    "2026-09-30T16:00:00+00:00",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO story_items (story_id, item_id) VALUES (?, ?)",
            [(100, 10), (101, 20), (102, 10), (103, 30), (104, 40), (104, 10)],
        )
        conn.execute(
            "INSERT INTO source_health (source_name, last_success_at, last_error_at, "
            "last_error, consecutive_failures) VALUES ('Feed', "
            "'2026-09-30T15:00:00+00:00', '2026-09-30T16:00:00+00:00', 'boom', 2)"
        )
        for status, code in CODES.items():
            conn.execute(
                "INSERT INTO alerted_codes (code, first_seen_at, source_name, item_url, "
                "message_id, pinged, status, from_roundup) VALUES (?, ?, 'Feed', "
                "'https://example.com/bl4-patch', ?, ?, ?, ?)",
                (
                    code,
                    "2026-09-29T10:00:00+00:00",
                    999 if status == "posted" else None,
                    int(status == "posted"),
                    status,
                    int(status == "roundup"),
                ),
            )
        conn.executemany(
            "INSERT INTO alert_state (key, value) VALUES (?, ?)",
            [
                ("seeded_at", "2026-09-01T00:00:00+00:00"),
                ("ping_day", "2026-09-30"),
                ("ping_count", "2"),
                ("last_sweep_summary", "17/19 sources ok"),
            ],
        )
        conn.executemany(
            "INSERT INTO lounge_quotes_used (source_key, quote_hash, used_at) VALUES (?, ?, ?)",
            [
                ("wikiquote:Oscar Wilde", "a" * 64, "2026-09-29T15:00:00+00:00"),
                ("wikiquote:Mark Twain", "b" * 64, "2026-09-30T15:00:00+00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO lounge_state (key, value) VALUES ('last_quote_date', '2026-09-30')"
        )


@pytest.fixture
def codes() -> dict[str, str]:
    """The SHiFT codes `populate_v4` inserts, keyed by the status each one has."""
    return dict(CODES)


@pytest.fixture
def v4_db(tmp_path, monkeypatch) -> Path:
    """Path to a populated database at `user_version` 4, built by the real migrations."""
    v4_dir = _migrations_up_to(tmp_path / "migrations_v4", 4)
    path = tmp_path / "v4.db"
    with monkeypatch.context() as m:
        m.setattr(db, "_MIGRATIONS_DIR", v4_dir)
        with closing(connect(path)) as conn:
            assert migrate(conn) == 4
            populate_v4(conn)
    return path


@pytest.fixture
def v22_db(v4_db) -> Path:
    """The populated v4 database after migrations 005 to 007 (prod once v3 is live)."""
    with closing(connect(v4_db)) as conn:
        assert migrate(conn) == 8
    return v4_db


@pytest.fixture
def v3_db(tmp_path):
    """An empty, fully migrated database at a fresh path (for the v3 command tests)."""
    path = str(tmp_path / "newsbot.db")
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def v3_cfg(monkeypatch):
    """The v3 fixture config: catalog of borderlands4, palworld and rust; home guild 2000...01."""
    from v3_fakes import load_v3_config

    return load_v3_config(monkeypatch)
