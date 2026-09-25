#!/usr/bin/env bash
# Routine update on the Droplet: pull the latest image, recreate the
# container, and wait for the healthcheck to come back green before calling
# it done. Nothing here is magic -- it's the same two `docker compose`
# commands from docs/deploy.md, just saved from being fat-fingered at
# 9:03 a.m. while three tabs are open.
#
# Run this from /opt/newsbot (or wherever docker-compose.yml and
# docker-compose.prod.yml actually live). It does not touch .env,
# config.yaml, or the backup cron -- those are one-time setup, not part of
# a routine update.
#
# Rollback: TAG=sha-xxxxxxx ./scripts/deploy.sh
set -euo pipefail

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

echo "deploy.sh: pulling ${TAG:-latest}..."
"${COMPOSE[@]}" pull

echo "deploy.sh: recreating container..."
"${COMPOSE[@]}" up -d

echo "deploy.sh: waiting for healthy status..."
ATTEMPTS=30
CID="$("${COMPOSE[@]}" ps -q newsbot)"

if [ -z "$CID" ]; then
    echo "deploy.sh: could not find the newsbot container after 'up -d'" >&2
    exit 1
fi

for _ in $(seq 1 "$ATTEMPTS"); do
    STATUS="$(docker inspect -f '{{.State.Health.Status}}' "$CID" 2>/dev/null || echo "unknown")"
    if [ "$STATUS" = "healthy" ]; then
        echo "deploy.sh: container is healthy"
        "${COMPOSE[@]}" ps
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
