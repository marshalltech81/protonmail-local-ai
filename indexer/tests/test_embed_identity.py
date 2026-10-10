"""Embedder identity record and startup check (#645).

The index records which embedder built it (``vector_generations``, one
``active`` row) plus a calibration vector, and every startup re-embeds
the calibration text and compares, so vectors from a different model
are never mixed into the index.
"""

import hashlib
import logging
import math
import sqlite3

import httpx2 as httpx
import openai
import pytest
from src import main
from src.database import EMBEDDING_DIM, Database
from src.embed_identity import (
    CALIBRATION_MAX_COSINE_DISTANCE,
    CALIBRATION_SHA256,
    CALIBRATION_TEXT,
    CalibrationRequestError,
    EmbedderDimensionError,
    EmbedderIdentityError,
    cosine_distance,
    sanitize_endpoint,
    verify_or_record_embedder,
)

from tests.baseline.hash_embedder import HashEmbedder, embed_text
from tests.conftest import make_message, make_thread

ENDPOINT = "http://host.docker.internal:8001/v1"
MODEL = "synthetic-embed-model"


class _Embedder:
    """Deterministic embedder returning ``vector`` for every input."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self.vector)


def _unit(n: int, hot: int = 0) -> list[float]:
    vec = [0.0] * n
    vec[hot] = 1.0
    return vec


def _record(db: Database, embedder, *, model: str = MODEL, endpoint: str = ENDPOINT) -> str:
    return verify_or_record_embedder(
        db, embedder, provider="openai", endpoint=endpoint, model=model
    )


def _rows(db: Database) -> list[sqlite3.Row]:
    return db._conn.execute("SELECT * FROM vector_generations").fetchall()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "mail.db")
    yield database
    database.close()


# --- the calibration text is pinned ------------------------------------------


def test_calibration_text_is_pinned():
    # mcp-server pins the same digest (``mcp-server/tests/test_embed_identity.py``):
    # the two services must embed byte-identical text, and changing it
    # would invalidate every recorded index.
    assert CALIBRATION_SHA256 == hashlib.sha256(CALIBRATION_TEXT.encode("utf-8")).hexdigest()
    assert (
        CALIBRATION_SHA256
        == (
            "cc906a93e5713eb5bbd12034c582ee221e31e08234edb06398f0c663cfa63910"  # pragma: allowlist secret
        )
    )


# --- schema --------------------------------------------------------------------


def test_fresh_schema_has_empty_vector_generations(db):
    columns = [r["name"] for r in db._conn.execute("PRAGMA table_info(vector_generations)")]
    assert columns == [
        "generation_id",
        "provider",
        "endpoint",
        "model",
        "revision",
        "dimensions",
        "tokenizer",
        "context_window",
        "chunk_config_hash",
        "label",
        "calibration_sha256",
        "calibration_vector",
        "created_at",
        "status",
    ]
    assert _rows(db) == []


def test_status_is_constrained(db):
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO vector_generations (provider, endpoint, model, dimensions, "
            "calibration_sha256, calibration_vector, created_at, status) "
            "VALUES ('openai', 'e', 'm', 4, 'h', x'00', 't', 'bogus')"
        )


def test_at_most_one_active_generation(db):
    insert = (
        "INSERT INTO vector_generations (provider, endpoint, model, dimensions, "
        "calibration_sha256, calibration_vector, created_at, status) "
        "VALUES ('openai', 'e', 'm', 4, 'h', x'00', 't', 'active')"
    )
    db._conn.execute(insert)
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(insert)


# --- fresh index records one row ----------------------------------------------


def test_fresh_index_records_one_active_row(db):
    embedder = _Embedder([0.6, 0.8, 0.0, 0.0])
    assert _record(db, embedder, endpoint=ENDPOINT + "/?token=abc#frag") == "recorded"
    assert embedder.calls == [CALIBRATION_TEXT]
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "active"
    assert row["provider"] == "openai"
    assert row["model"] == MODEL
    # Resolved endpoint, with the query and fragment dropped.
    assert row["endpoint"] == ENDPOINT
    assert row["dimensions"] == 4
    assert row["calibration_sha256"] == CALIBRATION_SHA256
    stored = db.get_active_vector_generation()
    assert stored is not None
    assert stored["calibration_vector"] == pytest.approx([0.6, 0.8, 0.0, 0.0], abs=1e-6)
    assert stored["created_at"]
    # Not exposed by the OpenAI-compatible embeddings API today.
    for column in ("revision", "tokenizer", "context_window", "chunk_config_hash", "label"):
        assert row[column] is None


def test_matching_restart_passes_and_keeps_one_row(db):
    _record(db, _Embedder(_unit(8)))
    assert _record(db, _Embedder(_unit(8))) == "verified"
    assert len(_rows(db)) == 1


def test_hashed_embedder_is_deterministic(db):
    # The test embedder is exactly reproducible: distance 0.
    assert cosine_distance(embed_text(CALIBRATION_TEXT), embed_text(CALIBRATION_TEXT)) == 0.0
    _record(db, HashEmbedder())
    assert _record(db, HashEmbedder()) == "verified"


def test_drift_within_tolerance_passes(db):
    base = _unit(8)
    _record(db, _Embedder(base))
    # A provider's run-to-run float noise (well under the tolerance).
    drifted = list(base)
    drifted[1] = 0.01
    assert cosine_distance(base, drifted) < CALIBRATION_MAX_COSINE_DISTANCE
    assert _record(db, _Embedder(drifted)) == "verified"


# --- mismatches fail closed with a fixed message ------------------------------


def _assert_mismatch(exc: EmbedderIdentityError, *needles: str) -> None:
    text = str(exc)
    assert "not the one that built this index" in text
    assert "rebuild the index" in text
    assert "docs/troubleshooting.md" in text
    for needle in needles:
        assert needle in text


def test_different_model_fails_closed(db):
    _record(db, _Embedder(_unit(8)))
    with pytest.raises(EmbedderIdentityError) as info:
        _record(db, _Embedder(_unit(8)), model="other-model")
    _assert_mismatch(info.value, "EMBED_MODEL", MODEL, "other-model")


def test_different_endpoint_fails_closed(db):
    _record(db, _Embedder(_unit(8)))
    with pytest.raises(EmbedderIdentityError) as info:
        _record(db, _Embedder(_unit(8)), endpoint="https://api.openai.com/v1")
    _assert_mismatch(info.value, "endpoint", ENDPOINT, "https://api.openai.com/v1")


def test_different_dimensions_fails_closed(db):
    _record(db, _Embedder(_unit(8)))
    with pytest.raises(EmbedderIdentityError) as info:
        _record(db, _Embedder(_unit(16)))
    _assert_mismatch(info.value, "dimensions", "8", "16")


def test_same_name_different_vector_fails_closed(db):
    # A server reloaded a different same-dimension model under one name.
    _record(db, _Embedder(_unit(8, hot=0)))
    with pytest.raises(EmbedderIdentityError) as info:
        _record(db, _Embedder(_unit(8, hot=3)))
    _assert_mismatch(info.value, "calibration")


def test_changed_calibration_text_fails_closed(db):
    _record(db, _Embedder(_unit(8)))
    db._conn.execute("UPDATE vector_generations SET calibration_sha256 = 'other'")
    db._conn.commit()
    with pytest.raises(EmbedderIdentityError) as info:
        _record(db, _Embedder(_unit(8)))
    _assert_mismatch(info.value, "calibration text")


def test_built_index_without_record_fails_closed(db):
    # Vectors exist but nothing says which embedder wrote them: never
    # bless the current one.
    thread = make_thread([make_message()])
    db.upsert_thread(thread, _unit(4096))
    with pytest.raises(EmbedderIdentityError) as info:
        _record(db, _Embedder(_unit(8)))
    assert "rebuild the index" in str(info.value)
    assert _rows(db) == []


def test_wrong_width_fails_before_a_fresh_index_records(db):
    # #841: the calibration vector doubles as the width check, so a fresh
    # index must not record an embedder whose vectors it cannot store.
    embedder = _Embedder(_unit(16))
    with pytest.raises(EmbedderDimensionError) as info:
        verify_or_record_embedder(
            db, embedder, provider="openai", endpoint=ENDPOINT, model=MODEL, dimensions=8
        )
    assert str(info.value).startswith("Embedder produced 16-dim vectors")
    assert "reserves 8-dim" in str(info.value)
    assert embedder.calls == [CALIBRATION_TEXT]
    assert _rows(db) == []


def test_wrong_width_fails_before_a_recorded_index_is_compared(db):
    _record(db, _Embedder(_unit(8)))
    with pytest.raises(EmbedderDimensionError):
        verify_or_record_embedder(
            db, _Embedder(_unit(8)), provider="openai", endpoint=ENDPOINT, model=MODEL, dimensions=4
        )
    assert [row["dimensions"] for row in _rows(db)] == [8]


def test_index_from_before_the_record_fails_with_rebuild_message(db):
    db._conn.execute("DROP TABLE vector_generations")
    db._conn.commit()
    with pytest.raises(RuntimeError, match="wipe the sqlite-volume"):
        db.get_active_vector_generation()


# --- provider errors during calibration ---------------------------------------

_MARKER = "SYNTHETIC-CALIBRATION-MARKER-7f3a"


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://host.docker.internal:8001/v1/embeddings")
    response = httpx.Response(status, request=request, json={"error": _MARKER})
    return openai.APIStatusError(f"error {_MARKER}", response=response, body={"error": _MARKER})


class _FailingEmbedder:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def embed(self, text: str) -> list[float]:
        raise self.exc


@pytest.mark.parametrize("status", [401, 503])
def test_calibration_provider_error_is_scrubbed(db, caplog, status):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CalibrationRequestError) as info:
        _record(db, _FailingEmbedder(_status_error(status)))
    assert f"status={status}" in str(info.value)
    assert _MARKER not in str(info.value)
    assert _MARKER not in caplog.text
    assert _rows(db) == []


def test_main_check_exits_with_fixed_message_on_mismatch(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    embedder = _Embedder(_unit(EMBEDDING_DIM))
    embedder.base_url = ENDPOINT  # type: ignore[attr-defined]
    monkeypatch.setattr(main, "EMBED_MODEL", MODEL)
    main._check_embedder_identity(db, embedder)
    assert "Recorded embedder identity" in caplog.text
    main._check_embedder_identity(db, embedder)
    assert "Embedder identity verified" in caplog.text
    monkeypatch.setattr(main, "EMBED_MODEL", "other-model")
    with pytest.raises(SystemExit) as info:
        main._check_embedder_identity(db, embedder)
    assert "not the one that built this index" in str(info.value.code)


def test_main_check_exits_on_calibration_outage(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    embedder = _FailingEmbedder(_status_error(503))
    embedder.base_url = ENDPOINT  # type: ignore[attr-defined]
    monkeypatch.setattr(main, "EMBED_MODEL", MODEL)
    with pytest.raises(SystemExit) as info:
        main._check_embedder_identity(db, embedder)
    assert "status=503" in str(info.value.code)
    assert _MARKER not in str(info.value.code)
    assert _MARKER not in caplog.text


# --- helpers -----------------------------------------------------------------


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
def test_sanitize_endpoint(url, expected):
    assert sanitize_endpoint(url) == expected


def test_cosine_distance_edges():
    assert cosine_distance([1.0, 0.0], [2.0, 0.0]) == pytest.approx(0.0)
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    # A zero or non-finite vector is never "close".
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 2.0
    assert cosine_distance([math.nan, 0.0], [1.0, 0.0]) == 2.0
    assert cosine_distance([1.0], [1.0, 0.0]) == 2.0
