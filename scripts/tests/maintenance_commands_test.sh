#!/bin/bash
set -Eeuo pipefail

# Tests for the one-run maintenance targets `make rotate-bridge-pin` and
# `make force-deletion` (#1609). A fake `docker` first on PATH logs each
# call and never contacts a daemon; FAKE_FAIL names the calls that fail
# (`run`, `up`, `stop`) and FAKE_INTERRUPT the call during which Ctrl-C
# arrives; make runs in its own process group. The validate-env prerequisite is skipped with
# `make -o validate-env`: scripts/tests/validate_env_test.sh covers it.
#
# Run: bash scripts/tests/maintenance_commands_test.sh

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

mkdir -p "$WORK/bin"
cat >"$WORK/bin/docker" <<'EOF'
#!/bin/bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
[[ "$1" == compose ]] || exit 99
if [[ " ${FAKE_FAIL:-} " == *" $2 "* ]]; then
    exit 3
fi
# FAKE_INTERRUPT=run: the terminal's Ctrl-C, sent to the whole process
# group (make, the recipe shell and this call).
if [[ "${FAKE_INTERRUPT:-}" == "$2" ]]; then
    kill -s INT 0
    sleep 1
fi
EOF
chmod +x "$WORK/bin/docker"

# run_target TARGET: runs it with the fake docker; sets STATUS.
run_target() {
    : >"$WORK/docker.log"
    PATH="$WORK/bin:$PATH" FAKE_DOCKER_LOG="$WORK/docker.log" \
        perl -e 'setpgrp(0, 0); exec @ARGV or exit 127' \
        make --no-print-directory -s -C "$REPO" -o validate-env "$1" >"$WORK/out" 2>&1 \
        && STATUS=0 || STATUS=$?
}

calls_are() {
    local expected
    expected="$(printf '%s\n' "$@")"
    [[ "$(cat "$WORK/docker.log")" == "$expected" ]] || {
        printf 'calls:\n%s\nexpected:\n%s\n' "$(cat "$WORK/docker.log")" "$expected"
        return 1
    }
}

# The rotation run, then the service recreated so it starts with the
# current .env (an updated BRIDGE_CERT_FINGERPRINT) and no argument.
rotation_recreates_mbsync_after_the_run() {
    run_target rotate-bridge-pin
    [[ "$STATUS" -eq 0 ]] || return 1
    calls_are "compose run --rm --no-deps mbsync rotate-bridge-pin" \
        "compose up -d --no-deps --force-recreate mbsync"
}

# A refused rotation leaves the service alone and fails.
refused_rotation_leaves_mbsync_alone() {
    FAKE_FAIL=run run_target rotate-bridge-pin
    [[ "$STATUS" -ne 0 ]] || return 1
    calls_are "compose run --rm --no-deps mbsync rotate-bridge-pin"
}

# A failed recreate fails the target.
failed_recreate_fails_the_rotation() {
    FAKE_FAIL=up run_target rotate-bridge-pin
    [[ "$STATUS" -ne 0 ]] || return 1
}

# Stop, the one forced pass, then the indexer started again with the
# current .env.
forced_deletion_stops_runs_and_starts_the_indexer() {
    run_target force-deletion
    [[ "$STATUS" -eq 0 ]] || return 1
    calls_are "compose stop indexer" \
        "compose run --rm --no-deps indexer python -m src.force_deletion" \
        "compose up -d --no-deps indexer"
}

# A failed pass still starts the indexer, and the target fails.
failed_pass_still_starts_the_indexer() {
    FAKE_FAIL=run run_target force-deletion
    [[ "$STATUS" -ne 0 ]] || return 1
    calls_are "compose stop indexer" \
        "compose run --rm --no-deps indexer python -m src.force_deletion" \
        "compose up -d --no-deps indexer"
}

# A pass that succeeds while the indexer cannot be started again fails
# the target: the indexer is left stopped.
failed_start_fails_the_target() {
    FAKE_FAIL=up run_target force-deletion
    [[ "$STATUS" -ne 0 ]] || return 1
    FAKE_FAIL="run up" run_target force-deletion
    [[ "$STATUS" -ne 0 ]] || return 1
}

# Ctrl-C during the pass still starts the indexer, and the target fails.
interrupted_pass_still_starts_the_indexer() {
    FAKE_INTERRUPT=run run_target force-deletion
    [[ "$STATUS" -ne 0 ]] || return 1
    calls_are "compose stop indexer" \
        "compose run --rm --no-deps indexer python -m src.force_deletion" \
        "compose up -d --no-deps indexer"
}

# The indexer could not be stopped: no pass runs beside it.
failed_stop_runs_no_pass() {
    FAKE_FAIL=stop run_target force-deletion
    [[ "$STATUS" -ne 0 ]] || return 1
    calls_are "compose stop indexer"
}

check() {
    local description="$1" rc=0
    shift
    set +e
    (set -e; "$@") >"$WORK/case" 2>&1
    rc=$?
    set -e
    if ((rc == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/case" "$WORK/out"
        FAILURES=$((FAILURES + 1))
    fi
}

check "rotate-bridge-pin recreates mbsync after the run" rotation_recreates_mbsync_after_the_run
check "a refused rotation leaves mbsync alone" refused_rotation_leaves_mbsync_alone
check "a failed recreate fails rotate-bridge-pin" failed_recreate_fails_the_rotation
check "force-deletion stops the indexer, runs one pass and starts it again" \
    forced_deletion_stops_runs_and_starts_the_indexer
check "a failed pass still starts the indexer and fails" failed_pass_still_starts_the_indexer
check "an indexer that cannot be started again fails force-deletion" failed_start_fails_the_target
check "an interrupted pass still starts the indexer and fails" interrupted_pass_still_starts_the_indexer
check "an indexer that cannot be stopped runs no pass" failed_stop_runs_no_pass

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
