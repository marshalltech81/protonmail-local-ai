#!/bin/bash
set -Eeuo pipefail

# Tests for the module-download step in bridge/Dockerfile (#618).
#
# Each case extracts the RUN instruction that calls `go mod download` and
# runs it the way the builder stage does (bash -o pipefail -c, per its
# SHELL line), with `go` and `sleep` replaced by mock executables that
# record their calls. No Docker, network or Go toolchain is used.
#
# Run: bash bridge/tests/dockerfile_test.sh

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$TESTS_DIR/../Dockerfile"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0
readonly TESTS_DIR DOCKERFILE

# Same harness shape as entrypoint_test.sh: each case runs in a subshell
# with errexit on, and every [[ ]] / (( )) assertion ends in `|| return 1`
# for bash 3.2 (macOS /bin/bash).
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

# Prints the shell command of the one RUN instruction that mentions
# `go mod download`, with its line continuations joined.
module_step() {
    awk '
        /^RUN / { block = ""; in_run = 1 }
        in_run {
            line = $0
            continued = sub(/\\$/, "", line)
            block = block line
            if (!continued) {
                in_run = 0
                if (block ~ /go mod download/) { found++; print block }
            }
        }
        END { if (found != 1) exit 1 }
    ' "$DOCKERFILE" | sed '1s/^RUN //'
}

# Installs mocks: `go mod download` fails the first $2 calls, and
# `go mod verify` exits $3. Every go and sleep call is appended to $LOG.
setup() {
    BIN="$WORK/bin-$1"
    LOG="$WORK/log-$1"
    mkdir -p "$BIN"
    : > "$LOG"
    cat > "$BIN/go" <<EOF
#!/bin/bash
printf 'go %s\n' "\$*" >> "$LOG"
if [[ "\$*" == "mod download" ]]; then
    calls=\$(grep -c '^go mod download\$' "$LOG")
    ((calls > $2)) || exit 1
    exit 0
fi
if [[ "\$*" == "mod verify" ]]; then
    exit $3
fi
exit 2
EOF
    cat > "$BIN/sleep" <<EOF
#!/bin/bash
printf 'sleep %s\n' "\$*" >> "$LOG"
EOF
    chmod +x "$BIN/go" "$BIN/sleep"
}

run_step() {
    local step
    step="$(module_step)"
    PATH="$BIN:$PATH" bash -o pipefail -c "$step"
}

count() {
    grep -c -x -- "$1" "$LOG" || true
}

first_try_success_verifies_without_sleeping() {
    setup first 0 0
    run_step
    [[ "$(count 'go mod download')" == 1 ]] || return 1
    [[ "$(count 'go mod verify')" == 1 ]] || return 1
    ! grep -q '^sleep' "$LOG" || return 1
}

transient_failures_are_retried_then_verified() {
    setup transient 2 0
    run_step
    [[ "$(count 'go mod download')" == 3 ]] || return 1
    [[ "$(count 'go mod verify')" == 1 ]] || return 1
    [[ "$(grep -c '^sleep' "$LOG")" == 2 ]] || return 1
    # verify runs only after the successful download.
    [[ "$(tail -n 1 "$LOG")" == 'go mod verify' ]] || return 1
}

persistent_failure_fails_after_three_attempts() {
    setup persistent 99 0
    local rc=0
    run_step || rc=$?
    ((rc != 0)) || return 1
    [[ "$(count 'go mod download')" == 3 ]] || return 1
    [[ "$(count 'go mod verify')" == 0 ]] || return 1
}

verify_failure_fails_the_step_without_retry() {
    setup verify 0 1
    local rc=0
    run_step || rc=$?
    ((rc != 0)) || return 1
    [[ "$(count 'go mod download')" == 1 ]] || return 1
    [[ "$(count 'go mod verify')" == 1 ]] || return 1
}

check "a first-try download is verified without a retry (#618)" \
    first_try_success_verifies_without_sleeping
check "transient download failures are retried, then verified (#618)" \
    transient_failures_are_retried_then_verified
check "a persistent download failure fails after three attempts (#618)" \
    persistent_failure_fails_after_three_attempts
check "a go mod verify failure fails the step and is not retried (#618)" \
    verify_failure_fails_the_step_without_retry

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
