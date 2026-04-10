"""
Email parser.
Reads raw .eml files from Maildir and returns structured Message objects.
Handles MIME, HTML-to-text conversion, and attachment metadata.
"""
import email
import email.message
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import html2text

log = logging.getLogger("indexer.parser")

h2t = html2text.HTML2Text()
h2t.ignore_links = True
h2t.ignore_images = True
h2t.body_width = 0


@dataclass
class Attachment:
    filename: str
    content_type: str
    size: int


@dataclass
class Message:
    message_id: str
    in_reply_to: Optional[str]
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


def parse_email(path: Path) -> Optional[Message]:
    """Parse a single .eml file from Maildir into a Message object."""
    try:
        raw = path.read_bytes()
        msg = email.message_from_bytes(raw)

        message_id = _clean_id(msg.get("Message-ID", ""))
        if not message_id:
            log.debug(f"Skipping message with no Message-ID: {path}")
            return None

        in_reply_to = _clean_id(msg.get("In-Reply-To", ""))
        references = [
            _clean_id(r)
            for r in msg.get("References", "").split()
            if r.strip()
        ]

        subject = _decode_header(msg.get("Subject", "(no subject)"))
        from_addr = _decode_header(msg.get("From", ""))
        to_addrs = _parse_addrs(msg.get("To", ""))
        cc_addrs = _parse_addrs(msg.get("Cc", ""))
        date = _parse_date(msg.get("Date", ""))

        body_text, attachments = _extract_body_and_attachments(msg)

        # Derive folder from path — Maildir structure is folder/cur|new/file
        folder = path.parent.parent.name

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
        )

    except Exception as e:
        log.error(f"Failed to parse {path}: {e}")
        return None


def _extract_body_and_attachments(
    msg: email.message.Message,
) -> tuple[str, list[Attachment]]:
    body_text = ""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")

            if "attachment" in cd or "inline" in cd:
                filename = part.get_filename() or "unnamed"
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    Attachment(
                        filename=filename,
                        content_type=ct,
                        size=len(payload),
                    )
                )
            elif ct == "text/plain" and not body_text:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body_text = payload.decode(charset, errors="replace")
            elif ct == "text/html" and not body_text:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                body_text = h2t.handle(html)
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        if ct == "text/html":
            body_text = h2t.handle(payload.decode(charset, errors="replace"))
        else:
            body_text = payload.decode(charset, errors="replace")

    return body_text.strip(), attachments


def _clean_id(value: str) -> str:
    return value.strip().strip("<>").strip()


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def _parse_addrs(value: str) -> list[str]:
    if not value:
        return []
    return [addr.strip() for addr in value.split(",") if addr.strip()]


def _parse_date(value: str) -> datetime:
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value)
    except Exception:
        return datetime.utcnow()
