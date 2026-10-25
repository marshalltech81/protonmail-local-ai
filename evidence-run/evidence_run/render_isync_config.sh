#!/bin/bash
set -Eeuo pipefail

# Renders the isync configuration for the #1584 evidence run (#1759).
#
# It is the repository's own render (render_mbsync_config in
# mbsync/entrypoint.sh, from mbsync/mbsyncrc.template, with the production
# defaults and exclusions), followed by five exact line replacements and
# nothing else:
#   Tunnel "exec socat - TCP:host.docker.internal:1143"  -> Port <port>
#       (Bridge listens on this guest's own loopback; no socat tunnel)
#   PassCmd "cat /run/secrets/bridge_pass"               -> the secret file
#   CertificateFile /tmp/mbsync/bridge-cert.pem          -> the certificate file
#   Path /maildir/                                       -> the scratch Maildir
#   Inbox /maildir/INBOX                                 -> the scratch Maildir's INBOX
# Pull-only, Expunge None, TLSType IMAPS and the folder exclusions are kept
# as production sets them. Each replaced line must match exactly once, or
# nothing is written. The evidence-run tests diff the result against
# mbsync/tests/mbsyncrc.golden and fail on any other difference.
#
# Usage: BRIDGE_USER=... EVIDENCE_IMAP_PORT=... EVIDENCE_PASS_FILE=... \
#        EVIDENCE_CERT_FILE=... EVIDENCE_MAILDIR=... \
#        render_isync_config.sh OUTPUT_FILE
# TEMPLATE_FILE may name another template (the tests use it).
# Needs envsubst (gettext), as the production render does.

fail() {
    printf 'render_isync_config: %s\n' "$1" >&2
    exit 1
}

[[ $# -eq 1 && -n "$1" ]] || fail "usage: render_isync_config.sh OUTPUT_FILE"
OUTPUT="$1"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="${REPO_ROOT}/mbsync/entrypoint.sh"
SETTINGS="${REPO_ROOT}/mbsync/proton-settings.sh"

: "${BRIDGE_USER:=}" "${EVIDENCE_IMAP_PORT:=}" "${EVIDENCE_PASS_FILE:=}"
: "${EVIDENCE_CERT_FILE:=}" "${EVIDENCE_MAILDIR:=}"

# Values reach isync's configuration, so only plain characters are accepted;
# a refusal never repeats the value.
[[ "$BRIDGE_USER" =~ ^[A-Za-z0-9._@+-]+$ ]] || fail "BRIDGE_USER must be a plain user name"
if [[ ! "$EVIDENCE_IMAP_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || ((EVIDENCE_IMAP_PORT > 65535)); then
    fail "EVIDENCE_IMAP_PORT must be a port number from 1 to 65535"
fi
for name in EVIDENCE_PASS_FILE EVIDENCE_CERT_FILE EVIDENCE_MAILDIR; do
    [[ "${!name}" =~ ^/[A-Za-z0-9._/@+=,:-]+$ ]] || fail "${name} must be an absolute path of plain characters"
done
command -v envsubst >/dev/null 2>&1 || fail "envsubst (gettext) is required, as in the production render"

# Define the named functions exactly as the entrypoint writes them: from the
# `name() {` line through the first line that is a lone `}`.
load_function() {
    local name definition
    for name in "$@"; do
        definition="$(awk -v start="${name}() {" '$0 == start {p = 1} p {print} p && $0 == "}" {exit}' "$ENTRYPOINT")"
        [[ -n "$definition" ]] || fail "cannot find ${name} in the entrypoint"
        eval "$definition"
    done
}

# shellcheck source=mbsync/proton-settings.sh disable=SC1091 # linted on its own
source "$SETTINGS"
layout_line="$(grep -E "^readonly ISYNC_LAYOUT_EXCLUSIONS='[^']*'\$" "$ENTRYPOINT")" \
    || fail "cannot find ISYNC_LAYOUT_EXCLUSIONS in the entrypoint"
eval "${layout_line#readonly }"
load_function bridge_imap_host bridge_imap_connect provider_exclusion_patterns render_mbsync_config

TEMPLATE_FILE="${TEMPLATE_FILE:-${REPO_ROOT}/mbsync/mbsyncrc.template}"
[[ -f "$TEMPLATE_FILE" ]] || fail "the template is not a file"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CONFIG_FILE="${WORK}/production.rc"

# Production's own values, so the render below is production's render.
# shellcheck disable=SC2034 # read by the entrypoint functions loaded with eval
set_production_values() {
    BRIDGE_HOST="$PROTON_DEFAULT_BRIDGE_HOST"
    BRIDGE_IMAP_PORT="$PROTON_DEFAULT_BRIDGE_IMAP_PORT"
    BRIDGE_CERT_HOST="$PROTON_DEFAULT_BRIDGE_CERT_HOST"
    PROTON_IMAP_ACCOUNT_NAME="$PROTON_DEFAULT_IMAP_ACCOUNT_NAME"
    PROTON_REMOTE_STORE_NAME="$PROTON_DEFAULT_REMOTE_STORE_NAME"
    PROTON_LOCAL_STORE_NAME="$PROTON_DEFAULT_LOCAL_STORE_NAME"
    PROTON_CHANNEL_NAME="$PROTON_DEFAULT_CHANNEL_NAME"
    PROTON_FOLDER_EXCLUSIONS="$PROTON_DEFAULT_FOLDER_EXCLUSIONS"
}
set_production_values
export BRIDGE_USER
render_mbsync_config

# The five exact replacements; awk compares whole lines and counts matches.
awk \
    -v old1="Tunnel \"exec socat - TCP:${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}\"" \
    -v new1="Port ${EVIDENCE_IMAP_PORT}" \
    -v old2='PassCmd "cat /run/secrets/bridge_pass"' \
    -v new2="PassCmd \"cat ${EVIDENCE_PASS_FILE}\"" \
    -v old3='CertificateFile /tmp/mbsync/bridge-cert.pem' \
    -v new3="CertificateFile ${EVIDENCE_CERT_FILE}" \
    -v old4='Path /maildir/' \
    -v new4="Path ${EVIDENCE_MAILDIR}/" \
    -v old5='Inbox /maildir/INBOX' \
    -v new5="Inbox ${EVIDENCE_MAILDIR}/INBOX" '
    {
        for (i = 1; i <= 5; i++) {
            if ($0 == old[i]) { $0 = new[i]; hits[i]++; break }
        }
        print
    }
    BEGIN { old[1] = old1; old[2] = old2; old[3] = old3; old[4] = old4; old[5] = old5
            new[1] = new1; new[2] = new2; new[3] = new3; new[4] = new4; new[5] = new5 }
    END { for (i = 1; i <= 5; i++) if (hits[i] != 1) exit 1 }
' "$CONFIG_FILE" >"${WORK}/evidence.rc" || fail "a replaced line did not match exactly once"

chmod 600 "${WORK}/evidence.rc"
mv "${WORK}/evidence.rc" "$OUTPUT"
