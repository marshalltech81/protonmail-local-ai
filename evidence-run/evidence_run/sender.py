"""The external sender: stdlib ``smtplib`` over implicit TLS with a verified
certificate (procedure v5 2.4). The message goes out as the bytes given,
duplicate and forged header fields untouched; credentials come from a
mode-600 file and are never in a result or an error.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from pathlib import Path

from .tlscapture import CaptureError, read_secret_file


class SendError(Exception):
    """A send could not start. ``token`` is a fixed reason."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


@dataclass(frozen=True)
class SendResult:
    # sent | refused | ambiguous | tls_failed | auth_failed | connection_failed
    token: str


def _context(cafile: Path | str | None) -> ssl.SSLContext:
    if cafile is None:
        return ssl.create_default_context()  # system trust, hostname verified
    context = ssl.create_default_context(cafile=str(cafile))
    # A pinned self-signed leaf is its own anchor; verification stays on.
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def send(
    raw: bytes,
    *,
    host: str,
    port: int,
    mail_from: str,
    rcpt_to: str,
    user: str,
    password_file: Path | str,
    cafile: Path | str | None = None,
    timeout: float = 30.0,
) -> SendResult:
    try:
        password = read_secret_file(password_file)  # refused before connecting
    except CaptureError as exc:
        raise SendError(exc.token) from None
    try:
        smtp = smtplib.SMTP_SSL(host, port, context=_context(cafile), timeout=timeout)
    except ssl.SSLError:
        return SendResult("tls_failed")
    except OSError, smtplib.SMTPException:
        return SendResult("connection_failed")
    try:
        try:
            smtp.login(user, password)
        except smtplib.SMTPAuthenticationError:
            return SendResult("auth_failed")
        except OSError, smtplib.SMTPException:
            return SendResult("connection_failed")
        try:
            smtp.sendmail(mail_from, [rcpt_to], raw)
        except smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError:
            return SendResult("refused")
        except TimeoutError, smtplib.SMTPServerDisconnected, OSError:
            # The server may or may not have queued it; the budget counts
            # this attempt and the run decides, never a silent resend.
            return SendResult("ambiguous")
        return SendResult("sent")
    finally:
        try:
            smtp.quit()
        except OSError, smtplib.SMTPException:
            pass
