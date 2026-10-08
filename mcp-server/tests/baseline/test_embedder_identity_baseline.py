"""mcp-server's embedder identity check against the built baseline (#1268).

The build records the loopback hash-embedding service
(``indexer/tests/baseline/embed_server.py``) as the index's embedder,
with the port it was allocated. Served again on that port, the service
passes ``verify_embedder_identity`` with the production ``EmbedClient``;
the same service under any other endpoint is refused.

The service runs in the indexer's environment (it imports the indexer's
``src``), so it is started as a subprocess with ``uv run`` from
``indexer/``. Loopback only; no provider or credential.

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs it.
"""

import asyncio
import os
import subprocess
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from src.lib.embed import EmbedClient
from src.lib.embed_identity import EmbedderIdentityError, verify_embedder_identity
from src.lib.sqlite import Database

pytestmark = pytest.mark.baseline

_INDEXER = Path(__file__).parents[3] / "indexer"


@pytest.fixture(scope="module")
def db() -> Database:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Database(str(Path(baseline_dir) / "mail.db"))


@pytest.fixture(scope="module")
def recorded(db: Database) -> dict:
    record = db.get_active_vector_generation()
    assert record is not None, "the baseline build recorded no embedder identity"
    return record


@contextmanager
def _serve(port: int) -> Iterator[str]:
    """Run the hash-embedding service on ``port`` (0: any free port) and
    yield its base URL; stop it with SIGTERM and require a clean exit."""
    proc = subprocess.Popen(
        ["uv", "run", "--frozen", "python", "-m", "tests.baseline.embed_server"]
        + ["--port", str(port)],
        cwd=_INDEXER,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        base_url = proc.stdout.readline().strip()
        assert base_url, "the hash-embedding service did not start (port taken?)"
        yield base_url
        proc.terminate()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def _verify(base_url: str, model: str, db: Database) -> None:
    async def run() -> None:
        client = EmbedClient(base_url=base_url, model=model, api_key="unauthenticated")
        try:
            await verify_embedder_identity(db, client, provider="openai", secrets=[])
        finally:
            await client.aclose()

    asyncio.run(run())


def test_build_recorded_the_loopback_service(recorded: dict):
    endpoint = urllib.parse.urlsplit(recorded["endpoint"])
    assert recorded["provider"] == "openai"
    assert (endpoint.scheme, endpoint.hostname, endpoint.path) == ("http", "127.0.0.1", "/v1")
    assert endpoint.port and endpoint.port > 0


def test_identity_check_accepts_the_recorded_endpoint(recorded, db):
    """Served again on the recorded port, the service is accepted."""
    port = urllib.parse.urlsplit(recorded["endpoint"]).port
    assert port is not None
    with _serve(port) as base_url:
        assert base_url == recorded["endpoint"]
        _verify(base_url, recorded["model"], db)


def test_identity_check_refuses_another_endpoint(recorded, db):
    """The same service on another allocated port: the vectors match,
    only the endpoint differs, and that alone is refused."""
    with _serve(0) as base_url:
        assert base_url != recorded["endpoint"]
        with pytest.raises(EmbedderIdentityError, match="endpoint: index") as refused:
            _verify(base_url, recorded["model"], db)
    assert "calibration vector" not in str(refused.value)
