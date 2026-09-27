#!/bin/bash
set -Eeuo pipefail

readonly HEALTH_FILE="${INDEXER_HEALTH_FILE:-/tmp/indexer-health}"
readonly SQLITE_PATH="${SQLITE_PATH:-/data/mail.db}"
# The indexer refreshes the heartbeat between messages and between embed
# requests, so the longest legitimate silence is one embed request that
# exhausts its retries against a hung endpoint: 3 attempts x 120 s
# request timeout plus backoff, about 6.5 minutes. 10 minutes covers that
# with margin while still flagging a genuinely stuck indexer.
readonly HEALTH_MAX_AGE_SECONDS=600

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
