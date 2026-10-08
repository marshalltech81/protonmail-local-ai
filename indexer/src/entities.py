"""Deterministic entity resolution.

Every participant address becomes a **person** entity keyed by its
canonical address (``threader.canonical_addr``); each display name seen
for that address is recorded as an alias of that one entity. Two
different addresses are never merged, however similar their names: a
display name is sender-controlled, so name similarity is not identity.

Each person's exact address domain becomes an **organization** entity,
except for the common free-mail providers below, whose users share a
domain but not an organization. The domain is taken as written, with
no public-suffix lookup: ``mail.northwind.example`` and
``northwind.example`` are two organizations rather than a guessed
merge.

Entity IDs are deterministic (``person:<address>``,
``org:<domain>``), so reprocessing a message rewrites the same rows.

**Source authority** comes only from an operator-written rules file
(TOML, ``config/authority.toml`` mounted at ``/config``), mapping addresses and
domains to an ``authority_class``. It is recorded on each entity with
the rule that matched (provenance) and is never a ranking weight. No
model classifies anything.
"""

import hashlib
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .threader import canonical_addr

PERSON_PREFIX = "person:"
ORG_PREFIX = "org:"

# Common free-mail domains: an address here names a person, not an
# organization. Kept deliberately small; anything missing just gets an
# organization entity of its own, which merges nobody.
FREE_MAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "fastmail.com",
        "gmail.com",
        "gmx.com",
        "gmx.de",
        "googlemail.com",
        "hey.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mac.com",
        "mail.com",
        "me.com",
        "msn.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "tutanota.com",
        "yahoo.com",
        "yandex.ru",
        "zoho.com",
    }
)


def address_domain(address: str) -> str:
    """The domain part of a canonical address, or ``""``."""
    _, at, domain = address.rpartition("@")
    return domain if at else ""


def organization_domain(address: str) -> str | None:
    """The organization domain for a canonical address, or ``None`` for
    a free-mail provider or an address without a domain."""
    domain = address_domain(address)
    if not domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain


def person_entity_id(address: str) -> str:
    return PERSON_PREFIX + address


def org_entity_id(domain: str) -> str:
    return ORG_PREFIX + domain


# ---------------------------------------------------------------------------
# Source authority
# ---------------------------------------------------------------------------

# The classes an operator may assign; ``unclassified`` is what no rule
# matched and cannot be assigned. The MCP server's ``authority_class``
# filter accepts this set plus ``unclassified``.
AUTHORITY_CLASSES = ("counsel", "management", "vendor", "government", "personal", "other")
UNCLASSIFIED = "unclassified"

# Work bounds for the rules file. It is operator-written, not mail, but
# loading must still be bounded: the size is checked before parsing and
# the pattern count before building the lookup tables.
AUTHORITY_RULES_MAX_BYTES = 1024 * 1024
AUTHORITY_RULES_MAX_PATTERNS = 10_000

_RULE_KEYS = ("addresses", "domains")

# Domain bounds, shared by rule validation and classification. DNS caps
# a name at 253 characters; 16 labels is far deeper than any real mail
# domain. Classification looks at most ``_MAX_DOMAIN_LABELS`` suffixes of
# at most ``_MAX_DOMAIN_CHARS`` characters each, so the work per address
# is bounded however the sender shapes its domain; a longer or deeper
# domain is unclassified. A rule beyond either bound could never match,
# so loading rejects it.
_MAX_DOMAIN_CHARS = 253
_MAX_DOMAIN_LABELS = 16
# One LDH label: letters, digits and inner hyphens, 1-63 characters.
_LDH_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class AuthorityRulesError(ValueError):
    """The rules file is unusable. The message names the file position
    (class, key, entry index), never a pattern: the file holds
    addresses."""


@dataclass(frozen=True)
class AuthorityRules:
    """Exact-address and domain patterns, each mapped to one class.

    An address rule beats any domain rule; a domain rule matches the
    domain and its subdomains, the closest listed parent winning.
    """

    addresses: dict[str, str] = field(default_factory=dict)
    domains: dict[str, str] = field(default_factory=dict)

    @property
    def pattern_count(self) -> int:
        return len(self.addresses) + len(self.domains)

    def classify(self, address: str) -> tuple[str, str | None]:
        """``(authority_class, rule)`` for a canonical address; the rule
        is ``address:<pattern>`` or ``domain:<pattern>``, ``None`` when
        nothing matched."""
        if address in self.addresses:
            return self.addresses[address], f"address:{address}"
        return self.classify_domain(address_domain(address))

    def classify_domain(self, domain: str) -> tuple[str, str | None]:
        """Longest listed suffix of ``domain`` (on label boundaries).

        The domain is sender-controlled, so the length and label count
        are checked before any suffix is built: at most
        ``_MAX_DOMAIN_LABELS`` lookups of at most ``_MAX_DOMAIN_CHARS``
        characters each.
        """
        if not domain or len(domain) > _MAX_DOMAIN_CHARS or domain.count(".") >= _MAX_DOMAIN_LABELS:
            return UNCLASSIFIED, None
        # Suffix start offsets, whole domain first (closest parent wins).
        starts = [0]
        position = domain.find(".")
        while position != -1:
            starts.append(position + 1)
            position = domain.find(".", position + 1)
        for start in starts:
            candidate = domain[start:]
            if candidate in self.domains:
                return self.domains[candidate], f"domain:{candidate}"
        return UNCLASSIFIED, None


def _is_bare_domain(value: str) -> bool:
    """LDH labels joined by single dots, within the classification bounds.

    Anything else (a wildcard, a URL, an empty label) could never equal a
    canonical address domain, so a rule written that way would silently
    never match."""
    if not value or len(value) > _MAX_DOMAIN_CHARS:
        return False
    labels = value.split(".")
    return len(labels) <= _MAX_DOMAIN_LABELS and all(
        _LDH_LABEL_RE.fullmatch(label) for label in labels
    )


def _read_operator_toml(
    path: Path, max_bytes: int, what: str, error: type[ValueError]
) -> dict | None:
    """Parse an operator-written TOML file of at most ``max_bytes``.

    ``None`` when the file is absent. Anything else that is not a
    readable regular file within the bound, or not TOML, raises
    ``error``; the message names ``what``, the path and at most a line
    number, never the file's text (it holds addresses).
    """
    # Only a missing final path entry means "absent". Any other
    # failure to inspect it (a dangling symlink, an unsearchable parent)
    # fails closed rather than silently loading nothing.
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise error(f"{what} {path} could not be inspected ({type(exc).__name__})") from None
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise error(f"{what} {path} could not be inspected ({type(exc).__name__})") from None
    if not stat.S_ISREG(mode):
        raise error(f"{what} {path} is not a regular file")
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise error(f"{what} {path} could not be read ({type(exc).__name__})") from None
    if len(raw) > max_bytes:
        raise error(f"{what} {path} is larger than {max_bytes} bytes")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        where = getattr(exc, "lineno", None)
        suffix = f" (line {where})" if where else ""
        raise error(f"{what} {path} is not valid TOML{suffix}") from None
    return data


def load_authority_rules(path: Path) -> AuthorityRules:
    """Load the operator rules file at ``path``.

    An absent file returns empty rules (everything ``unclassified``).
    Anything else that is not a valid file — not a regular file,
    unreadable, too large, not TOML, an unknown class or key, a
    malformed or repeated pattern, too many patterns — raises
    ``AuthorityRulesError`` so the indexer fails closed at startup.

    Format: one table per class, each with optional ``addresses``
    (bare addresses, matched exactly) and ``domains`` (bare domains,
    matching subdomains too)::

        [counsel]
        domains = ["lawfirm.example"]
        addresses = ["outside.counsel@mail.example"]
    """
    data = _read_operator_toml(
        path, AUTHORITY_RULES_MAX_BYTES, "authority rules", AuthorityRulesError
    )
    if data is None:
        return AuthorityRules()

    addresses: dict[str, str] = {}
    domains: dict[str, str] = {}
    # Errors name positions, never the file's text: a table or key name
    # the operator mistyped may itself be an address.
    for table_number, (cls, table) in enumerate(data.items(), 1):
        if cls not in AUTHORITY_CLASSES:
            raise AuthorityRulesError(
                f"authority rules {path}: unknown authority class (table {table_number}); "
                f"use one of {', '.join(AUTHORITY_CLASSES)}"
            )
        if not isinstance(table, dict):
            raise AuthorityRulesError(f"authority rules {path}: [{cls}] must be a table")
        for key_number, (key, entries) in enumerate(table.items(), 1):
            if key not in _RULE_KEYS:
                raise AuthorityRulesError(
                    f"authority rules {path}: unknown key in [{cls}] (key {key_number}); "
                    f"use {' or '.join(_RULE_KEYS)}"
                )
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise AuthorityRulesError(
                    f"authority rules {path}: {cls}.{key} must be a list of strings"
                )
            if len(addresses) + len(domains) + len(entries) > AUTHORITY_RULES_MAX_PATTERNS:
                raise AuthorityRulesError(
                    f"authority rules {path}: at most {AUTHORITY_RULES_MAX_PATTERNS} "
                    "patterns are allowed"
                )
            for index, entry in enumerate(entries):
                pattern = entry.strip().lower()
                where = f"{cls}.{key}[{index}]"
                if key == "addresses":
                    if canonical_addr(pattern) != pattern or not _is_bare_domain(
                        address_domain(pattern)
                    ):
                        raise AuthorityRulesError(
                            f"authority rules {path}: {where} must be a bare address (name@domain)"
                        )
                    target = addresses
                else:
                    if not _is_bare_domain(pattern):
                        raise AuthorityRulesError(
                            f"authority rules {path}: {where} must be a bare domain: "
                            "dot-separated labels of letters, digits and hyphens, at most "
                            f"{_MAX_DOMAIN_LABELS} labels and {_MAX_DOMAIN_CHARS} characters"
                        )
                    target = domains
                if pattern in target:
                    raise AuthorityRulesError(
                        f"authority rules {path}: {where} lists a pattern more than once"
                    )
                target[pattern] = cls
    return AuthorityRules(addresses=addresses, domains=domains)


# ---------------------------------------------------------------------------
# Operator identity
# ---------------------------------------------------------------------------

# Work bounds for ``config/identity.toml``: the size is checked before
# parsing and the address count before any address is normalised.
IDENTITY_MAX_BYTES = 64 * 1024
IDENTITY_MAX_ADDRESSES = 1_000

_IDENTITY_KEY = "addresses"


class OperatorIdentityError(ValueError):
    """The identity file is unusable. The message names the entry's
    position, never an address."""


@dataclass(frozen=True)
class OperatorIdentity:
    """The operator's own canonical addresses (#824).

    Exact addresses only, normalised as ``canonical_addr`` normalises a
    participant address, so a stored participant address is in the set
    exactly when the operator listed it. No domain or plus-alias
    inference.
    """

    addresses: frozenset[str]


def identity_digest(addresses: frozenset[str]) -> str:
    """SHA-256 (hex) of the sorted addresses, each followed by ``\n``;
    the empty set's digest is that of the empty string."""
    return hashlib.sha256("".join(f"{a}\n" for a in sorted(addresses)).encode()).hexdigest()


def load_operator_identity(path: Path) -> OperatorIdentity | None:
    """Load the operator identity file at ``path``.

    ``None`` when the file is absent: the operator has not configured
    their addresses. Anything else that is not a valid file (not a
    regular file, unreadable, too large, not TOML, a key other than
    ``addresses``, no addresses, an entry that is not a bare canonical
    address, an address listed twice, too many addresses) raises
    ``OperatorIdentityError`` so the indexer fails closed at startup.

    Format::

        addresses = ["me@mail.example", "alias@mail.example"]
    """
    data = _read_operator_toml(path, IDENTITY_MAX_BYTES, "operator identity", OperatorIdentityError)
    if data is None:
        return None
    # Errors name positions, never the file's text: a mistyped key may
    # itself be an address.
    for key_number, key in enumerate(data, 1):
        if key != _IDENTITY_KEY:
            raise OperatorIdentityError(
                f"operator identity {path}: unknown key (key {key_number}); use {_IDENTITY_KEY}"
            )
    entries = data.get(_IDENTITY_KEY, [])
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise OperatorIdentityError(
            f"operator identity {path}: {_IDENTITY_KEY} must be a list of strings"
        )
    if not entries:
        raise OperatorIdentityError(
            f"operator identity {path} lists no addresses; list at least one, or remove the file"
        )
    if len(entries) > IDENTITY_MAX_ADDRESSES:
        raise OperatorIdentityError(
            f"operator identity {path}: at most {IDENTITY_MAX_ADDRESSES} addresses are allowed"
        )
    addresses: set[str] = set()
    for index, entry in enumerate(entries):
        address = entry.strip().lower()
        where = f"{_IDENTITY_KEY}[{index}]"
        if (
            canonical_addr(address) != address
            or address.startswith("@")
            or not _is_bare_domain(address_domain(address))
        ):
            raise OperatorIdentityError(
                f"operator identity {path}: {where} must be a bare address (name@domain)"
            )
        if address in addresses:
            raise OperatorIdentityError(
                f"operator identity {path}: {where} lists an address more than once"
            )
        addresses.add(address)
    return OperatorIdentity(addresses=frozenset(addresses))
