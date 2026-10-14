#!/bin/bash
set -Eeuo pipefail

# Write config/mode=legacy when it is missing (#1603, PLAN.md decision
# 50); run by `make init-config`, which `make up` runs after
# init-secrets. config/mode selects .env (legacy, today's only form) or
# config/config.toml (toml); it is written only by an explicit command,
# so this script never writes "toml" and never replaces an existing
# value. A fresh deployment (no config/config.toml yet) gets "legacy"
# so make up continues to work exactly as before. An existing
# config/config.toml with no config/mode is a configuration this script
# cannot safely default: the operator must choose explicitly with
# `make migrate-config` (to adopt it) or by removing the stray file.
# Runs on the host (Bash 3.2 on macOS).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly MODE_FILE="${ROOT_DIR}/config/mode"
readonly TOML_FILE="${ROOT_DIR}/config/config.toml"

# shellcheck source=scripts/config-mode.sh disable=SC1091 # linted on its own
source "${ROOT_DIR}/scripts/config-mode.sh"

if [[ -f "$MODE_FILE" ]]; then
    printf '  config/mode already set, skipping\n'
    exit 0
fi

if [[ -f "$TOML_FILE" ]]; then
    printf 'ERROR: config/config.toml exists but config/mode does not. Run make migrate-config to adopt it as the active configuration, or remove config/config.toml to continue with the legacy .env form.\n' >&2
    exit 1
fi

mkdir -p "${ROOT_DIR}/config"
write_config_mode "$MODE_FILE" "$CONFIG_MODE_LEGACY"
printf '  wrote config/mode=legacy (the .env form; see docs/setup.md)\n'
