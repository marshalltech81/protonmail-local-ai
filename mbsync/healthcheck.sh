#!/bin/bash
set -Eeuo pipefail

# Liveness, not freshness (#277): healthy while the sync loop is alive,
# whether or not a sync has completed yet. Whether the mail is current is
# reported by get_mailbox_status from the Maildir success stamp, so a
# first sync that runs for hours is "healthy, not yet current" and the
# indexer and MCP server can start during it.
#
# A sync loop that keeps failing still exits after its consecutive-failure
# limit and the container restarts. A single mbsync is stopped at its
# per-run deadline (#282); one still running past the deadline, the kill
# grace and the slack is reported unhealthy.

readonly RUNTIME_DIR="/tmp/mbsync"
readonly CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
readonly CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"
readonly SYNC_ACTIVITY_FILE="${RUNTIME_DIR}/last-sync-activity"
readonly PROC_DIR="/proc"
readonly SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
readonly HEALTH_SLACK_SECONDS=30
# The entrypoint's per-run deadline and kill grace: keep these in step
# with mbsync/entrypoint.sh.
readonly SYNC_DEADLINE_SECONDS="${SYNC_DEADLINE_SECONDS:-86400}"
readonly SYNC_KILL_GRACE_SECONDS=30

sync_in_progress() {
    # Any process named mbsync or find: inside this container these are
    # the sync the entrypoint is waiting on and the permission repair that
    # follows it, which walks the whole Maildir. A process can exit
    # between the listing and the read, so an unreadable entry is expected
    # and skipped.
    local comm_file name
    for comm_file in "$PROC_DIR"/[0-9]*/comm; do
        { read -r name <"$comm_file"; } 2>/dev/null || continue
        if [[ "$name" == "mbsync" || "$name" == "find" ]]; then
            return 0
        fi
    done
    return 1
}

check_health() {
    # The heartbeat is touched before and after every sync attempt, so
    # between attempts it is at most SYNC_INTERVAL old; during an attempt
    # it ages with the attempt, and the running mbsync or repair walk
    # stands in for it until the run's deadline (plus grace and slack)
    # has passed.
    local now last_activity age max_age_seconds max_run_seconds

    if [[ ! -s "$CONFIG_FILE" || ! -s "$CERT_FILE" || ! -f "$SYNC_ACTIVITY_FILE" ]]; then
        return 1
    fi

    now="$(date +%s)"
    last_activity="$(stat -c %Y "$SYNC_ACTIVITY_FILE")"
    age=$((now - last_activity))
    max_age_seconds=$((SYNC_INTERVAL * 3 + HEALTH_SLACK_SECONDS))
    max_run_seconds=$((SYNC_DEADLINE_SECONDS + SYNC_KILL_GRACE_SECONDS + HEALTH_SLACK_SECONDS))

    if ((age <= max_age_seconds)); then
        return 0
    fi
    if ! sync_in_progress; then
        echo "mbsync loop shows no sync activity for over ${max_age_seconds}s and no sync is running" >&2
        return 1
    fi
    if ((age <= max_run_seconds)); then
        return 0
    fi

    echo "a sync has run for over ${max_run_seconds}s, past its deadline (SYNC_DEADLINE_SECONDS=${SYNC_DEADLINE_SECONDS})" >&2
    return 1
}

if [[ ! "$SYNC_INTERVAL" =~ ^[0-9]+$ ]]; then
    echo "SYNC_INTERVAL must be an integer number of seconds" >&2
    exit 1
fi
if [[ ! "$SYNC_DEADLINE_SECONDS" =~ ^[1-9][0-9]{0,8}$ ]]; then
    echo "SYNC_DEADLINE_SECONDS must be a positive integer number of seconds (at most 999999999)" >&2
    exit 1
fi

check_health
