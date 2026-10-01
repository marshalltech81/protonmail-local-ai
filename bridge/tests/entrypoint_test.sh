#!/bin/bash
set -Eeuo pipefail

# Tests for bridge/entrypoint.sh: the launch-mode decision and the GPG /
# pass bootstrap that runs before it.
#
# Each case loads the real function definitions from the entrypoint,
# points its state paths at a temporary directory, and replaces gpg, pass
# and timeout with shell functions that keep a synthetic keyring in files.
# main() ends in `exec bridge ...`, so bridge is a mock executable on PATH
# that records its arguments. No Docker, Bridge, keyring or Proton state is
# touched, and no credential is real.
#
# Run: bash bridge/tests/entrypoint_test.sh

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$TESTS_DIR/../entrypoint.sh"
FIRST_RUN_OVERLAY="$TESTS_DIR/../../docker-compose.first-run.yml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# Define the named functions exactly as the entrypoint writes them: from
# the `name() {` line through the first line that is a lone `}`.
load() {
    local name definition
    for name in "$@"; do
        definition="$(awk -v start="${name}() {" '$0 == start {p = 1} p {print} p && $0 == "}" {exit}' "$ENTRYPOINT")"
        if [[ -z "$definition" ]]; then
            printf 'cannot find %s() in %s\n' "$name" "$ENTRYPOINT" >&2
            exit 1
        fi
        eval "$definition"
    done
}

# Runs each case in a subshell outside any condition, so errexit stays on
# inside it and every assertion counts, not only the last one.
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

# A synthetic fingerprint in gpg's format: 40 uppercase hex digits.
TEST_FPR="$(printf 'A%.0s' {1..40})"
readonly TEST_FPR

# --- harness ----------------------------------------------------------------

# Points the entrypoint's state paths at a fresh directory and installs the
# mocks. The synthetic keyring is two marker files: "public" and "secret".
# shellcheck disable=SC2034,SC2329 # used by the entrypoint functions loaded with eval
setup() {
    STATE="$WORK/state-$1"
    BIN="$WORK/bin-$1"
    mkdir -p "$STATE/config/protonmail/bridge-v3" "$STATE/gnupg" "$STATE/pass" "$BIN"
    VAULT="$STATE/config/protonmail/bridge-v3/vault.enc"
    PASSWORD_STORE_DIR="$STATE/pass"
    PASS_STORE_ID_FILE="$PASSWORD_STORE_DIR/.gpg-id"
    GPG_STATE="$STATE/gnupg"
    BOOTSTRAP_TIMEOUT_SECONDS=1
    unset BRIDGE_FORCE_CLI
    CALLS="$STATE/calls"
    : >"$CALLS"

    # Bridge creates vault.enc at startup, before any login (see
    # scripts/bridge-smoke.sh), so the mock does too. It then records how
    # it was launched and exits, as the CLI does on EOF.
    export CALLS VAULT
    cat >"$BIN/bridge" <<'MOCK'
#!/bin/bash
: >>"$VAULT"
printf 'bridge %s\n' "$*" >>"$CALLS"
MOCK
    chmod 755 "$BIN/bridge"
    PATH="$BIN:$PATH"

    # Every function the entrypoint defines, so a new helper needs no edit here.
    local functions
    mapfile -t functions < <(awk '/^[a-z_]+\(\) \{$/ {sub(/\(\) \{$/, ""); print}' "$ENTRYPOINT")
    load "${functions[@]}"
}

# timeout(1) can only run executables; this stand-in runs the mocks below.
# shellcheck disable=SC2329 # called by the entrypoint functions loaded with eval
timeout() {
    shift
    "$@"
}

# shellcheck disable=SC2329 # called by the entrypoint functions loaded with eval
gpg() {
    printf 'gpg %s\n' "$*" >>"$CALLS"
    case "$*" in
        *--quick-gen-key*)
            : >"$GPG_STATE/public"
            : >"$GPG_STATE/secret"
            ;;
        *--list-secret-keys*--with-colons*)
            [[ -e "$GPG_STATE/secret" ]] || return 2
            printf 'sec:u:255:22:0000000000000000:0:::u:::scESC:::+:::23::0:\n'
            printf 'fpr:::::::::%s:\n' "$TEST_FPR"
            ;;
        *--list-secret-keys*)
            [[ -e "$GPG_STATE/secret" ]] || return 2
            ;;
        *--list-keys*--with-colons*)
            [[ -e "$GPG_STATE/public" ]] || return 2
            printf 'pub:u:255:22:0000000000000000:0:::u:::scESC:::::23::0:\n'
            printf 'fpr:::::::::%s:\n' "$TEST_FPR"
            ;;
        *--list-keys*)
            [[ -e "$GPG_STATE/public" ]] || return 2
            ;;
        *--decrypt*)
            [[ -e "$GPG_STATE/secret" && ! -e "$GPG_STATE/undecryptable" ]] || return 2
            ;;
        *)
            printf 'unexpected gpg call: %s\n' "$*" >&2
            return 1
            ;;
    esac
}

# shellcheck disable=SC2329 # called by the entrypoint functions loaded with eval
pass() {
    printf 'pass %s\n' "$*" >>"$CALLS"
    [[ "$1" == "init" ]] || return 1
    printf '%s\n' "$2" >"$PASS_STORE_ID_FILE"
}

# Runs main() in a subshell (it ends in exec) with errexit on, as the
# entrypoint does, and keeps its output in $OUT and its status in $RC.
run_main() {
    set +e
    (set -e; main "$@") >"$STATE/out" 2>&1
    RC=$?
    set -e
    OUT="$(cat "$STATE/out")"
}

# A state where a previous run generated the key and initialized the pass
# store, and Bridge created its vault and stored the vault key in pass.
existing_install() {
    : >"$GPG_STATE/public"
    : >"$GPG_STATE/secret"
    printf '%s\n' "$TEST_FPR" >"$PASS_STORE_ID_FILE"
    mkdir -p "$PASSWORD_STORE_DIR/protonmail"
    : >"$PASSWORD_STORE_DIR/protonmail/vault-key.gpg"
    : >"$VAULT"
}

called() {
    grep -q -- "$1" "$CALLS"
}

# No key generated, no pass store re-initialized, nothing launched: the
# damaged state is reported, not repaired or started.
refused_without_changes() {
    ((RC != 0))
    if called 'quick-gen-key' || called '^pass init' || called '^bridge '; then
        printf 'damaged state was changed or launched:\n%s\n' "$(cat "$CALLS")"
        return 1
    fi
    [[ "$OUT" == *"$1"* ]]
    [[ "$OUT" == *"bridge-data"* ]]
    [[ -f "$VAULT" ]]
}

launched_with() {
    local last
    last="$(grep '^bridge ' "$CALLS" | tail -n 1)"
    if [[ "$last" != "bridge $1" ]]; then
        printf 'expected "bridge %s", got "%s"; main printed:\n%s\n' "$1" "$last" "$OUT"
        return 1
    fi
}

# --- first-run login retry (#242) -------------------------------------------
#
# Bridge writes vault.enc before any login, so a vault is no proof of an
# account. make first-run sets BRIDGE_FORCE_CLI=true, which opens the CLI
# whatever the volume holds.

fresh_install_opens_the_cli() {
    setup fresh
    run_main
    ((RC == 0))
    launched_with --cli
}

retried_first_run_opens_the_cli_again() {
    setup retry
    export BRIDGE_FORCE_CLI=true
    # The first attempt exits before login; the mock leaves a vault behind.
    run_main
    ((RC == 0))
    [[ -f "$VAULT" ]]
    launched_with --cli
    run_main
    ((RC == 0))
    launched_with --cli
}

forced_cli_opens_the_cli_over_an_existing_account() {
    setup forced-existing
    existing_install
    export BRIDGE_FORCE_CLI=true
    run_main
    ((RC == 0))
    launched_with --cli
}

existing_vault_starts_noninteractive_by_default() {
    setup existing
    existing_install
    run_main
    ((RC == 0))
    launched_with --noninteractive
}

forced_cli_false_keeps_the_default() {
    setup forced-false
    existing_install
    export BRIDGE_FORCE_CLI=false
    run_main
    ((RC == 0))
    launched_with --noninteractive
}

invalid_force_cli_value_fails_closed() {
    setup forced-invalid
    existing_install
    export BRIDGE_FORCE_CLI=yes
    run_main
    ((RC != 0))
    [[ "$OUT" == *"BRIDGE_FORCE_CLI must be"* ]]
    [[ "$(grep -c '^bridge ' "$CALLS")" == 0 ]]
}

first_run_overlay_forces_the_cli() {
    grep -Eq '^ +BRIDGE_FORCE_CLI: "true"$' "$FIRST_RUN_OVERLAY"
}

# --- existing credential state is checked, never rebuilt (#266) --------------
#
# With a vault, the key and the pass store hold the vault key Bridge needs,
# so generating a key or re-initializing pass would only hide the damage.
# Each damaged state fails closed and leaves the files in place, also under
# BRIDGE_FORCE_CLI. Without a vault there is nothing to lose, so a fresh
# install generates the key and initializes pass as before.

fresh_install_generates_the_key_and_initializes_pass() {
    setup fresh-bootstrap
    run_main
    ((RC == 0))
    called 'quick-gen-key'
    called "^pass init $TEST_FPR\$"
    [[ "$(cat "$PASS_STORE_ID_FILE")" == "$TEST_FPR" ]]
    launched_with --cli
}

intact_existing_state_starts_without_rebuilding() {
    setup intact
    existing_install
    run_main
    ((RC == 0))
    if called 'quick-gen-key' || called '^pass init'; then
        echo "intact credential state was rebuilt"
        return 1
    fi
    called '--decrypt .*vault-key.gpg'
    launched_with --noninteractive
}

public_only_keyring_is_refused() {
    setup public-only
    existing_install
    rm "$GPG_STATE/secret"
    run_main
    refused_without_changes "private key"
}

missing_key_is_refused() {
    setup missing-key
    existing_install
    rm "$GPG_STATE/public" "$GPG_STATE/secret"
    run_main
    refused_without_changes "private key"
}

missing_pass_metadata_is_refused() {
    setup missing-gpg-id
    existing_install
    rm "$PASS_STORE_ID_FILE"
    run_main
    refused_without_changes "pass store"
    [[ ! -e "$PASS_STORE_ID_FILE" ]]
}

pass_metadata_for_another_key_is_refused() {
    setup other-gpg-id
    existing_install
    printf '%s\n' "$(printf 'B%.0s' {1..40})" >"$PASS_STORE_ID_FILE"
    run_main
    refused_without_changes "pass store"
}

undecryptable_pass_entry_is_refused() {
    setup undecryptable
    existing_install
    : >"$GPG_STATE/undecryptable"
    run_main
    refused_without_changes "cannot be decrypted"
}

forced_cli_does_not_bypass_the_check() {
    setup forced-damaged
    existing_install
    rm "$GPG_STATE/secret"
    export BRIDGE_FORCE_CLI=true
    run_main
    refused_without_changes "private key"
}

fresh_install_repairs_missing_pass_metadata() {
    setup fresh-repair
    : >"$GPG_STATE/public"
    : >"$GPG_STATE/secret"
    run_main
    ((RC == 0))
    if called 'quick-gen-key'; then
        echo "a second key was generated over a usable one"
        return 1
    fi
    called "^pass init $TEST_FPR\$"
    launched_with --cli
}

fresh_install_refuses_a_public_only_keyring() {
    setup fresh-public-only
    : >"$GPG_STATE/public"
    run_main
    ((RC != 0))
    if called 'quick-gen-key' || called '^pass init' || called '^bridge '; then
        printf 'public-only keyring was changed or launched:\n%s\n' "$(cat "$CALLS")"
        return 1
    fi
    [[ "$OUT" == *"private key"* ]]
}

# --- a command is refused, not ignored (#270) ---------------------------------
#
# `docker compose run protonmail-bridge <command>` passes the command to the
# entrypoint. It used to be ignored, so the old documented credential
# shortcut started a second, noninteractive Bridge on the same volume. The
# entrypoint takes no arguments; credentials are shown through make first-run.

command_arguments_are_refused() {
    setup arguments
    existing_install
    run_main su -s /bin/bash bridge -c 'bridge info'
    ((RC != 0))
    [[ "$OUT" == *"make first-run"* ]]
    # Refused before any check or bootstrap runs, so nothing is called.
    [[ ! -s "$CALLS" ]]
}

check "a fresh install opens the CLI" fresh_install_opens_the_cli
check "a retried first run opens the CLI again (#242)" retried_first_run_opens_the_cli_again
check "BRIDGE_FORCE_CLI opens the CLI over an existing account" \
    forced_cli_opens_the_cli_over_an_existing_account
check "an existing vault starts noninteractive by default" \
    existing_vault_starts_noninteractive_by_default
check "BRIDGE_FORCE_CLI=false keeps the default" forced_cli_false_keeps_the_default
check "an invalid BRIDGE_FORCE_CLI fails closed" invalid_force_cli_value_fails_closed
check "the first-run overlay sets BRIDGE_FORCE_CLI=true" first_run_overlay_forces_the_cli
check "a fresh install generates the key and initializes pass" \
    fresh_install_generates_the_key_and_initializes_pass
check "intact existing state starts without rebuilding it" \
    intact_existing_state_starts_without_rebuilding
check "a public-only keyring with a vault is refused (#266)" public_only_keyring_is_refused
check "a missing key with a vault is refused" missing_key_is_refused
check "missing pass metadata with a vault is refused" missing_pass_metadata_is_refused
check "pass metadata for another key is refused" pass_metadata_for_another_key_is_refused
check "an undecryptable pass entry is refused" undecryptable_pass_entry_is_refused
check "BRIDGE_FORCE_CLI does not bypass the check" forced_cli_does_not_bypass_the_check
check "a fresh install repairs missing pass metadata" fresh_install_repairs_missing_pass_metadata
check "a fresh install refuses a public-only keyring" \
    fresh_install_refuses_a_public_only_keyring
check "command arguments are refused (#270)" command_arguments_are_refused

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
