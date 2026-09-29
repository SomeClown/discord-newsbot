-- Lounge daily quote (v2.2, design.md §14).
--
-- Two tables, both purely additive and both left alone by retention
-- (purge_older_than only ever touches items/stories). `lounge_quotes_used`
-- is the per-source no-repeat deck: one row per quote already posted, keyed
-- by the source's own identity (the configured Wikiquote title, resolved
-- file path or URL, not its position in the list) plus a sha256 of the
-- quote's normalized text. Keying on identity means reordering or editing
-- other sources never disturbs this one's deck; keying on text means edits
-- to a list need no bookkeeping. `lounge_state` is a tiny key/value
-- scratchpad, same idea as `alert_state`, holding `last_quote_date` for the
-- once-a-day guard. A v2.1.1 process opening this database never reads
-- either table, so rolling back needs no restore.

CREATE TABLE lounge_quotes_used (
    source_key TEXT NOT NULL CHECK (length(source_key) BETWEEN 1 AND 4096),
    quote_hash TEXT NOT NULL CHECK (length(quote_hash) = 64),
    used_at TEXT NOT NULL,
    PRIMARY KEY (source_key, quote_hash)
);

CREATE TABLE lounge_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
