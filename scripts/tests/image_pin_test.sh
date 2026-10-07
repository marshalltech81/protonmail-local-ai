#!/bin/bash
set -Eeuo pipefail

# Keeps the python image mbsync/tests/tls_check.sh pins (PYTHON_IMAGE,
# which runs its synthetic IMAP server) equal to the one indexer/Dockerfile
# and mcp-server/Dockerfile build from (#1023). Dependabot's docker
# updates rewrite only the Dockerfiles, so a base-image bump would
# otherwise leave the test's pin, and its comment, behind silently.
#
# The check reads every `FROM python:` line of both Dockerfiles (every
# stage: a digest bump moves them together, #1025) and the test's pin,
# and passes only when they are one image reference, tag and digest
# included. Copies of the three files with synthetic edits show that a
# bumped Dockerfile, a bumped pin and a missing or unpinned reference all
# fail. Needs no Docker.
#
# Run: bash scripts/tests/image_pin_test.sh  (or make test-image-pins)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

readonly DOCKERFILES=(indexer/Dockerfile mcp-server/Dockerfile)
readonly PIN_SCRIPT=mbsync/tests/tls_check.sh
readonly SYNTHETIC_DIGEST="sha256:4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e4c1e"

# Prints "file<TAB>reference" for every python image the tree under $1
# pins: each Dockerfile's `FROM python:` lines, then the test's
# PYTHON_IMAGE. Fails when a file has none, so a renamed base image or
# variable cannot pass by matching nothing.
image_references() {
    local root="$1" file ref refs
    for file in "${DOCKERFILES[@]}"; do
        refs="$(grep -E '^FROM[[:space:]]+python:' "$root/$file" | awk '{print $2}')"
        if [[ -z "$refs" ]]; then
            printf '%s: no FROM python: line\n' "$file" >&2
            return 1
        fi
        while IFS= read -r ref; do
            printf '%s\t%s\n' "$file" "$ref"
        done <<<"$refs"
    done
    ref="$(sed -nE 's/^readonly PYTHON_IMAGE="([^"]+)"$/\1/p' "$root/$PIN_SCRIPT")"
    if [[ -z "$ref" ]]; then
        printf '%s: no readonly PYTHON_IMAGE="..." line\n' "$PIN_SCRIPT" >&2
        return 1
    fi
    printf '%s\t%s\n' "$PIN_SCRIPT" "$ref"
}

# Fails unless every reference under $1 is the same digest-pinned image.
pins_agree() {
    local root="$1" refs distinct
    refs="$(image_references "$root")" || return 1
    distinct="$(cut -f2 <<<"$refs" | sort -u)"
    if (($(grep -c '' <<<"$distinct") != 1)); then
        printf 'the python image is pinned differently (bump them together):\n%s\n' "$refs"
        return 1
    fi
    if [[ ! "$distinct" =~ ^python:[^@]+@sha256:[0-9a-f]{64}$ ]]; then
        printf 'the python image is not pinned by digest: %s\n' "$distinct"
        return 1
    fi
}

# Copies the three files into $WORK/$1 with the repository's layout and
# prints that directory.
fixture() {
    local dir="$WORK/$1" file
    for file in "${DOCKERFILES[@]}" "$PIN_SCRIPT"; do
        mkdir -p "$dir/$(dirname "$file")"
        cp "$ROOT_DIR/$file" "$dir/$file"
    done
    printf '%s\n' "$dir"
}

# Runs pins_agree on $1 expecting failure, with $2 in its output.
expect_drift() {
    local root="$1" message="$2" out
    if out="$(pins_agree "$root" 2>&1)"; then
        printf 'passed, expected failure\n'
        return 1
    fi
    if ! grep -F -- "$message" <<<"$out" >/dev/null; then
        printf 'expected %q in:\n%s\n' "$message" "$out"
        return 1
    fi
}

the_tree_pins_one_python_image() {
    pins_agree "$ROOT_DIR"
}

a_bumped_dockerfile_digest_is_detected() {
    local root
    root="$(fixture dockerfile-bump)"
    # A Dependabot bump: every stage of one Dockerfile moves to a new digest.
    sed -i.bak -E "s/^(FROM python:[^@]+@)sha256:[0-9a-f]+/\1$SYNTHETIC_DIGEST/" \
        "$root/mcp-server/Dockerfile"
    grep -F "$SYNTHETIC_DIGEST" "$root/mcp-server/Dockerfile" >/dev/null
    expect_drift "$root" "pinned differently"
}

a_bumped_test_pin_is_detected() {
    local root
    root="$(fixture pin-bump)"
    sed -i.bak -E "s/^(readonly PYTHON_IMAGE=\"python:[^@]+@)sha256:[0-9a-f]+/\1$SYNTHETIC_DIGEST/" \
        "$root/$PIN_SCRIPT"
    grep -F "$SYNTHETIC_DIGEST" "$root/$PIN_SCRIPT" >/dev/null
    expect_drift "$root" "pinned differently"
}

a_missing_reference_is_detected() {
    local root
    root="$(fixture missing)"
    sed -i.bak -E 's/^FROM python:/FROM renamed:/' "$root/indexer/Dockerfile"
    expect_drift "$root" "indexer/Dockerfile: no FROM python: line"
}

an_unpinned_reference_is_detected() {
    local root
    root="$(fixture unpinned)"
    # Every file on the same tag with no digest: one reference, not pinned.
    sed -i.bak -E 's/^(FROM python:[^@]+)@sha256:[0-9a-f]+/\1/' \
        "$root/indexer/Dockerfile" "$root/mcp-server/Dockerfile"
    sed -i.bak -E 's/^(readonly PYTHON_IMAGE="python:[^@]+)@sha256:[0-9a-f]+/\1/' \
        "$root/$PIN_SCRIPT"
    if grep -E '^(FROM python:|readonly PYTHON_IMAGE=).*@sha256' "$root/indexer/Dockerfile" \
        "$root/mcp-server/Dockerfile" "$root/$PIN_SCRIPT" >/dev/null; then
        printf 'fixture still pinned\n'
        return 1
    fi
    expect_drift "$root" "not pinned by digest"
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

check "the tree pins one python image" the_tree_pins_one_python_image
check "a bumped Dockerfile digest is detected" a_bumped_dockerfile_digest_is_detected
check "a bumped test pin is detected" a_bumped_test_pin_is_detected
check "a missing reference is detected" a_missing_reference_is_detected
check "an unpinned reference is detected" an_unpinned_reference_is_detected

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
