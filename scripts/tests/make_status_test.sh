#!/bin/bash
set -Eeuo pipefail

# Tests for the Makefile `status` target (#733). A fake `docker` first on
# PATH stands in for the CLI and never contacts a daemon: `compose ps`
# prints a fixed table, `ps` reports whether mcp-server runs, and `exec`
# runs the target's real Python one-liner on the host against a stub
# `src.tools.system` whose helper returns $FAKE_MAILBOX_STATUS, or fails
# with a synthetic diagnostic when $FAKE_EXEC_FAIL is set.
#
# Run: bash scripts/tests/make_status_test.sh

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

mkdir -p "$WORK/bin" "$WORK/stub/src/tools"
touch "$WORK/stub/src/__init__.py" "$WORK/stub/src/tools/__init__.py"
cat >"$WORK/stub/src/tools/system.py" <<'EOF'
import json
import os


def get_mailbox_status():
    return json.loads(os.environ["FAKE_MAILBOX_STATUS"])
EOF

cat >"$WORK/bin/docker" <<'EOF'
#!/bin/bash
set -Eeuo pipefail
printf '%s\n' "$1" >>"$FAKE_DOCKER_LOG"
case "$1" in
compose)
    printf 'NAME SERVICE STATUS\nfake-mcp-server mcp-server Up\n'
    ;;
ps)
    if [[ "$FAKE_RUNNING" == 1 ]]; then
        printf '0123456789ab\n'
    fi
    ;;
exec)
    if [[ -n "${FAKE_EXEC_FAIL:-}" ]]; then
        printf '%s\n' "$FAKE_EXEC_FAIL" >&2
        exit 17
    fi
    # docker exec mcp-server python -c CODE
    [[ "$2" == mcp-server && "$3" == python && "$4" == -c ]]
    PYTHONPATH="$FAKE_STUB" exec python3 -c "$5"
    ;;
*)
    printf 'fake docker: unexpected command %s\n' "$1" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$WORK/bin/docker"

# run_status RUNNING [STATUS-JSON]: runs `make status` with the fake docker.
run_status() {
    : >"$WORK/docker.log"
    PATH="$WORK/bin:$PATH" FAKE_DOCKER_LOG="$WORK/docker.log" FAKE_STUB="$WORK/stub" \
        FAKE_RUNNING="$1" FAKE_MAILBOX_STATUS="${2:-}" \
        make --no-print-directory -s -C "$REPO" status >"$WORK/out" 2>&1 && STATUS=0 || STATUS=$?
}

shows_containers() {
    grep -F 'fake-mcp-server mcp-server Up' "$WORK/out" >/dev/null
}

current_index_succeeds() {
    run_status 1 '{"status": "ok", "current": true}'
    [[ "$STATUS" -eq 0 ]]
    shows_containers
    grep -F '"current": true' "$WORK/out" >/dev/null
}

not_current_index_still_succeeds() {
    run_status 1 '{"status": "ok", "current": false}'
    [[ "$STATUS" -eq 0 ]]
    grep -F '"current": false' "$WORK/out" >/dev/null
}

helper_error_fails() {
    run_status 1 '{"status": "error", "error": "Mailbox status error: ValueError"}'
    [[ "$STATUS" -ne 0 ]]
    shows_containers
    grep -F 'Mailbox status error: ValueError' "$WORK/out" >/dev/null
}

stopped_server_fails_with_a_clear_message() {
    run_status 0
    [[ "$STATUS" -ne 0 ]]
    shows_containers
    grep -F 'MCP server is not running' "$WORK/out" >/dev/null
    if grep -x exec "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

exec_failure_fails_and_shows_its_stderr() {
    FAKE_EXEC_FAIL='ModuleNotFoundError: synthetic-import-failure-4c1e' \
        run_status 1
    [[ "$STATUS" -ne 0 ]]
    grep -F 'synthetic-import-failure-4c1e' "$WORK/out" >/dev/null
    if grep -F 'not running' "$WORK/out" >/dev/null; then
        return 1
    fi
}

# Runs each case in a subshell with errexit on, outside any condition.
check() {
    local description="$1" status
    shift
    set +e
    (
        set -e
        "$@"
    ) >"$WORK/case-output" 2>&1
    status=$?
    set -e
    if ((status == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/case-output" "$WORK/out"
        FAILURES=$((FAILURES + 1))
    fi
}

check "a current index succeeds" current_index_succeeds
check "a readable index that is not current still succeeds" not_current_index_still_succeeds
check "a helper error fails" helper_error_fails
check "a stopped MCP server fails with a clear message" stopped_server_fails_with_a_clear_message
check "an exec failure fails and shows its stderr" exec_failure_fails_and_shows_its_stderr

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
