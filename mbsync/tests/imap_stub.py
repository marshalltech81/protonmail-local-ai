"""Synthetic IMAP server for mbsync/tests/tls_check.sh.

Speaks just enough IMAP for openssl s_client's certificate probe and for
one mbsync run against an empty account: a greeting, CAPABILITY, LOGIN,
LIST with no folders and LOGOUT. After LOGOUT it closes the TCP connection
without a TLS close_notify, the harshest close a client can see, so the
check also covers a tunnel that must pass that close on.

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
            command = rest.split(" ", 1)[0].upper()
            # Only a fixed set of command names is logged by name, so a
            # client's TLS bytes sent to the plaintext mode stay out of it.
            known = {"CAPABILITY", "STARTTLS", "LOGIN", "AUTHENTICATE", "LIST", "LOGOUT"}
            print(f"stub: tls={tls} command={command if command in known else 'OTHER'}", flush=True)
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
