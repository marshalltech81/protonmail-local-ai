#!/bin/bash
set -Eeuo pipefail

# Rendering checks for docker-compose.yml with and without the macOS Bridge
# overlay (#497): which services run, mbsync's Bridge dependency and
# endpoint, and the hardening and exposure settings the overlay must keep.
# Uses ``docker compose config``, which needs the Compose CLI but no
# daemon, images or secret files.
#
# Run: bash scripts/tests/compose_test.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

readonly BASE="docker-compose.yml"
readonly MACOS="docker-compose.macos-bridge.yml"
readonly HARDENED="docker-compose.hardened.yml"

# Renders the given compose files as JSON into $WORK/config.json. The
# environment is fixed so an operator's shell or .env cannot change the
# result; --env-file /dev/null keeps the repository's .env out.
render() {
    local args=() file
    for file in "$@"; do
        args+=(-f "$ROOT_DIR/$file")
    done
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" BRIDGE_USER="synthetic@example.com" \
        ${BRIDGE_IMAP_PORT:+BRIDGE_IMAP_PORT="$BRIDGE_IMAP_PORT"} \
        docker compose --project-directory "$ROOT_DIR" --env-file /dev/null "${args[@]}" \
        config --format json >"$WORK/config.json" 2>"$WORK/compose.err" || {
        cat "$WORK/compose.err"
        return 1
    }
}

# Evaluates a jq filter against the last render; fails unless it prints true.
expect() {
    local filter="$1" result
    result="$(jq -c "$filter" "$WORK/config.json")"
    if [[ "$result" != "true" ]]; then
        printf 'expected true: %s\n' "$filter"
        return 1
    fi
}

check() {
    local description="$1" rc=0
    shift
    set +e
    (set -e; "$@") >"$WORK/output" 2>&1
    rc=$?
    set -e
    if ((rc == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/output"
        FAILURES=$((FAILURES + 1))
    fi
}

# Hardening every service keeps, in either mode, plus the one published
# port: mcp-server on the host's loopback.
expect_hardening_and_exposure() {
    expect '[.services[] | .read_only] | all' || return 1
    expect '[.services[] | .cap_drop == ["ALL"]] | all' || return 1
    expect '[.services[] | .security_opt | index("no-new-privileges:true") != null] | all' || return 1
    expect '[.services[] | has("network_mode") | not] | all' || return 1
    expect '[.services[] | has("privileged") | not] | all' || return 1
    expect '[.services | to_entries[] | select(.value.ports != null) | .key] == ["mcp-server"]' || return 1
    expect '[.services["mcp-server"].ports[] | .host_ip] == ["127.0.0.1"]' || return 1
}

default_mode_runs_the_bridge_container() {
    render "$BASE"
    expect '.services | keys == ["indexer", "mbsync", "mcp-server", "protonmail-bridge"]' || return 1
    expect '.services.mbsync.depends_on["protonmail-bridge"].condition == "service_healthy"' || return 1
    expect '.services.mbsync.environment.BRIDGE_HOST == "protonmail-bridge"' || return 1
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1143"' || return 1
    expect '.services.mbsync.environment | has("BRIDGE_CERT_HOST") | not' || return 1
    expect_hardening_and_exposure
}

# BRIDGE_IMAP_PORT in .env is for the macOS app; the Bridge container
# always listens on 1143.
default_mode_ignores_the_macos_port_override() {
    BRIDGE_IMAP_PORT=1144 render "$BASE"
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1143"' || return 1
}

macos_mode_starts_only_mbsync_indexer_and_mcp_server() {
    render "$BASE" "$MACOS"
    expect '.services | keys == ["indexer", "mbsync", "mcp-server"]' || return 1
    expect '.volumes | has("bridge-data") | not' || return 1
}

macos_mode_drops_only_the_bridge_dependency() {
    render "$BASE" "$MACOS"
    expect '(.services.mbsync.depends_on // {}) == {}' || return 1
    expect '.services.indexer.depends_on.mbsync.condition == "service_healthy"' || return 1
    expect '.services["mcp-server"].depends_on.indexer.condition == "service_healthy"' || return 1
}

macos_mode_points_mbsync_at_the_host_app() {
    render "$BASE" "$MACOS"
    expect '.services.mbsync.environment.BRIDGE_HOST == "host.docker.internal"' || return 1
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1143"' || return 1
    expect '.services.mbsync.environment.BRIDGE_CERT_HOST == "127.0.0.1"' || return 1
    expect '.services.mbsync.environment.BRIDGE_CERT_PIN_ROTATE == "false"' || return 1
}

macos_mode_takes_the_port_from_the_environment() {
    BRIDGE_IMAP_PORT=1144 render "$BASE" "$MACOS"
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1144"' || return 1
}

macos_mode_keeps_mbsync_hardening_and_exposure() {
    render "$BASE" "$MACOS"
    expect_hardening_and_exposure || return 1
    expect '.services.mbsync.secrets | map(.source) == ["bridge_pass"]' || return 1
    expect '.services.mbsync.networks | keys == ["bridge-net"]' || return 1
    expect '.services.mbsync.pids_limit == 128' || return 1
    expect '.services.mbsync.tmpfs == ["/tmp"]' || return 1
    expect '[.services.mbsync.volumes[] | "\(.source):\(.target)"] == ["maildir-volume:/maildir", "mbsync-state:/state"]' || return 1
    expect '.services.indexer.volumes[] | select(.target == "/maildir") | .read_only' || return 1
    expect '.services.mbsync | has("extra_hosts") | not' || return 1
}

macos_mode_composes_with_the_hardened_overlay() {
    render "$BASE" "$MACOS" "$HARDENED"
    expect '.services | keys == ["indexer", "mbsync", "mcp-server"]' || return 1
    expect '.networks["app-net"].internal' || return 1
    expect '.services.mbsync.environment.BRIDGE_HOST == "host.docker.internal"' || return 1
}

check "default mode runs the Bridge container and waits for its health" \
    default_mode_runs_the_bridge_container
check "default mode ignores the macOS port override" default_mode_ignores_the_macos_port_override
check "macOS mode starts only mbsync, indexer and mcp-server" \
    macos_mode_starts_only_mbsync_indexer_and_mcp_server
check "macOS mode drops only mbsync's Bridge dependency" macos_mode_drops_only_the_bridge_dependency
check "macOS mode points mbsync at the host app" macos_mode_points_mbsync_at_the_host_app
check "macOS mode takes the IMAP port from the environment" \
    macos_mode_takes_the_port_from_the_environment
check "macOS mode keeps mbsync's hardening and exposes no new port" \
    macos_mode_keeps_mbsync_hardening_and_exposure
check "macOS mode composes with the hardened overlay" macos_mode_composes_with_the_hardened_overlay

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
