"""
Email parser.
Reads raw .eml files from Maildir and returns structured Message objects.
Handles MIME, HTML-to-text conversion, and attachment metadata.
"""

import email
import email.errors
import email.header
import email.message
import email.utils
import hashlib
import logging
import os
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
    Message-ID, etc.) so the indexer worker can route oversized files
    through ``mark_skipped(reason="oversized")`` rather than
    ``mark_succeeded`` — the file was never indexed, and the queue
    log line should reflect that for operator visibility.
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


def parse_email(path: Path, maildir_root: Path | None = None) -> Message | None:
    """Parse a single .eml file from Maildir into a Message object.

    ``maildir_root`` — when provided, the folder is derived as the relative
    path from the root to the directory that contains ``cur/``/``new/``.
    mbsync ``SubFolders Verbatim`` can nest folders more than one level
    deep (``Clients/ABC``, ``Archive/2023``); without the root, nested
    folders collapse to only the leaf directory name and unrelated threads
    can be merged by the subject-only fallback.

    When ``maildir_root`` is not provided the folder falls back to the
    leaf name (``path.parent.parent.name``) for backward compatibility.

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
    error and routes the row through ``mark_skipped(reason="oversized")``
    — terminal, no retry, but visible in operator logs as a skip
    rather than a silent success-deletion.
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
                "Skipping oversized email %s (%d bytes > %d cap); "
                "raise INDEXER_PARSE_MAX_BYTES to ingest, or 0 to disable.",
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
                    "Skipping oversized email %s (>%d cap, detected during read); "
                    "raise INDEXER_PARSE_MAX_BYTES to ingest, or 0 to disable.",
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
    date = _parse_date(msg.get("Date", ""))

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


def _extract_body_and_attachments(
    msg: email.message.Message,
) -> tuple[str, list[Attachment]]:
    plain_text = ""
    html_text = ""
    attachments: list[Attachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            # Content-Disposition values are case-insensitive per RFC 2183.
            # The old ``"attachment" in cd`` check missed ``Attachment``,
            # ``ATTACHMENT``, and similar variants some clients emit,
            # causing real attachments to be decoded as the body or vice
            # versa.
            cd = part.get("Content-Disposition", "").lower()
            has_filename = bool(part.get_filename())

            # Any part carrying a filename is treated as an attachment.
            # Message bodies are normally ``text/plain`` / ``text/html``
            # with no filename; anything that was given a filename is,
            # by convention, intended to be presented as a file. Some
            # clients also omit ``Content-Disposition`` entirely on
            # attachment parts — the explicit disposition check below
            # covers the filename-less ``Content-Disposition: attachment``
            # case, while the ``has_filename`` branch covers dispositions
            # that are absent, non-standard, or ``inline`` with a file.
            is_attachment = has_filename or "attachment" in cd

            if is_attachment:
                filename = part.get_filename() or "unnamed"
                payload = _decoded_payload(part)
                attachments.append(
                    Attachment(
                        filename=filename,
                        content_type=ct,
                        size=len(payload),
                        payload=payload,
                        content_hash=hashlib.sha256(payload).hexdigest(),
                    )
                )
            elif ct == "text/plain" and not plain_text:
                payload = _decoded_payload(part)
                charset = part.get_content_charset() or "utf-8"
                plain_text = _safe_decode(payload, charset)
            elif ct == "text/html" and not html_text:
                payload = _decoded_payload(part)
                charset = part.get_content_charset() or "utf-8"
                html_text = _html_to_text(_safe_decode(payload, charset))
    else:
        ct = msg.get_content_type()
        payload = _decoded_payload(msg)
        charset = msg.get_content_charset() or "utf-8"
        if ct == "text/html":
            html_text = _html_to_text(_safe_decode(payload, charset))
        else:
            plain_text = _safe_decode(payload, charset)

    # Prefer ``text/plain`` over ``text/html`` regardless of the order parts
    # appear in the message — otherwise a multipart where the HTML part
    # comes first wins, and the LLM gets html2text-converted output even
    # when the sender provided a clean plain-text body.
    body_text = plain_text or html_text
    return body_text.strip(), attachments


def _safe_decode(payload: bytes, charset: str) -> str:
    """Decode payload bytes, falling back to utf-8 on unknown charsets."""
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
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
    value = _ADJACENT_ENCODED_WORDS_RE.sub("?==?", value)
    return _ENCODED_WORD_RE.sub(
        lambda m: _decode_header_parts(email.header.decode_header(m.group(0))), value
    ).strip()


def _decode_header_parts(parts: list[tuple[bytes | str, str | None]]) -> str:
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            # ``charset`` is whatever the sender claimed in the MIME header;
            # obscure or invalid labels ("x-mac-romanian", typos, historical
            # aliases) raise LookupError. Handle that locally with a utf-8
            # fallback and ``errors="replace"`` so a single bad header does
            # not affect the rest of the message. Anything we DON'T catch
            # here propagates out of ``parse_email``: the function does
            # not have a blanket ``except Exception`` precisely so
            # unanticipated parser failures route through the durable
            # queue's retry + dead-letter cascade instead of being
            # dead-lettered as unindexable without any retry.
            encoding = charset or "utf-8"
            try:
                decoded.append(part.decode(encoding, errors="replace"))
            except LookupError:
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
_ADJACENT_ENCODED_WORDS_RE = re.compile(r"\?=\s+=\?")
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
    name = _ADJACENT_ENCODED_WORDS_RE.sub("?==?", name)
    return _ENCODED_WORD_RE.sub(_decode_encoded_word, name)


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


def _parse_date(value: str) -> datetime:
    """Parse an RFC 2822 date header and normalize to a UTC-aware datetime.

    ``parsedate_to_datetime`` returns a naive datetime for ``-0000`` ("no TZ
    info" per RFC 2822) and aware datetimes for everything else. Threader
    sorts and compares message dates, which raises ``TypeError`` when naive
    and aware values are mixed — so every parsed date is forced to UTC here.

    Unparseable headers fall back to the current UTC time so threading
    doesn't crash, but that fabricates a date — log at WARNING with the
    offending value so an operator notices a corrupt mailbox before the
    fabricated dates dominate "recent" sorts.
    """
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
    except TypeError:
        # Older Python releases occasionally raise TypeError on malformed
        # dates; ``parsedate_to_datetime`` proper raises ValueError below.
        log.warning("date header type error, using now(): %r", value)
        return datetime.now(UTC)
    except ValueError:
        # ``parsedate_to_datetime`` raises ValueError on unparseable headers
        # (empty string, single-token gibberish, malformed timezone). Force
        # current UTC so threader doesn't crash on a bad header.
        log.warning("date header unparseable, using now(): %r", value)
        return datetime.now(UTC)
    if dt is None:
        log.warning("date header parsed to None, using now(): %r", value)
        return datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
