#!/bin/bash
set -Eeuo pipefail

# Preflight check for host-Ollama mode. Two failure surfaces, two checks:
#
#   1. Listener responding on 127.0.0.1:11434 — the operator forgot to
#      bootstrap the LaunchAgent or the brew formula was upgraded and the
#      plist regenerated. Caught by a curl probe.
#
#   2. Listener bound to loopback only — brew's default plist binds
#      `127.0.0.1:11434`, but OrbStack containers reach the host via
#      `host.docker.internal` which only resolves to a non-loopback bind.
#      A loopback-only listener passes the curl probe above and then
#      fails minutes later inside docker compose with a confusing
#      "connection refused". Catch it up front via `lsof`.
#
# See docs/setup.md "Optional: native (host) Ollama on macOS" for the
# LaunchAgent layout that produces a wildcard bind.

if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    printf 'ERROR: native Ollama is not responding on 127.0.0.1:11434.\n' >&2
    printf 'Bootstrap the LaunchAgent per docs/setup.md, or run: brew services start ollama\n' >&2
    exit 1
fi

# `lsof` is part of the macOS base install. Skip-with-warning if it is
# somehow absent rather than fail-closed: the curl probe already proved
# something is listening, and we do not want the bind verification to
# block the whole flow on a tooling gap.
if ! command -v lsof >/dev/null 2>&1; then
    printf 'WARNING: lsof not found; skipping Ollama bind-address verification.\n' >&2
    exit 0
fi

# Only fail when lsof returns rows (we can see the listener) and none of
# them are wildcard-bound. If lsof returns nothing (process visibility
# edge case across user contexts), trust the curl probe and continue.
listens="$(lsof -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$listens" ]] \
    && ! printf '%s\n' "$listens" | grep -qE 'TCP (\*|\[::\]):11434'; then
    printf 'ERROR: Ollama is bound to loopback only.\n' >&2
    printf 'OrbStack containers reach the host via host.docker.internal, which requires *:11434.\n' >&2
    printf 'See docs/setup.md "Optional: native (host) Ollama on macOS" for the LaunchAgent setup.\n' >&2
    exit 1
fi
