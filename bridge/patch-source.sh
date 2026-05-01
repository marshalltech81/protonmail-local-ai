#!/bin/bash
set -Eeuo pipefail

readonly REPO_DIR="${1:-/build}"
readonly CONSTANTS_FILE="${REPO_DIR}/internal/constants/constants.go"
readonly CERTS_FILE="${REPO_DIR}/internal/certs/tls.go"
readonly SETTINGS_FILE="${REPO_DIR}/internal/vault/types_settings.go"

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
# Bridge's vault default has AutoUpdate: true. Patching to false disables the
# in-process auto-updater (which silently downloads new releases from the
# Proton CDN — including the Qt/GUI variant — and stages them under
# /data/local/protonmail/bridge-v3/updates/, bypassing the BRIDGE_VERSION
# pin and the bridge-patch-check / bridge-smoke gates). See AGENTS.md.
readonly UPSTREAM_AUTOUPDATE='AutoUpdate:        true,'
readonly PATCHED_AUTOUPDATE='AutoUpdate:        false,'

# Path to the synthetic Go test file written by verify_autoupdate_default.
# Tracked at script scope so the EXIT/INT/TERM trap below can clean it up
# even if `go test` is interrupted by a signal — a function-local RETURN
# trap would not fire on Ctrl-C and would leak the file into the patched
# tree, breaking the next re-run against a reused checkout.
ASSERT_TEST_FILE=""
cleanup_assert_test_file() {
    if [[ -n "$ASSERT_TEST_FILE" ]]; then
        rm -f "$ASSERT_TEST_FILE"
    fi
}
trap cleanup_assert_test_file EXIT INT TERM

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

if [[ ! -f "$CONSTANTS_FILE" || ! -f "$CERTS_FILE" || ! -f "$SETTINGS_FILE" ]]; then
    printf 'Bridge source files not found under %s.\n' "$REPO_DIR" >&2
    exit 1
fi

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "1" "upstream host binding"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "0" "patched host binding before patch"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "1" "upstream TLS SAN source line"
require_count "$CERTS_FILE" "$PATCHED_CERT" "0" "patched TLS SAN line before patch"
require_count "$SETTINGS_FILE" "$UPSTREAM_AUTOUPDATE" "1" "upstream AutoUpdate default"
require_count "$SETTINGS_FILE" "$PATCHED_AUTOUPDATE" "0" "patched AutoUpdate default before patch"

sed_in_place 's/Host = "127.0.0.1"/Host = "0.0.0.0"/' "$CONSTANTS_FILE"
sed_in_place \
    's|IPAddresses:           \[\]net\.IP{net\.ParseIP("127\.0\.0\.1")},|IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},\
\t\tDNSNames:    []string{"protonmail-bridge", "localhost"},|' \
    "$CERTS_FILE"
sed_in_place 's/AutoUpdate:        true,/AutoUpdate:        false,/' "$SETTINGS_FILE"

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "0" "upstream host binding after patch"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "1" "patched host binding"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "0" "upstream TLS SAN source line after patch"
require_count "$CERTS_FILE" "$PATCHED_CERT" "1" "patched TLS SAN line"
require_count "$SETTINGS_FILE" "$UPSTREAM_AUTOUPDATE" "0" "upstream AutoUpdate default after patch"
require_count "$SETTINGS_FILE" "$PATCHED_AUTOUPDATE" "1" "patched AutoUpdate default"

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
resolve_go_image() {
    local go_image="${BRIDGE_GO_IMAGE:-}"
    if [[ -z "$go_image" && -f "$BRIDGE_DOCKERFILE" ]]; then
        go_image="$(awk '/^FROM golang:/ {sub(/^FROM /, ""); sub(/ +AS .*/, ""); print; exit}' "$BRIDGE_DOCKERFILE")"
    fi
    if [[ -z "$go_image" ]]; then
        printf 'ERROR: could not resolve pinned Go image; set BRIDGE_GO_IMAGE or run from a tree containing bridge/Dockerfile.\n' >&2
        return 1
    fi
    printf '%s\n' "$go_image"
}

# Run a `go ...` command inside the pinned golang image with the same C
# build deps Bridge's own Dockerfile installs in its builder stage. The
# vault package transitively imports docker-credential-helpers, which
# requires libsecret-1-dev via pkg-config; without it `go build`/`go test`
# fail with "Package libsecret-1 was not found." Installing on every
# invocation is fine — the drift check is rare and the apt step is small
# next to the bridge git clone and module download it already does.
run_go_in_pinned_image() {
    local go_image="$1"
    shift
    local go_args
    go_args="$(printf ' %q' "$@")"

    docker run --rm \
        --workdir /src \
        --volume "$REPO_DIR:/src:ro" \
        --env GOTOOLCHAIN=local \
        --env DEBIAN_FRONTEND=noninteractive \
        "$go_image" \
        sh -c '
            if ! apt-get update >/dev/null 2>&1; then
                echo "ERROR: apt-get update failed inside the pinned Go image." >&2
                echo "       The drift check needs network access to Debian mirrors;" >&2
                echo "       reconnect and re-run, or run on a host with Go installed" >&2
                echo "       to skip the in-container path." >&2
                exit 1
            fi
            apt-get install -y --no-install-recommends \
                pkg-config libsecret-1-dev libfido2-dev libcbor-dev >/dev/null \
            && rm -rf /var/lib/apt/lists/* \
            && go'"$go_args"
}

compile_patched_packages() {
    if command -v go >/dev/null 2>&1; then
        ( cd "$REPO_DIR" \
          && go build ./internal/constants/... ./internal/certs/... ./internal/vault/... )
        return
    fi

    if ! command -v docker >/dev/null 2>&1; then
        printf 'ERROR: neither go nor docker found on PATH; cannot verify the patched source compiles.\n' >&2
        printf 'Install Go on the host, or ensure Docker is available so the pinned golang image can run the check.\n' >&2
        return 1
    fi

    local go_image
    go_image="$(resolve_go_image)" || return 1

    printf 'Compiling patched packages inside %s...\n' "$go_image"
    run_go_in_pinned_image "$go_image" \
        build ./internal/constants/... ./internal/certs/... ./internal/vault/...
}

# Layer 3: prove the AutoUpdate patch flips the *runtime* default, not just
# the source string. We synthesize a tiny test inside the patched vault
# package that calls the unexported newDefaultSettings() and asserts the
# field. If Proton ever renames the constructor or introduces a second
# code path that overrides the default, this test fails the build.
verify_autoupdate_default() {
    # Set the script-scope path before writing the file so the EXIT/INT/TERM
    # trap above will clean it up on any exit path, including SIGINT during
    # `go test`.
    ASSERT_TEST_FILE="${REPO_DIR}/internal/vault/autoupdate_patch_assert_test.go"

    cat > "$ASSERT_TEST_FILE" <<'GOEOF'
package vault

import "testing"

// TestPatchedAutoUpdateDefaultIsFalse is generated by
// bridge/patch-source.sh after the AutoUpdate hunk is applied. It verifies
// the patched source actually flips the runtime default returned by
// newDefaultSettings(); a passing string-replace alone does not guarantee
// that.
func TestPatchedAutoUpdateDefaultIsFalse(t *testing.T) {
	settings := newDefaultSettings(t.TempDir())
	if settings.AutoUpdate {
		t.Fatal("AutoUpdate default is true after patch — runtime field was not flipped")
	}
}
GOEOF

    if command -v go >/dev/null 2>&1; then
        ( cd "$REPO_DIR" \
          && go test -count=1 -run TestPatchedAutoUpdateDefaultIsFalse ./internal/vault/ )
        return
    fi

    if ! command -v docker >/dev/null 2>&1; then
        printf 'ERROR: neither go nor docker found on PATH; cannot run the AutoUpdate assertion.\n' >&2
        return 1
    fi

    local go_image
    go_image="$(resolve_go_image)" || return 1

    printf 'Running AutoUpdate default assertion inside %s...\n' "$go_image"
    run_go_in_pinned_image "$go_image" \
        test -count=1 -run TestPatchedAutoUpdateDefaultIsFalse ./internal/vault/
}

compile_patched_packages \
    || { printf 'ERROR: post-patch compilation failed in %s.\n' "$REPO_DIR" >&2; exit 1; }

verify_autoupdate_default \
    || { printf 'ERROR: AutoUpdate runtime-default assertion failed in %s.\n' "$REPO_DIR" >&2; exit 1; }

printf 'Bridge source patches applied cleanly in %s.\n' "$REPO_DIR"
