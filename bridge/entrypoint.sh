#!/bin/bash
set -Eeuo pipefail

VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"
LOG_DIR="$XDG_DATA_HOME/protonmail/bridge-v3/logs"

BRIDGE_PID=""
WATCHER_PID=""

cleanup() {
    for pid in "$BRIDGE_PID" "$WATCHER_PID"; do
        if [ -n "${pid:-}" ]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}

trap cleanup EXIT TERM INT

# =============================================================================
# Bootstrap GPG and pass on first run
# Only runs once and persists in the bridge-data volume
# =============================================================================
if ! gpg --list-keys "ProtonBridge" >/dev/null 2>&1; then
    echo ">>> First run: initializing GPG key and pass store..."

    gpg --batch --passphrase '' --quick-gen-key \
        'ProtonBridge' default default never >/dev/null 2>&1

    FPR="$(gpg --list-keys --with-colons 'ProtonBridge' | awk -F: '/^fpr/{print $10; exit}')"
    pass init "$FPR"

    echo ">>> GPG + pass initialized (fingerprint: $FPR)"
fi

# =============================================================================
# Detect whether a Proton account is already authenticated
# =============================================================================
LOGGED_IN=false
if [ -f "$VAULT" ]; then
    LOGGED_IN=true
fi

# =============================================================================
# Follow the Bridge log file and stream it to stdout (docker logs).
# Bridge creates one timestamped *_bri_*.log file per session in LOG_DIR.
#
# We wait for that file to appear using inotifywait -t (timeout) as a
# blocking sleep-with-notification — NOT as a pipeline source. Piping
# inotifywait into a while loop would run the loop body in a bash subshell,
# making variable assignments (tail_pid, current) invisible to the outer
# scope and breaking rotation handling. Using -t avoids the pipe entirely.
#
# Once the file exists, exec tail -F replaces this subshell process cleanly.
# tail -F (follow by name) handles in-place rotation; -n +1 prints from
# line 1 so no early boot messages are missed.
# =============================================================================
follow_bridge_logs() {
    mkdir -p "$LOG_DIR"

    local log_file=""

    echo ">>> Waiting for Bridge log file..."
    while [ -z "$log_file" ]; do
        log_file="$(find "$LOG_DIR" -maxdepth 1 -name '*_bri_*.log' \
            -printf '%T@\t%p\n' 2>/dev/null | sort -rn | head -n1 | cut -f2-)"
        if [ -z "$log_file" ]; then
            # Block up to 5 s waiting for a create/move event, then re-check.
            # The || true prevents set -e from exiting on inotifywait timeout.
            inotifywait -q -t 5 -e create -e moved_to "$LOG_DIR" >/dev/null 2>&1 || true
        fi
    done

    echo ">>> Streaming Bridge logs: $(basename "$log_file")"
    exec tail -n +1 -F "$log_file"
}

# =============================================================================
# Launch
# =============================================================================
if [ "$LOGGED_IN" = false ]; then
    cat <<'EOF'

┌──────────────────────────────────────────────────────────────┐
│  No Proton account found. Dropping to Bridge interactive CLI │
│                                                              │
│  Steps:                                                      │
│    login    → enter your Proton email, password, and 2FA     │
│    info     → copy bridge username + password into secrets   │
│    exit                                                      │
│                                                              │
│  Then: docker compose up -d                                  │
└──────────────────────────────────────────────────────────────┘

EOF

    exec bridge --cli
else
    echo ">>> Account found. Starting Bridge as user '$(whoami)'..."

    follow_bridge_logs &
    WATCHER_PID=$!

    bridge --noninteractive &
    BRIDGE_PID=$!

    wait "$BRIDGE_PID"
fi
