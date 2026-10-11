#!/bin/bash
set -Eeuo pipefail

# Sourced, not run. Proton provider settings (PLAN.md decision 46, #1497): the defaults and
# validators of the Bridge app's container-side host name, the name its
# certificate is checked against, and isync's account, store and channel
# names. One implementation, sourced by scripts/validate-env.sh on the host
# (Bash 3.2 on macOS) and by mbsync/entrypoint.sh in the image, whose
# require_prerequisites runs it before rendering or any network use, so
# both refuse the same values with the same text. A refusal never repeats
# the value. Defines constants and functions only.
#
# Compose passes each setting as ${NAME:-}; an unset or empty value takes
# the default below, at both sites.

readonly PROTON_DEFAULT_BRIDGE_HOST="host.docker.internal"
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_BRIDGE_CERT_HOST="127.0.0.1"
# The names mbsync/mbsyncrc.template held before they were settings, so
# the rendered config is unchanged when none is set.
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_IMAP_ACCOUNT_NAME="protonmail"
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_REMOTE_STORE_NAME="protonmail-remote"
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_LOCAL_STORE_NAME="protonmail-local"
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_CHANNEL_NAME="protonmail"

# BRIDGE_HOST: container names for this Mac that forward to its
# 127.0.0.1, where the Bridge app listens. Each one is admitted only with
# a recorded forwarding test to a listener bound to 127.0.0.1 alone
# (mbsync/tests/host_alias_check.sh, make test-mbsync-host-aliases).
# host.orb.internal and host.internal are OrbStack's names. A Bridge
# anywhere else waits on the decision in #1499. No resolved-address check:
# DNS cannot show that a name is local, so the container engine and its
# resolver are trusted (docs/architecture.md, "Trust Boundaries").
PROTON_BRIDGE_HOST_ALIASES=(host.docker.internal host.orb.internal host.internal)
readonly PROTON_BRIDGE_HOST_ALIASES

# BRIDGE_HOST as compared and used: surrounding whitespace removed, lower
# case.
proton_normalize_bridge_host() {
    local LC_ALL=C value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value" | tr '[:upper:]' '[:lower:]'
}

# Succeeds when the normalized BRIDGE_HOST is exactly one of the aliases.
proton_bridge_host_allowed() {
    local alias
    for alias in "${PROTON_BRIDGE_HOST_ALIASES[@]}"; do
        if [[ "$1" == "$alias" ]]; then
            return 0
        fi
    done
    return 1
}

# Succeeds when BRIDGE_CERT_HOST is exactly a loopback identity: 127.0.0.1
# (the Bridge app's own certificate names only this), localhost, another
# 127.0.0.0/8 address in canonical dotted decimal (no leading zeros) or
# ::1. Each is shipped with a make test-mbsync-tls case: isync accepts a
# certificate issued for it and refuses one that is not, before LOGIN.
# Compared as written: no trimming or case folding.
proton_cert_host_allowed() {
    local LC_ALL=C octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
    local loopback_re="^127\\.${octet}\\.${octet}\\.${octet}\$"
    [[ "$1" == "localhost" || "$1" == "::1" || "$1" =~ $loopback_re ]]
}

# Succeeds when the value can name an isync account, store or channel:
# 1 to 64 of A-Z a-z 0-9 _ -, starting with a letter or digit. These names
# are written into mbsyncrc and into the output filter's patterns.
proton_name_allowed() {
    local LC_ALL=C
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]
}

# Prints the first problem with the resolved settings (defaults applied,
# BRIDGE_HOST normalized), as fixed text, and fails; prints nothing and
# succeeds when every value is accepted. Arguments: BRIDGE_HOST,
# BRIDGE_CERT_HOST, PROTON_IMAP_ACCOUNT_NAME, PROTON_REMOTE_STORE_NAME,
# PROTON_LOCAL_STORE_NAME, PROTON_CHANNEL_NAME.
proton_settings_problem() {
    local i
    local names=(PROTON_IMAP_ACCOUNT_NAME PROTON_REMOTE_STORE_NAME PROTON_LOCAL_STORE_NAME
        PROTON_CHANNEL_NAME)
    local values=("$3" "$4" "$5" "$6")

    if ! proton_bridge_host_allowed "$1"; then
        printf 'BRIDGE_HOST must be one of %s (the default is %s): mbsync reaches the Proton Mail Bridge app on this Mac only, through a name that forwards to its 127.0.0.1. A Bridge elsewhere waits on the decision in #1499.\n' \
            "${PROTON_BRIDGE_HOST_ALIASES[*]}" "$PROTON_DEFAULT_BRIDGE_HOST"
        return 1
    fi
    if ! proton_cert_host_allowed "$2"; then
        printf 'BRIDGE_CERT_HOST must be 127.0.0.1 (the default), localhost, another 127.x.y.z address without leading zeros, or ::1: the name of a certificate for this Mac'"'"'s loopback. Any other name waits on the decision in #1499.\n'
        return 1
    fi
    for ((i = 0; i < ${#names[@]}; i++)); do
        if ! proton_name_allowed "${values[i]}"; then
            printf '%s must be 1 to 64 characters from A-Z, a-z, 0-9, _ and -, starting with a letter or digit.\n' \
                "${names[i]}"
            return 1
        fi
    done
    if [[ "$4" == "$5" ]]; then
        printf 'PROTON_REMOTE_STORE_NAME and PROTON_LOCAL_STORE_NAME must differ.\n'
        return 1
    fi
}
