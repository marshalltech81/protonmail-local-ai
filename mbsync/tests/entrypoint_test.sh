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
    [[ -f "$PIN_FILE" && ! -s "$PIN_FILE" ]]
}

malformed_pin_is_refused_and_kept() {
    pin_setup malformed
    mkdir -p "$STATE_DIR"
    printf '%s\n' "${FP_OLD:0:32}" >"$PIN_FILE"
    refuses_the_new_fingerprint
    [[ "$(cat "$PIN_FILE")" == "${FP_OLD:0:32}" ]]
}

unreadable_pin_is_refused() {
    pin_setup unreadable
    # A directory in the pin's place cannot be read, even as root.
    mkdir -p "$PIN_FILE"
    refuses_the_new_fingerprint
    [[ -d "$PIN_FILE" ]]
}

dangling_pin_link_is_refused_and_kept() {
    pin_setup dangling
    mkdir -p "$STATE_DIR"
    ln -s "$STATE_DIR/missing" "$PIN_FILE"
    refuses_the_new_fingerprint
    [[ -L "$PIN_FILE" && ! -e "$STATE_DIR/missing" ]]
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_replaces_an_invalid_pin() {
    pin_setup invalid-rotate
    mkdir -p "$STATE_DIR"
    : >"$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    verify_cert_pin "$FP_NEW"
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]]
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
    [[ ! -L "$PIN_FILE" && "$(cat "$PIN_FILE")" == "$FP_NEW" && ! -e "$STATE_DIR/missing" ]]
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
    [[ "$(cat "$PIN_FILE")" == "$FP_NEW" ]]
}

# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
rotation_refuses_a_directory_in_the_pins_place() {
    pin_setup directory-rotate
    mkdir -p "$PIN_FILE"
    BRIDGE_CERT_PIN_ROTATE="true"
    # mv would move the new pin into the directory and report success.
    refuses_the_new_fingerprint
    [[ -d "$PIN_FILE" && -z "$(find "$PIN_FILE" -mindepth 1)" ]]
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
