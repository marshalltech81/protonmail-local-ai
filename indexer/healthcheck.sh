#!/bin/bash
set -Eeuo pipefail

readonly HEALTH_FILE="${INDEXER_HEALTH_FILE:-/tmp/indexer-health}"
readonly SQLITE_PATH="${SQLITE_PATH:-/data/mail.db}"
readonly HEALTH_MAX_AGE_SECONDS=90

if [[ ! -f "$SQLITE_PATH" || ! -f "$HEALTH_FILE" ]]; then
    exit 1
fi

current_time="$(date +%s)"
last_health_time="$(stat -c %Y "$HEALTH_FILE")"

if ((current_time - last_health_time > HEALTH_MAX_AGE_SECONDS)); then
    echo "Indexer heartbeat is stale" >&2
    exit 1
fi

exit 0
