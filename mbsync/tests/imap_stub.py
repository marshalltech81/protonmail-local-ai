"""Synthetic IMAP server for mbsync/tests/tls_check.sh.

Speaks just enough IMAP for openssl s_client's certificate probe and for
an mbsync pull of one folder: a greeting, CAPABILITY, LOGIN, LIST with
INBOX alone, SELECT, the two UID FETCH commands isync 1.5.1 sends for a
new message (UID FLAGS, then BODY.PEEK[], with INTERNALDATE when
CopyArrivalDate is on, #1132) and LOGOUT. INBOX holds one synthetic
message whose INTERNALDATE has a non-UTC offset, so the check can compare
the near file's mtime with that date in UTC. Every response is fixed:
this is not an IMAP server. A command that would change the far side
(APPEND, STORE, EXPUNGE, CLOSE, COPY, MOVE or a folder command, with or without
UID) is refused and logged as a write, so the check can show the pull
never sends one. After LOGOUT it closes the TCP connection without a TLS
close_notify, the harshest close a client can see, so the check also
covers a tunnel that must pass that close on.

MODE is ``implicit`` (the TLS handshake comes first, as Bridge serves it
for mbsync, #638) or ``starttls`` (a plaintext greeting offering STARTTLS,
as a Bridge left in its STARTTLS mode serves it). In ``starttls`` mode
LOGIN is refused before TLS, and every command is logged, so the check can
show mbsync never sends credentials to such a server.

Usage: python imap_stub.py CERT KEY PORT MODE
"""

import socket
import ssl
import sys
import threading

# The one message in INBOX, UID 1. 05:04:05 at +0200 is 03:04:05 UTC.
INTERNALDATE = "02-Jan-2020 05:04:05 +0200"
MESSAGE = (
    b"From: Synthetic Sender <sender@sender.example>\r\n"
    b"To: Synthetic Recipient <recipient@recipient.example>\r\n"
    b"Subject: Synthetic arrival date check\r\n"
    b"Message-ID: <arrival-date-check@stub.example>\r\n"
    b"Date: Thu, 02 Jan 2020 05:04:05 +0200\r\n"
    b"\r\n"
    b"Synthetic body for the CopyArrivalDate check.\r\n"
)
# Commands that would change the far side. CLOSE expunges every message
# flagged \Deleted in a box opened with SELECT (RFC 3501 6.4.2).
WRITES = {"APPEND", "STORE", "EXPUNGE", "CLOSE", "COPY", "MOVE", "CREATE", "DELETE", "RENAME"}
# Only these command names are logged by name, so a client's TLS bytes
# sent to the plaintext mode stay out of the log.
KNOWN = {"CAPABILITY", "STARTTLS", "LOGIN", "AUTHENTICATE", "LIST", "SELECT", "EXAMINE"}
KNOWN |= {"LOGOUT", "UID FETCH"} | WRITES | {f"UID {name}" for name in WRITES}


def serve(conn: socket.socket, context: ssl.SSLContext, implicit: bool) -> None:
    tls = False
    if implicit:
        try:
            conn = context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            print(f"stub: handshake failed: {type(exc).__name__}", flush=True)
            conn.close()
            return
        tls = True
    stream = conn.makefile("rwb", buffering=0)

    def send(line: str) -> None:
        stream.write(line.encode() + b"\r\n")

    try:
        send("* OK IMAP4rev1 synthetic server ready")
        while line := stream.readline():
            tag, _, rest = line.decode(errors="replace").strip().partition(" ")
            words = rest.upper().replace("(", " ").replace(")", " ").split()
            command = " ".join(words[:2]) if words[:1] == ["UID"] else "".join(words[:1])
            print(f"stub: tls={tls} command={command if command in KNOWN else 'OTHER'}", flush=True)
            if command == "CAPABILITY":
                caps = "IMAP4rev1 AUTH=PLAIN" if tls else "IMAP4rev1 STARTTLS LOGINDISABLED"
                send(f"* CAPABILITY {caps}")
                send(f"{tag} OK CAPABILITY completed")
            elif command == "STARTTLS" and not tls:
                send(f"{tag} OK begin TLS")
                try:
                    conn = context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    print(f"stub: handshake refused by client: {type(exc).__name__}", flush=True)
                    return
                stream = conn.makefile("rwb", buffering=0)
                tls = True
            elif command in ("LOGIN", "AUTHENTICATE"):
                if not tls:
                    print("stub: credentials before TLS", flush=True)
                    send(f"{tag} NO LOGIN needs TLS")
                    continue
                print("stub: login over TLS", flush=True)
                send(f"{tag} OK LOGIN completed")
            elif command.removeprefix("UID ") in WRITES:
                print("stub: write command refused", flush=True)
                send(f"{tag} NO read-only synthetic server")
            elif command == "LIST":
                send('* LIST (\\HasNoChildren) "/" "INBOX"')
                send(f"{tag} OK LIST completed")
            elif command in ("SELECT", "EXAMINE"):
                send("* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
                send("* 1 EXISTS")
                send("* 0 RECENT")
                send("* OK [UIDVALIDITY 1132] UIDs valid")
                send("* OK [UIDNEXT 2] predicted next UID")
                send(f"{tag} OK {command} completed")
            elif command == "UID FETCH":
                # Answer only the items isync names, so it gets an
                # INTERNALDATE only when it asks for one.
                if "BODY.PEEK[]" in words:
                    internaldate = "INTERNALDATE" in words
                    print(f"stub: fetch body internaldate={internaldate}", flush=True)
                    date = f'INTERNALDATE "{INTERNALDATE}" ' if internaldate else ""
                    head = f"* 1 FETCH (UID 1 {date}BODY[] {{{len(MESSAGE)}}}\r\n"
                    stream.write(head.encode() + MESSAGE + b")\r\n")
                else:
                    send("* 1 FETCH (UID 1 FLAGS ())")
                send(f"{tag} OK UID FETCH completed")
            elif command == "LOGOUT":
                send("* BYE logging out")
                send(f"{tag} OK LOGOUT completed")
                break
            else:
                send(f"{tag} OK {command} completed")
    except OSError as exc:
        print(f"stub: connection ended: {type(exc).__name__}", flush=True)
    stream.close()
    conn.close()


def main() -> None:
    cert, key, port, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    if mode not in ("implicit", "starttls"):
        raise SystemExit(f"unknown mode {mode!r}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = socket.create_server(("0.0.0.0", port))
    print(f"stub: listening on {port} ({mode})", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(
            target=serve, args=(conn, context, mode == "implicit"), daemon=True
        ).start()


if __name__ == "__main__":
    main()
