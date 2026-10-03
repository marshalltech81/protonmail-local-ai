"""Embedder identity check at startup (PLAN Phase 2 item 1, first slice).

The indexer records which embedder built the index — one ``active``
``vector_generations`` row with the provider, resolved endpoint, model,
dimensions and a calibration vector (``indexer/src/embed_identity.py``).
mcp-server only reads it: at startup it compares its own embedder's
configuration with the row and re-embeds the calibration text. Query
vectors from a different embedder are not comparable with the indexed
ones, so a mismatch refuses to start instead of serving silently wrong
semantic results. No row yet (the indexer is still waiting for its
embedder) also refuses, and the restart policy retries, as for an index
the indexer has not created.

``CALIBRATION_TEXT``, ``CALIBRATION_MAX_COSINE_DISTANCE``,
``sanitize_endpoint``, ``cosine_distance`` and ``mismatches`` mirror the
indexer's copies byte for byte; both services' tests pin the text's
digest. Messages never quote a provider response.
"""

import asyncio
import hashlib
import logging
import math
import urllib.parse
from collections.abc import Callable, Iterable
from typing import Protocol

from .embed import DEFAULT_EMBED_TIMEOUT_SECS
from .security import safe_provider_exception_text
from .sqlite import Database

log = logging.getLogger("mcp.embed_identity")

# Never change: byte-identical to the indexer's copy, whose digest is
# stored with every index.
CALIBRATION_TEXT = (
    "Embedder calibration sample: the quarterly maintenance window for the "
    "shared storage cluster moves to Tuesday at 09:00 UTC. Please confirm the "
    "backup schedule and approve the invoice for the replacement drives."
)
CALIBRATION_SHA256 = hashlib.sha256(CALIBRATION_TEXT.encode("utf-8")).hexdigest()

# See the indexer's ``CALIBRATION_MAX_COSINE_DISTANCE`` for the reasoning.
CALIBRATION_MAX_COSINE_DISTANCE = 0.01

_REMEDY = (
    "Vectors from a different embedder are not comparable with the stored ones. "
    "Restore the original EMBED_BASE_URL and EMBED_MODEL (and the model the server "
    "behind them loads), or rebuild the index from Maildir; see docs/troubleshooting.md "
    '"Embedder identity mismatch".'
)


class EmbedderIdentityError(RuntimeError):
    """The configured embedder is not the one that built the index, or the
    indexer has not recorded one yet. Fixed text plus configuration values
    and computed distances only."""


class CalibrationRequestError(RuntimeError):
    """The calibration embed request failed; the message holds the
    ``safe_provider_exception_text`` form of the cause."""


class _EmbedClient(Protocol):
    model: str
    base_url: str

    async def embed(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


def sanitize_endpoint(url: str) -> str:
    """Scheme, host, port and path of a resolved endpoint; userinfo, query
    and fragment dropped."""
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


async def verify_embedder_identity(
    db: Database,
    client: _EmbedClient,
    *,
    provider: str,
    secrets: Iterable[str],
    deadline_secs: float = DEFAULT_EMBED_TIMEOUT_SECS,
) -> None:
    """Raise unless ``client`` is the embedder that built the index.

    ``deadline_secs`` bounds the whole calibration request: the client's
    own timeout is per operation, so a provider sending a response in
    small fragments could otherwise hold startup indefinitely.
    """
    stored = await asyncio.to_thread(db.get_active_vector_generation)
    if stored is None:
        raise EmbedderIdentityError(
            "The indexer has not recorded the embedder behind this index yet; it "
            "does so once its embedder answers. mcp-server exits and the restart "
            "policy retries; see docs/troubleshooting.md "
            '"Embedder identity mismatch".'
        )
    try:
        vector = await asyncio.wait_for(client.embed(CALIBRATION_TEXT), deadline_secs)
    except TimeoutError:
        raise CalibrationRequestError(
            f"Embedder calibration request did not answer within {deadline_secs:g} s"
        ) from None
    except Exception as exc:
        raise CalibrationRequestError(
            "Embedder calibration request failed: "
            + safe_provider_exception_text(exc, list(secrets))
        ) from None
    found = mismatches(
        stored,
        provider=provider,
        endpoint=sanitize_endpoint(client.base_url),
        model=client.model,
        vector=vector,
    )
    if found:
        raise EmbedderIdentityError(
            "The configured embedder is not the one that built this index ("
            + "; ".join(found)
            + "). "
            + _REMEDY
        )


def run_startup_identity_check(
    db: Database,
    make_client: Callable[[], _EmbedClient],
    *,
    provider: str,
    secrets: Iterable[str],
    deadline_secs: float = DEFAULT_EMBED_TIMEOUT_SECS,
) -> None:
    """Run ``verify_embedder_identity`` before the server starts; exit on
    failure with the fixed message.

    Uses its own client, closed before returning: an async HTTP client
    is tied to the event loop it first ran on, and the server runs its
    own loop later.
    """

    async def run() -> None:
        client = make_client()
        try:
            await verify_embedder_identity(
                db, client, provider=provider, secrets=secrets, deadline_secs=deadline_secs
            )
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    except (EmbedderIdentityError, CalibrationRequestError) as exc:
        raise SystemExit(str(exc)) from None
    log.info("Embedder identity verified against the index")
