"""The pinned-TLS IMAP capture of Bridge's served bytes (procedure v5
1.9, 5.3, P11).

The shape follows the entrypoint's: look at the certificate the server
presents, compare its SHA-256 fingerprint with the configured one **before
any credential is sent**, then connect again trusting only that certificate
with hostname verification on, so a certificate swapped between the two
connections fails the handshake before ``LOGIN``. Read-only throughout:
``EXAMINE`` and ``BODY.PEEK``, never a command that changes the far side.
Sizes are learned first and bounded before any body is fetched.

Errors are fixed tokens; the password is never in one.
"""

from __future__ import annotations

import hashlib
import imaplib
import os
import re
import ssl
import stat
from pathlib import Path

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_UID = re.compile(rb"UID (\d+)")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")


class CaptureError(Exception):
    """A capture failed. ``token`` is a fixed reason; nothing else is carried."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


def normalize_fingerprint(text: str) -> str:
    cleaned = text.strip().replace(":", "").lower()
    if not _FINGERPRINT.fullmatch(cleaned):
        raise CaptureError("fingerprint_format")
    return cleaned


def read_secret_file(path: Path | str) -> str:
    """The first line of a mode-600 (or stricter) regular file."""
    try:
        info = os.stat(path)
    except OSError:
        raise CaptureError("password_file_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise CaptureError("password_file_mode")
    return Path(path).read_text().splitlines()[0] if info.st_size else ""


def pinned_context(pem: str) -> ssl.SSLContext:
    """A client context that trusts only ``pem``, with certificate and
    hostname verification on. A self-signed leaf is its own anchor, so
    partial-chain is allowed and the strict CA-flag check is not applied to
    it; verification itself is never weakened."""
    context = ssl.create_default_context(cadata=pem)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def capture(
    host: str,
    port: int,
    *,
    expected_fingerprint: str,
    user: str,
    password_file: Path | str,
    folder: str = "INBOX",
    timeout: float = 30.0,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_messages: int = 1000,
) -> dict[int, bytes]:
    """The messages of ``folder`` as served, by UID."""
    expected = normalize_fingerprint(expected_fingerprint)
    if not folder or any(c in folder for c in "\r\n\x00"):
        raise CaptureError("bad_folder_name")  # refused before any connection
    password = read_secret_file(password_file)  # refused before any connection
    try:
        pem = ssl.get_server_certificate((host, port), timeout=timeout)
    except OSError, ssl.SSLError:
        raise CaptureError("tls_unavailable") from None
    if hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() != expected:
        raise CaptureError("fingerprint_mismatch")
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=pinned_context(pem), timeout=timeout)
    except ssl.SSLCertVerificationError:
        raise CaptureError("tls_verification_failed") from None
    except OSError, ssl.SSLError, imaplib.IMAP4.error:
        raise CaptureError("tls_failed") from None
    try:
        conn.login(user, password)
        status, selected = conn.select(folder, readonly=True)  # EXAMINE
        if status != "OK":
            raise CaptureError("folder_unavailable")
        # imaplib quotes the name itself (Python 3.14's select uses _astring).
        # An empty folder is read from EXISTS, not from a FETCH reply that a
        # server may answer with NO or with nothing.
        if selected and selected[0] is not None and int(selected[0]) == 0:
            return {}
        status, data = conn.uid("FETCH", "1:*", "(UID RFC822.SIZE)")
        if status != "OK":
            raise CaptureError("size_fetch_failed")  # never "an empty folder"
        sizes: dict[int, int] = {}
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if (
                isinstance(line, bytes)
                and (uid_match := _UID.search(line))
                and (size_match := _SIZE.search(line))
            ):
                sizes[int(uid_match.group(1))] = int(size_match.group(1))
        if len(sizes) > max_messages or sum(sizes.values()) > max_total_bytes:
            raise CaptureError("capture_cap")
        captured: dict[int, bytes] = {}
        for uid in sorted(sizes):
            status, data = conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
            literal = next((i[1] for i in data or [] if isinstance(i, tuple)), None)
            if status != "OK" or literal is None:
                raise CaptureError("fetch_failed")
            captured[uid] = bytes(literal)
        return captured
    except CaptureError:
        raise
    except OSError, ssl.SSLError, imaplib.IMAP4.error:
        raise CaptureError("capture_failed") from None
    finally:
        try:
            conn.logout()
        except OSError, imaplib.IMAP4.error:
            pass
