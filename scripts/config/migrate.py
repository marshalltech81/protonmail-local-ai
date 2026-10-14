"""`make migrate-config`: import today's .env into config/config.toml and
switch config/mode to toml (#1603, PLAN.md decision 50).

Reads every ``.env`` setting ``operator_config.MAPPING`` maps to a TOML
key through ``scripts/validate-env.sh --get`` (the one implementation
that parses ``.env``, without shell-sourcing it), builds the one
``[[accounts]]`` entry at ``/maildir`` plus ``[global]``, folds in
``config/identity.toml`` and ``config/authority.toml`` when present, and
validates the result with ``operator_config.validate`` before writing
anything. ``ACCOUNT_SCOPE`` is imported exactly as read, never
regenerated: a different UUID is a different account (#1596).

An unset or empty ``.env`` value is left out of the TOML, so the
schema's own default applies (the same default ``.env.example``
documents for that setting); a required value found empty surfaces as
the schema's own "is required" refusal. Password and API-key secrets
stay at their conventional paths: plain ``.env`` had no way to name
another one.

Usage: python3 scripts/config/migrate.py
"""

from __future__ import annotations

import dataclasses
import decimal
import json
import os
import subprocess  # nosec B404 - runs bash on the repository's own validator, fixed argv
import sys
import tomllib
from pathlib import Path
from typing import Any

import operator_config as oc

# The conventional secret paths (config/config.toml.example): plain .env
# had no per-setting way to point a secret elsewhere, so a migrated
# configuration keeps them.
_ACCOUNT_PASSWORD_FILE = "../.secrets/bridge_pass.txt"
_API_KEY_FILES = {
    "embedding": "../.secrets/embed_api_key.txt",
    "inference": "../.secrets/inference_api_key.txt",
    "reranking": "../.secrets/rerank_api_key.txt",
}

# docker-compose.yml's ${INFERENCE_MODEL:-claude-sonnet-5-5}: the one
# setting an empty .env value still defaults, and only in anthropic
# mode (scripts/validate-env.sh). The TOML schema has no model
# default, so the import applies this one explicitly.
_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"


class MigrateError(ValueError):
    """Fixed text: the key, never the value."""


@dataclasses.dataclass(frozen=True)
class Paths:
    """Every file this module reads or writes, under one root so tests
    can run against a temporary directory instead of the real checkout."""

    root: Path

    @property
    def validate_env(self) -> Path:
        return self.root / "scripts" / "validate-env.sh"

    @property
    def identity(self) -> Path:
        return self.root / "config" / "identity.toml"

    @property
    def authority(self) -> Path:
        return self.root / "config" / "authority.toml"

    @property
    def config(self) -> Path:
        return self.root / "config" / "config.toml"

    @property
    def mode(self) -> Path:
        return self.root / "config" / "mode"


def read_env(paths: Paths, key: str) -> str:
    """The effective value of ``key``: a variable exported in the
    invoking shell wins over ``.env``, even when it is empty, exactly
    as Compose and ``scripts/validate-env.sh``'s own ``env_value``
    read it (migrate-config runs as a child of that same shell, so
    ``os.environ`` already sees it). Otherwise, the ``.env`` file, by
    the same parser ``make validate-env`` uses (no shell-sourcing)."""
    if key in os.environ:
        return os.environ[key]
    result = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["bash", str(paths.validate_env), "--get", key],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise MigrateError(
            f"could not read {key} from .env (scripts/validate-env.sh --get): "
            f"{result.stderr.strip() or 'no output'}"
        )
    return result.stdout.rstrip("\n")


def _parse_leaf(leaf: oc.Leaf, key: str, raw: str) -> Any:
    """``raw`` as the TOML type ``leaf`` expects. Raises ``MigrateError``
    naming ``key``, never the value, on a value the leaf's own type
    cannot represent; ``operator_config.validate`` still checks range
    and every other rule once this is assembled."""
    if isinstance(leaf, oc.Boolean):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise MigrateError(f"{key} must be a boolean (true/false, 1/0, yes/no, on/off).")
    if isinstance(leaf, oc.Integer):
        try:
            return int(raw.strip())
        except ValueError:
            raise MigrateError(f"{key} must be an integer.") from None
    if isinstance(leaf, oc.Number | oc.Fraction):
        try:
            return int(raw.strip())
        except ValueError:
            pass
        try:
            return float(raw.strip())
        except ValueError:
            raise MigrateError(f"{key} must be a number.") from None
    if isinstance(leaf, oc.Choice):
        # The legacy validators compare every mode case-insensitively,
        # trimmed (normalize_mode): EMBED_MODE, INFERENCE_MODE,
        # RERANK_MODE and SOURCE_MODE all accept "OpenAI" or padding
        # that the TOML schema's exact-match Choice would otherwise
        # refuse.
        return raw.strip().lower()
    if isinstance(leaf, oc.TextList):
        return [item for item in raw.split("|") if item != ""] if raw else []
    # Text, Choice, Uuid: operator_config.validate checks the exact form.
    return raw


def _set_path(target: dict[str, Any], parts: list[str], value: Any) -> None:
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _import_mapping(
    paths: Paths, target_global: dict[str, Any], target_account: dict[str, Any]
) -> None:
    """Every ``MAPPING`` setting with a non-empty ``.env`` value, typed
    and placed at its TOML path. An unset or empty value is left out, so
    the schema's own default (the same one ``.env.example`` documents)
    applies, except ``SOURCE_MODE``: ``accounts[].provider`` has no TOML
    default (the schema requires it explicitly), while an unset
    ``SOURCE_MODE`` defaults at the shell layer
    (``mbsync/proton-settings.sh``) to ``proton-bridge``; the import
    applies that same default rather than skipping the key."""
    for setting in oc.MAPPING:
        if setting.kind not in ("account", "global") or setting.toml is None:
            continue
        raw = read_env(paths, setting.env)
        if setting.env == "SOURCE_MODE" and not raw.strip():
            # accounts[].provider has no TOML default, unlike the shell
            # layer's own default (mbsync/proton-settings.sh); the
            # Choice branch below still normalizes case and padding.
            raw = oc.PROTON
        elif not raw.strip():
            continue
        leaf = oc._leaf(setting.toml)
        value = _parse_leaf(leaf, setting.env, raw)
        if setting.toml.startswith("accounts[]."):
            _set_path(target_account, setting.toml.removeprefix("accounts[].").split("."), value)
        else:
            _set_path(target_global, setting.toml.removeprefix("global.").split("."), value)


def _import_identity(paths: Paths, account: dict[str, Any]) -> None:
    if not paths.identity.is_file():
        return
    data = tomllib.loads(paths.identity.read_text(encoding="utf-8"))
    if data:
        # The identity Table in operator_config.validate accepts only
        # "addresses": passing the parsed file through unfiltered,
        # rather than selecting "addresses" out of it, makes an
        # unrecognized key (a typo, say) fail closed there instead of
        # being silently dropped.
        account["identity"] = data


def _import_authority(paths: Paths, config_global: dict[str, Any]) -> None:
    if not paths.authority.is_file():
        return
    data = tomllib.loads(paths.authority.read_text(encoding="utf-8"))
    if data:
        # The authority Table in operator_config.validate accepts only
        # the known classes (AUTHORITY_CLASSES): passing the parsed
        # file through unfiltered, rather than selecting recognized
        # classes out of it, makes an unrecognized top-level table (a
        # misspelled or unknown class) fail closed there instead of
        # being silently dropped by a comprehension -- which would
        # otherwise migrate with those classification rules missing
        # and no error.
        config_global.setdefault("classification", {})["authority"] = data


def build_config(paths: Paths) -> dict[str, Any]:
    """The sparse configuration imported from .env (plus identity.toml
    and authority.toml when present), ready for ``operator_config.validate``."""
    target_global: dict[str, Any] = {}
    target_account: dict[str, Any] = {
        "name": "Legacy",
        "enabled": True,
        "maildir_root": oc.LEGACY_ROOT,
        "password_file": _ACCOUNT_PASSWORD_FILE,
    }
    _import_mapping(paths, target_global, target_account)
    for layer, path in _API_KEY_FILES.items():
        target_global.setdefault(layer, {}).setdefault("api_key_file", path)
    inference = target_global.get("inference", {})
    if inference.get("mode") == "anthropic" and "model" not in inference:
        target_global.setdefault("inference", {})["model"] = _ANTHROPIC_DEFAULT_MODEL
    _import_identity(paths, target_account)
    _import_authority(paths, target_global)
    return {"schema_version": 1, "global": target_global, "accounts": [target_account]}


# --- Serialization -----------------------------------------------------------

# The two FilePath leaves .env never had a per-setting way to override;
# the import always keeps the conventional path, never a resolved
# absolute one, so the rendered TOML reads like the example file.
_PATH_LITERALS = {
    "accounts[].password_file": _ACCOUNT_PASSWORD_FILE,
    "global.embedding.api_key_file": _API_KEY_FILES["embedding"],
    "global.inference.api_key_file": _API_KEY_FILES["inference"],
    "global.reranking.api_key_file": _API_KEY_FILES["reranking"],
}


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string: JSON's escaping of control
    characters, backslashes and quotes is also TOML's, but
    ``ensure_ascii`` must be off -- its default escapes a character
    outside the Basic Multilingual Plane (an emoji, say, in a folder
    exclusion) as a UTF-16 surrogate pair, which is valid JSON but not
    a valid TOML escape, so ``tomllib`` would refuse the render."""
    return json.dumps(value, ensure_ascii=False)


def _toml_scalar(leaf: oc.Leaf, path: str, value: Any) -> str:
    if isinstance(leaf, oc.FilePath):
        return _toml_string(_PATH_LITERALS[path])
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(decimal.Decimal(repr(value)), "f")
    return _toml_string(value)


def _emit_table(
    lines: list[str], spec: oc.Table, data: dict[str, Any], prefix: str, header_path: str
) -> None:
    scalars: list[str] = []
    tables: list[tuple[str, oc.Table, Any, str]] = []
    for key, field in spec.fields.items():
        value = data.get(key)
        child_path = f"{header_path}.{key}" if header_path else key
        if isinstance(field, oc.Table):
            tables.append((key, field, value, child_path))
            continue
        if value is None:
            # Unconfigured: omitting the key lets the leaf's own
            # default apply on reload, rather than writing TOML's
            # nonexistent null (and, for a TextList, rather than
            # writing [] -- a real, different, configured value).
            continue
        if isinstance(field, oc.TextList):
            scalars.append(f"{key} = [{', '.join(_toml_string(v) for v in (value or []))}]")
        else:
            scalars.append(f"{key} = {_toml_scalar(field, child_path, value)}")
    lines.extend(scalars)
    for key, field, value, child_path in tables:
        if field.optional and value is None:
            continue
        lines.append("")
        lines.append(f"[{prefix}.{key}]" if prefix else f"[{key}]")
        _emit_table(lines, field, value or {}, f"{prefix}.{key}" if prefix else key, child_path)


def render_toml(config: dict[str, Any]) -> str:
    """``config`` (already validated and defaulted by
    ``operator_config.validate``) as TOML text, in schema order."""
    lines = [
        "# Generated by scripts/config/migrate.py (make migrate-config) from .env.",
        "# Edit directly and run `python3 scripts/config/operator_config.py translate`",
        "# to check it, or `make validate-env` (TOML mode).",
        "",
        f"schema_version = {config['schema_version']}",
    ]
    _emit_table(lines, oc.GLOBAL, config["global"], "global", "global")
    account = config["accounts"][0]
    lines += ["", "[[accounts]]"]
    _emit_table(lines, oc.ACCOUNT, account, "accounts", "accounts[]")
    return "\n".join(lines) + "\n"


def migrate(paths: Paths) -> str:
    """Build, validate, self-check and render the TOML text. Raises
    ``MigrateError`` or ``operator_config.ConfigError`` with fixed text;
    writes nothing."""
    sparse = build_config(paths)
    config = oc.validate(sparse, paths.config.parent)
    oc.check_single_enabled_account(config)
    text = render_toml(config)
    # Round-trip: the rendered file must validate to the same
    # configuration that was imported (same base directory, so the two
    # FilePath leaves resolve identically from the same literal).
    reloaded = oc.validate(tomllib.loads(text), paths.config.parent)
    if reloaded != config:
        raise MigrateError("the rendered config/config.toml does not match what was imported.")
    return text


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _adopt_existing(paths: Paths) -> int:
    """config/config.toml already exists (hand-written from the
    example, or left over): validate it and switch config/mode to
    toml without touching it, rather than importing over it. This is
    also what makes scripts/init-config.sh's "run make migrate-config"
    advice, for a stray config.toml found with no config/mode, actually
    work."""
    try:
        data, base = oc.load_file(paths.config)
        config = oc.validate(data, base)
        oc.check_single_enabled_account(config)
    except oc.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        _write_private(paths.mode, "toml\n")
    except OSError as exc:
        print(f"ERROR: could not write config/mode ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"{paths.config} is valid; set config/mode=toml. Run make validate-env to check it.")
    return 0


def _import_from_env(paths: Paths) -> int:
    try:
        text = migrate(paths)
    except (MigrateError, oc.ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Both files or neither: a failure between them must not leave
    # config/config.toml present with config/mode still absent or
    # legacy, which the next run would then treat as adopting it
    # instead of retrying the import.
    try:
        _write_private(paths.config, text)
    except OSError as exc:
        print(f"ERROR: could not write config/config.toml ({type(exc).__name__}).", file=sys.stderr)
        return 1
    try:
        _write_private(paths.mode, "toml\n")
    except OSError as exc:
        paths.config.unlink(missing_ok=True)
        print(
            f"ERROR: could not write config/mode ({type(exc).__name__}); "
            "rolled back config/config.toml.",
            file=sys.stderr,
        )
        return 1
    print(f"Wrote {paths.config} and set config/mode=toml. Run make validate-env to check it.")
    return 0


def run(paths: Paths) -> int:
    if paths.mode.is_file() and paths.mode.read_text(encoding="utf-8").strip() == "toml":
        print("ERROR: config/mode is already toml; migrate-config runs once.", file=sys.stderr)
        return 1
    if paths.config.exists():
        return _adopt_existing(paths)
    return _import_from_env(paths)


def main(argv: list[str] | None = None) -> int:
    del argv
    return run(Paths(oc.REPO_ROOT))


if __name__ == "__main__":
    sys.exit(main())
