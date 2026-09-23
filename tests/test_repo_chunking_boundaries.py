"""Boundary tests for the `_SQLITE_VARIABLE_CHUNK` batching in `existing_urls`.

test_repo_write.py already proves 1500 URLs don't blow SQLite's bound
parameter limit. This file pins the exact edges around the chunk size
itself (900): one chunk, exactly full, one over, and the empty-input case
that the chunking loop has to skip cleanly instead of running a `WHERE url
IN ()` with no placeholders.
"""

from contextlib import closing
from datetime import date

import pytest

from newsbot.store import repo
from newsbot.store.db import connect, migrate
from newsbot.store.models import StoredItem, Usage

CHUNK = repo._SQLITE_VARIABLE_CHUNK


@pytest.fixture
def conn(tmp_path):
    with closing(connect(tmp_path / "newsbot.db")) as c:
        migrate(c)
        yield c


def _seed_urls_at(conn, indices):
    digest_id = repo.claim_digest(conn, date(2026, 9, 23), force=False)
    items = [
        StoredItem(
            url=f"https://e/{i}",
            title="x",
            excerpt="x",
            source_name="src",
            trust="official",
            published_at=None,
            topics={},
        )
        for i in indices
    ]
    repo.save_run(conn, digest_id, items, [], "ok", [], None, Usage(0, 0))


def test_empty_input_returns_empty_set_without_querying(conn):
    assert repo.existing_urls(conn, []) == set()


def test_exactly_one_chunk_worth(conn):
    _seed_urls_at(conn, [0, CHUNK // 2])
    urls = [f"https://e/{i}" for i in range(CHUNK)]
    found = repo.existing_urls(conn, urls)
    assert found == {"https://e/0", f"https://e/{CHUNK // 2}"}


def test_one_more_than_a_chunk_spills_into_second_chunk(conn):
    seeded = [0, CHUNK - 1, CHUNK]  # last one only exists in the second chunk
    _seed_urls_at(conn, seeded)
    urls = [f"https://e/{i}" for i in range(CHUNK + 1)]
    found = repo.existing_urls(conn, urls)
    assert found == {f"https://e/{i}" for i in seeded}


def test_one_fewer_than_a_chunk(conn):
    _seed_urls_at(conn, [0])
    urls = [f"https://e/{i}" for i in range(CHUNK - 1)]
    found = repo.existing_urls(conn, urls)
    assert found == {"https://e/0"}


def test_duplicate_urls_in_input_do_not_duplicate_output(conn):
    _seed_urls_at(conn, [0])
    found = repo.existing_urls(conn, ["https://e/0", "https://e/0", "https://e/0"])
    assert found == {"https://e/0"}
