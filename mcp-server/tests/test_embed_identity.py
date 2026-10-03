"""mcp-server's embedder identity check (#645).

The indexer records which embedder built the index (one ``active``
``vector_generations`` row with a calibration vector); mcp-server reads
it at startup and refuses to serve when its own embedder differs, since
its query vectors would not be comparable with the indexed ones.
"""

import asyncio
import hashlib
import logging
import sqlite3
import struct

import httpx
import openai
import pytest
from src.lib.embed_identity import (
    CALIBRATION_SHA256,
    CALIBRATION_TEXT,
    CalibrationRequestError,
    EmbedderIdentityError,
    cosine_distance,
    run_startup_identity_check,
    sanitize_endpoint,
    verify_embedder_identity,
)
from src.lib.sqlite import Database

ENDPOINT = "http://host.docker.internal:8001/v1"
MODEL = "synthetic-embed-model"
_MARKER = "SYNTHETIC-CALIBRATION-MARKER-9c1e"

# The indexer's DDL (``indexer/src/database.py``
# ``_run_vector_generation_schema_script``).
_DDL = """
CREATE TABLE vector_generations (
    generation_id      INTEGER PRIMARY KEY,
    provider           TEXT NOT NULL,
    endpoint           TEXT NOT NULL,
    model              TEXT NOT NULL,
    revision           TEXT,
    dimensions         INTEGER NOT NULL,
    tokenizer          TEXT,
    context_window     INTEGER,
    chunk_config_hash  TEXT,
    label              TEXT,
    calibration_sha256 TEXT NOT NULL,
    calibration_vector BLOB NOT NULL,
    created_at         TEXT NOT NULL,
    status             TEXT NOT NULL
)
"""


def _unit(n: int, hot: int = 0) -> list[float]:
    vec = [0.0] * n
    vec[hot] = 1.0
    return vec


def _make_db(tmp_path, vector: list[float] | None = None, *, table: bool = True, **fields):
    path = tmp_path / "mail.db"
    conn = sqlite3.connect(path)
    if table:
        conn.execute(_DDL)
    if vector is not None:
        row = {
            "provider": "openai",
            "endpoint": ENDPOINT,
            "model": MODEL,
            "dimensions": len(vector),
            "calibration_sha256": CALIBRATION_SHA256,
        } | fields
        conn.execute(
            "INSERT INTO vector_generations (provider, endpoint, model, dimensions, "
            "calibration_sha256, calibration_vector, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, '2026-10-02T00:00:00+00:00', 'active')",
            (
                row["provider"],
                row["endpoint"],
                row["model"],
                row["dimensions"],
                row["calibration_sha256"],
                struct.pack(f"{len(vector)}f", *vector),
            ),
        )
    conn.commit()
    conn.close()
    return Database(str(path))


class _Client:
    def __init__(self, vector=None, *, exc=None, model=MODEL, base_url=ENDPOINT):
        self.vector = vector
        self.exc = exc
        self.model = model
        self.base_url = base_url
        self.calls: list[str] = []
        self.closed = False

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return list(self.vector)

    async def aclose(self) -> None:
        self.closed = True


def _verify(db, client) -> None:
    asyncio.run(verify_embedder_identity(db, client, provider="openai", secrets=["sk-x"]))


def _assert_mismatch(exc: EmbedderIdentityError, *needles: str) -> None:
    text = str(exc)
    assert "not the one that built this index" in text
    assert "rebuild the index" in text
    assert "docs/troubleshooting.md" in text
    for needle in needles:
        assert needle in text


def test_calibration_text_matches_the_indexer():
    # Same pinned digest as ``indexer/tests/test_embed_identity.py``.
    assert CALIBRATION_SHA256 == hashlib.sha256(CALIBRATION_TEXT.encode("utf-8")).hexdigest()
    assert (
        CALIBRATION_SHA256
        == (
            "cc906a93e5713eb5bbd12034c582ee221e31e08234edb06398f0c663cfa63910"  # pragma: allowlist secret
        )
    )


def test_matching_embedder_passes(tmp_path):
    db = _make_db(tmp_path, [0.6, 0.8, 0.0, 0.0])
    client = _Client([0.6, 0.8, 0.0, 0.0])
    _verify(db, client)
    assert client.calls == [CALIBRATION_TEXT]


def test_endpoint_is_compared_sanitized(tmp_path):
    db = _make_db(tmp_path, _unit(8))
    _verify(db, _Client(_unit(8), base_url=ENDPOINT + "/?key=abc"))


def test_unnormalized_query_vector_compares_by_direction(tmp_path):
    # The indexer stores unit vectors; mcp-server's client does not
    # normalize, so the check is scale-invariant.
    db = _make_db(tmp_path, _unit(8))
    _verify(db, _Client([3.0] + [0.0] * 7))


def test_different_model_fails_closed(tmp_path):
    db = _make_db(tmp_path, _unit(8))
    with pytest.raises(EmbedderIdentityError) as info:
        _verify(db, _Client(_unit(8), model="other-model"))
    _assert_mismatch(info.value, "EMBED_MODEL", MODEL, "other-model")


def test_different_endpoint_fails_closed(tmp_path):
    db = _make_db(tmp_path, _unit(8))
    with pytest.raises(EmbedderIdentityError) as info:
        _verify(db, _Client(_unit(8), base_url="https://api.openai.com/v1"))
    _assert_mismatch(info.value, "endpoint", ENDPOINT, "https://api.openai.com/v1")


def test_different_provider_fails_closed(tmp_path):
    db = _make_db(tmp_path, _unit(8), provider="other")
    with pytest.raises(EmbedderIdentityError) as info:
        _verify(db, _Client(_unit(8)))
    _assert_mismatch(info.value, "EMBED_MODE")


def test_different_dimensions_fails_closed(tmp_path):
    db = _make_db(tmp_path, _unit(8))
    with pytest.raises(EmbedderIdentityError) as info:
        _verify(db, _Client(_unit(16)))
    _assert_mismatch(info.value, "dimensions", "8", "16")


def test_same_name_different_vector_fails_closed(tmp_path):
    db = _make_db(tmp_path, _unit(8, hot=0))
    with pytest.raises(EmbedderIdentityError) as info:
        _verify(db, _Client(_unit(8, hot=5)))
    _assert_mismatch(info.value, "calibration vector")


def test_changed_calibration_text_fails_closed(tmp_path):
    db = _make_db(tmp_path, _unit(8), calibration_sha256="other")
    with pytest.raises(EmbedderIdentityError) as info:
        _verify(db, _Client(_unit(8)))
    _assert_mismatch(info.value, "calibration text")


def test_no_record_yet_fails_until_the_indexer_writes_it(tmp_path):
    db = _make_db(tmp_path)
    client = _Client(_unit(8))
    with pytest.raises(EmbedderIdentityError, match="has not recorded"):
        _verify(db, client)
    # Nothing sent to the provider before there is something to compare.
    assert client.calls == []


def test_index_from_before_the_record_fails_with_rebuild_message(tmp_path):
    db = _make_db(tmp_path, table=False)
    with pytest.raises(RuntimeError, match="wipe the sqlite-volume"):
        db.get_active_vector_generation()


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", ENDPOINT + "/embeddings")
    response = httpx.Response(status, request=request, json={"error": _MARKER})
    return openai.APIStatusError(f"error {_MARKER}", response=response, body={"error": _MARKER})


@pytest.mark.parametrize("exc", [_status_error(503), ValueError(_MARKER)])
def test_calibration_provider_error_is_scrubbed(tmp_path, caplog, exc):
    caplog.set_level(logging.DEBUG)
    db = _make_db(tmp_path, _unit(8))
    with pytest.raises(CalibrationRequestError) as info:
        _verify(db, _Client(exc=exc))
    assert "calibration request failed" in str(info.value)
    assert _MARKER not in str(info.value)
    assert _MARKER not in caplog.text


def test_startup_check_closes_the_client_and_exits_on_mismatch(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    db = _make_db(tmp_path, _unit(8))
    ok = _Client(_unit(8))
    run_startup_identity_check(db, lambda: ok, provider="openai", secrets=[])
    assert ok.closed
    assert "Embedder identity verified" in caplog.text

    bad = _Client(_unit(8), model="other-model")
    with pytest.raises(SystemExit) as info:
        run_startup_identity_check(db, lambda: bad, provider="openai", secrets=[])
    assert bad.closed
    assert "not the one that built this index" in str(info.value.code)

    down = _Client(exc=_status_error(503))
    with pytest.raises(SystemExit) as info:
        run_startup_identity_check(db, lambda: down, provider="openai", secrets=[])
    assert "status=503" in str(info.value.code)
    assert _MARKER not in str(info.value.code)
    assert _MARKER not in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://host.docker.internal:8001/v1", "http://host.docker.internal:8001/v1"),
        ("http://host.docker.internal:8001/v1/", "http://host.docker.internal:8001/v1"),
        ("https://api.openai.com/v1?api_key=s3cret#x", "https://api.openai.com/v1"),
        ("https://user:pw@example.test/v1", "https://example.test/v1"),  # pragma: allowlist secret
        ("http://[::1]:8001/v1", "http://[::1]:8001/v1"),
    ],
)
def test_sanitize_endpoint_matches_the_indexer(url, expected):
    assert sanitize_endpoint(url) == expected


def test_cosine_distance_edges():
    assert cosine_distance([1.0, 0.0], [2.0, 0.0]) == pytest.approx(0.0)
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 2.0
    assert cosine_distance([float("nan"), 0.0], [1.0, 0.0]) == 2.0
    assert cosine_distance([1.0], [1.0, 0.0]) == 2.0


def test_embed_client_aclose_closes_its_http_client():
    from src.lib.embed import EmbedClient

    client = EmbedClient(base_url=ENDPOINT, model=MODEL, api_key="placeholder")
    asyncio.run(client.aclose())
    assert client.client.is_closed()
