-- SHiFT roundup marker (v2.0, design.md §13).
--
-- §13's "roundup-posted codes use `posted`" can't coexist with `/shift
-- codes` wanting to show a "from a roundup" marker on those same rows --
-- nothing else on `alerted_codes` distinguishes a roundup post from a
-- normal one once both share the `posted` status. This column is the fix:
-- additive, backfilled from the `roundup` status that already existed
-- (§12/A2), so a v1.3.0 database upgrades in place and a v1.3.0 process
-- reading a v2.0 database just never looks at the new column.

ALTER TABLE alerted_codes ADD COLUMN from_roundup INTEGER NOT NULL DEFAULT 0
    CHECK (from_roundup IN (0, 1));

UPDATE alerted_codes SET from_roundup = 1 WHERE status = 'roundup';
