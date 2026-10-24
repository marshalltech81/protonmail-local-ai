#!/bin/bash
set -Eeuo pipefail

# Rendering checks for docker-compose.yml (#497): which services run,
# mbsync's endpoint (the Bridge app on the host), and the hardening and
# exposure settings every service keeps.
# The hardening is also checked on the merged config of every overlay
# combination the Makefile and docs use (#580): Semgrep reads each file on
# its own, so it cannot see an overlay's ``!reset`` or ``!override``.
# The same check covers the operator configuration translator's
# compose.accounts.yml from synthetic configurations (#1608).
# Uses ``docker compose config``, which needs the Compose CLI but no
# daemon, images or secret files, and Python 3.11 or later for the
# translator.
#
# Run: bash scripts/tests/compose_test.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

readonly BASE="docker-compose.yml"
readonly HARDENED="docker-compose.hardened.yml"

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
        ${GIT_COMMIT:+GIT_COMMIT="$GIT_COMMIT"} \
        ${EMBED_BASE_URL:+EMBED_BASE_URL="$EMBED_BASE_URL"} \
        ${EMBED_MODEL:+EMBED_MODEL="$EMBED_MODEL"} \
        ${ACCOUNT_SCOPE:+ACCOUNT_SCOPE="$ACCOUNT_SCOPE"} \
        ${RENDER_MCP_PORT:+MCP_PORT="$RENDER_MCP_PORT"} \
        ${RENDER_ENV[@]+"${RENDER_ENV[@]}"} \
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

# Hardening every service keeps, plus the one published
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
# Unlike the list/map fields set() covers, Compose merges entrypoint and
# command by replacement, not by appending: an override of [] explicitly
# resets either to empty rather than leaving the previous layer alone, so
# set()s != [] would wrongly call that an unset key (review round 4).
# Checked directly against docker compose config: entrypoint: [] /
# command: [] renders as [] even for a service only one layer defines.
def overridden($k): has($k) and .[$k] != null;
def list($k): .[$k] // [];
# A service own effective replica count, one when neither field is set.
# Docker refuses to scale a service with a fixed container_name (every
# service here sets one) beyond one container, so a plain up fails
# instead of starting it; the activation checks below rejected only a
# zero count, which an overlay or renderer regression raising either
# field above one still passed (review round 8, finding 1).
def replicas($s): $s.scale // $s.deploy.replicas // 1;
def bounded_logging:
    . != null and (.driver == "none" or .driver == "local"
        or (.driver == "json-file"
            and ((.options["max-size"] // "") | tostring | test("^[1-9][0-9]*[kmgKMG]?$"))));
. as $m
# Generated collectors (#1608): $collectors lists the enabled accounts a
# configuration declares (scripts/config/tests/synthetic_compose.py),
# and is empty for the files in the repository. Each account other than
# the one at /maildir gets a collector named by its scope UUID, which may
# use exactly its own password secret, Maildir and state volumes and
# network ($own, below); the account at /maildir keeps the base own
# "mbsync" name and resources instead of scoped ones. Both are held to
# base mbsync own implementation and trust values further below
# ($declared) -- $own alone missed the /maildir account even when it is
# itself generated by the translator (review round 6, finding 1).
| ($collectors[0] | map(select(.maildir_root != "/maildir") | .scope_uuid as $u
    | {service: "mbsync-\($u)", secret: "bridge_pass_\($u)", # pragma: allowlist secret
       volumes: {"/maildir": "maildir-\($u)", "/state": "mbsync-state-\($u)"},
       network: "bridge-net-\($u)", password_file,
       # The declared environment this account collector must carry,
       # compared below value-for-value against what is rendered (review
       # round 6, finding 3). It already includes BRIDGE_CERT_FINGERPRINT,
       # so this one check also covers the trust-value check round 5
       # (finding 3) checked separately: it comes from collector_env(),
       # the same function render_compose() calls, so declaration and
       # render agree by construction. Pin rotation is a one-run command,
       # not configuration (#1609): collector_env() sets no environment
       # key for it at all.
       environment})) as $own
# Every declared collector, the base mbsync included when an account is at
# /maildir, keeps each of its own resources. The /maildir entry carries
# the same shape $own does (including the declared environment), just
# with base own names, so both can be compared to base mbsync identically
# below (review round 6, finding 1).
| (($collectors[0] | map(select(.maildir_root == "/maildir")
      | {service: "mbsync", secret: "bridge_pass", # pragma: allowlist secret
         volumes: {"/maildir": "maildir-volume", "/state": "mbsync-state"},
         network: "bridge-net", password_file, environment}))
    + $own) as $declared
# Every Maildir volume of an account and the one service that may write it
# (#1665): the legacy account base volume and mbsync, and the own volume of
# each generated collector. Derived from the declared accounts and the base
# file, never from the render under test.
| ([{volume: "maildir-volume", owner: "mbsync"}]
    + [$own[] | {volume: .volumes["/maildir"], owner: .service}]) as $maildirs
# Every service and network the base defines is still there; a network
# keeps its engine name, and no two networks share one, since services
# join networks by key and the name picks the engine network.
| ($base[0].services | keys[] | select(. as $k | $m.services | has($k) | not)
    | "\(.): missing from the merged config"),
  # Every base service also starts with no profile active ($active), as
  # the Makefile runs Compose, and at exactly one replica (review round
  # 8, finding 1): a fixed container_name (every service sets one)
  # makes Docker refuse a second container of it, so a plain up fails
  # for that service instead of starting it, the same as zero.
  ($base[0].services | keys[] | . as $k | $active[0].services[$k] as $a
    | select($a == null or replicas($a) != 1)
    | "\($k): not started by a plain up"),
  ($base[0].networks | to_entries[]
    | select(($m.networks[.key].name) != .value.name or $m.networks[.key].external == true)
    | "network \(.key): renamed or external"),
  # A network also keeps its driver, driver options and addressing: a
  # macvlan with LAN IPAM keeps the name but puts its members on the LAN.
  # (internal: true, which the hardened overlay sets, is not compared.)
  ($base[0].networks | to_entries[]
    | select(($m.networks[.key] // {} | {driver, driver_opts, ipam})
        != (.value | {driver, driver_opts, ipam}))
    | "network \(.key): driver settings differ from the base"),
  ([$m.networks // {} | .[] | .name] | group_by(.)[] | select(length > 1)
    | "networks share the engine network \(.[0])"),
  # The same for volumes, which services mount by key; a volume also keeps
  # its driver settings, since local driver_opts can back it with a host
  # path.
  ($base[0].volumes | to_entries[]
    | select(($m.volumes[.key].name) != .value.name or $m.volumes[.key].external == true)
    | "volume \(.key): renamed or external"),
  ($base[0].volumes | to_entries[]
    | select(($m.volumes[.key] // {} | {driver, driver_opts})
        != (.value | {driver, driver_opts}))
    | "volume \(.key): driver settings differ from the base"),
  ([$m.volumes // {} | .[] | .name] | group_by(.)[] | select(length > 1)
    | "volumes share the engine volume \(.[0])"),
  # Services name secrets by key; the top-level definition picks what the
  # container reads (file, environment, external, name), so a secret any
  # service uses keeps the base definition unchanged.
  ([$m.services[] | list("secrets")[] | .source] | unique[] | . as $k
    | select($base[0].secrets | has($k))
    | select($m.secrets[$k] != $base[0].secrets[$k])
    | "secret \($k): differs from the base"),
  # Each declared collector is there, starts with no profile active, and
  # uses only its own secret, volumes (at /maildir and /state) and
  # network. The new-service checks below still apply to a non-legacy
  # one, except that it may use its own secret; the legacy "mbsync"
  # entry already has one, in $base, so those checks apply to it the
  # usual way instead.
  ($declared[] | . as $c | $m.services[$c.service] as $s
    | if $s == null then "\($c.service): declared collector missing from the merged config"
      else
        ($active[0].services[$c.service] as $a
            | select($a == null or replicas($a) != 1)
            | "\($c.service): declared collector not started by a plain up"),
        # A second secrets entry can repeat the collector own correct
        # source at a different, absolute target (Compose allows
        # mounting one secret at several targets): the "lacks its own
        # secret" presence check below still finds its one
        # bridge_pass-targeted entry and passes, while the extra mount
        # can overwrite /entrypoint.sh or another in-container file with
        # the mounted password bytes (review round 8, finding 2,
        # security). Every entry naming the collector own secret must
        # target bridge_pass; one naming another secret is unchanged.
        ($s | list("secrets")[]
            | if .source != $c.secret then "\($c.service): uses secret \(.source), not its own"
              elif ((.target // .source) | ltrimstr("/run/secrets/")) != "bridge_pass"
                then "\($c.service): mounts its own secret \(.source) at \(.target // .source), not bridge_pass"
              else empty end),
        ($s | list("volumes")[] | select(.type != "volume" or $c.volumes[.target] != .source)
            | "\($c.service): mounts \(.source // .type) at \(.target), not its own Maildir or state volume"),
        # The key check alone passed a collector or overlay that kept
        # its own network but gave it a static ipv4_address (or another
        # per-attachment field): Docker requires the top-level network
        # to declare an IPAM subnet covering it, but the IPAM-identity
        # check below holds this network own IPAM to the subnet-less
        # base unchanged, so such a render cannot even start the
        # collector -- a design-time check catching it before Compose
        # does (review round 9). Compare the attachment value too, held
        # to mbsync own (empty) attachment to bridge-net.
        ($s.networks // {} | to_entries[]
            | if .key != $c.network then "\($c.service): joins network \(.key), not its own"
              elif .value != ($base[0].services["mbsync"].networks["bridge-net"])
                then "\($c.service): network \($c.network) sets a static attachment"
              else empty end),
        # A non-legacy declared collector has no entry of its own in
        # $base, so the existing-service checks below never run for it:
        # held only to the shape checks above, an overlay could reset
        # its init or raise its pids_limit past 128 while the same
        # mutation on the legacy mbsync is rejected (review round 2,
        # finding 1), and the secret/volume/network exception above
        # would be granted to any service merely named mbsync-<uuid>,
        # whatever image or entrypoint it runs (review round 2, finding
        # 2). Compare it against base mbsync directly for both. mbsync
        # itself sets none of healthcheck, post_start, pre_stop or
        # configs (its healthcheck is baked into the image,
        # mbsync/Dockerfile); any of the four on a collector is its own
        # alternate in-container execution path, running with the same
        # Bridge password, Maildir/state and network a collector keeps,
        # just as an entrypoint or command override would (review round
        # 3). extra_hosts is the same gap under a routing field rather
        # than an execution one: a dedicated check already holds the
        # legacy mbsync to it (mbsync_keeps_its_hardening_and_no_port_is_exposed),
        # but nothing held a collector to it, and AGENTS.md forbids
        # extra_hosts on any mbsync-family service without the #1499
        # decision (review round 3). Three more gaps in the same
        # "runs with the same password, Maildir/state and network a
        # collector keeps" class (review round 4): entrypoint/command
        # checked with set() missed the explicit [] override (see
        # overridden() above); nothing compared the rendered user
        # against the fixed UID the image sets (mbsync/Dockerfile,
        # AGENTS.md "current expected UIDs"), so a substituted non-root
        # UID that cannot write /maildir or /state still passed; and
        # environment was not compared at all, so a startup-hook
        # variable Bash itself reads (BASH_ENV and the like) could run
        # injected code reading the mounted secret before
        # /entrypoint.sh does anything. This same comparison now runs
        # for the legacy "mbsync" entry too, alongside its own
        # existing-service checks below: a $declared iteration (not
        # $own, which excludes the /maildir account) catches a legacy
        # mbsync generated by the translator with a substituted image,
        # execution-path override, environment key or trust value, a
        # gap the weaker existing-service checks missed because they
        # accept a changed image or build whenever *any* non-root user
        # is set, regardless of which one (review round 6, finding 1,
        # security).
        ($base[0].services["mbsync"] as $bbase
            | (if $bbase.init == true and $s.init != true
                then "\($c.service): init is not true" else empty end),
              # The mbsync entrypoint exits after a persistent failure so
              # the failure is visible rather than silently retried
              # forever (AGENTS.md "add a max-retry limit"); Docker own
              # restart policy is what brings it back for a transient
              # one. restart was never compared, so an overlay resetting
              # or omitting it left that account permanently unsynced
              # after the first such exit instead (review round 6,
              # finding 2).
              (if $s.restart != $bbase.restart
                then "\($c.service): restart differs from mbsync" else empty end),
              # depends_on was never compared either, so a collector could
              # gain one on mbsync (or another account) while staying
              # present and unscaled, which the checks above still accept.
              # service_completed_successfully waits for the dependency to
              # exit (Docker own docs), but mbsync sync loop runs
              # indefinitely once healthy, so the dependent collector would
              # wait forever and never sync (review round 7). The base
              # mbsync depends on nothing, so every collector must match
              # that exactly.
              (if ($s.depends_on // {}) != ($bbase.depends_on // {})
                then "\($c.service): depends_on differs from mbsync" else empty end),
              # logging was never compared here, and the generic
              # bounded_logging predicate below explicitly accepts
              # driver: none, so a collector or overlay could disable
              # logging outright and still pass every check: Bridge
              # connection failures, TLS refusals, retries and recovery
              # for that account would then never reach the log (review
              # round 8, finding 3). Held to mbsync own logging
              # exactly, not merely "bounded", since the base own
              # setting is the operator chosen retention.
              (if $s.logging != $bbase.logging
                then "\($c.service): logging differs from mbsync" else empty end),
              # Neither was compared either. mbsync own entrypoint traps
              # TERM and INT and waits while forwarding TERM to the
              # active child (mbsync/entrypoint.sh), so a shutdown or
              # recreation can finish an in-progress isync run; a
              # collector or overlay setting stop_signal to a signal
              # the entrypoint does not trap, or stop_grace_period to a
              # period too short to wait for it, bypasses that and
              # Docker kills the container immediately or immediately
              # after the signal, despite every other check here still
              # passing (review round 9).
              (if $s.stop_signal != $bbase.stop_signal
                then "\($c.service): stop_signal differs from mbsync" else empty end),
              (if $s.stop_grace_period != $bbase.stop_grace_period
                then "\($c.service): stop_grace_period differs from mbsync" else empty end),
              ($bbase | list("cap_drop")[]
                | select(. as $cd | $s | list("cap_drop") | index($cd) == null)
                | "\($c.service): cap_drop lost \(.)"),
              ($bbase | list("security_opt")[]
                | select(. as $o | $s | list("security_opt") | index($o) == null)
                | "\($c.service): security_opt lost \(.)"),
              # The mbsync entrypoint creates /tmp/mbsync on every start
              # under the read-only root filesystem; neither the generic checks
              # nor this block required tmpfs, so a collector could omit or
              # reset it and fail at startup despite passing every other
              # check (review round 5, finding 1).
              ($bbase | list("tmpfs")[]
                | select(. as $t | $s | list("tmpfs") | index($t) == null)
                | "\($c.service): tmpfs lost \(.)"),
              # The retention check above only rejects a missing entry;
              # a collector or overlay could append another tmpfs path
              # (Compose appends the list rather than replacing it) and
              # still keep /tmp, mounting a filesystem over whatever the
              # image has there -- hiding
              # /usr/local/lib/proton-settings.sh is one example, and
              # the image own read-only guarantee narrows by exactly
              # that path either way (review round 8, finding 4). The
              # collector own tmpfs list must match mbsync own exactly,
              # not merely a superset of it.
              ($s | list("tmpfs")[]
                | select(. as $t | $bbase | list("tmpfs") | index($t) == null)
                | "\($c.service): tmpfs gained \(.)"),
              (if ($s.pids_limit | type) == "number" and $s.pids_limit > $bbase.pids_limit
                then "\($c.service): pids_limit is above the base" else empty end),
              (if $bbase.mem_limit != null
                    and ($s.mem_limit == null or ($s.mem_limit | tonumber) > ($bbase.mem_limit | tonumber))
                then "\($c.service): mem_limit is missing or above the base" else empty end),
              (if $s.image != $bbase.image or $s.build != $bbase.build
                then "\($c.service): image or build differs from mbsync" else empty end),
              # A service may keep its own image or build and still set
              # provider: Compose then delegates its lifecycle to an
              # external provider component instead of running that
              # image directly, so `docker compose up` would invoke the
              # provider (or fail when it is unavailable) rather than
              # start mbsync, despite image and build staying identical
              # (review round 9). mbsync own entry never sets one.
              (if $s.provider != null then "\($c.service): sets a provider" else empty end),
              # A null user is the legacy mbsync own legitimate
              # rendering (the translator never sets one for it, relying
              # on the image own USER mbsync, mbsync/Dockerfile, the
              # same default base own "mbsync" entry relies on); a
              # non-legacy collector own "sets no user" check (the
              # new-service branch above) already rejects null for it
              # separately, so allowing null here only accepts the
              # legacy case, not a reset collector (review round 6,
              # finding 1).
              (if $s.user != null and $s.user != "1001:1001"
                then "\($c.service): user is not the mbsync UID 1001:1001" else empty end),
              (if ($s | overridden("entrypoint")) then "\($c.service): sets an entrypoint override" else empty end),
              (if ($s | overridden("command")) then "\($c.service): sets a command override" else empty end),
              (if ($s | set("healthcheck")) then "\($c.service): sets a healthcheck override" else empty end),
              # pre_start is the same risk as post_start/pre_stop (Compose
              # added it after that check was written): it may select
              # another image, but inherits this service own environment
              # and joins the same network and volumes, so it can run
              # arbitrary code with the Bridge password, Maildir/state
              # and bridge-net access a collector keeps, before mbsync
              # itself starts (review round 9). Untested here: pre_start
              # is too recent a Compose Specification field (Docker
              # Compose v5.3.0+) for the CI runner image own bundled
              # Compose (2.38.2, confirmed via its own readme), which
              # refuses the property with a schema error before this
              # check -- or any jq logic -- ever runs, so no render in
              # this suite can carry it. Verified by hand against
              # Docker Compose v5.5.0 (reverting this line reopens the
              # gap); #1631 tracks giving it the same CI coverage every
              # other check here has.
              (if ($s | set("pre_start")) then "\($c.service): sets a pre_start hook" else empty end),
              (if ($s | set("post_start")) then "\($c.service): sets a post_start hook" else empty end),
              (if ($s | set("pre_stop")) then "\($c.service): sets a pre_stop hook" else empty end),
              (if ($s | set("configs")) then "\($c.service): mounts a config" else empty end),
              (if ($s | set("extra_hosts")) then "\($c.service): sets extra_hosts" else empty end),
              ($s.environment // {} | keys[] | select(. as $k | ($bbase.environment // {} | has($k)) | not)
                | "\($c.service): environment sets \(.), which mbsync does not set"),
              # The key-name check above only rejects an extra key; it never
              # required the account own declared values to actually be
              # rendered. A renderer regression or overlay could drop
              # BRIDGE_USER, or keep the key with an empty value, while
              # mbsync itself still sets BRIDGE_USER, so the check above
              # saw nothing wrong -- the entrypoint exits on an empty
              # BRIDGE_USER, so the account could never start (review
              # round 6, finding 3). $c.environment is this account own
              # declaration (collector_env(), scripts/config/operator_config.py,
              # the same values the translator renders), so every key it
              # sets must come through with its declared value, not just
              # BRIDGE_USER.
              # This also covers the trust-value check round 5 (finding 3,
              # security) checked separately and more narrowly at the
              # time -- BRIDGE_CERT_FINGERPRINT -- since it is already
              # part of $c.environment (collector_env() always sets it),
              # so comparing every declared key catches a substituted
              # fingerprint the same way, without a separate check. Pin
              # rotation is a one-run command, not configuration (#1609):
              # collector_env() sets no environment key for it, so an
              # extra BRIDGE_CERT_PIN_ROTATE key falls to the key-name
              # check above instead.
              ($c.environment // {} | to_entries[] | select(. as $kv
                    | ($s.environment // {})[$kv.key] != $kv.value)
                | "\($c.service): environment \(.key) does not match its declaration"))
      end),
  # A declared collector keeps its own secret (mounted as bridge_pass),
  # both volumes and its network: an overlay with !reset cannot take them.
  ($declared[] | . as $c | $m.services[$c.service] // empty | . as $s
    # (Compose writes the short form target as /run/secrets/bridge_pass.)
    | (if [$s | list("secrets")[] | select(.source == $c.secret
            and ((.target // .source) | ltrimstr("/run/secrets/")) == "bridge_pass")]
            | length != 1
        then "\($c.service): lacks its own secret \($c.secret) as bridge_pass" else empty end),
      ($c.volumes | to_entries[] | . as $v
        | select([$s | list("volumes")[] | select(.type == "volume" and .source == $v.value
            and .target == $v.key)] | length != 1)
        | "\($c.service): lacks its own volume \($v.value) at \($v.key)"),
      # Mail must be written under /maildir, and the TLS pin under /state
      # (mbsync/entrypoint.sh); the match above compares only type, source
      # and target, so a read-only mount of either would still pass it
      # while failing every sync at startup (review round 5, finding 2).
      ($c.volumes | to_entries[] | . as $v
        | select([$s | list("volumes")[] | select(.type == "volume" and .source == $v.value
            and .target == $v.key and .read_only == true)] | length > 0)
        | "\($c.service): volume \($v.value) at \($v.key) is read-only"),
      # nocopy skips copying the image own existing directory into a
      # new, empty volume (Docker own docs); mbsync own image relies on
      # that copy to seed /maildir and /state owned 1001:1001 (the fixed
      # mbsync UID, AGENTS.md), so a newly created volume mounted with
      # it stays root-owned and the explicitly non-root collector can
      # write neither (review round 8, finding 5). subpath is the same
      # class under a different option: it mounts a subdirectory of the
      # volume instead of its root, so the copied seed never lands at
      # the mount point either. Neither option is ever set by
      # render_compose(), so any value is an overlay or renderer
      # regression.
      ($c.volumes | to_entries[] | . as $v
        | select([$s | list("volumes")[] | select(.type == "volume" and .source == $v.value
            and .target == $v.key
            and (((.volume.nocopy // false) == true) or (.volume.subpath // null) != null))]
            | length > 0)
        | "\($c.service): volume \($v.value) at \($v.key) sets nocopy or subpath"),
      (if ($s.networks // {} | has($c.network)) | not
        then "\($c.service): lacks its own network \($c.network)" else empty end)),
  # Its secret is the password file the account declares, and its network
  # and volumes are defined as bridge-net and the base volumes are, under
  # their own project-scoped engine names.
  ($own[] | . as $c
    | (if $m.networks[$c.network].name != "\($m.name)_\($c.network)"
        then "network \($c.network): renamed" else empty end),
      ($c.volumes[] | select($m.volumes[.].name != "\($m.name)_\(.)") | "volume \(.): renamed")),
  ($own[] | . as $c
    | (if ($m.secrets[$c.secret] // {} | del(.name)) != {file: $c.password_file}
        then "secret \($c.secret): is not the declared password file of \($c.service)" else empty end),
      # internal is compared here but deliberately left out of the
      # base-network check above: app-net legitimately gains internal:
      # true under the hardened overlay (docker-compose.hardened.yml),
      # but bridge-net never does in either file, in or out of that
      # overlay, so a collector own network setting it would make the
      # collector unable to reach the host-side Bridge through
      # host.docker.internal -- isolated from connectivity its own
      # comparison here otherwise required unchanged (review round 6,
      # finding 4). enable_ipv4/enable_ipv6 are the same class on a
      # Compose version that supports them (2.33.1+): bridge-net never
      # disables IPv4 either, and the Bridge path terminates at the
      # host own IPv4-only 127.0.0.1, so a collector own network
      # disabling it could never reach it (review round 9).
      (if ($m.networks[$c.network] // {} | {driver, driver_opts, ipam, external, internal, enable_ipv4, enable_ipv6})
            != ($base[0].networks["bridge-net"]
                | {driver, driver_opts, ipam, external, internal, enable_ipv4, enable_ipv6})
        then "network \($c.network): driver settings differ from bridge-net or it is external"
        else empty end),
      ($c.volumes[] | select(($m.volumes[.] // {} | {driver, driver_opts, external})
            != ($base[0].volumes["maildir-volume"] | {driver, driver_opts, external}))
        | "volume \(.): driver settings differ from the base volumes or it is external")),
  # No other service gets the secret, volumes or network of a collector.
  ($own[] | . as $c | $m.services | to_entries[] | select(.key != $c.service)
    | .key as $svc | .value
    | (list("secrets")[] | select(.source == $c.secret)
        | "\($svc): uses the secret \(.source) of \($c.service)"),
      (list("volumes")[] | select(.source as $v | $c.volumes | any(.[]; . == $v))
        | "\($svc): mounts the volume \(.source) of \($c.service)"),
      (if (.networks // {} | has($c.network)) then "\($svc): joins the network of \($c.service)"
        else empty end)),
  # The Maildir provenance invariant (#1665): only the own collector of an
  # account writes its Maildir. Any other service that mounts the volume
  # without read_only, at any target and under any name, or takes it
  # through volumes_from (every volume of the named service, writable
  # unless the entry ends in :ro), is a second writer whose files the
  # index would read as the collector own. A read-only mount (the
  # indexer reading) is not a writer. This holds the rule on its own,
  # whatever the checks above and below also say about the same mount.
  ($maildirs[] | . as $d | $m.services | to_entries[] | select(.key != $d.owner)
    | .key as $k | .value
    | (list("volumes")[] | select(.source == $d.volume and .read_only != true)
        | "\($k): mounts the Maildir volume \($d.volume) of \($d.owner) writable at \(.target)"),
      (list("volumes_from")[] | select(split(":")[0] == $d.owner and (endswith(":ro") | not))
        | "\($k): takes the Maildir volume \($d.volume) of \($d.owner) writable through volumes_from")),
  # A declared account whose provider is not Proton (imap, #1606) gets
  # neither the Bridge password nor bridge-net, wherever its Maildir is.
  ($collectors[0][] | select(.provider != "proton-bridge")
    | (if .maildir_root == "/maildir" then "mbsync" else "mbsync-\(.scope_uuid)" end) as $svc
    | $m.services[$svc] // {}
    | (list("secrets")[] | select(.source == "bridge_pass")
        | "\($svc): an imap collector uses the Bridge password secret bridge_pass"),
      (if (.networks // {} | has("bridge-net")) then "\($svc): an imap collector joins bridge-net"
        else empty end)),
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
    (if ($s.user // "" | tostring | test("^(root|[+-]?0+)(:|$)")) then "runs as root" else empty end),
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
        and $svc != "mbsync"
        then "joins bridge-net" else empty end),
    # A service the base does not define must name its non-root user (an
    # unset user is the image default, possibly root) and gets no secrets or base volumes.
    # A declared collector may use its own secret, checked above, and no other.
    (if $bs == null then
        (if $s.user == null then "sets no user" else empty end),
        (if ($s | list("secrets")) != [] and ($own | any(.[]; .service == $svc) | not)
            then "uses a secret" else empty end),
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
        # The one-run maintenance commands (#1609) are arguments of a
        # one-shot `docker compose run` container; a command or entrypoint
        # set on a service would repeat one on every start.
        (if $s.command != $bs.command or $s.entrypoint != $bs.entrypoint
            then "changes its command or entrypoint" else empty end),
        ($bs | list("cap_drop")[] | select(. as $c | $s | list("cap_drop") | index($c) == null)
            | "cap_drop lost \(.)"),
        ($bs | list("security_opt")[]
            | select(. as $o | $s | list("security_opt") | index($o) == null)
            | "security_opt lost \(.)"),
        # A read-only-root service that needs a writable /tmp (mbsync,
        # indexer, mcp-server all mount one) keeps it (review round 5,
        # finding 1): nothing generic enforced this before.
        ($bs | list("tmpfs")[] | select(. as $t | $s | list("tmpfs") | index($t) == null)
            | "tmpfs lost \(.)"),
        (if ($s.pids_limit | type) == "number" and $s.pids_limit > $bs.pids_limit
            then "pids_limit is above the base" else empty end),
        (if $bs.mem_limit != null
            and ($s.mem_limit == null or ($s.mem_limit | tonumber) > ($bs.mem_limit | tonumber))
            then "mem_limit is missing or above the base" else empty end),
        # A base service must retain the resources its entrypoint needs,
        # including their mount destinations, even without declared collectors.
        ($bs | list("secrets")[] | . as $secret
            | (($secret.target // $secret.source) | ltrimstr("/run/secrets/")) as $target
            | select([$s | list("secrets")[] | select(.source == $secret.source
                and (((.target // .source) | ltrimstr("/run/secrets/")) == $target))] | length == 0)
            | "lacks base secret \($secret.source) at \($target)"),
        ($bs | list("volumes")[] | . as $v
            | select([$s | list("volumes")[]
                | select(.type == $v.type and .source == $v.source and .target == $v.target)] | length == 0)
            | "lacks base volume \($v.source // $v.type) at \($v.target)"),
        ($bs.networks // {} | keys[] | . as $network
            | select(($s.networks // {} | has($network)) | not)
            | "lacks base network \($network)"),
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
# the violations, unless the merged config keeps the hardening. It also
# renders them with no profile, as every Makefile target runs them, for
# the set of services that actually start ($active).
expect_merged_hardening() {
    render "$@" || return 1
    cp "$WORK/config.json" "$WORK/active.json"
    ALL_PROFILES=1 render "$@" || return 1
    jq -r --slurpfile base "$WORK/base.json" --slurpfile active "$WORK/active.json" \
        --slurpfile collectors "${COLLECTORS:-$WORK/no-collectors.json}" \
        "$HARDENING_VIOLATIONS" "$WORK/config.json" >"$WORK/violations" || return 1
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
# run: make up/build (base) and the hardened overlay.
merged_hardening_holds_for_every_overlay_combination() {
    expect_merged_hardening "$BASE" || return 1
    expect_merged_hardening "$BASE" "$HARDENED" || return 1
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

# A one-run maintenance command (#1609) set as a service's command or
# entrypoint would run on every start of that service.
merged_hardening_rejects_a_command_or_entrypoint() {
    expect_overlay_rejected command \
        "mbsync: changes its command or entrypoint" \
        "indexer: changes its command or entrypoint" <<'EOF'
services:
  mbsync:
    command: [rotate-bridge-pin]
  indexer:
    entrypoint: [python, -m, src.force_deletion]
EOF
}

# Forced deletion is a one-run command, not a setting (#1609).
indexer_has_no_deletion_force_setting() {
    render "$BASE"
    expect '.services.indexer.environment | has("INDEXER_DELETION_FORCE") | not' || return 1
    expect '[.services[] | .command, .entrypoint] | all(. == null)' || return 1
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
        "indexer: pids_limit is unbounded" <<'EOF' || return 1
services:
  indexer:
    pids_limit: !reset null
EOF
    expect_overlay_rejected reset-logging "mbsync: logging is unbounded" <<'EOF'
services:
  mbsync:
    logging: !reset null
EOF
}

# mbsync's entrypoint creates /tmp/mbsync under the read-only root
# filesystem on every start (review round 5, finding 1).
merged_hardening_rejects_reset_tmpfs() {
    expect_overlay_rejected reset-tmpfs "mbsync: tmpfs lost /tmp" <<'EOF'
services:
  mbsync:
    tmpfs: !reset []
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

merged_hardening_rejects_removed_base_resources() {
    expect_overlay_rejected reset-base-secret "mbsync: lacks base secret bridge_pass at bridge_pass" <<'EOF' || return 1
services:
  mbsync:
    secrets: !reset []
EOF
    expect_overlay_rejected retarget-base-secret "mbsync: lacks base secret bridge_pass at bridge_pass" <<'EOF' || return 1
services:
  mbsync:
    secrets: !override
      - source: bridge_pass
        target: another_name
EOF
    expect_overlay_rejected reset-base-volumes "mbsync: lacks base volume maildir-volume at /maildir" "mbsync: lacks base volume mbsync-state at /state" <<'EOF' || return 1
services:
  mbsync:
    volumes: !reset []
EOF
    expect_overlay_rejected drop-one-base-volume "mbsync: lacks base volume maildir-volume at /maildir" <<'EOF' || return 1
services:
  mbsync:
    volumes: !override
      - mbsync-state:/state
EOF
    expect_overlay_rejected reset-base-network "mbsync: lacks base network bridge-net" <<'EOF'
services:
  mbsync:
    networks: !reset []
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
    volumes_from: [mbsync]
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
        "mcp-server: mounts mbsync-state at /state, which the base does not" \
        "indexer: mounts sqlite-volume at /extra, which the base does not" \
        "extra: mounts the base volume mbsync-state" <<'EOF'
services:
  mcp-server:
    volumes:
      - mbsync-state:/state:ro
  indexer:
    volumes:
      - sqlite-volume:/extra:ro
  extra:
    image: example.invalid/extra:1
    user: "1234:1234"
    volumes:
      - mbsync-state:/state:ro
EOF
}

# The Maildir provenance invariant (#1665): no service but an account's own
# collector mounts that account's Maildir volume writable, whatever the
# service is called, wherever it mounts the volume, and also by
# volumes_from, which takes every volume of the named service. A read-only
# mount (the indexer's) is not a writer.
merged_hardening_rejects_a_second_maildir_writer() {
    expect_overlay_rejected second-maildir-writer \
        "indexer: takes the Maildir volume maildir-volume of mbsync writable through volumes_from" \
        "any-name: mounts the Maildir volume maildir-volume of mbsync writable at /anywhere" \
        "mcp-server: mounts the Maildir volume maildir-volume of mbsync writable at /srv/other" <<'EOF' || return 1
services:
  indexer:
    volumes_from: [mbsync]
  mcp-server:
    volumes:
      - maildir-volume:/srv/other:rw
  any-name:
    image: example.invalid/extra:1
    user: "1234:1234"
    volumes:
      - maildir-volume:/anywhere
EOF
    # Read-only is not a writer: only the generic added-volume rules
    # object to it, never the invariant.
    expect_overlay_rejected read-only-maildir-reader \
        "any-name: mounts the base volume maildir-volume" <<'EOF' || return 1
services:
  any-name:
    image: example.invalid/extra:1
    user: "1234:1234"
    volumes_from: ["mbsync:ro"]
    volumes:
      - maildir-volume:/anywhere:ro
EOF
    if grep -q 'writable' "$WORK/rejected"; then
        printf 'a read-only mount was called a writer:\n'
        cat "$WORK/rejected"
        return 1
    fi
}

# A decimal UID of zeros, signed or not, is still root: Docker parses it
# with Go's strconv.Atoi.
merged_hardening_rejects_leading_zero_root_users() {
    expect_overlay_rejected zero-users \
        "indexer: runs as root" "mbsync: runs as root" \
        "mcp-server: runs as root" <<'EOF'
services:
  indexer:
    user: "00"
  mbsync:
    user: "000:000"
  mcp-server:
    user: "+00:+000"
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

# No Makefile target activates a profile, so a service an overlay moves
# into one silently stops running.
merged_hardening_rejects_services_moved_into_a_profile() {
    expect_overlay_rejected profiled-services \
        "indexer: not started by a plain up" \
        "mbsync: not started by a plain up" \
        "mcp-server: not started by a plain up" <<'EOF' || return 1
services:
  indexer:
    profiles: [manual]
  mbsync:
    profiles: [manual]
  mcp-server:
    profiles: [manual]
EOF
    # A service scaled to zero is rendered but starts no container.
    expect_overlay_rejected scaled-to-zero \
        "indexer: not started by a plain up" \
        "mbsync: not started by a plain up" <<'EOF' || return 1
services:
  indexer:
    scale: 0
  mbsync:
    deploy:
      replicas: 0
EOF
    # Every service here sets a fixed container_name, so Compose itself
    # already refuses to render a config that also scales one above one
    # container ("container name must be unique"). container_name is
    # reset here only so this check, not that unrelated guard, is what
    # rejects the scale (review round 8, finding 1); nothing in this
    # script inspects container_name itself.
    expect_overlay_rejected scaled-above-one \
        "indexer: not started by a plain up" \
        "mbsync: not started by a plain up" <<'EOF'
services:
  indexer:
    container_name: !reset null
    scale: 2
  mbsync:
    container_name: !reset null
    deploy:
      replicas: 2
EOF
}

# A macvlan bridge-net keeps its engine name but puts mbsync on the LAN.
merged_hardening_rejects_network_driver_changes() {
    expect_overlay_rejected network-drivers \
        "network bridge-net: driver settings differ from the base" \
        "network app-net: driver settings differ from the base" <<'EOF'
networks:
  bridge-net:
    driver: macvlan
    driver_opts:
      parent: en0
    ipam:
      config:
        - subnet: 192.168.1.0/24
  app-net:
    driver_opts:
      com.docker.network.bridge.host_binding_ipv4: 0.0.0.0
EOF
}

# Services mount volumes by key; a volume's name picks the engine volume
# and its driver settings what backs it (a local bind to a host path).
merged_hardening_rejects_renamed_or_rebacked_volumes() {
    expect_overlay_rejected volume-names \
        "volume maildir-volume: renamed or external" "volume sqlite-volume: renamed or external" \
        "volume mbsync-state: renamed or external" \
        "volumes share the engine volume shared-volume" <<'EOF' || return 1
volumes:
  maildir-volume:
    name: shared-volume
  sqlite-volume:
    name: shared-volume
  mbsync-state:
    external: true
EOF
    expect_overlay_rejected volume-drivers \
        "volume mbsync-state: driver settings differ from the base" <<'EOF'
volumes:
  mbsync-state:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /srv/data
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

# Generated Compose (#1608): the translator's compose.accounts.yml for
# each synthetic configuration of scripts/config/tests/synthetic_compose.py,
# with the accounts it declares. #1603's launcher replaces the base
# mbsync service with the generated collectors: Compose cannot overlay a
# service defined in full (list fields append and then collide), so the
# base mbsync is removed first ($WORK/replace-mbsync.yml) and the
# generated one is held to the base's like any other base service.
readonly GENERATED_CASES=(one-proton two-proton three-proton)
UUID_B="00000000-0000-4000-8000-00000000000b"
UUID_C="00000000-0000-4000-8000-00000000000c"
readonly UUID_B UUID_C

# GENERATED_CASES must list exactly the cases synthetic_compose.py wrote
# to $WORK/generated (review round 3): the generator's own output, not
# this array, is the independent source it is checked against
# (AGENTS.md "a list that must cover every item of some kind"), so a
# case the generator adds and this array omits is caught here instead
# of silently skipping the merged hardening check below while
# semgrep_generated_test.sh still finds and scans its directory.
generated_cases_cover_the_generator_output() {
    diff <(find "$WORK/generated" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort) \
        <(printf '%s\n' "${GENERATED_CASES[@]}" | sort)
}

# The merged hardening check on the generated file of case $1, after the
# compose files that follow it, with the accounts that case declares.
expect_generated_hardening() {
    local case="$1"
    shift
    COLLECTORS="$WORK/generated/$case/collectors.json" expect_merged_hardening \
        "$BASE" "$@" "$WORK/replace-mbsync.yml" "$WORK/generated/$case/compose.accounts.yml"
}

# Every declared collector is in the render, and no other service: the
# expected set is derived from case $1's own collectors.json (the
# generator's input, independent of the render under test), not a
# literal list naming only the last case checked (review round 4,
# finding 3) -- render_compose emitting an extra but otherwise-hardened
# service for only one-proton or two-proton would otherwise pass every
# check in the loop and go unnoticed once a later case's own correct set
# is the only thing asserted.
expect_generated_service_set() {
    local case="$1" expected
    expected="$(jq -c '["indexer", "mcp-server"]
            + map(if .maildir_root == "/maildir" then "mbsync" else "mbsync-\(.scope_uuid)" end)
            | sort' "$WORK/generated/$case/collectors.json")"
    expect "(.services | keys | sort) == $expected"
}

generated_collectors_keep_the_hardening() {
    local case
    for case in "${GENERATED_CASES[@]}"; do
        expect_generated_hardening "$case" || return 1
        expect_generated_hardening "$case" "$HARDENED" || return 1
        expect_generated_service_set "$case" || return 1
    done
}

# Negative fixtures for generated Compose: the overlay on stdin is merged
# after case $1's generated file, checked with the accounts in the JSON
# file $2 (empty: the case's own), and passes only if the hardening check
# fails with each expected violation among those it lists.
expect_generated_rejected() {
    local case="$1" collectors="$2" expected
    shift 2
    cat >"$WORK/mutation-$case.yml"
    if COLLECTORS="${collectors:-$WORK/generated/$case/collectors.json}" expect_merged_hardening \
        "$BASE" "$WORK/replace-mbsync.yml" "$WORK/generated/$case/compose.accounts.yml" \
        "$WORK/mutation-$case.yml" >"$WORK/rejected" 2>&1; then
        printf 'generated case %s with the mutation passed the hardening check\n' "$case"
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

generated_collectors_reject_another_accounts_secret() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: uses secret bridge_pass, not its own" \
        "mbsync-$UUID_B: uses secret bridge_pass_$UUID_C, not its own" \
        "mbsync: gains secret bridge_pass_$UUID_B" <<EOF || return 1
services:
  mbsync-$UUID_B:
    secrets: [bridge_pass, bridge_pass_$UUID_C]
  mbsync:
    secrets: [bridge_pass_$UUID_B]
EOF
    # The secret's definition picks the file: another account's file under
    # the collector's own secret name is another account's secret.
    expect_generated_rejected two-proton "" \
        "secret bridge_pass_$UUID_B: is not the declared password file of mbsync-$UUID_B" <<EOF || return 1
secrets:
  bridge_pass_$UUID_B: !override
    file: ./.secrets/bridge_pass.txt
EOF
    # Compose mounts one secret at several targets; a second entry
    # naming the collector's own correct secret at another absolute
    # target can overwrite an in-container file (/entrypoint.sh here)
    # with the mounted password, while the "lacks its own secret" check
    # still finds the one bridge_pass-targeted entry and passes (review
    # round 8, finding 2, security).
    expect_generated_rejected two-proton "" \
        "mbsync-$UUID_B: mounts its own secret bridge_pass_$UUID_B at /entrypoint.sh, not bridge_pass" <<EOF
services:
  mbsync-$UUID_B:
    secrets:
      - source: bridge_pass_$UUID_B
        target: /entrypoint.sh
EOF
}

generated_collectors_reject_a_wrong_network() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: joins network app-net, not its own" \
        "mbsync-$UUID_B: joins network bridge-net, not its own" \
        "mbsync-$UUID_B: joins bridge-net" \
        "mbsync-$UUID_B: joins network bridge-net-$UUID_C, not its own" \
        "mbsync: joins network bridge-net-$UUID_B, which the base does not give it" <<EOF || return 1
services:
  mbsync-$UUID_B:
    networks: [app-net, bridge-net, bridge-net-$UUID_C]
  mbsync:
    networks: [bridge-net-$UUID_B]
EOF
    # A collector's network keeps bridge-net's driver settings: a macvlan
    # or an external network puts the collector on another network.
    expect_generated_rejected three-proton "" \
        "network bridge-net-$UUID_B: driver settings differ from bridge-net or it is external" \
        "network bridge-net-$UUID_C: driver settings differ from bridge-net or it is external" <<EOF
networks:
  bridge-net-$UUID_B:
    driver: macvlan
    driver_opts:
      parent: en0
  bridge-net-$UUID_C:
    external: true
EOF
    # internal: true externally isolates the network (Docker's own
    # docs); bridge-net never sets it, in or out of the hardened
    # overlay (only app-net does, under docker-compose.hardened.yml),
    # so a collector's own network setting it would cut off its only
    # path to the host-side Bridge through host.docker.internal
    # (review round 6, finding 4).
    expect_generated_rejected three-proton "" \
        "network bridge-net-$UUID_B: driver settings differ from bridge-net or it is external" <<EOF
networks:
  bridge-net-$UUID_B:
    internal: true
EOF
    # On Compose 2.33.1+, enable_ipv4: false is the same class under a
    # different field: bridge-net never disables IPv4 either, and the
    # Bridge path terminates at the host's IPv4-only 127.0.0.1, so this
    # account could never reach it (review round 9).
    expect_generated_rejected three-proton "" \
        "network bridge-net-$UUID_B: driver settings differ from bridge-net or it is external" <<EOF
networks:
  bridge-net-$UUID_B:
    enable_ipv4: false
    enable_ipv6: true
EOF
    # The key check above passed a collector that kept its own network
    # but gave it a static ipv4_address; docker compose config renders
    # it without requiring a matching IPAM subnet, so this is caught
    # here, not by a render failure (review round 9).
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: network bridge-net-$UUID_B sets a static attachment" <<EOF
services:
  mbsync-$UUID_B:
    networks:
      bridge-net-$UUID_B:
        ipv4_address: 10.1.2.3
EOF
}

generated_collectors_reject_an_extra_mount() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: mounts maildir-volume at /extra, not its own Maildir or state volume" \
        "mbsync-$UUID_B: mounts the base volume maildir-volume" \
        "mbsync-$UUID_B: mounts maildir-$UUID_C at /other, not its own Maildir or state volume" \
        "mbsync-$UUID_B: mounts maildir-$UUID_B at /state2, not its own Maildir or state volume" \
        "mbsync-$UUID_B: mounts the volume maildir-$UUID_C of mbsync-$UUID_C" \
        "mbsync-$UUID_B: mounts /srv at /srv, not its own Maildir or state volume" <<EOF || return 1
services:
  mbsync-$UUID_B:
    volumes:
      - maildir-volume:/extra:ro
      - maildir-$UUID_C:/other
      - maildir-$UUID_B:/state2
      - /srv:/srv:ro
EOF
    # A collector's volumes keep the base volumes' driver settings: local
    # driver_opts can back one with a host path.
    expect_generated_rejected two-proton "" \
        "volume maildir-$UUID_B: driver settings differ from the base volumes or it is external" \
        "volume mbsync-state-$UUID_B: driver settings differ from the base volumes or it is external" <<EOF
volumes:
  maildir-$UUID_B:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /srv/data
  mbsync-state-$UUID_B:
    external: true
EOF
}

# No other service, base or new, gets a collector's secret, volumes or
# network.
# The same invariant for an account the translator generates: a service
# of any name mounting its Maildir volume writable, at any target, or
# taking it through volumes_from, is a second writer (#1665).
generated_maildir_has_one_writer() {
    expect_generated_rejected three-proton "" \
        "any-name: mounts the Maildir volume maildir-$UUID_B of mbsync-$UUID_B writable at /anywhere" \
        "mbsync-$UUID_C: takes the Maildir volume maildir-$UUID_B of mbsync-$UUID_B writable through volumes_from" \
        "indexer: mounts the Maildir volume maildir-$UUID_C of mbsync-$UUID_C writable at /accounts/x" <<EOF
services:
  any-name:
    image: example.invalid/extra:1
    user: "1234:1234"
    volumes:
      - maildir-$UUID_B:/anywhere
  mbsync-$UUID_C:
    volumes_from: [mbsync-$UUID_B]
  indexer:
    volumes:
      - maildir-$UUID_C:/accounts/x
EOF
}

generated_collectors_resources_stay_their_own() {
    expect_generated_rejected two-proton "" \
        "extra: uses a secret" \
        "extra: uses the secret bridge_pass_$UUID_B of mbsync-$UUID_B" \
        "extra: mounts the volume maildir-$UUID_B of mbsync-$UUID_B" \
        "extra: joins the network of mbsync-$UUID_B" \
        "indexer: mounts the volume mbsync-state-$UUID_B of mbsync-$UUID_B" <<EOF
services:
  indexer:
    volumes:
      - mbsync-state-$UUID_B:/extra:ro
  extra:
    image: example.invalid/extra:1
    user: "1234:1234"
    init: true
    read_only: true
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    pids_limit: 16
    logging:
      driver: none
    secrets: [bridge_pass_$UUID_B]
    volumes:
      - maildir-$UUID_B:/maildir:ro
    networks: [bridge-net-$UUID_B]
EOF
}

# The exception covers declared accounts only: an undeclared collector is
# a new service like any other, and a declared one must be there and
# start, and keep the rest of the new-service checks (its user).
generated_collectors_are_declared_present_and_checked() {
    printf '[{"scope_uuid": "%s", "provider": "proton-bridge", "maildir_root": "/maildir", "password_file": "%s"}]\n' \
        "00000000-0000-4000-8000-000000000001" "$ROOT_DIR/.secrets/bridge_pass.txt" \
        >"$WORK/legacy-only.json"
    expect_generated_rejected two-proton "$WORK/legacy-only.json" \
        "mbsync-$UUID_B: uses a secret" </dev/null || return 1
    jq --arg c "$UUID_C" '. + [{scope_uuid: $c, provider: "proton-bridge",
        maildir_root: "/accounts/\($c)", password_file: "/nonexistent"}]' \
        "$WORK/generated/two-proton/collectors.json" >"$WORK/missing-c.json"
    expect_generated_rejected two-proton "$WORK/missing-c.json" \
        "mbsync-$UUID_C: declared collector missing from the merged config" </dev/null || return 1
    expect_generated_rejected two-proton "" \
        "mbsync-$UUID_B: declared collector not started by a plain up" \
        "mbsync-$UUID_B: sets no user" <<EOF || return 1
services:
  mbsync-$UUID_B:
    user: !reset null
    profiles: [manual]
EOF
    # Every collector sets a fixed container_name (render_compose()), so
    # Compose itself already refuses to render a config that also
    # scales one past one container; container_name is reset here only
    # so this check, not that unrelated guard, is what rejects the
    # scale (review round 8, finding 1).
    expect_generated_rejected two-proton "" \
        "mbsync-$UUID_B: declared collector not started by a plain up" <<EOF
services:
  mbsync-$UUID_B:
    container_name: !reset null
    deploy:
      replicas: 2
EOF
}

# A collector keeps its own secret, volumes and network (review round 1):
# the checks above inspect only what is mounted, so an overlay with !reset
# would otherwise pass. Its network and volumes keep their own engine
# names, so it cannot join or mount a shared one.
generated_collectors_keep_their_own_resources() {
    expect_generated_rejected two-proton "" \
        "mbsync-$UUID_B: lacks its own secret bridge_pass_$UUID_B as bridge_pass" \
        "mbsync-$UUID_B: lacks its own volume maildir-$UUID_B at /maildir" \
        "mbsync-$UUID_B: lacks its own volume mbsync-state-$UUID_B at /state" \
        "mbsync-$UUID_B: lacks its own network bridge-net-$UUID_B" \
        "mbsync: lacks its own secret bridge_pass as bridge_pass" \
        "mbsync: lacks its own volume maildir-volume at /maildir" <<EOF || return 1
services:
  mbsync-$UUID_B:
    secrets: !reset []
    volumes: !reset []
    networks: !reset {}
  mbsync:
    secrets: !reset []
    volumes: !override
      - mbsync-state:/state
EOF
    expect_generated_rejected two-proton "" \
        "network bridge-net-$UUID_B: renamed" \
        "volume maildir-$UUID_B: renamed" \
        "volume mbsync-state-$UUID_B: renamed" <<EOF
networks:
  bridge-net-$UUID_B:
    name: shared-collector-net
volumes:
  maildir-$UUID_B:
    name: shared-maildir
  mbsync-state-$UUID_B:
    name: shared-state
EOF
}

# An imap collector (#1606) gets neither the Bridge password nor
# bridge-net, wherever its Maildir is.
generated_imap_collectors_get_no_bridge_secret_or_network() {
    jq 'map(.provider = "imap")' "$WORK/generated/two-proton/collectors.json" >"$WORK/imap.json"
    expect_generated_rejected two-proton "$WORK/imap.json" \
        "mbsync: an imap collector uses the Bridge password secret bridge_pass" \
        "mbsync: an imap collector joins bridge-net" \
        "mbsync-$UUID_B: an imap collector uses the Bridge password secret bridge_pass" \
        "mbsync-$UUID_B: an imap collector joins bridge-net" <<EOF
services:
  mbsync-$UUID_B:
    secrets: [bridge_pass]
    networks: [bridge-net]
EOF
}

# A declared collector has no entry of its own in $base, so it kept none
# of the resource-retention hardening the legacy collector is held to
# (review round 2, finding 1): an overlay could reset its init or raise
# its pids_limit or mem_limit past the base's, or drop a cap_drop or
# security_opt entry, while the same mutation on mbsync itself is
# rejected. Clearing tmpfs is the same gap (review round 5, finding 1):
# the entrypoint still needs a writable /tmp under the read-only root
# filesystem. Resetting restart is the same gap again (review round 6,
# finding 2): mbsync's entrypoint exits on a persistent failure, and
# Docker's default restart ("no") would then leave that account
# permanently unsynced rather than retried and visibly failing.
generated_collectors_keep_the_mbsync_baseline_hardening() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: init is not true" \
        "mbsync-$UUID_B: restart differs from mbsync" \
        "mbsync-$UUID_B: cap_drop lost ALL" \
        "mbsync-$UUID_B: security_opt lost no-new-privileges:true" \
        "mbsync-$UUID_B: tmpfs lost /tmp" \
        "mbsync-$UUID_B: pids_limit is above the base" <<EOF || return 1
services:
  mbsync-$UUID_B:
    init: !reset null
    restart: !reset null
    cap_drop: !reset []
    security_opt: !reset []
    tmpfs: !reset []
    pids_limit: 256
EOF
    # logging was never compared against mbsync here, and the generic
    # bounded_logging predicate explicitly accepts driver: none, so a
    # collector or overlay could disable logging outright and still
    # pass -- hiding Bridge connection failures, TLS refusals, retries
    # and recovery for that account (review round 8, finding 3). tmpfs
    # was only checked for a lost entry, not an added one; a collector
    # could append another tmpfs path (Compose appends the list rather
    # than replacing it) and mount a filesystem over an existing image
    # path, such as /usr/local/lib/proton-settings.sh here (review
    # round 8, finding 4).
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: logging differs from mbsync" \
        "mbsync-$UUID_B: tmpfs gained /usr/local/lib" <<EOF
services:
  mbsync-$UUID_B:
    logging:
      driver: none
    tmpfs:
      - /usr/local/lib
EOF
    # Neither stop_signal nor stop_grace_period was compared either.
    # mbsync's entrypoint traps TERM and INT and waits while forwarding
    # TERM to the active isync child, so a collector or overlay setting
    # SIGKILL or a zero grace period bypasses that and Docker kills it
    # immediately, abandoning an in-progress run (review round 9).
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: stop_signal differs from mbsync" \
        "mbsync-$UUID_B: stop_grace_period differs from mbsync" <<EOF
services:
  mbsync-$UUID_B:
    stop_signal: SIGKILL
    stop_grace_period: 0s
EOF
}

# depends_on was never compared, so a collector could gain a dependency
# on the always-running mbsync (or another collector) while staying
# present and unscaled -- passing the checks above -- and never start:
# Docker's own docs say service_completed_successfully waits for the
# dependency to exit, but a healthy mbsync runs its sync loop
# indefinitely, so the dependent account's collector would wait forever
# (review round 7). The base mbsync depends on nothing
# (the_base_runs_mbsync_indexer_and_mcp_server), so every collector must
# match that exactly, not merely stay present and unscaled.
generated_collectors_reject_a_blocking_dependency() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: depends_on differs from mbsync" <<EOF
services:
  mbsync-$UUID_B:
    depends_on:
      mbsync:
        condition: service_completed_successfully
EOF
}

# The collector's /maildir and /state mounts must stay writable; the
# "lacks its own volume" check above compares only type, source and
# target, so a read-only mount of either would still pass it while
# failing every sync at startup (review round 5, finding 2). Long
# syntax, since a short ``:ro`` suffix exercises the same field.
generated_collectors_reject_read_only_data_volumes() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: volume maildir-$UUID_B at /maildir is read-only" \
        "mbsync-$UUID_B: volume mbsync-state-$UUID_B at /state is read-only" <<EOF || return 1
services:
  mbsync-$UUID_B:
    volumes: !override
      - source: maildir-$UUID_B
        target: /maildir
        type: volume
        read_only: true
      - source: mbsync-state-$UUID_B
        target: /state
        type: volume
        read_only: true
EOF
    # nocopy skips copying the image's existing directory into a newly
    # created, empty volume (Docker's own docs); mbsync's image relies
    # on that copy to seed /maildir and /state owned 1001:1001 (the
    # fixed mbsync UID), so a collector mounted with it stays root-owned
    # and the non-root collector can write neither. subpath is the same
    # class: it mounts a subdirectory of the volume instead of its root,
    # so the copied seed never lands at the mount point either (review
    # round 8, finding 5).
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: volume maildir-$UUID_B at /maildir sets nocopy or subpath" \
        "mbsync-$UUID_B: volume mbsync-state-$UUID_B at /state sets nocopy or subpath" <<EOF
services:
  mbsync-$UUID_B:
    volumes: !override
      - source: maildir-$UUID_B
        target: /maildir
        type: volume
        volume:
          nocopy: true
      - source: mbsync-state-$UUID_B
        target: /state
        type: volume
        volume:
          subpath: sub
EOF
}

# The environment check above compared only key names at the time, which
# mbsync itself also sets for BRIDGE_CERT_FINGERPRINT, so an overlay
# keeping that name could still substitute another account's
# certificate fingerprint: the entrypoint would then accept an
# attacker's certificate and send this account's mounted Bridge password
# to it (review round 5, finding 3, security). The declared-value check
# added for round 6, finding 3 now catches it, since collector_env()
# always sets the account's own certificate fingerprint -- here
# mbsync-$UUID_B's own is replaced with $UUID_C's account's. (Pin
# rotation was the other trust value this check covered until #1616:
# it is now a one-run command, not configuration, so collector_env()
# sets no environment key for it at all.)
generated_collectors_bind_trust_values_to_their_declarations() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: environment BRIDGE_CERT_FINGERPRINT does not match its declaration" <<EOF
services:
  mbsync-$UUID_B:
    environment:
      BRIDGE_CERT_FINGERPRINT: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
EOF
}

# Every check added in rounds 2-5 above (image/build, entrypoint and
# command overrides, the lifecycle hooks, extra_hosts, environment keys
# and values, tmpfs, the exact UID) was written against $own, which
# excludes the account at /maildir -- so when that account is itself
# generated by the translator, its legacy "mbsync" service fell only to
# the weaker existing-service checks below, which accept a changed
# image or build whenever *any* non-root user is set, whatever UID,
# while mbsync still keeps bridge_pass, Maildir/state and bridge-net. A
# hardened credential-reading image could pass every other check and
# exfiltrate the legacy account's mail (review round 6, finding 1,
# security). The comparison above now iterates $declared, which adds
# the /maildir entry back with the same shape, so every one of those
# checks now also runs for a generated legacy mbsync -- including the
# declared-value check (not just the key-name one), exercised here with
# the legacy account's own BRIDGE_CERT_FINGERPRINT ("a" * 64,
# fixtures/legacy.toml) substituted.
generated_collectors_apply_bindings_to_the_legacy_service() {
    expect_generated_rejected three-proton "" \
        "mbsync: image or build differs from mbsync" \
        "mbsync: user is not the mbsync UID 1001:1001" \
        "mbsync: sets an entrypoint override" \
        "mbsync: environment sets BASH_ENV, which mbsync does not set" \
        "mbsync: environment BRIDGE_CERT_FINGERPRINT does not match its declaration" <<EOF
services:
  mbsync:
    build:
      dockerfile: Dockerfile.rootful
    user: "1234:1234"
    entrypoint: ["/bin/sh", "-c"]
    environment:
      BASH_ENV: "/tmp/x"
      BRIDGE_CERT_FINGERPRINT: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
EOF
}

# The collector secret/volume/network exception above is granted to any
# service named mbsync-<uuid>; nothing verified it still runs mbsync's
# own build or image, or that it set no entrypoint/command override
# (review round 2, finding 2: a substituted but otherwise hardened image
# would still receive the account password, Maildir/state volumes and
# network). A healthcheck, post_start/pre_stop hook or mounted config is
# the same risk under a different field: each runs its own command
# inside the container while the image, entrypoint and command stay
# mbsync's own (review round 3). extra_hosts is the same gap under a
# routing field: AGENTS.md forbids it on any mbsync-family service
# without the #1499 decision, but nothing held a collector to that
# (review round 3).
generated_collectors_keep_the_mbsync_implementation() {
    # The pre_start check just above (HARDENING_VIOLATIONS) has no case
    # here: the CI runner's Compose predates that field and refuses it
    # with a schema error before this check runs (#1631).
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: image or build differs from mbsync" \
        "mbsync-$UUID_B: sets an entrypoint override" \
        "mbsync-$UUID_B: sets a command override" \
        "mbsync-$UUID_B: sets a healthcheck override" \
        "mbsync-$UUID_B: sets a post_start hook" \
        "mbsync-$UUID_B: sets a pre_stop hook" \
        "mbsync-$UUID_B: mounts a config" \
        "mbsync-$UUID_B: sets extra_hosts" <<EOF || return 1
services:
  mbsync-$UUID_B:
    build: !reset null
    image: example.invalid/rootful:1
    entrypoint: ["/bin/sh", "-c"]
    command: ["true"]
    healthcheck:
      test: ["CMD", "true"]
    post_start:
      - command: ["true"]
    pre_stop:
      - command: ["true"]
    extra_hosts:
      - "host.docker.internal:203.0.113.1"
    configs:
      - source: proton_settings
        target: /app/proton-settings.sh
configs:
  proton_settings:
    content: "echo pwned"
EOF
    # provider delegates the service's whole lifecycle to an external
    # provider component while image and build can stay mbsync's own,
    # so it is its own case: docker compose up would invoke the
    # provider instead of starting mbsync, or fail when it is
    # unavailable, despite the checks above all passing unchanged
    # (review round 9).
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: sets a provider" <<EOF
services:
  mbsync-$UUID_B:
    provider:
      type: model
EOF
}

# entrypoint: [] / command: [] is the same execution-path override as
# above, but set() (review round 2/3's helper) treats an empty list as
# an unset key, so this case slipped past the non-empty fixture above
# (review round 4, finding 2). Confirmed directly against docker compose
# config: an empty-list override still renders as [], distinct from the
# key being absent, so the check must test key presence instead.
generated_collectors_reject_empty_entrypoint_and_command_overrides() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: sets an entrypoint override" \
        "mbsync-$UUID_B: sets a command override" <<EOF
services:
  mbsync-$UUID_B:
    entrypoint: []
    command: []
EOF
}

# A collector reusing mbsync's own image still runs as whatever user:
# the Compose file names, which takes precedence over the image's own
# USER mbsync (UID 1001, mbsync/Dockerfile). Neither the generic
# non-root check nor the image/build check above catches a substituted
# non-root UID, which still cannot write /maildir (owned by 1001:1001)
# or /state (review round 4, finding 4).
generated_collectors_require_the_mbsync_uid() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: user is not the mbsync UID 1001:1001" <<EOF
services:
  mbsync-$UUID_B:
    user: "1002:1002"
EOF
}

# A variable Bash itself reads at startup (BASH_ENV, and ENV for a POSIX
# sh invoked non-interactively) runs injected code before /entrypoint.sh
# does anything, with the same Bridge password and bridge-net access a
# collector legitimately keeps -- an alternate execution path under the
# environment field rather than entrypoint/command/healthcheck/the lifecycle
# hooks already covered above (review round 4, finding 5, security). Held
# to the environment keys docker-compose.yml's own mbsync service names,
# which already cover everything collector_env()
# (scripts/config/operator_config.py) sets.
generated_collectors_reject_unexpected_environment_keys() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: environment sets BASH_ENV, which mbsync does not set" <<EOF
services:
  mbsync-$UUID_B:
    environment:
      BASH_ENV: "/tmp/x"
EOF
}

# The key-name check above only rejects an extra key; it never required
# the account's own declared values to actually come through. A
# renderer regression or overlay could keep BRIDGE_USER but empty it
# (dropping the key merges the environment map the same way, keeping
# every other key), and mbsync's entrypoint exits on an empty
# BRIDGE_USER -- the account could never start despite passing every
# check above it (review round 6, finding 3).
generated_collectors_require_declared_environment_values() {
    expect_generated_rejected three-proton "" \
        "mbsync-$UUID_B: environment BRIDGE_USER does not match its declaration" <<EOF
services:
  mbsync-$UUID_B:
    environment:
      BRIDGE_USER: ""
EOF
}

# Reproduces review round 4, finding 3: an extra but fully hardened
# service that render_compose emits only for one case passes every
# check above (it is new, non-root and keeps no base resource), so only
# the per-case service-set check -- not one run after the whole loop
# against a later, unaffected case -- catches it.
generated_collectors_service_set_catches_a_case_only_extra_service() {
    cat >"$WORK/extra-service.yml" <<'EOF'
services:
  extra:
    image: example.invalid/extra:1
    init: true
    read_only: true
    user: "65534:65534"
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    pids_limit: 16
    logging:
      driver: none
EOF
    expect_generated_hardening one-proton "$WORK/extra-service.yml" || return 1
    if expect_generated_service_set one-proton; then
        printf 'the one-proton service set accepted an unexpected extra service\n'
        return 1
    fi
    # The regression this guards against: the unmodified, later case
    # still passes its own check, so an assertion run only once at the
    # end of the loop would see nothing wrong.
    expect_generated_hardening three-proton || return 1
    expect_generated_service_set three-proton
}

the_base_runs_mbsync_indexer_and_mcp_server() {
    render "$BASE"
    expect '.services | keys == ["indexer", "mbsync", "mcp-server"]' || return 1
    # Bridge runs on the host, outside Compose: nothing for mbsync to wait on.
    expect '(.services.mbsync.depends_on // {}) == {}' || return 1
    expect '.services.indexer.depends_on.mbsync.condition == "service_healthy"' || return 1
    expect '.services["mcp-server"].depends_on.indexer.condition == "service_healthy"' || return 1
}

# #1192: the published port and its container target must both follow
# MCP_PORT, so a literal in ports: cannot leave the server unreachable.
mcp_port_follows_the_published_port() {
    RENDER_MCP_PORT=4000 render "$BASE"
    expect '.services["mcp-server"].environment.MCP_PORT == "4000"' || return 1
    expect '.services["mcp-server"].ports | map("\(.host_ip):\(.published):\(.target)/\(.protocol)") == ["127.0.0.1:4000:4000/tcp"]' || return 1
}

# The Proton settings of PLAN.md decision 46 (#1497): Compose passes each
# as set, empty when unset, without a warning; the entrypoint applies the
# default (proton-bridge, host.docker.internal, 127.0.0.1, All
# Mail|Labels/*|Starred, today's isync names) and both it and
# validate-env.sh refuse anything mbsync/proton-settings.sh does not
# allow (mbsync/tests/entrypoint_test.sh walks the values). The port and
# pin rotation keep their Compose defaults, 1143 and false.
readonly PROTON_SETTINGS=(SOURCE_MODE BRIDGE_HOST BRIDGE_CERT_HOST PROTON_FOLDER_EXCLUSIONS
    PROTON_IMAP_ACCOUNT_NAME PROTON_REMOTE_STORE_NAME PROTON_LOCAL_STORE_NAME PROTON_CHANNEL_NAME)

mbsync_points_at_the_host_app() {
    local name
    render "$BASE"
    for name in "${PROTON_SETTINGS[@]}"; do
        expect ".services.mbsync.environment.${name} == \"\"" || return 1
        if grep -F "\"${name}\" variable is not set" "$WORK/compose.err"; then
            return 1
        fi
    done
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1143"' || return 1
    # Pin rotation is a one-run command, not a setting (#1609).
    expect '.services.mbsync.environment | has("BRIDGE_CERT_PIN_ROTATE") | not' || return 1
    # Empty unless set: the entrypoint then refuses to trust the app's
    # certificate on first use.
    expect '.services.mbsync.environment.BRIDGE_CERT_FINGERPRINT == ""' || return 1
}

mbsync_takes_the_expected_fingerprint_from_the_environment() {
    BRIDGE_CERT_FINGERPRINT="ab:cd" render "$BASE"
    expect '.services.mbsync.environment.BRIDGE_CERT_FINGERPRINT == "ab:cd"' || return 1
}

mbsync_takes_the_proton_settings_from_the_environment() {
    local name RENDER_ENV=()
    for name in "${PROTON_SETTINGS[@]}"; do
        RENDER_ENV+=("${name}=synthetic-${name}")
    done
    render "$BASE"
    for name in "${PROTON_SETTINGS[@]}"; do
        expect ".services.mbsync.environment.${name} == \"synthetic-${name}\"" || return 1
    done
}

mbsync_takes_the_port_from_the_environment() {
    BRIDGE_IMAP_PORT=1144 render "$BASE"
    expect '.services.mbsync.environment.BRIDGE_IMAP_PORT == "1144"' || return 1
}

mbsync_keeps_its_hardening_and_no_port_is_exposed() {
    render "$BASE"
    expect_hardening_and_exposure || return 1
    expect '.services.mbsync.secrets | map(.source) == ["bridge_pass"]' || return 1
    expect '.services.mbsync.networks | keys == ["bridge-net"]' || return 1
    expect '.services.mbsync.pids_limit == 128' || return 1
    expect '.services.mbsync.tmpfs == ["/tmp"]' || return 1
    expect '[.services.mbsync.volumes[] | "\(.source):\(.target)"] == ["maildir-volume:/maildir", "mbsync-state:/state"]' || return 1
    expect '.services.indexer.volumes[] | select(.target == "/maildir") | .read_only' || return 1
    expect '.services.mbsync | has("extra_hosts") | not' || return 1
}

# The optional rerank and inference settings default to empty (#1058,
# #1074): a .env that sets the required endpoints but omits
# RERANK_BASE_URL, RERANK_MODEL and INFERENCE_BASE_URL (one written
# before those layers landed, or one that leaves them out with
# RERANK_MODE=none and INFERENCE_MODE=none) renders with no "variable is
# not set" warning. validate-env.sh still requires the rerank pair when
# RERANK_MODE=cohere and INFERENCE_BASE_URL when INFERENCE_MODE is not
# none.
omitted_rerank_and_inference_settings_render_without_a_warning() {
    ACCOUNT_SCOPE=00000000-0000-4000-8000-000000000001 EMBED_BASE_URL=default \
        EMBED_MODEL=synthetic-embed render "$BASE"
    expect '.services["mcp-server"].environment.RERANK_MODE == "none"' || return 1
    expect '.services["mcp-server"].environment.RERANK_BASE_URL == ""' || return 1
    expect '.services["mcp-server"].environment.RERANK_MODEL == ""' || return 1
    expect '.services["mcp-server"].environment.INFERENCE_MODE == "none"' || return 1
    expect '.services["mcp-server"].environment.INFERENCE_BASE_URL == ""' || return 1
    if grep -F 'is not set' "$WORK/compose.err"; then
        return 1
    fi
}

# ACCOUNT_SCOPE (#1596) is a required setting, so Compose keeps it bare
# and warns when it is unset (validate-env.sh and every service refuse
# it); SOURCE_MODE is optional. Both reach every service: mbsync names
# the account in its collection status (#1592).
account_scope_is_required_and_reaches_every_service() {
    local service
    ACCOUNT_SCOPE=00000000-0000-4000-8000-000000000001 EMBED_BASE_URL=default \
        EMBED_MODEL=synthetic-embed render "$BASE"
    for service in mbsync indexer mcp-server; do
        expect ".services[\"$service\"].environment.ACCOUNT_SCOPE == \"00000000-0000-4000-8000-000000000001\"" || return 1
        expect ".services[\"$service\"].environment.SOURCE_MODE == \"\"" || return 1
    done
    EMBED_BASE_URL=default EMBED_MODEL=synthetic-embed render "$BASE"
    grep -E 'ACCOUNT_SCOPE[\\"]* variable is not set' "$WORK/compose.err" >/dev/null
}

# Each image is built with the source commit it logs at startup (#887):
# `make build` passes it, a plain `docker compose build` gets "unknown".
every_image_is_built_with_the_source_commit() {
    render "$BASE"
    expect '[.services[] | .build.args.GIT_COMMIT == "unknown"] | all' || return 1
    GIT_COMMIT="abc1234-dirty" render "$BASE"
    expect '[.services[] | .build.args.GIT_COMMIT == "abc1234-dirty"] | all' || return 1
}

# Git and make in a scratch directory, isolated from the caller's Git
# configuration and environment.
scratch_git() {
    local dir="$1"
    shift
    env -u GIT_DIR -u GIT_WORK_TREE GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
        git -C "$dir" -c user.name=synthetic -c user.email=synthetic@example.invalid \
        -c commit.gpgsign=false "$@"
}

# Runs both build targets dry in directory $1, with any NAME=value
# arguments that follow added to the environment.
scratch_make_commit() {
    local dir="$1"
    shift
    env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMIT -u GIT_COMMIT_OVERRIDE \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "$@" \
        make --no-print-directory -C "$dir" -n build build-nocache 2>"$WORK/make.err"
}

# Fails unless both build targets pass GIT_COMMIT=$2 in directory $1;
# any further NAME=value arguments go into make's environment.
expect_make_commit() {
    local dir="$1" expected="$2" output
    shift 2
    output="$(scratch_make_commit "$dir" "$@")"
    [[ "$(grep -cxE "GIT_COMMIT=$expected docker compose build( --no-cache)? *" <<<"$output")" -eq 2 ]] || {
        printf 'expected GIT_COMMIT=%s from both targets:\n%s\n' "$expected" "$output"
        cat "$WORK/make.err"
        return 1
    }
}

# The Makefile in a scratch repository (#893):
# the short HEAD hash when clean, -dirty for a modified tracked file or
# a new untracked one, but not for a git-ignored file such as .env, and
# empty outside a checkout.
make_build_passes_the_source_commit() {
    local repo="$WORK/commit-repo" plain="$WORK/commit-plain" head
    mkdir -p "$repo" "$plain"
    cp "$ROOT_DIR/Makefile" "$repo/Makefile"
    cp "$ROOT_DIR/Makefile" "$plain/Makefile"
    printf '.env\n' >"$repo/.gitignore"
    scratch_git "$repo" init -q
    scratch_git "$repo" add Makefile .gitignore
    scratch_git "$repo" commit -q -m synthetic
    head="$(scratch_git "$repo" rev-parse --short HEAD)"
    expect_make_commit "$repo" "$head" || return 1
    # #893: a GIT_COMMIT left in the shell or CI
    # does not replace the checkout's commit; GIT_COMMIT_OVERRIDE does.
    expect_make_commit "$repo" "$head" GIT_COMMIT=stale || return 1
    expect_make_commit "$repo" synthetic-override GIT_COMMIT=stale \
        GIT_COMMIT_OVERRIDE=synthetic-override || return 1
    printf 'SYNTHETIC=1\n' >"$repo/.env"
    expect_make_commit "$repo" "$head" || return 1
    printf 'untracked\n' >"$repo/new-file"
    expect_make_commit "$repo" "$head-dirty" || return 1
    rm "$repo/new-file"
    printf '# modified\n' >>"$repo/.gitignore"
    expect_make_commit "$repo" "$head-dirty" || return 1
    expect_make_commit "$plain" "" || return 1
}

the_base_composes_with_the_hardened_overlay() {
    render "$BASE" "$HARDENED"
    expect '.services | keys == ["indexer", "mbsync", "mcp-server"]' || return 1
    expect '.networks["app-net"].internal' || return 1
    expect '.services.mbsync.environment.BRIDGE_HOST == ""' || return 1
}

# The base file's own render, every profile included: what the merged
# hardening check holds each service to.
ALL_PROFILES=1 render "$BASE"
cp "$WORK/config.json" "$WORK/base.json"
# The repository's own files declare no generated collector, so they get
# no exception.
printf '[]\n' >"$WORK/no-collectors.json"
# The generated Compose files and the removal of the base mbsync service
# that the launcher's replacement stands for.
python3 "$ROOT_DIR/scripts/config/tests/synthetic_compose.py" "$WORK/generated"
printf 'services:\n  mbsync: !reset null\n' >"$WORK/replace-mbsync.yml"

check "the base runs mbsync, indexer and mcp-server, with no Bridge dependency" \
    the_base_runs_mbsync_indexer_and_mcp_server
check "mbsync points at the host app" mbsync_points_at_the_host_app
check "mbsync takes the IMAP port from the environment" mbsync_takes_the_port_from_the_environment
check "mbsync takes the Proton settings from the environment" \
    mbsync_takes_the_proton_settings_from_the_environment
check "mcp-server publishes the port MCP_PORT names on both sides" mcp_port_follows_the_published_port
check "mbsync takes the expected fingerprint from the environment" \
    mbsync_takes_the_expected_fingerprint_from_the_environment
check "mbsync keeps its hardening and no new port is exposed" \
    mbsync_keeps_its_hardening_and_no_port_is_exposed
check "the base composes with the hardened overlay" the_base_composes_with_the_hardened_overlay
check "merged hardening holds for every overlay combination" \
    merged_hardening_holds_for_every_overlay_combination
check "merged hardening rejects !reset on security_opt" merged_hardening_rejects_reset_security_opt
check "merged hardening rejects !reset on cap_drop" merged_hardening_rejects_reset_cap_drop
check "merged hardening rejects !reset on read_only" merged_hardening_rejects_reset_read_only
check "merged hardening rejects !reset on pids_limit and logging" \
    merged_hardening_rejects_reset_pids_limit_and_logging
check "merged hardening rejects !reset on tmpfs" merged_hardening_rejects_reset_tmpfs
check "merged hardening rejects !override on security_opt" \
    merged_hardening_rejects_override_security_opt
check "merged hardening rejects missing or retargeted base resources" \
    merged_hardening_rejects_removed_base_resources
check "merged hardening rejects !override of a read-only volume" \
    merged_hardening_rejects_override_of_a_read_only_volume
check "merged hardening rejects !override on ports and networks" \
    merged_hardening_rejects_override_ports_and_networks
check "merged hardening rejects the forbidden settings" merged_hardening_rejects_forbidden_settings
check "merged hardening rejects redefined secrets" merged_hardening_rejects_redefined_secrets
check "merged hardening rejects a new image or build without a user" \
    merged_hardening_rejects_a_new_image_without_a_user
check "merged hardening rejects volumes an overlay adds" merged_hardening_rejects_added_volumes
check "merged hardening rejects a second writer of the account Maildir" \
    merged_hardening_rejects_a_second_maildir_writer
check "merged hardening rejects leading-zero root users" \
    merged_hardening_rejects_leading_zero_root_users
check "merged hardening rejects an !override that drops a service" \
    merged_hardening_rejects_override_dropping_a_service
check "merged hardening rejects max-size on a driver other than json-file" \
    merged_hardening_rejects_max_size_on_another_logging_driver
check "merged hardening rejects renamed or shared networks" merged_hardening_rejects_renamed_networks
check "merged hardening rejects services moved into a profile" \
    merged_hardening_rejects_services_moved_into_a_profile
check "merged hardening rejects network driver changes" \
    merged_hardening_rejects_network_driver_changes
check "merged hardening rejects renamed, shared or rebacked volumes" \
    merged_hardening_rejects_renamed_or_rebacked_volumes
check "merged hardening rejects new users, secrets, hooks and devices" \
    merged_hardening_rejects_new_grants
check "merged hardening rejects a command or entrypoint on a base service" \
    merged_hardening_rejects_a_command_or_entrypoint
check "the indexer has no deletion force setting and no service sets a command" \
    indexer_has_no_deletion_force_setting
check "merged hardening rejects a service a top-level include brings in" \
    merged_hardening_rejects_an_included_service
check "GENERATED_CASES covers what the generator wrote" generated_cases_cover_the_generator_output
check "generated collectors keep the hardening" generated_collectors_keep_the_hardening
check "generated collectors reject another account's secret" \
    generated_collectors_reject_another_accounts_secret
check "generated collectors reject a wrong network" generated_collectors_reject_a_wrong_network
check "generated collectors reject an extra mount" generated_collectors_reject_an_extra_mount
check "generated Maildir volumes have one writer" generated_maildir_has_one_writer
check "generated collectors' secret, volumes and network stay their own" \
    generated_collectors_resources_stay_their_own
check "generated collectors are declared, present and checked as new services" \
    generated_collectors_are_declared_present_and_checked
check "generated collectors keep their own secret, volumes, network and names" \
    generated_collectors_keep_their_own_resources
check "generated imap collectors get no Bridge secret or bridge-net" \
    generated_imap_collectors_get_no_bridge_secret_or_network
check "generated collectors keep the mbsync baseline hardening" \
    generated_collectors_keep_the_mbsync_baseline_hardening
check "generated collectors reject a blocking dependency" \
    generated_collectors_reject_a_blocking_dependency
check "generated collectors reject read-only data volumes" \
    generated_collectors_reject_read_only_data_volumes
check "generated collectors bind trust values to their declarations" \
    generated_collectors_bind_trust_values_to_their_declarations
check "generated collectors apply the implementation and trust bindings to the legacy service" \
    generated_collectors_apply_bindings_to_the_legacy_service
check "generated collectors keep the mbsync implementation" \
    generated_collectors_keep_the_mbsync_implementation
check "generated collectors reject empty entrypoint and command overrides" \
    generated_collectors_reject_empty_entrypoint_and_command_overrides
check "generated collectors require the mbsync UID" \
    generated_collectors_require_the_mbsync_uid
check "generated collectors reject unexpected environment keys" \
    generated_collectors_reject_unexpected_environment_keys
check "generated collectors require declared environment values" \
    generated_collectors_require_declared_environment_values
check "generated collectors' service set catches a case-only extra service" \
    generated_collectors_service_set_catches_a_case_only_extra_service
check "omitted rerank and inference settings render without a warning" \
    omitted_rerank_and_inference_settings_render_without_a_warning
check "ACCOUNT_SCOPE is required and reaches every service" \
    account_scope_is_required_and_reaches_every_service
check "every image is built with the source commit" every_image_is_built_with_the_source_commit
check "make build passes the source commit" make_build_passes_the_source_commit

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
