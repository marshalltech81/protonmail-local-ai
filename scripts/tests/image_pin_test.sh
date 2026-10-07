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
# The same check covers the BuildKit image both workflows hand
# docker/setup-buildx-action through `driver-opts: image=moby/buildkit:`
# (#1122): Dependabot tracks the action's SHA but not a driver option,
# so the two pins are bumped by hand and must move together, or the
# two jobs build the same stage on two BuildKit versions through one
# shared cache.
#
# Run: bash scripts/tests/image_pin_test.sh  (or make test-image-pins)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

readonly DOCKERFILES=(indexer/Dockerfile mcp-server/Dockerfile)
readonly PIN_SCRIPT=mbsync/tests/tls_check.sh
readonly WORKFLOWS=(.github/workflows/docker.yml .github/workflows/tests.yml)
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

# Prints "file<TAB>reference" for the BuildKit image every workflow under
# $1 passes in a `driver-opts: image=moby/buildkit:...` line. Fails when
# a workflow has none, so a renamed option or image cannot pass by
# matching nothing.
buildkit_references() {
    local root="$1" file ref refs
    for file in "${WORKFLOWS[@]}"; do
        refs="$(sed -nE 's#^[[:space:]]*driver-opts:[[:space:]]*image=(moby/buildkit:[^[:space:],]+).*$#\1#p' \
            "$root/$file")"
        if [[ -z "$refs" ]]; then
            printf '%s: no driver-opts: image=moby/buildkit: line\n' "$file" >&2
            return 1
        fi
        while IFS= read -r ref; do
            printf '%s\t%s\n' "$file" "$ref"
        done <<<"$refs"
    done
}

# Fails unless the "file<TAB>reference" lines in $3 name one image, pinned
# by digest; $1 names the image in the message and $2 is the pattern a
# digest-pinned reference to it matches.
one_pinned_reference() {
    local name="$1" pattern="$2" refs="$3" distinct
    distinct="$(cut -f2 <<<"$refs" | sort -u)"
    if (($(grep -c '' <<<"$distinct") != 1)); then
        printf 'the %s image is pinned differently (bump them together):\n%s\n' "$name" "$refs"
        return 1
    fi
    if [[ ! "$distinct" =~ $pattern ]]; then
        printf 'the %s image is not pinned by digest: %s\n' "$name" "$distinct"
        return 1
    fi
}

# Fails unless every python reference under $1 is the same digest-pinned image.
pins_agree() {
    local root="$1" refs
    refs="$(image_references "$root")" || return 1
    one_pinned_reference python '^python:[^@]+@sha256:[0-9a-f]{64}$' "$refs"
}

# Fails unless both workflows pin the same digest-pinned BuildKit image.
buildkit_pins_agree() {
    local root="$1" refs
    refs="$(buildkit_references "$root")" || return 1
    one_pinned_reference BuildKit '^moby/buildkit:[^@]+@sha256:[0-9a-f]{64}$' "$refs"
}

# Copies the pinned files into $WORK/$1 with the repository's layout and
# prints that directory.
fixture() {
    local dir="$WORK/$1" file
    for file in "${DOCKERFILES[@]}" "$PIN_SCRIPT" "${WORKFLOWS[@]}"; do
        mkdir -p "$dir/$(dirname "$file")"
        cp "$ROOT_DIR/$file" "$dir/$file"
    done
    printf '%s\n' "$dir"
}

# Runs the check $1 on $2 expecting failure, with $3 in its output.
expect_drift() {
    local agree="$1" root="$2" message="$3" out
    if out="$("$agree" "$root" 2>&1)"; then
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
    expect_drift pins_agree "$root" "pinned differently"
}

a_bumped_test_pin_is_detected() {
    local root
    root="$(fixture pin-bump)"
    sed -i.bak -E "s/^(readonly PYTHON_IMAGE=\"python:[^@]+@)sha256:[0-9a-f]+/\1$SYNTHETIC_DIGEST/" \
        "$root/$PIN_SCRIPT"
    grep -F "$SYNTHETIC_DIGEST" "$root/$PIN_SCRIPT" >/dev/null
    expect_drift pins_agree "$root" "pinned differently"
}

a_missing_reference_is_detected() {
    local root
    root="$(fixture missing)"
    sed -i.bak -E 's/^FROM python:/FROM renamed:/' "$root/indexer/Dockerfile"
    expect_drift pins_agree "$root" "indexer/Dockerfile: no FROM python: line"
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
    expect_drift pins_agree "$root" "not pinned by digest"
}

the_tree_pins_one_buildkit_image() {
    buildkit_pins_agree "$ROOT_DIR"
}

a_bumped_workflow_buildkit_digest_is_detected() {
    local root
    root="$(fixture buildkit-bump)"
    # One workflow's pin bumped by hand without the other's.
    sed -i.bak -E "s/^([[:space:]]*driver-opts: image=moby\/buildkit:[^@]+@)sha256:[0-9a-f]+/\1$SYNTHETIC_DIGEST/" \
        "$root/.github/workflows/tests.yml"
    grep -F "$SYNTHETIC_DIGEST" "$root/.github/workflows/tests.yml" >/dev/null
    expect_drift buildkit_pins_agree "$root" "the BuildKit image is pinned differently"
}

a_missing_buildkit_reference_is_detected() {
    local root
    root="$(fixture buildkit-missing)"
    sed -i.bak -E 's/^([[:space:]]*driver-opts: )image=moby\/buildkit:/\1image=renamed\/buildkit:/' \
        "$root/.github/workflows/docker.yml"
    expect_drift buildkit_pins_agree "$root" \
        ".github/workflows/docker.yml: no driver-opts: image=moby/buildkit: line"
}

an_unpinned_buildkit_reference_is_detected() {
    local root
    root="$(fixture buildkit-unpinned)"
    # Both workflows on the same tag with no digest: one reference, not pinned.
    sed -i.bak -E 's/^([[:space:]]*driver-opts: image=moby\/buildkit:[^@]+)@sha256:[0-9a-f]+/\1/' \
        "$root/.github/workflows/docker.yml" "$root/.github/workflows/tests.yml"
    if grep -E '^[[:space:]]*driver-opts: image=moby/buildkit:.*@sha256' \
        "$root/.github/workflows/docker.yml" "$root/.github/workflows/tests.yml" >/dev/null; then
        printf 'fixture still pinned\n'
        return 1
    fi
    expect_drift buildkit_pins_agree "$root" "the BuildKit image is not pinned by digest"
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
check "the tree pins one BuildKit image" the_tree_pins_one_buildkit_image
check "a bumped workflow BuildKit digest is detected" a_bumped_workflow_buildkit_digest_is_detected
check "a missing BuildKit reference is detected" a_missing_buildkit_reference_is_detected
check "an unpinned BuildKit reference is detected" an_unpinned_buildkit_reference_is_detected

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
