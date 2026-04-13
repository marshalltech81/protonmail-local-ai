#!/bin/bash
set -Eeuo pipefail

readonly REPO_DIR="${1:-/build}"
readonly CONSTANTS_FILE="${REPO_DIR}/internal/constants/constants.go"
readonly CERTS_FILE="${REPO_DIR}/internal/certs/tls.go"

readonly UPSTREAM_HOST='Host = "127.0.0.1"'
readonly PATCHED_HOST='Host = "0.0.0.0"'
readonly UPSTREAM_CERT='IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},'
readonly PATCHED_CERT='DNSNames:    []string{"protonmail-bridge", "localhost"},'

count_matches() {
    local file="$1"
    local needle="$2"

    grep -F -c -- "$needle" "$file" || true
}

require_count() {
    local file="$1"
    local needle="$2"
    local expected="$3"
    local description="$4"
    local actual

    actual="$(count_matches "$file" "$needle")"
    if [[ "$actual" != "$expected" ]]; then
        printf 'Patch drift detected: expected %s match(es) for %s in %s, found %s.\n' \
            "$expected" "$description" "$file" "$actual" >&2
        exit 1
    fi
}

sed_in_place() {
    local expression="$1"
    local file="$2"

    if sed --version >/dev/null 2>&1; then
        sed -i "$expression" "$file"
    else
        sed -i '' "$expression" "$file"
    fi
}

if [[ ! -f "$CONSTANTS_FILE" || ! -f "$CERTS_FILE" ]]; then
    printf 'Bridge source files not found under %s.\n' "$REPO_DIR" >&2
    exit 1
fi

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "1" "upstream host binding"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "0" "patched host binding before patch"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "1" "upstream TLS SAN source line"
require_count "$CERTS_FILE" "$PATCHED_CERT" "0" "patched TLS SAN line before patch"

sed_in_place 's/Host = "127.0.0.1"/Host = "0.0.0.0"/' "$CONSTANTS_FILE"
sed_in_place \
    's|IPAddresses:           \[\]net\.IP{net\.ParseIP("127\.0\.0\.1")},|IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},\
\t\tDNSNames:    []string{"protonmail-bridge", "localhost"},|' \
    "$CERTS_FILE"

require_count "$CONSTANTS_FILE" "$UPSTREAM_HOST" "0" "upstream host binding after patch"
require_count "$CONSTANTS_FILE" "$PATCHED_HOST" "1" "patched host binding"
require_count "$CERTS_FILE" "$UPSTREAM_CERT" "0" "upstream TLS SAN source line after patch"
require_count "$CERTS_FILE" "$PATCHED_CERT" "1" "patched TLS SAN line"

printf 'Bridge source patches applied cleanly in %s.\n' "$REPO_DIR"
