#!/bin/bash
set -Eeuo pipefail

# The Bridge app runs on the host; mbsync connects to it here.
readonly BRIDGE_HOST="${BRIDGE_HOST:-host.docker.internal}"
readonly BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-1143}"
# The name the app's certificate is checked against (its CN). See
# render_mbsync_config.
readonly BRIDGE_CERT_HOST="${BRIDGE_CERT_HOST:-127.0.0.1}"
# Required: the SHA-256 fingerprint the operator took from the Bridge
# app; see verify_expected_fingerprint.
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
# Written by report_mbsync_notices once mbsync's stdout closes, so
# run_sync can wait for the last of it.
readonly MBSYNC_NOTICES_DONE_FILE="${RUNTIME_DIR}/mbsync-notices-done"
# How long run_sync waits for each filter after mbsync exits.
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
# Per-run deadline for mbsync (#282). isync's own socket timeout restarts
# on every byte the server sends, so a server that keeps a command open
# with keepalives would hold one run forever. Past the deadline run_sync
# stops mbsync (TERM, then KILL after the grace) and counts a failed
# sync. The default is a day: a first sync of a large mailbox must never
# hit it, and a run stopped anyway resumes from isync's sync state rather
# than starting over. Tune it once the first sync's duration is known
# (docs/troubleshooting.md). healthcheck.sh repeats both values.
readonly SYNC_DEADLINE_SECONDS="${SYNC_DEADLINE_SECONDS:-86400}"
readonly SYNC_KILL_GRACE_SECONDS=30
readonly BRIDGE_CERT_PIN_ROTATE="${BRIDGE_CERT_PIN_ROTATE:-false}"
readonly MAILDIR_PATH="/maildir"
# Last-sync stamp read by the indexer (indexer/src/maildir.py
# ``read_sync_stamp``) and reported by mcp-server's get_mailbox_status.
# It sits at the Maildir root, outside every cur/new folder, so the
# indexer never treats it as a message.
readonly SYNC_STAMP_FILE="${MAILDIR_PATH}/.mbsync-last-sync.json"
# Renamed into place after every permission repair, whatever the sync's
# outcome (signal_perms_repaired): the indexer then re-watches folders the
# repair opened (#524). Read by indexer/src/maildir.py
# ``PERMS_REPAIRED_NAME``; not a sign of a successful sync.
readonly PERMS_REPAIRED_FILE="${MAILDIR_PATH}/.mbsync-perms-repaired"

# Owner-only umask for the runtime tmp dir (config + cert material).
# mbsync itself ignores umask for Maildir writes — it explicitly passes
# mode 0600 to ``open()`` and can create subdirectories under this umask,
# so the post-sync chmod hook in ``relax_new_maildir_perms`` is what makes
# new Maildir paths traversable/readable to the indexer (a different UID).
umask 077
mkdir -p "$RUNTIME_DIR"

require_prerequisites() {
    if [[ -z "${BRIDGE_USER:-}" ]]; then
        echo ">>> ERROR: BRIDGE_USER is empty. Populate it from the Bridge app's IMAP details before starting mbsync." >&2
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

    # At most nine digits, so the healthcheck's arithmetic cannot overflow.
    if [[ ! "$SYNC_DEADLINE_SECONDS" =~ ^[1-9][0-9]{0,8}$ ]]; then
        echo ">>> ERROR: SYNC_DEADLINE_SECONDS must be a positive integer number of seconds (at most 999999999)." >&2
        exit 1
    fi

    validate_bridge_endpoint || exit 1
}

log_startup_identity() {
    # One line naming what is running (#887): the source commit baked into
    # the image (GIT_COMMIT; anything but a plain token is "unknown"), a
    # random ID for this start, and the first 12 hex digits of a SHA-256
    # over the non-secret settings named below: the endpoint, the sync
    # timing, the expected certificate fingerprint (not secret; in the
    # form verify_expected_fingerprint compares) and whether pin rotation
    # is on. BRIDGE_USER and the password are not inputs. It runs before
    # validation: values are hashed as configured, never parsed or printed,
    # so nothing here can fail on a malformed setting. An endpoint value
    # with an "@" (userinfo, which validation would refuse) is hashed as a
    # marker, so the hash cannot be used to test guesses at a password.
    local commit="${GIT_COMMIT:-unknown}" boot config rotate="false"
    local host="$BRIDGE_HOST" port="$BRIDGE_IMAP_PORT" cert_host="$BRIDGE_CERT_HOST"
    if [[ ! "$commit" =~ ^[0-9A-Za-z._-]{1,64}$ ]]; then
        commit="unknown"
    fi
    if [[ "$BRIDGE_CERT_PIN_ROTATE" == "true" ]]; then
        rotate="true"
    fi
    [[ "$host" != *@* ]] || host="<value-with-credentials>"
    [[ "$port" != *@* ]] || port="<value-with-credentials>"
    [[ "$cert_host" != *@* ]] || cert_host="<value-with-credentials>"
    boot="$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
    config="$(printf 'BRIDGE_HOST=%s\nBRIDGE_IMAP_PORT=%s\nBRIDGE_CERT_HOST=%s\nSYNC_INTERVAL=%s\nSYNC_DEADLINE_SECONDS=%s\nBRIDGE_CERT_FINGERPRINT=%s\nBRIDGE_CERT_PIN_ROTATE=%s\n' \
        "$host" "$port" "$cert_host" "$SYNC_INTERVAL" "$SYNC_DEADLINE_SECONDS" \
        "$(expected_fingerprint)" "$rotate" | sha256sum)"
    printf '>>> Startup identity: service=mbsync commit=%s boot=%s config=%s\n' \
        "$commit" "$boot" "${config:0:12}"
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
    if [[ ! "$BRIDGE_CERT_HOST" =~ $host_re ]]; then
        echo ">>> ERROR: BRIDGE_CERT_HOST must be a host name or IPv4 address." >&2
        return 1
    fi
    # The expected fingerprint is required (verify_expected_fingerprint).
    # Say so now rather than after the wait for Bridge, which can run for
    # the full bounded wait when the app is not running (#584).
    if [[ -z "$BRIDGE_CERT_FINGERPRINT" ]]; then
        echo ">>> ERROR: BRIDGE_CERT_FINGERPRINT is not set — refusing to trust the Bridge app's certificate on first use." >&2
        echo ">>> Take the fingerprint from the Bridge app on the host (docs/setup.md, step 4.3), set it in .env, and start again." >&2
        return 1
    fi
    if [[ ! "$(expected_fingerprint)" =~ ^[0-9a-f]{64}$ ]]; then
        echo ">>> ERROR: BRIDGE_CERT_FINGERPRINT must be a SHA-256 fingerprint: 64 hex digits, with or without colons." >&2
        return 1
    fi
}

check_maildir_layout() {
    # mbsyncrc keeps each folder's sync state in the folder's own directory
    # (SyncState *, #275) and writes a child folder as its parent's
    # directory plus "/." and its name (SubFolders Legacy, #281). A Maildir
    # synced with the earlier layout has its state files at the root and
    # its child folders without the dot. isync would read neither: it would
    # download every folder again, the nested ones into new directories,
    # next to the copies already there. Refuse to sync until the operator
    # starts the Maildir over. A top-level folder's directory holds only
    # cur, new, tmp and dot entries in this layout, so any other directory
    # there is the earlier one. The messages name no path, because these
    # paths hold folder names: find's own diagnostics (a directory it
    # cannot read, by path) are kept in a file and only counted.
    local state nested find_err lines

    find_err="$(mktemp "${RUNTIME_DIR}/layout-find.XXXXXX")"
    if ! state="$(find "$MAILDIR_PATH" -mindepth 1 -maxdepth 1 -name '.mbsyncstate*' -print -quit \
        2>"$find_err")" \
        || ! nested="$(find "$MAILDIR_PATH" -mindepth 2 -maxdepth 2 -type d \
            ! -path "${MAILDIR_PATH}/.*" ! -name '.*' ! -name cur ! -name new ! -name tmp \
            -print -quit 2>>"$find_err")"; then
        lines="$(wc -l <"$find_err" | tr -d '[:space:]')"
        rm -f "$find_err"
        echo ">>> ERROR: could not inspect ${MAILDIR_PATH} for an earlier Maildir layout — refusing to sync." >&2
        echo ">>> find reported ${lines} error line(s), not logged because they name folders. To see them: docker exec mbsync find ${MAILDIR_PATH} -maxdepth 2 -type d" >&2
        return 1
    fi
    rm -f "$find_err"
    if [[ -n "$state" || -n "$nested" ]]; then
        echo ">>> ERROR: ${MAILDIR_PATH} was synced with an earlier mbsync layout (sync state at the Maildir root, or subfolders without the leading dot) — refusing to sync." >&2
        echo ">>> Syncing it with this version would download mail again next to the existing copies. Start the Maildir over: see docs/troubleshooting.md, \"mbsync refuses an earlier Maildir layout\"." >&2
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
    # mbsync connects to an unprivileged port on the host's loopback,
    # which another local account can hold while the Bridge app is not
    # running. Trust on first use there would pin that account's
    # certificate and send it the Bridge password, so the certificate must
    # match the fingerprint the operator took from the app, on every start
    # and before the pin is consulted (a rotation accepts only that
    # certificate too).
    local current_fp="$1"

    if [[ -z "$BRIDGE_CERT_FINGERPRINT" ]]; then
        echo ">>> ERROR: BRIDGE_CERT_FINGERPRINT is not set — refusing to trust the Bridge app's certificate on first use." >&2
        echo ">>>   presented: sha256:${current_fp}" >&2
        echo ">>> Take the fingerprint from the Bridge app on the host (docs/setup.md, step 4.3), set it in .env, and start again." >&2
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
    # isync checks Bridge's certificate against the name in Host. The
    # Bridge app's certificate is issued for 127.0.0.1 only, so Host is
    # BRIDGE_CERT_HOST and the connection to BRIDGE_HOST:BRIDGE_IMAP_PORT
    # goes through a Tunnel, which isync opens instead of a socket to Host.
    # Implicit TLS, the certificate check and the pinned CertificateFile
    # apply as for a direct connection: TLS runs end to end between mbsync
    # and Bridge, and socat only relays bytes. socat rather than nc,
    # because isync waits for the server's close after LOGOUT and nc does
    # not pass it on.
    local imap_host="$BRIDGE_CERT_HOST"
    local imap_connect="Tunnel \"exec socat - TCP:${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}\""

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
    # Runs after verify_expected_fingerprint has matched the cert.
    # First boot: no pin on disk yet → save fingerprint.
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

    # Implicit TLS, like mbsyncrc's TLSType IMAPS (#638): the handshake is
    # the first thing on the connection. A Bridge serving STARTTLS or
    # plaintext greets in plaintext instead, the handshake fails, and so
    # does the extraction. There is no second attempt without TLS.
    echo ">>> Extracting Bridge TLS cert from ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
    if ! timeout "${CERT_EXTRACT_TIMEOUT_SECONDS}s" \
        openssl s_client \
            -connect "${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}" \
            < /dev/null \
            2>"$openssl_err_file" \
        | openssl x509 > "$cert_tmp"; then
        echo ">>> ERROR: cert extraction failed — refusing to sync without cert pinning." >&2
        echo ">>> mbsync connects with implicit TLS only. If Bridge is not serving implicit TLS on ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT} (it serves STARTTLS or plaintext), the handshake fails here: set the Bridge app's IMAP connection mode to SSL. See docs/troubleshooting.md." >&2
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
    #
    # find and chmod name the paths they fail on, and these paths hold
    # folder names, so their diagnostics are kept in a file and only
    # counted, as in check_maildir_layout (#879).
    local find_err lines

    find_err="$(mktemp "${RUNTIME_DIR}/perms-find.XXXXXX")" || return 1
    if ! find "$MAILDIR_PATH" -type d \! -perm -005 -exec chmod go+rx {} + 2>"$find_err" \
        || ! find "$MAILDIR_PATH" -type f \! -perm -044 -exec chmod go+r {} + 2>>"$find_err"; then
        lines="$(wc -l <"$find_err" | tr -d '[:space:]')"
        rm -f "$find_err"
        echo ">>> find/chmod reported ${lines} error line(s), not logged because they name folders. To see them: docker exec mbsync find ${MAILDIR_PATH} -type d ! -perm -005 -o -type f ! -perm -044" >&2
        return 1
    fi
    rm -f "$find_err"
}

signal_perms_repaired() {
    # Tells the indexer a repair pass has finished, so it re-watches the
    # folders the pass opened (#524): a failed sync writes no success
    # stamp to do it. An empty file renamed into place, so the indexer
    # sees one rename event.
    local tmp="${PERMS_REPAIRED_FILE}.tmp"
    : >"$tmp" && mv -f "$tmp" "$PERMS_REPAIRED_FILE"
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

filter_mbsync_output() {
    # Filters one of mbsync's streams ($1: err or out) as it arrives,
    # passing each line on to ours at once with any Proton folder name in
    # it replaced: folder names are mailbox content (#570). Only counts
    # are kept, in memory; at end of input it writes "<withheld> <passed
    # on>" to the file $2.
    #
    # Redaction is by message shape, from isync 1.5.1's source (Debian
    # trixie, the image's version) and the 1.4.4 shapes it reworded,
    # which are kept: each rule is an anchored prefix and suffix of fixed text
    # around the name, which is replaced whole by <folder> (a box name)
    # or <path> (a Maildir or sync state path, which holds the folder
    # name). Text the IMAP server chose (an alert, an error reply), which
    # may name a folder too, becomes "(server text withheld)". A
    # sys_error path keeps its ": <strerror>" tail, which holds
    # no colon. A box named exactly INBOX, a fixed IMAP name, is kept. A
    # line matching no rule, or whose kept tail would still hold a path
    # under the Maildir, is cut at the first such path. Any other line is
    # passed on unchanged. A fixed set of anchored matches per line, each
    # linear in the line's length.
    #
    # On stderr, a line saying a far-side box other than INBOX cannot be
    # opened (1.5.1 adds "anymore" when the box synced before) is
    # withheld and counted instead. isync writes it when Bridge
    # refuses to open a box, then syncs the remaining boxes and exits 1:
    # what a folder renamed or deleted in Proton after it synced produces
    # on every run, since Create Near and Expunge None keep its local
    # copy (#276). An INBOX line is passed on and counted as another
    # error: INBOX cannot be renamed or deleted, so Bridge refusing it
    # means Bridge is refusing boxes.
    #
    # The image's awk is mawk, which reads a pipe in blocks and would hold
    # lines back until mbsync exits; -W interactive makes it read lines.
    local awk_cmd=(awk)
    if command -v mawk >/dev/null; then
        awk_cmd=(mawk -W interactive)
    fi
    # shellcheck disable=SC2016 # an awk program: its $ are awk's
    "${awk_cmd[@]}" -v stream="$1" -v counts="$2" -v maildir="${MAILDIR_PATH}/" \
        -v far_box='^Error: channel protonmail: far side box .+ cannot be opened( anymore)?[.]$' \
        -v inbox='^Error: channel protonmail: far side box INBOX cannot be opened( anymore)?[.]$' '
        function rule(prefix, suffix, replacement, keep_inbox) {
            n++
            pre[n] = prefix; suf[n] = suffix; rep[n] = replacement; inbox_ok[n] = keep_inbox
        }
        function redact(line,    i, head, rest, name, tail, at) {
            for (i = 1; i <= n; i++) {
                if (!match(line, pre[i]))
                    continue
                head = substr(line, 1, RLENGTH)
                rest = substr(line, RLENGTH + 1)
                if (suf[i] == "") {
                    name = rest; tail = ""
                } else if (match(rest, suf[i])) {
                    name = substr(rest, 1, RSTART - 1); tail = substr(rest, RSTART)
                } else
                    continue
                if (name == "" || index(tail, maildir))
                    continue
                if (inbox_ok[i] && name == "INBOX")
                    return line
                return head rep[i] tail
            }
            at = index(line, maildir)
            if (at > 0)
                return substr(line, 1, at - 1) "<path>"
            return line
        }
        BEGIN {
            q = "\047"; F = "<folder>"; P = "<path>"
            ch = "^Error: channel protonmail"
            cut = q " (rest of line withheld)"
            srv = "(server text withheld)"
            sys = ": [^:]*$"
            # Box names: src/sync.c, main.c (1.5.1: main_sync.c),
            # drv_imap.c, drv_maildir.c.
            rule(ch ": (far|near) side box ", " cannot be opened( anymore)?[.]$", F, 1)
            rule(ch ": both far side ", " cannot be opened[.]$", F " and near side " F, 0)
            rule("^Warning: channel protonmail: far side box ", " is not empty[.]$",
                 F " cannot be opened anymore, and near side box " F, 0)
            rule("^Warning: channel protonmail: near side box ", " is not empty[.]$",
                 F " cannot be opened anymore, and far side box " F, 0)
            rule(ch ": UIDVALIDITY of both far side ", " changed[.]$", F " and near side " F, 0)
            rule(ch ", (far|near) side box ",
                 ": UIDVALIDITY genuinely changed [(]at UID [0-9]+[)][.]$", F, 1)
            rule(ch ", (far|near) side box ", ": Unable to recover from UIDVALIDITY change[.]$", F, 1)
            rule(ch ", (far|near) side box ",
                 " [(]at UID [0-9]+[)]: UIDVALIDITY genuinely changed[.]$", F, 1)
            rule(ch ", (far|near) side box ",
                 " [(]at UID [0-9]+[)]: Unable to recover from both-sided UIDVALIDITY change, as it is genuine on at least one side[.]$",
                 F, 1)
            rule("^Notice: channel protonmail, (far|near) side box ",
                 ": Recovered from change of UIDVALIDITY[.]$", F, 1)
            rule("^(Opening|Creating|Deleting) (far|near) side box ", "[.][.][.]$", F, 1)
            rule("^Error: channel :protonmail-remote:", " is locked$", "<folders>", 0)
            rule("^Error: canonical mailbox name " q, q " contains flattened hierarchy delimiter$", F, 0)
            rule("^Error: flattened mailbox name " q, q " contains canonical hierarchy delimiter$", F, 0)
            rule("^IMAP warning: ignoring unreasonably long mailbox name " q, "[[][.][.][.]." q "$", F, 0)
            rule("^IMAP warning: ignoring mailbox " q, q " due to empty name component$", F, 0)
            rule("^IMAP warning: ignoring mailbox " q, q " due to " q "[.]" q " component$", F, 0)
            rule("^IMAP warning: ignoring mailbox ", " [(]reserved character " q "/" q " in name[)]$", F, 0)
            rule("^IMAP error: LIST" q "d mailbox name " q,
                 q " contains " q "[.][.]" q " component - THIS MIGHT BE AN ATTEMPT TO HACK YOU!$", F, 0)
            rule("^IMAP error: mailbox name ", " contains server" q "s hierarchy delimiter$", F, 0)
            rule("^IMAP error: invalid modified-UTF-7 string " q, q "[.]$", F, 0)
            rule("^IMAP error: invalid UTF-8 string " q, q "$", F, 0)
            # 1.4.4 wrote no newline after this one, so the rest of the
            # line is cut.
            rule("^IMAP error: cannot use unqualified " q, "", F cut, 0)
            # The command quotes the box; the server reply may repeat it.
            rule("^IMAP command " q "(SELECT|CREATE|DELETE|APPEND|UID COPY [0-9]+|UID MOVE [0-9]+) ", "", F cut, 0)
            rule("^Maildir error: accessing subfolder " q,
                 q ", but store " q "[^" q "]*" q " does not specify SubFolders style$", F, 0)
            rule("^Maildir error: store " q "[^" q "]*" q ", folder " q,
                 q ": SubFolders style Maildir[+][+] does not support dots in mailbox names$", F, 0)
            rule("^Maildir error: found subfolder " q,
                 q ", but store " q "[^" q "]*" q " does not specify SubFolders style$", F, 0)
            # Paths (1.5.1 drv_maildir.c and sync_state.c). Before the
            # generic "cannot <verb>" rules below, whose strerror match
            # would end inside a path holding ": ".
            rule("^Maildir error: empty mailbox name under ",
                 " - did you forget the trailing slash[?]$", P, 0)
            rule("^Maildir error: cannot write UIDVALIDITY in ", "", P, 0)
            rule("^Maildir error: cannot (fcntl lock|read) UIDVALIDITY in ", "[.]$", P, 0)
            rule("^Maildir error: cannot fstat UID database in ", sys, P, 0)
            rule("^Maildir notice: no UIDVALIDITY in ", ", creating new[.]$", P, 0)
            rule("^Maildir error: duplicate UID [0-9]+ in ", "[.]$", P, 0)
            rule("^Maildir notice: duplicate UID in ", "; changing UIDVALIDITY[.]$", P, 0)
            rule("^Maildir error: UID [0-9]+ is beyond highest assigned UID [0-9]+ in ", "[.]$", P, 0)
            rule("^Maildir error: malformed X-TUID header in ", "", P, 0)
            rule("^Error: state file ", " does not match ExpireSide setting$", P, 0)
            # sys_error lines end in ": <strerror>".
            rule("^Error: cannot create SyncState directory " q, q sys, P, 0)
            rule("^Maildir (error|warning): cannot remove " q, q sys, P, 0)
            rule("^Maildir error: cannot (access|create) mailbox " q, q sys, P, 0)
            rule("^Maildir error: cannot (rename|move) ", sys, P " to " P, 0)
            rule("^Maildir error: cannot write ", "[.] Disk full[?]$", P, 0)
            rule("^Error: cannot (create new sync state|create journal|create lock file|read sync state|read journal) ",
                 sys, P, 0)
            rule("^Maildir error: cannot (list|access|remove|create directory|create|stat|re-stat|open|read|write|set times for) ",
                 sys, P, 0)
            rule("^Maildir error: path ", " is too deeply nested[.] Symlink loop[?]$", P, 0)
            rule("^Maildir error: " q, q " is no valid mailbox$", P, 0)
            rule("^Error: invalid SyncState location " q, q "$", P, 0)
            rule("^Error: (incomplete|malformed|unrecognized) sync state header entry at ", ":[0-9]+$", P, 0)
            rule("^Error: (incomplete|invalid) sync state entry at ", ":[0-9]+$", P, 0)
            rule("^Error: (incomplete|malformed|unrecognized) journal entry at ", ":[0-9]+$", P, 0)
            rule("^Error: journal entry at ", ":[0-9]+ refers to non-existing sync state entry$", P, 0)
            # Text the IMAP server (Bridge) chose, which may name a
            # folder: src/drv_imap.c. Cut after the fixed part.
            rule("^IMAP command " q "[^" q "]*" q " returned an error: (NO|BAD)", "", " " srv, 0)
            # 1.5.1 drops the NO or BAD.
            rule("^IMAP command " q "[^" q "]*" q " returned an error: ", "", srv, 0)
            rule("^IMAP error: malformed sequence number ", "", srv, 0)
            rule("^(Error|Warning) from IMAP server: ", "", srv, 0)
            rule("^[*][*][*] IMAP ALERT [*][*][*] ", "", srv, 0)
            rule("^IMAP error: unexpected (BYE response:|reply:|tag) ", "", srv, 0)
            rule("^IMAP error: (bogus greeting|unrecognized untagged) response ", "", srv, 0)
            rule("^IMAP warning: unknown system flag ", "", srv, 0)
            dest = (stream == "err") ? "/dev/stderr" : "/dev/stdout"
        }
        stream == "err" && $0 ~ far_box && $0 !~ inbox { withheld++; next }
        # One string, so each line is one write: mawk writes the parts
        # of a format separately, and the other filter may share dest.
        { other++; printf "%s", redact($0) "\n" > dest; fflush(dest) }
        END { print withheld + 0, other + 0 > counts; close(counts) }
    '
}

report_mbsync_errors() {
    # mbsync's stderr: see filter_mbsync_output.
    filter_mbsync_output err "$MBSYNC_ERROR_COUNTS_FILE"
}

report_mbsync_notices() {
    # mbsync's stdout (notices, such as a UIDVALIDITY recovery): redacted
    # like stderr, never withheld; its counts only mark the end.
    filter_mbsync_output out "$MBSYNC_NOTICES_DONE_FILE"
}

wait_for_mbsync_filter() {
    # mbsync's streams close when it exits, but a filter may still be
    # finishing. Waits a bounded time for the file a filter writes at end
    # of input ($1); fails if it never arrives.
    local i
    for ((i = 0; i < MBSYNC_ERROR_COUNTS_WAIT_TENTHS; i++)); do
        if [[ -s "$1" ]]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

read_mbsync_error_counts() {
    # Prints the stderr filter's counts; fails if they never arrive, so
    # nothing is tolerated.
    if wait_for_mbsync_filter "$MBSYNC_ERROR_COUNTS_FILE"; then
        cat "$MBSYNC_ERROR_COUNTS_FILE"
        return 0
    fi
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
    #
    # mbsync runs under timeout(1) with the per-run deadline (#282). A run
    # stopped there fails like any other, so the loop's failure limit
    # applies and no success stamp is written.
    local rc=0 counts="" withheld=0 other=0 started duration
    rm -f "$MBSYNC_ERROR_COUNTS_FILE" "$MBSYNC_NOTICES_DONE_FILE"
    mark_sync_activity
    # Both streams go through the filters as they are written; run_child
    # waits on (and signals) timeout, which passes a stop on to mbsync.
    started="$SECONDS"
    run_child timeout --kill-after="${SYNC_KILL_GRACE_SECONDS}s" "${SYNC_DEADLINE_SECONDS}s" \
        mbsync -c "$CONFIG_FILE" -a \
        > >(report_mbsync_notices) 2> >(report_mbsync_errors) || rc=$?
    duration=$((SECONDS - started))
    mark_sync_activity
    # 124: stopped by TERM at the deadline; 137: KILLed, after ignoring
    # TERM for the grace (or by the kernel, which timeout cannot tell).
    if ((rc == 124)); then
        echo ">>> ERROR: mbsync did not finish within SYNC_DEADLINE_SECONDS=${SYNC_DEADLINE_SECONDS} and was stopped; counting this sync as failed (see docs/troubleshooting.md)." >&2
    elif ((rc == 137)); then
        echo ">>> ERROR: mbsync was killed: it ignored the stop at SYNC_DEADLINE_SECONDS=${SYNC_DEADLINE_SECONDS} for ${SYNC_KILL_GRACE_SECONDS}s, or the kernel killed it; counting this sync as failed." >&2
    fi
    if ! wait_for_mbsync_filter "$MBSYNC_NOTICES_DONE_FILE"; then
        echo ">>> WARNING: mbsync's notice filter did not finish." >&2
    fi
    rm -f "$MBSYNC_NOTICES_DONE_FILE"
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
    # After every repair pass, even a failed one, which may still have
    # opened some folders. Missing it only delays the indexer's re-watch
    # to the next success stamp or its recovery sweep, so the result stands.
    if ! signal_perms_repaired; then
        echo ">>> WARNING: could not signal the indexer to re-watch Maildir folders; it does so at the next successful sync or its recovery sweep." >&2
    fi
    mark_sync_activity
    # The duration is mbsync's run, the part SYNC_DEADLINE_SECONDS bounds,
    # so the deadline can be tuned from this line (#879).
    if ((rc == 0)); then
        printf '>>> Sync ok in %ds (deadline %ds)\n' "$duration" "$SYNC_DEADLINE_SECONDS"
    fi
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
# app's endpoint from BRIDGE_HOST, BRIDGE_IMAP_PORT and BRIDGE_CERT_HOST, and
# BRIDGE_USER, all set in docker-compose.yml / .env.
# BRIDGE_PASS is NOT passed as an env var — mbsyncrc uses PassCmd to read
# it directly from the Docker secret at /run/secrets/bridge_pass.
# =============================================================================
install_signal_handlers
# Before validation, so a refused setting still leaves the line (#887).
log_startup_identity
require_prerequisites
check_maildir_layout || exit 1

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
# needing to verify it first; it must then match BRIDGE_CERT_FINGERPRINT
# (verify_expected_fingerprint). On first boot the SHA-256 fingerprint is
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
