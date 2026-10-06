#!/bin/bash
set -Eeuo pipefail

# Tests for the Makefile `status` target (#733). A fake `docker` first on
# PATH stands in for the CLI and never contacts a daemon: `compose ps`
# prints a fixed table, `ps` reports whether mcp-server runs, and `exec`
# runs the target's real Python one-liner on the host against a stub
# `src.tools.system` whose helper returns $FAKE_MAILBOX_STATUS, or fails
# with a synthetic diagnostic when $FAKE_EXEC_FAIL is set. The privacy
# one-liner reads the *_MODE / *_BASE_URL variables each case sets, and
# `inspect` / `network inspect` report app-net as internal when
# $FAKE_INTERNAL is true (#768).
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
inspect)
    printf 'fake_app-net\n'
    ;;
network)
    [[ "$2" == inspect && "${*: -1}" == fake_app-net ]]
    printf '%s\n' "$FAKE_INTERNAL"
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
        FAKE_RUNNING="$1" FAKE_MAILBOX_STATUS="${2:-}" FAKE_INTERNAL="${FAKE_INTERNAL:-false}" \
        EMBED_MODE="${EMBED_MODE:-openai}" EMBED_BASE_URL="${EMBED_BASE_URL:-http://host.docker.internal:1234/v1}" \
        INFERENCE_MODE="${INFERENCE_MODE:-none}" INFERENCE_BASE_URL="${INFERENCE_BASE_URL:-}" \
        RERANK_MODE="${RERANK_MODE:-none}" RERANK_BASE_URL="${RERANK_BASE_URL:-}" \
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

shows_line() {
    grep -E "$1" "$WORK/out" >/dev/null
}

privacy_shows_local_and_disabled_layers() {
    run_status 1 '{"status": "ok", "current": true}'
    [[ "$STATUS" -eq 0 ]]
    shows_line '^=== Privacy ===$'
    shows_line '^  embed +openai +LOCAL \(host\.docker\.internal\)$'
    shows_line '^  inference +none +disabled$'
    shows_line '^  rerank +none +disabled$'
    shows_line 'No-egress overlay: not active'
    shows_line 'cloud-backed MCP client'
}

privacy_names_remote_hosts_and_sdk_defaults() {
    # Synthetic userinfo marker, not a credential.
    # pragma: allowlist nextline secret
    EMBED_BASE_URL='https://user-4c1e:pass-4c1e@embed.example.test:8443/v1?k=q-4c1e' \
        INFERENCE_MODE=anthropic INFERENCE_BASE_URL=' Default ' \
        RERANK_MODE=cohere RERANK_BASE_URL=default \
        run_status 1 '{"status": "ok", "current": true}'
    [[ "$STATUS" -eq 0 ]]
    shows_line '^  embed +openai +REMOTE \(embed\.example\.test\)$'
    shows_line '^  inference +anthropic +REMOTE \(api\.anthropic\.com\)$'
    shows_line '^  rerank +cohere +REMOTE \(api\.cohere\.com\)$'
    # Only the host name: never userinfo, port, path or query.
    if grep -F -- '4c1e' "$WORK/out" >/dev/null || grep -F ':8443' "$WORK/out" >/dev/null; then
        return 1
    fi
}

privacy_treats_every_local_host_as_local() {
    local host
    for host in 127.0.0.1 '[::1]' localhost host.docker.internal; do
        EMBED_BASE_URL="http://$host:8080/v1" run_status 1 '{"status": "ok", "current": true}'
        shows_line '^  embed +openai +LOCAL '
    done
    EMBED_MODE=OpenAI EMBED_BASE_URL=DEFAULT run_status 1 '{"status": "ok", "current": true}'
    shows_line '^  embed +openai +REMOTE \(api\.openai\.com\)$'
}

privacy_local_hosts_match_the_services() {
    local services make_hosts
    services=$(grep -h '^_HOST_LOCAL_HOSTS = ' "$REPO/indexer/src/main.py" "$REPO/mcp-server/src/main.py" |
        grep -oE '"[^"]+"' | tr -d '"' | sort -u)
    make_hosts=$(grep -F "local = {" "$REPO/Makefile" | grep -oE "'[^']+'" | tr -d "'" | sort -u)
    [[ -n "$services" && "$services" == "$make_hosts" ]]
}

privacy_reports_the_no_egress_overlay() {
    FAKE_INTERNAL=true run_status 1 '{"status": "ok", "current": true}'
    [[ "$STATUS" -eq 0 ]]
    shows_line 'No-egress overlay: active'
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
check "privacy shows local and disabled layers" privacy_shows_local_and_disabled_layers
check "privacy names remote hosts and SDK defaults, nothing else" privacy_names_remote_hosts_and_sdk_defaults
check "privacy treats every host-local name as local" privacy_treats_every_local_host_as_local
check "privacy local hosts match both services" privacy_local_hosts_match_the_services
check "privacy reports the no-egress overlay" privacy_reports_the_no_egress_overlay

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
