#!/bin/bash
set -Eeuo pipefail

# Scans the Compose file the operator configuration translator generates
# (compose.accounts.yml, #1608) with the rules in .semgrep/compose.yaml.
# The translator writes it to .generated/, which is gitignored, and the
# repository scan reads tracked files only, so this generates it from
# each synthetic configuration of scripts/config/tests/synthetic_compose.py
# into a temporary directory and scans each file by explicit path. It
# fails on any finding, and on a generated file Semgrep did not scan
# (the rules' paths.include picks targets by file name).
# scripts/tests/compose_test.sh checks the same files merged with
# docker-compose.yml.
#
# Run (needs semgrep and Python 3.11 or later on PATH):
#   uvx --from semgrep==1.180.0 bash scripts/tests/semgrep_generated_test.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 "$ROOT/scripts/config/tests/synthetic_compose.py" "$WORK/generated"

files=()
while IFS= read -r file; do
    files+=("$file")
done < <(find "$WORK/generated" -name compose.accounts.yml -type f | sort)
if ((${#files[@]} < 3)); then
    printf 'FAIL: expected a generated file per synthetic configuration, found %d\n' \
        "${#files[@]}" >&2
    exit 1
fi

# A canary: a copy of the first file with a forbidden setting added under
# the same name, which the scan must report, so a clean result means the
# rules read the generated files.
mkdir "$WORK/canary"
canary="$WORK/canary/compose.accounts.yml"
awk '{ print } !done && $0 == "    read_only: true" { print "    privileged: true"; done = 1 }' \
    "${files[0]}" >"$canary"

semgrep scan --metrics=off --disable-version-check --strict --quiet --json \
    --config "$ROOT/.semgrep/compose.yaml" "${files[@]}" "$canary" >"$WORK/out.json"

failures=0
if ! jq -e --arg f "$canary" \
    '[.results[] | select(.path == $f) | .check_id | split(".") | last] | index("compose-privileged") != null' \
    "$WORK/out.json" >/dev/null; then
    printf 'FAIL: the canary copy with privileged: true was not reported\n' >&2
    failures=$((failures + 1))
fi
jq --arg f "$canary" '.results |= map(select(.path != $f))' "$WORK/out.json" >"$WORK/generated.json"
mv "$WORK/generated.json" "$WORK/out.json"

for file in "${files[@]}"; do
    if ! jq -e --arg f "$file" '.paths.scanned | index($f) != null' "$WORK/out.json" >/dev/null; then
        printf 'FAIL: %s was not scanned\n' "$file" >&2
        failures=$((failures + 1))
    fi
done
if jq -e '(.results | length) > 0 or (.errors | length) > 0' "$WORK/out.json" >/dev/null; then
    jq -r '.results[] | "FAIL: \(.path):\(.start.line): \(.check_id | split(".") | last)"' \
        "$WORK/out.json" >&2
    jq -r '.errors[] | "FAIL: semgrep error: \(.type // "unknown")"' "$WORK/out.json" >&2
    failures=$((failures + 1))
fi

if ((failures > 0)); then
    exit 1
fi
printf 'All %d generated Compose files were scanned with no findings.\n' "${#files[@]}"
