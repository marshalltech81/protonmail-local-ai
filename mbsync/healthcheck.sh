#!/bin/bash
set -Eeuo pipefail

# Liveness, not freshness (#277): healthy while the sync loop is alive,
# whether or not a sync has completed yet. Whether the mail is current is
# reported by get_mailbox_status from the Maildir success stamp, so a
# first sync that runs for hours is "healthy, not yet current" and the
# indexer and MCP server can start during it.
#
# A sync loop that keeps failing still exits after its consecutive-failure
# limit and the container restarts. A single mbsync that never returns
# stays healthy here; bounding it is the stall deadline's job (#282).

readonly RUNTIME_DIR="/tmp/mbsync"
readonly CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
readonly CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"
readonly SYNC_ACTIVITY_FILE="${RUNTIME_DIR}/last-sync-activity"
readonly PROC_DIR="/proc"
readonly SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
readonly HEALTH_SLACK_SECONDS=30

mbsync_running() {
    # Any process named mbsync: inside this container that is the sync
    # the entrypoint is waiting on. A process can exit between the listing
    # and the read, so an unreadable entry is expected and skipped.
    local comm_file name
    for comm_file in "$PROC_DIR"/[0-9]*/comm; do
        { read -r name <"$comm_file"; } 2>/dev/null || continue
        if [[ "$name" == "mbsync" ]]; then
            return 0
        fi
    done
    return 1
}

check_health() {
    # The heartbeat is touched before and after every sync attempt, so
    # between attempts it is at most SYNC_INTERVAL old; during an attempt
    # it ages with the attempt, and the running mbsync stands in for it.
    local now last_activity max_age_seconds

    if [[ ! -s "$CONFIG_FILE" || ! -s "$CERT_FILE" || ! -f "$SYNC_ACTIVITY_FILE" ]]; then
        return 1
    fi

    now="$(date +%s)"
    last_activity="$(stat -c %Y "$SYNC_ACTIVITY_FILE")"
    max_age_seconds=$((SYNC_INTERVAL * 3 + HEALTH_SLACK_SECONDS))

    if ((now - last_activity <= max_age_seconds)) || mbsync_running; then
        return 0
    fi

    echo "mbsync loop shows no sync activity for over ${max_age_seconds}s and no sync is running" >&2
    return 1
}

if [[ ! "$SYNC_INTERVAL" =~ ^[0-9]+$ ]]; then
    echo "SYNC_INTERVAL must be an integer number of seconds" >&2
    exit 1
fi

check_health
