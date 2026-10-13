"""Account registry configuration (#1596, PLAN.md decision 47).

The index records the account it serves: one ``accounts`` row holding
the operator's ``ACCOUNT_SCOPE`` (a canonical lower-case UUID kept in
``.env``, not a secret and not a Proton identifier) and the provider
``SOURCE_MODE`` selects. A fresh index stores the row with its initial
schema; an existing one binds it in the v14 migration, through the
connection-local SQL functions ``register_account_functions`` adds. On
every open the indexer (``Database``) and mcp-server
(``mcp-server/src/lib/account.py``) require exactly one stored account,
equal to the configured pair, and otherwise refuse to start, as for the
embedder identity record.

Every refusal is fixed text naming the setting and the fix, never the
value. ``SOURCE_MODE`` follows ``mbsync/proton-settings.sh``: surrounding
whitespace removed, ASCII lower case, empty means ``proton-bridge``, the
only accepted value; ``tests/test_account.py`` runs the shell helper on
the same table, so the two cannot drift.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# The Maildir root the indexer reads when ``MAILDIR_PATH`` is unset;
# ``main.MAILDIR_PATH`` and the maintenance commands share it.
DEFAULT_MAILDIR_PATH = "/maildir"

DEFAULT_SOURCE_MODE = "proton-bridge"
SOURCE_MODES = frozenset({DEFAULT_SOURCE_MODE})

# A canonical UUID as ``uuid.UUID(...).__str__`` writes it: lower case,
# hyphenated, nothing around it.
_ACCOUNT_SCOPE_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII
)

# The bytes Bash's ``[:space:]`` matches under ``LC_ALL=C``, which
# ``proton_normalize_source_mode`` strips.
_C_SPACE = " \t\n\v\f\r"
_ASCII_UPPER_TO_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

ACCOUNT_SCOPE_MISSING = (
    "ACCOUNT_SCOPE is not set. Run make init-secrets, which writes a new one to .env "
    "when none is there, or restore the value this index was built with; see "
    'docs/setup.md and docs/troubleshooting.md "Account scope mismatch".'
)
ACCOUNT_SCOPE_MALFORMED = (
    "ACCOUNT_SCOPE must be a UUID in canonical lower-case form "
    "(8-4-4-4-12 hexadecimal digits). Restore the value this index was built "
    'with; see docs/troubleshooting.md "Account scope mismatch".'
)
SOURCE_MODE_REFUSED = (
    f"SOURCE_MODE must be {DEFAULT_SOURCE_MODE} (the default when unset or empty), "
    "the only source mbsync supports."
)

_REMEDY = (
    "A different ACCOUNT_SCOPE or SOURCE_MODE is a different account, never a rename: "
    "restore the values this index was built with, or rebuild the index from Maildir; "
    'see docs/troubleshooting.md "Account scope mismatch".'
)
ACCOUNT_NONE_STORED = (
    "The index holds no account record, so it cannot be matched with ACCOUNT_SCOPE. "
    "Rebuild the index from Maildir; see docs/troubleshooting.md "
    '"Account scope mismatch".'
)
ACCOUNT_SEVERAL_STORED = (
    "The index records more than one account; this release serves exactly one. "
    "Rebuild the index from Maildir; see docs/troubleshooting.md "
    '"Account scope mismatch".'
)
ACCOUNT_SCOPE_DIFFERS = (
    "ACCOUNT_SCOPE differs from the account this index was built for. " + _REMEDY
)
ACCOUNT_PROVIDER_DIFFERS = (
    "SOURCE_MODE differs from the provider of the account this index was built for. " + _REMEDY
)


class AccountConfigError(RuntimeError):
    """``ACCOUNT_SCOPE`` or ``SOURCE_MODE`` is refused. Fixed text only."""


class AccountMismatchError(RuntimeError):
    """The index's account record is not the configured account. Fixed
    text only: neither the stored nor the configured value."""


@dataclass(frozen=True)
class AccountConfig:
    """The configured account: its scope UUID, its provider (the
    normalized ``SOURCE_MODE``) and its Maildir root, against which
    occurrence locators are relative."""

    scope_uuid: str
    provider: str
    maildir_root: Path


def normalize_source_mode(raw: str) -> str:
    """``SOURCE_MODE`` as compared, the rule of ``mbsync/entrypoint.sh``
    and ``scripts/validate-env.sh``: an empty value takes the default,
    then ``proton_normalize_source_mode`` removes surrounding C-locale
    whitespace and lower-cases ASCII letters (so a value of spaces only
    is refused, not defaulted)."""
    return (raw or DEFAULT_SOURCE_MODE).strip(_C_SPACE).translate(_ASCII_UPPER_TO_LOWER)


def validate_source_mode(raw: str) -> str:
    """The normalized provider, or ``AccountConfigError`` (fixed text)."""
    provider = normalize_source_mode(raw)
    if provider not in SOURCE_MODES:
        raise AccountConfigError(SOURCE_MODE_REFUSED)
    return provider


def validate_account_scope(raw: str) -> str:
    """``raw`` when it is a canonical lower-case UUID, compared exactly
    (no trimming: the scope is an identity), else ``AccountConfigError``
    with fixed text."""
    if not raw:
        raise AccountConfigError(ACCOUNT_SCOPE_MISSING)
    if _ACCOUNT_SCOPE_RE.fullmatch(raw) is None:
        raise AccountConfigError(ACCOUNT_SCOPE_MALFORMED)
    return raw


def account_config_from_env(environ: Mapping[str, str], *, maildir_root: Path) -> AccountConfig:
    """The configured account, validated: ``SOURCE_MODE`` first (as at
    every other site), then ``ACCOUNT_SCOPE``. ``maildir_root`` is the
    Maildir root the caller reads (``MAILDIR_PATH``)."""
    provider = validate_source_mode(environ.get("SOURCE_MODE", ""))
    scope = validate_account_scope(environ.get("ACCOUNT_SCOPE", ""))
    return AccountConfig(scope_uuid=scope, provider=provider, maildir_root=maildir_root)


def maildir_root_from_env(environ: Mapping[str, str]) -> Path:
    """``MAILDIR_PATH``, or the default the indexer uses, made absolute
    against the working directory with ``.`` and ``..`` segments folded:
    occurrence locators are relative to an absolute root with no parent
    segment (``occurrences.locate``), and the Maildir walk yields paths
    under it."""
    return Path(os.path.abspath(environ.get("MAILDIR_PATH", DEFAULT_MAILDIR_PATH)))


def register_account_functions(conn: sqlite3.Connection, account: AccountConfig) -> None:
    """Expose the configured account to SQL on ``conn`` only, for the v14
    migration's binding: ``pmai_account_scope()`` and
    ``pmai_account_provider()``. Connection-local, so nothing persists."""
    conn.create_function("pmai_account_scope", 0, lambda: account.scope_uuid, deterministic=True)
    conn.create_function("pmai_account_provider", 0, lambda: account.provider, deterministic=True)


def verified_account_id(rows: list[tuple[int, str, str]], account: AccountConfig) -> int:
    """The id of the one stored account ``(id, scope_uuid, provider)``,
    when it is the configured one; ``AccountMismatchError`` otherwise."""
    if not rows:
        raise AccountMismatchError(ACCOUNT_NONE_STORED)
    if len(rows) > 1:
        raise AccountMismatchError(ACCOUNT_SEVERAL_STORED)
    account_id, scope_uuid, provider = rows[0]
    if scope_uuid != account.scope_uuid:
        raise AccountMismatchError(ACCOUNT_SCOPE_DIFFERS)
    if provider != account.provider:
        raise AccountMismatchError(ACCOUNT_PROVIDER_DIFFERS)
    return account_id
