#!/bin/bash
set -Eeuo pipefail

# Tests for scripts/mcp-auth-headers.sh, Claude Code's headersHelper for
# the MCP bearer token. Each case copies the script into a fresh
# temporary root with a synthetic token file.
#
# Run: bash scripts/tests/mcp_auth_headers_test.sh

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/mcp-auth-headers.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# setup [TOKEN-FILE-CONTENTS]: no argument leaves the token file absent.
setup() {
    ROOT="$(mktemp -d "$WORK/root.XXXXXX")"
    mkdir -p "$ROOT/scripts" "$ROOT/.secrets"
    cp "$SCRIPT" "$ROOT/scripts/mcp-auth-headers.sh"
    if (($# > 0)); then
        printf '%s' "$1" >"$ROOT/.secrets/mcp_auth_token.txt"
    fi
}

run_helper() {
    "$BASH" "$ROOT/scripts/mcp-auth-headers.sh" >"$WORK/out" 2>"$WORK/err" && STATUS=0 || STATUS=$?
}

prints_the_header_as_json() {
    setup $'synthetic-token-1f2e\n'
    run_helper
    [[ "$STATUS" -eq 0 ]]
    [[ "$(<"$WORK/out")" == '{"Authorization": "Bearer synthetic-token-1f2e"}' ]]
}

strips_surrounding_whitespace() {
    setup $'  synthetic-token-1f2e \t\n\n'
    run_helper
    [[ "$(<"$WORK/out")" == '{"Authorization": "Bearer synthetic-token-1f2e"}' ]]
}

missing_token_file_fails() {
    setup
    run_helper
    [[ "$STATUS" -eq 1 ]]
    grep -F 'missing or empty' "$WORK/err" >/dev/null
    [[ ! -s "$WORK/out" ]]
}

empty_token_fails() {
    setup $' \n'
    run_helper
    [[ "$STATUS" -eq 1 ]]
    grep -F 'missing or empty' "$WORK/err" >/dev/null
}

json_breaking_token_fails_without_echoing_it() {
    setup 'synthetic"marker-9b7c'
    run_helper
    [[ "$STATUS" -eq 1 ]]
    [[ ! -s "$WORK/out" ]]
    ! grep -F 'marker-9b7c' "$WORK/err" >/dev/null
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
        sed 's/^/     /' "$WORK/case-output"
        FAILURES=$((FAILURES + 1))
    fi
}

check "prints the header as JSON" prints_the_header_as_json
check "strips surrounding whitespace" strips_surrounding_whitespace
check "a missing token file fails" missing_token_file_fails
check "an empty token fails" empty_token_fails
check "a token that would break the JSON fails without echoing it" \
    json_breaking_token_fails_without_echoing_it

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
