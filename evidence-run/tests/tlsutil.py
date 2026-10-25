"""Local, synthetic TLS responders for the evidence-run tests: a
certificate generator, an implicit-TLS IMAP responder and an implicit-TLS
SMTP responder. Nothing here leaves the loopback interface."""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import socket
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class Cert:
    cert: Path
    key: Path
    fingerprint: str  # SHA-256 over the DER certificate, lower-case hex


def make_cert(directory: Path, name: str, *, ip: str = "127.0.0.1") -> Cert:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{name}.pem"
    key_path = directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    return Cert(cert_path, key_path, hashlib.sha256(der).hexdigest())


class _Server:
    """A threaded TCP server on 127.0.0.1 that wraps each connection in TLS
    with the next certificate of ``certs`` (cycling), then calls ``handle``."""

    def __init__(self, certs: list[Cert]) -> None:
        self.contexts = []
        for cert in certs:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert.cert, cert.key)
            self.contexts.append(context)
        self.connections = 0
        self.handshake_failures = 0
        self._lock = threading.Lock()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                context = self.contexts[self.connections % len(self.contexts)]
                self.connections += 1
            threading.Thread(target=self._one, args=(conn, context), daemon=True).start()

    def _one(self, conn: socket.socket, context: ssl.SSLContext) -> None:
        try:
            conn.settimeout(10)
            tls = context.wrap_socket(conn, server_side=True)
        except ssl.SSLError, OSError:
            with self._lock:
                self.handshake_failures += 1
            conn.close()
            return
        try:
            self.handle(tls)
        except OSError:
            pass
        finally:
            tls.close()

    def handle(self, tls: ssl.SSLSocket) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        self._stop = True
        self._sock.close()

    def __enter__(self) -> _Server:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


WRITE_COMMANDS = {
    "APPEND",
    "STORE",
    "EXPUNGE",
    "CLOSE",
    "COPY",
    "MOVE",
    "CREATE",
    "DELETE",
    "RENAME",
}


class ImapResponder(_Server):
    """Implicit-TLS IMAP: LOGIN, EXAMINE/SELECT, UID FETCH 1:* (UID
    BODY.PEEK[]) over ``messages`` (UID i+1), LOGOUT. Records command names
    and whether the session selected read-only."""

    def __init__(
        self, certs: list[Cert], messages: list[bytes], *, fail_size_fetch: bool = False
    ) -> None:
        self.messages = messages
        self.fail_size_fetch = fail_size_fetch
        self.flags = False  # also report FLAGS in the size FETCH
        self.commands: list[str] = []
        self.logins: list[tuple[str, str]] = []
        self.mailboxes: list[str] = []  # the raw EXAMINE/SELECT argument
        super().__init__(certs)

    def handle(self, tls: ssl.SSLSocket) -> None:
        stream = tls.makefile("rwb", buffering=0)

        def send(line: str) -> None:
            stream.write(line.encode() + b"\r\n")

        send("* OK IMAP4rev1 synthetic server ready")
        while line := stream.readline():
            tag, _, rest = line.decode(errors="replace").strip().partition(" ")
            words = rest.split()
            command = words[0].upper() if words else ""
            if command == "UID" and len(words) > 1:
                command = "UID " + words[1].upper()
            self.commands.append(command)
            if command == "CAPABILITY":
                send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                send(f"{tag} OK done")
            elif command == "LOGIN":
                self.logins.append((words[1].strip('"'), words[2].strip('"')))
                send(f"{tag} OK LOGIN completed")
            elif command in ("SELECT", "EXAMINE"):
                argument = rest[len(command) :].strip()
                self.mailboxes.append(argument)
                if " " in argument and not (argument.startswith('"') and argument.endswith('"')):
                    # A conforming server reads an unquoted name with a space
                    # as two arguments.
                    send(f"{tag} BAD too many arguments")
                    continue
                send(f"* {len(self.messages)} EXISTS")
                send("* OK [UIDVALIDITY 1] valid")
                send(f"{tag} OK [READ-ONLY] {command} completed")
            elif command == "UID FETCH" and self.fail_size_fetch and "RFC822.SIZE" in rest:
                send(f"{tag} NO transient backend failure")
            elif command == "UID FETCH":
                wanted = words[2]
                for index, raw in enumerate(self.messages, start=1):
                    if wanted not in ("1:*", str(index)):
                        continue
                    if "RFC822.SIZE" in rest:
                        flags = " FLAGS (\\Seen)" if self.flags and "FLAGS" in rest else ""
                        send(f"* {index} FETCH (UID {index}{flags} RFC822.SIZE {len(raw)})")
                    else:
                        head = f"* {index} FETCH (UID {index} BODY[] {{{len(raw)}}}\r\n"
                        stream.write(head.encode() + raw + b")\r\n")
                send(f"{tag} OK UID FETCH completed")
            elif command.removeprefix("UID ") in WRITE_COMMANDS:
                send(f"{tag} NO read-only synthetic server")
            elif command == "LOGOUT":
                send("* BYE")
                send(f"{tag} OK LOGOUT completed")
                return
            else:
                send(f"{tag} OK {command} completed")


@dataclass
class SmtpState:
    commands: list[str] = field(default_factory=list)
    auth_attempts: int = 0
    received: list[bytes] = field(default_factory=list)


class SmtpResponder(_Server):
    """Implicit-TLS SMTP (the SMTPS shape ``smtplib.SMTP_SSL`` speaks):
    EHLO, AUTH PLAIN, MAIL, RCPT, DATA (dot-unstuffed, kept as received),
    QUIT. ``drop_after_data`` closes the connection once DATA is read,
    before the final 250, so the client cannot know the outcome."""

    def __init__(
        self, certs: list[Cert], *, drop_after_data: bool = False, refuse_rcpt: bool = False
    ) -> None:
        self.state = SmtpState()
        self.drop_after_data = drop_after_data
        self.refuse_rcpt = refuse_rcpt
        super().__init__(certs)

    def handle(self, tls: ssl.SSLSocket) -> None:
        stream = tls.makefile("rwb", buffering=0)

        def send(line: str) -> None:
            stream.write(line.encode() + b"\r\n")

        send("220 synthetic ESMTP ready")
        while line := stream.readline():
            text = line.decode(errors="replace").strip()
            command = text.split(" ", 1)[0].upper()
            self.state.commands.append(command)
            if command in ("EHLO", "HELO"):
                send("250-synthetic")
                send("250 AUTH PLAIN LOGIN")
            elif command == "AUTH":
                self.state.auth_attempts += 1
                send("235 2.7.0 authenticated")
            elif command == "MAIL":
                send("250 2.1.0 ok")
            elif command == "RCPT":
                send("550 5.1.1 refused" if self.refuse_rcpt else "250 2.1.5 ok")
            elif command == "DATA":
                send("354 end data with <CR><LF>.<CR><LF>")
                body = b""
                while True:
                    chunk = stream.readline()
                    if chunk == b".\r\n" or not chunk:
                        break
                    body += chunk[1:] if chunk.startswith(b"..") else chunk
                self.state.received.append(body)
                if self.drop_after_data:
                    return
                send("250 2.0.0 queued")
            elif command == "QUIT":
                send("221 bye")
                return
            else:
                send("250 ok")
