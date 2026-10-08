"""Loopback ``/v1/embeddings`` service over the baseline's hashed embedder.

Serves ``hash_embedder.embed_text`` in the OpenAI-compatible
``POST /v1/embeddings`` shape, so the production clients (the indexer's
``OpenAIEmbedder``, mcp-server's ``EmbedClient``) can talk to it. The
baseline build records this endpoint as the index's embedder identity
(``build.py``), and a later run serves it again on the recorded port so
mcp-server's identity check accepts the index (#803, #1268).

It binds ``127.0.0.1`` only, on the port given (``0``: one the kernel
allocates). Vectors are returned as JSON floats whatever
``encoding_format`` asks for: ``json`` writes each float's shortest
round-trip form, so a client reads back exactly what ``embed_text``
computed, and the OpenAI SDK accepts a float list where it asked for
base64 (it decodes only string embeddings). Base64 would be float32 and
lose precision. Requests are not logged.

Usage, from ``indexer/`` (prints the base URL, serves until SIGTERM or
SIGINT):

    uv run python -m tests.baseline.embed_server [--port N]
"""

import argparse
import json
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.baseline.hash_embedder import embed_text

HOST = "127.0.0.1"
# The model name the build records; the service answers any name, and the
# identity check compares the configured name with the recorded one.
MODEL = "baseline-hash-embedder"


def embeddings_response(body: object) -> dict | None:
    """The ``/v1/embeddings`` response for a request body, or ``None``
    when ``input`` is not a string or a non-empty list of strings."""
    if not isinstance(body, dict):
        return None
    texts = body.get("input")
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        return None
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": embed_text(t)}
            for i, t in enumerate(texts)
        ],
        "model": body.get("model", MODEL),
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/embeddings":
            self._reply(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        response = embeddings_response(body)
        if response is None:
            self._reply(400, {"error": {"message": "input must be a string or list of strings"}})
            return
        self._reply(200, response)

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return None


@contextmanager
def serve(port: int = 0) -> Iterator[str]:
    """Serve on ``127.0.0.1:port`` in a background thread and yield the
    base URL (``http://127.0.0.1:<port>/v1``); on exit stop serving,
    close the socket and join the thread."""
    server = ThreadingHTTPServer((HOST, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, name="hash-embed-server")
    thread.start()
    try:
        yield f"http://{HOST}:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with serve(args.port) as base_url:
        print(base_url, flush=True)
        stop.wait()


if __name__ == "__main__":
    main(sys.argv[1:])
