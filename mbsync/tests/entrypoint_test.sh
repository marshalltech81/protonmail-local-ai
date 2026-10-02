#!/bin/bash
set -Eeuo pipefail

# Tests for mbsync/entrypoint.sh functions that must fail closed, and for
# the liveness check in mbsync/healthcheck.sh.
#
# Both callers run these functions as `if` conditions, where Bash disables
# errexit for the whole call: a failed command inside them is ignored
# unless the function checks it. Each case loads the real function
# definitions from the entrypoint, replaces external commands with shell
# functions, and runs in a subshell against a temporary directory. No
# Bridge, Maildir, or container state is touched.
#
# Run: bash mbsync/tests/entrypoint_test.sh

ENTRYPOINT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/entrypoint.sh"
HEALTHCHECK="$(dirname "$ENTRYPOINT")/healthcheck.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# Define the named functions exactly as the script writes them: from the
# `name() {` line through the first line that is a lone `}`.
load_from() {
    local script="$1" name definition
    shift
    for name in "$@"; do
        definition="$(awk -v start="${name}() {" '$0 == start {p = 1} p {print} p && $0 == "}" {exit}' "$script")"
        if [[ -z "$definition" ]]; then
            printf 'cannot find %s() in %s\n' "$name" "$script" >&2
            exit 1
        fi
        eval "$definition"
    done
}

load() {
    load_from "$ENTRYPOINT" "$@"
}

# Runs each case in a subshell outside any condition, so errexit stays on
# inside it. Bash 3.2 (macOS /bin/bash) does not apply errexit to a failed
# [[ ]] or (( )), so every such assertion ends in `|| return 1`; otherwise
# only a case's last line would count there. A case that exercises a
# function the way its caller does (as a condition) says so.
check() {
    local description="$1" rc=0
    shift
    set +e
    (set -e; "$@") >"$WORK/output" 2>&1
    rc=$?
    set -e
    if ((rc == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/output"
        FAILURES=$((FAILURES + 1))
    fi
}

# Synthetic fingerprints in the pin's format: 64 lowercase hex digits.
FP_OLD="$(printf 'a%.0s' {1..64})"
FP_NEW="$(printf 'b%.0s' {1..64})"
readonly FP_OLD FP_NEW

# --- verify_cert_pin (#240) ------------------------------------------------

pin_setup() {
    STATE_DIR="$WORK/state-$1"
    PIN_FILE="$STATE_DIR/bridge-cert.fingerprint"
    BRIDGE_CERT_PIN_ROTATE="false"
    load write_pin verify_cert_pin
}

first_boot_pins_the_fingerprint() {
    pin_setup first
    mkdir -p "$STATE_DIR"
    verify_cert_pin "$FP_NEW"
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]] || return 1
    [[ "$(stat -c %a "$PIN_FILE" 2>/dev/null || stat -f %Lp "$PIN_FILE")" == "600" ]] || return 1
}

first_boot_fails_closed_when_the_pin_cannot_be_saved() {
    pin_setup unsaved
    # The state directory does not exist, so no write can succeed (even as
    # root, where a read-only directory would not stop it).
    if verify_cert_pin "$FP_NEW"; then
        echo "pin accepted although it was never saved"
        return 1
    fi
    [[ ! -e "$PIN_FILE" ]] || return 1
}

matching_fingerprint_is_accepted() {
    pin_setup match
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    verify_cert_pin "$FP_OLD"
}

mismatch_is_refused_without_rotation() {
    pin_setup mismatch
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    if verify_cert_pin "$FP_NEW"; then
        return 1
    fi
    [[ "$(cat "$PIN_FILE")" == "$FP_OLD" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_replaces_the_pin() {
    pin_setup rotate
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]] || return 1
}

# The flag lives in the container's environment and survives restarts
# (#267), so the documented recovery recreates mbsync with it false. From
# then on the rotated pin is enforced: a second change is refused.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_then_disabled_flag_enforces_the_new_pin() {
    local fp_third
    fp_third="$(printf 'c%.0s' {1..64})"
    pin_setup rotate-then-enforce
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    BRIDGE_CERT_PIN_ROTATE="false"
    verify_cert_pin "$FP_NEW"
    if verify_cert_pin "$fp_third"; then
        echo "a second change was accepted without a new authorization"
        return 1
    fi
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
failed_rotation_fails_closed_and_keeps_the_old_pin() {
    pin_setup rotate-fail
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    # The replacement cannot be moved into place.
    mv() { return 1; }
    if verify_cert_pin "$FP_NEW"; then
        echo "rotation accepted although the new pin was never saved"
        return 1
    fi
    [[ "$(cat "$PIN_FILE")" == "$FP_OLD" ]] || return 1
    # No temporary file is left behind in the state directory.
    [[ "$(find "$STATE_DIR" -type f | wc -l)" -eq 1 ]] || return 1
}

# --- an existing invalid pin is not a first boot (#278) ---------------------
#
# Only an absent pin is a first boot. A pin that exists but is empty,
# malformed, unreadable or a dangling link is refused and left in place.

refuses_the_new_fingerprint() {
    if verify_cert_pin "$FP_NEW"; then
        echo "a cert was accepted over an invalid pin"
        return 1
    fi
}

empty_pin_is_refused_and_kept() {
    pin_setup empty
    mkdir -p "$STATE_DIR"
    : >"$PIN_FILE"
    refuses_the_new_fingerprint
    [[ -f "$PIN_FILE" && ! -s "$PIN_FILE" ]] || return 1
}

malformed_pin_is_refused_and_kept() {
    pin_setup malformed
    mkdir -p "$STATE_DIR"
    printf '%s\n' "${FP_OLD:0:32}" >"$PIN_FILE"
    refuses_the_new_fingerprint
    [[ "$(cat "$PIN_FILE")" == "${FP_OLD:0:32}" ]] || return 1
}

unreadable_pin_is_refused() {
    pin_setup unreadable
    # A directory in the pin's place cannot be read, even as root.
    mkdir -p "$PIN_FILE"
    refuses_the_new_fingerprint
    [[ -d "$PIN_FILE" ]] || return 1
}

dangling_pin_link_is_refused_and_kept() {
    pin_setup dangling
    mkdir -p "$STATE_DIR"
    ln -s "$STATE_DIR/missing" "$PIN_FILE"
    refuses_the_new_fingerprint
    [[ -L "$PIN_FILE" && ! -e "$STATE_DIR/missing" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_replaces_an_invalid_pin() {
    pin_setup invalid-rotate
    mkdir -p "$STATE_DIR"
    : >"$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]] || return 1
}

# Rotation must be able to repair a pin it cannot read (#342 review round
# 1): the documented recovery is one run with BRIDGE_CERT_PIN_ROTATE=true.

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_replaces_a_dangling_pin_link() {
    pin_setup dangling-rotate
    mkdir -p "$STATE_DIR"
    ln -s "$STATE_DIR/missing" "$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    [[ ! -L "$PIN_FILE" && "$(cat "$PIN_FILE")" == "$FP_NEW" && ! -e "$STATE_DIR/missing" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_replaces_an_unreadable_pin_file() {
    pin_setup unreadable-rotate
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    chmod 000 "$PIN_FILE"
    # root reads a mode-000 file, so there is nothing to test as root.
    if [[ -r "$PIN_FILE" ]]; then
        return 0
    fi
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_refuses_a_directory_in_the_pins_place() {
    pin_setup directory-rotate
    mkdir -p "$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    # mv would move the new pin into the directory and report success.
    refuses_the_new_fingerprint
    [[ -d "$PIN_FILE" && -z "$(find "$PIN_FILE" -mindepth 1)" ]] || return 1
}

# A pin path that exists but is not a regular file is refused before it is
# opened (#342 review round 2): reading a FIFO blocks forever, so the
# container would neither report the damage nor exit.

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
fifo_pin_is_refused_without_reading() {
    local writer err
    pin_setup fifo
    mkdir -p "$STATE_DIR"
    mkfifo "$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    # If the FIFO is opened, this writer unblocks the read after 2 s, so a
    # regression fails on the message check instead of hanging the suite.
    { sleep 2 >"$PIN_FILE"; } &
    writer=$!
    if err="$(verify_cert_pin "$FP_NEW" 2>&1)"; then
        echo "a cert was accepted over a FIFO pin"
        kill "$writer" 2>/dev/null || true
        return 1
    fi
    kill "$writer" 2>/dev/null || true
    wait "$writer" 2>/dev/null || true
    [[ "$err" == *"not a regular file"* && -p "$PIN_FILE" ]] || return 1
}

# --- run_sync (#227) -------------------------------------------------------

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
sync_setup() {
    MAILDIR_PATH="$WORK/maildir-$1"
    CONFIG_FILE="$WORK/mbsyncrc"
    FIND_CALLS="$WORK/find-calls-$1"
    SYNC_ACTIVITY_FILE="$WORK/activity-$1"
    MBSYNC_ERROR_COUNTS_FILE="$WORK/mbsync-error-counts-$1"
    MBSYNC_ERROR_COUNTS_WAIT_TENTHS=50
    mkdir -p "$MAILDIR_PATH"
    : >"$FIND_CALLS"
    load run_child relax_new_maildir_perms mark_sync_activity report_mbsync_errors \
        read_mbsync_error_counts run_sync
}

# run_sync is called as `run_sync || rc=$?`, a condition like the
# entrypoint's `if run_sync`, so errexit is off inside it there too.

mbsync_ok() { return 0; }
mbsync_fails() { return 3; }

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
sync_succeeds_when_mbsync_and_repair_succeed() {
    local rc=0
    sync_setup ok
    mbsync() { mbsync_ok; }
    find() { printf 'find\n' >>"$FIND_CALLS"; }
    run_sync || rc=$?
    ((rc == 0)) || return 1
    [[ "$(wc -l <"$FIND_CALLS")" -eq 2 ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
failed_directory_repair_fails_the_sync() {
    local rc=0
    sync_setup dir-fail
    mbsync() { mbsync_ok; }
    find() {
        printf 'find\n' >>"$FIND_CALLS"
        [[ "$3" != "d" ]]
    }
    run_sync || rc=$?
    ((rc == 1)) || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
failed_file_repair_fails_the_sync() {
    local rc=0
    sync_setup file-fail
    mbsync() { mbsync_ok; }
    find() {
        printf 'find\n' >>"$FIND_CALLS"
        [[ "$3" != "f" ]]
    }
    run_sync || rc=$?
    ((rc == 1)) || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
repair_still_runs_after_a_failed_mbsync() {
    local rc=0
    sync_setup mbsync-fail
    mbsync() { mbsync_fails; }
    find() { printf 'find\n' >>"$FIND_CALLS"; }
    # mbsync's own status, not the repair's, is what the sync reports.
    run_sync || rc=$?
    ((rc == 3)) || return 1
    [[ "$(wc -l <"$FIND_CALLS")" -eq 2 ]] || return 1
}

# --- far-side boxes that cannot be opened (#276) ------------------------------
#
# A Proton folder renamed or deleted after it synced keeps its local copy
# (Create Near, Expunge None), so isync 1.4.4 tries to open the vanished
# far-side box on every run, prints the line below on stderr, syncs the
# other boxes and exits 1. That run is a degraded success: warned with a
# count, not counted as a failure. Folder names are mailbox content, so a
# synthetic marker stands in for one and must never reach the log.

readonly FAR_BOX_MARKER="Folders/MarkerZq9-276"
FAR_BOX_LINE="Error: channel protonmail: far side box ${FAR_BOX_MARKER} cannot be opened."
readonly FAR_BOX_LINE

# A mock mbsync: writes stdout line $1 (if any), each stderr line in
# MOCK_STDERR (newline-separated), then returns MOCK_RC.
mock_mbsync_output() {
    if [[ -n "${MOCK_STDOUT:-}" ]]; then
        printf '%s\n' "$MOCK_STDOUT"
    fi
    if [[ -n "${MOCK_STDERR:-}" ]]; then
        printf '%s\n' "$MOCK_STDERR" >&2
    fi
    return "$MOCK_RC"
}

# Runs run_sync the way the entrypoint does, capturing its output in
# SYNC_LOG and its status in SYNC_RC.
run_sync_logged() {
    SYNC_LOG="$WORK/sync-log-$1"
    SYNC_RC=0
    run_sync >"$SYNC_LOG" 2>&1 || SYNC_RC=$?
}

marker_not_logged() {
    if grep -qF "MarkerZq9" "$SYNC_LOG"; then
        echo "a folder name reached the log:"
        cat "$SYNC_LOG"
        return 1
    fi
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
unopenable_far_box_only_is_a_warned_success() {
    sync_setup far-only
    mbsync() { mock_mbsync_output; }
    find() { printf 'find\n' >>"$FIND_CALLS"; }
    MOCK_RC=1
    MOCK_STDOUT="Maildir notice: sleeping due to recent directory modification."
    MOCK_STDERR="${FAR_BOX_LINE}"$'\n'"${FAR_BOX_LINE/MarkerZq9-276/MarkerZq9-other}"
    run_sync_logged far-only
    ((SYNC_RC == 0)) || return 1
    grep -q 'WARNING: 2 far-side folder' "$SYNC_LOG" || return 1
    # mbsync's own stdout still passes through.
    grep -qxF "$MOCK_STDOUT" "$SYNC_LOG" || return 1
    [[ "$(wc -l <"$FIND_CALLS")" -eq 2 ]] || return 1
    marker_not_logged
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
unopenable_far_box_with_another_error_still_fails() {
    sync_setup far-mixed
    mbsync() { mock_mbsync_output; }
    find() { :; }
    MOCK_RC=1
    MOCK_STDERR="${FAR_BOX_LINE}"$'\n'"Error: synthetic other failure"
    run_sync_logged far-mixed
    ((SYNC_RC == 1)) || return 1
    grep -qxF "Error: synthetic other failure" "$SYNC_LOG" || return 1
    grep -q 'WARNING: 1 far-side folder' "$SYNC_LOG" || return 1
    marker_not_logged
}

# INBOX cannot be renamed or deleted, so an INBOX that cannot be opened is
# Bridge refusing boxes, not a vanished folder.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
unopenable_far_inbox_still_fails() {
    sync_setup far-inbox
    mbsync() { mock_mbsync_output; }
    find() { :; }
    MOCK_RC=1
    MOCK_STDERR="${FAR_BOX_LINE}"$'\n'"Error: channel protonmail: far side box INBOX cannot be opened."
    run_sync_logged far-inbox
    ((SYNC_RC == 1)) || return 1
    grep -qxF "Error: channel protonmail: far side box INBOX cannot be opened." "$SYNC_LOG" || return 1
    marker_not_logged
}

# Only mbsync's ordinary failure status (1) is tolerated; anything else (a
# crash, a signal) keeps its status.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
unopenable_far_box_with_another_status_still_fails() {
    sync_setup far-status
    mbsync() { mock_mbsync_output; }
    find() { :; }
    MOCK_RC=139
    MOCK_STDERR="${FAR_BOX_LINE}"
    run_sync_logged far-status
    ((SYNC_RC == 139)) || return 1
    marker_not_logged
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
failure_without_stderr_still_fails() {
    sync_setup far-silent
    mbsync() { mock_mbsync_output; }
    find() { :; }
    MOCK_RC=1
    MOCK_STDERR=""
    run_sync_logged far-silent
    ((SYNC_RC == 1)) || return 1
    if grep -q WARNING "$SYNC_LOG"; then
        return 1
    fi
}

# Only the exact, anchored line counts: a similar line with extra text is
# another error and is passed on as it is.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
near_miss_line_is_not_tolerated() {
    sync_setup far-near-miss
    mbsync() { mock_mbsync_output; }
    find() { :; }
    MOCK_RC=1
    MOCK_STDERR="Error: channel protonmail: near side box Synthetic cannot be opened."
    run_sync_logged far-near-miss
    ((SYNC_RC == 1)) || return 1
    grep -qxF "$MOCK_STDERR" "$SYNC_LOG" || return 1
}

# Errors other than an unopenable far box reach the log while mbsync is
# still running, not when it ends: a first sync can take hours, or stall
# after an error. The mock writes an error, then waits for it to appear
# in the log before it exits; only the counts are kept until the end.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
other_errors_are_streamed_while_mbsync_runs() {
    local seen="$WORK/streamed-seen"
    sync_setup far-stream
    SYNC_LOG="$WORK/sync-log-far-stream"
    : >"$SYNC_LOG"
    find() { :; }
    mbsync() {
        local i
        printf '%s\n' "$FAR_BOX_LINE" >&2
        printf 'Error: synthetic streamed failure\n' >&2
        for ((i = 0; i < 50; i++)); do
            if grep -qxF "Error: synthetic streamed failure" "$SYNC_LOG"; then
                : >"$seen"
                break
            fi
            command sleep 0.1
        done
        return 1
    }
    SYNC_RC=0
    run_sync >"$SYNC_LOG" 2>&1 || SYNC_RC=$?
    [[ -f "$seen" ]] || return 1
    ((SYNC_RC == 1)) || return 1
    grep -q 'WARNING: 1 far-side folder' "$SYNC_LOG" || return 1
    # Only the two counts were written to disk, and they are gone.
    [[ ! -e "$MBSYNC_ERROR_COUNTS_FILE" ]] || return 1
    marker_not_logged
}

# The real sync loop, extracted from the entrypoint, around the real
# run_sync: failures still count to the exit, and a degraded success
# resets the count and writes the success stamp like any success.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
sync_loop_counts_failures_but_not_unopenable_far_boxes() {
    local loop calls_file="$WORK/loop-calls" stamps_file="$WORK/loop-stamps" rc=0
    sync_setup loop
    loop="$(awk '$0 == "while true; do" {p = 1} p {print} p && $0 == "done" {exit}' "$ENTRYPOINT")"
    [[ -n "$loop" ]] || return 1
    : >"$calls_file"
    : >"$stamps_file"
    find() { :; }
    # Skip only the interval sleep; short waits inside run_sync stay real.
    sleep() { [[ "$1" == "$SYNC_INTERVAL" ]] || command sleep "$1"; }
    record_successful_sync() { printf 'stamp\n' >>"$stamps_file"; }
    # Calls 1-4 fail, call 5 meets only a vanished far box, 6-10 fail.
    mbsync() {
        local n
        printf 'call\n' >>"$calls_file"
        n="$(wc -l <"$calls_file")"
        if ((n == 5)); then
            printf '%s\n' "$FAR_BOX_LINE" >&2
        else
            printf 'Error: synthetic other failure\n' >&2
        fi
        return 1
    }
    MAX_CONSECUTIVE_SYNC_FAILURES=5
    SYNC_INTERVAL=3600
    consecutive_sync_failures=0
    (eval "$loop") >"$WORK/loop-log" 2>&1 || rc=$?
    ((rc == 1)) || return 1
    [[ "$(wc -l <"$calls_file")" -eq 10 ]] || return 1
    [[ "$(wc -l <"$stamps_file")" -eq 1 ]] || return 1
    grep -q 'exceeded 5 consecutive failures' "$WORK/loop-log" || return 1
    SYNC_LOG="$WORK/loop-log"
    marker_not_logged
}

# --- sync activity heartbeat (#277) ------------------------------------------
#
# The healthcheck counts the sync loop alive while this file is fresh or
# mbsync is running, so it is touched before mbsync starts, after mbsync
# ends (the permission repair that follows walks the whole Maildir), and
# once the attempt is over, whatever its outcome. Each mock consumes the
# file, so the log shows which touch preceded which step.

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
activity_setup() {
    sync_setup "activity-$1"
    ACTIVITY_LOG="$WORK/activity-log-$1"
    : >"$ACTIVITY_LOG"
}

# Logs the named step if the heartbeat was there for it, then removes it.
consume_activity() {
    if [[ -f "$SYNC_ACTIVITY_FILE" ]]; then
        printf '%s\n' "$1" >>"$ACTIVITY_LOG"
    fi
    rm -f "$SYNC_ACTIVITY_FILE"
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
activity_is_marked_around_a_successful_sync() {
    local rc=0
    activity_setup ok
    mbsync() { consume_activity mbsync; }
    find() { consume_activity "find-$3"; }
    run_sync || rc=$?
    ((rc == 0)) || return 1
    [[ "$(cat "$ACTIVITY_LOG")" == "mbsync"$'\n'"find-d" ]] || return 1
    [[ -f "$SYNC_ACTIVITY_FILE" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
activity_is_marked_after_a_failed_mbsync() {
    local rc=0
    activity_setup mbsync-fail
    mbsync() {
        consume_activity mbsync
        return 3
    }
    find() { consume_activity "find-$3"; }
    run_sync || rc=$?
    ((rc == 3)) || return 1
    [[ "$(cat "$ACTIVITY_LOG")" == "mbsync"$'\n'"find-d" ]] || return 1
    [[ -f "$SYNC_ACTIVITY_FILE" ]] || return 1
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
activity_is_marked_after_a_failed_repair() {
    local rc=0
    activity_setup repair-fail
    mbsync() { consume_activity mbsync; }
    find() {
        consume_activity "find-$3"
        return 1
    }
    run_sync || rc=$?
    ((rc == 1)) || return 1
    [[ -f "$SYNC_ACTIVITY_FILE" ]] || return 1
}

# The success stamp is freshness, not liveness: a completed sync still
# writes it with the same content and mode, and nothing else.
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
success_stamp_is_written_by_a_completed_sync() {
    MAILDIR_PATH="$WORK/maildir-stamp"
    SYNC_STAMP_FILE="$MAILDIR_PATH/.mbsync-last-sync.json"
    SYNC_INTERVAL=60
    mkdir -p "$MAILDIR_PATH"
    load record_successful_sync
    record_successful_sync
    grep -qE '^\{"completed_at": "[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z", "sync_interval_secs": 60\}$' \
        "$SYNC_STAMP_FILE" || return 1
    [[ "$(stat -c %a "$SYNC_STAMP_FILE" 2>/dev/null || stat -f %Lp "$SYNC_STAMP_FILE")" == "644" ]] || return 1
    [[ "$(find "$MAILDIR_PATH" -type f | wc -l)" -eq 1 ]] || return 1
}

# --- healthcheck: liveness, not freshness (#277) ----------------------------
#
# Healthy means the sync loop is alive: config and cert are in place and
# either the activity heartbeat is fresh or an mbsync or its permission
# repair walk is running. A first sync that takes hours is healthy with no
# success stamp; whether mail is current is get_mailbox_status's job. /proc
# is a temporary directory and stat reports the heartbeat's synthetic age.

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
health_setup() {
    local dir="$WORK/health-$1"
    CONFIG_FILE="$dir/mbsyncrc"
    CERT_FILE="$dir/bridge-cert.pem"
    SYNC_ACTIVITY_FILE="$dir/last-sync-activity"
    PROC_DIR="$dir/proc"
    SYNC_INTERVAL=60
    HEALTH_SLACK_SECONDS=30
    ACTIVITY_AGE=0
    mkdir -p "$PROC_DIR"
    printf 'synthetic-config\n' >"$CONFIG_FILE"
    printf 'synthetic-cert\n' >"$CERT_FILE"
    : >"$SYNC_ACTIVITY_FILE"
    # Processes that are always there: init and the entrypoint.
    add_process 1 docker-init
    add_process 7 entrypoint.sh
    stat() { printf '%s\n' "$(($(date +%s) - ACTIVITY_AGE))"; }
    load_from "$HEALTHCHECK" sync_in_progress check_health
}

add_process() {
    mkdir -p "$PROC_DIR/$1"
    printf '%s\n' "$2" >"$PROC_DIR/$1/comm"
}

unhealthy() {
    if check_health; then
        echo "reported healthy"
        return 1
    fi
}

# Three hours: far past the 210 s heartbeat limit at SYNC_INTERVAL=60.
readonly HOURS_AGO=10800

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
long_first_sync_in_progress_is_healthy() {
    health_setup first-sync
    ACTIVITY_AGE=$HOURS_AGO
    add_process 42 mbsync
    check_health
}

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
fresh_heartbeat_between_syncs_is_healthy() {
    health_setup between
    ACTIVITY_AGE=60
    add_process 43 sleep
    check_health
}

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
stale_heartbeat_without_mbsync_is_unhealthy() {
    local err
    health_setup stale
    ACTIVITY_AGE=$HOURS_AGO
    add_process 43 sleep
    add_process 44 chmod
    err="$(unhealthy 2>&1)" || return 1
    [[ "$err" == *"no sync activity"* ]] || return 1
}

# The permission repair after a sync walks the whole Maildir with no
# mbsync running; on a large Maildir it can outlast the heartbeat limit
# (#515 review round 1).
# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
repair_walk_in_progress_is_healthy() {
    health_setup repair
    ACTIVITY_AGE=$HOURS_AGO
    add_process 44 find
    check_health
}

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
heartbeat_just_past_the_limit_is_unhealthy() {
    health_setup limit
    ACTIVITY_AGE=211
    unhealthy
}

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
missing_heartbeat_is_unhealthy_before_the_first_attempt() {
    health_setup no-heartbeat
    rm -f "$SYNC_ACTIVITY_FILE"
    add_process 42 mbsync
    unhealthy
}

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
missing_config_is_unhealthy_even_while_syncing() {
    health_setup no-config
    rm -f "$CONFIG_FILE"
    add_process 42 mbsync
    unhealthy
}

# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
empty_cert_is_unhealthy_even_while_syncing() {
    health_setup no-cert
    : >"$CERT_FILE"
    add_process 42 mbsync
    unhealthy
}

# A process that exits between the /proc listing and the read is skipped
# (a directory stands in for its comm file, which then cannot be read).
# shellcheck disable=SC2034,SC2329 # used by the healthcheck functions loaded with eval
a_vanished_process_is_skipped() {
    health_setup vanished
    ACTIVITY_AGE=$HOURS_AGO
    mkdir -p "$PROC_DIR/50/comm"
    add_process 51 mbsync
    check_health
}

healthcheck_does_not_read_the_success_stamp() {
    if grep -n 'last-successful-sync\|mbsync-last-sync' "$HEALTHCHECK"; then
        return 1
    fi
}

# --- wait_for_bridge_imap (#271) ---------------------------------------------
#
# Each probe is bounded, so the wait's total is bounded by its attempts.
# The probe runs under timeout(1), which can only run executables, so the
# mock nc is a script on PATH that logs its arguments.

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
probe_setup() {
    RUNTIME_DIR="$WORK/runtime-$1"
    BRIDGE_HOST="bridge.invalid"
    BRIDGE_IMAP_PORT=1143
    BRIDGE_WAIT_INTERVAL_SECONDS=0
    BRIDGE_WAIT_MAX_ATTEMPTS=3
    BRIDGE_PROBE_TIMEOUT_SECONDS=1
    NC_CALLS="$WORK/nc-calls-$1"
    mkdir -p "$RUNTIME_DIR" "$WORK/bin-$1"
    : >"$NC_CALLS"
    PATH="$WORK/bin-$1:$PATH"
    load wait_for_bridge_imap
}

# Writes a mock nc that logs its arguments and then runs the given body.
mock_nc() {
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >>"%s"\n%s\n' "$NC_CALLS" "$2" >"$WORK/bin-$1/nc"
    chmod 755 "$WORK/bin-$1/nc"
}

# shellcheck disable=SC2034 # used by the entrypoint functions loaded with eval
hung_probes_are_cut_off_by_the_per_attempt_bound() {
    local start rc=0 err
    probe_setup hung
    # A blackholed connect: the probe never returns on its own.
    mock_nc hung 'exec sleep 10'
    BRIDGE_WAIT_MAX_ATTEMPTS=2
    start=$SECONDS
    err="$(wait_for_bridge_imap 2>&1)" || rc=$?
    ((rc == 1)) || return 1
    # Two attempts of at most two seconds each; without the bound the
    # first probe alone takes 10 s.
    ((SECONDS - start < 8)) || return 1
    [[ "$(wc -l <"$NC_CALLS")" -eq 2 ]] || return 1
    # nc gets its own connect timeout inside the outer timeout(1).
    grep -qx -- "-z -w 1 bridge.invalid 1143" "$NC_CALLS"
    [[ "$err" == *"after 2 attempts (at most 4 seconds)"* ]] || return 1
}

reachable_bridge_returns_after_one_probe() {
    probe_setup reachable
    mock_nc reachable 'exit 0'
    wait_for_bridge_imap
    [[ "$(wc -l <"$NC_CALLS")" -eq 1 ]] || return 1
}

refused_probes_fail_after_the_attempts_with_nc_stderr() {
    local rc=0 err
    probe_setup refused
    mock_nc refused 'echo "synthetic-refused" >&2; exit 1'
    err="$(wait_for_bridge_imap 2>&1)" || rc=$?
    ((rc == 1)) || return 1
    [[ "$(wc -l <"$NC_CALLS")" -eq 3 ]] || return 1
    [[ "$err" == *"synthetic-refused"* ]] || return 1
}

# --- shutdown signals reach the active child (#280) -------------------------
#
# The entrypoint is the only process Tini signals, so it must pass a stop on
# to the sync (or the sleep between syncs) and exit once that child ends.
# Each case runs the entrypoint's handlers in a background subshell that
# stands in for the entrypoint, signals it, and checks the child got TERM.

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
stop_setup() {
    MAILDIR_PATH="$WORK/maildir-stop-$1"
    CONFIG_FILE="$WORK/mbsyncrc"
    CHILD_LOG="$WORK/child-$1"
    SYNC_ACTIVITY_FILE="$WORK/activity-stop-$1"
    MBSYNC_ERROR_COUNTS_FILE="$WORK/mbsync-error-counts-stop-$1"
    MBSYNC_ERROR_COUNTS_WAIT_TENTHS=50
    mkdir -p "$MAILDIR_PATH" "$WORK/bin-stop-$1"
    : >"$CHILD_LOG"
    # A long-running child that records its start and any TERM it gets.
    # Background commands ignore INT, so TERM is what must reach it.
    export CHILD_LOG
    cat >"$WORK/bin-stop-$1/mbsync" <<'MOCK'
#!/bin/bash
trap 'echo term >>"$CHILD_LOG"; kill "$sleeper"; exit 143' TERM
echo started >>"$CHILD_LOG"
sleep 30 &
sleeper=$!
wait
MOCK
    chmod 755 "$WORK/bin-stop-$1/mbsync"
    PATH="$WORK/bin-stop-$1:$PATH"
    load run_child stop_on_signal install_signal_handlers relax_new_maildir_perms \
        mark_sync_activity report_mbsync_errors read_mbsync_error_counts run_sync
}

# Signals the stand-in entrypoint once its child has started and waits for
# it; sets STOP_RC to its exit status and STOP_SECONDS to how long it took.
# Runs in the shell that started the stand-in, since only it can wait.
signal_once_started() {
    local entrypoint="$1" signal="$2" start i
    for ((i = 0; i < 50; i++)); do
        grep -q started "$CHILD_LOG" && break
        sleep 0.1
    done
    grep -q started "$CHILD_LOG"
    start=$SECONDS
    kill "-$signal" "$entrypoint"
    STOP_RC=0
    wait "$entrypoint" || STOP_RC=$?
    STOP_SECONDS=$((SECONDS - start))
}

term_during_a_sync_stops_mbsync_and_exits() {
    stop_setup sync-term
    (install_signal_handlers && run_sync) &
    signal_once_started "$!" TERM
    ((STOP_RC == 143 && STOP_SECONDS < 5)) || return 1
    grep -qx term "$CHILD_LOG"
}

int_during_a_sync_stops_mbsync_and_exits() {
    stop_setup sync-int
    # Without job control, a background subshell starts with INT ignored,
    # and Bash 3.2 then refuses to trap it (Bash 5 allows the trap). The
    # entrypoint starts with INT at its default, so turn job control on
    # for the launch to give the stand-in the same start on both shells.
    set -m
    (install_signal_handlers && run_sync) &
    set +m
    signal_once_started "$!" INT
    ((STOP_RC == 130 && STOP_SECONDS < 5)) || return 1
    grep -qx term "$CHILD_LOG"
}

term_during_the_sleep_between_syncs_exits_promptly() {
    stop_setup sleep-term
    # The loop's sleep goes through run_child like the sync; the mock
    # stands in for a long sleep.
    (install_signal_handlers && run_child mbsync) &
    signal_once_started "$!" TERM
    ((STOP_RC == 143 && STOP_SECONDS < 5)) || return 1
    grep -qx term "$CHILD_LOG"
}

the_sleep_between_syncs_runs_through_run_child() {
    grep -qxF "    run_child sleep \"\$SYNC_INTERVAL\"" "$ENTRYPOINT"
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
run_child_returns_the_child_status() {
    load run_child
    run_child true
    if run_child false; then
        return 1
    fi
    [[ -z "$child_pid" ]] || return 1
}

check "TERM during a sync stops mbsync and exits 143" term_during_a_sync_stops_mbsync_and_exits
check "INT during a sync stops mbsync and exits 130" int_during_a_sync_stops_mbsync_and_exits
check "TERM during the sleep between syncs exits promptly" \
    term_during_the_sleep_between_syncs_exits_promptly
check "the sleep between syncs runs through run_child" \
    the_sleep_between_syncs_runs_through_run_child
check "run_child returns the child's status" run_child_returns_the_child_status
check "hung probes are cut off by the per-attempt bound" \
    hung_probes_are_cut_off_by_the_per_attempt_bound
check "a reachable Bridge returns after one probe" reachable_bridge_returns_after_one_probe
check "refused probes fail after the attempts with nc's stderr" \
    refused_probes_fail_after_the_attempts_with_nc_stderr
check "first boot pins the fingerprint (mode 600)" first_boot_pins_the_fingerprint
check "first boot fails closed when the pin cannot be saved" \
    first_boot_fails_closed_when_the_pin_cannot_be_saved
check "a matching fingerprint is accepted" matching_fingerprint_is_accepted
check "a mismatch is refused without rotation" mismatch_is_refused_without_rotation
check "rotation replaces the pin" rotation_replaces_the_pin
check "after a rotation, the flag set false enforces the new pin" \
    rotation_then_disabled_flag_enforces_the_new_pin
check "a failed rotation fails closed and keeps the old pin" \
    failed_rotation_fails_closed_and_keeps_the_old_pin
check "an existing empty pin is refused and kept" empty_pin_is_refused_and_kept
check "a malformed pin is refused and kept" malformed_pin_is_refused_and_kept
check "an unreadable pin is refused" unreadable_pin_is_refused
check "a dangling pin link is refused and kept" dangling_pin_link_is_refused_and_kept
check "rotation replaces an invalid pin" rotation_replaces_an_invalid_pin
check "rotation replaces a dangling pin link" rotation_replaces_a_dangling_pin_link
check "rotation replaces an unreadable pin file" rotation_replaces_an_unreadable_pin_file
check "rotation refuses a directory in the pin's place" \
    rotation_refuses_a_directory_in_the_pins_place
check "a FIFO pin is refused without reading it" fifo_pin_is_refused_without_reading
check "sync succeeds when mbsync and the repair succeed" \
    sync_succeeds_when_mbsync_and_repair_succeed
check "a failed directory repair fails the sync" failed_directory_repair_fails_the_sync
check "a failed file repair fails the sync" failed_file_repair_fails_the_sync
check "the repair still runs after a failed mbsync" repair_still_runs_after_a_failed_mbsync
check "unopenable far boxes alone are a warned success" \
    unopenable_far_box_only_is_a_warned_success
check "an unopenable far box with another error still fails" \
    unopenable_far_box_with_another_error_still_fails
check "an unopenable far INBOX still fails" unopenable_far_inbox_still_fails
check "an unopenable far box with another status still fails" \
    unopenable_far_box_with_another_status_still_fails
check "a failure with no stderr still fails" failure_without_stderr_still_fails
check "a near-miss error line is not tolerated" near_miss_line_is_not_tolerated
check "other errors are streamed while mbsync runs" other_errors_are_streamed_while_mbsync_runs
check "the sync loop counts failures but not unopenable far boxes" \
    sync_loop_counts_failures_but_not_unopenable_far_boxes
check "activity is marked before mbsync, before the repair and after the sync" \
    activity_is_marked_around_a_successful_sync
check "activity is marked after a failed mbsync" activity_is_marked_after_a_failed_mbsync
check "activity is marked after a failed repair" activity_is_marked_after_a_failed_repair
check "a completed sync still writes the success stamp" \
    success_stamp_is_written_by_a_completed_sync
check "health: a long first sync in progress is healthy" long_first_sync_in_progress_is_healthy
check "health: a fresh heartbeat between syncs is healthy" fresh_heartbeat_between_syncs_is_healthy
check "health: a stale heartbeat without mbsync is unhealthy" \
    stale_heartbeat_without_mbsync_is_unhealthy
check "health: the repair walk in progress is healthy" repair_walk_in_progress_is_healthy
check "health: a heartbeat just past the limit is unhealthy" \
    heartbeat_just_past_the_limit_is_unhealthy
check "health: no heartbeat before the first attempt is unhealthy" \
    missing_heartbeat_is_unhealthy_before_the_first_attempt
check "health: missing config is unhealthy even while syncing" \
    missing_config_is_unhealthy_even_while_syncing
check "health: an empty cert is unhealthy even while syncing" \
    empty_cert_is_unhealthy_even_while_syncing
check "health: a vanished process is skipped" a_vanished_process_is_skipped
check "health: the success stamp is not read" healthcheck_does_not_read_the_success_stamp

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
