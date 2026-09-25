#!/usr/bin/env bash
# Routine update on the Droplet, now that /opt/newsbot is a git clone
# (see docs/deploy.md §2): pull the repo, back up the database, pull the
# new image, recreate the container, and wait for the healthcheck to come
# back green before calling it done. Run this from /opt/newsbot.
#
# Usage:
#   ./scripts/deploy.sh                 routine update
#   ./scripts/deploy.sh --force         skip the 09:00-09:15 deploy-window check
#   ./scripts/deploy.sh --rollback TAG  deploy a specific image tag for this run
#
# TAG (for a pinned release, or a rollback) comes from .env or the shell
# environment -- docker compose already reads .env in the project
# directory for variable substitution, so nothing here needs to source it
# by hand (and doing so would risk echoing secrets; see CLAUDE.md).
set -euo pipefail

FORCE=0
ROLLBACK_TAG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --force)
            FORCE=1
            shift
            ;;
        --rollback)
            if [ $# -lt 2 ]; then
                echo "deploy.sh: --rollback needs a tag argument" >&2
                exit 1
            fi
            ROLLBACK_TAG="$2"
            shift 2
            ;;
        *)
            echo "deploy.sh: unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [ -n "$ROLLBACK_TAG" ]; then
    export TAG="$ROLLBACK_TAG"
    echo "deploy.sh: rolling back to TAG=$TAG for this run only."
    echo "deploy.sh: this does not persist -- set TAG=$TAG in .env yourself if you want it to stick."
fi

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

# --- 1. refuse to deploy during the digest window -----------------------
# The scheduled digest fires at 09:00 America/Los_Angeles; recreating the
# container mid-run risks an interrupted post. The Droplet's own clock is
# UTC (see docs/deploy.md §1), so this is computed in America/Los_Angeles
# terms regardless of host timezone, DST included.
if [ "$FORCE" -ne 1 ]; then
    HHMM_RAW="$(TZ=America/Los_Angeles date +%H%M)"
    # Force base-10: bash treats a leading-zero numeral like "0900" as
    # octal in an arithmetic context, and 9 isn't a valid octal digit --
    # `10#` pins the base explicitly so e.g. 08:30 doesn't blow up the
    # comparison below.
    HHMM=$((10#$HHMM_RAW))
    if [ "$HHMM" -ge 900 ] && [ "$HHMM" -le 915 ]; then
        echo "deploy.sh: it's $HHMM_RAW America/Los_Angeles -- refusing to deploy during the 09:00-09:15 digest window." >&2
        echo "  Use --force to override if you're sure this is safe." >&2
        exit 1
    fi
fi

# --- 2. refuse a dirty working tree -----------------------------------
# A routine update should never pull over local edits to tracked files --
# if something changed docker-compose.yml or scripts/*.sh by hand on the
# Droplet, that's a signal to go look, not to silently discard or merge it.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "deploy.sh: working tree has local changes to tracked files -- refusing to pull." >&2
    echo "  git status --short" >&2
    git status --short >&2
    exit 1
fi

# --- 3. pull the repo ---------------------------------------------------
echo "deploy.sh: pulling repo (git pull --ff-only)..."
git pull --ff-only

# --- 4. back up the database before touching the running container -----
if [ -f data/newsbot.db ]; then
    echo "deploy.sh: backing up data/newsbot.db..."
    # data/ belongs to the container's uid 10001, not to whoever's running
    # this script, hence sudo. (The first draft forgot, and would have
    # failed on the very first upgrade that had a database worth saving.)
    sudo ./scripts/backup.sh data/newsbot.db data/backups
else
    echo "deploy.sh: no database at data/newsbot.db yet -- skipping backup (first deploy)."
fi

# --- 5. pull the image and recreate the container -----------------------
echo "deploy.sh: pulling ${TAG:-latest}..."
"${COMPOSE[@]}" pull

echo "deploy.sh: recreating container..."
"${COMPOSE[@]}" up -d

# --- 6. wait for healthy -------------------------------------------------
echo "deploy.sh: waiting for healthy status..."
ATTEMPTS=36  # 36 * 5s = 3 minutes
CID="$("${COMPOSE[@]}" ps -q newsbot)"

if [ -z "$CID" ]; then
    echo "deploy.sh: could not find the newsbot container after 'up -d'" >&2
    exit 1
fi

STATUS="unknown"
for _ in $(seq 1 "$ATTEMPTS"); do
    STATUS="$(docker inspect -f '{{.State.Health.Status}}' "$CID" 2>/dev/null || echo "unknown")"
    if [ "$STATUS" = "healthy" ]; then
        echo "deploy.sh: container is healthy"
        "${COMPOSE[@]}" ps
        "${COMPOSE[@]}" logs --tail 50
        exit 0
    fi
    if [ "$STATUS" = "unhealthy" ]; then
        echo "deploy.sh: container reported unhealthy -- check logs:" >&2
        echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 100" >&2
        exit 1
    fi
    sleep 5
done

echo "deploy.sh: still '$STATUS' after $((ATTEMPTS * 5))s -- not necessarily broken" >&2
echo "  (the healthcheck has a 120s start period), but check logs to be sure:" >&2
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 100" >&2
exit 1
