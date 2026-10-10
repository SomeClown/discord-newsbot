-- Migration 009 as committed in fb3dc40 (an earlier draft of v3.1.0), comments stripped.
-- Used only by tests/test_qa2_stamps_migration.py to build a stale version 9 database.
CREATE TABLE code_details (
    code TEXT PRIMARY KEY CHECK (length(code) = 29),
    reward TEXT NOT NULL DEFAULT '' CHECK (length(reward) <= 300),
    reward_source TEXT NOT NULL DEFAULT '' CHECK (reward_source IN ('', 'official', 'table')),
    reward_from TEXT NOT NULL DEFAULT '',
    category TEXT CHECK (category IS NULL OR category IN ('golden', 'cosmetic', 'permanent', 'other')),
    category_source TEXT NOT NULL DEFAULT '' CHECK (category_source IN ('', 'official', 'table')),
    category_from TEXT NOT NULL DEFAULT '',
    expires_on TEXT CHECK (expires_on IS NULL OR expires_on GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    expires_source TEXT NOT NULL DEFAULT '' CHECK (expires_source IN ('', 'official', 'table')),
    expires_from TEXT NOT NULL DEFAULT '',
    added_on TEXT CHECK (added_on IS NULL OR added_on GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    origin TEXT NOT NULL DEFAULT '' CHECK (length(origin) <= 300),
    clock_start TEXT NOT NULL,
    first_listed_at TEXT,
    last_listed_at TEXT,
    updated_at TEXT NOT NULL,
    details_changed_at TEXT
);
CREATE TABLE guild_alert_messages (
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    channel_id INTEGER NOT NULL CHECK (channel_id > 0),
    header TEXT NOT NULL CHECK (length(header) BETWEEN 1 AND 200),
    golden_prefix INTEGER NOT NULL DEFAULT 0 CHECK (golden_prefix IN (0, 1)),
    posted_at TEXT NOT NULL,
    rendered_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'gone')),
    edit_tried_at TEXT,
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX idx_guild_alert_messages_posted ON guild_alert_messages (posted_at);
CREATE INDEX idx_guild_code_posts_message ON guild_code_posts (guild_id, message_id);
ALTER TABLE guild_shift ADD COLUMN edits_blocked_at TEXT;
INSERT INTO app_state (key, value)
VALUES ('shift_details_since', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
ON CONFLICT(key) DO NOTHING;
