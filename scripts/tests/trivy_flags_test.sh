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

# The scans the workflow runs, in order, one line per trivy-action step:
# "<scanners> <scan-ref> <skip-dirs or -> <offline true|false>". The
# expected `make trivy` calls derive from this list, so a step added,
# removed or retargeted in the workflow fails the test until the
# target follows.
EXPECTED=()
while IFS= read -r line; do
    EXPECTED+=("$line")
done < <(awk '
    /^ *- name:/ { if (ref != "") print scanners, ref, skip, offline
                   ref = ""; scanners = ""; skip = "-"; offline = "false" }
    /^ *scan-ref:/ { ref = $2 }
    /^ *scanners:/ { scanners = $2 }
    /^ *skip-dirs:/ { skip = $2 }
    /^ *TRIVY_OFFLINE_SCAN:/ { gsub(/"/, "", $2); offline = $2 }
    END { if (ref != "") print scanners, ref, skip, offline }
' "$WORKFLOW")
if [[ "${#EXPECTED[@]}" -lt 2 ]]; then
    printf 'FAIL: found %d Trivy steps in security.yml\n' "${#EXPECTED[@]}" >&2
    exit 1
fi
printf 'security.yml: %d Trivy scans: %s\n' "${#EXPECTED[@]}" "$(printf '[%s] ' "${EXPECTED[@]}")"

# The pinned version and clean scans: the workflow's scans, in its
# order, with its flags, and no warning. Each dependency scan passes
# --offline-scan=false explicitly so an exported TRIVY_OFFLINE_SCAN
# cannot make it skip dependencies silently.
run_make "$VERSION" 0
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy succeeds when every scan is clean" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "${#EXPECTED[@]}" ]] && ok=true
check "make trivy runs one scan per Trivy step of the workflow" "$ok"
for i in "${!EXPECTED[@]}"; do
    read -r scanners ref skip offline <<<"${EXPECTED[$i]}"
    call="$(sed -n "$((i + 1))p" "$WORK/calls")"
    ok=false; [[ "$call" == "fs --scanners $scanners "*" $ref" ]] && ok=true
    check "scan $((i + 1)) is the $scanners scan of $ref" "$ok"
    ok=false; [[ "$call" == *" --severity $SEVERITY "* ]] && ok=true
    check "scan $((i + 1)) uses the workflow's severity" "$ok"
    ok=false; [[ "$call" == *" --exit-code $EXIT_CODE "* ]] && ok=true
    check "scan $((i + 1)) uses the workflow's exit code" "$ok"
    if [[ "$skip" == - ]]; then
        ok=false; [[ "$call" != *"--skip-dirs"* ]] && ok=true
        check "scan $((i + 1)) skips no directory" "$ok"
    else
        ok=false; [[ "$call" == *" --skip-dirs $skip "* ]] && ok=true
        check "scan $((i + 1)) uses the workflow's skip-dirs" "$ok"
    fi
    if [[ "$offline" == true ]]; then
        ok=false; [[ "$call" == *" --offline-scan "* ]] && ok=true
        check "scan $((i + 1)) runs offline" "$ok"
    else
        ok=false; [[ "$call" == *" --offline-scan=false "* ]] && ok=true
        check "scan $((i + 1)) runs online whatever the environment says" "$ok"
    fi
done
ok=false; [[ ! -s "$WORK/err" ]] && ok=true
check "no warning for the pinned version" "$ok"

# A failing scan: the later scans still run, and the target fails.
run_make "$VERSION" 1
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy fails when a scan finds something" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "${#EXPECTED[@]}" ]] && ok=true
check "every scan still runs after a failing one" "$ok"

# Another version: a warning naming both versions, scans still run.
run_make v0.0.1 0
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy still succeeds with another version" "$ok"
ok=false; grep -q "0.0.1.*$VERSION" "$WORK/err" && ok=true
check "another version is warned about, naming the pinned one" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "${#EXPECTED[@]}" ]] && ok=true
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
