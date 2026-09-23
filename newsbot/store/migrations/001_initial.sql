-- Initial schema.
--
-- Three deliberate departures from the design doc's section 5, all
-- accepted by the owner as SPEC-DEV 1 and 2 (see docs/plans):
--   * items.topic_key doesn't exist here. An item can match more than one
--     topic ("2K" shows up in both a Borderlands post and, theoretically,
--     a crossover announcement), and a UNIQUE url column can't also hold a
--     one-to-many relationship. item_topics carries that instead.
--   * digests.status gains 'pending' and the table gains updated_at, so a
--     run can claim today's slot before it does anything slow (posting to
--     Discord), and a crash mid-run leaves evidence instead of a silent gap.
--   * stories_fts is an external-content FTS5 table kept in sync by
--     triggers, rather than duplicating headline/summary text into the
--     index itself.

CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    source_name TEXT NOT NULL,
    trust TEXT NOT NULL CHECK (trust IN ('official', 'press', 'community')),
    published_at TEXT,
    collected_at TEXT NOT NULL
);

CREATE INDEX idx_items_collected_at ON items (collected_at);

CREATE TABLE item_topics (
    item_id INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    topic_key TEXT NOT NULL,
    uncertain INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, topic_key)
);

CREATE TABLE digests (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'partial', 'failed')),
    posted_message_ids TEXT NOT NULL DEFAULT '[]',
    error_notes TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE stories (
    id INTEGER PRIMARY KEY,
    topic_key TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('official', 'reported', 'rumor')),
    is_update_of INTEGER REFERENCES stories (id) ON DELETE SET NULL,
    digest_id INTEGER NOT NULL REFERENCES digests (id),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_stories_topic_created ON stories (topic_key, created_at);
CREATE INDEX idx_stories_created_at ON stories (created_at);

CREATE TABLE story_items (
    story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    PRIMARY KEY (story_id, item_id)
);

CREATE TABLE source_health (
    source_name TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

-- External-content FTS5 index over stories.headline/summary. "External
-- content" means the index stores no text of its own, just a search
-- structure pointing back at stories by rowid; the triggers below are what
-- keep the two in sync, since SQLite won't do it for us.
CREATE VIRTUAL TABLE stories_fts USING fts5(
    headline,
    summary,
    content='stories',
    content_rowid='id'
);

CREATE TRIGGER stories_ai AFTER INSERT ON stories BEGIN
    INSERT INTO stories_fts (rowid, headline, summary)
    VALUES (new.id, new.headline, new.summary);
END;

CREATE TRIGGER stories_ad AFTER DELETE ON stories BEGIN
    INSERT INTO stories_fts (stories_fts, rowid, headline, summary)
    VALUES ('delete', old.id, old.headline, old.summary);
END;

CREATE TRIGGER stories_au AFTER UPDATE ON stories BEGIN
    INSERT INTO stories_fts (stories_fts, rowid, headline, summary)
    VALUES ('delete', old.id, old.headline, old.summary);
    INSERT INTO stories_fts (rowid, headline, summary)
    VALUES (new.id, new.headline, new.summary);
END;
