#!/bin/bash
set -Eeuo pipefail

readonly BRIDGE_HOST="${BRIDGE_HOST:-protonmail-bridge}"
readonly BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-1143}"
# The name Bridge's certificate is checked against, when it differs from
# BRIDGE_HOST (the macOS Bridge overlay sets 127.0.0.1). See
# render_mbsync_config.
readonly BRIDGE_CERT_HOST="${BRIDGE_CERT_HOST:-}"
# With BRIDGE_CERT_HOST, the SHA-256 fingerprint the operator took from
# the Bridge app; see verify_expected_fingerprint.
readonly BRIDGE_CERT_FINGERPRINT="${BRIDGE_CERT_FINGERPRINT:-}"
readonly SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
readonly RUNTIME_DIR="/tmp/mbsync"
readonly TEMPLATE_FILE="/etc/mbsyncrc.template"
readonly CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
readonly CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"
# Liveness heartbeat for healthcheck.sh, touched around every sync attempt
# whatever its outcome (see mark_sync_activity). Freshness is the separate
# success stamp below.
readonly SYNC_ACTIVITY_FILE="${RUNTIME_DIR}/last-sync-activity"
# Two numbers report_mbsync_errors leaves for run_sync once mbsync's
# stderr closes: far-side box lines withheld and other lines passed on.
readonly MBSYNC_ERROR_COUNTS_FILE="${RUNTIME_DIR}/mbsync-error-counts"
# How long run_sync waits for those counts after mbsync exits.
readonly MBSYNC_ERROR_COUNTS_WAIT_TENTHS=100
readonly BRIDGE_PASS_FILE="/run/secrets/bridge_pass"
# State directory persists the pinned Bridge cert fingerprint across
# container restarts. The directory is backed by a named volume so it
# survives `docker compose down` / rebuilds but is cleared by `make
# clean`, giving the operator a clean way to start over if needed.
readonly STATE_DIR="/state"
readonly PIN_FILE="${STATE_DIR}/bridge-cert.fingerprint"
readonly BRIDGE_WAIT_INTERVAL_SECONDS=2
readonly BRIDGE_WAIT_MAX_ATTEMPTS=300
# Per-probe connect timeout (nc -w). timeout(1) allows one second more
# and bounds the whole probe, name resolution included, so each attempt
# takes at most BRIDGE_PROBE_TIMEOUT_SECONDS + 1 seconds plus the interval.
readonly BRIDGE_PROBE_TIMEOUT_SECONDS=2
readonly CERT_EXTRACT_TIMEOUT_SECONDS=20
readonly MAX_CONSECUTIVE_SYNC_FAILURES=5
readonly BRIDGE_CERT_PIN_ROTATE="${BRIDGE_CERT_PIN_ROTATE:-false}"
readonly MAILDIR_PATH="/maildir"
# Last-sync stamp read by the indexer (indexer/src/maildir.py
# ``read_sync_stamp``) and reported by mcp-server's get_mailbox_status.
# It sits at the Maildir root, outside every cur/new folder, so the
# indexer never treats it as a message.
readonly SYNC_STAMP_FILE="${MAILDIR_PATH}/.mbsync-last-sync.json"

# Owner-only umask for the runtime tmp dir (config + cert material).
# mbsync itself ignores umask for Maildir writes — it explicitly passes
# mode 0600 to ``open()`` and can create subdirectories under this umask,
# so the post-sync chmod hook in ``relax_new_maildir_perms`` is what makes
# new Maildir paths traversable/readable to the indexer (a different UID).
umask 077
mkdir -p "$RUNTIME_DIR"

require_prerequisites() {
    if [[ -z "${BRIDGE_USER:-}" ]]; then
        echo ">>> ERROR: BRIDGE_USER is empty. Populate it from 'bridge --cli info' before starting mbsync." >&2
        exit 1
    fi

    if [[ ! -s "$BRIDGE_PASS_FILE" ]]; then
        echo ">>> ERROR: ${BRIDGE_PASS_FILE} is missing or empty. Refusing to start without the Bridge password secret." >&2
        exit 1
    fi

    if [[ ! "$SYNC_INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
        echo ">>> ERROR: SYNC_INTERVAL must be a positive integer number of seconds." >&2
        exit 1
    fi

    validate_bridge_endpoint || exit 1
}

validate_bridge_endpoint() {
    # These values are written into mbsyncrc and, with a tunnel, into the
    # shell command isync runs for it, so only a plain host name or IPv4
    # address and a decimal port are accepted.
    local host_re='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'

    if [[ ! "$BRIDGE_HOST" =~ $host_re ]]; then
        echo ">>> ERROR: BRIDGE_HOST must be a host name or IPv4 address." >&2
        return 1
    fi
    if [[ ! "$BRIDGE_IMAP_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || ((BRIDGE_IMAP_PORT > 65535)); then
        echo ">>> ERROR: BRIDGE_IMAP_PORT must be a port number from 1 to 65535." >&2
        return 1
    fi
    if [[ -n "$BRIDGE_CERT_HOST" && ! "$BRIDGE_CERT_HOST" =~ $host_re ]]; then
        echo ">>> ERROR: BRIDGE_CERT_HOST must be empty or a host name or IPv4 address." >&2
        return 1
    fi
    if [[ -n "$BRIDGE_CERT_FINGERPRINT" && ! "$(expected_fingerprint)" =~ ^[0-9a-f]{64}$ ]]; then
        echo ">>> ERROR: BRIDGE_CERT_FINGERPRINT must be a SHA-256 fingerprint: 64 hex digits, with or without colons." >&2
        return 1
    fi
}

expected_fingerprint() {
    # BRIDGE_CERT_FINGERPRINT in the pin's form: openssl's
    # "sha256 Fingerprint=AB:CD:..." line, its value, or bare hex.
    local fp="${BRIDGE_CERT_FINGERPRINT##*=}"
    fp="${fp//:/}"
    printf '%s' "$fp" | tr '[:upper:]' '[:lower:]'
}

verify_expected_fingerprint() {
    # With BRIDGE_CERT_HOST (the macOS Bridge app), mbsync connects to an
    # unprivileged port on the Mac's loopback, which another local account
    # can hold while the app is not running. Trust on first use there
    # would pin that account's certificate and send it the Bridge
    # password, so the certificate must match the fingerprint the operator
    # took from the app, on every start and before the pin is consulted
    # (a rotation accepts only that certificate too). The Bridge container
    # sits alone on bridge-net, so its mode keeps trust on first use.
    local current_fp="$1"

    if [[ -z "$BRIDGE_CERT_HOST" ]]; then
        return 0
    fi
    if [[ -z "$BRIDGE_CERT_FINGERPRINT" ]]; then
        echo ">>> ERROR: BRIDGE_CERT_FINGERPRINT is not set — refusing to trust the Bridge app's certificate on first use." >&2
        echo ">>>   presented: sha256:${current_fp}" >&2
        echo ">>> Take the fingerprint from the Bridge app on the Mac (docs/setup.md, macOS Bridge mode), set it in .env, and start again." >&2
        return 1
    fi
    if [[ "$(expected_fingerprint)" != "$current_fp" ]]; then
        echo ">>> ERROR: the Bridge certificate does not match BRIDGE_CERT_FINGERPRINT — refusing to sync." >&2
        echo ">>>   expected:  sha256:$(expected_fingerprint)" >&2
        echo ">>>   presented: sha256:${current_fp}" >&2
        echo ">>> If the app's certificate changed on purpose, update BRIDGE_CERT_FINGERPRINT. Otherwise this is a security event — something else may be listening on the app's port." >&2
        return 1
    fi
}

render_mbsync_config() {
    # isync checks Bridge's certificate against the name in Host. Without
    # BRIDGE_CERT_HOST that is BRIDGE_HOST, which mbsync connects to
    # directly: the Bridge container's certificate is issued for
    # protonmail-bridge.
    #
    # With BRIDGE_CERT_HOST (the macOS Bridge app, whose certificate is
    # issued for 127.0.0.1 only), Host is that name and the connection to
    # BRIDGE_HOST:BRIDGE_IMAP_PORT goes through a Tunnel, which isync opens
    # instead of a socket to Host. STARTTLS, the certificate check and the
    # pinned CertificateFile are unchanged: TLS runs end to end between
    # mbsync and Bridge, and socat only relays bytes. socat rather than nc,
    # because isync waits for the server's close after LOGOUT and nc does
    # not pass it on.
    local imap_host imap_connect

    if [[ -z "$BRIDGE_CERT_HOST" ]]; then
        imap_host="$BRIDGE_HOST"
        imap_connect="Port ${BRIDGE_IMAP_PORT}"
    else
        imap_host="$BRIDGE_CERT_HOST"
        imap_connect="Tunnel \"exec socat - TCP:${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}\""
    fi

    # Only the named variables are substituted. BRIDGE_PASS is never one:
    # mbsyncrc's PassCmd reads it from the Docker secret.
    # shellcheck disable=SC2016 # the list names variables for envsubst
    MBSYNC_IMAP_HOST="$imap_host" MBSYNC_IMAP_CONNECT="$imap_connect" \
        envsubst '${MBSYNC_IMAP_HOST} ${MBSYNC_IMAP_CONNECT} ${BRIDGE_USER}' \
        <"$TEMPLATE_FILE" >"$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE" # protect the file because it contains credentials
}

wait_for_bridge_imap() {
    local attempt
    local nc_err_file

    nc_err_file="$(mktemp "${RUNTIME_DIR}/nc-check.XXXXXX")"
    echo ">>> Waiting for ProtonBridge IMAP on ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
    for ((attempt = 1; attempt <= BRIDGE_WAIT_MAX_ATTEMPTS; attempt++)); do
        if timeout "$((BRIDGE_PROBE_TIMEOUT_SECONDS + 1))s" \
            nc -z -w "$BRIDGE_PROBE_TIMEOUT_SECONDS" "$BRIDGE_HOST" "$BRIDGE_IMAP_PORT" \
            2>"$nc_err_file"; then
            rm -f "$nc_err_file"
            echo ">>> Bridge IMAP port is reachable."
            return 0
        fi

        sleep "$BRIDGE_WAIT_INTERVAL_SECONDS"
    done

    echo ">>> ERROR: Bridge IMAP did not become reachable after ${BRIDGE_WAIT_MAX_ATTEMPTS} attempts (at most $((BRIDGE_WAIT_MAX_ATTEMPTS * (BRIDGE_PROBE_TIMEOUT_SECONDS + 1 + BRIDGE_WAIT_INTERVAL_SECONDS))) seconds)." >&2
    if [[ -s "$nc_err_file" ]]; then
        echo ">>> Last nc stderr follows:" >&2
        cat "$nc_err_file" >&2
    fi
    rm -f "$nc_err_file"
    return 1
}

cert_fingerprint() {
    local cert_path="$1"
    # SHA-256 over the DER-encoded cert — matches `openssl x509 -fingerprint
    # -sha256 -noout`. Emitted as a bare lowercase hex string without
    # colons so it's easy to compare and store.
    openssl x509 -in "$cert_path" -outform DER \
        | openssl dgst -sha256 \
        | awk '{print $NF}' \
        | tr '[:upper:]' '[:lower:]'
}

write_pin() {
    # Write the pin to a temporary file in the state directory and rename
    # it into place, so a failed write never leaves a partial pin and a
    # failed rotation keeps the previous one. Called only as a condition,
    # where errexit is off, so every step is checked.
    local fp="$1"
    local tmp

    if ! tmp="$(mktemp "${PIN_FILE}.XXXXXX")"; then
        return 1
    fi
    if ! printf '%s\n' "$fp" >"$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" "$PIN_FILE"; then
        rm -f "$tmp"
        return 1
    fi
}

verify_cert_pin() {
    # First boot: no pin on disk yet → TOFU, save fingerprint.
    # Subsequent boots: fingerprint must match, or the operator must opt
    # in to rotation via BRIDGE_CERT_PIN_ROTATE=true (used when Bridge is
    # upgraded and its TLS cert is deliberately replaced). Only an absent
    # pin is a first boot: one that exists but is empty, malformed,
    # unreadable or a dangling link is refused like a mismatch.
    #
    # The caller runs this as an ``if`` condition, which turns errexit off
    # inside it: every step that must succeed is checked explicitly, and
    # the cert is accepted only once the pin is stored.
    local current_fp="$1"
    local pinned_fp

    if [[ ! -e "$PIN_FILE" && ! -L "$PIN_FILE" ]]; then
        if ! write_pin "$current_fp"; then
            echo ">>> ERROR: could not save the Bridge cert pin to ${PIN_FILE} — refusing to sync." >&2
            return 1
        fi
        echo ">>> First boot — pinned Bridge cert fingerprint sha256:${current_fp}."
        return 0
    fi

    # A directory, FIFO or device in the pin's place is refused before it
    # is opened: reading a FIFO would block forever, and write_pin's mv
    # would move the new pin into a directory. Rotation does not repair
    # these; they are removed by hand.
    if [[ -e "$PIN_FILE" && ! -f "$PIN_FILE" ]]; then
        echo ">>> ERROR: the Bridge cert pin at ${PIN_FILE} is not a regular file — refusing to sync." >&2
        echo ">>> Remove it by hand, then recreate mbsync." >&2
        return 1
    fi

    if ! pinned_fp="$(tr -d '[:space:]' <"$PIN_FILE")"; then
        # Rotation replaces an unreadable file or a dangling link: write_pin
        # renames over the path itself.
        if [[ "$BRIDGE_CERT_PIN_ROTATE" != "true" ]]; then
            echo ">>> ERROR: could not read the Bridge cert pin at ${PIN_FILE} — refusing to sync." >&2
            return 1
        fi
        pinned_fp="(unreadable)"
    fi
    if [[ "$pinned_fp" == "$current_fp" ]]; then
        echo ">>> Bridge cert fingerprint matches the pinned value."
        return 0
    fi

    # write_pin stores 64 lowercase hex digits; anything else is damaged
    # state, not a fingerprint to compare against.
    if [[ ! "$pinned_fp" =~ ^[0-9a-f]{64}$ && "$BRIDGE_CERT_PIN_ROTATE" != "true" ]]; then
        echo ">>> ERROR: the Bridge cert pin at ${PIN_FILE} is empty or malformed — refusing to sync." >&2
        echo ">>> To re-pin the cert Bridge presents now, recreate mbsync once with BRIDGE_CERT_PIN_ROTATE=true." >&2
        return 1
    fi

    if [[ "$BRIDGE_CERT_PIN_ROTATE" == "true" ]]; then
        echo ">>> WARNING: Bridge cert fingerprint changed and BRIDGE_CERT_PIN_ROTATE=true — rotating pin." >&2
        echo ">>>   pinned:  sha256:${pinned_fp}" >&2
        echo ">>>   current: sha256:${current_fp}" >&2
        if ! write_pin "$current_fp"; then
            echo ">>> ERROR: could not save the rotated pin to ${PIN_FILE}; the previous pin is kept — refusing to sync." >&2
            return 1
        fi
        return 0
    fi

    echo ">>> ERROR: Bridge cert fingerprint does not match pinned value — refusing to sync." >&2
    echo ">>>   pinned:  sha256:${pinned_fp}" >&2
    echo ">>>   current: sha256:${current_fp}" >&2
    echo ">>> If this rotation is expected (e.g. Bridge upgrade), recreate mbsync once with BRIDGE_CERT_PIN_ROTATE=true." >&2
    echo ">>> Otherwise this is a security event — investigate before proceeding." >&2
    return 1
}

extract_bridge_cert() {
    local cert_tmp
    local openssl_err_file
    local current_fp

    cert_tmp="$(mktemp "${RUNTIME_DIR}/bridge-cert.XXXXXX")"
    openssl_err_file="$(mktemp "${RUNTIME_DIR}/openssl-s_client.XXXXXX")"

    echo ">>> Extracting Bridge TLS cert from ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
    if ! timeout "${CERT_EXTRACT_TIMEOUT_SECONDS}s" \
        openssl s_client \
            -connect "${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}" \
            -starttls imap \
            < /dev/null \
            2>"$openssl_err_file" \
        | openssl x509 > "$cert_tmp"; then
        echo ">>> ERROR: cert extraction failed — refusing to sync without cert pinning." >&2
        if [[ -s "$openssl_err_file" ]]; then
            echo ">>> openssl s_client stderr follows:" >&2
            cat "$openssl_err_file" >&2
        fi
        rm -f "$cert_tmp" "$openssl_err_file"
        return 1
    fi

    if ! current_fp="$(cert_fingerprint "$cert_tmp")" || [[ -z "$current_fp" ]]; then
        echo ">>> ERROR: failed to compute fingerprint for extracted cert." >&2
        rm -f "$cert_tmp" "$openssl_err_file"
        return 1
    fi

    if ! verify_expected_fingerprint "$current_fp" || ! verify_cert_pin "$current_fp"; then
        rm -f "$cert_tmp" "$openssl_err_file"
        return 1
    fi

    mv "$cert_tmp" "$CERT_FILE"
    chmod 600 "$CERT_FILE"
    rm -f "$openssl_err_file"
    echo ">>> Bridge cert extracted successfully."
    return 0
}

relax_new_maildir_perms() {
    # mbsync calls ``open(O_CREAT, 0600)`` for every new message and
    # ignores umask, so newly delivered files are owner-only by default
    # and unreadable to the indexer (a different UID). Subdirectories
    # created while the service umask is 077 are also not traversable by
    # the indexer. Re-apply directory execute/read and file read bits after
    # each sync so the indexer reads via "other" permission.
    #
    # Runs inside ``run_sync``, an ``if`` condition where errexit is off,
    # so each step's failure is returned explicitly.
    find "$MAILDIR_PATH" -type d \! -perm -005 -exec chmod go+rx {} + || return 1
    find "$MAILDIR_PATH" -type f \! -perm -044 -exec chmod go+r {} + || return 1
}

# PID of the child run_child is waiting on, if any.
child_pid=""

run_child() {
    # Run a command in the background and wait for it, so a stop signal
    # interrupts the wait and stop_on_signal can pass it on. Returns the
    # command's status.
    local rc=0
    "$@" &
    child_pid=$!
    wait "$child_pid" || rc=$?
    child_pid=""
    return "$rc"
}

stop_on_signal() {
    # Tini signals only this shell. Pass the stop to the active child as
    # TERM (background commands ignore INT), wait for it to end, then exit
    # with the conventional 128 + signal number status.
    local signal="$1" status="$2"
    if [[ -n "${child_pid:-}" ]]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" || true
    fi
    echo ">>> Received SIG${signal} — stopping." >&2
    exit "$status"
}

install_signal_handlers() {
    trap 'stop_on_signal TERM 143' TERM
    trap 'stop_on_signal INT 130' INT
}

mark_sync_activity() {
    # Liveness, not success: healthcheck.sh treats the loop as alive while
    # this is fresh or an mbsync is running, so a first sync that runs for
    # hours stays healthy without a success stamp (#277).
    touch "$SYNC_ACTIVITY_FILE"
}

report_mbsync_errors() {
    # Filters mbsync's stderr as it arrives, passing each line on to ours
    # at once, except a line saying a far-side box cannot be opened: it
    # names a Proton folder, which is mailbox content, so it is withheld
    # and counted instead. Only the counts are kept, in memory; at end of
    # input it writes "<withheld> <passed on>" to MBSYNC_ERROR_COUNTS_FILE.
    #
    # isync 1.4.4 (Debian bookworm) writes exactly this line to stderr
    # when Bridge refuses to open a box, then syncs the remaining boxes
    # and exits 1. That is what a folder renamed or deleted in Proton
    # after it synced produces on every run, since Create Near and
    # Expunge None keep its local copy (#276). An INBOX line is passed on
    # and counted as another error: INBOX cannot be renamed or deleted,
    # so Bridge refusing it means Bridge is refusing boxes. One pass,
    # linear in the output.
    awk -v far_box='^Error: channel protonmail: far side box .+ cannot be opened[.]$' \
        -v inbox='Error: channel protonmail: far side box INBOX cannot be opened.' \
        -v counts="$MBSYNC_ERROR_COUNTS_FILE" '
        $0 ~ far_box && $0 != inbox { withheld++; next }
        { other++; print > "/dev/stderr"; fflush("/dev/stderr") }
        END { print withheld + 0, other + 0 > counts; close(counts) }
    '
}

read_mbsync_error_counts() {
    # mbsync's stderr closes when it exits, but the filter may still be
    # finishing. Waits a bounded time for its counts and prints them;
    # fails if they never arrive, so nothing is tolerated.
    local i
    for ((i = 0; i < MBSYNC_ERROR_COUNTS_WAIT_TENTHS; i++)); do
        if [[ -s "$MBSYNC_ERROR_COUNTS_FILE" ]]; then
            cat "$MBSYNC_ERROR_COUNTS_FILE"
            return 0
        fi
        sleep 0.1
    done
    echo ">>> WARNING: mbsync's error filter did not finish; not classifying this sync's errors." >&2
    return 1
}

run_sync() {
    # Fails when mbsync or the permission repair fails: a sync whose mail
    # the indexer cannot read must not be recorded as successful. The
    # repair runs even after a failed mbsync, for what it did deliver.
    #
    # One failure is tolerated: when mbsync's only errors are far-side
    # folders it could not open (see report_mbsync_errors), the rest of
    # the mailbox synced and those folders have nothing left to pull, so
    # the run counts as a success, with a warning. Its success stamp is
    # then written as usual: the local Maildir is as current as Proton
    # allows, and withholding the stamp would report a stale mailbox for
    # as long as the local copy is kept.
    #
    # Activity is marked before mbsync, again once it ends (the repair
    # walks the whole Maildir with no mbsync running, so the heartbeat
    # must be fresh for it), and after the attempt, whatever its outcome.
    local rc=0 counts="" withheld=0 other=0
    rm -f "$MBSYNC_ERROR_COUNTS_FILE"
    mark_sync_activity
    # stderr goes through the filter as it is written; run_child still
    # waits on (and signals) mbsync itself.
    run_child mbsync -c "$CONFIG_FILE" -a 2> >(report_mbsync_errors) || rc=$?
    mark_sync_activity
    # Missing counts leave withheld at 0, so nothing is tolerated.
    if counts="$(read_mbsync_error_counts)"; then
        read -r withheld other <<<"$counts"
    fi
    rm -f "$MBSYNC_ERROR_COUNTS_FILE"
    if ((withheld > 0)); then
        echo ">>> WARNING: ${withheld} far-side folder(s) could not be opened, most likely renamed or deleted in Proton; their local copies are kept. Folder names are not logged (see docs/troubleshooting.md)." >&2
        if ((rc == 1 && other == 0)); then
            echo ">>> mbsync reported no other error — counting this sync as successful." >&2
            rc=0
        fi
    fi
    if ! relax_new_maildir_perms; then
        echo ">>> ERROR: could not make new Maildir entries readable to the indexer." >&2
        rc=1
    fi
    mark_sync_activity
    return "$rc"
}

record_successful_sync() {
    # Written only after relax_new_maildir_perms, so every message this
    # sync delivered is already on disk and readable. The temporary name
    # carries this sync's time and interval: the indexer acknowledges the
    # sync named by the rename event it sees, which its watcher handles
    # only after this sync's delivery events. Replaced atomically via
    # rename; world-readable because the indexer runs as a different UID.
    local completed_at tmp
    completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    tmp="${MAILDIR_PATH}/.mbsync-last-sync.${completed_at}.${SYNC_INTERVAL}.tmp"
    printf '{"completed_at": "%s", "sync_interval_secs": %d}\n' \
        "$completed_at" "$SYNC_INTERVAL" >"$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$SYNC_STAMP_FILE"
}

# =============================================================================
# Generate mbsync config from template (render_mbsync_config): the Bridge
# endpoint from BRIDGE_HOST, BRIDGE_IMAP_PORT and BRIDGE_CERT_HOST, and
# BRIDGE_USER, all set in docker-compose.yml / .env.
# BRIDGE_PASS is NOT passed as an env var — mbsyncrc uses PassCmd to read
# it directly from the Docker secret at /run/secrets/bridge_pass.
# =============================================================================
install_signal_handlers
require_prerequisites

# BRIDGE_CERT_PIN_ROTATE is an opt-in for accepting one legitimate
# Bridge cert rotation. It is part of the container's environment, so
# it stays true through every restart of this container (manual or by
# the restart policy) until the container is recreated with it false;
# until then every new cert is accepted without comparison. Surface
# that on every boot so the operator notices if they forgot.
if [[ "$BRIDGE_CERT_PIN_ROTATE" == "true" ]]; then
    echo ">>> WARNING: BRIDGE_CERT_PIN_ROTATE=true — any Bridge cert fingerprint change this boot will be accepted without comparison." >&2
    echo ">>> This is intended only for one boot after a deliberate Bridge cert rotation (e.g. Bridge upgrade); a restart keeps it enabled." >&2
    echo ">>> Once the rotation succeeds, recreate mbsync with BRIDGE_CERT_PIN_ROTATE=false to re-enable pin enforcement (see docs/troubleshooting.md)." >&2
fi

render_mbsync_config

# =============================================================================
# Wait for ProtonBridge IMAP to be available
# Bridge takes time to start and complete its internal Gluon sync before
# it will accept IMAP connections. Probe with a bounded connect, retry
# every 2 seconds, then fail so Docker restart policy makes the problem
# visible instead of hanging forever.
# =============================================================================
wait_for_bridge_imap

# =============================================================================
# Extract Bridge TLS certificate and verify the cert pin
# openssl s_client fetches the cert from the live IMAP connection without
# needing to verify it first. On first boot the SHA-256 fingerprint is
# saved to the persistent state volume ($PIN_FILE). On subsequent boots
# the fingerprint must match the pinned value — otherwise mbsync refuses
# to sync. A legitimate rotation (e.g. Bridge upgrade) is accepted by
# recreating mbsync once with BRIDGE_CERT_PIN_ROTATE=true. `make clean`
# deletes the state volume and resets the pin.
# =============================================================================
extract_bridge_cert

# =============================================================================
# Initial sync
# Runs once on startup to catch up on any messages that arrived while
# the container was down. A failed attempt is logged and counted so repeated
# failures eventually exit and let Docker restart the container.
# =============================================================================
consecutive_sync_failures=0
echo ">>> Running initial sync..."
if run_sync; then
    record_successful_sync
else
    consecutive_sync_failures=1
    echo ">>> Initial sync returned a non-zero status (${consecutive_sync_failures}/${MAX_CONSECUTIVE_SYNC_FAILURES})." >&2
fi

# =============================================================================
# Continuous sync loop
# Polls Bridge IMAP every SYNC_INTERVAL seconds for new messages.
# Default interval is 60 seconds — set SYNC_INTERVAL in .env to change.
# =============================================================================
echo ">>> Starting sync loop (interval: ${SYNC_INTERVAL}s)..."
while true; do
    # Through run_child so a stop during the interval exits at once.
    run_child sleep "$SYNC_INTERVAL"
    echo ">>> Syncing..."
    if run_sync; then
        consecutive_sync_failures=0
        record_successful_sync
        continue
    fi

    ((consecutive_sync_failures += 1))
    echo ">>> Sync failed (${consecutive_sync_failures}/${MAX_CONSECUTIVE_SYNC_FAILURES} consecutive failures)." >&2
    if ((consecutive_sync_failures >= MAX_CONSECUTIVE_SYNC_FAILURES)); then
        echo ">>> ERROR: mbsync exceeded ${MAX_CONSECUTIVE_SYNC_FAILURES} consecutive failures — exiting for container restart." >&2
        exit 1
    fi

    echo ">>> Bridge may still be busy — will retry next interval." >&2
done
