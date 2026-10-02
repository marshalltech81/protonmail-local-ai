"""
The stdio adapter Claude Desktop launches (``src.stdio_adapter``).

A stub server, built with the same ``src.main._build_app`` the container
serves (bearer token and Host/Origin guard included), runs under uvicorn
on a loopback port. The adapter is driven both in process and as the
subprocess Claude Desktop would start, over real stdio, so the tests pin
that tools/list and tool calls are relayed, that the token goes out as
``Authorization: Bearer``, that a missing, wrong or loosely permissioned
token fails closed, and that neither the token nor tool payloads reach
the adapter's stderr.
"""

import os
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
import src.stdio_adapter as adapter
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from src.main import _build_app

# Synthetic values; neither may appear in anything the adapter writes.
_TOKEN = "synthetic-adapter-token-9b1e-xxxxxxxxxxxxxxxx"
_MARKER = "synthetic-mail-marker-7f3a"
_MCP_SERVER_DIR = Path(__file__).resolve().parents[1]


class _HeaderRecorder:
    """ASGI wrapper recording the Authorization header of each /mcp request."""

    def __init__(self, app) -> None:
        self.app = app
        self.seen: list[str | None] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope["headers"])
            value = headers.get(b"authorization")
            self.seen.append(value.decode() if value is not None else None)
        await self.app(scope, receive, send)


def _stub_server() -> FastMCP:
    server = FastMCP("stdio-adapter-test")

    @server.tool()
    async def echo(text: str) -> str:
        return f"echo {text}"

    @server.tool()
    async def fails(text: str) -> str:
        raise RuntimeError(f"unexpected {text}")

    return server


@pytest.fixture(scope="module")
def stub() -> Iterator[tuple[int, _HeaderRecorder]]:
    """The stub server on a free loopback port, and its header recorder."""
    app = _HeaderRecorder(_build_app(_stub_server(), session_idle_timeout=60.0, auth_token=_TOKEN))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    assert server.started
    yield port, app
    server.should_exit = True
    thread.join(timeout=10)


def _token_file(tmp_path: Path, token: str = _TOKEN, mode: int = 0o600) -> Path:
    path = tmp_path / "mcp_auth_token.txt"
    path.write_text(f"{token}\n")
    path.chmod(mode)
    return path


# ---------------------------------------------------------------------------
# Token file and URL validation
# ---------------------------------------------------------------------------


class TestReadToken:
    def test_reads_and_strips_token(self, tmp_path):
        assert adapter.read_token(_token_file(tmp_path)) == _TOKEN

    def test_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(adapter.AdapterConfigError, match="does not exist"):
            adapter.read_token(tmp_path / "absent.txt")

    def test_directory_fails_closed(self, tmp_path):
        with pytest.raises(adapter.AdapterConfigError, match="not a regular file"):
            adapter.read_token(tmp_path)

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
    def test_group_or_other_access_fails_closed(self, tmp_path, mode):
        with pytest.raises(adapter.AdapterConfigError, match="mode 600") as excinfo:
            adapter.read_token(_token_file(tmp_path, mode=mode))
        assert _TOKEN not in str(excinfo.value)

    def test_owner_read_only_is_accepted(self, tmp_path):
        assert adapter.read_token(_token_file(tmp_path, mode=0o400)) == _TOKEN

    def test_empty_file_fails_closed(self, tmp_path):
        with pytest.raises(adapter.AdapterConfigError, match="is empty"):
            adapter.read_token(_token_file(tmp_path, token="  "))

    def test_unreadable_file_fails_closed(self, tmp_path):
        path = _token_file(tmp_path)
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(adapter.AdapterConfigError, match="cannot be read"):
            adapter.read_token(path)

    def test_token_with_header_breaking_characters_fails_closed(self, tmp_path):
        bad = f"{_MARKER}\r\nX-Injected: 1"
        with pytest.raises(adapter.AdapterConfigError, match="characters outside") as excinfo:
            adapter.read_token(_token_file(tmp_path, token=bad))
        assert _MARKER not in str(excinfo.value)

    def test_equals_sign_only_as_trailing_padding(self, tmp_path):
        # #589: one b64token rule across mcp-server, validate-env.sh,
        # mcp-auth-headers.sh and this adapter.
        with pytest.raises(adapter.AdapterConfigError, match="characters outside"):
            adapter.read_token(_token_file(tmp_path, token="synthetic=marker-xxxxxxxxxxxxxxxx"))
        padded = "synthetic-token-xxxxxxxxxxxxxxxx=="
        assert adapter.read_token(_token_file(tmp_path, token=padded)) == padded


class TestServerUrl:
    def test_default_port_on_ipv4_loopback(self):
        # Review round 1: the server is published on 127.0.0.1 only, and
        # ``localhost`` can resolve to ::1 first, where another local
        # account could listen and collect the bearer token.
        assert adapter.server_url("3000") == "http://127.0.0.1:3000/mcp"

    @pytest.mark.parametrize("port", ["", "0", "65536", "30a", "-1", "3000/x"])
    def test_rejects_non_port(self, port):
        with pytest.raises(adapter.AdapterConfigError, match="MCP_PORT"):
            adapter.server_url(port)


# ---------------------------------------------------------------------------
# main(): fixed messages and exit codes
# ---------------------------------------------------------------------------


class TestMain:
    def test_config_error_exits_1_with_fixed_message(self, tmp_path, capsys):
        assert adapter.main(["--token-file", str(tmp_path / "absent.txt")]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("ERROR: MCP bearer token file")

    def test_bad_port_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MCP_PORT", "nope")
        assert adapter.main(["--token-file", str(_token_file(tmp_path))]) == 1
        assert "MCP_PORT" in capsys.readouterr().err

    def test_runs_proxy_over_stdio(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_PORT", "3123")
        calls: list[tuple[str, str, dict]] = []

        class _Proxy:
            def run(self, **kwargs):
                calls.append((url, token, kwargs))

        def fake_build(u, t):
            nonlocal url, token
            url, token = u, t
            return _Proxy()

        url = token = ""
        monkeypatch.setattr(adapter, "build_proxy", fake_build)
        assert adapter.main(["--token-file", str(_token_file(tmp_path))]) == 0
        assert calls == [
            (
                "http://127.0.0.1:3123/mcp",
                _TOKEN,
                {"transport": "stdio", "show_banner": False},
            )
        ]

    def test_runtime_failure_reports_type_only(self, tmp_path, monkeypatch, capsys):
        class _Proxy:
            def run(self, **kwargs):
                raise RuntimeError(f"{_MARKER} {_TOKEN}")

        monkeypatch.setattr(adapter, "build_proxy", lambda u, t: _Proxy())
        assert adapter.main(["--token-file", str(_token_file(tmp_path))]) == 1
        err = capsys.readouterr().err
        assert err == "ERROR: MCP stdio adapter stopped (RuntimeError).\n"


# ---------------------------------------------------------------------------
# Relaying to a real Streamable HTTP server
# ---------------------------------------------------------------------------


class TestProxyInProcess:
    def test_lists_and_calls_tools_with_bearer_header(self, stub):
        port, recorder = stub
        recorder.seen.clear()
        proxy = adapter.build_proxy(f"http://127.0.0.1:{port}/mcp", _TOKEN)

        async def run():
            async with Client(proxy) as client:
                names = sorted(t.name for t in await client.list_tools())
                result = await client.call_tool("echo", {"text": "hi"})
                return names, result.data

        names, data = anyio.run(run)
        assert names == ["echo", "fails"]
        assert data == "echo hi"
        assert recorder.seen
        assert set(recorder.seen) == {f"Bearer {_TOKEN}"}

    def test_wrong_token_exposes_no_tools(self, stub):
        # The server answers 401; the proxy then lists no tools and
        # refuses every call, so nothing reaches a tool.
        port, recorder = stub
        recorder.seen.clear()
        proxy = adapter.build_proxy(f"http://127.0.0.1:{port}/mcp", "wrong-token")

        async def run():
            async with Client(proxy) as client:
                tools = await client.list_tools()
                result = await client.call_tool("echo", {"text": "hi"}, raise_on_error=False)
                return tools, result

        tools, result = anyio.run(run)
        assert tools == []
        assert result.is_error
        assert "echo hi" not in str(result.content)
        assert recorder.seen
        assert set(recorder.seen) == {"Bearer wrong-token"}


class TestAmbientProxyIgnored:
    """Review round 1: proxy settings in the environment must not route
    the token and tool traffic away from the loopback connection."""

    def test_proxy_environment_is_not_used(self, stub, monkeypatch):
        port, recorder = stub
        recorder.seen.clear()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)
        proxy_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        accepted: list[bytes] = []
        stop = threading.Event()

        def accept_loop() -> None:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                with conn:
                    conn.settimeout(1.0)
                    try:
                        accepted.append(conn.recv(65536))
                    except TimeoutError:
                        accepted.append(b"")

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy_url)
            monkeypatch.setenv(name.lower(), proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        try:
            proxy = adapter.build_proxy(f"http://127.0.0.1:{port}/mcp", _TOKEN)

            async def run():
                async with Client(proxy) as client:
                    names = sorted(t.name for t in await client.list_tools())
                    result = await client.call_tool("echo", {"text": "hi"})
                    return names, result.data

            names, data = anyio.run(run)
        finally:
            stop.set()
            thread.join(timeout=5)
            listener.close()
        assert accepted == []
        assert names == ["echo", "fails"]
        assert data == "echo hi"
        assert set(recorder.seen) == {f"Bearer {_TOKEN}"}


def _stdio_client(port: int, token_file: Path, stderr_path: Path) -> Client:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env["MCP_PORT"] = str(port)
    # At DEBUG, fastmcp logs each tool call with its arguments; the
    # adapter must still write none of it.
    env["FASTMCP_LOG_LEVEL"] = "DEBUG"
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "src.stdio_adapter", "--token-file", str(token_file)],
        env=env,
        cwd=str(_MCP_SERVER_DIR),
        log_file=stderr_path,
        keep_alive=False,
    )
    return Client(transport)


class TestStdioSubprocess:
    """The adapter as Claude Desktop runs it: a child process on stdio."""

    def test_relays_tools_and_keeps_payloads_out_of_stderr(self, stub, tmp_path):
        port, recorder = stub
        recorder.seen.clear()
        stderr_path = tmp_path / "adapter-stderr.log"
        client = _stdio_client(port, _token_file(tmp_path), stderr_path)

        async def run():
            async with client:
                names = sorted(t.name for t in await client.list_tools())
                ok = await client.call_tool("echo", {"text": _MARKER})
                failed = await client.call_tool("fails", {"text": _MARKER}, raise_on_error=False)
                return names, ok.data, failed.is_error

        names, data, is_error = anyio.run(run)
        assert names == ["echo", "fails"]
        assert data == f"echo {_MARKER}"
        assert is_error
        assert set(recorder.seen) == {f"Bearer {_TOKEN}"}
        stderr = stderr_path.read_text()
        assert _TOKEN not in stderr
        assert _MARKER not in stderr

    def test_wrong_token_fails_without_leaking_it(self, stub, tmp_path):
        port, recorder = stub
        recorder.seen.clear()
        wrong = "synthetic-wrong-token-41c2"
        stderr_path = tmp_path / "adapter-stderr.log"
        client = _stdio_client(port, _token_file(tmp_path, token=wrong), stderr_path)

        async def run():
            async with client:
                tools = await client.list_tools()
                result = await client.call_tool("echo", {"text": _MARKER}, raise_on_error=False)
                return tools, result

        tools, result = anyio.run(run)
        assert tools == []
        assert result.is_error
        assert f"echo {_MARKER}" not in str(result.content)
        assert set(recorder.seen) == {f"Bearer {wrong}"}
        stderr = stderr_path.read_text()
        assert wrong not in stderr
        assert _MARKER not in stderr

    def test_missing_token_file_exits_before_connecting(self, stub, tmp_path):
        port, recorder = stub
        recorder.seen.clear()
        stderr_path = tmp_path / "adapter-stderr.log"
        client = _stdio_client(port, tmp_path / "absent.txt", stderr_path)

        async def run():
            async with client:
                await client.list_tools()

        with pytest.raises(Exception):
            anyio.run(run)
        assert recorder.seen == []
        assert "ERROR: MCP bearer token file" in stderr_path.read_text()
