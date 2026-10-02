#!/bin/bash
set -Eeuo pipefail

readonly REPO_DIR="${1:-/build}"
readonly CONSTANTS_FILE="${REPO_DIR}/internal/constants/constants.go"
readonly CERTS_FILE="${REPO_DIR}/internal/certs/tls.go"
readonly SETTINGS_FILE="${REPO_DIR}/internal/vault/types_settings.go"
readonly UPDATES_FILE="${REPO_DIR}/internal/bridge/updates.go"

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
# gofmt indentation of the inserted SAN line. A literal tab, not a `\t` sed
# escape: `\t` in a replacement is a GNU extension that older BSD/macOS sed,
# used by local `make bridge-patch-check` runs, turns into a literal `t` (#617).
readonly SAN_INDENT=$'\t\t'
# Bridge's vault default has AutoUpdate: true. Patching to false disables the
# in-process auto-updater (which silently downloads new releases from the
# Proton CDN — including the Qt/GUI variant — and stages them under
# /data/local/protonmail/bridge-v3/updates/, bypassing the BRIDGE_VERSION
# pin and the bridge-patch-check / bridge-smoke gates). See AGENTS.md.
readonly UPSTREAM_AUTOUPDATE='AutoUpdate:        true,'
readonly PATCHED_AUTOUPDATE='AutoUpdate:        false,'
# The default above reaches only new vaults: an existing vault keeps the
# AutoUpdate=true it stored before that patch, and handleUpdate() reads the
# stored value at its install gate. Forcing the gate's input to false keeps
# the silent download-and-stage path off for every vault (#245). Bridge
# still announces an available update; installing one stays a manual step.
readonly UPSTREAM_UPDATE_GATE='autoUpdateEnabled := bridge.vault.GetAutoUpdate()'
readonly PATCHED_UPDATE_GATE='autoUpdateEnabled := false'

# Path to the synthetic Go test file written by verify_autoupdate_default.
# Tracked at script scope so the EXIT/INT/TERM trap below can clean it up
# even if `go test` is interrupted by a signal — a function-local RETURN
# trap would not fire on Ctrl-C and would leak the file into the patched
# tree, breaking the next re-run against a reused checkout.
ASSERT_TEST_FILE=""
GATE_TEST_FILE=""
cleanup_assert_test_file() {
    if [[ -n "$ASSERT_TEST_FILE" ]]; then
        rm -f "$ASSERT_TEST_FILE"
    fi
    if [[ -n "$GATE_TEST_FILE" ]]; then
        rm -f "$GATE_TEST_FILE"
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

if [[ ! -f "$CONSTANTS_FILE" || ! -f "$CERTS_FILE" || ! -f "$SETTINGS_FILE" || ! -f "$UPDATES_FILE" ]]; then
    printf 'Bridge source files not found under %s.\n' "$REPO_DIR" >&2
    exit 1
fi

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "1" "upstream host binding"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "0" "patched host binding before patch"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "1" "upstream TLS SAN source line"
require_count "$CERTS_FILE" "$PATCHED_CERT" "0" "patched TLS SAN line before patch"
require_count "$SETTINGS_FILE" "$UPSTREAM_AUTOUPDATE" "1" "upstream AutoUpdate default"
require_count "$SETTINGS_FILE" "$PATCHED_AUTOUPDATE" "0" "patched AutoUpdate default before patch"
require_count "$UPDATES_FILE" "$UPSTREAM_UPDATE_GATE" "1" "upstream auto-update gate"
require_count "$UPDATES_FILE" "$PATCHED_UPDATE_GATE" "0" "patched auto-update gate before patch"

sed_in_place 's/Host = "127.0.0.1"/Host = "0.0.0.0"/' "$CONSTANTS_FILE"
sed_in_place \
    's|IPAddresses:           \[\]net\.IP{net\.ParseIP("127\.0\.0\.1")},|IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},\
'"$SAN_INDENT"'DNSNames:    []string{"protonmail-bridge", "localhost"},|' \
    "$CERTS_FILE"
sed_in_place 's/AutoUpdate:        true,/AutoUpdate:        false,/' "$SETTINGS_FILE"
sed_in_place 's/autoUpdateEnabled := bridge\.vault\.GetAutoUpdate()/autoUpdateEnabled := false/' "$UPDATES_FILE"

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "0" "upstream host binding after patch"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "1" "patched host binding"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "0" "upstream TLS SAN source line after patch"
require_count "$CERTS_FILE" "$PATCHED_CERT" "1" "patched TLS SAN line"
require_count "$CERTS_FILE" "${SAN_INDENT}${PATCHED_CERT}" "1" "tab-indented patched TLS SAN line"
require_count "$SETTINGS_FILE" "$UPSTREAM_AUTOUPDATE" "0" "upstream AutoUpdate default after patch"
require_count "$SETTINGS_FILE" "$PATCHED_AUTOUPDATE" "1" "patched AutoUpdate default"
require_count "$UPDATES_FILE" "$UPSTREAM_UPDATE_GATE" "0" "upstream auto-update gate after patch"
require_count "$UPDATES_FILE" "$PATCHED_UPDATE_GATE" "1" "patched auto-update gate"

# Compile the patched packages to confirm the patches produce valid Go.
# String-count checks above verify content; this verifies the result compiles.
#
# Two paths:
#   1. A host toolchain, used only when it can actually build these packages
#      — Go on PATH plus every pkg-config module the container path installs.
#      Inside the Bridge Docker build that is always true (the pinned golang
#      builder stage installs them), so the build compiles directly.
#   2. Otherwise, run the same compile inside the exact pinned Go image
#      bridge/Dockerfile uses, so the check matches the real build's Go
#      version. The image reference is sourced from bridge/Dockerfile to
#      avoid drift between the two pins.
#
# The capability probe in (1) is load-bearing, not defensive dressing.
# ./internal/vault/... transitively imports docker-credential-helpers, which
# needs libsecret-1 via pkg-config, and Bridge links libfido2/libcbor. A host
# with Go but without those modules fails `go build` with "Package libsecret-1
# was not found" even though every require_count guard above passed — which
# reads as patch drift when nothing has drifted. GitHub's ubuntu runners ship
# Go but not libsecret-1-dev, and that is exactly how a false drift signal
# reached the weekly bridge-version-check workflow for Bridge v3.27.0.
HOST_PKG_CONFIG_MODULES=(libsecret-1 libfido2 libcbor)
readonly HOST_PKG_CONFIG_MODULES

# Minimum Go the cloned Bridge source demands, read from its own go.mod.
go_mod_required_version() {
    local go_mod="${REPO_DIR}/go.mod"

    if [[ ! -f "$go_mod" ]]; then
        return 1
    fi

    awk '$1 == "go" { print $2; exit }' "$go_mod"
}

# True when dotted numeric version $1 is >= $2. Implemented in bash rather
# than via `sort -V` because BSD/macOS sort does not reliably provide it.
version_at_least() {
    local have="$1"
    local want="$2"
    local -a have_parts want_parts
    local index have_part want_part

    IFS='.' read -r -a have_parts <<< "$have"
    IFS='.' read -r -a want_parts <<< "$want"

    for index in 0 1 2; do
        # Strip any non-numeric suffix (rc1, beta2) before comparing.
        have_part="${have_parts[index]:-0}"
        want_part="${want_parts[index]:-0}"
        have_part="${have_part%%[^0-9]*}"
        want_part="${want_part%%[^0-9]*}"

        if (( ${have_part:-0} > ${want_part:-0} )); then
            return 0
        fi
        if (( ${have_part:-0} < ${want_part:-0} )); then
            return 1
        fi
    done

    return 0
}

host_go_is_usable() {
    if ! command -v go >/dev/null 2>&1; then
        return 1
    fi

    # The host toolchain must be able to build this module ON ITS OWN. With
    # GOTOOLCHAIN=local an older Go refuses outright:
    #
    #   go: go.mod requires go >= 1.26.1 (running go 1.24.13; GOTOOLCHAIN=local)
    #
    # GitHub runners currently ship Go 1.24.x while Bridge requires 1.26.1, so
    # CI takes the container path. Without this gate the host path "worked"
    # only because Go silently downloaded another toolchain, defeating the
    # pinned-image contract AGENTS.md requires. Inside the Bridge Docker build
    # the builder stage's Go satisfies go.mod, so that path still compiles
    # directly — which it must, since Docker is unavailable there.
    local required host_version
    if required="$(go_mod_required_version)" && [[ -n "$required" ]]; then
        host_version="$(go env GOVERSION 2>/dev/null | sed 's/^go//')"
        if [[ -z "$host_version" ]] || ! version_at_least "$host_version" "$required"; then
            printf 'Host Go %s does not satisfy go.mod go >= %s; using the pinned Go image instead.\n' \
                "${host_version:-unknown}" "$required" >&2
            return 1
        fi
    fi

    if ! command -v pkg-config >/dev/null 2>&1; then
        printf 'Host has go but no pkg-config; using the pinned Go image instead.\n' >&2
        return 1
    fi

    local module
    for module in "${HOST_PKG_CONFIG_MODULES[@]}"; do
        if ! pkg-config --exists "$module" >/dev/null 2>&1; then
            printf 'Host is missing pkg-config module %s; using the pinned Go image instead.\n' \
                "$module" >&2
            return 1
        fi
    done

    return 0
}

# Run a `go ...` command against the checkout on the host.
#
# GOTOOLCHAIN=local mirrors the builder-stage ENV in bridge/Dockerfile and the
# --env already passed on the container path. Bridge v3.27.0 was the first
# release to carry a `toolchain` directive in go.mod; without this pin the host
# path silently downloads that Go version instead of using the one the build
# is pinned to, which AGENTS.md explicitly forbids.
run_go_on_host() {
    ( cd "$REPO_DIR" && GOTOOLCHAIN=local go "$@" )
}

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
    if host_go_is_usable; then
        run_go_on_host build ./internal/constants/... ./internal/certs/... ./internal/vault/... ./internal/bridge/...
        return
    fi

    if ! command -v docker >/dev/null 2>&1; then
        printf 'ERROR: no usable host Go toolchain and no docker on PATH; cannot verify the patched source compiles.\n' >&2
        printf 'Install Go plus pkg-config, libsecret-1-dev, libfido2-dev and libcbor-dev on the host,\n' >&2
        printf 'or ensure Docker is available so the pinned golang image can run the check.\n' >&2
        return 1
    fi

    local go_image
    go_image="$(resolve_go_image)" || return 1

    printf 'Compiling patched packages inside %s...\n' "$go_image"
    run_go_in_pinned_image "$go_image" \
        build ./internal/constants/... ./internal/certs/... ./internal/vault/... ./internal/bridge/...
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

    if host_go_is_usable; then
        run_go_on_host test -count=1 -run TestPatchedAutoUpdateDefaultIsFalse ./internal/vault/
        return
    fi

    if ! command -v docker >/dev/null 2>&1; then
        printf 'ERROR: no usable host Go toolchain and no docker on PATH; cannot run the AutoUpdate assertion.\n' >&2
        return 1
    fi

    local go_image
    go_image="$(resolve_go_image)" || return 1

    printf 'Running AutoUpdate default assertion inside %s...\n' "$go_image"
    run_go_in_pinned_image "$go_image" \
        test -count=1 -run TestPatchedAutoUpdateDefaultIsFalse ./internal/vault/
}

# Layer 2 for the update-gate hunk (#245). The default test above covers new
# vaults only, so this one reopens a vault that already stores
# AutoUpdate=true, offers an eligible release through Bridge's own mock
# updater, and asserts the gate only announces it: no silent install job is
# queued, so the package download and staging path is never entered. It
# runs in the external bridge_test package to reuse upstream's withEnv /
# withBridge harness (an in-process fake Proton API, no network).
verify_autoupdate_gate() {
    GATE_TEST_FILE="${REPO_DIR}/internal/bridge/autoupdate_gate_patch_assert_test.go"

    cat > "$GATE_TEST_FILE" <<'GOEOF'
package bridge_test

import (
	"context"
	"testing"
	"time"

	"github.com/Masterminds/semver/v3"
	"github.com/ProtonMail/gluon/async"
	"github.com/ProtonMail/go-proton-api"
	"github.com/ProtonMail/go-proton-api/server"
	bridgePkg "github.com/ProtonMail/proton-bridge/v3/internal/bridge"
	"github.com/ProtonMail/proton-bridge/v3/internal/events"
	"github.com/ProtonMail/proton-bridge/v3/internal/updater"
	"github.com/ProtonMail/proton-bridge/v3/internal/updater/versioncompare"
	"github.com/ProtonMail/proton-bridge/v3/internal/vault"
	"github.com/stretchr/testify/require"
)

// TestPatchedAutoUpdateGateIgnoresEnabledVault is generated by
// bridge/patch-source.sh after the update-gate hunk is applied. It reopens
// a vault that already stores AutoUpdate=true (as vaults created before the
// default patch do), offers an eligible release, and verifies Bridge only
// announces it: no silent install job is queued, so the package download
// and staging path is never entered.
func TestPatchedAutoUpdateGateIgnoresEnabledVault(t *testing.T) {
	withEnv(t, func(ctx context.Context, s *server.Server, netCtl *proton.NetCtl, locator bridgePkg.Locator, vaultKey []byte) {
		vaultDir, err := locator.ProvideSettingsPath()
		require.NoError(t, err)
		seeded, _, err := vault.New(vaultDir, t.TempDir(), vaultKey, async.NoopPanicHandler{})
		require.NoError(t, err)
		require.NoError(t, seeded.SetAutoUpdate(true))
		require.NoError(t, seeded.Close())

		withBridge(ctx, t, s.GetHostURL(), netCtl, locator, vaultKey, func(bridge *bridgePkg.Bridge, mocks *bridgePkg.Mocks) {
			require.True(t, bridge.GetAutoUpdate(), "seeded vault did not load AutoUpdate=true")

			updateCh, done := bridge.GetEvents(events.UpdateAvailable{}, events.UpdateInstalled{})
			defer done()

			bridge.SetCurrentVersionTest(semver.MustParse("2.1.1"))

			release := updater.Release{
				ReleaseCategory:   updater.StableReleaseCategory,
				Version:           semver.MustParse("2.1.3"),
				SystemVersion:     versioncompare.SystemVersion{},
				RolloutProportion: 1.0,
				MinAuto:           &semver.Version{},
				File: []updater.File{
					{URL: "RANDOM_PACKAGE_URL", Identifier: updater.PackageIdentifier},
				},
			}
			mocks.Updater.SetLatestVersion(updater.VersionInfo{Releases: []updater.Release{release}})
			bridge.CheckForUpdates()

			// The gate's announce-only branch publishes a non-silent
			// UpdateAvailable. The install job would instead publish a
			// silent UpdateAvailable and then UpdateInstalled.
			select {
			case event := <-updateCh:
				require.Equal(t, events.UpdateAvailable{Release: release, Compatible: true, Silent: false}, event,
					"auto-update gate queued an install for a vault with AutoUpdate=true")
			case <-time.After(30 * time.Second):
				t.Fatal("no update event after CheckForUpdates")
			}

			select {
			case event := <-updateCh:
				t.Fatalf("unexpected update event after the gate: %T", event)
			case <-time.After(2 * time.Second):
			}
		})
	})
}
GOEOF

    if host_go_is_usable; then
        run_go_on_host test -count=1 -run TestPatchedAutoUpdateGateIgnoresEnabledVault ./internal/bridge/
        return
    fi

    if ! command -v docker >/dev/null 2>&1; then
        printf 'ERROR: no usable host Go toolchain and no docker on PATH; cannot run the auto-update gate assertion.\n' >&2
        return 1
    fi

    local go_image
    go_image="$(resolve_go_image)" || return 1

    printf 'Running auto-update gate assertion inside %s...\n' "$go_image"
    run_go_in_pinned_image "$go_image" \
        test -count=1 -run TestPatchedAutoUpdateGateIgnoresEnabledVault ./internal/bridge/
}

compile_patched_packages \
    || { printf 'ERROR: post-patch compilation failed in %s.\n' "$REPO_DIR" >&2; exit 1; }

verify_autoupdate_default \
    || { printf 'ERROR: AutoUpdate runtime-default assertion failed in %s.\n' "$REPO_DIR" >&2; exit 1; }

verify_autoupdate_gate \
    || { printf 'ERROR: auto-update gate assertion failed in %s.\n' "$REPO_DIR" >&2; exit 1; }

printf 'Bridge source patches applied cleanly in %s.\n' "$REPO_DIR"
