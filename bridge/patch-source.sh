#!/bin/bash
set -Eeuo pipefail

readonly REPO_DIR="${1:-/build}"
readonly CONSTANTS_FILE="${REPO_DIR}/internal/constants/constants.go"
readonly CERTS_FILE="${REPO_DIR}/internal/certs/tls.go"

# Resolve early so the post-patch compile step can locate bridge/Dockerfile
# (the source of truth for the pinned Go image) when invoked from a host
# checkout without Go installed. Inside the Bridge Docker build this points
# at /usr/local/bin and is unused — `go` is on PATH there.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly BRIDGE_DOCKERFILE="${SCRIPT_DIR}/Dockerfile"

readonly UPSTREAM_HOST='Host = "127.0.0.1"'
readonly PATCHED_HOST='Host = "0.0.0.0"'
readonly UPSTREAM_CERT='IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},'
readonly PATCHED_CERT='DNSNames:    []string{"protonmail-bridge", "localhost"},'

# These exact string checks are intentionally strict: if Proton changes the
# surrounding source layout, we want the build and drift check to fail loudly
# instead of silently applying a partial or misplaced patch.
count_matches() {
    local file="$1"
    local needle="$2"

    grep -F -c -- "$needle" "$file" || true
}

require_count() {
    local file="$1"
    local needle="$2"
    local expected="$3"
    local description="$4"
    local actual

    actual="$(count_matches "$file" "$needle")"
    if [[ "$actual" != "$expected" ]]; then
        printf 'Patch drift detected: expected %s match(es) for %s in %s, found %s.\n' \
            "$expected" "$description" "$file" "$actual" >&2
        exit 1
    fi
}

sed_in_place() {
    local expression="$1"
    local file="$2"

    # GNU sed accepts `-i`, while BSD/macOS sed requires `-i ''`. Keep the
    # patch helper portable so local drift checks use the same logic as CI.
    if sed --version >/dev/null 2>&1; then
        sed -i "$expression" "$file"
    else
        sed -i '' "$expression" "$file"
    fi
}

if [[ ! -f "$CONSTANTS_FILE" || ! -f "$CERTS_FILE" ]]; then
    printf 'Bridge source files not found under %s.\n' "$REPO_DIR" >&2
    exit 1
fi

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "1" "upstream host binding"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "0" "patched host binding before patch"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "1" "upstream TLS SAN source line"
require_count "$CERTS_FILE" "$PATCHED_CERT" "0" "patched TLS SAN line before patch"

sed_in_place 's/Host = "127.0.0.1"/Host = "0.0.0.0"/' "$CONSTANTS_FILE"
sed_in_place \
    's|IPAddresses:           \[\]net\.IP{net\.ParseIP("127\.0\.0\.1")},|IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},\
\t\tDNSNames:    []string{"protonmail-bridge", "localhost"},|' \
    "$CERTS_FILE"

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "0" "upstream host binding after patch"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "1" "patched host binding"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "0" "upstream TLS SAN source line after patch"
require_count "$CERTS_FILE" "$PATCHED_CERT" "1" "patched TLS SAN line"

# Compile the two patched packages to confirm the patches produce valid Go.
# String-count checks above verify content; this verifies the result compiles.
#
# Two paths:
#   1. Inside the Bridge Docker build, `go` is already on PATH (the pinned
#      golang builder stage), so compile directly.
#   2. From the host drift check (`scripts/bridge-patch-drift.sh`), `go` is
#      typically not installed locally. Fall back to running the same compile
#      inside the exact pinned Go image bridge/Dockerfile uses, so the host
#      check matches the actual build's Go version. The image reference is
#      sourced from bridge/Dockerfile to avoid drift between the two pins.
compile_patched_packages() {
    if command -v go >/dev/null 2>&1; then
        ( cd "$REPO_DIR" && go build ./internal/constants/... ./internal/certs/... )
        return
    fi

    if ! command -v docker >/dev/null 2>&1; then
        printf 'ERROR: neither go nor docker found on PATH; cannot verify the patched source compiles.\n' >&2
        printf 'Install Go on the host, or ensure Docker is available so the pinned golang image can run the check.\n' >&2
        return 1
    fi

    local go_image="${BRIDGE_GO_IMAGE:-}"
    if [[ -z "$go_image" && -f "$BRIDGE_DOCKERFILE" ]]; then
        go_image="$(awk '/^FROM golang:/ {sub(/^FROM /, ""); sub(/ +AS .*/, ""); print; exit}' "$BRIDGE_DOCKERFILE")"
    fi
    if [[ -z "$go_image" ]]; then
        printf 'ERROR: could not resolve pinned Go image; set BRIDGE_GO_IMAGE or run from a tree containing bridge/Dockerfile.\n' >&2
        return 1
    fi

    printf 'Compiling patched packages inside %s...\n' "$go_image"
    docker run --rm \
        --workdir /src \
        --volume "$REPO_DIR:/src:ro" \
        --env GOTOOLCHAIN=local \
        "$go_image" \
        go build ./internal/constants/... ./internal/certs/...
}

compile_patched_packages \
    || { printf 'ERROR: post-patch compilation failed in %s.\n' "$REPO_DIR" >&2; exit 1; }

printf 'Bridge source patches applied cleanly in %s.\n' "$REPO_DIR"
