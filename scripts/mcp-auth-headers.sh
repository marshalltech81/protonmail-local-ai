#!/bin/bash
set -Eeuo pipefail

# Print the MCP server's Authorization header as a JSON object, for
# Claude Code's ``headersHelper`` (see docs/setup.md, "Connect an MCP
# client"). The token is read with a redirection and written with the
# printf builtin, so it never appears in any process's arguments, where
# other local accounts could read it from the process list.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TOKEN_FILE="${ROOT_DIR}/.secrets/mcp_auth_token.txt"

token=""
if [[ -f "$TOKEN_FILE" ]]; then
    token="$(<"$TOKEN_FILE")"
fi
# mcp-server strips surrounding whitespace from the secret; match it.
token="${token#"${token%%[![:space:]]*}"}"
token="${token%"${token##*[![:space:]]}"}"

if [[ -z "$token" ]]; then
    printf 'ERROR: MCP bearer token is missing or empty at %s.\n' "$TOKEN_FILE" >&2
    exit 1
fi
# RFC 6750 token characters only ('=' as trailing padding), which also
# keeps the JSON well formed; mcp-server and validate-env.sh apply the
# same set. ``openssl rand -hex 32`` output always passes.
if [[ ! "$token" =~ ^[A-Za-z0-9._~+/-]+=*$ ]]; then
    printf 'ERROR: MCP bearer token in %s has characters outside A-Z a-z 0-9 . _ ~ + / - (and trailing =); regenerate it with openssl rand -hex 32.\n' \
        "$TOKEN_FILE" >&2
    exit 1
fi

printf '{"Authorization": "Bearer %s"}\n' "$token"
