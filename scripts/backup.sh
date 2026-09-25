#!/usr/bin/env bash
# Daily backup of the live SQLite database, meant to run from cron on the
# Droplet. Uses sqlite3's own `.backup` command rather than `cp`, because
# copying a WAL-mode database file while the bot is mid-write is a great way
# to end up with a backup that's missing whatever was still in the
# write-ahead log. `.backup` takes a proper read lock and gives you a
# consistent snapshot instead.
#
# Usage: scripts/backup.sh [db_path] [backup_dir]
#   db_path     defaults to ./data/newsbot.db
#   backup_dir  defaults to ./data/backups
#
# Keeps the 7 newest backups and deletes anything older. That's about a
# week of "oh no" coverage, which is the point -- see docs/deploy.md for the
# restore procedure and why off-host backups are a deliberate non-goal for
# v1.
set -euo pipefail

DB_PATH="${1:-./data/newsbot.db}"
BACKUP_DIR="${2:-./data/backups}"
KEEP=7

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "backup.sh: sqlite3 not found. On the Droplet: apt install sqlite3" >&2
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "backup.sh: no database at $DB_PATH, nothing to back up" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

DATE="$(date +%F)"
DEST="$BACKUP_DIR/newsbot-$DATE.db"

# sqlite3's .backup is online-safe: it's the documented way to snapshot a
# database that WAL-mode writers might be touching right now, unlike `cp`,
# which just copies whatever bytes happen to be on disk at that instant.
sqlite3 "$DB_PATH" ".backup '$DEST'"

# A backup that doesn't open is worse than no backup: it's a false sense of
# safety. Cheap to check now, expensive to find out at restore time.
INTEGRITY="$(sqlite3 "$DEST" "pragma integrity_check;")"
if [ "$INTEGRITY" != "ok" ]; then
    echo "backup.sh: integrity check failed for $DEST: $INTEGRITY" >&2
    exit 1
fi

echo "backup.sh: wrote $DEST (integrity check: ok)"

# Keep the newest KEEP backups, delete the rest. `ls -1t` sorts newest
# first; skip the first KEEP lines, remove whatever's left. `xargs -r`
# means "don't run rm at all if there's nothing to remove" -- without -r,
# a directory with 7 or fewer backups would still invoke a bare `xargs
# rm` with no arguments, and some rm implementations treat that as an
# error rather than a no-op. Better to just not ask.
find "$BACKUP_DIR" -maxdepth 1 -name 'newsbot-*.db' -print \
    | sort -r \
    | tail -n "+$((KEEP + 1))" \
    | xargs -r rm --

REMAINING="$(find "$BACKUP_DIR" -maxdepth 1 -name 'newsbot-*.db' | wc -l | tr -d ' ')"
echo "backup.sh: $REMAINING backup(s) retained in $BACKUP_DIR"
