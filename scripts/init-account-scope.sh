#!/bin/bash
set -Eeuo pipefail

# Write ACCOUNT_SCOPE to .env when it is missing or empty (#1596); run by
# `make init-secrets`, which `make up` runs first. The index records the
# value with SOURCE_MODE, and the indexer and mcp-server refuse to start
# on a different one, so an existing value is never replaced, valid or
# not (scripts/validate-env.sh refuses a malformed one). A value exported
# in the calling shell is what Compose uses, so nothing is written then.
# Runs on the host (Bash 3.2 on macOS).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly ENV_FILE="${ROOT_DIR}/.env"

if [[ -n "${ACCOUNT_SCOPE+set}" ]]; then
    printf '  ACCOUNT_SCOPE is set in the environment, which Compose uses instead of .env; not writing one\n'
    exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
    printf 'ERROR: .env not found at %s; create it with cp .env.example .env, then run make init-secrets again.\n' "$ENV_FILE" >&2
    exit 1
fi

# The value of an ACCOUNT_SCOPE= line: surrounding spaces and one pair of
# quotes removed.
line_value() {
    local value="${1#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "$value"
}

# Compose reads the last ACCOUNT_SCOPE= line. It is written only when
# every such line is empty, so no value is ever replaced.
found=0
last=""
any_value=0
while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*ACCOUNT_SCOPE= ]]; then
        found=1
        last="$(line_value "$line")"
        [[ -z "$last" ]] || any_value=1
    fi
done <"$ENV_FILE"
if [[ -n "$last" ]]; then
    printf '  ACCOUNT_SCOPE already set in .env, skipping\n'
    exit 0
fi
if ((any_value)); then
    printf 'ERROR: .env has more than one ACCOUNT_SCOPE line and the last is empty; keep only the line with the value this index was built with.\n' >&2
    exit 1
fi

# A random (version 4) UUID in canonical lower-case form.
hex="$(openssl rand -hex 16)"
[[ "$hex" =~ ^[0-9a-f]{32}$ ]] || {
    printf 'ERROR: openssl rand did not return 16 random bytes; ACCOUNT_SCOPE not written.\n' >&2
    exit 1
}
variant="$(printf '%x' $((0x${hex:16:1} & 0x3 | 0x8)))"
scope="${hex:0:8}-${hex:8:4}-4${hex:13:3}-${variant}${hex:17:3}-${hex:20:12}"

if ((found)); then
    # Fill the empty ACCOUNT_SCOPE= line(s) in a copy that keeps .env's
    # mode, then rename it over .env, so an interruption leaves .env whole.
    tmp="$(mktemp "${ROOT_DIR}/.env.account-scope.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    cp -p "$ENV_FILE" "$tmp"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*ACCOUNT_SCOPE= ]]; then
            printf 'ACCOUNT_SCOPE=%s\n' "$scope"
        else
            printf '%s\n' "$line"
        fi
    done <"$ENV_FILE" >"$tmp"
    mv -f "$tmp" "$ENV_FILE"
    trap - EXIT
else
    # Appended after a newline when the file does not end with one.
    if [[ -s "$ENV_FILE" && -n "$(tail -c 1 "$ENV_FILE")" ]]; then
        printf '\n' >>"$ENV_FILE"
    fi
    printf 'ACCOUNT_SCOPE=%s\n' "$scope" >>"$ENV_FILE"
fi
printf '  wrote a new ACCOUNT_SCOPE to .env (the account the index is registered to; keep it with .env)\n'
