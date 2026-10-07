#!/bin/bash
set -Eeuo pipefail

# Checks the Trivy steps in .github/workflows/security.yml: each option
# (pinned version, severity, exit code, skip-dirs, offline mode) has one
# value across the Trivy steps, and the misconfiguration scan runs
# offline so it never resolves indexer/java/pom.xml from Maven Central
# (#1047).
#
# Run: bash scripts/tests/trivy_flags_test.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKFLOW="$ROOT/.github/workflows/security.yml"
FAILURES=0

# check DESCRIPTION OK: records one check's result.
check() {
    if [[ "$2" == true ]]; then
        printf 'ok: %s\n' "$1"
    else
        printf 'FAIL: %s\n' "$1" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

# The one value a `key: value` line has across the workflow (quotes
# stripped); exits when the key is missing or has several values, since
# every later comparison would be meaningless.
workflow_value() {
    local key="$1" values
    values="$(sed -n "s/^ *$key: *//p" "$WORKFLOW" | tr -d '"' | sort -u)"
    if [[ -z "$values" ]]; then
        printf 'FAIL: no %s line in security.yml\n' "$key" >&2
        exit 1
    fi
    if [[ "$(wc -l <<<"$values")" -ne 1 ]]; then
        printf 'FAIL: %s has several values in security.yml: %s\n' \
            "$key" "$(tr '\n' ' ' <<<"$values")" >&2
        exit 1
    fi
    printf '%s\n' "$values"
}

VERSION="$(workflow_value version)"
SEVERITY="$(workflow_value severity)"
EXIT_CODE="$(workflow_value exit-code)"
OFFLINE="$(workflow_value TRIVY_OFFLINE_SCAN)"
printf 'security.yml: trivy %s, severity %s, exit-code %s\n' \
    "$VERSION" "$SEVERITY" "$EXIT_CODE"

# The misconfiguration step: from its `scanners: misconfig` line to the
# next step, so its `env:` block is included.
MISCONFIG_STEP="$(awk '/^ *scanners: misconfig$/ {found = 1} found && /^ *- name:/ {exit} found' "$WORKFLOW")"

ok=false; [[ "$OFFLINE" == true ]] && ok=true
check "the misconfiguration scan runs offline" "$ok"
ok=false; grep -q '^ *TRIVY_OFFLINE_SCAN:' <<<"$MISCONFIG_STEP" && ok=true
check "offline mode is set on the misconfiguration scan" "$ok"
ok=false; [[ "$(grep -c '^ *TRIVY_OFFLINE_SCAN:' "$WORKFLOW")" -eq 1 ]] && ok=true
check "offline mode is set on no other scan" "$ok"
ok=false; [[ "$(grep -c '^ *skip-dirs:' "$WORKFLOW")" -eq 1 ]] && ok=true
check "skip-dirs is set on the misconfiguration scan only" "$ok"

if ((FAILURES > 0)); then
    printf '%d check(s) failed.\n' "$FAILURES" >&2
    exit 1
fi
printf 'All Trivy flag checks passed.\n'
