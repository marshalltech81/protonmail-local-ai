#!/bin/bash
set -Eeuo pipefail

# Layout (PLAN.md decision 45): the isync transport's settings, then the
# Proton provider integration between its begin and end markers, then
# the transport's functions and the startup sequence, which reach the
# provider only through the entry points its section names.

readonly SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
readonly RUNTIME_DIR="/tmp/mbsync"
readonly TEMPLATE_FILE="/etc/mbsyncrc.template"
# The channel's Patterns exclusions, two sets with two owners (PLAN.md
# decision 45), each substituted into its own placeholder on the
# template's Patterns line by render_mbsync_config, the layout set last.
# The Channel comment in mbsyncrc.template explains both. Proton's set,
# the PROTON_FOLDER_EXCLUSIONS setting (always keeping All Mail, Labels/*
# and Starred), is in the provider section below; the transport's set is
# not configuration.
# isync transport: a child folder named after a file isync keeps in its
# parent's directory (SubFolders Legacy, SyncState *), and everything
# below it.
readonly ISYNC_LAYOUT_EXCLUSIONS='!"*/uidvalidity" !"*/uidvalidity/*" !"*/isyncuidmap.db" !"*/isyncuidmap.db/*" !"*/mbsyncstate" !"*/mbsyncstate/*" !"*/mbsyncstate.journal" !"*/mbsyncstate.journal/*" !"*/mbsyncstate.new" !"*/mbsyncstate.new/*" !"*/mbsyncstate.lock" !"*/mbsyncstate.lock/*"'
readonly CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
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
readonly BRIDGE_WAIT_INTERVAL_SECONDS=2
readonly BRIDGE_WAIT_MAX_ATTEMPTS=300
# Per-probe connect timeout (nc -w). timeout(1) allows one second more
# and bounds the whole probe, name resolution included, so each attempt
# takes at most BRIDGE_PROBE_TIMEOUT_SECONDS + 1 seconds plus the interval.
readonly BRIDGE_PROBE_TIMEOUT_SECONDS=2
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
# The collection status (#1592), format 1: the latest attempt, the last
# terminal outcome with its withheld far-side folder count and failure
# class, and the last success. Published before and after every sync
# attempt (publish_collection_status) and read by the indexer
# (indexer/src/collection_status.py) for get_mailbox_status. It holds
# counts and fixed tokens only, never a folder name, and reading it never
# acknowledges a sync: the success stamp above does that.
readonly COLLECTION_STATUS_FILE="${MAILDIR_PATH}/.mbsync-collection-status.json"
readonly COLLECTION_STATUS_FORMAT=1
# The account the status is published for (#1596): a canonical lower-case
# UUID, checked by require_prerequisites. The indexer reads the status as
# unknown unless it names the account the index is registered to.
readonly ACCOUNT_SCOPE="${ACCOUNT_SCOPE:-}"

# Owner-only umask for the runtime tmp dir (config + cert material).
# mbsync itself ignores umask for Maildir writes — it explicitly passes
# mode 0600 to ``open()`` and can create subdirectories under this umask,
# so the post-sync chmod hook in ``relax_new_maildir_perms`` is what makes
# new Maildir paths traversable/readable to the indexer (a different UID).
umask 077
mkdir -p "$RUNTIME_DIR"

# --- Proton provider integration: begin ---
# The Proton Bridge app's connection and trust policy (PLAN.md decision
# 45): the endpoint and expected-fingerprint settings and their
# validation, the Host and Tunnel isync connects through, certificate
# extraction over implicit TLS, the expected fingerprint and the
# persistent pin with its rotation, and Proton's folder exclusions. The
# transport below calls only validate_bridge_endpoint,
# expected_fingerprint, bridge_imap_host, bridge_imap_connect,
# warn_if_pin_rotation_enabled, extract_bridge_cert,
# validate_proton_settings and provider_exclusion_patterns, and reads
# constants defined below: the endpoint, the isync names, the folder
# exclusions, the password file and, for the startup identity line, the
# source selector and the certificate settings.

# The defaults and validators of the Proton settings, one implementation
# shared with scripts/validate-env.sh (PLAN.md decision 46): copied from
# mbsync/proton-settings.sh by the Dockerfile.
readonly PROTON_SETTINGS_FILE="/usr/local/lib/proton-settings.sh"
# shellcheck source=mbsync/proton-settings.sh disable=SC1091 # linted on its own
source "$PROTON_SETTINGS_FILE"

# The source selector (PLAN.md decision 46): proton-bridge, the only
# source, is accepted; validate_proton_settings refuses anything else
# before rendering or any network use. Read here once, trimmed and in
# lower case like every *_MODE setting; nothing after that check reads
# it, and no control depends on it.
SOURCE_MODE="$(proton_normalize_source_mode "${SOURCE_MODE:-$PROTON_DEFAULT_SOURCE_MODE}")"
readonly SOURCE_MODE
# The Bridge app runs on this Mac; mbsync connects to it here, through a
# name that forwards to the host's 127.0.0.1 (validate_proton_settings).
# Compared and used without surrounding whitespace, in lower case.
BRIDGE_HOST="$(proton_normalize_bridge_host "${BRIDGE_HOST:-$PROTON_DEFAULT_BRIDGE_HOST}")"
readonly BRIDGE_HOST
readonly BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-$PROTON_DEFAULT_BRIDGE_IMAP_PORT}"
# The name the app's certificate is checked against (its CN). See
# bridge_imap_host.
readonly BRIDGE_CERT_HOST="${BRIDGE_CERT_HOST:-$PROTON_DEFAULT_BRIDGE_CERT_HOST}"
# isync's account, store and channel names (#1498): substituted into
# mbsyncrc by render_mbsync_config and matched by filter_mbsync_output,
# from these values only, once validate_proton_settings has accepted them.
readonly PROTON_IMAP_ACCOUNT_NAME="${PROTON_IMAP_ACCOUNT_NAME:-$PROTON_DEFAULT_IMAP_ACCOUNT_NAME}"
readonly PROTON_REMOTE_STORE_NAME="${PROTON_REMOTE_STORE_NAME:-$PROTON_DEFAULT_REMOTE_STORE_NAME}"
readonly PROTON_LOCAL_STORE_NAME="${PROTON_LOCAL_STORE_NAME:-$PROTON_DEFAULT_LOCAL_STORE_NAME}"
readonly PROTON_CHANNEL_NAME="${PROTON_CHANNEL_NAME:-$PROTON_DEFAULT_CHANNEL_NAME}"
# Required: the SHA-256 fingerprint the operator took from the Bridge
# app; see verify_expected_fingerprint.
readonly BRIDGE_CERT_FINGERPRINT="${BRIDGE_CERT_FINGERPRINT:-}"
# Literally true or false (validate_proton_settings).
readonly BRIDGE_CERT_PIN_ROTATE="${BRIDGE_CERT_PIN_ROTATE:-$PROTON_DEFAULT_BRIDGE_CERT_PIN_ROTATE}"
# Proton provider policy: folder patterns separated by |, rendered as the
# Patterns exclusions by render_mbsync_config
# (proton_folder_exclusion_patterns). They always include Bridge's
# virtual All Mail and top-level Starred and Labels/*, which list each
# message again (never synced, AGENTS.md; see the template's Channel
# comment); an operator may add more.
readonly PROTON_FOLDER_EXCLUSIONS="${PROTON_FOLDER_EXCLUSIONS:-$PROTON_DEFAULT_FOLDER_EXCLUSIONS}"
readonly BRIDGE_PASS_FILE="/run/secrets/bridge_pass"
readonly CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"
# State directory persists the pinned Bridge cert fingerprint and the
# success generation counter across container restarts. The directory
# is backed by a named volume so it survives `docker compose down` /
# rebuilds but is cleared by `make clean`, giving the operator a clean
# way to start over if needed.
readonly STATE_DIR="/state"
readonly PIN_FILE="${STATE_DIR}/bridge-cert.fingerprint"
# The durable, monotonically increasing counter next_success_generation
# allocates: one per successful sync, in the state volume rather than
# the index, so no schema change is needed (#1621 thread, 2026-10-13).
readonly GENERATION_FILE="${STATE_DIR}/sync-generation"
readonly CERT_EXTRACT_TIMEOUT_SECONDS=20

validate_proton_settings() {
    # The shared validators (proton-settings.sh), the ones
    # scripts/validate-env.sh runs: SOURCE_MODE first, then the Bridge
    # host and certificate name must be this Mac's, the port a port, pin
    # rotation literally true or false, the folder exclusions well formed
    # and keeping the three protected ones, and the isync names a safe
    # set of characters. Fixed text: a refused value is never printed.
    local problem
    if ! problem="$(proton_settings_problem "$SOURCE_MODE" "$BRIDGE_HOST" "$BRIDGE_IMAP_PORT" \
        "$BRIDGE_CERT_HOST" "$BRIDGE_CERT_PIN_ROTATE" "$PROTON_FOLDER_EXCLUSIONS" \
        "$PROTON_IMAP_ACCOUNT_NAME" "$PROTON_REMOTE_STORE_NAME" "$PROTON_LOCAL_STORE_NAME" \
        "$PROTON_CHANNEL_NAME")"; then
        printf '>>> ERROR: %s\n' "$problem" >&2
        return 1
    fi
}

provider_exclusion_patterns() {
    # Proton's folder exclusions as isync Patterns tokens (!"…", in the
    # order configured), from the value validate_proton_settings accepted.
    proton_folder_exclusion_patterns "$PROTON_FOLDER_EXCLUSIONS"
}

validate_bridge_endpoint() {
    # These values are written into mbsyncrc and, with a tunnel, into the
    # shell command isync runs for it, so only a plain host name or IPv4
    # address and a decimal port are accepted. Checked here on their own,
    # after validate_proton_settings has checked the hosts against their
    # allowlists.
    local host_re='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'

    if [[ ! "$BRIDGE_HOST" =~ $host_re ]]; then
        echo ">>> ERROR: BRIDGE_HOST must be a host name or IPv4 address." >&2
        return 1
    fi
    if [[ ! "$BRIDGE_IMAP_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || ((BRIDGE_IMAP_PORT > 65535)); then
        echo ">>> ERROR: BRIDGE_IMAP_PORT must be a port number from 1 to 65535." >&2
        return 1
    fi
    # The certificate name is never connected to, so it has its own exact
    # check instead of the host name pattern (PLAN.md decision 46): one of
    # the loopback names proton_cert_host_allowed lists, ::1 included.
    if ! proton_cert_host_allowed "$BRIDGE_CERT_HOST"; then
        echo ">>> ERROR: BRIDGE_CERT_HOST must be a loopback name (see validate_proton_settings)." >&2
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

bridge_imap_host() {
    # isync checks Bridge's certificate against the name in Host. The
    # Bridge app's certificate is issued for 127.0.0.1 only, so Host is
    # BRIDGE_CERT_HOST and the connection goes through bridge_imap_connect.
    printf '%s' "$BRIDGE_CERT_HOST"
}

bridge_imap_connect() {
    # The connection to BRIDGE_HOST:BRIDGE_IMAP_PORT goes through a Tunnel,
    # which isync opens instead of a socket to Host. Implicit TLS, the
    # certificate check and the pinned CertificateFile apply as for a
    # direct connection: TLS runs end to end between mbsync and Bridge,
    # and socat only relays bytes. socat rather than nc, because isync
    # waits for the server's close after LOGOUT and nc does not pass it on.
    printf '%s' "Tunnel \"exec socat - TCP:${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}\""
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

warn_if_pin_rotation_enabled() {
    # BRIDGE_CERT_PIN_ROTATE is an opt-in for accepting one legitimate
    # Bridge cert rotation. It is part of the container's environment, so
    # it stays true through every restart of this container (manual or by
    # the restart policy) until the container is recreated with it false;
    # until then a new cert that matches BRIDGE_CERT_FINGERPRINT replaces
    # the pin without comparison to it. Surface that on every boot so the
    # operator notices if they forgot.
    if [[ "$BRIDGE_CERT_PIN_ROTATE" == "true" ]]; then
        echo ">>> WARNING: BRIDGE_CERT_PIN_ROTATE=true — a Bridge cert fingerprint change this boot will be accepted without comparison to the pin (the cert must still match BRIDGE_CERT_FINGERPRINT)." >&2
        echo ">>> This is intended only for one boot after a deliberate Bridge cert rotation (e.g. Bridge upgrade); a restart keeps it enabled." >&2
        echo ">>> Once the rotation succeeds, recreate mbsync with BRIDGE_CERT_PIN_ROTATE=false to re-enable pin enforcement (see docs/troubleshooting.md)." >&2
    fi
}
# --- Proton provider integration: end ---

# The exact shapes publish_collection_status and finish_collection_attempt
# write; load_collection_records carries the records of a status file in
# these shapes only. An attempt ID is its start's boot token and attempt
# count. Numbers are not bounded here: the indexer reads any value up to
# Bash's 64-bit limit, as this start may write it.
readonly COLLECTION_ATTEMPT_ID_RE='[0-9a-f]{12}-[1-9][0-9]*'
readonly COLLECTION_TIME_RE='[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'
readonly COLLECTION_TERMINAL_RE="^\\{\"attempt_id\": \"${COLLECTION_ATTEMPT_ID_RE}\", \"completed_at\": \"${COLLECTION_TIME_RE}\", \"outcome\": \"(succeeded|succeeded_with_withheld_folders|failed)\", \"withheld_far_side_folders\": (null|0|[1-9][0-9]*), \"failure_class\": (null|\"(deadline|killed|error|permission_repair|interrupted)\")\\}\$"
readonly COLLECTION_SUCCESS_RE="^\\{\"attempt_id\": \"${COLLECTION_ATTEMPT_ID_RE}\", \"completed_at\": \"${COLLECTION_TIME_RE}\"\\}\$"

account_scope_is_canonical() {
    # A canonical lower-case UUID, compared byte by byte.
    local LC_ALL=C
    [[ "$ACCOUNT_SCOPE" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

require_prerequisites() {
    # First: nothing is rendered or connected with a refused value.
    validate_proton_settings || exit 1

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

    # The account the collection status names (#1592, #1596), with
    # scripts/validate-env.sh's fixed text, which never repeats the value.
    if [[ -z "$ACCOUNT_SCOPE" ]]; then
        printf '>>> ERROR: %s\n' 'ACCOUNT_SCOPE is not set. Run make init-secrets, which writes a new one to .env when none is there, or restore the value this index was built with; see docs/setup.md and docs/troubleshooting.md "Account scope mismatch".' >&2
        exit 1
    fi
    if ! account_scope_is_canonical; then
        printf '>>> ERROR: %s\n' 'ACCOUNT_SCOPE must be a UUID in canonical lower-case form (8-4-4-4-12 hexadecimal digits). Restore the value this index was built with; see docs/troubleshooting.md "Account scope mismatch".' >&2
        exit 1
    fi

    validate_bridge_endpoint || exit 1
}

log_startup_identity() {
    # One line naming what is running (#887): the source commit baked into
    # the image (GIT_COMMIT; anything but a plain token is "unknown"), a
    # random ID for this start, and the first 12 hex digits of a SHA-256
    # over the non-secret settings named below: the source selector, the
    # endpoint, Proton's folder exclusions, isync's
    # account, store and channel names, the sync timing, the expected certificate fingerprint (not secret; in the
    # form verify_expected_fingerprint compares), whether pin rotation
    # is on and the account the collection status names (ACCOUNT_SCOPE, a
    # UUID that is not secret). BRIDGE_USER and the password are not inputs. It runs before
    # validation: values are hashed as configured, never parsed or printed,
    # so nothing here can fail on a malformed setting. An endpoint value
    # with an "@" (userinfo, which validation would refuse) is hashed as a
    # marker, so the hash cannot be used to test guesses at a password.
    # Proton's folder exclusions are hashed as "default" or as the number
    # of patterns, never the patterns: an added one names a folder, which
    # is mailbox content, and a short hash of a low-entropy name could be
    # tested against guesses.
    local commit="${GIT_COMMIT:-unknown}" boot config rotate="false" exclusions pipes
    local host="$BRIDGE_HOST" port="$BRIDGE_IMAP_PORT" cert_host="$BRIDGE_CERT_HOST"
    if [[ "$PROTON_FOLDER_EXCLUSIONS" == "$PROTON_DEFAULT_FOLDER_EXCLUSIONS" ]]; then
        exclusions="default"
    else
        pipes="${PROTON_FOLDER_EXCLUSIONS//[!|]/}"
        exclusions="custom:$((${#pipes} + 1))"
    fi
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
    config="$(printf 'SOURCE_MODE=%s\nBRIDGE_HOST=%s\nBRIDGE_IMAP_PORT=%s\nBRIDGE_CERT_HOST=%s\nPROTON_FOLDER_EXCLUSIONS=%s\nPROTON_IMAP_ACCOUNT_NAME=%s\nPROTON_REMOTE_STORE_NAME=%s\nPROTON_LOCAL_STORE_NAME=%s\nPROTON_CHANNEL_NAME=%s\nSYNC_INTERVAL=%s\nSYNC_DEADLINE_SECONDS=%s\nBRIDGE_CERT_FINGERPRINT=%s\nBRIDGE_CERT_PIN_ROTATE=%s\nACCOUNT_SCOPE=%s\n' \
        "$SOURCE_MODE" "$host" "$port" "$cert_host" "$exclusions" \
        "$PROTON_IMAP_ACCOUNT_NAME" "$PROTON_REMOTE_STORE_NAME" \
        "$PROTON_LOCAL_STORE_NAME" "$PROTON_CHANNEL_NAME" "$SYNC_INTERVAL" "$SYNC_DEADLINE_SECONDS" \
        "$(expected_fingerprint)" "$rotate" "$ACCOUNT_SCOPE" | sha256sum)"
    printf '>>> Startup identity: service=mbsync commit=%s boot=%s config=%s\n' \
        "$commit" "$boot" "${config:0:12}"
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

render_mbsync_config() {
    # Host and the connection are the provider's (bridge_imap_host,
    # bridge_imap_connect), from settings require_prerequisites has
    # validated.
    local imap_host imap_connect provider_exclusions
    imap_host="$(bridge_imap_host)"
    imap_connect="$(bridge_imap_connect)"
    provider_exclusions="$(provider_exclusion_patterns)"

    # Only the named variables are substituted. BRIDGE_PASS is never one:
    # mbsyncrc's PassCmd reads it from the Docker secret. The two Patterns
    # exclusion sets come from the provider's (provider_exclusion_patterns)
    # and the ISYNC_LAYOUT_EXCLUSIONS constant, in that order, so the
    # layout exclusions come last; isync's account,
    # store and channel names (MBSYNC_IMAP_ACCOUNT, MBSYNC_REMOTE_STORE,
    # MBSYNC_LOCAL_STORE, MBSYNC_CHANNEL) from the PROTON_*_NAME settings
    # filter_mbsync_output matches, whatever the environment holds under
    # the substituted names.
    # shellcheck disable=SC2016 # the list names variables for envsubst
    MBSYNC_IMAP_HOST="$imap_host" MBSYNC_IMAP_CONNECT="$imap_connect" \
        MBSYNC_IMAP_ACCOUNT="$PROTON_IMAP_ACCOUNT_NAME" \
        MBSYNC_REMOTE_STORE="$PROTON_REMOTE_STORE_NAME" \
        MBSYNC_LOCAL_STORE="$PROTON_LOCAL_STORE_NAME" MBSYNC_CHANNEL="$PROTON_CHANNEL_NAME" \
        MBSYNC_PROVIDER_EXCLUSIONS="$provider_exclusions" \
        MBSYNC_LAYOUT_EXCLUSIONS="$ISYNC_LAYOUT_EXCLUSIONS" \
        envsubst '${MBSYNC_IMAP_HOST} ${MBSYNC_IMAP_CONNECT} ${BRIDGE_USER} ${MBSYNC_IMAP_ACCOUNT} ${MBSYNC_REMOTE_STORE} ${MBSYNC_LOCAL_STORE} ${MBSYNC_CHANNEL} ${MBSYNC_PROVIDER_EXCLUSIONS} ${MBSYNC_LAYOUT_EXCLUSIONS}' \
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

# The collection status's records (publish_collection_status): this
# start's boot token and attempt count, the attempt in progress (empty
# between attempts), whether run_sync has left that attempt's result
# (1) or not (0), and the last terminal and last success records as
# JSON objects, or null.
collection_boot=""
collection_count=0
collection_attempt_id=""
collection_result_ready=0
collection_attempt_started_at=""
collection_last_terminal="null"
collection_last_success="null"

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
    # A stop during a sync attempt is its outcome (#1592), unless run_sync
    # has already left the attempt's result, which is then published (a
    # success with the stamp's time once the stamp was written).
    if [[ -n "${collection_attempt_id:-}" ]]; then
        if [[ "${collection_result_ready:-0}" == 1 ]]; then
            record_collection_outcome
        else
            finish_collection_attempt failed null interrupted "$(collection_now)"
        fi
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
    # channel line (Error, Warning or Notice: channel) matching no rule
    # is cut after "channel " (see below). Any other line matching no
    # rule, or whose kept tail would still hold a path under the Maildir,
    # is cut at the first such path. Any other line is passed on
    # unchanged. A fixed set of anchored matches per line, each linear in
    # the line's length.
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
    # The channel and far store names in these shapes are the configured
    # ones (PROTON_CHANNEL_NAME, PROTON_REMOTE_STORE_NAME, the values
    # render_mbsync_config writes, #1498), each matched as literal text.
    # A channel line that no rule matches, whatever the channel's name, is
    # cut after "channel " (on stderr it still counts as another error,
    # so the sync fails), so a shape this filter does not know fails
    # closed instead of passing a folder name on.
    #
    # The image's awk is mawk, which reads a pipe in blocks and would hold
    # lines back until mbsync exits; -W interactive makes it read lines.
    local awk_cmd=(awk)
    if command -v mawk >/dev/null; then
        awk_cmd=(mawk -W interactive)
    fi
    # The names reach awk through ENVIRON, which keeps them as written
    # (-v would process backslash escapes).
    # shellcheck disable=SC2016 # an awk program: its $ are awk's
    FILTER_CHANNEL="$PROTON_CHANNEL_NAME" FILTER_FAR_STORE="$PROTON_REMOTE_STORE_NAME" \
        "${awk_cmd[@]}" -v stream="$1" -v counts="$2" -v maildir="${MAILDIR_PATH}/" '
        function rule(prefix, suffix, replacement, keep_inbox) {
            n++
            pre[n] = prefix; suf[n] = suffix; rep[n] = replacement; inbox_ok[n] = keep_inbox
        }
        # s as a regular expression matching itself: every character
        # other than a letter, digit or _ is matched by a bracket
        # expression (\ and ^, which cannot stand alone in one, by an
        # escape). The names are validated to [A-Za-z0-9_-] already.
        function lit(s,    i, c, out) {
            out = ""
            for (i = 1; i <= length(s); i++) {
                c = substr(s, i, 1)
                if (c ~ /[A-Za-z0-9_]/)
                    out = out c
                else if (c == "\\" || c == "^")
                    out = out "\\" c
                else
                    out = out "[" c "]"
            }
            return out
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
            if (match(line, "^(Error|Warning|Notice): channel "))
                return substr(line, 1, RLENGTH) "(rest of line withheld)"
            at = index(line, maildir)
            if (at > 0)
                return substr(line, 1, at - 1) "<path>"
            return line
        }
        BEGIN {
            q = "\047"; F = "<folder>"; P = "<path>"
            channel = lit(ENVIRON["FILTER_CHANNEL"])
            ch = "^Error: channel " channel
            far_box = ch ": far side box .+ cannot be opened( anymore)?[.]$"
            inbox = ch ": far side box INBOX cannot be opened( anymore)?[.]$"
            cut = q " (rest of line withheld)"
            srv = "(server text withheld)"
            sys = ": [^:]*$"
            # Box names: src/sync.c, main.c (1.5.1: main_sync.c),
            # drv_imap.c, drv_maildir.c.
            rule(ch ": (far|near) side box ", " cannot be opened( anymore)?[.]$", F, 1)
            rule(ch ": both far side ", " cannot be opened[.]$", F " and near side " F, 0)
            rule("^Warning: channel " channel ": far side box ", " is not empty[.]$",
                 F " cannot be opened anymore, and near side box " F, 0)
            rule("^Warning: channel " channel ": near side box ", " is not empty[.]$",
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
            rule("^Notice: channel " channel ", (far|near) side box ",
                 ": Recovered from change of UIDVALIDITY[.]$", F, 1)
            rule("^(Opening|Creating|Deleting) (far|near) side box ", "[.][.][.]$", F, 1)
            rule("^Error: channel :" lit(ENVIRON["FILTER_FAR_STORE"]) ":", " is locked$", "<folders>", 0)
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

collection_now() {
    # The format the success stamp uses, so a success's completion time
    # in the status file is the stamp's, character for character.
    date -u +%Y-%m-%dT%H:%M:%SZ
}

load_collection_records() {
    # Picks this start's boot token, then carries the last terminal and
    # last success records of the status file an earlier start published
    # into this start's first publication: the status file is their only
    # store. A missing file is a first start. A file in any other shape
    # than publish_collection_status writes (at most 4096 bytes read), or
    # one naming another account, is set aside with a warning and the
    # records start empty (null): the status then shows no earlier
    # outcome, never a made-up one. An earlier attempt left in_progress
    # (its shell was killed) is not carried: only its records are.
    local LC_ALL=C line="" terminal success attempt re
    collection_boot="$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
    if [[ ! -e "$COLLECTION_STATUS_FILE" && ! -L "$COLLECTION_STATUS_FILE" ]]; then
        return 0
    fi
    terminal="${COLLECTION_TERMINAL_RE#^}"
    terminal="${terminal%\$}"
    success="${COLLECTION_SUCCESS_RE#^}"
    success="${success%\$}"
    attempt="\\{\"id\": \"${COLLECTION_ATTEMPT_ID_RE}\", \"started_at\": \"${COLLECTION_TIME_RE}\", \"outcome\": \"(in_progress|succeeded|succeeded_with_withheld_folders|failed)\"\\}"
    re="^\\{\"format\": ${COLLECTION_STATUS_FORMAT}, \"scope_uuid\": \"${ACCOUNT_SCOPE}\", \"run_deadline_secs\": [1-9][0-9]*, \"sync_interval_secs\": [1-9][0-9]*, \"attempt\": ${attempt}, \"last_terminal\": (null|${terminal}), \"last_success\": (null|${success})\\}\$"
    if [[ -f "$COLLECTION_STATUS_FILE" ]] \
        && IFS= read -r -n 4096 line <"$COLLECTION_STATUS_FILE" \
        && [[ "$line" =~ $re ]]; then
        terminal="${line#*\"last_terminal\": }"
        collection_last_terminal="${terminal%, \"last_success\": *}"
        success="${line##*\"last_success\": }"
        collection_last_success="${success%\}}"
        return 0
    fi
    echo ">>> WARNING: the collection status at ${COLLECTION_STATUS_FILE} could not be read, or names another account; it starts again without an earlier outcome." >&2
}

publish_collection_status() {
    # Publishes the status file (format 1) for the current attempt with
    # outcome $1: written to a unique temporary file in the Maildir root,
    # made world-readable (the indexer runs as another UID) and renamed
    # into place, so a reader sees one whole publication. Every value is
    # a validated setting, a generated token, a time or a count. A failure
    # is warned and never changes the sync's result; the indexer keeps
    # reading the previous publication.
    local tmp=""
    if ! tmp="$(mktemp "${COLLECTION_STATUS_FILE}.XXXXXX")" \
        || ! printf '{"format": %d, "scope_uuid": "%s", "run_deadline_secs": %d, "sync_interval_secs": %d, "attempt": {"id": "%s", "started_at": "%s", "outcome": "%s"}, "last_terminal": %s, "last_success": %s}\n' \
            "$COLLECTION_STATUS_FORMAT" "$ACCOUNT_SCOPE" "$SYNC_DEADLINE_SECONDS" "$SYNC_INTERVAL" \
            "$collection_attempt_id" "$collection_attempt_started_at" "$1" \
            "$collection_last_terminal" "$collection_last_success" >"$tmp" \
        || ! chmod 644 "$tmp" || ! mv -f "$tmp" "$COLLECTION_STATUS_FILE"; then
        [[ -z "$tmp" ]] || rm -f "$tmp"
        echo ">>> WARNING: could not publish the collection status (outcome $1); get_mailbox_status shows the previous one." >&2
    fi
}

begin_collection_attempt() {
    # A new attempt, published as in_progress: "started, no terminal
    # result recorded". It proves nothing about whether mbsync is still
    # running; a shell killed outright leaves it in place until the next
    # start publishes.
    collection_count=$((collection_count + 1))
    collection_attempt_id="${collection_boot}-${collection_count}"
    collection_attempt_started_at="$(collection_now)"
    collection_result_ready=0
    publish_collection_status in_progress
}

finish_collection_attempt() {
    # The current attempt's terminal outcome: $1 succeeded,
    # succeeded_with_withheld_folders or failed; $2 the withheld far-side
    # folder count, or null when it is not known; $3 the failure class
    # (deadline, killed, error, permission_repair, interrupted), or null
    # for a success; $4 the completion time. The records follow
    # publication order, never their times.
    local class="null"
    [[ -n "$collection_attempt_id" ]] || return 0
    [[ "$3" == null ]] || class="\"$3\""
    collection_last_terminal="$(printf '{"attempt_id": "%s", "completed_at": "%s", "outcome": "%s", "withheld_far_side_folders": %s, "failure_class": %s}' \
        "$collection_attempt_id" "$4" "$1" "$2" "$class")"
    if [[ "$1" != failed ]]; then
        collection_last_success="$(printf '{"attempt_id": "%s", "completed_at": "%s"}' \
            "$collection_attempt_id" "$4")"
    fi
    publish_collection_status "$1"
    collection_attempt_id=""
}

record_collection_outcome() {
    # Publishes the outcome run_sync left in sync_outcome, sync_withheld
    # and sync_failure_class. A success is recorded after
    # record_successful_sync, with the stamp's completion time: the
    # indexer treats a success as delivered once the stamp it has
    # acknowledged names that time, and handles the stamp's rename first.
    local completed_at="${sync_completed_at:-}"
    if [[ "$sync_outcome" == failed || -z "$completed_at" ]]; then
        completed_at="$(collection_now)"
    fi
    finish_collection_attempt "$sync_outcome" "$sync_withheld" "$sync_failure_class" "$completed_at"
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
    #
    # The attempt is published as in_progress first, and its result left
    # for record_collection_outcome (#1592): sync_outcome, sync_withheld
    # (the withheld far-side folder count, null when the error filter's
    # counts never arrived) and sync_failure_class, the first cause of a
    # failure: deadline (stopped at SYNC_DEADLINE_SECONDS), killed (KILLed
    # after the grace, or by the kernel), error (any other mbsync failure,
    # far-side folders together with another error included) or
    # permission_repair (mbsync succeeded, the repair failed).
    local rc=0 counts="" withheld=0 other=0 started duration class=""
    sync_outcome=failed
    sync_withheld=null
    sync_failure_class=error
    sync_completed_at=""
    rm -f "$MBSYNC_ERROR_COUNTS_FILE" "$MBSYNC_NOTICES_DONE_FILE"
    begin_collection_attempt
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
        class=deadline
        echo ">>> ERROR: mbsync did not finish within SYNC_DEADLINE_SECONDS=${SYNC_DEADLINE_SECONDS} and was stopped; counting this sync as failed (see docs/troubleshooting.md)." >&2
    elif ((rc == 137)); then
        class=killed
        echo ">>> ERROR: mbsync was killed: it ignored the stop at SYNC_DEADLINE_SECONDS=${SYNC_DEADLINE_SECONDS} for ${SYNC_KILL_GRACE_SECONDS}s, or the kernel killed it; counting this sync as failed." >&2
    fi
    if ! wait_for_mbsync_filter "$MBSYNC_NOTICES_DONE_FILE"; then
        echo ">>> WARNING: mbsync's notice filter did not finish." >&2
    fi
    rm -f "$MBSYNC_NOTICES_DONE_FILE"
    # Missing counts leave withheld at 0, so nothing is tolerated.
    if counts="$(read_mbsync_error_counts)"; then
        read -r withheld other <<<"$counts"
        sync_withheld="$withheld"
    fi
    rm -f "$MBSYNC_ERROR_COUNTS_FILE"
    if ((withheld > 0)); then
        echo ">>> WARNING: ${withheld} far-side folder(s) could not be opened, most likely renamed or deleted in Proton; their local copies are kept. Folder names are not logged (see docs/troubleshooting.md)." >&2
        if ((rc == 1 && other == 0)); then
            echo ">>> mbsync reported no other error — counting this sync as successful." >&2
            rc=0
        fi
    fi
    if ((rc != 0)) && [[ -z "$class" ]]; then
        class=error
    fi
    if ! relax_new_maildir_perms; then
        echo ">>> ERROR: could not make new Maildir entries readable to the indexer." >&2
        rc=1
        [[ -n "$class" ]] || class=permission_repair
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
        sync_failure_class=null
        sync_outcome=succeeded
        if ((withheld > 0)); then
            sync_outcome=succeeded_with_withheld_folders
        fi
    else
        sync_failure_class="$class"
    fi
    collection_result_ready=1
    return "$rc"
}

# A durable, monotonically increasing counter: the generation
# record_successful_sync allocates to a success stamp, in the state
# volume (never SQLite — no schema change). Ordering an acknowledged
# stamp by this generation, rather than by its wall-clock completion
# time, closes the identity gap a clock rollback could open: two
# different attempts could otherwise publish the same completed_at
# (#1621 thread, 2026-10-13). First boot starts at 0 ("no generation
# allocated yet") and prints the next one, persisted before anything
# names it. A corrupt or unwritable counter is a failure, never a
# reused or skipped number, so the caller must not report the sync as
# acknowledged.
next_success_generation() {
    local current=0 raw next tmp

    if [[ -f "$GENERATION_FILE" ]]; then
        if ! IFS= read -r raw <"$GENERATION_FILE" || [[ ! "$raw" =~ ^[0-9]+$ ]]; then
            echo ">>> ERROR: the success generation at ${GENERATION_FILE} is not a non-negative integer — refusing to record this sync as successful." >&2
            return 1
        fi
        current="$raw"
    elif [[ -e "$GENERATION_FILE" ]]; then
        echo ">>> ERROR: ${GENERATION_FILE} is not a regular file — refusing to record this sync as successful." >&2
        return 1
    fi
    next=$((current + 1))
    if ! tmp="$(mktemp "${GENERATION_FILE}.XXXXXX")"; then
        return 1
    fi
    if ! printf '%s\n' "$next" >"$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" "$GENERATION_FILE"; then
        rm -f "$tmp"
        return 1
    fi
    printf '%s' "$next"
}

record_successful_sync() {
    # Written only after relax_new_maildir_perms, so every message this
    # sync delivered is already on disk and readable. The temporary name
    # carries this sync's identity: its completion time and interval (as
    # before), the attempt ID collection_status also names it by, and the
    # durable generation next_success_generation allocates. The indexer
    # acknowledges the sync named by the rename event it sees, which its
    # watcher handles only after this sync's delivery events, orders
    # acknowledgements by the generation (never wall-clock time, #1621
    # thread), and proves a later success delivered by the attempt ID
    # matching — never by a completion time a clock rollback could repeat
    # across two different attempts. Replaced atomically via rename;
    # world-readable because the indexer runs as a different UID. The
    # completion time is kept in sync_completed_at for
    # record_collection_outcome. Called as a condition (errexit is off),
    # so every step is checked: a failure removes the temporary file and
    # keeps the previous stamp, and the generation is allocated and
    # persisted before anything names it, so a failure allocating it
    # never reports the sync as acknowledged.
    local completed_at tmp generation
    completed_at="$(collection_now)"
    sync_completed_at="$completed_at"
    if ! generation="$(next_success_generation)"; then
        echo ">>> ERROR: could not allocate the durable success generation — refusing to record this sync as successful." >&2
        return 1
    fi
    tmp="${MAILDIR_PATH}/.mbsync-last-sync.${completed_at}.${SYNC_INTERVAL}.${collection_attempt_id}.${generation}.tmp"
    if ! printf '{"completed_at": "%s", "sync_interval_secs": %d, "attempt_id": "%s", "generation": %d}\n' \
        "$completed_at" "$SYNC_INTERVAL" "$collection_attempt_id" "$generation" >"$tmp" \
        || ! chmod 644 "$tmp" || ! mv -f "$tmp" "$SYNC_STAMP_FILE"; then
        rm -f "$tmp"
        return 1
    fi
}

stamp_write_failed() {
    # The sync succeeded but its stamp could not be written. Its outcome
    # is still published (#1592): the success is never delivered, since no
    # stamp names it, so it does not end an unrecovered failure. Then exit
    # for a container restart, as before.
    record_collection_outcome
    echo ">>> ERROR: could not write the success stamp ${SYNC_STAMP_FILE}; exiting for container restart." >&2
    exit 1
}

# =============================================================================
# Generate mbsync config from template (render_mbsync_config): the Bridge
# app's endpoint from BRIDGE_HOST, BRIDGE_IMAP_PORT and BRIDGE_CERT_HOST, and
# BRIDGE_USER, all set in docker-compose.yml / .env, isync's account,
# store and channel names (PROTON_*_NAME), plus the two Patterns
# exclusion sets (PROTON_FOLDER_EXCLUSIONS, ISYNC_LAYOUT_EXCLUSIONS).
# BRIDGE_PASS is NOT passed as an env var — mbsyncrc uses PassCmd to read
# it directly from the Docker secret at /run/secrets/bridge_pass.
# =============================================================================
install_signal_handlers
# Before validation, so a refused setting still leaves the line (#887).
log_startup_identity
require_prerequisites
check_maildir_layout || exit 1
# The collection status's earlier records and this start's boot token
# (#1592).
load_collection_records

warn_if_pin_rotation_enabled

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
    record_successful_sync || stamp_write_failed
    record_collection_outcome
else
    record_collection_outcome
    consecutive_sync_failures=1
    echo ">>> Initial sync returned a non-zero status (${consecutive_sync_failures}/${MAX_CONSECUTIVE_SYNC_FAILURES})." >&2
fi

# =============================================================================
# Continuous sync loop
# Waits SYNC_INTERVAL seconds after each sync ends, then polls Bridge IMAP
# again for new messages.
# Default interval is 60 seconds — set SYNC_INTERVAL in .env to change.
# =============================================================================
echo ">>> Starting sync loop (interval: ${SYNC_INTERVAL}s)..."
while true; do
    # Through run_child so a stop during the interval exits at once.
    run_child sleep "$SYNC_INTERVAL"
    echo ">>> Syncing..."
    if run_sync; then
        consecutive_sync_failures=0
        record_successful_sync || stamp_write_failed
        record_collection_outcome
        continue
    fi
    record_collection_outcome

    ((consecutive_sync_failures += 1))
    echo ">>> Sync failed (${consecutive_sync_failures}/${MAX_CONSECUTIVE_SYNC_FAILURES} consecutive failures)." >&2
    if ((consecutive_sync_failures >= MAX_CONSECUTIVE_SYNC_FAILURES)); then
        echo ">>> ERROR: mbsync failed ${MAX_CONSECUTIVE_SYNC_FAILURES} syncs in a row — exiting for container restart." >&2
        exit 1
    fi

    echo ">>> Bridge may still be busy — will retry next interval." >&2
done
