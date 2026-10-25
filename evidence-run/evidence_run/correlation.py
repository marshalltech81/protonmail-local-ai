"""Bounded, top-level correlation evidence; no body or identifier logging."""

from __future__ import annotations

import email.errors
import email.policy
import re
from email.parser import BytesHeaderParser

from .runner import MessageSummary

MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_FIELDS = 256
MAX_CORRELATION_FIELD_CHARS = 998
_TOKEN = re.compile(r"SYNTH-[A-Za-z0-9]{1,16}-[0-9]+")


def summary(raw: bytes) -> MessageSummary:
    """Read unambiguous Subject and From through the existing email parser.

    Bound header bytes, fields and parsed correlation-field length together
    before header-registry address parsing. An incomplete or malformed
    header never supplies a token or an allowed-domain observation.
    """
    unknown = MessageSummary(None, None)
    prefix = raw[: MAX_HEADER_BYTES + 4].replace(b"\r\n", b"\n")
    head, separator, _ = prefix.partition(b"\n\n")
    if not separator or len(head) > MAX_HEADER_BYTES:
        return unknown
    if sum(line[:1] not in (b" ", b"\t") for line in head.split(b"\n")) > MAX_HEADER_FIELDS:
        return unknown
    try:
        headers = BytesHeaderParser(policy=email.policy.default).parsebytes(head + b"\n\n")
        if headers.defects:
            return unknown
        fields: dict[str, list[str]] = {name: [] for name in ("subject", "from")}
        for name, value in headers.raw_items():
            if name.lower() in fields:
                fields[name.lower()].append(value)
        if any(
            len(values) != 1 or len(values[0]) > MAX_CORRELATION_FIELD_CHARS
            for values in fields.values()
        ):
            return unknown
        subject, sender = headers["Subject"], headers["From"]
        if subject.defects or sender.defects or len(sender.addresses) != 1:
            return unknown
        address = sender.addresses[0]
        if not address.username or not address.domain:
            return unknown
        text = str(subject)
        token = text if _TOKEN.fullmatch(text) else None
        return MessageSummary(token, address.domain.lower())
    except email.errors.MessageError, email.errors.MessageDefect, ValueError, LookupError:
        return unknown
