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

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from email.utils import parseaddr
from enum import Enum
from typing import Any


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
# the bound class, outside ``AUTHORITY_EXCLUDED_FOLDERS``. Bind the class
# followed by the excluded folders.
# Driven from ``idx_entities_authority`` into the participant address
# index.
#
# Only a message whose sender attribution is known safe qualifies
# (``messages.sender_ambiguous = 0``, #1144): 1 (a repeated From, or a
# header scan cut short) never does, and neither does NULL, a message
# the indexer has not assessed yet ("can't tell", owner 2026-10-08).
# After the v2 upgrade every message is NULL until the queued reparse
# reaches it, so authority filters match less, then nothing new, until
# it drains; a dead-lettered message stays NULL.
_SENDER_CLASS_MESSAGES = (
    # The f-string adds ``?`` placeholders only; the folders are bound.
    "SELECT p.claimant_id FROM entities e "  # nosec B608
    "JOIN message_participants p ON p.address = e.canonical_key AND p.role = 'from' "
    "JOIN messages am ON am.claimant_id = p.claimant_id "
    "WHERE e.kind = 'person' AND e.authority_class = ? "
    "AND am.sender_ambiguous = 0 "
    f"AND am.folder NOT IN ({','.join('?' * len(AUTHORITY_EXCLUDED_FOLDERS))})"
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
    fragment): case-insensitive substring of the address or display name.
    """
    # Nested-comment input that makes parseaddr recurse canonicalizes to
    # "", so it can only be a substring.
    canonical = canonical_addr(value)
    return "exact" if canonical and not canonical.startswith("@") else "substring"


def _participant_clause(value: str, roles: tuple[str, ...], params: list) -> str:
    """SQL restricting ``messages m`` to those where ``value`` appears in
    one of ``roles``; appends the bound values to ``params``."""
    role_sql = ",".join(["?"] * len(roles))
    if address_match_mode(value) == "exact":
        params.extend([canonical_addr(value), *roles])
        return (
            "m.claimant_id IN (SELECT claimant_id FROM message_participants "  # nosec B608
            f"WHERE address = ? AND role IN ({role_sql}))"
        )
    return (
        "m.claimant_id IN (SELECT claimant_id FROM message_participants "  # nosec B608
        f"WHERE {_substring_participant_rows(value, roles, params)})"
    )


def _substring_participant_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    """SQL selecting the ``message_participants`` rows in ``roles`` whose
    address or display name contains ``value``; appends the bound values
    to ``params``."""
    role_sql = ",".join(["?"] * len(roles))
    # Addresses are stored lowercased; names fold with ``mcp_casefold``.
    params.extend([*roles, value.strip().lower(), value.strip().casefold()])
    return (
        f"role IN ({role_sql}) "  # nosec B608
        "AND (instr(address, ?) > 0 OR instr(mcp_casefold(name), ?) > 0)"
    )


# ---------------------------------------------------------------------------
# Leaves
# ---------------------------------------------------------------------------


class Evaluability(Enum):
    """How a leaf decides a message.

    A placeholder until #1086 lands three-valued evaluation: every leaf
    today reads a field the index stores for every message, so it is
    true or false of each message, never unknown. Declared per leaf so
    the registry test requires it from the start.
    """

    DECIDED = "decided"


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


# Roles each address leaf searches in ``message_participants``.
ADDRESS_ROLES: dict[str, tuple[str, ...]] = {
    "sender": ("from",),
    "recipient": ("to", "cc"),
    "participant": ("from", "to", "cc"),
}


def _compile_sender(value: str, params: list) -> str:
    return _participant_clause(value, ADDRESS_ROLES["sender"], params)


def _compile_recipient(value: str, params: list) -> str:
    return _participant_clause(value, ADDRESS_ROLES["recipient"], params)


def _compile_participant(value: str, params: list) -> str:
    return _participant_clause(value, ADDRESS_ROLES["participant"], params)


def _compile_subject(value: str, params: list) -> str:
    params.append(value.casefold())
    return "instr(mcp_casefold(m.subject), ?) > 0"


def _compile_text(terms: tuple[str, ...], params: list) -> str:
    # One subquery per word, so the words may fall in different chunks
    # of the same message. Each is a quoted FTS phrase; unicode61 never
    # keeps a quote inside a token, but doubling any (FTS5 string
    # escaping) keeps FTS syntax out regardless.
    params.extend('"' + term.replace('"', '""') + '"' for term in terms)
    return " AND ".join(
        "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
        "JOIN message_chunks c ON c.fts_rowid = f.rowid "
        "WHERE message_chunks_fts MATCH ? AND c.attachment_id IS NULL)"
        for _ in terms
    )


def _compile_folder(folders: tuple[str, ...], params: list) -> str:
    params.extend(folders)
    return f"m.folder IN ({','.join('?' * len(folders))})"


def _compile_not_in_folders(folders: tuple[str, ...], params: list) -> str:
    params.extend(folders)
    return f"m.folder NOT IN ({','.join('?' * len(folders))})"


def _compile_effective_from(instant: str, params: list) -> str:
    params.append(instant)
    return "m.effective_at >= ?"


def _compile_effective_to(instant: str, params: list) -> str:
    params.append(instant)
    return "m.effective_at <= ?"


def _compile_flag(column: str) -> Callable[[bool, list], str]:
    """The compiler for a 0/1 column of ``messages`` matched either way."""

    def compile(state: bool, params: list) -> str:
        params.append(1 if state else 0)
        return f"m.{column} = ?"

    return compile


def _compile_authority_class(value: str, params: list) -> str:
    params.extend([value, *AUTHORITY_EXCLUDED_FOLDERS])
    return f"m.claimant_id IN ({_SENDER_CLASS_MESSAGES})"


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
        LeafKind("sender", "address", _compile_sender, Evaluability.DECIDED, _sender_test),
        LeafKind("recipient", "address", _compile_recipient, Evaluability.DECIDED),
        LeafKind(
            "participant", "address", _compile_participant, Evaluability.DECIDED, _participant_test
        ),
        LeafKind("subject", "text", _compile_subject, Evaluability.DECIDED),
        LeafKind("text", "words", _compile_text, Evaluability.DECIDED),
        LeafKind("folder", "folders", _compile_folder, Evaluability.DECIDED),
        LeafKind("not_in_folders", "folders", _compile_not_in_folders, Evaluability.DECIDED),
        LeafKind(
            "effective_from",
            "instant",
            _compile_effective_from,
            Evaluability.DECIDED,
            _effective_from_test,
        ),
        LeafKind(
            "effective_to",
            "instant",
            _compile_effective_to,
            Evaluability.DECIDED,
            _effective_to_test,
        ),
        LeafKind(
            "has_attachments",
            "bool",
            _compile_flag("has_attachments"),
            Evaluability.DECIDED,
            _has_attachments_test,
        ),
        LeafKind("seen", "bool", _compile_flag("seen"), Evaluability.DECIDED),
        LeafKind("flagged", "bool", _compile_flag("flagged"), Evaluability.DECIDED),
        LeafKind("authority_class", "class", _compile_authority_class, Evaluability.DECIDED),
    )
}


def compile_leaves(leaves: Sequence[Leaf]) -> tuple[str, list]:
    """The SQL predicate over ``messages m`` that holds when every leaf
    does, with its bound values; ``"1"`` for no leaves."""
    params: list = []
    sql = " AND ".join(LEAVES[leaf.name].compile(leaf.value, params) for leaf in leaves)
    return sql or "1", params


def leaf_digest(leaves: Sequence[Leaf]) -> str:
    """A short digest of the leaf list, in order, binding a keyset cursor
    to the predicates it was issued under (``Database.query_messages``).
    A cursor whose digest differs is rejected as foreign, never read
    against other predicates."""
    canonical = [[leaf.name, leaf.value] for leaf in leaves]
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()[:16]


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


def _bound_leaves(date_from_iso: str | None, date_to_iso: str | None) -> list[Leaf]:
    leaves = []
    if date_from_iso is not None:
        leaves.append(Leaf("effective_from", date_from_iso))
    if date_to_iso is not None:
        leaves.append(Leaf("effective_to", date_to_iso))
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
) -> list[Leaf]:
    """``query_messages``' flat parameters as leaves, blank ones ignored.

    Raises ``InvalidFilterError`` for an invalid date range, an unknown
    authority class, or a ``text`` with no words or more than
    ``_MAX_TEXT_TERMS``.
    """
    sender, recipient, participant, subject, text, folder = (
        _given(v) for v in (sender, recipient, participant, subject, text, folder)
    )
    date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
    authority_class = normalize_authority_class(authority_class)
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
    leaves += _bound_leaves(date_from_iso, date_to_iso)
    for name, state in (("has_attachments", has_attachments), ("seen", seen), ("flagged", flagged)):
        if state is not None:
            leaves.append(Leaf(name, state))
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
