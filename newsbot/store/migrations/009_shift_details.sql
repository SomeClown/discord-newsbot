-- SHiFT code details, and a record of every alert message so it can be edited
-- (design.md §16, v3.1.0).
--
-- Purely additive: two new tables, two indexes, one nullable column and one
-- app_state row. Nothing here renames, rebuilds or drops anything, so v3.0.5
-- (and v2.2.0 before it) can open a migrated file and never trip over it: they
-- only apply files newer than `user_version`, and neither names anything below.
-- Rolling back is a TAG change, plus the config, because v3.0.5 refuses a
-- `shift_table` source.
--
-- Like 006, this is its own file. Amending a migration that has already run
-- somewhere is how a "no such table" gets discovered at 3 a.m.
--
-- code_details: one row per code the bot has heard details about. It has no
-- foreign key to alerted_codes on purpose: a table row can name a code the bot
-- hasn't released yet, and the details shouldn't wait on that. Each of reward,
-- category and expiry carries a `*_source` rank ('official', 'table' or '' for
-- unknown), which is how "Gearbox outranks the fan site, field by field" is
-- stored, and a `*_from` source name, because "via mentalmars and Borderlands
-- (Bluesky)" needs names and not ranks. The precedence rules themselves live in
-- shift/details.py; SQL only keeps the merged row.
--
-- clock_start is set once, when the row is created, and never moves. The CHECKs
-- on length() count characters, and the read-time cap (at most 300 shown UTF-16
-- units) implies at most 300 raw ones, so they can't reject an honest row.
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

-- guild_alert_messages: what each server's alert message looked like when it
-- went out, so an edit can re-render it as it was. guild_code_posts has the
-- message id but not the channel or the header, and rebuilding those from
-- today's settings would guess wrong for any server that changed them since.
-- Alerts posted before 3.1.0 have no row here and are never edited (owner
-- decision E3). `rendered_at` is a version stamp, not a wall-clock time: the
-- largest details_changed_at among the message's codes as they were read for
-- rendering (the epoch if none had details). A code whose details_changed_at is
-- newer than that is waiting for an edit.
-- 'gone' means Discord says the message (or its channel) no longer exists.
CREATE TABLE guild_alert_messages (
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    channel_id INTEGER NOT NULL CHECK (channel_id > 0),
    header TEXT NOT NULL CHECK (length(header) BETWEEN 1 AND 200),
    golden_prefix INTEGER NOT NULL DEFAULT 0 CHECK (golden_prefix IN (0, 1)),
    posted_at TEXT NOT NULL,
    rendered_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'gone')),
    PRIMARY KEY (guild_id, message_id)
);

CREATE INDEX idx_guild_alert_messages_posted ON guild_alert_messages (posted_at);

-- The codes in a message are found through guild_code_posts, by (guild, message).
CREATE INDEX idx_guild_code_posts_message ON guild_code_posts (guild_id, message_id);

-- Set when an edit hits a permission error, so the server gets one notice and
-- then quiet; saving /newsbot shift clears it. v3.0.5 writes guild_shift with
-- named columns, so a new nullable one is invisible to it.
ALTER TABLE guild_shift ADD COLUMN edits_blocked_at TEXT;

-- The rollout marker: a code first seen at or after this moment keeps that first
-- sighting as its clock start; an older one starts fresh when it's first listed
-- (design.md §16, "the clock"). strftime writes the same shape Python's
-- isoformat() does, and '+' sorts before '.', so the two compare correctly as text.
INSERT INTO app_state (key, value)
VALUES ('shift_details_since', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
ON CONFLICT(key) DO NOTHING;
