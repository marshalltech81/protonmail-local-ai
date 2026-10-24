#!/bin/bash
set -Eeuo pipefail

# Sourced, not run. Proton provider settings (PLAN.md decision 46, #1497): the defaults and
# validators of the source selector (SOURCE_MODE), the Bridge app's
# container-side host name, IMAP port and the name its certificate is
# checked against, Proton's folder exclusions, and isync's account,
# store and channel names. Pin rotation is a one-run command, not a
# setting (#1609; mbsync/entrypoint.sh PIN_ROTATION_RUN). One implementation,
# sourced by scripts/validate-env.sh on the host
# (Bash 3.2 on macOS) and by mbsync/entrypoint.sh in the image, whose
# require_prerequisites runs it before rendering or any network use, so
# both refuse the same values with the same text. A refusal never repeats
# the value. Defines constants and functions only.
#
# Compose passes each setting as ${NAME:-}; an unset or empty value takes
# the default below, at both sites.

# The only source mbsync supports. SOURCE_MODE is read once at startup
# and selects nothing else: no control depends on it.
readonly PROTON_DEFAULT_SOURCE_MODE="proton-bridge"
readonly PROTON_DEFAULT_BRIDGE_HOST="host.docker.internal"
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_BRIDGE_IMAP_PORT="1143"
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
# case (ASCII only).
proton_normalize_bridge_host() {
    local LC_ALL=C value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value" | LC_ALL=C tr '[:upper:]' '[:lower:]'
}

# Proton's folder exclusions: folder patterns separated by |, each
# rendered as one isync Patterns exclusion
# (proton_folder_exclusion_patterns). Bridge's All Mail and top-level
# Starred are virtual folders that list each message again, and Labels/*
# holds another copy of each labelled message: all three stay excluded.
# Labels come from message metadata (#1313), never from syncing these
# folders, so no value may leave them out.
# shellcheck disable=SC2034 # read by the scripts that source this file
readonly PROTON_DEFAULT_FOLDER_EXCLUSIONS="All Mail|Labels/*|Starred"
PROTON_PROTECTED_FOLDER_EXCLUSIONS=("All Mail" "Labels/*" "Starred")
readonly PROTON_PROTECTED_FOLDER_EXCLUSIONS

# SOURCE_MODE as compared: surrounding whitespace removed, lower case, as
# every *_MODE setting is read; ASCII case only.
proton_normalize_source_mode() {
    local LC_ALL=C value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value" | LC_ALL=C tr '[:upper:]' '[:lower:]'
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

# Succeeds when BRIDGE_IMAP_PORT is a decimal port from 1 to 65535,
# written without a sign, spaces or leading zeros.
proton_port_allowed() {
    local LC_ALL=C
    [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && ((10#$1 <= 65535))
}

# Succeeds when PROTON_FOLDER_EXCLUSIONS is well formed. The whole value
# first: not empty, every ASCII character one an element may hold or the
# | between elements, and no empty element (a leading, trailing or
# doubled |). Characters outside ASCII are accepted: isync (1.5) matches
# Patterns against folder names decoded to UTF-8, so a folder with such a
# name is excluded by writing it in UTF-8. Then each element: no space at
# either end. Quotes, backslashes, !, $, # and control characters are
# never accepted, so an element is written inside isync's quotes as it
# is, and a .env line that Compose would read differently from
# scripts/validate-env.sh (a $ expansion, an inline # comment) is refused
# rather than read two ways.
proton_folder_exclusions_allowed() {
    local LC_ALL=C rest="$1|" element ascii_left
    local element_re='^[^ ](.*[^ ])?$'
    # With the allowed ASCII characters removed, no ASCII character (in
    # the C locale, every printable or control one) may be left.
    ascii_left="${1//[A-Za-z0-9 .\/_*%&+,()@|-]/}"
    [[ -n "$1" && "$ascii_left" != *[[:print:][:cntrl:]]* ]] || return 1
    case "$1" in
        "|"* | *"|" | *"||"*) return 1 ;;
    esac
    while [[ -n "$rest" ]]; do
        element="${rest%%|*}"
        rest="${rest#*|}"
        [[ "$element" =~ $element_re ]] || return 1
    done
}

# Succeeds when every protected exclusion is one of the value's elements,
# written exactly.
proton_folder_exclusions_protected() {
    local protected element rest found
    for protected in "${PROTON_PROTECTED_FOLDER_EXCLUSIONS[@]}"; do
        found=0
        rest="$1|"
        while [[ -n "$rest" ]]; do
            element="${rest%%|*}"
            rest="${rest#*|}"
            if [[ "$element" == "$protected" ]]; then
                found=1
                break
            fi
        done
        ((found)) || return 1
    done
}

# Prints an accepted PROTON_FOLDER_EXCLUSIONS as isync Patterns
# exclusions: each element as one !"element" token, in order, separated
# by single spaces. Plain parameter expansion only: no eval, word
# splitting or glob expansion touches an element.
proton_folder_exclusion_patterns() {
    local rest="$1|" element out=""
    while [[ -n "$rest" ]]; do
        element="${rest%%|*}"
        rest="${rest#*|}"
        out+="${out:+ }!\"${element}\""
    done
    printf '%s' "$out"
}

# Succeeds when the value can name an isync account, store or channel:
# 1 to 64 of A-Z a-z 0-9 _ -, starting with a letter or digit. These names
# are written into mbsyncrc and into the output filter's patterns.
proton_name_allowed() {
    local LC_ALL=C
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]
}

# Prints the first problem with the resolved settings (defaults applied,
# SOURCE_MODE and BRIDGE_HOST normalized), as fixed text, and fails;
# prints nothing and succeeds when every value is accepted. SOURCE_MODE
# is checked first. Arguments: SOURCE_MODE, BRIDGE_HOST,
# BRIDGE_IMAP_PORT, BRIDGE_CERT_HOST, PROTON_FOLDER_EXCLUSIONS,
# PROTON_IMAP_ACCOUNT_NAME, PROTON_REMOTE_STORE_NAME,
# PROTON_LOCAL_STORE_NAME, PROTON_CHANNEL_NAME.
proton_settings_problem() {
    local i
    local names=(PROTON_IMAP_ACCOUNT_NAME PROTON_REMOTE_STORE_NAME PROTON_LOCAL_STORE_NAME
        PROTON_CHANNEL_NAME)
    local values=("$6" "$7" "$8" "$9")

    if [[ "$1" != "$PROTON_DEFAULT_SOURCE_MODE" ]]; then
        printf 'SOURCE_MODE must be %s (the default when unset or empty), the only source mbsync supports.\n' \
            "$PROTON_DEFAULT_SOURCE_MODE"
        return 1
    fi
    if ! proton_bridge_host_allowed "$2"; then
        printf 'BRIDGE_HOST must be one of %s (the default is %s): mbsync reaches the Proton Mail Bridge app on this Mac only, through a name that forwards to its 127.0.0.1. A Bridge elsewhere waits on the decision in #1499.\n' \
            "${PROTON_BRIDGE_HOST_ALIASES[*]}" "$PROTON_DEFAULT_BRIDGE_HOST"
        return 1
    fi
    if ! proton_port_allowed "$3"; then
        printf 'BRIDGE_IMAP_PORT must be a port number from 1 to 65535, without leading zeros (the default is %s).\n' \
            "$PROTON_DEFAULT_BRIDGE_IMAP_PORT"
        return 1
    fi
    if ! proton_cert_host_allowed "$4"; then
        printf 'BRIDGE_CERT_HOST must be 127.0.0.1 (the default), localhost, another 127.x.y.z address without leading zeros, or ::1: the name of a certificate for this Mac'"'"'s loopback. Any other name waits on the decision in #1499.\n'
        return 1
    fi
    if ! proton_folder_exclusions_allowed "$5"; then
        printf 'PROTON_FOLDER_EXCLUSIONS must be folder patterns separated by |, none empty and none with a space at either end, written with A-Z, a-z, 0-9, spaces, . / _ * %% & + , ( ) @ - and non-ASCII characters only.\n'
        return 1
    fi
    if ! proton_folder_exclusions_protected "$5"; then
        printf 'PROTON_FOLDER_EXCLUSIONS must keep All Mail, Labels/* and Starred among its patterns: mbsync never syncs these folders, which hold further copies of messages (labels come from message metadata, #1313).\n'
        return 1
    fi
    for ((i = 0; i < ${#names[@]}; i++)); do
        if ! proton_name_allowed "${values[i]}"; then
            printf '%s must be 1 to 64 characters from A-Z, a-z, 0-9, _ and -, starting with a letter or digit.\n' \
                "${names[i]}"
            return 1
        fi
    done
    if [[ "$7" == "$8" ]]; then
        printf 'PROTON_REMOTE_STORE_NAME and PROTON_LOCAL_STORE_NAME must differ.\n'
        return 1
    fi
}
