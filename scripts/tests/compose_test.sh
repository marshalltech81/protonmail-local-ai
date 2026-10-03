#!/bin/bash
set -Eeuo pipefail

# Rendering checks for docker-compose.yml with and without the macOS Bridge
# overlay (#497): which services run, mbsync's Bridge dependency and
# endpoint, the hardening and exposure settings the overlay must keep, and
# that the Bridge pin copies match .env.example.
# The hardening is also checked on the merged config of every overlay
# combination the Makefile and docs use (#580): Semgrep reads each file on
# its own, so it cannot see an overlay's ``!reset`` or ``!override``.
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
readonly FIRST_RUN="docker-compose.first-run.yml"

# Renders the given compose files as JSON into $WORK/config.json. The
# environment is fixed so an operator's shell or .env cannot change the
# result; --env-file /dev/null keeps the repository's .env out. A path is
# relative to the repository unless absolute (the negative fixtures).
# ALL_PROFILES=1 also renders services an overlay moves into a profile.
render() {
    local args=() file
    for file in "$@"; do
        if [[ "$file" == /* ]]; then
            args+=(-f "$file")
        else
            args+=(-f "$ROOT_DIR/$file")
        fi
    done
    if [[ -n "${ALL_PROFILES:-}" ]]; then
        args+=(--profile "*")
    fi
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" BRIDGE_USER="synthetic@example.com" \
        ${BRIDGE_IMAP_PORT:+BRIDGE_IMAP_PORT="$BRIDGE_IMAP_PORT"} \
        ${BRIDGE_CERT_FINGERPRINT:+BRIDGE_CERT_FINGERPRINT="$BRIDGE_CERT_FINGERPRINT"} \
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

# A jq program printing one line per hardening violation in a render,
# checked against the base file's own render ($base). Every service must
# carry the required hardening and none of the forbidden settings
# (AGENTS.md "Network and exposure constraints" and "Container Runtime
# Hardening", as .semgrep/compose.yaml encodes them per file). A service
# the base defines must also keep what the base sets for it, so an
# overlay's !reset or !override cannot clear or loosen it.
# shellcheck disable=SC2016 # a jq program: its $ are jq's
readonly HARDENING_VIOLATIONS='
def set($k): has($k) and .[$k] != null and .[$k] != false and .[$k] != [] and .[$k] != {};
def list($k): .[$k] // [];
def bounded_logging:
    . != null and (.driver == "none" or .driver == "local"
        or (.driver == "json-file"
            and ((.options["max-size"] // "") | tostring | test("^[1-9][0-9]*[kmgKMG]?$"))));
. as $m
# Every service and network the base defines is still there; a network
# keeps its engine name, and no two networks share one, since services
# join networks by key and the name picks the engine network.
| ($base[0].services | keys[] | select(. as $k | $m.services | has($k) | not)
    | "\(.): missing from the merged config"),
  ($base[0].networks | to_entries[]
    | select(($m.networks[.key].name) != .value.name or $m.networks[.key].external == true)
    | "network \(.key): renamed or external"),
  ([$m.networks // {} | .[] | .name] | group_by(.)[] | select(length > 1)
    | "networks share the engine network \(.[0])"),
  # Services name secrets by key; the top-level definition picks what the
  # container reads (file, environment, external, name), so a secret any
  # service uses keeps the base definition unchanged.
  ([$m.services[] | list("secrets")[] | .source] | unique[] | . as $k
    | select($base[0].secrets | has($k))
    | select($m.secrets[$k] != $base[0].secrets[$k])
    | "secret \($k): differs from the base"),
  ($base[0].services as $b
| $m.services | to_entries[] | .key as $svc | .value as $s | $b[$svc] as $bs
| (
    (if $s.read_only != true then "read_only is not true" else empty end),
    (if ($s | list("cap_drop") | index("ALL")) == null then "cap_drop lacks ALL" else empty end),
    (if ($s | list("security_opt") | index("no-new-privileges:true")) == null
        then "security_opt lacks no-new-privileges:true" else empty end),
    ($s | list("security_opt")[]
        | select(test("(?i)^(seccomp|apparmor|systempaths)[:=]unconfined|^label[:=](disable|type:\\S*unconfined)|^no-new-privileges[:=]false"))
        | "security_opt weakens confinement: \(.)"),
    (if ($s.pids_limit | type) != "number" or $s.pids_limit < 1
        then "pids_limit is unbounded" else empty end),
    (if ($s.logging | bounded_logging) | not then "logging is unbounded" else empty end),
    ("privileged", "cap_add", "devices", "gpus", "device_cgroup_rules", "volumes_from",
     "extends", "network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup", "use_api_socket"
        | select(. as $k | $s | set($k)) | "sets \(.)"),
    (if ($s.user // "" | tostring | test("^(root|0+)(:|$)")) then "runs as root" else empty end),
    ($s | list("volumes")[] | select((.source // "") | test("docker\\.sock"))
        | "mounts the docker socket"),
    ($s | list("volumes")[] | select(.type == "bind" and .read_only != true)
        | "writable bind mount at \(.target)"),
    (if $svc != "mcp-server" and ($s | list("ports")) != []
        then "publishes a port" else empty end),
    (if $svc == "mcp-server"
        and ($s | list("ports") | map("\(.host_ip):\(.published):\(.target)/\(.protocol)"))
            != ["127.0.0.1:3000:3000/tcp"]
        then "ports are not only 127.0.0.1:3000:3000/tcp" else empty end),
    (if [$s | list("post_start")[], list("pre_stop")[] | select(.privileged == true)] != []
        then "runs a privileged lifecycle hook" else empty end),
    (if ($s.deploy.resources.reservations.devices // []) != []
        then "reserves devices" else empty end),
    (if ($s.networks // {} | has("bridge-net"))
        and ($svc | IN("protonmail-bridge", "mbsync") | not)
        then "joins bridge-net" else empty end),
    # A service the base does not define must name its non-root user (an
    # unset user is the image default, possibly root) and gets no secrets or base volumes.
    (if $bs == null then
        (if $s.user == null then "sets no user" else empty end),
        (if ($s | list("secrets")) != [] then "uses a secret" else empty end),
        ($s | list("volumes")[] | .source // "" | select(. as $v | $base[0].volumes | has($v))
            | "mounts the base volume \(.)")
    else
        # Another image or build may default to root, so it must name a
        # user (the root check above rejects a root one).
        (if ($s.image != $bs.image or $s.build != $bs.build) and $s.user == null
            then "changes its image or build and sets no user" else empty end),
        ($s | list("secrets")[] | .source as $src
            | select([$bs | list("secrets")[] | select(.source == $src)] == [])
            | "gains secret \($src)"),
        (if $bs.init == true and $s.init != true then "init is not true" else empty end),
        ($bs | list("cap_drop")[] | select(. as $c | $s | list("cap_drop") | index($c) == null)
            | "cap_drop lost \(.)"),
        ($bs | list("security_opt")[]
            | select(. as $o | $s | list("security_opt") | index($o) == null)
            | "security_opt lost \(.)"),
        (if ($s.pids_limit | type) == "number" and $s.pids_limit > $bs.pids_limit
            then "pids_limit is above the base" else empty end),
        (if $bs.mem_limit != null
            and ($s.mem_limit == null or ($s.mem_limit | tonumber) > ($bs.mem_limit | tonumber))
            then "mem_limit is missing or above the base" else empty end),
        ($s.networks // {} | keys[] | select(. as $n | $bs.networks // {} | has($n) | not)
            | "joins network \(.), which the base does not give it"),
        ($s | list("volumes")[] | . as $v
            | select([$bs | list("volumes")[]
                | select(.type == $v.type and .source == $v.source and .target == $v.target)] == [])
            | "mounts \($v.source // $v.type) at \($v.target), which the base does not"),
        ($bs | list("volumes")[] | select(.read_only == true) | .target as $t
            | select([$s | list("volumes")[] | select(.target == $t and .read_only == true)] == [])
            | "volume at \($t) is no longer read-only")
    end)
  )
| "\($svc): \(.)")
'

# Renders the given compose files with every profile and fails, listing
# the violations, unless the merged config keeps the hardening.
expect_merged_hardening() {
    ALL_PROFILES=1 render "$@" || return 1
    jq -r --slurpfile base "$WORK/base.json" "$HARDENING_VIOLATIONS" "$WORK/config.json" \
        >"$WORK/violations" || return 1
    if [[ -s "$WORK/violations" ]]; then
        cat "$WORK/violations"
        return 1
    fi
}

# Negative fixtures: writes the overlay on stdin to $WORK/<name>.yml,
# merges it over the base file and passes only if the hardening check
# fails with each expected violation among those it lists.
expect_overlay_rejected() {
    local name="$1" expected
    shift
    cat >"$WORK/$name.yml"
    if expect_merged_hardening "$BASE" "$WORK/$name.yml" >"$WORK/rejected" 2>&1; then
        printf 'overlay %s passed the hardening check\n' "$name"
        return 1
    fi
    for expected in "$@"; do
        if ! grep -qxF "$expected" "$WORK/rejected"; then
            printf 'expected violation: %s\ngot:\n' "$expected"
            cat "$WORK/rejected"
            return 1
        fi
    done
}

# Every combination the Makefile targets and the overlays' usage notes
# run: make up/build (base), make first-run, the hardened overlay, the
# macOS Bridge targets, and macOS mode with the hardened overlay.
merged_hardening_holds_for_every_overlay_combination() {
    expect_merged_hardening "$BASE" || return 1
    expect_merged_hardening "$BASE" "$FIRST_RUN" || return 1
    expect_merged_hardening "$BASE" "$HARDENED" || return 1
    expect_merged_hardening "$BASE" "$MACOS" || return 1
    expect_merged_hardening "$BASE" "$MACOS" "$HARDENED" || return 1
}

# make first-run must keep Bridge's log driver at none: its `info` output
# holds the Bridge credentials. Bounded logging alone would accept json-file.
first_run_logging_disabled() {
    render "$BASE" "$FIRST_RUN" "$@" || return 1
    expect '.services["protonmail-bridge"].logging.driver == "none"'
}

first_run_keeps_bridge_logging_disabled() {
    first_run_logging_disabled || return 1
    cat >"$WORK/first-run-logging.yml" <<'EOF'
services:
  protonmail-bridge:
    logging: !reset null
EOF
    if first_run_logging_disabled "$WORK/first-run-logging.yml" >/dev/null; then
        printf 'a reset first-run log driver passed\n'
        return 1
    fi
}

# Settings the base never uses, so its own render cannot vouch for them:
# a new service's user and secrets, lifecycle hooks, device reservations.
merged_hardening_rejects_new_grants() {
    expect_overlay_rejected new-grants \
        "extra: sets no user" "extra: uses a secret" \
        "extra: runs a privileged lifecycle hook" "extra: reserves devices" \
        "mcp-server: gains secret bridge_pass" <<'EOF'
services:
  mcp-server:
    secrets: [bridge_pass]
  extra:
    image: example.invalid/extra:1
    init: true
    read_only: true
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    pids_limit: 16
    logging:
      driver: none
    networks: [app-net]
    secrets: [embed_api_key]
    post_start:
      - command: ["true"]
        privileged: true
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
EOF
}

merged_hardening_rejects_reset_security_opt() {
    expect_overlay_rejected reset-security-opt \
        "mbsync: security_opt lacks no-new-privileges:true" <<'EOF'
services:
  mbsync:
    security_opt: !reset []
EOF
}

merged_hardening_rejects_reset_cap_drop() {
    expect_overlay_rejected reset-cap-drop "indexer: cap_drop lacks ALL" <<'EOF'
services:
  indexer:
    cap_drop: !reset []
EOF
}

merged_hardening_rejects_reset_read_only() {
    expect_overlay_rejected reset-read-only "mcp-server: read_only is not true" <<'EOF'
services:
  mcp-server:
    read_only: !reset null
EOF
}

merged_hardening_rejects_reset_pids_limit_and_logging() {
    expect_overlay_rejected reset-pids-limit \
        "protonmail-bridge: pids_limit is unbounded" <<'EOF' || return 1
services:
  protonmail-bridge:
    pids_limit: !reset null
EOF
    expect_overlay_rejected reset-logging "mbsync: logging is unbounded" <<'EOF'
services:
  mbsync:
    logging: !reset null
EOF
}

merged_hardening_rejects_override_security_opt() {
    expect_overlay_rejected override-security-opt \
        "indexer: security_opt lacks no-new-privileges:true" <<'EOF'
services:
  indexer:
    security_opt: !override
      - label:type:container_t
EOF
}

merged_hardening_rejects_override_of_a_read_only_volume() {
    expect_overlay_rejected override-volumes \
        "indexer: volume at /maildir is no longer read-only" <<'EOF'
services:
  indexer:
    volumes: !override
      - maildir-volume:/maildir
EOF
}

merged_hardening_rejects_override_ports_and_networks() {
    expect_overlay_rejected override-ports \
        "mcp-server: ports are not only 127.0.0.1:3000:3000/tcp" <<'EOF' || return 1
services:
  mcp-server:
    ports: !override
      - "3000:3000"
EOF
    expect_overlay_rejected override-ports-udp \
        "mcp-server: ports are not only 127.0.0.1:3000:3000/tcp" <<'EOF' || return 1
services:
  mcp-server:
    ports: !override
      - "127.0.0.1:3000:3000/udp"
EOF
    expect_overlay_rejected override-networks "mcp-server: joins bridge-net" <<'EOF'
services:
  mcp-server:
    networks: !override
      - bridge-net
EOF
}

# The forbidden settings, which Semgrep also rejects file by file: the
# merged check must not rely on that.
merged_hardening_rejects_forbidden_settings() {
    expect_overlay_rejected forbidden \
        "indexer: sets privileged" "indexer: sets cap_add" "indexer: sets devices" \
        "indexer: sets network_mode" "indexer: sets volumes_from" "indexer: runs as root" \
        "indexer: mounts the docker socket" "indexer: writable bind mount at /host-etc" \
        "indexer: publishes a port" \
        "indexer: security_opt weakens confinement: seccomp:unconfined" \
        "indexer: pids_limit is above the base" <<'EOF'
services:
  indexer:
    privileged: true
    cap_add: [NET_ADMIN]
    devices: ["/dev/fuse:/dev/fuse"]
    network_mode: host
    networks: !reset null
    volumes_from: [protonmail-bridge]
    user: "0:0"
    pids_limit: 4096
    security_opt: [seccomp:unconfined]
    ports: ["127.0.0.1:1143:1143"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /etc:/host-etc
EOF
}

# Services name secrets by key; the top-level definition picks the file.
merged_hardening_rejects_redefined_secrets() {
    expect_overlay_rejected secret-definitions \
        "secret embed_api_key: differs from the base" \
        "secret rerank_api_key: differs from the base" <<'EOF'
secrets:
  embed_api_key:
    file: ./.secrets/bridge_pass.txt
  rerank_api_key: !override
    environment: BRIDGE_PASS
EOF
}

# Another image may default to root; the base's images set their own user.
merged_hardening_rejects_a_new_image_without_a_user() {
    expect_overlay_rejected new-image \
        "indexer: changes its image or build and sets no user" \
        "mbsync: changes its image or build and sets no user" <<'EOF'
services:
  indexer:
    build: !reset null
    image: example.invalid/rootful:1
  mbsync:
    build:
      dockerfile: Dockerfile.rootful
EOF
}

# A named volume is neither a bind nor the docker socket, but mounting
# another service's volume hands over its data, to an existing service or
# a new one.
merged_hardening_rejects_added_volumes() {
    expect_overlay_rejected added-volumes \
        "mcp-server: mounts bridge-data at /bridge, which the base does not" \
        "indexer: mounts sqlite-volume at /extra, which the base does not" \
        "extra: mounts the base volume bridge-data" <<'EOF'
services:
  mcp-server:
    volumes:
      - bridge-data:/bridge:ro
  indexer:
    volumes:
      - sqlite-volume:/extra:ro
  extra:
    image: example.invalid/extra:1
    user: "1234:1234"
    volumes:
      - bridge-data:/bridge:ro
EOF
}

# A decimal UID of zeros is still root.
merged_hardening_rejects_leading_zero_root_users() {
    expect_overlay_rejected zero-users \
        "indexer: runs as root" "mbsync: runs as root" <<'EOF'
services:
  indexer:
    user: "00"
  mbsync:
    user: "000:000"
EOF
}

merged_hardening_rejects_override_dropping_a_service() {
    expect_overlay_rejected override-services "indexer: missing from the merged config" <<'EOF'
services: !override
  mcp-server:
    image: example.invalid/mcp:1
EOF
}

# max-size bounds only the json-file driver's files.
merged_hardening_rejects_max_size_on_another_logging_driver() {
    expect_overlay_rejected logging-driver "mbsync: logging is unbounded" <<'EOF'
services:
  mbsync:
    logging: !override
      driver: syslog
      options:
        max-size: "10m"
EOF
}

# Services join networks by key; a network's name picks the engine network.
merged_hardening_rejects_renamed_networks() {
    expect_overlay_rejected network-names \
        "network app-net: renamed or external" "network bridge-net: renamed or external" \
        "networks share the engine network shared-net" <<'EOF'
networks:
  app-net:
    name: shared-net
  bridge-net:
    name: shared-net
EOF
}

# docker compose config resolves a top-level include (#577), so the merged
# check sees the services an included fragment brings in.
merged_hardening_rejects_an_included_service() {
    cat >"$WORK/fragment.yml" <<'EOF'
services:
  rogue:
    image: example.invalid/rogue:1
    ports: ["8080:80"]
    networks: [bridge-net]
networks:
  bridge-net:
    driver: bridge
EOF
    expect_overlay_rejected include \
        "rogue: read_only is not true" "rogue: cap_drop lacks ALL" \
        "rogue: publishes a port" "rogue: joins bridge-net" <<EOF
include:
  - $WORK/fragment.yml
EOF
}

default_mode_runs_the_bridge_container() {
    render "$BASE"
    expect '.services | keys == ["indexer", "mbsync", "mcp-server", "protonmail-bridge"]' || return 1
    expect '.services.mbsync.depends_on["protonmail-bridge"].condition == "service_healthy"' || return 1
    expect '.services.mbsync.environment.BRIDGE_HOST == "protonmail-bridge"' || return 1
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1143"' || return 1
    expect '.services.mbsync.environment | has("BRIDGE_CERT_HOST") | not' || return 1
    expect '.services.mbsync.environment | has("BRIDGE_CERT_FINGERPRINT") | not' || return 1
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
    # Empty unless set: the entrypoint then refuses to trust the app's
    # certificate on first use.
    expect '.services.mbsync.environment.BRIDGE_CERT_FINGERPRINT == ""' || return 1
}

macos_mode_takes_the_expected_fingerprint_from_the_environment() {
    BRIDGE_CERT_FINGERPRINT="ab:cd" render "$BASE" "$MACOS"
    expect '.services.mbsync.environment.BRIDGE_CERT_FINGERPRINT == "ab:cd"' || return 1
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

# .env.example is the source of truth for the Bridge pin. Compose and the
# Dockerfile cannot read it, so the docker-compose.yml build-arg fallbacks
# (used when .env leaves a key unset) and the bridge/Dockerfile ARG
# defaults are copies that must equal it.
bridge_pin_copies_match_env_example() {
    local name source copy
    render "$BASE"
    for name in BRIDGE_VERSION BRIDGE_COMMIT; do
        source="$(grep -E "^${name}=" "$ROOT_DIR/.env.example" | head -n 1 | cut -d= -f2-)"
        [[ -n "$source" ]] || {
            printf '%s is missing from .env.example\n' "$name"
            return 1
        }
        expect ".services[\"protonmail-bridge\"].build.args.${name} == \"${source}\"" || return 1
        copy="$(grep -E "^ARG ${name}=" "$ROOT_DIR/bridge/Dockerfile" | head -n 1 | cut -d= -f2-)"
        [[ "$copy" == "$source" ]] || {
            printf 'bridge/Dockerfile ARG %s=%s, .env.example has %s\n' "$name" "$copy" "$source"
            return 1
        }
    done
}

# The base file's own render, every profile included: what the merged
# hardening check holds each service to.
ALL_PROFILES=1 render "$BASE"
cp "$WORK/config.json" "$WORK/base.json"

check "default mode runs the Bridge container and waits for its health" \
    default_mode_runs_the_bridge_container
check "default mode ignores the macOS port override" default_mode_ignores_the_macos_port_override
check "macOS mode starts only mbsync, indexer and mcp-server" \
    macos_mode_starts_only_mbsync_indexer_and_mcp_server
check "macOS mode drops only mbsync's Bridge dependency" macos_mode_drops_only_the_bridge_dependency
check "macOS mode points mbsync at the host app" macos_mode_points_mbsync_at_the_host_app
check "macOS mode takes the IMAP port from the environment" \
    macos_mode_takes_the_port_from_the_environment
check "macOS mode takes the expected fingerprint from the environment" \
    macos_mode_takes_the_expected_fingerprint_from_the_environment
check "macOS mode keeps mbsync's hardening and exposes no new port" \
    macos_mode_keeps_mbsync_hardening_and_exposure
check "macOS mode composes with the hardened overlay" macos_mode_composes_with_the_hardened_overlay
check "Bridge pin copies match .env.example" bridge_pin_copies_match_env_example
check "merged hardening holds for every overlay combination" \
    merged_hardening_holds_for_every_overlay_combination
check "merged hardening rejects !reset on security_opt" merged_hardening_rejects_reset_security_opt
check "merged hardening rejects !reset on cap_drop" merged_hardening_rejects_reset_cap_drop
check "merged hardening rejects !reset on read_only" merged_hardening_rejects_reset_read_only
check "merged hardening rejects !reset on pids_limit and logging" \
    merged_hardening_rejects_reset_pids_limit_and_logging
check "merged hardening rejects !override on security_opt" \
    merged_hardening_rejects_override_security_opt
check "merged hardening rejects !override of a read-only volume" \
    merged_hardening_rejects_override_of_a_read_only_volume
check "merged hardening rejects !override on ports and networks" \
    merged_hardening_rejects_override_ports_and_networks
check "merged hardening rejects the forbidden settings" merged_hardening_rejects_forbidden_settings
check "merged hardening rejects redefined secrets" merged_hardening_rejects_redefined_secrets
check "merged hardening rejects a new image or build without a user" \
    merged_hardening_rejects_a_new_image_without_a_user
check "merged hardening rejects volumes an overlay adds" merged_hardening_rejects_added_volumes
check "merged hardening rejects leading-zero root users" \
    merged_hardening_rejects_leading_zero_root_users
check "merged hardening rejects an !override that drops a service" \
    merged_hardening_rejects_override_dropping_a_service
check "merged hardening rejects max-size on a driver other than json-file" \
    merged_hardening_rejects_max_size_on_another_logging_driver
check "merged hardening rejects renamed or shared networks" merged_hardening_rejects_renamed_networks
check "first run keeps Bridge's log driver at none" first_run_keeps_bridge_logging_disabled
check "merged hardening rejects new users, secrets, hooks and devices" \
    merged_hardening_rejects_new_grants
check "merged hardening rejects a service a top-level include brings in" \
    merged_hardening_rejects_an_included_service

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
