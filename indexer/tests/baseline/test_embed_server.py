"""The loopback hash-embedding service (#1268): exact vectors, loopback
only, a kernel-allocated port, and a clean stop."""

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from openai import OpenAI
from src.embedder import OpenAIEmbedder

from tests.baseline.embed_server import MODEL, serve
from tests.baseline.hash_embedder import embed_text

_INDEXER = Path(__file__).parents[2]
_TEXTS = ["Quarterly maintenance window moves to Tuesday", "maintenence", "", "ünïcødé — 42"]


def _post(base_url: str, body: object, path: str = "/embeddings") -> tuple[int, dict]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read())


def test_raw_response_is_byte_identical_to_the_hash_embedder():
    with serve() as base_url:
        status, payload = _post(base_url, {"model": MODEL, "input": _TEXTS})
    assert status == 200
    assert [d["index"] for d in payload["data"]] == list(range(len(_TEXTS)))
    assert [d["embedding"] for d in payload["data"]] == [embed_text(t) for t in _TEXTS]
    assert payload["model"] == MODEL


def test_sdk_reads_back_exact_vectors_for_its_default_base64_request():
    """The SDK asks for base64 by default; the service answers floats,
    which the SDK passes through unchanged, so nothing is rounded to
    float32."""
    with serve() as base_url:
        client = OpenAI(base_url=base_url, api_key="unauthenticated", max_retries=0)
        try:
            single = client.embeddings.create(model=MODEL, input=_TEXTS[0])
            batch = client.embeddings.create(model=MODEL, input=_TEXTS)
        finally:
            client.close()
    assert single.data[0].embedding == embed_text(_TEXTS[0])
    assert [d.embedding for d in batch.data] == [embed_text(t) for t in _TEXTS]


def test_production_embedder_reads_the_hash_vectors():
    with serve() as base_url:
        embedder = OpenAIEmbedder(base_url, MODEL, api_key="unauthenticated")
        try:
            vectors = embedder.embed_batch(_TEXTS[:2])
        finally:
            embedder.client.close()
    assert vectors == [embed_text(t) for t in _TEXTS[:2]]


@pytest.mark.parametrize(
    "body", [{"input": []}, {"input": [1, 2]}, {"input": None}, {}, ["not", "an", "object"]]
)
def test_rejects_an_input_that_is_not_text(body):
    with serve() as base_url:
        status, payload = _post(base_url, body)
    assert status == 400 and "error" in payload


def test_unknown_path_is_not_found():
    with serve() as base_url:
        status, _ = _post(base_url, {"input": "x"}, path="/chat/completions")
    assert status == 404


def test_binds_loopback_on_an_allocated_port_and_stops_cleanly():
    with serve() as base_url:
        host, port = base_url.removeprefix("http://").removesuffix("/v1").split(":")
        assert host == "127.0.0.1"
        assert int(port) > 0
        with socket.create_connection((host, int(port)), timeout=5):
            pass
    with pytest.raises(OSError):
        socket.create_connection((host, int(port)), timeout=1).close()
    # The port is free again, so a later run can serve the recorded endpoint.
    with serve(int(port)) as again:
        assert again == base_url


def test_cli_serves_the_requested_port_and_exits_on_sigterm():
    with serve() as first:
        port = int(first.rsplit(":", 1)[1].removesuffix("/v1"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.baseline.embed_server", "--port", str(port)],
        cwd=_INDEXER,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        base_url = proc.stdout.readline().strip()
        assert base_url == first
        status, payload = _post(base_url, {"input": "hello"})
        assert status == 200 and payload["data"][0]["embedding"] == embed_text("hello")
        proc.terminate()
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
