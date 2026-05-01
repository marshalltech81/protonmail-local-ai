#!/bin/bash
set -Eeuo pipefail

# Open WebUI talks to the MCP server over `/mcp` (Streamable HTTP). The
# MCP server's default transport is `sse`, which only serves `/sse`, so
# starting Open WebUI without flipping the transport leaves the UI
# unable to reach MCP. Valid values are `dual` (serves both endpoints)
# or `streamable-http` (serves only `/mcp`).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR

transport="$("$ROOT_DIR/scripts/validate-env.sh" --get MCP_TRANSPORT)"
if [[ "$transport" != "dual" && "$transport" != "streamable-http" ]]; then
    printf 'ERROR: set MCP_TRANSPORT=dual or MCP_TRANSPORT=streamable-http in .env before starting Open WebUI.\n' >&2
    exit 1
fi
