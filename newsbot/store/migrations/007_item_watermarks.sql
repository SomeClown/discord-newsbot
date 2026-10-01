-- Item watermarks: how a digest or a summary says which stored items it covers
-- (design.md §15, the task 16 qa review).
--
-- Windows used to be time ranges over `items.collected_at`, and `collected_at`
-- is stamped when a collection pass *starts* but the pass stores its items only
-- after every source answered. A pass that starts at 08:59:30 and commits at
-- 09:01 stores items stamped 08:59:30 after the 09:00 digest has read its
-- window and closed it, so no later window (which starts at 09:00) can ever
-- pick them up. `items.id` is assigned at commit, in commit order, so "the
-- newest id this digest covered" is a mark that can't be overtaken by a slow
-- writer: whatever commits later has a bigger id and lands in the next window.
-- The time columns stay for display and for the 48 hour floor.
--
-- digests.items_after / items_upto: the exclusive and inclusive id bounds this
-- digest covered. items_after is NULL for a server's first digest (nothing came
-- before it; the time floor alone limits it). items_upto is NULL on rows v2.2
-- wrote; the reader derives it from the row's window_end.
-- digests.game_items_upto: JSON {game_key: id} for the games a comped digest
-- took from a shared summary, whose own coverage ends where the summary did,
-- not where the digest was claimed. Games not listed use items_upto.
-- game_summaries.items_after / items_upto: the same bounds for a summary. A
-- server may reuse a summary only if it starts exactly where that server's own
-- coverage of the game ended (plan §3.6), so two servers with different
-- schedules each get their whole news and nothing twice.
--
-- Every column is nullable, which is what keeps v2.2.0 happy after a rollback:
-- its INSERTs name their columns and never mention these, and v2.2 has never
-- heard of game_summaries at all. (tests/test_migration_005_v22_compat.py runs
-- v2.2's SQL against a database with this applied.)
ALTER TABLE digests ADD COLUMN items_after INTEGER;
ALTER TABLE digests ADD COLUMN items_upto INTEGER;
ALTER TABLE digests ADD COLUMN game_items_upto TEXT;
ALTER TABLE game_summaries ADD COLUMN items_after INTEGER;
ALTER TABLE game_summaries ADD COLUMN items_upto INTEGER;
