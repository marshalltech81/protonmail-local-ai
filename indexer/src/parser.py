"""
Email parser.
Reads raw .eml files from Maildir and returns structured Message objects.
Handles MIME, HTML-to-text conversion, and attachment metadata.
"""

import email
import email.message
import email.utils
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import html2text

log = logging.getLogger("indexer.parser")

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
    return max(0, value)


h2t = html2text.HTML2Text()
h2t.ignore_links = True
h2t.ignore_images = True
h2t.body_width = 0


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
    than collapsing them into ``None`` — which the worker treats as
    "terminal success, no Message-ID" and would silently drop the file
    from the index.

    Content-pathology errors (a malformed MIME structure ``email`` cannot
    decompose, an html2text blowup, anything raised by the body /
    attachment walker that is not already caught locally) also propagate.
    The previous bare ``except Exception`` collapsed those into the same
    ``None`` channel as missing-Message-ID, which silently un-indexed
    every affected file with no dead-letter visibility. Letting the
    exception escape routes the row through the queue's retry +
    dead-letter cascade so operators see persistent parser bugs instead
    of a quietly shrinking index.

    Files larger than ``INDEXER_PARSE_MAX_BYTES`` (default 50 MB) are
    skipped before ``read_bytes`` so a malicious or corrupt Maildir
    entry cannot exhaust container memory. A skipped file returns
    ``None`` so the queue marks the row succeeded and stops retrying —
    the file will not shrink on retry, and an unbounded retry against
    the cap would just burn embed-budget without progress.
    """
    # ``stat`` doubles as the size-cap pre-check AND the source of the
    # ``mtime_ns`` we capture below. A single OSError covers both.
    try:
        stat = os.stat(path)
    except OSError:
        stat = None

    cap = _parse_max_bytes()
    if cap > 0 and stat is not None and stat.st_size > cap:
        log.warning(
            "Skipping oversized email %s (%d bytes > %d cap); "
            "raise INDEXER_PARSE_MAX_BYTES to ingest, or 0 to disable.",
            path,
            stat.st_size,
            cap,
        )
        return None

    raw = path.read_bytes()
    msg = email.message_from_bytes(raw)

    message_id = _clean_id(msg.get("Message-ID", ""))
    if not message_id:
        log.debug(f"Skipping message with no Message-ID: {path}")
        return None

    in_reply_to = _clean_id(msg.get("In-Reply-To", ""))
    references = [_clean_id(r) for r in msg.get("References", "").split() if r.strip()]

    subject = _decode_header(msg.get("Subject", "(no subject)"))
    from_addr = _decode_header(msg.get("From", ""))
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
                html_text = h2t.handle(_safe_decode(payload, charset))
    else:
        ct = msg.get_content_type()
        payload = _decoded_payload(msg)
        charset = msg.get_content_charset() or "utf-8"
        if ct == "text/html":
            html_text = h2t.handle(_safe_decode(payload, charset))
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


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
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
            # queue's retry + dead-letter cascade instead of silently
            # dropping the file as terminal success.
            encoding = charset or "utf-8"
            try:
                decoded.append(part.decode(encoding, errors="replace"))
            except LookupError:
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def _parse_addrs(value: str) -> list[str]:
    """Parse an address header, handling display names with commas correctly."""
    if not value:
        return []
    pairs = email.utils.getaddresses([value])
    return [f"{name} <{addr}>" if name else addr for name, addr in pairs if addr.strip()]


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
