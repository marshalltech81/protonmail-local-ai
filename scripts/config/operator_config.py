"""Operator configuration: config/config.toml, schema version 1 (#1607).

The owner decision of 2026-10-12 on #1353 approves a versioned TOML
operator configuration with a ``[global]`` table and ``[[accounts]]``
entries. This module is its host-side translator: it reads the file with
a bounded read, validates it (unknown keys, types, ranges, account roots,
duplicate UUIDs, a non-empty Proton ``include``) with fixed-text
refusals that never repeat a value, and writes the runtime projections:

- ``compose.env``: today's environment settings, the ones ``.env`` holds,
  for Compose to interpolate into ``docker-compose.yml``;
- ``compose.accounts.yml``: one hardened collector per enabled account,
  each with only its own password secret, volumes and network;
- ``config/identity.toml`` and ``config/authority.toml``: the indexer's
  existing classification inputs, when configured.

Nothing reads these outputs yet: the launcher that selects TOML mode is
#1603. ``MAPPING`` lists every ``.env.example`` setting with its TOML key,
default, validation, consumer and restart requirement, and
``TOML_ONLY`` and ``RUNTIME_OWNED`` the rest.

Python 3.11 or later, standard library only (``tomllib``).

Usage: python3 scripts/config/operator_config.py translate
           [--config config/config.toml] [--out .generated]
"""

from __future__ import annotations

import argparse
import dataclasses
import decimal
import email.utils
import json
import math
import os
import re
import stat
import subprocess  # nosec B404 - runs bash on the repository's own settings file, fixed argv
import sys
import tomllib
import unicodedata
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.toml"
DEFAULT_OUT = REPO_ROOT / ".generated"
PROTON_SETTINGS = REPO_ROOT / "mbsync" / "proton-settings.sh"

# The whole file is read at most this far: the authority rules alone may
# take 1 MiB (indexer/src/entities.py), and each account's identity 64 KiB.
MAX_CONFIG_BYTES = 4 * 1024 * 1024
# Accounts are validated one by one, each with a bash run of the Proton
# settings check, so their number is bounded too.
MAX_ACCOUNTS = 32
# The largest value scripts/validate-env.sh's shell arithmetic reads (it
# wraps past it), and short enough for str() under Python's digit limit.
MAX_NUMBER = 2**63 - 1
# The indexer's own limits (indexer/src/entities.py), checked here on the
# files this module renders, so a configuration the indexer would refuse
# is refused before it is written. The indexer still checks them.
AUTHORITY_CLASSES = ("counsel", "management", "vendor", "government", "personal", "other")
AUTHORITY_RULES_MAX_BYTES = 1024 * 1024
AUTHORITY_RULES_MAX_PATTERNS = 10_000
IDENTITY_MAX_BYTES = 64 * 1024
IDENTITY_MAX_ADDRESSES = 1_000

LEGACY_ROOT = "/maildir"
# The mbsync image's user (mbsync/Dockerfile), named on every collector
# docker-compose.yml does not define.
MBSYNC_USER = "1001:1001"
# Credentials with a fixed place, which no configured secret path may name:
# the MCP bearer token (and the header file an older setup left), read by
# the client helpers and mounted in mcp-server only.
RESERVED_SECRET_FILES = (".secrets/mcp_auth_token.txt", ".secrets/mcp_client_headers.txt")
# AGENTS.md's other sensitive files and this module's own outputs (under
# the default --out), and private-key files: never a key or password.
RESERVED_DATA_FILES = (
    ".env",
    "config/config.toml",
    "config/identity.toml",
    "config/authority.toml",
    ".generated/compose.env",
    ".generated/compose.accounts.yml",
    ".generated/config/identity.toml",
    ".generated/config/authority.toml",
)
PRIVATE_KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
# Each role's conventional file (make init-secrets, docs/setup.md), refused
# in the other role even when its own role is configured elsewhere.
PASSWORD_SECRET_FILES = (".secrets/bridge_pass.txt",)
MODEL_KEY_SECRET_FILES = (
    ".secrets/embed_api_key.txt",
    ".secrets/inference_api_key.txt",
    ".secrets/rerank_api_key.txt",
)
# .env.example's placeholder, which scripts/validate-env.sh refuses too.
PLACEHOLDER_USERNAME = "your@proton.me"
PROTON = "proton-bridge"
IMAP = "imap"
# mbsync/proton-settings.sh PROTON_PROTECTED_FOLDER_EXCLUSIONS, which
# refuses a value without them; the default exclusion list.
PROTON_PROTECTED_EXCLUSIONS = ("All Mail", "Labels/*", "Starred")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A key is named in a refusal only when it looks like a schema key; any
# other key text could be private (an address written as a key).
_KEY_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ConfigError(ValueError):
    """A refusal. Its text is fixed: key paths and limits, never a value."""


# --- Schema -----------------------------------------------------------------

_REQUIRED: Any = object()  # no default: the key must be present


@dataclasses.dataclass(frozen=True)
class Leaf:
    """One key's type and range. ``check`` returns the accepted value."""

    default: Any = _REQUIRED

    def check(self, value: Any, path: str, base: Path) -> Any:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class Integer(Leaf):
    minimum: int = 0
    maximum: int | None = None

    def check(self, value: Any, path: str, base: Path) -> Any:
        upper = f" to {self.maximum}" if self.maximum is not None else " or more"
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{path} must be an integer from {self.minimum}{upper}.")
        if value < self.minimum or (self.maximum is not None and value > self.maximum):
            raise ConfigError(f"{path} must be an integer from {self.minimum}{upper}.")
        if value > MAX_NUMBER:
            raise ConfigError(f"{path} must be at most {MAX_NUMBER}.")
        return value

    def describe(self) -> str:
        if self.maximum is not None:
            return f"integer {self.minimum} to {self.maximum}"
        return f"integer >= {self.minimum}"


def _unsigned_zero(value: int | float) -> int | float:
    """-0.0 as 0.0: it passes a lower bound of 0, but its projection
    "-0.0" is not a form scripts/validate-env.sh accepts."""
    return 0.0 if isinstance(value, float) and value == 0 else value


@dataclasses.dataclass(frozen=True)
class Number(Leaf):
    """A finite number, integer or float, read by the services with float()."""

    minimum: int = 0

    def check(self, value: Any, path: str, base: Path) -> Any:
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or (isinstance(value, float) and not math.isfinite(value))
            or value < self.minimum
        ):
            raise ConfigError(f"{path} must be a finite number of at least {self.minimum}.")
        if value > MAX_NUMBER:
            raise ConfigError(f"{path} must be at most {MAX_NUMBER}.")
        return _unsigned_zero(value)

    def describe(self) -> str:
        return f"number >= {self.minimum}"


@dataclasses.dataclass(frozen=True)
class Fraction(Leaf):
    def check(self, value: Any, path: str, base: Path) -> Any:
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or (isinstance(value, float) and not math.isfinite(value))
            or not 0 <= value <= 1
        ):
            raise ConfigError(f"{path} must be a number from 0 to 1.")
        return _unsigned_zero(value)

    def describe(self) -> str:
        return "number 0 to 1"


@dataclasses.dataclass(frozen=True)
class Boolean(Leaf):
    def check(self, value: Any, path: str, base: Path) -> Any:
        if not isinstance(value, bool):
            raise ConfigError(f"{path} must be true or false.")
        return value

    def describe(self) -> str:
        return "true or false"


@dataclasses.dataclass(frozen=True)
class Choice(Leaf):
    choices: tuple[str, ...] = ()

    def check(self, value: Any, path: str, base: Path) -> Any:
        if not isinstance(value, str) or value not in self.choices:
            raise ConfigError(f"{path} must be one of: {', '.join(self.choices)}.")
        return value

    def describe(self) -> str:
        return "one of " + ", ".join(self.choices)


@dataclasses.dataclass(frozen=True)
class Text(Leaf):
    """A string of 1 to ``max_chars`` characters without control characters.

    ``projected`` strings are written into compose.env inside single
    quotes, so they cannot hold one. ``no_pipe`` strings are joined with
    ``|`` (Proton's folder exclusions)."""

    max_chars: int = 1024
    projected: bool = True
    no_pipe: bool = False

    def check(self, value: Any, path: str, base: Path) -> Any:
        if not isinstance(value, str) or not 1 <= len(value) <= self.max_chars:
            raise ConfigError(f"{path} must be text of 1 to {self.max_chars} characters.")
        if _CONTROL_RE.search(value):
            raise ConfigError(f"{path} must not hold control characters or line breaks.")
        # The services read values trimmed: a blank one is a missing one.
        if not value.strip():
            raise ConfigError(f"{path} must not be blank.")
        if self.projected and "'" in value:
            raise ConfigError(f"{path} must not hold a single quote (').")
        if self.no_pipe and "|" in value:
            raise ConfigError(f"{path} must not hold |.")
        return value

    def describe(self) -> str:
        return "text"


@dataclasses.dataclass(frozen=True)
class Uuid(Leaf):
    def check(self, value: Any, path: str, base: Path) -> Any:
        if not isinstance(value, str) or not UUID_RE.fullmatch(value):
            raise ConfigError(
                f"{path} must be a UUID in canonical lower-case form (8-4-4-4-12 hexadecimal digits)."
            )
        return value

    def describe(self) -> str:
        return "UUID, canonical lower case"


@dataclasses.dataclass(frozen=True)
class FilePath(Leaf):
    """A host file path, relative to the configuration file's directory."""

    def check(self, value: Any, path: str, base: Path) -> Any:
        Text(max_chars=4096, projected=False).check(value, path, base)
        return Path(os.path.normpath(base / value))

    def describe(self) -> str:
        return "path, relative to config/"


@dataclasses.dataclass(frozen=True)
class TextList(Leaf):
    element: Text = Text()
    max_items: int = 1000
    min_items: int = 0

    def check(self, value: Any, path: str, base: Path) -> Any:
        if not isinstance(value, list):
            raise ConfigError(f"{path} must be a list of text.")
        if not self.min_items <= len(value) <= self.max_items:
            raise ConfigError(f"{path} must hold {self.min_items} to {self.max_items} entries.")
        return [self.element.check(item, f"{path}[{i}]", base) for i, item in enumerate(value)]

    def describe(self) -> str:
        return f"list of text, at most {self.max_items}"


@dataclasses.dataclass(frozen=True)
class Table:
    """A TOML table. An ``optional`` table that is absent reads as None
    (unconfigured); any other absent table takes its keys' defaults."""

    fields: Mapping[str, Leaf | Table]
    optional: bool = False


def _model_layer(mode: Leaf, key_file: str, extra: Mapping[str, Leaf]) -> Table:
    return Table(
        {
            "mode": mode,
            "base_url": Text(default=None),
            "model": Text(default=None),
            "api_key_file": FilePath(default=key_file),
            **extra,
        }
    )


_AUTHORITY_CLASS = Table(
    {
        "domains": TextList(
            element=Text(projected=False), max_items=AUTHORITY_RULES_MAX_PATTERNS, default=None
        ),
        "addresses": TextList(
            element=Text(projected=False), max_items=AUTHORITY_RULES_MAX_PATTERNS, default=None
        ),
    },
    optional=True,
)

GLOBAL = Table(
    {
        "embedding": _model_layer(
            Choice(choices=("openai",), default="openai"),
            "../.secrets/embed_api_key.txt",
            {
                "timeout_seconds": Number(minimum=1, default=60),
                "warmup_timeout_seconds": Number(minimum=1, default=600),
                "batch_size": Integer(minimum=1, default=64),
                "concurrency": Integer(minimum=1, default=1),
            },
        ),
        "inference": _model_layer(
            Choice(choices=("anthropic", "openai", "none"), default="none"),
            "../.secrets/inference_api_key.txt",
            {
                "timeout_seconds": Number(minimum=1, default=300),
                "max_tokens": Integer(minimum=1, default=None),
                "context_tokens": Integer(minimum=1, default=None),
                "structured_output": Boolean(default=True),
            },
        ),
        "reranking": _model_layer(
            Choice(choices=("cohere", "none"), default="none"),
            "../.secrets/rerank_api_key.txt",
            {
                "candidates": Integer(minimum=1, default=20),
                "timeout_seconds": Number(minimum=1, default=60),
            },
        ),
        "indexer": Table(
            {
                "initial_batch_size": Integer(minimum=1, default=50),
                "steady_state_batch_size": Integer(minimum=1, default=8),
                "parse_max_bytes": Integer(minimum=0, default=50000000),
                "message_timeout_seconds": Integer(minimum=0, default=3600),
                "wal_checkpoint_interval_seconds": Integer(minimum=60, default=600),
                "recovery_sweep_interval_seconds": Integer(minimum=60, default=1800),
                "retry": Table(
                    {
                        "max_attempts": Integer(minimum=1, default=5),
                        "base_seconds": Integer(minimum=1, default=30),
                    }
                ),
                "chunking": Table(
                    {
                        "target_tokens": Integer(minimum=1, default=1000),
                        "max_tokens": Integer(minimum=1, default=1500),
                        "overlap_tokens": Integer(minimum=0, default=150),
                    }
                ),
                "deletion": Table(
                    {
                        "enabled": Boolean(default=True),
                        "grace_days": Integer(minimum=0, default=7),
                        "sweep_interval_seconds": Integer(minimum=60, default=3600),
                        "max_batch_fraction": Fraction(default=0.05),
                    }
                ),
                "attachments": Table(
                    {
                        "enabled": Boolean(default=True),
                        "max_bytes": Integer(minimum=1, default=33554432),
                        "max_extracted_chars": Integer(minimum=0, default=2000000),
                        "pdf_max_digital_pages": Integer(minimum=0, default=500),
                        "ocr": Table(
                            {
                                "enabled": Boolean(default=True),
                                "max_pages": Integer(minimum=1, default=20),
                                "timeout_seconds": Integer(minimum=0, default=60),
                            }
                        ),
                    }
                ),
            }
        ),
        "mcp": Table(
            {
                "port": Integer(minimum=1, maximum=65535, default=3000),
                "session_idle_timeout_seconds": Number(minimum=1, default=1800),
                "experimental_tools": Boolean(default=False),
            }
        ),
        "classification": Table(
            {
                "authority": Table(
                    {name: _AUTHORITY_CLASS for name in AUTHORITY_CLASSES}, optional=True
                ),
            }
        ),
    }
)

# Provider-dependent defaults (connection, exclusions) are None here and
# filled in by _account_defaults once the provider is known.
ACCOUNT = Table(
    {
        "scope_uuid": Uuid(),
        "name": Text(max_chars=64, projected=False),
        "enabled": Boolean(default=True),
        "provider": Choice(choices=(PROTON, IMAP)),
        "maildir_root": Text(max_chars=64, projected=False),
        "password_file": FilePath(),
        "connection": Table(
            {
                "host": Text(max_chars=253, default=None),
                "port": Integer(minimum=1, maximum=65535, default=None),
                "username": Text(),
            }
        ),
        "sync": Table(
            {
                "interval_seconds": Integer(minimum=1, default=60),
                # Nine digits at most, so mbsync's healthcheck arithmetic
                # cannot overflow (mbsync/entrypoint.sh).
                "deadline_seconds": Integer(minimum=1, maximum=999999999, default=86400),
                "imap_account_name": Text(max_chars=64, default="protonmail"),
                "remote_store_name": Text(max_chars=64, default="protonmail-remote"),
                "local_store_name": Text(max_chars=64, default="protonmail-local"),
                "channel_name": Text(max_chars=64, default="protonmail"),
                "mailboxes": Table(
                    {
                        "include": TextList(
                            element=Text(no_pipe=True), max_items=256, default=None
                        ),
                        "exclude": TextList(
                            element=Text(no_pipe=True), max_items=256, default=None
                        ),
                    }
                ),
            }
        ),
        "identity": Table(
            {
                "addresses": TextList(
                    element=Text(projected=False),
                    max_items=IDENTITY_MAX_ADDRESSES,
                    min_items=1,
                ),
            },
            optional=True,
        ),
        "proton_bridge": Table(
            {
                "certificate_host": Text(max_chars=253, default="127.0.0.1"),
                "certificate_fingerprint": Text(max_chars=256),
                "declared_address_scope": Choice(
                    choices=("combined", "split", "unverified"), default="unverified"
                ),
            },
            optional=True,
        ),
        "imap": Table(
            {
                "folder_roles": Table(
                    {
                        role: Text(max_chars=1024, projected=False, default=None)
                        for role in ("trash", "sent", "drafts", "spam")
                    }
                ),
            },
            optional=True,
        ),
    }
)


def _key(path: str, key: object) -> str:
    """``path.key`` when the key looks like a schema key, else a fixed
    placeholder: a key could be private text."""
    shown = key if isinstance(key, str) and _KEY_NAME_RE.fullmatch(key) else "<a key>"
    return f"{path}.{shown}" if path else str(shown)


def _check_table(data: Any, spec: Table, path: str, base: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a table.")
    for key in data:
        if key not in spec.fields:
            raise ConfigError(f"{_key(path, key)} is not a known key.")
    result: dict[str, Any] = {}
    for key, field in spec.fields.items():
        child = _key(path, key)
        if isinstance(field, Table):
            if key in data:
                result[key] = _check_table(data[key], field, child, base)
            elif field.optional:
                result[key] = None
            else:
                result[key] = _check_table({}, field, child, base)
        elif key in data:
            result[key] = field.check(data[key], child, base)
        elif field.default is _REQUIRED:
            raise ConfigError(f"{child} is required.")
        elif isinstance(field, FilePath) and field.default is not None:
            result[key] = field.check(field.default, child, base)
        else:
            result[key] = field.default
    return result


# --- Reading and validation --------------------------------------------------


def read_bounded(handle: Any, limit: int) -> bytes:
    """At most ``limit`` + 1 bytes from ``handle``: one more than allowed
    tells an oversized file apart without reading the rest of it."""
    data: bytes = handle.read(limit + 1)
    return data


def load_file(path: Path) -> tuple[dict[str, Any], Path]:
    """The parsed file and its directory. The file must be a regular file
    readable by its owner only, at most MAX_CONFIG_BYTES, and TOML."""
    name = path.name
    # Checked before opening too: opening a FIFO would wait for a writer.
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ConfigError(f"{name} is not a regular file.")
        handle = path.open("rb")
    except FileNotFoundError:
        raise ConfigError(f"{name} does not exist.") from None
    except OSError as exc:
        raise ConfigError(f"{name} could not be opened ({type(exc).__name__}).") from None
    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{name} is not a regular file.")
        if info.st_mode & 0o077:
            raise ConfigError(
                f"{name} must be readable by its owner only (chmod 600): it holds addresses."
            )
        raw = read_bounded(handle, MAX_CONFIG_BYTES)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{name} is larger than {MAX_CONFIG_BYTES} bytes.")
    # Absolute, so the paths read relative to it are absolute too.
    return parse(raw, name), Path(os.path.abspath(path)).parent


def parse(raw: bytes, name: str = "config.toml") -> dict[str, Any]:
    """TOML from bytes. A decoding error names at most a line and column:
    tomllib's own message can quote the text (an invalid escape)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{name} is not UTF-8 text.") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        found = re.search(r"\(at line (\d+), column (\d+)\)", str(exc))
        where = f" (line {found.group(1)}, column {found.group(2)})" if found else ""
        raise ConfigError(f"{name} is not valid TOML{where}.") from None
    except RecursionError:
        raise ConfigError(f"{name} nests arrays or tables too deeply.") from None
    except ValueError:
        # Not a TOMLDecodeError: int() past Python's 4300-digit limit.
        raise ConfigError(f"{name} holds a number too long to read.") from None


def validate(data: dict[str, Any], base: Path) -> dict[str, Any]:
    """The validated configuration with defaults filled in. ``base`` is
    the directory relative paths are read from."""
    for key in data:
        if key not in ("schema_version", "global", "accounts"):
            raise ConfigError(f"{_key('', key)} is not a known key.")
    version = data.get("schema_version")
    if version is None:
        raise ConfigError(
            f"schema_version is required; this translator reads version {SCHEMA_VERSION}."
        )
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ConfigError(
            f"schema_version must be {SCHEMA_VERSION}, the only version this translator reads."
        )
    if "global" not in data:
        raise ConfigError("global is required.")
    config_global = _check_table(data["global"], GLOBAL, "global", base)
    _check_model_layers(config_global)
    roots = _roots(base)
    for layer in ("embedding", "inference", "reranking"):
        key_file = config_global[layer]["api_key_file"]
        _check_not_reserved(key_file, f"global.{layer}.api_key_file", roots)
        _check_not_conventional(
            key_file,
            f"global.{layer}.api_key_file",
            PASSWORD_SECRET_FILES,
            "the Bridge password's",
            roots,
        )
    _check_inference_budget(config_global["inference"])
    _check_chunking(config_global["indexer"]["chunking"])
    _check_authority(config_global["classification"]["authority"])

    if "accounts" not in data:
        raise ConfigError("accounts is required: at least one [[accounts]] table.")
    accounts = data["accounts"]
    if not isinstance(accounts, list) or not accounts:
        raise ConfigError("accounts must be one or more [[accounts]] tables.")
    if len(accounts) > MAX_ACCOUNTS:
        raise ConfigError(f"accounts may hold at most {MAX_ACCOUNTS} tables.")
    checked = []
    for i, account in enumerate(accounts):
        path = f"accounts[{i}]"
        result = _check_table(account, ACCOUNT, path, base)
        _account_defaults(result, path)
        _check_identity(result["identity"], path)
        checked.append(result)
    _check_accounts(checked, config_global, roots)
    return {"schema_version": version, "global": config_global, "accounts": checked}


def _check_model_layers(config_global: dict[str, Any]) -> None:
    """An enabled layer names its endpoint and model (the required-vars
    contract, AGENTS.md): embedding always, inference and reranking unless
    their mode is none. base_url is a URL or ``default``, in the form
    scripts/validate-env.sh requires (_check_base_url)."""
    for layer in ("embedding", "inference", "reranking"):
        table = config_global[layer]
        if table["base_url"] is not None:
            _check_url_userinfo(f"global.{layer}.base_url", table["base_url"])
        if table["mode"] == "none":
            continue
        for key in ("base_url", "model"):
            if table[key] is None:
                raise ConfigError(
                    f"global.{layer}.{key} is required when global.{layer}.mode is {table['mode']}"
                    + (
                        " (base_url: the provider's URL, or default for the SDK's own endpoint)."
                        if key == "base_url"
                        else "."
                    )
                )
        _check_base_url(layer, table)


@dataclasses.dataclass(frozen=True)
class _FileKey:
    """A secret path as compared with others: the device and inode of an
    existing file (links, hard links and a case-insensitive filesystem's
    other spellings open the same file), and the resolved path, case
    folded, for a file not created yet."""

    spelling: str
    identity: tuple[int, int] | None

    @classmethod
    def of(cls, path: Path) -> _FileKey:
        real = os.path.realpath(path)
        try:
            info = os.stat(real)
            identity: tuple[int, int] | None = (info.st_dev, info.st_ino)
        except OSError:
            identity = None
        return cls(unicodedata.normalize("NFC", real).casefold(), identity)

    def same(self, other: _FileKey) -> bool:
        return self.spelling == other.spelling or (
            self.identity is not None and self.identity == other.identity
        )


def _any_same(key: _FileKey, others: list[_FileKey]) -> bool:
    return any(key.same(other) for other in others)


def _roots(base: Path) -> tuple[Path, ...]:
    """Where the fixed and conventional files are: this checkout, and the
    directory above the configuration's (its own .secrets/ and config/)."""
    return (REPO_ROOT, base.parent)


def _named(names: tuple[str, ...], roots: tuple[Path, ...]) -> list[_FileKey]:
    return [_FileKey.of(root / name) for root in roots for name in names]


def _check_not_reserved(path: Path, key: str, roots: tuple[Path, ...]) -> None:
    if _any_same(_FileKey.of(path), _named(RESERVED_SECRET_FILES, roots)):
        raise ConfigError(
            f"{key} is a reserved credential file ({', '.join(RESERVED_SECRET_FILES)}): "
            "each secret goes only where it belongs."
        )
    suffixes = {Path(p).suffix.lower() for p in (path, os.path.realpath(path))}
    if suffixes & set(PRIVATE_KEY_SUFFIXES) or _any_same(
        _FileKey.of(path), _named(RESERVED_DATA_FILES, roots)
    ):
        raise ConfigError(
            f"{key} is a configuration, data or private-key file "
            f"({', '.join(RESERVED_DATA_FILES)}, or "
            f"{', '.join('*' + x for x in PRIVATE_KEY_SUFFIXES)}): "
            "each secret goes only where it belongs."
        )


def _check_not_conventional(
    path: Path, key: str, names: tuple[str, ...], role: str, roots: tuple[Path, ...]
) -> None:
    if _any_same(_FileKey.of(path), _named(names, roots)):
        raise ConfigError(
            f"{key} is {role} file ({', '.join(names)}): each secret goes only where it belongs."
        )


def _check_url_userinfo(path: str, value: str) -> None:
    """mcp-server's _reject_url_userinfo, which reads every *_BASE_URL,
    whatever the layer's mode: urlsplit must accept it and its netloc
    hold no @. A disabled layer's value is projected too."""
    try:
        netloc = urllib.parse.urlsplit(value).netloc
    except ValueError:
        raise ConfigError(f"{path} is not a valid URL.") from None
    if "@" in netloc:
        raise ConfigError(
            f"{path} must not embed credentials (user:pass@host): put the key in the layer's api_key_file."
        )


def _check_base_url(layer: str, table: dict[str, Any]) -> None:
    """The form scripts/validate-env.sh requires of an enabled layer's
    base_url: ``default`` (any case), or an http(s) URL without userinfo,
    and for anthropic not ending in /v1."""
    path = f"global.{layer}.base_url"
    value = table["base_url"].strip()
    if value.lower() == "default":
        return
    if not re.match(r"https?://", value):
        raise ConfigError(f"{path} must be default or start with http:// or https://.")
    if re.match(r"https?://[^/]*@", value):
        raise ConfigError(
            f"{path} must not embed credentials (user:pass@host): put the key in the layer's api_key_file."
        )
    if table["mode"] == "anthropic" and value.rstrip("/").endswith("/v1"):
        raise ConfigError(
            f"{path} must not end with /v1 when global.{layer}.mode is anthropic: "
            "the SDK appends /v1/messages itself."
        )


# mcp-server's per-mode defaults (#764) and the reserve PromptBudget keeps
# (mcp-server/src/lib/inference.py), as scripts/validate-env.sh checks them.
_DEFAULT_TOKENS = {"anthropic": (16000, 48000), "openai": (1024, 32768)}
_PROMPT_RESERVE = 1088


def _check_inference_budget(inference: dict[str, Any]) -> None:
    if inference["mode"] == "none":
        return
    default_max, default_context = _DEFAULT_TOKENS[inference["mode"]]
    max_tokens = inference["max_tokens"] if inference["max_tokens"] is not None else default_max
    context = (
        inference["context_tokens"] if inference["context_tokens"] is not None else default_context
    )
    if context < max_tokens + _PROMPT_RESERVE:
        raise ConfigError(
            f"global.inference.context_tokens ({default_context} when unset) must exceed "
            f"global.inference.max_tokens ({default_max} when unset) "
            f"by at least {_PROMPT_RESERVE}."
        )


def _check_chunking(chunking: dict[str, Any]) -> None:
    """chunker.chunk_message's rules, which the indexer checks at startup."""
    path = "global.indexer.chunking"
    if chunking["target_tokens"] > chunking["max_tokens"]:
        raise ConfigError(f"{path}.target_tokens must be at most {path}.max_tokens.")
    if chunking["overlap_tokens"] >= chunking["target_tokens"]:
        raise ConfigError(f"{path}.overlap_tokens must be less than {path}.target_tokens.")


# indexer/src/entities.py's address and domain rules (load_authority_rules,
# load_operator_identity, canonical_addr in indexer/src/parser.py),
# mirrored on the values this module renders; the indexer still checks.
# indexer/tests/test_operator_config_parity.py compares them on a catalogue.
_MAX_DOMAIN_CHARS = 253
_MAX_DOMAIN_LABELS = 16
_LDH_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_BARE_DOMAIN_TEXT = (
    "must be a bare domain: dot-separated labels of letters, digits and hyphens, "
    f"at most {_MAX_DOMAIN_LABELS} labels and {_MAX_DOMAIN_CHARS} characters."
)


def _is_bare_domain(value: str) -> bool:
    if not value or len(value) > _MAX_DOMAIN_CHARS:
        return False
    labels = value.split(".")
    return len(labels) <= _MAX_DOMAIN_LABELS and all(
        _LDH_LABEL_RE.fullmatch(label) for label in labels
    )


def _is_bare_address(value: str) -> bool:
    """canonical_addr(value) == value and the domain is bare."""
    try:
        parsed = email.utils.parseaddr(value)[1].strip().lower()
    except Exception:  # canonical_addr reads any failure as no address
        return False
    _, at, domain = value.rpartition("@")
    return "@" in parsed and parsed == value and bool(at) and _is_bare_domain(domain)


def _check_identity(identity: dict[str, Any] | None, path: str) -> None:
    if identity is None:
        return
    seen: set[str] = set()
    for i, entry in enumerate(identity["addresses"]):
        address = entry.strip().lower()
        where = f"{path}.identity.addresses[{i}]"
        if not _is_bare_address(address) or address.startswith("@"):
            raise ConfigError(f"{where} must be a bare address (name@domain).")
        if address in seen:
            raise ConfigError(
                f"{where} repeats an earlier address (compared trimmed and in lower case)."
            )
        seen.add(address)


def _check_authority(authority: dict[str, Any] | None) -> None:
    if authority is None:
        return
    patterns = sum(
        len(table[key] or [])
        for table in authority.values()
        if table is not None
        for key in ("domains", "addresses")
    )
    if patterns > AUTHORITY_RULES_MAX_PATTERNS:
        raise ConfigError(
            f"global.classification.authority may hold at most {AUTHORITY_RULES_MAX_PATTERNS} patterns."
        )
    # In the rendered file's order; addresses and domains are unique each.
    seen: dict[str, set[str]] = {"domains": set(), "addresses": set()}
    for cls in AUTHORITY_CLASSES:
        table = authority[cls]
        if table is None:
            continue
        for key in ("domains", "addresses"):
            for i, entry in enumerate(table[key] or []):
                pattern = entry.strip().lower()
                where = f"global.classification.authority.{cls}.{key}[{i}]"
                if key == "domains" and not _is_bare_domain(pattern):
                    raise ConfigError(f"{where} {_BARE_DOMAIN_TEXT}")
                if key == "addresses" and not _is_bare_address(pattern):
                    raise ConfigError(f"{where} must be a bare address (name@domain).")
                if pattern in seen[key]:
                    raise ConfigError(
                        f"{where} repeats an earlier pattern (compared trimmed and in lower case)."
                    )
                seen[key].add(pattern)


def _account_defaults(account: dict[str, Any], path: str) -> None:
    """Fill in the provider-dependent defaults and check the provider
    table: exactly the one table the provider names."""
    provider = account["provider"]
    own, other = ("proton_bridge", "imap") if provider == PROTON else ("imap", "proton_bridge")
    if account[own] is None:
        raise ConfigError(f"{path}.{own} is required when {path}.provider is {provider}.")
    if account[other] is not None:
        raise ConfigError(f"{path}.{other} must not be set when {path}.provider is {provider}.")
    connection = account["connection"]
    mailboxes = account["sync"]["mailboxes"]
    if provider == PROTON:
        if connection["host"] is None:
            connection["host"] = "host.docker.internal"
        if connection["port"] is None:
            connection["port"] = 1143
        if mailboxes["include"]:
            raise ConfigError(
                f"{path}.sync.mailboxes.include must be empty for provider {PROTON}: "
                "Proton accounts sync every folder except their exclusions."
            )
        mailboxes["include"] = []
        if mailboxes["exclude"] is None:
            mailboxes["exclude"] = list(PROTON_PROTECTED_EXCLUSIONS)
    else:
        for key in ("host", "port"):
            if connection[key] is None:
                raise ConfigError(
                    f"{path}.connection.{key} is required when {path}.provider is {IMAP}."
                )
        mailboxes["include"] = mailboxes["include"] or []
        mailboxes["exclude"] = mailboxes["exclude"] or []


def _check_accounts(
    accounts: list[dict[str, Any]], config_global: dict[str, Any], roots: tuple[Path, ...]
) -> None:
    seen_uuids: set[str] = set()
    legacy = []
    key_files = [
        _FileKey.of(config_global[layer]["api_key_file"])
        for layer in ("embedding", "inference", "reranking")
    ]
    password_files: list[_FileKey] = []
    for i, account in enumerate(accounts):
        path = f"accounts[{i}]"
        uuid = account["scope_uuid"]
        if uuid in seen_uuids:
            raise ConfigError(
                f"{path}.scope_uuid repeats an earlier account's: each account has its own UUID."
            )
        seen_uuids.add(uuid)
        root = account["maildir_root"]
        if root == LEGACY_ROOT:
            legacy.append(i)
        elif root != f"/accounts/{uuid}":
            raise ConfigError(
                f"{path}.maildir_root must be {LEGACY_ROOT} (one account at most) or "
                f"/accounts/ followed by {path}.scope_uuid."
            )
        if len(legacy) > 1:
            raise ConfigError(f"{path}.maildir_root: only one account may use {LEGACY_ROOT}.")
        if account["connection"]["username"].strip() == PLACEHOLDER_USERNAME:
            raise ConfigError(
                f"{path}.connection.username must be the username from the Bridge app's IMAP "
                "details, not the example's placeholder."
            )
        _check_not_reserved(account["password_file"], f"{path}.password_file", roots)
        password = _FileKey.of(account["password_file"])
        if _any_same(password, password_files + key_files):
            raise ConfigError(
                f"{path}.password_file is another account's password file or a model API key file: "
                "each account has its own secret."
            )
        _check_not_conventional(
            account["password_file"],
            f"{path}.password_file",
            MODEL_KEY_SECRET_FILES,
            "a model API key's",
            roots,
        )
        password_files.append(password)
        if account["provider"] == IMAP:
            if account["enabled"]:
                raise ConfigError(
                    f"{path}: provider {IMAP} is defined but cannot be enabled yet (#1606); set enabled = false."
                )
            continue
        _check_proton_account(account, path)


def proton_settings_problem(args: list[str]) -> str | None:
    """mbsync/proton-settings.sh's proton_settings_problem on ``args``: the
    one implementation scripts/validate-env.sh and mbsync's entrypoint
    run (PLAN.md decision 46). Its text is fixed and never repeats a
    value. None when every value is accepted."""
    try:
        # The values go on stdin, NUL-separated (Text refuses control
        # characters): other local accounts can read process arguments,
        # and folder names are mailbox metadata.
        result = subprocess.run(  # nosec B603 B607 - fixed program and script, values on stdin
            [
                "bash",
                "-c",
                'source "$1"; values=(); while IFS= read -r -d "" v; do values+=("$v"); done; '
                'proton_settings_problem "${values[@]}"',
                "bash",
                str(PROTON_SETTINGS),
            ],
            input="".join(f"{arg}\0" for arg in args),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(
            f"The Proton settings check (bash, mbsync/proton-settings.sh) could not run ({type(exc).__name__})."
        ) from None
    if result.returncode == 0:
        return None
    problem = result.stdout.strip()
    return problem or "the Proton settings check failed without a reason."


def _check_proton_account(account: dict[str, Any], path: str) -> None:
    proton = account["proton_bridge"]
    fingerprint = proton["certificate_fingerprint"].rsplit("=", 1)[-1].replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ConfigError(
            f"{path}.proton_bridge.certificate_fingerprint must be a SHA-256 fingerprint: "
            "64 hex digits, with or without colons."
        )
    sync = account["sync"]
    problem = proton_settings_problem(
        [
            account["provider"],
            account["connection"]["host"].strip().lower(),
            str(account["connection"]["port"]),
            proton["certificate_host"],
            "|".join(sync["mailboxes"]["exclude"]),
            sync["imap_account_name"],
            sync["remote_store_name"],
            sync["local_store_name"],
            sync["channel_name"],
        ]
    )
    if problem is not None:
        raise ConfigError(
            f"{path}: {problem} (mbsync/proton-settings.sh; the TOML keys are in docs/setup.md)."
        )


# --- The mapping ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Setting:
    """One ``.env.example`` setting. ``kind`` is ``global`` or
    ``account`` (a TOML key projects it) or ``runtime`` (fixed by the
    project)."""

    env: str
    kind: str
    toml: str | None
    consumer: str
    restart: str
    note: str = ""


_RECREATE_MBSYNC = "recreate mbsync"
_RECREATE_INDEXER = "recreate indexer"
_RECREATE_MCP = "recreate mcp-server"
_RECREATE_BOTH = "recreate indexer and mcp-server"
_EMBEDDER = (
    "recreate indexer and mcp-server; on an existing index it must match the recorded embedder"
)

# One row per setting, kept on one line each to read as a table.
# fmt: off
MAPPING: tuple[Setting, ...] = (
    Setting("SOURCE_MODE", "account", "accounts[].provider", "mbsync, indexer, mcp-server", "recreate all three; recorded in the index with the UUID"),
    Setting("ACCOUNT_SCOPE", "account", "accounts[].scope_uuid", "indexer, mcp-server", "never changed: a different UUID is a different account (#1596)"),
    Setting("BRIDGE_USER", "account", "accounts[].connection.username", "mbsync (mbsyncrc User)", _RECREATE_MBSYNC),
    Setting("BRIDGE_IMAP_PORT", "account", "accounts[].connection.port", "mbsync (Tunnel, probes)", _RECREATE_MBSYNC, "proton-settings.sh"),
    Setting("BRIDGE_CERT_FINGERPRINT", "account", "accounts[].proton_bridge.certificate_fingerprint", "mbsync (certificate check)", _RECREATE_MBSYNC, "64 hex digits"),
    Setting("BRIDGE_HOST", "account", "accounts[].connection.host", "mbsync (Tunnel, probes)", _RECREATE_MBSYNC, "proton-settings.sh allowlist"),
    Setting("BRIDGE_CERT_HOST", "account", "accounts[].proton_bridge.certificate_host", "mbsync (mbsyncrc Host)", _RECREATE_MBSYNC, "proton-settings.sh loopback names"),
    Setting("PROTON_IMAP_ACCOUNT_NAME", "account", "accounts[].sync.imap_account_name", "mbsync (mbsyncrc)", _RECREATE_MBSYNC, "proton-settings.sh names"),
    Setting("PROTON_REMOTE_STORE_NAME", "account", "accounts[].sync.remote_store_name", "mbsync (mbsyncrc, output filter)", _RECREATE_MBSYNC, "proton-settings.sh names"),
    Setting("PROTON_LOCAL_STORE_NAME", "account", "accounts[].sync.local_store_name", "mbsync (mbsyncrc)", _RECREATE_MBSYNC, "proton-settings.sh names"),
    Setting("PROTON_CHANNEL_NAME", "account", "accounts[].sync.channel_name", "mbsync (mbsyncrc, output filter)", _RECREATE_MBSYNC, "proton-settings.sh names"),
    Setting("PROTON_FOLDER_EXCLUSIONS", "account", "accounts[].sync.mailboxes.exclude", "mbsync (mbsyncrc Patterns)", "recreate mbsync; mail already synced stays", "joined with |; must keep All Mail, Labels/*, Starred"),
    Setting("SYNC_INTERVAL", "account", "accounts[].sync.interval_seconds", "mbsync (entrypoint, healthcheck)", _RECREATE_MBSYNC),
    Setting("SYNC_DEADLINE_SECONDS", "account", "accounts[].sync.deadline_seconds", "mbsync (entrypoint, healthcheck)", _RECREATE_MBSYNC),
    Setting("EMBED_BASE_URL", "global", "global.embedding.base_url", "indexer, mcp-server", _EMBEDDER, "URL or default"),
    Setting("EMBED_MODEL", "global", "global.embedding.model", "indexer, mcp-server", _EMBEDDER),
    Setting("EMBED_MODE", "global", "global.embedding.mode", "indexer, mcp-server", _RECREATE_BOTH),
    Setting("EMBED_TIMEOUT_SECS", "global", "global.embedding.timeout_seconds", "mcp-server", _RECREATE_MCP),
    Setting("EMBED_BATCH_SIZE", "global", "global.embedding.batch_size", "indexer", _RECREATE_INDEXER),
    Setting("EMBED_CONCURRENCY", "global", "global.embedding.concurrency", "indexer", _RECREATE_INDEXER),
    Setting("INITIAL_INDEX_BATCH_SIZE", "global", "global.indexer.initial_batch_size", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_STEADY_STATE_BATCH_SIZE", "global", "global.indexer.steady_state_batch_size", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_WAL_CHECKPOINT_INTERVAL_SECS", "global", "global.indexer.wal_checkpoint_interval_seconds", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_RECOVERY_SWEEP_INTERVAL_SECS", "global", "global.indexer.recovery_sweep_interval_seconds", "indexer", _RECREATE_INDEXER),
    Setting("RERANK_MODE", "global", "global.reranking.mode", "mcp-server", _RECREATE_MCP),
    Setting("RERANK_BASE_URL", "global", "global.reranking.base_url", "mcp-server", _RECREATE_MCP, "URL or default; required when enabled"),
    Setting("RERANK_MODEL", "global", "global.reranking.model", "mcp-server", _RECREATE_MCP, "required when enabled"),
    Setting("RERANK_CANDIDATES", "global", "global.reranking.candidates", "mcp-server", _RECREATE_MCP),
    Setting("RERANK_TIMEOUT_SECS", "global", "global.reranking.timeout_seconds", "mcp-server", _RECREATE_MCP),
    Setting("EMBED_WARMUP_TIMEOUT_SECS", "global", "global.embedding.warmup_timeout_seconds", "indexer", _RECREATE_INDEXER),
    Setting("INFERENCE_MODE", "global", "global.inference.mode", "mcp-server", _RECREATE_MCP),
    Setting("INFERENCE_BASE_URL", "global", "global.inference.base_url", "mcp-server", _RECREATE_MCP, "URL or default; required when enabled"),
    Setting("INFERENCE_MODEL", "global", "global.inference.model", "mcp-server", _RECREATE_MCP, "required when enabled"),
    Setting("INFERENCE_TIMEOUT_SECS", "global", "global.inference.timeout_seconds", "mcp-server", _RECREATE_MCP),
    Setting("INFERENCE_MAX_TOKENS", "global", "global.inference.max_tokens", "mcp-server", _RECREATE_MCP, "unset: the mode's default"),
    Setting("INFERENCE_CONTEXT_TOKENS", "global", "global.inference.context_tokens", "mcp-server", _RECREATE_MCP, "unset: the mode's default"),
    Setting("INFERENCE_STRUCTURED_OUTPUT", "global", "global.inference.structured_output", "mcp-server", _RECREATE_MCP),
    Setting("INDEXER_DELETION_ENABLED", "global", "global.indexer.deletion.enabled", "indexer (reconciler)", _RECREATE_INDEXER),
    Setting("INDEXER_DELETION_GRACE_DAYS", "global", "global.indexer.deletion.grace_days", "indexer (reconciler)", _RECREATE_INDEXER),
    Setting("INDEXER_DELETION_SWEEP_INTERVAL_SECS", "global", "global.indexer.deletion.sweep_interval_seconds", "indexer (reconciler)", _RECREATE_INDEXER),
    Setting("INDEXER_DELETION_MAX_BATCH_PCT", "global", "global.indexer.deletion.max_batch_fraction", "indexer (reconciler)", _RECREATE_INDEXER),
    Setting("INDEXER_MAX_ATTEMPTS", "global", "global.indexer.retry.max_attempts", "indexer (queue)", _RECREATE_INDEXER),
    Setting("INDEXER_RETRY_BASE_SECONDS", "global", "global.indexer.retry.base_seconds", "indexer (queue)", _RECREATE_INDEXER),
    Setting("INDEXER_MESSAGE_TIMEOUT_SECONDS", "global", "global.indexer.message_timeout_seconds", "indexer", _RECREATE_INDEXER, "0 disables"),
    Setting("INDEXER_CHUNK_TARGET_TOKENS", "global", "global.indexer.chunking.target_tokens", "indexer", _RECREATE_INDEXER, "<= max_tokens"),
    Setting("INDEXER_CHUNK_MAX_TOKENS", "global", "global.indexer.chunking.max_tokens", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_CHUNK_OVERLAP_TOKENS", "global", "global.indexer.chunking.overlap_tokens", "indexer", _RECREATE_INDEXER, "< target_tokens"),
    Setting("INDEXER_PARSE_MAX_BYTES", "global", "global.indexer.parse_max_bytes", "indexer", _RECREATE_INDEXER, "0 disables"),
    Setting("INDEXER_ATTACHMENT_EXTRACTION_ENABLED", "global", "global.indexer.attachments.enabled", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_OCR_ENABLED", "global", "global.indexer.attachments.ocr.enabled", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_ATTACHMENT_MAX_BYTES", "global", "global.indexer.attachments.max_bytes", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_OCR_MAX_PAGES", "global", "global.indexer.attachments.ocr.max_pages", "indexer", _RECREATE_INDEXER),
    Setting("INDEXER_OCR_TIMEOUT_SECONDS", "global", "global.indexer.attachments.ocr.timeout_seconds", "indexer", _RECREATE_INDEXER, "0 disables"),
    Setting("INDEXER_PDF_MAX_DIGITAL_PAGES", "global", "global.indexer.attachments.pdf_max_digital_pages", "indexer", _RECREATE_INDEXER, "0 disables"),
    Setting("INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", "global", "global.indexer.attachments.max_extracted_chars", "indexer", _RECREATE_INDEXER, "0 disables"),
    Setting("MCP_PORT", "global", "global.mcp.port", "mcp-server and its loopback port mapping", _RECREATE_MCP),
    Setting("MCP_TRANSPORT", "runtime", None, "mcp-server", "-", "fixed: streamable-http"),
    Setting("MCP_SESSION_IDLE_TIMEOUT_SECS", "global", "global.mcp.session_idle_timeout_seconds", "mcp-server", _RECREATE_MCP),
    Setting("MCP_EXPERIMENTAL_TOOLS", "global", "global.mcp.experimental_tools", "mcp-server", _RECREATE_MCP),
)

# Schema keys with no .env.example setting: (TOML key, consumer, restart).
TOML_ONLY: tuple[tuple[str, str, str], ...] = (
    ("accounts[].name", "none: a display name, never an identifier", "-"),
    ("accounts[].enabled", "which accounts get a collector in compose.accounts.yml; disabling keeps the data", "recreate the collector"),
    ("accounts[].maildir_root", f"{LEGACY_ROOT}: today's projection (compose.env); /accounts/<scope_uuid>: its collector only, the indexer mount is #1605", "-"),
    ("accounts[].password_file", "the collector's password secret (bridge_pass)", "recreate the collector"),
    ("accounts[].sync.mailboxes.include", "proton-bridge: must be empty; imap: #1606", "-"),
    ("accounts[].identity.addresses", "config/identity.toml for the indexer (the account at /maildir)", "restart indexer"),
    ("accounts[].proton_bridge.declared_address_scope", "none until #1593", "-"),
    ("accounts[].imap.folder_roles.trash", "none until #1606", "-"),
    ("accounts[].imap.folder_roles.sent", "none until #1606", "-"),
    ("accounts[].imap.folder_roles.drafts", "none until #1606", "-"),
    ("accounts[].imap.folder_roles.spam", "none until #1606", "-"),
    ("global.embedding.api_key_file", "the embed_api_key secret (indexer, mcp-server)", _RECREATE_BOTH),
    ("global.inference.api_key_file", "the inference_api_key secret (mcp-server)", _RECREATE_MCP),
    ("global.reranking.api_key_file", "the rerank_api_key secret (mcp-server)", _RECREATE_MCP),
    *(
        (f"global.classification.authority.{cls}.{key}", "config/authority.toml for the indexer", "restart indexer")
        for cls in AUTHORITY_CLASSES
        for key in ("domains", "addresses")
    ),
)

# fmt: on

# Not configurable: fixed by the project, whatever the configuration.
RUNTIME_OWNED: tuple[tuple[str, str], ...] = (
    ("MCP_TRANSPORT", "Streamable HTTP at /mcp is the only transport (#498)"),
    ("MAILDIR_PATH", "the Maildir mount path, /maildir"),
    ("SQLITE_PATH", "the index mount path, /data/mail.db"),
    ("INDEXER_HEALTH_FILE", "the indexer's healthcheck file on its tmpfs"),
    ("HOME", "the indexer user's home on its tmpfs"),
    ("PATH", "the search path the PDF extractor child sets"),
    ("GIT_COMMIT", "a build argument make build passes"),
    (
        "EMBED_API_KEY",
        "a Docker secret; the variable is a fallback outside containers",
    ),  # pragma: allowlist secret
    (
        "INFERENCE_API_KEY",
        "a Docker secret; the variable is a fallback outside containers",
    ),  # pragma: allowlist secret
    (
        "RERANK_API_KEY",
        "a Docker secret; the variable is a fallback outside containers",
    ),  # pragma: allowlist secret
    (
        "MCP_AUTH_TOKEN",
        ".secrets/mcp_auth_token.txt, a fixed path the client helpers read",
    ),  # pragma: allowlist secret
    (
        "mbsync transport",
        "pull-only sync, Expunge None, Create Near, CopyArrivalDate, SubFolders Legacy, SyncState *, the isync layout exclusions",
    ),
    (
        "Bridge trust",
        "implicit TLS through the Tunnel, the fingerprint check, the persistent pin, the host and certificate-name allowlists",
    ),
    (
        "hardening",
        "networks, volumes, non-root users, read-only root filesystems, capabilities, the loopback-only MCP port",
    ),
)


def _leaf(path: str) -> Leaf:
    """The schema leaf at a MAPPING or TOML_ONLY key."""
    if path.startswith("accounts[]."):
        spec: Table | Leaf = ACCOUNT
        parts = path.removeprefix("accounts[].").split(".")
    else:
        spec = GLOBAL
        parts = path.removeprefix("global.").split(".")
    for part in parts:
        if not isinstance(spec, Table):
            raise KeyError(path)
        spec = spec.fields[part]
    if not isinstance(spec, Leaf):
        raise KeyError(path)
    return spec


# Defaults and validation the leaf alone cannot state: provider-dependent
# defaults, keys required by another key, and cross-key rules.
_DOC_DEFAULTS = {
    "accounts[].connection.host": "host.docker.internal (proton-bridge)",
    "accounts[].connection.port": "1143 (proton-bridge)",
    "accounts[].sync.mailboxes.exclude": "All Mail, Labels/*, Starred (proton-bridge)",
    "accounts[].identity.addresses": "table absent: unconfigured",
    "global.embedding.base_url": "required",
    "global.embedding.model": "required",
    "global.inference.base_url": "required when enabled",
    "global.inference.model": "required when enabled",
    "global.reranking.base_url": "required when enabled",
    "global.reranking.model": "required when enabled",
    "global.inference.max_tokens": "unset: the mode's default",
    "global.inference.context_tokens": "unset: the mode's default",
}
_DOC_VALIDATION = {
    **{
        f"global.classification.authority.{cls}.{key}": (
            f"list of bare {kind}, at most {AUTHORITY_RULES_MAX_PATTERNS}; unique across classes"
        )
        for cls in AUTHORITY_CLASSES
        for key, kind in (("domains", "domains"), ("addresses", "addresses (name@domain)"))
    },
    "accounts[].maildir_root": f"{LEGACY_ROOT} (one account at most) or /accounts/<scope_uuid>",
    "accounts[].password_file": (
        "path, relative to config/; no other account's or model key's, "
        "nor a model key's conventional file"
    ),
    "global.embedding.api_key_file": "path, relative to config/; not .secrets/bridge_pass.txt",  # pragma: allowlist secret
    "global.inference.api_key_file": "path, relative to config/; not .secrets/bridge_pass.txt",  # pragma: allowlist secret
    "global.reranking.api_key_file": "path, relative to config/; not .secrets/bridge_pass.txt",  # pragma: allowlist secret
    "accounts[].scope_uuid": "UUID, canonical lower case; unique",
    "accounts[].provider": "one of proton-bridge, imap (imap cannot be enabled until #1606)",
    "accounts[].sync.mailboxes.include": "list of text; empty for proton-bridge",
    "accounts[].identity.addresses": "list of 1 to 1000 bare addresses (name@domain); unique",
}


def _cell(text: str) -> str:
    """Text for a Markdown table cell: | and * would be read as markup."""
    return text.replace("|", "\\|").replace("*", "\\*")


def _default_text(path: str, leaf: Leaf) -> str:
    if path in _DOC_DEFAULTS:
        return _DOC_DEFAULTS[path]
    if leaf.default is _REQUIRED:
        return "required"
    if leaf.default is None:
        return "unset"
    return _format(leaf.default)


def schema_leaves() -> list[str]:
    """Every leaf key of schema version 1, as MAPPING and TOML_ONLY name them."""
    found: list[str] = []

    def walk(spec: Table, prefix: str) -> None:
        for key, field in spec.fields.items():
            if isinstance(field, Table):
                walk(field, f"{prefix}{key}.")
            else:
                found.append(f"{prefix}{key}")

    walk(GLOBAL, "global.")
    walk(ACCOUNT, "accounts[].")
    return found


def mapping_markdown() -> str:
    """The mapping as the Markdown tables in docs/setup.md."""
    rows = [
        "| `.env.example` setting | TOML key | Default | Validation | Consumer | After a change |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in MAPPING:
        if s.toml is None:
            rows.append(
                f"| `{s.env}` | {_cell(s.note)} | - | - | {_cell(s.consumer)} | {_cell(s.restart)} |"
            )
            continue
        leaf = _leaf(s.toml)
        validation = _DOC_VALIDATION.get(s.toml, leaf.describe()) + (
            f"; {s.note}" if s.note else ""
        )
        cells = (_default_text(s.toml, leaf), validation, s.consumer, s.restart)
        rows.append(f"| `{s.env}` | `{s.toml}` | " + " | ".join(_cell(c) for c in cells) + " |")
    rows += [
        "",
        "| TOML key without a `.env.example` setting | Default | Validation | Consumer | After a change |",
        "| --- | --- | --- | --- | --- |",
    ]
    for path, consumer, restart in TOML_ONLY:
        leaf = _leaf(path)
        cells = (
            _default_text(path, leaf),
            _DOC_VALIDATION.get(path, leaf.describe()),
            consumer,
            restart,
        )
        rows.append(f"| `{path}` | " + " | ".join(_cell(c) for c in cells) + " |")
    rows += [
        "",
        "| Runtime-owned, not configurable | Why |",
        "| --- | --- |",
    ]
    rows += [f"| {_cell(name)} | {_cell(why)} |" for name, why in RUNTIME_OWNED]
    return "\n".join(rows) + "\n"


# --- Projections ------------------------------------------------------------


def _format(value: Any) -> str:
    """A value as the services read it from the environment."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Plain decimal: 1e-05 would fail scripts/validate-env.sh.
        return format(decimal.Decimal(repr(value)), "f")
    if isinstance(value, list):
        return "|".join(value)
    return str(value)


def _lookup(config: dict[str, Any], path: str, account: dict[str, Any]) -> Any:
    if path.startswith("accounts[]."):
        node: Any = account
        parts = path.removeprefix("accounts[].").split(".")
    else:
        node = config["global"]
        parts = path.removeprefix("global.").split(".")
    for part in parts:
        node = node[part]
    return node


def legacy_account(config: dict[str, Any]) -> dict[str, Any]:
    """The enabled account at /maildir, the one today's indexer, mcp-server
    and mbsync service read."""
    accounts: list[dict[str, Any]] = config["accounts"]
    for account in accounts:
        if account["maildir_root"] == LEGACY_ROOT and account["enabled"]:
            return account
    raise ConfigError(
        f'No enabled account has maildir_root = "{LEGACY_ROOT}": today\'s indexer and '
        f"mcp-server read the account at {LEGACY_ROOT} only (#1605)."
    )


def project_env(config: dict[str, Any]) -> dict[str, str]:
    """Every setting a TOML key projects, as ``.env`` would hold it, with
    defaults applied: for the global keys and the account at /maildir."""
    account = legacy_account(config)
    return {
        s.env: _format(_lookup(config, s.toml, account))
        for s in MAPPING
        if s.kind in ("global", "account") and s.toml is not None
    }


def render_env(env: dict[str, str]) -> str:
    """compose.env: each value in single quotes, which Compose reads
    literally and scripts/validate-env.sh strips (Text refuses a quote)."""
    lines = [
        "# Generated by scripts/config/operator_config.py from config/config.toml.",
        "# Do not edit: edit config/config.toml and translate again. Not yet used (#1603).",
    ]
    lines += [f"{key}='{value}'" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def _yaml(value: str) -> str:
    """A YAML double-quoted scalar Compose reads literally: JSON escaping
    is valid YAML, and $ is doubled so Compose does not interpolate it."""
    return json.dumps(value.replace("$", "$$"))


def _relative(path: Path) -> str:
    """A path as Compose reads it: relative to the project directory (the
    repository) when inside it, else absolute."""
    try:
        return "./" + path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


@dataclasses.dataclass(frozen=True)
class Collector:
    """The Compose names of one account's collector. The account at
    /maildir keeps today's names."""

    service: str
    secret: str
    maildir_volume: str
    state_volume: str
    network: str

    @classmethod
    def of(cls, account: dict[str, Any]) -> Collector:
        if account["maildir_root"] == LEGACY_ROOT:
            return cls("mbsync", "bridge_pass", "maildir-volume", "mbsync-state", "bridge-net")
        uuid = account["scope_uuid"]
        return cls(
            f"mbsync-{uuid}",
            f"bridge_pass_{uuid}",
            f"maildir-{uuid}",
            f"mbsync-state-{uuid}",
            f"bridge-net-{uuid}",
        )


def collector_env(account: dict[str, Any]) -> dict[str, str]:
    """The environment of an account's collector: today's mbsync settings,
    from that account only. Pin rotation is a one-run command, not
    configuration (#1609)."""
    return {
        s.env: _format(_lookup({}, s.toml, account))
        for s in MAPPING
        if s.kind == "account" and s.toml is not None and s.env != "ACCOUNT_SCOPE"
    }


def render_compose(config: dict[str, Any]) -> str:
    """compose.accounts.yml: one collector per enabled account, each with
    the hardening of docker-compose.yml's mbsync and only its own secret,
    volumes and network."""
    enabled = [a for a in config["accounts"] if a["enabled"]]
    enabled.sort(key=lambda a: (a["maildir_root"] != LEGACY_ROOT, a["scope_uuid"]))
    out = [
        "# Generated by scripts/config/operator_config.py from config/config.toml.",
        "# Do not edit: edit config/config.toml and translate again. Not yet used (#1603).",
        "services:",
    ]
    for account in enabled:
        names = Collector.of(account)
        legacy = names.service == "mbsync"
        out += [
            f"  {names.service}:",
            "    build:",
            "      context: ./mbsync",
            "      args:",
            "        GIT_COMMIT: ${GIT_COMMIT:-unknown}",
            "    secrets:",
        ]
        # The legacy secret keeps its name, so it mounts where the
        # entrypoint reads it; any other is mounted under that name.
        if legacy:
            out.append(f"      - {names.secret}")
        else:
            out += [f"      - source: {names.secret}", "        target: bridge_pass"]
        out.append(f"    container_name: {names.service}")
        # A service docker-compose.yml does not define names its non-root
        # user (scripts/tests/compose_test.sh, #1608): mbsync's UID and GID
        # (mbsync/Dockerfile).
        if not legacy:
            out.append(f'    user: "{MBSYNC_USER}"')
        out += [
            "    restart: unless-stopped",
            "    init: true",
            "    read_only: true",
            "    security_opt:",
            "      - no-new-privileges:true",
            "    cap_drop:",
            "      - ALL",
            "    pids_limit: 128",
            "    tmpfs:",
            "      - /tmp",
            "    environment:",
        ]
        out += [f"      {key}: {_yaml(value)}" for key, value in collector_env(account).items()]
        out += [
            "    volumes:",
            f"      - {names.maildir_volume}:/maildir",
            f"      - {names.state_volume}:/state",
            "    logging:",
            "      driver: json-file",
            "      options:",
            '        max-size: "10m"',
            '        max-file: "3"',
            "    networks:",
            f"      - {names.network}",
        ]
    out.append("volumes:")
    for account in enabled:
        names = Collector.of(account)
        out += [f"  {names.maildir_volume}: {{}}", f"  {names.state_volume}: {{}}"]
    out.append("networks:")
    for account in enabled:
        out += [f"  {Collector.of(account).network}:", "    driver: bridge"]
    out.append("secrets:")
    for account in enabled:
        names = Collector.of(account)
        out += [f"  {names.secret}:", f"    file: {_yaml(_relative(account['password_file']))}"]
    for layer, secret in (
        ("embedding", "embed_api_key"),
        ("inference", "inference_api_key"),
        ("reranking", "rerank_api_key"),
    ):
        path = config["global"][layer]["api_key_file"]
        out += [f"  {secret}:", f"    file: {_yaml(_relative(path))}"]
    return "\n".join(out) + "\n"


def _toml_list(name: str, items: list[str]) -> list[str]:
    # A JSON string is a valid TOML basic string.
    return [f"{name} = [", *(f"    {json.dumps(item)}," for item in items), "]"]


def render_identity(account: dict[str, Any]) -> str | None:
    """config/identity.toml for the account at /maildir, or None when its
    identity is unconfigured (then the file is absent)."""
    identity = account["identity"]
    if identity is None:
        return None
    text = (
        "\n".join(
            [
                "# Generated by scripts/config/operator_config.py. Do not edit.",
                "",
                *_toml_list("addresses", identity["addresses"]),
            ]
        )
        + "\n"
    )
    if len(text.encode("utf-8")) > IDENTITY_MAX_BYTES:
        raise ConfigError(
            f"The identity of the account at {LEGACY_ROOT} renders larger than {IDENTITY_MAX_BYTES} bytes."
        )
    return text


def render_authority(config: dict[str, Any]) -> str | None:
    """config/authority.toml, or None when no authority rules are configured."""
    authority = config["global"]["classification"]["authority"]
    if authority is None:
        return None
    lines = ["# Generated by scripts/config/operator_config.py. Do not edit."]
    for cls in AUTHORITY_CLASSES:
        table = authority[cls]
        if table is None:
            continue
        lines += ["", f"[{cls}]"]
        for key in ("domains", "addresses"):
            if table[key] is not None:
                lines += _toml_list(key, table[key])
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > AUTHORITY_RULES_MAX_BYTES:
        raise ConfigError(
            f"global.classification.authority renders larger than {AUTHORITY_RULES_MAX_BYTES} bytes."
        )
    return text


def translate(config: dict[str, Any]) -> dict[str, str | None]:
    """Every output by its path under the output directory; None means
    the file must be absent."""
    account = legacy_account(config)
    return {
        "compose.env": render_env(project_env(config)),
        "compose.accounts.yml": render_compose(config),
        "config/identity.toml": render_identity(account),
        "config/authority.toml": render_authority(config),
    }


# --- Writing ----------------------------------------------------------------


def _private_dir(path: Path) -> None:
    """A directory only its owner can use (700), created if needed."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ConfigError(f"{path.name} must be a directory, not a link.")
    os.chmod(path, 0o700)


def _write_atomic(path: Path, text: str) -> None:
    """Write mode 600 under a temporary name, then rename it into place,
    so a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # LF on every platform: the byte limits are checked on LF text.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def check_targets(
    config: dict[str, Any], config_path: Path, out_dir: Path, outputs: dict[str, str | None]
) -> None:
    """No output, written or removed, is the configuration file, a secret
    file it names, or a protected file (the reserved secrets, each role's
    conventional file, AGENTS.md's sensitive data files). --out chooses
    the targets, so this is checked against them, not the defaults."""
    protected = [_FileKey.of(config_path)]
    protected += [
        _FileKey.of(config["global"][layer]["api_key_file"])
        for layer in ("embedding", "inference", "reranking")
    ]
    protected += [_FileKey.of(account["password_file"]) for account in config["accounts"]]
    names: tuple[str, ...] = RESERVED_SECRET_FILES + PASSWORD_SECRET_FILES + MODEL_KEY_SECRET_FILES
    names += tuple(n for n in RESERVED_DATA_FILES if not n.startswith(".generated/"))
    protected += _named(names, _roots(Path(os.path.abspath(config_path)).parent))
    for name in outputs:
        if _any_same(_FileKey.of(out_dir / name), protected):
            raise ConfigError(
                f"--out: {name} would replace the configuration file, a secret file it names "
                "or a protected file; choose another output directory."
            )


def write_outputs(out_dir: Path, outputs: dict[str, str | None]) -> None:
    _private_dir(out_dir)
    _private_dir(out_dir / "config")
    for name, text in outputs.items():
        target = out_dir / name
        if text is None:
            target.unlink(missing_ok=True)
        else:
            _write_atomic(target, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Translate config/config.toml (schema version 1).")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("translate", help="validate config.toml and write the projections")
    command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    command.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        data, base = load_file(args.config)
        config = validate(data, base)
        outputs = translate(config)
        check_targets(config, args.config, args.out, outputs)
        write_outputs(args.out, outputs)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: could not write the projections ({type(exc).__name__}).", file=sys.stderr)
        return 1
    written = sum(1 for text in outputs.values() if text is not None)
    print(f"Wrote {written} files to {args.out} (not yet used: see docs/setup.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
