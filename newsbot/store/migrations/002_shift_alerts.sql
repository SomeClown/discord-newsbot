-- SHiFT code alerts (v1.2, design.md §12).
--
-- Two tables, both left alone by retention (purge_older_than only ever
-- touches items/stories): `alerted_codes` is the once-per-code-ever guard
-- that keeps this feature from re-pinging @everyone for a code it already
-- posted, and `alert_state` is a tiny key/value scratchpad for the handful
-- of cross-sweep facts (seeded marker, last sweep, today's ping count)
-- that don't deserve their own columns anywhere else.

CREATE TABLE alerted_codes (
    code TEXT PRIMARY KEY CHECK (length(code) = 29),
    first_seen_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    item_url TEXT NOT NULL,
    message_id INTEGER,
    pinged INTEGER NOT NULL DEFAULT 0 CHECK (pinged IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('seeded', 'too_old', 'pending', 'posted', 'failed'))
);

CREATE TABLE alert_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
