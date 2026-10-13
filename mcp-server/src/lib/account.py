"""Account registry check at startup (#1596, PLAN.md decision 47).

The indexer registers the account the index serves in ``accounts``: the
operator's ``ACCOUNT_SCOPE`` (a canonical lower-case UUID in ``.env``)
and the provider ``SOURCE_MODE`` selects (``indexer/src/account.py``).
mcp-server only reads it: at startup it validates its own configuration
and requires exactly one stored account, equal to the configured pair,
and otherwise refuses to start, as for the embedder identity record. A
missing registry (an index the indexer has not created or migrated yet)
also refuses, and the restart policy retries.

The validation rules, the refusal texts and the comparison mirror the
indexer's copies; ``tests/test_account.py`` in each service runs
``mbsync/proton-settings.sh`` on the same ``SOURCE_MODE`` table. Every
refusal is fixed text, never the value.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .sqlite import Database

log = logging.getLogger("mcp.account")

DEFAULT_SOURCE_MODE = "proton-bridge"
SOURCE_MODES = frozenset({DEFAULT_SOURCE_MODE})

_ACCOUNT_SCOPE_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII
)
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
ACCOUNT_REGISTRY_MISSING = (
    "The index has no account registry yet: the indexer adds it when it creates or "
    "migrates the index. mcp-server exits until then and its restart policy retries."
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
    """The index's account record is missing or not the configured
    account. Fixed text only."""


@dataclass(frozen=True)
class AccountConfig:
    """The configured account: its scope UUID and provider."""

    scope_uuid: str
    provider: str


def normalize_source_mode(raw: str) -> str:
    """``SOURCE_MODE`` as compared: empty takes the default, then
    surrounding C-locale whitespace is removed and ASCII letters are
    lower-cased (``proton_normalize_source_mode``)."""
    return (raw or DEFAULT_SOURCE_MODE).strip(_C_SPACE).translate(_ASCII_UPPER_TO_LOWER)


def account_config_from_env(environ: Mapping[str, str]) -> AccountConfig:
    """The configured account, validated: ``SOURCE_MODE`` first, then
    ``ACCOUNT_SCOPE`` (exactly, no trimming). Raises
    ``AccountConfigError`` with fixed text."""
    provider = normalize_source_mode(environ.get("SOURCE_MODE", ""))
    if provider not in SOURCE_MODES:
        raise AccountConfigError(SOURCE_MODE_REFUSED)
    scope = environ.get("ACCOUNT_SCOPE", "")
    if not scope:
        raise AccountConfigError(ACCOUNT_SCOPE_MISSING)
    if _ACCOUNT_SCOPE_RE.fullmatch(scope) is None:
        raise AccountConfigError(ACCOUNT_SCOPE_MALFORMED)
    return AccountConfig(scope_uuid=scope, provider=provider)


def verify_account(db: Database, account: AccountConfig) -> None:
    """Require the index's one account to be ``account``; raise
    ``AccountMismatchError`` with fixed text otherwise."""
    rows = db.get_accounts()
    if rows is None:
        raise AccountMismatchError(ACCOUNT_REGISTRY_MISSING)
    if not rows:
        raise AccountMismatchError(ACCOUNT_NONE_STORED)
    if len(rows) > 1:
        raise AccountMismatchError(ACCOUNT_SEVERAL_STORED)
    scope_uuid, provider = rows[0]
    if scope_uuid != account.scope_uuid:
        raise AccountMismatchError(ACCOUNT_SCOPE_DIFFERS)
    if provider != account.provider:
        raise AccountMismatchError(ACCOUNT_PROVIDER_DIFFERS)


def run_startup_account_check(db: Database, account: AccountConfig) -> None:
    """Run ``verify_account`` before the server starts; exit with the
    fixed message on a refusal."""
    try:
        verify_account(db, account)
    except AccountMismatchError as exc:
        raise SystemExit(str(exc)) from None
    log.info("Account verified against the index (provider=%s)", account.provider)
