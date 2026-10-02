"""stdio adapter for MCP clients that only launch local commands.

Claude Desktop's ``claude_desktop_config.json`` starts MCP servers as
commands and talks to them over stdio; it cannot connect to a URL. This
module runs on the operator's Mac (not in the container), speaks stdio
to the client and relays every request to the server's Streamable HTTP
endpoint at ``http://127.0.0.1:${MCP_PORT:-3000}/mcp`` with the bearer
token, using fastmcp's proxy. See docs/setup.md, "Connect an MCP
client", for the ``claude_desktop_config.json`` entry.

The token is read from a file, by default ``.secrets/mcp_auth_token.txt``
at the repository root, or the path given with ``--token-file``. It is
never taken from an argument or an environment variable, which other
local accounts could read from the process list. The file must be a
regular file with mode 600 or stricter and hold a non-empty RFC 6750
token; otherwise the adapter exits with a fixed message before
connecting.

Nothing the adapter writes to stderr contains the token or a tool's
arguments or results (mail content): logging is disabled for the whole
process, and the only output is the adapter's own fixed messages.
"""

import argparse
import logging
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import fastmcp
import httpx2
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

_DEFAULT_TOKEN_FILE = Path(__file__).resolve().parents[2] / ".secrets" / "mcp_auth_token.txt"
_DEFAULT_PORT = "3000"
# RFC 6750 ``b64token`` characters ('=' as trailing padding), as
# scripts/mcp-auth-headers.sh and mcp-server startup check.
# ``openssl rand -hex 32`` output always matches.
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")


class AdapterConfigError(Exception):
    """A configuration problem, reported with a fixed message."""


def read_token(path: Path) -> str:
    """The bearer token in ``path``, with surrounding whitespace removed.

    Fails closed, with a message naming only the path, when the file is
    missing, is not a regular file, is readable or writable by anyone
    but its owner, is empty, or holds characters outside RFC 6750.
    """
    try:
        info = path.stat()
    except OSError:
        raise AdapterConfigError(f"MCP bearer token file {path} does not exist or cannot be read.")
    if not stat.S_ISREG(info.st_mode):
        raise AdapterConfigError(f"MCP bearer token file {path} is not a regular file.")
    if info.st_mode & 0o077:
        raise AdapterConfigError(
            f"MCP bearer token file {path} must have mode 600; run chmod 600 on it."
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeError:
        raise AdapterConfigError(f"MCP bearer token file {path} cannot be read.")
    if not token:
        raise AdapterConfigError(f"MCP bearer token file {path} is empty.")
    if _TOKEN_RE.fullmatch(token) is None:
        raise AdapterConfigError(
            f"MCP bearer token in {path} has characters outside "
            "A-Z a-z 0-9 . _ ~ + / - (and trailing =); regenerate it with openssl rand -hex 32."
        )
    return token


def server_url(port: str) -> str:
    """The server's Streamable HTTP URL on this machine's IPv4 loopback.

    Compose publishes the port on ``127.0.0.1`` only. ``localhost`` can
    resolve to ``::1`` first, where another local account could listen
    and collect the bearer token, so the address is spelled out.
    """
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise AdapterConfigError("MCP_PORT must be a port number between 1 and 65535.")
    return f"http://127.0.0.1:{int(port)}/mcp"


def _loopback_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
    **_ignored: Any,
) -> httpx2.AsyncClient:
    """The HTTP client for the server connection, ignoring proxy settings.

    ``trust_env=False`` stops ``HTTP_PROXY``, ``ALL_PROXY`` and the macOS
    system proxy from routing the token and tool traffic through a
    proxy. Other keywords fastmcp passes (``follow_redirects``) are
    dropped: as in the MCP SDK's default client, redirects are not
    followed. Timeouts default to the SDK's (30 s, 300 s read).
    """
    return httpx2.AsyncClient(
        headers=headers,
        timeout=timeout or httpx2.Timeout(30.0, read=300.0),
        auth=auth,
        trust_env=False,
    )


def build_proxy(url: str, token: str) -> fastmcp.FastMCP:
    """A fastmcp proxy relaying to ``url`` with ``Authorization: Bearer``."""
    # A string ``auth`` makes the transport send the token as a bearer
    # header on every request; it is not part of the URL.
    transport = StreamableHttpTransport(url, auth=token, httpx_client_factory=_loopback_http_client)
    return create_proxy(transport, name="protonmail-local-ai")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--token-file",
        type=Path,
        default=_DEFAULT_TOKEN_FILE,
        help="path of the file holding the bearer token (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    # Log records from fastmcp, the MCP SDK and httpx can carry tool
    # arguments, results and error text; none is written anywhere.
    logging.disable(logging.CRITICAL)
    # As in src.main: create no OpenTelemetry spans.
    fastmcp.settings.telemetry_mode = "off"

    try:
        token = read_token(args.token_file)
        url = server_url(os.environ.get("MCP_PORT", _DEFAULT_PORT))
    except AdapterConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        build_proxy(url, token).run(transport="stdio", show_banner=False)
    except Exception as exc:
        # The type alone: an exception's text can quote request or
        # response data.
        print(f"ERROR: MCP stdio adapter stopped ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
