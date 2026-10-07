#!/bin/bash
set -Eeuo pipefail

# Checks the Trivy steps in .github/workflows/security.yml: each option
# (pinned version, severity, exit code, skip-dirs, offline mode) has one
# value across the Trivy steps, and the misconfiguration scan runs
# offline so it never resolves indexer/java/pom.xml from Maven Central
# (#1047). Then runs `make trivy` against a fake `trivy` that records
# each call and answers `--version` with $FAKE_TRIVY_VERSION, and checks
# the target runs the workflow's three scans with the workflow's flags
# (#1017), runs every scan when one fails, warns on a version other
# than the pinned one and prints the install hint when trivy is
# missing. No Trivy install and no network needed.
#
# Run: bash scripts/tests/trivy_flags_test.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKFLOW="$ROOT/.github/workflows/security.yml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
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
SKIP_DIRS="$(workflow_value skip-dirs)"
OFFLINE="$(workflow_value TRIVY_OFFLINE_SCAN)"
printf 'security.yml: trivy %s, severity %s, exit-code %s, skip-dirs %s\n' \
    "$VERSION" "$SEVERITY" "$EXIT_CODE" "$SKIP_DIRS"

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
ok=false; [[ ",$SKIP_DIRS," == *,.uv-cache,* ]] && ok=true
check "the misconfiguration scan skips .uv-cache" "$ok"

# --- make trivy ---------------------------------------------------------

mkdir "$WORK/bin"
cat >"$WORK/bin/trivy" <<'EOF'
#!/bin/bash
set -Eeuo pipefail
if [[ "$1" == --version ]]; then
    printf 'Version: %s\nVulnerability DB:\n  Version: 2\n' "$FAKE_TRIVY_VERSION"
    exit 0
fi
printf '%s\n' "$*" >>"$FAKE_TRIVY_LOG"
exit "$FAKE_TRIVY_EXIT"
EOF
chmod 755 "$WORK/bin/trivy"

# run_make VERSION EXIT: runs `make trivy` against the fake trivy, which
# reports VERSION and exits EXIT from every scan; records the calls in
# $WORK/calls, the output in $WORK/out and $WORK/err, the status in
# $MAKE_STATUS.
run_make() {
    : >"$WORK/calls"
    MAKE_STATUS=0
    FAKE_TRIVY_LOG="$WORK/calls" FAKE_TRIVY_VERSION="${1#v}" FAKE_TRIVY_EXIT="$2" \
        make -s -C "$ROOT" trivy TRIVY="$WORK/bin/trivy" \
        >"$WORK/out" 2>"$WORK/err" || MAKE_STATUS=$?
}

# The pinned version and clean scans: the three scans of the workflow,
# with its flags, and no warning.
run_make "$VERSION" 0
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy succeeds when every scan is clean" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq 3 ]] && ok=true
check "make trivy runs three scans" "$ok"
ok=false; [[ "$(sed -n 1p "$WORK/calls")" == "fs --scanners vuln "*" indexer" ]] && ok=true
check "the first scan is the vuln scan of indexer" "$ok"
ok=false; [[ "$(sed -n 2p "$WORK/calls")" == "fs --scanners vuln "*" mcp-server" ]] && ok=true
check "the second scan is the vuln scan of mcp-server" "$ok"
ok=false; [[ "$(sed -n 3p "$WORK/calls")" == "fs --scanners misconfig "*" ." ]] && ok=true
check "the third scan is the misconfig scan of the repository" "$ok"
ok=false; [[ "$(grep -c -- "--severity $SEVERITY " "$WORK/calls")" -eq 3 ]] && ok=true
check "every scan uses the workflow's severity" "$ok"
ok=false; [[ "$(grep -c -- "--exit-code $EXIT_CODE " "$WORK/calls")" -eq 3 ]] && ok=true
check "every scan uses the workflow's exit code" "$ok"
ok=false; [[ "$(sed -n 3p "$WORK/calls")" == *" --skip-dirs $SKIP_DIRS "* ]] && ok=true
check "the misconfig scan uses the workflow's skip-dirs" "$ok"
ok=false; [[ "$(sed -n 3p "$WORK/calls")" == *" --offline-scan "* ]] && ok=true
check "the misconfig scan runs offline" "$ok"
ok=false; [[ "$(grep -c -- "--skip-dirs\|--offline-scan" "$WORK/calls")" -eq 1 ]] && ok=true
check "the vuln scans use neither skip-dirs nor offline mode" "$ok"
ok=false; [[ ! -s "$WORK/err" ]] && ok=true
check "no warning for the pinned version" "$ok"

# A failing scan: the later scans still run, and the target fails.
run_make "$VERSION" 1
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy fails when a scan finds something" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq 3 ]] && ok=true
check "every scan still runs after a failing one" "$ok"

# Another version: a warning naming both versions, scans still run.
run_make v0.0.1 0
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy still succeeds with another version" "$ok"
ok=false; grep -q "0.0.1.*$VERSION" "$WORK/err" && ok=true
check "another version is warned about, naming the pinned one" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq 3 ]] && ok=true
check "the scans run with another version" "$ok"

# No trivy: the install hint, no scan.
MAKE_STATUS=0
make -s -C "$ROOT" trivy TRIVY="$WORK/missing/trivy" >"$WORK/out" 2>"$WORK/err" || MAKE_STATUS=$?
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy fails when trivy is missing" "$ok"
ok=false; grep -q "trivy not found.*$VERSION" "$WORK/err" && ok=true
check "the install hint names the pinned version" "$ok"

if ((FAILURES > 0)); then
    printf '%d check(s) failed.\n' "$FAILURES" >&2
    exit 1
fi
printf 'All Trivy flag checks passed.\n'
