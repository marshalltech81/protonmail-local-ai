#!/bin/bash
set -Eeuo pipefail

# Bridge expects these runtime paths to be set explicitly. Unset before
# exporting so a caller-injected value cannot silently redirect Bridge state
# to an unexpected path. Exported here rather than baked into Dockerfile
# metadata so Trivy does not flag PASSWORD_STORE_DIR as a leaked secret.
unset HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME GNUPGHOME PASSWORD_STORE_DIR
export HOME="/home/bridge"
export XDG_CONFIG_HOME="/data/config"
export XDG_DATA_HOME="/data/local"
export XDG_CACHE_HOME="/data/cache"
export GNUPGHOME="/data/gnupg"
export PASSWORD_STORE_DIR="/data/pass"

readonly VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"
readonly PASS_STORE_ID_FILE="$PASSWORD_STORE_DIR/.gpg-id"
readonly BOOTSTRAP_TIMEOUT_SECONDS=30

run_with_timeout() {
    local description="$1"
    shift

    if ! timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" "$@"; then
        echo "ERROR: ${description} failed or timed out after ${BOOTSTRAP_TIMEOUT_SECONDS}s." >&2
        exit 1
    fi
}

have_bridge_key() {
    timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" gpg --list-keys "ProtonBridge" >/dev/null 2>&1
}

# The public key alone cannot decrypt the pass store, so readiness needs the
# private key.
have_bridge_secret_key() {
    timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" gpg --list-secret-keys "ProtonBridge" >/dev/null 2>&1
}

bridge_fingerprint() {
    timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" gpg --list-secret-keys --with-colons "ProtonBridge" \
        | awk -F: '/^fpr/{print $10; exit}'
}

# The key the pass store encrypts to, or nothing when .gpg-id is missing.
pass_store_id() {
    if [[ -f "$PASS_STORE_ID_FILE" ]]; then
        cat "$PASS_STORE_ID_FILE"
    fi
}

refuse_damaged_state() {
    echo "ERROR: vault.enc exists but $1." >&2
    echo "       Bridge's vault key is stored in the GPG-backed pass store, so the vault" >&2
    echo "       cannot be opened. Nothing was changed. Restore the bridge-data volume" >&2
    echo "       from a backup, or remove it and run: make first-run" >&2
    exit 1
}

# =============================================================================
# Fresh install: no vault yet, so nothing depends on the existing key or pass
# store. Generate the key when there is none and point pass at it.
# The empty GPG passphrase is intentional: Bridge must restart unattended, so
# the design relies on Docker volume isolation, restrictive permissions, and
# host-level disk encryption rather than an interactive key-unlock step.
# =============================================================================
bootstrap_credentials() {
    local fpr

    if ! have_bridge_secret_key; then
        if have_bridge_key; then
            echo "ERROR: the GPG keyring holds the 'ProtonBridge' public key without its private key." >&2
            echo "       Remove the bridge-data volume and run: make first-run" >&2
            exit 1
        fi
        echo ">>> First run: generating the GPG key for the pass store..."
        run_with_timeout \
            "GPG key generation" \
            gpg --batch --passphrase '' --quick-gen-key \
            "ProtonBridge" default default never
    fi

    fpr="$(bridge_fingerprint)"
    [[ -n "$fpr" ]] \
        || { echo "ERROR: Failed to extract the GPG fingerprint." >&2; exit 1; }

    if [[ "$(pass_store_id)" != "$fpr" ]]; then
        echo ">>> Initializing the pass store..."
        run_with_timeout "pass store initialization" pass init "$fpr"
        echo ">>> GPG + pass initialized (fingerprint: $fpr)"
    fi
}

# =============================================================================
# Existing vault: its key lives in the pass store, encrypted to the
# ProtonBridge key. Generating a key or re-initializing pass here would only
# hide the damage, so check the chain and fail closed, changing nothing.
# =============================================================================
verify_existing_credentials() {
    local fpr entries entry

    have_bridge_secret_key \
        || refuse_damaged_state "the GPG private key 'ProtonBridge' is missing"

    fpr="$(bridge_fingerprint)" \
        || refuse_damaged_state "the GPG private key 'ProtonBridge' cannot be read"
    [[ -n "$fpr" ]] \
        || refuse_damaged_state "the GPG private key 'ProtonBridge' cannot be read"

    [[ "$(pass_store_id)" == "$fpr" ]] \
        || refuse_damaged_state "the pass store is not initialized for the 'ProtonBridge' key"

    # Decrypt every entry to /dev/null: proves the private key opens them
    # without the plaintext leaving gpg.
    entries="$(find "$PASSWORD_STORE_DIR" -type f -name '*.gpg')" \
        || refuse_damaged_state "the pass store cannot be listed"
    # Bridge keeps its vault key in the store, so a vault with no entry
    # beside it cannot be opened.
    [[ -n "$entries" ]] \
        || refuse_damaged_state "the pass store has no entries"
    while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" gpg --batch --quiet --decrypt "$entry" >/dev/null \
            || refuse_damaged_state "a pass store entry cannot be decrypted"
    done <<<"$entries"
}

main() {
    # The entrypoint runs no commands of its own: a command given to
    # `docker compose run protonmail-bridge ...` would otherwise be ignored
    # and start a second Bridge on the same volume. To see the Bridge
    # credentials, stop the stack and use make first-run (see docs/setup.md).
    if (($# > 0)); then
        echo "ERROR: the Bridge entrypoint takes no arguments." >&2
        echo "       To show the Bridge credentials: make down, make first-run, then 'info' in the CLI." >&2
        exit 1
    fi

    # make first-run sets BRIDGE_FORCE_CLI=true (docker-compose.first-run.yml).
    # Bridge writes vault.enc at startup, before any login, so a vault does
    # not show that an account is logged in: a retried, unfinished first run
    # must still get the CLI. Checked before anything touches the volume.
    local force_cli="${BRIDGE_FORCE_CLI:-false}"
    if [[ "$force_cli" != "true" && "$force_cli" != "false" ]]; then
        echo "ERROR: BRIDGE_FORCE_CLI must be 'true' or 'false'." >&2
        exit 1
    fi

    # The vault decides first: an existing vault is checked, never rebuilt;
    # without one, the key and pass store are bootstrapped. A vault is not
    # proof of a logged-in account: Bridge creates it before any login.
    local have_vault=false
    if [[ -f "$VAULT" ]]; then
        verify_existing_credentials
        have_vault=true
    else
        bootstrap_credentials
    fi

    # =========================================================================
    # Launch
    # =========================================================================
    if [[ "$have_vault" == true && "$force_cli" == false ]]; then
        echo ">>> Vault found. Starting Bridge as user '$(whoami)'..."

        # exec replaces this shell with the bridge process — Docker tracks
        # bridge directly and SIGTERM from docker stop reaches it without a
        # wrapper.
        exec bridge --noninteractive
    fi

    cat <<'EOF'

┌──────────────────────────────────────────────────────────────┐
│  Bridge interactive CLI                                      │
│                                                              │
│  Steps:                                                      │
│    login    → enter your Proton email, password, and 2FA     │
│               (skip if `info` already lists your account)    │
│    info     → copy bridge username + password into secrets   │
│    exit                                                      │
│                                                              │
│  Then: make up                                               │
└──────────────────────────────────────────────────────────────┘

EOF

    exec bridge --cli
}

main "$@"
