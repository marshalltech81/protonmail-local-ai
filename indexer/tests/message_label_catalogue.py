"""The differential shape catalogue of #1416 round 5 (owner decision on
#1416): attachments labelled ``message/*`` under every transfer encoding,
carrying an OLE2 or ZIP container, a genuine email or a genuine delivery
report. Shared by ``tests/test_message_label_containers.py``."""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path

from src.parser import parse_email

from tests.conftest import make_ole2, make_zip

LABELS = (
    "message/rfc822",
    "message/global",
    "message/external-body",
    "message/delivery-status",
)
ENCODINGS = ("none", "7bit", "8bit", "binary", "base64", "quoted-printable")

# Bytes whose first bytes are an OLE2 or ZIP signature: identification
# receives them as sent. Blank lines inside the bytes split a delivery
# report into blocks and end a header block, so they are included.
CONTAINERS = {
    "zip": make_zip(
        "[Content_Types].xml", "word/document.xml", contents=b"a\r\n\r\nb\n\nSYNTHETIC_ZIP_MARKER"
    ),
    "zip-multi-blank": make_zip(
        "[Content_Types].xml", "xl/workbook.xml", contents=b"\r\n\r\n\r\n" * 4 + b"\n\n\n"
    ),
    "zip-spanned": b"PK\x07\x08" + make_zip("[Content_Types].xml", "ppt/presentation.xml"),
    "ole2": make_ole2("WordDocument", trailer=b"\r\n\r\nSYNTHETIC_OLE2_MARKER\r\n"),
}

# Genuine contents: their parse is pinned as it was before round 5.
GENUINE = {
    "email": (
        b"From: a@example.test\r\nSubject: SYNTHETIC_HEADER_MARKER\r\n\r\nSYNTHETIC_TEXT_MARKER\r\n"
    ),
    "email-with-attachment": (
        b"From: a@example.test\r\nContent-Type: multipart/mixed; boundary=in\r\n\r\n"
        b"--in\r\nContent-Type: text/plain\r\n\r\ninner body\r\n"
        b"--in\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="inner.pdf"\r\n\r\n%PDF-1.7 inner\r\n'
        b"--in--\r\n"
    ),
    "dsn-trailing-blank-lines": (
        b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
        b"Final-Recipient: rfc822; b@example.test\r\nAction: failed\r\nStatus: 5.1.1\r\n"
        b"\r\n\r\n"
    ),
    "dsn": (
        b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
        b"Final-Recipient: rfc822; b@example.test\r\nAction: failed\r\nStatus: 5.1.1\r\n"
    ),
}


def encode(payload: bytes, encoding: str) -> bytes:
    if encoding == "base64":
        return base64.encodebytes(payload)
    if encoding == "quoted-printable":
        # Binary: line breaks in the bytes are encoded too, so the decode
        # gives back every CR and LF.
        return binascii.b2a_qp(payload, quotetabs=True, istext=False)
    return payload


def message_bytes(label: str, encoding: str, payload: bytes, *, nested: bool = False) -> bytes:
    """A message whose second part is ``payload`` under ``label`` and
    ``encoding`` (no Content-Transfer-Encoding field for ``none``), or,
    with ``nested``, an attached email carrying that part."""
    cte = (
        b"" if encoding == "none" else b"Content-Transfer-Encoding: " + encoding.encode() + b"\r\n"
    )
    part = (
        b"Content-Type: "
        + label.encode()
        + b"\r\n"
        + cte
        + b'Content-Disposition: attachment; filename="SYNTHETIC_FILENAME.bin"\r\n\r\n'
        + encode(payload, encoding)
    )
    if nested:
        inner = (
            b"From: a@example.test\r\nContent-Type: multipart/mixed; boundary=in\r\n\r\n"
            b"--in\r\nContent-Type: text/plain\r\n\r\ninner body\r\n--in\r\n"
            + part
            + b"\r\n--in--\r\n"
        )
        part = (
            b"Content-Type: message/rfc822\r\n"
            b'Content-Disposition: attachment; filename="outer.eml"\r\n\r\n' + inner
        )
    return (
        b"From: s@example.test\r\nTo: r@example.test\r\nSubject: catalogue\r\n"
        b"Message-ID: <catalogue@example.test>\r\nDate: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=out\r\n\r\n"
        b"--out\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n--out\r\n"
        + part
        + b"\r\n--out--\r\n"
    )


def digest(data: bytes) -> str:
    """``sha256:<hex>``, as the parser pin writes digests."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def outcome(path: Path, data: bytes) -> list[tuple[str, str, bool]]:
    """Each attachment the parser records: (content type, digest of the
    payload, payload_complete)."""
    path.write_bytes(data)
    msg = parse_email(path)
    assert msg is not None
    return [(a.content_type, digest(a.payload), a.payload_complete) for a in msg.attachments]
