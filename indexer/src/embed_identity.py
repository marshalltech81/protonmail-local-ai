"""Embedder identity record and startup check (PLAN Phase 2 item 1, first slice).

The index records which embedder built it: one ``vector_generations``
row, status ``active``, holding the provider, the resolved endpoint,
the model, the vector dimensions and a **calibration vector** — the
embedding of ``CALIBRATION_TEXT`` taken when the index was created. On
every start the indexer (here) and mcp-server
(``mcp-server/src/lib/embed_identity.py``) compare the configured
embedder against that row and re-embed the calibration text. A model,
endpoint or dimension change, or a calibration vector further than
``CALIBRATION_MAX_COSINE_DISTANCE`` from the stored one (a server that
reloaded a different model under the same name, a moved provider
alias), fails startup closed: vectors from a different embedder are not
comparable with the stored ones, and mixing them corrupts semantic
search silently.

Only the indexer writes the row, on a fresh index. Lifecycle (building
a second generation, switching, retiring) is out of scope until the
Phase 2 design document lands.

Messages never quote a provider response: a calibration request failure
is reduced by ``scrub_embed_error``, and a mismatch names configuration
values and a computed distance only.
"""

import hashlib
import logging
import math
import urllib.parse
from typing import Protocol

from .database import Database
from .embedder import scrub_embed_error

log = logging.getLogger("indexer.embed_identity")

# Fixed synthetic text. Never change it: its digest is stored with every
# index and pinned by both services' tests, and mcp-server holds a
# byte-identical copy.
CALIBRATION_TEXT = (
    "Embedder calibration sample: the quarterly maintenance window for the "
    "shared storage cluster moves to Tuesday at 09:00 UTC. Please confirm the "
    "backup schedule and approve the invoice for the replacement drives."
)
CALIBRATION_SHA256 = hashlib.sha256(CALIBRATION_TEXT.encode("utf-8")).hexdigest()

# Cosine distance (1 - cosine similarity) beyond which the calibration
# vector means a different embedder. Re-embedding one input on the same
# deployment differs only by float noise: zero for a deterministic
# server, and well under 1e-3 for GPU-batched providers. Unrelated
# models' vectors for one text sit near distance 1 (their spaces are not
# aligned), and coarse re-quantizations of one model drift by about 1e-2,
# so 0.01 leaves a wide margin either side. Same-dimension swaps are what
# this catches; model, endpoint and dimension changes are caught by the
# field comparison first.
CALIBRATION_MAX_COSINE_DISTANCE = 0.01

_REMEDY = (
    "Vectors from a different embedder are not comparable with the stored ones. "
    "Restore the original EMBED_BASE_URL and EMBED_MODEL (and the model the server "
    "behind them loads), or rebuild the index from Maildir; see docs/troubleshooting.md "
    '"Embedder identity mismatch".'
)


class EmbedderIdentityError(RuntimeError):
    """The configured embedder is not the one that built the index.

    Messages are fixed text plus configuration values and computed
    distances, never provider response content.
    """


class EmbedderDimensionError(RuntimeError):
    """The calibration vector is not as wide as the schema's vector columns.

    The message names the two widths only.
    """


class CalibrationRequestError(RuntimeError):
    """The calibration embed request failed (outage or configuration).

    The message is the ``scrub_embed_error`` form of the cause.
    """


class _Embeds(Protocol):
    def embed(self, text: str) -> list[float]: ...


def sanitize_endpoint(url: str) -> str:
    """The resolved endpoint as stored and compared: scheme, host, port
    and path only. Userinfo, query and fragment are dropped so no
    credential a URL might carry reaches the database."""
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def cosine_distance(a: list[float], b: list[float]) -> float:
    """``1 - cos(a, b)``; 2.0 (the maximum) when the vectors differ in
    length, are zero or hold non-finite values."""
    if len(a) != len(b):
        return 2.0
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b))
    if not math.isfinite(dot) or not math.isfinite(norm) or norm == 0.0:
        return 2.0
    return 1.0 - dot / norm


def _calibrate(embedder: _Embeds) -> list[float]:
    try:
        return embedder.embed(CALIBRATION_TEXT)
    except Exception as exc:
        raise CalibrationRequestError(
            f"Embedder calibration request failed: {scrub_embed_error(exc)}"
        ) from None


def mismatches(
    stored: dict,
    *,
    provider: str,
    endpoint: str,
    model: str,
    vector: list[float],
) -> list[str]:
    """Each way the configured embedder differs from the stored record."""
    found: list[str] = []
    if stored["provider"] != provider:
        found.append(f"EMBED_MODE: index {stored['provider']!r}, configured {provider!r}")
    if stored["model"] != model:
        found.append(f"EMBED_MODEL: index {stored['model']!r}, configured {model!r}")
    if stored["endpoint"] != endpoint:
        found.append(f"endpoint: index {stored['endpoint']!r}, configured {endpoint!r}")
    if stored["dimensions"] != len(vector):
        found.append(f"dimensions: index {stored['dimensions']}, embedder {len(vector)}")
    elif stored["calibration_sha256"] != CALIBRATION_SHA256:
        found.append("calibration text: differs from the release that recorded the index")
    else:
        distance = cosine_distance(stored["calibration_vector"], vector)
        if distance > CALIBRATION_MAX_COSINE_DISTANCE:
            found.append(
                f"calibration vector: cosine distance {distance:.4f} exceeds "
                f"{CALIBRATION_MAX_COSINE_DISTANCE}"
            )
    return found


def verify_or_record_embedder(
    db: Database,
    embedder: _Embeds,
    *,
    provider: str,
    endpoint: str,
    model: str,
    dimensions: int | None = None,
) -> str:
    """Record the embedder on a fresh index, else verify it matches.

    Returns ``"recorded"`` or ``"verified"``. Raises
    ``EmbedderDimensionError`` when ``dimensions`` is given and the
    calibration vector has another width, before the record is read or
    written, so a fresh index never records an embedder it cannot store
    vectors from; ``EmbedderIdentityError`` on a mismatch, or when the
    index already holds messages but no record (nothing says which
    embedder wrote its vectors, so the current one is never assumed);
    and ``CalibrationRequestError`` when the calibration request fails.
    """
    endpoint = sanitize_endpoint(endpoint)
    vector = _calibrate(embedder)
    if dimensions is not None and len(vector) != dimensions:
        raise EmbedderDimensionError(
            f"Embedder produced {len(vector)}-dim vectors, but the SQLite "
            f"schema reserves {dimensions}-dim (threads_vec "
            f"FLOAT[{dimensions}]). Either switch to a model that "
            f"outputs {dimensions}-dim vectors, or migrate the schema."
        )
    stored = db.get_active_vector_generation()
    if stored is None:
        if db.count_total_messages():
            raise EmbedderIdentityError(
                "The index holds messages but no record of the embedder that built "
                "them, so the indexer cannot tell whether the configured embedder "
                "matches. Wipe the sqlite-volume and rebuild the index from Maildir; "
                "see docs/troubleshooting.md "
                '"Embedder identity mismatch".'
            )
        db.record_vector_generation(
            provider=provider,
            endpoint=endpoint,
            model=model,
            calibration_sha256=CALIBRATION_SHA256,
            calibration_vector=vector,
        )
        return "recorded"
    found = mismatches(stored, provider=provider, endpoint=endpoint, model=model, vector=vector)
    if found:
        raise EmbedderIdentityError(
            "The configured embedder is not the one that built this index ("
            + "; ".join(found)
            + "). "
            + _REMEDY
        )
    return "verified"
