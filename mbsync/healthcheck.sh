#!/bin/bash
set -Eeuo pipefail

readonly RUNTIME_DIR="/tmp/mbsync"
readonly CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
readonly CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"
readonly HEALTH_FILE="${RUNTIME_DIR}/last-successful-sync"
readonly SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
readonly HEALTH_SLACK_SECONDS=30

if [[ ! "$SYNC_INTERVAL" =~ ^[0-9]+$ ]]; then
    echo "SYNC_INTERVAL must be an integer number of seconds" >&2
    exit 1
fi

if [[ ! -s "$CONFIG_FILE" || ! -s "$CERT_FILE" || ! -f "$HEALTH_FILE" ]]; then
    exit 1
fi

current_time="$(date +%s)"
last_success_time="$(stat -c %Y "$HEALTH_FILE")"
max_age_seconds=$((SYNC_INTERVAL * 3 + HEALTH_SLACK_SECONDS))

if ((current_time - last_success_time > max_age_seconds)); then
    echo "Last successful mbsync run is stale" >&2
    exit 1
fi

exit 0
