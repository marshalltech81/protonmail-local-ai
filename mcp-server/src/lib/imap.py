"""
IMAP/SMTP client for live Bridge operations.
Used for message retrieval, send, move, flag, and delete actions.
"""

import email
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses
from pathlib import Path
from typing import Any

import aioimaplib

from .security import safe_exception_text

log = logging.getLogger("mcp.imap")


def _decoded_payload(part: Any) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


@dataclass
class FullMessage:
    message_id: str
    subject: str
    from_addr: str
    to_addrs: list[str]
    cc_addrs: list[str]
    date: datetime
    body_text: str
    body_html: str
    folder: str
    imap_uid: str
    attachments: list[dict]


class IMAPClient:
    def __init__(
        self,
        host: str,
        imap_port: int,
        user: str,
        password: str,
        smtp_port: int = 1025,
        tls_cert_file: str | None = None,
        use_implicit_imap_tls: bool = False,
    ):
        self.host = host
        self.port = imap_port
        self.smtp_port = smtp_port
        self.user = user
        self.password = password
        self.tls_cert_file = tls_cert_file
        self.use_implicit_imap_tls = use_implicit_imap_tls

    async def _connect(self, folder: str = "INBOX") -> aioimaplib.IMAP4:
        context = self._tls_context()
        if not self.use_implicit_imap_tls:
            raise RuntimeError(
                "Refusing insecure live IMAP login: aioimaplib 2.0.1 does not expose a "
                "cert-validated STARTTLS upgrade path for Bridge port 1143. Keep the "
                "default write path disabled until a cert-pinned implicit TLS IMAP "
                "endpoint is explicitly configured."
            )

        client = aioimaplib.IMAP4_SSL(host=self.host, port=self.port, ssl_context=context)
        await client.wait_hello_from_server()
        await client.login(self.user, self.password)
        await client.select(folder)
        return client

    async def fetch_message(self, message_id: str, folder: str = "INBOX") -> FullMessage | None:
        """Fetch a full message by Message-ID from IMAP.

        Uses ``UID SEARCH`` / ``UID FETCH`` so the value stored on the
        returned ``FullMessage`` is a persistent UID that action tools
        (move, flag, delete) can pass back into IMAP. Plain ``SEARCH``
        returns session-local sequence numbers that become invalid as
        soon as any message in the folder is expunged.
        """
        try:
            client = await self._connect(folder)
            _, data = await client.uid_search(f'HEADER Message-ID "{message_id}"')
            uids = data[0].decode().split()
            if not uids:
                await client.logout()
                return None

            uid = uids[0]
            _, msg_data = await client.uid("FETCH", uid, "(RFC822)")
            raw = msg_data[1]
            await client.logout()

            return self._parse_full_message(raw, folder, uid)
        except Exception as e:
            log.error("Failed to fetch message %s: %s", message_id, self._safe_error(e))
            return None

    async def list_folders(self) -> list[dict]:
        """List all IMAP folders."""
        try:
            client = await self._connect()
            _, data = await client.list('""', "*")
            await client.logout()
            folders = []
            for line in data:
                if isinstance(line, bytes):
                    parts = line.decode().split('"."')
                    if parts:
                        name = parts[-1].strip().strip('"')
                        folders.append({"name": name})
            return folders
        except Exception as e:
            log.error("Failed to list folders: %s", self._safe_error(e))
            return []

    async def move_message(self, uid: str, src_folder: str, dst_folder: str) -> bool:
        """Move a message from one folder to another by UID."""
        try:
            client = await self._connect(src_folder)
            await client.uid("COPY", uid, dst_folder)
            await client.uid("STORE", uid, "+FLAGS", "\\Deleted")
            await client.expunge()
            await client.logout()
            return True
        except Exception as e:
            log.error("Failed to move message: %s", self._safe_error(e))
            return False

    async def set_flag(self, uid: str, folder: str, flag: str, value: bool) -> bool:
        """Set or unset an IMAP flag (e.g. \\Seen, \\Flagged) by UID."""
        try:
            client = await self._connect(folder)
            op = "+FLAGS" if value else "-FLAGS"
            await client.uid("STORE", uid, op, flag)
            await client.logout()
            return True
        except Exception as e:
            log.error("Failed to set flag: %s", self._safe_error(e))
            return False

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_format: str = "text",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to_message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> bool:
        """Send an email via Bridge SMTP."""
        try:
            msg = MIMEMultipart("alternative") if body_format == "html" else MIMEText(body, "plain")

            msg["From"] = self.user
            msg["To"] = ", ".join(to)
            msg["Subject"] = subject

            if cc:
                msg["Cc"] = ", ".join(cc)
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
            if references:
                msg["References"] = references

            if body_format == "html":
                msg.attach(MIMEText(body, "html"))

            context = self._tls_context()

            with smtplib.SMTP(self.host, self.smtp_port) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(self.user, self.password)
                all_recipients = to + (cc or []) + (bcc or [])
                smtp.sendmail(self.user, all_recipients, msg.as_string())
            return True
        except Exception as e:
            log.error("Failed to send email: %s", self._safe_error(e))
            return False

    def _parse_full_message(self, raw: bytes, folder: str, uid: str) -> FullMessage:
        msg = email.message_from_bytes(raw)
        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                # RFC 2183 doesn't constrain the case of ``Content-Disposition``
                # values; producers in the wild emit ``Attachment`` / ``ATTACHMENT``.
                # A case-sensitive ``"attachment" in cd`` check would miss those
                # and incorrectly fold the attachment's decoded payload into
                # ``body_text`` as if it were the message body.
                cd = part.get("Content-Disposition", "").lower()
                # Mirror the indexer parser's policy: keep attachment metadata,
                # but treat only the first plain/html body part as message
                # content so forwarded alternatives do not overwrite it.
                if "attachment" in cd:
                    attachments.append(
                        {
                            "filename": part.get_filename() or "unnamed",
                            "content_type": ct,
                            "size": len(part.get_payload(decode=True) or b""),
                        }
                    )
                elif ct == "text/plain" and not body_text:
                    payload = _decoded_payload(part)
                    body_text = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                elif ct == "text/html" and not body_html:
                    payload = _decoded_payload(part)
                    body_html = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        else:
            payload = _decoded_payload(msg)
            body_text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

        from email.utils import parsedate_to_datetime

        try:
            date = parsedate_to_datetime(msg.get("Date", ""))
        except Exception:
            date = datetime.now(UTC)

        # ``getaddresses`` parses RFC 5322 address lists correctly, including
        # display names that contain commas (``"Doe, Jane" <jane@x.com>``).
        # A naive ``split(",")`` would shred those into ``"Doe"`` and
        # ``"Jane" <jane@x.com>``.
        to_addrs = [addr for _, addr in getaddresses([msg.get("To", "")]) if addr]
        cc_addrs = [addr for _, addr in getaddresses([msg.get("Cc", "")]) if addr]

        return FullMessage(
            message_id=msg.get("Message-ID", "").strip("<>"),
            subject=msg.get("Subject", ""),
            from_addr=msg.get("From", ""),
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            date=date,
            body_text=body_text,
            body_html=body_html,
            folder=folder,
            imap_uid=uid,
            attachments=attachments,
        )

    def _tls_context(self) -> ssl.SSLContext:
        if not self.tls_cert_file:
            raise RuntimeError(
                "Pinned Bridge TLS certificate path is required for any live Bridge operation."
            )

        cert_path = Path(self.tls_cert_file)
        if not cert_path.is_file():
            raise RuntimeError(f"Pinned Bridge TLS certificate not found at {cert_path}.")

        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(cert_path))
        return context

    def _safe_error(self, error: Exception) -> str:
        return safe_exception_text(error, [self.password])
