-- newsbot: foreign-keys-off
-- Public app tables (v3.0, design.md §15).
--
-- The first line above is a switch for the runner in store/db.py, not a
-- comment for people: it turns foreign keys off around this script (SQLite
-- ignores that pragma inside a transaction, so the runner has to do it
-- outside one) and runs `PRAGMA foreign_key_check` before committing.
--
-- Most of this file is new tables, which is the easy kind of migration.
-- Two tables are not new, and I'd have loved to leave them alone:
--   * digests.run_date is UNIQUE, so two servers can't each have a digest
--     for the same day.
--   * stories.digest_id is NOT NULL with no ON DELETE, so a server's
--     digests could never be deleted while its stories pointed at them.
-- SQLite can't drop a constraint in place, so both get rebuilt using its
-- documented recipe: create the new table, copy the rows (ids preserved,
-- which keeps stories_fts's rowids valid), drop the old one, rename the
-- new one, recreate the indexes and triggers.
--
-- The rebuilt tables are column supersets, and that is what keeps a v2.2.0
-- process working against this database (rollback is a TAG change). v2.2
-- names its columns in every query, the new ones are nullable or defaulted,
-- and its inserts leave guild_id NULL. The one thing a NULL guild_id loses
-- is UNIQUE(guild_id, run_date), because SQLite treats NULLs as distinct;
-- the partial index on run_date below gives v2.2's own rows back the
-- one-digest-per-day guarantee they had before.
--
-- No backfill happens here: SQL can't know the friend's server id. The
-- Python import fills digests.guild_id and lounge_quotes_used.guild_id
-- when it writes the first guilds row, and `adopt_orphan_digests` mops up
-- after a rollback-then-forward.

CREATE TABLE guilds (
    guild_id INTEGER PRIMARY KEY CHECK (guild_id > 0),
    digest_time TEXT NOT NULL DEFAULT '09:00'
        CHECK (digest_time GLOB '[01][0-9]:[0-5][0-9]' OR digest_time GLOB '2[0-3]:[0-5][0-9]'),
    timezone TEXT NOT NULL DEFAULT 'UTC',
    admin_channel_id INTEGER CHECK (admin_channel_id IS NULL OR admin_channel_id > 0),
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'comped')),
    set_up INTEGER NOT NULL DEFAULT 0 CHECK (set_up IN (0, 1)),
    joined_at TEXT NOT NULL,
    imported_at TEXT,
    permission_problems TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE guild_games (
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    game_key TEXT NOT NULL,
    channel_id INTEGER NOT NULL CHECK (channel_id > 0),
    PRIMARY KEY (guild_id, game_key)
);

CREATE TRIGGER guild_games_limit BEFORE INSERT ON guild_games
WHEN (SELECT COUNT(*) FROM guild_games WHERE guild_id = NEW.guild_id) >= 10
BEGIN
    SELECT RAISE(ABORT, 'at most 10 games per guild');
END;

CREATE TABLE guild_shift (
    guild_id INTEGER PRIMARY KEY REFERENCES guilds (guild_id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    channel_id INTEGER CHECK (channel_id IS NULL OR channel_id > 0),
    -- A role id is a Discord snowflake: 17 to 20 digits, no leading zero.
    ping TEXT NOT NULL DEFAULT 'none'
        CHECK (ping IN ('none', 'everyone')
            OR (length(ping) BETWEEN 17 AND 20
                AND ping GLOB '[1-9]*' AND ping NOT GLOB '*[^0-9]*')),
    enabled_at TEXT,
    ping_day TEXT,
    ping_count INTEGER NOT NULL DEFAULT 0,
    CHECK (enabled = 0 OR channel_id IS NOT NULL)
);

CREATE TABLE guild_code_posts (
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES alerted_codes (code),
    -- queued: released, waiting its guild's turn; pending: claimed, send in
    -- flight; posted / failed: outcome; skipped: the guild stopped wanting it.
    status TEXT NOT NULL CHECK (status IN ('queued', 'pending', 'posted', 'failed', 'skipped')),
    message_id INTEGER,
    pinged INTEGER NOT NULL DEFAULT 0 CHECK (pinged IN (0, 1)),
    from_roundup INTEGER NOT NULL DEFAULT 0 CHECK (from_roundup IN (0, 1)),
    -- what detection knew at release time, for a delivery that happens later
    golden INTEGER NOT NULL DEFAULT 0 CHECK (golden IN (0, 1)),
    trusted INTEGER NOT NULL DEFAULT 1 CHECK (trusted IN (0, 1)),
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, code)
);

CREATE TABLE guild_lounge (
    guild_id INTEGER PRIMARY KEY REFERENCES guilds (guild_id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL CHECK (channel_id > 0),
    welcome_enabled INTEGER NOT NULL DEFAULT 0,
    welcome_message TEXT NOT NULL DEFAULT '',
    quote_enabled INTEGER NOT NULL DEFAULT 0,
    quote_time TEXT NOT NULL DEFAULT '08:00'
        CHECK (quote_time GLOB '[01][0-9]:[0-5][0-9]' OR quote_time GLOB '2[0-3]:[0-5][0-9]'),
    quote_sources TEXT NOT NULL DEFAULT '[]',
    last_quote_date TEXT
);

CREATE TABLE guild_notices (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000)
);

CREATE INDEX idx_guild_notices ON guild_notices (guild_id, created_at);

CREATE TABLE game_summaries (
    id INTEGER PRIMARY KEY,
    game_key TEXT NOT NULL,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'fallback')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    coverage_notes TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (game_key, run_date)
);

CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE INDEX idx_item_topics_topic ON item_topics (topic_key, item_id);

-- lounge_quotes_used keeps its (source_key, quote_hash) primary key, which
-- is fine while only one server has a lounge. NULL until the import fills it.
ALTER TABLE lounge_quotes_used ADD COLUMN guild_id INTEGER
    REFERENCES guilds (guild_id) ON DELETE CASCADE;

-- Free servers' /news reads item headlines, so items get the same kind of
-- external-content FTS index stories have (see 001): the index stores no
-- text of its own and the triggers keep it pointed at items.
CREATE VIRTUAL TABLE items_fts USING fts5(
    title,
    excerpt,
    content='items',
    content_rowid='id'
);

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

INSERT INTO items_fts (items_fts) VALUES ('rebuild');

-- Rebuild digests as a superset. Nullable guild_id so v2.2's inserts work.
CREATE TABLE digests_new (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER REFERENCES guilds (guild_id) ON DELETE CASCADE,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'partial', 'failed')),
    posted_message_ids TEXT NOT NULL DEFAULT '[]',
    posted_by_game TEXT NOT NULL DEFAULT '{}',
    window_start TEXT,
    window_end TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_notes TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (guild_id, run_date)
);

INSERT INTO digests_new (
    id, run_date, status, posted_message_ids, error_notes,
    input_tokens, output_tokens, created_at, updated_at
)
SELECT
    id, run_date, status, posted_message_ids, error_notes,
    input_tokens, output_tokens, created_at, updated_at
FROM digests;

DROP TABLE digests;
ALTER TABLE digests_new RENAME TO digests;

CREATE UNIQUE INDEX idx_digests_orphan_run_date ON digests (run_date) WHERE guild_id IS NULL;

-- Rebuild stories as a superset: digest_id is nullable and SET NULL on
-- delete, and summary_id links a comped story to its shared summary.
CREATE TABLE stories_new (
    id INTEGER PRIMARY KEY,
    topic_key TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('official', 'reported', 'rumor')),
    is_update_of INTEGER REFERENCES stories (id) ON DELETE SET NULL,
    digest_id INTEGER REFERENCES digests (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    summary_id INTEGER REFERENCES game_summaries (id) ON DELETE SET NULL
);

INSERT INTO stories_new (
    id, topic_key, headline, summary, label, is_update_of, digest_id, created_at
)
SELECT id, topic_key, headline, summary, label, is_update_of, digest_id, created_at
FROM stories;

DROP TABLE stories;
ALTER TABLE stories_new RENAME TO stories;

CREATE INDEX idx_stories_topic_created ON stories (topic_key, created_at);
CREATE INDEX idx_stories_created_at ON stories (created_at);

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
