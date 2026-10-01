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
import email.utils
import hashlib
import logging
import os
import quopri
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import html2text

log = logging.getLogger("indexer.parser")


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


# Hard ceiling on the size of a single ``.eml`` we will read into
# memory. Bridge inbound usually caps at ~25 MB, but a malicious /
# corrupt Maildir file with no such bound would otherwise let a
# single ``read_bytes`` call exhaust the indexer container's memory
# (default ``mem_limit: 6g``). 50 MB is comfortably above any
# legitimate message and well below the container ceiling. ``0``
# disables the cap; operators on environments with larger inbound
# limits can raise it via ``INDEXER_PARSE_MAX_BYTES``.
_DEFAULT_PARSE_MAX_BYTES = 50_000_000


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


@dataclass
class Attachment:
    """One MIME attachment from a message.

    ``payload`` holds the raw decoded bytes for the lifetime of the
    indexer pass — the extractor needs them to pull text out of PDFs,
    DOCX, images, etc. They are not persisted anywhere; once the
    indexer has chunked + embedded any extracted text, the Attachment
    object goes out of scope and the bytes are GC'd. Callers that only
    need metadata can ignore ``payload``.

    ``content_hash`` is the SHA-256 of ``payload`` and acts as the
    deduplication key in ``attachment_extractions`` — a forwarded PDF
    is OCR'd / parsed once per content, regardless of how many emails
    carry it.
    """

    filename: str
    content_type: str
    size: int
    payload: bytes = b""
    content_hash: str = ""


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


def parse_email(path: Path, maildir_root: Path | None = None) -> Message | None:
    """Parse a single .eml file from Maildir into a Message object.

    ``maildir_root`` — when provided, the folder is derived as the relative
    path from the root to the directory that contains ``cur/``/``new/``.
    mbsync ``SubFolders Verbatim`` can nest folders more than one level
    deep (``Clients/ABC``, ``Archive/2023``); without the root, nested
    folders collapse to only the leaf directory name and unrelated threads
    can be merged by the subject-only fallback.

    When ``maildir_root`` is not provided the folder falls back to the
    leaf name (``path.parent.parent.name``).

    Transient I/O errors (``PermissionError`` from the mbsync 0600→0644
    chmod race, ``FileNotFoundError`` from a rename mid-event) propagate
    so the worker's queue routes them to the retry/backoff path rather
    than collapsing them into ``None`` — which the worker treats as a
    permanent "no Message-ID" outcome and dead-letters without retry.

    Content-pathology errors (a malformed MIME structure ``email`` cannot
    decompose, an html2text blowup, anything raised by the body /
    attachment walker that is not already caught locally) also propagate.
    The previous bare ``except Exception`` collapsed those into the same
    ``None`` channel as missing-Message-ID, which silently un-indexed
    every affected file with no dead-letter visibility. Letting the
    exception escape routes the row through the queue's retry +
    dead-letter cascade so operators see persistent parser bugs instead
    of a quietly shrinking index.

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
    msg = email.message_from_bytes(raw)

    message_id = _clean_id(msg.get("Message-ID", ""))
    if not message_id:
        log.debug(f"Skipping message with no Message-ID: {path}")
        return None

    in_reply_to = _clean_id(msg.get("In-Reply-To", ""))
    references = [_clean_id(r) for r in msg.get("References", "").split() if r.strip()]

    subject = _decode_header(msg.get("Subject", "(no subject)"))
    # Parse From structurally, like To / Cc: decoding the whole header
    # first turns an encoded name with a comma ("=?utf-8?q?Doe=2C_Jane?=")
    # into an unquoted "Doe, Jane <...>" that no longer parses as one
    # address, and a multi-author From would be read as a single address.
    from_addrs = _parse_addrs(msg.get("From", ""))
    from_addr = from_addrs[0] if from_addrs else _decode_header(msg.get("From", ""))
    to_addrs = _parse_addrs(msg.get("To", ""))
    cc_addrs = _parse_addrs(msg.get("Cc", ""))
    # A raw 8-bit Date header comes back as an ``email.header.Header``,
    # which ``parsedate_to_datetime`` cannot split (#361). A Date header
    # is ASCII by RFC 5322, so drop anything else from its text: a
    # stray byte glued to the zone ("+0500\xe9") would otherwise hide
    # the offset and the time would be read as UTC.
    date_text = str(msg.get("Date", "")).encode("ascii", "ignore").decode("ascii")
    parsed_date = _parse_date(date_text)
    date = parsed_date if parsed_date is not None else datetime.now(UTC)

    body_text, attachments = _extract_body_and_attachments(msg)

    folder = _derive_folder(path, maildir_root)

    # Capture file identity. ``size`` is the length of the
    # bytes we actually hashed; ``content_hash`` is computed over the
    # raw file — not the decoded body — so flag-only renames keep the
    # same hash while any real content mutation shows up as a mismatch.
    # ``mtime_ns`` reuses the ``stat`` captured above for the size cap
    # check. A ``stat`` failure is treated as "identity unknown"
    # rather than a parse failure: the file was just read
    # successfully, so the row still belongs in the index. Future
    # passes can backfill.
    size = len(raw)
    content_hash = hashlib.sha256(raw).hexdigest()
    mtime_ns = stat.st_mtime_ns if stat is not None else None

    return Message(
        message_id=message_id,
        in_reply_to=in_reply_to or None,
        references=references,
        subject=subject,
        from_addr=from_addr,
        from_addrs=from_addrs,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        date=date,
        date_is_fallback=parsed_date is None,
        body_text=body_text,
        folder=folder,
        filepath=str(path),
        attachments=attachments,
        has_attachments=len(attachments) > 0,
        size=size,
        mtime_ns=mtime_ns,
        content_hash=content_hash,
    )


def _derive_folder(path: Path, maildir_root: Path | None) -> str:
    """Derive a Maildir folder name for a message path.

    With a root provided, returns the POSIX-style relative path from the
    root to the directory containing ``cur/``/``new/`` — so
    ``/maildir/Clients/ABC/cur/msg`` becomes ``Clients/ABC`` rather than
    collapsing to ``ABC``. Falls back to the leaf directory name when the
    path is outside the root (legacy behavior, for tests that do not pass
    a root).
    """
    folder_dir = path.parent.parent
    if maildir_root is not None:
        try:
            return folder_dir.relative_to(maildir_root).as_posix()
        except ValueError:
            pass
    return folder_dir.name


def _is_attachment(part: email.message.Message) -> bool:
    """True when ``part`` is presented as a file rather than as text."""
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
    return bool(part.get_filename()) or "attachment" in cd


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
    stack = [(root, 1)]
    while stack:
        part, depth = stack.pop()
        headers = part.items()
        header_bytes = sum(len(name) + len(str(value)) for name, value in headers)
        budget.remaining -= 1 + len(headers) + header_bytes // _HEADER_BYTES_PER_UNIT
        if depth > limit or budget.remaining < 0:
            return True
        children = part.get_payload() if part.is_multipart() else None
        if isinstance(children, list):
            stack.extend((c, depth + 1) for c in children if isinstance(c, email.message.Message))
    return False


def _attachment_payload(
    part: email.message.Message,
    *,
    serialize_containers: bool,
    budget: _SerializationBudget,
    decode_depth: int = 0,
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
        if decode_depth >= MAX_ATTACHED_MESSAGE_DEPTH or _nesting_exceeds(
            part, MAX_ATTACHED_MESSAGE_DEPTH + 1, budget
        ):
            return b"", None
        try:
            transport = _transport_text(part)
        except email.errors.MessageError, UnicodeError:
            return b"", None
        budget.decodable -= len(transport)
        if budget.decodable < 0:
            return b"", None
        content_type = str(part.get("Content-Type", "message/rfc822"))
        decoded = _decode_transport_form(transport, encoding, content_type)
        if decoded is None:
            return b"", None
        # From here the decoded container is the part: the same depth
        # check, serialization and traversal as an identity-encoded one.
        part = decoded
        if _nesting_exceeds(part, MAX_ATTACHED_MESSAGE_DEPTH + 1, budget):
            return b"", None
        if not serialize_containers:
            return b"", part
        try:
            return _serialized_body(part), part
        except email.errors.MessageError, UnicodeError:
            return b"", part
    if not serialize_containers:
        return b"", None
    # The part's own tree is one level deeper than the email it carries.
    if _nesting_exceeds(part, MAX_ATTACHED_MESSAGE_DEPTH + 1, budget):
        return b"", None
    try:
        return _serialized_body(part), None
    except email.errors.MessageError, UnicodeError:
        return b"", None


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


def _extract_body_and_attachments(
    msg: email.message.Message,
) -> tuple[str, list[Attachment]]:
    plain_text = ""
    html_text = ""
    attachments: list[Attachment] = []

    # Depth-first in document order, like ``msg.walk()``, but nothing
    # inside an attachment is a candidate for the body: an attached
    # email's text is not the parent's. Attachments inside it are still
    # recorded, as the old walk did (a PDF in a forwarded email).
    # Iterative, so nesting depth cannot recurse. The root is classified
    # too: a message can be one attachment part, or a bundle presented as
    # one, whose text is then not the message's body.
    budget = _SerializationBudget()
    stack: list[tuple[email.message.Message, bool, int]] = [(msg, False, 0)]
    while stack:
        part, in_attachment, decode_depth = stack.pop()
        ct = part.get_content_type()
        is_attachment = _is_attachment(part)
        decoded: email.message.Message | None = None
        if is_attachment:
            payload, decoded = _attachment_payload(
                part,
                serialize_containers=not in_attachment,
                budget=budget,
                decode_depth=decode_depth,
            )
            attachments.append(
                Attachment(
                    filename=part.get_filename() or "unnamed",
                    content_type=ct,
                    size=len(payload),
                    payload=payload,
                    content_hash=hashlib.sha256(payload).hexdigest(),
                )
            )
        if part.is_multipart():
            # A decoded container stands in for its transport form; its
            # children are one decode deeper.
            if decoded is not None:
                children, depth = decoded.get_payload(), decode_depth + 1
            else:
                children, depth = part.get_payload(), decode_depth
            if isinstance(children, list):
                inside = in_attachment or is_attachment
                stack.extend(
                    (c, inside, depth)
                    for c in reversed(children)
                    if isinstance(c, email.message.Message)
                )
        elif is_attachment or in_attachment:
            continue
        elif ct == "text/html":
            if not html_text:
                payload = _decoded_payload(part)
                charset = part.get_content_charset() or "utf-8"
                html_text = _html_to_text(_safe_decode(payload, charset))
        elif ct == "text/plain" or (part is msg and part.get_content_maintype() == "text"):
            # A single-part message's text is its body whatever the text
            # subtype (text/calendar, text/enriched); inside a multipart
            # only text/plain is. Binary parts are never decoded as text.
            if not plain_text:
                payload = _decoded_payload(part)
                charset = part.get_content_charset() or "utf-8"
                plain_text = _safe_decode(payload, charset)

    # Prefer ``text/plain`` over ``text/html`` regardless of the order parts
    # appear in the message — otherwise a multipart where the HTML part
    # comes first wins, and the LLM gets html2text-converted output even
    # when the sender provided a clean plain-text body. A whitespace-only
    # plain part has no content to prefer, so the HTML text is used.
    plain_text = plain_text.strip()
    return plain_text or html_text.strip(), attachments


def _safe_decode(payload: bytes, charset: str) -> str:
    """Decode payload bytes, falling back to utf-8 on unknown charsets.

    ``UnicodeError`` covers codecs that reject ``errors="replace"``
    (``idna`` raises ``UnicodeError("Unsupported error handling")``).
    """
    try:
        return payload.decode(charset, errors="replace")
    except LookupError, UnicodeError:
        return payload.decode("utf-8", errors="replace")


def _clean_id(value: str) -> str:
    return value.strip().strip("<>").strip()


def _decode_header(value: str | email.header.Header) -> str:
    """Decode a header value's RFC 2047 encoded-words.

    A ``Header`` (raw 8-bit header) already holds decoded chunks. A
    string is scanned for encoded-words in one linear pass and each one
    is decoded on its own: ``decode_header`` on the whole string rescans
    the rest of the header at every malformed ``=?`` prefix, which is
    quadratic in a hostile Subject.
    """
    if isinstance(value, email.header.Header):
        return _decode_header_parts(email.header.decode_header(value)).strip()
    if "=?" not in value:
        return value.strip()
    return _decode_encoded_word_runs(
        value, lambda m: _decode_header_parts(email.header.decode_header(m.group(0)))
    ).strip()


def _decode_header_parts(parts: list[tuple[bytes | str, str | None]]) -> str:
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            # ``charset`` is whatever the sender claimed in the MIME header;
            # obscure or invalid labels ("x-mac-romanian", typos, historical
            # aliases) raise LookupError, and a codec that rejects
            # ``errors="replace"`` (``idna``) raises UnicodeError. Handle
            # both locally with a utf-8 fallback and ``errors="replace"``
            # so a single bad header does not affect the rest of the
            # message. Anything we DON'T catch
            # here propagates out of ``parse_email``: the function does
            # not have a blanket ``except Exception`` precisely so
            # unanticipated parser failures route through the durable
            # queue's retry + dead-letter cascade instead of being
            # dead-lettered as unindexable without any retry.
            encoding = charset or "utf-8"
            try:
                decoded.append(part.decode(encoding, errors="replace"))
            except LookupError, UnicodeError:
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


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


def _parse_addrs(value: str | email.header.Header) -> list[str]:
    """Parse an address header into one parseable string per address.

    Raw 8-bit headers (UTF-8 written directly in the header) arrive as
    ``email.header.Header`` objects whose ``str()`` mangles the non-ASCII
    bytes, so they are decoded first. Encoded-words are then made opaque,
    the list is split at top level, and each element is parsed strictly
    on its own; display names are decoded only after the address is
    fixed, so name content can never become address syntax. Every step
    fails safe: an element that cannot be parsed costs only that
    recipient, never the message.
    """
    if not value:
        return []
    text = _decode_header(value) if isinstance(value, email.header.Header) else value
    if len(text) > _MAX_ADDRESS_HEADER_CHARS:
        log.debug("address header over %d chars; recipients not parsed", _MAX_ADDRESS_HEADER_CHARS)
        return []
    protected, restore = _protect_encoded_words(text)
    addresses = []
    for element in _split_address_list(protected):
        if len(element) > _MAX_ADDRESS_ELEMENT_CHARS:
            continue
        try:
            name, addr = email.utils.parseaddr(element)
            if not addr.strip():
                continue
            addr = restore(addr)
            # Every emitted string is re-parsed downstream — the identity
            # check in _format_address, canonical_addr in the threader,
            # the participant writer, the MCP contact reader. A restored
            # address must therefore survive re-parsing unchanged: one
            # that is not a parseaddr fixed point (an unsafe restored
            # encoded-word, say) is discarded HERE, inside the failure
            # boundary, not handed to an unguarded reparser later.
            if len(addr) > _MAX_ADDRESS_CHARS or email.utils.parseaddr(addr)[1] != addr:
                continue
            name = _decode_display_name(restore(name)) if name else ""
            formatted = _format_address(name, addr)
        except Exception:
            # e.g. RecursionError from parseaddr on deeply nested comments,
            # whether in the element or re-created by restoring a token.
            continue
        addresses.append(formatted)
    return addresses


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
    if dt is None:
        log.warning("Date header parsed to None; using now()")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
