-- SHiFT "confirmed by a second source" follow-ups (design.md §15, plan D14).
--
-- A community-only code posts unpinged. If a second, independent source sees
-- it within 24 hours, each server that has pinging on gets one short follow-up
-- that carries the ping. Three additions make that possible, and all of them
-- are invisible to v2.2.0 (rollback is a TAG change): v2.2 has never heard of
-- either new table, and the new column lives on a v3-only table.
--
-- This is its own file, and not an edit to 005, because 005 may already have
-- run on a development database. Amending a migration that has run somewhere
-- is how a "no such table" gets discovered at 3 a.m.
--
-- code_sightings: one row per (code, source name) with when we first saw it
-- there. It deliberately has no foreign key to alerted_codes: sightings are
-- written before the code's own row (a crash in between should cost nothing).
-- `trusted` is whether that source's trust was in `ping_trust` when seen;
-- `roundup` is true only while every sighting by that source came from a
-- roundup post, which never counts as confirmation.
CREATE TABLE code_sightings (
    code TEXT NOT NULL,
    source_name TEXT NOT NULL,
    trusted INTEGER NOT NULL DEFAULT 0 CHECK (trusted IN (0, 1)),
    roundup INTEGER NOT NULL DEFAULT 0 CHECK (roundup IN (0, 1)),
    seen_at TEXT NOT NULL,
    PRIMARY KEY (code, source_name)
);

-- Stamped when a server's original post went out unpinged *because the whole
-- batch was untrusted* (and the server had pinging on): the only kind of post
-- a confirmation is allowed to follow up. Old rows read 0, so nothing already
-- posted ever gets a follow-up out of nowhere.
ALTER TABLE guild_code_posts
    ADD COLUMN followup_ok INTEGER NOT NULL DEFAULT 0 CHECK (followup_ok IN (0, 1));

-- One row per (server, code), ever: the primary key is what makes "at most one
-- follow-up" true across restarts and overlapping passes. Same life cycle as
-- guild_code_posts: queued -> pending -> posted | failed, or skipped (the
-- server stopped wanting it, or its ping budget was spent).
CREATE TABLE guild_code_followups (
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES alerted_codes (code),
    status TEXT NOT NULL CHECK (status IN ('queued', 'pending', 'posted', 'failed', 'skipped')),
    message_id INTEGER,
    pinged INTEGER NOT NULL DEFAULT 0 CHECK (pinged IN (0, 1)),
    queued_at TEXT NOT NULL,
    claimed_at TEXT,
    PRIMARY KEY (guild_id, code)
);
