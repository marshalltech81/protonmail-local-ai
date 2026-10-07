#!/bin/bash
set -Eeuo pipefail

# Checks the Trivy steps in .github/workflows/security.yml: each option
# (pinned version, severity, exit code, skip-dirs, offline mode) has one
# value across the Trivy steps, and the misconfiguration scan runs
# offline so it never resolves indexer/java/pom.xml from Maven Central
# (#1047). Then runs `make trivy` against a fake `trivy` that records
# each call and answers `--version` with $FAKE_TRIVY_VERSION, and checks
# the target runs the workflow's three scans with the workflow's flags
# (#1017), then the image gates of .github/workflows/docker.yml with
# their flags (#1065), runs every scan when one fails, warns on a
# version other than the pinned one and prints the install hint when
# trivy is missing. `make trivy-images` runs the image gates alone
# against a fake `docker` and fails, naming the image, when one is not
# built. No Trivy install, no Docker and no network needed.
#
# Run: bash scripts/tests/trivy_flags_test.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKFLOW="$ROOT/.github/workflows/security.yml"
DOCKER_WORKFLOW="$ROOT/.github/workflows/docker.yml"
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

# The one value a `key: value` line has across a workflow (quotes
# stripped; security.yml unless a file is given); exits when the key is
# missing or has several values, since every later comparison would be
# meaningless.
workflow_value() {
    local key="$1" file="${2:-$WORKFLOW}" values
    values="$(sed -n "s/^ *$key: *//p" "$file" | tr -d '"' | sort -u)"
    if [[ -z "$values" ]]; then
        printf 'FAIL: no %s line in %s\n' "$key" "${file##*/}" >&2
        exit 1
    fi
    if [[ "$(wc -l <<<"$values")" -ne 1 ]]; then
        printf 'FAIL: %s has several values in %s: %s\n' \
            "$key" "${file##*/}" "$(tr '\n' ' ' <<<"$values")" >&2
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

# --- docker.yml ---------------------------------------------------------

# The image gates (#977): the trivy-action steps with `scan-type: image`
# whose exit code can fail the job; the report steps (exit code 0) are
# not gates. One line per gate, in order: "<scanners> <image-ref>
# <exit-code> <ignore-unfixed true|false> <severity or ->". The images
# are named after the workflow's COMPOSE_PROJECT_NAME, so the service
# each gate scans is the image-ref without that prefix.
DOCKER_VERSION="$(workflow_value version "$DOCKER_WORKFLOW")"
PROJECT="$(workflow_value COMPOSE_PROJECT_NAME "$DOCKER_WORKFLOW")"
ok=false; [[ "$DOCKER_VERSION" == "$VERSION" ]] && ok=true
check "docker.yml pins the Trivy version security.yml pins" "$ok"

GATES=()
while IFS= read -r line; do
    GATES+=("$line")
done < <(awk '
    function flush() {
        if (type == "image" && code != "0" && code != "")
            print scanners, ref, code, unfixed, severity
        type = ""; ref = ""; scanners = ""; code = ""; unfixed = "false"; severity = "-"
    }
    /^ *- name:/ { flush() }
    /^ *scan-type:/ { type = $2 }
    /^ *image-ref:/ { ref = $2 }
    /^ *scanners:/ { scanners = $2 }
    /^ *exit-code:/ { gsub(/"/, "", $2); code = $2 }
    /^ *ignore-unfixed:/ { unfixed = $2 }
    /^ *severity:/ { severity = $2 }
    END { flush() }
' "$DOCKER_WORKFLOW")
if [[ "${#GATES[@]}" -lt 2 ]]; then
    printf 'FAIL: found %d image gates in docker.yml\n' "${#GATES[@]}" >&2
    exit 1
fi
printf 'docker.yml: trivy %s, project %s, %d image gates: %s\n' \
    "$DOCKER_VERSION" "$PROJECT" "${#GATES[@]}" "$(printf '[%s] ' "${GATES[@]}")"
GATE_SERVICES=()
for gate in "${GATES[@]}"; do
    read -r _ ref _ _ _ <<<"$gate"
    ok=false; [[ "$ref" == "$PROJECT-"?* ]] && ok=true
    check "gate image $ref is named after the Compose project" "$ok"
    GATE_SERVICES+=("${ref#"$PROJECT"-}")
done

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

# A fake docker, first on PATH for make: `compose config --images`
# lists the gated services' images under the project `fakeproj`, in
# the reverse of docker.yml's order, so a hard-coded image name or a
# name built from a parsed project name fails the test; `image
# inspect` fails for the images listed in $FAKE_DOCKER_MISSING.
cat >"$WORK/bin/docker" <<'EOF'
#!/bin/bash
set -Eeuo pipefail
case "$1 $2 ${3:-}" in
    "compose config --images") printf '%s\n' "$FAKE_DOCKER_IMAGES" ;;
    "image inspect "*) [[ " ${FAKE_DOCKER_MISSING:-} " == *" $3 "* ]] && exit 1; exit 0 ;;
    *) printf 'fake docker: unexpected call: %s\n' "$*" >&2; exit 2 ;;
esac
EOF
chmod 755 "$WORK/bin/docker"
FAKE_DOCKER_IMAGES=""
for ((i = ${#GATE_SERVICES[@]} - 1; i >= 0; i--)); do
    FAKE_DOCKER_IMAGES+="${FAKE_DOCKER_IMAGES:+$'\n'}fakeproj-${GATE_SERVICES[$i]}"
done
FAKE_DOCKER_MISSING=""

# run_make VERSION EXIT [TARGET]: runs `make TARGET` (trivy by default)
# against the fake trivy, which reports VERSION and exits EXIT from
# every scan, and the fake docker; records the calls in $WORK/calls,
# the output in $WORK/out and $WORK/err, the status in $MAKE_STATUS.
run_make() {
    : >"$WORK/calls"
    MAKE_STATUS=0
    PATH="$WORK/bin:$PATH" FAKE_DOCKER_IMAGES="$FAKE_DOCKER_IMAGES" FAKE_DOCKER_MISSING="$FAKE_DOCKER_MISSING" \
        FAKE_TRIVY_LOG="$WORK/calls" FAKE_TRIVY_VERSION="${1#v}" FAKE_TRIVY_EXIT="$2" \
        make -s -C "$ROOT" "${3:-trivy}" TRIVY="$WORK/bin/trivy" \
        >"$WORK/out" 2>"$WORK/err" || MAKE_STATUS=$?
}

# check_image_calls FIRST: the fake trivy's calls from line FIRST on are
# docker.yml's image gates, one per gate in the order docker compose
# lists the images (not the workflow's), each on the image docker
# compose names for its service, with the gate's flags, online whatever
# the environment says (as the fs scans).
check_image_calls() {
    local i gate scanners ref code unfixed severity service call
    for i in "${!GATES[@]}"; do
        gate="${GATES[$i]}"
        read -r scanners ref code unfixed severity <<<"$gate"
        service="${GATE_SERVICES[$i]}"
        call="$(tail -n "+$1" "$WORK/calls" | grep -- " fakeproj-$service\$" || true)"
        ok=false; [[ "$(grep -c . <<<"$call")" -eq 1 ]] && ok=true
        check "the image of $service, named by docker compose, is scanned once" "$ok"
        ok=false; [[ "$call" == "image --scanners $scanners "* ]] && ok=true
        check "the $service image scan is the gate's $scanners scan" "$ok"
        ok=false; [[ "$call" == *" --severity $severity "* ]] && ok=true
        check "the $service image scan uses the gate's severity" "$ok"
        ok=false; [[ "$call" == *" --exit-code $code "* ]] && ok=true
        check "the $service image scan uses the gate's exit code" "$ok"
        if [[ "$unfixed" == true ]]; then
            ok=false; [[ "$call" == *" --ignore-unfixed "* ]] && ok=true
            check "the $service image scan ignores unfixed findings as the gate does" "$ok"
        else
            ok=false; [[ "$call" != *"--ignore-unfixed"* ]] && ok=true
            check "the $service image scan gates unfixed findings as the gate does" "$ok"
        fi
        ok=false; [[ "$call" == *" --offline-scan=false "* ]] && ok=true
        check "the $service image scan runs online whatever the environment says" "$ok"
    done
    ok=false; [[ "$(tail -n "+$1" "$WORK/calls" | grep -vc '^image ')" -eq 0 ]] && ok=true
    check "no fs scan runs after the image gates" "$ok"
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
TOTAL=$((${#EXPECTED[@]} + ${#GATES[@]}))
run_make "$VERSION" 0
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy succeeds when every scan is clean" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "$TOTAL" ]] && ok=true
check "make trivy runs one scan per Trivy step of security.yml and one per image gate of docker.yml" "$ok"
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
check_image_calls $((${#EXPECTED[@]} + 1))
ok=false; [[ ! -s "$WORK/err" ]] && ok=true
check "no warning for the pinned version" "$ok"

# A failing scan: the later scans still run, and the target fails.
run_make "$VERSION" 1
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy fails when a scan finds something" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "$TOTAL" ]] && ok=true
check "every scan still runs after a failing one" "$ok"

# Another version: a warning naming both versions, scans still run.
run_make v0.0.1 0
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy still succeeds with another version" "$ok"
ok=false; grep -q "0.0.1.*$VERSION" "$WORK/err" && ok=true
check "another version is warned about, naming the pinned one" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "$TOTAL" ]] && ok=true
check "the scans run with another version" "$ok"

# No trivy: the install hint, no scan.
MAKE_STATUS=0
make -s -C "$ROOT" trivy TRIVY="$WORK/missing/trivy" >"$WORK/out" 2>"$WORK/err" || MAKE_STATUS=$?
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy fails when trivy is missing" "$ok"
ok=false; grep -q "trivy not found.*$VERSION" "$WORK/err" && ok=true
check "the install hint names the pinned version" "$ok"

# --- make trivy-images --------------------------------------------------

# The image gates alone, with the same preflight.
run_make "$VERSION" 0 trivy-images
ok=false; [[ "$MAKE_STATUS" -eq 0 ]] && ok=true
check "make trivy-images succeeds when every image is clean" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "${#GATES[@]}" ]] && ok=true
check "make trivy-images runs one scan per image gate and no fs scan" "$ok"
check_image_calls 1
ok=false; [[ ! -s "$WORK/err" ]] && ok=true
check "make trivy-images prints no warning for the pinned version" "$ok"

run_make "$VERSION" 1 trivy-images
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy-images fails when an image gate finds something" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "${#GATES[@]}" ]] && ok=true
check "every image gate still runs after a failing one" "$ok"

MAKE_STATUS=0
make -s -C "$ROOT" trivy-images TRIVY="$WORK/missing/trivy" >"$WORK/out" 2>"$WORK/err" || MAKE_STATUS=$?
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy-images fails when trivy is missing" "$ok"
ok=false; grep -q "trivy not found.*$VERSION" "$WORK/err" && ok=true
check "make trivy-images prints the install hint" "$ok"

# An image that is not built: a fixed message naming it and make build,
# no image scan; the fs scans of make trivy still run and it fails.
MISSING="fakeproj-${GATE_SERVICES[1]}"
FAKE_DOCKER_MISSING="$MISSING"
run_make "$VERSION" 0 trivy-images
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy-images fails when an image is not built" "$ok"
ok=false; grep -q "image $MISSING is not built.*make build" "$WORK/err" && ok=true
check "the message names the missing image and make build" "$ok"
ok=false; [[ ! -s "$WORK/calls" ]] && ok=true
check "no image is scanned while one is missing" "$ok"
run_make "$VERSION" 0
ok=false; [[ "$MAKE_STATUS" -ne 0 ]] && ok=true
check "make trivy fails when an image is not built" "$ok"
ok=false; [[ "$(wc -l <"$WORK/calls")" -eq "${#EXPECTED[@]}" ]] && ok=true
check "make trivy still runs the fs scans when an image is not built" "$ok"
FAKE_DOCKER_MISSING=""

if ((FAILURES > 0)); then
    printf '%d check(s) failed.\n' "$FAILURES" >&2
    exit 1
fi
printf 'All Trivy flag checks passed.\n'
