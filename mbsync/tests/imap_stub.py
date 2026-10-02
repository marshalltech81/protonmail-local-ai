"""Synthetic STARTTLS IMAP server for mbsync/tests/tls_check.sh.

Speaks just enough IMAP for openssl s_client's STARTTLS probe and for one
mbsync run against an empty account: a greeting, CAPABILITY, STARTTLS,
LOGIN (refused before TLS), LIST with no folders and LOGOUT. After LOGOUT
it closes the TCP connection without a TLS close_notify, the harshest
close a client can see, so the check also covers a tunnel that must pass
that close on.

Usage: python imap_stub.py CERT KEY PORT
"""

import socket
import ssl
import sys
import threading


def serve(conn: socket.socket, context: ssl.SSLContext) -> None:
    stream = conn.makefile("rwb", buffering=0)
    tls = False

    def send(line: str) -> None:
        stream.write(line.encode() + b"\r\n")

    send("* OK IMAP4rev1 synthetic server ready")
    while line := stream.readline():
        tag, _, rest = line.decode(errors="replace").strip().partition(" ")
        command = rest.split(" ", 1)[0].upper()
        print(f"stub: tls={tls} command={command}", flush=True)
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
        elif command == "LOGIN":
            if not tls:
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
    stream.close()
    conn.close()


def main() -> None:
    cert, key, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = socket.create_server(("0.0.0.0", port))
    print(f"stub: listening on {port}", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve, args=(conn, context), daemon=True).start()


if __name__ == "__main__":
    main()
