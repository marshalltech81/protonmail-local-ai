#!/bin/bash
set -Eeuo pipefail

# Tests for the pass/fail decision in scripts/bridge-smoke.sh.
#
# Each case runs the real script with a stub `docker` first on PATH. The
# stub succeeds silently for every call except the end-to-end AutoUpdate
# run (the one that mounts a tmpfs /data), where it prints STUB_OUTPUT and
# exits with STUB_STATUS. No image is built and no container is started;
# the in-container steps are exercised by `make bridge-smoke` itself.
#
# Run: bash scripts/tests/bridge_smoke_test.sh

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bridge-smoke.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

mkdir -p "$WORK/bin"
cat >"$WORK/bin/docker" <<'STUB'
#!/bin/bash
set -Eeuo pipefail
for arg in "$@"; do
    if [[ "$arg" == "/data:uid=1000,gid=1000,mode=700" ]]; then
        printf '%s\n' "$STUB_OUTPUT"
        exit "$STUB_STATUS"
    fi
done
STUB
chmod +x "$WORK/bin/docker"

# Run the smoke script against the stub; keep its stdout, stderr and status.
run_smoke() {
    STUB_OUTPUT="$1" STUB_STATUS="$2" PATH="$WORK/bin:$PATH" \
        bash "$SCRIPT" >"$WORK/stdout" 2>"$WORK/stderr" && SMOKE_STATUS=0 || SMOKE_STATUS=$?
}

# Each case runs in a subshell with errexit on and outside any `if`, so a
# failed assertion ends the case instead of being ignored.
check() {
    local description="$1" status
    shift
    set +e
    (
        set -e
        "$@"
    ) >"$WORK/output" 2>&1
    status=$?
    set -e
    if ((status == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/output"
        FAILURES=$((FAILURES + 1))
    fi
}

show() {
    printf -- '--- stdout ---\n'
    cat "$WORK/stdout"
    printf -- '--- stderr ---\n'
    cat "$WORK/stderr"
}

# A real Bridge log line carrying the marker.
readonly MARKER='time="2026-01-01 00:00:00.000" level="info" msg="Vault loaded" autoUpdate="false"'

passes() {
    show
    [[ "$SMOKE_STATUS" -eq 0 ]]
    grep -F 'Proton Bridge smoke checks passed.' "$WORK/stdout"
}

fails_with() {
    show
    [[ "$SMOKE_STATUS" -eq 1 ]]
    grep -F "$1" "$WORK/stderr"
    grep -F -- '--- captured output (first 60 lines) ---' "$WORK/stderr"
    if grep -F 'smoke checks passed' "$WORK/stdout"; then
        return 1
    fi
}

# The status lines the in-container step prints after the Bridge log.
CLEAN_EXIT=$'SMOKE_ENTRYPOINT_EXIT=0\nSMOKE_HARNESS_SIGNAL=none'
readonly CLEAN_EXIT

# Bridge sees EOF on stdin and exits 0 on its own after the marker.
marker_then_clean_exit_passes() {
    run_smoke "$MARKER"$'\n'"$CLEAN_EXIT" 0
    passes
}

# Bridge is still running at the marker and the check stops it.
marker_then_intended_stop_passes() {
    run_smoke "$MARKER"$'\nSMOKE_ENTRYPOINT_EXIT=143\nSMOKE_HARNESS_SIGNAL=term' 0  # pragma: allowlist secret
    passes
}

marker_then_intended_kill_passes() {
    run_smoke "$MARKER"$'\nSMOKE_ENTRYPOINT_EXIT=137\nSMOKE_HARNESS_SIGNAL=kill' 0
    passes
}

# The check's TERM landed but its KILL did not: a 137 then came from
# someone else (OOM, an external kill) during the grace period.
marker_then_external_kill_after_term_fails() {
    run_smoke "$MARKER"$'\nSMOKE_ENTRYPOINT_EXIT=137\nSMOKE_HARNESS_SIGNAL=term' 0  # pragma: allowlist secret
    fails_with 'Bridge did not exit cleanly'
}

# A signal status the check did not send (OOM, an external kill) is a
# failure, not the intended stop.
marker_then_external_kill_fails() {
    run_smoke "$MARKER"$'\nSMOKE_ENTRYPOINT_EXIT=143\nSMOKE_HARNESS_SIGNAL=none' 0
    fails_with 'Bridge did not exit cleanly'
}

# A panic written only to the entrypoint stream is shown even when the
# Bridge log alone exceeds the head limit.
marker_then_entrypoint_panic_is_printed() {
    local log
    log="$MARKER"$'\n'"$(printf 'time="2026-01-01 00:00:00.001" level="debug" msg="filler %s"\n' {1..70})"
    run_smoke "$log"$'\nSMOKE_ENTRYPOINT_EXIT=2\nSMOKE_HARNESS_SIGNAL=none\n--- entrypoint output ---\npanic: SYNTHETIC_PANIC' 0
    fails_with 'Bridge did not exit cleanly'
    grep -F 'panic: SYNTHETIC_PANIC' "$WORK/stderr"
    if grep -F 'filler 61' "$WORK/stderr"; then
        return 1
    fi
}

# #268: the issue's reproduction, a marker followed by a failed invocation.
marker_then_failed_invocation_fails() {
    run_smoke "$MARKER"$'\nSYNTHETIC_FATAL_AFTER_VAULT' 42
    fails_with 'exited with status 42'
}

marker_then_entrypoint_failure_fails() {
    run_smoke "$MARKER"$'\nSMOKE_ENTRYPOINT_EXIT=1\nSMOKE_HARNESS_SIGNAL=none' 0
    fails_with 'Bridge did not exit cleanly'
}

marker_then_fatal_log_line_fails() {
    run_smoke "$MARKER"$'\ntime="2026-01-01 00:00:00.001" level="fatal" msg="SYNTHETIC"\n'"$CLEAN_EXIT" 0
    fails_with 'fatal or panic'
}

# Without the status line the in-container check did not finish.
marker_without_entrypoint_status_fails() {
    run_smoke "$MARKER" 0
    fails_with 'Bridge did not exit cleanly'
}

# #269: the diagnostics header must not be parsed as a printf option.
missing_marker_fails_and_prints_the_captured_output() {
    run_smoke $'NO_BRIDGE_LOG_WRITTEN\nSYNTHETIC_GPG_FAILURE' 0
    show
    [[ "$SMOKE_STATUS" -eq 1 ]]
    grep -F 'AutoUpdate marker not found' "$WORK/stderr"
    grep -F -- '--- captured output (first 60 lines) ---' "$WORK/stderr"
    grep -F 'SYNTHETIC_GPG_FAILURE' "$WORK/stderr"
    if grep -F 'invalid option' "$WORK/stderr"; then
        return 1
    fi
}

check "the marker then a clean Bridge exit passes" marker_then_clean_exit_passes
check "the marker then the intended stop passes" marker_then_intended_stop_passes
check "the marker then the intended kill passes" marker_then_intended_kill_passes
check "the marker then an external kill fails" marker_then_external_kill_fails
check "an external kill after the check's TERM fails" marker_then_external_kill_after_term_fails
check "a panic in the entrypoint output is printed" marker_then_entrypoint_panic_is_printed
check "the marker then a failed invocation fails" marker_then_failed_invocation_fails
check "the marker then a failed entrypoint fails" marker_then_entrypoint_failure_fails
check "the marker then a fatal log line fails" marker_then_fatal_log_line_fails
check "the marker without an entrypoint status fails" marker_without_entrypoint_status_fails
check "a missing marker fails and prints the captured output" \
    missing_marker_fails_and_prints_the_captured_output

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
