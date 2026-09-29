#!/bin/bash
set -Eeuo pipefail

# Tests for mbsync/entrypoint.sh functions that must fail closed.
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
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# Define the named functions exactly as the entrypoint writes them: from
# the `name() {` line through the first line that is a lone `}`.
load() {
    local name definition
    for name in "$@"; do
        definition="$(awk -v start="${name}() {" '$0 == start {p = 1} p {print} p && $0 == "}" {exit}' "$ENTRYPOINT")"
        if [[ -z "$definition" ]]; then
            printf 'cannot find %s() in %s\n' "$name" "$ENTRYPOINT" >&2
            exit 1
        fi
        eval "$definition"
    done
}

check() {
    local description="$1"
    shift
    if ("$@") >"$WORK/output" 2>&1; then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/output"
        FAILURES=$((FAILURES + 1))
    fi
}

readonly FP_OLD="aaaa"
readonly FP_NEW="bbbb"

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
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]]
    [[ "$(stat -c %a "$PIN_FILE" 2>/dev/null || stat -f %Lp "$PIN_FILE")" == "600" ]]
}

first_boot_fails_closed_when_the_pin_cannot_be_saved() {
    pin_setup unsaved
    # The state directory does not exist, so no write can succeed (even as
    # root, where a read-only directory would not stop it).
    if verify_cert_pin "$FP_NEW"; then
        echo "pin accepted although it was never saved"
        return 1
    fi
    [[ ! -e "$PIN_FILE" ]]
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
    [[ "$(cat "$PIN_FILE")" == "$FP_OLD" ]]
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_replaces_the_pin() {
    pin_setup rotate
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$FP_OLD" >"$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]]
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
    [[ "$(cat "$PIN_FILE")" == "$FP_OLD" ]]
    # No temporary file is left behind in the state directory.
    [[ "$(find "$STATE_DIR" -type f | wc -l)" -eq 1 ]]
}

# --- run_sync (#227) -------------------------------------------------------

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
sync_setup() {
    MAILDIR_PATH="$WORK/maildir-$1"
    CONFIG_FILE="$WORK/mbsyncrc"
    FIND_CALLS="$WORK/find-calls-$1"
    mkdir -p "$MAILDIR_PATH"
    : >"$FIND_CALLS"
    load relax_new_maildir_perms run_sync
}

mbsync_ok() { return 0; }
mbsync_fails() { return 1; }

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
sync_succeeds_when_mbsync_and_repair_succeed() {
    sync_setup ok
    mbsync() { mbsync_ok; }
    find() { printf 'find\n' >>"$FIND_CALLS"; }
    run_sync
    [[ "$(wc -l <"$FIND_CALLS")" -eq 2 ]]
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
failed_directory_repair_fails_the_sync() {
    sync_setup dir-fail
    mbsync() { mbsync_ok; }
    find() {
        printf 'find\n' >>"$FIND_CALLS"
        [[ "$3" != "d" ]]
    }
    if run_sync; then
        echo "sync reported success although the directory repair failed"
        return 1
    fi
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
failed_file_repair_fails_the_sync() {
    sync_setup file-fail
    mbsync() { mbsync_ok; }
    find() {
        printf 'find\n' >>"$FIND_CALLS"
        [[ "$3" != "f" ]]
    }
    if run_sync; then
        echo "sync reported success although the file repair failed"
        return 1
    fi
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
repair_still_runs_after_a_failed_mbsync() {
    sync_setup mbsync-fail
    mbsync() { mbsync_fails; }
    find() { printf 'find\n' >>"$FIND_CALLS"; }
    if run_sync; then
        return 1
    fi
    [[ "$(wc -l <"$FIND_CALLS")" -eq 2 ]]
}

check "first boot pins the fingerprint (mode 600)" first_boot_pins_the_fingerprint
check "first boot fails closed when the pin cannot be saved" \
    first_boot_fails_closed_when_the_pin_cannot_be_saved
check "a matching fingerprint is accepted" matching_fingerprint_is_accepted
check "a mismatch is refused without rotation" mismatch_is_refused_without_rotation
check "rotation replaces the pin" rotation_replaces_the_pin
check "a failed rotation fails closed and keeps the old pin" \
    failed_rotation_fails_closed_and_keeps_the_old_pin
check "sync succeeds when mbsync and the repair succeed" \
    sync_succeeds_when_mbsync_and_repair_succeed
check "a failed directory repair fails the sync" failed_directory_repair_fails_the_sync
check "a failed file repair fails the sync" failed_file_repair_fails_the_sync
check "the repair still runs after a failed mbsync" repair_still_runs_after_a_failed_mbsync

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
