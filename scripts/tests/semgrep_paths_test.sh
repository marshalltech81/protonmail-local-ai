#!/bin/bash
set -Eeuo pipefail

# Checks that every rule in .semgrep/compose.yaml reads every file name
# Docker Compose loads: docker-compose*.yml / *.yaml and the newer
# compose*.yml / *.yaml defaults. `semgrep test` ignores each rule's
# paths, so this copies the rule fixture under each name into a
# temporary directory, scans it, and expects a finding from every
# Compose rule in every copy.
#
# Run (needs semgrep on PATH):
#   uvx --from semgrep==1.180.0 bash scripts/tests/semgrep_paths_test.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
NAMES=(
    docker-compose.yml
    docker-compose.override.yaml
    compose.yaml
    compose.yml
    compose.override.yml
)

mkdir "$WORK/target"
for name in "${NAMES[@]}"; do
    cp "$ROOT/.semgrep/compose.test.yml" "$WORK/target/$name"
done

semgrep scan --metrics=off --disable-version-check --strict --quiet --json \
    --config "$ROOT/.semgrep/compose.yaml" "$WORK/target" >"$WORK/out.json"

# Rule ids from the config, and the ids reported for each copy (Semgrep
# prefixes a local config's ids with its path, so keep the last part).
grep -E '^  - id: ' "$ROOT/.semgrep/compose.yaml" | sed 's/^  - id: //' | sort >"$WORK/expected"
if [[ ! -s "$WORK/expected" ]]; then
    printf 'FAIL: no rule ids found in .semgrep/compose.yaml\n' >&2
    exit 1
fi

failures=0
for name in "${NAMES[@]}"; do
    jq -r --arg name "$name" \
        '.results[] | select(.path | endswith("/" + $name)) | .check_id | split(".") | last' \
        "$WORK/out.json" | sort -u >"$WORK/got"
    if missing="$(comm -23 "$WORK/expected" "$WORK/got")" && [[ -z "$missing" ]]; then
        printf 'ok: %s\n' "$name"
    else
        printf 'FAIL: %s is not read by: %s\n' "$name" "$(tr '\n' ' ' <<<"$missing")" >&2
        failures=$((failures + 1))
    fi
done

if ((failures > 0)); then
    exit 1
fi
printf 'All %d Compose file names are scanned by every rule.\n' "${#NAMES[@]}"
