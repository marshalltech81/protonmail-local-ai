"""One predicate layer for the per-message filters (#1084).

Every message-level filter the tools accept is a *leaf*: a named
predicate over one ``messages m`` row with one value. ``LEAVES``
registers each leaf kind with its parameter shape, its SQL compiler,
its evaluability rule and, where ``search_emails`` decides it on the
thread row instead, its thread test. ``compile_leaves`` conjoins a leaf
list into one SQL fragment plus bound values, ``leaf_digest`` binds a
keyset cursor to the list, and three adapters build the list each call
site needs from its existing parameters:

- ``query_messages_leaves`` for ``Database.query_messages``: one
  message satisfies every leaf;
- ``message_scope_leaves`` for ``Database.message_scope``: the same,
  as a label per message;
- ``search_emails_leaves`` for ``Database._apply_filters``: each leaf
  is decided on its own against the thread (its recorded senders and
  participants, its effective-time span, its attachment flag, or the
  existence of a message satisfying the leaf), so one message can
  satisfy the sender leaf and another the date leaf.

The parsing and matching helpers below (addresses, dates, text words,
the authority and folder constants) belong to the leaves and live here
with them; ``lib/sqlite`` imports what its other queries share.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from email.utils import parseaddr
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InvalidFilterError(ValueError):
    """A filter or query argument the caller supplied was rejected (a
    date, a cursor, the ``query_messages`` text), or the two date bounds
    name an empty interval.

    The message may quote the rejected value so the caller learns why,
    which means it must never be logged: tool handlers catch this and log
    only which field failed.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field_name = field_name


# Source-authority classes the indexer assigns from the operator rules
# file (``indexer/src/entities.py`` ``AUTHORITY_CLASSES``) plus
# ``unclassified`` for senders no rule matched.
AUTHORITY_CLASSES = (
    "counsel",
    "management",
    "vendor",
    "government",
    "personal",
    "other",
    "unclassified",
)


def normalize_authority_class(value: str | None) -> str | None:
    """The ``authority_class`` filter to apply: ``None`` for a missing or
    blank value (ignored, like every blank string filter), otherwise the
    stripped value, which must be one of ``AUTHORITY_CLASSES``."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value not in AUTHORITY_CLASSES:
        raise InvalidFilterError(
            "authority_class",
            f"authority_class must be one of {', '.join(AUTHORITY_CLASSES)}",
        )
    return value


# Folders whose messages never count toward an ``authority_class``
# filter (#463). Authority comes from the claimed From address, which is
# sender-controlled, and Proton files most spoofed or DMARC-failing mail
# in Spam. Matched exactly against ``messages.folder``, like the folder
# filters.
AUTHORITY_EXCLUDED_FOLDERS = ("Spam",)

# Claimants (per-message keys) whose From sender's person entity carries
# the bound class. Driven from ``idx_entities_authority`` into the
# participant address index. ``_compile_authority_class`` decides the
# excluded folders and the sender flag on the outer message, so this
# subquery is uncorrelated.
_SENDER_CLASS_MESSAGES = (
    "SELECT p.claimant_id FROM entities e "
    "JOIN message_participants p ON p.address = e.canonical_key AND p.role = 'from' "
    "WHERE e.kind = 'person' AND e.authority_class = ?"
)


def canonical_addr(value: str) -> str:
    """Extract the bare lowercased email from a display string.

    Mirrors ``indexer.threader.canonical_addr`` (the two services are separate
    ``uv`` projects, so the helper is duplicated intentionally until a shared
    package exists). Returns ``""`` when no ``@``-bearing address is
    recoverable, so a callee can distinguish "no email found" from a
    successful normalization.
    """
    if not value:
        return ""
    try:
        _, addr = parseaddr(value)
    except Exception:
        # parseaddr recurses on nested comments. Stored senders and
        # participants are indexed mail, so one hostile entry must count
        # as "no address", not abort a search whose filter is valid.
        return ""
    addr = addr.strip().lower()
    if "@" not in addr:
        return ""
    return addr


# Folders whose mail stays synced and indexed but is left out of
# mailbox-wide retrieval unless the caller names them (#441). Under
# mirror retention a deleted message lives on as its Trash copy until
# it is purged from Trash. Matched exactly, as ``folders`` filters are.
# Thread searches leave out a thread only when every message of it is
# in one of these folders (the per-message membership the ``folders``
# filter uses); message and attachment lookups leave out the messages
# filed there. Lookups of one named thread or message are unaffected.
DEFAULT_EXCLUDED_FOLDERS = ("Trash",)


def _parse_date_range(
    date_from: str | None, date_to: str | None
) -> tuple[datetime | None, datetime | None]:
    """Parse the ``date_from`` / ``date_to`` filters into tz-aware UTC
    bounds, ``None`` for a bound that was not supplied.

    Every tool that takes both bounds parses them here, so they all
    reject the same input. Raises ``InvalidFilterError`` (a
    ``ValueError``) on an unparseable value, and on ``date_from`` after
    ``date_to`` (#312): that interval is empty, but the thread overlap
    predicates (``date_last >= from AND date_first <= to``) would still
    accept a thread spanning it. The comparison runs on the parsed UTC
    instants, after date-only promotion, so a single date names its whole
    day and two offsets for one instant compare equal.
    """
    start = (
        _parse_filter_date(date_from, end_of_day=False, _field_name="date_from")
        if date_from
        else None
    )
    end = _parse_filter_date(date_to, end_of_day=True, _field_name="date_to") if date_to else None
    if start is not None and end is not None and start > end:
        raise InvalidFilterError("date_from/date_to", "date_from must not be after date_to")
    return start, end


def validate_date_range(
    date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    """Raise ``InvalidFilterError`` for a date filter pair the search
    methods would reject. Tool handlers call it on entry so a bad range
    fails before any embedding, retrieval or model call; the database
    methods still check for themselves.

    Returns the UTC bounds the search methods apply
    (``_normalize_date_range``), so a tool can echo them (#802).
    """
    return _normalize_date_range(date_from, date_to)


def _normalize_date_range(
    date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    """``_parse_date_range`` as ISO 8601 strings for SQL pushdown, where
    they are compared lexicographically against stored ``+00:00``
    timestamps (``date_first`` / ``date_last`` / ``effective_at``).
    """
    start, end = _parse_date_range(date_from, date_to)
    return (
        start.isoformat() if start is not None else None,
        end.isoformat() if end is not None else None,
    )


def _parse_filter_date(
    value: str, *, end_of_day: bool, _field_name: str = "date filter"
) -> datetime:
    """Parse a user-supplied date filter into a tz-aware UTC ``datetime``.

    Accepts:
    - date-only values, meaning any form ``date.fromisoformat`` accepts
      (``"2024-12-31"``, ``"20241231"``, ``"2025-W01-2"``, ...), with or
      without a trailing ``Z``: promoted to ``00:00:00`` when used as a
      lower bound, ``23:59:59.999999`` when used as an upper bound, both
      in UTC — so the filter includes the full day the user named (#330).
    - any ISO 8601 datetime ``datetime.fromisoformat`` accepts, including
      a trailing ``Z``: the instant it names, for either bound.

    Naive datetimes are assumed to be UTC. Offset-aware values are
    converted to UTC before being returned, so callers that feed the
    result's ``isoformat()`` into SQL string comparisons against stored
    UTC timestamps compare the same instant rather than two offset-shifted
    strings that happen to sort differently.
    """
    # Date-only is whatever the date parser accepts, not a string shape:
    # a length check missed the basic and week-date forms (#330). The
    # ``Z`` is dropped because the date parser rejects it and it only
    # restates the UTC the day is already read in.
    try:
        day = date.fromisoformat(value.removesuffix("Z"))
    except ValueError:
        day = None
    if day is not None:
        return datetime.combine(day, time.max if end_of_day else time.min, tzinfo=UTC)

    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidFilterError(_field_name, f"{_field_name}: invalid datetime {value!r}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    try:
        return dt.astimezone(UTC)
    except OverflowError as exc:
        # A parseable value at datetime's limit with an outward offset
        # ("0001-01-01T00:00:00+14:00") has no UTC instant.
        raise InvalidFilterError(_field_name, f"{_field_name}: invalid datetime {value!r}") from exc


def _addr_matches(haystack: list[str], query_lower: str) -> bool:
    """True if ``query_lower`` matches an address string in ``haystack``.

    Match mode depends on the shape of the query:

    * A full address (``bob@example.com``) is compared by canonical
      equality so that case variation in the stored display string
      (``Bob@Example.com``, ``Bob Smith <bob@example.com>``) still matches.
    * A bare name (``bob``) or domain fragment (``@example.com``,
      ``example.com``) keeps substring behavior against the display
      string, both sides casefolded (Unicode caseless: ``STRASSE``
      matches ``Straße``), since those shapes cannot canonicalize.
    """
    canonical_query = canonical_addr(query_lower)
    # A canonicalizable full address requires a non-empty local part.
    # ``canonical_addr`` still returns the input for a bare domain like
    # ``@example.com`` because the ``@`` check passes, but equality against
    # ``bob@example.com`` would then miss. Route the domain-only shape through
    # the substring fallback so ``"@example.com"`` still behaves like a
    # domain filter.
    if canonical_query and not canonical_query.startswith("@"):
        return any(canonical_addr(s) == canonical_query for s in haystack)
    query_folded = query_lower.casefold()
    return any(query_folded in s.casefold() for s in haystack)


def _matches_sender(result, from_addr_lower: str) -> bool:
    """True if ``from_addr_lower`` matches one of the thread's senders.

    Senders is the list of ``From`` addresses recorded on the thread.
    See ``_addr_matches`` for the full-address vs. substring match rules.
    """
    return _addr_matches(result.senders, from_addr_lower)


def _matches_participant(result, participant_lower: str) -> bool:
    """True if ``participant_lower`` matches anyone on the thread.

    Participants is the broader From + To + Cc set, so this surfaces
    threads a person was on in *any* role — distinct from
    ``_matches_sender``, which is From-line only. See ``_addr_matches``
    for the full-address vs. substring match rules.
    """
    return _addr_matches(result.participants, participant_lower)


# Each ``text`` term is its own FTS subquery; bound the count so one call
# cannot fan out into hundreds of them.
_MAX_TEXT_TERMS = 16


def _text_terms(text: str) -> list[str]:
    """Split ``text`` into distinct words exactly as the chunk index does.

    ``message_chunks_fts`` tokenizes with ``porter unicode61``. Hand-rolled
    splitting kept drifting from unicode61 — combining marks, underscores
    (separators to FTS), private-use characters (word characters to FTS)
    — and every drift silently changed an exhaustive count. So the words
    come from unicode61 itself: a throwaway in-memory FTS5 table and its
    ``fts5vocab`` instance view, in text order. Porter is left out so each
    word stays unstemmed; the index's tokenizer stems it once at MATCH
    time. unicode61's case and diacritic folding is idempotent, so a word
    quoted as a phrase re-tokenizes to itself. The text is not normalized
    (indexed chunks are stored as written).
    """
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61')")
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        conn.execute("INSERT INTO t(x) VALUES (?)", (text,))
        rows = conn.execute("SELECT term FROM v ORDER BY offset").fetchall()
    return list(dict.fromkeys(row[0] for row in rows))


def address_match_mode(value: str) -> str:
    """How a sender / recipient / participant predicate matches.

    ``"exact"`` when ``value`` holds a full address (``jane@example.com``,
    ``Jane <jane@example.com>``): canonical equality, an indexed lookup.
    ``"substring"`` otherwise (a domain like ``@example.com`` or a name
    fragment): case-insensitive substring of the address or of any one
    display name it was written with.
    """
    # Nested-comment input that makes parseaddr recurse canonicalizes to
    # "", so it can only be a substring.
    canonical = canonical_addr(value)
    return "exact" if canonical and not canonical.startswith("@") else "substring"


# The ``messages`` flag that says a role's stored addresses are complete
# (#1086): 1 when the parser kept every address of that header, 0 when
# a cap or an unparseable element lost one, NULL when not assessed.
ROLE_COMPLETE_COLUMNS = {
    "from": "from_addresses_complete",
    "to": "to_addresses_complete",
    "cc": "cc_addresses_complete",
}


def _role_clause(mode: str, value: str, roles: tuple[str, ...], params: list) -> str:
    """SQL deciding whether the address leaf ``mode`` (an
    ``_ADDRESS_MODES`` key) finds ``value`` in one of ``roles`` of
    ``messages m``; appends the bound values to ``params``.

    A stored match is 1. No match is 0 only when every role's stored
    addresses are complete (``ROLE_COMPLETE_COLUMNS`` all 1, #1086) and,
    for a mode that reads display names, every display name is stored
    (``m.participant_names_complete`` 1, #1140); otherwise it is unknown
    (NULL): a cap or an unparseable element lost an address, the name
    budget dropped a name, or the message is not assessed yet (not
    reparsed since the upgrade that added the flag)."""
    match_sql, reads_names = _ADDRESS_MODES[mode]
    complete = [f"m.{ROLE_COMPLETE_COLUMNS[role]} = 1" for role in roles]
    if reads_names:
        complete.append("m.participant_names_complete = 1")
    match = match_sql(value, roles, params)
    # The column names are constants; the values are bound.
    return f"CASE WHEN {match} THEN 1 WHEN {' AND '.join(complete)} THEN 0 ELSE NULL END"


def _address_is_match(value: str, roles: tuple[str, ...], params: list) -> str:
    # Canonical equality, an indexed lookup. A value with no full address
    # canonicalizes to "" and matches nothing.
    role_sql = ",".join(["?"] * len(roles))
    params.extend([canonical_addr(value), *roles])
    return (
        "m.claimant_id IN (SELECT claimant_id FROM message_participants "  # nosec B608
        f"WHERE address = ? AND role IN ({role_sql}))"
    )


def _rows_match(rows: Callable[[str, tuple[str, ...], list], str]) -> Callable[..., str]:
    """The match of a mode that selects ``message_participants p`` rows."""

    def match(value: str, roles: tuple[str, ...], params: list) -> str:
        return (
            "m.claimant_id IN (SELECT p.claimant_id FROM message_participants p "  # nosec B608
            f"WHERE {rows(value, roles, params)})"
        )

    return match


def _address_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    # Addresses are stored lowercased.
    role_sql = ",".join(["?"] * len(roles))
    params.extend([*roles, value.strip().lower()])
    return f"p.role IN ({role_sql}) AND instr(p.address, ?) > 0"


def _name_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    # Each display name the message wrote the address with (#1140) is
    # matched on its own, so no match spans two names.
    role_sql = ",".join(["?"] * len(roles))
    params.extend([*roles, value.strip().casefold()])
    return (
        f"p.role IN ({role_sql}) "  # nosec B608
        "AND EXISTS (SELECT 1 FROM message_participant_names n "
        "WHERE n.claimant_id = p.claimant_id AND n.role = p.role AND n.address = p.address "
        "AND instr(mcp_casefold(n.name), ?) > 0)"
    )


def _domain_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    # The stored (canonical, lowercased) address ends in "@" plus the
    # domain: its exact domain, never a suffix such as a subdomain. The
    # value's validation and normalization are settled with the ``where``
    # parameter (#1088); here it is only stripped and lowercased.
    role_sql = ",".join(["?"] * len(roles))
    suffix = "@" + value.strip().lower()
    params.extend([*roles, -len(suffix), suffix])
    return f"p.role IN ({role_sql}) AND substr(p.address, ?) = ?"


def _substring_participant_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    """SQL selecting the ``message_participants p`` rows in ``roles``
    whose address, or any one display name the message wrote it with
    (``message_participant_names``, #1140), contains ``value``; appends
    the bound values to ``params``. Each name is matched on its own, so
    no match spans two names."""
    role_sql = ",".join(["?"] * len(roles))
    # Addresses are stored lowercased; names fold with ``mcp_casefold``.
    params.extend([*roles, value.strip().lower(), value.strip().casefold()])
    return (
        f"p.role IN ({role_sql}) "  # nosec B608
        "AND (instr(p.address, ?) > 0 OR EXISTS (SELECT 1 FROM message_participant_names n "
        "WHERE n.claimant_id = p.claimant_id AND n.role = p.role AND n.address = p.address "
        "AND instr(mcp_casefold(n.name), ?) > 0))"
    )


# ---------------------------------------------------------------------------
# Leaves
# ---------------------------------------------------------------------------


class Evaluability(Enum):
    """How a leaf decides a message.

    ``DECIDED``: the leaf reads a field the index stores for every
    message, so it is true or false of each message, never unknown.
    ``UNKNOWN_WHEN_NULL``: the field can be NULL (``size_bytes`` for a
    message whose file size was not recorded, ``occurred_at`` for one
    without a parseable topmost ``Received:`` header, the send date of
    one without a parseable ``Date:`` header or not yet assessed, and
    the effective time of one with neither date, #1080), or the leaf reads
    it only when another field says it can be trusted (``sender`` and
    the From side of ``participant``, when ``sender_ambiguous`` is not
    0, #1153; ``authority_class`` likewise outside Spam, #1161; a
    substring ``sender``, ``recipient`` or ``participant`` that matches
    nothing, when ``participant_names_complete`` is not 1, #1140). Such
    a message is
    neither matched nor missed: the leaf's SQL yields NULL, so the
    conjunction is unknown (SQL's three-valued AND: false if any leaf is
    false, else unknown), the row is left out of the matches and of
    ``total_matches``, and ``Database.query_messages`` counts it as
    ``indeterminate`` (#1085). Every such leaf's SQL must be NULL exactly
    when it cannot be decided, and every ``DECIDED`` leaf's 0 or 1, for
    that count to hold. #1086 extends the rule to stored content: a
    ``subject``, ``text``, ``has_attachments`` or address leaf that finds
    nothing is false only when the content it reads is complete
    (``messages.*_complete`` 1), and unknown under 0 (a parse cap lost
    part of it) or NULL (not assessed yet).
    """

    DECIDED = "decided"
    UNKNOWN_WHEN_NULL = "unknown_when_null"


@dataclass(frozen=True)
class Leaf:
    """One predicate on a message: a registered leaf ``name`` and the
    value its ``LeafKind.param`` describes."""

    name: str
    value: Any


@dataclass(frozen=True)
class LeafKind:
    """A registered leaf type.

    ``param`` names the shape of ``Leaf.value`` (``docs/mcp-tools.md``,
    "Filter predicates"). ``compile`` appends the leaf's bound values to
    a parameter list and returns its SQL predicate over ``messages m``,
    safe inside an ``AND`` chain. ``thread_test``, when set, is how
    ``search_emails`` decides the leaf on a thread row instead: it
    takes the value and returns a test over one ``ThreadResult``.
    """

    name: str
    param: str
    compile: Callable[[Any, list], str]
    evaluability: Evaluability
    thread_test: Callable[[Any], Callable[[Any], bool]] | None = None


# ``messages.sent_at_status`` (#1080): why ``sent_at`` is what it is.
SentAtStatus = Literal["parsed", "missing", "invalid"]

# A message's send date as evidence (#1080): its ``sent_at`` only once
# the indexer has parsed it from the ``Date:`` header. NULL for a
# missing or unparseable header, and for a row not yet assessed (from
# before schema v8, until its reparse), whose stored value may be a
# made-up fallback.
SENT_CLOCK_SQL = "CASE WHEN m.sent_at_status = 'parsed' THEN m.sent_at END"
# A message's effective time as evidence: its delivery date, else its
# send date. NULL when it has neither: ``messages.effective_at`` then
# holds ``first_indexed_at``, an ordering position that no date bound
# reads (#1080).
EFFECTIVE_EVIDENCE_SQL = f"COALESCE(m.occurred_at, {SENT_CLOCK_SQL})"


@dataclass(frozen=True)
class DateBasis:
    """One clock of ``messages`` that ``query_messages``' date bounds,
    order and cursor run on (#1085; ``docs/architecture.md`` "Message
    time"). ``clock`` is the SQL value of the order and cursor,
    ``bound`` the SQL value its bound leaves ``from_leaf`` / ``to_leaf``
    compare, NULL (unknown) where the message carries no such date.
    ``nullable`` says ``clock`` can be NULL; the adapter then adds the
    ``dated`` leaf so every row of the page has a place in the ordering.
    The effective clock is never NULL (``effective_at`` falls back to
    ``first_indexed_at``), but its bound is (#1080).
    """

    name: str
    clock: str
    bound: str
    from_leaf: str
    to_leaf: str
    nullable: bool


DEFAULT_DATE_BASIS = "effective"

DATE_BASES: dict[str, DateBasis] = {
    basis.name: basis
    for basis in (
        DateBasis(
            "effective",
            "m.effective_at",
            EFFECTIVE_EVIDENCE_SQL,
            "effective_from",
            "effective_to",
            False,
        ),
        DateBasis("sent", SENT_CLOCK_SQL, SENT_CLOCK_SQL, "sent_from", "sent_to", True),
        DateBasis(
            "occurred", "m.occurred_at", "m.occurred_at", "occurred_from", "occurred_to", True
        ),
    )
}

# A legal basis the index cannot serve yet, with the fixed text it
# answers: the server arrival time (IMAP INTERNALDATE) is stored by
# #1081 and served by #1092.
UNAVAILABLE_DATE_BASES: dict[str, str] = {
    "internal": (
        "date_basis 'internal' is unavailable until #1092 (the index stores no "
        "server arrival time); use effective, sent or occurred"
    ),
}


def normalize_date_basis(value: Any) -> str:
    """The ``date_basis`` to apply: ``DEFAULT_DATE_BASIS`` for a missing
    or blank value, otherwise the stripped name of a ``DATE_BASES``
    entry. Raises ``InvalidFilterError`` (fixed text) for a non-string
    (the ``query_messages`` tool passes the raw argument through, so the
    rejection reaches the rate-limited per-field log) or an unavailable
    or unknown basis."""
    if value is not None and not isinstance(value, str):
        raise InvalidFilterError("date_basis", "date_basis must be a string")
    if value is None or not value.strip():
        return DEFAULT_DATE_BASIS
    value = value.strip()
    if value in DATE_BASES:
        return value
    if value in UNAVAILABLE_DATE_BASES:
        raise InvalidFilterError("date_basis", UNAVAILABLE_DATE_BASES[value])
    raise InvalidFilterError(
        "date_basis",
        "date_basis must be one of " + ", ".join([*DATE_BASES, *UNAVAILABLE_DATE_BASES]),
    )


# The largest value a size bound can take: SQLite's INTEGER is 64-bit
# signed, and ``sqlite3`` refuses to bind a larger Python int
# (``OverflowError``). The tool's schema states the same range.
MAX_SIZE_BYTES = 2**63 - 1


def normalize_size_bound(name: str, value: Any) -> int | None:
    """A ``size_min`` / ``size_max`` filter to apply: ``None`` when not
    given, otherwise the value, which must be an integer from 0 to
    ``MAX_SIZE_BYTES`` (fixed-text ``InvalidFilterError`` otherwise).
    The ``query_messages`` tool passes the raw argument through to this
    check (its schema states the range), so a rejection reaches the
    rate-limited per-field log."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SIZE_BYTES:
        raise InvalidFilterError(name, f"{name} must be an integer from 0 to {MAX_SIZE_BYTES}")
    return value


# The explicit address leaves (#1088): name -> (match SQL builder,
# whether it reads display names, so needs them complete to say no).
_ADDRESS_MODES: dict[str, tuple[Callable[[str, tuple[str, ...], list], str], bool]] = {
    # Canonical equality with a full address.
    "address_is": (_address_is_match, False),
    # Caseless substring of the stored address only.
    "address_contains": (_rows_match(_address_rows), False),
    # Caseless (casefolded) substring of one stored display name only.
    "display_name_contains": (_rows_match(_name_rows), True),
    # Either: the inferred substring mode of the flat filters.
    "address_or_name_contains": (_rows_match(_substring_participant_rows), True),
    # The exact domain of the stored address.
    "domain_is": (_rows_match(_domain_rows), False),
}


def _address_is_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    role_sql = ",".join(["?"] * len(roles))
    params.extend([*roles, canonical_addr(value)])
    return f"p.role IN ({role_sql}) AND p.address = ?"


# The ``message_participants p`` rows each explicit address leaf's match
# selects: where its matched addresses come from (``where`` leaf results).
ADDRESS_LEAF_ROWS: dict[str, Callable[[str, tuple[str, ...], list], str]] = {
    "address_is": _address_is_rows,
    "address_contains": _address_rows,
    "display_name_contains": _name_rows,
    "address_or_name_contains": _substring_participant_rows,
    "domain_is": _domain_rows,
}

# The stored ``message_participants`` roles each explicit address
# leaf's role names (#1088). ``visible_participant`` (From, To or Cc)
# is internal: it is what the flat ``participant`` filter compiles to.
# The Bcc-inclusive ``recipient`` and ``any`` arrive with Bcc (#1090).
ROLE_SETS: dict[str, tuple[str, ...]] = {
    "from": ("from",),
    "to": ("to",),
    "cc": ("cc",),
    "visible_recipient": ("to", "cc"),
    "visible_participant": ("from", "to", "cc"),
}

# The role each flat address filter compiles to; the flat filters keep
# their visible meaning permanently (#1088).
_FLAT_ADDRESS_ROLES = {
    "sender": "from",
    "recipient": "visible_recipient",
    "participant": "visible_participant",
}

# Roles each flat address leaf searches in ``message_participants``.
ADDRESS_ROLES: dict[str, tuple[str, ...]] = {
    name: ROLE_SETS[role] for name, role in _FLAT_ADDRESS_ROLES.items()
}


def inferred_address_leaf(leaf: Leaf) -> Leaf:
    """The explicit leaf a flat ``sender`` / ``recipient`` /
    ``participant`` leaf compiles to: ``address_is`` for a full address,
    otherwise ``address_or_name_contains`` (``address_match_mode``),
    on the flat filter's role."""
    mode = "address_is" if address_match_mode(leaf.value) == "exact" else "address_or_name_contains"
    return Leaf(mode, (_FLAT_ADDRESS_ROLES[leaf.name], leaf.value))


def _compile_address(mode: str) -> Callable[[tuple[str, str], list], str]:
    """The compiler of the explicit address leaf ``mode``, whose value is
    ``(role, value)`` with ``role`` a ``ROLE_SETS`` key."""

    def compile(role_value: tuple[str, str], params: list) -> str:
        role, value = role_value
        roles = ROLE_SETS[role]
        if "from" not in roles:
            return _role_clause(mode, value, roles, params)
        # The From role decides the leaf only when the message's sender
        # attribution is known safe (``sender_ambiguous = 0``, #1144).
        # For 1 (a repeated From, or a header scan cut short) or NULL (not
        # assessed yet) the author cannot be told, so the From side is
        # unknown whether or not the stored From carries the value
        # (#1153).
        others = tuple(r for r in roles if r != "from")
        other_sql = _role_clause(mode, value, others, params) if others else ""
        from_sql = (
            "CASE WHEN m.sender_ambiguous = 0 THEN "
            f"{_role_clause(mode, value, ('from',), params)} ELSE NULL END"
        )
        # SQL's three-valued OR: a match in another role decides the leaf
        # whatever the sender flag; otherwise the From side answers.
        return f"({other_sql} OR {from_sql})" if others else from_sql

    return compile


def _compile_inferred(name: str) -> Callable[[str, list], str]:
    """The compiler of the flat address leaf ``name``: its inferred
    explicit leaf's (``inferred_address_leaf``)."""

    def compile(value: str, params: list) -> str:
        explicit = inferred_address_leaf(Leaf(name, value))
        return LEAVES[explicit.name].compile(explicit.value, params)

    return compile


def _decided_by(match: str, complete: str) -> str:
    """``match`` as a leaf over stored content: 1 when it holds, 0 when
    it does not and the content is complete (``complete`` is a
    ``messages`` flag, 1 complete, #1086), otherwise unknown (NULL: a
    parse cap lost part of the content, or it is not assessed yet)."""
    return f"CASE WHEN {match} THEN 1 WHEN m.{complete} = 1 THEN 0 ELSE NULL END"


def _compile_subject(value: str, params: list) -> str:
    # The stored subject is cut to the parser's limit; a cut one cannot
    # rule a value out (``subject_complete`` 0).
    params.append(value.casefold())
    return _decided_by("instr(mcp_casefold(m.subject), ?) > 0", "subject_complete")


def _compile_body_words(terms: tuple[str, ...], params: list) -> str:
    # One subquery per word, so the words may fall in different chunks
    # of the same message. Each is a quoted FTS phrase; unicode61 never
    # keeps a quote inside a token, but doubling any (FTS5 string
    # escaping) keeps FTS syntax out regardless. The body chunks hold
    # the whole body only under ``body_complete`` 1, written with them
    # (#1086).
    params.extend('"' + term.replace('"', '""') + '"' for term in terms)
    match = " AND ".join(
        "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
        "JOIN message_chunks c ON c.fts_rowid = f.rowid "
        "WHERE message_chunks_fts MATCH ? AND c.attachment_id IS NULL)"
        for _ in terms
    )
    return _decided_by(f"({match})", "body_complete")


def _compile_folder(folders: tuple[str, ...], params: list) -> str:
    params.extend(folders)
    return f"m.folder IN ({','.join('?' * len(folders))})"


def _compile_not_in_folders(folders: tuple[str, ...], params: list) -> str:
    params.extend(folders)
    return f"m.folder NOT IN ({','.join('?' * len(folders))})"


def _operand(value_sql: str) -> str:
    """``value_sql`` safe as one operand: a column as it is, any other
    expression in parentheses."""
    column = value_sql.removeprefix("m.")
    return value_sql if column != value_sql and column.isidentifier() else f"({value_sql})"


def _compile_bound(value_sql: str, op: str) -> Callable[[Any, list], str]:
    """The compiler for an inclusive bound (``op`` is ``>=`` or ``<=``)
    on a clock or size value of ``messages m`` (``value_sql``). A NULL
    value compares as unknown, so the row is left out
    (``Evaluability``)."""

    def compile(value: Any, params: list) -> str:
        params.append(value)
        return f"{_operand(value_sql)} {op} ?"

    return compile


def _compile_dated(basis: str, params: list) -> str:
    # 1 when the clock is stored, NULL (unknown) when it is not, so the
    # conjunction is unknown, not false, for a row the basis cannot place
    # and ``indeterminate`` counts it (``Database.query_messages``).
    return f"NULLIF({_operand(DATE_BASES[basis].clock)} IS NOT NULL, 0)"


def _compile_has_attachments(state: bool, params: list) -> str:
    # A stored attachment decides the leaf either way. An empty list
    # rules attachments out only when the parser walked the whole
    # message (``attachments_manifest_complete`` 1, #1086); otherwise
    # it is unknown.
    params.append(1 if state else 0)
    return (
        "CASE WHEN m.has_attachments = 1 OR m.attachments_manifest_complete = 1 "
        "THEN m.has_attachments = ? ELSE NULL END"
    )


def _compile_flag(column: str) -> Callable[[bool, list], str]:
    """The compiler for a 0/1 column of ``messages`` matched either way."""

    def compile(state: bool, params: list) -> str:
        params.append(1 if state else 0)
        return f"m.{column} = ?"

    return compile


def _compile_authority_class(value: str, params: list) -> str:
    # A message in ``AUTHORITY_EXCLUDED_FOLDERS`` is a decided "no"
    # whatever its sender flag (#463). Otherwise the From sender decides
    # the leaf only when its attribution is known safe
    # (``sender_ambiguous = 0``, #1144): for 1 (a repeated From, or a
    # header scan cut short) or NULL (not assessed yet) the author cannot
    # be told, so the leaf is unknown (#1161, owner 2026-10-08). After
    # the v2 upgrade every message is NULL until the queued reparse
    # reaches it, so ``query_messages`` counts those as indeterminate
    # until it drains; a dead-lettered message stays NULL. A safe
    # sender with a stored address of the class is a match; one without
    # is a miss only when its From list is complete
    # (``from_addresses_complete`` 1), and otherwise unknown (#1086,
    # owner 2026-10-08).
    params.extend([*AUTHORITY_EXCLUDED_FOLDERS, value])
    return (
        # The f-string adds ``?`` placeholders only; the values are bound.
        f"CASE WHEN m.folder IN ({','.join('?' * len(AUTHORITY_EXCLUDED_FOLDERS))}) THEN 0 "
        "WHEN m.sender_ambiguous IS NOT 0 THEN NULL "
        f"WHEN m.claimant_id IN ({_SENDER_CLASS_MESSAGES}) THEN 1 "
        "WHEN m.from_addresses_complete = 1 THEN 0 ELSE NULL END"
    )


# Thread tests: how ``search_emails`` decides a leaf on a thread row,
# unchanged from before the leaves existed. An address is matched
# against the thread's recorded senders or participants (display
# strings, ``_addr_matches``), a bound against the thread's span, and
# the attachment flag against the thread's own.


def _sender_test(value: str) -> Callable[[Any], bool]:
    needle = value.lower()
    return lambda result: _matches_sender(result, needle)


def _participant_test(value: str) -> Callable[[Any], bool]:
    needle = value.lower()
    return lambda result: _matches_participant(result, needle)


def _effective_from_test(instant: str) -> Callable[[Any], bool]:
    bound = datetime.fromisoformat(instant)
    return lambda result: result.date_last >= bound


def _effective_to_test(instant: str) -> Callable[[Any], bool]:
    bound = datetime.fromisoformat(instant)
    return lambda result: result.date_first <= bound


def _has_attachments_test(state: bool) -> Callable[[Any], bool]:
    return lambda result: result.has_attachments == state


LEAVES: dict[str, LeafKind] = {
    kind.name: kind
    for kind in (
        # The flat address filters, in their inferred mode
        # (``inferred_address_leaf``).
        LeafKind(
            "sender",
            "address",
            _compile_inferred("sender"),
            Evaluability.UNKNOWN_WHEN_NULL,
            _sender_test,
        ),
        LeafKind(
            "recipient", "address", _compile_inferred("recipient"), Evaluability.UNKNOWN_WHEN_NULL
        ),
        LeafKind(
            "participant",
            "address",
            _compile_inferred("participant"),
            Evaluability.UNKNOWN_WHEN_NULL,
            _participant_test,
        ),
        # The explicit address leaves (#1088): a ``ROLE_SETS`` role and a
        # value.
        *(
            LeafKind(mode, param, _compile_address(mode), Evaluability.UNKNOWN_WHEN_NULL)
            for mode, param in (
                ("address_is", "role, address"),
                ("address_contains", "role, text"),
                ("display_name_contains", "role, text"),
                ("address_or_name_contains", "role, text"),
                ("domain_is", "role, domain"),
            )
        ),
        LeafKind("subject", "text", _compile_subject, Evaluability.UNKNOWN_WHEN_NULL),
        # ``text`` is the flat filter's name for ``body_words`` (#1088).
        LeafKind("text", "words", _compile_body_words, Evaluability.UNKNOWN_WHEN_NULL),
        LeafKind("body_words", "words", _compile_body_words, Evaluability.UNKNOWN_WHEN_NULL),
        LeafKind("folder", "folders", _compile_folder, Evaluability.DECIDED),
        LeafKind("not_in_folders", "folders", _compile_not_in_folders, Evaluability.DECIDED),
        # A message with neither a delivery nor a send date is unknown
        # under an effective-time bound, never placed by the time it was
        # first indexed (#1080).
        LeafKind(
            "effective_from",
            "instant",
            _compile_bound(EFFECTIVE_EVIDENCE_SQL, ">="),
            Evaluability.UNKNOWN_WHEN_NULL,
            _effective_from_test,
        ),
        LeafKind(
            "effective_to",
            "instant",
            _compile_bound(EFFECTIVE_EVIDENCE_SQL, "<="),
            Evaluability.UNKNOWN_WHEN_NULL,
            _effective_to_test,
        ),
        # The other clocks of ``query_messages``' ``date_basis`` (#1085).
        # Each is NULL without its date (a send date also until it is
        # assessed, #1080), so the ``dated`` leaf keeps such rows out of
        # a page ordered by it.
        LeafKind(
            "sent_from",
            "instant",
            _compile_bound(SENT_CLOCK_SQL, ">="),
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
        LeafKind(
            "sent_to",
            "instant",
            _compile_bound(SENT_CLOCK_SQL, "<="),
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
        LeafKind(
            "occurred_from",
            "instant",
            _compile_bound("m.occurred_at", ">="),
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
        LeafKind(
            "occurred_to",
            "instant",
            _compile_bound("m.occurred_at", "<="),
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
        LeafKind("dated", "basis", _compile_dated, Evaluability.UNKNOWN_WHEN_NULL),
        LeafKind(
            "has_attachments",
            "bool",
            _compile_has_attachments,
            Evaluability.UNKNOWN_WHEN_NULL,
            _has_attachments_test,
        ),
        LeafKind("seen", "bool", _compile_flag("seen"), Evaluability.DECIDED),
        LeafKind("flagged", "bool", _compile_flag("flagged"), Evaluability.DECIDED),
        LeafKind("replied", "bool", _compile_flag("replied"), Evaluability.DECIDED),
        # The local Maildir file's size, NULL when not recorded.
        LeafKind(
            "size_min",
            "bytes",
            _compile_bound("m.size_bytes", ">="),
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
        LeafKind(
            "size_max",
            "bytes",
            _compile_bound("m.size_bytes", "<="),
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
        LeafKind(
            "authority_class",
            "class",
            _compile_authority_class,
            Evaluability.UNKNOWN_WHEN_NULL,
        ),
    )
}


def compile_leaves(leaves: Sequence[Leaf]) -> tuple[str, list]:
    """The SQL predicate over ``messages m`` that holds when every leaf
    does, with its bound values; ``"1"`` for no leaves."""
    params: list = []
    sql = " AND ".join(LEAVES[leaf.name].compile(leaf.value, params) for leaf in leaves)
    return sql or "1", params


# The format of ``leaf_digest``'s input. A change to what the digest
# covers, or to the meaning of an expression it covers, bumps it, so a
# cursor issued before is foreign. Cursors from before the version
# existed (#1088) are foreign too. 2: the ``where`` clauses in canonical
# form, with ``any`` and ``negate`` evaluated (#1087). 3: a date bound
# and the ``sent`` order read only dates the message carries (#1080).
QUERY_DIGEST_FORMAT = 3


def leaf_digest(
    leaves: Sequence[Leaf],
    date_basis: str = DEFAULT_DATE_BASIS,
    where: Sequence[WhereLeaf] = (),
) -> str:
    """A short digest of the flat leaf list in order, of the normalized
    ``where`` expression (``normalize_where``) in canonical form, kept
    apart so a flat filter and the explicit leaf it compiles to stay
    distinct, of the ``date_basis`` the page is ordered by and of
    ``QUERY_DIGEST_FORMAT``. The canonical form keeps the order of
    ``all``'s clauses, each clause's leaves (name, value, ``negate``)
    sorted, so the leaf order within an ``any`` group does not change
    the digest; paths and ids, which do not change the matches, are
    left out. It binds a keyset cursor to the predicates
    and the ordering it was issued under (``Database.query_messages``).
    The same leaves under another basis are another keyset (#1085). A
    cursor whose digest differs is rejected as foreign, never read
    against other predicates or another clock."""
    flat = [[leaf.name, leaf.value] for leaf in leaves]
    explicit = [
        sorted(
            ([w.leaf.name, w.leaf.value, w.negate] for w in clause),
            key=json.dumps,
        )
        for clause in where_clauses(where)
    ]
    payload = [QUERY_DIGEST_FORMAT, date_basis, flat, explicit]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# where: the explicit leaves of ``query_messages`` (#1088)
# ---------------------------------------------------------------------------

# The leaves ``where`` accepts; the five address leaves take a role.
WHERE_ADDRESS_LEAVES = (
    "address_is",
    "address_contains",
    "display_name_contains",
    "address_or_name_contains",
    "domain_is",
)
WHERE_LEAVES = (*WHERE_ADDRESS_LEAVES, "body_words")
# The ``ROLE_SETS`` roles a caller names. ``visible_participant`` stays
# internal (the flat ``participant``); the Bcc-inclusive ``recipient``
# and ``any`` arrive with #1090.
WHERE_ROLES = ("from", "to", "cc", "visible_recipient")

# Every leaf and every ``any`` group counts one node.
MAX_WHERE_NODES = 16
# The longest value each leaf takes, checked before it is normalized.
MAX_WHERE_VALUE_CHARS = {
    "address_is": 320,
    "address_contains": 320,
    "display_name_contains": 320,
    "address_or_name_contains": 320,
    "domain_is": 255,
    "body_words": 1000,
}
MAX_WHERE_ID_CHARS = 64

# Whether each explicit address leaf reads display names (so needs them
# all stored to answer no, #1140).
ADDRESS_LEAF_READS_NAMES = {mode: reads for mode, (_, reads) in _ADDRESS_MODES.items()}

WhereLeafName = Literal[
    "address_is",
    "address_contains",
    "display_name_contains",
    "address_or_name_contains",
    "domain_is",
    "body_words",
]
WhereRole = Literal["from", "to", "cc", "visible_recipient"]


# The ``where`` argument models. They carry no docstrings: FastMCP
# serves a model's docstring as schema text wherever the model appears,
# and it inlines the leaf model twice (#818).


class WhereLeafItem(BaseModel):
    # One leaf: ``role`` on the five address leaves only.
    model_config = ConfigDict(extra="forbid")

    leaf: WhereLeafName
    role: WhereRole | None = None
    value: str
    negate: bool = False
    id: str | None = None

    @model_validator(mode="after")
    def _role_fits_the_leaf(self) -> WhereLeafItem:
        if self.leaf in WHERE_ADDRESS_LEAVES:
            if self.role is None:
                raise ValueError("role is required on an address leaf")
        elif "role" in self.model_fields_set:
            raise ValueError("role is not accepted on body_words")
        return self


class WhereAnyGroup(BaseModel):
    # An ``any`` group of leaves (#1087).
    model_config = ConfigDict(extra="forbid")

    # Capped here too, so an oversized list is refused before its items
    # are built; ``normalize_where`` counts the nodes of both together.
    any: list[WhereLeafItem] = Field(max_length=MAX_WHERE_NODES)


class Where(BaseModel):
    # Every item of ``all`` must hold.
    model_config = ConfigDict(extra="forbid")

    all: list[WhereLeafItem | WhereAnyGroup] = Field(max_length=MAX_WHERE_NODES)


@dataclass(frozen=True)
class WhereLeaf:
    """A normalized ``where`` leaf: its request ``path``, the caller's
    ``id``, the ``Leaf`` it compiles to, the index of its ``all``
    clause (a bare leaf, or the ``any`` group holding it) and whether
    it is negated."""

    path: str
    id: str | None
    leaf: Leaf
    clause: int
    negate: bool = False


def where_clauses(where: Sequence[WhereLeaf]) -> list[list[WhereLeaf]]:
    """``where``'s leaves grouped by ``all`` clause, in order: the
    leaves of one clause are ORed, the clauses ANDed."""
    clauses: dict[int, list[WhereLeaf]] = {}
    for w in where:
        clauses.setdefault(w.clause, []).append(w)
    return list(clauses.values())


def compile_where(where: Sequence[WhereLeaf]) -> tuple[str, list]:
    """The SQL predicate over ``messages m`` of the ``where``
    expression, with its bound values: each leaf compiled once,
    ``NOT`` for a negated leaf, ``OR`` within an ``all`` clause and
    ``AND`` between clauses (#1087). Every leaf's SQL is 1, 0 or NULL
    (unknown, ``Evaluability``), and SQL's three-valued operators give
    the decided semantics: ``AND`` is false if any clause is false,
    else unknown if any is unknown; ``OR`` is true if any leaf is true,
    else unknown if any is unknown; ``NOT`` swaps 1 and 0 and keeps
    NULL. ``"1"`` for no leaves."""
    params: list = []
    clauses = []
    for clause in where_clauses(where):
        terms = [
            f"{'NOT ' if w.negate else ''}({LEAVES[w.leaf.name].compile(w.leaf.value, params)})"
            for w in clause
        ]
        clauses.append(terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})")
    return " AND ".join(clauses) or "1", params


def _where_value(path: str, item: WhereLeafItem) -> Any:
    """``item``'s normalized ``Leaf`` value. Raises
    ``InvalidFilterError`` (fixed text) for a value over its leaf's
    limit, a blank one, a non-address ``address_is``, an invalid
    ``domain_is`` or a ``body_words`` without words or with too many."""
    name, value = item.leaf, item.value
    limit = MAX_WHERE_VALUE_CHARS[name]
    if len(value) > limit:
        raise InvalidFilterError("where", f"{path}: {name} value is longer than {limit} characters")
    value = value.strip()
    if not value:
        raise InvalidFilterError("where", f"{path}: {name} value is empty")
    if name == "body_words":
        terms = _text_terms(value)
        if not terms:
            raise InvalidFilterError("where", f"{path}: body_words value holds no word")
        if len(terms) > _MAX_TEXT_TERMS:
            raise InvalidFilterError(
                "where", f"{path}: body_words holds at most {_MAX_TEXT_TERMS} words"
            )
        return tuple(terms)
    if name == "address_is":
        if address_match_mode(value) != "exact":
            raise InvalidFilterError(
                "where", f"{path}: address_is takes a full address (name@example.com)"
            )
        value = canonical_addr(value)
    elif name == "domain_is":
        # The domain of a stored (lowercased) address: no "@", no
        # whitespace, no empty label.
        value = value.lower().removeprefix("@")
        if "@" in value or any(c.isspace() for c in value) or "" in value.split("."):
            raise InvalidFilterError("where", f"{path}: domain_is takes a domain (example.com)")
    return (item.role, value)


def normalize_where(where: Where) -> list[WhereLeaf]:
    """``where``'s leaves, validated and normalized, in order.

    The node cap is checked first, so an oversized expression is refused
    before any value is read. Then, with fixed text, an empty ``all`` or
    an empty ``any`` group, each value (``_where_value``) and each ``id``
    (non-blank, at most ``MAX_WHERE_ID_CHARS``, unique across the
    expression). Every rejection is an ``InvalidFilterError`` on the
    field ``where``. A leaf of an ``any`` group has the path
    ``where.all[i].any[j]``.
    """
    nodes = sum(1 + len(item.any) if isinstance(item, WhereAnyGroup) else 1 for item in where.all)
    if nodes > MAX_WHERE_NODES:
        raise InvalidFilterError(
            "where",
            f"where holds at most {MAX_WHERE_NODES} nodes (each leaf and each any group counts one)",
        )
    if not where.all:
        raise InvalidFilterError("where", "where.all must hold at least one leaf")
    leaves: list[WhereLeaf] = []
    ids: set[str] = set()
    for i, item in enumerate(where.all):
        path = f"where.all[{i}]"
        if isinstance(item, WhereAnyGroup):
            if not item.any:
                raise InvalidFilterError("where", f"{path}: an any group holds at least one leaf")
            members = [(f"{path}.any[{j}]", leaf) for j, leaf in enumerate(item.any)]
        else:
            members = [(path, item)]
        for leaf_path, leaf in members:
            if leaf.id is not None:
                if len(leaf.id) > MAX_WHERE_ID_CHARS:
                    raise InvalidFilterError(
                        "where", f"{leaf_path}: id is longer than {MAX_WHERE_ID_CHARS} characters"
                    )
                if not leaf.id.strip():
                    raise InvalidFilterError("where", f"{leaf_path}: id is empty")
                if leaf.id in ids:
                    raise InvalidFilterError(
                        "where", f"{leaf_path}: id repeats an earlier leaf's id"
                    )
                ids.add(leaf.id)
            value = _where_value(leaf_path, leaf)
            leaves.append(WhereLeaf(leaf_path, leaf.id, Leaf(leaf.leaf, value), i, leaf.negate))
    return leaves


# ---------------------------------------------------------------------------
# Adapters: each call site's parameters as a leaf list
# ---------------------------------------------------------------------------


def _given(value: str | None) -> str | None:
    """``value`` stripped, or ``None`` for a missing or blank filter."""
    return value.strip() if value and value.strip() else None


def _folder_leaf(folders: tuple[str, ...]) -> Leaf:
    """Membership of the named folders; without any, the default scope
    that leaves ``DEFAULT_EXCLUDED_FOLDERS`` out (#441)."""
    if folders:
        return Leaf("folder", folders)
    return Leaf("not_in_folders", DEFAULT_EXCLUDED_FOLDERS)


def _bound_leaves(
    date_from_iso: str | None, date_to_iso: str | None, date_basis: str = DEFAULT_DATE_BASIS
) -> list[Leaf]:
    """The inclusive bounds on the clock ``date_basis`` names, preceded
    under a nullable clock by the ``dated`` leaf that keeps rows without
    that clock out of the ordering."""
    basis = DATE_BASES[date_basis]
    leaves = [Leaf("dated", basis.name)] if basis.nullable else []
    if date_from_iso is not None:
        leaves.append(Leaf(basis.from_leaf, date_from_iso))
    if date_to_iso is not None:
        leaves.append(Leaf(basis.to_leaf, date_to_iso))
    return leaves


def query_messages_leaves(
    *,
    sender: str | None,
    recipient: str | None,
    participant: str | None,
    subject: str | None,
    text: str | None,
    folder: str | None,
    date_from: str | None,
    date_to: str | None,
    has_attachments: bool | None,
    authority_class: str | None,
    seen: bool | None,
    flagged: bool | None,
    replied: bool | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    date_basis: str | None = None,
) -> list[Leaf]:
    """``query_messages``' flat parameters as leaves, blank ones ignored.
    The date bounds are leaves on the clock ``date_basis`` names
    (``DATE_BASES``, default ``effective``; #1085).

    Raises ``InvalidFilterError`` for an invalid date range, an unknown
    authority class, a ``text`` with no words or more than
    ``_MAX_TEXT_TERMS``, an unavailable or unknown ``date_basis``, a
    size bound that is not a non-negative integer, or ``size_min``
    above ``size_max``.
    """
    sender, recipient, participant, subject, text, folder = (
        _given(v) for v in (sender, recipient, participant, subject, text, folder)
    )
    date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
    date_basis = normalize_date_basis(date_basis)
    authority_class = normalize_authority_class(authority_class)
    size_min = normalize_size_bound("size_min", size_min)
    size_max = normalize_size_bound("size_max", size_max)
    if size_min is not None and size_max is not None and size_min > size_max:
        raise InvalidFilterError("size_min/size_max", "size_min must not be greater than size_max")
    leaves = [
        Leaf(name, value)
        for name, value in (
            ("sender", sender),
            ("recipient", recipient),
            ("participant", participant),
        )
        if value
    ]
    if subject:
        leaves.append(Leaf("subject", subject))
    if text:
        terms = _text_terms(text)
        if not terms:
            raise InvalidFilterError("text", "text must contain at least one word")
        if len(terms) > _MAX_TEXT_TERMS:
            raise InvalidFilterError("text", f"text supports at most {_MAX_TEXT_TERMS} words")
        leaves.append(Leaf("text", tuple(terms)))
    leaves.append(_folder_leaf((folder,) if folder else ()))
    leaves += _bound_leaves(date_from_iso, date_to_iso, date_basis)
    for name, value in (
        ("has_attachments", has_attachments),
        ("seen", seen),
        ("flagged", flagged),
        ("replied", replied),
        ("size_min", size_min),
        ("size_max", size_max),
    ):
        if value is not None:
            leaves.append(Leaf(name, value))
    if authority_class:
        leaves.append(Leaf("authority_class", authority_class))
    return leaves


def message_scope_leaves(
    *,
    from_addr: str | None,
    participant: str | None,
    folders: list[str] | None,
    date_from: str | None,
    date_to: str | None,
) -> list[Leaf]:
    """``message_scope``'s parameters as leaves: the sender and
    participant filters, the named folders or the default scope, and
    the effective-time bounds."""
    leaves = []
    if from_addr:
        leaves.append(Leaf("sender", from_addr))
    if participant:
        leaves.append(Leaf("participant", participant))
    leaves.append(_folder_leaf(tuple(folders or ())))
    leaves += _bound_leaves(*_normalize_date_range(date_from, date_to))
    return leaves


def search_emails_leaves(
    *,
    folders: list[str] | None,
    from_addr: str | None,
    date_from: str | None,
    date_to: str | None,
    has_attachments: bool | None,
    participant: str | None,
    authority_class: str | None,
) -> list[Leaf]:
    """The thread filters of ``search_emails`` (and the tools that share
    them) as leaves, in the order ``_apply_filters`` decides them. The
    values are passed as given: the search methods validate the
    authority class and the folder scope before filtering."""
    date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
    leaves = []
    if from_addr:
        leaves.append(Leaf("sender", from_addr))
    if participant:
        leaves.append(Leaf("participant", participant))
    leaves += _bound_leaves(date_from_iso, date_to_iso)
    if has_attachments is not None:
        leaves.append(Leaf("has_attachments", has_attachments))
    if folders:
        leaves.append(Leaf("folder", tuple(folders)))
    if authority_class:
        leaves.append(Leaf("authority_class", authority_class))
    return leaves
