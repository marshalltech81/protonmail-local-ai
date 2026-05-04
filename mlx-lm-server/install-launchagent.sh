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

# Two transformations are needed before substituting these values
# into the plist:
#
#   1. XML-encode characters that are special in plist string content:
#      ``&``, ``<``, ``>``. macOS allows all three in filesystem paths
#      (rare but legal), and the result is parsed by launchd as XML.
#      Order matters: encode ``&`` first so the ``&`` introduced by
#      ``&lt;`` / ``&gt;`` does not get double-encoded.
#   2. Escape characters that have special meaning in a sed
#      replacement string with ``|`` as the delimiter: ``\``, ``&``
#      (sed expands it to the matched pattern), ``|`` (the delimiter
#      itself). Order also matters: escape ``\`` first so the
#      backslashes introduced for ``&`` and ``|`` are not re-doubled.
xml_encode() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "$s"
}

sed_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/[&|]/\\&/g'
}

REPO_ROOT_SAFE="$(sed_escape "$(xml_encode "${REPO_ROOT}")")"
HOME_SAFE="$(sed_escape "$(xml_encode "${HOME}")")"

sed \
    -e "s|__REPO_ROOT__|${REPO_ROOT_SAFE}|g" \
    -e "s|__USER_HOME__|${HOME_SAFE}|g" \
    "${TEMPLATE}" > "${TARGET}"
chmod 644 "${TARGET}"

# Belt-and-suspenders: fail loudly if any placeholder survived the
# substitution (e.g. a future template grew a placeholder this script
# doesn't know about).
if grep -q '__REPO_ROOT__\|__USER_HOME__' "${TARGET}"; then
    printf 'ERROR: unsubstituted placeholder found in %s — check sed_escape coverage.\n' \
        "${TARGET}" >&2
    exit 1
fi

printf 'Installed: %s\n' "${TARGET}"

# Heredoc keeps the literal ``$(id -u)`` in the printed output so the
# operator can copy-paste each line into their shell — substitution
# happens in their shell, not here. ``${TARGET}`` is double-quoted in
# the printed command so a HOME path containing whitespace stays
# intact when the operator runs ``launchctl bootstrap``.
cat <<EOF

Next steps:
  launchctl bootstrap "gui/\$(id -u)" "${TARGET}"
  launchctl print "gui/\$(id -u)/com.local.mlx-lm-server" | head

To restart after \`uv sync\` rebuilds the venv:
  ./mlx-lm-server/install-launchagent.sh    # regenerate plist
  launchctl kickstart -k "gui/\$(id -u)/com.local.mlx-lm-server"
EOF
