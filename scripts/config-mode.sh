#!/bin/bash
# config/mode selects the operator configuration form (#1603, PLAN.md
# decision 50): "legacy" (.env, today's only form) or "toml"
# (config/config.toml, scripts/config/operator_config.py). It is
# written only by an explicit command: `make init-config` writes
# "legacy" for a fresh deployment, `make migrate-config` writes "toml"
# after importing the effective .env settings. There is no fallback in
# either direction: a missing file, an unrecognized value, or a
# configuration that mixes both forms (a stray config/config.toml in
# legacy mode, or a missing one in toml mode) is refused with fixed
# text that never repeats a value.
#
# Sourced by scripts/validate-env.sh and scripts/init-config.sh. Bash
# 3.2 compatible (macOS), standalone like mbsync/proton-settings.sh:
# it does not depend on functions another script defines.

CONFIG_MODE_LEGACY="legacy"
CONFIG_MODE_TOML="toml"

# The file's permission bits as three octal digits, or empty when they
# cannot be read on this platform.
_config_mode_file_perm() {
    local path="$1"
    local mode

    # GNU coreutils (Linux containers) uses -c; BSD (macOS host) uses -f.
    if mode=$(stat -c '%a' "$path" 2>/dev/null); then
        printf '%s' "$mode"
        return 0
    fi
    if mode=$(stat -f '%Lp' "$path" 2>/dev/null); then
        printf '%s' "$mode"
        return 0
    fi
    printf ''
    return 1
}

# MODE_FILE's content, trimmed of surrounding whitespace, or empty when
# the file is absent. Does not validate it: use config_mode_problem first.
read_config_mode() {
    local mode_file="$1"
    local value

    [[ -f "$mode_file" ]] || {
        printf ''
        return 0
    }
    value="$(<"$mode_file")"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

# A fixed-text problem with MODE_FILE and TOML_FILE (config/config.toml),
# or empty when the configuration is consistent and MODE_FILE can be
# trusted. Never repeats a value.
config_mode_problem() {
    local mode_file="$1" toml_file="$2"
    local perm mode

    if [[ ! -f "$mode_file" ]]; then
        printf '%s\n' "config/mode is missing. Run 'make init-config' (a fresh legacy .env deployment) or 'make migrate-config' (to adopt config/config.toml); see docs/setup.md."
        return 0
    fi
    perm="$(_config_mode_file_perm "$mode_file")"
    if [[ -z "$perm" ]]; then
        printf '%s\n' "config/mode: unable to read its file mode on this platform."
        return 0
    fi
    if [[ "$perm" != "600" ]]; then
        printf '%s\n' "config/mode must have mode 600, found $perm."
        return 0
    fi
    mode="$(read_config_mode "$mode_file")"
    case "$mode" in
        "$CONFIG_MODE_LEGACY")
            if [[ -f "$toml_file" ]]; then
                printf '%s\n' "config/mode is legacy but config/config.toml exists: a mixed configuration is refused. Run 'make migrate-config' to adopt it, or remove config/config.toml to stay in legacy mode."
                return 0
            fi
            ;;
        "$CONFIG_MODE_TOML")
            if [[ ! -f "$toml_file" ]]; then
                printf '%s\n' "config/mode is toml but config/config.toml does not exist. Restore it; config/mode is written only by an explicit command, so it is never reset automatically."
                return 0
            fi
            ;;
        *)
            printf '%s\n' "config/mode must be exactly 'legacy' or 'toml'."
            return 0
            ;;
    esac
    printf ''
}

# Write VALUE ("legacy" or "toml") to MODE_FILE: a temporary file in the
# same directory, mode 600, renamed into place so a reader never sees a
# half-written file.
write_config_mode() {
    local mode_file="$1" value="$2"
    local tmp

    tmp="$(mktemp "${mode_file}.XXXXXX")"
    printf '%s\n' "$value" >"$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$mode_file"
}
