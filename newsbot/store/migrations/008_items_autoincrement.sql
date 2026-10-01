-- newsbot: foreign-keys-off
-- Item ids that never come back (design.md §15, the task 16 qa round 2).
--
-- The item watermarks (007) are item ids: a digest says "I covered everything up
-- to id N" and the next one starts after N. That only works if an id, once handed
-- out, is never handed out again. `items.id` was a bare INTEGER PRIMARY KEY, which
-- SQLite numbers as max(id) + 1, so a purge that emptied the table (collection
-- stalled for the whole retention period; rare, but silent when it happens) made
-- the numbering start from 1 again, and a burst bigger than the old mark hid
-- behind it: new items numbered 1..mark looked like news the digest had already
-- shown. AUTOINCREMENT keeps the high-water mark in sqlite_sequence, so ids only
-- ever go up.
--
-- AUTOINCREMENT can't be added to an existing table, so this is SQLite's
-- documented rebuild (the header on line one is the runner's switch for it, see
-- 005 and store/db.py). What has to survive it:
--   * the ids themselves, copied as they are (item_topics and story_items point
--     at them; with foreign keys off the DROP doesn't cascade into either);
--   * the collected_at index and the three triggers that keep items_fts in step
--     (they die with the old table and are made again below);
--   * items_fts itself, an external-content index keyed on rowid. The rows keep
--     their ids, so the index stays right; the rebuild at the end is insurance
--     that costs a few thousand rows' worth of work.
-- The sequence is also seeded with the highest mark any digest or summary ever
-- recorded, so a store that was already emptied before this ran doesn't restart
-- below a mark it has handed out.
--
-- v2.2.0 after a rollback: its INSERTs name their columns and leave id alone, which
-- AUTOINCREMENT numbers the same way, and its purge is a plain DELETE
-- (tests/test_migration_005_v22_compat.py runs v2.2's SQL against this).
CREATE TABLE items_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    source_name TEXT NOT NULL,
    trust TEXT NOT NULL CHECK (trust IN ('official', 'press', 'community')),
    published_at TEXT,
    collected_at TEXT NOT NULL
);

INSERT INTO items_new (id, url, title, excerpt, source_name, trust, published_at, collected_at)
SELECT id, url, title, excerpt, source_name, trust, published_at, collected_at
FROM items ORDER BY id;

DROP TABLE items;
ALTER TABLE items_new RENAME TO items;

CREATE INDEX idx_items_collected_at ON items (collected_at);

CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (rowid, title, excerpt)
    VALUES (new.id, new.title, new.excerpt);
END;

CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, excerpt)
    VALUES ('delete', old.id, old.title, old.excerpt);
END;

CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, excerpt)
    VALUES ('delete', old.id, old.title, old.excerpt);
    INSERT INTO items_fts (rowid, title, excerpt)
    VALUES (new.id, new.title, new.excerpt);
END;

-- sqlite_sequence has no unique key on name, so replace the row by hand.
DELETE FROM sqlite_sequence WHERE name = 'items';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'items', MAX(
    COALESCE((SELECT MAX(id) FROM items), 0),
    COALESCE((SELECT MAX(items_upto) FROM digests), 0),
    COALESCE((SELECT MAX(items_upto) FROM game_summaries), 0)
);

INSERT INTO items_fts (items_fts) VALUES ('rebuild');
