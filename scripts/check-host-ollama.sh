#!/bin/bash
set -Eeuo pipefail

# Preflight check for host-Ollama mode. Three failure surfaces:
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
#   3. Application Firewall not closing the LAN exposure that the wildcard
#      bind opens. AGENTS.md treats the wildcard bind as safe only when
#      paired with global firewall on, stealth mode on, and a binary-level
#      block on the Ollama binary. Catch any of those being off via
#      `socketfilterfw`.
#
# See docs/setup.md "Optional: native (host) Ollama on macOS" for the
# LaunchAgent layout that produces a wildcard bind and the firewall
# steps that close the LAN exposure.

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

# macOS Application Firewall verification. AGENTS.md treats the wildcard
# bind as safe only when paired with three layers: global firewall on,
# stealth mode on, and a binary-level block on the Ollama binary
# (loopback and the OrbStack vmnet bridge bypass the per-binary block, so
# containers still reach Ollama; LAN neighbors do not). Without these,
# *:11434 is an unauthenticated Ollama API exposed to anything that can
# route to this host. Read commands below do not require sudo.
SOCKETFILTERFW="/usr/libexec/ApplicationFirewall/socketfilterfw"
OLLAMA_BIN="/opt/homebrew/bin/ollama"
SETUP_REF='docs/setup.md "Optional: native (host) Ollama on macOS" step 3'

# Skip-with-warning rather than fail-closed when the binary is absent:
# the script must remain usable on non-Darwin hosts (e.g. CI containers
# running compose validation) where there is no Application Firewall to
# verify. Real macOS hosts always ship socketfilterfw.
if [[ ! -x "$SOCKETFILTERFW" ]]; then
    printf 'WARNING: %s not present; skipping macOS firewall verification.\n' \
        "$SOCKETFILTERFW" >&2
    exit 0
fi

if ! "$SOCKETFILTERFW" --getglobalstate 2>/dev/null | grep -qi 'enabled'; then
    printf 'ERROR: macOS Application Firewall is not enabled.\n' >&2
    printf 'See %s.\n' "$SETUP_REF" >&2
    exit 1
fi

if ! "$SOCKETFILTERFW" --getstealthmode 2>/dev/null | grep -qi 'is on'; then
    printf 'ERROR: macOS firewall stealth mode is not enabled.\n' >&2
    printf 'See %s.\n' "$SETUP_REF" >&2
    exit 1
fi

if ! "$SOCKETFILTERFW" --getappblocked "$OLLAMA_BIN" 2>/dev/null \
    | grep -qi 'is blocked'; then
    printf 'ERROR: %s is not blocked by the Application Firewall.\n' \
        "$OLLAMA_BIN" >&2
    printf 'A brew upgrade can move the binary path and silently void the rule.\n' >&2
    printf 'See %s.\n' "$SETUP_REF" >&2
    exit 1
fi
