"""
IMAP/SMTP client for live Bridge operations.
Used for message retrieval, send, move, flag, and delete actions.
"""
import asyncio
import email
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import aioimaplib

log = logging.getLogger("mcp.imap")


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
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    async def _connect(self, folder: str = "INBOX") -> aioimaplib.IMAP4:
        client = aioimaplib.IMAP4(host=self.host, port=self.port)
        await client.wait_hello_from_server()
        await client.login(self.user, self.password)
        await client.select(folder)
        return client

    async def fetch_message(
        self, message_id: str, folder: str = "INBOX"
    ) -> Optional[FullMessage]:
        """Fetch a full message by Message-ID from IMAP."""
        try:
            client = await self._connect(folder)
            # Search by Message-ID header
            _, data = await client.search(
                f'HEADER Message-ID "{message_id}"'
            )
            uids = data[0].decode().split()
            if not uids:
                await client.logout()
                return None

            uid = uids[0]
            _, msg_data = await client.fetch(uid, "(RFC822)")
            raw = msg_data[1]
            await client.logout()

            return self._parse_full_message(raw, folder, uid)
        except Exception as e:
            log.error(f"Failed to fetch message {message_id}: {e}")
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
            log.error(f"Failed to list folders: {e}")
            return []

    async def move_message(
        self, uid: str, src_folder: str, dst_folder: str
    ) -> bool:
        """Move a message from one folder to another."""
        try:
            client = await self._connect(src_folder)
            await client.copy(uid, dst_folder)
            await client.store(uid, "+FLAGS", "\\Deleted")
            await client.expunge()
            await client.logout()
            return True
        except Exception as e:
            log.error(f"Failed to move message: {e}")
            return False

    async def set_flag(
        self, uid: str, folder: str, flag: str, value: bool
    ) -> bool:
        """Set or unset an IMAP flag (e.g. \\Seen, \\Flagged)."""
        try:
            client = await self._connect(folder)
            op = "+FLAGS" if value else "-FLAGS"
            await client.store(uid, op, flag)
            await client.logout()
            return True
        except Exception as e:
            log.error(f"Failed to set flag: {e}")
            return False

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_format: str = "text",
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        reply_to_message_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> bool:
        """Send an email via Bridge SMTP."""
        try:
            msg = MIMEMultipart("alternative") if body_format == "html" \
                else MIMEText(body, "plain")

            msg["From"]    = self.user
            msg["To"]      = ", ".join(to)
            msg["Subject"] = subject

            if cc:
                msg["Cc"] = ", ".join(cc)
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
            if references:
                msg["References"] = references

            if body_format == "html":
                msg.attach(MIMEText(body, "html"))

            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            with smtplib.SMTP(self.host, 1025) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.user, self.password)
                all_recipients = to + (cc or []) + (bcc or [])
                smtp.sendmail(self.user, all_recipients, msg.as_string())
            return True
        except Exception as e:
            log.error(f"Failed to send email: {e}")
            return False

    def _parse_full_message(
        self, raw: bytes, folder: str, uid: str
    ) -> FullMessage:
        msg = email.message_from_bytes(raw)
        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = part.get("Content-Disposition", "")
                if "attachment" in cd:
                    attachments.append({
                        "filename": part.get_filename() or "unnamed",
                        "content_type": ct,
                        "size": len(part.get_payload(decode=True) or b""),
                    })
                elif ct == "text/plain" and not body_text:
                    payload = part.get_payload(decode=True) or b""
                    body_text = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                elif ct == "text/html" and not body_html:
                    payload = part.get_payload(decode=True) or b""
                    body_html = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        else:
            payload = msg.get_payload(decode=True) or b""
            body_text = payload.decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )

        from email.utils import parsedate_to_datetime
        try:
            date = parsedate_to_datetime(msg.get("Date", ""))
        except Exception:
            date = datetime.utcnow()

        return FullMessage(
            message_id=msg.get("Message-ID", "").strip("<>"),
            subject=msg.get("Subject", ""),
            from_addr=msg.get("From", ""),
            to_addrs=[a.strip() for a in msg.get("To", "").split(",")],
            cc_addrs=[a.strip() for a in msg.get("Cc", "").split(",") if msg.get("Cc")],
            date=date,
            body_text=body_text,
            body_html=body_html,
            folder=folder,
            imap_uid=uid,
            attachments=attachments,
        )
