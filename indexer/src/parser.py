"""
Email parser.
Reads raw .eml files from Maildir and returns structured Message objects.
Handles MIME, HTML-to-text conversion, and attachment metadata.
"""

import base64
import binascii
import email
import email.errors
import email.header
import email.message
import email.parser
import email.utils
import hashlib
import logging
import os
import quopri
import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import html2text

from .extractors import (
    note_parser_address_repeats,
    note_parser_caps_message,
    resolved_extractor_module,
    warn_rate_limited,
)
from .maildir import parse_flags

log = logging.getLogger("indexer.parser")

# The per-message work caps that drop content, by the fixed name the
# parse logs them under (#872), in log order. Each counts the parts,
# headers or addresses it dropped from one message. A container
# attachment counts only when content is lost (review round 1 on #884):
# its decoded tree goes unwalked (the attachments inside a transfer-
# encoded attached email), or its emptied payload is one an extractor
# would read (a container named ``.txt``). An identity-encoded container
# is still walked, so the attachments inside it are kept.
#
# * ``attached_depth`` / ``attached_fields``: a container attachment
#   past ``MAX_ATTACHED_MESSAGE_DEPTH`` or the message's
#   ``MAX_ATTACHED_MESSAGE_FIELDS`` budget;
# * ``transport_decode``: a transfer-encoded attached email that does
#   not decode;
# * ``decoded_bytes``: one past ``MAX_DECODED_ATTACHMENT_BYTES``;
# * ``container_serialize``: a container the generator refuses;
# * ``body_parts``: text parts past ``MAX_BODY_TEXT_PARTS`` are left out
#   of the body;
# * ``mime_parts``: parts past the first ``MAX_WALKED_PARTS`` in
#   document order are left out of the walk (their text and
#   attachments); counted once per message (#996);
# * ``address_header``: an address header over
#   ``_MAX_ADDRESS_HEADER_CHARS`` loses all its recipients;
# * ``address_element`` / ``address_length``: one address-list element
#   over ``_MAX_ADDRESS_ELEMENT_CHARS``, or an address over
#   ``_MAX_ADDRESS_CHARS``, is dropped;
# * ``address_fields``: the scan for From / To / Cc stopped at
#   ``MAX_ADDRESS_HEADER_FIELDS`` header fields, so any later one is
#   lost and a second From cannot be ruled out (the sender is ambiguous);
# * ``address_occurrences`` / ``address_chars``: a From / To / Cc
#   occurrence past ``MAX_ADDRESS_OCCURRENCES``, or past
#   ``_MAX_ADDRESS_TOTAL_CHARS`` raw characters in all, loses its
#   recipients, unread;
# * ``address_elements`` / ``address_count``: address-list elements past
#   ``MAX_ADDRESS_ELEMENTS``, or past ``MAX_MESSAGE_ADDRESSES`` kept
#   addresses, are dropped unparsed (#1144);
# * ``address_unparsed``: an address-list element that yields no address
#   a participant row can store (none at all, one without ``@``, one that
#   does not parse back to itself, or a parse that raised) is dropped
#   (#1086);
# * ``participant_names``: a display name past the first for its (role,
#   address), past the ``MAX_EXTRA_PARTICIPANT_*`` budget, is not stored
#   (#1140); the address and its first name are kept;
# * ``subject_length``: a decoded subject over ``SUBJECT_MAX_CHARS`` is
#   cut to the cap (#902);
# * ``in_reply_to_length`` / ``references_length``: an In-Reply-To, or
#   a References entry, over ``MESSAGE_ID_MAX_CHARS`` is dropped, so
#   threading sees the rest (#902).
#
# Two names count repeated headers, which lose nothing (#1144) and are
# logged on their own lines, not the cap line (``PARSE_REPEATS``):
#
# * ``address_repeated``: a To or Cc header after the first of its name,
#   merged into that role (recovered);
# * ``from_repeated``: a From header after the first, counted and never
#   parsed: the first is kept and the sender flagged ambiguous.
PARSE_CAPS: tuple[str, ...] = (
    "attached_depth",
    "attached_fields",
    "transport_decode",
    "decoded_bytes",
    "container_serialize",
    "body_parts",
    "mime_parts",
    "address_header",
    "address_element",
    "address_length",
    "address_fields",
    "address_occurrences",
    "address_chars",
    "address_elements",
    "address_count",
    "address_unparsed",
    "participant_names",
    "subject_length",
    "in_reply_to_length",
    "references_length",
    "address_repeated",
    "from_repeated",
)
PARSE_REPEATS: tuple[str, ...] = ("address_repeated", "from_repeated")

# The ``PARSE_CAPS`` names whose content a filter reads (#1086): a
# message they fired on is stored with that content incomplete, so its
# completeness flag is cleared (``messages.*_complete``). The address
# names are attributed to the role (From, To, Cc) being read when they
# fire; ``address_fields`` stops the scan before any role is read and
# clears all three.
BODY_LOSS_CAPS: tuple[str, ...] = ("body_parts", "mime_parts")
ATTACHMENT_LOSS_CAPS: tuple[str, ...] = (
    "attached_depth",
    "attached_fields",
    "transport_decode",
    "decoded_bytes",
    "container_serialize",
    "mime_parts",
)
ADDRESS_LOSS_CAPS: tuple[str, ...] = (
    "address_header",
    "address_element",
    "address_length",
    "address_occurrences",
    "address_chars",
    "address_elements",
    "address_count",
    "address_unparsed",
)


class OversizedMessageError(Exception):
    """Raised when an ``.eml`` exceeds ``INDEXER_PARSE_MAX_BYTES``.

    Distinct from the parser's other ``None``-return paths (no
    Message-ID, etc.) so the indexer worker can dead-letter oversized
    files terminally (``mark_dead_terminal``) rather than
    ``mark_succeeded`` — the file was never indexed. The dead row is
    durable, so startup discovery does not re-enqueue the same file on
    every restart, and it stays visible in ``queue.stats()['dead']``
    until an operator raises the cap and runs ``make requeue-dead``.
    """

    def __init__(self, path: Path, size: int, cap: int) -> None:
        super().__init__(
            f"oversized email {path} ({size} bytes > {cap} cap); "
            "raise INDEXER_PARSE_MAX_BYTES to ingest, or 0 to disable."
        )
        self.path = path
        self.size = size
        self.cap = cap


class MessageNestingError(Exception):
    """Raised when the stdlib email parser hits the recursion limit.

    ``email.message_from_bytes`` recurses once per nested
    ``message/rfc822`` (or multipart) level, so a small crafted message
    nested about a thousand levels deep raises ``RecursionError`` on
    every attempt. The worker dead-letters it terminally
    (``mark_dead_terminal``) instead of retrying the same failure (#1296).
    The text is fixed: it never quotes the message.
    """

    TEXT = "message nested too deeply for the email parser"

    def __init__(self) -> None:
        super().__init__(self.TEXT)


# Hard ceiling on the size of a single ``.eml`` we will read into
# memory. Bridge inbound usually caps at ~25 MB, but a malicious /
# corrupt Maildir file with no such bound would otherwise let a
# single ``read_bytes`` call exhaust the indexer container's memory
# (default ``mem_limit: 6g``). 50 MB is comfortably above any
# legitimate message and well below the container ceiling. ``0``
# disables the cap; operators on environments with larger inbound
# limits can raise it via ``INDEXER_PARSE_MAX_BYTES``.
_DEFAULT_PARSE_MAX_BYTES = 50_000_000


# Longest subject stored (#541), in decoded characters. ``parse_email``
# cuts the decoded Subject here, so ``messages.subject``,
# ``threads.subject`` / ``display_subject`` and every reader of them
# (the ``threads_fts`` subject scan, the rerank reply-subject scan,
# subject-fallback threading) handle at most this many characters per
# row: a ``substr()`` in SQL bounds only what it returns, not what
# SQLite reads. RFC 5322 limits a header line to 998 characters and a
# real subject is far shorter, so the cap only touches crafted mail.
# Subject-fallback threading compares the capped values: two subjects
# that agree on their first ``SUBJECT_MAX_CHARS`` characters match,
# and a ``Re:`` reply to a subject longer than the cap does not.
SUBJECT_MAX_CHARS = 2000

# Stored as the subject of a message with no Subject header. A literal
# "(no subject)" header parses to the same value and cannot be told apart.
NO_SUBJECT = "(no subject)"

# Longest Message-ID accepted, in characters, counted after the
# surrounding whitespace and angle brackets are removed (``_clean_id``):
# the value stored and used as a thread ID. RFC 5322 caps a line at 998
# characters, so no conforming ID is longer. A message whose own ID is
# longer is unindexable, like one without a Message-ID, so a crafted
# root ID cannot become an unbounded thread ID; longer In-Reply-To and
# References entries are dropped, since they can never match an
# indexed ID.
MESSAGE_ID_MAX_CHARS = 998

# Longest date text read from the top ``Received:`` header, in
# characters. ``occurred_at`` is the text after the header's last ``;``
# (RFC 5321 puts the date there), so only the header's last
# ``RECEIVED_DATE_MAX_CHARS`` characters are searched for that ``;``:
# a real date with its zone comment is under 60 characters, and a
# crafted multi-megabyte header costs one slice instead of a scan.
# A ``;`` further back means a date text longer than the cap, which is
# read as unparseable (``None``).
RECEIVED_DATE_MAX_CHARS = 256

# Most bytes ``message_sort_time`` reads from one file, in chunks of
# ``_SORT_READ_CHUNK``, stopping at the blank line that ends the
# headers. Proton's own headers (ARC, DKIM, its ``Received:`` chain) fit
# well inside; a header block longer than this is read up to the cap.
SORT_HEADER_MAX_BYTES = 64 * 1024
_SORT_READ_CHUNK = 8 * 1024


def _parse_max_bytes() -> int:
    raw = os.environ.get("INDEXER_PARSE_MAX_BYTES", "").strip()
    if not raw:
        return _DEFAULT_PARSE_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "invalid INDEXER_PARSE_MAX_BYTES=%r; falling back to %d",
            raw,
            _DEFAULT_PARSE_MAX_BYTES,
        )
        return _DEFAULT_PARSE_MAX_BYTES
    # ``0`` is the explicit "disable the cap" sentinel. Negative values
    # are a typo / misconfiguration — not a second opt-out — so fall
    # back rather than silently collapsing them to ``0`` (the old
    # ``max(0, value)`` did, which let ``-1`` quietly disable the OOM
    # protection on every read).
    if value < 0:
        log.warning(
            "invalid INDEXER_PARSE_MAX_BYTES=%r (negative); falling back to %d",
            raw,
            _DEFAULT_PARSE_MAX_BYTES,
        )
        return _DEFAULT_PARSE_MAX_BYTES
    return value


def _html_to_text(html: str) -> str:
    """Render HTML to text with a fresh converter.

    ``HTML2Text`` keeps parser state between ``handle`` calls, so a
    shared instance let one message's unclosed ``<style>`` blank every
    later HTML body until some document closed it.
    """
    h2t = html2text.HTML2Text()
    h2t.ignore_links = True
    h2t.ignore_images = True
    h2t.body_width = 0
    return h2t.handle(html)


def _decoded_payload(part: Any) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


# The defects a lenient base64 decode records when it loses bytes: an
# invalid character skipped, or a truncated quantum (the stdlib then
# returns the undecoded text). A padding defect alone loses nothing.
_DECODE_LOSS_DEFECTS = (
    email.errors.InvalidBase64CharactersDefect,
    email.errors.InvalidBase64LengthDefect,
)


def _decode_lost_bytes(part: email.message.Message) -> bool:
    """Whether decoding this leaf part's payload (``_decoded_payload``,
    which fills ``part.defects``) lost bytes (#1242, review round 2 on
    #1286). Quoted-printable and uuencode failures record no defect, so
    they are not detected."""
    return any(isinstance(d, _DECODE_LOSS_DEFECTS) for d in part.defects)


def _base64_transport_lost(data: bytes) -> bool:
    """Whether a container's base64 transport text loses bytes when
    decoded (#1242, review round 4 on #1286). ``_decode_transport_form``
    decodes it leniently (``base64.b64decode`` drops characters outside
    the alphabet), and its bytes are kept; this decodes the same text once
    more through the stdlib leaf decoder, only to read the defects it
    records, and discards that output. One linear pass, run only on a
    transport the decodable-bytes budget already admitted."""
    pseudo = email.message.Message()
    pseudo["Content-Transfer-Encoding"] = "base64"
    pseudo.set_payload(data.decode("ascii", "surrogateescape"))
    pseudo.get_payload(decode=True)
    return _decode_lost_bytes(pseudo)


@dataclass
class Attachment:
    """One MIME attachment from a message.

    ``payload`` holds the raw decoded bytes for the lifetime of the
    indexer pass — the extractor needs them to pull text out of PDFs,
    DOCX, images, etc. They are not persisted anywhere; once the
    indexer has chunked + embedded any extracted text, the Attachment
    object goes out of scope and the bytes are GC'd. Callers that only
    need metadata can ignore ``payload``.

    ``content_hash`` is the SHA-256 of ``payload`` and, with the
    extractor module the label selects, the deduplication key in
    ``attachment_extractions`` — a forwarded PDF is OCR'd / parsed once
    per content, regardless of how many emails carry it.

    ``payload_complete`` is False for a container attachment whose body
    was not serialized (``_attachment_payload`` kept the empty payload:
    a parse cap, a failure, or a container nested inside another
    attachment), and for a leaf whose base64 decode lost bytes
    (``_decode_lost_bytes``), so the bytes an extractor reads are not
    the attachment's (#1242).
    """

    filename: str
    content_type: str
    size: int
    payload: bytes = b""
    content_hash: str = ""
    payload_complete: bool = True


# Hex digits of the file hash in a claimant ID (see ``claimant_id``).
# 64 bits: a 32-bit prefix could be matched by a crafted file (#454).
CLAIMANT_HASH_CHARS = 16


def claimant_id(message_id: str, content_hash: str | None) -> str:
    """The per-message key: ``"<Message-ID>#<hash prefix>"`` (#217).

    A Message-ID is sender-controlled, so two different files can claim
    the same one. Both are kept, each keyed by the Message-ID plus the
    first ``CLAIMANT_HASH_CHARS`` hex digits of the SHA-256 of the
    file's raw bytes (``Message.content_hash``). The bytes are the
    identity because they are the one input that does not change while
    the file lives: Maildir flags and the delivery name are in the
    filename and the folder is the directory, so a flag rename, a folder
    move and a reparse (even by a changed parser) all keep the key,
    while any difference in content gives a new one. A byte-identical
    copy of a message (the same mail filed twice) shares its key, as it
    did when the key was the bare Message-ID.

    A ``Message`` built without file identity (only in tests) keys on
    the bare Message-ID.
    """
    if not content_hash:
        return message_id
    return f"{message_id}#{content_hash[:CLAIMANT_HASH_CHARS]}"


@dataclass
class Message:
    message_id: str
    in_reply_to: str | None
    references: list[str]
    subject: str
    from_addr: str
    to_addrs: list[str]
    cc_addrs: list[str]
    date: datetime
    body_text: str
    folder: str
    filepath: str
    attachments: list[Attachment] = field(default_factory=list)
    has_attachments: bool = False
    # Every author in From, each a parseable address string. Usually one;
    # RFC 5322 allows several. ``from_addr`` stays the first author for the
    # thread-level sender lists.
    from_addrs: list[str] = field(default_factory=list)
    # File identity captured at parse time. ``size`` / ``mtime_ns``
    # / ``content_hash`` feed ``indexed_files`` so the reconciler can tell a
    # flag-only rename from a genuine content change without re-reading every
    # file from disk. Defaults to ``None`` for Messages built by test
    # fixtures that do not round-trip through ``parse_email``.
    size: int | None = None
    mtime_ns: int | None = None
    content_hash: str | None = None
    # True when ``date`` is the parser's current-time fallback for a
    # missing or unparseable Date header. Reprocessing keeps the date
    # already persisted for the message instead (#297), so an undated
    # message is not re-dated every time it is parsed.
    date_is_fallback: bool = False
    # Delivery time: the date of the topmost ``Received:`` header, in
    # UTC (``_parse_received_date``). ``None`` when the header is absent
    # (sent mail) or its date is unparseable; never taken from ``Date:``.
    occurred_at: datetime | None = None
    # True when the message repeats its From header, or the header scan
    # stopped with fields left unread, so a second one cannot be ruled
    # out (#1144). RFC 5322 leaves repeated From headers undefined and
    # they are a spoofing vector, so ``from_addr`` / ``from_addrs`` are
    # the first header's only and nothing may trust them as the author:
    # stored as ``messages.sender_ambiguous``, it keeps the message out
    # of source authority, and the threader skips the sender-dependent
    # subject fallback.
    sender_ambiguous: bool = False
    # Every distinct display name per (role, address), as
    # ``participant_names`` returns them (#1140), stored in
    # ``message_participant_names``. ``None`` for a Message built without
    # parsing (only in tests); the writer then derives them itself.
    participant_names: list[tuple[str, str, str]] | None = None
    # False when the ``MAX_EXTRA_PARTICIPANT_*`` budget dropped a name
    # from ``participant_names`` (#1140), stored as
    # ``messages.participant_names_complete``. Read only with
    # ``participant_names``.
    participant_names_complete: bool = True
    # Per-message completeness (#1086), each stored as a
    # ``messages.*_complete`` column: False when a parse cap or a
    # rejected element lost part of that content (``BODY_LOSS_CAPS``,
    # ``ATTACHMENT_LOSS_CAPS``, ``ADDRESS_LOSS_CAPS`` by role, a
    # repeated From, ``subject_length``). A filter that finds nothing in
    # the stored content can answer "no" only when its flag is True.
    # ``body_complete`` is stored with the body chunks (phase 2c), the
    # others with the message row.
    subject_complete: bool = True
    from_addresses_complete: bool = True
    to_addresses_complete: bool = True
    cc_addresses_complete: bool = True
    attachments_manifest_complete: bool = True
    body_complete: bool = True
    # The parse's nonzero ``PARSE_CAPS`` counts by name, in
    # ``PARSE_CAPS`` order: fixed names and integers only, stored as
    # ``messages.caps_json``.
    parse_caps: dict[str, int] = field(default_factory=dict)

    @property
    def effective_date(self) -> datetime:
        """The message's effective time: ``occurred_at``, else ``date``.

        Date filters and thread spans use it, matching the
        ``messages.effective_at`` column.
        """
        return self.occurred_at if self.occurred_at is not None else self.date

    @property
    def claimant_id(self) -> str:
        """This file's per-message key; see the module-level ``claimant_id``."""
        return claimant_id(self.message_id, self.content_hash)


@dataclass(frozen=True)
class SourceMetadata:
    """What the source adapter knows about a message beyond its bytes
    (#1077). The Maildir adapter, ``parse_email``, fills it from the
    file; ``parse_email_bytes`` copies ``folder``, ``path`` and
    ``mtime_ns`` onto the ``Message`` and names ``path`` in its log
    lines, and derives nothing from any field.
    """

    # The folder the message is filed in (``_derive_folder``).
    folder: str
    # The Maildir flag letters in the file name (``maildir.parse_flags``);
    # empty for a ``new/`` delivery.
    flags: frozenset[str]
    # The size in bytes of the file as read.
    size: int
    # The file's modification time, or ``None`` when ``stat`` failed.
    mtime_ns: int | None
    # The file's path, for log lines and the ``Message.filepath`` record.
    path: str


def parse_email(path: Path, maildir_root: Path | None = None) -> Message | None:
    """Parse a single .eml file from Maildir into a Message object.

    The Maildir adapter (#1077): reads the file under the size cap,
    builds the ``SourceMetadata`` the file gives (folder, flags, size,
    mtime, path) and hands both to ``parse_email_bytes``, which does
    the parsing and reads nothing from disk.

    ``maildir_root`` — when provided, the folder is derived as the relative
    path from the root to the directory that contains ``cur/``/``new/``.
    mbsync's ``SubFolders Legacy`` layout nests folders more than one level
    deep as dotted child directories (``Folders/.Clients/.ABC`` is
    ``Folders/Clients/ABC``; see ``_derive_folder``); without the root,
    nested folders collapse to only the leaf directory name and unrelated
    threads can be merged by the subject-only fallback.

    When ``maildir_root`` is not provided the folder falls back to the
    leaf name (``path.parent.parent.name``).

    Transient I/O errors (``PermissionError`` from the mbsync 0600→0644
    chmod race, ``FileNotFoundError`` from a rename mid-event) propagate
    so the worker's queue routes them to the retry/backoff path rather
    than collapsing them into ``None`` — which the worker treats as a
    permanent "no Message-ID" outcome and dead-letters without retry.
    Content-pathology errors propagate from ``parse_email_bytes``; see
    there.

    Files larger than ``INDEXER_PARSE_MAX_BYTES`` (default 50 MB) raise
    ``OversizedMessageError`` either at the fstat pre-check or while
    reading — whichever fires first — so a malicious or corrupt Maildir
    entry cannot exhaust container memory even if the file grew between
    fstat and read or fstat itself failed. The worker catches the
    error and dead-letters the row through ``mark_dead_terminal`` —
    terminal, no retry, and kept as a durable dead row rather than a
    silent success-deletion.
    """
    # Open the file once, fstat that descriptor, and bound the read on
    # the same fd. Opening first means the cap check and the read see
    # the same inode regardless of any rename / unlink / replace that
    # happens after we entered the function — closing the
    # stat-then-read TOCTOU window. The bounded ``f.read(cap + 1)``
    # below also catches the case where fstat reports a size <= cap but
    # the file grows after the check, and the case where fstat itself
    # raised (size unknown but read still bounded).
    cap = _parse_max_bytes()
    with path.open("rb") as f:
        try:
            stat = os.fstat(f.fileno())
        except OSError:
            stat = None

        if cap > 0 and stat is not None and stat.st_size > cap:
            log.warning(
                "Not parsing oversized email %s (%d bytes > %d cap); raise "
                "INDEXER_PARSE_MAX_BYTES (or 0 to disable), recreate the indexer, "
                "then run make requeue-dead if it was dead-lettered.",
                path,
                stat.st_size,
                cap,
            )
            raise OversizedMessageError(path, stat.st_size, cap)

        if cap > 0:
            # Read at most cap+1 bytes so a file that grew between
            # fstat and read (or that fstat could not size) cannot
            # exceed the memory budget. cap+1 lets us positively
            # detect the over-cap case.
            raw = f.read(cap + 1)
            if len(raw) > cap:
                # If the file grew between fstat and read, ``stat.st_size``
                # can be stale and even <= cap, which would make the
                # dead-letter message misleading. ``len(raw)`` is the
                # lower bound on the actual file size (we capped the read
                # at cap+1, so a file >= cap+1 reads exactly cap+1 bytes);
                # taking the max with ``stat.st_size`` keeps the larger of
                # the two so the operator-visible size is never an
                # under-report relative to either signal.
                actual = max(stat.st_size, len(raw)) if stat is not None else len(raw)
                log.warning(
                    "Not parsing oversized email %s (>%d cap, detected during read); raise "
                    "INDEXER_PARSE_MAX_BYTES (or 0 to disable), recreate the indexer, "
                    "then run make requeue-dead if it was dead-lettered.",
                    path,
                    cap,
                )
                raise OversizedMessageError(path, actual, cap)
        else:
            raw = f.read()
    source = SourceMetadata(
        folder=_derive_folder(path, maildir_root),
        flags=frozenset(parse_flags(path)),
        size=len(raw),
        # ``mtime_ns`` reuses the ``stat`` captured above for the size
        # cap check. A ``stat`` failure is treated as "identity unknown"
        # rather than a parse failure: the file was just read
        # successfully, so the row still belongs in the index. Future
        # passes can backfill.
        mtime_ns=stat.st_mtime_ns if stat is not None else None,
        path=str(path),
    )
    return parse_email_bytes(raw, source)


def parse_email_bytes(raw: bytes, source: SourceMetadata) -> Message | None:
    """Parse an RFC 822 message from its bytes into a Message object.

    The pure half of ``parse_email`` (#1077): no filesystem access, so
    a parser test needs no file and another source adapter can plug in
    here. ``source`` is what the adapter knows beyond the bytes; its
    ``path`` names the message in log lines (never any of its content).

    Returns ``None`` for a message without a usable Message-ID (none, or
    one over ``MESSAGE_ID_MAX_CHARS``), which the worker dead-letters.

    Raises ``MessageNestingError`` when the stdlib parse itself hits the
    recursion limit (a message nested too deeply), which the worker
    dead-letters terminally.

    Other content-pathology errors (a malformed MIME structure ``email`` cannot
    decompose, an html2text blowup, anything raised by the body /
    attachment walker that is not already caught locally) propagate.
    The previous bare ``except Exception`` collapsed those into the same
    ``None`` channel as missing-Message-ID, which silently un-indexed
    every affected file with no dead-letter visibility. Letting the
    exception escape routes the row through the queue's retry +
    dead-letter cascade so operators see persistent parser bugs instead
    of a quietly shrinking index.
    """
    path = source.path
    try:
        msg = email.message_from_bytes(raw)
    except RecursionError:
        # Deterministic for these bytes, so retrying cannot help (#1296).
        # Only this call is the boundary: a ``RecursionError`` from the
        # walk below keeps the ordinary retry path.
        raise MessageNestingError() from None

    message_id = _clean_id(msg.get("Message-ID", ""))
    if not message_id:
        log.debug(f"Skipping message with no Message-ID: {path}")
        return None
    if len(message_id) > MESSAGE_ID_MAX_CHARS:
        # Length only: the ID is sender-controlled.
        log.debug(
            "Skipping message with a Message-ID over %d characters (%d): %s",
            MESSAGE_ID_MAX_CHARS,
            len(message_id),
            path,
        )
        return None

    # Work caps that drop content, counted by ``PARSE_CAPS`` name and
    # logged once below.
    caps: Counter[str] = Counter()
    in_reply_to = _clean_id(msg.get("In-Reply-To", ""))
    if len(in_reply_to) > MESSAGE_ID_MAX_CHARS:
        caps["in_reply_to_length"] += 1
        in_reply_to = ""
    references: list[str] = []
    for ref in (_clean_id(r) for r in msg.get("References", "").split() if r.strip()):
        if len(ref) <= MESSAGE_ID_MAX_CHARS:
            references.append(ref)
        else:
            caps["references_length"] += 1
    subject = _decode_text_header(msg.get("Subject", NO_SUBJECT))
    if len(subject) > SUBJECT_MAX_CHARS:
        caps["subject_length"] += 1
        subject = subject[:SUBJECT_MAX_CHARS]
    # Parse From structurally, like To / Cc: decoding the whole header
    # first turns an encoded name with a comma ("=?utf-8?q?Doe=2C_Jane?=")
    # into an unquoted "Doe, Jane <...>" that no longer parses as one
    # address, and a multi-author From would be read as a single address.
    addresses = _read_address_headers(msg, caps)
    names = participant_names(
        (
            ("from", addresses.from_addrs or [addresses.from_addr]),
            ("to", addresses.to_addrs),
            ("cc", addresses.cc_addrs),
        ),
        caps,
    )
    # A raw 8-bit Date header comes back as an ``email.header.Header``,
    # which ``parsedate_to_datetime`` cannot split (#361). A Date header
    # is ASCII by RFC 5322, so drop anything else from its text: a
    # stray byte glued to the zone ("+0500\xe9") would otherwise hide
    # the offset and the time would be read as UTC.
    date_text = str(msg.get("Date", "")).encode("ascii", "ignore").decode("ascii")
    parsed_date = _parse_date(date_text)
    date = parsed_date if parsed_date is not None else datetime.now(UTC)
    occurred_at = _parse_received_date(msg)

    body_text, attachments = _extract_body_and_attachments(msg, caps=caps)
    lost = [name for name in PARSE_CAPS if caps[name] and name not in PARSE_REPEATS]
    if lost:
        # Fixed names and counts only, with the Maildir path: the
        # message is indexed with this content missing (#872), so
        # WARNING. Rate limited with the extractors' per-item lines, and
        # every such message counted for the attachments aggregate
        # (review round 5 on #884).
        note_parser_caps_message()
        warn_rate_limited(
            log,
            "parser work caps dropped content from %s: %s",
            path,
            ",".join(f"{name}={caps[name]}" for name in lost),
        )
    if caps["address_repeated"] or addresses.sender_ambiguous:
        note_parser_address_repeats(
            merged=bool(caps["address_repeated"]), ambiguous=addresses.sender_ambiguous
        )
    if caps["from_repeated"]:
        # Counts and the path only (#1144): the repeated From is a known
        # spoofing shape, and the flag it sets changes authority and
        # threading.
        warn_rate_limited(
            log,
            "parser kept the first of %d From headers in %s; sender ambiguous, "
            "no source authority or subject-fallback threading",
            caps["from_repeated"] + 1,
            path,
            attachment=False,
        )
    if caps["address_repeated"]:
        warn_rate_limited(
            log,
            "parser merged %d repeated To/Cc headers in %s",
            caps["address_repeated"],
            path,
            level=logging.INFO,
            attachment=False,
        )

    # Capture file identity. ``size`` is the length of the
    # bytes we actually hashed; ``content_hash`` is computed over the
    # raw file — not the decoded body — so flag-only renames keep the
    # same hash while any real content mutation shows up as a mismatch.
    # ``mtime_ns`` is the adapter's (see ``parse_email``).
    size = len(raw)
    content_hash = hashlib.sha256(raw).hexdigest()

    return Message(
        message_id=message_id,
        in_reply_to=in_reply_to or None,
        references=references,
        subject=subject,
        from_addr=addresses.from_addr,
        from_addrs=addresses.from_addrs,
        to_addrs=addresses.to_addrs,
        cc_addrs=addresses.cc_addrs,
        sender_ambiguous=addresses.sender_ambiguous,
        participant_names=names,
        participant_names_complete=not caps["participant_names"],
        subject_complete=not caps["subject_length"],
        from_addresses_complete=addresses.from_complete,
        to_addresses_complete=addresses.to_complete,
        cc_addresses_complete=addresses.cc_complete,
        attachments_manifest_complete=not any(caps[name] for name in ATTACHMENT_LOSS_CAPS),
        body_complete=not any(caps[name] for name in BODY_LOSS_CAPS),
        parse_caps={name: caps[name] for name in PARSE_CAPS if caps[name]},
        date=date,
        date_is_fallback=parsed_date is None,
        occurred_at=occurred_at,
        body_text=body_text,
        folder=source.folder,
        filepath=path,
        attachments=attachments,
        has_attachments=len(attachments) > 0,
        size=size,
        mtime_ns=source.mtime_ns,
        content_hash=content_hash,
    )


def _derive_folder(path: Path, maildir_root: Path | None) -> str:
    """Derive a Maildir folder name for a message path.

    With a root provided, returns the folder name from the path below the
    root to the directory containing ``cur/``/``new/``. mbsync writes child
    folders with ``SubFolders Legacy`` (#281): a top-level folder is the
    directory of that name, and each later component gets one leading dot,
    which is removed here. So ``/maildir/Folders/.Clients/.cur/cur/msg``
    is ``Folders/Clients/cur``. A component without the dot is kept as it
    is. Falls back to the leaf directory name when the path is outside the
    root (for tests that do not pass a root).
    """
    folder_dir = path.parent.parent
    if maildir_root is not None:
        try:
            relative = folder_dir.relative_to(maildir_root)
        except ValueError:
            pass
        else:
            if not relative.parts:
                return relative.as_posix()
            first, *rest = relative.parts
            return "/".join([first, *(c.removeprefix(".") for c in rest)])
    return folder_dir.name


# RFC 5322 2.2.3 folding: a line break followed by a space or tab. One
# linear pass, no backtracking.
_FOLD_RE = re.compile(r"\r?\n(?=[ \t])")


def _unfold(text: str) -> str:
    """Unfold header text (RFC 5322 2.2.3): remove each line break that
    is followed by a space or tab, keeping the whitespace.

    The standard library keeps a fold that falls inside a quoted string,
    a comment or a parameter value, so a display name or filename folded
    there would keep the line break (#688).
    """
    return _FOLD_RE.sub("", text)


def _part_filename(part: email.message.Message, degraded: Counter[str] | None = None) -> str | None:
    """``_raw_part_filename`` read from the part's headers unfolded (#688).

    The headers are unfolded before ``get_filename()`` decodes them, so a
    line break an RFC 2231 value percent-encodes (``%0A%20``) stays
    filename content; only syntactic folds are removed. The unfolded
    Content-Disposition and Content-Type values go on a throwaway message,
    copied raw (``raw_items()``; the compat32 policy stores and fetches
    them unchanged), so the part itself is never modified: attached emails
    are re-serialized later. ``degraded`` (when given) counts the
    fallbacks below, as for ``_decode_header``.
    """
    headers = email.message.Message()
    for name, value in part.raw_items():
        if name.lower() in ("content-disposition", "content-type"):
            headers[name] = _unfold(value)
    return _decode_filename_words(_raw_part_filename(headers, degraded), degraded)


def _decode_filename_words(
    filename: str | None, degraded: Counter[str] | None = None
) -> str | None:
    """Decode the RFC 2047 encoded-words the standard library leaves in a
    filename parameter, the same way as Subject (#924).

    ``get_filename()`` decodes RFC 2231 only, so a filename sent as
    encoded-words (often several, a long name folded by the sending
    client) came back undecoded. ``_decode_header`` does the encoded-word
    grammar through ``email.header.decode_header``, in one linear pass.
    A charset label the codec rejects (unknown, ``idna``, a NUL in it)
    takes ``_decode_header``'s UTF-8 fallback, as in Subject (#942).
    The undecoded value is kept when decoding raises, yields text that is not valid
    Unicode (a ``unicode_escape`` word can decode to a lone surrogate),
    or leaves nothing:
    a filename never becomes empty, so it stays an attachment. The
    exception is reported by type only; the filename is mail content.
    """
    if not filename or "=?" not in filename:
        return filename
    try:
        decoded = _decode_header(filename, degraded)
        decoded.encode("utf-8")
    except (email.errors.HeaderParseError, ValueError, LookupError) as exc:
        if degraded is not None:
            degraded[FILENAME_DEGRADED] += 1
        warn_rate_limited(
            log,
            "attachment filename encoded-words could not be decoded (%s); kept 1 filename as sent",
            type(exc).__name__,
        )
        return filename
    return decoded or filename


def _raw_part_filename(
    part: email.message.Message, degraded: Counter[str] | None = None
) -> str | None:
    """``part.get_filename()``, falling back to the raw parameter text when
    its charset cannot decode it.

    ``get_filename()`` decodes an RFC 2231 ``filename*=`` (or ``name*=``)
    value with ``errors="replace"``. It already falls back to the raw text
    for a charset label it does not know (``LookupError``), but a codec
    that refuses ``errors="replace"`` (``idna``, ``undefined``) raises
    ``UnicodeError``, and a NUL in the label raises ``ValueError``
    (``UnicodeError`` is one too) (#362). On those, take the same raw text
    the standard library uses for an unknown label: the parameter that
    ``get_filename()`` reads, unquoted, without decoding. The exception
    is reported by type only; its text and the filename are mail content.
    """
    try:
        return part.get_filename()
    except ValueError as exc:
        if degraded is not None:
            degraded[FILENAME_DEGRADED] += 1
        # Rate limited (#1330): one crafted message can carry many such parts.
        warn_rate_limited(
            log,
            "attachment filename charset could not decode it (%s); using the raw parameter",
            type(exc).__name__,
        )
    # The same lookup ``get_filename()`` does: Content-Disposition
    # ``filename``, else Content-Type ``name``.
    missing = object()
    value = part.get_param("filename", missing, "content-disposition")
    if value is missing:
        value = part.get_param("name", missing, "content-type")
    # Only an RFC 2231 ``(charset, language, text)`` value is decoded, so
    # only that shape can have raised.
    if not isinstance(value, tuple):
        return None
    return email.utils.unquote(value[2]).strip()


def _is_attachment(part: email.message.Message, filename: str | None) -> bool:
    """True when ``part`` (whose ``_part_filename`` is ``filename``) is
    presented as a file rather than as text."""
    # Content-Disposition values are case-insensitive per RFC 2183.
    # The old ``"attachment" in cd`` check missed ``Attachment``,
    # ``ATTACHMENT``, and similar variants some clients emit,
    # causing real attachments to be decoded as the body or vice
    # versa.
    # ``str()``: an unencoded 8-bit header value comes back as a
    # ``Header``, which has no ``lower()``.
    cd = str(part.get("Content-Disposition", "")).lower()
    # Any part carrying a filename is treated as an attachment.
    # Message bodies are normally ``text/plain`` / ``text/html``
    # with no filename; anything that was given a filename is,
    # by convention, intended to be presented as a file. Some
    # clients also omit ``Content-Disposition`` entirely on
    # attachment parts — the explicit disposition check covers the
    # filename-less ``Content-Disposition: attachment`` case, while
    # the filename check covers dispositions that are absent,
    # non-standard, or ``inline`` with a file.
    return bool(filename) or "attachment" in cd


# Nesting levels inside an attached email that are still serialized for
# its hash. Serializing copies each subtree once per level above it, so
# a large leaf under many wrappers costs depth x size; a legitimate
# forward chain (a few forwards, each a few multipart levels) stays well
# below this.
MAX_ATTACHED_MESSAGE_DEPTH = 20
# Serialization work, across every container attachment of one message,
# that is still done: one unit per part, per header field, and per
# ``_HEADER_BYTES_PER_UNIT`` bytes of header text, which are the three
# things the generator spends time on (parsing is several times cheaper
# for each; body bytes are a copy, bounded by the parse cap). A delivery
# report is one field per line, or one headerless part per blank block,
# and a header can be megabytes, so a crafted report — or fifty side by
# side — would otherwise cost the single worker seconds for payloads
# nothing extracts. A real email has under a hundred fields and some
# kilobytes of headers; a report for two thousand recipients is about
# ten thousand units.
MAX_ATTACHED_MESSAGE_FIELDS = 10_000
_HEADER_BYTES_PER_UNIT = 16
# Transfer-encoded attachment bytes decoded per message. Each nested
# transfer-encoded attached email is decoded whole, so a chain of them
# would otherwise decode its large descendant once per level; this
# allows the outermost to be decoded in full (the parse cap is 50 MB by
# default) and stops a chain soon after.
MAX_DECODED_ATTACHMENT_BYTES = 64_000_000


@dataclass
class _SerializationBudget:
    """What one message's container attachments may still visit before
    serialization, and how many transfer-encoded bytes they may still
    decode; shared across them so both caps are per message."""

    remaining: int = MAX_ATTACHED_MESSAGE_FIELDS
    decodable: int = MAX_DECODED_ATTACHMENT_BYTES


def _nesting_exceeds(root: email.message.Message, limit: int, budget: _SerializationBudget) -> bool:
    """Whether ``root``'s part tree is more than ``limit`` levels deep or
    exhausts ``budget`` (each part counts one, plus one per header field
    and per ``_HEADER_BYTES_PER_UNIT`` bytes of header text). Iterative,
    stops at the first part past either limit, and leaves an exhausted
    budget exhausted for every later container."""
    # Children are taken lazily, last first as before, so a part with a
    # crafted number of children is not queued whole (review round 1 on
    # #1020); each one taken is visited and charged to the budget.
    stack: list[tuple[Iterator[email.message.Message], int]] = [(iter((root,)), 1)]
    while stack:
        siblings, depth = stack[-1]
        part = next(siblings, None)
        if part is None:
            stack.pop()
            continue
        headers = part.items()
        header_bytes = sum(len(name) + len(str(value)) for name, value in headers)
        budget.remaining -= 1 + len(headers) + header_bytes // _HEADER_BYTES_PER_UNIT
        if depth > limit or budget.remaining < 0:
            return True
        children = part.get_payload() if part.is_multipart() else None
        if isinstance(children, list):
            stack.append(
                ((c for c in reversed(children) if isinstance(c, email.message.Message)), depth + 1)
            )
    return False


# The Content-Transfer-Encoding values that leave a part's bytes as they
# are; base64 and quoted-printable are decoded, and any other value on an
# attached email is not.
_IDENTITY_ENCODINGS = frozenset({"", "7bit", "8bit", "binary"})


def _attachment_payload(
    part: email.message.Message,
    *,
    serialize_containers: bool,
    budget: _SerializationBudget,
    caps: Counter[str],
    payload_read: bool,
    decode_depth: int = 0,
    transport_lost: list[bool] | None = None,
) -> tuple[bytes, email.message.Message | None]:
    """The bytes an attachment carries, and, for a transfer-encoded
    attached email, its decoded tree to traverse (else ``None``).

    A container attachment — an attached email (``message/rfc822``), a
    delivery report, a ``multipart/*`` bundle — is parsed into subparts,
    so it has no decoded payload: serialize its body instead, or every
    one would hash to ``sha256(b"")`` and share one attachment ID. That
    is done only for an outermost container (``serialize_containers``)
    nested at most ``MAX_ATTACHED_MESSAGE_DEPTH`` deep, since
    serializing copies each subtree once per level above it; others
    keep the empty payload. A base64 or quoted-printable attached email
    (not allowed by RFC 2046, but sent) holds its transport form, so it
    is decoded and parsed first, into a container with this part's own
    Content-Type, which is then handled exactly as an identity-encoded
    part is: the same email hashes the same either way, and a decoded
    delivery report keeps its blocks. The parsed form holds none of the
    email's attachments, so the decoded container is returned for the
    caller to walk instead — for a nested container too, whose payload
    is not kept, until ``decode_depth`` reaches the depth cap or the
    message's decodable bytes are spent.

    The identity is that of the serialized form, not of the bytes in
    the file: ``as_bytes`` normalizes line endings and header folding.
    A container the generator or the transport decoder cannot handle
    (a header it refuses, 8-bit bytes in a transport form) keeps the
    empty payload: such errors quote the input, so they are never
    allowed to escape into a job's recorded error.

    A cap or failure that loses content is counted in ``caps`` under
    its ``PARSE_CAPS`` name: always when a decoded tree is left unwalked,
    and for an emptied payload only when ``payload_read`` (an extractor
    would read this attachment's payload).

    ``transport_lost`` (when given) gets ``True`` appended when a base64
    transport decoded but lost bytes (``_base64_transport_lost``), or the
    transport is quoted-printable, whose loss cannot be detected (#1288);
    the returned bytes are the lenient decode's either way (#1242). An
    attached email in any other non-identity transfer encoding (uuencode
    and its aliases included) keeps the empty payload, counted as
    ``transport_decode`` when an extractor would read it.
    """
    if not part.is_multipart():
        return _decoded_payload(part), None
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    nested = part.get_payload()
    if (
        part.get_content_maintype() == "message"
        and encoding in ("base64", "quoted-printable")
        and isinstance(nested, list)
        and nested
        and isinstance(nested[0], email.message.Message)
    ):
        # The parser read the transport form as MIME whatever the label
        # (one child for an attached email, one per block for a delivery
        # report), so check that tree's depth before rebuilding it.
        if decode_depth >= MAX_ATTACHED_MESSAGE_DEPTH:
            caps["attached_depth"] += 1
            return b"", None
        if _nesting_exceeds(part, MAX_ATTACHED_MESSAGE_DEPTH + 1, budget):
            caps[_nesting_cap(budget)] += 1
            return b"", None
        try:
            transport = _transport_text(part)
        except email.errors.MessageError, UnicodeError:
            caps["transport_decode"] += 1
            return b"", None
        budget.decodable -= len(transport)
        if budget.decodable < 0:
            caps["decoded_bytes"] += 1
            return b"", None
        content_type = str(part.get("Content-Type", "message/rfc822"))
        decoded = _decode_transport_form(transport, encoding, content_type)
        if decoded is None:
            caps["transport_decode"] += 1
            return b"", None
        # A quoted-printable loss records nothing to detect, so its bytes
        # (the lenient decode's) are kept but never certified until
        # #1288 detects the loss (review round 4 on #1311).
        if transport_lost is not None and (
            encoding == "quoted-printable" or _base64_transport_lost(transport)
        ):
            transport_lost.append(True)
        # From here the decoded container is the part: the same depth
        # check, serialization and traversal as an identity-encoded one.
        part = decoded
        if _nesting_exceeds(part, MAX_ATTACHED_MESSAGE_DEPTH + 1, budget):
            caps[_nesting_cap(budget)] += 1
            return b"", None
        if not serialize_containers:
            return b"", part
        try:
            return _serialized_body(part), part
        except email.errors.MessageError, UnicodeError:
            # The decoded tree is still walked: only the payload is lost.
            if payload_read:
                caps["container_serialize"] += 1
            return b"", part
    if part.get_content_maintype() == "message" and encoding not in _IDENTITY_ENCODINGS:
        # An attached email in a transfer encoding not decoded here
        # (uuencode and its aliases, or any other): its parsed form is the
        # transport text, so no payload is kept and none of it is ever
        # extracted (review round 4 on #1311). The caller still walks it.
        if serialize_containers and payload_read:
            caps["transport_decode"] += 1
        return b"", None
    if not serialize_containers:
        return b"", None
    # The part's own tree is one level deeper than the email it carries.
    # The caller still walks it, so only the payload is lost.
    if _nesting_exceeds(part, MAX_ATTACHED_MESSAGE_DEPTH + 1, budget):
        if payload_read:
            caps[_nesting_cap(budget)] += 1
        return b"", None
    try:
        return _serialized_body(part), None
    except email.errors.MessageError, UnicodeError:
        if payload_read:
            caps["container_serialize"] += 1
        return b"", None


def _nesting_cap(budget: _SerializationBudget) -> str:
    """Which cap a ``_nesting_exceeds`` that returned True hit: the
    message's field budget once it is spent (it stays spent), else the
    depth cap."""
    return "attached_fields" if budget.remaining < 0 else "attached_depth"


def _transport_text(container: email.message.Message) -> bytes:
    """The transfer-encoded text of a container attachment, recovered
    from the messages the parser made of it.

    The parser reads the transport text as MIME whatever the label: an
    attached email becomes one pseudo-message whose first lines are
    header fields and the rest the body, a delivery report one
    pseudo-message per block. Rendering those with the generator refolds
    the "headers" and adds multipart framing, either of which corrupts
    the text before it is decoded; the raw header tuples and body
    strings are put back together instead, blocks separated by the
    blank line the report had between them. Quoted-printable encodes
    every ``=``, so a boundary never survives and a pseudo-message's
    body is always a string; a list payload (never seen for real
    transport text) falls back to the generator.
    """
    children = container.get_payload()
    if not isinstance(children, list) or not children:
        return _serialized_body(container)
    texts: list[str] = []
    for child in children:
        if not isinstance(child, email.message.Message) or isinstance(child.get_payload(), list):
            return _serialized_body(container)
        texts.append(_pseudo_message_text(child))
    return "\r\n".join(texts).encode("ascii", "surrogateescape")


def _pseudo_message_text(pseudo: email.message.Message) -> str:
    """One pseudo-message's transport text: see ``_transport_text``.

    The blank line after the header lines is put back only where the
    text had one: not when the parser recorded a missing header/body
    separator (a quoted-printable soft break at the end of a header
    line leaves the next line looking like neither a header nor a
    continuation, and the soft break must still join them), and not
    after a block with no body. When the first lines name a multipart
    type with a boundary parameter, the parser keeps one more line
    break at the end of the body than the file has (that break belongs
    to the MIME delimiter), so it is dropped.
    """
    body = pseudo.get_payload()
    body = body if isinstance(body, str) else ""
    if pseudo.get_content_maintype() == "multipart" and pseudo.get_boundary():
        for newline in ("\r\n", "\n"):
            if body.endswith(newline):
                body = body[: -len(newline)]
                break
    lines = [f"{name}: {value}" for name, value in pseudo.items()]
    separated = not any(
        isinstance(d, email.errors.MissingHeaderBodySeparatorDefect) for d in pseudo.defects
    )
    text = "\r\n".join(lines)
    if lines:
        text += "\r\n"
    if body and separated and lines:
        text += "\r\n"
    return text + body


def _serialized_body(part: email.message.Message) -> bytes:
    """``part`` serialized without its own headers: its whole body, every
    subpart included. The generator ends the header block with a blank
    line, and a folded header line never contains one."""
    data = part.as_bytes()
    if data.startswith(b"\n"):
        return data[1:]
    return data.partition(b"\n\n")[2]


def _decode_transport_form(
    data: bytes, encoding: str, content_type: str
) -> email.message.Message | None:
    """Decode a container's transfer-encoded text and parse it as a
    container of ``content_type`` (the part's own), so an attached email
    is one nested message and a delivery report its blocks, exactly as
    the parser reads an identity-encoded part. ``None`` when the text
    does not decode or nests too deeply for the parser."""
    try:
        decoded = base64.b64decode(data) if encoding == "base64" else quopri.decodestring(data)
        header = b"Content-Type: " + content_type.encode("ascii", "surrogateescape") + b"\r\n\r\n"
        return email.message_from_bytes(header + decoded)
    except binascii.Error, RecursionError, UnicodeError:
        return None


# Inline text parts decoded for one message's body. Each costs a fixed
# setup (a fresh html2text converter is about 9 µs) on top of its bytes,
# which the parse cap already bounds, so a crafted message of a million
# tiny parts would otherwise spend seconds converting them. Real mail has
# a handful; parts past the cap are left out of the body.
MAX_BODY_TEXT_PARTS = 200

# MIME parts one message's body-and-attachment walk visits, the root and
# the parts inside attachments included (#996). Every part costs a stack
# entry and a fixed classification (and a body node when it is outside
# attachments) on top of the stdlib parse, so a crafted message of
# 700,000 empty parts (4.9 MB) spent about 2.3 s here (plain timing); at
# this cap the walk takes about 30 ms. Real mail has tens of parts; the
# walk keeps the first parts in document order and stops at the cap.
MAX_WALKED_PARTS = 10_000


@dataclass
class _BodyNode:
    """One part outside every attachment, as a candidate for the body.

    ``text`` is a leaf's stripped text; ``has_plain`` / ``has_text`` say
    whether the part (with its subtree, once assembled) contributes
    non-blank plain text / any non-blank text. An alternative or related
    container records its children in document order and which one it
    contributes."""

    parent: int
    alternative: bool
    related: bool = False
    text: str = ""
    has_plain: bool = False
    has_text: bool = False
    children: list[int] = field(default_factory=list)
    chosen: int = -1


def _selects(node: _BodyNode) -> bool:
    """Whether ``node`` contributes one chosen child rather than all."""
    return node.alternative or node.related


def _assemble_body(nodes: list[_BodyNode]) -> str:
    """The body: every non-blank inline text part in document order,
    separated by a blank line, where a ``multipart/alternative``
    contributes one child, the first carrying plain text, else the first
    carrying any text (the parts of an alternative are renderings of one
    body), and a ``multipart/related`` contributes only its root, taken
    to be its first child (RFC 2387's default; a ``start`` parameter
    naming another root is not read), and nothing when that child is an
    attachment (#450): its other parts are resources the root refers to.
    The parts of any other container are sequential content (#295).

    ``nodes`` is in walk (pre-)order, so a child always follows its
    parent: one backward pass settles each alternative's and related's
    choice, one forward pass keeps the parts every such container above
    them chose."""
    kept = _kept_nodes(nodes)
    return "\n\n".join(node.text for i, node in enumerate(nodes) if kept[i] and node.text)


def _kept_nodes(nodes: list[_BodyNode]) -> list[bool]:
    """Which of ``nodes`` the body keeps, by ``_assemble_body``'s rule
    (it updates each container's choice and flags in place)."""
    for i in range(len(nodes) - 1, -1, -1):
        node = nodes[i]
        if node.alternative:
            with_plain = (c for c in node.children if nodes[c].has_plain)
            with_text = (c for c in node.children if nodes[c].has_text)
            node.chosen = next(with_plain, next(with_text, -1))
        elif node.related:
            node.chosen = node.children[0] if node.children else -1
        if node.chosen >= 0:
            node.has_plain = nodes[node.chosen].has_plain
            node.has_text = nodes[node.chosen].has_text
        if node.parent >= 0 and not _selects(nodes[node.parent]):
            nodes[node.parent].has_plain |= node.has_plain
            nodes[node.parent].has_text |= node.has_text
    kept = [False] * len(nodes)
    for i, node in enumerate(nodes):
        parent = node.parent
        kept[i] = parent < 0 or (
            kept[parent] and (not _selects(nodes[parent]) or nodes[parent].chosen == i)
        )
    return kept


def _capped_parts_lost(nodes: list[_BodyNode], capped: list[tuple[int, bool]]) -> int:
    """How many of the ``capped`` text parts (node index, plain or not)
    could have contributed to the body: those the body would keep had
    each carried text (review round 4 on #884). An alternative after the
    one the body selects is not a loss. Works on a copy, so the body
    already assembled is not affected; linear in the nodes."""
    hypothetical = [replace(node, children=list(node.children)) for node in nodes]
    for index, plain in capped:
        hypothetical[index].has_text = True
        hypothetical[index].has_plain = plain
    kept = _kept_nodes(hypothetical)
    return sum(1 for index, _ in capped if kept[index])


@dataclass
class BodyWalk:
    """Body-only walks of several messages sharing one budget: the
    attached-email extractor's (#922), which renders an attached email
    and the attached emails nested in it.

    In such a walk attachments are neither materialized nor walked into
    (the enclosing message's parse indexes them); each ``message/rfc822``
    part met, an attachment or inline, is added to ``nested``, in
    document order, for the caller to walk as a message of its own.
    ``parts_left`` and ``text_parts_left`` are what ``MAX_WALKED_PARTS``
    and ``MAX_BODY_TEXT_PARTS`` allow across all the walks together.
    ``degraded`` counts the decoders' fallbacks (``HEADER_DEGRADED``,
    ``FILENAME_DEGRADED``, ``CHARSET_DEGRADED``), whose lines a caller in
    the extractor child cannot log; ``decode_lost_parts`` counts the text
    parts whose transfer decoding lost bytes (``_decode_lost_bytes``) or
    may have (any quoted-printable part, #1288), and
    ``structure_lost_parts`` the parts declared ``multipart/*`` that the
    standard library could not decompose (no or a missing boundary), whose
    text is lost."""

    parts_left: int = MAX_WALKED_PARTS
    text_parts_left: int = MAX_BODY_TEXT_PARTS
    nested: list[email.message.Message] = field(default_factory=list)
    degraded: Counter[str] = field(default_factory=Counter)
    decode_lost_parts: int = 0
    structure_lost_parts: int = 0


def _extract_body_and_attachments(
    msg: email.message.Message,
    caps: Counter[str] | None = None,
    walk: BodyWalk | None = None,
) -> tuple[str, list[Attachment]]:
    """The message's body text and attachments. ``caps`` (when given)
    counts the content a work cap dropped, by ``PARSE_CAPS`` name. With
    ``walk``, a body-only walk under its shared budget (``BodyWalk``):
    no attachments are returned."""
    if caps is None:
        caps = Counter()
    max_parts = MAX_WALKED_PARTS if walk is None else walk.parts_left
    max_text_parts = MAX_BODY_TEXT_PARTS if walk is None else walk.text_parts_left
    attachments: list[Attachment] = []
    nodes: list[_BodyNode] = []
    text_parts = 0
    # Text parts past MAX_BODY_TEXT_PARTS: (node index, plain or not).
    capped: list[tuple[int, bool]] = []

    # Depth-first in document order, like ``msg.walk()``, but nothing
    # inside an attachment is a candidate for the body: an attached
    # email's text is not the parent's. Attachments inside it are still
    # recorded, as the old walk did (a PDF in a forwarded email).
    # Iterative, so nesting depth cannot recurse. The root is classified
    # too: a message can be one attachment part, or a bundle presented as
    # one, whose text is then not the message's body. Each part outside
    # attachments becomes a ``_BodyNode`` under its parent's index (-1 for
    # none), and ``_assemble_body`` turns those into the body. ``no_body``
    # marks parts inside an inline email in a transfer encoding: the
    # parser exposes such an email as its encoded transport text, not
    # its content, so none of it is body text.
    budget = _SerializationBudget()
    # One frame per open container: its children, taken one at a time as
    # they are visited (review round 1 on #1020), so the walk keeps the
    # first MAX_WALKED_PARTS parts in document order and never queues a
    # container's children all at once.
    frames: list[tuple[Iterator[email.message.Message], bool, int, int, bool]] = [
        (iter((msg,)), False, 0, -1, False)
    ]
    walked = 0
    while frames:
        siblings, in_attachment, decode_depth, parent, no_body = frames[-1]
        part = next(siblings, None)
        if part is None:
            frames.pop()
            continue
        if walked >= max_parts:
            caps["mime_parts"] += 1
            break
        walked += 1
        ct = part.get_content_type()
        if (
            walk is not None
            and part.get_content_maintype() == "multipart"
            and not part.is_multipart()
        ):
            # A declared container the parse left as one undecomposed
            # payload: nothing in it is read (review round 6 on #1311;
            # the default walk's same gap is #1348).
            walk.structure_lost_parts += 1
        filename = _part_filename(part, None if walk is None else walk.degraded)
        is_attachment = _is_attachment(part, filename)
        decoded: email.message.Message | None = None
        if walk is not None and ct == "message/rfc822" and part.is_multipart():
            # An attached email, inline or not, is a message of its own
            # here: its headers and body are rendered as a nested
            # section, never folded into this body (review round 2).
            walk.nested.append(part)
            is_attachment = True
        elif is_attachment and walk is not None:
            pass
        elif is_attachment:
            transport_lost: list[bool] = []
            payload, decoded = _attachment_payload(
                part,
                serialize_containers=not in_attachment,
                budget=budget,
                caps=caps,
                payload_read=resolved_extractor_module(ct, filename or "unnamed") is not None,
                decode_depth=decode_depth,
                transport_lost=transport_lost,
            )
            attachments.append(
                Attachment(
                    filename=filename or "unnamed",
                    content_type=ct,
                    size=len(payload),
                    payload=payload,
                    content_hash=hashlib.sha256(payload).hexdigest(),
                    # A container's payload is its serialized body; the
                    # empty bytes mean it was not serialized, and a lossy
                    # base64 transport means it is not the sender's. A
                    # leaf's is its decoded bytes, read after the decode
                    # (#1242).
                    payload_complete=(
                        bool(payload) and not transport_lost
                        if part.is_multipart()
                        else not _decode_lost_bytes(part)
                    ),
                )
            )
        inside = in_attachment or is_attachment
        node: _BodyNode | None = None
        if not inside and not no_body:
            node = _BodyNode(
                parent,
                alternative=ct == "multipart/alternative",
                related=ct == "multipart/related",
            )
            nodes.append(node)
            if parent >= 0 and _selects(nodes[parent]):
                nodes[parent].children.append(len(nodes) - 1)
        elif is_attachment and parent >= 0 and nodes[parent].related:
            # An attachment keeps its position among a related's children
            # as an empty node, so one that is the root makes the related
            # contribute nothing rather than promoting the next part (#450).
            nodes.append(_BodyNode(parent, alternative=False))
            nodes[parent].children.append(len(nodes) - 1)
        if part.is_multipart():
            if walk is not None and is_attachment:
                continue
            # A decoded container stands in for its transport form; its
            # children are one decode deeper.
            if decoded is not None:
                children, depth = decoded.get_payload(), decode_depth + 1
            else:
                children, depth = part.get_payload(), decode_depth
            if isinstance(children, list):
                index = -1 if node is None else len(nodes) - 1
                encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
                skip = no_body or (
                    part.get_content_maintype() == "message"
                    and encoding not in ("", "7bit", "8bit", "binary")
                )
                frames.append(
                    (
                        (c for c in children if isinstance(c, email.message.Message)),
                        inside,
                        depth,
                        index,
                        skip,
                    )
                )
            continue
        if node is None:
            continue
        # A single-part message's text is its body whatever the text
        # subtype (text/calendar, text/enriched); inside a multipart only
        # text/plain and text/html are. Binary parts are never decoded as
        # text. Plain text is preferred over the html2text rendering of an
        # HTML alternative whatever their order (the LLM should get the
        # sender's clean plain text); a whitespace-only plain part has no
        # content to prefer (#298).
        is_html = ct == "text/html"
        if not is_html and not (
            ct == "text/plain" or (part is msg and part.get_content_maintype() == "text")
        ):
            continue
        if text_parts >= max_text_parts:
            # Counted below, once the body's selection is known.
            capped.append((len(nodes) - 1, not is_html))
            continue
        text_parts += 1
        payload = _decoded_payload(part)
        # A quoted-printable loss records no defect to read, and a part
        # in any other encoding not decoded here (uuencode, its aliases,
        # anything unknown) can come back as its transport text, so in a
        # body-only walk each counts as lossy until #1288 detects it
        # (review rounds 3 and 5 on #1311).
        if walk is not None and (
            _decode_lost_bytes(part)
            or str(part.get("Content-Transfer-Encoding", "")).strip().lower()
            not in _IDENTITY_ENCODINGS | {"base64"}
        ):
            walk.decode_lost_parts += 1
        text = _safe_decode(
            payload,
            part.get_content_charset() or "utf-8",
            None if walk is None else walk.degraded,
        )
        node.text = (_html_to_text(text) if is_html else text).strip()
        node.has_text = bool(node.text)
        node.has_plain = node.has_text and not is_html

    if walk is not None:
        walk.parts_left -= walked
        walk.text_parts_left -= text_parts
    body = _assemble_body(nodes)
    if capped:
        lost = _capped_parts_lost(nodes, capped)
        if lost:
            caps["body_parts"] += lost
    return body, attachments


def _safe_decode(payload: bytes, charset: str, degraded: Counter[str] | None = None) -> str:
    """Decode payload bytes, falling back to utf-8 on unknown charsets.

    ``UnicodeError`` covers codecs that reject ``errors="replace"``
    (``idna`` raises ``UnicodeError("Unsupported error handling")``).
    ``degraded`` (when given) counts a fallback or a replacement under
    ``CHARSET_DEGRADED``. The text returned is always the default path's;
    a strict decode runs only to detect a replacement, when the lenient
    one succeeded (review round 5 on #1311: ``idna`` decodes strictly but
    rejects ``errors="replace"``, so its text is the UTF-8 fallback's).
    """
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError, UnicodeError:
        if degraded is not None:
            degraded[CHARSET_DEGRADED] += 1
        return payload.decode("utf-8", errors="replace")
    if degraded is not None:
        try:
            payload.decode(charset)
        except UnicodeError:
            degraded[CHARSET_DEGRADED] += 1
    return text


def _clean_id(value: str) -> str:
    return value.strip().strip("<>").strip()


def _decode_header(value: str | email.header.Header, degraded: Counter[str] | None = None) -> str:
    """Decode a header value's RFC 2047 encoded-words.

    A ``Header`` (raw 8-bit header) already holds decoded chunks. A
    string is scanned for encoded-words in one linear pass and each one
    is decoded on its own: ``decode_header`` on the whole string rescans
    the rest of the header at every malformed ``=?`` prefix, which is
    quadratic in a hostile Subject.

    ``degraded`` (when given) counts each fallback the decoders below log
    under ``HEADER_DEGRADED``, for a caller whose log lines do not reach
    the log (the attached-email extractor's child, #922).
    """
    if isinstance(value, email.header.Header):
        return _decode_header_parts(email.header.decode_header(value), degraded).strip()
    if "=?" not in value:
        return value.strip()
    return _decode_encoded_word_runs(
        value, lambda m: _decode_header_parts(email.header.decode_header(m.group(0)), degraded)
    ).strip()


# The ``degraded`` keys the decoders count their fallbacks under: a
# header (an encoded-word in a filename included), a part's filename,
# and a body text part's charset (an unknown label, or bytes the label's
# codec replaced).
HEADER_DEGRADED = "header_degraded"
FILENAME_DEGRADED = "filename_degraded"
CHARSET_DEGRADED = "charset_degraded"


def _decode_text_header(
    value: str | email.header.Header, degraded: Counter[str] | None = None
) -> str:
    """Decode a header stored as text (Subject, the From fallback).

    compat32 returns a raw 8-bit header as one chunk, so an encoded-word
    beside the raw bytes stays as sent (#1186); that partial decode logs
    one rate-limited WARNING with fixed text (#1147 review round 1).
    Address display names are not decoded here: ``_parse_addrs`` decodes
    their encoded-words after the address is fixed. The search is one
    linear pass of ``_ENCODED_WORD_RE``, whose groups stop at ``?``.
    """
    text = _decode_header(value, degraded)
    if isinstance(value, email.header.Header) and _ENCODED_WORD_RE.search(text):
        if degraded is not None:
            degraded[HEADER_DEGRADED] += 1
        warn_rate_limited(
            log,
            "raw 8-bit header holds encoded-words that were not decoded; kept 1 header as sent",
            attachment=False,
        )
    return text


def _decode_header_parts(
    parts: list[tuple[bytes | str, str | None]], degraded: Counter[str] | None = None
) -> str:
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            # ``charset`` is whatever the sender claimed in the MIME header;
            # obscure or invalid labels ("x-mac-romanian", typos, historical
            # aliases) raise LookupError, a codec that rejects
            # ``errors="replace"`` (``idna``) raises UnicodeError, and a
            # NUL in the label makes the codec lookup raise a plain
            # ValueError (#942); ``ValueError`` covers UnicodeError too.
            # Handle them locally with a utf-8 fallback and ``errors="replace"``
            # so a single bad header does not affect the rest of the
            # message. Anything we DON'T catch
            # here propagates out of ``parse_email``: the function does
            # not have a blanket ``except Exception`` precisely so
            # unanticipated parser failures route through the durable
            # queue's retry + dead-letter cascade instead of being
            # dead-lettered as unindexable without any retry. The fallback
            # changes the indexed text, so it logs one rate-limited WARNING
            # per word naming the exception type only (the label and text
            # are mail content).
            encoding = charset or "utf-8"
            if encoding.lower() == _RAW_8BIT_CHARSET:
                decoded.append(_decode_raw_8bit(part, degraded))
                continue
            try:
                decoded.append(part.decode(encoding, errors="replace"))
            except (LookupError, ValueError) as exc:
                if degraded is not None:
                    degraded[HEADER_DEGRADED] += 1
                warn_rate_limited(
                    log,
                    "header encoded-word charset could not be decoded (%s); decoded 1 word as UTF-8",
                    type(exc).__name__,
                    attachment=False,
                )
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


# The label compat32 gives header bytes sent without an encoded-word
# (raw 8-bit), and that the standard library writes when it re-encodes
# them. No codec has this name, so it is handled here (#1147).
_RAW_8BIT_CHARSET = "unknown-8bit"


def _decode_raw_8bit(part: bytes, degraded: Counter[str] | None = None) -> str:
    """Decode a raw 8-bit header chunk: exactly and silently when it is
    valid UTF-8, otherwise as UTF-8 with replacement characters and one
    rate-limited WARNING, since characters were lost (#1147)."""
    try:
        return part.decode("utf-8")
    except UnicodeDecodeError:
        if degraded is not None:
            degraded[HEADER_DEGRADED] += 1
        warn_rate_limited(
            log,
            "raw 8-bit header is not UTF-8; decoded 1 header chunk with replacement characters",
            attachment=False,
        )
        return part.decode("utf-8", errors="replace")


# RFC 5322 "specials": a display name containing any of these must be
# quoted to stay one address (the same trigger ``formataddr`` uses).
_ADDR_SPECIALS_RE = re.compile(r'[][\\()<>@,:;".]')


def _format_address(name: str, addr: str) -> str:
    """Serialize one parsed (name, address) pair so it parses back intact.

    Not ``email.utils.formataddr``: it raises ``UnicodeEncodeError`` on a
    non-ASCII address (``josé@example.com``, an IDN domain) — aborting the
    whole message's ingestion — and rewrites non-ASCII display names as
    RFC 2047 encoded-words. This quotes the name only when it contains a
    special (``"Doe, Jane"``) and never encodes anything.
    """
    if not name:
        return addr
    if _ADDR_SPECIALS_RE.search(name):
        name = f'"{email.utils.quote(name)}"'
    formatted = f"{name} <{addr}>"
    # Identity invariant: downstream code re-parses this string, so it
    # must yield the same address. If any name content would change the
    # parsed address (or make it unparseable), drop the name — a lost
    # display name is cosmetic, a changed recipient is not.
    if email.utils.parseaddr(formatted)[1] != addr:
        return addr
    return formatted


def canonical_addr(value: str) -> str:
    """Normalize an address string to a lowercase bare email for matching.

    RFC 2822 ``From`` / ``To`` headers can carry the same person as
    ``Bob Smith <bob@example.com>``, ``bob@example.com``, or
    ``"Bob S." <bob@example.com>`` — stable string comparison treats
    those three as different participants and produces false misses for
    subject-fallback matching and duplicate entries in participant
    lists. ``parseaddr`` extracts the bare address; lowercasing makes
    the match case-insensitive.

    Returns an empty string when no usable email address can be
    recovered. ``parseaddr`` is permissive and will return a first-token
    value like ``"just"`` for a header like ``"just a name"`` — rejecting
    results without an ``@`` keeps malformed entries from becoming their
    own spurious "participant" and from matching other malformed entries
    to each other.
    """
    if not value:
        return ""
    try:
        _, addr = email.utils.parseaddr(value)
    except Exception:
        # parseaddr recurses on nested comments; hostile input (which
        # reaches here via thread participants and the from_addr
        # fallback for unparseable From headers) must degrade to
        # "no address", never abort threading.
        return ""
    addr = addr.strip().lower()
    if "@" not in addr:
        return ""
    return addr


# One RFC 2047 encoded-word: =?charset?Q|B?text?=. Bounded character
# classes (no whitespace, no "?") keep the scan linear.
_ENCODED_WORD_RE = re.compile(r"=\?([^?\s]+)\?([QqBb])\?([^?\s]*)\?=")
# RFC 2047 §6.2: whitespace between two adjacent encoded-words is dropped.
# Matching whole runs of valid words keeps whitespace next to anything
# that only looks like one (a malformed ``=?...?=`` stays raw text).
_ENCODED_WORD_RUN_RE = re.compile(
    _ENCODED_WORD_RE.pattern + r"(?:\s+" + _ENCODED_WORD_RE.pattern + r")*"
)
# RFC 5322 line limit, applied per encoded-word: a valid long name folds
# into many short encoded-words, so bounding the whole name would leave
# legitimate long names undecoded.
_MAX_ENCODED_WORD_CHARS = 998


def _decode_encoded_word(match: re.Match[str]) -> str:
    """Decode one encoded-word, or return it unchanged if that fails.

    Deliberately broad: a display name is cosmetic, so no failure here
    may cost the address or the message. That covers codec errors, a
    charset label the codec lookup rejects (``ValueError`` on an embedded
    NUL), and text that decodes but is not valid Unicode (a UTF-7 word
    yielding a lone surrogate, which would crash later tokenization).
    """
    token = match.group(0)
    if len(token) > _MAX_ENCODED_WORD_CHARS:
        return token
    try:
        pieces = []
        for part, charset in email.header.decode_header(token):
            if not isinstance(part, bytes):
                pieces.append(part)
                continue
            try:
                pieces.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                # Unknown label ("x-bogus"): same UTF-8 fallback as _decode_header.
                pieces.append(part.decode("utf-8", errors="replace"))
        text = "".join(pieces)
        text.encode("utf-8")
    except Exception:
        return token
    # A decoded CR / LF / other control character survives into the
    # serialized "name <addr>" string and breaks re-parsing — it can even
    # make a name like "Mallory@example.com\r" read as the address.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return token
    return text


def _decode_display_name(name: str) -> str:
    """Decode the RFC 2047 encoded-words in a display name.

    Only encoded-word tokens are decoded; any other text — including
    Unicode already decoded from a raw 8-bit header — is left exactly as
    it is. (Running the whole name through ``decode_header`` re-encoded
    that Unicode and corrupted it, and rescanned the name at every
    malformed ``=?`` prefix, which is quadratic.) A token that fails to
    decode keeps its raw text. The scan is linear, and each encoded-word
    is bounded individually by ``_decode_encoded_word``.
    """
    if "=?" not in name:
        return name
    return _decode_encoded_word_runs(name, _decode_encoded_word)


def _decode_encoded_word_runs(text: str, decode_word: Callable[[re.Match[str]], str]) -> str:
    """Decode each encoded-word in ``text``, joining adjacent words.

    One linear pass: each run of whitespace-separated valid encoded-words
    is decoded word by word and joined without the whitespace; all other
    text, including malformed look-alikes, is left as it is.
    """
    return _ENCODED_WORD_RUN_RE.sub(
        lambda run: "".join(decode_word(m) for m in _ENCODED_WORD_RE.finditer(run.group(0))),
        text,
    )


# Structural-parsing budget. The split below is linear, and each element
# is parsed on its own, so these only bound pathological headers; a
# header or element over budget costs its recipients, never the message.
_MAX_ADDRESS_HEADER_CHARS = 256_000
_MAX_ADDRESS_ELEMENT_CHARS = 128_000
# Longest address we will emit. Real addresses cap at 254 octets
# (RFC 5321); 998 is the RFC 5322 line limit, generous for anything
# deliverable, and it bounds the fixed-point re-parse below.
_MAX_ADDRESS_CHARS = 998

# One message's address-header budget (#1144), spent by
# ``_read_address_headers`` across From, To and Cc together, so
# repeating a header cannot multiply the work. Each dimension the cost
# depends on has its own limit, checked before the work it bounds:
# header fields scanned for the three names; occurrences read; raw
# characters in all (checked, with the per-value
# ``_MAX_ADDRESS_HEADER_CHARS``, before an occurrence is fetched,
# decoded or split, so decoding is linear in at most this much text);
# elements split and parsed (each element costs at most two
# ``parseaddr`` calls); and addresses kept, one participant row each.
# Real mail has one From, To and Cc among a few dozen fields, so these
# only touch crafted messages; what they drop is counted in
# ``PARSE_CAPS``.
MAX_ADDRESS_HEADER_FIELDS = 10_000
MAX_ADDRESS_OCCURRENCES = 64
# Three headers at the per-value cap: the most the parser read before
# repeated headers were.
_MAX_ADDRESS_TOTAL_CHARS = 3 * _MAX_ADDRESS_HEADER_CHARS
MAX_ADDRESS_ELEMENTS = 20_000
MAX_MESSAGE_ADDRESSES = 10_000

# One message's display-name budget (#1140), spent by
# ``participant_names``. The first display name of each (role, address)
# is always kept: at most one per ``message_participants`` row, so the
# address budget above already bounds it. Each
# further distinct name for the same (role, address) costs one name and
# its UTF-8 bytes from this budget, across From, To and Cc together, so
# every stored name row and byte past the participant rows themselves is
# bounded. Real mail rarely writes one address under two names; names
# past the budget are dropped and counted as ``participant_names`` in
# ``PARSE_CAPS``, and the address keeps its first name.
MAX_EXTRA_PARTICIPANT_NAMES = 1_000
MAX_EXTRA_PARTICIPANT_NAME_BYTES = 64_000


def participant_names(
    roles: Iterable[tuple[str, Iterable[str]]], caps: Counter[str]
) -> list[tuple[str, str, str]]:
    """Every distinct display name per (role, address), as
    ``(role, address, name)`` rows in header order (#1140).

    ``roles`` is ``(role, address strings)`` in From, To, Cc order, as
    the participant writer reads them; ``address`` is the
    ``canonical_addr`` it keys rows by and ``name`` the stripped
    ``parseaddr`` display name it stores, so a participant row's
    ``name``, when it has one, is the first name here for its (role,
    address). Exact duplicates are kept once and
    casing is kept. Names past the first of a (role, address) spend the
    ``MAX_EXTRA_PARTICIPANT_*`` budget; each one refused is counted in
    ``caps["participant_names"]``. Two ``parseaddr`` calls per address.
    """
    seen: dict[tuple[str, str], set[str]] = {}
    rows: list[tuple[str, str, str]] = []
    extra_names = extra_bytes = 0
    for role, values in roles:
        for value in values:
            address = canonical_addr(value or "")
            if not address:
                continue
            name = email.utils.parseaddr(value)[0].strip()
            if not name:
                continue
            names = seen.setdefault((role, address), set())
            if name in names:
                continue
            if names:
                size = len(name.encode("utf-8", errors="replace"))
                if (
                    extra_names >= MAX_EXTRA_PARTICIPANT_NAMES
                    or extra_bytes + size > MAX_EXTRA_PARTICIPANT_NAME_BYTES
                ):
                    caps["participant_names"] += 1
                    continue
                extra_names += 1
                extra_bytes += size
            names.add(name)
            rows.append((role, address, name))
    return rows


@dataclass
class _AddressBudget:
    """What one message's address headers have spent so far, one
    counter per ``MAX_ADDRESS_*`` dimension, plus the ``parseaddr``
    calls made (``parse_calls``) so a test can assert the work done."""

    fields: int = 0
    occurrences: int = 0
    chars: int = 0
    elements: int = 0
    addresses: int = 0
    parse_calls: int = 0


@dataclass
class _AddressHeaders:
    """The From, To and Cc of one message (``_read_address_headers``)."""

    from_addr: str
    from_addrs: list[str]
    to_addrs: list[str]
    cc_addrs: list[str]
    sender_ambiguous: bool
    # Whether every address of the role was kept (#1086).
    from_complete: bool
    to_complete: bool
    cc_complete: bool


def _address_losses(caps: Counter[str]) -> int:
    """The addresses lost so far, summed over ``ADDRESS_LOSS_CAPS``."""
    return sum(caps[name] for name in ADDRESS_LOSS_CAPS)


def _read_address_headers(msg: email.message.Message, caps: Counter[str]) -> _AddressHeaders:
    """Read From, To and Cc under one ``_AddressBudget`` (#1144).

    One pass over the raw header fields (up to
    ``MAX_ADDRESS_HEADER_FIELDS``) collects every occurrence of the three
    names, matched case-insensitively as ``Message.get`` does. Every To
    is merged in order into ``to_addrs``, and every Cc into ``cc_addrs``
    (RFC 5322 section 4.5.3 combines repeated destination fields). Only
    the first From is parsed, before any recipient so a crafted header
    order cannot spend its budget; later From headers are counted, and
    they, or a scan cut short, make the sender ambiguous. ``from_addr``
    is the first author, else the first From's decoded text when it
    holds no parseable address. A role is complete when no
    ``ADDRESS_LOSS_CAPS`` count grew while it was read, the scan was not
    cut short, and (for From) no later From went unparsed (#1086).
    """
    budget = _AddressBudget()
    found: dict[str, list[str]] = {"from": [], "to": [], "cc": []}
    complete = True
    for name, raw in msg.raw_items():
        if budget.fields >= MAX_ADDRESS_HEADER_FIELDS:
            caps["address_fields"] += 1
            complete = False
            break
        budget.fields += 1
        occurrences = found.get(name.lower())
        if occurrences is not None:
            occurrences.append(raw)

    from_addrs: list[str] = []
    from_text = ""
    lost = _address_losses(caps)
    if found["from"]:
        value = _address_value(msg, "From", found["from"][0], budget, caps)
        if value is not None:
            from_addrs = _parse_addrs(value, caps, budget)
            if not from_addrs:
                from_text = _decode_text_header(value)
    if len(found["from"]) > 1:
        caps["from_repeated"] += len(found["from"]) - 1
    from_complete = complete and len(found["from"]) <= 1 and _address_losses(caps) == lost

    recipients: dict[str, list[str]] = {}
    recipients_complete: dict[str, bool] = {}
    for role, header in (("to", "To"), ("cc", "Cc")):
        addrs: list[str] = []
        lost = _address_losses(caps)
        for index, raw in enumerate(found[role]):
            value = _address_value(msg, header, raw, budget, caps)
            if value is None:
                continue
            if index:
                caps["address_repeated"] += 1
            addrs.extend(_parse_addrs(value, caps, budget))
        recipients[role] = addrs
        recipients_complete[role] = complete and _address_losses(caps) == lost

    return _AddressHeaders(
        from_addr=from_addrs[0] if from_addrs else from_text,
        from_addrs=from_addrs,
        to_addrs=recipients["to"],
        cc_addrs=recipients["cc"],
        sender_ambiguous=len(found["from"]) > 1 or not complete,
        from_complete=from_complete,
        to_complete=recipients_complete["to"],
        cc_complete=recipients_complete["cc"],
    )


def _address_value(
    msg: email.message.Message,
    name: str,
    raw: str,
    budget: _AddressBudget,
    caps: Counter[str],
) -> str | email.header.Header | None:
    """One occurrence's value as ``Message.get`` would return it, or
    ``None`` when the budget refuses it. The occurrence and raw-length
    limits are checked on the raw text before the policy builds a
    ``Header`` for an 8-bit value, so nothing over budget is decoded."""
    if budget.occurrences >= MAX_ADDRESS_OCCURRENCES:
        caps["address_occurrences"] += 1
        return None
    budget.occurrences += 1
    size = len(raw)
    if size > _MAX_ADDRESS_HEADER_CHARS:
        caps["address_header"] += 1
        return None
    if budget.chars + size > _MAX_ADDRESS_TOTAL_CHARS:
        caps["address_chars"] += 1
        return None
    budget.chars += size
    value: str | email.header.Header = msg.policy.header_fetch_parse(name, raw)
    return value


def _protect_encoded_words(text: str) -> tuple[str, Callable[[str], str]]:
    """Replace each encoded-word with an opaque placeholder atom.

    Encoded-word contents are data, not syntax. Left in place, parentheses,
    colons, commas, or ``@`` inside a charset label or encoded text are
    parsed as address structure — recursing on nested "comments", driving
    the standard library's group parser quadratic, or fabricating an
    address (``bob@example.com (=?utf-8?q?A)_(B?=)`` read as
    ``bob@example.com_``). Returns the protected text and a function that
    restores the original tokens in any parsed fragment.
    """
    tokens: list[str] = []
    nonce = secrets.token_hex(6)

    def protect(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"ew{nonce}n{len(tokens) - 1}x"

    protected = _ENCODED_WORD_RE.sub(protect, text)
    if not tokens:
        return protected, lambda fragment: fragment
    placeholder_re = re.compile(rf"ew{nonce}n(\d+)x")

    def restore(fragment: str) -> str:
        return placeholder_re.sub(lambda m: tokens[int(m.group(1))], fragment)

    return protected, restore


def _split_address_list(text: str) -> list[str]:
    """Split an address-list header into its elements, in one linear pass.

    Separators are recognized only at top level — never inside quoted
    strings, comments, angle brackets, or ``[domain literals]``, and
    backslash escapes are honored. Group syntax is flattened: the group
    name before a top-level ``:`` is dropped and ``;`` ends an element.
    Empty elements (doubled, leading, or trailing commas) simply vanish.
    Parsing each element separately keeps the standard library's
    quadratic group parser out of the path entirely.
    """
    elements: list[str] = []
    buf: list[str] = []
    in_quote = in_literal = in_angle = False
    comment_depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quote or comment_depth or in_literal:
            if ch == "\\" and i + 1 < n:
                buf.append(text[i : i + 2])
                i += 2
                continue
            if in_quote and ch == '"':
                in_quote = False
            elif comment_depth and ch == "(":
                comment_depth += 1
            elif comment_depth and ch == ")":
                comment_depth -= 1
            elif in_literal and ch == "]":
                in_literal = False
            buf.append(ch)
        elif ch == '"':
            in_quote = True
            buf.append(ch)
        elif ch == "(":
            comment_depth = 1
            buf.append(ch)
        elif ch == "[":
            in_literal = True
            buf.append(ch)
        elif in_angle:
            if ch == ">":
                in_angle = False
            buf.append(ch)
        elif ch == "<":
            in_angle = True
            buf.append(ch)
        elif ch in ",;":
            elements.append("".join(buf))
            buf = []
        elif ch == ":":
            buf = []  # group display name
        else:
            buf.append(ch)
        i += 1
    elements.append("".join(buf))
    return [element.strip() for element in elements if element.strip()]


def _parse_addrs(
    value: str | email.header.Header,
    caps: Counter[str] | None = None,
    budget: _AddressBudget | None = None,
) -> list[str]:
    """Parse an address header into one parseable string per address.

    Raw 8-bit headers (UTF-8 written directly in the header) arrive as
    ``email.header.Header`` objects whose ``str()`` mangles the non-ASCII
    bytes, so they are decoded first. Encoded-words are then made opaque,
    the list is split at top level, and each element is parsed strictly
    on its own; display names are decoded only after the address is
    fixed, so name content can never become address syntax. Every step
    fails safe: an element that cannot be parsed costs only that
    recipient, never the message. ``caps`` (when given) counts the
    recipients a work cap dropped, by ``PARSE_CAPS`` name, and each
    element that yields no storable address as ``address_unparsed``
    (#1086). ``budget``
    (when given) is the message's ``_AddressBudget``: elements past its
    element or address limit are dropped unparsed. A message's raw
    length limits are checked before decoding, by
    ``_read_address_headers``; the length check here only guards a
    direct caller.
    """
    if caps is None:
        caps = Counter()
    if budget is None:
        budget = _AddressBudget()
    if not value:
        return []
    text = _decode_header(value) if isinstance(value, email.header.Header) else value
    if len(text) > _MAX_ADDRESS_HEADER_CHARS:
        log.debug("address header over %d chars; recipients not parsed", _MAX_ADDRESS_HEADER_CHARS)
        caps["address_header"] += 1
        return []
    # Unfold once, before anything parses the text: the standard library
    # keeps a fold inside a quoted string or comment, and a CRLF there
    # can even change the parsed address (#688).
    text = _unfold(text)
    protected, restore = _protect_encoded_words(text)
    addresses = []
    elements = _split_address_list(protected)
    for index, element in enumerate(elements):
        if budget.elements >= MAX_ADDRESS_ELEMENTS:
            caps["address_elements"] += len(elements) - index
            break
        if budget.addresses >= MAX_MESSAGE_ADDRESSES:
            caps["address_count"] += len(elements) - index
            break
        budget.elements += 1
        if len(element) > _MAX_ADDRESS_ELEMENT_CHARS:
            caps["address_element"] += 1
            continue
        try:
            budget.parse_calls += 1
            name, addr = email.utils.parseaddr(element)
            if not addr.strip():
                caps["address_unparsed"] += 1
                continue
            addr = restore(addr)
            # Every emitted string is re-parsed downstream — the identity
            # check in _format_address, canonical_addr in the threader,
            # the participant writer, the MCP contact reader. A restored
            # address must therefore survive re-parsing unchanged: one
            # that is not a parseaddr fixed point (an unsafe restored
            # encoded-word, say) is discarded HERE, inside the failure
            # boundary, not handed to an unguarded reparser later.
            if len(addr) > _MAX_ADDRESS_CHARS:
                caps["address_length"] += 1
                continue
            budget.parse_calls += 1
            if email.utils.parseaddr(addr)[1] != addr:
                caps["address_unparsed"] += 1
                continue
            name = _decode_display_name(restore(name)) if name else ""
            formatted = _format_address(name, addr)
        except Exception:
            # e.g. RecursionError from parseaddr on deeply nested comments,
            # whether in the element or re-created by restoring a token.
            caps["address_unparsed"] += 1
            continue
        if "@" not in addr:
            # Kept (the From fallback and thread lists show it), but the
            # participant writer stores only ``@``-bearing addresses
            # (``canonical_addr``), so the role's rows are incomplete.
            caps["address_unparsed"] += 1
        budget.addresses += 1
        addresses.append(formatted)
    return addresses


def _parse_received_date(msg: email.message.Message) -> datetime | None:
    """The delivery time from ``msg``'s topmost ``Received:`` header.

    The date is the text after the header's last ``;``, searched for in
    the last ``RECEIVED_DATE_MAX_CHARS`` characters only, parsed with
    ``parsedate_to_datetime`` and converted to UTC (a naive ``-0000``
    value is taken as UTC). Returns ``None`` when the header is absent,
    has no ``;`` in that tail, or its date is unparseable; ``Date:`` is
    never consulted. The header is attacker-influenced, so every error
    the email package and the codecs can raise on it degrades to
    ``None`` and its text is never logged.
    """
    try:
        # ``get`` returns the first, i.e. topmost, occurrence. A raw
        # 8-bit header comes back as an ``email.header.Header``; a date
        # is ASCII, so anything else is dropped, as for ``Date:``.
        value = msg.get("Received")
        if value is None:
            return None
        tail = str(value)[-RECEIVED_DATE_MAX_CHARS:]
        semicolon = tail.rfind(";")
        if semicolon < 0:
            return None
        date_text = tail[semicolon + 1 :].encode("ascii", "ignore").decode("ascii")
        dt = email.utils.parsedate_to_datetime(date_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        # Inside the guard: a date near year 9999 with a negative
        # offset parses but overflows on conversion to UTC.
        return dt.astimezone(UTC)
    except (
        email.errors.MessageError,
        email.errors.MessageDefect,
        UnicodeError,
        LookupError,
        ValueError,
        TypeError,
        OverflowError,
    ) as exc:
        log.debug("Received header date unreadable (%s)", type(exc).__name__)
        return None


def message_sort_time(path: Path) -> datetime | None:
    """The time that orders a first full index (#699): the message's
    effective time as ``parse_email`` derives it (the topmost
    ``Received:``, else ``Date:``), from its header block alone.

    Reads at most ``SORT_HEADER_MAX_BYTES`` and parses only those bytes
    with the standard library's header parser, so the work is bounded
    whatever the file's size. Returns ``None`` when the file cannot be
    read or neither header gives a date. Nothing is logged: the message
    is parsed in full, and any problem reported, when it is indexed.
    """
    head = b""
    try:
        with open(path, "rb") as f:
            while len(head) < SORT_HEADER_MAX_BYTES:
                chunk = f.read(min(_SORT_READ_CHUNK, SORT_HEADER_MAX_BYTES - len(head)))
                if not chunk:
                    break
                # The blank line can straddle two chunks.
                start = max(0, len(head) - 3)
                head += chunk
                if b"\n\n" in head[start:] or b"\n\r\n" in head[start:]:
                    break
    except OSError:
        return None
    try:
        msg = email.parser.BytesHeaderParser().parsebytes(head)
        occurred = _parse_received_date(msg)
        if occurred is not None:
            return occurred
        # As ``parse_email`` reads ``Date:``, without its warning.
        date_text = str(msg.get("Date", "")).encode("ascii", "ignore").decode("ascii")
        dt = email.utils.parsedate_to_datetime(date_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (
        email.errors.MessageError,
        email.errors.MessageDefect,
        UnicodeError,
        LookupError,
        ValueError,
        TypeError,
        OverflowError,
    ):
        return None


def _parse_date(value: str) -> datetime | None:
    """Parse an RFC 2822 date header and normalize to a UTC-aware datetime.

    ``parsedate_to_datetime`` returns a naive datetime for ``-0000`` ("no TZ
    info" per RFC 2822) and aware datetimes for everything else. Threader
    sorts and compares message dates, which raises ``TypeError`` when naive
    and aware values are mixed — so every parsed date is forced to UTC here.

    Unparseable headers return ``None``; ``parse_email`` then falls back
    to the current UTC time so threading doesn't crash, but that
    fabricates a date — log at WARNING so an operator notices a corrupt
    mailbox before the fabricated dates dominate "recent" sorts. The
    header value itself is attacker-controlled mail content and is never
    logged (#257).
    """
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
    except TypeError:
        # Older Python releases occasionally raise TypeError on malformed
        # dates; ``parsedate_to_datetime`` proper raises ValueError below.
        log.warning("Date header raised TypeError on parse; using now()")
        return None
    except ValueError:
        # ``parsedate_to_datetime`` raises ValueError on unparseable headers
        # (empty string, single-token gibberish, malformed timezone);
        # ``parse_email`` substitutes current UTC so threader doesn't crash.
        log.warning("unparseable Date header; using now()")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        return dt.astimezone(UTC)
    except OverflowError:
        # A date near year 9999 with a negative offset parses but
        # passes the largest datetime once converted to UTC.
        log.warning("Date header outside the UTC range; using now()")
        return None
