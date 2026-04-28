#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR

# Resolve the target Bridge version from the first explicit source available so
# local runs and CI both check the same upstream release the repo is configured
# to build.
resolve_bridge_version() {
    local candidate="${1:-${BRIDGE_VERSION:-}}"
    local env_file

    if [[ -n "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
    fi

    for env_file in "$ROOT_DIR/.env" "$ROOT_DIR/.env.example"; do
        if [[ -f "$env_file" ]]; then
            candidate="$(grep -E '^BRIDGE_VERSION=' "$env_file" | head -n 1 | cut -d= -f2- || true)"
            if [[ -n "$candidate" ]]; then
                candidate="${candidate%$'\r'}"
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done

    printf 'Could not determine BRIDGE_VERSION from argument, environment, .env, or .env.example.\n' >&2
    exit 1
}

BRIDGE_VERSION="$(resolve_bridge_version "${1:-}")"
readonly BRIDGE_VERSION

# Clone into a throwaway directory so the drift check always evaluates pristine
# upstream source instead of whatever may already exist in the repo workspace.
TMP_DIR="$(mktemp -d)"
readonly TMP_DIR
readonly CLONE_DIR="${TMP_DIR}/proton-bridge"

cleanup() {
    rm -rf "$TMP_DIR"
}

trap cleanup EXIT

# `timeout` is the GNU coreutils binary; on Linux it ships as `timeout`, on
# macOS as `gtimeout` (from `brew install coreutils`). Pick whichever the host
# provides so the drift check runs the same on dev laptops and CI.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD="gtimeout"
else
    printf 'ERROR: neither timeout nor gtimeout found on PATH.\n' >&2
    printf 'On macOS, install with: brew install coreutils\n' >&2
    exit 1
fi
readonly TIMEOUT_CMD

# Fetch the exact upstream Bridge release and then run the same patch helper the
# Docker build uses. If the helper cannot find the expected patch points, it
# exits non-zero and we know the upstream source drifted.
printf 'Checking Proton Bridge patch points for %s...\n' "$BRIDGE_VERSION"
GIT_TERMINAL_PROMPT=0 "$TIMEOUT_CMD" 60s git clone --depth 1 --branch "$BRIDGE_VERSION" \
    https://github.com/ProtonMail/proton-bridge.git "$CLONE_DIR"

printf 'Fetched upstream Bridge commit %s.\n' "$(git -C "$CLONE_DIR" rev-parse --short HEAD)"
"$ROOT_DIR/bridge/patch-source.sh" "$CLONE_DIR"
printf 'Patch drift check passed for %s.\n' "$BRIDGE_VERSION"
