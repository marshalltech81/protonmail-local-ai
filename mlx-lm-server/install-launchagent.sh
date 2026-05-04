#!/bin/bash
set -Eeuo pipefail

# Install the mlx-lm-server LaunchAgent for the current user.
#
# Substitutes the absolute repo path and ``$HOME`` into the vendored
# template, writes the result to ``~/Library/LaunchAgents/``, and prints
# the bootstrap command. Idempotent: safe to re-run after ``uv sync``
# rebuilds the venv (the regenerated plist re-points at the new venv
# absolute path).
#
# Usage:
#   ./mlx-lm-server/install-launchagent.sh
#
# After install:
#   launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.local.mlx-lm-server.plist
#   launchctl print "gui/$(id -u)/com.local.mlx-lm-server" | head

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/com.local.mlx-lm-server.plist.template"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET="${TARGET_DIR}/com.local.mlx-lm-server.plist"

[[ -f "${TEMPLATE}" ]] || {
    printf 'ERROR: template not found at %s\n' "${TEMPLATE}" >&2
    exit 1
}

[[ -x "${REPO_ROOT}/mlx-lm-server/.venv/bin/mlx_lm.server" ]] || {
    printf 'ERROR: mlx_lm.server binary missing at %s\n' \
        "${REPO_ROOT}/mlx-lm-server/.venv/bin/mlx_lm.server" >&2
    printf 'Run: cd mlx-lm-server && uv sync\n' >&2
    exit 1
}

mkdir -p "${TARGET_DIR}"

# sed | placeholder substitution. Use ``|`` as the delimiter because
# REPO_ROOT contains ``/`` characters that would have to be escaped.
sed \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__USER_HOME__|${HOME}|g" \
    "${TEMPLATE}" > "${TARGET}"
chmod 644 "${TARGET}"

printf 'Installed: %s\n' "${TARGET}"

# Heredoc keeps the literal ``$(id -u)`` in the printed output so the
# operator can copy-paste each line into their shell — substitution
# happens in their shell, not here.
cat <<EOF

Next steps:
  launchctl bootstrap "gui/\$(id -u)" ${TARGET}
  launchctl print "gui/\$(id -u)/com.local.mlx-lm-server" | head

To restart after \`uv sync\` rebuilds the venv:
  ./mlx-lm-server/install-launchagent.sh    # regenerate plist
  launchctl kickstart -k "gui/\$(id -u)/com.local.mlx-lm-server"
EOF
